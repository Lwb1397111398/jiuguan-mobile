# -*- coding: utf-8 -*-
"""迭代增量化（2026-11-05「AI 判断保留」）。

迭代类任务（总纲迭代 / 切章迭代）的增量输出协议与解析：AI 先输出
【迭代评估】清单（每章/卷一行「保留/修改 + 一句理由」），再输出
【修订内容】（只写评估为「修改」的段）。程序靠评估清单区分「AI 判断保留」
与「漏输出」，按章号/章名把修订段匹配回现有条目，未提及部分逐字不动。

模型没按协议输出（无评估标记）→ parse_iter_review 返回 None，调用方降级
走全量解析路径（现行逻辑），功能不坏。
"""

import re

from .pipeline import (Pipeline, _chapter_no_of, _clean_chapter_title,
                       _cn_to_int, _strip_bold)
from .recall import norm as _recall_norm

# 两个段标记行：容忍前导装饰（#>*、空白）与冒号变体，但必须独占一行
_ITER_REVIEW_MARK_RE = re.compile(
    r"^[ \t]*[#>*\s]*【\s*迭代评估\s*】\s*[：:]?\s*$", re.M)
_ITER_BODY_MARK_RE = re.compile(
    r"^[ \t]*[#>*\s]*【\s*修订内容\s*】\s*[：:]?\s*$", re.M)
# 兜底：评估清单区结束于下一个 Markdown 标题行（模型漏写【修订内容】标记时）
_MD_ANY_TITLE_LINE_RE = re.compile(r"^[ \t]*#{1,6}\s+\S", re.M)

# 评估清单行的 key 部分：整体层 / 分卷N / 第N卷 / 第N章（容忍列表符与装饰）
_ITER_LINE_KEY_RE = re.compile(
    r"^[ \t]*[-*•·\s]*(整体层"
    r"|分卷\s*([0-9〇一二三四五六七八九十百千]+)"
    r"|第\s*([0-9〇一二三四五六七八九十百千]+)\s*卷"
    r"|第\s*([0-9〇一二三四五六七八九十百千]+)\s*章)"
    r"\s*[：:、.\-—]?\s*(.*)$")

# 动作词：按「在行内最早出现」判定，防理由里的反向词干扰（如「修改（保留原钩子）」）
_ITER_KEEP_WORDS = ("保留", "保持", "维持", "不动", "不变",
                    "无需修订", "无需修改", "无需调整", "keep")
_ITER_UPDATE_WORDS = ("修改", "重写", "修订", "调整", "更新",
                      "rewrite", "update", "revise")

# 「全部保留」时修订内容段的合法占位词
_ITER_NOOP_WORDS = {"无需修订", "无需修改", "无需调整", "无", "无修改", "无改动", "none"}


def _iter_action_of(rest: str) -> tuple[str | None, str]:
    """从评估行剩余文本判定动作（keep/update）与理由。

    动作词按「行内最早出现」判定：理由里的反向词不会翻盘
    （「修改（保留原钩子）」→ update；「保留（…但中段需修改…）」→ keep）。
    认不出动作词 → (None, "")。
    """
    rest = (rest or "").strip()
    if not rest:
        return None, ""
    best_pos, action, word = None, None, ""
    for w in _ITER_KEEP_WORDS + _ITER_UPDATE_WORDS:
        p = rest.find(w)
        if p >= 0 and (best_pos is None or p < best_pos):
            best_pos, action, word = p, None, w
    if best_pos is None:
        return None, ""
    for w in _ITER_KEEP_WORDS:
        if rest.find(w) == best_pos:
            action = "keep"
    if action is None:
        action = "update"
    reason = (rest[:best_pos] + " " + rest[best_pos + len(word):]).strip()
    reason = reason.strip("（()）：:、，,·-— ")
    return action, reason


def is_noop_body(body: str) -> bool:
    """【修订内容】段是否为「无需修订」类占位/空段（AI 评估全部保留的合法输出）。"""
    s = (body or "").strip()
    return (not s) or s.strip("*# ：:、") in _ITER_NOOP_WORDS


def parse_iter_review(text: str) -> dict | None:
    """解析迭代增量化输出（【迭代评估】清单 + 【修订内容】段）→ {items, body}。

    items 每项 {key, kind, no, action, reason}：kind ∈ overview/volume/chapter，
    action ∈ keep/update；reason = 剥动作词后的剩余文本。
    body = 【修订内容】标记之后到文本尾；模型漏写该标记时，评估区结束于第一个
    Markdown 标题行、其后为 body（宽松兜底）；再没有 → body=""。
    找不到【迭代评估】标记 → None（调用方降级走全量解析路径，功能不坏）。
    """
    text = (text or "").strip()
    if not text:
        return None
    m = _ITER_REVIEW_MARK_RE.search(text)
    if not m:
        return None
    after = text[m.end():]
    items: list[dict] = []
    body = ""
    mb = _ITER_BODY_MARK_RE.search(after)
    if mb:
        review_zone = after[:mb.start()]
        body = after[mb.end():].strip()
    else:
        title_m = _MD_ANY_TITLE_LINE_RE.search(after)
        if title_m:
            review_zone = after[:title_m.start()]
            body = after[title_m.start():].strip()
        else:
            review_zone = after
    for line in review_zone.splitlines():
        lm = _ITER_LINE_KEY_RE.match(line)
        if not lm:
            continue
        key_full = lm.group(1)
        rest = lm.group(5) or ""
        action, reason = _iter_action_of(rest)
        if action is None:
            continue
        if key_full.startswith("整体层"):
            kind, no = "overview", None
        elif key_full.startswith("分卷") or "卷" in key_full:
            kind, no = ("volume", _cn_to_int(lm.group(2) or lm.group(3)))
        else:
            kind, no = ("chapter", _cn_to_int(lm.group(4)))
        items.append({"key": key_full.strip(), "kind": kind, "no": no,
                      "action": action, "reason": reason})
    if not items:
        return None
    return {"items": items, "body": body}


def match_iter_chapters(parsed: list[dict], own: list[dict]) -> tuple[list[dict], list[dict]]:
    """把增量化切章迭代输出的改动章匹配回本卷现有章节。

    匹配序（与 manual_import._match_all 同口径：章号优先、章名兜底）：
    ① 标题章号 _chapter_no_of 有效（1..len(own)）且未被占用 → 按卷内序号对号；
    ② 清洗+归一化章名唯一命中（recall.norm：全角/空白/标点差异不敏感）。
    返回 (matched, unmatched)：matched 每项 {**章, "own_idx": 下标, "matched_by"}；
    unmatched 是解析出但定位不到的章——调用方在确认框标黄、默认不勾，不替用户决定。
    """
    matched: list[dict] = []
    unmatched: list[dict] = []
    used: set[int] = set()
    by_title: dict[str, int] = {}
    for i, ch in enumerate(own):
        key = _recall_norm(_clean_chapter_title(_strip_bold(ch.get("title") or "")))
        if key and key not in by_title:
            by_title[key] = i
    for ch in parsed:
        idx, how = None, ""
        no = _chapter_no_of(ch.get("title") or "")
        if no is not None and 1 <= no <= len(own) and (no - 1) not in used:
            idx, how = no - 1, "no"
        else:
            t = _clean_chapter_title(_strip_bold(ch.get("title") or ""))
            key = _recall_norm(t)
            if key:
                cand = by_title.get(key)
                if cand is not None and cand not in used:
                    idx, how = cand, "title"
        if idx is None:
            unmatched.append(ch)
            continue
        used.add(idx)
        matched.append({**ch, "own_idx": idx, "matched_by": how})
    matched.sort(key=lambda d: d["own_idx"])
    return matched, unmatched


def replace_synopsis_overview(content: str, body: str) -> str | None:
    """总纲原文里就地替换『## 整体层』段为 body，其余文字逐字不动。

    段边界到下一个任意级 Markdown 标题（整体层下通常直接跟 ### 分卷）。
    找不到整体层标题 / body 为空 → None，调用方整体拒绝该候选——
    与 replace_synopsis_volume 同一纪律：宁可不写，不能写错位置。
    """
    if not (body or "").strip():
        return None
    target = None
    for m in re.finditer(r"^[ \t]*##(?!#)\s+(.*)$", content, re.M):
        if m.group(1).strip().replace(" ", "").startswith("整体层"):
            target = m
            break
    if target is None:
        return None
    nxt = next((m for m in _MD_ANY_TITLE_LINE_RE.finditer(content)
                if m.start() > target.start()), None)
    end = nxt.start() if nxt else len(content)
    seg = content[target.start():end]
    ws = seg[len(seg.rstrip("\n")):]  # 段尾原有空行分隔，保留给下一段
    head = f"## {target.group(1).strip()}"
    return (content[:target.start()] + head + "\n" + body.strip()
            + (ws or "\n\n") + content[end:])


def apply_synopsis_iter_patch(base: str, review: dict) -> tuple[str | None, str]:
    """把总纲迭代的「评估+修订内容」候选合成进底稿：未提及的卷/整体层逐字不动。

    返回 (new_content, note)：
    - "noop"：AI 评估全部保留（修订内容只是「无需修订」类占位词）→ 返回底稿原样；
    - "ok..."：合成成功，note 带替换明细；含交叉校验——评估标「修改」但修订
      内容缺段时在 note 里点名（不阻塞，确认框提示）。
    - (None, 说明)：某段在底稿中找不到原位（如 ■/裸行形态总纲）→ 整批拒绝该候选。
      修订内容只是片段，绝不能当全文整存（会把未修订的卷全部弄丢）。
    """
    base = (base or "").strip()
    body = (review.get("body") or "").strip()
    upd = [it for it in review.get("items") or [] if it.get("action") == "update"]
    stripped = body.strip("*# ：:、")
    if not body or stripped in _ITER_NOOP_WORDS:
        return base, "noop"
    rev = Pipeline.parse_synopsis_volumes(body)
    rev_overview = (rev.get("overview") or "").strip()
    rev_volumes = rev.get("volumes") or []
    if not rev_overview and not rev_volumes:
        return None, "修订内容里没有识别到『## 整体层』或『### 分卷N』段落"
    new = base
    replaced: list[str] = []
    if rev_overview:
        patched = replace_synopsis_overview(new, rev_overview)
        if patched is None:
            return None, "底稿中找不到『## 整体层』标题，无法原位替换"
        new = patched
        replaced.append("整体层")
    base_titles = {v.get("index"): (v.get("title") or "")
                   for v in base_parsed_volumes(base)}
    for v in rev_volumes:
        idx = v.get("index") or 0
        vtitle = (v.get("title") or "").strip() or base_titles.get(idx, "")
        patched = Pipeline.replace_synopsis_volume(new, idx, vtitle, v.get("content") or "")
        if patched is None:
            return None, f"底稿中找不到『分卷{idx}』的 Markdown 标题，无法原位替换"
        new = patched
        replaced.append(f"分卷{idx}")
    # 交叉校验：评估标「修改」但修订内容缺段 → note 点名（不阻塞）
    missed = []
    for it in upd:
        if it["kind"] == "volume" and it.get("no") and not any(
                v.get("index") == it["no"] for v in rev_volumes):
            missed.append(f"分卷{it['no']}")
        elif it["kind"] == "overview" and not rev_overview:
            missed.append("整体层")
    note = "ok:已替换 " + "、".join(replaced)
    if missed:
        note += f"（评估标修改但未输出内容：{'、'.join(missed)}）"
    return new, note


def base_parsed_volumes(base: str) -> list[dict]:
    """底稿总纲的分卷清单（卷名回填用）：解析失败安全返回空表。"""
    try:
        return Pipeline.parse_synopsis_volumes(base).get("volumes") or []
    except Exception:
        return []
