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
