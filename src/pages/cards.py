# -*- coding: utf-8 -*-
"""偏好卡片管理（手机版）：对齐桌面「偏好中心」三级卡片。

- `global` 全局偏好：所有阶段都注入；可置顶（紧跟破甲锚定）与限定注入阶段。
- `stage`  阶段偏好：绑定某个阶段（总纲/分卷/大纲/细纲/正文/改写/检查/文风审校）。
- `manual` 手动偏好：默认不启用，需要时在生成前手动勾选。

卡片属于全局配置（与具体小说无关），读写全局库（`app.gstore`），不需要打开项目。
展示顺序：全局档 pinned 优先 → sort_order → id；阶段档按创作流程序 → 优先级降序 → id。
"""
import flet as ft

import theme as T
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
                                   color=T.danger())]),
                padding=T.GAP_L, expand=True)
        cards = self._sort_cards(cards)
        rows = [self._row(c) for c in cards]

        body = [
            self._head(),
            self._scope_bar(),
            # 平分整行宽度（原来两个按钮各按字宽排、右边空一大截）；
            # 上下留白靠 margin——本页 Column spacing=0，靠组件自带 margin 分隔。
            ft.Container(
                T.button_row(
                    T.primary("新建卡片", icon=ft.Icons.ADD,
                              on_click=lambda e: self._edit_new()),
                    T.tonal("任务模板", icon=ft.Icons.DESCRIPTION,
                            on_click=self._goto_templates)),
                margin=ft.Margin(left=0, right=0, top=T.GAP_M, bottom=T.GAP_S)),
            hint(self._scope_hint()),
            section(f"{dict(SCOPES)[self.scope]}（{len(cards)}）"),
            *(rows if rows else [empty("这一档还没有卡片\n点「＋ 新建卡片」添加")]),
        ]
        return ft.Container(
            ft.Column(body, scroll=ft.ScrollMode.AUTO, expand=True, spacing=0,
                      horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
            padding=T.GAP_L, expand=True)

    def _head(self) -> ft.Control:
        return T.back_head(
            "偏好卡片", lambda: (self.on_back() if self.on_back else None))

    def _scope_hint(self) -> str:
        if self.scope == "global":
            return ("全局卡片在所有阶段注入（按优先级排序）。可「置顶」（紧跟破甲锚定）"
                    "或限定生效阶段；未限定=全阶段。")
        if self.scope == "stage":
            return "阶段卡片只在该阶段注入；阶段卡片可关联「任务模板」做跨阶段定制。"
        return "手动卡片默认不启用，需要时在生成前手动勾选（避免误注入）。"

    def _scope_bar(self) -> ft.Control:
        btns = [T.chip(label, active=(self.scope == key),
                       on_click=lambda e, k=key: self._set_scope(k))
                for key, label in SCOPES]
        return ft.Row(btns, spacing=T.GAP_S, wrap=True)

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
        tags = [T.pill(f"优先 {int(c.get('priority') or 0)}")]
        if c.get("pinned"):
            tags.append(T.pill("置顶", T.warn(), opacity=0.16))
        if self.scope == "stage" and c.get("stage"):
            tags.append(T.pill(STAGE_NAMES.get(c["stage"], c["stage"]),
                               ft.Colors.TEAL))
        stag = (c.get("stages") or "").strip()
        if self.scope == "global" and stag:
            names = "、".join(STAGE_NAMES.get(s, s) for s in stag.split(",") if s.strip())
            tags.append(T.pill(f"仅 {names}", ft.Colors.PURPLE))

        ops = [ft.IconButton(ft.Icons.EDIT_OUTLINED, tooltip="编辑", icon_size=18,
                             on_click=lambda e, cc=c: self._edit_existing(cc)),
               ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="删除", icon_size=18,
                             icon_color=T.muted(),
                             on_click=lambda e, cc=c: self._delete(cc))]
        if self.scope == "global":
            ops.insert(0, ft.IconButton(
                ft.Icons.VERTICAL_ALIGN_TOP if not c.get("pinned") else ft.Icons.VERTICAL_ALIGN_BOTTOM,
                tooltip="取消置顶" if c.get("pinned") else "置顶（紧跟破甲）",
                icon_size=18, icon_color=T.muted(),
                on_click=lambda e, cc=c: self._toggle_pinned(cc)))
            ops.insert(1, ft.IconButton(
                ft.Icons.FILTER_ALT, tooltip="限定注入阶段", icon_size=18,
                icon_color=T.muted(),
                on_click=lambda e, cc=c: self._pick_stages(cc)))
        if self.scope == "stage":
            ops.insert(0, ft.IconButton(
                ft.Icons.DESCRIPTION, tooltip="任务模板", icon_size=18,
                icon_color=T.muted(),
                on_click=lambda e, cc=c: self._open_stage_templates(cc)))

        return T.card(ft.Column([
            ft.Row([
                ft.Text(c.get("title") or "（无标题）", size=14,
                        weight=ft.FontWeight.W_600, expand=True),
                ft.Switch(value=bool(c.get("enabled")), scale=0.8,
                          active_color=T.brand(),
                          on_change=lambda e, cc=c: self._toggle_enabled(cc, e)),
            ], spacing=T.GAP_XS, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row(tags, spacing=T.GAP_XS + 2, wrap=True),
            # 预览固定 2 行：原来 3 行时，短卡片显示 2 行、长卡片 3 行，
            # 一屏里卡片高低参差，看着像没对齐。
            ft.Text(c.get("content") or "", size=11, max_lines=2,
                    color=T.muted(), overflow=ft.TextOverflow.ELLIPSIS),
            ft.Row(ops, spacing=0, alignment=ft.MainAxisAlignment.END),
        ], spacing=T.GAP_XS + 2))

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
            shape=ft.RoundedRectangleBorder(radius=T.R_XL),
            title=ft.Text("限定注入阶段", size=17, weight=ft.FontWeight.W_600),
            content=ft.Container(
                ft.Column([
                    ft.Text("勾选的阶段才注入这张全局卡片；全不勾 = 所有阶段都注入。",
                            size=12, color=T.muted()),
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
        title_f = T.field("", (card or {}).get("title") or "", dense=True)
        content_f = T.field("", (card or {}).get("content") or "",
                            multiline=True, min_lines=5, max_lines=12, size=12)
        prio_f = T.field("", str((card or {}).get("priority") or 5), dense=True)
        note_f = T.field("", (card or {}).get("note") or "", dense=True)
        # 标签一律走 field_group（标签在框外）：弹层里原本一半字段用浮标签、
        # 一半用自绘小标签，同一个弹窗两种排法。
        enabled_row, enabled_sw = T.switch_row(
            "启用此卡片", bool((card or {}).get("enabled", True)))

        # 作用域（新建可改；编辑时也允许改，与桌面一致）
        # 下拉用裸控件 + 独立小标签：后面要读 .value / 改 .on_select。
        cur_scope = (card or {}).get("scope") or self.scope
        scope_dd = T.select("", cur_scope, SCOPES)
        stage_dd = T.select("", (card or {}).get("stage") or "synopsis",
                            list(STAGE_NAMES.items()))
        stage_wrap = ft.Container(T.field_group(stage_dd, "所属阶段"),
                                  visible=(cur_scope == "stage"))

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
            shape=ft.RoundedRectangleBorder(radius=T.R_XL),
            title=ft.Text("新建卡片" if is_new else "编辑卡片", size=17,
                          weight=ft.FontWeight.W_600),
            content=ft.Container(
                ft.Column([
                    T.field_group(title_f, "标题"),
                    T.field_group(content_f, "正文内容"),
                    ft.Row([ft.Container(
                                T.field_group(prio_f, "优先级（1-10）"),
                                expand=True),
                            ft.Container(
                                T.field_group(scope_dd, "作用域"),
                                expand=True)],
                           spacing=T.GAP_S),
                    stage_wrap,
                    T.field_group(note_f, "备注（可选）"),
                    enabled_row,
                    ft.Text("手动偏好保存后默认不启用（避免误注入）。",
                            size=11, color=T.muted()),
                ], spacing=T.GAP_M, scroll=ft.ScrollMode.AUTO),
                width=420, height=self._height(0.72, 520)),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton("保存", on_click=_save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        ))
