# -*- coding: utf-8 -*-
"""自更新 UI 接线：启动静默检查 + 更新弹窗（BottomSheet）。

纯逻辑在 update.py；本文件只管控件与页面交互，设置页与启动流程共用。
下载进度回调来自工作线程，控件刷新走 page.update()（bridge.py 已验证跨线程安全）。

2026-11-16 老板平板：pyjnius 桥接整链失败，「安装更新」唤不起系统安装器，
旧的降级文案让用户「从电脑上装」——可主力设备就是平板，等于没修。
现在弹窗主推「用平板浏览器下载安装」：走 Flet 的 UrlLauncher（Flutter
插件 Java 侧，与 pyjnius 无关），把 APK 直链交给平板浏览器/下载管理器，
下载完点通知栏安装，全程不用电脑；同时保留 App 内一键安装（桥接恢复时
自动可用），一键安装失败也会自动切到浏览器通路。

2026-11-17 第一阶段（完整性）：弹窗把**官方 Release 地址 + SHA-256**摊给用户
核对；浏览器通路的文案明确写「无法自动校验」（浏览器下载的文件 App 拿不到，
不许谎称已校验）；App 内下载/复用/安装三处都校验 SHA-256，校验不通过则
删文件、不唤起安装器。
"""
import threading
import time

import flet as ft

import theme as T
from update import (
    ChecksumError,
    NetworkError,
    NoReleaseError,
    _remove_quiet as _rm,
    browser_url_or_latest,
    check_update,
    download_update,
    format_sha256,
    get_local_version,
    install_apk,
    is_android,
    official_apk_url,
    official_release_page,
    should_auto_check,
)
from widgets import toast

CHECK_DELAY = 8  # 启动后延迟秒数：避开启动高峰，也避免弹窗打断首屏


# ---------- 检查更新卡片（「我的」页顶部 + 设置页共用） ----------

def update_card(page, session) -> ft.Control:
    """「当前版本 + 检查更新」卡片。

    2026-11-16 从设置页最底部提升为「我的」页顶部第一张卡（老板：更新按钮
    不该藏在二十张设置卡片后面）；设置页保留同款卡片（引用同一实现）。
    桌面预览模式显示「仅手机版可用」。"""
    from update import (LATEST_MIRROR_URL, get_local_version, is_android,
                        jnius_diag, platform_diag)
    code, name = get_local_version()
    android = is_android()
    status = ft.Text("", size=12, color=T.muted())
    ver_text = (f"{name}（build {code}）" if android
                else "电脑预览（更新仅手机版可用）")

    async def _open_latest(e=None):
        """不经过「检查更新」，直接在平板浏览器打开最新安装包。

        破解自更新的引导死循环：修自更新自身的 bug 只在新版生效，旧版
        拉不到修复版；卡片上永远留一条能手动装最新版的路（平板内完成）。
        """
        if not is_android():
            toast(page, "仅手机版可用（电脑预览模式）")
            return
        try:
            await ft.UrlLauncher().launch_url(
                LATEST_MIRROR_URL,
                mode=ft.LaunchMode.EXTERNAL_APPLICATION)
            status.value = ("已打开平板浏览器，下载完点通知栏安装即可。"
                            "浏览器下载无法自动校验，安装前请到官方 Release 页"
                            "核对 SHA-256。")
        except Exception:
            status.value = ("浏览器没打开成功，请长按复制这条链接到浏览器："
                            + LATEST_MIRROR_URL)
        try:
            page.update()
        except Exception:
            pass

    def _check(e=None):
        if not is_android():
            toast(page, "检查更新仅手机版可用（电脑预览模式）")
            return
        status.value = "正在检查更新…"
        try:
            page.update()
        except Exception:
            return
        job, done = manual_check(page)
        from bridge import run_bg
        run_bg(page, job, done)

    rows = [
        ft.Row([
            ft.Container(ft.Icon(ft.Icons.SYSTEM_UPDATE_ALT, color=T.brand(),
                                 size=19),
                         width=34, height=34, border_radius=T.R_MD,
                         bgcolor=T.soft(T.brand(), 0.10),
                         alignment=ft.Alignment.CENTER),
            ft.Column([
                ft.Text("检查更新", size=15, weight=ft.FontWeight.W_600),
                ft.Text(f"当前版本 {ver_text}", size=11, color=T.muted()),
            ], spacing=2, expand=True),
            T.tonal("检查", on_click=_check),
        ], spacing=T.GAP_S, vertical_alignment=ft.CrossAxisAlignment.CENTER),
    ]
    diag = jnius_diag() if android else ""
    if diag:
        # pyjnius 桥接异常时显示一行可复制的诊断（老板截图即可定位真因，
        # 2026-11-16 平板真机 pyjnius 整链失败就是靠这个抓回来的）
        rows.append(ft.Text(f"桥接诊断：{diag}（不影响下面的浏览器安装）",
                            size=10, color=T.muted(), selectable=True))
    elif not android:
        # 排障信息只在「疑似被误判」时才摊开：真机被认成电脑预览时，
        # platform_diag 里会出现 android / /data/ 这类痕迹，截图即可定位，
        # 不用再猜「为什么我是平板却显示电脑」。正常电脑预览下不再展示
        # 「platform=win32 TMPDIR= 标记=」这种纯调试串（用户看不懂，白占版面）。
        d = platform_diag()
        suspicious = ("android" in str(d).lower()) or ("/data/" in str(d))
        if suspicious:
            rows.append(ft.Text(f"平台判定：{d}", size=10, color=T.warn(),
                                selectable=True))
        # 正常电脑预览下不再补一行说明：卡片副标题已经写了「更新仅手机版可用」，
        # 再加一句同样意思的灰字只是重复占版面。
    if android:
        rows.append(ft.TextButton(
            content=ft.Text("直接下载最新安装包（浏览器打开，安装前核对 SHA-256）",
                            size=11),
            on_click=_open_latest))
    rows.append(status)
    return T.card(ft.Column(rows, spacing=T.GAP_S))


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
    """弹「发现新版本」BottomSheet：主推平板浏览器安装 + App 内一键安装备选。"""
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
        # 主推通路：走 Flet UrlLauncher（Flutter 插件），完全不碰 pyjnius
        self._browser_btn = ft.FilledButton("用平板浏览器下载安装",
                                            on_click=self._open_browser)
        # 次要通路：App 内下载后由 pyjnius 唤起安装器（桥接恢复时一键装）
        self._download_btn = ft.TextButton("App 内下载并安装",
                                           on_click=self._start_download)
        self._install_btn = ft.FilledButton("安装更新", visible=False,
                                            on_click=self._install)
        self._close_btn = ft.TextButton("稍后", on_click=self._close)
        # 浏览器通路打开的地址（镜像优先，国内直连 github 常被污染）
        self._browser_url = browser_url_or_latest(info)
        # 展示给用户核对的两个「参照物」：官方地址 + Release 页（镜像只是加速）
        self._official_url = official_apk_url(info)
        self._release_page = official_release_page()
        self._mirror_url = self._browser_url
        self._sha = format_sha256(getattr(info, "apk_sha256", ""))
        self._link = ft.Text(self._official_url, size=10, selectable=True,
                             color=ft.Colors.ON_SURFACE_VARIANT)
        self._mirror_link = ft.Text(self._mirror_url, size=10, selectable=True,
                                    color=ft.Colors.ON_SURFACE_VARIANT)
        self._release_link = ft.Text(self._release_page, size=10,
                                     selectable=True,
                                     color=ft.Colors.ON_SURFACE_VARIANT)
        self._sha_text = ft.Text(
            self._sha or "该版本未提供 SHA-256（App 内下载安装已停用）",
            size=10, selectable=True, color=ft.Colors.ON_SURFACE_VARIANT)
        if not getattr(info, "has_checksum", lambda: bool(self._sha))():
            # 无法校验就不给 App 内安装入口：宁可只能走浏览器，也不装来路不明的包
            self._download_btn.disabled = True

    def _mid_height(self):
        """中部信息区高度：至少 200、最多 420，随屏幕高收缩。

        完整性信息（官方地址 + SHA-256 + 说明）比旧版长得多，手机上不设界会
        把按钮顶出屏幕；Flet 0.86 的 `ft.Container` 没有 scroll 参数，
        限高滚动只能给 `ft.Column(height=…, scroll=…)`。
        """
        try:
            h = int(getattr(self.page, "height", 0) or 0)
        except Exception:
            h = 0
        if h <= 0:          # 单测/未知环境
            return 300
        return max(200, min(h - 300, 420))

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
        # 中部：更新说明 + 完整性信息，限高滚动（按钮/进度永远在屏幕内）
        mid = []
        if self.info.notes:
            mid.append(ft.Text(self.info.notes, size=12, selectable=True))
        mid += [
            ft.Text("平板上点「用平板浏览器下载安装」即可：下载完点通知栏的"
                    "安装提示，全程不用电脑。",
                    size=11, color=ft.Colors.ON_SURFACE_VARIANT),
            # ---- 完整性信息（2026-11-17）：官方地址 + SHA-256 摊给用户核对 ----
            ft.Text("官方安装包地址（github.com 原站，可长按复制）",
                    size=10, color=ft.Colors.ON_SURFACE_VARIANT),
            self._link,
            ft.Text("官方 Release 页面（可核对更新说明与校验值）",
                    size=10, color=ft.Colors.ON_SURFACE_VARIANT),
            self._release_link,
            ft.Text("SHA-256（安装前请核对下载到的文件哈希与这一串完全一致）",
                    size=10, color=ft.Colors.ON_SURFACE_VARIANT),
            self._sha_text,
            ft.Text("注意：浏览器下载由系统下载管理器完成，App 拿不到文件，"
                    "无法自动校验；App 内「下载并安装」会自动校验 SHA-256，"
                    "校验不通过不会安装。",
                    size=10, color=T.warn()),
            ft.Text("镜像加速地址（与官方地址同一个文件，官方地址打不开时用）",
                    size=10, color=ft.Colors.ON_SURFACE_VARIANT),
            self._mirror_link,
        ]
        parts += [
            ft.Column(mid, spacing=8, scroll=ft.ScrollMode.AUTO,
                      height=self._mid_height()),
            self._bar, self._status,
            ft.Row([
                self._close_btn, self._download_btn, self._install_btn,
                self._browser_btn,
            ], wrap=True, spacing=6,
                alignment=ft.MainAxisAlignment.END),
        ]
        return ft.Container(ft.Column(parts, spacing=8),
                            padding=16, width=520)

    # ---------- 下载 ----------

    def _start_download(self, e=None):
        if not getattr(self.info, "has_checksum", lambda: bool(self._sha))():
            # 没有校验值 = 无法证明这个包是官方那份，App 内下载直接不开工
            self._status.value = ("该版本没有提供 SHA-256 校验值，App 内安装已停用；"
                                  "请点「用平板浏览器下载安装」并用上面的官方地址核对。")
            try:
                self.page.update()
            except Exception:
                pass
            return
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

        try:
            return download_update(self.info, progress=on_progress)
        except ChecksumError as e:
            # 与网络失败分开上报：下载失败可以重试，内容不对必须换官方通路
            return {"ok": False, "checksum": True, "error": str(e)}

    def _download_done(self, result):
        # run_bg 兜底约定：异常被包装成 {"ok": False, "error": ...}
        if isinstance(result, dict) and result.get("ok") is False:
            self._bar.visible = False
            self._bar.value = 0
            if result.get("checksum"):
                self._status.value = (
                    "校验失败，已删除下载文件、未安装：" + str(result.get("error") or "")
                    + "。请用上面的官方地址手动下载并核对 SHA-256。")
                self._download_btn.visible = False
            else:
                self._status.value = "下载失败：" + str(result.get("error") or "网络异常")
                self._download_btn.visible = True
                self._download_btn.content = ft.Text("重试下载")
        else:
            self._path = result
            self._bar.visible = False
            self._status.value = ("下载完成，SHA-256 已校验通过。点「安装更新」试一键安装；"
                                  "若不可用会自动改用平板浏览器，全程不用电脑。")
            self._install_btn.visible = True
        try:
            self.page.update()
        except Exception:
            pass

    # ---------- 安装 ----------

    async def _open_browser(self, e=None):
        """平板内安装主通路：把 APK 直链交给系统浏览器 / 下载管理器。

        走 Flet 的 UrlLauncher（Flutter 插件、Java 侧），与 pyjnius 完全无关。
        EXTERNAL_APPLICATION 明确交给外部浏览器，避免 in-app WebView 下不了
        APK。万一这步也失败，弹窗里已有可长按复制的直链兜底。
        """
        self._status.value = "正在打开平板浏览器…"
        try:
            self.page.update()
        except Exception:
            pass
        try:
            await ft.UrlLauncher().launch_url(
                self._browser_url, mode=ft.LaunchMode.EXTERNAL_APPLICATION)
            # 明确不宣称「已校验」：浏览器下载的文件 App 拿不到，校验只能是用户的活
            self._status.value = ("已交给平板浏览器下载：下载完点通知栏安装，"
                                  "首次若提示「安装未知应用」允许一次即可。"
                                  "浏览器下载无法自动校验，安装前请核对文件 "
                                  "SHA-256 与上面那串一致。")
        except Exception as ex:
            self._status.value = ("浏览器没打开成功，请长按复制上面的官方安装包地址，"
                                  "贴到浏览器里打开。" + str(ex)[:60])
        try:
            self.page.update()
        except Exception:
            pass

    async def _install(self, e=None):
        """App 内一键安装（pyjnius）；安装前复验 SHA-256，桥接不可用时切浏览器。"""
        if not self._path:
            await self._open_browser()
            return
        try:
            started = install_apk(self._path,
                                  getattr(self.info, "apk_sha256", ""))
        except ChecksumError as ex:
            # 第三道校验点没通过：删文件、禁止安装、把人引到官方通路
            _rm(self._path)
            self._path = None
            self._install_btn.visible = False
            self._download_btn.visible = True
            self._download_btn.content = ft.Text("重新下载")
            self._status.value = (f"安装已阻止：{ex}。文件已删除，"
                                  "请重新下载或用上面的官方地址手动安装并核对 SHA-256。")
            try:
                self.page.update()
            except Exception:
                pass
            return
        except Exception:
            # 2026-11-16 老板平板：pyjnius 整链失败。旧文案让用户「从电脑上装」，
            # 但主力设备就是平板，等于没修 —— 现在自动走平板浏览器，全程不碰电脑。
            self._status.value = "一键安装通道不可用，正在改用平板浏览器…"
            try:
                self.page.update()
            except Exception:
                pass
            await self._open_browser()
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
