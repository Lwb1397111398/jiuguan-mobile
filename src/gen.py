# -*- coding: utf-8 -*-
"""生成入口：组装 → 发送前预览（bottom sheet）→ 后台流式执行 → 结果回调。

对齐桌面版 run_generation 的数据约定（build dict / result dict），UI 面向手机。
"""
from widgets import preview_sheet, toast
from bridge import StreamTask


def start_generation(page, session, *, role, context, task, title, on_done,
                     on_text=None, stream=True, bare_profile=None,
                     max_tokens=None, on_started=None, preview=True):
    """统一生成入口。

    on_done(result)：工作线程回调，result = pipeline 返回 dict（ok/text/error/status/...）。
    on_text(buffer)：流式节流回调（工作线程），buffer 为累计生成文本。
    on_started(task)：任务启动时回调（拿到 StreamTask 以便停止）。
    preview=False：跳过发送前预览直接执行（⚡ 自动模式=用户预先授权）。
    """
    store = session.store
    pipe = session.pipeline()
    try:
        if bare_profile is not None:
            profile, route = bare_profile, None
            build = pipe.assembler.build_bare(bare_profile, task)
        else:
            route, profile = pipe._route_profile(role)
            build = pipe.assembler.build(role, context, task, profile)
    except Exception as e:
        toast(page, f"组装失败：{e}", error=True)
        return

    if not preview:
        _execute(page, pipe, role, context, task, title, on_done, on_text,
                 bare_profile, max_tokens, "", on_started, use_cache=False)
        return

    def _send(extra: str):
        _execute(page, pipe, role, context, task, title, on_done, on_text,
                 bare_profile, max_tokens, extra, on_started, use_cache=True)

    preview_sheet(page, build, profile, route, _send, title=title)


def _execute(page, pipe, role, context, task, title, on_done, on_text,
             bare_profile, max_tokens, extra, on_started, use_cache=False):
    ctx = {**context, "extra": extra} if extra else context
    tk = StreamTask(page)
    tk.on_text = on_text

    def job():
        if bare_profile is not None:
            return pipe.run_bare(
                bare_profile, task, stream=True, on_chunk=tk._chunk,
                stop_event=tk.stop_event, max_tokens=max_tokens,
                extra=extra or "")
        # 生成结果缓存（2026-10-27）：手动生成（preview=True 路径）按指纹复用
        # 相同请求的上次完整结果；preview=False 是⚡自动模式，绝不缓存
        # （失败重试不能拿到同一个坏结果）。开关判定在内核 pipeline.run 内。
        return pipe.run(
            role, ctx, task, stream=True, on_chunk=tk._chunk,
            stop_event=tk.stop_event, max_tokens=max_tokens, use_cache=use_cache)

    if on_started:
        try:
            on_started(tk)
        except Exception:
            pass

    def done(result):
        if isinstance(result, dict) and result.get("ok") is False \
                and result.get("error"):
            toast(page, f"{title}失败：{result['error']}", error=True)
        elif isinstance(result, dict) and result.get("channel_note"):
            # 渠道故障自动切换的如实反馈（2026-09-18）：成功也提示换了哪条渠道
            toast(page, result["channel_note"])
        if on_done:
            try:
                on_done(result)
            except Exception:
                pass
    from bridge import run_bg
    run_bg(page, job, done)
