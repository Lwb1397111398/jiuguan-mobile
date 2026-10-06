# -*- coding: utf-8 -*-
"""JSON 提取工具：从模型输出中稳健解析 JSON（围栏、前后缀干扰、截断容忍）。"""
import json
import re

from .textclean import has_think_marks, strip_model_output

# 思考提示词：出现在首个 JSON 片段之前时，说明模型先写了思考/草稿
_THINK_HINT_RE = re.compile(
    r"(?:思考|思维|分析|推理|我先|让我想|草稿|酝酿|自检|检查一下)")
# 思考前缀长度上限：超过就不像「思考引导句」，多半是正文说明
_THINK_PREFIX_MAX = 400


def strip_fences(text: str) -> str:
    """去掉 ```json ... ``` 围栏。"""
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


# 文本中任意位置的 ``` 围栏块（模型常在 JSON 前后加说明文字：
# 『【自我发挥】…前尘已定，故事将演。』+ ```json …``` + 『末块 to 确认：…』）
_FENCE_BLOCK_RE = re.compile(r"```[a-zA-Z0-9_+-]*[ \t]*\r?\n(.*?)(?:\r?\n[ \t]*```|\Z)", re.S)


def fenced_blocks(text: str) -> list[str]:
    """取出文本中所有 ``` 围栏块的内容（围栏不在开头时 strip_fences 剥不掉）。

    末尾围栏缺失（输出被截断）也收，故用 \\Z 兜底。
    """
    return [m.group(1).strip() for m in _FENCE_BLOCK_RE.finditer(text or "")
            if m.group(1).strip()]


def json_region(text: str) -> str:
    """文本里最可能是 JSON 的那段：整段剥围栏后不像 JSON 时，退回取围栏块内容。"""
    t = strip_fences(text or "")
    if t.lstrip().startswith(("[", "{")):
        return t
    for b in fenced_blocks(text or ""):
        if b.lstrip().startswith(("[", "{")):
            return b
    return t


def _is_json_literal(s: str) -> bool:
    """裸词是否合法 JSON 字面量（true/false/null/数字）。"""
    try:
        json.loads(s)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


def _repair_json(t: str, allow_bare: bool = False) -> str:
    """修复 LLM 输出中常见的 JSON 错误：

    1. 字符串内出现真实换行符（\n / \r）→ 转义为 \\n / \\r；
       特例：换行后（跳过空白）紧跟 , } ] 或文本结尾 ⇒ 判定为字符串缺闭合引号，
       在换行处补一个 `"` 并闭合字符串（LLM 常漏写结尾引号）；
    2. 字符串内出现未转义的双引号（如正文里写 他说"入笼" 用了 ASCII 引号）
       → 启发式判定：引号后（跳过空白）是 , } ] : 或文本结尾 ⇒ 视为闭合引号；
       否则视为正文内容 ⇒ 转义为 \\"；
    3. 字符串外：导致深度为负的右括号（多余 } / ]）→ 删除；
    4. 字符串外：悬挂逗号（, 后跳过空白紧跟 } / ] 或结尾）→ 删除；
    5. 字符串外的裸词（漏写引号的值，如 "key_nodes": [无] / "to": 省略）
       → 非 true/false/null/数字 ⇒ 补引号变成字符串。仅在 allow_bare=True 时启用：
       它会把散文里的 `[前缀]` 也变成合法数组，若无条件开启，_scan_json 会误取
       正文里的方括号片段而不是真正的 JSON，故只作最后手段（见 _scan_json 两轮扫描）。
    """
    out = []
    i, n = 0, len(t)
    in_str = False
    changed = False
    depth = 0
    while i < n:
        c = t[i]
        if in_str:
            if c == "\\":
                out.append(c)
                if i + 1 < n:
                    out.append(t[i + 1])
                    i += 2
                    continue
                i += 1
                continue
            if c == '"':
                j = i + 1
                while j < n and t[j] in " \t\n\r":
                    j += 1
                nxt = t[j] if j < n else ""
                if nxt in ",}]:\"" or nxt == "":
                    in_str = False
                    out.append(c)
                else:
                    out.append('\\"')
                    changed = True
                i += 1
                continue
            if c in "\n\r":
                # 换行后（跳过空白）紧跟 , } ] 或结尾 ⇒ 字符串缺闭合引号，补上
                j = i + 1
                while j < n and t[j] in " \t":
                    j += 1
                nxt = t[j] if j < n else ""
                if nxt in ",}]" or j >= n:
                    out.append('"')
                    in_str = False
                    changed = True
                    i += 1
                    continue
                out.append("\\n" if c == "\n" else "\\r")
                changed = True
                i += 1
                continue
            out.append(c)
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c in "[{":
            depth += 1
            out.append(c)
            i += 1
            continue
        if c == ",":
            # 悬挂逗号：跳过空白后紧跟 } ] 或结尾 ⇒ 删除
            j = i + 1
            while j < n and t[j] in " \t\n\r":
                j += 1
            nxt = t[j] if j < n else ""
            if nxt in "}]" or j >= n:
                changed = True
                i += 1
                continue
            out.append(c)
            i += 1
            continue
        if c in "}]":
            if depth <= 0:
                changed = True  # 多余右括号，删除
                i += 1
                continue
            depth -= 1
            out.append(c)
            i += 1
            continue
        if c in " \t\n\r:":
            out.append(c)
            i += 1
            continue
        # 字符串外的裸词：扫到下一个结构符，非法字面量则补引号（仅 allow_bare）
        j = i
        while j < n and t[j] not in ",}]{[\"\n\r":
            j += 1
        token = t[i:j]
        core = token.strip()
        if allow_bare and core and not _is_json_literal(core):
            out.append(json.dumps(core, ensure_ascii=False))
            out.append(token[len(token.rstrip()):])  # 保留尾部空白
            changed = True
        else:
            out.append(token)
        i = j
    return "".join(out) if changed else t


def _try_parse_fixed(candidate: str, allow_bare: bool = False):
    """尝试解析候选片段：原样 → _repair_json → raw_decode 取第一个完整 JSON。"""
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    fixed = _repair_json(candidate, allow_bare=allow_bare)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        # Extra data 等：取第一个完整 JSON，丢弃尾部（保留已修复的主体）
        try:
            obj, _ = json.JSONDecoder().raw_decode(fixed)
            return obj
        except json.JSONDecodeError:
            return None


def _scan_json_pass(t: str, allow_bare: bool):
    """按位置扫描每个 `[`/`{` 起点，返回第一个能完整解析的 JSON；无则 None。"""
    for i, ch in enumerate(t):
        if ch not in "[{":
            continue
        depth = 0
        in_str = False
        esc = False
        for j in range(i, len(t)):
            c = t[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
                if depth == 0:
                    obj = _try_parse_fixed(t[i:j + 1], allow_bare=allow_bare)
                    if obj is not None:
                        return obj
                    # 起点可能错（如说明文字里的方括号）：继续找下一个起点
                    break
    return None


def _balanced_end(t: str, i: int) -> int:
    """从 t[i]（`[` 或 `{`）起找括号配平的收尾下标；找不到返回 -1。"""
    depth = 0
    in_str = esc = False
    for j in range(i, len(t)):
        c = t[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                return j
    return -1


def _outermost_spans(t: str) -> list[tuple[int, int]]:
    """左到右取最外层的括号平衡区间。未闭合的起点跳过（让内层仍有机会）。"""
    spans: list[tuple[int, int]] = []
    i, n = 0, len(t)
    while i < n:
        if t[i] not in "[{":
            i += 1
            continue
        end = _balanced_end(t, i)
        if end < 0:
            i += 1
            continue
        spans.append((i, end))
        i = end + 1
    return spans


def _outermost_spans_reordered(t: str) -> list:
    """最外层平衡区间（必要时倒序）：思考里先写 JSON 草稿时正式 JSON 在最后。

    模型常在思考段里先写一个 JSON 草稿、再输出正式 JSON，直接取第一个会拿到
    草稿。判据（保守，宁可不判）：存在 2 个以上顶层片段，且首个片段之前的文字
    像思考引导句（≤400 字且含思考/分析类词）→ 从最后一个片段开始尝试。
    """
    spans = _outermost_spans(t)
    if len(spans) >= 2:
        prefix = t[:spans[0][0]]
        if len(prefix) <= _THINK_PREFIX_MAX and _THINK_HINT_RE.search(prefix):
            return spans[::-1]
    return spans


def _clean_think_for_json(text: str) -> str:
    """JSON 提取前的思考剥离：内联 <think> 与「思考环节」段（无痕时零开销）。"""
    if not text or not has_think_marks(text):
        return text or ""
    clean, _removed, info = strip_model_output(text)
    return clean


def _scan_json(t: str):
    """扫描文本取第一个能解析的 JSON；无则 (None, 错误)。

    先只在「最外层平衡区间」里找：否则 `[{"a": ["x",], "b": [无]}]` 会命中内层
    的 `["x",]`，静默返回 ['x'] 这种错数据（内层片段比整段更容易修好）。
    每轮内部先不许裸词补引号，全失败才放开——保证散文里的 `[前缀]` 不会抢在
    真正的 JSON 前面被选中。最外层全解析不出（如整段被截断）才退回逐起点扫描。
    """
    spans = _outermost_spans_reordered(t)
    for allow_bare in (False, True):
        for s, e in spans:
            obj = _try_parse_fixed(t[s:e + 1], allow_bare=allow_bare)
            if obj is not None:
                return obj, None
    for allow_bare in (False, True):  # 最外层全失败：退回含内层的逐起点扫描
        obj = _scan_json_pass(t, allow_bare)
        if obj is not None:
            return obj, None
    return None, "JSON 不完整或格式错误（可能是输出被截断，请增大 max_tokens 或减小生成内容）"


def _close_truncated(frag: str, stack: list, in_str: bool) -> str:
    """给截断的 JSON 片段补结尾：未闭合字符串补引号、去掉悬挂逗号、按栈补右括号。"""
    out = frag + ('"' if in_str else "")
    out = out.rstrip()
    if out.endswith(","):
        out = out[:-1]
    for ch in reversed(stack):
        out += "}" if ch == "{" else "]"
    return out


def _array_start(t: str) -> int:
    """数组扫描起点：思考里先写了数组草稿时（前缀像思考引导句），正式数组在最后。"""
    i = t.find("[")
    if i < 0:
        return -1
    last = t.rfind("[")
    if last > i and len(t[:i]) <= _THINK_PREFIX_MAX and _THINK_HINT_RE.search(t[:i]):
        return last
    return i


def _scan_array_items(t: str) -> list:
    """从（可能被截断的）JSON 数组里收齐所有顶层元素。

    输出被 max_tokens 截断时整个数组无法闭合，_scan_json 只会捞到第一个
    完整对象、后面全丢（细纲十几个子事件只剩一个）。这里从 `[` 起按括号栈
    扫描，逐个解析闭合的顶层元素；末尾那个残缺元素补齐括号后再试一次，
    补不出合法 JSON 才丢弃。
    """
    i = _array_start(t)
    if i < 0:
        return []
    items: list = []
    stack: list = []
    start = -1
    in_str = esc = False
    for j in range(i + 1, len(t)):
        c = t[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c in "[{":
            if not stack:
                start = j
            stack.append(c)
        elif c in "]}":
            if not stack:
                return items  # 数组自身闭合，元素已收完
            stack.pop()
            if not stack and start >= 0:
                obj = _try_parse_fixed(t[start:j + 1], allow_bare=True)
                if obj is not None:
                    items.append(obj)
                start = -1
    if stack and start >= 0:  # 末尾元素被截断：补齐括号抢救
        obj = _try_parse_fixed(_close_truncated(t[start:], stack, in_str), allow_bare=True)
        if obj is not None:
            items.append(obj)
    return items


def extract_json(text: str):
    """从文本中提取第一个完整 JSON 对象/数组。失败返回 (None, 错误信息)。

    思考内容（内联 <think> 或「思考环节」段）先剥离：模型在思考里写 JSON 草稿时，
    不剥会取到思考里的假 JSON（记忆归档/审校/文风/破甲 AI 解析都走这里）。
    """
    if not text:
        return None, "输出为空"
    text = _clean_think_for_json(text)
    if not text:
        return None, "输出为空"
    # 围栏可能夹在说明文字中间（前有『【自我发挥】…』后有『末块 to 确认：…』），
    # strip_fences 只认开头的围栏，故用 json_region 优先取围栏内容
    t = json_region(text)
    # 直接解析
    try:
        return json.loads(t), None
    except json.JSONDecodeError:
        pass
    # 逐位置扫描（处理模型在 JSON 前加了话、说明文字里含方括号等干扰）
    return _scan_json(t)


def extract_json_list(text: str):
    """提取 JSON 数组；若是单对象则包装成数组。

    数组被截断时（末尾缺 ] / }）改用逐元素扫描，尽量把已完整的元素都捞回来。
    """
    obj, err = extract_json(text)
    t = json_region(_clean_think_for_json(text) or "")
    if t.lstrip().startswith("["):
        salvaged = _scan_array_items(t)
        if salvaged and (err or not isinstance(obj, list) or len(salvaged) > len(obj)):
            return salvaged, None
    if err:
        return None, err
    if isinstance(obj, list):
        return obj, None
    if isinstance(obj, dict):
        return [obj], None
    return None, "JSON 不是数组或对象"


def find_field(obj: dict, *names, default=None):
    """容错取字段：兼容 snake_case/camelCase。"""
    if not isinstance(obj, dict):
        return default
    for n in names:
        if n in obj:
            return obj[n]
        alt = n[0].upper() + n[1:] if n else n
        if alt in obj:
            return obj[alt]
    return default


def find_any_field(obj, groups, max_depth: int = 8):
    """递归模糊匹配：按优先级分组在嵌套 JSON 中找字段。

    groups: 关键词分组列表，按优先级从高到低（如 [强关键词组, 弱关键词组]）。
    对每组做一次全树深度优先搜索，键名包含组内任一关键词（忽略大小写）
    且值为字符串/数字（bool 除外）时命中，返回其字符串形式；找不到返回 None。
    """
    for group in groups:
        hit = _search_group(obj, group, 0, max_depth)
        if hit is not None:
            return hit
    return None


def find_any_list(obj, groups, max_depth: int = 8):
    """递归模糊匹配数组：键名匹配且值为 list 时返回该 list；
    否则退化为字符串匹配（如"张三、李四"）→ 逗号拆分。找不到返回 None。"""
    for group in groups:
        hit = _search_group_list(obj, group, 0, max_depth)
        if hit is not None:
            return [str(x).strip() for x in hit if str(x).strip()]
    val = find_any_field(obj, groups, max_depth=max_depth)
    if isinstance(val, str) and val.strip():
        return [x.strip() for x in val.replace("，", ",").replace("、", ",").split(",")
                if x.strip()]
    return None


def find_any_key(obj, groups, max_depth: int = 8):
    """返回第一个键名匹配（含关键词子串）的键名；找不到返回 None。

    与 find_any_field 同规则扫描，但只返回键名本身，不要求值类型。
    """
    for group in groups:
        hit = _search_group_key(obj, group, 0, max_depth)
        if hit is not None:
            return hit
    return None


def _search_group_key(obj, keywords, depth: int, max_depth: int):
    if depth > max_depth:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                continue
            kl = k.lower()
            if any(kw in kl for kw in keywords):
                return k
            if isinstance(v, (dict, list)):
                r = _search_group_key(v, keywords, depth + 1, max_depth)
                if r is not None:
                    return r
        for v in obj.values():
            if isinstance(v, (dict, list)):
                r = _search_group_key(v, keywords, depth + 1, max_depth)
                if r is not None:
                    return r
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                r = _search_group_key(item, keywords, depth + 1, max_depth)
                if r is not None:
                    return r
    return None


def _search_group_list(obj, keywords, depth: int, max_depth: int):
    if depth > max_depth:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                continue
            kl = k.lower()
            if any(kw in kl for kw in keywords):
                if isinstance(v, list) and v:
                    return v
                if isinstance(v, (dict, list)):
                    r = _search_group_list(v, keywords, depth + 1, max_depth)
                    if r is not None:
                        return r
        for v in obj.values():
            if isinstance(v, (dict, list)):
                r = _search_group_list(v, keywords, depth + 1, max_depth)
                if r is not None:
                    return r
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                r = _search_group_list(item, keywords, depth + 1, max_depth)
                if r is not None:
                    return r
    return None


def _search_group(obj, keywords, depth: int, max_depth: int):
    if depth > max_depth:
        return None
    if isinstance(obj, dict):
        # 先查本层键是否命中，再递归子值（保持键的书写顺序，先到先得）
        for k, v in obj.items():
            if not isinstance(k, str):
                continue
            kl = k.lower()
            if any(kw in kl for kw in keywords):
                if _is_scalar_value(v):
                    return str(v).strip()
                if isinstance(v, (dict, list)):
                    r = _search_group(v, keywords, depth + 1, max_depth)
                    if r is not None:
                        return r
        for v in obj.values():
            if isinstance(v, (dict, list)):
                r = _search_group(v, keywords, depth + 1, max_depth)
                if r is not None:
                    return r
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                r = _search_group(item, keywords, depth + 1, max_depth)
                if r is not None:
                    return r
    return None


def _is_scalar_value(v) -> bool:
    """可作字段值的标量：字符串/数字（bool 排除，None 排除）。"""
    if v is None or isinstance(v, (bool, dict, list)):
        return False
    return isinstance(v, (str, int, float)) and str(v).strip() != ""
