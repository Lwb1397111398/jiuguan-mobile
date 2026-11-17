# -*- coding: utf-8 -*-
"""酒馆小说生成器 · 手机版入口（Flet）。"""
import bootstrap  # noqa: F401  必须最先执行：sys.path 注入

import flet as ft

import crashguard
from state import Session
from widgets import toast
from pages.home import HomePage
from pages.models import ModelsPage
from pages.writing import WritingPage
from pages.mine import MinePage


def _version_name() -> str:
    try:
        from _version import APP_VERSION_NAME
        return str(APP_VERSION_NAME)
    except Exception:
        return ""


def main(page: ft.Page):
    # 闪退留痕（APK 无控制台）：装在 main 第一步，之后的异常都能落到
    # 数据目录的 crash.log，入口在「我的 → 崩溃诊断」。
    crashguard.install(_version_name())
    page.title = "酒馆小说生成器"
    page.padding = 0
    page.theme_mode = ft.ThemeMode.SYSTEM
    # 品牌深蓝（与 APP 图标底色 #2A3550 一致）：Material 3 会从 seed 自动派生
    # 整套配色；比旧橙色更贴合「书本/创作」气质，明暗两套主题都稳。
    seed = "#2A3550"
    page.theme = ft.Theme(color_scheme_seed=seed)
    page.dark_theme = ft.Theme(color_scheme_seed=seed)
    # 页面底色走设计系统的语义色（theme.py）：卡片浮在底色上才分层，
    # 旧版不设 bgcolor，M3 默认 surface 与卡片同色，全靠阴影区分，手机上很糊。
    import theme as T
    page.bgcolor = T.bg()
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
        elevation=0,
        # 80 = Material 3 标准高。旧值 68 会让图标+文字贴到屏幕最下沿，
        # 系统导航栏一压就「整排下半截」看不见（2026-11-17 平板反馈）。
        height=T.APP_NAV_HEIGHT,
        # 与导航外框（app_shell 的 Container）同色：底部系统栏那一截才连成一体，
        # 不会在导航下面露出第二条页面底色的色带。
        bgcolor=T.card_bg(),
        label_behavior=ft.NavigationBarLabelBehavior.ALWAYS_SHOW,
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
    # 整屏骨架统一走 theme.app_shell：安卓 15+ 强制 edge-to-edge，不套系统栏
    # 安全区的话，底部导航会被系统导航栏（三键 48dp / 手势条 24dp）压住下半截。
    page.add(T.app_shell(body, nav))

    # 启动后 8 秒静默检查更新（24h 节流，仅 Android；桌面预览直接跳过）
    from update_ui import schedule_silent_check
    schedule_silent_check(page, session)

    # 云同步守护线程：改动静默 60 秒后自动上传当前项目（打开项目时的拉取在
    # pages/home.py 里，因为要知道开的是哪本书）。没配置时线程只是每 8 秒
    # 读一次全局设置就返回，开销可以忽略。
    import threading
    import sync_ui
    watcher = sync_ui.Watcher(page, session)
    watcher.start()

    def _flush_async():
        threading.Thread(target=watcher.flush, daemon=True).start()

    # 退到后台 / 断开时尽力推一次（安卓可能直接杀进程，所以只是尽力）。
    # flush 内部再开线程并在自己线程里 join，这里绝不能让事件线程等网络。
    def _on_lifecycle(e):
        try:
            st = str(getattr(e, "state", "")).upper()
        except Exception:
            return
        if any(k in st for k in ("HIDE", "PAUSE", "DETACH")):
            _flush_async()

    try:
        page.on_app_lifecycle_state_change = _on_lifecycle
        page.on_disconnect = lambda e: _flush_async()
    except Exception:
        pass        # 某些平台没有这些钩子，靠 60 秒 debounce 兜底


if __name__ == "__main__":
    ft.run(main)
