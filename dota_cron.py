"""AstrBot「未来任务」（定时任务）桥接层。

插件不自己造一套调度器，而是**直接用 AstrBot 的 ``CronJobManager``**：
AstrBot 4.x 内置了「未来任务」能力（WebUI 里有独立页面，聊天里也能让机器人
自己建），它的调度器就是 ``astrbot.core.cron.manager.CronJobManager``。
把它用起来的好处是任务的可见性与生命周期都归平台管：建出来的任务会出现在
WebUI 的「未来任务」页面里，可以停用、改时间、删除、立即执行。

任务有两种，我们**必须**用前者：

``active_agent``
    官方给「让主智能体在某个时间醒来做事」用的。它触发时由
    ``CronJobManager._woke_main_agent`` **直接**构造并运行 AstrBot 的主智能体
    （注意是直接 ``build_main_agent`` 跑，**不走事件流水线**），也就是说：

    * 插件**无法接管**这次执行 —— 没有事件能拦，``stop_event()`` 也没用；
    * 干活的是 AstrBot 的默认模型，不是插件自带的模型通道；
    * 它拿不到插件的数据层（双数据源降级、中文英雄名、英雄池版本口径…），
      要靠模型自己调工具，而工具得先注册进主智能体。

    用它来实现「每天早上七点通报群里战绩」会得到一份**不受控**的输出。

``basic``
    插件侧真正的扩展点：``add_basic_job(handler=...)`` 把 handler 登记在
    ``CronJobManager._basic_handlers`` 里，到点由 ``_run_basic_job`` 直接调用
    （``handler(**payload)``）。**执行的是插件自己的代码**，所以数据、口径、
    模型通道、推送链路全都是插件这一套。

因此本插件一律建 ``basic`` 任务，理由见上。

.. warning::

   ``basic`` 任务的 handler 只活在内存里（``_basic_handlers``），而 AstrBot
   启动时的 ``sync_from_db()`` 对「有行、没 handler」的 basic 任务会**跳过并
   打警告**（见 ``CronJobManager.sync_from_db``）。插件加载发生在
   ``cron_manager.start()`` 之前，所以本模块的 :meth:`CronBridge.adopt`
   在插件启动时把既有任务「接管」一遍：读出行 → 删行 → 用
   ``add_basic_job`` 原样重建（时间 / 名称 / 负载都照抄），handler 就位。
   重建后 job_id 会变，这是唯一的副作用 —— 对用户不可见（列表按名称显示）。

   之所以不直接把 handler 塞进 ``_basic_handlers``：那是私有属性，跨版本
   随时可能改名或改语义。``list_jobs`` / ``delete_job`` / ``add_basic_job``
   都是公开方法，用它们能做到同样的事。

**存在的权威在数据库行**：插件不另存一份任务清单。用户在 WebUI 里删掉某个
任务，下次启动读不到这一行 → 就不会重建它 —— 删除是**真的删除**。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api import logger

#: 负载里的归属标记。凡是带这个键的任务才算本插件的，
#: 其余（用户自己在聊天里让 AstrBot 建的）一律不碰。
OWNER = "astrbot_plugin_dota2"

#: ``add_basic_job`` 的 ``persistent`` 参数。
#:
#: 用 False 是**故意的**：我们的 handler 在插件启动时重建，不依赖平台的
#: 「启动时从库里恢复」那条路；若标成 True，AstrBot 在恢复时找不到 handler
#: 会打一行警告，而我们的重建又紧跟在后面 —— 白噪。
PERSISTENT = False


def is_owned(job: Any) -> bool:
    """这个任务是不是本插件建的。"""
    payload = getattr(job, "payload", None)
    return isinstance(payload, dict) and payload.get("owner") == OWNER


class CronUnavailable(RuntimeError):
    """AstrBot 的定时任务能力不可用。

    只可能出现在两种情况下：AstrBot 版本太老没有 ``cron_manager``，
    或者它被关掉了。此时**不要**退回插件自己的定时器 —— 用户要的就是
    「能在 AstrBot 的任务页面里看到并管理」，偷偷用另一套实现只会让人找不到任务。
    """


class CronBridge:
    """与 ``CronJobManager`` 打交道的唯一出口（全部使用公开 API）。"""

    def __init__(self, context: Any) -> None:
        self._context = context

    # ------------------------------------------------------------------
    # 能力探测
    # ------------------------------------------------------------------
    @property
    def manager(self) -> Any | None:
        return getattr(self._context, "cron_manager", None)

    def unavailable_reason(self) -> str:
        """返回不可用原因；可用时返回空串。"""
        mgr = self.manager
        if mgr is None:
            return (
                "当前 AstrBot 没有提供定时任务能力（cron_manager 不存在），"
                "升级 AstrBot 到 4.x 后再试。"
            )
        if not hasattr(mgr, "add_basic_job"):
            return "当前 AstrBot 的定时任务实现不支持插件注册任务。"
        return ""

    def available(self) -> bool:
        return not self.unavailable_reason()

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    async def list_owned(self) -> list[Any]:
        """列出本插件建的全部定时任务（按名称排序）。"""
        mgr = self.manager
        if mgr is None:
            return []
        try:
            jobs = await mgr.list_jobs("basic")
        except Exception as e:  # noqa: BLE001 - 列不出任务不该拖垮命令
            logger.error(f"[dota2] 读取定时任务失败：{e}")
            return []
        owned = [job for job in jobs if is_owned(job)]
        return sorted(owned, key=lambda j: (str(getattr(j, "name", "")), str(j.job_id)))

    @staticmethod
    def job_summary(job: Any) -> dict[str, Any]:
        """把任务行整理成便于渲染的字典。"""
        payload = getattr(job, "payload", None)
        payload = payload if isinstance(payload, dict) else {}
        return {
            "job_id": str(getattr(job, "job_id", "")),
            "name": str(getattr(job, "name", "") or ""),
            "cron": str(getattr(job, "cron_expression", "") or ""),
            "enabled": bool(getattr(job, "enabled", True)),
            "session": str(payload.get("session") or ""),
            "action": str(payload.get("action") or ""),
            "args": str(payload.get("args") or ""),
            "schedule_id": str(payload.get("schedule_id") or ""),
            "next_run": getattr(job, "next_run_time", None),
        }

    def next_run_text(self, job: Any, timezone: str = "") -> str:
        """把下次执行时间渲染成本地时间文本。

        平台库里存的是 **UTC**（SQLite 没有 tz-aware 列类型，值本身是
        naive 的 UTC），因此这里要先把 UTC 标签补回去再换本地时区 ——
        直接当本地时间显示会差 8 小时。同时读一次调度器里的实时值：
        ``add_*_job`` 写库是 fire-and-forget 的，刚建完那一刻库里可能还是 None。
        """
        value = None
        mgr = self.manager
        job_id = str(getattr(job, "job_id", ""))
        if mgr is not None and job_id:
            try:
                value = mgr.get_next_run_time(job_id)
            except Exception:  # noqa: BLE001 - 老版本没有这个方法
                value = None
        if value is None:
            value = getattr(job, "next_run_time", None)
        if value is None:
            return "未知"
        if value.tzinfo is None:
            value = value.replace(tzinfo=_dt.timezone.utc)
        tzinfo = None
        if timezone:
            try:
                tzinfo = ZoneInfo(timezone)
            except ZoneInfoNotFoundError:
                tzinfo = None
        local = value.astimezone(tzinfo) if tzinfo else value.astimezone()
        return local.strftime("%Y-%m-%d %H:%M")

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    async def create(
        self,
        payload: dict[str, Any],
        *,
        name: str,
        cron_expression: str,
        handler: Callable[..., Any],
        timezone: str = "",
        enabled: bool = True,
    ) -> Any:
        """建一个 ``basic`` 定时任务（到点执行插件自己的 handler）。

        Args:
            payload: 任务负载。会被原样传回 handler（``handler(**payload)``），
                因此**每个键都必须是合法的 Python 标识符**，且 handler 要能吃下
                这些键。``owner`` 由本方法负责写入。
            name: 任务名（WebUI 里显示的就是它）。
            cron_expression: 5 段标准 cron（分 时 日 月 周）。
            handler: 到点执行的协程函数。
            timezone: 时区名，留空用系统时区。
            enabled: 建出来是否直接启用。

        Raises:
            CronUnavailable: 平台不支持。
        """
        mgr = self.manager
        reason = self.unavailable_reason()
        if mgr is None or reason:
            raise CronUnavailable(reason or "定时任务不可用")
        body = {**payload, "owner": OWNER}
        description = str(body.get("note") or name)
        return await mgr.add_basic_job(
            name=name,
            cron_expression=cron_expression,
            handler=handler,
            description=description,
            timezone=timezone or None,
            payload=body,
            enabled=enabled,
            persistent=PERSISTENT,
        )

    async def delete(self, job_id: str) -> bool:
        """删除任务。返回是否删掉了（找不到也算成功，任务本来就不在）。"""
        mgr = self.manager
        if mgr is None or not job_id:
            return False
        try:
            await mgr.delete_job(str(job_id))
            return True
        except Exception as e:  # noqa: BLE001
            logger.error(f"[dota2] 删除定时任务 {job_id} 失败：{e}")
            return False

    async def set_enabled(self, job_id: str, enabled: bool) -> bool:
        """停用 / 启用任务。

        走 ``update_job`` 而不是 ``delete + create``：``job_id`` 不变，
        ``_basic_handlers[job_id]`` 里绑好的 handler 也就原封不动 ——
        停用再启用不会把 handler 弄丢（那正是「停用后又没反应」的来源）。
        """
        mgr = self.manager
        if mgr is None or not job_id:
            return False
        try:
            return await mgr.update_job(str(job_id), enabled=bool(enabled)) is not None
        except Exception as e:  # noqa: BLE001
            logger.error(f"[dota2] 修改定时任务 {job_id} 的启用状态失败：{e}")
            return False

    async def run_now(self, job_id: str) -> bool:
        """立刻执行一次（不等下一个时间点）。"""
        mgr = self.manager
        if mgr is None or not job_id:
            return False
        try:
            await mgr.run_job_now(str(job_id))
            return True
        except Exception as e:  # noqa: BLE001
            logger.error(f"[dota2] 立即执行定时任务 {job_id} 失败：{e}")
            return False

    # ------------------------------------------------------------------
    # 启动接管
    # ------------------------------------------------------------------
    async def adopt(self, handler: Callable[..., Any]) -> tuple[int, int]:
        """插件启动时接管既有任务，返回 ``(重建数, 失败数)``。

        流程：读出行 → 删行 → 用 ``add_basic_job`` 原样重建并绑定 handler。
        名称 / cron / 时区 / 启用状态 / 负载全部照抄，因此用户在 WebUI 里
        改过的时间与启停状态**都会被保留**。

        只处理带 ``owner`` 标记的任务；用户在 WebUI 或聊天里自己建的任务
        一概不碰。相反，如果某个任务在库里已经不存在（用户删了它），这里
        自然就不会重建 —— 删除是真的删除。
        """
        mgr = self.manager
        if mgr is None or not hasattr(mgr, "add_basic_job"):
            return (0, 0)
        try:
            jobs = await self.list_owned()
        except Exception as e:  # noqa: BLE001
            logger.error(f"[dota2] 接管定时任务时读取失败：{e}")
            return (0, 0)
        if not jobs:
            return (0, 0)

        specs: list[dict[str, Any]] = []
        for job in jobs:
            payload = getattr(job, "payload", None)
            specs.append(
                {
                    "payload": dict(payload) if isinstance(payload, dict) else {},
                    "name": str(getattr(job, "name", "") or "dota2_task"),
                    "cron": str(getattr(job, "cron_expression", "") or ""),
                    "timezone": str(getattr(job, "timezone", "") or ""),
                    "enabled": bool(getattr(job, "enabled", True)),
                }
            )
            await self.delete(str(job.job_id))

        rebuilt = 0
        failed = 0
        for spec in specs:
            if not spec["cron"]:
                # 一次性任务的 cron 为空（它靠 run_at），不是我们建的形状
                failed += 1
                continue
            try:
                await self.create(
                    spec["payload"],
                    name=spec["name"],
                    cron_expression=spec["cron"],
                    handler=handler,
                    timezone=spec["timezone"],
                    enabled=spec["enabled"],
                )
                rebuilt += 1
            except Exception as e:  # noqa: BLE001
                failed += 1
                logger.error(
                    f"[dota2] 重建定时任务「{spec['name']}」失败，已跳过：{e}"
                )
        return (rebuilt, failed)


__all__ = ["CronBridge", "CronUnavailable", "OWNER", "PERSISTENT", "is_owned"]
