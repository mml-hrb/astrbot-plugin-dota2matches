"""录像下载、解析与单波团数据块的组装。

为什么要单独一个模块
--------------------
``dota_replay`` 里是「算哪波值得看」和「排队」，纯计算、无副作用、可离线测。
这里则是**有副作用且重**的那一半：下载几十 MB、解压、起 JVM 进程、读回产物。
分开是为了让队列和候选逻辑不必依赖 httpx / 进程管理，测试也好写。

三条时间轴（每次都要现标，不能写死）
------------------------------------
* demo tick 时间 —— Clarity 采样用的 ``tickNo / 30``
* 战斗日志时间 —— ``CombatLogEntry.getTimestamp()``
* 比赛时间 —— OpenDota 口径，玩家看到的一切时间都是这个

三者互不相等。本模块用「英雄阵亡时刻集合」对齐前两条，用「建筑倒塌时刻」
与 ``match.objectives`` 对齐后两条 —— 都是**当次录像现算**的，
因为偏移取决于选人与录像起点，不同比赛不一样。
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Callable, Iterable

import httpx

#: JVM 堆上限。实测 192m 时峰值 RSS 约 160 MB，且能正常解析 25 分钟的录像；
#: 再往上给不会更快（瓶颈在 Clarity 的流式解析），只会多占云端容器的余量。
HEAP_MB = 192

#: 单个 java 进程的墙钟上限。云端实测 3 个导出合计 21 秒，留足余量。
STEP_TIMEOUT = 180.0

#: 录像下载上限（国内 CDN 实测 16 MB/s，21 MB 约 1 秒）。
DOWNLOAD_TIMEOUT = 300.0

#: 数据块里事件流水的时间裕量（秒）：团战窗口前后各带一点，
#: 让模型能看到「开打前发生了什么」和「打完谁还在追」。
EVENT_PAD = 12.0


class ReplayError(Exception):
    """录像准备阶段的可预期失败（下载失败 / 解析失败 / 没有录像）。"""


# ======================================================================
# 文件准备
# ======================================================================


async def download_replay(url: str, dest: str, timeout: float = DOWNLOAD_TIMEOUT) -> str:
    """把录像下到 ``dest``（可能带 .bz2 后缀，实际是 zstd 流）。"""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".part"
    size = 0
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                     headers={"User-Agent": "AstrBot-dota2/1.0"}) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise ReplayError(f"录像下载失败：HTTP {resp.status_code}")
                with open(tmp, "wb") as fh:
                    async for chunk in resp.aiter_bytes(1 << 16):
                        fh.write(chunk)
                        size += len(chunk)
    except httpx.HTTPError as exc:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise ReplayError(f"录像下载失败：{exc}") from exc
    if size < 1024:
        os.remove(tmp)
        raise ReplayError("录像下载失败：文件过小，可能是录像已过期")
    os.replace(tmp, dest)
    return dest


def unpack_replay(src: str, dest: str) -> str:
    """解压 .dem.bz2。

    后缀写的是 bz2，**实际是 zstd 流**（Valve 从 2016 年起就这样），
    而且帧头没有写 content size，不能用一次性 ``decompress()``，
    必须 ``copy_stream`` 流式解。
    """
    try:
        import zstandard
    except ImportError as exc:  # pragma: no cover - 环境检测已经拦过一次
        raise ReplayError("缺少 zstandard 模块，无法解压录像") from exc
    try:
        with open(src, "rb") as fin, open(dest, "wb") as fout:
            zstandard.ZstdDecompressor().copy_stream(fin, fout)
    except Exception as exc:  # noqa: BLE001 - 解压库的异常类型不稳定
        raise ReplayError(f"录像解压失败：{exc}") from exc
    if not os.path.getsize(dest):
        raise ReplayError("录像解压失败：输出为空")
    return dest


async def run_java(
    java: str,
    engine: str,
    class_name: str,
    args: list[str],
    *,
    heap_mb: int = HEAP_MB,
    timeout: float = STEP_TIMEOUT,
    cwd: str | None = None,
) -> str:
    """跑一个导出器，返回 stdout/stderr 尾部（用于日志与判错）。"""
    lib = os.path.join(engine, "lib", "*")
    classpath = os.pathsep.join([os.path.join(engine, "classes"), lib])
    cmd = [java, f"-Xmx{heap_mb}m", "-cp", classpath, class_name, *args]
    proc = None
    out = b""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd or engine,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise ReplayError(f"解析超时（{class_name} 超过 {int(timeout)} 秒）") from exc
    except FileNotFoundError as exc:
        raise ReplayError(f"无法启动 Java：{java}") from exc
    finally:
        # 无论正常结束、超时还是被取消，都不能留下 JVM 进程 ——
        # 插件热重载会 cancel 后台任务，残留的 java 会一直占着 160 MB，
        # 多来几次整个容器就被 OOM 了。
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
    text = (out or b"").decode("utf-8", "replace")
    if proc.returncode != 0:
        tail = "\n".join(text.strip().splitlines()[-3:])
        raise ReplayError(f"解析失败（{class_name} 退出码 {proc.returncode}）：{tail}")
    return text


#: 胜率曲线的输出标记。``run_java`` 把 stderr 并进了 stdout，
#: 不加标记的话 slf4j 的日志会和 JSON 混在一起。
WIN_MARKER = "WINCHANCE:"

#: 只导胜率时的 JVM 上限与超时。实测 38 MB demo 用 6.5 秒、157 MB 内存 ——
#: 它不解析战斗日志、不采样血量，比完整解析轻两个数量级。
WIN_HEAP_MB = 256
WIN_TIMEOUT = 120.0


async def export_win_curve(
    java: str,
    engine: str,
    demo: str,
    *,
    timeout: float = WIN_TIMEOUT,
) -> dict:
    """只导出整局胜率曲线，**不碰战斗日志**。

    为什么要有这条轻量路径（2026-11-05）：战报要回答「哪一波才是关键团」，
    而阵亡人数和经济增量都回答不了 —— 实测有一波死了七个人，胜率只动了
    一个点（是劣势下的互换）；另一波只死三个，胜率却跳了九个点。
    判关键团得看「这波之后局势有没有变」，而这正是 Valve 在观战界面
    自己算的那条曲线。它写在 demo 里，读出来整局只要六秒多。

    Returns:
        ``{"start": float, "end": float, "chance": list[int],
        "radiant": list[int], "dire": list[int],
        "series": list[[float, float]]}``

        ``start`` / ``end`` 与 ``series`` 的时间都是 **demo 时间**（含选人），
        换算成比赛时间要减去 ``end - duration``，见
        :func:`dota_replay.align_curve`。

    Raises:
        ReplayError: JVM 起不来、超时，或输出里没有曲线数据。
    """
    text = await run_java(
        java, engine, "WinCurve", [demo], heap_mb=WIN_HEAP_MB, timeout=timeout
    )
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith(WIN_MARKER):
            continue
        try:
            data = json.loads(line[len(WIN_MARKER):])
        except ValueError as exc:
            raise ReplayError(f"胜率曲线解析失败：{exc}") from exc
        if isinstance(data, dict) and data.get("chance"):
            return data
        break
    raise ReplayError("胜率曲线导出失败：录像里没有曲线数据")


# ======================================================================
# 时间标定
# ======================================================================

#: ``CDOTA_Unit_Hero_FacelessVoid`` → ``faceless_void`` 这类驼峰转换。
#: 绝大多数英雄符合这个规律；不符合的（如 Furion / NaturesProphet）会因为
#: 匹配不上而被跳过，标定用集合对齐，少几个英雄不影响结果。
_HERO_DT_PREFIX = "CDOTA_Unit_Hero_"


def _camel_to_snake(name: str) -> str:
    """``Ogre_Magi`` → ``ogre_magi``，``StormSpirit`` → ``storm_spirit``。

    只解决「类名 → 小写下划线」这一步的**形式**转换，不保证结果和英雄表一致 ——
    真实对照请用 :func:`canon_npc`（见那里的说明）。

    ⚠️ 这里踩过的坑（2026-11-13）：第一版对每个大写字母前都补 ``_``，
    而 ``Ogre_Magi`` 的 ``M`` 前面本来就有下划线，于是转出 ``ogre__magi``，
    和英雄表里的 ``npc_dota_hero_ogre_magi`` 永远对不上。血量、装备、位置
    三张表都只按 npc 名索引，认不出就等于「这个英雄没参战」—— 数据块里写成
    「无数据」，从字面上完全看不出是解析错了。修好这一版后 ``ogre_magi``
    正确，但 ``VengefulSpirit`` 这类**连写**的仍然会转成 ``vengeful_spirit``，
    而英雄表里是 ``vengefulspirit`` —— 所以只靠这条规则是修不完的。
    """
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i and not name[i - 1].isupper() and name[i - 1] != "_":
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def canon_npc(name: Any) -> str:
    """把任意写法的英雄 npc 名归一成 ``npc_dota_hero_`` + 纯小写字母数字。

    归一化是**必须**的，因为同一场录像里三种写法并存，而且互相都对不上：

    - 录像类名：``CDOTA_Unit_Hero_Ogre_Magi`` / ``CDOTA_Unit_Hero_VengefulSpirit``
    - 战斗日志：``npc_dota_hero_ogre_magi`` / ``npc_dota_hero_vengefulspirit``
    - 英雄常量：``npc_dota_hero_ogre_magi`` / ``npc_dota_hero_vengefulspirit``

    但血量文件里 VS 是 ``CDOTA_Unit_Hero_VengefulSpirit`` 而战斗日志里是
    ``npc_dota_hero_vengefulspirit``，按「驼峰补下划线」的规则会得到
    ``vengeful_spirit``，两边对不上；反过来 ``Ogre_Magi`` 若「去掉下划线」又会
    变成 ``ogremagi``。**两种规则各有各的反例，所以不能靠猜词边界** ——
    直接抹掉所有 ``_`` 与大小写差异，只比字母数字序列：

        ogre_magi      → ogremagi
        OgreMagi       → ogremagi
        Ogre_Magi      → ogremagi
        vengefulspirit → vengefulspirit

    这样任何写法都能对上同一个英雄。代价是「名字本身有歧义」时无法区分，
    但英雄 npc 名两两之间不存在这种情况（127 个英雄实测去重后仍是 127 个）。
    """
    raw = str(name or "").strip()
    for prefix in ("npc_dota_hero_", "CDOTA_Unit_Hero_", _HERO_DT_PREFIX):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    body = "".join(ch for ch in raw.lower() if ch.isalnum())
    return "npc_dota_hero_" + body


def _read_hp(path: str) -> dict[str, list[tuple]]:
    """读血量采样。

    每行：``t, 索引, 实体名, hp, maxHp, mana, maxMana, 等级, 存活, x, y[, 复活时刻]``
    返回 ``{npc名: [(t, hp, maxHp, mana, maxMana, lvl, alive, x, y, respawn), ...]}``。

    第 10 项 ``respawn`` 是引擎给的复活时刻（demo 时间基准），活着时是 -1 或 0。
    **不要拿它算倒计时**：实测它恒定给「阵亡 + 约 52 秒」，而 13 级英雄 34 秒就
    站起来了（Ember 52.5、Lion 51.8，那局零买活），偏大约 18 秒。
    真实复活时刻用 :func:`_death_span` 从血量序列里认。
    老版本的 hp.csv 只有 11 列、没有这一项，这里补 -1 兼容。
    """
    series: dict[str, list[tuple]] = {}
    if not os.path.exists(path):
        return series
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            parts = line.strip().split(",")
            if len(parts) not in (11, 12):
                continue
            try:
                row = (float(parts[0]), int(parts[3]), int(parts[4]), int(parts[5]),
                       int(parts[6]), int(parts[7]), int(parts[8]),
                       float(parts[9]), float(parts[10]),
                       float(parts[11]) if len(parts) > 11 else -1.0)
            except ValueError:
                continue
            npc = canon_npc(parts[2])
            series.setdefault(npc, []).append(row)
    return series


def _read_combat(path: str) -> list[dict]:
    rows: list[dict] = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    rows.sort(key=lambda e: float(e.get("t") or 0))
    return rows


def calibrate(combat: list[dict], hp_series: dict[str, list], match: dict) -> dict:
    """标定 demo→战斗日志、战斗日志→比赛时间两个偏移。

    返回 ``{"demo_to_combatlog": a, "combatlog_to_match": b}``，
    都是「后者减前者」的秒数。标不出来时给 0，调用方据此决定要不要降级。

    ⚠️ **demo→战斗日志必须用「区间求交」，不能取最近邻中位数。**
    血量是每 2.1 秒采一次，真正的死亡时刻落在「最后一次活着」与
    「第一次 0 血」之间，所以每次死亡给出的是一个**区间**而不是一个点。
    实测教训：用最近邻中位数时，某英雄一局里死了几次就会配错几次
    （第 3 次死亡配到了第 2 次的血量归零点），20 个样本里错 6 个，
    结果算出 4.13 秒（真值 19.58），整个数据块的血量、位置、装备
    全部错位 15 秒 —— 表面上却完全正常，看不出来。
    """
    # ---- demo → 战斗日志：按英雄逐个配对，把每次死亡收窄成一个区间再求交 ----
    cl_deaths: dict[str, list[float]] = {}
    for e in combat:
        if e.get("type") != "DOTA_COMBATLOG_DEATH":
            continue
        tgt = str(e.get("tgt") or "")
        if not tgt.startswith("npc_dota_hero_"):
            continue
        cl_deaths.setdefault(tgt, []).append(float(e.get("t") or 0))

    lo, hi = -1e9, 1e9
    accepted = 0
    for npc, rows in hp_series.items():
        deaths = cl_deaths.get(npc)
        if not deaths:
            continue
        seq = sorted(rows, key=lambda r: r[0])
        for when in sorted(deaths):
            for i in range(1, len(seq)):
                if seq[i - 1][6] == 1 and seq[i][6] == 0:
                    before, after = seq[i - 1][0], seq[i][0]
                    # 偏移必须落在 [when-after, when-before] 之间
                    cand_lo, cand_hi = when - after, when - before
                    if cand_hi < lo or cand_lo > hi:
                        continue
                    lo, hi = max(lo, cand_lo), min(hi, cand_hi)
                    accepted += 1
                    break
    a = round((lo + hi) / 2, 2) if accepted >= 2 and lo <= hi else 0.0
    # 区间宽到几十秒说明配对整体错位，宁可判 0 让上层走降级
    if accepted and (hi - lo) > 60:
        a = 0.0

    # ---- 战斗日志 → 比赛时间：建筑倒塌时刻与 objectives 顺序配对，取中位数 ----
    tower_times = sorted(
        float(e.get("t") or 0) for e in combat
        if e.get("type") == "DOTA_COMBATLOG_DEATH"
        and any(k in (e.get("tgt") or "") for k in ("tower", "rax", "fort"))
        and "creep" not in (e.get("tgt") or "")
    )
    obj_times = sorted(float(o.get("time") or 0) for o in (match.get("objectives") or [])
                       if o.get("type") == "building_kill")
    b = 0.0
    if tower_times and obj_times:
        pairs = [t - o for t, o in zip(tower_times, obj_times)]
        pairs.sort()
        b = round(pairs[len(pairs) // 2], 2)
    elif tower_times:
        # ⚠️ 退化分支：没有 objectives 时拿「最后一座塔的 combat 时刻 − 比赛时长」
        # 反推偏移。**这里必须挡住 duration 缺失**：
        # 没有 objectives、duration 又是 0 时，会算出「最后一座塔的绝对时刻」
        # 这种荒谬的偏移（实测某局给出 +2192 秒），而调用方完全看不出来 ——
        # 团战窗口会被换算到录像之外，页面能打开但画的是空场或错误时段。
        # 判 0 至少是「不偏移」，与「标不出来时给 0」的既有约定一致。
        duration = float(match.get("duration") or 0)
        if duration > 0:
            b = round(tower_times[-1] - duration, 2)
        else:
            b = 0.0
    return {"demo_to_combatlog": a, "combatlog_to_match": b}


# ======================================================================
# 数据块
# ======================================================================


def _clock(seconds: Any) -> str:
    try:
        total = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        return "-"
    return "%02d:%02d" % (total // 60, total % 60)


def _short(npc: Any) -> str:
    raw = str(npc or "")
    for prefix in ("npc_dota_hero_", "npc_dota_"):
        if raw.startswith(prefix):
            return raw[len(prefix):]
    return raw


def read_items(path: str) -> dict[str, list[tuple[float, list[str]]]]:
    """装备快照：``{npc名: [(demo时间, [装备...]), ...]}``（按时间升序）。

    这里**必须保留时间**：团战分析要的是「开打前他手里有什么」，
    取全场最后一次会把团战后补的装备算进去，直接误导判断。
    """
    out: dict[str, list[tuple[float, list[str]]]] = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            parts = line.strip().split(",")
            if len(parts) < 4:
                continue
            try:
                when = float(parts[0])
            except ValueError:
                continue
            npc = canon_npc(parts[2])
            out.setdefault(npc, []).append((when, [x for x in parts[3:] if x]))
    for rows in out.values():
        rows.sort(key=lambda r: r[0])
    return out


def gear_at(items: dict[str, list[tuple[float, list[str]]]], npc: str,
            demo_time: float) -> list[str]:
    """取某个 demo 时刻**之前**最后一次的装备。"""
    best: list[str] = []
    for when, gear in items.get(npc, []):
        if when > demo_time:
            break
        best = gear
    return best


def read_wards(path: str, offset: float) -> list[dict]:
    """眼位记录（demo 时间 → 比赛时间）。"""
    wards: list[dict] = []
    if not os.path.exists(path):
        return wards
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            parts = line.strip().split(",")
            if len(parts) != 8:
                continue
            try:
                wards.append({
                    "kind": "假眼" if parts[0].endswith("_Ward") else "真眼",
                    "team": "天辉" if parts[2] == "2" else "夜魇",
                    "born": float(parts[3]) + offset,
                    "last": float(parts[6]) + offset,
                    "x": float(parts[4]),
                    "y": float(parts[5]),
                })
            except ValueError:
                continue
    return wards


def _death_span(seq: list[tuple], idx: int) -> tuple[float, float | None]:
    """``seq[idx]`` 是尸体时，返回这次死亡的 ``(阵亡时刻, 复活时刻)``（demo 时间）。

    为什么不用引擎的 ``m_flRespawnTime``：实测它给的是「阵亡时刻 + 约 52 秒」，
    而 13 级英雄实际 34 秒就复活了，两个样例（Ember 52.5、Lion 51.8）都偏大约 18 秒，
    且那局没有任何买活。拿它算倒计时会系统性地把「队友马上就到」说成「还要等一会儿」，
    正好把该掐的时间点说反。血量序列是观测出来的，不存在这个问题。

    ``复活时刻`` 为 ``None`` 表示到数据结尾都还没站起来。
    """
    head = idx
    while head > 0 and seq[head - 1][6] == 0:
        head -= 1
    dead = seq[head][0]
    tail = idx
    while tail < len(seq) - 1 and seq[tail][6] == 0:
        tail += 1
    revive = seq[tail][0] if seq[tail][6] == 1 else None
    return dead, revive


# ======================================================================
# A 杖祝福：谁在什么时候有了它
# ======================================================================

_AGHS_ITEM = "item_ultimate_scepter"
_AGHS_MOD = "modifier_item_ultimate_scepter"

#: A 杖祝福的两种来源 —— **任何英雄都能买，不是炼金专属**（用户 2026-11-13 指出）。
#:
#: 录像里能拿到的证据有三条，而且**互不重叠**（实测比赛 9086986335）：
#:
#: 1. 装备格里的 ``ultimatescepter``：手里拿着还没吃，``items.csv`` 能看到。
#: 2. ``MODIFIER_ADD modifier_item_ultimate_scepter`` 且 ``atk == tgt``：
#:    自己吃掉了祝福。那场沉默 9:58、撼地者 10:20 各一条，之后一直没有 REMOVE。
#: 3. ``ITEM item_ultimate_scepter`` 从 A → B：熔铸后赠送队友。
#:    那场送了四把（复仇之魂 8:57、风暴之灵 11:48、噬魂鬼 14:49、拉尔戈 16:21）。
#:
#: ⚠️ **第 3 条不会在受赠者身上留下任何 scepter 记录。** 四个受赠人全场的
#: scepter 相关事件**各只有那一条 ITEM** —— 既没有 MODIFIER_ADD，装备格里也没有。
#: 熔铸是直接写进「已获得神杖升级」标记，不走 modifier。
#: 所以只扫 modifier 会漏掉这四个受赠人，而他们恰恰是团战主角；
#: 只认炼金又会漏掉所有自己买的。**两条来源必须都扫。**
#:
#: ⚠️ 另一个坑：炼金自己也走第 2 条。他每次都是「买到手 → 几秒后熔铸送人」
#: （实测间隔 1.4–4.4 秒）。不处理的话会写成「炼金 14:46 获得神杖」，
#: 而模型会据此以为他有 A 杖效果 —— 他只是在过手，马上就不是他的了。
#: 判据：该段 ``REMOVE`` 前后 :data:`_AGHS_TRANSIT_WINDOW` 内，同一人有一条
#: ``ITEM`` 送给别人 → 判为中转，跳过。
_AGHS_TRANSIT_WINDOW = 1.5


def read_aghs(combat: list[dict]) -> dict[str, list[tuple[float, str, str]]]:
    """谁在什么时候拿到 A 杖祝福。

    返回 ``{npc名: [(战斗日志时间, 来源, 说明), ...]}``，按时间升序。
    来源是 ``"self"``（自己吃祝福，说明为空）或 ``"gift"``（熔铸赠送，
    说明是赠送者的 npc 名）。

    时间基准是**战斗日志时间**，调用方自己换算成比赛时间。
    """
    gifts: list[tuple[float, str, str]] = []
    adds: dict[str, list[float]] = {}
    removes: dict[str, list[float]] = {}

    for e in combat:
        typ = str(e.get("type") or "")
        if str(e.get("inf") or "") not in (_AGHS_ITEM, _AGHS_MOD):
            continue
        when = float(e.get("t") or 0)
        if typ == "DOTA_COMBATLOG_ITEM":
            giver, taker = canon_npc(e.get("atk")), canon_npc(e.get("tgt"))
            if giver.startswith("npc_dota_hero_") and taker.startswith("npc_dota_hero_"):
                gifts.append((when, giver, taker))
        elif typ == "DOTA_COMBATLOG_MODIFIER_ADD":
            npc = canon_npc(e.get("tgt"))
            # 只认「自己给自己」：别人给的走 ITEM 那条，不会产生 atk==tgt 的 ADD
            if npc.startswith("npc_dota_hero_") and npc == canon_npc(e.get("atk")):
                adds.setdefault(npc, []).append(when)
        elif typ == "DOTA_COMBATLOG_MODIFIER_REMOVE":
            npc = canon_npc(e.get("tgt"))
            if npc.startswith("npc_dota_hero_"):
                removes.setdefault(npc, []).append(when)

    out: dict[str, list[tuple[float, str, str]]] = {}

    for npc, stamps in adds.items():
        rem = sorted(removes.get(npc) or [])
        for add_t in sorted(stamps):
            # 找这一段是何时结束的（熔铸会 REMOVE 掉自己那份）
            end = next((x for x in rem if x >= add_t), None)
            transit = end is not None and any(
                giver == npc and abs(gt - end) <= _AGHS_TRANSIT_WINDOW
                for gt, giver, _ in gifts
            )
            if transit:
                continue
            out.setdefault(npc, []).append((add_t, "self", ""))

    for gt, giver, taker in gifts:
        out.setdefault(taker, []).append((gt, "gift", giver))

    for rows in out.values():
        rows.sort(key=lambda r: r[0])
    return out


def aghs_at(aghs: dict[str, list[tuple[float, str, str]]], npc: str,
            when: float) -> tuple[float, str, str] | None:
    """``when``（战斗日志时间）这一刻，某人是否已有 A 杖祝福。

    返回 ``(获得时刻, 来源, 说明)``，没有则 ``None``。祝福是永久的（吃了不会掉），
    所以取「最后一次不晚于 ``when`` 的获得记录」。
    """
    hit: tuple[float, str, str] | None = None
    for t, kind, note in aghs.get(npc, []):
        if t > when:
            break
        hit = (t, kind, note)
    return hit


#: 替身类 modifier：``modifier_illusion`` 是「场上出现一个该英雄的幻象」。
#:
#: ⚠️ **不能一看到它就说「本体死了、幻象接管」。** 同一场录像里它有两种来源，
#: 时长完全不同（实测 9086986335）：
#:
#: · ``dur=18`` 左右、add 时本体**活着** → 分身斧（Manta Style）开出来的，
#:   是主动道具效果，不是死亡替身。复仇之魂、噬魂鬼各有一堆。
#: · ``dur=49``、add 紧贴本体阵亡 → **A 杖幻象**。复仇之魂 15:01.24 阵亡时
#:   生成，15:50.04 消失，正好覆盖她整段死亡窗口。
#:
#: 所以判据必须是「幻象活跃期间本体是不是尸体」，不能只看有没有这条 modifier。
_ILLUSION_MOD = "modifier_illusion"


def read_illusions(combat: list[dict]) -> dict[str, list[tuple[float, float | None]]]:
    """幻象的存活区间（战斗日志时间）：``{npc: [(生成, 消失), ...]}``。

    消失为 ``None`` 表示到录像结尾都还在。

    ⚠️ **配对必须按 ``mdur`` 对，不能按时间顺序就近配。** 实测复仇之魂那一段：

        14:55.04 ADD    dur=18   ← 分身斧
        14:59.44 REMOVE dur=18
        15:01.24 ADD    dur=49   ← A 杖幻象（本体刚死）
        15:01.57 REMOVE dur=18   ← 上一条的残留记录
        15:50.04 REMOVE dur=49

    按时间就近配，A 杖幻象会被配成「15:01.57 就消失」——
    于是最有价值的那 49 秒替身直接消失，等于这个功能白做。
    按 ``mdur`` 配对才能得到 ``(15:01.24, 15:50.04)``。
    """
    adds: list[tuple[float, str, float]] = []
    removes: list[tuple[float, str, float]] = []
    for e in combat:
        if str(e.get("inf") or "") != _ILLUSION_MOD:
            continue
        typ = str(e.get("type") or "")
        npc = canon_npc(e.get("tgt"))
        if not npc.startswith("npc_dota_hero_"):
            continue
        try:
            dur = round(float(e.get("mdur") or -1), 2)
        except (TypeError, ValueError):
            dur = -1.0
        if typ == "DOTA_COMBATLOG_MODIFIER_ADD":
            adds.append((float(e.get("t") or 0), npc, dur))
        elif typ == "DOTA_COMBATLOG_MODIFIER_REMOVE":
            removes.append((float(e.get("t") or 0), npc, dur))

    out: dict[str, list[tuple[float, float | None]]] = {}
    used: set[int] = set()
    for when, npc, dur in sorted(set(adds)):
        end: float | None = None
        for i, (rt, rnpc, rdur) in enumerate(sorted(removes)):
            if i in used or rnpc != npc or rt < when:
                continue
            if dur > 0 and rdur > 0 and abs(rdur - dur) > 0.01:
                continue
            end = rt
            used.add(i)
            break
        out.setdefault(npc, []).append((when, end))
    return out


# ======================================================================
# 事件流水：渲染 + 分层保留
# ======================================================================

#: 事件流水最多输出多少行。超了按时间分桶抽稀，**不是**从前往后截断。
#:
#: 2026-11-13 从 600 提到 1500（用户原话：「条数也可以放宽，放到 1000 甚至
#: 1500 都 ok，600 条可能太紧了」）。
#:
#: 提高的理由，是 600 这个数字在**噪音滤掉之后**反而变成了新的瓶颈：
#: 那一波团滤完还有 1057 条候选，600 的帽子意味着硬丢一半 —— 而丢的是
#: 「抽稀判定认为可省」的，恰好包括清莲宝珠那次罩子的结束行，
#: 于是复查时看到「用了清莲宝珠」却看不到罩子什么时候断的。
#: 放宽到 1500 后，绝大多数团战一波就能全量保留，抽稀只在极端长团时触发。
#:
#: token 代价实测：本局第 5 波 1057 条全留，整块约 4.1 万字（原 3.0 万字），
#: 折合约 +35% 输入 token。这个量级是可接受的 —— 团战复盘本来就是最贵的
#: 那条路径，而它贵在**模型为了补猜而多写的废话**，不比多给数据便宜。
EVENT_LIMIT = 1500

#: 聚合段里「不进汇总、按原样单条列出」的阈值（伤害/治疗量）。
#:
#: 一次 400 的伤害是关键打击，一次 400 的治疗是救人，都不是噪音 ——
#: 它们出现在聚合行里会被「累计 N」抹平，看不出那一击/那一奶有多重。
_AGG_KEEP_SINGLE = 300

#: 治疗**他人**时「不进聚合、单条列出」的阈值。
#:
#: 比伤害阈值低一半：治疗自己是续航（可以并进聚合），治疗别人是救人 ——
#: 一次 180 的奶救不救得活人，往往比一次 300 的伤害更能决定这波团的结论。
_AGG_KEEP_HEAL_OTHER = 150

#: 保留优先级（数字越小越必留）。
#:
#: 判据是「这条信息能不能改变对一个决策的评价」。
#:
#: **必留档只到 ``_EV_HIGH``（含），共 0–1 两档。** 这个边界是实测定的：
#: 早先划到 ``_EV_MID``（含伤害）时，本局第 5 波团的必留项有 974 条、
#: 超过当时的限额，于是必留项自己也被抽稀 —— 连清莲宝珠的 buff 结束行
#: 都被丢了，读者看到「用了清莲宝珠」却看不到罩子什么时候断的。
#: 即便限额已放宽到 1500，这条边界仍然要守住：必留项一旦逼近限额，
#: 抽稀就会退化成「必留项互相挤」，而挤掉谁是无序的、不可预期的。
#: 缩到 0–1 后必留 534 条，留出了给时间桶的余量。
#:
#: · 0 ``_EV_KEEP``：无法从别处还原的唯一证据 —— 阵亡、控制（眩晕/沉默/
#:   位移/减速）、技能与道具的施放、主动道具的 buff 起止。
#: · 1 ``_EV_HIGH``：重要性高但可替代 —— 英雄对英雄的大额伤害、
#:   有时长的控制结束、对野怪施放的技能。
#: · 2 ``_EV_MID``：有信息量、抽稀时优先，但不保证留。
#: · 3–4 ``_EV_LOW`` / ``_EV_NOISE``：均值稀释类，能丢就丢。
_EV_KEEP, _EV_HIGH, _EV_MID, _EV_LOW, _EV_NOISE = 0, 1, 2, 3, 4

#: 野怪/小兵一侧的事件，伤害低于这个值就当噪音丢掉。
#:
#: 实测本局第 5 波团：英雄对野怪 929 条、对线小兵 380 条，合计 1309 条
#: （总候选 3475 条的 38%）。炼金术士一个人在北边野区刷了 20 分钟，
#: 光 radiance 的持续伤害就占了上百行 —— 而这恰好是判断「他到底有没有
#: 参战」的关键人物，噪音把他真正有价值的那几行淹掉了。
#: 阈值取 150：低于它的基本是光环/法球跳字，高于它的通常是真实爆发。
_MOB_DAMAGE_FLOOR = 150


def _strip_prefix(text: Any, *prefixes: str) -> str:
    raw = str(text or "")
    for prefix in prefixes:
        if raw.startswith(prefix):
            return raw[len(prefix):]
    return raw


def _render_event(
    e: dict,
    c2m: float,
    short: Callable[[Any], str],
    *,
    item_label: Callable[[str], str] | None = None,
    ability_label: Callable[[str], str] | None = None,
    item_resolve: Callable[[str], str | None] | None = None,
) -> str | None:
    """把一条战斗日志渲染成一行文本；返回 ``None`` 表示这条不输出。

    **返回 None 就是不占名额。** 老版本的 bug 正是「没输出也占名额」，
    所以这里的约定必须严格：调用方只对非 None 的结果计数。
    """
    typ = _strip_prefix(e.get("type"), "DOTA_COMBATLOG_")
    atk, tgt = str(e.get("atk") or ""), str(e.get("tgt") or "")
    atk_hero = atk.startswith("npc_dota_hero_")
    tgt_hero = tgt.startswith("npc_dota_hero_")

    tm = _clock(float(e.get("t") or 0) - c2m)
    a = short(atk)
    g = short(tgt)
    info_raw = str(e.get("inf") or "")
    # 技能/道具内部键：去掉 modifier_ / ability_ 前缀，保留 item_ 前缀判断
    info = _strip_prefix(info_raw, "modifier_", "ability_")

    item_fn = item_label or (lambda x: str(x).replace("_", " "))
    abil_fn = ability_label or (lambda x: str(x).replace("_", " "))

    # ---- 阵亡：只有英雄死亡才值得写 ----
    if typ == "DEATH":
        if not tgt_hero and not atk_hero:
            return None
        if tgt_hero:
            killer = a or "?"
            if atk_hero and info_raw:
                # 击杀手段：技能名优先，其次道具名。老版本这里写的是
                # `%s（）` —— 括号里塞的是去掉 item_ 的 raw 键，还经常是空的，
                # 于是输出「阵亡，击杀者 silencer（）」这种残缺行。
                src = (
                    _resolve_item_label(info_raw, item_fn, item_resolve)
                    if info_raw.startswith(("item_", "modifier_item_"))
                    else abil_fn(info_raw)
                )
                if src:
                    killer = "%s（%s）" % (a, src)
            return "%s %s 阵亡，击杀者 %s" % (tm, g, killer)
        # 英雄杀了非英雄单位：只有建筑/肉山/野怪首领值得记，小兵不要
        return None

    # ---- 技能与道具施放：这是「他做了什么」的第一手证据 ----
    if typ == "ABILITY":
        # 信使/小兵/野怪也会「施放技能」（courier take stash、creep 的被动），
        # 写进团的日志里只会让人以为有人放了个不认识的技能。
        if not atk_hero:
            return None
        skill = abil_fn(info)
        tail = ""
        if tgt_hero:
            tail = " -> " + g
        elif tgt and not tgt.startswith("npc_dota_creep"):
            tail = " -> " + g
        return "%s %s 施放 %s%s%s" % (
            tm, a, skill, "（大招）" if e.get("ult") else "", tail)

    if typ == "ITEM":
        if not atk_hero:
            return None
        # 这里的 info 形如 `item_blink` / `item_lotus_orb`
        key = _strip_prefix(info, "item_")
        # 归一化后交给 item_fn：录像里的旧键（`emptybottle`）要变成现行键，
        # 否则调用方的查表逻辑还得自己维护一套别名（已经维护过一份，会走岔）。
        label = item_fn(_normalize_item_key(key)) or key.replace("_", " ")
        tail = " -> " + g if tgt_hero else ""
        return "%s %s 使用 %s%s" % (tm, a, label, tail)

    if typ == "DAMAGE":
        val = int(e.get("val") or 0)
        # 野怪/小兵一侧的小额伤害是纯噪音
        if not (atk_hero and tgt_hero):
            if val < _MOB_DAMAGE_FLOOR:
                return None
        if val <= 0:
            return None
        # 技能/道具打出来的伤害，把来源写清楚（模型据此还原连招顺序）
        src = ""
        if info_raw.startswith("item_"):
            src = _resolve_item_label(info_raw, item_fn, item_resolve)
        elif info_raw and not info_raw.startswith("modifier_"):
            src = abil_fn(info_raw)
        suffix = " [%s]" % src if src else ""
        return "%s %s -> %s 伤害%d%s" % (tm, a, g, val, suffix)

    if typ == "HEAL":
        val = int(e.get("val") or 0)
        # 1~4 点的治疗基本都是光环/回复符的持续跳动（实测一次团战里
        # 能刷出上百行「治疗 1」），留着只会把真正的关键事件淹掉。
        if val < 5:
            return None
        return "%s %s 治疗 %s %d" % (tm, a, g, val)

    # ---- modifier：控制、沉默、减速、隐身，以及**主动道具的持续效果** ----
    if typ == "MODIFIER_ADD":
        return _render_modifier_add(e, tm, a, g, info, info_raw, item_fn, item_resolve)
    if typ == "MODIFIER_REMOVE":
        return _render_modifier_remove(e, tm, g, info, info_raw, item_fn, item_resolve)

    # 其余类型（MODIFIER_STACK_EVENT / ABILITY_TRIGGER / CRITICAL_DAMAGE ...）
    # 一律不输出。**老代码就是在这里吃了暗亏：这些类型没有分支，却还被计数。**
    return None


#: 道具 modifier 名的常见后缀，按「越长越先试」排列。
#:
#: 战斗日志里道具效果的命名是 `<道具>_<作用>`，而道具表的键只有 `<道具>`：
#:
#:     modifier_item_lotus_orb_active        → lotus_orb
#:     modifier_item_shivas_guard_blast      → shivas_guard
#:     modifier_item_mjollnir_static         → mjollnir
#:     modifier_item_radiance_debuff         → radiance
#:     modifier_item_shivas_guard_thinker    → shivas_guard
#:
#: 不去做模糊匹配（difflib 之类）：道具名互相很像（``blade_mail`` /
#: ``blades_of_attack``），模糊匹配会把「板甲」认成「攻击之爪」，
#: 而且错得静悄悄。这里只做**确定性剥离**，剥完还不认识就退回可读原名。
_ITEM_MODIFIER_SUFFIXES: tuple[str, ...] = (
    "_active", "_thinker", "_blast", "_static", "_debuff", "_buff",
    "_slow", "_effect", "_aura", "_poison", "_stun", "_silence",
)


def _item_key_variants(raw: str) -> list[str]:
    """由 ``modifier_item_lotus_orb_active`` 生成候选道具键，按可信度排序。

    第一项永远是**原样（已去掉 modifier_ 与 item_ 前缀）**，保证已经能直接
    命中的情况不受影响。注意两个前缀都要剥：只剥 ``modifier_`` 会剩下
    ``item_lotus_orb``，而这个键在道具表里同样查不到。
    """
    key = _strip_prefix(str(raw or ""), "modifier_")
    key = _strip_prefix(key, "item_")
    out = [key]
    for _ in range(3):  # 最多剥三层，`x_debuff_thinker` 这类也能覆盖
        changed = False
        for suf in _ITEM_MODIFIER_SUFFIXES:
            if out[-1].endswith(suf) and len(out[-1]) > len(suf):
                cand = out[-1][: -len(suf)]
                if cand not in out:
                    out.append(cand)
                    changed = True
        if not changed:
            break
    return out


def _resolve_item_label(
    raw: str,
    item_fn: Callable[[str], str],
    resolve: Callable[[str], str | None] | None = None,
) -> str:
    """把道具内部键换成显示名；优先精确解析，退化为可读原名。

    ``resolve`` 是调用方给的「键 → 中文名」精确查询器（``None`` 表示查不到）。
    解析顺序：原始键 → 剥后缀后的键 → 别名表。全程确定性，不猜。

    两条都是踩过的坑（2026-11-13，测试抓到的）：

    1. **判断「查到了没有」不能靠「返回值是否等于把下划线换成空格」。**
       那会把 ``shivas_guard`` → ``"shivas guard"``（一个合法的人可读名）
       误判成没查到，于是落进「旧版道具名」兜底。

    2. **有精确解析器时，不许对同一个候选再问 ``item_fn``。**
       ``modifier_item_shivas_guard_blast`` 的第一个候选是
       ``shivas_guard_blast``，它在道具表里**不存在**；若此时退回
       ``item_fn`` 拿一个人可读的 ``"shivas guard blast"``，看起来像查到了，
       于是不会再去试第二个候选 ``shivas_guard`` —— 结果就是永远认不出
       希瓦的守护。所以 ``resolve`` 给了就只信它，``None`` 就往下换候选。
    """
    variants = _item_key_variants(raw)
    if resolve is not None:
        for cand in variants:
            got = resolve(_normalize_item_key(cand))
            if got:
                return got
    else:
        # 没有精确解析器（测试/离线场景）：只能退回 item_fn，
        # 且**只用剥离得最彻底的那个候选**，避免上面第 2 类误判。
        # 注意这里传的是**原始下划线写法**，不是归一化后的：item_fn 的契约
        # 是「吃道具键、吐显示名」，而归一化键（`shivasguard`）是查表用的，
        # 两个不同用途的字符串不能混着传（混了测试替身就查不到）。
        last = variants[-1]
        got = item_fn(last)
        if got and got != last:
            return got
    # 实在认不出来：给出人能读的旧名，并标注这是旧版道具名，
    # 让读者知道「不是插件瞎写，是这局录像里的老名字」。
    tail = variants[-1].replace("_", " ")
    return "%s（旧版道具名）" % tail


#: 录像里仍在用的**旧版道具键** → 现行键。
#:
#: 录像的战斗日志是按当年那版的内部名写的，而道具常量表跟的是现行版本。
#: Valve 改过名的道具就会两边对不上（2026-11-13 实测本局 87 种装备里
#: 有 10 种是这种情况）。这里只列**确定改过名**的几条，绝不做模糊匹配：
#:
#: 装备名互相之间很像（``blade_mail`` / ``blades_of_attack``、
#: ``eaglehorn`` / ``eaglesong``），模糊匹配很容易把「板甲」认成「攻击之爪」，
#: 而且错得静悄悄 —— 读报告的人不会发现，只会觉得这份复盘在胡说。
#: 认不出来的就保留原名并加「(旧名)」，让读者自己判断，比编一个强。
_LEGACY_ITEM_KEYS: dict[str, str] = {
    "emptybottle": "bottle",
    "ironwoodbranch": "branches",
    "craniumbasher": "basher",
    "eaglehorn": "eaglesong",
    "bootsofelven": "boots_of_elves",
    "robeofmagi": "robe",
    "greatfamango": "great_famango",
    "greaterfamango": "greater_famango",
    "ultimatescepter": "ultimate_scepter",
    "sobi_mask": "sobi_mask",
}


def _normalize_item_key(raw: Any) -> str:
    """录像里的道具键 → 常量表的键。

    **只抹下划线与大小写，不做任何模糊匹配**（理由见 :data:`_LEGACY_ITEM_KEYS`）。
    已确认改过名的走别名表。
    """
    text = str(raw or "").strip().lower().replace("-", "")
    plain = "".join(ch for ch in text if ch.isalnum())
    if plain in _LEGACY_ITEM_KEYS:
        return _LEGACY_ITEM_KEYS[plain]
    return plain


def _modifier_effect_text(e: dict) -> str:
    """把 modifier 身上带的「实际效果」翻译成人话。

    这些字段是战斗日志直接给的（实测本局第 5 波团：stun 44 条、motion 20 条、
    sil 12 条、slow 82 条），比从名字猜要可靠得多：

    - ``stun`` / ``motion``：眩晕/位移控制的**实际持续秒数**
    - ``sil``：沉默
    - ``slow``：减速幅度（秒数在 ``mdur``）
    - ``invis``：隐身
    """
    parts: list[str] = []
    if e.get("stun"):
        parts.append("眩晕%.1fs" % float(e["stun"]))
    if e.get("motion"):
        parts.append("位移控制")
    if e.get("sil"):
        parts.append("沉默")
    if e.get("slow"):
        try:
            pct = float(e["slow"]) * 100
            parts.append("减速%.0f%%" % pct if pct < 100 else "减速")
        except (TypeError, ValueError):
            parts.append("减速")
    if e.get("invis"):
        parts.append("隐身")
    return "、".join(parts)


def _render_modifier_add(
    e: dict,
    tm: str,
    a: str,
    g: str,
    info: str,
    info_raw: str,
    item_fn: Callable[[str], str],
    item_resolve: Callable[[str], str | None] | None = None,
) -> str | None:
    """``MODIFIER_ADD`` 的一行文本。

    **主动道具的效果主要活在这里**（用户 2026-11-13 指出「主动道具的效果也没有
    识别到」）—— 这是对的：``ITEM`` 事件只有「谁在什么时候用了什么」，
    一点效果信息都没有；真正的效果落在这三类里：

    1. 道具自己带的 debuff（``modifier_item_shivas_guard_blast`` 带 slow=4）
    2. 道具触发的 buff（``modifier_item_lotus_orb_active``，mdur=6.55 秒）
    3. 由技能触发、但挂在道具名下的效果
       （``src=modifier_ogre_magi_ignite_multicast`` 说明这个减速是被食人魔的
       多重施法带出来的，不是拉尔戈自己主动放的）

    所以这里既输出效果（眩晕/减速/沉默），也输出**持续秒数与触发来源**，
    模型才能判断「这个罩子到底罩住了几秒、是被谁打出来的」。

    判据是「有没有可解读的效果」，不是「是不是道具」：
    - 带 stun/sil/motion/slow 或 invis → 真效果，写；
    - 只有 mdur，且**持续很短（< 2 秒）**，又没有任何控制效果 → 这是光环
      每 0.5 秒刷新一次的痕迹（``ring_of_basilius_effect`` 一场团 20+ 条），
      写出来只是刷屏，丢；
    - 只有 mdur 且持续较长（``lotus_orb_active`` 6.55s、``mjollnir_static``
      12s）→ 这是真 buff，写。
    """
    effect = _modifier_effect_text(e)
    mdur = 0.0
    try:
        mdur = float(e.get("mdur") or 0)
    except (TypeError, ValueError):
        mdur = 0.0

    dur = ""
    if 0 < mdur < 60:
        # 已经写在效果里的秒数（「眩晕1.3s」）就不要再说一遍「持续1.3s」，
        # 老版本会输出「眩晕1.3s，持续1.3s」这种同一件事说两遍的行。
        stun_s = float(e.get("stun") or 0)
        if not (stun_s and abs(stun_s - mdur) < 0.15):
            dur = "持续%.1fs" % mdur

    # ⚠️ `src` 字段**不要输出**（2026-11-13 引入后当场撤掉）。
    #
    # 它看起来像「谁触发了这条 modifier」，实测不是，而且会直接误导模型：
    #
    #     15:06 largo_catchy_lick_knockback 作用于 Lifestealer
    #           src = modifier_ogre_magi_ignite_multicast
    #
    # 拉尔戈自己的击退技能，`src` 却指向食人魔的引燃多重施法 —— 因果明显是反的。
    # 同一波团里 `src` 的取值横跨技能名（`juggernaut_omni_slash`）、
    # 触发它的 modifier 名（`modifier_ogre_magi_ignite_multicast`）、
    # npc 名（`npc_dota_hero_earthshaker`）、`dota_fountain`、`dota_unknown`，
    # 五种语义混在一个字段里，没有任何可靠口径。
    #
    # 当时按「由 X 引发」写进提示词，模型立刻拿它做归因，写出了
    # 「这一下击退是被食人魔多重施法带出来的」这种结论 —— 而按技能归属，
    # 拉尔戈的击退只可能由他自己施放。**宁可不说，也不能给错因果。**
    # 需要还原连招顺序时，靠 `ABILITY` / `ITEM` 事件的时间顺序就够，
    # 那才是观测事实。

    is_item = info_raw.startswith("modifier_item_")
    label = (
        _resolve_item_label(info_raw, item_fn, item_resolve)
        if is_item
        else info.replace("_", " ")
    )

    # 光环刷新迹：没有任何控制/隐身效果，且持续极短。
    if not effect and mdur and mdur < 2.0:
        return None
    # 连 mdur 都没有、也没有效果字段：属性类，无解读价值。
    if not effect and not dur:
        return None

    # 自己作用于自己、且没有效果字段的「技能状态标记」：纯粹是记账。
    # 例如「Juggernaut 的 无敌斩 作用于 Juggernaut（持续3.5s）」——
    # 无敌斩的核心信息是它打了谁（DAMAGE 事件里有），不是「他自己身上挂了
    # 一个 3.5 秒的标记」。写出来只会让读者以为这是个对自身的增益。
    #
    # **但道具不能一起挡。**「Largo 使用 清莲宝珠 → Largo 的 清莲宝珠
    # 作用于 Largo」正是「我自己开了个罩子」—— 这是主动道具的核心事实，
    # 挡掉就会出现「用了清莲宝珠之后什么都没发生」。
    if a == g and not effect and not is_item:
        return None

    bits = [x for x in (effect, dur) if x]
    detail = "（%s）" % "，".join(bits) if bits else ""
    return "%s %s 的 %s 作用于 %s%s" % (tm, a, label, g, detail)


def _render_modifier_remove(
    e: dict,
    tm: str,
    g: str,
    info: str,
    info_raw: str,
    item_fn: Callable[[str], str],
    item_resolve: Callable[[str], str | None] | None = None,
) -> str | None:
    """``MODIFIER_REMOVE`` 的一行文本：只留「有实际时长」的，丢掉瞬间掉的 buff。

    ``mel`` 是**实际持续秒数**。控制类（stun/silence/slow）结束时它特别有用 ——
    「沟壑晕住 3.4 秒」比「沟壑命中」更能说明问题。而 ``mel < 0.8`` 的基本是
    同帧生灭的残留（光环、被动触发后立即被覆盖），写出来只是噪声。

    另有一类必须挡掉：**作用在非英雄实体上的技能残留**。录像里技能会生成
    一个 ``thinker`` 辅助实体承载地面效果，它的 modifier 到期时也会产生
    一条 REMOVE，于是数据块里出现「thinker 身上的 frogstomp thinker 结束」
    这种没有主语的句子 —— 它说的是「技能范围效果消失」，不是「谁身上的
    状态消失」，对复盘毫无用处，还会让读者以为漏了一个人。
    """
    try:
        mel = float(e.get("mel") or 0)
    except (TypeError, ValueError):
        return None
    if mel < 0.8:
        return None
    # 作用对象不是英雄：thinker / 信使 / 建筑 / 野怪，一律不写
    if not str(e.get("tgt") or "").startswith("npc_dota_hero_"):
        return None
    is_item = info_raw.startswith("modifier_item_")
    if is_item and "aura" in info:
        return None
    label = (
        _resolve_item_label(info_raw, item_fn, item_resolve)
        if is_item
        else info.replace("_", " ")
    )
    return "%s %s 身上的 %s 结束（实际%.1fs）" % (tm, g, label, mel)


def _event_source_label(
    e: dict,
    item_label: Callable[[str], str] | None,
    ability_label: Callable[[str], str] | None,
    item_resolve: Callable[[str], str | None] | None,
) -> str:
    """一条伤害/治疗事件的来源名（技能或道具），取不到返回空串。"""
    info_raw = str(e.get("inf") or "")
    info = _strip_prefix(info_raw, "modifier_", "ability_")
    if info_raw.startswith("item_"):
        fn = item_label or (lambda x: str(x).replace("_", " "))
        return _resolve_item_label(info_raw, fn, item_resolve) or ""
    if info_raw and not info_raw.startswith("modifier_"):
        fn = ability_label or (lambda x: str(x).replace("_", " "))
        return fn(info) or ""
    return ""


def _aggregate_noise_events(
    combat: list[dict],
    cl0: float,
    cl1: float,
    c2m: float,
    *,
    name_of: Callable[[Any], str],
    item_label: Callable[[str], str] | None = None,
    ability_label: Callable[[str], str] | None = None,
    item_resolve: Callable[[str], str | None] | None = None,
    gap: float = 8.0,
    keep_single: int = 300,
) -> list[str]:
    """把「小额高频」的伤害与治疗压成按时间段的汇总行。

    为什么不直接丢：一段「08:31–08:39 莱恩 → 主宰 24 次、累计 380 伤害」
    本身就是画面的一部分 —— 它说明这几秒谁在持续挨打、谁在持续输出。
    一律删掉，模型看到的就是「这 8 秒什么都没发生」，反而更容易编。

    切段口径：同一对「攻击者 → 目标」的同类事件按时间排序，相邻两条间隔
    超过 ``gap`` 秒就断开成新的一段。按固定时间桶切会把一次连续交火切成
    两半，而按间隔切出来的每一段正好是一次「持续输出窗口」。

    ``keep_single`` 以上的单次伤害/治疗不进聚合，按原样单条给 —— 一次 400
    的伤害是「关键打击」，不是噪音。治疗同理：大额救治必须逐条可见。

    控制、沉默、位移、减速、救人道具（清莲宝珠/梅肯/卫士胫甲）都不在这里，
    它们在 ``_event_priority`` 里已经是必留档，由 ``_triage_events`` 原样输出。
    """
    pairs: dict[tuple[str, str, str], list[dict]] = {}
    for e in combat:
        t = float(e.get("t") or 0)
        if not (cl0 - EVENT_PAD <= t <= cl1 + EVENT_PAD):
            continue
        typ = _strip_prefix(e.get("type"), "DOTA_COMBATLOG_")
        if typ not in ("DAMAGE", "HEAL"):
            continue
        atk, tgt = str(e.get("atk") or ""), str(e.get("tgt") or "")
        atk_hero = atk.startswith("npc_dota_hero_")
        tgt_hero = tgt.startswith("npc_dota_hero_")
        val = int(e.get("val") or 0)
        # 与 _render_event 保持同一套出局条件：否则会把它本来不写的行聚合出来，
        # 「安静的一段时间」凭空多出一段打架记录。
        if typ == "DAMAGE":
            if val <= 0:
                continue
            if not (atk_hero and tgt_hero):
                # 打野、清兵的伤害不进聚合段。它们不是团战画面的一部分，
                # 而「这个人这波在刷钱没来」由位置段和「未参战」说明给出，
                # 不需要靠一堆 `→ 野怪` 行去暗示。
                continue
        else:
            if val < 5:
                continue
            if not (atk_hero or tgt_hero):
                continue
        pairs.setdefault((atk, tgt, typ), []).append(e)

    rows: list[tuple[float, str]] = []
    for (atk, tgt, typ), events in pairs.items():
        events.sort(key=lambda x: float(x.get("t") or 0))
        cur: dict | None = None

        def flush() -> None:
            if not cur:
                return
            if cur["n"] < 2 and cur["total"] < 100:
                # 零星一两下、总量也可忽略：写出来比不写更吵。
                return
            if cur["n"] < 2:
                # 只有一条的不算「高频」，按单条原样给，省得模型看到
                # 「1 次，累计 42」这种比原文还啰嗦的汇总。
                rows.append((cur["t0"], cur["single"]))
                return
            span = "%s–%s" % (_clock(cur["t0"]), _clock(cur["t1"]))
            src_note = ""
            if cur["srcs"]:
                top = sorted(cur["srcs"].items(), key=lambda kv: -kv[1])[:3]
                src_note = "（%s）" % "、".join("%s %d" % (k, v) for k, v in top)
            if typ == "HEAL":
                text = "%s %s 治疗 %s：%d 次，累计 %d%s" % (
                    span, cur["a"], cur["g"], cur["n"], cur["total"], src_note)
            else:
                text = "%s %s → %s：%d 次，累计 %d 伤害%s" % (
                    span, cur["a"], cur["g"], cur["n"], cur["total"], src_note)
            rows.append((cur["t0"], text))

        for e in events:
            tm = float(e.get("t") or 0) - c2m
            val = int(e.get("val") or 0)
            single = _render_event(
                e, c2m, name_of, item_label=item_label,
                ability_label=ability_label, item_resolve=item_resolve,
            )
            if val >= (_AGG_KEEP_HEAL_OTHER if typ == "HEAL" and atk != tgt else keep_single):
                # 关键打击 / 救人：单独一条，不进聚合，也不打乱当前段。
                # 治疗他人的阈值比伤害低得多 —— 一次 180 的奶救不救得活人，
                # 比一次 300 的伤害更能决定这波团的结论，不能混进「累计 N」里。
                flush()
                cur = None
                if single:
                    rows.append((tm, single))
                continue
            if cur is None or tm - cur["t1"] > gap:
                flush()
                cur = {
                    "a": name_of(atk), "g": name_of(tgt), "n": 0, "total": 0,
                    "t0": tm, "t1": tm, "srcs": {}, "single": single or "",
                }
            cur["n"] += 1
            cur["total"] += val
            cur["t1"] = tm
            src = _event_source_label(e, item_label, ability_label, item_resolve)
            if src:
                cur["srcs"][src] = cur["srcs"].get(src, 0) + 1
        flush()

    rows.sort(key=lambda r: r[0])
    return [text for _, text in rows]


def _triage_events(
    combat: list[dict],
    cl0: float,
    cl1: float,
    c2m: float,
    *,
    render: Callable[[dict], str | None],
    floor: int = _EV_NOISE,
    limit: int = EVENT_LIMIT,
) -> tuple[list[str], int, int]:
    """先渲染、再分层保留，保证**时间上均匀覆盖整波团**。

    返回 ``(最终行, 丢弃条数, 候选总条数)``。

    为什么不能「从前往后取满 600 条就停」：那正是老 bug 的表现 ——
    前 7 秒塞满 600 行，后面 53 秒一条不剩，而后面恰恰是双方真正打起来的部分。

    现在的做法：
    1. 先给每条候选算优先级（:func:`_render_event` 返回 None 的直接出局，
       **不占名额**）；
    2. 优先级 0–2 的全留（阵亡/控制/技能/道具/暴击）；
    3. 剩余的按 4 秒分桶，每桶按优先级取，配额 = 剩余名额 / 桶数；
    4. 必留项自己就超限时（极端情况），把必留项也按桶抽稀，绝不整段丢掉尾部。

    Args:
        floor: 只留优先级 **不超过** 它的条目。默认 ``_EV_NOISE``（全收）；
            逐波核验传 ``_EV_HIGH``，直接丢掉「大额伤害」与「小额伤害、
            治疗、普通 buff 到期」——2026-11-27 实测，单波 960 行里
            有 646 行属于这一类，占了整块的 80% 体积。
        limit: 行数上限，默认 :data:`EVENT_LIMIT`。
    """
    cands: list[tuple[float, int, str]] = []
    for e in combat:
        t = float(e.get("t") or 0)
        if not (cl0 - EVENT_PAD <= t <= cl1 + EVENT_PAD):
            continue
        atk, tgt = str(e.get("atk") or ""), str(e.get("tgt") or "")
        if not (atk.startswith("npc_dota_hero_") or tgt.startswith("npc_dota_hero_")):
            continue
        line = render(e)
        if line is None:
            continue
        prio = _event_priority(e, atk, tgt)
        if prio > floor:
            continue
        cands.append((t, prio, line))

    if not cands:
        return [], 0, 0
    total = len(cands)
    if total <= limit:
        return [c[2] for c in cands], 0, total

    must = [c for c in cands if c[1] <= _EV_HIGH]
    rest = [c for c in cands if c[1] > _EV_HIGH]
    room = limit - len(must)

    def bucket_pick(items: list[tuple[float, int, str]], quota: int) -> list[tuple[float, int, str]]:
        """按 4 秒分桶，每桶优先取高优先级的，桶内保持时间顺序。"""
        buckets: dict[int, list[tuple[float, int, str]]] = {}
        for c in items:
            buckets.setdefault(int((c[0] - (cl0 - EVENT_PAD)) // 4.0), []).append(c)
        if not buckets:
            return []
        per = max(1, quota // len(buckets))
        out: list[tuple[float, int, str]] = []
        for bidx in sorted(buckets):
            group = sorted(buckets[bidx], key=lambda x: (x[1], x[0]))
            out.extend(sorted(group[:per], key=lambda x: x[0]))
        return out

    if room <= 0:
        # 必留项自己就超了：按桶抽稀，但**保留时间覆盖**，不砍尾巴
        kept = bucket_pick(must, limit)
    else:
        kept = must + bucket_pick(rest, room)
    kept.sort(key=lambda x: x[0])
    return [c[2] for c in kept], total - len(kept), total


def _event_priority(e: dict, atk: str, tgt: str) -> int:
    """一条事件的保留优先级，数字越小越必留。

    ⚠️ **必留档（≤ ``_EV_HIGH``）的总量必须远小于** :data:`EVENT_LIMIT`，
    否则「必留」这个词就没意义了 —— 实测第一版把「英雄对英雄的 ≥100 伤害」
    也算必留，结果必留项 974 条、超过 600 的限额，于是必留项自己也被抽稀，
    连清莲宝珠的 buff 结束行都被丢了（读者看到「用了清莲宝珠」，
    却看不到罩子什么时候断的）。

    现在的必留只放**无法从别处还原**的事件：
    · 英雄阵亡 —— 唯一来源
    · 控制效果（眩晕/沉默/位移/减速）—— 唯一来源，且是判断失误的核心依据
    · 技能与道具的施放 —— 连招顺序的唯一来源
    · 道具 buff 的起止 —— 主动道具「有没有用对」的唯一依据

    伤害再大也不进必留：伤害有聚合统计（块开头的 OpenDota 每人分项），
    丢几行不影响结论，而且伤害是高频事件，正是最该被抽稀的那一类。
    """
    typ = _strip_prefix(e.get("type"), "DOTA_COMBATLOG_")
    atk_hero = atk.startswith("npc_dota_hero_")
    tgt_hero = tgt.startswith("npc_dota_hero_")
    hvh = atk_hero and tgt_hero

    if typ == "DEATH":
        return _EV_KEEP
    if typ in ("ABILITY", "ITEM"):
        # 英雄对英雄的技能/道具是最高价值；对野怪的技能降一档但仍保留
        return _EV_KEEP if hvh or typ == "ITEM" else _EV_HIGH
    if typ == "MODIFIER_ADD":
        if e.get("stun") or e.get("sil") or e.get("motion") or e.get("slow"):
            return _EV_KEEP
        # 有实际 buff 时长（lotus_orb_active、mjollnir_static 一类）才是有效果的。
        #
        # ⚠️ 这里**必须和 ITEM 同级必留**：道具的「使用」和「效果」是两行，
        # 中间隔着抽稀。如果只把使用那行标为必留、把效果标成可丢，就会出现
        # 「Largo 使用 清莲宝珠 → 然后没有任何一件事发生」——
        # 用户看到的正是这个（2026-11-13：「主动道具的效果也没有识别到」）。
        try:
            mdur = float(e.get("mdur") or 0)
        except (TypeError, ValueError):
            mdur = 0.0
        if 0 < mdur < 60 and mdur >= 2.0:
            return _EV_KEEP if hvh else _EV_MID
        return _EV_NOISE
    if typ == "MODIFIER_REMOVE":
        try:
            mel = float(e.get("mel") or 0)
        except (TypeError, ValueError):
            mel = 0.0
        # 道具 buff 的「结束」和「加入」要配对保留：只留 ADD 会让读者
        # 不知道罩子什么时候断的，而「断了之后才被打死」恰恰是关键判断。
        if str(e.get("inf") or "").startswith("modifier_item_") and mel >= 1.0:
            return _EV_HIGH
        return _EV_HIGH if mel >= 1.0 else _EV_LOW
    if typ == "DAMAGE":
        # 伤害一律**不进必留档**（理由见 docstring）：它有聚合统计兜底，
        # 而且是最高频的事件，正是最该被抽稀的那一类。
        # 英雄对英雄的大额伤害排在次档（抽稀时优先留，但不保证留）。
        val = int(e.get("val") or 0)
        if hvh and val >= 100:
            return _EV_MID
        return _EV_LOW
    if typ in ("HEAL", "CRITICAL_DAMAGE"):
        return _EV_LOW
    return _EV_NOISE


def build_fight_block(
    match: dict,
    fight: dict,
    *,
    combat: list[dict],
    hp_series: dict[str, list],
    items: dict[str, list[tuple[float, list[str]]]],
    wards: list[dict],
    offsets: dict,
    npc_label: dict[str, str],
    hero_of: Callable[[Any], str],
    npc_by_hero: dict[int, str],
    item_label: Callable[[str], str] | None = None,
    ability_label: Callable[[str], str] | None = None,
    item_resolve: Callable[[str], str | None] | None = None,
    aghs: dict[str, list[tuple[float, str, str]]] | None = None,
    illusions: dict[str, list[tuple[float, float | None]]] | None = None,
    focus_account_ids: Iterable[Any] | None = None,
    density: str = "full",
) -> str:
    """把一波团整理成给模型看的文本块。

    时间一律用**比赛时间**；内部按 ``combatlog_to_match`` 换算回战斗日志时间。

    ``density`` 有两种：

    · ``"full"``（默认）—— 给人看完整过程、也用于旧的整体注入路径；
    · ``"brief"`` —— 给**逐波核验**用：事件只留阵亡/控制/技能/道具
      （丢掉大额伤害、小额伤害、治疗、普通 buff 到期），血量与位置按
      每两个采样点取一（约 4 秒一行）。实测同一波从 6.0 万字符降到
      2.2 万左右，而「谁先手、控了谁、谁先倒」这些画面要素一条没少。

    ``npc_label`` 是 ``npc_dota_hero_xxx → 显示名`` 的映射，由调用方用英雄表
    建好传进来：录像里的实体名是英文 npc 名，而插件显示的是中文名，
    在函数内部硬拼是拼不出来的。**键必须经过** :func:`canon_npc` 归一化，
    否则带下划线的英雄（Ogre_Magi 之类）会对不上血量表。

    ``item_label`` / ``ability_label`` 把录像里的内部键（``blinkdagger``、
    ``silencer_global_silence``）换成中文显示名。两者都可以不传 —— 不传时
    退化成「把下划线换成空格」，至少不会把 raw 键直接丢给模型。

    ``aghs`` 是 :func:`read_aghs` 的结果：A 杖祝福**不占装备格**，装备栏里
    永远看不到，必须单列一段，否则模型会认定「他没有 A 杖」，
    进而把「死了还在输出」编成「买活」。
    ``illusions`` 是 :func:`read_illusions` 的结果，用来把「本体已死、
    幻象还在场上打」这件事写清楚，而不是把幻象的战绩当成尸体在动。

    ``focus_account_ids`` 是主视角玩家的 ``account_id``（可多位）：只用于在
    开头的「十人名单」里标一个「（焦点）」。这个块的其他段落全部按英雄列，
    昵称不出现，所以没有这张表，模型无法把「请以某某为主视角」对到
    具体哪个英雄身上。
    """
    aghs = aghs or {}
    illusions = illusions or {}
    brief = str(density or "full").lower() == "brief"
    c2m = float(offsets.get("combatlog_to_match") or 0)
    d2c = float(offsets.get("demo_to_combatlog") or 0)
    demo_to_match = d2c - c2m

    t0 = float(fight.get("start") or 0)
    t1 = float(fight.get("end") or 0)
    cl0, cl1 = t0 + c2m, t1 + c2m
    # 装备快照是按 demo 时间记的
    demo_start = t0 - demo_to_match

    label = item_label or (lambda x: str(x).replace("_", " "))
    ability = ability_label or (lambda x: str(x).replace("_", " "))

    def name_of(npc: str) -> str:
        """事件流水里显示用的名字。

        英雄优先走 ``npc_label``（中文名）；**先过 canon_npc**，因为事件里的
        atk/tgt 是原始写法（``npc_dota_hero_ogre_magi``），而 npc_label 的键
        是归一化后的（``npc_dota_hero_ogremagi``），不归一对不上。
        非英雄（小兵/野怪/建筑）退化成可读短名。
        """
        raw = str(npc or "")
        if raw.startswith("npc_dota_hero_"):
            hit = npc_label.get(canon_npc(raw))
            if hit:
                return hit
        short = _short(raw)
        if short.startswith("neutral_"):
            return "野怪·" + short[len("neutral_"):].replace("_", " ")
        if short.startswith("creep_"):
            return "兵线"
        return short.replace("_", " ")

    entries = fight.get("players") or []
    players = sorted([p for p in (match.get("players") or []) if isinstance(p, dict)],
                     key=lambda p: int(p.get("player_slot") or 0))

    def team_name(slot: int) -> str:
        """阵营。``slot`` 是 OpenDota 的 ``player_slot``（天辉 0–4、夜魇 128–132）。

        ⚠️ **不要传 players 的下标。** 十人齐全时下标 5–9 也满足 ``>=5``，
        结果碰巧是对的，于是这个混用一直没被发现；一旦缺人（或入口只给了
        部分玩家）下标就整体错位，夜魇会被标成天辉。
        实测就是这么踩到的：两个人时夜魇被算进天辉，人头比从「夜魇 1」
        变成「天辉 3」—— 正是这次要修的那类错误。
        """
        return "天辉" if slot < 5 else "夜魇"

    lines: list[str] = []
    lines.append("比赛 %s，时长 %s，%s。以下所有时间都是**比赛时间**。" % (
        match.get("match_id"), _clock(match.get("duration")),
        "天辉获胜" if match.get("radiant_win") else "夜魇获胜"))

    # 十人名单：这个块的**每一段都按英雄列**（血量、技能、位置），
    # 玩家的昵称在别处一次都不出现。主视角说的是「某个人」，
    # 没有这张对照表，模型就只能猜谁在玩哪个英雄 —— 猜错了整份报告
    # 都在评一个不相干的人，而且从文字上看不出来。
    lines.append("")
    lines.append("=== 十人名单（阵营 · 玩家 · 英雄）===")
    focus_set: set[int] = set()
    for raw_id in focus_account_ids or ():
        try:
            focus_set.add(int(raw_id))
        except (TypeError, ValueError):
            continue
    for player in players[:10]:
        hero = hero_of(player.get("hero_id"))
        name = str(player.get("personaname") or "").strip()
        try:
            account_id = int(player.get("account_id") or 0)
        except (TypeError, ValueError):
            account_id = 0
        mark = "（焦点）" if account_id and account_id in focus_set else ""
        who = "%s · %s" % (name, hero) if name else hero
        try:
            side = "天辉" if int(player.get("player_slot") or 0) < 5 else "夜魇"
        except (TypeError, ValueError):
            side = "天辉"
        lines.append("  [%s] %s%s" % (side, who, mark))

    lines.append("")
    lines.append("=== 这一波的坐标与结果（OpenDota 统计）===")
    lines.append("时间 %s – %s，持续 %s，阵亡 %s 人。" % (
        _clock(t0), _clock(t1), _clock(max(0, t1 - t0)), fight.get("deaths")))

    # ---- 阵亡名单：**能直接算出来的结论，不要让模型自己加总** ----
    #
    # ⚠️ 2026-11-13：这一波的真实人头是**夜魇 2 换 4**（天辉死 4：撼地者 15:02、
    # 沉默 15:13、食人魔 15:15、主宰 15:18；夜魇死 2：复仇之魂 15:01、拉尔戈 15:10）。
    # 模型写成「夜魇最终 4 换 2」，把一场大胜写成小亏 —— 而它下一句列的阵亡名单
    # 和自己的结论是矛盾的。它不是在编，是**在数一堆散落在 1000 多行事件里的
    # 死亡记录**，数错了。
    #
    # 教训不是「让模型更仔细」，而是：凡是能从事件流直接统计的事实
    # （人头比、谁死了、谁在哪一刻死的），都必须在块里给出**成句的结论**。
    # 让模型做减法，等于把最容易错的一步交给最不该负责的一步。
    # 英雄判据取 ``npc_by_hero``（十人名单）而不是 ``hp_series`` 的键：
    # 血量表缺人时（缓存不全、解析中断）阵亡名单照样要能算出来，
    # 那是本波团最基础的事实，不能跟着血量数据一起丢。
    #
    # ⚠️ 阵营必须用 ``player_slot`` 判，**不能用 players 里的下标**。
    # 十人齐全时两者恰好都落在 0–9 且顺序一致，看不出区别；但只要缺人
    # （或像测试里只给两个），下标就全错位 —— 实测夜魇的玩家会被算进天辉，
    # 于是「夜魇 2 换 4」算成「天辉 3 换 0」，正是这次要修的那类错误。
    by_hero: dict[str, int] = {}
    for owner in players:
        npc = npc_by_hero.get(int(owner.get("hero_id") or 0))
        if not npc:
            continue
        try:
            slot = int(owner.get("player_slot") or 0)
        except (TypeError, ValueError):
            slot = 0
        by_hero[npc] = 0 if slot < 5 else 5
    hero_npcs = set(by_hero)
    side_kills: dict[str, list[str]] = {"天辉": [], "夜魇": []}
    for e in combat:
        if str(e.get("type") or "") != "DOTA_COMBATLOG_DEATH":
            continue
        when = float(e.get("t") or 0)
        if not (cl0 <= when <= cl1):
            continue
        victim = canon_npc(e.get("tgt"))
        if victim not in hero_npcs:
            continue
        side = "天辉" if by_hero[victim] == 0 else "夜魇"
        killer = canon_npc(e.get("atk"))
        kname = name_of(killer) if killer in hero_npcs else (
            "小兵/野怪/建筑" if killer and not killer.startswith("npc_dota_hero_") else "不明")
        side_kills[side].append("%s %s（被 %s 击杀）" % (
            _clock(when - c2m), name_of(victim), kname))
    if side_kills["天辉"] or side_kills["夜魇"]:
        lines.append("")
        lines.append("=== 这一波的人头（已统计好，直接引用，不要自己重新数）===")
        lines.append("  天辉阵亡 %d 人：%s" % (
            len(side_kills["天辉"]), "；".join(side_kills["天辉"]) or "无"))
        lines.append("  夜魇阵亡 %d 人：%s" % (
            len(side_kills["夜魇"]), "；".join(side_kills["夜魇"]) or "无"))
        lines.append("  → 本波人头交换：天辉 %d 换 夜魇 %d。"
                     % (len(side_kills["天辉"]), len(side_kills["夜魇"])))
    for slot, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        owner = players[slot] if slot < len(players) else {}
        damage = int(entry.get("damage") or 0)
        if not damage and not int(entry.get("deaths") or 0):
            continue
        # 买活次数是事后统计（`buybacks` 在不同数据源里可能是列表或整数）
        raw_buy = entry.get("buybacks")
        if isinstance(raw_buy, (list, dict)):
            buy_n = len(raw_buy)
        else:
            try:
                buy_n = int(raw_buy or 0)
            except (TypeError, ValueError):
                buy_n = 0
        buy_txt = "，买活 %d 次" % buy_n if buy_n else ""
        # 阵营照着**真实 player_slot** 判（entries 与 match.players 下标对齐，
        # 但下标本身不是 player_slot：夜魇是 128+0..4）。
        try:
            raw_slot = int(owner.get("player_slot") or slot)
        except (TypeError, ValueError):
            raw_slot = slot
        lines.append("  [%s] %s：伤害 %d，治疗 %d，阵亡 %d，经济 %+d%s" % (
            team_name(raw_slot),
            hero_of(owner.get("hero_id")),
            damage, int(entry.get("healing") or 0),
            int(entry.get("deaths") or 0), int(entry.get("gold_delta") or 0), buy_txt))

    # ---- 整局经济走势 ----
    gold = match.get("radiant_gold_adv") or []
    if gold:
        lines.append("")
        lines.append("=== 整局经济走势（每分钟，正数=天辉领先）===")
        for minute, value in enumerate(gold):
            if value is None:
                continue
            if minute < max(0, int(t0 // 60) - 3) or minute > int(t1 // 60) + 3:
                continue
            mark = "  ← 本波团" if int(t0 // 60) == minute else ""
            lines.append("%2d 分钟  经济差 %+7d%s" % (minute, int(value), mark))

    # ---- 开打前的状态与装备 ----
    lines.append("")
    lines.append("=== 开打前（%s）的状态 ===" % _clock(t0))
    # 「开打前最后状态」用两个字典分开记：``base`` 只收活着的，``seen`` 收全部。
    # 只用一个字典的话，「团战时正在读秒」会被写成「无数据」——
    # 而这两件事对判断的影响完全相反（前者说明他晚到，后者才说明数据缺失）。
    base: dict[str, tuple] = {}
    seen: dict[str, tuple[list[tuple], int]] = {}
    for npc, rows in hp_series.items():
        seq = sorted(rows, key=lambda r: r[0])
        for i, row in enumerate(seq):
            mt = row[0] + demo_to_match
            if t0 - 20 <= mt <= t0 + 5:
                seen[npc] = (seq, i)
                if row[6] == 1 and row[1] > 0:
                    base[npc] = row
    def _raw_slot(player: dict) -> int:
        """``player_slot`` 原值；缺失时退回 0（宁可当天辉，也不静默错位）。"""
        try:
            return int(player.get("player_slot") or 0)
        except (TypeError, ValueError):
            return 0

    for slot in range(10):
        owner = players[slot] if slot < len(players) else {}
        npc = npc_by_hero.get(int(owner.get("hero_id") or 0), "")
        row = base.get(npc) if npc else None
        if row:
            hp_text = "Lv%d  %d/%d 血  %d/%d 蓝" % (row[5], row[1], row[2], row[3], row[4])
        elif npc and npc in seen:
            # 「团战时是尸体」和「这个人的数据缺失」是两件完全不同的事：
            # 前者说明他晚到（或者正在读秒，队友在等他），后者才说明我们没数据。
            # 而「还有几秒复活」尤其关键 —— 队友 5 秒后就到，控场英雄此时先手
            # 是配合；只看「当时全是尸体」会把正确的先手判成送人头。
            seq, idx = seen[npc]
            dead_demo, revive_demo = _death_span(seq, idx)
            dead_mt = dead_demo + demo_to_match
            if revive_demo is None:
                hp_text = "阵亡读秒中（%s 阵亡），到本节数据结尾仍未复活" % _clock(dead_mt)
            else:
                rev_mt = revive_demo + demo_to_match
                gap = rev_mt - t0
                if gap >= 1:
                    hp_text = "阵亡读秒中（%s 阵亡），%s 复活，团战开始后 %.0f 秒回来" % (
                        _clock(dead_mt), _clock(rev_mt), gap)
                else:
                    hp_text = "阵亡读秒中（%s 阵亡），%s 复活" % (
                        _clock(dead_mt), _clock(rev_mt))
        else:
            hp_text = "无数据"
        gear = gear_at(items, npc, demo_start) if npc else []
        lines.append("  [%s] %-10s %s  装备：%s" % (
            team_name(_raw_slot(owner)), hero_of(owner.get("hero_id")), hp_text,
            "、".join(label(g) for g in gear) if gear else "空"))

    # ---- A 杖祝福 ----
    #
    # ⚠️ 这一段是**必须单列**的，不能指望装备栏（2026-11-13 查实的坑）：
    # 熔铸赠送的 A 杖**不占装备格**，受赠者身上连 modifier 都没有，
    # 全录像只留一条 ITEM 事件。于是数据块里「复仇之魂的装备：分身斧、治疗药膏、
    # 真假眼、魔杖、动力鞋」看起来完全没有 A 杖，而实际上她 8:57 就拿到了。
    #
    # 后果是连锁的：英雄资料库写着「复仇之魂 A 杖 → 死亡时产生强幻象，
    # 幻象能放全部技能」，数据块却没写她有 A 杖 —— 模型只能得出「她没有 A 杖」，
    # 然后在「她死了还在输出」这件事上编出「买活」这种通用解释。
    # 用户当场就指出了：「不是买活，是 a 杖效果」。
    #
    # ⚠️ 必须分三档，不能只写「开打前有没有」（2026-11-13 实测踩到的）：
    # 噬魂鬼 14:49.20 拿到 A 杖，而这波团 14:46 就开始了 —— 只按开打前算，
    # 他会被归进「本波没有 A 杖」，**恰恰漏掉本波最关键的那一件装备**
    # （A 杖让他可以钻敌方英雄，也让他能钻小兵躲无敌斩）。
    # 所以：开打前已有 / 团战中刚获得 / 整波都没有，三档分开写。
    if aghs:
        got_rows: list[str] = []
        mid_rows: list[str] = []
        miss_rows: list[str] = []
        for slot in range(10):
            owner = players[slot] if slot < len(players) else {}
            npc = npc_by_hero.get(int(owner.get("hero_id") or 0), "")
            hero = hero_of(owner.get("hero_id"))
            side = team_name(_raw_slot(owner))
            hit = aghs_at(aghs, npc, cl0) if npc else None
            if not hit:
                # 开打时还没有 —— 看整波里有没有拿到
                mid = aghs_at(aghs, npc, cl1) if npc else None
                if mid and mid[0] > cl0:
                    when_cl, kind, note = mid
                    how = ("由 %s 熔铸赠送" % (name_of(note) if note else "队友")
                           if kind == "gift" else "自己吃下祝福")
                    mid_rows.append(
                        "  [%s] %s：**开打时没有**，%s 才%s —— 在此之前不能按有 A 杖判。"
                        % (side, hero, _clock(when_cl - c2m), how))
                else:
                    miss_rows.append(hero)
                continue
            when_cl, kind, note = hit
            when_mt = _clock(when_cl - c2m)
            if kind == "gift":
                got_rows.append("  [%s] %s：%s 由 %s 熔铸赠送（不占装备格，装备栏里看不到）" % (
                    side, hero, when_mt, name_of(note) if note else "队友"))
            else:
                got_rows.append("  [%s] %s：%s 自己吃下 A 杖祝福（不占装备格，装备栏里看不到）" % (
                    side, hero, when_mt))
        if got_rows or mid_rows:
            lines.append("")
            lines.append("=== A 杖祝福（不占装备格，务必与装备栏分开看）===")
            lines.append("（熔铸赠送或自己吃下的祝福都不占装备格，装备栏里查不到；"
                         "判断技能机制时必须算上）")
            if got_rows:
                lines.append("开打前（%s）就已经有 A 杖效果：" % _clock(t0))
                lines.extend(got_rows)
            if mid_rows:
                lines.append("团战进行中才拿到 A 杖（时间点之前不算有）：")
                lines.extend(mid_rows)
            if miss_rows:
                lines.append("  其余 %d 人（%s）整波团都**没有** A 杖祝福。" % (
                    len(miss_rows), "、".join(miss_rows)))

    # ---- 团战期间的复活 ----
    # 「他 10:12 复活」和「他整波都没到」对判断的影响完全不同：
    # 前者说明队友在等读秒、控场先手是配合；后者说明这波就是少人。
    revives: list[str] = []
    for npc, rows in hp_series.items():
        dead_at: float | None = None
        for row in sorted(rows, key=lambda r: r[0]):
            mt = row[0] + demo_to_match
            if row[6] == 0 and dead_at is None:
                dead_at = mt
            elif row[6] == 1 and dead_at is not None:
                if t0 - 40 <= mt <= t1 + 20:
                    revives.append("%s %s 复活（%s 阵亡，读秒 %.0f 秒）" % (
                        _clock(mt), name_of(npc), _clock(dead_at), mt - dead_at))
                dead_at = None
    if revives:
        lines.append("")
        lines.append("=== 团战前后的复活（读秒结束时刻）===")
        lines.extend(sorted(revives))

    # ---- 本体阵亡期间的替身（A 杖幻象）----
    #
    # ⚠️ 这张表是为了修一个**结论级**错误（2026-11-13 用户当场指出）。
    # ``hp.csv`` 只追踪本体实例：本体一死，alive 就写 0、坐标定住不动，
    # 直到引擎的复活计时器到点才翻回 1。而 A 杖幻象是**另一个实体**，
    # 录像里只留一条 ``modifier_illusion``，血量表完全看不见。
    #
    # 于是数据块只写了「复仇之魂 15:50 复活（15:01 阵亡，读秒 49 秒）」，
    # 而战斗日志里她 15:08 就在放恐怖波动、15:13 亲手杀了沉默术士 ——
    # 模型面对「尸体在输出」，套了通用 Dota 常识，写成「她买活了」。
    # 用户直接纠正：「不是买活，是 a 杖效果」。
    #
    # 判据：幻象活跃区间**覆盖了本体是尸体的那段时间**，才算「死亡替身」。
    # 只看有没有 ``modifier_illusion`` 会把分身斧也认成死亡替身 ——
    # 本局复仇之魂、噬魂鬼各有一堆 dur≈18 的分身斧幻象，都在活着的时候。
    if illusions:
        notes: list[str] = []
        for npc, spans in illusions.items():
            seq = sorted(hp_series.get(npc) or [], key=lambda r: r[0])
            if not seq:
                continue
            for born, gone in spans:
                born_mt = born - c2m
                if gone is None:
                    gone_mt = None
                else:
                    gone_mt = gone - c2m
                if not (t0 - 40 <= born_mt <= t1 + 20):
                    continue
                # 生成前后「本体正在死」吗？
                #
                # ⚠️ 不能要求「生成那一刻采样到的是尸体」：血量是每约 2.1 秒采一次，
                # 幻象在引擎判定死亡的同一帧就生成，而采样点往往还停在
                # 「刚挨完最后一下、还活着」（实测复仇之魂 15:01.09 采样仍是
                # alive=1/271 血，15:01.24 幻象生成，15:03.19 才采到 alive=0）。
                # 按「当时必须是尸体」判，这条最有价值的替身会被整个漏掉。
                #
                # 所以窗口放宽到「生成后 4 秒内翻成尸体」。这个宽度既能容下
                # 采样间隔（2.1s×2），又远小于分身斧的 18 秒寿命，
                # 不会把活着时开的幻象误判成死亡替身。
                dead_soon = False
                for row in seq:
                    mt = row[0] + demo_to_match
                    if mt < born_mt - 3.0:
                        continue
                    if mt > born_mt + 4.0:
                        break
                    if row[6] == 0:
                        dead_soon = True
                        break
                if not dead_soon:
                    continue
                span = ("%s 消失" % _clock(gone_mt)) if gone_mt else "录像结尾仍在"
                notes.append(
                    "%s 本体约 %s 阵亡，幻象从 %s 打到 %s（%d 秒）。"
                    "这期间战斗日志里出现的「%s 施法/输出」都是**幻象**在做，"
                    "本体仍然在读秒 —— 不要判成买活或本体复活。"
                    % (name_of(npc), _clock(born_mt), _clock(born_mt), span,
                       int(gone_mt - born_mt) if gone_mt else 0, name_of(npc)))
        if notes:
            lines.append("")
            lines.append("=== 本体阵亡期间的替身（幻象仍在输出）===")
            lines.extend(notes)

    # ---- 事件流水 ----
    #
    # ⚠️ 这里曾经有一个非常隐蔽的截断 bug（2026-11-13 排查昨晚团战复盘时发现）：
    #
    #     count += 1                    # ← 在 elif 链**外面**，不管有没有写出行都加
    #     if count > 600: break
    #
    # `DOTA_COMBATLOG_MODIFIER_STACK_EVENT` 这类事件在 elif 链里根本没有分支，
    # 一条都不输出，却照样吃掉名额。实测那一波团里它单独就有 876 条，
    # 于是 600 个名额只换来 392 行有效事件，事件流到 14:53 就断了 ——
    # 而团战实际打到 15:34，**后 74%（53 秒）一行都没有**。
    #
    # 更糟的是它"看起来"是正常的：块里有内容、有格式、没有任何报错。
    # 模型当时在报告里写了「14:53 之后，日志截断，只剩血和坐标」——
    # 它没编，是我们真没给它。而 OpenDota 统计这一波死 6 个人，
    # 块里英雄阵亡记录是 0 条（只剩野怪和小兵）。
    #
    # 现在的口径：
    #   1. 先按「信息价值」分层打分，只在**真正写出行**时才计数；
    #   2. 超限时按时间分桶抽稀，保证整波团**时间上均匀覆盖**，
    #      而不是「从头开始取满 600 条然后砍掉后面」。
    lines.append("")
    lines.append("=== 战斗日志（%s – %s）===" % (_clock(t0 - EVENT_PAD), _clock(t1 + EVENT_PAD)))
    picked, dropped, total_cand = _triage_events(
        combat, cl0, cl1, c2m, render=lambda e: _render_event(
            e, c2m, name_of, item_label=label, ability_label=ability,
            item_resolve=item_resolve,
        ),
        floor=_EV_HIGH if brief else _EV_NOISE,
        # 两种模式共用同一个行数上限：核验模式已经把噪音挡在 floor 之外，
        # 留下来的全是阵亡/控制/技能/道具 —— 再对它们抽稀就等于随机丢控制，
        # 而「控了几秒、谁被控」正是这层唯一不可替代的证据。
        limit=EVENT_LIMIT,
    )
    lines.extend(picked)
    if brief:
        note = (
            "（核验模式：阵亡、控制、技能、道具逐条保留，共 %d 条；"
            "小额伤害与持续治疗见下方聚合段" % len(picked)
        )
        if dropped:
            note += "；超出上限再按时间均匀抽稀 %d 条" % dropped
        lines.append(note + "）")
        aggregated = _aggregate_noise_events(
            combat, cl0, cl1, c2m,
            name_of=name_of,
            item_label=label,
            ability_label=ability,
            item_resolve=item_resolve,
            keep_single=_AGG_KEEP_SINGLE,
        )
        if aggregated:
            lines.append("")
            lines.append(
                "=== 小额高频记录（按时段聚合；单次 ≥%d 的伤害、≥%d 的救人治疗仍逐条列出）==="
                % (_AGG_KEEP_SINGLE, _AGG_KEEP_HEAL_OTHER)
            )
            lines.extend(aggregated)
    elif dropped:
        lines.append(
            "（共 %d 条候选，保留 %d 条；按时间分桶抽稀，已略去 %d 条 "
            "重复的小额伤害/短增益，阵亡·控制·技能·道具·大额伤害全部保留）"
            % (total_cand, len(picked), dropped)
        )

    # ---- 血量曲线 ----
    lines.append("")
    lines.append("=== 血量（每约 2 秒，当前/最大）===")
    snap_times: set[float] = set()
    for rows in hp_series.values():
        for row in rows:
            mt = row[0] + demo_to_match
            if t0 <= mt <= t1 + 3:
                snap_times.add(round(mt, 1))
    ordered = sorted(snap_times)
    if brief:
        # 每两个采样点取一（约 4.2 秒一行）。血量/位置段原本各占 4 千字符，
        # 而画面还原看的是「谁什么时候掉到危险线」，不是 2.1 秒一格的全量轨迹。
        # 阵亡与复活另有一段独立输出（见上），不会因为这里抽稀而消失。
        ordered = ordered[::2]
    for mt in ordered:
        parts = []
        for npc, rows in hp_series.items():
            best = None
            for row in rows:
                if abs(row[0] + demo_to_match - mt) < 1.1:
                    best = row
                    break
            if not best:
                continue
            parts.append("%s %s" % (name_of(npc),
                                    "死亡" if best[6] == 0 else "%d/%d" % (best[1], best[2])))
        if parts:
            lines.append("%s %s" % (_clock(mt), "  ".join(parts)))

    # ---- 位置 ----
    lines.append("")
    lines.append("=== 位置（x,y），同一时刻坐标完全相同 = 被控在原地 ===")
    for mt in ordered:
        parts = []
        for npc, rows in hp_series.items():
            for row in rows:
                if abs(row[0] + demo_to_match - mt) < 1.1 and row[6] == 1:
                    parts.append("%s(%.0f,%.0f)" % (name_of(npc), row[7], row[8]))
                    break
        if parts:
            lines.append("%s %s" % (_clock(mt), " ".join(parts)))

    # ---- 视野 ----
    lines.append("")
    lines.append("=== 视野（团战中心 4500 码以内）===")
    shown = []
    center = _fight_center(hp_series, t0, t1, demo_to_match)
    for w in wards:
        dist = ((w["x"] - center[0]) ** 2 + (w["y"] - center[1]) ** 2) ** 0.5
        if dist <= 4500 and w["last"] >= t0 - 150 and w["born"] <= t1:
            shown.append((dist, w))
    if not shown:
        lines.append("（团战区域附近没有守卫）")
    for dist, w in sorted(shown, key=lambda x: x[1]["born"]):
        lines.append("%s %s 放下%s（%.0f,%.0f），距中心 %.0f 码" % (
            _clock(w["born"]), w["team"], w["kind"], w["x"], w["y"], dist))
    lines.append("（守卫进迷雾会从录像里消失，「消失」不等于「被反」，只能当地理参考）")
    return "\n".join(lines)


def _fight_center(hp_series: dict[str, list], t0: float, t1: float, offset: float) -> tuple[float, float]:
    xs, ys = [], []
    for rows in hp_series.values():
        for row in rows:
            mt = row[0] + offset
            if t0 <= mt <= t1 and row[6] == 1:
                xs.append(row[7])
                ys.append(row[8])
    if not xs:
        return (0.0, 0.0)
    xs.sort()
    ys.sort()
    return (xs[len(xs) // 2], ys[len(ys) // 2])
