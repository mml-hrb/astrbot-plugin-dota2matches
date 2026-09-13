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
      ]
    }
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
