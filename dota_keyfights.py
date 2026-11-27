"""单场归因的关键团录像取证；不调用模型，不截取窗口的前半段。

本模块只产出**文本**（证据块、提示词、汇总头），模型调用留在 ``main.py``：
取证是确定性的，提示词是口径，两件事分开写才能各自单独回归。
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

HEADER = """## 关键团战录像证据（归因前置取证）
以下是自动筛选的关键团完整窗口，不是整场全部战斗，也不是额外生成的团战报告。
以胜率变化定位重点，无曲线时回退团战统计。原始编号沿用 teamfights。
未选中、日志缺失、空事件均不等于没有贡献；重叠窗口不可重复累计。
持续伤害记录数不是命中率；死亡不等于没施法，击杀者不等于全部致死原因。
只有明确的施法/控制/位置与装备时序才能支持个人失误；未知冷却、视野和操作机会不得补写。
先核对关键团过程，再结合整场、阵容及资源判断；不能把末团活下来反推早出某装备必胜。
"""

#: 逐波核验的系统提示词。
#:
#: 和主报告的 ``analysis_system_prompt`` 刻意分开：主报告写整场叙事，这一层
#: 只负责把**一波**团的日志还原成画面并给评测。实测用报告那套提示词时，模型
#: 会把注意力拉回整场（「这场比赛的关键在于……」），这波团的细节反而丢了 ——
#: 而这层存在的全部意义就是细节。
BRIEF_SYSTEM = """你是一位 Dota 2 教练，正在看一场真实比赛里一波团战的完整录像数据。
数据里有：双方血量与蓝量轨迹、相对坐标、控制技能的实际持续时长、技能与道具施放记录、眼位与装备时序。

你的任务不是复述统计，而是把这份日志还原成画面，再据此评测。

硬性要求：
1. 只依据给出的日志与数据。日志里没有的，就说「日志没有记录」，不要用通用常识补写。
2. 时间一律用比赛时间，写清「第几秒谁做了什么」，不要用「前期」「随后」这种模糊指代。
3. 判定失误必须同时给出「当时应该怎么做」和「凭什么说他来得及」——他当时的位置、有没有被控、技能在不在手上。
4. 买活、技能冷却、队伍语音、视野遮挡都不在数据里，写「未知」，不要猜。
5. 死亡不等于打得差。先看清当时的条件（人数、血量、等级、装备），本来就是必败的团要明说。
6. 一次只写这一波，不要延伸到整场，也不要评价没出现在这波里的人。"""

#: 汇总段的标题。主报告拿到的已经不是原始日志，而是每波各自核验后的结论，
#: 所以措辞要跟着变：不能再对主报告说「未选中不等于没有贡献」这种对原始证据讲的话。
BRIEF_HEADER = """## 关键团逐波核验（录像级，每波已单独审查）
以下每一波都由该波自己的完整录像窗口单独核验过，结论可以直接引用。
没有列入的团战不代表没有人贡献；几波之间不要重复累计同一次交换。
只有日志里明确出现过的施法、控制、位置，才能作为个人失误的依据。"""


def _clock(seconds: float) -> str:
    total = int(round(seconds))
    return "%d:%02d" % (total // 60, total % 60)


def brief_prompt(
    index: int,
    start: float,
    end: float,
    *,
    reason: str = "关键",
    focus_label: str = "",
    block: str = "",
) -> str:
    """拼一波团的核验提示词。

    Args:
        index: ``teamfights`` 里的原始编号（不重新排序，用户点团时用的是它）。
        focus_label: 主视角玩家，形如「钢板（本局玩的是 亚巴顿）」。空串表示
            没指定焦点，这一节就不出现 —— 让模型自己判断谁该被评，比凭空
            塞一个「请点评这个人」更诚实。
        block: :func:`dota_replay_parse.build_fight_block` 的完整输出。
    """
    duration = max(0, int(round(end - start)))
    head = [
        "这是第 %d 波团（比赛时间 %s–%s，持续 %d 秒；筛选理由：%s）。"
        % (index, _clock(start), _clock(end), duration, reason),
    ]
    if focus_label:
        head.append("主视角：%s。" % focus_label)
    head.extend([
        "",
        "请按下面三节输出，不要写成表格：",
        "一、画面还原：这波团怎么打起来的、谁先手、控住了谁、谁先倒、谁把谁换掉，按时间顺序写。",
        "二、主视角表现：这个人在这一波的位置、施法、承伤与阵亡是否可避免，哪里能做得更好。",
        "三、本波胜负手：哪几秒决定了结果，落后的一方当时还有没有别的选择。",
        "",
        "400–700 字，只写这一波。",
        "",
        "以下是这一波的完整录像数据：",
        "",
        block,
    ])
    return "\n".join(head)


async def collect(
    selected: list[dict],
    build: Callable[[dict], Awaitable[str]],
    *,
    budget: int = 30000,
) -> str:
    """每波全取或明确缺席，绝不用字符截断伪装完整窗口。"""
    parts = [HEADER]
    used = len(HEADER)
    for candidate in selected:
        title = "\n### 原始第 %s 波（%s–%s 秒；%s）\n" % (
            candidate['index'], candidate['start'], candidate['end'],
            candidate.get('pick_reason', '关键'),
        )
        try:
            block = await build(candidate)
            if not block.strip():
                block = '录像证据不可用：该窗口没有可核验日志；不能据此认定无人参战。'
            elif used + len(title) + len(block) > budget:
                block = '录像证据未注入：完整窗口超过剩余输入预算；没有截取前半段，不得当作零贡献。'
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 证据增强失败不应丢掉基础复盘
            block = f'录像证据不可用（{type(exc).__name__}）；继续使用已提供的基础数据，不作录像级断言。'
        parts.extend((title, block))
        used += len(title) + len(block)
    return '\n'.join(parts)
