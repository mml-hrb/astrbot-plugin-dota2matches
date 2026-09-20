"""把 OpenDota 的原始数据整理成「给人看的文本」与「给大模型看的结构化文本」。

本模块不依赖 AstrBot，纯函数式，方便单独测试。
"""

from __future__ import annotations

import datetime
import time
from collections import Counter
from typing import Any

# ----------------------------------------------------------------------
# 常量映射
# ----------------------------------------------------------------------

GAME_MODES: dict[int, str] = {
    0: "Unknown",
    1: "All Pick",
    2: "Captains Mode",
    3: "Random Draft",
    4: "Single Draft",
    5: "All Random",
    6: "Intro",
    7: "Diretide",
    8: "Reverse Captains Mode",
    9: "Greeviling",
    10: "Tutorial",
    11: "Mid Only",
    12: "Least Played",
    13: "Limited Heroes",
    14: "Compendium Matchmaking",
    15: "Custom",
    16: "Captains Draft",
    17: "Balanced Draft",
    18: "Ability Draft",
    19: "Event",
    20: "All Random Death Match",
    21: "1v1 Mid",
    22: "All Pick",
    23: "Turbo",
    24: "Mutation",
}

LOBBY_TYPES: dict[int, str] = {
    -1: "Invalid",
    0: "Normal",
    1: "Practice",
    2: "Tournament",
    3: "Tutorial",
    4: "Co-op vs Bots",
    5: "Team Match",
    6: "Solo Mid",
    7: "Ranked",
    8: "1v1 Mid",
    9: "Battle Cup",
}

LANE_ROLES: dict[int, str] = {
    1: "优势路(Safe Lane)",
    2: "中路(Mid)",
    3: "劣势路(Off Lane)",
    4: "野区(Jungle)",
}

#: STRATZ 的 ``position`` 枚举 → 中文「几号位」。
#: OpenDota 侧没有这个字段（只有 lane_role），但 STRATZ 直连时每个玩家都带，
#: 是判断阵容分工最直接的依据，因此这里补一张映射表。
POSITION_TEXT: dict[str, str] = {
    "POSITION_1": "1号位（核心/优势路）",
    "POSITION_2": "2号位（中单）",
    "POSITION_3": "3号位（劣势路）",
    "POSITION_4": "4号位（游走/打架辅助）",
    "POSITION_5": "5号位（保人辅助）",
}

RANK_MEDALS: list[str] = [
    "无段位",
    "先锋 Herald",
    "守卫 Guardian",
    "十字军 Crusader",
    "执政官 Archon",
    "传奇 Legend",
    "万古流芳 Ancient",
    "神圣 Divine",
    "不朽 Immortal",
]

OBJECTIVE_HINTS: dict[str, str] = {
    "CHAT_MESSAGE_ROSHAN_KILL": "肉山被击杀",
    "CHAT_MESSAGE_AEGIS": "拾取不朽之守护",
    "CHAT_MESSAGE_AEGIS_STOLEN": "抢到不朽之守护",
    "CHAT_MESSAGE_AEGIS_DENIED": "不朽之守护被打断",
    "CHAT_MESSAGE_COURIER_LOST": "信使被击杀",
    "CHAT_MESSAGE_FIRSTBLOOD": "一血",
    "CHAT_MESSAGE_BUYBACK": "买活",
    "CHAT_MESSAGE_MINIBOSS_KILL": "击杀小型首领",
    "CHAT_MESSAGE_GLYPH_USED": "使用防御符文",
}

BUILDING_HINTS: list[tuple[str, str]] = [
    ("tower1", "一塔"),
    ("tower2", "二塔"),
    ("tower3", "三塔"),
    ("tower4", "四塔"),
    ("fort", "基地"),
    # 键名取自 OpenDota objectives 的真实 key：``..._melee_rax_bot`` /
    # ``..._range_rax_bot``（顺序不能与下面通用的 "rax" 对调，否则会被吃掉）
    ("melee_rax", "近战兵营"),
    ("range_rax", "远程兵营"),
    ("rax", "兵营"),
    ("shrine", "圣坛"),
    ("fillers", "建筑"),
]

LANE_HINTS: list[tuple[str, str]] = [
    ("top", "上路"),
    ("mid", "中路"),
    ("bot", "下路"),
]

#: OpenDota ``gold_reasons`` 的语义。
#:
#: 口径取自 Valve 官方枚举 ``EDOTA_ModifyGold_Reason``（不是网上流传的
#: YASP 旧表——旧表把 14 写成 Roshan、15 写成 Courier，与现行引擎对不上：
#: 实测拿野怪击杀数与 key 14 相除恒为 50~70 金币/只，key 17 则同队五人完全
#: 相等，正是赏金符的「全队均分」特征）。这张表是「金币从哪来」的分解：
#: 补刀占比高 = 刷子型；击杀英雄占比高 = 打架型；拆建筑占比高 = 推进型。
GOLD_REASONS: dict[int, str] = {
    0: "其他/初始金",
    1: "死亡损失",
    2: "买活支出",
    3: "购买消耗品",
    4: "购买装备",
    5: "队友放弃后分配",
    6: "卖出物品",
    7: "技能耗金",
    10: "随时间自然增长",
    11: "拆建筑",
    12: "击杀英雄",
    13: "补刀小兵",
    14: "击杀野怪",
    15: "击杀肉山",
    16: "击杀信使",
    17: "赏金符",
    18: "团队共享",
    19: "技能产金",
    20: "击杀守卫",
}

#: OpenDota ``xp_reasons`` 的语义（Valve 官方枚举 ``EDOTA_ModifyXP_Reason``）。
XP_REASONS: dict[int, str] = {
    0: "其他",
    1: "击杀英雄",
    2: "补刀/野怪",
    3: "肉山",
    4: "经验书",
    5: "前哨站",
}

#: ``multi_kills`` 的键（连杀人数）→ 中文。
MULTI_KILL_TEXT: dict[int, str] = {
    2: "双杀",
    3: "三杀",
    4: "四杀",
    5: "暴走",
    6: "6 连杀以上",
}

#: ``life_state`` 的键 → 中文语义。实测三者之和恒等于比赛时长，且键 ``2``
#: 的数值随死亡次数单调增长（11 死 → 433s、4 死 → 168s），因此 ``2`` 是
#: 「阵亡 + 复活等待」。
LIFE_STATE_TEXT: dict[int, str] = {
    0: "存活",
    1: "其他状态",
    2: "阵亡/复活等待",
}

#: 视野类日志的字段名 → 中文，用于统一渲染
WARD_LOGS: list[tuple[str, str, str]] = [
    ("obs_log", "假眼", "obs_left_log"),
    ("sen_log", "真眼", "sen_left_log"),
]


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
def fmt_duration(seconds: Any) -> str:
    """把秒数格式化成 ``mm:ss`` / ``h:mm:ss``。"""
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "??:??"
    if total < 0:
        return "??:??"
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes}:{sec:02d}"


def fmt_clock(seconds: Any) -> str:
    """把「距比赛开始 N 秒」格式化成 ``mm:ss``（负数返回 ``-`` ）。"""
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "-"
    if total < 0:
        return f"赛前 {abs(total)}s"
    return fmt_duration(total)


def fmt_timestamp(ts: Any) -> str:
    """Unix 时间戳 → 本地时间字符串。"""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except (TypeError, ValueError, OSError):
        return "未知时间"


def fmt_ago(ts: Any) -> str:
    """Unix 时间戳 → ``3 小时前``。"""
    try:
        delta = int(time.time()) - int(ts)
    except (TypeError, ValueError):
        return ""
    if delta < 0:
        return "刚刚"
    if delta < 60:
        return f"{delta} 秒前"
    if delta < 3600:
        return f"{delta // 60} 分钟前"
    if delta < 86400:
        return f"{delta // 3600} 小时前"
    return f"{delta // 86400} 天前"


def fmt_num(value: Any, default: str = "-") -> str:
    """整数千分位格式化。"""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return default


def fmt_float(value: Any, digits: int = 2, default: str = "-") -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return default


def fmt_k(value: Any) -> str:
    """把大数字缩写成 ``12.3k``。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "-"
    if abs(num) >= 1000:
        return f"{num / 1000:.1f}k"
    return f"{num:.0f}"


def hname(heroes: dict[int, dict], hero_id: Any) -> str:
    """hero_id → 英雄名。"""
    try:
        hid = int(hero_id)
    except (TypeError, ValueError):
        return "未知英雄"
    info = heroes.get(hid) if heroes else None
    if not info:
        return f"英雄#{hid}"
    return info.get("localized_name") or f"英雄#{hid}"


def hname_by_npc(heroes: dict[int, dict], npc_key: Any) -> str:
    """``npc_dota_hero_xxx`` → 英雄名（``killed`` 字段用的是这种内部名）。"""
    raw = str(npc_key)
    if raw.startswith("npc_dota_hero_"):
        raw = raw[len("npc_dota_hero_"):]
    for info in (heroes or {}).values():
        if not isinstance(info, dict):
            continue
        name = info.get("name")
        if isinstance(name, str) and name == f"npc_dota_hero_{raw}":
            return info.get("localized_name") or raw
    # 常量表拿不到时，退化成可读的短名
    return raw.replace("_", " ").title()


class ItemIndex:
    """道具常量查询器。

    单场比赛里 ``item_0`` ~ ``item_5``、``item_neutral`` 存的是道具**数字 ID**，
    而出装日志 ``purchase_log`` 里存的是道具 **key**（如 ``blink``），
    这里统一把两种写法都解析成道具信息。
    """

    def __init__(self, items: dict[str, dict] | None = None) -> None:
        self._by_key: dict[str, dict] = {}
        self._by_id: dict[int, dict] = {}
        for key, value in (items or {}).items():
            if not isinstance(value, dict):
                continue
            self._by_key[str(key)] = value
            try:
                self._by_id[int(value.get("id"))] = value
            except (TypeError, ValueError):
                continue

    def get(self, value: Any) -> dict | None:
        """按 key 或数字 ID 查询道具信息。"""
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return self._by_id.get(value) if value > 0 else None
        text = str(value).strip()
        if not text or text in ("0", "null", "None"):
            return None
        info = self._by_key.get(text)
        if info is not None:
            return info
        if text.isdigit():
            return self._by_id.get(int(text))
        return None

    def name(self, value: Any, default: str = "-") -> str:
        """道具显示名。"""
        info = self.get(value)
        if not info:
            return default
        return info.get("dname") or info.get("name") or default

    def cost(self, value: Any) -> int:
        """道具价格。"""
        info = self.get(value)
        if not info:
            return 0
        try:
            return int(info.get("cost") or 0)
        except (TypeError, ValueError):
            return 0


#: 兼容旧调用方的两个薄封装
def imname(items: dict[str, dict], key: Any) -> str:
    """道具 key / ID → 道具显示名（无索引缓存的简化版）。"""
    return ItemIndex(items).name(key, default=str(key))


def item_cost(items: dict[str, dict], key: Any) -> int:
    """道具 key / ID → 道具价格（无索引缓存的简化版）。"""
    return ItemIndex(items).cost(key)


def is_radiant(player_slot: Any) -> bool:
    """player_slot < 128 表示天辉。

    ``None`` / 无法解析时**默认 True** —— 这是 OpenDota 的历史约定（缺失 slot
    的多是单人视角数据，按天辉处理）。但要注意：这个默认值会让「未知阵营」
    被当成天辉，所以**数据源必须保证 player_slot 有值**，别指望这里兜底。
    """
    try:
        return int(player_slot) < 128
    except (TypeError, ValueError):
        return True


def player_win(match: dict) -> bool:
    """判断该玩家在这场比赛中是否获胜。

    优先用数据源自带的 ``isVictory`` / ``win``（最权威，不依赖 slot 编码）；
    没有时再按 ``player_slot`` 推。这样即使某个源的 slot 归一化出了岔子，
    胜负也不会反 —— 早期 STRATZ 侧 slot 缺失就曾导致「天辉获胜 + 焦点❌负」。
    """
    for key in ("isVictory", "is_victory", "win"):
        flag = match.get(key)
        if isinstance(flag, bool):
            return flag
    radiant_win = bool(match.get("radiant_win"))
    return radiant_win if is_radiant(match.get("player_slot")) else not radiant_win


def mode_text(match: dict) -> str:
    """拼出 ``All Pick · Ranked`` 这样的模式描述。"""
    mode = GAME_MODES.get(match.get("game_mode"), f"模式{match.get('game_mode')}")
    lobby = LOBBY_TYPES.get(match.get("lobby_type"))
    if lobby and lobby not in ("Normal", "Invalid", "Practice"):
        return f"{mode} · {lobby}"
    return mode


def rank_text(rank_tier: Any) -> str:
    """rank_tier 数值 → ``不朽 Immortal`` 之类的段位描述。"""
    try:
        tier = int(rank_tier)
    except (TypeError, ValueError):
        return "无段位"
    if tier <= 0:
        return "无段位（未定级 / 未公开）"
    medal, stars = divmod(tier, 10)
    if medal >= len(RANK_MEDALS):
        medal = len(RANK_MEDALS) - 1
    name = RANK_MEDALS[medal]
    return f"{name} {stars}★" if stars else name


def is_valid_game(match: dict, min_duration: int = 600) -> bool:
    """过滤掉明显无效的对局（秒退、自定义、时长过短）。"""
    try:
        if int(match.get("duration") or 0) < min_duration:
            return False
    except (TypeError, ValueError):
        return False
    return int(match.get("lobby_type") or 0) != -1


# ----------------------------------------------------------------------
# 比赛列表
# ----------------------------------------------------------------------
def format_match_list(
    player_name: str,
    account_id: int,
    matches: list[dict],
    heroes: dict[int, dict],
    title: str | None = None,
) -> str:
    """把比赛列表整理成一条可读的战绩文本。"""
    if not matches:
        return f"没有查询到 {player_name} 的比赛记录。"

    wins = 0
    lines: list[str] = []
    for index, match in enumerate(matches, start=1):
        win = player_win(match)
        wins += 1 if win else 0
        kills = match.get("kills", 0) or 0
        deaths = match.get("deaths", 0) or 0
        assists = match.get("assists", 0) or 0
        kda = (kills + assists) / max(1, deaths)
        flag = "✅胜" if win else "❌负"
        lines.append(
            f"{index:>2}. {time.strftime('%m-%d %H:%M', time.localtime(int(match.get('start_time') or 0)))} "
            f"{flag} · {hname(heroes, match.get('hero_id'))} · KDA {kills}/{deaths}/{assists} ({kda:.2f}) "
            f"· GPM {match.get('gold_per_min') or '-'} · 补刀 {match.get('last_hits') or '-'} "
            f"· 时长 {fmt_duration(match.get('duration'))} · {mode_text(match)} "
            f"· ID {match.get('match_id')}"
        )

    total = len(matches)
    head = title or f"{player_name} 的最近 {total} 场比赛"
    summary = (
        f"{wins} 胜 {total - wins} 负 · 胜率 {wins / total * 100:.1f}%"
    )
    return f"{head}\n账号 ID {account_id} · {summary}\n\n" + "\n".join(lines)


def summarize_matches(matches: list[dict], economy_samples: int | None = None) -> dict[str, Any]:
    """统计一组比赛的关键指标。

    Args:
        matches: 比赛列表。
        economy_samples: 其中带经济类字段（GPM 等）的场次数。不传则自动统计。
    """
    valid = [m for m in matches if isinstance(m, dict)]
    result: dict[str, Any] = {"games": len(valid)}
    if not valid:
        return result

    wins = [m for m in valid if player_win(m)]
    result["wins"] = len(wins)
    result["losses"] = len(valid) - len(wins)
    result["winrate"] = len(wins) / len(valid) * 100

    def _nums(key: str) -> list[float]:
        nums: list[float] = []
        for match in valid:
            value = match.get(key)
            if value is None:
                continue
            try:
                nums.append(float(value))
            except (TypeError, ValueError):
                continue
        return nums

    def _avg(key: str) -> float:
        nums = _nums(key)
        return sum(nums) / len(nums) if nums else 0.0

    # 经济类字段只有 recentMatches 接口才提供，实际样本数可能小于总场次
    detected = len(_nums("gold_per_min"))
    result["economy_samples"] = (
        int(economy_samples) if economy_samples is not None else detected
    )

    result["avg_kills"] = _avg("kills")
    result["avg_deaths"] = _avg("deaths")
    result["avg_assists"] = _avg("assists")
    result["avg_gpm"] = _avg("gold_per_min")
    result["avg_xpm"] = _avg("xp_per_min")
    result["avg_last_hits"] = _avg("last_hits")
    result["avg_denies"] = _avg("denies")
    result["avg_hero_damage"] = _avg("hero_damage")
    result["avg_tower_damage"] = _avg("tower_damage")
    result["avg_hero_healing"] = _avg("hero_healing")
    result["avg_duration"] = _avg("duration")
    result["avg_kda"] = (result["avg_kills"] + result["avg_assists"]) / max(
        1.0, result["avg_deaths"]
    )
    # 场均击杀参与度（K+A 占全队击杀的比例，粗略衡量参团程度）
    kp_values: list[float] = []
    for match in valid:
        team_kills = match.get("_team_kills")
        if not team_kills:
            continue
        try:
            kp_values.append(
                ((match.get("kills") or 0) + (match.get("assists") or 0))
                / float(team_kills)
                * 100
            )
        except (TypeError, ValueError):
            continue
    result["avg_kill_participation"] = (
        sum(kp_values) / len(kp_values) if kp_values else 0.0
    )
    result["kp_samples"] = len(kp_values)

    # 胜负走势：从最近一场往前看
    streak_kind = None
    streak_len = 0
    for match in valid:
        win = player_win(match)
        if streak_kind is None:
            streak_kind, streak_len = win, 1
        elif streak_kind == win:
            streak_len += 1
        else:
            break
    result["streak"] = (streak_kind, streak_len)

    # 最近 10 场胜率
    recent10 = valid[:10]
    result["recent10_winrate"] = (
        sum(1 for m in recent10 if player_win(m)) / len(recent10) * 100
    )
    # 前后半段胜率对比，用于判断状态趋势
    half = max(1, len(valid) // 2)
    result["half_winrate_newer"] = sum(1 for m in valid[:half] if player_win(m)) / half * 100
    older = valid[half:]
    result["half_winrate_older"] = (
        sum(1 for m in older if player_win(m)) / len(older) * 100 if older else 0.0
    )

    # 分路倾向
    lane_counter: Counter = Counter()
    for match in valid:
        role = match.get("lane_role")
        if role:
            lane_counter[int(role)] += 1
    result["lanes"] = lane_counter
    result["roaming_games"] = sum(1 for m in valid if m.get("is_roaming"))

    # 英雄池
    hero_counter: Counter = Counter()
    hero_wins: Counter = Counter()
    for match in valid:
        hid = match.get("hero_id")
        if hid:
            hero_counter[int(hid)] += 1
            if player_win(match):
                hero_wins[int(hid)] += 1
    result["hero_counter"] = hero_counter
    result["hero_wins"] = hero_wins

    # 组队情况（是否单排）
    party = Counter()
    for match in valid:
        size = match.get("party_size")
        if size is None:
            size = 1
        party[int(size)] += 1
    result["party"] = party
    return result


def format_summary_block(
    summary: dict[str, Any], heroes: dict[int, dict]
) -> str:
    """把 summarize_matches 的结果整理成文本块（供大模型阅读）。"""
    if not summary.get("games"):
        return "（没有可用于统计的比赛）"

    streak_kind, streak_len = summary.get("streak", (None, 0))
    streak_text = (
        ("连胜" if streak_kind else "连败") + f" {streak_len} 场"
        if streak_kind is not None and streak_len
        else "无"
    )

    lanes = summary.get("lanes") or Counter()
    lane_text = (
        "、".join(
            f"{LANE_ROLES.get(int(role), f'位置{role}')} {count} 场"
            for role, count in lanes.most_common()
        )
        or "未知"
    )

    hero_counter: Counter = summary.get("hero_counter") or Counter()
    hero_wins: Counter = summary.get("hero_wins") or Counter()
    hero_text = (
        "、".join(
            f"{hname(heroes, hid)} {count}场(胜率{hero_wins.get(hid, 0) / count * 100:.0f}%)"
            for hid, count in hero_counter.most_common(6)
        )
        or "无"
    )

    party: Counter = summary.get("party") or Counter()
    solo = party.get(1, 0)
    party_text = f"单排 {solo} 场 / 组队 {summary['games'] - solo} 场"

    economy_samples = summary.get("economy_samples", 0)
    economy_note = (
        f"（经济与分路类数据仅 {economy_samples}/{summary['games']} 场可用，"
        f"来自 OpenDota recentMatches 接口，最多 20 场）"
        if 0 < economy_samples < summary["games"]
        else ""
    )

    kp_line = ""
    if summary.get("kp_samples"):
        kp_line = (
            f"场均击杀参与率: {summary.get('avg_kill_participation', 0):.1f}%"
            f"（样本 {summary.get('kp_samples', 0)} 场）\n"
        )

    return (
        f"样本场次: {summary['games']} 场（{summary.get('wins', 0)} 胜 "
        f"{summary.get('losses', 0)} 负，胜率 {summary.get('winrate', 0):.1f}%）\n"
        f"最近 10 场胜率: {summary.get('recent10_winrate', 0):.1f}%\n"
        f"状态趋势: 后一半场次胜率 {summary.get('half_winrate_newer', 0):.1f}% "
        f"vs 前一半 {summary.get('half_winrate_older', 0):.1f}%\n"
        f"当前连续战绩: {streak_text}\n"
        f"场均 K/D/A: {summary.get('avg_kills', 0):.1f} / {summary.get('avg_deaths', 0):.1f} "
        f"/ {summary.get('avg_assists', 0):.1f}（KDA {summary.get('avg_kda', 0):.2f}）\n"
        f"{kp_line}"
        f"场均 GPM/XPM: {summary.get('avg_gpm', 0):.0f} / {summary.get('avg_xpm', 0):.0f}{economy_note}\n"
        f"场均补刀/反补: {summary.get('avg_last_hits', 0):.0f} / {summary.get('avg_denies', 0):.0f}\n"
        f"场均英雄伤害/建筑伤害/治疗: {summary.get('avg_hero_damage', 0):.0f} / "
        f"{summary.get('avg_tower_damage', 0):.0f} / {summary.get('avg_hero_healing', 0):.0f}\n"
        f"场均时长: {fmt_duration(summary.get('avg_duration', 0))}\n"
        f"分路分布: {lane_text}\n"
        f"游走对局: {summary.get('roaming_games', 0)} 场\n"
        f"英雄池: {hero_text}\n"
        f"组队情况: {party_text}"
    )


# ----------------------------------------------------------------------
# 单场比赛
# ----------------------------------------------------------------------
def _describe_objective(objective: dict) -> str:
    """把一条 objectives 记录翻译成中文描述。"""
    obj_type = str(objective.get("type") or "")
    when = fmt_clock(objective.get("time"))
    team = objective.get("team")

    if obj_type == "building_kill":
        key = str(objective.get("key") or "")
        # npc_dota_badguys_tower1_mid → 夜魇（Dire）建筑被摧毁
        if "badguys" in key:
            side, target = "天辉", "夜魇"
        elif "goodguys" in key:
            side, target = "夜魇", "天辉"
        else:
            side, target = "未知方", "未知方"
        building = "建筑"
        for token, name in BUILDING_HINTS:
            if token in key:
                building = name
                break
        lane = ""
        for token, name in LANE_HINTS:
            if key.endswith(token):
                lane = name
                break
        return f"{when} [{side}] 摧毁 {target} 的{lane}{building}"

    if obj_type in OBJECTIVE_HINTS:
        text = OBJECTIVE_HINTS[obj_type]
        # 部分事件只有 player_slot，需要反推阵营
        if team not in (2, 3):
            slot = objective.get("player_slot")
            if slot is not None:
                team = 2 if is_radiant(slot) else 3
        if team in (2, 3):
            side = "天辉" if team == 2 else "夜魇"
            return f"{when} [{side}] {text}"
        return f"{when} {text}"

    return f"{when} {obj_type}"


def summarize_objectives(objectives: list[dict]) -> list[str]:
    """整理关键事件时间轴。"""
    rows: list[str] = []
    for objective in objectives or []:
        if not isinstance(objective, dict):
            continue
        obj_type = str(objective.get("type") or "")
        if obj_type in (
            "CHAT_MESSAGE_COURIER_LOST",
            "CHAT_MESSAGE_GLYPH_USED",
            "CHAT_MESSAGE_MINIBOSS_KILL",
        ):
            # 信使与符文类事件过多，只在后面单独统计
            continue
        rows.append(_describe_objective(objective))
    return rows


def summarize_gold_advance(match: dict, step_minutes: int = 3) -> list[str]:
    """把 radiant_gold_adv 采样成可读的经济走势。"""
    adv = match.get("radiant_gold_adv")
    if not isinstance(adv, list) or not adv:
        return []
    rows: list[str] = []
    for minute in range(0, len(adv), step_minutes):
        value = adv[minute]
        try:
            num = float(value)
        except (TypeError, ValueError):
            continue
        rows.append(f"{minute}m {num / 1000:+.1f}k")
    # 补上最后一分钟
    try:
        last = float(adv[-1])
        rows.append(f"{len(adv) - 1}m {last / 1000:+.1f}k")
    except (TypeError, ValueError):
        pass
    return rows


def peak_gold_advance(match: dict) -> tuple[float, int]:
    """返回 ``(最大经济领先绝对值, 出现分钟)``。"""
    adv = match.get("radiant_gold_adv")
    if not isinstance(adv, list) or not adv:
        return 0.0, 0
    best_value = 0.0
    best_minute = 0
    for minute, value in enumerate(adv):
        try:
            num = float(value)
        except (TypeError, ValueError):
            continue
        if abs(num) > abs(best_value):
            best_value, best_minute = num, minute
    return best_value, best_minute


def gold_adv_metrics(match: dict) -> dict[str, Any] | None:
    """从 ``radiant_gold_adv`` 自行推导局势波动指标（口径明确、不依赖 OpenDota 语义模糊的字段）。

    返回 ``{"lead_changes", "winner_behind", "final_margin", "radiant_win"}``；
    数据缺失时返回 ``None``。
    """
    adv = match.get("radiant_gold_adv")
    if not isinstance(adv, list) or not adv:
        return None
    series: list[float] = []
    for value in adv:
        try:
            series.append(float(value))
        except (TypeError, ValueError):
            return None
    if not series:
        return None
    radiant_win = bool(match.get("radiant_win"))

    # 领先易手：过滤掉平局段后统计符号变化次数
    signs = [1 if v > 0 else (-1 if v < 0 else 0) for v in series]
    lead_changes = 0
    last = 0
    for sign in signs:
        if sign == 0:
            continue
        if last and sign != last:
            lead_changes += 1
        last = sign

    # 最终胜方曾经落后的最大幅度（翻盘深度）
    winner_behind = 0.0
    if radiant_win:
        winner_behind = abs(min(series)) if min(series) < 0 else 0.0
    else:
        winner_behind = max(series) if max(series) > 0 else 0.0

    return {
        "lead_changes": lead_changes,
        "winner_behind": winner_behind,
        "final_margin": series[-1],
        "radiant_win": radiant_win,
    }


def summarize_teamfights(
    teamfights: list[dict],
    limit: int = 12,
    *,
    heroes: dict[int, dict] | None = None,
    players: list[dict] | None = None,
    focus_ids: list[int] | None = None,
    focus_names: dict[int, str] | None = None,
) -> list[str]:
    """整理团战列表。

    Args:
        heroes: 英雄常量表，用于把 ``killed`` 里的 NPC 名翻成英雄名。
        players: 本场十名玩家，用来建立「团战里的第 N 个位置 → 是哪位玩家」
            的映射（``teamfights[].players`` 按 player_slot 顺序排列）。
        focus_ids: 焦点玩家，会在每次团战里单独列出他们的贡献。
        focus_names: ``{account_id: 昵称}``。

    每场团战输出主行（时间、阵亡人数、双方经济）＋可选的两条细节：
    「谁死了」与「焦点玩家做了什么」。解析产物里 ``teamfights[].players``
    本来就带 ``damage`` / ``healing`` / ``killed`` / ``deaths`` / ``gold_delta``
    / ``xp_delta``，早期版本只把它们求和成一条经济数字，白白丢掉了
    「这场团战谁在打输出、谁先死」这类最关键的复盘证据。
    """
    heroes = heroes or {}
    ordered_players = sorted(
        [p for p in (players or []) if isinstance(p, dict)],
        key=lambda p: int(p.get("player_slot") or 0),
    )
    focus_set = {int(i) for i in (focus_ids or [])}
    names = focus_names or {}

    rows: list[str] = []
    for index, fight in enumerate(teamfights or [], start=1):
        if not isinstance(fight, dict):
            continue
        if index > limit:
            break
        start = fmt_clock(fight.get("start"))
        end = fmt_clock(fight.get("end"))
        deaths = fight.get("deaths", 0)
        fighters = fight.get("players") or []
        radiant_gold = 0
        dire_gold = 0
        radiant_deaths = 0
        dire_deaths = 0
        radiant_dead_names: list[str] = []
        dire_dead_names: list[str] = []
        focus_notes: list[str] = []

        for slot, entry in enumerate(fighters):
            if not isinstance(entry, dict):
                continue
            try:
                delta = int(entry.get("gold_delta") or 0)
            except (TypeError, ValueError):
                delta = 0
            # teamfights.players 按 player_slot 顺序排列（0-4 天辉，128-132 夜魇）
            is_rad = slot < 5
            if is_rad:
                radiant_gold += delta
            else:
                dire_gold += delta

            # 找出这一位是哪名玩家（用于把击杀/阵亡翻译成人名英雄名）
            owner: dict = {}
            if slot < len(ordered_players):
                owner = ordered_players[slot]
            own_label = ""
            if owner:
                hero_name = hname(heroes, owner.get("hero_id"))
                own_label = (
                    f"{names.get(int(owner.get('account_id') or 0)) or owner.get('name') or owner.get('account_id')}·{hero_name}"
                )

            entry_deaths = int(entry.get("deaths") or 0)
            if entry_deaths:
                if is_rad:
                    radiant_deaths += entry_deaths
                    if own_label:
                        radiant_dead_names.append(own_label)
                else:
                    dire_deaths += entry_deaths
                    if own_label:
                        dire_dead_names.append(own_label)

            account_id = int((owner or {}).get("account_id") or 0)
            if account_id and account_id in focus_set:
                bits = [f"{names.get(account_id) or account_id}"]
                kills = entry.get("killed")
                if isinstance(kills, dict) and kills:
                    killed_names = [
                        hname_by_npc(heroes, k)
                        for k in kills
                        if str(k).startswith("npc_dota_hero_")
                    ]
                    if killed_names:
                        bits.append("击杀 " + "、".join(killed_names))
                if entry_deaths:
                    bits.append(f"阵亡 {entry_deaths} 次")
                damage = entry.get("damage")
                if damage:
                    bits.append(f"打出 {fmt_num(damage)} 伤害")
                healing = entry.get("healing")
                if healing:
                    bits.append(f"治疗 {fmt_num(healing)}")
                bits.append(f"经济 {delta:+d}")
                try:
                    bits.append(f"经验 {int(entry.get('xp_delta') or 0):+d}")
                except (TypeError, ValueError):
                    pass
                if entry.get("buybacks"):
                    bits.append("交了买活")
                focus_notes.append("　".join(bits))

        rows.append(
            f"#{index} {start}-{end} 阵亡 {deaths} 人"
            f"（天辉 {radiant_deaths} / 夜魇 {dire_deaths}）· "
            f"天辉合计经济 {radiant_gold:+d} / 夜魇 {dire_gold:+d}"
        )
        if radiant_dead_names or dire_dead_names:
            dead_bits = []
            if radiant_dead_names:
                dead_bits.append("天辉 " + "、".join(radiant_dead_names))
            if dire_dead_names:
                dead_bits.append("夜魇 " + "、".join(dire_dead_names))
            rows.append("   阵亡: " + "；".join(dead_bits))
        for note in focus_notes:
            rows.append(f"   焦点贡献: {note}")
    return rows


# ----------------------------------------------------------------------
# 解析产物的通用渲染工具
# ----------------------------------------------------------------------
#: ``damage`` / ``killed`` 这类字典的键是「单位内部名」，这里把它们收敛成
#: 少量可读类别，避免提示词里出现一长串 ``npc_dota_neutral_xxx``。
def unit_label(heroes: dict[int, dict], key: Any) -> str:
    """把单位内部名翻成可读标签（英雄名 / 小兵 / 野怪 / 建筑）。"""
    raw = str(key or "").strip()
    if not raw or raw.lower() in ("null", "none"):
        return "普攻/未记录来源"
    if raw.startswith("npc_dota_hero_"):
        return hname_by_npc(heroes, raw)
    if raw.startswith("npc_dota_creep_"):
        return "小兵"
    if raw.startswith("npc_dota_neutral_"):
        return "野怪"
    if raw.startswith("npc_dota_badguys_") or raw.startswith("npc_dota_goodguys_"):
        return "建筑"
    if raw.startswith("npc_dota_"):
        return raw[len("npc_dota_"):].replace("_", " ")
    return raw


def ability_label(key: Any) -> str:
    """技能 / 物品内部名 → 可读标签（``ogre_magi_fireblast`` → ``Ogre Magi Fireblast``）。"""
    raw = str(key or "").strip()
    if not raw or raw.lower() in ("null", "none"):
        return "普攻/未记录"
    if raw.startswith("npc_dota_"):
        return raw[len("npc_dota_"):].replace("_", " ")
    return raw.replace("_", " ")


def _breakdown_text(
    data: Any,
    limit: int = 5,
    *,
    labels: dict[int, str] | None = None,
    labeler: Any = None,
    percent: bool = True,
) -> str:
    """把 ``{键: 数值}`` 形状的解析数据渲染成「标签 数值(占比)」。

    解析产物里大量字段都是这个形状（``gold_reasons`` / ``damage`` /
    ``item_uses`` / ``killed`` / ``damage_taken`` …），统一在这里处理：

    * 数值解析失败的项直接跳过（``None`` / 嵌套 dict 等）；
    * 按绝对值倒序取前 ``limit`` 项；
    * 占比按绝对值之和算，这样「死亡损失」这类负值不会把分母搞错。
    """
    if not isinstance(data, dict) or not data:
        return ""
    rows: list[tuple[float, float, str]] = []
    for key, value in data.items():
        if isinstance(value, bool):
            continue
        try:
            num = float(value)
        except (TypeError, ValueError):
            continue
        if num == 0:
            continue
        if labeler is not None:
            label = labeler(key)
        elif labels is not None:
            try:
                label = labels.get(int(key), f"原因{key}")
            except (TypeError, ValueError):
                label = f"原因{key}"
        else:
            label = str(key)
        rows.append((abs(num), num, label))
    if not rows:
        return ""
    total = sum(row[0] for row in rows) or 1.0
    rows.sort(key=lambda item: item[0], reverse=True)
    parts: list[str] = []
    for _, num, label in rows[:limit]:
        if percent:
            parts.append(f"{label} {num:,.0f}({abs(num) / total * 100:.0f}%)")
        else:
            parts.append(f"{label} {num:,.0f}")
    return "、".join(parts)


def summarize_series(values: Any, step: int = 3) -> str:
    """把逐分钟数组采样成 ``0m 0 · 3m 2.1k · …``。

    解析产物动辄 40~60 个采样点，全部塞进提示词性价比极低：采样后既保住
    走势形状，又不会把其它信息淹掉。
    """
    if not isinstance(values, list) or not values:
        return ""
    step = max(1, int(step))
    parts: list[str] = []
    for minute in range(0, len(values), step):
        parts.append(f"{minute}m {fmt_k(values[minute])}")
    last = len(values) - 1
    if last % step:  # 末尾没落在采样点上时补一个，避免丢掉终局值
        parts.append(f"{last}m {fmt_k(values[last])}")
    return " · ".join(parts)


def summarize_player_curve(player: dict, step: int = 3) -> list[str]:
    """逐分钟发育曲线（经济 / 经验 / 补刀 / 反补 / 英雄伤害 / 堆野）。

    只有 OpenDota 的完整解析产物才带这些数组；缺哪条就跳过哪条。
    """
    rows: list[str] = []
    pairs = [
        ("gold_t", "逐分钟累计金钱"),
        ("xp_t", "逐分钟累计经验"),
        ("lh_t", "逐分钟累计补刀"),
        ("dn_t", "逐分钟累计反补"),
        ("hero_damage_t", "逐分钟累计英雄伤害"),
    ]
    for key, title in pairs:
        text = summarize_series(player.get(key), step)
        if text:
            rows.append(f"{title}: {text}")
    stacked = player.get("camps_stacked_t")
    if isinstance(stacked, list) and any(int(x or 0) for x in stacked if isinstance(x, (int, float))):
        rows.append("逐分钟堆野: " + summarize_series(stacked, step))
    healed = player.get("hero_healing_t")
    if isinstance(healed, list) and any(int(x or 0) for x in healed if isinstance(x, (int, float))):
        rows.append("逐分钟累计治疗: " + summarize_series(healed, step))
    return rows


def dead_time_text(player: dict) -> str:
    """由 ``life_state`` 推算「阵亡 + 复活等待」时长（占比赛时长比例）。"""
    states = player.get("life_state")
    if not isinstance(states, dict):
        return "-"
    total = 0.0
    dead = 0.0
    found = False
    for key, value in states.items():
        try:
            num = float(value)
            index = int(key)
        except (TypeError, ValueError):
            continue
        total += num
        if index == 2:
            dead = num
            found = True
    if not found or total <= 0:
        return "-"
    return f"{dead:,.0f}s（占总时长 {dead / total * 100:.0f}%）"


def summarize_damage_targets(
    player: dict, heroes: dict[int, dict], limit: int = 4
) -> str:
    """``damage_targets``（技能 → 受害者）汇总成「对谁造成最多英雄伤害」。

    这是判断「谁在对线/团战里针对谁」最直接的证据，比单纯的
    ``hero_damage`` 总量信息量大得多。
    """
    targets = player.get("damage_targets")
    if not isinstance(targets, dict):
        return ""
    total: dict[str, float] = {}
    for victims in targets.values():
        if not isinstance(victims, dict):
            continue
        for victim, value in victims.items():
            name = str(victim)
            if not name.startswith("npc_dota_hero_"):
                continue
            try:
                num = float(value)
            except (TypeError, ValueError):
                continue
            total[name] = total.get(name, 0.0) + num
    if not total:
        return ""
    ordered = sorted(total.items(), key=lambda kv: kv[1], reverse=True)
    return "、".join(
        f"对{hname_by_npc(heroes, name)} {int(value):,}" for name, value in ordered[:limit]
    )


def summarize_wards(player: dict, heroes: dict[int, dict]) -> str:
    """视野：插眼数量 + 时间跨度 + 被敌方清除的数量。

    ``obs_left_log`` / ``sen_left_log`` 里 ``attackername`` 是「是谁让它消失的」，
    自己把自己插的眼换掉（补位）也会记进去，因此这里只把「敌方英雄清除」算作被反。
    """
    hero_info = heroes.get(player.get("hero_id")) if isinstance(heroes, dict) else None
    own_npc = (hero_info or {}).get("name")

    def _times(rows: Any) -> list[int]:
        out: list[int] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            stamp = row.get("time")
            if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
                continue
            out.append(int(stamp))
        return out

    parts: list[str] = []
    for log_key, label, left_key in WARD_LOGS:
        placed = _times(player.get(log_key))
        if not placed:
            continue
        text = f"{label} {len(placed)} 个（首次 {fmt_clock(min(placed))}"
        if len(placed) > 1:
            text += f" / 末次 {fmt_clock(max(placed))}"
        text += "）"
        denied = 0
        for row in player.get(left_key) or []:
            if not isinstance(row, dict):
                continue
            attacker = row.get("attackername")
            if not isinstance(attacker, str) or not attacker.startswith("npc_dota_hero_"):
                continue
            if own_npc and attacker == own_npc:
                continue  # 自己换的眼，不算被反
            denied += 1
        if denied:
            text += f"（被敌方清除 {denied} 个）"
        parts.append(text)
    return "　".join(parts)


def summarize_kill_log(player: dict, heroes: dict[int, dict]) -> str:
    """``kills_log`` → 「时间 目标」的击杀时间线（判断节奏点用）。"""
    log = player.get("kills_log")
    if not isinstance(log, list) or not log:
        return ""
    parts: list[str] = []
    for row in log:
        if not isinstance(row, dict):
            continue
        parts.append(f"{fmt_clock(row.get('time'))} {hname_by_npc(heroes, row.get('key'))}")
    return "、".join(parts)


def summarize_killed_by(player: dict, heroes: dict[int, dict], limit: int = 5) -> str:
    """``killed_by`` → 「被谁击杀了几次」。"""
    killed_by = player.get("killed_by")
    if not isinstance(killed_by, dict) or not killed_by:
        return ""
    rows: list[tuple[float, str]] = []
    for name, value in killed_by.items():
        if not str(name).startswith("npc_dota_hero_"):
            continue
        try:
            num = float(value)
        except (TypeError, ValueError):
            continue
        rows.append((num, hname_by_npc(heroes, name)))
    if not rows:
        return ""
    rows.sort(key=lambda item: item[0], reverse=True)
    return "、".join(f"{label}×{int(num)}" for num, label in rows[:limit])


def summarize_multi_kills(player: dict) -> str:
    """``multi_kills`` / ``kill_streaks`` → 「双杀×2、三杀×1」这种描述。"""
    parts: list[str] = []
    multi = player.get("multi_kills")
    if isinstance(multi, dict):
        rows: list[tuple[int, int]] = []
        for key, value in multi.items():
            try:
                rows.append((int(key), int(value)))
            except (TypeError, ValueError):
                continue
        rows.sort()
        text = "、".join(
            f"{MULTI_KILL_TEXT.get(size, f'{size}连杀')}×{count}"
            for size, count in rows
            if count
        )
        if text:
            parts.append(text)
    streaks = player.get("kill_streaks")
    if isinstance(streaks, dict):
        rows = []
        for key, value in streaks.items():
            try:
                rows.append((int(key), int(value)))
            except (TypeError, ValueError):
                continue
        rows.sort(reverse=True)
        if rows:
            parts.append(
                "最长连杀 %d 次" % rows[0][0]
                + (f"（达成 {sum(c for _, c in rows)} 次）" if rows[0][1] else "")
            )
    return "；".join(parts)


def summarize_item_timeline(
    player: dict, item_index: ItemIndex, min_cost: int = 2000
) -> str:
    """``purchase_log`` → 关键装备（≥ ``min_cost``）的购买时间线。"""
    log = player.get("purchase_log")
    if not isinstance(log, list) or not log:
        return ""
    parts: list[str] = []
    for entry in log:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if item_index.cost(key) < min_cost:
            continue
        parts.append(
            f"{item_index.name(key, default=str(key))}@{fmt_clock(entry.get('time'))}"
        )
    return "、".join(parts)


def summarize_neutral_items(player: dict, item_index: ItemIndex) -> str:
    """``neutral_item_history`` → 中立物品（含附魔）的获取时间线。"""
    history = player.get("neutral_item_history")
    if not isinstance(history, list) or not history:
        return ""
    parts: list[str] = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        name = item_index.name(
            entry.get("item_neutral"), default=str(entry.get("item_neutral") or "?")
        )
        enhancement = entry.get("item_neutral_enhancement")
        text = f"{name}@{fmt_clock(entry.get('time'))}"
        if enhancement:
            text += f"[{str(enhancement).replace('enhancement_', '')}]"
        parts.append(text)
    return "、".join(parts)


def summarize_ability_build(
    player: dict, ability_names: dict[int, str] | None, limit: int = 20
) -> str:
    """``ability_upgrades_arr`` → 加点顺序。

    数组里存的是技能**数字 ID**，没有常量表就完全不可读（``5439`` 是什么
    谁也猜不出来），因此拿不到名字映射时**宁可不输出**，也不喂一串数字。
    """
    upgrades = player.get("ability_upgrades_arr")
    if not isinstance(upgrades, list) or not upgrades or not ability_names:
        return ""
    ordinals = [
        "1级", "2级", "3级", "4级", "5级", "6级", "7级", "8级", "9级", "10级",
        "11级", "12级", "13级", "14级", "15级", "16级", "17级", "18级",
        "19级", "20级", "21级", "22级", "23级", "24级", "25级",
    ]
    parts: list[str] = []
    for index, ability_id in enumerate(upgrades[:limit]):
        try:
            name = ability_names.get(int(ability_id))
        except (TypeError, ValueError):
            name = None
        label = ability_label(name) if name else f"技能#{ability_id}"
        level_text = ordinals[index] if index < len(ordinals) else f"第{index + 1}次"
        parts.append(f"{level_text} {label}")
    return " → ".join(parts)


def summarize_ability_uses(player: dict, limit: int = 6) -> str:
    """``ability_uses`` → 用得最多的技能（看这名玩家实际在干什么）。"""
    return _breakdown_text(player.get("ability_uses"), limit, labeler=ability_label)


def summarize_item_uses(player: dict, limit: int = 6) -> str:
    """``item_uses`` → 用得最多的主动道具（跳刀/魔棒/雾…看行动力）。"""
    return _breakdown_text(player.get("item_uses"), limit, labeler=ability_label)


def player_advanced_line(
    player: dict,
    heroes: dict[int, dict],
    item_index: ItemIndex,
    *,
    limit: int = 3,
) -> str:
    """十人表里的「进阶一行」：把解析产物按最影响判断的几个维度压缩成一行。

    与焦点玩家的深入小节故意保持不同粒度 —— 十人表要的是横向可比，
    焦点要的是纵向细节。
    """
    parts: list[str] = []
    dealt = _breakdown_text(player.get("damage_inflictor"), limit, labeler=ability_label)
    if dealt:
        parts.append(f"输出构成 {dealt}")
    taken = _breakdown_text(
        player.get("damage_taken"), limit, labeler=lambda k: unit_label(heroes, k)
    )
    if taken:
        parts.append(f"承伤构成 {taken}")
    # 说明：经济来源已在上一条「进阶行」里给过（那里还带死亡耗时等），
    # 这里不再重复，避免同一份提示词里同一条信息出现两次。
    xp_from = _breakdown_text(player.get("xp_reasons"), limit, labels=XP_REASONS)
    if xp_from:
        parts.append(f"经验来源 {xp_from}")
    targets = summarize_damage_targets(player, heroes, limit)
    if targets:
        parts.append(f"主要输出对象 {targets}")
    uses = summarize_item_uses(player, limit)
    if uses:
        parts.append(f"道具使用 {uses}")
    abilities = summarize_ability_uses(player, limit)
    if abilities:
        parts.append(f"技能使用 {abilities}")
    return " | ".join(parts)


def _player_row(
    player: dict,
    heroes: dict[int, dict],
    item_index: ItemIndex,
) -> str:
    """生成一名玩家的单行数据。"""
    account_id = player.get("account_id")
    name = player.get("name") or player.get("personaname") or (
        f"账号{account_id}" if account_id else "匿名玩家"
    )
    slot = player.get("player_slot")
    side = "天辉" if is_radiant(slot) else "夜魇"
    kda = f"{player.get('kills', 0)}/{player.get('deaths', 0)}/{player.get('assists', 0)}"
    tf = player.get("teamfight_participation")
    tf_text = f"{float(tf) * 100:.0f}%" if isinstance(tf, (int, float)) and not isinstance(tf, bool) else "-"
    net = fmt_k(player.get("net_worth") or player.get("total_gold") or 0)

    inventory = []
    for index in range(6):
        item_name = item_index.name(player.get(f"item_{index}"), default="")
        if item_name:
            inventory.append(item_name)
    for index in range(3):
        backpack_item = item_index.name(player.get(f"backpack_{index}"), default="")
        if backpack_item:
            inventory.append(f"[背包]{backpack_item}")
    neutral = item_index.name(player.get("item_neutral"), default="")
    if neutral:
        inventory.append(f"[中立]{neutral}")
    if player.get("aghanims_scepter"):
        inventory.append("[已出神杖]")
    if player.get("aghanims_shard"):
        inventory.append("[已出魔晶]")
    if player.get("moonshard"):
        inventory.append("[已吃月之碎片]")

    # 受伤/死亡相关的进阶字段：只有完整解析才有，缺失时整段不输出
    extras: list[str] = []
    gold_reasons = _breakdown_text(player.get("gold_reasons"), 3, labels=GOLD_REASONS)
    if gold_reasons:
        extras.append(f"经济来源 {gold_reasons}")
    dead_time = dead_time_text(player)
    if dead_time != "-":
        extras.append(f"阵亡+复活耗时 {dead_time}")
    gold_spent = player.get("gold_spent")
    gold_left = player.get("gold")
    if gold_spent is not None or gold_left is not None:
        extras.append(f"已花费{fmt_num(gold_spent)}/余额{fmt_num(gold_left)}")
    if player.get("firstblood_claimed"):
        extras.append("拿到一血")
    multi = summarize_multi_kills(player)
    if multi:
        extras.append(multi)
    for label, key in (
        ("击杀建筑", "towers_killed"),
        ("击杀肉山", "roshans_killed"),
        ("堆野", "camps_stacked"),
        ("吃符", "rune_pickups"),
        ("信使击杀", "courier_kills"),
        ("野怪击杀", "neutral_kills"),
    ):
        value = player.get(key)
        if value:
            extras.append(f"{label}{_int_or_dash(value)}")
    pings = player.get("pings")
    if pings is not None:
        extras.append(f"打点 {pings} 次")

    lines = [
        (
            f"{side} | {name}"
            f"{'(→焦点)' if player.get('_focus') else ''} | {hname(heroes, player.get('hero_id'))} "
            f"Lv{player.get('level', '-')} | {kda} KDA{fmt_float(player.get('_kda', 0))} "
            f"| 净经济{net} GPM{player.get('gold_per_min', '-')}/XPM{player.get('xp_per_min', '-')} "
            f"| 补刀{player.get('last_hits', '-')}/{player.get('denies', '-')} "
            f"| 英雄伤害{fmt_num(player.get('hero_damage', 0))} 塔伤{fmt_num(player.get('tower_damage', 0))} "
            f"治疗{fmt_num(player.get('hero_healing', 0))} | 参团率{tf_text} "
            f"| 控制{_num_or_dash(player.get('stuns'), 1, suffix='s')} "
            f"假眼{_int_or_dash(player.get('obs_placed'))}/真眼{_int_or_dash(player.get('sen_placed'))} "
            f"| 出装: {', '.join(inventory) or '-'}"
        )
    ]
    if extras:
        lines.append("    　" + " | ".join(extras))
    advanced = player_advanced_line(player, heroes, item_index)
    if advanced:
        lines.append("    　" + advanced)
    return "\n".join(lines)


def lane_efficiency_text(player: dict) -> str:
    """对线补刀效率（把两种口径统一成百分比）。

    OpenDota 同时给两个字段，而且**量纲不一样**，早期实现直接把
    ``lane_efficiency_pct`` 又乘了一次 100，于是 119% 变成了「11900%」：

    * ``lane_efficiency``：比率，1.2 表示 120%；
    * ``lane_efficiency_pct``：已经是百分数，119 表示 119%。

    优先用比率，其次用百分数（值 ≤5 时按比率处理，兼容少数把百分数字段
    写成比率的解析结果）。两者都缺时返回 ``-``。
    """
    ratio = player.get("lane_efficiency")
    if isinstance(ratio, (int, float)) and not isinstance(ratio, bool):
        return f"{float(ratio) * 100:.0f}%"
    pct = player.get("lane_efficiency_pct")
    if isinstance(pct, (int, float)) and not isinstance(pct, bool):
        value = float(pct)
        return f"{value * 100:.0f}%" if value <= 5 else f"{value:.0f}%"
    return "-"


def _lane_text(player: dict) -> str:
    role = player.get("lane_role")
    lane = player.get("lane")
    parts = []
    position = POSITION_TEXT.get(str(player.get("position") or "").upper())
    if position:
        parts.append(position)
    if role:
        parts.append(LANE_ROLES.get(int(role), f"位置{role}"))
    if lane:
        parts.append(f"lane={lane}")
    if player.get("is_roaming"):
        parts.append("游走")
    return " ".join(parts) or "未识别"


def curve_points_text(player: dict, minutes: tuple[int, ...] = (10, 20, 30, 40)) -> str:
    """把逐分钟曲线抽成「第 N 分钟累计值」，方便十人横向对比。

    与 :func:`summarize_player_curve` 的分工：这里只取几个关键节点，
    用来横向比谁发育快；那里保留完整走势，用来看单人的曲线形状。
    """
    bits: list[str] = []
    gold = player.get("gold_t")
    if isinstance(gold, list) and gold:
        points = []
        for minute in minutes:
            if minute < len(gold):
                points.append(f"{minute}m {fmt_k(gold[minute])}")
        if points:
            bits.append("金钱累计 " + " / ".join(points))
    lh = player.get("lh_t")
    if isinstance(lh, list) and lh:
        points = []
        for minute in minutes:
            if minute < len(lh):
                points.append(f"{minute}m {fmt_k(lh[minute])}")
        if points:
            bits.append("补刀累计 " + " / ".join(points))
    xp = player.get("xp_t")
    if isinstance(xp, list) and xp:
        points = []
        for minute in minutes:
            if minute < len(xp):
                points.append(f"{minute}m {fmt_k(xp[minute])}")
        if points:
            bits.append("经验累计 " + " / ".join(points))
    return "；".join(bits)


def normalize_focus_ids(value: Any) -> list[int]:
    """把「焦点玩家」入参统一成 account_id 列表。

    同时兼容三种写法（调用方既有单场复盘的单焦点，也有监听推送的多焦点）：

    * ``None`` / 空 → ``[]``
    * 单个 ``int`` → ``[value]``
    * 任意 ``int`` 可迭代对象 → 去重后的列表（保持顺序）
    """
    if value is None:
        return []
    if isinstance(value, bool):  # bool 是 int 的子类，但不是合法账号
        return []
    if isinstance(value, int):
        return [value] if value else []
    if isinstance(value, (str, bytes)):
        return []
    result: list[int] = []
    for item in value:
        try:
            account_id = int(item)
        except (TypeError, ValueError):
            continue
        if account_id and account_id not in result:
            result.append(account_id)
    return result


def build_match_data_text(
    match: dict,
    heroes: dict[int, dict],
    items: dict[str, dict] | None = None,
    focus_account_ids: int | list[int] | tuple[int, ...] | None = None,
    include_timeline: bool = True,
    *,
    abilities: dict[int, str] | None = None,
    ability_names: dict[int, str] | None = None,
    curve_ids: list[int] | None = None,
) -> str:
    """把单场比赛整理成详尽的文本，供大模型复盘使用。

    ``focus_account_ids`` 支持传一位或多位焦点玩家（同一局里可能有多位被监听
    的玩家参战）；每位焦点玩家都会得到一份独立的「深入数据」小节。

    ``abilities`` / ``ability_names``：技能常量映射 ``{技能ID: 技能内部名}``。
    只有拿到它才能把 ``ability_upgrades_arr``（一串数字）翻译成加点顺序，
    因此这是个可选增强项，拿不到就不输出加点小节。

    ``curve_ids``：额外需要「逐分钟发育曲线」的玩家。焦点玩家默认都有，
    这里主要给「无焦点玩家时也要看两边核心的发育节奏」这种场景用。
    """
    items = items or {}
    item_index = ItemIndex(items)
    ability_map = ability_names if ability_names is not None else abilities
    players = [p for p in (match.get("players") or []) if isinstance(p, dict)]

    radiant = [p for p in players if is_radiant(p.get("player_slot"))]
    dire = [p for p in players if not is_radiant(p.get("player_slot"))]
    radiant_win = bool(match.get("radiant_win"))
    winner = "天辉" if radiant_win else "夜魇"

    focus_ids = normalize_focus_ids(focus_account_ids)
    curve_id_list = normalize_focus_ids(curve_ids)

    for player in players:
        try:
            player["_kda"] = (
                (player.get("kills") or 0) + (player.get("assists") or 0)
            ) / max(1, player.get("deaths") or 0)
        except (TypeError, ValueError):
            player["_kda"] = 0.0
        player["_focus"] = bool(player.get("account_id") in focus_ids)

    focuses = [p for p in players if p.get("_focus")]

    lines: list[str] = []
    lines.append("=== 对局基本信息 ===")
    lines.append(f"match_id: {match.get('match_id')}")
    lines.append(
        f"开始时间: {fmt_timestamp(match.get('start_time'))}"
        f"（{fmt_ago(match.get('start_time'))}）"
    )
    lines.append(f"时长: {fmt_duration(match.get('duration'))}")
    lines.append(f"模式: {mode_text(match)}")
    lines.append(
        f"比分: 天辉 {match.get('radiant_score', 0)} - {match.get('dire_score', 0)} 夜魇"
        f"　获胜方: {winner}"
    )
    radiant_buyback = sum(
        len(p.get("buyback_log") or []) for p in radiant if isinstance(p, dict)
    )
    dire_buyback = sum(
        len(p.get("buyback_log") or []) for p in dire if isinstance(p, dict)
    )
    lines.append(
        f"一血时间: {fmt_clock(match.get('first_blood_time'))}"
        f"　买活次数: 天辉 {radiant_buyback}/夜魇 {dire_buyback}"
    )
    region = match.get("region")
    cluster = match.get("cluster")
    if match.get("patch"):
        lines.append(f"补丁版本代号(patch): {match.get('patch')}")
    if region or cluster:
        lines.append(
            f"region: {region if region is not None else '未提供'}"
            f"　cluster: {cluster if cluster is not None else '未提供'}"
        )
    buildings = building_states_text(match)
    if buildings:
        lines.append("建筑存活: " + buildings)
    if match.get("average_rank"):
        lines.append(f"双方平均段位: {rank_text(match.get('average_rank'))}")
    if match.get("human_players") is not None:
        lines.append(f"真人玩家数: {match.get('human_players')}/10")
    league = match.get("leagueid")
    if league:
        lines.append(
            f"联赛对局: leagueid={league}"
            + (f"　series_id={match.get('series_id')}" if match.get("series_id") else "")
        )
    _parsed, _parsed_note = parsed_state(match)
    lines.append(f"数据完整度: {_parsed_note}")

    # 阵容
    lines.append("")
    lines.append("=== 阵容 ===")
    lines.append(
        "天辉: " + ", ".join(hname(heroes, p.get("hero_id")) for p in radiant)
    )
    lines.append(
        "夜魇: " + ", ".join(hname(heroes, p.get("hero_id")) for p in dire)
    )
    picks_bans = match.get("picks_bans")
    if isinstance(picks_bans, list) and picks_bans:
        draft = []
        for entry in picks_bans:
            if not isinstance(entry, dict):
                continue
            side = "天辉" if entry.get("team") == 0 else "夜魇"
            action = "选" if entry.get("is_pick") else "禁"
            draft.append(f"{side}{action}{hname(heroes, entry.get('hero_id'))}")
        lines.append("BP 顺序: " + " → ".join(draft))

    # 经济走势
    advance = summarize_gold_advance(match)
    if advance:
        lines.append("")
        lines.append("=== 经济走势（radiant_gold_adv，正=天辉领先，负=夜魇领先）===")
        lines.append(" ".join(advance))
        peak, minute = peak_gold_advance(match)
        side = "天辉" if peak >= 0 else "夜魇"
        lines.append(f"最大领先: {side} {abs(peak) / 1000:.1f}k @ {minute} 分钟")
    xp_adv = match.get("radiant_xp_adv")
    if isinstance(xp_adv, list) and xp_adv:
        sampled = []
        for minute in range(0, len(xp_adv), 3):
            try:
                sampled.append(f"{minute}m {float(xp_adv[minute]) / 1000:+.1f}k")
            except (TypeError, ValueError):
                continue
        if sampled:
            lines.append("经验差采样（每 3 分钟）: " + " ".join(sampled))

    swing = gold_adv_metrics(match)
    if swing:
        lines.append(f"经济领先易手次数: {swing['lead_changes']} 次")
        if swing["winner_behind"] > 0:
            lines.append(
                f"最终胜方曾落后的最大幅度: {swing['winner_behind'] / 1000:.1f}k（翻盘深度）"
            )
        else:
            lines.append("最终胜方全场未曾落后（一路领先）")

    # 关键事件时间轴
    if include_timeline:
        events = summarize_objectives(match.get("objectives") or [])
        if events:
            lines.append("")
            lines.append("=== 关键事件时间轴 ===")
            lines.extend(events)

    # 团战：带上英雄/玩家映射，才能说清「哪场团战谁先死、焦点玩家打出了什么」
    fights = summarize_teamfights(
        match.get("teamfights") or [],
        heroes=heroes,
        players=players,
        focus_ids=focus_ids,
        focus_names={
            int(p.get("account_id") or 0): str(p.get("name") or "") for p in players
        },
    )
    if fights:
        lines.append("")
        lines.append(f"=== 团战（共 {len(match.get('teamfights') or [])} 次，列出前 12 次）===")
        lines.extend(fights)

    # 十人数据
    lines.append("")
    lines.append("=== 十人数据 ===")
    for player in sorted(players, key=lambda p: int(p.get("player_slot") or 0)):
        lines.append(_player_row(player, heroes, item_index))

    # 分路
    lines.append("")
    lines.append("=== 分路信息 ===")
    for player in players:
        detail = _lane_text(player)
        efficiency = lane_efficiency_text(player)
        if efficiency != "-":
            detail += f"　对线补刀效率 {efficiency}"
        lines.append(
            f"{('天辉' if is_radiant(player.get('player_slot')) else '夜魇')} "
            f"{hname(heroes, player.get('hero_id'))}"
            f"（{player.get('name') or player.get('account_id')}）: {detail}"
        )

    # 十人发育节奏对比（只对完整解析产物有效）
    comparison = []
    for player in sorted(players, key=lambda p: int(p.get("player_slot") or 0)):
        points = curve_points_text(player)
        if points:
            comparison.append(
                f"{('天辉' if is_radiant(player.get('player_slot')) else '夜魇')} "
                f"{hname(heroes, player.get('hero_id'))}"
                f"（{player.get('name') or player.get('account_id')}）: {points}"
            )
    if comparison:
        lines.append("")
        lines.append("=== 发育节奏节点对比（累计值，可横向比发育速度）===")
        lines.extend(comparison)

    # 全员视野：判断「哪边视野做得好、辅助有没有干活」的直接证据
    ward_rows = []
    for player in sorted(players, key=lambda p: int(p.get("player_slot") or 0)):
        ward = summarize_wards(player, heroes)
        if ward:
            ward_rows.append(
                f"{('天辉' if is_radiant(player.get('player_slot')) else '夜魇')} "
                f"{hname(heroes, player.get('hero_id'))}"
                f"（{player.get('name') or player.get('account_id')}）: {ward}"
            )
    if ward_rows:
        lines.append("")
        lines.append("=== 视野与守卫 ===")
        lines.extend(ward_rows)

    # 焦点玩家深入数据（可能有多位：同一局里被监听的多位玩家都参战了）
    if focuses:
        lines.append("")
        lines.append("=== 焦点玩家深入数据 ===")
        if len(focuses) > 1:
            lines.append(
                f"本局共有 {len(focuses)} 位焦点玩家（都是被关注的选手），"
                "请在报告中对他们**逐一**深入点评："
                + "、".join(
                    str(p.get("name") or p.get("account_id")) for p in focuses
                )
            )
        for index, focus in enumerate(focuses, start=1):
            if len(focuses) > 1:
                lines.append("")
                lines.append(f"--- 焦点玩家 {index} ---")
            lines.extend(
                _focus_detail_lines(
                    focus, heroes, item_index, radiant_win, ability_map
                )
            )

    # 额外指定的曲线对象（无焦点玩家时也能看到指定玩家的发育走势）
    extra_curve = [
        p
        for p in players
        if int(p.get("account_id") or 0) in curve_id_list
        and not p.get("_focus")
    ]
    if extra_curve:
        lines.append("")
        lines.append("=== 指定玩家发育曲线 ===")
        for player in extra_curve:
            lines.append(
                f"{hname(heroes, player.get('hero_id'))}"
                f"（{player.get('name') or player.get('account_id')}）:"
            )
            lines.extend(
                "　" + row for row in summarize_player_curve(player)
            )

    # 未解析时补充提示（与标题行走同一套判据，不会自相矛盾）
    if not _parsed:
        lines.append("")
        lines.append(
            "注意: 本场尚未被解析，缺少逐分钟经济、团战分布与出装日志，"
            "请避免对「具体时间点的决策」下结论。"
        )

    return "\n".join(lines)


def _num_or_dash(value: Any, digits: int = 1, *, suffix: str = "") -> str:
    """数值缺失时返回 ``-``，而不是把 ``None`` 伪装成 ``0``。

    ``None`` 表示「数据源没给这个字段」（例如未解析时的团战数据），
    与「确实是 0」是完全不同的语义。把它们都渲染成 0 会误导大模型
    得出「这名选手参团率为 0」这种错误结论。
    """
    if value is None or isinstance(value, bool):
        return "-"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "-"
    if digits <= 0:
        return f"{int(round(num))}{suffix}"
    return f"{num:.{digits}f}{suffix}"


def _int_or_dash(value: Any) -> str:
    """整数缺失时返回 ``-``。"""
    if value is None or isinstance(value, bool):
        return "-"
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "-"


def _pct_or_dash(value: Any, digits: int = 1) -> str:
    """把 0~1 的比例渲染成百分比；缺失时返回 ``-``。"""
    if value is None or isinstance(value, bool):
        return "-"
    try:
        return f"{float(value) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "-"


def _match_has_timeseries(match: dict) -> bool:
    """这场比赛是否具备「逐分钟」级别的解析产物。

    两个数据源的产物形态不同，这里统一判定，避免出现「标题说已解析、
    正文说未解析」的自相矛盾：

    - **OpenDota**：解析后会给每名玩家填 ``gold_t``（逐分钟金钱）。
    - **STRATZ**：不给 ``gold_t``，但会给比赛级的 ``radiant_gold_adv`` /
      ``radiant_xp_adv`` 曲线（逐分钟经济/经验差）。

    任一存在即视为「有逐分钟数据」。注意本函数只回答「有没有逐分钟曲线」，
    「是否已解析」的对外口径统一由 :func:`_parsed_state` 给出。
    """
    if not isinstance(match, dict):
        return False
    # STRATZ：比赛级曲线
    for key in ("radiant_gold_adv", "radiant_xp_adv"):
        series = match.get(key)
        if isinstance(series, list) and series:
            return True
    # OpenDota：玩家级逐分钟金钱
    for player in match.get("players") or []:
        if isinstance(player, dict) and player.get("gold_t"):
            return True
    return False


def _has_timeseries(players: list[dict]) -> bool:
    """兼容旧调用点：只按玩家 ``gold_t`` 判断（OpenDota 口径）。

    新代码请优先用 :func:`_match_has_timeseries`，它同时认 STRATZ 的曲线，
    不会误判成「未解析」。
    """
    for player in players:
        if player.get("gold_t"):
            return True
    return False


def parsed_state(match: dict) -> tuple[bool, str]:
    """统一的「解析状态」判定，返回 ``(是否已解析, 人类可读说明)``。

    这是**唯一**的解析口径，标题行与提示词正文都必须用它，否则同一条信息
    在两处会打架。判定顺序：

    1. 有逐分钟曲线（OpenDota ``gold_t`` 或 STRATZ 的经济/经验曲线）→ 已解析；
    2. 否则看 ``od_data.has_parsed``（OpenDota 的解析状态对象）；
    3. 都没有 → 未解析（仅基础统计）。
    """
    if not isinstance(match, dict):
        return False, "未知（无数据）"
    if _match_has_timeseries(match):
        # 区分一下来源，方便排查时看出走的是哪条通道
        dates = []
        for key in ("radiant_gold_adv", "radiant_xp_adv"):
            series = match.get(key)
            if isinstance(series, list) and series:
                dates.append(f"{len(series)} 分钟")
        has_gold_t = any(
            isinstance(p, dict) and p.get("gold_t")
            for p in (match.get("players") or [])
        )
        detail = "含逐分钟经济/经验曲线"
        if dates:
            detail += f"（{dates[0]}）"
        elif has_gold_t:
            detail += "（玩家 gold_t）"
        return True, f"已解析（{detail}）"

    od_data = match.get("od_data")
    if isinstance(od_data, dict) and od_data.get("has_parsed") is True:
        # 声明已解析，却没有拿到任何逐分钟曲线 —— 说明解析产物不完整。
        return True, "已解析（未取到逐分钟曲线，团战/出装日志可能缺失）"

    return False, "未解析（仅基础统计）"


def _focus_win(player: dict, radiant_win: bool) -> bool:
    return radiant_win if is_radiant(player.get("player_slot")) else not radiant_win


def _focus_detail_lines(
    focus: dict,
    heroes: dict[int, dict],
    item_index: ItemIndex,
    radiant_win: bool,
    ability_names: dict[int, str] | None = None,
    *,
    curve_step: int = 3,
) -> list[str]:
    """生成一位焦点玩家的「深入数据」小节。

    这里的原则是**把解析产物榨干**：凡是能影响「这名玩家打得怎么样」判断的
    字段，只要数据源给了，就都翻成可读文本喂进去。整段都是「有则输出、
    无则跳过」，所以未解析的比赛不会出现半截空小节。
    """
    lines: list[str] = []
    lines.append(
        f"玩家: {focus.get('name') or focus.get('account_id')}（account_id={focus.get('account_id')}）"
    )
    lines.append(
        f"英雄: {hname(heroes, focus.get('hero_id'))}　结果: "
        f"{'胜' if _focus_win(focus, radiant_win) else '负'}"
    )
    lines.append(
        f"K/D/A {focus.get('kills')}/{focus.get('deaths')}/{focus.get('assists')} "
        f"（KDA {focus.get('_kda'):.2f}）　等级 {focus.get('level')}"
    )
    lines.append(
        f"GPM {focus.get('gold_per_min')} / XPM {focus.get('xp_per_min')} "
        f"净经济 {fmt_num(focus.get('net_worth') or focus.get('total_gold') or 0)} "
        f"补刀 {focus.get('last_hits')} 反补 {focus.get('denies')}"
    )
    lines.append(
        f"英雄伤害 {fmt_num(focus.get('hero_damage') or 0)} "
        f"建筑伤害 {fmt_num(focus.get('tower_damage') or 0)} "
        f"治疗 {fmt_num(focus.get('hero_healing') or 0)}"
    )
    lines.append(
        f"参团率 {_pct_or_dash(focus.get('teamfight_participation'))} "
        f"控制时长 {_num_or_dash(focus.get('stuns'), 1, suffix='s')} "
        f"击杀建筑 {focus.get('towers_killed', '-')} 击杀肉山 {focus.get('roshans_killed', '-')}"
    )
    lines.append(
        f"假眼 {_int_or_dash(focus.get('obs_placed'))} 真眼 {_int_or_dash(focus.get('sen_placed'))} "
        f"堆野 {_int_or_dash(focus.get('camps_stacked'))} "
        f"吃符 {_int_or_dash(focus.get('rune_pickups'))} "
        f"信使击杀 {focus.get('courier_kills', '-')}"
    )
    lines.append(
        f"分路: {_lane_text(focus)}　"
        f"补刀效率 {lane_efficiency_text(focus)}"
    )

    # ---- 经济与经验：钱和经验到底从哪来 ------------------------------
    gold_from = _breakdown_text(focus.get("gold_reasons"), 6, labels=GOLD_REASONS)
    if gold_from:
        lines.append("经济来源分解: " + gold_from)
    xp_from = _breakdown_text(focus.get("xp_reasons"), 5, labels=XP_REASONS)
    if xp_from:
        lines.append("经验来源分解: " + xp_from)
    money_bits = []
    for label, key in (
        ("总金钱", "total_gold"),
        ("总经验", "total_xp"),
        ("已花费", "gold_spent"),
        ("未花费", "gold"),
        ("野怪击杀", "neutral_kills"),
        ("堆野", "camps_stacked"),
        ("吃符", "rune_pickups"),
    ):
        value = focus.get(key)
        if value is not None:
            money_bits.append(f"{label} {fmt_num(value)}")
    if money_bits:
        lines.append("　".join(money_bits))

    # ---- 输出 / 承伤构成：判断「他到底在打谁、被谁打」----------------
    dealt_by_ability = _breakdown_text(
        focus.get("damage_inflictor"), 6, labeler=ability_label
    )
    if dealt_by_ability:
        lines.append("输出构成（按技能，null 为普攻）: " + dealt_by_ability)
    dealt_by_target = _breakdown_text(
        focus.get("damage"), 6, labeler=lambda k: unit_label(heroes, k)
    )
    if dealt_by_target:
        lines.append("输出构成（按目标单位）: " + dealt_by_target)
    hero_matrix = summarize_damage_targets(focus, heroes, limit=5)
    if hero_matrix:
        lines.append("对英雄伤害明细: " + hero_matrix)
    taken_from = _breakdown_text(
        focus.get("damage_taken"), 6, labeler=lambda k: unit_label(heroes, k)
    )
    if taken_from:
        lines.append("承伤构成（按来源单位）: " + taken_from)
    taken_by_ability = _breakdown_text(
        focus.get("damage_inflictor_received"), 6, labeler=ability_label
    )
    if taken_by_ability:
        lines.append("承伤构成（按技能）: " + taken_by_ability)
    max_hit = focus.get("max_hero_hit")
    if isinstance(max_hit, dict) and max_hit.get("value"):
        lines.append(
            f"单次最高英雄伤害 {fmt_num(max_hit.get('value'))}"
            f"（{fmt_clock(max_hit.get('time'))}，"
            f"{ability_label(max_hit.get('inflictor'))} → "
            f"{hname_by_npc(heroes, max_hit.get('key'))}）"
        )

    # ---- 击杀网络 -------------------------------------------------
    killed = focus.get("killed")
    if isinstance(killed, dict):
        hero_kills = {
            k: v
            for k, v in killed.items()
            if str(k).startswith("npc_dota_hero_")
        }
        if hero_kills:
            ordered = sorted(
                hero_kills.items(), key=lambda kv: (-int(kv[1]), str(kv[0]))
            )
            lines.append(
                "击杀记录: "
                + "、".join(f"{hname_by_npc(heroes, k)}×{v}" for k, v in ordered)
            )
    killed_by = summarize_killed_by(focus, heroes, 5)
    if killed_by:
        lines.append("被谁击杀: " + killed_by)
    kill_log = summarize_kill_log(focus, heroes)
    if kill_log:
        lines.append("击杀时间线: " + kill_log)
    multi = summarize_multi_kills(focus)
    if multi:
        lines.append("连杀记录: " + multi)

    # ---- 生存能力 -------------------------------------------------
    dead_time = dead_time_text(focus)
    if dead_time != "-":
        lines.append("阵亡+复活耗时: " + dead_time)

    # ---- 视野 -----------------------------------------------------
    wards = summarize_wards(focus, heroes)
    if wards:
        lines.append("视野: " + wards)

    # ---- 技能与道具 ------------------------------------------------
    build = summarize_ability_build(focus, ability_names)
    if build:
        lines.append("技能加点顺序: " + build)
    ability_uses = summarize_ability_uses(focus, 8)
    if ability_uses:
        lines.append("技能使用次数: " + ability_uses)
    ability_targets = focus.get("ability_targets")
    if isinstance(ability_targets, dict) and ability_targets:
        bits = []
        for ability, victims in list(ability_targets.items())[:4]:
            if not isinstance(victims, dict) or not victims:
                continue
            top = sorted(
                ((str(k), v) for k, v in victims.items()),
                key=lambda kv: -float(kv[1] or 0),
            )[0]
            bits.append(
                f"{ability_label(ability)} 主要给 {unit_label(heroes, top[0])}×{top[1]}"
            )
        if bits:
            lines.append("技能施放对象: " + "、".join(bits))
    item_uses = summarize_item_uses(focus, 8)
    if item_uses:
        lines.append("道具使用次数: " + item_uses)
    hero_hits = _breakdown_text(focus.get("hero_hits"), 6, labeler=ability_label)
    if hero_hits:
        lines.append("技能命中英雄次数（按技能）: " + hero_hits)

    # ---- 装备 -----------------------------------------------------
    neutral_items = summarize_neutral_items(focus, item_index)
    if neutral_items:
        lines.append("中立物品获取: " + neutral_items)
    buybacks = focus.get("buyback_log")
    if isinstance(buybacks, list) and buybacks:
        lines.append(
            "买活时间: "
            + "、".join(
                fmt_clock(item.get("time"))
                for item in buybacks
                if isinstance(item, dict)
            )
        )
    major_timeline = summarize_item_timeline(focus, item_index, min_cost=2000)
    if major_timeline:
        lines.append("关键装备时间线（≥2000 金币）: " + major_timeline)
    all_timeline = summarize_item_timeline(focus, item_index, min_cost=500)
    if all_timeline and all_timeline != major_timeline:
        lines.append("全部装备时间线（≥500 金币）: " + all_timeline)

    # ---- 逐分钟发育曲线 --------------------------------------------
    curve = summarize_player_curve(focus, curve_step)
    if curve:
        lines.append("逐分钟发育曲线（每 %d 分钟采样）:" % curve_step)
        lines.extend("　" + row for row in curve)

    # ---- 行动节奏 -------------------------------------------------
    pings = focus.get("pings")
    if pings is not None:
        lines.append(f"信号打点次数: {pings}")
    return lines


# ----------------------------------------------------------------------
# 玩家资料 / 英雄统计
# ----------------------------------------------------------------------
def summarize_hero_history(hero_rows: list[dict], min_games: int = 1) -> list[dict]:
    """把 ``/players/{id}/heroes`` 的结果精简成有实际对局的英雄列表。

    Returns:
        形如 ``[{"hero_id": 48, "games": 12, "win": 7, "winrate": 58.3}, ...]``，
        按场次倒序。
    """
    rows: list[dict] = []
    for row in hero_rows or []:
        if not isinstance(row, dict):
            continue
        games = int(row.get("games") or 0)
        if games < min_games:
            continue
        wins = int(row.get("win") or 0)
        rows.append(
            {
                "hero_id": int(row.get("hero_id") or 0),
                "games": games,
                "win": wins,
                "winrate": wins / games * 100 if games else 0.0,
                "last_played": int(row.get("last_played") or 0),
            }
        )
    rows.sort(key=lambda item: item["games"], reverse=True)
    return rows


def format_hero_stats(
    player_name: str,
    hero_rows: list[dict],
    heroes: dict[int, dict],
    top: int = 12,
) -> str:
    """按英雄维度展示玩家统计（**生涯累计口径**）。

    .. note::
        命令层已改用 :mod:`dota_pool` 的版本口径（只看当前版本、含加速模式），
        本函数保留给「确实要看生涯累计」的场景与既有测试。
        直接拿 ``/players/{id}/heroes`` 的默认返回有两个坑：不认版本、
        且 OpenDota 默认把加速模式整套丢掉（详见
        :meth:`dota_api.OpenDotaClient.get_player_heroes`）。

    这里**不展示「最近使用」**：唯一的时间来源是 ``/players/{id}/heroes``
    的 ``last_played``，而该字段实测系统性陈旧（见 :func:`recent_hero_usage`
    的说明），照抄会把「昨天刚打过」的英雄标成「3 年前用过」。
    要近期时效请走 :func:`recent_hero_usage`（需要近期对局列表）。
    """
    rows = summarize_hero_history(hero_rows)
    if not rows:
        return f"没有查询到 {player_name} 的英雄使用记录。"

    total_games = sum(row["games"] for row in rows)
    lines = [
        f"🦸 {player_name} 的英雄统计（共 {len(rows)} 个英雄、{total_games} 场有效对局）",
        "",
    ]
    for index, row in enumerate(rows[:top], start=1):
        lines.append(
            f"{index:>2}. {hname(heroes, row['hero_id'])}　{row['games']} 场 "
            f"{row['win']} 胜　胜率 {row['winrate']:.1f}%"
        )
    if len(rows) > top:
        lines.append(f"... 以及另外 {len(rows) - top} 个英雄")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# 英雄版本胜率（「轮椅」榜）
# ----------------------------------------------------------------------

#: 官方英雄定位 → 中文。取值就是 ``/heroStats`` 里的 ``roles``。
HERO_ROLE_ZH: dict[str, str] = {
    "Carry": "核心",
    "Support": "辅助",
    "Initiator": "先手",
    "Durable": "耐久",
    "Disabler": "控制",
    "Nuker": "爆发",
    "Escape": "灵动",
    "Pusher": "推进",
}

#: 主属性 → 中文。Dota 现在还有「全才」英雄（``all``）。
HERO_ATTR_ZH: dict[str, str] = {
    "str": "力量",
    "agi": "敏捷",
    "int": "智力",
    "all": "全才",
}

#: 口语位置词 → 内部类目。
#:
#: .. warning::
#:   ``roles`` 是**官方英雄定位**，不是分路统计。用它推位置只能是近似：
#:   带 ``Carry`` 的通常走 1/2 号位、带 ``Support`` 的通常走 4/5 号位，
#:   但一个常年打辅助的力量英雄同样带 ``Durable``，会被归进「三号位」。
#:   所以报告里必须写明这是「按官方定位近似」，不能让人当成分路数据。
POSITION_ALIASES: dict[str, tuple[str, ...]] = {
    "core": (
        "核心", "大哥", "1号位", "一号位", "2号位", "二号位", "中单", "carry", "c位",
    ),
    "offlane": ("三号位", "3号位", "劣单", "上单", "offlane", "前排", "肉盾"),
    "support": (
        "辅助", "酱油", "4号位", "四号位", "5号位", "五号位", "support", "挂件",
    ),
}

#: 内部类目 → 给人看的名字
POSITION_LABELS: dict[str, str] = {
    "core": "核心（1/2 号位）",
    "offlane": "三号位",
    "support": "辅助（4/5 号位）",
}


def fmt_wan(value: Any) -> str:
    """把大数字缩写成「49.3 万」（中文场景比 ``k`` 好读）。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "-"
    if abs(num) >= 10000:
        return f"{num / 10000:.1f} 万"
    return f"{num:.0f}"


def match_position(text: str) -> str:
    """认出参数里的位置词，返回 ``core`` / ``offlane`` / ``support``。

    只做**整词**匹配（去掉空白后与别名完全相等）。不这样做的话，
    「中」这种单字会把玩家昵称咬掉一块 —— 而这个参数位也可能是昵称。
    """
    token = "".join(str(text or "").split()).lower()
    if not token:
        return ""
    for position, aliases in POSITION_ALIASES.items():
        if token in aliases:
            return position
    return ""


def _in_position(roles: list[str], position: str) -> bool:
    """按官方定位粗略判断英雄是否属于某个位置类目。

    用**互斥**判定，而不是「挂了某个标签就算」：官方 ``roles`` 里不少英雄同时
    挂着 ``Carry`` 与 ``Support``（骷髅王两个都有），只看「含 Carry」会让两边
    的榜单混进同一个英雄。这里要求标签**不冲突**才收 —— 宁可漏掉几个打法兼容
    的英雄，也不能把辅助英雄摆进核心榜榜首。
    """
    if not position:
        return True
    values = {str(role) for role in (roles or [])}
    is_carry = "Carry" in values
    is_support = "Support" in values
    if position == "core":
        return is_carry and not is_support
    if position == "support":
        return is_support and not is_carry
    if position == "offlane":
        # 三号位没有对应标签，只能用「耐久 / 先手」近似；
        # 带核心或辅助标签的一律排除，否则幽鬼会被当成三号位。
        return (
            ("Durable" in values or "Initiator" in values)
            and not is_carry
            and not is_support
        )
    return True


#: 趋势至少要多少样本才给结论。几百场的胜率波动纯属噪声，
#: 拿去说「某某正在变强」会误导人。
TREND_MIN_SAMPLES = 200


def hero_meta_rows(
    hero_stats: list[dict],
    *,
    position: str = "",
    min_pick: int | None = None,
) -> tuple[list[dict], dict[str, Any]]:
    """把 ``/heroStats`` 的原始行整理成可排序的榜单行。

    只使用 ``pub_pick`` / ``pub_win``（全分段公开对局）与它们的逐日趋势。

    .. note::
       接口还带 ``1_pick`` … ``7_pick`` 这套**分档**字段，但项目里**不启用**：
       实测各档的样本分布与真实天梯分布对不上（且第 8 档恒为 0），
       编号到段位的映射无法验证。与其猜一个映射去误导人，不如不用。

    Args:
        hero_stats: ``/heroStats`` 的原始返回。
        position: ``core`` / ``offlane`` / ``support``，空串表示不过滤。
        min_pick: 最小场次门槛；``None`` 或 ``<=0`` 表示按样本中位数自适应
            （版本刚发布时总样本会骤降，写死门槛会出空榜）。

    Returns:
        ``(rows, meta)``。``rows`` 按胜率降序；``meta`` 是口径信息，供渲染与
        提示词标注使用。
    """
    rows: list[dict] = []
    for raw in hero_stats or []:
        if not isinstance(raw, dict):
            continue
        try:
            hero_id = int(raw.get("id"))
        except (TypeError, ValueError):
            continue
        roles = [str(role) for role in (raw.get("roles") or [])]
        if not _in_position(roles, position):
            continue
        pick = int(raw.get("pub_pick") or 0)
        win = int(raw.get("pub_win") or 0)
        if pick <= 0:
            continue

        wins_trend = [int(value or 0) for value in (raw.get("pub_win_trend") or [])]
        picks_trend = [int(value or 0) for value in (raw.get("pub_pick_trend") or [])]
        recent_winrate: float | None = None
        trend: float | None = None
        if len(wins_trend) >= 4 and len(picks_trend) == len(wins_trend):
            recent_pick = sum(picks_trend[-3:])
            recent_win = sum(wins_trend[-3:])
            early_pick = sum(picks_trend[:-3])
            early_win = sum(wins_trend[:-3])
            if recent_pick >= TREND_MIN_SAMPLES and early_pick >= TREND_MIN_SAMPLES:
                recent_winrate = recent_win / recent_pick
                trend = recent_winrate - early_win / early_pick

        rows.append(
            {
                "hero_id": hero_id,
                "npc": str(raw.get("name") or ""),
                "english_name": str(raw.get("localized_name") or ""),
                "roles": roles,
                "attr": str(raw.get("primary_attr") or ""),
                "pick": pick,
                "win": win,
                "winrate": win / pick,
                "recent_winrate": recent_winrate,
                "trend": trend,
            }
        )

    picks_sorted = sorted(row["pick"] for row in rows)
    median = picks_sorted[len(picks_sorted) // 2] if picks_sorted else 0
    if min_pick is None or int(min_pick) <= 0:
        # 中位数的 10%：剔掉「两万场」这种冷门小样本，同时保住主流英雄。
        # 写死绝对量的门槛（例如 2 万场）会在版本刚发布时出空榜，
        # 所以再压一个 200 场的绝对下限，随样本总量自动缩放。
        threshold = max(200, int(median * 0.1))
        threshold_source = "auto"
    else:
        threshold = int(min_pick)
        threshold_source = "config"

    kept = [row for row in rows if row["pick"] >= threshold]
    kept.sort(key=lambda row: (-row["winrate"], -row["pick"]))
    meta: dict[str, Any] = {
        "window_days": 7,
        "threshold": threshold,
        "threshold_source": threshold_source,
        "median_pick": median,
        "heroes_total": len(hero_stats or []),
        "heroes_pool": len(rows),
        "heroes_kept": len(kept),
        "total_pick": sum(row["pick"] for row in kept),
        "position": position,
    }
    return kept, meta


def hero_meta_note(meta: dict[str, Any], patch: dict | None = None) -> str:
    """渲染口径说明（含版本号与「窗口混版本」提醒）。"""
    days = int(meta.get("window_days") or 7)
    total_pick = int(meta.get("total_pick") or 0)
    # 位置过滤时「过门槛数 / 全部英雄数」会误导（分母是全英雄），
    # 所以分母用位置内的候选数。
    pool = int(meta.get("heroes_pool") or meta.get("heroes_total") or 0)
    parts = [
        f"📊 口径：最近 {days} 天全分段公开对局",
        f"{meta.get('heroes_kept', 0)}/{pool} 个英雄过门槛",
        f"约 {fmt_wan(total_pick / 10)}场",
    ]
    position = str(meta.get("position") or "")
    if position:
        parts.append(f"位置：{POSITION_LABELS.get(position, position)}")

    threshold = int(meta.get("threshold") or 0)
    source = str(meta.get("threshold_source") or "auto")
    if source == "config":
        gate = f"门槛 ≥ {fmt_wan(threshold)}场（配置）"
    else:
        gate = f"门槛 ≥ {fmt_wan(threshold)}场（按样本中位数自适应）"

    lines = ["　· ".join(parts), f"　{gate}"]

    # ⚠️ 统计窗口是「滚动 7 天」，不是按补丁切 —— 补丁刚发布时窗口里混着旧版本
    patch_name = str((patch or {}).get("name") or "")
    patch_date = str((patch or {}).get("date") or "")
    if patch_name:
        fresh = _patch_age_days(patch_date)
        if fresh is not None and fresh < days:
            lines.append(
                f"　⚠️ 补丁 {patch_name} 才发布 {fresh} 天，而统计窗口是最近 {days} 天，"
                "数据里混着上个版本的对局，只作参考。"
            )
    return "\n".join(lines)


def _patch_age_days(patch_date: str) -> int | None:
    """补丁发布日期距今天数；解析不了返回 ``None``（不猜）。"""
    text = str(patch_date or "").strip()
    if not text:
        return None
    import datetime

    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            when = datetime.datetime.strptime(text[: len(fmt) + 6], fmt).replace(
                tzinfo=datetime.timezone.utc
            )
        except ValueError:
            continue
        delta = datetime.datetime.now(datetime.timezone.utc) - when
        return max(0, int(delta.total_seconds() // 86400))
    return None


def hero_meta_label(row: dict, heroes: dict[int, dict] | None = None) -> str:
    """英雄显示名：优先用已本地化的常量表，回退到接口自带的英文名。"""
    if heroes:
        name = hname(heroes, row.get("hero_id"))
        if name and not name.startswith("英雄#"):
            return name
    return str(row.get("english_name") or f"英雄#{row.get('hero_id')}")


def _hero_meta_line(row: dict, heroes: dict[int, dict] | None, index: int) -> str:
    """榜单的一行：`` 1. 冥魂大帝　54.9%　50.6 万场　力量·核心/耐久　↑0.3``"""
    roles = "/".join(
        HERO_ROLE_ZH.get(role, role) for role in (row.get("roles") or [])[:3]
    )
    tags = "·".join(
        part
        for part in (
            HERO_ATTR_ZH.get(row.get("attr") or "", ""),
            roles,
        )
        if part
    )
    trend = row.get("trend")
    if trend is None:
        trend_text = ""
    else:
        delta = trend * 100
        if abs(delta) < 0.2:
            trend_text = "　→持平"
        else:
            arrow = "↑" if delta > 0 else "↓"
            trend_text = f"　{arrow}{abs(delta):.1f}"
    return (
        f"{index:>2}. {hero_meta_label(row, heroes)}　{row['winrate'] * 100:.1f}%"
        f"　{fmt_wan(row['pick'])}场"
        + (f"　{tags}" if tags else "")
        + trend_text
    )


def format_hero_meta_board(
    rows: list[dict],
    meta: dict[str, Any],
    heroes: dict[int, dict] | None = None,
    *,
    patch: dict | None = None,
    top: int = 10,
    hot_top: int = 5,
    cold_top: int = 5,
) -> str:
    """渲染「版本轮椅榜」：胜率最高 + 热门里最横的 + 垫底的。

    Args:
        rows: :func:`hero_meta_rows` 的输出（已按胜率降序）。
        meta: 同上的口径信息。
        heroes: 英雄常量表（用于中文名）。
        patch: 最新补丁信息，用来标注版本号。
        top: 胜率榜长度。
        hot_top: 「热门里最横的」条数。
        cold_top: 「悠着点」条数，``0`` 表示不显示。
    """
    if not rows:
        return "没有拿到英雄版本数据（数据源可能暂时不可用），稍后再试。"

    position = str(meta.get("position") or "")
    patch_name = str((patch or {}).get("name") or "")
    title = f"🏆 版本轮椅榜 · {patch_name}" if patch_name else "🏆 版本轮椅榜"
    if position:
        title += f"（{POSITION_LABELS.get(position, position)}）"

    lines = [title, hero_meta_note(meta, patch), ""]

    hottest = rows[:top]
    lines.append(f"【胜率最高 TOP {len(hottest)}】")
    for index, row in enumerate(hottest, start=1):
        lines.append(_hero_meta_line(row, heroes, index))

    # 「悠着点」要保住「真正的垫底」这个语义，所以**先算它**，再让「热门」避开它。
    # 反过来（热门先挑、垫底再避开热门）会出问题：位置筛选后池子本来就小
    # （三号位只过门槛 15 个），「热门」那段会把整个池子都扫进去，于是一批
    # 胜率 44~46% 的英雄同时出现在「热门里最横的」和「悠着点」两段 —— 用户
    # 看到同一批名字被两头点名，只会觉得榜单坏了。谁更该说真话？
    # 「悠着点」的职责就是点名最差的三个，让位给「热门」就等于说谎。
    shown = {row["hero_id"] for row in hottest}
    cold: list[dict] = []
    if cold_top > 0:
        cold = [row for row in rows if row["hero_id"] not in shown][-cold_top:]
        cold.reverse()
        shown |= {row["hero_id"] for row in cold}

    # 「轮椅」的本义是「大家都在玩、而且真的能赢」：只看胜率会把
    # 冷门绝活英雄（样本小、胜率高）顶上榜首，那不是轮椅。这里从**场次最高**
    # 的一批里再筛胜率，两层都满足才是真·轮椅。
    hot: list[dict] = []
    hot_pool_size = 0
    if hot_top > 0:
        hot_pool_size = max(15, len(rows) // 5)
        by_pick = sorted(rows, key=lambda row: -row["pick"])[:hot_pool_size]
        hot = [row for row in by_pick if row["hero_id"] not in shown]
        hot.sort(key=lambda row: -row["winrate"])
        hot = hot[:hot_top]

    if hot:
        lines.append("")
        # 池子本身不到扫描条数时，说「场次前 N 名」会让人以为还有更热门的没被算进来
        scope = (
            f"全部 {len(rows)} 个过门槛英雄"
            if hot_pool_size >= len(rows)
            else f"场次前 {hot_pool_size} 名"
        )
        lines.append(f"【热门里最横的】（{scope}中胜率最高）")
        for index, row in enumerate(hot, start=1):
            lines.append(_hero_meta_line(row, heroes, index))

    if cold:
        lines.append("")
        lines.append("【悠着点】（胜率垫底，非绝活慎选）")
        for index, row in enumerate(cold, start=1):
            lines.append(_hero_meta_line(row, heroes, index))

    return "\n".join(lines).rstrip()


def format_hero_meta_footer(meta: dict[str, Any]) -> str:
    """榜单尾部提示（告诉用户还能怎么用）。"""
    return (
        "💡 想按位置看，用 `/d2 轮椅 辅助`（或 核心 / 三号位）；\n"
        "想看自己该练哪个，用 `/d2 轮椅 我`（或 `/d2 轮椅 <昵称>`）。"
    )


#: 判断「最近还在玩」的天数门槛
RECENT_PLAY_DAYS = 30

#: 「他玩过这个英雄」至少要几场才算有效样本。低于此的只作参考，
#: 不能据此下「他擅长 / 不擅长」的结论（1 场 100% 和 1 场 0% 都不是信息）。
MIN_HERO_SAMPLE_GAMES = 3


def recent_hero_usage(
    matches: list[dict], days: int = RECENT_PLAY_DAYS
) -> tuple[dict[int, int], dict[int, int]]:
    """从**近期对局列表**统计每个英雄的出场次数与最近一次开赛时间。

    Returns:
        ``(counts, latest)``：``counts[hero_id]`` 是出场次数，
        ``latest[hero_id]`` 是最近一次开赛时间戳。``days <= 0`` 表示不按时间过滤。

    **为什么不用 ``/players/{id}/heroes`` 的 ``last_played``**（实测取证）：
    该字段**系统性陈旧**，不能用来判断「最近还在玩吗」。实测账号 153659639
    近 20 场打过的 18 个英雄，池内 ``last_played`` **全部**早于实际出场
    （主宰的真实最近出场是 2026-09-13，池内写 2024-07-26），整个池子里
    最新的 ``last_played`` 停在 69 天前 —— 而那个账号三天前还在打。
    用它分类会把当下最常玩的英雄判成「很久没动」，进而给出反向建议。

    近期对局是我们自己拉的，时间取自比赛本身的 ``start_time``，
    以它为准才是可辩护的口径。
    """
    counts: dict[int, int] = {}
    latest: dict[int, int] = {}
    now = time.time()
    for match in matches or []:
        try:
            hero_id = int(match.get("hero_id"))
        except (TypeError, ValueError):
            continue
        start = int(match.get("start_time") or 0)
        if days > 0 and start and (now - start) > days * 86400:
            continue
        counts[hero_id] = counts.get(hero_id, 0) + 1
        if start > latest.get(hero_id, 0):
            latest[hero_id] = start
    return counts, latest

#: 从主力英雄推断玩家偏好时，一个标签至少要在几个英雄上出现
ROLE_HINT_MIN = 2


def _player_role_hints(
    pool: list[dict], heroes: dict[int, dict] | None, limit: int = 8
) -> set[str]:
    """从主力英雄的官方定位反推玩家偏好标签。

    一个英雄的 ``roles`` 不能说明什么（很多英雄横跨好几种定位），但**一批**
    主力英雄反复出现的标签（≥ :data:`ROLE_HINT_MIN` 个）就足以说明他平时
    在玩哪一类。
    """
    counter: dict[str, int] = {}
    for row in (pool or [])[:limit]:
        info = (heroes or {}).get(row.get("hero_id")) or {}
        for role in info.get("roles") or []:
            key = str(role)
            counter[key] = counter.get(key, 0) + 1
    return {role for role, count in counter.items() if count >= ROLE_HINT_MIN}


def pick_heroes_for_player(
    meta_rows: list[dict],
    hero_rows: list[dict],
    matches: list[dict],
    heroes: dict[int, dict] | None = None,
    *,
    top: int = 3,
    board_size: int = 30,
) -> str:
    """规则版推荐：在版本强势英雄里，按「他已经会什么」挑几个出来。

    这是大模型不可用时的降级方案，只做**数据交叉**、不做主观解读：

    * **已经在玩**：他玩过 ≥3 场，且出现在近期对局里；
    * **会玩但没动了**：玩过 ≥3 场，但近期对局里没出现；
    * **没玩过但定位相近**：版本榜靠前，且官方定位与他主力英雄的标签有交集。

    三类都只在版本榜 ``board_size`` 名以内挑，不碰榜外英雄 —— 与提示词里
    对模型的要求共用同一条口径。全空时返回空串，由调用方决定怎么措辞。

    「近期」以 :func:`recent_hero_usage` 从 ``matches`` 现算，**不用**
    ``/players/{id}/heroes`` 的 ``last_played``（实测该字段系统性陈旧，
    详见该函数说明）。
    """
    pool = summarize_hero_history(hero_rows)
    if not meta_rows:
        return ""
    by_id = {row["hero_id"]: row for row in meta_rows}
    rank_index = {row["hero_id"]: rank for rank, row in enumerate(meta_rows, start=1)}
    board_ids = [row["hero_id"] for row in meta_rows[:board_size]]
    played = {row["hero_id"]: row for row in pool}
    recent_counts, _recent_latest = recent_hero_usage(matches)
    split = f"最近 {len(matches)} 场" if matches else "近期"

    playing: list[tuple[int, dict, dict]] = []
    idle: list[tuple[int, dict, dict]] = []
    for hero_id in board_ids:
        entry = played.get(hero_id)
        if entry is None or entry["games"] < MIN_HERO_SAMPLE_GAMES:
            continue
        fresh = recent_counts.get(hero_id, 0) > 0
        (playing if fresh else idle).append((rank_index[hero_id], by_id[hero_id], entry))

    hints = _player_role_hints(pool, heroes)
    never: list[tuple[int, dict, dict | None]] = []
    for hero_id in board_ids:
        if hero_id in played:
            continue
        row = by_id[hero_id]
        if hints and not (set(row.get("roles") or []) & hints):
            continue
        never.append((rank_index[hero_id], row, None))

    playing.sort(key=lambda item: item[0])
    idle.sort(key=lambda item: item[0])
    never.sort(key=lambda item: item[0])

    def render(rank: int, row: dict, entry: dict | None) -> str:
        name = hero_meta_label(row, heroes)
        if entry:
            hero_id = row["hero_id"]
            mine = f"你玩过 {entry['games']} 场，胜率 {entry['winrate']:.0f}%"
            hits = recent_counts.get(hero_id, 0)
            mine += f"（{split}出场 {hits} 次）" if hits else f"（{split}未出现）"
        else:
            mine = "你没用过"
        return f"  {name} — 版本第 {rank}（{row['winrate'] * 100:.1f}%）· {mine}"

    sections: list[tuple[str, list]] = [
        ("【已经在玩、版本又强】", playing[:top]),
        ("【会玩但最近没动】", idle[:top]),
        ("【没玩过、定位相近】", never[:top]),
    ]
    lines: list[str] = []
    for title, items in sections:
        if not items:
            continue
        if lines:
            lines.append("")
        lines.append(title)
        lines.extend(render(rank, row, entry) for rank, row, entry in items)

    if not lines:
        return ""
    lines.append("")
    lines.append(
        "（未启用大模型分析，以上只做了数据交叉，没有额外解读）"
    )
    return "\n".join(lines)


def format_player_profile(
    profile_data: dict,
    wl: dict,
    hero_rows: list[dict],
    heroes: dict[int, dict],
) -> str:
    """把玩家资料整理成可读文本。"""
    profile = profile_data.get("profile") or {}
    personaname = profile.get("personaname") or "未知玩家"
    account_id = profile.get("account_id")
    steam_id = profile.get("steamid") or (to_steam_id64(account_id) if account_id else "")

    lines = [f"👤 {personaname}"]
    if profile.get("name"):
        lines.append(f"曾用昵称/备注: {profile['name']}")
    lines.append(f"账号 ID: {account_id}")
    if steam_id:
        lines.append(f"SteamID64: {steam_id}")
    if profile.get("profileurl"):
        lines.append(f"个人主页: {profile['profileurl']}")
    if profile.get("loccountrycode"):
        lines.append(f"地区: {profile['loccountrycode']}")

    lines.append(f"段位: {rank_text(profile_data.get('rank_tier'))}")
    leaderboard = profile_data.get("leaderboard_rank")
    if leaderboard:
        lines.append(f"排行榜名次: 第 {leaderboard} 名")
    if profile.get("plus"):
        lines.append("Dota Plus: 已订阅")

    wins = int(wl.get("win") or 0)
    loses = int(wl.get("lose") or 0)
    total = wins + loses
    if total:
        lines.append(
            f"生涯战绩: {total} 场 · {wins} 胜 {loses} 负 · 胜率 {wins / total * 100:.1f}%"
        )

    rows = summarize_hero_history(hero_rows)
    if rows:
        top = "、".join(
            f"{hname(heroes, row['hero_id'])} {row['games']}场/{row['winrate']:.0f}%"
            for row in rows[:5]
        )
        lines.append(f"最常用英雄: {top}")
    return "\n".join(lines)


def to_steam_id64(account_id: Any) -> str:
    """32 位 account_id → 64 位 SteamID。"""
    try:
        return str(int(account_id) + 76561197960265728)
    except (TypeError, ValueError):
        return ""


def _popcount(value: Any, mask: int) -> int:
    """位掩码里被置位的数量（用于塔/兵营存活状态）。"""
    try:
        return bin(int(value) & mask).count("1")
    except (TypeError, ValueError):
        return 0


def building_states_text(match: dict) -> str:
    """由塔 / 兵营的状态位掩码推算出「丢了多少、还剩多少」。

    **口径（踩过坑，别再改回去）**：OpenDota 的 ``tower_status_*`` /
    ``barracks_status_*`` 位掩码里，**置位 = 这座建筑还活着（standing）**，
    不是「已被摧毁」。``0x7FF``（11 位全 1）= 11 座塔全在，``0`` = 全丢。

    取证方式（两处独立字段互证）：样本 ``8995536921`` 里
    ``tower_status_radiant = 54``（popcount 4）、``tower_status_dire = 1974``
    （popcount 8），而 ``objectives`` 的 ``building_kill`` 事件显示天辉被拆
    7 座、夜魇被拆 3 座 —— ``11 - 7 = 4``、``11 - 3 = 8``，正好等于置位数。
    兵营同理（``barracks_status_radiant = 15`` → 4 存活，而事件只拆了
    下路近战/远程两座）。
    """
    parts: list[str] = []
    tower_mask = 0x7FF  # 11 座塔（三路各 3 座 + 2 座四塔）
    barracks_mask = 0x3F  # 6 座兵营（三路各 近战/远程）
    for key, label, mask, total in (
        ("tower_status_radiant", "天辉塔", tower_mask, 11),
        ("tower_status_dire", "夜魇塔", tower_mask, 11),
        ("barracks_status_radiant", "天辉兵营", barracks_mask, 6),
        ("barracks_status_dire", "夜魇兵营", barracks_mask, 6),
    ):
        if match.get(key) is None:
            continue
        alive = _popcount(match.get(key), mask)
        lost = total - alive
        if lost >= total:
            parts.append(f"{label} 全丢（{total}/{total}）")
        elif lost:
            parts.append(f"{label} 丢 {lost}/{total}（剩 {alive}）")
        else:
            parts.append(f"{label} 完好（{total}/{total}）")
    return "　".join(parts)


def match_quality_block(match: dict) -> str:
    """比赛质量评估用到的客观指标。"""
    players = match.get("players") or []
    duration = int(match.get("duration") or 0)
    total_kills = int(match.get("radiant_score") or 0) + int(match.get("dire_score") or 0)
    peak, minute = peak_gold_advance(match)
    fights = match.get("teamfights")

    rows = [
        f"总击杀数: {total_kills}（{(total_kills / max(1, duration / 60)):.2f} 次/分钟）",
        f"时长: {fmt_duration(duration)}",
        f"最大经济领先: {'天辉' if peak >= 0 else '夜魇'} {abs(peak) / 1000:.1f}k @ {minute} 分钟",
    ]
    # 数据源自带的翻盘/碾压指标（与上面自行推导的口径可以互相印证）
    comeback = match.get("comeback")
    stomp = match.get("stomp")
    if comeback is not None and stomp is not None:
        rows.append(
            f"数据源判定: 最终输方全场最大领先 {float(comeback) / 1000:.1f}k（翻盘深度）、"
            f"最终胜方全场最大领先 {float(stomp) / 1000:.1f}k（碾压程度）"
        )
    # 团战数据只在真的解析出来时才可用。未解析时 teamfights 缺失/为空，
    # 此时必须说「不可用」；说「0 次」会让模型以为这局没有团战。
    if isinstance(fights, list) and fights:
        rows.append(f"团战次数（5 人以上交战）: {len(fights)}")
    else:
        rows.append("团战数据: 不可用（该局未产出团战解析数据，请勿据此推断团战次数）")
    swing = gold_adv_metrics(match)
    if swing:
        rows.append(f"经济领先易手次数: {swing['lead_changes']} 次")
        if swing["winner_behind"] > 0:
            rows.append(
                f"最终胜方曾落后的最大幅度（翻盘深度）: {swing['winner_behind'] / 1000:.1f}k"
            )
        else:
            rows.append("最终胜方全场未曾落后（一路领先/碾压局）")
        rows.append(f"终局经济差: {swing['final_margin'] / 1000:+.1f}k（正=天辉）")
    # 塔 / 兵营 / 视野 / 经济总量：判断「优势有没有转化成推进」的关键
    buildings = building_states_text(match)
    if buildings:
        rows.append("建筑存活: " + buildings)
    sides = {"天辉": [], "夜魇": []}
    for player in players:
        if not isinstance(player, dict):
            continue
        sides["天辉" if is_radiant(player.get("player_slot")) else "夜魇"].append(player)
    if all(sides.values()):
        net_lines = []
        for side, members in sides.items():
            net = 0
            obs = 0
            sen = 0
            for member in members:
                try:
                    net += int(member.get("net_worth") or member.get("total_gold") or 0)
                except (TypeError, ValueError):
                    pass
                try:
                    obs += int(member.get("obs_placed") or 0)
                    sen += int(member.get("sen_placed") or 0)
                except (TypeError, ValueError):
                    pass
            net_lines.append(f"{side} 总净经济 {net / 1000:.1f}k、合计插眼 {obs}假/{sen}真")
        rows.append("　".join(net_lines))
    deaths = sum(int(p.get("deaths") or 0) for p in players if isinstance(p, dict))
    rows.append(f"十人合计阵亡: {deaths}")
    average_rank = match.get("average_rank")
    if average_rank:
        rows.append(f"双方平均段位: {rank_text(average_rank)}")
    human = match.get("human_players")
    if human is not None:
        rows.append(f"真人玩家数: {human}/10")
    league = match.get("leagueid")
    series = match.get("series_id")
    if league:
        rows.append(f"联赛对局: leagueid={league}" + (f" series_id={series}" if series else ""))
    if match.get("pre_game_duration"):
        rows.append(f"赛前准备时长: {fmt_duration(match.get('pre_game_duration'))}")
    return "\n".join(rows)
