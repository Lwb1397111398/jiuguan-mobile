# -*- coding: utf-8 -*-
"""项目页：最近项目列表、新建项目、打开/移除。"""
import datetime
import os

import flet as ft

import health
import theme as T
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
            # 写云端墓碑：电脑上的这本书会在下次同步时移进回收站。
            # 必须在 remove_recent 之前调，文件夹还在才能读出 project_uuid。
            from app.core.sync_engine import mark_deleted_bg
            mark_deleted_bg(self.session.app, path)
            self.session.app.remove_recent(path)
            self.page.update()
        confirm(self.page, "移除记录",
                "本机只移除这条记录（文件夹保留），但会同步删除到其它设备："
                "它们会把这本书移进 .sync_trash 回收站，不真删；"
                "云端数据仍保留，可反悔。", _yes)

    # ---------- 构建 ----------

    def build(self) -> ft.Control:
        s = self.session
        recents = list(s.app.list_recent() or [])
        sub = (f"共 {len(recents)} 本，最近打开的排在最前" if recents
               else "还没有作品")
        head = T.page_head("我的作品", sub,
                           trailing=T.primary("新建项目", icon=ft.Icons.ADD,
                                              on_click=self._new_project))
        items = []
        for r in recents:
            exists = r.get("exists")
            last = r.get("last_open", "")
            try:
                last = datetime.datetime.fromisoformat(last).strftime("%m-%d %H:%M")
            except (ValueError, TypeError):
                last = ""
            sub = (r.get("path") or "") + (f" ｜ {last}打开" if last else "")
            tip = "" if exists else "文件夹不在本机：可重新导入备份，或点右侧移除记录"
            card = T.card(ft.Row([
                ft.Container(
                    ft.Icon(ft.Icons.MENU_BOOK if exists else ft.Icons.FOLDER_OFF,
                            size=20, color=T.brand() if exists else T.muted()),
                    width=38, height=38, border_radius=T.R_MD,
                    bgcolor=T.soft(T.brand() if exists else T.muted(), 0.10),
                    alignment=ft.Alignment.CENTER),
                ft.Column([
                    ft.Text(r.get("title") or "未命名", size=15,
                            weight=ft.FontWeight.W_600,
                            color=None if exists else T.muted()),
                    ft.Text(sub, size=11, color=T.muted(),
                            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    *([ft.Text(tip, size=10, color=T.danger(),
                               max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)]
                      if tip else []),
                ], spacing=2, expand=True, tight=True),
                *( [ft.Icon(ft.Icons.CHEVRON_RIGHT, size=17,
                            color=ft.Colors.with_opacity(0.45, ft.Colors.ON_SURFACE))]
                   if exists else
                   [ft.IconButton(ft.Icons.DELETE_OUTLINE, icon_size=18,
                                  icon_color=T.muted(),
                                  tooltip="移除记录",
                                  on_click=lambda e, p=r["path"]: self._remove(p))] ),
            ], spacing=T.GAP_M,
                vertical_alignment=ft.CrossAxisAlignment.CENTER),
                padding=ft.Padding(left=T.GAP_M, right=T.GAP_S,
                                   top=T.GAP_S + 2, bottom=T.GAP_S + 2),
                # 整行可点：手机/平板上没人会专门去戳最右边那个小箭头
                # （2026-11-16 老板反馈「最近项目点不动」，当时只有箭头带事件）
                on_click=(lambda e, p=r["path"]: self._open(p)) if exists else None)
            items.append(card)

        if s.has_project:
            cur = T.card(ft.Row([
                ft.Icon(ft.Icons.PLAY_CIRCLE, color=T.brand(), size=20),
                ft.Text(f"当前：《{s.store.title}》", expand=True, size=14),
                T.ghost("关闭", on_click=lambda e: (
                    s.close_project(), self.page.update())),
            ], spacing=T.GAP_S, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                tone="brand")
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
        ], scroll=ft.ScrollMode.AUTO, expand=True, spacing=0,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        return ft.Container(body, padding=T.GAP_L, expand=True)
