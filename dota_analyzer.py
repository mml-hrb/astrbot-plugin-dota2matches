"""调用大模型生成 Dota 2 分析报告。

职责：
1. 把 OpenDota 的数据拼装成结构化的提示词（Prompt）；
2. 通过 AstrBot 的 Provider 接口，或插件自带的 OpenAI 兼容通道拿到模型回复。

本模块不直接依赖具体平台，只依赖 AstrBot 的 ``Context``。
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any

from astrbot.api import logger

try:  # 插件目录被作为包加载时的相对导入
    from .dota_format import (
        build_match_data_text,
        fmt_duration,
        fmt_timestamp,
        format_summary_block,
        hname,
        match_quality_block,
        mode_text,
        normalize_focus_ids,
        player_win,
        summarize_hero_history,
        summarize_matches,
        to_steam_id64,
    )
except ImportError:  # 兜底：以普通模块方式加载时，把插件目录加入 sys.path
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dota_format import (  # type: ignore[no-redef]
        build_match_data_text,
        fmt_duration,
        fmt_timestamp,
        format_summary_block,
        hname,
        match_quality_block,
        mode_text,
        normalize_focus_ids,
        player_win,
        summarize_hero_history,
        summarize_matches,
        to_steam_id64,
    )

#: 近期表现分析报告的章节要求
RECENT_REPORT_FORMAT = """请严格按照以下结构输出（使用 Markdown 二级/三级标题）：

## 一句话结论
（一句话概括这名玩家当前的竞技状态，直接给判断，不要铺垫）

## 近期状态走势
（结合胜率、连胜连败、前后半段胜率对比、KDA 与经济的波动，说明状态是在上升、下滑还是原地踏步）

## 打法风格画像
（这是重点。从分路倾向、英雄池、场均补刀与 GPM、伤害构成、死亡数、参团程度、单排/组队比例等数据推断出这名玩家的打法特征，例如：偏发育的后期核心 / 高风险的节奏发起者 / 稳健的功能型辅助 / 容易被抓的莽夫。必须给出数据依据）

## 数据亮点与短板
（分别列出 2-3 条最能说明问题的数据，注明具体数值）

## 可执行的改进建议
（3 条以内，每条要具体到「做什么、为什么」，例如「把场均死亡从 7.2 压到 5 以内，重点是在野区丢失视野后不要单人推线」）"""

#: 单场比赛复盘报告的章节要求
MATCH_REPORT_FORMAT = """写作纪律（先读这三条，再动笔）：
1. 数据块里给出的**每个维度都可能影响结论**，请优先引用具体数值（时间点、金币、次数、百分比、装备名），不要只写「发挥不错」这类没有依据的话。
2. **只使用数据块里出现过的数字**。数据块没给、或明确标注为「不可用 / -」的维度，就直说「数据未提供」，不要自行推测或编造。
3. 同一维度同时给了总量与构成时（英雄伤害 vs 输出构成、净经济 vs 经济来源、参团率 vs 团战逐场贡献），要把两者结合起来解释「为什么」，而不是各说一句。

请严格按照以下结构输出（使用 Markdown 二级/三级标题）：

## 比赛走势复盘
（按时间顺序梳理这场比赛。要指出：对线期谁占优、第一个重大转折发生在第几分钟、双方经济曲线在哪些时间点发生交叉、肉山与防御塔的争夺节奏。引用具体时间点和经济差数值）

## 比赛质量评估
（评估这是一场高质量对局还是崩盘局/碾压局：双方失误多不多、翻盘幅度有多大、团战是否胶着、经济曲线是否反复。给出你的判断依据）

## 关键数据对照
（把双方的客观数据摆出来对比，至少覆盖：总净经济、建筑存活（塔/兵营）、视野投入（假眼/真眼数量与被反数量）、团队英雄伤害、团队阵亡与买活、经济领先易手次数。用表格或分点列出并注明数值，最后说明这些差距把胜负解释到了什么程度）

## 焦点玩家点评
（对数据中标记为「焦点玩家」的**每一位**选手分别详细点评，每人单独起一个三级小标题。依次覆盖：对线表现与补刀效率、经济来源构成说明他靠什么变强、逐分钟发育曲线的拐点、输出构成与被针对情况（承伤来源）、关键装备的到手时间是否合理、技能加点与道具使用的合理性、视野投入（若是辅助）、死亡时段与阵亡占比反映的失误类型、团战中每一场的具体贡献。引用他的具体数据。如果只有一位焦点玩家，就只点评这一位）

## 其余选手点评
（逐一点评除焦点玩家之外的其余选手，每人 2-3 句，说明他在这场比赛里扮演的角色和发挥水平；至少提到他的装备或视野数据。用「英雄名(选手标识)」开头）

## 关键转折点
（列出本场最重要的 1-3 个转折点，说明当时发生了什么、对后续局势造成了什么影响。优先引用团战逐场数据与关键事件时间轴里的具体时间点）"""


def _player_headline(
    profile_data: dict, account_id: int, matches: list[dict]
) -> str:
    """拼出玩家基础信息头。"""
    profile = (profile_data or {}).get("profile") or {}
    name = profile.get("personaname") or (matches[0].get("personaname") if matches else None)
    name = name or f"账号{account_id}"
    steam_id = profile.get("steamid") or to_steam_id64(account_id)
    rank = profile_data.get("rank_tier") if profile_data else None
    return (
        f"玩家昵称: {name}\n"
        f"account_id: {account_id}\n"
        f"SteamID64: {steam_id}\n"
        f"rank_tier: {rank if rank else '未知'}"
    )


def build_recent_analysis_prompt(
    account_id: int,
    profile_data: dict,
    wl: dict,
    matches: list[dict],
    heroes: dict[int, dict],
    hero_rows: list[dict],
    economy_samples: int | None = None,
    requested_count: int = 20,
) -> str:
    """构造「近期表现与打法风格分析」的提示词。"""
    summary = summarize_matches(matches, economy_samples=economy_samples)
    rows = summarize_hero_history(hero_rows)

    lines: list[str] = []
    lines.append("请分析下面这位 Dota 2 玩家最近一段时间的表现与打法风格。")
    lines.append("")
    lines.append("=== 玩家信息 ===")
    lines.append(_player_headline(profile_data, account_id, matches))

    wins = int(wl.get("win") or 0)
    loses = int(wl.get("lose") or 0)
    if wins + loses:
        lines.append(
            f"生涯总战绩: {wins + loses} 场 · {wins} 胜 {loses} 负 · "
            f"胜率 {wins / (wins + loses) * 100:.1f}%"
        )

    if rows:
        lines.append(
            "生涯英雄池（按场次倒序，前 10）: "
            + "、".join(
                f"{hname(heroes, row['hero_id'])} {row['games']}场/{row['winrate']:.0f}%"
                for row in rows[:10]
            )
        )

    lines.append("")
    lines.append(f"=== 最近 {len(matches)} 场对局逐场明细（从最近往前）===")
    lines.append(
        "序号 | 比赛ID | 时间 | 英雄 | 结果 | K/D/A | GPM | XPM | 补刀 | 英雄伤害 | 时长 | 模式"
    )
    for index, match in enumerate(matches, start=1):
        lines.append(
            f"{index} | {match.get('match_id')} | {fmt_timestamp(match.get('start_time'))} | "
            f"{hname(heroes, match.get('hero_id'))} | "
            f"{'胜' if player_win(match) else '负'} | "
            f"{match.get('kills', 0)}/{match.get('deaths', 0)}/{match.get('assists', 0)} | "
            f"{match.get('gold_per_min') or '-'} | {match.get('xp_per_min') or '-'} | "
            f"{match.get('last_hits') or '-'} | {match.get('hero_damage') or '-'} | "
            f"{fmt_duration(match.get('duration'))} | {mode_text(match)}"
        )

    lines.append("")
    lines.append("=== 聚合统计 ===")
    lines.append(format_summary_block(summary, heroes))

    lines.append("")
    lines.append("=== 输出要求 ===")
    lines.append(RECENT_REPORT_FORMAT)
    return "\n".join(lines)


def build_single_match_analysis_prompt(
    match: dict,
    heroes: dict[int, dict],
    items: dict[str, dict],
    focus_account_ids: int | list[int] | tuple[int, ...] | None = None,
    focus_names: dict[int, str] | None = None,
    extra_context: str = "",
    abilities: dict[int, str] | None = None,
    curve_ids: list[int] | None = None,
) -> str:
    """构造「单场比赛深度复盘」的提示词。

    Args:
        focus_account_ids: 需要重点点评的玩家。支持一位或多位——同一局里可能
            有多位被监听的玩家参战，此时仍只生成**一份**报告，报告里对每位
            焦点玩家分别深入点评。
        focus_names: ``{account_id: 昵称}``，用于让提示词里出现可读的名字。
        extra_context: 附加上下文（例如焦点玩家近期的整体状态）。
        abilities: 技能常量映射 ``{技能ID: 技能名}``，用于把解析产物里的
            ``ability_upgrades_arr`` 翻译成可读的加点顺序；拿不到就不输出。
        curve_ids: 额外需要逐分钟发育曲线的玩家（无焦点时用来看核心的节奏）。
    """
    ids = normalize_focus_ids(focus_account_ids)
    names = focus_names or {}

    lines: list[str] = []
    lines.append("请对下面这场 Dota 2 比赛做一次深度复盘。")
    if ids:
        labels = [f"{names.get(aid) or aid}（account_id={aid}）" for aid in ids]
        if len(labels) == 1:
            lines.append(
                f"需要重点点评的选手是 {labels[0]}，数据中已标记为「焦点玩家」。"
            )
        else:
            lines.append(
                f"需要重点点评的选手有 {len(labels)} 位，数据中均已标记为「焦点玩家」："
                + "、".join(labels)
                + "。请在「焦点玩家点评」一节里为他们逐一单独点评。"
            )
    lines.append("")

    lines.append(
        build_match_data_text(
            match,
            heroes,
            items,
            ids,
            abilities=abilities,
            curve_ids=curve_ids,
        )
    )

    lines.append("")
    lines.append("=== 比赛质量客观指标 ===")
    lines.append(match_quality_block(match))

    if extra_context:
        lines.append("")
        lines.append("=== 附加上下文 ===")
        lines.append(extra_context)

    lines.append("")
    lines.append("=== 输出要求 ===")
    lines.append(MATCH_REPORT_FORMAT)
    return "\n".join(lines)


async def resolve_provider(context: Any, umo: str, provider_id: str = "") -> Any:
    """获取用于分析的模型提供商实例。

    Args:
        context: AstrBot 的 ``Context``。
        umo: unified_msg_origin，用于解析当前会话使用的模型。
        provider_id: 指定提供商 ID，留空则使用会话默认提供商。

    Returns:
        Provider 实例；拿不到时返回 None。
    """
    provider_id = (provider_id or "").strip()
    if provider_id:
        try:
            provider = context.get_provider_by_id(provider_id)
            if provider:
                return provider
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dota2] 指定的模型提供商 {provider_id} 不可用: {e}")

    try:
        return await context.get_using_provider_async(umo=umo)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[dota2] 获取默认模型提供商失败: {e}")
        return None


async def call_llm(provider: Any, system_prompt: str, user_prompt: str) -> str:
    """调用大模型并返回纯文本结果。

    Raises:
        RuntimeError: 提供商为空或调用失败。
    """
    if provider is None:
        raise RuntimeError("没有可用的模型提供商")

    # 不同 AstrBot 版本 text_chat 的参数略有差异，这里做兼容降级
    try:
        response = await provider.text_chat(
            prompt=user_prompt, system_prompt=system_prompt
        )
    except TypeError:
        response = await provider.text_chat(prompt=user_prompt)

    text = getattr(response, "completion_text", "") or ""
    text = text.strip()
    if not text:
        raise RuntimeError("模型返回了空结果")
    return text


# ======================================================================
# 插件自带的 OpenAI 兼容通道
# ======================================================================
#: 常见服务商的默认接口地址，方便用户在配置里只填 key
PROVIDER_PRESETS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "siliconflow": "https://api.siliconflow.cn/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": "http://127.0.0.1:11434/v1",
}

#: 归一化 provider 名 → 预设 key 的别名表。
#: 用户可能写成「DeepSeek」「deep_seek」「深度求索」等，这里都收敛到 canonical 名。
PRESET_ALIASES: dict[str, str] = {
    "openai": "openai",
    "azure": "openai",
    "deepseek": "deepseek",
    "deep_seek": "deepseek",
    "深度求索": "deepseek",
    "moonshot": "moonshot",
    "kimi": "moonshot",
    "月之暗面": "moonshot",
    "dashscope": "dashscope",
    "qwen": "dashscope",
    "通义": "dashscope",
    "阿里": "dashscope",
    "zhipu": "zhipu",
    "glm": "zhipu",
    "智谱": "zhipu",
    "siliconflow": "siliconflow",
    "硅基流动": "siliconflow",
    "openrouter": "openrouter",
    "ollama": "ollama",
}


class LLMRequestError(RuntimeError):
    """自建通道调用失败（网络、鉴权、限流、返回体异常等）。"""


def normalize_base_url(raw: str, provider_hint: str = "") -> str:
    """把用户填的地址或服务商名归一成可用的 base_url。

    允许三种写法：
    * 完整地址（``https://api.deepseek.com/v1``）→ 原样去尾斜杠；
    * 只写服务商名（``deepseek`` / ``kimi``）→ 查 :data:`PROVIDER_PRESETS`；
    * 留空 → 结合 ``provider_hint`` 再查一次，仍无则回退 OpenAI 官方。

    末尾的 ``/chat/completions`` 会被剥掉，避免用户误把完整端点填进来。
    """
    value = (raw or "").strip()
    if value and "://" not in value:
        key = PRESET_ALIASES.get(value.lower().replace("-", "_").replace(" ", ""))
        key = key or PRESET_ALIASES.get((provider_hint or "").strip().lower())
        if key:
            return PROVIDER_PRESETS[key]

    if not value:
        key = PRESET_ALIASES.get((provider_hint or "").strip().lower())
        return PROVIDER_PRESETS.get(key, PROVIDER_PRESETS["openai"])

    value = value.rstrip("/")
    for suffix in ("/chat/completions", "/completions"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    return value.rstrip("/")


class OpenAICompatibleClient:
    """极简的 OpenAI 兼容 ``/chat/completions`` 客户端。

    只依赖标准库：插件因此不必新增 ``openai`` / ``aiohttp`` 之类的依赖，
    也不会和 AstrBot 自带的 HTTP 栈产生版本冲突。请求放在线程里跑，
    不阻塞事件循环。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "",
        model: str = "",
        *,
        timeout: float = 120.0,
        provider_hint: str = "",
        proxy: str = "",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = normalize_base_url(base_url, provider_hint)
        self.model = (model or "").strip()
        self.timeout = max(5.0, float(timeout))
        self.proxy = (proxy or "").strip()
        self.extra_headers = dict(extra_headers or {})

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def build_payload(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        """组装请求体。"""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt or ""},
                {"role": "user", "content": user_prompt or ""},
            ],
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = float(temperature)
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        return payload

    def _request_sync(self, payload: dict) -> str:
        """同步发一次请求（由 :meth:`chat` 放进线程池执行）。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            # 部分中转站会校验 UA，这里给一个常规值
            "User-Agent": "astrbot-plugin-dota2/1.4",
        }
        headers.update(self.extra_headers)

        request = urllib.request.Request(
            self.endpoint, data=body, headers=headers, method="POST"
        )
        # 按需挂代理：走 handler 而不是全局 ``urlopen``，避免影响其他插件的请求
        opener = None
        if self.proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler(
                    {"http": self.proxy, "https": self.proxy}
                )
            )
        send = opener.open if opener is not None else urllib.request.urlopen
        try:
            with send(request, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:400]
            except Exception:  # noqa: BLE001
                detail = ""
            raise LLMRequestError(
                f"HTTP {e.code} {e.reason}"
                + (f"：{detail}" if detail else "")
            ) from e
        except urllib.error.URLError as e:
            raise LLMRequestError(f"无法连接 {self.endpoint}：{e.reason}") from e
        except asyncio.TimeoutError as e:  # pragma: no cover - 由线程内抛出
            raise LLMRequestError(f"请求超时（{self.timeout:g}s）") from e

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise LLMRequestError(f"返回体不是合法 JSON：{raw[:200]}") from e

        return self.parse_response(data)

    @staticmethod
    def parse_response(data: dict) -> str:
        """从返回体里取出正文，兼容几种常见的字段布局。"""
        # 有些服务在 200 里塞 error
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise LLMRequestError(f"接口返回错误：{msg}")

        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            raise LLMRequestError(f"返回体里没有 choices：{str(data)[:200]}")

        first = choices[0] or {}
        message = first.get("message") or {}
        text = message.get("content")
        if text is None:
            # 少数实现把结果放在 text / delta 里
            text = first.get("text") or (first.get("delta") or {}).get("content")
        if isinstance(text, list):
            # 多模态返回：拼接其中的文本片段
            text = "".join(
                part.get("text", "")
                for part in text
                if isinstance(part, dict)
            )
        text = (text or "").strip()
        if not text:
            raise LLMRequestError("模型返回了空内容")
        return text

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """调用模型并返回正文。

        Raises:
            LLMRequestError: 缺少 key/model、网络异常或返回体不可解析。
        """
        if not self.api_key:
            raise LLMRequestError("未配置 API Key")
        if not self.model:
            raise LLMRequestError("未配置模型名称")

        payload = self.build_payload(
            system_prompt,
            user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return await asyncio.to_thread(self._request_sync, payload)
