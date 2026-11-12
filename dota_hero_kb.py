# -*- coding: utf-8 -*-
"""英雄判读参考：把人工整理的英雄画像注入复盘提示词。

为什么要这个模块
----------------
插件此前判「这一场谁打得好、谁出了问题」只有两样东西：数据块里的客观数字，
和模型的通用常识。于是同一份数据换一个模型，结论就飘；面对非常规打法
（敌法师打三号位、牛头走物理流），模型只会按刻板印象判「这不合常规」。

``hero_profiles.json`` 是一份人工整理 127 个英雄的画像：输出来自哪里、
不能做什么、怕什么、怕的东西怎么应对、什么算失职。它补的正是这层
「这个英雄本来该干什么」的基准。

**它不是限制**（用户 2026-11-10 明确要求）
-------------------------------------------
画像描述的是**常见对局的主流打法**，不是唯一合法打法。本局走了非常规位置、
换了流派、出了怪装，恰恰是模型最容易被误判的地方。所以渲染出的块头部
固定带四条纪律，其中第一条就是「按本局实际打法判，不要拿参考否定他」。
这不是客套话，是这份数据能安全使用的前提 —— 详见 :data:`DISCIPLINE`。

数据从哪来（三级查找）
----------------------
1. **包内静态表** ``hero_profiles.json``（与 ``dota_zh_data.json`` 同级）：
   随插件发版分发，所有用户都有。
2. **插件数据目录覆盖** ``{data_dir}/hero_profiles.json``：逐条覆盖包内表，
   自建部署想改某几个英雄不必发版。
3. 两处都没有 → 渲染成空串，调用方直接不插入这一段，**复盘照常进行**。

与 ``dota_zh.py`` 的三级查找同构，差别在于这里**不联网补译**：英雄画像是
人工判断，拿不到就是拿不到，不能由模型现编一份（那会把「参考」变成
「模型自己的刻板印象」，正好是这套机制要防的东西）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Sequence

try:  # 插件目录被作为包加载时
    from .dota_format import hname, is_radiant
except ImportError:  # 兜底：以普通模块方式加载
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dota_format import hname, is_radiant  # type: ignore[no-redef]


#: 包内文件布局版本。字段增删时与 ``export_kb.py`` 的 ``SCHEMA_VERSION`` 同步 +1。
SCHEMA_VERSION = 1

#: 包内静态表文件名。
FILENAME = "hero_profiles.json"

#: 复盘注入取的字段。刻意不含 ``aghs`` / ``shard`` / ``damage`` / ``survival``
#: / ``curve`` / ``synergy`` / ``win_condition`` ——
#: 那些是「这个英雄怎么玩」的全景，对「这一局他该做什么」帮助有限，
#: 十个人各带一遍会把提示词撑大一倍（见 :func:`render_review_block` 的预算）。
#:
#: ⚠️ ``counters`` / ``fear_response`` 曾经也不在里面，2026-11-11 加了回来。
#:
#: 剔除它们的理由是「太长」（敌法师的 counters 194 字、fear_response 180 字，
#: 是两个最长的字段）。但实测代价被严重低估了 —— **这两个字段才是判断打法
#: 对不对的唯一依据**，而留下的 ``builds`` 之类只是描述「他怎么玩」。
#:
#: 真实翻车案例：库里写着「敌法师克制高蓝上限的法师（法力虚空按缺失魔法量
#: 结算）」，对面正好有风暴之灵，战报里既没提打法也没提克制 —— 因为这条
#: **根本没进提示词**。渲染出的块里「克制」出现 0 次。
#:
#: 加回后十人平均从 8521 字涨到 10765 字（按 127 个英雄实测），
#: 预算相应提到 16000。省这点字远比漏掉判据不值得。
REVIEW_FIELDS: tuple[str, ...] = (
    "position",
    "builds",
    "power_source",
    "cannot",
    "counters",
    "fears",
    "fear_response",
    "review_baseline",
)

#: 全字段（十人以外的场景用，例如以后做英雄问答）。
FULL_FIELDS: tuple[str, ...] = (
    "position", "builds", "power_source", "aghs", "shard",
    "damage", "survival", "curve", "cannot", "counters",
    "fears", "fear_response", "synergy", "win_condition", "review_baseline",
)

#: 字段 → 提示词里的中文标签。
#:
#: 标签刻意不照抄字段名：``position`` 写成「常见位置（参考）」是把「非限定」
#: 这个前提写进每一行，而不是只在块头声明一次 —— 模型读单行时看不到块头。
LABELS: dict[str, str] = {
    "position": "常见位置（参考，非限定）",
    "builds": "流派与识别",
    "power_source": "输出来自哪里",
    "aghs": "A 杖",
    "shard": "魔晶",
    "damage": "伤害构成",
    "survival": "生存与防御",
    "curve": "强弱时间轴",
    "cannot": "不能做什么",
    "counters": "克制什么",
    "fears": "怕什么",
    "fear_response": "怕的东西怎么应对",
    "synergy": "需要什么队友",
    "win_condition": "取胜条件",
    "review_baseline": "复盘基准",
}

#: 块头纪律（用户 2026-11-10 要求「必须标明只是常见打法、不是限定」）。
#:
#: 顺序不能乱：先立「非限定」这个总纲，再讲判责边界，然后才是数据可信度，
#: 最后才是「怎么用」。反过来写（先讲怎么用）模型会把前三条当成风格建议。
#:
#: ⚠️ 最后一条 2026-11-11 改过。原文是「**不要把它复述进正文**」，本意是防止
#: 模型把画像原文照抄成一篇英雄攻略。但实际效果是**连判断一起压掉了**：
#: 库里写着敌法师克制高蓝上限的法师、对面正好有风暴之灵，模型却一个字不提，
#: 因为它把「别复述」执行成了「别用它」。
#:
#: 改成「用它下判断，但别抄原话」——把禁止的对象从「引用内容」收窄到
#: 「照搬措辞」，这正是原来想表达的意思。
DISCIPLINE = """\
以下是本局十个英雄在**常见对局**里的主流打法，用来判断「他这一局该做什么」，
**不是限制他怎么打**：

- 本局走的号位、选的流派、出的装备与参考不同时，按**本局实际打法**判，
  不要拿参考去否定他 —— 非常规打法打出效果，就是对的。
- 「失职」只在他**按这套打法打却没做到**时成立；换了打法，要按新打法的职责重新衡量。
- 参考里的数值（成长、伤害、冷却、时间点）可能随版本变化，与游戏内不一致时以游戏内为准。
- 这份参考**就是判断武器，必须真的用上**：「克制什么」和「怕什么」两栏是用来
  跟对面十个人对位的 —— 谁该压谁、谁该躲谁，先对出来再判。用了它得出的结论，
  **用你自己的话讲**（例如「他这盘对线期本该把对方的蓝量压住」），
  不要照抄画像原文，也不要点名说「参考里写着」。"""

#: 十人注入的默认字符预算。
#:
#: 实测（比赛 8995536921）：普通英雄 650~780 字，画像写得厚的两个
#: （狙击手 2113、复仇之魂 1906）会把总量顶到 9700，加块头约 1.01 万字，
#: 占单场复盘提示词（3.0 万字符）的 33%。取 12000 让常见对局走满全字段。
#: 超预算时按 :data:`_FALLBACK_FIELDS` 降级，**不是悄悄砍人不报** ——
#: 降级了哪些人、哪些人完全没给，块里都会写明。
#:
#: 2026-11-11 从 12000 提到 16000：``counters`` / ``fear_response`` 加回注入
#: 后，按 127 个英雄实测十人平均 10765 字，12000 会让写得厚的对局频繁降级。
DEFAULT_BUDGET = 16000

#: 超预算时的降级字段集。
#:
#: 保留的是**判断力最强的三项**：常见位置、不能做什么、复盘基准。
#: 2026-11-11 特意把 ``counters`` 也放了进来 —— 它和「不能做什么」是一对
#: （前者说「该吃谁」，后者说「该躲谁」），只留一半会让模型判反。
_FALLBACK_FIELDS: tuple[str, ...] = (
    "position", "cannot", "counters", "review_baseline",
)

_STATIC_CACHE: dict[str, dict[str, str]] | None = None


def _read_table(path: Path) -> dict[str, dict[str, str]]:
    """读一份画像表，返回 ``{hero_id(str): {字段: 值}}``。读不了返回空字典。

    任何异常都吞掉：这份表是**增强**，不是必需品。它读不出来时复盘必须照常
    跑，不能让「英雄资料损坏」变成「战报出不来」。
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 缺文件/坏 JSON/权限问题一律降级
        return {}
    if not isinstance(raw, dict):
        return {}
    heroes = raw.get("heroes")
    if not isinstance(heroes, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for key, rec in heroes.items():
        hid = str(key or "").strip()
        if not hid.isdigit() or not isinstance(rec, dict):
            continue
        clean = {
            str(k): str(v).strip()
            for k, v in rec.items()
            if isinstance(v, str) and v.strip()
        }
        if len(clean) > 1:  # 至少得有 name + 一项正文
            out[hid] = clean
    return out


def _static_table() -> dict[str, dict[str, str]]:
    """包内静态表，进程内只读缓存一次。"""
    global _STATIC_CACHE
    if _STATIC_CACHE is None:
        path = Path(__file__).with_name(FILENAME)
        _STATIC_CACHE = _read_table(path)
    return _STATIC_CACHE


class HeroKnowledge:
    """英雄画像查询与渲染。

    Args:
        data_dir: 插件数据目录。该目录下的同名文件会**逐条覆盖**包内表
            （只覆盖写到的英雄，没写的仍用包内版本）。为 None 时只用包内表。
        budget: 一次渲染的字符预算，超出即降级。0 或负数表示不设上限。
    """

    def __init__(self, data_dir: Any = None, *, budget: int = DEFAULT_BUDGET) -> None:
        self._data_dir = Path(data_dir) if data_dir else None
        self.budget = int(budget or 0)
        self._override: dict[str, dict[str, str]] | None = None
        self._loaded = False

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------
    def load(self) -> "HeroKnowledge":
        """读数据目录覆盖表。**不必显式调用** —— 首次查询时会自动加载。"""
        if self._loaded:
            return self
        self._loaded = True
        if self._data_dir is None:
            self._override = {}
            return self
        self._override = _read_table(self._data_dir / FILENAME)
        return self

    def _table(self) -> dict[str, dict[str, str]]:
        """合并后的只读表：包内为底，数据目录逐条覆盖。"""
        if not self._loaded:
            self.load()
        merged = dict(_static_table())
        if self._override:
            for hid, rec in self._override.items():
                base = merged.get(hid) or {}
                merged[hid] = {**base, **rec}
        return merged

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def profile(self, hero_id: Any) -> dict[str, str]:
        """取一个英雄的画像；没有时返回空字典（不是 None，省得调用方判两次）。"""
        try:
            hid = str(int(hero_id))
        except (TypeError, ValueError):
            return {}
        return self._table().get(hid) or {}

    def has(self, hero_id: Any) -> bool:
        return bool(self.profile(hero_id))

    def hero_name(self, hero_id: Any, heroes: dict | None = None) -> str:
        """取英雄名：包内画像自带中文名，优先用它（不依赖调用方有没有英雄表）。"""
        rec = self.profile(hero_id)
        name = str(rec.get("name") or "").strip()
        if name:
            return name
        return hname(heroes or {}, hero_id)

    def coverage(self, hero_ids: Iterable[Any]) -> tuple[int, int]:
        """``(有画像的个数, 总个数)`` —— 给日志与测试用。"""
        ids = [i for i in hero_ids]
        hit = sum(1 for i in ids if self.has(i))
        return hit, len(ids)

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def _render_one(
        self,
        player: dict,
        heroes: dict | None,
        focus_ids: set[int],
        fields: Sequence[str],
    ) -> tuple[str, int]:
        """渲染一位玩家。返回 ``(文本, 字符数)``；没有画像时返回 ``("", 0)``。"""
        hero_id = player.get("hero_id")
        rec = self.profile(hero_id)
        if not rec:
            return "", 0

        name = self.hero_name(hero_id, heroes)
        aid = _int(player.get("account_id"))
        nick = str(player.get("personaname") or "").strip()
        if not nick:
            # 匿名玩家：OpenDota 常给空串。用 account_id 兜底，没有就干脆不写，
            # 绝不写成「玩家：None」这种漏子。
            nick = f"account_id={aid}" if aid else ""

        tail = []
        if nick:
            tail.append(f"玩家：{nick}")
        if aid and aid in focus_ids:
            tail.append("本局焦点")
        head = f"- {name}" + (f"（{'，'.join(tail)}）" if tail else "")

        lines = [head]
        for f in fields:
            v = str(rec.get(f) or "").strip()
            if v:
                lines.append(f"  - {LABELS.get(f, f)}：{v}")
        text = "\n".join(lines)
        # 计长度时把换行与缩进也算上：预算要能真实反映提示词增量。
        return text, len(text)

    def render_review_block(
        self,
        players: Sequence[dict] | None,
        heroes: dict | None = None,
        focus_ids: Sequence[int] | None = None,
    ) -> str:
        """渲染「十人英雄判读参考」块；无可渲染内容时返回空串。

        Args:
            players: 本局玩家列表（``match["players"]``）。
            heroes: ``{hero_id: {...}}``，只用于兜底英雄名 —— 画像自带中文名，
                所以**可以为 None**。
            focus_ids: 焦点玩家 ``account_id``，用于在条目上标「本局焦点」。

        Returns:
            可直接插进提示词的整段文本（含块头与纪律），或空串。
        """
        people = [p for p in (players or []) if isinstance(p, dict)]
        if not people:
            return ""

        focus = {_int(i) for i in (focus_ids or []) if _int(i)}
        budget = self.budget if self.budget > 0 else 10**9

        # 阵营分组：天辉在前、夜魇在后，组内保持原始顺序（数据块也是这个顺序，
        # 模型对齐时不用再做一次映射）。
        radiant = [p for p in people if is_radiant(p.get("player_slot"))]
        dire = [p for p in people if not is_radiant(p.get("player_slot"))]

        body: list[str] = []
        used = 0
        degraded: list[str] = []
        skipped: list[str] = []

        for label, group in (("天辉", radiant), ("夜魇", dire)):
            section: list[str] = []
            for player in group:
                if not self.profile(player.get("hero_id")):
                    continue
                hero_name = self.hero_name(player.get("hero_id"), heroes)

                if used >= budget:
                    skipped.append(hero_name)
                    continue

                # 先按全字段渲染；装不下就用精简集**重算一遍**而不是截断字符串 ——
                # 截断会把「复盘基准」这种关键段落砍成半句话。
                text, size = self._render_one(player, heroes, focus, REVIEW_FIELDS)
                if used + size > budget:
                    lean, lean_size = self._render_one(
                        player, heroes, focus, _FALLBACK_FIELDS
                    )
                    if lean and used + lean_size <= budget:
                        text, size = lean, lean_size
                        degraded.append(hero_name)
                    else:
                        # 降级也装不下：整条跳过。**不能同时记进 degraded** ——
                        # 否则同一个人会既出现在「只给了精简版」又出现在
                        # 「完全没给」两条说明里，自相矛盾。
                        skipped.append(hero_name)
                        continue
                if not text:
                    skipped.append(hero_name)
                    continue
                section.append(text)
                used += size
            if section:
                body.append(f"\n【{label}】")
                body.extend(section)

        if not body:
            return ""

        out = ["=== 英雄判读参考（常见打法，不是限定）===", DISCIPLINE]
        out.extend(body)

        # 降级/遗漏必须写明：模型若以为「没写就是没要求」，会拿别处的标准硬判。
        if degraded:
            out.append(
                "\n（篇幅所限，以下英雄只给了常见位置、不能做什么与复盘基准："
                + "、".join(dict.fromkeys(degraded))
                + "）"
            )
        if skipped:
            out.append(
                "（篇幅所限，以下英雄没有给参考，按你对该英雄的通用认知判："
                + "、".join(dict.fromkeys(skipped))
                + "）"
            )
        return "\n".join(out)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def render_profile_text(name: str, rec: dict[str, str]) -> str:
    """把**单个英雄**的画像渲染成一段可读文本（给闲聊问答用）。

    与 :meth:`HeroKnowledge.render_review_block` 的区别：那个是「本局十人」的
    清单，带块头纪律、带预算降级；这个是**单英雄问答**，一次只讲一个英雄，
    所以不降级、不截断 —— 一条画像实测一千五百字左右，整条给它才讲得全。

    字段顺序照 :data:`LABELS` 的定义顺序走（位置 → 流派 → 输出 → ... →
    复盘基准），这个顺序本身是有叙事的：先说「他一般怎么玩」，再说
    「他做不到什么」，最后才是「什么算失职」。倒过来读会像在数落人。
    """
    lines = ["%s（英雄判读参考）" % name, ""]
    lines.append(
        "以下是**常见对局**里的主流打法与判读基准，**不是限定** —— "
        "本局走的号位、选的流派、出的装备与这里不同时，按本局实际打法判，"
        "不要拿参考去否定他。"
    )
    lines.append("")
    for key, label in LABELS.items():
        val = str(rec.get(key) or "").strip()
        if val:
            lines.append("· %s：%s" % (label, val))
    return "\n".join(lines).strip()
