# -*- coding: utf-8 -*-
"""崩溃 / 异常留痕（手机版）：把 Python 未捕获异常落到 `<数据目录>/crash.log`。

为什么需要：APK 没有控制台，闪退回桌面时什么提示都不留，事后只能靠猜。
这里装两个钩子（主线程 sys.excepthook + 工作线程 threading.excepthook），
把「什么时间、在哪个线程、执行到哪一行」写进日志；同步守护线程、归档、
生成这些后台链路崩了都能留下最后现场。

**它抓不到 Flutter/原生层崩溃**（那种是 OOM 或渲染引擎层，Python 还没机会
执行）。所以日志的价值是「排除法」：闪退后打开日志——最后一行是启动到现在
的第几步、有没有 Python 异常，就能判断是业务代码炸了还是原生层崩了。

读取入口：「我的 → 崩溃诊断」（`logs_ui.py` 之外单独一条，只在有日志时出现）。
文件超过 256KB 自动轮转一份 `.1`，不会无限涨。
"""
import os
import sys
import threading
import time
import traceback

_MAX_BYTES = 256 * 1024
_installed = False


def log_path() -> str:
    try:
        from app.core.paths import app_data_dir
        return os.path.join(app_data_dir(), "crash.log")
    except Exception:
        return ""


def _rotate(path: str):
    try:
        if os.path.isfile(path) and os.path.getsize(path) > _MAX_BYTES:
            old = path + ".1"
            if os.path.isfile(old):
                os.remove(old)
            os.replace(path, old)
    except OSError:
        pass


def write(text: str, tag: str = "INFO"):
    """写一行（立即 flush + fsync：闪退时也要保住）。"""
    path = log_path()
    if not path:
        return
    try:
        _rotate(path)
        stamp = time.strftime("%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"[{stamp}][{tag}] {text}\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
    except Exception:
        pass


def _fmt(etype, value, tb, thread_name: str = "") -> str:
    try:
        body = "".join(traceback.format_exception(etype, value, tb))
    except Exception:
        body = f"{etype}: {value}"
    who = f"thread={thread_name}" if thread_name else "thread=main"
    return f"{who}\n{body.rstrip()}"


def _excepthook(etype, value, tb):
    write(_fmt(etype, value, tb), "PYERR")
    sys.__excepthook__(etype, value, tb)


def _thread_hook(args):
    if args.exc_type is SystemExit:
        return
    write(_fmt(args.exc_type, args.exc_value, args.exc_traceback,
               getattr(args.thread, "name", "")), "THREAD")
    if getattr(args, "thread", None) is threading.main_thread():
        sys.__excepthook__(args.exc_type, args.exc_value, args.exc_traceback)


def install(version: str = ""):
    """装钩子（幂等）。main() 里第一件事调用。"""
    global _installed
    if _installed:
        return
    _installed = True
    try:
        sys.excepthook = _excepthook
        threading.excepthook = _thread_hook
    except Exception:
        pass
    try:
        import platform
        write(f"==== 启动 v{version} ==== python={sys.version.split()[0]} "
              f"platform={sys.platform} machine={platform.machine()} "
              f"data={os.path.dirname(log_path())}", "BOOT")
    except Exception:
        write("==== 启动 ====", "BOOT")


def mark(note: str):
    """记一笔关键动作（生成/归档/切写法…），闪退后能看出最后走到哪。"""
    write(note, "STEP")


def tail(n: int = 200) -> str:
    """读最后 n 行（给「崩溃诊断」页用）；没有日志返回空串。"""
    path = log_path()
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except Exception:
        return ""


def exists() -> bool:
    path = log_path()
    return bool(path and os.path.isfile(path))


def clear():
    path = log_path()
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass
