# -*- coding: utf-8 -*-
"""文风改写编排（流水线第 6 环）：整章「去 AI 味」句子层改写的共用内核。

双端共用（桌面写作台 / 自动模式 / 手机内核）：提示词组装、调用编排、差量替换
全部在本模块，UI 层只做交互（线程、预览落库）。

单模式（2026-10-06 老板拍板，删除旧「全章扫描 + 逐批执行」多调用分批流程）：
整章一次调用完成扫描与执行，输出差量条目——模型只回传「P段号：改写后的整段」，
没有病灶的段落一字不提，由 apply_diff 程序确定性拼回初稿、不经模型。
按次计费渠道只花一次调用的钱，保留段零误伤。

删除条目（2026-11-07 老板拍板）：模型可以输出「P段号：删除」（可带括号原因）
要求整段删除，apply_diff 执行真删除；括号占位标记（【此段落已按指令删除】）
不算删除指令、照旧丢弃（meta_dropped），协议要求用删除条目表达删除意图。
"""
from .pipeline import Pipeline, number_paragraphs, split_paragraphs
from .task_templates import render as render_task


def apply_diff(draft: str, replacements: dict[int, str],
               deletions: list[int] | tuple = ()) -> dict:
    """把差量条目执行进初稿（替换 + 删除）→ 结果字典。

    返回 {"text": 成稿, "applied": 已替换段号, "skipped": 越界替换段号,
    "deleted": 已删除段号, "del_skipped": 越界删除段号, "del_rejected": 防跑偏
    拦下的删除条数（0=未拦）}。替换与删除都是程序确定性操作：没有条目的段落
    一字不动地原样保留（不经模型），「照抄不伤」由实现保证而非提示词约束。
    重复段号已在解析层取后值；替换与删除同段号互相覆盖（解析层保证互斥）。

    防跑偏护栏（2026-11-07）：删除条目超过全章段数三分之一且超过 3 段时，
    视为模型跑偏 → **全部删除条目不成立、原段全保留**，条数记进 del_rejected
    （正常删除是零星几段，合法删除不可能命中护栏）。全章段全被删 → 成稿为空，
    由调用方按「结果为空保留原稿」兜底。
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
    deleted: list[int] = []
    del_skipped: list[int] = []
    del_rejected = 0
    dels: list[int] = []
    for no in deletions:
        if 1 <= no <= len(paras):
            dels.append(no)
        else:
            del_skipped.append(no)
    if len(dels) > max(3, len(paras) // 3):
        del_rejected = len(dels)
        dels = []
    for no in dels:
        paras[no - 1] = None
        deleted.append(no)
    text = "\n\n".join(p for p in paras if p is not None)
    return {"text": text, "applied": applied, "skipped": skipped,
            "deleted": deleted, "del_skipped": del_skipped,
            "del_rejected": del_rejected}


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
    删除条目（2026-11-07）：「P段号：删除」由 apply_diff 执行真删除；
    防跑偏护栏把超限删除整体否决（del_rejected>0 = 删除全部不成立）。
    返回 {ok, text(整章成稿), changed(替换段号), skipped(越界替换段号),
    deleted(已删除段号), del_skipped(越界删除段号), del_rejected(被否决删除条数),
    none_declared(模型声明全章无病灶), meta_dropped(模型只回「已删除」类标记、
    被丢弃的段号——原段保留，绝不写进正文), error, stopped}。
    """
    _empty = {"text": "", "changed": [], "skipped": [], "none_declared": False,
              "meta_dropped": [], "deleted": [], "del_skipped": [],
              "del_rejected": 0}
    if stop_event is not None and stop_event.is_set():
        return {"ok": False, "stopped": True, "error": "已停止", **_empty}
    ctx, task = build_whole(store, chapter, draft)
    r = runner("prose_rewrite", ctx, task)
    if r.get("status") == "stopped":
        return {"ok": False, "stopped": True, "error": r.get("error") or "已停止",
                **_empty}
    if not r.get("ok"):
        return {"ok": False, "stopped": False, "error": r.get("error") or "改写调用失败",
                **_empty}
    text = (r.get("text") or "").strip()
    if not text:
        return {"ok": False, "stopped": False, "error": "改写输出为空", **_empty}
    info: dict = {}
    repl = Pipeline.parse_style_rewrite_diff(text, info)
    dels = list(info.get("deletions") or [])
    if not repl and not dels and not info.get("none_declared") and info.get("noise_lines"):
        # 一条条目（含删除条目）都没认出来，还净是噪声行 → 大概率没按格式输出（如重抄了全文）
        return {"ok": False, "stopped": False,
                "error": "未识别到任何替换条目（P段号：改写后的段落），输出可能不符合格式",
                **_empty}
    res = apply_diff(draft, repl, dels)
    return {"ok": True, "stopped": False, "error": "", "text": res["text"],
            "changed": res["applied"], "skipped": res["skipped"],
            "deleted": res["deleted"], "del_skipped": res["del_skipped"],
            "del_rejected": res["del_rejected"],
            "none_declared": bool(info.get("none_declared")),
            "meta_dropped": list(info.get("meta_dropped") or [])}
