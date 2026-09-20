"""插件数据持久化。

数据统一存放在 AstrBot 的 ``data/plugin_data/astrbot_plugin_dota2/`` 目录下，
避免插件更新 / 重装时数据被覆盖。

存储结构（bindings.json）::

    {
      "version": 1,
      "bindings": {
        "<umo>#<用户ID>": {
          "account_id": 86745912,
          "personaname": "xxx",
          "steam_id": "76561198047011640",
          "bound_at": 1788905661
        }
      },
      "watchers": [
        {
          "id": "<umo>#<account_id>",
          "umo": "...",
          "account_id": 86745912,
          "personaname": "xxx",
          "platform": "aiocqhttp",
          "created_by": "10001",
          "created_by_name": "小明",
          "created_at": 1788905661,
          "last_match_id": 8989601141
        }
      ],
      "count_tasks": [
        {
          "id": "<umo>#10",
          "umo": "...",
          "kind": "watch_count",
          "action": "watch_summary",
          "every": 10,
          "enabled": true,
          "created_by": "10001",
          "created_at": 1788905661,
          "seen": [{"match_id": 8989601141, "desc": "...", "time_text": "..."}],
          "last_fired_at": 1788905661
        }
      ]
    }

**时间型的定时任务不在这里** —— 它们存在 AstrBot 自己的任务库
（``dashboard`` 里的「未来任务」），由 ``dota_cron`` 桥接。这样做是为了让用户
在 AstrBot 的任务页面里就能看到、改时间、停用、删除，而不是在插件里另开一套。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

SCHEMA_VERSION = 1


class DotaStore:
    """绑定关系与监听关系的读写器（异步安全 + 原子写入）。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.file_path = self.data_dir / "bindings.json"
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = {
            "version": SCHEMA_VERSION,
            "bindings": {},
            "watchers": [],
            "count_tasks": [],
        }
        self._loaded = False

    # ------------------------------------------------------------------
    # 基础读写
    # ------------------------------------------------------------------
    def load(self) -> None:
        """从磁盘加载数据，文件不存在或损坏时使用空结构。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists():
            self._loaded = True
            return
        try:
            raw = self.file_path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
        except (OSError, ValueError) as e:
            logger.error(f"[dota2] 读取 {self.file_path} 失败，将使用空数据: {e}")
            data = {}

        if not isinstance(data, dict):
            data = {}
        self._data = {
            "version": SCHEMA_VERSION,
            "bindings": data.get("bindings") if isinstance(data.get("bindings"), dict) else {},
            "watchers": data.get("watchers") if isinstance(data.get("watchers"), list) else [],
            # 计数型定时任务（「每监听到 N 盘做一次总结」）。
            # 时间型的任务不在这里 —— 它们存在 AstrBot 的任务库里，见 dota_cron。
            "count_tasks": (
                data.get("count_tasks")
                if isinstance(data.get("count_tasks"), list)
                else []
            ),
        }
        self._loaded = True

    def _save_sync(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self.file_path.with_suffix(".json.tmp")
        try:
            tmp_path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp_path, self.file_path)
        except OSError as e:
            logger.error(f"[dota2] 写入 {self.file_path} 失败: {e}")
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass

    async def save(self) -> None:
        """异步保存（加锁）。"""
        async with self._lock:
            self._save_sync()

    # ------------------------------------------------------------------
    # 个人绑定
    # ------------------------------------------------------------------
    @staticmethod
    def binding_key(umo: str, user_id: str) -> str:
        return f"{umo}#{user_id}"

    def get_binding(self, umo: str, user_id: str) -> dict | None:
        """获取某用户在某会话下的绑定。"""
        return self._data["bindings"].get(self.binding_key(umo, user_id))

    def list_bindings(self, umo: str) -> dict[str, dict]:
        """列出某会话下的全部绑定，返回 ``{用户ID: 绑定信息}``。"""
        prefix = f"{umo}#"
        result: dict[str, dict] = {}
        for key, value in self._data["bindings"].items():
            if key.startswith(prefix):
                result[key[len(prefix):]] = value
        return result

    def list_all_bindings(self) -> list[tuple[str, str, dict]]:
        """列出所有绑定，返回 ``[(用户ID, umo, 绑定信息), ...]``。"""
        rows: list[tuple[str, str, dict]] = []
        for key, value in self._data["bindings"].items():
            if "#" not in key:
                continue
            umo, _, user_id = key.rpartition("#")
            rows.append((user_id, umo, value))
        return rows

    async def set_binding(
        self,
        umo: str,
        user_id: str,
        account_id: int,
        personaname: str,
        steam_id: str = "",
    ) -> dict:
        """写入 / 覆盖一条绑定。"""
        record = {
            "account_id": int(account_id),
            "personaname": personaname or "",
            "steam_id": steam_id or "",
            "bound_at": int(time.time()),
        }
        async with self._lock:
            self._data["bindings"][self.binding_key(umo, user_id)] = record
            self._save_sync()
        return record

    async def remove_binding(self, umo: str, user_id: str) -> bool:
        """删除一条绑定，返回是否存在。"""
        async with self._lock:
            removed = self._data["bindings"].pop(self.binding_key(umo, user_id), None)
            if removed is not None:
                self._save_sync()
        return removed is not None

    # ------------------------------------------------------------------
    # 监听
    # ------------------------------------------------------------------
    @staticmethod
    def watcher_id(umo: str, account_id: int) -> str:
        return f"{umo}#{int(account_id)}"

    def list_watchers(self, umo: str | None = None) -> list[dict]:
        """列出监听项。传入 umo 时只返回该会话下的监听。"""
        watchers = [
            w for w in self._data["watchers"] if isinstance(w, dict) and w.get("umo")
        ]
        if umo is not None:
            watchers = [w for w in watchers if w.get("umo") == umo]
        return watchers

    def get_watcher(self, wid: str) -> dict | None:
        for watcher in self._data["watchers"]:
            if isinstance(watcher, dict) and watcher.get("id") == wid:
                return watcher
        return None

    def find_watcher(self, umo: str, account_id: int) -> dict | None:
        return self.get_watcher(self.watcher_id(umo, account_id))

    def watched_account_ids(self) -> list[int]:
        """返回所有被监听的 account_id（去重）。"""
        ids: list[int] = []
        for watcher in self.list_watchers():
            account_id = watcher.get("account_id")
            if isinstance(account_id, int) and account_id not in ids:
                ids.append(account_id)
        return ids

    async def add_watcher(
        self,
        umo: str,
        account_id: int,
        personaname: str,
        last_match_id: int = 0,
        platform: str = "",
        created_by: str = "",
        created_by_name: str = "",
    ) -> tuple[dict, bool]:
        """新增一个监听项。

        Returns:
            ``(监听记录, 是否新建)``。已存在时返回已有记录且 created 为 False。
        """
        wid = self.watcher_id(umo, account_id)
        async with self._lock:
            existing = self.get_watcher(wid)
            if existing is not None:
                # 已存在：仅刷新昵称，不重置 already seen 的比赛
                existing["personaname"] = personaname or existing.get("personaname", "")
                self._save_sync()
                return existing, False

            record = {
                "id": wid,
                "umo": umo,
                "account_id": int(account_id),
                "personaname": personaname or "",
                "platform": platform or "",
                "created_by": str(created_by or ""),
                "created_by_name": created_by_name or "",
                "created_at": int(time.time()),
                "last_match_id": int(last_match_id or 0),
            }
            self._data["watchers"].append(record)
            self._save_sync()
        return record, True

    async def remove_watcher(self, wid: str) -> bool:
        async with self._lock:
            before = len(self._data["watchers"])
            self._data["watchers"] = [
                w
                for w in self._data["watchers"]
                if not (isinstance(w, dict) and w.get("id") == wid)
            ]
            changed = len(self._data["watchers"]) != before
            if changed:
                self._save_sync()
        return changed

    async def remove_watchers_for_account(self, umo: str, account_id: int) -> bool:
        return await self.remove_watcher(self.watcher_id(umo, account_id))

    async def remove_watchers_for_umo(self, umo: str) -> int:
        """删除某个会话下的**全部**监听项，返回删除条数。

        给「bot 被移出群聊 / 被拉黑」用：这时该会话已经不可能再收到推送，
        留着监听只会让后台一直白跑（还会白白消耗大模型配额）。
        """
        async with self._lock:
            before = len(self._data["watchers"])
            self._data["watchers"] = [
                w
                for w in self._data["watchers"]
                if not (isinstance(w, dict) and w.get("umo") == umo)
            ]
            removed = before - len(self._data["watchers"])
            if removed:
                self._save_sync()
        return removed

    async def update_watcher_last_match(self, wid: str, match_id: int) -> None:
        async with self._lock:
            watcher = self.get_watcher(wid)
            if watcher is not None:
                watcher["last_match_id"] = int(match_id)
                self._save_sync()

    async def update_watcher_last_match_bulk(self, updates: dict[str, int]) -> None:
        """批量更新多个监听项的 last_match_id，减少磁盘写入次数。"""
        if not updates:
            return
        async with self._lock:
            changed = False
            for wid, match_id in updates.items():
                watcher = self.get_watcher(wid)
                if watcher is not None and int(watcher.get("last_match_id", 0)) < int(
                    match_id
                ):
                    watcher["last_match_id"] = int(match_id)
                    changed = True
            if changed:
                self._save_sync()

    # ------------------------------------------------------------------
    # 计数型定时任务（「每监听到 N 盘做一次总结」）
    # ------------------------------------------------------------------
    #
    # 时间型的定时任务**不存这里** —— 它们存在 AstrBot 自己的任务库里
    # （见 dota_cron），免得同一件事有两份真相、还有一份会漂移。
    #
    # 计数型的存这里，因为 cron 表达不了「每 N 场」。``seen`` 是窗口：
    # 每推送成功一场就往里放一条简短记录，攒到 ``every`` 场触发一次总结，
    # 触发后清空窗口重新攒。**窗口要落盘** —— 只放内存的话，插件重启
    # （我们改代码时经常重启）会让已攒的场次归零，用户永远等不到第十场。
    def list_count_tasks(self, umo: str | None = None) -> list[dict]:
        """列出计数型任务；给了 ``umo`` 就只列该会话的。"""
        tasks = [
            t for t in self._data.get("count_tasks", []) if isinstance(t, dict)
        ]
        if umo is not None:
            tasks = [t for t in tasks if str(t.get("umo") or "") == str(umo)]
        return list(tasks)

    def get_count_task(self, task_id: str) -> dict | None:
        for task in self._data.get("count_tasks", []):
            if isinstance(task, dict) and str(task.get("id")) == str(task_id):
                return task
        return None

    def find_count_task(self, umo: str, every: int) -> dict | None:
        """同一个会话里有没有「每 N 场」这个任务（避免重复添加）。"""
        for task in self.list_count_tasks(umo):
            if int(task.get("every") or 0) == int(every):
                return task
        return None

    async def add_count_task(
        self,
        umo: str,
        every: int,
        *,
        action: str = "watch_summary",
        task_id: str = "",
        created_by: str = "",
        created_by_name: str = "",
    ) -> tuple[dict, bool]:
        """新增计数型任务。

        Returns:
            ``(任务记录, 是否新建)``。同一会话已有相同的 ``every`` 时返回已有项。
        """
        every = max(1, int(every))
        async with self._lock:
            existing = self.find_count_task(umo, every)
            if existing is not None:
                return existing, False
            record = {
                "id": task_id or f"{umo}#{every}",
                "umo": str(umo),
                "kind": "watch_count",
                "action": action or "watch_summary",
                "every": every,
                "enabled": True,
                "created_by": str(created_by or ""),
                "created_by_name": created_by_name or "",
                "created_at": int(time.time()),
                "seen": [],
            }
            self._data.setdefault("count_tasks", []).append(record)
            self._save_sync()
        return record, True

    async def remove_count_task(self, task_id: str) -> bool:
        async with self._lock:
            before = len(self._data.get("count_tasks", []))
            self._data["count_tasks"] = [
                t
                for t in self._data.get("count_tasks", [])
                if not (isinstance(t, dict) and str(t.get("id")) == str(task_id))
            ]
            changed = len(self._data["count_tasks"]) != before
            if changed:
                self._save_sync()
        return changed

    async def set_count_task_enabled(self, task_id: str, enabled: bool) -> bool:
        async with self._lock:
            task = self.get_count_task(task_id)
            if task is None:
                return False
            task["enabled"] = bool(enabled)
            self._save_sync()
        return True

    async def push_count_task_seen(
        self, task_id: str, row: dict, *, every: int
    ) -> bool:
        """把一场比赛记进窗口，返回**这次是否该触发总结**。

        同一场只记一次（``_deliver`` 在重试路径上可能重复调用）；触发之后
        由调用方负责 :meth:`reset_count_task_window` —— 分开两步是为了让
        「生成总结」失败时窗口还在，下一轮可以重试，而不是白白丢掉这些场次。

        触发判据是 ``len(seen) >= every 且 len(seen) % every == 0``：
        后半个条件看着多余，其实是**失败后的节流阀**。生成总结失败时窗口
        不会被清空（那是故意的），若只看 ``>= every``，那之后每来一场都会
        再触发一次 —— 模型挂掉的那段时间里，每场新比赛都会白烧一次调用。
        加上取模之后变成「每再攒够一整个窗口才重试一次」，代价可控。
        """
        match_id = int(row.get("match_id") or 0)
        if match_id <= 0:
            return False
        async with self._lock:
            task = self.get_count_task(task_id)
            if task is None or not task.get("enabled", True):
                return False
            seen = task.setdefault("seen", [])
            if any(int(item.get("match_id") or 0) == match_id for item in seen):
                return False
            seen.append(
                {
                    "match_id": match_id,
                    "desc": str(row.get("desc") or ""),
                    "time_text": str(row.get("time_text") or ""),
                    "at": int(time.time()),
                }
            )
            # 窗口留一点余量（``every`` 的两倍起），一是给失败重试留空间，
            # 二是失败后要能再攒够一整个窗口（见上面的取模判据），
            # 所以下限取 ``every`` 的三倍。
            limit = max(4, int(every) * 3)
            if len(seen) > limit:
                del seen[: len(seen) - limit]
            self._save_sync()
            size = len(seen)
            return size >= max(1, int(every)) and size % max(1, int(every)) == 0


    async def reset_count_task_window(self, task_id: str) -> None:
        """清空窗口（一次总结成功发出去之后调用）。"""
        async with self._lock:
            task = self.get_count_task(task_id)
            if task is None:
                return
            task["seen"] = []
            task["last_fired_at"] = int(time.time())
            self._save_sync()
