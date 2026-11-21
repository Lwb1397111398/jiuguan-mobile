# -*- coding: utf-8 -*-
"""「我的」页：全量备份（双端同步）、导出（正文 / 项目 zip）、请求日志、关于。"""
import datetime
import os

import flet as ft

import backup_io
import health
import theme as T
from widgets import section, menu_group, toast, confirm
from bridge import run_bg


class MinePage:
    def __init__(self, page, session, runtime=None):
        self.page = page
        self.session = session
        self.on_refresh = None  # main() 注入：重建本页
        # 应用级运行时（runtime.AppRuntime）：全量导入成功后由它把旧 Session /
        # 全部页面 / 项目缓存一次性作废并重建。None = 独立使用（单元测试）。
        self.runtime = runtime
        self._sub = None        # 子页面（None=本页；"jailbreak"/"cards"/"templates"/"presets"/"settings"/"sync"）
        self._subs = {}         # 子页面实例缓存（惰性创建，保留页内状态）
        self._picker = ft.FilePicker()
        # 系统分享（flet 0.86 服务控件）：备份要能发到网盘/微信——那是
        # 「卸载 App 也不会删」的第二个去处（P0 换钥过渡版，见 backup_io.py）
        self._share = ft.Share()
        # 没有 runtime 时的本地导入闸门（防重复点击 + 成功回调只执行一次）
        self._import_busy = False
        self._import_token = 0
        self._import_done_token = 0
        # flet 0.86：FilePicker 是服务控件（不渲染），必须挂 page.services；
        # 误挂 page.overlay（渲染层）会被前端画成红色错误块「Unknown control:
        # FilePicker」，盖住页面左侧（2026-11-15 真机截图定位）。
        # 挂载做成幂等：全量导入失败时本页会原地重建，invalidate() 已把旧 picker
        # 从 services 摘掉，build() 必须能补回来，否则「导入备份」按钮从此没反应。
        self._mount_services()

    def _mount_services(self):
        backup_io.mount_service(self.page, self._picker)
        backup_io.mount_service(self.page, self._share)

    # ---------- 子页面 ----------

    def invalidate(self):
        """整机替换前让本页停手：丢掉全部子页面实例。

        子页面（破甲库/偏好卡/预设/设置/云同步/任务模板）都持有旧 Session；
        不丢就会在导入后继续显示旧数据、继续按旧连接写库。

        **这里不摘 FilePicker**：导入失败回滚时本页还要继续用（「导入备份」
        按钮不能失灵）；只有确定要丢弃本页时才走 `dispose()`。
        """
        self._sub = None
        for obj in list(self._subs.values()):
            fn = getattr(obj, "invalidate", None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass
        self._subs.clear()

    def dispose(self):
        """本页即将被整体丢弃（导入成功重建 / 退出）：连服务控件一起摘掉。

        不摘的话每次导入都会在 `page.services` 里多留一个服务控件。
        """
        self.invalidate()
        backup_io.unmount_service(self.page, self._picker)
        backup_io.unmount_service(self.page, self._share)

    def _sub_refresh(self):
        """子页面重建（复用 main() 注入的整页重建回调）。"""
        if self.on_refresh:
            self.on_refresh()
        else:
            try:
                self.page.update()
            except Exception:
                pass

    def _ensure_sub(self, name: str):
        """惰性创建子页面实例（同一子页面只建一次，保留页内状态）。"""
        obj = self._subs.get(name)
        if obj is not None:
            return obj
        if name == "jailbreak":
            from pages.jailbreaks import JailbreakLibrary
            obj = JailbreakLibrary(self.page, self.session, on_back=self._close_sub)
        elif name == "cards":
            from pages.cards import PrefsLibrary
            obj = PrefsLibrary(self.page, self.session, on_back=self._close_sub,
                               on_open_templates=self._open_templates)
        elif name == "presets":
            from pages.presets import PresetLibrary
            obj = PresetLibrary(self.page, self.session, on_back=self._close_sub)
        elif name == "settings":
            from pages.settings import SettingsPage
            obj = SettingsPage(self.page, self.session, on_back=self._close_sub,
                               on_open_templates=lambda: self._open_templates(""))
        elif name == "sync":
            from pages.sync import SyncPage
            obj = SyncPage(self.page, self.session, on_back=self._close_sub)
        else:
            return None
        obj.on_refresh = self._sub_refresh
        self._subs[name] = obj
        return obj

    def _open_jailbreaks(self, e):
        self._open_sub("jailbreak")

    def _open_cards(self, e):
        self._open_sub("cards")

    def _open_presets(self, e):
        self._open_sub("presets")

    def _record_tiles(self, _tile):
        """「产出与记录」分组的条目（崩溃诊断只在真有过日志时出现）。

        普通用户不该看到一条空的诊断入口；但只要 crash.log 存在，就一定摆出来
        —— 闪退过一次的人第一件事就是找「哪里能看原因」。
        """
        import crashguard
        items = [
            _tile(ft.Icons.SAVE_ALT, "导出作品正文",
                  "TXT / Markdown / Word，可选章节范围与总纲",
                  self._export_prose),
            _tile(ft.Icons.RECEIPT_LONG, "请求日志",
                  "筛选 / 成本统计 / 清空 / 查看完整提示词",
                  self._show_logs),
        ]
        if crashguard.exists():
            items.append(_tile(
                ft.Icons.BUG_REPORT, "崩溃诊断",
                "闪退过一次？这里最后一次跑到哪、有没有报错",
                self._show_crash))
        items.append(_tile(
            ft.Icons.SETTINGS, "设置",
            "上下文注入、防截断、草稿自检、去 AI 味、嵌入接口",
            self._open_settings))
        return items

    def _show_crash(self, e=None):
        """崩溃日志查看：复制全文发给 AI 定位，或清空重来。"""
        import crashguard
        text = crashguard.tail(150)
        if not text:
            toast(self.page, "没有崩溃日志")
            return
        field = ft.TextField(value=text, multiline=True, min_lines=14,
                             max_lines=14, read_only=True, text_size=11)

        def _copy(ev):
            try:
                self.page.run_task(self.page.clipboard.set, text)
                toast(self.page, "已复制，可直接发给 AI 定位")
            except Exception:
                toast(self.page, "复制失败，请长按选择文本", error=True)

        def _clear(ev):
            self.page.pop_dialog()
            crashguard.clear()
            toast(self.page, "崩溃日志已清空")
            if self.on_refresh:
                self.on_refresh()

        self.page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("崩溃诊断（最后 150 行）"),
            content=ft.Container(field, width=560, height=420),
            actions=[
                ft.TextButton("清空", on_click=_clear),
                ft.TextButton("复制全文", on_click=_copy),
                ft.TextButton("关闭", on_click=lambda ev: self.page.pop_dialog()),
            ],
            actions_alignment=ft.MainAxisAlignment.END))

    def _open_settings(self, e):
        self._open_sub("settings")

    def _open_sync(self, e):
        self._open_sub("sync")

    def _open_templates(self, stage: str = ""):
        """打开任务模板页；stage 非空时把可选范围限定到该阶段的模板。"""
        from pages.templates import TaskTemplateEditor, STAGE_TPL_KEYS
        keys = STAGE_TPL_KEYS.get(stage) if stage else None
        obj = TaskTemplateEditor(self.page, self.session, on_back=self._close_sub,
                                 keys=keys,
                                 initial=(keys[0] if keys else None))
        obj.on_refresh = self._sub_refresh
        self._subs["templates"] = obj          # 每次重建：限定范围可能不同
        self._sub = "templates"
        self._sub_refresh()

    def _open_sub(self, name: str):
        if self._ensure_sub(name) is None:
            return
        self._sub = name
        self._sub_refresh()

    def _close_sub(self):
        self._sub = None
        self._sub_refresh()

    # ---------- 全量备份 ----------

    def _pw_field(self, label: str, helper: str = ""):
        """密码输入框（默认遮点、可点眼睛看明文）+ 外置标签说明。

        flet 0.86：password/can_reveal_password 是控件属性，不是构造参数；
        标签走 `T.field_group`（框外标签），不用 floating label。
        """
        tf = T.field("", "")
        tf.password = True
        tf.can_reveal_password = True
        return tf, T.field_group(tf, label, helper)

    def _export_full(self, e):
        """导出前先让用户设密码（默认必须设；明文要显式打开开关）。"""
        from app.core.exporter import MIN_BACKUP_PASSWORD
        s = self.session
        export_dir = os.path.join(s.app.data_dir, "exports")
        os.makedirs(export_dir, exist_ok=True)
        path = os.path.join(
            export_dir, f"全量备份_{datetime.datetime.now():%Y%m%d_%H%M%S}.zip")

        pw1, f1 = self._pw_field("备份密码", f"至少 {MIN_BACKUP_PASSWORD} 位；"
                                    "不上传、不保存，忘了就打不开这份备份")
        pw2, f2 = self._pw_field("再输一次", "防手滑打错")
        err = ft.Text("", size=11, color=T.danger())
        plain_row, plain_sw = T.switch_row(
            "不使用密码（明文 zip，不推荐）", False,
            desc="明文包里能直接搜到 API Key，只在自己电脑上临时周转时考虑")
        # 留引用：校验要改 err 的文字，测试也要能直接摆弄这几个控件
        self._pw1, self._pw2, self._err, self._plain_sw = pw1, pw2, err, plain_sw

        def _go(ev):
            pwd = "" if plain_sw.value else (pw1.value or "")
            if not plain_sw.value:
                if not pwd.strip():
                    err.value = "请先设置备份密码（或打开下面的明文开关）"
                    self.page.update()
                    return
                if len(pwd) < MIN_BACKUP_PASSWORD:
                    err.value = f"密码至少 {MIN_BACKUP_PASSWORD} 位"
                    self.page.update()
                    return
                if pwd != (pw2.value or ""):
                    err.value = "两次输入的密码不一致"
                    self.page.update()
                    return
                self.page.pop_dialog()
                self._start_export(path, pwd, False)
                return
            # 明文是兼容选项，必须再明确确认一次（与电脑端一致）
            self.page.pop_dialog()
            confirm(self.page, "确认使用明文备份",
                    "你选择了不使用密码。这样的 zip 里 API Key 是明文，"
                    "用记事本就能搜到。\n\n确定要导出明文备份吗？",
                    lambda: self._start_export(path, "", True),
                    yes_text="导出明文")

        T.sheet(self.page, "给备份设置密码", [
            T.hint_text("全量备份里有模型 API Key 等敏感信息，默认整个文件加密。"
                        "密码不保存、不上传，也无法找回——请记在纸上或密码管理器。"),
            f1, f2, err, plain_row,
        ], actions=T.action_row(
            self.page,
            primary_btn=T.primary("开始备份", icon=ft.Icons.LOCK, on_click=_go)))

    def _start_export(self, path: str, password: str, allow_plain: bool):
        toast(self.page, "正在加密打包全部资料…")

        def done(r):
            if isinstance(r, dict):           # run_bg 的异常兜底形态
                toast(self.page, str(r.get("error") or "备份失败"), error=True)
                return
            ok, msg = r
            if not ok:
                toast(self.page, msg, error=True)
                return
            self._show_export_receipt(path, msg)
        run_bg(self.page,
               lambda: self._do_export(path, password, allow_plain), done)

    def _show_export_receipt(self, path: str, msg: str):
        """导出成功后的「收货单」：包内清单 + 另存到手机 + 系统分享。

        换钥过渡版（P0）的关键一步：备份文件默认落在应用私有目录
        （`data_dir/exports/`），**卸载 App 就一起没了**。用户必须能把它
        「另存到手机（下载/网盘）」或分享出去，否则「先备份再卸载」等于空话。
        """
        summary = None
        try:
            from app.core.exporter import backup_summary
            summary = backup_summary(self.session.app)
        except Exception:
            summary = None
        status = ft.Text("", size=12, color=T.muted(), selectable=True)

        try:
            h = int((getattr(self.page, "height", 0) or 0) * 0.45)
        except Exception:
            h = 320
        h = max(220, min(h or 320, 420))

        async def _save(ev):
            status.value = "正在等系统「另存」窗口…（选「下载」或网盘目录）"
            try:
                self.page.update()
            except Exception:
                pass
            res = await backup_io.save_backup_copy(
                self._picker, path, os.path.basename(path))
            if res.get("ok"):
                status.value = (
                    "已另存到你选择的位置（如「下载」）。卸载 App 不会删这份文件；"
                    "换新包后用「导入备份」选它即可恢复。")
            elif res.get("cancelled"):
                status.value = "已取消另存：这份还在应用私有目录里，卸载会丢。"
            else:
                status.value = str(res.get("error") or "另存失败")
            try:
                self.page.update()
            except Exception:
                pass

        async def _share(ev):
            status.value = "正在打开系统分享…"
            try:
                self.page.update()
            except Exception:
                pass
            res = await backup_io.share_backup(
                self._share, path,
                text="酒馆小说生成器 全量备份（含可解密的 API Key，请勿公开分享）")
            if res.get("ok"):
                status.value = ("已交给系统分享面板：发到网盘/微信/文件管理器都行。"
                                "发出去的那份卸载 App 不会删。")
            else:
                status.value = str(res.get("error") or "分享失败")
            try:
                self.page.update()
            except Exception:
                pass

        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("备份完成"),
            content=ft.Container(ft.Column([
                ft.Text(msg, size=12, selectable=True),
                ft.Text(backup_io.receipt_text(summary), size=12,
                        selectable=True),
                ft.Text(f"应用内位置（卸载会丢）：{path}", size=10,
                        color=T.muted(), selectable=True),
                ft.Text(backup_io.SAFE_HINT, size=11, color=T.warn()),
                status,
            ], spacing=8, tight=True, scroll=ft.ScrollMode.AUTO, height=h)),
            actions=[
                ft.TextButton("关闭", on_click=lambda ev: self.page.pop_dialog()),
                ft.TextButton("分享", on_click=_share),
                ft.FilledButton("另存到手机", on_click=_save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        ))

    def _migration_notice(self) -> ft.Control:
        """换钥预警（P0）：先把「要卸载重装」这件事讲清楚，并给出动作。

        文案与 `docs/签名换钥与安装用户数据迁移方案.md` 附录 A 同口径：
        先备份 → 卸载 → 安装新包 → 导入恢复。这里只做说明，不催用户操作——
        真正需要换钥时，更新弹窗会按远端 `apkSigner` 自动切到迁移流程。
        """
        return T.card(ft.Column([
            ft.Row([ft.Icon(ft.Icons.VERIFIED_USER, color=T.warn(), size=20),
                    ft.Text("提前知道：将来某一版要「卸载重装」一次", size=14,
                            weight=ft.FontWeight.W_600, expand=True)]),
            ft.Text("为了保护作品安全，那个版本会更换应用签名。安卓不允许"
                    "「签名不同」的版本直接覆盖安装，所以顺序是："
                    "① 先备份 → ② 卸载旧版 → ③ 安装新包 → ④ 导入备份恢复。",
                    size=11, color=T.muted()),
            ft.Text("现在就可以做一次：上面「导出备份」→ 完成后点「另存到手机」"
                    "存进「下载」，或点「分享」发到网盘/微信。"
                    "那份文件卸载 App 不会删。", size=11, color=T.muted()),
        ], spacing=T.GAP_S))

    def _do_export(self, path, password=None, allow_plain=False):
        try:
            from app.core.exporter import export_full_backup
            return export_full_backup(path, self.session.app, password=password,
                                      allow_plain=allow_plain)
        except Exception as e:
            return False, f"备份失败：{e}"

    async def _import_full(self, e):
        # 导入进行中：连选文件都不让开（重复进入会二次替换整个资料库）
        if self._importing_now():
            toast(self.page, "正在导入，请稍候…")
            return
        # flet 0.86：pick_files 是 async，必须 await（2026-11-16 修复
        # 「'coroutine' object is not subscriptable」真机闪退）
        files = await self._picker.pick_files(
            dialog_title="选择全量备份文件", allow_multiple=False)
        self._import_picked(files)

    def _importing_now(self) -> bool:
        """是否已有全量导入在跑（重复点击的唯一判据）。"""
        rt = self.runtime
        if rt is not None:
            return bool(rt.importing)
        return bool(self._import_busy)

    def _import_picked(self, files):
        if not files:
            return
        if self._importing_now():
            toast(self.page, "正在导入，请稍候…")
            return
        path = files[0].path
        if not path:
            toast(self.page, "未能读取所选文件", error=True)
            return
        # 先认文件（只读文件头，不解密、不动本机数据）：加密包问密码，明文包先示警
        try:
            from app.core.exporter import describe_backup_file
            info = describe_backup_file(path)
        except Exception as e:
            toast(self.page, f"读取备份文件失败：{e}", error=True)
            return
        kind = info.get("kind")
        if kind == "unknown":
            toast(self.page, f"不是有效的全量备份：{info.get('reason') or '无法识别'}",
                  error=True)
            return
        if kind == "encrypted":
            self._ask_import_password(path, info)
        else:
            self._confirm_import(path, None, plain=True)

    def _ask_import_password(self, path: str, info: dict):
        """加密备份：输入密码 → 再确认覆盖。密码错在后台解不开，本机数据不动。"""
        tf, fg = self._pw_field("备份密码", "导出这份备份时设的密码；丢了就打不开了")
        err = ft.Text("", size=11, color=T.danger())
        self._pw_import = tf
        exported = info.get("exported_at") or ""

        def _go(ev):
            pwd = tf.value or ""
            if not pwd.strip():
                err.value = "请先输入密码"
                self.page.update()
                return
            self.page.pop_dialog()
            self._confirm_import(path, pwd, plain=False)

        T.sheet(self.page, "这是加密的备份", [
            T.hint_text("需要导出时设的密码才能打开。"
                        + (f"导出时间：{exported}" if exported else "")
                        + "　密码不保存，无法找回，只能从别的备份重来。"),
            fg, err,
        ], actions=T.action_row(
            self.page,
            primary_btn=T.primary("解密并导入", icon=ft.Icons.LOCK_OPEN, on_click=_go)))

    def _confirm_import(self, path: str, password, plain: bool):
        msg = ("将整体替换本机全部资料（模型/API Key、提示词偏好、预设、破甲库、"
               "项目列表），与备份文件完全一致；导入前自动做安全备份。确定继续？")
        if plain:
            msg = ("这个备份是明文 zip（旧版备份，或导出时没设密码），"
                   "里面能直接搜到 API Key。\n\n" + msg)
        confirm(self.page, "导入全量备份", msg,
                lambda: self._start_import(path, password), yes_text="导入")

    def _start_import(self, path: str, password):
        # 进导入态：重复点击在这里被挡住（第二次返回 None），并且先把会写库的
        # 自动模式 / 流式任务 / 防抖存稿 / 云同步守护全部静默——它们绝不能
        # 跨过「库被整体替换」这条线继续写（详见 runtime.py 顶部说明）。
        rt = self.runtime
        token = rt.begin_import() if rt is not None else self._begin_local_import()
        if token is None:
            toast(self.page, "正在导入，请稍候…")
            return
        toast(self.page, "正在解密并恢复全部资料…")

        def done(r):
            if isinstance(r, dict):           # run_bg 的异常兜底形态
                self._finish_import(token, False,
                                    str(r.get("error") or "导入失败"))
                return
            ok, msg = r
            self._finish_import(token, bool(ok), msg)

        run_bg(self.page, lambda: self._do_import(path, password), done)

    def _begin_local_import(self):
        """没有 runtime 时的导入闸门（单元测试 / 独立使用）。"""
        if self._import_busy:
            return None
        self._import_busy = True
        self._import_token += 1
        return self._import_token

    def _finish_import(self, token: int, ok: bool, msg: str):
        """导入结果收尾：**同一令牌只处理一次**（连点/回调重入都不会重建两次）。"""
        rt = self.runtime
        if rt is None:
            if token != self._import_token or token == self._import_done_token:
                return
            self._import_done_token = token
            self._import_busy = False
            toast(self.page, msg, error=not ok)
            if ok and self.on_refresh:
                self.on_refresh()
            return

        if not ok:
            # 失败/回滚：runtime 不重建页面，只把 core 关掉的项目重新打开
            if rt.import_failed(token):
                toast(self.page, msg, error=True)
            return
        # 成功：作废旧 Session + 全部页面/子页面缓存，再按导入后的数据重建。
        # 只有拿到「本次真的重建成功」才弹完成框——按钮重复点击不会重建两次；
        # 重建失败时 runtime 已切阻断页（rt.dead），绝不弹「导入完成」盖在
        # 「旧界面已停用」上，只给一句中性提示（不加按钮、不改阻断页版式）。
        if not rt.import_succeeded(token):
            if rt.dead:
                toast(self.page, "界面已停用，请重新打开应用")
            return
        self._show_import_done(msg)

    def _show_import_done(self, msg: str):
        from runtime import strip_restart_hint
        text = strip_restart_hint(msg)
        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("导入完成"),
            content=ft.Text(
                text + "\n\n已按备份重建全部页面（项目 / 创作 / 模型 / 我的），"
                       "当前显示的就是导入后的资料。", size=13),
            actions=[ft.FilledButton(
                "好", on_click=lambda ev: self.page.pop_dialog())],
            actions_alignment=ft.MainAxisAlignment.END,
        ))

    def _do_import(self, path, password=None):
        try:
            from app.core.exporter import import_full_backup
            return import_full_backup(path, self.session.app, password=password)
        except Exception as e:
            return False, f"导入失败：{e}"

    # ---------- 提示词资产（只读） ----------

    def _show_prompts(self, e):
        g = self.session.app.gstore
        cards = g.list_cards()
        presets = g.list_presets()
        jb_name = g.get_active_jailbreak_name()
        jb_text = g.get_jailbreak_prompt() or ""

        rows = []
        if cards:
            rows.append(section("偏好卡片"))
            for c in cards:
                rows.append(ft.Container(
                    ft.Column([
                        ft.Row([
                            ft.Text(c.get("title", ""), size=13,
                                    weight=ft.FontWeight.W_600, expand=True),
                            ft.Container(
                                ft.Text(f"{c.get('scope', '')}{'/' + c['stage'] if c.get('stage') else ''}",
                                        size=10),
                                bgcolor=ft.Colors.with_opacity(0.12, ft.Colors.BLUE),
                                border_radius=6,
                                padding=ft.Padding(left=6, right=6, top=2, bottom=2)),
                        ]),
                        ft.Text(c.get("content", ""), size=11, max_lines=3,
                                overflow=ft.TextOverflow.ELLIPSIS),
                    ], spacing=3),
                    padding=8))
        if presets:
            rows.append(section("提示词预设"))
            # 预设的 data 列是 JSON 字符串（GlobalStore.save_preset 里 json.dumps 存的），
            # 必须先解析再取 items —— 直接当 dict 用会 AttributeError: 'str'。
            # 桌面版用 presets_page 的 jload(p["data"], {}) 做同样的事（2026-09-23 修）。
            from app.core.store import jload
            for p in presets:
                data = jload(p.get("data"), {}) or {}
                items = data.get("items") or []
                rows.append(ft.Container(
                    ft.Row([ft.Icon(ft.Icons.LAYERS, size=16),
                            ft.Text(f"{p.get('name', '')}（{len(items)}条目）",
                                    size=13)]),
                    padding=6))
        rows.append(section("破甲库"))
        try:
            jb_count = len(g.list_jailbreak_profiles())
            jb_enabled = g.get_jailbreak_enabled()
        except Exception:
            jb_count, jb_enabled = 0, False
        rows.append(ft.Container(
            ft.Column([
                ft.Text(f"当前生效：{jb_name or '无'}（共 {jb_count} 套，"
                        f"注入{'已启用' if jb_enabled else '已关闭'}）", size=13,
                        weight=ft.FontWeight.W_600),
                ft.Text(jb_text[:400] or "（无破甲内容）", size=11,
                        color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Row([ft.TextButton("到破甲库管理", icon=ft.Icons.SHIELD,
                                      on_click=self._goto_jb_from_dialog)],
                       alignment=ft.MainAxisAlignment.END),
            ], spacing=4),
            padding=8))
        if not rows:
            rows.append(ft.Text("还没有配置：在电脑版配置好偏好/预设/破甲后，"
                                "用全量备份同步到手机即可自动生效。", size=13))

        def _close(ev):
            self.page.pop_dialog()
        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("提示词资产（随全量备份同步）"),
            content=ft.Container(ft.Column(rows, scroll=ft.ScrollMode.AUTO),
                                 width=560, height=480),
            actions=[ft.TextButton("关闭", on_click=_close)],
            actions_alignment=ft.MainAxisAlignment.END,
        ))

    def _goto_jb_from_dialog(self, e):
        """从只读弹窗直接跳到破甲库（先关弹窗再切子页面）。"""
        try:
            self.page.pop_dialog()
        except Exception:
            pass
        self._open_jailbreaks(None)

    # ---------- 正文导出 / 项目 zip ----------

    def _export_prose(self, e):
        from export_ui import export_prose_flow
        export_prose_flow(self.page, self.session)

    def _export_project(self, e):
        from export_ui import export_project_flow
        export_project_flow(self.page, self.session)

    async def _import_project(self, e):
        # flet 0.86：pick_files 是 async，必须 await（2026-11-16 修复
        # 「'coroutine' object is not subscriptable」真机闪退）
        files = await self._picker.pick_files(
            dialog_title="选择项目 zip", allow_multiple=False)
        if not files:
            return
        path = files[0].path if files else None
        from export_ui import import_project_flow
        import_project_flow(self.page, self.session, path, on_done=self.on_refresh)

    # ---------- 日志 ----------

    def _show_logs(self, e):
        from logs_ui import logs_view
        logs_view(self.page, self.session, on_refresh=self.on_refresh)

    # ---------- 云同步 ----------

    def _sync_summary(self) -> str:
        """首页/我的页显示的一行同步状态（不联网，只读本地配置）。"""
        try:
            from app.core import sync_config
            cfg = sync_config.load_config()
            state = sync_config.load_state()
        except Exception as e:
            return "读取同步配置失败：%s" % e
        if not sync_config.is_configured(cfg):
            return ("未配置。填好存储桶 + AccessKey 后，手机与电脑就共用同一份资料"
                    "（大纲、正文、记忆、模型密钥、偏好卡、破甲库）。")
        enabled = True
        try:
            enabled = str(self.session.app.gstore.get_global_setting(
                "sync_enabled", "0")) == "1"
        except Exception:
            pass
        parts = []
        if not enabled:
            parts.append("已暂停")
        parts.append("桶 " + (cfg.get("bucket") or "-"))
        try:
            parts.append("密码" + ("已绑定" if sync_config.get_crypto(cfg) else "未设置"))
        except Exception:
            parts.append("密码未知")
        parts.append("上次同步 " + (state.get("last_sync_at") or "从未"))
        usage = (state.get("usage") or {}).get("human")
        if usage:
            parts.append("云端 " + usage)
        return " · ".join(parts)

    def _sync_now_quick(self, e):
        """「我的」页的立即同步：直接借用云同步页的动作（不需要先打开那个页）。"""
        obj = self._ensure_sub("sync")
        if obj is None:
            return
        obj._sync_now(None)

    # ---------- 构建 ----------

    def build(self) -> ft.Control:
        # 重建时补挂服务控件（invalidate() 摘过；见 __init__ 的说明）
        self._mount_services()
        # 子页面分派：有打开的子页面就先渲染它（页内返回按钮把 _sub 置回 None 并重建）。
        # 缺了这段，子页面入口点了不跳转（历史缺陷，2026-09-18 修）。
        if self._sub:
            sub = self._subs.get(self._sub)
            if sub is not None:
                return sub.build()
            self._sub = None        # 实例丢了就退回本页，避免白屏
        s = self.session
        banner = health.banner(self.page, s, self.on_refresh)

        # 检查更新置顶（老板 2026-11-16：更新按钮不该藏在设置页二十张卡片后面）
        # on_go_backup：换签名版本的更新弹窗里「先备份」直接跳到这里导出
        from update_ui import update_card
        ub = update_card(self.page, s,
                         on_go_backup=lambda: self._export_full(None))

        def _tile(icon, title, subtitle, handler, async_handler=False):
            """统一菜单条目（转发 theme.nav_tile：圆角图标底座 + 右箭头）。"""
            return T.nav_tile(icon, title, subtitle, on_click=handler)

        body = ft.Column([
            T.page_head("我的", "资料同步、创作工具与产出记录"),
            ub,                                     # ← 检查更新：顶部第一张卡
            *([banner] if banner else []),
            section("资料同步"),
            T.card(ft.Column([
                ft.Row([ft.Icon(ft.Icons.SYNC, color=T.brand(), size=20),
                        ft.Text("全量备份 / 恢复", size=14,
                                weight=ft.FontWeight.W_600, expand=True)]),
                ft.Text("导出一个文件带走全部资料（模型、API Key、提示词偏好、预设、"
                        "破甲库、任务模板、所有项目），电脑 ↔ 手机 导入即完全一致。"
                        "默认用密码加密整个文件（密码无法找回，请记牢）。"
                        "导出后可「另存到手机」或分享到网盘：卸载 App 不会删那份。",
                        size=11, color=T.muted()),
                ft.Row([
                    T.primary("导出备份", icon=ft.Icons.UPLOAD_FILE,
                              on_click=self._export_full, expand=True),
                    T.tonal("导入备份", icon=ft.Icons.DOWNLOAD,
                            on_click=self._import_full, expand=True),
                ], spacing=T.GAP_S),
            ], spacing=T.GAP_S)),
            self._migration_notice(),
            T.card(ft.Column([
                ft.Row([ft.Icon(ft.Icons.CLOUD_SYNC, color=T.brand(), size=20),
                        ft.Text("云同步（数据胶囊）", size=14,
                                weight=ft.FontWeight.W_600, expand=True),
                        ft.IconButton(
                            ft.Icons.SETTINGS, icon_size=18,
                            tooltip="同步设置",
                            on_click=self._open_sync)]),
                ft.Text(self._sync_summary(), size=11, color=T.muted()),
                ft.Row([
                    T.primary("立即同步", icon=ft.Icons.SYNC,
                              on_click=self._sync_now_quick, expand=True),
                    T.tonal("同步设置", on_click=self._open_sync, expand=True),
                ], spacing=T.GAP_S),
            ], spacing=T.GAP_S)),
            section("项目 zip（搬单本书）"),
            menu_group("", [
                _tile(ft.Icons.FOLDER_ZIP, "导出项目 zip",
                      "打包当前项目（大纲/正文/记忆），跨设备搬单本",
                      self._export_project),
                _tile(ft.Icons.UNARCHIVE, "导入项目 zip",
                      "从 zip 还原一个项目，与现有项目并存",
                      self._import_project),
            ]),
            section("创作工具"),
            menu_group("", [
                _tile(ft.Icons.SHIELD, "破甲库",
                      "新建 / 编辑四段 / 切换生效 / 强度档 / 删除",
                      self._open_jailbreaks),
                _tile(ft.Icons.TUNE, "偏好卡片",
                      "全局 / 阶段 / 手动三级卡片，可置顶与限定注入阶段",
                      self._open_cards),
                _tile(ft.Icons.LAYERS, "预设管理",
                      "导入酒馆预设、AI 识别破限并提取破甲",
                      self._open_presets),
                _tile(ft.Icons.DESCRIPTION, "任务模板",
                      "16 个任务提示词的跨阶段定制",
                      lambda e: self._open_templates("")),
                _tile(ft.Icons.ARTICLE, "提示词资产（只读概要）",
                      "随全量备份自动同步生效",
                      self._show_prompts),
            ]),
            section("产出与记录"),
            menu_group("", self._record_tiles(_tile)),
        ], scroll=ft.ScrollMode.AUTO, expand=True, spacing=0,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        return ft.Container(body, padding=T.GAP_L, expand=True)
