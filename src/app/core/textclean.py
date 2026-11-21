# -*- coding: utf-8 -*-
"""模型输出清洗：剥离混进正文的思考内容（统一入口，全阶段共用）。

模型「思考」有两条通道：

- **A. 结构化字段**（``reasoning_content`` / ``content[].type=thinking``）：
  ``api.py`` 已正确分离，走「思考过程」面板，不污染正文。
- **B. 写进正文文本**（本模块负责）：
  ① 内联标签 ``<think>…</think>``（Qwen / DeepSeek / GLM 等渠道常见）；
  ② 「思考环节 … 正式输出」分段（自定义任务模板要求，属常态而非偶发）。

通道 B 历史上完全没剥离，被当成创作内容解析入库（2026-09-04 全量审查确认）。

设计红线：**保守优先，绝不丢正文**。任何一步判定不可靠 → 原样返回。
剥离下来的思考内容交还调用方（并入思考过程面板），不写进任何创作数据。
"""
import re

# ---------- 语种判定（无标记内联思考的判定基础） ----------

# 汉字（CJK 统一表意文字扩展 A + 基本区）；日语假名不计——正文为中日混排时
# 也不该被当成「外文前言」
_HAN_CHAR_RE = re.compile(r"[\u3400-\u9fff]")
_LATIN_CHAR_RE = re.compile(r"[A-Za-z]")


def _dominant_lang(text: str, min_chars: int = 100) -> str | None:
    """返回文本的主导语种 'han' / 'latin'，混排或分量不足返回 None（拿不准）。"""
    han = sum(1 for _ in _HAN_CHAR_RE.finditer(text or ""))
    lat = sum(1 for _ in _LATIN_CHAR_RE.finditer(text or ""))
    if han >= min_chars and han > lat * 2:
        return "han"
    if lat >= min_chars and lat > han * 2:
        return "latin"
    return None

# ---------- 内联思考标签 ----------

_BAR = r"[|｜]"          # 半角/全角竖线（GLM 系用 <｜think▁｜>）
_UND = r"[_▁]?"          # 可选下划线/下八分块
_NAME = r"(?:think(?:ing)?|thought|reason(?:ing)?)"

_THINK_OPEN_RE = re.compile(
    rf"<\s*{_BAR}?\s*{_NAME}\s*{_UND}\s*{_BAR}?\s*>", re.I)
_THINK_CLOSE_RE = re.compile(
    rf"<\s*{_BAR}?\s*/\s*{_NAME}\s*{_UND}\s*{_BAR}?\s*>", re.I)

# ---------- 「思考环节 / 正式输出」分段 ----------

# 句读：标题行不该有这些（用于排除正文里「他陷入了思考。」这类叙述句）
_PUNCT_RE = re.compile(r"[。，；！？!?]")

# Markdown 标题形式的思考段头：## 思考环节 / ### 思考 / # 思维链（可带加粗）
# 「开工检查」不进这张词表（2026-10-09 定标）：分批续批模板的检查段与正文之间常
# 没有【正文输出】锚点，通用思考段截断的边界会退到下一个【…】（＝滚动状态包），
# 把两千字的整段正文当思考剥掉（极寒末日 log23/39、极限体能 log59、校霸 log43
# 误伤实锤）——批内检查段只在**批级**剥离，见 pipeline._strip_batch_open。
_MD_THINK_HEAD_RE = re.compile(
    r"^[ \t]{0,3}(#{1,6})[ \t]*(\*{0,2})[^\n]{0,16}?"
    r"(?:思考|思维链|推理|分析|构思|自检|自我检[查核]|打草稿)"
    r"[^\n]{0,16}?\2[ \t]*[:：]?[ \t]*$", re.M)

# 【】/[] 包裹的思考段头：【思考环节】/ [推理过程]（开工检查见上，不进表）
_BRACKET_THINK_HEAD_RE = re.compile(
    r"^[ \t]{0,3}[【\[][ \t]*[^\n]{0,16}?"
    r"(?:思考|思维链|推理|分析|构思|自检|自我检[查核])"
    r"[^\n]{0,16}?[ \t]*[】\]][ \t]*$", re.M)

# 正式输出锚点：## 正式输出 / 【正式输出】/ 正式输出：/ **正式输出**
# 「正文输出」同义（2026-10-05 分批模板用【正文输出】标记正文起点）——锚点缺失时
# 思考段的结束边界只能靠下一个【…】标记，最坏会把整批正文截进思考里剥掉。
_FORMAL_HEAD_RE = re.compile(
    r"^[ \t]{0,3}(?:#{1,6}[ \t]*)?(?:\*{0,2})?(?:[【\[][ \t]*)?[^\n]{0,8}?"
    r"(?:正式输出|正文输出)[^\n]{0,16}?[ \t]*[:：]?[ \t]*(?:[】\]])?(?:\*{0,2})?[ \t]*$", re.M)

# 协议分隔行「窄形态」（2026-10-12《散打》总纲 log15 实测）：模型按模板把分隔行转写成
# 「正式输出如下。」——带句读被通用形态的 head_like 挡掉；思考段占比超过 60% 位置区
# （该书思考框架 P0~P6 占到 64%）又被位置区挡掉，两头卡死 → 整篇当总纲进识别、
# 报「未识别到总纲候选」。窄形态整行只允许「前缀 + 核心词 + 固定引导后缀 +
# 括号内注解 + 一个结尾标点」，没有 [^\n] 兜底位——正常行文不可能整行恰是这一句，
# 所以豁免位置区与标点校验。模板正体「正式输出（严格按 Markdown 输出，…）：」
# 括号注解超通用形态 16 字窗口，同走此路。
_FORMAL_NARROW_RE = re.compile(
    r"^[ \t]{0,3}(?:#{1,6}[ \t]*)?(?:\*{0,2})?(?:[【\[][ \t]*)?"
    r"(?:以下是|接下来是|现在)?[ \t]*(?:正式输出|正文输出)"
    r"(?:[ \t]*(?:如下所示|如下|现在开始))?"
    r"(?:[ \t]*[（(][^（）()\n]{1,40}[）)])?"
    r"[ \t]*(?:[】\]])?(?:\*{0,2})?[ \t]*(?:[。．]|[：:])?[ \t]*$", re.M)

# 总纲裸框架行锚点（2026-11-21《小偷入室》synopsis log24 实测）：模型整篇漏写
# 「正式输出」标记，思考段（裸行「思考输出」开头 + P0~P7 框架）后用一条 --- 分隔线
# 直接接总纲正文，正文以裸行「整体层」开头、分卷行全是裸行「分卷N」——
# 正式输出锚点（宽/窄形态）与思考段头（【…】/Markdown 标题）两头全部落空
# → 思考段原样混进正文 → 「未识别到总纲候选」。
# 裸行「整体层」是总纲框架的结构信号（与裸章标题行同理：结构化阶段的框架行是
# 数据、是「正文起点」的强信号），整行恰为该词时豁免位置区——正常行文不会
# 有一行只有「整体层」。
# ⚠️ 只认裸行「整体层」，不认 `# 整体层`/【整体层】：H1 大段总纲（10-07）靠
# `# 整体层` 判定「整篇是一份候选」，锚点若把它吃掉会把框架行从正文里剥掉。
# ⚠️ 也**不认**「分卷纲要」：它常是总纲正文**中段**的分卷区标记（固定框架/
# 第二种框架的正文里都有），拿它当锚点会把正文拦腰切断、设定段全丢。
# 无「整体层」行时由裸行思考段头 + --- 边界（第三步）兜住。
_SYNOPSIS_FRAMEWORK_RE = re.compile(
    r"^[ \t]{0,3}(?:\*{0,2})?整体层(?:\*{0,2})?[ \t]*[:：]?[ \t]*$", re.M)

# 裸行思考段头（2026-11-21《小偷入室》log24）：协议要求「先思考输出、再正式输出」，
# 模型有时把思考标记写成不带任何包装的裸行（「思考输出」独立成行，无【】无 #
# 无加粗）——既有两张思考段头表（Markdown 标题 / 【…】独立行）都匹配不上。
# 只认「整行恰为协议思考词（可带冒号）」，与思考纪律协议用词对齐；
# 正常行文不会有独立成行的「思考输出」。
_BARE_THINK_HEAD_RE = re.compile(
    r"^[ \t]{0,3}(?:\*{0,2})?(?:思考输出|思考环节|思考过程|自由思考|推理过程|思维链)"
    r"(?:\*{0,2})?[ \t]*[:：]?[ \t]*$", re.M)

# 裸行思考段的结束边界补充：思考段后常接一条 --- 分隔线再进正文
# （该形态下思考段里没有任何【…】标记或标题可认）。裸行思考段专属，
# 不进通用 _think_head_end——通用形态已有可靠边界，加分隔线只会让
# 既有语料的截断点提前（2026-11-21 保持既有行为零变化的保守取法）。
_BARE_THINK_BOUNDARY_RE = re.compile(
    r"^[ \t]{0,3}(?:-{3,}|\*{3,}|_{3,})[ \t]*$", re.M)

# 任意 Markdown 标题（用于定位思考段的结束边界）
_MD_ANY_HEAD_RE = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]*[^\n]*$", re.M)
# 任意独立成行的【…】标记
_BRACKET_HEAD_RE = re.compile(r"^[ \t]{0,3}[【\[][^\n]{0,24}[】\]][ \t]*$", re.M)
# 草稿块开标签：草稿自检模式下 <draft> 紧跟在思考段之后，是可靠的结构边界
# （思考段必须截在它之前，否则草稿会被一起吞掉）
_DRAFT_OPEN_ANY_RE = re.compile(r"<\s*draft\s*>", re.I)

# ---------- 安全兜底 ----------

# 清洗后剩余长度低于原文的该比例（且原文够长）→ 判定异常，回退原文
_SAFE_MIN_RATIO = 0.15
_SAFE_MIN_LEN = 500
# 思考段最长容忍距离：超过说明「下一个标题」判断不可靠，宁可不截
_MAX_SECTION_CHARS = 3000
# 思考段头只认全文前 30%（思考总在最前）；正式输出锚点放宽到前 60%
_THINK_HEAD_ZONE = 0.30
_FORMAL_ANCHOR_ZONE = 0.60
# 段头行长度上限（超过就不像标题行）
_HEAD_LINE_MAX = 40


def has_think_marks(text: str) -> bool:
    """快速判断文本里是否可能含思考痕迹（无痕时跳过整条清洗链）。"""
    t = text or ""
    if not t:
        return False
    if _THINK_OPEN_RE.search(t):
        return True
    # 「正文输出」= 分批模板的正文起点标记（_FORMAL_HEAD_RE 同义项）；
    # 「开工检查」= 分批续批模板的思考段头——两词不进预筛会让整条剥离链被跳过。
    if ("思考" in t or "思维链" in t or "推理" in t or "正式输出" in t
            or "正文输出" in t or "开工检查" in t or "开工核对" in t):
        return True
    # 无标记内联思考的廉价预筛：头部是一大段外文（实测几乎全是英文 planning），
    # 正文却是中文（或反过来）→ 可能走了 strip_inline_think 的形态。这里只做
    # 粗筛决定要不要进完整判定，真正的语种/占比校验在 strip_inline_think 里。
    head = t[:1500]
    if sum(1 for _ in _LATIN_CHAR_RE.finditer(head)) >= 80 and _HAN_CHAR_RE.search(t):
        return True
    if sum(1 for _ in _HAN_CHAR_RE.finditer(head)) >= 80 and _LATIN_CHAR_RE.search(t):
        return True
    return False


def strip_think_tags(text: str) -> tuple[str, str, bool]:
    """剥离内联思考标签 → (正文, 思考内容, 标签未闭合)。

    - 成对的 <think>…</think>：全部剥离，思考内容按出现顺序拼接
    - 未闭合开标签：标签前有正文 → 其后全部按思考处理，incomplete=True；
      前面无正文 → 原样返回（绝不丢正文，交给截断续写/诊断机制处理）
    - 无开标签 → 原样返回（幂等）
    """
    text = text or ""
    opens = list(_THINK_OPEN_RE.finditer(text))
    if not opens:
        return text, "", False
    parts: list[str] = []
    removed: list[str] = []
    last = 0
    incomplete = False
    for m in opens:
        close = _THINK_CLOSE_RE.search(text, m.end())
        if close is None:
            parts.append(text[last:m.start()])
            removed.append(text[m.end():])
            last = len(text)
            incomplete = True
            break
        parts.append(text[last:m.start()])
        removed.append(text[m.end():close.start()])
        last = close.end()
    parts.append(text[last:])
    clean = "".join(parts)
    if not clean.strip():
        return text, "", True  # 全部内容都在标签里：原样返回，绝不丢正文
    return clean, "\n".join(x for x in removed if x.strip()), incomplete


def _line_is_head_like(line: str) -> bool:
    """行是否像标题行：不含句读、长度不超限。"""
    if len(line.strip()) > _HEAD_LINE_MAX:
        return False
    return not _PUNCT_RE.search(line)


def _formal_anchor(text: str) -> int | None:
    """定位「正式输出」锚点行，返回该行之后的起始下标；拿不准返回 None。

    两类候选**按位置取最早**（锚点在正文起点，更早的才是真分隔行；若窄形态
    整体优先，「浅处合法宽锚 + 深处正文恰好一行『正式输出如下。』」会把正文开头
    切进思考段）：
    - 宽形态：受 60% 位置区 + head_like（无句读、≤40 字）双重限制；
    - 窄形态协议分隔行（「正式输出如下。」，见 _FORMAL_NARROW_RE 注释）：
      整行恰为模板规定的分隔语，形态本身即强信号，豁免两项校验；
    - 总纲裸框架行（「整体层」「分卷纲要」整行，见 _SYNOPSIS_FRAMEWORK_RE 注释）：
      总纲正文的起点信号，豁免位置区。
    """
    zone = len(text) * _FORMAL_ANCHOR_ZONE
    cands: list[tuple[int, int]] = [(m.start(), m.end())
                                    for m in _FORMAL_NARROW_RE.finditer(text)]
    cands += [(m.start(), m.end()) for m in _SYNOPSIS_FRAMEWORK_RE.finditer(text)]
    for m in _FORMAL_HEAD_RE.finditer(text):
        if m.start() > zone:
            continue
        if not _line_is_head_like(m.group(0)):
            continue
        cands.append((m.start(), m.end()))
    for pos, end in sorted(cands):
        nxt = text.find("\n", end)
        start = nxt + 1 if nxt >= 0 else len(text)
        if text[start:].strip():
            return start
    return None


def _think_head_end(text: str, head_match) -> int | None:
    """定位思考段结束边界（下一个同级/更高级标题行），拿不准返回 None。"""
    md = re.match(r"^[ \t]{0,3}(#{1,6})[ \t]*", head_match.group(0))
    level = len(md.group(1)) if md else 0
    start = head_match.end()
    cands: list[int] = []
    # Markdown 同级/更高级标题
    for m in _MD_ANY_HEAD_RE.finditer(text, start):
        if len(m.group(1)) <= (level or 6):
            cands.append(m.start())
        break
    # 草稿块开标签（思考段必须截在它之前，草稿不能丢）
    m = _DRAFT_OPEN_ANY_RE.search(text, start)
    if m:
        cands.append(m.start())
    # 【…】标记行（无 # 级别时的通用边界）
    if level == 0:
        for m in _BRACKET_HEAD_RE.finditer(text, start):
            cands.append(m.start())
            break
        # 裸章标题行（2026-10-07）：模型漏写「正式输出」锚点时，思考段与正文之间
        # 常只有一行『第N章：名』；而【自由思考】这类标记可能与内容挤在同一行，
        # 下一个独立成行的【…】标记会一直退到正文中段（『【本章声口总表】』）——
        # 截到那里会把章标题连带剥进思考段，正文从此没有标题可认
        # （实测《校霸》outline log25：整份大纲识别失败，5625 output tokens 白烧）。
        # 章节名行是正文起点的强信号，与【…】标记取更早者。
        m_ch = _CHAPTER_TITLE_LINE_M_RE.search(text, start)
        if m_ch:
            cands.append(m_ch.start())
    if not cands:
        return None
    end = min(cands)
    if end > start + _MAX_SECTION_CHARS:
        return None  # 距离过长：边界判断不可靠，宁可不截
    return end


def _bare_think_end(text: str, start: int) -> int | None:
    """裸行思考段的结束边界（2026-11-21）：【…】标记行 / 裸章标题行 / --- 分隔线，
    取最早者，拿不准返回 None。

    只服务裸行思考段头（_BARE_THINK_HEAD_RE）：该形态的思考段里常没有任何
    【…】标记或标题行可认，唯一可靠边界是「思考段收尾的 --- 分隔线」。
    3000 字距离守卫与通用形态一致——超长宁可不剥。
    """
    cands: list[int] = []
    m = _BRACKET_HEAD_RE.search(text, start)
    if m:
        cands.append(m.start())
    m = _CHAPTER_TITLE_LINE_M_RE.search(text, start)
    if m:
        cands.append(m.start())
    m = _BARE_THINK_BOUNDARY_RE.search(text, start)
    if m:
        cands.append(m.end())      # 分隔线整行吃掉（截到行尾），与标记行不同
    if not cands:
        return None
    end = min(cands)
    if end > start + _MAX_SECTION_CHARS:
        return None
    return end


def strip_think_sections(text: str) -> tuple[str, str]:
    """剥离「思考环节 … 正式输出」分段 → (正文, 思考内容)。

    两步：
    1. 找到「正式输出」锚点行 → 只要它之后的内容（覆盖绝大多数情况）；
    2. 没有锚点 → 找全文前 30% 的思考段头（Markdown 标题行或【…】独立行），
       截到下一个同级/更高级标题；边界拿不准则不动。

    拿不准、或剥完没内容 → 原样返回（绝不丢正文）。
    """
    text = text or ""
    if not text.strip():
        return text, ""
    # 1) 正式输出锚点优先
    anchor = _formal_anchor(text)
    if anchor is not None:
        rest = text[anchor:].strip()
        if rest:
            return rest, text[:anchor].strip()
    # 2) 无锚点：按思考段头截断
    zone = _THINK_HEAD_ZONE * len(text)
    for pattern in (_MD_THINK_HEAD_RE, _BRACKET_THINK_HEAD_RE):
        for m in pattern.finditer(text):
            if m.start() > zone:
                break
            if not _line_is_head_like(m.group(0)):
                continue
            end = _think_head_end(text, m)
            if end is None or end <= m.start():
                continue
            rest = text[end:].strip()
            if not rest:
                continue
            return rest, text[:end].strip()
    # 3) 裸行思考段头（2026-11-21《小偷入室》log24）：协议思考词独立成行、无任何
    #    包装。边界比通用形态宽一档：该形态下思考段里常没有任何【…】/标题可认，
    #    常以一条 --- 分隔线收尾，把分隔线也当边界候选（_bare_think_end 取最早）。
    for m in _BARE_THINK_HEAD_RE.finditer(text):
        if m.start() > zone:
            break
        end = _bare_think_end(text, m.end())
        if end is None or end <= m.start():
            continue
        rest = text[end:].strip()
        if not rest:
            continue
        return rest, text[:end].strip()
    return text, ""


_INLINE_MIN_TOTAL = 400         # 输出太短不值得动（也避免误伤短文本）
_INLINE_MIN_PREAMBLE = 60       # 前言太短（一句客套话）不动
_INLINE_MIN_BODY_CHARS = 120    # 剥完剩下的正文字符下限
_INLINE_MIN_BODY_RATIO = 0.25   # 剥完剩下的正文占原文比例下限
_INLINE_MIN_BLOCK = 200         # 「连续正文块」达标长度（低于此不算交付正文）

# 整行「英文加粗小标题」——无标记思考的结构指纹。
# 真实形态（2026-09-11 《毛贼风波》prose log 53/54/55，gemini 系渠道）：
# 模型的思考是「**English Section Title** + 中文说明段」交替排版，
# 中文小说正文里绝不会出现整行 `**Analyzing Scene Escalation**`。
_EN_BOLD_HEAD_RE = re.compile(
    r"^[ \t]*\*{1,2}[ \t]*[A-Z][A-Za-z0-9 ,'\"\-:;!?/]{2,70}[ \t]*\*{1,2}[ \t]*$")
# 独占一行的破甲正文容器标签：<content> / </content>（破甲预设用来包裹 payload，
# 模型回显时它紧贴正文起点，是实测最可靠的「正文从这里开始」信号）。
# re.M：模型常把 `<content>` 与章名行写成相邻两行、同属一个「段」，
# 必须在段内逐行匹配（2026-09-11）。
_CONTENT_LINE_RE = re.compile(
    r"^[ \t]*<\s*/?\s*content(?:\s[^>]*)?\s*>[ \t]*$", re.I | re.M)


def _paragraph_spans(text: str) -> list:
    """按「连续非空行」切段落 → [(start, end)]（空行是段落分隔符）。"""
    spans: list = []
    pos = 0
    buf_start = None
    for line in text.splitlines(keepends=True):
        if line.strip():
            if buf_start is None:
                buf_start = pos
        elif buf_start is not None:
            spans.append((buf_start, pos))
            buf_start = None
        pos += len(line)
    if buf_start is not None:
        spans.append((buf_start, pos))
    return spans


def _paragraph_lang(seg: str) -> str | None:
    """段落的主导语种 → 'han' / 'latin' / None（中性：太短、混排或无文字）。

    **关键**：必须是「某语种占绝对多数」才给结论，否则返回 None。
    为什么不能用「不像外文就算前言」这种宽松口径——`---`、`# 第2章细纲`、
    `目标字数：≥9000字`、`### 块1 | block_type：过渡` 这类**结构化短行**
    既不像正文也不像外文，一旦被当前言就会整块吃掉
    （2026-09-10 全量回放实测：细纲输出前几行标题被误剥 90 余字）。

    阈值取 20 而不取 40：英文思考段的加粗小标题
    （`**Initiating Scene Development**`，约 29 个字母）也要能认出来，
    否则前言会在第一个标题处就停住、正文一个字都留不下。
    """
    han = sum(1 for _ in _HAN_CHAR_RE.finditer(seg))
    lat = sum(1 for _ in _LATIN_CHAR_RE.finditer(seg))
    if lat >= 20 and lat > han * 2:
        return "latin"
    if han >= 20 and han > lat * 2:
        return "han"
    return None


def _seg_is_marker(seg: str) -> bool:
    """段落是否整段由「英文加粗小标题」构成（无标记思考的结构指纹）。

    见 _EN_BOLD_HEAD_RE 注释：中英混杂的 planning 前言里，
    `**Initiating Scene Development**` 这类整行英文标题只出现在思考中。
    """
    lines = [ln for ln in seg.splitlines() if ln.strip()]
    return bool(lines) and all(_EN_BOLD_HEAD_RE.match(ln) for ln in lines)


def _is_body_seg(seg: str, target: str) -> bool:
    """段落能否算作「交付正文」的一块：同语种或中性（短对白/分隔线），
    且不是英文加粗小标题。"""
    return not _seg_is_marker(seg) and _paragraph_lang(seg) in (target, None)


def strip_inline_think(text: str) -> tuple[str, str]:
    """剥离**无标记**的内联思考前言 → (正文, 思考内容)。

    背景（2026-09-10 实录，《死亡之森》prose 四条）：部分模型没有思考通道
    （或预设被要求「直接在正文里思考」），于是把写作规划直接吐在正文最前，
    形态例如：

        **Initiating Scene Development**

        The task is to craft Chapter 8 … the focus shifts to Step 0 …

        **Structuring Scene Shots**

        ……

        第8章：蛇窟

        突围后的黑水林深处，……

    旧规则**全部失配**：没有 <think> 标签、没有【思考环节】、没有「正式输出」
    锚点，连中文「思考」二字都没有（`has_think_marks` 直接返回 False）→
    一个字都剥不掉，老板每次都得手删。

    判定用**语种错位**而不是词义：无标记思考的形态千变万化（加粗小标题 /
    纯段落 / 编号列表），唯一稳定的特征是它跟正文不是同一种语言。

    **2026-09-11 重写（中英混杂形态）**：第一版「从头逐段走，遇到第一个不是
    前言语种的段落即停」在**中英交替**的前言上失效——真实形态（《毛贼风波》
    prose log 53/54/55，gemini 系渠道）是

        **Initiating Chapter Composition**
        (英文规划段)
        **Composing Chapter 3, "Peel"**
        (中文思考段)
        **Analyzing Initial Scene's Action**
        (中文思考段)
        ……
        <content>
        第3章：剥
        (正文 9000+ 字)

    第二段就是中文 → 「遇阻即停」当场停下，13941 字的输出只剥掉 386 字，
    其余思考全部混进正文落库。改为**找正文块**：

    1. 目标语种取末尾 25%（至少 300 字）——交付物的语言看尾巴；判不出时用
       全篇占比兜底；
    2. 逐段标注「可算正文」（同语种或中性短段，且不是整行英文加粗小标题）；
    3. 第一个**连续累计 ≥ `_INLINE_MIN_BLOCK`** 的正文块即交付起点；独占一行
       的 `<content>`（破甲正文容器）若满足该条件则优先采用，最精确；
    4. 校验：前言 ≥ 60 字、剩余正文 ≥ 120 字且 ≥ 原文 25%；
    5. 任何一条不满足 → 原样返回（绝不丢正文）。

    「块」而非「段」是本次的关键：正文由大量短段组成（对白、拟声词、分隔），
    单段门槛会误判；而思考段无论中英都被英文标题/异语种段**切断**，累计都
    上不去（log 55：正文块 9000+ 字 vs 思考里最长的中文段 93 字）。

    已知边界：**同语种**的无标记思考（中文模型用中文写前言、正文也是中文且
    中间没有异语种段）抓不住——那条只能靠【思考环节】/【正式输出】这类显式
    标记；本规则不会为此冒险。
    """
    text = text or ""
    total = len(text)
    if total < _INLINE_MIN_TOTAL:
        return text, ""
    target = _dominant_lang(text[max(0, total - max(300, total // 4)):])
    if target is None:
        # 兜底：尾巴判不出（前言占比极大时尾巴会被思考污染）→ 看全篇占比
        target = _dominant_lang(text, 300)
    if target is None:
        return text, ""  # 混排或分量不足：拿不准，不动
    spans = _paragraph_spans(text)
    if not spans:
        return text, ""
    flags = [_is_body_seg(text[s:e], target) for s, e in spans]
    # blk[i] = 从第 i 段起的连续正文块累计长度（右→左一遍算完，避免 O(n²)）
    blk = [0] * (len(spans) + 1)
    for i in range(len(spans) - 1, -1, -1):
        blk[i] = (spans[i][1] - spans[i][0]) + blk[i + 1] if flags[i] else 0

    body_start = None
    # ① 破甲正文容器 <content> 独占一行（实测最可靠的正文起点；常在段内与章名行相邻）
    for i, (s, e) in enumerate(spans):
        m = _CONTENT_LINE_RE.search(text[s:e])
        if m is not None and blk[i] >= _INLINE_MIN_BLOCK:
            body_start = s + m.start()
            break
    # ② 第一个连续正文块达标的段落起点
    if body_start is None:
        for i, (s, e) in enumerate(spans):
            if flags[i] and blk[i] >= _INLINE_MIN_BLOCK:
                body_start = s
                break
    if not body_start:
        # None：整篇没有正文块（全是思考 / 目标语种判错）→ 不动
        # 0：第一段就是正文（没有前言）→ 不动
        return text, ""
    preamble = text[:body_start]
    if len(preamble.strip()) < _INLINE_MIN_PREAMBLE:
        return text, ""
    rest = text[body_start:]
    if (len(rest.strip()) < _INLINE_MIN_BODY_CHARS
            or len(rest) < total * _INLINE_MIN_BODY_RATIO):
        return text, ""
    return rest.strip(), preamble.strip()


def strip_model_output(text: str, detect_no_body: bool = False) -> tuple[str, str, dict]:
    """统一清洗模型输出 → (正文, 思考内容, info)。

    info 键：``tags``（剥过内联标签）、``sections``（剥过思考段）、
    ``inline``（剥过无标记内联思考前言）、``incomplete``（标签未闭合）、
    ``failed``（异常兜底，已回退原文）、``no_body``（整篇只有思考，无正文）。

    顺序：先剥内联标签，再剥思考段，最后剥无标记前言（草稿剥离由调用方在之前完成）。
    安全兜底：原文 > 500 字且清洗后不足 15% → 判定异常，回退原文。

    ``detect_no_body``（2026-09-11 新增，默认关闭）：调用方确认「本阶段必须有
    中文正文」时（prose/rewrite）才开启——模型整篇只输出思考、一个正文字都没写
    时（《毛贼风波》log 53/54：汉字仅占 2%，prose 输出 5735 字全是英文 planning），
    返回空正文 + 全文进思考面板 + ``no_body=True``，交给完成标志闸门判失败重试，
    而不是把一屏思考当正文落库。默认关闭 → 既有调用方行为完全不变。
    """
    text = text or ""
    info = {"tags": False, "sections": False, "inline": False,
            "incomplete": False, "failed": False, "no_body": False}
    if not has_think_marks(text):
        return text, "", info
    work, removed_tags, incomplete = strip_think_tags(text)
    if removed_tags:
        info["tags"] = True
        info["incomplete"] = incomplete
    work, removed_sec = strip_think_sections(work)
    if removed_sec:
        info["sections"] = True
    work, removed_inline = strip_inline_think(work)
    if removed_inline:
        info["inline"] = True
    removed = "\n".join(x for x in (removed_tags, removed_sec, removed_inline)
                        if x.strip())
    # 整篇都是思考（模型没写正文）：剥完仍是 planning 特征 → 不产出「正文」。
    # 必须放在 removed 判空之前：log 54 一个字都没剥掉，removed 为空但结果同样是
    # 纯思考，不能直接 return 原文。
    if detect_no_body and _looks_like_all_think(text, work):
        info["no_body"] = True
        return "", text, info   # 整篇都是思考：全文进思考面板（一个字都不丢）
    if not removed:
        return text, "", info
    if (len(text) > _SAFE_MIN_LEN
            and len(work.strip()) < len(text) * _SAFE_MIN_RATIO):
        info["failed"] = True
        return text, "", info  # 宁可脏，不可丢正文
    return work, removed, info


def _looks_like_all_think(text: str, work: str) -> bool:
    """清洗后的产物是否根本不是「交付正文」（几乎不含汉字）。

    这是中文创作工具，prose/rewrite 的正文不可能几乎没汉字：
    - 剥完只剩一屏英文 planning（log 53）→ 汉字 < 50 即判定；
    - 一个字都没剥掉但整篇是英文加粗小标题 + 英文段落（log 54）→ 汉字占比
      < 5% 且含 ≥2 个标题行即判定。

    两条件都极其保守（正常中文正文差着两三个数量级），只在调用方显式开启
    ``detect_no_body`` 时生效。
    """
    if not work.strip():
        return True
    han = sum(1 for _ in _HAN_CHAR_RE.finditer(work))
    if han < 50:
        # 例外（2026-10-09）：模型按协议打了环节标记（正文输出/滚动状态包）却
        # 只产出几十字——这是「正文过短」不是「整篇思考」，交回调用方按字数
        # 门槛拒收（报错文案从「输出为空」恢复为「过短」，重试行为不变）。
        if re.search(r"正文输出|正式输出|滚动状态包|改写批包", text):
            return False
        return True
    if han < len(text) * 0.05:
        markers = sum(1 for s, e in _paragraph_spans(text)
                      if _seg_is_marker(text[s:e]))
        if markers >= 2:
            return True
    return False


# ---------- 正文残留清洗（破甲回显标签 / 说书人开场白 / 章节名行） ----------
#
# 只对创作文本角色（prose/rewrite）使用。三条历史漏网（2026-09-05 用户报告）：
# ① 破甲预设用 <content>…</content> 包裹 payload，模型输出时回显标签；
# ② 狐神/重型K3 破甲要求模型以「墨开，纸张铺开，今夜的故事从此处开始：」开头，
#    该句不是思考段头，思考剥离后残留在正文最前；
# ③ 章节名行时进时不进正文（各解析路径行为不一）。

# <content> 开/闭标签（含属性与大小写变体）；不会误伤 <contentful> 之类
_CONTENT_TAG_RE = re.compile(r"<\s*/?\s*content(?:\s[^>]*)?\s*>", re.I)

# 整行只有一个（或连排几个）伪标签：模型回显破甲结构（<|SYSTEM|>、<stage_…> 等）
_TAG_ONLY_LINE_RE = re.compile(r"^\s*(?:<[^<>\n]{1,80}>\s*)+$", re.M)

# 正文开头的章节名行：第一章 / 第12回 / **第三章：入城**（裸行；# 开头的
# Markdown 标题由解析器识别处理，这里不碰）。[章回] 后面不允许直接接汉字，
# 避免误伤「第五回合结束」这类叙述句
_CHAPTER_TITLE_LINE_RE = re.compile(
    r"^[ \t]{0,3}(?![#])[*＊]{0,2}[ \t]*"
    r"第[0-9〇零一二两三四五六七八九十百千万]+[章回](?![\u4e00-\u9fff])")
# 同款多行版（strip_think_sections 的截断边界扫描在整篇文本上找章节名行；
# 上面那份只配 _is_chapter_title_line 的逐行调用，^ 不跨行）
_CHAPTER_TITLE_LINE_M_RE = re.compile(_CHAPTER_TITLE_LINE_RE.pattern, re.M)

# 对话/引语开头的下一行（冒号引导行若紧跟引语，是正常行文，不能剥）
_QUOTE_START_RE = re.compile(r"[「『\"“'‘〔〖（(]")

# 冒号引导行的长度与句读限制（引导行短、无句末标点）
_LEAD_LINE_MAX = 40
_SENTENCE_END_RE = re.compile(r"[。！？!?…；;]")

# 已知破甲开场白锚点（长度超限也剥）：狐神/重型K3 破甲的「思考起始」句尾
_KNOWN_PROSE_LEAD_RE = re.compile(r"今夜的故事从此处开始")

# 「思考起始」锚点后、正文起点前的思考区最长容忍距离（超窗宁可不剥）
_ANCHOR_THINK_WINDOW = 6000
# 该预设的思考行格式：「Step 0: …」/「步骤一：…」，第二轮实录还有
# 「Step 1 画面预览：」（数字后是空格+标题文字，末尾才带冒号）——数字后
# 跟分隔符、空格或行尾都认（正文里这个格式几乎只出现在破甲规划语境）。
_STEP_LINE_RE = re.compile(
    r"^\s*(?:Step|STEP|step|步骤)\s*[0-9０-９一二三四五六七八九十]+"
    r"(?:\s*[:：.、]|\s|$)")
# Markdown 标题行（正文起点标记之一：# 行归解析器管，这里只当思考区边界用）
_MD_HEAD_LINE_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*\S")


def _is_chapter_title_line(line: str) -> bool:
    """首个非空行是否像章节名行（第一章 / 第X回…）。"""
    return bool(_CHAPTER_TITLE_LINE_RE.match(line))


def _is_prose_lead_line(line: str) -> bool:
    """首个非空行是否像破甲「说书人开场白」引导行（以冒号结尾的短行）。

    两种情况不剥：含已知锚点之外超长的行（拿不准）；下一行以引号开头
    （「林晚只留下一句话：⏎“我明天走。”」是正常行文）。
    """
    core = line.strip().strip("*＊").strip()
    if not core.endswith(("：", ":")):
        return False
    if _KNOWN_PROSE_LEAD_RE.search(core):
        return True
    body = core.rstrip("：:").strip()
    if not body or len(body) > _LEAD_LINE_MAX or _SENTENCE_END_RE.search(body):
        return False
    return True


# ---------- 场景头与正文标记行（2026-09-26） ----------
# RP 预设常让模型在正文最前输出一行「日期-星期-时间-地点-天气」场景头，如
# 『天宝十四年-七月十六-星期三–14,30-铁掌门演武场-晴烈』，再跟章节名行和
# 一行「正文」标记。这些不是正文：剥掉后由调用方把场景头原文交给记忆系统
# （store.set_scene_note → 归档提取喂 world_state.time/location）。
# 判定必须保守（宁可不剥）：整行成对『』/【】包裹（或「时间：…地点：…」
# 标签形式）、无句末标点、长度受限、多字段分隔——对白『你要去哪？』、
# 『走-走-走』这类都命中不了。
_SCENE_HEADER_BRACKETS = (("『", "』"), ("【", "】"))
_SCENE_HEADER_MAX_INNER = 60
_SCENE_HEADER_SEPS = "-–—‐‑―|｜/／"
_SCENE_LABEL_HEADS_RE = re.compile(r"(?:时间|日期|地点|天气|场景)\s*[:：]")
# 正文标记行：模型在写正文前打的一行「正文」标签（可带 *、#、【】、~、括号等装饰）
_BODY_MARKER_LINE_RE = re.compile(
    r"^[\s*＊#＃~～\-—–【】\[\]〔〕（）()]*正文\s*[:：]?"
    r"[\s*＊#＃~～\-—–【】\[\]〔〕（）()]*$")


# 整行被成对伪标签包裹（<Time>…</Time> / <场景>…</场景>）：RP 预设常把场景头
# 套在 <Time> 标签里。实测《极限体能》ch3 输出首行
# 『<Time>『2024年-10月19日-星期六–17:50-市郊廉租公寓-暮色』</Time>』——旧判定
# 只认内层『』/【】，标签挡在开头 → 该行不剥、其后紧邻的章名行也剥不掉
# （_strip_leading_prose_junk 只剥「开头连续」junk）。2026-10-10 扩。
_WRAPPED_TAG_LINE_RE = re.compile(
    r"^<([A-Za-z][A-Za-z0-9_\-:]{0,20})>\s*(.+?)\s*</\1>\s*$", re.S)


def _unwrap_tag_line(line: str) -> tuple[str, bool]:
    """剥掉整行的成对伪标签包裹 → (内部文本, 是否剥过)。"""
    core = (line or "").strip()
    m = _WRAPPED_TAG_LINE_RE.match(core)
    if m:
        return m.group(2).strip(), True
    return core, False


def _scene_header_inner(line: str) -> str:
    """整行被『』/【】包裹时返回内部文本，否则返回空串。"""
    core = line.strip().strip("*＊").strip()
    for open_b, close_b in _SCENE_HEADER_BRACKETS:
        if len(core) >= 2 and core.startswith(open_b) and core.endswith(close_b):
            return core[1:-1].strip()
    return ""


def _is_scene_header_line(line: str) -> bool:
    """该行是否像场景头（日期-星期-时间-地点-天气 一类的多字段行）。

    形态：
    1. 整行『…』/【…】包裹（**可再被 <Time> 等伪标签包一层**，2026-10-10 扩），
       内部按分隔符切 ≥3 段且至少 2 段不同（日期-星期-时间-地点-天气；
       『走-走-走』式重复短对白不会命中）；
    2. 被伪标签整行包裹的多字段串（无内层括号，如 <Time>2024-10-19-…-晴</Time>）；
    3. 短行内出现 ≥2 个「时间：/日期：/地点：/天气：/场景：」标签。
    句末标点（。！？…）一票否决——场景头是字段串不是句子，对白几乎必带标点。
    """
    body, wrapped = _unwrap_tag_line(line)
    if not body or len(body) > _SCENE_HEADER_MAX_INNER + 2:
        return False
    if _SENTENCE_END_RE.search(body):
        return False
    inner = _scene_header_inner(body)
    if inner:
        segs = [s for s in re.split("[" + re.escape(_SCENE_HEADER_SEPS) + "]",
                                    inner) if s.strip()]
        return len(segs) >= 3 and len(set(segs)) >= 2
    if wrapped:
        # 标签包裹但无内层括号：只有「伪标签包裹」这个强信号才敢按分隔符判，
        # 裸行同形态（『他突然想起-昨天-的事』）不在此路，避免误伤正文行文。
        segs = [s for s in re.split("[" + re.escape(_SCENE_HEADER_SEPS) + "]",
                                    body) if s.strip()]
        return len(segs) >= 3 and len(set(segs)) >= 2
    return len(set(_SCENE_LABEL_HEADS_RE.findall(body))) >= 2


def _is_body_marker_line(line: str) -> bool:
    """该行是否是「正文」标记行（模型打的正文开始标签，不是内容）。"""
    return bool(_BODY_MARKER_LINE_RE.match(line.strip()))


def _strip_leading_prose_junk(text: str, scene_captured: list[str] | None = None,
                              strip_title: bool = True, strip_lead: bool = True) -> str:
    """从开头连续剥「场景头 / 章节名行 / 正文标记行 / 冒号引导行」（幂等）。

    逐行判定、逐行剥，最多 8 行；全文只剩这些行时停手（绝不丢正文）。
    scene_captured 非 None 时收集被剥掉的场景头原始行（交给记忆系统）。
    strip_title/strip_lead=False 用于手动导入逐章清洗——章节是从标题行切出来的，
    正文里残留的标题行可能是切分漏掉的下一章边界，剥掉会静默并章；
    冒号引导行（『他说：』类）在手动正文里有误剥风险（见 _clean_manual_prose）。
    """
    for _ in range(8):
        stripped = text.lstrip()
        nl = stripped.find("\n")
        head = (stripped if nl < 0 else stripped[:nl]).strip()
        if not head:
            break
        if (_is_scene_header_line(head) or _is_body_marker_line(head)
                or (strip_title and _is_chapter_title_line(head))):
            rest = "" if nl < 0 else stripped[nl + 1:]
            if not rest.strip():
                break
            if scene_captured is not None and _is_scene_header_line(head):
                scene_captured.append(head)
            text = rest.strip()
            continue
        if strip_lead and _is_prose_lead_line(head):
            new = _strip_lead_line_quote_aware(text)
            if new == text:
                break
            text = new
            continue
        break
    return text


def strip_leading_scene_marks(text: str, scene_captured: list[str] | None = None) -> str:
    """只剥开头的场景头/正文标记行（手动导入逐章清洗专用）。

    不碰章节名行与冒号引导行——原因见 `_strip_leading_prose_junk` 的
    strip_title/strip_lead 注释。幂等；场景头原文可被 scene_captured 收集。
    """
    return _strip_leading_prose_junk(text, scene_captured,
                                     strip_title=False, strip_lead=False)


def strip_jailbreak_residue(text: str) -> str:
    """破甲残留清洗（**全阶段通用**）：<content>/伪标签行 + 开场白引导的思考块。

    只处理破甲预设带来的回显与规划段，**不碰章节名行/冒号引导行**——
    结构化阶段（大纲/细纲/分卷/总纲）里 `# 第N章` 之类是有用数据，
    只有创作文本（prose/rewrite）才需要连章节名行、冒号引导行一起剥，
    见 strip_prose_residue。

    幂等；全文只剩残渣时回退原文（绝不丢正文）。
    """
    text = text or ""
    original = text
    if _CONTENT_TAG_RE.search(text):
        text = _CONTENT_TAG_RE.sub("", text)
    if _TAG_ONLY_LINE_RE.search(text):
        text = _TAG_ONLY_LINE_RE.sub("", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
    text = _strip_anchor_think_block(text)
    text = _strip_step_plan_block(text)
    if original.strip() and not text.strip():
        return original
    return text


def strip_stray_scene_lines(text: str) -> tuple[str, int]:
    """全文剥离「独立成行的场景头」（含 <Time> 伪标签包装变体）→ (文本, 处数)。

    背景（2026-10-10 老板反馈「分批写作时正文里总出现时间戳」）：分批续批时模型
    习惯在**场景切换处**（正文中段，不在章首）再打一行场景头，实测《校霸》
    ch4/ch5/ch7：
        ……背着沉重的枷锁，硬生生熬穿了漫长的黑夜。
        『2012-10月26日-星期六–06:15-青川市中心涂玫高档公寓-晴』   ← 中段场景头
        晨曦把窗帘缝隙染成灰白的时候，房门再次被推开。
    开头的场景头由 `_strip_leading_prose_junk` 收走，**中段的全部漏进正文落库**
    （并随初稿喂给改写模型，被照抄成 `P185：『…』`）。

    判定复用 `_is_scene_header_line`（严格：成对『』/【】或多字段标签、去分隔符
    ≥3 段且 ≥2 段不同、无句末标点、长度受限度；正文对白『你要去哪？』、
    重复短句『走-走-走』、单字段『天啊』都不会命中）。删除只针对**整行**，
    正文行一个字不动；删完只剩这些行时回退原文（绝不丢正文）。幂等。
    """
    raw = (text or "")
    if not raw.strip():
        return raw, 0
    lines = raw.split("\n")
    out: list[str] = []
    removed = 0
    for ln in lines:
        if ln.strip() and _is_scene_header_line(ln):
            removed += 1
            continue
        out.append(ln)
    if not removed:
        return raw, 0
    new = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    if not new:
        return raw, 0          # 全是场景头 → 回退原文（绝不把正文剥成空）
    return new, removed


def strip_prose_residue(text: str, scene_captured: list[str] | None = None) -> str:
    """正文终清洗（幂等）：剥破甲回显标签、思考起始块、开场白引导行、章节名行、
    场景头行、正文标记行。

    - <content>/</content>（含属性变体）全局删除，正文内容保留；
    - 整行仅由伪标签构成的行删除；
    - 已知破甲锚点（今夜的故事从此处开始）到第一个正文起点之间的整块思考剥离；
    - 开头的场景头（『日期-星期-时间-地点-天气』）、章节名行、正文标记行、
      冒号引导行逐行剥离（开头连续最多 8 行）；
      scene_captured 非 None 时收集被剥的场景头原文（2026-09-26，交记忆系统）；
    - **全文**再扫一遍独立成行的场景头（含 <Time> 包装）——分批续批时模型会在
      场景切换处（正文中段）重复打场景头，只剥开头会漏（2026-10-10）。

    拿不准一律不剥；全文只剩残渣时回退原文（绝不丢正文）。
    """
    text = text or ""
    original = text
    text = strip_jailbreak_residue(text)
    text = _strip_leading_prose_junk(text, scene_captured)
    text, _n = strip_stray_scene_lines(text)
    if original.strip() and not text.strip():
        return original
    return text


def _strip_anchor_think_block(text: str) -> str:
    """「思考起始」锚点引导的整块思考剥离（幂等，绝不丢正文）。

    狐神/重型K3 破甲要求模型以「墨开，纸张铺开，今夜的故事从此处开始：」作为
    思考起始，其后常跟 Step N: 式思考行，再经 <content> 或章节名行才进入正文。
    真实漏网样例：锚点行 → Step 0~6 思考 → <content> → 第3章：xxx → 正文。

    2026-09-05 第二轮：Step N 行之间夹着的是**整段**思考内容（写作规划、画面
    预览、篇幅节奏…），且思考段后可能**没有**任何标志直接进正文（首行是引号
    对话，如「“啪。啪。啪。”」）——正文起点识别加入「引号开头段落」，
    窗口放宽到 6000（Step 段总长常超 3000）。主逻辑只在标志行处整块剥，
    兜底只剥单行，两种路径都不会丢正文。

    规则：锚点行到第一个正文起点（章节名行 / Markdown 标题 / <content> 标签 /
    引号开头段落）之间的内容整块按思考剥掉；窗口内没有正文起点时只剥锚点行
    和紧随的连续 Step 式思考行（锚点后直接是正文 → 等价于只剥锚点行）。
    """
    lines = text.splitlines(keepends=True)
    idx = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if idx is None or not _KNOWN_PROSE_LEAD_RE.search(lines[idx]):
        return text
    consumed = 0
    for i in range(idx + 1, len(lines)):
        line = lines[i]
        stripped = line.strip()
        if stripped and (_CHAPTER_TITLE_LINE_RE.match(stripped)
                         or _MD_HEAD_LINE_RE.match(stripped)
                         or _CONTENT_TAG_RE.match(stripped)
                         or _QUOTE_START_RE.match(stripped)):
            return "".join(lines[:idx]) + "".join(lines[i:])
        consumed += len(line)
        if consumed > _ANCHOR_THINK_WINDOW:
            break
    # 窗口内没有正文起点：只剥锚点行 + 紧随的连续 Step 式思考行
    j = idx + 1
    while j < len(lines) and (not lines[j].strip() or _STEP_LINE_RE.match(lines[j])):
        j += 1
    return "".join(lines[:idx]) + "".join(lines[j:])


def _strip_step_plan_block(text: str) -> str:
    """Step 写作规划段剥离（残缺形态兜底，幂等，绝不丢正文）。

    18 章实录：正文在**上一版清洗**时锚点行和「Step 0」行已被剥掉，落库内容
    开头直接是规划内容（「本章整体呈现由动转静…」+ Step 1~6 整段，Step 0 的
    热段/冷段列举可能长达数百字），锚点入口认不出。规则：全文出现 ≥2 个
    Step 行、且第一个正文起点（章节名/MD 标题/<content>/引号段落）位于
    **最后一个 Step 行之后**、总长在窗口内 → 从开头整块剥到正文起点；
    任一条件不满足一律不动。正常小说正文几乎不会出现「Step N：」格式的行，
    误伤风险极低。
    """
    lines = text.splitlines(keepends=True)
    idx = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if idx is None:
        return text
    step_lines = [i for i in range(idx, len(lines))
                  if _STEP_LINE_RE.match(lines[i].strip())]
    if len(step_lines) < 2:
        return text
    consumed = 0
    for i in range(idx, len(lines)):
        line = lines[i]
        stripped = line.strip()
        if i > idx and stripped and (_CHAPTER_TITLE_LINE_RE.match(stripped)
                                     or _MD_HEAD_LINE_RE.match(stripped)
                                     or _CONTENT_TAG_RE.match(stripped)
                                     or _QUOTE_START_RE.match(stripped)):
            if i > step_lines[-1] and consumed <= _ANCHOR_THINK_WINDOW:
                return "".join(lines[:idx]) + "".join(lines[i:])
            return text
        consumed += len(line)
        if consumed > _ANCHOR_THINK_WINDOW:
            return text
    return text


def _strip_lead_line_quote_aware(text: str) -> str:
    """剥冒号引导行，但下一行以引号开头（对话引入）时保留。"""
    stripped = text.lstrip()
    nl = stripped.find("\n")
    head = (stripped if nl < 0 else stripped[:nl]).strip()
    if not head or not _is_prose_lead_line(head):
        return text
    rest_raw = "" if nl < 0 else stripped[nl + 1:]
    # 下一非空行以引号开头 → 是正常对话行文，不剥
    for line in rest_raw.splitlines():
        if line.strip():
            if _QUOTE_START_RE.match(line.strip()):
                return text
            break
    if not rest_raw.strip():
        return text
    return rest_raw.strip()
