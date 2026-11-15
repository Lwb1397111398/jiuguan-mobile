# -*- coding: utf-8 -*-
"""通用控件与提示：toast、确认对话框、预览 bottom sheet、小部件。"""
import threading

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
    """发送前预览（bottom sheet）：头部信息 + 提示词全文（含去破限视图）+ 费用
    + 额外要求 + 复制/取消/发送。

    去破限视图（桌面 2f8e900 移植，2026-11-15）：勾选后预览区切换为剔除全部
    破限模块的报文（assembler.strip_jailbreak_build，仅显示层——原 build 一个
    字节不动，实际发送仍含全部破限内容），配合「复制当前全文」可把当前视图
    贴进酒馆对比破限效果。build 由 gen.py 直接传入组装产物，自带
    messages/modules/msg_modules，无需调用方额外配合。
    """
    from app.core.assembler import JAILBREAK_KINDS, strip_jailbreak_build

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

    # ---- 去破限视图（仅显示层；实际发送的 build 不受影响）----
    stripped_build = None   # 去破限 build（首次勾选时惰性构建，不改原 build）
    view_field = ft.TextField(
        value=preview_full_text(build),
        multiline=True, min_lines=8, max_lines=18,
        read_only=True, border_color=ft.Colors.TRANSPARENT,
        text_size=11,
    )
    strip_hint = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT,
                         visible=False)

    def _current_build() -> dict:
        """当前视图使用的 build：勾选「去除破限内容」时返回去破限 build
        （惰性构建），否则原 build——与桌面 PromptPreviewDialog._current_build 同构。"""
        nonlocal stripped_build
        if strip_check.value:
            stripped_build = stripped_build or strip_jailbreak_build(build)
            return stripped_build
        return build

    def _on_strip_toggle(e):
        """勾选「去除破限内容」：切换视图 + 更新统计提示（对齐桌面 _on_strip_toggled）。"""
        on = bool(strip_check.value)
        strip_hint.visible = on
        if on:
            jb = [m for m in build.get("modules", [])
                  if m.get("kind") in JAILBREAK_KINDS]
            chars = sum(len(m.get("text") or "") for m in jb)
            strip_hint.value = (
                f"已剔除 {len(jb)} 个破限模块（共 {chars:,} 字）——仅预览显示；"
                "实际发送的提示词仍包含全部破限内容，对比酒馆后点「取消」即不花钱。")
        view_field.value = preview_full_text(_current_build())
        try:
            strip_hint.update()
            view_field.update()
        except Exception:
            pass

    strip_check = ft.Checkbox(label="去除破限内容（对比酒馆用）",
                              on_change=_on_strip_toggle)

    def _copy_current(e):
        """复制当前视图文本（勾选=去破限版，未勾=原文）到剪贴板，带「已复制 ✓」反馈
        （对齐桌面 _copy_current；flet 0.86 剪贴板为异步 API，经 page.run_task 调度）。"""
        try:
            page.run_task(page.clipboard.set, view_field.value or "")
        except Exception:
            pass
        try:
            copy_btn.content = ft.Text("已复制 ✓")
            copy_btn.update()
        except Exception:
            pass

        def _restore():
            # threading.Timer 线程里刷 UI：一律 page.update() 且吞异常（页面可能已切走）
            try:
                copy_btn.content = ft.Text("复制当前全文")
                page.update()
            except Exception:
                pass

        threading.Timer(1.5, _restore).start()

    copy_btn = ft.TextButton("复制当前全文", on_click=_copy_current)

    content = ft.Column([
        ft.Row([ft.Text(title, size=16, weight=ft.FontWeight.BOLD)],),
        ft.Text(head, size=12, color=ft.Colors.ON_SURFACE_VARIANT),
        *( [error_card(f"⚠ 超出上下文预算，已自动裁剪：{'、'.join(dropped)}")]
           if dropped else [] ),
        strip_check,
        strip_hint,
        ft.Container(
            view_field,
            height=380,
        ),
        ft.Text(cost, size=12, color=ft.Colors.ON_SURFACE_VARIANT),
        extra_field,
        ft.Row([
            copy_btn,
            ft.Container(expand=True),
            ft.TextButton("取消", on_click=_cancel),
            ft.FilledButton("发送请求", on_click=_send),
        ]),
    ], spacing=8, scroll=ft.ScrollMode.AUTO)

    page.show_dialog(ft.BottomSheet(
        content=ft.Container(content, padding=16),
        show_drag_handle=True,
        use_safe_area=True,
    ))
