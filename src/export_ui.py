# -*- coding: utf-8 -*-
"""导出（Flet）：正文 TXT/Markdown/DOCX（可选章范围）+ 项目 zip 导出 / 导入。

业务逻辑全部走 `app.core.exporter`（与桌面导出页同源），本模块只做手机端交互：

- **正文导出**：BottomSheet 面板选「格式 / 章节范围 / 包含总纲 / 包含本章概要」，
  输出固定落到 `<数据目录>/exports/`（手机没有"另存为"对话框，固定目录最省事）；
- **项目 zip**：单个项目打包（`project.db`），用于跨设备搬一本小说；
  不含全局配置与 API Key（那部分由「全量备份」负责）。

DOCX：安卓端装不上 python-docx（依赖 lxml 的 C 扩展，无 arm64 wheel），
core 里已内置零依赖极简 DOCX 回退，手机同样能导出 Word 文档。
"""
import datetime
import os

import flet as ft

from widgets import section, hint, toast, confirm
from bridge import run_bg
from state import safe_folder_name

FORMATS = (("TXT", "纯文本 .txt"),
           ("Markdown", "Markdown .md（保留章节结构）"),
           ("DOCX", "Word 文档 .docx"))
_EXT = {"TXT": "txt", "Markdown": "md", "DOCX": "docx"}


def exports_dir(session) -> str:
    """导出目录（不存在则创建）。"""
    d = os.path.join(session.app.data_dir, "exports")
    os.makedirs(d, exist_ok=True)
    return d


def split_result(r) -> tuple[bool, str]:
    """归一化 run_bg 回调结果：core 返回 (ok, msg)，异常兜底返回 dict。

    直接 `ok, msg = r` 在 r 是 dict 时会解包出键名（恒真），这里统一处理。
    """
    if isinstance(r, dict):
        return bool(r.get("ok")), str(r.get("error") or r.get("msg") or "未知错误")
    if isinstance(r, (tuple, list)) and len(r) >= 2:
        return bool(r[0]), str(r[1])
    return False, "未知错误"


def _sheet(page, content: ft.Control):
    page.show_dialog(ft.BottomSheet(
        content=ft.Container(content, padding=14),
        show_drag_handle=True, use_safe_area=True))


def _height(page, ratio=0.5) -> int:
    return int((getattr(page, "height", 720) or 720) * ratio)


# =====================================================
# 正文导出
# =====================================================

def export_prose_flow(page, session):
    """打开正文导出面板（格式 / 章范围 / 选项）。"""
    s = session
    if not s.has_project:
        toast(page, "请先打开一个项目", error=True)
        return
    store = s.store
    chapters = sorted(store.list_chapters(),
                      key=lambda c: c.get("order_index") or 0)
    if not chapters:
        toast(page, "本项目还没有章节（先在分卷/大纲/写作台生成）", error=True)
        return
    title = store.title or "未命名"
    opts = [(c["id"], f"第{c.get('order_index')}章 {c.get('title') or ''}".rstrip())
            for c in chapters]

    fmt_dd = ft.Dropdown(label="导出格式", dense=True, value="TXT",
                         options=[ft.dropdown.Option(key=k, text=t) for k, t in FORMATS])
    start_dd = ft.Dropdown(label="起始章", dense=True, value=str(opts[0][0]),
                           options=[ft.dropdown.Option(key=str(i), text=t) for i, t in opts])
    end_dd = ft.Dropdown(label="结束章", dense=True, value=str(opts[-1][0]),
                         options=[ft.dropdown.Option(key=str(i), text=t) for i, t in opts])
    range_row = ft.Row([start_dd, end_dd], spacing=8, visible=False)

    def _scope_change(e):
        range_row.visible = (scope_dd.value == "指定范围")
        try:
            page.update()
        except Exception:
            pass

    scope_dd = ft.Dropdown(label="章节范围", dense=True, value="全部章节",
                           on_select=_scope_change,
                           options=[ft.dropdown.Option(key="全部章节", text="全部章节"),
                                    ft.dropdown.Option(key="指定范围", text="指定范围")])
    syn_cb = ft.Checkbox(label="包含总纲", value=False)
    out_cb = ft.Checkbox(label="包含本章概要", value=False)

    def _do(ev):
        fmt = fmt_dd.value or "TXT"
        cids = None
        if scope_dd.value == "指定范围":
            oi = {c["id"]: (c.get("order_index") or 0) for c in chapters}
            a, b = oi.get(int(start_dd.value or 0)), oi.get(int(end_dd.value or 0))
            if a is None or b is None:
                toast(page, "请选择有效的起止章", error=True)
                return
            lo, hi = min(a, b), max(a, b)
            cids = [c["id"] for c in chapters if lo <= (c.get("order_index") or 0) <= hi]
            if not cids:
                toast(page, "所选范围内没有章节", error=True)
                return
        path = os.path.join(exports_dir(s), f"{safe_folder_name(title)}.{_EXT[fmt]}")
        try:
            page.pop_dialog()
        except Exception:
            pass
        toast(page, "正在导出…")

        def job():
            from app.core.exporter import export_prose
            return export_prose(store, fmt, path, chapter_ids=cids,
                                include_synopsis=bool(syn_cb.value),
                                include_outline=bool(out_cb.value))

        def done(r):
            ok, msg = split_result(r)
            if not ok:
                toast(page, msg, error=True)
                return
            extra = ("Word 文档可直接用手机 WPS 打开；若设备缺少 python-docx，"
                     "程序已自动改用内置极简 DOCX 写入器。"
                     if fmt == "DOCX" else
                     "Markdown 保留章节结构，传到电脑可直接排版。")
            page.show_dialog(ft.AlertDialog(
                modal=True, title=ft.Text("导出完成"),
                content=ft.Text(f"{msg}\n\n文件位置：{path}\n\n{extra}", size=13),
                actions=[ft.FilledButton("好", on_click=lambda e: page.pop_dialog())],
                actions_alignment=ft.MainAxisAlignment.END))

        run_bg(page, job, done)

    body = ft.Column([
        section("导出作品正文"),
        hint("只导出「已接受 / 已锁定」的正文块；章名与分卷标题会一并带上。"),
        fmt_dd,
        scope_dd,
        range_row,
        ft.Row([syn_cb, out_cb], spacing=12, wrap=True),
        hint(f"输出目录：{exports_dir(s)}"),
        ft.Row([ft.FilledButton("导出", on_click=_do),
                ft.TextButton("取消", on_click=lambda e: page.pop_dialog())],
               alignment=ft.MainAxisAlignment.END, spacing=8),
    ], spacing=8, scroll=ft.ScrollMode.AUTO, height=_height(page, 0.62))
    _sheet(page, body)


# =====================================================
# 项目 zip（单项目打包 / 导入）
# =====================================================

def export_project_flow(page, session):
    """导出当前项目为 zip。"""
    s = session
    if not s.has_project:
        toast(page, "请先打开一个项目", error=True)
        return
    store = s.store
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(exports_dir(s),
                        f"{safe_folder_name(store.title or '未命名')}_项目_{stamp}.zip")
    toast(page, "正在打包项目…")

    def job():
        from app.core.exporter import export_project_zip
        return export_project_zip(store, path)

    def done(r):
        ok, msg = split_result(r)
        if not ok:
            toast(page, msg, error=True)
            return
        page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("项目导出完成"),
            content=ft.Text(
                f"{msg}\n\n文件位置：{path}\n\n项目 zip 只含内容数据（大纲/正文/记忆），"
                "不含全局配置与 API Key；换设备时用「导入项目」载入，"
                "与现有项目并存不会覆盖。", size=13),
            actions=[ft.FilledButton("好", on_click=lambda e: page.pop_dialog())],
            actions_alignment=ft.MainAxisAlignment.END))

    run_bg(page, job, done)


def _unique_folder(session, zip_path: str) -> str:
    """为导入的项目挑一个不重名的目标文件夹（避免覆盖同名项目）。"""
    base = safe_folder_name(os.path.splitext(os.path.basename(zip_path))[0])
    root = os.path.join(session.app.data_dir, "projects")
    dest = os.path.join(root, base)
    i = 2
    while os.path.exists(dest):
        dest = os.path.join(root, f"{base}_{i}")
        i += 1
    return dest


def import_project_flow(page, session, path: str, on_done=None):
    """导入项目 zip（来自文件选择器）。与现有项目并存，不覆盖。"""
    if not path:
        toast(page, "未能读取所选文件", error=True)
        return

    def _yes():
        toast(page, "正在导入项目…")
        dest = _unique_folder(session, path)

        def job():
            from app.core.exporter import import_project_zip
            st = import_project_zip(path, dest)
            return {"ok": True, "path": dest, "title": getattr(st, "title", "")}

        def done(r):
            if not isinstance(r, dict) or not r.get("ok"):
                ok, msg = split_result(r)
                toast(page, f"导入失败：{msg}", error=True)
                return
            opened = True
            try:
                session.open_project(r["path"])
                session.app.add_recent(r["path"], r.get("title") or "")
            except Exception as e:
                opened = False
                toast(page, f"项目已导入，但打开失败：{e}", error=True)
            page.show_dialog(ft.AlertDialog(
                modal=True, title=ft.Text("项目导入完成"),
                content=ft.Text(
                    f"《{r.get('title') or '未命名'}》已导入到：\n{r['path']}"
                    + ("\n\n已切换为当前项目。" if opened else ""), size=13),
                actions=[ft.FilledButton(
                    "好", on_click=lambda e: (
                        page.pop_dialog(),
                        on_done() if on_done else None))],
                actions_alignment=ft.MainAxisAlignment.END))

        run_bg(page, job, done)

    confirm(page, "导入项目 zip",
            "将从该 zip 还原一个项目（含分卷/大纲/正文/记忆），与现有项目并存，"
            "不会覆盖任何已有项目。确定继续？", _yes, yes_text="导入")
