"""话题归属判定：这条消息是不是在找 Dota2 助手。

**只做二分类，不判「不归我的话该归谁」。** 自保方案不需要知道别的插件叫什么，
这是刻意的设计取舍：

* prompt 里不放插件清单 ⇒ 装多少插件都不涨成本、也不用维护名单；
* 不判「归谁」⇒ 少一类失败（目标插件被会话级禁用、清单过期、名字对不上）；
* 失败语义单一：判 NO 就是不接，最坏交给默认大模型，绝不抢。

提示词是**放宽**的（有线索就往 YES 靠）。这一点必须保留：实测把措辞收紧成
「拿不准就 NO」后，模型会整体往 NO 倒，召回从 12/12 掉到 7~8/12 —— 用户会觉得
「说了它不理」。宁可多接一点，「说错了话」比「叫了它不应」更容易被发现和纠正。

代价是缺上下文的句子会被误接（「刚才那个怎么样」），所以在
:func:`build_user_prompt` 里带上上一轮原文 —— 实测能把这两条误接全部消掉。
"""

from __future__ import annotations

#: 助手回复进 prompt 前的截断长度。判断「这个 / 那个」指代什么，前 80 字足够，
#: 而复盘报告动辄两三千字，整段塞进去只会把判定成本翻十倍。
BOT_TURN_LIMIT = 80

#: 高置信关键词：命中就直接判「是我的」，**不用花模型调用**。
#:
#: 这里刻意只放**别的话题基本不会用**的词。「比赛」「英雄」「赛程」这类太泛的词
#: 一律不进：规则层是「命中即接」的短路，误判的代价是把别人的活抢走（11.8k token
#: + 群里抢话），比省下的两百 token 贵得多。宁可让它们去过模型。
HINT_WORDS: tuple[str, ...] = (
    "dota",
    "刀塔",
    "天梯",
    "mmr",
    "补刀",
    "参战",
    "开黑",
    "出装",
    "复盘",
    "上分",
    "战绩",
    "胜率",
    "段位",
    "英雄池",
    "版本榜",
)

#: 系统提示词。能力范围必须写细 —— 只写「Dota2 助手」，模型会漏掉
#: 「钢板最近打得怎么样」这种只有昵称的句子（钢板是玩家昵称，不是游戏术语）。
TOPIC_SYSTEM = """你在判断一句话是不是在找 Dota2 助手。

这个助手的能力范围：Dota2 游戏内容 —— 玩家战绩、某场比赛复盘、英雄和出装、
赛事赛程、上分建议、当前版本的英雄强弱。

判断规则：
1. 句子在讲 Dota2 相关的事就往 YES 靠：对局输赢、某个英雄、某场比赛、
   玩家水平、段位分数、补刀参战这些数据、赛事赛程。
2. 口语和简称也算：玩家昵称（比如「钢板」）、「那把 / 这盘 / 打得好不好」
   这类说法，只要是说游戏里的局，就是 YES。
3. 只有确实在说别的东西（漫画、天气、写代码、别的软件、纯打招呼）才 NO。

只输出 YES 或 NO，不要解释。"""


def truncate(text: str, limit: int = BOT_TURN_LIMIT) -> str:
    """按字符截断，并压掉换行 —— 换行会把 prompt 的结构撑乱。"""
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "…"


def rule_says_mine(text: str) -> bool:
    """规则层：命中高置信词就返回 True。

    **只做正向判定**。返回 False 不代表「不是我的」——它只代表「规则看不出来」，
    必须继续问模型。把规则做成双向判定会引入一类无法排查的漏接。
    """
    low = str(text or "").lower()
    return any(word in low for word in HINT_WORDS)


def build_user_prompt(
    text: str,
    *,
    prev_user: str = "",
    prev_bot: str = "",
) -> str:
    """拼用户侧 prompt。

    带上上一轮原文的理由：同一句「这个怎么下载」，上文是「找到 3 个源」还是
    「今天北京晴」，归属完全不同。实测不带上下文时，这类句子会被误接
    （「刚才那个怎么样」「帮我看看我最近的表现」）；带上之后误接归零。

    Args:
        prev_user: 上一轮**用户说的话**（本插件处理的上一轮，不是群里上一条）。
        prev_bot: 上一轮的助手回复，会截断到 :data:`BOT_TURN_LIMIT`。
    """
    parts: list[str] = []
    prev_user = truncate(prev_user, 120)
    prev_bot = truncate(prev_bot)
    if prev_user or prev_bot:
        parts.append("最近对话：")
        if prev_user:
            parts.append(f"用户：{prev_user}")
        if prev_bot:
            parts.append(f"助手：{prev_bot}")
        parts.append("")
    parts.append(f"现在这句：{text}")
    parts.append("输出：")
    return "\n".join(parts)


def parse_verdict(raw: str) -> bool | None:
    """把模型输出解析成三态：True / False / None（判不出）。

    ``None`` 会由调用方当成「不是我的」处理。这里**必须容错**：模型偶尔会回
    「（无输出）」这类占位符，或者带一堆标点；把占位符当 YES 会让插件凭空接管
    别人的消息。
    """
    text = str(raw or "").strip().upper()
    text = text.lstrip("。.!！?？`\"' *_【[（(")
    if not text:
        return None
    if text.startswith(("YES", "Y", "是", "TRUE", "对")):
        return True
    if text.startswith(("NO", "N", "否", "FALSE", "不")):
        return False
    return None
