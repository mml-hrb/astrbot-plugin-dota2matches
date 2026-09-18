"""把「人话」翻译成插件指令：自然语言意图识别。

设计原则
--------
1. **零成本优先**：绝大多数说法靠规则就能判定，不必请求大模型。
2. **宁缺毋滥**：宁可识别不出来、把消息交给默认大模型闲聊，也不能把闲聊
   误判成数据查询——误判会白白消耗 OpenDota 配额，还会在群里刷屏。
   因此每个意图都要求命中到一定分数才生效，光杆一个弱词不算。
3. **只认明确的实体**：比赛 ID / 账号 ID 用数字特征区分；昵称只在出现
   ``XX 的战绩`` 这类明确句式时才抽取；其余情况一律不指定目标，让插件
   回落到「当前会话的绑定」，这既安全也符合大多数真实用法。

对外只暴露四个入口：

* :func:`parse` —— 规则解析，返回 :class:`Intent` 或 ``None``；
* :func:`normalize` —— 文本归一化（去 @、压空白）；
* :func:`strip_wake_keyword` —— 检测并剥离唤醒词（如「dota2助手」），
  让插件只在被明确点名时才抢答；
* :func:`build_classifier_prompt` / :func:`parse_classifier_reply`
  —— 可选的大模型兜底分类（规则没把握时才用）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable

# ======================================================================
# 意图关键词表
# ======================================================================
# 权重约定：
#   4 = 这个意图独有的强特征动词，出现即基本可以确定
#   3 = 强特征名词
#   2 = 弱特征，需要搭配别的词才够阈值
#   1 = 很常见的词，只做加分，单独出现不构成意图
INTENT_KEYWORDS: dict[str, dict[str, int]] = {
    "help": {
        "帮助": 3,
        "怎么用": 4,
        "使用方法": 4,
        "使用说明": 4,
        "菜单": 3,
        "用法": 3,
        "指令": 2,
        "命令": 2,
        "能干什么": 4,
        "会什么": 4,
        "有什么功能": 4,
        "功能列表": 4,
        "help": 3,
        "不会用": 3,
    },
    "bind": {
        "绑定": 3,
        "绑一下": 4,
        "绑上": 4,
        "绑个": 4,
        "添加账号": 4,
        "登记": 2,
        "bind": 3,
    },
    "unbind": {
        "解绑": 4,
        "取消绑定": 5,
        "解除绑定": 5,
        "删掉绑定": 5,
        "删除绑定": 5,
        "去掉绑定": 5,
        "unbind": 4,
    },
    "my": {
        "我的绑定": 4,
        "我绑的": 4,
        "我绑定的是谁": 5,
        "当前绑定": 4,
        "我绑定的号": 4,
        "绑了谁": 4,
    },
    "bindings": {
        "绑定列表": 5,
        "谁绑定了": 4,
        "有哪些绑定": 4,
        "都绑了谁": 4,
        "绑定了哪些": 4,
    },
    "info": {
        "资料": 3,
        "个人信息": 4,
        "玩家信息": 4,
        "段位": 4,
        "天梯": 4,
        "天梯分": 5,
        "冠绝": 3,
        "mmr": 4,
        "分数": 1,
        "几颗星": 3,
    },
    "heroes": {
        "英雄池": 5,
        "常用英雄": 5,
        "英雄数据": 4,
        "英雄统计": 4,
        "玩什么英雄": 4,
        "擅长英雄": 4,
        "绝活": 4,
        "拿手英雄": 4,
    },
    "wheelchair": {
        "轮椅": 6,
        "版本答案": 6,
        "版本强势": 6,
        "版本之子": 6,
        "强势英雄": 5,
        "版本英雄": 5,
        "胜率最高": 5,
        "胜率榜": 5,
        "胜率排行": 5,
        "英雄胜率": 5,
        "最强英雄": 5,
        "哪个英雄强": 5,
        "什么英雄强": 5,
        "上分英雄": 5,
        "英雄强势": 5,
        "练什么英雄": 5,
        "超模": 5,
        # 「上分」单独给 4 分：真实说法「推荐几个英雄给我上分」里
        # 一个强特征词都没有，但意图很明确。它也会让「上分好难」这类
        # 抱怨命中 —— 回一份版本强势榜并不算答非所问，可以接受。
        "上分": 4,
        "t0": 4,
    },
    "matches": {
        "战绩": 3,
        "战况": 3,
        "比赛记录": 4,
        "对局记录": 4,
        "最近比赛": 3,
        "最近的比赛": 3,
        "最近几场": 3,
        "胜率": 2,
        "赢了几场": 3,
        "连胜": 2,
        "连败": 2,
        "记录": 1,
        "比赛": 1,
    },
    "analyze": {
        "分析": 4,
        "复盘": 4,
        "点评": 4,
        "评价": 2,
        "总结一下": 3,
        "表现": 2,
        "发挥": 2,
        "打法": 4,
        "风格": 2,
        "状态": 2,
        "怎么样": 2,
        "打得如何": 4,
        "水平": 2,
        "问题在哪": 3,
        "该怎么改进": 4,
        "最近": 1,
        "近期": 1,
    },
    "match": {
        "单场": 4,
        "这局": 3,
        "这把": 3,
        "这盘": 3,
        "那局": 3,
        "那把": 3,
        "上一把": 4,
        "上把": 4,
        "上一局": 4,
        "刚才那把": 5,
        "刚才那局": 5,
        "这场比赛": 4,
        "那场比赛": 4,
        "录像": 2,
    },
    "forceparse": {
        "催一下解析": 8,
        "催解析": 8,
        "催一下": 5,
        "申请解析": 8,
        "提交解析": 8,
        "强制解析": 8,
        "让它解析": 5,
        "让 opendota 解析": 8,
        "解析一下这局": 7,
        "解析一下": 4,
        "解析状态": 7,
        "解析好了吗": 8,
        "解析完了吗": 8,
        "解析了没": 7,
        "解析没有": 6,
        "解析完成了吗": 8,
    },
    "watch": {
        "监听": 4,
        "订阅": 4,
        "盯一下": 4,
        "盯着": 4,
        "关注": 2,
        "watch": 3,
    },
    "unwatch": {
        "取消监听": 5,
        "取消订阅": 5,
        "停止监听": 5,
        "别监听": 5,
        "不用监听": 5,
        "关掉监听": 5,
        "移除监听": 5,
        "删掉监听": 5,
    },
    "watchlist": {
        "监听列表": 5,
        "在监听谁": 5,
        "监听了谁": 5,
        "我的监听": 4,
        "订阅列表": 5,
        "都在监听谁": 5,
    },
    "llmtest": {
        "模型测试": 5,
        "模型自检": 5,
        "测试模型": 5,
        "检查模型": 4,
    },
    "datasource": {
        "数据源测试": 5,
        "数据源自检": 5,
        "测试数据源": 5,
        "数据源状态": 5,
        "检查数据源": 4,
        "数据源": 3,
    },
}

#: 否定词：出现时把「绑定 / 监听」翻成对应的取消动作。
#: 只作用于成对出现的意图，避免误伤「战绩」这类无关意图。
NEGATION_RE = re.compile(r"取消|解除|去掉|删掉|删除|移除|停止|关闭|关掉|别再|不再|不用|不要|撤掉")
NEGATION_FLIP = {"bind": "unbind", "watch": "unwatch"}

#: 触发判定所需的最低分数。设成 3 意味着必须命中一个「强特征」词。
MIN_SCORE = 3

#: 「指示插件做事」型意图。它们的关键词很容易被**分析型问句**顺带带出来：
#: 「对比一下目前监听的几个人谁最菜」里那个「监听」只是描述现状，
#: 打分却是 ``watch``=4，于是被当成「添加监听」，答非所问（而且真的
#: 会去改数据）。命中下面的问句特征时，这些意图一律让位给闲聊兜底。
ANALYSIS_GATED_INTENTS = frozenset(
    {"watch", "unwatch", "bind", "unbind", "watchlist", "my", "bindings"}
)

#: 「复合问题」判定用的**功能族**。一句话里命中两个以上不同族 = 用户一次
#: 要两件事，例如「钢板最近打得怎么样，顺便推荐几个轮椅」（个人战绩 + 版本榜）、
#: 「看看他的资料和英雄池」（两个不同的数据源）。规则打分只能挑一个交出去，
#: 挑哪个都漏答一半，因此返回 ``None`` 交给带工具的兜底对话逐项查。
#:
#: 分族而不是直接数意图个数，是为了**不误伤同义说法**：
#: ``analyze``（分析近期）与 ``matches``（看战绩）本来就是同一件事的两种问法，
#: 「我最近发挥怎么样，分析一下」同时踩中两个词是常态，不该被当成复合问题。
#: 同理 ``heroes`` / ``info`` 都属于「个人面板数据」，一起出现很自然。
COMPOSITE_INTENT_GROUPS = (
    frozenset({"wheelchair"}),
    frozenset({"analyze", "matches"}),
    frozenset({"heroes", "info"}),
)

#: 分析型问句特征：要的是**结论 / 对比 / 建议**，不是一条指令。
#: 刻意写得很窄（带上具体搭配），避免误伤「我该怎么改进」这类
#: 本来就该走 ``analyze`` 指令的说法。
ANALYSIS_QUESTION_RE = re.compile(
    r"谁(最|更|比|厉|强|菜|牛|秀|坑)"
    r"|最(菜|强|弱|厉害|牛|秀|坑|差|水)"
    r"|对比|比较|排名|排行"
    r"|该(怎么|如何)(练|提升|进步|上分|补|学|改)"
    r"|怎么(练|提升|进步|上分)"
    r"|推荐|值不值"
)

#: **指代某一局**的说法：「这一盘」「那把」「上一局」「刚才那场」「最后一把」。
#: 出现这些词时，用户要的是**单场复盘**（``match``），即使他同时说了
#: 「分析」——「详细分析这一盘」说的是一局，不是近期总结。
#: 之前没有这条，``analyze`` 靠「分析」拿 4 分压过 ``match`` 的「这盘」3 分，
#: 于是「详细分析这一盘」被翻译成「分析近期 1 场表现」，答非所问。
DEICTIC_RE = re.compile(
    r"(?:这|那|刚(?:才)?|上(?:一|个)?|最后|最近(?:的)?)\s*(?:一\s*)?(?:场|把|局|盘)"
)

#: **聚合 / 总结**说法：要的是「最近这段时间打得怎么样」，不是某一局。
#: 与 :data:`DEICTIC_RE` 同时出现时以它为准（「最近十场里我上一把」仍算单场，
#: 但「最近的表现」不是）。
AGGREGATE_RE = re.compile(
    r"近期|最近\s*[0-9十]+\s*(?:场|把|局|盘)|平均|场均|总体|整体|走势|起伏|"
    r"表现|状态|发挥|水平|打法|风格|习惯|胜率|命中率|总结|总结下|"
    r"问题在哪|该怎么改进|改进|提升|上分|练"
)

#: 明确表示「要看一下某一局」的动作词。只有指代、**没有动作词**的话多半是
#: 在吐槽（「刚才那把真是气死我了」），不该被当成复盘请求。
SINGLE_MATCH_ACTION_RE = re.compile(
    r"分析|复盘|点评|评价|看看|看下|看一下|查|详细|具体|说说|讲讲|总结|回顾|拉一下|来一下"
    r"|怎么样|如何|为啥|为什么|咋|什么情况"
)


def looks_like_single_match(text: str) -> bool:
    """这句话是不是在指**某一局**（而不是近期总结）。

    「详细分析这一盘」「复盘一下刚才那把」→ True；
    「分析一下我最近的表现」「最近 20 场胜率多少」→ False。
    """
    src = text or ""
    return bool(DEICTIC_RE.search(src)) and not AGGREGATE_RE.search(src)


def wants_single_match(text: str) -> bool:
    """用户是不是在**要求看**某一局（而不是随口提到）。

    比 :func:`looks_like_single_match` 多一道动作词校验，用于「规则完全没
    识别出意图」时决定要不要按单场复盘处理。
    """
    return looks_like_single_match(text) and bool(
        SINGLE_MATCH_ACTION_RE.search(text or "")
    )

#: 意图 → 是否需要「目标玩家」参数
#:
#: 这里也包含 ``match``：它要的是比赛 ID（不是玩家），但缺参数时同样需要
#: 提示用户怎么补。**早期漏了它**，于是「复盘一下」（没给 ID）会一路走到
#: ``d2_match`` 被当成「用法不对」弹一整段命令帮助，而不是问一句「哪一盘」。
INTENT_NEEDS_TARGET = {
    "bind",
    "unbind",
    "info",
    "heroes",
    "matches",
    "analyze",
    "match",
    "watch",
    "unwatch",
    "forceparse",
}

#: 意图 → 人类可读的说明（给大模型兜底分类用，也用于日志）
INTENT_LABELS: dict[str, str] = {
    "help": "查看插件使用说明",
    "bind": "绑定一个 Dota2 账号（需要昵称或账号 ID）",
    "unbind": "解除当前会话的绑定",
    "my": "查看我当前绑定的是哪个账号",
    "bindings": "查看本会话所有人的绑定",
    "info": "查询玩家资料（段位 / 天梯分等）",
    "heroes": "查询玩家英雄池 / 常用英雄",
    "wheelchair": "查看当前版本胜率最高的英雄（玩家口中的「轮椅」），带位置词或「我」时会结合该玩家的英雄池做推荐",
    "matches": "查询最近战绩列表",
    "analyze": "用 AI 分析近期表现与打法风格（需要指定场次时给出场次）",
    "match": "深度复盘某一场比赛（需要比赛 ID）",
    "forceparse": "催 OpenDota 解析某一场比赛 / 查这局解析好了没（需要比赛 ID）",
    "watch": "监听玩家，比赛结束后自动推送分析（需要昵称或账号 ID）",
    "unwatch": "取消监听（需要昵称、账号 ID，或「全部」）",
    "watchlist": "查看本会话的监听列表",
    "llmtest": "测试插件专用的大模型 API Key 是否配置正确（连通性自检）",
    "datasource": "检查主/后备数据源（STRATZ / OpenDota）的连通性与降级状态",
}


@dataclass
class Intent:
    """一次识别结果。"""

    name: str
    args: str = ""
    score: int = 0
    via: str = "rule"  # rule | llm
    detail: str = ""

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        return f"<Intent {self.name} args={self.args!r} score={self.score} via={self.via}>"


# ======================================================================
# 实体抽取
# ======================================================================

#: Dota2 比赛 ID 目前是 10 位、约 8.9e9；account_id 上限是 2^32-1（约 4.3e9）。
#: 超过 5e9 一定不是 account_id。
ACCOUNT_ID_MAX = 4_294_967_295
MATCH_ID_FLOOR = 5_000_000_000

#: 出现这些词时，长数字更可能是比赛 ID 而不是账号 ID
MATCH_CTX_RE = re.compile(r"这局|这把|这盘|那局|那把|这场|那场|单场|比赛|复盘|录像|局|把")

NUM_RE = re.compile(r"(?<!\d)(\d{6,20})(?!\d)")
STEAM64_RE = re.compile(r"(?<!\d)(7656\d{13})(?!\d)")

#: 场次：「最近 20 场」「看 10 把」「三十场」
COUNT_RE = re.compile(
    r"(?:最近|近|看|查|分析|拉|给|取|来|最近)?\s*"
    r"(\d{1,3}|[一二两三四五六七八九十]{1,3})\s*(?:场|把|局|盘|条)"
)

#: 场次描述（「最近 20 把」「近 10 场」）出现在目标片段里一定是误抓 ——
#: 昵称不会长成「20把」。清洗目标时直接把这部分剃掉，避免把场次当昵称
#: 去搜索（搜不到还会白耗一次接口调用）。
COUNT_TOKEN_RE = re.compile(r"(?:最近|近|前)?\s*\d{1,3}\s*(?:场|把|局|盘|条)")
CN_NUM = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

#: 目标抽取时的前置动词（吃掉它们，剩下的才是昵称）。
#:
#: 注意：正则的备选是**先匹配先赢**，所以长词必须排在短词前面。早期把
#: ``分析`` 排在 ``分析一下`` 前面，导致「分析一下天鸽的打法」只被吃掉
#: 「分析」，剩下「一下天鸽的打法」，「一下」于是被当成昵称的一部分——
#: 这类错位很难从代码看出来，只能靠用例兜住。
TARGET_PREFIX_RE = re.compile(
    r"^\s*(?:帮我|给我|我想|我想要|麻烦|请|能不能|可以)?\s*"
    r"(?:分析一下|分析下|分析|"
    r"查一下|查查|查下|查询|查|"
    r"看一下|看一看|看看|看下|看|"
    r"复盘一下|复盘下|复盘|"
    r"总结一下|总结下|总结|统计一下|统计下|统计|"
    r"搜一下|搜搜|搜下|搜索|搜|"
    r"来一下|来个|整个|搞个|发一下|发|给个|拉一下|拉)?\s*"
)

#: 「XX 的战绩 / 表现 / 英雄池…」句式
TARGET_OF_RE = re.compile(
    r"([^\s，。!！?？、,]{1,28}?)\s*(?:的|de)?\s*"
    r"(?:战绩|战况|比赛记录|对局记录|数据|资料|个人信息|玩家信息|英雄池|英雄数据|"
    r"常用英雄|表现|发挥|状态|水平|情况|信息|近期|最近|段位|天梯|"
    r"打法|打法风格|风格|特点)"
)

#: 「绑定 XX」「监听 XX」这类后面直接跟目标的句式
TARGET_AFTER_RE = re.compile(
    r"(?:取消掉|解除掉|删掉|移除掉|取消|解除|移除|删掉|绑定|绑|解绑|"
    r"取消绑定|解除绑定|监听|订阅|盯|看着|停止监听|取消监听|移除监听|"
    r"删掉监听|关注|加入|添加|加)\s*(?:一下|着|住|上|到|进来|入|了)?\s*"
    r"([\s\S]{1,32}?)\s*(?:吧|呢|啊|吗|了|一下|好吧)?\s*$"
)

#: 目标片段里出现这些词，说明抓到的是动作 / 修饰而不是昵称。
#: 注意：不要放「天鸽」这种真昵称；只放纯语气与动作残留。
TARGET_ACTION_RE = re.compile(
    r"取消|解除|移除|删掉|删除|停止|关闭|加入|添加|进来|起来|通知|推送|提醒|"
    r"怎么|如何|^[的]|^[着住上]"
)

#: 「XX 对吧」这类尾巴：剥离后若只剩半截语气词，就整段丢弃。
#: 例如「你刚才确实说了【钢板】对」——多剪掉一个字反而能对上真昵称，
#: 这种「剪残了」的片段不能拿去搜索。
DANGLING_TAIL_RE = re.compile(r"(?:对|是|好|行|可以|能|要|会|想|说|看|得|过)$")

#: 目标藏在动词「前面」的句式，例如「把天鸽加入监听」「给天鸽加个监听」。
#: 中文里「把 / 给 / 帮 + 目标 + 动作」极常见，必须单独处理。
TARGET_BEFORE_RE = re.compile(
    r"(?:把|给|帮|让|对)\s*([^\s，。!！?？、,]{1,28}?)\s*"
    r"(?:加入|加进|加个|加上|添加|加到|放进|设成|设置成|"
    r"取消|解除|移除|删掉|删除|停止|关闭|绑定|解绑|监听|订阅|"
    r"查|查询|看看|看一下|看|分析|复盘|统计|总结)"
)

#: 目标里需要剔除的人称 / 修饰残留
TARGET_STRIP_HEAD_RE = re.compile(
    r"^(?:我的|你的|他的|她的|我们的|你们的|我|你|他|她|它|咱|我们|你们|他们|"
    r"一下|着|住|上|帮我|给我|麻烦|请|能不能|可以|查查|查一下|查下|查|看看|"
    r"看下|看一下|看一看|看|搜搜|搜一下|搜索|来个|来一下|整个|搞个|统计下|"
    r"统计|总结下|总结|监听|订阅|绑定|关注)+"
)
TARGET_STRIP_TAIL_RE = re.compile(
    # 注意：多字语气词必须排在单字前面。「对吧」若被「吧」先吃掉，
    # 会留下一个半截的「对」被当成昵称。
    r"(?:最近|近期|怎么样|多少|如何|是啥|是什么|对吧|是的|好吗|行吗|有没有|"
    r"取消掉|解除掉|移除掉|删掉|取消|解除|移除|删除|停止|关闭|"
    r"几条|几场|几把|这|那|一下|吧|呢|啊|吗|了)+$"
)

#: 句子尾部常见的语气 / 疑问残留，用于「英雄池怎么样」这类无目标句式。
#: 多字词必须排在单字前面——否则「对吧」会被单字「吧」先吃掉，剩下一个
#: 半截的「对」被当成昵称。
TRAILING_QUESTION_RE = re.compile(
    r"\s*(?:怎么样|如何|多少|是啥|是什么|有没有|可以不|行吗|好吗|对吧|是的|"
    r"了没|呢|吧|啊|呀|吗|了|没)\s*$"
)

#: 命中这些词的片段不可能包含昵称，直接判定为「没有指定目标」
TARGET_BAD_SUBSTR = re.compile(
    r"怎么样|如何|多少|是啥|是什么|有比赛|推送|通知|提醒|帮我|给我|看看|查查|"
    r"资料|个人信息|玩家信息|段位|天梯|英雄池|英雄数据|常用英雄|战绩|战况|"
    r"比赛记录|对局记录|表现|发挥|状态|水平|胜率|总结|打法|我的|你的"
)

#: 看起来像昵称的白名单校验：至少含一个中文 / 字母 / 数字
TARGET_VALID_RE = re.compile(r"[\w\u4e00-\u9fff]")

#: 不可能是昵称的残留虚词
TARGET_STOPWORDS = {
    "", "我", "你", "他", "她", "它", "自己", "本人", "最近", "近期", "一下",
    "看看", "查查", "帮我", "给我", "大家", "有人", "谁", "怎么", "什么",
}

#: 整句就是「功能名 + 疑问」时，说明用户没指定具体玩家。
#: 例如「英雄池怎么样」「段位多少」「我的战绩」——此时目标留空，
#: 由插件回落到当前会话的绑定。
NO_TARGET_PHRASES = {
    "英雄池", "英雄池怎么样", "常用英雄", "英雄数据", "我的英雄池", "我的英雄",
    "段位", "段位多少", "天梯", "天梯分", "天梯分多少", "我的段位", "我的天梯分",
    "资料", "个人信息", "我的资料", "玩家信息",
    "战绩", "我的战绩", "最近战绩", "最近比赛", "比赛记录", "对局记录", "比赛",
    "表现", "我的表现", "状态", "我的状态", "打法", "我的打法", "水平",
    "数据", "我的数据", "胜率", "我的胜率",
    "总结", "分析", "复盘", "点评",
}

#: 「XX 的 资料 / 战绩 / 英雄池」里的名词修饰符。
#: 抽取目标时若片段末尾剩下这些词，说明前面的部分才是真昵称。
NOMINAL_HEAD_WORDS = (
    "战绩", "战况", "比赛记录", "对局记录", "资料", "个人信息", "玩家信息",
    "英雄池", "英雄数据", "常用英雄", "表现", "发挥", "状态", "水平",
    "数据", "信息", "情况", "段位", "天梯", "胜率", "单场", "复盘",
    "打法", "打法风格", "风格", "特点",
)


def _cn_to_int(token: str) -> int | None:
    """把「二十」「十五」这类中文数字转成 int；失败返回 None。"""
    token = token.strip()
    if token.isdigit():
        return int(token)
    if token in CN_NUM:
        return CN_NUM[token]
    if len(token) == 2 and token[0] in CN_NUM and token[-1] == "十":
        return CN_NUM[token[0]] * 10
    if len(token) == 2 and token[0] == "十" and token[1] in CN_NUM:
        return 10 + CN_NUM[token[1]]
    if len(token) == 3 and token[0] in CN_NUM and token[1] == "十" and token[2] in CN_NUM:
        return CN_NUM[token[0]] * 10 + CN_NUM[token[2]]
    return None


def extract_match_id(text: str) -> int | None:
    """从文本里认出比赛 ID。

    规则：
    * 17 位且以 7656 开头 → SteamID64，不是比赛 ID；
    * 数值 >= 50 亿 → 一定超过 account_id 上限，必是比赛 ID；
    * 10 亿 ~ 50 亿之间 → 需要出现「这局 / 这场 / 比赛」这类语境才认定为比赛 ID。
    """
    for raw in NUM_RE.findall(text):
        value = int(raw)
        if STEAM64_RE.fullmatch(raw):
            continue
        if value >= MATCH_ID_FLOOR:
            return value
        if value >= 1_000_000_000 and MATCH_CTX_RE.search(text):
            return value
    return None


def extract_account_id(text: str) -> int | None:
    """从文本里认出 32 位 account_id（不是比赛 ID、不是 SteamID64）。"""
    for raw in NUM_RE.findall(text):
        value = int(raw)
        if STEAM64_RE.fullmatch(raw):
            continue
        if value <= ACCOUNT_ID_MAX:
            return value
    return None


def extract_steam_id64(text: str) -> str | None:
    match = STEAM64_RE.search(text)
    return match.group(1) if match else None


def extract_count(text: str) -> int | None:
    """抽取「最近 20 场」里的场次。"""
    for raw in COUNT_RE.findall(text):
        value = _cn_to_int(raw)
        if value:
            return value
    return None


def _clean_target(raw: str) -> str:
    """清洗抽取到的目标片段。

    除了去人称、去语气词，还会做一次「自检」：片段里若还残留
    「怎么样 / 有比赛 / 推送」这类明显不是昵称的成分，就整段丢弃——
    宁可让插件回落到当前会话的绑定，也不要拿一串虚词去搜玩家。
    """
    target = (raw or "").strip()
    target = TARGET_STRIP_HEAD_RE.sub("", target)
    # 顺手剃掉「最近20把」这类场次描述：它绝不会是昵称的一部分
    target = COUNT_TOKEN_RE.sub("", target).strip(" \t的了地得,，。.、!！?？~～")
    target = TRAILING_QUESTION_RE.sub("", target)
    target = target.strip(" \t的了地得,，。.、!！?？~～")
    # 「天鸽的战绩」→ 剪掉尾巴上的名词修饰，并重做一次人称/语气清洗
    for word in NOMINAL_HEAD_WORDS:
        if target.endswith(word) and len(target) > len(word):
            target = target[: -len(word)].strip("的了")
            break
    target = TARGET_STRIP_TAIL_RE.sub("", target)
    target = target.strip(" \t的了地得,，。.、!！?？~～")
    # 句尾被剪掉半个语气词（例如「…对吧」）时，回退到括号前的最后一个
    # 完整片段，避免把「你刚才确实说了【钢板】对」这种半截话当昵称。
    if "【" in target and "】" not in target:
        target = target[: target.rindex("【")].strip()
    target = TARGET_STRIP_HEAD_RE.sub("", target)
    target = target.strip(" \t的了地得,，。.、!！?？~～")
    # 剪到最后剩一个孤零零的语气词，说明这片段已经剪残了，直接放弃
    if len(target) <= 2 and DANGLING_TAIL_RE.search(target):
        return ""
    if target in TARGET_STOPWORDS or not TARGET_VALID_RE.search(target):
        return ""
    if TARGET_BAD_SUBSTR.search(target) or TARGET_ACTION_RE.search(target):
        return ""
    if len(target) > 28:
        return ""
    return target


def extract_target(text: str, intent: str) -> str:
    """按意图抽取目标玩家（昵称 / 账号 ID / SteamID64）。

    抽取不出来时返回空串——宁可让插件回落到当前会话的绑定，也不要拿
    一堆虚词去 OpenDota 搜索。
    """
    # 数字 ID 最可靠，优先
    steam64 = extract_steam_id64(text)
    if steam64:
        return steam64
    if intent in {"bind", "unbind", "watch", "unwatch"}:
        account_id = extract_account_id(text)
        if account_id:
            return str(account_id)

    # 「绑定 / 监听」类：先剥掉动作词，再看剩下的片段像不像昵称。
    # 直接正则会连「加入监听」里的「监听」一起抓到，很脏。
    if intent in {"bind", "unbind", "watch", "unwatch"}:
        # 先试「把天鸽加入监听」这类目标在动词前面的句式
        before = TARGET_BEFORE_RE.search(text)
        if before:
            cleaned = _clean_target(before.group(1))
            if cleaned:
                return cleaned
        after = TARGET_AFTER_RE.search(text)
        if after:
            cleaned = _clean_target(after.group(1))
            # 反复去掉尾部的动作词：「取消监听天鸽」抽出来是「监听天鸽」，
            # 「把天鸽加入监听」是「监听」——都要剥干净才剩昵称。
            for _ in range(3):
                stripped = re.sub(
                    r"(?:监听|订阅|绑定|关注)$", "", cleaned
                ).strip()
                if stripped == cleaned:
                    break
                cleaned = stripped
            cleaned = re.sub(r"^[个进到加的着住上]+", "", cleaned).strip()
            if re.fullmatch(r"全部(?:的)?(?:监听|订阅)?", cleaned):
                return "全部"
            if cleaned and cleaned not in {"监听", "订阅", "绑定", "关注"}:
                return cleaned

    # 「XX 的战绩」句式
    stripped = TARGET_PREFIX_RE.sub("", text)
    found = TARGET_OF_RE.search(stripped)
    if found:
        cleaned = _clean_target(found.group(1))
        if cleaned:
            return cleaned

    # 「英雄池怎么样」「段位多少」这类：整句去掉疑问尾巴后，若刚好等于
    # 纯功能名词，说明用户没提具体是谁 → 交给当前会话的绑定
    bare = TRAILING_QUESTION_RE.sub("", text).strip("的了地得,，。.、!！?？ ")
    bare = TARGET_PREFIX_RE.sub("", bare).strip()
    if bare in NO_TARGET_PHRASES:
        return ""

    # 兜底：整句去掉动词后剩下的短片段（仅在句子很短时采用，避免误伤）
    rest = stripped.strip()
    if 0 < len(rest) <= 16 and intent in {"info", "heroes"}:
        cleaned = _clean_target(rest)
        if cleaned:
            return cleaned
    return ""


# ======================================================================
# 规则解析
# ======================================================================

#: 归一化：去掉 @ 提及、首尾空白、连续空格
AT_RE = re.compile(r"@\S{1,20}")
SPACE_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """归一化输入文本。"""
    if not text:
        return ""
    text = AT_RE.sub(" ", text)
    text = SPACE_RE.sub(" ", text)
    return text.strip()


# ======================================================================
# 唤醒词（触发关键词）
# ======================================================================
#: 唤醒词两侧需要一并清掉的**软分隔符**：空白，以及中文「。！~」这类
#: 既可能是标点、也可能是别的插件命令前缀的字符。
#: 刻意**不含** ``/`` ``!`` ``#`` —— 那些一旦被吃掉，就会绕过上层
#: 「不截胡指令」的判断（见 :data:`NLU_STRIP_CMD_PREFIXES`）。
WAKE_TRIM_CHARS = " \t\r\n，,。.、:：;；！!~～-—+*|"

#: 剥离唤醒词后**仍然**以这些字符开头的，视为「给别的插件 / 本插件指令的
#: 消息」，不截胡。只保留语义明确、基本不会被当标点用的几个前缀
#: （``！`` ``。`` ``~`` 已归入上面的软分隔符，不在此列）。
NLU_STRIP_CMD_PREFIXES = ("/", "／", "#")

#: 关键词正则缓存。编译一次即可复用，避免每来一条消息都重新编译。
#:
#: 连写关键词时允许插入空白的位置仅限英文/数字与中日韩字符的交界处：
#: 覆盖「dota2 助手」这类中间多打一个空格的手滑，又不至于把关键词拆成
#: 一堆单字匹配（那样任何含这些字的句子都会命中，闸门就失效了）。
_WAKE_CACHE: dict[str, re.Pattern[str]] = {}


def _wake_pattern(keyword: str) -> re.Pattern[str]:
    """把唤醒词编译成正则：忽略大小写，且允许中英文交界处多打空格。"""
    cached = _WAKE_CACHE.get(keyword)
    if cached is not None:
        return cached
    parts: list[str] = []
    prev = ""
    for ch in keyword:
        if ch.isspace():
            parts.append(r"\s*")
            prev = ""
            continue
        is_cjk = not ch.isascii()
        prev_is_cjk = bool(prev) and not prev.isascii()
        if prev and is_cjk != prev_is_cjk:
            parts.append(r"\s*")
        parts.append(re.escape(ch))
        prev = ch
    pattern = re.compile("".join(parts), re.IGNORECASE) if parts else re.compile(r"(?!)")
    _WAKE_CACHE[keyword] = pattern
    return pattern


def strip_wake_keyword(text: str, keyword: str) -> tuple[bool, str]:
    """检测并剥离唤醒词。

    返回 ``(是否命中, 剥离后的正文)``。关键词为空时视为「未启用唤醒词」，
    直接返回 ``(False, 原文)``，这样配置里留空也不会把功能整个堵死。

    会一并清掉关键词两侧残留的标点与空白，例如
    ``「dota2助手，帮我看看战绩」`` → ``(True, "帮我看看战绩")``。
    """
    src = text or ""
    kw = (keyword or "").strip()
    if not kw or not src:
        return False, src
    pattern = _wake_pattern(kw)
    if not pattern.search(src):
        return False, src
    rest = pattern.sub(" ", src)
    # 关键词在句首/句尾时，剥离后常留下一个连接用的标点，一并去掉
    rest = rest.strip(WAKE_TRIM_CHARS)
    return True, rest


def _score_all(text: str) -> dict[str, int]:
    lowered = text.lower()
    scores: dict[str, int] = {}
    for intent, keywords in INTENT_KEYWORDS.items():
        total = 0
        for word, weight in keywords.items():
            # 英文关键词按全词匹配，中文直接子串匹配
            if re.fullmatch(r"[a-z]+", word):
                if re.search(rf"(?<![a-z]){word}(?![a-z])", lowered):
                    total += weight
            elif word in text:
                total += weight
        if total:
            scores[intent] = total
    return scores


def parse(text: str) -> Intent | None:
    """把一句自然语言解析成插件意图；识别不出来返回 ``None``。"""
    raw = normalize(text)
    if not raw:
        return None

    lowered = raw.lower()
    scores = _score_all(raw)

    # 比赛 ID 是极强的单场复盘信号
    match_id = extract_match_id(raw)
    if match_id:
        scores["match"] = scores.get("match", 0) + 5

    if not scores:
        # 一个关键词都没踩中，但「帮我看看最后一把」这种说法确实是要看某一局。
        # 只在**有明确动作词**时才认（否则「刚才那把真是气死我了」也会被
        # 当成复盘请求）。
        if wants_single_match(raw) and not ANALYSIS_QUESTION_RE.search(raw):
            return _build_intent("match", raw, 0, match_id, False)
        return None

    # 「催解析 / 解析好了吗」这类问法里通常也带比赛 ID，而 ID 本身会给
    # ``match`` 加权，很容易把 forceparse 挤掉。只要 forceparse 已经踩到
    # 阈值（说明出现了「催解析」「申请解析」「解析好了吗」这类独有的说法），
    # 就让它优先——用户的意图明显是问解析状态，而不是要一份复盘。
    if scores.get("forceparse", 0) >= MIN_SCORE and scores.get(
        "forceparse", 0
    ) >= scores.get("match", 0) - 5:
        best = "forceparse"
        return _build_intent(best, raw, scores[best], match_id, False)

    if not scores:
        # 一个关键词都没踩中，但「帮我看看最后一把」这种说法确实是要看某一局。
        # 只在**有明确动作词**时才认（否则「刚才那把真是气死我了」也会被
        # 当成复盘请求），并且必须先过分析型问句闸门。
        if wants_single_match(raw) and not ANALYSIS_QUESTION_RE.search(raw):
            return _build_intent("match", raw, 0, match_id, False)
        return None

    best = max(scores, key=lambda name: (scores[name], name))
    if scores[best] < MIN_SCORE:
        if wants_single_match(raw) and not ANALYSIS_QUESTION_RE.search(raw):
            return _build_intent("match", raw, scores.get("match", 0), match_id, False)
        return None
    best_score = scores[best]

    # 复合问题：一句话里同时要**两件不同的事**（命中两个以上功能族）。
    #
    # 「钢板最近打得怎么样，顺便推荐几个轮椅」既要个人战绩、又要版本推荐；
    # 「看看天鸽的英雄池，再讲讲版本什么英雄强」同理。规则打分只能挑一个
    # 分高的交出去 —— 无论挑哪个都漏答一半，而且用户完全看不出是漏了。
    # 返回 None 会把消息交给兜底对话，那边有工具，可以逐项查完再一起回答。
    strong = {name for name, score in scores.items() if score >= MIN_SCORE}
    if sum(1 for group in COMPOSITE_INTENT_GROUPS if strong & group) >= 2:
        return None

    # 「刚才那把…」「这把…」这种只有指代、没有任何「要看」的动作词，
    # 多半是在吐槽（「刚才那把真是气死我了」）而不是要复盘 —— 别自作主张
    # 去问比赛 ID，交给闲聊兜底更合适。
    if (
        best == "match"
        and not match_id
        and DEICTIC_RE.search(raw)
        and not wants_single_match(raw)
    ):
        return None

    # 分析型问句让位给闲聊兜底。
    #
    # 「对比一下目前监听的几个人谁最菜」这类问题里，「监听」是在描述现状，
    # 不是在要求插件添加监听 —— 但打分只看关键词，``watch`` 照样拿 4 分。
    # 与其把它执行成一个**会改数据的动作**（真的往监听列表里加人），
    # 不如交回给上层：插件的闲聊兜底会带着监听名单与各人战绩来回答，
    # 这正是用户想要的。注意 `analyze` / `matches` / `heroes` 这些
    # **只读查询**不在闸门里，被误判也只是多给一份数据，代价可控。
    if best in ANALYSIS_GATED_INTENTS and ANALYSIS_QUESTION_RE.search(raw):
        return None

    # 「详细分析这一盘」这类：**分析**的是某一局，不是近期总结。
    # 关键词打分只看词频（分析=4 > 这盘=3），必须靠指代特征纠偏，
    # 否则用户会得到一份「近 1 场表现分析」，而不是他想要的复盘。
    if best == "analyze" and looks_like_single_match(raw):
        best = "match"
        best_score = max(best_score, scores.get("match", 0))

    # 否定词把「绑定 / 监听」翻成取消动作
    flipped = False
    if best in NEGATION_FLIP and NEGATION_RE.search(raw):
        best = NEGATION_FLIP[best]
        flipped = True
        # 翻转后的意图自己也有关键词得分（例如「取消监听」里的「取消监听」）
        best_score = max(best_score, scores.get(best, 0))

    return _build_intent(best, raw, best_score, match_id, flipped)


#: 「查版本强势英雄」意图里的位置词。
#:
#: handler 侧（``dota_format.match_position``）还会再解析一次，这里留一份是
#: 为了**别把它当成玩家昵称** —— 「这版本辅助哪个是轮椅」里根本没有昵称，
#: 但目标抽取很容易把「辅助」抓成一个人。
WHEELCHAIR_POSITION_WORDS = (
    "核心", "大哥", "1号位", "一号位", "2号位", "二号位", "中单", "carry", "c位",
    "三号位", "3号位", "劣单", "上单", "offlane", "前排", "肉盾",
    "辅助", "酱油", "4号位", "四号位", "5号位", "五号位", "support", "挂件",
)


#: 「XX 的轮椅」句式：抽出「的」前面的昵称。
#:
#: 不能直接复用 :data:`TARGET_OF_RE` —— 那张名词表里根本没有「轮椅」，
#: 「天鸽的轮椅」会被整句跳过，人就这么丢了。
WHEELCHAIR_OWNER_RE = re.compile(
    r"([^\s，。!！?？、,]{1,20}?)\s*(?:的|de)\s*"
    r"(?:轮椅|版本答案|版本强势|版本英雄|强势英雄|"
    r"胜率榜|胜率排行|上分英雄|最强英雄|t0)"
)

#: 出现在「的」前面、看着像昵称其实不是的版本 / 时间修饰。
#: 少了这张表，「这版本的轮椅」会拿「这版本」去 OpenDota 搜一趟账号。
WHEELCHAIR_SKIP_TARGETS = frozenset({
    "这版本", "这个版本", "当前版本", "现在版本", "目前版本", "本版本",
    "新版本", "版本", "这赛季", "本赛季", "这个赛季", "新赛季",
    "现在", "目前", "今天", "明天", "昨天", "最近", "近期", "这", "那",
})

#: 出现这些词就说明是在给自己查（而不是给别人）
WHEELCHAIR_SELF_WORDS = ("我", "自己", "本人", "咱", "俺")

#: 简称反查的护栏：这些片段在原句里到处都是，绝不能当成昵称去搜账号。
#: 少了它，「这版本哪个英雄是轮椅」里的「英雄」会去撞某个叫
#: 「英雄联盟战神」的昵称 —— 白白出一份别人的个人推荐。
WHEELCHAIR_FILLER_WORDS = frozenset({
    "版本", "轮椅", "英雄", "强势", "胜率", "最高", "排行", "上分", "推荐",
    "适合", "什么", "哪个", "哪些", "现在", "目前", "这版", "当前", "最强",
    "答案", "怎么", "如何", "帮我", "看看", "一下", "厉害", "好用", "打钱",
    "核心", "辅助", "中单", "大哥", "酱油", "前排", "肉盾", "三号", "号位",
})


#: 简称反查时单个片段的最大长度。名字再长也只会按这个窗口滑 ——
#: 枚举是 O(长度² × 人数)，原句里一个没标点的长句能有几十字。
MAX_PIECE_LEN = 12


def match_session_name(value: str, names: Iterable[str]) -> str:
    """在本会话名单里认人：**精确优先，其次简称反查**。

    这是「这句话里说的是谁」的唯一权威实现 —— 轮椅参数构造（
    :func:`build_wheelchair_args`）与兜底对话的工具取数
    （``dota_tools`` 的 ``player`` 参数）都走它。两份实现一定会漂移，
    到时候同一个昵称在指令里认得出、在工具里认不出，很难查。

    返回命中的**名单原名**（不是片段）：调用方多半要拿它去查 account_id，
    用原名查最省事。认不出来返回空串 —— **绝不猜**。

    Args:
        value: 用户原话（或模型给的参数）。
        names: 本会话名单（已绑定 / 已监听玩家的 ``personaname``）。
    """
    text = str(value or "")
    if not text.strip():
        return ""
    lowered = text.lower()
    pool = [str(item).strip() for item in (names or ()) if str(item).strip()]
    # 1) 名单里的全名**原样出现在原句里**，长名优先（「钢板不锈」先于「钢板」）
    for name in sorted(set(pool), key=len, reverse=True):
        if len(name) >= 2 and name.lower() in lowered:
            return name
    # 2) 反过来查：群里有事只会喊简称，而名单存的是完整 Steam 昵称。
    #    命中片段后再映射回名单原名。
    piece = _wheelchair_pool_hit(text, tuple(pool))
    if not piece:
        return ""
    piece_low = piece.lower()
    for name in sorted(set(pool), key=len):
        if piece_low in name.lower():
            return name
    return piece


def _wheelchair_pool_hit(value: str, names: tuple[str, ...]) -> str:
    """名单反查：Steam 昵称常带一堆前后缀，而用户张口只会喊简称。

    真实名单里就躺着「你刚才确实说了【钢板】对吧」这种名字 —— 群里喊的是
    「钢板」。正着匹配（名字 in 原句）永远对不上，于是反过来做：把原句切成
    2~4 字的片段，看哪一段落在某个名单名字里。

    三条护栏缺一不可：

    * 片段不能是功能词（见 :data:`WHEELCHAIR_FILLER_WORDS`）与位置词；
    * 长的片段优先 —— 「钢板不锈」比「钢板」更具体；
    * 同一长度的片段若撞上**两个不同的人**，整段放弃：这种时候猜谁都是错。

    返回命中的片段本身（不是名单里的全名）：用户喊的就是这个名字，
    handler 拿它去 OpenDota 搜反而更容易命中那个账号。
    """
    lowered = [str(item).strip().lower() for item in (names or ())]
    lowered = [name for name in lowered if len(name) >= 2]
    if not lowered:
        return ""

    candidates: dict[str, set[str]] = {}
    for chunk in re.findall(r"[\w\u4e00-\u9fff]{2,}", str(value or "").lower()):
        # 同一个 chunk 里只保留「对得上人的最长片段」；这一层命中了就
        # 不必再切更短的片段（「illyasviel」命中后不用再看「illy」）。
        local: dict[str, set[str]] = {}
        for size in range(min(len(chunk), MAX_PIECE_LEN), 1, -1):
            for start in range(0, len(chunk) - size + 1):
                piece = chunk[start:start + size]
                if piece in WHEELCHAIR_FILLER_WORDS:
                    continue
                if piece in WHEELCHAIR_POSITION_WORDS:
                    continue
                matched = {name for name in lowered if piece in name}
                if matched:
                    local.setdefault(piece, set()).update(matched)
            if local:
                break
        if local:
            candidates = local
            break

    if not candidates:
        return ""
    # 同长度的候选必须指向**同一个人**：指向两个人说明原句有歧义，
    # 猜谁都是错的。同一人的多个重叠片段（英文长昵称常发生）则是安全的。
    longest = max(len(piece) for piece in candidates)
    picks = {piece: who for piece, who in candidates.items() if len(piece) == longest}
    everyone: set[str] = set()
    for who in picks.values():
        everyone |= who
    if len(everyone) != 1:
        return ""
    piece = min(picks, key=lambda item: str(value or "").lower().find(item))
    # 还原成原句里的大小写：用户怎么喊就怎么交给后续搜账号，别硬转小写
    raw = str(value or "")
    at = raw.lower().find(piece)
    return raw[at:at + len(piece)] if at != -1 else piece


def _wheelchair_target_ok(candidate: str) -> bool:
    """候选玩家名是否可信：位置词 / 版本词 / 虚词一律挡掉。

    宁可返回空（退回「只看榜单」），也不能拿「这版本」去搜账号 ——
    那不仅白跑一次网络请求，还会回一句「没找到『这版本』这个账号」。
    """
    name = (candidate or "").strip()
    if not name or name in TARGET_STOPWORDS:
        return False
    if name in WHEELCHAIR_SKIP_TARGETS:
        return False
    if "版本" in name or "赛季" in name:
        return False
    if name.lower() in WHEELCHAIR_POSITION_WORDS:
        return False
    return len(name) >= 2


def _wheelchair_position(value: str, lowered: str, *, exclude: str = "") -> str:
    """取句子里**最先出现**的位置词。

    原来是「按元组顺序谁先命中取谁」，句子里同时出现两个位置词时
    （「辅助和核心哪个是轮椅」）结果取决于别名表的排布；改成按出现
    位置取，稳定且可解释。

    ``exclude`` 传已识别出的玩家名：整段落在名字里的位置词要忽略 ——
    玩家昵称叫「小辅助」「前排队友」的不少，把名字的一半当位置过滤，
    榜单会莫名其妙少一批人（而且是静默的，看不出来）。
    """
    ex = (exclude or "").strip().lower()
    ex_at = lowered.find(ex) if ex else -1
    picked = ""
    best_at = len(lowered) + 1
    for word in WHEELCHAIR_POSITION_WORDS:
        at = lowered.find(word)
        if at == -1 or at >= best_at:
            continue
        if ex_at != -1 and ex_at <= at and at + len(word) <= ex_at + len(ex):
            continue
        best_at, picked = at, word
    return picked


def build_wheelchair_args(
    text: str,
    *,
    llm_target: str = "",
    known_names: tuple[str, ...] = (),
) -> str:
    """拼「查版本强势英雄」的参数串：``[位置] [玩家]``。

    玩家这一项按**可信度由高到低**依次尝试，前一步拿到就不再往下走：

    1. 本会话名单（:func:`match_session_name`）—— 已绑定 / 已监听的人，
       先按全名**原样出现在原句里**匹配，对不上再按**简称反查**：
       昵称常带一堆前后缀，群里只会喊简称，「钢板」得对上
       「你刚才确实说了【钢板】对吧」。命中后返回的是**名单里的原名**
       （不是用户喊的那个片段）：main 侧会先拿它去本会话名单里换
       account_id，换不到才联网搜 —— 用完整原名搜反而比片段更唯一。
    2. ``llm_target`` —— 分类模型给的 target。它能看见会话语境，所以有价值，
       但它会把「辅助」这类位置词填进 target，必须过 :func:`_wheelchair_target_ok`
       的安检，否则 handler 会拿「辅助」去搜账号。
    3. :data:`WHEELCHAIR_OWNER_RE` —— 「天鸽的轮椅」这种句式。
    4. 句子里的账号 ID（32 位 account_id / 64 位 SteamID，至少 6 位数字，
       所以「1号位」里的 1 不会误伤）。
    5. 「我 / 自己 / 本人」⇒ ``"我"``，由 handler 解析成当前会话的绑定。

    都没有就只返回位置词（或空串），由 handler 出纯榜单 —— **不猜人**。
    """
    value = str(text or "")
    lowered = value.lower()

    target = ""
    # 1) 本会话名单：先精确命中全名，再按简称反查（同一份实现，见
    #    match_session_name —— 工具层解析 player 参数走的也是它）
    matched_name = match_session_name(value, known_names)
    if matched_name:
        target = matched_name

    # 2) 分类模型给的 target（先认「我」，再按昵称安检）
    llm_position = ""
    if not target:
        raw_llm = str(llm_target or "").strip()
        if raw_llm in WHEELCHAIR_SELF_WORDS:
            target = "我"
        elif raw_llm.lower() in WHEELCHAIR_POSITION_WORDS:
            # 模型把位置词填进了 target：那当然不是人，但它的**意图**是对的
            # （用户问的就是辅助位），白白丢掉太可惜，接过来当位置用。
            llm_position = raw_llm.lower()
        else:
            candidate = _clean_target(raw_llm)
            if _wheelchair_target_ok(candidate):
                target = candidate

    # 3) 「XX 的轮椅」
    if not target:
        found = WHEELCHAIR_OWNER_RE.search(value)
        if found:
            candidate = _clean_target(found.group(1))
            if _wheelchair_target_ok(candidate):
                target = candidate

    # 4) 账号 ID
    if not target:
        digits = re.search(r"\d{6,20}", value)
        if digits:
            target = digits.group(0)

    # 5) 「我 / 自己」= 当前会话的绑定
    if not target and any(word in value for word in WHEELCHAIR_SELF_WORDS):
        target = "我"

    # 位置在玩家名之后算：先把人认准，才知道哪些「位置词」其实长在名字里。
    # 原句里没有位置词时，接受模型填进 target 的那个（见上面的 llm_position）。
    position = _wheelchair_position(value, lowered, exclude=target) or llm_position

    return " ".join(part for part in (position, target) if part)


def _build_intent(
    name: str,
    text: str,
    score: int,
    match_id: int | None,
    flipped: bool,
) -> Intent:
    """按意图拼装传给 handler 的参数串。"""
    detail = f"score={score}" + (" (negated)" if flipped else "")

    if name == "wheelchair":
        # 这里先出一版参数；真正的权威版本由 main 侧统一重建（它拿得到
        # 本会话的绑定 / 监听名单，能把「天鸽的轮椅」对上号）。
        return Intent(name, build_wheelchair_args(text), score, "rule", detail)

    if name == "match":
        args = str(match_id) if match_id else ""
        count = extract_count(text)
        return Intent(name, args, score, "rule", detail)

    if name == "forceparse":
        # 催解析只关心比赛 ID，没有 ID 就没有意义（由入口提示怎么补）
        return Intent(name, str(match_id) if match_id else "", score, "rule", detail)
    if name in {"help", "my", "bindings", "unbind", "watchlist", "llmtest", "datasource"}:
        # 取消监听 / 解绑支持「全部」
        if name == "unwatch" and re.search(r"全部|所有|都取消|清掉|清空", text):
            return Intent(name, "全部", score, "rule", detail)
        return Intent(name, "", score, "rule", detail)

    target = extract_target(text, name)
    count = extract_count(text)

    if name in {"matches", "analyze"}:
        parts: list[str] = []
        if count:
            parts.append(str(count))
        if target:
            parts.append(target)
        return Intent(name, " ".join(parts), score, "rule", detail)

    # bind / unbind / info / heroes / watch / unwatch：只有目标
    return Intent(name, target, score, "rule", detail)


# ======================================================================
# 大模型兜底分类（可选）
# ======================================================================

CLASSIFIER_SYSTEM_PROMPT = (
    "你是 Dota2 数据助手的意图分类器。只根据用户这句话（以及给出的会话上下文）"
    "判断他想用哪个功能，不要回答他的问题，也不要编造数据。必须只输出一行 JSON。"
)

#: 比赛 ID 的合理下界。模型偶尔会凭空编一个 ID 出来，凡是小于这个数
#: 的一概不信（现役比赛 ID 都在 80 亿以上）。
MIN_PLAUSIBLE_MATCH_ID = 1_000_000_000


def render_context(
    recent_lines: list[str] | None = None,
    recent_matches: list[dict] | None = None,
) -> str:
    """把会话上下文渲染成给模型看的一段说明文字。

    Args:
        recent_lines: 最近的对话（由旧到新），每条形如 ``用户: xxx``。
        recent_matches: 本会话最近提到过的比赛，每项含 ``match_id`` / ``desc``。
    """
    blocks: list[str] = []
    if recent_lines:
        blocks.append("本会话最近的对话（由旧到新）：\n" + "\n".join(recent_lines[-10:]))
    if recent_matches:
        rows = []
        for item in list(recent_matches)[:5]:
            mid = str(item.get("match_id") or "").strip()
            desc = str(item.get("desc") or "").strip()
            rows.append(f"- {mid}" + (f"：{desc}" if desc else ""))
        if rows:
            blocks.append(
                "本会话最近提到过的比赛（由新到旧）：\n" + "\n".join(rows)
            )
    return "\n\n".join(blocks)


def build_classifier_prompt(
    text: str,
    recent_lines: list[str] | None = None,
    recent_matches: list[dict] | None = None,
) -> str:
    """构造给大模型的意图分类提示词。

    ``recent_lines`` / ``recent_matches`` 是**消歧的关键**：用户说「这一盘」
    「上面那局」时，光看这一句话根本无从判断是哪场，必须把会话里刚提过的
    比赛一起给模型。
    """
    options = "\n".join(
        f"- {name}: {label}" for name, label in INTENT_LABELS.items()
    )
    context = render_context(recent_lines, recent_matches)
    parts = [
        f"可选功能：\n{options}\n"
        "- none: 以上都不是（闲聊、问别的问题、意图不明确）\n",
    ]
    if context:
        parts.append(
            "【会话上下文（用于理解『这一盘』『上面那局』这类指代）】\n"
            f"{context}\n"
        )
        parts.append(
            "判断规则：\n"
            "1. 用户说「这一盘 / 这把 / 那局 / 上一把 / 刚才那场 / 最后一把」等指代某一局时，"
            "输出 match，并把 match_id 填成上下文里对应的那一场；"
            "**只许填上下文或原句里出现过的比赛 ID，绝不许自己编一个**。\n"
            "2. 说「最近 N 场 / 近期表现 / 状态 / 打法 / 胜率」这类总结性要求 → analyze。\n"
            "3. 只是在聊天、问别的、或看不出明确意图 → none。\n"
            "4. 问「版本强势英雄 / 轮椅 / 胜率最高」这类问题（含「哪个英雄好上分」）"
            "→ wheelchair。他要是说了**想给谁看**，把那个人填进 target："
            "说「我 / 自己 / 给我 / 帮我」时 target 填「我」，只问榜单、没提人就留空。"
            "「辅助 / 核心 / 中单 / 三号位」这类是位置词，**不要**填进 target。\n"
            "5. 一句话里**同时要好几件事**（例如「钢板最近打得怎么样，顺便给他推荐几个轮椅」"
            "既要战绩又要英雄推荐；「看看大家谁强，再看版本榜」既要对比又要榜单）→ "看
            "**不要**只挑一个功能填，一律填 none：插件那边会带着工具逐项查完再一起回答，"
            "只填一个反而会漏答一半。\n"
            "6. match_id / count 没有就填 0，target 没有就填空字符串。\n"
        )
        parts.append(
            "输出格式（只输出 JSON，不要解释）：\n"
            '{"intent": "<功能名或 none>", "target": "<玩家昵称或账号ID；'
            '想给说话人自己看就填「我」，没有就空字符串>", '
            '"count": <场次数字，没有就 0>, "match_id": <比赛ID数字，没有就 0>}\n'
        )
        parts.append(f"用户说：{text}")
    return "\n".join(parts)


def parse_classifier_reply(reply: str) -> Intent | None:
    """解析大模型返回的 JSON；解析不出来或意图为 none 时返回 ``None``。"""
    if not reply:
        return None
    match = re.search(r"\{[\s\S]*?\}", reply)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None

    name = str(data.get("intent") or "").strip().lower()
    if not name or name == "none" or name not in INTENT_LABELS:
        return None

    target = str(data.get("target") or "").strip()
    count = data.get("count") or 0
    match_id = data.get("match_id") or 0
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 0
    try:
        match_id = int(match_id)
    except (TypeError, ValueError):
        match_id = 0

    # 模型编出来的比赛 ID 比认不出更糟——会去查一场根本不存在的比赛。
    # 只认「合理量级」的数字，其余一律当作没有。
    if match_id and match_id < MIN_PLAUSIBLE_MATCH_ID:
        match_id = 0

    matched = re.search(r"\d{6,20}", target)
    if name == "match":
        args = str(match_id) if match_id else (matched.group(0) if matched else "")
    elif name == "forceparse":
        args = str(match_id) if match_id else (matched.group(0) if matched else "")
    elif name in {"help", "my", "bindings", "unbind", "watchlist", "llmtest", "datasource"}:
        args = ""
    elif name in {"matches", "analyze"}:
        parts = []
        if count > 0:
            parts.append(str(count))
        if target:
            parts.append(target)
        args = " ".join(parts)
    else:
        args = target

    return Intent(name=name, args=args, score=0, via="llm", detail="llm-classifier")
