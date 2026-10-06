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


def app_data_dir() -> str:
    """应用数据目录：Windows=%APPDATA%/酒馆小说生成器，安卓=应用私有目录同名子目录。"""
    override = os.environ.get("JIUGUAN_DATA_DIR")
    if override:
        path = override
    elif is_android():
        # flet 安卓运行时 HOME 指向应用私有 files 目录；无 HOME 时退缓存目录
        base = os.environ.get("HOME") or os.path.expanduser("~") or "/data/local/tmp"
        path = os.path.join(base, APP_DIR_NAME)
    else:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        path = os.path.join(base, APP_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path
