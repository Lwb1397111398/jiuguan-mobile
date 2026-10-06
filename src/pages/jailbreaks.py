# -*- coding: utf-8 -*-
"""破甲库（手机版）：破甲档案的完整管理。

对齐桌面「模型页 → 破甲库」的能力：启用开关 / 强度档位 / 当前生效切换 /
新建 / 编辑（名称 + 投放模式 + 锚定·预确认·推杆·预填充四段，每段可填内置示例）/
查看组装结果 / 删除 / 恢复内置。

破甲属于工具级配置（一个小说一个项目，破甲与书无关），因此读写全局库
（app.gstore），不需要打开项目即可管理。
"""
import flet as ft

from widgets import section, hint, empty, toast, confirm


def _jb_api():
    """惰性取破甲内置模块的 API（避免模块导入期就拉起 core）。"""
    from app.core import jailbreak_builtin as jb
    return jb


class JailbreakLibrary:
    """破甲库页面（由「我的」页进入）。"""

    def __init__(self, page, session, on_back=None):
        self.page = page
        self.session = session
        self.on_back = on_back      # 返回「我的」页
        self.on_refresh = None      # 由 MinePage/Main 注入：整页重建

    # ---------- 基础 ----------

    @property
    def gstore(self):
        return self.session.app.gstore

    def refresh(self):
        if self.on_refresh:
            self.on_refresh()
        else:
            try:
                self.page.update()
            except Exception:
                pass

    def _height(self, ratio=0.86, fallback=640):
        h = getattr(self.page, "height", None)
        try:
            h = int(h)
        except (TypeError, ValueError):
            h = 0
        return max(320, int((h or fallback) * ratio))

    @staticmethod
    def _summary(text: str) -> str:
        jb = _jb_api()
        mode, head, prelude, tail, prefill, order = jb.parse_jailbreak_full(text or "")
        segs = [n for n, v in (("锚定", head), ("预确认", prelude), ("条款", order),
                              ("推杆", tail), ("预填充", prefill)) if (v or "").strip()]
        label = jb.JB_MODE_LABELS.get(mode, mode)
        return f"{label} · {'＋'.join(segs) if segs else '空破甲'} · {len(text or '')} 字"

    @staticmethod
    def _examples():
        """内置五段示例：(锚定, 预确认, 条款, 推杆, 预填充)。"""
        jb = _jb_api()
        head, _, _ = jb.split_jailbreak(jb.BUILTIN_JAILBREAK)
        return (head, jb.format_prelude(jb.PRELUDE_DEFAULT), jb.ORDER_EXAMPLE,
                jb.PUSHER_FULL, jb.PREFILL_EXAMPLE)

    # =====================================================
    # 页面
    # =====================================================

    def build(self) -> ft.Control:
        g = self.gstore
        try:
            profiles = g.list_jailbreak_profiles()
            active = g.get_active_jailbreak_name() or ""
            enabled = g.get_jailbreak_enabled()
            strength = g.get_jailbreak_strength()
        except Exception as e:
            return ft.Container(
                ft.Column([self._head(),
                           ft.Text(f"读取破甲库失败：{e}", size=13,
                                   color=ft.Colors.RED_300)]),
                padding=12, expand=True)

        rows = []
        for p in profiles:
            rows.append(self._row(p, active))

        body = [
            self._head(),
            self._status_card(enabled, strength, active, len(profiles)),
            ft.Row([
                ft.FilledButton("＋ 新建破甲", icon=ft.Icons.ADD,
                                on_click=lambda e: self._edit_new()),
                ft.OutlinedButton("恢复内置破限", icon=ft.Icons.RESTORE,
                                  on_click=self._restore_builtin),
            ], spacing=8, wrap=True),
            section(f"破甲库（{len(profiles)}）"),
            hint("破甲按五段注入：锚定（system 末）＋预确认（伪造对话对）＋推杆（任务末）"
                 "＋条款（任务后正向条款）＋预填充（最后一条 assistant，可选）。"
                 "点条目可切换生效、编辑或删除。"),
            *(rows if rows else [empty("破甲库还是空的\n点「＋ 新建破甲」，"
                                       "或从酒馆预设导入（电脑版），或恢复内置破限")]),
        ]
        return ft.Container(
            ft.Column(body, scroll=ft.ScrollMode.AUTO, expand=True, spacing=6),
            padding=14, expand=True)

    def _head(self) -> ft.Control:
        return ft.Row([
            ft.IconButton(ft.Icons.ARROW_BACK, on_click=lambda e: (
                self.on_back() if self.on_back else None)),
            ft.Text("破甲库", size=20, weight=ft.FontWeight.BOLD, expand=True),
        ])

    def _status_card(self, enabled: bool, strength: str, active: str,
                     count: int) -> ft.Control:
        strength_dd = ft.Dropdown(
            label="强度档位",
            value=strength,
            dense=True,
            options=[
                ft.dropdown.Option(key="standard",
                                   text="标准（锚定＋推杆全套）"),
                ft.dropdown.Option(key="light",
                                   text="轻量（只保留推杆发令枪）"),
            ],
            on_select=self._on_strength,
        )
        try:
            guard = self.gstore.get_jailbreak_completion_guard()
        except Exception:
            guard = True
        return ft.Card(ft.Container(ft.Column([
            ft.Row([
                ft.Icon(ft.Icons.SHIELD, color=ft.Colors.PRIMARY, size=20),
                ft.Text("启用破甲注入", size=14, weight=ft.FontWeight.W_600,
                        expand=True),
                ft.Switch(value=bool(enabled), on_change=self._on_enabled),
            ]),
            ft.Text(f"当前生效：{active or '（无）'}",
                    size=13, weight=ft.FontWeight.W_600,
                    color=ft.Colors.PRIMARY if active else ft.Colors.ON_SURFACE_VARIANT),
            ft.Text("创作系任务注入全套；检查/文风审校只注入「引用许可」；"
                    "记忆提取不注入（纯机械 JSON，避免干扰纪律）。",
                    size=11, color=ft.Colors.ON_SURFACE_VARIANT),
            strength_dd,
            ft.Row([
                ft.Text("收尾护栏", size=13, expand=True),
                ft.Switch(value=bool(guard), on_change=self._on_guard),
            ]),
            ft.Text("Gemini 系报文末尾自动补「assistant 起手＋user 收尾」"
                    "（与酒馆成功报文同形态）。拒收 assistant 结尾的渠道可关。",
                    size=11, color=ft.Colors.ON_SURFACE_VARIANT),
            hint(f"破甲库存放于应用数据目录 jailbreaks/，随全量备份与电脑版互通。"
                 f"共 {count} 套。"),
        ], spacing=8), padding=14))

    def _row(self, prof: dict, active_name: str) -> ft.Control:
        name = prof.get("name") or ""
        is_active = bool(prof.get("active")) or (name == active_name)
        content = prof.get("content") or ""
        title_row = [ft.Text(name, size=14, weight=ft.FontWeight.W_600,
                             expand=True)]
        if is_active:
            title_row.append(ft.Container(
                ft.Text("当前生效", size=10, color=ft.Colors.WHITE),
                bgcolor=ft.Colors.PRIMARY, border_radius=6,
                padding=ft.Padding(left=6, right=6, top=2, bottom=2)))
        return ft.Card(ft.Container(
            ft.Column([
                ft.Row(title_row, spacing=6),
                ft.Text(self._summary(content), size=11,
                        color=ft.Colors.ON_SURFACE_VARIANT),
            ], spacing=3),
            padding=12,
            on_click=lambda e, p=prof: self._actions(p),
            ink=True,
        ), margin=ft.Margin(left=0, right=0, top=0, bottom=6))

    # =====================================================
    # 开关 / 档位 / 切换
    # =====================================================

    def _on_enabled(self, e):
        on = bool(e.control.value)
        try:
            self.gstore.set_jailbreak_enabled(on)
        except Exception as ex:
            toast(self.page, f"设置失败：{ex}", error=True)
            return
        toast(self.page, f"已{'启用' if on else '关闭'}破甲注入")
        if on and not (self.gstore.get_jailbreak_prompt() or "").strip():
            toast(self.page, "当前破甲为空，请新建或恢复内置破限", error=True)
        self.refresh()

    def _on_strength(self, e):
        value = e.control.value or "standard"
        try:
            self.gstore.set_jailbreak_strength(value)
        except Exception as ex:
            toast(self.page, f"设置失败：{ex}", error=True)
            return
        toast(self.page, "破甲强度已切换为「标准（锚定＋推杆全套）」"
              if value == "standard" else
              "破甲强度已切换为「轻量（只保留推杆发令枪）」")
        self.refresh()

    def _on_guard(self, e):
        on = bool(e.control.value)
        try:
            self.gstore.set_jailbreak_completion_guard(on)
        except Exception as ex:
            toast(self.page, f"设置失败：{ex}", error=True)
            return
        toast(self.page, f"收尾护栏已{'开启' if on else '关闭'}"
              + ("（Gemini 系报文末尾自动补 assistant 起手＋user 收尾）" if on
                 else "（报文不再自动补尾，适合拒收 assistant 结尾的渠道）"))
        self.refresh()

    def _set_active(self, name: str):
        try:
            self.gstore.set_active_jailbreak(name)
            self.gstore.set_jailbreak_enabled(True)
        except Exception as ex:
            toast(self.page, f"切换失败：{ex}", error=True)
            return
        toast(self.page, f"已切换破甲为「{name}」（对「跟随全局」的档案立即生效）")
        self.refresh()

    # =====================================================
    # 条目操作
    # =====================================================

    def _actions(self, prof: dict):
        name = prof.get("name") or ""
        content = prof.get("content") or ""
        open_cfg = (self.gstore.get_jailbreak_prompt() or "").strip()
        rows = [
            ft.ListTile(leading=ft.Icon(ft.Icons.CHECK_CIRCLE_OUTLINE),
                        title=ft.Text("设为当前生效"),
                        subtitle=ft.Text("对「跟随全局」的模型档案立即生效", size=11),
                        on_click=lambda e: (self.page.pop_dialog(),
                                            self._set_active(name))),
            ft.ListTile(leading=ft.Icon(ft.Icons.EDIT),
                        title=ft.Text("编辑（名称 / 投放模式 / 四段内容）"),
                        on_click=lambda e: (self.page.pop_dialog(),
                                            self._edit_existing(name, content))),
            ft.ListTile(leading=ft.Icon(ft.Icons.VISIBILITY),
                        title=ft.Text("查看组装后的破甲全文"),
                        on_click=lambda e: (self.page.pop_dialog(),
                                            self._view(name, content))),
            ft.ListTile(
                leading=ft.Icon(ft.Icons.DELETE_OUTLINE, color=ft.Colors.RED_400),
                title=ft.Text("删除这套破甲", color=ft.Colors.RED_400),
                on_click=lambda e: (self.page.pop_dialog(),
                                    self._delete(name))),
        ]
        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column(
                [ft.Text(f"破甲：{name}", size=15, weight=ft.FontWeight.BOLD),
                 ft.Text(self._summary(content), size=11,
                         color=ft.Colors.ON_SURFACE_VARIANT),
                 *rows], scroll=ft.ScrollMode.AUTO, spacing=0),
                padding=16, height=min(460, self._height(0.72))),
            show_drag_handle=True))

    def _view(self, name: str, content: str):
        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column([
                ft.Text(f"破甲全文：{name}", size=15, weight=ft.FontWeight.BOLD),
                hint("含 ---PRELUDE--- / ---TAIL--- / ---PREFILL--- 分段标记与模式声明行。"),
                ft.Container(ft.TextField(
                    value=content, multiline=True, read_only=True,
                    border_color=ft.Colors.TRANSPARENT, text_size=11),
                    height=self._height(0.66)),
                ft.Row([ft.TextButton("关闭",
                                      on_click=lambda e: self.page.pop_dialog())],
                       alignment=ft.MainAxisAlignment.END),
            ], spacing=6), padding=16, height=self._height(0.88)),
            show_drag_handle=True))

    def _delete(self, name: str):
        def _yes():
            try:
                self.gstore.delete_jailbreak_profile(name)
            except Exception as ex:
                toast(self.page, f"删除失败：{ex}", error=True)
                return
            toast(self.page, f"已删除破甲「{name}」（指定它的档案自动回退跟随全局）")
            self.refresh()

        confirm(self.page, "删除破甲",
                f"确定删除破甲「{name}」？删除后不可恢复。", _yes,
                yes_text="删除")

    def _restore_builtin(self, e):
        jb = _jb_api()
        name, text = jb.BUILTIN_JAILBREAK_NAME, jb.BUILTIN_JAILBREAK
        try:
            exists = any((p.get("name") == name)
                         for p in self.gstore.list_jailbreak_profiles())
        except Exception:
            exists = False

        def _yes():
            try:
                self.gstore.save_jailbreak_profile(name, text)
                self.gstore.set_active_jailbreak(name)
                self.gstore.set_jailbreak_enabled(True)
            except Exception as ex:
                toast(self.page, f"恢复失败：{ex}", error=True)
                return
            toast(self.page, f"已恢复内置破限「{name}」并设为当前破甲")
            self.refresh()

        confirm(self.page, "恢复内置破限",
                (f"破甲库中已有「{name}」，将覆盖为内置最新版并设为当前破甲，"
                 "你的修改会被覆盖。继续？" if exists else
                 f"把内置破限「{name}」写入破甲库并设为当前破甲？"),
                _yes, yes_text="恢复")

    # =====================================================
    # 新建 / 编辑
    # =====================================================

    def _edit_new(self):
        """新建：默认带内置三段示例，打开就能照着改。"""
        jb = _jb_api()
        self._edit_dialog(jb.BUILTIN_JAILBREAK, "", old_name=None)

    def _edit_existing(self, name: str, content: str):
        self._edit_dialog(content or "", name, old_name=name)

    def _edit_dialog(self, text: str, name: str, old_name):
        jb = _jb_api()
        mode, head, prelude, tail, prefill, order = jb.parse_jailbreak_full(text or "")
        head_ex, prelude_ex, order_ex, tail_ex, prefill_ex = self._examples()

        name_f = ft.TextField(label="破甲名称", value=name, dense=True)
        mode_dd = ft.Dropdown(
            label="投放模式", value=mode, dense=True,
            options=[
                ft.dropdown.Option(key=jb.JB_MODE_LAYERED,
                                   text=jb.JB_MODE_LABELS[jb.JB_MODE_LAYERED]),
                ft.dropdown.Option(key=jb.JB_MODE_RUNTIME,
                                   text=jb.JB_MODE_LABELS[jb.JB_MODE_RUNTIME]),
            ])
        head_f = ft.TextField(label="锚定层 / 运行时协议层（可留空）",
                              value=head, multiline=True, min_lines=3, max_lines=7)
        prelude_f = ft.TextField(label="预确认层（user: / assistant: 对话对，可留空）",
                                 value=prelude, multiline=True, min_lines=3, max_lines=7)
        tail_f = ft.TextField(label="推杆层（发令枪，可留空）",
                              value=tail, multiline=True, min_lines=2, max_lines=5)
        order_f = ft.TextField(label="条款层（创作执行条款，可留空）",
                               value=order, multiline=True, min_lines=2, max_lines=7)
        prefill_f = ft.TextField(label="预填充层（最后一条 assistant，可留空）",
                                 value=prefill, multiline=True, min_lines=2, max_lines=5)

        def _seg(title: str, tip: str, field: ft.TextField, example: str,
                 warning: str = "") -> ft.Control:
            def _fill(ev):
                field.value = example
                self.page.update()

            return ft.Container(ft.Column([
                ft.Row([
                    ft.Text(title, size=13, weight=ft.FontWeight.W_600, expand=True),
                    ft.TextButton("填示例", icon=ft.Icons.AUTO_FIX_HIGH,
                                  on_click=_fill),
                ], spacing=4),
                ft.Text(tip, size=11, color=ft.Colors.ON_SURFACE_VARIANT),
                *( [ft.Text(warning, size=11, color=ft.Colors.ORANGE_700)] if warning else [] ),
                field,
            ], spacing=4), margin=ft.Margin(left=0, right=0, top=4, bottom=8))

        form = ft.Column([
            name_f,
            mode_dd,
            ft.Text("破甲分五段注入，每段都可留空（全空 = 不注入）。点「填示例」填入内置参考写法。",
                    size=11, color=ft.Colors.ON_SURFACE_VARIANT),
            _seg("① 锚定层", "注入最后一条 system 消息，负责身份定性（建议三五行）",
                 head_f, head_ex),
            _seg("② 预确认层", "user:/assistant: 一行一个对话对，让模型先「亲口确认」接受任务",
                 prelude_f, prelude_ex),
            _seg("③ 推杆层", "追加到任务指令末尾（发令枪，紧贴生成点）", tail_f, tail_ex),
            _seg("④ 条款层", "创作执行条款（防重复/认知限制等），发送时排在任务之后、发令枪之前",
                 order_f, order_ex),
            _seg("⑤ 预填充层", "作为请求最后一条 assistant 消息预填开头，让模型接着往下写"
                              "（只对部分模型有效，留空则不产生这条消息）",
                 prefill_f, prefill_ex),
        ], scroll=ft.ScrollMode.AUTO, spacing=2)

        def _cancel(ev):
            self.page.pop_dialog()

        def _commit(final_name: str, body: str):
            try:
                if old_name and final_name != old_name:
                    safe = self.gstore.save_jailbreak_profile(final_name, body)
                    moved = self.gstore.rename_profile_jailbreak(old_name, safe)
                    self.gstore.delete_jailbreak_profile(old_name)
                    self.gstore.set_active_jailbreak(safe)
                    note = f"（{moved} 个档案已改指向新名）" if moved else ""
                    toast(self.page, f"已改名保存为「{safe}」{note}")
                else:
                    safe = self.gstore.save_jailbreak_profile(final_name, body)
                    if not old_name:
                        self.gstore.set_active_jailbreak(safe)
                        self.gstore.set_jailbreak_enabled(True)
                        toast(self.page, f"已新建并启用破甲「{safe}」")
                    else:
                        toast(self.page, "破甲已保存")
            except Exception as ex:
                toast(self.page, f"保存失败：{ex}", error=True)
                return
            self.refresh()

        def _do_save():
            final_name = (name_f.value or "").strip()
            if not final_name:
                toast(self.page, "请填写破甲名称", error=True)
                return
            body = jb.build_jailbreak_text(head_f.value or "", prelude_f.value or "",
                                          tail_f.value or "", mode_dd.value,
                                          prefill_f.value or "",
                                          order_f.value or "")
            pre = (prelude_f.value or "").strip()
            if pre and not jb.parse_prelude(pre):
                self.page.pop_dialog()

                def _anyway():
                    _commit(final_name, body)

                confirm(self.page, "预确认段格式提醒",
                        "预确认段没有按 user:/assistant: 对话对写，这一段不会被注入成"
                        "认账对话。仍然保存？", _anyway, yes_text="仍然保存")
                return
            # 同名覆盖 / 改名覆盖都要先问一声（避免静默覆盖别人的破甲）
            try:
                others = [p.get("name") for p in self.gstore.list_jailbreak_profiles()
                          if p.get("name") != old_name]
            except Exception:
                others = []
            if final_name in others:
                self.page.pop_dialog()

                def _overwrite():
                    _commit(final_name, body)

                confirm(self.page, "覆盖确认",
                        f"破甲库中已有「{final_name}」，保存将覆盖它的内容。继续？",
                        _overwrite, yes_text="覆盖")
                return
            self._save_and_close(final_name, body, _commit)

        self.page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("编辑破甲" if old_name else "新建破甲"),
            content=ft.Container(ft.Column([
                form,
                ft.Row([
                    ft.TextButton("取消", on_click=_cancel),
                    ft.FilledButton("保存", on_click=lambda e: _do_save()),
                ], alignment=ft.MainAxisAlignment.END, spacing=8),
            ], spacing=6, scroll=ft.ScrollMode.AUTO),
                width=min(560, int(getattr(self.page, "width", 560) or 560)),
                height=self._height(0.82)),
            actions=[],
            actions_alignment=ft.MainAxisAlignment.END,
        ))

    def _save_and_close(self, name: str, body: str, commit):
        """无冲突时：关掉编辑框后落库。"""
        self.page.pop_dialog()
        commit(name, body)
