# -*- coding: utf-8 -*-
"""Qt 兼容层：让 core 层在无 PySide6 的环境（安卓 Flet 打包）下可运行。

桌面版装有 PySide6，直接用真 Qt 信号，行为与历史版本完全一致；
安卓版（flet build apk 的 Python 运行时）没有 PySide6，回退到纯 Python 实现。

core 里只用到了 QObject 继承 + 若干无参 Signal（store.ProjectStore 的
chapters_changed / volumes_changed / synopsis_changed 等，emit-only，
connect 发生在 UI 层），所以这里只需模拟这一小撮接口。
"""
from __future__ import annotations

import sys
import traceback

try:  # 桌面版：真 Qt
    from PySide6.QtCore import QObject, Signal  # noqa: F401

    USING_REAL_QT = True
except ImportError:  # 安卓/纯 Python 环境
    USING_REAL_QT = False

    class _BoundSignal:
        """单个信号实例：connect/emit/disconnect，签名对齐 Qt 用法。"""

        def __init__(self):
            self._slots = []

        def connect(self, slot):
            if slot not in self._slots:
                self._slots.append(slot)

        def disconnect(self, slot=None):
            if slot is None:
                self._slots.clear()
            elif slot in self._slots:
                self._slots.remove(slot)

        def emit(self, *args):
            # 对齐 Qt 语义：某个槽抛异常不影响其余槽，打印后继续
            for slot in list(self._slots):
                try:
                    slot(*args)
                except Exception:
                    traceback.print_exc(file=sys.stderr)

    class Signal:
        """类属性描述符：每个 QObject 实例获得独立的槽列表。"""

        def __init__(self, *args, **kwargs):  # 接受 Qt 的 Signal(...) 类型参数
            self._name = ""

        def __set_name__(self, owner, name):
            self._name = name

        def __get__(self, obj, objtype=None):
            if obj is None:
                return self
            key = "_sig_" + self._name
            bound = obj.__dict__.get(key)
            if bound is None:
                bound = _BoundSignal()
                obj.__dict__[key] = bound
            return bound

    class QObject:
        def __init__(self, *args, **kwargs):
            super().__init__()
