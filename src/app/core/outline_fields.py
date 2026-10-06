# -*- coding: utf-8 -*-
"""旧版章节大纲字段定义与兼容处理。

当前章节大纲正文由 ``app.core.chapter_outline`` 统一管理；本模块保留旧字段配置、
旧 JSON 解析所需的值处理和公开接口，供旧项目与外部调用继续使用。
"""
import copy
import json
import re


FIELD_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CORE_OUTLINE_KEYS = {"title", "characters", "target_words"}
LEGACY_OUTLINE_KEYS = {
    "title", "event", "conflict", "characters", "info_progress",
    "char_change", "hook", "target_words",
}

DEFAULT_OUTLINE_FIELDS = [
    {"key": "title", "label": "章节标题", "enabled": True,
     "builtin": True, "value_type": "text"},
    {"key": "event", "label": "本章事件", "enabled": True,
     "builtin": True, "value_type": "text"},
    {"key": "conflict", "label": "核心冲突", "enabled": True,
     "builtin": True, "value_type": "text"},
    {"key": "characters", "label": "出场人物", "enabled": True,
     "builtin": True, "value_type": "characters"},
    {"key": "info_progress", "label": "信息推进", "enabled": True,
     "builtin": True, "value_type": "text"},
    {"key": "char_change", "label": "人物变化", "enabled": True,
     "builtin": True, "value_type": "text"},
    {"key": "hook", "label": "章末钩子", "enabled": True,
     "builtin": True, "value_type": "text"},
    {"key": "target_words", "label": "预计字数", "enabled": True,
     "builtin": True, "value_type": "integer"},
]

_DEFAULT_BY_KEY = {field["key"]: field for field in DEFAULT_OUTLINE_FIELDS}


def normalize_outline_fields(fields) -> list[dict]:
    """规范化字段定义，保留顺序并确保系统核心字段存在。"""
    if fields is None:
        fields = DEFAULT_OUTLINE_FIELDS
    if not isinstance(fields, list):
        raise ValueError("大纲字段配置必须是列表")

    normalized = []
    seen = set()
    for raw in fields:
        if not isinstance(raw, dict):
            raise ValueError("大纲字段配置项必须是对象")
        key = str(raw.get("key", "")).strip()
        label = str(raw.get("label", "")).strip()
        if not FIELD_KEY_RE.fullmatch(key):
            raise ValueError(f"JSON 键不安全：{key or '空'}")
        if not label:
            raise ValueError(f"字段「{key}」的显示名称不能为空")
        if key in seen:
            raise ValueError(f"大纲字段 JSON 键重复：{key}")
        seen.add(key)

        builtin = key in _DEFAULT_BY_KEY
        default = _DEFAULT_BY_KEY.get(key, {})
        value_type = default.get("value_type", "text") if builtin else "text"
        normalized.append({
            "key": key,
            "label": label,
            "enabled": bool(raw.get("enabled", True)),
            "builtin": builtin,
            "value_type": value_type,
        })

    # 核心字段不可物理删除；用户仍可通过 enabled 隐藏/停用。
    for key in ("title", "characters", "target_words"):
        if key not in seen:
            default = _DEFAULT_BY_KEY[key]
            normalized.append(copy.deepcopy(default) | {"enabled": False})
    return normalized


def effective_field_defs(fields=None) -> list[dict]:
    """返回不会被调用方修改的规范化字段列表。"""
    return copy.deepcopy(normalize_outline_fields(fields))


def active_outline_fields(fields=None) -> list[dict]:
    return [field for field in effective_field_defs(fields) if field["enabled"]]


def outline_fields_instruction(fields=None) -> str:
    """生成可放进任务模板的 JSON 字段说明。"""
    parts = []
    for field in active_outline_fields(fields):
        kind = field["value_type"]
        if kind == "characters":
            shape = "出场人物数组"
        elif kind == "integer":
            shape = "整数"
        else:
            shape = "文本"
        parts.append(f"{field['key']}({field['label']}，{shape})")
    if not parts:
        return "当前没有启用的大纲字段。"
    return "当前启用字段：" + "、".join(parts) + "。"


def outline_value(chapter: dict, field: dict):
    """按字段定义从章节字典取值，兼容旧固定列与自定义 JSON。"""
    key = field["key"]
    if key in chapter:
        return chapter.get(key)
    return (chapter.get("custom_fields") or {}).get(key, "")


def display_outline_value(value, field: dict) -> str:
    """把字段值转换成编辑器/上下文可读文本。"""
    if value is None:
        return ""
    if field.get("value_type") == "characters":
        if isinstance(value, list):
            return "、".join(str(item) for item in value)
        return str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def custom_field_values(payload: dict, fields=None) -> dict:
    """提取非固定字段，未知字段也保留，避免手写模板结果丢失。"""
    core_keys = set(_DEFAULT_BY_KEY)
    values = payload.get("custom_fields") if isinstance(payload, dict) else {}
    values = dict(values) if isinstance(values, dict) else {}
    for key, value in (payload or {}).items():
        if key not in core_keys and key not in {"custom_fields"}:
            values[key] = value
    return {str(key): coerce_custom_value(value)
            for key, value in values.items() if str(key).strip()}


def coerce_custom_value(value) -> str:
    """自定义字段统一按文本编辑，结构化误输出转成可读 JSON。"""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def outline_summary(chapter: dict, fields=None, include_empty: bool = False) -> str:
    """生成章概要文本，供上下文、预览和导出共用。

    新格式：章节大纲全文存在 outline_content，直接返回（不再拆字段）；
    旧书无 outline_content 时按字段配置拼接，保证历史数据仍可读。
    """
    content = str((chapter or {}).get("outline_content") or "").strip()
    if content:
        return content
    # 这是旧公开接口：保留字段配置顺序、标签和自定义字段的原返回格式。
    # 当前 UI、组装器和导出器使用 chapter_outline_text()，不会走到这里。
    lines = []
    for field in active_outline_fields(fields):
        value = display_outline_value(outline_value(chapter or {}, field), field).strip()
        if value or include_empty:
            lines.append(f"{field['label']}：{value or '未填写'}")
    return "\n".join(lines)


# ---------- 大纲序号点切分（供 UI 分点展示/预览复用） ----------

_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
_OUTLINE_POINT_START_RE = re.compile(
    r"^\s*("
    r"[①-⑳]"
    r"|\(?\d+[)．.、:：]?"
    r"|[一二三四五六七八九十]+[、.．]"
    r")\s*(.*)$")


def _point_index(num: str) -> int:
    """大纲序号点 → 序号整数：①→1、12.→12、一、→1、（3）→3。"""
    s = (num or "").strip().strip("()）.．、:：")
    if not s:
        return 0
    if len(s) == 1 and "①" <= s <= "⑳":
        return ord(s) - ord("①") + 1
    if s.isdigit():
        return int(s)
    total, num_ = 0, 0
    for ch in s:
        if ch in _CN_DIGITS:
            num_ = _CN_DIGITS[ch]
        elif ch == "十":
            total += num_ if num_ else 10
            num_ = 0
    return total + num_


def split_outline_points(content: str) -> list[dict]:
    """兼容入口；新代码请从 ``chapter_outline`` 导入。"""
    from .chapter_outline import split_outline_points as _split
    return _split(content)
