# -*- coding: utf-8 -*-
"""设定库页（对齐桌面 `app/ui/lore_page.py`）。

分类卡片 + 快速检索（按名称/内容/标签）+ 条目增删改（名称/分类/内容/标签/锁定）
+ 注入状态标注（未匹配记忆人物 / 沉睡 / 已移除 / 已锁定）+ 恢复已移除条目
+ 从记忆导入全部（预览确认，导入后消除「设定资料库缺卡」提醒）+ 锁定项管理。

业务全部走 `app.core.store`（与桌面同源），界面按 Flet 惯例重写。
检索走 `visible` 切换而不是重建控件树 —— ExpansionTile 实例保持稳定，
折叠状态与滚动位置都不会因为打字而丢失（对齐桌面 setRowHidden 的做法）。
"""
import re

import flet as ft

from widgets import hint, section, toast, confirm
from memory_ui import _sheet_height

CATS = ["角色", "地点", "物品", "术语"]
LOCK_TYPES = ["人名", "关键设定", "结局走向", "状态卡字段"]


# =====================================================
# 纯函数：标签解析 / 状态判定
# =====================================================

def _split_tags(text: str) -> list:
    """标签解析：逗号/顿号/分号分隔（与桌面 `_LoreEntryDialog._save` 同口径）。"""
    return [t.strip() for t in re.split(r"[,，、;；]", text or "") if t.strip()]


def _mem_names(store) -> set:
    """当前记忆里的人物名 + 别名集合（角色设定是否「能注入」的判据）。

    注入侧 `assembler._lore_text` 按人物名与别名精确匹配，对不上 = 永远注入不了。
    """
    names: set = set()
    try:
        chars = store.current_memory().get("characters") or {}
    except Exception:
        return names
    for n, c in chars.items():
        names.add(str(n).strip())
        if isinstance(c, dict):
            for a in (c.get("aliases") or []):
                names.add(str(a).strip())
    names.discard("")
    return names


def _status_of(cat: str, en: dict, mem_names: set, cur_order: int, gap: int) -> tuple:
    """条目注入状态 → (标记, 说明, 是否变灰)。

    与注入侧同口径：已移除不注入 > 锁定永远注入 > 角色按名匹配 > 非角色按沉睡。
    """
    removed_at = en.get("removed_at_chapter")
    if removed_at:
        return (f"🗑 已移除·第{removed_at}章",
                "归档判定该实体已消失，不再注入（误判可点「恢复」重新注入）", True)
    if en.get("locked"):
        return ("🔒 已锁定", "锁定条目永远注入，并带「不可更改」标注", False)
    if cat == "角色":
        if str(en.get("name") or "").strip() in mem_names:
            return ("✓ 与记忆人物匹配", "该角色出场时会注入", False)
        return ("⚠ 名字对不上",
                "与当前记忆中的人物（含别名）都对不上，正文生成时不会注入。"
                "请改成小说实际使用的名字，或到记忆页给该人物加别名", True)
    seen = en.get("last_seen_chapter")
    if cur_order and gap > 0 and seen is not None and cur_order - seen >= gap:
        return ("💤 沉睡中", f"超过 {gap} 章未被正文提及，暂不注入（写到会自动恢复）", True)
    return ("", "", False)


# =====================================================
# 页面
# =====================================================

def lore_view(page, session, on_refresh=None) -> ft.Control:
    """设定库页（页签 6）。"""
    store = session.store
    gap = store.get_setting_int("auto_entity_gap", 3)
    try:
        cur_order = store.max_chapter_order()
    except Exception:
        cur_order = 0
    mem_names = _mem_names(store)

    index: list = []      # [分类, 条目, 控件, 检索干草堆]
    titles: dict = {}     # 分类 -> 标题控件（显示 命中/总数）
    counts: dict = {}     # 分类 -> 总数

    def _title_text(cat: str, shown: int) -> str:
        n = counts.get(cat, 0)
        return f"{cat}（{shown}/{n}）" if shown != n else f"{cat}（{n}）"

    tiles = []
    for cat in CATS:
        entries = store.list_lore(cat)
        counts[cat] = len(entries)
        t = ft.Text(f"{cat}（{len(entries)}）", size=14)
        titles[cat] = t
        rows: list = []
        for en in entries:
            status = _status_of(cat, en, mem_names, cur_order, gap)
            ctrl = _entry_row(page, session, cat, en, status, on_refresh)
            hay = " ".join([
                str(en.get("name") or ""), str(en.get("content") or ""),
                " ".join(str(x) for x in (en.get("tags") or [])),
            ]).lower()
            index.append([cat, en, ctrl, hay])
            rows.append(ctrl)
        if not entries:
            rows.append(ft.Container(
                ft.Text("（空）点下方「＋ 新增」开始建；角色名要用小说中实际使用的名字"
                        "才能随出场注入", size=11, color=ft.Colors.ON_SURFACE_VARIANT),
                padding=8))
        else:
            rows.append(ft.Container(
                ft.Text(f"该分类共 {len(entries)} 条", size=10,
                        color=ft.Colors.ON_SURFACE_VARIANT), padding=4))
        rows.append(ft.Container(
            ft.TextButton("＋ 新增",
                          on_click=lambda e, c=cat: entry_dialog(
                              page, session, c, None, on_refresh)),
            padding=4))
        tiles.append(ft.ExpansionTile(
            title=t, expanded=True, maintain_state=True, controls=rows))

    search = ft.TextField(
        label="快速检索", hint_text="按名称、内容或标签搜索…",
        dense=True, prefix_icon=ft.Icons.SEARCH, on_change=lambda e: _apply())

    def _apply():
        q = (search.value or "").strip().lower()
        shown: dict = {}
        for cat, _en, ctrl, hay in index:
            ok = (not q) or (q in hay)
            ctrl.visible = ok
            if ok:
                shown[cat] = shown.get(cat, 0) + 1
        for cat in CATS:
            titles[cat].value = _title_text(cat, shown.get(cat, 0))
        try:
            page.update()
        except Exception:
            pass

    def _clear(e=None):
        search.value = ""
        _apply()

    # ---------- 锁定项 ----------

    locks = store.list_locks()
    lock_rows = []
    for l in locks:
        lock_rows.append(ft.Container(ft.Row([
            ft.Container(ft.Text(str(l.get("item_type") or ""), size=10),
                         bgcolor=ft.Colors.with_opacity(0.14, ft.Colors.DEEP_PURPLE),
                         border_radius=6,
                         padding=ft.Padding(left=6, right=6, top=2, bottom=2)),
            ft.Text(str(l.get("value") or "") +
                    (f"（{l['note']}）" if l.get("note") else ""),
                    size=12, expand=True, max_lines=1,
                    overflow=ft.TextOverflow.ELLIPSIS),
            ft.IconButton(ft.Icons.LOCK_OPEN, icon_size=16, tooltip="解除锁定",
                          on_click=lambda e, x=l: lock_delete(page, session, x, on_refresh)),
        ]), padding=ft.Padding(left=4, right=0, top=3, bottom=3)))

    return ft.Column([
        ft.Row([
            ft.Text("设定库", size=17, weight=ft.FontWeight.BOLD, expand=True),
            ft.TextButton("＋ 新增", icon=ft.Icons.ADD,
                          on_click=lambda e: entry_dialog(
                              page, session, CATS[0], None, on_refresh)),
            ft.TextButton("从记忆导入", icon=ft.Icons.DOWNLOAD,
                          on_click=lambda e: import_from_memory(page, session, on_refresh)),
        ], spacing=0),
        hint("全书级别的静态百科（世界观 / 人物初始设定 / 地理 / 物品 / 术语），"
             "不随章节推进改变。角色卡按人物名与别名随出场注入；地点/物品/术语长期"
             "未被正文提及会自动沉睡（💤，写到自动恢复）；锁定条目永远注入并带"
             "「不可更改」标注。"),
        ft.Row([ft.Container(search, expand=True),
                ft.TextButton("清除", on_click=_clear)], spacing=4),
        ft.Column(tiles, spacing=0),
        section("锁定项"),
        hint("锁定项会以「不可更改」标注注入生成提示词"
             "（如人名、关键设定、结局走向、状态卡字段）。"),
        ft.Card(ft.Container(ft.Column(
            lock_rows if lock_rows else
            [ft.Container(ft.Text("没有锁定项", size=11,
                                  color=ft.Colors.ON_SURFACE_VARIANT), padding=8)],
            spacing=0), padding=6)),
        ft.Row([ft.TextButton("＋ 新增锁定项", icon=ft.Icons.LOCK,
                              on_click=lambda e: lock_add(page, session, on_refresh))]),
    ], scroll=ft.ScrollMode.AUTO, expand=True, spacing=4)


def _entry_row(page, session, cat: str, en: dict, status: tuple, on_refresh) -> ft.Control:
    """单条设定卡片：名称 + 状态标记 + 内容摘要 + 标签 + 编辑/恢复/删除。"""
    label, note, muted = status
    lines = [ft.Text(en.get("name") or "", size=13, weight=ft.FontWeight.W_600)]
    if label:
        lines.append(ft.Text(f"{label} ｜ {note}", size=10,
                             color=ft.Colors.ORANGE_700 if muted
                             else ft.Colors.ON_SURFACE_VARIANT))
    content = str(en.get("content") or "")
    if content:
        lines.append(ft.Text(content, size=11, max_lines=2,
                             overflow=ft.TextOverflow.ELLIPSIS,
                             color=ft.Colors.OUTLINE if muted
                             else ft.Colors.ON_SURFACE_VARIANT))
    tags = en.get("tags") or []
    if tags:
        lines.append(ft.Text("标签：" + "、".join(str(t) for t in tags), size=10,
                             color=ft.Colors.ON_SURFACE_VARIANT))

    acts = []
    if en.get("removed_at_chapter"):
        acts.append(ft.IconButton(ft.Icons.RESTORE, icon_size=16, tooltip="恢复条目",
                                  on_click=lambda e, x=en: restore_entry(
                                      page, session, x, on_refresh)))
    acts.append(ft.IconButton(ft.Icons.EDIT_OUTLINED, icon_size=16, tooltip="编辑",
                              on_click=lambda e, x=en, c=cat: entry_dialog(
                                  page, session, c, x, on_refresh)))
    acts.append(ft.IconButton(ft.Icons.DELETE_OUTLINE, icon_size=16, tooltip="删除",
                              on_click=lambda e, x=en: delete_entry(
                                  page, session, x, on_refresh)))
    return ft.Container(ft.Row([
        ft.Column(lines, spacing=2, expand=True),
        ft.Row(acts, spacing=0),
    ]), padding=ft.Padding(left=8, right=0, top=4, bottom=4))


# =====================================================
# 操作：条目增删改 / 恢复
# =====================================================

def entry_dialog(page, session, cat: str, entry: dict | None = None, on_refresh=None):
    """新增 / 编辑设定条目（名称 / 分类 / 内容 / 标签 / 锁定）—— 桌面 _LoreEntryDialog 同口径。"""
    store = session.store
    is_edit = bool(entry)
    name = ft.TextField(label="名称", value=str((entry or {}).get("name") or ""))
    cat_dd = ft.Dropdown(
        label="分类", value=str((entry or {}).get("category") or cat),
        options=[ft.dropdown.Option(key=c, text=c) for c in CATS], dense=True)
    content = ft.TextField(label="内容", value=str((entry or {}).get("content") or ""),
                           multiline=True, min_lines=3, max_lines=8)
    tags = ft.TextField(
        label="标签（逗号/顿号分隔）",
        value="、".join(str(t) for t in ((entry or {}).get("tags") or [])),
        hint_text="如：城市、酒馆")
    locked = ft.Checkbox(label="锁定（注入时带「不可更改」标注）",
                         value=bool((entry or {}).get("locked")))

    def _save(e):
        n = (name.value or "").strip()
        if not n:
            toast(page, "请填写名称", error=True)
            return
        patch = {"name": n, "category": cat_dd.value or cat,
                 "content": content.value or "",
                 "tags": _split_tags(tags.value or ""), "locked": bool(locked.value)}
        if is_edit:
            store.update_lore(entry["id"], patch)
        else:
            store.add_lore(patch["category"], n, content=patch["content"],
                           tags=patch["tags"], locked=patch["locked"])
        page.pop_dialog()
        toast(page, "已保存修改" if is_edit else "已新增设定条目")
        if on_refresh:
            on_refresh()

    def _cancel(e):
        page.pop_dialog()

    page.show_dialog(ft.AlertDialog(
        modal=True,
        title=ft.Text("编辑设定条目" if is_edit else "新增设定条目"),
        content=ft.Container(
            ft.Column([name, cat_dd, content, tags, locked], spacing=8,
                      scroll=ft.ScrollMode.AUTO),
            width=460, height=max(260, min(420, _sheet_height(page) - 220))),
        actions=[ft.TextButton("取消", on_click=_cancel),
                 ft.FilledButton("保存", on_click=_save)],
        actions_alignment=ft.MainAxisAlignment.END,
    ))


def delete_entry(page, session, entry: dict, on_refresh=None):
    """物理删除（用户手动操作，不可恢复）—— 与桌面 _delete_entry 同口径。"""
    def _yes():
        session.store.delete_lore(entry["id"])
        toast(page, "已删除")
        if on_refresh:
            on_refresh()
    confirm(page, "删除设定", f"确定删除「{entry.get('name')}」？删除后不可恢复。", _yes)


def restore_entry(page, session, entry: dict, on_refresh=None):
    """取消「已移除」标记（软删除的撤销），条目重新参与注入。"""
    session.store.restore_lore(entry["id"])
    toast(page, f"已恢复「{entry.get('name')}」，重新参与注入")
    if on_refresh:
        on_refresh()


def import_from_memory(page, session, on_refresh=None):
    """从记忆导入全部：预览 → 确认 → 导入 → 消除「设定资料库缺卡」提醒（闭环）。"""
    from app.core.store import lore_import_preview, lore_import_from_memory as _do_import
    store = session.store
    pending = lore_import_preview(store)
    if not pending:
        toast(page, "记忆中的人物与实体都已收录在设定库")
        return
    by_cat: dict = {}
    for c, _n, _x in pending:
        by_cat[c] = by_cat.get(c, 0) + 1
    dist = "、".join(f"{c} {n} 条" for c, n in by_cat.items())
    preview = "、".join(n for _c, n, _x in pending[:8]) + ("…" if len(pending) > 8 else "")

    def _yes():
        imported = _do_import(store)
        try:
            from app.core.diagnostics import clear_issue
            clear_issue("lore_sync")
        except Exception:
            pass
        toast(page, f"已从记忆导入 {len(imported)} 条设定（{dist}）")
        if on_refresh:
            on_refresh()

    confirm(page, "从记忆导入全部设定",
            f"将把记忆中 {len(pending)} 个对象导入设定库（{dist}）：\n{preview}\n\n"
            "已存在的同名条目自动跳过；导入内容可在设定库里继续编辑。", _yes)


# =====================================================
# 操作：锁定项
# =====================================================

def lock_add(page, session, on_refresh=None):
    """新增锁定项（类型 / 内容 / 备注）。"""
    store = session.store
    dd = ft.Dropdown(label="类型", value=LOCK_TYPES[0],
                     options=[ft.dropdown.Option(key=t, text=t) for t in LOCK_TYPES],
                     dense=True)
    value = ft.TextField(label="内容")
    note = ft.TextField(label="备注（可选）")

    def _save(e):
        v = (value.value or "").strip()
        if not v:
            toast(page, "请填写内容", error=True)
            return
        page.pop_dialog()
        store.add_lock(dd.value or LOCK_TYPES[0], v, (note.value or "").strip())
        toast(page, "已锁定")
        if on_refresh:
            on_refresh()

    def _cancel(e):
        page.pop_dialog()

    page.show_dialog(ft.AlertDialog(
        modal=True, title=ft.Text("新增锁定项"),
        content=ft.Container(ft.Column([
            hint("锁定项会以「不可更改」标注注入生成提示词。"), dd, value, note],
            spacing=8), width=440),
        actions=[ft.TextButton("取消", on_click=_cancel),
                 ft.FilledButton("锁定", on_click=_save)],
        actions_alignment=ft.MainAxisAlignment.END,
    ))


def lock_delete(page, session, lock: dict, on_refresh=None):
    """删除锁定项（带确认）。"""
    def _yes():
        session.store.remove_lock(lock["id"])
        toast(page, "已解除锁定")
        if on_refresh:
            on_refresh()
    confirm(page, "删除锁定项",
            f"确定解除「{lock.get('value')}」的锁定？解除后该内容不再以"
            "「不可更改」标注注入。", _yes)
