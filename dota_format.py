"""把 OpenDota 的原始数据整理成「给人看的文本」与「给大模型看的结构化文本」。

本模块不依赖 AstrBot，纯函数式，方便单独测试。
"""

from __future__ import annotations

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
    ("rax_melee", "近战兵营"),
    ("rax_ranged", "远程兵营"),
    ("rax", "兵营"),
    ("shrine", "圣坛"),
    ("fillers", "建筑"),
]

LANE_HINTS: list[tuple[str, str]] = [
    ("top", "上路"),
    ("mid", "中路"),
    ("bot", "下路"),
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


def summarize_teamfights(teamfights: list[dict], limit: int = 12) -> list[str]:
    """整理团战列表。"""
    rows: list[str] = []
    for index, fight in enumerate(teamfights or [], start=1):
        if not isinstance(fight, dict):
            continue
        if index > limit:
            break
        start = fmt_clock(fight.get("start"))
        end = fmt_clock(fight.get("end"))
        deaths = fight.get("deaths", 0)
        players = fight.get("players") or []
        radiant_gold = 0
        dire_gold = 0
        for slot, player in enumerate(players):
            if not isinstance(player, dict):
                continue
            try:
                delta = int(player.get("gold_delta") or 0)
            except (TypeError, ValueError):
                delta = 0
            # teamfights.players 按 player_slot 顺序排列（0-4 天辉，128-132 夜魇）
            if slot < 5:
                radiant_gold += delta
            else:
                dire_gold += delta
        rows.append(
            f"#{index} {start}-{end} 阵亡 {deaths} 人 · 天辉合计经济 {radiant_gold:+d} "
            f"/ 夜魇 {dire_gold:+d}"
        )
    return rows


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
    neutral = item_index.name(player.get("item_neutral"), default="")
    if neutral:
        inventory.append(f"[中立]{neutral}")

    return (
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


def _lane_text(player: dict) -> str:
    role = player.get("lane_role")
    lane = player.get("lane")
    parts = []
    if role:
        parts.append(LANE_ROLES.get(int(role), f"位置{role}"))
    if lane:
        parts.append(f"lane={lane}")
    if player.get("is_roaming"):
        parts.append("游走")
    return " ".join(parts) or "未识别"


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
) -> str:
    """把单场比赛整理成详尽的文本，供大模型复盘使用。

    ``focus_account_ids`` 支持传一位或多位焦点玩家（同一局里可能有多位被监听
    的玩家参战）；每位焦点玩家都会得到一份独立的「深入数据」小节。
    """
    items = items or {}
    item_index = ItemIndex(items)
    players = [p for p in (match.get("players") or []) if isinstance(p, dict)]

    radiant = [p for p in players if is_radiant(p.get("player_slot"))]
    dire = [p for p in players if not is_radiant(p.get("player_slot"))]
    radiant_win = bool(match.get("radiant_win"))
    winner = "天辉" if radiant_win else "夜魇"

    focus_ids = normalize_focus_ids(focus_account_ids)

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
        lines.append(f"region: {region}　cluster: {cluster}")
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
        for minute in range(0, len(xp_adv), 6):
            try:
                sampled.append(f"{minute}m {float(xp_adv[minute]) / 1000:+.1f}k")
            except (TypeError, ValueError):
                continue
        if sampled:
            lines.append("经验差采样: " + " ".join(sampled))

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

    # 团战
    fights = summarize_teamfights(match.get("teamfights") or [])
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
        lines.append(
            f"{('天辉' if is_radiant(player.get('player_slot')) else '夜魇')} "
            f"{hname(heroes, player.get('hero_id'))}: {_lane_text(player)}"
        )

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
            lines.extend(_focus_detail_lines(focus, heroes, item_index, radiant_win))

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
) -> list[str]:
    """生成一位焦点玩家的「深入数据」小节。"""
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
        f"补刀效率 {_pct_or_dash(focus.get('lane_efficiency_pct'))}"
    )
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
    # 大件时间线
    log = focus.get("purchase_log")
    if isinstance(log, list) and log:
        majors = []
        for entry in log:
            if not isinstance(entry, dict):
                continue
            key = entry.get("key")
            if item_index.cost(key) >= 2000:
                majors.append(
                    f"{item_index.name(key, default=str(key))}@{fmt_clock(entry.get('time'))}"
                )
        if majors:
            lines.append("关键装备时间线: " + "、".join(majors))
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
    """按英雄维度展示玩家统计。"""
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
            + (f"　（最近使用 {fmt_ago(row['last_played'])}）" if row["last_played"] else "")
        )
    if len(rows) > top:
        lines.append(f"... 以及另外 {len(rows) - top} 个英雄")
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
    deaths = sum(int(p.get("deaths") or 0) for p in players if isinstance(p, dict))
    rows.append(f"十人合计阵亡: {deaths}")
    return "\n".join(rows)
