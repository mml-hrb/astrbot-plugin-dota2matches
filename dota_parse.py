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

未解析时可以用 ``POST /request/{match_id}`` 主动催，之后只能轮询等待。
本模块把「催 + 等」封装成 :func:`wait_for_parse`，调用方拿到
:class:`ParseWaitResult` 再决定是继续分析还是放弃。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

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


def _as_bool(value: Any) -> bool:
    return value is True


def parse_state(match: dict | None) -> ParseState:
    """读取一场比赛的解析状态。

    判定顺序（任一满足即视为已解析）：

    1. ``od_data.has_parsed is True`` —— OpenDota 官方标记，最权威；
    2. 任一玩家的 ``gold_t`` 非空 —— 只有解析后才会注入的分时间线数据。

    其他字段只用于给用户描述「卡在哪一步」。
    """
    state = ParseState()
    if not isinstance(match, dict) or not match:
        return state

    if not match.get("players"):
        # 没有 players 说明这条记录还没成形成，等价于未收录
        return state
    state.known = True

    od_data = match.get("od_data")
    if isinstance(od_data, dict):
        state.has_api = _as_bool(od_data.get("has_api"))
        state.has_gcdata = _as_bool(od_data.get("has_gcdata"))
        state.has_parsed = _as_bool(od_data.get("has_parsed"))
        state.has_archive = _as_bool(od_data.get("has_archive"))

    if state.has_parsed:
        state.parsed = True
        return state

    for player in match.get("players") or []:
        if isinstance(player, dict) and player.get("gold_t"):
            state.parsed = True
            return state

    # 顶层派生数据是解析的副产物，作为最后的补充判据
    for key in ("obj", "chat", "teamfights"):
        if match.get(key):
            state.parsed = True
            return state

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
    on_check: Callable[[ParseState, int, int], Awaitable[None]] | None = None,
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
