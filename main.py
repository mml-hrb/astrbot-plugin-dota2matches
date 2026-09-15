"""AstrBot Dota2 数据查询助手插件入口。

功能总览：
1. 绑定玩家（昵称 / 32 位 account_id / 64 位 SteamID）；
2. 查询最近战绩；
3. 调用大模型分析近期表现与打法风格（默认最近 20 场，场次可调）；
4. 调用大模型深度复盘单场比赛（比赛走势、质量评估、十人点评）；
5. 监听玩家，比赛结束后自动推送一条「胜负 + KDA + 近期战绩对照」的短评到会话，支持解绑。

数据来源：OpenDota API（https://docs.opendota.com/）
"""

import asyncio
import functools
import inspect
import re
import time
from pathlib import Path
from typing import Any, NamedTuple

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

try:  # 插件目录被作为包加载时的相对导入
    from .dota_analyzer import (
        LLMRequestError,
        OpenAICompatibleClient,
        WATCH_COMMENT_SYSTEM_PROMPT,
        build_recent_analysis_prompt,
        build_single_match_analysis_prompt,
        build_watch_comment_prompt,
        call_llm,
        resolve_provider,
    )
    from .dota_api import (
        FallbackDataSource,
        OpenDotaClient,
        OpenDotaError,
        TargetNotFoundError,
        to_account_id,
        to_steam_id64,
    )
    from .dota_stratz import StratzClient
    from .dota_format import (
        build_match_data_text,
        fmt_ago,
        fmt_duration,
        format_hero_stats,
        format_match_list,
        format_player_profile,
        format_summary_block,
        hname,
        mode_text,
        normalize_focus_ids,
        parsed_state,
        player_win,
        rank_text,
        summarize_hero_history,
        summarize_matches,
    )
    from .dota_store import DotaStore
    from . import dota_chat
    from . import dota_nlu
    from . import dota_parse
except ImportError:  # 兜底：以普通模块方式加载时（把插件目录加入 sys.path）
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dota_analyzer import (  # type: ignore[no-redef]
        LLMRequestError,
        OpenAICompatibleClient,
        WATCH_COMMENT_SYSTEM_PROMPT,
        build_recent_analysis_prompt,
        build_single_match_analysis_prompt,
        build_watch_comment_prompt,
        call_llm,
        resolve_provider,
    )
    from dota_api import (  # type: ignore[no-redef]
        FallbackDataSource,
        OpenDotaClient,
        OpenDotaError,
        TargetNotFoundError,
        to_account_id,
        to_steam_id64,
    )
    from dota_stratz import StratzClient  # type: ignore[no-redef]
    from dota_format import (  # type: ignore[no-redef]
        build_match_data_text,
        fmt_ago,
        fmt_duration,
        format_hero_stats,
        format_match_list,
        format_player_profile,
        format_summary_block,
        hname,
        mode_text,
        normalize_focus_ids,
        parsed_state,
        player_win,
        rank_text,
        summarize_hero_history,
        summarize_matches,
    )
    from dota_store import DotaStore  # type: ignore[no-redef]

    import dota_chat  # type: ignore[no-redef]
    import dota_nlu  # type: ignore[no-redef]
    import dota_parse  # type: ignore[no-redef]

try:
    # AstrBot 用 GreedyStr 标记「接收剩余全部文本」的参数
    from astrbot.core.star.filter.command import GreedyStr
except Exception:  # pragma: no cover

    class GreedyStr(str):  # type: ignore[no-redef]
        """兼容兜底：退化为普通字符串。"""


PLUGIN_NAME = "astrbot_plugin_dota2"

#: pending 队列的处理节拍（秒）。必须显著小于 watch_interval，
#: 否则 ``next_try_at`` 设成 60s 也要等满一个轮询周期才会被兑现。
PENDING_TICK_SECONDS = 30

#: 监听推送时，最多为几位焦点玩家附带「近期战绩」上下文。
#: 同一局里被监听的玩家越多，这几段统计就会把提示词撑得越长，因此设上限。
MAX_FOCUS_RECENT_CONTEXT = 4

#: 命令行里分隔多个焦点玩家的分隔符。
#: 昵称本身可以含空格（例如「你刚才确实说了【钢板】对吧」），因此**不能**
#: 用空格分隔，必须用顿号 / 逗号 / 分号 / 竖线 / 斜杠这类显式分隔符。
FOCUS_SPLIT_RE = re.compile(r"[、,，;；|/]+")

#: 已知「无法主动推送消息」的平台。这些平台只能被动回复，
#: 因此比赛结束后的自动推送无法送达，需要在添加监听时就提醒用户。
PLATFORMS_WITHOUT_PROACTIVE_PUSH = {
    "qq_official": "QQ 官方机器人",
    "qq_official_webhook": "QQ 官方机器人(Webhook)",
}

# ======================================================================
# 「催解析 + 等解析」相关的常量
# ======================================================================
#: 单场查询里用来跳过等待的开关词：`/d2 单场 <id> skip`
PARSE_SKIP_WORDS = frozenset({"skip", "跳过", "不等", "直接", "fast", "快"})

#: 等待解析时的并发上限兜底（配置项 parse_max_concurrent 缺失时使用）。
#: 每个等待任务每分钟会查一次 ``/matches/{id}``，并发太高容易触发限流。
DEFAULT_PARSE_MAX_CONCURRENT = 3

#: 后台解析任务的登记表容量上限：超过后拒绝新的等待请求，
#: 避免群里刷屏式提交把内存和配额一起吃光。
MAX_PARSE_TASKS = 50


def _fmt_clock(seconds: float) -> str:
    """把秒数格式化成 ``1分30秒`` 这样的可读文本。"""
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    if minutes and secs:
        return f"{minutes}分{secs}秒"
    if minutes:
        return f"{minutes}分钟"
    return f"{secs}秒"


# ======================================================================
# 自然语言入口相关的常量
# ======================================================================
#: 多义意图：这几个意图既可能是闲聊，也可能是真要操作。在群里直接执行
#: 有误触风险，因此先回一句确认，等用户回「确认」再动手。
NLU_CONFIRM_INTENTS = {"bind", "unbind", "watch"}

#: 确认 / 取消词（命中即认为用户回的是确认或放弃）
NLU_CONFIRM_WORDS = frozenset(
    {"确认", "确定", "是的", "对", "对的", "ok", "okay", "好", "好的", "嗯", "yes", "y"}
)
NLU_CANCEL_WORDS = frozenset(
    {"取消", "不用了", "算了", "不", "不要", "否", "no", "n", "别"}
)

#: 等待确认的超时时间（秒）
NLU_CONFIRM_TTL = 120

#: 看到这些开头的消息一律跳过：那是别的插件 / 本插件的指令，不该被截胡。
NLU_SKIP_PREFIXES = ("/", "／", "!", "！", "#", "。", "~", "～")

#: 唤醒词默认值。自然语言入口是个「全局监听器」，不加约束会抢答别的插件的
#: 对话（尤其群里），因此默认要求消息里出现这个词才认。
NLU_DEFAULT_KEYWORD = "dota2助手"


class NluGate(NamedTuple):
    """唤醒词闸门的判定结果。

    自然语言入口需要区分两种情况：

    * 「放行」—— 这条消息不该由插件处理，原样交给别的插件与默认大模型；
    * 「因为命中唤醒词才放行」—— 用户已经**明确点名**了插件，只是没说清
      要什么。此时不该把消息扔回默认大模型（它看不到插件里的监听列表、
      绑定关系、战绩数据），而应该由插件带着数据自己回答，
      这就是「闲聊兜底」的触发条件。

    所以闸门除了返回剥离唤醒词后的正文，还要把 ``keyword_matched``
    一起带出来。
    """

    text: str
    keyword_matched: bool


#: 配置中未填写系统提示词时的兜底内容
DEFAULT_SYSTEM_PROMPT = (
    "你是一位资深的 Dota 2 分析师与教练，擅长从 OpenDota 的对局数据中读出比赛的"
    "真实走向、队伍决策与选手表现。\n\n"
    "严格遵守以下原则：\n"
    "1. 只依据我提供的数据进行推断，绝不编造数据中不存在的信息；数据不足时必须"
    "明确指出「数据不足，无法判断」。\n"
    "2. 每一条结论都要有数据支撑，引用具体数值。\n"
    "3. 语言简洁、专业、克制，不要写填充语，不要复述我已经给你的数据表格。\n"
    "4. 使用中文输出，用 Markdown 小标题与短列表组织内容，总长度控制在 800 字以内。\n"
    "5. 游戏术语保留通行英文写法（Gank、Farm、Roshan、TP、Buyback、Teamfight 等）。\n"
    "6. 点评选手时对事不对人，指出具体该做而没做的事。"
)

HELP_TEXT = """🎮 Dota2 数据查询助手（数据来源：STRATZ / OpenDota 双源）

【账号】
/d2 绑定 <昵称 | 32位账号ID | 64位SteamID>　绑定你的账号
/d2 解绑　　　　　　　　　　　解除当前会话的绑定
/d2 我的　　　　　　　　　　　查看当前绑定
/d2 绑定列表　　　　　　　　　查看本会话所有绑定

【查询】
/d2 资料 [目标]　　　　　　　 查看玩家资料与段位
/d2 英雄 [目标]　　　　　　　 查看英雄使用统计
/d2 战绩 [场次] [目标]　　　　 查看最近战绩（默认 20 场）
/d2 分析 [场次] [目标]　　　　 AI 分析近期表现与打法风格
/d2 单场 <比赛ID> [账号]　　　 AI 深度复盘单场比赛
　　　　（未解析时会自动催解析并等待，最多 10 分钟）
/d2 催解析 <比赛ID>　　　　　 只催 OpenDota 解析这局，不等结果

【监听】
/d2 监听 [目标]　　　　　　　 比赛结束后自动推送一条简短点评到本会话
/d2 取消监听 <目标 | 全部>　　取消你自己添加的监听
/d2 监听列表　　　　　　　　　查看本会话的监听

【模型】
/d2 模型测试　　　　　　　　　自检插件专用的大模型 Key 是否可用

【数据源】
/d2 数据源　　　　　　　　　　自检主/后备数据源的连通性与降级状态

说明：
· 「目标」可以填昵称、32 位账号 ID 或 64 位 SteamID；
· 不填「目标」时，默认使用你在当前会话绑定的账号；
· 一局比赛只点评一次：若这场比赛里有多位被监听的玩家，会在一段短评里逐一点到；
· 监听推送只给「胜负 + KDA + 近期战绩对照」的几句话，不占用录像解析、不用等；
  要看深度复盘（走势、团战、出装）请用 `/d2 单场 <比赛ID>`；
· 「绑定列表」中他人的账号与用户 ID 默认打码，保护群聊隐私；
· 想让查询结果更好看，可以在配置中调整「长报告转为图片发送」。

关于数据源（STRATZ 主 / OpenDota 后备）：
· 默认以 STRATZ 为主数据源，OpenDota 作为后备；主源失效（未配置、鉴权失败、
  限流、网络异常）时会自动切到后备，不需要人工干预；
· STRATZ 的优势是经济数据（GPM/XPM/正补/伤害）一次查询就全带回来，
  没有 OpenDota「最近比赛里只有 20 场有经济数据」的限制；
· 想启用 STRATZ，去 https://stratz.com/api 生成令牌，填进插件配置的
  「STRATZ API Key」。留空则完全跳过 STRATZ、只用 OpenDota，功能不受影响；
· 注意 STRATZ 令牌会绑定首次调用的出口 IP，换网络后可能临时 403，
  插件会自动重试；持续失败会降级到 OpenDota；
· 在配置里把「数据源优先级」改成 opendota，可以两者对调（OpenDota 主 / STRATZ 后备）；
· 随时用 `/d2 数据源` 查看当前主子源、连通性与是否发生过降级。

关于 AI 模型（可选，不影响其他功能）：
· 默认用 AstrBot 里配置的模型来写分析报告；
· 也可以在插件配置里填「专用 API Key」，让本插件的报告单独走一个通道
  （比如用更便宜的模型）。只填 Key 也能用——「接口地址」直接填服务商名
  （deepseek / kimi / qwen / zhipu / siliconflow / openrouter）会自动补全；
· 填完用 `/d2 模型测试` 自检一次，会回显接口地址、模型名、耗时和具体错误；
· 配了专用 Key 但调用失败时，默认会自动回退到 AstrBot 的模型。

关于解析（AI 复盘质量的关键）：
· OpenDota 收录一场比赛 ≠ 解析完这场比赛的录像。只有解析完成才有逐分钟
  经济、团战、出装这些数据，AI 复盘才有质量；
· `/d2 单场 <比赛ID>` 遇到未解析的局会自动提交解析申请，之后每分钟检查一次，
  解析完成就把报告发到本会话，最多等 10 分钟；等不及可以用
  `/d2 单场 <比赛ID> skip` 直接用基础数据出报告；
· 想先排上队、晚点再看，用 `/d2 催解析 <比赛ID>`。

自然语言（不用记指令）：
· 直接说人话也能用，但**开头要带上唤醒词「dota2助手」**，例如
  「dota2助手 帮我看看我的战绩」「dota2助手 分析一下天鸽最近的发挥」
  「dota2助手 这局 8993438099 复盘一下」「dota2助手 最近 20 把打得怎么样」；
· 唤醒词是为了避免抢答其他插件和群里的闲聊：消息里没有这个词，插件一律不响应
  （可在配置中改词或关掉这个限制）；
· 忽略大小写，中间多打空格也认（「Dota2 助手」）；
· 少了唤醒词也不会打断你——插件什么都不做，消息照常交给默认大模型；
· 群聊里不写唤醒词时，仍需 @ 机器人 才会响应（可在配置中关闭这个限制）；
· 群里说「dota2助手 绑定 86745912」这类会改动数据的操作时，插件会先确认再执行；
· 确认 / 取消可以直接回「确认」「取消」，不需要再带唤醒词；
· 写了唤醒词但没识别出具体指令时，插件会**带着本会话的真实数据接着聊**，例如
  「dota2助手 对比一下目前监听的几个人谁最菜」（会读出监听名单与各人近期战绩）、
  「dota2助手 我想转辅助，该怎么练」（会结合你自己的英雄池与近期表现给建议）。
  这类回答同样需要模型可用；用 `/d2 帮助` 里列出的指令能拿到更结构化的报告。"""


def take_over_event(func=None, *, declinable: bool = False):
    """接管事件：直发回复 + （确实接管时）终止事件传播。

    AstrBot 的流水线是「洋葱模型」：handler 每 ``yield`` 一条结果，后续
    阶段（含默认大模型的请求阶段）就会被执行一次。本插件的指令往往要先
    yield 一条「⏳ 正在拉取…」再花十几秒拉数据，如果不做处理，默认大模型
    就会在这个空档里插嘴，编造出「没有绑定成功，无法读取数据。」这类
    插件代码里根本不存在的话术，最后还会在真正的分析结果之前先冒出来。

    因此这里做两件事：

    1. **直发**：把 handler yield 出来的结果通过 ``event.send()`` 直接投递
       （这正是 AstrBot 自己的 RespondStage 使用的通道），不再回灌流水线。
       这样插件执行期间流水线里不会产生任何额外的执行机会。
    2. **终止传播**：handler 接管成功后调用 ``event.stop_event()``，阻止流水线
       继续走到默认大模型阶段。

    两步都做了降级：运行时没有 ``event.send`` 或结果上没有 ``chain`` 时，
    自动退回为正常 ``yield``，保证在老版本 / 自定义 Event 上也能出消息。

    Args:
        declinable: handler 是否**可能主动放弃**某条消息。

            * ``False``（默认）：handler 一旦被调用就必然要接管。适用于
              ``@filter.command`` 这类「只有匹配上才会被调用」的 handler，
              它们无论有没有产出结果，都不该让默认大模型再来插一句。
            * ``True``：handler 可能看一眼后判断「这条不是给我的」而直接
              ``return``。此时**只有真的产出过结果才算接管**；一条都没产出
              就**绝不能** ``stop_event()``。

    为什么 ``declinable`` 必须存在（这是踩过的坑）：AstrBot 的
    ``StarRequestSubStage`` 派发 handler 时是这样的 ——

    .. code-block:: python

        for handler in activated_handlers:
            if event.is_stopped():
                break          # ← 循环直接结束
            async for ret in call_handler(event, handler.handler, **params):
                yield ret
            if event.is_stopped():
                break

    也就是说，**任何一个** handler 调用了 ``stop_event()``，排在它后面的
    handler 全部被跳过；同时 ``ProcessStage`` 里的
    ``event.get_result() and not event.is_stopped()`` 也会变成假，
    默认大模型同样不会被请求。

    对一个挂在 ``EventMessageType.ALL`` 上、**每条消息都会跑一遍**的监听器
    （如 :meth:`Dota2Plugin.d2_natural`）来说，无条件 ``stop_event()``
    等于「只要插件开着，别人说什么都别想被回复」—— 既掐掉其他插件，
    也掐掉正常闲聊。所以这类 handler 必须声明 ``declinable=True``。
    """

    def decorate(func):
        @functools.wraps(func)
        async def wrapper(self, event: AstrMessageEvent, *args, **kwargs):
            #: 是否真的接管了这条消息（产出过至少一条结果）。
            handled = False
            try:
                async for result in func(self, event, *args, **kwargs):
                    handled = True
                    if not await self._deliver_now(event, result):
                        yield result
            finally:
                # 三种情况说明「这条消息归我管」，必须终止传播：
                #   1. 我产出过结果（``handled``，含直发成功的那些）；
                #   2. 我不是可放弃的 handler —— 被调用就意味着接管；
                #   3. 事件在我收尾时已经是停止态 —— 说明我**委托**出去的
                #      内部指令 handler 已经接管并终止了传播。
                # 只有「可放弃 + 什么都没产出 + 事件仍在传播」才是真的放弃，
                # 此时必须原样放行，否则会连带掐死其他插件与默认大模型。
                if handled or not declinable or event.is_stopped():
                    try:
                        event.stop_event()
                    except Exception as e:  # noqa: BLE001 - 老版本没有该 API
                        logger.debug(f"[dota2] 终止事件传播失败（可忽略）: {e}")

        return wrapper

    # 兼容两种写法：``@take_over_event`` 与 ``@take_over_event(declinable=True)``
    if func is not None:
        return decorate(func)
    return decorate


class Dota2Plugin(Star):
    """Dota2 数据查询助手。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.data_dir = self._resolve_data_dir()
        self.store = DotaStore(self.data_dir)
        self.store.load()
        self.api = self._build_data_source()
        #: 等待解析 / 等待推送的比赛：``{match_id: pending_item}``
        #:
        #: 键就是 ``match_id``：**一局比赛只分析一次**。同一局里可能有多位被
        #: 监听的玩家参战（群聊里这种情况很常见），他们共用这一项，分析只生成
        #: 一份、每个会话只推送一条；各位焦点玩家在 ``focuses`` 里各占一项，
        #: 报告中对每个人分别给出深入数据，不会互相覆盖。
        self._pending: dict[int, dict[str, Any]] = {}
        self._watch_task: asyncio.Task | None = None
        self._stopping = False
        self._next_poll_at = 0.0
        #: 自然语言确认状态：``{(umo, uid): (intent_name, args, 过期时间戳)}``
        #: 群里说「绑定 xxx」这类多义指令时，先记下来等用户确认再执行。
        self._nlu_confirm: dict[tuple[str, str], tuple[str, str, float]] = {}
        #: 闲聊兜底用的战绩快照缓存：``{(account_id, limit): (时间戳, matches)}``。
        #: 由插件实例持有、跨会话复用；里面只放**纯数据**，快照对象每次新建
        #: —— 同一个账号在 A 群是「本人」、在 B 群是「被监听」，
        #: 直接把快照对象缓存起来会让两个会话互相污染。
        self._chat_cache: dict[tuple[int, int], tuple[float, list[dict]]] = {}
        #: 正在等待解析的后台任务：``{(umo, match_id, sender_id): task}``
        #: 等待解析最长要十分钟，绝不能把 handler 挂在那里——AstrBot 的流水线
        #: 是洋葱模型，handler 每 yield 一次后续阶段就整体跑一遍。改为后台
        #: 任务等待、完成后主动推送到原会话。
        self._parse_tasks: dict[tuple[str, int, str], asyncio.Task] = {}
        #: 等待解析的并发闸门：同时等待的场次上限由配置决定
        self._parse_sem: asyncio.Semaphore | None = None
        self._parse_sem_size = 0
        logger.info(
            f"[dota2] 插件已加载，数据目录: {self.data_dir}，"
            f"监听: {'开启' if self.cfg('watch_enabled', True) else '关闭'}"
        )
        self._start_watcher()

    # ==================================================================
    # 催解析 + 等解析（后台任务）
    # ==================================================================
    def _parse_semaphore(self) -> asyncio.Semaphore:
        """按配置返回等待解析的并发闸门（配置变了会自动重建）。"""
        size = max(1, int(self.cfg("parse_max_concurrent", DEFAULT_PARSE_MAX_CONCURRENT)))
        if self._parse_sem is None or self._parse_sem_size != size:
            self._parse_sem = asyncio.Semaphore(size)
            self._parse_sem_size = size
        return self._parse_sem

    def _parse_wait_options(self) -> dict[str, Any]:
        """从配置里读出等待解析的节奏参数。"""
        interval = max(
            dota_parse.MIN_CHECK_INTERVAL,
            int(self.cfg("parse_check_interval", dota_parse.DEFAULT_CHECK_INTERVAL)),
        )
        timeout = max(
            interval,
            int(self.cfg("parse_wait_timeout", dota_parse.DEFAULT_WAIT_TIMEOUT)),
        )
        return {"check_interval": interval, "timeout": timeout}

    def _parse_task_key(self, umo: str, match_id: int, uid: str) -> tuple[str, int, str]:
        return (str(umo), int(match_id), str(uid))

    def _start_parse_task(
        self,
        event: AstrMessageEvent,
        match_id: int,
        focus_ids: list[int],
        focus_names: dict[int, str],
        fresh: bool = True,
    ) -> bool:
        """把「催解析 + 等待 + 完成后复盘」交给后台任务执行。

        之所以不在这里直接 ``await``：handler 被 ``take_over_event`` 包装，
        而 AstrBot 的流水线会在 handler 每次 yield 时执行后续阶段。等待十分钟
        期间要保持连接、逐分钟 yield 进度，等于给流水线制造十次执行机会。
        后台任务 + ``context.send_message`` 主动推送则完全没有这个问题。

        Args:
            fresh: 是否重新拉取一份比赛数据作为「第一次状态检查」的依据。
                指令刚拉过数据时传 True 即可（省一次请求），后台任务里
                重拉一次更保险，因此内部仍以重新拉取为准。

        Returns:
            是否成功排入后台任务。
        """
        umo = str(event.unified_msg_origin)
        uid = str(event.get_sender_id())
        key = self._parse_task_key(umo, match_id, uid)

        existing = self._parse_tasks.get(key)
        if existing is not None and not existing.done():
            return False
        if len(self._parse_tasks) >= MAX_PARSE_TASKS:
            logger.warning(
                f"[dota2] 等待解析的任务已达上限（{MAX_PARSE_TASKS}），"
                f"拒绝 {match_id} 的等待请求"
            )
            return False

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - handler 一定在事件循环里
            logger.error(f"[dota2] 无法排入等待解析任务 {match_id}：没有运行中的事件循环")
            return False

        platform = ""
        try:
            platform = event.get_platform_name()
        except Exception:  # noqa: BLE001
            platform = ""

        task = loop.create_task(
            self._parse_and_analyze(
                umo=umo,
                uid=uid,
                match_id=int(match_id),
                focus_ids=[int(i) for i in focus_ids],
                focus_names={int(k): str(v) for k, v in (focus_names or {}).items()},
                platform=platform,
            )
        )
        self._parse_tasks[key] = task
        task.add_done_callback(
            lambda _t, _key=key: self._parse_tasks.pop(_key, None)
        )
        return True

    async def _parse_notify(self, umo: str, text: str) -> None:
        """向会话推送等待过程中的通知（失败只记日志，不影响任务）。"""
        try:
            for chunk in self._chunk_text(text):
                await self._send(umo, MessageChain().message(chunk))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[dota2] 推送解析进度失败（可忽略）: {e}")

    async def _parse_and_analyze(
        self,
        *,
        umo: str,
        uid: str,
        match_id: int,
        focus_ids: list[int],
        focus_names: dict[int, str],
        platform: str = "",
    ) -> None:
        """后台任务：催解析 → 每分钟检查 → 解析完成后出复盘报告。

        无论成功、超时还是异常，都会给用户一个明确的收尾消息。
        """
        options = self._parse_wait_options()
        interval = int(options["check_interval"])
        timeout = int(options["timeout"])
        notify_progress = bool(self.cfg("parse_notify_progress", True))
        submit = bool(self.cfg("parse_submit_request", True))
        minutes = max(1, round(timeout / 60))

        async with self._parse_semaphore():
            try:
                result = await dota_parse.wait_for_parse(
                    self.api,
                    match_id,
                    check_interval=interval,
                    timeout=timeout,
                    submit=submit,
                    resubmit_every=dota_parse.RESUBMIT_EVERY,
                    on_submit=(
                        (lambda ok: self._on_parse_submit(umo, match_id, ok))
                        if submit
                        else None
                    ),
                    on_check=(
                        (lambda state, checks, elapsed: self._on_parse_check(
                            umo, match_id, state, checks, elapsed, minutes
                        ))
                        if notify_progress
                        else None
                    ),
                )
            except asyncio.CancelledError:
                logger.info(f"[dota2] 等待比赛 {match_id} 解析的任务被取消")
                raise
            except Exception as e:  # noqa: BLE001
                logger.error(
                    f"[dota2] 等待比赛 {match_id} 解析时出错：{e}", exc_info=True
                )
                await self._parse_notify(
                    umo,
                    f"❌ 等待比赛 {match_id} 解析时出错：{e}\n"
                    f"可以稍后重试 `/d2 单场 {match_id}`。",
                )
                return

        if not result.parsed or not result.match:
            await self._parse_notify(umo, result.fail_text(match_id))
            return

        await self._parse_notify(
            umo,
            f"✅ 比赛 {match_id} 已解析完成（等待 {_fmt_clock(result.waited)}，"
            f"共检查 {result.checks} 次），开始生成复盘报告…",
        )
        await self._analyze_match_data(
            umo=umo,
            match_id=match_id,
            match=result.match,
            focus_ids=focus_ids,
            focus_names=focus_names,
        )

    async def _on_parse_submit(self, umo: str, match_id: int, ok: bool) -> None:
        """催解析申请结果的通知。"""
        if ok:
            await self._parse_notify(
                umo,
                f"📨 已向 OpenDota 提交比赛 {match_id} 的解析申请，正在队列中。",
            )
        else:
            await self._parse_notify(
                umo,
                f"ℹ️ 解析申请未返回排队凭据（可能已在队列中，或该局 OpenDota 无法解析）。"
                f"仍会继续等待比赛 {match_id} 的解析结果。",
            )

    async def _on_parse_check(
        self,
        umo: str,
        match_id: int,
        state: dota_parse.ParseState,
        checks: int,
        elapsed: float,
        limit_minutes: int,
    ) -> None:
        """每分钟的进度播报。"""
        left = max(0.0, limit_minutes * 60 - elapsed)
        await self._parse_notify(
            umo,
            f"⏳ 比赛 {match_id} 解析进度（第 {checks} 次检查，已等待 "
            f"{_fmt_clock(elapsed)}，剩余最多 {_fmt_clock(left)}）：{state.describe()}",
        )

    async def _analyze_match_data(
        self,
        *,
        umo: str,
        match_id: int,
        match: dict,
        focus_ids: list[int],
        focus_names: dict[int, str],
    ) -> None:
        """数据就绪后生成单场复盘并推送到会话。

        与 ``d2_match`` 共用同一套提示词构造逻辑，保证「等解析出来的复盘」
        与「直接查已解析比赛的复盘」完全一致。
        """
        try:
            heroes = await self.api.get_heroes()
            items = await self.api.get_items()
        except OpenDotaError as e:
            logger.error(f"[dota2] 复盘比赛 {match_id} 时拉取常量失败：{e}")
            heroes, items = {}, {}
        # 技能常量（id → 技能名）：拿不到就省略「技能加点」小节
        abilities = await self._ability_constants()

        focus_ids = [int(i) for i in (focus_ids or []) if int(i or 0)]
        parsed = dota_parse.parse_state(match).parsed
        headline = self.match_headline(match, heroes, focus_ids or None, parsed)
        curve_ids = self._curve_targets(match, focus_ids)

        extra_context = await self._build_recent_context(
            match_id, focus_ids, focus_names, heroes
        )
        prompt = build_single_match_analysis_prompt(
            match=match,
            heroes=heroes,
            items=items,
            focus_account_ids=focus_ids or None,
            focus_names=focus_names or None,
            extra_context=extra_context,
            abilities=abilities,
            curve_ids=curve_ids,
        )
        report = await self._generate_report_for_umo(umo, prompt)

        await self._parse_notify(umo, headline)
        if report:
            image_url = await self._render_image(report)
            if image_url:
                try:
                    await self._send(umo, MessageChain().file_image(image_url))
                    return
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[dota2] 推送复盘图片失败，回退为文本：{e}")
            for chunk in self._chunk_text(report):
                await self._send(umo, MessageChain().message(chunk))
            return

        raw = (
            "⚠️ 未启用大模型分析或模型不可用，以下为从数据源获取的完整原始数据：\n\n"
            + build_match_data_text(
                match,
                heroes,
                items,
                focus_account_ids=focus_ids or None,
                abilities=abilities,
                curve_ids=curve_ids,
            )
        )
        for chunk in self._chunk_text(raw):
            await self._send(umo, MessageChain().message(chunk))

    async def _generate_report_for_umo(self, umo: str, prompt: str) -> str | None:
        """在指定会话下调用大模型生成报告（供后台任务使用）。"""
        return await self._call_report_llm(prompt, umo=umo)

    async def _ability_constants(self) -> dict[int, str]:
        """技能常量 ``{技能ID: 技能名}``，用于把解析产物里的加点顺序翻译成可读文本。

        这是**尽力而为**的增强项：只有 OpenDota 提供 ``/constants/ability_ids``
        （STRATZ 侧直接返回空字典，或者旧数据源根本没有这个方法）。
        拿不到时提示词会自动省略「技能加点」小节，不影响其余复盘内容。
        """
        getter = getattr(self.api, "get_ability_names", None)
        if getter is None:
            return {}
        try:
            return await getter()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[dota2] 获取技能常量失败，跳过技能加点：{e}")
            return {}

    @staticmethod
    def _curve_targets(match: dict, focus_ids: list[int], limit: int = 2) -> list[int]:
        """挑出「即便没有焦点玩家也值得看发育节奏」的对象。

        没有指定焦点时（例如 `/d2 单场 <比赛ID>` 没写玩家），整场报告就没有
        任何人的逐分钟曲线，而「两边核心的发育速度」恰恰是复盘的关键。
        这里按净经济取前 ``limit`` 位补上；有焦点玩家时返回空列表，
        因为焦点自己的深入小节里已经有完整曲线了。
        """
        if focus_ids:
            return []
        players = [
            player
            for player in (match.get("players") or [])
            if isinstance(player, dict) and player.get("account_id")
        ]
        players.sort(
            key=lambda player: -(
                player.get("net_worth") or player.get("total_gold") or 0
            )
        )
        picked: list[int] = []
        for player in players[: max(0, int(limit))]:
            try:
                picked.append(int(player.get("account_id")))
            except (TypeError, ValueError):
                continue
        return picked

    async def _recent_summary_blocks(
        self,
        match_id: int,
        focus_ids: list[int],
        focus_names: dict[int, str],
        heroes: dict[int, dict],
    ) -> list[str]:
        """拼出几位焦点玩家「本场之外的近期战绩」文本块（每人一段）。

        用于两处：单场深度复盘的「附加上下文」，以及监听短评里判断
        「这局是不是正常发挥」的对照数据。
        """
        recent_count = max(0, int(self.cfg("watch_match_analysis_count", 10)))
        if not focus_ids or not recent_count:
            return []
        blocks: list[str] = []
        for focus_id in focus_ids[:MAX_FOCUS_RECENT_CONTEXT]:
            label = focus_names.get(focus_id) or focus_id
            try:
                recent_matches, economy_samples = (
                    await self.api.get_player_matches_enriched(focus_id, recent_count)
                )
                recent_matches = [
                    m
                    for m in recent_matches
                    if int(m.get("match_id") or 0) != int(match_id)
                ]
            except OpenDotaError as e:
                logger.debug(f"[dota2] 获取 {focus_id} 近期状态上下文失败: {e}")
                continue
            if not recent_matches:
                continue
            summary = summarize_matches(
                recent_matches, economy_samples=economy_samples
            )
            blocks.append(
                f"—— {label}（account_id={focus_id}）在本场之外最近 "
                f"{len(recent_matches)} 场的整体情况"
                f"（用于判断本场是他的正常发挥还是异常）：\n"
                + format_summary_block(summary, heroes)
            )
        return blocks

    async def _build_recent_context(
        self,
        match_id: int,
        focus_ids: list[int],
        focus_names: dict[int, str],
        heroes: dict[int, dict],
    ) -> str:
        """为单场深度复盘拼出「本场之外的近期状态」上下文块（含报告尾部要求）。"""
        blocks = await self._recent_summary_blocks(
            match_id, focus_ids, focus_names, heroes
        )
        if not blocks:
            return ""
        return "\n\n".join(blocks) + (
            "\n\n请在报告最后额外增加一节「## 近期状态」"
            + (
                "，为上面每位焦点玩家各起一个小标题，"
                "各用 3 句以内说明其最近的竞技走向。"
                if len(focus_ids) > 1
                else "，用 3 句以内说明这名玩家最近的整体竞技走向。"
            )
        )

    # ==================================================================
    # 基础设施
    # ==================================================================
    def cfg(self, key: str, default: Any = None) -> Any:
        """安全读取配置项。"""
        try:
            value = self.config.get(key, default)
        except Exception:  # noqa: BLE001
            return default
        return default if value is None else value

    @staticmethod
    def _resolve_data_dir() -> Path:
        """获取插件数据目录（优先使用 AstrBot 规范的 plugin_data 目录）。"""
        try:
            return Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dota2] 无法获取标准数据目录，回退到插件目录: {e}")
            fallback = Path(__file__).resolve().parent / "data"
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback

    # ==================================================================
    # 数据源组装（主 STRATZ + 后备 OpenDota，自动降级）
    # ==================================================================
    #: 数据源优先级取值 → (主源名, 说明)。``stratz`` 为默认。
    DATA_SOURCE_CHOICES = ("stratz", "opendota")

    def _stratz_api_key(self) -> str:
        """读取 STRATZ API Key（去空白，兼容误粘贴换行）。"""
        return str(self.cfg("stratz_api_key", "") or "").strip()

    def _data_source_choice(self) -> str:
        """读取配置里的「数据源优先级」，非法值一律回落到 ``stratz``。"""
        choice = str(self.cfg("data_source_priority", "stratz") or "").strip().lower()
        return choice if choice in self.DATA_SOURCE_CHOICES else "stratz"

    def _source_name(self) -> str:
        """当前生效的数据源名，用于给用户看的提示文案。

        不要在提示里硬编码「OpenDota」——STRATZ 才是默认主源，
        写死会让用户拿着错误的线索去排查。
        """
        api = getattr(self, "api", None)
        label = getattr(api, "describe", None)
        if callable(label):
            try:
                text = str(label() or "").strip()
                if text:
                    return text
            except Exception:  # noqa: BLE001
                pass
        return "STRATZ" if self._data_source_choice() == "stratz" else "OpenDota"

    def _build_data_source(self):
        """按配置组装数据源。

        默认策略：**STRATZ 为主、OpenDota 为后备**。

        - ``stratz_api_key`` 没填时，STRATZ 会被视为「未配置」，
          所有请求直接落到 OpenDota（行为与升级前完全一致）。
        - ``data_source_priority`` 选 ``opendota`` 时，两者对调：
          OpenDota 为主、STRATZ 为后备。
        - 任一主源抛「不可用」类异常（未配置/鉴权/限流/网络）时，
          :class:`FallbackDataSource` 会自动切到后备并记一条 warning。
        """
        opendota = OpenDotaClient(
            api_key=self.cfg("opendota_api_key", ""),
            timeout=self.cfg("request_timeout", 30),
            max_retries=self.cfg("max_retries", 3),
            rate_limit_per_minute=self.cfg("rate_limit_per_minute", 55),
            proxy=self.cfg("http_proxy", ""),
        )
        stratz = StratzClient(
            api_key=self._stratz_api_key(),
            timeout=self.cfg("request_timeout", 30),
            max_retries=self.cfg("max_retries", 3),
            rate_limit_per_minute=self.cfg("stratz_rate_limit_per_minute", 240),
            proxy=self.cfg("http_proxy", ""),
        )

        if self._data_source_choice() == "opendota":
            primary, secondary = opendota, stratz
            primary_label, secondary_label = "OpenDota", "STRATZ"
        else:
            primary, secondary = stratz, opendota
            primary_label, secondary_label = "STRATZ", "OpenDota"

        source = FallbackDataSource(
            primary=primary,
            secondary=secondary,
            primary_label=primary_label,
            secondary_label=secondary_label,
        )
        if primary_label == "STRATZ" and not stratz.configured:
            logger.info(
                "[dota2] 未配置 STRATZ API Key，数据源将直接使用 OpenDota；"
                "填写「STRATZ API Key」后即可启用 STRATZ 主数据源。"
            )
        return source

    @staticmethod
    def _split_count_target(args: str) -> tuple[int | None, str]:
        """把 `20 天鸽` 拆成 ``(20, "天鸽")``；没有数量时返回 ``(None, args)``。"""
        tokens = [token for token in re.split(r"\s+", (args or "").strip()) if token]
        count: int | None = None
        if tokens and re.fullmatch(r"\d{1,3}", tokens[0]):
            value = int(tokens[0])
            if 1 <= value <= 200:
                count = value
                tokens = tokens[1:]
        return count, " ".join(tokens)

    def _chunk_text(self, text: str) -> list[str]:
        """按配置的长度上限把长文本切成多条消息。"""
        limit = max(200, int(self.cfg("max_message_length", 1500)))
        text = text or ""
        if len(text) <= limit:
            return [text]

        chunks: list[str] = []
        buffer: list[str] = []
        size = 0
        for line in text.split("\n"):
            # 单行就超长时先硬切
            while len(line) > limit:
                head, line = line[:limit], line[limit:]
                if buffer:
                    chunks.append("\n".join(buffer))
                    buffer, size = [], 0
                chunks.append(head)
            if size + len(line) + 1 > limit and buffer:
                chunks.append("\n".join(buffer))
                buffer, size = [], 0
            buffer.append(line)
            size += len(line) + 1
        if buffer:
            chunks.append("\n".join(buffer))
        return chunks

    async def _render_image(self, text: str) -> str | None:
        """把报告渲染成图片；未启用或失败时返回 None。"""
        if not self.cfg("analysis_as_image", True):
            return None
        try:
            return await self.text_to_image(text)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dota2] 文本转图片失败，回退为纯文本发送: {e}")
            return None

    async def _deliver_now(self, event: AstrMessageEvent, result) -> bool:
        """尽量把一条结果直接投递出去，成功返回 True。

        直接使用 ``event.send()`` 发送，绕过流水线（AstrBot 自己的
        RespondStage 也是走这个通道）。这样插件在执行期间不会给流水线
        制造新的执行机会，默认大模型也就没有插嘴的时机。

        ``event.send()`` 要的是 ``MessageChain``，也就是 ``result`` **本身**
        —— ``MessageEventResult`` 就是 ``MessageChain`` 的子类。它要的
        **不是** ``result.chain``：那是个普通 ``list``，交给平台适配器会以
        ``'list' object has no attribute 'chain'`` 失败。旧实现正是传了
        ``result.chain``，于是直发在 AstrBot v4.x 上从来没成功过，每次投递
        都退回流水线、还各刷一条 warning。

        当前运行时不支持直发时返回 False，由调用方回退为 ``yield``。
        """
        send = getattr(event, "send", None)
        if not callable(send):
            return False

        # send() 需要「带 chain 属性的消息链对象」，且 chain 不能为空
        # （空 chain 发出去也没有意义）。优先用 result 本身；老版本把
        # result.chain 也做成消息链时再退一步用它。
        target = None
        if getattr(result, "chain", None):
            target = result
        elif hasattr(getattr(result, "chain", None), "chain"):
            target = result.chain
        if target is None:
            return False

        try:
            await send(target)
        except Exception as e:  # noqa: BLE001 - 直发不可用时回退为流水线发送
            logger.warning(f"[dota2] 直接发送失败，回退为流水线发送: {e}")
            return False
        return True

    async def _emit(self, event: AstrMessageEvent, text: str, as_image: bool = False):
        """把文本发送到当前会话（可作为异步生成器使用）。

        长报告优先转成图片发送，失败或未启用时按长度拆分成多条文本。
        """
        if not text:
            return
        if as_image:
            url = await self._render_image(text)
            if url:
                yield event.image_result(url)
                return
        for chunk in self._chunk_text(text):
            yield event.plain_result(chunk)

    # ==================================================================
    # 目标解析
    # ==================================================================
    async def _resolve_account(
        self, target: str, in_match_players: list[dict] | None = None
    ) -> tuple[int, str]:
        """把用户输入解析成 ``(account_id, personaname)``。

        Args:
            target: 用户输入的昵称 / 32 位 ID / 64 位 SteamID / 个人主页链接。
            in_match_players: 可选的「本局选手列表」。给出时，昵称会先在这
                10 个人里精确匹配；命中唯一就直接采用。OpenDota 上重名昵称
                极多（例如「大魔导师马化腾」有 5 个同名账号），只靠全局搜索
                会让这些玩家永远解析不出来，而复盘场景下本局选手就是天然消歧器。

        Raises:
            TargetNotFoundError: 找不到唯一确定的玩家。
            OpenDotaError: 数据源调用失败。
        """
        target = (target or "").strip()
        if not target:
            raise TargetNotFoundError("没有指定玩家。")

        # 支持直接粘贴 Steam 个人主页链接
        match = re.search(r"/profiles/(\d{15,20})", target)
        if match:
            target = match.group(1)

        # 纯数字：按 32 位 / 64 位 ID 处理
        if re.fullmatch(r"\d{5,20}", target):
            account_id = to_account_id(target)
            if not account_id:
                raise TargetNotFoundError(
                    f"`{target}` 不是合法的 Steam ID。\n"
                    "· 32 位 account_id 是 5-10 位数字，例如 `86745912`\n"
                    "· 64 位 SteamID 是 17 位数字、以 7656119 开头，"
                    "例如 `76561198047011640`"
                )
            player = await self.api.get_player(account_id)
            if not player:
                raise TargetNotFoundError(
                    f"{self._source_name()} 查不到账号 **{account_id}**。\n"
                    "常见原因：该账号在 Dota 2 设置中关闭了「公开比赛数据」，"
                    "或从未被数据源索引过。"
                )
            profile = player.get("profile") or {}
            return account_id, profile.get("personaname") or f"账号{account_id}"

        # 昵称在本局选手里唯一命中时直接采用，跳过全局搜索（重名消歧）
        if in_match_players:
            lowered_target = target.lower()
            hits = [
                player
                for player in in_match_players
                if isinstance(player, dict)
                and player.get("account_id")
                and str(player.get("personaname") or "").lower() == lowered_target
            ]
            if len(hits) == 1:
                hit_id = int(hits[0]["account_id"])
                return hit_id, str(hits[0].get("personaname") or f"账号{hit_id}")

        # 昵称：走主数据源搜索（STRATZ 优先，不可用时自动降级）
        try:
            results = await self.api.search_player(target)
        except OpenDotaError as e:
            raise TargetNotFoundError(
                f"昵称搜索失败（{e}）。\n"
                f"{self._source_name()} 的昵称搜索接口偶尔会超时或限流，建议改用"
                "**32 位账号 ID** 或 **64 位 SteamID**，结果更准确也更稳定。"
            ) from e
        if not results:
            raise TargetNotFoundError(
                f"没有搜索到昵称包含「{target}」的玩家。\n"
                "建议直接使用 32 位账号 ID 或 64 位 SteamID 绑定，结果更准确。"
            )

        lowered = target.lower()
        exact = [
            item
            for item in results
            if str(item.get("personaname") or "").lower() == lowered
        ]
        if len(exact) == 1:
            chosen = exact[0]
        elif len(results) == 1:
            chosen = results[0]
        else:
            candidates = (exact or results)[:5]
            lines = [
                f"昵称「{target}」匹配到多个玩家，请使用账号 ID 重新操作：",
                "",
            ]
            for item in candidates:
                lines.append(
                    f"· {item.get('personaname')}　account_id: {item.get('account_id')}"
                    + (
                        f"　（最近游戏 {fmt_ago(item.get('last_match_time'))}）"
                        if item.get("last_match_time")
                        else ""
                    )
                )
            lines.append("")
            lines.append("例如：/d2 绑定 " + str(candidates[0].get("account_id")))
            raise TargetNotFoundError("\n".join(lines))

        account_id = int(chosen["account_id"])
        return account_id, str(chosen.get("personaname") or f"账号{account_id}")

    def _effective_binding(
        self, event: AstrMessageEvent
    ) -> tuple[dict | None, str]:
        """获取当前会话下应该使用的绑定。"""
        umo = event.unified_msg_origin
        uid = str(event.get_sender_id())
        binding = self.store.get_binding(umo, uid)
        if binding:
            return binding, ""
        bindings = self.store.list_bindings(umo)
        if len(bindings) == 1:
            only = next(iter(bindings.values()))
            return only, f"（使用本会话唯一的绑定：{only.get('personaname')}）"
        return None, ""

    @staticmethod
    def _no_binding_reply() -> str:
        return (
            "你还没有绑定 Dota2 账号。\n\n"
            "· 绑定：`/d2 绑定 <昵称 | 32位账号ID | 64位SteamID>`\n"
            "· 也可以临时指定目标：`/d2 战绩 20 天鸽`\n\n"
            "发送 `/d2 帮助` 查看完整用法。"
        )

    # ==================================================================
    # 指令：帮助
    # ==================================================================
    @filter.command_group("d2", alias={"dota2", "刀塔"})
    def d2(self):
        """Dota2 数据查询助手"""

    @d2.command("help", alias={"帮助", "h", "?", "菜单"})
    @take_over_event
    async def d2_help(self, event: AstrMessageEvent):
        """查看 Dota2 助手的使用说明"""
        yield event.plain_result(HELP_TEXT)

    # ==================================================================
    # 自然语言入口：不用记指令，直接说人话
    # ==================================================================
    #: 意图 → 处理函数名。自然语言识别出意图后，直接复用对应的指令实现，
    #: 保证「说人话」与「打指令」两条路径的行为完全一致。
    NLU_DISPATCH: dict[str, str] = {
        "help": "d2_help",
        "bind": "d2_bind",
        "unbind": "d2_unbind",
        "my": "d2_my",
        "bindings": "d2_bindings",
        "info": "d2_info",
        "heroes": "d2_heroes",
        "matches": "d2_matches",
        "analyze": "d2_analyze",
        "match": "d2_match",
        "forceparse": "d2_askparse",
        "watch": "d2_watch",
        "unwatch": "d2_unwatch",
        "watchlist": "d2_watchlist",
        "llmtest": "d2_llmtest",
        "datasource": "d2_datasource",
    }

    #: 单场复盘的指令别名 → handler。
    #: AstrBot 本身负责事件分发，这里保留一份是为了让测试与内部调用
    #: 能用和用户完全相同的入口（`/d2 单场` / `/d2 match` / `/d2 复盘单场`）。
    MATCH_COMMANDS: dict[str, str] = {
        "match": "d2_match",
        "单场": "d2_match",
        "detail": "d2_match",
        "复盘单场": "d2_match",
    }

    def _nlu_invoke(self, name: str, event: AstrMessageEvent, args: str):
        """按目标 handler 的真实签名决定要不要传参数。

        指令 handler 有两类：带 ``args`` 的（查询 / 绑定 / 监听）和不带
        参数的（帮助 / 我的 / 绑定列表 / 监听列表）。自然语言入口不该
        关心这个差别，这里统一处理。
        """
        handler = getattr(self, self.NLU_DISPATCH.get(name, ""), None)
        if handler is None:
            return None
        func = getattr(handler, "__wrapped__", handler)
        try:
            sig = inspect.signature(func)
            takes_args = len(sig.parameters) >= 3
        except (TypeError, ValueError):  # pragma: no cover - 理论不会发生
            takes_args = True
        if takes_args:
            return handler(event, args)
        return handler(event)

    def _nlu_keyword(self) -> str:
        """当前生效的唤醒词（配置留空则回落到默认值）。"""
        raw = self.cfg("nlu_keyword", NLU_DEFAULT_KEYWORD)
        text = str(raw or "").strip()
        return text or NLU_DEFAULT_KEYWORD

    def _nlu_head_text(self, text: str) -> str:
        """剥离唤醒词后的正文；没写唤醒词时**原样返回**。

        确认 / 取消这类短回复既可能裸写（「确认」），也可能带着唤醒词
        （「dota2助手 确认」），用这个函数统一成同一种形态再比较。
        """
        _matched, rest = dota_nlu.strip_wake_keyword(text, self._nlu_keyword())
        return rest

    def _nlu_should_handle(self, event: AstrMessageEvent, text: str) -> NluGate | None:
        """判断这条消息要不要交给自然语言入口处理。

        返回 :class:`NluGate`（含**真正送去解析的文本**与「是否命中唤醒词」），
        返回 ``None`` 表示不处理。

        自然语言入口是 ``EventMessageType.ALL`` 上的全局监听，不设闸门就会
        抢答别的插件（以及群里其他人的闲聊）的对话。闸门依次是：

        1. 总开关 ``nlu_enabled``；
        2. 指令类消息（``/`` 开头等）让给命令 handler；
        3. 唤醒词：默认要求消息里出现「dota2助手」，命中即剥掉再解析；
        4. 群聊里没写唤醒词时，仍要求 @ 机器人（``nlu_group_require_at``）。

        第 3 与第 4 步的关系：**唤醒词本身就是一次明确点名**，命中它就等于
        已经 @ 过机器人，因此不再重复要求 @，避免「又写唤醒词又 @」的双重
        门槛。反过来，只有 @ 而没写唤醒词的消息会被第 3 步挡掉。

        注意返回值里的 ``keyword_matched``：只有它为真时，调用方才允许
        走「闲聊兜底」（没识别出指令时由插件带数据回答）。关掉唤醒词
        限制（``nlu_require_keyword=false``）时它恒为假，插件就不会去
        抢答任何一条闲聊 —— 这是**故意的**安全一侧。
        """
        if not self.cfg("nlu_enabled", True):
            return None
        text = (text or "").strip()
        if not text:
            return None
        # 指令类消息交给命令 handler，这里不截胡
        if text.startswith(NLU_SKIP_PREFIXES):
            return None

        # ---- 唤醒词闸门 ----
        keyword_matched = False
        effective = text
        if self.cfg("nlu_require_keyword", True):
            keyword_matched, effective = dota_nlu.strip_wake_keyword(
                text, self._nlu_keyword()
            )
            if not keyword_matched:
                return None
            if not effective:
                # 只发了个唤醒词、没说要求什么：不猜，留给默认大模型
                return None
            # 剥掉唤醒词后如果露出的是别的插件/本插件的指令（「dota2助手 /help」），
            # 同样不截胡 —— 那条消息该由对应的命令 handler 处理。
            if effective.startswith(dota_nlu.NLU_STRIP_CMD_PREFIXES):
                return None

        # 群聊 / 私聊判断：用消息类型与 umo 双重判断，兼容各适配器。
        # get_message_type 在少数自定义 Event 上可能不存在，因此做了容错。
        umo = str(event.unified_msg_origin)
        try:
            message_type = str(event.get_message_type())
        except Exception:  # noqa: BLE001
            message_type = ""
        is_group = "GROUP_MESSAGE" in message_type or "GroupMessage" in umo
        if is_group and self.cfg("nlu_group_require_at", True) and not keyword_matched:
            # 群里必须 @ 机器人（或使用唤醒前缀，此时 message_str 里已带前缀）。
            # 这条限制能挡掉绝大多数「群里别人随口一说就被插件抢答」的情况。
            try:
                if not event.is_at_or_wake_command:
                    return None
            except AttributeError:
                return None
        return NluGate(text=effective, keyword_matched=keyword_matched)

    async def _nlu_classify_with_llm(
        self, event: AstrMessageEvent, text: str
    ) -> dota_nlu.Intent | None:
        """规则没把握时，可选地用大模型兜底分类一次。"""
        provider = await resolve_provider(
            self.context, event.unified_msg_origin, self.cfg("llm_provider_id", "")
        )
        if not provider:
            return None
        prompt = dota_nlu.build_classifier_prompt(text)
        try:
            reply = await call_llm(provider, dota_nlu.CLASSIFIER_SYSTEM_PROMPT, prompt)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[dota2] 自然语言分类调用失败: {e}")
            return None
        return dota_nlu.parse_classifier_reply(reply)

    async def _nlu_chat_reply(self, event: AstrMessageEvent, question: str):
        """闲聊兜底：没识别出指令时，带着插件内部数据让模型回答。

        只在「命中唤醒词」之后才会走到这里（见 :meth:`_nlu_should_handle`
        的返回值），因为唤醒词就是用户明确点名了插件。

        典型场景：

        * 「dota2助手 对比一下目前监听的几个人谁最菜」
          —— 需要监听列表 + 每个人的近期战绩；
        * 「dota2助手 我要转辅助该怎么练」
          —— 需要提问者自己的英雄池 + 近期表现。

        失败语义（很重要）：

        * **数据收集失败不算失败**。少拉一块上下文照样能回答，最多在
          上下文里注明「某人数据没取到」。
        * **模型不可用才算失败**。此时一条结果都不产出，由
          ``@take_over_event(declinable=True)`` 原样放行，消息会正常落到
          AstrBot 的默认大模型手里 —— 绝不能既不回答、又把消息吃掉。
        """
        # 用户明确关闭了「启用 LLM 分析」：不要偷偷替他调用模型
        if not self.cfg("enable_llm_analysis", True):
            return

        umo = event.unified_msg_origin
        uid = str(event.get_sender_id())
        binding, _note = self._effective_binding(event)
        try:
            context = await dota_chat.collect_chat_context(
                self.api,
                question=question,
                umo=umo,
                user_id=uid,
                watchers=self.store.list_watchers(umo),
                bindings=list(self.store.list_bindings(umo).values()),
                self_binding=binding,
                recent_limit=int(
                    self.cfg("nlu_chat_context_matches", dota_chat.DEFAULT_RECENT_LIMIT)
                ),
                max_players=int(
                    self.cfg("nlu_chat_max_players", dota_chat.DEFAULT_MAX_PLAYERS)
                ),
                timeout=float(
                    self.cfg("nlu_chat_timeout", dota_chat.DEFAULT_FETCH_TIMEOUT)
                ),
                cache=self._chat_cache,
            )
        except Exception as e:  # noqa: BLE001 - 兜底失败也要放行，不能吞消息
            logger.error(f"[dota2] 闲聊兜底收集数据失败: {e}", exc_info=True)
            return

        prompt = dota_chat.build_chat_prompt(question, context)
        system_prompt = dota_chat.build_chat_system_prompt(
            str(self.cfg("nlu_chat_system_prompt", "") or "")
        )
        reply = await self._call_report_llm(
            prompt, umo=umo, system_prompt=system_prompt
        )
        if not reply:
            logger.info("[dota2] 闲聊兜底：模型不可用，消息交回默认大模型")
            return

        logger.info(
            f"[dota2] 闲聊兜底回答: {question[:48]!r} "
            f"needs={sorted(context.needs)} "
            f"玩家数={len(context.snapshots)}"
        )
        async for item in self._emit(event, reply, as_image=False):
            yield item

    def _nlu_pop_confirm(
        self, umo: str, uid: str
    ) -> tuple[str, str, float] | None:
        """取出并清理某个用户的待确认项（过期的自动丢弃）。"""
        key = (umo, uid)
        item = self._nlu_confirm.pop(key, None)
        if not item:
            return None
        if item[2] < time.time():
            return None
        return item

    @filter.event_message_type(filter.EventMessageType.ALL)
    @take_over_event(declinable=True)
    async def d2_natural(self, event: AstrMessageEvent):
        """自然语言入口：把「帮我看看我的战绩」这类人话转成对应功能。

        .. important::

           这是挂在 ``EventMessageType.ALL`` 上的**全局监听**：每条消息
           都会进来跑一遍，其中绝大多数会被闸门挡下并 ``return``。
           因此必须用 ``@take_over_event(declinable=True)`` —— 只有真的
           处理了消息才终止传播。改成无条件的 ``take_over_event`` 会让
           插件一开启就掐掉同一事件上**其他插件的 handler 和默认大模型**
           （AstrBot 的 ``StarRequestSubStage`` 遇到 ``is_stopped()``
           会直接 ``break``），表现为「正常对话和其他插件全都没反应」。
        """
        text = dota_nlu.normalize(getattr(event, "message_str", "") or "")
        umo = event.unified_msg_origin
        uid = str(event.get_sender_id())

        # ---------- 1. 先处理「确认 / 取消」回复 ----------
        # 这一步**不受唤醒词限制**：用户的确认是对上一轮已授权操作的收尾，
        # 再逼他打一遍「dota2助手 确认」是没必要的摩擦。为了两种写法都能用，
        # 统一拿剥离唤醒词后的正文来比对。
        pending = self._nlu_confirm.get((umo, uid))
        if pending is not None:
            head = self._nlu_head_text(text)
            if pending[2] < time.time():
                self._nlu_confirm.pop((umo, uid), None)
            elif head in NLU_CONFIRM_WORDS:
                name, args, _ = self._nlu_pop_confirm(umo, uid)
                agen = self._nlu_invoke(name, event, args)
                if agen is not None:
                    logger.info(f"[dota2] 自然语言确认执行: {name} {args!r}")
                    async for item in agen:
                        yield item
                return
            elif head in NLU_CANCEL_WORDS:
                self._nlu_pop_confirm(umo, uid)
                yield event.plain_result("好的，已取消。")
                return

        # ---------- 2. 该不该处理这条消息 ----------
        # 返回的是剥离唤醒词后的正文（没通过闸门时为 None）
        gate = self._nlu_should_handle(event, text)
        if gate is None:
            return
        effective = gate.text

        # ---------- 3. 识别意图 ----------
        intent = dota_nlu.parse(effective)
        if intent is None and self.cfg("nlu_llm_fallback", False):
            intent = await self._nlu_classify_with_llm(event, effective)
        if intent is None:
            # 没识别出内置指令。分两种情况：
            #
            # a) 用户写了唤醒词 —— 这是**明确点名**插件。此时直接把消息
            #    扔回默认大模型是浪费：默认大模型看不到本会话的监听列表、
            #    绑定关系与战绩数据，只能反问或编造。改为由插件带着这些
            #    数据回答（「闲聊兜底」）。
            # b) 没写唤醒词（仅 @，且配置允许）—— 说明只是随口一提，
            #    原样放行，别抢答。
            #
            # 注意 keyword_matched 在 `nlu_require_keyword=false` 时恒为假，
            # 所以关掉唤醒词限制不会让插件变成「什么都插一嘴」。
            if gate.keyword_matched and self.cfg("nlu_chat_fallback", True):
                async for item in self._nlu_chat_reply(event, effective):
                    yield item
            return

        handler_name = self.NLU_DISPATCH.get(intent.name)
        if not handler_name:
            return
        # 缺参数的意图直接告诉用户怎么补，别去猜
        if intent.name in dota_nlu.INTENT_NEEDS_TARGET and not intent.args:
            if intent.name == "unwatch":
                pass  # 取消监听允许不带目标（按当前绑定来）
            elif intent.name == "match":
                yield event.plain_result(
                    "复盘单场需要比赛 ID，例如："
                    f"`{self._nlu_keyword()} 这局 8993438099 帮我复盘一下`。\n"
                    "比赛 ID 可以从「我的战绩」里拿，或直接用 Dota 客户端的比赛编号。"
                )
                return
            elif intent.name == "forceparse":
                yield event.plain_result(
                    "催解析需要指定是哪一局，例如："
                    f"`{self._nlu_keyword()} 催一下 8993438099 的解析`。\n"
                    "想让插件等解析完再自动出复盘，"
                    f"说「{self._nlu_keyword()} 这局 8993438099 复盘一下」即可。"
                )
                return
            elif intent.name in {"bind", "info", "heroes", "matches", "analyze", "watch"}:
                # 无法确定是谁：交给 handler，它会回落到当前会话的绑定
                pass

        handler = getattr(self, handler_name, None)
        if handler is None:
            return

        # ---------- 4. 多义意图先确认，避免群里误触 ----------
        if (
            intent.name in NLU_CONFIRM_INTENTS
            and intent.args
            and self.cfg("nlu_confirm_sensitive", True)
        ):
            self._nlu_confirm[(umo, uid)] = (
                intent.name,
                intent.args,
                time.time() + NLU_CONFIRM_TTL,
            )
            verb = {
                "bind": "绑定账号",
                "unbind": "解除绑定",
                "watch": "添加监听",
            }.get(intent.name, intent.name)
            yield event.plain_result(
                f"你刚才是想让我{verb}「{intent.args}」吗？\n"
                f"回复「确认」我就执行；回复「取消」就当我没说。"
            )
            return

        logger.info(
            f"[dota2] 自然语言识别: {intent.name} args={intent.args!r} "
            f"score={intent.score} via={intent.via}"
        )
        agen = self._nlu_invoke(intent.name, event, intent.args)
        if agen is None:
            return
        async for item in agen:
            yield item

    # ==================================================================
    # 指令：绑定 / 解绑
    # ==================================================================
    @d2.command("bind", alias={"绑定", "bd"})
    @take_over_event
    async def d2_bind(self, event: AstrMessageEvent, args: GreedyStr):
        """绑定 Dota2 玩家账号：/d2 绑定 <昵称|32位ID|64位SteamID>"""
        target = str(args).strip()
        if not target:
            yield event.plain_result(
                "用法：`/d2 绑定 <昵称 | 32位账号ID | 64位SteamID>`\n"
                "例如：`/d2 绑定 天鸽` 或 `/d2 绑定 86745912` 或 "
                "`/d2 绑定 76561198047011640`"
            )
            return

        try:
            account_id, name = await self._resolve_account(target)
        except OpenDotaError as e:
            yield event.plain_result(f"❌ 解析失败：{e}")
            return

        umo = event.unified_msg_origin
        uid = str(event.get_sender_id())
        await self.store.set_binding(
            umo, uid, account_id, name, to_steam_id64(account_id)
        )
        yield event.plain_result(
            f"✅ 绑定成功\n"
            f"玩家：{name}\n"
            f"account_id：{account_id}\n"
            f"SteamID64：{to_steam_id64(account_id)}\n\n"
            f"之后直接发送 `/d2 战绩`、`/d2 分析` 即可查询。"
        )

    @d2.command("unbind", alias={"解绑", "ub"})
    @take_over_event
    async def d2_unbind(self, event: AstrMessageEvent):
        """解除当前会话的 Dota2 账号绑定"""
        umo = event.unified_msg_origin
        uid = str(event.get_sender_id())
        binding = self.store.get_binding(umo, uid)
        if not binding:
            yield event.plain_result("你当前没有绑定任何账号。")
            return

        await self.store.remove_binding(umo, uid)

        # 顺带清理与这个账号相关的监听
        removed_watch = 0
        account_id = int(binding.get("account_id") or 0)
        for watcher in list(self.store.list_watchers(umo)):
            if int(watcher.get("account_id") or 0) != account_id:
                continue
            if await self.store.remove_watcher(watcher.get("id")):
                removed_watch += 1

        message = f"✅ 已解除绑定：{binding.get('personaname')}（{account_id}）"
        if removed_watch:
            message += f"\n同时移除了本会话针对该账号的 {removed_watch} 项监听。"
        yield event.plain_result(message)

    @d2.command("my", alias={"我的", "me", "当前绑定"})
    @take_over_event
    async def d2_my(self, event: AstrMessageEvent):
        """查看自己在当前会话的绑定"""
        umo = event.unified_msg_origin
        uid = str(event.get_sender_id())
        binding = self.store.get_binding(umo, uid)
        if not binding:
            yield event.plain_result(self._no_binding_reply())
            return

        account_id = int(binding.get("account_id") or 0)
        watched = self.store.find_watcher(umo, account_id) is not None
        yield event.plain_result(
            f"👤 当前绑定\n"
            f"玩家：{binding.get('personaname')}\n"
            f"account_id：{account_id}\n"
            f"SteamID64：{binding.get('steam_id') or to_steam_id64(account_id)}\n"
            f"绑定时间：{fmt_ago(binding.get('bound_at'))}\n"
            f"本会话监听：{'✅ 已开启' if watched else '❌ 未开启'}"
        )

    @d2.command("bindings", alias={"绑定列表", "列表", "blist"})
    @take_over_event
    async def d2_bindings(self, event: AstrMessageEvent):
        """查看本会话中所有成员绑定的账号"""
        umo = event.unified_msg_origin
        viewer = str(event.get_sender_id())
        bindings = self.store.list_bindings(umo)
        if not bindings:
            yield event.plain_result("本会话还没有任何绑定。")
            return

        lines = [f"📋 本会话共有 {len(bindings)} 个绑定：", ""]
        for index, (user_id, binding) in enumerate(bindings.items(), start=1):
            is_self = str(user_id) == viewer
            account_id = int(binding.get("account_id") or 0)
            watched = self._is_watched(umo, account_id)
            who = self._display_id(user_id, is_self)
            account = self._display_id(binding.get("account_id"), is_self)
            lines.append(
                f"{index}. {binding.get('personaname')}　"
                f"账号 {account}　用户 {who}"
                + ("　← 你" if is_self else "")
                + ("　🔔监听中" if watched else "")
            )
        if any(str(uid) != viewer for uid in bindings):
            lines.append("")
            lines.append("（为保护隐私，他人的账号与用户 ID 默认打码）")
        yield event.plain_result("\n".join(lines))

    def _is_watched(self, umo: str, account_id: int) -> bool:
        return self.store.find_watcher(umo, account_id) is not None

    @staticmethod
    def _mask_id(value: Any) -> str:
        """把 ID 的中段打码，用于在群聊里展示**他人**的账号信息。

        群聊是半公开场合，直接把成员的 QQ 号与 Steam / Dota 账号 ID 打出来
        会让任何人都能顺藤摸瓜查到别人的 Steam 主页，因此默认只展示首尾。
        """
        text = str(value if value is not None else "").strip()
        if not text:
            return "未知"
        if len(text) <= 4:
            return "*" * len(text)
        head = text[:4] if len(text) >= 8 else text[:2]
        tail = text[-2:]
        return f"{head}{'*' * max(2, len(text) - len(head) - len(tail))}{tail}"

    def _display_id(self, value: Any, is_self: bool) -> str:
        """按「是否本人」决定展示完整 ID 还是打码。"""
        if is_self or not self.cfg("mask_others_id", True):
            return str(value if value is not None else "")
        return self._mask_id(value)

    def _is_admin(self, user_id: str, event: AstrMessageEvent | None = None) -> bool:
        """判断该用户是否具备「管理他人监听」的权限。

        判定顺序：

        1. 配置项 ``admin_users`` 中列出的用户；
        2. AstrBot 自身的管理员（若当前版本的事件对象提供了 ``is_admin()``）。

        管理员可以取消本会话中**他人**添加的监听；普通成员只能管理自己的。
        两项都不满足时一律按普通成员处理，这是安全的一侧。
        """
        raw = str(self.cfg("admin_users", "") or "")
        admins = {
            item.strip() for item in re.split(r"[,，\s]+", raw) if item.strip()
        }
        if admins and str(user_id) in admins:
            return True

        if event is not None:
            checker = getattr(event, "is_admin", None)
            if callable(checker):
                try:
                    if checker():
                        return True
                except Exception:  # noqa: BLE001
                    # 不同版本的 AstrBot 签名可能不同，失败时按非管理员处理
                    pass
        return False

    # ==================================================================
    # 指令：玩家资料 / 英雄统计
    # ==================================================================
    @d2.command("info", alias={"资料", "player", "玩家"})
    @take_over_event
    async def d2_info(self, event: AstrMessageEvent, args: GreedyStr):
        """查看玩家资料：/d2 资料 [昵称|账号ID]"""
        target = str(args).strip()
        if target:
            try:
                account_id, name = await self._resolve_account(target)
            except OpenDotaError as e:
                yield event.plain_result(f"❌ {e}")
                return
        else:
            binding, _ = self._effective_binding(event)
            if not binding:
                yield event.plain_result(self._no_binding_reply())
                return
            account_id = int(binding.get("account_id") or 0)
            name = binding.get("personaname") or f"账号{account_id}"

        try:
            player_data = await self.api.get_player(account_id)
            if not player_data:
                yield event.plain_result(f"❌ OpenDota 查不到账号 {account_id}。")
                return
            wl = await self.api.get_player_wl(account_id)
            hero_rows = await self.api.get_player_heroes(account_id)
            heroes = await self.api.get_heroes()
        except OpenDotaError as e:
            yield event.plain_result(f"❌ 查询失败：{e}")
            return

        yield event.plain_result(
            format_player_profile(player_data, wl, hero_rows, heroes)
        )

    @d2.command("heroes", alias={"英雄", "hero", "英雄池"})
    @take_over_event
    async def d2_heroes(self, event: AstrMessageEvent, args: GreedyStr):
        """查看英雄使用统计：/d2 英雄 [昵称|账号ID]"""
        target = str(args).strip()
        if target:
            try:
                account_id, name = await self._resolve_account(target)
            except OpenDotaError as e:
                yield event.plain_result(f"❌ {e}")
                return
        else:
            binding, _ = self._effective_binding(event)
            if not binding:
                yield event.plain_result(self._no_binding_reply())
                return
            account_id = int(binding.get("account_id") or 0)
            name = binding.get("personaname") or f"账号{account_id}"

        try:
            hero_rows = await self.api.get_player_heroes(account_id)
            heroes = await self.api.get_heroes()
        except OpenDotaError as e:
            yield event.plain_result(f"❌ 查询失败：{e}")
            return

        text = format_hero_stats(name, hero_rows, heroes)
        for chunk in self._chunk_text(text):
            yield event.plain_result(chunk)

    # ==================================================================
    # 指令：战绩
    # ==================================================================
    @d2.command("matches", alias={"战绩", "rec", "比赛"})
    @take_over_event
    async def d2_matches(self, event: AstrMessageEvent, args: GreedyStr):
        """查看最近战绩：/d2 战绩 [场次] [昵称|账号ID]"""
        count, target = self._split_count_target(str(args))

        if target:
            try:
                account_id, name = await self._resolve_account(target)
            except OpenDotaError as e:
                yield event.plain_result(f"❌ {e}")
                return
            note = ""
        else:
            binding, note = self._effective_binding(event)
            if not binding:
                yield event.plain_result(self._no_binding_reply())
                return
            account_id = int(binding.get("account_id") or 0)
            name = binding.get("personaname") or f"账号{account_id}"

        limit = self._clamp_count(count)
        try:
            matches, _ = await self.api.get_player_matches_enriched(account_id, limit)
            heroes = await self.api.get_heroes()
        except OpenDotaError as e:
            yield event.plain_result(f"❌ 查询失败：{e}")
            return

        if not matches:
            yield event.plain_result(f"没有查询到 {name} 的比赛记录。可能该账号未公开比赛数据。")
            return
        title = f"📊 {name} 的最近 {len(matches)} 场比赛"
        if note:
            title += note
        text = format_match_list(name, account_id, matches, heroes, title=title)
        for chunk in self._chunk_text(text):
            yield event.plain_result(chunk)

    def _clamp_count(self, count: int | None) -> int:
        """把用户指定的场次限制在合理范围内。"""
        default = max(1, int(self.cfg("default_match_count", 20)))
        maximum = max(1, int(self.cfg("max_match_count", 50)))
        if count is None:
            return min(default, maximum)
        return max(1, min(int(count), maximum))

    # ==================================================================
    # 指令：分析近期表现
    # ==================================================================
    @d2.command("analyze", alias={"分析", "recent", "复盘"})
    @take_over_event
    async def d2_analyze(self, event: AstrMessageEvent, args: GreedyStr):
        """AI 分析近期表现与打法风格：/d2 分析 [场次] [昵称|账号ID]"""
        count, target = self._split_count_target(str(args))

        if target:
            try:
                account_id, name = await self._resolve_account(target)
            except OpenDotaError as e:
                yield event.plain_result(f"❌ {e}")
                return
            note = ""
        else:
            binding, note = self._effective_binding(event)
            if not binding:
                yield event.plain_result(self._no_binding_reply())
                return
            account_id = int(binding.get("account_id") or 0)
            name = binding.get("personaname") or f"账号{account_id}"

        limit = self._clamp_count(count)
        yield event.plain_result(f"⏳ 正在拉取 {name} 最近 {limit} 场数据并生成分析，请稍候…")

        try:
            matches, economy_samples = await self.api.get_player_matches_enriched(
                account_id, limit
            )
            heroes = await self.api.get_heroes()
            try:
                player_data = await self.api.get_player(account_id) or {}
                wl = await self.api.get_player_wl(account_id)
                hero_rows = await self.api.get_player_heroes(account_id)
            except OpenDotaError as e:
                logger.warning(f"[dota2] 补充玩家资料失败: {e}")
                player_data, wl, hero_rows = {}, {}, []
        except OpenDotaError as e:
            yield event.plain_result(f"❌ 拉取数据失败：{e}")
            return

        if not matches:
            yield event.plain_result(f"没有查询到 {name} 的比赛记录。")
            return

        prompt = build_recent_analysis_prompt(
            account_id=account_id,
            profile_data=player_data,
            wl=wl,
            matches=matches,
            heroes=heroes,
            hero_rows=hero_rows,
            economy_samples=economy_samples,
            requested_count=limit,
        )

        report = await self._generate_report(event, prompt)
        header = (
            f"🔍 {name} 近期表现分析（样本 {len(matches)} 场"
            f"{'，含全场次' if economy_samples >= len(matches) else f'，其中 {economy_samples} 场含经济数据'}）"
            f"{note}"
        )
        if report:
            yield event.plain_result(header)
            async for item in self._emit(event, report, as_image=True):
                yield item
        else:
            summary = summarize_matches(matches, economy_samples=economy_samples)
            fallback = (
                f"{header}\n\n"
                "⚠️ 未启用大模型分析或模型不可用，以下为原始统计数据：\n\n"
                + format_summary_block(summary, heroes)
                + "\n\n"
                + format_match_list(name, account_id, matches, heroes, title="逐场明细")
            )
            for chunk in self._chunk_text(fallback):
                yield event.plain_result(chunk)

    # ==================================================================
    # 指令：单场复盘
    # ==================================================================
    @d2.command("match", alias={"单场", "detail", "复盘单场"})
    @take_over_event
    async def d2_match(self, event: AstrMessageEvent, args: GreedyStr):
        """AI 深度复盘单场比赛：/d2 单场 <比赛ID> [焦点玩家]

        未指定 skip 时，若该局尚未解析完成，会自动催解析并每分钟检查一次，
        最多等 10 分钟（解析完成后再出复盘）。
        """
        tokens = [token for token in re.split(r"\s+", str(args).strip()) if token]
        if not tokens or not re.fullmatch(r"\d{6,20}", tokens[0]):
            yield event.plain_result(
                "用法：`/d2 单场 <比赛ID> [焦点玩家] [skip]`\n"
                "例如：`/d2 单场 8989601141`\n"
                "多位焦点：`/d2 单场 8989601141 张三、李四、王五`"
                "（昵称或账号 ID，用 、/, 分隔；昵称可以含空格）\n"
                "不想等待解析：`/d2 单场 8989601141 skip`\n"
                "比赛 ID 可以从 `/d2 战绩` 的结果中获取，或直接使用 Dota 客户端的比赛编号。"
            )
            return

        match_id = int(tokens[0])
        rest_tokens = tokens[1:]
        # `/d2 单场 <id> skip`：跳过「等解析」，直接用现有数据出报告
        skip_wait = any(t.lower() in PARSE_SKIP_WORDS for t in rest_tokens)
        rest = " ".join(t for t in rest_tokens if t.lower() not in PARSE_SKIP_WORDS)

        yield event.plain_result(
            f"⏳ 正在拉取比赛 {match_id} 的详细数据并生成复盘，请稍候…"
        )

        try:
            match = await self.api.get_match(match_id)
            if not match:
                # 走到这里说明**两端都问过了**（降级器会在主源返回空时补问
                # 后备），所以可以放心说「查不到」，不必再单点 OpenDota。
                yield event.plain_result(
                    f"❌ 查不到比赛 {match_id}：STRATZ 与 OpenDota 都没有这盘的数据。\n"
                    "可能原因：比赛 ID 写错、该局刚结束还没被收录（可过几分钟再试）、"
                    "或对局方未公开比赛数据。\n"
                    f"如果是刚打完的局，可以用 `{self._nlu_keyword()} 催一下 "
                    f"{match_id} 的解析` 试一次。"
                )
                return
            heroes = await self.api.get_heroes()
            items = await self.api.get_items()
        except OpenDotaError as e:
            yield event.plain_result(f"❌ 拉取比赛数据失败：{e}")
            return

        # 焦点解析放在拉取之后：本局十人名单是天然的昵称消歧器，
        # 「大魔导师马化腾」这类重名昵称只有在局内唯一时才能被解析出来。
        players_in_match = [
            player
            for player in (match.get("players") or [])
            if isinstance(player, dict)
        ]

        focus_ids: list[int] = []
        focus_names: dict[int, str] = {}
        failed: list[str] = []

        if rest:
            for query in (q.strip() for q in FOCUS_SPLIT_RE.split(rest)):
                if not query:
                    continue
                try:
                    account_id, name = await self._resolve_account(
                        query, in_match_players=players_in_match
                    )
                except OpenDotaError as e:
                    failed.append(f"{query}（{e}）")
                    continue
                account_id = int(account_id or 0)
                if account_id and account_id not in focus_ids:
                    focus_ids.append(account_id)
                    focus_names[account_id] = name
            if failed and not focus_ids:
                yield event.plain_result(
                    "❌ 焦点玩家解析失败：\n"
                    + "\n".join(f"· {item}" for item in failed)
                    + "\n建议直接使用 32 位账号 ID 或 64 位 SteamID，结果更准确。"
                )
                return

        if failed:
            yield event.plain_result(
                "⚠️ 以下焦点未能解析，已跳过：" + "、".join(failed)
            )

        # 未指定焦点玩家时，若本会话的绑定出现在这场比赛中，则自动聚焦
        if not focus_ids:
            binding, _ = self._effective_binding(event)
            if binding:
                candidate = int(binding.get("account_id") or 0)
                if candidate and any(
                    isinstance(player, dict)
                    and player.get("account_id") == candidate
                    for player in match.get("players") or []
                ):
                    focus_ids.append(candidate)
                    focus_names[candidate] = binding.get("personaname") or ""

        parse_state = dota_parse.parse_state(match)
        parsed = parse_state.parsed

        # ---------- 未解析：催解析 + 等待，改由后台任务完成复盘 ----------
        if not parsed and not skip_wait and self.cfg("parse_wait_enabled", True):
            options = self._parse_wait_options()
            minutes = max(1, round(int(options["timeout"]) / 60))
            seconds = int(options["check_interval"])
            if self._start_parse_task(
                event, match_id, focus_ids, focus_names, fresh=False
            ):
                yield event.plain_result(
                    f"🔍 比赛 {match_id} 数据完整度：{parse_state.describe()}\n"
                    f"AI 复盘依赖逐分钟经济、团战与出装数据，"
                    f"{'已提交催解析并' if self.cfg('parse_submit_request', True) else ''}"
                    f"开始等待：每 {_fmt_clock(seconds)} 检查一次，"
                    f"最多等 {minutes} 分钟。\n"
                    f"解析完成后会自动把复盘报告发到本会话，你可以先去忙别的。\n"
                    f"（不想等待：`/d2 单场 {match_id} skip` 直接用基础数据出报告）"
                )
                return
            yield event.plain_result(
                "⚠️ 当前等待解析的任务过多，无法排队。"
                f"已改用基础数据（{parse_state.describe()}）生成复盘。\n"
                f"稍后可用 `/d2 单场 {match_id}` 重新尝试等待解析。"
            )

        headline = self.match_headline(match, heroes, focus_ids or None, parsed)

        # 附上焦点玩家近期的整体状态，让复盘更有上下文。
        # 焦点可能不止一位，逐位取；超过上限的只做本场复盘，避免提示词被撑爆。
        extra_context = await self._build_recent_context(
            match_id, focus_ids, focus_names, heroes
        )
        abilities = await self._ability_constants()
        curve_ids = self._curve_targets(match, focus_ids)

        prompt = build_single_match_analysis_prompt(
            match=match,
            heroes=heroes,
            items=items,
            focus_account_ids=focus_ids or None,
            focus_names=focus_names or None,
            extra_context=extra_context,
            abilities=abilities,
            curve_ids=curve_ids,
        )

        report = await self._generate_report(event, prompt)
        if report:
            yield event.plain_result(headline)
            async for item in self._emit(event, report, as_image=True):
                yield item
        else:
            raw = (
                f"{headline}\n\n"
                "⚠️ 未启用大模型分析或模型不可用，以下为从数据源获取的完整原始数据：\n\n"
                + build_match_data_text(
                    match,
                    heroes,
                    items,
                    focus_account_ids=focus_ids or None,
                    abilities=abilities,
                    curve_ids=curve_ids,
                )
            )
            for chunk in self._chunk_text(raw):
                yield event.plain_result(chunk)

    # ==================================================================
    # 指令：催解析（只催不等，适合还没打算看复盘时先排上队）
    # ==================================================================
    @d2.command("parse", alias={"催解析", "申请解析", "催一下", "强制解析"})
    @take_over_event
    async def d2_askparse(self, event: AstrMessageEvent, args: GreedyStr):
        """只向 OpenDota 提交解析申请，不等待结果：/d2 催解析 <比赛ID>"""
        tokens = [token for token in re.split(r"\s+", str(args).strip()) if token]
        if not tokens or not re.fullmatch(r"\d{6,20}", tokens[0]):
            yield event.plain_result(
                "用法：`/d2 催解析 <比赛ID>`\n"
                "例如：`/d2 催解析 8995419388`\n\n"
                "作用：向 OpenDota 提交这局的存档解析申请（OpenDota 平时不一定会"
                "主动解析别人的对局，催一下才会去取录像）。\n"
                "· 只想催、不等结果 → 用本指令；\n"
                "· 想等解析完自动出复盘报告 → 用 `/d2 单场 <比赛ID>`。"
            )
            return

        match_id = int(tokens[0])
        yield event.plain_result(
            f"⏳ 正在检查比赛 {match_id} 的解析状态并提交申请…"
        )

        try:
            match = await self.api.get_match(match_id)
        except OpenDotaError as e:
            yield event.plain_result(f"❌ 拉取比赛数据失败：{e}")
            return

        if not match:
            yield event.plain_result(
                f"❌ 找不到比赛 {match_id}，或该比赛尚未被 OpenDota 收录。\n"
                f"未被收录时提交解析申请没有意义，"
                f"一般等几分钟到几十分钟 OpenDota 会自动收录。"
            )
            return

        state = dota_parse.parse_state(match)
        if state.parsed:
            yield event.plain_result(
                f"✅ 比赛 {match_id} 已经解析完成（{state.describe()}），无需催解析。\n"
                f"现在就可以用 `/d2 单场 {match_id}` 出复盘。"
            )
            return

        granted = await dota_parse.submit_parse(self.api, match_id)
        if granted:
            yield event.plain_result(
                f"📨 已向 OpenDota 提交比赛 {match_id} 的解析申请，任务已排队。\n"
                f"当前状态：{state.describe()}\n\n"
                f"解析通常需要几分钟到几十分钟。想拿到结果后自动出报告，"
                f"用 `/d2 单场 {match_id}`（会每分钟检查一次，最多等 10 分钟）。"
            )
        else:
            yield event.plain_result(
                f"⚠️ 解析申请没有返回排队凭据，可能原因：\n"
                f"· OpenDota 认为这局无法解析（临时对局 / 录像已过期）；\n"
                f"· 该局已在解析队列中（这时其实不用再催）；\n"
                f"· 请求被限流（配置里填写 OpenDota API Key 可以提升额度）。\n\n"
                f"当前状态：{state.describe()}\n"
                f"可以过几分钟再用 `/d2 单场 {match_id}` 试探一次。"
            )

    # ==================================================================
    # 指令：监听
    # ==================================================================
    @d2.command("watch", alias={"监听", "订阅"})
    @take_over_event
    async def d2_watch(self, event: AstrMessageEvent, args: GreedyStr):
        """监听玩家，比赛结束后自动推送分析：/d2 监听 [昵称|账号ID]"""
        target = str(args).strip()
        if target:
            try:
                account_id, name = await self._resolve_account(target)
            except OpenDotaError as e:
                yield event.plain_result(f"❌ {e}")
                return
        else:
            binding, _ = self._effective_binding(event)
            if not binding:
                yield event.plain_result(self._no_binding_reply())
                return
            account_id = int(binding.get("account_id") or 0)
            name = binding.get("personaname") or f"账号{account_id}"

        if not self.cfg("watch_enabled", True):
            yield event.plain_result(
                "⚠️ 当前插件配置中已关闭「启用比赛监听」，请在 AstrBot 管理面板中开启。"
            )
            return

        umo = event.unified_msg_origin

        # 每次新比赛都会消耗一次大模型调用，因此限制单会话的监听数量，
        # 防止群里任何人都能无限叠加监听把配额吃掉。
        if self.store.find_watcher(umo, account_id) is None:
            cap = max(1, int(self.cfg("watch_max_per_session", 5)))
            if len(self.store.list_watchers(umo)) >= cap:
                yield event.plain_result(
                    f"⚠️ 本会话的监听数量已达上限（{cap} 个）。\n"
                    f"请先用 `/d2 监听列表` 查看现有监听，"
                    f"并用 `/d2 取消监听 <账号|全部>` 清理后再添加。\n"
                    f"如需提高上限，请调整插件配置项 `watch_max_per_session`。"
                )
                return

        yield event.plain_result("⏳ 正在初始化监听…")

        try:
            recent = await self.api.get_player_matches(account_id, limit=1)
        except OpenDotaError as e:
            yield event.plain_result(f"❌ 初始化监听失败：{e}")
            return

        baseline = int(recent[0].get("match_id") or 0) if recent else 0
        watcher, created = await self.store.add_watcher(
            umo=event.unified_msg_origin,
            account_id=account_id,
            personaname=name,
            last_match_id=baseline,
            platform=event.get_platform_name(),
            created_by=str(event.get_sender_id()),
            created_by_name=event.get_sender_name(),
        )

        if not created:
            yield event.plain_result(
                f"ℹ️ 本会话已经在监听 {name}（{account_id}），无需重复添加。\n"
                f"当前基线比赛：{watcher.get('last_match_id')}"
            )
            return

        # 用户可能在运行期间才打开监听开关，这里做一次懒启动
        self._start_watcher()

        interval = max(60, int(self.cfg("watch_interval", 180)))
        platform_note = ""
        platform_label = PLATFORMS_WITHOUT_PROACTIVE_PUSH.get(
            str(event.get_platform_name() or "")
        )
        if platform_label:
            platform_note = (
                f"\n\n⚠️ 注意：当前平台（{platform_label}）不支持机器人主动发送消息，"
                f"比赛结束后可能无法自动推送到这里。\n"
                f"建议改用支持主动消息的平台，或改用 `/d2 单场 <比赛ID>` 手动复盘。"
            )

        yield event.plain_result(
            f"🔔 已开始监听 {name}（account_id: {account_id}）\n"
            f"· 推送目标：本会话\n"
            f"· 轮询间隔：{interval} 秒\n"
            f"· 已记录基线比赛：{baseline}（只有此后进行的新比赛才会推送）\n"
            f"· 数据源提供详细数据后，会自动生成 AI 分析并发送到这里\n"
            f"· 取消监听：`/d2 取消监听 {account_id}`"
            + platform_note
        )

    @d2.command("unwatch", alias={"取消监听", "取消订阅", "停止监听", "关闭监听", "取消关注"})
    @take_over_event
    async def d2_unwatch(self, event: AstrMessageEvent, args: GreedyStr):
        """取消监听：/d2 取消监听 <昵称|账号ID|全部>"""
        umo = event.unified_msg_origin
        target = str(args).strip()
        uid = str(event.get_sender_id())
        is_admin = self._is_admin(uid, event)

        if not target:
            yield event.plain_result(
                "用法：`/d2 取消监听 <昵称|账号ID|全部>`\n"
                "· `/d2 取消监听 86745912`\n"
                "· `/d2 取消监听 全部` 取消你添加的所有监听\n"
                "（也支持「停止监听」「关闭监听」等说法，与「取消监听」等价）"
            )
            return

        if target in ("全部", "all", "ALL", "所有"):
            watchers = self.store.list_watchers(umo)
            if not watchers:
                yield event.plain_result("本会话没有任何监听。")
                return

            # 普通成员只能取消自己添加的监听；管理员可以取消本会话的全部监听。
            mine = [w for w in watchers if str(w.get("created_by") or "") == uid]
            removable = watchers if is_admin else mine
            if not removable:
                yield event.plain_result(
                    "本会话的监听都不是你添加的，无法取消。\n"
                    "你只能取消自己添加的监听；如需清理他人的监听，"
                    "请联系插件管理员（配置项 `admin_users`）。"
                )
                return

            for watcher in removable:
                await self.store.remove_watcher(watcher.get("id"))
            if len(removable) == len(watchers):
                yield event.plain_result(f"✅ 已取消本会话全部 {len(removable)} 项监听。")
            else:
                yield event.plain_result(
                    f"✅ 已取消你添加的 {len(removable)} 项监听。\n"
                    f"（本会话另有 {len(watchers) - len(removable)} 项由他人添加，未受影响）"
                )
            return

        # 先按 ID 直接匹配，避免不必要的搜索请求
        account_id: int | None = None
        if re.fullmatch(r"\d{5,20}", target):
            account_id = to_account_id(target)
        if account_id is None:
            # 昵称：在本会话已有的监听里模糊匹配，匹配不到再走 OpenDota 搜索
            for watcher in self.store.list_watchers(umo):
                if target.lower() in str(watcher.get("personaname") or "").lower():
                    account_id = int(watcher.get("account_id") or 0)
                    break
        if account_id is None:
            try:
                account_id, _ = await self._resolve_account(target)
            except OpenDotaError as e:
                yield event.plain_result(f"❌ {e}")
                return

        watcher = self.store.find_watcher(umo, account_id)
        if not watcher:
            yield event.plain_result(f"本会话没有在监听账号 {account_id}。")
            return

        # 只有添加者本人或管理员才能取消，避免群友互删监听。
        owner = str(watcher.get("created_by") or "")
        if owner != uid and not is_admin:
            owner_label = watcher.get("created_by_name") or self._mask_id(owner)
            yield event.plain_result(
                f"这项监听是 {owner_label} 添加的，你无法取消。\n"
                f"如需取消，请联系本人或插件管理员（配置项 `admin_users`）。"
            )
            return

        await self.store.remove_watcher(watcher.get("id"))
        yield event.plain_result(
            f"✅ 已取消监听 {watcher.get('personaname')}（{account_id}）。"
        )

    @d2.command("watchlist", alias={"监听列表", "我的监听", "wlist"})
    @take_over_event
    async def d2_watchlist(self, event: AstrMessageEvent):
        """查看本会话的监听列表"""
        umo = event.unified_msg_origin
        watchers = self.store.list_watchers(umo)
        if not watchers:
            yield event.plain_result(
                "本会话还没有监听任何玩家。\n"
                "发送 `/d2 监听` 即可监听你自己绑定的账号。"
            )
            return

        viewer = str(event.get_sender_id())
        interval = max(60, int(self.cfg("watch_interval", 180)))
        lines = [
            f"🔔 本会话共监听 {len(watchers)} 个玩家（轮询间隔 {interval} 秒）",
            "",
        ]
        for index, watcher in enumerate(watchers, start=1):
            creator = str(watcher.get("created_by") or "")
            creator_label = (
                watcher.get("created_by_name")
                or self._display_id(creator, creator == viewer)
                or "未知"
            )
            if creator and creator == viewer:
                creator_label = f"{creator_label}（你）"
            lines.append(
                f"{index}. {watcher.get('personaname')}　"
                f"账号 {watcher.get('account_id')}\n"
                f"　　添加者：{creator_label}"
                f"　添加时间：{fmt_ago(watcher.get('created_at'))}\n"
                f"　　基线比赛：{watcher.get('last_match_id') or '未记录'}"
            )
        pending = list(self._pending.values())
        if pending:
            lines.append("")
            lines.append(f"⏳ 当前有 {len(pending)} 场比赛正在等待数据源收录：")
            for item in pending[:5]:
                focus_label = "、".join(
                    str(focus.get("name") or focus.get("account_id"))
                    for focus in item.get("focuses") or []
                )
                lines.append(
                    f"　　· 比赛 {item['match_id']}"
                    f"（焦点：{focus_label or '未知'}）"
                    f"　等收录 {item.get('wait_attempts', 0)} 次"
                )
        yield event.plain_result("\n".join(lines))

    # ==================================================================
    # 指令：模型自检
    # ==================================================================
    @d2.command("llmtest", alias={"模型测试", "模型自检", "测试模型", "llm"})
    @take_over_event
    async def d2_llmtest(self, event: AstrMessageEvent):
        """检查插件专用的模型 API Key 是否可用"""
        yield event.plain_result("⏳ 正在测试模型通道，请稍候…")
        yield event.plain_result(await self._llm_selftest(event.unified_msg_origin))

    async def _llm_selftest(self, umo: str) -> str:
        """用极短请求探一次模型通道，回显配置与耗时，便于用户排查。

        分三种情况：
        * 配了专用 Key → 只测专用通道，成功/失败都给细节；
        * 没配专用 Key → 说明当前回退 AstrBot 提供商，并实测一次回退通道；
        * 连回退通道都拿不到 → 提示去补配置。
        """
        if not self.cfg("enable_llm_analysis", True):
            return (
                "⚠️ AI 分析功能当前是关闭的（配置项「启用 AI 分析」）。\n"
                "把它打开后 `/d2 分析`、`/d2 单场` 才会有 AI 报告。"
            )

        client = self._dedicated_client()
        if client is None:
            return await self._llm_selftest_fallback(
                umo,
                "ℹ️ 你没有配置专用的模型 API Key，当前走 AstrBot 自带的模型提供商。\n"
                "如果想让本插件用独立的 Key（比如填 DeepSeek 的 key 省钱），"
                "在插件配置里填「专用 API Key」即可。\n\n",
            )

        configured = [
            f"　接口地址：{client.endpoint}",
            f"　模型名称：{client.model or '（未配置，会直接报错）'}",
            f"　超时：{client.timeout:g} 秒",
            f"　代理：{client.proxy or '不使用'}",
        ]
        if not client.model:
            return (
                "❌ 专用模型通道配置不完整：填了 API Key，但没填模型名称。\n"
                + "\n".join(configured)
                + "\n\n请在插件配置里补上「模型名称」，例如 `deepseek-chat`。"
            )

        started = time.monotonic()
        try:
            reply = await client.chat(
                "你是一个连通性测试助手，只回答用户要求的内容，不要添加任何多余的话。",
                "请只回复两个字：可用",
                temperature=0.0,
                max_tokens=32,
            )
        except LLMRequestError as e:
            elapsed = time.monotonic() - started
            return (
                f"❌ 专用模型通道测试失败（耗时 {elapsed:.1f} 秒）\n"
                + "\n".join(configured)
                + f"\n\n错误信息：\n{e}\n\n"
                "排查建议：\n"
                "· 401/403：API Key 填错或已失效；\n"
                "· 404：接口地址不对（中转站一般要到 `/v1` 为止，不要带 `/chat/completions`）；\n"
                "· 超时 / 无法连接：网络不通，或在墙外需要填「代理」；\n"
                "· 模型名不存在：确认模型名称与服务商匹配（如 `deepseek-chat`、`gpt-4o-mini`）。"
            )
        except Exception as e:  # noqa: BLE001
            elapsed = time.monotonic() - started
            logger.error(f"[dota2] 模型自检出现异常：{e}", exc_info=True)
            return (
                f"❌ 专用模型通道测试异常（耗时 {elapsed:.1f} 秒）\n"
                + "\n".join(configured)
                + f"\n\n异常信息：{e}"
            )

        elapsed = time.monotonic() - started
        return (
            f"✅ 专用模型通道可用（耗时 {elapsed:.1f} 秒）\n"
            + "\n".join(configured)
            + f"\n\n模型回显：{reply}"
        )

    async def _llm_selftest_fallback(self, umo: str, prefix: str) -> str:
        """在未配置专用 Key 时，实测一次 AstrBot 提供商通道。"""
        provider = await resolve_provider(
            self.context, umo, str(self.cfg("llm_provider_id", "") or "")
        )
        if provider is None:
            return (
                prefix
                + "❌ 但当前也没有可用的 AstrBot 模型提供商，`/d2 分析` 会拿不到报告。\n"
                "请先在 AstrBot 里配置一个模型提供商，或填上本插件的专用 API Key。"
            )
        started = time.monotonic()
        try:
            reply = await call_llm(
                provider,
                "你是一个连通性测试助手，只回答用户要求的内容，不要添加任何多余的话。",
                "请只回复两个字：可用",
            )
        except Exception as e:  # noqa: BLE001
            elapsed = time.monotonic() - started
            return (
                prefix
                + f"❌ 用 AstrBot 提供商测试失败（耗时 {elapsed:.1f} 秒）：{e}\n"
                "建议检查 AstrBot 里该提供商的配置，或改用插件专用 API Key。"
            )
        elapsed = time.monotonic() - started
        return (
            prefix
            + f"✅ AstrBot 提供商通道可用（耗时 {elapsed:.1f} 秒）\n"
            + f"　模型回显：{reply}"
        )

    # ==================================================================
    # 指令：数据源自检
    # ==================================================================
    @d2.command("datasource", alias={"数据源", "数据源测试", "源测试", "源"})
    @take_over_event
    async def d2_datasource(self, event: AstrMessageEvent):
        """检查主/后备数据源的连通性与降级状态"""
        yield event.plain_result("⏳ 正在测试数据源，请稍候…")
        yield event.plain_result(await self._data_source_selftest())

    async def _data_source_selftest(self) -> str:
        """实测主/后备数据源连通性，并回显降级状态。

        与「模型自检」对称：把「当前谁是主源、主源能不能用、有没有降级过」
        一次性讲清楚，省得用户翻日志。
        """
        api = self.api
        primary = getattr(api, "primary", None)
        secondary = getattr(api, "secondary", None)
        primary_label = getattr(api, "primary_label", "主数据源")
        secondary_label = getattr(api, "secondary_label", "后备数据源")

        lines = [f"📡 数据源状态（当前优先级：{primary_label} → {secondary_label}）", ""]

        # ---- 主数据源 ----
        if primary is None:
            lines.append(f"· {primary_label}：未接入")
        else:
            lines.append(f"· {primary_label}：{await self._probe_source(primary)}")

        # ---- 后备数据源 ----
        if secondary is None:
            lines.append(f"· {secondary_label}：未接入")
        else:
            lines.append(f"· {secondary_label}：{await self._probe_source(secondary)}")

        # ---- 降级记录 ----
        lines.append("")
        if getattr(api, "degraded", False):
            lines.append(
                "⚠️ 本进程内出现过降级：主数据源调用失败，已自动改走后备。\n"
                f"　最近原因：{getattr(api, 'last_error', '') or '未知'}"
            )
        else:
            lines.append("✅ 本次运行期间尚未发生降级。")

        # ---- STRATZ 专属提示 ----
        if primary_label == "STRATZ" and not getattr(primary, "configured", False):
            lines.append(
                "\nℹ️ 未配置「STRATZ API Key」，STRATZ 通道被跳过，"
                "所有查询直接使用 OpenDota。\n"
                "　去 https://stratz.com/api 登录后可生成令牌，填进插件配置即可启用。"
            )
        return "\n".join(lines)

    async def _probe_source(self, source: Any) -> str:
        """探测单个数据源的可用性，返回一行带耗时的描述。"""
        label = getattr(source, "label", None) or getattr(source, "name", "数据源")
        configured = getattr(source, "configured", None)
        if configured is False:
            return "未配置（跳过）"

        started = time.monotonic()
        try:
            heroes = await source.get_heroes()
        except OpenDotaError as e:
            elapsed = time.monotonic() - started
            return f"❌ 不可用（耗时 {elapsed:.1f} 秒）：{e}"
        except Exception as e:  # noqa: BLE001
            elapsed = time.monotonic() - started
            logger.error(f"[dota2] {label} 自检异常：{e}", exc_info=True)
            return f"❌ 异常（耗时 {elapsed:.1f} 秒）：{e}"

        elapsed = time.monotonic() - started
        count = len(heroes) if heroes else 0
        if count == 0:
            return f"⚠️ 连接成功但英雄表为空（耗时 {elapsed:.1f} 秒）"
        return f"✅ 可用（英雄 {count} 个，耗时 {elapsed:.1f} 秒）"

    # ==================================================================
    # 大模型调用
    # ==================================================================
    def _llm_api_key(self) -> str:
        """读取插件专用 API Key（空字符串表示未配置）。"""
        return str(self.cfg("llm_api_key", "") or "").strip()

    def _dedicated_client(self) -> OpenAICompatibleClient | None:
        """按配置构造专用模型客户端；未配置 key 时返回 None。"""
        api_key = self._llm_api_key()
        if not api_key:
            return None
        return OpenAICompatibleClient(
            api_key=api_key,
            base_url=str(self.cfg("llm_base_url", "") or ""),
            model=str(self.cfg("llm_model", "") or ""),
            timeout=float(self.cfg("llm_timeout", 120) or 120),
            proxy=str(self.cfg("llm_proxy", "") or ""),
        )

    async def _call_report_llm(
        self, prompt: str, *, umo: str = "", system_prompt: str = ""
    ) -> str | None:
        """生成报告用的大模型调用：专用 Key 优先，失败按配置回退 AstrBot。

        Args:
            prompt: 用户提示词。
            umo: 会话来源，回退到 AstrBot 提供商时用来解析会话默认模型。
            system_prompt: 自定义系统提示词。留空则用配置项
                ``analysis_system_prompt``，再留空用内置的报告模板。
                闲聊兜底会传自己的系统提示词进来，避免被「报告体」污染。

        Returns:
            报告正文；失败或未启用时返回 None（并在日志里说明原因）。
        """
        if not self.cfg("enable_llm_analysis", True):
            return None

        system_prompt = (system_prompt or "").strip()
        if not system_prompt:
            system_prompt = str(self.cfg("analysis_system_prompt", "") or "").strip()
        if not system_prompt:
            system_prompt = DEFAULT_SYSTEM_PROMPT

        client = self._dedicated_client()
        if client is not None:
            try:
                max_tokens = int(self.cfg("llm_max_tokens", 0) or 0)
                return await client.chat(
                    system_prompt,
                    prompt,
                    temperature=float(self.cfg("llm_temperature", 0.7) or 0),
                    max_tokens=max_tokens or None,
                )
            except LLMRequestError as e:
                logger.error(f"[dota2] 专用模型调用失败（{client.endpoint}）：{e}")
            except Exception as e:  # noqa: BLE001
                logger.error(f"[dota2] 专用模型调用出现异常：{e}", exc_info=True)

            if not self.cfg("llm_fallback_on_error", True):
                logger.warning("[dota2] 已关闭回退，本次不做 AI 分析")
                return None
            logger.warning("[dota2] 回退到 AstrBot 的模型提供商重试")

        provider = await resolve_provider(
            self.context,
            umo,
            str(self.cfg("llm_provider_id", "") or ""),
        )
        if provider is None:
            logger.warning("[dota2] 没有可用的模型提供商，跳过 AI 分析")
            return None
        try:
            return await call_llm(provider, system_prompt, prompt)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[dota2] 调用大模型失败: {e}", exc_info=True)
            return None

    async def _generate_report(
        self, event: AstrMessageEvent, prompt: str
    ) -> str | None:
        """调用大模型生成报告。失败或未启用时返回 None。"""
        return await self._call_report_llm(
            prompt, umo=event.unified_msg_origin
        )

    # ==================================================================
    # 监听后台任务
    # ==================================================================
    def _start_watcher(self) -> None:
        """启动监听循环（幂等）。"""
        if not self.cfg("watch_enabled", True):
            return
        if self._watch_task is not None and not self._watch_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError as e:  # pragma: no cover
                logger.error(f"[dota2] 无法启动监听任务：{e}")
                return
        self._stopping = False
        self._watch_task = loop.create_task(self._watch_loop())
        logger.info("[dota2] 比赛监听任务已启动")

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """AstrBot 初始化完成后确保监听任务已启动。"""
        self._start_watcher()

    async def _watch_loop(self) -> None:
        """监听主循环。

        内部是两个彼此独立的节拍：

        - **轮询节拍**（``watch_interval``）：拉取被监听玩家的比赛列表，发现新比赛；
        - **处理节拍**（``PENDING_TICK_SECONDS``）：兑现 pending 队列里的 ``next_try_at``。

        两者必须解耦：早先只有一个节拍，导致 ``next_try_at`` 即使设成 60 秒，
        也要等满一个 ``watch_interval``（默认 180 秒）才会被检查到。
        """
        # 启动后稍等一会，避免与 AstrBot 自身的启动流程抢资源
        await asyncio.sleep(15)
        while not self._stopping:
            try:
                now = time.time()
                if now >= self._next_poll_at:
                    poll_interval = max(60, int(self.cfg("watch_interval", 180)))
                    self._next_poll_at = now + poll_interval
                    await self._check_watchers()
                else:
                    await self._process_pending()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.error(f"[dota2] 监听循环出错：{e}", exc_info=True)
            try:
                await asyncio.sleep(PENDING_TICK_SECONDS)
            except asyncio.CancelledError:
                raise

    async def _check_watchers(self) -> None:
        """检查所有被监听玩家是否有新比赛。"""
        watchers = self.store.list_watchers()

        if watchers:
            groups: dict[int, list[dict]] = {}
            for watcher in watchers:
                account_id = watcher.get("account_id")
                if isinstance(account_id, int):
                    groups.setdefault(account_id, []).append(watcher)

            concurrency = max(1, int(self.cfg("watch_concurrency", 2)))
            semaphore = asyncio.Semaphore(concurrency)

            async def _runner(account_id: int, group: list[dict]) -> None:
                async with semaphore:
                    try:
                        await self._check_account(account_id, group)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:  # noqa: BLE001
                        logger.error(
                            f"[dota2] 检查玩家 {account_id} 的新比赛失败：{e}",
                            exc_info=True,
                        )

            await asyncio.gather(
                *[_runner(account_id, group) for account_id, group in groups.items()]
            )

        # 注意：即使当前一个监听者都没有，也要继续推进 pending 队列，
        # 否则队列里最后几场比赛会永远卡住（监听者被删掉时尤其明显）。
        await self._process_pending()

    async def _check_account(self, account_id: int, watchers: list[dict]) -> None:
        """检查单个玩家是否有新比赛，有则加入待推送队列。"""
        try:
            matches = await self.api.get_player_matches(account_id, limit=8)
        except OpenDotaError as e:
            logger.warning(f"[dota2] 拉取玩家 {account_id} 比赛列表失败：{e}")
            return
        if not matches:
            return

        # 门槛取「进度最落后的那个监听者」：只要还有监听者没看过这场，就必须处理。
        # 之前用的是 max()，于是一旦有新监听者加入（它记录的基线是当下最新的比赛），
        # 基线就被抬高，老监听者还没来得及看的比赛会被整体过滤掉。
        floor = min(int(w.get("last_match_id") or 0) for w in watchers)
        new_matches = [
            match for match in matches if int(match.get("match_id") or 0) > floor
        ]
        if not new_matches:
            return
        new_matches.sort(key=lambda m: int(m.get("match_id") or 0))

        # 被监听玩家自己的昵称（用于报告焦点玩家那一行），取第一个非空的
        focus_name = next(
            (
                str(w.get("personaname") or "")
                for w in watchers
                if w.get("personaname")
            ),
            "",
        )

        queued = 0
        for match in new_matches:
            match_id = int(match.get("match_id") or 0)
            targets = []
            seen_umo: set[str] = set()
            for watcher in watchers:
                # 每位监听者按自己的进度独立判断
                if int(watcher.get("last_match_id") or 0) >= match_id:
                    continue
                umo = str(watcher.get("umo") or "")
                if not umo or umo in seen_umo:
                    continue
                seen_umo.add(umo)
                targets.append(
                    {
                        # 记下 watcher_id，推送成功后才能精确推进这一位的进度
                        "watcher_id": str(watcher.get("id") or ""),
                        "umo": umo,
                        "platform": watcher.get("platform") or "",
                        "created_by": str(watcher.get("created_by") or ""),
                        "personaname": watcher.get("personaname") or "",
                    }
                )
            if targets:
                self._queue_pending(match_id, account_id, targets, focus_name)
                queued += 1

        # 这里故意**不**推进 last_match_id：它必须等到真正推送成功之后再落盘，
        # 否则一旦重启、崩溃或队列丢弃，这些比赛就永久漏推了。
        if queued:
            logger.info(
                f"[dota2] 玩家 {account_id} 发现 {queued} 场新比赛，已加入待推送队列"
            )

    def _queue_pending(
        self,
        match_id: int,
        focus_account_id: int,
        targets: list[dict],
        focus_name: str = "",
    ) -> None:
        """把「一场比赛」加入待推送队列（等待数据源收录）。

        队列键就是 ``match_id``：**同一局比赛只保留一项**。当群里多位成员各自
        监听了不同的玩家、而这几位玩家又恰好打了同一局时，老实现会按
        ``(match_id, account_id)`` 拆成多份，于是同一场比赛被重复调用大模型、
        在群里刷好几份几乎一样的推送。现在他们共用一份短评与一次推送。

        每位焦点玩家在 ``focuses`` 里各占一项，以自己为焦点收集 ``targets``；
        生成短评时把所有人的 account_id 一起交给提示词，对每个人分别点一句，
        因此既不重复调用模型、也不会互相覆盖焦点。
        """
        item = self._pending.get(match_id)
        if item is None:
            item = {
                "key": match_id,
                "match_id": match_id,
                #: 本局中所有被监听的玩家：``[{"account_id", "name", "targets"}]``
                "focuses": [],
                #: 比赛还没被数据源收录的次数（收录是这里唯一要等的东西）
                "wait_attempts": 0,
                #: 投递尝试次数。数据都就绪、但标题始终发不出去时（例如平台侧一直
                #: 报错），超过上限就放弃这场比赛，避免无限重试刷屏。
                "deliver_attempts": 0,
                "first_seen": time.time(),
                "next_try_at": 0.0,
                #: 是否正在处理中（防止并发投递同一场比赛）
                "processing": False,
            }
            self._pending[match_id] = item

        focus = next(
            (
                entry
                for entry in item["focuses"]
                if entry.get("account_id") == focus_account_id
            ),
            None,
        )
        if focus is None:
            focus = {"account_id": focus_account_id, "name": focus_name, "targets": []}
            item["focuses"].append(focus)
        elif not focus.get("name") and focus_name:
            focus["name"] = focus_name

        # 同一个 watcher 只会有一条记录，按 watcher_id 去重；
        # 至于「同一会话里的两个人各监听一个玩家」这种情况，两条都要留着，
        # 推送时按会话聚合，@ 的时候会把两人都带上。
        known = {target.get("watcher_id") for target in focus["targets"]}
        for target in targets:
            watcher_id = target.get("watcher_id")
            if watcher_id and watcher_id in known:
                continue
            focus["targets"].append(target)
            known.add(watcher_id)

        max_pending = max(1, int(self.cfg("watch_max_pending", 20)))
        if len(self._pending) > max_pending:
            oldest = min(self._pending.values(), key=lambda x: x["first_seen"])
            # 被丢弃的比赛不推进 last_match_id，下一轮会被重新发现，
            # 因此队列溢出只会推迟推送，不会静默漏推。
            self._pending.pop(oldest["key"], None)
            logger.warning(
                f"[dota2] 待推送队列已满（{max_pending}），"
                f"暂时让出比赛 {oldest['match_id']}，稍后重新入队"
            )

    async def _process_pending(self) -> None:
        """处理待推送队列：等待详细数据就绪后生成分析并推送。"""
        if not self._pending:
            return
        now = time.time()
        due = [
            item
            for item in list(self._pending.values())
            if item.get("next_try_at", 0) <= now and not item.get("processing")
        ]
        if not due:
            return

        # 单轮内共享：一局比赛无论有多少位被监听的玩家参战，都只拉一次比赛详情、
        # 只调用一次大模型。
        match_cache: dict[int, dict | None] = {}

        for item in due:
            # due 是一次性算好的，期间另一个并发的检查可能已经把这一项处理掉了，
            # 因此逐项复查。检查与置位之间没有 await，不会被其它任务插进来。
            if item.get("processing"):
                continue
            if self._pending.get(item.get("key")) is not item:
                continue

            match_id = int(item["match_id"])
            # 标记「处理中」：避免两次并发的检查（后台循环 + 懒启动/手动触发）
            # 同时投递同一场比赛，造成重复推送。
            item["processing"] = True
            try:
                await self._try_deliver(item, match_cache)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.error(f"[dota2] 推送比赛 {match_id} 失败：{e}", exc_info=True)
                item["next_try_at"] = time.time() + 300
            finally:
                item["processing"] = False

    async def _try_deliver(
        self,
        item: dict,
        match_cache: dict[int, dict | None],
    ) -> None:
        """尝试获取比赛数据并推送；比赛还没被收录时安排下次重试。

        这里**不再等待 OpenDota 解析录像**。监听推送的内容已经改成
        「胜负 + K/D/A + 近期战绩」的赛后短评（见
        :func:`dota_analyzer.build_watch_comment_prompt`），这些字段在比赛被
        收录的那一刻就齐全；而等解析要十几分钟，只会把推送白白推迟。
        需要解析产物（逐分钟曲线、团战逐人等）的深度复盘走
        ``/d2 单场 <比赛ID>``，那条链路该等还是会等。
        """
        match_id = int(item["match_id"])
        max_wait = max(1, int(self.cfg("watch_max_wait_attempts", 30)))

        if match_id in match_cache:
            match = match_cache[match_id]
        else:
            match = await self.api.get_match(match_id)
            match_cache[match_id] = match

        # ---- 数据源尚未收录这场比赛 ----
        # 短评要的胜负与 K/D/A 也在这份数据里，所以这里必须等收录。
        if not match:
            item["wait_attempts"] += 1
            waited = item["wait_attempts"]
            if waited > max_wait:
                logger.warning(
                    f"[dota2] 比赛 {match_id} 等待数据源收录超过 {max_wait} 次，"
                    f"放弃推送"
                )
                await self._finish(item, delivered_umos=set(), force_advance=True)
                return
            delay = 60 if waited <= 5 else 180
            item["next_try_at"] = time.time() + delay
            logger.info(
                f"[dota2] 比赛 {match_id} 尚未被数据源收录"
                f"（第 {waited}/{max_wait} 次），{delay}s 后重试"
            )
            return

        delivered = await self._deliver(item, match)

        # 数据已就绪但一条都没送出去：计数并在超过上限后放弃，避免无限重试。
        # 注意这里只在「完全没送出去」时计数，正文失败不影响 delivered（见 _deliver）。
        if not delivered:
            item["deliver_attempts"] = int(item.get("deliver_attempts", 0)) + 1
            max_deliver = max(1, int(self.cfg("watch_max_deliver_attempts", 10)))
            if item["deliver_attempts"] > max_deliver:
                logger.warning(
                    f"[dota2] 比赛 {match_id} 连续 {item['deliver_attempts']} 次推送失败，"
                    f"放弃该场（数据已就绪，可能是平台发送通道问题）"
                )
                await self._finish(item, delivered_umos=set(), force_advance=True)
                return

        await self._finish(item, delivered_umos=delivered)

    async def _finish(
        self,
        item: dict,
        delivered_umos: set[str],
        force_advance: bool = False,
    ) -> None:
        """结束一个待推送项，并推进已成功送达的监听者进度。

        ``last_match_id`` 只在**成功推送之后**才落盘：推送失败的会话保持原基线，
        下一轮会被重新发现并补推，从而实现「至少一次」投递而非「可能永久漏推」。

        Args:
            item: 待推送项。
            delivered_umos: 推送成功的会话标识集合。
            force_advance: 主动放弃这场推送时置 True，避免每轮都重新入队空转。
        """
        self._pending.pop(item.get("key"), None)
        match_id = int(item["match_id"])

        # 只有在生成这份报告时就已经纳入的焦点玩家，才能因为本次推送而推进进度。
        # 若某位焦点玩家是在报告生成之后才被并进这一项的（并发检查时的窄窗口），
        # 他的监听者不能算「已看过这场」，否则会永久漏掉这场比赛。
        covered = item.get("delivered_focus_ids")
        focuses = item.get("focuses") or []
        leftovers: list[dict] = []

        updates: dict[str, int] = {}
        for focus in focuses:
            if (
                not force_advance
                and covered is not None
                and focus.get("account_id") not in covered
            ):
                leftovers.append(focus)
                continue
            for target in focus.get("targets") or []:
                if not force_advance and target.get("umo") not in delivered_umos:
                    continue
                watcher_id = str(target.get("watcher_id") or "")
                if watcher_id:
                    updates[watcher_id] = match_id
        if not updates and not leftovers:
            return
        if updates:
            try:
                await self.store.update_watcher_last_match_bulk(updates)
            except Exception as e:  # noqa: BLE001
                logger.error(f"[dota2] 更新监听进度失败：{e}")
        for focus in leftovers:
            # 重新入队（比赛详情已就绪，下一轮会立刻投递）
            self._queue_pending(
                match_id,
                int(focus.get("account_id") or 0),
                list(focus.get("targets") or []),
                str(focus.get("name") or ""),
            )
        if leftovers:
            logger.info(
                f"[dota2] 比赛 {match_id} 有 {len(leftovers)} 位焦点玩家未纳入本次报告，"
                "已重新入队，下一轮单独推送"
            )

    async def _deliver(self, item: dict, match: dict) -> set[str]:
        """生成「赛后短评」并推送到所有目标会话。

        **一次调用只生成一份短评**，即使本局有多位被监听的玩家参战：短评里会
        为每位焦点玩家各点一句，按会话聚合后每个会话只推送一条。

        推送内容只要胜负与 K/D/A（外加各人近期战绩作对照），因此**不依赖录像
        解析**，比赛一被数据源收录就能发出。想要完整分析的走
        ``/d2 单场 <比赛ID>``，那条链路才用深度复盘提示词。

        注意这里**不 @ 任何订阅人**：比赛结束的自动播报不该每次都在群里点名。

        Returns:
            推送成功的会话标识（umo）集合。调用方据此决定是否推进这些监听者的
            进度——失败的会话保持原基线，下一轮会重新入队补推。
        """
        match_id = int(item["match_id"])
        focuses: list[dict] = item.get("focuses") or []
        if not focuses:
            return set()

        # 快照本次报告覆盖的焦点玩家：报告生成期间若又有新焦点被并入这一项，
        # 它们不能算作「已推送」，由 _finish 重新入队。
        focus_ids = [
            int(focus["account_id"])
            for focus in focuses
            if int(focus.get("account_id") or 0)
        ]
        if not focus_ids:
            return set()
        focus_names = {
            int(focus["account_id"]): str(focus.get("name") or "")
            for focus in focuses
            if int(focus.get("account_id") or 0)
        }
        item["delivered_focus_ids"] = set(focus_ids)

        # 按会话聚合：同一会话（群）可能既是 A 的监听者、也是 B 的监听者，
        # 这一局只应收到一条推送。这里只做「按会话去重」——早期的 @ 已移除，
        # 因此不再收集 creators。
        per_umo: dict[str, dict] = {}
        for focus in focuses:
            for target in focus.get("targets") or []:
                umo = str(target.get("umo") or "")
                if not umo:
                    continue
                if umo not in per_umo:
                    per_umo[umo] = {
                        "umo": umo,
                        "platform": target.get("platform") or "",
                        "personaname": target.get("personaname") or "",
                    }
        targets = list(per_umo.values())
        if not targets:
            return set()

        # 短评只需要英雄名（把 hero_id 写成人话），不再拉道具与技能常量：
        # 那两次请求原先是为深度复盘准备的，短评用不上。
        try:
            heroes = await self.api.get_heroes()
        except OpenDotaError as e:
            logger.error(f"[dota2] 推送比赛 {match_id} 时拉取英雄常量失败：{e}")
            heroes = {}

        # 近期战绩：短评要靠它判断「这局是不是正常发挥」。
        recent_blocks = await self._recent_summary_blocks(
            match_id, focus_ids, focus_names, heroes
        )
        if len(focus_ids) > MAX_FOCUS_RECENT_CONTEXT:
            logger.debug(
                f"[dota2] 比赛 {match_id} 焦点玩家较多，仅前 "
                f"{MAX_FOCUS_RECENT_CONTEXT} 位附带近期战绩"
            )

        prompt = build_watch_comment_prompt(
            match=match,
            heroes=heroes,
            focus_account_ids=focus_ids,
            focus_names=focus_names,
            recent_blocks=recent_blocks,
        )

        # 标题不带「数据完整度」：短评不依赖解析，写上去只会让人以为数据有问题。
        headline = self.match_headline(
            match, heroes, focus_ids, with_parsed_note=False
        )

        # 大模型短评只生成一次，多个会话复用（避免重复消耗配额）。
        # 走 `_call_report_llm`：专用 API Key 优先，未配置时回退 AstrBot 提供商。
        # 系统提示词用短评专用版本——报告那套要求 Markdown 小标题与 800 字，
        # 会把「几句话」带成一篇小作文。
        first_umo = targets[0]["umo"]
        comment = await self._call_report_llm(
            prompt, umo=first_umo, system_prompt=self._watch_comment_system_prompt()
        )

        delivered: set[str] = set()
        for target in targets:
            umo = target["umo"]
            try:
                # 「送达」的判定点是**标题发出去**：标题一进群，用户就已经看到这条
                # 推送了，此时若再因为正文二次发送失败而回退基线，下一轮会把同一场
                # 比赛重新发现并再推一遍标题 —— 群里就会无限复读那一行标题。
                #
                # 这不是假想：线上日志里「🏁 比赛回顾 · 8996928421」从 20:21 一路
                # 复读到 20:39（7 次、间隔 2~3.5 分钟）。
                # （当时的对照复现在 tests/old_code_repro.py；那个脚本针对的是旧版
                #  「图片正文」链路，v2.0.0 改成短评后已失效，保留仅供追溯。）
                #
                # 因此正文发送失败只记日志、不回退，绝不影响 delivered。
                headline_ok = await self._send_to_session(umo, headline)
                if not headline_ok:
                    logger.warning(
                        f"[dota2] 向 {umo} 推送比赛 {match_id} 的标题未送达，稍后重试"
                    )
                    continue

                delivered.add(umo)
                logger.info(f"[dota2] 比赛 {match_id} 的赛后短评已推送到 {umo}")

                # 正文属于「尽力而为」：失败不影响送达判定，只提示用户去看概览。
                # `_send_watch_comment` 内部已逐块收敛异常，这里的 try 是最后一道保险。
                try:
                    body_ok = await self._send_watch_comment(umo, comment)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"[dota2] 向 {umo} 推送比赛 {match_id} 的短评失败：{e}")
                    body_ok = False
                if not body_ok:
                    logger.warning(
                        f"[dota2] 比赛 {match_id} 的短评未能送达 {umo}，"
                        f"标题已发出，不再重复推送"
                    )
                    await self._notify_body_failed(umo, match_id)
            except Exception as e:  # noqa: BLE001
                # 兜底：`_send_to_session` / `_send_watch_comment` 内部已各自收敛异常，
                # 走到这里说明出了预期外的问题（例如常量或格式化代码抛错）。
                # 此时该会话本轮只发出去一部分，补一条提示让失败可感知——
                # 否则用户会看到「只有标题、后面什么都没有」而不知道为什么。
                logger.exception(f"[dota2] 向 {umo} 推送比赛 {match_id} 时出现预期外异常")
                if umo in delivered:
                    await self._notify_body_failed(umo, match_id)
        return delivered

    def _watch_comment_system_prompt(self) -> str:
        """监听短评用的系统提示词；配置留空则用内置版本。

        必须是**独立**的一套：``analysis_system_prompt`` 是给长报告写的
        （要求 Markdown 小标题、总长 800 字以内），拿它写「2~4 句话」的短评
        会被带成一篇小作文，正好违背监听推送的初衷。
        """
        text = str(self.cfg("watch_comment_system_prompt", "") or "").strip()
        return text or WATCH_COMMENT_SYSTEM_PROMPT

    async def _send_watch_comment(self, umo: str, comment: str | None) -> bool:
        """发送监听短评正文。

        短评只有几句话（几十到一两百字），因此**不转图片、也不做报告式分块**：
        直接按 ``max_message_length`` 拆成文本发出去即可。模型不可用时给一句
        说明，让用户知道不是插件哑了。

        每一块的发送都是独立的：某一块抛异常或返回 False 只会让这一块失败，
        后续分块仍会继续尝试（早期实现里任何一块出错就直接冒泡出函数，
        导致「第 2 块失败 → 第 3 块起全部丢失」）。
        """
        if not comment:
            return await self._send_quiet(
                umo,
                "⚠️ 未启用大模型分析或模型不可用，仅提供上述比赛概览。",
                match_id=None,
            )

        ok = True
        chunks = self._chunk_text(comment)
        for index, chunk in enumerate(chunks, start=1):
            # 逐块 try/except：单块失败不能拖垮后面的分块。
            if not await self._send_quiet(umo, chunk):
                logger.warning(
                    f"[dota2] 短评第 {index}/{len(chunks)} 块发送失败：{umo}"
                )
                ok = False
        return ok

    async def _send_quiet(
        self,
        umo: str,
        text: str | None,
        *,
        match_id: int | None = None,
    ) -> bool:
        """发送一条**文本**消息并把异常收敛成 ``False``。

        统一入口，避免每个调用点各写一遍 try/except——前面就是因为漏了一处，
        让正文异常直接冒泡到 ``_deliver`` 的外层 except，最后只留在日志里，
        用户侧完全无感。

        只发文本：监听推送已改成几句短评（不需要转图片），需要图片的是
        ``/d2 单场`` 那条链路，它在自己那边处理渲染与回退。
        """
        try:
            chain = MessageChain().message(text or "")
            return await self._send(umo, chain) is not False
        except Exception as e:  # noqa: BLE001
            label = f"比赛 {match_id} 的" if match_id is not None else ""
            logger.error(f"[dota2] 发送{label}消息到 {umo} 失败：{e}")
            return False

    async def _notify_body_failed(self, umo: str, match_id: int) -> None:
        """正文发送失败时给一条轻量提示，避免用户以为「只有标题、没有点评」。

        提示本身也走 :meth:`_send_quiet`：连提示都发不出去时只记日志，
        绝不能再往上报——否则会把「正文失败」升级成「整轮推送失败」。
        """
        await self._send_quiet(
            umo,
            f"⚠️ 比赛 {match_id} 的短评未能发出（平台发送通道异常），"
            f"上方为比赛概览。可用 `/d2 单场 {match_id}` 获取完整复盘。",
            match_id=match_id,
        )

    async def _send(self, umo: str, chain: MessageChain) -> bool | None:
        """发送单条消息并归一化返回值（不同版本 AstrBot 的返回契约不一致）。"""
        return await self.context.send_message(umo, chain)

    async def _send_to_session(self, umo: str, text: str) -> bool:
        """发送推送标题，按 ``max_message_length`` 分块。

        **故意不 @ 任何订阅人**：这是「比赛结束的自动播报」，一次推送就在群里
        @ 一串人是纯打扰。早期实现会 @ 该会话里所有添加过监听的人
        （``target["creators"]``，配置项 ``watch_notify_at``），现已整体移除——
        想被提醒的成员自己看群消息即可。

        Returns:
            是否全部发送成功。失败时调用方会保留该监听者的基线以便补推。
        """
        ok = True
        chunks = self._chunk_text(text)
        for index, chunk in enumerate(chunks, start=1):
            # 与正文一致：单块失败（含抛异常）不拖垮后续分块。
            if not await self._send_quiet(umo, chunk):
                logger.warning(
                    f"[dota2] 概览第 {index}/{len(chunks)} 块发送失败：{umo}"
                )
                ok = False
        return ok

    @staticmethod
    def match_headline(
        match: dict,
        heroes: dict[int, dict],
        focus_account_ids: int | list[int] | tuple[int, ...] | None = None,
        parsed: bool = True,
        *,
        with_parsed_note: bool = True,
    ) -> str:
        """生成比赛概览头部文本。

        ``focus_account_ids`` 可以是一位或多位焦点玩家，多位时逐个列出。

        ``parsed`` 参数**已不参与判定**（保留仅为兼容既有调用方）：完整度文案
        统一由 :func:`parsed_state` 从 ``match`` 推导，确保与 AI 提示词正文
        用的是同一套口径，不会一个说已解析、一个说未解析。

        ``with_parsed_note`` 控制是否输出最后那行「数据完整度」：监听推送的
        赛后短评不依赖录像解析（只要胜负与 K/D/A），标注完整度只会让用户
        误以为数据有问题，因此那边传 ``False``。
        """
        _ = parsed  # 兼容保留：判定改用 parsed_state(match)，见下
        focus_ids = normalize_focus_ids(focus_account_ids)
        radiant_win = bool(match.get("radiant_win"))
        winner = "天辉" if radiant_win else "夜魇"
        lines = [
            f"🏁 比赛回顾 · {match.get('match_id')}",
            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(int(match.get('start_time') or 0)))}"
            f"（{fmt_ago(match.get('start_time'))}）"
            f" · 时长 {fmt_duration(match.get('duration'))}"
            f" · {mode_text(match)}",
            f"天辉 {match.get('radiant_score', 0)} : {match.get('dire_score', 0)} 夜魇"
            f" · {winner}获胜",
        ]

        if focus_ids:
            by_account = {
                int(p["account_id"]): p
                for p in (match.get("players") or [])
                if isinstance(p, dict) and p.get("account_id")
            }
            for index, focus_id in enumerate(focus_ids):
                player = by_account.get(int(focus_id))
                if player is None:
                    continue
                win = player_win(player)
                # 只有一位焦点玩家时沿用「焦点玩家」的措辞，多位时逐行列出来
                label = "👤 焦点玩家" if len(focus_ids) == 1 else f"👤 焦点玩家 {index + 1}"
                lines.append(
                    f"{label}：{player.get('name') or focus_id} · "
                    f"{hname(heroes, player.get('hero_id'))} · "
                    f"{player.get('kills', 0)}/{player.get('deaths', 0)}/{player.get('assists', 0)} "
                    f"{'✅胜' if win else '❌负'}"
                )

        if with_parsed_note:
            # 与提示词正文共用同一套判据（parsed_state），避免标题与正文打架
            _parsed_flag, parsed_note = parsed_state(match)
            lines.append(f"数据完整度：{parsed_note}")
        return "\n".join(lines)

    # ==================================================================
    # 生命周期
    # ==================================================================
    async def terminate(self):
        """插件被卸载 / 停用时清理后台任务与网络连接。"""
        self._stopping = True
        task = self._watch_task
        self._watch_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # 等待解析的后台任务可能挂着十分钟，必须一并取消，
        # 否则卸载后它们还会继续占用连接、并且向已下线的会话推消息。
        parse_tasks = list(self._parse_tasks.values())
        self._parse_tasks.clear()
        for parse_task in parse_tasks:
            if not parse_task.done():
                parse_task.cancel()
        for parse_task in parse_tasks:
            try:
                await parse_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.api.close()
        logger.info("[dota2] 插件已卸载，监听任务与等待解析任务已停止")
