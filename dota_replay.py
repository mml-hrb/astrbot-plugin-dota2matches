"""关键团候选、运行环境检测与**单线**录像解析队列。

这一模块解决三个问题
--------------------
1. **哪几波团值得深挖**：只用 OpenDota 的 ``teamfights`` 就能算出来，
   不需要碰录像。整局 20 分钟里有十几二十波交火，全丢给模型 token 会爆，
   所以先在报告里给出一份带编号的候选清单，用户点名后再深挖。
2. **环境够不够**：录像解析依赖 JRE + 解析引擎 + zstandard，插件市场装来的
   用户多半没有。调用前先检测，缺什么就明说怎么装，而不是抛一串栈。
3. **不许并发**：云端容器内存上限 900 MiB，AstrBot 本体就占掉约 385 MiB，
   单场解析峰值约 160 MiB。一场很宽裕，两场同时跑必被 OOM kill ——
   而这个容器自己就是 PID 1，被杀等于整个机器人下线。
   所以所有解析任务必须过同一个单消费者队列。

编号约定
--------
候选清单里的「第 N 波」= ``teamfights`` 数组的第 N 项（从 1 开始、按时间顺序）。
**不做按关键度重排**：重排之后编号会随算法微调而漂移，用户上次说的
「第 7 波」下次就指向另一波了。清单只决定「展示哪几波」，不改变编号。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")

#: 解析引擎默认位置。放在 ``data/work`` 下是为了**容器重建后不丢**：
#: 该目录是宿主机挂载进来的，而容器的 /usr 是临时的。
DEFAULT_ENGINE_DIR = "/AstrBot/data/work/dota-replay"

#: 候选清单默认展示几波。
DEFAULT_TOP_N = 4

#: 队列里最多允许排几个（含正在跑的那个）。再多就拒绝，
#: 免得用户连点十次把一场 20 分钟的解析排到天荒地老。
MAX_QUEUE = 4


# ======================================================================
# 运行环境
# ======================================================================


@dataclass
class RuntimeStatus:
    """录像解析的运行环境检测结果。"""

    ok: bool = False
    java: str = ""
    engine: str = ""
    keyword: str = "/d2"
    problems: list[str] = field(default_factory=list)
    detail: list[str] = field(default_factory=list)

    def describe(self) -> str:
        """给用户看的说明（缺什么、怎么装）。"""
        if self.ok:
            return "✅ 录像解析环境就绪。"
        lines = ["⚠️ 录像深度解析需要额外运行环境，当前还缺："]
        for item in self.problems:
            lines.append(f"· {item}")
        lines.append("")
        lines.append("安装方法（在 AstrBot 所在机器执行）：")
        lines.append("1. 装 Java 运行时（21 或更高）：")
        lines.append("   `apt-get install -y openjdk-21-jre-headless`")
        lines.append("   或从 https://adoptium.net 下载 Temurin 21 的 JRE 解压到任意目录。")
        lines.append(f"2. 把解析引擎（classes/ 与 lib/）放到 {self.engine or DEFAULT_ENGINE_DIR}")
        lines.append("3. 装 Python 依赖：`pip install zstandard`")
        lines.append("")
        lines.append(f"装好后可用 `{self.keyword} 复盘 环境` 复查。")
        return "\n".join(lines)


def _resolve_java(engine_dir: str, java_path: str = "") -> str:
    """按「显式配置 → 引擎自带 → PATH」三级找 java。"""
    candidates = []
    if java_path:
        candidates.append(java_path)
    if engine_dir:
        candidates.append(os.path.join(engine_dir, "jre", "bin", "java"))
    found = shutil.which("java")
    if found:
        candidates.append(found)
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return ""


def _java_works(java: str, timeout: float = 10.0) -> bool:
    """``java -version`` 能不能正常跑完（能跑说明架构/glibc 都对得上）。"""
    try:
        proc = subprocess.run(  # noqa: S603 - 路径来自本地配置，不接受用户输入
            [java, "-version"],
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def check_runtime(engine_dir: str = "", java_path: str = "", keyword: str = "/d2") -> RuntimeStatus:
    """检测录像解析能不能跑。

    只做**便宜**的检查（文件存在 + ``java -version`` + import zstandard），
    不真的解析一场录像 —— 那个要 20 多秒，不该藏在「预检」里。
    """
    engine = (engine_dir or DEFAULT_ENGINE_DIR).strip()
    status = RuntimeStatus(engine=engine, keyword=keyword)
    status.detail.append(f"引擎目录：{engine}")

    # 1) 解析引擎
    need = {
        "classes/Snap.class": "血量与眼位采样器",
        "classes/Dump.class": "战斗日志导出器",
        "classes/Eco.class": "装备快照导出器",
        "lib/clarity-5.0.1.jar": "Clarity 录像解析库",
    }
    for rel, label in need.items():
        if not os.path.isfile(os.path.join(engine, rel)):
            status.problems.append(f"解析引擎缺文件：{rel}（{label}）")
    if not os.path.isdir(os.path.join(engine, "lib")):
        status.problems.append("解析引擎目录下没有 lib/（jar 依赖）")

    # 2) Java
    java = _resolve_java(engine, java_path)
    if not java:
        status.problems.append("找不到 Java 运行时（java / jre/bin/java 都没有）")
    elif not _java_works(java):
        status.problems.append(f"Java 存在但跑不起来：{java}（版本或架构不匹配）")
    else:
        status.java = java
        status.detail.append(f"Java：{java}")

    # 3) Python 解压依赖
    try:
        import zstandard  # noqa: F401

        status.detail.append("zstandard：已安装")
    except ImportError:
        status.problems.append("缺少 Python 模块 zstandard（用来解压 .dem.bz2）")

    status.ok = not status.problems
    return status


# ======================================================================
# 关键团候选（纯 OpenDota 数据，零成本）
# ======================================================================


def _clock(sec: Any) -> str:
    try:
        total = max(0, int(sec))
    except (TypeError, ValueError):
        return "-"
    return "%02d:%02d" % (total // 60, total % 60)


def score_fights(match: dict, focus_ids: list[int] | tuple[int, ...] = ()) -> list[dict]:
    """给每一波团打关键度，返回**按时间顺序编号**的完整列表。

    关键度的口径（刻意简单，可解释）：
    * 阵亡人数占主导（最能说明「这波打崩了」）；
    * 参战人数、持续时间次之；
    * 双方经济净变化差越大，说明结果越悬殊；
    * 后期（超过全局时长的 60%）的一波再加权 —— 后期的团更接近胜负手。

    焦点玩家在这波团的表现一并算进去（伤害占比、击杀、是否阵亡），
    用来在候选里标出「他打得好的那波」。
    """
    fights = match.get("teamfights") or []
    players = sorted(
        [p for p in (match.get("players") or []) if isinstance(p, dict)],
        key=lambda p: int(p.get("player_slot") or 0),
    )
    try:
        total_dur = int(match.get("duration") or 0)
    except (TypeError, ValueError):
        total_dur = 0
    focus = {int(i) for i in (focus_ids or [])}

    out: list[dict] = []
    for index, fight in enumerate(fights, start=1):
        if not isinstance(fight, dict):
            continue
        entries = fight.get("players") or []
        start = int(fight.get("start") or 0)
        end = int(fight.get("end") or 0)
        duration = max(0, end - start)
        deaths = int(fight.get("deaths") or 0)

        def _int(entry: Any, key: str) -> int:
            try:
                return int((entry or {}).get(key) or 0)
            except (TypeError, ValueError):
                return 0

        gold_rad = sum(_int(p, "gold_delta") for p in entries[:5])
        gold_dire = sum(_int(p, "gold_delta") for p in entries[5:])
        engaged = sum(1 for p in entries if _int(p, "damage") > 0)

        score = (deaths * 10.0
                 + engaged * 1.5
                 + min(duration, 60) * 0.2
                 + min(abs(gold_rad - gold_dire), 8000) / 800.0)
        if total_dur and start >= total_dur * 0.6:
            score += 3.0

        focus_note = None
        for slot, entry in enumerate(entries):
            owner = players[slot] if slot < len(players) else {}
            if int((owner or {}).get("account_id") or 0) not in focus:
                continue
            team_entries = entries[:5] if slot < 5 else entries[5:]
            team_damage = sum(_int(p, "damage") for p in team_entries)
            damage = _int(entry, "damage")
            focus_note = {
                "hero_id": owner.get("hero_id"),
                "damage": damage,
                "share": round(damage / team_damage * 100.0, 1) if team_damage else 0.0,
                "deaths": _int(entry, "deaths"),
                "kills": len([k for k in (entry.get("killed") or {})
                              if str(k).startswith("npc_dota_hero_")]),
                "gold_delta": _int(entry, "gold_delta"),
            }
            break

        out.append({
            "index": index,
            "start": start,
            "end": end,
            "duration": duration,
            "deaths": deaths,
            "engaged": engaged,
            "gold_delta": gold_rad - gold_dire,
            "score": round(score, 1),
            "focus": focus_note,
        })
    return out


def pick_candidates(cands: list[dict], top_n: int = DEFAULT_TOP_N) -> list[dict]:
    """从全部团战里挑出要展示的几波，**保持原编号**。

    除了关键度最高的几波，另外单独保一个「焦点玩家高光团」：
    他没死、伤害占全队比例最高的一波。用户要的正是「我打得好的那波」，
    而它未必在团队关键度榜上（比如顺风局的一次小规模收割）。
    """
    if not cands:
        return []
    picked: dict[int, str] = {}
    for c in sorted(cands, key=lambda c: -c["score"])[:top_n]:
        picked[c["index"]] = "关键"

    good = [c for c in cands
            if c.get("focus") and c["focus"]["deaths"] == 0 and c["focus"]["share"] >= 25.0]
    if good:
        best = max(good, key=lambda c: c["focus"]["share"])
        picked.setdefault(best["index"], "高光")

    return [c for c in cands if c["index"] in picked]


def _fight_tag(cand: dict, all_cands: list[dict]) -> str:
    """给一波团起一句短标签（只在有明显特征时给）。"""
    deaths = cand["deaths"]
    max_deaths = max((c["deaths"] for c in all_cands), default=0)
    tags = []
    if max_deaths >= 4 and deaths == max_deaths:
        tags.append("全场阵亡最多")
    elif deaths >= 6:
        tags.append("大规模团战")
    delta = cand["gold_delta"]
    if abs(delta) >= 5000:
        tags.append("经济差拉开 %d" % abs(delta))
    return "、".join(tags)


def render_candidates(
    match: dict,
    *,
    focus_ids: list[int] | tuple[int, ...] = (),
    focus_names: dict[int, str] | None = None,
    hero_name: Callable[[Any], str] | None = None,
    top_n: int = DEFAULT_TOP_N,
    keyword: str = "/d2",
    match_id: Any = "",
) -> str:
    """生成报告末尾的候选团战清单（纯文本，直接发给用户）。

    ⚠️ **必须是 Markdown 列表结构，不能靠「每行一句」+ 缩进来排版。**
    这份文本会被拼进复盘正文，而正文在 QQ 里是**纯文本**、在图片里是
    **Markdown 渲染**。前者的换行能看见，后者的单换行会被折叠成空格 ——
    于是同一份清单在聊天里还好，一转图就挤成一大坨（2026-11-05 用户截图）：

        · 第 3 波 08:01–08:51 · 阵亡 6 · 全场阵亡最多 某某 在这波：…

    只有 ``- `` / 缩进 ``  - `` 这种真正的列表块，两种渲染下都保证分行，
    且图片里还会自动带项目符号与缩进层级。
    """
    cands = score_fights(match, focus_ids)
    if not cands:
        return ""
    chosen = pick_candidates(cands, top_n)
    if not chosen:
        return ""

    names = focus_names or {}
    focus_aid = int(next(iter(focus_ids), 0) or 0) if focus_ids else 0
    who = names.get(focus_aid) or ("焦点玩家" if focus_aid else "")

    # 标题前先来一条分隔线，原因见下；标题后空一行，否则部分 Markdown
    # 实现会把「🎯 …」和第一项并成同一个段落。
    #
    # ⚠️ 分隔线不只是好看：清单是接在复盘正文**末尾**的，而正文经常以
    # 「1. …\n2. …」这种有序列表收尾。只有一个空行时，渲染出来跟上面的
    # 列表几乎贴在一起（图片里看着就像第 3 个列表项），反而更乱
    # （2026-11-05 第一次改完的实拍效果）。``---`` 会被渲染成一条真正的
    # 横线，两种渲染下都能明确「下面是另一段东西」。
    lines = ["", "---", "", "本场值得单独深挖的团战（录像级复盘）", ""]
    for cand in chosen:
        head = "第 %d 波 %s–%s · 阵亡 %d" % (
            cand["index"], _clock(cand["start"]), _clock(cand["end"]), cand["deaths"])
        tag = _fight_tag(cand, cands)
        if tag:
            head += " · " + tag
        lines.append("- " + head)
        note = cand.get("focus")
        if note and who:
            if hero_name:
                hero = hero_name(note.get("hero_id"))
            else:
                hero = "本命"
            bits = ["伤害 %d（占全队 %.0f%%）" % (note["damage"], note["share"])]
            if note["kills"]:
                bits.append("击杀 %d" % note["kills"])
            bits.append("阵亡" if note["deaths"] else "存活")
            # 子项用两空格缩进 + `- `：Markdown 的嵌套列表，渲染成缩进一层的
            # 次级条目；聊天里也是一行一条，比「名字 在这波：英雄 伤害…」
            # 这种一行到底的写法容易扫读得多。
            #
            # 名字与英雄用「 · 」隔开而不是「名字（英雄）」：后面还有
            # 「伤害 1925（占全队 36%）」，括号套括号读起来很绕。
            lines.append("  - %s · %s：%s" % (who, hero, "、".join(bits)))

    lines.append("")
    mid = str(match_id or "")
    example = "%s 团战 %s %d" % (keyword, mid, chosen[0]["index"])
    lines.append("想看某一波的录像级细节（技能命中、控了多久、血量与视野），回复：")
    lines.append("")
    # ⚠️ 命令**不要**包反引号（Markdown 代码样式）。本地渲染兜底时
    # 等宽字体没有中文字形，「团战」两个字会变成豆腐块 —— 变成
    # `/d2 ⯑⯑ 9073115303 4`，用户连抄都抄不了（2026-11-05 实拍）。
    # 网络渲染没事、本地兜底就废，而兜底恰恰在网络端点不可用时发生。
    lines.append(example)
    return "\n".join(lines)


# ======================================================================
# 单线队列
# ======================================================================


class QueueFull(Exception):
    """排队的人太多，直接拒绝而不是无限堆。"""


class ReplayQueue:
    """**单消费者**队列：同一时刻只会有一个解析任务在跑。

    实现刻意不用 ``asyncio.Semaphore`` —— 信号量允许 N 个任务同时进入，
    这里要的是「一个接一个」，并且还要能回答「前面还有几个」。
    """

    def __init__(self, max_queue: int = MAX_QUEUE) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._max = max_queue
        self._current: str = ""
        self._running = 0

    @property
    def current(self) -> str:
        """正在跑的任务标签（空字符串表示空闲）。"""
        return self._current

    def pending(self) -> int:
        """还没开始跑的任务数（不含正在跑的那个）。"""
        return self._queue.qsize()

    def status_text(self) -> str:
        if self._current:
            extra = f"，后面还有 {self.pending()} 个" if self.pending() else ""
            return f"当前正在解析：{self._current}{extra}"
        return "解析队列空闲"

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._loop())

    async def run(self, label: str, fn: Callable[[], Awaitable[T]]) -> T:
        """把一个任务排进队列，等它**轮到自己**并跑完，返回结果。

        ``fn`` 是个零参协程工厂：调用时已经开始执行，所以下载/解析要写在
        工厂内部，不能提前创建协程（提前创建会在排队期间就「预热」起来）。
        """
        if self._queue.qsize() >= self._max:
            raise QueueFull(f"解析队列已满（最多排 {self._max} 个）")
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        await self._queue.put((label, fn, future))
        self._ensure_worker()
        return await future

    async def _loop(self) -> None:
        while True:
            label, fn, future = await self._queue.get()
            self._current = label
            self._running += 1
            try:
                if future.cancelled():
                    continue
                result = await fn()
                if not future.done():
                    future.set_result(result)
            except asyncio.CancelledError:
                if not future.done():
                    future.cancel()
                raise
            except Exception as exc:  # noqa: BLE001 - 单个任务失败不能打死 worker
                if not future.done():
                    future.set_exception(exc)
            finally:
                self._running -= 1
                self._current = ""
                self._queue.task_done()

    async def stop(self) -> None:
        """插件卸载时调用：取消 worker，并把还在排队的任务一并取消。"""
        worker = self._worker
        self._worker = None
        if worker is not None and not worker.done():
            worker.cancel()
            try:
                await worker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        while not self._queue.empty():
            try:
                _label, _fn, future = self._queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - 竞态兜底
                break
            if not future.done():
                future.cancel()
            self._queue.task_done()


def parse_fight_reference(text: str) -> int | None:
    """把用户说的「第 7 波 / 7 / 第7波团战」解析成团战序号。

    只接受**纯数字**或带「波 / 团」的说法。刻意宽松解析中文数字以外的形式，
    但拒绝「上次那波」这类指代 —— 那种要模型结合上下文判断，不该在这里猜。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    digits = ""
    for ch in raw:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    if not digits:
        return None
    try:
        value = int(digits)
    except ValueError:
        return None
    if 1 <= value <= 60:
        return value
    return None
