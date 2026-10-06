# -*- coding: utf-8 -*-
"""输出完成标志协议（End Mark Protocol，2026-09-09）。

目标：抓「软截断」——finish_reason 正常（非 length），但内容实际中断
（模型停笔/自我审查/服务端掐断）。这类截断 finish_reason=length 抓不到；
detect_unfinished 的末尾标点启发式对大纲/细纲这类「缺一块后仍以句号收尾」
的结构化产物会漏检，导致垃圾内容落库（真实事故：自动模式大纲细化少一块，
后续写作缺重要情节）。

机制（四层）：
1. 注入：assembler 在贴近生成点（order=12.6，critical）注入 END_MARK_INSTRUCTION，
   要求模型全部内容输出完毕后在回复最末尾单独输出一行【生成完毕】。
2. 检测+剥离：pipeline._complete_and_log 统一出口调用 strip_end_mark——
   命中 → 剥掉标志行（不污染入库内容）；未命中 → 返回 found=False。
3. 判定：_complete_and_log 按开关归一 result["end_mark"]（关=恒 True 不参与判定），
   自动模式无标志判失败走重试节拍，手动模式亮「疑似未写完」警告。
4. 开关：end_mark_enabled（默认开）。关闭=不注入不判定；检测/剥离幂等保留。

防误剥护栏：
- 只扫末尾若干个非空行——正文中段的「报告完毕」不受影响；
- 命中行必须独立成行且极短（≤ END_MARK_MAX_CHARS）：核心词元 + 可选引导词元
  + 装饰符号；「这场战争迎来最终的结束。」这类长句不命中（re 的 \\w 含汉字，
  行首出现汉字即不可能命中）；
- 标志行下方的纯装饰行（---/***）一并剥；未命中标志时文本一个字都不动；
- 幂等：重复调用结果一致；日志存原文（含标志行），剥离只影响入库文本。

本模块为纯函数实现（仅依赖 re），可被 core/ui/手机端安全共用。
"""

import re

# 注入给模型的协议文案（assembler 引用）。要点承接草稿协议的坑：
# 必须明写「出现在最终回复文本的最后一行，写在思考里无效」。
END_MARK_INSTRUCTION = (
    "【输出完成标志】全部内容输出完毕后，另起一行，在回复的最末尾单独输出一行：\n"
    "【生成完毕】\n"
    "硬性要求：\n"
    "1) 该标志必须出现在最终回复文本的最后一行，写在思考里无效；\n"
    "2) 标志行之外不得在结尾追加任何解释、总结或后记；\n"
    "3) 若内容因故无法全部输出，不要输出该标志。"
)

# 扫描窗口：只看末尾 END_MARK_TAIL_LINES 个非空行（护栏一：正文中间的
# 「报告完毕」等词不受影响）
END_MARK_TAIL_LINES = 4

# 命中行最大长度（护栏二：标志必须近乎独立成行；带大段正文的行不可能是标志）
END_MARK_MAX_CHARS = 20

# 核心匹配：可选引导词元（≤2 个组合，如「本次生成」）+ 核心词 + 可选装饰。
# \W 在 Python3 str 模式下不含汉字（汉字属 \w）——行首只要出现正文文字
# 就不可能命中，这是「长句不误剥」的根本保证。英文 END/DONE/COMPLETE
# 大小写不敏感；「全文完/本章完/本卷完/全文终」是网文常见收尾元信息。
END_MARK_RE = re.compile(
    r"^[\W_]*"
    r"(?:本次|任务|输出|回复|生成|内容){0,2}"
    r"(?:完毕|结束|完成|全文完|本章完|本卷完|全文终|END|DONE|COMPLETE)"
    r"[\W_]*$",
    re.IGNORECASE,
)

# 纯装饰/分隔行（标志行下方常见 --- /*** 等，跟随标志一起剥）
_DECOR_LINE_RE = re.compile(r"^[\s\-—=_*~#·.。…]+$")

# ---- 中间残留标志（2026-09-10 补） ----
# 末尾剥离只处理「最末尾」：模型若把标志打在正文中间（分节/多段输出时常见），
# 末尾剥离拿不到它 → 标志随正文一起落库（老板实测反馈）。这里做一次全文清理，
# 但只认「强形式」，绝不碰正常行文：
#   A) 成对括号包裹且括号内同时含动作词与完成词：如【生成完毕】/（输出完成）/[任务完成]
#   B) 无括号但整行就是「(本次)?(生成|输出)(完毕|完成|结束)」这一种极端短行
# 「本章完/全文完」不含动作词 → 不删（网文常用，删了就是破坏正文）；
# 对话行「结束。」无动作词且非 A 类 → 不删。宁可漏删，不可误删正文。
_INLINE_BRACKET_RE = re.compile(
    r"^[\s\-—*·>》）)]*[（(【\[][^（）()【】\[\]]{1,20}[）)】\]][\s\-—*·<《（(.。!！~～]*$")
_INLINE_BARE_RE = re.compile(
    r"^(?:本次|任务|输出|回复)?(?:生成|输出)(?:完毕|完成|结束)[\s.。!！~～]*$")
# 括号内部：只允许「可选引导词 + 完成词」，不能有数字/字母/别的字——
# 「【系统提示：任务完成度 80%】」这类正文内容因此不会误删。
_INLINE_INNER_RE = re.compile(
    r"^[\W_]*(?:本次|任务|输出|回复|内容|生成){0,2}(?:完毕|完成|结束)[\W_]*$")


def _is_strong_mark_line(line: str) -> bool:
    """该行是否为「强形式完成标志」（可安全全文删除）。"""
    s = (line or "").strip()
    if not s or len(s) > 24:
        return False
    if _INLINE_BARE_RE.match(s):
        return True
    if not _INLINE_BRACKET_RE.match(s):
        return False
    m = re.search(r"[（(【\[](.*?)[）)】\]]", s)
    inner = (m.group(1) if m else "").strip()
    if not inner or len(inner) > 8 or re.search(r"[0-9A-Za-z]", inner):
        return False
    return bool(_INLINE_INNER_RE.match(inner))


def strip_inline_end_marks(text: str) -> tuple:
    """清理正文中残留的「强形式」完成标志行（任意位置）。返回 (文本, 删除行数)。

    幂等；无残留时原样返回（连空白都不动）。只删标志行及其造成的冗余空行，
    正常行文（含「本章完/全文完」「结束。」这类收尾）一律不动。
    """
    text = text or ""
    if not text.strip():
        return text, 0
    lines = text.splitlines()
    kept = [ln for ln in lines if not _is_strong_mark_line(ln)]
    removed = len(lines) - len(kept)
    if removed <= 0:
        return text, 0
    # 标志行留下的空洞：连续空行/装饰行压缩成一处，不让正文出现大片空档
    out: list = []
    for ln in kept:
        s = ln.strip()
        if (not s or _DECOR_LINE_RE.match(s)) and \
                out and (not out[-1].strip() or _DECOR_LINE_RE.match(out[-1].strip())):
            continue
        out.append(ln)
    return "\n".join(out).strip(), removed


def end_mark_enabled(store) -> bool:
    """协议开关（默认开）。关闭 = 不注入协议、不参与完整性判定。

    store 兼容 ProjectStore（get_setting_int 对全局键转发 gstore）与
    GlobalStore（get_global_setting）；两者都不可用时视为开启。
    """
    try:
        return int(store.get_setting_int("end_mark_enabled", 1)) > 0
    except AttributeError:
        pass
    except (TypeError, ValueError):
        return True
    try:
        return int(store.get_global_setting("end_mark_enabled", "1")) > 0
    except Exception:
        return True


def strip_end_mark(text: str) -> tuple:
    """剥离文本末尾的完成标志行。返回 (清洗后文本, 是否命中)。幂等。

    - 未命中任何标志 → 原样返回（一个字不动，含尾部空白与分隔装饰）；
    - 命中 → 剥掉标志行、其下方所有行（空行/装饰）以及其上方紧邻的
      空行/纯装饰行（模型常把 --- 垫在标志前后）；
    - 连续多个标志行逐个剥；文本只剩标志 → 返回 ("", True)。
    """
    text = text or ""
    if not text.strip():
        return text, False
    lines = text.splitlines()
    found = False
    while True:
        # 找最后一个「实体行」（跳过空行与纯装饰行——--- 垫在标志下方不挡检测）
        last = len(lines) - 1
        while last >= 0:
            s = lines[last].strip()
            if s and not _DECOR_LINE_RE.match(s):
                break
            last -= 1
        if last < 0:
            break
        s = lines[last].strip()
        if len(s) <= END_MARK_MAX_CHARS and END_MARK_RE.match(s):
            found = True
            # 连带剥掉标志上方紧邻的空行/装饰行
            j = last - 1
            while j >= 0:
                sj = lines[j].strip()
                if not sj or _DECOR_LINE_RE.match(sj):
                    j -= 1
                    continue
                break
            del lines[j + 1:]
            continue
        break
    if not found:
        return text, False
    return "\n".join(lines).rstrip(), True
