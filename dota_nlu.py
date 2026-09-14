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

#: 意图 → 是否需要「目标玩家」参数
INTENT_NEEDS_TARGET = {
    "bind",
    "unbind",
    "info",
    "heroes",
    "matches",
    "analyze",
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

    best = max(scores, key=lambda name: (scores[name], name))
    if scores[best] < MIN_SCORE:
        return None
    best_score = scores[best]

    # 否定词把「绑定 / 监听」翻成取消动作
    flipped = False
    if best in NEGATION_FLIP and NEGATION_RE.search(raw):
        best = NEGATION_FLIP[best]
        flipped = True
        # 翻转后的意图自己也有关键词得分（例如「取消监听」里的「取消监听」）
        best_score = max(best_score, scores.get(best, 0))

    return _build_intent(best, raw, best_score, match_id, flipped)


def _build_intent(
    name: str,
    text: str,
    score: int,
    match_id: int | None,
    flipped: bool,
) -> Intent:
    """按意图拼装传给 handler 的参数串。"""
    detail = f"score={score}" + (" (negated)" if flipped else "")

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
    "你是 Dota2 数据助手的意图分类器。只根据用户这句话判断他想用哪个功能，"
    "不要回答他的问题，也不要编造数据。必须只输出一行 JSON。"
)


def build_classifier_prompt(text: str) -> str:
    """构造给大模型的意图分类提示词。"""
    options = "\n".join(
        f"- {name}: {label}" for name, label in INTENT_LABELS.items()
    )
    return (
        f"可选功能：\n{options}\n"
        "- none: 以上都不是（闲聊、问别的问题、意图不明确）\n\n"
        "输出格式（只输出 JSON，不要解释）：\n"
        '{"intent": "<功能名或 none>", "target": "<玩家昵称或账号ID，没有就空字符串>", '
        '"count": <场次数字，没有就 0>, "match_id": <比赛ID数字，没有就 0>}\n\n'
        f"用户说：{text}"
    )


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

    matched = re.search(r"\d{6,20}", target)
    if name == "match":
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
