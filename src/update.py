# -*- coding: utf-8 -*-
"""应用内自动更新（手机版专属）：检查 → 下载 → 唤起系统安装器。

云端：GitHub 公开仓库 jiuguan-mobile 的 latest Release（tag 固定 latest、
资产文件名固定 jiuguan-novel.apk，永远只有「最新版」一个入口）。
Release 说明第一行是 HTML 注释元数据（GitHub 页面不渲染，App 用正则解析）：

    <!-- appupdate versionCode=21 versionName=0.1.21 -->

网络链（skill 2026-10 实测）：国内运营商 DNS 会污染 api.github.com，查询走
「直连 → 透传镜像」逐源试，任一成功记下通道前缀（调用方存 update_source_prefix，
下次查询和下载都优先复用）。镜像透传能力不一样，用前先验证：
gh-proxy.com / gh-proxy.org 能透传 API；ghfast.top / ghproxy.net 只透传下载
（API 返 403）——所以它们只进下载候选，不进查询链。

安装：flet 模板 AndroidManifest 已内置 FileProvider（authority 为
"<包名>.provider"，res/xml/provider_paths.xml 已暴露 cacheDir 整个目录），
APK 落 cacheDir/updates/ 后拿 content:// URI，ACTION_INSTALL_PACKAGE 唤起
系统安装器。pyjnius 由 serious_python_android 带进运行时（release 构建的
ProGuard keep 规则已保留其引导类）。

本模块保持纯逻辑（无 page 控件依赖）：UI 接线见 update_ui.py。
"""
import os
import re
import tempfile
import time

import requests

REPO = "Lwb1397111398/jiuguan-mobile"
ASSET_NAME = "jiuguan-novel.apk"
USER_AGENT = "jiuguan-novel-updater"
API_PATH = f"/repos/{REPO}/releases/latest"

# 查询链：直连 → 能透传 GitHub API 的镜像（skill 4.1 实测，镜像会倒闭/改规则，
# 新增或替换时先用 curl -A <UA> 验证返回真 JSON）
QUERY_PREFIXES = ["", "https://gh-proxy.com/", "https://gh-proxy.org/"]
# 下载候选：查询链 + 只透传文件下载的镜像（API 403 但 APK 直链可用）
DOWNLOAD_PREFIXES = QUERY_PREFIXES + [
    "https://ghfast.top/",
    "https://ghproxy.net/",
]

AUTO_CHECK_INTERVAL = 24 * 3600     # 启动静默检查节流：24h
CONNECT_TIMEOUT = 10                # 连接超时（秒）
READ_TIMEOUT = 30                   # 读超时（秒）
DOWNLOAD_CHUNK = 64 * 1024          # 流式缓冲（skill 2.4）

FLAG_GRANT_READ_URI_PERMISSION = 1          # android.content.Intent
FLAG_ACTIVITY_NEW_TASK = 0x10000000


class NoReleaseError(Exception):
    """所有源都返回 404 = 云端还没发布过任何版本（与网络失败分开）。"""


class NetworkError(Exception):
    """直连与镜像都连不上（文案要写「都失败」，别误导用户等 CI）。"""


def _headers(extra=None):
    h = {"User-Agent": USER_AGENT,
         "Accept": "application/vnd.github+json"}
    if extra:
        h.update(extra)
    return h


def is_android() -> bool:
    """跑在 Android 运行时（pyjnius 可用且宿主 Activity 就绪）？桌面预览为 False。"""
    global _ANDROID
    try:
        return _ANDROID
    except NameError:
        pass
    try:
        from jnius import autoclass
        act = autoclass("com.flet.serious_python_android.PythonActivity")
        _ANDROID = bool(act.mActivity)
    except Exception:
        _ANDROID = False
    return _ANDROID


def get_local_version():
    """本地版本 (versionCode, versionName)。非 Android 返回 (0, "dev")。"""
    if not is_android():
        return (0, "dev")
    from jnius import autoclass
    act = autoclass("com.flet.serious_python_android.PythonActivity")
    ctx = act.mActivity.getApplicationContext()
    info = ctx.getPackageManager().getPackageInfo(ctx.getPackageName(), 0)
    return (int(info.versionCode), str(info.versionName or ""))


def get_download_dir():
    """更新包落盘目录：Android 用应用私有 cacheDir/updates（FileProvider 已暴露）；
    桌面预览退回系统临时目录。"""
    if is_android():
        from jnius import autoclass
        act = autoclass("com.flet.serious_python_android.PythonActivity")
        ctx = act.mActivity.getApplicationContext()
        base = ctx.getCacheDir().getAbsolutePath()
    else:
        base = tempfile.gettempdir()
    d = os.path.join(str(base), "updates")
    os.makedirs(d, exist_ok=True)
    return d


def parse_release(data, source_prefix=""):
    """解析 releases/latest 的 JSON → UpdateInfo；不是我们的发版格式返回 None。"""
    body = data.get("body") or ""
    first = body.splitlines()[0].strip() if body.strip() else ""
    m = re.search(
        r"<!--\s*appupdate\s+versionCode=(\d+)\s+versionName=([^\s>-]+)\s*-->",
        first)
    if not m:
        return None
    assets = data.get("assets") or []
    apk = next((a for a in assets if a.get("name") == ASSET_NAME), None)
    if apk is None:
        apk = next((a for a in assets
                    if str(a.get("name") or "").lower().endswith(".apk")), None)
    if apk is None:
        return None
    # 更新说明：body 去掉首行元数据后的剩余文本（CI 写的是最近提交标题）
    notes = "\n".join(body.splitlines()[1:]).strip()
    return UpdateInfo(
        version_code=int(m.group(1)),
        version_name=m.group(2),
        apk_url=apk.get("browser_download_url") or "",
        size=int(apk.get("size") or 0),
        notes=notes,
        title=data.get("name") or "",
        source_prefix=source_prefix,
    )


class UpdateInfo:
    """一次检查的结果。source_prefix 记录成功通道，下载时优先复用。"""

    def __init__(self, version_code, version_name, apk_url, size=0,
                 notes="", title="", source_prefix=""):
        self.version_code = int(version_code)
        self.version_name = str(version_name or "")
        self.apk_url = str(apk_url or "")
        self.size = int(size or 0)
        self.notes = str(notes or "")
        self.title = str(title or "")
        self.source_prefix = str(source_prefix or "")

    def has_update(self, local_version_code):
        """远端 versionCode 更大才算有更新（重装同版本走「检查」按钮也不误报）。"""
        return self.version_code > int(local_version_code)

    def describe(self):
        s = f"新版本 {self.version_name}（build {self.version_code}）"
        if self.title:
            s += f"\n{self.title}"
        if self.notes:
            s += f"\n\n{self.notes}"
        return s


def check_update(local_version_code, preferred_prefix=""):
    """查最新版并比较。远端不比本地新返回 None；远端有更新返回 UpdateInfo。

    preferred_prefix：上次成功通道（存储键 update_source_prefix），本次优先试。
    全部源 404 → NoReleaseError（云端没发布）；其它全败 → NetworkError。
    """
    chain = list(QUERY_PREFIXES)
    if preferred_prefix and preferred_prefix in chain:
        chain.remove(preferred_prefix)
        chain.insert(0, preferred_prefix)
    saw_404 = False
    saw_404_count = 0
    errors = []
    for prefix in chain:
        url = prefix + "https://api.github.com" + API_PATH
        try:
            r = requests.get(url, headers=_headers(),
                             timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        except requests.RequestException as e:
            errors.append(f"{prefix or '直连'}: {type(e).__name__}")
            continue
        if r.status_code == 404:
            # 公开仓库匿名 404 = 该源认为没有 latest Release
            saw_404 = True
            saw_404_count += 1
            errors.append(f"{prefix or '直连'}: 404")
            continue
        try:
            r.raise_for_status()
            info = parse_release(r.json(), source_prefix=prefix)
        except ValueError:
            errors.append(f"{prefix or '直连'}: 返回内容不是 JSON")
            continue
        except requests.RequestException as e:
            errors.append(f"{prefix or '直连'}: {type(e).__name__}")
            continue
        if info is None or not info.has_update(local_version_code):
            return None
        return info
    if saw_404 and len(errors) == saw_404_count:
        raise NoReleaseError("云端还没有发布过任何版本")
    if errors:
        raise NetworkError("直连与镜像都失败：" + "；".join(errors))
    raise NetworkError("检查更新失败")


def download_update(info, progress=None):
    """流式下载 APK 到 cacheDir/updates/update-<versionCode>.apk。

    先写 .part → 校验 Content-Length 与实际字数一致 → rename 正式名；
    已存在同版本同大小文件直接复用不重下。逐源轮换，.part 由下一候选从头覆盖。
    progress(done_bytes, total_bytes) 在工作线程回调。
    返回落盘路径。
    """
    dest = os.path.join(get_download_dir(), f"update-{info.version_code}.apk")
    if info.size > 0 and os.path.exists(dest) and \
            os.path.getsize(dest) == info.size:
        return dest
    part = dest + ".part"
    candidates = [info.source_prefix] if info.source_prefix else []
    candidates += [p for p in DOWNLOAD_PREFIXES if p not in candidates]
    last_err = None
    for prefix in candidates:
        url = prefix + info.apk_url
        try:
            with requests.get(url, headers=_headers(), stream=True,
                              timeout=(CONNECT_TIMEOUT, 120),
                              allow_redirects=True) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length") or 0) or info.size
                done = 0
                with open(part, "wb") as f:
                    for chunk in r.iter_content(DOWNLOAD_CHUNK):
                        if not chunk:
                            continue
                        f.write(chunk)
                        done += len(chunk)
                        if progress:
                            try:
                                progress(done, total)
                            except Exception:
                                pass
            if total and done != total:
                raise IOError(f"下载不完整（{done}/{total} 字节）")
            os.replace(part, dest)
            return dest
        except Exception as e:      # 网络/校验失败换下一源，不留半截包
            last_err = e
            try:
                if os.path.exists(part) and os.path.getsize(part) == 0:
                    os.remove(part)
            except OSError:
                pass
            continue
    raise NetworkError(f"下载失败（直连与镜像都试过）：{last_err}")


def install_apk(apk_path):
    """唤起系统安装器。

    返回 True = 已跳安装器；False = 缺「安装未知应用」授权，已跳系统开关页，
    用户开完回来再点一次「安装更新」。非 Android 抛 RuntimeError。
    """
    if not is_android():
        raise RuntimeError("安装更新仅手机版可用（桌面预览模式）")
    from jnius import autoclass
    PythonActivity = autoclass("com.flet.serious_python_android.PythonActivity")
    activity = PythonActivity.mActivity
    File = autoclass("java.io.File")
    FileProvider = autoclass("androidx.core.content.FileProvider")
    Intent = autoclass("android.content.Intent")
    Uri = autoclass("android.net.Uri")
    Settings = autoclass("android.provider.Settings")

    ctx = activity.getApplicationContext()
    pkg = str(ctx.getPackageName())
    file = File(str(apk_path))
    # authority 与 flet 模板 AndroidManifest 内置 FileProvider 一致
    uri = FileProvider.getUriForFile(activity, pkg + ".provider", file)

    # Android 8+ 需要「安装未知应用」逐应用授权（canRequestPackageInstalls 为
    # API 26 方法，老设备查不到就当作未授权走开关页）
    try:
        allowed = bool(
            ctx.getPackageManager().canRequestPackageInstalls())
    except Exception:
        allowed = False
    if not allowed:
        intent = Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                        Uri.parse(f"package:{pkg}"))
        intent.addFlags(FLAG_ACTIVITY_NEW_TASK)
        activity.startActivity(intent)
        return False

    intent = Intent(Intent.ACTION_INSTALL_PACKAGE)
    intent.setData(uri)
    intent.addFlags(FLAG_GRANT_READ_URI_PERMISSION)
    intent.addFlags(FLAG_ACTIVITY_NEW_TASK)
    activity.startActivity(intent)
    return True


def should_auto_check(last_check_epoch, now=None):
    """启动静默检查节流：距上次成功检查超过 24h（或从没查过）才查。"""
    if not last_check_epoch:
        return True
    now = time.time() if now is None else now
    try:
        return (now - float(last_check_epoch) >= AUTO_CHECK_INTERVAL)
    except (TypeError, ValueError):
        return True
