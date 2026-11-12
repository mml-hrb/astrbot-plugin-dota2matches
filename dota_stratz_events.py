"""STRATZ 原始事件流 → 业务层已经在用的 OpenDota 方言。

为什么单独成一个模块
--------------------
``dota_stratz.StratzClient`` 只负责「HTTP + 字段改名」，而 STRATZ 的
**事件流**（``killEvents`` / ``deathEvents`` / ``wards`` / ``itemPurchases`` /
``buildingEvents`` / ``roshanEvents``）在 OpenDota 那边是被**聚合**过的
（``teamfights`` / ``objectives`` / ``purchase_log`` / ``obs_log``）。
要让上层的渲染与提示词一行都不用改，就必须把原料聚合成同样的形状 ——
这部分是纯函数、无 IO，单独放一个文件才好测。

三条铁律
--------
1. **只映射有证据的**。聊天内容（``chatEvents[*].value`` 恒为 0，没有文本）、
   塔的路名与等级（只有 ``npcId`` 与坐标，没有可信对照表）、反补（``csEvents``
   全是正补，``numDenies`` 对不上任何事件）—— 这些一律不猜。
2. **缺失返回空，不返回 0**。「这局没人插眼」和「拿不到眼位数据」在提示词里
   必须是两句话，写成 0 会被模型读成「辅助没做视野」。
3. **STRATZ 独有的标签不硬塞 OpenDota 字段**。``isGank`` / ``isSmoke`` /
   ``isDieBack`` / ``isBurst`` 在 OpenDota 的 ``teamfights`` 里根本没有对应
   位置，硬塞会被下游当成别的意思。它们只跟着 ``_clustered`` 标记走独立渲染。

口径对照（实测于比赛 9084367738）
---------------------------------
============================  ==============================================
STRATZ 原料                    聚合结果（OpenDota 方言）
============================  ==============================================
``towerDeaths`` +              ``objectives[]``（building_kill）
``playbackData.buildingEvents``
``roshanEvents[hp == 0]``      ``objectives[]``（ROSHAN_KILL）
``buyBackEvents``              ``objectives[]``（BUYBACK）
``pickBans``                   ``picks_bans[]``
``deathEvents`` × 10           ``teamfights[]``（按时间 + 坐标聚类）
``stats.wards``                ``obs_log`` / ``sen_log``
``stats.itemPurchases``        ``purchase_log[]``
``stats.killEvents``           ``kills_log[]``
``playbackData.goldEvents``    ``gold_t``（逐分钟累计金钱）
``playbackData.experienceEvents``  ``xp_t``
``playbackData.csEvents``      ``lh_t``（只含正补，与 ``numLastHits`` 严格相等）
============================  ==============================================

关于 ``gold_t`` 的口径：OpenDota 的 ``gold_t`` 是「该分钟结束时的累计金钱」，
STRATZ 的 ``goldEvents`` 是「每一笔收入/支出」，按分钟桶求和再做前缀和即可。
两者都**包含已花费**，所以可以横向比。实测 10 名玩家逐分钟累计值与
``goldPerMinute × 分钟数`` 同量级。

关于 ``teamfights`` 的口径：OpenDota 是服务端聚合好的团战段，带每人
``gold_delta`` / ``damage`` / ``ability_uses``；STRATZ 只有逐次死亡。
这里按「相邻死亡间隔 ≤ 12 秒、阵亡点相距 ≤ 30、整段跨度 ≤ 45 秒」聚成一段，
**段内至少 2 人阵亡**才算团战。因此：

* ``deaths`` / ``deaths_pos`` / ``killed`` 是**真有证据**的；
* ``gold_delta`` 记的是「本段内这些人掉了多少钱」（负值），与 OpenDota 的
  「团战期间净赚」方向相反 —— 所以渲染层看到 ``_clustered`` 就不写经济差；
* ``damage`` / ``ability_uses`` / ``item_uses`` 一律留空，绝不编。
"""

from __future__ import annotations

from typing import Any

#: 建筑类型 → (写进 ``key`` 的词, 中文名)
#:
#: ``key`` 必须带 ``goodguys`` / ``badguys``：``dota_format._describe_objective``
#: 靠这两个词判断「谁的建筑被拆」。天辉 = goodguys，夜魇 = badguys。
BUILDING_LABELS: dict[str, tuple[str, str]] = {
    "TOWER": ("tower", "塔"),
    "BARRACKS": ("rax", "兵营"),
    "FORT": ("fort", "基地"),
    "OUTPOST": ("outpost", "前哨"),
    "HEALER": ("shrine", "圣坛"),
}

#: 团战聚类参数（见模块注释里的口径）
FIGHT_GAP_SECONDS = 12
FIGHT_MAX_SPAN_SECONDS = 45
FIGHT_RADIUS = 30.0
#: 2 人阵亡时，两人间隔必须多近才算「互换」而不是「先后被抓」
FIGHT_SWAP_SECONDS = 5

#: STRATZ 眼位类型编号 → 目标日志。实测与 ``playbackData.wardEvents.wardType``
#: 的枚举名严格一一对应（OBSERVER=0 共 37 条、SENTRY=1 共 71 条，两边一致）。
WARD_OBSERVER = 0
WARD_SENTRY = 1


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _stats_of(player: dict) -> dict:
    node = player.get("stats")
    return node if isinstance(node, dict) else {}


def _playback_of(player: dict) -> dict:
    node = player.get("playbackData")
    return node if isinstance(node, dict) else {}


def _events(player: dict, group: str, key: str) -> list[dict]:
    rows = _stats_of(player).get(key) if group == "stats" else _playback_of(player).get(key)
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


# ----------------------------------------------------------------------
# 比赛级：objectives
# ----------------------------------------------------------------------
def build_objectives(match: dict) -> list[dict]:
    """把拆塔 / 肉山 / 买活 / 一血拼成 OpenDota ``objectives`` 形状。

    * 建筑：``towerDeaths`` 给「时间 + npcId + 属于哪方」，类型从
      ``playbackData.buildingEvents`` 的 ``npcId → type`` 查；查不到的 npcId
      （本场出现 36/37，共 8 条）**只写「建筑」不写具体名**。
    * 肉山：``roshanEvents`` 是**每分钟血量快照**，``hp == 0`` 的那一条才是
      击杀（实测 1634s / 2281s 两次，中间 ``maxHp`` 由 15880 重新长回 16920，
      正是重刷）。不取 ``createTime``，因为它恒为 ``None``。
    * 买活：``buyBackEvents`` 带时间，阵营从玩家自己身上拿。
    """
    rows: list[dict] = []

    building_types: dict[int, str] = {}
    playback = match.get("playbackData") or {}
    for event in playback.get("buildingEvents") or []:
        if not isinstance(event, dict):
            continue
        npc_id = _as_int(event.get("npcId"))
        kind = event.get("type")
        if npc_id is not None and kind and npc_id not in building_types:
            building_types[npc_id] = str(kind)

    for event in match.get("towerDeaths") or []:
        if not isinstance(event, dict):
            continue
        when = _as_int(event.get("time"))
        if when is None:
            continue
        npc_id = _as_int(event.get("npcId"))
        side_is_radiant = event.get("isRadiant")
        kind = building_types.get(npc_id or -1, "")
        token, label = BUILDING_LABELS.get(kind, ("", "建筑"))
        prefix = "goodguys" if side_is_radiant else "badguys"
        rows.append(
            {
                "type": "building_kill",
                "time": when,
                "key": f"npc_dota_{prefix}_{token}" if token else f"npc_dota_{prefix}_building",
                # ``_describe_objective`` 优先读这个字段，拿不到才回落到 key 猜
                "building": label,
                "npc_id": npc_id,
            }
        )

    for event in playback.get("roshanEvents") or []:
        if not isinstance(event, dict) or _as_int(event.get("hp")) != 0:
            continue
        when = _as_int(event.get("time"))
        if when is not None:
            rows.append({"type": "CHAT_MESSAGE_ROSHAN_KILL", "time": when})

    first_blood = _as_int(match.get("firstBloodTime"))
    if first_blood is not None and first_blood >= 0:
        rows.append({"type": "CHAT_MESSAGE_FIRSTBLOOD", "time": first_blood})

    for player in match.get("players") or []:
        if not isinstance(player, dict):
            continue
        team = 2 if player.get("is_radiant") else 3
        for event in _events(player, "playback", "buyBackEvents"):
            when = _as_int(event.get("time"))
            if when is not None:
                rows.append({"type": "CHAT_MESSAGE_BUYBACK", "time": when, "team": team})

    rows.sort(key=lambda row: (row.get("time") or 0, str(row.get("type"))))
    return rows


# ----------------------------------------------------------------------
# 比赛级：BP
# ----------------------------------------------------------------------
def build_picks_bans(match: dict) -> list[dict]:
    """``pickBans`` → OpenDota ``picks_bans``。

    STRATZ 的这一条记录**同时**带 ``heroId`` 与 ``bannedHeroId``：
    ``isPick=False`` 时 ``bannedHeroId`` 才是被 ban 的那个，``heroId`` 只是
    「当时这名玩家已经选到的英雄」（不是 ban 目标）。早期若直接读 ``heroId``
    会把 8 个 ban 全写成选人。
    """
    rows: list[dict] = []
    picks = match.get("pickBans")
    if not isinstance(picks, list):
        return rows
    for index, entry in enumerate(picks):
        if not isinstance(entry, dict):
            continue
        is_pick = bool(entry.get("isPick"))
        hero_id = _as_int(entry.get("heroId" if is_pick else "bannedHeroId"))
        if not hero_id:
            continue
        rows.append(
            {
                "is_pick": is_pick,
                "hero_id": hero_id,
                # OpenDota 的 team：0=天辉、1=夜魇
                "team": 0 if entry.get("isRadiant") else 1,
                "order": _as_int(entry.get("order")) if is_pick else index,
            }
        )
    return rows


# ----------------------------------------------------------------------
# 比赛级：团战（由死亡事件聚类）
# ----------------------------------------------------------------------
def _death_events(match: dict) -> tuple[list[dict], list[dict], dict[int, int], dict[int, str]]:
    """收集十人的死亡事件，返回 ``(有序玩家, 事件, heroId→槽位, heroId→npc名)``。"""
    players = [p for p in (match.get("players") or []) if isinstance(p, dict)]
    ordered = sorted(players, key=lambda p: int(p.get("player_slot") or 0))
    slot_of_hero: dict[int, int] = {}
    for index, player in enumerate(ordered):
        hero_id = _as_int(player.get("hero_id"))
        if hero_id is not None and hero_id not in slot_of_hero:
            slot_of_hero[hero_id] = index

    events: list[dict] = []
    for index, player in enumerate(ordered):
        hero_id = _as_int(player.get("hero_id"))
        # 阵营直接用玩家自己的标记，不用「列表索引 < 5」——
        # 索引只在十人齐全时才对得上，缺人（测试 fixture、数据残缺）就会判错。
        is_radiant = player.get("is_radiant", player.get("isRadiant"))
        for event in _events(player, "stats", "deathEvents"):
            when = _as_int(event.get("time"))
            if when is None:
                continue
            events.append(
                {
                    "time": when,
                    "slot": index,
                    "is_radiant": is_radiant,
                    "victim_hero": hero_id,
                    "killer_hero": _as_int(event.get("attacker")),
                    "x": _as_float(event.get("positionX")),
                    "y": _as_float(event.get("positionY")),
                    "gold_lost": _as_int(event.get("goldLost")) or 0,
                    "xp_fed": _as_int(event.get("xpFed")) or 0,
                    "time_dead": _as_int(event.get("timeDead")),
                    "is_die_back": bool(event.get("isDieBack")),
                    "is_burst": bool(event.get("isBurst")),
                    "is_engaged": bool(event.get("isEngagedOnDeath")),
                    "is_tp_attempt": bool(event.get("isAttemptTpOut")),
                    "is_ward_walk": bool(event.get("isWardWalkThrough")),
                }
            )
    events.sort(key=lambda item: item["time"])
    return ordered, events, slot_of_hero, {}


def _distance(left: dict, right: dict) -> float | None:
    if None in (left.get("x"), left.get("y"), right.get("x"), right.get("y")):
        return None
    return ((left["x"] - right["x"]) ** 2 + (left["y"] - right["y"]) ** 2) ** 0.5


def _cluster_deaths(events: list[dict]) -> list[list[dict]]:
    """按「时间间隔 + 坐标距离」把死亡事件切成团战段。

    距离判定对缺坐标的事件**放行**（不能因为一条没有坐标就把整段切断）；
    跨度上限 ``FIGHT_MAX_SPAN_SECONDS`` 防止双方在野区反复拉扯时把整局
    串成一整段。
    """
    clusters: list[list[dict]] = []
    current: list[dict] = []
    for event in events:
        if not current:
            current = [event]
            continue
        last = current[-1]
        gap = event["time"] - last["time"]
        span = event["time"] - current[0]["time"]
        distance = _distance(event, last)
        if (
            gap <= FIGHT_GAP_SECONDS
            and span <= FIGHT_MAX_SPAN_SECONDS
            and (distance is None or distance <= FIGHT_RADIUS)
        ):
            current.append(event)
        else:
            clusters.append(current)
            current = [event]
    if current:
        clusters.append(current)
    return clusters


def _is_teamfight(cluster: list[dict]) -> bool:
    """判定一段死亡事件算不算「团战」。

    只用「2 人阵亡」当门槛太松：一场比赛里到处都是「A 在野区被抓、6 秒后
    B 在中路被抓」，两次单杀会被拼成一段假团战，报告里就会凭空多出十几场
    「团战」。实测 9084367738 用 2 人门槛能切出 23 段，前 3 段全是这种互换
    单杀。因此：

    * 3 人以上阵亡 —— 算；
    * 正好 2 人 —— 必须是**双方各一人**且间隔 ≤ 5 秒（真正的互换），
      同队连死两人多半是「一个接一个被抓」，不算团战。
    """
    if len(cluster) >= 3:
        return True
    if len(cluster) != 2:
        return False
    first, second = cluster
    if second["time"] - first["time"] > FIGHT_SWAP_SECONDS:
        return False
    return bool(first.get("is_radiant")) != bool(second.get("is_radiant"))


def build_teamfights(match: dict, hero_npc: dict[int, str] | None = None) -> list[dict]:
    """把死亡事件聚成 ``teamfights``（OpenDota 形状 + ``_clustered`` 标记）。

    ``hero_npc`` 是 ``{heroId: npc_dota_hero_xxx}``，只用来填 ``killed``
    字典（渲染层按 npc 名翻英雄名）。拿不到就留空，不影响团战本身。
    """
    hero_npc = hero_npc or {}
    ordered, events, slot_of_hero, _ = _death_events(match)
    if not ordered or not events:
        return []

    fights: list[dict] = []
    for cluster in _cluster_deaths(events):
        if not _is_teamfight(cluster):
            continue
        rows: list[dict] = [{} for _ in ordered]
        details: list[dict] = []
        for event in cluster:
            slot = event["slot"]
            if slot >= len(rows):
                continue
            row = rows[slot]
            row["deaths"] = int(row.get("deaths") or 0) + 1
            row["gold_delta"] = int(row.get("gold_delta") or 0) - event["gold_lost"]
            row["xp_delta"] = int(row.get("xp_delta") or 0) - event["xp_fed"]
            x, y = event["x"], event["y"]
            if x is not None and y is not None:
                positions = row.setdefault("deaths_pos", {})
                key_x = str(int(x))
                inner = positions.setdefault(key_x, {})
                key_y = str(int(y))
                inner[key_y] = int(inner.get(key_y) or 0) + 1

            killer_slot = slot_of_hero.get(event["killer_hero"])
            if killer_slot is not None and killer_slot < len(rows):
                victim_npc = hero_npc.get(event["victim_hero"] or -1)
                if victim_npc:
                    killed = rows[killer_slot].setdefault("killed", {})
                    killed[victim_npc] = int(killed.get(victim_npc) or 0) + 1

            details.append(
                {
                    "time": event["time"],
                    "victim_hero": event["victim_hero"],
                    "killer_hero": event["killer_hero"],
                    "is_die_back": event["is_die_back"],
                    "is_burst": event["is_burst"],
                    "is_engaged": event["is_engaged"],
                    "is_tp_attempt": event["is_tp_attempt"],
                    "is_ward_walk": event["is_ward_walk"],
                    "time_dead": event["time_dead"],
                }
            )

        fights.append(
            {
                "start": cluster[0]["time"],
                "end": cluster[-1]["time"],
                "deaths": len(cluster),
                "players": rows,
                # 渲染层靠它切换到「事件聚类」措辞，不写经济差、不写技能
                "_clustered": True,
                "_events": details,
            }
        )
    return fights


# ----------------------------------------------------------------------
# 玩家级：逐分钟曲线
# ----------------------------------------------------------------------
def _bucket_series(
    events: list[dict],
    duration: int | None,
    *,
    value_key: str | None = None,
) -> list[int] | None:
    """把事件按分钟分桶后做前缀和，返回 ``[第0分钟, 第1分钟, …]``。

    ``value_key`` 为空表示「一条计数 1」（补刀）。
    """
    if not events:
        return None
    minutes = max(1, int((duration or 0) // 60) + 1)
    buckets = [0] * minutes
    for event in events:
        when = _as_int(event.get("time"))
        if when is None:
            continue
        amount = 1 if value_key is None else (_as_int(event.get(value_key)) or 0)
        index = min(minutes - 1, max(0, when // 60))
        buckets[index] += amount
    total = 0
    series: list[int] = []
    for value in buckets:
        total += value
        series.append(total)
    return series


def build_player_series(player: dict, duration: int | None) -> dict:
    """``goldEvents`` / ``experienceEvents`` / ``csEvents`` → ``gold_t`` 等。

    只产出**有原料**的那几条；缺的键根本不出现（下游按「有则输出」处理），
    不会出现一条全是 0 的假曲线。

    ``csEvents`` 的实测口径：总数与 ``numLastHits`` **严格相等**
    （322/322、278/278 … 十人全对），且 ``isCreep`` 只占其中一部分，
    剩下的 ``isNeutral`` 是打野 —— 所以 ``lh_t`` 是「正补总数（含打野）」，
    与 OpenDota 的 ``lh_t`` 口径不完全一样，提示词里会标明「含野怪」。
    """
    out: dict[str, Any] = {}
    gold = _bucket_series(_events(player, "playback", "goldEvents"), duration, value_key="amount")
    if gold:
        out["gold_t"] = gold
    xp = _bucket_series(
        _events(player, "playback", "experienceEvents"), duration, value_key="amount"
    )
    if xp:
        out["xp_t"] = xp
    last_hits = _bucket_series(_events(player, "playback", "csEvents"), duration)
    if last_hits:
        out["lh_t"] = last_hits
    return out


# ----------------------------------------------------------------------
# 玩家级：日志类
# ----------------------------------------------------------------------
def build_purchase_log(player: dict, item_name_by_id: dict[int, str]) -> list[dict]:
    """``itemPurchases`` → ``purchase_log``（``key`` 是物品内部名）。

    必须落成**名字**而不是数字 ID：``dota_format.summarize_item_timeline`` 要
    用 ``item_index.cost(key)`` 过滤掉小件，拿到数字会恒为 0，整段出装时间线
    就静默消失了。
    """
    rows: list[dict] = []
    for event in _events(player, "stats", "itemPurchases"):
        when = _as_int(event.get("time"))
        item_id = _as_int(event.get("itemId"))
        if when is None or item_id is None:
            continue
        name = item_name_by_id.get(item_id)
        if not name:
            continue
        rows.append({"time": when, "key": name})
    rows.sort(key=lambda row: row["time"])
    return rows


def build_kills_log(player: dict, hero_npc: dict[int, str]) -> list[dict]:
    """``killEvents`` → ``kills_log``（``key`` 是被杀英雄的 npc 名）。

    实测 ``killEvents[*].target`` 全部落在本场 10 个 heroId 内（113/113），
    所以它就是被杀者的 heroId，不是玩家槽位。
    """
    rows: list[dict] = []
    for event in _events(player, "stats", "killEvents"):
        when = _as_int(event.get("time"))
        target = _as_int(event.get("target"))
        if when is None or target is None:
            continue
        npc = hero_npc.get(target)
        if not npc:
            continue
        rows.append({"time": when, "key": npc})
    rows.sort(key=lambda row: row["time"])
    return rows


def build_ward_logs(player: dict) -> dict[str, list[dict]]:
    """``stats.wards`` → ``obs_log`` / ``sen_log``。

    返回值里只出现实际有数据的键。``type`` 语义用实测钉死：
    ``stats.wards[*].type`` 的计数（0→37、1→71）与
    ``playbackData.wardEvents`` 的枚举计数（OBSERVER 37、SENTRY 71）完全一致。
    """
    obs: list[dict] = []
    sen: list[dict] = []
    for event in _events(player, "stats", "wards"):
        when = _as_int(event.get("time"))
        if when is None:
            continue
        row = {
            "time": when,
            "x": _as_float(event.get("positionX")),
            "y": _as_float(event.get("positionY")),
        }
        kind = _as_int(event.get("type"))
        if kind == WARD_SENTRY:
            sen.append(row)
        elif kind == WARD_OBSERVER:
            obs.append(row)
    out: dict[str, list[dict]] = {}
    if obs:
        obs.sort(key=lambda row: row["time"])
        out["obs_log"] = obs
    if sen:
        sen.sort(key=lambda row: row["time"])
        out["sen_log"] = sen
    return out


def build_runes_log(player: dict) -> list[dict]:
    """``stats.runes`` → ``runes_log``（只保留时间，渲染层也只用时间）。"""
    rows: list[dict] = []
    for event in _events(player, "stats", "runes"):
        when = _as_int(event.get("time"))
        if when is None:
            continue
        rows.append({"time": when, "key": event.get("rune")})
    rows.sort(key=lambda row: row["time"])
    return rows


def build_buyback_log(player: dict) -> list[dict]:
    """``buyBackEvents`` → ``buyback_log``（只带时间）。

    下游有两处消费：基本信息行的「买活次数」（按条数统计）与焦点玩家的
    「买活时间点」。STRATZ 只给 ``time``，没有 OpenDota 的 ``cost`` 字段，
    所以不给 ``cost`` —— 缺失就不编。
    """
    rows: list[dict] = []
    for event in _events(player, "playback", "buyBackEvents"):
        when = _as_int(event.get("time"))
        if when is not None:
            rows.append({"time": when})
    rows.sort(key=lambda row: row["time"])
    return rows


def build_lane_report(match: dict) -> dict:
    """``laneReport`` → ``{side: {lane: {正补, 反补}}}``。

    这是 STRATZ 独有的「三路正反补」统计（OpenDota 只有玩家级的
    ``lane_role`` + ``lane_efficiency``）。目前不渲染，先按原样收好，
    等提示词决定怎么用。
    """
    node = match.get("laneReport")
    if not isinstance(node, dict):
        return {}
    out: dict[str, dict] = {}
    for side in ("radiant", "dire"):
        lanes = node.get(side)
        if not isinstance(lanes, dict):
            continue
        parsed: dict[str, dict] = {}
        for lane in ("safeLane", "midLane", "offLane"):
            row = lanes.get(lane)
            if not isinstance(row, dict):
                continue
            parsed[lane] = {
                "melee": _as_int(row.get("meleeCount")) or 0,
                "ranged": _as_int(row.get("rangeCount")) or 0,
                "deny": _as_int(row.get("denyCount")) or 0,
            }
        if parsed:
            out[side] = parsed
    return out
