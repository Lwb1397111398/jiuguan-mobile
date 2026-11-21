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
                       _cn_to_int, _strip_bold, _ITER_BODY_MARK_RE,
                       _ITER_EVAL_MARK_RE, _SYN_VOL_HEADING_NUM_RE,
                       bare_synopsis_volume_marks)
from .recall import norm as _recall_norm

# 两个段标记行：定义在 pipeline（parse_synopsis_candidates 判断「这是增量协议输出」
# 用同一份），此处复用，避免两处正则漂移。容忍前导装饰（#>*、空白）与冒号变体，
# 但必须独占一行。
_ITER_REVIEW_MARK_RE = _ITER_EVAL_MARK_RE
# 兜底：评估清单区结束于下一个 Markdown 标题行（模型漏写【修订内容】标记时）
_MD_ANY_TITLE_LINE_RE = re.compile(r"^[ \t]*#{1,6}\s+\S", re.M)

# 整体层标题（任意级别）与段内【小节】标题行；整体层段的结束位置取「下一个同级或
# 更高级标题」——老板的总纲常写成 H1『# 整体层』+ H2『## 【小节】』，旧实现只认
# 『## 整体层』且把下一个任意级标题当段尾，会把整段整体层按小节边界切碎。
_OVERVIEW_HEAD_RE = re.compile(r"^[ \t]*(#{1,6})[ \t]*(\S.*)$", re.M)
# 裸行形态（2026-11-10 老板实测：「生成总纲」自定义模板产出的底稿没有任何 # 标记，
# 整体层 / 伏笔台账 / 资产清单 / 分卷纲要 / 分卷N 全是独立裸行，卷名分隔符甚至是
# 中文逗号）。只认 Markdown 时 replace_synopsis_overview 必然找不到 → 整批拒绝，
# 界面表现成「识别出错」，实际是底稿格式不匹配。两套形态都要认。
_BARE_OVERVIEW_HEAD_RE = re.compile(r"^[ \t]*整体层[ \t]*[：:]?[ \t]*$", re.M)
# 裸行总纲的分卷区起点（『分卷纲要』独占一行）：整体层段到这里结束
_BARE_VOL_SECTION_RE = re.compile(
    r"^[ \t]*(?:【[ \t]*分卷纲要[ \t]*】|分卷纲要)[ \t]*[：:]?[ \t]*$", re.M)
_SEC_HEAD_RE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*)?【[ \t]*([^】\n]{1,40}?)[ \t]*】[ \t]*(.*)$", re.M)
# 小节体尾部分隔区（空行 / --- 分隔线）：替换小节内容时原样保留
_SEC_TRAIL_RE = re.compile(r"(?:\n[ \t]*(?:-{3,}|\*{3,})[ \t]*)?[ \t]*\n*$")
# 裸行形态的整体层里还有几个无标记大节（老板模板固定写这三个），按【小节】合并时
# 必须当边界，否则最后一个小节的整节替换会把它们一起吃掉。
_BARE_SUBHEAD_NAME_RE = re.compile(
    r"^[ \t]*(伏笔台账|资产清单|分卷纲要|创意钩子总览|人物关系|时间线|附录)"
    r"[ \t]*[：:]?[ \t]*$", re.M)
# 通用裸行小标题判据（防老板改名后漏判）：独占一行、2~12 字、无标点数字。
_BARE_SUBHEAD_BAD_CHARS = "。！？；，、：:（）()「」『』《》,.!?;:()[]{}\"'“”‘’…—"
_BARE_SUBHEAD_MAX = 12
# 「在某节/某段之后增补如下：…」式修订（指令而非完整小节，见 _split_additive）
_ADDITIVE_HINT_RE = re.compile(
    r"^[^\n。]{0,60}?(?:增补|补充|添加|插入|追加|加上)[^\n。]{0,40}?如下")

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


def _strip_volumes_from_overview(text: str) -> str:
    """整体层段里混进来的分卷段截掉，只留整体层正文。

    parse_synopsis_volumes 按 H2 切整体层、按 H3 切分卷；迭代输出里
    『## 整体层』后面直接跟『### 分卷二』，H3 分卷会被整体层段整段吞进来。
    不截掉就会把分卷文本塞进整体层小节，随后 replace_synopsis_volume 命中的
    第一个『分卷二』恰好是这段错位文本，把中间所有小节/分卷一次吃掉。
    裸行形态（『分卷纲要』+『分卷一，卷名』）同样要截，否则整体层合并会把
    分卷纲要整段当小节内容。
    """
    text = text or ""
    cut = len(text)
    m = _SYN_VOL_HEADING_NUM_RE.search(text)
    if m:
        cut = min(cut, m.start())
    m = _BARE_VOL_SECTION_RE.search(text)
    if m:
        cut = min(cut, m.start())
    return text[:cut].strip()


def _overview_span(content: str) -> tuple[int, int, int] | None:
    """定位整体层段 → (标题行起点, 标题行终点, 段终点)；找不到返回 None。

    Markdown 优先（标题任意级别，段尾取下一个同级或更高级标题）；没有 # 标记时
    退裸行形态（『整体层』独占一行，段尾取第一个裸行卷标题行 / 下一个 Markdown
    标题行）——老板的自定义总纲模板产出的就是这种无标记形态。
    两种显式标题都没有时再退**隐式形态**（2026-11-21《小偷入室》实测）：底稿开头
    就是【世界观与故事概述】这类小节、根本没写『整体层』标题行——识别层的裸行
    切分早就把「开头 → 第一个分卷标题行」当 overview，替换链不认就会「底稿中
    找不到『整体层』段落标题」整批拒绝。隐式段 = 文本开头(0) → 第一个分卷区起点
    （Markdown 分卷标题 / 裸行分卷标题 / 『分卷纲要』分段标记三者取最早）；没有
    分卷区则无法定位 → 维持 None（整批拒绝，绝不把说不清边界的东西当整体层）。
    段内合并仍走 _merge_overview_body 的按小节对齐链，未提及的节逐字保留。
    """
    content = content or ""
    for m in _OVERVIEW_HEAD_RE.finditer(content):
        if m.group(2).strip().replace(" ", "").startswith("整体层"):
            level = len(m.group(1))
            ends = [len(content)]
            # 整体层段不吞分卷区：Markdown 分卷标题行 / 『分卷纲要』分段标记都算
            # 段尾候选（与隐式形态、裸行形态同一纪律）。底稿『## 整体层』后跟
            # 『### 分卷N』而分卷区中间还插着别的 H2 大节时（AI 分批生成实测：
            # 『## 资产清单』插在分卷三后），只认「下一个同级或更高级标题」会把
            # 分卷一~三整段吞进整体层段，随后的按小节合并又把分卷段当节内容顶掉
            # （2026-11-22《暗日机械》实测：分卷一标题+正文被顶掉，逐卷替换报
            # 「找不到分卷1」整批拒绝 → 界面「识别失败」）。
            md_vol = _SYN_VOL_HEADING_NUM_RE.search(content, m.end())
            if md_vol:
                ends.append(md_vol.start())
            sec = _BARE_VOL_SECTION_RE.search(content, m.end())
            if sec:
                ends.append(sec.start())
            nxt = next((x for x in _OVERVIEW_HEAD_RE.finditer(content)
                        if x.start() > m.start() and len(x.group(1)) <= level), None)
            if nxt:
                ends.append(nxt.start())
            return m.start(), m.end(), min(ends)
    m = _BARE_OVERVIEW_HEAD_RE.search(content)
    if m is None:
        # 隐式形态：无『整体层』标题，靠分卷区起点定段尾（见 docstring）
        starts: list[int] = []
        md_vol = _SYN_VOL_HEADING_NUM_RE.search(content)
        if md_vol:
            starts.append(md_vol.start())
        bare_marks = bare_synopsis_volume_marks(content)
        if bare_marks:
            starts.append(bare_marks[0]["line_start"])
        sec = _BARE_VOL_SECTION_RE.search(content)
        if sec:
            starts.append(sec.start())
        if not starts:
            return None
        vol_start = min(starts)
        if vol_start <= 0:
            return None
        return 0, 0, vol_start
    ends = [len(content)]
    mk = next((x for x in (bare_synopsis_volume_marks(content) or [])
               if x["line_start"] > m.start()), None)
    if mk:
        ends.append(mk["line_start"])
    md = next((x for x in _MD_ANY_TITLE_LINE_RE.finditer(content)
               if x.start() > m.start()), None)
    if md:
        ends.append(md.start())
    return m.start(), m.end(), min(ends)


def replace_synopsis_overview(content: str, body: str,
                              notes: list | None = None) -> str | None:
    """总纲原文里就地替换『整体层』段为 body，其余文字逐字不动。

    - 标题级别不限（#~######），段边界取「下一个同级或更高级标题」：老板的总纲
      常写成 H1『# 整体层』+ H2『## 【小节】』，旧实现只认『## 整体层』、又以
      任意级标题为段尾，于是既找不到标题、又会按子标题把整段切碎。
    - 无任何 # 标记的裸行形态同样支持（见 _overview_span）。
    - 整体层内部按【小节】增量合并（见 _merge_overview_body）。
    找不到整体层标题 / body 为空 → None，调用方整体拒绝该候选——
    与 replace_synopsis_volume 同一纪律：宁可不写，不能写错位置。
    """
    if not (body or "").strip():
        return None
    body = _strip_volumes_from_overview(body)
    if not body:
        return None
    span = _overview_span(content)
    if span is None:
        return None
    start, head_end, end = span
    head = content[start:head_end]
    merged = _merge_overview_body(content[head_end:end], body, notes)
    return content[:start] + head + merged + content[end:]


def _norm_sec_name(name: str) -> str:
    """小节名归一化：去包裹符号与全半角空白（模型写『【主角设定】』，底稿可能是『## 主角设定』）。"""
    return (name or "").strip().strip("【】[]").replace(" ", "").replace("　", "")


def _sec_spans(text: str) -> list[dict]:
    """【小节】标题位置清单（可带 Markdown 前缀）→ [{name, start, head_end}]。

    head_end 收在闭括号『】』之后而非行尾：小节标题可能与正文写在同一行
    （『【其他设定】一段中…』2026-11-10 实测），正文必须从标题闭合处开始算，
    否则修订小节的首行正文会被当成标题行丢掉（表现为该节原文没被替换）。
    """
    out = []
    for m in _SEC_HEAD_RE.finditer(text or ""):
        close = text.find("】", m.end(1))
        head_end = close + 1 if close >= 0 else m.end()
        out.append({"name": m.group(1).strip(), "start": m.start(),
                    "head_end": head_end})
    return out


def _split_tail(seg: str) -> tuple[str, str]:
    """拆出「内容」与「尾部分隔区」（末尾空行 / --- 分隔线），替换时原样保留分隔区。"""
    m = _SEC_TRAIL_RE.search(seg or "")
    if not m:
        return seg or "", ""
    return (seg or "")[:m.start()], (seg or "")[m.start():]


def _looks_like_bare_subhead(line: str) -> bool:
    """一行是否像裸行小标题（伏笔台账/资产清单/分卷纲要 这类无标记大节名）。"""
    s = (line or "").strip()
    if not (2 <= len(s) <= _BARE_SUBHEAD_MAX):
        return False
    if any(ch in _BARE_SUBHEAD_BAD_CHARS for ch in s):
        return False
    if any(ch.isdigit() for ch in s):
        return False
    return s[0] not in "#【*>-—·"


def _bare_subhead_starts(core: str) -> list[int]:
    """整体层段内裸行小标题的字符位置（前面必须空行，即独立成段）。

    只用于「收缩小节替换区间」：命中越少越保守（少替换 = 原文多保留），
    绝不据此删除任何文字。
    """
    starts: list[int] = []
    pos = 0
    prev_blank = True
    for raw in (core or "").splitlines(keepends=True):
        s = raw.strip()
        if not s:
            prev_blank = True
            pos += len(raw)
            continue
        if prev_blank and (_BARE_SUBHEAD_NAME_RE.match(raw.strip())
                           or _looks_like_bare_subhead(s)):
            starts.append(pos)
        prev_blank = False
        pos += len(raw)
    return starts


def _bare_section_name(line: str) -> str | None:
    """裸行大节的名字：白名单名命中取标准名，通用判据命中取整行；都不是 → None。"""
    s = (line or "").strip()
    if not s:
        return None
    m = _BARE_SUBHEAD_NAME_RE.match(s)
    if m:
        return m.group(1)
    if _looks_like_bare_subhead(s):
        return s
    return None


def _bare_section_spans(text: str) -> list[dict]:
    """独立成段的裸行大节 → [{name, start, head_end}]；head_end=标题行行尾。

    与 _bare_subhead_starts 同一判据（前面必须空行），额外返回名字供同名对齐。
    """
    out: list[dict] = []
    pos = 0
    prev_blank = True
    for raw in (text or "").splitlines(keepends=True):
        s = raw.strip()
        if not s:
            prev_blank = True
            pos += len(raw)
            continue
        if prev_blank:
            name = _bare_section_name(s)
            if name:
                # head_end 不含行尾换行：标题行 + 「\n\n」+ 正文 = 恰一个空行
                out.append({"name": name, "start": pos,
                            "head_end": pos + len(raw.rstrip("\n"))})
        prev_blank = False
        pos += len(raw)
    return out


def _merge_overview_by_bare_sections(base_body: str, rev_body: str,
                                     notes: list | None = None) -> str | None:
    """修订没有行首【小节】但带裸行大节（伏笔台账/资产清单…）时按大节同名对齐合并。

    背景（2026-11-14《催眠》实测）：模型把新『伏笔台账』写成裸行大节（不带【】），
    前面还有一句说明（『仅【伏笔台账】一节涉及修订，整节替换如下…』）——【】夹在
    句中不算小节，rev_secs 为空，旧逻辑走『整段替换』兜底，把整体层里的
    【世界观与故事概述】【主角设定】【反派设定】【其他设定】与资产清单全顶掉。
    此函数只在大节名能对上底稿时才动手：同名替换、未提及的节/小节逐字保留、
    第一个大节标题前的说明句剥掉（协议要求修订内容是可直接替换的完整节原文，
    标题前内容只可能是引导语）、底稿没有的新大节追加到末尾。
    返回 None = 对不齐（调用方走自己的兜底；有结构的底稿绝不能整段替换）。
    """
    lead = base_body[:len(base_body) - len(base_body.lstrip("\n"))]
    core, tail = _split_tail(base_body)
    base_bare = _bare_section_spans(core)
    if not base_bare:
        return None
    rev_bare = _bare_section_spans(rev_body)
    if not rev_bare:
        return None
    known = ({_norm_sec_name(b["name"]) for b in base_bare}
             | {_norm_sec_name(s["name"]) for s in _sec_spans(core)})
    if not any(_norm_sec_name(r["name"]) in known for r in rev_bare):
        return None
    # base 的节边界：裸行大节 + 【小节】统一排序，替换区间到下一个边界为止
    bounds = sorted({b["start"] for b in base_bare}
                    | {s["start"] for s in _sec_spans(core)})
    rev_map: dict[str, str] = {}
    for i, rs in enumerate(rev_bare):
        end = rev_bare[i + 1]["start"] if i + 1 < len(rev_bare) else len(rev_body)
        rev_map[_norm_sec_name(rs["name"])] = rev_body[rs["head_end"]:end].strip()
    parts: list[str] = []
    cursor = 0
    for b in base_bare:
        end = next((p for p in bounds if p > b["head_end"]), len(core))
        parts.append(core[cursor:b["start"]])
        rbody = rev_map.get(_norm_sec_name(b["name"]))
        if rbody is None:
            parts.append(core[b["start"]:end])
        else:
            add = _split_additive(rbody)
            if add is not None:
                # 指令式「增补」：原文保留，新内容补在节尾
                parts.append(core[b["start"]:end].rstrip() + "\n\n" + add + "\n\n")
                if notes is not None:
                    notes.append(f"{b['name']}按增补合并")
            else:
                parts.append(core[b["start"]:b["head_end"]] + "\n\n" + rbody + "\n\n")
        cursor = end
    parts.append(core[cursor:])
    rebuilt = "".join(parts)
    for rs in rev_bare:
        if _norm_sec_name(rs["name"]) in known:
            continue
        # 新大节插在『分卷纲要』引导行之前——它是分卷区入口，新节追加在它
        # 后面会落进分卷区（2026-11-14 测试样例实测）。
        ins = rebuilt.rstrip("\n")
        m = _BARE_VOL_SECTION_RE.search(ins)
        if m:
            ins = ins[:m.start()].rstrip("\n")
        rebuilt = (ins + "\n\n" + rev_body[rs["start"]:rs["head_end"]].strip()
                   + "\n\n" + (rev_map.get(_norm_sec_name(rs["name"])) or ""))
    return (lead or "\n\n") + rebuilt + (tail or "\n")


def _split_additive(rev_body: str) -> str | None:
    """把「在某节/某段之后增补如下：<正文>」拆成 <正文>；不是这种写法 → None。

    迭代协议要求修订小节写成「可直接替换的完整小节原文」，但模型常交指令式写法
    （2026-11-10 实测：【其他设定】写的是「一段中，三条院规之后增补如下衔接：…」）。
    整节替换会把这节原有内容顶掉（那次的「体院规矩三条」「力量铁律」全丢了），
    所以识别成增补 → 原文保留、新内容追加。
    """
    s = (rev_body or "").strip()
    if not s:
        return None
    first = s.split("\n", 1)[0][:150]
    m = _ADDITIVE_HINT_RE.match(first)
    if not m:
        return None
    rest = s[m.end():]
    m2 = re.match(r"[^\n：:]{0,8}[：:]", rest)
    if m2:
        rest = rest[m2.end():]
    else:
        rest = rest.lstrip("，,。 \n")
    return rest.strip() or None


def _merge_overview_body(base_body: str, rev_body: str,
                         notes: list | None = None) -> str:
    """整体层按【小节】增量合并：只换修订里出现的同名小节，未提及小节逐字保留。

    迭代协议里 AI 只输出「评估为修改的段」；改到整体层内部某一小节时，它只写
    『【主角设定】』那一节（老板 2026-11-09 实测）。若把整段整体层替换成这一节，
    世界观 / 反派设定 / 其他设定会被顶掉——这正是「没修改的部分消失」。
    - 修订里没有任何【小节】→ 当完整整体层整段替换（模型重写了整段）；
    - 底稿整体层没有【小节】结构 → 整段替换（无处对齐，只能整体接受）；
    - 修订里出现底稿没有的小节名 → 追加到整体层末尾（不丢 AI 写的内容）。
    """
    base_body = base_body or ""
    rev_body = (rev_body or "").strip()
    if not rev_body:
        return base_body
    lead = base_body[:len(base_body) - len(base_body.lstrip("\n"))]
    core, tail = _split_tail(base_body)
    rev_secs = _sec_spans(rev_body)
    base_secs = _sec_spans(base_body)
    if not rev_secs or not base_secs:
        # 2026-11-14：修订没有行首【小节】但带裸行大节（『伏笔台账』等）时先按
        # 裸行大节同名对齐合并；整段替换只许底稿整体层没有任何节结构时用——
        # 底稿有【小节】/裸行大节而修订对不上时整段替换，未提及的节会被整批
        # 顶掉（《催眠》实测丢了世界观/主角/反派/其他设定与资产清单五个节）。
        merged = _merge_overview_by_bare_sections(base_body, rev_body, notes)
        if merged is not None:
            return merged
        if base_secs or _bare_section_spans(core):
            # 底稿有节结构、修订完全对不上 → 追加到整体层末尾，原内容一个字不丢
            #（改动可重试，底稿丢失不可逆）；note 里点名，确认框可见。
            # 修订若自带节标题（裸行大节），从第一个标题行起追加，标题前的说明句
            # 剥掉；新节插在『分卷纲要』引导行之前（它是分卷区入口）。
            units = _bare_section_spans(rev_body)
            if units:
                rev_body = rev_body[units[0]["start"]:].strip()
            ins = core.rstrip("\n")
            m = _BARE_VOL_SECTION_RE.search(ins)
            if m:
                ins = ins[:m.start()].rstrip("\n")
            if notes is not None:
                notes.append("整体层修订无法按小节对齐，已追加到整体层末尾（原文保留）")
            return lead + ins + "\n\n" + rev_body.strip() + (tail or "\n")
        return (lead or "\n\n") + rev_body + (tail or "\n")
    rev_map: dict[str, str] = {}
    for i, rs in enumerate(rev_secs):
        end = rev_secs[i + 1]["start"] if i + 1 < len(rev_secs) else len(rev_body)
        rev_map[_norm_sec_name(rs["name"])] = rev_body[rs["head_end"]:end].strip()
    parts: list[str] = []
    bare_starts = _bare_subhead_starts(core)
    cursor = 0
    for i, bs in enumerate(base_secs):
        end = base_secs[i + 1]["start"] if i + 1 < len(base_secs) else len(core)
        # 小节正文里混进裸行大节（伏笔台账 / 资产清单 / 分卷纲要）→ 替换区间收缩到
        # 它之前。裸行形态没有 Markdown 标题可以当段尾，最后一个【小节】的区间会一直
        # 延伸到整体层段末尾，整节替换会把台账/清单/纲要一起吃掉（2026-11-10 实测）。
        cut = next((p for p in bare_starts if bs["head_end"] <= p < end), None)
        if cut is not None:
            end = cut
        parts.append(core[cursor:bs["start"]])
        rbody = rev_map.get(_norm_sec_name(bs["name"]))
        if rbody is None:
            parts.append(core[bs["start"]:end])
        else:
            add = _split_additive(rbody)
            if add is not None:
                # 指令式「增补」：保留原文该节，新内容补在后面（不整节替换）
                parts.append(core[bs["start"]:end].rstrip() + "\n\n" + add + "\n\n")
                if notes is not None:
                    notes.append(f"{bs['name']}按增补合并")
            else:
                parts.append(core[bs["start"]:bs["head_end"]] + "\n\n" + rbody + "\n\n")
        cursor = end
    parts.append(core[cursor:])
    # 不 rstrip：core 末尾的换行是原段与尾部分隔区（--- / 空行）之间的空行，
    # 去掉会把『正文\n\n---』压成『正文\n---』。
    rebuilt = "".join(parts)
    base_names = {_norm_sec_name(b["name"]) for b in base_secs}
    for rs in rev_secs:
        key = _norm_sec_name(rs["name"])
        if key in base_names:
            continue
        rebuilt = (rebuilt.rstrip("\n") + "\n\n【" + rs["name"] + "】\n\n"
                   + (rev_map.get(key) or ""))
    return rebuilt + (tail or "\n")


def _bare_volume_head(num_raw: str, sep: str, title: str) -> str:
    """裸行卷标题行：保留底稿的编号写法与分隔符（『分卷二，卷名』）。"""
    num_raw = (num_raw or "").strip()
    t = (title or "").strip()
    if not t:
        return f"分卷{num_raw}"
    return f"分卷{num_raw}{sep or '：'}{t}"


def replace_bare_synopsis_volume(content: str, index: int, title: str,
                                 body: str) -> str | None:
    """裸行形态总纲里原位替换『分卷N』段（无 # 标记，卷名分隔符可为中文逗号）。

    与 Pipeline.replace_synopsis_volume 同一纪律：段边界只认裸行卷标题行
    （可信门槛由 bare_synopsis_volume_marks 把关），找不到该卷 → None。
    """
    if not (body or "").strip():
        return None
    marks = bare_synopsis_volume_marks(content)
    if not marks:
        return None
    k = next((i for i, m in enumerate(marks) if m["index"] == int(index)), None)
    if k is None:
        return None
    mk = marks[k]
    end = marks[k + 1]["line_start"] if k + 1 < len(marks) else len(content)
    seg = content[mk["line_start"]:end]
    ws = seg[len(seg.rstrip("\n")):]
    head = _bare_volume_head(mk["num_raw"], mk["sep"], title)
    return (content[:mk["line_start"]] + head + "\n" + body.strip()
            + (ws or "\n\n") + content[end:])


def append_bare_synopsis_volume(content: str, index: int, title: str,
                                body: str) -> str | None:
    """裸行形态总纲末尾追加一卷（编号风格与分隔符跟随最后一卷）。"""
    if not (body or "").strip():
        return None
    marks = bare_synopsis_volume_marks(content)
    if not marks:
        return None
    last = marks[-1]
    num_raw = last["num_raw"]
    num = (str(int(index)) if num_raw.isdigit()
           else Pipeline._int_to_vol_num(int(index), "cn"))
    head = _bare_volume_head(num, last["sep"], title)
    block = head + "\n" + body.strip() + "\n"
    # 分卷区后面若还有 Markdown 大节（附录/台账），插在它之前，别追加到文件尾
    nxt = next((x for x in _MD_ANY_TITLE_LINE_RE.finditer(content)
                if x.start() > last["line_start"]), None)
    if nxt is None:
        return content.rstrip() + "\n\n" + block
    return content[:nxt.start()].rstrip() + "\n\n" + block + "\n" + content[nxt.start():]


def apply_synopsis_iter_patch(base: str, review: dict) -> tuple[str | None, str]:
    """把总纲迭代的「评估+修订内容」候选合成进底稿：未提及的卷/整体层逐字不动。

    返回 (new_content, note)：
    - "noop"：AI 评估全部保留（修订内容只是「无需修订」类占位词）→ 返回底稿原样；
    - "ok..."：合成成功，note 带替换明细；含交叉校验——评估标「修改」但修订
      内容缺段时在 note 里点名（不阻塞，确认框提示）。
    - (None, 说明)：某段在底稿中找不到原位 → 整批拒绝该候选。
      修订内容只是片段，绝不能当全文整存（会把未修订的卷全部弄丢）。
    底稿的两种形态都认：Markdown（## 整体层 / ### 分卷N）与裸行（整体层 /
    分卷一，卷名）——老板的自定义总纲模板产出的就是裸行形态（2026-11-10 实测）。
    """
    base = (base or "").strip()
    body = (review.get("body") or "").strip()
    upd = [it for it in review.get("items") or [] if it.get("action") == "update"]
    stripped = body.strip("*# ：:、")
    if not body or stripped in _ITER_NOOP_WORDS:
        return base, "noop"
    rev = Pipeline.parse_synopsis_volumes(body)
    rev_overview = _strip_volumes_from_overview(rev.get("overview") or "")
    rev_volumes = rev.get("volumes") or []
    if not rev_overview and not rev_volumes:
        return None, "修订内容里没有识别到『整体层』或『分卷N』段落"
    new = base
    replaced: list[str] = []
    warn: list[str] = []
    if rev_overview:
        patched = replace_synopsis_overview(new, rev_overview, warn)
        if patched is None:
            return None, "底稿中找不到『整体层』段落标题，无法原位替换"
        new = patched
        replaced.append("整体层")
    base_titles = {v.get("index"): (v.get("title") or "")
                   for v in base_parsed_volumes(base)}
    for v in rev_volumes:
        idx = int(v.get("index") or 0)
        vtitle = (v.get("title") or "").strip() or base_titles.get(idx, "")
        patched = Pipeline.replace_synopsis_volume(new, idx, vtitle, v.get("content") or "")
        if patched is None:
            patched = replace_bare_synopsis_volume(new, idx, vtitle, v.get("content") or "")
        if patched is not None:
            new = patched
            replaced.append(f"分卷{idx}")
            continue
        # 底稿没有这一卷：只有「紧接现行最后一卷的新增卷」才追加（迭代意见要求加卷）；
        # 卷号断档 / 模型幻觉的幽灵卷仍然整批拒绝——绝不把凭空冒出的卷号写进总纲。
        cur_max = max([int(x.get("index") or 0) for x in base_parsed_volumes(new)] or [0])
        if idx == cur_max + 1:
            appended = Pipeline.append_synopsis_volume(
                new, idx, vtitle, v.get("content") or "")
            if appended is None:
                appended = append_bare_synopsis_volume(
                    new, idx, vtitle, v.get("content") or "")
            if appended is not None:
                new = appended
                replaced.append(f"分卷{idx}（新增）")
                continue
        return None, f"底稿中找不到『分卷{idx}』的标题行，无法原位替换"
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
    if warn:
        note += f"（{'；'.join(warn)}）"
    return new, note


def looks_like_full_synopsis(cand: str, base: str) -> bool:
    """候选是否可当「完整总纲」整份覆盖：整体层有内容，且分卷数不少于底稿。

    迭代链路里模型没按两段式协议输出时，调用方靠它区分「真·全量重写」与
    「只剩修订片段」（2026-11-09 老板实测：片段被当全文落库，未改的卷全丢）。
    判不准一律当片段拒绝——宁可让用户重试，也不能把总纲写残。
    """
    try:
        c = Pipeline.parse_synopsis_volumes(cand or "")
        b = Pipeline.parse_synopsis_volumes(base or "")
    except Exception:
        return False
    if not (c.get("overview") or "").strip():
        return False
    cv = c.get("volumes") or []
    bv = b.get("volumes") or []
    if not cv:
        return False
    if bv:
        return len(cv) >= len(bv)
    # 底稿解析不出分卷（旧 ■/裸行混排）：退回长度比，仍按保守口径
    return len((cand or "").strip()) >= max(200, int(len((base or "").strip()) * 0.6))


def base_parsed_volumes(base: str) -> list[dict]:
    """底稿总纲的分卷清单（卷名回填用）：解析失败安全返回空表。"""
    try:
        return Pipeline.parse_synopsis_volumes(base).get("volumes") or []
    except Exception:
        return []
