"""单场归因的关键团录像取证；不调用模型，不截取窗口的前半段。"""
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
