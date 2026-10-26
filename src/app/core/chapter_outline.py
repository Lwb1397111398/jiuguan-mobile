# -*- coding: utf-8 -*-
"""章节大纲的统一接口。

当前流程中的章节大纲是一段按顺序组织的正文计划（①②③……）。数据库里的
旧字段仍然保留，但只作为兼容来源；新代码应通过本模块读取和拆分章节大纲。
"""
from __future__ import annotations

import re


_POINT_START_RE = re.compile(
    r"^\s*("
    r"[①-⑳]"
    r"|\(?\d+[)．.、:：]?"
    r"|[一二两三四五六七八九十百千万]+[、.．]"
    r")\s*(.*)$"
)
_CN_DIGITS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}

# 无序号的结构性行：『【章末状态收束】…』『【本章新增情节清单】』『——展开分卷节拍①：…』
# 独立成无序号点展示，避免被并入上一条序号点（自定义大纲模板兼容）；
# 【】块标题后允许跟内容（如『【章末状态收束】位置：X』整行独立）
_SECTION_START_RE = re.compile(r"^\s*(?:【[^】\r\n]+】|——)")

# 整行被 ** / ＊ 包裹的加粗行（『**节拍①｜标题**』）：剥装饰留文字。
# 模型爱给节拍标题自加粗，而加粗会挡住分批识别的行首节拍判定，大纲落库前统一剥掉。
_BOLD_WHOLE_LINE_RE = re.compile(r"^\s*([*＊]){1,2}(.*?)\1{1,2}\s*$")


def strip_whole_line_bold(text: str | None) -> str:
    """剥掉「整行被 ** 包裹」的加粗装饰，其余文字逐字保留。

    只处理整行加粗（含全角 ＊＊），行内局部加粗（强调排版）不动。
    AI 大纲落库前应用（parse_outline_chapter）：防止模型自作主张把
    『节拍①』排版成『**节拍①｜标题**』挡住分批识别（2026-10-24 五遁忍术）。
    """
    return "\n".join(
        _BOLD_WHOLE_LINE_RE.sub(r"\2", line)
        for line in str(text or "").splitlines()
    )

# title 是章节元数据，不属于章节大纲正文；target_words 一律不进大纲文本
# （字数由用户自定义提示词规定，系统不注入篇幅要求）。
_LEGACY_FIELDS = (
    ("event", "本章事件"),
    ("conflict", "核心冲突"),
    ("characters", "出场人物"),
    ("info_progress", "信息推进"),
    ("char_change", "人物变化"),
    ("hook", "章末钩子"),
)


def keep_chapter_title(data: dict | None, original: dict | None) -> dict:
    """章名锁定：AI 生成的大纲结果一律沿用原章名，只更新大纲正文。

    章名在分卷切章时就已确定，之后细化/重生成只应改写本章大纲正文；
    AI 常在正文里重写一遍『第N章：XX』标题行，直接采用会让章名被悄悄改掉
    （2026-09-08 老板反馈：第16章章名在大纲细化阶段被 AI 改写）。
    仅当原章名为空（空章创建后直接细化）时，才采用 AI 给出的章名。
    """
    data = dict(data or {})
    origin_title = str((original or {}).get("title") or "").strip()
    if origin_title and "title" in data:
        data["title"] = origin_title
    return data


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "、".join(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, dict):
        return "；".join(f"{key}：{val}" for key, val in value.items()
                         if str(val).strip())
    return str(value).strip()


def legacy_outline_text(chapter: dict | None) -> str:
    """把旧章节字段转换为当前的序号点大纲文本。

    只读取旧字段，不修改传入字典。目标字数一律不进大纲文本（字数由用户
    自定义提示词规定，系统不注入任何篇幅要求）。
    """
    chapter = chapter or {}
    lines: list[str] = []
    for key, label in _LEGACY_FIELDS:
        value = _text(chapter.get(key))
        if value:
            lines.append(value if key == "event" and not label else f"{label}：{value}")
    return "\n".join(f"{_point_mark(index)} {line}" for index, line in enumerate(lines, 1))


def _point_mark(index: int) -> str:
    return chr(0x2460 + index - 1) if 1 <= index <= 20 else f"{index}."


def chapter_outline_text(chapter: dict | None) -> str:
    """读取当前章节大纲；空正文时回退为旧字段兼容文本。"""
    chapter = chapter or {}
    content = str(chapter.get("outline_content") or "").strip()
    return content or legacy_outline_text(chapter)


def outline_draft_text(chapter: dict | None) -> str:
    """读取分卷切分初稿（outline_draft）：分卷页展示与「待细化」提示词的数据源。

    初稿永不覆盖：分卷切章时写入 outline_draft，之后细化/重生成/手动编辑只改
    outline_content，重新细化始终以初稿为底稿。旧库章节迁移时已用当时大纲回填
    初稿；仍为空（如空章后再手写大纲）时回退当前大纲。
    """
    chapter = chapter or {}
    draft = str(chapter.get("outline_draft") or "").strip()
    return draft or chapter_outline_text(chapter)


def outline_text_chars(text: str | None) -> int:
    """大纲文本字数：只数非空白字符（换行/空格/制表符不计）。

    UI 展示用（分卷页卡片「N 字」）。与旧字段 target_words（数据库默认 3000 的
    死数据，切章解析不产出、提示词也不注入）无关，统计的是真实存在的正文。
    """
    return sum(1 for ch in str(text or "") if not ch.isspace())


def _point_index(raw: str) -> int:
    value = (raw or "").strip().strip("()（）.．、:：")
    if not value:
        return 0
    if len(value) == 1 and "①" <= value <= "⑳":
        return ord(value) - ord("①") + 1
    if value.isdigit():
        return int(value)
    if value.isalpha() and all(ch in _CN_DIGITS or ch in "十百千万" for ch in value):
        total = 0
        current = 0
        units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
        for ch in value:
            if ch in _CN_DIGITS:
                current = _CN_DIGITS[ch]
            else:
                unit = units[ch]
                total += (current or 1) * unit
                current = 0
        return total + current
    return 0


def split_outline_points(content: str | None) -> list[dict]:
    """解析序号点大纲，返回 ``[{"index": int, "content": str}]``。

    序号可以是 ①、1.、（1）或“一、”；未带序号的续行并入前一点。完全没有
    序号时整段作为一个无编号点，便于旧项目平滑展示。
    【章末状态收束】【本章新增情节清单】等块标题行与「——展开…」节拍行
    独立成无序号点（自定义大纲模板兼容，预览更清晰）。
    """
    text = str(content or "").strip()
    if not text:
        return []
    points: list[dict] = []
    current_index = 0
    current_lines: list[str] = []
    for line in text.splitlines():
        match = _POINT_START_RE.match(line)
        if match:
            if current_lines:
                body = "\n".join(current_lines).strip()
                if body:
                    points.append({"index": current_index, "content": body})
            current_index = _point_index(match.group(1))
            current_lines = [match.group(2).strip()]
        elif _SECTION_START_RE.match(line):
            if current_lines:
                body = "\n".join(current_lines).strip()
                if body:
                    points.append({"index": current_index, "content": body})
            current_index = 0
            current_lines = [line.strip()]
        else:
            current_lines.append(line.strip())
    if current_lines:
        body = "\n".join(current_lines).strip()
        if body:
            points.append({"index": current_index, "content": body})
    return points or [{"index": 0, "content": text}]
