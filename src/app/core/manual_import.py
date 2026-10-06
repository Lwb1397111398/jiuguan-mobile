# -*- coding: utf-8 -*-
"""手动导入（粘贴文本 → 识别入库）的文本预处理。

老板需求（2026-09-16）：五个创作阶段都能把自己写的 / 别处拿到的文本直接喂进
识别管道，不必先让 AI 跑一遍。

粘贴进来的文本不是 pipeline 产物，可能带着别处的思考段、`<content>` 标签、
【生成完毕】这类完成标志行。这里做**与 pipeline 输出同源**的清洗，保证手动路径
与 AI 路径的入库口径一致（AI 路径的清洗在 `Pipeline._complete_and_log`）。

红线：**只剥不改**。任何情况下不得补全、改写、润色用户粘贴的文本；
清洗不彻底可以接受，丢用户内容不可以。
"""

from .endmark import strip_inline_end_marks
from .textclean import strip_jailbreak_residue, strip_model_output

# 各阶段的 kind：只用于区分「正文类」与「结构化类」的后续扩展，当前清洗口径相同
KIND_SYNOPSIS = "synopsis"
KIND_VOLUME = "volume"
KIND_OUTLINE = "outline"
KIND_DETAIL = "detail"
KIND_PROSE = "prose"


def clean_manual_text(text: str) -> dict:
    """清洗手动粘贴的文本 → {"text", "notes", "chars", "changed"}。

    notes：本次实际做过的处理（UI 用来提示「已自动剥离思考段」这类信息），
    没做的事不写，避免老板以为软件动过他的文本。

    处理顺序与 pipeline 一致：换行规整 → 剥思考段 → 剥破甲残留 → 剥完成标志行。
    全程幂等，重复调用结果不变。
    """
    raw = text or ""
    notes: list = []

    # 1. 基础规整：去 BOM、统一换行、去首尾空白。不动全角空格（中文正文首行缩进靠它）
    t = raw.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    if t != raw:
        notes.append("统一换行符 / 去 BOM")
    if t != t.strip():
        t = t.strip()
        notes.append("去除首尾空白")

    # 2. 剥思考段（内联标签、『思考环节…正式输出』分段、无标记前言）。
    #    无标记时为直通（has_think_marks 为假直接返回原文），不会动正常行文。
    cleaned, _think, info = strip_model_output(t)
    if info.get("tags") or info.get("sections") or info.get("inline"):
        notes.append("剥离思考段")
    if info.get("failed"):
        notes.append("思考剥离判定异常，已保留原文")
    t = cleaned

    # 3. 破甲残留：<content>/伪标签行、锚点引导的思考块。章节名行/冒号行不碰
    #    （正文专用清洗 strip_prose_residue 由正文解析器内部按需调用）。
    stripped = strip_jailbreak_residue(t)
    if stripped != t:
        notes.append("剥离破甲残留")
    t = stripped

    # 4. 「强形式」完成标志行（【生成完毕】《完》等）；『本章完』『全文完』不删
    t2, removed = strip_inline_end_marks(t)
    if removed:
        notes.append(f"剥离完成标志行 {removed} 行")
    t = t2

    return {"text": t, "notes": notes, "chars": len(t.strip()),
            "changed": bool(notes)}
