"""AstrBot Dota2 数据查询助手插件入口。

功能总览：
1. 绑定玩家（昵称 / 32 位 account_id / 64 位 SteamID）；
2. 查询最近战绩；
3. 调用大模型分析近期表现与打法风格（默认最近 20 场，场次可调）；
4. 调用大模型深度复盘单场比赛（比赛走势、质量评估、十人点评）；
5. 监听玩家，对有详细数据的新比赛自动生成分析并推送到绑定会话，支持解绑。

数据来源：OpenDota API（https://docs.opendota.com/）
"""

import asyncio
import functools
import inspect
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

try:  # 插件目录被作为包加载时的相对导入
    from .dota_analyzer import (
        build_recent_analysis_prompt,
        build_single_match_analysis_prompt,
        call_llm,
        resolve_provider,
    )
    from .dota_api import (
        OpenDotaClient,
        OpenDotaError,
        TargetNotFoundError,
        to_account_id,
        to_steam_id64,
    )
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
        player_win,
        rank_text,
        summarize_hero_history,
        summarize_matches,
    )
    from .dota_store import DotaStore
    from . import dota_nlu
except ImportError:  # 兜底：以普通模块方式加载时（把插件目录加入 sys.path）
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dota_analyzer import (  # type: ignore[no-redef]
        build_recent_analysis_prompt,
        build_single_match_analysis_prompt,
        call_llm,
        resolve_provider,
    )
    from dota_api import (  # type: ignore[no-redef]
        OpenDotaClient,
        OpenDotaError,
        TargetNotFoundError,
        to_account_id,
        to_steam_id64,
    )
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
        player_win,
        rank_text,
        summarize_hero_history,
        summarize_matches,
    )
    from dota_store import DotaStore  # type: ignore[no-redef]

    import dota_nlu  # type: ignore[no-redef]

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

#: 连续第几次「已收录但未解析」时重新提交一次解析申请。
#: OpenDota 的解析任务可能丢单，只申请一次不够稳。
REPARSE_REQUEST_EVERY = 5

#: 监听推送时，最多为几位焦点玩家附带「近期状态」上下文。
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

HELP_TEXT = """🎮 Dota2 数据查询助手（数据来源：OpenDota）

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

【监听】
/d2 监听 [目标]　　　　　　　 比赛结束后自动推送分析到本会话
/d2 取消监听 <目标 | 全部>　　取消你自己添加的监听
/d2 监听列表　　　　　　　　　查看本会话的监听

说明：
· 「目标」可以填昵称、32 位账号 ID 或 64 位 SteamID；
· 不填「目标」时，默认使用你在当前会话绑定的账号；
· 一局比赛只分析一次：若这场比赛里有多位被监听的玩家，报告会对他们逐一深入点评；
· 「绑定列表」中他人的账号与用户 ID 默认打码，保护群聊隐私；
· 想让查询结果更好看，可以在配置中调整「长报告转为图片发送」。

自然语言（不用记指令）：
· 直接说人话也能用，例如「帮我看看我的战绩」「分析一下天鸽最近的发挥」
  「这局 8993438099 复盘一下」「最近 20 把打得怎么样」；
· 群聊里需要 @ 机器人 才会响应（可在配置中关闭这个限制）；
· 群里说「绑定 86745912」这类闲聊式指令时，插件会先确认再执行，避免误触。"""


def take_over_event(func):
    """接管事件：直发回复 + 结束后终止事件传播。

    AstrBot 的流水线是「洋葱模型」：handler 每 ``yield`` 一条结果，后续
    阶段（含默认大模型的请求阶段）就会被执行一次。本插件的指令往往要先
    yield 一条「⏳ 正在拉取…」再花十几秒拉数据，如果不做处理，默认大模型
    就会在这个空档里插嘴，编造出「没有绑定成功，无法读取数据。」这类
    插件代码里根本不存在的话术，最后还会在真正的分析结果之前先冒出来。

    因此这里做两件事：

    1. **直发**：把 handler yield 出来的结果通过 ``event.send()`` 直接投递
       （这正是 AstrBot 自己的 RespondStage 使用的通道），不再回灌流水线。
       这样插件执行期间流水线里不会产生任何额外的执行机会。
    2. **终止传播**：handler 结束后调用 ``event.stop_event()``，阻止流水线
       继续走到默认大模型阶段。

    两步都做了降级：运行时没有 ``event.send`` 或结果上没有 ``chain`` 时，
    自动退回为正常 ``yield``，保证在老版本 / 自定义 Event 上也能出消息。
    """

    @functools.wraps(func)
    async def wrapper(self, event: AstrMessageEvent, *args, **kwargs):
        try:
            async for result in func(self, event, *args, **kwargs):
                if not await self._deliver_now(event, result):
                    yield result
        finally:
            try:
                event.stop_event()
            except Exception as e:  # noqa: BLE001 - 老版本没有该 API
                logger.debug(f"[dota2] 终止事件传播失败（可忽略）: {e}")

    return wrapper


class Dota2Plugin(Star):
    """Dota2 数据查询助手。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.data_dir = self._resolve_data_dir()
        self.store = DotaStore(self.data_dir)
        self.store.load()
        self.api = OpenDotaClient(
            api_key=self.cfg("opendota_api_key", ""),
            timeout=self.cfg("request_timeout", 30),
            max_retries=self.cfg("max_retries", 3),
            rate_limit_per_minute=self.cfg("rate_limit_per_minute", 55),
            proxy=self.cfg("http_proxy", ""),
        )
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
        logger.info(
            f"[dota2] 插件已加载，数据目录: {self.data_dir}，"
            f"监听: {'开启' if self.cfg('watch_enabled', True) else '关闭'}"
        )
        self._start_watcher()

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

        当前运行时不支持直发时返回 False，由调用方回退为 ``yield``。
        """
        send = getattr(event, "send", None)
        chain = getattr(result, "chain", None)
        if not callable(send) or chain is None:
            return False
        try:
            await send(chain)
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
                    f"OpenDota 查不到账号 **{account_id}**。\n"
                    "常见原因：该账号在 Dota 2 设置中关闭了「公开比赛数据」，"
                    "或从未被 OpenDota 索引过。"
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

        # 昵称：走 OpenDota 搜索
        try:
            results = await self.api.search_player(target)
        except OpenDotaError as e:
            raise TargetNotFoundError(
                f"昵称搜索失败（{e}）。\n"
                "OpenDota 的昵称搜索接口偶尔会超时或限流，建议改用"
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
        "watch": "d2_watch",
        "unwatch": "d2_unwatch",
        "watchlist": "d2_watchlist",
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

    def _nlu_should_handle(self, event: AstrMessageEvent, text: str) -> bool:
        """判断这条消息要不要交给自然语言入口处理。"""
        if not self.cfg("nlu_enabled", True):
            return False
        text = (text or "").strip()
        if not text:
            return False
        # 指令类消息交给命令 handler，这里不截胡
        if text.startswith(NLU_SKIP_PREFIXES):
            return False

        # 群聊 / 私聊判断：用消息类型与 umo 双重判断，兼容各适配器。
        # get_message_type 在少数自定义 Event 上可能不存在，因此做了容错。
        umo = str(event.unified_msg_origin)
        try:
            message_type = str(event.get_message_type())
        except Exception:  # noqa: BLE001
            message_type = ""
        is_group = "GROUP_MESSAGE" in message_type or "GroupMessage" in umo
        if is_group and self.cfg("nlu_group_require_at", True):
            # 群里必须 @ 机器人（或使用唤醒前缀，此时 message_str 里已带前缀）。
            # 这条限制能挡掉绝大多数「群里别人随口一说就被插件抢答」的情况。
            try:
                if not event.is_at_or_wake_command:
                    return False
            except AttributeError:
                return False
        return True

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
    @take_over_event
    async def d2_natural(self, event: AstrMessageEvent):
        """自然语言入口：把「帮我看看我的战绩」这类人话转成对应功能。"""
        text = dota_nlu.normalize(getattr(event, "message_str", "") or "")
        umo = event.unified_msg_origin
        uid = str(event.get_sender_id())

        # ---------- 1. 先处理「确认 / 取消」回复 ----------
        pending = self._nlu_confirm.get((umo, uid))
        if pending is not None:
            if pending[2] < time.time():
                self._nlu_confirm.pop((umo, uid), None)
            elif text in NLU_CONFIRM_WORDS:
                name, args, _ = self._nlu_pop_confirm(umo, uid)
                agen = self._nlu_invoke(name, event, args)
                if agen is not None:
                    logger.info(f"[dota2] 自然语言确认执行: {name} {args!r}")
                    async for item in agen:
                        yield item
                return
            elif text in NLU_CANCEL_WORDS:
                self._nlu_pop_confirm(umo, uid)
                yield event.plain_result("好的，已取消。")
                return

        # ---------- 2. 该不该处理这条消息 ----------
        if not self._nlu_should_handle(event, text):
            return

        # ---------- 3. 识别意图 ----------
        intent = dota_nlu.parse(text)
        if intent is None and self.cfg("nlu_llm_fallback", False):
            intent = await self._nlu_classify_with_llm(event, text)
        if intent is None:
            # 没识别出来：什么都不做，把消息让给默认大模型，避免抢答
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
                    "复盘单场需要比赛 ID，例如：`这局 8993438099 帮我复盘一下`。\n"
                    "比赛 ID 可以从「我的战绩」里拿，或直接用 Dota 客户端的比赛编号。"
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
        """AI 深度复盘单场比赛：/d2 单场 <比赛ID> [焦点玩家]（多位用 、或 , 分隔）"""
        tokens = [token for token in re.split(r"\s+", str(args).strip()) if token]
        if not tokens or not re.fullmatch(r"\d{6,20}", tokens[0]):
            yield event.plain_result(
                "用法：`/d2 单场 <比赛ID> [焦点玩家]`\n"
                "例如：`/d2 单场 8989601141`\n"
                "多位焦点：`/d2 单场 8989601141 张三、李四、王五`"
                "（昵称或账号 ID，用 、/, 分隔；昵称可以含空格）\n"
                "比赛 ID 可以从 `/d2 战绩` 的结果中获取，或直接使用 Dota 客户端的比赛编号。"
            )
            return

        match_id = int(tokens[0])
        rest = " ".join(tokens[1:])

        yield event.plain_result(
            f"⏳ 正在拉取比赛 {match_id} 的详细数据并生成复盘，请稍候…"
        )

        try:
            match = await self.api.get_match(match_id)
            if not match:
                yield event.plain_result(
                    f"❌ 找不到比赛 {match_id}，或该比赛尚未被 OpenDota 收录。"
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

        parsed = OpenDotaClient.is_parsed(match)
        headline = self.match_headline(match, heroes, focus_ids or None, parsed)

        # 附上焦点玩家近期的整体状态，让复盘更有上下文。
        # 焦点可能不止一位，逐位取；超过上限的只做本场复盘，避免提示词被撑爆。
        extra_context = ""
        recent_count = max(0, int(self.cfg("watch_match_analysis_count", 10)))
        if focus_ids and recent_count:
            blocks: list[str] = []
            for focus_id in focus_ids[:MAX_FOCUS_RECENT_CONTEXT]:
                label = focus_names.get(focus_id) or focus_id
                try:
                    recent_matches, economy_samples = (
                        await self.api.get_player_matches_enriched(
                            focus_id, recent_count
                        )
                    )
                    recent_matches = [
                        m
                        for m in recent_matches
                        if int(m.get("match_id") or 0) != match_id
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
            if blocks:
                extra_context = "\n\n".join(blocks) + (
                    "\n\n请在报告最后额外增加一节「## 近期状态」"
                    + (
                        "，为上面每位焦点玩家各起一个小标题，"
                        "各用 3 句以内说明其最近的竞技走向。"
                        if len(focus_ids) > 1
                        else "，用 3 句以内说明这名玩家最近的整体竞技走向。"
                    )
                )

        prompt = build_single_match_analysis_prompt(
            match=match,
            heroes=heroes,
            items=items,
            focus_account_ids=focus_ids or None,
            focus_names=focus_names or None,
            extra_context=extra_context,
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
                )
            )
            for chunk in self._chunk_text(raw):
                yield event.plain_result(chunk)

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

    @d2.command("unwatch", alias={"取消监听", "取消订阅"})
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
                "· `/d2 取消监听 全部` 取消你添加的所有监听"
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
            lines.append(f"⏳ 当前有 {len(pending)} 场比赛正在等待详细数据：")
            for item in pending[:5]:
                focus_label = "、".join(
                    str(focus.get("name") or focus.get("account_id"))
                    for focus in item.get("focuses") or []
                )
                lines.append(
                    f"　　· 比赛 {item['match_id']}"
                    f"（焦点：{focus_label or '未知'}）"
                    f"　等收录 {item.get('wait_attempts', 0)} 次 / "
                    f"等解析 {item.get('parse_attempts', 0)} 次"
                )
        yield event.plain_result("\n".join(lines))

    # ==================================================================
    # 大模型调用
    # ==================================================================
    async def _generate_report(
        self, event: AstrMessageEvent, prompt: str
    ) -> str | None:
        """调用大模型生成报告。失败或未启用时返回 None。"""
        if not self.cfg("enable_llm_analysis", True):
            return None
        provider = await resolve_provider(
            self.context,
            event.unified_msg_origin,
            str(self.cfg("llm_provider_id", "") or ""),
        )
        if provider is None:
            logger.warning("[dota2] 没有可用的模型提供商，跳过 AI 分析")
            return None
        system_prompt = str(self.cfg("analysis_system_prompt", "") or "").strip()
        if not system_prompt:
            system_prompt = DEFAULT_SYSTEM_PROMPT
        try:
            return await call_llm(provider, system_prompt, prompt)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[dota2] 调用大模型失败: {e}", exc_info=True)
            return None

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
        """把「一场比赛」加入待推送队列（等待详细数据就绪）。

        队列键就是 ``match_id``：**同一局比赛只保留一项**。当群里多位成员各自
        监听了不同的玩家、而这几位玩家又恰好打了同一局时，老实现会按
        ``(match_id, account_id)`` 拆成多份，于是同一场比赛被重复调用大模型、
        在群里刷好几份几乎一样的报告。现在他们共用一份分析与一次推送。

        每位焦点玩家在 ``focuses`` 里各占一项，以自己为焦点收集 ``targets``；
        分析时把所有人的 account_id 一起交给提示词，报告里对每个人分别给出
        深入数据，因此既不重复分析、也不会互相覆盖焦点。
        """
        item = self._pending.get(match_id)
        if item is None:
            item = {
                "key": match_id,
                "match_id": match_id,
                #: 本局中所有被监听的玩家：``[{"account_id", "name", "targets"}]``
                "focuses": [],
                #: 比赛还没被 OpenDota 收录的次数（不消耗解析配额）
                "wait_attempts": 0,
                #: 已收录但还没有逐分钟解析数据的次数
                "parse_attempts": 0,
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
        # 只提交一次解析申请（解析接口按 10 倍额度计费）、只调用一次大模型。
        match_cache: dict[int, dict | None] = {}
        parse_requested: set[int] = set()

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
                await self._try_deliver(item, match_cache, parse_requested)
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
        parse_requested: set[int],
    ) -> None:
        """尝试获取详细数据并推送；数据未就绪时安排下次重试。"""
        match_id = int(item["match_id"])
        require_parsed = bool(self.cfg("watch_require_parsed", True))
        max_parse = max(1, int(self.cfg("watch_max_parse_attempts", 20)))
        max_wait = max(1, int(self.cfg("watch_max_wait_attempts", 30)))

        if match_id in match_cache:
            match = match_cache[match_id]
        else:
            match = await self.api.get_match(match_id)
            match_cache[match_id] = match

        # ---- 情况一：OpenDota 尚未收录这场比赛 ----
        # 这一阶段不能消耗「等待解析」的次数：否则等比赛终于被收录时，
        # 解析次数早已用光，而且解析申请从头到尾都不会被发出去。
        if not match:
            item["wait_attempts"] += 1
            waited = item["wait_attempts"]
            if waited > max_wait:
                logger.warning(
                    f"[dota2] 比赛 {match_id} 等待 OpenDota 收录超过 {max_wait} 次，"
                    f"放弃推送"
                )
                await self._finish(item, delivered_umos=set(), force_advance=True)
                return
            delay = 60 if waited <= 5 else 180
            item["next_try_at"] = time.time() + delay
            logger.info(
                f"[dota2] 比赛 {match_id} 尚未被 OpenDota 收录"
                f"（第 {waited}/{max_wait} 次），{delay}s 后重试"
            )
            return

        parsed = OpenDotaClient.is_parsed(match)

        # ---- 情况二：已收录，但还没有逐分钟级别的解析数据 ----
        if require_parsed and not parsed:
            item["parse_attempts"] += 1
            attempts = item["parse_attempts"]

            # 只要观察到「已收录但未解析」就节流地申请解析。
            # 之前只在第 1 次尝试时申请，如果那会儿比赛还没被收录，就再也不会申请了。
            if match_id not in parse_requested and (
                attempts == 1 or attempts % REPARSE_REQUEST_EVERY == 0
            ):
                parse_requested.add(match_id)
                granted = await self.api.request_parse(match_id)
                logger.info(
                    f"[dota2] 已向 OpenDota 提交解析任务 {match_id}（第 {attempts} 次观察）："
                    f"{'受理' if granted else '未受理'}"
                )

            if attempts < max_parse:
                # 前几次尝试间隔短一些，之后拉长
                delay = 60 if attempts < 5 else 180
                item["next_try_at"] = time.time() + delay
                logger.info(
                    f"[dota2] 比赛 {match_id} 尚无详细数据"
                    f"（第 {attempts}/{max_parse} 次），{delay}s 后重试"
                )
                return

            if not self.cfg("watch_fallback_unparsed", True):
                logger.info(f"[dota2] 比赛 {match_id} 等待解析超时，按配置放弃推送")
                await self._finish(item, delivered_umos=set(), force_advance=True)
                return
            logger.info(f"[dota2] 比赛 {match_id} 等待解析超时，改用基础数据推送")

        delivered = await self._deliver(item, match, parsed)
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

    async def _deliver(self, item: dict, match: dict, parsed: bool) -> set[str]:
        """生成分析并推送到所有目标会话。

        **一次调用只生成一份大模型报告**，即使本局有多位被监听的玩家参战：
        报告里会为每位焦点玩家各给一段深入数据，按会话聚合后每个会话只推送
        一条（同一会话里的多位添加者会在 @ 时一并带上）。

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
        # 这一局只应收到一条推送，@ 时把该会话里的添加者都带上。
        per_umo: dict[str, dict] = {}
        for focus in focuses:
            for target in focus.get("targets") or []:
                umo = str(target.get("umo") or "")
                if not umo:
                    continue
                aggregated = per_umo.get(umo)
                if aggregated is None:
                    aggregated = {
                        "umo": umo,
                        "platform": target.get("platform") or "",
                        "personaname": target.get("personaname") or "",
                        "creators": [],
                    }
                    per_umo[umo] = aggregated
                creator = str(target.get("created_by") or "")
                if creator and creator not in aggregated["creators"]:
                    aggregated["creators"].append(creator)
        targets = list(per_umo.values())
        if not targets:
            return set()

        try:
            heroes = await self.api.get_heroes()
            items_const = await self.api.get_items()
        except OpenDotaError as e:
            logger.error(f"[dota2] 推送比赛 {match_id} 时拉取常量失败：{e}")
            heroes, items_const = {}, {}

        # 附上焦点玩家近期的整体状态，让报告更有上下文。
        # 焦点玩家可能不止一位，这里逐位取；超过上限的只做本场复盘，
        # 否则统计块会把提示词撑得过长。
        extra_blocks: list[str] = []
        recent_count = max(0, int(self.cfg("watch_match_analysis_count", 10)))
        if recent_count:
            for focus_id in focus_ids[:MAX_FOCUS_RECENT_CONTEXT]:
                label = focus_names.get(focus_id) or focus_id
                try:
                    recent_matches, economy_samples = (
                        await self.api.get_player_matches_enriched(
                            focus_id, recent_count
                        )
                    )
                    recent_matches = [
                        m
                        for m in recent_matches
                        if int(m.get("match_id") or 0) != match_id
                    ]
                except OpenDotaError as e:
                    logger.debug(f"[dota2] 获取 {focus_id} 近期状态上下文失败：{e}")
                    continue
                if not recent_matches:
                    continue
                summary = summarize_matches(
                    recent_matches, economy_samples=economy_samples
                )
                extra_blocks.append(
                    f"—— {label}（account_id={focus_id}）在本场之外最近 "
                    f"{len(recent_matches)} 场的整体情况"
                    f"（用于判断本场是他的正常发挥还是异常）：\n"
                    + format_summary_block(summary, heroes)
                )
            if len(focus_ids) > MAX_FOCUS_RECENT_CONTEXT:
                logger.debug(
                    f"[dota2] 比赛 {match_id} 焦点玩家较多，仅前 "
                    f"{MAX_FOCUS_RECENT_CONTEXT} 位附带近期状态"
                )

        recent_tail = (
            "\n\n请在报告最后额外增加一节「## 近期状态」"
            + (
                "，为上面每位焦点玩家各起一个小标题，各用 3 句以内说明其最近的竞技走向。"
                if len(focus_ids) > 1
                else "，用 3 句以内说明这名玩家最近的整体竞技走向。"
            )
        )
        extra_context = "\n\n".join(extra_blocks) + (recent_tail if extra_blocks else "")

        prompt = build_single_match_analysis_prompt(
            match=match,
            heroes=heroes,
            items=items_const,
            focus_account_ids=focus_ids,
            focus_names=focus_names,
            extra_context=extra_context,
        )

        headline = self.match_headline(match, heroes, focus_ids, parsed)

        # 大模型报告只生成一次，多个会话复用（避免重复消耗配额）
        report: str | None = None
        first_umo = targets[0]["umo"]
        if self.cfg("enable_llm_analysis", True):
            provider = await resolve_provider(
                self.context, first_umo, str(self.cfg("llm_provider_id", "") or "")
            )
            if provider is not None:
                system_prompt = str(
                    self.cfg("analysis_system_prompt", "") or ""
                ).strip() or DEFAULT_SYSTEM_PROMPT
                try:
                    report = await call_llm(provider, system_prompt, prompt)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"[dota2] 监听推送生成分析失败：{e}")

        delivered: set[str] = set()
        for target in targets:
            umo = target["umo"]
            try:
                ok = await self._send_to_session(umo, target, headline)
                if report:
                    image_url = await self._render_image(report)
                    if image_url:
                        if await self._send(umo, MessageChain().file_image(image_url)) is False:
                            ok = False
                    else:
                        for chunk in self._chunk_text(report):
                            if (
                                await self._send(umo, MessageChain().message(chunk))
                                is False
                            ):
                                ok = False
                else:
                    if await self._send(
                        umo,
                        MessageChain().message(
                            "⚠️ 未启用大模型分析或模型不可用，仅提供上述比赛概览。"
                        ),
                    ) is False:
                        ok = False

                if ok:
                    delivered.add(umo)
                    logger.info(f"[dota2] 比赛 {match_id} 分析已推送到 {umo}")
                else:
                    logger.warning(
                        f"[dota2] 向 {umo} 推送比赛 {match_id} 未成功送达，稍后重试"
                    )
            except Exception as e:  # noqa: BLE001
                logger.error(f"[dota2] 向 {umo} 推送比赛 {match_id} 失败：{e}")
        return delivered

    async def _send(self, umo: str, chain: MessageChain) -> bool | None:
        """发送单条消息并归一化返回值（不同版本 AstrBot 的返回契约不一致）。"""
        return await self.context.send_message(umo, chain)

    async def _send_to_session(self, umo: str, target: dict, text: str) -> bool:
        """发送推送消息，支持在群聊中 @ 添加监听的人。

        ``target["creators"]`` 是该会话里所有添加过监听的人：同一局比赛可能
        同时被群里两个人关注（各自监听了不同的玩家），此时把他们都 @ 上。

        Returns:
            是否全部发送成功。失败时调用方会保留该监听者的基线以便补推。
        """
        use_at = bool(self.cfg("watch_notify_at", True))
        platform = str(target.get("platform") or "")
        creators = [
            str(uid)
            for uid in (target.get("creators") or [])
            if str(uid)
        ]
        if not creators and target.get("created_by"):
            creators = [str(target["created_by"])]

        if use_at and creators and platform == "aiocqhttp":
            try:
                from astrbot.api.message_components import At, Plain

                chain = MessageChain(
                    chain=[
                        *[At(qq=uid) for uid in creators[:5]],
                        Plain(text=" " + text),
                    ]
                )
                if await self._send(umo, chain) is not False:
                    return True
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[dota2] 构造 At 消息失败，回退为纯文本：{e}")

        ok = True
        for chunk in self._chunk_text(text):
            if await self._send(umo, MessageChain().message(chunk)) is False:
                ok = False
        return ok

    @staticmethod
    def match_headline(
        match: dict,
        heroes: dict[int, dict],
        focus_account_ids: int | list[int] | tuple[int, ...] | None = None,
        parsed: bool = True,
    ) -> str:
        """生成比赛概览头部文本。

        ``focus_account_ids`` 可以是一位或多位焦点玩家，多位时逐个列出。
        """
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

        lines.append(
            f"数据完整度：{'已解析（含逐分钟经济、团战与出装日志）' if parsed else '未解析（仅基础统计）'}"
        )
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
        await self.api.close()
        logger.info("[dota2] 插件已卸载，监听任务已停止")
