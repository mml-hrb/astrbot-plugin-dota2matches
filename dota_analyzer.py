"""调用大模型生成 Dota 2 分析报告。

职责：
1. 把 OpenDota 的数据拼装成结构化的提示词（Prompt）；
2. 通过 AstrBot 的 Provider 接口拿到模型回复。

本模块不直接依赖具体平台，只依赖 AstrBot 的 ``Context``。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

try:  # 插件目录被作为包加载时的相对导入
    from .dota_format import (
        build_match_data_text,
        fmt_duration,
        fmt_timestamp,
        format_summary_block,
        hname,
        match_quality_block,
        mode_text,
        normalize_focus_ids,
        player_win,
        summarize_hero_history,
        summarize_matches,
        to_steam_id64,
    )
except ImportError:  # 兜底：以普通模块方式加载时，把插件目录加入 sys.path
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dota_format import (  # type: ignore[no-redef]
        build_match_data_text,
        fmt_duration,
        fmt_timestamp,
        format_summary_block,
        hname,
        match_quality_block,
        mode_text,
        normalize_focus_ids,
        player_win,
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
MATCH_REPORT_FORMAT = """请严格按照以下结构输出（使用 Markdown 二级/三级标题）：

## 比赛走势复盘
（按时间顺序梳理这场比赛。要指出：对线期谁占优、第一个重大转折发生在第几分钟、双方经济曲线在哪些时间点发生交叉、肉山与防御塔的争夺节奏。引用具体时间点和经济差数值）

## 比赛质量评估
（评估这是一场高质量对局还是崩盘局/碾压局：双方失误多不多、翻盘幅度有多大、团战是否胶着、经济曲线是否反复。给出你的判断依据）

## 焦点玩家点评
（对数据中标记为「焦点玩家」的**每一位**选手分别详细点评，每人单独起一个三级小标题：对线表现、发育效率、团战作用、关键决策是否合理、本场的最大问题。引用他的具体数据。如果只有一位焦点玩家，就只点评这一位）

## 其余选手点评
（逐一点评除焦点玩家之外的其余选手，每人 2-3 句，说明他在这场比赛里扮演的角色和发挥水平。用「英雄名(选手标识)」开头）

## 关键转折点
（列出本场最重要的 1-3 个转折点，说明当时发生了什么、对后续局势造成了什么影响）"""


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
        lines.append(
            "生涯英雄池（按场次倒序，前 10）: "
            + "、".join(
                f"{hname(heroes, row['hero_id'])} {row['games']}场/{row['winrate']:.0f}%"
                for row in rows[:10]
            )
        )

    lines.append("")
    lines.append(f"=== 最近 {len(matches)} 场对局逐场明细（从最近往前）===")
    lines.append(
        "序号 | 比赛ID | 时间 | 英雄 | 结果 | K/D/A | GPM | XPM | 补刀 | 英雄伤害 | 时长 | 模式"
    )
    for index, match in enumerate(matches, start=1):
        lines.append(
            f"{index} | {match.get('match_id')} | {fmt_timestamp(match.get('start_time'))} | "
            f"{hname(heroes, match.get('hero_id'))} | "
            f"{'胜' if player_win(match) else '负'} | "
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


def build_single_match_analysis_prompt(
    match: dict,
    heroes: dict[int, dict],
    items: dict[str, dict],
    focus_account_ids: int | list[int] | tuple[int, ...] | None = None,
    focus_names: dict[int, str] | None = None,
    extra_context: str = "",
) -> str:
    """构造「单场比赛深度复盘」的提示词。

    Args:
        focus_account_ids: 需要重点点评的玩家。支持一位或多位——同一局里可能
            有多位被监听的玩家参战，此时仍只生成**一份**报告，报告里对每位
            焦点玩家分别深入点评。
        focus_names: ``{account_id: 昵称}``，用于让提示词里出现可读的名字。
        extra_context: 附加上下文（例如焦点玩家近期的整体状态）。
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

    lines.append(build_match_data_text(match, heroes, items, ids))

    lines.append("")
    lines.append("=== 比赛质量客观指标 ===")
    lines.append(match_quality_block(match))

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
