"""分析任务登记表：让用户随时查「我那份复盘到底还在跑吗」。

## 为什么需要它

复盘是**分钟级**的后台任务：等录像解析十分钟、模型再写两三分钟。此前这条
链路上唯一的信号是零散的进度消息 —— 用户看到「已开始」之后，如果后面一条
消息都没来，他无法区分三件事：

1. 还在跑，只是慢；
2. 早就失败了（发送失败、模型超时、解析不可用）；
3. 任务根本丢了（插件重载、进程重启、异常被吞）。

第三种最伤：等了十几分钟，回头一看什么都没有，只能重新发起、再等一遍。
2026-11-10 用户明确要求「让用户能查看分析是否在跑」，这个模块就是那份台账。

## 设计取舍

* **key 与去重表同构**：``match|<umo>|<比赛ID>`` / ``fight|<umo>|<比赛ID>|<第N波>``。
  同一场比赛只可能有一个任务在跑（去重表保证了这点），所以 key 可以直接
  当作主键，查询时不必再按字段拼条件。
* **阶段靠 ContextVar 传播**：生成链路上有六七层函数（``_generate_two_step``
  → ``_call_report_llm`` → ``_deliver_report`` …），层层加一个 ``job_key``
  参数会污染每一个签名。``contextvars`` 在 ``create_task`` 时按任务复制，
  于是任务体内设一次，下游任何 ``await`` 都能读到自己的那一份，互不串台。
* **落盘**：进程重启等于内存清零，而「重启导致任务没了」恰恰是最需要
  被看见的情况。只持久化到 ``jobs.json``（几 KB，任务开始/结束时各写一次），
  启动时把上次遗留的 ``running`` 记录标成「中断」，用户就能查到
  「17:02 开始的那次复盘，插件 17:10 重启过，没有结果」。
"""

from __future__ import annotations

import json
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RUNNING = "running"
DONE = "done"
FAILED = "failed"
#: 任务在跑，但承载它的 Task 已经结束却没走收尾 —— 插件重载、进程重启、
#: 或被别的代码取消。与 ``FAILED`` 分开是因为**责任不在模型/数据源**，
#: 用户该做的事也不同（重新发起，而不是去修配置）。
LOST = "lost"

#: 已完成记录保留多久（秒）。用户「等完发现没消息」再来查，一般就在这窗口内。
RETENTION = 1800.0
#: 落盘/内存里最多保留多少条（含正在跑的）。
MAX_RECORDS = 64
#: 每个任务保留多少个阶段点，够还原「卡在哪一步」就行。
MAX_STAGES = 24

_current_key: ContextVar[str] = ContextVar("dota2_job_key", default="")


def current_key() -> str:
    """当前协程所属任务的 key（不在任务里时为空串）。"""
    return _current_key.get()


def use_key(key: str) -> None:
    """把当前协程绑定到某个任务。

    ``asyncio.create_task`` 会复制调用方的 context，所以在这里设一次，
    任务体内部**所有**下游调用都能读到；任务结束不会影响别的任务。
    """
    _current_key.set(key)


def fmt_span(seconds: float) -> str:
    """把秒数说成人话：``48 秒`` / ``4 分 12 秒`` / ``1 小时 3 分``。"""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total} 秒"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} 分 {sec:02d} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes:02d} 分"


@dataclass
class Job:
    """一个后台分析任务的台账。"""

    key: str
    kind: str
    title: str
    umo: str
    started: float
    uid: str = ""
    match_id: int = 0
    status: str = RUNNING
    stage: str = ""
    stages: list[tuple[float, str]] = field(default_factory=list)
    error: str = ""
    note: str = ""
    finished: float = 0.0
    #: 承载这个任务的 ``asyncio.Task``。**不落盘**，只用于「任务还在不在」的自检。
    task: Any = None

    def mark_stage(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.stage = text
        self.stages.append((time.time(), text))
        if len(self.stages) > MAX_STAGES:
            del self.stages[: len(self.stages) - MAX_STAGES]

    @property
    def elapsed(self) -> float:
        end = self.finished if self.status != RUNNING else time.time()
        return max(0.0, end - self.started)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "kind": self.kind,
            "title": self.title,
            "umo": self.umo,
            "uid": self.uid,
            "match_id": int(self.match_id or 0),
            "started": self.started,
            "status": self.status,
            "stage": self.stage,
            "stages": [[ts, text] for ts, text in self.stages],
            "error": self.error,
            "note": self.note,
            "finished": self.finished,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        raw_stages = data.get("stages") or []
        stages: list[tuple[float, str]] = []
        for item in raw_stages:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                try:
                    stages.append((float(item[0]), str(item[1])))
                except (TypeError, ValueError):
                    continue
        return cls(
            key=str(data.get("key") or ""),
            kind=str(data.get("kind") or "分析"),
            title=str(data.get("title") or ""),
            umo=str(data.get("umo") or ""),
            started=float(data.get("started") or 0.0),
            uid=str(data.get("uid") or ""),
            match_id=int(data.get("match_id") or 0),
            status=str(data.get("status") or RUNNING),
            stage=str(data.get("stage") or ""),
            stages=stages,
            error=str(data.get("error") or ""),
            note=str(data.get("note") or ""),
            finished=float(data.get("finished") or 0.0),
        )


class JobRegistry:
    """任务台账：登记、推进阶段、收尾、以及渲染给用户看的那段文字。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else None
        self._jobs: dict[str, Job] = {}

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def load(self) -> None:
        """读回上次的记录，并把遗留的 ``running`` 标成「中断」。

        这一步是「重启后还能查到丢了的任务」的全部实现：内存里没有正在跑的
        任务，但文件里那条 ``running`` 说明它**曾经**在跑、且没走完收尾。
        """
        self._jobs.clear()
        if self.path is None or not self.path.exists():
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
        except Exception:  # noqa: BLE001 - 台账读不出来绝不能影响插件启动
            return
        items = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                job = Job.from_dict(item)
            except Exception:  # noqa: BLE001
                continue
            if not job.key:
                continue
            if job.status == RUNNING:
                job.status = LOST
                job.finished = job.finished or time.time()
                job.error = job.error or "插件重启，这个任务没有走完"
            self._jobs[job.key] = job
        self.prune()

    def save(self) -> None:
        """把台账写盘（原子替换）。失败只吞掉 —— 台账不该影响分析本身。"""
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "jobs": [job.to_dict() for job in self._jobs.values()],
            }
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # 登记 / 推进 / 收尾
    # ------------------------------------------------------------------
    def begin(
        self,
        *,
        key: str,
        kind: str,
        title: str,
        umo: str,
        uid: str = "",
        match_id: int = 0,
        stage: str = "已受理",
    ) -> Job:
        """登记一个新任务，并把它设为**当前协程**的任务。"""
        now = time.time()
        job = Job(
            key=str(key),
            kind=str(kind),
            title=str(title),
            umo=str(umo),
            uid=str(uid),
            match_id=int(match_id or 0),
            started=now,
        )
        job.mark_stage(stage)
        self._jobs[job.key] = job
        use_key(job.key)
        self.prune(keep_key=job.key)
        self.save()
        return job

    def stage(self, text: str, *, key: str | None = None) -> None:
        """推进阶段。不传 key 时用当前协程绑定的那个任务。

        阶段变化**不落盘**：一分钟一条的话写盘次数太多，而重启后用户真正
        需要知道的是「这个任务没跑完」，不是它卡在第几条消息上。
        """
        job = self._jobs.get(key or current_key())
        if job is None or job.status != RUNNING:
            return
        job.mark_stage(text)

    def finish(
        self,
        *,
        ok: bool = True,
        error: str = "",
        note: str = "",
        key: str | None = None,
    ) -> None:
        """收尾。成功记耗时，失败记原因。"""
        job = self._jobs.get(key or current_key())
        if job is None:
            return
        if job.status != RUNNING:
            # 已经收过尾（比如 done_callback 先跑、任务体后跑）就不覆盖：
            # 先到的那条带了更具体的原因。
            return
        job.status = DONE if ok else FAILED
        job.finished = time.time()
        job.error = (error or "").strip()
        job.note = (note or "").strip()
        if not ok and not job.error:
            job.error = "任务没有正常结束"
        if current_key() == job.key:
            # 解绑：同一个协程可能接着干别的事（比如指令 handler 收尾），
            # 留着绑定会让后面的阶段更新写进一个已经结束的任务里。
            use_key("")
        self.save()

    def lose(self, *, reason: str, key: str | None = None) -> None:
        """标记「任务没走完就没了」。"""
        job = self._jobs.get(key or current_key())
        if job is None or job.status != RUNNING:
            return
        job.status = LOST
        job.finished = time.time()
        job.error = reason
        self.save()

    def watch(self, key: str, task: Any) -> None:
        """盯住承载任务的 ``asyncio.Task``，异常/取消时自动记账。

        任务体里的人肉 ``finish`` 是**正常路径**；这里管的是没走到那一步的
        情况 —— 抛异常、被取消（插件重载会 cancel 所有任务）、或者提前
        ``return`` 却没登记结果。
        """
        job = self._jobs.get(key)
        if job is None:
            return
        job.task = task

        def _on_done(done: Any) -> None:
            current = self._jobs.get(key)
            if current is None or current.status != RUNNING:
                return
            if getattr(done, "cancelled", lambda: False)():
                self.lose(reason="任务被取消（插件重载或停止）", key=key)
                return
            exc = None
            try:
                exc = done.exception()
            except Exception:  # noqa: BLE001 - 取不出来就当作没有
                exc = None
            if exc is not None:
                self.lose(reason=f"任务异常退出：{exc}", key=key)
                return
            # 正常返回却没登记结果：说明这条路径忘了收尾，如实标「无结果」，
            # 总比让用户以为它还在跑要好。
            self.lose(reason="任务已结束，但没有发出结果（被提前跳过）", key=key)

        try:
            task.add_done_callback(_on_done)
        except Exception:  # noqa: BLE001 - 非 Task 对象（测试桩）时忽略
            pass

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get(self, key: str) -> Job | None:
        return self._jobs.get(key)

    def active(self, umo: str) -> list[Job]:
        self._refresh_lost()
        return [
            job
            for job in self._jobs.values()
            if job.umo == umo and job.status == RUNNING
        ]

    def recent(self, umo: str, limit: int = 3) -> list[Job]:
        self._refresh_lost()
        finished = [
            job
            for job in self._jobs.values()
            if job.umo == umo and job.status != RUNNING
        ]
        finished.sort(key=lambda j: j.finished or j.started, reverse=True)
        return finished[:limit]

    def _refresh_lost(self) -> None:
        """兜一次「Task 已经不在了，但台账还写着 running」。"""
        for job in list(self._jobs.values()):
            if job.status != RUNNING:
                continue
            task = job.task
            if task is None:
                continue
            try:
                if not task.done():
                    continue
            except Exception:  # noqa: BLE001
                continue
            self.lose(reason="任务已结束，但没有发出结果", key=job.key)

    def prune(self, *, keep_key: str = "", now: float | None = None) -> None:
        """清掉过期的已完成记录，并保证总条数不超过上限。"""
        moment = now if now is not None else time.time()
        for key in list(self._jobs.keys()):
            job = self._jobs[key]
            if job.status == RUNNING:
                continue
            if key == keep_key:
                continue
            if moment - (job.finished or job.started) > RETENTION:
                self._jobs.pop(key, None)
        overflow = len(self._jobs) - MAX_RECORDS
        if overflow > 0:
            finished = sorted(
                (job for job in self._jobs.values() if job.status != RUNNING),
                key=lambda j: j.finished or j.started,
            )
            for job in finished[:overflow]:
                self._jobs.pop(job.key, None)

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def render(self, umo: str, *, limit: int = 3) -> str:
        """给用户看的那段文字（``/d2 进度`` 的正文）。"""
        self._refresh_lost()
        running = sorted(self.active(umo), key=lambda j: j.started)
        recent = self.recent(umo, limit=limit)

        if not running and not recent:
            return (
                "本会话现在没有在跑的分析任务。\n"
                "发起复盘之后，随时可以用 `/d2 进度` 看它到哪一步了。"
            )

        lines: list[str] = []
        if running:
            lines.append(f"📋 在跑的任务（{len(running)} 个）")
            for job in running:
                lines.append("")
                lines.append(f"⏳ {job.kind} · {job.title}")
                lines.append(
                    f"   已跑 {fmt_span(job.elapsed)}"
                    + (f"｜当前：{job.stage}" if job.stage else "")
                )
            lines.append("")
            lines.append(
                "还在跑就不用管，结果会自动发到本会话。"
                "要是这里显示的时间停住不动超过十几分钟，把它发给我看看。"
            )
        else:
            lines.append("现在没有在跑的分析任务。")

        if recent:
            if lines:
                lines.append("")
            lines.append("最近结束：")
            for job in recent:
                icon = "✅" if job.status == DONE else "⚠️"
                head = f"· {icon} {job.kind} · {job.title}｜{fmt_span(job.elapsed)}"
                if job.status == DONE:
                    lines.append(head)
                    # 「已发出」是废话（成功了当然发了），真正有价值的是
                    # 「解析没等完、用的是旧数据」这种补偿说明。
                    if job.note and "已发出" not in job.note:
                        lines.append(f"   {job.note}")
                else:
                    reason = job.error or job.note or "没有结果"
                    lines.append(f"{head}\n   {reason}")
        return "\n".join(lines)
