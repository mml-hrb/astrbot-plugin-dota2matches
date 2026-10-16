# -*- coding: utf-8 -*-
"""职业赛事信息：队伍、赛区、选手、积分、赛事（进行中 / 已结束）、名次、赛程、转会。

为什么单独一个模块
------------------
「职业比赛相关的信息」在原有两个数据源里**都不完整**，实测结论：

* **STRATZ** 的 ``league`` / ``team`` 类型字段很全（``standings`` 名次+奖金、
  ``tables`` 积分表、``nodeGroups`` 出线结构、``series`` 系列赛比分），但**赛事
  目录只覆盖到 2025 年 9 月** —— 用 OpenDota 返回的 2026 年职业比赛 id 反查，
  ``match(id)`` 拿得到比赛本体，``league`` 却恒为 ``null``（如 DreamLeague
  Season 30 Qualifiers / leagueId=20359）。也就是说**当前赛季的赛事元数据缺失**，
  只有比赛级数据可用。
* **OpenDota** 的 ``/proMatches``（最近职业比赛）、``/leagues``（赛事名单）、
  ``/teams/{id}``（队伍评分与胜负）、``/teams/{id}/players``（阵容，
  含 ``is_current_team_member``）都是当前数据，但**没有赛程、名次、积分、转会**。

所以赛事侧的主力是 **Liquipedia**（社区维护的 Dota2 赛事 wiki，覆盖最全），
OpenDota 负责「当前赛季的真实比赛 / 队伍评分 / 阵容」这类带 ID 的结构化数据。

Liquipedia 的三条硬约束（2026-10-12 实测 + 官方 api-terms-of-use）
--------------------------------------------------------------------
1. **``action=parse`` 限 1 次 / 30 秒**（比普通接口严得多，官方原文：
   "API action=parse requests should not exceed 1 request per 30 seconds"）。
   实测按 2.5 秒一次连打 7 次后即被 429，且封禁窗口持续数分钟。
2. 普通 ``action=query`` 限 1 次 / 2 秒；必须带**自定义 User-Agent（含联系方式）**
   且支持 gzip，否则 406 / 403。
3. **禁止自动化抓取 HTML 页面**，只能走 API；内容为 **CC BY-SA 3.0，必须署名**。

对应设计：**能用 ``action=query&prop=revisions`` 取 wikitext 的绝不走 parse**
（wikitext 车道 1 次/2 秒，parse 车道 1 次/30 秒，两条独立限速），
页面级磁盘缓存（默认 30 分钟），429 一律降级成「这一块现在取不到」而不是把异常抛给模型，
且**不许给用户时间承诺**（避让秒数是插件自己估的止损，不是站点解封时间）。

**转会是唯一必须走 ``action=parse`` 的数据**：``Portal:Transfers`` 的正文只有
一行 ``{{Transfer List|limit=50}}``，真实数据由 Lua 模块在渲染时生成，wikitext
里**根本不存在**（已实测）。所以转会走 parse 车道 + 更长的缓存（默认 60 分钟）。

数据能到什么颗粒度（实测样本：TI 2025 / TI 2026 / EWC 2026 / DreamLeague 30）
------------------------------------------------------------------------------
* **赛事基本信息** —— ``{{Infobox league}}``，字段规整：名称 / 简称 / 系列 /
  主办 / 版本 / 线下线上 / 国家 / 城市 / 场馆 / 起止日期 / 奖池 / 队伍数 / 赛制 /
  级别（``liquipediatier``）。
* **参赛队伍与阵容** —— ``{{TeamParticipants}}`` → ``{{Opponent}}`` →
  ``{{Persons}}`` → ``{{Person|role=N|选手名}}``，五个位置 + 教练齐全，
  还有 ``{{Qualification|method=invite}}`` 给出来源（邀请 / 预选赛）。
* **名次与奖金** —— ``{{TeamPrizePool}}`` → ``{{Slot|place=1|usdprize=…|percentage=42.5}}``。
  注意 ``usdprize`` 常写成表达式（``{{#expr:…}}``），**不可靠地求值**，所以
  本模块**只汇报名次区间与百分比**，金额按 ``percentage × 奖池`` 现算并标注为「约」。
* **积分榜与出线形势** —— ``{{GroupTableLeague}}`` 把**真实队名**直接写在 wikitext 里，
  且每队带一个状态色带（``bg1=up`` / ``stayup`` / ``stay`` / ``down``）——
  这就是「谁在晋级区、谁已经出局」的原始依据，**不需要渲染 HTML**。
  瑞士轮另有 ``{{SwissStandings|bg=1-3=up,…}}`` 只给区间含义，
  具体名次由 Lua 从对局算出，wikitext 里没有 ⇒ 该子页面**只能走 parse**，
  且拿到的是渲染后的表格。两者都支持，谁有数据用谁。
* **赛程** —— ``{{Match|opponent1=…|opponent2=…|date=…|map1={{Map|…}}}}``
  多在赛事**子页面**（``/Group Stage``、``/Main Event``）；未开赛的赛事
  在主页就是一张**空骨架**（``{{Match||opponent1={{TeamOpponent|}}}}``），
  此时如实说「尚未排出对阵」，不把空模板渲染成对阵。

缺失一律渲染 ``-``，不猜、不补零（本项目通用纪律）。
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Iterator

import httpx

try:  # 插件目录被作为包加载时
    from astrbot.api import logger
except ImportError:  # 离线单测 / 从工作区脚本直接 import 时
    # ⚠️ 刻意**不用**内置 `logging` 模块。
    # 插件市场自动检查有一条硬性规定：logger 必须且只能从 `astrbot.api` 导入，
    # 严禁 `import logging` —— 2026-10-15 `dota_pool.py` 已因同样写法被拒过一次
    # （见 CHANGELOG）。所以这里自带一个最小实现顶上，
    # 保住「没有 astrbot 也能 import 本模块」的能力。
    import sys

    class _FallbackLogger:
        """离线兜底 logger：只把 WARNING 及以上打到 stderr。

        门槛与内置 logging **未配置时一致**（根 logger 默认 WARNING，debug 与
        info 本就不会输出），所以这次替换不改变原有的日志可见性。
        """

        def _emit(self, label: str, level: int, msg: object) -> None:
            if level >= 30:  # WARNING
                print(f"[dota2.esports] {label} {msg}", file=sys.stderr)

        def debug(self, msg: object, *args: object, **kwargs: object) -> None:
            self._emit("DEBUG", 10, msg)

        def info(self, msg: object, *args: object, **kwargs: object) -> None:
            self._emit("INFO", 20, msg)

        def warning(self, msg: object, *args: object, **kwargs: object) -> None:
            self._emit("WARNING", 30, msg)

        def error(self, msg: object, *args: object, **kwargs: object) -> None:
            self._emit("ERROR", 40, msg)

        def exception(self, msg: object, *args: object, **kwargs: object) -> None:
            self._emit("ERROR", 40, msg)

    logger = _FallbackLogger()


# ======================================================================
# 常量
# ======================================================================

LIQUIPEDIA_ENDPOINT = "https://liquipedia.net/dota2/api.php"

#: Liquipedia 要求自定义 UA 且**必须带联系方式**，否则 406/403。
#: 这里的邮箱是插件作者的联系方式（见 metadata.yaml 的 author）。
DEFAULT_CONTACT = "https://github.com/mml-hrb/astrbot-plugin-dota2matches (3289469099@qq.com)"
DEFAULT_UA = f"AstrBot-Dota2-Plugin/1.0 ({DEFAULT_CONTACT})"

#: 两条独立限速车道（官方口径）。留一点余量，避免踩线被临时封。
QUERY_MIN_INTERVAL = 2.2      # action=query      → 官方 1 次 / 2 秒
PARSE_MIN_INTERVAL = 31.0     # action=parse      → 官方 1 次 / 30 秒

DEFAULT_CACHE_TTL = 1800.0    # 赛事页缓存 30 分钟
TRANSFER_CACHE_TTL = 3600.0   # 转会页缓存 60 分钟

#: 撞到 429 之后的强制避让窗口（秒）。官方只说「超限会被临时封禁」，
#: 实测按 2.5 秒一次连打 7 次后，封禁持续半小时以上 —— 这里取 10 分钟
#: 保守避让，并把解封时刻落盘（热重载也不忘）。
RATE_LIMIT_COOLDOWN = 600.0
#: 连续撞到 429 时的避让上限。
#:
#: 为什么要有「递增」这件事：实测固定 600 秒**不够** —— 避让期满回来再试，
#: 又一次拿到 429（站点自己的封禁窗口比我们记的长，官方口径是可长达数十分钟）。
#: 每撞一次就把避让时间翻倍，才有机会等到它真的解封。
RATE_LIMIT_COOLDOWN_MAX = 3600.0
#: 连续计数的有效窗口：超过这么久没再撞，就当作「上一段糟糕时期已经过去」重新算。
RATE_LIMIT_STRIKES_TTL = 86400.0

#: 出处署名。Liquipedia 内容是 CC BY-SA 3.0，条款要求注明来源。
SOURCE_NOTE = "数据来源：Liquipedia（CC BY-SA 3.0）"

#: 「赛程与结果」表最多渲染多少行。超出的只截断**展示**，但一定会写明总场次 ——
#: 不写总场次，用户会把「表里 24 行」当成「这个赛事只有 24 场」。
MAX_SCHEDULE_ROWS = 24

#: 队伍状态色带（``bgN`` / ``pbgN``）→ 中文。语义按「该位置落在哪个区间」解释，
#: 具体晋级规则由赛事主办方定义，渲染时会加一句说明，不过度解读。
ZONE_LABELS: dict[str, str] = {
    "up": "晋级区",
    "stayup": "晋级边缘",
    "stay": "待定",
    "staydown": "保级边缘",
    "down": "淘汰区",
    "eliminated": "已淘汰",
    "qualified": "已晋级",
    "winner": "冠军",
    "ru": "亚军",
}


def _zone_label(key: str) -> str:
    return ZONE_LABELS.get((key or "").strip().lower(), (key or "").strip() or "-")


def _parse_retry_after(value: str | None) -> float | None:
    """解析响应头 ``Retry-After``（秒数或 HTTP 日期）。拿不准返回 ``None``。

    站点自己给了「还要等多久」就听它的，比我们猜的准。有则取，没有也不影响。
    """
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        moment = parsedate_to_datetime(raw)
        if moment is None or moment.tzinfo is None:
            return None
        return max(0.0, moment.timestamp() - time.time())
    except Exception:  # noqa: BLE001 —— 解析不了就当它没给
        return None


# ======================================================================
# 异常
# ======================================================================


class EsportsError(Exception):
    """赛事数据源的通用异常基类。"""


class EsportsRateLimited(EsportsError):
    """被限流（429）。

    要点是**别让模型以为「数据不存在」**；但也**别写「一会儿就好」** ——
    我们这边只是停止请求（止损），站点那边是须人工过校验的闸门，等多久都不作数。
    """


class EsportsUnavailable(EsportsError):
    """站点不可达 / 结构解析失败等。"""


# ======================================================================
# 磁盘缓存
# ======================================================================


class PageCache:
    """页面级磁盘缓存。

    为什么必须落盘而不是只在内存：Liquipedia 的 parse 车道是 1 次/30 秒，
    插件重启（热重载很频繁）会把内存缓存清空，同一个赛事页又要重新走一遍限速。
    """

    def __init__(self, root: Path | None) -> None:
        self.root = Path(root) if root else None
        self._mem: dict[str, tuple[float, Any]] = {}

    @staticmethod
    def _key(kind: str, page: str) -> str:
        digest = hashlib.sha1(page.encode("utf-8")).hexdigest()[:16]
        return f"{kind}_{digest}"

    def get(self, kind: str, page: str, ttl: float) -> Any | None:
        now = time.time()
        key = self._key(kind, page)
        hit = self._mem.get(key)
        if hit and now - hit[0] <= ttl:
            return hit[1]
        if self.root is not None:
            path = self.root / f"{key}.json"
            try:
                if path.is_file() and now - path.stat().st_mtime <= ttl:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    self._mem[key] = (path.stat().st_mtime, data)
                    return data
            except Exception:  # noqa: BLE001 —— 缓存坏掉就当没命中
                return None
        return None

    def set(self, kind: str, page: str, value: Any) -> None:
        key = self._key(kind, page)
        self._mem[key] = (time.time(), value)
        if self.root is None:
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / f"{key}.json").write_text(
                json.dumps(value, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:  # noqa: BLE001 —— 写缓存失败不影响主流程
            logger.debug(f"[dota2] 写赛事缓存失败: {e}")

    # -- 限流窗口（必须落盘） ----------------------------------------
    #
    # 被 429 之后若只把「解封时刻」记在内存里，热重载一次就忘了，下一轮
    # 提问又去撞一次 —— 而每次撞都会把封禁窗口续上（实测踩过：连打几次
    # 之后封了半小时以上）。写进磁盘，重启后仍然知道「现在不能碰它」。
    def get_limit_state(self) -> dict[str, float]:
        """读磁盘上的限流状态：``until`` 解封时刻、``strikes`` 连续次数、``at`` 记下的时刻。"""
        blank = {"until": 0.0, "strikes": 0.0, "at": 0.0}
        if self.root is None:
            return blank
        path = self.root / "_ratelimit.json"
        try:
            if not path.is_file():
                return blank
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return blank
        state = dict(blank)
        for key in state:
            try:
                state[key] = float(data.get(key) or 0.0)
            except (TypeError, ValueError):
                state[key] = 0.0
        return state

    def get_block_until(self) -> float:
        """上次被限流后写下的解封时刻（epoch 秒）；没有或已过期返回 0。"""
        until = self.get_limit_state()["until"]
        return until if until > time.time() else 0.0

    def get_strikes(self) -> int:
        """**连续**撞到 429 的次数（避让时长按它翻倍）。

        隔了 ``RATE_LIMIT_STRIKES_TTL`` 那么久就重新算 —— 否则偶尔查一次也会
        被一整天前的记录顶到最长避让。
        """
        state = self.get_limit_state()
        if state["at"] and time.time() - state["at"] > RATE_LIMIT_STRIKES_TTL:
            return 0
        return max(0, int(state["strikes"]))

    def set_block_until(self, until: float, *, strikes: int | None = None) -> None:
        """写下解封时刻。``strikes`` 不给就沿用磁盘上的旧值。"""
        if self.root is None:
            return
        if strikes is None:
            strikes = self.get_strikes()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / "_ratelimit.json").write_text(
                json.dumps(
                    {"until": float(until), "strikes": int(strikes), "at": time.time()}
                ),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[dota2] 写限流标记失败: {e}")


class _Lane:
    """单条限速车道：保证同类请求之间的最小间隔。"""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            gap = self._last + self.min_interval - now
            if gap > 0:
                await asyncio.sleep(gap)
            self._last = time.monotonic()


# ======================================================================
# Liquipedia 客户端
# ======================================================================


class LiquipediaClient:
    """Liquipedia MediaWiki API 客户端（只读）。

    * ``wikitext(page)`` 走 ``action=query&prop=revisions``（1 次 / 2 秒）
    * ``rendered(page)`` 走 ``action=parse``（1 次 / 30 秒，代价高）
    * 两者都带磁盘缓存；429 抛 :class:`EsportsRateLimited`，由上层降级成
      「这一块现在取不到」而不是「查不到」。
    * 文案只说**我们这边的动作**（「插件已停发请求 X 分钟」），
      不写「约 X 分钟后恢复」—— 那个时长是本地止损，不是站点解封时间。
    """

    def __init__(
        self,
        *,
        contact: str = "",
        api_key: str = "",
        timeout: int = 30,
        proxy: str = "",
        cache_dir: Path | None = None,
        cache_ttl: float = DEFAULT_CACHE_TTL,
        max_retries: int = 2,
    ) -> None:
        contact = (contact or "").strip() or DEFAULT_CONTACT
        self.user_agent = (
            contact if contact.lower().startswith("astrbot") else f"AstrBot-Dota2-Plugin/1.0 ({contact})"
        )
        self.api_key = (api_key or "").strip()
        self.timeout = max(5, int(timeout))
        self.proxy = (proxy or "").strip()
        self.cache_ttl = max(60.0, float(cache_ttl))
        self.max_retries = max(0, int(max_retries))
        self.cache = PageCache(cache_dir)
        self._query_lane = _Lane(QUERY_MIN_INTERVAL)
        self._parse_lane = _Lane(PARSE_MIN_INTERVAL)
        self._client: httpx.AsyncClient | None = None
        #: 站点被临时封禁时，记下时间避免继续撞（封禁窗口实测持续数分钟到数十分钟）。
        #: 优先沿用磁盘上的解封时刻 —— 热重载之后仍然知道「现在别碰」。
        #: ``_strikes`` 是「连续撞了几次」，避让时长按它翻倍；解封时刻过了但
        #: 连续计数还在（同一段糟糕时期），所以**不能**因为解封就把它清零 ——
        #: 清了就会一直是「避让 600 秒 → 回来又撞 → 再避让 600 秒」的死循环。
        self._blocked_until = 0.0
        persisted = self.cache.get_block_until()
        if persisted > time.time():
            self._blocked_until = time.monotonic() + (persisted - time.time())
        self._strikes = self.cache.get_strikes()
        #: 保护「避让检查 → strikes 自增 → 置避让」这段读改写（P1-9）。
        #: 见 :meth:`_call` 的 docstring：不加锁时两个并发调用能把指数避让
        #: 的底数多算一次，一次 429 直接跳到上限 3600 秒。
        self._state_lock = asyncio.Lock()

    # -- 底层 --------------------------------------------------------
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            kwargs: dict[str, Any] = {
                "timeout": httpx.Timeout(self.timeout, connect=min(15, self.timeout)),
                "follow_redirects": True,
                # Liquipedia 要求 gzip；httpx 默认就会带 Accept-Encoding，
            }
            headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
            if self.proxy:
                kwargs["proxy"] = self.proxy
                kwargs["headers"] = headers
            else:
                kwargs["headers"] = headers
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            try:
                await self._client.aclose()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[dota2] 关闭 Liquipedia 客户端失败: {e}")
        self._client = None

    @property
    def blocked_seconds(self) -> float:
        """距离限流窗口结束还有多少秒（0 表示当前可用）。"""
        return max(0.0, self._blocked_until - time.monotonic())

    def _cooldown_for(self, strikes: int, retry_after: float | None) -> float:
        """撞了**第 ``strikes`` 次** 429 时该避让多久。

        固定 600 秒实测不够（避让期满回来再试还是一次 429），所以按次翻倍：
        600 → 1200 → 2400 → 3600（封顶）。站点给了 ``Retry-After`` 就听它的，
        但同样不超过封顶值。
        """
        step = RATE_LIMIT_COOLDOWN * (2 ** max(0, int(strikes) - 1))
        wait = min(step, RATE_LIMIT_COOLDOWN_MAX)
        if retry_after and retry_after > 0:
            wait = max(wait, min(float(retry_after), RATE_LIMIT_COOLDOWN_MAX))
        return wait

    @property
    def strikes(self) -> int:
        """连续撞 429 的次数（请求成功后清零）。"""
        return self._strikes

    async def _call(self, params: dict[str, Any], *, lane: _Lane, retry_after_lane: bool = True) -> dict:
        """发一次 API 请求，返回 JSON 的 ``parse`` / ``query`` 部分。

        ⚠️ 「检查避让 → 递增 strikes → 置避让时刻」必须**在同一把锁里**
        完成（P1-9，2026-10-14 审查）。:class:`_Lane` 只串行化**发请求**那一
        瞬间，管不到这里的状态检查与自增；而两个并发工具调用（模型一轮里
        同时调 ``query_tournament`` + ``query_transfers``）完全可以双双通过
        ``blocked_seconds > 0`` 的检查、各自 ``+= 1``。

        ``strikes`` 是**指数避让**的底数（``600 * 2**(strikes-1)``，封顶
        3600 秒），被多算一次就从 10 分钟跳到 20~60 分钟；而避让期内对**任何
        页面**一律拒绝，且落盘后热重载不忘 —— 一次并发就能让插件自锁一小时。

        锁内要**二次检查**：等锁的过程中另一次调用可能已经踩了 429 并把避让
        置上了，这时本次必须跟着停发，而不是拿着过期的判断继续往外发。
        """
        async with self._state_lock:
            if self.blocked_seconds > 0:
                raise EsportsRateLimited(
                    f"Liquipedia 限流中（插件已停发请求，本地避让还剩 "
                    f"{int(self.blocked_seconds)} 秒）"
                )
            # 真正发请求的整个重试循环都在锁内：否则「第 1 次尝试成功 →
            # 释放锁 → 另一个调用撞了 429 置上避让 → 本次第 2 次尝试又发出去」
            # 这条路径仍然能绕过避让。代价是同一条车道的请求会串起来 ——
            # 而 _Lane 本来就在做这件事，所以不额外损失并发度。
            return await self._call_locked(params, lane=lane)

    async def _call_locked(self, params: dict[str, Any], *, lane: _Lane) -> dict:
        """``_call`` 的实体（**必须**在 :attr:`_state_lock` 保护下调用）。"""
        request = dict(params)
        request.setdefault("format", "json")
        request.setdefault("formatversion", 2)
        if self.api_key:
            request.setdefault("apikey", self.api_key)

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            await lane.wait()
            try:
                resp = await self._get_client().get(LIQUIPEDIA_ENDPOINT, params=request)
            except httpx.HTTPError as e:
                last_error = EsportsUnavailable(f"连接 Liquipedia 失败：{e}")
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(min(6.0, 1.5 * (attempt + 1)))
                continue

            status = resp.status_code
            if status == 200:
                try:
                    data = resp.json()
                except ValueError as e:
                    last_error = EsportsUnavailable(f"Liquipedia 返回体不是合法 JSON：{e}")
                    if attempt >= self.max_retries:
                        break
                    await asyncio.sleep(1.5)
                    continue
                if isinstance(data, dict) and data.get("error"):
                    err = data["error"]
                    info = err.get("info") if isinstance(err, dict) else str(err)
                    raise EsportsUnavailable(f"Liquipedia 返回错误：{info}")
                if self._strikes:
                    # 真的请求通了 ⇒ 站点已经不封了，连续计数清零（并同步到磁盘，
                    # 否则热重载之后还会以为自己在「连续撞」里）。
                    self._strikes = 0
                    self.cache.set_block_until(0.0, strikes=0)
                return data

            if status == 429:
                # 官方条款：触发后会临时封禁（可完成 CAPTCHA 自助解封）。
                # 解封时刻**落盘**，否则热重载就把这层保护忘了，下一轮又去撞。
                # 避让时长按「连续撞了几次」翻倍 —— 固定值实测等不到解封。
                #
                # 文案纪律（2026-10-13 改）：只说「插件停发请求 X 分钟」这个**事实**，
                # **不许**写成「约 X 分钟后恢复」—— 那个时长是我们自己估的止损时间，
                # 不是站点给的解封时间（站点那侧要人工过校验，等待本身不解封）。
                #
                # ⚠️ 这一段是「读改写」，全靠 _call 外面那把 _state_lock 保护
                # （P1-9）：两个并发调用各自 += 1 会让避让时长一次翻到上限。
                self._strikes += 1
                wait = self._cooldown_for(
                    self._strikes, _parse_retry_after(resp.headers.get("retry-after"))
                )
                self._blocked_until = time.monotonic() + wait
                self.cache.set_block_until(time.time() + wait, strikes=self._strikes)
                escal = "；连续超限，避让时间已自动加长" if self._strikes > 1 else ""
                raise EsportsRateLimited(
                    f"Liquipedia 返回 429（请求频率超限），插件已停发请求 "
                    f"{int(wait // 60)} 分钟{escal}"
                )
            if status in (403, 406):
                # 406 的成因已实测钉死：**只**与缺 gzip 有关（不带 UA 也照样 200）；
                # UA 是否带联系方式是**条款合规**问题，不是 406 的开关。
                hint = (
                    "406 一般是客户端没有启用 gzip"
                    if status == 406
                    else "403 一般是站点的风控 / 自动化拦截"
                )
                raise EsportsUnavailable(
                    f"Liquipedia 拒绝了请求（HTTP {status}）：{hint}。"
                )
            last_error = EsportsUnavailable(f"Liquipedia 接口返回 HTTP {status}")
            if attempt >= self.max_retries:
                break
            await asyncio.sleep(min(6.0, 1.5 * (attempt + 1)))
        raise last_error or EsportsUnavailable("Liquipedia 请求失败")

    # -- 对外能力 ----------------------------------------------------
    async def search(self, query: str, *, limit: int = 8) -> list[str]:
        """在 Dota2 wiki 里搜页面标题。"""
        query = (query or "").strip()
        if not query:
            return []
        cache_key = f"search::{query}::{limit}"
        cached = self.cache.get("q", cache_key, self.cache_ttl)
        if cached is not None:
            return list(cached)
        data = await self._call(
            {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": max(1, min(20, int(limit))),
            },
            lane=self._query_lane,
        )
        hits = [
            str(h.get("title") or "")
            for h in (data.get("query", {}).get("search") or [])
            if h.get("title")
        ]
        self.cache.set("q", cache_key, hits)
        return hits

    async def wikitext(self, page: str) -> str:
        """取页面 wikitext（走 query 车道，代价低）。"""
        page = (page or "").strip().replace("_", " ")
        if not page:
            return ""
        cached = self.cache.get("wt", page, self.cache_ttl)
        if cached is not None:
            return str(cached)
        data = await self._call(
            {
                "action": "query",
                "prop": "revisions",
                "titles": page,
                "rvprop": "content",
                "rvslots": "main",
                "redirects": 1,
            },
            lane=self._query_lane,
        )
        pages = data.get("query", {}).get("pages") or []
        if not pages:
            return ""
        item = pages[0]
        if item.get("missing"):
            return ""
        revs = item.get("revisions") or []
        if not revs:
            return ""
        content = (revs[0].get("slots") or {}).get("main", {}).get("content", "")
        text = str(content or "")
        if text:
            self.cache.set("wt", page, text)
        return text

    async def subpages(self, page: str, *, limit: int = 100) -> list[str]:
        """列出 ``page/…`` 的子页面标题（``list=allpages&apprefix``，走 query 车道）。

        赛事页把**赛程与积分榜**放在子页面里（``/Group Stage``、``/Main Event``），
        主页只有一张导航。要拿到它们就得先知道名字 —— 用前缀列举一次即可，
        比逐个猜名字省请求。
        """
        page = (page or "").strip().replace("_", " ")
        if not page:
            return []
        cache_key = f"sub::{page}::{int(limit)}"
        cached = self.cache.get("q", cache_key, self.cache_ttl)
        if cached is not None:
            return list(cached)
        data = await self._call(
            {
                "action": "query",
                "list": "allpages",
                "apprefix": f"{page}/",
                "apnamespace": 0,
                "aplimit": max(1, min(200, int(limit))),
            },
            lane=self._query_lane,
        )
        titles = [
            str(item.get("title") or "")
            for item in (data.get("query", {}).get("allpages") or [])
            if item.get("title")
        ]
        self.cache.set("q", cache_key, titles)
        return titles

    async def rendered(self, page: str, *, ttl: float = TRANSFER_CACHE_TTL) -> str:
        """取渲染后的 HTML（走 parse 车道，1 次 / 30 秒，只在必要时用）。"""
        page = (page or "").strip().replace("_", " ")
        if not page:
            return ""
        cached = self.cache.get("html", page, ttl)
        if cached is not None:
            return str(cached)
        data = await self._call(
            {"action": "parse", "page": page, "prop": "text"},
            lane=self._parse_lane,
        )
        html = str((data.get("parse") or {}).get("text") or "")
        if html:
            self.cache.set("html", page, html)
        return html


# ======================================================================
# wikitext 解析工具
# ======================================================================

_TAG_RE = re.compile(r"<(?:ref|/ref|br|small|big|center|div|span|gallery|nowiki)\b[^>]*/?>", re.I)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_REF_RE = re.compile(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", re.I | re.S)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def find_template(text: str, name: str, start: int = 0) -> tuple[str, int, int] | None:
    """找第一个 ``{{name …}}``，返回 ``(模板体, 起始, 结束)``。

    用花括号配平扫描，避免被嵌套模板 / 表格语法骗到。
    """
    pattern = re.compile(r"\{\{\s*" + re.escape(name) + r"\s*(\||\})", re.I)
    m = pattern.search(text, start)
    if not m:
        return None
    i = m.start()
    depth = 0
    j = i
    while j < len(text):
        if text.startswith("{{", j):
            depth += 1
            j += 2
            continue
        if text.startswith("}}", j):
            depth -= 1
            j += 2
            if depth == 0:
                body = text[i + 2 : j - 2]
                return body, i, j
            continue
        j += 1
    return None


def iter_templates(text: str, name: str) -> Iterator[str]:
    """逐个产出 ``{{name …}}`` 的模板体（同级，不递归进嵌套的同类模板）。"""
    for body, _start, _end in iter_template_spans(text, name):
        yield body


def iter_template_spans(text: str, name: str) -> Iterator[tuple[str, int, int]]:
    """同 :func:`iter_templates`，但连**位置**一起给。

    位置有用：积分表的 ``title`` 常常是千篇一律的 ``Standings``（EWC 四张表
    全叫这个），真正的分组名写在**它上方最近的标题**里（``===Group A===``）。
    要知道这一点就得拿到模板在原文里的偏移，回头看上一段。
    """
    pos = 0
    while True:
        found = find_template(text, name, pos)
        if not found:
            return
        body, start, end = found
        yield body, start, end
        pos = end


#: 积分表模板自带的 ``title`` 常见取值 → 这些没有信息量（分组名在标题里）。
_GENERIC_TABLE_TITLES = {
    "standings", "standing", "table", "group", "groups", "points",
    "points table", "standings table", "results", "scoreboard",
}


def nearest_heading(text: str, before: int) -> str:
    """找 ``text[:before]`` 里**最后一个** wiki 标题（``== X ==``）的文字。"""
    hit = ""
    for m in re.finditer(r"^(=+)\s*([^=\n]+?)\s*\1\s*$", text[:before], re.M):
        label = clean(m.group(2))
        if label:
            hit = label
    return hit


def split_params(body: str) -> list[str]:
    """按顶层 ``|`` 切分模板参数（忽略 ``{{}}``、``[[]]`` 内部的竖线）。"""
    parts: list[str] = []
    depth_c = depth_s = 0
    buf: list[str] = []
    i = 0
    while i < len(body):
        if body.startswith("{{", i):
            depth_c += 1
            buf.append("{{")
            i += 2
            continue
        if body.startswith("}}", i):
            depth_c -= 1
            buf.append("}}")
            i += 2
            continue
        if body.startswith("[[", i):
            depth_s += 1
            buf.append("[[")
            i += 2
            continue
        if body.startswith("]]", i):
            depth_s -= 1
            buf.append("]]")
            i += 2
            continue
        ch = body[i]
        if ch == "|" and depth_c <= 0 and depth_s <= 0:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


_KV_RE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.*)$", re.S)


def template_fields(body: str) -> tuple[dict[str, str], list[str]]:
    """把模板体拆成 ``(具名参数字典, 位置参数列表)``。"""
    named: dict[str, str] = {}
    positional: list[str] = []
    for part in split_params(body)[1:]:  # 第 0 段是模板名之前的部分
        m = _KV_RE.match(part)
        if m:
            named[m.group(1).strip().lower()] = m.group(2).strip()
        else:
            value = part.strip()
            if value:
                positional.append(value)
    return named, positional


def clean(text: str) -> str:
    """把 wikitext 片段清成可读纯文本。

    * 去掉 ``<ref>`` / 注释 / HTML 标签
    * ``[[A|B]]`` → ``B``、``[[A]]`` → ``A``
    * 去掉 ``{{Flag|cn}}`` 这类装饰性模板，保留其文字参数
    """
    if not text:
        return ""
    out = _COMMENT_RE.sub(" ", text)
    out = _REF_RE.sub(" ", out)
    # <br> 是赛制/地点这类多值字段的分隔符，直接删掉会把两段粘成一个词
    out = re.sub(r"<br\s*/?>", " / ", out, flags=re.I)
    # [[a|b]] → b ；[[a]] → a（可能带 / 章节锚点）
    out = re.sub(r"\[\[[^\[\]|]*\|([^\[\]]*)\]\]", r"\1", out)
    out = re.sub(r"\[\[([^\[\]]*)\]\]", r"\1", out)
    # 装饰性模板：Flag / Abbr / Tooltip 等只保留最后一段文字
    def _decor(m: re.Match[str]) -> str:
        inner = m.group(1)
        parts = [p.strip() for p in split_params(inner)[1:] if p.strip()]
        return parts[-1] if parts else ""

    for _ in range(3):  # 嵌套几层
        out = re.sub(r"\{\{\s*(?:Flag|Abbr(?:/[A-Za-z]+)?|Tooltip|Nowrap)\s*\|([^{}]*)\}\}", _decor, out, flags=re.I)
    out = re.sub(r"\{\{[^{}]*\}\}", " ", out)  # 其余模板整体丢弃
    out = _TAG_RE.sub(" ", out)
    out = _HTML_TAG_RE.sub(" ", out)
    out = out.replace("'''", "").replace("''", "")
    out = out.replace("&nbsp;", " ").replace("&#160;", " ")
    out = re.sub(r"<[^>]*$", "", out)
    return re.sub(r"\s+", " ", out).strip()


def _first_positional(body: str) -> str:
    """取模板的第一个位置参数（``{{Opponent|TeamName …}}`` 里的队名）。"""
    parts = split_params(body)
    if len(parts) < 2:
        return ""
    # 位置参数可能和后面具名参数写在同一段（队名换行后才是 |players=…）
    return clean(parts[1].split("\n")[0])


# ======================================================================
# 页面级解析
# ======================================================================

#: Infobox 里要抽的字段（值统一清洗成纯文本）。
INFOBOX_FIELDS = (
    "name", "shortname", "tickername", "series", "organizer", "organizer2",
    "organizer3", "patch", "type", "country", "city", "venue1", "region",
    "sdate", "edate", "prizepoolusd", "team_number", "format", "liquipediatier",
    "pctier", "publishertier", "website", "leagueid",
)


def parse_infobox(wikitext: str) -> dict[str, str]:
    """解析 ``{{Infobox league}}``。"""
    found = find_template(wikitext, "Infobox league")
    if not found:
        return {}
    named, _ = template_fields(found[0])
    out: dict[str, str] = {}
    for key in INFOBOX_FIELDS:
        raw = named.get(key)
        if raw:
            value = clean(raw)
            if value:
                out[key] = value
    # 奖池常写成模板转写（``{{:The International/2025/prizepool}}``），清洗后是空串。
    # 原始值留给 :func:`resolve_prize_pool` 再去取一次那个子页面。
    raw_pool = named.get("prizepoolusd") or ""
    if raw_pool:
        out["_prizepool_raw"] = raw_pool
    return out


_AMOUNT_RE = re.compile(r"\$\s*([0-9][0-9,\s]*(?:\.[0-9]+)?)")


def _format_amount(number: float) -> str:
    return f"${int(round(number)):,}"


async def resolve_prize_pool(client: "LiquipediaClient", info: dict[str, str]) -> str:
    """把奖池解析成可读金额。

    ``|prizepoolusd=1600000`` 这种直接给数字的直接用；写成
    ``{{:The International/2025/prizepool}}``（把金额放在单独一个子页面里）
    的，就再取一次那个子页面 —— 它是纯数字页，走的是便宜的 query 车道。
    两次都拿不到就返回空串（调用方渲染 ``-``），**绝不按百分比倒推金额**。
    """
    cleaned = (info.get("prizepoolusd") or "").strip()
    if cleaned:
        m = _AMOUNT_RE.search(cleaned) or re.search(r"([0-9][0-9,]*)", cleaned)
        if m:
            try:
                return _format_amount(float(m.group(1).replace(",", "").replace(" ", "")))
            except ValueError:
                pass
    raw = info.get("_prizepool_raw") or ""
    m = re.search(r"\{\{\s*:\s*([^}|]+?)\s*\}\}", raw)
    if not m:
        # ⚠️ 这里**不能** ``return cleaned``（P1-8，2026-10-14 审查）。
        #
        # 走到这一行的含义是：``prizepoolusd`` 清洗后非空，但**两条数字正则
        # 都没匹配上**（带单位 / 脚注 / 写成模板名之类的写法）。原样返回它，
        # 上层会无条件采信（``if pool: info["prizepoolusd"] = pool``），
        # 于是「奖池：1,600,000 详见页面」这种**看起来是真实金额、实为页面原文**
        # 的字符串被当成解析成功的金额渲染出去 —— 渲染层完全分不出这两者。
        #
        # 金额这一项的口径是「只认原始数字或子页面里的纯数字」
        # （DEVELOPMENT.md「金额纪律」）。解析不出就如实**返回空**（渲染成 ``-``），
        # 要交代原因就交给调用方写进 ``notes``。
        return ""
    try:
        text = await client.wikitext(m.group(1).strip())
    except EsportsError:
        return ""
    digits = re.findall(r"[0-9][0-9,\s]*(?:\.[0-9]+)?", text or "")
    best: float | None = None
    for chunk in digits:
        try:
            value = float(chunk.replace(",", "").replace(" ", ""))
        except ValueError:
            continue
        if value >= 1000 and (best is None or value > best):
            best = value
    return _format_amount(best) if best is not None else ""


def parse_team_participants(wikitext: str) -> list[dict[str, Any]]:
    """解析参赛队伍与阵容（``TeamParticipants`` → ``Opponent`` → ``Person``）。"""
    block = find_template(wikitext, "TeamParticipants")
    if not block:
        return []
    teams: list[dict[str, Any]] = []
    for opponent in iter_templates(block[0], "Opponent"):
        name = _first_positional(opponent)
        if not name:
            continue
        players: list[dict[str, str]] = []
        persons = find_template(opponent, "Persons")
        if persons:
            for person_body in iter_templates(persons[0], "Person"):
                named, positional = template_fields(person_body)
                role = (named.get("role") or "").strip()
                label = ""
                if positional:
                    label = clean(positional[0])
                else:
                    # {{Person|role=1|Nightfall}} 里人名也可能是具名之外的第二段
                    for key in ("name", "player", "id"):
                        if named.get(key):
                            label = clean(named[key])
                            break
                if label:
                    players.append({"role": role, "name": label})
        qualification = ""
        qual = find_template(opponent, "Qualification")
        if qual:
            named_q, _ = template_fields(qual[0])
            qualification = clean(named_q.get("method") or named_q.get("text") or "")
        teams.append({"name": name, "players": players, "qualification": qualification})
    return teams


#: ``{{Person|role=…}}`` 的 role 取值 → 中文展示。
ROLE_LABELS: dict[str, str] = {
    "1": "一号位",
    "2": "二号位",
    "3": "三号位",
    "4": "四号位",
    "5": "五号位",
    "coach": "教练",
    "manager": "领队",
    "sub": "替补",
    "standin": "替补",
}


def parse_prize_slots(wikitext: str) -> list[dict[str, Any]]:
    """解析名次与奖金分配（``{{Slot|place=…|percentage=…}}``）。

    ``usdprize`` 常见写法是 ``{{#expr:{{#var:prizepool_total}} * .42}}``，
    求值不可靠，所以**只取 place / percentage / points**，金额由调用方按奖池现算。

    **``place`` 是可以省略的**：``{{Slot}}`` 不写 ``place`` 时，名次由它在
    ``TeamPrizePool`` 里的**出现位置**隐含。EWC 2026 就是这么写的 —— 冠亚军那两个
    Slot 只写了 ``usdprize`` / ``percentage`` / ``points``，没写 ``place``。

    早先的实现对「没有 place」直接 ``continue``，后果是 EWC 渲染出来的名次表
    **从第 3 名开始**，冠军那一档（37.5%）整行凭空消失，而且**看不出少了东西**。
    所以这里必须按出现序号补名次 —— **不能把「没写 place」当成「不是名次行」**。
    """
    found = find_template(wikitext, "TeamPrizePool")
    if not found:
        return []
    slots: list[dict[str, Any]] = []
    for index, slot_body in enumerate(iter_templates(found[0], "Slot"), start=1):
        named, positional = template_fields(slot_body)
        place = clean(named.get("place") or "")
        if not place and positional:
            # 少数写法把名次写成第一个位置参数
            place = clean(positional[0])
        if not place:
            place = str(index)
        pct: float | None = None
        raw_pct = named.get("percentage") or ""
        m = re.search(r"-?\d+(?:\.\d+)?", raw_pct)
        if m:
            try:
                pct = float(m.group(0))
            except ValueError:
                pct = None
        slots.append(
            {
                "place": place,
                "percent": pct,
                # 赛事方给出的排名积分（如 EWC 的俱乐部积分）。没有就不显示。
                "points": clean(named.get("points") or ""),
            }
        )
    return slots


def parse_group_tables(wikitext: str) -> list[dict[str, Any]]:
    """解析小组积分表（``{{GroupTableLeague}}`` / ``{{GroupTable}}``）。

    这些模板把**真实队名**直接写在 wikitext 里，并给每队一个状态色带
    （``bg1=up`` 之类）—— 也就是「谁在晋级区、谁已出局」的原始依据。
    名次顺序即模板里的 ``teamN`` 顺序。

    标题的处理有个细节：模板自带的 ``title`` 常是千篇一律的 ``Standings``
    （EWC 2026 四张表全叫这个，渲染出来四个同名小节、根本分不清哪个组），
    真正的分组名写在它**上方最近的标题**里（``===Group A===``）。所以：
    ``title`` 为空或属于「没有信息量」的一类时，改用上方标题。
    """
    tables: list[dict[str, Any]] = []
    for template_name in ("GroupTableLeague", "GroupTable", "GroupTableStart"):
        for body, start, _end in iter_template_spans(wikitext, template_name):
            named, _ = template_fields(body)
            own_title = clean(named.get("title") or "")
            if not own_title or own_title.strip().lower() in _GENERIC_TABLE_TITLES:
                title = nearest_heading(wikitext, start) or own_title
            else:
                title = own_title
            rows: list[dict[str, Any]] = []
            for i in range(1, 33):
                team = named.get(f"team{i}")
                if not team:
                    continue
                rows.append(
                    {
                        "order": i,
                        "team": clean(team),
                        "zone": (named.get(f"bg{i}") or "").strip().lower(),
                        "wins": clean(named.get(f"win{i}") or ""),
                        "losses": clean(named.get(f"loss{i}") or ""),
                        "points": clean(named.get(f"p{i}") or named.get(f"pts{i}") or ""),
                    }
                )
            if rows:
                tables.append(
                    {
                        "title": title,
                        "rows": rows,
                        # ``pbgN`` 描述「第 N 名意味着什么」，即区间含义
                        "zones": {
                            str(i): (named.get(f"pbg{i}") or "").strip()
                            for i in range(1, 33)
                            if named.get(f"pbg{i}")
                        },
                        "tournament": clean(named.get("tournament") or ""),
                    }
                )
    return tables


def parse_swiss_zones(wikitext: str) -> dict[str, Any]:
    """解析瑞士轮的区间含义（``{{SwissStandings|bg=1-3=up,…}}``）。

    ``bg`` 形如 ``1-3=up,4-8=stay,9-13=staydown,14-16=down``：
    左边是名次区间，右边是该区间的状态。具体名次由 Lua 从对局算出，
    wikitext 里**没有** —— 需要真实排名时得走 ``rendered()``。
    """
    found = find_template(wikitext, "SwissStandings")
    if not found:
        return {}
    named, _ = template_fields(found[0])
    zones: list[dict[str, str]] = []
    for chunk in (named.get("bg") or "").split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        span, _, status = chunk.partition("=")
        zones.append({"span": span.strip(), "status": status.strip()})
    rounds = [clean(r) for r in iter_templates(found[0], "Round")]
    return {
        "zones": zones,
        "placements": clean(named.get("placements") or ""),
        "round_count": len(rounds),
    }


_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


def parse_date(text: str) -> str:
    """把 Liquipedia 常见日期写法规整成 ``YYYY-MM-DD``（认不出就原样返回）。"""
    text = clean(text)
    if not text:
        return ""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})", text)
    if m:
        month = _MONTHS.get(m.group(1).lower())
        if month:
            return f"{m.group(3)}-{month:02d}-{int(m.group(2)):02d}"
    return text


#: 时刻写法（``16:00``）。**只在日期确实解析出来了才用** —— 见 :func:`parse_datetime`。
_TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")

#: 页面在日期旁边标注的时区缩写（``|date=October 16, 2026 - 16:00 {{Abbr/CST}}``）。
_TZ_RE = re.compile(r"\{\{\s*Abbr\s*/\s*([A-Za-z]{2,5})\s*\}\}", re.I)

#: 常见时区缩写 → 人话（认不出的缩写原样给）。
_TZ_GLOSS: dict[str, str] = {
    "cst": "CST＝北京时间（UTC+8）",
    "utc": "UTC",
    "gmt": "GMT",
    "cest": "CEST＝中欧夏令时（UTC+2）",
    "cet": "CET＝中欧时间（UTC+1）",
    "bst": "BST＝英国夏令时（UTC+1）",
    "kst": "KST＝韩国时间（UTC+9）",
    "jst": "JST＝日本时间（UTC+9）",
    "est": "EST＝美东时间（UTC-5）",
    "edt": "EDT＝美东夏令时（UTC-4）",
    "pst": "PST＝美西时间（UTC-8）",
    "pdt": "PDT＝美西夏令时（UTC-7）",
}

#: 规整后的日期 / 日期+时刻（用来判断「解析成功了没有」）。
_DATE_ONLY_RE = re.compile(r"(\d{4}-\d{2}-\d{2})(?: (\d{2}:\d{2}))?")


def _match_tz(raw: str) -> str:
    """赛程里的时区缩写（``{{Abbr/CST}}`` → ``CST``）；没有则空串。

    为什么要单独捞它：``clean`` 会把 ``{{Abbr/CST}}`` 整个吃掉，于是渲染出来的
    「16:00」**没有时区**。用户看到 16:00 会当成本地时间，而它其实是页面标注的
    时区 —— 这正是本次「你说的中午和我读的时间对不上」的来源。
    """
    m = _TZ_RE.search(raw or "")
    return m.group(1).upper() if m else ""


def parse_datetime(text: str) -> str:
    """日期规整成 ``YYYY-MM-DD``，**原文带时刻就一并带上**（``YYYY-MM-DD HH:MM``）。

    ``|date=October 16, 2026 - 16:00 {{Abbr/CST}}`` 里那个 ``16:00`` 是
    「几点开打」的**唯一**来源（bracket 模板没有别的时刻字段）。2026-10-16 之前
    这里只回日期，于是用户问「今天几点打」永远答不出 —— 渲染出来只有
    「2026-10-16　A vs B」，而「近期赛程日期时间」本身就是需求里点名要的一项。

    反向约束（有测试钉住）：**纯净日期不许凭空补时间**。原文里没有 ``HH:MM``
    就只回日期 —— 补一个 ``00:00`` 等于把「时刻未知」说成「半夜开打」。
    """
    date = parse_date(text)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return date
    m = _TIME_RE.search(clean(text))
    if not m:
        return date
    return f"{date} {int(m.group(1)):02d}:{m.group(2)}"


def date_to_ts(text: str) -> float:
    """``YYYY-MM-DD`` → 本地时间戳；认不出返回 ``0.0``。

    用途是给「按赛事日期窗口切 OpenDota 对局」提供一个边界值 —— 拿不到日期
    就返回 0，调用方按「不设边界」处理（宁可多给几条，也不要凭空砍掉）。
    """
    try:
        return time.mktime(time.strptime(str(text or "").strip(), "%Y-%m-%d"))
    except (ValueError, TypeError):
        return 0.0


def _split_datetime(when: str) -> tuple[str, str]:
    """``parse_datetime`` 的输出 → ``(日期, 时刻)``；认不出就整串当日期、时刻留空。"""
    m = _DATE_ONLY_RE.fullmatch(when or "")
    if not m:
        return when or "", ""
    return m.group(1), m.group(2) or ""


#: 「这一轮已排期、但对手还没决出」时对阵表里写什么。
#: **不是** ``-`` —— 那个符号在一张表里同时表示「时刻未知」「比分未出」，
#: 再用它表示「对手待定」就三种含义糊成一团，用户只会以为是脏数据。
TBD_OPPONENT = "待定"


def _format_match_score(left: str, right: str) -> str:
    """赛程行的比分列。两边都空就是空串（**不是 0**，也不是 ``-`` 的假比分）。"""
    if left and right:
        return f"{left}-{right}"
    return left or right or ""


#: ``{{Map}}`` 子模板里 ``winner`` 的取值 → 哪一边赢（``1`` = opponent1）。
_MAP_WINNERS = frozenset(("1", "2"))

#: 一场 ``{{Match}}`` 里最多几局（Bo5 用不到 5 个 ``matchid`` 槽，但模板留了位置）。
_MAX_MAP_IDS = 5


def _series_from_maps(match_body: str) -> tuple[str, list[str]]:
    """从一场 ``{{Match}}`` 的 ``{{Map}}`` 子模板数出**系列赛比分**。

    返回 ``(比分, matchid 列表)``；比分取不出来时是空串。

    Liquipedia 的比分**不在** ``{{Match}}`` 顶层的 ``score=`` 里（那个字段除了
    弃权 ``FF``/``W`` 常年为空），而是**逐局**记在 ``{{Map}}`` 上：每局一个
    ``|winner=1``（1 = opponent1 赢）/ ``|winner=2``。把这个数出来才是系列赛比分
    —— 实测 ``DreamLeague/30/China`` 整页 ``score=`` 全空，而同一天的
    ``BLAST/SLAM/9/China`` 逐局 ``winner`` 填得齐全（2-0、1-0 都数得出来）。

    两条不许含糊的规则：

    1. ``|map3={{Map|finished=skip}}`` 的意思是「**这一局不用打**」（Bo3 里 2-0
       之后收工），**不是「还没打」**。不排掉它，一场 2-0 会被讲成「第三局待打」。
    2. **局数不齐时不给比分。** ``winner`` 的个数若少于非空的 ``matchid`` 个数，
       说明有局只登记了 id、胜负还没填 —— 这时数出来的比分会**低报**（实测
       ``DreamLeague/30/Southeast Asia`` 有一场 ``matchid`` 两个、``winner`` 一个，
       真实是 1-1 而数出来只有 0-1）。宁可回空串交给对局库兜底，也不要给一个
       看着像事实的错数。
    """
    wins = {"1": 0, "2": 0}
    played = 0
    for body in iter_templates(match_body, "Map"):
        named, _pos = template_fields(body)
        if clean(named.get("finished") or "").lower() == "skip":
            continue  # 「不用打」的局，不许算进 series
        winner = clean(named.get("winner") or "")
        if winner in _MAP_WINNERS:
            wins[winner] += 1
            played += 1
    named, _pos = template_fields(match_body)
    ids = [
        clean(named.get(f"matchid{i}") or "")
        for i in range(1, _MAX_MAP_IDS + 1)
    ]
    ids = [i for i in ids if i.isdigit()]
    if not played or played < len(ids):
        return "", ids
    return f"{wins['1']}-{wins['2']}", ids


def parse_matches(wikitext: str) -> list[dict[str, Any]]:
    """解析赛程（``{{Match|opponent1=…|opponent2=…|date=…}}``）。

    行形状：``{left, right, date, time, score, map_score, matchids, winner}``。

    五条硬规则，都是真机撞出来的：

    1. **半骨架必须跳过。**上一版只跳「对手与日期**全**空」的，于是 10-17 那三场
       半决赛（``|date=October 17, 2026 - 16:00`` 而两个 ``{{TeamOpponent|}}``
       都空）被渲染成 ``- vs -`` 的**假对阵**（报告 10.3-③）。现在只要对手取不出来
       就跳过，日期有没有都不算数；这些场次由 :func:`pending_match_dates` 单独交代。
    2. **弃权比分在嵌套的 ``{{TeamOpponent|…|score=…}}`` 里**，不在 ``{{Match}}`` 上
       —— 写法是 ``score=FF`` / ``score=W``。**注意它只管弃权**：正常打完的比赛
       这里也是空的（全页扫下来几乎恒为 0），真比分见第 5 条。
    3. **时刻保留**（见 :func:`parse_datetime`）。
    4. **单边对手为空是真信息，保留并写「待定」。**``opponent1=X`` 而
       ``opponent2={{TeamOpponent|}}`` 不是脏数据 —— 那是「X 已进这一轮，
       对手等前面那场决出来」（实机：``Yangon Galacticos`` 16:00 一场、
       对手待定）。上一版把空的那边渲染成 ``-``，看起来像「对手叫横杠」；
       两边都空的才是纯骨架（由 :func:`pending_match_dates` 交代）。
    5. **打完的比分要从 ``{{Map}}`` 子模板数**（见 :func:`_series_from_maps`），
       落进 ``map_score``；``matchids`` 是对局库的精确钥匙，留着给跨源对齐用。
       这两样**都是 ``{{Match}}`` 顶层没有的** —— 只读顶层就会得出「wiki 不记比分」
       的错误结论（2026-10-16 踩到）。
    """
    out: list[dict[str, Any]] = []
    for body in iter_templates(wikitext, "Match"):
        named, _ = template_fields(body)
        left = right = ""
        score_left = score_right = ""
        for key, side in (("opponent1", "left"), ("opponent2", "right")):
            raw = named.get(key) or ""
            value = clean(raw)
            # ``{{TeamOpponent|TeamA|score=2}}`` 会被 clean 压成空串，要单独取。
            # 空骨架 ``{{TeamOpponent|}}`` 取出来是空串 —— **不能**退化成模板名本身，
            # 那会渲染出「TeamOpponent| vs TeamOpponent|」这种怪对阵。
            nested = find_template(raw, "TeamOpponent")
            score = ""
            if nested:
                n_named, _n_pos = template_fields(nested[0])
                value = _first_positional(nested[0]) or value
                score = clean(n_named.get("score") or "")
            if side == "left":
                left, score_left = value, score
            else:
                right, score_right = value, score
        # 反面：「对手全空」才跳过。**不许**改成「有日期就渲染」——那正是假对阵的来源。
        # 单边为空**不跳**：那一方是确定的，缺的一方等前面的比赛决出（写「待定」）。
        if not (left or right):
            continue
        date, clock = _split_datetime(parse_datetime(named.get("date") or ""))
        # 打完的比分在 {{Map}} 子模板里逐局记着（见 _series_from_maps）。
        map_score, matchids = _series_from_maps(body)
        out.append(
            {
                "left": left or TBD_OPPONENT,
                "right": right or TBD_OPPONENT,
                "date": date or "-",
                "time": clock,
                "tz": _match_tz(named.get("date") or ""),
                "score": _format_match_score(score_left, score_right),
                "map_score": map_score,
                "matchids": matchids,
                "winner": clean(named.get("winner") or ""),
            }
        )
    return out


def pending_match_dates(wikitext: str) -> list[str]:
    """**已排期但对手未定**的对阵日期（去重升序）。

    这些场次不能渲染成 ``- vs -``，但也不能一句不提 —— 「明天还有几场、
    几点打」本身就是赛程的一部分答案。收集的是**日期**，让渲染层能写出一句
    「另有 N 场已排期、对阵待定（10-17）」，而不是把它们混进对阵表里充数。
    """
    dates: set[str] = set()
    for body in iter_templates(wikitext, "Match"):
        named, _ = template_fields(body)
        if any(_opponent_name(named.get(key) or "") for key in ("opponent1", "opponent2")):
            continue
        when, _clock = _split_datetime(parse_datetime(named.get("date") or ""))
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", when):
            dates.add(when)
    return sorted(dates)


def _opponent_name(raw: str) -> str:
    """``{{TeamOpponent|X}}`` / 裸队名 → 队名（取不到返回空串）。"""
    nested = find_template(raw, "TeamOpponent")
    if nested:
        return _first_positional(nested[0])
    return clean(raw)


#: 选手模板的两种写法：``{{Person}}``（现在的写法）与 ``{{Player}}``（旧写法）。
#: 只认一种就会整块漏掉 —— 实机上问 Nemiga 的阵容返回空，就是因为页面用
#: ``{{Person}}`` 而解析只找了 ``{{Player}}``。
_PERSON_TEMPLATES = ("Person", "Player")

#: ``position`` / ``role`` 字段的取值 → 中文说法（认不出就原样保留）。
_POSITION_LABELS = {
    "1": "1 号位", "2": "2 号位", "3": "3 号位", "4": "4 号位", "5": "5 号位",
    "coach": "教练", "head coach": "主教练", "manager": "领队", "analyst": "分析师",
    "substitute": "替补", "stand-in": "临时替补",
}

#: 空位占位符：出现了也不能算一个人。
_EMPTY_PLAYER_LABELS = {"tbd", "vacant", "-", "?", "n/a", "none"}


def _position_label(raw: str) -> str:
    """把 ``position=2`` / ``position=Coach`` 翻成中文（认不出原样返回）。"""
    text = clean(raw)
    if not text:
        return ""
    return _POSITION_LABELS.get(text.lower(), text)


def _person_row(body: str) -> dict[str, str] | None:
    """一条 ``{{Person|…}}`` / ``{{Player|…}}`` → 阵容行；抽不出名字就返回 ``None``。

    名字优先取 ``id``（游戏 ID，用户认的是这个），没有才退回 ``name``（本名）
    或第一个位置参数。``captain=y`` 折进角色里一起显示。
    """
    named, positional = template_fields(body)
    nick = clean(named.get("id") or "")
    real = clean(named.get("name") or "")
    label = nick or real or (clean(positional[0]) if positional else "")
    if not label or label.lower() in _EMPTY_PLAYER_LABELS:
        return None
    role = _position_label(named.get("position") or named.get("role") or "")
    if clean(named.get("captain") or "").lower() in {"y", "yes", "true", "1"}:
        role = f"{role}·队长" if role else "队长"
    return {"name": label, "role": role, "real_name": real if nick else ""}


def _squad_rows(wikitext: str, status: str) -> list[dict[str, str]]:
    """抽 ``{{Squad|status=<status>}}`` 块里的选手。

    为什么必须按 ``status`` 取块：Dota2 wiki 的 ``==Player Roster==`` 小节下，
    现役阵容与历史阵容是**并列的十几个同类模板**，只有 ``status`` 不同
    （Nemiga 页面：1 个 ``active``、1 个 ``inactive``、17 个 ``former``，
    合计 79 个 ``Person``）。整页扫会得到一个几十人的花名册。
    """
    rows: list[dict[str, str]] = []
    for body in iter_templates(wikitext, "Squad"):
        named, _ = template_fields(body)
        if clean(named.get("status") or "").lower() != status:
            continue
        rows.extend(_person_rows_in(body))
    return rows


def _person_rows_in(block: str) -> list[dict[str, str]]:
    """块内的选手行（``{{Person}}`` 与旧写法 ``{{Player}}`` 都认）。"""
    rows: list[dict[str, str]] = []
    for name in _PERSON_TEMPLATES:
        for body in iter_templates(block, name):
            row = _person_row(body)
            if row:
                rows.append(row)
    return rows


def _person_rows_outside_squads(wikitext: str) -> list[dict[str, str]]:
    """整页里**不在任何 ``{{Squad}}`` 块内**的选手行（兜底用）。

    老页面可能把阵容直接写在 ``==Roster==`` 下、不套 ``{{Squad}}``。
    兜底时故意只取块外的：块内的一律按 ``status`` 处理，否则又会把
    ``status=former`` 的历史阵容混进现役。
    """
    spans = [(start, end) for _body, start, end in iter_template_spans(wikitext, "Squad")]
    rows: list[dict[str, str]] = []
    for name in _PERSON_TEMPLATES:
        for body, start, _end in iter_template_spans(wikitext, name):
            if any(left <= start < right for left, right in spans):
                continue
            row = _person_row(body)
            if row:
                rows.append(row)
    return rows


def parse_team_page(wikitext: str) -> dict[str, Any]:
    """解析战队页面（Infobox team + 阵容）。

    ``Infobox team`` 的字段名比 ``Infobox league`` 更随版本漂移（``region`` /
    ``location`` / ``country`` 都可能出现），所以这里**不做白名单**：把所有
    非空字段都收下来，由 :func:`format_team` 按需读取。多出来的键不影响渲染。

    阵容分两组返回：``roster``（现役）与 ``inactive``（``status=inactive``，
    替补 / 暂离队）。``former`` 一律不收 —— 历史阵容对「这队都有谁」是噪音，
    要查转会另有 :func:`filter_transfers`。
    """
    found = find_template(wikitext, "Infobox team")
    info: dict[str, str] = {}
    if found:
        named, _ = template_fields(found[0])
        for key, raw in named.items():
            value = clean(raw or "")
            if value:
                info[key] = value

    roster = _squad_rows(wikitext, "active")
    if not roster:
        # 没有 status=active 的块：老页面 / 别的写法，退回「块外的选手行」。
        roster = _person_rows_outside_squads(wikitext)
    inactive = _squad_rows(wikitext, "inactive")
    return {"info": info, "roster": roster, "inactive": inactive}


def parse_transfers_html(html: str) -> list[dict[str, str]]:
    """从 ``Portal:Transfers`` 的渲染结果里抽转会条目。

    该页正文只有 ``{{Transfer List|limit=50}}``，真实数据由 Lua 渲染，
    所以只能解析 HTML 表格。列依次是：日期、选手、原队、新队。
    """
    if not html:
        return []
    rows: list[dict[str, str]] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)
        if len(cells) < 3:
            continue
        values = [clean(_HTML_TAG_RE.sub(" ", c)) for c in cells]
        values = [v for v in values if v not in ("", "&nbsp;")]
        if len(values) < 3:
            continue
        # 跳过表头行
        joined = " ".join(values).lower()
        if "date" in joined and ("player" in joined or "team" in joined):
            continue
        rows.append(
            {
                "date": values[0],
                "player": values[1] if len(values) > 1 else "",
                "from": values[2] if len(values) > 2 else "",
                "to": values[3] if len(values) > 3 else "",
            }
        )
    return rows


# ======================================================================
# 渲染
# ======================================================================


def _dash(value: Any) -> str:
    """缺失一律 ``-``，不把「没给」和「是 0」混起来。"""
    text = str(value if value is not None else "").strip()
    return text if text else "-"


def tournament_status(info: dict[str, str]) -> tuple[str, str]:
    """按起止日期判断赛事状态，返回 ``(状态词, 日期区间描述)``。"""
    start = parse_date(info.get("sdate") or "")
    end = parse_date(info.get("edate") or "")
    span = f"{start or '?'} ~ {end or '?'}"

    def _to_ts(value: str) -> float | None:
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", value or "")
        if not m:
            return None
        try:
            import datetime as _dt

            return _dt.datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=_dt.timezone.utc
            ).timestamp()
        except ValueError:
            return None

    now = time.time()
    ts_start, ts_end = _to_ts(start), _to_ts(end)
    if ts_start and now < ts_start:
        return "未开始", span
    if ts_end and now > ts_end + 86400:
        return "已结束", span
    if ts_start and ts_end:
        return "进行中", span
    return "日程未定", span


def format_tournament(
    *,
    query: str,
    page: str,
    info: dict[str, str],
    teams: list[dict[str, Any]] | None = None,
    slots: list[dict[str, Any]] | None = None,
    tables: list[dict[str, Any]] | None = None,
    swiss: dict[str, Any] | None = None,
    matches: list[dict[str, str]] | None = None,
    results: list[dict[str, Any]] | None = None,
    notes: list[str] | None = None,
) -> str:
    """渲染赛事概览（中文 Markdown）。

    ``matches``：赛事页 bracket 里的对阵（**含未开打**，带日期与时刻）。
    ``results``：OpenDota 那边的对局结果（**只有打完的**）。两者在这里合成
    **一张「赛程与结果」表** —— 用户要的就是这一个视图：哪几场、几点打、谁赢了。
    """
    schedule, extra_results = merge_schedule(matches or [], results or [])
    status, span = tournament_status(info)
    title = info.get("name") or page
    short = info.get("shortname") or info.get("tickername") or ""
    head = f"【{title}】" + (f"（{short}）" if short else "")

    lines = [head]
    lines.append(f"- 状态：{status}｜{span}")
    tier = info.get("liquipediatier") or info.get("pctier") or info.get("publishertier")
    meta: list[str] = []
    if tier:
        meta.append(f"级别 T{tier}")
    if info.get("organizer"):
        meta.append(f"主办 {info['organizer']}")
    place = " ".join(x for x in (info.get("country"), info.get("city")) if x)
    if place:
        meta.append(place)
    if info.get("type"):
        meta.append(info["type"])
    if meta:
        lines.append("- " + "｜".join(meta))
    if info.get("format"):
        lines.append(f"- 赛制：{info['format']}")
    pool = _dash(info.get("prizepoolusd"))
    count = info.get("team_number") or "-"
    patch = info.get("patch") or "-"
    lines.append(f"- 奖池：{pool}｜参赛：{count} 队｜版本：{patch}")
    if info.get("series"):
        lines.append(f"- 系列：{info['series']}")

    if slots:
        lines.append("")
        lines.append("### 名次与奖金分配")
        has_points = False
        for slot in slots[:12]:
            pct = slot.get("percent")
            pct_text = f"约 {pct:g}%" if isinstance(pct, (int, float)) else "-"
            pts = slot.get("points") or ""
            if pts:
                has_points = True
            tail = f"｜{pts} 积分" if pts else ""
            lines.append(f"- 第 {slot['place']} 名：{pct_text}{tail}")
        # 这一块只有「按名次区间划分的档位」，**不指名战队** —— 逐队最终名次在淘汰赛
        # 对阵表里（常常是独立子页面）。不写这句，模型很容易把某一档说成「某队拿了第几」。
        lines.append("_（以上是按最终名次划分的奖金档位，未指名具体战队）_")
        if has_points:
            lines.append("_（「积分」是赛事方给出的排名积分，不是奖金）_")

    if tables:
        for table in tables:
            lines.append("")
            lines.append(f"### {table.get('title') or '积分榜'}")
            zones_hint = False
            for row in table.get("rows", [])[:20]:
                zone = row.get("zone") or ""
                label = _zone_label(zone) if zone else ""
                if zone:
                    zones_hint = True
                extra = []
                if row.get("wins") or row.get("losses"):
                    extra.append(f"{_dash(row.get('wins'))}-{_dash(row.get('losses'))}")
                if row.get("points"):
                    extra.append(f"积分 {row['points']}")
                tail = ("｜" + "｜".join(extra)) if extra else ""
                lines.append(f"{row['order']}. {row['team']}" + (f"（{label}）" if label else "") + tail)
            if zones_hint:
                lines.append("_（括号内为赛事定义的区域：晋级区 / 晋级边缘 / 待定 / 淘汰区）_")

    if swiss and swiss.get("zones"):
        lines.append("")
        lines.append("### 出线区间")
        for zone in swiss["zones"]:
            lines.append(f"- 第 {zone['span']} 名：{_zone_label(zone['status'])}")
        if swiss.get("round_count"):
            lines.append(f"- 已记录 {swiss['round_count']} 轮对阵")
        lines.append("_（逐队名次需读取赛事页的实时积分表，未取到时不给具体排名）_")

    if schedule or extra_results:
        lines.append("")
        total = len(schedule)
        lines.append(f"### 赛程与结果（{total} 场）")
        lines.append("| 日期 | 时间 | 对阵 | 比分/结果 |")
        lines.append("|---|---|---|---|")
        for row in schedule[:MAX_SCHEDULE_ROWS]:
            clock = str(row.get("time") or "") or "-"
            zone = str(row.get("tz") or "")
            lines.append(
                f"| {row.get('date') or '-'} | {clock}{f' {zone}' if zone else ''} | "
                f"{row.get('left') or '-'} vs {row.get('right') or '-'} | "
                f"{row.get('score') or '-'} |"
            )
        if total > MAX_SCHEDULE_ROWS:
            lines.append(f"_（共 {total} 场，这里列前 {MAX_SCHEDULE_ROWS} 场）_")
        zones = [t for t in dict.fromkeys(row.get("tz") or "" for row in schedule) if t]
        if zones:
            # 时区必须写出来：页面上的 16:00 是**页面标注的**时区，不是用户本地时间。
            # 不写这一句，用户会把 16:00 当成本地时间读 —— 「你说的中午和我读的
            # 时间对不上」就是这么来的。
            lines.append(
                "_（时区："
                + "、".join(_TZ_GLOSS.get(t.lower(), t) for t in zones)
                + "；没标时区的行多半是从对局库补的，时刻是插件本地时间）_"
            )
        if extra_results:
            lines.append(f"_（另有 {extra_results} 场已收录在 OpenDota、赛事页没列出）_")
        lines.append(
            "_（比分是**局分**、一局系列赛一行：**优先取赛事页自己逐局记的胜负**，"
            "赛事页还没记的才用对局库补 —— 没标时区的行就是从对局库补的那种。"
            "`-` 表示**还没打完 / 两边都还没记**，不是 0 分、也不是输了）_"
        )

    # 参赛队伍放**最后**：它是最长的一块（大赛事 20+ 行），而工具输出有长度
    # 上限 —— 放前面会把「名次 / 积分榜 / 出线」这些真正要判断的内容挤掉。
    if teams:
        lines.append("")
        lines.append(f"### 参赛队伍（{len(teams)} 队）")
        # 队伍多时只列名字（20+ 支队 × 一行阵容会直接把输出撑爆），
        # 阵容交给 query_esports_team（一次一支队，带赛区与教练）。
        detailed = len(teams) <= 8
        for team in teams[:32]:
            qual = f"（{team['qualification']}）" if team.get("qualification") else ""
            line = f"- {team['name']}{qual}"
            if detailed:
                roster = team.get("players") or []
                shown = "、".join(
                    f"{ROLE_LABELS.get(p['role'], p['role'] or '选手')} {p['name']}"
                    if p.get("role")
                    else p["name"]
                    for p in roster[:7]
                )
                if shown:
                    line += f"：{shown}"
            lines.append(line)
        if not detailed:
            lines.append("_（队伍较多，这里只列名单；想看某支队的阵容 / 赛区，问「XX 队什么阵容」）_")

    if notes:
        lines.append("")
        for note in notes:
            lines.append(f"> {note}")

    lines.append("")
    lines.append(f"_来源页面：{page}｜{SOURCE_NOTE}_")
    return "\n".join(lines)


def format_team(
    *,
    query: str,
    page: str,
    info: dict[str, str],
    roster: list[dict[str, str]] | None = None,
    inactive: list[dict[str, str]] | None = None,
    od: dict[str, Any] | None = None,
    recent: list[dict[str, Any]] | None = None,
    leagues: list[str] | None = None,
    notes: list[str] | None = None,
) -> str:
    """渲染战队档案（中文 Markdown）。"""
    # 标题优先用**解析出来的队名**，最后才退回用户原话。
    # 为什么要在意这个：用户说「雪碧」，别名表 + 索引匹配之后查的是 Team Spirit ——
    # 标题直接写「雪碧」的话，万一别名换错人（或索引里同名多解挑错），
    # 用户从输出上根本看不出来。把实际查到的那支队摆在标题里，错了一眼能认出来。
    resolved = str((od or {}).get("name") or "").strip()
    name = info.get("name") or resolved or query or page
    asked = (query or "").strip()
    head = f"【{name}】" if not asked or asked == name else f"【{name}】（你问的「{asked}」）"
    lines = [head]
    meta: list[str] = []
    location = (
        info.get("location") or info.get("country") or info.get("city") or ""
    )
    if location:
        meta.append(location)
    if info.get("region"):
        meta.append(f"赛区 {info['region']}")
    if info.get("founded") or info.get("created"):
        meta.append(f"成立于 {info.get('founded') or info.get('created')}")
    if info.get("coach"):
        meta.append(f"教练 {info['coach']}")
    if meta:
        lines.append("- " + "｜".join(meta))

    if od:
        rating = od.get("rating")
        wins, losses = od.get("wins"), od.get("losses")
        parts = []
        if isinstance(rating, (int, float)):
            parts.append(f"评分 {rating:.0f}")
        if isinstance(wins, int) and isinstance(losses, int):
            parts.append(f"战绩 {wins} 胜 {losses} 负")
        if parts:
            lines.append("- " + "｜".join(parts))

    if roster or inactive:
        lines.append("")
        lines.append("### 阵容")
        for title, rows in (("现役", roster or []), ("替补 / 暂离队", inactive or [])):
            if not rows:
                continue
            if inactive:
                lines.append(f"**{title}**")
            for player in rows[:12]:
                role = player.get("role") or ""
                real = player.get("real_name") or ""
                detail = "｜".join(x for x in (role, real) if x)
                lines.append(f"- {player['name']}" + (f"（{detail}）" if detail else ""))

    if leagues:
        lines.append("")
        lines.append("### 近期参加赛事")
        for item in leagues[:10]:
            lines.append(f"- {item}")

    if recent:
        lines.append("")
        lines.append("### 近期职业战绩")
        for m in recent[:10]:
            when = m.get("date") or "-"
            result = m.get("result") or "-"
            score = m.get("score") or ""
            tail = f"{result} {score}".strip()
            lines.append(
                f"- {when}　{m.get('league') or '-'}｜vs {m.get('opponent') or '-'}｜{tail}"
            )

    if notes:
        lines.append("")
        for note in notes:
            lines.append(f"> {note}")

    lines.append("")
    lines.append(f"_来源页面：{page}｜{SOURCE_NOTE}_" if page else f"_{SOURCE_NOTE}_")
    return "\n".join(lines)


def format_transfers(rows: list[dict[str, str]], *, query: str = "", limit: int = 20) -> str:
    """渲染转会动态（中文 Markdown）。"""
    lines = ["【选手转会动态】"]
    if query:
        lines.append(f"- 筛选：{query}")
    if not rows:
        lines.append("")
        lines.append("没取到转会条目（数据源当前不可用或该时间段内没有记录）。")
        lines.append("")
        lines.append(f"_来源页面：Portal:Transfers｜{SOURCE_NOTE}_")
        return "\n".join(lines)
    lines.append(f"- 共取到 {len(rows)} 条，展示前 {min(limit, len(rows))} 条")
    lines.append("")
    for row in rows[:limit]:
        date = row.get("date") or "-"
        player = row.get("player") or "-"
        src = row.get("from") or "?"
        dst = row.get("to") or "?"
        lines.append(f"- {date}　{player}：{src} → {dst}")
    lines.append("")
    lines.append(f"_来源页面：Portal:Transfers｜{SOURCE_NOTE}_")
    return "\n".join(lines)


def format_pro_matches(rows: list[dict[str, Any]], *, limit: int = 12) -> str:
    """渲染近期职业比赛（OpenDota ``/proMatches``）——**按赛事归并**。

    为什么必须归并（报告方案 B-8）：12 场平铺时，「昨天有哪些职业比赛」这个问题
    要人肉把同一联赛的行拼起来才答得出；更糟的是那份名单里**没有任何线索**能提示
    用户问的那个中国区预选赛在哪 —— 真机实测 12 条里一场 BLAST 中国区都没有，
    模型于是只能猜赛事名，三次全错。

    口径必须写出来（报告方案 B-10）：这条路只拿得到**已收录（即已结束）**的对局，
    而它长得像「今天有什么比赛」的标准答案 —— 不写清口径，模型会把
    「今天没别的比赛了」当成事实讲给用户。
    """
    lines = ["【近期职业比赛】"]
    if not rows:
        lines.append("- 没取到数据。")
        return "\n".join(lines)
    lines.append(
        "_口径：以下是 OpenDota **已收录（即已结束）** 的最近职业对局；"
        "正在进行与尚未开打的不在其中。_"
    )
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    index: dict[str, int] = {}
    for m in rows[:limit]:
        name = str(m.get("league") or "").strip() or "（未标明赛事）"
        if name not in index:
            index[name] = len(groups)
            groups.append((name, []))
        groups[index[name]][1].append(m)
    for name, items in groups:
        days = sorted({str(x.get("date") or "")[:10] for x in items if x.get("date")})
        span = ""
        if days:
            span = f"｜{days[0]}" + (f" ~ {days[-1]}" if len(days) > 1 else "")
        lines.append("")
        lines.append(f"### {name}（{len(items)} 场{span}）")
        for m in items:
            score = m.get("score") or ""
            tail = f"（{score}）" if score else ""
            lines.append(
                f"- {m.get('date') or '-'}　{m.get('radiant') or '-'} vs {m.get('dire') or '-'}{tail}"
            )
    return "\n".join(lines)


# ======================================================================
# 「今天有什么比赛」—— 跨赛事的日赛程（**只走 Liquipedia**）
# ======================================================================
#
# 为什么单开一条路：``query_tournament`` 空 query 原先退回 OpenDota
# ``/proMatches``，而那是**结果池**（滞后约 2.4 小时、只有已结束的对局）。
# 2026-10-16 真机：用户问「今天正在打的中国区预选赛」，模型两次都拿到 SEA /
# 西欧的**已结束**对局，一场中国区都没有 —— 于是开始猜赛事名（BetBoom /
# ESL One / TI 2026），三次全错。**连问两轮答不出，错的不是模型，是那条路
# 结构上就答不了「正在打 / 还没打」这件事。**
#
# Liquipedia 有的是**日程池**：没开打的比赛也挂在赛事页上。难的是「同一天
# 有哪些赛事」这个跨赛事视角 —— 实测 ``insource:"October 16, 2026"`` 一次
# 就能列出当天全部 6 个赛事页（含用户问的 BLAST/SLAM/9/China），再逐页解析。

#: 英文月份 —— Liquipedia 赛程的日期写法是 ``|date=October 16, 2026 - 16:00``。
_EN_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

#: ``date.weekday()`` → 中文星期（渲染标题用）。
_WEEKDAYS_CN = ("一", "二", "三", "四", "五", "六", "日")

#: 日赛程最多解析几个赛事页。每页一次 ``wikitext``（Liquipedia 1 次 / 2 秒），
#: 8 页约 18 秒；再多单次问答就太慢了，超出部分如实写「还有 N 个赛事没展开」。
MAX_DAILY_PAGES = 8

#: 整体结果的缓存时长（秒）。同一天连着问几次，不该重跑十几秒的请求链。
DAILY_CACHE_TTL = 600.0

#: ``insource`` 搜索会带上的**非赛事页**。实测 ``insource:"2026-10-16"``
#: 会返回 ``Primal Beast/Changelogs``（英雄改动页）这种 —— 每漏滤一个就多花
#: 一次 2 秒的 wikitext 请求。
_NON_EVENT_PREFIXES = (
    "Portal:", "Liquipedia:", "Template:", "Category:", "Help:", "User:",
    "File:", "Module:", "MediaWiki:", "Special:", "Talk:", "Draft:",
)
_NON_EVENT_SUFFIXES = ("/changelogs", "/broadcasts", "/statistics")


def en_date(iso: str) -> str:
    """``2026-10-16`` → ``October 16, 2026``（拼不出来回空串）。"""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", (iso or "").strip())
    if not m:
        return ""
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return f"{_EN_MONTHS[month - 1]} {day}, {year}"


def shift_date(iso: str, days: int) -> str:
    """``2026-10-16`` 加减 N 天 → ``YYYY-MM-DD``（认不出回空串）。"""
    try:
        base = datetime.date.fromisoformat((iso or "").strip())
    except ValueError:
        return ""
    return (base + datetime.timedelta(days=int(days))).isoformat()


def _weekday_suffix(iso: str) -> str:
    """``2026-10-16`` → ``（周五）``（认不出回空串）。"""
    try:
        base = datetime.date.fromisoformat((iso or "").strip())
    except ValueError:
        return ""
    return f"（周{_WEEKDAYS_CN[base.weekday()]}）"


def is_event_title(title: str) -> bool:
    """这个标题像不像「一场赛事的页面」（滤掉 ``insource`` 搜索的噪音）。"""
    text = (title or "").strip()
    if not text or text.startswith(_NON_EVENT_PREFIXES):
        return False
    low = text.lower()
    return not any(low.endswith(suffix) for suffix in _NON_EVENT_SUFFIXES)


def _region_of_title(title: str) -> str:
    """标题里带的赛区（``DreamLeague/30/Southeast Asia`` → ``Southeast Asia``）。

    标题级赛区**只用来筛页面**（省掉一次 wikitext 请求）；页面里到底有没有
    该赛区的场次，仍以解析结果为准。
    """
    region, _start, _end = _region_match(title or "")
    return region


async def find_daily_pages(
    client: LiquipediaClient, date: str, *, limit: int = 20
) -> list[str]:
    """搜出「这一天有比赛」的赛事页标题（去重保序）。

    **两种日期写法都得搜**：英文长格式与 ISO。实测同一天前者命中 6 页、后者
    命中 3 页，**不是子集关系**（BLAST 两页都命中，但东南亚 / 西欧 / EPL 只在
    长格式里）—— 少搜一种就漏赛事。
    """
    out: list[str] = []
    seen: set[str] = set()
    for term in (en_date(date), (date or "").strip()):
        if not term:
            continue
        for hit in await client.search(f'insource:"{term}"', limit=limit):
            if hit in seen or not is_event_title(hit):
                continue
            seen.add(hit)
            out.append(hit)
    return out


def _daily_event(page: str, rows: list[dict[str, str]], pending: list[str]) -> dict[str, Any]:
    """一个赛事页在某天窗口里的场次 → 渲染用的结构。"""
    rows.sort(key=lambda r: (str(r.get("date") or ""), str(r.get("time") or "")))
    return {
        "page": page,
        "region": _region_of_title(page),
        "matches": rows,
        "pending": pending,
    }


async def collect_daily_schedule(
    client: LiquipediaClient,
    *,
    start: str = "",
    days: int = 1,
    region: str = "",
    max_pages: int = MAX_DAILY_PAGES,
) -> dict[str, Any]:
    """跨赛事的日赛程（「今天有什么比赛」「接下来几天」）—— **只走 Liquipedia**。

    ``insource`` 只搜**起始日**，但解析后按 ``[start, start+days)`` 窗口过滤 ——
    一个赛事页通常含它自己好几天的赛程（BLAST 中国区页 10-16 与 10-17 都在），
    所以「接下来两天」不需要多搜那几天的日期，省下的都是 2 秒一趟的请求。

    返回 ``{start, end, days, region, events[], pages[], skipped, notes[]}``。
    """
    days = max(1, int(days))
    begin = (start or "").strip() or datetime.date.today().isoformat()
    end = shift_date(begin, days) or begin
    region = (region or "").strip()

    result: dict[str, Any] = {
        "start": begin, "end": end, "days": days, "region": region,
        "events": [], "pages": [], "skipped": 0, "notes": [],
    }

    cache_key = f"{begin}|{days}|{region}|{max_pages}"
    cached = client.cache.get("daily", cache_key, DAILY_CACHE_TTL)
    if isinstance(cached, dict):
        result.update(cached)
        return result

    pages = await find_daily_pages(client, begin)
    if not pages:
        result["notes"].append(f"Liquipedia 上没搜到 {begin} 这天有排期的赛事页。")
        client.cache.set("daily", cache_key, dict(result))
        return result

    if region:
        # 用户点名了赛区就**只**给那个赛区：标题里写着别的赛区的、以及压根没写
        # 赛区的（EPL、NEXUS 这种）都不是他要的。实测「中国区」若把「没写赛区」
        # 也放过，6 页里只会滤掉 2 页，等于没滤。
        pages = [p for p in pages if _region_of_title(p) == region]
        if not pages:
            result["notes"].append(
                f"Liquipedia 上没搜到 {begin} 这天标着「{region}」的赛事排期。"
            )
            client.cache.set("daily", cache_key, dict(result))
            return result

    result["pages"] = pages
    result["skipped"] = max(0, len(pages) - max_pages)

    for page in pages[:max_pages]:
        wikitext = await client.wikitext(page)
        if not wikitext:
            continue
        rows = [
            row for row in parse_matches(wikitext)
            if begin <= str(row.get("date") or "") < end
        ]
        if not rows:
            continue  # 这一页只是「提到了这一天」，当天并没有对阵
        pending = [d for d in pending_match_dates(wikitext) if begin <= d < end]
        result["events"].append(_daily_event(page, rows, pending))

    if not result["events"]:
        result["notes"].append(f"{begin} 这天没解析出任何对阵。")

    client.cache.set("daily", cache_key, dict(result))
    return result


def format_daily_schedule(data: dict[str, Any]) -> str:
    """渲染日赛程 —— 每个赛事一小节，一张「时间 / 对阵 / 比分」表。"""
    start = str(data.get("start") or "")
    days = max(1, int(data.get("days") or 1))
    end = str(data.get("end") or start)
    region = str(data.get("region") or "")
    events = data.get("events") or []
    notes = [str(n) for n in (data.get("notes") or []) if n]

    if days <= 1:
        lines = [f"【今日赛程 · {start}{_weekday_suffix(start)}】"]
    else:
        last = shift_date(end, -1) or end
        lines = [f"【赛程 · {start} ~ {last}】"]
    if region:
        lines.append(f"_（只看「{region}」）_")

    if not events:
        lines.append("- 没查到这天的赛程。")
        lines.extend(f"> {n}" for n in notes)
        return "\n".join(lines)

    total = sum(len(ev.get("matches") or []) for ev in events)
    lines.append(
        f"_口径：来自 Liquipedia 赛事页的**日程**（{len(events)} 个赛事、{total} 场）；"
        "打完的照赛事页逐局记的胜负写出比分，空着的是**还没打完**。_"
    )

    wide = days > 1
    for ev in events:
        rows = ev.get("matches") or []
        label = str(ev.get("page") or "")
        if ev.get("region"):
            label += f"｜{ev['region']}"
        lines.append("")
        lines.append(f"### {label}（{len(rows)} 场）")
        lines.append("| 日期 | 时间 | 对阵 | 比分/结果 |" if wide else "| 时间 | 对阵 | 比分/结果 |")
        lines.append("|---|---|---|---|" if wide else "|---|---|---|")
        for row in rows:
            clock = str(row.get("time") or "") or "-"
            zone = str(row.get("tz") or "")
            vs = f"{row.get('left') or TBD_OPPONENT} vs {row.get('right') or TBD_OPPONENT}"
            # 打完的场次：赛事页用 ``{{Map}}`` 逐局记着胜负，取它（``score`` 是弃权那种）。
            score = row.get("score") or row.get("map_score") or "-"
            if wide:
                lines.append(f"| {row.get('date') or '-'} | {clock} | {vs} | {score} |")
            else:
                lines.append(f"| {clock}{f' {zone}' if zone else ''} | {vs} | {score} |")
        if ev.get("pending"):
            pend = "、".join(str(d)[5:] for d in ev["pending"])
            lines.append(f"_（另有 {len(ev['pending'])} 场已排期、对阵待定（{pend}））_")

    if data.get("skipped"):
        lines.append("")
        lines.append(f"_（当天还有 {data['skipped']} 个赛事没展开）_")

    zones = [
        t for t in dict.fromkeys(
            str(row.get("tz") or "") for ev in events for row in (ev.get("matches") or [])
        ) if t
    ]
    if zones:
        lines.append(
            "_（时区：" + "、".join(_TZ_GLOSS.get(t.lower(), t) for t in zones)
            + "——是**赛事当地时间**）_"
        )
    lines.extend(f"> {n}" for n in notes)
    return "\n".join(lines)


# ======================================================================
# 取数编排（给工具层用）
# ======================================================================
#
# 工具层只负责「读参数 → 调这里 → 把返回的 kwargs 交给 format_*」，
# 解析细节全部留在本模块，测试也可以脱离框架直接跑。

#: 中文/口语说法 → Liquipedia 上的英文赛事名基干。
#:
#: Liquipedia 是英文 wiki，拿中文去搜一律落空。这里只映射**基干**，
#: 年份由用户说法里提取（「TI 2025」），或者由搜索结果的年份兜底。
TOURNAMENT_ALIASES: dict[str, str] = {
    "ti": "The International",
    "ti国际邀请赛": "The International",
    "国际邀请赛": "The International",
    "国际邀请赛ti": "The International",
    "ewc": "Esports World Cup",
    "电竞世界杯": "Esports World Cup",
    "利雅得大师赛": "Esports World Cup",
    "利雅得": "Esports World Cup",
    "世界杯": "Esports World Cup",
    "dreamleague": "DreamLeague Season",
    "梦幻联赛": "DreamLeague Season",
    "eslone": "ESL One",
    "esl": "ESL One",
    "esl挑战赛": "ESL One",
    "blast": "BLAST Slam",
    "blastslam": "BLAST Slam",
    "pgl": "PGL",
    "震中杯": "EPICENTER",
    "基辅major": "Kiev Major",
    "北京major": "Beijing Major",
    "major": "Major",
    "dpc": "DPC",
    "亚运会": "Asian Games",
    "石油杯": "Esports World Cup",
}

#: 中文/口语说法 → Liquipedia 战队页标题（只收昵称，缩写交给 OpenDota 队伍索引去匹配）。
TEAM_ALIASES: dict[str, str] = {
    "液体": "Team Liquid",
    "李逵": "Team Liquid",
    "tl": "Team Liquid",
    "雪碧": "Team Spirit",
    "绿龙": "Team Spirit",
    "ts": "Team Spirit",
    "秘密": "Team Secret",
    "石头人": "Tundra Esports",
    "冻土": "Tundra Esports",
    "法老": "Team Falcons",
    "绿鹰": "Team Falcons",
    "猎鹰": "Team Falcons",
    "绿宝石": "Team Falcons",
    "navi": "Natus Vincere",
    "纳威": "Natus Vincere",
    "熊": "Virtus.pro",
    "熊队": "Virtus.pro",
    "vp": "Virtus.pro",
    "xg": "Xtreme Gaming",
    "茶队": "Xtreme Gaming",
    "aster": "Team Aster",
    "星辰": "Team Aster",
    "bb": "BB Team",
    "欧若拉": "Aurora Gaming",
    "aurora": "Aurora Gaming",
    "石头": "Tundra Esports",
}

#: 子页面名里带这些词才值得多花一次请求（赛事把赛程与积分榜放在这些子页里）。
_STAGE_HINTS: tuple[str, ...] = (
    "group",
    "stage",
    "swiss",
    "main event",
    "playoff",
    "bracket",
    "standings",
    "results",
    "final",
)

#: 标题里**不改变指向**的修饰词。
#:
#: 实测：``DreamLeague Season 30`` 在 wiki 上的页面就叫 ``DreamLeague/30`` ——
#: 差的正是这个 ``Season``。这是**同一条赛事的两种写法**，不是两个赛事，
#: 所以比对时把它剔掉；而 ``International`` / ``World`` / ``Cup`` 这类
#: 决定指向的词一个都不能剔。
_TITLE_STOPWORDS: frozenset[str] = frozenset(
    {"the", "season", "series", "presented", "by", "edition", "of"}
)

#: 用户口语里的**赛区词** → Liquipedia 赛事子页面名里用的英文区域名。
#:
#: 为什么必须单列：中国区预选赛的页面叫 ``DreamLeague/30/China`` ——
#: 「中国区」这三个字**只出现在这一层**。别名表里原本一个区域词都没有
#: （报告根因 2），于是「梦幻联赛 S30 中国区预选赛」这条最自然的说法，
#: 三条路（直连 / 搜索 / 子页）全断。
#:
#: 英文词一律**按词边界**匹配（见 :func:`_region_match`），所以 ``cn`` / ``sea``
#: 这种短词不会被 ``Season`` 里的 ``sea`` 骗到。
_REGION_WORDS: dict[str, str] = {
    "中国区": "China", "中国": "China", "国内": "China",
    "china": "China", "cn": "China",
    "东南亚": "Southeast Asia", "southeast asia": "Southeast Asia", "sea": "Southeast Asia",
    "东欧": "Eastern Europe", "eastern europe": "Eastern Europe", "eeu": "Eastern Europe",
    "西欧": "Western Europe", "western europe": "Western Europe", "weu": "Western Europe",
    "南美": "South America", "南美洲": "South America", "south america": "South America",
    "北美": "North America", "north america": "North America",
    "中东": "Middle East", "middle east": "Middle East",
    "独联体": "CIS", "cis": "CIS",
}

#: 「预选赛」这类**不指向具体赛事**的通用词。
#:
#: 用户说「中国区预选赛」而没说哪个赛事时，拿这几个字去搜 Liquipedia 只会搜到
#: 一堆无关页面（真机实测：``China Qualifier`` 落到了 **2017 年**的
#: ``ROG MASTERS/2017/China Qualifier``）。把通用词摘掉后如果什么都不剩，
#: 就该直说「需要知道是哪个赛事的预选赛」，而不是拿一个九年前的赛事充数。
_GENERIC_QUALIFIERS: tuple[str, ...] = (
    "封闭预选赛", "公开预选赛", "封闭预选", "公开预选", "预选赛", "预选",
    "closed qualifier", "open qualifier", "closed qualifiers", "open qualifiers",
    "qualifiers", "qualifier", "正赛",
)

#: 赛季写法（在**归一化**后的尾巴上匹配）：``s30`` / ``season30``。
_SEASON_RE = re.compile(r"(?:^|[^a-z0-9])(?:s|season)(\d{1,2})(?![0-9])")
#: 中文赛季写法：``第 30 赛季`` / ``30 赛季``。
_SEASON_CN_RE = re.compile(r"(?:第\s*)?(\d{1,2})\s*赛季")
#: 罗马数字赛季：``BLAST SLAM IX`` / ``TI X``。Liquipedia 页面名用的是阿拉伯数字
#: （``BLAST/SLAM/9``），不换这一下，「BLAST SLAM IX 中国区」就少了赛季号。
_ROMAN_SEASONS: dict[str, int] = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6,
    "vii": 7, "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12,
}
#: 四位数一律当**年份**，不当赛季（``TI 2026`` 的 2026 不是「第 2026 赛季」）。
_YEAR_LIKE_RE = re.compile(r"(?:19|20)\d{2}")


def _region_match(text: str) -> tuple[str, int, int]:
    """在 ``text`` 里找**最长**的区域词，返回 ``(英文区域名, 起, 止)``。

    取最长是为了让「中国区」赢过「中国」（两级都要能吃住，但结果一致即可）；
    找不到返回 ``("", 0, 0)``。
    """
    raw = text or ""
    low = raw.lower()
    best = ("", 0, 0)
    for word, region in _REGION_WORDS.items():
        if any("\u4e00" <= ch <= "\u9fff" for ch in word):
            idx = raw.find(word)
            if idx < 0:
                continue
            span = (region, idx, idx + len(word))
        else:
            m = re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", low)
            if not m:
                continue
            span = (region, m.start(), m.end())
        if span[2] - span[1] > best[2] - best[1]:
            best = span
    return best


def _find_region(text: str) -> str:
    """``text`` 里的区域名（没有则空串）。"""
    return _region_match(text)[0]


def _season_of_text(tail: str) -> str:
    """从「基干之后剩下的那截」里认出赛季号，返回**十进制字符串**（认不出空串）。

    ``s30`` / ``season 30`` / ``30 赛季`` / ``ix`` / 光秃秃一个 ``30`` 都认。
    **四位数一律不算赛季**（那是年份，由 :func:`_extract_year` 管）。
    """
    text = (tail or "").strip()
    if not text:
        return ""
    m = _SEASON_RE.search(text) or _SEASON_CN_RE.search(text)
    if m:
        return str(int(m.group(1)))
    m = re.fullmatch(r"(\d{1,3})", text)
    if m and not _YEAR_LIKE_RE.fullmatch(m.group(1)):
        return str(int(m.group(1)))
    m = re.fullmatch(r"([ivx]{1,4})", text.lower())
    if m and m.group(1) in _ROMAN_SEASONS:
        return str(_ROMAN_SEASONS[m.group(1)])
    return ""


def _season_of_title(title: str) -> int:
    """页面标题里的赛季号（``DreamLeague/30`` → 30、``DreamLeague/Season 20`` → 20）。

    没有这一项时，排序只看年份；而 ``DreamLeague/30`` 这种**用赛季号命名**的页面
    一个四位数都没有 ⇒ 年份一律是 0，排序完全交给搜索结果的原始顺序 ——
    这就是「梦幻联赛」挑到 ``DreamLeague/Season 20`` 的一半原因（报告根因 4）。

    注意 ``/`` 之后必须是**整个尾段**才算：``…/Open Qualifier 1`` 里的 ``1``
    是场次不是赛季，所以要求尾段形态是 ``<数字>`` 或 ``Season <数字>``。
    """
    seg = (title or "").strip().rsplit("/", 1)[-1].strip().lower()
    if _YEAR_LIKE_RE.fullmatch(seg):
        return 0
    m = re.fullmatch(r"(?:season\s*)?(\d{1,3})", seg)
    return int(m.group(1)) if m else 0


def _alias_hit(text: str) -> tuple[str, str]:
    """中文 / 缩写说法 → ``(命中的原键, 英文基干)``；没命中返回 ``("", "")``。

    **最长键优先 + 只认前缀**，两条都是必须的：

    * 最长优先：``BLAST SLAM IX`` 同时命中 ``blast`` 与 ``blastslam``，
      不按长度排会得到「BLAST Slam Slam IX」这种脏基干；
    * 只认前缀：改成「包含匹配」的话，``Berlin Major 2023`` 里的 ``major``
      会把基干改写成 ``Major``、把 ``Berlin`` 整块丢掉（搜出来的页就不对了）。
      而真正需要放宽的场景（``梦幻联赛s30``）**本来就是前缀**，尾巴正好是
      赛季号 —— 前缀 + 尾巴解析已经覆盖，不必再放宽成包含。
    """
    norm = _norm(text)
    if not norm:
        return "", ""
    for key in sorted(TOURNAMENT_ALIASES, key=lambda k: -len(_norm(k))):
        nk = _norm(key)
        if nk and norm.startswith(nk):
            return key, TOURNAMENT_ALIASES[key]
    return "", ""


def _search_stem(base: str, year: str, season: str) -> str:
    """基干 + 赛季/年份 → 搜索词。

    ⚠️ **年份不能重复拼**（2026-10-14 真机事故）：旧版 ``tournament_alias`` 会把
    年份拼进基干，调用方再补一次，于是发出 ``The International 2026 2026``
    这种脏搜索词。现在拼装只在这一处发生。
    """
    if season:
        return f"{base} {season}"
    if year and year not in base:
        return f"{base} {year}"
    return base


def _norm(text: str) -> str:
    """归一化：去空格 / 下划线 / 点 / 连字符 / **斜杠**，转小写，便于比对。

    ⚠️ **斜杠必须一起归一化**（2026-10-14 修，真机事故）。Liquipedia 的赛事页
    用的是「基干/年份」这种层级写法（``The International/2026``、
    ``DreamLeague/30``、``Esports World Cup/2026``），而别名表和用户输入用空格
    （``The International 2026``）。少了斜杠这一项，**基干一旦带上年份，前缀匹配
    就永远对不上** —— 斜杠正好卡在年份前面，``theinternational2026`` 与
    ``theinternational/2026`` 互不匹配。

    真机现场：群里问「TI 2026」，搜索**已经返回了** ``The International/2026``
    （这条在站点返回里排第一），却因为这一步被判成「没找到」，
    用户看到的是「没在 Liquipedia 上找到赛事」。
    """
    return re.sub(r"[\s_.\-·・/\\]+", "", (text or "").strip().lower())


#: 队名里的**括号注释**（``ZENITH (stack)``、``Team X（临时阵容）``）。
_PAREN_NOTE_RE = re.compile(r"[\(（][^\)）]*[\)）]")


def _norm_loose(text: str) -> str:
    """比 :func:`_norm` 再宽一档：先把括号注释整块丢掉再归一化。

    实测（2026-10-16，真联赛 20359 = DreamLeague S30 的全球预选赛池）：赛事页把
    参赛队写成 ``ZENITH (stack)``，OpenDota 写成 ``ZENITH`` —— **同一支队，两种
    写法**。用严格归一化做「两支队都在参赛名单里」的判断时，这支队在这个赛事下
    的**全部 7 个系列赛**都被判成「不是本赛事的」整块丢掉（实测：8 场 → 15 场）。
    括号里是消歧 / 临时阵容说明，不改变「这是哪支队」，所以这一档必须丢掉。

    只用于**队名**比对（赛区切分、两源合并），不要用于页面标题比对 ——
    标题里的括号常常是有意义的区分（``BLAST/SLAM/9 (China)``）。
    """
    return _norm(_PAREN_NOTE_RE.sub("", text or ""))


def _title_tokens(text: str) -> frozenset[str]:
    """把标题切成「实义词」集合，用来兜住 wiki 与口语之间的写法差。

    切词只按「非字母数字」切，**不从中间拆词** —— ``DreamLeague`` 是一个词元，
    不会拆成 ``dream`` + ``league``（拆了就满盘误命中）。单字母词元直接丢掉
    （标题里的 ``1`` / ``a`` 基本都是噪声），再剔掉
    :data:`_TITLE_STOPWORDS` 那批不改变指向的修饰词。
    """
    words = re.findall(r"[a-z0-9\u4e00-\u9fff]+", (text or "").lower())
    return frozenset(w for w in words if len(w) > 1 and w not in _TITLE_STOPWORDS)


def _tokens_by_subset(cands: list[str], base: str) -> list[str]:
    """兜底匹配：**基干的实义词全部出现在候选标题里**才算命中。

    只在 :func:`pick_page_title` 的前两级（前缀 / 包含）都没命中时才启用，
    它比那两级宽松，所以必须有更严的门槛：

    * 要求基干至少有 **2 个**实义词 —— 单词元基干（``TI`` 这种）一放宽就满屏
      误命中，宁可让它走「没找到」；
    * 要求的是**子集**而不是「有交集」—— 有交集会让 ``LGD`` 这类短名
      把一堆无关页面都捞进来。

    为什么需要它：``DreamLeague Season 30`` 与 ``DreamLeague/30`` 去掉修饰词后
    实义词完全相同，但**谁也不是谁的前缀 / 子串**，前两级一律够不着。
    """
    want = _title_tokens(base)
    if len(want) < 2:
        return []
    return [h for h in cands if want <= _title_tokens(h)]


def team_alias(query: str) -> str:
    """战队名的中文昵称 → Liquipedia / OpenDota 上的英文名（没有对照则原样返回）。

    **两边都必须用它**：OpenDota 的队伍索引只有英文名与英文简称，拿「雪碧」
    去查必然落空 —— 真机第一次验收就是这么整块丢掉 OpenDota 数据的
    （只剩赛事 wiki 那半截，偏偏那时还被限流，等于什么都没答上来）。
    """
    text = (query or "").strip()
    if not text:
        return ""
    return TEAM_ALIASES.get(_norm(text)) or text


def tournament_parts(query: str) -> tuple[str, str, str, str]:
    """把用户说法拆成 ``(英文基干, 年份, 赛季号, 赛区)`` —— 四项都可能为空串。

    这是**唯一**的口语 → 页面线索的解析入口（旧版 ``tournament_alias`` 只认整串
    精确匹配，多打一个 ``s30`` 就全废 —— 用户前两天连试三次失败的直接原因）。拆解顺序：

    1. **年份**：``TI 2026`` → ``2026``（四位数）；
    2. **赛区**：找出最长的区域词并从原话里摘掉（``梦幻联赛 S30 **中国区**预选赛``）；
    3. **通用词**：摘掉「预选赛 / 公开预选 / Qualifier」这类不指向具体赛事的词；
    4. **基干**：剩下的部分按别名表**最长键 + 前缀**匹配；匹配不上就原样当基干
       （英文全名走的就是这条路，``The International 2026`` 不该被改写成别的）；
    5. **赛季**：别名键之后剩下的尾巴里找 ``s30`` / ``30 赛季`` / 罗马数字 / 光秃秃数字。

    摘掉区域与通用词是必须的：``中国区 梦幻联赛`` 这种语序（区域在前）会挡住前缀匹配。
    """
    text = (query or "").strip()
    if not text:
        return "", "", "", ""
    year = _extract_year(text)

    region, start, end = _region_match(text)
    core = (text[:start] + " " + text[end:]).strip() if region else text
    for word in _GENERIC_QUALIFIERS:
        if any("\u4e00" <= ch <= "\u9fff" for ch in word):
            core = core.replace(word, " ")
        else:
            core = re.sub(
                r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", " ", core, flags=re.I
            )
    core = re.sub(r"\s+", " ", core).strip(" ,，、-—–/\\").strip()

    if not core:
        # 只剩「中国区预选赛」这种 —— 没有赛事基干，交给调用方去问清楚。
        return "", year, "", region

    key, base = _alias_hit(core)
    if not key:
        return core, year, "", region
    tail = _norm(core)[len(_norm(key)):]
    return base, year, _season_of_text(tail), region


def tournament_alias(query: str) -> str:
    """赛事名的中文 / 缩写 → Liquipedia 上的英文名（没有对照则原样返回）。

    带年份的说法（「TI 2026」「利雅得大师赛 2026」）也能认。现在是
    :func:`tournament_parts` 的薄壳：基干来自别名表，年份/赛季拼回去。
    **保留它是因为旧调用点与测试都在用** —— 新代码请直接用 :func:`tournament_parts`，
    那边把「年份 / 赛季 / 赛区」分开给出，调用方不必再从字符串里反解。
    """
    base, year, season, _region = tournament_parts(query)
    if not base:
        return (query or "").strip()
    if season:
        return f"{base} {season}"
    if year and year not in base:
        return f"{base} {year}"
    return base


def _extract_year(text: str) -> str:
    m = re.search(r"(19|20)\d{2}", text or "")
    return m.group(0) if m else ""


def pick_page_title(
    hits: list[str], base: str, year: str = "", *, must_include: str = ""
) -> str:
    """从搜索结果里挑出最像目标页面的那个标题；**挑不出来就返回空串**。

    规则（三级，逐级放宽，前一级命中就不往下一级走）：

    1. **以基干开头**（`_norm` 之后比对，斜杠与空格等价 ⇒
       ``The International 2026`` 认得 ``The International/2026``）；
    2. **包含基干**（``LGD`` → ``PSG.LGD``，这种页面不以基干开头）；
    3. **实义词全中**（:func:`_tokens_by_subset`）—— 治的是「两种写法谁也不含谁」
       的那类，``DreamLeague Season 30`` ↔ ``DreamLeague/30``。

    给了年份就优先含该年份的；最后按 **(年份, 赛季号, 主页面优先)** 取大。
    赛季号那一档是 2026-10-16 补的：``DreamLeague/30`` 这种用赛季号命名的页面
    没有任何四位数，只看年份就会与 ``DreamLeague/Season 20`` 并列，排序退化成
    搜索结果的原始顺序 —— 这正是「梦幻联赛」挑到 20 赛季的原因（报告根因 4）。

    ``must_include``：**必须包含**的一截（赛区词，如 ``China``）。用户点名了赛区时
    用得上 —— 主赛事页 ``DreamLeague/30`` 与赛区子页 ``DreamLeague/30/China``
    都能通过前三级的比对，而按「主页面优先」主赛事会赢，于是「中国区预选赛」
    又被答成「正赛」。点名了赛区就以赛区为准。

    有一条**刻意的拒绝**：基干一条都对不上时返回空串，**不拿搜索结果第一条
    凑数**。实测踩过：搜一个根本不存在的赛事，旧逻辑会把「The International
    2026」当成答案渲染出来 —— 用户问 A、插件一本正经地答 B，比说「没找到」糟得多。
    """
    cands = [h for h in (hits or []) if str(h).strip()]
    if not cands:
        return ""
    if must_include:
        want = _norm(must_include)
        cands = [h for h in cands if want and want in _norm(h)]
        if not cands:
            return ""
    low = _norm(base)
    pool = cands
    if low:
        prefixed = [h for h in cands if _norm(h).startswith(low)]
        if prefixed:
            pool = prefixed
        elif len(low) >= 3:
            pool = [h for h in cands if low in _norm(h)]
            if not pool:
                pool = _tokens_by_subset(cands, base)
        else:
            pool = []
        if not pool:
            return ""
    if year:
        with_year = [h for h in pool if year in h]
        if with_year:
            pool = with_year

    def _year_of(title: str) -> int:
        m = re.search(r"(20\d{2})", title)
        return int(m.group(1)) if m else 0

    # 「同一基干的年份 / 赛季页面」优先于「子页面」（子页面名字更长、常带斜杠）
    pool = sorted(
        pool,
        key=lambda t: (_year_of(t), _season_of_title(t), -t.count("/")),
        reverse=True,
    )
    return pool[0]


async def resolve_tournament_page(client: "LiquipediaClient", query: str) -> tuple[str, str]:
    """把「TI」「利雅得大师赛」「The International 2025」解析成赛事页面标题。

    返回 ``(页面名, 说明)``；页面名为空表示没找到（说明里带原因，直接给模型看）。

    2026-10-16 起这里多带一层**赛区**处理，分三级：

    1. **直连**：用户给的是页面级写法（含 ``/`` 或本身就是基干）时先直取一次；
    2. **搜索**：``基干 + 赛季/年份 + 赛区``，并**要求落到的页标题里含该赛区**；
    3. **退到主赛事页再翻子页**：第一轮没直接命中赛区页时先落主赛事页，
       再从它的子页里挑出该赛区（``DreamLeague/30`` → ``DreamLeague/30/China``）。

    第 3 级是本次修复的关键一步：旧实现即使拿到了 16 个赛区子页列表，
    也会被 ``_STAGE_HINTS`` 那把筛子**全扔掉**，然后对用户说「没找到」——
    而其实只差一句「这个赛事有中国区 / 东南亚 等赛区，你要看哪个」。
    """
    q = (query or "").strip()
    if not q:
        return "", "需要给出赛事名。"
    base, year, season, region = tournament_parts(q)

    if not base:
        if region:
            return "", (
                f"「{q}」里只看到赛区（{region}），没有赛事名 —— 得先知道是**哪个赛事**的"
                "预选赛（例如「梦幻联赛 S30 中国区」）。"
            )
        return "", f"没在 Liquipedia 上找到赛事「{q}」对应的页面。"

    # 直连：用户给的是页面级写法时才做。
    # 走了别名说明给的是中文/缩写，拿它当页面名必然落空（白花一次请求）；
    # 但带 ``/`` 的（``DreamLeague/30/China``）就是页面名本身，值得先试一下。
    page_like = base == q or "/" in q
    if page_like and (not region or _norm(region) in _norm(q)):
        direct = q.replace("_", " ")
        text = await client.wikitext(direct)
        if text and "Infobox league" in text:
            return direct, ""

    # 年份不能重复拼（2026-10-14 真机事故）：拼装统一交给 :func:`_search_stem`。
    stem = _search_stem(base, year, season)
    terms: list[str] = [f"{stem} {region}", stem] if region else [stem]
    picked = ""
    for term in dict.fromkeys(terms):
        hits = await client.search(term, limit=10)
        picked = pick_page_title(hits, stem, year, must_include=region)
        if picked:
            break

    # 只给了**系列名**（「梦幻联赛」「PGL」）而没给赛季 / 年份时，站点搜索落到的
    # 往往是老命名的赛季页（真机：「梦幻联赛」→ ``DreamLeague/Season 23``，
    # 2021 年的那一届），而当前赛季叫 ``DreamLeague/30``。改用**系列主页的子页
    # 列表**挑最新一届，并把「这是按最新一届解析的」说给模型听。
    series_only = not region and not season and not year
    if series_only and (not picked or "season" in picked.lower()):
        latest = await _pick_from_series(client, q, year)
        if latest:
            return latest, (
                f"「{q}」没有说赛季，这里按**最新一届**（{latest}）解析。"
                "用户要的若是别的届次，让他带上赛季号（如「梦幻联赛 S30」）。"
            )
    if picked:
        return picked, ""

    if region:
        # 退一步：先落主赛事页，再从它的子页里挑这个赛区。
        try:
            hits = await client.search(stem, limit=10)
        except EsportsError as e:
            return "", f"搜索赛事「{q}」失败（{e}）。"
        parent = pick_page_title(hits, stem, year)
        if parent:
            try:
                subs = await client.subpages(parent, limit=100)
            except EsportsError:
                subs = []
            sub = _region_subpage(subs, region)
            if sub:
                return sub, ""
            available = _region_subpages(subs)
            if available:
                return "", (
                    f"「{q}」没有直接匹配到页面；该赛事（{parent}）下面有这些赛区："
                    + "、".join(sorted(available))
                    + "。先向用户确认要看哪个赛区，或让他给更完整的赛事名。"
                )
            return "", f"在 Liquipedia 上没找到「{parent}」的「{region}」子页面。"

    if series_only:
        latest = await _pick_from_series(client, q, year)
        if latest:
            return latest, (
                f"「{q}」没找到直接对应的页面，这里按**最新一届**（{latest}）解析。"
                "用户要的若是别的届次，让他带上赛季号（如「梦幻联赛 S30」）。"
            )
    return "", f"没在 Liquipedia 上找到赛事「{q}」对应的页面。"


async def _pick_from_series(
    client: "LiquipediaClient", query: str, year: str = ""
) -> str:
    """只给了系列名时，从**系列主页的子页面列表**里挑最新的一届。

    为什么不能只靠搜索：``DreamLeague`` 这个系列主页下挂着 ``/27`` ``/28``
    ``/29`` ``/30`` 与 ``/Season 20`` ``/Season 23`` 两套命名，而站点的相关性
    排序把老命名的几页排在前面 —— 于是「梦幻联赛」永远答成 2021 年的那一届。
    子页列表是**完整**的，按赛季号取最大即可。

    匹配基干时要把 ``Season`` 摘掉（``DreamLeague Season`` → ``DreamLeague``）：
    主赛事页叫 ``DreamLeague/30``，**不含** ``Season`` 这个词，带着它做前缀匹配
    只会命中 ``DreamLeague/Season 20`` 那一套老页面，等于没改。

    拿不到系列主页（或它没有子页）就返回空串，调用方保留原来的落页结果 ——
    这条是**锦上添花**，绝不允许它把已经对上的答案带偏。
    """
    key, base = _alias_hit(query)
    if not key:
        return ""
    series = re.sub(r"\s*(?:season|series)\s*$", "", base, flags=re.I).strip() or base
    try:
        hits = await client.search(series, limit=10)
    except EsportsError:
        return ""
    want = _norm(series)
    parent = next((h for h in hits if "/" not in h and _norm(h) == want), "")
    if not parent:
        parent = next(
            (h for h in hits if "/" not in h and _norm(h).startswith(want)), ""
        )
    if not parent:
        return ""
    try:
        subs = await client.subpages(parent, limit=200)
    except EsportsError:
        return ""
    numbered = [s for s in subs if _season_of_title(s)]
    if not numbered:
        return ""
    return pick_page_title(numbered, series, year)


def _region_subpage(titles: list[str], region: str) -> str:
    """子页列表里含该赛区的那个（没有则空串）。"""
    want = _norm(region)
    if not want:
        return ""
    for title in titles or []:
        if want in _norm(title):
            return title
    return ""


def _region_subpages(titles: list[str]) -> set[str]:
    """子页列表里出现的**赛区**（用于「这个赛事有哪几个赛区」这句话）。

    只看标题的**最后一段**且要求整段相等 —— ``.../China`` ✓、
    ``.../China/Open Qualifier 1``（尾段是 ``Open Qualifier 1``）✗。
    """
    out: set[str] = set()
    for title in titles or []:
        seg = (title or "").strip().rsplit("/", 1)[-1].strip().lower()
        if not seg:
            continue
        for word, region in _REGION_WORDS.items():
            if any("\u4e00" <= ch <= "\u9fff" for ch in word):
                continue
            if seg == word:
                out.add(region)
                break
    return out


async def resolve_team_page(client: "LiquipediaClient", query: str) -> tuple[str, str]:
    """把战队名解析成 Liquipedia 战队页标题，返回 ``(页面名, 说明)``。"""
    q = (query or "").strip()
    if not q:
        return "", "需要给出战队名。"
    base = team_alias(q)
    text = await client.wikitext(base)
    if text and "Infobox team" in text:
        return base, ""
    hits = await client.search(base, limit=8)
    picked = pick_page_title(hits, base, _extract_year(q))
    if not picked:
        return "", f"没在 Liquipedia 上找到战队「{q}」的页面。"
    if _norm(picked) == _norm(base):
        return picked, ""
    text = await client.wikitext(picked)
    if text and "Infobox team" in text:
        return picked, ""
    return "", f"Liquipedia 上「{picked}」不是战队页。"


def _usable_subpages(titles: list[str]) -> list[str]:
    """从子页面列表里挑出「可能有赛程 / 积分榜」的那些，按关键度排序。

    只留名字命中 :data:`_STAGE_HINTS` 的（``Group Stage`` / ``Main Event`` /
    ``Swiss`` …）。赛事页的子页面里同时有 ``/Broadcast``、``/Talent``、
    ``/Prize Pool`` 这类**没有对阵与积分**的，多取一个就多撞一次限流，
    所以这里**只认关键字**，宁缺毋滥。
    """
    scored: list[tuple[int, str]] = []
    for title in titles or []:
        low = title.lower()
        score = sum(
            2 if hint in ("group", "stage", "swiss", "main event") else 1
            for hint in _STAGE_HINTS
            if hint in low
        )
        if score:
            scored.append((score, title))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [title for _, title in scored]


async def collect_tournament(
    client: "LiquipediaClient",
    query: str,
    *,
    max_subpages: int = 2,
) -> dict[str, Any]:
    """取一个赛事的全部可解析内容，返回值可直接喂给 :func:`format_tournament`。

    请求预算（Liquipedia query 车道 1 次 / 2 秒）：搜索 1 + 主页 1 +（奖池子页 ≤1）
    + 子页列举 ≤1 + 子页正文 ≤2 ⇒ 最多约 6 次、十几秒，在工具超时之内。
    """
    result: dict[str, Any] = {
        "query": query,
        "page": "",
        "info": {},
        "teams": [],
        "slots": [],
        "tables": [],
        "swiss": {},
        "matches": [],
        "pending_dates": [],
        "regions": [],
        "leagueid": "",
        "notes": [],
    }
    page, note = await resolve_tournament_page(client, query)
    if not page:
        result["notes"].append(note or "没找到赛事页面。")
        return result
    result["page"] = page
    # ⚠️ **落页成功时也要带说明**（2026-10-16 实机验收抓到）。说明是「这个页是
    # 怎么挑出来的」：只给系列名（「梦幻联赛」）时按**最新一届**解析，落到的是
    # ``DreamLeague/32``（2027 年那届）—— 不把这句话交给模型，用户会把它当成
    # 自己问的那一届。原来 note 只在 ``not page`` 那条路上被用，落页成功时静默
    # 丢掉，于是解析层写好的说明**一条都到不了用户眼前**。
    if note:
        result["notes"].append(note)

    # ⚠️ 取主页正文要**兜住限流**（P1-7，2026-10-14 审查）。
    #
    # 原来 ``await client.wikitext(page)`` 是裸的：第一次 429 会一路冒到
    # ``dota_tools._esports_guard`` → 返回 ``(None, note)`` → 调用方看到
    # ``data["page"]`` 为空，于是回一句**「没查到赛事「X」的页面。」**。
    #
    # 这句话把「站点在限流（等一会儿就有）」说成了「这个赛事不存在」——
    # 正是本仓库反复强调不许混的那两件事，而且此时 ``page`` 明明已经解析
    # 出来了（就在上面一行），连页面标题都被丢掉了。
    #
    # 现在：限流/不可用 ⇒ 保留已解析出的 ``page``，把它记进 notes 交给渲染层，
    # 让用户看到「这个赛事页存在，但内容现在取不到」。
    try:
        wikitext = await client.wikitext(page)
    except EsportsError as e:
        result["notes"].append(
            f"赛事页「{page}」已找到，但正文这次没取到（{e}）。"
            "这是**这一块暂时取不到**，不是「没有这个赛事」。"
        )
        return result
    if not wikitext:
        result["notes"].append("赛事页面内容为空（可能不存在，或站点暂时不可用）。")
        return result

    info = parse_infobox(wikitext)
    teams = parse_team_participants(wikitext)
    slots = parse_prize_slots(wikitext)
    tables = parse_group_tables(wikitext)
    swiss = parse_swiss_zones(wikitext)
    matches = parse_matches(wikitext)

    pool = await resolve_prize_pool(client, info)
    if pool:
        info["prizepoolusd"] = pool
    elif info.get("_prizepool_raw") and (info.get("prizepoolusd") or "").strip():
        # 页面上**写了**奖池，但两条数字正则都没吃下来（带单位 / 脚注等写法），
        # 且子页面这条回退路也没走通 ⇒ 如实说「这里不给数字」，绝不回退成原文
        # （P1-8）。与下面那条「写在子页面里」分开措辞，否则排查时会指错方向。
        info["prizepoolusd"] = ""
        result["notes"].append(
            "奖池这一栏在页面上有内容，但不是可直接读取的纯数字"
            "（可能带单位 / 脚注或写在子页面里），这里不给具体金额。"
        )
    elif info.get("_prizepool_raw"):
        info["prizepoolusd"] = ""
        result["notes"].append("奖池金额写在单独的子页面里且没取到，这里不给具体数字。")

    # 赛程 / 积分榜常在子页面。只有「主页里没拿到」才去多花请求。
    #
    # ⚠️ 这一段是 P1-7 里最容易丢东西的地方：``info / teams / slots / tables``
    # **在上面已经全部解析好了**，原先一句裸抛的 ``client.wikitext(sub)``
    # 就能把整批成果连同「已找到页面」的事实一起丢掉，用户只看到
    # 「没查到赛事「X」的页面。」现在每一次取正文都单独兜住，取到多少算多少。
    if max_subpages > 0 and (not matches or not tables):
        try:
            subs = await client.subpages(page, limit=100)
        except EsportsError as e:
            subs = []
            result["notes"].append(f"子页面列举失败（{e}）。")

        # ⚠️ **拿到了线索却自己扔掉**，是这条路以前最可惜的一处（报告根因 3）：
        # ``DreamLeague/30`` 下面 16 个子页标题全都取回来了，
        # 然后被 ``_usable_subpages`` 的赛段关键字筛子**全扔掉**，
        # 最后只对模型说一句「没有对阵表」。
        #
        # 这里只把**赛区名字**告诉模型，**不自动取** —— 拿一个别的赛区的 bracket
        # 混进本赛事的赛程，比不取更糟（会把 SEA 的比赛说成中国区的）。
        regions = sorted(_region_subpages(subs))
        if regions:
            result["regions"] = regions
            result["notes"].append(
                "这个赛事下面按**赛区**分了子页面："
                + "、".join(regions)
                + "。上面给的是当前这一页的内容；用户问的是某个赛区的预选赛时，"
                "把赛区名跟赛事名一起说（如「梦幻联赛 S30 中国区」）就能落到那个赛区页。"
            )

        failed_subs: list[str] = []
        for sub in _usable_subpages(subs)[:max_subpages]:
            if sub.replace("_", " ") == page:
                continue
            try:
                sub_wt = await client.wikitext(sub)
            except EsportsError as e:
                # 单个子页取不到不该毁掉另一个子页，更不该毁掉主页成果
                failed_subs.append(f"{sub}（{e}）")
                continue
            if not sub_wt:
                continue
            if not matches:
                matches = parse_matches(sub_wt)
            if not tables:
                tables = parse_group_tables(sub_wt)
            if not swiss.get("zones"):
                swiss = parse_swiss_zones(sub_wt) or swiss
            if matches and tables:
                break
        if failed_subs:
            result["notes"].append(
                "以下子页面这次没取到（可能被限流）：" + "、".join(failed_subs) + "。"
                "主页信息照常给，只是赛程 / 积分榜可能不全。"
            )

    if not teams:
        result["notes"].append("该赛事页没有列出参赛队伍（可能还没抽签 / 页面未更新）。")
    if swiss.get("zones") and not tables:
        result["notes"].append(
            "该赛事用瑞士轮，逐队名次由实时积分表给出（需要渲染页面），"
            "这里只能给到出线区间。"
        )
    # 半骨架（有日期、对手未定）**不能**渲染成「- vs -」的假对阵，
    # 但也不能一字不提 —— 「明天还有几场」本身就是赛程的一部分答案。
    #
    # ⚠️ 措辞必须说「**两个**对手都还没决出」，不能写「这一天没列进表里」：
    # 实机（BLAST 中国区）同一天既有「待定 vs Yakult Brothers」这种**单边已定**
    # 的行进了表（对手写「待定」），也有好几场两边都空的 —— 写「没列进上面的
    # 赛程表」就和表里那一行当场打架。
    pending = pending_match_dates(wikitext)
    if pending:
        result["pending_dates"] = pending
        result["notes"].append(
            "另有已排期、但**两个对手都还没决出**的场次，日期：" + "、".join(pending)
            + "（同一天可能有别的场次已定了对手、列在上面的表里；这些不是没排期）。"
        )
    if not matches:
        result["notes"].append(
            "该赛事页没有可直接解析的对阵表（尚未排期，或对阵在渲染页 / 外部表格里）。"
        )

    result.update(
        {
            "info": info,
            "teams": teams,
            "slots": slots,
            "tables": tables,
            "swiss": swiss,
            "matches": matches,
            "leagueid": str(info.get("leagueid") or "").strip(),
        }
    )
    return result


async def collect_team_page(client: "LiquipediaClient", query: str) -> dict[str, Any]:
    """取战队页面的基本信息与阵容（Liquipedia 侧）。"""
    out: dict[str, Any] = {
        "query": query, "page": "", "info": {}, "roster": [], "inactive": [], "notes": [],
    }
    page, note = await resolve_team_page(client, query)
    if not page:
        out["notes"].append(note or "没找到战队页面。")
        return out
    out["page"] = page
    try:
        wikitext = await client.wikitext(page)
    except EsportsError as e:
        # 同 collect_tournament 的 P1-7：页面找到了就留住这个事实，
        # 别把「限流」讲成「这个队不存在」。对 query_esports_team 尤其重要 ——
        # OpenDota 那半边的评分 / 胜负 / 近期战绩**已经取成功了**，不能因为
        # wiki 这半边取不到就整批丢掉（``if not info and not od`` 那个分支）。
        out["notes"].append(
            f"战队页「{page}」已找到，但正文这次没取到（{e}）。"
            "这是**这一块暂时取不到**，不是「没有这个战队」。"
        )
        return out
    if not wikitext:
        out["notes"].append("战队页面内容为空（可能不存在，或站点暂时不可用）。")
        return out
    parsed = parse_team_page(wikitext)
    out["info"] = parsed.get("info") or {}
    out["roster"] = parsed.get("roster") or []
    out["inactive"] = parsed.get("inactive") or []
    if not out["roster"] and not out["inactive"]:
        # 页面拿到了、但里面没有名单 ⇒ 这必须说清楚，否则模型会自己去猜原因
        # （实测它猜成了「数据源限流」，用户当真了）。措辞要**中性、只讲事实**：
        # 是「这一块没解析出来」，不是「这支队没人」。
        out["notes"].append(
            "战队页抓到了，但页面里没有现役阵容这一块 —— 是这一项拿不到，"
            "不是「这支队没有选手」。"
        )
    return out


async def collect_transfers(
    client: "LiquipediaClient", *, limit: int = 20
) -> list[dict[str, str]]:
    """取转会条目（唯一必须走 ``action=parse`` 的数据）。"""
    html = await client.rendered("Portal:Transfers")
    rows = parse_transfers_html(html)
    if limit and int(limit) > 0:
        return rows[: int(limit)]
    return rows


def filter_transfers(rows: list[dict[str, str]], player: str) -> list[dict[str, str]]:
    """按选手名筛转会条目（归一化后双向包含，忽略大小写与标点）。

    返回空列表既可能「该选手确实没记录」、也可能「只是这段时间没收录」——
    调用方**必须**把这层不确定说清楚，不能替用户断定「他没转会」。
    """
    key = _norm(player)
    if not key:
        return list(rows or [])
    out: list[dict[str, str]] = []
    for row in rows or []:
        name = _norm(str(row.get("player") or ""))
        if name and (key in name or name in key):
            out.append(row)
    return out


# -- OpenDota 侧的纯函数（便于单测，不碰网络） -------------------------------


def _od_team_score(row: dict[str, Any], key: str) -> float:
    """给一行队伍索引打分（``key`` 已归一化）。

    全名精确 4 > 简称精确 3.5 > 全名前缀 3 > 简称前缀 2.5 > 全名包含 2 > 简称包含 1.5。
    """
    name = _norm(str(row.get("name") or ""))
    tag = _norm(str(row.get("tag") or ""))
    if name and name == key:
        return 4.0
    if tag and tag == key and len(key) >= 2:
        return 3.5
    if name and name.startswith(key) and len(key) >= 3:
        return 3.0
    if tag and tag.startswith(key) and len(key) >= 3:
        return 2.5
    if name and key in name and len(key) >= 3:
        return 2.0
    if tag and key in tag and len(key) >= 3:
        return 1.5
    return 0.0


def _od_team_freshness(row: dict[str, Any]) -> tuple[float, float]:
    """「这支队还活着吗」的排序信号：最后一场比赛时间、评分。

    OpenDota 的索引里**同一支队伍常有多个 ``team_id``**（实测 73 组归一化重名：
    ``Team Spirit`` 与 ``Team. Spirit``、``Cloud9`` 与 ``Cloud 9``、``Geek Fam`` 四个 id）。
    死掉的那些 ``last_match_time`` 停在好几年前、``match_id`` 是 ``null``，
    真实在打的那支时间戳是新的 —— 所以按「最后一场比赛时间」排序能把活的那支挑出来。
    """
    def _num(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    return (_num(row.get("last_match_time")), _num(row.get("rating")))


def match_od_team(index: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    """在 OpenDota 队伍索引里按 **名字 / 简称** 找最匹配的一行。

    归一化后比对（忽略大小写与空格/点/连字符），``Team Spirit`` 与 ``TSpirit``
    都能命中「雪碧 / team spirit / tspirit」。低于阈值返回 ``None``（宁可如实说没匹配到）。

    **同分怎么处理**分两种情况，这个区分很关键：

    * **前缀 / 包含型的同分 = 真的指代不明，返回 ``None``**。
      「Team」这个前缀同时命中 Team Spirit 与 Team Liquid，猜一个去查别人更糟。
    * **「完整队名」精确命中的同分 = 同一支队的历史 id，取还在打的那支**。
      曾经这里一律返回 ``None``，后果是**连「Team Spirit」都查不出来** ——
      索引里那支 2019 年就死了的「Team. Spirit」归一化后和它同名，
      于是全世界最有名的队之一被判成「歧义」（真机验收抓到的）。
      破平只用活跃度这一个信号；**若最活跃的也有并列**，仍然算歧义返回 ``None``。
    """
    key = _norm(query)
    if not key:
        return None
    best_score = 0.0
    for row in index or []:
        if isinstance(row, dict):
            best_score = max(best_score, _od_team_score(row, key))
    if best_score < 1.5:
        return None
    tied = [
        row
        for row in index or []
        if isinstance(row, dict) and _od_team_score(row, key) == best_score
    ]
    if len(tied) == 1:
        return tied[0]
    if best_score < 4.0:
        # 前缀 / 包含型同分：同分的是**不同**的队，不许猜
        return None
    freshest = max(_od_team_freshness(row) for row in tied)
    winners = [row for row in tied if _od_team_freshness(row) == freshest]
    return winners[0] if len(winners) == 1 else None


def find_team_in_pro_matches(
    matches: list[dict[str, Any]], *names: str
) -> dict[str, Any] | None:
    """在**最近职业比赛**里按队名找队 ID（``/teams`` 索引覆盖不到时的兜底）。

    为什么需要它：OpenDota 的 ``/teams`` 索引**恰好只有评分最高的 1000 支**
    （实测最低评分 1084），二线队根本不进去 —— 问 Nemiga Gaming 时整块
    OpenDota 数据（评分 / 总胜负 / 阵容 / 近期战绩）全丢，而它在职业比赛里
    明明有记录。``/proMatches`` 的行里带 ``radiant_team_id`` / ``dire_team_id``
    与队名，正好能补上这个映射。

    **只认归一化后的完整队名**（与 :func:`match_od_team` 的「精确档」同口径）：
    这里没有活跃度之类的客观信号可以破平，放宽到前缀/包含就等于猜。
    多个不同的 id 用同一个名字出现时返回 ``None``。
    """
    keys = {_norm(n) for n in names if _norm(n)}
    if not keys:
        return None
    found: dict[int, str] = {}
    for row in matches or []:
        if not isinstance(row, dict):
            continue
        for side in ("radiant", "dire"):
            team_id = row.get(f"{side}_team_id")
            name = str(row.get(f"{side}_name") or "").strip()
            if team_id and name and _norm(name) in keys:
                found.setdefault(int(team_id), name)
    if len(found) != 1:
        return None
    team_id, name = next(iter(found.items()))
    return {"team_id": team_id, "name": name}


def od_roster(players: list[dict[str, Any]], *, limit: int = 12) -> list[dict[str, str]]:
    """OpenDota 现役阵容 → :func:`format_team` 的 ``roster``。

    只保留 ``is_current_team_member`` 为真的（该端点会把历史成员一起返回，
    全量列出会让「阵容」变成一个几十人的花名册）。
    """
    rows: list[dict[str, str]] = []
    for player in players or []:
        if not isinstance(player, dict) or not player.get("is_current_team_member"):
            continue
        name = str(player.get("name") or "").strip()
        if not name:
            continue
        games = player.get("games_played")
        role = f"出场 {games} 场" if isinstance(games, int) and games > 0 else ""
        rows.append({"name": name, "role": role})
    rows.sort(key=lambda r: r["name"].lower())
    return rows[:limit]


def od_recent_rows(
    matches: list[dict[str, Any]], team_id: int, *, limit: int = 10
) -> list[dict[str, str]]:
    """OpenDota 战队比赛 → :func:`format_team` 的 ``recent``。

    胜负必须**按该队在哪一边**换算：``radiant_win`` 说的是天辉赢没赢，
    ``radiant`` 说的是该队是不是天辉。漏掉这一步就会把负局报成胜局。
    """
    rows: list[dict[str, str]] = []
    ordered = sorted(
        (m for m in (matches or []) if isinstance(m, dict)),
        key=lambda m: m.get("start_time") or 0,
        reverse=True,
    )
    for match in ordered[:limit]:
        was_radiant = bool(match.get("radiant"))
        radiant_win = bool(match.get("radiant_win"))
        result = "胜" if was_radiant == radiant_win else "负"
        r_score, d_score = match.get("radiant_score"), match.get("dire_score")
        if isinstance(r_score, int) and isinstance(d_score, int):
            mine, theirs = (r_score, d_score) if was_radiant else (d_score, r_score)
            score = f"{mine}-{theirs}"
        else:
            score = ""
        start = match.get("start_time")
        when = ""
        if isinstance(start, (int, float)) and start > 0:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(start))
        rows.append(
            {
                "date": when,
                # 去首尾空白是**纯格式归一化**：OpenDota 的联赛名真的带尾随空格
                # （实测 ``'EPL Masters 2026 '`` / ``'PARI Universe by FISSURE Closed Qualifiers '``），
                # 原样渲染会出现「EPL Masters 2026 ｜vs」这种双空格，而且
                # 「近期参加赛事」的去重会把同一个联赛当成两个。不改一个字的语义。
                "league": str(match.get("league_name") or "").strip(),
                "opponent": str(match.get("opposing_team_name") or "").strip(),
                "result": result,
                "score": score,
            }
        )
    return rows


def od_pro_match_rows(rows: list[dict[str, Any]], *, limit: int = 20) -> list[dict[str, Any]]:
    """OpenDota ``/proMatches`` → :func:`format_pro_matches` 的输入。"""
    out: list[dict[str, Any]] = []
    for row in (rows or [])[:limit]:
        if not isinstance(row, dict):
            continue
        start = row.get("start_time")
        when = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(start))
            if isinstance(start, (int, float)) and start > 0
            else ""
        )
        radiant = row.get("radiant_name") or row.get("radiant_team") or "-"
        dire = row.get("dire_name") or row.get("dire_team") or "-"
        r_score, d_score = row.get("radiant_score"), row.get("dire_score")
        score = f"{r_score}-{d_score}" if r_score is not None and d_score is not None else ""
        out.append(
            {
                "date": when,
                # 同上：只去首尾空白，语义不动（实测联赛名带尾随空格）。
                "league": str(row.get("league_name") or "").strip(),
                "radiant": str(radiant).strip(),
                "dire": str(dire).strip(),
                "score": score,
                "leagueid": row.get("leagueid"),
                "match_id": row.get("match_id"),
            }
        )
    return out


# ======================================================================
# 跨源拼装：赛事页的「赛程」+ 对局库的「结果」
# ======================================================================
#
# 用户要的四件事里，「是哪个比赛的预选 / 有哪些队」在 Liquipedia，
# 「赛程」Liquipedia 有（未开打就挂着），「结果」只有 OpenDota 有。
# 下面这几个函数把两边拼成同一张表。


def _pair_key(left: Any, right: Any) -> frozenset[str]:
    """对阵的「无向键」：两边队名归一化后的集合（与左右顺序无关）。

    用 :func:`_norm_loose` 而不是 :func:`_norm`：赛事页写 ``ZENITH (stack)``、
    对局库写 ``ZENITH`` —— 不放宽的话同一场会被当成两行（页面那行没有比分、
    库里那行又挂不上），表里就出现「同名两行、一行没比分」。实测见
    :func:`_norm_loose` 的注释。
    """
    a, b = _norm_loose(str(left or "")), _norm_loose(str(right or ""))
    if not a or not b or a == b:
        return frozenset()
    return frozenset((a, b))


def _dates_close(left: Any, right: Any) -> bool:
    """两个 ``YYYY-MM-DD`` 是否相差不超过一天（任一边不是日期时放行）。"""
    try:
        a = time.strptime(str(left), "%Y-%m-%d")
        b = time.strptime(str(right), "%Y-%m-%d")
    except (ValueError, TypeError):
        return True
    return abs(time.mktime(a) - time.mktime(b)) <= 86400 * 1.5


def merge_schedule(
    matches: list[dict[str, Any]], results: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """赛事页赛程 + OpenDota 结果 → **一张表**（按日期时间排序）。

    合并键是「归一化后的队名集合」（与左右顺序无关）+ 日期（允许差一天 ——
    两边的日期口径可能差一个时区）。

    **比分以赛事页为准**（``{{Map}}`` 逐局 winner 数出来的，见
    :func:`_series_from_maps`），赛事页那场没有比分时**才**用对局库兜底。
    上一版是反过来的（对局库覆盖赛事页），在「赛事页自己就数得出」这件事
    被发现之后就站不住了：wiki 更早（志愿者赛后立刻填）、还带 ``matchid``。

    ``source`` 记着这一行的比分**从哪来**（``wiki`` / ``opendota``），
    渲染层据此交代口径 —— 不标的话两种来源的时间口径不同（wiki 带页面时区、
    对局库是插件本地时间）却长得一样。

    返回 ``(行, 只出现在对局库里的结果数)``。后者必须如实交代：不交代的话
    「表里 N 行」会被当成「这个赛事只有 N 场」。
    """
    rows: list[dict[str, Any]] = []
    used: set[int] = set()
    for m in matches or []:
        # 赛事页的比分有两个来源：``score=``（弃权 FF/W）与 ``{{Map}}`` 数出来的。
        # 两者不会同时出现（弃权场没有 Map），取先有的那个。
        wiki_score = str(m.get("score") or m.get("map_score") or "")
        row = {
            "date": m.get("date") or "-",
            "time": m.get("time") or "",
            "tz": m.get("tz") or "",
            "left": m.get("left") or "-",
            "right": m.get("right") or "-",
            "score": wiki_score,
            "source": "wiki" if wiki_score else "",
        }
        key = _pair_key(row["left"], row["right"])
        if key:
            for i, r in enumerate(results or []):
                if i in used or _pair_key(r.get("left"), r.get("right")) != key:
                    continue
                if not _dates_close(row["date"], r.get("date")):
                    continue
                # **wiki 优先。**赛事页已有比分就**不拿对局库覆盖**；只在
                # 赛事页没有时才填上，并把来源记成 opendota。
                if not row["score"] and r.get("score"):
                    row["score"] = r.get("score")
                    row["source"] = "opendota"
                # 时刻仍然只在赛事页没写时才用对局库的（页面带时区，库里是本地时间）。
                row["time"] = row["time"] or (r.get("time") or "")
                row["date"] = row["date"] if row["date"] != "-" else (r.get("date") or "-")
                # 匹配上就算「已用」—— 哪怕比分是 wiki 给的，这条对局库记录也已经
                # 体现在表里了，不能再到 extra 里重复一遍。
                used.add(i)
                break
        rows.append(row)
    extra = 0
    for i, r in enumerate(results or []):
        if i in used:
            continue
        extra += 1
        rows.append(
            {
                "date": r.get("date") or "-",
                "time": r.get("time") or "",
                "tz": "",
                "left": r.get("left") or "-",
                "right": r.get("right") or "-",
                "score": r.get("score") or "",
                # 赛事页没列这一场，比分只能来自对局库。
                "source": "opendota",
            }
        )
    rows.sort(key=lambda r: (str(r.get("date") or "9999"), str(r.get("time") or "99:99")))
    return rows, extra


def od_team_name_index(rows: list[dict[str, Any]]) -> dict[int, str]:
    """``/proMatches`` 的行 → ``team_id → 队名``。

    为什么需要它：``/leagues/{id}/matches`` **队名恒为 null**（实测 253 行里
    非空的有 0 个），只有 ``team_id``；而 ``/proMatches`` 的行两样都带 ——
    「队名」只能从这里换回来。
    """
    out: dict[int, str] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        for side in ("radiant", "dire"):
            tid = row.get(f"{side}_team_id")
            name = str(row.get(f"{side}_name") or "").strip()
            if tid is None or not name:
                continue
            try:
                key = int(tid)
            except (TypeError, ValueError):
                continue
            out.setdefault(key, name)
    return out


def od_league_series(
    matches: list[dict[str, Any]],
    *,
    team_ids: dict[int, str] | None = None,
    team_names: set[str] | None = None,
    since: float = 0.0,
    until: float = 0.0,
) -> list[dict[str, Any]]:
    """``/leagues/{id}/matches`` → **按 series 归并**的「赛程 + 比分」。

    三个实测的坑（报告 §7 方案 C）：

    * 队名恒为 ``null``，只能用 ``team_id`` 去 :func:`od_team_name_index` 换；
    * 同一个 ``leagueid`` 覆盖**全部赛区**（``DreamLeague Season 30 Qualifiers``
      是 SEA / 中国 / 东欧 … 合体），要用赛事页给出的参赛队名单求交集才能切出
      这一块 —— **切不出来就一条都不给**，绝不把别的赛区的对局当成它的；

    切分用的是 :func:`_norm_loose`（丢掉 ``ZENITH (stack)`` 这类括号注释）而不是
    严格归一化：实测严格档会把该队整个漏掉（8 场 → 15 场，见 :func:`_norm_loose`）。
    判定口径是「**两支队都在名单里**」而不是「至少一支在」—— 后者会把
    ``10-12 Imperial Power vs Crescent`` 这种界外的对局也捞进来（实测差 1 场，
    那场在赛事 sdate 之前，只因为给了 ±1 天窗口才落进范围）。
    * 库里只有**打完并且被收录**的对局（实测滞后约 2.4 小时），所以这条路径
      天然只能补「结果」，补不了「尚未开打的赛程」。

    按 ``series_id`` 归并的理由：一个 Bo3 在库里是 3 行，平铺出来「赛程」会变成
    三倍长且看不出谁赢了哪一场；归并后一局系列赛一行、比分是**局分**。
    """
    ids = dict(team_ids or {})
    names = {_norm_loose(n) for n in (team_names or set()) if _norm_loose(n)}
    groups: dict[Any, list[dict[str, Any]]] = {}
    for row in matches or []:
        if not isinstance(row, dict):
            continue
        ts = row.get("start_time")
        if isinstance(ts, (int, float)) and ts > 0:
            if since and ts < since - 86400:
                continue
            if until and ts > until + 86400:
                continue
        try:
            r_id = int(row.get("radiant_team_id") or 0)
            d_id = int(row.get("dire_team_id") or 0)
        except (TypeError, ValueError):
            continue
        left, right = ids.get(r_id, ""), ids.get(d_id, "")
        if not (left and right):
            # 队名换不出来的行一律丢 —— 宁缺毋滥，绝不拿 id 当队名渲染。
            continue
        if names and not {_norm_loose(left), _norm_loose(right)} <= names:
            continue
        groups.setdefault(row.get("series_id") or row.get("match_id"), []).append(row)

    out: list[dict[str, Any]] = []
    for games in groups.values():
        first = games[0]
        try:
            r_id = int(first.get("radiant_team_id") or 0)
            d_id = int(first.get("dire_team_id") or 0)
        except (TypeError, ValueError):
            continue
        wins: dict[int, int] = {}
        for game in games:
            # ⚠️ 不能只在 ``radiant_win`` 为真时记一局 —— 天辉输了的那局，
            # 赢的是**夜魇**，整条跳过等于把这一局的胜利凭空抹掉（实测：一个
            # 1-1 的 Bo2 会被渲染成 1-0）。谁赢就把分记给谁。
            try:
                rid = int(game.get("radiant_team_id") or 0)
                did = int(game.get("dire_team_id") or 0)
            except (TypeError, ValueError):
                continue
            winner = rid if bool(game.get("radiant_win")) else did
            if winner:
                wins[winner] = wins.get(winner, 0) + 1
        stamps = [float(g.get("start_time") or 0) for g in games]
        ts = min(stamps) if stamps else 0.0
        when = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts > 0 else ""
        )
        date, _, clock = when.partition(" ")
        out.append(
            {
                "date": date or "-",
                "time": clock,
                "left": ids.get(r_id, ""),
                "right": ids.get(d_id, ""),
                "score": f"{wins.get(r_id, 0)}-{wins.get(d_id, 0)}",
                "games": len(games),
                "ts": ts,
            }
        )
    out.sort(key=lambda r: r.get("ts") or 0)
    return out
