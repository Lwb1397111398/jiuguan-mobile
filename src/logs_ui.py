# -*- coding: utf-8 -*-
"""请求日志（Flet）：完整留痕 + 角色/状态筛选 + 成本统计 + 清空 + 单条详情。

数据全部来自项目库 `request_logs`，读写走 core（`store.list_logs` / `get_log` /
`clear_logs` / `cost_stats`），口径与桌面 `app/ui/logs_page.py` 一致：

- 统计行：本月请求次数 / 本月费用 / 总费用；
- 筛选：角色（走 SQL）+ 状态（内存过滤）；
- 点条目看完整提示词与返回（超长内容截断并明示，完整记录在电脑版可看）。

字段名坑（2026-09-18 修）：表里的列是 `prompt_tokens` / `completion_tokens` / `time`，
旧实现读的是 `input_tokens` / `output_tokens` / `created_at` —— 全是空值，
所以手机版 token 恒显示 0。
"""
import flet as ft

from widgets import hint, toast, confirm, fmt_tokens

STATUS_TEXT = {"ok": "成功", "stopped": "已停止", "error": "失败"}
STATUS_COLOR = {"ok": ft.Colors.GREEN, "stopped": ft.Colors.ORANGE,
                "error": ft.Colors.RED}
_PREVIEW_CAP = 20000      # 单段提示词/返回的最大渲染字符数
_LIST_LIMIT = 200


def logs_view(page, session, on_refresh=None):
    """打开请求日志面板（BottomSheet）。"""
    if not session.has_project:
        toast(page, "请先打开一个项目", error=True)
        return
    store = session.store
    try:
        routes = store.list_routes()
    except Exception:
        routes = []
    name_of = {r.get("role"): (r.get("name") or r.get("role")) for r in routes}

    stats = ft.Text("", size=12)
    count = ft.Text("", size=11, color=ft.Colors.ON_SURFACE_VARIANT)
    body = ft.Column([], spacing=2, scroll=ft.ScrollMode.AUTO,
                     height=int((getattr(page, "height", 720) or 720) * 0.5))

    role_dd = ft.Dropdown(
        label="角色", dense=True, value="全部",
        options=[ft.dropdown.Option(key="全部", text="全部")]
        + [ft.dropdown.Option(key=r.get("role", ""),
                              text=f"{r.get('name') or r.get('role')}（{r.get('role')}）")
           for r in routes])
    status_dd = ft.Dropdown(
        label="状态", dense=True, value="全部",
        options=[ft.dropdown.Option(key=k, text=k)
                 for k in ("全部", "ok", "stopped", "error")])

    # ---------- 详情 ----------

    def _text_block(text):
        t = str(text or "")
        if len(t) > _PREVIEW_CAP:
            t = t[: _PREVIEW_CAP] + "\n…（内容过长已截断，完整记录请在电脑版查看）"
        return ft.Container(
            ft.Column([ft.Text(t or "（空）", size=11, selectable=True)],
                      scroll=ft.ScrollMode.AUTO, expand=True),
            bgcolor=ft.Colors.with_opacity(0.06, ft.Colors.ON_SURFACE),
            border_radius=8, padding=8, expand=True)

    def _detail(log_id):
        try:
            log = store.get_log(log_id)
        except Exception:
            log = None
        if not log:
            toast(page, "找不到该条日志", error=True)
            return
        st = log.get("status") or ""
        head = (f"{log.get('time', '')} ｜ 角色 "
                f"{name_of.get(log.get('role'), log.get('role') or '-')}"
                f" ｜ 模型 {log.get('model') or '-'} ｜ {STATUS_TEXT.get(st, st)} ｜ "
                f"输入 {fmt_tokens(log.get('prompt_tokens', 0))} / "
                f"输出 {fmt_tokens(log.get('completion_tokens', 0))} tok ｜ "
                f"费用 {float(log.get('cost') or 0):.4f} 元 ｜ "
                f"{log.get('duration_ms', 0)}ms"
                + (f" ｜ 错误：{log.get('error')}" if log.get("error") else ""))
        w = int(getattr(page, "width", 0) or 0) or 420
        page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text(f"请求日志 #{log.get('id')}"),
            content=ft.Container(
                ft.Column([
                    ft.Text(head, size=11, color=ft.Colors.ON_SURFACE_VARIANT),
                    ft.Text("提示词", size=13, weight=ft.FontWeight.W_600),
                    _text_block(log.get("prompt")),
                    ft.Text("返回", size=13, weight=ft.FontWeight.W_600),
                    _text_block(log.get("response")),
                ], spacing=6, expand=True),
                width=min(560, int(w * 0.94)),
                height=int((getattr(page, "height", 720) or 720) * 0.6)),
            actions=[ft.TextButton("关闭", on_click=lambda e: page.pop_dialog())],
            actions_alignment=ft.MainAxisAlignment.END))

    # ---------- 行 ----------

    def _tile(lg):
        st = lg.get("status") or ""
        cost = float(lg.get("cost") or 0)
        err = lg.get("error") or ""
        return ft.ListTile(
            leading=ft.Icon(ft.Icons.RECEIPT_LONG, size=18,
                            color=STATUS_COLOR.get(st)),
            title=ft.Text(
                f"{name_of.get(lg.get('role'), lg.get('role') or '-')}"
                f" · {lg.get('model') or '-'} · {STATUS_TEXT.get(st, st)}",
                size=13),
            subtitle=ft.Text(
                f"{lg.get('time', '')} ｜ 输入 {fmt_tokens(lg.get('prompt_tokens', 0))}"
                f" / 输出 {fmt_tokens(lg.get('completion_tokens', 0))} tok ｜ "
                f"{cost:.4f} 元 ｜ {lg.get('duration_ms', 0)}ms"
                + (f" ｜ {err}" if err else ""),
                size=11, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
            trailing=ft.Icon(ft.Icons.CHEVRON_RIGHT, size=16),
            on_click=lambda e, _id=lg.get("id"): _detail(_id))

    # ---------- 数据 ----------

    def _load():
        try:
            st = store.cost_stats()
        except Exception:
            st = {"month_count": 0, "month": 0.0, "total": 0.0}
        stats.value = (f"本月请求 {st.get('month_count', 0)} 次 ｜ "
                       f"本月费用 {float(st.get('month') or 0):.4f} 元 ｜ "
                       f"总费用 {float(st.get('total') or 0):.4f} 元")
        role = None if role_dd.value == "全部" else role_dd.value
        try:
            logs = store.list_logs(limit=_LIST_LIMIT, role=role)
        except Exception:
            logs = []
        want = status_dd.value
        if want != "全部":
            logs = [lg for lg in logs if (lg.get("status") or "") == want]
        body.controls = [_tile(lg) for lg in logs] or [
            hint("当前项目暂无请求记录（每次 AI 调用都会留痕，可在电脑版对照查看）")]
        count.value = f"共 {len(logs)} 条（最多显示 {_LIST_LIMIT} 条）"

    def _on_filter(e):
        _load()
        try:
            page.update()
        except Exception:
            pass

    role_dd.on_select = _on_filter
    status_dd.on_select = _on_filter

    def _clear(e):
        def _yes():
            try:
                store.clear_logs()
            except Exception as ex:
                toast(page, f"清空失败：{ex}", error=True)
                return
            _load()
            try:
                page.update()
            except Exception:
                pass
            toast(page, "日志已清空")

        confirm(page, "清空日志",
                "清空后所有请求记录将永久删除，无法恢复。确定清空？",
                _yes, yes_text="清空")

    _load()
    content = ft.Column([
        ft.Text("请求日志", size=18, weight=ft.FontWeight.BOLD),
        stats,
        ft.Row([role_dd, status_dd], spacing=8),
        ft.Row([ft.TextButton("刷新", icon=ft.Icons.REFRESH, on_click=_on_filter),
                ft.TextButton("清空日志", icon=ft.Icons.DELETE_FOREVER, on_click=_clear)],
               spacing=8, wrap=True),
        count,
        body,
        ft.Row([ft.TextButton("关闭", on_click=lambda e: page.pop_dialog())],
               alignment=ft.MainAxisAlignment.END),
    ], spacing=6)
    page.show_dialog(ft.BottomSheet(
        content=ft.Container(content, padding=14),
        show_drag_handle=True, use_safe_area=True))
