# -*- coding: utf-8 -*-
"""偏好卡片管理（手机版）：对齐桌面「偏好中心」三级卡片。

- `global` 全局偏好：所有阶段都注入；可置顶（紧跟破甲锚定）与限定注入阶段。
- `stage`  阶段偏好：绑定某个阶段（总纲/分卷/大纲/细纲/正文/改写/检查/文风审校）。
- `manual` 手动偏好：默认不启用，需要时在生成前手动勾选。

卡片属于全局配置（与具体小说无关），读写全局库（`app.gstore`），不需要打开项目。
展示顺序：全局档 pinned 优先 → sort_order → id；阶段档按创作流程序 → 优先级降序 → id。
"""
import flet as ft

from widgets import section, hint, empty, toast, confirm

# 阶段 key → 中文名（与 app/core/store.py 的 STAGE_NAMES 一致）
STAGE_NAMES = {
    "synopsis": "总纲", "volume": "分卷", "outline": "大纲", "detail": "细纲",
    "prose": "正文", "rewrite": "改写", "check": "检查", "style": "文风审校",
}
SCOPES = [("global", "全局偏好"), ("stage", "阶段偏好"), ("manual", "手动偏好")]
_STAGE_ORDER = list(STAGE_NAMES.keys())


class PrefsLibrary:
    """偏好卡片页（由「我的」页进入）。"""

    def __init__(self, page, session, on_back=None, on_open_templates=None):
        self.page = page
        self.session = session
        self.on_back = on_back
        self.on_refresh = None
        self.on_open_templates = on_open_templates   # 回调 (stage_key) → 打开任务模板页
        self.scope = "global"
        self._editor_open = None                     # 正在编辑的卡片 id（None=新建）

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

    # =====================================================
    # 页面
    # =====================================================

    def build(self) -> ft.Control:
        g = self.gstore
        try:
            cards = g.list_cards(scope=self.scope)
        except Exception as e:
            return ft.Container(
                ft.Column([self._head(),
                           ft.Text(f"读取偏好卡片失败：{e}", size=13,
                                   color=ft.Colors.RED_300)]),
                padding=12, expand=True)
        cards = self._sort_cards(cards)
        rows = [self._row(c) for c in cards]

        body = [
            self._head(),
            self._scope_bar(),
            ft.Row([
                ft.FilledButton("＋ 新建卡片", icon=ft.Icons.ADD,
                                on_click=lambda e: self._edit_new()),
                ft.OutlinedButton("任务模板…", icon=ft.Icons.DESCRIPTION,
                                  on_click=self._goto_templates),
            ], spacing=8, wrap=True),
            hint(self._scope_hint()),
            section(f"{dict(SCOPES)[self.scope]}（{len(cards)}）"),
            *(rows if rows else [empty("这一档还没有卡片\n点「＋ 新建卡片」添加")]),
        ]
        return ft.Container(
            ft.Column(body, scroll=ft.ScrollMode.AUTO, expand=True, spacing=6),
            padding=14, expand=True)

    def _head(self) -> ft.Control:
        return ft.Row([
            ft.IconButton(ft.Icons.ARROW_BACK, on_click=lambda e: (
                self.on_back() if self.on_back else None)),
            ft.Text("偏好卡片", size=20, weight=ft.FontWeight.BOLD, expand=True),
        ])

    def _scope_hint(self) -> str:
        if self.scope == "global":
            return ("全局卡片在所有阶段注入（按优先级排序）。可「置顶」（紧跟破甲锚定）"
                    "或限定生效阶段；未限定=全阶段。")
        if self.scope == "stage":
            return "阶段卡片只在该阶段注入；阶段卡片可关联「任务模板」做跨阶段定制。"
        return "手动卡片默认不启用，需要时在生成前手动勾选（避免误注入）。"

    def _scope_bar(self) -> ft.Control:
        btns = []
        for key, label in SCOPES:
            active = (self.scope == key)
            btns.append(ft.Container(
                ft.Text(label, size=13,
                        weight=ft.FontWeight.W_600 if active else ft.FontWeight.NORMAL,
                        color=ft.Colors.ON_PRIMARY_CONTAINER if active
                        else ft.Colors.ON_SURFACE),
                bgcolor=ft.Colors.PRIMARY_CONTAINER if active else None,
                border=ft.Border.all(1, ft.Colors.PRIMARY if active
                                     else ft.Colors.OUTLINE_VARIANT),
                border_radius=18,
                padding=ft.Padding(left=14, right=14, top=7, bottom=7),
                ink=True,
                on_click=lambda e, k=key: self._set_scope(k),
            ))
        return ft.Row(btns, spacing=8, wrap=True)

    def _sort_cards(self, cards: list[dict]) -> list[dict]:
        if self.scope == "stage":
            return sorted(cards, key=lambda c: (
                _STAGE_ORDER.index(c.get("stage") or "synopsis")
                if (c.get("stage") or "") in _STAGE_ORDER else len(_STAGE_ORDER),
                -int(c.get("priority") or 0), int(c.get("id") or 0)))
        return sorted(cards, key=lambda c: (
            -int(c.get("pinned") or 0), int(c.get("sort_order") or 0),
            int(c.get("id") or 0)))

    # ---------- 卡片行 ----------

    def _row(self, c: dict) -> ft.Control:
        cid = int(c.get("id") or 0)
        tags = [ft.Container(
            ft.Text(f"优先 {int(c.get('priority') or 0)}", size=10),
            bgcolor=ft.Colors.with_opacity(0.12, ft.Colors.BLUE),
            border_radius=6, padding=ft.Padding(left=6, right=6, top=2, bottom=2))]
        if c.get("pinned"):
            tags.append(ft.Container(
                ft.Text("置顶", size=10),
                bgcolor=ft.Colors.with_opacity(0.16, ft.Colors.ORANGE),
                border_radius=6, padding=ft.Padding(left=6, right=6, top=2, bottom=2)))
        if self.scope == "stage" and c.get("stage"):
            tags.append(ft.Container(
                ft.Text(STAGE_NAMES.get(c["stage"], c["stage"]), size=10),
                bgcolor=ft.Colors.with_opacity(0.12, ft.Colors.TEAL),
                border_radius=6, padding=ft.Padding(left=6, right=6, top=2, bottom=2)))
        stag = (c.get("stages") or "").strip()
        if self.scope == "global" and stag:
            names = "、".join(STAGE_NAMES.get(s, s) for s in stag.split(",") if s.strip())
            tags.append(ft.Container(
                ft.Text(f"仅 {names}", size=10),
                bgcolor=ft.Colors.with_opacity(0.12, ft.Colors.PURPLE),
                border_radius=6, padding=ft.Padding(left=6, right=6, top=2, bottom=2)))

        ops = [ft.IconButton(ft.Icons.EDIT, tooltip="编辑", icon_size=18,
                             on_click=lambda e, cc=c: self._edit_existing(cc)),
               ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="删除", icon_size=18,
                             on_click=lambda e, cc=c: self._delete(cc))]
        if self.scope == "global":
            ops.insert(0, ft.IconButton(
                ft.Icons.VERTICAL_ALIGN_TOP if not c.get("pinned") else ft.Icons.VERTICAL_ALIGN_BOTTOM,
                tooltip="取消置顶" if c.get("pinned") else "置顶（紧跟破甲）",
                icon_size=18, on_click=lambda e, cc=c: self._toggle_pinned(cc)))
            ops.insert(1, ft.IconButton(
                ft.Icons.FILTER_ALT, tooltip="限定注入阶段", icon_size=18,
                on_click=lambda e, cc=c: self._pick_stages(cc)))
        if self.scope == "stage":
            ops.insert(0, ft.IconButton(
                ft.Icons.DESCRIPTION, tooltip="任务模板", icon_size=18,
                on_click=lambda e, cc=c: self._open_stage_templates(cc)))

        return ft.Card(
            ft.Container(
                ft.Column([
                    ft.Row([
                        ft.Text(c.get("title") or "（无标题）", size=14,
                                weight=ft.FontWeight.W_600, expand=True),
                        ft.Switch(value=bool(c.get("enabled")), scale=0.85,
                                  on_change=lambda e, cc=c: self._toggle_enabled(cc, e)),
                    ]),
                    ft.Row(tags, spacing=6, wrap=True),
                    ft.Text(c.get("content") or "", size=11, max_lines=3,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                            overflow=ft.TextOverflow.ELLIPSIS),
                    ft.Row(ops, spacing=0,
                           alignment=ft.MainAxisAlignment.END),
                ], spacing=4),
                padding=10,
            ),
            elevation=1,
        )

    # ---------- 交互 ----------

    def _set_scope(self, key: str):
        if key == self.scope:
            return
        self.scope = key
        self.refresh()

    def _toggle_enabled(self, card: dict, e):
        try:
            self.gstore.update_card(int(card["id"]),
                                    {"enabled": bool(e.control.value)})
        except Exception as ex:
            toast(self.page, f"保存失败：{ex}", error=True)

    def _toggle_pinned(self, card: dict):
        try:
            self.gstore.update_card(int(card["id"]),
                                    {"pinned": not bool(card.get("pinned"))})
        except Exception as ex:
            toast(self.page, f"保存失败：{ex}", error=True)
            return
        self.refresh()

    def _delete(self, card: dict):
        def _yes():
            try:
                self.gstore.delete_card(int(card["id"]))
            except Exception as ex:
                toast(self.page, f"删除失败：{ex}", error=True)
                return
            toast(self.page, "已删除")
            self.refresh()

        confirm(self.page, "删除卡片",
                f"确定删除「{card.get('title') or '（无标题）'}」？删除后无法恢复。",
                _yes, yes_text="删除")

    # ---------- 注入阶段（global 的 stages 白名单） ----------

    def _pick_stages(self, card: dict):
        cur = [s for s in (card.get("stages") or "").split(",") if s.strip()]
        boxes = {}
        for k, name in STAGE_NAMES.items():
            boxes[k] = ft.Checkbox(label=name, value=(k in cur))

        def _save(ev):
            picked = [k for k, b in boxes.items() if b.value]
            try:
                self.gstore.update_card(int(card["id"]), {"stages": ",".join(picked)})
            except Exception as ex:
                toast(self.page, f"保存失败：{ex}", error=True)
                return
            self.page.pop_dialog()
            toast(self.page, "已更新注入阶段" if picked else "已改为全阶段注入")
            self.refresh()

        def _all(ev):
            for b in boxes.values():
                b.value = True
            try:
                self.page.update()
            except Exception:
                pass

        def _none(ev):
            for b in boxes.values():
                b.value = False
            try:
                self.page.update()
            except Exception:
                pass

        self.page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("限定注入阶段"),
            content=ft.Container(
                ft.Column([
                    ft.Text("勾选的阶段才注入这张全局卡片；全不勾 = 所有阶段都注入。",
                            size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                    *boxes.values(),
                ], spacing=2, scroll=ft.ScrollMode.AUTO),
                width=380, height=self._height(0.6, 420)),
            actions=[
                ft.TextButton("全不勾", on_click=_none),
                ft.TextButton("全勾", on_click=_all),
                ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton("保存", on_click=_save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        ))

    # ---------- 任务模板 ----------

    def _goto_templates(self, e):
        if self.on_open_templates:
            self.on_open_templates("")

    def _open_stage_templates(self, card: dict):
        if self.on_open_templates:
            self.on_open_templates(card.get("stage") or "")

    # ---------- 编辑 ----------

    def _edit_new(self):
        self._edit_dialog(None)

    def _edit_existing(self, card: dict):
        self._edit_dialog(card)

    def _edit_dialog(self, card: dict | None):
        is_new = card is None
        title_f = ft.TextField(label="标题", dense=True,
                               value=(card or {}).get("title") or "")
        content_f = ft.TextField(label="正文内容", multiline=True, min_lines=5,
                                 max_lines=12, text_size=12,
                                 value=(card or {}).get("content") or "")
        prio_f = ft.TextField(label="优先级（1-10，越大越先注入）", dense=True,
                              value=str((card or {}).get("priority") or 5))
        note_f = ft.TextField(label="备注（可选）", dense=True,
                              value=(card or {}).get("note") or "")
        enabled_sw = ft.Switch(label="启用此卡片",
                               value=bool((card or {}).get("enabled", True)))

        # 作用域（新建可改；编辑时也允许改，与桌面一致）
        cur_scope = (card or {}).get("scope") or self.scope
        scope_dd = ft.Dropdown(
            label="作用域", dense=True, value=cur_scope,
            options=[ft.dropdown.Option(k, v) for k, v in SCOPES],
        )
        stage_dd = ft.Dropdown(
            label="所属阶段", dense=True, value=(card or {}).get("stage") or "synopsis",
            options=[ft.dropdown.Option(k, v) for k, v in STAGE_NAMES.items()],
        )
        stage_wrap = ft.Container(stage_dd, visible=(cur_scope == "stage"))

        def _scope_change(e):
            stage_wrap.visible = (scope_dd.value == "stage")
            try:
                self.page.update()
            except Exception:
                pass

        scope_dd.on_select = _scope_change

        def _save(ev):
            title = (title_f.value or "").strip()
            if not title:
                toast(self.page, "请填写标题", error=True)
                return
            content = content_f.value or ""
            try:
                prio = int(prio_f.value or 5)
            except (TypeError, ValueError):
                prio = 5
            prio = max(1, min(10, prio))
            scope = scope_dd.value or "global"
            stage = (stage_dd.value or "") if scope == "stage" else ""
            enabled = bool(enabled_sw.value)
            if scope == "manual":
                # 手动卡默认不启用（桌面同款兜底）：避免误注入
                enabled = False
            try:
                if is_new:
                    self.gstore.add_card(title, content, scope=scope, stage=stage,
                                         priority=prio, enabled=enabled,
                                         note=(note_f.value or ""), stages="",
                                         pinned=False)
                else:
                    self.gstore.update_card(int(card["id"]), {
                        "title": title, "content": content, "note": (note_f.value or ""),
                        "priority": prio, "scope": scope, "stage": stage,
                        "enabled": enabled,
                    })
            except Exception as ex:
                toast(self.page, f"保存失败：{ex}", error=True)
                return
            self.page.pop_dialog()
            toast(self.page, "已保存")
            if scope != self.scope:
                self.scope = scope
            self.refresh()

        self.page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("新建卡片" if is_new else "编辑卡片"),
            content=ft.Container(
                ft.Column([
                    title_f, content_f,
                    ft.Row([ft.Container(prio_f, expand=True),
                            ft.Container(scope_dd, expand=True)], spacing=8),
                    stage_wrap,
                    note_f,
                    enabled_sw,
                    ft.Text("手动偏好保存后默认不启用（避免误注入）。",
                            size=11, color=ft.Colors.ON_SURFACE_VARIANT),
                ], spacing=8, scroll=ft.ScrollMode.AUTO),
                width=420, height=self._height(0.72, 520)),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton("保存", on_click=_save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        ))
