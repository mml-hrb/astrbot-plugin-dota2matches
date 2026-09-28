"""自然语言闲聊兜底：把插件内部数据交给大模型来回答。

触发场景
--------

用户写了唤醒词（默认「dota2助手」），但规则解析 + 模型分类都没识别出
内置指令。旧行为是直接 ``return``，把消息让给 AstrBot 的默认大模型，
于是：

* 「dota2助手 对比一下目前监听的几个人谁最菜」
  → 默认大模型只看到这句人话，不知道本会话监听列表里究竟有谁，
    只能反问「请告诉我是哪几位」，或者干脆编几个人名；
* 「dota2助手 我要转辅助，该怎么练」
  → 默认大模型给的是网上通用的训练建议，用不上插件里已经有的
    英雄池、近期胜率与 KDA。

新行为是**由插件带着真实数据来回答**：先整理「这个会话里有什么」
（监听列表 / 绑定列表 / 提问者资料），必要时再补上网络侧的近 N 场
战绩快照与英雄池，然后把数据 + 用户原话一次性交给模型。

设计要点
--------

1. **本地数据永远注入**（监听列表、绑定列表）。这两项直接读
   ``bindings.json``，零网络开销，所以普通闲聊也是秒回。
2. **网络数据按需注入**。只有当问题里出现「战绩 / 谁强 / 对比 /
   练什么 / 英雄 / 位置」这类信号（或点名了某个被监听者）时才去拉，
   避免每句闲聊都打十几个接口、把 API 配额和 token 一起烧掉。
3. **只给事实，不给结论**。上下文里同时给出原始数据和一个「参考分」，
   并在提示词里明确要求：参考分只是锚点，且 GPM 受位置影响
   （辅助位天然偏低），不许仅凭 GPM 判定强弱。
4. **缺失不冒充**。胜负判不出来的场次记为「未知」，不计入胜率分母；
   数据拿不到就写「数据获取失败」，不允许模型凭空补。
5. **取不到数据 ≠ 报错**。任何一步失败都只是少一块上下文，仍然回答 ——
   最差情况下模型退化成「普通闲聊 + 会话绑定信息」。

时效性与开黑（v2.2.7 起，v2.2.8 补「凌晨 4 点分界」）
--------------------------------------------------

「昨天谁打得好」「昨天群里开黑谁最牛逼」这两类问题，缺的从来不是数据，
而是**数据的坐标系**：

* 上下文顶部注入【当前时间】，每场比赛带自己的本地时间与「今天/昨天/前天」，
  模型才有依据把「昨天」落到具体某一天；
* 问题里出现时间词时解析成 :class:`TimeWindow`，此后**统计口径整体收窄到窗口内**
  —— 否则问「昨天」拿到的是「最近 10 场」的胜率，数字与口径对不上；
* 各人快照按 ``match_id`` 归并出【同场局】：同一场里出现两位以上本会话成员，
  是本群开黑**唯一的确凿信号**（``party_size`` 只说「跟几个人排」，不说「跟谁」），
  也是唯一能直接横向比高下的场景。

「一天」按**游戏日**切分（``DAY_BOUNDARY_HOUR``，默认凌晨 4 点），不按自然日：
玩家普遍熬夜，凌晨两点问「昨天」指的是**昨晚那一拨**，按 0 点切会把
昨晚 21~24 点整段踢出窗口 —— 恰好排除用户最想看的部分。详见
:func:`local_day_start`。

模块本身不依赖 AstrBot 的运行时（只用一个 logger），便于离线测试。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from astrbot.api import logger

try:  # 插件目录被作为包加载时的相对导入
    from . import dota_format, dota_pool
except ImportError:  # 兜底：以普通模块方式加载时，把插件目录加入 sys.path
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import dota_format  # type: ignore[no-redef]
    import dota_pool  # type: ignore[no-redef]

# ======================================================================
# 数据块：需要拿到什么，由问题里的信号决定
# ======================================================================

#: 需要「近 N 场战绩快照」（胜率 / KDA / GPM）。
NEED_RECENT = "recent"
#: 需要「提问者的英雄池」。
NEED_HEROES = "heroes"

#: 战绩类信号。命中就认为用户想看数据，而不只是闲聊。
RECENT_HINT_RE = re.compile(
    r"战绩|表现|水平|实力|状态|近况|最近|排名|排行|对比|比较|谁|几位|几个|"
    r"大家|全部|所有|强|菜|厉害|弱|牛|秀|坑|躺|上分|掉分|胜率|"
    r"kda|gpm|经济|输出|参团|数据|统计|评分|打得|发挥|"
    # 开黑 / 组队类：这类问题的答案只能从对局记录里读（party_size + 同场交集），
    # 不拉战绩就完全无从谈起。
    r"开黑|组队|车队|黑店|五黑|双排|三排|四排|同队|带飞|带躺|一起打|谁带"
)

#: 「时间词 + 这些词」= 在问**那段时间的比赛**。
#:
#: 单有时间词不够：「今天天气怎么样」也带时间词，但跟插件数据毫无关系，
#: 为它去拉一圈战绩纯属烧配额。所以这些「局/盘/玩/赢」这类**只有放到
#: 时间段里才成立**的词，只在检测到时间窗口时才拿来判定。
WINDOW_MATCH_HINT_RE = re.compile(
    r"局|盘|场|把|战绩|打得|打了几|打了没|玩了|玩了几|胜率|输赢|赢|输|"
    r"开黑|组队|推了|推过|战况|比赛|表现|发挥|谁|上分|掉分|菜|牛"
)

#: 英雄 / 位置类信号。
HERO_HINT_RE = re.compile(
    r"英雄|绝活|擅长|本命|练|位置|分路|中单|辅助|优势路|劣势路|游走|"
    r"carry|1号位|2号位|3号位|4号位|5号位|一号位|二号位|三号位|"
    r"四号位|五号位|出装|补刀|对线|转|换|改玩|上手|绝地"
)

#: 「位置」类信号：问「我打几号位 / 想转位置 / 该练哪个位置」的时候。
#:
#: 为什么要单拎出来：**位置数据藏在对局记录里**（每场比赛的
#: ``position`` 字段），英雄池接口本身不含位置。所以只加「英雄池」
#: 是不够的 —— 拿不到位置，模型就只能给网上抄来的通用建议，
#: 而「转位置怎么练」恰恰是本次要解决的场景。
POSITION_HINT_RE = re.compile(
    r"位置|分路|几号位|[1-5一二三四五]号位|中单|辅助|优势路|劣势路|游走|"
    r"转(?:位置|打|玩|型|成)|换位置|转型|换个位置"
)

#: 一次最多为几个玩家拉数据（含提问者本人）。监听列表可能很长，
#: 全拉会既慢又贵，这里截断并在上下文里注明。
DEFAULT_MAX_PLAYERS = 6
#: 每人取最近多少场。
DEFAULT_RECENT_LIMIT = 10
#: 每人单次拉取的超时（秒）。
DEFAULT_FETCH_TIMEOUT = 25.0
#: 战绩快照缓存 TTL（秒）。
DEFAULT_CACHE_TTL = 300.0
#: 明细行最多展示几场，防止提示词被撑爆。
DETAIL_ROWS = 5
#: 英雄池最多展示几个。
HERO_ROWS = 8

#: 识别到「昨天 / 今天 / 上周」这类时间窗口时，把取数场次放宽到这个值。
#:
#: 默认的 10 场通常只覆盖一两天，遇到「上周谁打得好」会因为手上根本没有
#: 那段时间的场次而得出「没打过」的假结论。放宽到 30 场不会增加请求数
#: （接口一次就返回），只是响应体稍大，属于划算的交换。
WINDOW_FETCH_LIMIT = 30

#: 【同场局】里最多展示几场，防止提示词被撑爆。
PARTY_MATCH_ROWS = 6

#: 「M月D日」形式的绝对日期。
#:
#: 刻意**不支持** ``9/14`` 这种写法：Dota 语境里 ``1/2号位``、``4/5号位``
#: 出现的频率远高于斜杠日期，而后者一旦被误判就会给整个会话套上
#: 「1 月 2 日」的窗口，把统计悄悄改错。宁可漏识别。
_ABS_DATE_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?")

#: 中文数字（时间窗口里出现的量级）。
_CN_DIGITS = {
    "一": 1,
    "两": 2,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

#: 天数片段：阿拉伯数字或中文数字（``3`` / ``三`` / ``十五``）。
#:
#: **必须支持中文数字**。原先只认 ``[1-9]\d?``，于是群里最常见的
#: 「这三天」「最近三天」全部解析不出来（2026-09-28 实测：「给群里这三天的
#: 战绩做个总结」返回 ``None``），统计口径静默退回「最近 N 场」——
#: 用户说的是三天，模型拿到的是最近十场。
_NUM_TOKEN = r"(?:\d{1,2}|[一两二三四五六七八九十]{1,3})"

#: 「最近 N 天 / 近 N 天 / 前 N 天 / 这 N 天」形式的**多日**窗口。
#:
#: 前缀比早期多了「这」：「这三天」「这两日」是很自然的说法。
#: 只跟「天 / 日」搭配，所以「这周」「这个月」「这两场」都不会被误伤。
_REL_DAYS_RE = re.compile(rf"(?:最近|近|前|这)\s*({_NUM_TOKEN})\s*(?:天|日)")

#: 「这几天 / 最近几天 / 近几天」这类**没给数字**的模糊说法。
#:
#: 按 :data:`VAGUE_DAYS` 天处理：中文里「这几天」强调「很近」，
#: 与「这三天」同量级；算 7 天会把用户根本没想看的日子拉进来。
_VAGUE_DAYS_RE = re.compile(r"(?:最近|近|这|前)\s*几\s*(?:天|日)")

#: 「这几天」没给数字时按几天算。
VAGUE_DAYS = 3

#: 「N 天前」形式的**单日**窗口（``三天前`` = 大前天）。
#:
#: 与 :data:`_REL_DAYS_RE` 是**反序**的，两者必须分开判：
#: 「前 3 天」是「3 天这么一个范围」，「3 天前」是「3 天之前那一天」。
#: 漏掉它的后果是「三天前谁打得好」解析成 ``None``，退回最近 N 场。
_DAYS_AGO_RE = re.compile(rf"({_NUM_TOKEN})\s*(?:天|日)\s*(?:前|以前|之前)")

#: 「一天」的分界点（小时）。默认 **凌晨 4 点**，不是 0 点。
#:
#: 为什么不用自然日：玩 Dota 的人普遍熬夜。凌晨 1 点还在连排，这时候问
#: 「昨天谁打得好」，他指的一定是**昨晚那一拨**（含刚打完的凌晨两三点的局），
#: 而不是「今天早上」那几个小时。按 0 点切会出现：
#:
#: * 凌晨 2 点问「昨天」→ 窗口是前天 0 点 ~ 昨天 0 点，昨晚 21~24 点的局
#:   全部落在窗口外，答案变成「昨天没人打」——**恰好把用户最想看的排除了**；
#: * 凌晨 2 点问「今天」→ 只能看到 0~2 点那零头。
#:
#: 所以 0:00~3:59 之间的时刻归**前一天**，一个「游戏日」= 当天 04:00 ~ 次日 04:00。
#: 改动这个值时注意 :func:`now_label` 会把它一并告诉模型。
DAY_BOUNDARY_HOUR = 4

#: 星期名（``time.localtime().tm_wday`` 是 0=周一）。
WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

#: Turbo 模式（OpenDota ``game_mode`` 编号）。
#: Turbo 的经济增长约为常规局的两倍，GPM/正补天然偏高，混在一起算平均值
#: 会把「爱打 Turbo 的人」直接抬成「最强者」，所以单独标注出来。
GAME_MODE_TURBO = 23
GAME_MODE_LABELS: dict[int, str] = {
    23: "Turbo",
    22: "全阵营随机",
    4: "单中模式",
    2: "队长模式",
    3: "随机征召",
}

DEFAULT_CHAT_SYSTEM_PROMPT = (
    "你是「Dota2 助手」，一个混在 QQ 群里的老玩家。"
    "你熟悉 Dota2 的英雄、位置、版本节奏与训练方法，说话口语化、接地气，"
    "可以直接给结论和态度，但要有依据、不装不吹。"
    "系统会把你手头真实掌握的数据一起发给你（会话里的监听名单、绑定名单、"
    "以及各人最近的战绩快照），你要优先使用这些数据来回答，"
    "因为它们比你的记忆更准确、更及时。"
)


# ======================================================================
# 小工具
# ======================================================================


def _as_int(value: Any) -> int | None:
    """尽力转成 int，失败返回 None（不把 None 当成 0）。"""
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:  # NaN
        return None
    return result


# ======================================================================
# 时间：让模型知道「昨天」到底是哪一天
# ======================================================================


def _now_ts(now: float | None = None) -> float:
    return time.time() if now is None else float(now)


def local_midnight(ts: Any) -> float | None:
    """取某个时刻所在**本地自然日**的 00:00（只做日期换算用，不含「一天」的定义）。"""
    try:
        value = int(ts)
    except (TypeError, ValueError):
        return None
    lt = time.localtime(value)
    try:
        return time.mktime(
            (lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)
        )
    except (OverflowError, ValueError):
        return None


def local_day_start(ts: Any) -> float | None:
    """取某个时刻所在**「游戏日」**的起点（当天 ``DAY_BOUNDARY_HOUR`` 点）。

    为什么按「游戏日」而不是自然日切：玩 Dota 的人普遍熬夜。
    凌晨 1 点问「昨天谁打得好」，用户指的是**昨晚那一拨**（含凌晨刚打的局），
    不是「今天早上」。按 0 点切，昨晚 21~24 点的局会被整段划出「昨天」窗口，
    答案就成了「昨天没人打」—— 恰好把用户最想看的排除了。

    所以 ``0:00 ~ 3:59`` 之间的时刻归**前一天**：
    一个游戏日 = 当天 ``04:00`` ~ 次日 ``04:00``。
    """
    midnight = local_midnight(ts)
    if midnight is None:
        return None
    try:
        start = midnight + DAY_BOUNDARY_HOUR * 3600
    except (OverflowError, ValueError):
        return None
    try:
        value = int(ts)
    except (TypeError, ValueError):
        return None
    if value < start:
        # 还没到当天的分界点 ⇒ 仍在**昨天**那个游戏日里
        start -= 86400
    return start


def day_offset(ts: Any, now: float | None = None) -> int | None:
    """目标时刻与「今天」相差几个**游戏日**：``0``=今天、``1``=昨天、``2``=前天。

    返回 ``None`` 表示时间戳缺失/非法 —— 调用方必须如实说「时间未知」，
    绝不能猜一个日期（猜错日期比不给日期更糟：模型会拿它去回答「昨天」）。
    """
    base = local_day_start(ts)
    reference = local_day_start(_now_ts(now))
    if base is None or reference is None:
        return None
    return int(round((reference - base) / 86400.0))


def rel_day_label(ts: Any, now: float | None = None) -> str:
    """``今天`` / ``昨天`` / ``前天`` / ``大前天`` / ``N 天前``（日期未知时给 ``时间未知``）。"""
    offset = day_offset(ts, now)
    if offset is None:
        return "时间未知"
    if offset <= 0:
        return "今天"
    if offset == 1:
        return "昨天"
    if offset == 2:
        return "前天"
    if offset == 3:
        return "大前天"
    return f"{offset} 天前"


def time_label(ts: Any, now: float | None = None) -> str:
    """比赛时刻的人话：``昨天 21:32``（越近越口语），四天以前补上 ``09-14``。"""
    try:
        value = int(ts)
    except (TypeError, ValueError):
        return "时间未知"
    clock = time.strftime("%H:%M", time.localtime(value))
    offset = day_offset(value, now)
    if offset is None:
        return clock
    if 0 <= offset <= 3:
        return f"{rel_day_label(value, now)} {clock}"
    return time.strftime("%m-%d %H:%M", time.localtime(value))


def now_label(now: float | None = None) -> str:
    """给模型看的当前时间：``2026-09-17 14:38 周四（…当前属于「09-17」这一天）``。

    必须把「一天从凌晨 4 点算起」一并说清：否则模型看到「当前时间
    2026-09-17 02:00」却被告知「今天 = 09-16」，会以为自己算错了。
    """
    current = _now_ts(now)
    lt = time.localtime(current)
    stamp = (
        time.strftime("%Y-%m-%d %H:%M", lt)
        + f" {WEEKDAY_NAMES[lt.tm_wday]}"
    )
    start = local_day_start(current)
    if start is None:
        return stamp
    day = time.strftime("%m-%d", time.localtime(start))
    return (
        f"{stamp}（本插件以**凌晨 {DAY_BOUNDARY_HOUR} 点**作为一天的分界，"
        f"熬夜打到凌晨的局算前一天；此刻属于「{day}」这一天）"
    )


@dataclass(frozen=True)
class TimeWindow:
    """用户问题里提到的**时间范围**（左闭右开）。

    ``start`` / ``end`` 是本地时间戳，``label`` 是给模型看的说法
    （``昨天`` / ``最近 3 天`` / ``09-14``）。
    """

    label: str
    start: float
    end: float

    def contains(self, ts: Any) -> bool:
        try:
            value = int(ts)
        except (TypeError, ValueError):
            return False
        return self.start <= value < self.end

    def describe(self) -> str:
        """``昨天（09-16）`` / ``最近 3 天（09-15 ~ 09-17）``。

        窗口两端都落在游戏日的 04:00 上，所以「最后一天」不能简单地用
        ``end - 1`` 取日期（那会得到次日 03:59，显示成次日）——
        要回退到 ``end`` 前一刻**所属的游戏日**。
        """
        if self.end - self.start <= 86400:
            return f"{self.label}（{time.strftime('%m-%d', time.localtime(self.start))}）"
        last_start = local_day_start(self.end - 1)
        if last_start is None:
            last_start = self.end - 1
        return (
            f"{self.label}（{time.strftime('%m-%d', time.localtime(self.start))}"
            f" ~ {time.strftime('%m-%d', time.localtime(last_start))}）"
        )


def rel_day_label_key(offset: int) -> str:
    """``0→今天``、``1→昨天`` ……（:func:`detect_time_window` 用来给窗口起名）。"""
    return {0: "今天", 1: "昨天", 2: "前天", 3: "大前天"}.get(offset, f"{offset} 天前")


def _day_window(offset: int, label: str, now: float | None = None) -> TimeWindow | None:
    """构造「N 天前那一整个游戏日」的窗口（04:00 ~ 次日 04:00）。"""
    today = local_day_start(_now_ts(now))
    if today is None:
        return None
    start = today - offset * 86400
    return TimeWindow(label=label, start=start, end=start + 86400)


def parse_day_count(token: str) -> int | None:
    """把 ``3`` / ``三`` / ``十五`` 这类天数片段转成整数；认不出返回 ``None``。"""
    token = (token or "").strip()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    if "十" in token:
        left, _, right = token.partition("十")
        # 「十五」= 15、「二十」= 20、「十」= 10：左边空着按 1 个十算
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones or None
    return _CN_DIGITS.get(token)


def recent_days_window(days: int, now: float | None = None) -> TimeWindow | None:
    """「最近 N 天」的窗口（**含今天**），两端落在游戏日的 04:00 上。

    ``days=3`` ⇒ ``[今天-2 天 04:00, 明天 04:00)``，即今天 / 昨天 / 前天。

    **解析器与工具参数共用这一份**：``query_matches`` 的 ``days`` 参数也调它。
    两边各写一套算式迟早在跨游戏日边界时对不上（一个含今天、一个不含），
    而「工具查出来的数」与「上下文里的统计」对不上是最难查的一类问题。
    """
    if not isinstance(days, int) or not 1 <= days <= 30:
        return None
    today = local_day_start(_now_ts(now))
    if today is None:
        return None
    start = today - (days - 1) * 86400
    return TimeWindow(f"最近 {days} 天", start, today + 86400)


def scope_matches(
    matches: Iterable[dict], window: TimeWindow | None
) -> list[dict]:
    """按时间窗口筛场次；``window`` 为 ``None`` 时原样返回。

    **全插件唯一一份窗口过滤实现**：上下文快照（:attr:`Snapshot.scoped`）与
    工具层 ``query_matches`` 都走它。各写一份的话迟早出现「上下文按三天统计、
    工具却把十场全算进去」——两条路径的数字对不上，而用户看的是同一句话。
    """
    if window is None:
        return list(matches)
    return [m for m in matches if window.contains(m.get("start_time"))]


def detect_time_window(
    question: str, now: float | None = None
) -> TimeWindow | None:
    """从问题里解析出时间窗口；没提到时间就返回 ``None``。

    所有「天」都按**游戏日**算：一天从凌晨 ``DAY_BOUNDARY_HOUR``（默认 4）点开始，
    ``0:00~3:59`` 归前一天 —— 熬夜打到凌晨两三点的局，用户会把它算进「昨晚」。

    支持的写法（按优先级从具体到宽泛）：

    * ``大前天`` / ``前天`` / ``昨天`` / ``今天``（含 昨晚 / 今早 / 刚才 这类变体）；
    * ``这周 / 本周``、``上周``（周一为一周之始，符合中文习惯）；
    * ``N 天前``（含中文数字：``三天前`` = 大前天）；
    * ``最近 3 天 / 近 5 天 / 前 3 天 / 这三天``（含今天，即从 N-1 天前算起）；
    * ``这几天 / 最近几天``（模糊说法，按 :data:`VAGUE_DAYS` 天算）；
    * ``9月14日``（若该**自然日**晚于今天，则当成去年的同一天）。

    没提时间时**必须返回 None**：此时统计口径是「最近 N 场」，
    不能被一个默认窗口悄悄改掉。
    """
    text = (question or "").strip()
    if not text:
        return None
    current = _now_ts(now)

    for offset, keywords in (
        (3, ("大前天", "大前日", "大前晚")),
        (2, ("前天", "前日", "前晚")),
        (1, ("昨天", "昨日", "昨晚", "昨儿")),
        (0, ("今天", "今日", "今晚", "今早", "今儿", "刚才", "刚刚")),
    ):
        for keyword in keywords:
            if keyword in text:
                window = _day_window(offset, rel_day_label_key(offset), current)
                if window is not None:
                    return window

    if any(key in text for key in ("上周", "上个星期", "上礼拜", "上一周")):
        today = local_day_start(current)
        if today is not None:
            this_monday = today - time.localtime(today).tm_wday * 86400
            return TimeWindow("上周", this_monday - 7 * 86400, this_monday)

    if any(key in text for key in ("这周", "本周", "这个星期", "这礼拜", "这一周")):
        today = local_day_start(current)
        if today is not None:
            this_monday = today - time.localtime(today).tm_wday * 86400
            return TimeWindow("本周", this_monday, today + 86400)

    match = _DAYS_AGO_RE.search(text)
    if match:
        days_ago = parse_day_count(match.group(1))
        if days_ago is not None and days_ago >= 1:
            window = _day_window(days_ago, rel_day_label_key(days_ago), current)
            if window is not None:
                return window

    match = _REL_DAYS_RE.search(text)
    if match:
        days = parse_day_count(match.group(1))
        if days is not None:
            window = recent_days_window(days, current)
            if window is not None:
                return window

    if _VAGUE_DAYS_RE.search(text):
        window = recent_days_window(VAGUE_DAYS, current)
        if window is not None:
            return window

    match = _ABS_DATE_RE.search(text)
    if match:
        month, day = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            lt = time.localtime(current)
            try:
                # 用户说的「9月14日」= 那一天的整个游戏日 ⇒ 从 04:00 起算
                start = time.mktime(
                    (lt.tm_year, month, day, DAY_BOUNDARY_HOUR, 0, 0, 0, 0, -1)
                )
                this_midnight = time.mktime(
                    (lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)
                )
            except (OverflowError, ValueError):
                return None
            # 未来日期 ⇒ 几乎必然指去年（「去年今天」是 365 天前）。
            # 注意比的是**自然日**而不是游戏日：凌晨 2 点说「9月17日」时，
            # 那个自然日已经开始过了，不该被当成明年。
            if time.mktime(
                (lt.tm_year, month, day, 0, 0, 0, 0, 0, -1)
            ) > this_midnight:
                try:
                    start = time.mktime(
                        (lt.tm_year - 1, month, day, DAY_BOUNDARY_HOUR, 0, 0, 0, 0, -1)
                    )
                except (OverflowError, ValueError):
                    return None
            return TimeWindow(f"{month} 月 {day} 日", start, start + 86400)

    return None


# ======================================================================
# 开黑 / 组队：同一场里有没有自己人
# ======================================================================


def party_size_of(match: dict) -> int | None:
    """``party_size`` 归一化：合法正数才返回，其余（含 ``None``）返回 ``None``。

    **缺这个字段不等于单排**。OpenDota 只有部分场次带 ``party_size``
    （实测某玩家最近 30 场里仅 8 场有值），把它当 0/1 会把「未知」
    系统性地说成「单排」。
    """
    size = _as_int(match.get("party_size"))
    if size is None or size <= 0:
        return None
    return size


def party_label(match: dict) -> str:
    """``开黑 4 人`` / ``单排``；组队情况未知时返回**空串**（未知就不说）。"""
    size = party_size_of(match)
    if size is None:
        return ""
    return "单排" if size <= 1 else f"开黑{size}人"


def side_of(match: dict) -> str:
    """``天辉`` / ``夜魇`` / ``未知``。"""
    slot = _as_int(match.get("player_slot"))
    if slot is not None:
        return "天辉" if slot < 128 else "夜魇"
    is_radiant = match.get("is_radiant")
    if isinstance(is_radiant, bool):
        return "天辉" if is_radiant else "夜魇"
    return "未知"


def row_win(match: dict) -> bool | None:
    """判断「玩家视角的一行比赛」是胜是负。

    返回 ``None`` 表示**判不出来** —— 这时候不能按负处理，否则胜率会
    被系统性低估（这和「缺失值不能伪装成 0」是同一类问题）。

    .. important::
       这里是 :func:`dota_format.match_result` 的**薄委托**，不再是独立实现。
       历史上本函数与 ``dota_format.player_win`` 各写了一套判定，而后者
       **不认 ``player_win`` 这个键**（STRATZ 写的恰恰是它），于是同一份数据
       在「事实底表」与「逐场明细」里给出相反的胜负 —— 模型看到两套矛盾
       数字，就会把用户的胜局讲成负局。判定只能有一处实现。
    """
    return dota_format.match_result(match)


def mode_label(match: dict) -> str:
    """非常规游戏模式的短标签（常规模式返回空串）。"""
    mode = _as_int(match.get("game_mode"))
    if mode is None or mode in (1, 0):
        return ""
    return GAME_MODE_LABELS.get(mode, f"模式{mode}")


#: STRATZ ``MatchPlayerType.position`` 枚举 → 位置名。
#:
#: **为什么不用 `lane_role`**：插件里既有的 ``STRATZ_POSITION_LANE_ROLE``
#: 把 ``POSITION_4 → 3``（「4 号位常驻劣势路」）、``POSITION_5 → 1``
#: （「5 号位常驻优势路」）压成了三档分路。那个映射是给「走哪条路」用的，
#: 对「转位置该怎么练」来说是**信息损失**：四号位和五号位的练法完全不同，
#: 压在一起就答不出「你想转 5 号位，先把视野和保人练起来」这种话。
#: 所以这里优先用精确到 1~5 的 ``position``，`lane_role` 只作退路。
POSITION_LABELS: dict[str, str] = {
    "POSITION_1": "1号位",
    "POSITION_2": "2号位",
    "POSITION_3": "3号位",
    "POSITION_4": "4号位",
    "POSITION_5": "5号位",
}

#: 退路：只有分路、没有明确位置时用（精度低一档，但比没有好）。
LANE_ROLE_LABELS: dict[int, str] = {
    1: "优势路",
    2: "中路",
    3: "劣势路",
    4: "野区/游走",
}


def position_label(match: dict) -> str:
    """取这场比赛里该玩家打的位置；**取不到返回空串，不许猜**。

    顺序：``position``（1~5 号位）→ ``lane_role``（分路）。

    取不到位置是常态而不是异常：未解析的比赛、早期版本的对局、
    以及后备数据源（OpenDota 的玩家对局列表）都可能没有这个字段。
    这时候宁可空着，也不能默认成某个位置 —— 位置判错会直接
    把「该练什么」的建议带偏。
    """
    raw = str(match.get("position") or "").strip().upper()
    if raw in POSITION_LABELS:
        return POSITION_LABELS[raw]
    lane_role = _as_int(match.get("lane_role"))
    if lane_role is not None and lane_role in LANE_ROLE_LABELS:
        return LANE_ROLE_LABELS[lane_role]
    return ""


def hero_label(heroes: dict[int, dict], hero_id: Any) -> str:
    """英雄 ID → 中文名；查不到时退化成 ``英雄#123``。"""
    hid = _as_int(hero_id)
    if hid is None:
        return "未知英雄"
    info = heroes.get(hid) if isinstance(heroes, dict) else None
    if isinstance(info, dict):
        name = info.get("localized_name") or info.get("name")
        if name:
            return str(name)
    return f"英雄#{hid}"


#: 出现这些词说明用户在问**多个人**（横向对比 / 群体排名），取数不能收窄到一个人。
#: 用于「兜底对话工具模式」下的按需取数：只点名了一个人、又没有这些词时，
#: 只拉那个人的数据就够了 —— 监听列表里六七个人全拉一遍纯属浪费配额和时间。
#:
#: ⚠️ 别把「几个 / 哪个」这类**也修饰物**的词收进来：「推荐几个轮椅英雄」
#: 里说的是英雄数量，不是人数 —— 收窄被它挡住过一次（实测发现）。
MULTI_PLAYER_HINT_RE = re.compile(
    r"谁|哪[个些]|对比|比较|大家|各位|全部|全都|所有人|每个人|群里|开黑|一起|"
    r"排名|排行|最菜|最强|最好|最差|几人|几位|几个人|我们"
)


def detect_needs(
    question: str, *, names: Iterable[str] = (), has_self: bool = False
) -> set[str]:
    """判断这个问题需要哪些**网络侧**数据块。

    本地数据（监听名单、绑定名单）永远注入，不在这里决定。

    命中规则：

    * 出现战绩类词（谁 / 对比 / 胜率 / 菜 / 厉害 …）→ 需要战绩快照；
    * 出现英雄 / 位置类词（练什么 / 转位置 / 绝活 …）→ 需要英雄池；
    * 问「位置」相关（想转位置 / 打几号位）→ 还需要战绩快照，**因为位置
      信息本身就在对局记录里**（见 :func:`position_label`）；
    * 问题里点名了某个被监听者的昵称 → 至少需要那个人的战绩快照
      （「Hangzz 最近怎么样」这种问题不一定带「战绩」二字）。
    """
    text = (question or "").lower()
    needs: set[str] = set()
    if not text:
        return needs

    if RECENT_HINT_RE.search(text):
        needs.add(NEED_RECENT)
    if HERO_HINT_RE.search(text):
        needs.add(NEED_HEROES)

    if POSITION_HINT_RE.search(text):
        needs.add(NEED_HEROES)
        # 「他打什么位置」只能从**提问者本人**的对局记录里读出来；
        # 没绑定账号就无从谈起，这时候别为了它白拉一圈别人的战绩。
        if has_self:
            needs.add(NEED_RECENT)

    # 点名：昵称至少两个字，避免「A」「我」这类误命中
    for name in names:
        token = (name or "").strip().lower()
        if len(token) >= 2 and token in text:
            needs.add(NEED_RECENT)
            break

    # 问「我该怎么练」但没绑定任何账号 → 英雄池无从谈起，去掉，
    # 免得白跑一次接口。
    if NEED_HEROES in needs and not has_self:
        needs.discard(NEED_HEROES)
    return needs


# ======================================================================
# 玩家快照
# ======================================================================


@dataclass
class PlayerSnapshot:
    """一个玩家近期表现的浓缩视图。"""

    account_id: int
    name: str
    relation: str = ""  # 「本人」/「被监听」
    matches: list[dict] = field(default_factory=list)
    #: 拉取失败时的原因（非空即为「拿不到数据」）
    error: str = ""
    #: 数据是否来自缓存
    cached: bool = False
    #: 用户问题里提到的时间窗口。非空时**所有统计与明细都只看窗口内的场次**
    #: —— 否则问「昨天谁打得好」，模型拿到的却是「最近 10 场」的胜率，
    #: 数字和口径对不上，回答必然跑偏。
    window: TimeWindow | None = None

    # ---------------- 统计 ----------------
    @property
    def scoped(self) -> list[dict]:
        """参与统计的场次：有窗口就只留窗口内的，没有就是全部。"""
        return scope_matches(self.matches, self.window)

    @property
    def excluded(self) -> int:
        """被时间窗口剔除掉的场次数量（提示词里要如实交代）。"""
        return len(self.matches) - len(self.scoped)

    @property
    def games(self) -> int:
        return len(self.scoped)

    @property
    def decided(self) -> int:
        """能判定胜负的场次（分母，不含未知）。"""
        return sum(1 for m in self.scoped if row_win(m) is not None)

    @property
    def wins(self) -> int:
        return sum(1 for m in self.scoped if row_win(m) is True)

    @property
    def win_rate(self) -> float | None:
        total = self.decided
        if not total:
            return None
        return self.wins / total

    @property
    def avg_kda(self) -> float | None:
        values = []
        for m in self.scoped:
            kills = _as_float(m.get("kills")) or 0.0
            deaths = _as_float(m.get("deaths")) or 0.0
            assists = _as_float(m.get("assists")) or 0.0
            values.append((kills + assists) / max(1.0, deaths))
        if not values:
            return None
        return sum(values) / len(values)

    @property
    def avg_gpm(self) -> float | None:
        values = [
            gpm
            for gpm in (_as_float(m.get("gold_per_min")) for m in self.scoped)
            if gpm is not None
        ]
        if not values:
            return None
        return sum(values) / len(values)

    @property
    def avg_deaths(self) -> float | None:
        values = [
            d
            for d in (_as_float(m.get("deaths")) for m in self.scoped)
            if d is not None
        ]
        if not values:
            return None
        return sum(values) / len(values)

    @property
    def turbo_games(self) -> int:
        return sum(1 for m in self.scoped if _as_int(m.get("game_mode")) == GAME_MODE_TURBO)

    @property
    def party_games(self) -> int:
        """窗口内明确是**组队（party_size≥2）**的场次。``None`` 不计入。"""
        return sum(
            1
            for m in self.scoped
            if (size := party_size_of(m)) is not None and size >= 2
        )

    @property
    def solo_games(self) -> int:
        """窗口内**明确是单排**（party_size==1）的场次。``None`` 不计入。"""
        return sum(1 for m in self.scoped if party_size_of(m) == 1)

    @property
    def party_unknown(self) -> int:
        """窗口内**组队情况未知**（没有 ``party_size`` 字段）的场次。

        单拎出来是因为它最容易被误读：数据源只有部分场次带这个字段，
        缺字段既不是单排也不是开黑。提示词里必须把「未知」这个量说出来，
        否则模型会默认「没标开黑的就是单排」。
        """
        return sum(1 for m in self.scoped if party_size_of(m) is None)

    @property
    def position_counts(self) -> list[tuple[str, int]]:
        """近期各位置的场次，按场次从多到少（取不到位置的场次不计入）。

        注意它可能与 :attr:`games` 不相等 —— 差值就是「位置未知」的场次。
        提示词里会把这个差值说明白，免得模型把「10 场里只认出 6 场」
        当成「另外 4 场是别的位置」来推理。
        """
        counter: dict[str, int] = {}
        for match in self.scoped:
            label = position_label(match)
            if label:
                counter[label] = counter.get(label, 0) + 1
        return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))

    def hero_positions(self) -> dict[int, str]:
        """近期对局里「英雄 → 打过什么位置」，用来给英雄池补上位置。

        英雄池接口（``get_player_heroes``）**不含位置**，所以只能在
        近期战绩里就地取：某个英雄在他最近这几场里打的什么位置，
        是这个英雄对他而言「打几号位」最直接的证据。
        """
        mapping: dict[int, str] = {}
        for match in self.scoped:
            hero_id = _as_int(match.get("hero_id"))
            label = position_label(match)
            if hero_id is None or not label:
                continue
            # 同一英雄打过多个位置时，以最近一场为准（列表按时间倒序）
            mapping.setdefault(hero_id, label)
        return mapping

    def score(self) -> float | None:
        """0~100 的**参考分**，只在横向对比时当锚点用。

        由胜率、KDA、GPM 加权（缺项时自动重新归一化权重）。
        它刻意做得很粗糙：位置差异、版本强度、开黑环境都不在里面，
        所以提示词里明确要求模型「别把参考分当真理」。

        KDA 与 GPM 都用 **soft cap**（``x / (x + k)``）而不是线性截断。
        这不是数学洁癖，而是实测踩到的坑：最初写的是 ``min(gpm / 700, 1)``，
        结果真实数据里两个人的 GPM 是 837 与 1072 —— **都封顶到 1.0**，
        GPM 这一项直接失去区分度，参考分只剩下胜率在起作用。
        soft cap 单调且永不饱和，再高的数值也还能贡献一点区分度。
        """
        weight = 0.0
        acc = 0.0

        win_rate = self.win_rate
        if win_rate is not None:
            acc += 0.45 * win_rate
            weight += 0.45

        kda = self.avg_kda
        if kda is not None:
            acc += 0.35 * (kda / (kda + 4.0))
            weight += 0.35

        gpm = self.avg_gpm
        if gpm is not None:
            acc += 0.20 * (gpm / (gpm + 900.0))
            weight += 0.20

        if not weight:
            return None
        return 100.0 * acc / weight

    # ---------------- 渲染 ----------------
    def summary_line(self, now: float | None = None) -> str:
        label = f"{self.name}（账号 {self.account_id}"
        if self.relation:
            label += f"，{self.relation}"
        label += "）"
        # 有窗口时把窗口写在名字后面：模型一眼就能看出「这个胜率是昨天的」
        if self.window is not None:
            label += f"｜{self.window.describe()}"

        if self.error:
            return f"· {label}：数据获取失败（{self.error}）"
        if not self.games:
            if self.window is None:
                return f"· {label}：最近没有可用的比赛记录"
            # 窗口内 0 场是个**结论**（「他昨天没打」），要说清楚。
            # 但必须同时交代手上还有窗口外的场次，否则模型会以为是数据没拉到。
            tail = (
                f"（手上另有 {self.excluded} 场其它时段的记录，未计入本次统计）"
                if self.excluded
                else "（手上也没有其它时段的记录）"
            )
            return f"· {label}：这段时间内没有比赛记录{tail}"

        parts = [f"{self.games} 场：{self.wins} 胜"]
        lost = self.decided - self.wins
        parts.append(f"{lost} 负")
        unknown = self.games - self.decided
        if unknown:
            parts.append(f"{unknown} 场胜负未知")
        win_rate = self.win_rate
        if win_rate is not None:
            parts.append(f"胜率 {win_rate * 100:.0f}%")
        kda = self.avg_kda
        if kda is not None:
            parts.append(f"平均 KDA {kda:.2f}")
        deaths = self.avg_deaths
        if deaths is not None:
            parts.append(f"平均死亡 {deaths:.1f}")
        gpm = self.avg_gpm
        if gpm is not None:
            parts.append(f"平均 GPM {gpm:.0f}")
        positions = self.position_counts
        if positions:
            top = "、".join(f"{label} {n} 场" for label, n in positions[:3])
            # 有窗口时「近期」是错的（统计其实只覆盖那个窗口），换成中性的「位置」
            parts.append(f"{'位置' if self.window else '近期位置'} {top}")
            known = sum(n for _, n in positions)
            if known < self.games:
                parts.append(f"另有 {self.games - known} 场位置未知")
        score = self.score()
        if score is not None:
            parts.append(f"参考分 {score:.0f}/100")
        turbo = self.turbo_games
        if turbo:
            parts.append(f"其中 {turbo} 场 Turbo")
        # 组队情况：把「未知」也说出来，否则模型会把没标开黑的当成单排
        group_bits = []
        if self.party_games:
            group_bits.append(f"开黑 {self.party_games} 场")
        if self.solo_games:
            group_bits.append(f"单排 {self.solo_games} 场")
        if self.party_unknown:
            group_bits.append(f"组队情况未知 {self.party_unknown} 场")
        if group_bits:
            parts.append("组队：" + " / ".join(group_bits))

        line = f"· {label}：" + " | ".join(parts)
        if self.window is not None and self.excluded:
            line += f"（另有 {self.excluded} 场在本时间窗口之外，未计入）"
        return line

    def detail_lines(self, heroes: dict[int, dict], now: float | None = None) -> list[str]:
        """最近几场的逐场明细（最多 :data:`DETAIL_ROWS` 条）。

        每行都带**本地时间与相对日期**（``昨天 21:32``）：没有时间，
        「昨天谁打得好」就只能靠猜。组队情况（开黑/单排）也一并标上，
        「哪些局是开黑的」正是靠它回答。
        """
        rows = self.scoped
        if not rows:
            return []
        lines = []
        for match in rows[:DETAIL_ROWS]:
            # 主语写进行内（「该玩家负」而不是「负」）：模型手上同时有「天辉/夜魇
            # 获胜」和「胜负」两套说法时，不带主语的简写最容易读反。
            flag = f"该玩家{dota_format.result_text(match)}"
            kills = _as_int(match.get("kills"))
            deaths = _as_int(match.get("deaths"))
            assists = _as_int(match.get("assists"))
            kda_text = (
                f"{kills}/{deaths}/{assists}"
                if None not in (kills, deaths, assists)
                else "-/-/-"
            )
            bits = [
                time_label(match.get("start_time"), now),
                flag,
                hero_label(heroes, match.get("hero_id")),
                kda_text,
            ]
            position = position_label(match)
            if position:
                bits.append(position)
            mode = mode_label(match)
            if mode:
                bits.append(mode)
            gpm = _as_float(match.get("gold_per_min"))
            if gpm is not None:
                bits.append(f"GPM {gpm:.0f}")
            duration = _as_int(match.get("duration"))
            if duration:
                bits.append(f"{duration // 60} 分钟")
            # 组队情况：明确是组队/单排才说，取不到就留白（空白 ≠ 单排）
            party = party_label(match)
            if party:
                bits.append(party)
            lines.append("　　" + " · ".join(bits))
        return lines


# ======================================================================
# 上下文
# ======================================================================


@dataclass
class ChatContext:
    """一次闲聊回答所依据的全部插件数据。"""

    question: str = ""
    umo: str = ""
    user_id: str = ""
    self_binding: dict | None = None
    bindings: list[dict] = field(default_factory=list)
    watchers: list[dict] = field(default_factory=list)
    snapshots: list[PlayerSnapshot] = field(default_factory=list)
    heroes: dict[int, dict] = field(default_factory=dict)
    hero_history: list[dict] = field(default_factory=list)
    #: 英雄池的**口径说明**（覆盖哪个版本、共多少场、其中加速多少场）。
    #: 空串表示没按版本口径取（或没取到）。不写进提示词的话，模型会把
    #: 「当前版本的场次」当成生涯数据，说出「他总共就玩过 17 场」这种错话。
    hero_pool_scope: str = ""
    needs: set[str] = field(default_factory=set)
    #: 给模型看的「注意事项」，例如某块数据没取到
    notes: list[str] = field(default_factory=list)
    #: 回答时刻（时间戳）。「今天/昨天」全部以它为准，不能用模块导入时的时间。
    now: float = 0.0
    #: 用户问题里提到的时间窗口（没提就是 ``None``，统计口径即「最近 N 场」）。
    window: TimeWindow | None = None
    #: **本会话近期涉及过的比赛**（监听推送 / 单场复盘 / 战绩列表都算），
    #: 由调用方从会话语境里取，形如 ``{match_id, desc, start_time, ts}``。
    session_matches: list[dict] = field(default_factory=list)
    #: **最近几轮对话**（``["用户: …", "机器人: …"]``，新的在后）。
    #: 交给模型消解指代用：「那他昨天呢」里的「他」只在上一条消息里出现过，
    #: 没有这份背景，模型只能反问「你说的是谁」。
    history: list[str] = field(default_factory=list)


def _self_hero_positions(ctx: ChatContext) -> dict[int, str]:
    """汇总「英雄 → 位置」，优先用**提问者本人**的近期对局。

    问「转位置怎么练」时，需要的是**他自己**拿某个英雄打什么位置；
    别人的位置记录对他没意义，所以优先只取 ``relation == "本人"`` 的那份。
    本人没有快照（未绑定）时退而用全部快照 —— 总比什么都不给强，
    但提示词里已经说明「提问者未绑定」，模型不会张冠李戴。
    """
    merged: dict[int, str] = {}
    for snap in ctx.snapshots:
        if snap.relation == "本人":
            merged.update(snap.hero_positions())
    if merged:
        return merged
    for snap in ctx.snapshots:
        merged.update(snap.hero_positions())
    return merged


def format_comparison_block(snapshots: Iterable[PlayerSnapshot]) -> str:
    """把多人的各项指标逐条排名。

    这比只给一个「综合分」有用得多：真实数据里强弱往往是**分项交叉**的
    —— 胜率高的 KDA 反而低、KDA 高的 GPM 低。只给总分等于替用户做了
    一个武断的加权，而把各项摊开之后，模型才能说出「他胜率领先但 KDA
    垫底、而且这 10 场里有 4 场 Turbo」这种真正有依据的话。

    少于 2 人有数据时返回空串（一个人没有可比性）。
    """
    rows = [s for s in snapshots if s.games]
    if len(rows) < 2:
        return ""

    def one_line(label: str, getter, fmt, bigger_better: bool) -> str | None:
        pairs = [(s.name, getter(s)) for s in rows]
        pairs = [(name, value) for name, value in pairs if value is not None]
        if len(pairs) < 2:
            return None
        pairs.sort(key=lambda item: item[1], reverse=bigger_better)
        # 排序方向始终是「从好到差」，但连接符要跟着数值走：
        # 「平均每场死亡」是越小越好，若一律用 ">" 会写出
        # 「Hangzz 4.0 > 钢板 4.5」这种数值上明显矛盾的句子，
        # 模型照抄就成了事实错误。
        joiner = " > " if bigger_better else " < "
        return f"· {label}：" + joiner.join(f"{name} {fmt(value)}" for name, value in pairs)

    lines: list[str] = []
    for label, getter, fmt, bigger_better in (
        ("胜率", lambda s: s.win_rate, lambda v: f"{v * 100:.0f}%", True),
        ("平均 KDA", lambda s: s.avg_kda, lambda v: f"{v:.2f}", True),
        ("平均每场死亡", lambda s: s.avg_deaths, lambda v: f"{v:.1f}", False),
        ("平均 GPM", lambda s: s.avg_gpm, lambda v: f"{v:.0f}", True),
        ("参考分", lambda s: s.score(), lambda v: f"{v:.0f}", True),
    ):
        row = one_line(label, getter, fmt, bigger_better)
        if row:
            lines.append(row)

    if not lines:
        return ""
    return "\n".join(
        ["【横向对比】各项从好到差排列（解读方式见下方注意事项）"] + lines
    )


def group_party_matches(
    snapshots: Iterable[PlayerSnapshot],
) -> tuple[list[tuple[dict, list[tuple[PlayerSnapshot, dict]]]], list[tuple[PlayerSnapshot, dict]]]:
    """把各人快照按 ``match_id`` 归并，分出「同场局」与「单人组队局」。

    Returns:
        ``(same_match_groups, solo_party_rows)``：

        * ``same_match_groups`` —— 同一场比赛里出现了 **2 位以上**本次取到数据的
          成员的 ``(代表行, [(快照, 该人在本局的行)])``，按开赛时间从新到旧；
        * ``solo_party_rows`` —— 明确是组队（``party_size≥2``）但只有一位成员在场的
          ``(快照, 行)``。

    「同场」是本群开黑**唯一的确凿信号**：``party_size`` 只告诉你他这局跟几个人
    一起排，不告诉你是谁；而两个人出现在同一个 ``match_id`` 里，是数据层面
    无可辩驳的事实。所以这个归并同时解决了「哪些局是开黑」和「谁跟谁开黑」。
    """
    by_match: dict[int, list[tuple[PlayerSnapshot, dict]]] = {}
    solo_party: list[tuple[PlayerSnapshot, dict]] = []
    for snap in snapshots:
        if snap.error:
            continue
        for match in snap.scoped:
            mid = _as_int(match.get("match_id"))
            if mid is None:
                continue
            by_match.setdefault(mid, []).append((snap, match))

    groups: list[tuple[dict, list[tuple[PlayerSnapshot, dict]]]] = []
    for mid, rows in by_match.items():
        if len(rows) >= 2:
            groups.append((rows[0][1], rows))
        else:
            snap, match = rows[0]
            size = party_size_of(match)
            if size is not None and size >= 2:
                solo_party.append((snap, match))

    groups.sort(key=lambda item: _as_int(item[0].get("start_time")) or 0, reverse=True)
    solo_party.sort(
        key=lambda item: _as_int(item[1].get("start_time")) or 0, reverse=True
    )
    return groups, solo_party


def _party_member_line(
    snap: PlayerSnapshot, match: dict, heroes: dict[int, dict]
) -> str:
    """同场局里某一位成员的那一行。"""
    kills = _as_int(match.get("kills"))
    deaths = _as_int(match.get("deaths"))
    assists = _as_int(match.get("assists"))
    kda = (
        f"{kills}/{deaths}/{assists}"
        if None not in (kills, deaths, assists)
        else "-/-/-"
    )
    bits = [
        hero_label(heroes, match.get("hero_id")),
        kda,
    ]
    position = position_label(match)
    if position:
        bits.append(position)
    gpm = _as_float(match.get("gold_per_min"))
    if gpm is not None:
        bits.append(f"GPM {gpm:.0f}")
    win = row_win(match)
    bits.append("胜" if win is True else ("负" if win is False else "胜负未知"))
    return f"　{snap.name}（{side_of(match)}）：" + " · ".join(bits)


def format_party_block(
    snapshots: Iterable[PlayerSnapshot],
    heroes: dict[int, dict],
    now: float | None = None,
) -> str:
    """渲染「同场局」与「明确组队局」。

    没有同场局时**不返回空串**，而是明说「本次取到数据的几位之间没有共同
    参与的场次」—— 因为「他们今天没一起打」本身就是一个结论，
    留空会让模型自己想象一个。
    """
    snapshot_list = [s for s in snapshots if not s.error]
    with_rows = [s for s in snapshot_list if s.scoped]
    if not with_rows:
        return ""

    groups, solo_party = group_party_matches(snapshot_list)
    lines: list[str] = []

    if groups:
        lines.append(
            "【同场局（同一场比赛里出现了 ≥2 位下面这些人 = 本群开黑的铁证）】"
        )
        for rep, rows in groups[:PARTY_MATCH_ROWS]:
            stamp = time_label(rep.get("start_time"), now)
            duration = _as_int(rep.get("duration"))
            winner = "天辉获胜" if rep.get("radiant_win") else "夜魇获胜"
            head = [stamp, mode_label(rep) or "", f"{duration // 60} 分钟" if duration else "", winner]
            lines.append(
                f"· 比赛 {rep.get('match_id')} · " + " · ".join(b for b in head if b)
            )
            for snap, match in rows:
                lines.append(_party_member_line(snap, match, heroes))
            sides = {side_of(match) for _, match in rows}
            known = sides - {"未知"}
            if "未知" in sides:
                lines.append("　→ 有人阵营未知，不能断定是否同队")
            elif len(known) == 1:
                lines.append(f"　→ 同队（{known.pop()}）")
            else:
                lines.append("　→ 不同队（互相对位）")
        if len(groups) > PARTY_MATCH_ROWS:
            lines.append(f"（还有 {len(groups) - PARTY_MATCH_ROWS} 场同场局未列出）")
    elif len(with_rows) >= 2:
        # 一个人谈不上「同场」，所以只有 ≥2 人时才出这个结论
        lines.append(
            "【同场局】本次取到数据的这些人之间**没有**共同参与的场次"
            "（注意：只在本次取到数据的人之间能这样判断）"
        )

    if solo_party:
        lines.append("")
        lines.append("【明确组队、但队友不在本次取到数据的名单里】")
        for snap, match in solo_party[:PARTY_MATCH_ROWS]:
            size = party_size_of(match)
            bits = [
                f"{snap.name} {time_label(match.get('start_time'), now)}",
                f"{size} 人组队",
                hero_label(heroes, match.get("hero_id")),
            ]
            win = row_win(match)
            bits.append("胜" if win is True else ("负" if win is False else "胜负未知"))
            lines.append("· " + " · ".join(bits))

    return "\n".join(lines)


def format_session_matches_block(ctx: ChatContext) -> str:
    """渲染「本会话近期涉及过的比赛」（监听推送 / 复盘 / 战绩查询）。

    时间只用比赛自己的 ``start_time``：**拿不到就写「时间未知」，绝不拿
    「记录时刻」顶替** —— 复盘一场三天前的旧局时，记录时刻就是「现在」，
    用它会把旧局标成「今天」，直接污染「昨天/今天」这类回答。
    """
    if not ctx.session_matches:
        return ""
    lines = [
        "【本会话近期涉及过的比赛】"
        "（监听推送 / 单场复盘 / 战绩查询中提到过的，由新到旧）"
    ]
    for row in ctx.session_matches:
        mid = _as_int(row.get("match_id"))
        if mid is None:
            continue
        start_time = row.get("start_time")
        stamp = time_label(start_time, ctx.now) if start_time else "时间未知"
        desc = str(row.get("desc") or "").strip()
        bits = [f"比赛 {mid}", stamp]
        if desc:
            bits.append(desc)
        lines.append("· " + " · ".join(bits))
    if len(lines) == 1:
        return ""
    lines.append(
        "（这里只有比赛 ID 与时间；要具体数据得靠上面各人的近期战绩，"
        "或让用户用单场复盘指令去查）"
    )
    return "\n".join(lines)


def describe_focus_result(
    match: dict,
    heroes: dict[int, dict],
    focus_ids: Iterable[Any] = (),
    focus_names: dict | None = None,
) -> str:
    """把「谁 + 什么英雄 + KDA + 胜负」压成一句话，供会话语境使用。

    监听推送是**唯一**手上握着完整 ``match`` 对象的时机（之后再提到这场
    就只剩一个 match_id 了），所以描述要在这里一次写足 —— 闲聊兜底回答
    「昨天谁打得好」时，这就是手上唯一的一手资料。

    取不到的字段直接跳过，**不编**（宁可少一句，不要写错一个英雄名）。
    """
    names = focus_names or {}
    players: dict[int, dict] = {}
    for row in match.get("players") or []:
        if not isinstance(row, dict):
            continue
        account_id = _as_int(row.get("account_id"))
        if account_id is not None:
            players[account_id] = row

    duration = _as_int(match.get("duration"))
    items: list[str] = []
    for raw_id in focus_ids or ():
        account_id = _as_int(raw_id)
        if account_id is None:
            continue
        label = str(names.get(account_id) or "").strip() or f"账号{account_id}"
        row = players.get(account_id)
        detail = [label]
        if row:
            detail.append(hero_label(heroes, row.get("hero_id")))
            kills = _as_int(row.get("kills"))
            deaths = _as_int(row.get("deaths"))
            assists = _as_int(row.get("assists"))
            if None not in (kills, deaths, assists):
                detail.append(f"{kills}/{deaths}/{assists}")
            merged = dict(row)
            if merged.get("radiant_win") is None:
                merged["radiant_win"] = match.get("radiant_win")
            win = row_win(merged)
            if win is not None:
                detail.append("胜" if win else "负")
        items.append(" ".join(bit for bit in detail if bit))

    text = "；".join(items)
    if not text:
        # 一个焦点玩家都没有 → 不产出描述（存进去只会是一条只有时长的噪音）
        return ""
    if duration:
        text += f"（{duration // 60} 分钟）"
    return text.strip()


def _binding_label(info: dict) -> str:
    name = str(info.get("personaname") or "").strip() or "未命名"
    account_id = _as_int(info.get("account_id"))
    return f"{name}（账号 {account_id}）" if account_id else name


def _watcher_label(watcher: dict) -> str:
    name = str(watcher.get("personaname") or "").strip() or "未命名"
    account_id = _as_int(watcher.get("account_id"))
    bits = [name]
    if account_id:
        bits.append(f"账号 {account_id}")
    creator = str(watcher.get("created_by_name") or "").strip()
    if creator:
        bits.append(f"由{creator}添加")
    return "（" + "，".join(bits) + "）" if len(bits) > 1 else f"（{bits[0]}）"


def format_context_block(ctx: ChatContext, *, tooled: bool = False) -> str:
    """把上下文渲染成给模型看的纯文本（无 markdown 表格）。

    Args:
        tooled: ``True`` 表示这份提示词是发给**带工具**的模型的（自然语言
            主路径）。此时的上下文里**没有任何网络数据**，只有本会话的事实
            底表，所以要在抬头把这件事讲清楚，否则模型会以为「没写战绩」
            等于「这些人都没打过」。
    """
    lines: list[str] = ["=== 本会话的插件数据 ==="]

    if tooled:
        lines.append(
            "（这里只有**本会话的事实**：有哪些人在名单里、最近发生过哪几场、"
            "当前时间。**没有任何战绩数字** —— 要具体战绩 / 英雄池 / 版本数据，"
            "必须自己调工具去查。下面只是底表。）"
        )

    # ---- 当前时间：一切「今天/昨天/前天」的基准 ----
    # 没有这一行，模型只能拿自己的训练时间或系统提示里的时间去猜，
    # 「昨天」就无从落地。放在最前面，因为它决定后面所有相对日期的读法。
    lines.append(
        f"【当前时间】{now_label(ctx.now)}"
        "（下面提到的「今天/昨天/前天」以及所有相对日期，都以这个时刻为准）"
    )

    # ---- 时间窗口 ----
    # 只在「窗口真的会作用到下面的数据」时才声明。
    # 「今天天气怎么样」这类纯闲聊虽然也能解析出时间词，但没有任何统计被收窄，
    # 这时还写一句「下面的统计只算窗口内」只会让模型以为真有一批数据被裁剪了。
    if ctx.window is not None and (ctx.snapshots or NEED_RECENT in ctx.needs):
        lines.append(
            f"【时间窗口】用户问的时间范围是 {ctx.window.describe()}。"
            "下面每人的统计**只统计窗口内的场次**，"
            "窗口外的场次已剔除、不计入任何比率；若某人窗口内 0 场，"
            "那就如实说他这段时间没打。"
        )

    # ---- 提问者 ----
    if ctx.self_binding:
        lines.append(f"【提问者】已绑定：{_binding_label(ctx.self_binding)}")
    else:
        lines.append(
            "【提问者】尚未绑定 Dota2 账号"
            "（所以无法直接查他本人的战绩/英雄池）"
        )

    # ---- 绑定名单 ----
    if ctx.bindings:
        names = "、".join(_binding_label(item) for item in ctx.bindings)
        lines.append(f"【本会话已绑定账号】共 {len(ctx.bindings)} 人：{names}")
    else:
        lines.append("【本会话已绑定账号】没有其他人绑定")

    # ---- 监听名单 ----
    if ctx.watchers:
        names = "、".join(_watcher_label(item) for item in ctx.watchers)
        lines.append(f"【本会话正在监听】共 {len(ctx.watchers)} 人：{names}")
    else:
        lines.append(
            "【本会话正在监听】没有监听任何玩家"
            "（用户可以用 `/d2 监听` 加上）"
        )

    # ---- 本会话涉及过的比赛（含监听推送捕捉到的） ----
    session_block = format_session_matches_block(ctx)
    if session_block:
        lines.append("")
        lines.append(session_block)

    # ---- 战绩快照 ----
    if ctx.snapshots:
        lines.append("")
        lines.append(
            f"【近期战绩快照】每人最近若干场（数据源：主数据源，"
            f"取不到时自动回退后备源）"
        )
        has_detail = False
        for snap in ctx.snapshots:
            lines.append(snap.summary_line(ctx.now))
            detail = snap.detail_lines(ctx.heroes, ctx.now)
            if detail:
                has_detail = True
                lines.extend(detail)
        if has_detail:
            lines.append("（上面缩进的几行是各自最近几场的逐场明细）")
        comparison = format_comparison_block(ctx.snapshots)
        if comparison:
            lines.append("")
            lines.append(comparison)

        # ---- 同场局 / 明确组队局 ----
        # 这是「群里开黑谁最牛逼」唯一站得住的依据：只有同场比赛里
        # 的数据才能直接横向比高下。
        party_block = format_party_block(ctx.snapshots, ctx.heroes, ctx.now)
        if party_block:
            lines.append("")
            lines.append(party_block)

    # ---- 英雄池 ----
    if ctx.hero_history:
        lines.append("")
        # 口径必须写在抬头：这份池子是**当前版本**的（且默认含加速局），
        # 不写模型就会当成生涯数据，说出「他总共就玩过这几个英雄」这种错话
        title = "【提问者的英雄池】按使用场次排序，取前几："
        if ctx.hero_pool_scope:
            title += f"（口径：{ctx.hero_pool_scope}）"
        lines.append(title)
        # 英雄池接口不带位置，位置只能从他自己近期的对局记录里就地取
        hero_pos = _self_hero_positions(ctx)
        for row in ctx.hero_history[:HERO_ROWS]:
            name = hero_label(ctx.heroes, row.get("hero_id"))
            games = _as_int(row.get("games"))
            wins = _as_int(row.get("win"))
            bits = [name]
            if games:
                bits.append(f"{games} 场")
            if games and wins is not None:
                bits.append(f"胜率 {wins / max(1, games) * 100:.0f}%")
            kda = _as_float(row.get("_kda"))
            if kda is not None:
                bits.append(f"KDA {kda:.2f}")
            gpm = _as_float(row.get("gold_per_min"))
            if gpm is not None:
                bits.append(f"GPM {gpm:.0f}")
            hero_id = _as_int(row.get("hero_id"))
            if hero_id is not None and hero_id in hero_pos:
                bits.append(f"近期打过 {hero_pos[hero_id]}")
            lines.append("· " + " · ".join(bits))

    if ctx.notes:
        lines.append("")
        lines.append("【注意事项】")
        lines.extend(f"· {note}" for note in ctx.notes)

    # ---- 最近几轮对话 ----
    # 放在最后：它是**背景**，不是事实。写清楚这一点，免得模型把别人上一句
    # 里提到的数字当成本次查询的结果。
    if ctx.history:
        lines.append("")
        lines.append(
            "【最近对话】（只用来理解上文在说谁、在说哪件事；"
            "里面的数字都不是本次查到的数据）"
        )
        lines.extend(f"· {row}" for row in ctx.history[-6:])

    return "\n".join(lines)


CHAT_TOOL_REQUIREMENTS = """=== 回答要求 ===
1. 用中文口语化地回答，像群里老玩家聊天；不要写标题、不要用 markdown 表格、
   不要罗列「一、二、三」这种报告格式（除非用户明确要正式分析）。
2. **凡涉及具体数据的，一律先调工具查，再开口**。具体数据包括：某个人的战绩 /
   胜率 / KDA / 英雄 / 位置、英雄池、版本强势英雄、比赛详情、定时任务列表。
   上面的上下文里**没有任何战绩数字**，凭印象或记忆作答一律算答错。
   · 问「我的战绩」而上下文里【提问者】显示未绑定时，先如实说需要先绑定，
     不要拿别人的数据凑。
3. **只讲工具真正返回过的数字**。工具报错、超时、说「没取到」时，就如实说
   这块没拿到；工具里没有的字段（段位高低、对手水平、真实心情）不要补。
   · **胜负只能引用，不能反推**：工具返回的逐场战绩都自带主语（「该玩家胜 /
     该玩家负 / 该玩家胜负未知」），直接照搬这几个字就行。**绝对不要**根据
     KDA、补刀、经济、时长或「这数据看着该赢」去判断输赢 —— 把用户自己的
     胜局说成负局，是这类回答里最严重、也最容易犯的错。
   · 单场数据里的「天辉阵营获胜 / 夜魇阵营获胜」说的是**两个阵营**谁赢，
     跟某个玩家的胜负是两件事，不要混用、更不要拿它去覆盖球员的个人胜负。
4. 多人相关的问题（「群里谁最猛 / 谁最菜 / 谁在掉分」「昨天谁打得好」
   「我们昨晚开黑怎么样」）用 `compare_players` 一次调完，它给的分项排名与
   「同场局」就是横向比较的唯一依据。
   · 不同场次之间的 KDA / GPM **不能直接比高下**（对手强度、位置、时长都不同）；
     要讲就必须说明「这是跨场次的分项对比，不是同一局里的对位」。
   · 「谁跟谁一起开黑」只能看 `compare_players` 返回的「同场局」；没有就
     如实说「这几场没看到你们一起打」，不要替他们编队友。
   · 「开黑N人」只说明他这局跟 N 个人一起排，队友是不是群里人要靠同场局判断。
   · **没有 party 标记 ≠ 单排**：把它说成单排是编造，只有明确标了「单排」的才算。
5. 判断强弱时要避开这几个坑，必要时在回答里点一句：
   · GPM 受位置影响，辅助 / 游走位天然偏低，别只看 GPM 判强弱；
   · Turbo（加速）局的经济数值约为常规局的两倍，标了 Turbo 的场次不要跟常规局直接比；
   · 场次很少（个位数）时说明「样本少，仅供参考」。
   · 英雄池类工具返回的口径行会写明覆盖了哪个版本、共多少场、其中加速多少场。
     这群人多数对局是加速局，「加速占比很高」是常态、别当异常；但也别把加速局的
     胜率直接说成天梯强度，提胜率时把口径一起说清楚。
6. 如果用户在问「谁最菜」「谁最强」这类问题：先给结论和一句理由，再补上关键数据；
   可以调侃，但不要人身攻击。
7. 如果用户问的是练什么、怎么提升、或者想转位置：结合他的英雄池与近期数据给具体建议
   （先练哪个英雄、哪项能力、怎么练），不要只给网上通用套话。
   · 摘要里的「近期位置」来自他**每场比赛的真实位置记录**，是判断打什么位置的第一依据。
   · 用户自称打某个位置、但数据里的位置分布对不上时，**先把这个矛盾点出来**再给建议。
   · 给「转位置」建议时必须说清：现有英雄池里哪些英雄在目标位置能用、缺的是哪类英雄、
     先从哪个英雄上手。推荐英雄要挑他真打过、场次/胜率站得住的，**不要推荐一场没打过的**。
8. 如果问题与 Dota2 和本插件的数据都无关（例如问天气、写代码、聊别的），
   就直接正常聊天回答，不要为了用工具而硬查数据。
9. **时间口径**：
   · 上下文开头给了【当前时间】，一切相对时间以它为准，不要用你的记忆猜日期。
   · 本插件把**凌晨 4 点**作为一天的分界（玩家普遍熬夜）：0:00~3:59 打的局算
     **前一天**。所以「昨晚」包含凌晨那几局，不要按自然日 0 点理解。
   · 用户说了时间范围（「这三天」「昨天」「最近一周」）就**必须把它转成工具的
     `days` 参数**：「这三天」=3、「昨天」=1、「最近一周」=7。不填 days
     等于按「最近 N 场」统计，会把好几天前的局当成这段时间的成绩。
   · 工具返回里标着「时间未知」的场次不要替它猜日期；比赛时间是空的就承认不知道。
10. 需要比赛 ID 的工具（`query_match_detail`）：ID 要么用户直接给了，要么来自
    上下文里「本会话涉及过的比赛」。**不要编造比赛 ID**，拿不准就先问用户。
11. 篇幅控制在 400 字以内；用户明确要求详细分析时才展开。
12. 结尾不要反问「还需要我做什么吗」这类客套。"""


CHAT_REQUIREMENTS = """=== 回答要求 ===
1. 用中文口语化地回答，像群里老玩家聊天；不要写标题、不要用 markdown 表格、
   不要罗列「一、二、三」这种报告格式（除非用户明确要正式分析）。
2. 优先使用上面给出的真实数据，引用时把名字和数字说准。
3. **数据里没有的东西一律不要编造**，尤其是具体战绩、段位、英雄、数字。
   如果某人的数据标注了「数据获取失败」或「没有可用记录」，就直说拿不到。
4. 「参考分」是插件按胜率/KDA/GPM 粗略加权的锚点，不是官方评分。
   它只能用来排序，而且**必须**配合上面「横向对比」的分项数据一起讲：
   强弱常常是分项交叉的（一个人胜率领先、KDA 却垫底），
   只报一个总分等于糊弄用户。
5. 判断强弱时要避开这几个坑，必要时在回答里点一句：
   · GPM 受位置影响，辅助 / 游走位天然偏低，别只看 GPM 判强弱；
   · Turbo 局的经济数值约为常规局的两倍，标了 Turbo 的场次不要跟常规局直接比；
   · 场次很少（个位数）时说明「样本少，仅供参考」。
6. 如果用户在问「谁最菜」「谁最强」这类问题：先给结论和一句理由，
   再补上关键数据；可以调侃，但不要人身攻击。
7. 如果用户问的是练什么、怎么提升、或者想转位置：结合他的英雄池与近期数据给具体建议
   （先练哪个英雄、哪项能力、怎么练），不要只给网上通用套话。
   · 每个人摘要里的「近期位置」来自他**每场比赛的真实位置记录**，
     是判断他打什么位置的第一依据；逐场明细里也标了当场位置。
   · 如果用户自称打某个位置、但数据里的位置分布对不上，**先把这个矛盾点出来**，
     再基于数据给建议；不要顺着他的说法编。
   · 给「转位置」建议时必须说清三件事：现有英雄池里哪些英雄在目标位置能用、
     缺的是哪一类英雄、先从哪个英雄上手。
     推荐英雄要挑他真打过、且场次/胜率站得住的，**不要推荐一场没打过的**。
   · 位置标着「未知」的场次就是没有记录，不要替它猜一个位置；
     位置样本少（个位数场次）时说明「样本少」。
8. 如果问题与 Dota2 和上面的数据都无关（例如问天气、写代码、聊别的），
   就直接正常聊天回答，不要硬扯数据。
9. **时间口径**：上下文开头给了【当前时间】，每场比赛也带了自己的本地时间与
   「今天 / 昨天 / 前天」标注，一切相对时间都以此为准，不要用你的记忆去猜日期。
   · 本插件把**凌晨 4 点**作为一天的分界（玩家普遍熬夜）：0:00~3:59 之间打的局
     算**前一天**。所以「昨晚」是包含凌晨那几局的，不要按自然日的 0 点去理解。
   · 由此会看到「昨天 02:40」排在「昨天 23:44」**前面**（它是次日凌晨打的，
     确实更晚），这是正常的，不要因为时刻看着小就把它当成更早的场次。
   · 如果上下文声明了【时间窗口】（例如用户问「昨天」对应 09-16），
     那就是**只统计窗口内的场次**：某人窗口内 0 场，就直说「他昨天没打」，
     绝不能拿窗口外的数据凑数——那是答非所问，还会让用户以为你在编。
   · 标着「时间未知」的场次不要替它猜日期；比赛时间是空的就承认不知道。
10. **开黑 / 同场口径**（回答「昨天群里开黑谁最牛逼」这类问题的关键）：
   · 「同场局」= 同一场比赛里出现了本次取到数据的两名以上成员，这是
     **本群开黑的确凿证据**，也是唯一能直接横向比高下的场景：同队就看谁贡献大
     （KDA / GPM / 参团），对位就看谁赢了那一局。
   · 不同场次之间的 KDA / GPM **不能直接比高下**（对手强度、位置、时长都不同），
     要讲就必须说明「这是跨场次的分项对比，不是同一局里的对位」。
   · 「开黑N人」只说明他这局跟 N 个人一起排，**队友是不是群里人要靠「同场局」判断**；
     没有同场局就说「这几场没看到你们一起打」，不要替他编队友。
   · **没有 party 标记 ≠ 单排**：数据源只有部分场次带组队信息，
     摘要里的「组队情况未知 N 场」就是那些；把它说成单排是编造。
     只有明确标了「单排」的才算单排。
11. 篇幅控制在 400 字以内；用户明确要求详细分析时才展开。
12. 结尾不要反问「还需要我做什么吗」这类客套。"""


def build_chat_prompt(question: str, ctx: ChatContext, *, tooled: bool = False) -> str:
    """拼出最终发给模型的用户提示词。

    Args:
        tooled: ``True`` 走**带工具**的自然语言主路径 —— 数据靠模型自己调工具
            取，上下文里只给事实底表，回答要求也换成
            :data:`CHAT_TOOL_REQUIREMENTS`。``False`` 是给定时播报用的
            「数据已预取好、单轮回答」模式，沿用 :data:`CHAT_REQUIREMENTS`。
    """
    blocks = [
        "=== 用户的问题 ===",
        (question or "").strip(),
        "",
        format_context_block(ctx, tooled=tooled),
        "",
        CHAT_TOOL_REQUIREMENTS if tooled else CHAT_REQUIREMENTS,
    ]
    return "\n".join(blocks)


# ======================================================================
# 数据收集
# ======================================================================


async def _fetch_snapshot(
    api: Any,
    account_id: int,
    name: str,
    relation: str,
    limit: int,
    timeout: float,
) -> PlayerSnapshot:
    """拉一位玩家的近期战绩；任何失败都转成带 ``error`` 的快照。"""
    try:
        result = await asyncio.wait_for(
            api.get_player_matches_enriched(account_id, limit), timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.warning(f"[dota2] 闲聊兜底：拉取 {account_id} 近期战绩超时")
        return PlayerSnapshot(
            account_id=account_id, name=name, relation=relation, error="请求超时"
        )
    except Exception as e:  # noqa: BLE001 - 数据源任何异常都不该打断回答
        logger.warning(f"[dota2] 闲聊兜底：拉取 {account_id} 近期战绩失败: {e}")
        return PlayerSnapshot(
            account_id=account_id,
            name=name,
            relation=relation,
            error=f"{type(e).__name__}",
        )

    matches = result[0] if isinstance(result, tuple) else result
    return PlayerSnapshot(
        account_id=account_id,
        name=name,
        relation=relation,
        matches=[m for m in (matches or []) if isinstance(m, dict)],
    )


async def _fetch_hero_history(
    api: Any,
    account_id: int,
    timeout: float,
    *,
    pool_cfg: dict | None = None,
) -> tuple[list[dict], str]:
    """取提问者的英雄池，返回 ``(英雄行, 口径说明)``。

    走 :func:`dota_pool.collect_hero_pool`：**只看当前版本**（样本不足时
    自动并入更早的版本并说明），且**包含加速模式** —— 群里多数人的对局
    是加速局，按旧口径（``/players/{id}/heroes`` 默认返回）他们的英雄池
    几乎是空的，模型据此说「他没什么在玩的英雄」就完全是冤枉。
    """
    cfg = dict(pool_cfg or {})
    try:
        pool = await asyncio.wait_for(
            dota_pool.collect_hero_pool(
                api,
                account_id,
                min_games=int(cfg.get("min_games") or dota_pool.DEFAULT_MIN_GAMES),
                max_patches=int(cfg.get("max_patches") or dota_pool.DEFAULT_MAX_PATCHES),
                include_turbo=cfg.get("include_turbo", True) is not False,
                patch_scope=cfg.get("patch_scope", True) is not False,
                timeout=max(1.0, float(timeout or dota_pool.DEFAULT_TIMEOUT)),
            ),
            # 内部每个请求各有超时，这里再兜一层整体超时（最多查 N 个版本）
            timeout=max(1.0, float(timeout or dota_pool.DEFAULT_TIMEOUT)) * 3,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[dota2] 闲聊兜底：拉取 {account_id} 英雄池失败: {e}")
        return [], ""
    if not pool.has_data:
        return [], ""
    return pool.rows, dota_pool.hero_pool_scope_text(pool)


async def collect_chat_context(
    api: Any,
    *,
    question: str,
    umo: str = "",
    user_id: str = "",
    watchers: Iterable[dict] = (),
    self_binding: dict | None = None,
    bindings: Iterable[dict] = (),
    recent_matches: Iterable[dict] = (),
    focus_accounts: Iterable[int] | None = None,
    recent_limit: int = DEFAULT_RECENT_LIMIT,
    max_players: int = DEFAULT_MAX_PLAYERS,
    timeout: float = DEFAULT_FETCH_TIMEOUT,
    cache: dict | None = None,
    cache_ttl: float = DEFAULT_CACHE_TTL,
    localizer: Any = None,
    now: float | None = None,
    hero_pool_cfg: dict | None = None,
    local_only: bool = False,
    history: Iterable[str] = (),
) -> ChatContext:
    """收集一次闲聊回答所需的插件数据。

    Args:
        api: 插件的数据源门面（含 ``get_player_matches_enriched`` 等方法）。
        question: 剥掉唤醒词之后的用户原话。
        watchers: **本会话**的监听项列表（调用方负责按 umo 过滤）。
        self_binding: 提问者在**本会话**的绑定，没有则 None。
        bindings: 本会话的绑定列表（展示用）。
        recent_matches: 本会话近期涉及过的比赛（监听推送 / 复盘 / 战绩查询），
            形如 ``{match_id, desc, start_time}``。纯本地数据，直接注入。
        focus_accounts: 只取这几个账号的数据（``account_id`` 列表）。
            调用方在「问题只点名了一个人、且没有横向对比语义」时用它收窄范围，
            省掉一轮无用请求。留空表示按常规规则取（本人 + 监听列表）。
        cache: 可选的战绩缓存 ``{(account_id, limit): (时间戳, 快照)}``，
            由调用方持有，跨消息复用；不传则不缓存。
        now: 回答时刻（时间戳）。测试可传入固定值，生产不传即取当前时间。
        hero_pool_cfg: 英雄池口径配置（``min_games`` / ``max_patches`` /
            ``include_turbo`` / ``patch_scope``），由插件主体从配置读好传入；
            不传则用 :mod:`dota_pool` 的默认值。
        local_only: ``True`` 时**只返回本地事实底表**（时间、名单、本会话
            涉及过的比赛），不打任何接口、也不做 needs / 时间窗口判断。
            自然语言主路径（带工具的模型）用这个 —— 数据由模型自己调工具取。
        history: 最近几轮对话（``["用户: …"]``），给模型消解指代用。

    Returns:
        :class:`ChatContext`。**不会抛异常** —— 任何一块数据拿不到，
        都只是少一段上下文 + 多一条 note。
    """
    watcher_list = [w for w in watchers if isinstance(w, dict)]
    binding_list = [b for b in bindings if isinstance(b, dict)]
    ctx = ChatContext(
        question=question or "",
        umo=umo,
        user_id=user_id,
        self_binding=self_binding,
        bindings=binding_list,
        watchers=watcher_list,
        now=_now_ts(now),
        session_matches=[r for r in recent_matches if isinstance(r, dict)],
        history=[str(row) for row in history if str(row or "").strip()],
    )

    self_account = (
        _as_int(self_binding.get("account_id")) if self_binding else None
    )

    if local_only:
        # 自然语言主路径（带工具的模型）：数据一律由模型自己调工具取，
        # 这里只给**事实底表** —— 当前时间、名单、本会话涉及过的比赛。
        #
        # 为什么连 needs / 时间窗口都不算：这三个判断都是**关键词正则**在做
        # 「用户想要什么数据」，而正则永远兜不住新说法（「给群里这三天的
        # 战绩做个总结」就因为没收录「三天」而被判成「不打接口」）。既然
        # 模型手上已经有全套工具，要什么由它自己决定，插件不猜。
        return ctx

    ctx.needs = detect_needs(
        question,
        names=[str(w.get("personaname") or "") for w in watcher_list],
        has_self=self_account is not None,
    )

    # ---- 时间窗口 ----
    # 时间词本身不构成「要看数据」的理由（「今天天气怎么样」也带时间词），
    # 必须配上「局 / 盘 / 玩 / 谁 / 开黑」这类只有放在时间段里才成立的词，
    # 才认定用户是在问那段时间的比赛 —— 这时答案全在对局记录里，
    # 而这类问法往往不带「战绩」二字。
    ctx.window = detect_time_window(question, ctx.now)
    if ctx.window is not None and WINDOW_MATCH_HINT_RE.search(question or ""):
        ctx.needs.add(NEED_RECENT)

    if not ctx.needs:
        # 跟数据无关的闲聊：只带本地名单，直接回答，不打任何接口
        return ctx

    # ---- 候选人：本人优先，然后是监听列表 ----
    candidates: list[tuple[int, str, str]] = []
    if self_account:
        candidates.append(
            (
                self_account,
                str((self_binding or {}).get("personaname") or "本人"),
                "本人",
            )
        )
    skipped = 0
    seen = {account for account, _, _ in candidates}
    for watcher in watcher_list:
        account_id = _as_int(watcher.get("account_id"))
        if account_id is None or account_id in seen:
            continue
        if len(candidates) >= max(1, max_players):
            skipped += 1
            continue
        seen.add(account_id)
        candidates.append(
            (
                account_id,
                str(watcher.get("personaname") or "未命名"),
                "被监听",
            )
        )

    # ---- 收窄到指定的人（工具模式下的按需取数）----
    # 这一步必须在 candidates 建好之后、英雄表与战绩之前：省的是网络请求，
    # 晚一步做就白拉了。收窄是调用方**主动**决定的（问题只点名了一个人），
    # 所以不该再往上下文里塞「还有 N 位没取数据」——那句话会让模型以为
    # 数据被截断了，从而不敢下结论。
    wanted = [int(acc) for acc in (focus_accounts or []) if _as_int(acc)]
    if wanted:
        by_account = {account: (account, name, relation) for account, name, relation in candidates}
        focused = [by_account[acc] for acc in wanted if acc in by_account]
        if focused:
            candidates = focused
            skipped = 0
            ctx.notes.append(
                "本次只取了问题里**点名提到**的那位玩家的数据"
                "（其他成员的数据没有拉取）。"
            )

    if skipped:
        ctx.notes.append(
            f"本会话还有 {skipped} 位被监听玩家没有取数据"
            f"（一次最多分析 {max(1, max_players)} 人）。"
            "因此「谁跟谁同场开黑」只能在这几位之间判断，"
            "没取数据的那些不能算作「没一起打」。"
        )

    # ---- 英雄常量表（只在需要英雄名时拉，失败就用「英雄#id」） ----
    if candidates:
        try:
            heroes = await asyncio.wait_for(api.get_heroes(), timeout=timeout)
            if isinstance(heroes, dict):
                # localizer 由调用方注入（插件的中文名服务），不给就保持原名
                if localizer is not None:
                    heroes = await localizer(heroes)
                ctx.heroes = heroes
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dota2] 闲聊兜底：拉取英雄常量表失败: {e}")
            ctx.notes.append("英雄名称表没取到，英雄以 ID 展示。")

    # ---- 战绩快照 ----
    if NEED_RECENT in ctx.needs and candidates:
        # 有时间窗口就放宽取数：默认 10 场常常只覆盖一两天，
        # 「上周谁打得好」会因为手上根本没有那段数据而得出「没打过」的假结论。
        fetch_limit = recent_limit
        if ctx.window is not None:
            fetch_limit = max(recent_limit, WINDOW_FETCH_LIMIT)
        ctx.snapshots = await _collect_snapshots(
            api, candidates, fetch_limit, timeout, cache, cache_ttl
        )
        for snapshot in ctx.snapshots:
            snapshot.window = ctx.window
        failed = [s.name for s in ctx.snapshots if s.error]
        if failed:
            ctx.notes.append(
                "以下玩家本次没能取到数据：" + "、".join(failed) + "。"
            )
        if any(s.cached for s in ctx.snapshots):
            ctx.notes.append(
                f"部分战绩来自 {int(cache_ttl // 60)} 分钟内的缓存，"
                "如果用户问的是刚刚打完的局，可能还没算进去。"
            )
        if ctx.window is not None:
            # 手上数据够不够覆盖这个窗口，必须让模型知道：
            # 否则它会把「取数范围内的 0 场」当成「这个人那段时间真的没打」。
            coverage = min(
                (
                    _as_int(m.get("start_time")) or 0
                    for s in ctx.snapshots
                    for m in s.matches
                ),
                default=0,
            )
            if coverage and coverage > ctx.window.start:
                ctx.notes.append(
                    f"每人只取了最近 {fetch_limit} 场，手上的记录最早到 "
                    f"{time_label(coverage, ctx.now)}，"
                    f"**更早于这个时刻的场次根本没取到** —— 因此不能断言"
                    f"「{ctx.window.label}没打过」以外的否定结论。"
                )
            if not any(s.scoped for s in ctx.snapshots):
                ctx.notes.append(
                    f"本次取到数据的玩家，在「{ctx.window.label}」这个时间段里"
                    "**都没有**比赛记录。"
                )

    # ---- 英雄池（只对提问者本人） ----
    if NEED_HEROES in ctx.needs and self_account:
        ctx.hero_history, ctx.hero_pool_scope = await _fetch_hero_history(
            api, self_account, timeout, pool_cfg=hero_pool_cfg
        )
        if not ctx.hero_history:
            ctx.notes.append("提问者的英雄池数据没取到。")

    return ctx


async def _collect_snapshots(
    api: Any,
    candidates: list[tuple[int, str, str]],
    recent_limit: int,
    timeout: float,
    cache: dict | None,
    cache_ttl: float,
) -> list[PlayerSnapshot]:
    """并发拉取所有候选人的战绩快照，命中缓存则直接复用。

    缓存里只存 ``(时间戳, matches)`` 这种**纯数据**，快照对象每次都新建。
    原因是缓存由插件实例持有、**跨会话共享**：同一个账号在 A 群是「本人」、
    在 B 群是「被监听」，昵称也可能不同。若把快照对象本身缓存起来复用，
    一个会话的标注会污染另一个会话。
    """
    limit = max(1, int(recent_limit))
    now = time.time()
    pending: list[tuple[int, str, str]] = []
    results: dict[int, PlayerSnapshot] = {}

    for account_id, name, relation in candidates:
        entry = cache.get((account_id, limit)) if cache is not None else None
        if entry and now - entry[0] < cache_ttl:
            results[account_id] = PlayerSnapshot(
                account_id=account_id,
                name=name,
                relation=relation,
                matches=list(entry[1]),
                cached=True,
            )
            continue
        pending.append((account_id, name, relation))

    if pending:
        snapshots = await asyncio.gather(
            *[
                _fetch_snapshot(api, account_id, name, relation, limit, timeout)
                for account_id, name, relation in pending
            ]
        )
        for snapshot in snapshots:
            results[snapshot.account_id] = snapshot
            if cache is not None and not snapshot.error:
                cache[(snapshot.account_id, limit)] = (now, list(snapshot.matches))

    # 保持传入顺序（本人优先），方便模型对照
    return [
        results[account_id] for account_id, _, _ in candidates if account_id in results
    ]


def build_chat_system_prompt(configured: str = "") -> str:
    """系统提示词：配置项优先，留空用内置默认。"""
    text = str(configured or "").strip()
    return text or DEFAULT_CHAT_SYSTEM_PROMPT
