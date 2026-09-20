"""定时任务的口径层：把「每天早上七点通报群里战绩情况」翻译成可执行的任务。

这里只做**纯逻辑**（不碰网络、不碰平台），因此可以单独跑回归：

1. :func:`parse_time_spec` —— 中文时间表达 → 5 段 cron 或一次性 ``run_at``；
2. :func:`parse_request` —— 整句话 → 任务请求（时间 + 动作 + 参数，或「每 N 盘」计数型）；
3. :func:`cron_label` / :func:`format_task_list` —— 给用户看的文案；
4. :func:`build_report_question` / :func:`build_watch_summary_prompt` —— 到点执行时的提示词。

### 为什么计数型任务不建 cron

「每监听到十盘战绩就生成一份总结」是**按事件计数**触发的，cron 只能表达时间，
硬凑（例如每天跑一次、看计数有没有满十）会把「攒够十盘就发」拖成「第二天早上才发」。
所以计数型任务存在插件自己的存储里，由监听推送链路计数触发；时间型任务才走
AstrBot 的「未来任务」。两种任务在 ``/d2 定时`` 里是同一个列表、同一套管理动作。

### 时间解析的边界（刻意保守）

* **必须同时出现「频率词」与「钟点」才算时间**（``每天`` + ``7点``）。只出现
  「10 点」不认 —— 那句话更可能是在说「10 场」「10 分钟」。
* 只说频率、没给钟点（「每天早上通报战绩」）→ 用配置里的默认时刻（默认 07:00），
  并在确认文案里**明确写出**用的是默认值，不让用户以为自己指定过。
* 12 小时制靠前缀消歧：``早上/上午/凌晨`` 原样，``中午`` 归 12，
  ``下午/傍晚/晚上/夜里`` 小于 12 的加 12。**没有前缀就原样**（「7点」= 07:00，
  「20点」= 20:00）—— 宁可按字面来，也不要自作聪明把「7点」当成 19:00。
"""

from __future__ import annotations

import datetime as _dt
import random
import re
import string
from dataclasses import dataclass
from typing import Any, Iterable

#: 动作名。到点执行时各自走不同的取数与渲染链路。
ACTION_GROUP = "group"
ACTION_PLAYER = "player"
ACTION_META = "meta"
#: 计数型任务的专用动作名（由 :data:`ACTION_GROUP` 转换而来）。
ACTION_WATCH_SUMMARY = "watch_summary"

ACTION_LABELS = {
    ACTION_GROUP: "通报群里战绩情况",
    ACTION_PLAYER: "通报指定玩家的近期表现",
    ACTION_META: "推送当前版本强势英雄榜",
    ACTION_WATCH_SUMMARY: "生成这 N 场的战绩总结",
}

#: 默认识别的钟点（只说频率、没给时间时用）。
DEFAULT_TIME = "07:00"

#: 计数型任务的 N 上限（防止有人写「每一盘」把群里刷屏）。
MAX_EVERY = 50

#: 计数型任务的 N 默认值（说了「每监听…做总结」但没给数字时）。
DEFAULT_EVERY = 10


# ======================================================================
# 中文数字
# ======================================================================
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "壹": 1, "两": 2, "二": 2, "贰": 2,
    "三": 3, "叁": 3, "四": 4, "肆": 4, "五": 5, "伍": 5,
    "六": 6, "陆": 6, "七": 7, "柒": 7, "八": 8, "捌": 8, "九": 9, "玖": 9,
}


def cn_to_int(token: str) -> int | None:
    """把「七 / 十 / 十二 / 二十」这类中文数字转成整数。

    只覆盖 0~99 这个范围 —— 定时任务里出现的数字（点、分、号、盘）都在里面。
    """
    token = str(token or "").strip()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    if token == "十":
        return 10
    if "十" in token:
        head, _, tail = token.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        if head and head not in _CN_DIGITS:
            return None
        if tail and tail not in _CN_DIGITS:
            return None
        return tens * 10 + ones
    if len(token) == 1:
        return _CN_DIGITS.get(token)
    # 「一七」这种连写不认，交给调用方放弃
    return None


# ======================================================================
# 时间表达
# ======================================================================
WEEKDAY_WORDS = {
    "一": "mon", "1": "mon", "二": "tue", "2": "tue", "三": "wed", "3": "wed",
    "四": "thu", "4": "thu", "五": "fri", "5": "fri", "六": "sat", "6": "sat",
    "日": "sun", "天": "sun", "7": "sun", "0": "sun",
}
WEEKDAY_LABELS = {
    "mon": "周一", "tue": "周二", "wed": "周三", "thu": "周四",
    "fri": "周五", "sat": "周六", "sun": "周日",
}

#: 频率词 → 周期类型
_DAILY_RE = re.compile(r"每天|每日|天天|每晚|每天晚|每日晚")
#: 「每周」与「周几」**必须分开匹配**。
#:
#: 合成一个正则（``每周|周([一二三四五六日天])``）时，``search`` 命中的是
#: 排在前面的那个分支 —— 「每周五晚上十一点」里先匹配到「每周」，周几就此丢失，
#: 任务被建到周一。多等一位：先找具体周几，找不到才按「每周」兜底。
_WEEKLY_ANY_RE = re.compile(r"每周|每星期|每个星期|一周一次|每周一次")
_WEEKLY_DAY_RE = re.compile(
    r"周([一二三四五六日天0-9])|星期([一二三四五六日天0-9])|礼拜([一二三四五六日天0-9])"
)
#: 月份表达与「几号」**必须分开**。
#:
#: 早先把它们写成一个正则（``每?月?(\d+)[号日]`` 那种形状），结果
#: 「3 日没打了」里的「3 日」被当成「每月 3 号」—— 只要句子里再出现一个
#: 每天/每周，就会被这条抢先生效，生成一个用户根本没要的月度任务。
#: 现在要求必须先出现「每月 / 每个月」，再在同一句里找「几号」。
_MONTHLY_RE = re.compile(r"每\s*个?\s*月")
_MONTH_DAY_RE = re.compile(r"(\d{1,2}|[一二三四五六七八九十]{1,3})\s*[号日]")
_INTERVAL_RE = re.compile(
    r"每\s*(?:隔)?\s*(\d{1,3}|[一二三四五六七八九十]{1,3})\s*个?\s*小时"
)
_HOURLY_RE = re.compile(r"每小时|每个小时")
_ONCE_RE = re.compile(r"今天|明天|后天|今晚|明晚|明早|今早|今天早上|明天早上")

#: 钟点前缀 → 小时修正
_AM_PREFIX_RE = re.compile(r"凌晨|清晨|早晨|早上|上午|早")
_PM_PREFIX_RE = re.compile(r"下午|傍晚|晚上|夜里|夜间|晚")
_NOON_RE = re.compile(r"中午|正午")

#: 钟点：``7点`` / ``7点半`` / ``7点30`` / ``7:30`` / ``7点30分``
_CLOCK_RE = re.compile(
    r"(?P<h>\d{1,2}|[一二三四五六七八九十]{1,3})"
    r"\s*(?:[点时]|[:：])"
    r"\s*(?:(?P<m>\d{1,2}|[一二三四五六七八九十]{1,3}|半)\s*分?)?"
)

#: 周末 / 工作日
_WEEKEND_RE = re.compile(r"周末|星期六日|周六周日")
_WEEKDAY_RANGE_RE = re.compile(r"工作日|周一至周五|星期一到星期五")


@dataclass
class Clock:
    """解析出来的钟点。"""

    hour: int
    minute: int
    raw: str = ""

    def text(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"


@dataclass
class TimeSpec:
    """时间规格：要么给 cron，要么给一次性 ``run_at``。"""

    mode: str  # daily | weekly | monthly | interval | once
    cron: str | None = None
    run_at: _dt.datetime | None = None
    label: str = ""
    #: 用户没写钟点、用了配置里的默认值 —— 确认文案必须说明这一点
    used_default_time: bool = False
    detail: str = ""

    @property
    def once(self) -> bool:
        return self.mode == "once"


def parse_clock(text: str) -> Clock | None:
    """从文本里抽一个钟点。没有明确钟点返回 ``None``。"""
    if _NOON_RE.search(text):
        return Clock(12, 0, "中午")
    for match in _CLOCK_RE.finditer(text):
        hour = cn_to_int(match.group("h"))
        if hour is None or hour > 24:
            continue
        raw_minute = match.group("m")
        if raw_minute in (None, ""):
            minute = 0
        elif raw_minute == "半":
            minute = 30
        else:
            minute = cn_to_int(raw_minute)
            if minute is None or minute > 59:
                continue
        if hour == 24:
            hour = 0
        # 前缀消歧只看钟点**前面**那一小段，避免把后面的话算进来
        head = text[max(0, match.start() - 4): match.start()]
        if _NOON_RE.search(head):
            hour = 12
        elif _AM_PREFIX_RE.search(head):
            pass  # 凌晨 3 点就是 3 点，不修正
        elif _PM_PREFIX_RE.search(head) and hour < 12:
            hour += 12
        return Clock(hour, minute, match.group(0).strip())
    return None


def parse_time_spec(text: str, *, default_time: str = DEFAULT_TIME) -> TimeSpec | None:
    """把一句话解析成 :class:`TimeSpec`；没有时间表达返回 ``None``。

    判定顺序是有讲究的：

    1. 「每小时 / 每隔 N 小时」这类**间隔**先判 —— 它们的「每」后面跟的是
       时间单位，而后面几个分支的「每」跟的是频率词，互不冲突，但先判更稳；
    2. 只有**整句都没有周期词**（每天 / 每周 / 每月 / 每隔）时，才考虑
       「今天 / 明天」这类一次性表达。
       反例：「每晚十点**总结一下大家今天**的表现」—— 句尾的「今天」是
       在说统计范围，不是「只做一次」；早先没这道闸门，它被建成了今晚 22:00
       的一次性任务，第二天就不会再响；
    3. 再判 每周 / 每月 / 每天。

    Args:
        text: 用户原话（已剥离唤醒词）。
        default_time: 只说频率、没说钟点时用的默认时刻，``HH:MM`` 格式。
    """
    raw = str(text or "")
    if not raw:
        return None

    clock = parse_clock(raw)

    # ---- 1. 每隔 N 小时 / 每小时 ----
    interval = _INTERVAL_RE.search(raw)
    if interval:
        hours = cn_to_int(interval.group(1))
        if hours and 1 <= hours <= 23:
            return TimeSpec(
                "interval",
                cron=f"0 */{hours} * * *",
                label=f"每 {hours} 小时一次",
                detail=interval.group(0).strip(),
            )
    if _HOURLY_RE.search(raw):
        return TimeSpec(
            "interval", cron="0 * * * *", label="每小时一次", detail="每小时"
        )

    recurring = bool(
        _DAILY_RE.search(raw) or _WEEKLY_ANY_RE.search(raw) or _MONTHLY_RE.search(raw)
    )

    # ---- 2. 一次性：今天/明天/后天 + 钟点（有周期词就不算一次性）----
    once_word = _ONCE_RE.search(raw) if not recurring else None
    if once_word and clock is not None:
        return _build_once(raw, once_word.group(0), clock)

    # ---- 3. 每月 ----
    monthly = _MONTHLY_RE.search(raw)
    if monthly:
        day_match = _MONTH_DAY_RE.search(raw)
        day = cn_to_int(day_match.group(1)) if day_match else None
        if not day or not 1 <= day <= 31:
            # 「每月」没说是几号：按 1 号算，文案里写清楚
            day = 1
        hour, minute, used_default = _resolve_clock(clock, default_time)
        return TimeSpec(
            "monthly",
            cron=f"{minute} {hour} {day} * *",
            label=f"每月 {day} 日 {hour:02d}:{minute:02d}",
            used_default_time=used_default,
            detail=monthly.group(0).strip(),
        )

    # ---- 4. 每周 ----
    if _WEEKEND_RE.search(raw):
        hour, minute, used_default = _resolve_clock(clock, default_time)
        return TimeSpec(
            "weekly",
            cron=f"{minute} {hour} * * sat,sun",
            label=f"每周六、周日 {hour:02d}:{minute:02d}",
            used_default_time=used_default,
            detail="周末",
        )
    if _WEEKDAY_RANGE_RE.search(raw):
        hour, minute, used_default = _resolve_clock(clock, default_time)
        return TimeSpec(
            "weekly",
            cron=f"{minute} {hour} * * mon-fri",
            label=f"每周一至周五 {hour:02d}:{minute:02d}",
            used_default_time=used_default,
            detail="工作日",
        )
    weekly_any = _WEEKLY_ANY_RE.search(raw)
    weekly_day = _WEEKLY_DAY_RE.search(raw)
    if weekly_any or weekly_day:
        token = ""
        if weekly_day:
            token = weekly_day.group(1) or weekly_day.group(2) or weekly_day.group(3) or ""
        code = WEEKDAY_WORDS.get(str(token or "")) or "mon"
        hour, minute, used_default = _resolve_clock(clock, default_time)
        return TimeSpec(
            "weekly",
            cron=f"{minute} {hour} * * {code}",
            label=f"每{WEEKDAY_LABELS.get(code, code)} {hour:02d}:{minute:02d}",
            used_default_time=used_default,
            detail=(weekly_day.group(0) if weekly_day else weekly_any.group(0)),
        )

    # ---- 5. 每天 ----
    daily = _DAILY_RE.search(raw)
    if daily:
        hour, minute, used_default = _resolve_clock(clock, default_time)
        return TimeSpec(
            "daily",
            cron=f"{minute} {hour} * * *",
            label=f"每天 {hour:02d}:{minute:02d}",
            used_default_time=used_default,
            detail=daily.group(0),
        )
    return None


def _resolve_clock(clock: Clock | None, default_time: str) -> tuple[int, int, bool]:
    """定下钟点：用户给了就用用户的，没给就用默认值（并标出来）。"""
    if clock is not None:
        return clock.hour, clock.minute, False
    hour, minute = _parse_default_time(default_time)
    return hour, minute, True


def _parse_default_time(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*", str(value or ""))
    if not match:
        return 7, 0
    hour = min(23, int(match.group(1)))
    minute = min(59, int(match.group(2)))
    return hour, minute


def once_cron(run_at: _dt.datetime) -> str:
    """把一次性时刻翻译成「钉死日期」的 cron。

    为什么要绕这一下：AstrBot 的 ``add_basic_job`` **没有** ``run_once`` /
    ``run_at`` 参数（那是 ``add_active_job`` 的，而后者会把活交给 AstrBot 的
    主智能体，插件接管不了）。所以一次性任务用「某月某日某时」的 cron 表达：
    ``0 8 21 9 *`` = 9 月 21 日 08:00。执行完由 handler 自己把它删掉。
    万一那次没跑成（插件没启动 / 模型不可用），最坏结果是**明年同一天再响一次**，
    比「静默丢掉用户的任务」好。
    """
    return f"{run_at.minute} {run_at.hour} {run_at.day} {run_at.month} *"


def _build_once(raw: str, word: str, clock: Clock) -> TimeSpec:
    """「明天早上八点」这类一次性任务。"""
    now = _dt.datetime.now()
    offset = 0
    if "后天" in word:
        offset = 2
    elif "明天" in word or "明晚" in word or "明早" in word:
        offset = 1
    run_at = (now + _dt.timedelta(days=offset)).replace(
        hour=clock.hour, minute=clock.minute, second=0, microsecond=0
    )
    if run_at <= now:
        # 「今天 8 点」但已经过了 8 点 ⇒ 顺延到明天，并且**明说**顺延了
        run_at += _dt.timedelta(days=1)
        label = f"{run_at.strftime('%Y-%m-%d %H:%M')}（今天这个时间已过，顺延到明天）"
    else:
        label = run_at.strftime("%Y-%m-%d %H:%M")
    return TimeSpec(
        "once",
        cron=once_cron(run_at),
        run_at=run_at,
        label=label,
        detail=word + clock.raw,
    )


def cron_label(cron: str) -> str:
    """把 5 段 cron 反渲染成人话（给任务列表用）。认不出来就原样返回。"""
    parts = str(cron or "").split()
    if len(parts) != 5:
        return str(cron or "")
    minute, hour, day, month, dow = parts
    if not minute.isdigit() or not hour.isdigit():
        if minute == "0" and hour.startswith("*/"):
            return f"每 {hour[2:]} 小时一次"
        if minute == "0" and hour == "*":
            return "每小时整点"
        return str(cron)
    clock = f"{int(hour):02d}:{int(minute):02d}"
    if dow != "*":
        names = [WEEKDAY_LABELS.get(x, x) for x in dow.split(",")]
        return f"每{'、'.join(names)} {clock}"
    if day != "*" and month == "*":
        return f"每月 {day} 日 {clock}"
    if day != "*" and month != "*":
        # 钉死日期的 cron 就是一次性任务（见 :func:`once_cron`）
        return f"{int(month)} 月 {int(day)} 日 {clock}（一次性）"
    return f"每天 {clock}"


# ======================================================================
# 任务请求
# ======================================================================
#: 计数型任务的特征词
_WATCH_WORD_RE = re.compile(r"监听")
_SUMMARY_WORD_RE = re.compile(r"总结|汇总|小结|报告|回顾|复盘|统计")
#: 「每（监听到）N 盘」。
#:
#: 中间那几个词必须**连着数词**才算数：「每监听到十盘」要能匹配，
#: 而「每天晚上十点」不能 —— 后者里的「天」既不是可选词也不是数词，
#: 正则走不下去，于是不会被误判成计数型任务。
_EVERY_COUNT_RE = re.compile(
    r"每\s*(?:隔)?\s*(?:(?:监听到|监听|收到|攒够|满|有|打了|打完)\s*)?"
    r"(\d{1,3}|[一二三四五六七八九十]{1,3})\s*(?:盘|场|把|局|条)"
)

#: 动作特征词。顺序敏感：先判版本榜，再判个人，最后兜底群通报。
_META_WORDS = ("轮椅", "版本强势", "版本答案", "强势英雄", "胜率榜", "版本榜")
_PLAYER_WORDS = ("表现", "发挥", "状态", "分析", "水平", "怎么样")
_GROUP_WORDS = (
    "战绩情况", "战绩通报", "通报战绩", "战报", "播报", "汇报", "通报",
    "群里", "大家", "所有人", "全员", "汇总", "总结", "统计",
)
#: 只是「提醒 / 闹钟」而不是查数据 —— 这类请求插件做不到，要明确拒绝。
_REMIND_ONLY_WORDS = ("提醒我", "闹钟", "叫我起床", "备忘")

#: 「这句话跟 Dota2 数据有关吗」的宽松判据。
#:
#: 只用于**一件事**：把「每天七点提醒我喝水」这类纯提醒挡在外面。
#: 宽松是关键 —— 用户说「每隔 2 小时提醒我看看战绩」，加了「提醒」，
#: 但主题是战绩，仍然该接。早先这条判据收得太紧（只认「通报 / 播报 / 群里」
#: 那几个动作词），把后半句也一并拒了。
_DOTA_HINT_WORDS = (
    "战绩", "战报", "比赛", "对局", "开黑", "英雄", "轮椅", "段位", "资料",
    "表现", "发挥", "状态", "分析", "统计", "总结", "汇总", "通报", "播报",
    "汇报", "监听", "队友", "群友", "上分", "胜率",
)


@dataclass
class TaskRequest:
    """一句话解析出来的任务请求。"""

    kind: str  # cron | watch_count
    action: str = ACTION_GROUP
    args: str = ""
    time: TimeSpec | None = None
    every: int = DEFAULT_EVERY
    label: str = ""
    raw: str = ""

    def describe(self) -> str:
        """给用户看的确认文案。"""
        what = ACTION_LABELS.get(self.action, self.action)
        if self.args:
            what = f"{what}（{self.args}）"
        if self.kind == "watch_count":
            return (
                f"每监听到 {self.every} 场比赛，就为这个群生成一份这 "
                f"{self.every} 场的总结"
            )
        if self.time is None:  # pragma: no cover - cron 型必然有时间
            return what
        return f"{self.time.label} —— {what}"


def parse_request(
    text: str,
    *,
    known_names: Iterable[str] = (),
    default_time: str = DEFAULT_TIME,
) -> TaskRequest | None:
    """把一句话解析成任务请求；认不出来返回 ``None``。

    Args:
        text: 用户原话（已剥离唤醒词与指令前缀）。
        known_names: 本会话已知的人名（绑定 / 监听名单），用于判断
            「分析一下钢板最近的表现」里的目标是钢板。
        default_time: 只说频率没给钟点时的默认时刻。
    """
    raw = str(text or "").strip()
    if not raw:
        return None

    # ---- 1. 计数型：「每监听到十盘战绩就生成一份总结」 ----
    count_match = _EVERY_COUNT_RE.search(raw)
    clock = parse_clock(raw)
    interval_like = _INTERVAL_RE.search(raw) or _HOURLY_RE.search(raw)
    time_spec = parse_time_spec(raw, default_time=default_time)
    # 只要句子里有**钟点**或「每小时」这类真正的周期，就当时间型；
    # 否则「每十盘」才是计数型（「每」+「盘」本来就不是时间单位）。
    if (
        count_match
        and (interval_like or (clock is not None))
        and time_spec is not None
    ):
        time_spec = None
    if count_match and time_spec is None:
        every = cn_to_int(count_match.group(1))
        if not every or every < 1:
            every = DEFAULT_EVERY
        every = min(MAX_EVERY, every)
        # 「每盘点一下」这种没数字、又没监听词的，更像闲聊，不认
        if _WATCH_WORD_RE.search(raw) or _SUMMARY_WORD_RE.search(raw) or every > 1:
            action, args = _detect_action(raw, known_names)
            if action == ACTION_META:
                # 版本榜是「当下时点」的数据，按场次触发没有意义
                action, args = ACTION_GROUP, ""
            return TaskRequest(
                kind="watch_count",
                action=(
                    ACTION_WATCH_SUMMARY if action == ACTION_GROUP else action
                ),
                args=args,
                every=every,
                label=f"每 {every} 场总结",
                raw=raw,
            )

    # ---- 2. 时间型 ----
    if time_spec is None:
        return None
    has_hint = any(word in raw for word in _DOTA_HINT_WORDS)
    if any(word in raw for word in _REMIND_ONLY_WORDS) and not has_hint:
        # 「每天七点提醒我喝水」：插件只认 Dota2 数据类任务，别硬接
        return None
    if not has_hint:
        # 一点 Dota2 的影子都没有（「每周一早上九点开会」）：宁可说不会，
        # 也不要给群里建一个每天发战绩的任务。
        return None
    action, args = _detect_action(raw, known_names)
    return TaskRequest(
        kind="cron",
        action=action,
        args=args,
        time=time_spec,
        label=f"{time_spec.label} · {ACTION_LABELS.get(action, action)}",
        raw=raw,
    )


def _detect_action(
    raw: str, known_names: Iterable[str]
) -> tuple[str, str]:
    """判断这句话要干什么，返回 ``(动作, 参数)``。"""
    for word in _META_WORDS:
        if word in raw:
            return ACTION_META, ""
    target = _match_known_name(raw, known_names)
    if target and any(word in raw for word in _PLAYER_WORDS):
        return ACTION_PLAYER, target
    if any(word in raw for word in _GROUP_WORDS):
        return ACTION_GROUP, ""
    if target:
        # 点了名但没说干什么：「每天通报钢板」→ 就是他的近期表现
        return ACTION_PLAYER, target
    # 什么都没说清，但时间表述完整（「每天早上七点」）⇒ 按最常用的群通报来，
    # 因为这是唯一「不说主语也说得通」的动作
    return ACTION_GROUP, ""


def _match_known_name(raw: str, known_names: Iterable[str]) -> str:
    """在句子里找本会话已知的人名（长名字优先，避免被短名字截胡）。"""
    names = [str(n).strip() for n in known_names if str(n).strip()]
    for name in sorted(set(names), key=len, reverse=True):
        if len(name) >= 2 and name in raw:
            return name
    return ""


# ======================================================================
# 任务标识
# ======================================================================
_ID_ALPHABET = string.ascii_lowercase + string.digits


def new_schedule_id(prefix: str = "d2") -> str:
    """生成一个短的任务标识（给用户报号用，也让 WebUI 里的名字好认）。"""
    body = "".join(random.choices(_ID_ALPHABET, k=6))
    return f"{prefix}{body}"


def short_id(schedule_id: str) -> str:
    """取前 6 位，用于列表与「删 xxx」。"""
    text = str(schedule_id or "").strip()
    return text[:6]


def task_name(request: TaskRequest, *, session_label: str = "") -> str:
    """任务名（显示在 WebUI 的「未来任务」列表里）。"""
    label = ACTION_LABELS.get(request.action, request.action)
    if request.kind == "watch_count":
        return f"Dota2 · 每 {request.every} 场 · 战绩总结"
    stamp = request.time.label if request.time else ""
    return f"Dota2 · {stamp} · {label}"


def task_note(request: TaskRequest, *, session_label: str = "") -> str:
    """任务说明（WebUI 里鼠标悬停/详情能看到，也是 handler 的载荷之一）。"""
    where = f"（{session_label}）" if session_label else ""
    if request.kind == "watch_count":
        return (
            f"Dota2 定时任务{where}：每监听到 {request.every} 场比赛，"
            f"生成本群这 {request.every} 场的战绩总结。"
        )
    return f"Dota2 定时任务{where}：{request.describe()}。"


def job_name(action: str, cron: str) -> str:
    """由「动作 + cron」还原任务名（改时间后名字里的时刻要跟着变）。

    任务名里带着时刻（``Dota2 · 每天 10:10 · 通报群里战绩情况``），
    改了执行时间却不改名字的话，用户看列表/WebUI 时看到的还是旧时刻，
    会以为没改成功。
    """
    label = ACTION_LABELS.get(action, action)
    return f"Dota2 · {cron_label(cron)} · {label}"


def job_note(action: str, cron: str, *, session_label: str = "") -> str:
    """由「动作 + cron」还原任务说明（改时间后要一起同步）。"""
    where = f"（{session_label}）" if session_label else ""
    label = ACTION_LABELS.get(action, action)
    return f"Dota2 定时任务{where}：{cron_label(cron)} —— {label}。"


# ======================================================================
# 列表渲染
# ======================================================================
def format_task_list(
    cron_rows: list[dict[str, Any]],
    count_rows: list[dict[str, Any]],
    *,
    keyword: str = "",
) -> str:
    """渲染 ``/d2 定时`` 的列表。

    Args:
        cron_rows: :meth:`dota_cron.CronBridge.job_summary` 的输出，
            每项含 ``name`` / ``cron`` / ``enabled`` / ``next_run_text``。
        count_rows: 插件存储里的计数型任务。
    """
    lines = ["⏰ 本会话的定时任务", ""]
    if not cron_rows and not count_rows:
        lines.append("（还没有任务）")
        lines.append("")
        lines.append("可以这样说：")
        lines.append(f"　{keyword} 每天早上七点，通报群里战绩情况")
        lines.append(f"　{keyword} 每晚十点总结一下大家今天的表现")
        lines.append(f"　{keyword} 每监听到十盘战绩就生成一份这十盘的总结")
        lines.append(f"　{keyword} 明天早上八点通报一下战绩")
        return "\n".join(lines)

    if cron_rows:
        lines.append("【按时间】到点自动发到本会话")
        for index, row in enumerate(cron_rows, start=1):
            mark = "✅" if row.get("enabled", True) else "⏸"
            sid = short_id(str(row.get("schedule_id") or row.get("job_id") or ""))
            when = cron_label(str(row.get("cron") or ""))
            lines.append(
                f" {index}. {mark} {sid}　{when}　{row.get('action_label') or ''}"
                f"　（下次 {row.get('next_run_text') or '未知'}）"
            )
        lines.append("")

    if count_rows:
        lines.append("【按场次】攒够就发")
        for index, row in enumerate(count_rows, start=1):
            sid = short_id(str(row.get("id") or ""))
            every = int(row.get("every") or DEFAULT_EVERY)
            done = len(row.get("seen") or [])
            mark = "✅" if row.get("enabled", True) else "⏸"
            lines.append(
                f" {index}. {mark} {sid}　每 {every} 场　"
                f"（已攒 {done}/{every}）"
            )
        lines.append("")

    lines.append(f"删除：`{keyword} 定时 删 <编号>`")
    lines.append(f"停用 / 启用：`{keyword} 定时 停 <编号>` / `{keyword} 定时 开 <编号>`")
    lines.append(f"改时间：`{keyword} 定时 改 <编号> 早上八点`")
    lines.append(f"立刻执行一次：`{keyword} 定时 现在 <编号>`")
    lines.append(f"全部删掉：`{keyword} 定时 删 全部`")
    lines.append("")
    lines.append("也可以直接说人话：「取消所有定时任务」「把 2 号删掉」。")
    return "\n".join(lines)


# ======================================================================
# 到点执行用的提示词
# ======================================================================
#: 「群里战绩通报」到点执行时送给模型的问题（数据由插件侧注入）
GROUP_QUESTION = (
    "请通报一下本群最近的战绩情况：这段时间谁状态最好、谁在掉分，"
    "整体胜率如何，有没有值得说一嘴的对局。这是定时播报，直接给结论，"
    "不要复述进度条或询问我想看什么。"
)


def build_report_question(action: str, args: str = "") -> str:
    """把动作翻译成送给模型的「问题」。"""
    if action == ACTION_PLAYER and args:
        return (
            f"请分析并通报「{args}」最近的表现：状态在上升还是下滑、"
            f"英雄池有什么变化、有什么值得提醒的问题。这是定时播报，直接给结论。"
        )
    if action == ACTION_META:
        return "请给出当前版本的强势英雄榜并点出重点。"
    return GROUP_QUESTION


WATCH_SUMMARY_SYSTEM_PROMPT = (
    "你是 Dota2 群里那位懂球的老玩家。现在群里刚打满一段比赛，"
    "你要用几句话给大家做个阶段性总结：谁这段时间最猛、谁在连败、"
    "有没有哪一局值得复盘。只依据给出的比赛记录，不要编造没出现的数字，"
    "也不要罗列逐场流水账。控制在 150 字以内，口语一点。"
)


def build_watch_summary_prompt(rows: list[dict[str, Any]], *, every: int) -> str:
    """渲染「这 N 盘的总结」提示词。

    Args:
        rows: 本次窗口内的比赛记录，每项含 ``desc``（一句话战果）、
            ``match_id``、``time_text``（本地时间）。
        every: 触发这次总结的场次阈值。
    """
    lines = [
        f"本群最近连续监听到 {len(rows)} 场比赛（每 {every} 场做一次总结），"
        "逐场记录如下：",
        "",
    ]
    for index, row in enumerate(rows, start=1):
        when = str(row.get("time_text") or "时间未知")
        desc = str(row.get("desc") or "").strip() or "（无描述）"
        lines.append(f"{index}. [{when}] {desc}（比赛 {row.get('match_id')}）")
    lines.append("")
    lines.append(
        f"请针对这 {len(rows)} 场写一段阶段性总结：整体胜负情况、谁最亮眼、"
        "谁需要调整。不要逐场复述。"
    )
    return "\n".join(lines)


def window_rows(
    seen: list[dict[str, Any]], every: int
) -> list[dict[str, Any]]:
    """从累计窗口里取最近 ``every`` 场（按记录顺序，最旧的在前）。"""
    items = [row for row in (seen or []) if isinstance(row, dict)]
    if every <= 0:
        return items
    return items[-every:]


# ======================================================================
# 会话标签：让任务说明里写清「发到哪」
# ======================================================================
#: 会话类型段 → 人话。AstrBot 的 umo 形如
#: ``aiocqhttp:GroupMessage:736185523``，中间那段就是会话类型。
_SESSION_KIND = {
    "GroupMessage": "群",
    "FriendMessage": "私聊",
    "GuildMessage": "频道",
    "DingTalkMessage": "会话",
    "LarkMessage": "会话",
    "WeChatOfficialAccountMessage": "会话",
}


def session_label(umo: str) -> str:
    """把 ``aiocqhttp:GroupMessage:736185523`` 渲染成 ``群736185523``。

    任务说明里必须有「发到哪」：同一个人可能在好几个群都建过任务，
    只写「当前会话」根本分不清。真名（群名称）拿不到也不去查 ——
    会话 ID 是确定的，而查群名要额外调平台接口、还可能失败。
    """
    text = str(umo or "").strip()
    if not text:
        return "当前会话"
    parts = [part for part in text.split(":") if part]
    if len(parts) >= 3:
        kind = _SESSION_KIND.get(parts[-2], parts[-2])
        return f"{kind}{parts[-1]}"
    return text[-12:]


# ======================================================================
# ``/d2 定时`` 的子动作
# ======================================================================
#: 子动作词 → 规范化动作名。
CONTROL_WORDS: dict[str, str] = {
    "列表": "list",
    "list": "list",
    "ls": "list",
    "查看": "list",
    "看": "list",
    "看看": "list",
    "删": "delete",
    "删除": "delete",
    "取消": "delete",
    "撤销": "delete",
    "del": "delete",
    "移除": "delete",
    "停": "disable",
    "停用": "disable",
    "暂停": "disable",
    "关": "disable",
    "开": "enable",
    "启用": "enable",
    "恢复": "enable",
    "现在": "run",
    "立即": "run",
    "执行": "run",
    "跑一次": "run",
    "run": "run",
}


#: 「整批操作」的占位编号：把动作作用到本会话的**全部**任务上。
#: 它不可能和真实编号撞车（编号是数字或短 ID），所以直接当哨兵值用。
ALL_TOKEN = "*"

#: 指令别名。自然语言入口把用户原话**整句**交给指令实现（见
#: ``main.d2_natural`` 里的 ``intent.args = effective``），句首往往还留着
#: 用户自己打的指令名。必须先剥掉再解析，否则会出**语义反转**的事故。
SCHEDULE_ALIASES: tuple[str, ...] = (
    "定时任务", "定时", "任务", "计划", "schedule", "cron", "d2",
)

#: 提到「定时任务」这件事本身的名词。整批操作（「都删了」）必须句子里有它，
#: 否则「删除所有监听」会被误认成「删掉所有定时任务」。
_SCHEDULE_NOUNS = ("定时", "任务", "计划")

#: 范围词：出现它才可能是在说「全部」。
_BULK_SCOPE_RE = re.compile(r"所有|全部|一切|全都|统统|都|全")

#: 「动词在前、名词在后」的句子（「取消定时任务」「看一下所有计划」）里，
#: 这些词只是把话说圆；剥掉之后剩下的才是真正的动作词。
_FILLER_WORDS: tuple[str, ...] = (
    "定时任务", "定时", "任务", "计划", "schedule",
    "所有", "全部", "一切", "全都", "统统",
    "一下", "我的", "本群的", "帮我", "帮忙", "给我", "请", "麻烦", "把",
    "这个", "那个", "这些", "那些", "该",
    "的", "了", "吧", "啦", "啊", "哦", "嘛", "呀",
)

#: :func:`normalize_target` 专用的填充词：**故意不含**「这个 / 那个」——
#: 「删 这个」指的是某一个（只是没说明白），当成「全删」会误伤；
#: 而「删 定时任务」里的名词确实只是把话说圆，可以直接归到「全部」。
_TARGET_FILLER: tuple[str, ...] = (
    "定时任务", "定时", "任务", "计划", "schedule",
    "所有", "全部", "一切", "全都", "统统",
    "一下", "我的", "本群的", "帮我", "帮忙", "给我", "请", "麻烦", "把",
    "的", "了", "吧", "啦", "啊", "哦", "嘛", "呀",
)

#: 批量动作词：动作名 → 词表。顺序有意义（先删、再停、最后开）。
_BULK_VERBS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("delete", ("清空", "清掉", "清理掉", "删除", "删掉", "移除", "取消", "撤销", "删")),
    ("disable", ("停用", "暂停", "停掉", "关闭", "关掉", "停")),
    ("enable", ("启用", "开启", "打开", "恢复", "开")),
)

#: 「光说动作、不带编号」也算整批的说法（「取消定时任务」「清空」）。
_BARE_BULK_VERBS = frozenset(
    {"删", "删除", "清空", "清掉", "移除", "取消", "撤销", "停", "停用", "暂停", "启用", "恢复", "开"}
)

#: 改时间的动词与必须伴随的时间词。
_UPDATE_VERB_RE = re.compile(r"改到|改成|改为|改一下|改下|调整|调到|调成|换到|换成|修改|改")
_UPDATE_TIME_RE = re.compile(r"时间|时刻|钟点|几点|[点时:：]")

#: 「把 2 号删掉」——编号在前、动作在后。
_TRAILING_INDEX_RE = re.compile(
    r"\s*(?:把|请|帮我|帮忙|麻烦)?\s*(?:第)?\s*(?P<idx>[0-9]{1,2})\s*号?(?:个)?\s*(?:的)?\s*"
    r"(?:定时任务|定时|任务|计划)?\s*"
    r"(?P<verb>删掉|删除|删|移除|取消|撤销|停用|暂停|停掉|启用|开启|恢复|执行|跑一次)"
    r"\s*(?:了|掉|吧|啦|啊|呗)?\s*"
)

#: 改时间句子里要抹掉的噪声词（**不能**把时间表达本身抹掉）。
_UPDATE_NOISE: tuple[str, ...] = (
    "执行时间", "运行时间", "触发时间", "提醒时间",
    "定时任务", "定时", "任务", "计划",
    "把那个", "把这个", "帮我", "帮忙", "给我", "麻烦",
    "这个", "那个", "该", "它的", "其", "以后", "从明天起", "改成", "改到", "改为",
    "调整到", "调整为", "调整", "调到", "调成", "换到", "换成", "修改", "改一下", "改下",
    "时间", "时刻", "一下", "下", "把", "的", "为", "到", "成",
)


def _strip_filler(text: str, words: tuple[str, ...] = _FILLER_WORDS) -> str:
    """把填充词剥掉，剩下的通常是真正的动作词。"""
    out = str(text or "")
    for word in sorted(words, key=len, reverse=True):
        out = out.replace(word, "")
    return re.sub(r"[\s，。、,.;；!！?？:：]+", "", out)


def _strip_alias(text: str) -> str:
    """剥掉句首的指令名与标点（「定时 删 1」→「删 1」）。

    自然语言入口不解析参数，而是把整句原话交给指令实现。用户说
    「dota2助手 定时 删 1」时，落到 ``d2_schedule`` 的 args 就是
    「定时 删 1」—— 句首还留着指令名。**不剥掉就会出语义反转的事故**：
    控制词解析扑空 → 整句被当成「新建任务」的人话 → 用户说「删」，
    插件却在准备「建」。
    """
    body = str(text or "").strip().lstrip("，。、,.;；!！?？ \t")
    changed = True
    while changed and body:
        changed = False
        for word in sorted(SCHEDULE_ALIASES, key=len, reverse=True):
            if not body.lower().startswith(word):
                continue
            rest = body[len(word):]
            # 只有「别名后面紧跟分隔符或就到头」才敢剥。否则
            # 「任务完成时提醒我」会被剥成「完成时提醒我」——另一句话了。
            if rest[:1] in ("", " ", "，", "。", "、", ",", ":", "：", "\t"):
                body = rest.strip().lstrip("，。、,.;；!！?？ \t")
                changed = True
                break
    return body


def _parse_bulk(raw: str) -> str | None:
    """「全部 / 所有」这类整批操作；不是整批返回 ``None``。"""
    text = str(raw or "").strip()
    if not text:
        return None
    has_noun = any(noun in text for noun in _SCHEDULE_NOUNS)
    forced = bool(re.search(r"清空|清掉|清理掉", text))
    has_scope = bool(_BULK_SCOPE_RE.search(text))
    verb_text = _strip_filler(text)
    if not (has_scope or forced or verb_text in _BARE_BULK_VERBS):
        return None
    for verb, words in _BULK_VERBS:
        for word in words:
            if word in text:
                # 范围词 + 动作词还不够：「删除所有监听」两者都有，但说的是
                # 监听不是任务。必须同时提到「定时 / 任务 / 计划」，或者
                # 明确说了「清空」、整句只剩一个光杆动作词。
                if has_noun or forced or verb_text in _BARE_BULK_VERBS:
                    return verb
                return None
    return None


def _looks_like_update(raw: str) -> bool:
    """这句话是不是在给**已有任务**改时间。"""
    text = str(raw or "").strip()
    if not text or not _UPDATE_VERB_RE.search(text):
        return False
    if _UPDATE_TIME_RE.search(text):
        return True
    # 「改 1 到八点」「把 2 号改成晚上十点」没写「时间」两个字，但给了编号 ——
    # 这是配合列表编号的另一种常见说法。要求动词后面**紧跟着编号**，
    # 免得「改一下通报内容」也被认成改时间。
    return bool(re.search(r"(改|调|换|调整|修改)\s*(?:第)?\s*[0-9]{1,2}\s*号?\s*(到|成|为)?", text))


def split_update_target(text: str) -> tuple[str, str]:
    """拆出「改时间」请求里的 ``(编号, 剩下的时间表达)``；没给编号返回空串。"""
    body = str(text or "").strip()
    index = ""
    head_match = re.match(r"^(?:把)?\s*(?:第)?\s*([0-9]{1,2})\s*号?\s*", body)
    if head_match:
        index = head_match.group(1)
        body = body[head_match.end():]
    else:
        mid = re.search(
            r"^(?:把)?\s*(改|调整|修改|换)\s*(?:第)?\s*([0-9]{1,2})\s*号?\s*(?:到|成|为)?\s*",
            body,
        )
        if mid:
            index = mid.group(2)
            body = body[mid.end():]
    clean = body
    for word in sorted(_UPDATE_NOISE, key=len, reverse=True):
        clean = clean.replace(word, "")
    return index, clean.strip(" ，。、,.;；!！?？")


def retime_cron(cron: str, hour: int, minute: int) -> str:
    """只换钟点，保留原有的周期（每天 / 每周几 / 每月几号 / 一次性日期）。

    「把这个任务的执行时间改到早上七点」只说钟点、没说频率，那频率就该
    沿用原来的：原来每周五 23:00，改完是每周五 07:00，不能变成一个每天
    都响的任务。
    """
    parts = str(cron or "").split()
    if len(parts) != 5:
        return f"{minute} {hour} * * *"
    return f"{minute} {hour} {parts[2]} {parts[3]} {parts[4]}"


def normalize_target(rest: str) -> str:
    """把「操作对象」规范化：``全部 / 所有 / 定时任务`` → :data:`ALL_TOKEN`。

    编号原样返回；实在看不懂的原样返回（由调用方回一句「要操作哪一个」）。
    空串返回空串 —— 「一个都没说」和「说了全部」是两件事，不能混。
    """
    text = str(rest or "").strip()
    if not text:
        return ""
    if text in ("全部", "所有", "一切", "全都", "统统", "全", ALL_TOKEN):
        return ALL_TOKEN
    if not _strip_filler(text, _TARGET_FILLER):
        # 「删 定时任务」：名词只是把话说圆，意思就是「全都删」
        return ALL_TOKEN
    return text


def parse_control(text: str) -> tuple[str, str]:
    """解析 ``/d2 定时`` 后面那串文本，返回 ``(动作, 余下文本)``。

    动作是 ``list`` / ``delete`` / ``disable`` / ``enable`` / ``run`` /
    ``update`` / ``create``；空输入按 ``list`` 处理。没命中任何控制词时一律当
    ``create``（后面接的就是用户的整句人话）。

    余下文本为 :data:`ALL_TOKEN` 时，表示对**全部**任务生效。

    「删3」这种不带空格的写法也要认 —— 中文用户很少记得打空格。
    """
    raw = _strip_alias(text)
    if not raw:
        return "list", ""

    head, sep, rest = raw.partition(" ")

    # ---- 0. 「把 2 号删掉」：编号在前、动作在后 ----
    # 中文里这是最口语的一种写法，但它和「删 1」的形状完全不同（动作在后），
    # 下面两条分支都接不住，不单独认就会掉进 create。
    trailing = _TRAILING_INDEX_RE.fullmatch(raw)
    if trailing:
        word = trailing.group("verb")
        for key in sorted(CONTROL_WORDS, key=len, reverse=True):
            if word.startswith(key):
                return CONTROL_WORDS[key], trailing.group("idx")

    # ---- 1. 先按「动作词打头」解析：这里的判据最硬（编号要紧跟动作词），
    #         必须先跑，否则「删 1」会被下面的整批判定抢走。
    if not sep:
        for word in sorted(CONTROL_WORDS, key=len, reverse=True):
            if not raw.startswith(word) or len(raw) <= len(word):
                continue
            tail = raw[len(word):].strip()
            # 没有空格时，**必须「剩下的就是个编号」**才算控制词。
            # 否则「开黑的时候每天早上七点通报战绩」会被开头的「开」
            # 截胡成「启用任务」—— 一句正常的定时请求就这么丢了。
            if re.fullmatch(r"[0-9a-zA-Z]{1,12}", tail):
                return CONTROL_WORDS[word], tail
    verb = CONTROL_WORDS.get(head.strip())
    if verb is not None:
        return verb, normalize_target(rest)

    # ---- 2. 整批：「取消所有定时任务」「把任务都删了」----
    bulk = _parse_bulk(raw)
    if bulk:
        return bulk, ALL_TOKEN

    # ---- 3. 动词在前、名词在后（「看一下定时任务」「查看所有计划」）----
    # 中文习惯把动作放前面，「看一下定时任务」里「一下」和名词都只是填充。
    # 剥掉之后整句只剩一个动作词，那它就是这条指令的意思。
    lone = CONTROL_WORDS.get(_strip_filler(raw))
    if lone is not None:
        return lone, ALL_TOKEN

    # ---- 3b. 问「有哪些 / 几个」的，就是想知道清单 ----
    # 「看一下我建了哪些定时任务」剥完填充词还剩一串东西，落不到上面那条，
    # 但它显然是在问列表，不能回一句「没听懂这个定时任务要做什么」。
    if any(noun in raw for noun in _SCHEDULE_NOUNS) and re.search(
        r"哪些|哪个|有什么|有几个|多少个|几个|都建了", raw
    ):
        return "list", ALL_TOKEN

    # ---- 4. 改时间：「把这个定时任务的执行时间改到早上七点」----
    if _looks_like_update(raw):
        return "update", raw

    return "create", raw


def looks_like_control(text: str) -> bool:
    """这句话是不是在**管理**已有任务（查看 / 删除 / 停用 / 改时间）。

    给自然语言入口用。这类句子**没有任何时间表达**，走不到
    :func:`looks_like_task` 那一步 —— 现场事故正是「取消所有定时任务」
    被判成闲聊，插件回了一段不相关的唠嗑，任务一个都没删。

    必须提到「定时 / 任务 / 计划」才认：光说「取消」「列表」太泛，
    别的功能（取消监听、看绑定列表）也在用这些词。
    """
    raw = str(text or "").strip()
    if not raw:
        return False
    if not any(noun in raw for noun in _SCHEDULE_NOUNS):
        return False
    verb, _rest = parse_control(raw)
    return verb != "create"


# ======================================================================
# 确认文案
# ======================================================================
def format_plan(
    request: TaskRequest,
    *,
    session_text: str = "",
    keyword: str = "dota2助手",
) -> str:
    """渲染「我准备建这样一个任务，对吗」。

    建任务会**持续生效**（每天到点往群里发东西），误判的代价远大于
    绑定 / 监听，所以把「什么时候、做什么、发到哪」三条全摆出来给用户核对，
    再让他回一句「确认」。计数型任务额外说明它不在「未来任务」页面里，
    免得用户去那儿找不到。
    """
    where = session_text or "当前会话"
    lines = ["📅 我理解的任务是这样的：", ""]
    if request.kind == "watch_count":
        lines.append(f"　触发：每监听到 {request.every} 场比赛")
        lines.append(f"　内容：{ACTION_LABELS[ACTION_WATCH_SUMMARY]}")
        lines.append(f"　发到：{where}")
        lines.append("")
        lines.append(
            "（这种任务按场次触发，不会出现在 AstrBot 的「未来任务」里；"
            "攒够场次就发，不用等固定时间）"
        )
    else:
        spec = request.time
        lines.append(f"　时间：{spec.label if spec is not None else '（没解析出时间）'}")
        if spec is not None and spec.used_default_time:
            lines.append("　　　　（你没说具体几点，用的是默认时刻）")
        what = ACTION_LABELS.get(request.action, request.action)
        if request.args:
            what = f"{what}（对象：{request.args}）"
        lines.append(f"　内容：{what}")
        lines.append(f"　发到：{where}")
    lines.append("")
    lines.append("回复「确认」我就建好；回复「取消」就当我没说。")
    lines.append(f"建好后可用 `{keyword} 定时 列表` 查看、停用或删除。")
    return "\n".join(lines)


def looks_like_task(
    text: str, *, default_time: str = DEFAULT_TIME
) -> bool:
    """这句话是不是一个定时任务请求（**不看会话名单**的形状判定）。

    给自然语言入口用：这类句子里必然夹着「战绩 / 监听 / 总结」这些功能词，
    走关键词打分会被拆成一次性的「查战绩」「加监听」——用户要的是**以后每天**
    都做。所以先按形状判一次（时间表达 + 跟 Dota2 数据有关），判中就走
    ``schedule`` 意图。

    这里刻意不传 ``known_names``：入口拿到的人名要联网/查存储，而这一步
    只是「像不像」，认人交给 :func:`parse_request` 在真正建任务时做。
    """
    return parse_request(text, default_time=default_time) is not None


__all__ = [
    "ACTION_GROUP",
    "ACTION_LABELS",
    "ACTION_META",
    "ACTION_PLAYER",
    "ACTION_WATCH_SUMMARY",
    "ALL_TOKEN",
    "CONTROL_WORDS",
    "Clock",
    "DEFAULT_EVERY",
    "DEFAULT_TIME",
    "GROUP_QUESTION",
    "MAX_EVERY",
    "TaskRequest",
    "TimeSpec",
    "WATCH_SUMMARY_SYSTEM_PROMPT",
    "build_report_question",
    "build_watch_summary_prompt",
    "cn_to_int",
    "cron_label",
    "format_plan",
    "format_task_list",
    "job_name",
    "job_note",
    "looks_like_control",
    "looks_like_task",
    "new_schedule_id",
    "normalize_target",
    "once_cron",
    "parse_clock",
    "parse_control",
    "parse_request",
    "parse_time_spec",
    "retime_cron",
    "session_label",
    "short_id",
    "split_update_target",
    "task_name",
    "task_note",
    "window_rows",
]
