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

2026-11-11 删除优先（老板拍板）：删冗余形容/压短长句/删废句属于**句内删**，
直接写进替换条目（该段删短后的完整段落），不需要任何新协议格式、也不受下面的
整段删除护栏限制；整段删除的合法类别扩到四类（含「整段都不适合」的废段），
含情节事实/对白信息/人物决定/状态落点的段只许句内删。护栏阈值一行未动。

2026-11-11 晚补两道「只算不拦」的体检：apply_diff 返回 cut_ratio（成稿/原稿
字数比）与 dialogue_hint（被替换段里台词量骤降的提示）。阈值与拦不拦由调用方
按入口决定：批量改写/自动模式静默落库 → 低于 rewrite_min_keep_ratio 不落库；
写作台/手机版有人工预览 → 只提示。core 不读全局设置、不改 text、不改差量协议。

2026-11-13 分段 / 合段（老板拍板）：偏好层第十三条病灶「段落切分」此前**只能
识别、无法执行**——协议一条只认一个段号，解析层还把条目内空行主动吞掉
（`if not s: continue`），模型按病灶拆的段落地时又被压回一段。现在分两路：
分段仍写在替换条目里（正文用空行，程序按空行还原成多段）；合段条目头写
「P3-P5：」，首段放正文、中间段并行消失。两者都是**程序确定性执行**，
段号一律以编号稿原段号为准（拆/合产生的新段不重新编号，否则后续条目整体错位）。
合段消耗段**不记进 deleted**（那是「整段删除」的语义出口，混进来会误触删除护栏、
也会让用户以为模型在删段），另设四道护栏 + 一道压缩率保险。
"""
import re

from .pipeline import (MAX_MERGE_SPAN, Pipeline, number_paragraphs,
                       split_paragraphs)
from .task_templates import render as render_task

# 合段护栏（2026-11-13 分段/合段）：跨度 MAX_MERGE_SPAN 与解析层同源（pipeline 导入）。
MERGE_TOTAL_DIVISOR = 4   # 合段消耗段总数上限 = max(4, 全章段数 // 4)
MERGE_MIN_KEEP = 0.7      # 合并后正文不得少于被合并区间原文的七成（防借合段吞信息）


# 成对引号里的字符视为台词。只认中文小说常用的三对引号与英文双引号，单引号
# 不认（免得把英文缩写、所有格当对白）。用于「原段有对白、改后台词明显变少」
# 的提示，只提示不拦截——【对白令】允许把长台词拆成短句、停顿、被打断，
# 引号本来就会自然变少，硬拦会跟刚放开的口语化重写对冲。
_QUOTE_PAIRS = (("「", "」"), ("『", "』"), ("“", "”"))
_QUOTE_DQ_RE = re.compile(r'"([^"]{1,})"', re.S)


def _dialogue_chars(text: str) -> int:
    """粗算一段文字里成对引号内的字符数（台词量）。"""
    total = 0
    for op, cl in _QUOTE_PAIRS:
        pat = re.escape(op) + r"(.*?)" + re.escape(cl)
        total += sum(len(m.group(1)) for m in re.finditer(pat, text, re.S))
    total += sum(len(m.group(1)) for m in _QUOTE_DQ_RE.finditer(text))
    return total


def _dialogue_loss_note(pairs: dict[int, tuple[int, int]]) -> str:
    """被替换段里台词量骤降的段号 → 一句提示；没有就返回空串。

    门槛刻意放宽：原段台词至少 12 字、改后不足一半才提，且只比对被替换的段
    （不比整篇），避免「合法整段删除对白段」与「长台词拆成短句」两类误报。
    """
    hurt = sorted(no for no, (before, after) in pairs.items()
                  if before >= 12 and after < before * 0.5)
    if not hurt:
        return ""
    shown = "、".join(str(n) for n in hurt[:5])
    more = f" 等 {len(hurt)} 段" if len(hurt) > 5 else ""
    return (f"第 {shown} 段{more}的对白内容明显变少，"
            "请核对台词的立场与信息有没有被删掉")


def thin_rewrite(draft: str, text: str, min_keep_ratio: float) -> tuple[bool, float]:
    """净删护栏判定（纯函数，不读设置）：返回 (是否删过头, 成稿/原稿字数比)。

    min_keep_ratio <= 0 表示关闭护栏；原稿为空或成稿为空时交给调用方原有的
    「结果为空保留原稿」兜底，这里不重复判。
    """
    base_len = len(draft.strip())
    ratio = (len(text) / base_len) if base_len else 1.0
    return (min_keep_ratio > 0 and ratio < min_keep_ratio), ratio


def apply_diff(draft: str, replacements: dict[int, str],
               deletions: list[int] | tuple = (),
               merges: "dict[int, int] | list[tuple[int, int]] | None" = None) -> dict:
    """把差量条目执行进初稿（替换 + 删除 + 分段 + 合段）→ 结果字典。

    返回 {"text": 成稿, "applied": 已替换段号, "skipped": 越界替换段号,
    "deleted": 已删除段号, "del_skipped": 越界删除段号, "del_rejected": 防跑偏
    拦下的删除条数（0=未拦）, "cut_ratio": 成稿/原稿字数比,
    "dialogue_hint": 台词量骤降提示（空串=无）,
    "merged": 已执行的合段 [[起, 止], ...], "merge_rejected": 被否决的合段条数}。
    "merge_out": 合段结果在**成稿**里的段号区间 [[起,止],...]，
    "split_in"/"split_out": 拆段的原段号 [[段号, 拆成几段]] 与成稿段号区间
    （2026-11-13，对比窗精确标分段/合段用：协议段号是原稿的，成稿因合段/删除/拆段
    整体前移，UI 自己推必然错位，必须用本函数顺手算出来的映射）。
    后几项都是**只算不拦**的体检：护栏阈值由调用方读全局设置后调 thin_rewrite()
    判定，本函数不改 text。替换与删除都是程序确定性操作：没有条目的段落
    一字不动地原样保留（不经模型），「照抄不伤」由实现保证而非提示词约束。
    重复段号已在解析层取后值；替换与删除同段号互相覆盖（解析层保证互斥）。

    **分段（2026-11-13）**：替换条目正文里带空行，直接落成多段——
    `paras[no-1] = "前一段\\n\\n后一段"`，拼回时天然就是两个自然段。
    分段不丢信息，因此不设上限、不拦。

    **合段（2026-11-13）**：`merges` 是 {起段号: 止段号}。首段放合并后的正文，
    中间各段置 None（**并行消失，但绝不记进 deleted**——那是「整段删除」的语义出口，
    UI/批量/自动三处都按它算「删了几段」，混进来会误触删除护栏、也会让老板以为
    是模型在删段）。`merges` 为 None 时取 `replacements.merges`（RewriteDiff 捎带），
    这样没同步的手机端老调用点也能正确执行合段，不会退化成「只替换首段」。
    四道护栏（越界/倒置、跨度 > MAX_MERGE_SPAN、区间内混有替换或删除条目、
    总量 > max(4, 段数//4)）逐条整条否决、不 clamp、不部分执行；
    另加一道保险：合并后的正文少于区间原文的 MERGE_MIN_KEEP 倍 → 判模型借合段
    吞信息，该条不成立、原段全保留。**被拒合段的起点连带跳过它的替换条目**——
    那段 body 是「几段合并后的整段」，只拿它替换首段会让后几段原样留着、内容重复。

    防跑偏护栏（2026-11-07）：删除条目超过全章段数三分之一且超过 3 段时，
    视为模型跑偏 → **全部删除条目不成立、原段全保留**，条数记进 del_rejected
    （正常删除是零星几段，合法删除不可能命中护栏）。全章段全被删 → 成稿为空，
    由调用方按「结果为空保留原稿」兜底。
    """
    paras = split_paragraphs(draft)
    n = len(paras)
    # 合段区间：显式传入优先，否则读 RewriteDiff 捎带的（向后兼容兜底）
    if merges is None:
        merges = getattr(replacements, "merges", ())
    pairs = list(merges.items()) if isinstance(merges, dict) else list(merges or ())
    pairs = [(int(a), int(b)) for a, b in pairs]

    del_skipped: list[int] = []
    del_rejected = 0
    dels: list[int] = []
    for no in deletions:
        if 1 <= no <= n:
            dels.append(no)
        else:
            del_skipped.append(no)
    if len(dels) > max(3, n // 3):
        del_rejected = len(dels)
        dels = []
    del_set = set(dels)

    applied: list[int] = []
    skipped: list[int] = []
    dialogue_pairs: dict[int, tuple[int, int]] = {}

    # ---- 合段校验：全部先算后改，逐条整条否决（绝不 clamp / 部分执行） ----
    accepted: list[tuple[int, int]] = []
    merge_rejected = 0
    rejected_starts: set[int] = set()   # 被拒合段的起点：连带跳过它的替换条目
    consumed: set[int] = set()
    for s, e in sorted(pairs):
        if not (1 <= s <= n and 1 <= e <= n) or e <= s:
            merge_rejected += 1
            rejected_starts.add(s)
            continue                       # 越界 / 倒置
        if e - s + 1 > MAX_MERGE_SPAN or s not in replacements:
            merge_rejected += 1
            rejected_starts.add(s)
            continue                       # 跨度超限 / 没给合并后的正文
        span = range(s + 1, e + 1)
        if s in consumed or any(
                i in consumed or i in del_set or i in replacements for i in span):
            merge_rejected += 1
            rejected_starts.add(s)
            continue                       # 与替换/删除/别的合段冲突 → 保守丢合段
        src_len = sum(len(paras[i - 1]) for i in range(s, e + 1))
        if src_len and len(replacements[s]) < src_len * MERGE_MIN_KEEP:
            merge_rejected += 1
            rejected_starts.add(s)
            continue                       # 保险：合并后压得太短 = 借合段吞信息
        accepted.append((s, e))
        consumed.update(span)
    if sum(e - s for s, e in accepted) > max(4, n // MERGE_TOTAL_DIVISOR):
        merge_rejected += len(accepted)
        rejected_starts.update(s for s, _ in accepted)
        accepted, consumed = [], set()     # 总量超限：全部合段作废、原段全保留

    # ---- 执行合段：首段放正文、消耗段置 None（不进 deleted） ----
    merged: list[list[int]] = []
    merge_starts = {s for s, _ in accepted}
    for s, e in accepted:
        body = replacements[s]
        # 台词量必须按**区间总量**比对：合 3 段对白时只跟首段比必然误报「骤降」
        dialogue_pairs[s] = (sum(_dialogue_chars(paras[i - 1]) for i in range(s, e + 1)),
                             _dialogue_chars(body))
        paras[s - 1] = body
        for i in range(s + 1, e + 1):
            paras[i - 1] = None
        applied.append(s)
        merged.append([s, e])

    # ---- 普通替换 ----
    for no, body in replacements.items():
        # 合段起点：已放正文；被拒合段的起点：body 是「合并后的整段」，
        # 只拿它替换首段会让后几段原样留着 → 内容重复，故整条不落地、原段全保留。
        if no in merge_starts or no in rejected_starts:
            continue
        if no in consumed:
            skipped.append(no)             # 兜底：上面冲突校验已否决，不会发生
            continue
        if 1 <= no <= n:
            # 只比对被替换的段：合法整段删除不进这里，越界条目也不比。
            dialogue_pairs[no] = (_dialogue_chars(paras[no - 1]),
                                  _dialogue_chars(body))
            paras[no - 1] = body
            applied.append(no)
        else:
            skipped.append(no)

    # ---- 执行删除 ----
    deleted: list[int] = []
    for no in dels:
        paras[no - 1] = None
        deleted.append(no)

    # 一条都没落地 → 原样返回：否则「整篇无空行、按行切」的稿子会被下面的
    # "\n\n".join 重排成空行分段（零改动也动了版式）。
    text = draft if (not applied and not deleted) \
        else "\n\n".join(p for p in paras if p is not None)
    base_len = len(draft.strip())
    cut_ratio = (len(text) / base_len) if base_len else 1.0

    # ---- 成稿段号映射（2026-11-13，对比窗精确标分段/合段）----
    # 协议段号是**原稿**的；成稿因为合段/删除/拆段，段号整体前移。这里按拼接
    # 顺序算出每个原段在成稿里的起始段号，UI 才能把「合段/分段」画到正确的段上。
    # 只算不拦：不影响 text，也不改任何既有返回值。
    out_start: dict[int, int] = {}
    _cnt = 0
    for _i, _p in enumerate(paras, 1):
        if _p is None:
            continue
        out_start[_i] = _cnt + 1
        _cnt += len(split_paragraphs(_p)) or 1
    merge_out: list[list[int]] = []
    for _s, _e in merged:
        if _s in out_start:
            _n = len(split_paragraphs(replacements[_s])) or 1
            merge_out.append([out_start[_s], out_start[_s] + _n - 1])
    split_in: list[list[int]] = []      # [原段号, 拆成几段]
    split_out: list[list[int]] = []     # [成稿起段, 成稿止段]
    for _no, _body in replacements.items():
        if _no in merge_starts or _no in rejected_starts or _no in consumed:
            continue
        _n = len(split_paragraphs(_body))
        if _n > 1 and _no in out_start:
            split_in.append([_no, _n])
            split_out.append([out_start[_no], out_start[_no] + _n - 1])

    return {"text": text, "applied": applied, "skipped": skipped,
            "deleted": deleted, "del_skipped": del_skipped,
            "del_rejected": del_rejected,
            "cut_ratio": cut_ratio,
            "dialogue_hint": _dialogue_loss_note(dialogue_pairs),
            "merged": merged, "merge_rejected": merge_rejected,
            "merge_out": merge_out, "split_in": split_in, "split_out": split_out}


# 审校「让 AI 改」的输出是「修好的那一段」，但落库目标是整块正文
# （整章模式一章只有一条 kind='chapter' 块）。2026-11-11 前是整块替换，
# 只要模型按整章差量终裁回一条「P1：…」，整章就会被换成一两行。
# 这里把「并回哪一段」做成可测的纯函数：按引文定位，只换那一段。
_FIX_P_PREFIX_RE = re.compile(r"^P\d+\s*[：:]\s*")


def merge_fix_into_text(old: str, quote: str, fixed: str) -> str | None:
    """把审校修复结果并回原正文，只替换引文所在的那一段。

    - `fixed` 行首若带「P3：」前缀（模型顺从整章差量协议的惯性）先剥掉；
    - 按 `quote` 在自然段里定位：命中就只换那一段，其余段落逐字保留；
    - 定位不到时：原正文只有一段 → 换它必然是「换那一段」，放行；
      多段 → 返回 None（调用方据此拒绝替换并提示复制），绝不整块覆盖。

    返回并好的新正文；None = 拒绝合并（多段块又定位不到引文）。
    """
    body = _FIX_P_PREFIX_RE.sub("", (fixed or "").strip())
    if not body:
        return None
    paras = split_paragraphs(old or "")
    idx = -1
    if quote:
        for i, p in enumerate(paras):
            if quote in p:
                idx = i
                break
    if idx < 0:
        return body if len(paras) <= 1 else None
    paras[idx] = body
    return "\n\n".join(paras)


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
    cut_ratio(成稿/原稿字数比), dialogue_hint(台词骤降提示，空串=无),
    merged(已执行合段 [[起,止]...]), merge_rejected(被否决合段条数),
    merge_dropped(解析层丢掉的合段条目 [(起,止)]),
    bad_head_dropped(因「P3、P4：」误写被丢弃的段号),
    none_declared(模型声明全章无病灶), meta_dropped(模型只回「已删除」类标记、
    被丢弃的段号——原段保留，绝不写进正文), error, stopped}。
    """
    _empty = {"text": "", "changed": [], "skipped": [], "none_declared": False,
              "meta_dropped": [], "deleted": [], "del_skipped": [],
              "del_rejected": 0, "cut_ratio": 1.0, "dialogue_hint": "",
              "merged": [], "merge_rejected": 0, "merge_dropped": [],
              "bad_head_dropped": []}
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
    res = apply_diff(draft, repl, dels, info.get("merges") or {})
    return {"ok": True, "stopped": False, "error": "", "text": res["text"],
            "changed": res["applied"], "skipped": res["skipped"],
            "deleted": res["deleted"], "del_skipped": res["del_skipped"],
            "del_rejected": res["del_rejected"],
            "cut_ratio": res["cut_ratio"],
            "dialogue_hint": res["dialogue_hint"],
            "merged": res["merged"],
            "merge_rejected": res["merge_rejected"],
            "none_declared": bool(info.get("none_declared")),
            "meta_dropped": list(info.get("meta_dropped") or []),
            "merge_dropped": list(info.get("merge_dropped") or []),
            "bad_head_dropped": list(info.get("bad_head_dropped") or [])}
