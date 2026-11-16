# -*- coding: utf-8 -*-
"""路径工具：应用数据目录（双端共用）。

桌面版：%APPDATA%/酒馆小说生成器（历史行为不变）。
安卓版（Flet 打包）：应用私有数据目录下的 酒馆小说生成器/ 子目录。
环境变量 JIUGUAN_DATA_DIR 可显式指定数据根目录（调试/迁移工具用）。

从 appstate 独立出来，供 appstate 与 global_store 共用（避免循环导入）。
"""
import os
import shutil
import sys

APP_DIR_NAME = "酒馆小说生成器"


def is_android() -> bool:
    """是否运行在安卓（Flet APK 的 CPython 运行时）。

    这是双端唯一的安卓判定源，必须宽、且绝不依赖 pyjnius（2026-11-16
    平板真机实测：pyjnius 桥接整链失败，凡是用它判平台的地方都误判）。

    - CPython 3.13+ 在 Android 上 sys.platform 报 "android"（PEP 738），
      更早版本报 "linux"——两种都要认。手机版 flet 打包用 3.14，只认
      "linux" 会把真机判成电脑（表现为更新页显示「电脑预览」并锁死）。
    - linux 时再查系统标记：ANDROID_DATA / ANDROID_ROOT（Android 进程必设）、
      /data/data 存在（Android 11+ 是 /data/user/0 的软链，stat 可用）。
    - 最后看 TMPDIR 是否落在 /data/ 下：安卓应用私有目录特征
      （/data/user/0/<pkg>/cache），桌面 Linux 是 /tmp 或 /var/...，不会误判。
    """
    if sys.platform == "android":
        return True
    if sys.platform != "linux":
        return False
    if "ANDROID_DATA" in os.environ or "ANDROID_ROOT" in os.environ:
        return True
    if os.path.isdir("/data/data"):
        return True
    return str(os.environ.get("TMPDIR") or "").startswith("/data/")


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


def android_files_dir_from_env() -> str:
    """不依赖 pyjnius 推导应用私有 files 目录：TMPDIR=.../<pkg>/cache →
    同根 .../<pkg>/files。推导不出、目录不存在或不可写返回空串。

    2026-11-16 平板真机实测：pyjnius 桥接整链失败（autoclass 抛异常），
    数据目录落到 cache——系统清缓存会清掉项目数据。cache 与 files 同在
    应用私有根下，TMPDIR 直接给出 cache 路径，零依赖即可推出 files。
    供本模块、手机工程 bootstrap.py 与 update.py 共用。
    """
    tmp = os.environ.get("TMPDIR") or ""
    if tmp.endswith("/cache"):
        files = os.path.normpath(os.path.dirname(tmp) + "/files")
        if os.path.isdir(files) and os.access(files, os.W_OK):
            return files
    return ""


def _migrate_data_dir_away_from_cache(new_path: str) -> None:
    """0.1.44 及以前 pyjnius 失败时数据落 cache（系统清缓存即丢项目数据）；
    0.1.45 起落 files，首次启动把 cache 旧数据整体搬到 files。

    只搬一次：copytree 成功后先写标记再删旧目录；rmtree 失败（文件占用等）
    下次启动看到标记只重删不重拷，避免旧快照覆盖 files 上已更新的数据。
    任何失败静默保留 cache，绝不崩 App。调用时机在 App 装配最早期、
    任何数据库打开之前。
    """
    tmp = os.environ.get("TMPDIR") or ""
    if not tmp.endswith("/cache"):
        return
    old = os.path.abspath(os.path.join(tmp, APP_DIR_NAME))
    new = os.path.abspath(new_path)
    if old == new or not os.path.isdir(old):
        return
    marker = os.path.join(old, ".migrated_to_files")
    if os.path.exists(marker):
        try:
            shutil.rmtree(old)
        except Exception:
            pass
        return
    try:
        shutil.copytree(old, new, dirs_exist_ok=True)
        open(marker, "w").close()
        shutil.rmtree(old)
    except Exception:
        pass


def app_data_dir() -> str:
    """应用数据目录：Windows=%APPDATA%/酒馆小说生成器，安卓=应用私有目录同名子目录。"""
    override = os.environ.get("JIUGUAN_DATA_DIR")
    if override:
        path = override
    elif is_android():
        # 权威顺序：Context.filesDir（pyjnius，永不清）→ TMPDIR 推导 files
        # （pyjnius 失败时的等价物）→ HOME（旧运行时设过）→ TMPDIR（应用 cache，
        # 系统可清，仅剩最后兜底用）→ /data/local/tmp（最终兜底）
        base = (_android_app_dir("getFilesDir")
                or android_files_dir_from_env()
                or os.environ.get("HOME")
                or os.environ.get("TMPDIR")
                or "/data/local/tmp")
        path = os.path.join(base, APP_DIR_NAME)
        _migrate_data_dir_away_from_cache(path)
    else:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        path = os.path.join(base, APP_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path
