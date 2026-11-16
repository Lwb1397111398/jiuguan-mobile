# -*- coding: utf-8 -*-
"""「我的」页：全量备份（双端同步）、导出（正文 / 项目 zip）、请求日志、关于。"""
import datetime
import os

import flet as ft

import health
import theme as T
from widgets import section, menu_group, toast, confirm
from bridge import run_bg


class MinePage:
    def __init__(self, page, session):
        self.page = page
        self.session = session
        self.on_refresh = None  # main() 注入：重建本页
        self._sub = None        # 子页面（None=本页；"jailbreak"/"cards"/"templates"/"presets"/"settings"/"sync"）
        self._subs = {}         # 子页面实例缓存（惰性创建，保留页内状态）
        self._picker = ft.FilePicker()
        # flet 0.86：FilePicker 是服务控件（不渲染），必须挂 page.services；
        # 误挂 page.overlay（渲染层）会被前端画成红色错误块「Unknown control:
        # FilePicker」，盖住页面左侧（2026-11-15 真机截图定位）。
        page.services.append(self._picker)

    # ---------- 子页面 ----------

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

    def _export_full(self, e):
        s = self.session
        export_dir = os.path.join(s.app.data_dir, "exports")
        os.makedirs(export_dir, exist_ok=True)
        path = os.path.join(
            export_dir, f"全量备份_{datetime.datetime.now():%Y%m%d_%H%M%S}.zip")
        toast(self.page, "正在打包全部资料…")

        def done(r):
            ok, msg = r
            if not ok:
                toast(self.page, msg, error=True)
                return
            self.page.show_dialog(ft.AlertDialog(
                modal=True, title=ft.Text("备份完成"),
                content=ft.Text(
                    f"{msg}\n\n文件位置：{path}\n\n把它传到电脑（微信/网盘/数据线均可），"
                    "在电脑版「导出页 → 导入全量备份」选择该文件即可完成同步。",
                    size=13),
                actions=[ft.FilledButton(
                    "好", on_click=lambda ev: self.page.pop_dialog())],
                actions_alignment=ft.MainAxisAlignment.END,
            ))
        run_bg(self.page, lambda: self._do_export(path), done)

    def _do_export(self, path):
        try:
            from app.core.exporter import export_full_backup
            return export_full_backup(path, self.session.app)
        except Exception as e:
            return False, f"备份失败：{e}"

    async def _import_full(self, e):
        # flet 0.86：pick_files 是 async，必须 await（2026-11-16 修复
        # 「'coroutine' object is not subscriptable」真机闪退）
        files = await self._picker.pick_files(
            dialog_title="选择全量备份 zip", allow_multiple=False)
        self._import_picked(files)

    def _import_picked(self, files):
        if not files:
            return
        path = files[0].path
        if not path:
            toast(self.page, "未能读取所选文件", error=True)
            return

        def _yes():
            toast(self.page, "正在恢复全部资料…")

            def done(r):
                ok, msg = r
                if not ok:
                    toast(self.page, msg, error=True)
                    return
                self.page.show_dialog(ft.AlertDialog(
                    modal=True, title=ft.Text("导入完成"),
                    content=ft.Text(msg + "\n\n建议重启应用后使用。", size=13),
                    actions=[ft.FilledButton(
                        "好", on_click=lambda ev: (
                            self.page.pop_dialog(),
                            self.on_refresh() if self.on_refresh else None))],
                    actions_alignment=ft.MainAxisAlignment.END,
                ))
            run_bg(self.page, lambda: self._do_import(path), done)

        confirm(self.page, "导入全量备份",
                "将整体替换本机全部资料（模型/API Key、提示词偏好、预设、破甲库、"
                "项目列表），与备份文件完全一致；导入前自动做安全备份。确定继续？",
                _yes, yes_text="导入")

    def _do_import(self, path):
        try:
            from app.core.exporter import import_full_backup
            return import_full_backup(path, self.session.app)
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
        from update_ui import update_card
        ub = update_card(self.page, s)

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
                        "备份含 API Key 明文，请妥善保管。", size=11,
                        color=T.muted()),
                ft.Row([
                    T.primary("导出备份", icon=ft.Icons.UPLOAD_FILE,
                              on_click=self._export_full, expand=True),
                    T.tonal("导入备份", icon=ft.Icons.DOWNLOAD,
                            on_click=self._import_full, expand=True),
                ], spacing=T.GAP_S),
            ], spacing=T.GAP_S)),
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
