"""关键团候选、运行环境检测与**单线**录像解析队列。

这一模块解决三个问题
--------------------
1. **哪几波团值得深挖**：只用 OpenDota 的 ``teamfights`` 就能算出来，
   不需要碰录像。整局 20 分钟里有十几二十波交火，全丢给模型 token 会爆，
   所以先在报告里给出一份带编号的候选清单，用户点名后再深挖。
2. **环境够不够**：录像解析依赖 JRE + 解析引擎 + zstandard，插件市场装来的
   用户多半没有。调用前先检测，缺什么就明说怎么装，而不是抛一串栈。
3. **不许并发**：云端容器内存上限 900 MiB，AstrBot 本体就占掉约 385 MiB，
   单场解析峰值约 160 MiB。一场很宽裕，两场同时跑必被 OOM kill ——
   而这个容器自己就是 PID 1，被杀等于整个机器人下线。
   所以所有解析任务必须过同一个单消费者队列。

编号约定
--------
候选清单里的「第 N 波」= ``teamfights`` 数组的第 N 项（从 1 开始、按时间顺序）。
**不做按关键度重排**：重排之后编号会随算法微调而漂移，用户上次说的
「第 7 波」下次就指向另一波了。清单只决定「展示哪几波」，不改变编号。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")

#: 解析引擎默认位置。放在 ``data/work`` 下是为了**容器重建后不丢**：
#: 该目录是宿主机挂载进来的，而容器的 /usr 是临时的。
DEFAULT_ENGINE_DIR = "/AstrBot/data/work/dota-replay"

#: 候选清单默认展示几波。
DEFAULT_TOP_N = 4

#: 队列里最多允许排几个（含正在跑的那个）。再多就拒绝，
#: 免得用户连点十次把一场 20 分钟的解析排到天荒地老。
MAX_QUEUE = 4


# ======================================================================
# 运行环境
# ======================================================================


@dataclass
class RuntimeStatus:
    """录像解析的运行环境检测结果。"""

    ok: bool = False
    java: str = ""
    engine: str = ""
    keyword: str = "/d2"
    problems: list[str] = field(default_factory=list)
    detail: list[str] = field(default_factory=list)

    def describe(self) -> str:
        """给用户看的说明（缺什么、怎么装）。"""
        if self.ok:
            return "✅ 录像解析环境就绪。"
        lines = ["⚠️ 录像深度解析需要额外运行环境，当前还缺："]
        for item in self.problems:
            lines.append(f"· {item}")
        lines.append("")
        lines.append("安装方法（在 AstrBot 所在机器执行）：")
        lines.append("1. 装 Java 运行时（21 或更高）：")
        lines.append("   `apt-get install -y openjdk-21-jre-headless`")
        lines.append("   或从 https://adoptium.net 下载 Temurin 21 的 JRE 解压到任意目录。")
        lines.append(f"2. 把解析引擎（classes/ 与 lib/）放到 {self.engine or DEFAULT_ENGINE_DIR}")
        lines.append("3. 装 Python 依赖：`pip install zstandard`")
        lines.append("")
        lines.append(f"装好后可用 `{self.keyword} 复盘 环境` 复查。")
        return "\n".join(lines)


def _resolve_java(engine_dir: str, java_path: str = "") -> str:
    """按「显式配置 → 引擎自带 → PATH」三级找 java。"""
    candidates = []
    if java_path:
        candidates.append(java_path)
    if engine_dir:
        candidates.append(os.path.join(engine_dir, "jre", "bin", "java"))
    found = shutil.which("java")
    if found:
        candidates.append(found)
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return ""


def _java_works(java: str, timeout: float = 10.0) -> bool:
    """``java -version`` 能不能正常跑完（能跑说明架构/glibc 都对得上）。"""
    try:
        proc = subprocess.run(  # noqa: S603 - 路径来自本地配置，不接受用户输入
            [java, "-version"],
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def check_runtime(engine_dir: str = "", java_path: str = "", keyword: str = "/d2") -> RuntimeStatus:
    """检测录像解析能不能跑。

    只做**便宜**的检查（文件存在 + ``java -version`` + import zstandard），
    不真的解析一场录像 —— 那个要 20 多秒，不该藏在「预检」里。
    """
    engine = (engine_dir or DEFAULT_ENGINE_DIR).strip()
    status = RuntimeStatus(engine=engine, keyword=keyword)
    status.detail.append(f"引擎目录：{engine}")

    # 1) 解析引擎
    need = {
        "classes/Snap.class": "血量与眼位采样器",
        "classes/Dump.class": "战斗日志导出器",
        "classes/Eco.class": "装备快照导出器",
        "lib/clarity-5.0.1.jar": "Clarity 录像解析库",
    }
    for rel, label in need.items():
        if not os.path.isfile(os.path.join(engine, rel)):
            status.problems.append(f"解析引擎缺文件：{rel}（{label}）")
    if not os.path.isdir(os.path.join(engine, "lib")):
        status.problems.append("解析引擎目录下没有 lib/（jar 依赖）")

    # 2) Java
    java = _resolve_java(engine, java_path)
    if not java:
        status.problems.append("找不到 Java 运行时（java / jre/bin/java 都没有）")
    elif not _java_works(java):
        status.problems.append(f"Java 存在但跑不起来：{java}（版本或架构不匹配）")
    else:
        status.java = java
        status.detail.append(f"Java：{java}")

    # 3) Python 解压依赖
    try:
        import zstandard  # noqa: F401

        status.detail.append("zstandard：已安装")
    except ImportError:
        status.problems.append("缺少 Python 模块 zstandard（用来解压 .dem.bz2）")

    status.ok = not status.problems
    return status


# ======================================================================
# 关键团候选（纯 OpenDota 数据，零成本）
# ======================================================================


def _clock(sec: Any) -> str:
    try:
        total = max(0, int(sec))
    except (TypeError, ValueError):
        return "-"
    return "%02d:%02d" % (total // 60, total % 60)


# ======================================================================
# 胜率曲线：判断「关键团」的客观口径
# ======================================================================
#
# 为什么不用阵亡人数或经济增量
# ----------------------------
# 两者都不回答「这波之后局势有没有变」。实测一场 19 分钟的对局：
#
#   第 3 波  7 人阵亡   胜率 79% → 88%（+9）
#   第 7 波  6 人阵亡   胜率 88% → 90%（+2）
#   第 10 波 4 人阵亡   胜率 99% → 99%（+0，比赛已定）
#
# 按阵亡人数排，「第 3 波」和「第 7 波」几乎并列；按胜率排，第 3 波
# 是全场唯一真正改变胜负走向的一波。用户要的是后者。
#
# 时间轴
# ------
# 曲线自带的时间是 **demo 时间**（从录像开始算，含选人），不是比赛时间。
# 实测某局曲线终点 1338.8 秒而比赛时长 1173 秒，差 165.8 秒 ——
# 直接拿它当比赛时间会让所有团战错位近三分钟，而错位之后数据看起来
# 依然「像那么回事」（每波都有胜率、都在合理区间），极难发现。
# 所以一律减 ``end - duration`` 换算，且这个偏移**不写死**。


@dataclass
class FightImpact:
    """一波团对胜负的影响。

    ``before`` / ``after`` 都是 0~100 的胜率（天辉视角），
    ``delta`` 为正表示这波之后天辉更接近赢。
    """

    index: int
    start: int
    end: int
    deaths: int
    before: float
    after: float
    delta: float

    @property
    def span(self) -> str:
        return "%s–%s" % (_clock(self.start), _clock(self.end))

    @property
    def shown_delta(self) -> int:
        """展示用的整数差。

        ``delta`` 是精确浮点差，``before`` / ``after`` 各自四舍五入 ——
        直接显示 ``round(delta)`` 会出现「80% → 88%（+7）」这种前后打架的写法。
        展示值一律取「取整后的后值减前值」，排序仍用精确值。
        """
        return round(self.after) - round(self.before)


@dataclass
class WinCurve:
    """整局胜率曲线与逐波团的影响。"""

    points: list[tuple[float, float]] = field(default_factory=list)
    fights: list[FightImpact] = field(default_factory=list)
    swings: list[dict] = field(default_factory=list)
    step: float = 0.0
    sampled_by: str = ""
    #: 阵容先验胜率（天辉视角，0~100）。
    #:
    #: 曲线最左端那个点就是 Valve 按双方阵容算出来的开盘值，不含任何操作 ——
    #: 开局 35% 的阵容输了很正常，开局 62% 的阵容赢了也说明不了打得好。
    #: 归因时它是必须扣掉的基线，否则会把「阵容差距」算成「谁打得好」。
    opening: float | None = None
    #: 全程天辉胜率的最高 / 最低点，用来一眼看出这局是单向还是来回。
    peak: float = 0.0
    trough: float = 0.0

    @property
    def ok(self) -> bool:
        return len(self.points) >= 8

    def chance_at(self, t: float) -> float:
        """取 ``t`` 时刻（比赛时间）的胜率，落在两点之间就取靠前的那个。"""
        if not self.points:
            return 0.0
        if t <= self.points[0][0]:
            return self.points[0][1]
        if t >= self.points[-1][0]:
            return self.points[-1][1]
        lo, hi = 0, len(self.points) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if self.points[mid][0] <= t:
                lo = mid
            else:
                hi = mid
        return self.points[lo][1]

    def turning_points(self, top_n: int = 3) -> list[FightImpact]:
        """按胜率影响排序的转折点（不是按阵亡人数）。"""
        return sorted(self.fights, key=lambda f: -abs(f.delta))[:top_n]

    def impact_at(self, index: int) -> FightImpact | None:
        """取第 ``index`` 波（从 1 开始）的胜率影响；没有就返回 None。"""
        for fight in self.fights:
            if fight.index == index:
                return fight
        return None


#: 团战结束后多看几秒再取「打完之后」的胜率。
#: 只取窗口内部会漏掉「打赢团顺势破塔」带来的后续变化，
#: 而那部分收益同样属于这波团 —— 但也不能看得太远，否则会把
#: 下一波团算进来。取 15 秒，约等于一次推塔 + 撤退的时间。
AFTER_GLANCE = 15.0

#: 曲线相邻两点差到这个幅度才算「走势变化」，用来找没有团战的转折
#: （推塔 / 肉山 / 关键装备成型）。低于这个数多半是采样噪声。
SWING_THRESHOLD = 5.0

#: 三分钟之前不判「运营转折」。这段时间胜率还在从选人阶段的先验收敛，
#: 实测会出现「开局涨 10 个点」但没有任何事件对应的假信号。
SWING_EARLIEST = 180.0


def _curve_points(curve: dict, duration: int) -> tuple[list[tuple[float, float]], float, str]:
    """把导出结果换算成 ``[(比赛时间, 胜率%)]``。

    优先用 ``series``（逐次更新的浮点值，粒度更细），它点数不够时退回
    ``chance`` 那份 64 点整数数组。两者都是 demo 时间，减同一个偏移。

    Returns:
        ``(points, step, source)``。
    """
    end = float(curve.get("end") or 0.0)
    offset = end - duration if duration else 0.0

    series = curve.get("series") or []
    pts: list[tuple[float, float]] = []
    for item in series:
        try:
            t, p = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if p <= 0.0 and t < 300:
            # 开局那个 0 是「还没算出来」的占位，不是真的 0%
            continue
        # 减完偏移是负数的点落在选人阶段：那时模型还在用先验推算，
        # 留着会让「整局」第一项显示成 00:00，还会造出「开局胜率跳了 10 个点」
        # 这种假转折（实测有 00:41 +10 的假信号）。
        local = t - offset
        if local < 0:
            continue
        pts.append((local, round(p * 100.0, 1)))

    if len(pts) >= 20:
        pts.sort()
        gaps = sorted(b[0] - a[0] for a, b in zip(pts, pts[1:]))
        return pts, (gaps[len(gaps) // 2] if gaps else 0.0), "series"

    start = float(curve.get("start") or 0.0)
    chance = curve.get("chance") or []
    values = [float(c) for c in chance if isinstance(c, (int, float))]
    if not values:
        return [], 0.0, ""
    step = (end - start) / (len(values) - 1) if len(values) > 1 else 0.0
    for i, v in enumerate(values):
        local = start + i * step - offset
        if local < 0:
            continue
        pts.append((local, v))
    return pts, step, "chance"


def _opening_chance(curve: dict) -> float | None:
    """阵容先验胜率：曲线最左端那个有效点。

    它落在选人结束时（比赛时间 0 附近甚至之前），会被
    :func:`_curve_points` 的「负时间过滤」剔掉 —— 这里单独取一次：
    它是判断「输在阵容还是输在人」的基线，不能跟着选人阶段一起丢。
    """
    for item in curve.get("series") or []:
        try:
            p = float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if p <= 0.0:
            continue
        return round(p * 100.0, 1)
    for v in curve.get("chance") or []:
        if isinstance(v, (int, float)) and float(v) > 0:
            return round(float(v), 1)
    return None


def align_curve(curve: dict, match: dict) -> WinCurve:
    """把胜率曲线对到每一波团上。

    对齐口径：以团战 ``start`` 时刻的胜率为「打之前」，
    以 ``end + :data:`AFTER_GLANCE``` 时刻的胜率为「打之后」。
    """
    try:
        duration = int(match.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0

    points, step, source = _curve_points(curve or {}, duration)
    out = WinCurve(points=points, step=step, sampled_by=source)
    if not out.ok:
        return out
    out.opening = _opening_chance(curve or {})
    values = [v for _, v in points]
    out.peak = max(values)
    out.trough = min(values)

    for index, fight in enumerate(match.get("teamfights") or [], start=1):
        if not isinstance(fight, dict):
            continue
        try:
            start = int(fight.get("start") or 0)
            end = int(fight.get("end") or start)
            deaths = int(fight.get("deaths") or 0)
        except (TypeError, ValueError):
            continue
        before = out.chance_at(float(start))
        after = out.chance_at(float(end) + AFTER_GLANCE)
        out.fights.append(FightImpact(
            index=index,
            start=start,
            end=end,
            deaths=deaths,
            before=before,
            after=after,
            delta=round(after - before, 1),
        ))

    # 没有团战、但胜率明显跳变的窗口：推塔 / 肉山 / 关键装备。
    # 这些同样改变走向，只是不体现为「一波团」。
    for i in range(1, len(points)):
        t, v = points[i]
        prev_t, prev_v = points[i - 1]
        if t < SWING_EARLIEST:
            # 前三分钟不判运营转折：这段时间曲线还在从选人先验收敛，
            # 实测会出现「00:41 涨 10 个点」这种没有任何事件对应的假信号。
            continue
        delta = v - prev_v
        if abs(delta) < SWING_THRESHOLD:
            continue
        near = any(f.start - 40 <= t <= f.end + 40 for f in out.fights)
        if near:
            continue
        out.swings.append({
            "from": prev_t,
            "to": t,
            "before": prev_v,
            "after": v,
            "delta": round(delta, 1),
        })
    return out


def score_fights(
    match: dict,
    focus_ids: list[int] | tuple[int, ...] = (),
    curve: WinCurve | None = None,
) -> list[dict]:
    """给每一波团打关键度，返回**按时间顺序编号**的完整列表。

    关键度的口径，有两条路，取决于能不能拿到 Valve 的胜率曲线：

    **有曲线时（``curve`` 非空）—— 以胜率变化为准。**
    一个点的胜率变动记 10 分，阵亡人数只当同分时的次序。理由见上面
    「胜率曲线」那节的实测：有一波死七个人只动 1 个点，另一波死三个动 9 个点，
    按人数排会把前者排第一。判「这波是不是胜负手」，只有胜率回答得了。

    **没有曲线时 —— 退回旧口径**（阵亡人数主导，参战与时长次之，
    经济净变化差越大说明结果越悬殊，后期加权）。这条兜底仍然要留着：
    插件市场装来的用户多半没有录像引擎，不能因为拿不到胜率就出不来清单。

    焦点玩家在这波团的表现一并算进去（伤害占比、击杀、是否阵亡），
    用来在候选里标出「他打得好的那波」。
    """
    fights = match.get("teamfights") or []
    players = sorted(
        [p for p in (match.get("players") or []) if isinstance(p, dict)],
        key=lambda p: int(p.get("player_slot") or 0),
    )
    try:
        total_dur = int(match.get("duration") or 0)
    except (TypeError, ValueError):
        total_dur = 0
    focus = {int(i) for i in (focus_ids or [])}

    out: list[dict] = []
    for index, fight in enumerate(fights, start=1):
        if not isinstance(fight, dict):
            continue
        entries = fight.get("players") or []
        start = int(fight.get("start") or 0)
        end = int(fight.get("end") or 0)
        duration = max(0, end - start)
        deaths = int(fight.get("deaths") or 0)

        def _int(entry: Any, key: str) -> int:
            try:
                return int((entry or {}).get(key) or 0)
            except (TypeError, ValueError):
                return 0

        gold_rad = sum(_int(p, "gold_delta") for p in entries[:5])
        gold_dire = sum(_int(p, "gold_delta") for p in entries[5:])
        engaged = sum(1 for p in entries if _int(p, "damage") > 0)
        # 这波**到底有没有逐人伤害数据** —— 用来分辨「伤害是 0」和「没有伤害字段」。
        #
        # ⚠️ 这两个意思完全不同，混起来会同时污染提示词和给用户看的清单：
        #   * OpenDota：``teamfights`` 是服务端聚合的，每条 entry **都带**
        #     ``damage`` 键（没出手就是 0）。这里的 0 是观测值。
        #   * STRATZ：``teamfights`` 是按死亡事件聚出来的（见
        #     ``dota_stratz_events.build_teamfights``），entry 从 ``{}`` 起步，
        #     只填 ``deaths`` / ``gold_delta`` / ``xp_delta`` / ``killed``，
        #     **从来没有 ``damage`` 键**。这里的 0 是「没这个字段」。
        #
        # 判据只能看「键在不在」，不能看值大不大 —— 前者问的是数据源有没有给，
        # 后者问的是他打没打，是两件事（实测 2026-11-11：主数据源是 STRATZ，
        # 于是每一波都渲染成「伤害 0（占全队 0%）」）。
        has_damage = any(
            "damage" in p for p in entries if isinstance(p, dict)
        )

        impact = curve.impact_at(index) if curve is not None else None
        if impact is not None:
            # 胜率口径。乘 10 是让「一个点」就压过阵亡人数带来的全部差异，
            # 排序完全由「这波有没有改变胜负」决定；deaths 只做同分次序。
            score = abs(impact.delta) * 10.0 + deaths * 0.3
        else:
            score = (deaths * 10.0
                     + engaged * 1.5
                     + min(duration, 60) * 0.2
                     + min(abs(gold_rad - gold_dire), 8000) / 800.0)
            if total_dur and start >= total_dur * 0.6:
                score += 3.0

        # ⚠️ 这里必须把**所有**焦点玩家都收进来，不能找到第一个就 break。
        #
        # 2026-11-10 修：原来只收第一个命中 ``focus`` 的玩家，而渲染层取名字用的
        # 是 ``focus_ids[0]`` —— 两位焦点玩家（「以我和某某为主视角」）时，
        # 名字与英雄会**点错人**：实测 ``focus_ids=[B, A]`` 输出「B · A 的英雄」。
        # 名字与英雄必须来自同一条 note，所以 note 里带上 ``account_id``。
        focus_notes: list[dict] = []
        for slot, entry in enumerate(entries):
            owner = players[slot] if slot < len(players) else {}
            try:
                owner_aid = int((owner or {}).get("account_id") or 0)
            except (TypeError, ValueError):
                owner_aid = 0
            # 匿名玩家 account_id 是 0，而 0 永远不该被当成焦点 —— 否则
            # 只要焦点集合里混进一个 0，全场的匿名玩家都会被算成焦点。
            if not owner_aid or owner_aid not in focus:
                continue
            team_entries = entries[:5] if slot < 5 else entries[5:]
            team_damage = sum(_int(p, "damage") for p in team_entries)
            damage = _int(entry, "damage")
            focus_notes.append({
                "account_id": owner_aid,
                "hero_id": owner.get("hero_id"),
                "damage": damage,
                "share": round(damage / team_damage * 100.0, 1) if team_damage else 0.0,
                # 渲染层据此决定写不写「伤害 x（占全队 y%）」。没有这个字段时
                # 写出来的 0 会被读成「他去了但一点输出没打」，而事实是数据源
                # 压根没提供逐人伤害。
                "has_damage": has_damage,
                "deaths": _int(entry, "deaths"),
                # killed 是 {npc 名: 次数} 的聚合字典，次数要累加不能只数 key：
                # 同一波里重复击杀同一个英雄（买活再死）时只数 key 会少算。
                "kills": sum(int(v or 0) for k, v in (entry.get("killed") or {}).items()
                             if str(k).startswith("npc_dota_hero_")),
                "gold_delta": _int(entry, "gold_delta"),
                # 这波到底参没参战。OpenDota 对**没到场的玩家**也补一条全零记录，
                # 原来一律渲染成「伤害 0（占全队 0%）、存活」—— 读起来像「他去了
                # 但一点输出没打」，其实是「他不在场」（2026-11-10 实测第 15 波）。
                # 判据只要「有动作」：伤害 / 施法 / 用物 / 阵亡，任一即可。
                # 不能用``healing``：被动回血、范围光环都会漏一点，实测有 6 点。
                #
                # ⚠️ 没有逐人伤害数据时（STRATZ）**不能**用 ``has_damage`` 兜底
                # 判「没到场」：那会把十个人全判成没到场（伤害键都没有 ⇒ 0）。
                # 这一类只有 ``deaths`` 是真有证据的，就只用它 —— 「这波他死了」
                # 至少能证明他在场，没死则**不作判断**（返回 None，渲染层不写状态）。
                "engaged": True if not has_damage else bool(
                    damage
                    or (entry.get("ability_uses") or {})
                    or (entry.get("item_uses") or {})
                    or _int(entry, "deaths")
                ),
                # 没有伤害数据时，「他死没死」是唯一能确定的事；死了必在场。
                "died": _int(entry, "deaths") > 0,
            })

        out.append({
            "index": index,
            "start": start,
            "end": end,
            "duration": duration,
            "deaths": deaths,
            "engaged": engaged,
            #: 这波有没有逐人伤害数据（OpenDota 有 / STRATZ 聚类没有）。
            #: 渲染层与 :func:`_fight_tag` 都靠它决定「能写什么、不能写什么」。
            "has_damage": has_damage,
            "gold_delta": gold_rad - gold_dire,
            "score": round(score, 1),
            # ``focus`` 保留「第一位焦点玩家」以兼容既有调用方与测试，
            # 渲染层一律用 ``focus_all`` —— 逐人一行，名字与英雄不会点错。
            "focus": focus_notes[0] if focus_notes else None,
            "focus_all": focus_notes,
            # 拿不到曲线时这三项全是 None，渲染层据此决定要不要显示胜率
            "win_before": round(impact.before) if impact else None,
            "win_after": round(impact.after) if impact else None,
            "win_delta": impact.shown_delta if impact else None,
        })
    return out


def pick_candidates(cands: list[dict], top_n: int = DEFAULT_TOP_N) -> list[dict]:
    """从全部团战里挑出要展示的几波，**保持原编号**。

    除了关键度最高的几波，另外单独保一个「焦点玩家高光团」：
    他没死、伤害占全队比例最高的一波。用户要的正是「我打得好的那波」，
    而它未必在团队关键度榜上（比如顺风局的一次小规模收割）。

    返回的是**副本**，每项多一个 ``pick_reason``（``"关键"`` / ``"高光"``）。
    2026-11-10 之前这个标签算出后就丢掉了 —— 高光团被挑进清单，可读者
    从清单上看不出它凭什么进来，也看不出哪一波是「打得好的那波」。
    """
    if not cands:
        return []
    picked: dict[int, str] = {}
    for c in sorted(cands, key=lambda c: -c["score"])[:top_n]:
        picked[c["index"]] = "关键"

    best: tuple[dict, float] | None = None
    for c in cands:
        # 多焦点时逐个判：任意一位「没死 + 伤害占比 ≥25%」都算高光，
        # 取占比最高的那一波。只看 ``focus``（第一位）会漏掉另一位的高光。
        for note in c.get("focus_all") or ([c["focus"]] if c.get("focus") else []):
            # ⚠️ 没有逐人伤害数据时 ``share`` 恒为 0（不是「他打得差」，
            # 而是「这个数据源不给逐波伤害」）。此时**判不出高光**，
            # 就不标 —— 宁可少一个标签，也不能凭 0 占比去挑一波夸。
            if not note.get("has_damage", True):
                continue
            if note["deaths"] == 0 and note["share"] >= 25.0:
                if best is None or note["share"] > best[1]:
                    best = (c, note["share"])
    highlight = best[0]["index"] if best is not None else 0
    if highlight:
        picked.setdefault(highlight, "高光")

    # ``focus_highlight`` 必须**独立于** ``pick_reason``：一波常常既是关键团、
    # 又是焦点玩家打得最好的一波。用 setdefault 合并进同一个字段的话，
    # 它已经以「关键」身份进了前 top_n，高光标记就永远显示不出来
    # （2026-11-10 实测：占比 67%、存活的那波，清单上什么也没标）。
    return [dict(c, pick_reason=picked[c["index"]],
                 focus_highlight=bool(highlight) and c["index"] == highlight)
            for c in cands if c["index"] in picked]


def _fight_tag(cand: dict, all_cands: list[dict]) -> str:
    """给一波团起一句短标签（只在有明显特征时给）。

    有胜率时**只说胜率**，不再提「经济差拉开多少」——
    经济差拉开和「这波是不是胜负手」是两件事，后者已经由胜率回答；
    两个口径并排出现，用户会以为它们等价。
    """
    win_delta = cand.get("win_delta")
    if win_delta is not None:
        # 只有真正排第一的那波才配「全场影响最大」—— 否则 +9 和 +8 会同时
        # 顶着这句话，读者反而看不出哪个更大。并列时取编号靠前的。
        top = max((abs(c.get("win_delta") or 0) for c in all_cands), default=0)
        leaders = [c for c in all_cands if abs(c.get("win_delta") or 0) == top]
        if leaders and abs(win_delta) >= 5:
            first = min(leaders, key=lambda c: c["index"])
            if first["index"] == cand["index"]:
                return "全场影响最大的一波"
        if abs(win_delta) >= 3:
            return "明显改变了局势"
        return ""

    # 兜底：没有胜率曲线时沿用阵亡人数 + 经济差口径
    deaths = cand["deaths"]
    max_deaths = max((c["deaths"] for c in all_cands), default=0)
    tags = []
    if max_deaths >= 4 and deaths == max_deaths:
        tags.append("全场阵亡最多")
    elif deaths >= 6:
        tags.append("大规模团战")
    delta = cand["gold_delta"]
    # ⚠️ 只有**这个数据源的经济差可信**时才敢写「哪边经济 +多少」。
    # OpenDota 的 ``gold_delta`` 是「团战期间净赚」；STRATZ 聚类团战的
    # ``gold_delta`` 是「这段里这些人阵亡掉了多少钱」（负值），方向正好相反 ——
    # 照原样渲染会把「这波团这边死了三个人」写成「这边经济 +5000」，
    # 是句反话。判据用「这波有没有逐人伤害数据」代指数据源，与渲染层同一口径。
    if abs(delta) >= 5000 and cand.get("has_damage", True):
        # 必须写清是哪边拉开的。原来只写「经济差拉开 6164」，读者无从判断
        # 这波是谁赚了 —— 顺风局里这句话可能是夸夜魇的（delta 是
        # 「天辉 − 夜魇」的净额，正数才是天辉赚）。
        tags.append("%s经济 +%d" % ("天辉" if delta > 0 else "夜魇", abs(delta)))
    return "、".join(tags)


def _focus_display_name(names: dict, account_id: Any) -> str:
    """从 ``focus_names`` 里取昵称，容忍 key 是 int 或 str。

    调用方有的传 ``{int: str}``（``main.py`` 里做过一次转换），有的直接透传
    上游的 ``{str: str}``。只按 int 查的话，后者会全体查不到、清单一律退化成
    「焦点玩家」—— 名字没了但报告照常出，属于不会报错的错。
    """
    try:
        aid = int(account_id or 0)
    except (TypeError, ValueError):
        return ""
    if not aid:
        return ""
    for key in (aid, str(aid)):
        value = names.get(key)
        if value:
            return str(value).strip()
    return ""


def render_candidates(
    match: dict,
    *,
    focus_ids: list[int] | tuple[int, ...] = (),
    focus_names: dict[int, str] | None = None,
    hero_name: Callable[[Any], str] | None = None,
    top_n: int = DEFAULT_TOP_N,
    keyword: str = "/d2",
    match_id: Any = "",
    curve: WinCurve | None = None,
) -> str:
    """生成报告末尾的候选团战清单（纯文本，直接发给用户）。

    ⚠️ **必须是 Markdown 列表结构，不能靠「每行一句」+ 缩进来排版。**
    这份文本会被拼进复盘正文，而正文在 QQ 里是**纯文本**、在图片里是
    **Markdown 渲染**。前者的换行能看见，后者的单换行会被折叠成空格 ——
    于是同一份清单在聊天里还好，一转图就挤成一大坨（2026-11-05 用户截图）：

        · 第 3 波 08:01–08:51 · 阵亡 6 · 全场阵亡最多 某某 在这波：…

    只有 ``- `` / 缩进 ``  - `` 这种真正的列表块，两种渲染下都保证分行，
    且图片里还会自动带项目符号与缩进层级。
    """
    cands = score_fights(match, focus_ids, curve)
    if not cands:
        return ""
    chosen = pick_candidates(cands, top_n)
    if not chosen:
        return ""

    names = focus_names or {}
    focus_aid = int(next(iter(focus_ids), 0) or 0) if focus_ids else 0
    who = _focus_display_name(names, focus_aid) or ("焦点玩家" if focus_aid else "")

    # 标题前先来一条分隔线，原因见下；标题后空一行，否则部分 Markdown
    # 实现会把「🎯 …」和第一项并成同一个段落。
    #
    # ⚠️ 分隔线不只是好看：清单是接在复盘正文**末尾**的，而正文经常以
    # 「1. …\n2. …」这种有序列表收尾。只有一个空行时，渲染出来跟上面的
    # 列表几乎贴在一起（图片里看着就像第 3 个列表项），反而更乱
    # （2026-11-05 第一次改完的实拍效果）。``---`` 会被渲染成一条真正的
    # 横线，两种渲染下都能明确「下面是另一段东西」。
    lines = ["", "---", "", "本场值得单独深挖的团战（录像级复盘）", ""]
    for cand in chosen:
        head = "第 %d 波 %s–%s · 阵亡 %d" % (
            cand["index"], _clock(cand["start"]), _clock(cand["end"]), cand["deaths"])
        # 有胜率就把「这波改变了多少」摆在主行 —— 它比阵亡人数更能说明
        # 这波为什么值得点开；没有就维持原来的样子。
        if cand.get("win_delta") is not None:
            head += " · 胜率 %d%%→%d%%（%+d）" % (
                cand["win_before"], cand["win_after"], cand["win_delta"])
        tag = _fight_tag(cand, cands)
        if tag:
            head += " · " + tag
        # 「高光」是单独特批进来的（未必在关键度榜上），不标出来读者会以为
        # 它排错位置了；它也可能是「关键」那一波，两种身份并存。
        if cand.get("focus_highlight"):
            head += " · 焦点玩家打得最好的一波"
        lines.append("- " + head)
        # 一位焦点玩家一行。名字与英雄**必须**取自同一条 note：多焦点时
        # 拿 focus_ids[0] 的名字去配另一位玩家的英雄，就是张冠李戴
        # （2026-11-10 实测：focus_ids=[B, A] 打出「B · A 的英雄」）。
        for note in (cand.get("focus_all")
                     or ([cand["focus"]] if cand.get("focus") else [])):
            # 查不到名字（调用方没给 focus_names、或匿名玩家）时兜底，
            # 不能留空 —— 空名字会渲染成「  -  · 英雄84：…」这种断头行。
            label = _focus_display_name(names, note.get("account_id"))
            label = label or who or "焦点玩家"
            if hero_name:
                hero = hero_name(note.get("hero_id"))
            else:
                hero = "本命"
            # ---- 没有逐人伤害数据时：只说「能确定的事」----
            #
            # ⚠️ 绝不能写「伤害 0（占全队 0%）」。那是把「数据源没给这个字段」
            # 渲染成了「他打了，但一点输出没有」—— 同一个 0，两种事实，
            # 模型和用户都会当真（实测 2026-11-11：主数据源 STRATZ 时每一波
            # 都长这样，用户直接问「为什么都显示伤害 0」）。
            #
            # 这一类（STRATZ 事件聚类）本来就只有 ``deaths`` / ``killed``
            # 是**真有证据**的，见 dota_stratz_events 模块头的三条铁律：
            # 「缺失返回空，不返回 0」。这里照着那条铁律办。
            if not note.get("has_damage", True):
                bits = []
                if note.get("kills"):
                    bits.append("击杀 %d" % note["kills"])
                if note.get("died"):
                    bits.append("阵亡")
                text = "、".join(bits) if bits else "本波无逐人数据"
                lines.append("  - %s · %s：%s" % (label, hero, text))
                continue
            # 没到场的人不要写成「伤害 0、存活」—— 那是「去了没打」的意思。
            # 「没给 / 真是 0 / 没做」是三件事，这里属于「没做」。
            if not note.get("engaged", True):
                lines.append("  - %s · %s：这波没到场" % (label, hero))
                continue
            bits = ["伤害 %d（占全队 %.0f%%）" % (note["damage"], note["share"])]
            if note["kills"]:
                bits.append("击杀 %d" % note["kills"])
            bits.append("阵亡" if note["deaths"] else "存活")
            # 子项用两空格缩进 + `- `：Markdown 的嵌套列表，渲染成缩进一层的
            # 次级条目；聊天里也是一行一条，比「名字 在这波：英雄 伤害…」
            # 这种一行到底的写法容易扫读得多。
            #
            # 名字与英雄用「 · 」隔开而不是「名字（英雄）」：后面还有
            # 「伤害 1925（占全队 36%）」，括号套括号读起来很绕。
            lines.append("  - %s · %s：%s" % (label, hero, "、".join(bits)))

    lines.append("")
    mid = str(match_id or "")
    example = "%s 团战 %s %d" % (keyword, mid, chosen[0]["index"])
    lines.append("想看某一波的录像级细节（技能命中、控了多久、血量与视野），回复：")
    lines.append("")
    # ⚠️ 命令**不要**包反引号（Markdown 代码样式）。本地渲染兜底时
    # 等宽字体没有中文字形，「团战」两个字会变成豆腐块 —— 变成
    # `/d2 ⯑⯑ 9073115303 4`，用户连抄都抄不了（2026-11-05 实拍）。
    # 网络渲染没事、本地兜底就废，而兜底恰恰在网络端点不可用时发生。
    lines.append(example)
    return "\n".join(lines)


# ======================================================================
# 单线队列
# ======================================================================


def _fight_cast(
    match: dict,
    impact: FightImpact,
    hero_name: Callable[[Any], str] | None,
) -> list[str]:
    """这波团里每队干了什么 —— 归因要的是角色，不是总分。

    一行一队，只给「伤害占全队比例 / 是否阵亡 / 是否买活」，
    因为判「他在这一波是什么角色」只需要这三样：
    输出位伤害是 0，和坦克伤害是 0，完全是两件事。

    ⚠️ **没有逐人伤害数据时，一个百分比都不许写。** 这一段是直接拼进
    **提示词**的，写出来的每个数字模型都会当证据用。原来对 STRATZ
    （按死亡事件聚类的团战，entry 里没有 ``damage`` 键）会渲染成
    「天辉：敌法师 0% ｜ 某某 0% ｜ …」，模型读到的就是
    「十个人全程零输出」—— 于是一边把真实失职判成团队问题，
    一边又凭空给所有人扣一条「这波没打出伤害」。这不是少给信息，
    是给了错的信息。所以这里退回「只报伤亡」，与
    :func:`dota_format._render_fight_lines` 的 ``_clustered`` 分支同一口径。
    """
    fights = match.get("teamfights") or []
    if not (1 <= impact.index <= len(fights)):
        return []
    entries = (fights[impact.index - 1] or {}).get("players") or []
    players = sorted(
        [p for p in (match.get("players") or []) if isinstance(p, dict)],
        key=lambda p: int(p.get("player_slot") or 0),
    )
    # 判据只能看「键在不在」，不能看值大不大 —— 见 score_fights 里的同款说明。
    has_damage = any("damage" in p for p in entries if isinstance(p, dict))

    out: list[str] = []
    for label, rng in (("天辉", range(0, 5)), ("夜魇", range(5, 10))):
        team_damage = 0
        for i in rng:
            if i < len(entries):
                try:
                    team_damage += int((entries[i] or {}).get("damage") or 0)
                except (TypeError, ValueError):
                    pass
        bits = []
        for i in rng:
            if i >= len(entries):
                continue
            entry = entries[i] or {}
            owner = players[i] if i < len(players) else {}
            name = hero_name(owner.get("hero_id")) if hero_name else ""
            if not name:
                name = "选手%d" % (i + 1)
            try:
                damage = int(entry.get("damage") or 0)
                deaths = int(entry.get("deaths") or 0)
                buybacks = int(entry.get("buybacks") or 0)
            except (TypeError, ValueError):
                damage, deaths, buybacks = 0, 0, 0
            share = damage / team_damage * 100.0 if team_damage else 0.0
            tail = ""
            if deaths:
                tail += " 死"
            if buybacks:
                tail += " 买活"
            if has_damage:
                bits.append("%s %.0f%%%s" % (name, share, tail))
            else:
                # 没有伤害数据：只报确定的事。没死也没买活的人不占位，
                # 否则会变成「XXX  ｜ YYY  ｜ ZZZ」一串空壳。
                if tail:
                    bits.append(name + tail)
        if bits:
            out.append("    %s：%s" % (label, " ｜ ".join(bits)))
        elif not has_damage:
            # 整队都没有可报的动作：如实写一句，别让模型以为「这一段忘了写」。
            out.append("    %s：这波无逐人伤亡记录" % label)
    return out


def _opening_verdict(chance: float) -> str:
    """把阵容先验胜率翻成一句人话。

    ⚠️ **分档必须贴在 50 附近，不能按「60% 才算占优」来切。**
    Valve 给的是双方阵容的胜率，实际分布极窄：53~54 已经是「阵容优势很大」，
    51 也算「占优」，55 以上基本见不到，60 以上几乎不存在
    （2026-11-05 用户指出）。按常识的 60/40 分档会把**每一局**都判成五五开。
    """
    if chance >= 55:
        return "阵容压倒性占优"
    if chance >= 53:
        return "阵容优势很大"
    if chance >= 51:
        return "阵容占优"
    if chance > 49:
        return "阵容基本五五开"
    if chance > 47:
        return "阵容略吃亏"
    if chance > 45:
        return "阵容明显吃亏"
    return "阵容非常吃亏"


def render_win_curve(
    curve: WinCurve,
    match: dict,
    *,
    hero_name: Callable[[Any], str] | None = None,
    top_n: int = 3,
    keyword: str = "/d2",
    match_id: Any = "",
) -> str:
    """生成「胜负走势」文本块，拼进复盘正文（图片版里也有）。

    这块是给**模型**读的，所以每条都要带上「所以呢」：
    只给一串胜率数字，模型只会复述数字；把「死得多不等于影响大」
    摆成对照，它才会用胜率而不是死亡人数去判关键团。
    """
    if not curve.ok:
        return ""

    lines = ["", "---", "", "胜负走势（Valve 官方胜率，非人工估算）", ""]

    # 曲线摘要：按整分钟抽样，让模型看到形状而不是 60 个数字。
    sampled: list[str] = []
    last_minute = -1
    for t, v in curve.points:
        minute = int(t // 60)
        if minute == last_minute or t < 0:
            continue
        last_minute = minute
        sampled.append("%s %d%%" % (_clock(t), round(v)))
    if sampled:
        lines.append("- 整局：%s" % " · ".join(sampled))
        lines.append("")

    if curve.opening is not None:
        lines.append("- 阵容先验：开局天辉 %d%%（%s）· 全程最高 %d%% / 最低 %d%%" % (
            round(curve.opening), _opening_verdict(curve.opening),
            round(curve.peak), round(curve.trough),
        ))
        lines.append(
            "  开局胜率是 Valve 按双方阵容算的，不含任何操作。"
            "**归因时先把这段扣掉**：开局就占优的最后赢了，未必是打得好；"
            "开局吃亏的最后输了，也未必是打得差 —— Dota 里选完人就已经很难打的局很常见。"
            "真正属于「人」的部分，是曲线从这条基线出发之后往哪边走了多少。"
        )
        lines.append("")

    ranked = curve.turning_points(top_n)
    if ranked:
        lines.append("按胜率影响排序的关键转折点（与阵亡人数无关）：")
        for impact in ranked:
            shown = impact.shown_delta
            sign = "+" if shown > 0 else ""
            lines.append("- 第 %d 波 %s · 阵亡 %d · 胜率 %d%% → %d%%（%s%d）" % (
                impact.index, impact.span, impact.deaths,
                round(impact.before), round(impact.after), sign, shown,
            ))
            lines.extend(_fight_cast(match, impact, hero_name))

        # 把「死得多但没影响」的那波单独点出来：这是模型最容易判错的地方。
        crowded = [f for f in curve.fights
                   if f.deaths >= 4 and f not in ranked and abs(f.delta) <= 2]
        if crowded:
            worst = max(crowded, key=lambda f: f.deaths)
            lines.append("- 对照：第 %d 波 %s 死了 %d 个人，胜率只从 %d%% 到 %d%%（%+d）——"
                         " 死亡多不等于影响大，判关键团请以胜率变化为准。" % (
                             worst.index, worst.span, worst.deaths,
                             round(worst.before), round(worst.after), worst.shown_delta,
                         ))
        lines.append("")

    if curve.swings:
        lines.append("没有团战、但胜率明显变化的时刻（推塔 / 肉山 / 关键装备成型）：")
        for swing in sorted(curve.swings, key=lambda s: -abs(s["delta"]))[:3]:
            lines.append("- %s 前后 · 胜率 %d%% → %d%%（%+d）" % (
                _clock(swing["to"]), round(swing["before"]),
                round(swing["after"]), round(swing["delta"]),
            ))
        lines.append("")

    if keyword:
        lines.append("想看某一波的逐秒细节（谁先死、血量怎么掉、有没有视野），"
                     "回复「%s 团战 %s <第N波>」。" % (keyword, match_id or "<比赛ID>"))
    return "\n".join(lines)


# ======================================================================
# 责任窗口：把「全队胜率曲线」拆到「哪个窗口、哪些人」
# ======================================================================
#
# 曲线只回答「什么时候局势变了」，不回答「是谁干的」。判最佳最差要的是后者，
# 所以这里把两件事拼起来：
#
#   1. 每个窗口列出**全员**的伤害占比与存活状态（``_fight_cast`` 已是全员）；
#   2. 每次阵亡带上**击杀者**（OpenDota 的 ``deaths_log`` 直接给 ``key``）；
#   3. 团战之外那些「一次被抓就把胜率打下去」的死亡单独列一节。
#
# 位置权重：同样是 12 个点，在 50% 附近是翻盘，在 95% 附近只是垃圾时间。
# 不区分这两者，模型会把「领先 95% 时被反打一波」和「五五开时被打崩一波」
# 判成同样的责任 —— 2026-11-09 按旧提示词跑出的「天辉最差是撼地者」
# 就是这个错：那波 −12 发生在 95%→83%，胜负从没悬起来过。

#: 位置权重的保底系数。
#:
#: 完全按「距 50% 的远近」线性衰减会让 99%→90% 直接归零，但它在数据上
#: 依然真实发生。给 0.35 的保底：保留三分之一的分量，又不与真正的胜负手并列。
STAKE_FLOOR = 0.35

#: 权重分档线（单位是「权重分」，不是百分点）。
STAKE_DECISIVE = 10.0
STAKE_MINOR = 4.0

#: 团战之外的关键死亡：死者一方胜率至少掉这么多点才算「关键」。
DEATH_DROP_MIN = 6.0

#: 死亡后看多少秒的胜率变化。太短会漏掉「死了之后接着被推塔」的连锁，
#: 太长会把下一波团算进来。
DEATH_GLANCE = 30.0


def stake_score(delta: float, before: float, after: float) -> float:
    """这次胜率变化对最终胜负的**实际影响**分。

    Args:
        delta: 胜率变化（百分点，带符号）。
        before: 变化前的胜率（0~100）。
        after: 变化后的胜率（0~100）。

    Returns:
        非负的权重分。``|delta|`` 越大、越靠近 50%，分越高 ——
        在 95% 上掉 12 个点只拿 6.8 分，在 63% 上掉 15 个点拿 12.4 分。
    """
    edge = min(abs(float(before) - 50.0), abs(float(after) - 50.0))
    closeness = max(0.0, 1.0 - edge / 50.0)
    return abs(float(delta)) * (STAKE_FLOOR + (1.0 - STAKE_FLOOR) * closeness)


def stake_label(score: float) -> str:
    """把权重分翻成给模型看的三档标签。"""
    if score >= STAKE_DECISIVE:
        return "决定性"
    if score >= STAKE_MINOR:
        return "影响有限"
    return "基本无关"


def npc_label(raw: str) -> str:
    """npc 名 → 可读名。英雄走本地化表，非英雄单位归成类别。

    实测 ``deaths_log`` 的 ``key`` 不一定是英雄：野区被抓会被记成
    ``npc_dota_miniboss``、越塔会记成 ``npc_dota_badguys_tower2_mid``。
    原样输出会让模型读到一串 ``npc dota miniboss``，既难看也容易被误读成玩家。
    """
    text = str(raw or "").strip()
    if not text:
        return "?"
    low = text.lower()
    if "tower" in low:
        return "防御塔"
    if "fort" in low or "fountain" in low:
        return "基地"
    if "roshan" in low:
        return "肉山"
    if "miniboss" in low or "neutral" in low or "creep" in low:
        return "野怪"
    return text.replace("npc_dota_hero_", "").replace("_", " ")


def _collect_deaths(
    match: dict,
    npc_text: Callable[[str], str],
) -> list[dict]:
    """把 ``deaths_log`` 摊平成按时间排序的统一列表。

    Returns:
        ``[{"t": 秒, "i": 玩家下标, "radiant": bool, "killer": 名字}, ...]``
    """
    players = sorted(
        [p for p in (match.get("players") or []) if isinstance(p, dict)],
        key=lambda p: int(p.get("player_slot") or 0),
    )
    out: list[dict] = []
    for i, player in enumerate(players):
        for item in player.get("deaths_log") or []:
            if not isinstance(item, dict):
                continue
            try:
                when = float(item.get("time") or 0)
            except (TypeError, ValueError):
                continue
            out.append({
                "t": when,
                "i": i,
                "radiant": int(player.get("player_slot") or 0) < 128,
                "killer": npc_text(str(item.get("key") or "")),
            })
    out.sort(key=lambda d: d["t"])
    return out


def key_deaths(
    curve: WinCurve,
    match: dict,
    *,
    npc_text: Callable[[str], str] | None = None,
    top_n: int = 4,
) -> list[dict]:
    """团战之外、把胜率明显打下去的那几次阵亡（单抓 / 被抓 / 独自送掉）。

    Args:
        curve: 已对齐的胜率曲线。
        match: 比赛数据，需要有 ``players[].deaths_log``。
        npc_text: npc 名 → 可读英雄名的转换器。
        top_n: 最多给几条。

    Returns:
        每条含 ``t`` / ``i`` / ``killer`` / ``own_before`` / ``own_after`` /
        ``drop``（死者一方视角，负数为掉点）/ ``stake`` / ``label``。
        **按 ``stake`` 从大到小排 —— 不是按掉点大小**：在 25% 上掉 13 个点，
        未必比在 79% 上掉 7 个点更改变胜负，得看它离 50% 有多远。
    """
    if not curve.ok:
        return []
    convert = npc_text or npc_label
    windows = [(f.start - 5, f.end + 5) for f in curve.fights]

    picked: list[dict] = []
    for death in _collect_deaths(match, convert):
        when = death["t"]
        if any(lo <= when <= hi for lo, hi in windows):
            continue
        before = curve.chance_at(when - 5)
        after = curve.chance_at(when + DEATH_GLANCE)
        # 换算到**死者这一方**的视角：天辉死看天辉胜率，夜魇死看夜魇胜率。
        own_before = before if death["radiant"] else 100.0 - before
        own_after = after if death["radiant"] else 100.0 - after
        drop = round(own_after - own_before, 1)
        if drop > -DEATH_DROP_MIN:
            continue
        score = stake_score(drop, own_before, own_after)
        picked.append({**death, "own_before": own_before,
                       "own_after": own_after, "drop": drop,
                       "stake": score, "label": stake_label(score)})

    picked.sort(key=lambda d: -d["stake"])
    return picked[:top_n]


def render_responsibility_block(
    curve: WinCurve,
    match: dict,
    *,
    hero_name: Callable[[Any], str] | None = None,
    npc_text: Callable[[str], str] | None = None,
    top_n: int = 4,
) -> str:
    """生成「责任窗口」文本块：全队曲线 → 窗口 → 人。

    Args:
        curve: 已对齐的胜率曲线。
        match: 比赛数据。
        hero_name: ``hero_id → 名字``，用于逐人行的显示。
        npc_text: ``npc_dota_hero_xxx → 名字``，用于击杀者。
        top_n: 渲染几个窗口（按权重分排序，**不是按时间**）。

    Returns:
        可直接拼进提示词的文本；曲线无效时返回空串。
    """
    if not curve.ok:
        return ""

    players = sorted(
        [p for p in (match.get("players") or []) if isinstance(p, dict)],
        key=lambda p: int(p.get("player_slot") or 0),
    )
    if not players:
        return ""
    convert = npc_text or npc_label

    def who(index: int) -> str:
        if index >= len(players):
            return "选手%d" % (index + 1)
        name = hero_name(players[index].get("hero_id")) if hero_name else ""
        return name or "选手%d" % (index + 1)

    deaths = _collect_deaths(match, convert)

    lines = ["", "---", "",
             "责任窗口（按**对胜负的实际影响**排序，不是按阵亡人数）", ""]
    lines.append(
        "权重口径：|胜率变化| × 距 50% 的接近程度。同样十几个点，"
        "在五五开附近是翻盘，在九成以上只是垃圾时间 —— "
        "**判「最差」有两条通路**：① 有至少一个【决定性】窗口作证；"
        "② 判出「贯穿型失职」（一个位置从头到尾没干过自己岗位该干的事，"
        "比如一号位整局没有输出端、辅助整局没有视野）—— 这类失败**本来就不会**"
        "落在任何单一窗口上，所以「找不到那个决定性窗口」**不等于**他没责任。"
        "只有【影响有限】的窗口"
        "最多算过程瑕疵；【基本无关】的窗口一个字都不许用来说谁打得差。"
    )

    scored = []
    for fight in curve.fights:
        score = stake_score(fight.delta, fight.before, fight.after)
        scored.append((score, fight))
    scored.sort(key=lambda pair: -pair[0])

    for score, fight in scored[:top_n]:
        lines.append("")
        lines.append("【%s】第 %d 波 %s · 天辉胜率 %d%% → %d%%（%+d）· 阵亡 %d" % (
            stake_label(score), fight.index, fight.span,
            round(fight.before), round(fight.after), fight.shown_delta, fight.deaths,
        ))
        lines.extend(_fight_cast(match, fight, hero_name))
        inside = [d for d in deaths
                  if fight.start - 2 <= d["t"] <= fight.end + 2]
        if inside:
            lines.append("    这段内阵亡：" + " ｜ ".join(
                "%s %s 被 %s 击杀" % (_clock(d["t"]), who(d["i"]), d["killer"])
                for d in inside
            ))

    singles = key_deaths(curve, match, npc_text=convert)
    if singles:
        lines.append("")
        lines.append("团战之外把胜率打下去的阵亡（单抓 / 被抓 / 独自送掉）：")
        for death in singles:
            lines.append(
                "- 【%s】%s %s 被 %s 击杀 · %s胜率 %d%% → %d%%（%+d）" % (
                    death["label"], _clock(death["t"]), who(death["i"]), death["killer"],
                    "天辉" if death["radiant"] else "夜魇",
                    round(death["own_before"]), round(death["own_after"]),
                    round(death["drop"]),
                )
            )
        lines.append(
            "  （这类死亡责任最清楚：没有团战、没有队友，就是他一个人被抓住。"
            "但**责任清楚不等于影响大** —— 权重档位同样要看它发生在什么局势下。）"
        )

    return "\n".join(lines)


class QueueFull(Exception):
    """排队的人太多，直接拒绝而不是无限堆。"""


class ReplayQueue:
    """**单消费者**队列：同一时刻只会有一个解析任务在跑。

    实现刻意不用 ``asyncio.Semaphore`` —— 信号量允许 N 个任务同时进入，
    这里要的是「一个接一个」，并且还要能回答「前面还有几个」。
    """

    def __init__(self, max_queue: int = MAX_QUEUE) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._max = max_queue
        self._current: str = ""
        self._running = 0

    @property
    def current(self) -> str:
        """正在跑的任务标签（空字符串表示空闲）。"""
        return self._current

    def pending(self) -> int:
        """还没开始跑的任务数（不含正在跑的那个）。"""
        return self._queue.qsize()

    def status_text(self) -> str:
        if self._current:
            extra = f"，后面还有 {self.pending()} 个" if self.pending() else ""
            return f"当前正在解析：{self._current}{extra}"
        return "解析队列空闲"

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._loop())

    async def run(self, label: str, fn: Callable[[], Awaitable[T]]) -> T:
        """把一个任务排进队列，等它**轮到自己**并跑完，返回结果。

        ``fn`` 是个零参协程工厂：调用时已经开始执行，所以下载/解析要写在
        工厂内部，不能提前创建协程（提前创建会在排队期间就「预热」起来）。
        """
        if self._queue.qsize() >= self._max:
            raise QueueFull(f"解析队列已满（最多排 {self._max} 个）")
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        await self._queue.put((label, fn, future))
        self._ensure_worker()
        return await future

    async def _loop(self) -> None:
        while True:
            label, fn, future = await self._queue.get()
            self._current = label
            self._running += 1
            try:
                if future.cancelled():
                    continue
                result = await fn()
                if not future.done():
                    future.set_result(result)
            except asyncio.CancelledError:
                if not future.done():
                    future.cancel()
                raise
            except Exception as exc:  # noqa: BLE001 - 单个任务失败不能打死 worker
                if not future.done():
                    future.set_exception(exc)
            finally:
                self._running -= 1
                self._current = ""
                self._queue.task_done()

    async def stop(self) -> None:
        """插件卸载时调用：取消 worker，并把还在排队的任务一并取消。"""
        worker = self._worker
        self._worker = None
        if worker is not None and not worker.done():
            worker.cancel()
            try:
                await worker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        while not self._queue.empty():
            try:
                _label, _fn, future = self._queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - 竞态兜底
                break
            if not future.done():
                future.cancel()
            self._queue.task_done()


def parse_fight_reference(text: str) -> int | None:
    """把用户说的「第 7 波 / 7 / 第7波团战」解析成团战序号。

    只接受**纯数字**或带「波 / 团」的说法。刻意宽松解析中文数字以外的形式，
    但拒绝「上次那波」这类指代 —— 那种要模型结合上下文判断，不该在这里猜。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    digits = ""
    for ch in raw:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    if not digits:
        return None
    try:
        value = int(digits)
    except ValueError:
        return None
    if 1 <= value <= 60:
        return value
    return None
