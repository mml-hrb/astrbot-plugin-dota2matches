"""解析状态判断与「催解析 + 等待」能力。

背景
----
OpenDota 对一场比赛的收录与解析是两件独立的事：

* **收录**：只要有人在 Dota 客户端里打过这局、且数据可见，OpenDota 很快
  就会有一条 ``/matches/{id}`` 记录——但内容只有 KDA、英雄、时长这类基础字段；
* **解析**：只有解析完成（replay 被下载并逐帧处理）才会有 ``gold_t`` /
  ``xp_t`` / ``teamfights`` / ``chat`` 这些逐分钟数据。AI 复盘的价值几乎
  全部来自这一层。

判断是否已解析的正确口径见 :func:`parse_state`：``od_data`` 是**状态对象**，
存在并不代表解析完成，必须看 ``has_parsed`` 或玩家级 ``gold_t``。

⚠️ 不过 :func:`parse_state` 自己**不再判**这件事（P1-5，2026-10-14 审查）——
它把结论整个委托给 :func:`dota_format.parsed_state`，只额外填出
:class:`ParseState` 里的描述性字段。历史上这里另有一套判据，和报告正文
用的那套**真的分叉了**（STRATZ 链路上「等待流程说未解析、报告标题说已解析」），
详见该函数的 docstring。

未解析时可以用 ``POST /request/{match_id}`` 主动催，之后只能轮询等待。
本模块把「催 + 等」封装成 :func:`wait_for_parse`，调用方拿到
:class:`ParseWaitResult` 再决定是继续分析还是放弃。

**超时不等于放弃**：调用方应该在 :data:`FALLBACK_REASONS` 覆盖的结束原因下，
用 ``result.match``（超时那一刻手上的那份数据）继续出「未解析版」报告——
用户要的是「别干等十分钟」，而不是「十分钟后什么都没有」。
:meth:`ParseWaitResult.fallback_text` 就是这种情况的过渡文案。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

# ======================================================================
# 默认节奏
# ======================================================================
#: 催解析之后，每隔多久查一次解析状态（秒）。用户要求「一分钟查询一次」。
DEFAULT_CHECK_INTERVAL = 60

#: 从提交催解析开始，最多等待多久（秒）。用户要求「十分钟后放弃」。
DEFAULT_WAIT_TIMEOUT = 600

#: 每隔多少次轮询重新提交一次催解析申请。
#: OpenDota 的解析任务偶有丢单，只申请一次不够稳；但 ``/request`` 按 10 倍
#: 额度计费，所以间隔要拉长（默认 5 次 ≈ 5 分钟再补一次）。
RESUBMIT_EVERY = 5

#: 单次状态查询之间的最小间隔（秒），防止调用方传入过小的值打爆配额。
MIN_CHECK_INTERVAL = 20

#: 允许「超时后自动降级为未解析版报告」的结束原因。
#:
#: 只放 ``timeout`` —— 用户要的是「等到十分钟还没解析就不要再干等」，
#: 而不是「无论什么情况都硬出一份报告」：
#:
#: * ``cancelled``：用户主动放弃等待，此时再自作主张出报告是违背意图的；
#: * ``unavailable`` / ``error``：连比赛数据都拿不到，也无从降级。
FALLBACK_REASONS = frozenset({"timeout"})


@dataclass
class ParseState:
    """一场比赛的解析状态快照。"""

    #: 是否已经解析完成（含逐分钟数据）
    parsed: bool = False
    #: OpenDota 是否已经收录这场比赛
    known: bool = False
    #: ``od_data.has_api``：有基础 API 记录
    has_api: bool = False
    #: ``od_data.has_gcdata``：拿到过游戏客户端 GC 数据
    has_gcdata: bool = False
    #: ``od_data.has_parsed``：OpenDota 官方「已解析」标记
    has_parsed: bool = False
    #: ``od_data.has_archive``：有可回填的存档
    has_archive: bool = False

    def describe(self) -> str:
        """给用户看的一行状态说明。"""
        if self.parsed:
            return "已解析（含逐分钟经济、团战与出装日志）"
        if not self.known:
            return "还没有被 OpenDota 收录"
        if not self.has_gcdata and not self.has_archive:
            return "已收录但未解析（OpenDota 尚未取到该局的录像数据）"
        if not self.has_gcdata:
            return "已收录但未解析（缺少游戏客户端数据）"
        return "已收录但未解析"


@dataclass
class ParseWaitResult:
    """一次「催 + 等」的最终结果。"""

    #: 比赛 ID
    match_id: int = 0
    #: 是否等到了解析完成
    parsed: bool = False
    #: 结束原因：parsed | timeout | cancelled | unavailable | error
    reason: str = "timeout"
    #: 实际等待了多少秒
    waited: float = 0.0
    #: 提交催解析的结果（True 表示 OpenDota 返回了 job，真的排上队）
    submitted: bool = False
    #: 一共查了多少次解析状态
    checks: int = 0
    #: 最终拿到的比赛数据（``parsed`` 为真时有值，超时时可能为最新一份）
    match: dict | None = None
    #: 失败原因（``reason == "error"`` 时有值）
    error: str = ""

    def fail_text(self, match_id: int | None = None) -> str:
        """超时 / 取消时给用户的收尾文案。"""
        mid = match_id or self.match_id
        minutes = self.waited / 60
        if self.reason == "cancelled":
            return f"🛑 已取消等待比赛 {mid} 的解析。"
        if self.reason == "unavailable":
            return (
                f"❌ 比赛 {mid} 拿不到数据，可能已被删除或 OpenDota 收录异常。"
            )
        return (
            f"⌛ 已等待 {minutes:.0f} 分钟，比赛 {mid} 仍未完成解析，放弃等待。\n\n"
            "OpenDota 的解析队列较长时可能超过 10 分钟。你可以：\n"
            "· 稍后再用 `/d2 单场 " + str(mid) + "` 试一次（那时可能已经解析好）；\n"
            "· 或先用 `/d2 单场 " + str(mid) + " skip` 看基础数据版的复盘。"
        )

    def fallback_text(
        self, match_id: int | None = None, *, limit_minutes: int = 0
    ) -> str:
        """放弃等待、**改用现有数据出报告**时的过渡文案。

        与 :meth:`fail_text` 的区别：那边是「什么都不出了」，
        这边是「不等了，但还是给你一份能看的东西」。

        Args:
            match_id: 比赛 ID，缺省用 ``self.match_id``。
            limit_minutes: 配置的等待上限（分钟）。给 0 时按实际等待时长估算。
        """
        mid = match_id or self.match_id
        limit = limit_minutes or max(1, round(self.waited / 60))
        return (
            f"⌛ 已等待 {limit} 分钟，比赛 {mid} 仍未完成解析，不再继续等解析了。\n"
            "已自动改用现有数据生成复盘报告"
            "（没有逐分钟经济、团战与出装日志，深度会明显弱于完整版）。\n"
            f"· 稍后想补完整复盘：等 OpenDota 解析好之后再发一次 "
            f"`/d2 单场 {mid}` 即可。"
        )


def _as_bool(value: Any) -> bool:
    return value is True


def _parsed_state_of(match: dict) -> tuple[bool, str]:
    """转调 :func:`dota_format.parsed_state`（本插件唯一的解析判据）。

    单独包一层是为了处理导入方式：``dota_format`` 是纯函数模块、不反向依赖
    本模块，因此不存在循环导入；但本仓库同时存在「包内相对导入」与
    「AstrBot 以模块方式加载时的绝对导入」两种运行方式，照抄
    ``dota_api`` 里的同一套双分支写法。
    """
    try:
        from .dota_format import parsed_state
    except ImportError:  # 模块方式加载时的兜底
        from dota_format import parsed_state  # type: ignore[no-redef]

    return parsed_state(match)


def parse_state(match: dict | None) -> ParseState:
    """读取一场比赛的解析状态。

    .. important::

       **「是否已解析」这个结论不再由本函数自己判**（P1-5，2026-10-14 审查）。

       本函数只负责把 :class:`ParseState` 这个**描述性**快照填出来（供
       :meth:`ParseState.describe` 给用户讲「卡在哪一步」，以及把
       ``od_data`` 四字段透出来）；**唯一**的判据是
       :func:`dota_format.parsed_state`，直接调它。

       为什么必须这样（这不是洁癖）：两套判据历史上已经**真的分叉**了 ——

       * ``dota_format`` 的 ``_match_has_timeseries`` 同时认
         **STRATZ 的比赛级曲线**（``radiant_gold_adv`` / ``radiant_xp_adv``）
         与 OpenDota 的玩家级 ``gold_t``；
       * 本函数原先只看 ``gold_t``，外加一层「顶层 ``obj`` / ``chat`` /
         ``teamfights`` 非空也算解析」。

       于是默认的 **STRATZ 链路上**，等待流程（用旧判据）会认为「还没解析」
       而一直催、一直等，最后超时降级出一份「未解析版」报告；而报告正文
       的标题行（用新判据）却写着「已解析（32 分钟）」—— 同一份报告里
       两条自相矛盾的信息。

       反向的分叉同样存在：``obj`` / ``chat`` 这种**解析副产物**在
       OpenDota 上偶尔先于 ``gold_t`` 出现，旧判据会把「曲线还没来」
       说成「已解析」，等待流程提前收工，报告里没有逐分钟数据。
    """
    state = ParseState()
    if not isinstance(match, dict) or not match:
        return state

    if not match.get("players"):
        # 没有 players 说明这条记录还没成形成，等价于未收录
        return state
    state.known = True

    # ``od_data`` 四字段仍然透出来 —— describe() 要靠它们细分「卡在哪一步」，
    # 这是本结构**描述性**的一半，与「判不判已解析」无关。
    od_data = match.get("od_data")
    if isinstance(od_data, dict):
        state.has_api = _as_bool(od_data.get("has_api"))
        state.has_gcdata = _as_bool(od_data.get("has_gcdata"))
        state.has_parsed = _as_bool(od_data.get("has_parsed"))
        state.has_archive = _as_bool(od_data.get("has_archive"))

    # 判据单一来源：与本插件所有其他出口（标题行、提示词正文）逐字同口径。
    parsed, _ = _parsed_state_of(match)
    state.parsed = bool(parsed)
    return state


async def submit_parse(api: Any, match_id: int) -> bool:
    """向 OpenDota 提交一次解析申请（催解析）。

    返回 OpenDota 是否返回了 ``job``（真的排上队）。接口不可用时不抛异常，
    只记日志并返回 False——催解析失败不该让整个流程崩掉。
    """
    submitter = getattr(api, "request_parse", None)
    if not callable(submitter):
        return False
    try:
        return bool(await submitter(int(match_id)))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - 催解析是尽力而为
        return False


async def wait_for_parse(
    api: Any,
    match_id: int,
    *,
    check_interval: int = DEFAULT_CHECK_INTERVAL,
    timeout: int = DEFAULT_WAIT_TIMEOUT,
    submit: bool = True,
    resubmit_every: int = RESUBMIT_EVERY,
    # 第三个参数是**秒数（float）**，不是整数 —— 原先这里标成 `int`，
    # 而两个调用点都传 `time.time() - started` / `result.waited`（都是 float），
    # 由 mypy 抓出（2026-10-14 第三批）。回调侧（`main._on_parse_check`）本来就
    # 声明的是 `elapsed: float`，只是这里写漏了。
    on_check: Callable[[ParseState, int, float], Awaitable[None]] | None = None,
    on_submit: Callable[[bool], Awaitable[None]] | None = None,
    cancel_event: asyncio.Event | None = None,
    first_match: dict | None = None,
) -> ParseWaitResult:
    """催解析并轮询等待解析完成。

    Args:
        api: :class:`~dota_api.OpenDotaClient` 实例。
        match_id: 比赛 ID。
        check_interval: 轮询间隔（秒），会被抬到 :data:`MIN_CHECK_INTERVAL` 以上。
        timeout: 总超时（秒），到点仍未解析则放弃。
        submit: 是否在开始时提交一次催解析申请。
        resubmit_every: 每轮询多少次重新补交一次催解析申请；``0`` 表示不补交。
        on_check: 每次轮询后的回调 ``(state, checks, elapsed_seconds)``，
            用于每分钟向用户播报进度。
        on_submit: 每次提交催解析后的回调，入参是「是否受理」。
        cancel_event: 外部取消信号（用户主动放弃时 set）。
        first_match: 调用方已经拉到的那份比赛数据，避免重复请求一次。

    Returns:
        :class:`ParseWaitResult`
    """
    result = ParseWaitResult(match_id=int(match_id))
    interval = max(MIN_CHECK_INTERVAL, int(check_interval or DEFAULT_CHECK_INTERVAL))
    deadline = time.time() + max(interval, int(timeout or DEFAULT_WAIT_TIMEOUT))
    started = time.time()

    # ---- 0. 先看看调用方给的这份数据是不是已经解析好了 ----
    if first_match:
        state = parse_state(first_match)
        # 没有 players 的「空壳记录」不能算已知，需要重新拉一次
        if state.known:
            result.match = first_match
            if state.parsed:
                result.parsed = True
                result.reason = "parsed"
                result.waited = time.time() - started
                result.checks = 1
                return result

    # ---- 1. 提交催解析 ----
    if submit:
        result.submitted = await submit_parse(api, match_id)
        if on_submit is not None:
            await on_submit(result.submitted)

    # ---- 2. 轮询等待 ----
    while True:
        if cancel_event is not None and cancel_event.is_set():
            result.reason = "cancelled"
            result.waited = time.time() - started
            return result

        if time.time() >= deadline:
            result.reason = "timeout"
            result.waited = time.time() - started
            return result

        # 睡到下一个检查点（同时能被 cancel_event 提前唤醒）
        wait_seconds = min(interval, max(0.0, deadline - time.time()))
        if await _sleep_or_cancel(wait_seconds, cancel_event):
            result.reason = "cancelled"
            result.waited = time.time() - started
            return result

        match = await _fetch_match(api, match_id)
        result.checks += 1
        if match is None:
            # 拉不到数据：可能是比赛不存在，也可能是瞬时网络问题。
            # 交给重试次数兜底，不在第一次失败时就下结论。
            if result.checks >= 3 and result.match is None and not result.submitted:
                result.reason = "unavailable"
                result.waited = time.time() - started
                return result
            if on_check is not None:
                await on_check(parse_state(None), result.checks, result.waited)
            continue

        result.match = match
        state = parse_state(match)
        if on_check is not None:
            await on_check(state, result.checks, time.time() - started)

        if state.parsed:
            result.parsed = True
            result.reason = "parsed"
            result.waited = time.time() - started
            return result

        # 仍未解析：隔一段时间补交一次申请（OpenDota 会丢单）
        if (
            submit
            and resubmit_every
            and result.checks
            and result.checks % int(resubmit_every) == 0
        ):
            again = await submit_parse(api, match_id)
            result.submitted = result.submitted or again
            if on_submit is not None:
                await on_submit(again)


async def _fetch_match(api: Any, match_id: int) -> dict | None:
    """拉取比赛数据；失败返回 ``None``（不向上抛异常）。"""
    getter = getattr(api, "get_match", None)
    if not callable(getter):
        return None
    try:
        return await getter(int(match_id))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - 网络抖动交给下一轮
        return None


async def _sleep_or_cancel(seconds: float, cancel_event: asyncio.Event | None) -> bool:
    """睡眠指定的秒数；被取消时立刻返回 True。

    不能直接用 ``asyncio.sleep``——那样用户点「取消」最多要等满一个
    60 秒的轮询周期才有反应。
    """
    if seconds <= 0:
        return bool(cancel_event is not None and cancel_event.is_set())
    if cancel_event is None:
        await asyncio.sleep(seconds)
        return False
    try:
        await asyncio.wait_for(cancel_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False
