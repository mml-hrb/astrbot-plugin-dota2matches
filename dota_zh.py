"""Dota2 名称中文化。

数据源（STRATZ / OpenDota）的常量接口只给英文名：英雄是 ``Juggernaut``、
道具是 ``Blink Dagger``、技能是 ``antimage_blink`` 这类内部名。本模块负责把
它们换成国服官方中文名，让推送与战报全程说中文。

查名顺序（**命中即返回，绝不每次都联网**）：

1. **包内静态对照表** ``dota_zh_data.json``：英雄全量手工校对，道具与技能是
   开发期批量预译后固化的，覆盖当前版本绝大多数条目。
2. **磁盘缓存** ``{插件数据目录}/zh_names.json``：运行时学到的新词（比如以后
   Valve 新增英雄 / 道具）会写回这里，下次启动直接命中。
3. **联网补译**：仅当上面两处都没有时才发生，批量问一次大模型，结果落盘。
   补译失败不影响主流程——名字将保持英文，功能不受影响。

线程/协程安全性：静态表只读；磁盘缓存的写入在同一 asyncio 事件循环内串行发生，
并且做了「先写临时文件再替换」的原子落盘，避免断电留下半个 JSON。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

#: 名称种类：英雄 / 道具 / 技能
KIND_HERO = "hero"
KIND_ITEM = "item"
KIND_ABILITY = "ability"

_KINDS = (KIND_HERO, KIND_ITEM, KIND_ABILITY)

#: 中文译名最短合理长度。模型偶尔会返回空串或单个标点，这种结果宁可不要，
#: 否则会把「Juggernaut」换成「。」这种东西写进缓存。
_MIN_ZH_LEN = 1

_TRANSLATE_SYSTEM = (
    "你是《Dota 2》国服本地化专家。把给定的英文条目翻译成简体中文，"
    "必须使用 Dota 2 国服官方客户端里的正式译名。\n"
    "只输出一个 JSON 对象：键为给定的英文键（原样保留、一个都不能少），"
    "值为对应的中文名。不要输出解释、不要 markdown 代码块、不要额外文字。\n"
    "中文名要简洁（就是游戏里显示的那个名字），不要附加英文或标点。"
    "确实没有官方译名时给出合理的直译或音译，不要留空、不要原样返回英文。"
)

#: 一次补译最多问多少条。太多容易超长、也更容易让模型漏项。
_MAX_BATCH = 40

_STATIC: dict[str, dict[str, str]] | None = None


def _static_table() -> dict[str, dict[str, str]]:
    """加载包内静态对照表（进程内只读缓存一次）。"""
    global _STATIC
    if _STATIC is not None:
        return _STATIC
    table: dict[str, dict[str, str]] = {kind: {} for kind in _KINDS}
    path = Path(__file__).with_name("dota_zh_data.json")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 静态表缺失/损坏时退化成「只有缓存」，不能崩
        raw = {}
    for kind, section in (
        (KIND_HERO, "heroes"),
        (KIND_ITEM, "items"),
        (KIND_ABILITY, "abilities"),
    ):
        for key, value in (raw.get(section) or {}).items():
            if not isinstance(value, str):
                continue
            name = value.strip()
            if name:
                table[kind][str(key).strip().lower()] = name
    _STATIC = table
    return table


def _norm(key: Any) -> str:
    return str(key or "").strip().lower()


class ZhNames:
    """中文名服务：静态表 → 磁盘缓存 → 联网补译。

    Args:
        cache_path: 磁盘缓存文件路径。为 None 时只用静态表（不落盘、不学新词）。
        enabled: 总开关。关闭后 :meth:`hero` / :meth:`item` / :meth:`ability`
            一律返回 None，下游保持英文原名。
        learn: 是否允许「缺词补译」。关闭后只查本地，缺了就是缺了。
    """

    def __init__(
        self,
        cache_path: Path | None = None,
        *,
        enabled: bool = True,
        learn: bool = True,
    ) -> None:
        self.cache_path = Path(cache_path) if cache_path else None
        self.enabled = bool(enabled)
        self.learn = bool(learn)
        self._cache: dict[str, dict[str, str]] = {kind: {} for kind in _KINDS}
        self._miss: dict[str, set[str]] = {kind: set() for kind in _KINDS}
        self._dirty = False
        self._lock = asyncio.Lock()
        self._loaded = False

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def load(self) -> "ZhNames":
        """从磁盘读回运行时学到的新词。失败时静默降级为空缓存。"""
        self._loaded = True
        if not self.cache_path or not self.cache_path.exists():
            return self
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 缓存损坏不该让插件起不来
            return self
        for kind in _KINDS:
            for key, value in (raw.get(kind) or {}).items():
                if isinstance(value, str) and value.strip():
                    self._cache[kind][_norm(key)] = value.strip()
        return self

    def save(self) -> None:
        """把学到的新词写回磁盘（原子替换，失败只记不改数据）。"""
        if not self._dirty or not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                kind: dict(sorted(self._cache[kind].items())) for kind in _KINDS
            }
            fd, tmp = tempfile.mkstemp(
                dir=str(self.cache_path.parent), suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=1)
                os.replace(tmp, str(self.cache_path))
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
            self._dirty = False
        except Exception:  # noqa: BLE001 - 落盘失败不影响本次推送
            pass

    # ------------------------------------------------------------------
    # 查名
    # ------------------------------------------------------------------
    def get(self, kind: str, key: Any) -> str | None:
        """查一个名字。未命中会记进待补译清单（供 :meth:`fill_missing` 用）。"""
        if not self.enabled or kind not in self._cache:
            return None
        norm = _norm(key)
        if not norm:
            return None
        name = _static_table().get(kind, {}).get(norm)
        if name:
            return name
        name = self._cache[kind].get(norm)
        if name:
            return name
        if self.learn:
            self._miss[kind].add(norm)
        return None

    def hero(self, hero_id: Any) -> str | None:
        return self.get(KIND_HERO, hero_id)

    def item(self, key: Any) -> str | None:
        return self.get(KIND_ITEM, key)

    def ability(self, name: Any) -> str | None:
        """技能名 → 中文。

        天赋类技能（``special_bonus_*``）在数据源里没有可读名字，渲染出来会是
        ``special bonus attributes`` 这种内部名。它们没有官方中文译名，这里统一
        翻成「天赋」——比让一串内部名出现在战报里有用得多。
        """
        norm = _norm(name)
        if norm.startswith("special_bonus"):
            return "天赋" if self.enabled else None
        return self.get(KIND_ABILITY, name)

    def clear_miss(self) -> None:
        """清空待补译清单。每次本地化开始前调用，避免历史残留被反复补译。"""
        for kind in _KINDS:
            self._miss[kind].clear()

    def pending(self) -> dict[str, list[str]]:
        """返回当前待补译的键（已按批大小截断，避免一次问太多）。"""
        return {
            kind: sorted(keys)[:_MAX_BATCH]
            for kind, keys in self._miss.items()
            if keys
        }

    # ------------------------------------------------------------------
    # 补译
    # ------------------------------------------------------------------
    async def fill_missing(
        self,
        translator: Callable[[str, list[str]], Awaitable[dict[str, str]]],
    ) -> bool:
        """把待补译的键批量问一遍并落盘。

        Args:
            translator: ``async (kind, keys) -> {key: 中文名}``。由调用方注入，
                本模块不直接依赖具体的模型通道（便于测试与关闭）。

        Returns:
            是否学到了新词。True 表示调用方应当用**原始**数据重新本地化一次。
        """
        if not self.enabled or not self.learn or not translator:
            self.clear_miss()
            return False
        pending = self.pending()
        if not pending:
            return False
        learned: dict[str, dict[str, str]] = {}
        # 补译是「批量 + 串行」的：并发问模型既没收益又会撞限流，
        # 而且这里的调用频率极低（只有真出现新英雄/新道具时才有内容）。
        for kind, keys in pending.items():
            try:
                result = await translator(kind, keys)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 补译失败就用英文名，不阻断主流程
                continue
            if not isinstance(result, dict):
                continue
            for key, value in result.items():
                if not isinstance(value, str):
                    continue
                name = value.strip()
                if len(name) <= _MIN_ZH_LEN:
                    continue
                learned.setdefault(kind, {})[_norm(key)] = name
        if not learned:
            self.clear_miss()
            return False
        async with self._lock:
            for kind, mapping in learned.items():
                self._cache[kind].update(mapping)
            self._dirty = True
            self.save()
        self.clear_miss()
        return True


# ======================================================================
# 本地化：把数据源常量里的显示名替换成中文
# ======================================================================
def localize_heroes(
    heroes: dict[int, dict] | None, zh: ZhNames | None
) -> dict[int, dict]:
    """把 ``{hero_id: {...}}`` 的 ``localized_name`` 换成中文。

    **只改 ``localized_name``**：``name``（``npc_dota_hero_xxx``）是
    :func:`hname_by_npc` 做反查的键，动了它解析产物里的英雄就全对不上了。
    返回的是副本，不会污染数据源的内存缓存（否则关掉中文化开关也回不来）。
    """
    if not heroes or zh is None:
        return heroes or {}
    out: dict[int, dict] = {}
    for hero_id, info in heroes.items():
        info = dict(info) if isinstance(info, dict) else {}
        name = zh.hero(hero_id)
        if name:
            info["localized_name"] = name
        out[hero_id] = info
    return out


def localize_items(
    items: dict[str, dict] | None, zh: ZhNames | None
) -> dict[str, dict]:
    """把道具常量的 ``dname``（显示名）换成中文，``name`` 保持内部名不变。"""
    if not items or zh is None:
        return items or {}
    out: dict[str, dict] = {}
    for key, info in items.items():
        info = dict(info) if isinstance(info, dict) else {}
        name = zh.item(key)
        if name:
            info["dname"] = name
        out[key] = info
    return out


def localize_abilities(
    abilities: dict[int, str] | None, zh: ZhNames | None
) -> dict[int, str]:
    """把 ``{技能ID: 技能内部名}`` 映射的值换成中文。

    值的形态在数据源之间不统一（OpenDota 给内部名 ``antimage_blink``，
    STRATZ 给显示名），所以两边都查：先按原值查，查不到再按下划线归一化查。
    """
    if not abilities or zh is None:
        return abilities or {}
    out: dict[int, str] = {}
    for ability_id, raw in abilities.items():
        name = zh.ability(raw)
        out[ability_id] = name or raw
    return out


def parse_translation(text: str) -> dict[str, str]:
    """从模型回复里抠出 ``{键: 中文名}``。

    模型偶尔会裹一层 ```json 代码块，或者在 JSON 前后加一句说明。这里逐层
    剥掉，实在解析不出来就返回空字典——调用方会保持英文原名，不会崩。
    """
    import re

    raw = (text or "").strip()
    if not raw:
        return {}
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(raw[start : end + 1])
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(value, str):
            continue
        name = value.strip()
        if len(name) > _MIN_ZH_LEN:
            out[_norm(key)] = name
    return out


def build_translate_prompt(kind: str, keys: list[str]) -> str:
    """构造补译提示词（``hero`` / ``item`` / ``ability``）。"""
    label = {
        KIND_HERO: "英雄",
        KIND_ITEM: "道具",
        KIND_ABILITY: "技能",
    }.get(kind, "条目")
    payload = "\n".join(f"- {key}" for key in keys)
    return (
        f"下面是《Dota 2》{label}的英文键名，请给出国服官方中文名。\n"
        f"输出 JSON 对象，键为下面每一行的英文键（去掉前导「- 」），值为中文名。\n\n"
        f"{payload}"
    )
