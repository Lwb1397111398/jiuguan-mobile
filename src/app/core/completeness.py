# -*- coding: utf-8 -*-
"""结构完整性备选判定（Stage Completeness Fallback，2026-09-11）。

**要解决的问题**：完成标志协议（`endmark.py`）要求模型在回复最末尾输出一行
【生成完毕】。实测模型经常「内容确实写完了，却忘了打标志」——真实样本：
《毛贼风波》prose log 58（14807 字，末句「……成为了这场狂欢序幕落下的最后一声
丧钟。」，是完整的章末收束，但没有标志）。旧行为把它判成「软截断」→ 自动模式
白跑 9 次重试节拍、手动模式弹「可能不完整」确认框，**完整产物被当废稿丢掉，
非常浪费 token**。

**机制**：标志缺失时，再问一次「产物自身是不是已经结构完整了」，每个阶段一条
专属判据（老板提的「序号对账」思路 —— 细纲末块序号 11.5 与大纲 ⑪（5）对上、
大纲末点序号与分卷初稿的序号点对上）：

| 阶段 | 备选判据 | 依据 |
|---|---|---|
| 正文 / 改写 | 正文 ≥ 500 字 **且** 末尾是收尾标点（。！？…） | 与 `detect_unfinished` 同一套标点表 |
| 细纲 | 输出的最大结构序号（**拍N.M 只取整数部分** / 子事件X / 归属X）≥ 本章大纲的最大序号点，且末尾收住 | 大纲共 11 个序号点 → 细纲得写到 11（11.5 只看 11；块N 不参与对账） |
| 大纲 | 细化稿的最大序号点 ≥ 分卷初稿（outline_draft）的最大序号点，且末尾收住 | 分卷第四章 11 个节拍 → 大纲要展开到 ⑪ |
| 分卷 | 切出的章数与「精确章数」设置一致（未设置则跳过），末章正文达标且收住 | 少一章 / 末章半截 = 没切完 |
| 总纲 | 解析出 ≥1 个分卷且末卷正文达标、收住 | 分卷写完才算完 |

**保守优先（红线）**：证据不足一律返回 False，上层照旧按「未写完」处理
（重试 / 弹确认框）。宁可多花一次重试，也不能把半截稿当完整稿静默落库。
所有判据只在标志缺失时才被调用，标志正常命中时这条链路完全不参与。

本模块是纯函数实现（仅依赖 `re` + 同目录 `chapter_outline`），桌面/手机端共用。
`END_PUNCTUATION` 在本模块定义、由 pipeline 反向导入，保证只此一份。
"""

import re

from .chapter_outline import split_outline_points

# 收尾标点（正文阶段备选判据）：正文自然结束时以这些结尾。
# 唯一含义来源——pipeline 的 detect_unfinished / trim_unfinished_tail 从这里导入。
END_PUNCTUATION = "。！？…\"”』」））"

# 行尾 markdown 装饰（粗体/斜体/行内代码）：判定收尾前必须先剥掉，否则
# 『**to：……全书定格收声。**』的末字符是 `*`，会被误判成「结尾没有闭合」
# （2026-09-17 老板实测：《腐化森林》细纲末行就是粗体的 to 状态行）。
TAIL_DECOR = " \t*_`~"

# 结构化产物末尾的「半截」信号：以这些字符结尾 = 句子/字段没收住。
# 半角句点「.」故意不在正文收尾表里（英文半截稿也可能是句点结尾）。
_DANGLING_TAIL_RE = re.compile(r"[（(【\[「『《<＜〔：:，,、；;—\-\|｜~～]$")

# 纯结构标题行（标记后面什么都没有）：『## 子事件④：』『### 块7』『【块12】』
# 出现在最末尾 = 结构开了个头没收内容 → 不算收住。
_BARE_MARK_HEAD_RE = re.compile(
    r"^\s*[>#*\-—·【\[（(]{0,8}\s*"
    r"(?:拍|子事件|节拍|块|归属)\s*[①-⑳\d]{1,3}"
    r"\s*[\]】）)*#|｜:：\-—\s]*$")

# 行首结构标记（细纲）：拍N.M / 子事件X / 节拍X / 归属X
# 装饰前缀（##、**、- 等）与标记之间允许空格：`### 块1 ｜ 归属①`、`**拍1.1 | …**`
# 注意「拍N.M」只取小数点前的 N（老板定标：细纲的 11.5 对应大纲的 ⑪，不看小数）；
# 注意「块N」**不在**主对账信号里——块的编号独立递增、与大纲序号点数无关
# （骑士团一章 14 块、大纲只有 7 个点），算进 max 会让半截稿提前"达标"，
# 只配做没有任何拍/子事件/归属信号时的最后兜底。
_INDEX_TOK = r"(?:[①-⑳]|\d{1,3})"
_PAI_LINE_RE = re.compile(r"^\s*[>#*\-—·]{0,8}\s*拍\s*(\d{1,3})\s*[.．]")
_MARK_LINE_RE = re.compile(
    r"^\s*[>#*\-—·]{0,8}\s*(?:拍|子事件|节拍|归属)\s*"
    r"([①-⑳]|\d{1,3})")
_ASSIGN_RE = re.compile(r"归属\s*([①-⑳]|\d{1,3})")
# 兜底一：整行以 ① 起头（部分自定义细纲模板直接套大纲的序号点格式）
_CIRCLED_LINE_RE = re.compile(r"^\s*[>#*\-—·]{0,8}\s*([①-⑳])")
# 兜底二：只有「块N」的极简模板（块编号与大纲点无关，最后才用）
_BLOCK_LINE_RE = re.compile(r"^\s*[>#*\-—·]{0,8}\s*块\s*([①-⑳]|\d{1,3})")


def _tok_index(tok: str) -> int:
    """序号词元 → 整数（①=1；阿拉伯数字直取；其它为 0）。"""
    tok = (tok or "").strip()
    if len(tok) == 1 and "①" <= tok <= "⑳":
        return ord(tok) - ord("①") + 1
    if tok.isdigit():
        return int(tok)
    return 0


# 成对符号：成对数量不等 = 有一段没写完（细纲的「帧链 {…}」、大纲的括注）
_BRACKET_PAIRS = (("{", "}"), ("（", "）"), ("【", "】"), ("《", "》"))

# 实测定标（2026-09-11，4 个项目 578 条日志回放，明细见 docs/识别模块总览.md 十八）：
# - 大纲：已知完整的 4/4 末行都以「）。」收尾；被这条规则拒掉的两条
#   （T001 log44「…感官锚点：颜料的束缚感与排」、log217「…热样例方向"运动员
#   的心脏是他自己练」）都是**真的截断**。故大纲阶段用 require_terminal=True。
# - 细纲：已知完整的 4 条里有 3 条末行是「to：状态行」「（本章备注）」这类
#   不带句号的收尾 → 细纲绝不能要求收尾标点（会误杀完整稿）。
_STRUCT_CLOSERS = END_PUNCTUATION + "）】」』》〉"


def _brackets_balanced(text: str) -> bool:
    """成对符号是否闭合（细纲的帧链 {…}、大纲的括注）。不闭合 = 半截稿。"""
    t = text or ""
    return all(t.count(o) == t.count(c) for o, c in _BRACKET_PAIRS)


def tail_looks_closed(text: str, require_terminal: bool = False) -> bool:
    """结构化产物末尾是否「收住」（不是半截标题/半截字段/悬空括号）。

    - 末行以悬空符号（：，「、—、开括号 等）结尾 → 没收住；
    - 末行只有一个结构标记（『## 子事件④：』『### 块7』）→ 没收住；
    - 全文成对符号（{ } （ ） 【 】 《 》）数量不等 → 有一段没写完；
    - require_terminal=True 时还要求末行以收尾标点/右括号收尾
      （大纲阶段用：其格式天然以「）。」结束，见上方实测定标）。
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return False
    last = lines[-1].rstrip(TAIL_DECOR)
    if not last:
        return False
    if _DANGLING_TAIL_RE.search(last):
        return False
    if _BARE_MARK_HEAD_RE.match(last):
        return False
    if not _brackets_balanced(text):
        return False
    if require_terminal and last[-1] not in _STRUCT_CLOSERS:
        return False
    return True


def detail_reached_point(text: str) -> int:
    """细纲输出覆盖到的最大「大纲序号点」（0 = 判不出）。

    对账信号（按优先级）：
    1. 拍N.M 的**整数部分** N（老板定标：细纲 11.5 ↔ 大纲 ⑪，小数不看）；
    2. 行首的 子事件X / 节拍X / 归属X；
    3. 兜底一：行首圈码 ①（部分自定义模板直接套大纲序号点格式）；
    4. 兜底二：块N（**块编号与大纲点无关**——独立递增、一章可以有 14 块而大纲
       只有 7 个点，算进对账会让半截稿提前"达标"，所以只在上面全都没有时才用；
       默认模板『### 块N ｜ 归属X』靠归属对账，不受影响）。

    刻意不扫行文里的 ①②③：模型会在标题里括注『（含节拍①②③）』、在衔接指令里
    写『切子事件⑪』——全量扫会让序号虚高，把半截稿误判成完整稿（这是本判据
    唯一的失效方向，必须堵死）。
    """
    best = 0
    for line in (text or "").splitlines():
        m = _PAI_LINE_RE.match(line)
        if m:
            # 拍行本身就是序号（拍11.5 → 第 11 个序号点），不再看行内其它标记
            best = max(best, int(m.group(1)))
            continue
        m = _MARK_LINE_RE.match(line)
        if m:
            best = max(best, _tok_index(m.group(1)))
        for m2 in _ASSIGN_RE.finditer(line):
            best = max(best, _tok_index(m2.group(1)))
    if best:
        return best
    for line in (text or "").splitlines():          # 兜底一：行首圈码
        m = _CIRCLED_LINE_RE.match(line)
        if m:
            best = max(best, _tok_index(m.group(1)))
    if best:
        return best
    for line in (text or "").splitlines():          # 兜底二：只有块N 的极简模板
        m = _BLOCK_LINE_RE.match(line)
        if m:
            best = max(best, _tok_index(m.group(1)))
    return best


def numbered_max_index(text: str) -> int:
    """序号点文本的最大序号（大纲/大纲初稿共用，0 = 判不出）。

    复用 `chapter_outline.split_outline_points`（①、1.、（1）、一、都能认）。
    """
    idx = [p.get("index") or 0 for p in split_outline_points(text or "")]
    return max(idx) if idx else 0


def prose_complete(text: str, min_chars: int = 500) -> tuple:
    """正文/改写备选：够长 + 末尾是收尾标点。返回 (是否完整, 依据说明)。

    min_chars=500（老板定标 2026-09-11「正文改写要大于等于五百，这样不容易出
    问题」）：只挡「拒答/占位/小段截断」级别的输出——门槛越高越不容易把半截稿
    当完整稿放行。整章正文动辄几千字不受影响；改写稿低于 500 字时备选不参与
    （照旧按未写完走重试/确认框，与旧行为一致）。

    末尾依次剥掉行尾装饰（`TAIL_DECOR`）与空白再判标点：`……丧钟。**` 也算收尾。
    """
    body = (text or "").strip().rstrip(TAIL_DECOR)
    if len(body) < min_chars:
        return False, ""
    if body[-1] not in END_PUNCTUATION:
        return False, ""
    return True, "正文已收尾（末尾为收尾标点）"


def detail_complete(text: str, expect_point: int) -> tuple:
    """细纲备选：写到本章大纲的最后一个序号点，且末尾收住。"""
    if expect_point <= 0:
        return False, ""      # 没有大纲可对账 → 不判（保守）
    reached = detail_reached_point(text)
    if reached < expect_point:
        return False, ""
    if not tail_looks_closed(text):
        return False, ""
    return True, f"细纲写到序号点 {reached}（本章大纲共 {expect_point} 个）"


def outline_complete(text: str, expect_point: int) -> tuple:
    """大纲备选：展开到分卷初稿的最后一个序号点，且末尾收住。

    末尾比细纲更严（要求收尾标点）：大纲格式天然以「）。」结束，实测能拦下
    「写到 ⑨ 但 ⑨ 的括注只写了一半」这类截断（见 _STRUCT_CLOSERS 上方定标）。
    """
    if expect_point <= 0:
        return False, ""      # 没有初稿可对账 → 不判（保守）
    reached = numbered_max_index(text)
    if reached < expect_point:
        return False, ""
    if not tail_looks_closed(text, require_terminal=True):
        return False, ""
    return True, f"大纲已展开到序号点 {reached}（分卷初稿共 {expect_point} 个）"


def volume_complete(chapters: list, expect_chapters: int | None = None,
                    min_chars: int = 200) -> tuple:
    """分卷备选：章数符合设置（若有）+ 末章正文达标且以收尾标点结束。

    末章正文是「整章剧情概要」散文，天然以「。」结束（实测定标：已知完整的
    分卷日志 2/2 以「。」收尾）；不要求收尾标点会放过「末章写到一半」的截断
    （实录 T001 volume log280 结尾「…目标G的坐标在」、log283「…下一个坐标正在演」）。
    """
    chapters = [c for c in (chapters or []) if c]
    if not chapters:
        return False, ""
    if expect_chapters and len(chapters) != expect_chapters:
        return False, ""
    last = (chapters[-1].get("content") or "").strip()
    if len(last) < min_chars or not tail_looks_closed(last, require_terminal=True):
        return False, ""
    if expect_chapters:
        return True, f"已切成 {len(chapters)} 章（要求 {expect_chapters} 章），末章完整"
    return True, f"已切成 {len(chapters)} 章，末章完整"


def synopsis_complete(volumes: list, min_chars: int = 50) -> tuple:
    """总纲备选：解析出分卷且末卷正文达标、以收尾标点结束。"""
    vols = [v for v in (volumes or []) if (v.get("content") or "").strip()]
    if not vols:
        return False, ""
    last = (vols[-1].get("content") or "").strip()
    if len(last) < min_chars or not tail_looks_closed(last, require_terminal=True):
        return False, ""
    return True, f"含 {len(vols)} 个分卷，末卷内容完整"
