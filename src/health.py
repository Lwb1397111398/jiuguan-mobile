# -*- coding: utf-8 -*-
"""健康提醒：把 core 诊断问题显示在页面顶部（对应桌面版的顶部提醒栏）。

问题来源与桌面完全一致（`app.core.diagnostics`，事件驱动、不做后台轮询）：
- 渠道/嵌入调用失败、归档失败、向量索引失败 → 各模块 record_issue 登记；
- 嵌入配置「填了一半」→ collect_issues 里的本地即时检查（零成本，不发请求）。

与桌面的差异（手机端取舍，写在此处备查）：
- 桌面 30 秒定时刷新；手机端改为「进页面即刷新 + 手动重新检测」，避免常驻定时器耗电；
- 桌面是全局横幅；手机端挂在「我的」与「项目」两页顶部，不挤占创作页面。
"""
import datetime

import flet as ft

from widgets import toast
from bridge import run_bg

_SEVERITY_ICON = {"danger": "🚨", "warn": "⚠️"}


def _fmt_time(raw) -> str:
    try:
        dt = datetime.datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return ""
    if dt.date() == datetime.date.today():
        return f"今天 {dt:%H:%M}"
    return f"{dt:%m-%d %H:%M}"


def collect(session) -> list:
    """当前待处理问题（已登记事件 + 本地检查）。任何异常都返回空表。"""
    try:
        from app.core.diagnostics import collect_issues
        store = session.store if session.has_project else None
        return collect_issues(store)
    except Exception:
        return []


def banner(page, session, on_refresh=None):
    """有诊断问题时返回提醒卡片，否则返回 None（零占位，不打搅正常使用）。"""
    issues = collect(session)
    if not issues:
        return None
    keys = [i.get("key") for i in issues]
    if getattr(session, "_health_dismissed", None) == frozenset(keys):
        return None     # 本会话已关闭，且问题集合没有变化

    def _refresh():
        if on_refresh:
            on_refresh()
        else:
            try:
                page.update()
            except Exception:
                pass

    def _dismiss(e):
        try:
            session._health_dismissed = frozenset(keys)
        except Exception:
            pass
        _refresh()

    def _recheck(e):
        def job():
            from app.core.embedding import test_embedding, load_embed_config
            from app.core.diagnostics import record_issue
            store = session.store if session.has_project else None
            embed = load_embed_config(store) if store is not None else None
            if not embed:
                return {"ok": False,
                        "msg": "嵌入配置不完整（地址/模型/密钥），到「设置 → 嵌入接口」补全"}
            r = test_embedding(embed["base_url"], embed["api_key"], embed["model"])
            if not r.get("ok"):
                record_issue("embed_call", "向量（嵌入）接口调用失败",
                             str(r.get("error") or r.get("message") or "")[:300],
                             hint="检查接口地址/密钥/模型后重试。")
            return {"ok": bool(r.get("ok")),
                    "msg": r.get("message") or r.get("error") or "未知结果"}

        def done(r):
            r = r if isinstance(r, dict) else {}
            ok = bool(r.get("ok"))
            toast(page, ("检测通过：" if ok else "") + str(r.get("msg") or ""),
                  error=not ok)
            _refresh()
        run_bg(page, job, done)

    def _import_lore(e):
        if not session.has_project:
            toast(page, "请先打开一个项目", error=True)
            return
        store = session.store

        def job():
            from app.core.store import lore_import_preview, lore_import_from_memory
            if not lore_import_preview(store):
                return []
            return lore_import_from_memory(store)

        def done(r):
            try:
                from app.core.diagnostics import clear_issue
                clear_issue("lore_sync")
            except Exception:
                pass
            n = len(r or [])
            toast(page, f"已导入 {n} 条设定" if n else "没有需要导入的条目")
            _refresh()
        run_bg(page, job, done)

    # 一键导入只在「设定库缺卡」且确实有待导入条目时出现
    show_import = False
    if any(i.get("key") == "lore_sync" for i in issues) and session.has_project:
        try:
            from app.core.store import lore_import_preview
            show_import = bool(lore_import_preview(session.store))
        except Exception:
            show_import = False

    rows = []
    for i in issues:
        bits = [ft.Row([
            ft.Text(_SEVERITY_ICON.get(i.get("severity"), "⚠️"), size=14),
            ft.Text(i.get("title") or "未知问题", size=13,
                    weight=ft.FontWeight.W_600, expand=True),
            ft.Text(_fmt_time(i.get("last_seen")), size=10,
                    color=ft.Colors.ON_SURFACE_VARIANT),
        ], spacing=6)]
        if i.get("detail"):
            bits.append(ft.Text(i["detail"], size=11,
                                color=ft.Colors.ON_SURFACE_VARIANT))
        if i.get("hint"):
            bits.append(ft.Text("建议：" + i["hint"], size=11,
                                color=ft.Colors.ON_SURFACE_VARIANT))
        if i.get("count", 1) and int(i.get("count", 1)) > 1:
            bits.append(ft.Text(f"已发生 {i['count']} 次", size=10,
                                color=ft.Colors.ON_SURFACE_VARIANT))
        rows.append(ft.Column(bits, spacing=2))

    danger = any(i.get("severity") == "danger" for i in issues)
    footer = [
        ft.TextButton("重新检测", icon=ft.Icons.REFRESH, on_click=_recheck),
    ]
    if show_import:
        footer.append(ft.TextButton("一键导入设定库", icon=ft.Icons.DOWNLOAD,
                                    on_click=_import_lore))
    footer.append(ft.TextButton("关闭提醒", on_click=_dismiss))

    title = (issues[0].get("title", "") if len(issues) == 1
             else f"发现 {len(issues)} 个问题")
    return ft.Card(ft.Container(ft.Column([
        ft.Row([
            ft.Icon(ft.Icons.ERROR if danger else ft.Icons.WARNING_AMBER,
                    size=18,
                    color=ft.Colors.RED_700 if danger else ft.Colors.ORANGE_800),
            ft.Text(title, size=13, weight=ft.FontWeight.BOLD, expand=True),
        ], spacing=6),
        *rows,
        ft.Row(footer, wrap=True, spacing=4),
    ], spacing=6), padding=12),
        margin=ft.Margin(left=0, right=0, top=0, bottom=6))
