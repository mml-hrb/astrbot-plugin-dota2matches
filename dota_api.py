"""OpenDota API 的异步客户端封装。

设计要点：
- 全异步（httpx.AsyncClient），不阻塞事件循环。
- 内置令牌桶式限流，保护 OpenDota 免费配额。
- 网络抖动 / 429 / 5xx 自动重试，带指数退避。
- 英雄常量等低频数据本地缓存，避免重复请求。
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Protocol

import httpx
from astrbot.api import logger

if TYPE_CHECKING:  # pragma: no cover - 仅静态检查期存在，运行期不导入
    # ⚠️ 必须放在 TYPE_CHECKING 里：`dota_stratz` 模块级就 import 了本模块的
    # `OpenDotaError`（见其类定义），这里若在运行期反向 import 会构成真循环。
    from .dota_stratz import StratzClient

#: 32 位 account_id 与 64 位 SteamID 的换算基准
STEAM_ID_64_BASE = 76561197960265728

#: OpenDota API 根地址
BASE_URL = "https://api.opendota.com/api"

#: 英雄常量缓存有效期（秒）
HERO_CACHE_TTL = 12 * 3600

#: 英雄版本胜率（``/heroStats``）缓存有效期（秒）。
#:
#: 该接口返回的是**最近 7 天滚动窗口**的聚合统计，分钟级变化没有意义，
#: 但也不能缓存太久——版本更新当天玩家会关心最新数据。3 小时是个折中。
HERO_STATS_CACHE_TTL = 3 * 3600

#: 补丁表（``/constants/patch``）缓存有效期（秒）。补丁几个月才动一次。
PATCH_CACHE_TTL = 12 * 3600

#: 战队索引（``/teams``）缓存有效期（秒）。
#:
#: 这个端点返回**评分最高的 1000 支队伍**（实测约 250KB），用来把「队伍名」
#: 解析成 ``team_id``。体量不大但也不该每次查询都拉，24 小时足够。
TEAMS_INDEX_CACHE_TTL = 24 * 3600

#: recentMatches 接口的硬上限（OpenDota 固定最多只返回最近 20 场）
RECENT_MATCHES_LIMIT = 20

#: 「经济类」字段。只有 recentMatches 接口会返回它们；
#: ``/players/{id}/matches`` 只返回 KDA / 英雄 / 时长等基础字段。
ECONOMY_FIELDS = (
    "gold_per_min",
    "xp_per_min",
    "last_hits",
    "denies",
    "hero_damage",
    "tower_damage",
    "hero_healing",
    "lane",
    "lane_role",
    "is_roaming",
)


class OpenDotaError(Exception):
    """OpenDota 调用失败（网络异常、限流、服务端错误等）。"""


class TargetNotFoundError(OpenDotaError):
    """找不到对应的玩家。"""


class AmbiguousTargetError(OpenDotaError):
    """昵称匹配到多个玩家，需要用户进一步确认。"""


def to_account_id(value: Any) -> int | None:
    """把 32 位 account_id 或 64 位 SteamID 统一转换成 32 位 account_id。

    Args:
        value: 数字或数字字符串。

    Returns:
        合法的 32 位 account_id；无法解析时返回 None。
    """
    try:
        num = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if num <= 0:
        return None
    # 超过 32 位范围的一律按 64 位 SteamID 处理
    if num > 0xFFFFFFFF:
        num -= STEAM_ID_64_BASE
    if 0 < num <= 0xFFFFFFFF:
        return num
    return None


def to_steam_id64(account_id: int | str) -> str:
    """把 32 位 account_id 转换成 64 位 SteamID 字符串。"""
    try:
        return str(int(account_id) + STEAM_ID_64_BASE)
    except (TypeError, ValueError):
        return ""


class RateLimiter:
    """最小间隔闸门：保证两次 ``acquire()`` 之间至少隔 ``60 / per_minute`` 秒。

    ⚠️ 名字里的「令牌桶」是历史误称（2026-10-14 改正）：这里**没有桶、也没有
    突发额度** —— 它只用「上一次请求的时刻」算一个下限，均匀铺开每次请求。
    真正的令牌桶允许攒额度后连打，本类不允许，这是刻意的：OpenDota 的限流按
    分钟计，均匀铺开比攒一波再打更安全。

    也**没有** ``configure()``（同日删除）：它当年在锁外直接改
    ``_min_interval``，而且从来没有调用点 —— 限速值来自配置，实例构造时定死。
    要改就跑新的实例，别让一个运行中的限速器在中途换档。
    """

    def __init__(self, per_minute: int) -> None:
        self._lock = asyncio.Lock()
        self._min_interval = 60.0 / max(1, per_minute)
        self._last_request_at = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            wait = self._last_request_at + self._min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()


class OpenDotaClient:
    """OpenDota API 客户端。"""

    #: 数据源身份标记。有些能力只归属某一端（如 ``request_parse``、
    #: ``get_hero_stats``），组合里要按**类型**定位而不能按 primary/secondary
    #: 定位 —— 否则用户切换 ``data_source_priority`` 后会静默失效。
    #: 用显式标记而不是 ``isinstance``：打桩/包装类同样能声明身份。
    SOURCE_KIND = "opendota"

    #: 本数据源能否**按补丁（版本）过滤**玩家数据。
    #:
    #: OpenDota 的 ``/players/{id}/heroes`` 与 ``/players/{id}/matches``
    #: 都接受 ``patch`` 参数（值取 ``/constants/patch`` 的 ``id``）。
    #: 这是「英雄池只看当前版本」这件事的地基 —— 拿掉它就只剩时间窗口
    #: 这种近似手段。STRATZ 侧没有对应维度（实测 ``patchIds`` 报
    #: ``Unknown field``），所以那边必须显式声明 False，由上层决定怎么降级，
    #: **绝不能默默返回全生涯数据冒充当前版本**。
    SUPPORTS_PATCH_FILTER = True

    def __init__(
        self,
        api_key: str = "",
        timeout: int = 30,
        max_retries: int = 3,
        rate_limit_per_minute: int = 55,
        proxy: str = "",
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.timeout = max(5, int(timeout))
        self.max_retries = max(0, int(max_retries))
        self.proxy = (proxy or "").strip()
        self._limiter = RateLimiter(rate_limit_per_minute)
        self._client: httpx.AsyncClient | None = None
        self._hero_cache: dict[int, dict] | None = None
        self._hero_cache_at = 0.0
        self._item_cache: dict[str, dict] | None = None
        self._item_cache_at = 0.0
        #: 技能常量（id → 技能名）。None 表示还没拉过；空 dict 表示拉过但失败。
        self._ability_cache: dict[int, str] | None = None
        self._ability_cache_at = 0.0
        #: 英雄版本胜率原始行（``/heroStats``）。同上：None 未拉过、空 list 拉过但失败。
        self._hero_stats_cache: list[dict] | None = None
        self._hero_stats_cache_at = 0.0
        #: 补丁列表（``/constants/patch``），**按时间倒序**（最新在前）。
        #: None 表示还没拉过；空 list 表示拉过但失败。
        self._patches_cache: list[dict] | None = None
        self._patches_cache_at = 0.0
        #: 战队索引（``/teams``，评分前 1000）。同上：None 未拉过、空 list 拉过但失败。
        self._teams_index_cache: list[dict] | None = None
        self._teams_index_at = 0.0

    # ------------------------------------------------------------------
    # 底层请求
    # ------------------------------------------------------------------
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            kwargs: dict[str, Any] = {
                "base_url": BASE_URL,
                "timeout": httpx.Timeout(self.timeout, connect=min(15, self.timeout)),
                "follow_redirects": True,
                "headers": {
                    "User-Agent": "AstrBot-Plugin-Dota2/1.0 (+https://github.com/AstrBotDevs)",
                    "Accept": "application/json",
                },
            }
            if self.proxy:
                kwargs["proxy"] = self.proxy
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def close(self) -> None:
        """关闭底层连接池，插件卸载时调用。"""
        if self._client is not None and not self._client.is_closed:
            try:
                await self._client.aclose()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[dota2] 关闭 HTTP 客户端失败: {e}")
        self._client = None

    async def _request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        allow_404: bool = False,
        method: str = "GET",
    ) -> Any:
        """发起一次请求，内置限流与重试。

        Args:
            path: 接口路径，如 ``/players/12345``。
            params: 查询串参数，值为 None 的项会被忽略。
            timeout: 本次请求的超时时间，None 表示用默认值。
            allow_404: 为 True 时 404/400 返回 None 而不是抛异常。
            method: HTTP 方法。OpenDota 的 ``/request/{match_id}``
                （提交解析任务）只接受 **POST**，其余接口都是 GET。
        """
        merged: dict[str, Any] = {}
        for key, value in (params or {}).items():
            if value is None:
                continue
            merged[key] = value
        if self.api_key:
            merged["api_key"] = self.api_key

        url = path if path.startswith("/") else f"/{path}"
        verb = method.upper()
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            await self._limiter.acquire()
            try:
                # POST 不带请求体：OpenDota 的解析接口只认方法，不认 body
                resp = await self._get_client().request(
                    verb,
                    url,
                    params=merged or None,
                    timeout=timeout or self.timeout,
                )
            except httpx.HTTPError as e:
                last_error = e
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(min(8.0, 1.5 * (2**attempt)))
                continue

            status = resp.status_code
            if status == 200:
                try:
                    return resp.json()
                except ValueError as e:
                    last_error = e
                    if attempt >= self.max_retries:
                        break
                    await asyncio.sleep(1.5)
                    continue

            if status in (404, 400):
                if allow_404:
                    return None
                raise TargetNotFoundError(f"OpenDota 接口 {url} 返回 {status}")

            if status == 429:
                retry_after = 5.0
                try:
                    retry_after = float(resp.headers.get("Retry-After", "") or 5.0)
                except ValueError:
                    retry_after = 5.0
                last_error = OpenDotaError("OpenDota 请求频率超限（429）")
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(min(30.0, max(2.0, retry_after)))
                continue

            # 5xx / 502 / 504 等
            last_error = OpenDotaError(f"OpenDota 接口 {url} 返回 {status}")
            if attempt >= self.max_retries:
                break
            await asyncio.sleep(min(8.0, 1.5 * (2**attempt)))

        detail = (
            f"{type(last_error).__name__}: {last_error}" if last_error else "未知错误"
        )
        raise DataSourceUnavailableError(f"请求 OpenDota 失败（{detail}）")

    # ------------------------------------------------------------------
    # 英雄常量
    # ------------------------------------------------------------------
    async def get_heroes(self, force: bool = False) -> dict[int, dict]:
        """获取英雄常量表，返回 ``{hero_id: hero_info}``。"""
        now = time.time()
        if (
            not force
            and self._hero_cache is not None
            and now - self._hero_cache_at < HERO_CACHE_TTL
        ):
            return self._hero_cache

        data = await self._request("/constants/heroes")
        heroes: dict[int, dict] = {}
        if isinstance(data, dict):
            for key, value in data.items():
                try:
                    heroes[int(key)] = value
                except (TypeError, ValueError):
                    continue
        self._hero_cache = heroes
        self._hero_cache_at = now
        return heroes

    async def hero_name(self, hero_id: int | None) -> str:
        """把 hero_id 转成英雄名，失败时回退为 ``英雄#id``。"""
        if not hero_id:
            return "未知英雄"
        try:
            heroes = await self.get_heroes()
        except OpenDotaError:
            return f"英雄#{hero_id}"
        info = heroes.get(int(hero_id))
        if not info:
            return f"英雄#{hero_id}"
        return info.get("localized_name") or f"英雄#{hero_id}"

    async def get_items(self, force: bool = False) -> dict[str, dict]:
        """获取道具常量表，返回 ``{item_key: item_info}``。"""
        now = time.time()
        if (
            not force
            and self._item_cache is not None
            and now - self._item_cache_at < HERO_CACHE_TTL
        ):
            return self._item_cache

        try:
            data = await self._request("/constants/items", timeout=max(30, self.timeout))
        except OpenDotaError as e:
            logger.debug(f"[dota2] 获取道具常量失败: {e}")
            data = {}

        items: dict[str, dict] = {}
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, dict):
                    items[str(key)] = value
        self._item_cache = items
        self._item_cache_at = now
        return items

    async def get_ability_names(self, force: bool = False) -> dict[int, str]:
        """获取技能常量表，返回 ``{ability_id: ability_key}``。

        用途很具体：解析产物里的 ``ability_upgrades_arr`` 存的是技能**数字 ID**
        （``5439`` 这种），没有这张表就完全不可读，加点顺序也就没法喂给大模型。

        这是**尽力而为**的增强项：接口不可用时返回空字典（并做负缓存，避免
        每次分析都白跑一趟），调用方按「拿不到就不输出加点小节」处理。
        """
        now = time.time()
        if (
            not force
            and self._ability_cache is not None
            and now - self._ability_cache_at < HERO_CACHE_TTL
        ):
            return self._ability_cache

        names: dict[int, str] = {}
        try:
            data = await self._request("/constants/ability_ids", timeout=max(30, self.timeout))
        except OpenDotaError as e:
            logger.debug(f"[dota2] 获取技能常量失败，跳过技能加点：{e}")
            data = {}

        rows = data.values() if isinstance(data, dict) else data
        if isinstance(rows, (list, dict)):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                raw_id = row.get("id")
                if raw_id is None:
                    continue
                try:
                    ability_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                name = row.get("name") or row.get("dname")
                if ability_id and name:
                    names[ability_id] = str(name)
        # 负缓存：失败时也记时间戳，避免每次分析都重试
        self._ability_cache = names
        self._ability_cache_at = now
        return names

    # ------------------------------------------------------------------
    # 玩家相关
    # ------------------------------------------------------------------
    async def get_player(self, account_id: int) -> dict | None:
        """获取玩家资料。不存在的账号会返回 None。"""
        data = await self._request(f"/players/{int(account_id)}", allow_404=True)
        if not isinstance(data, dict) or not data.get("profile"):
            return None
        return data

    async def search_player(self, query: str, timeout: float = 60.0) -> list[dict]:
        """按昵称搜索玩家。

        OpenDota 的 /search 接口偶发响应较慢，因此单独放宽超时。
        """
        data = await self._request(
            "/search", params={"q": query}, timeout=timeout, allow_404=True
        )
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict) and item.get("account_id")]

    async def get_player_matches(
        self,
        account_id: int,
        limit: int = 20,
        offset: int = 0,
        win: int | None = None,
        lose: int | None = None,
        hero_id: int | None = None,
        significant: int = 0,
        game_mode: int | None = None,
        lobby_type: int | None = None,
    ) -> list[dict]:
        """按时间倒序获取玩家最近的比赛列表。

        筛选口径（2026-10-27 实测，账号 297901780）：

        * ``limit`` 作用在**过滤之后** —— ``hero_id=X, limit=10`` 是「某英雄最近 10 场」，
          不是「最近 10 场里的某英雄」。想表达后者只能自己取多了再筛。
        * 带筛选时会**往前翻历史凑满 limit**，所以「某英雄 + 最近 N 天」不能只靠它，
          时间维度仍得在调用方按 ``start_time`` 过滤。
        * ``game_mode`` / ``lobby_type`` 都只接受**单个值**：想查「除 Turbo 外全部」
          这类口径，接口没有对应参数，不要用假值凑。

        Args:
            game_mode: 游戏模式编号（22=全阵营选择，23=Turbo）。实测有效。
            lobby_type: 大厅类型编号（0=普通，7=天梯）。实测有效。
            significant: OpenDota 的「显著性」过滤，**默认 0（所有模式）**。

                - ``significant=1``（OpenDota 接口默认）：只返回常规竞技模式
                  （Ranked All Pick、队长模式等），**Turbo 等加速/活动模式
                  会被排除**——实测只打 Turbo 的玩家在此口径下可能返回
                  几个月前的旧局，甚至一条都没有；
                - ``significant=0``：返回所有模式的最近比赛，是「玩家最近
                  实际打了什么」的正确口径。插件的战绩/分析/监听场景
                  都应该用这个。

                （该参数的官方说明：*"Whether the match was significant
                for aggregation purposes. Defaults to 1 (true), set this
                to 0 to return data for non-standard modes/matches."*）
        """
        data = await self._request(
            f"/players/{int(account_id)}/matches",
            params={
                "limit": int(limit),
                "offset": int(offset),
                "win": win,
                "lose": lose,
                "hero_id": hero_id,
                "game_mode": game_mode,
                "lobby_type": lobby_type,
                "significant": int(significant),
            },
        )
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    async def get_recent_matches(self, account_id: int, limit: int = 20) -> list[dict]:
        """获取玩家最近比赛（recentMatches 接口，响应更小且带经济类字段）。

        注意：该接口固定最多返回 20 场（OpenDota 硬上限）。
        """
        data = await self._request(
            f"/players/{int(account_id)}/recentMatches", params={"limit": int(limit)}
        )
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)][
            :RECENT_MATCHES_LIMIT
        ]

    async def get_player_matches_enriched(
        self, account_id: int, limit: int = 20, **filters: Any
    ) -> tuple[list[dict], int]:
        """获取玩家**真正的最近 N 场**（所有模式），并尽可能补全经济类字段。

        OpenDota 的两个接口各有取舍：

        - ``/players/{id}/matches?significant=0`` 支持任意 limit / offset 且
          **包含所有模式**（Turbo 等），但只返回 KDA、英雄、时长等基础字段；
        - ``/players/{id}/recentMatches`` 带 GPM / XPM / 补刀 / 伤害 / 分路等
          字段，但固定最多 20 场。

        ⚠️ 重要经验（实测踩坑）：

        1. ``matches`` 端点**默认 ``significant=1``，会把 Turbo 等非标准模式
           整个排除**——只打 Turbo 的玩家在此口径下会返回几个月前的旧局。
           必须显式传 ``significant=0`` 才是「最近实际打了什么」的正确口径。
        2. ``significant=0`` 的结果与 ``recentMatches`` 的 ``match_id`` 正常
           重叠（recent 是它的前 20 场子集），因此可以直接按 ``match_id``
           合并字段。

        合并策略：以 ``significant=0`` 的最近 N 场为主列表，用 ``recentMatches``
        的 20 场按 ``match_id`` 补全经济字段。

        Returns:
            ``(比赛列表, 带经济字段的场次数)`` —— 列表按开始时间倒序。

        ``**filters`` 原样转给 :meth:`get_player_matches`（``win`` / ``lose`` /
        ``hero_id`` / ``game_mode`` / ``lobby_type``）。带筛选时补字段这一步
        **照样能做**：``recentMatches`` 取的是全英雄的 20 场，按 ``match_id``
        合并，只会命中同一场比赛，不会把别的英雄的行混进来 —— 命不中就少几个
        经济字段，不会错。
        """
        limit = max(1, int(limit))
        # significant=0：包含所有模式（Turbo 等），这才是「真正最近打了什么」
        matches = await self.get_player_matches(
            account_id, limit=limit, significant=0, **filters
        )

        try:
            recent = await self.get_recent_matches(account_id)
        except OpenDotaError as e:
            logger.debug(f"[dota2] 获取 recentMatches 失败，降级为基础字段: {e}")
            recent = []

        if recent:
            rich_by_id = {m.get("match_id"): m for m in recent if m.get("match_id")}
            for match in matches:
                rich = rich_by_id.get(match.get("match_id"))
                if rich:
                    for field in ECONOMY_FIELDS:
                        if match.get(field) is None and rich.get(field) is not None:
                            match[field] = rich[field]

        matches.sort(key=lambda m: int(m.get("start_time") or 0), reverse=True)
        matches = matches[:limit]
        enriched_count = sum(
            1 for m in matches if m.get("gold_per_min") is not None
        )
        return matches, enriched_count

    async def get_player_wl(self, account_id: int) -> dict:
        """获取玩家总胜负场次。"""
        data = await self._request(f"/players/{int(account_id)}/wl")
        return data if isinstance(data, dict) else {}

    async def get_player_heroes(
        self,
        account_id: int,
        *,
        patch: int | None = None,
        game_mode: int | None = None,
        include_insignificant: bool = True,
    ) -> list[dict]:
        """获取玩家各英雄的使用统计（返回原始列表，含 games 为 0 的条目）。

        Args:
            patch: 只统计某个补丁的场次，值取 ``/constants/patch`` 的 ``id``。
                这是「英雄池只看当前版本」的实现基础——服务端过滤，
                比拿最近 N 场自己对时间切更准。
            game_mode: 只统计某个游戏模式，``23`` = 加速模式（Turbo）。
            include_insignificant: 是否纳入 OpenDota 判为「不重要」的对局。

        .. important::
            ``include_insignificant`` **默认 True，而且必须默认 True**。
            OpenDota 把加速模式整套归进 ``insignificant``，不显式传
            ``significant=0`` 就会全部丢掉——这个丢法是**静默**的，
            接口不报错、字段也正常，只是 ``games`` 少了一大截。
            实测账号 153659639 在当前版本因此只剩 **17 场**，而真实是
            **407 场（其中加速 389 场）**：这个人九成以上的局是加速模式，
            按旧口径他的「英雄池」几乎是空的，据此给出的推荐也就全错。
        """
        params: dict[str, Any] = {"patch": patch, "game_mode": game_mode}
        if include_insignificant:
            # 值为 None 的项会被 _request 丢掉，所以这里只在需要时塞进去
            params["significant"] = "0"
        data = await self._request(
            f"/players/{int(account_id)}/heroes", params=params
        )
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    async def get_player_totals(self, account_id: int, field: str = "kills") -> dict:
        """获取玩家某个维度的累计统计。"""
        data = await self._request(
            f"/players/{int(account_id)}/totals", params={"field": field}
        )
        return data if isinstance(data, dict) else {}

    async def get_player_peers(self, account_id: int) -> list[dict]:
        """获取玩家最常一起开黑的队友。"""
        data = await self._request(f"/players/{int(account_id)}/peers")
        return data if isinstance(data, list) else []

    # ------------------------------------------------------------------
    # 比赛相关
    # ------------------------------------------------------------------
    async def get_match(self, match_id: int, timeout_factor: float = 3.0) -> dict | None:
        """获取单场比赛的完整数据。

        该接口返回内容极大（解析过的比赛可能数 MB），因此超时时间会放宽。
        """
        data = await self._request(
            f"/matches/{int(match_id)}",
            timeout=self.timeout * timeout_factor,
            allow_404=True,
        )
        if not isinstance(data, dict) or not data.get("players"):
            return None
        return data

    async def request_parse(self, match_id: int) -> bool:
        """向 OpenDota 提交该场比赛的解析任务。

        .. important::
           这个接口是 **POST** ``/request/{match_id}``。用 GET 请求不会触发解析
           （OpenDota 只在该路由上注册了 POST），因此必须显式指定方法。

        注意该接口按 10 倍额度计费，调用方需要自行节流。

        Returns:
            是否成功提交了解析任务。
        """
        try:
            result = await self._request(
                f"/request/{int(match_id)}",
                timeout=min(20, self.timeout),
                method="POST",
            )
        except OpenDotaError as e:
            logger.debug(f"[dota2] 申请解析 {match_id} 失败: {e}")
            return False

        if isinstance(result, dict):
            if result.get("job"):
                return True
            # 已解析过 / 已在队列中时，OpenDota 可能不返回 job，这里如实记录
            logger.debug(f"[dota2] 申请解析 {match_id} 未返回 job: {result}")
            return False
        return False

    async def get_benchmarks(self, hero_id: int) -> dict:
        """获取某英雄的段位分位基准数据（可选增强项）。"""
        data = await self._request("/benchmarks", params={"hero_id": int(hero_id)})
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------
    # 英雄出装（「当前版本打法」用）
    # ------------------------------------------------------------------
    async def get_hero_item_popularity(self, hero_id: int) -> dict:
        """某英雄各阶段的**热门出装**（OpenDota ``GET /heroes/{id}/itemPopularity``）。

        返回四个阶段的字典，值是 ``{item_id: 出现次数}``：

        * ``start_game_items``（开局） / ``early_game_items``（前期）
        * ``mid_game_items``（中期） / ``late_game_items``（后期）

        取不到（404 / 该英雄没有统计数据）时返回空 dict，由上层如实少给一块。

        .. note::
           这是**全分段口径** —— OpenDota 这个端点不接受段位过滤。展示时
           必须如实标注，不要把它说成「高分段出装」（高分段那部分由 STRATZ
           的 ``itemStartingPurchase`` / ``itemBootPurchase`` 提供）。
        """
        data = await self._request(
            f"/heroes/{int(hero_id)}/itemPopularity", allow_404=True
        )
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------
    # 职业赛事（队伍 / 赛事 / 职业比赛）
    # ------------------------------------------------------------------
    async def get_pro_matches(
        self, limit: int = 100, *, before_match_id: int | None = None
    ) -> list[dict]:
        """最近结束的职业比赛（``GET /proMatches``）。

        每行含 ``match_id`` / ``leagueid`` / ``league_name`` /
        ``radiant_name`` / ``dire_name`` / 比分 / ``start_time``。

        ``before_match_id`` 是**翻页游标**：接口固定一次返回约 100 场，但接受
        ``less_than_match_id`` 参数往回翻。这一点 2026-10-16 才查实 —— 旧 docstring
        写的是「没有分页参数 ⇒ 想按赛事看全部对局得靠 /leagues/{id}/matches」，
        于是插件从来只看得见最近 100 场（约两天），没法给一个几天前开打的赛事
        对齐赛果。实测 3 页就能回溯两个月。

        .. note::
           对手队伍为 ``null`` 的行是**真实情况**（该队伍没在 OpenDota 登记 ID），
           调用方要如实渲染，不要补成「未知」以外的猜测。
        """
        params = {"less_than_match_id": int(before_match_id)} if before_match_id else None
        data = await self._request("/proMatches", params)
        rows = [row for row in (data or []) if isinstance(row, dict)]
        if limit and int(limit) > 0:
            return rows[: int(limit)]
        return rows

    async def get_league_matches(self, league_id: int) -> list[dict]:
        """某联赛的**全部**对局（``GET /leagues/{id}/matches``）。

        .. warning::
           这个端点的 ``radiant_team_name`` / ``dire_team_name`` **恒为 null**
           （实测 DreamLeague S30：253 行里非空的 0 个），只有
           ``radiant_team_id`` / ``dire_team_id``。队名要用
           :func:`dota_esports.od_team_name_index` 从 ``/proMatches`` 换回来。

           另外同一个 ``leagueid`` 可能把**多个赛区合在一起**
           （``DreamLeague Season 30 Qualifiers`` 是 SEA / 中国 / 东欧 … 合体），
           调用方要按参赛队名单自己切。
        """
        data = await self._request(f"/leagues/{int(league_id)}/matches", allow_404=True)
        return [row for row in (data or []) if isinstance(row, dict)]

    async def get_team(self, team_id: int) -> dict:
        """某战队的评分与总胜负（``GET /teams/{id}``）。取不到返回空 dict。"""
        data = await self._request(f"/teams/{int(team_id)}", allow_404=True)
        return data if isinstance(data, dict) else {}

    async def get_team_players(self, team_id: int) -> list[dict]:
        """某战队的选手名单（``GET /teams/{id}/players``）。

        关键字段是 ``is_current_team_member`` —— 它区分「现在在队」与
        「曾经在队」，是判断阵容变动最可靠的现成口径。
        """
        data = await self._request(f"/teams/{int(team_id)}/players", allow_404=True)
        return [row for row in (data or []) if isinstance(row, dict)]

    async def get_team_matches(self, team_id: int, limit: int = 20) -> list[dict]:
        """某战队最近的比赛（``GET /teams/{id}/matches``），**按时间倒序**。"""
        data = await self._request(f"/teams/{int(team_id)}/matches", allow_404=True)
        rows = [row for row in (data or []) if isinstance(row, dict)]
        rows.sort(key=lambda row: row.get("start_time") or 0, reverse=True)
        if limit and int(limit) > 0:
            return rows[: int(limit)]
        return rows

    async def get_teams_index(self, force: bool = False) -> list[dict]:
        """队伍索引（``GET /teams``），用于「队伍名 → team_id」。

        返回**评分最高的 1000 支**队伍（实测约 250KB），带 24 小时缓存。
        查不到的队伍（例如只在预选赛出现的小队）不在这份索引里 —— 此时
        调用方要如实说「按名字没匹配到战队」，不要猜一个 ID 去查别人。
        """
        now = time.time()
        if (
            not force
            and self._teams_index_cache is not None
            and now - self._teams_index_at < TEAMS_INDEX_CACHE_TTL
        ):
            return self._teams_index_cache
        data = await self._request("/teams", timeout=max(30, self.timeout))
        rows = [row for row in (data or []) if isinstance(row, dict)]
        self._teams_index_cache = rows
        self._teams_index_at = now
        return rows

    # ------------------------------------------------------------------
    # 英雄版本胜率（「轮椅」榜单的数据源）
    # ------------------------------------------------------------------
    async def get_hero_stats(self, force: bool = False) -> list[dict]:
        """获取「当前版本」各英雄的公开对局统计。

        数据取自 OpenDota ``GET /heroStats``，它按**最近 7 天滚动窗口**聚合
        全部分段的公开对局。窗口长度是实测出来的：每个英雄的 ``pub_pick``
        恰好等于 ``pub_pick_trend`` 里 7 项之和。

        每行除了总胜负，还带：

        * ``pub_pick`` / ``pub_win``：全分段公开对局；
        * ``1_pick`` … ``7_pick`` 及对应 ``_win``：**按水平分档**的样本
          （数字越大分段越高；第 8 档实测恒为 0，不可用，调用方要跳过）；
        * ``pub_pick_trend`` / ``pub_win_trend``：逐日样本，用来算近期走向；
        * ``roles``、``primary_attr``、``localized_name``：定位与显示名。

        .. note::
           这个窗口是**滚动 7 天**，不是严格按补丁切分。补丁刚发布的头几天，
            窗口里会混着上个版本的对局 —— 调用方必须把这一点如实告诉用户
           （见 :func:`dota_format.hero_meta_note`），不能宣称「本版本精确统计」。
        """
        now = time.time()
        if (
            not force
            and self._hero_stats_cache is not None
            and now - self._hero_stats_cache_at < HERO_STATS_CACHE_TTL
        ):
            return self._hero_stats_cache

        data = await self._request("/heroStats", timeout=max(30, self.timeout))
        rows = [row for row in (data or []) if isinstance(row, dict)]
        # 负缓存：失败时也记时间戳，避免每次查询都白跑一趟
        self._hero_stats_cache = rows
        self._hero_stats_cache_at = now
        return rows

    async def get_patches(self, limit: int = 0) -> list[dict]:
        """获取补丁列表，**按时间倒序**（最新在前），形如
        ``[{"name": "7.41", "date": ..., "id": 60}, ...]``。

        只用 ``name``/``id`` 两个字段：``name`` 给用户看，``id`` 拿去当
        ``patch`` 查询参数用（实测两者是同一套编号——``?patch=60`` 返回的
        场次数与 7.41 的起止时间对得上）。

        Args:
            limit: 取最近几个。``0``（默认）表示全部。

        拿不到就返回空列表 —— 上层据此退回「无版本口径」，绝不猜版本号。
        失败也写缓存（空列表），避免接口挂掉时每次查询都重试一遍退避。
        """
        now = time.time()
        rows = self._patches_cache
        if rows is None or now - self._patches_cache_at >= PATCH_CACHE_TTL:
            try:
                data = await self._request(
                    "/constants/patch", timeout=max(30, self.timeout)
                )
            except OpenDotaError as e:
                logger.debug(f"[dota2] 补丁表获取失败，本次不按版本过滤：{e}")
                data = []
            rows = (
                [row for row in data if isinstance(row, dict) and row.get("name")]
                if isinstance(data, list)
                else []
            )
            # 接口按时间升序返回；仍显式排序一次，避免顺序假设哪天失效
            rows.sort(key=lambda row: str(row.get("date") or ""), reverse=True)
            self._patches_cache = rows
            self._patches_cache_at = now
        return list(rows[:limit]) if limit > 0 else list(rows)

    async def get_latest_patch(self) -> dict:
        """获取 OpenDota 记录的最新补丁，形如 ``{"name": "7.41", "date": ..., "id": 60}``。

        只用来给用户**标注版本号**。拿不到就返回空字典 —— 此时报告里只写
        「最近 7 天」而不写版本名，绝不猜一个版本号糊弄过去。
        """
        rows = await self.get_patches(limit=1)
        return rows[0] if rows else {}

    @staticmethod
    def is_parsed(match: dict | None) -> bool:
        """判断一场比赛是否已经具备逐分钟级别的解析数据。

        判据集中在 :func:`dota_format.parsed_state`，两个数据源共用一套口径：

        - OpenDota 解析后会给每名玩家填 ``gold_t``（逐分钟金钱）；
        - STRATZ 不给 ``gold_t``，但给比赛级的 ``radiant_gold_adv`` /
          ``radiant_xp_adv`` 曲线。

        注意: OpenDota 的 ``match["od_data"]`` 是**状态对象**（形如
        ``{"has_api": bool, "has_gcdata": bool, "has_parsed": bool,
        "has_archive": bool}``），它存在并不代表拿到了逐分钟产物，因此
        只在没有曲线时才拿它兜底。
        """
        if not isinstance(match, dict):
            return False
        try:
            from .dota_format import parsed_state
        except ImportError:  # 模块方式加载时的兜底
            from dota_format import parsed_state  # type: ignore[no-redef]

        parsed, _note = parsed_state(match)
        return parsed


# ======================================================================
# 多数据源：主数据源 + 自动降级
# ======================================================================
#: 数据源未配置 / 不可用时的异常。单独成类，便于 :class:`FallbackDataSource`
#: 判断「该切后备了」。
class DataSourceUnavailableError(OpenDotaError):
    """数据源不可用（未配置 / 鉴权失败 / 限流 / 网络异常）。"""


#: 各数据源「不可用」的统一信号：这些异常意味着应该切到后备数据源。
#: 注意 ``TargetNotFoundError`` / ``AmbiguousTargetError`` **不在** 此列 ——
#: 玩家找不到是业务结果，换数据源也没用，应当直接如实报给用户。
_FALLBACK_TRIGGER: tuple[type[Exception], ...] = ()


def _fallback_trigger_types() -> tuple[type[Exception], ...]:
    """惰性构造降级触发异常元组（避免模块级循环导入）。"""
    global _FALLBACK_TRIGGER
    if _FALLBACK_TRIGGER:
        return _FALLBACK_TRIGGER
    types: list[type[Exception]] = [DataSourceUnavailableError]
    try:
        from .dota_stratz import StratzUnavailableError

        types.append(StratzUnavailableError)
    except ImportError:  # pragma: no cover - 模块方式加载时的兜底
        try:
            from dota_stratz import StratzUnavailableError  # type: ignore[no-redef]

            types.append(StratzUnavailableError)
        except ImportError:
            pass
    _FALLBACK_TRIGGER = tuple(types)
    return _FALLBACK_TRIGGER


# ======================================================================
# 双数据源的公共契约
# ======================================================================
class DataSource(Protocol):
    """STRATZ 与 OpenDota 两个客户端**可互换使用**的那部分接口。

    为什么要有这个 Protocol
    -----------------------
    本插件的核心设计是「双数据源 + 自动降级」，整套机制的成立前提是
    :class:`StratzClient` 与 :class:`OpenDotaClient` 在**同一批方法上
    口径一致、可以互换**。在此之前这条契约只靠两样东西维持：文档里的
    一句承诺，和 ``tests/fallback_check.py`` 里一条「STRATZ 实现了全部
    白名单方法」的存在性检查 —— 没有签名、没有返回类型、也没有任何人替
    上层调用点把关。这个 Protocol 把那条口头契约变成静态可验的东西：
    本模块末尾的 :data:`_CONTRACT_OD` / :data:`_CONTRACT_SZ` /
    :data:`_CONTRACT_FB` 会让 mypy 亲自确认两个客户端与组合数据源
    都满足它。

    它描述的边界
    ------------
    只有「**两端都实现、且上层可以不加区分地调用**」的成员才在这里。
    共 19 个方法 ＋ 1 个能力位，而 :data:`_FALLBACK_METHODS` 的降级转发
    白名单是它的**真子集**（16 个：19 减去 ``close`` / ``is_parsed`` /
    ``request_parse``，后三者不需要降级转发）。

    **单端能力一律不放进来**，它们由 :class:`FallbackDataSource` 用
    :meth:`FallbackDataSource._opendota_side` / :meth:`…_stratz_side`
    按类型定位后转发：

    * 只有 OpenDota 有：``get_hero_stats`` / ``get_latest_patch`` /
      ``get_hero_item_popularity`` / ``get_pro_matches`` / ``get_league_matches``
      / ``get_teams_index`` / ``get_team`` / ``get_team_players`` / ``get_team_matches``
    * 只有 STRATZ 有：``configured`` / ``get_hero_playstyle`` /
      ``get_hero_lane_outcome`` / ``get_hero_item_purchase``

    把它们写进来的代价不是「多余」，而是**主动撒谎**：Protocol 的语义
    就是「两端都有」。当年 ``get_hero_item_popularity`` 正是这样被误放进
    ``_FALLBACK_METHODS`` 的，被存在性检查当场抓住。

    签名口径：最窄的公共面
    ----------------------
    ``get_ability_names`` 两端并不一致 —— OpenDota 多一个
    ``force: bool = False``（缓存刷新开关），STRATZ 没有。这里取**没有参数**
    的那一版，因为 Protocol 描述的是「按这种调用方式，任一端都能用」：

    * 实现方多出**带默认值**的参数 → 满足（OpenDota 那版照样能按 ``()`` 调用）；
    * 实现方少参数 → 不满足（这正是要拦住的）。

    没有把 ``force`` 强行补到 STRATZ 上：补一个被忽略的假参数只会制造
    「签名看着一致、行为并不一致」的错觉 —— STRATZ 侧本来就有自己的缓存
    管理，那个开关对它没有意义。

    为什么不加 ``@runtime_checkable``
    ---------------------------------
    本 Protocol 只服务静态检查。运行期的数据源身份判定由 ``SOURCE_KIND``
    ＋ :func:`is_opendota_source` 负责，那套是按**类型**定位、能容忍包装类
    与打桩对象的。``runtime_checkable`` 的 ``isinstance`` 只查属性**名字**
    是否存在、**不查签名**，很容易被后人不小心当成可靠判据用，所以宁可不给
    这个能力。
    """

    # ------------------------------------------------------------------
    # 能力位
    # ------------------------------------------------------------------
    @property
    def SUPPORTS_PATCH_FILTER(self) -> bool:
        """当前数据源能否「按版本过滤场次」。

        两端都必须**显式声明**自己是哪一种（OpenDota ``True`` / STRATZ
        ``False``），不能靠默认值蒙过去：这是上层判断「手上的数据到底是
        当前版本口径还是全生涯口径」的唯一依据，猜错的代价是把两份不同口径
        的数据摆在一起比高下。

        ⚠️ 这里用 ``@property`` 而不是 ``SUPPORTS_PATCH_FILTER: bool``
        （可变属性声明）：两个客户端的写法是类属性、而
        :class:`FallbackDataSource` 是**动态计算**的只读 property
        （见其实现 —— 它必须按「主源此刻是否可用」实时算，写死会报错口径），
        只读属性通不过可变属性声明（mypy：``expected settable variable,
        got read-only attribute``）。声明成 property 则三者都能满足。
        """
        ...

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def close(self) -> None:
        """释放底层连接池。"""
        ...

    # ------------------------------------------------------------------
    # 常量与元数据
    # ------------------------------------------------------------------
    async def get_ability_names(self) -> dict[int, str]:
        """技能 ID → 技能名（口径见各端实现；签名口径见类文档）。"""
        ...

    async def get_benchmarks(self, hero_id: int) -> dict:
        """英雄分档基准数据。"""
        ...

    async def get_heroes(self, force: bool = False) -> dict[int, dict]:
        """英雄常量表：``hero_id → 英雄信息``。"""
        ...

    async def get_items(self, force: bool = False) -> dict[str, dict]:
        """物品常量表：``item_key → 物品信息``。"""
        ...

    async def get_patches(self, limit: int = 0) -> list[dict]:
        """补丁表（新→旧）。``limit<=0`` 表示不限。"""
        ...

    async def hero_name(self, hero_id: int | None) -> str:
        """英雄 ID → 中文 / 官方名；未知 ID 返回占位串而非抛错。"""
        ...

    # ------------------------------------------------------------------
    # 单条比赛
    # ------------------------------------------------------------------
    async def get_match(self, match_id: int, timeout_factor: float = 3.0) -> dict | None:
        """单场比赛详情；``None`` 表示**本数据源没有这条记录**。

        ``None`` 的语义很关键：它同时也是 :data:`_EMPTY_TRIGGERS_FALLBACK`
        的触发条件（两个源的收录范围不一致，主源没有不代表真的没有）。
        """
        ...

    def is_parsed(self, match: dict | None) -> bool:
        """这盘是否已解析（两端判据一致）。

        ⚠️ 两端都把它写成 ``@staticmethod``，而 :class:`FallbackDataSource`
        写成实例方法 —— 两者都满足这里的实例方法声明（mypy 实测），
        调用方一律按 ``api.is_parsed(match)`` 用，不要直接引用类上的函数。
        """
        ...

    async def request_parse(self, match_id: int) -> bool:
        """提交解析申请（**写操作**，不做降级）。"""
        ...

    # ------------------------------------------------------------------
    # 玩家维度
    # ------------------------------------------------------------------
    async def get_player(self, account_id: int) -> dict | None:
        """账号资料；``None`` 表示查不到这位玩家。"""
        ...

    async def search_player(self, query: str, timeout: float = 60.0) -> list[dict]:
        """按昵称 / ID 搜索玩家，返回候选列表。"""
        ...

    async def get_player_heroes(
        self,
        account_id: int,
        *,
        patch: int | None = None,
        game_mode: int | None = None,
        include_insignificant: bool = True,
    ) -> list[dict]:
        """玩家英雄池（可限定版本 / 模式）。"""
        ...

    async def get_player_matches(
        self,
        account_id: int,
        limit: int = 20,
        offset: int = 0,
        win: int | None = None,
        lose: int | None = None,
        hero_id: int | None = None,
        significant: int = 0,
        game_mode: int | None = None,
        lobby_type: int | None = None,
    ) -> list[dict]:
        """玩家比赛列表（可带胜负 / 英雄 / 模式 / 大厅 / 显著场次过滤）。"""
        ...

    async def get_player_matches_enriched(
        self, account_id: int, limit: int = 20, **filters: Any
    ) -> tuple[list[dict], int]:
        """带补充信息的比赛列表，返回 ``(场次, 总数)``。"""
        ...

    async def get_player_peers(self, account_id: int) -> list[dict]:
        """玩家常一起玩的队友 / 对手。"""
        ...

    async def get_player_totals(self, account_id: int, field: str = "kills") -> dict:
        """玩家某个字段的生涯汇总。"""
        ...

    async def get_player_wl(self, account_id: int) -> dict:
        """玩家胜负场次统计。"""
        ...

    async def get_recent_matches(self, account_id: int, limit: int = 20) -> list[dict]:
        """最近场次（两端都有硬性条数上限，见各端实现）。"""
        ...


#: 需要被「主数据源优先、失败切后备」包裹的方法名。
#: 这些方法都是**只读查询**；``request_parse`` 等写操作不做降级（见类文档）。
#:
#: ⚠️ 这张白名单必须是 :class:`DataSource` 的**子集**（16 = 19 − ``close`` −
#: ``is_parsed`` − ``request_parse``：那三个不需要降级转发）。往这里加名字之前
#: 先确认它是「双源共有」的，否则会重演当年 ``get_hero_item_popularity``
#: 误入白名单的错 —— 主源上没有这个方法，转发时只会拿到一句「没这个方法」，
#: 然后把「能力不存在」误报成「没有数据」。
_FALLBACK_METHODS: tuple[str, ...] = (
    "get_heroes",
    "hero_name",
    "get_items",
    "get_ability_names",
    "get_player",
    "search_player",
    "get_player_matches",
    "get_recent_matches",
    "get_player_matches_enriched",
    "get_player_wl",
    "get_player_heroes",
    "get_patches",
    "get_player_totals",
    "get_player_peers",
    "get_match",
    "get_benchmarks",
)


#: 「返回空 = 本数据源没有这条记录」的方法名。
#:
#: 与 :data:`_FALLBACK_TRIGGER` 是**两种不同的降级理由**：
#:
#: * ``_FALLBACK_TRIGGER`` 是**异常**触发 —— 源挂了、鉴权失败、限流；
#: * 这里是**空结果**触发 —— 源好好的，但它就是没有这条记录。
#:
#: 为什么必须有后者（踩过的坑）：两个数据源的**收录范围并不一致**。
#: 实测 8995536921 这盘，STRATZ 的 ``match(id:)`` 返回 ``null``（不是报错，
#: 是它真的没有这盘），而 OpenDota 有完整且已解析的数据。如果只对异常降级，
#: 主源一句「我没有」就会被当成「这盘不存在」直接抛给用户，用户连查三次
#: 都是「找不到比赛」—— 而数据明明就在后备源里。
#:
#: 只收录「单个资源、``None`` 明确表示该源没有这条记录」的方法。**列表类
#: 方法绝对不能加进来**：``get_player_matches`` 返回空列表完全可能是业务事实
#: （这位玩家就是没打过），把它列进来会让每次查询都白跑一趟后备源，既慢
#: 又浪费额度。
_EMPTY_TRIGGERS_FALLBACK: tuple[str, ...] = ("get_match",)


def is_opendota_source(source: Any) -> bool:
    """判断某个数据源是不是 OpenDota（用于「能力只归属某一端」的定位）。

    优先看对方显式声明的 :attr:`SOURCE_KIND`；没有声明时退回 ``isinstance``，
    这样既支持打桩/包装类主动标身份，也不会漏掉没标过的真客户端。
    """
    if source is None:
        return False
    kind = getattr(source, "SOURCE_KIND", "")
    if kind:
        return str(kind).strip().lower() == "opendota"
    return isinstance(source, OpenDotaClient)


class FallbackDataSource:
    """把「主数据源 + 后备数据源」组合成一个对外透明的数据源。

    策略
    ----
    - 所有只读查询**先走主数据源**；主数据源抛出「不可用」类异常时，
      自动切到后备数据源并记录一条 warning。
    - 主数据源**没配置**（例如 STRATZ 未填 Key）时，直接走后备，不做无谓尝试。
    - 主数据源**答得出来但答案是「没有这条记录」**（见
      :data:`_EMPTY_TRIGGERS_FALLBACK`）时，也会补问一次后备 —— 两个源的
      收录范围不一致，主源没有不代表真的没有。只有两边都没有才下结论。
    - 玩家不存在 / 昵称歧义这类**业务结果**不触发降级 —— 换数据源也查不到，
      直接如实返回用户。
    - 一旦主数据源在本进程内失败过，会在 ``_degraded`` 上记一笔；后续请求
      仍会先试主数据源（因为可能只是瞬时抖动），但日志会带上降级标记。

    之所以用「动态代理」而不是逐个方法手写包装：备选数据源的方法有十几个，
    手写包装容易漏方法、也容易在新增接口时忘记同步。这里按白名单在
    ``__init__`` 里统一生成转发函数，新增接口只需往 ``_FALLBACK_METHODS``
    里加一个名字。
    """

    #: 对外暴露的数据源名字，供日志与「当前数据源」提示使用
    name = "fallback"
    label = "STRATZ（后备 OpenDota）"

    # ------------------------------------------------------------------
    # 公共契约声明（**仅在类型检查期存在**，运行期一行都没有）
    # ------------------------------------------------------------------
    if TYPE_CHECKING:
        # 下面这 16 个 ``...`` 只是**声明**：真正的实现是 __init__ 里按
        # _FALLBACK_METHODS 动态生成的转发函数。之所以要手写一遍，是因为
        # ``setattr`` 对静态检查器完全不可见 —— 不写的话 mypy 会认为本类
        # 缺少全部 16 个查询方法，进而不满足 DataSource 契约，也跟着
        # 用不了任何按契约标注形参的地方。
        #
        # 代价是「_FALLBACK_METHODS 白名单」与「这份声明」成了两份真相。
        # 由 tests/batch3_check.py 的断言守着：逐名比对两份名单，漏一个就红。
        # （这也是没有把它们做成自动同步的原因 —— 动态生成的东西一旦出错
        #   是**静默**的，宁可留一份能被断言比对的明文。）
        async def get_ability_names(self) -> dict[int, str]: ...
        async def get_benchmarks(self, hero_id: int) -> dict: ...
        async def get_heroes(self, force: bool = False) -> dict[int, dict]: ...
        async def get_items(self, force: bool = False) -> dict[str, dict]: ...
        async def get_match(
            self, match_id: int, timeout_factor: float = 3.0
        ) -> dict | None: ...
        async def get_patches(self, limit: int = 0) -> list[dict]: ...
        async def get_player(self, account_id: int) -> dict | None: ...
        async def get_player_heroes(
            self,
            account_id: int,
            *,
            patch: int | None = None,
            game_mode: int | None = None,
            include_insignificant: bool = True,
        ) -> list[dict]: ...
        async def get_player_matches(
            self,
            account_id: int,
            limit: int = 20,
            offset: int = 0,
            win: int | None = None,
            lose: int | None = None,
            hero_id: int | None = None,
            significant: int = 0,
            game_mode: int | None = None,
            lobby_type: int | None = None,
        ) -> list[dict]: ...
        async def get_player_matches_enriched(
            self, account_id: int, limit: int = 20, **filters: Any
        ) -> tuple[list[dict], int]: ...
        async def get_player_peers(self, account_id: int) -> list[dict]: ...
        async def get_player_totals(
            self, account_id: int, field: str = "kills"
        ) -> dict: ...
        async def get_player_wl(self, account_id: int) -> dict: ...
        async def get_recent_matches(
            self, account_id: int, limit: int = 20
        ) -> list[dict]: ...
        async def hero_name(self, hero_id: int | None) -> str: ...
        async def search_player(self, query: str, timeout: float = 60.0) -> list[dict]: ...

    @property
    def SUPPORTS_PATCH_FILTER(self) -> bool:
        """当前**实际生效**的那一端能否按版本过滤（见 OpenDotaClient 上的说明）。

        不能简单写死 True：这个能力只归属 OpenDota。一旦已经降级到后备源，
        版本过滤就没了 —— 上层必须看得见这件事，否则会把全生涯数据
        当成当前版本摆给用户。

        ⚠️ 判据必须是**这一次的降级原因**，而不是「历史上降过级没有」
        （P1-4，2026-10-14 审查）：

        ``degrade_reason`` 是个**粘滞**标记 —— 只在下标两处被赋值、全仓库
        没有任何复位点，于是在这个进程里第一次网络抖动之后，它会一直挂着
        ``"unavailable"``。而 ``_make_forwarder`` 每次请求**仍然是主源优先**
        （它自己说了：「可能只是瞬时抖动」），所以主源早就恢复了、后面的
        每一次英雄池请求都走的是 OpenDota，这里却还在按「已经降级」报告
        ``SUPPORTS_PATCH_FILTER = False`` —— 上层于是把一份**完整的当前版本
        数据**标成「未按版本过滤的全生涯口径」，恰好是这段 docstring 最怕的
        那种错误，只是方向反了。

        改法：把这里的判据从粘滞原因换成**主源此刻有没有配置好**。它才是
        「这一次实际会用到哪一端」的真实依据（见 ``_primary_ready``）——
        主源可用 ⇒ 转发一定先走主源 ⇒ 能力就是主源的能力；
        主源不可用（未配置 / 无 Key）⇒ 必然走后备 ⇒ 用后备的能力。
        """
        if self._primary_ready():
            return bool(getattr(self.primary, "SUPPORTS_PATCH_FILTER", False))
        return bool(getattr(self.secondary, "SUPPORTS_PATCH_FILTER", False))

    def __init__(
        self,
        primary: Any,
        secondary: Any,
        *,
        primary_label: str = "主数据源",
        secondary_label: str = "后备数据源",
    ) -> None:
        self.primary = primary
        self.secondary = secondary
        self.primary_label = primary_label
        self.secondary_label = secondary_label
        #: 是否曾经降级过（供状态展示）
        self.degraded = False
        #: 最近一次降级的原因
        self.last_error = ""
        #: 最近一次降级的**类别**，用于抬头措辞：
        #: ``""`` 未降级 / ``"unavailable"`` 主源不可用 / ``"missing"`` 主源无此记录
        self.degrade_reason = ""

        for method_name in _FALLBACK_METHODS:
            setattr(
                self,
                method_name,
                self._make_forwarder(method_name),
            )

    # ------------------------------------------------------------------
    def _make_forwarder(self, method_name: str):
        """为白名单里的方法生成「主优先、失败切后备」的转发函数。"""

        async def forward(*args: Any, **kwargs: Any) -> Any:
            primary_method = getattr(self.primary, method_name, None)
            secondary_method = getattr(self.secondary, method_name, None)

            # 主数据源不可用（未配置）→ 直接走后备
            if primary_method is None or not self._primary_ready():
                return await self._call_secondary(
                    method_name, secondary_method, args, kwargs, reason="主数据源未启用"
                )

            try:
                result = await primary_method(*args, **kwargs)
            except _fallback_trigger_types() as e:
                # 主数据源挂了（未配置/鉴权/限流/网络）→ 切后备
                self.degraded = True
                self.degrade_reason = "unavailable"
                self.last_error = f"{type(e).__name__}: {e}"
                logger.warning(
                    f"[dota2] {self.primary_label}不可用（{e}），"
                    f"本次改走{self.secondary_label}：{method_name}"
                )
                return await self._call_secondary(
                    method_name, secondary_method, args, kwargs, reason=str(e)
                )

            # ---- 主源这一次答上来了：如果之前挂着降级标记，现在复位 ----
            # ⚠️ 必须复位（P1-4，2026-10-14 审查）：
            # ``degrade_reason`` 原本是个**只写不读回**的粘滞标记 —— 全仓库
            # 只有下面两处赋值、没有任何复位点。于是这个进程里第一次网络抖动
            # 之后，它会一直挂着 ``"unavailable"``，而 :meth:`describe` 会
            # 一直报告「STRATZ 暂时不可用」——**哪怕主源早就恢复了、后面每一次
            # 请求都走的它**。用户看到的抬头就成了一条永远不准的状态。
            #
            # 复位时机选在这里（主源成功返回）而不是「每次请求开始时清零」：
            # 后者会让降级标记在「主源又抖了一下」时来不及被看见。
            if self.degraded:
                logger.info(
                    f"[dota2] {self.primary_label}已恢复，清除降级标记"
                    f"（原因曾是 {self.degrade_reason or '未知'}）"
                )
                self.degraded = False
                self.degrade_reason = ""
                self.last_error = ""

            if (
                result is None
                and method_name in _EMPTY_TRIGGERS_FALLBACK
                and secondary_method is not None
            ):
                # 主数据源「没有这条记录」≠「这条记录不存在」。
                # 两个源的收录范围并不一致，实测存在「STRATZ 返回 null、
                # OpenDota 却有完整解析数据」的比赛。这里补问一次后备，
                # 避免把主源的沉默当成全局结论直接甩给用户。
                self.degraded = True
                self.degrade_reason = "missing"
                self.last_error = f"{self.primary_label}无此记录"
                logger.info(
                    f"[dota2] {self.primary_label}没有这条记录，"
                    f"改问{self.secondary_label}再下结论：{method_name}"
                )
                return await self._call_secondary(
                    method_name, secondary_method, args, kwargs, reason="主数据源无此记录"
                )

            return result

        forward.__name__ = method_name
        forward.__qualname__ = f"FallbackDataSource.{method_name}"
        return forward

    def _primary_ready(self) -> bool:
        """主数据源是否处于「可以一试」的状态。"""
        ready = getattr(self.primary, "configured", None)
        if ready is None:
            # 没声明 configured 的数据源默认视为可用
            return True
        return bool(ready)

    async def _call_secondary(
        self,
        method_name: str,
        method: Any,
        args: tuple,
        kwargs: dict,
        *,
        reason: str,
    ) -> Any:
        if method is None:
            raise DataSourceUnavailableError(
                f"{self.primary_label}与{self.secondary_label}都不支持 {method_name}"
            )
        try:
            return await method(*args, **kwargs)
        except OpenDotaError:
            # 后备也失败：如实抛给上层，由各 handler 统一提示
            raise
        except Exception as e:  # noqa: BLE001
            raise DataSourceUnavailableError(
                f"{self.secondary_label}调用 {method_name} 失败：{e}"
            ) from e

    # ------------------------------------------------------------------
    # 不做降级的写操作：直接转发到后备（OpenDota 独占能力）
    # ------------------------------------------------------------------
    async def request_parse(self, match_id: int) -> bool:
        """提交解析申请。

        OpenDota 独占能力（STRATZ 不支持主动提交，只能等它自己排队），
        因此固定路由到 **OpenDota 那一端**，避免在 STRATZ 通道下返回 False
        让等待流程误判。

        .. note::
           早先这里写的是「转发到 ``self.secondary``」，并假设后备源就是
           OpenDota。这个假设**只在 ``data_source_priority=stratz`` 时成立**：
           线上配置是 ``opendota``，此时 secondary 是 STRATZ，于是催解析会被
           转发给一个永远返回 False 的实现 —— 也就是说**从未真正提交过**。
           现在改为按类型定位 OpenDota（见 :meth:`_opendota_side`），
           与数据源优先级配置解耦。
        """
        method = getattr(self._opendota_side(), "request_parse", None)
        if method is None:
            return False
        try:
            return await method(match_id)
        except OpenDotaError:
            return False

    # ------------------------------------------------------------------
    # 只在某一端存在的能力：按类型定位，不走「主源优先」
    # ------------------------------------------------------------------
    def _opendota_side(self) -> Any:
        """找出数据源组合里的 OpenDota 客户端；没有则返回 ``None``。

        按**类型**而不是「primary / secondary」定位：哪个是 OpenDota 取决于
        ``data_source_priority`` 配置，写死某一端会在用户切换优先级后静默失效。
        """
        for source in (self.primary, self.secondary):
            if is_opendota_source(source):
                return source
        return None

    async def get_hero_stats(self, *args: Any, **kwargs: Any) -> list[dict]:
        """英雄版本胜率：**固定走 OpenDota**。

        为什么不做「主源优先」：``/heroStats`` 是 OpenDota 的**公开聚合接口**，
        不需要 Key、不消耗解析额度，返回最近 7 天全分段的样本（实测每个英雄
        最少也有两万场，样本充足）。STRATZ 侧要拿到同等口径得拼一个参数敏感的
        GraphQL 查询、还要花额度，收益不成正比。这与 :meth:`request_parse`
        「能力只归属某一边」的处理方式一致。

        组合里没有 OpenDota 时抛 :class:`DataSourceUnavailableError`，
        由上层如实提示，不静默返回空榜。
        """
        source = self._opendota_side()
        method = getattr(source, "get_hero_stats", None)
        if method is None:
            raise DataSourceUnavailableError("当前数据源组合里没有可用的 OpenDota")
        return await method(*args, **kwargs)

    async def get_latest_patch(self, *args: Any, **kwargs: Any) -> dict:
        """最新补丁号，同 :meth:`get_hero_stats` 固定走 OpenDota。

        拿不到就返回空字典（只影响报告里那行版本标注），不作为错误上报。
        """
        method = getattr(self._opendota_side(), "get_latest_patch", None)
        if method is None:
            return {}
        try:
            return await method(*args, **kwargs)
        except OpenDotaError:
            return {}

    async def get_hero_item_popularity(self, *args: Any, **kwargs: Any) -> dict:
        """某英雄的四阶段出装路线（开局 / 前期 / 中期 / 后期）：**固定走 OpenDota**。

        和 :meth:`get_hero_stats` / :meth:`get_latest_patch` 同一类：这个能力
        **只归属 OpenDota**（``/heroes/{id}/itemPopularity``，STRATZ 没有等价端点），
        所以不能进 :data:`_FALLBACK_METHODS` 那张「主备双源都该实现」的白名单 ——
        当初误放进去，被 ``tests/fallback_check.py`` 的「STRATZ 客户端实现了
        全部白名单方法」当场抓住。

        拿不到返回空 dict（只影响出装路线那一块，上层会如实说明），不抛错、
        也不妨碍高分段的「出门装 / 鞋子」——两者是不同数据源的不同口径。
        """
        method = getattr(self._opendota_side(), "get_hero_item_popularity", None)
        if method is None:
            logger.warning("[dota2] 全分段出装路线：当前数据源组合里没有 OpenDota，这一块将不可用")
            return {}
        try:
            return await method(*args, **kwargs)
        except OpenDotaError as e:
            logger.warning(f"[dota2] 全分段出装路线：OpenDota 查询失败，这一块将不可用：{e}")
            return {}

    async def _from_opendota(
        self, method_name: str, empty: Any, *args: Any, what: str, **kwargs: Any
    ) -> Any:
        """把「只归属 OpenDota」的接口固定转发过去，失败时返回 ``empty`` 并 warning。

        这类接口（职业比赛 / 赛事 / 战队）**没有 STRATZ 等价物**，所以不能
        按「主源优先」走 —— 主源是 STRATZ 时它会拿到一句「没这个方法」，
        然后把「能力不存在」误报成「没有数据」。与 :meth:`get_hero_stats`
        （抛错）的区别在于：那几个是**核心能力**，缺了要如实告知用户；
        这几个是**增强能力**，缺了只少一块，不该把整轮问答打断。
        """
        method = getattr(self._opendota_side(), method_name, None)
        if method is None:
            logger.warning(f"[dota2] {what}：当前数据源组合里没有 OpenDota，这一块将不可用")
            return empty
        try:
            return await method(*args, **kwargs)
        except OpenDotaError as e:
            logger.warning(f"[dota2] {what}：OpenDota 查询失败，这一块将不可用：{e}")
            return empty

    async def get_pro_matches(self, *args: Any, **kwargs: Any) -> list[dict]:
        """近期职业比赛：**固定走 OpenDota**（STRATZ 无等价端点）。"""
        return await self._from_opendota(
            "get_pro_matches", [], what="近期职业比赛", *args, **kwargs
        )

    async def get_league_matches(self, *args: Any, **kwargs: Any) -> list[dict]:
        """某联赛的全部对局：**固定走 OpenDota**（STRATZ 无等价端点）。"""
        return await self._from_opendota(
            "get_league_matches", [], what="联赛对局", *args, **kwargs
        )

    async def get_team(self, *args: Any, **kwargs: Any) -> dict:
        """战队概览：**固定走 OpenDota**。"""
        return await self._from_opendota("get_team", {}, what="战队概览", *args, **kwargs)

    async def get_team_players(self, *args: Any, **kwargs: Any) -> list[dict]:
        """战队阵容：**固定走 OpenDota**。"""
        return await self._from_opendota(
            "get_team_players", [], what="战队阵容", *args, **kwargs
        )

    async def get_team_matches(self, *args: Any, **kwargs: Any) -> list[dict]:
        """战队近期比赛：**固定走 OpenDota**。"""
        return await self._from_opendota(
            "get_team_matches", [], what="战队近期比赛", *args, **kwargs
        )

    async def get_teams_index(self, *args: Any, **kwargs: Any) -> list[dict]:
        """队伍索引（名字 → ID）：**固定走 OpenDota**。"""
        return await self._from_opendota(
            "get_teams_index", [], what="队伍索引", *args, **kwargs
        )

    def _stratz_side(self) -> Any:
        """找出数据源组合里的 STRATZ 客户端；没有则返回 ``None``。

        与 :meth:`_opendota_side` 配对：都按**类型**定位，不看 primary /
        secondary —— 哪个端在哪个位置取决于 ``data_source_priority``。
        """
        for source in (self.primary, self.secondary):
            if str(getattr(source, "SOURCE_KIND", "")).strip().lower() == "stratz":
                return source
        return None

    async def get_hero_playstyle(self, *args: Any, **kwargs: Any) -> dict:
        """英雄高分段打法数据（位置 / 对线 / 出门装 / 鞋子）：**固定走 STRATZ**。

        这几项在 OpenDota 侧没有等价端点（``/heroStats`` 无位置维度，也没有
        出门装与对线统计），所以不做「主源优先」。

        与 :meth:`get_hero_stats` 的失败策略**故意不同**：那边拿不到就抛错
        （整份榜单都靠它，没有就什么都不剩）；这里返回**空 dict** 并记一条
        warning —— 组合里没有 STRATZ / 没配 Key / 临时挂了时，「职业局」与
        「全分段出装」这两块仍然可用，不该被一起拖没。少了哪一块由上层在
        输出里如实交代（见 :func:`dota_hero_playstyle.format_hero_playstyle`
        的 ``notes``）。
        """
        source = self._stratz_side()
        method = getattr(source, "get_hero_playstyle", None)
        if method is None:
            logger.warning(
                "[dota2] 英雄打法：当前数据源组合里没有 STRATZ，"
                "高分段（位置 / 对线 / 出门装）这一块将不可用"
            )
            return {}
        try:
            return await method(*args, **kwargs)
        except OpenDotaError as e:
            logger.warning(f"[dota2] 英雄打法：STRATZ 查询失败，高分段那块将不可用：{e}")
            return {}

    async def get_hero_lane_outcome(self, *args: Any, **kwargs: Any) -> list:
        """英雄某个位置的高分段对线结果：**固定走 STRATZ**。

        与 :meth:`get_hero_playstyle` 同一套路数（OpenDota 无等价端点、
        失败时返回空 list 并记 warning、不拖没其他块）。为什么要单独一个方法
        而不是并进上面那个：它必须带上 ``position`` 参数（``laneOutcome``
        返回行里的 ``position`` 字段不可信，只能靠请求参数限定），而主位置
        要先从 ``stats`` 里算出来 —— 两次查询有先后依赖，没法并成一次请求。
        口径说明见 :func:`dota_hero_playstyle.format_hero_playstyle`。
        """
        source = self._stratz_side()
        method = getattr(source, "get_hero_lane_outcome", None)
        if method is None:
            logger.warning("[dota2] 英雄对线：当前数据源组合里没有 STRATZ，对线这一块将不可用")
            return []
        try:
            rows = await method(*args, **kwargs)
        except OpenDotaError as e:
            logger.warning(f"[dota2] 英雄对线：STRATZ 查询失败，对线那块将不可用：{e}")
            return []
        return rows if isinstance(rows, list) else []

    async def get_hero_item_purchase(self, *args: Any, **kwargs: Any) -> list:
        """英雄某个位置的**高分段完整装备购买**记录：**固定走 STRATZ**。

        与 :meth:`get_hero_lane_outcome` 同一套路数（OpenDota 无等价端点、
        失败时返回空 list 并记 warning、不拖没其他块），也必须带上 ``position``
        参数。**它的计数不能折算场次或百分比** —— 原因写在
        :meth:`dota_stratz.StratzClient.get_hero_item_purchase` 的说明里，
        上层只拿它排先后、算加权胜率与典型成型时间。
        """
        source = self._stratz_side()
        method = getattr(source, "get_hero_item_purchase", None)
        if method is None:
            logger.warning("[dota2] 高分段出装：当前数据源组合里没有 STRATZ，这一块将不可用")
            return []
        try:
            rows = await method(*args, **kwargs)
        except OpenDotaError as e:
            logger.warning(f"[dota2] 高分段出装：STRATZ 查询失败，这一块将不可用：{e}")
            return []
        return rows if isinstance(rows, list) else []

    async def close(self) -> None:
        """关闭两端数据源。"""
        for source in (self.primary, self.secondary):
            closer = getattr(source, "close", None)
            if closer is None:
                continue
            try:
                await closer()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[dota2] 关闭数据源失败: {e}")

    # ------------------------------------------------------------------
    def describe(self) -> str:
        """给出一行「当前数据源状态」描述，供查询结果抬头展示。

        「主源无此记录」与「主源不可用」要分开说：前者主源是好的，只是
        它没有这条数据，抬头若写成「暂时不可用」会让人误以为 STRATZ 挂了。
        """
        if self.degrade_reason == "missing":
            return f"{self.secondary_label}（{self.primary_label}无此记录）"
        if self.degraded:
            return f"{self.secondary_label}（{self.primary_label}暂时不可用）"
        return self.primary_label

    def is_parsed(self, match: dict | None) -> bool:
        """解析状态判定：两端判据一致，直接用主数据源的实现。"""
        checker = getattr(self.primary, "is_parsed", None)
        if checker is None:
            checker = getattr(self.secondary, "is_parsed", None)
        if checker is None:
            checker = OpenDotaClient.is_parsed
        return bool(checker(match))


# ======================================================================
# 公共契约的静态验证（运行期完全不执行）
# ======================================================================
# 这是 :class:`DataSource` Protocol 的**全部价值所在**：下面三行让 mypy 亲自确认
# 「两个客户端 + 组合数据源」都满足那份契约。只要哪天有人在某一端改了方法名、
# 收紧了参数、或者换了返回类型，CI 里的 mypy 一步就会红 —— 而不是等到线上某个
# 查询在降级路径上炸掉才发现。
#
# 在此之前，这条契约只有一份文档，外加 tests/fallback_check.py 里一条
# 「STRATZ 客户端实现了全部白名单方法」的**存在性**检查 —— 名字对上了就算过，
# 签名、参数、返回类型一概不管。
#
# 之所以放在模块末尾：FallbackDataSource 定义在上面几个段落之后，
# 而它的检查必须在它定义完之后才能写。
#
# 运行期整个块都不执行（TYPE_CHECKING 恒为 False）：既不会真的构造出三个
# 客户端、不产生连接池开销，`StratzClient` 的导入也只活在类型世界里，
# 不会和 `dota_stratz` 构成循环导入（那边模块级就 import 了本模块的 OpenDotaError）。
if TYPE_CHECKING:  # pragma: no cover - 仅静态检查期存在
    #: OpenDota 客户端满足公共契约
    _CONTRACT_OD: DataSource = OpenDotaClient(api_key="")
    #: STRATZ 客户端满足公共契约
    _CONTRACT_SZ: DataSource = StratzClient(api_key="")
    #: 组合数据源同样满足 —— 它是对外**唯一**的入口，上层拿到的 ``api``
    #: 绝大多数时候就是它。这里不靠继承，靠的是 Protocol 的**结构化子类型**：
    #: 成员齐、签名对，mypy 就认。那 16 个 ``setattr`` 动态生成的方法由
    #: 类体里的 ``if TYPE_CHECKING:`` 声明补上（见 FallbackDataSource 内部）。
    _CONTRACT_FB: DataSource = FallbackDataSource(
        OpenDotaClient(api_key=""), StratzClient(api_key="")
    )

