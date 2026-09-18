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

#: 英雄版本胜率（``/heroStats``）缓存有效期（秒）。
#:
#: 该接口返回的是**最近 7 天滚动窗口**的聚合统计，分钟级变化没有意义，
#: 但也不能缓存太久——版本更新当天玩家会关心最新数据。3 小时是个折中。
HERO_STATS_CACHE_TTL = 3 * 3600

#: 补丁表（``/constants/patch``）缓存有效期（秒）。补丁几个月才动一次。
PATCH_CACHE_TTL = 12 * 3600

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

    #: 数据源身份标记。有些能力只归属某一端（如 ``request_parse``、
    #: ``get_hero_stats``），组合里要按**类型**定位而不能按 primary/secondary
    #: 定位 —— 否则用户切换 ``data_source_priority`` 后会静默失效。
    #: 用显式标记而不是 ``isinstance``：打桩/包装类同样能声明身份。
    SOURCE_KIND = "opendota"

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
        #: 最新补丁（``/constants/patch`` 的最后一项）
        self._patch_cache: dict | None = None
        self._patch_cache_at = 0.0

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
                try:
                    ability_id = int(row.get("id"))
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

    async def get_latest_patch(self) -> dict:
        """获取 OpenDota 记录的最新补丁，形如 ``{"name": "7.41", "date": ..., "id": 60}``。

        只用来给用户**标注版本号**。拿不到就返回空字典 —— 此时报告里只写
        「最近 7 天」而不写版本名，绝不猜一个版本号糊弄过去。
        """
        now = time.time()
        if self._patch_cache is not None and now - self._patch_cache_at < PATCH_CACHE_TTL:
            return self._patch_cache

        try:
            data = await self._request("/constants/patch", timeout=max(30, self.timeout))
        except OpenDotaError as e:
            logger.debug(f"[dota2] 补丁表获取失败，本次不标注版本号：{e}")
            data = []

        latest: dict = {}
        if isinstance(data, list):
            rows = [row for row in data if isinstance(row, dict) and row.get("name")]
            if rows:
                # 接口按时间升序返回；仍显式排序一次，避免顺序假设哪天失效
                rows.sort(key=lambda row: str(row.get("date") or ""))
                latest = rows[-1]
        # 失败也写缓存（空 dict）：接口真挂时不能每次查询都重试一遍退避
        self._patch_cache = latest
        self._patch_cache_at = now
        return latest

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


#: 需要被「主数据源优先、失败切后备」包裹的方法名。
#: 这些方法都是**只读查询**；``request_parse`` 等写操作不做降级（见类文档）。
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
