# -*- coding: utf-8 -*-
"""双端共享的纯业务规则（能力契约的唯一裁决点）。

只放「桌面与手机各写一份就会漂移」的纯判断：不 import Qt / Flet，
不弹窗、不落库、不发网络请求。桌面 `app/ui/` 与手机 `src/` 都必须调用这里，
禁止在两端 UI 里另写一份同义判断。

对应能力矩阵：`docs/双端能力矩阵.md`。
新增判断先加到这里，再在两端接线；矩阵未登记不许声称「已支持」。
"""
from __future__ import annotations

from typing import Iterable


def prose_ready(store, chapter_id: int) -> bool:
    """本章正文是否已写完。

    判据：整章正文块存在、内容非空、状态为「已接受 / 已锁定」。
    分批正文最终也会合并成整章块，所以只看这一处即可。
    """
    tb = store.get_chapter_prose_block(chapter_id)
    return bool(tb and (tb.get("content") or "").strip()
                and tb.get("status") in ("已接受", "已锁定"))


def chapter_todo(store, chapter: dict, *, supports_detail: bool = True) -> dict | None:
    """单章待办：返回第一件没做的事；全做完返回 None。

    规则（2026-09-05 归档幂等口径，双端统一）：
    1. 大纲没细化 → outline；
    2. 写法不是「按大纲直写」且没有任何细纲块 → detail
       （端不支持细纲时跳过，直接从章大纲写正文）；
    3. 正文没写完 → prose；
    4. 没有记忆快照（过期也算有）→ archive；
    5. 全做完 → None。

    **不能只看 chapters.status 判「完本」**：正文一篇没写时 status 仍可能是
    「有大纲」，按状态判断会直接把整本书当完成（手机 2026-11-17 前的老毛病）。

    `supports_detail=False` 只给手机端：手机不生成细纲，直接从章大纲写正文。
    """
    cid = chapter["id"]
    status = chapter.get("status") or "未规划"
    # 老数据的兜底：状态已经推进到细纲/写作/完成，说明大纲必然细化过；
    # 只有「未规划 / 有大纲」才需要看 outline_refined 标记。
    outline_done = bool(chapter.get("outline_refined")) or status in (
        "有细纲", "写作中", "已完成", "已归档", "已锁定")
    if not outline_done:
        return {"kind": "outline", "chapter": chapter}
    if (supports_detail and store.get_prose_mode() != "outline"
            and not store.list_blocks(cid)):
        return {"kind": "detail", "chapter": chapter}
    if not prose_ready(store, cid):
        return {"kind": "prose", "chapter": chapter}
    if not store.has_memory_snapshot(cid):
        return {"kind": "archive", "chapter": chapter}
    return None


def unsaved_prose_text(editor_text: str, saved_texts: Iterable[str]) -> str:
    """编辑器里的稿子相对已落库文本算不算「未存稿」。

    返回未存稿原文（保留用户输入的空白，便于暂存/提示）；以下情况返回空串：
    - 编辑器为空或全是空白；
    - 与任一已落库文本按 strip 后相等（纯空白差异不算改过）。

    两端必须用同一口径：桌面拿它决定切章暂存，手机拿它决定切章前是否拦一下。
    """
    text = editor_text or ""
    if not text.strip():
        return ""
    norm = text.strip()
    for saved in saved_texts or ():
        if norm == (saved or "").strip():
            return ""
    return text


def rewrite_locked_reason(block: dict | None) -> str:
    """正文块是否禁止改写；返回空串表示可改写。"""
    if block and block.get("status") == "已锁定":
        return "当前正文已锁定，不可改写"
    return ""


def volume_title_or_default(title: str | None) -> str:
    """分卷标题兜底：空白一律「未命名分卷」（两端同一口径）。"""
    return (title or "").strip() or "未命名分卷"


def volume_chapter_rule(store) -> tuple[str, int | None]:
    """读项目分卷设置 → (模板 {chapter_rule} 文本, 期望章数)。

    两端自动模式共用同一段提示词，避免「桌面要求正好 N 章、手机不设限」
    导致同一个总纲切出不同章数。`expected` 为 None 表示不设硬性章数。
    """
    mode = str(store.get_setting("volume_chapter_mode", "auto") or "auto")
    raw = str(store.get_setting("volume_chapter_count", "") or "").strip()
    expected = int(raw) if (mode == "exact" and raw.isdigit()) else None
    if expected is not None:
        return (f"本卷必须正好切分为 {expected} 章。"
                f"必须输出且仅输出 {expected} 个章节标题，不得多于或少于 {expected} 章。",
                expected)
    return "本卷章数不设硬性规定，由你根据剧情节奏与总纲篇幅自行判断。", None


def parse_volume_split(text: str, expected: int | None = None):
    """分卷切章的统一解析与门槛：返回 (chapters, err)。

    - 单章正文最短 `MIN_VOLUME_CHAPTER_CHARS`（卡掉只回标题/一小段衔接的空壳章）；
    - 指定 expected 时，实际章数必须完全一致，否则拒绝写入。
    """
    from .pipeline import MIN_VOLUME_CHAPTER_CHARS, Pipeline

    chapters, err = Pipeline.parse_volume_chapters(
        text, min_chars=MIN_VOLUME_CHAPTER_CHARS)
    if err or not chapters:
        return None, f"切章解析失败：{err or '结果为空'}"
    if expected is not None and len(chapters) != expected:
        return None, f"要求 {expected} 章，实际 {len(chapters)} 章，已拒绝写入"
    return chapters, ""
