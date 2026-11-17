# -*- coding: utf-8 -*-
"""创作页：总纲 / 分卷 / 大纲 / 写作台 / 记忆 / 设定 六个页签。

流程与落库对齐桌面版（总纲候选→锁定导入分卷→切章→细化大纲→整章正文→归档），
UI 面向手机：确认环节用 bottom sheet / 对话框。

总纲支持「分批生成」（对齐桌面 2026-11-07）：超大故事专用，AI 每批写几卷就停、
批末带【分批进度】标记，每批停下四选确认（继续/收尾/微调/放弃），跨会话可续跑；
写作台「更多」含「清空全书正文」（删什么/留什么摊开确认，清前内核自动整库备份）；
大纲页含「⚡一键细化全部大纲」（AutoPilot scope="outline"，细化完即停）。

写作台支持两种写法（`store.get_prose_mode()`）：
- `chapter` 一次性（整章写完）：编辑器 + 生成整章 + 改写 + 接受本章；
- `batch` 分批（接力写作）：按细纲块分组逐批生成，滚动状态包自动衔接
  下一批；批次卡片可查看/从本批重新生成，全部批完成后合并接受为整章正文。
  细纲块卡片保留单块生成/编辑入口。
两种模式共用编辑器（整章正文 / 批次合并预览）与「更多」
（本章备注 / 版本快照 / 风格样本 / 粘贴细纲·正文 / 改写设置），
生成结果的拒答/截断/未写完判定统一走 `src/diagnose.py`，落库前必过
`_guard_incomplete`（与桌面「应用结果」同口径）。
"""
import flet as ft

import theme as T
from widgets import hint, empty, toast, confirm
from bridge import StreamTask, run_bg
from gen import start_generation

from app.core.assembler import Assembler



def chapter_rule_text(store) -> tuple[str, int | None]:
    """读分卷设置 → ({chapter_rule} 文本, 期望章数)。与桌面分卷页同语义。"""
    mode = str(store.get_setting("volume_chapter_mode", "auto") or "auto")
    raw = str(store.get_setting("volume_chapter_count", "") or "").strip()
    expected = int(raw) if (mode == "exact" and raw.isdigit()) else None
    if expected is not None:
        return (f"本卷必须正好切分为 {expected} 章。"
                f"必须输出且仅输出 {expected} 个章节标题，不得多于或少于 {expected} 章。"), expected
    return "本卷章数不设硬性规定，由你根据剧情节奏与总纲篇幅自行判断。", None


STATUS_COLORS = {"未规划": ft.Colors.GREY, "有大纲": ft.Colors.BLUE,
                 "有细纲": ft.Colors.INDIGO, "写作中": ft.Colors.ORANGE,
                 "已完成": ft.Colors.GREEN, "已归档": ft.Colors.TEAL,
                 "已锁定": ft.Colors.DEEP_PURPLE}

# 页签内容区的左右内边距（`_view_for_tab` 用）。按视口算宽度的控件必须把它
# 一起减掉，否则会顶出屏幕右缘——写作台底部操作栏的「更多」就这么被裁过。
PAGE_PAD = 12


class WritingPage:
    def __init__(self, page, session):
        self.page = page
        self.session = session
        self.on_refresh = None  # 主界面注入：整页重建
        self._tab = 0
        self._task = None       # 当前 StreamTask（可停止）
        # 当前章指针：**全页签共用**（2026-11-17 老板要求）。早先是两套独立指针
        # （`_outline_chapter_id` / `_desk_chapter_id`），在大纲页换到第三章、
        # 切到写作台还停在上次的章，来回翻要找两次。现在只有这一个。
        self._chapter_id = None
        self._chapter_scope = None   # 指针归属项目（store.folder），换项目自动复位
        # 改写来源的章（接受后即使已有快照也自动重归档，见 _after_prose_done）
        self._pending_rewrite: set = set()
        # 切章接力队列（总纲迭代保存后 _try_relay 填充，_relay_pump 串行推进）
        self._relay_queue: list = []
        # 分卷页展开态记忆（2026-11-17）：refresh() 整页重建会把 ExpansionTile
        # 全部收起，改了第 5 卷再回来要重新找——按卷 id 记住，重建时恢复。
        self._vol_open_ids: set = set()
        # ⚡自动模式引擎：全书流水线（build 时创建）与一键细化大纲（确认后创建，
        # scope="outline"）。同一时间只允许一台在跑（_ap_running_any 统一互斥）。
        self._ap = None
        self._ap_outline = None
        # 「retrying」状态的顶栏提示文本（引擎重试等待期间如实显示，非完成非出错）
        self._ap_retrying = ""
        # 总纲分批生成状态机（对齐桌面 synopsis_page 2026-11-07 两轮 af9aa94/37fe742）：
        # _batch_no=0 空闲；>0 链活着（_batch_running=True 运行中 / False 暂停微调，
        # 总纲页按钮变「继续下一批（第N+1批）」）。桌面把累积原文放在编辑器里，
        # 手机无持久编辑器 → 累积在 _batch_partial（内存），并同步 4 个项目设置键
        # （synopsis_batch_partial/no/req/plan）支持跨会话续跑。
        self._batch_running = False
        self._batch_no = 0
        self._batch_snapshot = ""   # 发起分批前的总纲原文（「放弃全部」时恢复）
        self._batch_req = ""        # 分批创作要求（每批注入）
        self._batch_plan = ""       # 批次规划文本（每批注入，可重新规划）
        self._batch_partial = ""    # 已累积的全部批次原文（尚未落库）

    # ---------- 通用 ----------

    TAB_LABELS = ["总纲", "分卷", "大纲", "写作台", "记忆", "设定"]

    def build(self) -> ft.Control:
        if not self.session.has_project:
            return ft.Container(empty("先到「项目」页打开或新建一部作品"),
                                padding=14, expand=True)
        if getattr(self, "_ap", None) is None:
            from autopilot import AutoPilot
            self._ap = AutoPilot(self.page, self.session)
            self._ap.on_state = self._ap_state
            self._ap.on_step = self._ap_set_label
            self._ap.on_chunk = lambda n: None  # 步骤切换才刷，字数不逐帧刷
        views = [ft.Container(self._view_for_tab(i), expand=True) for i in range(6)]
        # 顶栏挂在 host 里，自动模式状态变化只换 host.content（见 _ap_refresh_bar）
        self._ap_host = ft.Container(self._ap_bar())
        return ft.Tabs(
            length=6,
            selected_index=self._tab,
            on_change=self._on_tab_change,
            content=ft.Column([
                self._ap_host,
                ft.TabBar(
                    tabs=[ft.Tab(label=t) for t in self.TAB_LABELS],
                    scrollable=True,
                    tab_alignment=ft.TabAlignment.START,
                    indicator_color=ft.Colors.PRIMARY,
                    label_color=ft.Colors.PRIMARY,
                ),
                ft.TabBarView(controls=views, expand=True),
            ], expand=True, spacing=0),
            expand=True,
        )

    # ---------- ⚡ 自动模式 ----------

    def _ap_active(self):
        """当前应显示在顶栏的引擎：outline 一键细化在跑时优先显示它
        （互斥保证同一时间只有一台在跑）。"""
        ap2 = getattr(self, "_ap_outline", None)
        if ap2 is not None and ap2.running:
            return ap2
        return getattr(self, "_ap", None)

    def _ap_running_any(self) -> bool:
        """任一自动引擎在跑（⚡全书流水线 / ⚡一键细化大纲 共用同一互斥判据，
        对齐桌面「同一本书一台引擎」）。"""
        for ap in (getattr(self, "_ap", None), getattr(self, "_ap_outline", None)):
            if ap is not None and ap.running:
                return True
        return False

    def _ap_bar(self) -> ft.Control:
        ap = self._ap_active()
        label = (self._ap_label or {}).get("v", "") if hasattr(self, "_ap_label") else ""
        if ap is None or not ap.running:
            # 收起态压成一条细提示：这行常驻在页签栏上方，旧版一句长文案 +
            # 一个大按钮，天天占着一条首屏高度，实际很少点。
            return ft.Container(
                ft.Row([
                    ft.Icon(ft.Icons.BOLT, size=15, color=T.warn()),
                    ft.Text("自动模式", size=12, weight=ft.FontWeight.W_600),
                    ft.Text("从当前进度接力到完本", size=11, color=T.muted(),
                            expand=True, max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS),
                    ft.TextButton("开启", on_click=self._ap_start,
                                  style=self._minor_btn_style()),
                ], spacing=6),
                padding=ft.Padding(left=T.GAP_S, right=2, top=0, bottom=0),
                bgcolor=T.soft(T.warn(), 0.08),
                border_radius=T.R_SM,
                margin=ft.Margin(left=0, right=0, top=0, bottom=6))
        if ap.paused:
            state_text = "已暂停：" + label
            btn = ft.FilledTonalButton("继续", icon=ft.Icons.PLAY_ARROW,
                                       on_click=lambda e: ap.resume())
            color = ft.Colors.ORANGE
        else:
            retrying = getattr(self, "_ap_retrying", "")
            if retrying:
                # 重试等待中（2026-11-15 接线）：引擎到点自动续跑，这里如实显示，
                # 不当「完成」也不当「出错」处理；「暂停」可取消本次等待
                state_text = f"⚠ 重试等待中：{retrying}"
                color = ft.Colors.ORANGE
            else:
                state_text = label or "自动推进中…"
                color = ft.Colors.GREEN
            btn = ft.OutlinedButton("暂停", icon=ft.Icons.PAUSE,
                                    on_click=lambda e: ap.pause())
        return ft.Container(
            ft.Row([
                ft.Text(state_text, size=12, expand=True,
                        overflow=ft.TextOverflow.ELLIPSIS),
                btn,
                ft.IconButton(ft.Icons.STOP_CIRCLE_OUTLINED,
                              on_click=lambda e: self._ap_stop()),
            ]),
            bgcolor=T.soft(color, 0.10),
            border_radius=T.R_SM,
            padding=ft.Padding(left=12, right=6, top=6, bottom=6),
            margin=ft.Margin(left=0, right=0, top=0, bottom=6),
        )

    def _ap_start(self, e=None):
        brief_field = ft.TextField(
            label="创作要求（可选）", multiline=True, min_lines=2, max_lines=4,
            value=self.session.store.get_setting("auto_pilot_brief", "") or "",
            hint_text="题材/主角/风格/结局倾向…留空则由 AI 依据现有进度自由发挥")

        def _ok(ev):
            self.page.pop_dialog()
            self.session.store.set_setting("auto_pilot_brief",
                                           (brief_field.value or "").strip())
            confirm(self.page, "开启自动模式",
                    "AI 将从当前进度自动接力推进（总纲→分卷→大纲→正文→归档）直至完本，"
                    "生成结果直接落库不再逐步确认（正文/大纲自动存快照，可回滚）。"
                    "确定开启？",
                    self._ap_confirm_start, yes_text="开启")

        def _cancel(ev):
            self.page.pop_dialog()
        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("⚡ 自动模式"),
            content=ft.Container(ft.Column([
                ft.Text("从当前进度自动接管，已完成的步骤自动跳过，绝不重复生成。",
                        size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                brief_field,
            ], tight=True, spacing=10), width=440),
            actions=[ft.TextButton("取消", on_click=_cancel),
                     ft.FilledButton("下一步", on_click=_ok)],
            actions_alignment=ft.MainAxisAlignment.END,
        ))

    def _ap_confirm_start(self):
        if self._ap_running_any():
            toast(self.page, "已经有一个自动任务在跑，不能同时开第二台", error=True)
            return
        self._ap.start()
        self.refresh()

    def _ap_stop(self):
        confirm(self.page, "退出自动模式", "彻底停止自动推进？",
                lambda: (self._ap.stop(), self.refresh()))

    def _ap_state(self, state: str, info: str = ""):
        if state == "retrying":
            # 重试等待中：只更新顶栏提示，等引擎定时器到点自动续跑；
            # 绝不能误触「done（全书完本）」或「error（暂停交还用户）」语义
            self._ap_retrying = info or ""
        elif state in ("running", "paused", "idle", "done", "error"):
            self._ap_retrying = ""
        if state == "done":
            toast(self.page, info or "全书完本！")
        elif state == "error":
            toast(self.page, f"自动模式暂停：{info}", error=True)
        if state in ("done", "error", "idle"):
            # 收尾：章节状态/正文都变了，整页重建一次
            self.refresh()
        else:
            # 运行 / 暂停 / 重试等待：**只换顶栏**。自动模式跑一整本书要经历
            # 几十上百次状态回调，每次都整页重建（6 个页签全部重查库重拼控件）
            # 是挂机时卡顿乃至闪退的主要来源。
            self._ap_refresh_bar()

    def _ap_refresh_bar(self):
        """只重建「自动模式」那一行（不碰页签内容）。"""
        host = getattr(self, "_ap_host", None)
        if host is None:
            return
        try:
            host.content = self._ap_bar()
            self.page.update()
        except Exception:
            pass

    def _ap_set_label(self, label):
        if label is not None:
            self._ap_label = getattr(self, "_ap_label", {"v": ""})
            self._ap_label["v"] = label
            self.refresh()

    def _view_for_tab(self, i: int) -> ft.Control:
        views = [self._synopsis_view, self._volumes_view, self._outline_view,
                 self._desk_view, self._memory_view, self._lore_view]
        return ft.Container(views[i](), padding=PAGE_PAD, expand=True)

    def _on_tab_change(self, e):
        """切页签：只记索引并刷新，**不整页重建**（2026-11-16 稳定性修复）。

        旧实现走 refresh() → 主界面 `body.content = page.build()`，等于把 6 个
        页签全部重建一遍（每个页签都要重新查库、拼几十个控件）。手机上表现为
        切页签卡顿，写作台还会丢掉编辑器里尚未「接受本章」的手改稿——重建把
        TextField 的 value 刷回库里的旧正文。各页签自己的操作本来就会调
        refresh()，切换本身不需要重建。
        """
        self._tab = e.control.selected_index
        try:
            self.page.update()
        except Exception:
            pass

    def refresh(self):
        # 总纲分批运行中：批次成果在内存累积区（库内容还是旧总纲），整页重建会把
        # 界面刷回旧库内容、盖掉确认框底下的批次展示——只做轻量刷新（对齐桌面
        # synopsis_page.refresh 的跳过防呆）；批次链结束走落库/放弃 → 恢复正常刷新。
        if getattr(self, "_batch_running", False):
            try:
                self.page.update()
            except Exception:
                pass
            return
        if self.on_refresh:
            self.on_refresh()
        else:
            try:
                self.page.update()
            except Exception:
                pass

    def _dlg_h(self, base: int) -> int:
        """对话框高度兜底（Flet 0.86 小屏坑）：不超过页面可用高度。"""
        ph = getattr(self.page, "height", 0) or 0
        if ph and ph > 320:
            return max(240, min(base, int(ph - 96)))
        return base

    def _running(self, tk: StreamTask, title: str):
        """生成中模态框：字数进度 + 停止。返回关闭函数。"""
        info = ft.Text("0 字", size=13)

        def tick(buffer: str):
            info.value = f"{len(buffer)} 字"
        tk.on_text = tick

        def _close_dialog():
            try:
                self.page.pop_dialog()
            except Exception:
                pass

        def _stop(e):
            tk.stop()
        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text(title),
            content=ft.Row([ft.ProgressRing(width=30, height=30), info],
                           alignment=ft.MainAxisAlignment.CENTER),
            actions=[ft.TextButton("停止", on_click=_stop)],
            actions_alignment=ft.MainAxisAlignment.END,
        ))
        return _close_dialog

    def _text_dialog(self, title: str, label: str, value: str = "",
                     multiline=True, on_ok=None, ok_text="保存"):
        """通用文本编辑对话框。"""
        field = ft.TextField(label=label, value=value, multiline=multiline,
                             min_lines=4 if multiline else 1,
                             max_lines=16 if multiline else 1)

        def _close(ev):
            self.page.pop_dialog()

        def _ok(ev):
            self.page.pop_dialog()
            if on_ok:
                on_ok(field.value or "")
        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text(title),
            content=ft.Container(field, width=520, height=420 if multiline else None),
            actions=[ft.TextButton("取消", on_click=_close),
                     ft.FilledButton(ok_text, on_click=_ok)],
            actions_alignment=ft.MainAxisAlignment.END,
        ))

    def _pick_chapter(self, on_pick, current_id=None):
        """章节选择 bottom sheet（当前章打勾，省得再找）。"""
        store = self.session.store
        if current_id is None:
            cur = self._current_chapter()
            current_id = cur["id"] if cur else None
        rows = []
        for c in store.list_chapters():
            rows.append(ft.ListTile(
                dense=True,
                leading=ft.Container(
                    ft.Text(str(c.get("order_index", "")), size=12),
                    width=30, alignment=ft.Alignment.CENTER),
                title=ft.Text(c.get("title") or "未命名", size=14),
                subtitle=ft.Text(c.get("status", ""), size=11,
                                 color=STATUS_COLORS.get(c.get("status"),
                                                         ft.Colors.GREY)),
                trailing=(ft.Icon(ft.Icons.CHECK, size=18, color=T.brand())
                          if c["id"] == current_id else
                          ft.Icon(ft.Icons.CHEVRON_RIGHT, size=16)),
                on_click=lambda e, ch=c: (self.page.pop_dialog(), on_pick(ch)),
                selected=(c["id"] == current_id),
            ))
        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column(
                [ft.Text("选择章节", size=16, weight=ft.FontWeight.BOLD)] + rows,
                scroll=ft.ScrollMode.AUTO), padding=16, height=460),
            show_drag_handle=True))

    # ---------- 当前章：全页签共用一个指针 ----------

    def _chapter_list(self) -> list[dict]:
        return self.session.store.list_chapters()

    def _current_chapter(self):
        """当前章（大纲页 / 写作台共用）。

        - **换项目必须复位**：章节 id 是每个库从 1 自增，换一本书后旧 id 会撞上
          别的章（不回退的话直接跳到一个和用户预期无关的章）；
        - 指针指向的章被删/被回收：回落到第一章，不抛错。
        """
        store = self.session.store
        chapters = self._chapter_list()
        if not chapters:
            return None
        scope = getattr(store, "folder", "") or ""
        if self._chapter_scope is None:
            # 首次访问只登记归属，不复位：`_chapter_id` 可能已被调用方预设
            self._chapter_scope = scope
        elif scope != self._chapter_scope:
            self._chapter_scope = scope
            self._chapter_id = None
        for c in chapters:
            if c["id"] == self._chapter_id:
                return store.get_chapter(c["id"])
        self._chapter_id = chapters[0]["id"]
        return store.get_chapter(chapters[0]["id"])

    def _chapter_neighbors(self, cid):
        """→ (上一章, 下一章)；列表顺序即 order_index 升序，没有则为 None。"""
        chapters = self._chapter_list()
        ids = [c["id"] for c in chapters]
        if cid not in ids:
            return None, None
        i = ids.index(cid)
        return (chapters[i - 1] if i > 0 else None,
                chapters[i + 1] if i + 1 < len(chapters) else None)

    def _chapter_arrows(self, ch) -> ft.Control:
        """◀ 上一章 / 下一章 ▶（到头自动禁用，点不动就是到头了）。

        为什么用箭头不用「上一章」文字按钮：写作台章节卡右上角和标题抢宽度，
        手机窄屏放两个带字按钮会把章名挤成两三个字（Flet 的 Row 不换行）。
        箭头是固定 40dp 的通用语义，平板上也好按。
        """
        prev_ch, next_ch = self._chapter_neighbors(ch["id"])

        def _go(target):
            def _cb(e):
                self._goto_chapter(target)
            return _cb

        return ft.Row([
            ft.IconButton(ft.Icons.CHEVRON_LEFT, icon_size=22, tooltip="上一章",
                          visual_density=ft.VisualDensity.COMPACT,
                          disabled=prev_ch is None, on_click=_go(prev_ch)),
            ft.IconButton(ft.Icons.CHEVRON_RIGHT, icon_size=22, tooltip="下一章",
                          visual_density=ft.VisualDensity.COMPACT,
                          disabled=next_ch is None, on_click=_go(next_ch)),
        ], spacing=0, vertical_alignment=ft.CrossAxisAlignment.CENTER)

    def _unsaved_editor_text(self, ch) -> str:
        """编辑器里还没「接受本章」的手改稿（空串 = 没有 / 与库内一致）。

        加「上一章/下一章」后切章变成高频动作，不拦一下等于把刚手写的段落
        直接丢掉（切章会整页重建，编辑器按库内正文重新填）。
        """
        ed = getattr(self, "_desk_editor", None)
        if ch is None or ed is None:
            return ""
        text = (ed.value or "").strip()
        if not text:
            return ""
        store = self.session.store
        if text == (self._accepted_prose(ch["id"]) or "").strip():
            return ""
        if text == (store.prose_batch_merged(ch["id"]) or "").strip():
            return ""
        return text

    def _goto_chapter(self, ch):
        """切到指定章（箭头、换一章、大纲页选章都走这里）。"""
        if not ch or ch.get("id") == self._chapter_id:
            return
        if self._task is not None and not self._task.stopped:
            toast(self.page, "正在生成，先停止再切章", error=True)
            return
        if self._unsaved_editor_text(self._current_chapter()):
            confirm(self.page, "本章还有没落库的手改稿",
                    "切到别的章会把编辑器里这段没点「接受本章」的正文丢掉。"
                    "要切吗？",
                    lambda: self._apply_chapter(ch["id"]), yes_text="丢掉并切章")
            return
        self._apply_chapter(ch["id"])

    def _apply_chapter(self, cid: int):
        self._chapter_id = cid
        self.refresh()

    # =====================================================
    # 页签 1：总纲
    # =====================================================

    def _synopsis_view(self) -> ft.Control:
        store = self.session.store
        s = store.current_synopsis()
        locked = bool(s.get("is_locked")) if s else False
        # 总纲分批链活着：展示区改显已累积的批次原文（尚未落库，桌面=编辑器内容）
        chain_alive = self._batch_no > 0
        content = ((self._batch_partial or "").strip() if chain_alive
                   else ((s.get("content") or "").strip() if s else ""))
        head = ft.Row([
            ft.Text("故事总纲", size=17, weight=ft.FontWeight.BOLD, expand=True),
            ft.Chip(ft.Text("已锁定" if locked else "未锁定"),
                    bgcolor=ft.Colors.with_opacity(0.15, ft.Colors.TEAL if locked
                                                   else ft.Colors.GREY)),
        ])
        body_controls = [head]
        if chain_alive:
            body_controls.append(hint(
                f"分批进行中：已写到第 {self._batch_no} 批，全部完成后才会落库为新总纲"
                + ("（本批生成中…）" if self._batch_running
                   else "（已暂停：可点下方「微调」改已生成部分，再点「继续下一批」）")))
        if content:
            body_controls.append(ft.Container(ft.Text(content, size=13, selectable=True),
                             bgcolor=ft.Colors.with_opacity(0.05, ft.Colors.ON_SURFACE),
                             border_radius=10, padding=12,
                             margin=ft.Margin(left=0, right=0, top=8, bottom=8)))
        elif chain_alive:
            body_controls.append(empty("第 1 批生成中…\n每批完成会停下来让你确认"))
        else:
            body_controls.append(empty("还没有总纲\n点下方「AI 生成总纲」开始，"
                                       "或「手动编辑」直接写"))
        # 分批按钮双态（对齐桌面 _batch_btn_sync）：链活着→「继续下一批（第N+1批）」
        # 运行中禁用；空闲→「分批生成」。gen/iter 在链活着期间禁用——一次生成/迭代
        # 落库会覆盖批次累积内容。
        batch_label = (f"继续下一批（第{self._batch_no + 1}批）" if chain_alive
                       else "分批生成")
        actions = ft.Column([
            ft.Row([
                ft.FilledButton("AI 生成总纲", on_click=self._syn_generate,
                                icon=ft.Icons.AUTO_AWESOME, disabled=chain_alive),
                ft.FilledTonalButton(batch_label, on_click=self._generate_batched,
                                     icon=ft.Icons.PLAYLIST_ADD,
                                     disabled=(self._batch_running if chain_alive
                                               else bool(locked))),
                ft.TextButton("微调" if chain_alive else "AI 迭代",
                              on_click=lambda e: self._batch_tune_dialog()
                              if chain_alive else self._syn_iterate(e),
                              disabled=chain_alive and self._batch_running),
            ], spacing=8, wrap=True),
            # 协议栈增强开关（2026-09-23）：全局设置，与桌面版共用同一个键。
            # 破不了甲时打开（仅总纲任务生效，会多耗 token）。
            ft.Switch(label="协议栈增强（破不了甲时打开）",
                      value=str(store.get_setting("protocol_stack_enabled", "0")) == "1",
                      on_change=self._syn_toggle_stack),
            ft.Row([
                ft.TextButton("手动编辑", on_click=self._syn_edit,
                              disabled=bool(locked)),
                ft.TextButton("锁定并导入分卷", on_click=self._syn_lock,
                              icon=ft.Icons.LOCK,
                              disabled=not content or locked or chain_alive),
            ], spacing=4, wrap=True),
            hint("一般故事用「AI 生成总纲」；超大故事用「分批生成」——AI 每次只写几卷，"
                 "每批结束停下来确认（继续/收尾/微调/放弃），每卷概述更充分。"
                 "锁定时自动识别总纲里的「### 分卷一/二…」并导入分卷页。"),
        ], spacing=4)
        # 2026-11-16 美化改版：操作按钮排到内容前面（原来垫在长总纲后面，
        # 要滚到底才够到「AI 生成总纲」）
        return ft.Column([head, actions] + body_controls[1:],
                         scroll=ft.ScrollMode.AUTO, expand=True, spacing=2)

    def _syn_toggle_stack(self, e):
        """协议栈增强开关：全局设置键，与桌面版共用（详见 docs/酒馆破限对照排查SOP.md §4.9）。

        只影响总纲任务（role=synopsis）的组装，其他阶段不注入——协议栈内含总纲的
        输出格式要求，注入正文/大纲会污染它们的输出格式。
        """
        on = bool(getattr(e.control, "value", False))
        self.session.store.set_setting("protocol_stack_enabled", "1" if on else "0")
        toast(self.page, "协议栈已开启（仅总纲任务，会多耗 token）" if on
              else "协议栈已关闭（恢复常规组装）")

    def _syn_edit(self, e=None, preset_text: str = ""):
        store = self.session.store
        s = store.current_synopsis()
        value = preset_text or ((s.get("content") or "") if s else "")

        def _save(text: str):
            if not text.strip():
                toast(self.page, "总纲为空", error=True)
                return
            title = (text.strip().splitlines() or [""])[0].lstrip("# ").strip()[:30]
            store.save_synopsis(text.strip(), title, lock=False)
            toast(self.page, "已保存为新版本")
            self.refresh()
        self._text_dialog("编辑总纲", "总纲内容（第一行将作为标题）", value, on_ok=_save)

    def _syn_lock(self, e=None, content: str = "", title: str = ""):
        """锁定总纲并导入分卷（换纲判断与桌面版同款）。"""
        store = self.session.store
        s = store.current_synopsis()
        content = content or (s.get("content") or "")
        title = title or (s.get("title") or "")
        from app.core.pipeline import Pipeline
        parsed = Pipeline.parse_synopsis_volumes(content)
        volumes = parsed.get("volumes") or []
        if not volumes:
            toast(self.page, "没有识别到「### 分卷一/二…」，请检查总纲格式", error=True)
            return

        def _yes():
            fingerprint = store.synopsis_fingerprint(content)
            last = store.get_last_locked_synopsis_fingerprint()
            has_old = bool(store.list_volumes())
            changed = (fingerprint != last) if last else \
                (has_old and store.synopsis_volumes_differ(volumes))
            store.lock_synopsis_with_volumes(
                content, title, volumes, replace_existing=bool(changed and has_old))
            toast(self.page, f"总纲已锁定，导入 {len(volumes)} 个分卷")
            self._tab = 1
            self.refresh()
        confirm(self.page, "锁定总纲",
                f"识别到 {len(volumes)} 个分卷。锁定后总纲不可修改"
                f"{'；内容有变化将清空并重建旧分卷' if self.session.store.list_volumes() else ''}。"
                "确定锁定？", _yes)

    def _syn_generate(self, e):
        # 分批链活着时总纲页按钮已禁用；这里再拦一层防程序化重入
        # （一次生成落库会覆盖批次累积内容，对齐桌面 gen_btn 禁用语义）
        if self._batch_no > 0:
            toast(self.page, "总纲分批进行中——收尾或放弃后再生成总纲", error=True)
            return
        req_field = ft.TextField(label="创作要求（可选）", multiline=True, min_lines=2,
                                 max_lines=4,
                                 hint_text="题材、主角、金手指、风格、结局倾向…留空让 AI 自由发挥")

        def _ok(ev):
            self.page.pop_dialog()
            self._do_syn_generate((req_field.value or "").strip())

        def _cancel(ev):
            self.page.pop_dialog()
        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("生成总纲"),
            content=ft.Container(req_field, width=460),
            actions=[ft.TextButton("取消", on_click=_cancel),
                     ft.FilledButton("下一步", on_click=_ok)],
            actions_alignment=ft.MainAxisAlignment.END,
        ))

    def _do_syn_generate(self, user_req: str):
        from app.core.pipeline import Pipeline
        from app.core.task_templates import render as render_task
        store = self.session.store
        task = render_task("brainstorm", store)
        close_running = None

        def on_started(tk):
            nonlocal close_running
            close_running = self._running(tk, "AI 生成总纲…")

        def on_done(result):
            if close_running:
                close_running()
            if not result.get("ok"):
                return  # toast 已在 gen 里统一弹
            cands, err = Pipeline.parse_synopsis_candidates(result.get("text") or "")
            if err or not cands:
                toast(self.page, f"总纲候选解析失败：{err or '结果为空'}", error=True)
                return
            self._pick_candidate(cands)

        start_generation(self.page, self.session, role="synopsis",
                         context={"user_req": user_req}, task=task,
                         title="生成总纲", on_done=on_done, on_started=on_started)

    # ---------- 总纲分批生成（超大故事专用，对齐桌面 2026-11-07 两轮） ----------
    # 与一次生成（brainstorm）并行的可选通道：AI 每批写几卷就停，批末输出
    # 【分批进度】标记（Pipeline.parse_synopsis_batch_marker 解析），下一批把
    # 此前所有批次完整原文注入（context["synopsis_prev_batches"] → 内核
    # assembler critical 模块）。每批停下四选确认（继续/收尾/微调/放弃）。
    # 状态机：_batch_no=0 空闲；>0 链活着（_batch_running=True 运行中 /
    # False 暂停微调）。累积原文桌面放编辑器，手机放 _batch_partial，
    # 并同步 4 个项目设置键跨会话续跑。

    def _generate_batched(self, e=None):
        """「分批生成」入口（对齐桌面 _generate_batched）：链活着→续下一批；
        检测到未完成分批 → 问「接着写 / 丢弃重新开始 / 取消」；否则收创作要求
        → 收批数 → 规划 → 确认后跑第 1 批。"""
        store = self.session.store
        if store is None:
            return
        if self._batch_no > 0:
            # 链活着：运行中拒绝重入（防两条分批链互相覆盖）；暂停态直接续下一批
            if self._batch_running:
                toast(self.page, "上一批还在生成中，等本批完成弹出确认框")
                return
            self._run_batch(self._batch_no + 1, self._batch_req)
            return
        partial = str(store.get_setting("synopsis_batch_partial", "") or "")
        if partial.strip():
            self._batch_resume_dialog(store)
            return
        self._batch_ask_request()

    def _batch_resume_dialog(self, store):
        """跨会话/中断检测（对齐桌面三问框，Esc=取消）：
        接着写第N+1批 / 丢弃重新开始 / 取消。"""
        no = int(str(store.get_setting("synopsis_batch_no", "0") or "0") or 0)

        def _next(ev):
            self.page.pop_dialog()
            self._batch_resume_from_persisted()

        def _discard(ev):
            self.page.pop_dialog()
            self._batch_clear_state()
            self.refresh()
            self._batch_ask_request()

        def _cancel(ev):
            self.page.pop_dialog()
        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("检测到未完成的分批总纲"),
            content=ft.Container(ft.Text(
                f"上次分批写到第{no}批未完成，已生成部分已保存。怎么处理？\n\n"
                "（接着写会恢复已生成部分，从下一批继续，规划与创作要求一并恢复）",
                size=13), width=440),
            actions=[ft.TextButton("取消", on_click=_cancel),
                     ft.TextButton("丢弃重新开始", on_click=_discard),
                     ft.FilledButton("接着写", on_click=_next)],
            actions_alignment=ft.MainAxisAlignment.END))

    def _batch_resume_from_persisted(self):
        """跨会话/中断后接着写（对齐桌面 _batch_resume_from_persisted）：
        4 个设置键恢复累积原文与内存态，直接续跑下一批。"""
        store = self.session.store
        if store is None:
            return
        no = int(str(store.get_setting("synopsis_batch_no", "0") or "0") or 0)
        self._batch_no = no
        self._batch_req = str(store.get_setting("synopsis_batch_req", "") or "")
        self._batch_plan = str(store.get_setting("synopsis_batch_plan", "") or "")
        self._batch_partial = str(store.get_setting("synopsis_batch_partial", "") or "")
        self._batch_running = True
        self.refresh()
        self._run_batch(no + 1, self._batch_req)

    def _batch_ask_request(self):
        """收创作要求（对齐桌面 ask_long_text）→ 接着收批数。"""
        req_field = ft.TextField(label="创作要求（可选）", multiline=True, min_lines=2,
                                 max_lines=4,
                                 hint_text="题材、主角、金手指、风格、结局倾向…留空让 AI 自由发挥")

        def _ok(ev):
            req = (req_field.value or "").strip()
            self.page.pop_dialog()
            self._batch_ask_count(req)

        def _cancel(ev):
            self.page.pop_dialog()
        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("创作要求（分批生成）"),
            content=ft.Container(req_field, width=460),
            actions=[ft.TextButton("取消", on_click=_cancel),
                     ft.FilledButton("下一步", on_click=_ok)],
            actions_alignment=ft.MainAxisAlignment.END))

    def _batch_ask_count(self, req: str):
        """收批数（对齐桌面 ask）：留空 = AI 按故事体量自判（1~8 批，>8 夹 8）。"""
        count_field = ft.TextField(label="打算分几批写完？",
                                   hint_text="留空 = AI 按故事体量自判（1~8 批）")

        def _ok(ev):
            raw = (count_field.value or "").strip()
            self.page.pop_dialog()
            if raw and not raw.isdigit():
                toast(self.page, "批数请填数字（1~8），留空让 AI 自判", error=True)
                return
            batch_hint = (f"用户要求全书共分 {min(int(raw), 8)} 批，"
                          f"务必恰好 {min(int(raw), 8)} 批。" if raw
                          else "批数由你根据故事体量判断（1~8 批）。")
            self._call_batch_plan(req, batch_hint, prev_hint="")

        def _cancel(ev):
            self.page.pop_dialog()
        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("分批规划"),
            content=ft.Container(count_field, width=460),
            actions=[ft.TextButton("取消", on_click=_cancel),
                     ft.FilledButton("下一步", on_click=_ok)],
            actions_alignment=ft.MainAxisAlignment.END))

    def _call_batch_plan(self, req: str, batch_hint: str, prev_hint: str = ""):
        """发规划调用（对齐桌面 _call_batch_plan）：轻量一调出批次安排，
        结果经用户确认后才开跑。"""
        from app.core.task_templates import render as render_task
        store = self.session.store
        task = render_task("synopsis_batch_plan", store,
                           batch_hint=batch_hint, prev_hint=prev_hint)
        close_running = None

        def on_started(tk):
            nonlocal close_running
            close_running = self._running(tk, "总纲分批·批次规划…")

        def on_done(result):
            nonlocal close_running
            if close_running:
                close_running()
                close_running = None
            self._on_plan_done(req, batch_hint, prev_hint, result)

        start_generation(self.page, self.session, role="synopsis",
                         context={"user_req": req}, task=task,
                         title="总纲分批·批次规划",
                         on_done=on_done, on_started=on_started)

    def _on_plan_done(self, req: str, batch_hint: str, prev_hint: str, result):
        """规划完成（对齐桌面 _on_plan_done）：展示规划三选——按此开始 /
        重新规划（带上次规划重调）/ 放弃（未跑批无损失）。"""
        if not isinstance(result, dict) or not result.get("ok"):
            return  # 失败 toast 已在 gen 里统一弹
        plan = str(result.get("text") or "").strip()
        if not plan:
            toast(self.page, "批次规划输出为空，请重新点「分批生成」", error=True)
            return

        def _go(ev):
            self.page.pop_dialog()
            self._batch_plan = plan
            self._batch_no = 1
            self._batch_running = True
            s = self.session.store.current_synopsis()
            self._batch_snapshot = (s.get("content") or "") if s else ""
            self._batch_partial = ""
            self._batch_save_state()   # 规划随状态一并持久化（中断后可恢复）
            self.refresh()
            self._run_batch(1, req)

        def _re(ev):
            self.page.pop_dialog()
            self._call_batch_plan(req, batch_hint,
                                  prev_hint=f"上一版规划如下（用户认为不合适）：\n{plan}\n"
                                            f"请调整后重新给出。")

        def _no(ev):
            self.page.pop_dialog()
        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column([
                ft.Text("批次规划（确认后开跑）", size=15, weight=ft.FontWeight.BOLD),
                hint("这是 AI 对「全书分几批、每批写哪些卷」的安排，"
                     "每批执行时都会整体注入。"),
                ft.Container(ft.Text(plan, size=12, selectable=True),
                             bgcolor=ft.Colors.with_opacity(0.05, ft.Colors.ON_SURFACE),
                             border_radius=8, padding=8),
                ft.Row([
                    ft.TextButton("放弃", on_click=_no),
                    ft.TextButton("重新规划", on_click=_re),
                    ft.FilledButton("按此开始", on_click=_go),
                ], alignment=ft.MainAxisAlignment.END, spacing=8),
            ], spacing=8, scroll=ft.ScrollMode.AUTO),
                padding=16, height=self._dlg_h(520)),
            show_drag_handle=True))

    def _run_batch(self, batch_no: int, req: str):
        """发一批（对齐桌面 _run_batch）：模板按批号选首批/续批；注入内容=
        累积原文现值（暂停微调后的内容也生效）+ 批次规划。"""
        from app.core.task_templates import render as render_task
        store = self.session.store
        self._batch_running = True   # 防重入：续批/恢复续跑/四选续批统一在此置位
        prev = (self._batch_partial or "").strip()
        task = render_task("synopsis_batch_first" if batch_no == 1
                           else "synopsis_batch_next", store, batch_no=batch_no)
        close_running = None

        def on_started(tk):
            nonlocal close_running
            close_running = self._running(tk, f"总纲分批·第{batch_no}批…")

        def on_done(result):
            nonlocal close_running
            if close_running:
                close_running()
                close_running = None
            self._on_batch_done(store, batch_no, req, result)

        start_generation(self.page, self.session, role="synopsis",
                         context={"user_req": req, "synopsis_prev_batches": prev,
                                  "synopsis_batch_plan": self._batch_plan},
                         task=task, title=f"总纲分批·第{batch_no}批",
                         on_done=on_done, on_started=on_started)

    def _on_batch_done(self, store, batch_no: int, req: str, result):
        """批次完成（对齐桌面 _on_batch_done）：解析批末标记 → 累积原文 →
        分支：完结或达上限 → 收尾落库；未完结 → 四选确认。
        失败/取消/项目关闭走中断框。"""
        if store is None or store.is_closed:
            self._batch_interrupted(batch_no, req, "项目已关闭，本批结果未返回")
            return
        if not isinstance(result, dict) or not result.get("ok"):
            err = result.get("error", "生成失败") if isinstance(result, dict) \
                else "生成被取消"
            self._batch_interrupted(batch_no, req, err)
            return
        from app.core.pipeline import SYNOPSIS_BATCH_MAX, Pipeline
        clean, vol_no, finished = Pipeline.parse_synopsis_batch_marker(
            result.get("text") or "")
        cur = (self._batch_partial or "").rstrip()
        # 防呆①本批有效内容 <30 字疑似空转；②与已累积内容完全相同（复读）
        if len(clean) < 30 or (cur and clean == cur.strip()):
            self._batch_interrupted(batch_no, req,
                                    "本批输出过短或与已生成部分完全相同，疑似空转")
            return
        self._batch_partial = clean if not cur else cur + "\n\n" + clean
        self._batch_no = batch_no
        self._batch_save_state()   # 每批完成即持久化（中断后可跨会话续）
        self.refresh()
        if finished or batch_no >= SYNOPSIS_BATCH_MAX:
            if not finished:
                toast(self.page, f"已达 {SYNOPSIS_BATCH_MAX} 批上限，强制收尾（AI 未报完结）")
            self._batch_finish(store)
            return
        self._batch_confirm_sheet(store, batch_no, req, vol_no)

    def _batch_confirm_sheet(self, store, batch_no: int, req: str, vol_no: int):
        """每批完成四选确认（对齐桌面四问框）：继续下一批 / 就此收尾 /
        暂停微调 / 放弃全部。拖下手柄关闭 sheet = 暂停微调（桌面 Esc 同款防误触）。"""
        clicked = {"v": False}

        def _next(ev):
            clicked["v"] = True
            self.page.pop_dialog()
            self._run_batch(batch_no + 1, req)

        def _finish(ev):
            clicked["v"] = True
            self.page.pop_dialog()
            self._batch_finish(store)

        def _pause(ev):
            clicked["v"] = True
            self.page.pop_dialog()
            self._batch_running = False
            self.refresh()
            self._batch_tune_dialog()

        def _abort(ev):
            clicked["v"] = True
            self.page.pop_dialog()
            # 先落暂停态再确认：取消确认 = 留在暂停态（桌面同语义，防误触丢内容）
            self._batch_running = False
            self.refresh()
            confirm(self.page, "放弃分批总纲",
                    "已生成部分将丢弃并恢复分批前的原总纲。确定放弃？",
                    lambda: self._batch_abort(), yes_text="放弃")

        def _dismiss(e=None):
            # 拖下手柄/点外部关闭：没点过按钮才按「暂停微调」处理
            if not clicked["v"] and self._batch_running:
                self._batch_running = False
                self.refresh()
        self.page.show_dialog(ft.BottomSheet(
            on_dismiss=_dismiss,
            content=ft.Container(ft.Column([
                ft.Text(f"总纲分批·第{batch_no}批完成", size=15,
                        weight=ft.FontWeight.BOLD),
                ft.Text(f"第{batch_no}批已写到：分卷{vol_no}。"
                        "继续下一批将把已生成部分整体注入下一批提示词。",
                        size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Container(ft.Text((self._batch_partial or "")[-1200:], size=12,
                                     selectable=True),
                             bgcolor=ft.Colors.with_opacity(0.05, ft.Colors.ON_SURFACE),
                             border_radius=8, padding=8),
                ft.Row([
                    ft.TextButton("放弃全部", on_click=_abort),
                    ft.TextButton("暂停，先微调", on_click=_pause),
                    ft.FilledButton("继续下一批", on_click=_next),
                    ft.FilledTonalButton("就此收尾", on_click=_finish),
                ], alignment=ft.MainAxisAlignment.END, spacing=6, wrap=True),
            ], spacing=8, scroll=ft.ScrollMode.AUTO),
                padding=16, height=self._dlg_h(560)),
            show_drag_handle=True))

    def _batch_tune_dialog(self):
        """暂停微调（手机版「编辑器」）：把已累积原文放进可编辑框，
        保存后「继续下一批」注入的是这里的现值。取消 = 保持原样。"""
        def _save(text: str):
            if text.strip():
                self._batch_partial = text.strip()
                self._batch_save_state()
            self.refresh()
        self._text_dialog("微调已生成的总纲",
                          "总纲内容（点「继续下一批」时以此内容整体注入下一批）",
                          self._batch_partial, on_ok=_save, ok_text="保存微调")

    def _batch_finish(self, store):
        """收尾（对齐桌面 _batch_finish）：清分批状态后走候选识别+选择落库链路
        （与一次生成共用 _pick_candidate，同一落库口径）。"""
        from app.core.pipeline import Pipeline
        full = (self._batch_partial or "").strip()
        self._batch_clear_state()
        self.refresh()
        if not full:
            toast(self.page, "总纲分批：已生成部分为空，无法收尾", error=True)
            return
        cands, err = Pipeline.parse_synopsis_candidates(full)
        if err or not cands:
            toast(self.page, f"总纲候选解析失败：{err or '结果为空'}", error=True)
            return
        self._pick_candidate(cands)

    def _batch_abort(self):
        """放弃全部（对齐桌面 _batch_abort）：恢复发起分批前的快照，清状态。"""
        self._batch_partial = self._batch_snapshot
        self._batch_clear_state()
        self.refresh()
        toast(self.page, "已放弃分批，已恢复分批前的原总纲")

    def _batch_interrupted(self, batch_no: int, req: str, reason: str):
        """批次链中断（失败/取消/项目关闭/防呆）——有已生成内容时问「保留/恢复」。

        「保留」= 暂停态+持久化，按钮变「继续下一批（第N+1批）」点击即续，
        重启后也可从设置键恢复；「恢复」= 恢复分批前快照并清状态。
        """
        if not (self._batch_partial or "").strip():
            self._batch_abort()
            return

        def _keep(ev):
            self.page.pop_dialog()
            self._batch_running = False
            self._batch_save_state()
            self.refresh()
            toast(self.page, "已保留并暂停：点「继续下一批」接着写（重启后也可续）")

        def _restore(ev):
            self.page.pop_dialog()
            self._batch_abort()
        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("总纲分批中断"),
            content=ft.Container(ft.Text(f"{reason}\n\n已生成部分仍保留着。怎么处理？",
                                         size=13), width=440),
            actions=[ft.TextButton("恢复原总纲", on_click=_restore),
                     ft.FilledButton("保留已生成部分", on_click=_keep)],
            actions_alignment=ft.MainAxisAlignment.END))

    def _batch_save_state(self):
        """分批链状态持久化到项目设置键（对齐桌面 _batch_save_state）：每批完成/
        暂停/规划确认时调用——重启或切页回来点「分批生成」仍可接着写。"""
        store = self.session.store
        if store is None:
            return
        store.set_setting("synopsis_batch_partial", self._batch_partial or "")
        store.set_setting("synopsis_batch_no", str(self._batch_no))
        store.set_setting("synopsis_batch_req", self._batch_req or "")
        store.set_setting("synopsis_batch_plan", self._batch_plan or "")

    def _batch_clear_state(self):
        """清 4 个分批持久化键并复位状态机（完结落库/放弃恢复/丢弃重开时）。"""
        store = self.session.store
        if store is not None:
            for k in ("synopsis_batch_partial", "synopsis_batch_no",
                      "synopsis_batch_req", "synopsis_batch_plan"):
                store.set_setting(k, "")
        self._batch_no = 0
        self._batch_running = False
        self._batch_plan = ""

    def _pick_candidate(self, cands: list, iter_base: str | None = None,
                        iter_feedback: str | None = None):
        """候选选择 sheet → 增量合成/完整性校验 → 落库。

        iter_base（对齐桌面 2026-11-06 迭代增量化 / 2026-11-09 防丢稿）：迭代链路
        传入迭代前的底稿原文——应用候选时先增量合成进底稿，不再把候选整体当
        新总纲保存。生成/手动粘贴链路不传（全量候选直接落地）。
        """
        content_holder = {"text": cands[0].get("content", ""),
                          "title": cands[0].get("title", "")}
        preview = ft.TextField(value=content_holder["text"], multiline=True,
                               min_lines=10, max_lines=18, read_only=False,
                               border_color=ft.Colors.TRANSPARENT, text_size=12)
        radio_group = ft.RadioGroup(
            value="0",
            content=ft.Column([
                ft.Radio(value=str(i), label=f"候选{i + 1}：{c.get('title', '')}")
                for i, c in enumerate(cands)
            ], scroll=ft.ScrollMode.AUTO, height=120 if len(cands) > 2 else None))

        def _radio_changed(ev):
            i = int(radio_group.value or 0)
            if 0 <= i < len(cands):
                content_holder["text"] = cands[i].get("content", "")
                content_holder["title"] = cands[i].get("title", "")
                preview.value = content_holder["text"]
                self.page.update()

        radio_group.on_change = _radio_changed

        def _lock(ev):
            self.page.pop_dialog()
            self._apply_synopsis_candidate(preview.value.strip(),
                                           content_holder["title"],
                                           iter_base=iter_base,
                                           iter_feedback=iter_feedback, lock=True)

        def _save_only(ev):
            self.page.pop_dialog()
            self._apply_synopsis_candidate(preview.value.strip(),
                                           content_holder["title"],
                                           iter_base=iter_base,
                                           iter_feedback=iter_feedback, lock=False)

        head_text = (f"共 {len(cands)} 个候选：选择一版查看/调整，然后锁定"
                     if len(cands) > 1 else "已生成总纲：查看/调整后锁定")
        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column([
                ft.Text(head_text, size=15, weight=ft.FontWeight.BOLD),
                radio_group,
                ft.Container(preview, height=300),
                ft.Row([
                    ft.TextButton("仅保存为草稿", on_click=_save_only),
                    ft.FilledButton("锁定并导入分卷", on_click=_lock),
                ], alignment=ft.MainAxisAlignment.END, spacing=8),
            ], spacing=8), padding=16, height=560),
            show_drag_handle=True))

    def _syn_iterate(self, e):
        if self._batch_no > 0:
            toast(self.page, "总纲分批进行中——收尾或放弃后再迭代总纲", error=True)
            return
        store = self.session.store
        s = store.current_synopsis()
        content = (s.get("content") or "").strip()
        if not content:
            toast(self.page, "请先保存一版总纲再迭代", error=True)
            return
        title = s.get("title") or ""
        fb_field = ft.TextField(label="修改意见", multiline=True, min_lines=2, max_lines=4,
                                hint_text="方向、增删内容、换文风、改结局…")

        def _ok(ev):
            fb = (fb_field.value or "").strip()
            self.page.pop_dialog()
            self._do_syn_iterate(title, content, fb)

        def _cancel(ev):
            self.page.pop_dialog()
        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("AI 迭代总纲"),
            content=ft.Container(fb_field, width=460),
            actions=[ft.TextButton("取消", on_click=_cancel),
                     ft.FilledButton("生成修订版", on_click=_ok)],
            actions_alignment=ft.MainAxisAlignment.END,
        ))

    def _do_syn_iterate(self, title: str, content: str, fb: str):
        from app.core.pipeline import Pipeline
        from app.core.task_templates import render as render_task, single_plan_task
        store = self.session.store
        current = f"标题：{title}\n{content}"
        # 默认只出 1 个修订方案（2026-11-11 与桌面同口径）：单版改得更到位，
        # 不满意直接重新生成；n=1 时做单方案化，压住角色提示词里的「每个候选」。
        task = render_task("synopsis_iter", store, n=1, current=current, feedback=fb)
        task = single_plan_task(task, heading="# 方案一：方案标题", noun="修订方案",
                                detail="标题下分【迭代评估】【修订内容】两段写，")
        close_running = None

        def on_started(tk):
            nonlocal close_running
            close_running = self._running(tk, "AI 迭代总纲…")

        def on_done(result):
            if close_running:
                close_running()
            if not result.get("ok"):
                return
            cands, err = Pipeline.parse_synopsis_candidates(result.get("text") or "")
            if err or not cands:
                toast(self.page, f"修订候选解析失败：{err or '结果为空'}", error=True)
                return
            # 传底稿+修改意见：应用候选时增量合成，未提及的卷逐字不动
            # （对齐桌面 2026-11-09 防丢稿修复，勿改回整体保存）
            self._pick_candidate(cands, iter_base=content, iter_feedback=fb)

        start_generation(self.page, self.session, role="synopsis",
                         context={"user_req": f"当前总纲：\n{current}\n\n修改意见：\n{fb}"},
                         task=task, title="迭代总纲", on_done=on_done,
                         on_started=on_started)

    def _apply_synopsis_candidate(self, content: str, title: str,
                                  iter_base: str | None = None,
                                  iter_feedback: str | None = None,
                                  lock: bool = False) -> bool:
        """总纲候选落地（UI 解耦，供测试复用）：增量合成/完整性校验 → 保存或锁定。

        返回 True = 已进入保存/锁定流程；False = 被拒绝或无需修订（底稿原样保留）。
        iter_base 有值时（迭代链路，对齐桌面 _apply_synopsis 同款判据）：
        - 候选带【迭代评估】标记 → apply_synopsis_iter_patch 增量合成：评估里未提及
          的卷/整体层逐字不动；"noop"（AI 判断无需修订）不保存；缺段/找不到原位
          整批拒绝并提示原因（底稿保持不变）——绝不把修订片段当全文写库
          （2026-11-09 桌面丢稿事故形态，手机本次移植消除它）。
        - 候选无【迭代评估】标记（模型没按两段式协议输出）→ 只有候选本身是完整
          总纲（looks_like_full_synopsis：整体层有内容且分卷数不少于底稿）才允许
          整份覆盖，片段一律拒绝并提示。
        草稿保存成功后按改动卷号触发切章接力（_try_relay）；锁定链路不接力
        （锁定内容有变化时会清空重建旧分卷，接力没有意义）。
        """
        from app.core.iter_review import (apply_synopsis_iter_patch,
                                          looks_like_full_synopsis, parse_iter_review)
        store = self.session.store
        note = ""
        if iter_base:
            review = parse_iter_review(content)
            if review is None:
                # 模型没按两段式协议输出：只有候选本身是一份完整总纲才允许整份覆盖。
                # 片段（只剩修订小节/少数卷）绝不能当全文写库——那正是「没修改的
                # 部分全消失」的根因（桌面 2026-11-09 老板实测，勿放宽成静默降级）。
                if not looks_like_full_synopsis(content, iter_base):
                    toast(self.page, f"候选「{title}」不是完整总纲（缺整体层或多数分卷），"
                                     "模型没按迭代协议输出；已拒绝写入以免丢失未修改内容。"
                                     "请重试迭代，或把完整总纲手动粘贴进来。", error=True)
                    return False
            else:
                new, note = apply_synopsis_iter_patch(iter_base, review)
                if new is None:
                    toast(self.page, f"候选「{title}」增量合成失败，整批拒绝：{note}"
                                     "（底稿保持不变）", error=True)
                    return False
                if note == "noop":
                    toast(self.page, "AI 判断无需修订：该候选没有输出任何修改，"
                                     "当前总纲保持不变")
                    return False
                content = new
                note = f"（增量合成：{note[3:]}）"
        if lock:
            self._syn_lock(content=content, title=title)
            return True
        store.save_synopsis(content, title, lock=False)
        toast(self.page, f"已保存候选「{title}」为新版本{note}")
        self.refresh()
        # 切章接力（对齐桌面 2026-11-06）：迭代改动了哪些『分卷N』，就问要不要
        # 用同一条修改意见逐卷迭代这些卷的章节切分
        if iter_base:
            changed = self._changed_volume_numbers(iter_base, content)
            if changed:
                self._try_relay(changed, iter_feedback)
        return True

    # ---------- 切章接力（总纲迭代 → 逐卷迭代章节切分，对齐桌面 2026-11-06） ----------

    @staticmethod
    def _changed_volume_numbers(base: str, new: str) -> list[int]:
        """新旧总纲逐卷比对：标题或正文真的变过的『分卷N』序号列表。

        与桌面 _changed_volume_numbers 同一对齐口径（parse_synopsis_volumes 的
        index ↔ 库里第 N 行）。评估清单标「修改」但正文实际没变的不算——只接力
        内容确实变了的卷，不为没变的卷白花 AI 调用。
        """
        from app.core.pipeline import Pipeline

        def secs(text):
            return {int(v.get("index") or 0): v for v in
                    (Pipeline.parse_synopsis_volumes(text or "").get("volumes") or [])}
        old_secs, new_secs = secs(base), secs(new)
        changed = []
        for idx, nv in sorted(new_secs.items()):
            ov = old_secs.get(idx)
            if ov is None:
                changed.append(idx)
                continue
            if ((ov.get("title") or "").strip() != (nv.get("title") or "").strip()
                    or (ov.get("content") or "").strip() != (nv.get("content") or "").strip()):
                changed.append(idx)
        return changed

    def _try_relay(self, changed: list, iter_feedback: str | None) -> bool:
        """增量合成保存后：问用户要不要用同一条修改意见逐卷接力「迭代本卷切分」。

        changed=_changed_volume_numbers 的卷号；没切过章的卷自动跳过（日后切章时
        自然用上新总纲，无需接力）。同意后逐卷排进 _relay_queue，由 _relay_pump
        串行发起；用户拒绝或无可接力卷返回 False（全部跳过不弹框）。
        """
        store = self.session.store
        if store is None or not changed:
            return False
        all_chs = store.list_chapters()
        queue, skipped = [], 0
        for i, v in enumerate(store.list_volumes(), 1):
            if i not in changed:
                continue
            own = [c for c in all_chs if c.get("volume_id") == v["id"]]
            if not own:
                skipped += 1
                continue
            queue.append({"vol": v, "no": i, "own": own,
                          "feedback": iter_feedback})
        if not queue:
            if skipped:
                toast(self.page, "改动的分卷还没切章，无需接力（切章时自然用上新总纲）")
            return False
        names = "、".join(f"第{q['no']}卷「{(q['vol'].get('title') or '').strip()}」"
                          for q in queue)
        extra = f"\n（另有 {skipped} 卷还没切章，自动跳过）" if skipped else ""

        def _yes():
            self._relay_queue = queue
            self._relay_pump()
        confirm(self.page, "切章接力",
                f"本次总纲改了：{names}{extra}\n\n"
                "要用你刚才的修改意见，接力迭代这些卷的章节切分吗？\n"
                "（每卷一次 AI 调用，逐卷弹确认框把关；章数、章序、章名不动，"
                "只就地修订章内大纲，可随时放弃）", _yes, yes_text="开始接力")
        return True

    def _relay_advance(self):
        """当前接力卷处理完毕（写入/放弃/失败）：弹出队首并自动开下一卷。"""
        q = getattr(self, "_relay_queue", [])
        self._relay_queue = q[1:]
        if self._relay_queue:
            self._relay_pump()

    def _relay_pump(self):
        """接力队列串行推进：队首卷发起切章迭代（volume_chapters_iter 任务）。

        与桌面 _relay_pump 同语义；手机对话框非阻塞，队列推进不在结果回调里做，
        而在各卷确认闭环（写入/放弃/解析失败）里调 _relay_advance——否则上一卷
        确认框还开着，下一卷的生成确认就会叠上来。
        """
        queue = getattr(self, "_relay_queue", [])
        if not queue:
            return
        job = queue[0]
        store = self.session.store
        if store is None:
            self._relay_queue = []
            toast(self.page, "项目已关闭，切章接力中断", error=True)
            return
        from app.core.task_templates import render as render_task
        vol = job["vol"]
        n = job["no"]
        fb = job.get("feedback") or ""
        # 发起时刻重取章节快照：接力确认到真正开跑之间章节可能被用户动过；
        # 快照同时喂「现状」文本与落库定位，两处必须同源（防串章）
        job["own"] = [store.get_chapter(c["id"]) or c for c in job["own"]]
        own = job["own"]
        current = "\n\n".join(
            f"## 第{i}章：{(c.get('title') or '').strip() or '未命名'}\n"
            + (str(c.get("outline_content") or c.get("outline_draft") or "").strip()
               or "（本章无大纲）")
            for i, c in enumerate(own, 1))
        task = render_task("volume_chapters_iter", store, current=current,
                           feedback=fb, volume_no=n,
                           volume_count=len(store.list_volumes()))
        on_relay = self._on_relay_result(store, vol, own, n)
        start_generation(self.page, self.session, role="volume_chapters_iter",
                         context={"volume_id": vol["id"],
                                  "user_req": f"本卷现有章节切分：\n{current}\n\n修改意见：\n{fb}"},
                         task=task, title=f"接力·迭代切分·第{n}卷",
                         on_done=on_relay)

    def _chiter_result(self, store, vol: dict, own: list, n, on_finish=None):
        """切章迭代结果处理器工厂（接力 / 手动共用）；返回 on_done 回调。

        与桌面 VolumePage._on_chiter_result 同语义：增量优先，无评估标记降级全量；
        章数/章序/章名不动，只就地修订章内大纲（update_chapter），不删章不建章——
        走切分链的 add_chapter 会整卷重复建章，所以不复用切分写入。
        on_finish：每条路径结束后的收尾（接力 = 推进下一卷；手动 = None），
        失败 / 放弃 / 无需修订也必调，保证接力不断链。
        """
        from app.core.pipeline import Pipeline, MIN_VOLUME_CHAPTER_CHARS
        from app.core.iter_review import (is_noop_body, match_iter_chapters,
                                          parse_iter_review)
        finish = on_finish if on_finish is not None else (lambda: None)

        def cb(result: dict):
            if not result.get("ok"):
                finish()  # 失败 toast 已在 gen 里统一弹
                return
            text = result.get("text") or ""
            progressed = [c for c in own
                          if (c.get("block_count") or 0) or (c.get("done_count") or 0)]
            warn = (f"\n提醒：本卷有 {len(progressed)} 章已写细纲/正文，更新大纲后需到"
                    "写作台/大纲页重做对应章节。") if progressed else ""
            parsed = parse_iter_review(text)
            if parsed is not None:
                # ---- 增量路径：修订内容只含 AI 判定要改的章 ----
                body = (parsed.get("body") or "").strip()
                if is_noop_body(body):
                    toast(self.page, f"第{n}卷：AI 评估各章均无需修订，原大纲保持不变")
                    finish()
                    return
                rev, cerr, _diag = Pipeline.parse_volume_chapters_ex(
                    body, min_chars=MIN_VOLUME_CHAPTER_CHARS)
                if cerr or not rev:
                    toast(self.page, f"第{n}卷修订内容解析失败：{cerr or '未切出章节'}，"
                                     "该卷已跳过（可到分卷页手动迭代）", error=True)
                    finish()
                    return
                matched, unmatched = match_iter_chapters(rev, own)
                reasons = {it.get("no"): (it.get("reason") or "")
                           for it in parsed.get("items") or []
                           if it.get("kind") == "chapter"}
                matched_by_idx = {m["own_idx"]: m for m in matched}
                rows = []
                for i, ch in enumerate(own):
                    m = matched_by_idx.get(i)
                    if m:
                        rows.append({"title": ch.get("title") or "",
                                     "content": m.get("content") or "",
                                     "status": "update",
                                     "reason": reasons.get(i + 1, ""),
                                     "own_idx": i})
                    else:
                        rows.append({"title": ch.get("title") or "",
                                     "content": (str(ch.get("outline_content")
                                                     or ch.get("outline_draft") or "").strip()
                                                 or "（本章无大纲）"),
                                     "status": "keep",
                                     "reason": reasons.get(i + 1, "") or "AI 评估保留",
                                     "own_idx": i})
                for u in unmatched:
                    rows.append({"title": u.get("title") or "",
                                 "content": u.get("content") or "",
                                 "status": "unknown", "reason": "定位不到原章",
                                 "own_idx": None})
                self._chiter_confirm(vol, n, own, rows, warn, store=store,
                                     on_finish=on_finish)
                return
            # ---- 降级全量路径：模型没按两段式输出，走结构冻结校验 ----
            chapters, cerr, _diag = Pipeline.parse_volume_chapters_ex(
                text, min_chars=MIN_VOLUME_CHAPTER_CHARS)
            if cerr or not chapters:
                toast(self.page, f"第{n}卷切分解析失败：{cerr or '未切出章节'}，"
                                 "该卷已跳过（可到分卷页手动迭代）", error=True)
                finish()
                return
            if len(chapters) != len(own):
                toast(self.page, f"第{n}卷：AI 动了章节结构（原本 {len(own)} 章，"
                                 f"输出 {len(chapters)} 章）。切章迭代只修订章内内容"
                                 "（章数/章序/章名不变），该卷已跳过", error=True)
                finish()
                return
            rows = [{"title": (own[i].get("title") or "").strip()
                     or (chapters[i].get("title") or ""),
                     "content": chapters[i].get("content") or "",
                     "status": "update", "reason": "全量修订", "own_idx": i}
                    for i in range(len(own))]
            self._chiter_confirm(vol, n, own, rows, warn, store=store,
                                 on_finish=on_finish)
        return cb

    def _on_relay_result(self, store, vol: dict, own: list, n):
        """接力专用入口：结果落地后接着推进队列里的下一卷。"""
        return self._chiter_result(store, vol, own, n,
                                   on_finish=self._relay_advance)

    def _chiter_confirm(self, vol: dict, n, own: list, rows: list, warn: str = "",
                        store=None, on_finish=None):
        """切章迭代结果确认 sheet：勾选的章就地修订章内大纲，处理完调 on_finish。

        store：发起时的项目库（接力中途可能已切项目，写错库就是事故）。
        """
        store = store or self.session.store
        pairs = []
        rows_ui = []
        for r in rows:
            tag = {"update": "修订", "keep": "保留",
                   "unknown": "⚠未匹配"}.get(r.get("status"), r.get("status"))
            label = f"{tag}｜{(r.get('title') or '未命名')}（{r.get('reason') or '—'}）"
            ck = ft.Checkbox(label=label, value=r.get("status") == "update")
            pairs.append((r, ck))
            rows_ui.append(ck)

        def _finish():
            if on_finish:
                on_finish()

        def _write(ev):
            self.page.pop_dialog()
            updated = 0
            for r, ck in pairs:
                if not ck.value or r.get("status") != "update" \
                        or r.get("own_idx") is None:
                    continue
                new_content = str(r.get("content") or "").strip()
                ch = own[int(r["own_idx"])]
                if not new_content or \
                        new_content == str(ch.get("outline_content") or "").strip():
                    continue
                store.update_chapter(
                    ch["id"], {"outline_content": new_content,
                               "outline_draft": new_content})
                updated += 1
            toast(self.page, f"第{n}卷已就地修订 {updated} 章的大纲（章数、章名不变）"
                  if updated else "修订内容与现行一致，未做改动")
            self.refresh()
            _finish()

        def _cancel(ev):
            self.page.pop_dialog()
            toast(self.page, f"已放弃第{n}卷的切章修订")
            _finish()
        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column([
                ft.Text(f"第{n}卷 切章修订（{len(rows)} 章）", size=15,
                        weight=ft.FontWeight.BOLD),
                hint("勾选的章就地更新章内大纲：章数、章序、章名都不动，不删章不建章。"
                     + warn),
                ft.Column(rows_ui, scroll=ft.ScrollMode.AUTO, height=340),
                ft.Row([ft.TextButton("放弃本卷", on_click=_cancel),
                        ft.FilledButton("就地修订", on_click=_write)],
                       alignment=ft.MainAxisAlignment.END, spacing=8),
            ], spacing=8, scroll=ft.ScrollMode.AUTO), padding=16, height=520),
            show_drag_handle=True))

    # =====================================================
    # 页签 2：分卷
    # =====================================================

    # ---------- 分卷页：展开态 / 公共前置 ----------

    def _vol_open(self, vid) -> bool:
        """该卷是否展开（整页重建后按 _vol_open_ids 恢复）。"""
        return vid in self._vol_open_ids

    def _vol_toggle_open(self, vid, e=None):
        """记录 ExpansionTile 展开/收起：refresh() 整页重建，不记就全部收起。"""
        if e is None:
            return
        flag = getattr(e.control, "expanded", None)
        if flag is None:
            flag = str(getattr(e, "data", "")).lower() == "true"
        if flag:
            self._vol_open_ids.add(vid)
        else:
            self._vol_open_ids.discard(vid)

    def _vl_guard(self) -> bool:
        """分卷页 AI 操作公共前置：自动模式在跑就不许再发请求（双开会打架）。"""
        if self._ap_running_any():
            toast(self.page, "⚡ 自动模式正在跑全书流水线，等它停下再操作分卷", error=True)
            return False
        return True

    def _ch_rows(self, chs: list) -> list:
        """卷内章节行：整行可点 → 章名/切分初稿编辑（2026-11-17）。"""
        return [ft.Container(
            ft.Row([
                ft.Container(ft.Text(f"{c.get('order_index', '')}", size=12),
                             width=28, alignment=ft.Alignment.CENTER),
                ft.Text(c.get("title") or "未命名", size=13, expand=True),
                ft.Container(ft.Text(c.get("status", ""), size=10),
                             bgcolor=ft.Colors.with_opacity(
                                 0.14, STATUS_COLORS.get(c.get("status"), ft.Colors.GREY)),
                             border_radius=6,
                             padding=ft.Padding(left=6, right=6, top=2, bottom=2)),
            ]),
            padding=ft.Padding(left=8, right=8, top=5, bottom=5),
            on_click=lambda e, cc=dict(c): self._chapter_sheet(cc),
            ink=True, border_radius=T.R_SM,
        ) for c in chs]

    def _volumes_view(self) -> ft.Control:
        store = self.session.store
        volumes = store.list_volumes()
        chapters = store.list_chapters()

        # 卷级动作常驻页顶（空态也要有，否则新建卷没有入口）
        actions = T.button_row(
            T.tonal("＋ 新增卷", on_click=lambda e: self._volume_add()),
            T.ghost("导入总纲", on_click=lambda e: self._volume_import()),
            T.ghost("重建分段", on_click=lambda e: self._volume_rebuild()),
        )
        if not volumes and not chapters:
            return ft.Column([
                actions,
                empty("还没有分卷\n新增一卷，或用总纲锁定自动导入（也可点「导入总纲」）"),
            ], scroll=ft.ScrollMode.AUTO, expand=True, spacing=T.GAP_S)

        tiles = []
        for v in volumes:
            vchs = sorted((c for c in chapters if c.get("volume_id") == v["id"]),
                          key=lambda c: c.get("order_index") or 0)
            rows = self._ch_rows(vchs)
            if not vchs:
                rows.append(ft.Container(
                    ft.Text("本卷还没有切分章节", size=12,
                            color=ft.Colors.ON_SURFACE_VARIANT),
                    padding=10))
            ops = ft.Column([
                ft.Row([T.primary("AI 切分本卷章节", icon=ft.Icons.CALL_SPLIT,
                                  on_click=lambda e, vv=v: self._volume_split(vv),
                                  expand=True)]),
                T.button_row(
                    T.tonal("迭代本卷切分",
                            on_click=lambda e, vv=v: self._vol_iterate_chapters(vv)),
                    T.ghost("本卷操作", icon=ft.Icons.TUNE,
                            on_click=lambda e, vv=v: self._vol_ops_sheet(vv)),
                ),
            ], spacing=T.GAP_S)
            # 宽屏（平板）下按钮拉满整行会显得空旷：限宽 560 靠左（Row 包一层才
            # 能被推到左边，直接给 Container 宽度会被 ExpansionTile 的 Column 居中）
            ops_pad = ft.Padding(left=4, right=4, top=T.GAP_S, bottom=T.GAP_XS)
            if T.is_wide(self.page):
                ops_box = ft.Row([ft.Container(ops, width=560, padding=ops_pad)],
                                 alignment=ft.MainAxisAlignment.START)
            else:
                ops_box = ft.Container(ops, padding=ops_pad)
            tiles.append(ft.ExpansionTile(
                title=ft.Text(f"第{v.get('order_index')}卷 {v.get('title') or '未命名'}"
                              f"（{len(vchs)}章）", size=14,
                              weight=ft.FontWeight.W_600),
                subtitle=ft.Text((v.get("summary") or "")[:60], size=11, max_lines=1,
                                 overflow=ft.TextOverflow.ELLIPSIS)
                if v.get("summary") else None,
                maintain_state=True,
                expanded=self._vol_open(v["id"]),
                on_change=lambda e, vv=v: self._vol_toggle_open(vv["id"], e),
                controls=rows + [ops_box]))
        # 未分卷章节（同样可点开编辑）
        loose = [c for c in chapters if c.get("volume_id") is None]
        if loose:
            tiles.append(ft.ExpansionTile(
                title=ft.Text(f"未分卷（{len(loose)}章）", size=14),
                maintain_state=True,
                controls=self._ch_rows(loose)))
        return ft.Column([actions] + tiles, scroll=ft.ScrollMode.AUTO,
                         expand=True, spacing=T.GAP_S)

    # ---------- 分卷页：卷级增删移 + 总纲回写 ----------

    def _vol_after_write(self, *, pushed: dict = None, sync: dict = None):
        """分卷写库后的统一收尾：返回 (toast 尾缀, 需要重建总纲段的原因)。

        pushed = save_volume / AI 迭代后 push_volume_to_synopsis_ex 的结果；
        sync   = insert/delete/reorder 内部自动同步的结果（store.last_section_sync）。
        调用方顺序固定：写库 → 本函数 → refresh()/toast → _vol_ask_rebuild(reason)。
        绝不静默失败（对齐桌面 VolumePage._push_to_synopsis）。
        """
        res = pushed if pushed is not None else (sync or {})
        if not res:
            return "", ""
        if res.get("ok"):
            if pushed is None:
                return "；总纲分卷区已同步", ""
            tail = "（本卷原有段落缺失，已补齐）" if res.get("created_section") else ""
            return f"；已写回总纲本卷段{tail}", ""
        if res.get("reason") == "no_synopsis":
            return "", ""
        if res.get("needs_rebuild"):
            return "；总纲分卷段需重建", (res.get("reason") or "格式不识别")
        if res.get("reason") == "empty_summary":
            return "；描述为空，未写回总纲", ""
        return f"；未写回总纲（{res.get('reason') or '未知原因'}）", ""

    def _vol_ask_rebuild(self, reason: str):
        """总纲分卷段写不回去时的可见降级（重建 / 保持原样）。

        对齐桌面 _offer_section_rebuild：分卷库已保存，只提示总纲文本仍是旧的，
        由用户明确点「重建」才改写总纲分卷段（整体层原文不动）。
        """
        if not reason:
            return

        def _yes():
            res = self.session.store.sync_synopsis_volume_sections(op="rebuild")
            ok = bool(res.get("ok"))
            toast(self.page, "已按当前分卷重建总纲分卷区" if ok
                  else f"重建失败：{res.get('reason') or '未知原因'}", error=not ok)
            self.refresh()

        confirm(self.page, "总纲分卷段需要重建",
                f"总纲里的分卷段无法自动同步（{reason}）。\n\n"
                "分卷库的改动已经保存，但总纲页显示的仍是旧文本。\n\n"
                "点「重建」= 按当前分卷重写分卷段（整体层原文保留，分卷标题改成标准"
                "「### 分卷N：卷名」）；点「取消」= 保持原样，稍后在总纲页手动调整。",
                _yes, yes_text="重建")

    def _vol_write(self, vol: dict, title: str, summary: str):
        """保存卷名+描述并写回总纲本卷段。返回 (toast 文案, 需重建原因)。"""
        store = self.session.store
        saved = store.save_volume({"id": vol["id"],
                                   "title": (title or "").strip() or "未命名分卷",
                                   "summary": (summary or "").strip()})
        pushed = store.push_volume_to_synopsis_ex(vol["id"])
        suffix, reason = self._vol_after_write(pushed=pushed)
        ver = (saved or {}).get("version")
        note = f"第{vol.get('order_index')}卷已保存（卷名+描述）"
        if ver:
            note += f" · v{ver}"
        return note + suffix, reason

    def _volume_add(self):
        store = self.session.store
        name = T.field(hint="如 入局", autofocus=True)

        def _ok(ev):
            self.page.pop_dialog()
            title = (name.value or "").strip() or "未命名分卷"
            v = store.insert_volume(len(store.list_volumes()) + 1,
                                    {"title": title, "summary": ""})
            self._vol_open_ids.add(v["id"])
            suffix, reason = self._vol_after_write(sync=store.last_section_sync)
            self.refresh()
            toast(self.page, f"已新增第{v.get('order_index')}卷「{title}」{suffix}")
            self._vol_ask_rebuild(reason)

        T.sheet(self.page, "新增分卷", [
            T.field_group(name, "卷名", "留空记为「未命名分卷」，随后在「本卷操作」里改"),
        ], actions=T.action_row(self.page, T.primary("新增", on_click=_ok)))

    def _volume_import(self):
        """从当前总纲幂等导入分卷（不删已有卷、不动章节归属）。"""
        from app.core.pipeline import Pipeline
        store = self.session.store
        s = store.current_synopsis()
        content = (s.get("content") if s else "") or ""
        if not content.strip():
            toast(self.page, "还没有总纲：先在总纲页生成并保存", error=True)
            return
        parsed = Pipeline.parse_synopsis_volumes(content)
        vols = (parsed or {}).get("volumes") or []
        if not vols:
            toast(self.page, "总纲里没识别到「### 分卷」标记，无法导入（可用「＋ 新增卷」）",
                  error=True)
            return

        def _yes():
            imported = store.sync_volumes_from_synopsis(vols)
            self.refresh()
            toast(self.page, f"已从总纲导入 {len(imported)} 卷"
                             "（已有卷按总纲更新，旧版进「版本历史」）")

        confirm(self.page, "从总纲导入分卷",
                f"总纲里识别到 {len(vols)} 卷。导入是幂等的：已有卷按总纲内容覆盖"
                "（旧版自动进「版本历史」），缺少的卷追加；不删卷、不动章节归属。",
                _yes, yes_text="导入")

    def _volume_rebuild(self):
        """手动出口：按当前分卷库重写总纲分卷段（总纲段错位时用）。"""
        store = self.session.store
        s = store.current_synopsis()
        if not ((s or {}).get("content") or "").strip():
            toast(self.page, "还没有总纲，无需重建", error=True)
            return

        def _yes():
            res = store.sync_synopsis_volume_sections(op="rebuild")
            ok = bool(res.get("ok"))
            self.refresh()
            toast(self.page, "已按当前分卷重建总纲分卷区" if ok
                  else f"重建失败：{res.get('reason') or '未知原因'}", error=not ok)

        confirm(self.page, "重建总纲分卷区",
                "按当前分卷库重写总纲里的分卷段：整体层原文保留，分卷标题改成标准"
                "「### 分卷N：卷名」格式，卷内容取分卷库现行。\n\n"
                "只在总纲分卷段错位、或写入总纲失败时用；正常情况不需要。",
                _yes, yes_text="重建")

    def _volume_move(self, vol: dict, delta: int):
        store = self.session.store
        ids = [v["id"] for v in store.list_volumes()]
        try:
            i = ids.index(vol["id"])
        except ValueError:
            return
        j = i + delta
        if j < 0 or j >= len(ids):
            return
        ids[i], ids[j] = ids[j], ids[i]
        store.reorder_volumes(ids)
        suffix, reason = self._vol_after_write(sync=store.last_section_sync)
        self.refresh()
        toast(self.page, f"第{vol.get('order_index')}卷已"
                         f"{'上移' if delta < 0 else '下移'}{suffix}")
        self._vol_ask_rebuild(reason)

    def _volume_delete(self, vol: dict):
        def _yes():
            store = self.session.store
            store.delete_volume(vol["id"])
            self._vol_open_ids.discard(vol["id"])
            suffix, reason = self._vol_after_write(sync=store.last_section_sync)
            self.refresh()
            toast(self.page, f"第{vol.get('order_index')}卷已删除"
                             "（章节归入未分卷）" + suffix)
            self._vol_ask_rebuild(reason)

        confirm(self.page, "删除分卷",
                f"删除「{vol.get('title') or '未命名'}」？\n"
                "其下章节会归入「未分卷」（不删章节数据），后续卷号自动回补。",
                _yes, yes_text="删除")

    # ---------- 分卷页：本卷操作面板（A 档） ----------

    def _vol_ops_sheet(self, vol: dict):
        """本卷操作：卷名/描述、AI 迭代、版本历史、卷序、删除、粘贴成章。

        单层 BottomSheet，版本历史在同一层内切视图——叠两层 BottomSheet 在安卓
        会留遮罩残影（见 docs/分卷页手机版补齐方案.md D5）。
        """
        store = self.session.store
        vols = store.list_volumes()
        cur_vol = store.get_volume(vol["id"]) or vol
        n = cur_vol.get("order_index") or 1
        idx = next((i for i, v in enumerate(vols) if v["id"] == cur_vol["id"]), 0)
        last = len(vols) - 1

        title_f = T.field(value=cur_vol.get("title") or "", hint="卷名")
        summary_f = T.field(value=cur_vol.get("summary") or "", multiline=True,
                            min_lines=6, max_lines=6,
                            hint="本卷主线与情节概要：AI 切章 / 细化大纲时锚定这一卷")
        holder = ft.Column([], spacing=T.GAP_M, tight=True)

        def _save(ev):
            self.page.pop_dialog()
            note, reason = self._vol_write(cur_vol, title_f.value or "",
                                           summary_f.value or "")
            self.refresh()
            toast(self.page, note)
            self._vol_ask_rebuild(reason)

        def _iterate(ev):
            summary = (summary_f.value or "").strip()
            if not summary:
                toast(self.page, "本卷还没有分卷描述：先写一段再迭代（描述为空 AI 无从下手）",
                      error=True)
                return
            self.page.pop_dialog()
            self._vol_iterate(cur_vol,
                              (title_f.value or "").strip() or (cur_vol.get("title") or ""),
                              summary)

        def _paste(ev):
            self.page.pop_dialog()
            self._vol_paste_chapters(cur_vol)

        def _move(delta):
            def _go(ev):
                self.page.pop_dialog()
                self._volume_move(cur_vol, delta)
            return _go

        def _delete(ev):
            self.page.pop_dialog()
            self._volume_delete(cur_vol)

        def _main():
            return [
                T.field_group(title_f, "卷名"),
                T.field_group(summary_f, "分卷描述",
                              "AI 切章 / 细化大纲 / 写正文时锚定这一卷；改完点保存"),
                T.button_row(
                    T.primary("保存卷名+描述", on_click=_save),
                    T.tonal("AI 迭代本卷", on_click=_iterate),
                ),
                T.divider(),
                T.button_row(
                    T.ghost("版本历史", on_click=lambda e: _versions()),
                    T.ghost("粘贴章节文本", on_click=_paste),
                ),
                T.button_row(
                    T.ghost("上移", on_click=_move(-1), disabled=idx == 0),
                    T.ghost("下移", on_click=_move(1), disabled=idx >= last),
                    T.ghost("删除本卷", on_click=_delete),
                ),
            ]

        def _versions():
            versions = store.list_volume_versions(cur_vol["id"])
            if not versions:
                toast(self.page, "本卷还没有历史版本（内容被改过或迭代后自动留档）")
                return
            base = versions[0]
            preview = ft.TextField(
                value=f"卷名：{base.get('title') or ''}\n\n"
                      f"分卷描述：\n{base.get('summary') or ''}",
                multiline=True, min_lines=8, max_lines=8, read_only=True,
                text_size=12, border_color=ft.Colors.TRANSPARENT)
            pick = {"v": base}
            radios = ft.RadioGroup(
                value=str(base["version"]),
                content=ft.Column([
                    ft.Radio(value=str(v["version"]),
                             label=f"v{v['version']} ｜ {(v.get('created_at') or '')[:16]} ｜ "
                                   f"{v.get('title') or '未命名'}")
                    for v in versions
                ], scroll=ft.ScrollMode.AUTO,
                    height=min(176, 40 * len(versions) + 8)))

            def _changed(ev):
                try:
                    ver = int(radios.value)
                except (TypeError, ValueError):
                    return
                v = next((x for x in versions if int(x["version"]) == ver), None)
                if not v:
                    return
                pick["v"] = v
                preview.value = (f"卷名：{v.get('title') or ''}\n\n"
                                 f"分卷描述：\n{v.get('summary') or ''}")
                self.page.update()

            radios.on_change = _changed

            def _back(ev):
                holder.controls = _main()
                self.page.update()

            def _load(ev):
                v = pick["v"]
                title_f.value = v.get("title") or ""
                summary_f.value = v.get("summary") or ""
                holder.controls = _main()
                self.page.update()
                toast(self.page, f"已载入 v{v['version']} 到编辑框，"
                                 "点「保存卷名+描述」生效")

            holder.controls = [
                ft.Row([T.ghost("← 返回", on_click=_back)]),
                T.hint_text(f"当前 v{cur_vol.get('version', 1)}"
                            f"《{cur_vol.get('title') or '未命名'}》。载入历史版本后要点"
                            "「保存」才生效，当前版会自动再存一份历史。", size=11),
                radios,
                ft.Container(preview, height=190),
                T.button_row(T.primary("载入选中版本", on_click=_load)),
            ]

        holder.controls = _main()
        T.sheet(self.page, f"第{n}卷 · {cur_vol.get('title') or '未命名'}", [holder],
                height=self._dlg_h(720),
                actions=T.action_row(self.page, None, cancel_text="关闭"))

    # ---------- 分卷页：AI 迭代本卷（描述迭代） ----------

    def _vol_iterate(self, vol: dict, title: str, summary: str):
        """AI 迭代本卷：按修改意见出 1 个修订方案（与桌面单方案化同口径）。"""
        if not self._vl_guard():
            return
        route = self.session.store.get_route("volume_iter")
        if not route or not route.get("profile_id"):
            toast(self.page, "「分卷迭代」角色还没绑定模型：到「模型」页给它选一个档案",
                  error=True)
            return
        fb_f = T.field(multiline=True, min_lines=4, max_lines=4,
                       hint="改本卷主线、增删关键情节、换冲突设计、调卷末钩子…")

        def _ok(ev):
            fb = (fb_f.value or "").strip()
            self.page.pop_dialog()
            self._do_vol_iterate(vol, title, summary, fb)

        T.sheet(self.page, "AI 迭代本卷", [
            T.hint_text("只改这一卷的卷名与描述，不动其他卷；结果先出预览，点采用才落库。",
                        size=11),
            T.field_group(fb_f, "修改意见", "不写也行——AI 会按本卷现状做一轮打磨"),
        ], actions=T.action_row(self.page, T.primary("生成修订版", on_click=_ok)))

    def _do_vol_iterate(self, vol: dict, title: str, summary: str, fb: str):
        from app.core.pipeline import Pipeline
        from app.core.task_templates import render as render_task, single_plan_task
        store = self.session.store
        current = f"卷名：{title}\n分卷描述：\n{summary}"
        n = vol.get("order_index") or 1
        task = render_task("volume_iter", store, n=1, current=current, feedback=fb,
                           volume_no=n, volume_count=len(store.list_volumes()))
        task = single_plan_task(task, heading="# 方案一：修订后卷名", noun="修订方案",
                                detail="标题下直接写修订后的本卷描述，")
        close_running = None

        def on_started(tk):
            nonlocal close_running
            close_running = self._running(tk, "AI 迭代本卷…")

        def on_done(result):
            if close_running:
                close_running()
            if not result.get("ok"):
                return
            cands, err = Pipeline.parse_volume_candidates(result.get("text") or "")
            if err or not cands:
                toast(self.page, f"修订解析失败：{err or '结果为空'}", error=True)
                return
            self._vol_iter_preview(store, vol, cands, n)

        start_generation(self.page, self.session, role="volume_iter",
                         context={"volume_id": vol["id"],
                                  "user_req": f"本卷现状：\n{current}\n\n修改意见：\n{fb}"},
                         task=task, title=f"迭代分卷·第{n}卷", on_done=on_done,
                         on_started=on_started)

    def _vol_iter_preview(self, store, vol: dict, cands: list, n: int):
        """修订结果预览（单方案，可微调）：点采用才落库为新版本（旧版进历史）。"""
        c0 = cands[0] if cands else {}
        new_title = (c0.get("title") or "").strip() or (vol.get("title") or "")
        body = ft.TextField(value=c0.get("content") or "", multiline=True,
                            min_lines=12, max_lines=12, text_size=12)

        def _apply(ev):
            self.page.pop_dialog()
            saved = store.save_volume({"id": vol["id"], "title": new_title,
                                       "summary": (body.value or "").strip()})
            pushed = store.push_volume_to_synopsis_ex(vol["id"])
            suffix, reason = self._vol_after_write(pushed=pushed)
            ver = (saved or {}).get("version")
            self.refresh()
            toast(self.page, f"已迭代第{n}卷并保存为新版本"
                             + (f"（v{ver}）" if ver else "")
                             + "，旧版在「版本历史」" + suffix)
            self._vol_ask_rebuild(reason)

        T.sheet(self.page, f"本次修订结果 · 第{n}卷", [
            T.hint_text(f"新卷名：{new_title}。确认后立即保存为新版本，旧版进「版本历史」；"
                        "预览可直接微调。", size=11),
            ft.Container(body, height=300),
        ], height=self._dlg_h(560),
            actions=T.action_row(self.page, T.primary("采用并保存", on_click=_apply)))

    # ---------- 分卷页：迭代本卷切分（手动入口，B 档） ----------

    def _chiter_current(self, own: list) -> str:
        """本卷现有章节切分快照（喂给 volume_chapters_iter 的 current）。"""
        return "\n\n".join(
            f"## 第{i}章：{(c.get('title') or '').strip() or '未命名'}\n"
            + (str(c.get("outline_content") or c.get("outline_draft") or "").strip()
               or "（本章无大纲）")
            for i, c in enumerate(own, 1))

    def _vol_iterate_chapters(self, vol: dict):
        """迭代本卷切分：本卷现有章节整体 + 修改意见 → 就地修订章内要点。

        与桌面 VolumePage._iterate_chapters 同口径（结构冻结）：章数 / 章序 / 章名
        不动，只改每章内部的 ①② 情节点；落库 = 逐章 update_chapter，不删章不建章。
        """
        if not self._vl_guard():
            return
        store = self.session.store
        own = [c for c in store.list_chapters() if c.get("volume_id") == vol["id"]]
        if not own:
            toast(self.page, "本卷还没有章节：先用「AI 切分本卷章节」切出章，再迭代",
                  error=True)
            return
        route = store.get_route("volume_chapters_iter")
        if not route or not route.get("profile_id"):
            toast(self.page, "「分卷切章迭代」角色还没绑定模型：到「模型」页给它选一个档案",
                  error=True)
            return
        fb_f = T.field(multiline=True, min_lines=4, max_lines=4,
                       hint="如：本卷节奏太赶，把第2章展开；第4章结尾钩子太弱…")

        def _ok(ev):
            fb = (fb_f.value or "").strip()
            self.page.pop_dialog()
            if not fb:
                toast(self.page, "写一句修改意见再迭代（不然 AI 不知道改什么）", error=True)
                return
            self._do_vol_iterate_chapters(store, vol, own, fb)

        T.sheet(self.page, f"迭代本卷切分 · 第{vol.get('order_index')}卷", [
            T.hint_text("章数、章序、章名不动，只改每章内部的 ①② 要点；"
                        "已写细纲 / 正文的章更新后需重做。", size=11),
            T.field_group(fb_f, "修改意见",
                          "增/减章数、合并相邻章、调情节重心、修章末钩子…"),
        ], actions=T.action_row(self.page, T.primary("开始迭代", on_click=_ok)))

    def _do_vol_iterate_chapters(self, store, vol: dict, own: list, fb: str):
        from app.core.task_templates import render as render_task
        n = vol.get("order_index") or 1
        current = self._chiter_current(own)
        task = render_task("volume_chapters_iter", store, current=current, feedback=fb,
                           volume_no=n, volume_count=len(store.list_volumes()))
        close_running = None

        def on_started(tk):
            nonlocal close_running
            close_running = self._running(tk, "迭代本卷切分…")

        def on_done(result):
            if close_running:
                close_running()
            self._chiter_apply(store, vol, own, n, result, on_finish=None)

        start_generation(self.page, self.session, role="volume_chapters_iter",
                         context={"volume_id": vol["id"],
                                  "user_req": f"本卷现有章节切分：\n{current}\n\n"
                                              f"修改意见：\n{fb}"},
                         task=task, title=f"迭代切分·第{n}卷", on_done=on_done,
                         on_started=on_started)

    # ---------- 分卷页：粘贴章节文本（不走 AI，B 档） ----------

    def _vol_paste_chapters(self, vol: dict):
        text_f = T.field(multiline=True, min_lines=12, max_lines=12,
                         hint="## 第1章：章名\n\n① 第一个情节点…\n\n## 第2章：章名")

        def _ok(ev):
            text = text_f.value or ""
            self.page.pop_dialog()
            self._vol_apply_chapters(vol, text)

        T.sheet(self.page, f"粘贴章节文本 · 第{vol.get('order_index')}卷", [
            T.hint_text("每章用「## 第N章：章名」起头（「第N章：章名」独立成行也认）；"
                        "识别后逐章勾选，确认了才写入本卷。", size=11),
            T.field_group(text_f, "章节文本", "自己写的、别处生成的都行，不走 AI"),
        ], height=self._dlg_h(640),
            actions=T.action_row(self.page, T.primary("识别", on_click=_ok)))

    def _vol_apply_chapters(self, vol: dict, text: str):
        """章节文本落地：识别 → 勾选确认 → 写入本卷（与 AI 切分共用识别口径）。"""
        from app.core.pipeline import Pipeline, MIN_VOLUME_CHAPTER_CHARS
        if not (text or "").strip():
            toast(self.page, "文本为空", error=True)
            return
        chapters, err, _diag = Pipeline.parse_volume_chapters_ex(
            text, min_chars=MIN_VOLUME_CHAPTER_CHARS)
        if err or not chapters:
            toast(self.page, f"识别失败：{err or '没切出章节'}\n"
                             "检查每章是否都有「## 第N章：章名」标题", error=True)
            return
        self._confirm_chapters(vol, chapters, title=f"识别结果：{len(chapters)} 章")

    # ---------- 分卷页：章节编辑（章名 + 切分初稿） ----------

    def _chapter_sheet(self, ch: dict):
        """章节编辑：章名 + 切分初稿（①② 序号点）。

        与桌面 _edit_card 同口径：已细化的章只改初稿（outline_content 保留细化稿），
        未细化的章两者同写（大纲页同步看到新内容）。
        """
        from app.core.chapter_outline import outline_draft_text
        store = self.session.store
        cid = ch.get("id")
        full = store.get_chapter(cid) if cid else None
        if not full:
            toast(self.page, "章节已不存在", error=True)
            return
        refined = bool(full.get("outline_refined"))
        title_f = T.field(value=full.get("title") or "", hint="章名")
        draft_f = T.field(value=outline_draft_text(full), multiline=True,
                          min_lines=14, max_lines=14,
                          hint="① 情节点…")

        def _save(ev):
            self.page.pop_dialog()
            patch = {"title": (title_f.value or "").strip() or "未命名章节",
                     "outline_draft": draft_f.value or ""}
            if not refined:
                patch["outline_content"] = draft_f.value or ""
            store.update_chapter(cid, patch)
            self.refresh()
            toast(self.page, f"第{full['order_index']}章已保存"
                             + ("（已细化，只更新切分初稿）" if refined
                                else "（大纲页同步更新）"))

        T.sheet(self.page, f"第{full.get('order_index')}章 · "
                           f"{full.get('title') or '未命名'}", [
            T.hint_text("本章已细化：保存只更新「切分初稿」，细化后的大纲不变。" if refined
                        else "本章尚未细化：保存后大纲页同步更新。", size=11),
            T.field_group(title_f, "章名"),
            T.field_group(draft_f, "切分初稿（①② 序号点）"),
        ], height=self._dlg_h(640),
            actions=T.action_row(self.page, T.primary("保存", on_click=_save)))

    def _volume_split(self, vol: dict):
        store = self.session.store
        rule, expected = chapter_rule_text(store)
        count_field = ft.TextField(
            label="本卷章节规模（留空=AI 自行判断）",
            hint_text="如 6 或 4~6",
            value=str(store.get_setting("volume_chapter_count", "") or ""))

        def _ok(ev):
            self.page.pop_dialog()
            raw = (count_field.value or "").strip()
            if raw.isdigit():
                store.set_setting("volume_chapter_mode", "exact")
                store.set_setting("volume_chapter_count", raw)
            else:
                store.set_setting("volume_chapter_mode", "auto")
            self._do_volume_split(vol)
        self.page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text(f"切分第{vol.get('order_index')}卷《{vol.get('title') or ''}》"),
            content=ft.Container(count_field, width=420),
            actions=[ft.TextButton("取消", on_click=lambda ev: self.page.pop_dialog()),
                     ft.FilledButton("开始切分", on_click=_ok)],
            actions_alignment=ft.MainAxisAlignment.END,
        ))

    def _do_volume_split(self, vol: dict):
        from app.core.pipeline import Pipeline
        from app.core.task_templates import render as render_task
        store = self.session.store
        rule, expected = chapter_rule_text(store)
        task = render_task("volume", store, chapter_rule=rule,
                           volume_no=vol.get("order_index") or 1,
                           volume_count=len(store.list_volumes()))
        close_running = None

        def on_started(tk):
            nonlocal close_running
            close_running = self._running(tk, "AI 切分章节…")

        def on_done(result):
            if close_running:
                close_running()
            if not result.get("ok"):
                return
            chapters, err = Pipeline.parse_volume_chapters(result.get("text") or "")
            if err or not chapters:
                toast(self.page, f"切章解析失败：{err or '结果为空'}", error=True)
                return
            if expected is not None and len(chapters) != expected:
                toast(self.page, f"要求 {expected} 章，实际 {len(chapters)} 章，已放弃写入",
                      error=True)
                return
            self._confirm_chapters(vol, chapters)

        start_generation(self.page, self.session, role="volume",
                         context={"volume_id": vol["id"]}, task=task,
                         title="分卷切章", on_done=on_done, on_started=on_started)

    def _confirm_chapters(self, vol: dict, chapters: list, title: str = "切分结果"):
        """切章/粘贴结果确认：勾选后写入（AI 切分与粘贴共用，title 只改标题行）。"""
        store = self.session.store
        checks = [ft.Checkbox(label=f"{c.get('title', '')}", value=True) for c in chapters]

        def _write(ev):
            picked = [c for c, ck in zip(chapters, checks) if ck.value]
            self.page.pop_dialog()
            if not picked:
                toast(self.page, "未勾选任何章节", error=True)
                return
            for c in picked:
                store.add_chapter({**c, "status": "有大纲", "volume_id": vol["id"]})
            self._vol_open_ids.add(vol["id"])
            toast(self.page, f"第{vol.get('order_index')}卷已写入 {len(picked)} 章")
            self.refresh()

        def _cancel(ev):
            self.page.pop_dialog()
        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column([
                ft.Text(f"{title}（{len(chapters)} 章，勾选要写入的）", size=15,
                        weight=ft.FontWeight.BOLD),
                ft.Column(checks, scroll=ft.ScrollMode.AUTO, height=340),
                ft.Row([ft.TextButton("取消", on_click=_cancel),
                        ft.FilledButton("写入", on_click=_write)],
                       alignment=ft.MainAxisAlignment.END),
            ], spacing=8), padding=16, height=500),
            show_drag_handle=True))

    # =====================================================
    # 页签 3：大纲
    # =====================================================

    def _outline_view(self) -> ft.Control:
        store = self.session.store
        chapters = store.list_chapters()
        if not chapters:
            return ft.Column([empty("还没有章节\n先到「分卷」页切分章节")],
                             scroll=ft.ScrollMode.AUTO, expand=True)
        ch = self._current_outline_chapter()
        controls = [
            ft.Row([
                ft.Text("单章大纲", size=17, weight=ft.FontWeight.BOLD, expand=True),
                # 上一章 / 下一章：和写作台同一套指针，翻章不用再开「换一章」列表
                self._chapter_arrows(ch) if ch else ft.Container(),
                ft.TextButton("换一章", icon=ft.Icons.SWAP_HORIZONTAL_CIRCLE,
                              on_click=lambda e: self._pick_chapter(self._pick_outline_ch)),
            ], spacing=0, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row([ft.FilledTonalButton("⚡一键细化全部大纲", icon=ft.Icons.BOLT,
                                         on_click=lambda e: self._outline_batch_start())]),
        ]
        if not ch:
            controls.append(empty("还没有章节"))
            return ft.Column(controls, scroll=ft.ScrollMode.AUTO, expand=True)
        vol = store.get_volume(ch.get("volume_id")) if ch.get("volume_id") else None
        from app.core.chapter_outline import chapter_outline_text
        outline_txt = chapter_outline_text(ch)
        refined = bool(ch.get("outline_refined"))
        controls += [
            ft.Row([
                ft.Text(f"第{ch.get('order_index')}章 {ch.get('title') or '未命名'}",
                        size=15, weight=ft.FontWeight.W_600, expand=True),
                ft.Container(ft.Text(ch.get("status", ""), size=10),
                             bgcolor=ft.Colors.with_opacity(
                                 0.14, STATUS_COLORS.get(ch.get("status"), ft.Colors.GREY)),
                             border_radius=6, padding=ft.Padding(left=6, right=6, top=2, bottom=2)),
            ]),
            ft.Text((vol.get("title") or "") if vol else "未分卷", size=11,
                    color=ft.Colors.ON_SURFACE_VARIANT),
            # 2026-11-16 美化改版：细化/编辑按钮提到大纲内容前面（原来垫在
            # 长大纲后面要滚到底才够到）
            ft.Row([
                ft.FilledButton("AI 细化本章大纲", on_click=lambda e: self._outline_refine(ch),
                                icon=ft.Icons.AUTO_AWESOME),
                ft.TextButton("手动编辑", on_click=lambda e: self._outline_edit(ch)),
            ], spacing=8, wrap=True),
            ft.Container(ft.Text(outline_txt or "（本章还没有大纲内容）", size=13,
                                 selectable=True),
                         bgcolor=ft.Colors.with_opacity(0.05, ft.Colors.ON_SURFACE),
                         border_radius=10, padding=12, margin=ft.Margin(left=0, right=0, top=8, bottom=8)),
            hint("细化只处理当前这一章：在 ①② 序号点初稿上补足场景/人物/信息推进/细节。"),
        ]
        return ft.Column(controls, scroll=ft.ScrollMode.AUTO, expand=True, spacing=2)

    def _current_outline_chapter(self):
        """大纲页当前章 —— 与写作台共用 `_current_chapter()`（2026-11-17）。"""
        return self._current_chapter()

    def _pick_outline_ch(self, ch: dict):
        self._goto_chapter(ch)

    def _outline_edit(self, ch: dict):
        store = self.session.store
        from app.core.chapter_outline import chapter_outline_text

        def _save(text: str):
            store.update_chapter(ch["id"], {"outline_content": text.strip()})
            toast(self.page, "大纲已保存")
            self.refresh()
        self._text_dialog(f"编辑大纲：第{ch.get('order_index')}章",
                          "大纲内容", chapter_outline_text(ch), on_ok=_save)

    def _outline_refine(self, ch: dict):
        from app.core.pipeline import Pipeline
        from app.core.task_templates import render as render_task
        store = self.session.store
        vol = store.get_volume(ch.get("volume_id")) if ch.get("volume_id") else None
        vol_summary = (vol.get("summary") if vol else "") or ""
        chapters_in_vol = [c for c in store.list_chapters()
                           if c.get("volume_id") == (vol["id"] if vol else None)]
        chapter_no = len([c for c in chapters_in_vol
                          if (c.get("order_index") or 0) <= (ch.get("order_index") or 0)])
        ending_note = ""
        volumes = store.list_volumes()
        if (vol and volumes and chapter_no >= len(chapters_in_vol)
                and vol["order_index"] == max((v["order_index"] for v in volumes),
                                              default=0)):
            ending_note = ("本章是全书的结局章：必须收束主线冲突、完成主要人物成长弧、"
                           "给出明确结局，不得留主线悬念。")
        task = render_task("outline_chapter", store,
                           chapter_index=ch.get("order_index", 1),
                           chapter_title=ch.get("title", "") or "未命名",
                           chapter_no=chapter_no or 1,
                           chapter_count=len(chapters_in_vol) or 1,
                           volume_context=vol_summary,
                           volume_title=(vol.get("title") if vol else "") or "未分卷",
                           ending_note=ending_note)
        close_running = None

        def on_started(tk):
            nonlocal close_running
            close_running = self._running(tk, "AI 细化大纲…")

        def on_done(result):
            if close_running:
                close_running()
            if not result.get("ok"):
                return
            data, err = Pipeline.parse_outline_chapter(
                result.get("text") or "", ch, field_defs=store.get_outline_fields())
            if err or not data:
                toast(self.page, f"大纲解析失败：{err or '结果为空'}", error=True)
                return
            if not (data.get("outline_content") or "").strip():
                toast(self.page, "细化后大纲为空", error=True)
                return
            self._confirm_outline(ch, data)

        start_generation(self.page, self.session, role="outline",
                         context={"volume_id": vol["id"] if vol else None,
                                  "chapter": ch, "refine_single": True},
                         task=task, title="细化本章大纲",
                         on_done=on_done, on_started=on_started)

    def _confirm_outline(self, ch: dict, data: dict):
        store = self.session.store

        def _write(ev):
            self.page.pop_dialog()
            store.update_chapter(ch["id"], data)
            store.set_outline_refined(ch["id"], True)
            toast(self.page, "大纲已细化写入")
            self.refresh()

        def _cancel(ev):
            self.page.pop_dialog()
        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column([
                ft.Text("细化后的本章大纲", size=15, weight=ft.FontWeight.BOLD),
                ft.Container(ft.TextField(
                    value=data.get("outline_content") or "", multiline=True,
                    min_lines=10, max_lines=18, border_color=ft.Colors.TRANSPARENT,
                    text_size=12), height=320),
                ft.Row([ft.TextButton("取消", on_click=_cancel),
                        ft.FilledButton("写入", on_click=_write)],
                       alignment=ft.MainAxisAlignment.END),
            ], spacing=8), padding=16, height=480),
            show_drag_handle=True))

    # ---------- 一键细化全部大纲（对齐桌面 main_window.start_outline_batch，2026-11-10） ----------

    def _outline_batch_plan(self) -> dict:
        """算这次「一键细化全部大纲」要做什么（只读，对齐桌面 outline_batch_plan）：
        todo=还没细化也没锁定的章（本次要做的事）；refined=已细化（跳过，同时是
        断点续跑的进度标记）；locked=大纲已锁定（跳过，视为定稿绝不覆盖）；
        empty_volumes=还没切分章节的卷（无章可细化）。"""
        plan = {"todo": [], "refined": [], "locked": [], "empty_volumes": [],
                "chapters": []}
        store = self.session.store
        if store is None:
            return plan
        chapters = store.list_chapters()
        plan["chapters"] = chapters
        for ch in chapters:
            if ch.get("outline_refined"):
                plan["refined"].append(ch)
            elif ch.get("outline_locked"):
                plan["locked"].append(ch)
            else:
                plan["todo"].append(ch)
        plan["empty_volumes"] = [v for v in store.list_volumes()
                                 if not any(c.get("volume_id") == v["id"]
                                            for c in chapters)]
        return plan

    def _outline_batch_start(self):
        """大纲页「⚡一键细化全部大纲」入口（对齐桌面 start_outline_batch）：
        只读统计 → 确认对话框（文案照搬桌面）→ 启动 scope="outline" 的 AutoPilot
        （只细化大纲、细化完即停，不进细纲/正文/归档；进度记 chapters.outline_refined）。"""
        store = self.session.store
        if store is None:
            toast(self.page, "请先打开项目", error=True)
            return
        if self._ap_running_any():
            toast(self.page, "已经有一个自动任务在跑——请先在上方进度条点「退出」，"
                             "再一键细化大纲", error=True)
            return
        s = store.current_synopsis()
        if not s or not (s.get("content") or "").strip():
            toast(self.page, "请先生成并确认总纲——细化大纲要用总纲当上下文", error=True)
            return
        plan = self._outline_batch_plan()
        if not plan["chapters"]:
            toast(self.page, "还没有章节——请先到「分卷」页切分章节，再来细化大纲",
                  error=True)
            return
        if not plan["todo"]:
            why = []
            if plan["refined"]:
                why.append(f"{len(plan['refined'])} 章已细化")
            if plan["locked"]:
                why.append(f"{len(plan['locked'])} 章大纲已锁定")
            toast(self.page, "没有需要细化的章节" + (f"（{'、'.join(why)}）" if why else ""))
            return
        lines = [
            f"按章节顺序把还没细化的 {len(plan['todo'])} 章逐一细化，细化完就停——"
            "不会接着生成细纲 / 正文 / 归档。",
            "",
            "· 结果直接写入本章大纲并标记「已细化」（旧大纲自动存快照，可回滚），"
            "不再逐章弹确认",
            "· 失败按自动模式同一节拍重试（30 秒 → 1 分 → 5 分 … 共 9 次）；"
            "中途断网会自动攒断点续写，不用从头重写",
            "· 中途可随时暂停；关掉程序后再点一次，会跳过已细化的章接着做",
            "· 细化过程逐章自动落库并刷新本页",
        ]
        skipped = []
        if plan["refined"]:
            skipped.append(f"{len(plan['refined'])} 章已细化")
        if plan["locked"]:
            skipped.append(f"{len(plan['locked'])} 章大纲已锁定")
        if skipped:
            lines.append("· 跳过：" + "、".join(skipped) + "（不覆盖已定稿的内容）")
        if plan["empty_volumes"]:
            names = "、".join(f"第{v.get('order_index')}卷《{v.get('title') or '未命名'}》"
                              for v in plan["empty_volumes"][:3])
            more = "…" if len(plan["empty_volumes"]) > 3 else ""
            lines.append(f"· 注意：{names}{more}还没切分章节，这些卷暂时无法细化"
                         "（请先到「分卷」页切章）")

        def _yes(ev):
            self.page.pop_dialog()
            self._outline_batch_confirm()
        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("⚡一键细化全部大纲"),
            content=ft.Container(ft.Column([ft.Text(t, size=12) for t in lines],
                                           tight=True, spacing=2,
                                           scroll=ft.ScrollMode.AUTO),
                                 width=440, height=self._dlg_h(380)),
            actions=[ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                     ft.FilledButton("开始细化", on_click=_yes)],
            actions_alignment=ft.MainAxisAlignment.END))

    def _outline_batch_confirm(self):
        """启动 outline 范围的自动引擎（独立实例，与全书流水线共用同一互斥判据）。"""
        from autopilot import AutoPilot
        if self._ap_running_any():
            toast(self.page, "已经有一个自动任务在跑，不能同时开第二台", error=True)
            return
        if getattr(self, "_ap_outline", None) is None:
            self._ap_outline = AutoPilot(self.page, self.session, scope="outline")
            self._ap_outline.on_state = self._ap_state
            self._ap_outline.on_step = self._ap_set_label
            self._ap_outline.on_chunk = lambda n: None  # 步骤切换才刷，字数不逐帧刷
        self._ap_retrying = ""
        self._ap_outline.start()
        self.refresh()

    # =====================================================
    # 页签 4：写作台
    # =====================================================

    def _desk_view(self) -> ft.Control:
        """写作台（2026-11-16 第三轮重做 · 平板两栏）。

        窄屏（手机）单栏，自上而下：
        ① 章节卡 `_desk_hero`；② 写法条 `_desk_settings_row`；
        ③ 内容区 `_desk_body`（弹性）；④ 操作栏 `_desk_action_bar`（不随内容滚）。

        宽屏（平板/横屏）两栏：
        左栏固定 380 —— 章节卡 + 写法条 + 大纲/批次/细纲列表（自己滚）；
        右栏 —— 正文纸占满高度 + 底部操作栏。

        为什么加两栏：手机布局直接拉到 1000dp 宽时，正文每行 50+ 字、下拉框
        被拉成 600dp、卡片一路顶到屏幕边缘。「丑」的根源不是配色，是没有宽度
        约束。两栏把「参考信息」和「正在写的正文」分开，正文落回 520~620dp
        的舒适行宽（老板 2026-11-16 平板截图点名的第一问题）。
        """
        store = self.session.store
        chapters = store.list_chapters()
        if not chapters:
            return ft.Column([T.empty("还没有章节\n先到「分卷」页切分章节",
                                      icon=ft.Icons.MENU_BOOK_OUTLINED)],
                             scroll=ft.ScrollMode.AUTO, expand=True)
        ch = self._current_desk_chapter()
        mode = store.get_prose_mode()
        banner = self._diag_banner()
        hero = self._desk_hero(ch, mode)
        settings = self._desk_settings_row(ch, mode)
        if T.is_wide(self.page):
            return self._desk_wide(ch, mode, hero, settings, banner)
        return ft.Column([
            hero,
            settings,
            *([banner] if banner else []),
            self._desk_body(ch, mode),
            self._desk_action_bar(ch, mode),
        ], expand=True, spacing=T.GAP_S)

    # ---------- 写作台：平板两栏 ----------

    def _desk_wide(self, ch: dict, mode: str, hero, settings,
                   banner) -> ft.Control:
        """平板两栏：左栏「资料」+ 右栏「正文纸」。

        左栏定宽 380 而非按比例分：按比例分时正文会随屏幕变宽一直变宽，
        又回到「一行 50 字」；定宽后正文永远落在舒适行宽。
        """
        left_items = [hero, settings] + ([banner] if banner else [])
        left_items += self._desk_side_controls(ch, mode)
        left = ft.Container(
            ft.Column(left_items, scroll=ft.ScrollMode.AUTO, spacing=T.GAP_S,
                      horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
            width=380)
        # 正文纸限宽 720：平板横屏 1180 宽时右栏本可拉到 760，一行 45 字太散，
        # 720 ≈ 每行 32 字，是中文长文阅读的舒适上限（微信读书平板同量级）。
        paper_w = min(720, max(360, T.view_width(self.page) - 380
                               - T.GAP_L - 2 * T.GAP_M))
        right = ft.Container(
            ft.Column(self._desk_editor_block(ch, whole=(mode != "batch"),
                                              reserved_px=400),
                      expand=True, spacing=T.GAP_S,
                      horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
            width=paper_w)
        # 操作栏横跨整页底部（只挂右栏时左栏下方空一片，视觉左右失衡）；
        # Row 里左栏只放资料、右栏只放纸，两边都不含操作栏。
        return ft.Column([
            ft.Row([left, right], expand=True, spacing=T.GAP_L,
                   alignment=ft.MainAxisAlignment.CENTER,
                   vertical_alignment=ft.CrossAxisAlignment.START),
            self._desk_action_bar(ch, mode),
        ], expand=True, spacing=T.GAP_S)

    def _desk_side_controls(self, ch: dict, mode: str) -> list:
        """左栏「资料」区：按写法给大纲 / 分批口径 + 批次 / 细纲块列表。"""
        if mode == "batch":
            return (self._batch_plan_controls(ch)
                    + self._batch_rows(ch) + self._batch_blocks(ch))
        if mode == "outline":
            return self._outline_plan_controls(ch) + self._batch_rows(ch)
        # 一次性：左栏放大纲全文，写作时随手能看（桌面写作台同样的左右分工）
        from app.core.chapter_outline import chapter_outline_text
        outline = (chapter_outline_text(ch) or "").strip()
        items = [T.section("本章大纲")]
        if outline:
            items.append(T.inset(ft.Text(outline, size=12, color=T.muted(),
                                         selectable=True)))
        else:
            items.append(T.hint_text("本章还没有大纲，可到「大纲」页生成。"))
        return items

    # ---------- 写作台：章节卡 ----------

    def _desk_hero(self, ch: dict, mode: str) -> ft.Control:
        """章节卡：章号 + 状态 + 标题 + 字数进度 + 大纲折叠。

        第三轮改动：标题从 18 提到 21、状态徽章挪到章号旁边（原来吊在右上角
        离标题很远），进度条从 5 提到 6 且字数说明从 10 提到 11。平板上原来的
        10px 字基本看不清。
        """
        store = self.session.store
        cid = ch["id"]
        written = len(self._accepted_prose(cid))
        try:
            target = int(ch.get("target_words") or 0)
        except (TypeError, ValueError):
            target = 0
        if not target:
            target = sum(int(b.get("target_words") or 0)
                         for b in store.list_blocks(cid)) or 3000
        ratio = max(0.0, min(1.0, written / target)) if target else 0.0
        status = ch.get("status") or "未规划"
        color = STATUS_COLORS.get(status, ft.Colors.GREY)

        from app.core.chapter_outline import chapter_outline_text
        outline = (chapter_outline_text(ch) or "").strip()
        # 大纲折叠必须走局部 update：整页重建会把编辑器里还没「接受本章」的
        # 正文刷掉（写作台最不能丢的就是这个）。
        detail = ft.Container(
            ft.Text(outline or "（本章无大纲）", size=12, color=T.muted()),
            bgcolor=T.inset_bg(), border_radius=T.R_SM, padding=T.GAP_S,
            visible=False, margin=ft.Margin(left=0, right=0, top=2, bottom=0))
        arrow = ft.Icon(ft.Icons.EXPAND_MORE, size=18, color=T.muted())

        def _toggle(e):
            detail.visible = not detail.visible
            arrow.name = (ft.Icons.EXPAND_LESS if detail.visible
                          else ft.Icons.EXPAND_MORE)
            try:
                detail.update()
                arrow.update()
            except Exception:
                pass

        # 章号旁标注全书章数：翻章时能判断「还剩多少」，不用去大纲页数
        total = len(self._chapter_list())
        head = ft.Row([
            ft.Column([
                ft.Row([
                    ft.Text(f"第 {ch.get('order_index', '?')} 章 · 共 {total} 章",
                            size=11, color=T.muted()),
                    T.pill(status, color, size=9),
                ], spacing=6),
                ft.Text(ch.get("title") or "未命名", size=21,
                        weight=ft.FontWeight.BOLD, max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS),
            ], spacing=2, expand=True),
            # ◀ 上一章 / 下一章 ▶（2026-11-17 老板要求）：与「换一章」并排，
            # 三个紧凑图标按钮占 120dp，章名仍有一半以上宽度
            self._chapter_arrows(ch),
            ft.IconButton(ft.Icons.SWAP_HORIZONTAL_CIRCLE_OUTLINED,
                          icon_size=20, tooltip="换一章",
                          visual_density=ft.VisualDensity.COMPACT,
                          on_click=lambda e: self._pick_chapter(self._pick_desk_ch)),
        ], spacing=T.GAP_S, vertical_alignment=ft.CrossAxisAlignment.CENTER)
        progress = ft.Row([
            ft.ProgressBar(value=ratio, bar_height=6, color=T.brand(),
                           bgcolor=T.line(), border_radius=3, expand=True),
            ft.Text(f"{written:,} / {target:,} 字", size=11, color=T.muted()),
        ], spacing=T.GAP_S, vertical_alignment=ft.CrossAxisAlignment.CENTER)
        outline_row = ft.Container(
            ft.Row([
                ft.Icon(ft.Icons.SUBJECT, size=15, color=T.muted()),
                ft.Text("本章大纲", size=12, color=T.muted(), expand=True),
                arrow,
            ], spacing=6),
            on_click=_toggle, ink=True, border_radius=T.R_SM,
            padding=ft.Padding(left=2, right=2, top=4, bottom=2))
        return T.card(ft.Column([head, progress, outline_row, detail],
                                spacing=T.GAP_S), padding=T.GAP_M)

    # ---------- 写作台：写法条（三段切换 + 自动归档） ----------

    def _desk_settings_row(self, ch: dict, mode: str) -> ft.Control:
        """写法切换压成三段胶囊 + 自动归档开关（一行 32dp）。

        为什么不用下拉：三个选项值得一次全露出来（写作中切写法是高频动作，
        下拉要点两下还看不见当前有哪些选项）；下拉只留给选项多的场景。
        """
        store = self.session.store
        chips = ft.Row([
            T.chip(label, active=(mode == key),
                   on_click=lambda e, k=key: self._set_mode(k))
            for key, label in (("chapter", "一次性"),
                               ("batch", "分批接力"),
                               ("outline", "按大纲"))
        ], spacing=6)

        def _toggle_auto(ev):
            store.set_setting("auto_archive", "1" if auto_sw.value else "0")
            toast(self.page, "已开启自动归档：接受正文后自动提取记忆"
                  if auto_sw.value else
                  "已关闭自动归档：写完请点「归档本章」逐条确认后归档")

        auto_sw = ft.Switch(value=self._auto_archive_on(),
                            active_color=T.brand(), on_change=_toggle_auto)
        self._desk_auto_sw = auto_sw
        return ft.Row([
            chips,
            ft.Container(expand=True),
            ft.Text("自动归档", size=11, color=T.muted()),
            auto_sw,
        ], spacing=T.GAP_XS, vertical_alignment=ft.CrossAxisAlignment.CENTER)

    def _set_mode(self, mode: str):
        """切写法（胶囊点击与下拉事件共用这一条路）。"""
        store = self.session.store
        mode = mode or "chapter"
        if mode == store.get_prose_mode():
            return
        store.set_prose_mode(mode)
        note = self._clear_desk_batches("换写法")
        names = {"batch": "分批（按细纲接力）",
                 "outline": "按大纲直写（不用细纲）",
                 "chapter": "一次性（整章写完）"}
        toast(self.page, f"已切到「{names.get(mode, mode)}」写法{note}")
        self.refresh()

    # ---------- 写作台：内容区 ----------

    def _desk_body(self, ch: dict, mode: str) -> ft.Control:
        """内容区：一次性=正文纸；分批/按大纲=口径 + 批次 + 纸 + 细纲块。

        三条路径**一律可滚**（第三轮修正）：旧版一次性模式用不滚的 Column，
        整章 5000 字时编辑器撑高超过视口，Flutter 在安卓上会裁切甚至布局异常
        ——那是「写着写着闪退」的嫌疑点之一。滚动容器的代价只是多一层，值得。
        """
        if mode == "chapter":
            # 不挂 scroll：纸自己 expand 撑满，加 scroll 反而让 Column 按内容
            # 高度排版、把底部导航顶出屏幕（scroll+expand 在 Flet 里不稳）。
            return ft.Column(
                self._chapter_desk_controls(ch), expand=True,
                spacing=T.GAP_S,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        controls = (self._batch_desk_controls(ch) if mode == "batch"
                    else self._outline_desk_controls(ch))
        return ft.Column(controls, scroll=ft.ScrollMode.AUTO, expand=True,
                         spacing=T.GAP_S,
                         horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

    # ---------- 写作台：底部操作栏（一个主操作 + 3 个次要入口） ----------

    def _minor_btn_style(self) -> ft.ButtonStyle:
        """底部栏次要按钮样式：13px + 小图标，三个并排不挤（Flet 的
        TextButton 没有 `icon_size` 参数，只能走 ButtonStyle）。"""
        return ft.ButtonStyle(
            text_style=ft.TextStyle(size=13),
            icon_size=17,
            padding=ft.Padding(left=10, right=10, top=10, bottom=10),
            shape=ft.RoundedRectangleBorder(radius=T.R_MD),
        )

    def _desk_action_bar(self, ch: dict, mode: str) -> ft.Control:
        """底部操作栏：主操作占满一行，下面三个入口均布。

        不随内容滚动——分批模式下要滚过十几张批次/细纲卡片才能到底部，
        旧版把「检查/审校/更多」挂在那里，等于没有（老板 2026-11-16 痛点）。
        第三轮：底色改成卡片白 + 顶边线（原来是页面灰，在灰底页面上糊成一片），
        主按钮与次要行之间留 6dp。
        """
        running = self._task is not None and not self._task.stopped
        if running:
            main = T.tonal("停止生成", icon=ft.Icons.STOP_CIRCLE_OUTLINED,
                           on_click=self._prose_stop, expand=True)
        else:
            main = self._desk_primary(ch, mode)
        minor = ft.Container(
            ft.Row([
                ft.TextButton("接受本章", icon=ft.Icons.CHECK_CIRCLE_OUTLINED,
                              style=self._minor_btn_style(),
                              on_click=self._prose_accept),
                ft.TextButton("归档本章", icon=ft.Icons.ARCHIVE_OUTLINED,
                              style=self._minor_btn_style(),
                              on_click=self._archive_chapter),
                ft.TextButton("更多", icon=ft.Icons.MORE_HORIZ,
                              style=self._minor_btn_style(),
                              on_click=lambda e: self._more_sheet(ch)),
            ], spacing=0, alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            # 左右各 4：SPACE_BETWEEN 会把首尾按钮顶到容器边缘，视觉上像被裁了
            padding=ft.Padding(left=4, right=4, top=0, bottom=0))
        # 按钮组限宽 900 居中：平板横屏下让主按钮横跨 1100dp 会像条横幅。
        # 减掉的是「页签内边距 PAGE_PAD ×2 + 操作栏自身 GAP_M ×2」——只减后者时
        # 这一行比可用宽度宽 24dp，右端「更多」被顶出屏幕（2026-11-17 截图实测）。
        w = T.view_width(self.page)
        inner_w = min(900, max(300, w - 2 * (PAGE_PAD + T.GAP_M)))
        return ft.Container(
            ft.Row([
                ft.Container(
                    ft.Column([main, minor], spacing=6,
                              horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
                    width=inner_w),
            ], alignment=ft.MainAxisAlignment.CENTER),
            padding=ft.Padding(left=T.GAP_M, right=T.GAP_M,
                               top=T.GAP_S, bottom=T.GAP_XS),
            bgcolor=T.card_bg(),
            border=ft.Border.only(top=ft.BorderSide(1, T.line())),
        )

    def _desk_primary(self, ch: dict, mode: str) -> ft.Control:
        """主操作按钮（随写法变化；文案与原滚动区按钮一致，测试契约不变）。"""
        store = self.session.store
        if mode == "batch":
            blocks = store.list_blocks(ch["id"])
            if not blocks:
                return T.primary(
                    "开始分批写作", icon=ft.Icons.AUTO_AWESOME, disabled=True,
                    expand=True,
                    tooltip="分批模式需要细纲：先到「大纲」页生成本章细纲")
            rows = store.list_prose_batches(ch["id"])
            value, by_count = store.get_prose_batch_plan_value()
            from app.core.pipeline import plan_batches
            total = len(plan_batches(blocks, value, by_count)) if blocks else 0
            n = len(rows)
            btn = ("合并批次写入正文" if n >= total else
                   (f"继续第 {n + 1} 批" if rows else "开始分批写作"))
            return T.primary(btn, icon=ft.Icons.AUTO_AWESOME, expand=True,
                             on_click=lambda e: self._generate_next_batch(ch))
        if mode == "outline":
            split = store.get_outline_batch_split()
            if split == "whole":
                return T.primary("生成整章正文", icon=ft.Icons.EDIT_NOTE,
                                 on_click=self._prose_generate, expand=True)
            from app.core.chapter_outline import chapter_outline_text
            outline = chapter_outline_text(ch)
            if not outline.strip():
                return T.primary(
                    "开始按大纲分批", icon=ft.Icons.AUTO_AWESOME, disabled=True,
                    expand=True, tooltip="本章还没有大纲：先到「大纲」页生成")
            rows = store.list_prose_batches(ch["id"])
            value, by_count = store.get_outline_batch_plan_value()
            from app.core.pipeline import plan_outline_batches
            plans = plan_outline_batches(outline, value, by_count)
            total = len(plans)
            n = len(rows)
            btn = ("合并批次写入正文" if n >= total else
                   (f"继续第 {n + 1} 批" if rows else "开始按大纲分批"))
            return T.primary(btn, icon=ft.Icons.AUTO_AWESOME, expand=True,
                             on_click=lambda e: self._generate_outline_batch(ch))
        return T.primary("生成整章正文", icon=ft.Icons.EDIT_NOTE,
                         on_click=self._prose_generate, expand=True)

    # ---------- 写作台：停靠条（高频操作固定可见） ----------


    def _diag_banner(self):
        """上次生成的诊断横幅。无问题时返回 None（完全不占位）。"""
        d = getattr(self, "_diag", None)
        if not d or not d.get("level"):
            return None
        color = T.danger() if d["level"] == "error" else T.warn()
        return ft.Container(
            ft.Row([
                ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, size=16, color=color),
                ft.Text(d.get("reason") or "上次生成可能有异常", size=11, expand=True,
                        color=T.text(), max_lines=2,
                        overflow=ft.TextOverflow.ELLIPSIS),
                ft.TextButton("详情", on_click=lambda e: self._show_diag()),
                ft.IconButton(ft.Icons.CLOSE, icon_size=16,
                              on_click=lambda e: self._clear_diag()),
            ], spacing=2),
            bgcolor=T.soft(color, 0.12), border_radius=T.R_SM,
            padding=ft.Padding(left=T.GAP_S, right=2, top=2, bottom=2))

    def _clear_diag(self):
        self._diag = None
        self.refresh()

    def _show_diag(self):
        d = getattr(self, "_diag", None) or {}

        def _close(ev):
            self.page.pop_dialog()
        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("上次生成的诊断"),
            content=ft.Container(ft.Column([
                ft.Text(d.get("reason") or "", size=13, weight=ft.FontWeight.W_600),
                ft.Text(d.get("message") or "", size=12),
                hint("落库前会再确认一次；确认写入后可在「更多 → 版本快照」回滚。"),
            ], tight=True, spacing=8, scroll=ft.ScrollMode.AUTO),
                width=460, height=320),
            actions=[ft.TextButton("知道了", on_click=_close)],
            actions_alignment=ft.MainAxisAlignment.END))

    def _apply_diag(self, result, role: str, text: str,
                    batch_continue: bool = False) -> str:
        """跑三态诊断：命中则记横幅 + 弹提示；返回 level（""=无异常）。

        batch_continue=True（分批续批）：结尾停在章中间是正常形态，
        豁免「未写完」末尾标点启发式（与桌面 TaskRunDialog 同口径）。
        """
        from diagnose import diagnose
        lvl, reason, msg = diagnose(result, role, text,
                                    batch_continue=batch_continue)
        if lvl:
            self._diag = {"level": lvl, "reason": reason, "message": msg}
            toast(self.page, reason, error=True)
        else:
            self._diag = None
            if msg:
                toast(self.page, msg)
        return lvl

    def _guard_incomplete(self, on_yes, yes_text="仍然落库") -> bool:
        """落库前的不完整确认（桌面「应用结果」同口径）。

        返回 True = 已直接放行（无异常）；False = 弹了确认框，等用户决定。
        """
        d = getattr(self, "_diag", None)
        if not d or not d.get("level"):
            return True
        confirm(self.page, "内容可能不完整",
                f"⚠ {d.get('reason')}。\n{(d.get('message') or '')[:200]}\n\n仍要写入？",
                on_yes, yes_text=yes_text)
        return False

    def _clear_desk_batches(self, why: str) -> str:
        """设置变更后清掉当前章未合并批次，返回 toast 追加说明（无批次返回空串）。

        批号↔细纲块/大纲节拍的对应关系由设置决定：换写法、改分批划分或改粒度后
        旧批号与新计划对不上（next_idx = 已存批数+1 会跳批/覆盖），最坏把半截
        旧稿当整章合并。已接受的整章正文不受影响（对齐桌面同款方法）。
        """
        store = self.session.store
        if not store.list_chapters():
            return ""
        ch = self._current_desk_chapter()
        rows = store.list_prose_batches(ch["id"])
        if not rows:
            return ""
        store.clear_prose_batches(ch["id"])
        return f"；已清空本章 {len(rows)} 批未合并正文（{why}后旧批次不再沿用）"

    def _on_mode_change(self, e):
        mode = (e.control.value or "chapter")
        self.session.store.set_prose_mode(mode)
        note = self._clear_desk_batches("换写法")
        names = {"batch": "分批（按细纲接力）",
                 "outline": "按大纲直写（不用细纲）",
                 "chapter": "一次性（整章写完）"}
        toast(self.page, f"已切到「{names.get(mode, mode)}」写法{note}")
        self.refresh()

    # ---------- 写作台：编辑器区（一次性 / 分批共用） ----------

    def _desk_editor_block(self, ch: dict, whole: bool,
                           reserved_px: int | None = None) -> list:
        """编辑器区：正文纸 + 一句提示。

        whole=True（一次性）：编辑器承载整章正文；
        whole=False（分批）：承载已接受正文或批次合并预览（可改写/手改后
        「接受本章」落库）。

        **纸的高度由外层 Container 的 expand 决定，不靠算行数**（第三轮修正）：
        Flet 的 TextField 不吃父级高度（只会按 min_lines 长、或按内容无限长），
        所以把「纸」做成 expand 的 Container（白底+描边+圆角），TextField 放在
        里面只负责文字区（透明底、无边框、固定 max_lines，超长在纸内滚动）。
        这样纸永远精确填满剩余高度，不会因为行数算不准把底部导航顶出屏幕
        ——「算行数」的写法在真机上就是靠运气，差一行就整页溢出。

        reserved_px=None：分批模式，纸在长列表里跟着排（不 expand，14 行）；
        给值时：文字区按视口换算行数（纸本体会 expand 撑满）。
        """
        prose = self._accepted_prose(ch["id"])
        if not whole and not prose.strip():
            prose = self.session.store.prose_batch_merged(ch["id"])
        fill = reserved_px is not None
        # 不 expand（分批模式，纸在长列表里）时给 8 行：14/10 行都会让首屏只露出
        # 半张纸、被底部操作栏硬切一刀，看着像布局崩了。分批模式的首屏重点是
        # 「批次进度 + 主按钮」，纸只是合并预览，8 行足够看开头。
        lines = 8 if not fill else T.editor_lines(self.page, reserved_px)
        # 正文 16px / 行距 1.7 / 内边距 18：平板上一行约 34 个汉字，接近纸书
        # 的舒适行宽；字号从 15 提到 16 是「盯着看不累」的下限。
        editor = ft.TextField(
            value=prose,
            hint_text=("点下方「生成整章正文」让 AI 写；也可以直接在这里手写"
                       if whole else
                       "分批的合并预览显示在这里，检查/修改后「接受本章」落库；"
                       "也可手写"),
            multiline=True,
            # min 与 max 都给 lines：只给 min_lines 时 TextField 会随内容一路
            # 长高（整章 5000 字 → 纸长到 3000px），把底部操作栏顶穿。
            min_lines=lines,
            max_lines=lines,
            text_style=ft.TextStyle(size=16, height=1.7),
            text_align=ft.TextAlign.LEFT,
            filled=True,
            fill_color=ft.Colors.TRANSPARENT,
            border=ft.InputBorder.NONE,
            content_padding=ft.Padding(left=18, right=18, top=14, bottom=14),
        )
        self._desk_editor = editor

        def _focus(e):
            # 点纸的任意空白处都把焦点给输入框（否则大纸下半截点了没反应）
            try:
                editor.focus()
            except Exception:
                pass

        paper = ft.Container(
            editor,
            bgcolor=T.paper_bg(),
            border=ft.Border.all(1, T.line()),
            border_radius=T.R_LG,
            padding=0,
            on_click=None if fill else _focus,
            expand=fill,
            # 硬裁：内容行数与纸高不完全整除时，不加这句会露出「半截行」，
            # 看着像文本被切断（实际是纸内滚动区边界）。
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )
        # 提示只留一行：长说明会把纸挤矮（原来两行 40px，手机上正好把最后
        # 一段正文顶到操作栏底下）。细节都在「更多」里。
        tip = ("改完点「接受本章」落库，旧版自动存快照" if whole else
               "检查合并预览后点「接受本章」落库")
        return [paper, T.hint_text(tip, size=10)]

    def _chapter_desk_controls(self, ch: dict) -> list:
        return self._desk_editor_block(ch, whole=True, reserved_px=560)

    # ---------- 写作台：单块工作台（分批模式下的补写/自由写作入口） ----------

    def _block_map(self, cid: int, blocks: list | None = None):
        """→ (已写正文 {scene_block_id: text_block}, 待写块列表)"""
        store = self.session.store
        blocks = blocks if blocks is not None else store.list_blocks(cid)
        written = {}
        for t in store.list_text_blocks(cid):
            if t.get("scene_block_id") and t.get("status") in ("已接受", "已锁定"):
                written[t["scene_block_id"]] = t
        todo = [b for b in blocks if b["id"] not in written]
        return written, todo

    def _block_tb(self, cid: int, blk_id: int):
        for t in self.session.store.list_text_blocks(cid):
            if t.get("scene_block_id") == blk_id:
                return t
        return None

    def _save_block(self, ch: dict, blk: dict, text: str):
        """单块正文落库：不存在则建块再接受，已存在则直接接受（旧版自动存快照）。

        注意必须走 accept_text_block 而不是 add_text_block(content=...)——
        后者建出来的块状态是「草稿」，不会被 _block_map 当成已写，
        表现为「点开还是未写」的死循环（测试抓到的真实缺陷）。
        无 UI 依赖，供测试与其它页面复用（改块落库规则只改这里）。
        """
        store = self.session.store
        cid = ch["id"]
        tb = self._block_tb(cid, blk["id"])
        if tb is None:
            tb = store.add_text_block(cid, blk["id"])
        store.accept_text_block(tb["id"], text)
        store.set_block_status(blk["id"], "已确认")
        self._diag = None

    # ---------- 写作台：分批（接力写作） ----------

    def _batch_plan_controls(self, ch: dict) -> list:
        """分批口径（按总批数 / 按每批细纲块数）+ 换算结果一行说明。

        2026-10-24 起默认按「计划总批数」控制：规定一共写几批，程序按本章
        细纲块总数自动换算每批几块（12 块分 3 批 → 每批 4 块）。
        """
        from app.core.pipeline import plan_batches
        store = self.session.store
        blocks = store.list_blocks(ch["id"])
        if not blocks:
            return [hint("分批模式需要细纲：请先到「大纲」页生成本章细纲，"
                         "或用下方「更多 → 粘贴细纲」导入。")]
        value, by_count = store.get_prose_batch_plan_value()
        batches = plan_batches(blocks, value, by_count)
        out = [ft.Row([
            T.select("分批口径", store.get_batch_split_by(),
                     [("count", "按总批数"), ("size", "按每批块数")],
                     on_select=self._on_batch_split_by_change, expand=True),
            T.select("计划总批数" if by_count else "每批块数", str(value),
                     [(str(i), str(i)) for i in range(1, 21 if by_count else 9)],
                     on_select=self._on_batch_size_change, width=104),
        ], spacing=T.GAP_S)]
        if by_count and batches:
            out.append(hint(f"本章 {len(blocks)} 个细纲块 → {len(batches)} 批，"
                            f"每批 {len(batches[0])} 个（末批可少）"))
        return out

    def _batch_rows(self, ch: dict) -> list:
        """已生成的批次卡片（工作区进度）。"""
        store = self.session.store
        return [self._batch_card(ch, r, i)
                for i, r in enumerate(store.list_prose_batches(ch["id"]), start=1)]

    def _batch_blocks(self, ch: dict) -> list:
        """细纲块列表（单块补写 / 自由写作入口）。"""
        store = self.session.store
        blocks = store.list_blocks(ch["id"])
        if not blocks:
            return []
        out = [T.section("细纲块 · 可单独补写")]
        out += [self._outline_block_card(ch, b, i)
                for i, b in enumerate(blocks, start=1)]
        return out

    def _batch_desk_controls(self, ch: dict) -> list:
        """分批模式（手机单栏）：口径 → 批次 → 正文纸 → 细纲块。

        对齐桌面 writing_desk：批次是工作区（prose_batches 表），逐批
        「生成 → 确认落批」，全部批完成后合并接受为整章正文（旧版存快照）。
        平板两栏时批次/块列表移到左栏，正文纸独占右栏（见 `_desk_side_controls`）。
        """
        store = self.session.store
        if not store.list_blocks(ch["id"]):
            return self._batch_plan_controls(ch)
        return (self._batch_plan_controls(ch)
                + self._batch_rows(ch)
                + self._desk_editor_block(ch, whole=False)
                + self._batch_blocks(ch))

    def _on_batch_split_by_change(self, e):
        split_by = str(getattr(e.control, "value", "") or "count")
        self.session.store.set_batch_split_by(split_by)
        note = self._clear_desk_batches("换分批口径")
        name = "按总批数" if split_by == "count" else "按每批数量"
        toast(self.page, f"分批口径已切为「{name}」{note}")
        self.refresh()

    def _on_batch_size_change(self, e):
        store = self.session.store
        by_count = store.get_batch_split_by() == "count"
        raw = str(getattr(e.control, "value", "") or "").strip()
        limit = 20 if by_count else 8
        value = int(raw) if raw.isdigit() and 1 <= int(raw) <= limit else 3
        store.set_prose_batch_plan_value(value)
        note = self._clear_desk_batches("改分批粒度")
        if by_count:
            toast(self.page, f"计划共 {value} 批（每批几块按本章细纲块自动换算）{note}")
        else:
            toast(self.page, f"每批 {value} 个细纲块（对下一批生效）{note}")
        self.refresh()

    def _batch_card(self, ch: dict, row: dict, idx: int) -> ft.Control:
        """批次卡片：序号圆标 + 第 N 批 · 字数 · 覆盖范围，点开只读预览。

        旧版是 `ft.Card` + 🧩 emoji + 一行灰字，几十张排下来又高又花；
        改为紧凑行式卡片，一屏能扫到更多批次，emoji 也去掉（安卓渲染不一致）。
        """
        store = self.session.store
        text = row.get("content") or ""
        try:
            nb = len(store.prose_batch_block_ids(row))
        except Exception:
            nb = 0
        # 按大纲直写的批次没有细纲块（block_ids 空数组）→ 副标题写明批次类型
        scope = (f"覆盖 {nb} 个细纲块" if nb else
                 ("按大纲分批" if store.get_prose_mode() == "outline" else "本批范围未登记"))
        return T.card(ft.Row([
            ft.Container(ft.Text(str(idx), size=12, weight=ft.FontWeight.W_700,
                                 color=T.brand()),
                         width=26, height=26, bgcolor=T.soft(T.brand(), 0.10),
                         border_radius=T.R_PILL, alignment=ft.Alignment.CENTER),
            ft.Column([
                ft.Row([
                    ft.Text(f"第 {idx} 批", size=13, weight=ft.FontWeight.W_600),
                    T.pill("已生成", ft.Colors.GREEN, size=9),
                ], spacing=6),
                ft.Text(f"{len(text):,} 字 · {scope}", size=10, color=T.muted()),
            ], spacing=1, expand=True),
            ft.Icon(ft.Icons.CHEVRON_RIGHT, size=16,
                    color=ft.Colors.with_opacity(0.5, ft.Colors.ON_SURFACE)),
        ], spacing=T.GAP_S, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding(left=T.GAP_S, right=T.GAP_S, top=8, bottom=8),
            on_click=lambda e: self._batch_sheet(ch, row, idx))

    def _batch_sheet(self, ch: dict, row: dict, idx: int):
        """批次预览：只读查看该批正文 + 「从本批重新生成」（丢弃该批及后续）。"""
        text = row.get("content") or ""

        def _regen(ev):
            self.page.pop_dialog()
            self._regen_from_batch(ch, row, idx)

        def _close(ev):
            self.page.pop_dialog()
        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column([
                ft.Row([
                    ft.Text(f"第 {idx} 批预览（{len(text)} 字）", size=15,
                            weight=ft.FontWeight.BOLD, expand=True),
                    ft.TextButton("从本批重新生成", icon=ft.Icons.REFRESH,
                                  on_click=_regen),
                ]),
                ft.Container(ft.Text(text or "（空）", size=12, selectable=True),
                             bgcolor=ft.Colors.with_opacity(0.05, ft.Colors.ON_SURFACE),
                             border_radius=8, padding=8, height=360),
                ft.Row([ft.TextButton("关闭", on_click=_close)],
                       alignment=ft.MainAxisAlignment.END),
            ], spacing=8, scroll=ft.ScrollMode.AUTO), padding=16, height=520),
            show_drag_handle=True))

    def _outline_block_card(self, ch: dict, blk: dict, idx: int) -> ft.Control:
        """细纲块卡片（施工单视角）：块号 + 状态 + 目标字数 + 一行概述。

        点开是单块工作台（AI 写本块 / 手写 / 接受）。与批次卡片同一套行式卡片，
        两串卡片混排时视觉一致，不会一深一浅。
        """
        st = blk.get("status") or "未生成"
        color = (ft.Colors.GREEN if st in ("已确认", "已锁定")
                 else ft.Colors.BLUE if st == "写作中" else ft.Colors.GREY)
        target = blk.get("target_words") or 0
        preview = (blk.get("content") or "（空细纲）").replace("\n", " ")
        return T.card(ft.Row([
            ft.Container(ft.Text(str(idx), size=12, weight=ft.FontWeight.W_700,
                                 color=T.muted()),
                         width=26, height=26, bgcolor=T.inset_bg(),
                         border_radius=T.R_SM, alignment=ft.Alignment.CENTER),
            ft.Column([
                ft.Row([
                    ft.Text(f"块 {idx}", size=13, weight=ft.FontWeight.W_600),
                    T.pill(st, color, size=9),
                    ft.Text(f"目标 {target} 字" if target else "", size=10,
                            color=T.muted(), expand=True),
                ], spacing=6),
                ft.Text(preview, size=11, max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS, color=T.muted()),
            ], spacing=1, expand=True),
            ft.Icon(ft.Icons.CHEVRON_RIGHT, size=16,
                    color=ft.Colors.with_opacity(0.5, ft.Colors.ON_SURFACE)),
        ], spacing=T.GAP_S, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding(left=T.GAP_S, right=T.GAP_S, top=8, bottom=8),
            on_click=lambda e: self._block_sheet(ch, blk, idx))

    def _block_sheet(self, ch: dict, blk: dict, idx: int):
        """单块工作台：生成 / 编辑 / 接受 / 快照（手机端一屏一对话框）。"""
        store = self.session.store
        cid = ch["id"]
        tb = self._block_tb(cid, blk["id"])
        field = ft.TextField(
            value=(tb.get("content") or "") if tb else "",
            multiline=True, min_lines=8, max_lines=16, text_size=13,
            border_color=ft.Colors.TRANSPARENT,
            hint_text="点「AI 写本块」生成，或直接手写/粘贴本块正文")
        status = ft.Text("", size=11, color=ft.Colors.ON_SURFACE_VARIANT)
        holder = {"tk": None}

        def _gen(ev):
            from app.core.task_templates import render as render_task
            task = render_task("prose", store)
            context = {"chapter": ch, "block": blk, "notes": store.list_notes(cid)}

            def on_started(tk):
                holder["tk"] = tk
                field.value = ""
                status.value = "生成中…"
                self.page.update()

            def on_text(buf: str):
                if len(buf) <= 6000:   # 同 _prose_generate：超长不再逐帧上屏
                    field.value = buf
                status.value = f"{len(buf)} 字"

            def on_done(result):
                holder["tk"] = None
                if not result.get("ok"):
                    return
                if result.get("status") == "stopped":
                    status.value = "已停止（已生成部分可手动修改）"
                    return
                from app.core.pipeline import strip_prose_preamble
                from app.core.textclean import strip_prose_residue
                new_text = strip_prose_residue(
                    strip_prose_preamble(result.get("text") or ""))
                field.value = new_text
                self._apply_diag(result, "prose", new_text)
                status.value = f"已生成（{len(new_text)} 字），点「保存并接受」落库"

            start_generation(self.page, self.session, role="prose", context=context,
                             task=task, title=f"生成正文（块{idx}）", on_done=on_done,
                             on_text=on_text, on_started=on_started, preview=False)
            self.page.update()

        def _do_accept(text: str):
            self._save_block(ch, blk, text)
            toast(self.page, f"块{idx} 已落库（{len(text)} 字）")

        def _accept(ev):
            txt = (field.value or "").strip()
            if not txt:
                toast(self.page, "本块正文为空", error=True)
                return
            def _yes():
                self.page.pop_dialog()
                _do_accept(txt)
                self.refresh()
            if self._guard_incomplete(_yes):
                self.page.pop_dialog()
                _do_accept(txt)
                self.refresh()

        def _close(ev):
            self.page.pop_dialog()

        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column([
                ft.Row([
                    ft.Text(f"块{idx} ｜ 目标 {blk.get('target_words') or 0} 字",
                            size=15, weight=ft.FontWeight.BOLD, expand=True),
                    ft.TextButton("快照", icon=ft.Icons.HISTORY,
                                  on_click=lambda e: (
                                      self.page.pop_dialog(),
                                      self._snapshot_versions(ch, tb)) if tb
                                  else toast(self.page, "本块还没有落库正文")),
                ]),
                ft.Container(ft.Text(blk.get("content") or "（空细纲）", size=11,
                                     color=ft.Colors.ON_SURFACE_VARIANT),
                             bgcolor=ft.Colors.with_opacity(0.05, ft.Colors.ON_SURFACE),
                             border_radius=8, padding=8),
                ft.Container(field, height=260),
                status,
                ft.Row([
                    ft.TextButton("AI 写本块", icon=ft.Icons.AUTO_AWESOME, on_click=_gen),
                    ft.TextButton("停止", on_click=lambda e: (holder["tk"].stop()
                                                              if holder["tk"] else None)),
                    ft.FilledButton("保存并接受", on_click=_accept,
                                    icon=ft.Icons.CHECK_CIRCLE),
                ], spacing=6, wrap=True),
                ft.Row([ft.TextButton("关闭", on_click=_close)],
                       alignment=ft.MainAxisAlignment.END),
            ], spacing=8, scroll=ft.ScrollMode.AUTO), padding=16, height=580),
            show_drag_handle=True))

    def _generate_next_batch(self, ch: dict):
        """分批生成下一批（对齐桌面 writing_desk._generate_next_batch）。

        下一批序号 = 已存批次数 + 1；批全齐则合并接受落库。
        细纲重新生成后旧批次可能引用已不存在的块 → 全部作废从头分批。
        每批生成后弹确认落批（手机端的人工把关，桌面「应用结果」同口径）。
        """
        from app.core.pipeline import plan_batches
        store = self.session.store
        cid = ch["id"]
        blocks = store.list_blocks(cid)
        if not blocks:
            toast(self.page, "分批模式需要细纲：请先「生成细纲」", error=True)
            return
        valid_ids = {b["id"] for b in blocks}
        # 两种分批共用 prose_batches：别的写法留下的批次批号与新计划错位，整章作废
        foreign = store.drop_foreign_prose_batches(cid, "batch")
        rows = store.list_prose_batches(cid)
        if foreign:
            toast(self.page, f"检测到按大纲分批留下的 {foreign} 批旧批次，已清空，"
                             "本次从第 1 批重新开始")
        elif any(any(i not in valid_ids for i in store.prose_batch_block_ids(r))
                 for r in rows):
            store.clear_prose_batches(cid)
            rows = []
            toast(self.page, "细纲已变化，旧批次已清空，本次从第 1 批重新开始")
        value, by_count = store.get_prose_batch_plan_value()
        batches = plan_batches(blocks, value, by_count)
        next_idx = len(rows) + 1
        if next_idx > len(batches):
            merged = store.prose_batch_merged(cid)
            if not merged.strip():
                toast(self.page, "还没有任何批次，无法合并", error=True)
                return
            self._accept_merged_batches(cid, merged, len(rows))
            return
        if next_idx > 1:
            def _go():
                self._run_batch_gen(ch, blocks, batches, next_idx)
            confirm(self.page, f"继续第 {next_idx} 批",
                    f"已生成 {len(rows)} 批，继续生成本章第 {next_idx}/{len(batches)} 批？",
                    _go)
            return
        self._run_batch_gen(ch, blocks, batches, next_idx)

    def _run_batch_gen(self, ch: dict, blocks: list, batches: list, next_idx: int):
        """发出第 next_idx 批的生成请求（施工单注入全章块 + 本批标注）。"""
        from app.core.pipeline import Pipeline, batch_tail_two_paras
        from app.core.assembler import Assembler, batch_block_list_text
        from app.core.task_templates import render as render_task, BATCH_FINAL_NOTE
        store = self.session.store
        cid = ch["id"]
        rows = store.list_prose_batches(cid)
        batch_blocks = batches[next_idx - 1]
        batch_ids = [b["id"] for b in batch_blocks]
        is_final = next_idx == len(batches)
        # 思考覆盖全章 → 施工单注入全部块并标注「本批/后续批次」
        context = {
            "chapter": ch,
            "character_names": ch.get("characters") or [],
            "chapter_plan": [(b, None) for b in blocks],
            "batch_block_ids": set(batch_ids),
            "notes": store.list_notes(cid),
            "batch_index": next_idx,
            "batch_total": len(batches),
        }
        common = dict(chapter_no=ch.get("order_index", "?"),
                      chapter_title=ch.get("title") or "未命名",
                      block_list=batch_block_list_text(blocks, batch_ids),
                      final_note=BATCH_FINAL_NOTE if is_final else "")
        if next_idx == 1:
            task = render_task("prose_batch_first", store, **common)
            # 重写整章时不能拿本章旧正文当「前文结尾」（与桌面同口径）；
            # 跨章首批带上一章最后一批原文（没有批次则带上一章正文结尾）
            context["previous_end_override"] = Assembler(store).prose_prev_context(cid, [])
        else:
            task = render_task("prose_batch_next", store, batch_no=next_idx, **common)
            context["batch_state"] = store.prose_batch_state_package(cid)
            context["previous_end_override"] = Assembler(store).prose_prev_context(cid, rows)
        close_running = None

        def on_started(tk):
            nonlocal close_running
            self._task = tk
            close_running = self._running(tk, f"生成第 {next_idx} 批正文…")

        def on_done(result):
            nonlocal close_running
            self._task = None
            if close_running:
                close_running()
                close_running = None
            if not result.get("ok"):
                toast(self.page, result.get("error", "生成失败"), error=True)
                return
            if result.get("status") == "stopped":
                toast(self.page, "已停止")
                return
            info: dict = {}
            prose, state, err = Pipeline.parse_batch_prose(result.get("text") or "", info)
            if err or not (prose or "").strip():
                toast(self.page, err or "本批正文为空", error=True)
                return
            if info.get("note"):
                toast(self.page, info["note"])
            # 非末批结尾停在章中间是正常形态 → 豁免「未写完」启发式（桌面同口径）
            self._apply_diag(result, "prose", prose, batch_continue=not is_final)
            self._confirm_batch(ch, next_idx, batch_ids, is_final, prose, state)

        start_generation(self.page, self.session, role="prose", context=context,
                         task=task,
                         title=f"生成分批正文（第{next_idx}/{len(batches)}批）",
                         on_done=on_done, on_started=on_started)

    def _confirm_batch(self, ch: dict, batch_no: int, batch_ids: list,
                       is_final: bool, prose: str, state: str):
        """本批结果确认（落批前的把关）：可改文本后写入 prose_batches。"""
        store = self.session.store
        field = ft.TextField(value=prose, multiline=True, min_lines=10, max_lines=18,
                             text_size=12, border_color=ft.Colors.TRANSPARENT)
        d = getattr(self, "_diag", None)
        warn = "" if not (d and d.get("level")) else \
            f"\n\n⚠ 本次生成{d.get('reason')}。仍要落批吗？"

        def _yes():
            self.page.pop_dialog()
            txt = (field.value or "").strip()
            store.add_prose_batch(ch["id"], batch_no, batch_ids, txt, state)
            self._diag = None
            if not is_final:
                toast(self.page, f"第 {batch_no} 批已生成（{len(txt)} 字），"
                                 "点主按钮继续下一批")
            else:
                merged = store.prose_batch_merged(ch["id"])
                self._accept_merged_batches(ch["id"], merged, batch_no)
            self.refresh()

        def _cancel(ev):
            self.page.pop_dialog()
        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column([
                ft.Text(f"第 {batch_no} 批生成完成（{len(prose)} 字）", size=15,
                        weight=ft.FontWeight.BOLD),
                hint("检查/修改后「写入本批」；不想要就取消（可从主按钮重新生成）。" + warn),
                ft.Container(field, height=340),
                ft.Row([ft.TextButton("取消", on_click=_cancel),
                        ft.FilledButton("写入本批", on_click=_yes,
                                        icon=ft.Icons.CHECK_CIRCLE)],
                       alignment=ft.MainAxisAlignment.END, spacing=8),
            ], spacing=8, scroll=ft.ScrollMode.AUTO), padding=16, height=560),
            show_drag_handle=True))

    def _regen_from_batch(self, ch: dict, row: dict, idx: int):
        """从第 idx 批重新生成：丢弃该批及其后的全部批次，再重跑该批。"""
        store = self.session.store

        def _yes():
            store.truncate_prose_batches(row["chapter_id"], idx)
            self.refresh()
            # 按大纲直写的批次没有细纲块，重跑要走大纲分批入口（对齐桌面）
            if (store.get_prose_mode() == "outline"
                    and store.get_outline_batch_split() != "whole"):
                self._generate_outline_batch(ch)
            else:
                self._generate_next_batch(ch)
        confirm(self.page, "从本批重新生成",
                f"会丢弃第 {idx} 批及其后的全部批次（更早的批次保留，作为衔接基础），"
                "然后重新生成本批。继续？", _yes, yes_text="重新生成")

    def _clear_batches(self, ch: dict):
        """清空本章全部分批批次（不改动已接受的整章正文）。"""
        store = self.session.store
        rows = store.list_prose_batches(ch["id"])
        if not rows:
            toast(self.page, "本章还没有分批批次")
            return

        def _yes():
            store.clear_prose_batches(ch["id"])
            self.refresh()
            toast(self.page, "已清空全部分批批次")
        confirm(self.page, "清空分批重新开始",
                f"删除本章已生成的 {len(rows)} 批正文工作区？"
                "已接受的整章正文不受影响。此操作不可撤销。", _yes, yes_text="清空")

    # ---------- 写作台：按大纲直写（2026-10-17，对齐桌面） ----------

    def _outline_plan_controls(self, ch: dict) -> list:
        """按大纲直写：分批划分 + 口径 + 换算说明 + 大纲预览。

        与细纲分批的区别：不读细纲块，切段依据是本章大纲的「…展开分卷节拍①」
        或 ①②③ 序号点；批次仍落在 prose_batches（block_ids 为空数组）。
        """
        from app.core.chapter_outline import chapter_outline_text
        from app.core.pipeline import plan_outline_batches
        store = self.session.store
        split = store.get_outline_batch_split()
        split_dd = T.select(
            "分批划分", split,
            [("whole", "整章一次写完"), ("beat", "按大纲节拍分批"),
             ("free", "AI 自由划分")],
            on_select=self._on_outline_split_change, expand=True)
        outline = (chapter_outline_text(ch) or "").strip()
        out = [split_dd]
        if split == "whole":
            if outline:
                out.append(T.inset(ft.Text(outline[:600], size=12,
                                           color=T.muted(), selectable=True)))
            return out
        if not outline:
            out.append(hint("本章还没有大纲：先到「大纲」页生成，再按大纲写正文。"))
            return out
        value, by_count = store.get_outline_batch_plan_value()
        plans = plan_outline_batches(outline, value, by_count)
        out.append(ft.Row([
            T.select("分批口径", store.get_batch_split_by(),
                     [("count", "按总批数"), ("size", "按每批节拍")],
                     on_select=self._on_outline_split_by_change, expand=True),
            T.select("计划总批数" if by_count else "每批节拍", str(value),
                     [(str(i), str(i)) for i in range(1, 21 if by_count else 9)],
                     on_select=self._on_outline_size_change, width=104),
        ], spacing=T.GAP_S))
        if by_count and plans:
            per = plans[0]["last"] - plans[0]["first"] + 1
            out.append(hint(f"本章 {plans[0]['total_beats']} 个节拍 → "
                            f"{len(plans)} 批，每批 {per} 个（末批可少）"))
        out.append(T.section("本章大纲"))
        out.append(T.inset(ft.Text(outline[:800]
                                   + ("…" if len(outline) > 800 else ""),
                                   size=12, color=T.muted(),
                                   selectable=True)))
        return out

    def _outline_desk_controls(self, ch: dict) -> list:
        """按大纲直写（手机单栏）：划分/口径 → 批次 → 正文纸。

        平板两栏时划分/口径与批次移左栏，正文纸独占右栏。
        """
        store = self.session.store
        plan = self._outline_plan_controls(ch)
        if store.get_outline_batch_split() == "whole":
            return plan + self._desk_editor_block(ch, whole=True, reserved_px=560)
        return plan + self._batch_rows(ch) + self._desk_editor_block(ch, whole=False)

    def _on_outline_split_change(self, e):
        split = (e.control.value or "beat")
        self.session.store.set_outline_batch_split(split)
        note = self._clear_desk_batches("改分批划分")
        names = {"whole": "整章一次写完", "beat": "按大纲节拍分批", "free": "AI 自由划分"}
        toast(self.page, f"分批划分已切为「{names.get(split, split)}」{note}")
        self.refresh()

    def _on_outline_split_by_change(self, e):
        split_by = str(getattr(e.control, "value", "") or "count")
        self.session.store.set_batch_split_by(split_by)
        note = self._clear_desk_batches("换分批口径")
        name = "按总批数" if split_by == "count" else "按每批数量"
        toast(self.page, f"分批口径已切为「{name}」{note}")
        self.refresh()

    def _on_outline_size_change(self, e):
        store = self.session.store
        by_count = store.get_batch_split_by() == "count"
        raw = str(getattr(e.control, "value", "") or "").strip()
        limit = 20 if by_count else 8
        value = int(raw) if raw.isdigit() and 1 <= int(raw) <= limit else 3
        store.set_outline_batch_plan_value(value)
        note = self._clear_desk_batches("改分批粒度")
        if by_count:
            toast(self.page, f"计划共 {value} 批（每批几拍按本章大纲自动换算）{note}")
        else:
            toast(self.page, f"每批 {value} 个大纲节拍（对下一批生效）{note}")
        self.refresh()

    def _generate_outline_batch(self, ch: dict):
        """按大纲分批：下一批 = 已存批次数 + 1；批全齐则合并接受落库。"""
        from app.core.chapter_outline import chapter_outline_text
        from app.core.pipeline import plan_outline_batches
        store = self.session.store
        cid = ch["id"]
        outline = chapter_outline_text(ch)
        if not outline.strip():
            toast(self.page, "本章还没有大纲：先到「大纲」页生成", error=True)
            return
        value, by_count = store.get_outline_batch_plan_value()
        plans = plan_outline_batches(outline, value, by_count)
        if not plans:
            toast(self.page, "本章大纲为空，无法分批", error=True)
            return
        # 细纲分批留下的批次（block_ids 非空）按大纲批号接续会串批 → 整章作废
        foreign = store.drop_foreign_prose_batches(cid, "outline")
        if foreign:
            toast(self.page, f"检测到细纲分批留下的 {foreign} 批旧批次，已清空，"
                             "本次从第 1 批重新开始")
        rows = store.list_prose_batches(cid)
        next_idx = len(rows) + 1
        if next_idx > len(plans):
            merged = store.prose_batch_merged(cid)
            if not merged.strip():
                toast(self.page, "还没有任何批次，无法合并", error=True)
                return
            self._accept_merged_batches(cid, merged, len(rows))
            return
        if next_idx > 1:
            def _go():
                self._run_outline_batch_gen(ch, plans, next_idx)
            confirm(self.page, f"继续第 {next_idx} 批",
                    f"已按大纲生成 {len(rows)} 批，继续写第 {next_idx}/{len(plans)} 批？",
                    _go)
            return
        self._run_outline_batch_gen(ch, plans, next_idx)

    def _run_outline_batch_gen(self, ch: dict, plans: list, next_idx: int):
        """发出第 next_idx 批的按大纲生成请求（带【本批】/【后续批次】标注）。"""
        from app.core.pipeline import (Pipeline, batch_tail_two_paras,
                                       outline_batch_scope_text)
        from app.core.assembler import Assembler
        from app.core.task_templates import (render as render_task,
                                             OUTLINE_BATCH_FINAL_NOTE)
        store = self.session.store
        cid = ch["id"]
        rows = store.list_prose_batches(cid)
        free = store.get_outline_batch_split() == "free"
        is_final = next_idx == len(plans)
        if free:
            scope_brief = (f"本章共 {len(plans)} 批，本批是第 {next_idx} 批："
                           f"推进到全章约 {next_idx}/{len(plans)} 处，"
                           "停在一个自然场景的收束点，后面的内容留给下一批。")
        else:
            cur = plans[next_idx - 1]
            scope_brief = (f"本批覆盖：{cur['label']}"
                           f"（全章 {cur['total_beats']} 个节拍中的第 {cur['first']}~{cur['last']} 个）。"
                           "只写这一段，后面的节拍留给下一批。")
        if is_final:
            scope_brief += "本批是最后一批：把大纲剩下的内容全部写完，并收好章尾。"
        context = {
            "chapter": ch,
            "character_names": ch.get("characters") or [],
            "outline_only": True,
            "outline_batch_text": outline_batch_scope_text(plans, next_idx, free=free),
            "notes": store.list_notes(cid),
            "batch_index": next_idx,
            "batch_total": len(plans),
        }
        common = dict(chapter_no=ch.get("order_index", "?"),
                      chapter_title=ch.get("title") or "未命名",
                      batch_no=next_idx,
                      batch_total=len(plans),
                      batch_scope=scope_brief,
                      final_note=OUTLINE_BATCH_FINAL_NOTE if is_final else "")
        task = render_task("prose_outline_first" if next_idx == 1 else "prose_outline_next",
                           store, **common)
        if next_idx == 1:
            context["previous_end_override"] = Assembler(store).prose_prev_context(cid, [])
        else:
            context["batch_state"] = store.prose_batch_state_package(cid)
            context["previous_end_override"] = Assembler(store).prose_prev_context(cid, rows)
        close_running = None

        def on_started(tk):
            nonlocal close_running
            self._task = tk
            close_running = self._running(tk, f"按大纲生成第 {next_idx} 批…")

        def on_done(result):
            nonlocal close_running
            self._task = None
            if close_running:
                close_running()
                close_running = None
            if not result.get("ok"):
                toast(self.page, result.get("error", "生成失败"), error=True)
                return
            if result.get("status") == "stopped":
                toast(self.page, "已停止")
                return
            info: dict = {}
            prose, state, err = Pipeline.parse_batch_prose(result.get("text") or "", info)
            if err or not (prose or "").strip():
                toast(self.page, err or "本批正文为空", error=True)
                return
            if info.get("note"):
                toast(self.page, info["note"])
            self._apply_diag(result, "prose", prose, batch_continue=not is_final)
            self._confirm_batch(ch, next_idx, [], is_final, prose, state)

        start_generation(self.page, self.session, role="prose", context=context,
                         task=task,
                         title=f"按大纲生成第{next_idx}/{len(plans)}批",
                         on_done=on_done, on_started=on_started)

    def _accept_merged_batches(self, cid: int, merged: str, n: int):
        """全部批次合并接受为整章正文（旧版自动存快照）→ 章状态推进 + 自动归档。

        对齐桌面 writing_desk._accept_chapter_prose：本章旧的分块正文转为草稿。
        """
        store = self.session.store
        demoted = sum(1 for t in store.list_text_blocks(cid)
                      if t.get("scene_block_id") and t["status"] == "已接受")
        store.accept_chapter_prose(cid, merged)
        self._diag = None
        toast(self.page, f"分批完成：{n} 批合并写入正文（{len(merged)} 字，旧版已存快照）")
        if demoted:
            toast(self.page, f"本章 {demoted} 段旧的分块正文已转为草稿（整章正文已覆盖）")
        self._after_prose_done(cid)

    def _current_desk_chapter(self):
        """写作台当前章 —— 与大纲页共用 `_current_chapter()`（2026-11-17）。"""
        return self._current_chapter()

    def _pick_desk_ch(self, ch: dict):
        self._goto_chapter(ch)

    def _accepted_prose(self, cid: int) -> str:
        blocks = self.session.store.list_text_blocks(cid)
        accepted = [b for b in blocks if b["status"] in ("已接受", "已锁定")]
        return "\n\n".join(b["content"] for b in accepted)

    def _set_editor(self, text: str):
        ed = getattr(self, "_desk_editor", None)
        if ed is not None:
            ed.value = text
            try:
                self.page.update()
            except Exception:
                pass

    def _prose_stop(self, e):
        if self._task:
            self._task.stop()
            toast(self.page, "正在停止…")

    def _prose_generate(self, e):
        from app.core.pipeline import Pipeline
        from app.core.task_templates import render as render_task
        import crashguard
        store = self.session.store
        ch = self._current_desk_chapter()
        # 留痕：整章生成是「长文本 + 流式 + 大模型」的高风险路径，
        # 闪退后能从 crash.log 看出当时在哪一章、有没有走到收尾。
        crashguard.mark(f"整章正文生成开始 第{ch.get('order_index')}章 "
                        f"mode={store.get_prose_mode()}")
        cid = ch["id"]
        task = render_task("prose_chapter_md", store,
                           chapter_index=ch.get("order_index", "?"),
                           chapter_title=ch.get("title") or "未命名")
        context = {
            "chapter": ch,
            "character_names": ch.get("characters") or [],
            "whole_chapter": True,
            "notes": store.list_notes(cid),
            "previous_end_override": Assembler(store).prev_chapter_end(cid),
        }
        # 按大纲直写：组装器跳过细纲，只用本章大纲（对齐桌面，2026-10-17）
        if store.get_prose_mode() == "outline":
            context["outline_only"] = True
        self._task = None
        prose_holder = {"text": ""}

        def on_started(tk):
            self._task = tk
            self._set_editor("")

        def on_text(buffer: str):
            prose_holder["text"] = buffer
            ed = getattr(self, "_desk_editor", None)
            # 流式上屏只跟到 6000 字：每来一段都要把整篇 value 重新序列化发给
            # 客户端（O(n²) 数据量），安卓上整章 1 万字时内存和重排都吃不消。
            # 超过这个量只在下面的收尾里一次性落纸，用户仍能看到「已生成 N 字」。
            if ed is not None and len(buffer) <= 6000:
                ed.value = buffer

        def on_done(result):
            self._task = None
            crashguard.mark(f"整章正文生成结束 ok={result.get('ok')} "
                            f"chars={len(prose_holder['text'])}")
            if not result.get("ok"):
                if not prose_holder["text"]:
                    self._set_editor(self._accepted_prose(cid))
                return
            if result.get("status") == "stopped":
                toast(self.page, "已停止（保留已生成部分，可手动修改或重新生成）")
                return
            info: dict = {}
            _title, prose, err = Pipeline.parse_chapter_prose(
                result.get("text") or "", info)
            if err or not (prose or "").strip():
                toast(self.page, f"正文解析失败：{err or '整章为空'}", error=True)
                return
            if info.get("note"):
                toast(self.page, info["note"])
            self._set_editor(prose.strip())
            # 三态诊断（拒答 / 截断 / 未写完）——落库前会在「接受本章」再确认一次
            self._apply_diag(result, "prose", prose)
            toast(self.page, f"生成完成（{len(prose)} 字）。检查后点「接受本章」落库")
            self.refresh()

        # 输出上限不按目标字数估算（与桌面一致）：思考链与正文共享 max_tokens
        # 预算，紧凑估算值（目标 3000×3.2=9600）会被思考先耗掉大半、正文必被截断；
        # 走 pipeline 的角色默认（prose 60000）。渠道不接受大值时 api.py 会自动
        # 减半降级重发，无需手动收敛。
        start_generation(self.page, self.session, role="prose", context=context,
                         task=task, title="生成整章正文", on_done=on_done,
                         on_text=on_text, on_started=on_started)

    def _prose_accept(self, e):
        store = self.session.store
        ch = self._current_desk_chapter()
        cid = ch["id"]
        ed = getattr(self, "_desk_editor", None)
        text = (ed.value if ed else "").strip()
        if not text:
            toast(self.page, "正文为空", error=True)
            return

        def _yes():
            store.accept_chapter_prose(cid, text)
            self._diag = None
            toast(self.page, "本章已接受落库（旧版自动存快照）")
            self.refresh()
            # 章状态推进 + 自动归档统一走 _after_prose_done（对齐桌面）
            self._after_prose_done(cid)
        # 落库前确认（桌面「应用结果」同口径）：本次诊断到异常时必须显式确认
        d = getattr(self, "_diag", None)
        warn = ""
        if d and d.get("level"):
            warn = f"\n\n⚠ 本次生成{d.get('reason')}。仍要落库吗？"
        confirm(self.page, "接受本章",
                f"落库第{ch.get('order_index')}章正文（{len(text)} 字）"
                + ("并归档记忆" if self._auto_archive_on() else "") + f"？{warn}",
                _yes, yes_text="仍然接受" if warn else "接受")

    # ---------- 写作台：归档（对齐桌面 _after_prose_done / 归档本章） ----------

    def _auto_archive_on(self) -> bool:
        """项目级「自动归档」开关（默认开，与桌面同键 auto_archive）。"""
        try:
            return self.session.store.get_setting_int("auto_archive", 1) == 1
        except Exception:
            return True

    def _after_prose_done(self, chapter_id: int):
        """正文落库后的收尾（对齐桌面 writing_desk._after_prose_done）。

        归档幂等：已有记忆快照的章不自动重归档——情节由细纲固定，正文改动通常
        只是措辞调整，重复提取白烧一次记忆模型。需要更新时点「归档本章」。
        例外：改写来源的接受要重归档（流程语义是「正文→优化→接受→归档」，
        停旧稿就违背了幂等的本意）。
        """
        store = self.session.store
        ch = store.get_chapter(chapter_id)
        if not ch:
            return
        was_rewrite = chapter_id in self._pending_rewrite
        self._pending_rewrite.discard(chapter_id)
        has_snapshot = bool(store.has_memory_snapshot(chapter_id))
        if ch.get("status") != "已归档":
            store.set_chapter_status(chapter_id, "已完成")
        if not self._auto_archive_on():
            toast(self.page, "本章正文已完成，建议点「归档本章」更新记忆状态卡")
        elif was_rewrite or not has_snapshot:
            self._archive_auto(chapter_id, re_archive=has_snapshot)
        else:
            toast(self.page, "本章已有记忆归档，本次正文修改不重复归档"
                             "（需要更新请点「归档本章」）")

    def _archive_auto(self, chapter_id: int, re_archive: bool = False):
        """全自动归档（无确认）——仅当「自动归档」开着且非手动路径时走这里。"""
        from app.core.memory_service import MemoryService
        store = self.session.store
        mem = MemoryService(store)

        def done(r):
            if isinstance(r, dict) and r.get("ok"):
                toast(self.page, f"记忆 v{r.get('version')} 已归档"
                      + ("（重归档）" if r.get("replaced") else ""))
            elif isinstance(r, dict) and r.get("error"):
                toast(self.page, f"归档失败：{r['error']}", error=True)
            self.refresh()
        run_bg(self.page, lambda: mem.archive_auto(chapter_id, re_archive=re_archive),
               done)

    def _archive_chapter(self, e):
        """手动归档（流式提取 → 逐条确认 → 落库），对齐桌面「归档本章」。"""
        ch = self._current_desk_chapter()
        if not ch:
            toast(self.page, "请先选择章节", error=True)
            return
        from memory_ui import archive_flow
        archive_flow(self.page, self.session, ch["id"],
                     on_done=lambda v: self.refresh())

    # =====================================================
    # 页签 5：记忆
    # =====================================================

    def _memory_view(self) -> ft.Control:
        """记忆页（实现全部在 `memory_ui.py`，与桌面 memory_page.py 同语义）。"""
        from memory_ui import memory_view
        return memory_view(self.page, self.session, self.refresh)

    # =====================================================
    # 页签 6：设定（资料库 + 锁定项）
    # =====================================================

    def _lore_view(self) -> ft.Control:
        """设定页（实现全部在 `lore_ui.py`，与桌面 lore_page.py 同语义）。"""
        from lore_ui import lore_view
        return lore_view(self.page, self.session, self.refresh)

    # =====================================================
    # 写作台：检查 / 文风审校
    # =====================================================

    def _issues_sheet(self, title: str, issues: list):
        """检查结果 bottom sheet：每条可标记误报。"""
        store = self.session.store
        content_rows = []
        for it in issues:
            row = ft.Column([
                ft.Row([
                    ft.Container(ft.Text(it.get("type", "问题"), size=10),
                                 bgcolor=ft.Colors.with_opacity(0.14, ft.Colors.ORANGE),
                                 border_radius=6,
                                 padding=ft.Padding(left=6, right=6, top=2, bottom=2)),
                    ft.Text(it.get("location", ""), size=11,
                            color=ft.Colors.ON_SURFACE_VARIANT, expand=True,
                            text_align=ft.TextAlign.END),
                ], spacing=6),
                ft.Text(it.get("quote", ""), size=12, weight=ft.FontWeight.W_600,
                        max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)
                if it.get("quote") else ft.Container(height=0),
                ft.Text(it.get("problem", ""), size=12),
                ft.Text("建议：" + it["suggestion"], size=11,
                        color=ft.Colors.ON_SURFACE_VARIANT)
                if it.get("suggestion") else ft.Container(height=0),
                ft.Row([
                    ft.TextButton("标记误报", icon=ft.Icons.BLOCK, icon_size=14,
                                  on_click=lambda e, q=it.get("quote", ""):
                                  (store.add_false_positive(q),
                                   toast(self.page, "已标记误报"),
                                   self.page.pop_dialog()))
                    if it.get("quote") else ft.Container(),
                ], alignment=ft.MainAxisAlignment.END),
            ], spacing=4)
            content_rows.append(ft.Container(row, margin=ft.Margin(
                left=0, right=0, top=6, bottom=6)))
        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column([
                ft.Text(f"{title} · 共 {len(issues)} 条", size=15,
                        weight=ft.FontWeight.BOLD),
                hint("只报告，不改文。标记误报后该原文片段不再提示。"),
                ft.Column(content_rows, scroll=ft.ScrollMode.AUTO, height=420),
                ft.Row([ft.TextButton("关闭", on_click=lambda e: self.page.pop_dialog())],
                       alignment=ft.MainAxisAlignment.END),
            ], spacing=6), padding=16, height=560),
            show_drag_handle=True))

    def _check_chapter(self, e):
        ch = self._current_desk_chapter()
        cid = ch["id"]

        def job():
            from app.core.checker import Checker
            return Checker(self.session.store).check_chapter(cid)

        def done(r):
            if isinstance(r, dict) and not r.get("ok"):
                toast(self.page, r.get("error", "检查失败"), error=True)
                return
            issues = r.get("issues", []) if isinstance(r, dict) else []
            if not issues:
                toast(self.page, "未发现问题 ✓")
                return
            self._issues_sheet(f"第{ch.get('order_index')}章检查结果", issues)
        toast(self.page, "正在检查本章…")
        run_bg(self.page, job, done)

    def _style_check(self, e):
        ch = self._current_desk_chapter()
        store = self.session.store
        cid = ch["id"]

        def local_job():
            from app.core.style_checker import analyze, recent_chapter_texts, CLICHES
            blocks = store.list_text_blocks(cid)
            accepted = [b for b in blocks if b["status"] in ("已接受", "已锁定")]
            if not accepted:
                return {"ok": False, "error": "本章还没有已接受的正文"}
            prose = "\n\n".join(b["content"] for b in accepted)
            recent = recent_chapter_texts(store, cid)
            cliches = [x.strip() for x in
                       (store.get_setting("style_cliches", "") or "").splitlines()
                       if x.strip()] or CLICHES
            cands = analyze(prose, recent, cliches, None)
            return {"ok": True, "prose": prose, "candidates": cands, "chapter": ch}

        def local_done(r):
            if not r.get("ok"):
                toast(self.page, r.get("error", "审校失败"), error=True)
                return
            if not r.get("candidates"):
                toast(self.page, "未发现文风问题 ✓")
                return

            def on_done(result):
                if not result.get("ok"):
                    return
                from app.core.jsonx import extract_json
                obj, err = extract_json(result["text"])
                if err:
                    toast(self.page, f"审校结果解析失败：{err}", error=True)
                    return
                raw = obj.get("issues") if isinstance(obj, dict) else \
                    (obj if isinstance(obj, list) else [])
                issues = [it for it in raw if isinstance(it, dict)]
                if not issues:
                    toast(self.page, "未发现文风问题 ✓")
                    return
                self._issues_sheet(f"第{ch.get('order_index')}章文风审校", issues)

            from app.core.task_templates import render as render_task
            start_generation(self.page, self.session, role="style",
                             context={"chapter": ch, "prose": r["prose"],
                                      "style_candidates": r["candidates"],
                                      "character_names": ch.get("characters") or []},
                             task=render_task("style", store), title="文风审校",
                             on_done=on_done)
        toast(self.page, "程序检测中…")
        run_bg(self.page, local_job, local_done)

    # =====================================================
    # 写作台 · 更多（本章备注 / 版本快照 / 风格样本 / 粘贴导入 / 改写设置）
    # =====================================================

    def _more_sheet(self, ch: dict):
        """写作台「更多」：按用途分组的底部弹层。

        老板 2026-11-16 说「按钮太多太挤，找不到」：旧版 9 个操作平铺成两行
        wrap，主次不分。现在底部栏只留 1 主 + 3 次，其余全进这里按组排。
        分组不是装饰——找「快照」的人会先扫「本章资料」，不会去「写作」里翻。
        """
        def _go(fn):
            def handler(e):
                self.page.pop_dialog()
                fn()
            return handler

        groups = [
            ("写作", [
                (ft.Icons.FACT_CHECK_OUTLINED, "检查本章",
                 "查逻辑/设定冲突与前后不一致（只报告，不改文）",
                 lambda: self._check_chapter(None)),
                (ft.Icons.RATE_REVIEW_OUTLINED, "文风审校",
                 "查重复用词、AI 腔、节奏问题（只报告，不改文）",
                 lambda: self._style_check(None)),
                (ft.Icons.AUTO_FIX_HIGH, "改写本章",
                 "按改写设置重写已接受的正文，出稿后可对比再应用",
                 lambda: self._do_style_rewrite(ch)),
            ]),
            ("本章资料", [
                (ft.Icons.STICKY_NOTE_2_OUTLINED, "本章备注",
                 "记几条本章动态/提醒，写作时作为上下文注入",
                 lambda: self._notes_sheet(ch)),
                (ft.Icons.HISTORY, "版本快照 / 回滚",
                 "每次落库自动存旧版，可随时回看与回滚",
                 lambda: self._snapshot_sheet(ch)),
                (ft.Icons.BRUSH_OUTLINED, "风格样本",
                 "把本章某段正文标记为风格样本（注入写作与改写）",
                 lambda: self._style_sheet(ch)),
            ]),
            ("导入", [
                (ft.Icons.CONTENT_PASTE_GO_OUTLINED, "粘贴细纲…",
                 "直接粘贴细纲文本入库（只剥不改，跳过 AI）",
                 lambda: self._paste_dialog(ch, "detail")),
                (ft.Icons.CONTENT_PASTE_GO_OUTLINED, "粘贴正文…",
                 "直接粘贴正文本入库（单章或多章，只剥不改）",
                 lambda: self._paste_dialog(ch, "prose")),
            ]),
            ("危险操作", [
                (ft.Icons.BACKSPACE_OUTLINED, "清空编辑器",
                 "只清空当前编辑器里的内容，已落库正文不受影响",
                 lambda: self._set_editor("")),
                (ft.Icons.PLAYLIST_REMOVE, "清空分批",
                 "丢弃本章全部未合并批次（已接受正文不受影响）",
                 lambda: self._clear_batches(ch)),
                (ft.Icons.DELETE_FOREVER_OUTLINED, "清空全书正文",
                 "结构保留，全书正文推倒重写（清前自动整库备份）",
                 lambda: self._clear_all_prose()),
            ]),
        ]
        body: list = []
        for name, entries in groups:
            body.append(T.section(name))
            danger = (name == "危险操作")
            for icon, title, sub, fn in entries:
                body.append(T.menu_row(
                    icon, title, sub, on_click=_go(fn),
                    color=T.danger() if danger else None))
        T.sheet(self.page, "写作台 · 更多", body,
                subtitle_text=f"第{ch.get('order_index', '?')}章 "
                              f"{ch.get('title') or ''}",
                height=self._dlg_h(560))

    # ---------- 清空全书正文（危险操作，对齐桌面 writing_desk._clear_all_prose） ----------

    def _clear_all_prose(self):
        """清空全书正文（2026-11-13 桌面老板需求，手机对齐）：结构留着，正文推倒重写。

        全书级破坏性操作，护栏（桌面三道里手机适用的两道）：
        ① 自动模式 / 生成任务在跑就不许清——清完后台任务接着落库，界面与库两头对不上；
        ② 清理前整库备份：内核 store.clear_all_prose 内部先 backup() 再删，
           备份失败抛异常、数据不动。
        确认 sheet 把「删什么 / 留什么」摊开 + 明确确认，两个默认勾选项与桌面同语义。
        """
        store = self.session.store
        if store is None:
            toast(self.page, "请先打开项目再清空正文", error=True)
            return
        if self._ap_running_any():
            toast(self.page, "自动模式运行中——请先停止自动模式再清空正文", error=True)
            return
        if self._task is not None and not self._task.stopped:
            toast(self.page, "当前还有生成任务在跑——请先停止，再清空正文", error=True)
            return
        try:
            stats = store.prose_stats()
        except Exception as e:
            toast(self.page, f"读取正文统计失败：{e}", error=True)
            return
        if not stats.get("prose_chapters"):
            toast(self.page, "全书还没有正文，不需要清空")
            return
        deleted = f"· 全部正文（{stats['prose_chapters']} 章，{stats['blocks']} 块）"
        if stats.get("snapshots"):
            deleted += f" + {stats['snapshots']} 份历史版本快照"
        extra = []
        if stats.get("batches"):
            extra.append(f"· 分批 / 改写的中间底稿（{stats['batches']} 批）")
        if stats.get("notes"):
            extra.append(f"· 本章动态 {stats['notes']} 条（旧正文提取的事件与场景）")
        mem_ck = ft.Checkbox(
            label="同时重置记忆链（旧记忆来自旧正文，留着会让新正文带着旧剧情状态）",
            value=True)
        cache_ck = ft.Checkbox(
            label="同时清空正文生成缓存（不清的话「重新写」可能直接回放旧结果）",
            value=True)

        def _yes(ev):
            self.page.pop_dialog()
            self._do_clear_all_prose(bool(mem_ck.value), bool(cache_ck.value))

        def _cancel(ev):
            self.page.pop_dialog()
        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column([
                ft.Text("清空全书正文", size=16, weight=ft.FontWeight.BOLD),
                hint(f"全书 {stats['chapters']} 章，其中 {stats['prose_chapters']} 章已有正文："
                     f"约 {stats['words']:,} 字 / {stats['blocks']} 个正文块。"),
                ft.Text("将删除", size=13, weight=ft.FontWeight.W_600),
                ft.Text("\n".join([deleted] + extra), size=12),
                ft.Text("将保留", size=13, weight=ft.FontWeight.W_600),
                ft.Text("总纲 · 分卷 · 章节大纲 · 细纲块 · 设定库 · 伏笔台账 · 请求日志\n"
                        "清完后章状态回退成「有细纲 / 有大纲」，可直接重新生成正文。",
                        size=12),
                mem_ck, cache_ck,
                ft.Text("编辑器里还没点「接受」的正文也会一起丢弃。清理前会自动把整库"
                        "备份到项目文件夹 backups/，需要时可回滚。",
                        size=12, color=ft.Colors.RED_700),
                ft.Row([
                    ft.TextButton("取消", on_click=_cancel),
                    ft.FilledButton("清空正文", on_click=_yes),
                ], alignment=ft.MainAxisAlignment.END, spacing=8),
            ], spacing=6, scroll=ft.ScrollMode.AUTO),
                padding=16, height=self._dlg_h(620)),
            show_drag_handle=True))

    def _do_clear_all_prose(self, clear_memory: bool, clear_cache: bool):
        """确认后的执行（UI 与落库解耦，供测试复用）：内核一条链完成
        备份→删除→状态回退，返回统计供 toast 汇报。"""
        store = self.session.store
        try:
            result = store.clear_all_prose(clear_memory=clear_memory,
                                           clear_cache=clear_cache)
        except Exception as e:
            toast(self.page, f"清空失败（数据未改动）：{type(e).__name__}: {e}", error=True)
            return
        self._diag = None
        msg = f"已清空 {result['prose_chapters']} 章正文（约 {result['words']:,} 字）"
        if result.get("memory_reset"):
            msg += "，记忆链已重置"
        if result.get("backup"):
            name = str(result["backup"]).replace("\\", "/").rsplit("/", 1)[-1]
            msg += f"；清理前已备份 {name}（项目文件夹 backups/）"
        toast(self.page, msg)
        self.refresh()

    # ---------- 本章备注（chapter_notes） ----------

    def _notes_sheet(self, ch: dict):
        store = self.session.store
        cid = ch["id"]
        field = ft.TextField(label="新增一条备注", multiline=True, min_lines=2,
                             max_lines=4,
                             hint_text="本章要发生的事、要提醒 AI 的要点…")

        def _reload():
            try:
                self.page.pop_dialog()
            except Exception:
                pass
            self._notes_sheet(ch)
            self.refresh()

        def _add(ev):
            txt = (field.value or "").strip()
            if not txt:
                toast(self.page, "备注为空", error=True)
                return
            store.add_note(cid, txt)
            _reload()

        def _del(note_id):
            store.delete_note(note_id)
            _reload()

        def _clear(ev):
            def _yes():
                store.clear_notes(cid)
                _reload()
            confirm(self.page, "清空本章备注", "删除本章全部备注？", _yes)

        notes = store.list_notes(cid)
        rows = []
        for i, n in enumerate(notes, start=1):
            rows.append(ft.Container(ft.Row([
                ft.Text(f"{i}.", size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Text(n.get("content") or "", size=12, expand=True),
                ft.IconButton(ft.Icons.DELETE_OUTLINE, icon_size=16,
                              on_click=lambda e, nid=n["id"]: _del(nid)),
            ], spacing=6), padding=ft.Padding(left=4, right=0, top=2, bottom=2)))
        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column([
                ft.Text(f"本章备注（{len(notes)} 条）", size=16,
                        weight=ft.FontWeight.BOLD),
                hint("备注随正文生成一并注入，AI 写作时能看到。"),
                ft.Column(rows if rows else [
                    ft.Container(ft.Text("还没有备注", size=11,
                                         color=ft.Colors.ON_SURFACE_VARIANT),
                                 padding=8)],
                    scroll=ft.ScrollMode.AUTO, height=240),
                field,
                ft.Row([
                    ft.TextButton("清空全部", on_click=_clear, disabled=not notes),
                    ft.FilledButton("添加", on_click=_add),
                ], alignment=ft.MainAxisAlignment.END, spacing=8),
            ], spacing=8), padding=16, height=540),
            show_drag_handle=True))

    # ---------- 版本快照 / 回滚（text_block_snapshots） ----------

    @staticmethod
    def _block_label(tb: dict) -> str:
        if tb.get("kind") == "chapter":
            return "整章正文"
        if tb.get("block_order"):
            return f"块{tb.get('block_order')}"
        return "自由写作块"

    def _snapshot_sheet(self, ch: dict):
        store = self.session.store
        tbs = store.list_text_blocks(ch["id"])
        if not tbs:
            toast(self.page, "本章还没有已落库的正文")
            return
        rows = []
        for t in tbs:
            n = len(store.snapshot_history(t["id"]))
            rows.append(ft.ListTile(
                leading=ft.Icon(ft.Icons.ARTICLE_OUTLINED, size=18),
                title=ft.Text(f"{self._block_label(t)} ｜ 当前 v{t.get('version')}",
                              size=13),
                subtitle=ft.Text(f"{n} 个历史版本 ｜ {len(t.get('content') or '')} 字",
                                 size=11),
                dense=True,
                on_click=lambda e, tb=t: (self.page.pop_dialog(),
                                          self._snapshot_versions(ch, tb))))
        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column([
                ft.Text("版本快照 / 回滚", size=16, weight=ft.FontWeight.BOLD),
                hint("每次「接受」会把旧版自动存快照；回滚前当前版也会先存快照。"),
                ft.Column(rows, scroll=ft.ScrollMode.AUTO, height=380),
                ft.Row([ft.TextButton("关闭", on_click=lambda e: self.page.pop_dialog())],
                       alignment=ft.MainAxisAlignment.END),
            ], spacing=8), padding=16, height=520),
            show_drag_handle=True))

    def _snapshot_versions(self, ch: dict, tb: dict):
        store = self.session.store
        snaps = store.snapshot_history(tb["id"])
        if not snaps:
            toast(self.page, "这一版还没有历史快照")
            return

        def _rollback(version, content):
            def _yes():
                self.page.pop_dialog()
                store.rollback_text_block(tb["id"], version)
                self._diag = None
                toast(self.page, f"已回滚到 v{version}")
                self.refresh()
            confirm(self.page, "回滚正文",
                    f"回到 v{version}（{len(content)} 字）？当前版本会先存快照，可随时再切回。",
                    _yes, yes_text="回滚")

        rows = []
        for s in reversed(snaps):      # 新→旧：最可能回的目标排最上
            c = s.get("content") or ""
            rows.append(ft.ListTile(
                leading=ft.Icon(ft.Icons.HISTORY, size=18),
                title=ft.Text(f"v{s.get('version')} ｜ {len(c)} 字", size=13),
                subtitle=ft.Text((c.replace("\n", " ")[:50] or "（空）"), size=11,
                                 max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                dense=True,
                on_click=lambda e, v=s.get("version"), cc=c: _rollback(v, cc)))
        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column([
                ft.Text("选择要回滚的版本", size=16, weight=ft.FontWeight.BOLD),
                hint(f"{self._block_label(tb)}：共 {len(snaps)} 个历史版本"),
                ft.Column(rows, scroll=ft.ScrollMode.AUTO, height=380),
                ft.Row([ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog())],
                       alignment=ft.MainAxisAlignment.END),
            ], spacing=8), padding=16, height=520),
            show_drag_handle=True))

    # ---------- 风格样本（text_blocks.is_style_sample） ----------

    def _style_sheet(self, ch: dict):
        store = self.session.store
        tbs = store.list_text_blocks(ch["id"])
        if not tbs:
            toast(self.page, "本章还没有已落库的正文")
            return
        all_n = len(store.list_style_samples())

        def _toggle(tb_id, on):
            store.mark_style_sample(tb_id, on)
            self.refresh()

        rows = []
        for t in tbs:
            sw = ft.Switch(
                value=bool(t.get("is_style_sample")),
                label=f"{self._block_label(t)}（{len(t.get('content') or '')} 字）")
            sw.on_change = lambda e, tid=t["id"]: _toggle(tid, bool(e.control.value))
            rows.append(sw)
        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column([
                ft.Text(f"风格样本（全项目 {all_n} 段）", size=16,
                        weight=ft.FontWeight.BOLD),
                hint("标记后写作/改写会注入前 3 段（各取 800 字）作为文风参照。"),
                ft.Column(rows, scroll=ft.ScrollMode.AUTO, height=360),
                ft.Row([ft.TextButton("关闭", on_click=lambda e: self.page.pop_dialog())],
                       alignment=ft.MainAxisAlignment.END),
            ], spacing=8), padding=16, height=520),
            show_drag_handle=True))

    # ---------- 手动粘贴导入（只剥不改） ----------

    def _paste_dialog(self, ch: dict, kind: str):
        is_detail = kind == "detail"
        title = "粘贴细纲" if is_detail else "粘贴正文"
        field = ft.TextField(
            label=title + "内容", multiline=True, min_lines=12, max_lines=24,
            text_size=12,
            hint_text=("每块以『### 块N ｜ 归属X』起头，块内用『**字段**：值』…"
                       if is_detail else
                       "单章直接贴正文；多章用『## 第N章：章名』独立成行起头"))

        def _ok(ev):
            raw = field.value or ""
            self.page.pop_dialog()
            if not raw.strip():
                toast(self.page, "内容为空", error=True)
                return
            if is_detail:
                self._paste_detail(ch, raw)
            else:
                self._paste_prose(ch, raw)

        def _cancel(ev):
            self.page.pop_dialog()
        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text(title),
            content=ft.Container(ft.Column([
                hint("只做必要剥离（思考段 / 破甲残留 / 完成标志行）——"
                     "不补全、不改写、不润色。"),
                field,
            ], tight=True, spacing=8, scroll=ft.ScrollMode.AUTO),
                width=520, height=430),
            actions=[ft.TextButton("取消", on_click=_cancel),
                     ft.FilledButton("导入", on_click=_ok)],
            actions_alignment=ft.MainAxisAlignment.END))

    def _paste_detail(self, ch: dict, raw: str):
        from app.core.manual_import import clean_manual_text
        from app.core.pipeline import Pipeline
        store = self.session.store
        cid = ch["id"]
        cleaned = clean_manual_text(raw)
        if cleaned.get("notes"):
            toast(self.page, "已自动处理：" + "、".join(cleaned["notes"]))
        blocks, err = Pipeline.parse_detail_blocks(cleaned.get("text") or "")
        if err or not blocks:
            toast(self.page, f"细纲解析失败：{err or '结果为空'}", error=True)
            return
        checks = [ft.Checkbox(label=f"块{i + 1}：{(b.get('content') or '')[:40]}",
                              value=True)
                  for i, b in enumerate(blocks)]

        def _write(ev):
            kept = [b for b, ck in zip(blocks, checks) if ck.value]
            self.page.pop_dialog()
            if not kept:
                toast(self.page, "未勾选任何块", error=True)
                return
            old_n = len(store.list_blocks(cid))
            if old_n:
                def _yes():
                    self._apply_pasted_detail(ch, kept)
                confirm(self.page, "替换细纲",
                        f"本章已有 {old_n} 块细纲。替换会作废旧细纲及其正文块（含快照）。"
                        "继续？", _yes, yes_text="替换")
                return
            self._apply_pasted_detail(ch, kept)

        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column([
                ft.Text(f"解析出 {len(blocks)} 块（勾选要写入的）", size=15,
                        weight=ft.FontWeight.BOLD),
                ft.Column(checks, scroll=ft.ScrollMode.AUTO, height=360),
                ft.Row([ft.TextButton("取消",
                                      on_click=lambda e: self.page.pop_dialog()),
                        ft.FilledButton("写入", on_click=_write)],
                       alignment=ft.MainAxisAlignment.END),
            ], spacing=8), padding=16, height=520),
            show_drag_handle=True))

    def _paste_prose(self, ch: dict, raw: str):
        from app.core.manual_import import clean_manual_text
        from app.core.pipeline import Pipeline
        store = self.session.store
        cleaned = clean_manual_text(raw)
        if cleaned.get("notes"):
            toast(self.page, "已自动处理：" + "、".join(cleaned["notes"]))
        items, err = Pipeline.parse_multi_chapter_prose(
            cleaned.get("text") or "", min_chars=Pipeline._CHAPTER_PROSE_MIN)
        if err:
            toast(self.page, err, error=True)
            return
        if not items:
            toast(self.page, "没有解析出任何正文", error=True)
            return
        if len(items) == 1 and not items[0].get("no"):
            # 单章无标题 → 落到当前章（与桌面同口径）
            it = items[0]

            def _yes():
                self._apply_pasted_prose(ch, it.get("prose") or "")
            confirm(self.page, "导入正文",
                    f"未识别到章标题，按单章写入当前第{ch.get('order_index')}章"
                    f"《{ch.get('title') or ''}》（{it.get('chars') or 0} 字）？",
                    _yes, yes_text="写入")
            return
        self._confirm_paste_chapters(items)

    # ---- 粘贴导入落库（与 UI 解耦，便于测试与复用） ----

    def _apply_pasted_detail(self, ch: dict, blocks: list):
        """粘贴细纲落库：整章替换（旧细纲及其正文块作废，由 store 保证原子）。"""
        self.session.store.replace_blocks(ch["id"], blocks)
        toast(self.page, f"已写入 {len(blocks)} 块细纲")
        self.refresh()

    def _apply_pasted_prose(self, ch: dict, prose: str):
        """粘贴单章正文落到指定章（旧版自动存快照）。"""
        store = self.session.store
        store.accept_chapter_prose(ch["id"], prose)
        store.set_chapter_status(ch["id"], "已完成")
        toast(self.page, f"已写入本章（{len(prose)} 字）")
        self.refresh()

    def _apply_pasted_chapters(self, items: list) -> int:
        """多章粘贴落库：章号/标题能对上的覆盖，对不上的新建章节。返回写入章数。"""
        store = self.session.store
        n = 0
        for it in items:
            prose = (it.get("prose") or "").strip()
            if not prose:
                continue
            t = it.get("_target")
            if t:
                store.accept_chapter_prose(t["id"], prose)
                store.set_chapter_status(t["id"], "已完成")
            else:
                new_ch = store.add_chapter({
                    "title": (it.get("title")
                              or f"导入章节{it.get('no') or ''}").strip(),
                    "status": "已完成"})
                store.accept_chapter_prose(new_ch["id"], prose)
            n += 1
        return n

    def _confirm_paste_chapters(self, items: list):
        store = self.session.store
        chapters = store.list_chapters()
        by_no = {c.get("order_index"): c for c in chapters}
        by_title = {c.get("title"): c for c in chapters}
        rows = []
        for i, it in enumerate(items, start=1):
            no, title = it.get("no"), (it.get("title") or "").strip()
            target = by_no.get(no) if no else None
            if target is None and title:
                target = by_title.get(title)
            it["_target"] = target
            where = (f"→ 覆盖第{target.get('order_index')}章《{target.get('title') or ''}》"
                     if target else "→ 新建章节")
            rows.append(ft.Text(f"{i}. 第{no or '?'}章 {title or '（无标题）'}"
                                f"（{it.get('chars') or 0} 字）{where}", size=12))

        def _write(ev):
            self.page.pop_dialog()
            n = self._apply_pasted_chapters(items)
            toast(self.page, f"已导入 {n} 章正文（旧版自动存快照）")
            self.refresh()

        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column([
                ft.Text(f"识别到 {len(items)} 章，写入目标如下", size=15,
                        weight=ft.FontWeight.BOLD),
                hint("标题匹配不上的会新建章节；已存在的章节正文会被覆盖（旧版存快照）。"),
                ft.Column(rows, scroll=ft.ScrollMode.AUTO, height=360),
                ft.Row([ft.TextButton("取消",
                                      on_click=lambda e: self.page.pop_dialog()),
                        ft.FilledButton("写入", on_click=_write)],
                       alignment=ft.MainAxisAlignment.END),
            ], spacing=8), padding=16, height=540),
            show_drag_handle=True))

    # ---------- 改写（整章差量替换协议，role=prose_rewrite） ----------

    def _do_style_rewrite(self, ch: dict):
        """整章一次调用：AI 只回传病灶段条目（P段号：改后的段落），
        程序把条目替换进初稿，其余段落原样保留（不经模型）。
        只改写法不改事实；结果与原版对比择一，点「应用并接受」直接落库。"""
        from app.core.style_rewrite import apply_diff, build_whole
        store = self.session.store
        cid = ch["id"]
        ed = getattr(self, "_desk_editor", None)
        text = (ed.value if ed else "") or self._accepted_prose(cid)
        text = (text or "").strip()
        if not text:
            toast(self.page, "没有可改写的内容", error=True)
            return
        ctx, task = build_whole(store, ch, text)
        close_running = None

        def on_started(tk):
            nonlocal close_running
            close_running = self._running(tk, "改写中…")

        def on_done(result):
            if close_running:
                close_running()
            if not result.get("ok"):
                return
            if result.get("status") == "stopped":
                toast(self.page, "已停止")
                return
            from app.core.pipeline import Pipeline
            info: dict = {}
            repl = Pipeline.parse_style_rewrite_diff(result.get("text") or "", info)
            dels = info.get("deletions") or []
            if not repl and not dels and not info.get("none_declared"):
                toast(self.page, "未识别到替换条目（P段号：改写后的段落），输出可能不符合格式",
                      error=True)
                return
            res = apply_diff(text, repl, dels)
            merged = res["text"]
            applied, skipped = res["applied"], res["skipped"]
            if skipped:
                toast(self.page, "段号 %s 超出本章范围，已忽略"
                      % "、".join(str(s) for s in skipped))
            dropped = info.get("meta_dropped") or []
            if dropped:
                # 模型想「删掉这一段」只回了标记（如【此段落已按指令删除】）：
                # 解析层已丢弃该条目 → 原段保留，标记绝不落进正文（2026-10-10）。
                toast(self.page, "模型对第 %s 段只回了「已删除」类标记（未按删除条目格式），已保留原段"
                      % "、".join(str(s) for s in dropped))
            if res["del_rejected"]:
                toast(self.page, "模型要求删除 %d 段，超过安全上限（全章段数三分之一），已全部保留原段"
                      % res["del_rejected"])
            if res["deleted"]:
                toast(self.page, "AI 将删除第 %s 段（对比确认后生效）"
                      % "、".join(str(s) for s in res["deleted"]))
            if not applied and not res["deleted"]:
                if dropped or res["del_rejected"]:
                    return
                toast(self.page, "AI 判定全章无病灶，无需改写"
                      if info.get("none_declared") else "没有可落地的替换条目")
                return
            self._apply_diag(result, "prose_rewrite", merged)
            self._pick_rewrite_version(ch, text, merged)

        start_generation(self.page, self.session, role="prose_rewrite",
                         context=ctx, task=task,
                         title=f"改写（整章 {len(text)} 字）", on_done=on_done,
                         on_started=on_started)

    def _pick_rewrite_version(self, ch: dict, old: str, new: str):
        """改写结果与原版对比择一（不做静默覆盖）。"""
        rg = ft.RadioGroup(value="new", content=ft.Column([
            ft.Radio(value="new", label=f"改写版（{len(new)} 字）"),
            ft.Radio(value="old", label=f"原版（{len(old)} 字）"),
        ], spacing=0))

        def _ok(ev):
            chosen = new if rg.value == "new" else old
            self.page.pop_dialog()
            self._apply_rewrite_result(ch, chosen)

        def _cancel(ev):
            self.page.pop_dialog()
        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column([
                ft.Text("改写完成，选一版", size=15, weight=ft.FontWeight.BOLD),
                rg,
                ft.Text("改写版", size=12, weight=ft.FontWeight.W_600),
                ft.Container(ft.TextField(value=new, multiline=True, read_only=True,
                                          min_lines=6, max_lines=12, text_size=12,
                                          border_color=ft.Colors.TRANSPARENT),
                             height=170),
                ft.Text("原版", size=12, weight=ft.FontWeight.W_600),
                ft.Container(ft.TextField(value=old, multiline=True, read_only=True,
                                          min_lines=6, max_lines=12, text_size=12,
                                          border_color=ft.Colors.TRANSPARENT),
                             height=170),
                ft.Row([ft.TextButton("取消", on_click=_cancel),
                        ft.FilledButton("应用并接受", on_click=_ok)],
                       alignment=ft.MainAxisAlignment.END, spacing=8),
            ], spacing=6, scroll=ft.ScrollMode.AUTO), padding=16, height=600),
            show_drag_handle=True))

    def _apply_rewrite_result(self, ch: dict, text: str):
        """改写结果「应用即接受」（对齐桌面 2026-11-14 定调）：对比窗点「应用并接受」
        就是确认这一稿，直接落库（旧版自动存快照）+ 章状态推进 + 归档收尾同链，
        不再回填编辑器等二次点「接受本章」。

        落库口径与 _prose_accept 相同：store.accept_chapter_prose 一条链完成
        快照/降级分块正文/清批次工作区；分批模式下编辑器里是批次合并预览，
        同样走这里（批次未合并时落库即代表接受当前合并稿）。
        落库前仍过 _guard_incomplete 闸门：诊断到异常时先停下来问，闸门不拆。
        改写来源标记：接受后即使已有快照也重归档（与桌面「改写例外」同口径）。
        """
        store = self.session.store
        cid = ch["id"]
        text = (text or "").strip()
        if not text:
            toast(self.page, "改写结果为空", error=True)
            return

        def _do():
            demoted = sum(1 for t in store.list_text_blocks(cid)
                          if t.get("scene_block_id") and t["status"] == "已接受")
            self._pending_rewrite.add(cid)
            store.accept_chapter_prose(cid, text)
            self._diag = None
            self._set_editor(text)
            toast(self.page, f"已应用并接受（{len(text)} 字，旧版自动存快照）")
            if demoted:
                toast(self.page, f"本章 {demoted} 段旧的分块正文已转为草稿"
                                 "（整章正文已覆盖）")
            self.refresh()
            # 章状态推进 + 自动归档统一走 _after_prose_done（对齐桌面）
            self._after_prose_done(cid)
        if self._guard_incomplete(_do, yes_text="仍然应用"):
            _do()

