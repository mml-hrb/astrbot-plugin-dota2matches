# -*- coding: utf-8 -*-
"""英雄画像知识库：把 ``hero_profiles.json`` 灌进 AstrBot 知识库。

为什么要有这个模块（2026-11-12 用户指出）
-----------------------------------------
画像原来只喂给复盘。想在闲聊里问「敌法师克谁」就查不到 —— 模型手里没有
那份数据，只能凭常识答。做成知识库是为了让**闲聊也能检索**。

但**建库这件事不能靠人工跑一次脚本**：插件从市场装下来的人，既不知道
要去建库、也不会有我的运维脚本。所以这里做成插件自带的两个入口：

1. **自动尝试**（``ensure``）：插件启动完成后调用一次。已建好就跳过；
   宿主没配嵌入模型就安静跳过（记一条 info，不报错）。
2. **手动触发**：``/d2 知识库 重建``，让人在 WebUI 改了 provider 之后
   不必重启插件。

⚠️ 三条必须守住的界线
---------------------
* **不写死嵌入 provider。** 谁配了就用谁：遍历 ``embedding_provider_insts``
  取第一个可用的。写死某个 id 的话，别人装下来必然失败。
* **没有嵌入模型不是错误。** 这是最常见的情况（很多部署没有嵌入服务），
  此时**静默跳过**，画像仍然只喂复盘 —— 功能少一块，但不能报错、不能刷屏。
* **不阻塞启动。** 建库要发 127 次嵌入请求（首次约 40 秒）。放在启动路径上
  必须用后台任务，否则「AstrBot 启动完成」会晚一分钟。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - 离线单测
    import logging

    logger = logging.getLogger("dota2.hero_kb_ingest")

#: 知识库名。检索时按名字找 —— AstrBot 的 retrieve 接口只认名字。
KB_NAME = "dota2_hero_profiles"

#: 入库用的字段与中文标题。**与 `dota_hero_kb.LABELS` 同口径**，
#: 但这里刻意不复用那份 dict：入库文本是给检索用的长文（带 markdown 标题），
#: 提示词渲染是给模型读的短行，两种排版后续可能各自演进。
FIELD_TITLES: tuple[tuple[str, str], ...] = (
    ("position", "常见位置（参考，非限定）"),
    ("builds", "流派与识别"),
    ("power_source", "输出来自哪里"),
    ("aghs", "A 杖"),
    ("shard", "魔晶"),
    ("damage", "伤害构成"),
    ("survival", "生存与防御"),
    ("curve", "强弱时间轴"),
    ("cannot", "不能做什么"),
    ("counters", "克制什么"),
    ("fears", "怕什么"),
    ("fear_response", "怕的东西怎么应对"),
    ("synergy", "需要什么队友"),
    ("win_condition", "取胜条件"),
    ("review_baseline", "复盘基准"),
)

#: 建库用的 chunk 参数。
#:
#: ⚠️ ``chunk_size`` 给得比默认 512 大很多，是刻意的：**一条画像约 1553 字**，
#: 用 512 会被切成 3~4 块，而「克制什么」和「怕什么」必须成对读才有意义
#: （A 克 B ⟸ A 的 counters 命中 B 的 fears）。切开就是丢语义。
#: 2048 能让绝大多数英雄整条落在一块里；重叠给 0，因为整条不切就没有边界问题。
CHUNK_SIZE = 2048
CHUNK_OVERLAP = 0

#: 入库时给知识库写的说明。
KB_DESCRIPTION = (
    "Dota2 127 个英雄的人工画像：常见打法、克制与被克制、什么算失职。"
    "由插件自动建立与维护。"
)


def render_document(name: str, rec: dict) -> str:
    """把一个英雄的画像渲染成一篇可检索的 markdown。

    标题层级用 ``#`` / ``##`` 而不是纯文本，是为了让 AstrBot 的分块器
    （``RecursiveCharacterChunker``）优先在标题处断行 —— 万一某条画像超过
    chunk_size 被拆开，也能拆在字段边界上，而不是拆在一句话中间。
    """
    lines = ["# %s 英雄判读参考" % name, ""]
    lines.append("以下为常见对局里的主流打法，不是限定；本局打法不同时以本局为准。")
    lines.append("")
    for key, title in FIELD_TITLES:
        val = str(rec.get(key) or "").strip()
        if val:
            lines.append("## %s" % title)
            lines.append(val)
            lines.append("")
    return "\n".join(lines).strip()


def load_profiles(plugin_dir: Path | None = None, data_dir: Path | None = None) -> dict:
    """读英雄画像：包内静态表为底，数据目录同名文件逐条覆盖。

    与 :meth:`dota_hero_kb.HeroKnowledge._table` 同一套「三级查找」口径 ——
    两处必须一致，否则「复盘用的画像」和「知识库里的画像」会是两份内容，
    用户改了数据目录那份，闲聊里查到的还是旧版。

    Returns:
        ``{hero_id: {...}}``；读不到返回空字典（**不抛异常**，调用方据此跳过）。
    """
    merged: dict[str, dict] = {}

    for path in (
        (plugin_dir / "hero_profiles.json") if plugin_dir else None,
        (data_dir / "hero_profiles.json") if data_dir else None,
    ):
        if path is None:
            continue
        if not path.exists():
            # 数据目录那份是**可选覆盖**，正常部署里就没有 —— 这不是问题，
            # 别打日志（原来打了一条「读不到」，看着像故障，实际每天都在报）。
            continue
        try:
            raw = path.read_bytes().decode("utf-8-sig")
            payload = json.loads(raw)
        except (OSError, ValueError) as e:
            logger.info("[dota2] 英雄画像读不到（%s）：%s", path, e)
            continue
        heroes = payload.get("heroes") if isinstance(payload, dict) else None
        if isinstance(heroes, dict):
            merged.update(heroes)

    return merged


def pick_embedding_provider(provider_manager: Any) -> Any:
    """从已加载的 provider 里挑一个能做嵌入的。

    **不写死 provider id**：别人装插件时配的嵌入服务可能完全不是我这边用的那个
    （硅基流动 / DashScope / 本地 ollama 都行）。这里只要求「有这个能力且可用」。

    Returns:
        provider 实例；没有返回 ``None``（调用方据此静默跳过建库）。
    """
    insts = getattr(provider_manager, "embedding_provider_insts", None) or []
    for inst in insts:
        # 有的适配器带 enable 标记；没有就当它可用（被禁用的一般不会进这个列表）。
        if getattr(inst, "enable", True) is False:
            continue
        if callable(getattr(inst, "get_embeddings", None)) or callable(
            getattr(inst, "get_embedding", None)
        ):
            return inst
    return None


def provider_id_of(inst: Any) -> str:
    """取 provider 的 id（建库时必须传给 ``create_kb``）。"""
    meta = getattr(inst, "meta", None)
    if callable(meta):
        try:
            return str(meta().id or "")
        except Exception:  # noqa: BLE001
            pass
    for attr in ("provider_id", "id"):
        val = getattr(inst, attr, None)
        if val:
            return str(val)
    cfg = getattr(inst, "provider_config", None)
    if isinstance(cfg, dict) and cfg.get("id"):
        return str(cfg["id"])
    return ""


async def ensure(
    kb_manager: Any,
    profiles: dict,
    *,
    force: bool = False,
    marker_path: Path | None = None,
) -> tuple[bool, str]:
    """确保知识库存在且已灌满。幂等。

    流程：找嵌入 provider → 找/建知识库 → 逐条补缺失的文档 → 写 marker。

    Args:
        kb_manager: ``context.kb_manager``。
        profiles: :func:`load_profiles` 的返回值。
        force: True 时忽略 marker 与现有文档，强制重灌（用于画像更新后重建）。
        marker_path: 成功后的标记文件。存在即跳过（除非 ``force``）。
            放 marker 而不是每次都查文档数，是为了让**正常启动**少一次 SQL 查询；
            真正判断「有没有灌全」在下面按 doc_name 逐条比，不依赖 marker。

    Returns:
        ``(是否新建/补灌了东西, 给人看的结论)``。
    """
    if not profiles:
        return False, "本地没有英雄画像数据，跳过建库"

    if marker_path is not None and marker_path.exists() and not force:
        return False, "已建过库（找到 marker），跳过"

    pm = getattr(kb_manager, "provider_manager", None)
    ep = pick_embedding_provider(pm)
    if ep is None:
        # 最常见的正常情况：宿主没配嵌入模型。**这不是错误**，静默跳过建库，
        # 但要留一条**可查到的**提示 —— 只打 info 等于没人看得见，
        # 用户只会觉得「闲聊里问英雄怎么查不到」，却不知道差的是嵌入模型。
        # 具体怎么配写在 `/d2 知识库` 的回复里（那里才有足够篇幅讲清楚）。
        logger.info(
            "[dota2] 宿主没有配置嵌入模型，跳过英雄画像建库。"
            "画像仍照常注入复盘；想让闲聊也能查，配一个嵌入类型的 provider 后发 "
            "`/d2 知识库 重建`（详见该指令的提示）。"
        )
        return False, "宿主没有可用的嵌入模型，跳过建库（画像仍只用于复盘）"

    pid = provider_id_of(ep)
    if not pid:
        return False, "嵌入模型缺少 id，跳过建库"

    try:
        helper = await kb_manager.get_kb_by_name(KB_NAME)
    except Exception as e:  # noqa: BLE001 - 知识库模块自身没就绪
        return False, "知识库模块不可用（%s），跳过建库" % e

    created = False
    if helper is None:
        helper = await kb_manager.create_kb(
            kb_name=KB_NAME,
            description=KB_DESCRIPTION,
            emoji="🗡️",
            embedding_provider_id=pid,
            chunk_size=CHUNK_SIZE,
            chunk_overlap=CHUNK_OVERLAP,
        )
        created = True

    existing: set[str] = set()
    if not force:
        try:
            docs = await helper.list_documents(limit=1000)
            existing = {d.doc_name for d in docs}
        except Exception as e:  # noqa: BLE001
            logger.info("[dota2] 读知识库文档列表失败（%s），按空处理", e)

    ok = skip = 0
    failed: list[tuple[str, str]] = []
    items = sorted(profiles.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0)
    for hid, rec in items:
        if not isinstance(rec, dict):
            continue
        name = str(rec.get("name") or ("英雄#%s" % hid))
        fname = "%s.md" % name
        if fname in existing:
            skip += 1
            continue
        try:
            await helper.upload_document(
                file_name=fname,
                file_content=None,
                file_type="md",
                pre_chunked_text=[render_document(name, rec)],
            )
            ok += 1
        except Exception as e:  # noqa: BLE001 - 单条失败不该中断整批
            failed.append((name, str(e)[:100]))

    total = skip + ok
    msg = "英雄画像知识库：新增 %d，已有 %d，失败 %d（共 %d）" % (
        ok, skip, len(failed), len(profiles))
    if failed:
        msg += "；失败示例：" + "、".join(n for n, _ in failed[:3])
        logger.warning("[dota2] %s", msg)
    else:
        logger.info("[dota2] %s", msg)

    # 只有「该有的都有了」才写 marker：失败过半就不写，下次启动会再补
    if marker_path is not None and len(failed) < max(1, len(profiles) // 10):
        try:
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.write_text(
                "docs=%d\ntotal=%d\n" % (total, len(profiles)), encoding="utf-8"
            )
        except OSError as e:
            logger.info("[dota2] 写建库 marker 失败：%s", e)

    return bool(created or ok), msg


async def search(
    kb_manager: Any,
    query: str,
    *,
    top: int = 3,
    timeout: float = 20.0,
) -> str:
    """在英雄画像知识库里检索，返回可直接塞进提示词的文本。

    检索失败/知识库不存在都返回空串 —— 调用方据此退化到本地精确查找，
    而不是把一句报错喂给模型。
    """
    if not query.strip():
        return ""
    try:
        res = await asyncio.wait_for(
            kb_manager.retrieve(query=query, kb_names=[KB_NAME], top_m_final=top),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.info("[dota2] 英雄画像检索超时（%.0fs）", timeout)
        return ""
    except Exception as e:  # noqa: BLE001 - 没建库 / 模块没就绪都会到这里
        logger.info("[dota2] 英雄画像检索不可用：%s", e)
        return ""

    items = (res or {}).get("results") or []
    if not items:
        return ""

    parts: list[str] = []
    seen: set[str] = set()
    for r in items:
        doc = str(r.get("doc_name") or "").strip()
        body = str(r.get("content") or "").strip()
        if not body or doc in seen:
            continue
        seen.add(doc)
        parts.append(body)
    if not parts:
        return ""

    head = (
        "以下是英雄判读参考（人工整理，**不是当前版本统计**）。"
        "本局打法与参考不同时按本局判，不要拿参考去否定他。\n\n"
    )
    return head + "\n\n---\n\n".join(parts)