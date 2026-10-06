# -*- coding: utf-8 -*-
"""崩溃留痕：把 Qt 致命消息 / Python 未捕获异常 / 运行心跳写进日志文件。

为什么需要：打包版（--windowed）没有控制台，程序被 Qt 主动 abort 时
（Windows 事件日志：Qt6Core.dll + 0xc0000409）什么提示都没有，只留一个
AppCrash 记录，事后无法定位。这里把三类信息落到
`<数据目录>/crash.log`，崩溃后打开即可看到「最后一刻发生了什么」。

- Qt 消息：qInstallMessageHandler 捕获，QtFatalMsg 单独高亮；
- Python 未捕获异常：sys.excepthook；
- 心跳：主线程定时写入（时间 + 内存占用 + 自动模式状态），用于判断
  崩溃前是否长时间运行 / 内存是否异常增长。

文件超过 1MB 自动轮转（保留上一份为 crash.log.1）。
"""
import os
import sys
import time
import traceback

_MAX_BYTES = 1_000_000
_LOG_NAME = "crash.log"
_initialized = False


def _dir() -> str:
    from .paths import app_data_dir
    try:
        return app_data_dir()
    except Exception:
        return os.path.dirname(os.path.abspath(__file__))


def log_path() -> str:
    return os.path.join(_dir(), _LOG_NAME)


def _rotate(path: str):
    try:
        if os.path.isfile(path) and os.path.getsize(path) > _MAX_BYTES:
            old = path + ".1"
            if os.path.isfile(old):
                os.remove(old)
            os.rename(path, old)
    except OSError:
        pass


def write_line(text: str, tag: str = "INFO"):
    """写一行日志（立即落盘，崩溃前也能保住）。"""
    path = log_path()
    try:
        _rotate(path)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{stamp}][{tag}] {text}\n"
        with open(path, "a", encoding="utf-8", errors="replace") as f:
            f.write(line)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
    except Exception:
        pass
    # 有控制台时同步输出，便于源码运行观察
    try:
        print(f"[{tag}] {text}", file=sys.stderr)
    except Exception:
        pass


def rss_mb() -> int:
    """当前进程物理内存占用（MB）；取不到返回 -1。"""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            k32 = ctypes.windll.kernel32
            # 必须显式声明 restype：GetCurrentProcess 返回 64 位伪句柄（-1），
            # 默认按 int 截断会拿到 0xFFFFFFFF → 调用失败（内存读数一直是 -1）
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            proc = k32.GetCurrentProcess()
            ok = False
            for dll in ("kernel32", "psapi"):
                try:
                    fn = getattr(ctypes.windll, dll).GetProcessMemoryInfo
                    fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
                    fn.restype = wintypes.BOOL
                    ok = bool(fn(proc, ctypes.byref(pmc), pmc.cb))
                    if ok:
                        break
                except Exception:
                    continue
            return int(pmc.WorkingSetSize // 1024 // 1024) if ok else -1
        import resource
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024)
    except Exception:
        return -1


def heartbeat(note: str = ""):
    """定时心跳：崩溃后据此判断最后状态与内存走势。"""
    write_line(f"心跳 内存={rss_mb()}MB {note}", "HB")


# ---------------- Qt 消息 / Python 异常钩子 ----------------

def _qt_handler(msg_type, context, *rest):
    """qInstallMessageHandler 回调：兼容 2/3 参数签名。"""
    msg = rest[-1] if rest else str(context)
    try:
        level = int(msg_type)
    except Exception:
        level = -1
    # QtMsgType：0 Debug / 1 Warning / 2 Critical / 3 Fatal / 4 Info
    if level == 3:
        where = ""
        try:
            where = f" ({context.file}:{context.line} {context.function})"
        except Exception:
            pass
        write_line(f"Qt 致命消息（即将 abort）: {msg}{where}", "FATAL")
    elif level in (2, 1):
        write_line(f"Qt 消息[{level}]: {msg}", "QT")
    return None


def _excepthook(etype, value, tb):
    try:
        text = "".join(traceback.format_exception(etype, value, tb))
    except Exception:
        text = f"{etype}: {value}"
    write_line(f"未捕获异常:\n{text}", "PYERR")


def install():
    """安装全部钩子（幂等）。main.py 在 QApplication 创建前调用。"""
    global _initialized
    if _initialized:
        return
    _initialized = True
    write_line(f"==== 启动 ==== python={sys.version.split()[0]} "
               f"argv0={os.path.basename(sys.argv[0])}", "BOOT")
    # Python 层未捕获异常（Qt 槽里的异常 PySide6 已自行打印，这里兜住其余场景）
    sys.excepthook = _excepthook
    try:
        from PySide6.QtCore import qInstallMessageHandler
        qInstallMessageHandler(_qt_handler)
    except Exception:
        pass
