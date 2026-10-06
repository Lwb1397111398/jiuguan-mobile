# -*- coding: utf-8 -*-
"""电源管理：阻止电脑自动睡眠/关闭屏幕，供长时间自动创作挂机使用。

背景：系统进入睡眠（或合盖休眠）后，进程被整体冻结 —— 自动模式的网络请求
会卡死，唤醒后连接已断、这一步必然失败重试。因此挂机跑自动模式需要主动
向系统声明「我在忙，别睡」。

实现：Windows 用 SetThreadExecutionState（ES_CONTINUOUS 持续生效，直到
再次调用或进程退出）；其它平台（安卓/Linux）无对应实现，全部静默跳过。
核心层不依赖 Qt，手机端共用也不会报错。

注意：
- 只阻止「系统空闲自动睡眠」，不阻止用户手动睡眠/合盖/按电源键；
- 不阻止锁屏（锁屏后程序照常运行，网络请求不受影响）；
- 笔记本用电池时开启此项会持续耗电，故默认只阻止睡眠、不强制屏幕常亮。
"""
import sys

# 执行状态标记（Windows winbase.h）
ES_SYSTEM_REQUIRED = 0x00000001   # 阻止系统空闲进入睡眠
ES_DISPLAY_REQUIRED = 0x00000002  # 阻止显示器关闭（屏幕常亮）
ES_CONTINUOUS = 0x80000000        # 持续生效（否则只是一次性重置空闲计时器）

_current: int | None = None   # 当前已生效的标记（None=未设置）
_last_ok: bool = False        # 最近一次调用是否成功


def supported() -> bool:
    """当前平台是否支持防休眠（目前仅 Windows）。"""
    return sys.platform == "win32"


def _apply(flags: int) -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception:
        return False


def set_awake(enabled: bool, display: bool = False) -> bool:
    """设置/取消防休眠。

    enabled=True  → 阻止系统睡眠；display=True 时同时保持屏幕常亮。
    enabled=False → 立即恢复系统原本的电源策略（必须调用，否则进程退出前
                    系统一直不会睡）。
    返回是否设置成功（不支持的平台返回 False）。
    """
    global _current, _last_ok
    if not supported():
        _current = None
        _last_ok = False
        return False
    if not enabled:
        flags = ES_CONTINUOUS
    else:
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        if display:
            flags |= ES_DISPLAY_REQUIRED
    if _current == flags:
        return _last_ok          # 状态没变，不重复打扰系统
    _last_ok = _apply(flags)
    # 只有「真正提出了需求」才算已生效；仅 ES_CONTINUOUS 是恢复默认策略，
    # 记为 None，status_text 才不会误报「已生效」
    _current = flags if (flags & (ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)) else None
    return _last_ok


def pulse(enabled: bool, display: bool = False) -> bool:
    """心跳式重申防休眠（与 set_awake 同效，状态未变时跳过）。

    某些电源策略/唤醒事件后状态会被重置，定时重申更稳妥。
    """
    return set_awake(enabled, display)


def release() -> None:
    """恢复系统电源策略（退出应用时调用）。"""
    global _current
    _current = None              # 置空，确保下次 set_awake 一定真正调用
    set_awake(False)


def status_text() -> str:
    """当前防休眠状态（设置页展示用）。"""
    if not supported():
        return "当前平台不支持（仅 Windows 有效）"
    if not _current:
        return "未生效（系统按电源设置正常休眠）"
    txt = "已生效：系统不会自动睡眠"
    if _current & ES_DISPLAY_REQUIRED:
        txt += "，屏幕保持常亮"
    return txt + ("" if _last_ok else "（⚠ 最近一次设置失败）")
