"""STRATZ GraphQL 数据源客户端。

这是插件的**主数据源**：STRATZ 的数据比 OpenDota 更新、更全（自有解析器 +
analytics），并且提供 OpenDota 没有的字段（分位表现 IMP、位置评分等）。

设计要点
--------
1. **对外接口与 :class:`~dota_api.OpenDotaClient` 完全一致** —— 同名方法、
   同样的返回结构。这样 :class:`~dota_api.FallbackDataSource` 才能把两者
   无缝串起来，上层业务代码一行都不用改。
2. **返回结构向 OpenDota「方言」对齐** —— STRATZ 返回的是 GraphQL 驼峰字段
   （``goldPerMinute`` / ``isVictory`` / ``steamAccount.name``…），本模块在
   ``_normalize_*`` 系列函数里把它们映射成 OpenDota 的蛇形字段
   （``gold_per_min`` / ``player_win`` / ``personaname``…）。
   **这是本模块最核心的价值**：所有字段映射都集中在这里，而不是散落到业务层。
3. **只读、不改写业务语义** —— 例如 STRATZ 的 ``isVictory`` 已经正确处理了
   天辉/夜魇，直接映射成 OpenDota 的 ``radiant_win`` 派生结果即可。

已知差异（已在本模块内抹平）
--------------------------
- STRATZ 没有 ``lane_role``；用 ``position``（POSITION_1~5）表达分路，映射到
  ``lane``（SAFE/MID/OFF）与 ``lane_role``（1=优势路/2=中路/3=劣势路）。
- STRATZ 的 ``heroId`` 与 OpenDota 的 ``hero_id`` **编号一致**，可直接复用。
- STRATZ 的 ``startDateTime`` / ``parsedDateTime`` 是 unix 秒，与 OpenDota 相同。
- STRATZ 用 ``gameMode`` 枚举字符串（``TURBO``/``ALL_PICK``…），OpenDota 用
  数字（22=All Pick、23=Turbo）。本模块按 OpenDota 的编号表回填数字。
"""

from __future__ import annotations

import asyncio
import http.client
import json
import urllib.error
import urllib.request
from typing import Any, Callable

from astrbot.api import logger

try:  # 插件目录被作为包加载时的相对导入
    from .dota_api import (
        OpenDotaError,
    )
except ImportError:  # 兜底：以普通模块方式加载时，把插件目录加入 sys.path
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dota_api import (  # type: ignore[no-redef]
        OpenDotaError,
    )


#: STRATZ 的 GraphQL 端点
STRATZ_ENDPOINT = "https://api.stratz.com/graphql"

#: STRATZ 强制要求的 User-Agent（官方文档明确写死这个值）
STRATZ_USER_AGENT = "STRATZ_API"

#: 英雄常量缓存有效期（秒），与 OpenDota 侧保持一致
HERO_CACHE_TTL = 12 * 3600

#: STRATZ 的 GameMode 枚举 → OpenDota 的 game_mode 编号。
#: 只需要覆盖插件会展示的常见模式，其余回退 None（展示为「模式未知」）。
STRATZ_GAME_MODE_IDS: dict[str, int] = {
    "ALL_PICK": 1,
    "CAPTAINS_MODE": 2,
    "RANDOM_DRAFT": 3,
    "SINGLE_DRAFT": 4,
    "ALL_RANDOM": 5,
    "INTRO": 6,
    "THE_DIRETIDE": 7,
    "REVERSE_CAPTAINS_MODE": 8,
    "THE_GREEVILING": 9,
    "TUTORIAL": 10,
    "MID_ONLY": 11,
    "LEAST_PLAYED": 12,
    "NEW_PLAYER_POOL": 13,
    "COMPENDIUM_MATCHMAKING": 14,
    "CUSTOM": 15,
    "CAPTAINS_DRAFT": 16,
    "BALANCED_DRAFT": 17,
    "ABILITY_DRAFT": 18,
    "EVENT": 19,
    "ALL_RANDOM_DEATH_MATCH": 20,
    "SOLO_MID": 21,
    "ALL_PICK_RANKED": 22,
    "TURBO": 23,
    "MUTATION": 24,
}

#: STRATZ 的 LobbyType 枚举 → OpenDota 的 lobby_type 编号
STRATZ_LOBBY_IDS: dict[str, int] = {
    "UNRANKED": 0,
    "PRACTICE": 1,
    "TOURNAMENT": 2,
    "TUTORIAL": 3,
    "COOP_VS_BOTS": 4,
    "TEAM_MATCH": 5,
    "SOLO_QUEUE": 6,
    "RANKED": 7,
    "SOLO_MID": 8,
    "BATTLE_CUP": 9,
    "EVENT": 10,
}

#: STRATZ 的位置枚举 → 分路角色编号（对齐 OpenDota 的 lane_role）
#: 1=优势路(Safe) 2=中路(Mid) 3=劣势路(Off) 4=野区
STRATZ_POSITION_LANE_ROLE: dict[str, int] = {
    "POSITION_1": 1,
    "POSITION_2": 2,
    "POSITION_3": 3,
    "POSITION_4": 3,  # 4 号位常驻劣势路/游走，归为 off
    "POSITION_5": 1,  # 5 号位常驻优势路
}

#: STRATZ 的 lane 枚举 → OpenDota 的 lane_role
STRATZ_LANE_ROLE: dict[str, int] = {
    "SAFE_LANE": 1,
    "MID_LANE": 2,
    "OFF_LANE": 3,
    "JUNGLE": 4,
    "ROAMING": 4,
}


class StratzUnavailableError(OpenDotaError):
    """STRATZ 通道不可用（未配置 Key / 鉴权失败 / 网络异常）。

    单独成类是为了让 :class:`~dota_api.FallbackDataSource` 能明确区分
    「主数据源挂了，该切后备」与「这个玩家确实不存在，后备也救不了」。
    继承 :class:`OpenDotaError` 以保证上层既有的 ``except OpenDotaError``
    依然能兜住它。
    """


class StratzRateLimitError(StratzUnavailableError):
    """STRATZ 限流（429）。值得重试，重试耗尽后降级。"""


class StratzIPMismatchError(StratzUnavailableError):
    """STRATZ 令牌的 IP 绑定不匹配。

    官方把令牌绑定到首次调用的出口 IP。换网络/换代理时会**临时** 403，
    稍后重试通常能恢复，因此这类异常会走重试而不是立刻降级。
    """


class StratzClient:
    """STRATZ GraphQL 客户端（对外接口与 OpenDotaClient 对齐）。

    只依赖标准库 ``urllib``：不引入 ``gql`` / ``httpx`` 之外的依赖，也不会
    和 AstrBot 自带的 HTTP 栈产生版本冲突。请求跑在线程池里，不阻塞事件循环。
    """

    #: 数据源身份标记，与 ``OpenDotaClient.SOURCE_KIND`` 配对使用：组合数据源
    #: 靠它区分「哪一端是 OpenDota」，从而实现某些能力只走某一边
    #: （例如 ``request_parse`` / ``get_hero_stats`` 是 OpenDota 独占）。
    SOURCE_KIND = "stratz"

    #: 与 ``OpenDotaClient.SUPPORTS_PATCH_FILTER`` 配对：STRATZ **没有**
    #: 版本维度。实测 ``heroesPerformance(request: { patchIds: [60] })``
    #: 直接报 ``Unknown field``，只能在 ``gameModeIds`` 上过滤。
    #: 所以这里必须显式声明 False —— 上层据此决定「退回全量口径并如实标注」，
    #: 而不是把全生涯数据当成当前版本摆给用户。
    SUPPORTS_PATCH_FILTER = False

    #: 数据源名称，供日志与「当前数据源」提示使用
    name = "STRATZ"
    label = "STRATZ"

    def __init__(
        self,
        api_key: str = "",
        timeout: int = 30,
        max_retries: int = 3,
        rate_limit_per_minute: int = 240,
        proxy: str = "",
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.timeout = max(5, int(timeout))
        self.max_retries = max(0, int(max_retries))
        self.proxy = (proxy or "").strip()
        #: STRATZ 默认令牌上限 250 次/分钟；本地限流默认给一点余量
        self.rate_limit_per_minute = max(1, int(rate_limit_per_minute))
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._hero_cache: dict[int, dict] | None = None
        self._hero_cache_at = 0.0
        self._item_cache: dict[str, dict] | None = None
        self._item_cache_at = 0.0

    # ------------------------------------------------------------------
    # 底层请求
    # ------------------------------------------------------------------
    @property
    def configured(self) -> bool:
        """是否配置了 API Key。"""
        return bool(self.api_key)

    async def _throttle(self) -> None:
        async with self._lock:
            import time as _time

            interval = 60.0 / self.rate_limit_per_minute
            wait = self._last_request_at + interval - _time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = _time.monotonic()

    def _post_sync(self, query: str, variables: dict[str, Any]) -> dict:
        """同步发一次 GraphQL 请求（由 :meth:`_gql` 放进线程池执行）。"""
        body = json.dumps(
            {"query": query, "variables": variables or {}}, ensure_ascii=False
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            # STRATZ 官方要求带上这个 UA，缺失会被拒
            "User-Agent": STRATZ_USER_AGENT,
        }
        request = urllib.request.Request(
            STRATZ_ENDPOINT, data=body, headers=headers, method="POST"
        )
        # ⚠️ 走 `.open()` 而不是直接 `opener(request, …)`：`OpenerDirector`
        # **是可调用的**（`__call__` 等价于 `.open()`），线上一直是那么跑的；
        # 但 typeshed 没有声明 `__call__`，于是 mypy 报
        # `"OpenerDirector" not callable`（2026-10-14）。两条分支在这里统一成
        # 同一个签名 `(request, timeout=…)`，行为与原先逐字一致。
        fetch: Callable[..., Any]
        if self.proxy:
            fetch = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
            ).open
        else:
            fetch = urllib.request.urlopen
        try:
            with fetch(request, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                detail = ""
            # STRATZ 会把令牌绑定到首次使用的 IP。切换网络 / 出口 IP 变化时
            # 会临时 403，稍后重试通常就通了，因此单独标记成「可重试」。
            if e.code in (401, 403) and "different IP" in detail:
                raise StratzIPMismatchError(
                    f"STRATZ 拒绝：令牌与当前出口 IP 不一致（HTTP {e.code}）。"
                    f"该令牌已绑定到首次调用时的 IP，请稍后重试，"
                    f"或在 stratz.com 重新生成令牌。原始信息：{detail}"
                ) from e
            # 401/403 其余情况视为 Key 问题，明确成「不可用」以便切后备
            if e.code in (401, 403):
                raise StratzUnavailableError(
                    f"STRATZ 鉴权失败（HTTP {e.code}）"
                    + (f"：{detail}" if detail else "")
                    + "。请检查「STRATZ API Key」配置。"
                ) from e
            if e.code == 429:
                raise StratzRateLimitError("STRATZ 请求频率超限（429）") from e
            raise StratzUnavailableError(
                f"STRATZ 接口返回 HTTP {e.code}"
                + (f"：{detail}" if detail else "")
            ) from e
        except urllib.error.URLError as e:
            raise StratzUnavailableError(f"无法连接 STRATZ：{e.reason}") from e
        except (OSError, TimeoutError, http.client.HTTPException) as e:
            # ⚠️ 必须补这一段（P1-1，2026-10-14 审查）：CPython 的
            # ``urllib/request.py`` **只把「发送请求」阶段的 OSError 包成
            # URLError**；``getresponse()`` / ``resp.read()`` 里抛出的
            # ``TimeoutError``（socket 超时）/ ``ConnectionResetError`` /
            # ``RemoteDisconnected`` / ``BadStatusLine``（都是 OSError 或
            # http.client.HTTPException 的子类）会**直接穿出**上面两个分支。
            #
            # 后果不只是「日志难看」：`_gql` 只 catch 三个 Stratz* 异常，
            # `FallbackDataSource._make_forwarder` 只 catch
            # `_fallback_trigger_types()`（= Stratz*/OpenDota*）——
            # 于是读超时会**逃出整条降级链**，直接冒到 handler 层变成
            # 「执行失败」，后备的 OpenDota 一次都不会被问到。
            #
            # 顺序要紧：``HTTPError`` 是 ``URLError`` 的子类，
            # 必须排在它前面（这里是独立分支，天然满足）。
            raise StratzUnavailableError(
                f"连接 STRATZ 时发生网络错误（{type(e).__name__}）：{e}"
            ) from e

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise StratzUnavailableError(f"STRATZ 返回体不是合法 JSON：{raw[:200]}") from e

        if isinstance(data, dict) and data.get("errors"):
            errors = data["errors"]
            first = errors[0] if isinstance(errors, list) and errors else errors
            msg = first.get("message") if isinstance(first, dict) else str(first)
            # "User is not an admin." 说明该字段需要更高权限令牌
            raise StratzUnavailableError(f"STRATZ 查询出错：{msg}")
        return data.get("data") or {}

    async def _gql(self, query: str, variables: dict[str, Any] | None = None) -> dict:
        """执行一次 GraphQL 查询，返回 ``data`` 部分。

        重试策略与 OpenDota 侧一致（指数退避），但有两类异常**不重试**：
        未配置 Key、以及 GraphQL 语义错误（查询写错）——重试不会改变结果。
        限流与 IP 绑定不匹配属于瞬时问题，会正常重试。
        """
        if not self.api_key:
            raise StratzUnavailableError("未配置 STRATZ API Key")

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            await self._throttle()
            try:
                return await asyncio.to_thread(
                    self._post_sync, query, variables or {}
                )
            except StratzRateLimitError as e:
                last_error = e
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(min(20.0, 2.0 * (2**attempt)))
            except StratzIPMismatchError as e:
                last_error = e
                if attempt >= self.max_retries:
                    break
                # IP 绑定是服务端状态，给一点时间让它稳定
                await asyncio.sleep(min(6.0, 1.5 * (attempt + 1)))
            except StratzUnavailableError as e:
                last_error = e
                # 鉴权失败 / 未配置 / 查询语义错误：重试没意义
                text = str(e)
                if (
                    "鉴权失败" in text
                    or "未配置" in text
                    or "查询出错" in text
                ):
                    raise
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(min(8.0, 1.5 * (2**attempt)))
        raise last_error or StratzUnavailableError("STRATZ 请求失败")

    async def close(self) -> None:
        """与 OpenDotaClient 对齐的空实现（本客户端不用连接池）。"""

    # ------------------------------------------------------------------
    # 字段映射工具
    # ------------------------------------------------------------------
    @staticmethod
    def _map_game_mode(mode: Any) -> int | None:
        if isinstance(mode, int):
            return mode
        if isinstance(mode, str):
            return STRATZ_GAME_MODE_IDS.get(mode.upper())
        return None

    @staticmethod
    def _map_lobby(lobby: Any) -> int | None:
        if isinstance(lobby, int):
            return lobby
        if isinstance(lobby, str):
            return STRATZ_LOBBY_IDS.get(lobby.upper())
        return None

    @staticmethod
    def _lane_role_of(player: dict) -> int | None:
        lane = str(player.get("lane") or "").upper()
        if lane in STRATZ_LANE_ROLE:
            return STRATZ_LANE_ROLE[lane]
        position = str(player.get("position") or "").upper()
        return STRATZ_POSITION_LANE_ROLE.get(position)

    @classmethod
    def _normalize_player(cls, node: dict, *, include_lobby: bool = False) -> dict:
        """把 STRATZ 的 MatchPlayerType 映射成 OpenDota 的 player dict。

        注意 **`isRadiant` 必须落到 `player_slot` 上**，不能只存一个平行的
        ``is_radiant`` 字段：下游 ``dota_format.player_win`` / ``is_radiant``
        读的是 ``player_slot``，而 STRATZ 不直接给这个字段。早期版本这里
        直接写 ``node.get("playerSlot")``（几乎永远是 ``None``）→
        ``int(None)`` 走 except 分支默认成「天辉」→ 夜魇玩家被误判为胜方，
        标题上就出现「天辉获胜」配「❌负」的自相矛盾。
        """
        account = node.get("steamAccount") or {}
        hero = node.get("hero") or {}
        is_radiant = node.get("isRadiant")
        if is_radiant is None:
            # 没有阵营标记时，退回原始 playerSlot（STRATZ 偶有直给）
            raw_slot = node.get("playerSlot")
            if raw_slot is not None:
                try:
                    is_radiant = int(raw_slot) < 128
                except (TypeError, ValueError):
                    is_radiant = None
        out: dict[str, Any] = {
            "account_id": node.get("steamAccountId"),
            # player_slot 统一成 OpenDota 口径：<128 天辉 / >=128 夜魇。
            # 具体序号在 get_match 里按阵营归类后回填（那边能看到整队）。
            "player_slot": (0 if is_radiant else 128) if is_radiant is not None else None,
            "hero_id": node.get("heroId"),
            "kills": node.get("kills"),
            "deaths": node.get("deaths"),
            "assists": node.get("assists"),
            "gold_per_min": node.get("goldPerMinute"),
            "xp_per_min": node.get("experiencePerMinute"),
            "last_hits": node.get("numLastHits"),
            "denies": node.get("numDenies"),
            "hero_damage": node.get("heroDamage"),
            "tower_damage": node.get("towerDamage"),
            "hero_healing": node.get("heroHealing"),
            "net_worth": node.get("networth"),
            "level": node.get("level"),
            "lane_role": cls._lane_role_of(node),
            "is_roaming": (str(node.get("lane") or "").upper() == "ROAMING") or None,
            # 附加（OpenDota 侧没有，但提示词里能用上）
            "personaname": account.get("name"),
            # ``name`` 是下游取昵称的统一字段（OpenDota 用它、提示词用它），
            # 只给 personaname 会让 STRATZ 侧所有人显示成 "None"。
            "name": account.get("name"),
            "hero_name": hero.get("displayName"),
            "position": node.get("position"),
            "is_radiant": is_radiant,
            # 胜负的权威来源（STRATZ 直接给），player_win 会优先用它
            "isVictory": node.get("isVictory"),
        }
        # ---- 物品槽位 -------------------------------------------------
        # 查询里本来就带了 item0Id~item5Id / neutral0Id，但早期版本**没有映射**，
        # 于是 STRATZ 直连时「出装」永远显示成 `-`：数据明明拿到了却凭空丢掉。
        # 这里按 OpenDota 的 ``item_0``~``item_5`` / ``item_neutral`` 命名落地，
        # 数值是道具 ID，``ItemIndex`` 能直接按 ID 查出显示名。
        for index in range(6):
            out[f"item_{index}"] = node.get(f"item{index}Id")
        out["item_neutral"] = node.get("neutral0Id")
        return out

    @classmethod
    def _normalize_match_player_row(
        cls, node: dict, *, account_id: int | None = None
    ) -> dict:
        """把 player.matches 里嵌套的 players 节点映射成「玩家视角」的扁平行。

        这是 OpenDota ``/players/{id}/matches`` 的返回形状：一行 = 该玩家
        在某场比赛里的表现，字段直接平铺。
        """
        player = node.get("players") or {}
        if isinstance(player, list):
            player = player[0] if player else {}
        row = cls._normalize_player(player)
        row["match_id"] = node.get("id")
        row["start_time"] = node.get("startDateTime")
        row["duration"] = node.get("durationSeconds")
        row["game_mode"] = cls._map_game_mode(node.get("gameMode"))
        row["lobby_type"] = cls._map_lobby(node.get("lobbyType"))
        # 该玩家是否胜利：优先用它自己的 isVictory，其次由 radiant_win 推导
        did_radiant_win = node.get("didRadiantWin")
        is_radiant = player.get("isRadiant")
        if player.get("isVictory") is not None:
            row["player_win"] = bool(player.get("isVictory"))
        elif did_radiant_win is not None and is_radiant is not None:
            row["player_win"] = bool(did_radiant_win) == bool(is_radiant)
        row["radiant_win"] = did_radiant_win
        row["is_radiant"] = is_radiant
        row["parsed_datetime"] = node.get("parsedDateTime")
        # 「队伍总击杀」—— ``dota_format.summarize_matches`` 的场均击杀参与率要用它。
        # 这个键在本文件之外**只被读、从来没有写入方**（2026-10-14 审查报告 4.4），
        # 于是那项指标恒不出数。补上唯一能拿到它的地方：
        #
        # * OpenDota 侧**拿不到**：``/players/{id}/recentMatches`` 与
        #   ``/players/{id}/matches`` 都不返回 ``radiant_score``/``dire_score``
        #   （2026-10-14 实测各 20 条全为 None，``project=radiant_score`` 也无效）；
        # * STRATZ 侧**能拿到**：``radiantKills``/``direKills`` 是逐分钟击杀数数组，
        #   与本文件 :meth:`_normalize_match` 的口径一致，求和即可。
        #
        # 取不到就**不写这个键**，让渲染层按「样本 0 场」自然省略那一行 ——
        # 不要写 0（0 会被读成「零参与」）。
        row["_team_kills"] = cls._team_kills_of(node, is_radiant)
        if account_id is not None:
            row["account_id"] = account_id
        return row

    @staticmethod
    def _team_kills_of(node: dict, is_radiant: object) -> int | None:
        """该玩家**所在队伍**本场总击杀（拿不到返回 ``None``）。

        ``radiantKills``/``direKills`` 是逐分钟击杀数（形状与 OpenDota 的
        ``radiant_score``/``dire_score`` 一致，见 :meth:`_normalize_match`）。
        阵营未知、数组缺失、元素不是数字时一律返回 ``None`` —— 让「参团率」
        这一项缺席，而不是拿半份数据算出个错的百分比。
        """
        if not isinstance(is_radiant, bool):
            return None
        series = node.get("radiantKills") if is_radiant else node.get("direKills")
        if not isinstance(series, list) or not series:
            return None
        total = 0
        for value in series:
            try:
                total += int(value or 0)
            except (TypeError, ValueError):
                return None
        return total if total > 0 else None

    # ------------------------------------------------------------------
    # 英雄 / 道具常量
    # ------------------------------------------------------------------
    async def get_heroes(self, force: bool = False) -> dict[int, dict]:
        """获取英雄常量表，返回 ``{hero_id: hero_info}``（OpenDota 形状）。"""
        import time

        now = time.time()
        if (
            not force
            and self._hero_cache is not None
            and now - self._hero_cache_at < HERO_CACHE_TTL
        ):
            return self._hero_cache

        data = await self._gql(
            """
            query Heroes {
              constants {
                heroes { id displayName shortName name }
              }
            }
            """
        )
        heroes: dict[int, dict] = {}
        rows = ((data.get("constants") or {}).get("heroes")) or []
        for row in rows:
            try:
                hid = int(row.get("id"))
            except (TypeError, ValueError):
                continue
            # 映射成 OpenDota 的 /constants/heroes 形状，业务层直接复用
            heroes[hid] = {
                "id": hid,
                "name": row.get("name"),
                "localized_name": row.get("displayName"),
                "short_name": row.get("shortName"),
            }
        self._hero_cache = heroes
        self._hero_cache_at = now
        return heroes

    async def hero_name(self, hero_id: int | None) -> str:
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
        """获取道具常量表。

        STRATZ 的 items 常量很大（含全部属性），这里按 OpenDota 的形状做一层
        裁剪：只保留 ``id`` / ``dname`` / ``img`` 这些业务层真正用到的字段。
        """
        import time

        now = time.time()
        if (
            not force
            and self._item_cache is not None
            and now - self._item_cache_at < HERO_CACHE_TTL
        ):
            return self._item_cache

        items: dict[str, dict] = {}
        try:
            data = await self._gql(
                """
                query Items {
                  constants {
                    items { id name displayName }
                  }
                }
                """
            )
            rows = ((data.get("constants") or {}).get("items")) or []
            for row in rows:
                key = str(row.get("name") or row.get("id") or "")
                if not key:
                    continue
                items[key] = {
                    "id": row.get("id"),
                    "dname": row.get("displayName"),
                }
        except OpenDotaError as e:
            logger.debug(f"[dota2] STRATZ 获取道具常量失败: {e}")
            items = {}

        self._item_cache = items
        self._item_cache_at = now
        return items

    async def get_ability_names(self) -> dict[int, str]:
        """技能常量表。

        STRATZ 侧的 GraphQL 没有 OpenDota ``/constants/ability_ids`` 那种
        「技能 ID → 技能名」的扁平映射，而且 STRATZ 的 ``get_match`` 也不返回
        ``ability_upgrades_arr``，所以这里没有可复用的数据，直接返回空字典。
        调用方按「拿不到就不输出技能加点小节」处理。
        """
        return {}

    # ------------------------------------------------------------------
    # 玩家相关
    # ------------------------------------------------------------------
    async def get_player(self, account_id: int) -> dict | None:
        """获取玩家资料，返回 OpenDota ``/players/{id}`` 形状。"""
        data = await self._gql(
            """
            query Player($id: Long!) {
              player(steamAccountId: $id) {
                steamAccountId
                matchCount
                winCount
                behaviorScore
                steamAccount {
                  id
                  name
                  avatar
                  profileUri
                  seasonRank
                  seasonLeaderboardRank
                  isAnonymous
                  isDotaPlusSubscriber
                }
                ranks { seasonRankId rank isCore }
              }
            }
            """,
            {"id": int(account_id)},
        )
        player = data.get("player")
        if not isinstance(player, dict):
            return None
        account = player.get("steamAccount") or {}
        if not account and not player.get("matchCount"):
            return None

        ranks = player.get("ranks") or []
        rank_tier = None
        if isinstance(ranks, list) and ranks:
            # 取「核心」那条；没有就取第一条
            core = next(
                (r for r in ranks if isinstance(r, dict) and r.get("isCore")),
                ranks[0],
            )
            if isinstance(core, dict):
                # rank 字段缺失 / 不是数字时 `rank_tier` 保持 None（不猜段位）。
                # 原先写成 `int(core.get("rank"))` 靠 except 兜住 None —— 效果一样，
                # 但静态检查器看不到「None 也被兜住了」（mypy `[arg-type]`）。
                raw_rank = core.get("rank")
                if raw_rank is not None:
                    try:
                        # STRATZ 的 rank 是 1~80 的「段位*10+星级」，与 OpenDota 的
                        # rank_tier（十位=段位、个位=星级）是同一套编码
                        # （如 42 = 统帅 2 星 = Archon II），可直接用。
                        rank_tier = int(raw_rank)
                    except (TypeError, ValueError):
                        rank_tier = None

        return {
            "profile": {
                "account_id": player.get("steamAccountId"),
                "personaname": account.get("name"),
                "name": account.get("name"),
                "avatarfull": account.get("avatar"),
                "profileurl": account.get("profileUri"),
                "steamid": None,
                "loccountrycode": None,
            },
            "rank_tier": rank_tier,
            # 保留原始计数，业务层可直接用
            "_stratz_match_count": player.get("matchCount"),
            "_stratz_win_count": player.get("winCount"),
            "_stratz_behavior_score": player.get("behaviorScore"),
        }

    async def search_player(self, query: str, timeout: float = 60.0) -> list[dict]:
        """按昵称搜索玩家，返回 OpenDota ``/search`` 形状的列表。"""
        query = (query or "").strip()
        if not query:
            return []
        data = await self._gql(
            """
            query Search($q: String!) {
              stratz {
                search(request: { query: $q, searchType: [PLAYERS], take: 20 }) {
                  players { id name avatar }
                }
              }
            }
            """,
            {"q": query},
        )
        rows = (((data.get("stratz") or {}).get("search")) or {}).get("players") or []
        results: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            account_id = row.get("id")
            if not account_id:
                continue
            results.append(
                {
                    "account_id": account_id,
                    "personaname": row.get("name"),
                    "avatarfull": row.get("avatar"),
                    "similarity": 0.0,
                }
            )
        return results

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
        """按时间倒序获取玩家最近比赛（OpenDota ``/players/{id}/matches`` 形状）。

        ``significant`` 参数在 STRATZ 侧没有对应概念（STRATZ 默认就包含所有
        模式，包括 Turbo），因此这里接受但忽略，保持接口签名一致。
        """
        take = max(1, min(int(limit), 100))
        request_parts = [f"take: {take}", f"skip: {max(0, int(offset))}"]
        # 胜负过滤：STRATZ 用 request 里的 isVictory
        if win:
            request_parts.append("isVictory: true")
        elif lose:
            request_parts.append("isVictory: false")
        if hero_id:
            request_parts.append(f"heroIds: [{int(hero_id)}]")
        request = "{ " + ", ".join(request_parts) + " }"

        data = await self._gql(
            f"""
            query Matches($id: Long!) {{
              player(steamAccountId: $id) {{
                matches(request: {request}) {{
                  id
                  startDateTime
                  durationSeconds
                  didRadiantWin
                  gameMode
                  lobbyType
                  parsedDateTime
                  players(steamAccountId: $id) {{
                    steamAccountId
                    isRadiant
                    isVictory
                    heroId
                    kills
                    deaths
                    assists
                    goldPerMinute
                    experiencePerMinute
                    numLastHits
                    numDenies
                    heroDamage
                    towerDamage
                    heroHealing
                    networth
                    level
                    lane
                    position
                  }}
                  # ⚠️ 只为「队伍总击杀」而取（2026-10-14，审查报告 4.4 ：
                  # ``dota_format`` 的「场均击杀参与率」靠 ``_team_kills``，
                  # 而那个键**从来没有写入方**）。STRATZ 的 MatchType **没有**
                  # 标量比分字段（实测 ``radiantScore`` / ``direScore`` 都报
                  # Unknown field），只有这两个逐分钟击杀数数组，求和即
                  # 「该队本场总击杀」。代价实测：20 场查询 9,573 → 11,469
                  # 字节（+20%），仍是一次请求。
                  radiantKills
                  direKills
                }}
              }}
            }}
            """,
            {"id": int(account_id)},
        )
        rows = ((data.get("player") or {}).get("matches")) or []
        out: list[dict] = []
        for node in rows:
            if not isinstance(node, dict):
                continue
            out.append(
                self._normalize_match_player_row(node, account_id=int(account_id))
            )
        out.sort(key=lambda m: int(m.get("start_time") or 0), reverse=True)
        return out

    async def get_recent_matches(self, account_id: int, limit: int = 20) -> list[dict]:
        """最近比赛（带经济字段）。STRATZ 单次查询即带全字段，无需二次补全。"""
        rows = await self.get_player_matches(account_id, limit=limit)
        return rows[: max(1, int(limit))]

    async def get_player_matches_enriched(
        self, account_id: int, limit: int = 20
    ) -> tuple[list[dict], int]:
        """获取玩家真正的最近 N 场（所有模式），并返回带经济字段的场次数。

        STRATZ 一次查询就同时返回基础字段与经济字段，因此这里天然不存在
        OpenDota 那种「20 场以上没有经济数据」的限制 —— 这是 STRATZ 作为
        主数据源的主要收益之一。
        """
        limit = max(1, int(limit))
        matches = await self.get_player_matches(account_id, limit=limit)
        matches = matches[:limit]
        enriched = sum(1 for m in matches if m.get("gold_per_min") is not None)
        return matches, enriched

    async def get_player_wl(self, account_id: int) -> dict:
        """获取玩家总胜负场次，返回 ``{"win": n, "lose": n}``。"""
        data = await self._gql(
            """
            query WL($id: Long!) {
              player(steamAccountId: $id) {
                matchCount
                winCount
              }
            }
            """,
            {"id": int(account_id)},
        )
        player = data.get("player") or {}
        try:
            total = int(player.get("matchCount") or 0)
            wins = int(player.get("winCount") or 0)
        except (TypeError, ValueError):
            return {}
        return {"win": wins, "lose": max(0, total - wins)}

    async def get_patches(self, limit: int = 0) -> list[dict]:
        """STRATZ 没有补丁表，返回空列表。

        不是「忘了实现」：STRATZ 的 schema 里没有版本维度（见
        :attr:`SUPPORTS_PATCH_FILTER`），拿不到补丁编号与起止时间。
        返回空列表而不是抛异常，是为了让上层走**已知的降级路径**：
        :func:`dota_pool.collect_hero_pool` 见补丁表为空 → 退回全量口径
        并在文案里写明「未按版本过滤」。抛异常反而会触发数据源整体的
        降级链，把一次正常的「这个源没这个能力」变成一次故障。
        """
        return []

    async def get_player_heroes(
        self,
        account_id: int,
        *,
        patch: int | None = None,
        game_mode: int | None = None,
        include_insignificant: bool = True,
    ) -> list[dict]:
        """获取玩家各英雄使用统计（OpenDota ``/players/{id}/heroes`` 形状）。

        Args:
            patch: **本数据源不支持**，传了也会被忽略（见类属性
                :attr:`SUPPORTS_PATCH_FILTER`）。留着这个形参只为与
                :meth:`OpenDotaClient.get_player_heroes` 保持同一份签名，
                免得两个源在同一段调用代码下行为分叉。
            game_mode: 只统计某个模式，``23`` = 加速模式。STRATZ 的
                加速数据是**独立统计**的，不走 OpenDota 那套
                ``significant`` 默认过滤，所以这里传不传都能拿到。
            include_insignificant: STRATZ 侧无此概念（它本来就收全模式），
                参数保留只为签名一致。
        """
        req: list[str] = ["take: 100"]
        if game_mode is not None:
            req.append(f"gameModeIds: [{int(game_mode)}]")
        request = "{ " + ", ".join(req) + " }"
        data = await self._gql(
            """
            query HP($id: Long!) {
              player(steamAccountId: $id) {
                heroesPerformance(request: %s) {
                  heroId
                  matchCount
                  winCount
                  goldPerMinute
                  experiencePerMinute
                  kDA
                  avgKills
                  avgDeaths
                  avgAssists
                  lastPlayedDateTime
                }
              }
            }
            """
            % request,
            {"id": int(account_id)},
        )
        rows = ((data.get("player") or {}).get("heroesPerformance")) or []
        out: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            out.append(
                {
                    "hero_id": row.get("heroId"),
                    "games": row.get("matchCount"),
                    "win": row.get("winCount"),
                    "gold_per_min": row.get("goldPerMinute"),
                    "xp_per_min": row.get("experiencePerMinute"),
                    "last_played": row.get("lastPlayedDateTime"),
                    # OpenDota 侧没有的 STRATZ 特色字段，保留供提示词增强
                    "_kda": row.get("kDA"),
                    "_avg_kills": row.get("avgKills"),
                    "_avg_deaths": row.get("avgDeaths"),
                    "_avg_assists": row.get("avgAssists"),
                }
            )
        # OpenDota 的 /players/{id}/heroes 按场次倒序返回，下游（summarize_hero_history）
        # 会直接取前 N 条当作「最常用的英雄」。STRATZ 的 heroesPerformance 不保证
        # 顺序，这里统一按场次倒序，保证两端行为一致。
        out.sort(key=lambda r: int(r.get("games") or 0), reverse=True)
        return out

    #: 英雄打法数据允许的段位白名单。这几个值会被**直接拼进 GraphQL 查询**
    #: （枚举字面量不能走变量），所以必须是受控常量而不是调用方传什么用什么。
    PLAYSTYLE_BRACKETS: tuple[str, ...] = (
        "DIVINE_IMMORTAL",
        "LEGEND_ANCIENT",
        "CRUSADER_ARCHON",
        "HERALD_GUARDIAN",
        "ALL",
    )

    async def get_hero_playstyle(
        self, hero_id: int, *, bracket: str = "DIVINE_IMMORTAL"
    ) -> dict:
        """英雄当前版本的**高分段**打法数据（位置分布 / 出门装 / 鞋子）。

        这几项在 OpenDota 侧**没有等价端点**（``/heroStats`` 没有位置维度、
        也没有出门装统计），所以本方法只走 STRATZ，组合数据源里没有
        STRATZ 时由上层如实降级。

        一次查齐三块：它们永远一起用，分三次查只会多打两次网络。

        对线（``laneOutcome``）**故意不在这里查**，见
        :meth:`get_hero_lane_outcome` 的说明。

        Returns:
            ``{"bracket", "stats", "starting", "boots"}``，各键都是
            list（拿不到就是空 list，**不抛**——上层按「这块没有」渲染）。
        """
        key = str(bracket or "").strip().upper()
        if key not in self.PLAYSTYLE_BRACKETS:
            key = "DIVINE_IMMORTAL"
        data = await self._gql(
            """
            query HeroPlaystyle($heroId: Short!) {
              heroStats {
                stats(heroIds: [$heroId], bracketBasicIds: [%s], groupByPosition: true) {
                  position
                  matchCount
                  winCount
                  cs
                  level
                  heroDamage
                  towerDamage
                  kills
                  deaths
                  assists
                }
                itemStartingPurchase(heroId: $heroId, bracketBasicIds: [%s]) {
                  itemId
                  instance
                  wasGiven
                  matchCount
                  winCount
                }
                itemBootPurchase(heroId: $heroId, bracketBasicIds: [%s]) {
                  itemId
                  instance
                  matchCount
                  winCount
                  time
                }
              }
            }
            """
            % (key, key, key),
            {"heroId": int(hero_id)},
        )
        block = data.get("heroStats") or {}

        def _rows(name: str) -> list[dict]:
            rows = block.get(name)
            return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

        return {
            "bracket": key,
            "stats": _rows("stats"),
            "starting": _rows("itemStartingPurchase"),
            "boots": _rows("itemBootPurchase"),
        }

    #: 位置过滤查询（``laneOutcome`` / ``itemFullPurchase``）能接受的
    #: ``MatchPlayerPositionType``。这些值会被**直接拼进 GraphQL 查询**
    #: （枚举字面量不能走变量），所以必须是受控白名单。
    POSITION_IDS: tuple[str, ...] = (
        "POSITION_1",
        "POSITION_2",
        "POSITION_3",
        "POSITION_4",
        "POSITION_5",
    )

    async def get_hero_lane_outcome(
        self,
        hero_id: int,
        *,
        position: str = "",
        bracket: str = "DIVINE_IMMORTAL",
    ) -> list[dict]:
        """英雄在**某个位置**上的高分段对线结果。

        为什么单独一个方法、还要传 ``position``
        ----------------------------------------
        ``laneOutcome`` 返回的是**按对位拆开**的行 —— 实测敌法师在一号位
        会回 125 行（每行一个 ``heroId2`` 对手，另加 ``week`` 维度），
        直接求和会得到一个与场次口径**对不上**的数（实测是 ``stats`` 场次的
        1.5~3 倍，且各位置倍数不一致，说明它的计数单位是「对位」而不是「场」）。
        所以上层**只拿它算比例**，不折算场次。

        更要命的是：这个接口返回的 ``position`` 字段**不可信** —— 实测斧王
        （94% 都在三号位）回的行全部标成 ``POSITION_1``。因此**不能**靠读返回
        行的 ``position`` 来筛位置，必须在请求参数里用 ``positionIds`` 限定。
        这一个参数经实测是准的（斧王 ``POSITION_3`` 回 34291、``POSITION_1``
        只回 129），所以本方法是「先由上层算出主位置，再按位置问一次」。

        Args:
            position: 主位置枚举（``POSITION_1``~``POSITION_5``）。传空或不认识
                的值时**不加位置过滤**，拿回全部位置的混合数据。

        Returns:
            行列表（拿不到就是空 list，**不抛**）。
        """
        key = str(bracket or "").strip().upper()
        if key not in self.PLAYSTYLE_BRACKETS:
            key = "DIVINE_IMMORTAL"
        pos = str(position or "").strip().upper()
        pos_filter = ", positionIds: [%s]" % pos if pos in self.POSITION_IDS else ""
        data = await self._gql(
            """
            query HeroLane($heroId: Short!) {
              heroStats {
                laneOutcome(heroId: $heroId, isWith: false, bracketBasicIds: [%s]%s) {
                  week
                  position
                  matchCount
                  winCount
                  lossCount
                  drawCount
                  stompWinCount
                  stompLossCount
                }
              }
            }
            """
            % (key, pos_filter),
            {"heroId": int(hero_id)},
        )
        rows = ((data.get("heroStats") or {}).get("laneOutcome"))
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    #: ``itemFullPurchase`` 的返回行数上限（防呆）。实测一个英雄一个位置
    #: 68~133 行 —— 每件装备按购买分钟拆行；这个上限只防极端情况。
    MAX_ITEM_PURCHASE_ROWS = 600

    async def get_hero_item_purchase(
        self,
        hero_id: int,
        *,
        position: str = "",
        bracket: str = "DIVINE_IMMORTAL",
    ) -> list[dict]:
        """英雄在**某个位置**上、高分段的**完整装备购买**记录。

        这个字段的计数口径（实测，上层务必照着用）
        ------------------------------------------
        每一行是 **(英雄, 位置, 比赛分钟 ``time``, 装备 ``itemId``)**：

        * ``matchCount`` = 「在**该分钟**买到这件装备」的数量，
        * ``winCount``   = 其中赢下的数量，
        * 同一件装备会**按购买分钟拆成多行**（实测斧王刃甲 15 行，``time`` 9~23）。

        于是各行的 ``matchCount`` 相加**大于** ``stats`` 里的同位置场次
        （斧王三号位：刃甲合计 15528 vs 场次 11871）—— 说明它与 ``stats``
        **不是同一个样本池**。因此上层**只用它排先后、算加权胜率**，
        **不折算百分比、不报绝对场次**，否则会被拿去和场次相除得出 >100%。

        位置同样只能靠参数筛：返回行的 ``position`` **不可信**（实测斧王
        94% 在三号位，回的行却全标 ``POSITION_1``），与
        :meth:`get_hero_lane_outcome` 一个毛病。

        它能提供的**新**信息是两条：**典型成型时间**（按 ``matchCount``
        加权的 ``time`` 均值，单位是分钟）与**该装备的对局胜率**。

        Args:
            position: 主位置枚举（``POSITION_1``~``POSITION_5``）。传空或
                认不出的值时**不加位置过滤**，拿回混合位置的原始行。

        Returns:
            行列表（拿不到就是空 list，**不抛**）。
        """
        key = str(bracket or "").strip().upper()
        if key not in self.PLAYSTYLE_BRACKETS:
            key = "DIVINE_IMMORTAL"
        pos = str(position or "").strip().upper()
        pos_filter = ", positionIds: [%s]" % pos if pos in self.POSITION_IDS else ""
        data = await self._gql(
            """
            query HeroItemPurchase($heroId: Short!) {
              heroStats {
                itemFullPurchase(heroId: $heroId, bracketBasicIds: [%s]%s) {
                  itemId
                  instance
                  time
                  matchCount
                  winCount
                }
              }
            }
            """
            % (key, pos_filter),
            {"heroId": int(hero_id)},
        )
        rows = (data.get("heroStats") or {}).get("itemFullPurchase")
        out = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
        return out[: self.MAX_ITEM_PURCHASE_ROWS]

    async def get_player_totals(self, account_id: int, field: str = "kills") -> dict:
        """STRATZ 没有等价的 totals 接口，返回空 dict（业务层已按缺省处理）。"""
        return {}

    async def get_player_peers(self, account_id: int) -> list[dict]:
        """STRATZ 的队友接口需要额外权限，返回空列表。"""
        return []

    # ------------------------------------------------------------------
    # 比赛相关
    # ------------------------------------------------------------------
    async def get_match(self, match_id: int, timeout_factor: float = 3.0) -> dict | None:
        """获取单场比赛完整数据（OpenDota ``/matches/{id}`` 形状）。"""
        data = await self._gql(
            """
            query Match($id: Long!) {
              match(id: $id) {
                id
                durationSeconds
                startDateTime
                didRadiantWin
                gameMode
                lobbyType
                radiantKills
                direKills
                parsedDateTime
                averageRank
                radiantNetworthLeads
                radiantExperienceLeads
                players {
                  steamAccountId
                  playerSlot
                  isRadiant
                  isVictory
                  heroId
                  kills
                  deaths
                  assists
                  goldPerMinute
                  experiencePerMinute
                  numLastHits
                  numDenies
                  heroDamage
                  towerDamage
                  heroHealing
                  networth
                  level
                  lane
                  position
                  steamAccount { name }
                  hero { displayName }
                  item0Id
                  item1Id
                  item2Id
                  item3Id
                  item4Id
                  item5Id
                  neutral0Id
                }
              }
            }
            """,
            {"id": int(match_id)},
        )
        match = data.get("match")
        if not isinstance(match, dict) or not match.get("players"):
            return None

        players = [
            self._normalize_player(p)
            for p in match.get("players") or []
            if isinstance(p, dict)
        ]
        # player_slot：OpenDota 用 0~4（天辉）/128~132（夜魇）编码。
        # _normalize_player 已按 isRadiant 落下 0 / 128 的阵营标记，
        # 这里再按阵营顺序回填具体序号，保证同队之间不重复。
        raw_rows = [p for p in match.get("players") or [] if isinstance(p, dict)]
        radiant_idx = 0
        dire_idx = 0
        for raw, normalized in zip(raw_rows, players):
            is_radiant = normalized.get("is_radiant")
            if is_radiant is None:
                is_radiant = raw.get("isRadiant")
            if is_radiant is None:
                # 阵营未知：保留 None，让下游的判据显式判定而不是默认天辉
                continue
            if is_radiant:
                normalized["player_slot"] = radiant_idx
                radiant_idx += 1
            else:
                normalized["player_slot"] = 128 + dire_idx
                dire_idx += 1
            normalized["is_radiant"] = bool(is_radiant)

        radiant_win = match.get("didRadiantWin")
        radiant_kills = match.get("radiantKills") or []
        dire_kills = match.get("direKills") or []

        return {
            "match_id": match.get("id"),
            "start_time": match.get("startDateTime"),
            "duration": match.get("durationSeconds"),
            "radiant_win": radiant_win,
            "game_mode": self._map_game_mode(match.get("gameMode")),
            "lobby_type": self._map_lobby(match.get("lobbyType")),
            "players": players,
            # 与 OpenDota 形状对齐：radiant_score/dire_score 是总击杀数
            "radiant_score": sum(int(x or 0) for x in radiant_kills)
            if radiant_kills
            else None,
            "dire_score": sum(int(x or 0) for x in dire_kills) if dire_kills else None,
            "radiant_gold_adv": match.get("radiantNetworthLeads"),
            "radiant_xp_adv": match.get("radiantExperienceLeads"),
            "average_rank": match.get("averageRank"),
            # 供 is_parsed 判定：STRATZ 有 parsedDateTime 即视为已解析
            "od_data": {
                "has_parsed": bool(match.get("parsedDateTime")),
                "has_api": True,
                "has_gcdata": bool(match.get("parsedDateTime")),
                "has_archive": False,
            },
            "_stratz": True,
        }

    async def request_parse(self, match_id: int) -> bool:
        """STRATZ 不支持用户主动提交解析（它自己排队解析）。

        返回 False 表示「没能提交」，调用方（等待解析流程）在 STRATZ 通道下
        会走「等待收录」分支而不是「提交解析」分支。
        """
        logger.debug(f"[dota2] STRATZ 通道不支持主动提交解析（{match_id}）")
        return False

    async def get_benchmarks(self, hero_id: int) -> dict:
        """STRATZ 没有等价的 benchmarks 接口。"""
        return {}

    @staticmethod
    def is_parsed(match: dict | None) -> bool:
        """判断比赛是否已解析。

        与 OpenDota 侧共用 :func:`dota_format.parsed_state` 这一套**唯一**判据，
        它会同时认 OpenDota 的玩家 ``gold_t`` 和 STRATZ 的比赛级经济/经验曲线。
        这样「标题行」与「提示词正文」不会再出现一个说已解析、一个说未解析。
        """
        try:
            from .dota_format import parsed_state
        except ImportError:  # 模块方式加载时的兜底
            from dota_format import parsed_state  # type: ignore[no-redef]

        parsed, _note = parsed_state(match or {})
        return parsed
