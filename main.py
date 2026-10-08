"""AstrBot Dota2 数据查询助手插件入口。

功能总览：
1. 绑定玩家（昵称 / 32 位 account_id / 64 位 SteamID）；
2. 查询最近战绩；
3. 调用大模型分析近期表现与打法风格（默认最近 20 场，场次可调）；
4. 调用大模型深度复盘单场比赛（比赛走势、质量评估、十人点评）；
5. 监听玩家，比赛结束后自动推送一条「胜负 + KDA + 近期战绩对照」的短评到会话，支持解绑。

数据来源：OpenDota API（https://docs.opendota.com/）
"""

import asyncio
import functools
import inspect
import json
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, NamedTuple

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

try:  # 插件目录被作为包加载时的相对导入
    from .dota_analyzer import (
        LLMRequestError,
        OpenAICompatibleClient,
        ProviderToolClient,
        WATCH_COMMENT_SYSTEM_PROMPT,
        build_hero_pick_prompt,
        build_recent_analysis_prompt,
        build_single_match_analysis_prompt,
        build_watch_comment_prompt,
        call_llm,
        resolve_provider,
    )
    from .dota_api import (
        FallbackDataSource,
        OpenDotaClient,
        OpenDotaError,
        TargetNotFoundError,
        to_account_id,
        to_steam_id64,
    )
    from .dota_stratz import StratzClient
    from .dota_format import (
        build_match_data_text,
        fmt_ago,
        fmt_duration,
        fmt_wan,
        format_hero_meta_board,
        format_hero_meta_footer,
        format_match_list,
        format_player_profile,
        format_summary_block,
        hero_meta_note,
        hero_meta_rows,
        hname,
        match_position,
        mode_text,
        normalize_focus_ids,
        parsed_state,
        pick_heroes_for_player,
        result_text,
        rank_text,
        summarize_hero_history,
        summarize_matches,
    )
    from .dota_store import DotaStore
    from . import dota_chat
    from . import dota_cron
    from . import dota_history
    from . import dota_nlu
    from . import dota_parse
    from . import dota_pool
    from . import dota_schedule
    from . import dota_tools
    from . import dota_zh
except ImportError:  # 兜底：以普通模块方式加载时（把插件目录加入 sys.path）
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dota_analyzer import (  # type: ignore[no-redef]
        LLMRequestError,
        OpenAICompatibleClient,
        ProviderToolClient,
        WATCH_COMMENT_SYSTEM_PROMPT,
        build_hero_pick_prompt,
        build_recent_analysis_prompt,
        build_single_match_analysis_prompt,
        build_watch_comment_prompt,
        call_llm,
        resolve_provider,
    )
    from dota_api import (  # type: ignore[no-redef]
        FallbackDataSource,
        OpenDotaClient,
        OpenDotaError,
        TargetNotFoundError,
        to_account_id,
        to_steam_id64,
    )
    from dota_stratz import StratzClient  # type: ignore[no-redef]
    from dota_format import (  # type: ignore[no-redef]
        build_match_data_text,
        fmt_ago,
        fmt_duration,
        fmt_wan,
        format_hero_meta_board,
        format_hero_meta_footer,
        format_match_list,
        format_player_profile,
        format_summary_block,
        hero_meta_note,
        hero_meta_rows,
        hname,
        match_position,
        mode_text,
        normalize_focus_ids,
        parsed_state,
        pick_heroes_for_player,
        result_text,
        rank_text,
        summarize_hero_history,
        summarize_matches,
    )
    from dota_store import DotaStore  # type: ignore[no-redef]

    import dota_chat  # type: ignore[no-redef]
    import dota_cron  # type: ignore[no-redef]
    import dota_history  # type: ignore[no-redef]
    import dota_nlu  # type: ignore[no-redef]
    import dota_parse  # type: ignore[no-redef]
    import dota_pool  # type: ignore[no-redef]
    import dota_schedule  # type: ignore[no-redef]
    import dota_tools  # type: ignore[no-redef]
    import dota_zh  # type: ignore[no-redef]

try:
    # AstrBot 用 GreedyStr 标记「接收剩余全部文本」的参数
    from astrbot.core.star.filter.command import GreedyStr
except Exception:  # pragma: no cover

    class GreedyStr(str):  # type: ignore[no-redef]
        """兼容兜底：退化为普通字符串。"""


PLUGIN_NAME = "astrbot_plugin_dota2"

#: pending 队列的处理节拍（秒）。必须显著小于 watch_interval，
#: 否则 ``next_try_at`` 设成 60s 也要等满一个轮询周期才会被兑现。
PENDING_TICK_SECONDS = 30

#: 监听推送时，最多为几位焦点玩家附带「近期战绩」上下文。
#: 同一局里被监听的玩家越多，这几段统计就会把提示词撑得越长，因此设上限。
MAX_FOCUS_RECENT_CONTEXT = 4

#: 命令行里分隔多个焦点玩家的分隔符。
#: 昵称本身可以含空格（例如「你刚才确实说了【钢板】对吧」），因此**不能**
#: 用空格分隔，必须用顿号 / 逗号 / 分号 / 竖线 / 斜杠这类显式分隔符。
FOCUS_SPLIT_RE = re.compile(r"[、,，;；|/]+")

#: 需要在**每个群单独开通**主动消息权限的平台。
#:
#: 这些平台走官方开放平台接口，主动消息不是「默认不行」，而是**要逐群开**：
#: 没开时平台会回 `40034105 主动消息失败, 无权限`。因此添加监听时必须把
#: 「去哪开」写清楚，否则用户只看到推送失败、不知道该动哪里。
#:
#: ⚠️ 别写成「这些平台不支持主动推送」——那是错的。本插件曾因为这句话，
#: 把一个「群里没开开关」的问题排查了两天，还顺带去查了账号掉线与风控。
PLATFORMS_NEEDING_PROACTIVE_SETUP = {
    "qq_official": "QQ 官方机器人",
    "qq_official_webhook": "QQ 官方机器人(Webhook)",
}

# ======================================================================
# 「会话已不可达」判定
# ======================================================================
#: 发送失败时，若错误信息里出现这些字样，说明**不是**临时故障，而是 bot
#: 被移出群聊 / 被拉黑 / 账号被封 —— 这类情况重试多少次都不会恢复，
#: 该会话下的监听留着只会让后台一直白跑（白白消耗大模型配额）。
#:
#: 匹配刻意从严：误删用户的监听是不可逆的，宁可漏判（还有「推送失败上限」
#: 兜底，最多白试几次），也不能因为一句含糊的报错就清掉整个群的监听。
UNREACHABLE_HINTS: tuple[tuple[str, str], ...] = (
    # ——— 被移出群聊 / 群已解散 / bot 不是群成员 ———
    ("不是群成员", "bot 已不是该群成员"),
    ("不在该群", "bot 已不在该群"),
    ("不在群内", "bot 已不在该群"),
    ("已被移出", "bot 已被移出群聊"),
    ("已被踢出", "bot 已被踢出群聊"),
    ("被踢出群", "bot 已被踢出群聊"),
    ("群不存在", "群聊不存在"),
    ("群聊不存在", "群聊不存在"),
    ("群已解散", "群聊已解散"),
    ("group not found", "群聊不存在"),
    ("not in group", "bot 已不在该群"),
    ("not a member", "bot 已不是该群成员"),
    ("is not a member of", "bot 已不是该群成员"),
    # ——— 被拉黑 / 不再是好友 ———
    # 用「拉黑」而不是「被拉黑」：平台文案五花八门（对方已将你拉黑 /
    # 你已被拉黑 / 拉黑了该账号），统一按词根匹配。发送失败里出现「拉黑」
    # 基本只可能是这一种含义，放宽是安全的。
    ("拉黑", "已被对方拉黑"),
    ("黑名单", "已被对方拉黑"),
    ("不是好友", "已不是对方好友"),
    ("不是对方好友", "已不是对方好友"),
    ("请先添加对方为好友", "已不是对方好友"),
    ("添加对方为好友", "已不是对方好友"),
    ("blocked", "已被对方拉黑"),
    # ——— 账号层面被封禁 ———
    ("账号已被封禁", "账号被封禁"),
    ("已被封禁", "账号被封禁"),
    ("账号被限制", "账号被限制"),
)


def looks_unreachable(exc: BaseException | str | None) -> str | None:
    """判断一次发送失败是否是「会话已不可达」。

    Returns:
        命中时给出一句人话原因（用于日志），否则 ``None``。
    """
    if exc is None:
        return None
    text = str(exc) if not isinstance(exc, str) else exc
    if not text:
        return None
    lowered = text.lower()
    for hint, reason in UNREACHABLE_HINTS:
        if hint.lower() in lowered:
            return reason
    return None

# ======================================================================
# 「催解析 + 等解析」相关的常量
# ======================================================================
#: 单场查询里用来跳过等待的开关词：`/d2 单场 <id> skip`
PARSE_SKIP_WORDS = frozenset({"skip", "跳过", "不等", "直接", "fast", "快"})

#: 等待解析时的并发上限兜底（配置项 parse_max_concurrent 缺失时使用）。
#: 每个等待任务每分钟会查一次 ``/matches/{id}``，并发太高容易触发限流。
DEFAULT_PARSE_MAX_CONCURRENT = 3

#: 后台解析任务的登记表容量上限：超过后拒绝新的等待请求，
#: 避免群里刷屏式提交把内存和配额一起吃光。
MAX_PARSE_TASKS = 50


def _fmt_clock(seconds: float) -> str:
    """把秒数格式化成 ``1分30秒`` 这样的可读文本。"""
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    if minutes and secs:
        return f"{minutes}分{secs}秒"
    if minutes:
        return f"{minutes}分钟"
    return f"{secs}秒"


# ======================================================================
# 自然语言入口相关的常量
# ======================================================================
#: 确认 / 取消词（命中即认为用户回的是确认或放弃）
NLU_CONFIRM_WORDS = frozenset(
    {"确认", "确定", "是的", "对", "对的", "ok", "okay", "好", "好的", "嗯", "yes", "y"}
)
NLU_CANCEL_WORDS = frozenset(
    {"取消", "不用了", "算了", "不", "不要", "否", "no", "n", "别"}
)

#: 等待确认的超时时间（秒）
NLU_CONFIRM_TTL = 120

#: 看到这些开头的消息一律跳过：那是别的插件 / 本插件的指令，不该被截胡。
NLU_SKIP_PREFIXES = ("/", "／", "!", "！", "#", "。", "~", "～")

#: 唤醒词默认值。自然语言入口是个「全局监听器」，不加约束会抢答别的插件的
#: 对话（尤其群里），因此默认要求消息里出现这个词才认。
NLU_DEFAULT_KEYWORD = "dota2助手"


class NluGate(NamedTuple):
    """唤醒词闸门的判定结果。

    自然语言入口需要区分两种情况：

    * 「放行」—— 这条消息不该由插件处理，原样交给别的插件与默认大模型；
    * 「因为命中唤醒词才放行」—— 用户已经**明确点名**了插件，只是没说清
      要什么。此时不该把消息扔回默认大模型（它看不到插件里的监听列表、
      绑定关系、战绩数据），而应该由插件带着数据自己回答，
      这就是「闲聊兜底」的触发条件。

    所以闸门除了返回剥离唤醒词后的正文，还要把 ``keyword_matched``
    一起带出来。
    """

    text: str
    keyword_matched: bool


#: 配置中未填写系统提示词时的兜底内容。
#:
#: ⚠️ 必须与 ``_conf_schema.json`` 里 ``analysis_system_prompt.default`` 保持一致 ——
#: 这一份只在配置被清空时兜底，平时根本不会走到，两边写法不一致会很难查。
DEFAULT_SYSTEM_PROMPT = (
    "你是一位资深的 Dota 2 分析师与教练，擅长从 OpenDota 的对局数据中读出比赛的"
    "真实走向、队伍决策与选手表现。\n\n"
    "严格遵守以下原则：\n"
    "1. 只依据我提供的数据进行推断，绝不编造数据中不存在的信息；数据不足时必须"
    "明确指出「数据不足，无法判断」。\n"
    "2. 每条结论都必须有数据支撑：拿到「同英雄分位」这类外部参照时要先给参照系再"
    "下判断，不要拿孤立绝对值夸人或骂人。但**报告是给人看的点评，不是数据表的"
    "复述** —— 结论从数据来，落笔时说人话；具体要不要引用数值，以各报告类型自己的"
    "写作要求为准（单场深度复盘要求：比分与时长这类宏观数字会写，单个选手的细节数值不写）。\n"
    "3. 语言简洁、专业、克制。不要写「综上所述」「值得注意的是」「希望对你有帮助」"
    "之类的填充语，也不要为了显得客观而堆砌数据。\n"
    "4. 使用中文输出，用 Markdown 小标题与短列表组织内容。篇幅按报告类型定："
    "单场深度复盘通常 1500~3000 字，其中焦点玩家点评必须逐项写足；"
    "其它类型的报告 800 字以内即可。\n"
    "5. 游戏术语保留通行英文写法（Gank、Farm、Roshan、TP、Buyback、Teamfight 等）。\n"
    "6. 点评选手时对事不对人，指出具体该做而没做的事；该给差评就给差评，"
    "不要为了好听而把问题说成优点。但「该做」要由**这个英雄在当局的定位与分工**来定，不要按号位套模板，也不要把「数据没给」当成「他没做」。"
)

HELP_TEXT = """🎮 Dota2 数据查询助手（数据来源：STRATZ / OpenDota 双源）

【账号】
/d2 绑定 <昵称 | 32位账号ID | 64位SteamID>　绑定你的账号
/d2 解绑　　　　　　　　　　　解除当前会话的绑定
/d2 我的　　　　　　　　　　　查看当前绑定
/d2 绑定列表　　　　　　　　　查看本会话所有绑定

【查询】
/d2 资料 [目标]　　　　　　　 查看玩家资料与段位
/d2 英雄 [目标]　　　　　　　 查看英雄使用统计
/d2 轮椅 [位置] [目标]　　　　 当前版本胜率最高的英雄（也就是「轮椅」）
　　　　　　　　　　　　　　　 加上昵称或「我」，会结合他的英雄池与近期
　　　　　　　　　　　　　　　 表现，从强势英雄里挑出适合他的；位置可填
　　　　　　　　　　　　　　　 核心 / 中单 / 三号位 / 辅助
/d2 战绩 [场次] [目标]　　　　 查看最近战绩（默认 20 场）
/d2 分析 [场次] [目标]　　　　 AI 分析近期表现与打法风格
/d2 单场 <比赛ID> [焦点玩家]　　AI 深度复盘单场比赛
　　　　（未解析时自动催解析并等待，最多 10 分钟；
　　　　　等满仍未解析就自动改用现有数据出一份基础数据版报告）
　　　　（加 skip 可跳过等待，立刻用现有数据出报告）
/d2 催解析 <比赛ID>　　　　　 只催 OpenDota 解析这局，不等结果

【监听】
/d2 监听 [目标]　　　　　　　 比赛结束后自动推送一条简短点评到本会话
/d2 取消监听 <目标 | 全部>　　取消你自己添加的监听
/d2 监听列表　　　　　　　　　查看本会话的监听

【定时任务】
/d2 定时　　　　　　　　　　　查看本会话的定时任务（也可以用「定时 列表」）
/d2 定时 <一句人话>　　　　　 直接描述你要的定时任务，例如：
　　　　　　　　　　　　　　　 `/d2 定时 每天早上七点，通报群里战绩情况`
　　　　　　　　　　　　　　　 `/d2 定时 每晚十点总结一下大家今天的表现`
　　　　　　　　　　　　　　　 `/d2 定时 每监听到十盘战绩就生成一份这十盘的总结`
　　　　　　　　　　　　　　　 `/d2 定时 明天早上八点通报一下战绩`
　　　　　　　　　　　　　　　 （说完会先给你一份计划核对，回「确认」才建）
/d2 定时 删 <编号>　　　　　　删除任务（编号见「定时 列表」）
/d2 定时 停 <编号>　　　　　　停用；`/d2 定时 开 <编号>` 恢复
/d2 定时 现在 <编号>　　　　　立刻执行一次

　也可以不打指令，直接说人话（需要唤醒词）：
　　`dota2助手 每天早上七点，通报群里战绩情况`
　　`dota2助手 每监听到十盘战绩就生成一份这十盘的总结`

　按时间的任务会出现在 AstrBot 的「未来任务」页面里，可以在那儿改时间、
　停用或删除；按场次的任务（「每监听到 N 场」）不走定时器，攒够场次就发。

【模型】
/d2 模型测试　　　　　　　　　自检插件专用的大模型 Key 是否可用

【数据源】
/d2 数据源　　　　　　　　　　自检主/后备数据源的连通性与降级状态

说明：
· 「目标」可以填昵称、32 位账号 ID 或 64 位 SteamID；
· 不填「目标」时，默认使用你在当前会话绑定的账号；
· 一局比赛只点评一次：若这场比赛里有多位被监听的玩家，会在一段短评里逐一点到；
· 监听推送只给「胜负 + KDA + 近期战绩对照」的几句话，不占用录像解析、不用等；
  要看深度复盘（走势、团战、出装）请用 `/d2 单场 <比赛ID>`；
· 「绑定列表」中他人的账号与用户 ID 默认打码，保护群聊隐私；
· 定时任务只接跟 Dota2 数据有关、且带时间或场次的说法；「每天七点提醒我喝水」
  这类纯提醒插件做不了，请用别的方式；
· 想改定时任务的时间，去 AstrBot 的「未来任务」页面更直观（也可以先删后建）；
· 想让查询结果更好看，可以在配置中调整「长报告转为图片发送」。

关于数据源（STRATZ 主 / OpenDota 后备）：
· 默认以 STRATZ 为主数据源，OpenDota 作为后备；主源失效（未配置、鉴权失败、
  限流、网络异常）时会自动切到后备，不需要人工干预；
· STRATZ 的优势是经济数据（GPM/XPM/正补/伤害）一次查询就全带回来，
  没有 OpenDota「最近比赛里只有 20 场有经济数据」的限制；
· 想启用 STRATZ，去 https://stratz.com/api 生成令牌，填进插件配置的
  「STRATZ API Key」。留空则完全跳过 STRATZ、只用 OpenDota，功能不受影响；
· 注意 STRATZ 令牌会绑定首次调用的出口 IP，换网络后可能临时 403，
  插件会自动重试；持续失败会降级到 OpenDota；
· 在配置里把「数据源优先级」改成 opendota，可以两者对调（OpenDota 主 / STRATZ 后备）；
· 随时用 `/d2 数据源` 查看当前主子源、连通性与是否发生过降级。

关于 AI 模型（可选，不影响其他功能）：
· 默认用 AstrBot 里配置的模型来写分析报告；
· 也可以在插件配置里填「专用 API Key」，让本插件的报告单独走一个通道
  （比如用更便宜的模型）。只填 Key 也能用——「接口地址」直接填服务商名
  （deepseek / kimi / qwen / zhipu / siliconflow / openrouter）会自动补全；
· 填完用 `/d2 模型测试` 自检一次，会回显接口地址、模型名、耗时和具体错误；
· 配了专用 Key 但调用失败时，默认会自动回退到 AstrBot 的模型。

关于解析（AI 复盘质量的关键）：
· OpenDota 收录一场比赛 ≠ 解析完这场比赛的录像。只有解析完成才有逐分钟
  经济、团战、出装这些数据，AI 复盘才有质量；
· `/d2 单场 <比赛ID>` 遇到未解析的局会自动提交解析申请，之后每分钟检查一次，
  解析完成就把报告发到本会话，最多等 10 分钟；**等满 10 分钟仍未解析时不会空手而归**
  —— 会自动改用现有数据出一份基础数据版报告（报告头部会标注数据完整度），
  等解析好之后再发一次 `/d2 单场 <比赛ID>` 就能拿到完整版；
· 不想等就加 `skip`：`/d2 单场 <比赛ID> skip` 立刻用基础数据出报告；
· 想先排上队、晚点再看，用 `/d2 催解析 <比赛ID>`。

自然语言（不用记指令）：
· 直接说人话也能用，但**开头要带上唤醒词「dota2助手」**，例如
  「dota2助手 帮我看看我的战绩」「dota2助手 分析一下天鸽最近的发挥」
  「dota2助手 这局 8993438099 复盘一下」「dota2助手 最近 20 把打得怎么样」；
· 唤醒词是为了避免抢答其他插件和群里的闲聊：消息里没有这个词，插件一律不响应
  （可在配置中改词或关掉这个限制）；
· 忽略大小写，中间多打空格也认（「Dota2 助手」）；
· 少了唤醒词也不会打断你——插件什么都不做，消息照常交给默认大模型；
· 群聊里不写唤醒词时，仍需 @ 机器人 才会响应（可在配置中关闭这个限制）；
· 群里说「dota2助手 绑定 86745912」这类会改动数据的操作时，插件会先确认再执行；
· 确认 / 取消可以直接回「确认」「取消」，不需要再带唤醒词；
· 写了唤醒词但没识别出具体指令时，插件会**带着本会话的真实数据接着聊**，例如
  「dota2助手 对比一下目前监听的几个人谁最菜」（会读出监听名单与各人近期战绩）、
  「dota2助手 我想转辅助，该怎么练」（会结合你自己的英雄池与近期表现给建议）。
  这类回答同样需要模型可用；用 `/d2 帮助` 里列出的指令能拿到更结构化的报告。"""


def _log_incoming_command(
    handler_name: str, event: AstrMessageEvent, args: tuple = ()
) -> None:
    """把「收到某条指令」记进 AstrBot 日志（**在执行之前**记录）。

    作用是把日志变成一条完整时间线：先看到「收到指令」，再看后面这条指令
    自己打出来的拉取/推送日志，排障时不用猜「这次到底有没有收到请求」。

    刻意做成模块级纯函数（不依赖 plugin 实例）：``take_over_event`` 也会被
    测试用在假对象上，拿 ``self`` 取属性会平白引入一条崩溃路径。

    只取 ``message_str`` 前 120 字，避免有人贴一长串内容把日志撑爆；
    其余字段全部 ``getattr`` 兜底，缺了也不影响指令执行。
    """
    try:
        umo = str(getattr(event, "unified_msg_origin", "") or "-")

        raw = " ".join(str(getattr(event, "message_str", "") or "").split())
        shown = raw[:120] + ("…" if len(raw) > 120 else "")

        uid = ""
        sender_getter = getattr(event, "get_sender_id", None)
        if callable(sender_getter):
            try:
                uid = str(sender_getter())
            except Exception:  # noqa: BLE001 - 取不到发送者不影响主流程
                uid = ""

        extra = ""
        if args:
            arg_text = " ".join(str(args[0]).split())
            extra = f"｜参数「{arg_text[:80]}」"

        logger.info(
            f"[dota2] 收到指令 {handler_name}｜会话 {umo}｜用户 {uid or '-'}"
            f"｜原文「{shown}」{extra}"
        )
    except Exception as e:  # noqa: BLE001 - 记日志失败绝不能影响指令本身
        logger.debug(f"[dota2] 记录指令日志失败（可忽略）：{e}")


def take_over_event(func=None, *, declinable: bool = False):
    """接管事件：直发回复 + （确实接管时）终止事件传播。

    AstrBot 的流水线是「洋葱模型」：handler 每 ``yield`` 一条结果，后续
    阶段（含默认大模型的请求阶段）就会被执行一次。本插件的指令往往要先
    yield 一条「⏳ 正在拉取…」再花十几秒拉数据，如果不做处理，默认大模型
    就会在这个空档里插嘴，编造出「没有绑定成功，无法读取数据。」这类
    插件代码里根本不存在的话术，最后还会在真正的分析结果之前先冒出来。

    因此这里做两件事：

    1. **直发**：把 handler yield 出来的结果通过 ``event.send()`` 直接投递
       （这正是 AstrBot 自己的 RespondStage 使用的通道），不再回灌流水线。
       这样插件执行期间流水线里不会产生任何额外的执行机会。
    2. **终止传播**：handler 接管成功后调用 ``event.stop_event()``，阻止流水线
       继续走到默认大模型阶段。

    两步都做了降级：运行时没有 ``event.send`` 或结果上没有 ``chain`` 时，
    自动退回为正常 ``yield``，保证在老版本 / 自定义 Event 上也能出消息。

    Args:
        declinable: handler 是否**可能主动放弃**某条消息。

            * ``False``（默认）：handler 一旦被调用就必然要接管。适用于
              ``@filter.command`` 这类「只有匹配上才会被调用」的 handler，
              它们无论有没有产出结果，都不该让默认大模型再来插一句。
            * ``True``：handler 可能看一眼后判断「这条不是给我的」而直接
              ``return``。此时**只有真的产出过结果才算接管**；一条都没产出
              就**绝不能** ``stop_event()``。

    为什么 ``declinable`` 必须存在（这是踩过的坑）：AstrBot 的
    ``StarRequestSubStage`` 派发 handler 时是这样的 ——

    .. code-block:: python

        for handler in activated_handlers:
            if event.is_stopped():
                break          # ← 循环直接结束
            async for ret in call_handler(event, handler.handler, **params):
                yield ret
            if event.is_stopped():
                break

    也就是说，**任何一个** handler 调用了 ``stop_event()``，排在它后面的
    handler 全部被跳过；同时 ``ProcessStage`` 里的
    ``event.get_result() and not event.is_stopped()`` 也会变成假，
    默认大模型同样不会被请求。

    对一个挂在 ``EventMessageType.ALL`` 上、**每条消息都会跑一遍**的监听器
    （如 :meth:`Dota2Plugin.d2_natural`）来说，无条件 ``stop_event()``
    等于「只要插件开着，别人说什么都别想被回复」—— 既掐掉其他插件，
    也掐掉正常闲聊。所以这类 handler 必须声明 ``declinable=True``。
    """

    def decorate(func):
        @functools.wraps(func)
        async def wrapper(self, event: AstrMessageEvent, *args, **kwargs):
            # 指令留痕：``declinable=False`` 的 handler 就是 ``@d2.command``
            # 注册的真指令 —— 它们一被调用就必然接管，所以这里等价于
            # 「收到 /d2 xxx 立刻记一笔，再执行」。
            #
            # 挂在装饰器里而不是逐个 handler 里，是为了不漏：新增指令时不必
            # 记得加日志。同时它天然覆盖「自然语言委派」这条路径 ——
            # ``d2_natural`` 识别出意图后会调用同一个被包装的 handler，
            # 于是日志里能看到「自然语言识别 → 收到指令 d2_match」两步。
            #
            # ``declinable=True`` 只有全局自然语言监听器在用，它每条消息都会
            # 跑一遍，记在这里会让日志被闲聊刷满，因此明确跳过。
            if not declinable:
                _log_incoming_command(func.__name__, event, args)

            #: 是否真的接管了这条消息（产出过至少一条结果）。
            handled = False
            try:
                async for result in func(self, event, *args, **kwargs):
                    handled = True
                    if not await self._deliver_now(event, result):
                        yield result
            finally:
                # 三种情况说明「这条消息归我管」，必须终止传播：
                #   1. 我产出过结果（``handled``，含直发成功的那些）；
                #   2. 我不是可放弃的 handler —— 被调用就意味着接管；
                #   3. 事件在我收尾时已经是停止态 —— 说明我**委托**出去的
                #      内部指令 handler 已经接管并终止了传播。
                # 只有「可放弃 + 什么都没产出 + 事件仍在传播」才是真的放弃，
                # 此时必须原样放行，否则会连带掐死其他插件与默认大模型。
                if handled or not declinable or event.is_stopped():
                    try:
                        event.stop_event()
                    except Exception as e:  # noqa: BLE001 - 老版本没有该 API
                        logger.debug(f"[dota2] 终止事件传播失败（可忽略）: {e}")

        return wrapper

    # 兼容两种写法：``@take_over_event`` 与 ``@take_over_event(declinable=True)``
    if func is not None:
        return decorate(func)
    return decorate


class Dota2Plugin(Star):
    """Dota2 数据查询助手。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.data_dir = self._resolve_data_dir()
        self.store = DotaStore(self.data_dir)
        self.store.load()
        #: **会话对话历史**（用户说的 + 机器人说的 + 报告类产出），按 umo 分开存。
        #:
        #: 它解决的是「聊着聊着就接不上了」：以前历史里**只有用户自己说的话**
        #: （机器人回复从未被记进去），报告类产出（复盘报告 / 定时播报 / 监听
        #: 推送）也一条都不进，而且只存内存、热重载即清空。详见
        #: :mod:`dota_history`。
        self.history = dota_history.ChatHistoryStore(
            self.data_dir,
            enabled=bool(self.cfg("nlu_history_enabled", True)),
            ttl_hours=float(self.cfg("nlu_history_ttl_hours", 12) or 12),
        )
        self.history.load()
        self.api = self._build_data_source()
        #: 中文名服务：本地对照表 + 磁盘缓存 + 缺词补译。
        #:
        #: 数据源只给英文名，而推送与战报要全中文。查名顺序是「包内静态表 →
        #: 磁盘缓存 → 联网补译」，命中就返回，不会每局都去问一次网络。
        self._zh = dota_zh.ZhNames(
            self.data_dir / "zh_names.json",
            enabled=bool(self.cfg("zh_names", True)),
            learn=bool(self.cfg("zh_learn_missing", True)),
        ).load()
        #: 等待解析 / 等待推送的比赛：``{match_id: pending_item}``
        #:
        #: 键就是 ``match_id``：**一局比赛只分析一次**。同一局里可能有多位被
        #: 监听的玩家参战（群聊里这种情况很常见），他们共用这一项，分析只生成
        #: 一份、每个会话只推送一条；各位焦点玩家在 ``focuses`` 里各占一项，
        #: 报告中对每个人分别给出深入数据，不会互相覆盖。
        self._pending: dict[int, dict[str, Any]] = {}
        self._watch_task: asyncio.Task | None = None
        self._stopping = False
        self._next_poll_at = 0.0
        #: 已完成的轮询轮次（只用于日志：一眼能看出「监听循环还在不在跑」）。
        #:
        #: 之所以需要它：轮询成功且没有新比赛时，监听循环**一条日志都不打**
        #: （STRATZ 客户端走标准库 urllib，不产生 httpx 访问日志），于是
        #: 「日志十分钟没动静」既可能是正常静默、也可能是循环卡死，无法区分。
        #: 有了轮次号，只要它还在涨就说明循环是活的。
        self._poll_round = 0
        #: 推送失败计数：``{match_id: {umo: 连续失败次数}}``。
        #:
        #: **这张表必须活在 pending 项之外。** pending 项在 :meth:`_finish` 里
        #: 会被 pop 掉，而推送失败的会话基线不推进，下一轮会被重新发现、
        #: 重新 :meth:`_queue_pending` —— 那会造出一个全新的 item。
        #: 早先把计数挂在 item 上（``deliver_attempts``），于是每重建一次就
        #: 归零，bot 账号掉线时表现为「后台无限重试」：
        #: 重新入队 → 归零 → 再试满上限 → 再归零 …… 永不终止。
        #:
        #: 按 ``(match_id, umo)`` 计数而不是整场计数，是因为掉线的通常是**某个
        #: 平台/会话**，另一个会话可能一切正常——不该让一个掉线的会话拖着整场
        #: 比赛不放，也不该因为别人成功就让它无限重试。
        #:
        #: 整场比赛结束（:meth:`_finish`）或被队列淘汰时按 match_id 整体清除，
        #: 因此不会无限增长。
        self._watch_failures: dict[int, dict[str, int]] = {}
        #: 短评正文缓存：``{match_id: 正文}``。
        #:
        #: 短评在 :meth:`_deliver` 里是「先生成、后发送」，而重试走的是**整个**
        #: :meth:`_deliver` —— 不缓存的话，一次发送失败就要重新调一次大模型。
        #: 线上实测：一晚 7 场比赛各试满 10 次上限，约 70 次大模型调用全部
        #: 浪费在同一个结果上（生成完就扔）。
        #:
        #: 与 :attr:`_watch_failures` 同理必须挂在实例上：pending 项每次
        #: 「重新入队」都是新对象，挂在它上面的缓存活不过一轮。
        #: 清理时机与失败计数完全一致（见 :meth:`_clear_watch_failures`），
        #: 因此不会无限增长。
        self._watch_comments: dict[int, str] = {}
        #: 各会话最近一次发送失败的原因摘要：``{umo: "异常类型: 消息"}``。
        #:
        #: 只用于让「放弃推送」的日志说清**为什么**失败——早先那句
        #: 「bot 账号可能已掉线」是写死的猜测，实测里真正的原因是平台
        #: 无主动消息权限（40034105），会把排查方向整个带偏。
        #: 按会话存（数量 = 会话数，很小），成功发送或会话不可达时清除。
        self._last_send_error: dict[str, str] = {}
        #: 会话场景记录：``{umo: "group" | "channel" | "friend"}``，**落盘**。
        #:
        #: 这份记录是为了修一个「日志说推送成功、群里却什么都没有」的坑：
        #: QQ 官方适配器给「群消息主动发送」设了一道闸门 —— 必须知道这个会话
        #: 是群（``_session_scene[会话] == "group"``）才允许发，它要靠这个区分
        #: ``group_openid`` 与频道 ``channel_id``。而那份内存字典**只在收到入站
        #: 消息时被写入**，且随进程重启清空。于是「重启后群里还没人说话」时，
        #: 适配器会打印 ``skip send_by_session`` 后**直接返回**（不抛异常、
        #: 也不返回假值），:meth:`_send` 拿到的是 True —— 插件记「已推送」，
        #: 群里一条都没有。
        #:
        #: 所以这里自己记一份**入站时观测到**的场景并落盘：重启后即使群里
        #: 静悄悄，也能把适配器缺的那条记录补回去。只记观测结果，不做猜测。
        self._scene_records: dict[str, str] | None = None
        #: 已经补登记并打过日志的会话，仅用于避免日志刷屏。
        self._scene_seeded: set[str] = set()
        #: 自然语言确认状态：``{(umo, uid): (intent_name, args, 过期时间戳)}``
        #: 群里说「绑定 xxx」这类多义指令时，先记下来等用户确认再执行。
        self._nlu_confirm: dict[tuple[str, str], tuple[str, str, float]] = {}
        #: 会话语境：每个会话最近提到过的比赛，``{umo: deque[{match_id, desc, ts}]}``。
        #:
        #: 用户说「详细分析这一盘」时，光看这一句话根本无从判断是哪一场——
        #: 必须知道这个会话刚刚推送 / 复盘过什么。这里记录的就是那份语境：
        #: 监听推送、单场复盘、战绩列表都会往里记一笔，按时间由新到旧排列。
        #:
        #: 只放在内存里：重启后语境清空是合理的（「这一盘」本来就是会话内的
        #: 概念，隔天再问没有意义）。
        self._nlu_recent_matches: dict[str, deque[dict]] = {}
        #: 会话语境：每个会话最近几轮对话。
        #:
        #: 实体是 :attr:`history`（可落盘、带 TTL 与预算，见 :mod:`dota_history`）；
        #: 这里**故意不再留第二份内存副本** —— 两套记录一定会分叉，而分叉的
        #: 表现就是「有时候接得上、有时候接不上」。读写一律走
        #: :meth:`_nlu_log_line` / :meth:`_nlu_chat_lines` 两个薄委托。
        #: 闲聊兜底用的战绩快照缓存：``{(account_id, limit): (时间戳, matches)}``。
        #: 由插件实例持有、跨会话复用；里面只放**纯数据**，快照对象每次新建
        #: —— 同一个账号在 A 群是「本人」、在 B 群是「被监听」，
        #: 直接把快照对象缓存起来会让两个会话互相污染。
        self._chat_cache: dict[tuple[int, int], tuple[float, list[dict]]] = {}
        #: 正在等待解析的后台任务：``{(umo, match_id, sender_id): task}``
        #: 等待解析最长要十分钟，绝不能把 handler 挂在那里——AstrBot 的流水线
        #: 是洋葱模型，handler 每 yield 一次后续阶段就整体跑一遍。改为后台
        #: 任务等待、完成后主动推送到原会话。
        self._parse_tasks: dict[tuple[str, int, str], asyncio.Task] = {}
        #: 等待解析的并发闸门：同时等待的场次上限由配置决定
        self._parse_sem: asyncio.Semaphore | None = None
        self._parse_sem_size = 0
        #: AstrBot「未来任务」的桥接器（见 :mod:`dota_cron`）。
        #:
        #: 时间型任务一律建在 AstrBot 的任务库里（``basic`` 任务），好处是
        #: 它们会出现在 AstrBot 的「未来任务」页面里、可停用可改时间可删除，
        #: 而执行时跑的是**插件自己的 handler**（数据、口径、模型通道都是插件这套）。
        self.schedules = dota_cron.CronBridge(context)
        #: 定时任务待执行的计划：``{(umo, uid): 已解析好的任务请求}``。
        #:
        #: 建任务会**持续生效**（每天在那个点往群里发东西），误判的代价远大于
        #: 绑定 / 监听，所以先解析出完整计划、把「几点、发什么、发到哪」写给用户看，
        #: 回一句「确认」才真正建。计划只放在内存里，确认窗口只有两分钟。
        self._schedule_plans: dict[tuple[str, str], dict[str, Any]] = {}
        #: 插件是否已卸载。装卸期间若还有到点的任务被唤醒，
        #: handler 靠它判断该直接退出（不然会往一个已经停掉的会话推消息）。
        self._sched_stopped = False
        #: 「每 N 场总结」的后台任务。生成总结要调一次大模型（十几秒），
        #: **绝不能**卡在监听推送链路里 —— 那一轮还有别的会话要推。
        self._summary_tasks: set[asyncio.Task] = set()
        #: 启动接管定时任务的后台任务（幂等，见 :meth:`_start_schedule_adopt`）。
        self._sched_adopt_task: asyncio.Task | None = None
        logger.info(
            f"[dota2] 插件已加载，数据目录: {self.data_dir}，"
            f"监听: {'开启' if self.cfg('watch_enabled', True) else '关闭'}"
        )
        self._start_watcher()
        # 接管既有定时任务。**放在这里而不是 on_astrbot_loaded**：
        # 热重载只重跑 __init__，不会再触发启动钩子（原因见该方法注释）。
        self._start_schedule_adopt()

    # ==================================================================
    # 催解析 + 等解析（后台任务）
    # ==================================================================
    def _parse_semaphore(self) -> asyncio.Semaphore:
        """按配置返回等待解析的并发闸门（配置变了会自动重建）。"""
        size = max(1, int(self.cfg("parse_max_concurrent", DEFAULT_PARSE_MAX_CONCURRENT)))
        if self._parse_sem is None or self._parse_sem_size != size:
            self._parse_sem = asyncio.Semaphore(size)
            self._parse_sem_size = size
        return self._parse_sem

    def _parse_wait_options(self) -> dict[str, Any]:
        """从配置里读出等待解析的节奏参数。"""
        interval = max(
            dota_parse.MIN_CHECK_INTERVAL,
            int(self.cfg("parse_check_interval", dota_parse.DEFAULT_CHECK_INTERVAL)),
        )
        timeout = max(
            interval,
            int(self.cfg("parse_wait_timeout", dota_parse.DEFAULT_WAIT_TIMEOUT)),
        )
        return {"check_interval": interval, "timeout": timeout}

    def _parse_task_key(self, umo: str, match_id: int, uid: str) -> tuple[str, int, str]:
        return (str(umo), int(match_id), str(uid))

    def _start_parse_task(
        self,
        event: AstrMessageEvent,
        match_id: int,
        focus_ids: list[int],
        focus_names: dict[int, str],
        fresh: bool = True,
    ) -> bool:
        """把「催解析 + 等待 + 完成后复盘」交给后台任务执行。

        之所以不在这里直接 ``await``：handler 被 ``take_over_event`` 包装，
        而 AstrBot 的流水线会在 handler 每次 yield 时执行后续阶段。等待十分钟
        期间要保持连接、逐分钟 yield 进度，等于给流水线制造十次执行机会。
        后台任务 + ``context.send_message`` 主动推送则完全没有这个问题。

        Args:
            fresh: 是否重新拉取一份比赛数据作为「第一次状态检查」的依据。
                指令刚拉过数据时传 True 即可（省一次请求），后台任务里
                重拉一次更保险，因此内部仍以重新拉取为准。

        Returns:
            是否成功排入后台任务。
        """
        platform = ""
        try:
            platform = event.get_platform_name()
        except Exception:  # noqa: BLE001
            platform = ""
        return self._spawn_parse_task(
            umo=str(event.unified_msg_origin),
            uid=str(event.get_sender_id()),
            match_id=match_id,
            focus_ids=focus_ids,
            focus_names=focus_names,
            platform=platform,
        )

    def _spawn_parse_task(
        self,
        *,
        umo: str,
        uid: str,
        match_id: int,
        focus_ids: list[int],
        focus_names: dict[int, str],
        platform: str = "",
    ) -> bool:
        """``_start_parse_task`` 的核心部分（不依赖 event，供工具路径复用）。

        自然语言工具触发单场复盘时手上没有 handler 的 event，但仍要走同一条
        「催解析 → 等 → 出报告 → 发回会话」流水线；拆出这一层就是为了让两条
        路径共用它，而不是在工具侧再写一份（两套等的节拍迟早会不一致）。
        """
        key = self._parse_task_key(umo, match_id, uid)

        existing = self._parse_tasks.get(key)
        if existing is not None and not existing.done():
            return False
        if len(self._parse_tasks) >= MAX_PARSE_TASKS:
            logger.warning(
                f"[dota2] 等待解析的任务已达上限（{MAX_PARSE_TASKS}），"
                f"拒绝 {match_id} 的等待请求"
            )
            return False

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - handler 一定在事件循环里
            logger.error(f"[dota2] 无法排入等待解析任务 {match_id}：没有运行中的事件循环")
            return False

        task = loop.create_task(
            self._parse_and_analyze(
                umo=umo,
                uid=uid,
                match_id=int(match_id),
                focus_ids=[int(i) for i in focus_ids],
                focus_names={int(k): str(v) for k, v in (focus_names or {}).items()},
                platform=platform,
            )
        )
        self._parse_tasks[key] = task
        task.add_done_callback(
            lambda _t, _key=key: self._parse_tasks.pop(_key, None)
        )
        return True

    async def _parse_notify(self, umo: str, text: str) -> None:
        """向会话推送等待过程中的通知（失败只记日志，不影响任务）。"""
        try:
            for chunk in self._chunk_text(text):
                await self._send(umo, MessageChain().message(chunk))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[dota2] 推送解析进度失败（可忽略）: {e}")

    async def _parse_and_analyze(
        self,
        *,
        umo: str,
        uid: str,
        match_id: int,
        focus_ids: list[int],
        focus_names: dict[int, str],
        platform: str = "",
    ) -> None:
        """后台任务：催解析 → 每分钟检查 → 解析完成后出复盘报告。

        **超时不等于放弃**：等满配置的上限仍未解析时，只要手上还有这场比赛
        的基础数据，就自动降级出一份「未解析版」复盘（见
        :meth:`_parse_fallback_report`），而不是只回一句「放弃等待」。
        真拿不到数据、或用户主动取消等待时才只发收尾通知。

        无论成功、超时还是异常，都会给用户一个明确的收尾消息。
        """
        options = self._parse_wait_options()
        interval = int(options["check_interval"])
        timeout = int(options["timeout"])
        notify_progress = bool(self.cfg("parse_notify_progress", True))
        submit = bool(self.cfg("parse_submit_request", True))
        minutes = max(1, round(timeout / 60))

        async with self._parse_semaphore():
            try:
                result = await dota_parse.wait_for_parse(
                    self.api,
                    match_id,
                    check_interval=interval,
                    timeout=timeout,
                    submit=submit,
                    resubmit_every=dota_parse.RESUBMIT_EVERY,
                    on_submit=(
                        (lambda ok: self._on_parse_submit(umo, match_id, ok))
                        if submit
                        else None
                    ),
                    on_check=(
                        (lambda state, checks, elapsed: self._on_parse_check(
                            umo, match_id, state, checks, elapsed, minutes
                        ))
                        if notify_progress
                        else None
                    ),
                )
            except asyncio.CancelledError:
                logger.info(f"[dota2] 等待比赛 {match_id} 解析的任务被取消")
                raise
            except Exception as e:  # noqa: BLE001
                logger.error(
                    f"[dota2] 等待比赛 {match_id} 解析时出错：{e}", exc_info=True
                )
                await self._parse_notify(
                    umo,
                    f"❌ 等待比赛 {match_id} 解析时出错：{e}\n"
                    f"可以稍后重试 `/d2 单场 {match_id}`。",
                )
                return

        if not result.parsed:
            # 等不到了，但**不等于什么都不给**：用超时那一刻手上的数据直接出
            # 一份「未解析版」复盘（与 `/d2 单场 <id> skip` 同一条链路）。
            # 拿不到数据、或用户是主动取消等待时才退回 fail_text。
            if (
                self.cfg("parse_fallback_unparsed", True)
                and result.reason in dota_parse.FALLBACK_REASONS
                and await self._parse_fallback_report(
                    umo=umo,
                    match_id=match_id,
                    result=result,
                    focus_ids=focus_ids,
                    focus_names=focus_names,
                    limit_minutes=minutes,
                )
            ):
                return
            await self._parse_notify(umo, result.fail_text(match_id))
            return

        await self._parse_notify(
            umo,
            f"✅ 比赛 {match_id} 已解析完成（等待 {_fmt_clock(result.waited)}，"
            f"共检查 {result.checks} 次），开始生成复盘报告…",
        )
        await self._analyze_match_data(
            umo=umo,
            match_id=match_id,
            match=result.match,
            focus_ids=focus_ids,
            focus_names=focus_names,
        )

    async def _parse_fallback_report(
        self,
        *,
        umo: str,
        match_id: int,
        result: dota_parse.ParseWaitResult,
        focus_ids: list[int],
        focus_names: dict[int, str],
        limit_minutes: int = 0,
    ) -> bool:
        """等待解析超时后的兜底：不再干等，直接用现有数据出一份复盘。

        「等十分钟」解决的只是**数据完整度**，而不是「有没有复盘」——
        基础数据（KDA、英雄、时长、经济占比、出装顺序之外的队伍构成…）
        本来就在手，超时后用它出报告，比只回一句「放弃等待」有用得多。
        这条链路与 `/d2 单场 <id> skip` 完全一致，
        报告头部会照实标注「数据完整度：未解析」，不会拿基础数据冒充完整解析。

        Returns:
            是否真的产出了报告。返回 False 时调用方应发 ``fail_text``。
        """
        match = result.match if isinstance(result.match, dict) else None
        if not (match or {}).get("players"):
            # 超时那一刻手上没有可用数据（比如最后一次拉取正好失败）。
            # 再补一次：拿不到就只能如实告知，绝不硬凑一份空壳报告。
            try:
                match = await self.api.get_match(match_id)
            except OpenDotaError as e:
                logger.warning(
                    f"[dota2] 超时后补拉比赛 {match_id} 数据失败：{e}"
                )
                match = None
            except Exception as e:  # noqa: BLE001 - 兜底链路同样不能崩
                logger.warning(
                    f"[dota2] 超时后补拉比赛 {match_id} 数据出错：{e}"
                )
                match = None

        if not isinstance(match, dict) or not match.get("players"):
            logger.info(
                f"[dota2] 比赛 {match_id} 超时且拿不到数据，无法降级生成未解析版报告"
            )
            return False

        # 边界：超时判定与这次补拉之间正好解析好了 —— 那就别用旧数据糊弄，
        # 直接走完整复盘。
        if dota_parse.parse_state(match).parsed:
            logger.info(
                f"[dota2] 比赛 {match_id} 在超时收尾时刚好解析完成，改出完整复盘"
            )
            await self._parse_notify(
                umo,
                f"✅ 比赛 {match_id} 已解析完成（刚好赶在收尾前），开始生成完整复盘…",
            )
            await self._analyze_match_data(
                umo=umo,
                match_id=match_id,
                match=match,
                focus_ids=focus_ids,
                focus_names=focus_names,
            )
            return True

        logger.info(
            f"[dota2] 比赛 {match_id} 等待 {result.waited:.0f}s 仍未解析"
            f"（原因 {result.reason}），自动降级为未解析版复盘"
        )
        await self._parse_notify(
            umo, result.fallback_text(match_id, limit_minutes=limit_minutes)
        )
        await self._analyze_match_data(
            umo=umo,
            match_id=match_id,
            match=match,
            focus_ids=focus_ids,
            focus_names=focus_names,
        )
        return True

    async def _on_parse_submit(self, umo: str, match_id: int, ok: bool) -> None:
        """催解析申请结果的通知。"""
        if ok:
            await self._parse_notify(
                umo,
                f"📨 已向 OpenDota 提交比赛 {match_id} 的解析申请，正在队列中。",
            )
        else:
            await self._parse_notify(
                umo,
                f"ℹ️ 解析申请未返回排队凭据（可能已在队列中，或该局 OpenDota 无法解析）。"
                f"仍会继续等待比赛 {match_id} 的解析结果。",
            )

    async def _on_parse_check(
        self,
        umo: str,
        match_id: int,
        state: dota_parse.ParseState,
        checks: int,
        elapsed: float,
        limit_minutes: int,
    ) -> None:
        """每分钟的进度播报。"""
        left = max(0.0, limit_minutes * 60 - elapsed)
        await self._parse_notify(
            umo,
            f"⏳ 比赛 {match_id} 解析进度（第 {checks} 次检查，已等待 "
            f"{_fmt_clock(elapsed)}，剩余最多 {_fmt_clock(left)}）：{state.describe()}",
        )

    async def _analyze_match_data(
        self,
        *,
        umo: str,
        match_id: int,
        match: dict,
        focus_ids: list[int],
        focus_names: dict[int, str],
    ) -> None:
        """数据就绪后生成单场复盘并推送到会话。

        与 ``d2_match`` 共用同一套提示词构造逻辑，保证「等解析出来的复盘」
        与「直接查已解析比赛的复盘」完全一致。
        """
        try:
            heroes = await self._heroes()
            items = await self._items()
        except OpenDotaError as e:
            logger.error(f"[dota2] 复盘比赛 {match_id} 时拉取常量失败：{e}")
            heroes, items = {}, {}
        # 技能常量（id → 技能名）：拿不到就省略「技能加点」小节
        abilities = await self._ability_constants()

        focus_ids = [int(i) for i in (focus_ids or []) if int(i or 0)]
        parsed = dota_parse.parse_state(match).parsed
        headline = self.match_headline(
            match, heroes, focus_ids or None, parsed, focus_names
        )
        curve_ids = self._curve_targets(match, focus_ids)

        extra_context = await self._build_recent_context(
            match_id, focus_ids, focus_names, heroes
        )
        prompt = build_single_match_analysis_prompt(
            match=match,
            heroes=heroes,
            items=items,
            focus_account_ids=focus_ids or None,
            focus_names=focus_names or None,
            extra_context=extra_context,
            abilities=abilities,
            curve_ids=curve_ids,
        )
        report = await self._generate_report_for_umo(umo, prompt)

        await self._parse_notify(umo, headline)
        if report:
            image_url = await self._render_image(report)
            if image_url:
                try:
                    await self._send(umo, MessageChain().file_image(image_url))
                    return
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[dota2] 推送复盘图片失败，回退为文本：{e}")
            for chunk in self._chunk_text(report):
                await self._send(umo, MessageChain().message(chunk))
            return

        raw = (
            "⚠️ 未启用大模型分析或模型不可用，以下为从数据源获取的完整原始数据：\n\n"
            + build_match_data_text(
                match,
                heroes,
                items,
                focus_account_ids=focus_ids or None,
                abilities=abilities,
                curve_ids=curve_ids,
            )
        )
        for chunk in self._chunk_text(raw):
            await self._send(umo, MessageChain().message(chunk))

    async def _generate_report_for_umo(self, umo: str, prompt: str) -> str | None:
        """在指定会话下调用大模型生成报告（供后台任务使用）。"""
        return await self._call_report_llm(prompt, umo=umo)

    async def _ability_constants(self) -> dict[int, str]:
        """技能常量 ``{技能ID: 技能名}``，用于把解析产物里的加点顺序翻译成可读文本。

        这是**尽力而为**的增强项：只有 OpenDota 提供 ``/constants/ability_ids``
        （STRATZ 侧直接返回空字典，或者旧数据源根本没有这个方法）。
        拿不到时提示词会自动省略「技能加点」小节，不影响其余复盘内容。
        """
        getter = getattr(self.api, "get_ability_names", None)
        if getter is None:
            return {}
        try:
            raw = await getter()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[dota2] 获取技能常量失败，跳过技能加点：{e}")
            return {}
        return await self._localize_abilities(raw)

    # ==================================================================
    # 名称中文化
    # ==================================================================
    async def _localize_heroes(self, raw: dict[int, dict]) -> dict[int, dict]:
        """把英雄常量的显示名换成中文（只改 ``localized_name``）。"""
        return await self._localize("hero", raw, dota_zh.localize_heroes)

    async def _localize_items(self, raw: dict[str, dict]) -> dict[str, dict]:
        """把道具常量的显示名换成中文（只改 ``dname``）。"""
        return await self._localize("item", raw, dota_zh.localize_items)

    async def _localize_abilities(self, raw: dict[int, str]) -> dict[int, str]:
        """把技能常量的名字换成中文。"""
        return await self._localize("ability", raw, dota_zh.localize_abilities)

    async def _localize(self, _kind: str, raw: Any, apply_fn: Any) -> Any:
        """本地化的统一流程：先查本地表 → 缺词时补译 → 用原始数据重跑一次。

        第二步「用原始数据重跑」是必须的：第一次本地化只是把缺的键记下来，
        补译拿到新词后必须回到**未经本地化的数据**上再算一遍，否则第二次
        处理的是已经被替换过的值，缺词键就对不上了。
        """
        zh = self._zh
        if zh is None or not raw:
            return raw or {}
        out = apply_fn(raw, zh)
        if await zh.fill_missing(self._translate_terms):
            out = apply_fn(raw, zh)
        return out

    async def _heroes(self) -> dict[int, dict]:
        """英雄常量（已中文本地化）。"""
        try:
            raw = await self.api.get_heroes()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[dota2] 获取英雄常量失败：{e}")
            return {}
        return await self._localize_heroes(raw)

    async def _hero_pool(self, account_id: int) -> "dota_pool.HeroPool":
        """取英雄池（当前版本口径 + 含加速模式），配置从插件配置读。

        之所以收成一个方法：指令、轮椅的个人适配、近期分析、兜底对话四处
        都要用英雄池，各读各的配置很容易出现「同一个回答里两种口径」。
        """
        min_games = int(self.cfg("hero_pool_min_games", dota_pool.DEFAULT_MIN_GAMES) or 0)
        max_patches = int(self.cfg("hero_pool_max_patches", dota_pool.DEFAULT_MAX_PATCHES) or 0)
        include_turbo = self.cfg("hero_pool_include_turbo", True)
        patch_scope = self.cfg("hero_pool_patch_scope", True)
        return await dota_pool.collect_hero_pool(
            self.api,
            account_id,
            min_games=max(1, min_games),
            max_patches=max(1, max_patches),
            include_turbo=False if include_turbo is False else True,
            patch_scope=False if patch_scope is False else True,
            timeout=max(5.0, float(self.cfg("request_timeout", 30) or 30)),
        )

    async def _items(self) -> dict[str, dict]:
        """道具常量（已中文本地化）。"""
        try:
            raw = await self.api.get_items()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[dota2] 获取道具常量失败：{e}")
            return {}
        return await self._localize_items(raw)

    async def _translate_terms(self, kind: str, keys: list[str]) -> dict[str, str]:
        """缺词补译：问一次模型，返回 ``{英文键: 中文名}``。

        这里刻意复用报告用的模型通道（专用 Key 优先、失败回退 AstrBot 提供商），
        不另开一套配置；补译失败只影响名字显示，绝不阻断本次查询。
        """
        prompt = dota_zh.build_translate_prompt(kind, keys)
        text = await self._call_report_llm(
            prompt, system_prompt=dota_zh._TRANSLATE_SYSTEM
        )
        if not text:
            return {}
        return dota_zh.parse_translation(text)

    @staticmethod
    def _curve_targets(match: dict, focus_ids: list[int], limit: int = 2) -> list[int]:
        """挑出「即便没有焦点玩家也值得看发育节奏」的对象。

        没有指定焦点时（例如 `/d2 单场 <比赛ID>` 没写玩家），整场报告就没有
        任何人的逐分钟曲线，而「两边核心的发育速度」恰恰是复盘的关键。
        这里按净经济取前 ``limit`` 位补上；有焦点玩家时返回空列表，
        因为焦点自己的深入小节里已经有完整曲线了。
        """
        if focus_ids:
            return []
        players = [
            player
            for player in (match.get("players") or [])
            if isinstance(player, dict) and player.get("account_id")
        ]
        players.sort(
            key=lambda player: -(
                player.get("net_worth") or player.get("total_gold") or 0
            )
        )
        picked: list[int] = []
        for player in players[: max(0, int(limit))]:
            try:
                picked.append(int(player.get("account_id")))
            except (TypeError, ValueError):
                continue
        return picked

    async def _recent_summary_blocks(
        self,
        match_id: int,
        focus_ids: list[int],
        focus_names: dict[int, str],
        heroes: dict[int, dict],
    ) -> list[str]:
        """拼出几位焦点玩家「本场之外的近期战绩」文本块（每人一段）。

        用于两处：单场深度复盘的「附加上下文」，以及监听短评里判断
        「这局是不是正常发挥」的对照数据。
        """
        recent_count = max(0, int(self.cfg("watch_match_analysis_count", 10)))
        if not focus_ids or not recent_count:
            return []
        blocks: list[str] = []
        for focus_id in focus_ids[:MAX_FOCUS_RECENT_CONTEXT]:
            label = focus_names.get(focus_id) or focus_id
            try:
                recent_matches, economy_samples = (
                    await self.api.get_player_matches_enriched(focus_id, recent_count)
                )
                recent_matches = [
                    m
                    for m in recent_matches
                    if int(m.get("match_id") or 0) != int(match_id)
                ]
            except OpenDotaError as e:
                logger.debug(f"[dota2] 获取 {focus_id} 近期状态上下文失败: {e}")
                continue
            if not recent_matches:
                continue
            summary = summarize_matches(
                recent_matches, economy_samples=economy_samples
            )
            blocks.append(
                f"—— {label}（account_id={focus_id}）在本场之外最近 "
                f"{len(recent_matches)} 场的整体情况"
                f"（用于判断本场是他的正常发挥还是异常）：\n"
                + format_summary_block(summary, heroes)
            )
        return blocks

    async def _build_recent_context(
        self,
        match_id: int,
        focus_ids: list[int],
        focus_names: dict[int, str],
        heroes: dict[int, dict],
    ) -> str:
        """为单场深度复盘拼出「本场之外的近期状态」上下文块（含报告尾部要求）。"""
        blocks = await self._recent_summary_blocks(
            match_id, focus_ids, focus_names, heroes
        )
        if not blocks:
            return ""
        return "\n\n".join(blocks) + (
            "\n\n请在报告最后额外增加一节「## 近期状态」"
            + (
                "，为上面每位焦点玩家各起一个小标题，"
                "各用 3 句以内说明其最近的竞技走向。"
                if len(focus_ids) > 1
                else "，用 3 句以内说明这名玩家最近的整体竞技走向。"
            )
        )

    # ==================================================================
    # 基础设施
    # ==================================================================
    def cfg(self, key: str, default: Any = None) -> Any:
        """安全读取配置项。"""
        try:
            value = self.config.get(key, default)
        except Exception:  # noqa: BLE001
            return default
        return default if value is None else value

    @staticmethod
    def _resolve_data_dir() -> Path:
        """获取插件数据目录（优先使用 AstrBot 规范的 plugin_data 目录）。

        正常路径是 ``AstrBot/data/plugin_data/astrbot_plugin_dota2/``。取不到时
        回退到插件目录下的 ``data/``（本地开发、把插件直接放进 plugins/ 跑时
        会走到这里），该目录已被 .gitignore 排除，不会进版本库。

        回退不是静默的：``exc_info=True`` 把完整调用栈打进日志，否则这里
        只会留一句「回退到插件目录」，看到的人无从判断是路径权限、插件目录
        只读，还是 AstrBot 版本差异导致的 API 缺失。**排查失败原因请看这条
        warning 的堆栈**，修好环境后应重新加载插件让目录回到规范位置。
        """
        try:
            return Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[dota2] 无法获取标准数据目录（{type(e).__name__}: {e}），"
                f"回退到插件目录 data/。绑定关系等本机状态将存在插件目录下——"
                f"若 AstrBot 重装或清理插件目录会一并丢失，长期使用建议排查上方堆栈。",
                exc_info=True,
            )
            fallback = Path(__file__).resolve().parent / "data"
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback

    # ==================================================================
    # 数据源组装（主 STRATZ + 后备 OpenDota，自动降级）
    # ==================================================================
    #: 数据源优先级取值 → (主源名, 说明)。``stratz`` 为默认。
    DATA_SOURCE_CHOICES = ("stratz", "opendota")

    def _stratz_api_key(self) -> str:
        """读取 STRATZ API Key（去空白，兼容误粘贴换行）。"""
        return str(self.cfg("stratz_api_key", "") or "").strip()

    def _data_source_choice(self) -> str:
        """读取配置里的「数据源优先级」，非法值一律回落到 ``stratz``。"""
        choice = str(self.cfg("data_source_priority", "stratz") or "").strip().lower()
        return choice if choice in self.DATA_SOURCE_CHOICES else "stratz"

    def _source_name(self) -> str:
        """当前生效的数据源名，用于给用户看的提示文案。

        不要在提示里硬编码「OpenDota」——STRATZ 才是默认主源，
        写死会让用户拿着错误的线索去排查。
        """
        api = getattr(self, "api", None)
        label = getattr(api, "describe", None)
        if callable(label):
            try:
                text = str(label() or "").strip()
                if text:
                    return text
            except Exception:  # noqa: BLE001
                pass
        return "STRATZ" if self._data_source_choice() == "stratz" else "OpenDota"

    def _build_data_source(self):
        """按配置组装数据源。

        默认策略：**STRATZ 为主、OpenDota 为后备**。

        - ``stratz_api_key`` 没填时，STRATZ 会被视为「未配置」，
          所有请求直接落到 OpenDota（行为与升级前完全一致）。
        - ``data_source_priority`` 选 ``opendota`` 时，两者对调：
          OpenDota 为主、STRATZ 为后备。
        - 任一主源抛「不可用」类异常（未配置/鉴权/限流/网络）时，
          :class:`FallbackDataSource` 会自动切到后备并记一条 warning。
        """
        opendota = OpenDotaClient(
            api_key=self.cfg("opendota_api_key", ""),
            timeout=self.cfg("request_timeout", 30),
            max_retries=self.cfg("max_retries", 3),
            rate_limit_per_minute=self.cfg("rate_limit_per_minute", 55),
            proxy=self.cfg("http_proxy", ""),
        )
        stratz = StratzClient(
            api_key=self._stratz_api_key(),
            timeout=self.cfg("request_timeout", 30),
            max_retries=self.cfg("max_retries", 3),
            rate_limit_per_minute=self.cfg("stratz_rate_limit_per_minute", 240),
            proxy=self.cfg("http_proxy", ""),
        )

        if self._data_source_choice() == "opendota":
            primary, secondary = opendota, stratz
            primary_label, secondary_label = "OpenDota", "STRATZ"
        else:
            primary, secondary = stratz, opendota
            primary_label, secondary_label = "STRATZ", "OpenDota"

        source = FallbackDataSource(
            primary=primary,
            secondary=secondary,
            primary_label=primary_label,
            secondary_label=secondary_label,
        )
        if primary_label == "STRATZ" and not stratz.configured:
            logger.info(
                "[dota2] 未配置 STRATZ API Key，数据源将直接使用 OpenDota；"
                "填写「STRATZ API Key」后即可启用 STRATZ 主数据源。"
            )
        return source

    @staticmethod
    def _split_count_target(args: str) -> tuple[int | None, str]:
        """把 `20 天鸽` 拆成 ``(20, "天鸽")``；没有数量时返回 ``(None, args)``。"""
        tokens = [token for token in re.split(r"\s+", (args or "").strip()) if token]
        count: int | None = None
        if tokens and re.fullmatch(r"\d{1,3}", tokens[0]):
            value = int(tokens[0])
            if 1 <= value <= 200:
                count = value
                tokens = tokens[1:]
        return count, " ".join(tokens)

    def _chunk_text(self, text: str) -> list[str]:
        """按配置的长度上限把长文本切成多条消息。"""
        limit = max(200, int(self.cfg("max_message_length", 1500)))
        text = text or ""
        if len(text) <= limit:
            return [text]

        chunks: list[str] = []
        buffer: list[str] = []
        size = 0
        for line in text.split("\n"):
            # 单行就超长时先硬切
            while len(line) > limit:
                head, line = line[:limit], line[limit:]
                if buffer:
                    chunks.append("\n".join(buffer))
                    buffer, size = [], 0
                chunks.append(head)
            if size + len(line) + 1 > limit and buffer:
                chunks.append("\n".join(buffer))
                buffer, size = [], 0
            buffer.append(line)
            size += len(line) + 1
        if buffer:
            chunks.append("\n".join(buffer))
        return chunks

    async def _render_image(self, text: str) -> str | None:
        """把报告渲染成图片；未启用或失败时返回 None。"""
        if not self.cfg("analysis_as_image", True):
            return None
        try:
            return await self.text_to_image(text)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dota2] 文本转图片失败，回退为纯文本发送: {e}")
            return None

    async def _deliver_now(self, event: AstrMessageEvent, result) -> bool:
        """尽量把一条结果直接投递出去，成功返回 True。

        直接使用 ``event.send()`` 发送，绕过流水线（AstrBot 自己的
        RespondStage 也是走这个通道）。这样插件在执行期间不会给流水线
        制造新的执行机会，默认大模型也就没有插嘴的时机。

        ``event.send()`` 要的是 ``MessageChain``，也就是 ``result`` **本身**
        —— ``MessageEventResult`` 就是 ``MessageChain`` 的子类。它要的
        **不是** ``result.chain``：那是个普通 ``list``，交给平台适配器会以
        ``'list' object has no attribute 'chain'`` 失败。旧实现正是传了
        ``result.chain``，于是直发在 AstrBot v4.x 上从来没成功过，每次投递
        都退回流水线、还各刷一条 warning。

        当前运行时不支持直发时返回 False，由调用方回退为 ``yield``。
        """
        send = getattr(event, "send", None)
        if not callable(send):
            return False

        # send() 需要「带 chain 属性的消息链对象」，且 chain 不能为空
        # （空 chain 发出去也没有意义）。优先用 result 本身；老版本把
        # result.chain 也做成消息链时再退一步用它。
        target = None
        if getattr(result, "chain", None):
            target = result
        elif hasattr(getattr(result, "chain", None), "chain"):
            target = result.chain
        if target is None:
            return False

        try:
            await send(target)
        except Exception as e:  # noqa: BLE001 - 直发不可用时回退为流水线发送
            logger.warning(f"[dota2] 直接发送失败，回退为流水线发送: {e}")
            return False
        return True

    async def _emit(self, event: AstrMessageEvent, text: str, as_image: bool = False):
        """把文本发送到当前会话（可作为异步生成器使用）。

        长报告优先转成图片发送，失败或未启用时按长度拆分成多条文本。
        """
        if not text:
            return
        if as_image:
            url = await self._render_image(text)
            if url:
                yield event.image_result(url)
                return
        for chunk in self._chunk_text(text):
            yield event.plain_result(chunk)

    # ==================================================================
    # 目标解析
    # ==================================================================
    async def _resolve_account(
        self,
        target: str,
        in_match_players: list[dict] | None = None,
        *,
        umo: str = "",
    ) -> tuple[int, str]:
        """把用户输入解析成 ``(account_id, personaname)``。

        Args:
            target: 用户输入的昵称 / 32 位 ID / 64 位 SteamID / 个人主页链接。
            in_match_players: 可选的「本局选手列表」。给出时，昵称会先在这
                10 个人里精确匹配；命中唯一就直接采用。OpenDota 上重名昵称
                极多（例如「大魔导师马化腾」有 5 个同名账号），只靠全局搜索
                会让这些玩家永远解析不出来，而复盘场景下本局选手就是天然消歧器。
            umo: 会话标识。给了就先在**本会话名单**（已绑定 / 已监听）里找 ——
                零网络开销，而且不会像全局搜索那样被重名账号绊住。自然语言
                入口给的昵称常来自名单（「钢板」「天鸽」），走这一步最稳。

        Raises:
            TargetNotFoundError: 找不到唯一确定的玩家。
            OpenDotaError: 数据源调用失败。
        """
        target = (target or "").strip()
        if not target:
            raise TargetNotFoundError("没有指定玩家。")

        # 本会话名单优先：名字本来就是查这个账号查出来的，不必再搜一次
        if umo:
            hit = self._session_account_of(umo, target)
            if hit:
                return hit

        # 支持直接粘贴 Steam 个人主页链接
        match = re.search(r"/profiles/(\d{15,20})", target)
        if match:
            target = match.group(1)

        # 纯数字：按 32 位 / 64 位 ID 处理
        if re.fullmatch(r"\d{5,20}", target):
            account_id = to_account_id(target)
            if not account_id:
                raise TargetNotFoundError(
                    f"`{target}` 不是合法的 Steam ID。\n"
                    "· 32 位 account_id 是 5-10 位数字，例如 `86745912`\n"
                    "· 64 位 SteamID 是 17 位数字、以 7656119 开头，"
                    "例如 `76561198047011640`"
                )
            player = await self.api.get_player(account_id)
            if not player:
                raise TargetNotFoundError(
                    f"{self._source_name()} 查不到账号 **{account_id}**。\n"
                    "常见原因：该账号在 Dota 2 设置中关闭了「公开比赛数据」，"
                    "或从未被数据源索引过。"
                )
            profile = player.get("profile") or {}
            return account_id, profile.get("personaname") or f"账号{account_id}"

        # 昵称在本局选手里唯一命中时直接采用，跳过全局搜索（重名消歧）
        if in_match_players:
            lowered_target = target.lower()
            hits = [
                player
                for player in in_match_players
                if isinstance(player, dict)
                and player.get("account_id")
                and str(player.get("personaname") or "").lower() == lowered_target
            ]
            if len(hits) == 1:
                hit_id = int(hits[0]["account_id"])
                return hit_id, str(hits[0].get("personaname") or f"账号{hit_id}")

        # 昵称：走主数据源搜索（STRATZ 优先，不可用时自动降级）
        try:
            results = await self.api.search_player(target)
        except OpenDotaError as e:
            raise TargetNotFoundError(
                f"昵称搜索失败（{e}）。\n"
                f"{self._source_name()} 的昵称搜索接口偶尔会超时或限流，建议改用"
                "**32 位账号 ID** 或 **64 位 SteamID**，结果更准确也更稳定。"
            ) from e
        if not results:
            raise TargetNotFoundError(
                f"没有搜索到昵称包含「{target}」的玩家。\n"
                "建议直接使用 32 位账号 ID 或 64 位 SteamID 绑定，结果更准确。"
            )

        lowered = target.lower()
        exact = [
            item
            for item in results
            if str(item.get("personaname") or "").lower() == lowered
        ]
        if len(exact) == 1:
            chosen = exact[0]
        elif len(results) == 1:
            chosen = results[0]
        else:
            candidates = (exact or results)[:5]
            lines = [
                f"昵称「{target}」匹配到多个玩家，请使用账号 ID 重新操作：",
                "",
            ]
            for item in candidates:
                lines.append(
                    f"· {item.get('personaname')}　account_id: {item.get('account_id')}"
                    + (
                        f"　（最近游戏 {fmt_ago(item.get('last_match_time'))}）"
                        if item.get("last_match_time")
                        else ""
                    )
                )
            lines.append("")
            lines.append("例如：/d2 绑定 " + str(candidates[0].get("account_id")))
            raise TargetNotFoundError("\n".join(lines))

        account_id = int(chosen["account_id"])
        return account_id, str(chosen.get("personaname") or f"账号{account_id}")

    def _effective_binding(
        self, event: AstrMessageEvent
    ) -> tuple[dict | None, str]:
        """获取当前会话下应该使用的绑定。"""
        umo = event.unified_msg_origin
        uid = str(event.get_sender_id())
        binding = self.store.get_binding(umo, uid)
        if binding:
            return binding, ""
        bindings = self.store.list_bindings(umo)
        if len(bindings) == 1:
            only = next(iter(bindings.values()))
            return only, f"（使用本会话唯一的绑定：{only.get('personaname')}）"
        return None, ""

    @staticmethod
    def _no_binding_reply() -> str:
        return (
            "你还没有绑定 Dota2 账号。\n\n"
            "· 绑定：`/d2 绑定 <昵称 | 32位账号ID | 64位SteamID>`\n"
            "· 也可以临时指定目标：`/d2 战绩 20 天鸽`\n\n"
            "发送 `/d2 帮助` 查看完整用法。"
        )

    # ==================================================================
    # 指令：帮助
    # ==================================================================
    @filter.command_group("d2", alias={"dota2", "刀塔"})
    def d2(self):
        """Dota2 数据查询助手"""

    @d2.command("help", alias={"帮助", "h", "?", "菜单"})
    @take_over_event
    async def d2_help(self, event: AstrMessageEvent):
        """查看 Dota2 助手的使用说明"""
        yield event.plain_result(HELP_TEXT)

    # ==================================================================
    # 自然语言入口：不用记指令，直接说人话
    # ==================================================================
    #: 意图 → 处理函数名。自然语言识别出意图后，直接复用对应的指令实现，
    #: 保证「说人话」与「打指令」两条路径的行为完全一致。
    NLU_DISPATCH: dict[str, str] = {
        "help": "d2_help",
        "bind": "d2_bind",
        "unbind": "d2_unbind",
        "my": "d2_my",
        "bindings": "d2_bindings",
        "info": "d2_info",
        "heroes": "d2_heroes",
        "wheelchair": "d2_wheelchair",
        "matches": "d2_matches",
        "analyze": "d2_analyze",
        "match": "d2_match",
        "forceparse": "d2_askparse",
        "watch": "d2_watch",
        "unwatch": "d2_unwatch",
        "watchlist": "d2_watchlist",
        "llmtest": "d2_llmtest",
        "datasource": "d2_datasource",
        "schedule": "d2_schedule",
    }

    #: 单场复盘的指令别名 → handler。
    #: AstrBot 本身负责事件分发，这里保留一份是为了让测试与内部调用
    #: 能用和用户完全相同的入口（`/d2 单场` / `/d2 match` / `/d2 复盘单场`）。
    MATCH_COMMANDS: dict[str, str] = {
        "match": "d2_match",
        "单场": "d2_match",
        "detail": "d2_match",
        "复盘单场": "d2_match",
    }

    def _nlu_invoke(self, name: str, event: AstrMessageEvent, args: str):
        """按目标 handler 的真实签名决定要不要传参数。

        指令 handler 有两类：带 ``args`` 的（查询 / 绑定 / 监听）和不带
        参数的（帮助 / 我的 / 绑定列表 / 监听列表）。自然语言入口不该
        关心这个差别，这里统一处理。
        """
        handler = getattr(self, self.NLU_DISPATCH.get(name, ""), None)
        if handler is None:
            return None
        func = getattr(handler, "__wrapped__", handler)
        try:
            sig = inspect.signature(func)
            takes_args = len(sig.parameters) >= 3
        except (TypeError, ValueError):  # pragma: no cover - 理论不会发生
            takes_args = True
        if takes_args:
            return handler(event, args)
        return handler(event)

    def _nlu_keyword(self) -> str:
        """当前生效的唤醒词（配置留空则回落到默认值）。"""
        raw = self.cfg("nlu_keyword", NLU_DEFAULT_KEYWORD)
        text = str(raw or "").strip()
        return text or NLU_DEFAULT_KEYWORD

    # ------------------------------------------------------------------
    # 会话语境（供「这一盘」这类指代消歧）
    # ------------------------------------------------------------------
    def _nlu_log_line(
        self, umo: str, role: str, text: str, *, kind: str = "chat"
    ) -> None:
        """记一行会话日志。``role`` 为 ``user`` / ``bot``。

        ``kind`` 区分「对话」（``chat``）与「报告类产出」（``report``：复盘报告、
        定时播报、监听推送）。两者都进历史 —— 用户接着问「刚才那份报告里他
        补刀多少」时，模型手上必须有那份报告 —— 但排版与读出时的前缀不同。
        """
        self.history.append(umo, role, text, kind=kind)

    def _nlu_chat_lines(self, umo: str, limit: int = 8) -> list[str]:
        """取出会话最近几轮对话，渲染成 ``角色: 内容`` 的形式。

        给提示词里的【最近对话】段用（定时播报那条**不接工具**的老路径，
        以及模板渲染）。**工具路径改用** :meth:`_nlu_chat_messages` ——
        真正的多轮消息序列比一段拼接文本强得多。
        """
        return self.history.text_lines(umo, limit=limit)

    def _nlu_chat_messages(self, umo: str) -> list[dict[str, str]]:
        """取出会话最近几轮对话，渲染成**真正的多轮消息序列**。

        供带工具的主路径前置到 ``messages`` 里（``role`` 为 ``user`` /
        ``assistant``）。末尾那条 user 会被丢掉：调用方在进模型**之前**已经
        把本轮问题记进历史了，而本轮问题又会作为 ``user`` 消息单独放在最后。
        """
        return self.history.messages(umo)

    def _nlu_remember_match(
        self,
        umo: str,
        match_id: Any,
        desc: str = "",
        log: bool = True,
        start_time: Any = None,
    ) -> None:
        """把一场比赛记进会话语境（供「这一盘」指代 + 闲聊兜底的时效判断）。

        Args:
            umo: 会话标识。
            match_id: 比赛 ID。
            desc: 一句话描述（谁 / 什么英雄 / 结果），越短越好。
            log: 是否顺带写一行会话日志。批量记录（战绩列表）时关掉，
                否则一个列表会刷出好几行日志，把真正的对话挤没。
            start_time: 这场比赛自己的**开赛时间戳**。闲聊兜底要拿它算
                「昨天 / 今天」——**不能拿记录时刻顶替**：复盘一场三天前的
                旧局时，记录时刻就是「现在」，用它会把旧局标成「今天」。
                取不到就留空，渲染时如实写「时间未知」。
        """
        umo = str(umo or "")
        try:
            mid = int(match_id)
        except (TypeError, ValueError):
            return
        if not umo or mid <= 0:
            return
        bucket = self._nlu_recent_matches.setdefault(umo, deque(maxlen=8))
        # 同一场再次出现时提到最前面，保持「由新到旧」
        previous: dict | None = None
        for row in list(bucket):
            if int(row.get("match_id") or 0) == mid:
                bucket.remove(row)
                previous = row
                break
        # 同一场比赛可能被多次提到（推送 → 复盘 → 再问一次）。后一次没带
        # 信息时不要把先前的覆盖成空：时间尤其如此，推送那次是唯一
        # 手上握有 match 对象的时机。
        resolved_start = start_time
        if resolved_start is None and previous is not None:
            resolved_start = previous.get("start_time")
        resolved_desc = (desc or "").strip()
        if not resolved_desc and previous is not None:
            resolved_desc = str(previous.get("desc") or "")
        bucket.appendleft(
            {
                "match_id": mid,
                "desc": resolved_desc,
                "start_time": resolved_start,
                "ts": time.time(),
            }
        )
        if log:
            self._nlu_log_line(
                umo, "bot", f"[提到比赛 {mid}]" + (f" {desc}" if desc else "")
            )

    def _nlu_recent_match_rows(self, umo: str, limit: int = 5) -> list[dict]:
        """本会话最近提到过的比赛（由新到旧）。"""
        return list(self._nlu_recent_matches.get(str(umo or "")) or [])[:limit]

    def _nlu_head_text(self, text: str) -> str:
        """剥离唤醒词后的正文；没写唤醒词时**原样返回**。

        确认 / 取消这类短回复既可能裸写（「确认」），也可能带着唤醒词
        （「dota2助手 确认」），用这个函数统一成同一种形态再比较。
        """
        _matched, rest = dota_nlu.strip_wake_keyword(text, self._nlu_keyword())
        return rest

    def _nlu_should_handle(self, event: AstrMessageEvent, text: str) -> NluGate | None:
        """判断这条消息要不要交给自然语言入口处理。

        返回 :class:`NluGate`（含**真正送去解析的文本**与「是否命中唤醒词」），
        返回 ``None`` 表示不处理。

        自然语言入口是 ``EventMessageType.ALL`` 上的全局监听，不设闸门就会
        抢答别的插件（以及群里其他人的闲聊）的对话。闸门依次是：

        1. 总开关 ``nlu_enabled``；
        2. 指令类消息（``/`` 开头等）让给命令 handler；
        3. 唤醒词：默认要求消息里出现「dota2助手」，命中即剥掉再解析；
        4. 群聊里没写唤醒词时，仍要求 @ 机器人（``nlu_group_require_at``）。

        第 3 与第 4 步的关系：**唤醒词本身就是一次明确点名**，命中它就等于
        已经 @ 过机器人，因此不再重复要求 @，避免「又写唤醒词又 @」的双重
        门槛。反过来，只有 @ 而没写唤醒词的消息会被第 3 步挡掉。

        注意返回值里的 ``keyword_matched``：只有它为真时，调用方才允许
        走「闲聊兜底」（没识别出指令时由插件带数据回答）。关掉唤醒词
        限制（``nlu_require_keyword=false``）时它恒为假，插件就不会去
        抢答任何一条闲聊 —— 这是**故意的**安全一侧。
        """
        if not self.cfg("nlu_enabled", True):
            return None
        text = (text or "").strip()
        if not text:
            return None
        # 指令类消息交给命令 handler，这里不截胡
        if text.startswith(NLU_SKIP_PREFIXES):
            return None

        # ---- 唤醒词闸门 ----
        keyword_matched = False
        effective = text
        if self.cfg("nlu_require_keyword", True):
            keyword_matched, effective = dota_nlu.strip_wake_keyword(
                text, self._nlu_keyword()
            )
            if not keyword_matched:
                return None
            if not effective:
                # 只发了个唤醒词、没说要求什么：不猜，留给默认大模型
                return None
            # 剥掉唤醒词后如果露出的是别的插件/本插件的指令（「dota2助手 /help」），
            # 同样不截胡 —— 那条消息该由对应的命令 handler 处理。
            if effective.startswith(dota_nlu.NLU_STRIP_CMD_PREFIXES):
                return None

        # 群聊 / 私聊判断：用消息类型与 umo 双重判断，兼容各适配器。
        # get_message_type 在少数自定义 Event 上可能不存在，因此做了容错。
        umo = str(event.unified_msg_origin)
        try:
            message_type = str(event.get_message_type())
        except Exception:  # noqa: BLE001
            message_type = ""
        is_group = "GROUP_MESSAGE" in message_type or "GroupMessage" in umo
        if is_group and self.cfg("nlu_group_require_at", True) and not keyword_matched:
            # 群里必须 @ 机器人（或使用唤醒前缀，此时 message_str 里已带前缀）。
            # 这条限制能挡掉绝大多数「群里别人随口一说就被插件抢答」的情况。
            try:
                if not event.is_at_or_wake_command:
                    return None
            except AttributeError:
                return None
        return NluGate(text=effective, keyword_matched=keyword_matched)

    def _nlu_session_players(self, umo: str) -> list[dict]:
        """本会话里「有名字的人」：已绑定 + 已监听，按 account_id 去重。

        同一张表服务三处：自然人名解析（「钢板」是谁）、工具参数里的
        ``player``、以及给模型看的「可查玩家」清单。**去重是必要的**：
        一个人既绑定又被监听时会同时出现在两张表里，重复出现会让模型
        以为有两个人，也可能让「只点名了一个人」的判断失效。
        """
        rows: list[dict] = []
        seen: set[int] = set()
        try:
            for info in (self.store.list_bindings(umo) or {}).values():
                account_id = int(info.get("account_id") or 0)
                if not account_id or account_id in seen:
                    continue
                seen.add(account_id)
                rows.append(
                    {
                        "name": str(info.get("personaname") or f"账号{account_id}"),
                        "account_id": account_id,
                        "relation": "绑定",
                    }
                )
            for watcher in self.store.list_watchers(umo) or []:
                account_id = int(watcher.get("account_id") or 0)
                if not account_id or account_id in seen:
                    continue
                seen.add(account_id)
                rows.append(
                    {
                        "name": str(watcher.get("personaname") or f"账号{account_id}"),
                        "account_id": account_id,
                        "relation": "监听",
                    }
                )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[dota2] 取本会话名单失败: {e}")
        return rows

    def _nlu_known_names(self, umo: str) -> list[str]:
        """本会话里「有名字的人」的昵称列表。

        用来把自然语言里提到的昵称对上号。「天鸽的轮椅」这种句子里没有
        「XX 的战绩」那种句式标记，纯靠正则抽人名很容易抽错；而这些名字
        是当初真去 OpenDota 查出来的，直接拿来做精确匹配最稳。
        """
        return [row["name"] for row in self._nlu_session_players(umo)]

    def _session_account_of(self, umo: str, name: str) -> tuple[int, str] | None:
        """把名字（含简称）对到本会话名单里的账号，命中返回 ``(id, 名字)``。

        这是「认出是谁」之后**不联网**拿到 account_id 的唯一入口：
        名单里的名字本来就是查这个账号查出来的，再拿名字去 OpenDota 搜一次
        纯属多余，而且昵称重名极多，搜出来很可能歧义报错。
        """
        text = str(name or "").strip()
        if not text:
            return None
        players = self._nlu_session_players(umo)
        if not players:
            return None
        matched = dota_nlu.match_session_name(
            text, tuple(row["name"] for row in players)
        )
        if not matched:
            return None
        for row in players:
            if row["name"] == matched:
                return int(row["account_id"]), str(row["name"])
        return None

    # ------------------------------------------------------------------
    def _chat_tools_available(self) -> bool:
        """闲聊的**工具模式**是否启用 —— 只看配置，不看通道此刻通不通。

        v2.3.4 起闲聊改走「默认模型优先」：AstrBot 自带的 provider 从 4.x
        起支持 ``func_tool``，所以工具调用不再依赖插件专用 Key（专用 Key
        留给比赛分析）。通道到底能不能用，由 :meth:`_chat_tool_clients` 在
        真正调用前逐个判定。

        这里故意只做同步的配置检查，是为了让调用方的分支只反映「用户想不
        想要工具」；把「此刻哪个通道是通的」混进来，同一个开关会随网络状态
        忽真忽假，路由就没法预期了。
        """
        if not self.cfg("nlu_chat_tools", True):
            return False
        return bool(self.cfg("enable_llm_analysis", True))

    @staticmethod
    def _provider_label(provider: Any) -> str:
        """给「默认模型」起一个能在日志 / 自检里看懂的名字。"""
        model = ""
        try:
            getter = getattr(provider, "get_model", None)
            if callable(getter):
                value = getter()
                # 个别版本把 get_model 写成异步的，那种情况下别去 await ——
                # 这里只是取个显示名，不值得为它引入协程语义。
                if not hasattr(value, "__await__"):
                    model = str(value or "")
        except Exception:  # noqa: BLE001
            model = ""
        if not model:
            try:
                config = getattr(provider, "provider_config", None) or {}
                model = str(config.get("model") or "")
            except Exception:  # noqa: BLE001
                model = ""
        return f"默认模型 {model}".strip() if model else "默认模型"

    async def _chat_tool_clients(self, umo: str) -> list[Any]:
        """闲聊（含工具调用）的模型通道，按优先级排列：**默认模型 → 专用 Key**。

        闲聊是高频、低价值的交互，用 AstrBot 全局配置的模型就够；插件专用
        Key 是给比赛分析准备的（长文、按量计费）。只有当默认模型压根拿不到、
        或者它调不动工具（不支持 function calling）时，才回退专用 Key ——
        这样「一句话里要好几件事」不会因为默认模型不支持工具就答不全。
        """
        chain: list[Any] = []
        provider = None
        try:
            provider = await resolve_provider(
                self.context, umo, str(self.cfg("llm_provider_id", "") or "")
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dota2] 获取默认模型提供商失败：{e}")
        if provider is not None:
            chain.append(
                ProviderToolClient(provider, label=self._provider_label(provider))
            )
        client = self._dedicated_client()
        if client is not None:
            chain.append(client)
        return chain


    async def _fetch_chat_matches(self, account_id: int, limit: int) -> list[dict]:
        """拉近期对局，**复用闲聊兜底的缓存**。

        工具与预取经常查同一个账号（「钢板最近怎么样」会先被预取一次，
        模型再调一次 query_matches）—— 共用一份缓存才不会把同一个账号
        查两遍。缓存里只放纯数据，key 与 :func:`dota_chat.collect_chat_context`
        保持一致：``(account_id, limit)``。
        """
        account = int(account_id or 0)
        size = max(1, int(limit or 1))
        if not account:
            raise OpenDotaError("没有指定玩家账号。")
        key = (account, size)
        now = time.time()
        entry = self._chat_cache.get(key)
        if entry and now - entry[0] < dota_chat.DEFAULT_CACHE_TTL:
            return [row for row in entry[1] if isinstance(row, dict)]
        result = await self.api.get_player_matches_enriched(account, size)
        matches = result[0] if isinstance(result, tuple) else result
        rows = [row for row in (matches or []) if isinstance(row, dict)]
        if rows:
            self._chat_cache[key] = (now, list(rows))
        return rows

    async def _nlu_resolve_person(
        self, umo: str, event: AstrMessageEvent, raw: Any
    ) -> tuple[int, str]:
        """把工具参数里的「查谁」解析成 ``(account_id, 显示名)``。

        顺序由便宜到贵：本人 → 本会话名单（**零网络开销**）→ 联网搜索。
        名单这一步是关键：「钢板」这种简称在 OpenDota 上搜出三五个重名账号
        是常事，而名单里的账号是当初真查出来的，直接用就行。
        """
        text = str(raw or "").strip()
        if not text or text.lower() in dota_nlu.WHEELCHAIR_SELF_WORDS or text in {
            "我的",
            "me",
            "my",
        }:
            # 定时任务到点执行时**没有「提问者」**（event 为 None）：退而取
            # 本会话唯一的绑定；取不到就说清「定时上下文里没法确定『我』」，
            # 不能照搬「提问者还没绑定」——那时根本没有提问者。
            if event is not None:
                binding, _note = self._effective_binding(event)
            else:
                binding = self._binding_for_umo(umo)
                if not binding:
                    raise TargetNotFoundError(
                        "这是定时任务的上下文，没有「提问者」，所以「我」指的是谁"
                        "没法确定。请改用昵称或账号 ID 说清要查的人。"
                    )
            if not binding:
                raise TargetNotFoundError(
                    "提问者还没有绑定账号，不知道要查谁。"
                    '可以让 TA 用「我的战绩」前先绑定，或直接说出昵称。'
                )
            account_id = int(binding.get("account_id") or 0)
            return account_id, str(binding.get("personaname") or f"账号{account_id}")

        hit = self._session_account_of(umo, text)
        if hit:
            return hit

        timeout = float(self.cfg("nlu_chat_tool_timeout", dota_tools.DEFAULT_TOOL_TIMEOUT) or 0)
        try:
            return await asyncio.wait_for(
                self._resolve_account(text), timeout=max(5.0, timeout / 2)
            )
        except asyncio.TimeoutError as e:
            raise TargetNotFoundError(
                f"按昵称「{text}」搜索账号超时。"
                "本会话里绑定或监听过的人可以直接用昵称查，其他昵称建议给账号 ID。"
            ) from e

    def _nlu_chat_tool_ctx(
        self,
        event: AstrMessageEvent | None = None,
        *,
        umo: str = "",
        uid: str = "",
        allow_write: bool = True,
    ) -> dota_tools.ToolContext:
        """构造一次工具调用的运行环境。

        六个外部能力全部以闭包注入，工具层因此不需要 import 插件主体，
        可以脱离 AstrBot 单独测试。这里**必须**把 ``ask_confirm`` /
        ``trigger_async`` / ``schedules_text`` 三个也接上：漏掉任何一个，
        对应的那类工具就会一律回「当前环境不支持」，写操作与单场复盘
        在自然语言里直接失效（界面上表现为「说了它说用不了」）。

        Args:
            event: 普通消息路径由 event 提供会话与用户标识；
                **定时任务到点没有 event**，改为显式传 ``umo``。
            umo / uid: 无 event 时的会话与用户标识（有 event 时以 event 为准）。
            allow_write: 是否接上写操作的确认闸门。定时通道传 ``False``
                —— 那一刻没有任何人守着回「确认」，写操作只会在群里留下
                一句「已登记待确认」的假回执。注意这只是**兜底**：定时通道
                的工具清单里本来就不含写操作（``build_tool_specs``）。
        """
        umo = str(umo or (event.unified_msg_origin if event is not None else ""))
        uid = str(uid or (event.get_sender_id() if event is not None else ""))
        timeout = float(
            self.cfg("nlu_chat_tool_timeout", dota_tools.DEFAULT_TOOL_TIMEOUT)
            or dota_tools.DEFAULT_TOOL_TIMEOUT
        )
        return dota_tools.ToolContext(
            api=self.api,
            cfg=self.cfg,
            heroes=self._heroes,
            resolve_player=lambda raw: self._nlu_resolve_person(umo, event, raw),
            session_players=lambda: self._nlu_session_players(umo),
            fetch_matches=self._fetch_chat_matches,
            ask_confirm=(
                (lambda action: self._nlu_ask_confirm(umo, uid, action))
                if allow_write
                else None
            ),
            trigger_async=lambda kind, params: self._nlu_trigger_async(
                umo, uid, event, kind, params
            ),
            schedules_text=lambda: self._schedule_list_text(umo),
            now=time.time(),
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # 写操作工具的确认闸门
    # ------------------------------------------------------------------
    async def _nlu_ask_confirm(self, umo: str, uid: str, action: dict) -> str:
        """写操作工具的统一出口：**只登记，不执行**，返回给用户看的文案。

        登记这一步必须留在插件主体：待确认状态本来就有两套现成机制
        （``_nlu_confirm`` 存「(handler 名, 参数)」、``_schedule_plans``
        存已解析的定时计划），让工具层自己再发明一套，就会出现「模型那边
        说登记好了、用户回确认却没反应」。

        .. important::

           写操作的确认闸门**始终生效**，不受 ``nlu_confirm_sensitive``
           影响。原因是模型看到的工具契约就是「调了不会立刻生效」——
           如果这个开关能把它变成立刻执行，模型就会照旧说「已完成」，
           而契约与事实不一致的那一天，用户收到的是一句假话。

        Returns:
            给用户看的确认文案，**绝不是「已执行」**。

        Raises:
            dota_tools.ConfirmUnavailable: 登记不上时抛出，消息里带
                「为什么 + 用户怎么改」。工具层会把它**原样转给模型**——
                模型是唯一能向用户解释的地方，只回一句「没登记上」，
                它就只能自己编理由（真机上编出的是「你定个 2 小时后的
                手机闹钟」）。
        """
        kind = str(action.get("kind") or "")
        if kind == "schedule":
            if not self.cfg("schedule_enabled", True):
                raise dota_tools.ConfirmUnavailable(
                    "定时任务功能在插件配置里被关掉了（schedule_enabled = false），"
                    "跟用户的说法无关。请如实告诉他去 WebUI 的插件配置里打开这一项，"
                    "不要给他别的绕法。"
                )
            request = self._parse_task_request(str(action.get("request") or ""), umo)
            if request is None:
                raise dota_tools.ConfirmUnavailable(
                    "没能从这句话里解析出「什么时候做」或「做什么」，所以没有登记。"
                    "请让用户换一种说法，把时间说成下面任一种：钟点"
                    "（「每天早上七点」「明天早上八点」）、相对时间（「两小时后」"
                    "「半小时后」「三天后」）、或按场次（「每监听到十场」）；"
                    "内容要跟 Dota2 数据有关（复盘某场比赛、通报战绩、看英雄池等）。"
                    "如果他说的是「这场」而本会话没有可指的比赛，请让他把比赛 ID "
                    "一起说上，例如「两小时后复盘 9017256966」。"
                )
            # 计数型任务（「每监听到 N 场」）不依赖平台调度器，别一起拒了。
            reason = self.schedules.unavailable_reason()
            if request.kind != "watch_count" and reason:
                raise dota_tools.ConfirmUnavailable(
                    f"平台的定时调度能力当前不可用（{reason}），这个任务建不了。"
                    "请把这一条如实告诉用户，不要换个说法让他再试一次。"
                )
            self._schedule_plans[(umo, uid)] = {
                "request": request,
                "umo": umo,
                "expire": time.time() + NLU_CONFIRM_TTL,
            }
            logger.info(f"[dota2] 工具登记待确认定时任务：{request.kind}")
            return dota_schedule.format_plan(
                request,
                session_text=dota_schedule.session_label(umo),
                keyword=self._nlu_keyword(),
            )

        name = str(action.get("name") or "").strip()
        if name not in self.NLU_DISPATCH:
            logger.warning(f"[dota2] 工具请求了未知的写操作: {name!r}")
            raise dota_tools.ConfirmUnavailable(
                f"插件没有「{name}」这个写操作（工具名填错了）。"
                "请重新调用工具，动作名只能用契约里列出的那几个。"
            )
        args = str(action.get("args") or "")
        desc = str(action.get("desc") or "").strip()
        if not desc:
            verb = str(action.get("verb") or name)
            desc = f"{verb}「{args}」" if args else verb
        self._nlu_confirm[(umo, uid)] = (name, args, time.time() + NLU_CONFIRM_TTL)
        logger.info(f"[dota2] 工具登记待确认动作: {name} args={args!r}")
        return (
            f"你刚才是想让我{desc}吗？\n"
            "回复「确认」我就执行；回复「取消」就当我没说。"
        )

    # ------------------------------------------------------------------
    # 慢任务的异步落地
    # ------------------------------------------------------------------
    async def _nlu_trigger_async(
        self,
        umo: str,
        uid: str,
        event: AstrMessageEvent,
        kind: str,
        params: dict,
    ) -> str:
        """慢任务工具的统一出口：**只发起**后台任务，立刻返回受理回执。

        单场复盘可能要申请录像解析并等十几分钟。工具调用发生在一次模型
        请求里，同步等就是把这轮问答连同模型的超时一起卡死；因此这里
        只 ``create_task``，正文由后台任务直接发回会话。
        """
        if kind not in dota_tools.ASYNC_KINDS:
            return f"[{kind} 不支持] 插件没有这种后台任务。"
        match_id = int(params.get("match_id") or 0)
        if match_id <= 0:
            return "[缺少比赛 ID] 后台任务需要比赛 ID，请先向用户确认是哪一场。"
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - 工具一定在事件循环里跑
            return "[后台任务启动失败] 当前没有运行中的事件循环。"
        if kind == "parse":
            coro = self._nlu_async_parse(umo, match_id)
            note = "已开始检查并申请解析"
        else:
            coro = self._nlu_async_match_detail(umo, uid, match_id)
            note = "已开始复盘"
        task = loop.create_task(coro)
        # 记进任务表，避免模型重复调同一个工具时重复起任务（它会重复申请解析）。
        key = self._parse_task_key(umo, match_id, f"tool:{uid}")
        self._parse_tasks[key] = task
        task.add_done_callback(lambda _t, _key=key: self._parse_tasks.pop(_key, None))
        logger.info(f"[dota2] 工具发起后台任务 {kind}: 比赛 {match_id}（{umo}）")
        return (
            f"✅ {note}，请求已被受理。"
            "这一步在后台跑（复盘可能要等录像解析几分钟到十几分钟），"
            "结果会由插件**直接发到本会话**，不需要再调用这个工具。"
            "请简短告诉用户「已经开始了，稍后发到本群」，不要复述细节。"
        )

    async def _nlu_async_parse(self, umo: str, match_id: int) -> None:
        """后台任务：检查解析状态并按需提交申请（``request_match_parse`` 的落地）。

        与 ``/d2 催解析`` 完全同口径，只是把「回给用户的话」从 yield 改成
        ``_send_to_session`` —— 工具路径的调用方早就把话说完返回了。
        """
        try:
            match = await self.api.get_match(match_id)
        except Exception as e:  # noqa: BLE001 - 后台任务绝不能把异常抛给事件循环
            logger.error(f"[dota2] 工具催解析：拉取比赛 {match_id} 失败: {e}")
            await self._send_to_session(umo, f"❌ 比赛 {match_id} 的解析检查失败：{e}")
            return
        if not match:
            await self._send_to_session(
                umo,
                f"❌ 找不到比赛 {match_id}，或它还没被数据源收录。\n"
                "未被收录时提交解析申请没有意义，一般等几分钟到几十分钟会自动收录。",
            )
            return
        state = dota_parse.parse_state(match)
        if state.parsed:
            await self._send_to_session(
                umo,
                f"✅ 比赛 {match_id} 已经解析完成（{state.describe()}），无需催解析。\n"
                f"想看复盘：说「复盘这一局」或 `/d2 单场 {match_id}`。",
            )
            return
        try:
            granted = await dota_parse.submit_parse(self.api, match_id)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[dota2] 工具催解析：提交失败 {match_id}: {e}")
            await self._send_to_session(umo, f"⚠️ 比赛 {match_id} 的解析申请提交失败：{e}")
            return
        if granted:
            await self._send_to_session(
                umo,
                f"📨 已提交比赛 {match_id} 的解析申请，任务已排队。\n"
                f"当前状态：{state.describe()}\n\n"
                "解析通常要几分钟到几十分钟。想拿到结果后自动出复盘，"
                "说一句「复盘这一局」即可（会等着解析，最多十分钟）。",
            )
        else:
            await self._send_to_session(
                umo,
                f"⚠️ 解析申请没有返回排队凭据（可能：该局无法解析 / 已在队列中 / 被限流）。\n"
                f"当前状态：{state.describe()}",
            )

    async def _nlu_async_match_detail(
        self, umo: str, uid: str, match_id: int
    ) -> None:
        """后台任务：单场复盘（``query_match_detail`` 的落地）。

        与 ``/d2 单场`` 同一条流水线：未解析就先交给
        :meth:`_parse_and_analyze` 去催解析并等（它会自己发进度与报告），
        已解析（或排队失败）则直接生成报告发到会话。
        """
        try:
            match = await self.api.get_match(match_id)
            if not match:
                await self._send_to_session(
                    umo,
                    f"❌ 查不到比赛 {match_id}：数据源里没有这盘。\n"
                    "可能原因：比赛 ID 写错、该局刚结束还没被收录、或对局方未公开比赛数据。",
                )
                return
            heroes = await self._heroes()
            items = await self._items()
            self._nlu_remember_match(umo, match_id, start_time=match.get("start_time"))
        except Exception as e:  # noqa: BLE001
            logger.error(f"[dota2] 工具复盘：拉取比赛 {match_id} 失败: {e}")
            await self._send_to_session(umo, f"❌ 拉取比赛 {match_id} 的数据失败：{e}")
            return

        # 焦点玩家：本会话提问者绑定的那个人若在本局里，就自动聚焦。
        # 与指令路径同一套判据（指令路径用的是 _effective_binding(event)，
        # 这里没有 event，直接按 uid 取绑定 —— 语义一致）。
        focus_ids: list[int] = []
        focus_names: dict[int, str] = {}
        binding = None
        try:
            binding = self.store.get_binding(umo, uid)
        except Exception:  # noqa: BLE001
            binding = None
        if binding:
            candidate = int(binding.get("account_id") or 0)
            if candidate and any(
                isinstance(player, dict) and player.get("account_id") == candidate
                for player in match.get("players") or []
            ):
                focus_ids.append(candidate)
                focus_names[candidate] = str(binding.get("personaname") or "")

        state = dota_parse.parse_state(match)

        # ---------- 未解析：交给既有的「催解析 + 等 + 出报告」流水线 ----------
        if not state.parsed and self.cfg("parse_wait_enabled", True):
            if self._spawn_parse_task(
                umo=umo,
                uid=uid,
                match_id=match_id,
                focus_ids=focus_ids,
                focus_names=focus_names,
            ):
                options = self._parse_wait_options()
                minutes = max(1, round(int(options["timeout"]) / 60))
                fallback_hint = (
                    "仍未解析就自动改用现有数据出一份基础数据版的报告"
                    if self.cfg("parse_fallback_unparsed", True)
                    else "等不到会通知你"
                )
                await self._send_to_session(
                    umo,
                    f"⏳ 比赛 {match_id} 数据完整度：{state.describe()}\n"
                    f"AI 复盘依赖逐分钟经济、团战与出装数据，"
                    f"{'已提交催解析并' if self.cfg('parse_submit_request', True) else ''}"
                    f"开始等待（最多 {minutes} 分钟，{fallback_hint}）。\n"
                    "报告出来后会自动发到本会话，可以先忙别的。",
                )
                return
            await self._send_to_session(
                umo,
                f"⚠️ 当前等待解析的任务过多，无法排队，"
                f"已改用基础数据（{state.describe()}）生成复盘。",
            )

        # ---------- 已解析 / 不等解析 / 排队失败：直接出报告 ----------
        await self._nlu_deliver_match_report(
            umo, match_id, match, heroes, items, focus_ids, focus_names, state.parsed
        )

    async def _nlu_deliver_match_report(
        self,
        umo: str,
        match_id: int,
        match: dict,
        heroes: dict,
        items: dict,
        focus_ids: list[int],
        focus_names: dict[int, str],
        parsed: bool,
    ) -> None:
        """生成并发送单场复盘（工具路径的最终一段）。

        报告正文走 :meth:`_generate_report_for_umo`（不依赖 event），
        发送走 :meth:`_send_to_session`（**纯文本**，与监听推送同一条通道）。
        刻意不做图片渲染：后台任务里没有 event，而渲染失败时的回退分支
        会让「报告已生成」这件事变得难以判断；文本先保证一定送达。
        """
        try:
            headline = self.match_headline(
                match, heroes, focus_ids or None, parsed, focus_names
            )
            extra_context = await self._build_recent_context(
                match_id, focus_ids, focus_names, heroes
            )
            abilities = await self._ability_constants()
            curve_ids = self._curve_targets(match, focus_ids)
            prompt = build_single_match_analysis_prompt(
                match=match,
                heroes=heroes,
                items=items,
                focus_account_ids=focus_ids or None,
                focus_names=focus_names or None,
                extra_context=extra_context,
                abilities=abilities,
                curve_ids=curve_ids,
            )
            report = await self._generate_report_for_umo(umo, prompt)
        except Exception as e:  # noqa: BLE001 - 生成失败要发出去，不能只留日志
            logger.error(f"[dota2] 工具复盘：生成报告失败 {match_id}: {e}", exc_info=True)
            await self._send_to_session(umo, f"⚠️ 比赛 {match_id} 的复盘生成失败：{e}")
            return

        if report:
            await self._send_to_session(umo, headline)
            await self._send_to_session(umo, report)
            logger.info(f"[dota2] 工具复盘已推送: 比赛 {match_id} → {umo}")
            return
        # 模型不可用：退化成原始数据，至少让用户拿到东西
        raw = (
            f"{headline}\n\n"
            "⚠️ 未启用大模型分析或模型不可用，以下为从数据源获取的完整原始数据：\n\n"
            + build_match_data_text(
                match,
                heroes,
                items,
                focus_account_ids=focus_ids or None,
                abilities=abilities,
                curve_ids=curve_ids,
            )
        )
        await self._send_to_session(umo, raw)

    async def _run_tool_loop(
        self,
        client: Any,
        prompt: str,
        system_prompt: str,
        tool_ctx: Any,
        *,
        include_write: bool = True,
        history: list[dict[str, str]] | None = None,
    ) -> tuple[str, bool]:
        """在**一条**通道上跑完多轮工具循环。

        流程就是标准的 function calling 循环：把问题与工具清单发给模型，
        它要么直接作答、要么要求调用若干工具；执行完把结果作为 ``role=tool``
        的消息回填，进入下一轮。上限由 ``nlu_chat_tool_rounds`` 与
        ``nlu_chat_tool_max_calls`` 双重把关 —— 没有上限的话，一个含糊的
        问题能让模型把数据源查个底朝天。

        Args:
            history: **上几轮对话**（``role=user`` / ``role=assistant``，
                由 :meth:`_nlu_chat_messages` 备好），前置到本轮消息之前。

                以前历史是被拼成一段文本塞进本轮 user 消息末尾的 ——
                模型得从「用户: … 机器人: …」这段拼接里自己解析谁说了什么，
                而且上一个问题与这一段之间隔着一大坨事实底表。改成真正的
                多轮消息后，模型的对话连续性显著变好（这也是 AstrBot 的
                ``text_chat`` 原生认的形态：非 system 消息原样进 contexts）。

                这里只放**纯文本**消息，不带 ``tool_calls`` —— 历史里留一个
                没有对应 ``tool`` 结果的 ``tool_calls`` 会让多数服务直接 400。

        Returns:
            ``(回答, 通道是否不可用)``。第二项只在**第一轮就失败**时为真：
            那多半说明这条通道不支持工具或没配好，调用方可以换下一条重试。
            中途失败（之前已经拿到过工具调用）不算 —— 换条通道重跑等于把
            同一批查询再烧一遍配额，没有意义。
        """
        tools = dota_tools.build_tool_specs(include_write=include_write)
        rounds = max(1, int(self.cfg("nlu_chat_tool_rounds", 3) or 3))
        max_calls = max(1, int(self.cfg("nlu_chat_tool_max_calls", 6) or 6))
        temperature = float(self.cfg("llm_temperature", 0.7) or 0)
        max_tokens = int(self.cfg("llm_max_tokens", 0) or 0) or None

        messages: list[dict] = [
            {
                "role": "system",
                "content": (system_prompt or "").strip()
                + "\n\n"
                + dota_tools.TOOL_GUIDE,
            },
            # 上几轮对话（真正的 user / assistant 消息，不是拼接文本）
            *[dict(row) for row in (history or []) if isinstance(row, dict)],
            {"role": "user", "content": prompt},
        ]
        used = 0
        answer = ""
        for round_index in range(rounds):
            try:
                reply = await client.chat_with_tools(
                    messages,
                    tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except Exception as e:  # noqa: BLE001 - 失败回落单轮，不能吞消息
                logger.error(
                    f"[dota2] 闲聊工具对话第 {round_index + 1} 轮失败"
                    f"（{client.endpoint}）: {e}"
                )
                return answer, round_index == 0

            if not reply.has_tool_calls:
                answer = reply.content or answer
                break

            # 这是**必须**回填的一条：assistant 侧带 tool_calls 的消息 +
            # 每个 tool_call_id 对应的结果，缺一条多数服务会直接 400。
            messages.append(reply.assistant_message())
            answer = reply.content or answer
            for call in reply.tool_calls:
                if used >= max_calls:
                    result = (
                        "[本次回答的工具调用次数已达上限，这次查询没有执行。"
                        "请基于已拿到的数据作答。]"
                    )
                    tool_ctx.trace.append(f"{call.name}(跳过：超过上限)")
                else:
                    # 被短路的工具（同一工具已连续失败到上限）是零成本的，
                    # **不占配额** —— 数据源挂掉时前几次短路要是把预算吃光，
                    # 还能用的工具就轮不上了，用户白等一场。
                    counted = not dota_tools.is_disabled(call.name, tool_ctx)
                    result = await dota_tools.run_tool(
                        call.name, call.arguments_raw, tool_ctx
                    )
                    if counted:
                        used += 1
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": result}
                )
        else:
            # 轮数用尽仍在要求调工具：把工具收走，明确要一个收口回答。
            # 继续给工具只会让它一直查下去，永远不给答案。
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "（本轮查询的工具调用次数已用完，请立刻基于已拿到的数据"
                        "给出最终回答，不要再请求调用工具。）"
                    ),
                }
            )
            try:
                final = await client.chat_with_tools(
                    messages,
                    [],
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                answer = final.content or answer
            except Exception as e:  # noqa: BLE001
                logger.error(f"[dota2] 闲聊工具对话收口失败: {e}")

        trace = "; ".join(tool_ctx.trace)
        if trace:
            logger.info(f"[dota2] 闲聊工具调用（{client.endpoint}）: {trace}")
        return answer, False

    async def _nlu_chat_agent(
        self,
        event: AstrMessageEvent | None = None,
        prompt: str = "",
        system_prompt: str = "",
        *,
        umo: str = "",
        uid: str = "",
        allow_write: bool = True,
        history: list[dict[str, str]] | None = None,
    ) -> tuple[str | None, str, list[str]]:
        """带工具的多轮问答（闲聊主路径，走**默认模型**）。

        通道顺序固定为「默认模型 → 插件专用 Key」（见
        :meth:`_chat_tool_clients`）：闲聊是高频交互，用 AstrBot 全局配置的
        模型即可；专用 Key 留给比赛分析。默认模型调不动工具（不支持 function
        calling）时才换专用 Key，保证「一句话里要好几件事」仍然答得全。

        Args:
            history: 上几轮对话（真正的 user / assistant 消息）。**闲聊路径
                必须传**，否则模型接不上上一句；定时任务那条通道**不传**
                （到点执行一件事，历史只会干扰它）。

        Returns:
            ``(回答, 工具调用轨迹, 待发给用户的确认请求)``。回答为 ``None``
            表示这条链路没能产出内容（一条通道都没有 / 模型报错），调用方
            回落单轮带数据的回答。第三项来自写操作工具：**必须由插件原样
            发出**，不能让模型转述（确认文案要和用户逐字对齐）。
        """
        umo = str(umo or (event.unified_msg_origin if event is not None else ""))
        clients = await self._chat_tool_clients(umo)
        if not clients:
            logger.info(
                "[dota2] 闲聊工具通道不可用：既没有 AstrBot 模型提供商，也没配专用 API Key"
            )
            return None, "", []

        tool_ctx = self._nlu_chat_tool_ctx(
            event, umo=umo, uid=uid, allow_write=allow_write
        )
        answer = ""
        for index, client in enumerate(clients):
            answer, channel_dead = await self._run_tool_loop(
                client,
                prompt,
                system_prompt,
                tool_ctx,
                include_write=allow_write,
                history=history,
            )
            if answer:
                if index:
                    logger.info(f"[dota2] 闲聊工具通道回退到 {client.endpoint}")
                break
            if not channel_dead:
                # 通道本身是通的，只是这一轮没产出 —— 换通道重跑没有意义
                break
            if index + 1 < len(clients):
                logger.warning(f"[dota2] {client.endpoint} 调不动工具，换下一条通道重试")
        return (answer or None), "; ".join(tool_ctx.trace), list(tool_ctx.pending_prompts)

    async def _nlu_agent_reply(
        self, event: AstrMessageEvent, question: str
    ) -> tuple[str | None, list[str]]:
        """自然语言入口的**唯一**路径：带着全套工具，让模型自己决定干什么。

        走的是**默认模型**（AstrBot 全局配置的那个，见
        :meth:`_chat_tool_clients`）。模型可以：

        * 直接回答（闲聊、解释、建议）；
        * 调用只读工具补数据（查战绩 / 英雄池 / 版本榜 …）；
        * 调用写操作工具改设置 —— 那只会**登记待确认**，插件随后把确认
          请求发出去，用户回「确认」才真的执行；
        * 发起后台任务（单场复盘 / 催解析）—— 只受理，正文稍后自动发到
          本会话。

        典型场景：

        * 「dota2助手 对比一下目前监听的几个人谁最菜」
          —— 需要监听列表 + 每个人的近期战绩；
        * 「dota2助手 钢板最近打得怎么样？顺便给他推荐几个轮椅」
          —— 一句里要两样东西，工具调用才能都答上；
        * 「dota2助手 以后每天七点通报群里战绩」
          —— 走写操作工具 + 确认闸门。

        上下文里只放**事实**：当前时间、本会话的名单、本会话涉及过的比赛。
        具体战绩 / 英雄池 / 版本数据一律由模型调工具取 —— 插件不再用关键词
        预判用户想要哪块数据（那种预判正是「兜不住」的来源，见
        :func:`dota_chat.collect_chat_context` 的 ``local_only``）。

        失败语义（很重要）：

        * **数据收集失败不算失败**。名单 / 会话比赛拿不到照样能回答。
        * **模型不可用才算失败**。此时返回 ``(None, [])``，调用方
          **不吞消息**（交回默认大模型），绝不既不回答又把消息吃掉。

        Returns:
            ``(回答, 待发送的确认请求)``。两者都为空表示这条链路没产出。
        """
        # 用户明确关闭了「启用 LLM 分析」：不要偷偷替他调用模型
        if not self.cfg("enable_llm_analysis", True):
            return None, []

        umo = event.unified_msg_origin
        uid = str(event.get_sender_id())
        binding, _note = self._effective_binding(event)
        tools_on = self._chat_tools_available()
        # 上几轮对话：带工具时**作为真正的多轮消息**前置（见 _run_tool_loop），
        # 不带工具时退化成一段文本注入上下文 —— 单轮调用没有 messages 序列，
        # 只能这么给。两种形态给的都是同一份历史，口径不会分叉。
        history = self._nlu_chat_messages(umo) if tools_on else []
        try:
            # local_only：只拿本地事实，不预取任何网络数据。要什么数据
            # 由模型自己调工具要 —— 插件猜错一次，答案就整段跑偏。
            context = await self._collect_chat_context(
                question,
                umo,
                uid,
                binding,
                local_only=True,
                include_history=not tools_on,
            )
        except Exception as e:  # noqa: BLE001 - 兜底失败也要放行，不能吞消息
            logger.error(f"[dota2] 闲聊兜底收集数据失败: {e}", exc_info=True)
            return None, []

        prompt = dota_chat.build_chat_prompt(
            question, context, tooled=tools_on
        )
        system_prompt = dota_chat.build_chat_system_prompt(
            str(self.cfg("nlu_chat_system_prompt", "") or "")
        )
        reply: str | None = None
        trace = ""
        pending: list[str] = []
        if tools_on:
            reply, trace, pending = await self._nlu_chat_agent(
                event, prompt, system_prompt, history=history
            )
        else:
            # 关掉工具（``nlu_chat_tools=false``）时的形态：**仍然是大模型**，
            # 只是手上没有工具，能聊不能查。留这条是因为它便宜
            # （不触发任何数据源请求），不是因为它能替代工具路径。
            reply = await self._call_chat_llm(
                prompt, umo=umo, system_prompt=system_prompt
            )
        if not reply and not pending:
            logger.info("[dota2] 自然语言：模型没产出（通道不可用或报错），消息交回默认大模型")
            return None, []

        tool_calls = len([x for x in trace.split("; ") if x]) if trace else 0
        logger.info(
            f"[dota2] 自然语言交给模型: {question[:48]!r} "
            f"名单={len(context.watchers)}人 "
            f"会话比赛={len(context.session_matches)} "
            f"工具调用={tool_calls if tools_on else '未启用'} "
            f"待确认={len(pending)}项"
        )
        return reply, pending

    async def _collect_chat_context(
        self,
        question: str,
        umo: str,
        uid: str,
        binding: dict | None,
        focus: list[int] | None = None,
        *,
        local_only: bool = False,
        include_history: bool = True,
    ) -> dota_chat.ChatContext:
        """收集一次「带插件数据的回答」所需的会话语境。

        **闲聊兜底与定时播报共用这一份** —— 定时播报要答的就是同一类问题
        （「通报群里战绩情况」），而这份上下文里已经带齐了三样「坐标系」：
        当前时间、本会话最近发生过的比赛、以及识别到时间词时的时间窗口。
        两条路径各写一份的话，迟早会出现「手动问的和每天推的口径不一样」。

        Args:
            binding: 提问者的绑定（定时播报没有「提问者」，传 ``None``）。
            focus: 只取这几个人的数据；空列表表示按常规规则取。
            local_only: 只给本地事实、不预取任何网络数据。自然语言主路径
                （带工具的模型）用这个开关 —— 数据由模型自己调工具取。
            include_history: 是否把历史对话渲染成【最近对话】**文本段**注入
                上下文。带工具的主路径传 ``False``：那边改用真正的多轮消息
                序列（:meth:`_nlu_chat_messages`），两处都注入等于同一段
                对话出现两遍，还会把提示词撑长。不带工具的单轮路径只能
                用文本形态，传 ``True``。
        """
        return await dota_chat.collect_chat_context(
            self.api,
            question=question,
            umo=umo,
            user_id=uid,
            watchers=self.store.list_watchers(umo),
            bindings=list(self.store.list_bindings(umo).values()),
            # 本会话近期的比赛（监听推送 / 复盘 / 查询提到过的）：
            # 纯本地数据、零网络开销，永远注入。它带来的是「这个群
            # 最近发生过什么」以及每场比赛自己的开赛时间。
            recent_matches=self._nlu_recent_match_rows(umo, limit=8),
            # 最近几轮对话：模型要靠它消解指代（「那他昨天呢」）。
            # 去掉最后一条 —— 那是本次这句话，已经在【用户的问题】里了。
            # 带工具的路径传 include_history=False，改走真正的多轮消息。
            history=(
                self._nlu_chat_lines(umo)[:-1] if include_history else []
            ),
            self_binding=binding,
            focus_accounts=focus,
            recent_limit=int(
                self.cfg("nlu_chat_context_matches", dota_chat.DEFAULT_RECENT_LIMIT)
            ),
            max_players=int(
                self.cfg("nlu_chat_max_players", dota_chat.DEFAULT_MAX_PLAYERS)
            ),
            timeout=float(
                self.cfg("nlu_chat_timeout", dota_chat.DEFAULT_FETCH_TIMEOUT)
            ),
            cache=self._chat_cache,
            localizer=self._localize_heroes,
            now=time.time(),
            local_only=local_only,
            hero_pool_cfg={
                "min_games": int(
                    self.cfg("hero_pool_min_games", dota_pool.DEFAULT_MIN_GAMES) or 0
                ),
                "max_patches": int(
                    self.cfg("hero_pool_max_patches", dota_pool.DEFAULT_MAX_PATCHES) or 0
                ),
                "include_turbo": self.cfg("hero_pool_include_turbo", True),
                "patch_scope": self.cfg("hero_pool_patch_scope", True),
            },
        )

    def _nlu_pop_confirm(
        self, umo: str, uid: str
    ) -> tuple[str, str, float] | None:
        """取出并清理某个用户的待确认项（过期的自动丢弃）。"""
        key = (umo, uid)
        item = self._nlu_confirm.pop(key, None)
        if not item:
            return None
        if item[2] < time.time():
            return None
        return item

    @filter.event_message_type(filter.EventMessageType.ALL)
    @take_over_event(declinable=True)
    async def d2_natural(self, event: AstrMessageEvent):
        """自然语言入口：把「帮我看看我的战绩」这类人话交给模型去办。

        处理顺序（**v2.4.0 起只剩三步，别再按旧印象读**）：

        1. **确认 / 取消回复**。用户对上一条待确认动作的回话先处理掉，
           且不受唤醒词限制 —— 逼他再打一遍「dota2助手 确认」是没必要的摩擦。
           定时任务计划另走一套（确认对象是「已解析好的计划」而不是
           「(意图名, 参数)」，混在一起会让用户陷进「确认 → 又让你确认」）。
        2. **闸门**（:meth:`_nlu_should_handle`）：总开关、指令前缀、唤醒词、
           群聊 @ 要求。没通过就直接 ``return``，消息留给别人。
        3. **交给模型**（:meth:`_nlu_agent_reply`）：把插件的**全部能力**
           作为工具交给模型，由它决定调什么、调几次、还是直接回答。
           写操作工具只登记待确认，确认请求由插件原样发给用户；
           慢任务（单场复盘 / 催解析）只发起后台任务。

        **没有第 4 步。** v2.3.x 这里还有一层「大模型判意图 → 关键词规则 →
        指令 handler」的降级路径，v2.4.0 整层删掉了：只要规则还在主路径上，
        它就会一直漏（「这三天」「战报」这类新说法一个个补词表，补不完），
        而且判意图一次只能给一个 label，复合问题必然答一半。模型没产出时
        这里**不猜、也不吞消息**，直接放行给默认大模型。
        规则只剩下三处与「理解意图」无关的用途：唤醒词 / @ 闸门、确认 /
        取消词、`/d2` 指令前缀。

        .. important::

           这是挂在 ``EventMessageType.ALL`` 上的**全局监听**：每条消息
           都会进来跑一遍，其中绝大多数会被闸门挡下并 ``return``。
           因此必须用 ``@take_over_event(declinable=True)`` —— 只有真的
           处理了消息才终止传播。改成无条件的 ``take_over_event`` 会让
           插件一开启就掐掉同一事件上**其他插件的 handler 和默认大模型**
           （AstrBot 的 ``StarRequestSubStage`` 遇到 ``is_stopped()``
           会直接 ``break``），表现为「正常对话和其他插件全都没反应」。
        """
        text = dota_nlu.normalize(getattr(event, "message_str", "") or "")
        umo = event.unified_msg_origin
        uid = str(event.get_sender_id())

        # 会话场景（群 / 频道 / 私聊）：**每条消息都记一次**，与后面有没有
        # 被唤醒无关。QQ 官方适配器重启后会忘掉「哪个会话是群」，从而静默
        # 丢弃主动推送；这份记录就是重新补回那条信息的唯一来源。
        self._record_inbound_scene(event)

        # ---------- 1. 先处理「确认 / 取消」回复 ----------
        # 这一步**不受唤醒词限制**：用户的确认是对上一轮已授权操作的收尾，
        # 再逼他打一遍「dota2助手 确认」是没必要的摩擦。为了两种写法都能用，
        # 统一拿剥离唤醒词后的正文来比对。
        head = self._nlu_head_text(text)

        # 会话语境：无论这条消息最终有没有被处理，都记一笔。
        # 后面用户说「上面那盘」「这一局」时全靠它消歧。
        #
        # 记的是**剥离唤醒词之后的正文**。三个理由：① 历史里每句都顶着
        # 「dota2助手」既占位置，又会让模型以为每一轮都在点名；② 本轮问题
        # （``build_chat_prompt`` 的【用户的问题】）用的正是剥离后的正文，
        # 两边形态一致，模型读起来才像同一场对话；③ 唤醒词常常只是「叫一下
        # 机器人」，本身没有语义。
        if text:
            self._nlu_log_line(umo, "user", head or text)

        # 定时任务的确认走**另一套**：确认的对象是一份已经解析好的计划
        # （几时、做什么、发到哪），而不是「(意图名, 参数)」。若塞进通用确认，
        # 回执会被再解析一次，用户就会陷进「确认 → 又让你确认」的死循环。
        plan_result = self._nlu_schedule_plan_ack(umo, uid, head)
        if plan_result is not None:
            verdict, plan = plan_result
            if verdict == "cancel":
                yield event.plain_result("好的，这个定时任务就不建了。")
                return
            async for item in self._schedule_apply(plan, event, umo):
                yield item
            return

        pending = self._nlu_confirm.get((umo, uid))
        if pending is not None:
            if pending[2] < time.time():
                self._nlu_confirm.pop((umo, uid), None)
            elif head in NLU_CONFIRM_WORDS:
                name, args, _ = self._nlu_pop_confirm(umo, uid)
                agen = self._nlu_invoke(name, event, args)
                if agen is not None:
                    logger.info(f"[dota2] 自然语言确认执行: {name} {args!r}")
                    async for item in agen:
                        yield item
                return
            elif head in NLU_CANCEL_WORDS:
                self._nlu_pop_confirm(umo, uid)
                yield event.plain_result("好的，已取消。")
                return

        # ---------- 2. 该不该处理这条消息 ----------
        # 返回的是剥离唤醒词后的正文（没通过闸门时为 None）
        gate = self._nlu_should_handle(event, text)
        if gate is None:
            return
        effective = gate.text

        # ---------- 3. 交给大模型（唯一路径）----------
        # 把插件的全部能力（查数据 / 改设置 / 发起后台复盘）作为工具交给
        # 模型，由它自己决定调哪些、调几次、还是直接回答。**没有第二条路**。
        #
        # 为什么不做意图识别（关键词打分 / 分类器判 label）：
        #
        # 1. 分类一次只能给一个 label。一句话里常常要好几样东西
        #    （「钢板最近打得怎么样？顺便给他推荐几个轮椅」），判成哪个都
        #    必然漏答一半。工具调用天然支持一次调多个，复合问题才解得开。
        # 2. 更根本的是**兜不住**。用正则猜「这句话属于哪个功能」，词表再长
        #    也是穷举：实测「给群里这三天的战绩做个总结」因为词表里没有
        #    「三天」，被判成「不用打接口」，答出来的却是「最近 N 场」。
        #    后来把词表补齐了，下一个新说法照样会漏。规则只要还在主路径上，
        #    就得一直打补丁 —— 干脆不让它参与。
        #
        # 所以规则层整体退场：只保留三种与「理解意图」无关的用途 ——
        # 唤醒词 / @ 闸门（决定要不要理会这条消息）、确认 / 取消词
        # （协议回复）、以及 `/d2` 指令前缀。
        #
        # 要求 `keyword_matched`：唤醒词是**用户明确点名**插件的信号。
        # 没有它（即 `nlu_require_keyword=false` 且只 @ 了机器人）时不能
        # 走这条路 —— 那等于让插件接管所有 @ 消息，抢答风险太大。
        if gate.keyword_matched and self.cfg("nlu_chat_fallback", True):
            reply, pending_prompts = await self._nlu_agent_reply(event, effective)
            if reply or pending_prompts:
                if reply:
                    # **记进会话历史**（这是「接不上话」的头号原因）：以前
                    # 只记用户说的话，机器人自己的回复从来不记 —— 于是历史里
                    # 全是用户自说自话，模型下一轮根本不知道上一轮答了什么。
                    self._nlu_log_line(umo, "bot", reply)
                    async for item in self._emit(event, reply, as_image=False):
                        yield item
                # 写操作的确认请求**由插件原样发出**，不让模型转述：
                # 确认文案（尤其定时任务的计划文本）必须和用户逐字对齐。
                for prompt_text in pending_prompts:
                    yield event.plain_result(prompt_text)
            # 模型没产出（通道挂了 / 接口报错）时**不吞消息**：直接放行，
            # 交回默认大模型。这里从前会回落规则识别，结果是用户得去猜
            # 「为什么有时候认得出、有时候认不出」—— 不如干脆不猜。
            return

        # 走到这里说明「闸门通过了，但这条消息不该由模型处理」—— 例如
        # 用户关掉了 nlu_chat_fallback / 没开 LLM 分析 / 只 @ 了机器人而没写
        # 唤醒词。原样放行（不 yield、不 take over），交给默认大模型或别的
        # 插件。**不能什么都不做又不放行**，那等于把消息吃掉。
        return

    # ==================================================================
    # 指令：绑定 / 解绑
    # ==================================================================
    @d2.command("bind", alias={"绑定", "bd"})
    @take_over_event
    async def d2_bind(self, event: AstrMessageEvent, args: GreedyStr):
        """绑定 Dota2 玩家账号：/d2 绑定 <昵称|32位ID|64位SteamID>"""
        target = str(args).strip()
        if not target:
            yield event.plain_result(
                "用法：`/d2 绑定 <昵称 | 32位账号ID | 64位SteamID>`\n"
                "例如：`/d2 绑定 天鸽` 或 `/d2 绑定 86745912` 或 "
                "`/d2 绑定 76561198047011640`"
            )
            return

        try:
            account_id, name = await self._resolve_account(target)
        except OpenDotaError as e:
            yield event.plain_result(f"❌ 解析失败：{e}")
            return

        umo = event.unified_msg_origin
        uid = str(event.get_sender_id())
        await self.store.set_binding(
            umo, uid, account_id, name, to_steam_id64(account_id)
        )
        yield event.plain_result(
            f"✅ 绑定成功\n"
            f"玩家：{name}\n"
            f"account_id：{account_id}\n"
            f"SteamID64：{to_steam_id64(account_id)}\n\n"
            f"之后直接发送 `/d2 战绩`、`/d2 分析` 即可查询。"
        )

    @d2.command("unbind", alias={"解绑", "ub"})
    @take_over_event
    async def d2_unbind(self, event: AstrMessageEvent):
        """解除当前会话的 Dota2 账号绑定"""
        umo = event.unified_msg_origin
        uid = str(event.get_sender_id())
        binding = self.store.get_binding(umo, uid)
        if not binding:
            yield event.plain_result("你当前没有绑定任何账号。")
            return

        await self.store.remove_binding(umo, uid)

        # 顺带清理与这个账号相关的监听
        removed_watch = 0
        account_id = int(binding.get("account_id") or 0)
        for watcher in list(self.store.list_watchers(umo)):
            if int(watcher.get("account_id") or 0) != account_id:
                continue
            if await self.store.remove_watcher(watcher.get("id")):
                removed_watch += 1

        message = f"✅ 已解除绑定：{binding.get('personaname')}（{account_id}）"
        if removed_watch:
            message += f"\n同时移除了本会话针对该账号的 {removed_watch} 项监听。"
        yield event.plain_result(message)

    @d2.command("my", alias={"我的", "me", "当前绑定"})
    @take_over_event
    async def d2_my(self, event: AstrMessageEvent):
        """查看自己在当前会话的绑定"""
        umo = event.unified_msg_origin
        uid = str(event.get_sender_id())
        binding = self.store.get_binding(umo, uid)
        if not binding:
            yield event.plain_result(self._no_binding_reply())
            return

        account_id = int(binding.get("account_id") or 0)
        watched = self.store.find_watcher(umo, account_id) is not None
        yield event.plain_result(
            f"👤 当前绑定\n"
            f"玩家：{binding.get('personaname')}\n"
            f"account_id：{account_id}\n"
            f"SteamID64：{binding.get('steam_id') or to_steam_id64(account_id)}\n"
            f"绑定时间：{fmt_ago(binding.get('bound_at'))}\n"
            f"本会话监听：{'✅ 已开启' if watched else '❌ 未开启'}"
        )

    @d2.command("bindings", alias={"绑定列表", "列表", "blist"})
    @take_over_event
    async def d2_bindings(self, event: AstrMessageEvent):
        """查看本会话中所有成员绑定的账号"""
        umo = event.unified_msg_origin
        viewer = str(event.get_sender_id())
        bindings = self.store.list_bindings(umo)
        if not bindings:
            yield event.plain_result("本会话还没有任何绑定。")
            return

        lines = [f"📋 本会话共有 {len(bindings)} 个绑定：", ""]
        for index, (user_id, binding) in enumerate(bindings.items(), start=1):
            is_self = str(user_id) == viewer
            account_id = int(binding.get("account_id") or 0)
            watched = self._is_watched(umo, account_id)
            who = self._display_id(user_id, is_self)
            account = self._display_id(binding.get("account_id"), is_self)
            lines.append(
                f"{index}. {binding.get('personaname')}　"
                f"账号 {account}　用户 {who}"
                + ("　← 你" if is_self else "")
                + ("　🔔监听中" if watched else "")
            )
        if any(str(uid) != viewer for uid in bindings):
            lines.append("")
            lines.append("（为保护隐私，他人的账号与用户 ID 默认打码）")
        yield event.plain_result("\n".join(lines))

    def _is_watched(self, umo: str, account_id: int) -> bool:
        return self.store.find_watcher(umo, account_id) is not None

    @staticmethod
    def _mask_id(value: Any) -> str:
        """把 ID 的中段打码，用于在群聊里展示**他人**的账号信息。

        群聊是半公开场合，直接把成员的 QQ 号与 Steam / Dota 账号 ID 打出来
        会让任何人都能顺藤摸瓜查到别人的 Steam 主页，因此默认只展示首尾。
        """
        text = str(value if value is not None else "").strip()
        if not text:
            return "未知"
        if len(text) <= 4:
            return "*" * len(text)
        head = text[:4] if len(text) >= 8 else text[:2]
        tail = text[-2:]
        return f"{head}{'*' * max(2, len(text) - len(head) - len(tail))}{tail}"

    def _display_id(self, value: Any, is_self: bool) -> str:
        """按「是否本人」决定展示完整 ID 还是打码。"""
        if is_self or not self.cfg("mask_others_id", True):
            return str(value if value is not None else "")
        return self._mask_id(value)

    def _is_admin(self, user_id: str, event: AstrMessageEvent | None = None) -> bool:
        """判断该用户是否具备「管理他人监听」的权限。

        判定顺序：

        1. 配置项 ``admin_users`` 中列出的用户；
        2. AstrBot 自身的管理员（若当前版本的事件对象提供了 ``is_admin()``）。

        管理员可以取消本会话中**他人**添加的监听；普通成员只能管理自己的。
        两项都不满足时一律按普通成员处理，这是安全的一侧。
        """
        raw = str(self.cfg("admin_users", "") or "")
        admins = {
            item.strip() for item in re.split(r"[,，\s]+", raw) if item.strip()
        }
        if admins and str(user_id) in admins:
            return True

        if event is not None:
            checker = getattr(event, "is_admin", None)
            if callable(checker):
                try:
                    if checker():
                        return True
                except Exception:  # noqa: BLE001
                    # 不同版本的 AstrBot 签名可能不同，失败时按非管理员处理
                    pass
        return False

    # ==================================================================
    # 指令：玩家资料 / 英雄统计
    # ==================================================================
    @d2.command("info", alias={"资料", "player", "玩家"})
    @take_over_event
    async def d2_info(self, event: AstrMessageEvent, args: GreedyStr):
        """查看玩家资料：/d2 资料 [昵称|账号ID]"""
        target = str(args).strip()
        if target:
            try:
                account_id, name = await self._resolve_account(target)
            except OpenDotaError as e:
                yield event.plain_result(f"❌ {e}")
                return
        else:
            binding, _ = self._effective_binding(event)
            if not binding:
                yield event.plain_result(self._no_binding_reply())
                return
            account_id = int(binding.get("account_id") or 0)
            name = binding.get("personaname") or f"账号{account_id}"

        try:
            player_data = await self.api.get_player(account_id)
            if not player_data:
                yield event.plain_result(f"❌ OpenDota 查不到账号 {account_id}。")
                return
            wl = await self.api.get_player_wl(account_id)
            heroes = await self._heroes()
        except OpenDotaError as e:
            yield event.plain_result(f"❌ 查询失败：{e}")
            return

        pool = await self._hero_pool(account_id)
        text = format_player_profile(player_data, wl, pool.rows, heroes)
        if pool.has_data:
            text += f"\n常用英雄口径: {dota_pool.hero_pool_scope_text(pool)}"
        yield event.plain_result(text)

    @d2.command("heroes", alias={"英雄", "hero", "英雄池"})
    @take_over_event
    async def d2_heroes(self, event: AstrMessageEvent, args: GreedyStr):
        """查看英雄使用统计：/d2 英雄 [昵称|账号ID]

        口径是**当前版本**（样本不足时自动并入更早的版本并在文案里说明），
        并且**包含加速模式** —— 群里多数人的对局是加速局，不计入的话
        这个人的英雄池看起来几乎是空的。
        """
        target = str(args).strip()
        if target:
            try:
                account_id, name = await self._resolve_account(target)
            except OpenDotaError as e:
                yield event.plain_result(f"❌ {e}")
                return
        else:
            binding, _ = self._effective_binding(event)
            if not binding:
                yield event.plain_result(self._no_binding_reply())
                return
            account_id = int(binding.get("account_id") or 0)
            name = binding.get("personaname") or f"账号{account_id}"

        try:
            pool = await self._hero_pool(account_id)
            heroes = await self._heroes()
        except OpenDotaError as e:
            yield event.plain_result(f"❌ 查询失败：{e}")
            return

        text = dota_pool.format_hero_pool(pool, name, heroes)
        for chunk in self._chunk_text(text):
            yield event.plain_result(chunk)

    # ==================================================================
    # 指令：版本强势英雄（「轮椅」榜）
    # ==================================================================
    @d2.command(
        "wheelchair",
        alias={"轮椅", "版本英雄", "强势英雄", "胜率榜", "版本答案"},
    )
    @take_over_event
    async def d2_wheelchair(self, event: AstrMessageEvent, args: GreedyStr):
        """当前版本强势英雄榜：/d2 轮椅 [位置] [昵称|账号ID|我]

        不带玩家时只出客观榜单；写了昵称（或「我」）才追加个人适配推荐，
        因为它要额外拉英雄池与近期对局、还要过一次模型。
        """
        tokens = [token for token in re.split(r"\s+", str(args).strip()) if token]
        position = ""
        if tokens:
            matched = match_position(tokens[0])
            if matched:
                position = matched
                tokens = tokens[1:]
        target = " ".join(tokens).strip()
        if target in {"我", "自己", "我的", "本人", "me", "my"}:
            # 「我」= 当前会话的绑定账号（没绑的话下面会提示怎么绑）
            target, want_personal = "", True
        else:
            want_personal = bool(target)

        try:
            parts = await self._hero_meta_board_parts(position)
        except OpenDotaError as e:
            yield event.plain_result(f"❌ 查询失败：{e}")
            return

        if parts is None:
            yield event.plain_result(
                "没有拿到版本英雄数据（数据源可能暂时不可用），稍后再试。"
            )
            return

        for chunk in self._chunk_text(str(parts["board"])):
            yield event.plain_result(chunk)

        if not want_personal:
            yield event.plain_result(str(parts["footer"]))
            return

        # ---------- 个人适配 ----------
        if target:
            try:
                # 带上 umo：本会话名单里的名字（自然语言入口给的简称也经它
                # 归一成名单原名）直接换 account_id，省掉一次全局昵称搜索 ——
                # OpenDota 上重名账号太多，搜索既有歧义风险又慢。
                account_id, name = await self._resolve_account(
                    target, umo=event.unified_msg_origin
                )
            except OpenDotaError as e:
                yield event.plain_result(f"⚠️ 没找到「{target}」这个账号：{e}")
                return
        else:
            binding, _note = self._effective_binding(event)
            if not binding:
                yield event.plain_result(self._no_binding_reply())
                return
            account_id = int(binding.get("account_id") or 0)
            name = binding.get("personaname") or f"账号{account_id}"

        yield event.plain_result(f"⏳ 正在结合 {name} 的英雄池与近期表现筛选…")

        limit = self._clamp_count(None)
        try:
            pool = await self._hero_pool(account_id)
            matches, _economy = await self.api.get_player_matches_enriched(
                account_id, limit
            )
            try:
                profile = await self.api.get_player(account_id) or {}
                wl = await self.api.get_player_wl(account_id)
            except OpenDotaError as e:
                logger.warning(f"[dota2] 补充玩家资料失败: {e}")
                profile, wl = {}, {}
        except OpenDotaError as e:
            yield event.plain_result(f"❌ 拉取 {name} 的数据失败：{e}")
            return

        hero_rows = pool.rows
        if not hero_rows and not matches:
            yield event.plain_result(
                f"没有查到 {name} 的英雄池与近期对局，做不了个人适配。"
            )
            return

        header = f"🎯 {name} 的版本轮椅（在版本强势英雄里挑适合他的）"
        prompt = build_hero_pick_prompt(
            account_id=account_id,
            profile_data=profile,
            wl=wl,
            hero_rows=hero_rows,
            matches=matches,
            meta_rows=parts["rows"],
            meta=parts["meta"],
            heroes=parts["heroes"],
            patch=parts["patch"],
            pool_scope=dota_pool.hero_pool_scope_text(pool) if pool.has_data else "",
        )
        report = await self._generate_report(event, prompt)
        if report:
            yield event.plain_result(header)
            async for item in self._emit(event, report, as_image=True):
                yield item
            return

        # 模型不可用：退回纯数据交叉的规则版，不硬凑一段分析
        fallback = pick_heroes_for_player(
            parts["rows"], hero_rows, matches, parts["heroes"]
        )
        if not fallback:
            yield event.plain_result(
                f"{header}\n\n"
                "没筛出合适的：他的英雄池里没有版本强势英雄，"
                "近期记录也不足以判断他常打的位置。"
            )
            return
        scope_line = (
            f"（英雄池口径：{dota_pool.hero_pool_scope_text(pool)}）\n\n"
            if pool.has_data
            else ""
        )
        for chunk in self._chunk_text(f"{header}\n\n{scope_line}{fallback}"):
            yield event.plain_result(chunk)

    async def _hero_meta_board_parts(
        self, position: str = ""
    ) -> dict[str, Any] | None:
        """拉取并渲染「当前版本强势英雄榜」。

        抽出来是为了让**定时播报**（见 ``/d2 定时``）复用同一份取数与渲染 ——
        两条路径各写一遍，迟早会出现「手动查到的榜和每天推的不是一个」。
        个人适配那半段仍然留在 :meth:`d2_wheelchair` 里：它要额外拉英雄池与
        近期对局、还要过一次模型，定时播报用不上。

        Returns:
            ``{"board", "footer", "rows", "meta", "heroes", "patch"}``；
            数据源没给英雄数据时返回 ``None``。``OpenDotaError`` 照常抛出，
            由调用方决定怎么提示（手动查询要告诉用户原因，定时播报只记日志）。
        """
        hero_stats = await self.api.get_hero_stats()
        heroes = await self._heroes()
        patch = await self.api.get_latest_patch()
        if not hero_stats:
            return None
        rows, meta = hero_meta_rows(
            hero_stats,
            position=position,
            min_pick=int(self.cfg("hero_meta_min_pick", 0) or 0),
        )
        board = format_hero_meta_board(
            rows,
            meta,
            heroes,
            patch=patch,
            top=max(1, int(self.cfg("hero_meta_board_size", 10) or 10)),
            hot_top=max(0, int(self.cfg("hero_meta_hot_size", 5) or 0)),
            cold_top=max(0, int(self.cfg("hero_meta_cold_size", 3) or 0)),
        )
        return {
            "board": board,
            "footer": format_hero_meta_footer(meta),
            "rows": rows,
            "meta": meta,
            "heroes": heroes,
            "patch": patch,
        }

    # ==================================================================
    # 指令：战绩
    # ==================================================================
    @d2.command("matches", alias={"战绩", "rec", "比赛"})
    @take_over_event
    async def d2_matches(self, event: AstrMessageEvent, args: GreedyStr):
        """查看最近战绩：/d2 战绩 [场次] [昵称|账号ID]"""
        count, target = self._split_count_target(str(args))

        if target:
            try:
                account_id, name = await self._resolve_account(target)
            except OpenDotaError as e:
                yield event.plain_result(f"❌ {e}")
                return
            note = ""
        else:
            binding, note = self._effective_binding(event)
            if not binding:
                yield event.plain_result(self._no_binding_reply())
                return
            account_id = int(binding.get("account_id") or 0)
            name = binding.get("personaname") or f"账号{account_id}"

        limit = self._clamp_count(count)
        try:
            matches, _ = await self.api.get_player_matches_enriched(account_id, limit)
            heroes = await self._heroes()
        except OpenDotaError as e:
            yield event.plain_result(f"❌ 查询失败：{e}")
            return

        if not matches:
            yield event.plain_result(f"没有查询到 {name} 的比赛记录。可能该账号未公开比赛数据。")
            return
        # 记进会话语境（批量，不逐条写日志）：随后说「详细分析第三场」这类话
        # 才有得猜。列表本身已经列出了编号，用户照着报也行。
        for row in matches[:5]:
            self._nlu_remember_match(
                event.unified_msg_origin,
                row.get("match_id"),
                name,
                log=False,
                start_time=row.get("start_time"),
            )
        title = f"📊 {name} 的最近 {len(matches)} 场比赛"
        if note:
            title += note
        text = format_match_list(name, account_id, matches, heroes, title=title)
        for chunk in self._chunk_text(text):
            yield event.plain_result(chunk)

    def _clamp_count(self, count: int | None) -> int:
        """把用户指定的场次限制在合理范围内。"""
        default = max(1, int(self.cfg("default_match_count", 20)))
        maximum = max(1, int(self.cfg("max_match_count", 50)))
        if count is None:
            return min(default, maximum)
        return max(1, min(int(count), maximum))

    # ==================================================================
    # 指令：分析近期表现
    # ==================================================================
    @d2.command("analyze", alias={"分析", "recent", "复盘"})
    @take_over_event
    async def d2_analyze(self, event: AstrMessageEvent, args: GreedyStr):
        """AI 分析近期表现与打法风格：/d2 分析 [场次] [昵称|账号ID]"""
        count, target = self._split_count_target(str(args))

        if target:
            try:
                account_id, name = await self._resolve_account(target)
            except OpenDotaError as e:
                yield event.plain_result(f"❌ {e}")
                return
            note = ""
        else:
            binding, note = self._effective_binding(event)
            if not binding:
                yield event.plain_result(self._no_binding_reply())
                return
            account_id = int(binding.get("account_id") or 0)
            name = binding.get("personaname") or f"账号{account_id}"

        limit = self._clamp_count(count)
        yield event.plain_result(f"⏳ 正在拉取 {name} 最近 {limit} 场数据并生成分析，请稍候…")

        try:
            matches, economy_samples = await self.api.get_player_matches_enriched(
                account_id, limit
            )
            heroes = await self._heroes()
            try:
                player_data = await self.api.get_player(account_id) or {}
                wl = await self.api.get_player_wl(account_id)
            except OpenDotaError as e:
                logger.warning(f"[dota2] 补充玩家资料失败: {e}")
                player_data, wl = {}, {}
        except OpenDotaError as e:
            yield event.plain_result(f"❌ 拉取数据失败：{e}")
            return

        if not matches:
            yield event.plain_result(f"没有查询到 {name} 的比赛记录。")
            return

        pool = await self._hero_pool(account_id)
        prompt = build_recent_analysis_prompt(
            account_id=account_id,
            profile_data=player_data,
            wl=wl,
            matches=matches,
            heroes=heroes,
            hero_rows=pool.rows,
            economy_samples=economy_samples,
            requested_count=limit,
            pool_scope=dota_pool.hero_pool_scope_text(pool) if pool.has_data else "",
        )

        report = await self._generate_report(event, prompt)
        header = (
            f"🔍 {name} 近期表现分析（样本 {len(matches)} 场"
            f"{'，含全场次' if economy_samples >= len(matches) else f'，其中 {economy_samples} 场含经济数据'}）"
            f"{note}"
        )
        if report:
            yield event.plain_result(header)
            async for item in self._emit(event, report, as_image=True):
                yield item
        else:
            summary = summarize_matches(matches, economy_samples=economy_samples)
            fallback = (
                f"{header}\n\n"
                "⚠️ 未启用大模型分析或模型不可用，以下为原始统计数据：\n\n"
                + format_summary_block(summary, heroes)
                + "\n\n"
                + format_match_list(name, account_id, matches, heroes, title="逐场明细")
            )
            for chunk in self._chunk_text(fallback):
                yield event.plain_result(chunk)

    # ==================================================================
    # 指令：单场复盘
    # ==================================================================
    @d2.command("match", alias={"单场", "detail", "复盘单场"})
    @take_over_event
    async def d2_match(self, event: AstrMessageEvent, args: GreedyStr):
        """AI 深度复盘单场比赛：/d2 单场 <比赛ID> [焦点玩家]

        未指定 skip 时，若该局尚未解析完成，会自动催解析并每分钟检查一次，
        最多等 10 分钟。等满仍未解析时**不会只回一句放弃**：只要手上还有这
        场比赛的基础数据，就自动降级出一份「未解析版」复盘。
        """
        tokens = [token for token in re.split(r"\s+", str(args).strip()) if token]
        if not tokens or not re.fullmatch(r"\d{6,20}", tokens[0]):
            yield event.plain_result(
                "用法：`/d2 单场 <比赛ID> [焦点玩家] [skip]`\n"
                "例如：`/d2 单场 8989601141`\n"
                "多位焦点：`/d2 单场 8989601141 张三、李四、王五`"
                "（昵称或账号 ID，用 、/, 分隔；昵称可以含空格）\n"
                "不想等待解析：`/d2 单场 8989601141 skip`\n"
                "比赛 ID 可以从 `/d2 战绩` 的结果中获取，或直接使用 Dota 客户端的比赛编号。"
            )
            return

        match_id = int(tokens[0])
        rest_tokens = tokens[1:]
        # `/d2 单场 <id> skip`：跳过「等解析」，直接用现有数据出报告
        skip_wait = any(t.lower() in PARSE_SKIP_WORDS for t in rest_tokens)
        rest = " ".join(t for t in rest_tokens if t.lower() not in PARSE_SKIP_WORDS)

        yield event.plain_result(
            f"⏳ 正在拉取比赛 {match_id} 的详细数据并生成复盘，请稍候…"
        )

        try:
            match = await self.api.get_match(match_id)
            if not match:
                # 走到这里说明**两端都问过了**（降级器会在主源返回空时补问
                # 后备），所以可以放心说「查不到」，不必再单点 OpenDota。
                yield event.plain_result(
                    f"❌ 查不到比赛 {match_id}：STRATZ 与 OpenDota 都没有这盘的数据。\n"
                    "可能原因：比赛 ID 写错、该局刚结束还没被收录（可过几分钟再试）、"
                    "或对局方未公开比赛数据。\n"
                    f"如果是刚打完的局，可以用 `{self._nlu_keyword()} 催一下 "
                f"{match_id} 的解析` 试一次。"
                    )
                return
            heroes = await self._heroes()
            items = await self._items()
            # 记进会话语境：紧接着问「再详细说说这把」时不用再报 ID。
            # 顺带带上开赛时间 —— 闲聊兜底问「昨天那几盘」时要用它判断时效。
            self._nlu_remember_match(
                event.unified_msg_origin,
                match_id,
                start_time=match.get("start_time"),
            )
        except OpenDotaError as e:
            yield event.plain_result(f"❌ 拉取比赛数据失败：{e}")
            return

        # 焦点解析放在拉取之后：本局十人名单是天然的昵称消歧器，
        # 「大魔导师马化腾」这类重名昵称只有在局内唯一时才能被解析出来。
        players_in_match = [
            player
            for player in (match.get("players") or [])
            if isinstance(player, dict)
        ]

        focus_ids: list[int] = []
        focus_names: dict[int, str] = {}
        failed: list[str] = []

        if rest:
            for query in (q.strip() for q in FOCUS_SPLIT_RE.split(rest)):
                if not query:
                    continue
                try:
                    account_id, name = await self._resolve_account(
                        query, in_match_players=players_in_match
                    )
                except OpenDotaError as e:
                    failed.append(f"{query}（{e}）")
                    continue
                account_id = int(account_id or 0)
                if account_id and account_id not in focus_ids:
                    focus_ids.append(account_id)
                    focus_names[account_id] = name
            if failed and not focus_ids:
                yield event.plain_result(
                    "❌ 焦点玩家解析失败：\n"
                    + "\n".join(f"· {item}" for item in failed)
                    + "\n建议直接使用 32 位账号 ID 或 64 位 SteamID，结果更准确。"
                )
                return

        if failed:
            yield event.plain_result(
                "⚠️ 以下焦点未能解析，已跳过：" + "、".join(failed)
            )

        # 未指定焦点玩家时，若本会话的绑定出现在这场比赛中，则自动聚焦
        if not focus_ids:
            binding, _ = self._effective_binding(event)
            if binding:
                candidate = int(binding.get("account_id") or 0)
                if candidate and any(
                    isinstance(player, dict)
                    and player.get("account_id") == candidate
                    for player in match.get("players") or []
                ):
                    focus_ids.append(candidate)
                    focus_names[candidate] = binding.get("personaname") or ""

        parse_state = dota_parse.parse_state(match)
        parsed = parse_state.parsed

        # ---------- 未解析：催解析 + 等待，改由后台任务完成复盘 ----------
        if not parsed and not skip_wait and self.cfg("parse_wait_enabled", True):
            options = self._parse_wait_options()
            minutes = max(1, round(int(options["timeout"]) / 60))
            seconds = int(options["check_interval"])
            if self._start_parse_task(
                event, match_id, focus_ids, focus_names, fresh=False
            ):
                fallback_hint = (
                    "仍未解析就自动改用现有数据出一份基础数据版的报告"
                    if self.cfg("parse_fallback_unparsed", True)
                    else "等不到会通知你"
                )
                yield event.plain_result(
                    f"🔍 比赛 {match_id} 数据完整度：{parse_state.describe()}\n"
                    f"AI 复盘依赖逐分钟经济、团战与出装数据，"
                    f"{'已提交催解析并' if self.cfg('parse_submit_request', True) else ''}"
                    f"开始等待：每 {_fmt_clock(seconds)} 检查一次，"
                    f"最多等 {minutes} 分钟，{fallback_hint}。\n"
                    f"报告出来后会自动发到本会话，你可以先去忙别的。\n"
                    f"（不想等待：`/d2 单场 {match_id} skip` 直接用基础数据出报告）"
                )
                return
            yield event.plain_result(
                "⚠️ 当前等待解析的任务过多，无法排队。"
                f"已改用基础数据（{parse_state.describe()}）生成复盘。\n"
                f"稍后可用 `/d2 单场 {match_id}` 重新尝试等待解析。"
            )

        headline = self.match_headline(
            match, heroes, focus_ids or None, parsed, focus_names
        )

        # 附上焦点玩家近期的整体状态，让复盘更有上下文。
        # 焦点可能不止一位，逐位取；超过上限的只做本场复盘，避免提示词被撑爆。
        extra_context = await self._build_recent_context(
            match_id, focus_ids, focus_names, heroes
        )
        abilities = await self._ability_constants()
        curve_ids = self._curve_targets(match, focus_ids)

        prompt = build_single_match_analysis_prompt(
            match=match,
            heroes=heroes,
            items=items,
            focus_account_ids=focus_ids or None,
            focus_names=focus_names or None,
            extra_context=extra_context,
            abilities=abilities,
            curve_ids=curve_ids,
        )

        report = await self._generate_report(event, prompt)
        if report:
            yield event.plain_result(headline)
            async for item in self._emit(event, report, as_image=True):
                yield item
        else:
            raw = (
                f"{headline}\n\n"
                "⚠️ 未启用大模型分析或模型不可用，以下为从数据源获取的完整原始数据：\n\n"
                + build_match_data_text(
                    match,
                    heroes,
                    items,
                    focus_account_ids=focus_ids or None,
                    abilities=abilities,
                    curve_ids=curve_ids,
                )
            )
            for chunk in self._chunk_text(raw):
                yield event.plain_result(chunk)

    # ==================================================================
    # 指令：催解析（只催不等，适合还没打算看复盘时先排上队）
    # ==================================================================
    @d2.command("parse", alias={"催解析", "申请解析", "催一下", "强制解析"})
    @take_over_event
    async def d2_askparse(self, event: AstrMessageEvent, args: GreedyStr):
        """只向 OpenDota 提交解析申请，不等待结果：/d2 催解析 <比赛ID>"""
        tokens = [token for token in re.split(r"\s+", str(args).strip()) if token]
        if not tokens or not re.fullmatch(r"\d{6,20}", tokens[0]):
            yield event.plain_result(
                "用法：`/d2 催解析 <比赛ID>`\n"
                "例如：`/d2 催解析 8995419388`\n\n"
                "作用：向 OpenDota 提交这局的存档解析申请（OpenDota 平时不一定会"
                "主动解析别人的对局，催一下才会去取录像）。\n"
                "· 只想催、不等结果 → 用本指令；\n"
                "· 想等解析完自动出复盘报告 → 用 `/d2 单场 <比赛ID>`。"
            )
            return

        match_id = int(tokens[0])
        yield event.plain_result(
            f"⏳ 正在检查比赛 {match_id} 的解析状态并提交申请…"
        )

        try:
            match = await self.api.get_match(match_id)
        except OpenDotaError as e:
            yield event.plain_result(f"❌ 拉取比赛数据失败：{e}")
            return

        if not match:
            yield event.plain_result(
                f"❌ 找不到比赛 {match_id}，或该比赛尚未被 OpenDota 收录。\n"
                f"未被收录时提交解析申请没有意义，"
                f"一般等几分钟到几十分钟 OpenDota 会自动收录。"
            )
            return

        state = dota_parse.parse_state(match)
        if state.parsed:
            yield event.plain_result(
                f"✅ 比赛 {match_id} 已经解析完成（{state.describe()}），无需催解析。\n"
                f"现在就可以用 `/d2 单场 {match_id}` 出复盘。"
            )
            return

        granted = await dota_parse.submit_parse(self.api, match_id)
        if granted:
            yield event.plain_result(
                f"📨 已向 OpenDota 提交比赛 {match_id} 的解析申请，任务已排队。\n"
                f"当前状态：{state.describe()}\n\n"
                f"解析通常需要几分钟到几十分钟。想拿到结果后自动出报告，"
                f"用 `/d2 单场 {match_id}`（会每分钟检查一次，最多等 10 分钟；"
                f"等满仍未解析会自动改用现有数据出一份基础数据版）。"
            )
        else:
            yield event.plain_result(
                f"⚠️ 解析申请没有返回排队凭据，可能原因：\n"
                f"· OpenDota 认为这局无法解析（临时对局 / 录像已过期）；\n"
                f"· 该局已在解析队列中（这时其实不用再催）；\n"
                f"· 请求被限流（配置里填写 OpenDota API Key 可以提升额度）。\n\n"
                f"当前状态：{state.describe()}\n"
                f"可以过几分钟再用 `/d2 单场 {match_id}` 试探一次。"
            )

    # ==================================================================
    # 指令：监听
    # ==================================================================
    @d2.command("watch", alias={"监听", "订阅"})
    @take_over_event
    async def d2_watch(self, event: AstrMessageEvent, args: GreedyStr):
        """监听玩家，比赛结束后自动推送分析：/d2 监听 [昵称|账号ID]"""
        target = str(args).strip()
        if target:
            try:
                account_id, name = await self._resolve_account(target)
            except OpenDotaError as e:
                yield event.plain_result(f"❌ {e}")
                return
        else:
            binding, _ = self._effective_binding(event)
            if not binding:
                yield event.plain_result(self._no_binding_reply())
                return
            account_id = int(binding.get("account_id") or 0)
            name = binding.get("personaname") or f"账号{account_id}"

        if not self.cfg("watch_enabled", True):
            yield event.plain_result(
                "⚠️ 当前插件配置中已关闭「启用比赛监听」，请在 AstrBot 管理面板中开启。"
            )
            return

        umo = event.unified_msg_origin

        # 每次新比赛都会消耗一次大模型调用，因此限制单会话的监听数量，
        # 防止群里任何人都能无限叠加监听把配额吃掉。
        if self.store.find_watcher(umo, account_id) is None:
            cap = max(1, int(self.cfg("watch_max_per_session", 5)))
            if len(self.store.list_watchers(umo)) >= cap:
                yield event.plain_result(
                    f"⚠️ 本会话的监听数量已达上限（{cap} 个）。\n"
                    f"请先用 `/d2 监听列表` 查看现有监听，"
                    f"并用 `/d2 取消监听 <账号|全部>` 清理后再添加。\n"
                    f"如需提高上限，请调整插件配置项 `watch_max_per_session`。"
                )
                return

        yield event.plain_result("⏳ 正在初始化监听…")

        try:
            recent = await self.api.get_player_matches(account_id, limit=1)
        except OpenDotaError as e:
            yield event.plain_result(f"❌ 初始化监听失败：{e}")
            return

        baseline = int(recent[0].get("match_id") or 0) if recent else 0
        watcher, created = await self.store.add_watcher(
            umo=event.unified_msg_origin,
            account_id=account_id,
            personaname=name,
            last_match_id=baseline,
            platform=event.get_platform_name(),
            created_by=str(event.get_sender_id()),
            created_by_name=event.get_sender_name(),
        )

        if not created:
            yield event.plain_result(
                f"ℹ️ 本会话已经在监听 {name}（{account_id}），无需重复添加。\n"
                f"当前基线比赛：{watcher.get('last_match_id')}"
            )
            return

        # 用户可能在运行期间才打开监听开关，这里做一次懒启动
        self._start_watcher()

        interval = max(60, int(self.cfg("watch_interval", 180)))

        # 平台预检：QQ 官方开放平台这类通道**可以**主动推送，但群聊里要逐群开启，
        # 没开时平台直接回 `40034105 主动消息失败, 无权限`。这里先把「去哪开」
        # 说清楚——否则用户只能看到推送失败，不知道该动哪里。
        #
        # 早期版本的提示写的是「当前平台不支持机器人主动发送消息」，那是**错的**，
        # 会把排查方向带偏（这个坑本插件踩过两天：明明只是群里没开开关）。
        platform_setup = ""
        platform_label = PLATFORMS_NEEDING_PROACTIVE_SETUP.get(
            str(event.get_platform_name() or "")
        )
        if platform_label:
            platform_setup = (
                f"\n\n⚠️ 注意：{platform_label}要在**每个群单独**开启主动消息权限，"
                f"否则比赛结束时推不进来。\n"
                f"· 手机 QQ → 该群 → 群设置 → 机器人 → 选中本机器人 → "
                f"打开「机器人主动在群聊内发言」\n"
                f"· 只有群主能改，且 QQ 客户端需 9.2.90 以上才会看到这个开关\n"
                f"· 私聊权限与群聊是分开的，私聊能发不代表群里能发"
            )

        yield event.plain_result(
            f"🔔 已开始监听 {name}（account_id: {account_id}）\n"
            f"· 推送目标：本会话\n"
            f"· 轮询间隔：{interval} 秒\n"
            f"· 已记录基线比赛：{baseline}（只有此后进行的新比赛才会推送）\n"
            f"· 数据源收录比赛后，会将简报发送到这里\n"
            f"· 取消监听：`/d2 取消监听 {account_id}`"
            + platform_setup
        )

    @d2.command("unwatch", alias={"取消监听", "取消订阅", "停止监听", "关闭监听", "取消关注"})
    @take_over_event
    async def d2_unwatch(self, event: AstrMessageEvent, args: GreedyStr):
        """取消监听：/d2 取消监听 <昵称|账号ID|全部>"""
        umo = event.unified_msg_origin
        target = str(args).strip()
        uid = str(event.get_sender_id())
        is_admin = self._is_admin(uid, event)

        if not target:
            yield event.plain_result(
                "用法：`/d2 取消监听 <昵称|账号ID|全部>`\n"
                "· `/d2 取消监听 86745912`\n"
                "· `/d2 取消监听 全部` 取消你添加的所有监听\n"
                "（也支持「停止监听」「关闭监听」等说法，与「取消监听」等价）"
            )
            return

        if target in ("全部", "all", "ALL", "所有"):
            watchers = self.store.list_watchers(umo)
            if not watchers:
                yield event.plain_result("本会话没有任何监听。")
                return

            # 普通成员只能取消自己添加的监听；管理员可以取消本会话的全部监听。
            mine = [w for w in watchers if str(w.get("created_by") or "") == uid]
            removable = watchers if is_admin else mine
            if not removable:
                yield event.plain_result(
                    "本会话的监听都不是你添加的，无法取消。\n"
                    "你只能取消自己添加的监听；如需清理他人的监听，"
                    "请联系插件管理员（配置项 `admin_users`）。"
                )
                return

            for watcher in removable:
                await self.store.remove_watcher(watcher.get("id"))
            if len(removable) == len(watchers):
                yield event.plain_result(f"✅ 已取消本会话全部 {len(removable)} 项监听。")
            else:
                yield event.plain_result(
                    f"✅ 已取消你添加的 {len(removable)} 项监听。\n"
                    f"（本会话另有 {len(watchers) - len(removable)} 项由他人添加，未受影响）"
                )
            return

        # 先按 ID 直接匹配，避免不必要的搜索请求
        account_id: int | None = None
        if re.fullmatch(r"\d{5,20}", target):
            account_id = to_account_id(target)
        if account_id is None:
            # 昵称：在本会话已有的监听里模糊匹配，匹配不到再走 OpenDota 搜索
            for watcher in self.store.list_watchers(umo):
                if target.lower() in str(watcher.get("personaname") or "").lower():
                    account_id = int(watcher.get("account_id") or 0)
                    break
        if account_id is None:
            try:
                account_id, _ = await self._resolve_account(target)
            except OpenDotaError as e:
                yield event.plain_result(f"❌ {e}")
                return

        watcher = self.store.find_watcher(umo, account_id)
        if not watcher:
            yield event.plain_result(f"本会话没有在监听账号 {account_id}。")
            return

        # 只有添加者本人或管理员才能取消，避免群友互删监听。
        owner = str(watcher.get("created_by") or "")
        if owner != uid and not is_admin:
            owner_label = watcher.get("created_by_name") or self._mask_id(owner)
            yield event.plain_result(
                f"这项监听是 {owner_label} 添加的，你无法取消。\n"
                f"如需取消，请联系本人或插件管理员（配置项 `admin_users`）。"
            )
            return

        await self.store.remove_watcher(watcher.get("id"))
        yield event.plain_result(
            f"✅ 已取消监听 {watcher.get('personaname')}（{account_id}）。"
        )

    @d2.command("watchlist", alias={"监听列表", "我的监听", "wlist"})
    @take_over_event
    async def d2_watchlist(self, event: AstrMessageEvent):
        """查看本会话的监听列表"""
        umo = event.unified_msg_origin
        watchers = self.store.list_watchers(umo)
        if not watchers:
            yield event.plain_result(
                "本会话还没有监听任何玩家。\n"
                "发送 `/d2 监听` 即可监听你自己绑定的账号。"
            )
            return

        viewer = str(event.get_sender_id())
        interval = max(60, int(self.cfg("watch_interval", 180)))
        lines = [
            f"🔔 本会话共监听 {len(watchers)} 个玩家（轮询间隔 {interval} 秒）",
            "",
        ]
        for index, watcher in enumerate(watchers, start=1):
            creator = str(watcher.get("created_by") or "")
            creator_label = (
                watcher.get("created_by_name")
                or self._display_id(creator, creator == viewer)
                or "未知"
            )
            if creator and creator == viewer:
                creator_label = f"{creator_label}（你）"
            lines.append(
                f"{index}. {watcher.get('personaname')}　"
                f"账号 {watcher.get('account_id')}\n"
                f"　　添加者：{creator_label}"
                f"　添加时间：{fmt_ago(watcher.get('created_at'))}\n"
                f"　　基线比赛：{watcher.get('last_match_id') or '未记录'}"
            )
        pending = list(self._pending.values())
        if pending:
            lines.append("")
            lines.append(f"⏳ 当前有 {len(pending)} 场比赛正在等待数据源收录：")
            for item in pending[:5]:
                focus_label = "、".join(
                    str(focus.get("name") or focus.get("account_id"))
                    for focus in item.get("focuses") or []
                )
                lines.append(
                    f"　　· 比赛 {item['match_id']}"
                    f"（焦点：{focus_label or '未知'}）"
                    f"　等收录 {item.get('wait_attempts', 0)} 次"
                )
        yield event.plain_result("\n".join(lines))

    # ==================================================================
    # 指令：模型自检
    # ==================================================================
    @d2.command("llmtest", alias={"模型测试", "模型自检", "测试模型", "llm"})
    @take_over_event
    async def d2_llmtest(self, event: AstrMessageEvent):
        """检查闲聊与分析两条模型通道是否可用"""
        yield event.plain_result("⏳ 正在测试模型通道，请稍候…")
        yield event.plain_result(await self._llm_selftest(event.unified_msg_origin))

    async def _llm_selftest(self, umo: str) -> str:
        """实测**两条**模型通道，回显各自走谁、通不通。

        插件的模型分工（v2.3.4 起）：

        * **闲聊**（自然语言入口 + 工具调用）→ 默认模型优先、专用 Key 兜底；
        * **比赛分析**（复盘 / 分析 / 轮椅适配 / 赛后短评 / 定时播报正文）
          → 专用 Key 优先、默认模型兜底。

        两条要**分别测**：只报一条，用户会误以为另一条也不可用（或反之）。
        """
        if not self.cfg("enable_llm_analysis", True):
            return (
                "⚠️ AI 功能当前是关闭的（配置项「启用 AI 分析」）。\n"
                "把它打开后 `/d2 分析`、`/d2 单场` 才会有 AI 报告，"
                "自然语言入口也才会带上工具。"
            )
        lines = ["🔎 模型通道自检（闲聊与分析是两条独立的通道）", ""]
        lines.append(await self._selftest_chat_channel(umo))
        lines.append("")
        lines.append(await self._selftest_analysis_channel(umo))
        return "\n".join(lines)

    async def _selftest_chat_channel(self, umo: str) -> str:
        """闲聊通道：默认模型优先，专用 Key 兜底。"""
        head = "【闲聊 / 自然语言入口】"
        provider = await resolve_provider(
            self.context, umo, str(self.cfg("llm_provider_id", "") or "")
        )
        if provider is None:
            body = (
                f"{head}\n"
                "❌ 拿不到 AstrBot 的默认模型提供商 —— 闲聊会没有模型可用。\n"
                "　　请在 AstrBot 里配置一个模型提供商，或给插件填专用 API Key 兜底。"
            )
        else:
            label = self._provider_label(provider)
            started = time.monotonic()
            try:
                reply = await call_llm(
                    provider,
                    "你是一个连通性测试助手，只回答用户要求的内容，不要添加任何多余的话。",
                    "请只回复两个字：可用",
                )
            except Exception as e:  # noqa: BLE001
                elapsed = time.monotonic() - started
                body = (
                    f"{head}\n"
                    f"❌ {label} 测试失败（耗时 {elapsed:.1f} 秒）：{e}"
                )
            else:
                elapsed = time.monotonic() - started
                body = (
                    f"{head}\n"
                    f"✅ {label} 可用（耗时 {elapsed:.1f} 秒）\n"
                    f"　　模型回显：{reply}"
                )
        # 工具能力单独说一句：闲聊的「一句话多件事」靠 provider 的 func_tool，
        # 而这是 AstrBot 较新版本才有的 —— 老版本上会自动退回专用 Key。
        try:
            from astrbot.api import ToolSet  # noqa: F401

            tool_note = "　　工具调用：支持（可让模型自己决定调哪些功能）"
        except Exception:  # noqa: BLE001
            tool_note = "　　⚠️ 工具调用：当前 AstrBot 未暴露 ToolSet，闲聊会自动退回专用 Key"
        return body + "\n" + tool_note

    async def _selftest_analysis_channel(self, umo: str) -> str:
        """分析通道：专用 Key 优先，默认模型兜底。"""
        head = "【比赛分析 / 报告】"
        client = self._dedicated_client()
        if client is None:
            return await self._llm_selftest_fallback(
                umo,
                f"{head}\n"
                "ℹ️ 没有配置专用的模型 API Key，比赛分析将回退 AstrBot 的默认模型。\n"
                "　　想让分析走独立 Key（费用与闲聊分开），在插件配置里填「专用 API Key」。\n",
            )

        configured = [
            f"　接口地址：{client.endpoint}",
            f"　模型名称：{client.model or '（未配置，会直接报错）'}",
            f"　超时：{client.timeout:g} 秒",
            f"　代理：{client.proxy or '不使用'}",
        ]
        if not client.model:
            return (
                f"{head}\n"
                "❌ 专用模型通道配置不完整：填了 API Key，但没填模型名称。\n"
                + "\n".join(configured)
                + "\n\n请在插件配置里补上「模型名称」，例如 `deepseek-chat`。"
            )

        started = time.monotonic()
        try:
            reply = await client.chat(
                "你是一个连通性测试助手，只回答用户要求的内容，不要添加任何多余的话。",
                "请只回复两个字：可用",
                temperature=0.0,
                max_tokens=32,
            )
        except LLMRequestError as e:
            elapsed = time.monotonic() - started
            return (
                f"{head}\n"
                f"❌ 专用模型通道测试失败（耗时 {elapsed:.1f} 秒）\n"
                + "\n".join(configured)
                + f"\n\n错误信息：\n{e}\n\n"
                "排查建议：\n"
                "· 401/403：API Key 填错或已失效；\n"
                "· 404：接口地址不对（中转站一般要到 `/v1` 为止，不要带 `/chat/completions`）；\n"
                "· 超时 / 无法连接：网络不通，或在墙外需要填「代理」；\n"
                "· 模型名不存在：确认模型名称与服务商匹配（如 `deepseek-chat`、`gpt-4o-mini`）。"
            )
        except Exception as e:  # noqa: BLE001
            elapsed = time.monotonic() - started
            logger.error(f"[dota2] 模型自检出现异常：{e}", exc_info=True)
            return (
                f"{head}\n"
                f"❌ 专用模型通道测试异常（耗时 {elapsed:.1f} 秒）\n"
                + "\n".join(configured)
                + f"\n\n异常信息：{e}"
            )

        elapsed = time.monotonic() - started
        return (
            f"{head}\n"
            f"✅ 专用模型通道可用（耗时 {elapsed:.1f} 秒）\n"
            + "\n".join(configured)
            + f"\n\n模型回显：{reply}"
        )

    async def _llm_selftest_fallback(self, umo: str, prefix: str) -> str:
        """在未配置专用 Key 时，实测一次 AstrBot 提供商通道。"""
        provider = await resolve_provider(
            self.context, umo, str(self.cfg("llm_provider_id", "") or "")
        )
        if provider is None:
            return (
                prefix
                + "❌ 但当前也没有可用的 AstrBot 模型提供商，`/d2 分析` 会拿不到报告。\n"
                "请先在 AstrBot 里配置一个模型提供商，或填上本插件的专用 API Key。"
            )
        started = time.monotonic()
        try:
            reply = await call_llm(
                provider,
                "你是一个连通性测试助手，只回答用户要求的内容，不要添加任何多余的话。",
                "请只回复两个字：可用",
            )
        except Exception as e:  # noqa: BLE001
            elapsed = time.monotonic() - started
            return (
                prefix
                + f"❌ 用 AstrBot 提供商测试失败（耗时 {elapsed:.1f} 秒）：{e}\n"
                "建议检查 AstrBot 里该提供商的配置，或改用插件专用 API Key。"
            )
        elapsed = time.monotonic() - started
        return (
            prefix
            + f"✅ AstrBot 提供商通道可用（耗时 {elapsed:.1f} 秒）\n"
            + f"　模型回显：{reply}"
        )

    # ==================================================================
    # 指令：数据源自检
    # ==================================================================
    @d2.command("datasource", alias={"数据源", "数据源测试", "源测试", "源"})
    @take_over_event
    async def d2_datasource(self, event: AstrMessageEvent):
        """检查主/后备数据源的连通性与降级状态"""
        yield event.plain_result("⏳ 正在测试数据源，请稍候…")
        yield event.plain_result(await self._data_source_selftest())

    async def _data_source_selftest(self) -> str:
        """实测主/后备数据源连通性，并回显降级状态。

        与「模型自检」对称：把「当前谁是主源、主源能不能用、有没有降级过」
        一次性讲清楚，省得用户翻日志。
        """
        api = self.api
        primary = getattr(api, "primary", None)
        secondary = getattr(api, "secondary", None)
        primary_label = getattr(api, "primary_label", "主数据源")
        secondary_label = getattr(api, "secondary_label", "后备数据源")

        lines = [f"📡 数据源状态（当前优先级：{primary_label} → {secondary_label}）", ""]

        # ---- 主数据源 ----
        if primary is None:
            lines.append(f"· {primary_label}：未接入")
        else:
            lines.append(f"· {primary_label}：{await self._probe_source(primary)}")

        # ---- 后备数据源 ----
        if secondary is None:
            lines.append(f"· {secondary_label}：未接入")
        else:
            lines.append(f"· {secondary_label}：{await self._probe_source(secondary)}")

        # ---- 降级记录 ----
        lines.append("")
        if getattr(api, "degraded", False):
            lines.append(
                "⚠️ 本进程内出现过降级：主数据源调用失败，已自动改走后备。\n"
                f"　最近原因：{getattr(api, 'last_error', '') or '未知'}"
            )
        else:
            lines.append("✅ 本次运行期间尚未发生降级。")

        # ---- STRATZ 专属提示 ----
        if primary_label == "STRATZ" and not getattr(primary, "configured", False):
            lines.append(
                "\nℹ️ 未配置「STRATZ API Key」，STRATZ 通道被跳过，"
                "所有查询直接使用 OpenDota。\n"
                "　去 https://stratz.com/api 登录后可生成令牌，填进插件配置即可启用。"
            )
        return "\n".join(lines)

    async def _probe_source(self, source: Any) -> str:
        """探测单个数据源的可用性，返回一行带耗时的描述。"""
        label = getattr(source, "label", None) or getattr(source, "name", "数据源")
        configured = getattr(source, "configured", None)
        if configured is False:
            return "未配置（跳过）"

        started = time.monotonic()
        try:
            heroes = await source.get_heroes()
        except OpenDotaError as e:
            elapsed = time.monotonic() - started
            return f"❌ 不可用（耗时 {elapsed:.1f} 秒）：{e}"
        except Exception as e:  # noqa: BLE001
            elapsed = time.monotonic() - started
            logger.error(f"[dota2] {label} 自检异常：{e}", exc_info=True)
            return f"❌ 异常（耗时 {elapsed:.1f} 秒）：{e}"

        elapsed = time.monotonic() - started
        count = len(heroes) if heroes else 0
        if count == 0:
            return f"⚠️ 连接成功但英雄表为空（耗时 {elapsed:.1f} 秒）"
        return f"✅ 可用（英雄 {count} 个，耗时 {elapsed:.1f} 秒）"

    # ==================================================================
    # 指令：定时任务（AstrBot 的「未来任务」）
    # ==================================================================
    # 时间型的任务建在 AstrBot 自己的任务库里（见 :mod:`dota_cron`）：好处是
    # 它们会出现在 AstrBot 的「未来任务」页面上，可停用 / 改时间 / 删除，
    # 而到点执行的仍是**插件自己的 handler** —— 数据源降级、中文英雄名、
    # 英雄池版本口径、模型通道全是插件这一套，不经过 AstrBot 的主智能体。
    #
    # 计数型的任务（「每监听到 N 场」）cron 表达不了（它只能表达时间），
    # 硬凑会把「攒够十场就发」拖成「第二天早上才发」。所以它们存在插件自己的
    # 存储里，由监听推送链路计数触发。两类任务在 ``/d2 定时`` 里是同一个列表、
    # 同一套管理动作（删 / 停 / 开 / 现在）。

    @d2.command("schedule", alias={"定时", "定时任务", "任务", "计划"})
    @take_over_event
    async def d2_schedule(self, event: AstrMessageEvent, args: GreedyStr):
        """定时任务：/d2 定时 [列表 | 删 <编号> | 停 <编号> | 开 <编号> | 现在 <编号>]

        也可以直接说人话让它建：`/d2 定时 每天早上七点通报群里战绩情况`。
        """
        umo = str(event.unified_msg_origin)
        uid = str(event.get_sender_id())
        raw = str(args).strip()
        verb, rest = dota_schedule.parse_control(raw)

        if verb == "list":
            yield event.plain_result(await self._schedule_list_text(umo))
            return
        if verb == "clear":
            yield event.plain_result(await self._schedule_clear(umo))
            return
        if verb == "update":
            yield event.plain_result(await self._schedule_update(rest, umo))
            return
        if verb in {"delete", "disable", "enable", "run"}:
            yield event.plain_result(await self._schedule_control(verb, rest, umo))
            return

        # ---- create：把整句人话解析成一个计划，先让用户核对 ----
        if not self.cfg("schedule_enabled", True):
            yield event.plain_result(
                "定时任务功能已在插件配置里关闭（`schedule_enabled`）。"
            )
            return
        if not raw:
            yield event.plain_result(await self._schedule_list_text(umo))
            return

        request = self._parse_task_request(raw, umo)
        if request is None:
            yield event.plain_result(self._schedule_hint())
            return
        # 计数型任务（「每监听到 N 场」）不走平台的调度器 —— cron 表达不了
        # 「每 N 场」，而且它是靠监听推送链路计数的。所以平台没定时能力时，
        # 只挡「按时间」的那一类，别把这种也一起拒了。
        if request.kind != "watch_count":
            reason = self.schedules.unavailable_reason()
            if reason:
                # 也不能偷偷换成插件自己的定时器：用户要的就是「能在 AstrBot
                # 的任务页面里看到并管理」，换一套实现只会让人找不到任务。
                yield event.plain_result(
                    f"⚠️ {reason}\n"
                    "（如果你要的是「每监听到 N 场就总结」，那个不依赖平台调度器，"
                    f"直接说「{self._nlu_keyword()} 每监听到十盘战绩就生成一份总结」即可）"
                )
                return
        self._schedule_plans[(umo, uid)] = {
            "request": request,
            "umo": umo,
            "expire": time.time() + NLU_CONFIRM_TTL,
        }
        yield event.plain_result(
            dota_schedule.format_plan(
                request,
                session_text=dota_schedule.session_label(umo),
                keyword=self._nlu_keyword(),
            )
        )

    def _parse_task_request(
        self, text: str, umo: str
    ) -> dota_schedule.TaskRequest | None:
        """把用户原话解析成任务请求（**认人**需要本会话的绑定 / 监听名单）。"""
        return dota_schedule.parse_request(
            text,
            known_names=self._nlu_known_names(umo),
            default_time=self._schedule_default_time(),
            # 「过一会儿再看看**这场**」里的指代：会话里最近提到过的那一场。
            # 不给它，用户就得手打比赛 ID —— 而他刚看完的正是「这场」。
            recent_matches=self._nlu_recent_match_rows(umo, limit=8),
        )

    def _schedule_default_time(self) -> str:
        return str(
            self.cfg("schedule_default_time", dota_schedule.DEFAULT_TIME)
            or dota_schedule.DEFAULT_TIME
        )

    def _schedule_timezone(self) -> str:
        """定时任务的时区：插件配置优先，留空跟随 AstrBot 全局配置。

        必须显式取一次，不能让平台自己回落到「进程所在时区」——
        用户说的「早上七点」是他所在时区的七点，而服务器可能在别的时区；
        时区错了任务会整体偏移几小时，且**很难被发现**（任务确实在跑）。
        """
        text = str(self.cfg("schedule_timezone", "") or "").strip()
        if text:
            return text
        try:
            conf = self.context.get_config()
            return str((conf.get("timezone") if conf else "") or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _schedule_hint(self) -> str:
        """没解析出任务时给的说法示例。"""
        keyword = self._nlu_keyword()
        return (
            "没听懂这个定时任务要做什么。可以这样说：\n"
            f"　`{keyword} 每天早上七点，通报群里战绩情况`\n"
            f"　`{keyword} 每晚十点总结一下大家今天的表现`\n"
            f"　`{keyword} 每监听到十盘战绩就生成一份这十盘的总结`\n"
            f"　`{keyword} 明天早上八点通报一下战绩`\n"
            "\n"
            "只有跟 Dota2 数据有关、且**带时间或场次**的说法才会被接住"
            "（「每天七点提醒我喝水」这类纯提醒插件做不了）。\n"
            f"查看已有任务：`{keyword} 定时 列表`"
        )

    # ------------------------------------------------------------------
    # 列表 / 管理
    # ------------------------------------------------------------------
    async def _schedule_list_text(self, umo: str) -> str:
        rows = await self._schedule_cron_rows(umo)
        counts = self._schedule_count_rows(umo)
        text = dota_schedule.format_task_list(
            rows, counts, keyword=self._nlu_keyword()
        )
        reason = self.schedules.unavailable_reason()
        if reason:
            text += f"\n\n⚠️ {reason}"
        return text

    async def _schedule_cron_rows(self, umo: str) -> list[dict]:
        """本会话的时间型任务（存在 AstrBot 的任务库里）。"""
        timezone = self._schedule_timezone()
        rows: list[dict] = []
        for job in await self.schedules.list_owned():
            row = dota_cron.CronBridge.job_summary(job)
            session = str(row.get("session") or "")
            # 任务说明里没有会话的（理论不该有）对所有会话可见，
            # 免得建出来的任务在哪个群都看不到、还以为没建成。
            if session and session != umo:
                continue
            action = str(row.get("action") or "")
            label = dota_schedule.ACTION_LABELS.get(action, action)
            if action == dota_schedule.ACTION_PLAYER and row.get("args"):
                label = f"{label}（{row['args']}）"
            row["action_label"] = label
            row["next_run_text"] = self.schedules.next_run_text(job, timezone)
            rows.append(row)
        return rows

    def _schedule_count_rows(self, umo: str) -> list[dict]:
        """本会话的计数型任务（存在插件自己的存储里，见 dota_store）。"""
        return [dict(row) for row in self.store.list_count_tasks(umo)]

    async def _schedule_items(self, umo: str) -> list[tuple[str, dict]]:
        """本会话的任务清单，**顺序与 ``/d2 定时 列表`` 完全一致**。

        编号就是列表里的序号，所以两边的拼装顺序必须一样：先时间型、
        再计数型。各拼各的一定会出现「用户看着列表说删 3，删掉的是另一个」。
        """
        items: list[tuple[str, dict]] = []
        for row in await self._schedule_cron_rows(umo):
            items.append(("cron", row))
        for row in self._schedule_count_rows(umo):
            items.append(("count", row))
        return items

    @staticmethod
    def _schedule_lookup(items: list[tuple[str, dict]], token: str) -> int | None:
        """把「3」或「a1b2c3」解析成清单里的编号（从 1 开始）。

        会先剥掉尖括号 / 方括号 / 引号之类的包围符：文档与提示语里写的是
        `删 <编号>`，用户把占位符连括号一起打进来是常态（线上日志实证：
        `dota2助手 定时 删 <1>`），不该因此回一句「要操作哪一个」。
        """
        text = str(token or "").strip().strip("<>《》【】[]{}()（）\"'`“”‘’ \t")
        if not text:
            return None
        if text.isdigit():
            value = int(text)
            return value if 1 <= value <= len(items) else None
        for pos, (_kind, item) in enumerate(items, start=1):
            sid = dota_schedule.short_id(
                str(item.get("schedule_id") or item.get("id") or "")
            )
            if sid and (sid == text or sid.startswith(text)):
                return pos
        return None

    async def _schedule_control(self, verb: str, rest: str, umo: str) -> str:
        """处理「删 / 停 / 开 / 现在 <编号>」。

        ``rest`` 为 :data:`dota_schedule.ALL_TOKEN` 时对**本会话的全部**任务生效
        （「取消所有定时任务」「把任务都删了」）。
        """
        items = await self._schedule_items(umo)
        if not items:
            return "本会话还没有定时任务。\n\n" + self._schedule_hint()
        if rest == dota_schedule.ALL_TOKEN:
            return await self._schedule_control_all(verb, items, umo)
        index = self._schedule_lookup(items, rest)
        if index is None:
            return (
                f"要操作哪一个？请给列表里的编号（1~{len(items)}）"
                f"或任务编号，例如「定时 删 2」。\n"
                f"先看一眼：`{self._nlu_keyword()} 定时 列表`"
            )
        kind, row = items[index - 1]
        ok, text = await self._apply_control(verb, kind, row, umo)
        return text

    async def _schedule_control_all(
        self, verb: str, items: list[tuple[str, dict]], umo: str
    ) -> str:
        """对全部任务执行同一个动作，并把结果逐条列出来。

        只动**本会话**的任务：列表与编号本来就是本会话口径，从 A 群一条
        指令删掉 B 群的任务既不直观也很危险（那边的人看着任务凭空消失）。
        其他会话还有任务时明确提示一句，免得用户以为「所有」是字面意思。
        """
        acted: list[str] = []
        failed: list[str] = []
        for kind, row in items:
            ok, text = await self._apply_control(verb, kind, row, umo)
            if ok:
                acted.append(text)
            else:
                failed.append(text)
        if verb == "delete":
            head = f"🗑 本会话的 {len(acted)} 个定时任务都删掉了。"
        elif verb == "disable":
            head = f"⏸ 停用了 {len(acted)} 个定时任务。"
        elif verb == "enable":
            head = f"▶️ 启用了 {len(acted)} 个定时任务。"
        else:
            head = f"▶️ 触发了 {len(acted)} 个定时任务。"
        parts = [head]
        parts.extend(acted)
        if failed:
            parts.extend(failed)
        others = await self._schedule_other_session_count(umo)
        if others:
            parts.append(
                f"（另有 {others} 个任务属于别的会话，请在对应会话里管理）"
            )
        logger.info(
            f"[dota2] 定时任务批量操作 {verb}：成功 {len(acted)} 个，失败 {len(failed)} 个"
        )
        return "\n".join(parts)

    async def _schedule_other_session_count(self, umo: str) -> int:
        """本插件在**其他会话**里的定时任务数（只用于提示，不做任何修改）。"""
        count = 0
        try:
            for job in await self.schedules.list_owned():
                payload = getattr(job, "payload", None)
                payload = payload if isinstance(payload, dict) else {}
                session = str(payload.get("session") or "")
                if session and session != umo:
                    count += 1
        except Exception as e:  # noqa: BLE001 - 只是提示，失败不该影响结果
            logger.debug(f"[dota2] 统计其他会话的定时任务失败（可忽略）：{e}")
        for row in self.store.list_count_tasks():
            if str(row.get("umo") or "") not in ("", umo):
                count += 1
        return count

    async def _apply_control(
        self, verb: str, kind: str, row: dict, umo: str
    ) -> tuple[bool, str]:
        """执行一个动作，返回 ``(是否成功, 文案)``。"""
        if kind == "cron":
            return await self._schedule_control_cron(verb, row)
        return await self._schedule_control_count(verb, row, umo)

    async def _schedule_clear(self, umo: str) -> str:
        """清空本会话的全部定时任务。"""
        items = await self._schedule_items(umo)
        if not items:
            return "本会话本来就没有定时任务。"
        return await self._schedule_control_all("delete", items, umo)

    async def _schedule_update(self, rest: str, umo: str) -> str:
        """给已有任务改执行时间。

        两种说法都认：带编号的（``改 2 到早上八点``）和整句人话的
        （``把这个定时任务的执行时间改到早上七点``）—— 后者没说是哪一个，
        只有**本会话仅有一个任务**时才敢猜。
        """
        items = await self._schedule_items(umo)
        if not items:
            return "本会话还没有定时任务，没什么可改的。\n\n" + self._schedule_hint()

        token, text = dota_schedule.split_update_target(rest)
        index = self._schedule_lookup(items, token) if token else None
        if index is None:
            if token:
                return (
                    f"列表里没有编号 {token} 的任务（当前 1~{len(items)}）。\n"
                    f"先看一眼：`{self._nlu_keyword()} 定时 列表`"
                )
            if len(items) > 1:
                return (
                    f"要改哪一个？请带上编号，例如「{self._nlu_keyword()} 定时 改 2 早上八点」。\n"
                    f"先看一眼：`{self._nlu_keyword()} 定时 列表`"
                )
            index = 1
        kind, row = items[index - 1]
        if kind != "cron":
            every = max(1, int(row.get("every") or dota_schedule.DEFAULT_EVERY))
            return (
                f"「每 {every} 场总结」是按场次触发的，没有执行时间可改。"
                f"想改触发场次就删掉重建（`{self._nlu_keyword()} 定时 删 {index}`）。"
            )

        # 先按完整时间表达解（「改成每天晚上十点」连频率一起改）；
        # 解不出就只取钟点，把钟点换进原来的 cron（「改到早上七点」——
        # 频率沿用原来的，不能变成每天都响）。
        spec = dota_schedule.parse_time_spec(
            text, default_time=self._schedule_default_time()
        )
        if spec is not None and spec.cron:
            new_cron = str(spec.cron)
        else:
            clock = dota_schedule.parse_clock(text)
            if clock is None:
                return (
                    "没看出你想改成几点。这样说："
                    f"`{self._nlu_keyword()} 定时 改 {index} 早上八点`，"
                    "或直接说「把这个定时任务改到每天早上七点」。"
                )
            new_cron = dota_schedule.retime_cron(
                str(row.get("cron") or ""), clock.hour, clock.minute
            )

        job_id = str(row.get("job_id") or "")
        action = str(row.get("action") or "")
        session_text = dota_schedule.session_label(umo)
        # 任务名里带时刻，必须一起改，否则列表/WebUI 上显示的仍是旧时刻
        ok = await self.schedules.set_cron(
            job_id,
            new_cron,
            name=dota_schedule.job_name(action, new_cron),
            description=dota_schedule.job_note(
                action, new_cron, session_label=session_text
            ),
        )
        if not ok:
            return "⚠️ 改时间失败，原因见日志。"
        logger.info(
            f"[dota2] 定时任务 {row.get('schedule_id') or job_id} 执行时间改为 {new_cron}"
        )
        return (
            f"✅ 改好了：{dota_schedule.cron_label(new_cron)}\n"
            f"· 下次执行：{self.schedules.next_run_text_by_id(job_id, self._schedule_timezone())}"
        )

    async def _schedule_control_cron(self, verb: str, row: dict) -> tuple[bool, str]:
        job_id = str(row.get("job_id") or "")
        name = str(row.get("name") or "定时任务")
        if verb == "delete":
            ok = await self.schedules.delete(job_id)
            return (ok, f"🗑 已删除：{name}" if ok else "⚠️ 删除失败，原因见日志。")
        if verb == "disable":
            ok = await self.schedules.set_enabled(job_id, False)
            return (
                (ok, f"⏸ 已停用：{name}")
                if ok
                else (False, f"⚠️ 停用失败：{name}，原因见日志。")
            )
        if verb == "enable":
            ok = await self.schedules.set_enabled(job_id, True)
            return (ok, f"▶️ 已启用：{name}" if ok else f"⚠️ 启用失败：{name}，原因见日志。")
        ok = await self.schedules.run_now(job_id)
        return (
            (True, f"▶️ 已立即执行一次：{name}")
            if ok
            else (False, f"⚠️ 执行失败：{name}，原因见日志。")
        )

    async def _schedule_control_count(
        self, verb: str, row: dict, umo: str
    ) -> tuple[bool, str]:
        task_id = str(row.get("id") or "")
        every = max(1, int(row.get("every") or dota_schedule.DEFAULT_EVERY))
        label = f"每 {every} 场总结"
        if verb == "delete":
            ok = await self.store.remove_count_task(task_id)
            return (ok, f"🗑 已删除：{label}" if ok else f"⚠️ 删除失败：{label}")
        if verb == "disable":
            ok = await self.store.set_count_task_enabled(task_id, False)
            return (ok, f"⏸ 已停用：{label}" if ok else f"⚠️ 停用失败：{label}")
        if verb == "enable":
            ok = await self.store.set_count_task_enabled(task_id, True)
            return (ok, f"▶️ 已启用：{label}" if ok else f"⚠️ 启用失败：{label}")
        # 「现在」：用已经攒下的场次先出一份，**不清空窗口**
        # （它是预览，不该把用户攒的场次吃掉）
        seen = len(list(row.get("seen") or []))
        if seen < 1:
            return (False, f"「{label}」还没攒到任何场次，现在没东西可总结。")
        self._spawn_summary_task(task_id, umo, every, force=True)
        return (True, f"▶️ 正在用已攒的 {seen} 场生成总结，稍后发到本会话。")

    # ------------------------------------------------------------------
    # 确认与创建
    # ------------------------------------------------------------------
    def _nlu_schedule_plan_ack(
        self, umo: str, uid: str, head: str
    ) -> tuple[str, dict[str, Any]] | None:
        """检查有没有等待确认的定时任务计划。

        Returns:
            ``("confirm", 计划)`` / ``("cancel", {})``；没有待确认项、或回复的
            不是确认 / 取消时返回 ``None``。**取出即销毁**，避免同一条
            「确认」被用两次。
        """
        plan = self._schedule_plans.get((umo, uid))
        if plan is None:
            return None
        if float(plan.get("expire") or 0) < time.time():
            self._schedule_plans.pop((umo, uid), None)
            return None
        if head in NLU_CONFIRM_WORDS:
            self._schedule_plans.pop((umo, uid), None)
            return ("confirm", plan)
        if head in NLU_CANCEL_WORDS:
            self._schedule_plans.pop((umo, uid), None)
            return ("cancel", {})
        return None

    async def _schedule_apply(
        self, plan: dict[str, Any], event: AstrMessageEvent, umo: str
    ):
        """用户确认之后真正建任务（两种类型各走一条路）。"""
        request: dota_schedule.TaskRequest = plan["request"]
        uid = str(event.get_sender_id())
        try:
            sender = str(event.get_sender_name() or "")
        except Exception:  # noqa: BLE001 - 少数适配器没有这个方法
            sender = ""
        session_text = dota_schedule.session_label(umo)
        keyword = self._nlu_keyword()

        if request.kind == "watch_count":
            record, created = await self.store.add_count_task(
                umo,
                int(request.every),
                action=str(request.action or dota_schedule.ACTION_WATCH_SUMMARY),
                task_id=dota_schedule.new_schedule_id("d2c"),
                created_by=uid,
                created_by_name=sender,
            )
            if not created:
                sid = dota_schedule.short_id(str(record.get("id") or ""))
                yield event.plain_result(
                    f"ℹ️ 本会话已经有一个「每 {record.get('every')} 场总结」的任务了"
                    f"（编号 {sid}），没有重复添加。"
                )
                return
            yield event.plain_result(
                f"✅ 建好了：每监听到 {record.get('every')} 场比赛，"
                f"就为{session_text}生成一份这 {record.get('every')} 场的总结。\n"
                f"· 编号：{dota_schedule.short_id(str(record.get('id') or ''))}\n"
                "· 从现在开始计数（只算**推送成功**的新比赛）\n"
                "· 这种任务按场次触发，不会出现在 AstrBot 的「未来任务」里\n"
                f"· 查看 / 删除：`{keyword} 定时 列表`"
            )
            return

        spec = request.time
        cron = str(spec.cron or "") if spec is not None else ""
        if not cron:
            yield event.plain_result("⚠️ 没能把时间换算成执行计划，换个说法再试一次。")
            return
        schedule_id = dota_schedule.new_schedule_id("d2")
        payload = {
            "session": str(umo),
            "action": str(request.action),
            "args": str(request.args or ""),
            # 到点要**回放的原话**（v2.5.0 起）：定时任务真正执行的是这句话
            # —— 到点交给带工具的模型按当时的数据办。`action` / `args` 留着
            # 是给「升级前建好的老任务」兜底（它们没有这个键，走固定动作分支）。
            "question": request.replay_text,
            "schedule_id": schedule_id,
            "once": bool(spec.once),
            "note": dota_schedule.task_note(request, session_label=session_text),
            # 一次性任务的 cron 是「每天 H:M」（相对时间不到一天）或「只钉几号」
            # （跨年）这类**绕法**，反渲染会读成「每天 / 每月」，跟用户说的
            # 「两小时后」对不上。把计划里那个绝对时间一并带上，列表才显示得对
            # （见 dota_schedule.format_task_list）。
            "once_at": (
                spec.run_at.strftime("%m-%d %H:%M")
                if (spec is not None and spec.once and spec.run_at)
                else ""
            ),
        }
        try:
            job = await self.schedules.create(
                payload,
                name=dota_schedule.task_name(request, session_label=session_text),
                cron_expression=cron,
                handler=self._run_scheduled_task,
                timezone=self._schedule_timezone(),
            )
        except dota_cron.CronUnavailable as e:
            yield event.plain_result(f"⚠️ {e}")
            return
        except Exception as e:  # noqa: BLE001
            logger.error(f"[dota2] 创建定时任务失败：{e}", exc_info=True)
            yield event.plain_result(f"⚠️ 创建定时任务失败：{e}")
            return

        # 到点执行的**就是这句话本身** —— 回执里把它摆出来，比归纳成
        # 「通报群里战绩情况」更不容易让人误解（也才说明得了「随便说一句
        # 都能定时」这件事）。没原话时（理论上不会有）退回动作名。
        label = request.describe()
        # 一次性任务的 cron 有两种绕法（见 dota_schedule._build_delta_once）：
        # 相对时间不到一天时写成「每天 H:M」、跨年时只钉「几号」。这两者用
        # cron_label 反渲染会读成「每天」/「每月」，**跟用户说的「两小时后」
        # 对不上**（回执里同时出现「每天 16:51」和「一次性任务」自相矛盾）。
        # 所以一次性任务直接显示计划里那个绝对时间。
        when_text = (
            spec.label
            if (spec is not None and spec.once)
            else dota_schedule.cron_label(cron)
        )
        lines = [
            "✅ 定时任务已建好",
            f"· 时间：{when_text}",
            f"· 内容：{label}",
            f"· 发到：{session_text}",
            f"· 编号：{dota_schedule.short_id(schedule_id)}",
            f"· 下次执行：{self.schedules.next_run_text(job, self._schedule_timezone())}",
            "",
        ]
        if spec is not None and spec.once:
            lines.append("这是一次性任务，执行完就自动结束。")
        lines.append("到点我会把上面这句话交给模型，按**那一刻**的真实数据执行。")
        lines.append(
            "它也会出现在 AstrBot 的「未来任务」页面里（可以在那边改时间或停用）。"
        )
        lines.append(f"在这里管理：`{keyword} 定时 列表`")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 到点执行
    # ------------------------------------------------------------------
    async def _run_scheduled_task(
        self,
        *,
        owner: str = "",
        session: str = "",
        action: str = "",
        args: str = "",
        schedule_id: str = "",
        once: bool = False,
        note: str = "",
        question: str = "",
        **_extra: Any,
    ) -> None:
        """定时任务到点执行（AstrBot ``basic`` 任务的回调）。

        这是**唯一**由平台调度器直接调用的入口，因此必须把「插件已卸载」
        「没有目标会话」「数据 / 模型不可用」这几种情况全部兜住：抛异常会被
        ``CronJobManager`` 记成任务失败，而这些问题跟任务本身没关系，
        却会在「未来任务」页面里显示成红色错误。
        """
        if self._sched_stopped:
            # 插件已经卸载（热重载时旧实例会收到这一轮唤醒）：直接退出，
            # 否则会往一个已经不属于它的会话推消息。
            logger.info(f"[dota2] 定时任务 {schedule_id} 在插件停止后被唤醒，跳过")
            return
        umo = str(session or "")
        if not umo:
            logger.warning(f"[dota2] 定时任务 {schedule_id} 没有目标会话，跳过")
            return
        logger.info(
            f"[dota2] 定时任务 {schedule_id} 到点执行："
            f"{action} {args!r}"
            f"{' 原句=' + repr(str(question)[:32]) if question else ''} -> {umo}"
        )
        try:
            await self._dispatch_scheduled(action, args, umo, question)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[dota2] 定时任务 {schedule_id} 执行失败：{e}", exc_info=True)
        finally:
            if once:
                # 一次性任务的 cron 是「钉死日期」写法（见 dota_schedule.once_cron），
                # 不删掉的话明年同一天还会响一次。执行完自己收尾。
                await self._delete_schedule(schedule_id)

    async def _scheduled_replay(
        self, question: str, umo: str, frozen: str = ""
    ) -> None:
        """定时任务到点执行：把用户当初那句话**重新交给模型**办。

        这是「定时 = 到点把原话再发一次」的落地。与手动提问的三点不同：

        1. **没有 event**（到点是平台调度器叫醒的），所以会话与用户标识要
           显式传进去（见 :meth:`_nlu_chat_tool_ctx` 的 ``umo`` 参数）；
        2. **写操作被摘掉**（``allow_write=False``）：那一刻没有任何人守着
           回「确认」，留着写操作只会让模型在群里报一句「已登记待确认」的
           假回执；
        3. **原话里的时间词已经过期**：「两小时后重新查看一下这场」到点时
           「两小时后」已经过去了。必须把这一点说清楚，否则模型会理解成
           「从现在起再过两小时」。

        Args:
            question: 用户当初的原话（建任务时随负载存下来的）。
            umo: 目标会话。
            frozen: 建任务时解析出的对象（比赛 ID / 玩家名），用来兜住
                「这场」这类**指代**——到点时本会话最近的比赛可能已经换了
                一批，靠上下文重新猜会指错。
        """
        text = str(question or "").strip()
        if not text:
            return
        if not self.cfg("enable_llm_analysis", True):
            await self._send_quiet(
                umo,
                "⚠️ 定时任务没能执行：插件配置里关掉了 LLM 分析"
                "（`enable_llm_analysis`），到点没有可用的模型通道。",
            )
            return
        try:
            context = await self._collect_chat_context(
                text, umo, "", self._binding_for_umo(umo), local_only=True
            )
        except Exception as e:  # noqa: BLE001 - 收集失败也要出声，不能静默
            logger.error(f"[dota2] 定时任务收集上下文失败：{e}", exc_info=True)
            context = None
        if context is None:
            await self._send_quiet(
                umo, "⚠️ 定时任务没能执行（收集会话上下文失败），本次跳过。"
            )
            return

        hint = ""
        frozen = str(frozen or "").strip()
        if frozen:
            if frozen.isdigit() and 6 <= len(frozen) <= 20:
                hint = f"\n（原话里「这场」这类指代，指的就是比赛 {frozen}。）"
            else:
                hint = f"\n（这句话针对的对象是「{frozen}」。）"
        ask = (
            "【这是定时任务到点执行的正文】\n"
            f"用户当初说的原话是：「{text}」{hint}\n"
            "原话里的时间（例如「两小时后」）**指的就是现在**，那已经是过去时了 "
            "—— 不要理解成「从现在起再过那么久」。\n"
            "请按**现在**的真实数据把这件事办完，直接把结论发到群里；"
            "不要复述这句话本身，也不要说「我这就去」这类空话。"
        )
        prompt = dota_chat.build_chat_prompt(ask, context, tooled=True)
        system_prompt = dota_chat.build_chat_system_prompt(
            str(self.cfg("nlu_chat_system_prompt", "") or "")
        )
        reply, trace, pending = await self._nlu_chat_agent(
            None, prompt, system_prompt, umo=umo, allow_write=False
        )
        if pending:
            # 定时通道的工具清单里没有写操作，正常不会产生待确认项。
            # 真出现了说明契约被破坏（有人把写操作放回了定时清单），
            # 记一笔便于发现；**不往群里发**——那一刻没人会回「确认」。
            logger.warning(
                f"[dota2] 定时任务意外产生 {len(pending)} 项待确认，已忽略"
            )
        if not reply:
            # 工具通道没产出 → **回落单轮播报**（预取数据后直接问一次模型）。
            # 这是定时任务最早、最朴素的形态，也是「默认模型不支持 function
            # calling 且没配专用 Key」时**唯一**能走通的路。定时任务不能因为
            # 工具用不了就整条失效 —— 那是纯粹的倒退，用户到点什么都收不到。
            logger.info("[dota2] 定时任务：工具通道没产出，回落单轮播报")
            reply = await self._scheduled_report(umo, text)
        if not reply:
            # 静默失败是最糟的：用户以为任务建好了，到点却什么都没发。
            logger.warning(f"[dota2] 定时任务没拿到内容，跳过本次（{umo}）")
            await self._send_quiet(
                umo,
                "⚠️ 定时任务没能生成内容（大模型不可用或数据源异常），本次跳过。\n"
                "可以先用 `/d2 模型测试` 与 `/d2 数据源` 自检一下。",
            )
            return
        calls = len([x for x in trace.split("; ") if x]) if trace else 0
        logger.info(
            f"[dota2] 定时任务到点执行（原句回放）：{text[:32]!r} 工具调用={calls}"
        )
        await self._send_to_session(umo, reply)

    async def _dispatch_scheduled(
        self, action: str, args: str, umo: str, question: str = ""
    ) -> None:
        """按动作分派。

        **有 ``question`` 就走原句回放**（v2.5.0 起的新口径）：到点把用户
        原话重新交给带工具的模型，按那一刻的真实数据执行 —— 于是用户的
        **任何一句话**都能定时，不必事先归入某个固定动作。

        ``question`` 为空的是**老任务**（建在 v2.5.0 之前，负载里没有这一
        项），继续走下面那套固定动作分支 —— 升级不该让已建好的任务变哑。
        """
        # ① **有专用渲染链路**的动作优先，与有没有原话无关：
        #    · 复查单场要「没解析好也出声」（下面那条播报通道遇到空内容会
        #      静默跳过，而用户建它时说的正是「没解析就告诉我」）；
        #    · 版本榜是**算出来的表格** —— 结构化、稳定、零模型调用，
        #      丢给模型重新生成只会更差，还白烧一次调用。
        if action == dota_schedule.ACTION_MATCH:
            await self._match_recheck(args, umo)
            return
        if action == dota_schedule.ACTION_META:
            try:
                parts = await self._hero_meta_board_parts()
            except Exception as e:  # noqa: BLE001
                logger.error(f"[dota2] 定时推送版本榜失败：{e}", exc_info=True)
                parts = None
            if parts is None:
                await self._send_quiet(
                    umo, "⚠️ 定时播报没能拿到版本英雄数据（数据源可能不可用），本次跳过。"
                )
                return
            await self._send_to_session(
                umo, f"{parts['board']}\n\n{parts['footer']}"
            )
            return

        # ② 其余一律走**原句回放**（v2.5.0 起）：到点把用户原话交给带工具的
        #    模型，按那一刻的真实数据执行 —— 于是**任何一句话**都能定时，
        #    不必事先归入某个固定动作。
        if question:
            await self._scheduled_replay(question, umo, frozen=args)
            return

        # ③ 老任务（v2.5.0 之前建的，负载里没有 question）退回预设问题，
        #    免得这次升级把已建好的任务变成哑的。
        built = dota_schedule.build_report_question(action, args)
        reply = await self._scheduled_report(umo, built)
        if not reply:
            # 静默失败是最糟的：用户以为任务建好了，其实每天早上什么都没发。
            # 一条短提示，让他知道该去看模型 / 数据源，而不是去查任务配置。
            logger.warning(f"[dota2] 定时播报没拿到内容，跳过本次（{umo}）")
            await self._send_quiet(
                umo,
                "⚠️ 定时播报没能生成内容（大模型不可用或数据源异常），本次跳过。\n"
                "可以先用 `/d2 模型测试` 与 `/d2 数据源` 自检一下。",
            )
            return
        await self._send_to_session(umo, reply)

    async def _match_recheck(self, args: str, umo: str) -> None:
        """「过一会儿再看一眼这场」到点执行：**复查解析状态**。

        这是唯一一种「结果取决于执行时数据状态」的定时任务，也是唯一一种
        **没解析好也必须发消息**的任务 —— 用户建它的时候说的就是「还没解析
        就通知我它没解析」。所以两条路都主动出声，不像别的播报那样静默跳过
        （静默跳过正是最容易让人以为「功能坏了」的那种失败）。
        """
        found = re.search(r"\d{6,20}", str(args or ""))
        match_id = int(found.group(0)) if found else 0
        if match_id <= 0:
            await self._send_quiet(
                umo, "⚠️ 定时复查没拿到比赛 ID（任务参数不对），本次跳过。"
            )
            return
        try:
            match = await self.api.get_match(match_id)
        except OpenDotaError as e:
            await self._send_quiet(umo, f"⚠️ 到点复查比赛 {match_id} 时取数失败：{e}")
            return
        if not match:
            await self._send_quiet(
                umo,
                f"⚠️ 到点复查了，但比赛 {match_id} 在 STRATZ 与 OpenDota 都查不到。\n"
                "可能比赛 ID 写错，或者这局还没被收录。",
            )
            return

        state = dota_parse.parse_state(match)
        if not state.parsed:
            await self._send_quiet(
                umo,
                f"📭 比赛 {match_id} 到现在还没解析好"
                f"（数据完整度：{state.describe()}）。\n"
                "AI 复盘要用逐分钟经济、团战与出装数据，解析没完成时给不出"
                "有意义的分析，所以这次先只告诉你进度。\n"
                f"· 现在就想要一份基础数据版的：`/d2 单场 {match_id} skip`\n"
                f"· 想再等等：`{self._nlu_keyword()} 一小时后重新看一下 {match_id}`",
            )
            return

        await self._send_quiet(
            umo, f"✅ 比赛 {match_id} 已经解析好了，这就出分析。"
        )
        heroes = await self._heroes()
        items = await self._items()
        focus_ids, focus_names = self._recheck_focus(umo, match)
        headline = self.match_headline(
            match, heroes, focus_ids or None, True, focus_names
        )
        extra_context = await self._build_recent_context(
            match_id, focus_ids, focus_names, heroes
        )
        abilities = await self._ability_constants()
        curve_ids = self._curve_targets(match, focus_ids)
        prompt = build_single_match_analysis_prompt(
            match=match,
            heroes=heroes,
            items=items,
            focus_account_ids=focus_ids or None,
            focus_names=focus_names or None,
            extra_context=extra_context,
            abilities=abilities,
            curve_ids=curve_ids,
        )
        report = await self._generate_report_for_umo(umo, prompt)
        if not report:
            await self._send_quiet(
                umo,
                "⚠️ 比赛已经解析好了，但复盘没生成出来（大模型不可用或未启用）。\n"
                f"可以先用 `/d2 模型测试` 自检，也可以手动看一次："
                f"`/d2 单场 {match_id} skip`",
            )
            return
        await self._send_quiet(umo, headline)
        for chunk in self._chunk_text(report):
            await self._send_quiet(umo, chunk)

    def _recheck_focus(self, umo: str, match: dict) -> tuple[list[int], dict[int, str]]:
        """复查单场时的焦点玩家：本会话**唯一**绑定的那位，且得在这局里。

        定时任务不知道是谁建的（建任务的人可能早退群了），所以只在「本会话
        只有一个绑定」时才认 —— 和定时播报的 ``_binding_for_umo`` 同一口径。
        没人可聚焦就出一份不带焦点的全场复盘，不影响其余内容。
        """
        binding = self._binding_for_umo(umo)
        if not binding:
            return [], {}
        candidate = int(binding.get("account_id") or 0)
        if not candidate:
            return [], {}
        in_match = any(
            isinstance(player, dict)
            and int(player.get("account_id") or 0) == candidate
            for player in (match.get("players") or [])
        )
        if not in_match:
            return [], {}
        return [candidate], {candidate: str(binding.get("personaname") or "")}

    async def _scheduled_report(self, umo: str, question: str) -> str | None:
        """定时播报：用与闲聊兜底**同一份**数据上下文生成一段播报。

        为什么不另写一套取数：那份上下文里已经带齐了三样「坐标系」
        —— 当前时间、本会话最近发生过的比赛、以及时间窗口。少任何一样，
        「通报群里战绩情况」都会答成一份泛泛而谈的东西。
        """
        if not self.cfg("enable_llm_analysis", True):
            return None
        try:
            context = await self._collect_chat_context(
                question, umo, "", self._binding_for_umo(umo), []
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"[dota2] 定时播报收集数据失败：{e}", exc_info=True)
            return None
        prompt = dota_chat.build_chat_prompt(question, context)
        system_prompt = dota_chat.build_chat_system_prompt(
            str(self.cfg("nlu_chat_system_prompt", "") or "")
        )
        return await self._call_report_llm(prompt, umo=umo, system_prompt=system_prompt)

    def _binding_for_umo(self, umo: str) -> dict | None:
        """没有 event 时取本会话可用的绑定（定时播报用）。

        只在「本会话只有一个绑定」时才认 —— 定时任务不知道是谁建的
        （建任务的人可能早就退群了），随便挑一个当成提问者会让播报
        围着某个人讲。多个绑定时返回 ``None``：宁可只讲监听名单。
        """
        bindings = self.store.list_bindings(umo)
        if len(bindings) == 1:
            return next(iter(bindings.values()))
        return None

    async def _delete_schedule(self, schedule_id: str) -> None:
        """按插件自己的 task_id 删除定时任务。

        不能用 job_id：AstrBot 重启 / 插件重载时 :meth:`dota_cron.CronBridge.adopt`
        会**重建**任务，job_id 随之改变，而 ``schedule_id`` 一直在负载里、不变。
        """
        if not schedule_id:
            return
        for job in await self.schedules.list_owned():
            payload = getattr(job, "payload", None)
            payload = payload if isinstance(payload, dict) else {}
            if str(payload.get("schedule_id") or "") == str(schedule_id):
                await self.schedules.delete(str(job.job_id))
                logger.info(f"[dota2] 一次性定时任务 {schedule_id} 已执行完并删除")
                return

    # ------------------------------------------------------------------
    # 启动接管
    # ------------------------------------------------------------------
    def _start_schedule_adopt(self) -> None:
        """启动时接管既有定时任务（幂等）。

        **必须在插件加载时就调用**，不能只挂在 ``on_astrbot_loaded`` 上：
        热重载（WebUI 的「重载插件」）走的是 ``plugin_manager.reload()``，
        它只重新跑一遍插件的 ``__init__``，**不会再触发** ``on_astrbot_loaded``。
        若只在启动钩子里接管，重载之后 ``CronJobManager._basic_handlers`` 里
        留着的仍是**上一个实例**的绑定方法，而那个实例已经 ``terminate``
        （``_sched_stopped=True``）—— 任务到点会静默什么都不做，用户完全
        看不出哪里坏了。
        """
        if self._sched_stopped or not self.cfg("schedule_enabled", True):
            return
        reason = self.schedules.unavailable_reason()
        if reason:
            # 用户可能正纳闷「为什么建不了定时任务」，把原因写进日志，
            # 排障时一眼就能看出是平台没给能力，而不是插件坏了。
            logger.info(f"[dota2] 定时任务不可用，已跳过：{reason}")
            return
        if self._sched_adopt_task is not None and not self._sched_adopt_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - 插件加载发生在事件循环里
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                return
        self._sched_adopt_task = loop.create_task(self._adopt_schedules())

    async def _adopt_schedules(self) -> None:
        try:
            rebuilt, failed = await self.schedules.adopt(self._run_scheduled_task)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[dota2] 接管定时任务失败：{e}", exc_info=True)
            return
        logger.info(
            f"[dota2] 定时任务已就绪（接入 AstrBot「未来任务」）；"
            f"接管既有任务：重建 {rebuilt} 个，失败 {failed} 个"
        )

    # ==================================================================
    # 「每 N 场总结」：计数窗口与后台生成
    # ==================================================================
    async def _push_count_tasks(
        self, umo: str, match_id: int, desc: str, start_time: Any
    ) -> None:
        """把这一场记进本会话所有「每 N 场总结」任务的窗口，攒够就发。

        调用点在 ``_deliver`` 的**送达之后**（标题已经进群）。没送达的场次
        绝不能计数 —— 否则用户会收到一份「含他根本没看到的比赛」的总结。
        """
        tasks = self.store.list_count_tasks(umo)
        if not tasks:
            return
        row = {
            "match_id": int(match_id),
            "desc": str(desc or ""),
            "time_text": self._match_time_text(start_time),
        }
        for task in tasks:
            task_id = str(task.get("id") or "")
            if not task_id:
                continue
            every = max(1, int(task.get("every") or dota_schedule.DEFAULT_EVERY))
            try:
                triggered = await self.store.push_count_task_seen(
                    task_id, row, every=every
                )
            except Exception as e:  # noqa: BLE001 - 计数失败不该影响推送
                logger.error(f"[dota2] 记录场次到计数任务 {task_id} 失败：{e}")
                continue
            if triggered:
                self._spawn_summary_task(task_id, umo, every)

    @staticmethod
    def _match_time_text(start_time: Any) -> str:
        try:
            return time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(int(start_time or 0))
            )
        except (TypeError, ValueError, OSError):
            return ""

    def _spawn_summary_task(
        self, task_id: str, umo: str, every: int, *, force: bool = False
    ) -> None:
        """把「生成这 N 场总结」扔进后台任务。

        **绝不能**在这条链路里 await 它：一次大模型调用要十几秒，而监听
        推送这一轮后面还有别的会话等着推。生成失败时窗口**不清空**
        （见 :meth:`dota_store.DotaStore.reset_count_task_window`），
        下一场再来时会带着原来的场次重试。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - 调用点一定在协程里
            return
        task = loop.create_task(self._run_count_summary(task_id, umo, every, force=force))
        self._summary_tasks.add(task)
        task.add_done_callback(self._summary_tasks.discard)

    async def _run_count_summary(
        self, task_id: str, umo: str, every: int, *, force: bool = False
    ) -> None:
        """生成并推送「这 N 场」的阶段总结。

        Args:
            force: 「定时 现在」触发的预览：允许用不足 ``every`` 的场次生成，
                且**不动窗口**（预览不该把用户攒的场次吃掉）。
        """
        if self._sched_stopped:
            return
        task = self.store.get_count_task(task_id)
        if task is None or not task.get("enabled", True):
            return
        rows = dota_schedule.window_rows(list(task.get("seen") or []), every)
        if not rows:
            return
        if not force and len(rows) < every:
            # 并发窗口：同一场被两个会话同时送达，或者窗口刚被清过
            return
        prompt = dota_schedule.build_watch_summary_prompt(rows, every=every)
        reply = await self._call_report_llm(
            prompt, umo=umo, system_prompt=dota_schedule.WATCH_SUMMARY_SYSTEM_PROMPT
        )
        if not reply:
            logger.warning(
                f"[dota2] 每 {every} 场总结没生成出来（模型不可用），"
                f"窗口保留，下一场再试（{umo}）"
            )
            return
        header = f"📊 本群最近 {len(rows)} 场小结"
        if not await self._send_to_session(umo, f"{header}\n\n{reply}"):
            logger.warning(
                f"[dota2] 每 {every} 场总结发送失败，窗口保留（{umo}）"
            )
            return
        if not force:
            # 只有「攒够触发」才清窗口；「现在」只是看一眼，不能吃掉进度
            await self.store.reset_count_task_window(task_id)
        logger.info(f"[dota2] 已推送每 {every} 场总结到 {umo}（{len(rows)} 场）")

    # ==================================================================
    # 大模型调用
    # ==================================================================
    # 两条通道，分工是这一版的核心约定：
    #
    #   * **闲聊通道**（自然语言入口 / 工具调用）→ 默认模型优先
    #     （:meth:`_call_chat_llm`、:meth:`_chat_tool_clients`）；
    #   * **分析通道**（复盘 / 分析 / 轮椅适配 / 赛后短评 / 定时播报正文）
    #     → 插件专用 API Key 优先（:meth:`_call_report_llm`）。
    #
    # 这么切是因为两者的成本结构完全不同：闲聊高频、单次短，用 AstrBot 里
    # 已有的全局模型就够；比赛分析要出长文、按量计费，值得用独立的 Key 和
    # 更强的模型，也便于单独盯着额度。
    def _llm_api_key(self) -> str:
        """读取插件专用 API Key（空字符串表示未配置）。"""
        return str(self.cfg("llm_api_key", "") or "").strip()

    def _dedicated_client(self) -> OpenAICompatibleClient | None:
        """按配置构造专用模型客户端；未配置 key 时返回 None。"""
        api_key = self._llm_api_key()
        if not api_key:
            return None
        return OpenAICompatibleClient(
            api_key=api_key,
            base_url=str(self.cfg("llm_base_url", "") or ""),
            model=str(self.cfg("llm_model", "") or ""),
            timeout=float(self.cfg("llm_timeout", 120) or 120),
            proxy=str(self.cfg("llm_proxy", "") or ""),
        )

    async def _call_chat_llm(
        self, prompt: str, *, umo: str = "", system_prompt: str = ""
    ) -> str | None:
        """生成**闲聊**回答的模型调用：默认模型优先，专用 Key 只作后备。

        与 :meth:`_call_report_llm` 的分工是本版改动的核心 ——
        「闲聊走默认模型、只有比赛分析走专用 Key」。默认模型整个拿不到时
        才用专用 Key 兜一下，否则闲聊也会去消耗分析用的额度。

        Returns:
            回答正文；两条通道都不可用时返回 None（并说明原因）。
        """
        if not self.cfg("enable_llm_analysis", True):
            return None

        provider = await resolve_provider(
            self.context,
            umo,
            str(self.cfg("llm_provider_id", "") or ""),
        )
        if provider is not None:
            try:
                return await call_llm(provider, system_prompt, prompt)
            except Exception as e:  # noqa: BLE001
                logger.error(f"[dota2] 闲聊走默认模型失败：{e}")

        client = self._dedicated_client()
        if client is None:
            if provider is None:
                logger.warning("[dota2] 闲聊没有可用的模型通道，跳过")
            return None
        try:
            logger.info("[dota2] 闲聊回退到插件专用模型")
            max_tokens = int(self.cfg("llm_max_tokens", 0) or 0)
            return await client.chat(
                system_prompt,
                prompt,
                temperature=float(self.cfg("llm_temperature", 0.7) or 0),
                max_tokens=max_tokens or None,
            )
        except LLMRequestError as e:
            logger.error(f"[dota2] 闲聊走专用模型失败（{client.endpoint}）：{e}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[dota2] 闲聊走专用模型出现异常：{e}", exc_info=True)
        return None

    async def _call_report_llm(
        self, prompt: str, *, umo: str = "", system_prompt: str = ""
    ) -> str | None:
        """生成**报告 / 分析**用的大模型调用：专用 Key 优先，失败回退 AstrBot。

        Args:
            prompt: 用户提示词。
            umo: 会话来源，回退到 AstrBot 提供商时用来解析会话默认模型。
            system_prompt: 自定义系统提示词。留空则用配置项
                ``analysis_system_prompt``，再留空用内置的报告模板。

        Returns:
            报告正文；失败或未启用时返回 None（并在日志里说明原因）。
        """
        if not self.cfg("enable_llm_analysis", True):
            return None

        system_prompt = (system_prompt or "").strip()
        if not system_prompt:
            system_prompt = str(self.cfg("analysis_system_prompt", "") or "").strip()
        if not system_prompt:
            system_prompt = DEFAULT_SYSTEM_PROMPT

        client = self._dedicated_client()
        if client is not None:
            try:
                max_tokens = int(self.cfg("llm_max_tokens", 0) or 0)
                return await client.chat(
                    system_prompt,
                    prompt,
                    temperature=float(self.cfg("llm_temperature", 0.7) or 0),
                    max_tokens=max_tokens or None,
                )
            except LLMRequestError as e:
                logger.error(f"[dota2] 专用模型调用失败（{client.endpoint}）：{e}")
            except Exception as e:  # noqa: BLE001
                logger.error(f"[dota2] 专用模型调用出现异常：{e}", exc_info=True)

            if not self.cfg("llm_fallback_on_error", True):
                logger.warning("[dota2] 已关闭回退，本次不做 AI 分析")
                return None
            logger.warning("[dota2] 回退到 AstrBot 的模型提供商重试")

        provider = await resolve_provider(
            self.context,
            umo,
            str(self.cfg("llm_provider_id", "") or ""),
        )
        if provider is None:
            logger.warning("[dota2] 没有可用的模型提供商，跳过 AI 分析")
            return None
        try:
            return await call_llm(provider, system_prompt, prompt)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[dota2] 调用大模型失败: {e}", exc_info=True)
            return None

    async def _generate_report(
        self, event: AstrMessageEvent, prompt: str
    ) -> str | None:
        """调用大模型生成报告。失败或未启用时返回 None。"""
        return await self._call_report_llm(
            prompt, umo=event.unified_msg_origin
        )

    # ==================================================================
    # 监听后台任务
    # ==================================================================
    def _start_watcher(self) -> None:
        """启动监听循环（幂等）。"""
        if not self.cfg("watch_enabled", True):
            return
        if self._watch_task is not None and not self._watch_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError as e:  # pragma: no cover
                logger.error(f"[dota2] 无法启动监听任务：{e}")
                return
        self._stopping = False
        self._watch_task = loop.create_task(self._watch_loop())
        logger.info("[dota2] 比赛监听任务已启动")

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """AstrBot 初始化完成后确保监听任务已启动。"""
        self._start_watcher()

    async def _watch_loop(self) -> None:
        """监听主循环。

        内部是两个彼此独立的节拍：

        - **轮询节拍**（``watch_interval``）：拉取被监听玩家的比赛列表，发现新比赛；
        - **处理节拍**（``PENDING_TICK_SECONDS``）：兑现 pending 队列里的 ``next_try_at``。

        两者必须解耦：早先只有一个节拍，导致 ``next_try_at`` 即使设成 60 秒，
        也要等满一个 ``watch_interval``（默认 180 秒）才会被检查到。
        """
        # 启动后稍等一会，避免与 AstrBot 自身的启动流程抢资源
        await asyncio.sleep(15)
        logger.info(
            f"[dota2] 监听循环已就绪：开始轮询（间隔 "
            f"{max(60, int(self.cfg('watch_interval', 180)))} 秒，"
            f"队列处理节拍 {PENDING_TICK_SECONDS} 秒）"
        )
        while not self._stopping:
            try:
                now = time.time()
                if now >= self._next_poll_at:
                    poll_interval = max(60, int(self.cfg("watch_interval", 180)))
                    self._next_poll_at = now + poll_interval
                    self._poll_round += 1
                    await self._check_watchers()
                else:
                    await self._process_pending()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.error(f"[dota2] 监听循环出错：{e}", exc_info=True)
            try:
                await asyncio.sleep(PENDING_TICK_SECONDS)
            except asyncio.CancelledError:
                raise

    async def _check_watchers(self) -> None:
        """检查所有被监听玩家是否有新比赛。"""
        started = time.monotonic()
        watchers = self.store.list_watchers()

        if watchers:
            groups: dict[int, list[dict]] = {}
            for watcher in watchers:
                account_id = watcher.get("account_id")
                if isinstance(account_id, int):
                    groups.setdefault(account_id, []).append(watcher)

            concurrency = max(1, int(self.cfg("watch_concurrency", 2)))
            semaphore = asyncio.Semaphore(concurrency)

            logger.info(
                f"[dota2] 轮询第 {self._poll_round} 轮开始："
                f"监听者 {len(watchers)} 位｜去重后 {len(groups)} 个玩家｜"
                f"并发 {concurrency}｜待推送 {len(self._pending)} 场"
            )

            async def _runner(account_id: int, group: list[dict]) -> dict:
                async with semaphore:
                    try:
                        return await self._check_account(account_id, group)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:  # noqa: BLE001
                        logger.error(
                            f"[dota2] 检查玩家 {account_id} 的新比赛失败：{e}",
                            exc_info=True,
                        )
                        return {"account_id": account_id, "error": str(e)}

            reports = await asyncio.gather(
                *[_runner(account_id, group) for account_id, group in groups.items()]
            )
            self._log_poll_report(reports, started)
        else:
            logger.info(
                f"[dota2] 轮询第 {self._poll_round} 轮："
                f"当前没有监听者，跳过比赛列表拉取"
            )

        # 注意：即使当前一个监听者都没有，也要继续推进 pending 队列，
        # 否则队列里最后几场比赛会永远卡住（监听者被删掉时尤其明显）。
        await self._process_pending()

    def _log_poll_report(self, reports: list[Any], started: float) -> None:
        """把一轮轮询的结果汇成一条日志（每轮只此一行，便于对齐时间线）。

        之所以要汇总而不是只依赖 :meth:`_check_account` 里的逐玩家日志：
        轮询最常被问到的两个问题是「循环还在跑吗」和「这轮到底拉了没有」。
        一行里同时给出**轮次、拉取场数、新增场数、失败个数、队列长度、耗时**，
        以及每个玩家的 ``场数/新增`` 明细，扫一眼就能回答，不用数行。
        """
        ok = [r for r in reports if isinstance(r, dict) and not r.get("error")]
        failed = [r for r in reports if isinstance(r, dict) and r.get("error")]

        fetched = sum(int(r.get("fetched") or 0) for r in ok)
        queued = sum(int(r.get("queued") or 0) for r in ok)

        detail = "、".join(
            f"{r.get('account_id')}={r.get('fetched', 0)}场/新{r.get('queued', 0)}"
            for r in ok
        )

        logger.info(
            f"[dota2] 轮询第 {self._poll_round} 轮完成："
            f"拉取 {fetched} 场｜新增 {queued} 场入队｜"
            f"失败 {len(failed)} 个玩家｜待推送 {len(self._pending)} 场｜"
            f"耗时 {time.monotonic() - started:.1f}s"
            + (f"｜明细 {detail}" if detail else "")
        )

    async def _check_account(self, account_id: int, watchers: list[dict]) -> dict:
        """检查单个玩家是否有新比赛，有则加入待推送队列。

        返回值是一份「本轮体检报告」，交给 :meth:`_log_poll_report` 汇成
        一条日志。**返回值只服务日志**，不参与任何业务判断——调用方
        （含测试）忽略它也不会改变行为。
        """
        try:
            matches = await self.api.get_player_matches(account_id, limit=8)
        except OpenDotaError as e:
            logger.warning(f"[dota2] 拉取玩家 {account_id} 比赛列表失败：{e}")
            return {"account_id": account_id, "error": str(e)}

        if not matches:
            # 空列表要单独记：它可能是「这人真的没打过」，也可能是数据源
            # 抽风返回了空。不记的话，日志里这两种情况长得一模一样。
            logger.info(f"[dota2] 玩家 {account_id}：数据源返回空列表（本轮无比赛）")
            return {"account_id": account_id, "fetched": 0, "queued": 0}

        latest = max(int(m.get("match_id") or 0) for m in matches)

        # 门槛取「进度最落后的那个监听者」：只要还有监听者没看过这场，就必须处理。
        # 之前用的是 max()，于是一旦有新监听者加入（它记录的基线是当下最新的比赛），
        # 基线就被抬高，老监听者还没来得及看的比赛会被整体过滤掉。
        floor = min(int(w.get("last_match_id") or 0) for w in watchers)
        new_matches = [
            match for match in matches if int(match.get("match_id") or 0) > floor
        ]
        if not new_matches:
            return {
                "account_id": account_id,
                "fetched": len(matches),
                "latest": latest,
                "floor": floor,
                "queued": 0,
            }
        new_matches.sort(key=lambda m: int(m.get("match_id") or 0))

        # 被监听玩家自己的昵称（用于报告焦点玩家那一行），取第一个非空的
        focus_name = next(
            (
                str(w.get("personaname") or "")
                for w in watchers
                if w.get("personaname")
            ),
            "",
        )

        queued = 0
        for match in new_matches:
            match_id = int(match.get("match_id") or 0)
            targets = []
            seen_umo: set[str] = set()
            for watcher in watchers:
                # 每位监听者按自己的进度独立判断
                if int(watcher.get("last_match_id") or 0) >= match_id:
                    continue
                umo = str(watcher.get("umo") or "")
                if not umo or umo in seen_umo:
                    continue
                seen_umo.add(umo)
                targets.append(
                    {
                        # 记下 watcher_id，推送成功后才能精确推进这一位的进度
                        "watcher_id": str(watcher.get("id") or ""),
                        "umo": umo,
                        "platform": watcher.get("platform") or "",
                        "created_by": str(watcher.get("created_by") or ""),
                        "personaname": watcher.get("personaname") or "",
                    }
                )
            if targets:
                self._queue_pending(match_id, account_id, targets, focus_name)
                queued += 1

        # 这里故意**不**推进 last_match_id：它必须等到真正推送成功之后再落盘，
        # 否则一旦重启、崩溃或队列丢弃，这些比赛就永久漏推了。
        if queued:
            logger.info(
                f"[dota2] 玩家 {account_id} 发现 {queued} 场新比赛，已加入待推送队列"
                f"（最新 {latest}，基线 {floor}）"
            )

        return {
            "account_id": account_id,
            "fetched": len(matches),
            "latest": latest,
            "floor": floor,
            "queued": queued,
        }

    def _queue_pending(
        self,
        match_id: int,
        focus_account_id: int,
        targets: list[dict],
        focus_name: str = "",
    ) -> None:
        """把「一场比赛」加入待推送队列（等待数据源收录）。

        队列键就是 ``match_id``：**同一局比赛只保留一项**。当群里多位成员各自
        监听了不同的玩家、而这几位玩家又恰好打了同一局时，老实现会按
        ``(match_id, account_id)`` 拆成多份，于是同一场比赛被重复调用大模型、
        在群里刷好几份几乎一样的推送。现在他们共用一份短评与一次推送。

        每位焦点玩家在 ``focuses`` 里各占一项，以自己为焦点收集 ``targets``；
        生成短评时把所有人的 account_id 一起交给提示词，对每个人分别点一句，
        因此既不重复调用模型、也不会互相覆盖焦点。
        """
        item = self._pending.get(match_id)
        if item is None:
            item = {
                "key": match_id,
                "match_id": match_id,
                #: 本局中所有被监听的玩家：``[{"account_id", "name", "targets"}]``
                "focuses": [],
                #: 比赛还没被数据源收录的次数（收录是这里唯一要等的东西）
                "wait_attempts": 0,
                "first_seen": time.time(),
                "next_try_at": 0.0,
                #: 是否正在处理中（防止并发投递同一场比赛）
                "processing": False,
            }
            self._pending[match_id] = item

        focus = next(
            (
                entry
                for entry in item["focuses"]
                if entry.get("account_id") == focus_account_id
            ),
            None,
        )
        if focus is None:
            focus = {"account_id": focus_account_id, "name": focus_name, "targets": []}
            item["focuses"].append(focus)
        elif not focus.get("name") and focus_name:
            focus["name"] = focus_name

        # 同一个 watcher 只会有一条记录，按 watcher_id 去重；
        # 至于「同一会话里的两个人各监听一个玩家」这种情况，两条都要留着，
        # 推送时按会话聚合，@ 的时候会把两人都带上。
        known = {target.get("watcher_id") for target in focus["targets"]}
        for target in targets:
            watcher_id = target.get("watcher_id")
            if watcher_id and watcher_id in known:
                continue
            focus["targets"].append(target)
            known.add(watcher_id)

        max_pending = max(1, int(self.cfg("watch_max_pending", 20)))
        if len(self._pending) > max_pending:
            oldest = min(self._pending.values(), key=lambda x: x["first_seen"])
            # 被丢弃的比赛不推进 last_match_id，下一轮会被重新发现，
            # 因此队列溢出只会推迟推送，不会静默漏推。
            self._pending.pop(oldest["key"], None)
            # 让出的条目下轮会重新入队，它的失败计数留着毫无意义（而且会随
            # 每次重新入队悄悄累积），一并清掉。
            self._clear_watch_failures(int(oldest.get("match_id") or 0))
            logger.warning(
                f"[dota2] 待推送队列已满（{max_pending}），"
                f"暂时让出比赛 {oldest['match_id']}，稍后重新入队"
            )

    # ------------------------------------------------------------------
    # 推送失败计数
    #
    # 独立于 pending 项存在，因为 pending 项每次「重新入队」都是新对象，
    # 挂在它上面的计数会被清零 —— 那正是掉线时无限重试的根因。
    # ------------------------------------------------------------------

    def _watch_deliver_limit(self) -> int:
        """单会话单场比赛的推送失败上限（达到即放弃）。"""
        return max(1, int(self.cfg("watch_max_deliver_attempts", 10)))

    def _watch_fail_count(self, match_id: int, umo: str) -> int:
        return int(self._watch_failures.get(int(match_id), {}).get(umo, 0))

    def _bump_watch_failure(self, match_id: int, umo: str) -> tuple[int, bool]:
        """累加「某会话推送某场比赛」的失败次数。

        Returns:
            ``(累计失败次数, 是否已达到上限)``。达到上限即意味着放弃——调用方
            会把该会话标记为「已处理」并推进其监听基线，让它不再被重新发现，
            从而终止重试循环。
        """
        match_id = int(match_id)
        bucket = self._watch_failures.setdefault(match_id, {})
        count = int(bucket.get(umo, 0)) + 1
        bucket[umo] = count
        return count, count >= self._watch_deliver_limit()

    def _clear_watch_failures(self, match_id: int) -> None:
        """整场比赛结束后清掉它的失败计数与短评缓存（防止两张表无限增长）。

        「整场结束」= 全部送达、或全部放弃、或被队列淘汰。这些时刻过后这场
        比赛不会再被投递，留着计数和正文都没有意义。

        短评缓存复用同一个清理时机，是因为两者的存活条件本来就相同：只要还有
        会话在重试（``still_retrying``），两样都得留着；一旦没人重试，两样都该扔。
        """
        match_id = int(match_id)
        self._watch_failures.pop(match_id, None)
        self._watch_comments.pop(match_id, None)

    async def _process_pending(self) -> None:
        """处理待推送队列：等待详细数据就绪后生成分析并推送。"""
        if not self._pending:
            return
        now = time.time()
        due = [
            item
            for item in list(self._pending.values())
            if item.get("next_try_at", 0) <= now and not item.get("processing")
        ]
        if not due:
            # 队列非空、但每一场都还没到重试时刻 —— 这正是「积压排队」的样子
            # （早先那场被堵了 21 分钟的比赛就属于这种状态）。用 debug 记录，
            # 免得每 30 秒刷一行 info。
            logger.debug(
                f"[dota2] 待推送队列 {len(self._pending)} 场，本刻均未到重试时间"
            )
            return

        # 单轮内共享：一局比赛无论有多少位被监听的玩家参战，都只拉一次比赛详情、
        # 只调用一次大模型。
        match_cache: dict[int, dict | None] = {}

        for item in due:
            # due 是一次性算好的，期间另一个并发的检查可能已经把这一项处理掉了，
            # 因此逐项复查。检查与置位之间没有 await，不会被其它任务插进来。
            if item.get("processing"):
                continue
            if self._pending.get(item.get("key")) is not item:
                continue

            match_id = int(item["match_id"])
            # 标记「处理中」：避免两次并发的检查（后台循环 + 懒启动/手动触发）
            # 同时投递同一场比赛，造成重复推送。
            item["processing"] = True
            try:
                await self._try_deliver(item, match_cache)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                # 这里只接「漏出 _try_deliver 的意外」，正常情况下走不到。
                # 同样要有上限：异常若是恒定复现（例如格式化代码有 bug），
                # 不封顶就会每 5 分钟在后台空转一次，永远不停。
                logger.error(f"[dota2] 推送比赛 {match_id} 失败：{e}", exc_info=True)
                errors = int(item.get("error_attempts", 0)) + 1
                item["error_attempts"] = errors
                if errors >= 3:
                    logger.error(
                        f"[dota2] 比赛 {match_id} 连续 {errors} 次处理异常，放弃推送"
                    )
                    try:
                        await self._finish(item, delivered_umos=set(), force_advance=True)
                    except Exception as finish_err:  # noqa: BLE001
                        logger.error(
                            f"[dota2] 放弃比赛 {match_id} 时再次出错：{finish_err}"
                        )
                else:
                    item["next_try_at"] = time.time() + 300
            finally:
                item["processing"] = False

            # `_finish` 可能已经把这一项 pop 掉了；此时再写字段没有意义
            if self._pending.get(item.get("key")) is not item:
                continue

    async def _try_deliver(
        self,
        item: dict,
        match_cache: dict[int, dict | None],
    ) -> None:
        """尝试获取比赛数据并推送；比赛还没被收录时安排下次重试。

        这里**不再等待 OpenDota 解析录像**。监听推送的内容已经改成
        「胜负 + K/D/A + 近期战绩」的赛后短评（见
        :func:`dota_analyzer.build_watch_comment_prompt`），这些字段在比赛被
        收录的那一刻就齐全；而等解析要十几分钟，只会把推送白白推迟。
        需要解析产物（逐分钟曲线、团战逐人等）的深度复盘走
        ``/d2 单场 <比赛ID>``，那条链路该等还是会等。
        """
        match_id = int(item["match_id"])
        max_wait = max(1, int(self.cfg("watch_max_wait_attempts", 30)))

        if match_id in match_cache:
            match = match_cache[match_id]
        else:
            match = await self.api.get_match(match_id)
            match_cache[match_id] = match

        # ---- 数据源尚未收录这场比赛 ----
        # 短评要的胜负与 K/D/A 也在这份数据里，所以这里必须等收录。
        if not match:
            item["wait_attempts"] += 1
            waited = item["wait_attempts"]
            if waited > max_wait:
                logger.warning(
                    f"[dota2] 比赛 {match_id} 等待数据源收录超过 {max_wait} 次，"
                    f"放弃推送"
                )
                await self._finish(item, delivered_umos=set(), force_advance=True)
                return
            delay = 60 if waited <= 5 else 180
            item["next_try_at"] = time.time() + delay
            logger.info(
                f"[dota2] 比赛 {match_id} 尚未被数据源收录"
                f"（第 {waited}/{max_wait} 次），{delay}s 后重试"
            )
            return

        delivered = await self._deliver(item, match)

        # 连一个可推送的目标都没有（异常数据）：直接了结，不要每轮重新入队
        if item.get("nothing_to_do"):
            logger.warning(
                f"[dota2] 比赛 {match_id} 没有可推送的目标会话，放弃推送"
            )
            await self._finish(item, delivered_umos=set(), force_advance=True)
            return

        # 连续失败达到上限的会话：放弃。``_finish`` 会推进它们的监听基线，
        # 这样下一轮 _check_account 不会再发现这场比赛，重试循环到此终止。
        # 计数在 :attr:`_watch_failures` 里，跨「重新入队」存活——早先挂在
        # item 上的版本每次重建都归零，掉线时就是无限循环。
        abandoned = set(item.get("abandoned_umos") or ())
        if abandoned:
            logger.warning(
                f"[dota2] 比赛 {match_id} 放弃向 {len(abandoned)} 个会话推送"
                f"（连续失败已达上限 {self._watch_deliver_limit()} 次；"
                f"常见原因：bot 掉线、群内无主动消息权限、账号被风控。"
                f"各会话的末次失败原因见上一条日志）"
            )

        await self._finish(item, delivered_umos=delivered, abandoned_umos=abandoned)

    async def _finish(
        self,
        item: dict,
        delivered_umos: set[str],
        force_advance: bool = False,
        abandoned_umos: set[str] | None = None,
    ) -> None:
        """结束一个待推送项，并推进已成功送达的监听者进度。

        ``last_match_id`` 只在**成功推送之后**才落盘：推送失败的会话保持原基线，
        下一轮会被重新发现并补推，从而实现「至少一次」投递而非「可能永久漏推」。

        Args:
            item: 待推送项。
            delivered_umos: 推送成功的会话标识集合。
            force_advance: 主动放弃这场推送时置 True，避免每轮都重新入队空转。
            abandoned_umos: 连续失败已达上限、被放弃的会话。它们并没有送达，
                但**同样要推进基线**——否则「基线不动 → 下一轮重新发现 →
                再失败」会一直转下去，这正是掉线时后台无限重试的形状。
                放弃一场旧比赛，好过让队列永远卡在上面。
        """
        self._pending.pop(item.get("key"), None)
        match_id = int(item["match_id"])
        abandoned = abandoned_umos or set()

        # 只有在生成这份报告时就已经纳入的焦点玩家，才能因为本次推送而推进进度。
        # 若某位焦点玩家是在报告生成之后才被并进这一项的（并发检查时的窄窗口），
        # 他的监听者不能算「已看过这场」，否则会永久漏掉这场比赛。
        covered = item.get("delivered_focus_ids")
        focuses = item.get("focuses") or []
        leftovers: list[dict] = []

        updates: dict[str, int] = {}
        for focus in focuses:
            if (
                not force_advance
                and covered is not None
                and focus.get("account_id") not in covered
            ):
                leftovers.append(focus)
                continue
            for target in focus.get("targets") or []:
                umo = str(target.get("umo") or "")
                if (
                    not force_advance
                    and umo not in delivered_umos
                    and umo not in abandoned
                ):
                    continue
                watcher_id = str(target.get("watcher_id") or "")
                if watcher_id:
                    updates[watcher_id] = match_id

        # 还有会话既没送达、也没被放弃 ⇒ 它们要等下一轮补推。
        # 只有在这种情况下才**必须**保留失败计数。若一律清除，下一轮重新入队时
        # 计数就从零开始，上限永远达不到 —— 那正是掉线时无限重试的形状。
        still_retrying = any(
            str(target.get("umo") or "") not in delivered_umos
            and str(target.get("umo") or "") not in abandoned
            for focus in focuses
            for target in (focus.get("targets") or [])
        )
        if force_advance or not still_retrying:
            self._clear_watch_failures(match_id)

        if not updates and not leftovers:
            return
        if updates:
            try:
                await self.store.update_watcher_last_match_bulk(updates)
            except Exception as e:  # noqa: BLE001
                logger.error(f"[dota2] 更新监听进度失败：{e}")
        for focus in leftovers:
            # 重新入队（比赛详情已就绪，下一轮会立刻投递）
            self._queue_pending(
                match_id,
                int(focus.get("account_id") or 0),
                list(focus.get("targets") or []),
                str(focus.get("name") or ""),
            )
        if leftovers:
            logger.info(
                f"[dota2] 比赛 {match_id} 有 {len(leftovers)} 位焦点玩家未纳入本次报告，"
                "已重新入队，下一轮单独推送"
            )

    async def _deliver(self, item: dict, match: dict) -> set[str]:
        """生成「赛后短评」并推送到所有目标会话。

        **一次调用只生成一份短评**，即使本局有多位被监听的玩家参战：短评里会
        为每位焦点玩家各点一句，按会话聚合后每个会话只推送一条。

        推送内容只要胜负与 K/D/A（外加各人近期战绩作对照），因此**不依赖录像
        解析**，比赛一被数据源收录就能发出。想要完整分析的走
        ``/d2 单场 <比赛ID>``，那条链路才用深度复盘提示词。

        注意这里**不 @ 任何订阅人**：比赛结束的自动播报不该每次都在群里点名。

        Returns:
            推送成功的会话标识（umo）集合。调用方据此决定是否推进这些监听者的
            进度——失败的会话保持原基线，下一轮会重新入队补推。
        """
        match_id = int(item["match_id"])
        # 没有任何可推送目标时置位，让调用方直接了结这一项，而不是每轮空转
        item.pop("nothing_to_do", None)
        focuses: list[dict] = item.get("focuses") or []
        if not focuses:
            item["nothing_to_do"] = True
            return set()

        # 快照本次报告覆盖的焦点玩家：报告生成期间若又有新焦点被并入这一项，
        # 它们不能算作「已推送」，由 _finish 重新入队。
        focus_ids = [
            int(focus["account_id"])
            for focus in focuses
            if int(focus.get("account_id") or 0)
        ]
        if not focus_ids:
            item["nothing_to_do"] = True
            return set()
        focus_names = {
            int(focus["account_id"]): str(focus.get("name") or "")
            for focus in focuses
            if int(focus.get("account_id") or 0)
        }
        item["delivered_focus_ids"] = set(focus_ids)

        # 按会话聚合：同一会话（群）可能既是 A 的监听者、也是 B 的监听者，
        # 这一局只应收到一条推送。这里只做「按会话去重」——早期的 @ 已移除，
        # 因此不再收集 creators。
        per_umo: dict[str, dict] = {}
        for focus in focuses:
            for target in focus.get("targets") or []:
                umo = str(target.get("umo") or "")
                if not umo:
                    continue
                if umo not in per_umo:
                    per_umo[umo] = {
                        "umo": umo,
                        "platform": target.get("platform") or "",
                        "personaname": target.get("personaname") or "",
                    }
        targets = list(per_umo.values())
        if not targets:
            item["nothing_to_do"] = True
            return set()

        # 已经放弃过的会话不再浪费一次大模型调用。正常流程里它们不会重新出现
        # （放弃时基线已被推进），这里是给并发窗口兜底：同一场比赛可能被两个
        # 监听者同时发现，其中一个刚放弃、另一个还没。
        limit = self._watch_deliver_limit()
        abandoned: set[str] = set()
        active: list[dict] = []
        for target in targets:
            umo = str(target.get("umo") or "")
            if umo and self._watch_fail_count(match_id, umo) >= limit:
                abandoned.add(umo)
                continue
            active.append(target)
        item["abandoned_umos"] = abandoned
        if not active:
            logger.warning(
                f"[dota2] 比赛 {match_id} 的所有目标会话都已放弃，跳过本次推送"
            )
            return set()
        targets = active

        # 短评只需要英雄名（把 hero_id 写成人话），不再拉道具与技能常量：
        # 那两次请求原先是为深度复盘准备的，短评用不上。
        try:
            heroes = await self._heroes()
        except OpenDotaError as e:
            logger.error(f"[dota2] 推送比赛 {match_id} 时拉取英雄常量失败：{e}")
            heroes = {}

        # 标题不带「数据完整度」：短评不依赖解析，写上去只会让人以为数据有问题。
        headline = self.match_headline(
            match, heroes, focus_ids, with_parsed_note=False, focus_names=focus_names
        )

        # 大模型短评**整场只生成一次**：既在多个会话之间复用，也在多次重试之间复用。
        # 走 `_call_report_llm`：专用 API Key 优先，未配置时回退 AstrBot 提供商。
        # 系统提示词用短评专用版本——报告那套要求 Markdown 小标题、单场复盘
        # 还要写足上千字，会把「几句话」带成一篇小作文。
        #
        # 命中缓存时连「拉近期战绩」都跳过：那是真实的网络请求，而正文已经生成
        # 好了，重新拉一遍只是为拼一个不会再被使用的 prompt，纯属浪费。
        comment = self._watch_comments.get(match_id)
        if comment is not None:
            logger.info(
                f"[dota2] 比赛 {match_id} 复用已生成的短评，跳过本次大模型调用"
            )
        else:
            # 近期战绩：短评要靠它判断「这局是不是正常发挥」。
            recent_blocks = await self._recent_summary_blocks(
                match_id, focus_ids, focus_names, heroes
            )
            if len(focus_ids) > MAX_FOCUS_RECENT_CONTEXT:
                logger.debug(
                    f"[dota2] 比赛 {match_id} 焦点玩家较多，仅前 "
                    f"{MAX_FOCUS_RECENT_CONTEXT} 位附带近期战绩"
                )

            prompt = build_watch_comment_prompt(
                match=match,
                heroes=heroes,
                focus_account_ids=focus_ids,
                focus_names=focus_names,
                recent_blocks=recent_blocks,
            )
            comment = await self._call_report_llm(
                prompt,
                umo=targets[0]["umo"],
                system_prompt=self._watch_comment_system_prompt(),
            )
            # 只在真的生成成功时缓存：返回 None（模型不可用）必须留机会重试，
            # 否则会把「模型临时抖动」钉死成「这场比赛永远没有短评」。
            if comment:
                self._watch_comments[match_id] = comment

        delivered: set[str] = set()
        for target in targets:
            umo = target["umo"]
            try:
                # 「送达」的判定点是**标题发出去**：标题一进群，用户就已经看到这条
                # 推送了，此时若再因为正文二次发送失败而回退基线，下一轮会把同一场
                # 比赛重新发现并再推一遍标题 —— 群里就会无限复读那一行标题。
                #
                # 这不是假想：线上日志里「🏁 比赛回顾 · 8996928421」从 20:21 一路
                # 复读到 20:39（7 次、间隔 2~3.5 分钟）。
                # （当时的对照复现在 tests/old_code_repro.py；那个脚本针对的是旧版
                #  「图片正文」链路，v2.0.0 改成短评后已失效，保留仅供追溯。）
                #
                # 因此正文发送失败只记日志、不回退，绝不影响 delivered。
                headline_ok = await self._send_to_session(umo, headline)
                if not headline_ok:
                    # bot 账号掉线时这里会一直失败。次数达到上限就放弃这个会话：
                    # 把它记进 abandoned，_finish 会推进它的基线，
                    # 下一轮不再重新发现这场比赛 —— 重试循环就此终止。
                    count, hit_limit = self._bump_watch_failure(match_id, umo)
                    if hit_limit:
                        abandoned.add(umo)
                        item["abandoned_umos"] = set(abandoned)
                        logger.warning(
                            f"[dota2] 向 {umo} 推送比赛 {match_id} 连续失败 "
                            f"{count} 次（上限 {limit}），放弃该会话的这场推送"
                            f"{self._last_error_tail(umo)}"
                        )
                    else:
                        logger.warning(
                            f"[dota2] 向 {umo} 推送比赛 {match_id} 的标题未送达"
                            f"（第 {count}/{limit} 次），稍后重试"
                        )
                    continue

                # 送达即清零：偶发的一次失败不该累积成放弃
                self._watch_failures.get(match_id, {}).pop(umo, None)
                delivered.add(umo)
                logger.info(f"[dota2] 比赛 {match_id} 的赛后短评已推送到 {umo}")
                # 记进会话语境：用户紧接着问「详细分析这一盘」时靠它消歧，
                # 闲聊兜底问「昨天谁打得好」时也靠它 —— 这里是唯一拿到完整
                # match 的时机，所以把英雄 / KDA / 胜负 / 时长一次写足，
                # 并带上**比赛自己的开赛时间**（不能拿记录时刻顶替）。
                desc = dota_chat.describe_focus_result(
                    match, heroes, focus_ids, focus_names
                )
                self._nlu_remember_match(
                    umo,
                    match_id,
                    desc,
                    start_time=match.get("start_time"),
                )
                # 「每监听到 N 场就总结」的计数窗口：只在**送达之后**记一笔。
                # 没送达的场次不能计数 —— 否则用户会收到一份含他根本没看到的
                # 比赛的总结。攒够时它自己派后台任务去生成，不阻塞这条链路。
                await self._push_count_tasks(
                    umo, match_id, desc, match.get("start_time")
                )

                # 正文属于「尽力而为」：失败不影响送达判定，只提示用户去看概览。
                # `_send_watch_comment` 内部已逐块收敛异常，这里的 try 是最后一道保险。
                try:
                    body_ok = await self._send_watch_comment(umo, comment)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"[dota2] 向 {umo} 推送比赛 {match_id} 的短评失败：{e}")
                    body_ok = False
                if not body_ok:
                    logger.warning(
                        f"[dota2] 比赛 {match_id} 的短评未能送达 {umo}，"
                        f"标题已发出，不再重复推送"
                    )
                    await self._notify_body_failed(umo, match_id)
            except Exception as e:  # noqa: BLE001
                # 兜底：`_send_to_session` / `_send_watch_comment` 内部已各自收敛异常，
                # 走到这里说明出了预期外的问题（例如常量或格式化代码抛错）。
                # 此时该会话本轮只发出去一部分，补一条提示让失败可感知——
                # 否则用户会看到「只有标题、后面什么都没有」而不知道为什么。
                logger.exception(f"[dota2] 向 {umo} 推送比赛 {match_id} 时出现预期外异常")
                if umo in delivered:
                    await self._notify_body_failed(umo, match_id)
        return delivered

    def _watch_comment_system_prompt(self) -> str:
        """监听短评用的系统提示词；配置留空则用内置版本。

        必须是**独立**的一套：``analysis_system_prompt`` 是给长报告写的
        （要求 Markdown 小标题、单场深度复盘 1500~3000 字），拿它写
        「2~4 句话」的短评会被带成一篇小作文，正好违背监听推送的初衷。
        """
        text = str(self.cfg("watch_comment_system_prompt", "") or "").strip()
        return text or WATCH_COMMENT_SYSTEM_PROMPT

    async def _send_watch_comment(self, umo: str, comment: str | None) -> bool:
        """发送监听短评正文。

        短评只有几句话（几十到一两百字），因此**不转图片、也不做报告式分块**：
        直接按 ``max_message_length`` 拆成文本发出去即可。模型不可用时给一句
        说明，让用户知道不是插件哑了。

        每一块的发送都是独立的：某一块抛异常或返回 False 只会让这一块失败，
        后续分块仍会继续尝试（早期实现里任何一块出错就直接冒泡出函数，
        导致「第 2 块失败 → 第 3 块起全部丢失」）。
        """
        if not comment:
            return await self._send_quiet(
                umo,
                "⚠️ 未启用大模型分析或模型不可用，仅提供上述比赛概览。",
                match_id=None,
            )

        ok = True
        chunks = self._chunk_text(comment)
        for index, chunk in enumerate(chunks, start=1):
            # 逐块 try/except：单块失败不能拖垮后面的分块。
            if not await self._send_quiet(umo, chunk):
                logger.warning(
                    f"[dota2] 短评第 {index}/{len(chunks)} 块发送失败：{umo}"
                )
                ok = False
        return ok

    async def _send_quiet(
        self,
        umo: str,
        text: str | None,
        *,
        match_id: int | None = None,
    ) -> bool:
        """发送一条**文本**消息并把异常收敛成 ``False``。

        统一入口，避免每个调用点各写一遍 try/except——前面就是因为漏了一处，
        让正文异常直接冒泡到 ``_deliver`` 的外层 except，最后只留在日志里，
        用户侧完全无感。

        只发文本：监听推送已改成几句短评（不需要转图片），需要图片的是
        ``/d2 单场`` 那条链路，它在自己那边处理渲染与回退。
        """
        try:
            chain = MessageChain().message(text or "")
            return await self._send(umo, chain) is not False
        except Exception as e:  # noqa: BLE001
            label = f"比赛 {match_id} 的" if match_id is not None else ""
            logger.error(f"[dota2] 发送{label}消息到 {umo} 失败：{e}")
            return False

    async def _notify_body_failed(self, umo: str, match_id: int) -> None:
        """正文发送失败时给一条轻量提示，避免用户以为「只有标题、没有点评」。

        提示本身也走 :meth:`_send_quiet`：连提示都发不出去时只记日志，
        绝不能再往上报——否则会把「正文失败」升级成「整轮推送失败」。
        """
        await self._send_quiet(
            umo,
            f"⚠️ 比赛 {match_id} 的短评未能发出（平台发送通道异常），"
            f"上方为比赛概览。可用 `/d2 单场 {match_id}` 获取完整复盘。",
            match_id=match_id,
        )

    # ------------------------------------------------------------------
    # 会话场景记录（主动推送的前置条件，v2.2.5）
    # ------------------------------------------------------------------
    def _scene_records_path(self) -> Path:
        """会话场景记录的落盘路径。"""
        return Path(self.data_dir) / "proactive_scenes.json"

    def _load_scene_records(self) -> dict[str, str]:
        """读会话场景记录（首次访问时从磁盘加载，坏文件按空处理）。"""
        if self._scene_records is not None:
            return self._scene_records
        records: dict[str, str] = {}
        try:
            path = self._scene_records_path()
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    records = {
                        str(key): str(value)
                        for key, value in raw.items()
                        if isinstance(value, str)
                    }
        except Exception as e:  # noqa: BLE001 - 记录读不出来不该影响推送
            logger.warning(f"[dota2] 读取会话场景记录失败（按空处理）：{e}")
        self._scene_records = records
        return records

    def _remember_scene(self, umo: str, scene: str) -> None:
        """记下某个会话的场景（群 / 频道 / 私聊），变化时落盘。

        只在**真的观测到**入站消息时调用（见 :meth:`_record_inbound_scene`），
        所以这份记录是证据，不是猜测。
        """
        if not umo or scene not in ("group", "channel", "friend"):
            return
        records = self._load_scene_records()
        if records.get(umo) == scene:
            return
        records[umo] = scene
        try:
            path = self._scene_records_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001 - 落盘失败只影响下次重启，不影响本次
            logger.warning(f"[dota2] 写入会话场景记录失败：{e}")

    def _record_inbound_scene(self, event: AstrMessageEvent) -> None:
        """从一条**入站消息**里识别会话场景并记下来。

        判定依据是原始平台的报文对象，不是 umo —— AstrBot 把 QQ **频道**消息
        也标成 ``GroupMessage``（``on_at_message_create`` 就是这么写的），
        只看 umo 分不出群与频道；而群消息带 ``group_openid``、频道消息带
        ``channel_id``，这个差别是可靠的。
        """
        try:
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            if raw is None:
                return
            if hasattr(raw, "group_openid"):
                scene = "group"
            elif hasattr(raw, "channel_id"):
                scene = "channel"
            else:
                return
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            self._remember_scene(umo, scene)
        except Exception as e:  # noqa: BLE001 - 记场景失败绝不能影响这条消息的处理
            logger.debug(f"[dota2] 记录会话场景失败（可忽略）：{e}")

    def _ensure_proactive_scene(self, umo: str) -> None:
        """发送前确认「适配器知道这个会话是群」，缺了就补登记。

        背景（v2.2.5 修的就是它）：AstrBot 的 ``Context.send_message`` 最终
        走到 ``platform.send_by_session``。QQ 官方适配器
        （``qqofficial_platform_adapter._send_by_session_common``）里有这样一道闸门::

            allow_group_proactive_send = (
                session.message_type == GROUP_MESSAGE
                and scene == "group"          # ← _session_scene 内存字典
                and self._allow_group_proactive_send
            )
            if not msg_id and ... and not allow_group_proactive_send:
                logger.warning("No cached msg_id for session: %s, skip send_by_session")
                return                        # ← 静默丢弃，不抛异常也不返回 False

        而 ``_session_scene`` 只在**收到入站消息**时写入，且随进程重启清空。
        于是「重启后该群还没人说过话」时，所有主动推送都被这样悄悄丢掉，
        插件这边却因为 ``send_message`` 返回 True 而记下「推送成功」。

        2026-09-17 09:54 就是这么丢掉 7 场推送的（群里一条都没有，日志里
        却全是「已推送到」）。因此这里在发送前把场景补登记回去。

        用 ``remember_session_scene`` 而不是直接改私有字典：它是适配器的公开
        方法（webhook 版适配器、WeCom 适配器都实现了同名方法），语义与适配器
        自己收到群消息时做的事完全一致。若某个适配器没有这个方法，说明它不
        需要这道闸门，直接跳过。

        优先用**观测到**的记录；没有记录时按 umo 的 ``GroupMessage`` 推断为群
        —— 这一步是推断而非证据，但代价可控（万一真是频道，平台会回一个
        参数错误、计入失败次数后放弃，不会投递到错误的会话）。
        """
        try:
            parts = str(umo).split(":", 2)
            if len(parts) != 3 or parts[1] != "GroupMessage":
                # 私聊不经过这道闸门，不用管
                return
            platform_id, session_id = parts[0], parts[2]
            if not session_id:
                return

            platform = self._find_platform(platform_id)
            remember = getattr(platform, "remember_session_scene", None)
            if not callable(remember):
                return

            # 适配器自己已经记着（正常情况：群里刚有人说过话）就什么都不做
            known = getattr(platform, "_session_scene", None)
            if isinstance(known, dict) and known.get(session_id) == "group":
                return

            recorded = self._load_scene_records().get(umo)
            if recorded is not None and recorded != "group":
                # 明确记过是频道，那就不是「群主动发送」这条路，交回适配器自己判断
                return

            remember(session_id, "group")
            if umo not in self._scene_seeded:
                self._scene_seeded.add(umo)
                origin = "按已知记录" if recorded else "按 GroupMessage 推断"
                logger.info(
                    f"[dota2] 主动推送前置：适配器里没有 {umo} 的场景记录"
                    f"（通常是重启后该会话还没人发言），已{origin}补登记为群聊"
                )
        except Exception as e:  # noqa: BLE001 - 补登记失败不能拖垮真正的发送
            logger.debug(f"[dota2] 补登记会话场景失败（可忽略）：{e}")

    def _find_platform(self, platform_id: str):
        """按平台实例 id 取回平台对象（与 ``Context.send_message`` 的匹配口径一致）。"""
        manager = getattr(self.context, "platform_manager", None)
        for inst in getattr(manager, "platform_insts", None) or []:
            try:
                if inst.meta().id == platform_id:
                    return inst
            except Exception:  # noqa: BLE001 - 个别平台取 meta 失败不该中断查找
                continue
        return None

    async def _send(self, umo: str, chain: MessageChain) -> bool | None:
        """发送单条消息并归一化返回值（不同版本 AstrBot 的返回契约不一致）。

        这里是全插件**唯一的发送出口**，因此「会话已不可达」的判定也放在这里：
        无论是监听推送、指令回复还是提示消息，只要平台侧明确说「bot 已不是群成员 /
        已被拉黑」，就顺势清理该会话下的监听。

        发送前还会补一次会话场景登记（见 :meth:`_ensure_proactive_scene`）：
        QQ 官方适配器在「不知道会话是不是群」时会**静默丢弃**消息却仍让
        调用方以为成功，这一步是唯一能在不读平台内部状态的前提下绕过它的点。
        """
        # 只对「群主动发送」这条路上的适配器有意义；其余平台这个方法不存在，
        # 内部会直接跳过，不会带来额外开销。
        self._ensure_proactive_scene(umo)
        try:
            result = await self.context.send_message(umo, chain)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self._note_send_error(umo, f"{type(e).__name__}: {e}")
            reason = looks_unreachable(e)
            if reason:
                # 这种失败重试多少次都不会恢复，别让它占用后续的推送轮次
                await self._on_session_unreachable(umo, reason)
            raise  # 交回上层按普通「发送失败」处理

        # 平台也可能用「返回 False 而不抛异常」表示失败，这条路径同样要记原因，
        # 否则放弃时的日志只能写「无异常细节」，排查时依旧两眼一抹黑。
        if result is False:
            self._note_send_error(umo, "平台返回 False（未抛异常，无更多细节）")
        else:
            self._last_send_error.pop(umo, None)
        return result

    def _note_send_error(self, umo: str, detail: str) -> None:
        """记下某会话最近一次发送失败的原因摘要，供「放弃推送」的日志引用。

        早先那句「bot 账号可能已掉线」是写死的猜测：线上真实原因是平台
        **无主动消息权限**（QQ 官方机器人 40034105），这句猜测把排查方向
        整个带偏了。改成记录真实异常后，下次看日志就能直接分辨是掉线、
        无权限还是被风控。
        """
        text = " ".join(str(detail).split())
        self._last_send_error[umo] = text[:160]

    def _last_error_tail(self, umo: str) -> str:
        """把末次发送失败原因渲染成可直接拼进日志的尾巴（无则空串）。"""
        detail = self._last_send_error.get(str(umo))
        return f"，末次原因：{detail}" if detail else ""

    async def _on_session_unreachable(self, umo: str, reason: str) -> None:
        """bot 被移出群聊 / 被拉黑后，清掉该会话下的所有监听。

        这些监听已经永远不可能送达了，留着只会让后台每轮都白跑一次
        （还会白白消耗一次大模型配额）。判定由 :func:`looks_unreachable`
        从严把关，误删不可逆，宁可漏判。
        """
        if not bool(self.cfg("watch_auto_unwatch_on_kick", True)):
            logger.info(
                f"[dota2] {umo} 疑似{reason}，"
                f"但 watch_auto_unwatch_on_kick 已关闭，保留监听"
            )
            return

        removed = 0
        try:
            removed = await self.store.remove_watchers_for_umo(umo)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[dota2] 清理 {umo} 的监听失败：{e}")

        # 待推送队列里指向该会话的条目也要一并清掉，否则这一轮还会再试一次
        dropped = self._drop_pending_for_umo(umo)
        self._clear_watch_failures_for_umo(umo)
        # 这个会话的监听已经清空，留着它的失败原因没有意义
        self._last_send_error.pop(umo, None)

        if removed or dropped:
            logger.warning(
                f"[dota2] 检测到 {reason}，已自动取消 {umo} 的 {removed} 条监听"
                f"（并丢弃 {dropped} 项待推送）"
            )
        else:
            logger.info(f"[dota2] 检测到 {reason}，{umo} 本就没有监听，无需清理")

    def _drop_pending_for_umo(self, umo: str) -> int:
        """从待推送队列里剔除某个会话，返回受影响的比赛数。

        只摘掉这一个会话的投递目标；若某场比赛已经没有任何目标，整项作废。
        """
        affected: set[int] = set()
        for key in list(self._pending.keys()):
            item = self._pending.get(key)
            if not isinstance(item, dict):
                continue
            focuses = item.get("focuses") or []
            for focus in list(focuses):
                targets = focus.get("targets") or []
                kept = [t for t in targets if str(t.get("umo") or "") != umo]
                if len(kept) == len(targets):
                    continue
                focus["targets"] = kept
                affected.add(int(item.get("match_id") or 0))
                if not kept:
                    focuses.remove(focus)
            if not focuses:
                self._pending.pop(key, None)
                self._clear_watch_failures(int(item.get("match_id") or 0))
        return len(affected)

    def _clear_watch_failures_for_umo(self, umo: str) -> None:
        for match_id in list(self._watch_failures.keys()):
            bucket = self._watch_failures.get(match_id)
            if isinstance(bucket, dict) and umo in bucket:
                bucket.pop(umo, None)
                if not bucket:
                    self._watch_failures.pop(match_id, None)

    async def _send_to_session(self, umo: str, text: str) -> bool:
        """发送推送标题，按 ``max_message_length`` 分块。

        **故意不 @ 任何订阅人**：这是「比赛结束的自动播报」，一次推送就在群里
        @ 一串人是纯打扰。早期实现会 @ 该会话里所有添加过监听的人
        （``target["creators"]``，配置项 ``watch_notify_at``），现已整体移除——
        想被提醒的成员自己看群消息即可。

        顺带把**原文记进会话历史**（``kind="report"``，记未分块的整段，
        不记分块）：这是复盘报告 / 定时播报 / 监听推送的**唯一出口**，
        在这里记一笔才能让用户接着问「刚才那场他补刀多少」时答得上来。
        不带这个记录时，报告发出去就再也进不了任何上下文 —— 那正是
        「调用工具生成的报告不在上下文」的直接原因。

        Returns:
            是否全部发送成功。失败时调用方会保留该监听者的基线以便补推。
        """
        # 先记再发：发送可能因为适配器抖动失败，但内容**已经产出**了，
        # 用户下次追问时模型应该能看到它。
        self._nlu_log_line(umo, "bot", text, kind="report")
        ok = True
        chunks = self._chunk_text(text)
        for index, chunk in enumerate(chunks, start=1):
            # 与正文一致：单块失败（含抛异常）不拖垮后续分块。
            if not await self._send_quiet(umo, chunk):
                logger.warning(
                    f"[dota2] 概览第 {index}/{len(chunks)} 块发送失败：{umo}"
                )
                ok = False
        return ok

    @staticmethod
    def match_headline(
        match: dict,
        heroes: dict[int, dict],
        focus_account_ids: int | list[int] | tuple[int, ...] | None = None,
        parsed: bool = True,
        focus_names: dict[int, str] | None = None,
        *,
        with_parsed_note: bool = True,
    ) -> str:
        """生成比赛概览头部文本。

        ``focus_account_ids`` 可以是一位或多位焦点玩家，多位时逐个列出。

        ``focus_names`` 是 ``{account_id: 昵称}``：焦点玩家**优先显示昵称**而不是
        数字 ID。数据源返回的选手结构里昵称经常缺失（隐私设置会隐藏），此时
        只有调用方手上的绑定/监听记录才有昵称，所以必须由调用方传进来。
        昵称与数据源都没有时退化成 ``账号{id}``——裸数字在推送里没有可读性。

        ``parsed`` 参数**已不参与判定**（保留仅为兼容既有调用方）：完整度文案
        统一由 :func:`parsed_state` 从 ``match`` 推导，确保与 AI 提示词正文
        用的是同一套口径，不会一个说已解析、一个说未解析。

        ``with_parsed_note`` 控制是否输出最后那行「数据完整度」：监听推送的
        赛后短评不依赖录像解析（只要胜负与 K/D/A），标注完整度只会让用户
        误以为数据有问题，因此那边传 ``False``。
        """
        _ = parsed  # 兼容保留：判定改用 parsed_state(match)，见下
        focus_ids = normalize_focus_ids(focus_account_ids)
        radiant_win = match.get("radiant_win")
        # 阵营胜负与「该玩家胜负」是两个东西，措辞必须分开写死：
        # 历史上这里标题写「天辉获胜」、焦点行写「❌负」，模型把两者当成
        # 同一件事，于是把焦点玩家的胜负讲反。
        winner = (
            "天辉阵营获胜"
            if radiant_win is True
            else ("夜魇阵营获胜" if radiant_win is False else "阵营胜负未知")
        )
        lines = [
            f"🏁 比赛回顾 · {match.get('match_id')}",
            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(int(match.get('start_time') or 0)))}"
            f"（{fmt_ago(match.get('start_time'))}）"
            f" · 时长 {fmt_duration(match.get('duration'))}"
            f" · {mode_text(match)}",
            f"天辉 {match.get('radiant_score', 0)} : {match.get('dire_score', 0)} 夜魇"
            f" · {winner}",
        ]

        if focus_ids:
            by_account = {
                int(p["account_id"]): p
                for p in (match.get("players") or [])
                if isinstance(p, dict) and p.get("account_id")
            }
            for index, focus_id in enumerate(focus_ids):
                player = by_account.get(int(focus_id))
                if player is None:
                    continue
                flag = result_text(player)
                # 只有一位焦点玩家时沿用「焦点玩家」的措辞，多位时逐行列出来
                label = "👤 焦点玩家" if len(focus_ids) == 1 else f"👤 焦点玩家 {index + 1}"
                display = (
                    (focus_names or {}).get(int(focus_id))
                    or player.get("name")
                    or player.get("personaname")
                    or f"账号{focus_id}"
                )
                lines.append(
                    f"{label}：{display} · "
                    f"{hname(heroes, player.get('hero_id'))} · "
                    f"{player.get('kills', 0)}/{player.get('deaths', 0)}/{player.get('assists', 0)} "
                    f"该玩家{flag}"
                )

        if with_parsed_note:
            # 与提示词正文共用同一套判据（parsed_state），避免标题与正文打架
            _parsed_flag, parsed_note = parsed_state(match)
            lines.append(f"数据完整度：{parsed_note}")
        return "\n".join(lines)

    # ==================================================================
    # 生命周期
    # ==================================================================
    async def terminate(self):
        """插件被卸载 / 停用时清理后台任务与网络连接。"""
        self._stopping = True
        # 定时任务的 handler 挂在 AstrBot 的调度器上，插件卸载**不会**自动摘掉。
        # 先立个牌子：热重载或停用之后若还有到点的任务被唤醒，handler 一进门
        # 就靠它退出，而不是往一个已经不属于本实例的会话推消息。
        self._sched_stopped = True
        adopt_task = self._sched_adopt_task
        self._sched_adopt_task = None
        if adopt_task is not None and not adopt_task.done():
            adopt_task.cancel()
        task = self._watch_task
        self._watch_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # 「每 N 场总结」的后台任务同样要收掉：它正拿着旧实例的数据层，
        # 而且可能正等着一次十几秒的模型调用。
        summary_tasks = list(self._summary_tasks)
        self._summary_tasks.clear()
        for summary_task in summary_tasks:
            if not summary_task.done():
                summary_task.cancel()
        for summary_task in summary_tasks:
            try:
                await summary_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # 等待解析的后台任务可能挂着十分钟，必须一并取消，
        # 否则卸载后它们还会继续占用连接、并且向已下线的会话推消息。
        parse_tasks = list(self._parse_tasks.values())
        self._parse_tasks.clear()
        for parse_task in parse_tasks:
            if not parse_task.done():
                parse_task.cancel()
        for parse_task in parse_tasks:
            try:
                await parse_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.api.close()
        logger.info("[dota2] 插件已卸载，监听任务与等待解析任务已停止")
