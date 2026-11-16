# -*- coding: utf-8 -*-
"""线程桥：AI 生成任务的后台执行 + 流式节流回调 + 停止控制。

flet 0.86：page.run_thread(fn) 在独立线程执行；控件赋值后 page.update() 跨线程安全
（序列化消息发往客户端）。on_chunk 高频回调统一在这里节流（240ms）。
"""
import threading
import time


class StreamTask:
    """一次可停止的流式生成任务。"""

    def __init__(self, page):
        self.page = page
        self.stop_event = threading.Event()
        self.buffer = ""    # 累计生成文本（流式上屏用）
        self.chars = 0      # 累计字数（进度用）
        self.on_text = None  # callable(buffer) 由工作线程节流回调
        self._last = 0.0

    def _chunk(self, text: str):
        """pipeline 的 on_chunk 回调（工作线程）。"""
        if not text:
            return
        self.buffer += text
        self.chars += len(text)
        now = time.monotonic()
        # 240ms → 400ms：这条节流决定「每秒往客户端推几次整篇正文」。手机上
        # 240ms 时生成 1 万字要推上百万字符，是写作台生成期卡顿/闪退的放大器。
        if self.on_text and (now - self._last >= 0.4 or self.chars < 60):
            self._last = now
            try:
                self.on_text(self.buffer)
                self.page.update()
            except Exception:
                pass  # 页面已切换/销毁时静默放弃本次刷新

    def stop(self):
        self.stop_event.set()

    @property
    def stopped(self) -> bool:
        return self.stop_event.is_set()


def run_bg(page, fn, on_done=None):
    """后台线程执行 fn()，完成后工作线程回调 on_done(result) 并刷新页面。"""
    def wrap():
        try:
            result = fn()
        except Exception as e:  # 兜底：任何异常都变成 error 结果
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if on_done:
            try:
                on_done(result)
            except Exception:
                pass
        try:
            page.update()
        except Exception:
            pass
    return page.run_thread(wrap)
