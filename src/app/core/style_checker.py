# -*- coding: utf-8 -*-
"""文风审校：程序级重复检测（n-gram/整句/套话表/可选语义相似）。

设计：候选召回 + LLM 裁决。本模块只负责"找出疑似"（本地零成本），
有意/无意判定交给 LLM（style 角色）与用户审核。
向量层（嵌入 API）为可选增强：未配置或调用失败时静默跳过，不阻断审校。
"""
import json
import math
import re

from .embedding import embed_texts, cosine_similarity, test_embedding, list_embedding_models
from .store import ProjectStore

# 内置套话表（设置键 style_cliches 可覆盖，换行分隔；空 = 用本表）
CLICHES = [
    "惨白的灯光", "昏黄的灯光", "惨白的月光",
    "嘴角勾起一抹弧度", "眼底闪过一丝", "眼中闪过一丝",
    "空气仿佛凝固", "时间仿佛停止",
    "心头一紧", "呼吸一滞", "瞳孔骤缩", "背脊发凉",
    "手指微微颤抖", "不自觉地攥紧", "心中泛起涟漪",
    "不知为何", "说不清道不明", "一丝不易察觉",
]

_END_RE = re.compile(r"[。！？；!?;]")
_NONCHAR_RE = re.compile(r"[\s，。！？；：、,.!?;:（）()\"'“”‘’\-—…]")
_EMBEDDING_USER_AGENT = "JiuGuanNovelGenerator/1.0"

def _split_sentences(text: str) -> list[str]:
    """按句末标点切句（保留原文）。"""
    out, cur = [], []
    for ch in text:
        cur.append(ch)
        if _END_RE.match(ch):
            s = "".join(cur).strip()
            if s:
                out.append(s)
            cur = []
    s = "".join(cur).strip()
    if s:
        out.append(s)
    return out

def _norm(s: str) -> str:
    return _NONCHAR_RE.sub("", s)

def _ngrams(s: str, lo: int = 2, hi: int = 6) -> list[str]:
    s = _norm(s)
    n = len(s)
    out = []
    for k in range(lo, min(hi, n) + 1):
        for i in range(n - k + 1):
            out.append(s[i:i + k])
    return out

def _count_ngrams(text: str, lo: int = 2, hi: int = 6) -> dict[str, int]:
    cnt: dict[str, int] = {}
    for s in _split_sentences(text):
        for g in _ngrams(s, lo, hi):
            cnt[g] = cnt.get(g, 0) + 1
    return cnt

def recent_chapter_texts(store: ProjectStore, chapter_id: int, count: int = 5) -> list[str]:
    """本章之前最近 count 章的已接受正文（程序检测与 LLM 裁决共用）。"""
    chapters = store.list_chapters()
    cur = next((c for c in chapters if c["id"] == chapter_id), None)
    if cur is None:
        return []
    prior = [c for c in chapters if c["order_index"] < cur["order_index"]][-count:]
    out = []
    for c in prior:
        blocks = store.list_text_blocks(c["id"])
        accepted = [b for b in blocks if b["status"] in ("已接受", "已锁定")]
        text = "\n".join(b["content"] for b in accepted)
        if text.strip():
            out.append(text)
    return out

def high_frequency_phrases(recent_texts: list[str], min_count: int = 3) -> list[str]:
    """近章高频短语（正文生成前防复发提醒）。"""
    cnt: dict[str, int] = {}
    for rt in recent_texts:
        for g, c in _count_ngrams(rt).items():
            cnt[g] = cnt.get(g, 0) + c
    return [g for g, c in sorted(cnt.items(), key=lambda x: -x[1])
            if 4 <= len(g) <= 8 and c >= min_count][:5]

def analyze(chapter_text: str, recent_texts: list[str],
            cliches: list[str] | None = None,
            embed: dict | None = None) -> list[dict]:
    """程序检测入口：返回候选清单（top 30），供 LLM 裁决。

    embed: {"base_url", "api_key", "model"} 或 None（禁用语义层）。
    """
    cliches = cliches or CLICHES
    recent_ng: dict[str, int] = {}
    for rt in recent_texts:
        for g, c in _count_ngrams(rt).items():
            recent_ng[g] = recent_ng.get(g, 0) + c
    cur_ng = _count_ngrams(chapter_text)
    recent_sents: dict[str, int] = {}
    for rt in recent_texts:
        for s in _split_sentences(rt):
            ns = _norm(s)
            if ns:
                recent_sents[ns] = recent_sents.get(ns, 0) + 1

    cands: list[dict] = []
    # 1) 本章高频短语（长度 4~8、出现 ≥2 次）
    for g, c in cur_ng.items():
        if 4 <= len(g) <= 8 and c >= 2:
            cands.append({"kind": "ngram", "phrase": g, "chapter_count": c,
                          "recent_count": recent_ng.get(g, 0), "sample": ""})
    # 2) 整句重复（规范化后：本章 ≥2 次或近章出现过）
    seen: dict[str, int] = {}
    cur_sents = []
    for s in _split_sentences(chapter_text):
        ns = _norm(s)
        if ns:
            seen[ns] = seen.get(ns, 0) + 1
            cur_sents.append((s, ns))
    for s, ns in cur_sents:
        if len(ns) >= 8 and (seen[ns] >= 2 or recent_sents.get(ns, 0) > 0):
            cands.append({"kind": "sentence", "phrase": s, "chapter_count": seen[ns],
                          "recent_count": recent_sents.get(ns, 0), "sample": ""})
    # 3) 套话表命中
    for cl in cliches:
        c = chapter_text.count(cl)
        if c > 0:
            cands.append({"kind": "cliche", "phrase": cl, "chapter_count": c,
                          "recent_count": sum(rt.count(cl) for rt in recent_texts),
                          "sample": ""})
    # 4) 语义相似（可选）
    if embed and embed.get("base_url") and embed.get("api_key") and embed.get("model"):
        cands.extend(_semantic_cands(chapter_text, recent_texts, embed))

    # 去重 + 排序 + 截断
    merged = {}
    for c in cands:
        merged[(c["kind"], c["phrase"])] = c
    return sorted(merged.values(), key=lambda x: x["chapter_count"] + x["recent_count"],
                  reverse=True)[:30]

def _semantic_cands(chapter_text: str, recent_texts: list[str],
                    embed: dict) -> list[dict]:
    """语义相似召回：本章句子 vs 近章句子，余弦 > 0.85。任何失败返回 []。"""
    try:
        cur = [_norm(s) for s in _split_sentences(chapter_text) if len(_norm(s)) >= 8][:100]
        rec = []
        for rt in recent_texts:
            rec.extend(_norm(s) for s in _split_sentences(rt) if len(_norm(s)) >= 8)
        rec = rec[:500]
        if not cur or not rec:
            return []
        vecs = embed_texts(embed["base_url"], embed["api_key"], embed["model"], cur + rec)
        if vecs is None or len(vecs) != len(cur) + len(rec):
            return []
        cv, rv = vecs[:len(cur)], vecs[len(cur):]
        out = []
        for i, c in enumerate(cur):
            best, best_j = 0.0, -1
            for j, r in enumerate(rec):
                sim = cosine_similarity(cv[i], rv[j])
                if sim > best:
                    best, best_j = sim, j
            if best >= 0.85 and best_j >= 0:
                out.append({"kind": "semantic", "phrase": f"{c} ≈ {rec[best_j]}",
                            "chapter_count": 1, "recent_count": 1,
                            "sample": f"相似度 {best:.2f}"})
        return out
    except Exception:
        return []
