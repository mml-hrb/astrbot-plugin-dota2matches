"""调用大模型生成 Dota 2 分析报告。

职责：
1. 把 OpenDota 的数据拼装成结构化的提示词（Prompt）；
2. 通过 AstrBot 的 Provider 接口，或插件自带的 OpenAI 兼容通道拿到模型回复。

本模块不直接依赖具体平台，只依赖 AstrBot 的 ``Context``。
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

try:  # 插件目录被作为包加载时的相对导入
    from .dota_format import (
        HERO_ATTR_ZH,
        HERO_ROLE_ZH,
        MIN_HERO_SAMPLE_GAMES,
        POSITION_LABELS,
        build_match_data_text,
        fmt_ago,
        fmt_duration,
        fmt_timestamp,
        fmt_wan,
        format_summary_block,
        hero_meta_label,
        hname,
        match_quality_block,
        match_result,
        mode_text,
        normalize_focus_ids,
        rank_text,
        result_text,
        recent_hero_usage,
        summarize_hero_history,
        summarize_matches,
        to_steam_id64,
    )
except ImportError:  # 兜底：以普通模块方式加载时，把插件目录加入 sys.path
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dota_format import (  # type: ignore[no-redef]
        HERO_ATTR_ZH,
        HERO_ROLE_ZH,
        MIN_HERO_SAMPLE_GAMES,
        POSITION_LABELS,
        build_match_data_text,
        fmt_ago,
        fmt_duration,
        fmt_timestamp,
        fmt_wan,
        format_summary_block,
        hero_meta_label,
        hname,
        match_quality_block,
        match_result,
        mode_text,
        normalize_focus_ids,
        rank_text,
        result_text,
        recent_hero_usage,
        summarize_hero_history,
        summarize_matches,
        to_steam_id64,
    )

#: 近期表现分析报告的章节要求
RECENT_REPORT_FORMAT = """请严格按照以下结构输出（使用 Markdown 二级/三级标题）：

## 一句话结论
（一句话概括这名玩家当前的竞技状态，直接给判断，不要铺垫）

## 近期状态走势
（结合胜率、连胜连败、前后半段胜率对比、KDA 与经济的波动，说明状态是在上升、下滑还是原地踏步）

## 打法风格画像
（这是重点。从分路倾向、英雄池、场均补刀与 GPM、伤害构成、死亡数、参团程度、单排/组队比例等数据推断出这名玩家的打法特征，例如：偏发育的后期核心 / 高风险的节奏发起者 / 稳健的功能型辅助 / 容易被抓的莽夫。必须给出数据依据）

## 数据亮点与短板
（分别列出 2-3 条最能说明问题的数据，注明具体数值）

## 可执行的改进建议
（3 条以内，每条要具体到「做什么、为什么」，例如「把场均死亡从 7.2 压到 5 以内，重点是在野区丢失视野后不要单人推线」）"""

#: 单场比赛复盘报告的章节要求
#:
#: 这一版的重点是**把「焦点玩家点评」从一段概述变成一张逐项清单**：每位焦点
#: 玩家都被要求按 9 个固定子项逐条写，每项都必须带数字，写完再给总评。早先
#: 的写法只罗列了「依次覆盖：对线表现、经济来源、发育拐点……」这一类提示，模型
#: 实际输出经常退化成一段三四句的泛泛之谈 —— 因为「依次覆盖」没有任何一项
#: 是必须交付的，弱模型会直接合并成一段。
#:
#: 另外补了两条纪律：一是**先给参照系再下结论**（配合数据块里的「同英雄分位」，
#: 避免拿孤立绝对值夸骂），二是**明确解除 800 字全局上限对本报告的限制** ——
#: 全局系统提示词里有「总长度 800 字以内」，与本节的细节要求直接冲突，不写清楚
#: 模型会优先服从那个更短的约束。
MATCH_REPORT_FORMAT = """写作纪律（先读这五条，再动笔）：
1. 数据块里给出的**每一个维度都是证据**，优先引用具体数值（时间点、金币、次数、百分比、装备名、技能名），不要写「发挥不错」这类没有依据的话。
2. **只使用数据块里出现过的数字**。数据块没给、或标注为「不可用 / 数据未提供 / -」的维度，就写「数据未提供」，不要推测、不要编造。
3. 同一维度同时给了总量与构成时（英雄伤害 vs 输出构成、净经济 vs 经济来源分解、参团率 vs 团战逐场贡献、英雄治疗量 vs 治疗分布），必须把两者结合起来解释「为什么」，而不是各说一句。
4. **下判断前先给参照系**：能拿到「同英雄分位」的就用分位说话（优 93% 与优 9% 是完全不同的评价）；拿不到的就用对手的同类数据、他本人的逐分钟曲线做对照。禁止拿孤立绝对值下结论 —— 同一个 GPM 放在 1 号位和 5 号位身上含义相反。
5. 该批评就批评：该做没做的事、该出没出的装备、该撤没撤的团，都要点名说清。要允许自己得出「他这场打得不好」这个结论。对事不对人，不写脏话、不做人身攻击。

篇幅：焦点玩家点评是全文重心，**不要为了压缩总字数把它写薄**。整篇通常 1500~3000 字；本轮篇幅以本节的要求为准，**不受「800 字以内」那类全局限制的约束**。

请严格按照以下结构输出（使用 Markdown 二级/三级标题）：

## 一句话结论
（一句话给这场比赛定性，并给焦点玩家的总体表现下一个明确判断。直接给结论，不要铺垫）

## 比赛走势复盘
（按时间顺序梳理这场比赛，必须覆盖：对线期哪一路先崩或先拿到优势、第一个重大转折发生在第几分钟、双方经济曲线与经验曲线在哪些时间点发生交叉、肉山与防御塔的争夺节奏、最后胜负是怎么被决定的。引用具体时间点和经济差数值）

## 比赛质量评估
（判断这是胶着局、翻盘局还是崩盘/碾压局，并给出可核对的依据：经济领先易手次数、最终胜方是否曾落后及其最大幅度、双方总阵亡、建筑存活对比、团战次数与单场团战的阵亡规模、对局完整性。最后说明哪一边的失误更致命）

## 关键数据对照
（把双方的客观数据摆出来对比，至少覆盖：总净经济、建筑存活（塔/兵营）、视野投入（假眼/真眼数量与被反数量、排眼数量、买了多少插了多少）、团队英雄伤害与治疗、团队阵亡与买活、经济领先易手次数、十人里同英雄分位的分布。用表格或分点列出并注明数值，最后说明这些差距把胜负解释到了什么程度、哪些差距其实与胜负无关）

## 焦点玩家点评
（对数据中标记为「焦点玩家」的**每一位**选手分别点评。**每人一个三级小标题**，标题格式：`### 英雄名（选手标识）· 胜/负 · K/D/A`。
  每位焦点玩家都**必须逐条**覆盖下面 9 个子项，每个子项 1~3 句、**每句都要带数字**；某一项数据确实没给，就在该项下明写「数据未提供」，**不要跳过、也不要合并成笼统的一大段**：

1. **同英雄分位定位** —— 直接用「同英雄分位」那一行，说明他这局用这个英雄、在玩同一英雄的人里属于上游/中游/下游，并点出最突出与最拖后腿的两项（例如「补刀分位只有 7%，治疗分位却有 90%」）。
2. **对线阶段** —— 分路与号位、是否游走、对线补刀效率、第 5/10 分钟的补刀·金钱·经验累计值，判断他是压住了对面还是被打崩、大概从什么时候开始崩。
3. **经济来源结构** —— 用「经济来源分解」的占比说明他靠什么变强（打架流 / 刷钱流 / 推进流 / 吃团队资源），再对照「经验来源分解」与「刷钱细节」（远古野、堆野、吃符），指出这套来源是否可持续、有没有挤占队友资源。
4. **发育节奏与拐点** —— 用逐分钟发育曲线指出他金钱/等级增长最快与停滞的时段，并把拐点与同时段的团战、阵亡、关键装备到手时间一一对上。
5. **输出与承伤画像** —— 英雄伤害总量 + 输出构成（按技能、按目标单位）+ 对英雄伤害明细 + 单次最高伤害；再看他承受了谁多少伤害、被哪些技能打，判断他是在有效输出还是被针对架住，有没有「伤害打在小兵身上」这类问题。
6. **击杀网络与生存** —— 击杀记录、被谁击杀、击杀时间线、连杀记录、阵亡+复活耗时占比，指出他的击杀集中在哪个时段、最容易被谁抓，以及每次死亡换到了什么（换人头、换塔还是纯送）。
7. **视野与辅助工作** —— 假眼/真眼数量与时间跨度、被反掉几个、排掉敌方几个眼、买了几个插了几个、堆野、吃符时间点、信使击杀、信号打点。**非辅助也要照实写**，没有就写「本场未插眼」。
8. **技能与道具** —— 技能加点顺序的取舍是否合理、主要技能的使用次数与命中英雄次数、技能施放对象、关键装备与全部装备的时间线是否踩在节奏点上（明确指出哪件装备晚了、早了或多此一举）、中立物品与神杖/魔晶的获取时间，以及道具使用次数里有没有明显异常。
9. **团战逐场贡献** —— 用团战块里属于他的每一行「焦点贡献」，逐场说明这一波他打出多少伤害/治疗、是否阵亡、经济与经验变化；挑出最关键的一场与最失败的一场分别展开。

  写完这 9 项后，再给这一位选手一段 **3~5 句的总评**：这场最大的问题是什么、做得最好的是什么、如果重打一次最该改的一件事是什么。总评里的每个判断都必须能对应到前面的具体数字。

  只有一位焦点玩家就只写一位；有几位就写几位，**一位都不能漏**。）

## 其余选手点评
（逐一点评除焦点玩家之外的其余选手，每人 2~3 句，说明他在这场比赛里扮演的角色、发挥水平、是否完成了自己位置的职责；至少引用一项他的数据（装备、视野、伤害、同英雄分位、补刀效率任选）。用「英雄名（选手标识）」开头。**输赢双方的选手都要写**，不要只写赢的一方）

## 关键转折点
（列出本场最重要的 1~3 个转折点，说明当时发生了什么、对后续局势造成了什么影响、焦点玩家在这些转折点上是受益者还是责任人。优先引用团战逐场数据与关键事件时间轴里的具体时间点）"""

#: 数据块的口径说明。
#:
#: 这些不是客气话，而是**不写清楚就会被读错的点**。每一条都对应一个真实陷阱：
#: 分位是按分钟归一化的（短局会整体抬高 GPM 量级）、治疗分布里含自我治疗、
#: 符文类型本仓库证伪不了、团战块只列前 12 次。缺了这段，模型会把「Turbo 局
#: GPM 1752」当成常规局的怪物数据，也会把自我治疗算成团队贡献。
DATA_CAVEATS = """=== 读数据前必须知道的口径 ===
1. 「同英雄分位」的百分比 = 他优于多少比例的同英雄玩家，越高越好；其中「死亡频率」已按「越低越好」反向成「生存」。
2. 分位是按**每分钟**归一化的：节奏快的模式（Turbo 等）或时长很短的局会整体抬高 GPM/XPM 的量级。判断水平请以分位为准，不要拿绝对值跨模式比较。
3. 「治疗分布」里的「自我治疗」不计入英雄治疗量，「给队友合计」才等于英雄治疗量。评价团队贡献时看后者。
4. 「吃符时间点」只给时间、不给符文类型（数据源的类型编码本仓库无法证实），所以只能判断他有没有在符点去抢符，不能说他吃到了哪种符。
5. 「视野收支」里的「排眼」指清掉**敌方**的眼，是判断辅助有没有做视野对抗的关键；「买X/插Y（差N）」的差值表示买了没插。
6. 团战块**只列出前 12 次**团战（原文会注明总次数）；「焦点贡献」是焦点玩家在该次团战里的个人数据。
7. 任何标注「数据未提供 / 不可用 / -」的维度都表示数据源没有给，请如实写「数据未提供」，不要用同类比赛的经验补全。"""


def mode_caveat(match: dict) -> str:
    """模式相关的口径提醒。

    Turbo（加速模式）的经济/经验获取速度远高于常规局，样本里一局 23 分钟的
    Turbo 能出现 GPM 1752 的 Sniper —— 与常规局放在一起比会得出「这是顶级
    发挥」的错误结论，而他的同英雄分位其实只有 93%（上游但谈不上碾压）。
    """
    mode = mode_text(match)
    lowered = mode.lower()
    if "turbo" in lowered or "加速" in mode:
        return (
            f"本局模式为「{mode}」：金钱与经验获取速度远高于常规局，"
            "GPM / XPM / 净经济的绝对值不要与常规局直接比较，"
            "判断水平请以「同英雄分位」为准。"
        )
    return ""

#: 监听自动短评的系统提示词。
#: **必须与报告用的 ``analysis_system_prompt`` 分开**：后者要求「Markdown 小标题、
#: 单场深度复盘 1500~3000 字」，把 2~4 句的短评交给它写会被带成一篇小作文，
#: 正好违背监听推送「几句话说清楚这局打得怎么样」的初衷。
WATCH_COMMENT_SYSTEM_PROMPT = """你是一位 Dota 2 老玩家，负责在群聊里对刚打完的一局做一个口头点评。

写作要求：
1. 只写 2~4 句话，总长度控制在 120 字以内。直接给结论，不要铺垫、不要总结段。
2. 用聊天口吻，像朋友在群里随口点评。不要 Markdown 标题、不要分点列举、
   不要写「点评如下」「综上」之类的套话。
3. 必须点到这局的胜负与该玩家的 K/D/A，并把它放进这名玩家近期的状态里做对照：
   这局算正常发挥、明显超常，还是明显拉胯。
4. 只使用我给出的数字。没有给我的数据（装备、经济、团战细节等）一律不要提，
   更不要编造。
5. 对事不对人：可以说「这局送得多」「发挥好于近期平均」，但不要脏话、不要人身攻击。
6. 直接输出点评正文，不要任何开场白或结尾寒暄。"""

#: 监听短评的用户提示词里的输出要求（与 ``WATCH_COMMENT_SYSTEM_PROMPT`` 配套）。
WATCH_COMMENT_FORMAT = """请直接输出一段 2~4 句话、120 字以内的点评：
· 先说这局的结果与他本人的 K/D/A；
· 再对照上面的近期战绩，说明这局算正常、超常还是拉胯，并用给出的数字作依据；
· 不要分点、不要小标题、不要超过 4 句话。"""


def _watch_focus_line(
    match: dict,
    heroes: dict[int, dict],
    account_id: int,
    name: str,
) -> str:
    """监听短评里那一行「谁、什么英雄、多少 KDA、赢没赢」。"""
    label = f"{name}（account_id={account_id}）" if name else f"account_id={account_id}"
    player = None
    for entry in match.get("players") or []:
        if not isinstance(entry, dict):
            continue
        if int(entry.get("account_id") or 0) == int(account_id):
            player = entry
            break
    if player is None:
        # 玩家不在本局名单里（数据源没给全 / 焦点已过期）：如实说明，别编数据
        return f"{label}：本局数据里没有这名玩家"
    return (
        f"{label}：{hname(heroes, player.get('hero_id'))} · "
        f"{player.get('kills', 0)}/{player.get('deaths', 0)}/{player.get('assists', 0)} · "
        f"该玩家{result_text(player)}"
    )


def build_watch_comment_prompt(
    match: dict,
    heroes: dict[int, dict],
    focus_account_ids: int | list[int] | tuple[int, ...] | None = None,
    focus_names: dict[int, str] | None = None,
    recent_blocks: list[str] | None = None,
) -> str:
    """构造「监听推送的短评」提示词。

    与 :func:`build_single_match_analysis_prompt` 的关键区别是**故意只要很少的数据**：

    * 本场只给胜负、K/D/A，外加英雄 / 时长 / 模式这几个用于「说清楚是哪局」的字段；
    * 再有就是焦点玩家近期战绩（``recent_blocks``，由调用方用
      :func:`dota_format.format_summary_block` 生成），用来判断本场是否异常。

    这里**不渲染任何解析产物**（逐分钟曲线、伤害构成、视野日志、团战逐人…）。
    这样做的直接收益是监听推送不再需要等 OpenDota 解析录像——解析要等十几分钟，
    而短评只需要比赛被收录时就有胜负与 K/D/A。想要完整复盘的走
    ``/d2 单场 <比赛ID>``，那条链路仍然用深度提示词。

    Args:
        match: 比赛详情。
        heroes: 英雄常量 ``{hero_id: {...}}``。
        focus_account_ids: 需要点评的玩家（同一局可能有多位被监听的玩家）。
        focus_names: ``{account_id: 昵称}``，让提示词里出现可读的名字。
        recent_blocks: 每位焦点玩家一段近期战绩文本（可空）。
    """
    ids = normalize_focus_ids(focus_account_ids)
    names = focus_names or {}
    radiant_win = bool(match.get("radiant_win"))

    lines: list[str] = []
    if len(ids) > 1:
        lines.append(
            f"请根据下面这场比赛的结果，分别对 {len(ids)} 位被关注的玩家各写一段简短点评。"
        )
    else:
        lines.append("请根据下面这场比赛的结果，写一段简短点评。")
    lines.append("")

    lines.append("=== 本场比赛 ===")
    lines.append(f"比赛ID: {match.get('match_id')}")
    lines.append(
        f"开始时间: {fmt_timestamp(match.get('start_time'))}"
        f"（{fmt_ago(match.get('start_time'))}）"
    )
    lines.append(
        f"时长: {fmt_duration(match.get('duration'))} · 模式: {mode_text(match)}"
    )
    lines.append(
        f"比分: 天辉 {match.get('radiant_score', 0)} : {match.get('dire_score', 0)} 夜魇"
        f" · {'天辉' if radiant_win else '夜魇'}获胜"
    )
    lines.append("")

    lines.append("=== 被关注的玩家（本场）===")
    if ids:
        for account_id in ids:
            lines.append(
                _watch_focus_line(match, heroes, account_id, names.get(account_id) or "")
            )
    else:
        # 没有指定玩家时退化成「这场比赛」的点评，仍比不推好
        lines.append("（本次没有指定具体玩家，就这局比赛本身点评）")
    lines.append("")

    if recent_blocks:
        lines.append("=== 近期战绩（不含本场，用于判断本场是否正常）===")
        lines.append("\n\n".join(recent_blocks))
        lines.append("")
    else:
        lines.append("=== 近期战绩 ===")
        lines.append("（拿不到近期战绩，只能就本场的数据点评，不要臆测他的状态趋势）")
        lines.append("")

    lines.append("=== 输出要求 ===")
    lines.append(WATCH_COMMENT_FORMAT)
    return "\n".join(lines)


def _player_headline(
    profile_data: dict, account_id: int, matches: list[dict]
) -> str:
    """拼出玩家基础信息头。"""
    profile = (profile_data or {}).get("profile") or {}
    name = profile.get("personaname") or (matches[0].get("personaname") if matches else None)
    name = name or f"账号{account_id}"
    steam_id = profile.get("steamid") or to_steam_id64(account_id)
    rank = profile_data.get("rank_tier") if profile_data else None
    return (
        f"玩家昵称: {name}\n"
        f"account_id: {account_id}\n"
        f"SteamID64: {steam_id}\n"
        f"rank_tier: {rank if rank else '未知'}"
    )


def build_recent_analysis_prompt(
    account_id: int,
    profile_data: dict,
    wl: dict,
    matches: list[dict],
    heroes: dict[int, dict],
    hero_rows: list[dict],
    economy_samples: int | None = None,
    requested_count: int = 20,
    pool_scope: str = "",
) -> str:
    """构造「近期表现与打法风格分析」的提示词。"""
    summary = summarize_matches(matches, economy_samples=economy_samples)
    rows = summarize_hero_history(hero_rows)

    lines: list[str] = []
    lines.append("请分析下面这位 Dota 2 玩家最近一段时间的表现与打法风格。")
    lines.append("")
    lines.append("=== 玩家信息 ===")
    lines.append(_player_headline(profile_data, account_id, matches))

    wins = int(wl.get("win") or 0)
    loses = int(wl.get("lose") or 0)
    if wins + loses:
        lines.append(
            f"生涯总战绩: {wins + loses} 场 · {wins} 胜 {loses} 负 · "
            f"胜率 {wins / (wins + loses) * 100:.1f}%"
        )

    if rows:
        # 这份英雄池是**当前版本口径**（不是生涯），抬头必须写清楚：
        # 否则模型会把它当生涯数据，说出「他生涯就玩过这几个英雄」这种错话
        title = "他在当前版本的英雄池" if pool_scope else "英雄池"
        lines.append(
            f"{title}（按场次倒序，前 10）: "
            + "、".join(
                f"{hname(heroes, row['hero_id'])} {row['games']}场/{row['winrate']:.0f}%"
                for row in rows[:10]
            )
        )
    if pool_scope:
        lines.append(f"英雄池口径: {pool_scope}")

    lines.append("")
    lines.append(f"=== 最近 {len(matches)} 场对局逐场明细（从最近往前）===")
    lines.append(
        "序号 | 比赛ID | 时间 | 英雄 | 结果 | K/D/A | GPM | XPM | 补刀 | 英雄伤害 | 时长 | 模式"
    )
    for index, match in enumerate(matches, start=1):
        lines.append(
            f"{index} | {match.get('match_id')} | {fmt_timestamp(match.get('start_time'))} | "
            f"{hname(heroes, match.get('hero_id'))} | "
            f"该玩家{result_text(match)} | "
            f"{match.get('kills', 0)}/{match.get('deaths', 0)}/{match.get('assists', 0)} | "
            f"{match.get('gold_per_min') or '-'} | {match.get('xp_per_min') or '-'} | "
            f"{match.get('last_hits') or '-'} | {match.get('hero_damage') or '-'} | "
            f"{fmt_duration(match.get('duration'))} | {mode_text(match)}"
        )

    lines.append("")
    lines.append("=== 聚合统计 ===")
    lines.append(format_summary_block(summary, heroes))

    lines.append("")
    lines.append("=== 输出要求 ===")
    lines.append(RECENT_REPORT_FORMAT)
    return "\n".join(lines)


#: OpenDota 的 ``lane_role`` → 中文分路
LANE_ROLE_ZH: dict[int, str] = {
    1: "优势路",
    2: "中路",
    3: "劣势路",
    4: "野区",
}

#: 「版本强势英雄 → 哪些适合他」的输出结构
HERO_PICK_FORMAT = """请严格按照以下结构输出（Markdown 二级标题）：

## 一句话结论
（直接说这位玩家这版本该玩什么，一句话，不要铺垫）

## 已经在玩、版本又强（直接上分）
## 会玩但最近没怎么用（值得捡起来）
## 没玩过、但位置对得上（可以练）
## 这版本别碰

每个英雄写成一行：**英雄名** · 版本胜率 xx.x% · 他自己的数据 · 一句话理由。

硬性要求：

1. **只能从上文给出的版本榜里挑英雄**。不要在榜外推荐，更不要凭印象编造胜率或英雄名。
2. 每条推荐都要同时给出「版本数据」与「他的数据」；哪一项没有就如实写「无数据」，
   不要用近似数字凑。**判断「他玩过没有」一律以「版本榜前 N 名里他玩过的」那一节为准**
   （英雄池表被按场次截断了，不能因为没出现在那张表里就说他「无数据」）。
3. 第三类（没玩过）的依据只能是他近期的分路分布，措辞用「位置对得上」，
   不能写成「他很擅长」。
4. 「已经在玩」这类里，如果某个英雄他自己的胜率明显低于 50%，要点出来。
5. 任何一类确实没有合适对象，就写「暂无」，不要为了凑结构硬填。
6. 「近 N 场出场」列统计的是我另外拉取的最近对局：写「未出现」只说明**这批对局里没打**，
   不代表他没玩过这个英雄，别写成「从没玩过」；判断玩没玩过仍以上面那节为准。
7. 他标注「样本不足」的英雄（场次太少）不要拿来下「他擅长 / 他不擅长」的结论，
   最多说「样本太少，参考价值有限」。
8. 总长度 500 字以内，不要复述我给你的表格。"""


def _lane_role_text(matches: list[dict]) -> str:
    """统计近期比赛的分路分布。缺 ``lane_role`` 的场次单独计数，不猜。"""
    counts: dict[int, int] = {}
    unknown = 0
    for match in matches or []:
        try:
            lane = int(match.get("lane_role"))
        except (TypeError, ValueError):
            unknown += 1
            continue
        if lane not in LANE_ROLE_ZH:
            unknown += 1
            continue
        counts[lane] = counts.get(lane, 0) + 1
    if not counts:
        return "数据缺失"
    parts = [
        f"{LANE_ROLE_ZH[lane]} {count} 场"
        for lane, count in sorted(counts.items(), key=lambda item: -item[1])
    ]
    if unknown:
        parts.append(f"（另有 {unknown} 场无分路数据）")
    return "、".join(parts)


def build_hero_pick_prompt(
    *,
    account_id: int,
    profile_data: dict,
    wl: dict,
    hero_rows: list[dict],
    matches: list[dict],
    meta_rows: list[dict],
    meta: dict,
    heroes: dict[int, dict],
    patch: dict | None = None,
    board_size: int = 25,
    pool_size: int = 20,
    pool_scope: str = "",
) -> str:
    """构造「版本强势英雄里，哪些适合他」的提示词。

    三层数据缺一不可：

    1. **版本榜**（客观）：最近 7 天各英雄的胜率与场次；
    2. **他的英雄池**（熟练度）：各英雄场次 / 胜率 / 上次使用，并**逐条标注**
       该英雄在版本榜里的名次 —— 逼模型去做交叉，而不是凭印象挑；
    3. **他的近期表现**（当前状态）：逐场 KDA / GPM 与分路分布。

    模型只负责「挑与解释」：所有数字都由这里给定，提示词里也明确禁止它在
    榜外推荐或编造胜率。这样即便模型换了一个，结论也不会飘。
    """
    board = list(meta_rows[:board_size])
    board_index = {
        row["hero_id"]: rank for rank, row in enumerate(meta_rows, start=1)
    }
    rows = summarize_hero_history(hero_rows)

    lines: list[str] = []
    lines.append(
        "请从下面这份「当前版本强势英雄」榜单里，挑出最适合这位玩家上手的英雄，"
        "并给出理由。"
    )
    lines.append("")

    # ---- 口径 ----
    lines.append("=== 数据口径 ===")
    days = int(meta.get("window_days") or 7)
    lines.append(
        f"统计窗口: 最近 {days} 天全分段公开对局（滚动窗口，不是严格按补丁切分）"
    )
    patch_name = str((patch or {}).get("name") or "")
    if patch_name:
        lines.append(f"版本: {patch_name}（OpenDota 记录的最新补丁）")
    lines.append(f"样本门槛: 单英雄 ≥ {int(meta.get('threshold') or 0)} 场")
    position = str(meta.get("position") or "")
    if position:
        lines.append(f"榜单范围: 已按位置筛选为 {POSITION_LABELS.get(position, position)}")
    if pool_scope:
        # 「他的英雄池」那一节是按**当前版本**取的（并可能含加速局），
        # 不写清楚的话模型会把它当成生涯数据，说出「他生涯玩过 N 场」这种错话
        lines.append(f"他的英雄池口径: {pool_scope}")
    lines.append("")

    # ---- 版本榜 ----
    lines.append(f"=== 版本强势英雄（按胜率倒序，前 {len(board)} 名）===")
    lines.append("排名 | 英雄 | 胜率 | 场次 | 主属性 | 官方定位 | 近 3 天走势")
    for rank, row in enumerate(board, start=1):
        attr = HERO_ATTR_ZH.get(row.get("attr") or "", row.get("attr") or "-")
        roles = "/".join(
            HERO_ROLE_ZH.get(role, role) for role in (row.get("roles") or [])[:3]
        )
        trend = row.get("trend")
        trend_text = "无数据" if trend is None else f"{trend * 100:+.1f} 个百分点"
        lines.append(
            f"{rank} | {hero_meta_label(row, heroes)} | {row['winrate'] * 100:.1f}% | "
            f"{row['pick']} | {attr} | {roles} | {trend_text}"
        )
    lines.append("")

    # ---- 玩家 ----
    lines.append("=== 这位玩家 ===")
    lines.append(_player_headline(profile_data, account_id, matches))
    rank_tier = (profile_data or {}).get("rank_tier")
    if rank_tier:
        lines.append(f"段位: {rank_text(rank_tier)}")
    wins = int(wl.get("win") or 0)
    loses = int(wl.get("lose") or 0)
    if wins + loses:
        lines.append(
            f"生涯总战绩: {wins + loses} 场 · {wins} 胜 {loses} 负 · "
            f"胜率 {wins / (wins + loses) * 100:.1f}%"
        )
    lines.append("")

    # ---- 英雄池（与版本榜交叉标注）----
    # 时效列用**近期对局现算**的出场次数，不用 /players/{id}/heroes 的
    # last_played：实测该字段系统性陈旧（池内最新值停在 69 天前，而玩家三天前
    # 还在打），照抄会让模型以为他「很久没动」，进而把当下最常玩的英雄
    # 归到「会玩但没怎么用」里。
    recent_counts, _recent_latest = recent_hero_usage(matches)
    recent_label = f"近 {len(matches)} 场出场" if matches else "近期出场"
    lines.append(f"=== 他的英雄池（按场次倒序，前 {pool_size}）===")
    if rows:
        lines.append(f"英雄 | 场次 | 胜率 | {recent_label} | 与版本榜的关系")
        for row in rows[:pool_size]:
            hero_id = row["hero_id"]
            rank = board_index.get(hero_id)
            if rank is None:
                relation = "不在版本榜内"
            else:
                hit = meta_rows[rank - 1]
                mark = "★" if rank <= board_size else ""
                relation = f"{mark}版本榜第 {rank} 名（胜率 {hit['winrate'] * 100:.1f}%）"
            hits = recent_counts.get(hero_id, 0)
            fresh_text = f"{hits} 次" if hits else "未出现"
            lines.append(
                f"{hname(heroes, hero_id)} | {row['games']} | {row['winrate']:.1f}% | "
                f"{fresh_text} | {relation}"
            )
    else:
        lines.append("（没有查到英雄池数据）")
    lines.append("")

    # ---- 版本榜 ∩ 他的英雄池 ----
    # 英雄池表按**场次**截断到前 N。实测的坑：玩家玩过 24 场的版本榜第 1 名
    # （冥魂大帝）排不进前 20，模型看到的资料里就没有它，于是如实写「无数据」，
    # 甚至把它归到「没玩过、可以练」—— 而规则版（用完整池）明确写着「玩过 24 场」。
    # 同一份数据两条链路给出互相矛盾的结论，说明**给的视图不对**。
    # 所以这里改按**版本榜名次**再列一遍交叉结果：凡是榜内他玩过的都在这里，
    # 模型不需要在截断的英雄池里碰运气。
    played = {row["hero_id"]: row for row in rows}
    crossed = [
        (rank, row, played[row["hero_id"]])
        for rank, row in enumerate(board, start=1)
        if row["hero_id"] in played
    ]
    lines.append(f"=== 版本榜前 {len(board)} 名里他玩过的（按版本榜名次排）===")
    if crossed:
        lines.append(f"版本排名 | 英雄 | 版本胜率 | 他的场次 | 他的胜率 | {recent_label}")
        for rank, row, entry in crossed:
            hits = recent_counts.get(row["hero_id"], 0)
            fresh_text = f"{hits} 次" if hits else "未出现"
            games = int(entry["games"])
            # 1 场 0% 和 1 场 100% 都不是信息，标出来免得模型当成「他不擅长 / 是他的绝活」
            mine_text = (
                f"{games}（样本不足）"
                if games < MIN_HERO_SAMPLE_GAMES
                else str(games)
            )
            # 与上方版本榜用同一个取名口径，免得同一行英雄在两处叫法不一致
            lines.append(
                f"{rank} | {hero_meta_label(row, heroes)} | "
                f"{row['winrate'] * 100:.1f}% | {mine_text} | "
                f"{entry['winrate']:.1f}% | {fresh_text}"
            )
        lines.append(
            "（这一节是按版本榜列的，与上面「英雄池」表的截断无关；"
            "没有出现在这一节里的版本榜英雄，他一场都没玩过）"
        )
    else:
        lines.append("（版本榜前若干名里，他一个都没玩过）")
    lines.append("")

    # ---- 近期表现 ----
    lines.append(f"=== 他最近 {len(matches)} 场（从最近往前）===")
    if matches:
        lines.append("英雄 | 结果 | K/D/A | GPM | 分路 | 时间")
        for match in matches:
            try:
                lane_text = LANE_ROLE_ZH.get(int(match.get("lane_role")), "未知")
            except (TypeError, ValueError):
                lane_text = "未知"
            lines.append(
                f"{hname(heroes, match.get('hero_id'))} | "
                f"该玩家{result_text(match)} | "
                f"{match.get('kills', 0)}/{match.get('deaths', 0)}/"
                f"{match.get('assists', 0)} | "
                f"{match.get('gold_per_min') or '-'} | {lane_text} | "
                f"{fmt_timestamp(match.get('start_time'))}"
            )
        flags = [match_result(m) for m in matches]
        win_count = sum(1 for f in flags if f is True)
        loss_count = sum(1 for f in flags if f is False)
        decided = win_count + loss_count
        unknown = len(matches) - decided
        rate = f"{win_count / decided * 100:.1f}%" if decided else "不可计算"
        lines.append(
            f"近期汇总: {len(matches)} 场 · {win_count} 胜 {loss_count} 负"
            + (f" · {unknown} 场胜负未知" if unknown else "")
            + f" · 胜率 {rate}"
        )
        gpm_values = [
            int(match.get("gold_per_min"))
            for match in matches
            if match.get("gold_per_min")
        ]
        if gpm_values:
            lines.append(f"场均 GPM: {sum(gpm_values) / len(gpm_values):.0f}")
        lines.append(f"分路分布: {_lane_role_text(matches)}")
    else:
        lines.append("（没有查到近期对局）")
    lines.append("")

    lines.append("=== 输出要求 ===")
    lines.append(HERO_PICK_FORMAT)
    return "\n".join(lines)


def build_single_match_analysis_prompt(
    match: dict,
    heroes: dict[int, dict],
    items: dict[str, dict],
    focus_account_ids: int | list[int] | tuple[int, ...] | None = None,
    focus_names: dict[int, str] | None = None,
    extra_context: str = "",
    abilities: dict[int, str] | None = None,
    curve_ids: list[int] | None = None,
) -> str:
    """构造「单场比赛深度复盘」的提示词。

    整体分成四段，顺序不能调换：**数据块** → **比赛质量客观指标** →
    **口径说明**（:data:`DATA_CAVEATS` ＋ 模式提醒）→ **输出要求**。
    口径说明必须夹在数据与要求之间：它是数据的注解，放到最后会被模型当成
    收尾的客套话略过。

    Args:
        focus_account_ids: 需要重点点评的玩家。支持一位或多位——同一局里可能
            有多位被监听的玩家参战，此时仍只生成**一份**报告，报告里对每位
            焦点玩家分别深入点评。
        focus_names: ``{account_id: 昵称}``，用于让提示词里出现可读的名字。
        extra_context: 附加上下文（例如焦点玩家近期的整体状态）。
        abilities: 技能常量映射 ``{技能ID: 技能名}``，用于把解析产物里的
            ``ability_upgrades_arr`` 翻译成可读的加点顺序；拿不到就不输出。
        curve_ids: 额外需要逐分钟发育曲线的玩家（无焦点时用来看核心的节奏）。
    """
    ids = normalize_focus_ids(focus_account_ids)
    names = focus_names or {}

    lines: list[str] = []
    lines.append("请对下面这场 Dota 2 比赛做一次深度复盘。")
    if ids:
        labels = [f"{names.get(aid) or aid}（account_id={aid}）" for aid in ids]
        if len(labels) == 1:
            lines.append(
                f"需要重点点评的选手是 {labels[0]}，数据中已标记为「焦点玩家」。"
            )
        else:
            lines.append(
                f"需要重点点评的选手有 {len(labels)} 位，数据中均已标记为「焦点玩家」："
                + "、".join(labels)
                + "。请在「焦点玩家点评」一节里为他们逐一单独点评。"
            )
    lines.append("")

    lines.append(
        build_match_data_text(
            match,
            heroes,
            items,
            ids,
            abilities=abilities,
            curve_ids=curve_ids,
        )
    )

    lines.append("")
    lines.append("=== 比赛质量客观指标 ===")
    lines.append(match_quality_block(match))

    # 口径说明必须单独成段、且排在输出要求之前：它是数据的一部分。
    # 放在最后会被模型的「先看结构再填内容」习惯吃掉。
    lines.append("")
    lines.append(DATA_CAVEATS)
    caveat = mode_caveat(match)
    if caveat:
        lines.append(caveat)

    if extra_context:
        lines.append("")
        lines.append("=== 附加上下文 ===")
        lines.append(extra_context)

    lines.append("")
    lines.append("=== 输出要求 ===")
    lines.append(MATCH_REPORT_FORMAT)
    return "\n".join(lines)


async def resolve_provider(context: Any, umo: str, provider_id: str = "") -> Any:
    """获取用于分析的模型提供商实例。

    Args:
        context: AstrBot 的 ``Context``。
        umo: unified_msg_origin，用于解析当前会话使用的模型。
        provider_id: 指定提供商 ID，留空则使用会话默认提供商。

    Returns:
        Provider 实例；拿不到时返回 None。
    """
    provider_id = (provider_id or "").strip()
    if provider_id:
        try:
            provider = context.get_provider_by_id(provider_id)
            if provider:
                return provider
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dota2] 指定的模型提供商 {provider_id} 不可用: {e}")

    try:
        return await context.get_using_provider_async(umo=umo)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[dota2] 获取默认模型提供商失败: {e}")
        return None


async def call_llm(provider: Any, system_prompt: str, user_prompt: str) -> str:
    """调用大模型并返回纯文本结果。

    Raises:
        RuntimeError: 提供商为空或调用失败。
    """
    if provider is None:
        raise RuntimeError("没有可用的模型提供商")

    # 不同 AstrBot 版本 text_chat 的参数略有差异，这里做兼容降级
    try:
        response = await provider.text_chat(
            prompt=user_prompt, system_prompt=system_prompt
        )
    except TypeError:
        response = await provider.text_chat(prompt=user_prompt)

    text = getattr(response, "completion_text", "") or ""
    text = text.strip()
    if not text:
        raise RuntimeError("模型返回了空结果")
    return text


# ======================================================================
# 闲聊通道：把 AstrBot 的模型提供商接上工具
# ======================================================================
# 背景：插件的模型分工是「闲聊走默认模型、比赛分析走专用 Key」。
# 早期版本里 AstrBot 的 provider 只能纯文本进出，所以带工具的闲聊只能
# 绑在自建通道上；从 AstrBot 4.x 起 ``text_chat`` 支持 ``func_tool``，
# 于是闲聊可以回到默认模型，而工具调用能力不必牺牲。
#
# 下面这层适配把 provider 包装成与 ``OpenAICompatibleClient`` **同形**的
# 通道（都提供 ``chat_with_tools`` / ``chat``，都返回 ``ToolReply``），
# 这样 main 侧的多轮循环对两条通道一视同仁，不必写两套。


def build_provider_tool_set(tools: list[dict]) -> Any:
    """把 OpenAI 格式的工具清单转成 AstrBot 的 ``ToolSet``。

    用**延迟导入**是有意的：插件要能在离线测试（没有 AstrBot 内部包）里被
    导入，也要装到旧版 AstrBot（没有 ``astrbot.api.ToolSet``）上时不至于
    直接崩 —— 那种情况下这里抛 ``LLMRequestError``，上层据此判定
    「这条通道用不了」，改走专用 Key。

    Raises:
        LLMRequestError: 拿不到 ``ToolSet`` / ``FunctionTool``，或某个工具的
            schema 不被 AstrBot 接受（它内部会用 jsonschema 校验）。
    """
    try:
        from astrbot.api import FunctionTool, ToolSet  # 延迟导入，见上
    except Exception as e:  # noqa: BLE001
        raise LLMRequestError(f"当前 AstrBot 未暴露 ToolSet，无法在默认模型上调用工具（{e}）") from e

    tool_set = ToolSet()
    for item in tools or []:
        function = item.get("function") if isinstance(item, dict) else None
        function = function if isinstance(function, dict) else {}
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        parameters = function.get("parameters")
        if not isinstance(parameters, dict) or not parameters:
            parameters = {"type": "object", "properties": {}}
        try:
            tool_set.add_tool(
                FunctionTool(
                    name=name,
                    description=str(function.get("description") or ""),
                    parameters=parameters,
                )
            )
        except Exception as e:  # noqa: BLE001
            raise LLMRequestError(f"工具 {name} 的 schema 不被 AstrBot 接受：{e}") from e
    return tool_set


def split_system_prompt(messages: list[dict]) -> tuple[str, list[dict]]:
    """把 ``messages`` 里的 system 消息摘出来，其余原样返回。

    AstrBot 的 ``text_chat`` 用的是 ``system_prompt`` 参数（内部插到上下文
    最前面），**不认 contexts 里的 system 角色** —— 混着传会被静默丢掉，
    提示词就白写了。
    """
    system_parts: list[str] = []
    rest: list[dict] = []
    for message in messages or []:
        if isinstance(message, dict) and message.get("role") == "system":
            text = str(message.get("content") or "").strip()
            if text:
                system_parts.append(text)
            continue
        rest.append(message)
    return "\n\n".join(system_parts), rest


def parse_provider_reply(response: Any) -> ToolReply:
    """把 AstrBot 的 ``LLMResponse`` 归一成插件自己的 ``ToolReply``。

    ``tools_call_args`` 给的是 dict（自建通道给的是 JSON 字符串），这里统一
    转成字符串：回填给模型时要**一字不改**，字典再序列化一次反而更安全。
    """
    content = str(getattr(response, "completion_text", "") or "").strip()
    names = list(getattr(response, "tools_call_name", None) or [])
    args = list(getattr(response, "tools_call_args", None) or [])
    ids = list(getattr(response, "tools_call_ids", None) or [])
    calls: list[ToolCall] = []
    for index, name in enumerate(names):
        name = str(name or "").strip()
        if not name:
            continue
        raw = args[index] if index < len(args) else {}
        if not isinstance(raw, str):
            raw = json.dumps(raw if raw is not None else {}, ensure_ascii=False)
        call_id = str(ids[index]) if index < len(ids) and ids[index] else f"call_{index}"
        calls.append(ToolCall(id=call_id, name=name, arguments_raw=raw))
    return ToolReply(content=content, tool_calls=calls)


class ProviderToolClient:
    """把 AstrBot 的模型提供商包装成带工具通道（与自建通道同形）。

    与 :class:`OpenAICompatibleClient` 的三点差异，都是这条通道的性质决定的：

    * system 提示词走 ``system_prompt`` 参数，不能留在 contexts 里
      （见 :func:`split_system_prompt`）；
    * ``temperature`` / ``max_tokens`` 由 AstrBot 的提供商配置决定，这里传了
      也不生效 —— 保留参数只为对上调用签名；
    * 工具 schema 要走 AstrBot 的 ``ToolSet``（含 jsonschema 校验）。
    """

    def __init__(self, provider: Any, *, label: str = "默认模型") -> None:
        self.provider = provider
        self.label = label
        #: 按工具名集合缓存 ToolSet：`_run_tool_loop` 每轮都会重建 messages，
        #: 但工具清单在一轮问答内是固定的，没必要每轮重新校验 schema。
        self._tool_sets: dict[tuple[str, ...], Any] = {}

    @property
    def endpoint(self) -> str:
        """与自建通道的 ``endpoint`` 对齐，日志 / 自检里可以直接打印。"""
        return self.label

    def _tool_set(self, tools: list[dict]) -> Any:
        key = tuple(
            sorted(
                str((item.get("function") or {}).get("name") or "")
                for item in tools or []
                if isinstance(item, dict)
            )
        )
        if key not in self._tool_sets:
            self._tool_sets[key] = build_provider_tool_set(tools)
        return self._tool_sets[key]

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: Any = "auto",
    ) -> ToolReply:
        """带工具的一轮对话。

        Raises:
            LLMRequestError: 通道不可用（不支持 func_tool、鉴权 / 网络失败等）。
        """
        system_prompt, contexts = split_system_prompt(messages)
        kwargs: dict[str, Any] = {
            "contexts": contexts,
            "func_tool": self._tool_set(tools) if tools else None,
            "tool_choice": tool_choice or "auto",
        }
        if system_prompt:
            kwargs["system_prompt"] = system_prompt
        try:
            response = await self.provider.text_chat(**kwargs)
        except LLMRequestError:
            raise
        except TypeError as e:
            # 旧版 AstrBot 的 text_chat 不接受 func_tool，硬塞会 TypeError
            raise LLMRequestError(f"当前 AstrBot 的模型提供商不支持工具调用：{e}") from e
        except Exception as e:  # noqa: BLE001
            raise LLMRequestError(f"{self.label}调用失败：{e}") from e
        return parse_provider_reply(response)

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """不带工具的普通一轮（闲聊的单轮兜底走它）。"""
        try:
            response = await self.provider.text_chat(
                prompt=user_prompt, system_prompt=system_prompt
            )
        except TypeError:
            response = await self.provider.text_chat(prompt=user_prompt)
        except Exception as e:  # noqa: BLE001
            raise LLMRequestError(f"{self.label}调用失败：{e}") from e
        text = str(getattr(response, "completion_text", "") or "").strip()
        if not text:
            raise LLMRequestError(f"{self.label}返回了空结果")
        return text


# ======================================================================
# 插件自带的 OpenAI 兼容通道
# ======================================================================
#: 常见服务商的默认接口地址，方便用户在配置里只填 key
PROVIDER_PRESETS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "siliconflow": "https://api.siliconflow.cn/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": "http://127.0.0.1:11434/v1",
}

#: 归一化 provider 名 → 预设 key 的别名表。
#: 用户可能写成「DeepSeek」「deep_seek」「深度求索」等，这里都收敛到 canonical 名。
PRESET_ALIASES: dict[str, str] = {
    "openai": "openai",
    "azure": "openai",
    "deepseek": "deepseek",
    "deep_seek": "deepseek",
    "深度求索": "deepseek",
    "moonshot": "moonshot",
    "kimi": "moonshot",
    "月之暗面": "moonshot",
    "dashscope": "dashscope",
    "qwen": "dashscope",
    "通义": "dashscope",
    "阿里": "dashscope",
    "zhipu": "zhipu",
    "glm": "zhipu",
    "智谱": "zhipu",
    "siliconflow": "siliconflow",
    "硅基流动": "siliconflow",
    "openrouter": "openrouter",
    "ollama": "ollama",
}


class LLMRequestError(RuntimeError):
    """自建通道调用失败（网络、鉴权、限流、返回体异常等）。"""


def normalize_base_url(raw: str, provider_hint: str = "") -> str:
    """把用户填的地址或服务商名归一成可用的 base_url。

    允许三种写法：
    * 完整地址（``https://api.deepseek.com/v1``）→ 原样去尾斜杠；
    * 只写服务商名（``deepseek`` / ``kimi``）→ 查 :data:`PROVIDER_PRESETS`；
    * 留空 → 结合 ``provider_hint`` 再查一次，仍无则回退 OpenAI 官方。

    末尾的 ``/chat/completions`` 会被剥掉，避免用户误把完整端点填进来。
    """
    value = (raw or "").strip()
    if value and "://" not in value:
        key = PRESET_ALIASES.get(value.lower().replace("-", "_").replace(" ", ""))
        key = key or PRESET_ALIASES.get((provider_hint or "").strip().lower())
        if key:
            return PROVIDER_PRESETS[key]

    if not value:
        key = PRESET_ALIASES.get((provider_hint or "").strip().lower())
        return PROVIDER_PRESETS.get(key, PROVIDER_PRESETS["openai"])

    value = value.rstrip("/")
    for suffix in ("/chat/completions", "/completions"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    return value.rstrip("/")


class OpenAICompatibleClient:
    """极简的 OpenAI 兼容 ``/chat/completions`` 客户端。

    只依赖标准库：插件因此不必新增 ``openai`` / ``aiohttp`` 之类的依赖，
    也不会和 AstrBot 自带的 HTTP 栈产生版本冲突。请求放在线程里跑，
    不阻塞事件循环。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "",
        model: str = "",
        *,
        timeout: float = 120.0,
        provider_hint: str = "",
        proxy: str = "",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = normalize_base_url(base_url, provider_hint)
        self.model = (model or "").strip()
        self.timeout = max(5.0, float(timeout))
        self.proxy = (proxy or "").strip()
        self.extra_headers = dict(extra_headers or {})

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def build_payload(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        """组装请求体。"""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt or ""},
                {"role": "user", "content": user_prompt or ""},
            ],
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = float(temperature)
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        return payload

    def _post_sync(self, payload: dict) -> dict:
        """同步发一次请求并返回解析好的 JSON 对象。

        由 :meth:`chat` / :meth:`chat_with_tools` 放进线程池执行。
        拆出这一层是因为**工具的返回体不能只取正文**：``tool_calls``
        与 ``finish_reason`` 都要读出来做下一步决策。
        """
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            # 部分中转站会校验 UA，这里给一个常规值
            "User-Agent": "astrbot-plugin-dota2/1.4",
        }
        headers.update(self.extra_headers)

        request = urllib.request.Request(
            self.endpoint, data=body, headers=headers, method="POST"
        )
        # 按需挂代理：走 handler 而不是全局 ``urlopen``，避免影响其他插件的请求
        opener = None
        if self.proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler(
                    {"http": self.proxy, "https": self.proxy}
                )
            )
        send = opener.open if opener is not None else urllib.request.urlopen
        try:
            with send(request, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:400]
            except Exception:  # noqa: BLE001
                detail = ""
            raise LLMRequestError(
                f"HTTP {e.code} {e.reason}"
                + (f"：{detail}" if detail else "")
            ) from e
        except urllib.error.URLError as e:
            raise LLMRequestError(f"无法连接 {self.endpoint}：{e.reason}") from e
        except asyncio.TimeoutError as e:  # pragma: no cover - 由线程内抛出
            raise LLMRequestError(f"请求超时（{self.timeout:g}s）") from e

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise LLMRequestError(f"返回体不是合法 JSON：{raw[:200]}") from e

        return data

    def _request_sync(self, payload: dict) -> str:
        """同步发一次请求并取出正文（由 :meth:`chat` 放进线程池执行）。"""
        return self.parse_response(self._post_sync(payload))

    @staticmethod
    def parse_message(data: dict) -> dict:
        """从返回体里取出 ``choices[0].message``（不做任何字段裁剪）。"""
        # 有些服务在 200 里塞 error
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise LLMRequestError(f"接口返回错误：{msg}")

        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            raise LLMRequestError(f"返回体里没有 choices：{str(data)[:200]}")

        first = choices[0] or {}
        message = first.get("message")
        if not isinstance(message, dict):
            # 少数实现把结果放在 delta / text 里
            message = first.get("delta") if isinstance(first.get("delta"), dict) else {}
            if not message and first.get("text"):
                message = {"role": "assistant", "content": first.get("text")}
        if not isinstance(message, dict):
            raise LLMRequestError(f"返回体里没有 message：{str(first)[:200]}")
        return message

    @staticmethod
    def parse_response(data: dict) -> str:
        """从返回体里取出正文，兼容几种常见的字段布局。"""
        message = OpenAICompatibleClient.parse_message(data)
        text = message.get("content")
        if text is None:
            # 少数实现把结果放在顶层 text 里（delta 已在 parse_message 里兜过）
            text = message.get("text")
        if isinstance(text, list):
            # 多模态返回：拼接其中的文本片段
            text = "".join(
                part.get("text", "")
                for part in text
                if isinstance(part, dict)
            )
        text = (text or "").strip()
        if not text:
            raise LLMRequestError("模型返回了空内容")
        return text

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """调用模型并返回正文。

        Raises:
            LLMRequestError: 缺少 key/model、网络异常或返回体不可解析。
        """
        if not self.api_key:
            raise LLMRequestError("未配置 API Key")
        if not self.model:
            raise LLMRequestError("未配置模型名称")

        payload = self.build_payload(
            system_prompt,
            user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return await asyncio.to_thread(self._request_sync, payload)

    # ------------------------------------------------------------------
    # 工具调用（Function Calling）通道
    # ------------------------------------------------------------------
    def build_tool_payload(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: Any = "auto",
    ) -> dict:
        """组装带 ``tools`` 的请求体。

        ``messages`` 是**完整的对话历史**（system / user / assistant /
        tool 四种角色），不由本方法拼装 —— 多轮工具调用的上下文必须原样
        带着走，少一条消息模型就会重复调工具或答非所问。
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [dict(m) for m in (messages or [])],
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
        if temperature is not None:
            payload["temperature"] = float(temperature)
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        return payload

    @staticmethod
    def parse_tool_reply(data: dict) -> "ToolReply":
        """把返回体解析成 :class:`ToolReply`（正文 + 工具调用请求）。"""
        message = OpenAICompatibleClient.parse_message(data)
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        calls: list[ToolCall] = []
        for item in message.get("tool_calls") or []:
            if not isinstance(item, dict):
                continue
            function = item.get("function")
            if not isinstance(function, dict):
                function = {}
            name = str(function.get("name") or "").strip()
            if not name:
                continue
            raw = function.get("arguments")
            if not isinstance(raw, str):
                # 少数实现直接给对象，统一转成字符串，回填时必须原样
                raw = json.dumps(raw if raw is not None else {}, ensure_ascii=False)
            calls.append(
                ToolCall(
                    id=str(item.get("id") or "").strip(),
                    name=name,
                    arguments_raw=raw,
                )
            )
        choices = data.get("choices") if isinstance(data, dict) else None
        finish = ""
        if choices and isinstance(choices[0], dict):
            finish = str(choices[0].get("finish_reason") or "")
        return ToolReply(
            content=(content or "").strip(),
            tool_calls=calls,
            raw_message=message,
            finish_reason=finish,
        )

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: Any = "auto",
    ) -> "ToolReply":
        """带工具的一轮对话。

        与 :meth:`chat` 的区别是**返回整个 message**：调用方要看
        ``tool_calls`` 决定接下来执行哪些工具，执行完再把结果作为
        ``role=tool`` 的消息追加进 ``messages`` 继续下一轮。

        Raises:
            LLMRequestError: 缺少 key/model、网络异常或返回体不可解析。
        """
        if not self.api_key:
            raise LLMRequestError("未配置 API Key")
        if not self.model:
            raise LLMRequestError("未配置模型名称")

        payload = self.build_tool_payload(
            messages,
            tools,
            temperature=temperature,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
        )
        data = await asyncio.to_thread(self._post_sync, payload)
        return self.parse_tool_reply(data)


@dataclass
class ToolCall:
    """模型要求调用的一次工具。"""

    id: str
    name: str
    #: 模型给的参数，**原样保留的 JSON 字符串**（回填给模型时必须一字不改）
    arguments_raw: str = ""

    @property
    def arguments(self) -> dict:
        """把参数字符串解析成字典；不是合法 JSON 时返回空字典。"""
        raw = (self.arguments_raw or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def to_payload(self) -> dict:
        """转成回填 ``messages`` 用的 assistant 侧结构。"""
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.arguments_raw or "{}",
            },
        }


@dataclass
class ToolReply:
    """一轮带工具的模型回复。"""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_message: dict = field(default_factory=dict)
    finish_reason: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def assistant_message(self) -> dict:
        """回填进 ``messages`` 的 assistant 消息。

        只保留 ``role`` / ``content`` / ``tool_calls`` 三个字段：部分服务
        （DeepSeek 的 ``reasoning_content`` 是典型）明确要求**不要把推理
        内容带回下一轮**，原样回填会被拒或产生莫名其妙的效果。
        """
        message: dict[str, Any] = {
            "role": "assistant",
            "content": self.content or "",
        }
        if self.tool_calls:
            message["tool_calls"] = [call.to_payload() for call in self.tool_calls]
        return message
