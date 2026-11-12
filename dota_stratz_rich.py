"""STRATZ **独有**数据的原生利用层（不转译成 OpenDota 方言）。

与 :mod:`dota_stratz_events` 的分工
-----------------------------------
``dota_stratz_events`` 的职责是「把 STRATZ 的原料聚合成 OpenDota 已有的形状」，
为的是让渲染层一行都不用改。但 STRATZ 还有一批 OpenDota **根本没有**的数据：

============================  ============================================
STRATZ 字段                     内容
============================  ============================================
``playbackData.abilityLearnEvents``  加点顺序 + 英雄等级 + 是否天赋/大招/满级
``stats.heroDamageReport``      物理/魔法/纯净伤害拆分、**控制次数与总时长**
``stats.abilityCastReport``     每个技能放了几次、打了谁、多少伤害
``stats.itemUsed``              主动道具用了多少次
``stats.actionReport``          扫描/塔防/标记次数
``stats.farmDistributionReport`` 经济来自线野/野区/远古/建筑
``stats.wardDestruction``       反眼（时间、收益）
``imp``                         本场 IMP 表现分
``award``                       本场称号（TOP_CORE / MVP …）
``invisibleSeconds``            隐身总时长
============================  ============================================

这批数据**不能塞进 OpenDota 字段**：OpenDota 的 ``ability_upgrades_arr`` 只有
一串技能 ID、``stuns`` 只有「被控秒数」一个数、``damage_inflictor`` 只有
「技能 → 伤害」。硬塞会把「STRATZ 给了 6 个维度」压成「OpenDota 的 1 个维度」，
正好丢掉本次要用的东西。所以这里**原样保留结构**，由渲染层按自己的口径组合。

单位口径（实测钉死）
--------------------
``stunDuration`` / ``disableDuration`` / ``slowDuration`` 是 **1/100 秒**
（centisecond），不是毫秒也不是秒。实测比赛 9084367738：

* 英雄 49 眩晕 69 次、``stunDuration=18998`` → 除 100 得 190 秒，
  平均每次 2.75 秒 —— 与 Dota 常见眩晕时长（1.5~3 秒）吻合；
* 若按毫秒解释，平均每次 0.275 秒 —— 全场 69 次全是 0.3 秒级眩晕，
  在 Dota 里不存在这样的英雄。

``build_control_report`` 里再兜一层：算出的「平均每次」落在 0.2~10 秒之外时，
把原始值当毫秒重算一次，仍不合理就整段丢弃（宁可缺，不可错）。

缺失一律返回 ``None`` / 空容器，**绝不返回 0**
---------------------------------------------
「这局没插眼」和「拿不到眼位数据」在提示词里必须是两句话。这条在
``dota_stratz_events`` 里已经写死，这里沿用。
"""

from __future__ import annotations

from typing import Any

#: 控制时长原始值的换算基准。见模块注释里的实测依据。
CONTROL_TIME_DIVISOR = 100.0

#: 「平均每次控制」的合理区间（秒）。超出这个区间说明换算基准判断错了。
CONTROL_PER_EVENT_MIN = 0.2
CONTROL_PER_EVENT_MAX = 10.0

#: 天赋条目的等级上限。25 级是天赋树的最后一层，之后出现的
#: ``isTalent=True`` 条目不是天赋树的选择，见 :func:`build_ability_build`。
TALENT_MAX_LEVEL = 25

#: 经济来源里 ``buildings`` 的 id → 中文名。实测 id 0~8 依次对应一塔到基地，
#: 但塔的具体归属（天辉/夜魇）在 ``farmDistributionReport`` 里没有信息，因此
#: 只按「第 N 座建筑」表述，不猜阵营。
BUILDING_NAMES: dict[int, str] = {
    0: "防御塔",
    1: "防御塔",
    2: "防御塔",
    3: "防御塔",
    4: "防御塔",
    5: "兵营",
    6: "兵营",
    7: "兵营",
    8: "基地",
}

#: ``creepType`` 里 id 与兵种类的对照。STRATZ 用的是 npc 常量 id，
#: 这里只保留实测能对上、且对复盘有用的几个；对不上的一律不猜。
CREEP_TYPE_NAMES: dict[int, str] = {
    10: "近战兵",
    4: "远程兵",
}


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num


def _stats_of(player: dict) -> dict:
    node = player.get("stats")
    return node if isinstance(node, dict) else {}


def _playback_of(player: dict) -> dict:
    node = player.get("playbackData")
    return node if isinstance(node, dict) else {}


# ----------------------------------------------------------------------
# 加点顺序（含天赋）
# ----------------------------------------------------------------------
def build_ability_build(
    player: dict,
    ability_name_by_id: dict[int, str] | None = None,
    ability_display_by_id: dict[int, str] | None = None,
) -> dict[str, Any]:
    """``playbackData.abilityLearnEvents`` → 加点顺序。

    返回两个键：

    * ``ability_upgrades_arr``：按**学习时的英雄等级**排好序的技能 ID 列表，
      直接复用渲染层既有的 :func:`dota_format.summarize_ability_build`；
    * ``ability_build``：带上下文的明细（英雄等级、该技能升到几级、是否天赋/
      大招/满级），供「天赋选择」这类 STRATZ 独有的小节使用。

    为什么可以复用一个字段名：OpenDota 的 ``ability_upgrades_arr`` 本来就是
    「按学习顺序排列的技能 ID」，STRATZ 给的只是**同一件事的更详细版本**
    （多了英雄等级与天赋标记）。塞进去不会造成语义漂移，反而让既有渲染
    自动拿到真实数据。

    排序依据是 ``levelObtained``（英雄等级 1~25）而不是 ``time``：天赋在
    10/15/20/25 级的同一时刻可能出现两条，``time`` 完全相同，按时间排会把
    顺序打散。实测第一条 ``time=-89``（开局前学的技能）也是靠等级 1 才排对。
    """
    rows: list[dict] = []
    displays = ability_display_by_id or {}
    for event in _playback_of(player).get("abilityLearnEvents") or []:
        if not isinstance(event, dict):
            continue
        ability_id = _as_int(event.get("abilityId"))
        hero_level = _as_int(event.get("levelObtained"))
        if ability_id is None or hero_level is None:
            continue
        rows.append(
            {
                "ability_id": ability_id,
                "hero_level": hero_level,
                "ability_level": _as_int(event.get("level")),
                "time": _as_int(event.get("time")),
                "is_ultimate": bool(event.get("isUltimate")),
                "is_talent": bool(event.get("isTalent")),
                "is_max": bool(event.get("isMaxLevel")),
                "name": (ability_name_by_id or {}).get(ability_id),
                # 天赋的内部名不可读（``special_bonus_unique_furion_6``），
                # 必须带显示名，否则渲染出来是「special bonus unique furion 6」。
                "display": displays.get(ability_id),
            }
        )
    if not rows:
        return {}
    rows.sort(key=lambda row: (row["hero_level"], row["time"] or 0))

    result: dict[str, Any] = {
        "ability_upgrades_arr": [row["ability_id"] for row in rows],
        "ability_build": rows,
    }
    # 天赋单独拎出来：OpenDota 的加点数组里天赋和别人长得一样，
    # 光看一串 ID 分不出「这是哪个天赋」。这里给成「11级 天赋名」。
    #
    # 筛选条件是 ``hero_level <= 25`` 而不是「等于 10/15/20/25」：
    # ``levelObtained`` 是**选择时**的英雄等级，玩家可以攒着天赋点晚点选
    # （实测龙骑的 10 级天赋记在 11 级、NP 的第二层记在 16 级）。按固定的
    # 四个等级筛会漏掉这些「迟选」的天赋。
    # 上限取 25 是因为 25 级之后还存在 ``isTalent=True`` 的额外条目
    # （实测 26~29 级各一条），那不是天赋树的四层，混进来会让「天赋选择」
    # 看起来像有七八层。宁可少列，不可列错。
    talents = [
        {
            "hero_level": row["hero_level"],
            "name": row["name"],
            "display": row["display"],
            "ability_id": row["ability_id"],
        }
        for row in rows
        if row["is_talent"] and row["hero_level"] <= TALENT_MAX_LEVEL
    ]
    if talents:
        result["talent_picks"] = talents
    return result


# ----------------------------------------------------------------------
# 伤害与控制
# ----------------------------------------------------------------------
def _control_seconds(raw: Any, count: int | None) -> float | None:
    """控制时长原始值 → 秒。见模块注释「单位口径」。"""
    total = _as_num(raw)
    if total is None:
        return None
    seconds = total / CONTROL_TIME_DIVISOR
    if count and count > 0:
        per = seconds / count
        if per < CONTROL_PER_EVENT_MIN or per > CONTROL_PER_EVENT_MAX:
            # 换算基准可能不是 1/100 秒：按毫秒再试一次
            alt = total / 1000.0
            alt_per = alt / count
            if CONTROL_PER_EVENT_MIN <= alt_per <= CONTROL_PER_EVENT_MAX:
                return alt
            return None
    return seconds


def build_damage_report(
    player: dict,
    ability_name_by_id: dict[int, str] | None = None,
    item_name_by_id: dict[int, str] | None = None,
    hero_npc: dict[int, str] | None = None,
) -> dict[str, Any]:
    """``stats.heroDamageReport`` → 伤害构成（含 OpenDota 没有的控制数据）。

    产出三类键，全部对齐渲染层已经在读的形状：

    * ``damage_by_type`` / ``damage_taken_by_type``：``{物理/魔法/纯净: 数值}``。
      **不能叫 ``damage``** —— 那个键在渲染层已经是「按目标单位的伤害分解」
      （``{npc_dota_hero_xxx: 数值}``），占用它会把「物理/魔法」渲染成英雄名；
    * ``damage_inflictor``：``{技能名: 伤害}``，技能与道具**合并**成一份 ——
      OpenDota 的 ``damage_inflictor`` 本来就是「伤害来源」不分技能道具；
    * ``damage_targets``：``{来源: {英雄 npc 名: 伤害}}``，喂
      :func:`dota_format.summarize_damage_targets`。

    关于「普攻」：STRATZ 的 ``dealtSourceAbility`` / ``dealtSourceItem``
    **只统计技能与道具造成的伤害**，普通攻击的物理伤害不在其中。不补这一块，
    ``_breakdown_text`` 的占比分母就只有技能伤害，每项占比都会虚高。所以这里
    把「总伤害 − 已列来源」的差额补成一个 ``None`` 键（渲染层把 ``None``
    标成「普攻/未记录」）。实测 hero 53 的差额 29385 正好等于它的物理伤害。
    """
    report = _stats_of(player).get("heroDamageReport")
    if not isinstance(report, dict):
        return {}
    ability_names = ability_name_by_id or {}
    item_names = item_name_by_id or {}
    npc_by_hero = hero_npc or {}
    result: dict[str, Any] = {}
    total_dealt = 0.0

    dealt_total = report.get("dealtTotal")
    if isinstance(dealt_total, dict):
        # ⚠️ 这里**保留 0**：``pureDamage=0`` 是「这局确实没造成纯净伤害」这个
        # 观测结果，不是「拿不到数据」。过滤掉会让模型以为纯净伤害那一栏缺失，
        # 进而写「没有纯净伤害数据」——而实际上它是有数据且为零。
        dealt = {
            "物理": _as_num(dealt_total.get("physicalDamage")),
            "魔法": _as_num(dealt_total.get("magicalDamage")),
            "纯净": _as_num(dealt_total.get("pureDamage")),
        }
        dealt = {k: v for k, v in dealt.items() if v is not None}
        if dealt:
            result["damage_by_type"] = dealt
            total_dealt = sum(float(v) for v in dealt.values() if v is not None)
        control = _build_control(dealt_total, prefix="dealt")
        if control:
            result["control_dealt"] = control
        heal = {
            "自我治疗": _as_num(dealt_total.get("selfHeal")),
            "队友治疗": _as_num(dealt_total.get("allyHeal")),
        }
        heal = {k: v for k, v in heal.items() if v}
        if heal:
            result["damage_heal"] = heal

    received_total = report.get("receivedTotal")
    if isinstance(received_total, dict):
        taken = {
            "物理": _as_num(received_total.get("physicalDamage")),
            "魔法": _as_num(received_total.get("magicalDamage")),
            "纯净": _as_num(received_total.get("pureDamage")),
        }
        taken = {k: v for k, v in taken.items() if v is not None}
        if taken:
            result["damage_taken_by_type"] = taken
        control = _build_control(received_total, prefix="received")
        if control:
            result["control_taken"] = control

    inflictor: dict[Any, float] = {}
    for row in report.get("dealtSourceAbility") or []:
        if not isinstance(row, dict):
            continue
        amount = _as_num(row.get("amount"))
        name = ability_names.get(_as_int(row.get("abilityId")) or -1)
        if not amount or not name:
            continue
        inflictor[name] = inflictor.get(name, 0.0) + amount
    for row in report.get("dealtSourceItem") or []:
        if not isinstance(row, dict):
            continue
        amount = _as_num(row.get("amount"))
        name = item_names.get(_as_int(row.get("itemId")) or -1)
        if not amount or not name:
            continue
        inflictor[name] = inflictor.get(name, 0.0) + amount
    if inflictor:
        # 差额 = 普攻 + 没被 dealtSourceAbility 记进去的那部分。
        # 见函数注释里关于占比分母的说明。
        residual = total_dealt - sum(inflictor.values()) if total_dealt else 0.0
        if residual > 0:
            # 键必须是 None：``ability_label(None)`` 会渲染成「普攻/未记录」，
            # 与 OpenDota 链路里 ``damage_inflictor`` 的 null 键语义一致。
            inflictor[None] = residual  # type: ignore[index]
        result["damage_inflictor"] = inflictor

    targets: dict[str, dict[str, float]] = {}
    total_by_victim: dict[str, float] = {}
    for row in report.get("dealtTargets") or []:
        if not isinstance(row, dict):
            continue
        amount = _as_num(row.get("amount"))
        npc = npc_by_hero.get(_as_int(row.get("target")) or -1)
        if not amount or not npc:
            continue
        total_by_victim[npc] = total_by_victim.get(npc, 0.0) + amount
    if total_by_victim:
        # 键必须是 ``npc_dota_hero_xxx``：summarize_damage_targets 靠这个前缀
        # 过滤掉建筑与小兵。来源名这里有意义 —— 但 STRATZ 没给「对某人用哪个
        # 技能打了多少」，拆不出来就别编，统一挂在“全部来源”下。
        targets["全部来源"] = total_by_victim
        result["damage_targets"] = targets

    return result


def _build_control(node: dict, *, prefix: str) -> dict[str, Any]:
    """``dealtTotal`` / ``receivedTotal`` 里的控制字段 → 统一结构。"""
    stun_count = _as_int(node.get("stunCount"))
    disable_count = _as_int(node.get("disableCount"))
    slow_count = _as_int(node.get("slowCount"))
    out: dict[str, Any] = {}
    stun_seconds = _control_seconds(node.get("stunDuration"), stun_count)
    if stun_count is not None or stun_seconds:
        out["stun"] = {"count": stun_count, "seconds": stun_seconds}
    disable_seconds = _control_seconds(node.get("disableDuration"), disable_count)
    if disable_count is not None or disable_seconds:
        out["disable"] = {"count": disable_count, "seconds": disable_seconds}
    slow_seconds = _control_seconds(node.get("slowDuration"), slow_count)
    if slow_count is not None or slow_seconds:
        out["slow"] = {"count": slow_count, "seconds": slow_seconds}
    out["_side"] = "dealt" if prefix == "dealt" else "received"
    return out if len(out) > 1 else {}


# ----------------------------------------------------------------------
# 技能与道具的使用
# ----------------------------------------------------------------------
def build_usage_report(
    player: dict,
    ability_name_by_id: dict[int, str] | None = None,
    item_name_by_id: dict[int, str] | None = None,
    hero_npc: dict[int, str] | None = None,
) -> dict[str, Any]:
    """``abilityCastReport`` / ``itemUsed`` → 技能与道具的实际使用情况。

    ``abilityCastReport`` 比 OpenDota 的 ``ability_uses`` 多两样东西：

    * ``count`` 是**施放次数**（OpenDota 的 ``ability_uses`` 也是次数，但只给
      总数，不分对象）；
    * ``targets`` 是**对谁放、打了多少、控了多久**。有它才能看出「这个技能
      是拿去打团还是单抓」，「命中率」也才有分母。

    ``hit_rate`` 只在技能确实是「指向性」时才有意义，这里**不猜**：只把
    「有目标成交的施放次数 / 总施放次数」两个数摆出来，让模型自己判断。
    """
    ability_names = ability_name_by_id or {}
    item_names = item_name_by_id or {}
    npc_by_hero = hero_npc or {}

    casts: dict[str, int] = {}
    detail: list[dict] = []
    for row in _stats_of(player).get("abilityCastReport") or []:
        if not isinstance(row, dict):
            continue
        ability_id = _as_int(row.get("abilityId"))
        count = _as_int(row.get("count"))
        if ability_id is None or not count:
            continue
        name = ability_names.get(ability_id) or f"技能#{ability_id}"
        casts[name] = casts.get(name, 0) + count
        targets: list[dict[str, Any]] = []
        target_casts = 0
        for target in row.get("targets") or []:
            if not isinstance(target, dict):
                continue
            npc = npc_by_hero.get(_as_int(target.get("target")) or -1)
            target_count = _as_int(target.get("count")) or 0
            if not npc:
                continue
            target_casts += target_count
            targets.append(
                {
                    "npc": npc,
                    "count": target_count,
                    "damage": _as_num(target.get("damage")),
                    "duration": _as_num(target.get("duration")),
                }
            )
        targets.sort(key=lambda item: int(item["count"]), reverse=True)
        detail.append(
            {
                "name": name,
                "count": count,
                "target_casts": target_casts,
                "targets": targets,
            }
        )
    detail.sort(key=lambda item: item["count"], reverse=True)

    item_uses: dict[str, int] = {}
    # ``itemUsed`` 是**已聚合**的形状 ``{itemId, count}``（没有 time 字段），
    # 直接按 count 累加即可 —— 这一点与 ``itemPurchases`` 的逐条事件不同。
    for event in _stats_of(player).get("itemUsed") or []:
        if not isinstance(event, dict):
            continue
        item_name = item_names.get(_as_int(event.get("itemId")) or -1)
        count = _as_int(event.get("count"))
        if item_name and count:
            item_uses[item_name] = item_uses.get(item_name, 0) + count

    result: dict[str, Any] = {}
    if casts:
        result["ability_uses"] = casts
    if detail:
        result["ability_casts"] = detail
        # ``ability_targets`` 是 OpenDota 解析产物里的形状
        # ``{技能名: {受害者 npc 名: 次数}}``，渲染层的「技能施放对象」直接读它。
        # STRATZ 的 ``abilityCastReport.targets`` 正好是一一对应的信息，
        # 所以这里顺手把它补上，不另开渲染分支。
        target_map: dict[str, dict[str, int]] = {}
        for row in detail:
            victims = {
                item["npc"]: item["count"] for item in row["targets"] if item["count"]
            }
            if victims:
                target_map[row["name"]] = victims
        if target_map:
            result["ability_targets"] = target_map
    if item_uses:
        result["item_uses"] = item_uses
    return result


# ----------------------------------------------------------------------
# 行动与环境
# ----------------------------------------------------------------------
def build_action_report(player: dict) -> dict[str, Any]:
    """``stats.actionReport`` → 扫描 / 塔防 / 标记次数。

    这三个数 OpenDota 一个都拿不到，但它们是「谁在指挥」的直接证据：
    扫描 0 次的辅助和扫描 5 次的中单，复盘时该说的话完全不同。
    """
    node = _stats_of(player).get("actionReport")
    if not isinstance(node, dict):
        return {}
    out = {
        "scan": _as_int(node.get("scanUsed")),
        "glyph": _as_int(node.get("glyphCast")),
        "ping": _as_int(node.get("pingUsed")),
    }
    out = {k: v for k, v in out.items() if v}
    return {"actions": out} if out else {}


def build_farm_report(player: dict) -> dict[str, Any]:
    """``stats.farmDistributionReport`` → 野区与建筑收益。

    只报**口径明确**的三项：``neutralLocation``（打野）、
    ``ancientLocation``（远古）、``buildings``（建筑）。

    「线上兵」故意不报 —— ``creepType`` 按**兵种**切（近战/远程/攻城/各种野怪），
    ``creepLocation`` 按**地图区域**切（id 1~11 没有可信对照表），两者总量还
    对不上（实测 23926 vs 13134）。把 ``creepType`` 当「线上兵」是错的：
    它同样是 Furion 这种刷野英雄的主要收入，写出来会变成「他 69% 收入来自线上」，
    与事实相反。宁可不给这一项，也不给一个方向错的数。
    """
    node = _stats_of(player).get("farmDistributionReport")
    if not isinstance(node, dict):
        return {}

    def _sum(key: str) -> float | None:
        rows = node.get(key)
        if not isinstance(rows, list):
            return None
        total = 0.0
        found = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            gold = _as_num(row.get("gold"))
            if gold is None:
                continue
            total += gold
            found = True
        return total if found else None

    groups = {
        "打野": _sum("neutralLocation"),
        "远古": _sum("ancientLocation"),
        "建筑": _sum("buildings"),
    }
    groups = {k: v for k, v in groups.items() if v}
    result: dict[str, Any] = {}
    if groups:
        result["farm_sources"] = groups
    buyback = _as_num(node.get("buyBackGold"))
    if buyback:
        result["buyback_gold"] = buyback
    bounty = node.get("bountyGold")
    if isinstance(bounty, dict):
        # 实测 ``bountyGold.gold`` 恒为 0（11 个赏金符却 0 金币），只有 ``count``
        # 可信。金币为 0 时不落这个键，免得渲染出「赏金符 11 个 / 0 金币」。
        bounty_count = _as_int(bounty.get("count"))
        bounty_gold = _as_num(bounty.get("gold"))
        if bounty_gold:
            result["bounty"] = {"gold": bounty_gold, "count": bounty_count}
        elif bounty_count:
            result["bounty"] = {"count": bounty_count}
    return result


def build_ward_destruction(player: dict) -> dict[str, Any]:
    """``stats.wardDestruction`` → 反眼记录与收益。

    OpenDota 的 ``obs_left_log`` 只告诉你「眼什么时候没了」，而
    ``attackername`` 分不清「被反」还是「自己补位换眼」。STRATZ 的
    ``wardDestruction`` 是**反眼方**的视角，天然只含敌方眼位。
    """
    rows: list[dict] = []
    for event in _stats_of(player).get("wardDestruction") or []:
        if not isinstance(event, dict):
            continue
        when = _as_int(event.get("time"))
        if when is None:
            continue
        rows.append(
            {
                "time": when,
                "gold": _as_num(event.get("gold")),
                "xp": _as_num(event.get("experience")),
                "is_ward": bool(event.get("isWard")),
            }
        )
    if not rows:
        return {}
    rows.sort(key=lambda row: row["time"])
    gold = sum(row["gold"] or 0 for row in rows)
    return {"ward_kills": rows, "ward_kill_gold": gold}


def build_profile(player: dict, hero_npc: dict[int, str] | None = None) -> dict[str, Any]:
    """玩家级独有指标：IMP / 称号 / 回泉次数 / 拉野次数。

    ⚠️ **不收 ``invisibleSeconds``**：实测它不可信。比赛 9084367738 时长
    2642 秒，出隐刀的自然先知该字段为 ``3700`` —— 比整场比赛还长，
    既不是秒也不是毫秒（毫秒则只有 3.7 秒，隐刀一次都不到）。在口径确定之前
    不展示，免得提示词里出现「隐身 3700 秒」这种明显不可能的数。
    """
    out: dict[str, Any] = {}
    imp = _as_int(player.get("imp"))
    if imp is not None:
        out["imp"] = imp
    award = player.get("award")
    if isinstance(award, str) and award and award.upper() not in ("NONE", "NULL"):
        out["award"] = award
    fountain = _stats_of(player).get("tripsFountainPerMinute")
    if isinstance(fountain, list) and fountain:
        total = sum(_as_int(x) or 0 for x in fountain)
        if total:
            out["fountain_trips"] = total
    stacks = _stats_of(player).get("campStack")
    if isinstance(stacks, list) and stacks:
        total = sum(_as_int(x) or 0 for x in stacks)
        if total:
            out["camp_stacks"] = total
    return out


#: 单场称号 → 中文。STRATZ 的 ``MatchPlayerAward`` 枚举，未列出的一律照原样显示。
AWARD_LABELS: dict[str, str] = {
    "MVP": "MVP",
    "TOP_CORE": "最佳核心",
    "TOP_SUPPORT": "最佳辅助",
    "TOP_MID": "最佳中单",
    "TOP_OFFLANE": "最佳劣势路",
    "TOP_CARRY": "最佳大哥",
}


def award_label(raw: str | None) -> str:
    if not raw:
        return ""
    key = str(raw).upper()
    return AWARD_LABELS.get(key, str(raw).replace("_", " ").title())
