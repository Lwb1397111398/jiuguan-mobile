# -*- coding: utf-8 -*-
"""生成入口：组装 → 发送前预览（bottom sheet）→ 后台流式执行 → 结果回调。

对齐桌面版 run_generation 的数据约定（build dict / result dict），UI 面向手机。
"""
from widgets import preview_sheet, toast
from bridge import StreamTask


def start_generation(page, session, *, role, context, task, title, on_done,
                     on_text=None, stream=True, bare_profile=None,
                     max_tokens=None, on_started=None, preview=True,
                     continue_round=False):
    """统一生成入口。

    on_done(result)：工作线程回调，result = pipeline 返回 dict（ok/text/error/status/...）。
    on_text(buffer)：流式节流回调（工作线程），buffer 为累计生成文本。
    on_started(task)：任务启动时回调（拿到 StreamTask 以便停止）。
    preview=False：跳过发送前预览直接执行（⚡ 自动模式=用户预先授权）。
    continue_round：续写轮且确实带上了上轮思考 → 内核临时把思考档位降到 low
    （2026-11-05，与桌面 run_generation 同名参数对齐）。正常生成恒为 False。
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
                 bare_profile, max_tokens, "", on_started, use_cache=False,
                 continue_round=continue_round)
        return

    def _send(extra: str):
        # 2026-11-09 起手动生成一律出新的（旧稿去日志拿），不再传 use_cache=True
        _execute(page, pipe, role, context, task, title, on_done, on_text,
                 bare_profile, max_tokens, extra, on_started, use_cache=False,
                 continue_round=continue_round)

    preview_sheet(page, build, profile, route, _send, title=title)


def _execute(page, pipe, role, context, task, title, on_done, on_text,
             bare_profile, max_tokens, extra, on_started, use_cache=False,
             continue_round=False):
    ctx = {**context, "extra": extra} if extra else context
    tk = StreamTask(page)
    tk.on_text = on_text
    # 登记到进程级任务表：全量导入前 bridge.stop_all_tasks() 一次叫停所有生成，
    # 不依赖「页面有没有把这个 tk 存进 self._task」（弹层里发起的常常没存）。
    tk.register()

    def job():
        if bare_profile is not None:
            return pipe.run_bare(
                bare_profile, task, stream=True, on_chunk=tk._chunk,
                stop_event=tk.stop_event, max_tokens=max_tokens,
                extra=extra or "", continue_round=continue_round)
        # 生成结果缓存已于 2026-11-09 从所有手动生成链路摘除（老板：再点生成要新的，
        # 旧稿去请求日志拿）；use_cache 参数保留给底层能力，两端调用点一律传 False。
        return pipe.run(
            role, ctx, task, stream=True, on_chunk=tk._chunk,
            stop_event=tk.stop_event, max_tokens=max_tokens, use_cache=use_cache,
            continue_round=continue_round)

    if on_started:
        try:
            on_started(tk)
        except Exception:
            pass

    def done(result):
        try:
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
        finally:
            # 无论回调有没有炸都要注销：否则任务表会留下已结束的僵尸任务
            tk.unregister()

    from bridge import run_bg
    run_bg(page, job, done)
