# -*- coding: utf-8 -*-
"""把仓库根的 app/core 同步到 mobile/src/app（构建 APK 前调用）。

桌面仓库 app/ 是唯一源；src/app 是打包副本（每次构建重新生成，不手工改）。
只打包 core（手机 UI 不用 PySide6，排除 app/ui 与桌面入口，减小体积）。

用法：
    python sync_core.py            # 执行同步
    python sync_core.py --check    # 只报告差异，不写任何文件

退出码：0 = 已一致 / 同步成功；1 = 存在差异（--check）或出错。
"""
import hashlib
import os
import shutil
import sys

try:  # Windows 控制台可能是 GBK：中文与箭头符号别让脚本自己崩掉
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))            # jiuguan-mobile/
# 手机端与桌面主项目已分为两个目录；桌面项目路径可用环境变量覆盖
REPO = os.environ.get(
    "JIUGUAN_REPO", r"E:\AI Agent\work area\酒馆小说生成器"
)
SRC_APP = os.path.join(ROOT, "src", "app")
DST_APP = os.path.join(REPO, "app")


def _md5(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def _py_map(folder: str) -> dict:
    if not os.path.isdir(folder):
        return {}
    return {f: _md5(os.path.join(folder, f))
            for f in os.listdir(folder) if f.endswith(".py")}


def pair_diff():
    """比较两边 app/core 的 .py 文件。

    返回 (桌面独有, 手机独有, 内容不同)；桌面源目录不存在时返回 None。
    """
    src_core = os.path.join(DST_APP, "core")
    if not os.path.isdir(src_core):
        return None
    a = _py_map(src_core)
    b = _py_map(os.path.join(SRC_APP, "core"))
    return (sorted(set(a) - set(b)),
            sorted(set(b) - set(a)),
            sorted(f for f in set(a) & set(b) if a[f] != b[f]))


def check() -> int:
    """只报告差异，不写任何文件。返回退出码。"""
    d = pair_diff()
    if d is None:
        print("找不到桌面源目录: " + os.path.join(DST_APP, "core"))
        return 1
    only_desktop, only_mobile, changed = d
    if not (only_desktop or only_mobile or changed):
        print("已同步：手机版 app/core 与桌面完全一致")
        return 0
    print("手机版内核落后于桌面版：")
    for f in only_desktop:
        print("  桌面新增  " + f)
    for f in only_mobile:
        print("  手机独有  " + f + "（同步会删除，请确认是否需要保留）")
    for f in changed:
        print("  内容不同  " + f)
    total = len(only_desktop) + len(only_mobile) + len(changed)
    print("共 %d 处差异 -> 运行 python sync_core.py 同步" % total)
    return 1


def sync():
    src_core = os.path.join(SRC_APP, "core")
    dst_core = os.path.join(DST_APP, "core")
    if not os.path.isdir(dst_core):
        print("找不到源目录: " + dst_core)
        sys.exit(1)
    d = pair_diff() or ([], [], [])
    if d[1]:
        # 只对齐 core：手机侧独有的 core 文件会被清掉，先说清楚再动手
        print("注意：以下文件只存在于手机版，同步后会被删除：")
        for f in d[1]:
            print("  " + f)
    os.makedirs(SRC_APP, exist_ok=True)
    shutil.copy2(os.path.join(DST_APP, "__init__.py"),
                 os.path.join(SRC_APP, "__init__.py"))
    # 只重建 core 子目录（不整体删 src/app）：将来手机侧若在 app/ 下放别的文件，
    # 不会被同步误删。
    if os.path.isdir(src_core):
        shutil.rmtree(src_core)
    shutil.copytree(
        dst_core, src_core,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    n = len([f for f in os.listdir(src_core) if f.endswith(".py")])
    print("已同步 app/core -> jiuguan-mobile/src/app/core（%d 个文件）" % n)


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(check())
    sync()
    sys.exit(check())
