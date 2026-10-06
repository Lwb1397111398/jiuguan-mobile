# -*- coding: utf-8 -*-
"""通用控件与提示：toast、确认对话框、预览 bottom sheet、小部件。"""
import flet as ft


def toast(page, msg: str, error: bool = False):
    try:
        page.show_dialog(ft.SnackBar(
            content=ft.Text(msg, color=ft.Colors.WHITE),
            bgcolor=ft.Colors.RED_700 if error else None,
            duration=4000,
        ))
    except Exception:
        pass


def confirm(page, title: str, msg: str, on_yes, yes_text="确定"):
    """确认对话框：用户点 yes_text 后回调 on_yes()。"""
    def _close(e):
        page.pop_dialog()

    def _yes(e):
        page.pop_dialog()
        on_yes()

    page.show_dialog(ft.AlertDialog(
        modal=True,
        title=ft.Text(title),
        content=ft.Text(msg),
        actions=[
            ft.TextButton("取消", on_click=_close),
            ft.FilledButton(yes_text, on_click=_yes),
        ],
        actions_alignment=ft.MainAxisAlignment.END,
    ))


def section(text: str) -> ft.Container:
    return ft.Container(
        ft.Text(text, size=15, weight=ft.FontWeight.BOLD),
        margin=ft.Margin(left=0, right=0, top=14, bottom=6),
    )


def hint(text: str) -> ft.Container:
    return ft.Container(
        ft.Text(text, size=12, color=ft.Colors.ON_SURFACE_VARIANT),
        margin=ft.Margin(left=0, right=0, top=0, bottom=4),
    )


def empty(text: str) -> ft.Container:
    return ft.Container(
        content=ft.Column([
            ft.Icon(ft.Icons.AUTO_STORIES, size=42,
                    color=ft.Colors.with_opacity(0.35, ft.Colors.ON_SURFACE)),
            ft.Text(text, color=ft.Colors.ON_SURFACE_VARIANT, size=13,
                    text_align=ft.TextAlign.CENTER),
        ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
        alignment=ft.Alignment.CENTER,
        padding=36,
    )


def error_card(msg: str) -> ft.Container:
    return ft.Container(
        ft.Text(msg, color=ft.Colors.RED_300, size=13),
        bgcolor=ft.Colors.with_opacity(0.12, ft.Colors.RED_700),
        border_radius=8,
        padding=10,
        margin=ft.Margin(left=0, right=0, top=6, bottom=6),
    )


def preview_full_text(build: dict) -> str:
    """把 build.messages 拼成可读全文（与发送内容一致，加角色分段标题）。"""
    role_names = {"system": "系统", "user": "用户", "assistant": "助手"}
    parts = []
    for msg in build.get("messages", []):
        rn = role_names.get(msg.get("role"), msg.get("role", ""))
        parts.append(f"--- [{rn}] ---\n{msg.get('content', '')}")
    return "\n\n".join(parts)


def fmt_tokens(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "0"
    return f"{n / 10000:.1f}万" if n >= 10000 else str(n)


def preview_sheet(page, build: dict, profile: dict, route: dict | None,
                  on_send, title: str = "发送前预览"):
    """发送前预览（bottom sheet）：头部信息 + 提示词全文 + 费用 + 额外要求 + 取消/发送。"""
    dropped = build.get("dropped") or []
    extra_field = ft.TextField(
        label="额外要求（可选，本次生成附加）",
        hint_text="如：第三段重点写气氛／埋一个玉佩伏笔…",
        multiline=True, min_lines=1, max_lines=3, dense=True,
    )
    head = ("" if route is None else f"角色：{route.get('name', '')} ｜ ") + \
        f"模型：{profile.get('name', '')}（{profile.get('model', '')}）"
    cost = (f"预估输入 {fmt_tokens(build.get('est_input_tokens', 0))} + "
            f"输出 {fmt_tokens(build.get('est_output_tokens', 0))} token ｜ "
            f"费用 {build.get('est_cost', 0):.4f} {profile.get('currency', '元')}")

    def _send(e):
        page.pop_dialog()
        on_send((extra_field.value or "").strip())

    def _cancel(e):
        page.pop_dialog()

    content = ft.Column([
        ft.Row([ft.Text(title, size=16, weight=ft.FontWeight.BOLD)],),
        ft.Text(head, size=12, color=ft.Colors.ON_SURFACE_VARIANT),
        *( [error_card(f"⚠ 超出上下文预算，已自动裁剪：{'、'.join(dropped)}")]
           if dropped else [] ),
        ft.Container(
            ft.TextField(
                value=preview_full_text(build),
                multiline=True, min_lines=8, max_lines=18,
                read_only=True, border_color=ft.Colors.TRANSPARENT,
                text_size=11,
            ),
            height=380,
        ),
        ft.Text(cost, size=12, color=ft.Colors.ON_SURFACE_VARIANT),
        extra_field,
        ft.Row([
            ft.TextButton("取消", on_click=_cancel),
            ft.FilledButton("发送请求", on_click=_send),
        ], alignment=ft.MainAxisAlignment.END),
    ], spacing=8, scroll=ft.ScrollMode.AUTO)

    page.show_dialog(ft.BottomSheet(
        content=ft.Container(content, padding=16),
        show_drag_handle=True,
        use_safe_area=True,
    ))
