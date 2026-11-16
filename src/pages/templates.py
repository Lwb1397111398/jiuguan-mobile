# -*- coding: utf-8 -*-
"""任务模板编辑（手机版）：对齐桌面「偏好卡片 → 任务模板」与预设编辑弹窗。

任务模板属于全局配置（与具体小说无关），存在全局库 meta：`task_template_<key>`；
**留空 = 用内置默认**（`app/core/task_templates.TEMPLATES`）。
渲染占位符逐个替换（不做 str.format，模板里的 JSON 花括号安全），
支持中文别名：{章名}/{章节号}/{章数}/{卷名}/{卷内容}/{前大纲}。
"""
import flet as ft

import theme as T
from widgets import section, hint, toast, confirm


def _tpl_api():
    """惰性取模板模块（避免导入期拉起 core）。"""
    from app.core import task_templates as tt
    return tt


# 阶段 → 相关模板 key（第一个为主模板）。
# 与桌面 `app/ui/prefs_page.py` 的 STAGE_TPL_KEYS 保持一致 —— 改一处必须改两处。
STAGE_TPL_KEYS = {
    "synopsis": ["brainstorm", "synopsis_iter"],
    "volume": ["volume"],
    "outline": ["outline_chapter", "outline_single"],
    "detail": ["detail", "detail_block"],
    # 一次性主模板 prose_chapter_md；分批模板 prose_batch_first/next；
    # 按大纲直写分批 prose_outline_first/next（2026-10-17，与桌面同步）
    "prose": ["prose_chapter_md", "prose_batch_first", "prose_batch_next",
              "prose_outline_first", "prose_outline_next", "prose"],
    # 2026-10-07 旧 rewrite 模板删除（桌面同步），改写阶段卡指向整章差量模板
    "rewrite": ["prose_rewrite_whole"],
    "check": ["check", "check_selection"],
    "style": ["style"],
}


class TaskTemplateEditor:
    """任务模板编辑页：选模板 → 编辑 → 保存 / 恢复默认 / 清空覆盖。"""

    def __init__(self, page, session, on_back=None, keys=None, initial=None):
        self.page = page
        self.session = session
        self.on_back = on_back
        self.on_refresh = None
        self.keys = list(keys) if keys else None      # 限定可选范围（阶段入口）
        self.key = initial or (self.keys[0] if self.keys else "prose_chapter_md")
        if self.keys and self.key not in self.keys:
            self.key = self.keys[0]
        self._field = None
        self._info = None
        self._btn_save = None

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

    def _options(self):
        """下拉选项：限定 keys 时只列这些，否则列全部模板。"""
        tt = _tpl_api()
        keys = self.keys or list(tt.TEMPLATES.keys())
        return [ft.dropdown.Option(k, tt.LABELS.get(k, k)) for k in keys]

    def _current(self) -> str:
        """用户覆盖文本（空串 = 未覆盖）。"""
        try:
            return (self.gstore.get_task_template(self.key) or "")
        except Exception:
            return ""

    def _default(self) -> str:
        tt = _tpl_api()
        return tt.TEMPLATES.get(self.key, "")

    # =====================================================
    # 页面
    # =====================================================

    def build(self) -> ft.Control:
        tt = _tpl_api()
        cur = self._current()
        overridden = bool(cur.strip())

        # 编辑器要吃掉剩余高度：模板动辄几千字，塞进十来行里改一处要来回翻。
        # ⚠ Flet 0.86 的 TextField **不吃父级给的高度**——外层 Container(height=…)
        # 和 expand=True 两轮都试过，只把外面那层框撑开，输入框本身仍按行数渲染
        # （截图实测：框白高 300px 上下，容器再高也是透明的）。唯一有效的旋钮
        # 是 min_lines，所以按视口高度换算行数：15px 字 + 1.4 行距 ≈ 21px/行。
        page_h = 880
        try:
            page_h = int(getattr(self.page, "height", 0) or 880)
        except (TypeError, ValueError):
            pass
        lines = max(10, (page_h - 330) // 21)
        self._field = T.editor(cur, hint="留空则使用内置默认模板", expand=False,
                               min_lines=lines)

        self._info = ft.Text(
            ("已自定义（保存于全局库，所有项目共用）" if overridden
             else "当前用内置默认；填写内容并保存即覆盖"),
            size=12,
            color=T.brand() if overridden else T.muted(),
        )

        keys = self.keys or list(tt.TEMPLATES.keys())
        dd = T.select("任务模板", self.key,
                      [(k, tt.LABELS.get(k, k)) for k in keys],
                      on_select=self._pick)

        items = [
            self._head(),
            dd,
            T.button_row(
                T.tonal("填入内置默认", icon=ft.Icons.RESTORE,
                        on_click=self._reset),
                T.tonal("清空（回到内置）", icon=ft.Icons.CLEAR,
                        on_click=self._clear)),
            self._info,
            hint(f"占位符：{{chapter_title}} 等；中文别名 {{章名}}/{{章节号}}/{{章数}}/"
                 f"{{卷名}}/{{卷内容}}/{{前大纲}} 可直接用。"),
            self._field,
        ]

        save_bar = ft.Container(
            T.primary("保存模板", icon=ft.Icons.SAVE, on_click=self._save,
                      expand=True),
            padding=ft.Padding(left=T.GAP_L, right=T.GAP_L,
                               top=T.GAP_S, bottom=T.GAP_S),
            bgcolor=T.bg(),
            border=ft.Border.only(top=ft.BorderSide(1, T.line())))
        # 固定头 + 弹性编辑器 + 固定保存栏。外层保留 scroll 兜底：小屏上行数
        # 换算可能偏高，能滚总比把保存栏顶出屏幕好。
        return ft.Container(
            ft.Column([ft.Column(items, scroll=ft.ScrollMode.AUTO, expand=True,
                                 spacing=T.GAP_S,
                                 horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
                       save_bar], expand=True, spacing=0,
                      horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
            padding=ft.Padding(left=T.GAP_L, right=T.GAP_L,
                               top=T.GAP_S + 2, bottom=0),
            expand=True)

    def _head(self) -> ft.Control:
        return T.back_head(
            "任务模板", lambda: (self.on_back() if self.on_back else None))

    # ---------- 交互 ----------

    def _pick(self, e):
        val = getattr(e.control, "value", None) or self.key
        if val == self.key:
            return
        self.key = val
        self.refresh()

    def _reset(self, e):
        """把内置默认填进编辑框（需再点保存才生效，与桌面一致）。"""
        if self._field is not None:
            self._field.value = self._default()
        if self._info is not None:
            self._info.value = "已填入内置默认，点「保存」后作为你的自定义文本生效"
        try:
            self.page.update()
        except Exception:
            pass

    def _clear(self, e):
        """清空覆盖 → 回到内置默认（立即落库）。"""
        def _yes():
            try:
                self.gstore.set_task_template(self.key, "")
            except Exception as ex:
                toast(self.page, f"清空失败：{ex}", error=True)
                return
            toast(self.page, "已清空自定义，恢复内置默认")
            self.refresh()

        confirm(self.page, "清空自定义模板",
                f"确定清空「{_tpl_api().LABELS.get(self.key, self.key)}」的自定义内容，"
                f"恢复为内置默认？", _yes, yes_text="清空")

    def _save(self, e):
        text = (self._field.value or "") if self._field is not None else ""
        try:
            self.gstore.set_task_template(self.key, text)
        except Exception as ex:
            toast(self.page, f"保存失败：{ex}", error=True)
            return
        label = _tpl_api().LABELS.get(self.key, self.key)
        if text.strip():
            toast(self.page, f"已保存「{label}」的自定义模板（{len(text)} 字）")
        else:
            toast(self.page, f"「{label}」已恢复内置默认")
        self.refresh()
