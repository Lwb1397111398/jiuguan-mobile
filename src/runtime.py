# -*- coding: utf-8 -*-
"""应用级运行时：Session + 四个主页 + 「整机替换」生命周期（全量导入）。

为什么需要这一层（2026-11-20 老板点名问题）：
全量导入会把 `app.db`、`projects/` 整体换掉。旧的 `Session`、四个主页、各子页面
实例、写作台编辑器绑定、防抖定时器、自动模式引擎、流式任务，全都还指着**旧的**
库对象和旧的数据。只调 `MinePage.on_refresh` 只重建「我的」页——首页/写作台/模型页
拿着旧缓存继续显示，甚至把旧稿写进新库里同 id 的章节。所以整机替换必须走这里。

三个不变量（改任何一处先读 docs/手机版模块总览.md「全量导入的运行时治理」）：
1. `begin_import()` 是唯一入口：重复点击返回 None；先冲手改稿（还在旧库上），
   再停自动模式、流式任务、防抖定时器与云同步守护——**不许它们跨过库替换继续写**；
2. `import_succeeded(token)` 只认一次令牌：旧 Session `deactivate()`（旧页面读不到
   库、写不进库）→ 丢弃全部页面与子页面缓存 → 用新 Session 重建 → 回首页；
3. `import_failed(token)` 不重建：把 core 在替换前关掉的项目重新打开，原会话照用。

平台限制：正常路径不需要重启（进程内重建即可）。只有重建本身失败时才
`hard_restart()`（安卓拉起自家启动页 + 结束进程），并先把界面切成「已停用」的
阻断页——**绝不让用户继续点旧界面**，也绝不只弹一句「建议重启」。
"""
import os
import re
import threading

import flet as ft

from state import Session
from bridge import stop_all_tasks
import theme as T

# core 的返回文案末尾带「请重启应用使全部更改生效」；手机端改成进程内重建，
# 这句会误导用户去杀进程，展示前先摘掉（只动文案，不动 core）。
_RESTART_HINT = re.compile(r"[\s。]*请重启应用[^\n]*")


def strip_restart_hint(msg: str) -> str:
    """去掉 core 文案里的「请重启应用…」一句（手机端会自己重建页面）。"""
    return _RESTART_HINT.sub("", str(msg or "")).strip()


def is_android() -> bool:
    try:
        from update import is_android as _is_android
        return bool(_is_android())
    except Exception:
        return False


def _hard_exit():
    """直接结束进程（重启链路最后一步）。

    这里必须 `os._exit`：正常退出会触发 lifecycle/云同步 flush，拿着**已经被
    替换掉**的库再写一次，比直接退出危险得多。
    """
    os._exit(0)


def hard_restart(page=None) -> bool:
    """自动重启应用（Android）：拉起自家启动页 + 结束当前进程。

    返回 True = 已发出重启请求（进程随后退出）；False = 平台不支持（桌面预览）
    或 pyjnius 不可用——调用方必须显示阻断页，不能退化成「建议用户重启」。

    时序：先把启动请求交给 AMS（同一 task 清空重建），300ms 后再硬退出，
    让系统在本进程消失后拉起新进程跑 main()。
    """
    if not is_android():
        return False
    try:
        from jnius import autoclass
        PythonActivity = autoclass("com.flet.serious_python_android.PythonActivity")
        activity = PythonActivity.mActivity
        ctx = activity.getApplicationContext()
        pkg = str(ctx.getPackageName())
        Intent = autoclass("android.content.Intent")
        intent = ctx.getPackageManager().getLaunchIntentForPackage(pkg)
        if intent is None:
            return False
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK
                        | Intent.FLAG_ACTIVITY_CLEAR_TASK)
        ctx.startActivity(intent)
    except Exception:
        return False
    t = threading.Timer(0.35, _hard_exit)
    t.daemon = True
    t.start()
    return True


class AppRuntime:
    """进程级运行时：持有 Session / 四个主页 / 底部导航 / 导入闸门。"""

    # 页面索引（与底部导航顺序一致）
    HOME, WRITING, MODELS, MINE = 0, 1, 2, 3

    def __init__(self, page):
        self.page = page
        self.session = None
        self.body = ft.Container(expand=True, alignment=ft.Alignment.TOP_CENTER)
        self.nav = None
        self._pages = []
        self.watcher = None
        self._lock = threading.RLock()
        # ---- 全量导入状态机 ----
        self._importing = False       # 有导入在跑（禁止重复进入）
        self._import_token = 0        # 每次 begin_import +1
        self._import_done_token = 0   # 已完成（成功/失败）的令牌：同一令牌只处理一次
        self._import_prev_folder = None   # 导入前打开的项目（失败时恢复）
        self._dead_shell = False      # 重建失败后的阻断页：旧界面一律不可用

    # ================= 装配 =================

    def mount(self):
        """建 Session → 建页面 → 挂底部导航 → 起云同步/更新检查/生命周期钩子。"""
        session = Session()
        session.page = self.page
        self.session = session
        self._create_pages(session)

        nav = ft.NavigationBar(
            selected_index=0,
            elevation=0,
            # 80 = Material 3 标准高。旧值 68 会让图标+文字贴到屏幕最下沿，
            # 系统导航栏一压就「整排下半截」看不见（2026-11-17 平板反馈）。
            height=T.APP_NAV_HEIGHT,
            # 与导航外框（app_shell 的 Container）同色：底部系统栏那一截才连成一体。
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
            on_change=lambda e: self.switch(e.control.selected_index),
        )
        self.nav = nav
        self.body.content = self._pages[0].build()
        # 整屏骨架统一走 theme.app_shell：安卓 15+ 强制 edge-to-edge，
        # 不套系统栏安全区的话底部导航会被系统栏压住下半截。
        self.page.add(T.app_shell(self.body, nav))

        # 启动后 8 秒静默检查更新（24h 节流，仅 Android；桌面预览直接跳过）
        from update_ui import schedule_silent_check
        schedule_silent_check(self.page, session)

        # 云同步守护线程：改动静默 60 秒后自动上传当前项目。
        self._start_watcher()

        def _on_lifecycle(e):
            try:
                st = str(getattr(e, "state", "")).upper()
            except Exception:
                return
            if any(k in st for k in ("HIDE", "PAUSE", "DETACH")):
                self.flush_drafts()
                self._flush_sync_async()

        def _on_disconnect(e):
            self.flush_drafts()
            self._flush_sync_async()

        try:
            self.page.on_app_lifecycle_state_change = _on_lifecycle
            self.page.on_disconnect = _on_disconnect
        except Exception:
            pass        # 某些平台没有这些钩子，靠 60 秒 debounce 兜底

    def _create_pages(self, session):
        """用给定 Session 新建四个主页（导入成功后也走这里整体换血）。"""
        from pages.home import HomePage
        from pages.models import ModelsPage
        from pages.writing import WritingPage
        from pages.mine import MinePage

        pages = [
            HomePage(self.page, session,
                     on_open_project=lambda: self.switch(self.WRITING)),
            WritingPage(self.page, session),
            ModelsPage(self.page, session),
            MinePage(self.page, session, runtime=self),
        ]
        for p in pages:
            p.on_refresh = self.rebuild_current
        self._pages = pages
        return pages

    @property
    def pages(self):
        return list(self._pages)

    # ================= 导航 / 刷新 =================

    def current_index(self) -> int:
        try:
            idx = int(self.nav.selected_index or 0)
        except Exception:
            idx = 0
        return max(0, min(idx, max(0, len(self._pages) - 1)))

    def switch(self, i: int):
        """切底部导航（离开创作页前先冲刷手改稿）。"""
        if self._dead_shell:
            return          # 阻断页：任何导航都不许把旧页面拿回来
        if (self.nav.selected_index or 0) != i:
            self.flush_drafts()
        self.nav.selected_index = i
        self.body.content = self._pages[i].build()
        self.page.update()

    def rebuild_current(self):
        """整页重建当前页（页面 `on_refresh` 的唯一落点）。"""
        if self._dead_shell:
            return
        idx = self.current_index()
        self.body.content = self._pages[idx].build()
        self.page.update()

    def flush_drafts(self):
        """把写作台还没到防抖时间的手改稿立即落库（只碰本地 sqlite）。

        走全部页面里的写作页实例，而不是只认当前页——导入/切页/切后台都要用。
        """
        for p in list(self._pages):
            fn = getattr(p, "flush_editor_draft", None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass
                break

    def _flush_sync_async(self):
        """切后台/断连时尽力推一次云同步（另开线程，绝不等网络）。"""
        w = self.watcher
        if w is None:
            return
        try:
            threading.Thread(target=w.flush, daemon=True).start()
        except Exception:
            pass

    # ================= 云同步守护 =================

    def _start_watcher(self):
        self._stop_watcher()
        try:
            import sync_ui
            self.watcher = sync_ui.Watcher(self.page, self.session)
            self.watcher.start()
        except Exception:
            self.watcher = None

    def _stop_watcher(self):
        w, self.watcher = getattr(self, "watcher", None), None
        if w is not None:
            try:
                w.stop()
            except Exception:
                pass

    # ================= 全量导入：运行时治理 =================

    @property
    def importing(self) -> bool:
        return bool(self._importing)

    @property
    def dead(self) -> bool:
        return bool(self._dead_shell)

    def begin_import(self):
        """进入导入态并静默全部会写库的活动。返回令牌；None = 重复点击/已阻断。

        顺序不能换：
        1. 冲手改稿——这一步还在**旧库**上（替换后再冲就是往新库写旧稿）；
        2. 停页面级活动（自动模式、防抖定时器、编辑器绑定）；
        3. 停进程级流式任务；
        4. 停云同步守护（它会推/拉项目库）。
        """
        with self._lock:
            if self._importing or self._dead_shell:
                return None
            self._importing = True
            self._import_token += 1
            token = self._import_token
            prev = None
            if self.session is not None:
                try:
                    prev = self.session.app.project_folder
                except Exception:
                    prev = None
            self._import_prev_folder = prev

        # 1) 手改稿先落地（旧库）
        self.flush_drafts()
        # 2) 页面级：自动模式停机、取消防抖定时器、断开编辑器绑定
        self._quiesce()
        # 3) 进程级：所有流式任务置停止位
        try:
            stop_all_tasks()
        except Exception:
            pass
        # 4) 云同步守护：导入期间不再推拉
        self._stop_watcher()
        return token

    def import_succeeded(self, token: int) -> bool:
        """导入成功：旧会话/旧页面全部作废并受控重建。返回**是否真的重建成功**。

        两种情况返回 False：
        1. 同一令牌重复调用（按钮连点、回调重入）—— 只重建一次；
        2. 重建本身抛异常（已切「旧界面已停用」阻断页）—— 界面层不许再弹
           「导入完成」盖在阻断页上。两种情况下 `_import_done_token` 都已置位，
           同一令牌仍只处理一次。
        """
        with self._lock:
            if token != self._import_token or token == self._import_done_token:
                return False
            self._import_done_token = token
            self._importing = False

        old = self.session
        if old is not None:
            try:
                old.deactivate()     # 旧页面对象从此读不到库、写不进库
            except Exception:
                pass
        try:
            self._rebuild_all()
        except Exception as e:
            # 重建失败 = 旧界面绝不能再用：切阻断页 + 尽力自动重启；
            # 返回 False 让界面层不要弹「导入完成」盖在阻断页上（自相矛盾）。
            self._enter_dead_shell(f"重建页面失败：{type(e).__name__}: {e}")
            return False
        return True

    def import_failed(self, token: int) -> bool:
        """导入失败/回滚：不动页面，把 core 关掉的项目重新打开，原会话继续可用。

        返回是否本次真的处理了（同一令牌只处理一次）。
        """
        with self._lock:
            if token != self._import_token or token == self._import_done_token:
                return False
            self._import_done_token = token
            self._importing = False
            prev = self._import_prev_folder

        # core 在替换前调过 close_project（即使密码错也可能已关）：不恢复的话
        # 用户会看到「写作台没项目了」——数据没丢，但会话被打断。
        if prev and self.session is not None:
            try:
                if self.session.app.store is None and os.path.isdir(prev):
                    self.session.app.open_project(prev)
            except Exception:
                pass
        self._start_watcher()
        try:
            self.rebuild_current()
        except Exception:
            pass
        return True

    # ---------- 内部 ----------

    def _invalidate_page(self, page):
        fn = getattr(page, "invalidate", None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass

    def _dispose_pages(self):
        """页面即将被整体丢弃：先停手（invalidate），再放掉页面级资源（dispose）。"""
        for p in list(self._pages):
            fn = getattr(p, "dispose", None)
            if callable(fn):
                try:
                    fn()
                    continue
                except Exception:
                    pass
            self._invalidate_page(p)

    def _quiesce(self):
        """让所有页面及其子页面停下来（幂等，可重复调用；不丢页面资源）。"""
        for p in list(self._pages):
            self._invalidate_page(p)

    def _rebuild_all(self):
        """整机重建：丢缓存 → 新 Session → 新页面 → 回首页 → 重启云同步。"""
        self._close_all_dialogs()   # 旧弹层（接力确认/导出/进度）不许盖在新界面上
        self._dispose_pages()
        self._pages = []

        session = Session()
        session.page = self.page
        self.session = session
        self._create_pages(session)

        if self.nav is not None:
            self.nav.selected_index = self.HOME
        self.body.content = self._pages[self.HOME].build()
        self.page.update()
        self._start_watcher()

    def _close_all_dialogs(self):
        """关掉所有还开着的弹层（Flet 返回 None 表示栈已空）。"""
        for _ in range(8):
            try:
                if self.page.pop_dialog() is None:
                    break
            except Exception:
                break

    def _enter_dead_shell(self, reason: str = ""):
        """阻断页：旧界面整体停用（不留任何可点回旧页面的入口）。"""
        self._dead_shell = True
        self._stop_watcher()
        self._close_all_dialogs()
        try:
            if self.nav is not None:
                self.nav.selected_index = self.HOME
        except Exception:
            pass
        try:
            self.body.content = self._dead_view(reason)
            self.page.update()
        except Exception:
            pass
        # 先画阻断页再尝试重启：重启失败也不会把旧界面留给用户
        if hard_restart(self.page):
            return
        try:
            from widgets import toast
            toast(self.page, "请手动关闭并重新打开应用")
        except Exception:
            pass

    def _dead_view(self, reason: str = "") -> ft.Control:
        def _retry(ev):
            if hard_restart(self.page):
                return
            try:
                from widgets import toast
                toast(self.page, "请手动关闭并重新打开应用", error=True)
            except Exception:
                pass

        rows = [
            ft.Icon(ft.Icons.LOCK_RESET, size=44, color=T.brand()),
            ft.Text("资料已替换，旧界面已停用", size=16,
                    weight=ft.FontWeight.W_600, text_align=ft.TextAlign.CENTER),
            ft.Text("为了保证只使用导入后的数据，旧页面不再可用。"
                    "请重新打开应用继续使用。"
                    + (f"\n\n{reason}" if reason else ""),
                    size=12, color=T.muted(), text_align=ft.TextAlign.CENTER),
            T.primary("重新打开应用", icon=ft.Icons.RESTART_ALT, on_click=_retry),
        ]
        return ft.Container(
            ft.Column(rows, spacing=T.GAP_M, tight=True,
                      horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            alignment=ft.Alignment.CENTER, padding=T.GAP_XL, expand=True)

    # ================= 退出 =================

    def shutdown(self):
        """退出/测试收尾：停守护线程、停生成、关项目（幂等）。"""
        self._stop_watcher()
        self._dispose_pages()
        try:
            stop_all_tasks()
        except Exception:
            pass
        if self.session is not None:
            try:
                self.session.app.close_all_parked()
            except Exception:
                pass
            try:
                self.session.close_project()
            except Exception:
                pass
