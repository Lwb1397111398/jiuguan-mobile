# -*- coding: utf-8 -*-
"""云同步（手机版）：连数据胶囊、设同步密码、立即同步、从云端恢复。

同步逻辑全在 `app.core.sync_engine`（桌面/手机同一份，由 sync_core.py 同步
过来），本页只做三件事：把连接参数写进 sync.json、把开关写进全局设置、
把网络动作丢进后台线程。

**绝不在 flet 事件线程里等网络**：胶囊下行实测 0.3 MB/s，一次全量同步
十几秒，卡在 UI 线程上就是整页无响应（真机表现为闪退）。
"""
import flet as ft

import theme as T
from widgets import hint, toast, confirm
from bridge import run_bg


class SyncPage:
    """「我的 → 云同步」页（由 MinePage 的子页面机制托管）。"""

    def __init__(self, page, session, on_back=None):
        self.page = page
        self.session = session
        self.on_back = on_back
        self.on_refresh = None       # MinePage/Main 注入：整页重建
        self._f = {}                 # 名称 -> 控件（build 创建，动作时读值）
        self._show_log = False       # 日志卡展开状态（跨重建保留）

    # ---------------- 基础设施 ----------------

    @property
    def gstore(self):
        return self.session.app.gstore

    def _g(self, key, default=""):
        try:
            return self.gstore.get_global_setting(key, default)
        except Exception:
            return default

    def _gi(self, key, default):
        try:
            return int(float(str(self._g(key, default) or default)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _sc():
        """惰性取 sync_config（导入期不拉起 core）。"""
        from app.core import sync_config
        return sync_config

    def _engine(self):
        from app.core.sync_engine import SyncEngine
        cfg = self._sc().load_config()
        cfg["history_keep"] = str(self._gi("sync_history_keep", 5))
        return SyncEngine(cfg=cfg)

    def refresh(self):
        if self.on_refresh:
            self.on_refresh()
        else:
            try:
                self.page.update()
            except Exception:
                pass

    # ---------------- 保存 ----------------

    def save_fields(self) -> bool:
        """连接卡片 + 开关 → 落盘。返回是否配置完整（桶/AK/密码齐全）。"""
        sc = self._sc()
        cfg = sc.load_config()
        f = self._f
        if f:
            cfg["endpoint"] = (f["endpoint"].value or "").strip() or "https://s3.cstcloud.cn"
            cfg["bucket"] = (f["bucket"].value or "").strip()
            cfg["prefix"] = ((f["prefix"].value or "").strip()
                             or "jiuguan-sync/v1").strip("/")
            cfg["region"] = (f["region"].value or "").strip() or "us-east-1"
            cfg["user_agent"] = (f["ua"].value or "").strip() or "S3Drive/1.0.0"
            cfg["access_key"] = (f["ak"].value or "").strip()
            cfg["device_name"] = ((f["device"].value or "").strip()
                                  or cfg.get("device_name", ""))
            sk = (f["sk"].value or "").strip()
            if sk:                       # 留空 = 不修改（与桌面一致）
                sc.set_secret(cfg, sk)
                f["sk"].value = ""
                f["sk"].helper = "已保存（留空表示不修改）"
        sc.save_config(cfg)

        if f:
            g = self.gstore
            g.set_global_setting("sync_enabled", "1" if f["ck_enabled"].value else "0")
            g.set_global_setting("sync_auto_pull", "1" if f["ck_pull"].value else "0")
            g.set_global_setting("sync_auto_push", "1" if f["ck_push"].value else "0")
            g.set_global_setting("sync_include_logs", "1" if f["ck_logs"].value else "0")
            try:
                keep = int(float(str(f["keep"].value or "5").strip()))
            except (TypeError, ValueError):
                keep = 5
            g.set_global_setting("sync_history_keep", str(max(0, min(20, keep))))
        return sc.is_configured(sc.load_config())

    def _save_quiet(self) -> bool:
        """动作前先落盘；返回是否可以继续（缺连接参数就给提示）。"""
        try:
            ok = self.save_fields()
        except Exception as e:              # 落盘失败不能继续往下跑网络
            toast(self.page, "保存连接设置失败：%s" % e, error=True)
            return False
        if not ok:
            toast(self.page, "请先填写存储桶、AccessKey ID 与 Secret", error=True)
        return ok

    # ---------------- 动作 ----------------

    def _save(self, e):
        try:
            self.save_fields()
            toast(self.page, "同步设置已保存")
        except Exception as e:
            toast(self.page, "保存失败：%s" % e, error=True)
        self.refresh()

    def _test(self, e):
        if not self._save_quiet():
            return

        def work():
            sc = self._sc()
            cfg = sc.load_config()
            cli = sc.make_client(cfg)
            cli.head_bucket()
            prefix = (cfg.get("prefix") or "jiuguan-sync/v1").strip("/") + "/"
            return {"ok": True, "objects": len(cli.list_all(prefix))}

        def done(r):
            r = r or {}
            if not r.get("ok"):
                toast(self.page, "连接失败：%s" % str(r.get("error") or "未知错误")[:180],
                      error=True)
                return
            toast(self.page, "连接成功，云端目录现有 %d 个对象" % r.get("objects", 0))

        toast(self.page, "正在连接数据胶囊…")
        run_bg(self.page, work, done)

    def _apply_password(self, e):
        if not self._save_quiet():
            return
        pw = (self._f["pw"].value or "").strip()
        if not pw:
            toast(self.page, "请先输入同步密码", error=True)
            return

        def work():
            eng = self._engine()
            existed = bool(eng.read_meta())
            eng.bind(pw)
            return {"ok": True, "existed": existed}

        def done(r):
            r = r or {}
            if not r.get("ok"):
                toast(self.page, "绑定失败：%s" % str(r.get("error") or "")[:180],
                      error=True)
                self.refresh()
                return
            if r.get("existed"):
                toast(self.page, "密码正确，已与云端绑定")
            else:
                toast(self.page, "已在云端建立同步空间（请记住这个密码，丢了云端数据解不开）")
            self.refresh()

        toast(self.page, "正在校验云端密钥…")
        run_bg(self.page, work, done)

    def _sync_now(self, e):
        if not self._save_quiet():
            return
        sc = self._sc()
        try:
            if sc.get_crypto(sc.load_config()) is None:
                toast(self.page, "请先设置同步密码：在下面「同步密码」卡里输入后点「设置 / 校验」",
                      error=True)
                return
        except Exception:
            pass

        def work():
            eng = self._engine()
            eng.bind("")            # 本机已有密钥则直接复用，无需密码
            return eng.sync_all(self.session.app, include_global=True)

        def done(r):
            r = r or {}
            if "push" not in r and "pull" not in r:
                toast(self.page, "同步失败：%s" % str(
                    r.get("error") or r.get("reason") or "未知错误")[:180], error=True)
                self.refresh()
                return
            msg = "同步完成：上传 %d、下载 %d、新书 %d、删除 %d、跳过 %d、失败 %d" % (
                r.get("push", 0), r.get("pull", 0), r.get("adopted", 0),
                r.get("deleted", 0), r.get("skip", 0), r.get("failed", 0))
            if r.get("deleted") or r.get("adopted"):
                msg += ("\n项目列表有变化；被删的书已移到 .sync_trash 回收站，"
                        "请回到「项目」页刷新。")
            if r.get("failed"):
                toast(self.page, msg + "\n" + "；".join((r.get("errors") or [])[:2]),
                      error=True)
            else:
                toast(self.page, msg)
            self.refresh()

        toast(self.page, "正在与云端同步…（慢请勿退出，下行约 0.3 MB/s）")
        run_bg(self.page, work, done)

    def _restore(self, e):
        if not self._save_quiet():
            return

        def _yes():
            def work():
                eng = self._engine()
                eng.bind("")
                return eng.restore_all(self.session.app)

            def done(r):
                r = r or {}
                if r.get("ok"):
                    toast(self.page, "已恢复 %d 个项目" % r.get("done", 0))
                else:
                    why = "；".join((r.get("errors") or [])[:2]) or str(r.get("reason") or "")
                    toast(self.page, "恢复 %d/%d 个，失败：%s" % (
                        r.get("done", 0), r.get("total", 0), why[:160]), error=True)
                self.refresh()

            toast(self.page, "正在下载云端项目…")
            run_bg(self.page, work, done)

        confirm(self.page, "从云端恢复",
                "会把云端已有的项目全部拉到本机：\n"
                "· 本机已有的同一本书会被云端版本覆盖；\n"
                "· 云端没有的本机项目保持不动；\n"
                "· 覆盖前会自动留一份本地快照。\n\n云端有历史版本可回滚。确定继续吗？",
                _yes, yes_text="恢复")

    def _refresh_usage(self, e):
        if not self._save_quiet():
            return

        def work():
            return self._engine().usage(refresh=True)

        def done(r):
            r = r or {}
            if r.get("ok"):
                toast(self.page, "云端占用 %s（%d 个对象）" % (r.get("human", "?"),
                                                             r.get("objects", 0)))
                self.refresh()
            else:
                toast(self.page, "统计失败：%s" % str(r.get("reason") or r.get("error")
                                                    or "")[:160], error=True)

        toast(self.page, "正在统计云端对象…")
        run_bg(self.page, work, done)

    def _toggle_log(self, e):
        self._show_log = not self._show_log
        self.refresh()

    # ---------------- 构建 ----------------

    def _head(self) -> ft.Control:
        return T.back_head(
            "云同步", lambda: (self.on_back() if self.on_back else None))

    def _tf(self, key, label, value, **kw) -> ft.Control:
        """带标签的输入框，返回「标签 + 框 + 说明」整块（控件引用存进 self._f）。

        标签不用 TextField 的 floating label：压在描边上糊成一团，而且本页
        字段密（8 个连着排），浮标签会让每格看着比实际更高更乱。
        """
        pwd = kw.pop("password", False)
        reveal = kw.pop("can_reveal_password", False)
        helper = kw.pop("helper", "")
        tf = T.field("", str(value or ""), **kw)
        if pwd:
            tf.password = True
            tf.can_reveal_password = reveal or pwd
        self._f[key] = tf
        return T.field_group(tf, label, helper)

    def _status_card(self) -> ft.Control:
        sc = self._sc()
        cfg = sc.load_config()
        state = sc.load_state()
        try:
            bound = sc.get_crypto(cfg) is not None
        except Exception:
            bound = False
        lines = [
            ("本机名称", cfg.get("device_name") or cfg.get("device_id") or "-"),
            ("同步密码", "已绑定（无需每次输入）" if bound else "未设置"),
            ("上次同步", state.get("last_sync_at") or "从未"),
            ("云端占用", (state.get("usage") or {}).get("human") or "未统计"),
        ]
        rows = [ft.Row([
            ft.Text(k, size=11, color=T.muted(), width=72),
            ft.Text(v, size=12, expand=True),
        ], spacing=T.GAP_S) for k, v in lines]
        if state.get("last_error"):
            rows.append(ft.Text("上次错误：" + str(state["last_error"])[:160],
                                size=11, color=T.danger()))
        return T.card(ft.Column([
            ft.Row([ft.Container(
                        ft.Icon(ft.Icons.CLOUD_SYNC_OUTLINED, color=T.brand(),
                                size=19),
                        width=34, height=34, border_radius=T.R_MD,
                        bgcolor=T.soft(T.brand(), 0.10),
                        alignment=ft.Alignment.CENTER),
                    ft.Text("同步状态", size=14, weight=ft.FontWeight.W_600,
                            expand=True)]),
            *rows,
        ], spacing=T.GAP_S - 2))

    def build(self) -> ft.Control:
        self._f = {}
        sc = self._sc()
        try:
            cfg = sc.load_config()
            secret_saved = bool(sc.get_secret(cfg))
        except Exception:
            cfg, secret_saved = {}, False
        g = self._g

        # ---- 连接 ----
        conn = T.card(ft.Column([
            ft.Text("连接（数据胶囊 · S3）", size=14, weight=ft.FontWeight.W_600),
            self._tf("endpoint", "接入点", cfg.get("endpoint")
                     or "https://s3.cstcloud.cn"),
            self._tf("bucket", "存储桶", cfg.get("bucket")),
            self._tf("prefix", "云端目录前缀", cfg.get("prefix") or "jiuguan-sync/v1"),
            self._tf("region", "区域", cfg.get("region") or "us-east-1"),
            self._tf("ua", "User-Agent", cfg.get("user_agent") or "S3Drive/1.0.0"),
            self._tf("ak", "AccessKey ID", cfg.get("access_key")),
            self._tf("sk", "AccessKey Secret", "",
                     password=True,
                     helper="已保存（留空表示不修改）" if secret_saved
                     else "粘贴 Secret Access Key"),
            self._tf("device", "本机名称（云端显示）", cfg.get("device_name")),
            hint("User-Agent 必须与 AccessKey 绑定的「应用」一致：胶囊按应用类型校验 UA，"
                 "不匹配一律返回 401 空响应（看着像网关故障）。胶囊后台「客户端访问」里"
                 "能看到该 AK 的应用名：建在 S3Drive 下就填 S3Drive/1.0.0。"),
        ], spacing=T.GAP_S,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH))

        # ---- 同步密码 ----
        pw = T.card(ft.Column([
            ft.Text("同步密码", size=14, weight=ft.FontWeight.W_600),
            hint("云端数据（含模型 API Key）都用它加密。第一台设备设置密码；"
                 "第二台设备输入同一个密码即完成绑定。密码不上传，"
                 "丢了云端数据和历史版本都解不开，只能重置后整体重传。"),
            self._tf("pw", "同步密码", "", password=True),
            T.tonal("设置 / 校验", icon=ft.Icons.KEY,
                    on_click=self._apply_password, expand=True),
        ], spacing=T.GAP_S,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH))

        # ---- 行为 ----
        def _sw(key, label, on, desc=""):
            widget, sw = T.switch_row(label, bool(on), desc=desc)
            self._f[key] = sw
            return widget

        beh = T.card(ft.Column([
            ft.Text("同步行为", size=14, weight=ft.FontWeight.W_600),
            _sw("ck_enabled", "启用云同步（关掉完全不联网）",
                g("sync_enabled", "0") == "1"),
            _sw("ck_pull", "打开项目时自动拉取云端最新版",
                g("sync_auto_pull", "1") == "1",
                "在首页打开一本书时先比对云端版本，云端更新就合并下来"),
            _sw("ck_push", "内容改动静默 60 秒后自动上传",
                g("sync_auto_push", "1") == "1",
                "生成为止先攒着，停止改动 60 秒再推一次，避免边写边传"),
            _sw("ck_logs", "连请求日志一起同步",
                g("sync_include_logs", "0") == "1",
                "体积大十倍以上，一般不用开"),
            self._tf("keep", "云端每个项目保留历史版本（份）",
                     self._gi("sync_history_keep", 5),
                     keyboard_type=ft.KeyboardType.NUMBER,
                     helper="留最近几份覆盖前的云端旧版。填 0 = 关闭云端历史："
                            "覆盖后旧版就找不回来了（本机覆盖前仍留一份快照）"),
            hint("冲突规则：谁最后改动谁覆盖（LWW）。覆盖前一定先存快照——"
                 "本机旧版进项目 backups/sync_overwrite_*.db，云端旧版进 history。"),
        ], spacing=T.GAP_S,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH))

        # ---- 动作 ----
        acts = ft.Row([
            T.primary("立即同步", icon=ft.Icons.SYNC, on_click=self._sync_now,
                      expand=True),
        ], spacing=T.GAP_S)
        acts2 = ft.Row([
            T.tonal("测试连接", icon=ft.Icons.WIFI_TETHERING,
                    on_click=self._test, expand=True),
            T.tonal("刷新用量", icon=ft.Icons.DATA_USAGE,
                    on_click=self._refresh_usage, expand=True),
        ], spacing=T.GAP_S)
        acts3 = T.ghost("从云端恢复全部项目", icon=ft.Icons.CLOUD_DOWNLOAD,
                        on_click=self._restore)

        # ---- 日志 ----
        tail = ""
        try:
            tail = sc.SyncLog().tail(30)
        except Exception:
            pass
        log_card = T.card(ft.Column([
            ft.Row([
                ft.Text("同步日志（最近 30 行）", size=13,
                        weight=ft.FontWeight.W_600, expand=True),
                T.ghost("隐藏" if self._show_log else "展开",
                        on_click=self._toggle_log),
            ]),
            *([T.inset(ft.Text(tail or "（还没有日志）", size=10, selectable=True,
                               font_family="monospace"))] if self._show_log else []),
        ], spacing=T.GAP_S))

        body = [
            self._head(),
            self._status_card(),
            acts, acts2, acts3,
            conn,
            pw,
            beh,
            log_card,
            hint("同步范围：所有项目的 project.db（大纲/正文/记忆/设定）+ 全局配置"
                 "（模型与 API Key、偏好卡、预设、任务模板、破甲库）。"
                 "连接凭据只留在本机，每台设备各配一次。"),
            # 尾部留白：不留的话滚到底时最后一段紧贴固定保存栏，像被裁掉了。
            ft.Container(height=T.GAP_L),
        ]
        save_bar = ft.Container(
            T.primary("保存设置", icon=ft.Icons.SAVE, on_click=self._save,
                      expand=True),
            padding=ft.Padding(left=T.GAP_L, right=T.GAP_L,
                               top=T.GAP_S, bottom=T.GAP_S),
            bgcolor=T.bg(),
            border=ft.Border.only(top=ft.BorderSide(1, T.line())),
        )
        # ⚠ 左右内边距不能写在 Column 上：Flet 0.86 的 Column 没有 padding 参数
        # （会直接抛 TypeError，整页白屏只剩 Error running app）。
        scroll = ft.Container(
            ft.Column(body, scroll=ft.ScrollMode.AUTO, expand=True,
                      spacing=T.GAP_S,
                      horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
            padding=ft.Padding(left=T.GAP_L, right=T.GAP_L, top=0, bottom=0),
            expand=True)
        return ft.Container(
            ft.Column([scroll, save_bar], expand=True, spacing=0,
                      horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
            padding=ft.Padding(left=0, right=0, top=T.GAP_S + 2, bottom=0),
            expand=True)
