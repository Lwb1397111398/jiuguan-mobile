# -*- coding: utf-8 -*-
"""自更新 UI 接线：启动静默检查 + 更新弹窗（BottomSheet）。

纯逻辑在 update.py；本文件只管控件与页面交互，设置页与启动流程共用。
下载进度回调来自工作线程，控件刷新走 page.update()（bridge.py 已验证跨线程安全）。
"""
import threading
import time

import flet as ft

from update import (
    NetworkError,
    NoReleaseError,
    check_update,
    download_update,
    get_local_version,
    install_apk,
    is_android,
    should_auto_check,
)
from widgets import toast

CHECK_DELAY = 8  # 启动后延迟秒数：避开启动高峰，也避免弹窗打断首屏


# ---------- 启动静默检查 ----------

def schedule_silent_check(page, session):
    """启动延迟静默检查（24h 节流、仅 Android）：查到新版直接弹更新弹窗。"""
    if not is_android():
        return
    try:
        last = session.app.get_setting("update_last_check", "")
    except Exception:
        last = ""
    if not should_auto_check(last):
        return
    threading.Timer(CHECK_DELAY, _silent_check, (page, session)).start()


def _silent_check(page, session):
    """后台线程：静默检查，任何失败都不打扰用户。"""
    try:
        local_code, _ = get_local_version()
        info = check_update(local_code)
        session.app.set_setting("update_last_check", str(time.time()))
    except Exception:
        return  # 静默：网络失败/云端没发布都跳过，等下次启动
    if info is None:
        return
    try:
        show_update_sheet(page, info)
    except Exception:
        pass


# ---------- 手动检查（设置页「检查更新」按钮） ----------

def manual_check(page):
    """设置页入口。返回 job/done 两个函数，交 run_bg 执行。"""
    def job():
        try:
            local_code, _ = get_local_version()
            info = check_update(local_code)
        except NoReleaseError:
            return {"kind": "norelease"}
        except NetworkError as e:
            return {"kind": "network", "message": str(e)}
        except Exception as e:
            return {"kind": "error", "message": f"{type(e).__name__}: {e}"}
        return {"kind": "checked", "info": info}

    def done(r):
        r = r if isinstance(r, dict) else {"kind": "error", "message": str(r)}
        kind = r.get("kind")
        if kind == "checked":
            info = r.get("info")
            if info is None:
                toast(page, "已是最新版")
            else:
                try:
                    show_update_sheet(page, info)
                except Exception:
                    pass
        elif kind == "norelease":
            toast(page, "云端还没有发布过任何版本（等 CI 发版后再试）")
        elif kind == "network":
            toast(page, f"检查失败：{r.get('message')}", error=True)
        else:
            toast(page, f"检查失败：{r.get('message')}", error=True)

    return job, done


# ---------- 更新弹窗 ----------

def show_update_sheet(page, info):
    """弹「发现新版本」BottomSheet：下载（进度条）→ 安装（系统安装器）。"""
    flow = _UpdateFlow(page, info)
    page.show_dialog(ft.BottomSheet(
        content=flow.build(), show_drag_handle=True, use_safe_area=True))


class _UpdateFlow:
    """一个弹窗对应一个实例：持有控件引用，下载线程只碰这些控件。"""

    def __init__(self, page, info):
        self.page = page
        self.info = info
        self._path = None
        self._bar = ft.ProgressBar(value=0, visible=False)
        self._status = ft.Text("", size=12,
                               color=ft.Colors.ON_SURFACE_VARIANT)
        self._download_btn = ft.FilledButton("下载并安装",
                                             on_click=self._start_download)
        self._install_btn = ft.FilledButton("安装更新", visible=False,
                                            on_click=self._install)
        self._close_btn = ft.TextButton("稍后", on_click=self._close)

    def build(self):
        cur, _ = get_local_version()
        parts = [
            ft.Row([
                ft.Icon(ft.Icons.SYSTEM_UPDATE, color=ft.Colors.PRIMARY),
                ft.Text(f"发现新版本 {self.info.version_name}", size=16,
                        weight=ft.FontWeight.BOLD, expand=True),
            ]),
            ft.Text(f"当前 build {cur}  →  新版 build {self.info.version_code}",
                    size=13),
        ]
        if self.info.notes:
            # 更新说明（最近提交标题）可能多行：限高滚动，弹窗不撑爆手机屏
            parts.append(ft.Column(
                [ft.Text(self.info.notes, size=12, selectable=True)],
                height=140, scroll=ft.ScrollMode.AUTO))
        parts += [
            self._bar, self._status,
            ft.Row([
                self._close_btn,
                ft.Container(expand=True),
                self._download_btn, self._install_btn,
            ]),
        ]
        return ft.Container(ft.Column(parts, spacing=8),
                            padding=16, width=520)

    # ---------- 下载 ----------

    def _start_download(self, e=None):
        self._download_btn.visible = False
        self._bar.visible = True
        self._status.value = "正在下载更新包…"
        try:
            self.page.update()
        except Exception:
            return
        from bridge import run_bg
        run_bg(self.page, self._download_job, self._download_done)

    def _download_job(self):
        def on_progress(done, total):
            if total:
                self._bar.value = min(1.0, done / total)
                self._status.value = f"正在下载更新包… {int(done / total * 100)}%"
            else:
                self._bar.value = None  # 服务端没给长度：不确定进度模式
            try:
                self.page.update()
            except Exception:
                pass

        return download_update(self.info, progress=on_progress)

    def _download_done(self, result):
        # run_bg 兜底约定：异常被包装成 {"ok": False, "error": ...}
        if isinstance(result, dict) and result.get("ok") is False:
            self._bar.visible = False
            self._bar.value = 0
            self._status.value = "下载失败：" + str(result.get("error") or "网络异常")
            self._download_btn.visible = True
            self._download_btn.content = ft.Text("重试下载")
        else:
            self._path = result
            self._bar.visible = False
            self._status.value = ("下载完成。点「安装更新」唤起系统安装器；"
                                  "若提示去开「安装未知应用」权限，开启后回来再点一次。")
            self._install_btn.visible = True
        try:
            self.page.update()
        except Exception:
            pass

    # ---------- 安装 ----------

    def _install(self, e=None):
        if not self._path:
            return
        try:
            started = install_apk(self._path)
        except Exception as ex:
            toast(self.page, f"安装失败：{ex}", error=True)
            return
        if started:
            try:
                self.page.pop_dialog()
            except Exception:
                pass
        else:
            toast(self.page, "已打开「安装未知应用」开关页："
                             "允许本应用安装后，回来再点一次「安装更新」")

    def _close(self, e=None):
        try:
            self.page.pop_dialog()
        except Exception:
            pass
