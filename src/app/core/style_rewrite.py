# -*- coding: utf-8 -*-
"""文风改写编排（流水线第 6 环）：整章「去 AI 味」句子层改写的共用内核。

双端共用（桌面写作台 / 自动模式 / 手机内核）：提示词组装、调用编排、差量替换
全部在本模块，UI 层只做交互（线程、预览落库）。

单模式（2026-10-06 老板拍板，删除旧「全章扫描 + 逐批执行」多调用分批流程）：
整章一次调用完成扫描与执行，输出差量条目——模型只回传「P段号：改写后的整段」，
没有病灶的段落一字不提，由 apply_diff 程序确定性拼回初稿、不经模型。
按次计费渠道只花一次调用的钱，保留段零误伤。
"""
from .pipeline import Pipeline, number_paragraphs, split_paragraphs
from .task_templates import render as render_task


def apply_diff(draft: str, replacements: dict[int, str]) -> tuple[str, list[int], list[int]]:
    """把差量条目替换进初稿 → (成稿, 已替换段号列表, 越界忽略段号列表)。

    替换是程序确定性操作：没有条目的段落一字不动地原样保留（不经模型），
    「照抄不伤」由实现保证而非提示词约束。重复段号已在解析层取后值。
    """
    paras = split_paragraphs(draft)
    applied: list[int] = []
    skipped: list[int] = []
    for no, body in replacements.items():
        if 1 <= no <= len(paras):
            paras[no - 1] = body
            applied.append(no)
        else:
            skipped.append(no)
    return "\n\n".join(paras), applied, skipped


def _chapter_task_args(chapter: dict) -> dict:
    return {"chapter_no": chapter.get("order_index", "?"),
            "chapter_title": chapter.get("title") or "未命名"}


def build_whole(store, chapter: dict, draft: str) -> tuple[dict, str]:
    """整章模式：一次调用（扫描+执行）→ (context, task)。

    ctx["rewrite_text"] 是带自然段编号的初稿（P1：段落），段号即差量条目
    的替换依据，编号规则必须与 parse_style_rewrite_diff / apply_diff 一致。
    """
    ctx = {"chapter": chapter,
           "character_names": chapter.get("characters") or [],
           "rewrite_text": number_paragraphs(draft)}
    task = render_task("prose_rewrite_whole", store, **_chapter_task_args(chapter))
    return ctx, task


def run_whole_call(store, chapter: dict, draft: str, runner,
                   stop_event=None) -> dict:
    """整章调用（自动模式用；桌面手动走 run_generation + build_whole）。

    差量替换：模型只回传「P段号：改写后的整段」条目，这里解析后由
    apply_diff 替换进初稿——没有条目的段落原样保留（不经模型）。
    返回 {ok, text(整章成稿), changed(替换段号), skipped(越界段号),
    none_declared(模型声明全章无病灶), error, stopped}。
    """
    if stop_event is not None and stop_event.is_set():
        return {"ok": False, "stopped": True, "error": "已停止", "text": "",
                "changed": [], "skipped": [], "none_declared": False}
    ctx, task = build_whole(store, chapter, draft)
    r = runner("prose_rewrite", ctx, task)
    if r.get("status") == "stopped":
        return {"ok": False, "stopped": True, "error": r.get("error") or "已停止",
                "text": "", "changed": [], "skipped": [], "none_declared": False}
    if not r.get("ok"):
        return {"ok": False, "stopped": False, "error": r.get("error") or "改写调用失败",
                "text": "", "changed": [], "skipped": [], "none_declared": False}
    text = (r.get("text") or "").strip()
    if not text:
        return {"ok": False, "stopped": False, "error": "改写输出为空", "text": "",
                "changed": [], "skipped": [], "none_declared": False}
    info: dict = {}
    repl = Pipeline.parse_style_rewrite_diff(text, info)
    if not repl and not info.get("none_declared") and info.get("noise_lines"):
        # 一条条目都没认出来，还净是噪声行 → 大概率没按格式输出（如重抄了全文）
        return {"ok": False, "stopped": False,
                "error": "未识别到任何替换条目（P段号：改写后的段落），输出可能不符合格式",
                "text": "", "changed": [], "skipped": [], "none_declared": False}
    merged, applied, skipped = apply_diff(draft, repl)
    return {"ok": True, "stopped": False, "error": "", "text": merged,
            "changed": applied, "skipped": skipped,
            "none_declared": bool(info.get("none_declared"))}
