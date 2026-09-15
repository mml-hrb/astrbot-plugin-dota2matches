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

模块本身不依赖 AstrBot 的运行时（只用一个 logger），便于离线测试。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from astrbot.api import logger

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
    r"kda|gpm|经济|输出|参团|数据|统计|评分|打得"
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


def row_win(match: dict) -> bool | None:
    """判断「玩家视角的一行比赛」是胜是负。

    返回 ``None`` 表示**判不出来** —— 这时候不能按负处理，否则胜率会
    被系统性低估（这和「缺失值不能伪装成 0」是同一类问题）。

    判定顺序：

    1. ``player_win`` / ``isVictory`` / ``win``（数据源直接给的权威布尔）；
    2. ``radiant_win`` + ``is_radiant``（两个都是布尔才敢用）；
    3. ``radiant_win`` + ``player_slot``（``<128`` 为天辉）。
    """
    for key in ("player_win", "isVictory", "is_victory", "win"):
        flag = match.get(key)
        if isinstance(flag, bool):
            return flag

    radiant_win = match.get("radiant_win")
    if not isinstance(radiant_win, bool):
        return None

    is_radiant = match.get("is_radiant")
    if isinstance(is_radiant, bool):
        return radiant_win == is_radiant

    slot = _as_int(match.get("player_slot"))
    if slot is None:
        return None
    return radiant_win == (slot < 128)


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

    # ---------------- 统计 ----------------
    @property
    def games(self) -> int:
        return len(self.matches)

    @property
    def decided(self) -> int:
        """能判定胜负的场次（分母，不含未知）。"""
        return sum(1 for m in self.matches if row_win(m) is not None)

    @property
    def wins(self) -> int:
        return sum(1 for m in self.matches if row_win(m) is True)

    @property
    def win_rate(self) -> float | None:
        total = self.decided
        if not total:
            return None
        return self.wins / total

    @property
    def avg_kda(self) -> float | None:
        values = []
        for m in self.matches:
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
            for gpm in (_as_float(m.get("gold_per_min")) for m in self.matches)
            if gpm is not None
        ]
        if not values:
            return None
        return sum(values) / len(values)

    @property
    def avg_deaths(self) -> float | None:
        values = [
            d
            for d in (_as_float(m.get("deaths")) for m in self.matches)
            if d is not None
        ]
        if not values:
            return None
        return sum(values) / len(values)

    @property
    def turbo_games(self) -> int:
        return sum(1 for m in self.matches if _as_int(m.get("game_mode")) == GAME_MODE_TURBO)

    @property
    def position_counts(self) -> list[tuple[str, int]]:
        """近期各位置的场次，按场次从多到少（取不到位置的场次不计入）。

        注意它可能与 :attr:`games` 不相等 —— 差值就是「位置未知」的场次。
        提示词里会把这个差值说明白，免得模型把「10 场里只认出 6 场」
        当成「另外 4 场是别的位置」来推理。
        """
        counter: dict[str, int] = {}
        for match in self.matches:
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
        for match in self.matches:
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
    def summary_line(self) -> str:
        label = f"{self.name}（账号 {self.account_id}"
        if self.relation:
            label += f"，{self.relation}"
        label += "）"

        if self.error:
            return f"· {label}：数据获取失败（{self.error}）"
        if not self.games:
            return f"· {label}：最近没有可用的比赛记录"

        parts = [f"近 {self.games} 场：{self.wins} 胜"]
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
            parts.append(f"近期位置 {top}")
            known = sum(n for _, n in positions)
            if known < self.games:
                parts.append(f"另有 {self.games - known} 场位置未知")
        score = self.score()
        if score is not None:
            parts.append(f"参考分 {score:.0f}/100")
        turbo = self.turbo_games
        if turbo:
            parts.append(f"其中 {turbo} 场 Turbo")

        return f"· {label}：" + " | ".join(parts)

    def detail_lines(self, heroes: dict[int, dict]) -> list[str]:
        """最近几场的逐场明细（最多 :data:`DETAIL_ROWS` 条）。"""
        if not self.matches:
            return []
        lines = []
        for match in self.matches[:DETAIL_ROWS]:
            win = row_win(match)
            if win is True:
                flag = "胜"
            elif win is False:
                flag = "负"
            else:
                flag = "胜负未知"
            kills = _as_int(match.get("kills"))
            deaths = _as_int(match.get("deaths"))
            assists = _as_int(match.get("assists"))
            kda_text = (
                f"{kills}/{deaths}/{assists}"
                if None not in (kills, deaths, assists)
                else "-/-/-"
            )
            bits = [flag, hero_label(heroes, match.get("hero_id")), kda_text]
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
    needs: set[str] = field(default_factory=set)
    #: 给模型看的「注意事项」，例如某块数据没取到
    notes: list[str] = field(default_factory=list)


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
        return f"· {label}：" + " > ".join(f"{name} {fmt(value)}" for name, value in pairs)

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


def format_context_block(ctx: ChatContext) -> str:
    """把上下文渲染成给模型看的纯文本（无 markdown 表格）。"""
    lines: list[str] = ["=== 本会话的插件数据 ==="]

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

    # ---- 战绩快照 ----
    if ctx.snapshots:
        lines.append("")
        lines.append(
            f"【近期战绩快照】每人最近若干场（数据源：主数据源，"
            f"取不到时自动回退后备源）"
        )
        has_detail = False
        for snap in ctx.snapshots:
            lines.append(snap.summary_line())
            detail = snap.detail_lines(ctx.heroes)
            if detail:
                has_detail = True
                lines.extend(detail)
        if has_detail:
            lines.append("（上面缩进的几行是各自最近几场的逐场明细）")
        comparison = format_comparison_block(ctx.snapshots)
        if comparison:
            lines.append("")
            lines.append(comparison)

    # ---- 英雄池 ----
    if ctx.hero_history:
        lines.append("")
        lines.append("【提问者的英雄池】按使用场次排序，取前几：")
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

    return "\n".join(lines)


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
9. 篇幅控制在 400 字以内；用户明确要求详细分析时才展开。
10. 结尾不要反问「还需要我做什么吗」这类客套。"""


def build_chat_prompt(question: str, ctx: ChatContext) -> str:
    """拼出最终发给模型的用户提示词。"""
    blocks = [
        "=== 用户的问题 ===",
        (question or "").strip(),
        "",
        format_context_block(ctx),
        "",
        CHAT_REQUIREMENTS,
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


async def _fetch_hero_history(api: Any, account_id: int, timeout: float) -> list[dict]:
    try:
        rows = await asyncio.wait_for(
            api.get_player_heroes(account_id), timeout=timeout
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[dota2] 闲聊兜底：拉取 {account_id} 英雄池失败: {e}")
        return []
    return [row for row in (rows or []) if isinstance(row, dict)]


async def collect_chat_context(
    api: Any,
    *,
    question: str,
    umo: str = "",
    user_id: str = "",
    watchers: Iterable[dict] = (),
    self_binding: dict | None = None,
    bindings: Iterable[dict] = (),
    recent_limit: int = DEFAULT_RECENT_LIMIT,
    max_players: int = DEFAULT_MAX_PLAYERS,
    timeout: float = DEFAULT_FETCH_TIMEOUT,
    cache: dict | None = None,
    cache_ttl: float = DEFAULT_CACHE_TTL,
) -> ChatContext:
    """收集一次闲聊回答所需的插件数据。

    Args:
        api: 插件的数据源门面（含 ``get_player_matches_enriched`` 等方法）。
        question: 剥掉唤醒词之后的用户原话。
        watchers: **本会话**的监听项列表（调用方负责按 umo 过滤）。
        self_binding: 提问者在**本会话**的绑定，没有则 None。
        bindings: 本会话的绑定列表（展示用）。
        cache: 可选的战绩缓存 ``{(account_id, limit): (时间戳, 快照)}``，
            由调用方持有，跨消息复用；不传则不缓存。

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
    )

    self_account = (
        _as_int(self_binding.get("account_id")) if self_binding else None
    )
    ctx.needs = detect_needs(
        question,
        names=[str(w.get("personaname") or "") for w in watcher_list],
        has_self=self_account is not None,
    )

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

    if skipped:
        ctx.notes.append(
            f"本会话还有 {skipped} 位被监听玩家没有取数据"
            f"（一次最多分析 {max(1, max_players)} 人）。"
        )

    # ---- 英雄常量表（只在需要英雄名时拉，失败就用「英雄#id」） ----
    if candidates:
        try:
            heroes = await asyncio.wait_for(api.get_heroes(), timeout=timeout)
            if isinstance(heroes, dict):
                ctx.heroes = heroes
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dota2] 闲聊兜底：拉取英雄常量表失败: {e}")
            ctx.notes.append("英雄名称表没取到，英雄以 ID 展示。")

    # ---- 战绩快照 ----
    if NEED_RECENT in ctx.needs and candidates:
        ctx.snapshots = await _collect_snapshots(
            api, candidates, recent_limit, timeout, cache, cache_ttl
        )
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

    # ---- 英雄池（只对提问者本人） ----
    if NEED_HEROES in ctx.needs and self_account:
        ctx.hero_history = await _fetch_hero_history(api, self_account, timeout)
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
