# -*- coding: utf-8 -*-
"""文风改写编排（流水线第 6 环）：整章「去 AI 味」句子层改写的共用内核。

双端共用（桌面写作台 / 自动模式 / 手机内核）：提示词组装、调用编排、批次落库
全部在本模块，UI 层只做交互（弹窗、线程、预览落库）。

两种执行模式（共用同一套扫描口径，见 task_templates.STYLE_REWRITE_PREAMBLE）：
- A 整章（whole）：一次调用完成扫描与执行，适合病灶零散的常规章；
- B 分批（batch）：一次全章扫描 + 逐批执行，适合病灶密集/长章/分批初稿。

批次持久化在 rewrite_batches（中断续跑：重跑幂等，跳过已完成批）；
扫描报告按正文指纹存取（初稿变了自动作废重扫）。
"""
from .pipeline import (Pipeline, batch_tail_two_paras, number_paragraphs,
                       numbered_range, parse_batch_plan,
                       plan_rewrite_batches_fallback, split_paragraphs)
from .task_templates import REWRITE_FINAL_NOTE, render as render_task

# 智能模式的程序侧判据：章长超过该字数才建议分批。协议的完整判据是「病灶密度」，
# 但密度只有 AI 扫描才知道——程序只按「超长章」这一条近似，弹窗里用户可手动改。
# 阈值 12000（2026-10-06 整章模式改差量替换后提到此值）：常见章 7000~10000 字
# 一次调用即可（差量输出只有病灶段，无截断风险）；且整章模式按次计费渠道只花
# 一次调用的钱，分批多调用反而更贵。
SMART_BATCH_CHARS = 12000


def heuristic_mode(store, chapter: dict) -> tuple[str, str]:
    """智能模式 → (模式, 人话理由)。mode ∈ {"whole", "batch"}。

    判据（整章模式已改差量替换：只回传病灶段，输出短、一次调用）：
    - 章长 ≥ SMART_BATCH_CHARS → 分批（超长章一次输出的上下文遵从度下降）；
    - 其余（含分批生成的初稿——接缝段在差量模式下只是普通段）→ 整章。
    """
    full = (chapter.get("content") or "").strip()
    if len(full) >= SMART_BATCH_CHARS:
        return "batch", f"本章约 {len(full)} 字（超长章），分批改写更稳"
    return "whole", f"本章约 {len(full)} 字，整章一次改完（只回传病灶段，其余原样保留）"


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


def build_base_ctx(chapter: dict) -> dict:
    """三种调用共用的 context 骨架（人物名单供状态卡分级注入）。"""
    return {"chapter": chapter,
            "character_names": chapter.get("characters") or []}


def build_whole(store, chapter: dict, draft: str) -> tuple[dict, str]:
    """A 整章模式：一次调用（扫描+执行）→ (context, task)。

    ctx["rw_mode"] 供自动模式 _run 区分质量闸：整章输出是纯正文，照常过
    「末尾标点 + 完成标志」两道闸；扫描/执行批输出结构化尾部，必须豁免。
    """
    ctx = build_base_ctx(chapter)
    ctx["rewrite_text"] = number_paragraphs(draft)
    ctx["rw_mode"] = "whole"
    task = render_task("prose_rewrite_whole", store, **_chapter_task_args(chapter))
    return ctx, task


def build_scan(store, chapter: dict, draft: str) -> tuple[dict, str]:
    """B 分批·调用一（全章扫描，只出报告不改文）→ (context, task)。

    扫描输出末尾是「分批方案：批1｜第1~8段 / 总批数：N」结构化行，
    非收尾标点 → 自动模式两道完整性闸都会误判，rw_mode 供其豁免
    （报告完整性由 parse_batch_plan 结构校验兜底）。
    """
    ctx = build_base_ctx(chapter)
    ctx["rewrite_text"] = number_paragraphs(draft)
    ctx["rw_mode"] = "scan"
    task = render_task("prose_rewrite_scan", store, **_chapter_task_args(chapter))
    return ctx, task


def build_batch(store, chapter: dict, draft: str, batches: list[tuple[int, int]],
                batch_no: int) -> tuple[dict, str]:
    """B 分批·执行批（batch_no 1-based）→ (context, task)。

    每批输入（协议规定）：本批编号段落 / 扫描报告全文 / 上一批成稿末两段 +
    改写批包 / 细纲块清单与文风由组装器按角色固定注入。末批带汇总收束检查。
    """
    paras = split_paragraphs(draft)
    ps, pe = batches[batch_no - 1]
    scan = store.get_rewrite_scan(chapter["id"]) if chapter.get("id") else None
    prev_tail = ""
    prev_pkg = ""
    if batch_no > 1 and chapter.get("id"):
        rows = store.list_rewrite_batches(chapter["id"])
        if rows:
            prev_tail = batch_tail_two_paras(rows[-1].get("content") or "")
            prev_pkg = store.rewrite_batch_state_package(chapter["id"])
    ctx = build_base_ctx(chapter)
    ctx["rewrite_text"] = numbered_range(paras, ps, pe)
    ctx["rw_mode"] = "batch"
    ctx["rewrite_batch_no"] = batch_no
    ctx["rewrite_batch_total"] = len(batches)
    ctx["scan_report"] = (scan or {}).get("report", "")
    if prev_tail:
        ctx["prev_batch_tail"] = prev_tail
    if prev_pkg:
        ctx["prev_batch_package"] = prev_pkg
    args = _chapter_task_args(chapter)
    args.update(batch_no=batch_no, para_start=ps, para_end=pe,
                final_note=REWRITE_FINAL_NOTE if batch_no == len(batches) else "")
    task = render_task("prose_rewrite_batch", store, **args)
    return ctx, task


def resolve_batches(store, chapter: dict, draft: str) -> tuple[list[tuple[int, int]], str]:
    """批方案两来源：扫描报告解析 → 程序兜底。返回 (batches, 人话说明)。"""
    total = len(split_paragraphs(draft))
    scan = store.get_rewrite_scan(chapter["id"]) if chapter.get("id") else None
    plan = parse_batch_plan((scan or {}).get("report", ""), total_paras=total)
    if plan:
        return plan, f"分批方案来自扫描报告（{len(plan)} 批）"
    fb = plan_rewrite_batches_fallback(draft)
    return fb, "扫描报告未给出可解析的分批方案，已按字数程序切批（每批约 2500~4000 字）"


def run_scan_call(store, chapter: dict, draft: str, runner,
                  stop_event=None) -> tuple[str, str]:
    """执行全章扫描调用并落库 → (报告, err)。

    runner(role, ctx, task) -> pipeline 结果 dict（UI/自动模式各自包装，
    自动模式经 _run 获得拒答重试与质量判定）。
    """
    if stop_event is not None and stop_event.is_set():
        return "", "已停止"
    ctx, task = build_scan(store, chapter, draft)
    r = runner("prose_rewrite", ctx, task)
    if r.get("status") == "stopped":
        return "", "已停止"
    if not r.get("ok"):
        return "", r.get("error") or "扫描调用失败"
    report = (r.get("text") or "").strip()
    if not report:
        return "", "扫描输出为空"
    store.set_rewrite_scan(chapter["id"], report, store.rewrite_fingerprint(draft))
    return report, ""


def run_batch_loop(store, chapter: dict, draft: str, runner,
                   on_progress=None, stop_event=None) -> dict:
    """分批执行循环：确保扫描 → 解析方案 → 逐批执行 → 拼接。

    幂等可续跑：已完成批次在 rewrite_batches，重入时跳过；初稿变了（指纹
    不匹配）旧报告与旧批次自动作废重来。方案与已存批次范围不一致也重来
    （扫描重跑后方案可能变化，拼接错批比重跑更糟）。

    返回 {ok, merged, done, total, stopped, error, note}。
    """
    def _progress(msg: str):
        if on_progress:
            try:
                on_progress(msg)
            except Exception:
                pass

    cid = chapter.get("id")
    if not cid:
        return {"ok": False, "error": "缺少章 id", "done": 0, "total": 0,
                "stopped": False, "merged": "", "note": ""}
    fp = store.rewrite_fingerprint(draft)
    scan = store.get_rewrite_scan(cid)
    rows = store.list_rewrite_batches(cid)
    if scan and scan.get("fingerprint") != fp and rows:
        store.clear_rewrite_batches(cid)
        rows = []
    if not scan or scan.get("fingerprint") != fp:
        _progress("全章扫描中（病灶清单/保留区/热冷分档/配额盘点/分批方案）…")
        report, err = run_scan_call(store, chapter, draft, runner, stop_event)
        if err:
            return {"ok": False, "error": f"全章扫描失败：{err}", "done": 0,
                    "total": 0, "stopped": err == "已停止", "merged": "", "note": ""}
        _progress("扫描完成，报告已保存")
    batches, note = resolve_batches(store, chapter, draft)
    if not batches:
        return {"ok": False, "error": "本章没有可切分的自然段", "done": 0, "total": 0,
                "stopped": False, "merged": "", "note": note}
    total = len(batches)
    rows = store.list_rewrite_batches(cid)
    if rows:
        consistent = len(rows) <= total and all(
            (r.get("para_start"), r.get("para_end")) == batches[i]
            for i, r in enumerate(rows))
        if not consistent:
            store.clear_rewrite_batches(cid)
            rows = []
            note += "（旧批次与本次分批方案不一致，已重来）"
    done = len(rows)
    if done >= total:
        return {"ok": True, "merged": store.rewrite_batch_merged(cid), "done": done,
                "total": total, "stopped": False, "error": "", "note": note}
    for i in range(done + 1, total + 1):
        if stop_event is not None and stop_event.is_set():
            return {"ok": False, "stopped": True, "error": "已停止",
                    "done": i - 1, "total": total, "merged": "", "note": note}
        ps, pe = batches[i - 1]
        _progress(f"第 {i}/{total} 批改写中（第{ps}~{pe}段）…")
        ctx, task = build_batch(store, chapter, draft, batches, i)
        r = runner("prose_rewrite", ctx, task)
        if r.get("status") == "stopped":
            return {"ok": False, "stopped": True, "error": r.get("error") or "已停止",
                    "done": i - 1, "total": total, "merged": "", "note": note}
        if not r.get("ok"):
            return {"ok": False, "error": f"第 {i}/{total} 批失败：{r.get('error') or '生成失败'}",
                    "done": i - 1, "total": total, "merged": "", "note": note}
        info: dict = {}
        prose, pkg, err = Pipeline.parse_rewrite_batch(r.get("text") or "", info)
        if err or not prose.strip():
            return {"ok": False, "error": f"第 {i}/{total} 批解析失败：{err or '成稿为空'}",
                    "done": i - 1, "total": total, "merged": "", "note": note}
        store.add_rewrite_batch(cid, i, ps, pe, prose, pkg)
        _progress(f"第 {i}/{total} 批完成（{len(prose)} 字）")
    return {"ok": True, "merged": store.rewrite_batch_merged(cid), "done": total,
            "total": total, "stopped": False, "error": "", "note": note}


def run_whole_call(store, chapter: dict, draft: str, runner,
                   stop_event=None) -> dict:
    """A 整章调用（自动模式用；桌面手动走 run_generation + build_whole）。

    差量替换（2026-10-06）：模型只回传「P段号：改写后的整段」条目，这里
    解析后由 apply_diff 替换进初稿——没有条目的段落原样保留（不经模型）。
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
