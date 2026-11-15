# -*- coding: utf-8 -*-
"""酒馆小说生成器 · 手机版入口（Flet）。"""
import bootstrap  # noqa: F401  必须最先执行：sys.path 注入

import flet as ft

from state import Session
from widgets import toast
from pages.home import HomePage
from pages.models import ModelsPage
from pages.writing import WritingPage
from pages.mine import MinePage


def main(page: ft.Page):
    page.title = "酒馆小说生成器"
    page.padding = 0
    page.theme_mode = ft.ThemeMode.SYSTEM
    # 品牌深蓝（与 APP 图标底色 #2A3550 一致）：Material 3 会从 seed 自动派生
    # 整套配色；比旧橙色更贴合「书本/创作」气质，明暗两套主题都稳。
    seed = "#2A3550"
    page.theme = ft.Theme(color_scheme_seed=seed)
    page.dark_theme = ft.Theme(color_scheme_seed=seed)
    # 不指定 font_family：安卓自动用系统中文字体（旧值 "Microsoft YaHei UI"
    # 在安卓上不存在，会触发不可控 fallback）

    session = Session()
    session.page = page

    body = ft.Container(expand=True, alignment=ft.Alignment.TOP_CENTER)

    home = HomePage(page, session, on_open_project=lambda: _switch(1))
    writing = WritingPage(page, session)
    models = ModelsPage(page, session)
    mine = MinePage(page, session)
    for p in (home, writing, models, mine):
        p.on_refresh = lambda: _rebuild_current()
    _pages = [home, writing, models, mine]

    def _rebuild_current():
        body.content = _pages[nav.selected_index or 0].build()
        page.update()

    nav = ft.NavigationBar(
        selected_index=0,
        destinations=[
            ft.NavigationBarDestination(icon=ft.Icons.BOOK_OUTLINED,
                                        selected_icon=ft.Icons.BOOK,
                                        label="项目"),
            ft.NavigationBarDestination(icon=ft.Icons.EDIT_NOTE_OUTLINED,
                                        selected_icon=ft.Icons.EDIT_NOTE,
                                        label="创作"),
            ft.NavigationBarDestination(icon=ft.Icons.SMART_TOY_OUTLINED,
                                        selected_icon=ft.Icons.SMART_TOY,
                                        label="模型"),
            ft.NavigationBarDestination(icon=ft.Icons.PERSON_OUTLINE,
                                        selected_icon=ft.Icons.PERSON,
                                        label="我的"),
        ],
        on_change=lambda e: _switch(e.control.selected_index),
    )

    def _switch(i: int):
        nav.selected_index = i
        body.content = _pages[i].build()
        page.update()

    body.content = home.build()
    page.add(ft.Column([body, nav], expand=True, spacing=0))

    # 启动后 8 秒静默检查更新（24h 节流，仅 Android；桌面预览直接跳过）
    from update_ui import schedule_silent_check
    schedule_silent_check(page, session)


if __name__ == "__main__":
    ft.run(main)
