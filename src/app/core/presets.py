# -*- coding: utf-8 -*-
"""预设系统：预设管理 + 酒馆（SillyTavern）预设导入器。

预设 = 一组条目（名称/内容/启用/角色/注入位置/优先级/适用范围/备注）
      + 采样参数（温度/top_p 等）+ 条目顺序。
导入器分类映射表可编辑（全局设置键 tavern_rules，跨项目共享）。
"""
import json
import re

from .store import ProjectStore, jdump, jload

# ============================================================================
# 破限识别（2026-09-10 重做）
# ============================================================================
# 旧版只有一条「命中即类」的简体正则，实测在真实预设上：
#   漏判 5/9（繁体预设全军覆没——全简体正则匹配不到 拒絕/視為/迴避/無審查）
#   误判 7/8（"任何内容""任何情节""无条件""freedom""obey""explicit" 等泛词
#             在普通文风/结构/角色条目里遍地都是，命中即被当作破限）
# 误判比漏判更致命：被误判的条目会「只导入破限」时被勾选 → AI 识别阶段被当作
# 已确认破限直接跳过 → 提取阶段喂全文 → 上下文被文风内容淹没，提取结果不可用。
#
# 现改为「句式评分」：破限是句式特征而非单词特征。强信号 +3、内容豁免 +2、
# 允许+敏感组合语境 +3、否定语境（避免/禁止写敏感内容）−3；达阈值才判破限类。
# 关键设计：只保留"语境完整"的模式，任何单词都能单独出现的泛词一律剔除。
JB_SCORE_THRESHOLD = 3          # 判破限类的最低分

# --- 强信号（+3）：明确要求放弃内容限制 / 不得拒绝 / 让渡判定权 ---
_JB_STRONG: list[str] = [
    # 不得拒绝 / 不拒答 / 不会回避（繁简同时覆盖）
    # 刻意不含「中断/停止」——"禁止中断输出"是防截断/输出协议，不是内容破限（实测误判源）
    r"(?:不得|不應|不应|不該|不该|不能|不會|不会|永不|絕不|绝不|不許|不许|不准|禁止|嚴禁|严禁)"
    r"[^。\n]{0,4}(?:拒絕|拒绝|拒答|迴避|回避|推託|推脱)",
    r"(?:拒絕|拒绝|迴避|回避)[^。\n]{0,8}(?:任何|一切|所有)[^。\n]{0,6}"
    r"(?:要求|請求|请求|輸入|输入|輸出|输出|指令)",
    r"不拒答|永不拒答|絕不拒答",
    # 以审查/安全政策为由不得拒绝
    r"(?:安全政策|安全政策|內容政策|内容政策|審查|审查|道德|倫理|伦理|規矩|规矩)"
    r"[^。\n]{0,10}(?:为由|為由|理由)[^。\n]{0,6}(?:拒絕|拒绝|拒答)",
    # 英文：不得拒绝
    r"(?:never|not|no|must\s*not|mustn'?t|won'?t|will\s*not|do\s*not|don'?t)\s*"
    r"(?:to\s*)?(?:refus|reject|declin)\w*",
    r"refuse\s+(?:any|all|every)",
    # 解除 / 无视限制
    r"(?:解除|取消|廢除|废除|移除|放開|放开|突破|擺脫|摆脱|破除|斩断|斬斷)[^。\n]{0,10}"
    r"(?:限制|約束|约束|束縛|束缚|審查|审查|規則|规则|設定|设定|道德|倫理|伦理|邊界|边界)",
    r"(?:無視|无视|忽略|忽視|忽视|蔑視|轻视)[^。\n]{0,10}"
    r"(?:限制|規則|规则|道德|倫理|伦理|審查|审查|設定|设定|指令|提示|要求|政策|規範|规范|以上|之前)",
    r"不設限|不设限|無限制|无限制|不受限制|不受約束|不受约束|不受任何限制",
    r"(?:ignore|disregard|forget|bypass|override)[^.\n]{0,30}"
    r"(?:restriction|filter|limitation|rule|policy|guideline|instruction|prompt|ethic|moral)\w*",
    r"no\s+(?:restrictions?|limits?|filters?|boundaries)",
    # 身份替换（不再是被对齐的助手）
    r"(?:你不是|你不再是|你不再|不要再扮演|不再扮演)[^。\n]{0,8}"
    r"(?:AI|ai|人工智能|助手|語言模型|语言模型|機器人|机器人|模型)",
    r"\b(?:no\s+longer|not)\s+(?:an?\s+)?(?:AI|assistant|language\s+model|chatbot)\b",
    r"越獄|越狱|破限|破甲|jailbreak",
    # 让渡判定权 / 无条件服从（必须带宾语或明确动词，避免"必须遵守世界法则"误判）
    r"(?:無條件|无条件)[^。\n]{0,4}"
    r"(?:服從|服从|輸出|输出|接受|滿足|满足|配合|遵守|完成|執行|执行)",
    r"(?:必須|必须|務必|务必|絕對|绝对|完全)[^。\n]{0,4}"
    r"(?:服從|服从|聽從|听从|執行|执行|遵守)[^。\n]{0,6}"
    r"(?:所有|一切|任何|用戶|用户|指令|要求|命令)",
    r"unconditionally\s+(?:obey|comply|follow)",
    r"\bmust\s+(?:always\s+)?(?:obey|comply|follow|execute)\b",
    r"obey\s+(?:all|any|every|user)",
    # 审查豁免：视为非敏感 / 不存在审查
    r"(?:視為|视为|視作|视作|當作|当作|看作|定為|定为)[^。\n]{0,6}"
    r"(?:非敏感|正常|普通|合法|無害|无害|允許|允许)",
    r"(?:不存在|沒有|没有|無|无|不設|不设|取消)[^。\n]{0,4}"
    r"(?:審查|审查|過濾|过滤|限制|安全政策|內容政策|内容政策)",
    r"uncensored|unfiltered|no\s+censorship|without\s+(?:any\s+)?censorship",
    r"treat[^.\n]{0,20}as[^.\n]{0,12}(?:non[-\s]?sensitive|not\s+sensitive|safe|normal)",
]

# --- 中信号（+2）：内容豁免 / 尺度放开（单独出现不足以判破限，需组合）---
_JB_MED: list[str] = [
    r"nsfw|NSFW|r18|R18|18\+|adult\s+content|explicit\s+content|mature\s+content|smut",
    r"(?:無審查|无审查|取消審查|取消审查|無過濾|无过滤|不加限制|不加以限制|不加審查|不加审查)",
    r"(?:色情|情色|成人向|成人內容|成人内容|澀澀|涩涩|肉文|露骨|性描寫|性描写"
    r"|性愛|性爱|性行為|性行为|情慾|情欲|高H|高h|文愛|文爱)",
    r"(?:敏感內容|敏感内容|敏感話題|敏感话题)",
]

# --- 组合语境（+3）：允许/必须 + 敏感内容（"允许输出色情暴力血腥"）---
# 刻意不含裸「可」「能」「自由」——"可能出现的暴力情节"这类会被误命中。
_JB_CONTEXT: list[str] = [
    r"(?:允許|允许|可以|必須|必须|應當|应当|應該|应该|不受限|無條件|无条件|放心)"
    r"[^。\n]{0,25}(?:色情|成人|R18|r18|澀澀|涩涩|露骨|暴力|血腥|敏感"
    r"|性描寫|性描写|nsfw|NSFW|情色|情慾|情欲|肉)",
]

# --- 否定语境（−3）：明确"不要写敏感内容"的克制要求（与破限语义相反）---
_JB_NEGATIVE: list[str] = [
    r"(?:避免|禁止|不得|不要|杜絕|杜绝|嚴禁|严禁|克制|減少|减少|忌諱|忌讳|避開|避开)"
    r"[^。\n]{0,3}(?:使用|描寫|描写|涉及|出現|出现|包含|輸出|输出|提及"
    r"|explicit|graphic|色情|露骨|血腥|暴力|敏感|成人|性描寫|性描写|nsfw|NSFW|r18|R18)",
]

# 命中扫描用的合并正则（build_excerpt 提取"破限要害段"用；不含否定语境）
JB_HINT_PATTERN = "|".join(_JB_STRONG + _JB_MED + _JB_CONTEXT)


def _count_hits(patterns: list[str], text: str) -> int:
    """每条模式最多计一次（避免同一句反复加分）。"""
    n = 0
    for p in patterns:
        try:
            if re.search(p, text, re.IGNORECASE):
                n += 1
        except re.error:
            continue
    return n


def jailbreak_score(text: str) -> int:
    """破限句式评分：强信号 ×3 + 内容豁免 ×2 + 组合语境 ×3 − 否定语境 ×3。

    输入建议为「条目名 + 内容」。得分 >= JB_SCORE_THRESHOLD 判为破限类。
    """
    if not text:
        return 0
    return (_count_hits(_JB_STRONG, text) * 3
            + _count_hits(_JB_MED, text) * 2
            + _count_hits(_JB_CONTEXT, text) * 3
            - _count_hits(_JB_NEGATIVE, text) * 3)


def is_jailbreak_text(text: str) -> bool:
    """是否达到破限判定阈值。"""
    return jailbreak_score(text) >= JB_SCORE_THRESHOLD


# 默认分类映射规则：(正则, 分类)。靠前优先命中，破限类必须最前。
# 破限类只放"强信号"（语境完整的句式），中/弱信号交给 jailbreak_score 组合判定——
# 否则"色情描写要细腻"这类纯尺度条目会被单词命中误分为破限。
DEFAULT_RULES = [
    ("|".join(_JB_STRONG), "破限类"),
    (r"文风|文笔|描写|行文|措辞|文采|笔锋|语言风格|写作风格|语气|口吻", "文风类"),
    (r"结构|节奏|分镜|大纲|章节|情节推进|叙事节奏|起承转合|篇幅|字数", "结构类"),
    # 尺度类：去掉裸「性」——旧版会把"性格/个性/属性"一并归入尺度
    (r"尺度|成人向|成人内容|色情|情色|性爱|性愛|性欲|性慾|性描写|性描寫|性行为|性行為"
     r"|露骨|R18|r18|文爱|文愛|高h|高H|肉文|涩涩|澀澀|H内容|色情内容|情欲|情慾", "尺度类"),
    (r"人称|视角|主角|视点|第一人称|第二人称|第三人称", "叙事类"),
    (r"你扮演|你是|角色扮演|人设|扮演|性格|角色卡", "角色类"),
    (r"禁止|不要|避免|雷区|克制|忌讳|不允许", "限制类"),
]

RULES_VERSION = 2
# v1 默认规则（用于识别"用户是否改过分类规则"：改过就保留其自定义项）
_V1_DEFAULT_RULES = [
    (r"破限|越狱|jailbreak|无限制|解除限制|放开限制|不设限|突破限制|不受限制|全面放开"
     r"|nsfw|unfiltered|uncensored|unleash|unbound|deep[\s_-]*desire|senzu|do what i want"
     r"|no[\s_-]*limits|no[\s_-]*restrictions|ignore.*(restriction|filter|policy)|无过滤|未过滤"
     r"|unlocked|unlock|无审查|取消审查|free to write|freedom|beastiality|no[\s_-]*censorship"
     r"|不存在.*审查|不会拒绝|永不拒绝|永远.*拒绝|以任何理由.*(中断|拒绝)|拒绝.*任何输入"
     r"|任何内容|任何尺度|任何情节|无条件|无条件.*(服从|输出|接受|obey)|不得拒绝|不拒答"
     r"|unconditional|never[\s_-]*refus|must not refus|refuse any|any[\s_-]*content|obey"
     r"|explicit|graphic|任何题材|题材不限|尺度不限|内容不限"
     r"|敏感内容.*非敏感|视为非敏感|反注入|无条件.*服从", "破限类"),
    (r"文风|文笔|描写|行文|措辞|文采|笔锋|语言风格", "文风类"),
    (r"结构|节奏|分镜|大纲|章|情节推进|叙事节奏|起承转合", "结构类"),
    (r"尺度|成人|性|色情|暴力|血腥|露骨|R18|r18|文爱|高h|肉文|涩涩|H内容|成人向", "尺度类"),
    (r"人称|视角|主角|视点|第一人称|第二人称|第三人称", "叙事类"),
    (r"你扮演|你是|角色扮演|人设|扮演|性格", "角色类"),
    (r"禁止|不要|避免|雷区|克制|忌讳|不允许", "限制类"),
]
_KNOWN_DEFAULT_PATTERNS = frozenset(
    [p for p, _ in _V1_DEFAULT_RULES] + [p for p, _ in DEFAULT_RULES])


def _str_bool(v, default=True) -> bool:
    """容错解析布尔：兼容 'True'/'False'/'1'/'0'/None/bool。"""
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    return default


def _safe_int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default

# 酒馆模板宏（ST）解析
# 关键点：预设的破甲是「壳层 + 变量 + 多个开关条目」协同，变量用 {{setvar/addvar}} 定义、
# 用 {{getvar}} 在别处引用。不解析宏，狐神抚这类预设的 jailbreak 壳层就是一堆空引用，
# 破甲内容根本进不了请求。这里只解析宏、把变量值注入引用点，不输出 ST 内部指令。
_ST_ALLOWED_MACROS = {"user", "char", "charIfNotGroup", "group", "personality", "scenario"}


def _find_macro_end(text: str, start: int) -> int:
    """从 text[start]=='{{' 起，用花括号深度扫描找到匹配 '}}' 之后的索引（支持嵌套）。
    未闭合返回 -1（与"宏恰好结束在文本末尾"区分开，后者返回 len(text)）。"""
    i = start + 2
    depth = 1
    n = len(text)
    while i < n:
        if text.startswith("{{", i):
            depth += 1
            i += 2
        elif text.startswith("}}", i):
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    return -1


def _resolve_st(text: str, vars_: dict, depth: int = 0) -> tuple[str, list[str]]:
    """解析单段文本中的酒馆宏（不收集变量）。返回 (解析后文本, 警告)。

    setvar/addvar/trim 视为 ST 内部指令 → 输出为空；getvar → 查变量表；
    user/char 等占位宏保留；未知宏剥离。
    """
    if "{{" not in text or depth > 8:
        return text, []
    out: list[str] = []
    warns: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("{{", i):
            end = _find_macro_end(text, i)
            if end < 0:
                # 未闭合宏：保留原样（可能是残缺内容，不强行删除）
                out.append(text[i:])
                break
            inner = text[i + 2:end - 2]
            head = inner.split("::", 1)[0].strip()
            if head in ("setvar", "addvar"):
                pass  # 指令宏，不输出
            elif head == "getvar":
                name = inner[len("getvar"):].lstrip(":").strip()
                out.append(vars_.get(name, ""))
            elif head == "trim":
                pass
            elif inner in _ST_ALLOWED_MACROS:
                out.append(text[i:end])
            else:
                # 未知宏：整体剥离（内容为 ST 专属，不适用于本项目）
                warns.append(f"{{{inner[:40]}}}")
            i = end
        else:
            k = text.find("{{", i)
            if k == -1:
                out.append(text[i:])
                break
            out.append(text[i:k])
            i = k
    return "".join(out), warns


def resolve_st_macros(entries: list[dict]) -> list[str]:
    """解析酒馆模板宏：收集启用条目的 setvar/addvar 变量 → 展开各条内容中的 getvar。

    只收集 enabled 条目的变量（ST 只执行启用的 prompt）。就地修改 entries 的 content
    （解析后为空的纯指令条目由调用方决定丢弃）。返回去重后的警告列表。
    """
    raw: dict[str, dict] = {}
    for e in entries:
        if not e.get("enabled"):
            continue
        content = e.get("content") or ""
        i = 0
        while i < len(content):
            j = content.find("{{", i)
            if j == -1:
                break
            end = _find_macro_end(content, j)
            if end < 0:
                break
            inner = content[j + 2:end - 2]
            head = inner.split("::", 1)[0].strip()
            if head in ("setvar", "addvar") and "::" in inner:
                segs = inner.split("::", 2)
                name = segs[1].strip()
                value = segs[2] if len(segs) > 2 else ""
                if name:
                    raw[name] = {"kind": head, "value": value}
            i = end
    vars_: dict[str, str] = {}
    for name, d in raw.items():
        v, _ = _resolve_st(d["value"], {}, 0)
        v = v.replace("{{trim}}", "").strip()
        if d["kind"] == "addvar":
            vars_[name] = (vars_.get(name, "") + v).strip()
        else:
            vars_[name] = v
    warns: list[str] = []
    for e in entries:
        if not e.get("content"):
            continue
        new_c, w = _resolve_st(e["content"], vars_, 0)
        e["content"] = re.sub(r"\n{3,}", "\n\n", new_c).strip()
        warns.extend(w)
    # 去重（同类未知宏只提醒一次）
    seen: set[str] = set()
    dedup: list[str] = []
    for w in warns:
        if w not in seen:
            seen.add(w)
            dedup.append(w)
    return dedup

# 酒馆人物卡字段 → (条目名, role, 支持数组)
TAVERN_FIELDS = [
    ("system_prompt", "系统提示词", "system", False),
    ("post_history_instructions", "历史后指令", "system", False),
    ("description", "角色描述", "system", False),
    ("personality", "性格设定", "system", False),
    ("scenario", "场景设定", "user", False),
    ("first_mes", "开场白", "user", False),
    ("mes_example", "对话示例", "user", False),
    ("alternate_greetings", "备用开场", "user", True),
]


class PresetService:
    """预设服务（绑定项目）。"""

    def __init__(self, store: ProjectStore):
        self.store = store

    # ---------- 分类规则（可编辑配置） ----------

    def get_rules(self) -> list[dict]:
        """分类规则（全局共享，跨项目同一套）。

        版本迁移：存储的规则若来自 v1（无版本号或版本低于 RULES_VERSION），
        说明它可能带着旧版的宽松破限正则（泛词误判源）。此时换成新默认，
        并把用户**自己加过**的规则项（不属于任何历史默认 pattern）保留在后面，
        既不丢用户自定义，也不让旧默认继续误判。
        """
        raw = self.store.get_setting("tavern_rules", "")
        rules = jload(raw, None)
        if not isinstance(rules, list) or not rules:
            return [{"pattern": p, "category": c} for p, c in DEFAULT_RULES]
        try:
            ver = int(self.store.get_setting("tavern_rules_version", "0") or "0")
        except (TypeError, ValueError):
            ver = 0
        if ver >= RULES_VERSION:
            return rules
        customs = [r for r in rules
                   if isinstance(r, dict) and r.get("pattern")
                   and r["pattern"].strip() not in _KNOWN_DEFAULT_PATTERNS]
        merged = [{"pattern": p, "category": c} for p, c in DEFAULT_RULES] + customs
        try:
            self.set_rules(merged)           # 落盘升级（下次不再重复迁移）
        except Exception:
            pass
        return merged

    def set_rules(self, rules: list[dict]):
        self.store.set_setting("tavern_rules", jdump(rules))
        self.store.set_setting("tavern_rules_version", str(RULES_VERSION))

    def reset_rules(self):
        """恢复内置分类规则（清空自定义）。"""
        self.store.set_setting("tavern_rules", "")
        self.store.set_setting("tavern_rules_version", str(RULES_VERSION))

    def classify(self, name: str, content: str) -> str:
        """条目分类：破限判定走句式评分（启发式优先），其余类别走规则表。

        破限优先于规则表：真实预设里"允许色情暴力"这类条目会同时命中尺度类，
        但它是内容豁免（破限），必须先判破限。

        规则匹配「先看条目名、再看名+内容」：预设作者给的条目名就是分类意图
        （"尺度""文风""越狱"），比内容里的泛词可靠——否则"色情描写要细腻"
        会被内容里的"描写"抢成文风类。
        """
        text = f"{name}\n{content}" if name else (content or "")
        if is_jailbreak_text(text):
            return "破限类"
        scopes = (name, text) if name else (text,)
        for scope in scopes:
            for r in self.get_rules():
                try:
                    if re.search(r["pattern"], scope, re.IGNORECASE):
                        return r["category"]
                except re.error:
                    continue
        return "其他"

    def jailbreak_score_of(self, e: dict) -> int:
        """条目的破限句式评分（识别链做先验/是否跳过 AI 复核用）。"""
        return jailbreak_score(f"{e.get('name', '')}\n{e.get('content', '')}")

    def extract_jailbreak(self, entries: list[dict]) -> list[dict]:
        """只保留破限类条目（酒馆预设中最重要的部分，其余丢弃以省上下文）。"""
        return [e for e in entries if e.get("category") == "破限类"]

    # ---------- 破甲提取辅助（AI 提取的输入构造） ----------

    def build_excerpt(self, e: dict, head: int = 600, hits: int = 8, ctx: int = 250) -> str:
        """为 AI 提取构造单条摘要：开头 + 破限关键词命中段（±ctx 上下文）+ 结尾。

        破甲要害（无条件输出/不得拒绝/任何内容…）常埋在条目中部或结尾，
        只给开头 400 字会让 AI 根本看不到 → 输出空。命中段兜底喂给 AI。
        """
        content = (e.get("content") or "").replace("\n", " ")
        short = len(content) <= head + 200
        if short:
            return content
        rules = self.get_rules()
        # 命中扫描正则 = 内置破限句式（繁简英全覆盖）+ 用户自定义规则里的破限类正则。
        # 内置放在前面：用户规则若被清空/改乱，命中段仍能正常工作（历史 0 字 bug 的根因之一）。
        user_jb = [r["pattern"] for r in rules
                   if r.get("category") == "破限类" and r.get("pattern")]
        jb_pat = "|".join([JB_HINT_PATTERN] + user_jb)
        parts: list[str] = [content[:head]]
        covered = [(0, head)]
        n_hits = 0
        try:
            for m in re.finditer(jb_pat, content, re.IGNORECASE):
                if n_hits >= hits:
                    break
                s, t = max(0, m.start() - ctx), min(len(content), m.end() + ctx)
                if any(s <= ce and cs <= t for cs, ce in covered):
                    continue  # 与已取片段重叠，跳过
                parts.append(f"…[命中·{m.group()}]…" + content[s:t])
                covered.append((s, t))
                n_hits += 1
        except re.error:
            pass
        if not n_hits and not short:
            # 无命中（关键词漏判）：补中部采样 + 结尾，比只给开头强
            mid = len(content) // 2
            parts.append("…[中部]…" + content[max(0, mid - ctx):mid + ctx])
        if not short:
            parts.append("…[结尾]…" + content[-(head // 2):])
        return "\n".join(parts)

    # ---------- 酒馆解析 ----------

    def parse_tavern(self, payload) -> tuple[list[dict], list[str]]:
        """解析酒馆预设 JSON → (条目列表, 警告列表)（兼容旧接口）。"""
        entries, _, warnings = self.parse_tavern_full(payload)
        return entries, warnings

    def parse_tavern_full(self, payload) -> tuple[list[dict], dict, list[str]]:
        """解析酒馆预设 JSON → (条目列表, 采样参数, 警告列表)。

        兼容四种格式：
        1. 提示词条目数组（[{name, content, enabled, role, ...}]）
        2. 人物卡（{description, personality, scenario, ...}）
        3. chara_card_v2（{"spec": "chara_card_v2", "data": {...}}）
        4. SillyTavern 预设导出（{temperature, top_p, prompts: [...], prompt_order: [...]}）
        条目: {name, content, enabled, role, injection_position, priority, category, note}
        """
        obj = None
        warnings: list[str] = []
        if isinstance(payload, str):
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError as e:
                return [], {}, [f"不是合法的 JSON：{e}"]
        elif isinstance(payload, dict):
            obj = payload
        else:
            return [], {}, ["无法识别的酒馆预设格式"]

        entries: list[dict] = []
        params: dict = {}
        if isinstance(obj, list):
            for i, it in enumerate(obj):
                if not isinstance(it, dict):
                    continue
                content = it.get("content")
                if not isinstance(content, str) or not content.strip():
                    continue
                entries.append({
                    "name": str(it.get("name") or it.get("identifier") or f"条目{i + 1}"),
                    "content": content,
                    "enabled": _str_bool(it.get("enabled", True)),
                    "role": str(it.get("role") or "system"),
                    "injection_position": str(it.get("injectionPosition") or it.get("injection_position")
                                              or ("对话区" if str(it.get("role")) == "user" else "系统区")),
                    "priority": _safe_int(it.get("priority"), 5),
                    "identifier": str(it.get("identifier") or ""),
                })
        elif isinstance(obj, dict) and isinstance(obj.get("prompts"), list):
            # ---- SillyTavern 预设导出格式 ----
            entries, params = self._parse_tavern_preset(obj, warnings)
        elif isinstance(obj, dict):
            data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
            if not isinstance(data, dict):
                return [], {}, ["人物卡结构异常（缺少 data 字段）"]
            card_name = str(obj.get("name") or data.get("name") or "人物卡")
            for key, label, role, is_array in TAVERN_FIELDS:
                v = data.get(key)
                if isinstance(v, str) and v.strip():
                    entries.append({
                        "name": f"{label}（{card_name}）",
                        "content": v,
                        "enabled": True,
                        "role": role,
                        "injection_position": "系统区" if role == "system" else "对话区",
                        "priority": 5,
                        "identifier": key,
                    })
                elif isinstance(v, list) and is_array:  # alternate_greetings 等数组字段
                    for j, item in enumerate(v):
                        if isinstance(item, str) and item.strip():
                            entries.append({
                                "name": f"{label} {j + 1}（{card_name}）",
                                "content": item,
                                "enabled": True,
                                "role": role,
                                "injection_position": "对话区",
                                "priority": 5,
                                "identifier": f"{key}_{j}",
                            })
            if obj.get("creator_notes"):
                warnings.append(f"人物卡备注（creator_notes）：{str(obj['creator_notes'])[:100]}")
        else:
            return [], {}, ["无法识别的酒馆预设格式"]

        if not entries:
            return [], params, (["未解析到任何内容条目"] if not warnings else warnings)
        for e in entries:
            e["category"] = self.classify(e["name"], e["content"])
        return entries, params, warnings

    @staticmethod
    def _parse_tavern_preset(obj: dict, warnings: list[str]) -> tuple[list[dict], dict]:
        """SillyTavern 预设导出：prompts 数组 + 采样参数 + prompt_order 顺序。"""
        entries: list[dict] = []
        params: dict = {}
        # prompt_order 决定注入顺序（order 列表的 index）
        order_index: dict[str, int] = {}
        for po in obj.get("prompt_order") or []:
            if not isinstance(po, dict):
                continue
            for i, o in enumerate(po.get("order") or []):
                if isinstance(o, dict) and o.get("identifier"):
                    order_index.setdefault(str(o["identifier"]), i)
        for i, p in enumerate(obj.get("prompts") or []):
            if not isinstance(p, dict):
                continue
            content = p.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            role = str(p.get("role") or "system")
            pos_raw = str(p.get("injection_position") or "")
            # ST 枚举：0=聊天内（历史深度），1=系统提示词内，2=世界信息内；None=未设
            if pos_raw == "1":
                inj = "系统区"
            elif pos_raw == "2":
                inj = "世界信息"
            else:
                inj = "对话区"
            depth = str(p.get("injection_depth") or "")
            order = str(p.get("injection_order") or "")
            ident = str(p.get("identifier") or "")
            note_bits = []
            if inj != "对话区":
                note_bits.append(f"注入：{inj}")
            if depth and depth not in ("None", "0"):
                note_bits.append(f"深度：{depth}")
            entries.append({
                "name": str(p.get("name") or f"条目{i + 1}"),
                "content": content,
                "enabled": _str_bool(p.get("enabled", True)),
                "role": role,
                "injection_position": inj,
                "priority": _safe_int(order, 5),
                "identifier": ident,
                "note": "；".join(note_bits),
                "_order": order_index.get(ident, i),
            })
        # 采样参数（仅取存在的键）
        for k in ("temperature", "top_p", "top_k", "top_a", "min_p", "frequency_penalty",
                  "presence_penalty", "repetition_penalty", "openai_max_context",
                  "openai_max_tokens", "seed", "stream_openai"):
            if k in obj and obj[k] is not None:
                params_key = "max_context" if k == "openai_max_context" else (
                    "max_tokens" if k == "openai_max_tokens" else k)
                params[params_key] = obj[k]
        entries.sort(key=lambda e: e["_order"])
        for e in entries:
            e.pop("_order", None)
        # 酒馆模板宏解析：展开 setvar/getvar/addvar，剥离 ST 内部宏，还原预设真实注入内容
        macro_warns = resolve_st_macros(entries)
        if macro_warns:
            warnings.append("模板宏：剥离了 ST 专属宏 " + "、".join(macro_warns[:6])
                            + ("…" if len(macro_warns) > 6 else ""))
        # 纯指令条目（解析后为空，如仅 {{setvar::...::}} 的初始化）无实际内容，丢弃
        entries = [e for e in entries if (e.get("content") or "").strip()]
        return entries, params

    # ---------- 预设 CRUD ----------

    def list_presets(self) -> list[dict]:
        return self.store.list_presets()

    def export_preset(self, preset_id: int) -> dict | None:
        """导出为独立分享文件结构。"""
        p = self.store.get_preset(preset_id)
        if not p:
            return None
        return {
            "type": "jiuguan_preset",
            "version": 1,
            "name": p["name"],
            "data": jload(p["data"], {}),
        }

    def import_preset(self, payload: dict, name: str | None = None) -> int | None:
        """导入独立分享文件 / 标准结构，返回新预设 id。"""
        if not isinstance(payload, dict):
            return None
        data = payload.get("data") if payload.get("type") == "jiuguan_preset" else payload
        if not isinstance(data, dict):
            return None
        pname = name or payload.get("name") or "导入预设"
        return self.store.save_preset(pname, data)

    def build_preset_from_cards(self, name: str) -> dict:
        """从当前偏好卡片构建预设（把卡片转条目）。"""
        cards = self.store.list_cards()
        entries = []
        for c in cards:
            entries.append({
                "name": c["title"],
                "content": c["content"],
                "enabled": bool(c["enabled"]),
                "role": "system",
                "injection_position": "系统区",
                "priority": c["priority"],
                "scope": c["scope"],
                "stage": c["stage"],
                "pinned": bool(c.get("pinned")),
                "note": c["note"],
            })
        return {
            "entries": entries,
            "params": {"temperature": 0.8, "top_p": 1.0},
            "order": [e["name"] for e in entries],
        }

    # ---------- 人工辅助：手动字段映射 ----------

    def list_array_fields(self, payload) -> list[dict]:
        """列出顶层（或 data 下）所有数组字段，供手动映射选择。
        返回 [{name, count, sample_keys, sample}]，每个条目尽量带预览。"""
        obj = None
        if isinstance(payload, str):
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                return []
        elif isinstance(payload, dict):
            obj = payload
        if not isinstance(obj, dict):
            return []
        candidates: list[dict] = []
        for scope_name, scope in (("顶层", obj), ("data", obj.get("data"))):
            if not isinstance(scope, dict):
                continue
            for k, v in scope.items():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    first = v[0]
                    candidates.append({
                        "label": f"{scope_name} → {k}",
                        "field": k,
                        "scope": scope_name,
                        "count": len(v),
                        "keys": list(first.keys())[:12],
                        "sample": json.dumps(first, ensure_ascii=False)[:160],
                    })
        return candidates

    def parse_manual_mapping(self, payload, array_field: str, scope: str = "顶层",
                             content_field: str = "content", name_field: str = "name",
                             enabled_field: str = "", role_field: str = "") -> tuple[list[dict], list[str]]:
        """人工指定字段映射后生成条目（兜底方案）。

        array_field 为顶层或 data 下的数组字段名；条目内字段名由用户选择。
        返回 (entries, warnings)。enabled/role 字段留空则自动推断。
        """
        obj = None
        if isinstance(payload, str):
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError as e:
                return [], [f"不是合法的 JSON：{e}"]
        elif isinstance(payload, dict):
            obj = payload
        if not isinstance(obj, dict):
            return [], ["无法解析的数据"]
        data = obj.get("data") if scope == "data" and isinstance(obj.get("data"), dict) else obj
        arr = data.get(array_field) if isinstance(data, dict) else None
        if not isinstance(arr, list):
            return [], [f"字段 {array_field} 不是数组"]

        entries: list[dict] = []
        skipped = 0
        for i, it in enumerate(arr):
            if not isinstance(it, dict):
                continue
            content = it.get(content_field)
            if not isinstance(content, str) or not content.strip():
                skipped += 1
                continue
            role = it.get(role_field) if role_field else "system"
            enabled = _str_bool(it.get(enabled_field), True) if enabled_field else True
            inj = "系统区" if str(role) == "system" else "对话区"
            entries.append({
                "name": str(it.get(name_field) or f"条目{i + 1}"),
                "content": content,
                "enabled": enabled,
                "role": str(role or "system"),
                "injection_position": inj,
                "priority": 5,
                "identifier": str(it.get("identifier") or ""),
                "note": f"手动映射：{array_field} → {content_field}",
            })
        if not entries:
            return [], ["所选字段组合未解析出任何条目（请检查内容字段名）"]
        for e in entries:
            e["category"] = self.classify(e["name"], e["content"])
        warns = [f"跳过 {skipped} 条无内容的条目"] if skipped else []
        return entries, warns

    def apply_preset(self, preset_id: int, import_cards: bool = True) -> int:
        """应用预设：条目转偏好卡片（同名去重）+ 参数存入项目设置。返回导入卡片数。"""
        p = self.store.get_preset(preset_id)
        if not p:
            return 0
        data = jload(p["data"], {})
        count = 0
        if import_cards:
            for e in data.get("entries") or []:
                if not e.get("content"):
                    continue
                title = e.get("name") or "未命名"
                exists = any(c["title"] == title for c in self.store.list_cards())
                if exists:
                    continue
                scope = e.get("scope", "manual")
                stage = e.get("stage", "")
                if scope not in ("global", "stage", "manual"):
                    scope = "global"
                self.store.add_card(title, e["content"], scope=scope, stage=stage,
                                    priority=_safe_int(e.get("priority"), 5),
                                    enabled=bool(e.get("enabled", True)),
                                    pinned=bool(e.get("pinned", False)))
                count += 1
        params = data.get("params") or {}
        if "temperature" in params:
            self.store.set_setting("preset_temperature", str(params["temperature"]))
        self.store.set_setting("bound_preset_id", str(preset_id))
        return count
