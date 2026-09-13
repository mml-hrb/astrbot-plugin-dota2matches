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
from typing import Any

import httpx

from astrbot.api import logger

#: 32 位 account_id 与 64 位 SteamID 的换算基准
STEAM_ID_64_BASE = 76561197960265728

#: OpenDota API 根地址
BASE_URL = "https://api.opendota.com/api"

#: 英雄常量缓存有效期（秒）
HERO_CACHE_TTL = 12 * 3600

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
    """极简令牌桶：保证两次请求之间的最小间隔。"""

    def __init__(self, per_minute: int) -> None:
        self._lock = asyncio.Lock()
        self._min_interval = 60.0 / max(1, per_minute)
        self._last_request_at = 0.0

    def configure(self, per_minute: int) -> None:
        self._min_interval = 60.0 / max(1, per_minute)

    async def acquire(self) -> None:
        async with self._lock:
            wait = self._last_request_at + self._min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()


class OpenDotaClient:
    """OpenDota API 客户端。"""

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
        raise OpenDotaError(f"请求 OpenDota 失败（{detail}）")

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
    ) -> list[dict]:
        """按时间倒序获取玩家最近的比赛列表。

        Args:
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
        self, account_id: int, limit: int = 20
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
        """
        limit = max(1, int(limit))
        # significant=0：包含所有模式（Turbo 等），这才是「真正最近打了什么」
        matches = await self.get_player_matches(account_id, limit=limit, significant=0)

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

    async def get_player_heroes(self, account_id: int) -> list[dict]:
        """获取玩家各英雄的使用统计（返回原始列表，含 games 为 0 的条目）。"""
        data = await self._request(f"/players/{int(account_id)}/heroes")
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

    @staticmethod
    def is_parsed(match: dict | None) -> bool:
        """判断一场比赛是否已经具备逐分钟级别的解析数据。

        注意:OpenDota 返回的 ``match["od_data"]`` 是一个**状态对象**(形如
        ``{"has_api": bool, "has_gcdata": bool, "has_parsed": bool,
        "has_archive": bool}``),存在并不代表解析完成。必须 ``has_parsed`` 为真
        **且** 至少一名玩家的 ``gold_t`` 已被填充,才视为真正解析完毕。
        """
        if not isinstance(match, dict):
            return False
        od_data = match.get("od_data")
        if isinstance(od_data, dict) and od_data.get("has_parsed") is True:
            return True
        players = match.get("players") or []
        for player in players:
            if isinstance(player, dict) and player.get("gold_t"):
                return True
        return False
