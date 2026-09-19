"""兜底对话的工具层：把插件已有的查询能力暴露给模型**按需调用**。

为什么要这一层
--------------
「dota2助手 钢板最近打得怎么样？顺便给他推荐几个轮椅英雄」这类问题
**一次要用到多个功能**：既要个人战绩，又要版本榜的个人适配。意图分类
一次只能给一个 label，无论判成哪个都注定少答一半。做法是把每个查询包成
一个工具，让模型自己决定调几次、调哪些，结果回填后再作答 —— 也就是
标准的 function calling 循环。

边界（有意为之，不是漏做）
--------------------------
* **只暴露只读能力**。绑定 / 解绑 / 添加监听这类会改数据的动作不进工具箱：
  模型判断失误的代价不对称（会真的往监听列表里塞人），这些操作仍然只走
  「自然语言 → 确认 → 执行」的既有链路。
* **单场复盘不进工具箱**。它可能触发催解析并等上十分钟，属于后台任务；
  塞进一次问答里会把整条回答卡死。用户明确说「复盘这一局」时，
  意图分类会把它交给对应的指令 handler。
* **每个工具的输出都截断**（``MAX_OUTPUT_CHARS``）。模型不需要全文，
  上下文里塞两万字只会稀释重点、还把成本抬上去。

本模块不依赖 AstrBot，也不直接依赖插件主体：所有外部能力都通过
:class:`ToolContext` 注入，因此可以脱离框架单独测试。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from astrbot.api import logger

try:  # 插件目录被作为包加载时的相对导入
    from .dota_format import (
        format_hero_meta_board,
        format_hero_stats,
        format_match_list,
        format_player_profile,
        format_summary_block,
        hero_meta_note,
        hero_meta_rows,
        match_position,
        pick_heroes_for_player,
        summarize_matches,
    )
except ImportError:  # 兜底：以普通模块方式加载时，把插件目录加入 sys.path
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dota_format import (  # type: ignore[no-redef]
        format_hero_meta_board,
        format_hero_stats,
        format_match_list,
        format_player_profile,
        format_summary_block,
        hero_meta_note,
        hero_meta_rows,
        match_position,
        pick_heroes_for_player,
        summarize_matches,
    )

#: 单个工具返回给模型的文本上限。超出的部分截掉并标注 —— 只留最前面
#: 最有信息量的部分，比塞满上下文更有用。
MAX_OUTPUT_CHARS = 2000

#: 单个工具的执行超时（秒）。工具内部可能串行调多个数据源接口
#: （英雄表 + 玩家资料 + 英雄池），给得比单接口宽一些。
DEFAULT_TOOL_TIMEOUT = 30.0

#: 战绩类工具一次最多取多少场
MAX_MATCHES_PER_CALL = 50

#: 同一个工具**连续失败**多少次之后就不再真的执行、直接短路。
#:
#: 起因是一次真实故障（2026-09-18，OpenDota 大面积 521/500）：模型为了回答
#: 「昨天群里谁输得最惨」逐个玩家调 `query_matches`，6 个人各超时 30 秒，
#: 合计沉默 194 秒，还把调用预算烧光，导致真正想要的 `recommend_heroes`
#: 一次都没轮到。数据源整体挂掉时，重试同一个工具不会有任何新结果 ——
#: 必须让它**快速失败并说清楚**，把剩下的预算留给别的工具。
#:
#: 取 2 而不是 1：单次失败可能只是这一个账号 / 这一次网络抖动，
#: 立刻禁用会让一次偶发失败白白废掉一个工具。
MAX_TOOL_FAILURES = 2


@dataclass
class ToolContext:
    """工具执行需要的一切外部能力（由插件主体注入）。"""

    #: 数据源门面（OpenDota / STRATZ 的 FallbackDataSource）
    api: Any
    #: ``cfg(key, default)``：读插件配置
    cfg: Callable[[str, Any], Any]
    #: ``await heroes()``：英雄常量表（含中文名），带缓存
    heroes: Callable[[], Awaitable[dict]]
    #: ``await resolve_player(text)``：把昵称 / 「我」/ 账号 ID 解析成
    #: ``(account_id, 显示名)``；解析不了抛异常
    resolve_player: Callable[[str], Awaitable[tuple[int, str]]]
    #: 本会话可查的玩家列表（本地数据，不联网）：
    #: ``[{"name", "account_id", "relation"}]``
    session_players: Callable[[], list[dict]]
    #: ``await fetch_matches(account_id, limit)``：近期对局，**带缓存**
    #: （与闲聊兜底共用同一份缓存，避免同一个账号被拉两遍）
    fetch_matches: Callable[[int, int], Awaitable[list[dict]]]
    #: 当前时间戳
    now: float = 0.0
    #: 单工具超时
    timeout: float = DEFAULT_TOOL_TIMEOUT
    #: 工具调用日志（每次调用追加一条，供排障取证）
    trace: list[str] = field(default_factory=list)
    #: 各工具**连续**失败次数（成功即清零），用于 :data:`MAX_TOOL_FAILURES`
    #: 的短路判断。放在 context 上而不是模块全局：一次提问一个实例，
    #: 上一轮问答的失败不该影响下一轮。
    failures: dict[str, int] = field(default_factory=dict)


# ======================================================================
# 工具定义（OpenAI function calling schema）
# ======================================================================


def _spec(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


_PLAYER_PROP = {
    "type": "string",
    "description": (
        "要查的玩家：昵称（本会话绑定或监听过的人最准）、32 位账号 ID，"
        "或『我』表示提问者本人。留空等同于『我』。"
    ),
}

_POSITION_PROP = {
    "type": "string",
    "description": "位置筛选：核心 / 中单 / 三号位 / 辅助 / 一号位 / 四号位 等，留空表示不筛。",
}


def build_tool_specs() -> list[dict]:
    """返回工具清单（每次新拷贝，调用方随意改动不会互相影响）。"""
    return [
        _spec(
            "list_players",
            "列出本会话里已绑定或正在监听的玩家（含账号 ID）。"
            "当用户说的是「群里的人」「监听的几个人」而你没把握是谁时，先调这个。",
            {},
            [],
        ),
        _spec(
            "query_matches",
            "查询某位玩家最近的 Dota2 对局：逐场明细（时间 / 英雄 / 胜负 / KDA / "
            "位置 / 模式）加汇总统计（胜率、场均 KDA、GPM、位置分布、组队情况）。"
            "问「他最近打得怎么样」「谁最菜」时用。只查一个人，多个人要分别调用。",
            {
                "player": _PLAYER_PROP,
                "count": {
                    "type": "integer",
                    "description": "查最近几场，默认 10，最多 50。",
                },
            },
            [],
        ),
        _spec(
            "query_hero_pool",
            "查询某位玩家的英雄池统计：每个英雄的场次、胜率、最近使用时间。"
            "问「他会玩什么」「绝活是什么」「英雄池深不深」时用。",
            {"player": _PLAYER_PROP},
            [],
        ),
        _spec(
            "query_profile",
            "查询某位玩家的账号资料：段位、生涯总场次与胜率、常用英雄。"
            "问「他什么段位」「打了多少场」时用。",
            {"player": _PLAYER_PROP},
            [],
        ),
        _spec(
            "query_meta",
            "查询**当前版本**的强势英雄榜单（最近 7 天全分段公开对局）："
            "胜率最高的英雄、热门里胜率最突出的、以及胜率垫底的。"
            "问「这版本什么英雄强」「轮椅有哪些」「版本答案」时用。",
            {"position": _POSITION_PROP},
            [],
        ),
        _spec(
            "recommend_heroes",
            "在**当前版本强势英雄**里，结合某位玩家的英雄池与近期表现，挑出适合他"
            "上手/上分的英雄（纯数据交叉，给出名次、他会玩的证据与理由）。"
            "问「给他推荐几个英雄上分」「他这版本该玩什么」时用。",
            {"player": _PLAYER_PROP, "position": _POSITION_PROP},
            [],
        ),
    ]


#: 工具名 → 是否需要「玩家解析」（不需要的工具不会因为没绑定而报错）
TOOL_NAMES: tuple[str, ...] = (
    "list_players",
    "query_matches",
    "query_hero_pool",
    "query_profile",
    "query_meta",
    "recommend_heroes",
)


# ======================================================================
# 执行
# ======================================================================


def _clamp_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(number, high))


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n…（内容过长，已截断）"


def parse_arguments(raw: Any) -> dict:
    """把模型给的参数解析成字典（容错：非法 JSON 一律当空参数）。"""
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _person(args: dict, ctx: ToolContext) -> tuple[int, str]:
    """解析工具参数里的「查谁」，返回 ``(account_id, 显示名)``。"""
    raw = str(args.get("player") or "").strip()
    return await ctx.resolve_player(raw)


async def _guarded(coro: Awaitable[Any], ctx: ToolContext) -> Any:
    """给工具内部的数据源调用加超时。

    下限只取 0.1 秒：超时值由配置决定，配置成 0 / 负数时回落到
    :data:`DEFAULT_TOOL_TIMEOUT`。**不要**在这里写死一个较大的下限
    （例如 5 秒）—— 那会让「把超时调小」这个配置项彻底失效，
    测试也没法验证超时分支。
    """
    timeout = float(ctx.timeout or 0) or DEFAULT_TOOL_TIMEOUT
    return await asyncio.wait_for(coro, timeout=max(0.1, timeout))


# ---------------------------------------------------------------- 各工具实现


async def _tool_list_players(args: dict, ctx: ToolContext) -> str:
    players = ctx.session_players() or []
    if not players:
        return "本会话没有绑定或监听的玩家，无法按名字查人。可以让用户先绑定账号。"
    lines = [f"本会话可查的玩家共 {len(players)} 位（绑定 / 监听）："]
    for row in players:
        name = str(row.get("name") or "未命名")
        relation = str(row.get("relation") or "")
        account_id = row.get("account_id")
        lines.append(f"· {name}（account_id {account_id}，{relation}）")
    return "\n".join(lines)


async def _tool_query_matches(args: dict, ctx: ToolContext) -> str:
    account_id, name = await _person(args, ctx)
    count = _clamp_int(args.get("count"), default=10, low=1, high=MAX_MATCHES_PER_CALL)
    matches = await _guarded(ctx.fetch_matches(account_id, count), ctx)
    if not matches:
        return (
            f"{name} 最近没有可用的对局记录"
            "（可能未公开比赛数据，或这段时间没打）。"
        )
    heroes = await _guarded(ctx.heroes(), ctx)
    summary = summarize_matches(matches)
    blocks = [
        f"=== {name} 最近 {len(matches)} 场 ===",
        format_summary_block(summary, heroes),
        "",
        format_match_list(name, account_id, matches, heroes, title="逐场明细"),
    ]
    return "\n".join(blocks)


async def _tool_query_hero_pool(args: dict, ctx: ToolContext) -> str:
    account_id, name = await _person(args, ctx)
    rows = await _guarded(ctx.api.get_player_heroes(account_id), ctx)
    if not rows:
        return f"没有取到 {name} 的英雄池数据（可能该账号未公开比赛数据）。"
    heroes = await _guarded(ctx.heroes(), ctx)
    return format_hero_stats(name, rows, heroes, top=12)


async def _tool_query_profile(args: dict, ctx: ToolContext) -> str:
    account_id, name = await _person(args, ctx)
    profile = await _guarded(ctx.api.get_player(account_id), ctx)
    if not profile:
        return f"数据源查不到账号 {account_id}（{name}）。"
    wl = await _guarded(ctx.api.get_player_wl(account_id), ctx)
    rows = await _guarded(ctx.api.get_player_heroes(account_id), ctx)
    heroes = await _guarded(ctx.heroes(), ctx)
    return format_player_profile(profile, wl or {}, rows or [], heroes)


async def _meta_board(ctx: ToolContext, position: str = "") -> tuple[list[dict], dict, dict]:
    """拉版本英雄榜的行与口径信息（供两个工具共用）。"""
    hero_stats = await _guarded(ctx.api.get_hero_stats(), ctx)
    heroes = await _guarded(ctx.heroes(), ctx)
    patch = await _guarded(ctx.api.get_latest_patch(), ctx)
    rows, meta = hero_meta_rows(
        hero_stats or [],
        position=position,
        min_pick=int(ctx.cfg("hero_meta_min_pick", 0) or 0),
    )
    return rows, meta, {"heroes": heroes, "patch": patch}


def _board_sizes(ctx: ToolContext) -> dict[str, int]:
    return {
        "top": max(1, int(ctx.cfg("hero_meta_board_size", 10) or 10)),
        "hot_top": max(0, int(ctx.cfg("hero_meta_hot_size", 5) or 0)),
        "cold_top": max(0, int(ctx.cfg("hero_meta_cold_size", 3) or 0)),
    }


async def _tool_query_meta(args: dict, ctx: ToolContext) -> str:
    position = match_position(str(args.get("position") or ""))
    rows, meta, extra = await _meta_board(ctx, position)
    if not rows:
        return "没有拿到版本英雄数据（数据源可能暂时不可用），稍后再试。"
    sizes = _board_sizes(ctx)
    text = format_hero_meta_board(
        rows, meta, extra["heroes"], patch=extra["patch"], **sizes
    )
    note = hero_meta_note(meta, extra["patch"])
    return text + ("\n\n" + note if note else "")


async def _tool_recommend_heroes(args: dict, ctx: ToolContext) -> str:
    position = match_position(str(args.get("position") or ""))
    account_id, name = await _person(args, ctx)
    rows, meta, extra = await _meta_board(ctx, position)
    if not rows:
        return "没有拿到版本英雄数据（数据源可能暂时不可用），稍后再试。"

    hero_rows = await _guarded(ctx.api.get_player_heroes(account_id), ctx)
    matches = await _guarded(
        ctx.fetch_matches(
            account_id,
            _clamp_int(
                ctx.cfg("default_match_count", 20),
                default=20,
                low=1,
                high=MAX_MATCHES_PER_CALL,
            ),
        ),
        ctx,
    )
    if not hero_rows and not matches:
        return f"没有查到 {name} 的英雄池与近期对局，做不了个人适配。"

    text = pick_heroes_for_player(rows, hero_rows or [], matches or [], extra["heroes"])
    scope = f"（位置筛选：{position}）" if position else ""
    if not text:
        return (
            f"{name} 的英雄池里没有版本强势英雄，近期记录也不足以判断他常打的位置，"
            f"筛不出合适的{scope}。"
        )
    header = f"=== 版本强势英雄里适合 {name} 的{scope} ==="
    return "\n".join(
        [
            header,
            hero_meta_note(meta, extra["patch"]),
            text,
        ]
    )


_DISPATCH: dict[str, Callable[[dict, ToolContext], Awaitable[str]]] = {
    "list_players": _tool_list_players,
    "query_matches": _tool_query_matches,
    "query_hero_pool": _tool_query_hero_pool,
    "query_profile": _tool_query_profile,
    "query_meta": _tool_query_meta,
    "recommend_heroes": _tool_recommend_heroes,
}


def is_disabled(name: str, ctx: ToolContext) -> bool:
    """这个工具是否已因**连续失败**被短路。

    短路的调用是「零成本」的（不发任何请求），因此调用方**不该**把它算进
    ``nlu_chat_tool_max_calls`` 配额 —— 否则数据源挂掉时，几次短路就把预算
    吃光，真正还能用的工具反而轮不上。
    """
    return ctx.failures.get(str(name or "").strip(), 0) >= MAX_TOOL_FAILURES


async def run_tool(name: str, arguments: Any, ctx: ToolContext) -> str:
    """执行一个工具。

    **永不抛异常**：任何失败都转成一段可读的说明回填给模型 ——
    工具挂掉不该让整条回答消失，模型可以基于「这块没拿到」继续作答。
    返回文本统一截断到 :data:`MAX_OUTPUT_CHARS`。

    同一个工具**连续失败**到 :data:`MAX_TOOL_FAILURES` 次后会被**短路**：
    不再真的执行，直接回一句「数据源本次不可用，别再调它了」。这一条是
    为了止血 —— 没有它，数据源整体挂掉时模型会逐个玩家重试同一个工具，
    6 个人各等 30 秒超时，用户要盯着三分钟没有任何动静。
    """
    tool = _DISPATCH.get(str(name or "").strip())
    if tool is None:
        return f"[未知工具 {name}] 插件没有这个工具，请改用清单里的工具。"

    # 短路：这个工具已经连续失败太多次，再试一次也不会有新结果。
    # 明确告诉模型「别再调了」，否则它会一直重试到轮数 / 次数用尽。
    if is_disabled(name, ctx):
        ctx.trace.append(f"{name}(短路：已连续失败 {MAX_TOOL_FAILURES} 次)")
        return (
            f"[{name} 本次不可用] 这个工具已经连续失败 {MAX_TOOL_FAILURES} 次，"
            "多半是数据源整体不可用，**不要再调用它**。"
            "请基于已经拿到的数据作答，并如实说明哪部分没取到。"
        )

    args = parse_arguments(arguments)
    started = asyncio.get_running_loop().time()
    failed = False
    try:
        text = await tool(args, ctx)
    except asyncio.TimeoutError:
        failed = True
        text = f"[{name} 查询超时] 数据源响应太慢，这块数据这次没取到。"
    except Exception as e:  # noqa: BLE001 - 工具失败不该打断回答
        failed = True
        logger.warning(f"[dota2] 兜底工具 {name} 执行失败: {e}")
        text = f"[{name} 执行失败] {type(e).__name__}: {str(e)[:120]}"
    if failed:
        ctx.failures[name] = ctx.failures.get(name, 0) + 1
    else:
        # 成功即清零：失败计数说的是「连续」，一次成功就说明工具本身没坏
        ctx.failures.pop(name, None)
    elapsed = asyncio.get_running_loop().time() - started
    out = _truncate(text)
    ctx.trace.append(f"{name}({_brief_args(args)}) {elapsed:.1f}s {len(out)}字")
    return out


def _brief_args(args: dict) -> str:
    parts = []
    for key, value in args.items():
        text = str(value or "").strip()
        if text:
            parts.append(f"{key}={text[:16]}")
    return ", ".join(parts)


# ======================================================================
# 提示词
# ======================================================================

TOOL_GUIDE = """=== 你可以调用工具查真实数据 ===
你手上有若干**只读查询工具**（见接口定义）。用法要点：

1. 需要真实数据时**直接调用工具**，不要凭上下文猜，也不要让用户自己去敲指令。
2. **一次可以调用多个工具**：问题里同时要好几样东西就分别调（例如既问某人战绩、
   又要给他推荐上分英雄，就 query_matches + recommend_heroes 各调一次；
   并行发起的多个调用会一起执行）。
3. 工具返回的就是插件的真实数据。**只讲工具给过的内容**，不要补充记忆里的
   战绩、段位、数字；工具报错或明确说没取到，就如实说这块没拿到。
4. **不要重复调用**同一个工具同一组参数；信息够了就停止调用，直接回答。
5. **多人横向对比（「谁最惨 / 谁最强 / 昨天谁打得好」）不要逐个调 `query_matches`**：
   上面上下文里的「近期战绩快照」与「横向对比」已经**按时间窗口**统计好了每个人，
   直接用它们下判断。只有当上下文里**确实没有**某个人、或明确标了「没取到」而你
   还需要他时，才为那一个人单独查一次。逐个查六个人会让用户干等好几分钟，
   答案并不会更准。
6. 工具回「本次不可用 / 已经连续失败」时就**别再调它了**，换别的数据或直接作答 ——
   那说明数据源整体有问题，重试不会有新结果。
7. 工具查不到「谁跟谁一起开黑」这类**关系型**结论：同场开黑只能从同一场比赛里
   出现两人以上来判断，而工具是单人视角。要回答开黑问题就用上面上下文里的
   「同场局」信息。
8. 工具里**没有**绑定、解绑、加监听这类会改数据的操作。用户要这些就直接告诉他
   用 `/d2 绑定 …`、`/d2 监听 …` 指令。
9. 单场复盘（要比赛 ID 的那种）也不在工具里：它可能要等解析好几分钟，
   请让用户直接说「复盘 <比赛ID>」由指令处理。
"""
