# -*- coding: utf-8 -*-
"""项目页：最近项目列表、新建项目、打开/移除。"""
import datetime
import os

import flet as ft

import health
from state import safe_folder_name
from widgets import section, hint, empty, toast, confirm, page_header


class HomePage:
    def __init__(self, page, session, on_open_project):
        self.page = page
        self.session = session
        self.on_open_project = on_open_project  # 打开项目后回调（切到写作页）
        self.on_refresh = None                  # main() 注入：整页重建

    # ---------- 动作 ----------

    def _open(self, path: str):
        try:
            self.session.open_project(path)
            toast(self.page, f"已打开《{self.session.store.title}》")
            self.on_open_project()
        except FileNotFoundError:
            toast(self.page, "项目文件夹不存在", error=True)
            return
        except Exception as e:
            toast(self.page, f"打开失败：{e}", error=True)
            return
        # 云同步：云端有更新的版本就合并下来（只拉不推，异步）。
        # 放在切页之后：先让用户看到本地内容，合并完再重建一次页面。
        import sync_ui
        sync_ui.pull_on_open(self.page, self.session,
                             on_pulled=self.on_refresh)

    def _new_project(self, e):
        name_field = ft.TextField(label="作品名", autofocus=True)

        def _cancel(ev):
            self.page.pop_dialog()

        def _create(ev):
            name = (name_field.value or "").strip()
            if not name:
                toast(self.page, "请填写作品名", error=True)
                return
            self.page.pop_dialog()
            try:
                self.session.create_project(name)
                toast(self.page, f"已创建《{name}》")
                self.on_open_project()
            except Exception as ex:
                toast(self.page, f"创建失败：{ex}", error=True)

        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("新建项目"),
            content=ft.Column([ft.Text("项目将创建在本应用的数据目录中，"
                                       "可通过「我的 → 全量备份」与电脑同步。", size=13),
                               name_field], tight=True, spacing=10),
            actions=[ft.TextButton("取消", on_click=_cancel),
                     ft.FilledButton("创建", on_click=_create)],
            actions_alignment=ft.MainAxisAlignment.END,
        ))

    def _remove(self, path: str):
        def _yes():
            self.session.app.remove_recent(path)
            self.page.update()
        confirm(self.page, "移除记录",
                "只移除这条最近项目记录，不删除项目文件夹里的任何数据。", _yes)

    # ---------- 构建 ----------

    def build(self) -> ft.Control:
        s = self.session
        head = page_header("我的作品", trailing=ft.FilledButton(
            "＋ 新建项目", on_click=self._new_project))
        items = []
        for r in s.app.list_recent():
            exists = r.get("exists")
            last = r.get("last_open", "")
            try:
                last = datetime.datetime.fromisoformat(last).strftime("%m-%d %H:%M")
            except (ValueError, TypeError):
                last = ""
            sub = (r.get("path") or "") + (f" ｜ {last}打开" if last else "")
            card = ft.Card(ft.Container(
                ft.Row([
                    ft.Icon(ft.Icons.MENU_BOOK if exists else ft.Icons.FOLDER_OFF,
                            color=ft.Colors.PRIMARY if exists
                            else ft.Colors.ON_SURFACE_VARIANT),
                    ft.Column([
                        ft.Text(r.get("title") or "未命名", size=15,
                                weight=ft.FontWeight.W_600,
                                color=None if exists else ft.Colors.ON_SURFACE_VARIANT),
                        ft.Text(sub, size=11, color=ft.Colors.ON_SURFACE_VARIANT,
                                max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    ], spacing=2, expand=True),
                    *( [ft.IconButton(ft.Icons.CHEVRON_RIGHT,
                                      on_click=lambda e, p=r["path"]: self._open(p))]
                       if exists else
                       [ft.IconButton(ft.Icons.DELETE_OUTLINE,
                                      on_click=lambda e, p=r["path"]: self._remove(p))] ),
                ]),
                padding=ft.Padding(left=12, right=12, top=10, bottom=10),
            ), margin=ft.Margin(left=0, right=0, top=0, bottom=8))
            items.append(card)

        if s.has_project:
            cur = ft.Container(
                ft.Row([
                    ft.Icon(ft.Icons.PLAY_CIRCLE, color=ft.Colors.PRIMARY),
                    ft.Text(f"当前：《{s.store.title}》", expand=True, size=14),
                    ft.TextButton("关闭", on_click=lambda e: (
                        s.close_project(), self.page.update())),
                ]),
                bgcolor=ft.Colors.with_opacity(0.08, ft.Colors.PRIMARY),
                border_radius=10, padding=12, margin=ft.Margin(left=0, right=0, top=0, bottom=10),
            )
        else:
            cur = ft.Container()

        banner = health.banner(self.page, s, self.on_refresh)
        body = ft.Column([
            head,
            *([banner] if banner else []),
            cur,
            section("最近项目"),
            *(items if items else [empty("还没有项目\n点「＋ 新建项目」开始创作，"
                                         "或用「我的 → 导入全量备份」从电脑同步")]),
        ], scroll=ft.ScrollMode.AUTO, expand=True, spacing=0)
        return ft.Container(body, padding=14, expand=True)
