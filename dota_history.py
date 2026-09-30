"""会话对话历史：让「接着上一句说」真的接得上。

背景（用户原话）：

    「在闲聊的过程中，多次对话没有上下文，没有连续性。
     而且调用工具生成的报告也不在上下文。」

以前那份历史（``main.Dota2Plugin._nlu_chat_log``）有三个问题，叠在一起就是
「聊不起来」：

1. **只记了用户说的话**。机器人自己的回复从未写进去 —— 全插件唯一写
   ``role="bot"`` 的地方是 :meth:`Dota2Plugin._nlu_remember_match` 里的
   ``[提到比赛 N]`` 这种标记。于是模型看到的「最近对话」全是用户自说自话，
   接不下去是必然的。
2. **完全不记报告类产出**。单场复盘报告、定时播报、监听推送都经
   :meth:`Dota2Plugin._send_to_session` 直接发到会话，一条都不进历史。
   用户接着问「刚才那场他补刀多少」，模型手上什么都没有。
3. **留不住也放不下**。每条截断到 **80 字**（几千字的复盘报告只剩个开头）、
   每个会话只留 12 条、而且**只在内存里** —— 插件每次热重载都清空，
   而热重载恰恰是开发期的常态。

本模块把三件事一起修掉：**记全**（机器人回复 + 报告类产出都进）、
**留够**（单条与总量各有预算，报告不再被砍成一句）、**存得住**
（落盘 + 按时间过期，重载 / 重启都还在）。

两个读出形态，用途不同，别混：

* :meth:`ChatHistoryStore.text_lines` —— ``["用户: …", "机器人: …"]``，
  给 :func:`dota_chat.format_context_block` 的【最近对话】段用（定时播报那
  条不接工具的老路径，以及单轮兜底）。
* :meth:`ChatHistoryStore.messages` —— 真正的多轮消息序列
  （``role=user`` / ``role=assistant``），给工具路径前置到 ``messages`` 里。
  **这才是「有上下文」的本质**：模型天然知道哪句是自己说的、哪句是用户说的，
  不必再从一段拼接文本里自己解析角色。
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

#: 单条**用户**消息的字符上限。用户说话通常很短，留一点余量就够。
MAX_USER_CHARS = 200

#: 单条**机器人**产出的字符上限。给得比较宽是有原因的：单场复盘报告实测
#: 有 7000+ 字符，截到 80 字（旧值）等于根本没记 —— 那正是「报告不在上下文」
#: 的直接原因。超长时**保留头部**（报告的开头就是结论与焦点点评）。
MAX_BOT_CHARS = 4000

#: 每个会话最多保留的条目数（用户 + 机器人合计）。
MAX_TURNS_PER_SESSION = 40

#: 一次注入的历史总字符预算（从新到旧装填，装不下就停）。
MESSAGE_BUDGET_CHARS = 8000

#: 默认保留时长（小时）。超过这个时间的条目不再注入、也不再落盘。
#: 太短会让「昨晚聊的」断掉，太长会把几天前的旧数据当成本次语境。
DEFAULT_TTL_HOURS = 12

#: 落盘节流：距上次写盘不足这个秒数时只更新内存，攒一攒再写。
#: 群里闲聊可能连着好几轮，每次都写盘既没必要也浪费。
FLUSH_INTERVAL = 3.0

_ROLE_BOT = "bot"
_ROLE_USER = "user"


def _clip(text: str, limit: int) -> str:
    """按字符上限裁剪，并在截断处留一个明确的省略标记。

    留标记很重要：模型看到「…（后文略）」就知道这份材料不完整，
    不会把「没提到」当成「没有」。
    """
    text = (text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…（后文略）"


def _clean_line(text: str) -> str:
    """压成单行（历史条目不做多行渲染，省得把提示词结构撑乱）。"""
    return " ".join((text or "").split())


class ChatHistoryStore:
    """每个会话最近几轮对话的读写器（带 TTL、上限与原子落盘）。

    ``enabled=False`` 时整体退化成空实现：不记、不读、不落盘 ——
    等价于回到「没有历史」的旧行为，供用户按需关闭（对话内容会写进
    磁盘，有人可能不愿意）。
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        enabled: bool = True,
        ttl_hours: float = DEFAULT_TTL_HOURS,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.file_path = self.data_dir / "chat_history.json"
        self.enabled = bool(enabled)
        try:
            hours = float(ttl_hours)
        except (TypeError, ValueError):
            hours = DEFAULT_TTL_HOURS
        # 0 或负数没有意义（等于永不注入），回落到默认值。
        self.ttl = (hours if hours > 0 else DEFAULT_TTL_HOURS) * 3600.0
        self._lock = asyncio.Lock()
        #: ``{umo: [ChatTurn, ...]}``，按时间由旧到新。
        self._sessions: dict[str, list[dict[str, Any]]] = {}
        self._last_flush = 0.0
        self._loaded = False

    # ------------------------------------------------------------------
    # 落盘
    # ------------------------------------------------------------------
    def load(self) -> None:
        """从磁盘加载。文件不存在或损坏时用空结构（历史丢了不该拖垮插件）。"""
        if not self.enabled:
            self._loaded = True
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists():
            self._loaded = True
            return
        try:
            raw = self.file_path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
        except (OSError, ValueError) as e:
            logger.error(f"[dota2] 读取 {self.file_path} 失败，将使用空历史: {e}")
            data = {}

        sessions = data.get("sessions") if isinstance(data, dict) else {}
        if not isinstance(sessions, dict):
            sessions = {}
        now = time.time()
        kept: dict[str, list[dict[str, Any]]] = {}
        for umo, rows in sessions.items():
            if not isinstance(rows, list):
                continue
            valid = [
                row
                for row in rows
                if isinstance(row, dict)
                and str(row.get("text") or "").strip()
                and self._fresh(row, now)
            ]
            if valid:
                kept[str(umo)] = valid[-MAX_TURNS_PER_SESSION:]
        self._sessions = kept
        self._loaded = True
        if kept:
            logger.info(
                f"[dota2] 对话历史已载入：{len(kept)} 个会话、"
                f"{sum(len(v) for v in kept.values())} 条"
            )

    def _fresh(self, row: dict[str, Any], now: float) -> bool:
        try:
            ts = float(row.get("ts") or 0)
        except (TypeError, ValueError):
            return False
        return now - ts <= self.ttl

    def _save_sync(self) -> None:
        if not self.enabled:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self.file_path.with_suffix(".json.tmp")
        payload = {
            "version": SCHEMA_VERSION,
            "sessions": self._sessions,
        }
        try:
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp_path, self.file_path)
            self._last_flush = time.time()
        except OSError as e:
            logger.error(f"[dota2] 写入 {self.file_path} 失败: {e}")
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass

    async def save(self) -> None:
        """强制落盘（加锁）。"""
        if not self.enabled:
            return
        async with self._lock:
            self._save_sync()

    def _maybe_flush(self, now: float) -> None:
        """节流落盘：攒够 :data:`FLUSH_INTERVAL` 再写一次。

        故意**同步**写：单个会话的历史只有几十条、每会话几万字符，
        一次写入是毫秒级，不值得为它引入后台任务与「任务忘了 await」这类
        更麻烦的失效模式。
        """
        if now - self._last_flush >= FLUSH_INTERVAL:
            self._save_sync()

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def append(
        self,
        umo: str,
        role: str,
        text: str,
        *,
        kind: str = "chat",
        now: float | None = None,
    ) -> None:
        """记一条。``role`` 为 ``user`` / ``bot``；``kind`` 为 ``chat`` / ``report``。

        **失败一律吞掉**（只打日志）：历史记不上不该影响这次回答。
        """
        if not self.enabled:
            return
        umo = str(umo or "")
        if not umo:
            return
        body = _clean_line(text)
        if not body:
            return
        is_user = str(role or "").strip().lower() == _ROLE_USER
        limit = MAX_USER_CHARS if is_user else MAX_BOT_CHARS
        entry = {
            "role": _ROLE_USER if is_user else _ROLE_BOT,
            "text": _clip(body, limit),
            "ts": float(now if now is not None else time.time()),
            "kind": str(kind or "chat"),
        }
        rows = self._sessions.setdefault(umo, [])
        # 连续两条一模一样的（同一句话被重复登记）只留一条 —— 否则模型会
        # 以为自己说过两遍，或者以为用户追问了同一件事。
        if rows and rows[-1]["role"] == entry["role"] and rows[-1]["text"] == entry["text"]:
            rows[-1]["ts"] = entry["ts"]
            return
        rows.append(entry)
        if len(rows) > MAX_TURNS_PER_SESSION:
            del rows[: len(rows) - MAX_TURNS_PER_SESSION]
        self._maybe_flush(entry["ts"])

    def clear(self, umo: str) -> None:
        """清空某个会话的历史（维护口子：测试与将来的管理指令用）。"""
        if self._sessions.pop(str(umo or ""), None) is not None:
            self._save_sync()

    # ------------------------------------------------------------------
    # 读出
    # ------------------------------------------------------------------
    def turns(self, umo: str, *, now: float | None = None) -> list[dict[str, Any]]:
        """未过期的条目（由旧到新）。"""
        if not self.enabled:
            return []
        moments = float(now if now is not None else time.time())
        rows = self._sessions.get(str(umo or "")) or []
        return [row for row in rows if self._fresh(row, moments)]

    def text_lines(
        self, umo: str, limit: int = 8, *, now: float | None = None
    ) -> list[str]:
        """渲染成 ``["用户: …", "机器人: …"]``，供提示词里的【最近对话】段用。"""
        rows = self.turns(umo, now=now)[-max(1, int(limit or 1)):]
        lines: list[str] = []
        for row in rows:
            who = "用户" if row.get("role") == _ROLE_USER else "机器人"
            prefix = "" if row.get("kind") != "report" else "[自动播报] "
            lines.append(f"{who}: {prefix}{row.get('text')}")
        return lines

    def messages(
        self,
        umo: str,
        *,
        now: float | None = None,
        budget: int = MESSAGE_BUDGET_CHARS,
        drop_tail_user: bool = True,
    ) -> list[dict[str, str]]:
        """渲染成真正的多轮消息序列，供工具路径前置到 ``messages``。

        从**新到旧**装填直到用满 ``budget``（新的对话比旧的更有用），最后
        翻回由旧到新交给模型。

        Args:
            drop_tail_user: 丢掉末尾那条 user 条目。调用方（自然语言的
                ``_nlu_agent_reply``）在进入模型之前**已经**把本轮问题记进
                历史了，而本轮问题又会作为 ``user`` 消息单独放在最后 ——
                不丢就是同一句话出现两遍。

        .. note::

           只放**纯文本**的 user / assistant 消息，**不带** ``tool_calls``。
            历史里若留着一个没有对应 ``tool`` 结果消息的 ``tool_calls``，
            多数服务会直接 400；而工具结果本身又太长、放进历史得不偿失。
            工具这一轮的结论由模型的自然语言回答承载（回答会入历史）。
        """
        rows = self.turns(umo, now=now)
        if drop_tail_user and rows and rows[-1].get("role") == _ROLE_USER:
            rows = rows[:-1]
        picked: list[dict[str, str]] = []
        used = 0
        for row in reversed(rows):
            text = str(row.get("text") or "")
            if not text:
                continue
            if picked and used + len(text) > budget:
                break
            picked.append(
                {
                    "role": "user" if row.get("role") == _ROLE_USER else "assistant",
                    "content": text,
                }
            )
            used += len(text)
        picked.reverse()
        return picked

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------
    def purge_expired(self, now: float | None = None) -> int:
        """删掉全部已过期的条目，返回删掉的条数（顺手回收空会话）。"""
        if not self.enabled:
            return 0
        moments = float(now if now is not None else time.time())
        removed = 0
        for umo in list(self._sessions):
            rows = self._sessions.get(umo) or []
            kept = [row for row in rows if self._fresh(row, moments)]
            removed += len(rows) - len(kept)
            if kept:
                self._sessions[umo] = kept
            else:
                self._sessions.pop(umo, None)
        if removed:
            self._save_sync()
        return removed

    def stats(self) -> dict[str, int]:
        """给日志 / 自检用的小结。"""
        return {
            "sessions": len(self._sessions),
            "turns": sum(len(v) for v in self._sessions.values()),
        }
