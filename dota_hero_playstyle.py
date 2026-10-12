# -*- coding: utf-8 -*-
"""英雄当前版本打法：高分段（STRATZ）+ 职业局（OpenDota）数据的聚合与渲染。

为什么单独一个模块
------------------
「这个英雄现在怎么玩」要跨**两个数据源**拼图，而且两边的能力边界不一样：

* **高分段**的位置分布、对线结果、出门装 / 鞋子 —— 只有 STRATZ 的
  ``heroStats`` 提供。OpenDota 的 ``/heroStats`` **没有位置维度**，它那套
  ``1_pick``~``7_pick`` 分档字段本项目已判定**不可用**（见
  ``dota_format.hero_meta_rows`` 的说明：分档与真实水平对不上、第 8 档恒为 0）。
* **职业局**与**全分段出装路线** —— 只有 OpenDota 提供
  （``/heroStats`` 的 ``pro_pick`` / ``pro_win`` / ``pro_ban``、
  ``/heroes/{id}/itemPopularity``）。

两边各自都可能拿不到，此时**如实说明缺哪一块**，绝不拿全分段数据冒充高分段
（这是本项目一以贯之的纪律，见 ``dota_stratz.SUPPORTS_PATCH_FILTER`` 的处理）。

**技能加点（skill build）不产出**
---------------------------------
公开数据源里**没有**「英雄技能加点顺序」的聚合统计：OpenDota 无对应端点；
STRATZ ``heroStats`` 的 21 个字段里，``talent`` 只给「天赋节点被选中的场次」
（节点内容还要另找一份中文表）、``abilityMinLevel`` / ``abilityMaxLevel``
经实测语义未明（返回的 ``level`` 与技能等级、英雄等级都对不上）。
**语义未明的数据宁可不给**，所以本模块不给加点顺序，交由上层在工具说明里
如实交代。想要加点请走单场复盘的 ``ability_upgrades_arr``。

**对线只给比例，不给场次**
---------------------------
``laneOutcome`` 是**按对位拆行**的（一行一个对手英雄 + 一个 ``week``）：
实测敌法师一号位回 125 行。把这些行求和得到的数**不是场次** —— 它是
同口径场次的 1.5~3 倍，而且各位置倍数还不一样（三个英雄实测：一号位 3.15x /
三号位 3.10x / 中单 1.52x），说明计数单位是「对位」而非「场」。写成
「对线 N 场」会读出「对线次数比这英雄的出战场次还多」这种荒唐结论。
所以本模块**只报比例**（单位无关，倍数约掉）。

另外这个接口返回的 ``position`` 字段**不可信**：斧王 94% 局都在三号位，
回的行却全部标成 ``POSITION_1``。所以位置**必须在请求参数里用
``positionIds`` 限定**（那个参数实测是准的），不能在拿到数据后再按字段筛。

**高分段出装只排先后，不折算百分比**
--------------------------------------
``itemFullPurchase`` 每行是 **(装备, 比赛分钟 ``time``)**：``matchCount`` 是
「在该分钟买到这件装备」的数量，同一件装备按购买分钟拆成多行（实测斧王刃甲
15 行，``time`` 9~23）。各行的 ``matchCount`` 相加**大于** ``stats`` 的同位置
场次（15528 vs 11871）—— 两个字段**不是同一个样本池**。所以本模块只用它
**排先后、算典型成型时间（按 ``matchCount`` 加权的 ``time`` 均值）与该装备的
对局胜率**，**绝不折算百分比、也不报绝对场次**（报出去会被拿去和场次相除）。

**购买计数写「次」，不写「场」**
---------------------------------
``itemStartingPurchase`` / ``itemPopularity`` 这类字段统计的是**购买次数**：
消耗品一局能买多个（实测「铁树枝干」在同一位置回两行、合计 86070，而该位置
只有 11871 场）。写成「N 场」会被读成「有 N 局这么出」，是错的。
"""

from __future__ import annotations

from typing import Any

try:  # 插件目录被作为包加载时的相对导入
    from .dota_format import ItemIndex
except ImportError:  # 兜底：以普通模块方式加载时，把插件目录加入 sys.path
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dota_format import ItemIndex  # type: ignore[no-redef]


#: 高分段口径：STRATZ 的 ``RankBracketBasicEnum``。
#: ``DIVINE_IMMORTAL`` = 超凡入圣 + 冠绝一世，是「高分局」的通行界线。
BRACKET_HIGH = "DIVINE_IMMORTAL"
BRACKET_HIGH_LABEL = "超凡入圣及以上"

#: STRATZ 位置枚举 → 中文。
POSITION_LABELS: dict[str, str] = {
    "POSITION_1": "一号位（优势路核心）",
    "POSITION_2": "二号位（中单）",
    "POSITION_3": "三号位（劣势路）",
    "POSITION_4": "四号位（游走辅助）",
    "POSITION_5": "五号位（保人辅助）",
}

POSITION_ORDER: tuple[str, ...] = (
    "POSITION_1",
    "POSITION_2",
    "POSITION_3",
    "POSITION_4",
    "POSITION_5",
)

#: 位置分布里低于这个场次的一律并入「其它」。
#: 没有门槛的话，「4 号位 24 场 62% 胜率」会被当成一条结论 —— 它是噪声。
MIN_POSITION_SAMPLE = 30

#: 各阶段出装最多列几个。
MAX_ITEMS_PER_STAGE = 5

#: 高分段出装里购买量低于这个数的装备不进榜单。
#: 实测「阿哈利姆神杖 260 场、胜率 63.8%」这种小样本进榜会误导读者。
MIN_ITEM_PURCHASE = 500

#: 高分段出装最多列几件。
MAX_HIGH_ITEMS = 6


def position_label(position: Any) -> str:
    """STRATZ 位置枚举 → 中文；认不出返回空串（不猜）。"""
    return POSITION_LABELS.get(str(position or "").strip().upper(), "")


def resolve_hero(heroes: dict | None, query: Any) -> tuple[int, str] | None:
    """英雄名 / 数字 ID → ``(hero_id, 显示名)``；认不出返回 ``None``（不猜）。

    匹配顺序：数字 ID → 英雄名精确匹配（中文名 / ``npc_dota_hero_xxx`` 全名或短名）
    → 唯一的中文名子串匹配。**歧义时返回 ``None``**，让上层如实说「认不出这个英雄」，
    而不是随机挑一个。
    """
    text = str(query or "").strip()
    if not text:
        return None
    if text.isdigit():
        hid = int(text)
        info = (heroes or {}).get(hid) or (heroes or {}).get(str(hid))
        if isinstance(info, dict) and info.get("localized_name"):
            return hid, str(info["localized_name"])
        return hid, f"英雄#{hid}"

    low = text.lower()
    exact: list[tuple[int, str]] = []
    fuzzy: list[tuple[int, str]] = []
    for raw_id, info in (heroes or {}).items():
        if not isinstance(info, dict):
            continue
        try:
            hid = int(raw_id)
        except (TypeError, ValueError):
            continue
        loc = str(info.get("localized_name") or "")
        npc = str(info.get("name") or "")
        short = npc[len("npc_dota_hero_"):] if npc.startswith("npc_dota_hero_") else npc
        if low in {loc.lower(), npc.lower(), short.lower()} and loc:
            exact.append((hid, loc))
        elif loc and low in loc.lower():
            fuzzy.append((hid, loc))
    if exact:
        return exact[0]
    if len(fuzzy) == 1:
        return fuzzy[0]
    return None


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _num(value: Any, digits: int = 0) -> str:
    """数值渲染：拿不到就 ``-``（**不用 0 冒充**，见项目口径）。"""
    if value is None or isinstance(value, bool):
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{number:.{digits}f}" if digits else f"{int(round(number))}"


def _rate(win: Any, total: Any) -> str:
    w, t = _int(win), _int(total)
    if t <= 0:
        return "-"
    return f"{w / t * 100:.1f}%"


def _position_rows(stats: list[dict] | None) -> tuple[list[dict], int]:
    """把 STRATZ 的 ``stats`` 行按场次倒序整理，返回 ``(行, 总场次)``。"""
    rows = [r for r in (stats or []) if isinstance(r, dict)]
    rows.sort(key=lambda r: -_int(r.get("matchCount")))
    total = sum(_int(r.get("matchCount")) for r in rows)
    return rows, total


def main_position(stats: list[dict] | None) -> str:
    """英雄的**主位置**枚举（``POSITION_1``~``POSITION_5``）；判不出返回空串。

    判据：按场次倒序后第一个**达到 :data:`MIN_POSITION_SAMPLE` 门槛**的位置。
    都不到门槛（样本整体很小）时退回场次最多的那个 —— 那是「已知里最像主位置
    的一个」，仍要如实标注样本不足。

    这个值上游要拿去**再查一次对线**（``laneOutcome`` 的 ``positionIds``
    必须靠它限定），所以单独抽出来，避免渲染与取数两处各判一遍。
    """
    rows, total = _position_rows(stats)
    if not rows or total <= 0:
        return ""
    for row in rows:
        if _int(row.get("matchCount")) >= MIN_POSITION_SAMPLE:
            return str(row.get("position") or "").strip().upper()
    return str(rows[0].get("position") or "").strip().upper()


def _render_positions(
    lines: list[str],
    rows: list[dict],
    total: int,
    high_label: str,
) -> None:
    """渲染【流行位置】。"""
    if not rows or total <= 0:
        return
    lines.append("")
    lines.append(f"【流行位置】高分段（{high_label}）最近一周样本 {total} 场")
    smaller: list[str] = []
    for row in rows:
        mc = _int(row.get("matchCount"))
        label = position_label(row.get("position")) or str(row.get("position") or "未知位置")
        if mc < MIN_POSITION_SAMPLE:
            smaller.append(f"{label} {mc} 场")
            continue
        share = mc / total * 100
        lines.append(
            f"· {label}：{mc} 场（占样本 {share:.0f}%），胜率 {_rate(row.get('winCount'), mc)}"
        )
    if smaller:
        lines.append(f"· （样本不足 {MIN_POSITION_SAMPLE} 场，未计入结论：{'、'.join(smaller)}）")

    # 主位置的场均表现 —— 这才是「怎么玩」的落点
    top = rows[0]
    top_label = position_label(top.get("position")) or "主位置"
    lines.append(
        f"  主位置「{top_label}」场均：补刀 {_num(top.get('cs'))}、等级 {_num(top.get('level'), 1)}、"
        f"英雄伤害 {_num(top.get('heroDamage'))}、建筑伤害 {_num(top.get('towerDamage'))}、"
        f"K/D/A {_num(top.get('kills'), 1)}/{_num(top.get('deaths'), 1)}/{_num(top.get('assists'), 1)}"
    )


def _render_lane(
    lines: list[str],
    lane: list[dict] | None,
    high_label: str,
    position: str,
) -> None:
    """渲染【对线】—— **只给比例，不折算场次**。

    为什么只给比例：``laneOutcome`` 是按「对位」拆行的（一行一个对手英雄，
    敌法师一号位就回 125 行），求和得到的数**不是场次**（实测是同口径场次的
    1.5~3 倍，且各位置倍数不一致）。把那个数当成「对线 N 场」写出去，
    会读出「对线次数比这个英雄的出战场次还多」这种荒唐结论。
    比例是**单位无关**的，所以只报比例。理由记在
    ``dota_stratz.StratzClient.get_hero_lane_outcome``。
    """
    rows = [r for r in (lane or []) if isinstance(r, dict)]
    if not rows:
        return
    win = sum(_int(r.get("winCount")) for r in rows)
    loss = sum(_int(r.get("lossCount")) for r in rows)
    draw = sum(_int(r.get("drawCount")) for r in rows)
    total = win + loss + draw
    if total <= 0:
        return
    stomp_w = sum(_int(r.get("stompWinCount")) for r in rows)
    stomp_l = sum(_int(r.get("stompLossCount")) for r in rows)
    label = position_label(position) or "主位置"
    lines.append("")
    lines.append(f"【对线】高分段（{high_label}）· 主位置「{label}」")
    lines.append(
        f"· 对线占优 {win / total * 100:.0f}% / 打平 {draw / total * 100:.0f}% / "
        f"落后 {loss / total * 100:.0f}%（判定按补刀与经验差）"
    )
    lines.append(
        f"· 其中把对面打崩 {stomp_w / total * 100:.0f}%、自己被崩 {stomp_l / total * 100:.0f}%"
    )
    lines.append("· （该数据源的对线统计按「对位」计数，与上面的场次口径不同，这里只给比例）")


def _render_start_and_boots(
    lines: list[str],
    starting: list[dict] | None,
    boots: list[dict] | None,
    items: dict | None,
    high_label: str,
) -> None:
    """渲染【出门装 / 鞋子】。

    计数一律写「**次**」而不是「场」：出门装是可重复购买的消耗品
    （实测「铁树枝干」在同一位置会回**两行**，三号位各行合计 86070，
    远超该位置 11871 场），它统计的是**购买次数**。
    鞋子的 ``time`` 是该装备所有购买的时间**总和（秒）**，除以次数得平均值。
    """
    idx = ItemIndex(items)
    srows = sorted(
        [r for r in (starting or []) if isinstance(r, dict)],
        key=lambda r: -_int(r.get("matchCount")),
    )
    brows = sorted(
        [r for r in (boots or []) if isinstance(r, dict)],
        key=lambda r: -_int(r.get("matchCount")),
    )
    if not srows and not brows:
        return
    lines.append("")
    lines.append(f"【出门装 / 鞋子】高分段（{high_label}）")
    if srows:
        parts = []
        for row in srows[: MAX_ITEMS_PER_STAGE + 1]:
            name = idx.name(row.get("itemId"))
            mark = "（共享）" if row.get("wasGiven") else ""
            parts.append(f"{name}{mark} {_int(row.get('matchCount'))} 次")
        lines.append("· 出门装：" + "、".join(parts))
    if brows:
        parts = []
        for row in brows[:3]:
            name = idx.name(row.get("itemId"))
            mc = _int(row.get("matchCount"))
            secs = _int(row.get("time")) / mc if mc else 0
            when = f"，平均 {secs / 60:.1f} 分钟出" if secs > 0 else ""
            parts.append(f"{name} {mc} 次{when}")
        lines.append("· 鞋子：" + "、".join(parts))


def _aggregate_item_purchase(rows: list[dict] | None) -> list[dict]:
    """把 ``itemFullPurchase`` 的「按购买分钟拆行」聚成「一件装备一行」。

    聚合方式：按 ``itemId`` 累加 ``matchCount`` / ``winCount``，并用
    ``matchCount`` 对 ``time``（单位：**分钟**）加权求均值 —— 得到该装备的
    **典型成型时间**。

    **刻意不做百分比**：各行的计数与 ``stats`` 的场次**不是同一个样本池**
    （实测斧王三号位刃甲合计 15528，而该位置场次只有 11871），两者相除会
    得出 >100% 的荒唐值。详见
    :meth:`dota_stratz.StratzClient.get_hero_item_purchase`。

    Returns:
        每项 ``{"itemId", "sample", "win", "minute"}``，按 ``sample`` 倒序。
        ``minute`` 拿不到时为 ``None``（渲染成 ``-``，不用 0 冒充）。
    """
    agg: dict[int, dict] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        item_id = _int(row.get("itemId"), -1)
        mc = _int(row.get("matchCount"))
        if item_id < 0 or mc <= 0:
            continue
        entry = agg.setdefault(
            item_id,
            {"itemId": item_id, "sample": 0, "win": 0, "time_sum": 0, "time_n": 0},
        )
        entry["sample"] += mc
        entry["win"] += _int(row.get("winCount"))
        # ``time`` 缺失（None）的行**不进**时间加权 —— 否则会被当成「第 0 分钟买到」，
        # 渲染出「约 0 分钟成型」这种假数据（项目纪律：缺失不是 0）。
        if row.get("time") is not None:
            entry["time_sum"] += mc * _int(row.get("time"))
            entry["time_n"] += mc
    out: list[dict] = []
    for entry in agg.values():
        out.append(
            {
                "itemId": entry["itemId"],
                "sample": entry["sample"],
                "win": entry["win"],
                "minute": (entry["time_sum"] / entry["time_n"]) if entry["time_n"] else None,
            }
        )
    out.sort(key=lambda e: -e["sample"])
    return out


def _render_high_items(
    lines: list[str],
    rows: list[dict] | None,
    items: dict | None,
    position: str,
    high_label: str,
) -> None:
    """渲染【高分段出装】—— 排序 + 典型成型时间 + 该装备的对局胜率。

    为什么只排先后、不给占比：``itemFullPurchase`` 的计数与 ``stats`` 场次
    不是一个口径（见 :func:`_aggregate_item_purchase`），折算百分比会 >100%。
    """
    picked = [
        e for e in _aggregate_item_purchase(rows) if e["sample"] >= MIN_ITEM_PURCHASE
    ][:MAX_HIGH_ITEMS]
    if not picked:
        return
    idx = ItemIndex(items)
    label = position_label(position)
    where = f"主位置「{label}」" if label else "全部位置"
    parts: list[str] = []
    for entry in picked:
        name = idx.name(entry["itemId"], default="")
        if not name:
            continue
        minute = entry["minute"]
        when = f"约 {minute:.0f} 分钟" if minute is not None else "成型时间 -"
        parts.append(f"{name}（{when}、对局胜率 {_rate(entry['win'], entry['sample'])}）")
    if not parts:
        return
    lines.append("")
    lines.append(f"【高分段出装】{high_label} · {where}（按购买热度排序，含前期小件）")
    lines.append("· " + "、".join(parts))
    lines.append(
        "· （口径：这是 STRATZ 按「购买分钟」统计的高分段购买热度，样本池与上面的"
        "**场次不是同一口径**，所以只排先后、不折算百分比；括号里的胜率是「出过这件"
        "装备的对局」胜率，**没有『不出』的对照**，不能据此断定这件装备该不该出）"
    )


def _render_item_route(lines: list[str], item_pop: dict | None, items: dict | None) -> None:
    """全分段出装路线（OpenDota ``/heroes/{id}/itemPopularity``，四阶段）。

    各阶段桶里是**购买次数**（不是场次），阶段之间按时间互斥。但它与 STRATZ
    高分段那块的**样本量级差两个数量级**（同一件装备：全分段几十次 vs
    高分段上万），所以文案里必须标明来源，不能让两边的数字并列可比。
    """
    if not isinstance(item_pop, dict) or not item_pop:
        return
    idx = ItemIndex(items)
    stages = (
        ("开局", "start_game_items"),
        ("前期", "early_game_items"),
        ("中期", "mid_game_items"),
        ("后期", "late_game_items"),
    )
    rendered: list[str] = []
    for label, key in stages:
        bucket = item_pop.get(key)
        if not isinstance(bucket, dict) or not bucket:
            continue
        ranked = sorted(bucket.items(), key=lambda kv: -_int(kv[1]))[:MAX_ITEMS_PER_STAGE]
        names = "、".join(
            f"{idx.name(k)} {_int(v)} 次" for k, v in ranked if idx.name(k, default="")
        )
        if names:
            rendered.append(f"· {label}：{names}")
    if not rendered:
        return
    lines.append("")
    lines.append("【出装路线】全分段（OpenDota 各阶段购买次数，样本口径与上面的高分段不同）")
    lines.extend(rendered)


def _render_pro(lines: list[str], pro: dict | None) -> None:
    """职业局（OpenDota ``/heroStats`` 的 pro_* 字段，最近 7 天职业赛）。"""
    if not isinstance(pro, dict) or not pro:
        return
    pick = _int(pro.get("pro_pick"))
    win = _int(pro.get("pro_win"))
    ban = _int(pro.get("pro_ban"))
    if pick <= 0 and ban <= 0:
        lines.append("")
        lines.append("【职业局】最近 7 天职业赛没有该英雄的出场记录")
        return
    lines.append("")
    lines.append("【职业局】最近 7 天职业赛")
    lines.append(f"· 出场 {pick} 场，胜 {win} 场（胜率 {_rate(win, pick)}）；被 ban {ban} 次")


def format_hero_playstyle(
    *,
    hero_name: str,
    stats: list[dict] | None = None,
    starting: list[dict] | None = None,
    boots: list[dict] | None = None,
    lane: list[dict] | None = None,
    high_items: list[dict] | None = None,
    item_pop: dict | None = None,
    pro: dict | None = None,
    items: dict | None = None,
    notes: list[str] | None = None,
    high_label: str = BRACKET_HIGH_LABEL,
) -> str:
    """把两个数据源的原始行拼成一段结构化文本（交给模型组织回答）。

    Args:
        stats: STRATZ ``heroStats.stats(groupByPosition: true)`` 的行。
        starting / boots: STRATZ ``itemStartingPurchase`` / ``itemBootPurchase``。
            **计数是「购买次数」不是「场次」**（消耗品一局能买多个）。
        lane: STRATZ ``laneOutcome(isWith: false, positionIds: [主位置])`` 的行。
            **只用来算比例**（它的计数单位是「对位」，不是场次）。
        high_items: STRATZ ``itemFullPurchase(positionIds: [主位置])`` 的行。
            **只用来排先后、算典型成型时间与对局胜率**，不折算百分比。
        item_pop: OpenDota ``/heroes/{id}/itemPopularity`` 的四阶段字典
            （各阶段是「购买次数」，与高分段那块不是一个样本口径）。
        pro: OpenDota ``/heroStats`` 里该英雄的 ``pro_pick`` / ``pro_win`` /
            ``pro_ban``。
        notes: 取数缺口说明（哪块没拿到、为什么），会原样附在末尾。
    """
    lines: list[str] = [f"=== {hero_name} · 当前版本打法 ==="]
    rows, total = _position_rows(stats)
    _render_positions(lines, rows, total, high_label)
    if rows and total > 0:
        _render_lane(lines, lane, high_label, main_position(stats))
        _render_start_and_boots(lines, starting, boots, items, high_label)
    _render_high_items(lines, high_items, items, main_position(stats), high_label)
    _render_item_route(lines, item_pop, items)
    _render_pro(lines, pro)

    tail = [str(n) for n in (notes or []) if n]
    if (not rows or total <= 0) and not tail:
        # 调用方没交代缺口时自己兜一句；调用方给了 notes 就不重复说同一件事
        tail.append("高分段（位置 / 对线 / 出门装）这一块没有取到数据。")
    if tail:
        lines.append("")
        lines.append("（口径说明：" + " ".join(tail) + "）")
    return "\n".join(lines)
