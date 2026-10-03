# -*- coding: utf-8 -*-
"""玩家英雄池的取数与口径：**只看当前版本**，并且**包含加速模式**。

为什么要有这个模块
------------------
之前几处「英雄池」都直接拿 ``/players/{id}/heroes`` 的返回，那是个
**全生涯累计**，既不认版本、也拿不到加速模式。两件事都会让结论反向：

1. **不认版本**。英雄强度是版本决定的，全生涯池里躺着几年前的东西。
   问「他该练什么」时把上个版本的绝活算进来，等于给过期答案。
2. **拿不到加速模式**（这个更致命）。OpenDota 把加速模式归进
   ``insignificant`` 默认整套丢掉，而且**静默丢**——不报错、字段正常，
   只是 ``games`` 少一大截。实测监听名单里 6 个人，
   当前版本真实场次 vs 旧口径场次：

   ============ ====== ====== ====== ====== ====== ======
   玩家          钢板   A      B      C      D      E
   ============ ====== ====== ====== ====== ====== ======
   旧口径        17     1      5      0      0      29
   真实（含加速） 407    101    221    548    629    340
   ============ ====== ====== ====== ====== ====== ======

   这群人**九成以上的局是加速模式**，旧口径下他们的「英雄池」几乎是空的，
   据此算出来的「他会不会玩这个英雄」「该给他推荐什么」自然全错。

口径
----
* **版本**：以 ``/constants/patch`` 的最新补丁为准，只统计该补丁的场次
  （服务端 ``patch=`` 过滤，比拿最近 N 场自己按时间切更准——对局列表
  返回体里根本没有 patch 字段，客户端切不了）。
* **样本不足时向前放宽**：当前版本总场次低于 :data:`DEFAULT_MIN_GAMES`
  时，按补丁由新到旧累加，最多 :data:`DEFAULT_MAX_PATCHES` 个版本，
  并**如实标注**实际覆盖了哪些版本、为什么放宽。
* **含加速**：默认纳入，并标注其中加速占多少——加速局的胜负与正常局
  不是一个含金量，混在一起看排名可以，但不能不告诉用户。
* **数据源不支持时如实降级**：STRATZ 没有版本维度（见
  ``StratzClient.SUPPORTS_PATCH_FILTER``）。此时退回全量口径并在文案里
  写明「未按版本过滤」，**绝不拿全生涯数据冒充当前版本**。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from astrbot.api import logger

from dota_format import hname, summarize_hero_history

__all__ = [
    "GAME_MODE_TURBO",
    "HeroPool",
    "collect_hero_pool",
    "format_hero_pool",
    "hero_pool_note",
    "hero_pool_scope_text",
    "SCOPE_CURRENT",
    "SCOPE_EXTENDED",
    "SCOPE_ALL_TIME",
]

#: 加速模式（Turbo）的 ``game_mode`` 编号，与 dota_chat 里那份保持同一个值
GAME_MODE_TURBO = 23

#: 当前版本样本低于此值，就并入更早的版本。
#:
#: 取 30 的依据（实测分布）：活跃玩家当前版本普遍在 100~600 场，
#: 30 场意味着前几名的英雄各有 5~10 场样本，够排个序；再低就纯属噪音。
#: 版本刚放出时所有人都够不着这个数，于是自动退成「当前 + 上一版本」，
#: 而不是给出一份只有 3 场样本的「当前版本英雄池」。
DEFAULT_MIN_GAMES = 30

#: 最多向前放宽几个版本。再多就不是「近期版本」而是考古了。
DEFAULT_MAX_PATCHES = 3

#: 单次取数超时
DEFAULT_TIMEOUT = 30.0

#: 口径：只用了当前版本
SCOPE_CURRENT = "current"
#: 口径：当前版本样本不足，已并入更早的版本
SCOPE_EXTENDED = "extended"
#: 口径：没有版本过滤（数据源不支持 / 拿不到补丁表 / 用户关掉了版本口径）
SCOPE_ALL_TIME = "all_time"


@dataclass
class HeroPool:
    """一次英雄池取数的结果：数据 + 口径说明。"""

    #: 英雄行，形状与 :func:`dota_format.summarize_hero_history` 一致
    #: （``hero_id`` / ``games`` / ``win`` / ``winrate``），按场次倒序
    rows: list[dict] = field(default_factory=list)
    #: 实际覆盖的补丁（新→旧）。空表示没按版本过滤
    patches: list[dict] = field(default_factory=list)
    #: 补丁表里最新的那个（用于文案里写「当前版本是 X」）
    latest_patch: dict = field(default_factory=dict)
    #: 口径，见 SCOPE_* 常量
    scope: str = SCOPE_ALL_TIME
    #: 总场次（覆盖版本内的全部模式）
    games: int = 0
    #: 其中加速模式场次
    turbo_games: int = 0
    #: 放宽的原因 / 降级的原因，供文案与排障
    reason: str = ""

    # ------------------------------------------------------------------
    @property
    def normal_games(self) -> int:
        """非加速场次。"""
        return max(0, self.games - self.turbo_games)

    @property
    def patch_names(self) -> list[str]:
        """覆盖到的补丁名，新→旧。"""
        return [str(p.get("name") or "") for p in self.patches if p.get("name")]

    @property
    def has_data(self) -> bool:
        return bool(self.rows)

    @property
    def wide(self) -> bool:
        """是否放宽过版本（跨了不止一个补丁）。"""
        return len(self.patches) > 1

    def hero_games(self, hero_id: int) -> int:
        """某英雄在本口径下的场次（不在池里返回 0）。"""
        for row in self.rows:
            if row.get("hero_id") == hero_id:
                return int(row.get("games") or 0)
        return 0


async def _fetch_heroes(
    api: Any,
    account_id: int,
    *,
    patch: int | None = None,
    game_mode: int | None = None,
    include_turbo: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict]:
    """拉一次英雄统计，失败/超时返回空列表（英雄池取不到不该打断整条回答）。"""
    kwargs: dict[str, Any] = {"include_insignificant": include_turbo}
    if patch is not None:
        kwargs["patch"] = int(patch)
    if game_mode is not None:
        kwargs["game_mode"] = int(game_mode)
    try:
        coro = api.get_player_heroes(account_id, **kwargs)
    except TypeError:
        # 老签名（不接受这些关键字）——例如别处塞进来的简易桩。
        # 退化成旧口径调用：至少还能给出一份全量英雄池，而不是报错。
        logger.debug("[dota2] 英雄池：数据源不支持过滤参数，退回全量调用")
        coro = api.get_player_heroes(account_id)
    try:
        rows = await asyncio.wait_for(coro, timeout=max(0.1, timeout))
    except Exception as e:  # noqa: BLE001 - 取不到就是没有，交给上层措辞
        logger.warning(f"[dota2] 英雄池取数失败（patch={patch} mode={game_mode}）: {e}")
        return []
    return [row for row in (rows or []) if isinstance(row, dict)]


def _sum_games(rows: Iterable[dict]) -> int:
    return sum(int(row.get("games") or 0) for row in rows if isinstance(row, dict))


def _merge(target: dict[int, dict], rows: Iterable[dict]) -> None:
    """把一批英雄行累加进 ``target``（跨版本合并用）。

    只累加 ``games`` / ``win``，``last_played`` 取较新的一次。**不累加
    KDA / GPM 这类均值型字段**——两个版本的样本混合后的均值没有意义，
    留着反而会让渲染层以为它是可用的。
    """
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            hero_id = int(row.get("hero_id") or 0)
        except (TypeError, ValueError):
            continue
        if hero_id <= 0:
            continue
        entry = target.setdefault(hero_id, {"games": 0, "win": 0, "last_played": 0})
        entry["games"] += int(row.get("games") or 0)
        entry["win"] += int(row.get("win") or 0)
        try:
            last = int(row.get("last_played") or 0)
        except (TypeError, ValueError):
            last = 0
        if last > entry["last_played"]:
            entry["last_played"] = last


async def collect_hero_pool(
    api: Any,
    account_id: int,
    *,
    min_games: int = DEFAULT_MIN_GAMES,
    max_patches: int = DEFAULT_MAX_PATCHES,
    include_turbo: bool = True,
    patch_scope: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
    summarize: Callable[[list[dict]], list[dict]] | None = None,
) -> HeroPool:
    """取某个玩家的英雄池，口径见模块文档。

    Args:
        api: 数据源客户端（可能是 ``FallbackDataSource``）。
        account_id: 玩家账号。
        min_games: 当前版本样本下限，低于它就向前放宽版本。
        max_patches: 最多覆盖几个版本。
        include_turbo: 是否纳入加速模式。**建议保持 True**，关掉就退回
            「只有正常局」的旧口径（也正是那个让英雄池几乎为空的坑）。
        patch_scope: 是否启用版本口径。关掉则一律全量。
        timeout: 单次请求超时。
        summarize: 结果整理函数，默认用
            :func:`dota_format.summarize_hero_history`。

    Returns:
        :class:`HeroPool`。**任何取数失败都返回空池**，不抛异常。
    """
    to_rows = summarize or summarize_hero_history
    pool = HeroPool()

    supports_patch = bool(getattr(api, "SUPPORTS_PATCH_FILTER", False))
    if not patch_scope:
        pool.reason = "版本口径已关闭"
    elif not supports_patch:
        pool.reason = "当前数据源不支持按版本过滤英雄池（STRATZ 无此维度）"

    if pool.reason:
        # 全量口径：一次请求拿完（含加速），并如实标注
        rows = await _fetch_heroes(
            api, account_id, include_turbo=include_turbo, timeout=timeout
        )
        turbo = 0
        if include_turbo:
            turbo = _sum_games(
                await _fetch_heroes(
                    api,
                    account_id,
                    game_mode=GAME_MODE_TURBO,
                    include_turbo=True,
                    timeout=timeout,
                )
            )
        pool.rows = to_rows(rows)
        pool.games = _sum_games(rows)
        pool.turbo_games = min(turbo, pool.games)
        pool.scope = SCOPE_ALL_TIME
        logger.info(f"[dota2] 英雄池走全量口径（{pool.reason}）")
        return pool

    get_patches = getattr(api, "get_patches", None)
    patches: list[dict] = []
    if get_patches is not None:
        try:
            patches = list(
                await asyncio.wait_for(
                    get_patches(limit=max(1, int(max_patches))),
                    timeout=max(0.1, timeout),
                )
                or []
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dota2] 补丁表取数失败，英雄池退回全量口径: {e}")
            patches = []
    if not patches:
        pool.reason = "拿不到补丁表，无法按版本过滤"
        rows = await _fetch_heroes(
            api, account_id, include_turbo=include_turbo, timeout=timeout
        )
        turbo = 0
        if include_turbo:
            turbo = _sum_games(
                await _fetch_heroes(
                    api,
                    account_id,
                    game_mode=GAME_MODE_TURBO,
                    include_turbo=True,
                    timeout=timeout,
                )
            )
        pool.rows = to_rows(rows)
        pool.games = _sum_games(rows)
        pool.turbo_games = min(turbo, pool.games)
        pool.scope = SCOPE_ALL_TIME
        return pool

    pool.latest_patch = dict(patches[0])
    merged: dict[int, dict] = {}
    covered: list[dict] = []
    total = 0
    turbo_total = 0

    for patch in patches:
        pid = patch.get("id")
        if pid is None:
            continue
        # 全模式 + 加速模式两次请求：前者给英雄维度，后者只用来算「其中加速多少场」
        rows = await _fetch_heroes(
            api, account_id, patch=int(pid), include_turbo=include_turbo, timeout=timeout
        )
        patch_games = _sum_games(rows)
        turbo_games = 0
        if include_turbo:
            turbo_games = _sum_games(
                await _fetch_heroes(
                    api,
                    account_id,
                    patch=int(pid),
                    game_mode=GAME_MODE_TURBO,
                    include_turbo=True,
                    timeout=timeout,
                )
            )
        _merge(merged, rows)
        covered.append(dict(patch))
        total += patch_games
        turbo_total += min(turbo_games, patch_games)
        if total >= max(1, int(min_games)):
            break

    flattened = [
        {"hero_id": hero_id, **entry} for hero_id, entry in merged.items()
    ]
    pool.rows = to_rows(flattened)
    pool.patches = covered
    pool.games = total
    pool.turbo_games = turbo_total
    if len(covered) <= 1:
        pool.scope = SCOPE_CURRENT
    else:
        pool.scope = SCOPE_EXTENDED
        names = " + ".join(pool.patch_names)
        pool.reason = (
            f"当前版本样本不足（{min_games} 场门槛），已并入更早的版本一起统计"
            f"（覆盖 {names}）"
        )
    if total < max(1, int(min_games)) and len(covered) >= len(patches):
        pool.reason = (
            f"最近 {len(covered)} 个版本合计仅 {total} 场，样本仍然偏少"
        )
    return pool


def hero_pool_scope_text(pool: HeroPool) -> str:
    """一行口径摘要，给报告抬头 / 提示词复用。"""
    if not pool.patches:
        base = "全部历史（未按版本过滤）"
    else:
        base = f"{' + '.join(pool.patch_names)} 版本"
    if not pool.games:
        return base
    if pool.turbo_games:
        detail = f"{pool.games} 场（加速 {pool.turbo_games} / 普通 {pool.normal_games}）"
    else:
        detail = f"{pool.games} 场"
    return f"{base} · {detail}"


def hero_pool_note(pool: HeroPool) -> str:
    """给模型看的口径提醒（放进提示词，避免它把口径说错）。"""
    if not pool.has_data:
        return ""
    parts = [f"英雄池口径：{hero_pool_scope_text(pool)}"]
    if pool.reason:
        parts.append(pool.reason)
    if pool.turbo_games:
        parts.append(
            "加速模式与正常局的胜负含金量不同，谈到胜率时可以提一句加速占比，"
            "不要把两个模式的胜率直接当成天梯强度"
        )
    return "；".join(parts) + "。"


def format_hero_pool(
    pool: HeroPool,
    player_name: str,
    heroes: dict[int, dict] | None = None,
    top: int = 12,
) -> str:
    """渲染英雄池。

    抬头**必须**写清版本与模式构成：这是这一版最容易被忽略的信息 ——
    同一份「胜率 55%」，在只有正常局和九成加速两种口径下含义完全不同。
    """
    if not pool.has_data:
        return f"没有查询到 {player_name} 的英雄使用记录。"

    total_heroes = len(pool.rows)
    headline = f"🦸 {player_name} 的英雄池"
    scope_line = hero_pool_scope_text(pool)

    lines = [headline, scope_line, ""]
    for index, row in enumerate(pool.rows[:max(1, top)], start=1):
        games = int(row.get("games") or 0)
        wins = int(row.get("win") or 0)
        winrate = float(row.get("winrate") or 0.0)
        lines.append(
            f"{index:>2}. {hname(heroes, row.get('hero_id'))}　{games} 场 "
            f"{wins} 胜　胜率 {winrate:.1f}%"
        )
    if total_heroes > top:
        lines.append(f"... 以及另外 {total_heroes - top} 个英雄")
    if pool.reason:
        lines.append("")
        lines.append(f"注：{pool.reason}。")
    return "\n".join(lines)
