"""Steam 官方 WebAPI 的录像取址。

为什么要有这个模块
------------------
录像下载地址的固定格式是::

    http://replay{cluster}.valve.net/570/{match_id}_{replay_salt}.dem.bz2

``cluster``（比赛所在服务器集群）与 ``replay_salt``（文件名盐值）都是 Valve
游戏协调服务器下发的比赛元信息。**文件本身托管在 Valve 的 CDN 上**，第三方
站点一份文件都不存，只是转述这两个值。

转述的意愿各不相同：

- **OpenDota** 愿意转述（``/matches/{id}`` 的 ``replay_url``），代价是要等它
  同步 GC 数据 —— 刚打完的比赛常常只有 25 个基础字段，没有 ``replay_url``。
- **STRATZ** 不转述。它的 ``MatchType`` 确实声明了 ``replaySalt``，但实测
  **恒为 null**，连盐值早已确定的老比赛也是 null；它只给 ``clusterId``。
  所以 ``data_source_priority=stratz`` 之后，录像地址是真的断了。

Valve 自己的接口是有的：``IDOTA2Match_570/GetMatchDetails``。官方 WebAPI
FAQ 的原话是「The cluster, match_id and replay_salt are available from the
fields in GetMatchDetails」。配了 ``steam_api_key`` 就走这条 —— 不经过任何
第三方，也不必等谁同步。

⚠️ **但这条路 2026-11-11 实测是断的，原因还不在我们这边。**
``GetMatchDetails`` 自 2024 年 8 月（7.36 版本）起就恒返回 ``HTTP 500`` +
空 body；Valve 的 issue tracker 上有两份报告（#24383、#32617），前者在
2026-07 因 365 天无活动被自动关闭、标记 not planned，后者明确写着「always
returns error code 500 and an empty response」。实测对照：同一个 key 调
``GetMatchHistory`` 正常返回真实比赛数据，调 ``GetMatchDetails`` 两场都是
500 —— 所以不是 key 的问题，也不是网络的问题（虽然 ``api.steampowered.com``
在国内确实经常整段不可达，本机与阿里云北京各重试 6 次全超时）。

代码保留在这一层，等 Valve 修好或用户挂上代理就能直接生效；在此之前，失败
会被 :func:`_note_failure` 计数，连续三次即冷却十五分钟，把取址让回 OpenDota。

域名怎么选
----------
国服由完美世界运营，录像走 ``dota2.com.cn``（CNAME 到阿里云 CDN）；国际服走
``valve.net``（Akamai）。实测同一个对象在两个域名上都能取到（ETag 与
Content-Length 完全一致），所以这里**两个都探一次**、谁先答应用谁，而不是把
「cluster 属于哪个地区」硬编码成一张需要长期维护的表。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from astrbot.api import logger

#: ``GetMatchDetails`` 的固定入口。注意 Valve 的 WebAPI 只用 ``key`` 鉴权，
#: 没有 OAuth，也没有别的门槛 —— 这也意味着 key 会长期躺在配置里。
MATCH_DETAILS_URL = "https://api.steampowered.com/IDOTA2Match_570/GetMatchDetails/v1/"

#: 录像域名候选，按「国内部署下更可能命中」的顺序排列。
REPLAY_HOSTS = ("dota2.com.cn", "valve.net")

#: Valve WebAPI 的礼貌速率是「约每秒一次」（官方 FAQ 自己写的）。
#: 我们只在取不到录像地址时单点查询，天然远低于这个量级，不再额外限流。

#: 一旦 Steam 明确回 403（key 无效），本进程内不再重试 —— 否则每场比赛都会
#: 撞一次墙、刷一条 warning。重启后会重新判断，避免用户中途补填 key 却要等重启。
_auth_rejected = False

#: 连续失败（超时 / 5xx）达到这个次数就进冷却期。
#:
#: 为什么需要它：``api.steampowered.com`` 在国内整段不可达是常态（实测阿里云
#: 北京连续 6 次重试全部超时，本机也一样），而录像取址发生在**每场新比赛**上。
#: 不做冷却的话，每份战报都要先白等一次超时，才轮到 OpenDota 兜底。
#:
#: 冷却期内直接返回空串，一次网络请求都不发。
_FAILURE_THRESHOLD = 3
_COOLDOWN_SECONDS = 900.0

_consecutive_failures = 0
_cooldown_until = 0.0


def _note_failure() -> None:
    """记一次「这次没拿到，且原因是网络/服务端」，累计够了就进冷却。"""
    global _consecutive_failures, _cooldown_until
    _consecutive_failures += 1
    if _consecutive_failures >= _FAILURE_THRESHOLD and _cooldown_until == 0.0:
        _cooldown_until = time.monotonic() + _COOLDOWN_SECONDS
        logger.info(
            f"[dota2] Steam 取址连续失败 {_consecutive_failures} 次，"
            f"暂停 {int(_COOLDOWN_SECONDS / 60)} 分钟（录像地址改由 OpenDota 兜底）"
        )


def _note_success() -> None:
    global _consecutive_failures, _cooldown_until
    _consecutive_failures = 0
    _cooldown_until = 0.0


def build_replay_url(cluster: int, match_id: int, salt: int, host: str) -> str:
    """按 Valve 的固定命名规则拼一个下载地址。"""
    return f"http://replay{int(cluster)}.{host}/570/{int(match_id)}_{int(salt)}.dem.bz2"


async def _probe(client: httpx.AsyncClient, url: str) -> bool:
    """HEAD 探活：2xx 且 Content-Length 大于 0 才算这个域名有这份录像。

    只发 HEAD 不拉正文，一个请求几百字节。要判 Content-Length 是因为录像
    过期后部分 CDN 会回 200 + 空对象，光看状态码会误判成「能下」。
    """
    try:
        resp = await client.head(url)
    except Exception:  # noqa: BLE001 - 探活失败等于「这个域名不行」，不必区分原因
        return False
    if resp.status_code != 200:
        return False
    try:
        return int(resp.headers.get("content-length") or 0) > 0
    except (TypeError, ValueError):
        return False


async def fetch_replay_url(
    match_id: int,
    api_key: str,
    *,
    timeout: float = 10.0,
    proxy: str = "",
) -> str:
    """向 Steam 官方接口要 ``cluster`` / ``replay_salt``，拼出可下载的地址。

    取不到就返回空串 —— 调用方据此回落到 OpenDota。**本函数不抛异常**：
    录像取址是「锦上添花」，它失败不该把复盘或深挖流程一起带走。

    Args:
        match_id: 比赛 ID。
        api_key: Steam WebAPI key（``steamcommunity.com/dev/apikey`` 免费申请）。
        timeout: 单次 HTTP 请求超时（秒）。
        proxy: 可选代理，与 OpenDota / STRATZ 客户端共用同一个配置。

    Returns:
        可下载的 ``.dem.bz2`` 地址；任何一环缺失都返回 ``""``。
    """
    global _auth_rejected

    key = (api_key or "").strip()
    if not key or not match_id or _auth_rejected:
        return ""
    if time.monotonic() < _cooldown_until:
        # 冷却期：上一轮已经连续撞了三次墙，这次直接让 OpenDota 兜底。
        return ""

    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(timeout, connect=min(8.0, timeout)),
        "follow_redirects": True,
        "headers": {
            "User-Agent": "AstrBot-Plugin-Dota2/1.0 (+https://github.com/AstrBotDevs)",
            "Accept": "application/json",
        },
    }
    if proxy:
        kwargs["proxy"] = proxy

    try:
        async with httpx.AsyncClient(**kwargs) as client:
            try:
                resp = await client.get(
                    MATCH_DETAILS_URL,
                    params={"key": key, "match_id": int(match_id)},
                )
            except Exception as e:  # noqa: BLE001 - 网络问题一律当"这次没拿到"
                _note_failure()
                logger.info(
                    f"[dota2] 比赛 {match_id} 向 Steam 取录像信息失败："
                    f"{type(e).__name__}: {e}"
                )
                return ""

            if resp.status_code == 403:
                _auth_rejected = True
                logger.warning(
                    "[dota2] Steam WebAPI 拒绝了 steam_api_key（403），"
                    "本次运行内不再尝试；请到 https://steamcommunity.com/dev/apikey "
                    "核对 key"
                )
                return ""
            if resp.status_code != 200:
                _note_failure()
                logger.info(
                    f"[dota2] 比赛 {match_id} 向 Steam 取录像信息返回 "
                    f"HTTP {resp.status_code}"
                )
                return ""

            try:
                payload = resp.json()
            except ValueError:
                logger.info(f"[dota2] 比赛 {match_id} 的 Steam 响应不是 JSON")
                return ""

            result = (payload or {}).get("result") or {}
            cluster = result.get("cluster")
            salt = result.get("replay_salt")
            if not cluster or not salt:
                # 私密比赛、未结算，或 Valve 把盐值收敛了 —— 都归到这里。
                logger.info(
                    f"[dota2] 比赛 {match_id} 的 Steam 响应里没有 cluster/replay_salt"
                )
                return ""

            urls = [
                build_replay_url(int(cluster), int(match_id), int(salt), host)
                for host in REPLAY_HOSTS
            ]
            # 并发探活：串行的话最坏要等两次超时（一次国服一次国际）。
            verdicts = await asyncio.gather(
                *(_probe(client, url) for url in urls), return_exceptions=True
            )
            for url, ok in zip(urls, verdicts):
                if ok is True:
                    _note_success()
                    return url

            logger.info(
                f"[dota2] 比赛 {match_id} 拼出了地址但 CDN 取不到"
                f"（cluster={cluster}），录像可能已过期"
            )
            return ""
    except Exception as e:  # noqa: BLE001 - 兜底层，绝不把异常抛给业务流程
        logger.info(f"[dota2] 比赛 {match_id} 的 Steam 取址异常：{type(e).__name__}: {e}")
        return ""
