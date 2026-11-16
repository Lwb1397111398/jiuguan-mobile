# -*- coding: utf-8 -*-
"""路径工具：应用数据目录（双端共用）。

桌面版：%APPDATA%/酒馆小说生成器（历史行为不变）。
安卓版（Flet 打包）：应用私有数据目录下的 酒馆小说生成器/ 子目录。
环境变量 JIUGUAN_DATA_DIR 可显式指定数据根目录（调试/迁移工具用）。

从 appstate 独立出来，供 appstate 与 global_store 共用（避免循环导入）。
"""
import json
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


def _cache_root_from_tmpdir(tmp: str) -> str:
    """TMPDIR → 应用私有根（去掉 /cache 及其以下）。推不出返回空串。

    2026-11-16 平板真机：TMPDIR **不一定**正好是 `<pkg>/cache`——带尾斜杠、
    或指到 `<pkg>/cache/tmp` 之类子目录时，旧版 `endswith("/cache")` 判不中，
    推导失败后退回 TMPDIR 本身 → 数据目录落进 cache（系统清缓存=丢书）。
    这里按路径段找 `/cache`，只要 TMPDIR 落在缓存树下就一定能推出根。
    """
    p = (tmp or "").replace("\\", "/").rstrip("/")
    idx = p.find("/cache")            # 取最左的 /cache：越靠左越接近应用根
    if idx <= 0:                      # 没命中，或 TMPDIR 就是 "/cache"
        return ""
    tail = p[idx + len("/cache"):]
    if tail and not tail.startswith("/"):
        return ""                     # 形如 /cacheXxx，不是缓存根
    return p[:idx]


def is_cache_path(path: str) -> bool:
    """路径是否落在某个 cache 目录树下（数据目录的禁区）。"""
    p = (path or "").replace("\\", "/")
    idx = p.find("/cache")
    if idx < 0:
        return False
    tail = p[idx + len("/cache"):]
    return not tail or tail.startswith("/")


def android_files_dir_from_env() -> str:
    """不依赖 pyjnius 推导应用私有 files 目录：TMPDIR 在 `<pkg>/cache` 树下 →
    同根 `<pkg>/files`。推导不出或目录不可写返回空串。

    2026-11-16 平板真机实测：pyjnius 桥接整链失败（autoclass 抛异常），
    数据目录落到 cache——系统清缓存会清掉项目数据。cache 与 files 同在
    应用私有根下，TMPDIR 直接给出 cache 路径，零依赖即可推出 files。
    供本模块、手机工程 bootstrap.py 与 update.py 共用。
    """
    root = _cache_root_from_tmpdir(os.environ.get("TMPDIR") or "")
    if not root:
        return ""
    files = os.path.normpath(root.replace("/", os.sep) + os.sep + "files")
    if is_cache_path(files):
        return ""                     # 推出来的还在缓存树下，宁可不推
    if os.path.isdir(files):
        return files if os.access(files, os.W_OK) else ""
    # files 目录理论上必然存在；个别 ROM/权限异常时尝试建一次，建不出就放弃
    parent = os.path.dirname(files)
    if os.path.isdir(parent) and os.access(parent, os.W_OK):
        try:
            os.makedirs(files, exist_ok=True)
            return files
        except OSError:
            return ""
    return ""


def remap_legacy_cache_path(path: str, data_dir: str) -> str:
    """旧版落 cache 时记下的绝对路径 → 当前数据目录下的同位置路径。

    纯字符串换算，不判断目标是否存在（调用方自己查）。用于把
    `<任意>/cache/.../酒馆小说生成器/projects/x` 改写成
    `<当前数据目录>/projects/x`。
    """
    if not path or not data_dir:
        return path
    norm = path.replace("\\", "/")
    seg = "/" + APP_DIR_NAME + "/"
    idx = norm.rfind(seg)
    if idx < 0:
        return path
    if not is_cache_path(norm[:idx]):
        return path
    return os.path.join(data_dir, norm[idx + len(seg):].replace("/", os.sep))


def _rewrite_config_paths(cfg_path: str, pairs) -> bool:
    """把 config.json 里 recent_projects 的旧路径前缀换成本机新路径。

    数据目录从 cache 搬到 files 时，config.json 里的项目路径还是 cache 的；
    只搬文件不改路径 → 首页所有书记录都指向已删除的目录（灰卡片、点不开）。
    pairs: [(旧目录, 新目录), ...]
    """
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return False
    items = cfg.get("recent_projects") if isinstance(cfg, dict) else None
    if not isinstance(items, list):
        return False
    changed = False
    for it in items:
        if not isinstance(it, dict):
            continue
        p = it.get("path") or ""
        for old, new in pairs:
            old_n = old.rstrip("/\\")
            if not old_n or not p:
                continue
            if os.path.normcase(p) == os.path.normcase(old_n):
                it["path"] = new
                changed = True
                break
            prefix = old_n + "/"
            if os.path.normcase(p).startswith(os.path.normcase(prefix)):
                it["path"] = os.path.join(new, p[len(prefix):])
                changed = True
                break
    if changed:
        try:
            tmp_path = cfg_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, cfg_path)
        except OSError:
            return False
    return changed


def _migrate_data_dir_away_from_cache(new_path: str) -> None:
    """数据目录从 cache 搬到 files（系统清缓存即丢项目数据）。

    两种旧位置都要认：
    - `<TMPDIR>/酒馆小说生成器`（TMPDIR 本身就在缓存树下时的兜底落点，
      2026-11-16 平板真机就是这个形状）；
    - `<应用根>/cache/酒馆小说生成器`（TMPDIR 曾被设成缓存子目录时）。

    只搬一次：copytree 成功后先写标记再删旧目录；rmtree 失败（文件占用等）
    下次启动看到标记只重删不重拷，避免旧快照覆盖 files 上已更新的数据。
    **搬完必须改写 config.json 里的项目路径**——否则首页记录全指向已删除的
    cache 目录（灰卡片、点不开）。任何失败静默保留 cache，绝不崩 App。
    调用时机在 App 装配最早期、任何数据库打开之前。
    """
    tmp = os.environ.get("TMPDIR") or ""
    root = _cache_root_from_tmpdir(tmp)
    if not root:
        return
    new = os.path.abspath(new_path)
    if is_cache_path(new):
        return                        # 目标本身还在 cache 里，搬了等于没搬
    cands = [os.path.abspath(os.path.join(tmp, APP_DIR_NAME))]
    legacy = os.path.abspath(os.path.join(root.replace("/", os.sep), "cache",
                                          APP_DIR_NAME))
    if os.path.normcase(legacy) not in [os.path.normcase(c) for c in cands]:
        cands.append(legacy)
    pairs = []
    for old in cands:
        if old == new or not os.path.isdir(old):
            continue
        marker = os.path.join(old, ".migrated_to_files")
        if os.path.exists(marker):
            try:
                shutil.rmtree(old)
            except Exception:
                pass
            continue
        try:
            shutil.copytree(old, new, dirs_exist_ok=True)
            pairs.append((old, new))
            open(os.path.join(old, ".migrated_to_files"), "w").close()
            shutil.rmtree(old)
        except Exception:
            pass
    if pairs:
        _rewrite_config_paths(os.path.join(new, "config.json"), pairs)


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
        # 红线：数据目录绝不留在 cache（系统清缓存=丢书）。上面的顺序已经优先
        # files，唯独 ROM 把 filesDir 报成 cache 这类歪路径会在这一步兜住。
        if is_cache_path(path):
            files_root = android_files_dir_from_env()
            if files_root:
                path = os.path.join(files_root, APP_DIR_NAME)
        _migrate_data_dir_away_from_cache(path)
    else:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        path = os.path.join(base, APP_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path
