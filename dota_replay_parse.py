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
from typing import Any, Callable

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


# ======================================================================
# 时间标定
# ======================================================================

#: ``CDOTA_Unit_Hero_FacelessVoid`` → ``faceless_void`` 这类驼峰转换。
#: 绝大多数英雄符合这个规律；不符合的（如 Furion / NaturesProphet）会因为
#: 匹配不上而被跳过，标定用集合对齐，少几个英雄不影响结果。
_HERO_DT_PREFIX = "CDOTA_Unit_Hero_"


def _camel_to_snake(name: str) -> str:
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i and (not name[i - 1].isupper()):
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


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
            npc = parts[2]
            if npc.startswith(_HERO_DT_PREFIX):
                npc = "npc_dota_hero_" + _camel_to_snake(npc[len(_HERO_DT_PREFIX):])
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
        b = round(tower_times[-1] - float(match.get("duration") or 0), 2)
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
            npc = parts[2]
            if npc.startswith(_HERO_DT_PREFIX):
                npc = "npc_dota_hero_" + _camel_to_snake(npc[len(_HERO_DT_PREFIX):])
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
) -> str:
    """把一波团整理成给模型看的文本块。

    时间一律用**比赛时间**；内部按 ``combatlog_to_match`` 换算回战斗日志时间。

    ``npc_label`` 是 ``npc_dota_hero_xxx → 显示名`` 的映射，由调用方用英雄表
    建好传进来：录像里的实体名是英文 npc 名，而插件显示的是中文名，
    在函数内部硬拼是拼不出来的。
    """
    c2m = float(offsets.get("combatlog_to_match") or 0)
    d2c = float(offsets.get("demo_to_combatlog") or 0)
    demo_to_match = d2c - c2m

    t0 = float(fight.get("start") or 0)
    t1 = float(fight.get("end") or 0)
    cl0, cl1 = t0 + c2m, t1 + c2m
    # 装备快照是按 demo 时间记的
    demo_start = t0 - demo_to_match

    label = item_label or (lambda x: x)

    def name_of(npc: str) -> str:
        return npc_label.get(npc) or _short(npc)

    entries = fight.get("players") or []
    players = sorted([p for p in (match.get("players") or []) if isinstance(p, dict)],
                     key=lambda p: int(p.get("player_slot") or 0))

    def team_name(slot: int) -> str:
        return "天辉" if slot < 5 else "夜魇"

    lines: list[str] = []
    lines.append("比赛 %s，时长 %s，%s。以下所有时间都是**比赛时间**。" % (
        match.get("match_id"), _clock(match.get("duration")),
        "天辉获胜" if match.get("radiant_win") else "夜魇获胜"))

    lines.append("")
    lines.append("=== 这一波的坐标与结果（OpenDota 统计）===")
    lines.append("时间 %s – %s，持续 %s，阵亡 %s 人。" % (
        _clock(t0), _clock(t1), _clock(max(0, t1 - t0)), fight.get("deaths")))
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
        lines.append("  [%s] %s：伤害 %d，治疗 %d，阵亡 %d，经济 %+d%s" % (
            team_name(slot),
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
            team_name(slot), hero_of(owner.get("hero_id")), hp_text,
            "、".join(label(g) for g in gear) if gear else "空"))

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

    # ---- 事件流水 ----
    lines.append("")
    lines.append("=== 战斗日志（%s – %s）===" % (_clock(t0 - EVENT_PAD), _clock(t1 + EVENT_PAD)))
    count = 0
    for e in combat:
        t = float(e.get("t") or 0)
        if not (cl0 - EVENT_PAD <= t <= cl1 + EVENT_PAD):
            continue
        atk, tgt = e.get("atk") or "", e.get("tgt") or ""
        if not (atk.startswith("npc_dota_hero_") or tgt.startswith("npc_dota_hero_")):
            continue
        typ = str(e.get("type") or "").replace("DOTA_COMBATLOG_", "")
        info = str(e.get("inf") or "").replace("modifier_", "").replace("ability_", "")
        tm = _clock(t - c2m)
        a, g = _short(atk), _short(tgt)
        if typ == "DAMAGE":
            lines.append("%s %s -> %s %s 伤害%d" % (tm, a, g, info, e.get("val") or 0))
        elif typ == "HEAL":
            # 1~4 点的治疗基本都是光环/回复符的持续跳动（实测一次团战里
            # 能刷出上百行「治疗 1」），留着只会把真正的关键事件淹掉。
            if int(e.get("val") or 0) < 5:
                continue
            lines.append("%s %s 治疗 %s %d" % (tm, a, g, e.get("val") or 0))
        elif typ == "MODIFIER_ADD":
            extra = []
            if e.get("stun"):
                extra.append("眩晕%.1fs" % float(e["stun"]))
            if e.get("sil"):
                extra.append("沉默")
            if e.get("motion"):
                extra.append("位移控制")
            lines.append("%s %s 的 %s 作用于 %s（%s）" % (tm, a, info, g, " ".join(extra) or "-"))
        elif typ == "MODIFIER_REMOVE":
            lines.append("%s %s 身上的 %s 结束（实际%.1fs）" % (tm, g, info, float(e.get("mel") or 0)))
        elif typ == "ABILITY":
            lines.append("%s %s 施放 %s%s%s" % (
                tm, a, info, "（大招）" if e.get("ult") else "",
                (" -> " + g) if tgt.startswith("npc_dota_hero_") else ""))
        elif typ == "ITEM":
            lines.append("%s %s 使用 %s%s" % (
                tm, a, info, (" -> " + g) if tgt.startswith("npc_dota_hero_") else ""))
        elif typ == "DEATH":
            lines.append("%s %s 阵亡，击杀者 %s" % (tm, g, a or "?"))
        count += 1
        if count > 600:
            lines.append("（事件过多，已截断）")
            break

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
