# -*- coding: utf-8 -*-
"""手机版启动引导：让打包环境（src 为根）与桌面开发环境（仓库根）都能 import core。

打包后：src/ 整体是 Python 源根，src/app 即 app 包。
桌面开发：仓库根加入 sys.path 后 import app.core（与本仓库桌面版同一份代码）。
"""
import os
import sys

_SRC = os.path.dirname(os.path.abspath(__file__))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_REPO = os.path.dirname(os.path.dirname(_SRC))
if os.path.isdir(os.path.join(_REPO, "app", "core")) and _REPO not in sys.path:
    sys.path.insert(0, _REPO)

# 安卓：趁早把 HOME 钉到应用私有 files 目录。
# flet 的 dart-bridge 运行时不设 HOME，安卓进程 expanduser("~") 会经 bionic
# 伪 passwd 表落到 /data（应用无权写，2026-11-15 真机首启即崩的根因）。
# 标准库里任何读 ~ 的代码（tempfile、Path.home() 等）都受益。
# 平台判定统一走内核 is_android（唯一源：认 "android" 平台名 + TMPDIR 特征，
# 2026-11-16 平板真机：CPython 3.14 在安卓上 sys.platform 是 "android"）；
# 引导阶段 import 失败也不能让 App 起不来，故留一份等价兜底。
def _android_like() -> bool:
    try:
        from app.core.paths import is_android
        return bool(is_android())
    except Exception:
        pass
    if sys.platform == "android":
        return True
    if sys.platform != "linux":
        return False
    return (os.path.isdir("/data/data")
            or "ANDROID_DATA" in os.environ
            or "ANDROID_ROOT" in os.environ
            or str(os.environ.get("TMPDIR") or "").startswith("/data/"))


if _android_like() and not os.environ.get("HOME"):
    _files = ""
    try:
        from jnius import autoclass
        _act = autoclass("com.flet.serious_python_android.PythonActivity")
        _files = _act.mActivity.getApplicationContext().getFilesDir().getAbsolutePath()
    except Exception:
        pass
    if not _files:
        try:
            from app.core.paths import android_files_dir_from_env
            _files = android_files_dir_from_env()
        except Exception:
            _files = ""
    if _files:
        os.environ["HOME"] = str(_files)
