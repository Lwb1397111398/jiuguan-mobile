# -*- coding: utf-8 -*-
"""线程桥：AI 生成任务的后台执行 + 流式节流回调 + 停止控制。

flet 0.86：page.run_thread(fn) 在独立线程执行；控件赋值后 page.update() 跨线程安全
（序列化消息发往客户端）。on_chunk 高频回调统一在这里节流（240ms）。

2026-11-20 新增**进程级在跑任务登记表**：全量导入会把整个资料库换掉，任何还在跑的
流式任务都必须能被一次性叫停（`stop_all_tasks`），不能靠页面自己那一个 `_task`
引用——弹层里发起的生成、自动模式里的生成，页面字段未必都记得住。
"""
import threading
import time

# 进程内所有未结束的流式任务（StreamTask.register / unregister 维护）。
# 只做「停机广播」，不参与调度；列表很短（同一时刻通常 0~2 个）。
_LIVE_TASKS: list = []
_TASKS_LOCK = threading.Lock()


class StreamTask:
    """一次可停止的流式生成任务。"""

    def __init__(self, page):
        self.page = page
        self.stop_event = threading.Event()
        self.buffer = ""    # 累计生成文本（流式上屏用）
        self.chars = 0      # 累计字数（进度用）
        self.on_text = None  # callable(buffer) 由工作线程节流回调
        self._last = 0.0
        self._registered = False

    # ---------- 生命周期登记 ----------

    def register(self):
        """登记为「在跑任务」（重复调用无副作用）。"""
        with _TASKS_LOCK:
            if not self._registered:
                _LIVE_TASKS.append(self)
                self._registered = True

    def unregister(self):
        """任务结束后注销（结果回调的 finally 里调，异常也不漏）。"""
        with _TASKS_LOCK:
            self._registered = False
            try:
                _LIVE_TASKS.remove(self)
            except ValueError:
                pass

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


def stop_all_tasks() -> int:
    """叫停所有在跑的流式任务（全量导入前调用），返回叫停数量。

    只置停止位：pipeline 在自己线程里看到就收工，落库回调随后会被会话闸门挡掉。
    这里**不 join**——事件线程不能等网络。
    """
    with _TASKS_LOCK:
        tasks = list(_LIVE_TASKS)
    for t in tasks:
        try:
            t.stop()
        except Exception:
            pass
    return len(tasks)


def live_task_count() -> int:
    """当前在跑的流式任务数（测试用：导入后必须为 0）。"""
    with _TASKS_LOCK:
        return len(_LIVE_TASKS)


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
