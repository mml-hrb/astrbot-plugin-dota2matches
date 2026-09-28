"""工具层：把插件的**全部能力**暴露给模型按需调用。

为什么要这一层
--------------
「dota2助手 钢板最近打得怎么样？顺便给他推荐几个轮椅英雄」这类问题
**一次要用到多个功能**：既要个人战绩，又要版本榜的个人适配。意图分类
一次只能给一个 label，无论判成哪个都注定少答一半。做法是把每个能力包成
一个工具，让模型自己决定调几次、调哪些，结果回填后再作答 —— 也就是
标准的 function calling 循环。

三层边界（改动前必须整段读完，别只看一行）
------------------------------------------
* **只读工具**（:data:`TOOL_READ_NAMES`）直接执行，把真实数据回填给模型。
* **写操作工具**（:data:`TOOL_WRITE_NAMES`）**绝不直接执行**。它们只把动作
  登记成「待确认」，再由插件把确认请求发给用户；用户回「确认」才真的落地。
  理由：模型判断失误的代价不对称 —— 它会真的往监听列表里塞人、真的解绑账号。
  但一刀切不给它手，用户就得多打一遍指令，「用说的就能干所有事」也就落了空。
  折中是**给手，但握手之前要用户点头**。
* **慢工具**（:data:`TOOL_ASYNC_NAMES`）不阻塞对话。单场复盘可能触发催解析
  并等上十分钟，同步等会把整轮问答卡死；因此它们只**发起**后台任务并立刻
  回执，正文稍后由后台任务直接发回会话。
* **每个工具的输出都截断**（:data:`MAX_OUTPUT_CHARS`）。模型不需要全文，
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
        format_match_list,
        format_player_profile,
        format_summary_block,
        hero_meta_note,
        hero_meta_rows,
        match_position,
        pick_heroes_for_player,
        summarize_matches,
    )
    from . import dota_chat, dota_pool
except ImportError:  # 兜底：以普通模块方式加载时，把插件目录加入 sys.path
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dota_format import (  # type: ignore[no-redef]
        format_hero_meta_board,
        format_match_list,
        format_player_profile,
        format_summary_block,
        hero_meta_note,
        hero_meta_rows,
        match_position,
        pick_heroes_for_player,
        summarize_matches,
    )
    import dota_chat  # type: ignore[no-redef]
    import dota_pool  # type: ignore[no-redef]

#: 单个工具返回给模型的文本上限。超出的部分截掉并标注 —— 只留最前面
#: 最有信息量的部分，比塞满上下文更有用。
MAX_OUTPUT_CHARS = 2000

#: 单个工具的执行超时（秒）。工具内部可能串行调多个数据源接口
#: （英雄表 + 玩家资料 + 英雄池），给得比单接口宽一些。
DEFAULT_TOOL_TIMEOUT = 30.0

#: 战绩类工具一次最多取多少场
MAX_MATCHES_PER_CALL = 50

#: ``compare_players`` 一次最多比几个人。六七个是群规模，再多也没人真要看，
#: 反而会把调用预算烧光（每人一次取数）。
MAX_COMPARE_PLAYERS = 8

#: 没点名比谁时，默认拿本会话名单里的前几位。
DEFAULT_COMPARE_PLAYERS = 6


class ConfirmUnavailable(Exception):
    """写操作**登记不上**，``str(e)`` 是**给模型看的**原因。

    为什么要把原因抛上来：以前这里只有一句笼统的「没能登记成功」，模型
    拿不到任何细节，只能自己编 —— 真机上它编出的是「建议你定个 2 小时后
    的手机闹钟」，而真实原因是「没解析出时间」。工具是模型唯一的眼睛，
    失败时不给原因，它就只能给用户一个看起来像样、实际上跑偏的建议。

    所以插件侧（``main._nlu_ask_confirm``）在每一条失败分支上都要带上
    「为什么 + 用户怎么改」，这里原样转给模型。
    """

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

#: 比赛 ID 的合理下界。Dota2 的比赛 ID 早就越过百亿，低于这个数的
#: 基本可以断定是模型把账号 ID（< 43 亿）或场次数当成了比赛 ID ——
#: 与其拿它去查、拿回一个「查不到」，不如当场说清楚。
MIN_MATCH_ID = 1_000_000_000

#: 慢工具的 ``kind``（对应 :attr:`ToolContext.trigger_async` 的第一个参数）。
ASYNC_KINDS: tuple[str, ...] = ("match_detail", "parse")


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
    #: ``await ask_confirm(action)``：把**待确认动作**登记到插件侧，返回
    #: **给用户看的确认文案**。``action`` 有两种形状::
    #:
    #:     {"kind": "handler", "name": "bind", "args": "天鸽",
    #:      "verb": "绑定账号", "desc": "绑定账号「天鸽」"}
    #:     {"kind": "schedule", "request": "每天早上七点通报群里战绩情况"}
    #:
    #: ``desc`` 是**给人读的整句描述**，插件的确认文案直接用它 ——
    #: 不能拿 ``verb`` + ``args`` 现拼：解绑 / 取消监听的目标本来就为空，
    #: 拼出来会是「解除绑定「」」；定时任务管理的 ``args`` 又是控制词
    #: （「删 2」），拼进去读起来像另一个动作。
    #:
    #: 登记失败时**抛 :class:`ConfirmUnavailable`**（带「为什么 + 怎么改」，
    #: 会原样转给模型）；返回空串表示「登记失败但没原因」；为 ``None``
    #: 表示当前环境不支持待确认操作（例如离线单测），写工具会据此回一句
    #: 「请改用指令」。
    ask_confirm: Callable[[dict], Awaitable[str]] | None = None
    #: ``await trigger_async(kind, params)``：发起一个**慢任务**并返回给模型
    #: 看的回执。``kind`` 取 :data:`ASYNC_KINDS` 里的值。为 ``None`` 时慢工具
    #: 会拒绝执行，而不是假装受理。
    trigger_async: Callable[[str, dict], Awaitable[str]] | None = None
    #: ``await schedules_text()``：本会话定时任务列表的**渲染文本**。
    #: 渲染留在插件主体（工具层不重复实现一套排版，否则两处会漂移）。
    schedules_text: Callable[[], Awaitable[str]] | None = None
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
    #: 写工具登记成功后追加的、**待发给用户**的确认文案。调用方在 agent 循环
    #: 结束后统一取走发送 —— 由插件发而不是让模型转述：确认文案（尤其定时任务
    #: 的计划文本）要和用户逐字对齐，模型顺手改一个字，用户点头的东西就和
    #: 实际建的不是一回事了。
    pending_prompts: list[str] = field(default_factory=list)


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


def build_tool_specs(*, include_write: bool = True) -> list[dict]:
    """返回工具清单（每次新拷贝，调用方随意改动不会互相影响）。

    Args:
        include_write: 是否把**写操作**工具（绑定 / 监听 / 定时任务）放进
            清单。**定时任务到点执行时必须传 ``False``** —— 写操作走确认
            闸门，而那一刻没有任何人守着回「确认」，模型会把「已登记待
            确认」当成「已经办好了」报告给群里，用户收到的是一句假话。
    """
    specs = [
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
            "问「他最近打得怎么样」「谁最菜」时用。只查一个人，多个人要分别调用。"
            "用户**明确说了时间范围**（「这三天」「最近一周」「这两天」）时必须填 days，"
            "否则你会把好几天前的局当成这段时间的成绩。",
            {
                "player": _PLAYER_PROP,
                "count": {
                    "type": "integer",
                    "description": "查最近几场，默认 10，最多 50。",
                },
                "days": {
                    "type": "integer",
                    "description": (
                        "只看**最近几天**的对局（含今天）。用户说了时间范围才填："
                        "「这三天 / 最近三天」填 3，「最近一周」填 7，「这两天」填 2；"
                        "用户没提时间就别填（此时按最近 N 场统计）。"
                    ),
                },
            },
            [],
        ),
        _spec(
            "compare_players",
            "把**多位玩家**放在同一时间范围里横向对比：每人的战绩摘要与逐场明细、"
            "胜率 / KDA / GPM 的分项排名、以及他们**一起打过哪些场**（判断是否开黑）。"
            "问「群里谁最猛 / 谁最菜 / 谁在掉分」「昨天谁打得好」「我们昨晚开黑打得怎么样」"
            "这类**涉及多人**的问题就用它，一次调用把所有人一起比完。"
            "只问一个人时用 query_matches，不要用这个。",
            {
                "players": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "要比的玩家昵称列表，例如 [\"钢板\", \"天鸽\"]。"
                        "用户说的就是「群里的人 / 被监听的几个人 / 所有人」时不填，"
                        "默认比本会话全部绑定与监听的人。"
                    ),
                },
                "days": {
                    "type": "integer",
                    "description": (
                        "只看**最近几天**的对局（含今天）。用户说了时间范围才填："
                        "「这三天 / 最近三天 / 这三天」填 3，「最近一周」填 7，"
                        "「昨天」填 1；用户没提时间就别填（此时按最近 N 场统计）。"
                    ),
                },
                "count": {
                    "type": "integer",
                    "description": "每人取最近几场，默认 10，最多 50。",
                },
            },
            [],
        ),
        _spec(
            "query_hero_pool",
            "查询某位玩家的英雄池统计：每个英雄的场次与胜率。"
            "口径是**当前版本**（样本太少时会自动并入更早的版本并说明），"
            "并且**包含加速模式**——这群人多数对局是加速局，不带上它池子几乎是空的。"
            "问「他会玩什么」「绝活是什么」「英雄池深不深」时用。",
            {"player": _PLAYER_PROP},
            [],
        ),
        _spec(
            "query_profile",
            "查询某位玩家的账号资料：段位、生涯总场次与胜率、当前版本的常用英雄。"
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
        # ---------------- 只读：定时任务 ----------------
        _spec(
            "query_schedules",
            "列出本会话已建的定时任务：按时间触发的和按场次触发的都在内，"
            "含编号、周期、下次执行时间、启停状态。"
            "问「我建了哪些定时任务」「每天几点播报」「有没有定时任务」时用。",
            {},
            [],
        ),
        # ---------------- 慢任务：只发起，不等待 ----------------
        _spec(
            "query_match_detail",
            "让插件为**指定的一场**比赛写完整复盘（战报 / 分路 / 出装 / 关键节点）。"
            "这个动作在后台跑：可能需要先申请录像解析、等上几分钟到十几分钟，"
            "所以工具只负责**发起**并立刻返回受理回执，复盘正文稍后由插件"
            "直接发到本会话。比赛 ID 必须由用户给出，或者来自会话上下文里"
            "已经出现过的某一场（工具会自行取用，不要编造）。"
            "问「复盘这一局」「刚才那场打得怎么样」时用。",
            {
                "match_id": {
                    "type": "integer",
                    "description": "要复盘的比赛 ID（8~10 位数字）。",
                }
            },
            ["match_id"],
        ),
        _spec(
            "request_match_parse",
            "催平台去解析某场比赛的录像。解析是异步的，可能要几分钟到十几分钟。"
            "只在用户**明确说要催**、或复盘时提示「录像还没解析好」时才用；"
            "不要主动替用户催。",
            {
                "match_id": {
                    "type": "integer",
                    "description": "要催解析的比赛 ID（8~10 位数字）。",
                }
            },
            ["match_id"],
        ),
        # ---------------- 写操作：只登记，必须用户确认 ----------------
        _spec(
            "manage_binding",
            "绑定或解绑某个玩家的 Dota2 账号。「绑定」问「把 XX 绑上」「我是 XXX」"
            "时用；「解绑」问「不是我」「换一个」时用（解绑作用于提问者自己的绑定）。"
            "**这个动作不会立刻生效**：插件会请用户确认，用户回「确认」才真的执行。",
            {
                "action": {
                    "type": "string",
                    "enum": ["bind", "unbind"],
                    "description": "bind = 绑定，unbind = 解绑提问者自己的绑定。",
                },
                "player": {
                    "type": "string",
                    "description": (
                        "要绑定的玩家：昵称、32 位账号 ID 或 64 位 SteamID。"
                        "只有在 action=bind 时是必需的；解绑不用填。"
                    ),
                },
            },
            ["action"],
        ),
        _spec(
            "manage_watch",
            "添加或取消「监听某个人的比赛，打完自动往本会话推送短评」。"
            "**这个动作不会立刻生效**：插件会请用户确认，用户回「确认」才真的执行。"
            "取消时不填 player 就取消当前绑定的那个人的监听。",
            {
                "action": {
                    "type": "string",
                    "enum": ["add", "remove"],
                    "description": "add = 加监听，remove = 取消监听。",
                },
                "player": {
                    "type": "string",
                    "description": (
                        "要监听 / 取消监听的人：昵称或账号 ID。"
                        "取消时留空表示提问者当前绑定的那个人。"
                    ),
                },
            },
            ["action"],
        ),
        _spec(
            "manage_schedule",
            "创建 / 删除 / 停用 / 启用 / 修改定时任务的触发时间。"
            "**这个动作不会立刻生效**：插件会请用户确认，用户回「确认」才真的执行。"
            "创建时 request 必须填**用户的原话**（例如「每天早上七点通报群里战绩情况」"
            "「每监听到十盘战绩就生成一份总结」「两小时后重新查看一下 9017256966 这场」），"
            "不要自己改写或拆成时间字段 —— 「每 N 场」这类说法只有原句才能解析对。"
            "插件认的时间说法包括**相对时间**（几分钟后 / 几小时后 / 过一会儿 / 几天后），"
            "用户这么说时照原话填，别替他换成每天或别的时刻。"
            "**到点执行的就是这句话本身**：时间一到，插件会把原话重新交给模型，"
            "按那一刻的真实数据执行。所以 request 要写清「要做什么」"
            "（「每天早上七点通报群里战绩」），而不是「提醒用户去看战绩」——"
            "后者到点只会发出一句让用户自己动手的提醒。"
            "删除 / 停用 / 启用 / 改时间之前先调 query_schedules 拿编号。",
            {
                "action": {
                    "type": "string",
                    "enum": ["create", "delete", "disable", "enable", "retime"],
                    "description": (
                        "create = 新建（用 request）；delete = 删除；"
                        "disable = 停用；enable = 启用；retime = 改触发时间。"
                    ),
                },
                "request": {
                    "type": "string",
                    "description": (
                        "仅 action=create 时必需：用户的**原话**，"
                        "要带上时间（可以是「两小时后」「明天早上八点」这类说法）"
                        "或「每 N 场」，以及要做什么。"
                        "**这句话到点会被原样交给模型按当时的数据执行**，"
                        "所以要写成「做什么」（「每天七点通报群里战绩」），"
                        "不要写成「提醒我」—— 那到点只会吐一句提醒。"
                    ),
                },
                "target": {
                    "type": "string",
                    "description": (
                        "仅 action 为 delete/disable/enable/retime 时必需："
                        "query_schedules 列表里的编号（数字）或任务编号。"
                        "用户说的是「所有 / 全部 / 都」这类**整批**操作时填 `*`"
                        "（delete / disable / enable 支持；retime 不支持，"
                        "因为改时间必须落到某一个任务上）。"
                    ),
                },
                "when": {
                    "type": "string",
                    "description": (
                        "仅 action=retime 时必需：新的触发时间，例如「早上八点」"
                        "「每天 22:30」。**只给钟点时会沿用原来的周期**"
                        "（原来每周五，改完还是每周五）。"
                    ),
                },
            },
            ["action"],
        ),
    ]
    if not include_write:
        # 定时通道：把写操作**整个摘掉**，而不是「留着但禁用」——
        # 留着的话模型仍会去调，然后拿到一句「当前环境不支持」，
        # 白烧一轮调用，还把「本来能做」的错觉留给它。
        specs = [
            spec for spec in specs if spec["function"]["name"] not in TOOL_WRITE_NAMES
        ]
    return specs


#: **只读**工具：直接执行，把真实数据回填给模型。
TOOL_READ_NAMES: tuple[str, ...] = (
    "list_players",
    "query_matches",
    "compare_players",
    "query_hero_pool",
    "query_profile",
    "query_meta",
    "recommend_heroes",
    "query_schedules",
)

#: **慢任务**工具：只发起后台任务、立刻回执，**不等待结果**。
#: 它们本质上也是只读的，单列出来是因为「不等」这个约束必须显式可见 ——
#: 谁把它们改成同步等待，谁就会把整轮问答卡死几分钟。
TOOL_ASYNC_NAMES: tuple[str, ...] = (
    "query_match_detail",
    "request_match_parse",
)

#: **写操作**工具：只登记待确认动作，**绝不直接执行**。
#: 测试里有一条硬断言：这三个必须在 :data:`TOOL_WRITE_NAMES` 里，
#: 且执行它们不能改变任何插件状态（除了登记待确认）。
TOOL_WRITE_NAMES: tuple[str, ...] = (
    "manage_binding",
    "manage_watch",
    "manage_schedule",
)

#: 全部工具名。顺序与 :func:`build_tool_specs` 一致，便于对照。
TOOL_NAMES: tuple[str, ...] = TOOL_READ_NAMES + TOOL_ASYNC_NAMES + TOOL_WRITE_NAMES

#: 定时任务**到点执行**时允许用的工具：只读 + 慢任务，**不含写操作**。
#: 到点那一刻没有任何人守着回「确认」，写操作只会产出一句
#: 「已登记待确认」的假回执（见 :func:`build_tool_specs` 的 ``include_write``）。
SCHEDULE_TOOL_NAMES: tuple[str, ...] = TOOL_READ_NAMES + TOOL_ASYNC_NAMES


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


async def _guarded(
    coro: Awaitable[Any], ctx: ToolContext, *, factor: float = 1.0
) -> Any:
    """给工具内部的数据源调用加超时。

    下限只取 0.1 秒：超时值由配置决定，配置成 0 / 负数时回落到
    :data:`DEFAULT_TOOL_TIMEOUT`。**不要**在这里写死一个较大的下限
    （例如 5 秒）—— 那会让「把超时调小」这个配置项彻底失效，
    测试也没法验证超时分支。

    Args:
        factor: 放大倍数。有些调用内部要串好几个接口（英雄池按版本放宽时
            可能连查 3 个版本、每个两次请求），按单接口的超时去卡它必然误杀。
    """
    timeout = float(ctx.timeout or 0) or DEFAULT_TOOL_TIMEOUT
    return await asyncio.wait_for(coro, timeout=max(0.1, timeout * max(1.0, factor)))


async def _pool_of(ctx: ToolContext, account_id: int) -> "dota_pool.HeroPool":
    """按插件配置取一位玩家的英雄池（版本口径 + 含加速模式）。

    口径配置集中在这里读，三个用到英雄池的工具共用一份，避免各读各的
    导致同一个回答里出现两种口径。**取数失败时返回空池**（不抛异常）。
    """
    min_games = _clamp_int(
        ctx.cfg("hero_pool_min_games", dota_pool.DEFAULT_MIN_GAMES),
        default=dota_pool.DEFAULT_MIN_GAMES,
        low=1,
        high=100000,
    )
    max_patches = _clamp_int(
        ctx.cfg("hero_pool_max_patches", dota_pool.DEFAULT_MAX_PATCHES),
        default=dota_pool.DEFAULT_MAX_PATCHES,
        low=1,
        high=20,
    )
    cfg_turbo = ctx.cfg("hero_pool_include_turbo", True)
    cfg_scope = ctx.cfg("hero_pool_patch_scope", True)
    pool = await _guarded(
        dota_pool.collect_hero_pool(
            ctx.api,
            account_id,
            min_games=min_games,
            max_patches=max_patches,
            include_turbo=False if cfg_turbo is False else True,
            patch_scope=False if cfg_scope is False else True,
            timeout=ctx.timeout or dota_pool.DEFAULT_TIMEOUT,
        ),
        ctx,
        factor=3.0,
    )
    return pool


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
    # days 缺省 0 = 不限时间（按最近 N 场统计）。
    days = _clamp_int(args.get("days"), default=0, low=0, high=30)
    window = dota_chat.recent_days_window(days, ctx.now or None) if days else None
    if window is not None:
        # 有时间范围就放宽取数：默认 10 场常常只覆盖一两天，
        # 说「这三天」时手上可能根本没取全，会得出「他只打了 2 场」这种假否定。
        count = max(count, min(dota_chat.WINDOW_FETCH_LIMIT, MAX_MATCHES_PER_CALL))
    matches = await _guarded(ctx.fetch_matches(account_id, count), ctx)
    if not matches:
        return (
            f"{name} 最近没有可用的对局记录"
            "（可能未公开比赛数据，或这段时间没打）。"
        )
    scope_note = ""
    if window is not None:
        scoped = dota_chat.scope_matches(matches, window)
        if not scoped:
            return (
                f"{name} 在{window.describe()}内没有对局记录"
                f"（手上另有最近 {len(matches)} 场其它时段的记录，未计入本次统计）。"
            )
        scope_note = (
            f"（统计口径：{window.describe()}——手上最近 {len(matches)} 场里"
            f"有 {len(scoped)} 场落在范围内，其余 {len(matches) - len(scoped)} 场不计）"
        )
        matches = scoped
    heroes = await _guarded(ctx.heroes(), ctx)
    summary = summarize_matches(matches)
    head = (
        f"=== {name} {window.describe()} 共 {len(matches)} 场 ==="
        if window is not None
        else f"=== {name} 最近 {len(matches)} 场 ==="
    )
    blocks = [
        head,
        format_summary_block(summary, heroes),
        "",
        format_match_list(name, account_id, matches, heroes, title="逐场明细"),
    ]
    text = "\n".join(blocks)
    return f"{text}\n\n{scope_note}" if scope_note else text


def _split_names(raw: Any) -> list[str]:
    """把模型给的玩家列表归一成字符串列表（容忍它填成逗号分隔的一整串）。"""
    if isinstance(raw, (list, tuple, set)):
        items = [str(x) for x in raw]
    else:
        text = str(raw or "")
        for sep in ("，", "、", ",", ";", "；", "/", "|", "\n"):
            text = text.replace(sep, "\n")
        items = text.split("\n")
    out: list[str] = []
    for item in items:
        token = item.strip()
        if token and token not in out:
            out.append(token)
    return out


async def _compare_one(
    ctx: ToolContext,
    account_id: int,
    name: str,
    relation: str,
    count: int,
    window: "dota_chat.TimeWindow | None",
) -> tuple["dota_chat.PlayerSnapshot", int]:
    """取一位玩家的近期战绩并套上时间窗口，返回 ``(快照, 窗口外场次数)``。

    单人失败**不影响**其他人：这一位的快照带上 ``error``，其余照常比。
    """
    snapshot = dota_chat.PlayerSnapshot(
        account_id=account_id, name=name, relation=relation
    )
    try:
        rows = await _guarded(ctx.fetch_matches(account_id, count), ctx)
    except asyncio.TimeoutError:
        snapshot.error = "请求超时"
        return snapshot, 0
    except Exception as e:  # noqa: BLE001 - 单人失败不该拖垮整次对比
        # 带上原始消息：模型要靠它判断「是这人没数据」还是「数据源在抖」，
        # 只给一个类型名（`RuntimeError`）等于什么都没说。
        snapshot.error = f"{type(e).__name__}: {str(e)[:80]}"
        logger.warning(f"[dota2] compare_players 取 {name}({account_id}) 失败: {e}")
        return snapshot, 0
    rows = [m for m in (rows or []) if isinstance(m, dict)]
    if window is None:
        snapshot.matches = rows
        return snapshot, 0
    # 口径只在一处生效：``window`` 同时挂到快照上，摘要/明细/对比三处
    # 读的都是 ``scoped``，不会出现「胜率按窗口算、明细却不是」的错位。
    snapshot.matches = dota_chat.scope_matches(rows, window)
    snapshot.window = window
    return snapshot, len(rows) - len(snapshot.matches)


async def _tool_compare_players(args: dict, ctx: ToolContext) -> str:
    """多人横向对比：一次把若干玩家的战绩摊开，并找出同场开黑。

    为什么要有这个工具：横向对比（谁最猛 / 谁最菜 / 谁跟谁一起打的）需要
    **多个人在同一口径下的数据**，而其余查询类工具都是单人视角。让模型逐个调
    ``query_matches`` 会串行取六次数（用户干等好几分钟），且各次的时间范围
    一旦不一致，比出来的高下就站不住。这里一次取完，口径天然一致。
    """
    count = _clamp_int(args.get("count"), default=10, low=1, high=MAX_MATCHES_PER_CALL)
    days = _clamp_int(args.get("days"), default=0, low=0, high=30)
    window = dota_chat.recent_days_window(days, ctx.now or None) if days else None
    if window is not None:
        # 同 query_matches：默认 10 场往往只覆盖一两天，说「这三天」时
        # 手上可能压根没取全，会得出「他就打了 2 场」这种假否定。
        count = max(count, min(dota_chat.WINDOW_FETCH_LIMIT, MAX_MATCHES_PER_CALL))

    names = _split_names(args.get("players"))
    problems: list[str] = []
    targets: list[tuple[int, str, str]] = []

    if names:
        limit = _clamp_int(
            ctx.cfg("nlu_chat_max_players", DEFAULT_COMPARE_PLAYERS),
            default=DEFAULT_COMPARE_PLAYERS,
            low=1,
            high=MAX_COMPARE_PLAYERS,
        )
        if len(names) > limit:
            problems.append(f"一次最多比 {limit} 个人，后面的先略过了。")
        for raw in names[:limit]:
            try:
                account_id, label = await ctx.resolve_player(raw)
            except Exception as e:  # noqa: BLE001 - 认不出人要让模型自己换说法
                problems.append(f"「{raw}」没认出来：{str(e)[:80]}")
                continue
            targets.append((account_id, label, ""))
    else:
        rows = ctx.session_players() or []
        limit = _clamp_int(
            ctx.cfg("nlu_chat_max_players", DEFAULT_COMPARE_PLAYERS),
            default=DEFAULT_COMPARE_PLAYERS,
            low=1,
            high=MAX_COMPARE_PLAYERS,
        )
        for row in rows[:limit]:
            try:
                account_id = int(row.get("account_id") or 0)
            except (TypeError, ValueError):
                continue
            if account_id > 0:
                targets.append(
                    (
                        account_id,
                        str(row.get("name") or f"账号{account_id}"),
                        str(row.get("relation") or ""),
                    )
                )

    if not targets:
        hint = "、".join(problems) if problems else ""
        return (
            "没有可对比的对象：本会话既没有绑定 / 监听的玩家，也没认出你点的人。"
            + (f"\n（{hint}）" if hint else "")
            + "\n可以先让用户说清楚要比谁的昵称。"
        )

    results = await asyncio.gather(
        *(
            _compare_one(ctx, account_id, name, relation, count, window)
            for account_id, name, relation in targets
        )
    )
    snapshots = [item[0] for item in results]
    excluded = sum(item[1] for item in results)
    heroes = await _guarded(ctx.heroes(), ctx)
    now = ctx.now or None

    with_data = [s for s in snapshots if s.games]
    failed = [s for s in snapshots if s.error]
    empty = [s for s in snapshots if not s.error and not s.games]
    if not with_data:
        period = window.describe() if window is not None else "最近一段时间"
        detail = "；".join(
            f"{s.name}（{s.error}）" if s.error else f"{s.name}（{period}内 0 场）"
            for s in snapshots
        )
        return f"{period}内这 {len(snapshots)} 个人都没有可用的对局记录：{detail}。"

    head = (
        f"=== {len(snapshots)} 人横向对比（口径：{window.describe()}）==="
        if window is not None
        else f"=== {len(snapshots)} 人横向对比（每人最近 {count} 场）==="
    )
    lines = [head]
    if window is not None and excluded:
        lines.append(
            f"（时间范围外的场次已剔除，共 {excluded} 场不计入任何统计；"
            "下面每人的数字都只算范围内的场次）"
        )
    lines.append("")

    for snap in snapshots:
        lines.append(snap.summary_line(now))
        detail = snap.detail_lines(heroes, now)
        if detail:
            lines.extend(detail)
    lines.append("（上面缩进的行是对应玩家的逐场明细）")

    comparison = dota_chat.format_comparison_block(snapshots)
    if comparison:
        lines.append("")
        lines.append(comparison)

    # 同场开黑：唯一能直接横向比高下的场景（同一局里对位 / 同队）。
    party = dota_chat.format_party_block(snapshots, heroes, now)
    if party:
        lines.append("")
        lines.append(party)

    tail: list[str] = []
    if failed:
        tail.append(
            "没取到数据的："
            + "、".join(f"{s.name}（{s.error}）" for s in failed)
            + " —— 别提他们的数字。"
        )
    if empty:
        period = window.describe() if window is not None else "最近这段时间"
        tail.append(
            f"{'、'.join(s.name for s in empty)} 在{period}内一场没打"
            "（这是「确实没打」，不是数据没取到）。"
        )
    if problems:
        tail.append("另外：" + " ".join(problems))
    if tail:
        lines.append("")
        lines.extend(tail)

    return "\n".join(lines)


async def _tool_query_hero_pool(args: dict, ctx: ToolContext) -> str:
    account_id, name = await _person(args, ctx)
    pool = await _pool_of(ctx, account_id)
    if not pool.has_data:
        return f"没有取到 {name} 的英雄池数据（可能该账号未公开比赛数据）。"
    heroes = await _guarded(ctx.heroes(), ctx)
    text = dota_pool.format_hero_pool(pool, name, heroes, top=12)
    note = dota_pool.hero_pool_note(pool)
    return text + ("\n\n（口径：" + note + "）" if note else "")


async def _tool_query_profile(args: dict, ctx: ToolContext) -> str:
    account_id, name = await _person(args, ctx)
    profile = await _guarded(ctx.api.get_player(account_id), ctx)
    if not profile:
        return f"数据源查不到账号 {account_id}（{name}）。"
    wl = await _guarded(ctx.api.get_player_wl(account_id), ctx)
    heroes = await _guarded(ctx.heroes(), ctx)
    # 「常用英雄」也按同一份版本口径取，否则档案里的英雄会是几年前的老黄历
    pool = await _pool_of(ctx, account_id)
    text = format_player_profile(profile, wl or {}, pool.rows, heroes)
    if pool.has_data:
        text += f"\n常用英雄口径: {dota_pool.hero_pool_scope_text(pool)}"
    return text


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

    pool = await _pool_of(ctx, account_id)
    hero_rows = pool.rows
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
    lines = [header]
    if pool.has_data:
        lines.append(f"（英雄池口径：{dota_pool.hero_pool_scope_text(pool)}）")
    lines.append(hero_meta_note(meta, extra["patch"]))
    lines.append(text)
    return "\n".join(lines)


async def _tool_query_schedules(args: dict, ctx: ToolContext) -> str:
    if ctx.schedules_text is None:
        return "当前环境不支持查看定时任务，请让用户用 `/d2 定时 列表`。"
    text = await ctx.schedules_text()
    return text or "本会话还没有定时任务。"


def _match_id_arg(args: dict) -> tuple[int | None, str]:
    """解析比赛 ID，返回 ``(ID, 说明)``；说明非空表示没解析成功。"""
    raw = args.get("match_id")
    if raw is None or str(raw).strip() == "":
        return None, "需要比赛 ID 才能做这件事（8~10 位数字）。"
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None, f"比赛 ID 得是数字，{raw!r} 不是。"
    if value < MIN_MATCH_ID:
        # 常见误用：把账号 ID 或「第 N 场」的序号当成了比赛 ID。
        # 与其拿去查、拿回一个「查不到」，不如当场说清 —— 后者会被模型
        # 解读成「这场不存在」，进而编一个理由出来。
        return None, (
            f"{value} 不像比赛 ID（太小了，Dota2 的比赛 ID 是 8~10 位）。"
            "如果那是账号 ID 或列表里的序号，请换一个真正的比赛 ID。"
        )
    return value, ""


async def _async_or_refuse(ctx: ToolContext, kind: str, params: dict) -> str:
    """慢工具的统一出口：发起后台任务，不等待结果。"""
    if ctx.trigger_async is None:
        return "当前环境不支持后台任务，请让用户直接用对应指令发起。"
    try:
        return await ctx.trigger_async(kind, params)
    except Exception as e:  # noqa: BLE001 - 触发失败不该打断整轮回答
        logger.warning(f"[dota2] 后台任务 {kind} 触发失败: {e}")
        return (
            f"[{kind} 启动失败] 后台任务没能起来，请如实告诉用户这次没发起成功，"
            "稍后重试或改用指令。"
        )


async def _tool_query_match_detail(args: dict, ctx: ToolContext) -> str:
    match_id, why = _match_id_arg(args)
    if match_id is None:
        return why
    return await _async_or_refuse(ctx, "match_detail", {"match_id": match_id})


async def _tool_request_match_parse(args: dict, ctx: ToolContext) -> str:
    match_id, why = _match_id_arg(args)
    if match_id is None:
        return why
    return await _async_or_refuse(ctx, "parse", {"match_id": match_id})


# ---------------------------------------------------------------- 写操作


async def _request_confirm(ctx: ToolContext, action: dict) -> str:
    """登记一个待确认动作，返回**给模型看**的说明。

    写工具的统一出口。登记成功后把「给用户看的确认文案」塞进
    :attr:`ToolContext.pending_prompts`，由调用方在 agent 循环结束后发送 ——
    **不让模型转述**：确认文案要和用户逐字对齐（尤其定时任务的计划文本），
    模型顺手改一个字，用户点头的东西就和实际建的不是一回事了。
    """
    if ctx.ask_confirm is None:
        return (
            "当前环境没接上待确认通道（插件侧没提供 ask_confirm），"
            "请让用户改用 `/d2` 指令完成这个操作。"
        )
    prompt = ""
    try:
        prompt = await ctx.ask_confirm(action)
    except ConfirmUnavailable as e:
        # 原因由插件侧给出，**原样转给模型**：模型要能告诉用户「怎么改」。
        # 只回一句「没登记上」，模型就只能自己编理由（真机上编出的是
        # 「你定个手机闹钟吧」）。
        logger.info(f"[dota2] 写操作未能登记：{e}")
        return str(e)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[dota2] 登记待确认动作失败: {e}")
    if not prompt:
        return (
            "这个操作没能登记成功，而且插件侧没给出原因。"
            "请让用户改用 `/d2` 指令再试一次。"
        )
    ctx.pending_prompts.append(prompt)
    ctx.trace.append(f"待确认({action.get('name') or action.get('kind')})")
    return (
        "已登记待确认动作，插件的确认请求已经发给用户。"
        "**注意**：这件事还没发生，不要说「已完成 / 已绑定 / 已建好」；"
        "也不要重复描述操作细节，简短说一句「请回复确认」即可。"
    )


#: 写工具的 action → 用户可读的动词（确认文案里用）。
BIND_VERBS: dict[str, str] = {"bind": "绑定账号", "unbind": "解除绑定"}
WATCH_VERBS: dict[str, str] = {"add": "添加监听", "remove": "取消监听"}
SCHEDULE_VERBS: dict[str, str] = {
    "create": "建一个定时任务",
    "delete": "删掉定时任务",
    "disable": "停用定时任务",
    "enable": "启用定时任务",
    "retime": "改定时任务的触发时间",
}
#: 管理类 action → ``d2_schedule`` 认的控制词。
SCHEDULE_CONTROL: dict[str, str] = {
    "delete": "删",
    "disable": "停",
    "enable": "开",
    "retime": "改",
}


def _desc(verb: str, target: str = "") -> str:
    """拼一句给人读的操作描述；目标为空时只留动词（不出现空的「」）。"""
    target = str(target or "").strip()
    if target == ALL_TARGET:
        target = "全部"
    return f"{verb}「{target}」" if target else verb


#: 整批操作的槽位值。值与 ``dota_schedule.ALL_TOKEN`` 相同（``"*"``），
#: 但**故意不 import 过来**：工具层只负责把模型给的参数拼成 ``d2_schedule``
#: 认得的那串控制文本（``删 *``），认不认是口径层的事。少一层依赖，
#: 少一处「改了口径忘了改工具层」的机会。
ALL_TARGET = "*"

#: 模型把「全部」按**人类说法**原样填进 target 时会出现的形式。
#: 指令层的 ``dota_schedule.normalize_target`` 也认这些词，但那里是给
#: 用户打字用的；工具参数由模型生成，写法五花八门（还会带英文 all），
#: 在这里一次性收敛成槽位值，比让两道正则各维护一份名单稳。
_ALL_TARGET_WORDS = frozenset(
    {"*", "all", "全部", "所有", "一切", "全都", "统统", "全", "都"}
)


def _is_all_target(text: str) -> bool:
    """这个 target 是不是在说「全部任务」。"""
    return str(text or "").strip().lower() in _ALL_TARGET_WORDS


async def _tool_manage_binding(args: dict, ctx: ToolContext) -> str:
    action = str(args.get("action") or "").strip().lower()
    if action not in BIND_VERBS:
        return "action 只能是 bind（绑定）或 unbind（解绑），收到的是 " + repr(action)
    player = str(args.get("player") or "").strip()
    if action == "bind" and not player:
        # 缺参数时**不猜**：猜错的代价是绑错账号，而绑定是长期状态。
        return "要绑定谁还没确定。请先问清用户，再带上昵称或账号 ID 重新调用。"
    # 解绑作用于提问者自己的绑定，文案里不必（也不该）再出现目标。
    return await _request_confirm(
        ctx,
        {
            "kind": "handler",
            "name": "bind" if action == "bind" else "unbind",
            "args": player,
            "verb": BIND_VERBS[action],
            "desc": _desc(BIND_VERBS[action], player if action == "bind" else ""),
        },
    )


async def _tool_manage_watch(args: dict, ctx: ToolContext) -> str:
    action = str(args.get("action") or "").strip().lower()
    if action not in WATCH_VERBS:
        return "action 只能是 add（加监听）或 remove（取消监听），收到的是 " + repr(action)
    player = str(args.get("player") or "").strip()
    if action == "add" and not player:
        return "要监听谁还没确定。请先问清用户，再带上昵称或账号 ID 重新调用。"
    # 取消监听允许不带人：指令侧会回落到「当前绑定的那个人」。
    return await _request_confirm(
        ctx,
        {
            "kind": "handler",
            "name": "watch" if action == "add" else "unwatch",
            "args": player,
            "verb": WATCH_VERBS[action],
            "desc": _desc(WATCH_VERBS[action], player),
        },
    )


async def _tool_manage_schedule(args: dict, ctx: ToolContext) -> str:
    action = str(args.get("action") or "").strip().lower()
    if action not in SCHEDULE_VERBS:
        return (
            "action 只能是 create / delete / disable / enable / retime 之一，"
            "收到的是 " + repr(action)
        )

    if action == "create":
        request = str(args.get("request") or "").strip()
        if not request:
            return (
                "创建定时任务要把**用户的原话**放进 request（例如"
                "「每天早上七点通报群里战绩情况」），不要自己拆成时间字段。"
            )
        # create 的确认文案由插件用**解析后的计划**渲染（几时、做什么、发到哪），
        # 原话本身不是给人核对的好文本，因此这里不带 desc。
        return await _request_confirm(ctx, {"kind": "schedule", "request": request})

    target = str(args.get("target") or "").strip()
    if not target:
        return (
            "要操作哪一个定时任务？请先调 query_schedules 拿到列表编号，"
            "再把编号填进 target；用户说的是「所有 / 全部」时填 `*`。"
        )
    # 整批操作的写法收敛到槽位值 `*`：模型很容易把「全部」按人类说法原样
    # 填进来，而 `d2_schedule.normalize_target` 认的也是这些词 —— 与其指望
    # 两道正则各认一套，不如在这里一次性归一。**retime 不接受 `*`**：
    # 改时间必须落到某一个任务上，"把所有任务的触发时间都改成八点" 在
    # 指令层也走不通（`改 * 早上八点` 会被当成编号越界）。
    if action != "retime" and _is_all_target(target):
        target = ALL_TARGET
    if action == "retime" and _is_all_target(target):
        return (
            "改时间要指定具体某一个任务（先调 query_schedules 拿编号），"
            "不能对全部任务一起改。"
        )
    payload = f"{SCHEDULE_CONTROL[action]} {target}"
    desc = _desc(SCHEDULE_VERBS[action], target)
    if action == "retime":
        when = str(args.get("when") or "").strip()
        if not when:
            return "改时间要给出新的时间，例如「早上八点」「每天 22:30」。"
        payload = f"{payload} {when}"
        desc = f"{desc}，改成「{when}」"
    return await _request_confirm(
        ctx,
        {
            "kind": "handler",
            "name": "schedule",
            "args": payload,
            "verb": SCHEDULE_VERBS[action],
            "desc": desc,
        },
    )


_DISPATCH: dict[str, Callable[[dict, ToolContext], Awaitable[str]]] = {
    "list_players": _tool_list_players,
    "query_matches": _tool_query_matches,
    "compare_players": _tool_compare_players,
    "query_hero_pool": _tool_query_hero_pool,
    "query_profile": _tool_query_profile,
    "query_meta": _tool_query_meta,
    "recommend_heroes": _tool_recommend_heroes,
    "query_schedules": _tool_query_schedules,
    "query_match_detail": _tool_query_match_detail,
    "request_match_parse": _tool_request_match_parse,
    "manage_binding": _tool_manage_binding,
    "manage_watch": _tool_manage_watch,
    "manage_schedule": _tool_manage_schedule,
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

TOOL_GUIDE = """=== 你可以调用工具查数据、也可以调用工具改设置 ===
你手上的工具分**三类**，用法完全不同，务必分清：

【一、查询类】直接执行，返回真实数据
1. 需要真实数据时**直接调用工具**，不要凭上下文猜，也不要让用户自己去敲指令。
   **逐场战绩里的胜负是「那名玩家」自己的胜负** —— 工具一律写成「该玩家胜 /
   该玩家负 / 该玩家胜负未知」这种带主语的形式，直接引用这几个字就行。
   **不要根据 KDA、补刀、经济或「这数据看着该赢」去反推输赢**：工具说的话
   就是事实，凭数据反推很容易把胜局讲成负局（这是最容易犯的错）。
   单场数据里同时出现的「天辉阵营获胜」说的是**两个阵营**谁赢，跟「该玩家
   胜负」是两件事，不要混为一谈。
2. **一次可以调用多个工具**：问题里同时要好几样东西就分别调（例如既问某人战绩、
   又要给他推荐上分英雄，就 query_matches + recommend_heroes 各调一次；
   并行发起的多个调用会一起执行）。
3. 工具返回的就是插件的真实数据。**只讲工具给过的内容**，不要补充记忆里的
   战绩、段位、数字；工具报错或明确说没取到，就如实说这块没拿到。
4. **不要重复调用**同一个工具同一组参数；信息够了就停止调用，直接回答。
5. **多人横向对比（「谁最惨 / 谁最强 / 昨天谁打得好」「我们昨晚开黑怎么样」）
   一律用 `compare_players` 一次调完**，不要逐个调 `query_matches`。
   它把这些玩家放在**同一个时间范围**里一次取完，直接给你每人的摘要、
   分项排名与「同场局」；逐个查六个人既慢（用户要干等好几分钟），
   各次口径还可能不一致，比出来的高下站不住。
6. 工具回「本次不可用 / 已经连续失败」时就**别再调它了**，换别的数据或直接作答 ——
   那说明数据源整体有问题，重试不会有新结果。
7. 「谁跟谁一起开黑」是**关系型**结论，单人视角的 `query_matches` 答不了：
   要用 `compare_players` —— 它会把各人取到的场次按比赛 ID 归并，列出
   「同场局」（同一场比赛里出现两人以上，这是开黑的确凿证据）。没有同场局
   就如实说「这几场没看到你们一起打」，不要替他们编队友。
8. 英雄池类工具（`query_hero_pool` / `recommend_heroes`）返回的口径行里会写明
   **覆盖了哪个版本、共多少场、其中加速模式多少场**。这群人多数对局是加速局，
   所以「加速占比很高」是常态，别当成异常；但也别把加速局的胜率直接说成
   天梯强度 —— 提到胜率时把口径一起说清楚。

【二、后台任务类】只发起，**不等待结果**
9. `query_match_detail`（单场复盘）与 `request_match_parse`（催解析）都是**异步**的：
   调用后立刻返回受理回执，真正的正文由插件稍后直接发到本会话。
   - 拿到回执就告诉用户「已经开始了，结果稍后发到本群」，然后该干嘛干嘛。
   - **不要**为了等结果反复调同一个工具 —— 重调只会重复申请，不会更快。
   - `query_match_detail` 需要比赛 ID：要么用户直接给了，要么会话上下文里已经
     出现过那一场（插件会按上下文自行解析）。**不要编造比赛 ID**，拿不准就先问。
   - `request_match_parse` 只在用户明确说要催、或复盘提示「录像还没解析好」时用。

【三、修改设置类】调了也**不会立刻生效**
10. `manage_binding` / `manage_watch` / `manage_schedule` 覆盖绑定、解绑、加监听、
    取消监听、以及定时任务的增删改停。用户说「帮我绑定 XX」「以后每天七点播报
    战绩」这类**要改设置**的请求时，就调用它们。
11. 调用后插件会给用户发一条确认请求，**用户回「确认」才会真的执行**。因此：
    - **绝对不要说「已完成 / 已绑定 / 已建好」** —— 那时候什么都还没发生；
    - 也不要重复描述操作细节：确认请求里已经写全了时间、周期、发到哪，
      你只需简短说一句「请回复确认」；
    - 用户想自己来，就告诉他可以用 `/d2 绑定 …`、`/d2 监听 …`、`/d2 定时 …`。
12. `manage_schedule` 的 `request` 必须填**用户的原话**，不要自己改写、不要拆成
    时间字段。「每监听到十盘战绩就生成一份总结」这种按场次的说法只有原句才解析
    得对，拆开就变成另一个任务了。
    插件认这些时间说法：钟点（每天早上七点 / 明天早上八点）、**相对时间**
    （两小时后 / 半小时后 / 十分钟后 / 三天后 / 过一会儿）、周期（每周五晚上十点）、
    按场次（每监听到十场）。用户说「两小时后重新看一下这场」时**照原话填**，
    别自作主张替他换成每天、或改成别的时刻。他说「这场」而插件补不出比赛 ID 时，
    工具会让你去问用户，照做即可。
13. 写工具返回的文本**如果是在说「为什么没登记上」，那是在给你解释原因**：
    把它翻成人话告诉用户，并把它建议的那种说法给出来。**不要假装已经成功**，
    也不要自己另编理由或另提建议（真机上曾编出「你定个 2 小时后的手机闹钟」，
    而真实原因是「没解析出时间」）。
14. 删除 / 停用 / 启用 / 改定时任务之前，**先调 `query_schedules` 拿编号**，
    不要凭记忆猜编号。
15. 如果你发现手上**没有**修改设置类的工具（只有查询与后台任务），说明你正在
    **定时任务到点执行**的上下文里 —— 那一刻没有人守着回「确认」，所以写操作
    被摘掉了。此时：
    - **不要承诺**「我已经帮你把监听关了」这类还没发生的事；
    - 需要用户确认的设置改动，就在回答里说一句「你可以回一句 XXX 让我来做」；
    - 你的主要任务是按**现在**的真实数据把事情办了（查最新战绩、出复盘、做总结），
      用户当初那句话说的是什么，就按那个来。
"""
