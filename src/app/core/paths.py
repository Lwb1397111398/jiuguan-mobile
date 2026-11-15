# -*- coding: utf-8 -*-
"""路径工具：应用数据目录（双端共用）。

桌面版：%APPDATA%/酒馆小说生成器（历史行为不变）。
安卓版（Flet 打包）：应用私有数据目录下的 酒馆小说生成器/ 子目录。
环境变量 JIUGUAN_DATA_DIR 可显式指定数据根目录（调试/迁移工具用）。

从 appstate 独立出来，供 appstate 与 global_store 共用（避免循环导入）。
"""
import os
import sys

APP_DIR_NAME = "酒馆小说生成器"


def is_android() -> bool:
    """是否运行在安卓（Flet APK 的 CPython 运行时）。"""
    if sys.platform == "android":
        return True
    # flet 安卓运行时是 Linux 内核 + ANDROID 环境标记
    return (sys.platform == "linux"
            and ("ANDROID_DATA" in os.environ or os.path.isdir("/data/data")))


def _android_app_dir(method: str) -> str:
    """经 pyjnius 拿安卓应用私有目录（getFilesDir / getCacheDir），拿不到返回空串。

    安卓应用进程没有 HOME 环境变量（flet 的 dart-bridge 运行时也不设），
    expanduser("~") 会问 bionic 伪 passwd 表，应用 uid 的 pw_dir 被合成成
    "/data"——应用无权写 /data 根目录，makedirs 直接 PermissionError
    （2026-11-15 真机首启即崩的根因）。所以安卓分支绝不许用 expanduser，
    权威来源是应用 Context。pyjnius 用法与手机工程 src/update.py 同源。
    """
    try:
        from jnius import autoclass  # 仅安卓包内置；桌面/测试不会走到这里
        act = autoclass("com.flet.serious_python_android.PythonActivity")
        ctx = act.mActivity.getApplicationContext()
        f = getattr(ctx, method)()
        return str(f.getAbsolutePath()) if f else ""
    except Exception:
        return ""


def app_data_dir() -> str:
    """应用数据目录：Windows=%APPDATA%/酒馆小说生成器，安卓=应用私有目录同名子目录。"""
    override = os.environ.get("JIUGUAN_DATA_DIR")
    if override:
        path = override
    elif is_android():
        # 顺序：Context.filesDir（权威、永不清）→ HOME（旧运行时设过）→
        # TMPDIR（部分系统给到应用 cache）→ /data/local/tmp（最终兜底）
        base = (_android_app_dir("getFilesDir")
                or os.environ.get("HOME")
                or os.environ.get("TMPDIR")
                or "/data/local/tmp")
        path = os.path.join(base, APP_DIR_NAME)
    else:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        path = os.path.join(base, APP_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path
