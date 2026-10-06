# -*- coding: utf-8 -*-
"""创作页：总纲 / 分卷 / 大纲 / 写作台 / 记忆 / 设定 六个页签。

流程与落库对齐桌面版（总纲候选→锁定导入分卷→切章→细化大纲→整章正文→归档），
UI 面向手机：确认环节用 bottom sheet / 对话框。

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

from widgets import hint, empty, toast, confirm
from bridge import StreamTask, run_bg
from gen import start_generation

from app.core.assembler import Assembler

# 改写要求兜底（与桌面 `app/ui/writing_desk.py` 的 REQ_FALLBACK 同文案，改一处必须改两处）
REQ_FALLBACK = "按用户要求改写该段落，保持原意，提升表达质量"


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


class WritingPage:
    def __init__(self, page, session):
        self.page = page
        self.session = session
        self.on_refresh = None  # 主界面注入：整页重建
        self._tab = 0
        self._task = None       # 当前 StreamTask（可停止）
        self._outline_chapter_id = None
        self._desk_chapter_id = None
        # 改写来源的章（接受后即使已有快照也自动重归档，见 _after_prose_done）
        self._pending_rewrite: set = set()

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
        return ft.Tabs(
            length=6,
            selected_index=self._tab,
            on_change=self._on_tab_change,
            content=ft.Column([
                self._ap_bar(),
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

    def _ap_bar(self) -> ft.Control:
        ap = self._ap
        label = (self._ap_label or {}).get("v", "") if hasattr(self, "_ap_label") else ""
        if ap is None or not ap.running:
            return ft.Row([
                ft.Text("⚡ 自动模式：从当前进度自动接力到完本", size=12,
                        color=ft.Colors.ON_SURFACE_VARIANT, expand=True),
                ft.OutlinedButton("开启", icon=ft.Icons.BOLT, on_click=self._ap_start),
            ])
        if ap.paused:
            state_text = "已暂停：" + label
            btn = ft.FilledTonalButton("继续", icon=ft.Icons.PLAY_ARROW,
                                       on_click=lambda e: ap.resume())
            color = ft.Colors.ORANGE
        else:
            state_text = label or "自动推进中…"
            btn = ft.OutlinedButton("暂停", icon=ft.Icons.PAUSE,
                                    on_click=lambda e: ap.pause())
            color = ft.Colors.GREEN
        return ft.Container(
            ft.Row([
                ft.Text(state_text, size=12, expand=True,
                        overflow=ft.TextOverflow.ELLIPSIS),
                btn,
                ft.IconButton(ft.Icons.STOP_CIRCLE_OUTLINED,
                              on_click=lambda e: self._ap_stop()),
            ]),
            bgcolor=ft.Colors.with_opacity(0.10, color),
            border_radius=10,
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
        self._ap.start()
        self.refresh()

    def _ap_stop(self):
        confirm(self.page, "退出自动模式", "彻底停止自动推进？",
                lambda: (self._ap.stop(), self.refresh()))

    def _ap_state(self, state: str, info: str = ""):
        if state == "done":
            toast(self.page, info or "全书完本！")
        elif state == "error":
            toast(self.page, f"自动模式暂停：{info}", error=True)
        self.refresh()

    def _ap_set_label(self, label):
        if label is not None:
            self._ap_label = getattr(self, "_ap_label", {"v": ""})
            self._ap_label["v"] = label
            self.refresh()

    def _view_for_tab(self, i: int) -> ft.Control:
        views = [self._synopsis_view, self._volumes_view, self._outline_view,
                 self._desk_view, self._memory_view, self._lore_view]
        return ft.Container(views[i](), padding=12, expand=True)

    def _on_tab_change(self, e):
        self._tab = e.control.selected_index
        self.refresh()

    def refresh(self):
        if self.on_refresh:
            self.on_refresh()
        else:
            try:
                self.page.update()
            except Exception:
                pass

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
        """章节选择 bottom sheet。"""
        store = self.session.store
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
                trailing=ft.Icon(ft.Icons.CHEVRON_RIGHT, size=16),
                on_click=lambda e, ch=c: (self.page.pop_dialog(), on_pick(ch)),
            ))
        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column(
                [ft.Text("选择章节", size=16, weight=ft.FontWeight.BOLD)] + rows,
                scroll=ft.ScrollMode.AUTO), padding=16, height=460),
            show_drag_handle=True))

    # =====================================================
    # 页签 1：总纲
    # =====================================================

    def _synopsis_view(self) -> ft.Control:
        store = self.session.store
        s = store.current_synopsis()
        content = (s.get("content") or "").strip() if s else ""
        locked = bool(s.get("is_locked")) if s else False
        head = ft.Row([
            ft.Text("故事总纲", size=17, weight=ft.FontWeight.BOLD, expand=True),
            ft.Chip(ft.Text("已锁定" if locked else "未锁定"),
                    bgcolor=ft.Colors.with_opacity(0.15, ft.Colors.TEAL if locked
                                                   else ft.Colors.GREY)),
        ])
        body_controls = [
            head,
            *( [ft.Container(ft.Text(content, size=13, selectable=True),
                             bgcolor=ft.Colors.with_opacity(0.05, ft.Colors.ON_SURFACE),
                             border_radius=10, padding=12,
                             margin=ft.Margin(left=0, right=0, top=8, bottom=8))]
               if content else [empty("还没有总纲\n点下方「AI 生成总纲」开始，"
                                       "或「手动编辑」直接写")]),
        ]
        actions = ft.Column([
            ft.Row([
                ft.FilledButton("AI 生成总纲", on_click=self._syn_generate,
                                icon=ft.Icons.AUTO_AWESOME),
            ], spacing=8, wrap=True),
            # 协议栈增强开关（2026-09-23）：全局设置，与桌面版共用同一个键。
            # 破不了甲时打开（仅总纲任务生效，会多耗 token）。
            ft.Switch(label="协议栈增强（破不了甲时打开）",
                      value=str(store.get_setting("protocol_stack_enabled", "0")) == "1",
                      on_change=self._syn_toggle_stack),
            ft.Row([
                ft.TextButton("AI 迭代", on_click=self._syn_iterate,
                              disabled=not content),
                ft.TextButton("手动编辑", on_click=self._syn_edit,
                              disabled=bool(locked)),
                ft.TextButton("锁定并导入分卷", on_click=self._syn_lock,
                              icon=ft.Icons.LOCK, disabled=not content or locked),
            ], spacing=4, wrap=True),
            hint("锁定时自动识别总纲里的「### 分卷一/二…」并导入分卷页。"),
        ], spacing=4)
        return ft.Column(body_controls + [actions], scroll=ft.ScrollMode.AUTO,
                         expand=True, spacing=2)

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
        req_field = ft.TextField(label="创作要求（可选）", multiline=True, min_lines=2,
                                 max_lines=4,
                                 hint_text="题材、主角、金手指、风格、结局倾向…留空让 AI 自由发挥")

        def _ok(ev):
            self.page.pop_dialog()
            self._do_syn_generate((req_field.value or "").strip())

        def _cancel(ev):
            self.page.pop_dialog()
        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("生成总纲候选"),
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

    def _pick_candidate(self, cands: list):
        """候选选择 sheet → 确认全文 → 锁定。"""
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
            self._syn_lock(content=preview.value.strip(), title=content_holder["title"])

        def _save_only(ev):
            self.page.pop_dialog()
            self._syn_edit(preset_text=preview.value.strip())

        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column([
                ft.Text(f"共 {len(cands)} 个候选：选择一版查看/调整，然后锁定",
                        size=15, weight=ft.FontWeight.BOLD),
                radio_group,
                ft.Container(preview, height=300),
                ft.Row([
                    ft.TextButton("仅保存为草稿", on_click=_save_only),
                    ft.FilledButton("锁定并导入分卷", on_click=_lock),
                ], alignment=ft.MainAxisAlignment.END, spacing=8),
            ], spacing=8), padding=16, height=560),
            show_drag_handle=True))

    def _syn_iterate(self, e):
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
        from app.core.task_templates import render as render_task
        store = self.session.store
        current = f"标题：{title}\n{content}"
        task = render_task("synopsis_iter", store, n=2, current=current, feedback=fb)
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
            self._pick_candidate(cands)

        start_generation(self.page, self.session, role="synopsis",
                         context={"user_req": f"当前总纲：\n{current}\n\n修改意见：\n{fb}"},
                         task=task, title="迭代总纲", on_done=on_done,
                         on_started=on_started)

    # =====================================================
    # 页签 2：分卷
    # =====================================================

    def _volumes_view(self) -> ft.Control:
        store = self.session.store
        volumes = store.list_volumes()
        chapters = store.list_chapters()
        if not volumes and not chapters:
            return ft.Column([
                empty("还没有分卷\n锁定总纲后自动导入分卷，再逐卷「AI 切分章节」"),
            ], scroll=ft.ScrollMode.AUTO, expand=True)
        tiles = []
        for v in volumes:
            vchs = sorted((c for c in chapters if c.get("volume_id") == v["id"]),
                          key=lambda c: c.get("order_index") or 0)
            rows = []
            for c in vchs:
                rows.append(ft.Container(
                    ft.Row([
                        ft.Container(ft.Text(f"{c.get('order_index', '')}", size=12),
                                     width=28, alignment=ft.Alignment.CENTER),
                        ft.Text(c.get("title") or "未命名", size=13, expand=True),
                        ft.Container(ft.Text(c.get("status", ""), size=10),
                                     bgcolor=ft.Colors.with_opacity(
                                         0.14, STATUS_COLORS.get(c.get("status"),
                                                                 ft.Colors.GREY)),
                                     border_radius=6, padding=ft.Padding(left=6, right=6, top=2, bottom=2)),
                    ]), padding=ft.Padding(left=8, right=8, top=5, bottom=5)))
            if not vchs:
                rows.append(ft.Container(
                    ft.Text("本卷还没有切分章节", size=12,
                            color=ft.Colors.ON_SURFACE_VARIANT),
                    padding=10))
            tiles.append(ft.ExpansionTile(
                title=ft.Text(f"第{v.get('order_index')}卷 {v.get('title') or '未命名'}"
                              f"（{len(vchs)}章）", size=14,
                              weight=ft.FontWeight.W_600),
                subtitle=ft.Text((v.get("summary") or "")[:60], size=11, max_lines=1,
                                 overflow=ft.TextOverflow.ELLIPSIS)
                if v.get("summary") else None,
                maintain_state=True,
                controls=rows + [ft.Container(
                    ft.FilledTonalButton("AI 切分本卷章节",
                                         on_click=lambda e, vv=v: self._volume_split(vv),
                                         icon=ft.Icons.CALL_SPLIT),
                    padding=8)]))
        # 未分卷章节
        loose = [c for c in chapters if c.get("volume_id") is None]
        if loose:
            rows = [ft.Container(ft.Row([
                ft.Container(ft.Text(f"{c.get('order_index', '')}", size=12), width=28,
                             alignment=ft.Alignment.CENTER),
                ft.Text(c.get("title") or "未命名", size=13, expand=True),
                ft.Text(c.get("status", ""), size=10,
                        color=STATUS_COLORS.get(c.get("status"), ft.Colors.GREY)),
            ]), padding=ft.Padding(left=8, right=8, top=5, bottom=5)) for c in loose]
            tiles.append(ft.ExpansionTile(
                title=ft.Text(f"未分卷（{len(loose)}章）", size=14), controls=rows))
        return ft.Column(tiles, scroll=ft.ScrollMode.AUTO, expand=True, spacing=4)

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

    def _confirm_chapters(self, vol: dict, chapters: list):
        """切章结果确认：勾选后写入。"""
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
            toast(self.page, f"第{vol.get('order_index')}卷已写入 {len(picked)} 章")
            self.refresh()

        def _cancel(ev):
            self.page.pop_dialog()
        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column([
                ft.Text(f"切分结果：{len(chapters)} 章（勾选要写入的）", size=15,
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
                ft.TextButton("换一章", icon=ft.Icons.SWAP_HORIZONTAL_CIRCLE,
                              on_click=lambda e: self._pick_chapter(self._pick_outline_ch)),
            ]),
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
            ft.Container(ft.Text(outline_txt or "（本章还没有大纲内容）", size=13,
                                 selectable=True),
                         bgcolor=ft.Colors.with_opacity(0.05, ft.Colors.ON_SURFACE),
                         border_radius=10, padding=12, margin=ft.Margin(left=0, right=0, top=8, bottom=8)),
            ft.Row([
                ft.FilledButton("AI 细化本章大纲", on_click=lambda e: self._outline_refine(ch),
                                icon=ft.Icons.AUTO_AWESOME),
                ft.TextButton("手动编辑", on_click=lambda e: self._outline_edit(ch)),
            ], spacing=8, wrap=True),
            hint("细化只处理当前这一章：在 ①② 序号点初稿上补足场景/人物/信息推进/细节。"),
        ]
        return ft.Column(controls, scroll=ft.ScrollMode.AUTO, expand=True, spacing=2)

    def _current_outline_chapter(self):
        store = self.session.store
        chapters = store.list_chapters()
        if not chapters:
            return None
        for c in chapters:
            if c["id"] == self._outline_chapter_id:
                return store.get_chapter(c["id"])
        self._outline_chapter_id = chapters[0]["id"]
        return store.get_chapter(chapters[0]["id"])

    def _pick_outline_ch(self, ch: dict):
        self._outline_chapter_id = ch["id"]
        self.refresh()

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

    # =====================================================
    # 页签 4：写作台
    # =====================================================

    def _desk_view(self) -> ft.Control:
        store = self.session.store
        chapters = store.list_chapters()
        if not chapters:
            return ft.Column([empty("还没有章节\n先到「分卷」页切分章节")],
                             scroll=ft.ScrollMode.AUTO, expand=True)
        ch = self._current_desk_chapter()
        mode = store.get_prose_mode()
        from app.core.chapter_outline import chapter_outline_text
        controls = [
            ft.Row([
                ft.Text("写作台", size=17, weight=ft.FontWeight.BOLD),
                ft.Container(ft.Text(f"第{ch.get('order_index')}章 {ch.get('title') or ''}",
                                     size=13, expand=True, text_align=ft.TextAlign.END),
                             expand=True),
                ft.IconButton(ft.Icons.SWAP_HORIZONTAL_CIRCLE,
                              on_click=lambda e: self._pick_chapter(self._pick_desk_ch)),
            ]),
            ft.Container(ft.Text(chapter_outline_text(ch) or "（本章无大纲）", size=11,
                                 color=ft.Colors.ON_SURFACE_VARIANT, max_lines=3,
                                 overflow=ft.TextOverflow.ELLIPSIS),
                         bgcolor=ft.Colors.with_opacity(0.05, ft.Colors.ON_SURFACE),
                         border_radius=8, padding=8),
        ]
        banner = self._diag_banner()
        if banner is not None:
            controls.append(banner)
        auto_sw = ft.Switch(label="自动归档（写完接受即提取记忆，无需确认）",
                            value=self._auto_archive_on())

        def _toggle_auto(ev):
            store.set_setting("auto_archive", "1" if auto_sw.value else "0")
            toast(self.page, "已开启自动归档：接受正文后自动提取记忆"
                  if auto_sw.value else
                  "已关闭自动归档：写完请点「归档本章」逐条确认后归档")
        auto_sw.on_change = _toggle_auto
        controls.append(auto_sw)
        controls.append(ft.Dropdown(
            label="正文写法", value=mode, dense=True,
            options=[ft.dropdown.Option("chapter", "一次性（整章写完）"),
                     ft.dropdown.Option("batch", "分批（接力写作）")],
            on_select=self._on_mode_change))
        if mode == "batch":
            controls += self._batch_desk_controls(ch)
        else:
            controls += self._chapter_desk_controls(ch)
        controls.append(self._more_row(ch))
        return ft.Column(controls, scroll=ft.ScrollMode.AUTO, expand=True, spacing=6)

    # ---------- 写作台：诊断横幅（三态提示的落点） ----------

    def _diag_banner(self):
        """上次生成的诊断横幅。无问题时返回 None（完全不占位）。"""
        d = getattr(self, "_diag", None)
        if not d or not d.get("level"):
            return None
        color = ft.Colors.ERROR if d["level"] == "error" else ft.Colors.ORANGE
        return ft.Container(
            ft.Row([
                ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, size=16, color=color),
                ft.Text(d.get("reason") or "上次生成可能有异常", size=11, expand=True,
                        color=ft.Colors.ON_SURFACE, max_lines=2,
                        overflow=ft.TextOverflow.ELLIPSIS),
                ft.TextButton("详情", on_click=lambda e: self._show_diag()),
                ft.IconButton(ft.Icons.CLOSE, icon_size=16,
                              on_click=lambda e: self._clear_diag()),
            ], spacing=2),
            bgcolor=ft.Colors.with_opacity(0.12, color), border_radius=8, padding=6)

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

    def _on_mode_change(self, e):
        mode = (e.control.value or "chapter")
        self.session.store.set_prose_mode(mode)
        toast(self.page, "已切到「分批」写法"
              if mode == "batch" else "已切到「一次性」写法")
        self.refresh()

    # ---------- 写作台：编辑器区（一次性 / 分批共用） ----------

    def _desk_editor_block(self, ch: dict, whole: bool) -> list:
        """编辑器区：编辑器 + 生成/改写行 + 接受/归档行。

        whole=True（一次性）：显示「生成整章正文」，编辑器承载整章正文；
        whole=False（分批）：不显示整章按钮，编辑器承载已接受正文或
        批次合并预览（可改写/手改后「接受本章」落库）。
        """
        prose = self._accepted_prose(ch["id"])
        if not whole and not prose.strip():
            prose = self.session.store.prose_batch_merged(ch["id"])
        editor = ft.TextField(
            value=prose, multiline=True, min_lines=14, max_lines=24,
            border_color=ft.Colors.TRANSPARENT, text_size=13,
            hint_text=("点击「生成整章正文」让 AI 写作；也可以直接在这里手动写作/修改"
                       if whole else
                       "分批的合并预览显示在这里，检查/修改后「接受本章」落库；"
                       "也可手动写作"))
        self._desk_editor = editor
        gen_row = [
            *([ft.FilledButton("生成整章正文", on_click=self._prose_generate,
                               icon=ft.Icons.EDIT_NOTE)] if whole else []),
            ft.TextButton("改写", on_click=lambda e: self._rewrite_sheet(ch),
                          icon=ft.Icons.AUTO_FIX_HIGH),
            ft.TextButton("停止", on_click=self._prose_stop,
                          disabled=self._task is None or self._task.stopped),
        ]
        tip = ("生成后可手动修改，再点「接受本章」落库（旧版自动存快照，可回滚）。"
               "「归档本章」= 逐条确认后写入记忆（自动归档关闭时用它）。")
        if not whole:
            tip = ("全部批次完成后点「合并批次写入正文」，或在这里检查合并预览后"
                   "「接受本章」落库（旧版自动存快照）。")
        return [
            editor,
            ft.Row(gen_row, spacing=6, wrap=True),
            ft.Row([
                ft.FilledTonalButton("接受本章", on_click=self._prose_accept,
                                     icon=ft.Icons.CHECK_CIRCLE),
                ft.TextButton("归档本章", on_click=self._archive_chapter,
                              icon=ft.Icons.ARCHIVE_OUTLINED),
                ft.TextButton("清空编辑器", on_click=lambda e: self._set_editor("")),
            ], spacing=4, wrap=True),
            hint(tip),
        ]

    def _chapter_desk_controls(self, ch: dict) -> list:
        return self._desk_editor_block(ch, whole=True)

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

    def _batch_desk_controls(self, ch: dict) -> list:
        """分批模式：主按钮 + 每批块数 + 批次卡片 + 共用编辑器区 + 细纲块卡片。

        对齐桌面 writing_desk 分批模式：批次是工作区（prose_batches 表），
        逐批「生成 → 确认落批」，全部批完成后合并接受为整章正文（旧版存快照）。
        """
        from app.core.pipeline import plan_batches
        store = self.session.store
        blocks = store.list_blocks(ch["id"])
        if not blocks:
            return [hint("分批模式需要细纲：请先到「大纲」页生成本章细纲，"
                         "或用下方「更多 → 粘贴细纲」导入。")]
        rows = store.list_prose_batches(ch["id"])
        total = len(plan_batches(blocks,
                                 store.get_setting_int("prose_batch_size", 3)))
        n = len(rows)
        if n == 0:
            btn_text = "开始分批写作"
        elif n >= total:
            btn_text = "合并批次写入正文"
        else:
            btn_text = f"继续第 {n + 1} 批"
        controls = [ft.Row([
            ft.FilledButton(btn_text, icon=ft.Icons.AUTO_AWESOME,
                            on_click=lambda e: self._generate_next_batch(ch)),
            ft.TextButton("停止", on_click=self._prose_stop,
                          disabled=self._task is None or self._task.stopped),
            ft.TextButton("清空分批", on_click=lambda e: self._clear_batches(ch),
                          disabled=not rows),
        ], spacing=6, wrap=True)]
        controls.append(ft.Dropdown(
            label="每批细纲块数", dense=True,
            value=str(store.get_setting_int("prose_batch_size", 3)),
            options=[ft.dropdown.Option(str(i)) for i in range(1, 9)],
            on_select=self._on_batch_size_change))
        for i, r in enumerate(rows, start=1):
            controls.append(self._batch_card(ch, r, i))
        controls += self._desk_editor_block(ch, whole=False)
        controls.append(hint(
            "分批接力写作：每批按细纲块分组生成，滚动状态包自动衔接下一批；"
            "点下方细纲块可单独生成/编辑该块正文（自由写作，不进批次）。"))
        for i, b in enumerate(blocks, start=1):
            controls.append(self._outline_block_card(ch, b, i))
        return controls

    def _on_batch_size_change(self, e):
        raw = str(getattr(e.control, "value", "") or "").strip()
        size = int(raw) if raw.isdigit() and 1 <= int(raw) <= 8 else 3
        self.session.store.set_setting("prose_batch_size", str(size))
        toast(self.page, f"每批 {size} 个细纲块（对下一批生效）")
        self.refresh()

    def _batch_card(self, ch: dict, row: dict, idx: int) -> ft.Control:
        """批次卡片：🧩 第 N 批 · 字数 · 覆盖块数，点开只读预览。"""
        store = self.session.store
        text = row.get("content") or ""
        try:
            nb = len(store.prose_batch_block_ids(row))
        except Exception:
            nb = 0
        return ft.Card(ft.Container(ft.Column([
            ft.Row([
                ft.Text(f"🧩 第 {idx} 批", size=12, weight=ft.FontWeight.W_600),
                ft.Text(f"{len(text)} 字 · 覆盖 {nb} 个细纲块", size=10,
                        color=ft.Colors.ON_SURFACE_VARIANT, expand=True),
                ft.Container(ft.Text("已生成", size=10),
                             bgcolor=ft.Colors.with_opacity(0.14, ft.Colors.GREEN),
                             border_radius=6,
                             padding=ft.Padding(left=6, right=6, top=2, bottom=2)),
            ], spacing=6),
            ft.Text(text.replace("\n", " ")[:60], size=11, max_lines=2,
                    overflow=ft.TextOverflow.ELLIPSIS,
                    color=ft.Colors.ON_SURFACE_VARIANT),
        ], spacing=4),
            padding=8,
            on_click=lambda e: self._batch_sheet(ch, row, idx),
            ink=True))

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
        """细纲块卡片（施工单视角）：显示块状态与概述，点开单块工作台。"""
        st = blk.get("status") or "未生成"
        color = (ft.Colors.GREEN if st in ("已确认", "已锁定")
                 else ft.Colors.BLUE if st == "写作中" else ft.Colors.GREY)
        target = blk.get("target_words") or 0
        preview = (blk.get("content") or "（空细纲）").replace("\n", " ")
        return ft.Card(ft.Container(ft.Column([
            ft.Row([
                ft.Text(f"块{idx}", size=12, weight=ft.FontWeight.W_600),
                ft.Text(f"目标 {target} 字" if target else "", size=10,
                        color=ft.Colors.ON_SURFACE_VARIANT, expand=True),
                ft.Container(ft.Text(st, size=10),
                             bgcolor=ft.Colors.with_opacity(0.14, color),
                             border_radius=6,
                             padding=ft.Padding(left=6, right=6, top=2, bottom=2)),
            ], spacing=6),
            ft.Text(preview[:60], size=11, max_lines=2,
                    overflow=ft.TextOverflow.ELLIPSIS,
                    color=ft.Colors.ON_SURFACE_VARIANT),
        ], spacing=4),
            padding=8,
            on_click=lambda e: self._block_sheet(ch, blk, idx),
            ink=True))

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
        rows = store.list_prose_batches(cid)
        if any(any(i not in valid_ids for i in store.prose_batch_block_ids(r))
               for r in rows):
            store.clear_prose_batches(cid)
            rows = []
            toast(self.page, "细纲已变化，旧批次已清空，本次从第 1 批重新开始")
        batches = plan_batches(blocks, store.get_setting_int("prose_batch_size", 3))
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
            # 重写整章时不能拿本章旧正文当「前文结尾」（与桌面同口径）
            context["previous_end_override"] = Assembler(store).prev_chapter_end(cid)
        else:
            task = render_task("prose_batch_next", store, batch_no=next_idx, **common)
            context["batch_state"] = store.prose_batch_state_package(cid)
            context["previous_end_override"] = (
                "【前块结尾（衔接）：这是上一批正文的最后两段，请紧接其后续写本批，"
                "不要重复这段内容】\n" + batch_tail_two_paras(rows[-1].get("content") or ""))
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
        store = self.session.store
        chapters = store.list_chapters()
        for c in chapters:
            if c["id"] == self._desk_chapter_id:
                return store.get_chapter(c["id"])
        self._desk_chapter_id = chapters[0]["id"]
        return store.get_chapter(chapters[0]["id"])

    def _pick_desk_ch(self, ch: dict):
        self._desk_chapter_id = ch["id"]
        self.refresh()

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
        store = self.session.store
        ch = self._current_desk_chapter()
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
        self._task = None
        prose_holder = {"text": ""}

        def on_started(tk):
            self._task = tk
            self._set_editor("")

        def on_text(buffer: str):
            prose_holder["text"] = buffer
            ed = getattr(self, "_desk_editor", None)
            if ed is not None:
                ed.value = buffer

        def on_done(result):
            self._task = None
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

    def _more_row(self, ch: dict) -> ft.Control:
        return ft.Row([
            ft.TextButton("检查本章", icon=ft.Icons.FACT_CHECK_OUTLINED,
                          on_click=self._check_chapter),
            ft.TextButton("文风审校", icon=ft.Icons.RATE_REVIEW_OUTLINED,
                          on_click=self._style_check),
            ft.TextButton("更多", icon=ft.Icons.MORE_HORIZ,
                          on_click=lambda e: self._more_sheet(ch)),
        ], spacing=2, wrap=True)

    def _more_sheet(self, ch: dict):
        entries = [
            ("notes", ft.Icons.STICKY_NOTE_2_OUTLINED, "本章备注",
             "记几条本章动态/提醒，写作时作为上下文注入"),
            ("snap", ft.Icons.HISTORY, "版本快照 / 回滚",
             "每次落库自动存旧版，可随时回看与回滚"),
            ("style", ft.Icons.BRUSH_OUTLINED, "风格样本",
             "把本章某段正文标记为风格样本（注入写作与改写）"),
            ("detail", ft.Icons.CONTENT_PASTE_GO_OUTLINED, "粘贴细纲…",
             "直接粘贴细纲文本入库（只剥不改，跳过 AI）"),
            ("prose", ft.Icons.CONTENT_PASTE_GO_OUTLINED, "粘贴正文…",
             "直接粘贴正文本入库（单章或多章，只剥不改）"),
            ("rewrite", ft.Icons.TUNE, "改写设置（注入模块）",
             "选择改写时注入哪些上下文模块"),
        ]

        def _go(name, e):
            self.page.pop_dialog()
            if name == "notes":
                self._notes_sheet(ch)
            elif name == "snap":
                self._snapshot_sheet(ch)
            elif name == "style":
                self._style_sheet(ch)
            elif name == "detail":
                self._paste_dialog(ch, "detail")
            elif name == "prose":
                self._paste_dialog(ch, "prose")
            else:
                self._rewrite_settings_sheet()

        rows = [ft.ListTile(leading=ft.Icon(i), title=ft.Text(t, size=14),
                            subtitle=ft.Text(s, size=11), dense=True,
                            on_click=lambda e, n=n: _go(n, e))
                for n, i, t, s in entries]
        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column(
                [ft.Text("写作台 · 更多", size=16, weight=ft.FontWeight.BOLD)] + rows,
                scroll=ft.ScrollMode.AUTO), padding=16, height=470),
            show_drag_handle=True))

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

    # ---------- 改写（role=rewrite，注入模块白名单） ----------

    def _rewrite_sheet(self, ch: dict):
        store = self.session.store
        cid = ch["id"]
        ed = getattr(self, "_desk_editor", None)
        text = (ed.value if ed else "") or self._accepted_prose(cid)
        text = (text or "").strip()
        if not text:
            toast(self.page, "没有可改写的内容", error=True)
            return
        default_req = (store.get_setting("rewrite_default_req", "") or "").strip()
        presets = [ln.strip() for ln in
                   (store.get_setting("rewrite_presets", "") or "").splitlines()
                   if ln.strip()]
        ask = (store.get_setting("rewrite_ask_req", "1") or "1") == "1"
        if not ask:
            # 设置里关了「每次询问」→ 直接用默认要求执行
            self._do_rewrite(ch, text, default_req)
            return
        field = ft.TextField(label="改写要求", value=default_req, multiline=True,
                             min_lines=2, max_lines=4,
                             hint_text="留空则用兜底指令：" + REQ_FALLBACK)

        def _set(v):
            field.value = v
            self.page.update()

        def _ok(ev):
            self.page.pop_dialog()
            self._do_rewrite(ch, text, (field.value or "").strip())

        def _cancel(ev):
            self.page.pop_dialog()
        chips = ft.Row([ft.OutlinedButton(p[:16], on_click=lambda e, p=p: _set(p))
                        for p in presets[:6]], spacing=4, wrap=True) if presets \
            else ft.Container(height=0)
        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text(f"改写（{len(text)} 字）"),
            content=ft.Container(ft.Column([
                hint("改写只动文字表达，不改剧情事实；可到「更多 → 改写设置」调整注入模块。"),
                chips,
                field,
            ], tight=True, spacing=8, scroll=ft.ScrollMode.AUTO),
                width=460, height=340),
            actions=[ft.TextButton("取消", on_click=_cancel),
                     ft.FilledButton("开始改写", on_click=_ok)],
            actions_alignment=ft.MainAxisAlignment.END))

    def _do_rewrite(self, ch: dict, text: str, req: str):
        from app.core.task_templates import render as render_task
        store = self.session.store
        effective = req or REQ_FALLBACK
        task = render_task("rewrite", store, req=effective)
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
            from app.core.pipeline import strip_prose_preamble
            from app.core.textclean import strip_prose_residue
            new_text = strip_prose_residue(
                strip_prose_preamble(result.get("text") or ""))
            if not new_text.strip():
                toast(self.page, "改写结果为空", error=True)
                return
            self._apply_diag(result, "rewrite", new_text)
            self._pick_rewrite_version(ch, text, new_text)

        start_generation(self.page, self.session, role="rewrite",
                         context={"chapter": ch, "rewrite_text": text,
                                  "character_names": ch.get("characters") or [],
                                  "user_req": effective},
                         task=task, title="改写", on_done=on_done,
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
                        ft.FilledButton("采用所选", on_click=_ok)],
                       alignment=ft.MainAxisAlignment.END, spacing=8),
            ], spacing=6, scroll=ft.ScrollMode.AUTO), padding=16, height=600),
            show_drag_handle=True))

    def _apply_rewrite_result(self, ch: dict, text: str):
        """改写结果回填编辑器（改写永远整篇一次过，两种写法同口径）。

        改写来源标记：接受后即使已有快照也要重归档（与桌面「改写例外」同口径）。
        """
        self._pending_rewrite.add(ch["id"])
        self._set_editor(text)
        toast(self.page, f"改写完成（{len(text)} 字），检查后点「接受本章」落库")
        self.refresh()

    def _rewrite_settings_sheet(self):
        from app.core.assembler import (REWRITE_DEFAULT_MODULES, REWRITE_MODULES,
                                        rewrite_modules)
        store = self.session.store
        cur = rewrite_modules(store)
        boxes: dict = {}
        rows = []
        for kind, label in REWRITE_MODULES.items():
            cb = ft.Checkbox(label=label, value=(kind in cur))
            boxes[kind] = cb
            rows.append(cb)

        def _save(ev):
            picked = [k for k, cb in boxes.items() if cb.value]
            store.set_setting("rewrite_modules", ",".join(picked))
            self.page.pop_dialog()
            toast(self.page, f"已保存（注入 {len(picked)} 个模块）" if picked
                  else "已保存：改写时不注入可选模块")

        def _reset(ev):
            keep = set(REWRITE_DEFAULT_MODULES.split(","))
            for k, cb in boxes.items():
                cb.value = k in keep
            self.page.update()
        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column([
                ft.Text("改写注入模块", size=16, weight=ft.FontWeight.BOLD),
                hint("勾选的模块会在改写时作为上下文注入。待改写文本、改写要求、系统身份、"
                     "破甲、锁定项始终注入，不在此列表。"),
                ft.Column(rows, scroll=ft.ScrollMode.AUTO, height=420),
                ft.Row([ft.TextButton("恢复默认", on_click=_reset),
                        ft.FilledButton("保存", on_click=_save)],
                       alignment=ft.MainAxisAlignment.END, spacing=8),
            ], spacing=8), padding=16, height=580),
            show_drag_handle=True))
