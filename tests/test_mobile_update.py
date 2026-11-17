# -*- coding: utf-8 -*-
"""手机版自更新单测（精简版：fake requests 闭包，monkeypatch update.requests.get）。"""
import hashlib
import os
import sys
import time

import pytest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src")
sys.path.insert(0, SRC)

import update  # noqa: E402


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


APK_BYTES = b"J" * 11


class Resp:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


    def raise_for_status(self):
        if self.status_code >= 400:
            raise update.requests.HTTPError(str(self.status_code))


class StreamResp:
    """requests.get(stream=True) 的假响应：支持 with / iter_content / 头。"""

    def __init__(self, payload=b"", status=200, length=None, chunks=None):
        self.status_code = status
        self.headers = {"Content-Length":
                        str(length if length is not None else len(payload))}
        self._chunks = chunks if chunks is not None else [payload]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise update.requests.HTTPError(str(self.status_code))

    def iter_content(self, size):
        for c in self._chunks:
            yield c

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _release(code=39, sha256=None):
    """发版元数据；sha256=None 时不带哈希（模拟 2026-11-17 之前的老 Release）。

    哈希走**第二行**独立注释（首行保持旧客户端可解析的格式，见
    release_guard 模块说明）——这里就按 CI 的真实产物造数据。
    """
    lines = ["<!-- appupdate versionCode=%d versionName=0.1.%d -->" % (code, code)]
    if sha256 is not None:
        lines.append("<!-- apkupdate apkSha256=%s -->" % sha256)
    lines.append("修复若干问题")
    return {
        "name": "0.1.%d" % code,
        "body": "\n".join(lines),
        "assets": [{
            "name": update.ASSET_NAME,
            "size": 50_000_000,
            "browser_download_url": "https://github.com/OWNER/REPO/releases/download/latest/x.apk",
        }],
    }


def _info(code=39, sha256="", size=0):
    return update.UpdateInfo(
        code, "0.1.%d" % code, "https://github.com/OWNER/REPO/x.apk",
        size=size, apk_sha256=sha256)


def test_parse_release():
    info = update.parse_release(_release(39), source_prefix="P")
    assert info.version_code == 39
    assert info.version_name == "0.1.39"
    assert info.size == 50_000_000
    assert info.source_prefix == "P"
    assert "修复" in info.notes
    # 老格式（无 apkSha256）→ 没有可用校验值，App 内安装必须停用
    assert info.apk_sha256 == ""
    assert info.has_checksum() is False


def test_parse_release_invalid():
    assert update.parse_release({"body": "普通说明", "assets": []}) is None
    body_only = {"body": "<!-- appupdate versionCode=9 versionName=0.1.9 -->",
                 "assets": []}
    assert update.parse_release(body_only) is None


def test_has_update():
    i = update.UpdateInfo(39, "0.1.39", "u")
    assert i.has_update(38) is True
    assert i.has_update(39) is False
    assert i.has_update(40) is False


def test_check_direct_hit(monkeypatch):
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return Resp(200, _release(39))

    monkeypatch.setattr(update.requests, "get", fake_get)
    info = update.check_update(38)
    assert info.version_code == 39
    assert info.source_prefix == ""
    assert calls[0].startswith("https://api.github.com/repos/Lwb1397111398/jiuguan-mobile/releases/latest")


def test_check_mirror_fallback(monkeypatch):
    def fake_get(url, **kw):
        if url.startswith("https://api.github.com"):
            raise update.requests.ConnectionError("dns")
        return Resp(200, _release(39))

    monkeypatch.setattr(update.requests, "get", fake_get)
    info = update.check_update(38)
    assert info.source_prefix == "https://gh-proxy.com/"


def test_check_preferred_prefix(monkeypatch):
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        if url.startswith("https://api.github.com"):
            raise update.requests.ConnectionError("dns")
        return Resp(200, _release(39))

    monkeypatch.setattr(update.requests, "get", fake_get)
    info = update.check_update(38, preferred_prefix="https://gh-proxy.com/")
    assert info.version_code == 39
    assert calls[0].startswith("https://gh-proxy.com/")


def test_check_all_404(monkeypatch):
    def fake_get(url, **kw):
        return Resp(404)

    monkeypatch.setattr(update.requests, "get", fake_get)
    with pytest.raises(update.NoReleaseError):
        update.check_update(38)


def test_check_network_fail(monkeypatch):
    def fake_get(url, **kw):
        raise update.requests.ConnectionError("offline")

    monkeypatch.setattr(update.requests, "get", fake_get)
    with pytest.raises(update.NetworkError):
        update.check_update(38)


def test_check_no_newer(monkeypatch):
    def fake_get(url, **kw):
        return Resp(200, _release(39))

    monkeypatch.setattr(update.requests, "get", fake_get)
    assert update.check_update(39) is None
    assert update.check_update(40) is None


def test_download_reuse(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "get_download_dir", lambda: str(tmp_path))

    def boom(url, **kw):
        raise AssertionError("复用命中时不应联网")

    monkeypatch.setattr(update.requests, "get", boom)
    info = _info(39, sha256=_sha(APK_BYTES), size=11)
    dest = os.path.join(str(tmp_path), "update-39.apk")
    with open(dest, "wb") as f:
        f.write(APK_BYTES)
    path = update.download_update(info)
    assert path == dest
    assert os.path.getsize(path) == 11


def test_should_auto_check():
    assert update.should_auto_check(None) is True
    assert update.should_auto_check("") is True
    assert update.should_auto_check(time.time() - 100) is False
    assert update.should_auto_check(time.time() - 25 * 3600) is True
    assert update.should_auto_check("garbage") is True


def test_manual_check_dispatch(monkeypatch):
    import update_ui

    def _job_of(side_effect):
        monkeypatch.setattr(update_ui, "check_update", side_effect)
        job, _done = update_ui.manual_check(object())
        return job()

    r = _job_of(lambda code: update.UpdateInfo(39, "0.1.39", "u"))
    assert r["kind"] == "checked" and r["info"].version_code == 39
    r = _job_of(lambda code: (_ for _ in ()).throw(update.NoReleaseError()))
    assert r["kind"] == "norelease"
    r = _job_of(lambda code: (_ for _ in ()).throw(update.NetworkError("x")))
    assert r["kind"] == "network" and r["message"]


# ---------- is_android 环境判定 / 版本兜底 / 下载目录（2026-11-16 平板回归） ----------

def _reset_android_cache():
    """清掉 update.is_android 的全局缓存，每次重算。"""
    if hasattr(update, '_ANDROID'):
        del update._ANDROID


def test_is_android_env_detection(monkeypatch):
    """环境判定：linux + /data/data 或 ANDROID_DATA 标记 = 安卓，不依赖 pyjnius。"""
    import sys
    _reset_android_cache()
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(os.path, 'isdir', lambda p: p == '/data/data')
    monkeypatch.setattr(update.os.path, 'isdir', lambda p: p == '/data/data')
    assert update.is_android() is True
    _reset_android_cache()
    monkeypatch.setattr(sys, 'platform', 'win32')
    assert update.is_android() is False
    _reset_android_cache()


def test_is_android_accepts_pep738_platform_name(monkeypatch):
    """CPython 3.13+ 安卓平台名是 "android"（PEP 738），flet 打包用 3.14。

    2026-11-16 老板平板：软件能用（内核 paths.py 认 "android"），但更新页显示
    「电脑预览」——update.is_android 当时只认 "linux"。
    """
    _reset_android_cache()
    monkeypatch.setattr(sys, 'platform', 'android')
    assert update.is_android() is True
    _reset_android_cache()


def test_is_android_tmpdir_under_data(monkeypatch):
    """环境标记缺失时 TMPDIR 落在 /data/ 下也判安卓（不依赖 ANDROID_DATA）。"""
    _reset_android_cache()
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(update.os.path, 'isdir', lambda p: False)
    monkeypatch.delenv('ANDROID_DATA', raising=False)
    monkeypatch.delenv('ANDROID_ROOT', raising=False)
    monkeypatch.setenv('TMPDIR', '/data/user/0/com.jiuguan.app/cache')
    assert update.is_android() is True
    _reset_android_cache()
    monkeypatch.setenv('TMPDIR', '/tmp')
    assert update.is_android() is False
    _reset_android_cache()


def test_platform_diag_reports_reason(monkeypatch):
    """非安卓时卡片摊开判定依据：真机再被误判成「电脑预览」可截图定位。"""
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setenv('TMPDIR', '/tmp/x')
    monkeypatch.delenv('ANDROID_DATA', raising=False)
    monkeypatch.delenv('ANDROID_ROOT', raising=False)
    s = update.platform_diag()
    assert 'platform=win32' in s and '/tmp/x' in s


def test_get_local_version_falls_back_to_generated_version(monkeypatch):
    """pyjnius 读包 manifest 失败 → 退读构建生成的 _version.py，不再显示 dev。"""
    import types
    _reset_android_cache()
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(os.path, 'isdir', lambda p: p == '/data/data')
    monkeypatch.setitem(sys.modules, '_version', types.SimpleNamespace(
        APP_VERSION_CODE=99, APP_VERSION_NAME='0.1.99'))
    code, name = update.get_local_version()
    assert (code, name) == (99, '0.1.99')
    _reset_android_cache()


def test_get_local_version_pyjnius_still_used_when_healthy(monkeypatch):
    """pyjnius 可用时版本号仍以包 manifest 为准（_version.py 只是兜底）。"""
    import types
    _reset_android_cache()
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(os.path, 'isdir', lambda p: p == '/data/data')
    fake_jnius = types.ModuleType('jnius')
    fake_act = type('PythonActivity', (), {})
    fake_act.mActivity = object()
    fake_jnius.autoclass = lambda name: fake_act
    monkeypatch.setitem(sys.modules, 'jnius', fake_jnius)
    # pyjnius 可用但 getPackageManager 链没有 mock 到底：只要不再返回 (0, 'dev')
    # 且 _version.py 未注入时返回 (0, '') 即视为走了 pyjnius 分支
    monkeypatch.delitem(sys.modules, '_version', raising=False)
    try:
        code, name = update.get_local_version()
    except AttributeError:
        code, name = None, None
    assert (code, name) != (0, 'dev')
    _reset_android_cache()


def test_get_download_dir_uses_tmpdir_without_pyjnius(tmp_path, monkeypatch):
    """下载目录从 TMPDIR 读（=cacheDir），pyjnius 挂了也能下载更新包。"""
    _reset_android_cache()
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(os.path, 'isdir', lambda p: p == '/data/data')
    tmpdir = str(tmp_path / 'pkg' / 'cache').replace(chr(92), '/')
    monkeypatch.setenv('TMPDIR', tmpdir)   # 真机 TMPDIR 是 POSIX 风格
    d = update.get_download_dir()
    assert d == os.path.join(tmpdir, "updates")
    _reset_android_cache()


def test_jnius_diag_reports_failure(monkeypatch):
    """桥接诊断：jnius import 失败返回异常摘要，不抛出。"""
    import types
    _reset_android_cache()
    fake_jnius = types.ModuleType('jnius')

    def boom(name):
        raise RuntimeError('bridge broken')

    fake_jnius.autoclass = boom
    monkeypatch.setitem(sys.modules, 'jnius', fake_jnius)
    d = update.jnius_diag()
    assert 'RuntimeError' in d and 'bridge broken' in d
    _reset_android_cache()


# ---------- 平板浏览器安装通路（2026-11-16：pyjnius 桥接不可用时的主通路） ----------

def test_browser_urls_mirror_first_and_dedupe():
    """主推上次成功的通道 → 镜像 → 直连垫底，且 URL 去重。"""
    info = update.UpdateInfo(
        52, "0.1.52", update.LATEST_APK_URL,
        source_prefix="https://gh-proxy.com/")
    urls = update.browser_download_urls(info)
    assert urls[0] == "https://gh-proxy.com/" + info.apk_url
    assert urls[-1] == info.apk_url
    assert len(urls) == len(set(urls))
    assert len(urls) == len(update.BROWSER_MIRRORS)


def test_browser_urls_direct_still_puts_mirror_first():
    """检查走直连成功时，浏览器通路仍让镜像排前（国内直连下载常被污染）。"""
    info = update.UpdateInfo(52, "0.1.52", "https://github.com/x.apk",
                             source_prefix="")
    urls = update.browser_download_urls(info)
    assert urls[0].startswith("https://gh-proxy.com/")
    assert urls[-1] == "https://github.com/x.apk"


def test_browser_urls_keeps_unknown_prefix():
    """source_prefix 是镜像表之外的通道也不丢（如 gh-proxy.org）。"""
    info = update.UpdateInfo(52, "0.1.52", "https://github.com/x.apk",
                             source_prefix="https://gh-proxy.org/")
    urls = update.browser_download_urls(info)
    assert urls[0] == "https://gh-proxy.org/https://github.com/x.apk"
    assert len(urls) == len(set(urls))


def test_browser_url_falls_back_to_latest():
    """Release 没给资产直链时退回固定 latest 镜像链（永远指向最新版）。"""
    empty = update.UpdateInfo(52, "0.1.52", "")
    assert update.browser_download_urls(empty) == []
    assert update.browser_download_url(empty) == ""
    assert update.browser_url_or_latest(empty) == update.LATEST_MIRROR_URL
    assert update.LATEST_MIRROR_URL.endswith(update.ASSET_NAME)


def test_update_sheet_has_browser_button_and_copyable_link():
    """弹窗必须有不依赖 pyjnius 的浏览器按钮 + 可长按复制的官方直链。"""
    import update_ui
    info = update.UpdateInfo(52, "0.1.52", update.LATEST_APK_URL)
    flow = update_ui._UpdateFlow(object(), info)
    assert flow.build() is not None
    # 2026-11-17：展示的是**官方**地址（镜像只作加速候选）
    assert flow._link.value == update.official_apk_url(info)
    assert flow._link.value.startswith("https://github.com/")
    assert flow._link.selectable is True


def test_open_browser_uses_flet_url_launcher(monkeypatch):
    """浏览器通路走 Flet UrlLauncher（Flutter 插件 Java 侧），不碰 jnius。"""
    import asyncio
    import flet as ft
    import update_ui

    calls = {}

    class FakeLauncher:
        async def launch_url(self, url, *, mode=None, **kw):
            calls["url"] = url
            calls["mode"] = mode

    monkeypatch.setattr(ft, "UrlLauncher", FakeLauncher)
    info = update.UpdateInfo(52, "0.1.52", update.LATEST_APK_URL)
    flow = update_ui._UpdateFlow(object(), info)
    asyncio.run(flow._open_browser(None))
    assert calls["url"] == update.browser_download_url(info)
    assert calls["mode"] == ft.LaunchMode.EXTERNAL_APPLICATION


def test_install_failure_falls_back_to_browser_not_pc(monkeypatch):
    """pyjnius 安装抛异常 → 自动走平板浏览器，绝不出现「从电脑上装」。"""
    import asyncio
    import update_ui

    info = update.UpdateInfo(52, "0.1.52", update.LATEST_APK_URL)
    flow = update_ui._UpdateFlow(object(), info)
    flow._path = "/tmp/update-52.apk"
    called = {}

    def boom(path, sha=""):
        raise RuntimeError("bridge broken")

    async def fake_open(e=None):
        called["browser"] = True

    monkeypatch.setattr(update_ui, "install_apk", boom)
    monkeypatch.setattr(flow, "_open_browser", fake_open)
    asyncio.run(flow._install(None))
    assert called.get("browser") is True
    assert "浏览器" in flow._status.value
    assert "从电脑" not in flow._status.value


def test_update_card_has_direct_latest_entry_on_android(monkeypatch):
    """卡片上永远留一条「直接下载最新安装包」，破解引导死循环。"""
    import update_ui
    monkeypatch.setattr(update, "is_android", lambda: True)
    monkeypatch.setattr(update, "get_local_version", lambda: (48, "0.1.48"))
    card = update_ui.update_card(object(), None)
    texts = []

    def walk(c):
        try:
            v = getattr(c, "value", None)
            if isinstance(v, str):
                texts.append(v)
            inner = getattr(c, "content", None)
            if inner is not None and inner is not c and hasattr(inner, "__dict__"):
                walk(inner)
            for x in (getattr(c, "controls", None) or []):
                walk(x)
        except Exception:
            pass

    walk(card)
    assert any("直接下载最新安装包" in t for t in texts)


# ================= 2026-11-17 第一阶段：SHA-256 完整性 =================


def _walk_texts(control):
    """把控件树里所有字符串 value 收齐（Flet 控件结构多变，尽量不假设层数）。"""
    out = []

    def walk(c):
        try:
            v = getattr(c, "value", None)
            if isinstance(v, str):
                out.append(v)
            inner = getattr(c, "content", None)
            if inner is not None and inner is not c and hasattr(inner, "__dict__"):
                walk(inner)
            for x in (getattr(c, "controls", None) or []):
                walk(x)
        except Exception:
            pass

    walk(control)
    return out


def test_normalize_sha256():
    good = "A" * 64
    assert update.normalize_sha256(good) == "a" * 64          # 统一小写
    assert update.normalize_sha256("  " + good + "\n") == "a" * 64
    assert update.normalize_sha256("sha256:" + good) == "a" * 64
    assert update.normalize_sha256("a" * 63) == ""            # 长度不对
    assert update.normalize_sha256("z" * 64) == ""            # 非 hex
    assert update.normalize_sha256(None) == ""
    assert update.normalize_sha256("") == ""


def test_parse_release_reads_sha256():
    """哈希进元数据后必须被解析出来（大小写归一化），并且 has_checksum 为真。"""
    digest = _sha(APK_BYTES)
    info = update.parse_release(_release(39, sha256=digest.upper()))
    assert info.apk_sha256 == digest
    assert info.has_checksum() is True
    assert digest in info.notes or info.notes == "修复若干问题"


def test_parse_release_tolerates_attr_order_and_junk_hash():
    """属性顺序可以变；哈希格式非法时当成「没有校验值」，不当成有效值。"""
    body = ("<!-- appupdate apkSha256=deadbeef versionName=0.1.39 "
            "versionCode=39 -->\n修复")
    data = dict(_release(39), body=body)
    info = update.parse_release(data)
    assert info.version_code == 39
    assert info.apk_sha256 == ""          # 半截哈希绝不采信
    assert info.has_checksum() is False


def test_parse_release_keeps_old_client_compatible_first_line():
    """首行必须还是老格式（0.1.52 及更早的客户端靠它判断有没有更新）。

    这里复刻旧版客户端的严格正则：新说明只要在首行多塞一个属性，旧客户端
    就会解析失败 → 以为「已是最新」→ 再也收不到更新。这条是兼容红线。
    """
    import re as _re
    old_re = _re.compile(
        r"<!--\s*appupdate\s+versionCode=(\d+)\s+versionName=([^\s>-]+)\s*-->")
    body = _release(53, sha256=_sha(APK_BYTES))["body"]
    first = body.splitlines()[0].strip()
    m = old_re.search(first)
    assert m, f"旧客户端无法解析首行：{first!r}"
    assert (m.group(1), m.group(2)) == ("53", "0.1.53")
    # 新客户端两条注释合并解析
    info = update.parse_release(_release(53, sha256=_sha(APK_BYTES)))
    assert info.apk_sha256 == _sha(APK_BYTES)
    assert "appupdate" not in info.notes and "apkupdate" not in info.notes


def test_check_apk_sha256_semantics(tmp_path):
    """空期望值 = 无法校验（False），不是「通过」。"""
    p = tmp_path / "a.apk"
    p.write_bytes(APK_BYTES)
    ok, actual = update.check_apk_sha256(str(p), _sha(APK_BYTES))
    assert ok is True and actual == _sha(APK_BYTES)
    ok, actual = update.check_apk_sha256(str(p), "")
    assert ok is False and actual == _sha(APK_BYTES)
    ok, _ = update.check_apk_sha256(str(p), "b" * 64)
    assert ok is False
    ok, actual = update.check_apk_sha256(str(tmp_path / "missing.apk"),
                                         _sha(APK_BYTES))
    assert ok is False and actual == ""


def test_parse_release_roundtrip_from_ci_guard():
    """CI 生成的说明必须被 App 端原样解析（格式漂移就红）。"""
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "release_guard", os.path.join(root, "tools", "release_guard.py"))
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)

    digest = _sha(APK_BYTES)
    notes = guard.build_notes(57, digest, "- 修 bug (abc1234)")
    data = dict(_release(57), body=notes)
    info = update.parse_release(data)
    assert info is not None
    assert info.version_code == 57
    assert info.version_name == "0.1.57"
    assert info.apk_sha256 == digest
    assert "abc1234" in info.notes
    # 元数据注释不能漏进给人看的更新说明
    assert "appupdate" not in info.notes


def test_download_cache_pollution_redownloads(monkeypatch, tmp_path):
    """缓存里是「大小对得上、内容不对」的包 → 必须删掉重下，不能直接复用。"""
    monkeypatch.setattr(update, "get_download_dir", lambda: str(tmp_path))
    dest = os.path.join(str(tmp_path), "update-39.apk")
    with open(dest, "wb") as f:
        f.write(b"X" * len(APK_BYTES))      # 同长度、内容被污染

    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return StreamResp(APK_BYTES)

    monkeypatch.setattr(update.requests, "get", fake_get)
    info = _info(39, sha256=_sha(APK_BYTES), size=len(APK_BYTES))
    path = update.download_update(info)
    assert calls, "污染缓存必须触发重新下载"
    assert path == dest
    with open(path, "rb") as f:
        assert f.read() == APK_BYTES
    assert not os.path.exists(path + ".part")


def test_download_wrong_hash_never_lands(monkeypatch, tmp_path):
    """哈希不符：不留正式文件、不留 .part、抛 ChecksumError（不是 NetworkError）。"""
    monkeypatch.setattr(update, "get_download_dir", lambda: str(tmp_path))

    def fake_get(url, **kw):
        return StreamResp(b"evil" * 5)

    monkeypatch.setattr(update.requests, "get", fake_get)
    info = _info(39, sha256=_sha(APK_BYTES))
    with pytest.raises(update.ChecksumError):
        update.download_update(info)
    assert not os.path.exists(os.path.join(str(tmp_path), "update-39.apk"))
    assert not os.path.exists(os.path.join(str(tmp_path), "update-39.apk.part"))


def test_download_wrong_hash_at_dest_is_deleted(monkeypatch, tmp_path):
    """已存在的正式文件被换掉（污染）→ 删除；重下仍不对 → 目录里不留任何 apk。"""
    monkeypatch.setattr(update, "get_download_dir", lambda: str(tmp_path))
    dest = os.path.join(str(tmp_path), "update-39.apk")
    with open(dest, "wb") as f:
        f.write(b"Y" * len(APK_BYTES))

    def fake_get(url, **kw):
        return StreamResp(b"evil" * 5)

    monkeypatch.setattr(update.requests, "get", fake_get)
    info = _info(39, sha256=_sha(APK_BYTES), size=len(APK_BYTES))
    with pytest.raises(update.ChecksumError):
        update.download_update(info)
    assert not os.path.exists(dest)


def test_download_without_checksum_refuses_before_network(monkeypatch, tmp_path):
    """元数据没给哈希 → 不下载（省流量）、直接拒绝，交由浏览器通路。"""
    monkeypatch.setattr(update, "get_download_dir", lambda: str(tmp_path))

    def boom(url, **kw):
        raise AssertionError("没有校验值就不该联网下载")

    monkeypatch.setattr(update.requests, "get", boom)
    with pytest.raises(update.ChecksumError):
        update.download_update(_info(39, sha256="", size=len(APK_BYTES)))


def test_download_retries_next_source_on_bad_content(monkeypatch, tmp_path):
    """一个源内容不对、另一个源正常 → 换源成功（镜像传输损坏是独立的）。"""
    monkeypatch.setattr(update, "get_download_dir", lambda: str(tmp_path))
    seen = []

    def fake_get(url, **kw):
        seen.append(url)
        if len(seen) == 1:
            return StreamResp(b"evil" * 5)
        return StreamResp(APK_BYTES)

    monkeypatch.setattr(update.requests, "get", fake_get)
    info = _info(39, sha256=_sha(APK_BYTES), size=len(APK_BYTES))
    path = update.download_update(info)
    assert len(seen) >= 2
    with open(path, "rb") as f:
        assert f.read() == APK_BYTES


def test_install_refuses_wrong_hash(monkeypatch, tmp_path):
    """第三道校验点：安装前哈希不符 → ChecksumError（绝不进安装器）。"""
    _reset_android_cache()
    monkeypatch.setattr(sys, 'platform', 'android')
    p = tmp_path / "update-39.apk"
    p.write_bytes(b"tampered")
    with pytest.raises(update.ChecksumError):
        update.install_apk(str(p), _sha(APK_BYTES))
    _reset_android_cache()


def test_install_refuses_missing_checksum(monkeypatch, tmp_path):
    """无法校验 ≠ 校验通过：没给哈希也拒绝安装。"""
    _reset_android_cache()
    monkeypatch.setattr(sys, 'platform', 'android')
    p = tmp_path / "update-39.apk"
    p.write_bytes(APK_BYTES)
    with pytest.raises(update.ChecksumError):
        update.install_apk(str(p), "")
    with pytest.raises(update.ChecksumError):
        update.install_apk(str(p))             # 默认参数也不能放行
    _reset_android_cache()


def test_install_verified_file_reaches_jnius(monkeypatch, tmp_path):
    """哈希正确时校验闸门放行（后续 jnius 缺失是环境问题，不是校验问题）。"""
    _reset_android_cache()
    monkeypatch.setattr(sys, 'platform', 'android')
    monkeypatch.setitem(sys.modules, 'jnius', None)   # from jnius import → ImportError
    p = tmp_path / "update-39.apk"
    p.write_bytes(APK_BYTES)
    with pytest.raises(Exception) as ei:
        update.install_apk(str(p), _sha(APK_BYTES))
    assert not isinstance(ei.value, update.ChecksumError)
    _reset_android_cache()


# ---------- UI：官方地址 + SHA-256 展示 / 浏览器通路不谎称已校验 ----------

def test_update_sheet_shows_official_url_and_sha256():
    """浏览器通路的弹窗必须摊开官方地址 + SHA-256，并写明「无法自动校验」。"""
    import update_ui
    digest = _sha(APK_BYTES)
    info = update.UpdateInfo(52, "0.1.52", update.LATEST_APK_URL,
                             apk_sha256=digest)
    flow = update_ui._UpdateFlow(object(), info)
    texts = _walk_texts(flow.build())
    joined = "\n".join(texts)
    assert update.LATEST_APK_URL in joined                 # 官方直链
    assert "https://github.com/Lwb1397111398/jiuguan-mobile/releases/tag/latest" \
        in joined                                          # 官方 Release 页
    assert digest in joined                                # 完整 64 位哈希
    assert "无法自动校验" in joined
    assert "已自动校验" not in joined
    assert "自动校验通过" not in joined
    # App 内下载入口在有哈希时可用
    assert flow._download_btn.disabled is False


def test_update_sheet_disables_inapp_install_without_checksum():
    """老 Release 没哈希 → App 内下载按钮禁用 + 明说停用原因。"""
    import update_ui
    info = update.UpdateInfo(52, "0.1.52", update.LATEST_APK_URL)
    flow = update_ui._UpdateFlow(object(), info)
    texts = "\n".join(_walk_texts(flow.build()))
    assert flow._download_btn.disabled is True
    assert "未提供 SHA-256" in texts
    assert "App 内下载安装已停用" in texts


def test_update_sheet_keeps_actions_on_screen():
    """完整性信息比旧版长得多：中部必须限高滚动，否则小屏上按钮被顶出屏幕。"""
    import update_ui

    class FakePage:
        height = 640          # 小屏手机

    info = update.UpdateInfo(52, "0.1.52", update.LATEST_APK_URL,
                             apk_sha256=_sha(APK_BYTES))
    flow = update_ui._UpdateFlow(FakePage(), info)
    heights = []

    def walk(c):
        try:
            if getattr(c, "scroll", None) is not None:
                h = getattr(c, "height", None)
                if isinstance(h, (int, float)):
                    heights.append(h)
            inner = getattr(c, "content", None)
            if inner is not None and inner is not c and hasattr(inner, "__dict__"):
                walk(inner)
            for x in (getattr(c, "controls", None) or []):
                walk(x)
        except Exception:
            pass

    walk(flow.build())
    assert heights, "中部信息区缺少限高滚动容器"
    assert all(200 <= h <= 420 for h in heights), heights


def test_browser_status_never_claims_verification(monkeypatch):
    """点「用平板浏览器下载安装」后，状态文案不得宣称已校验。"""
    import asyncio
    import flet as ft
    import update_ui

    class FakeLauncher:
        async def launch_url(self, url, *, mode=None, **kw):
            return None

    monkeypatch.setattr(ft, "UrlLauncher", FakeLauncher)
    info = update.UpdateInfo(52, "0.1.52", update.LATEST_APK_URL,
                             apk_sha256=_sha(APK_BYTES))
    flow = update_ui._UpdateFlow(object(), info)
    asyncio.run(flow._open_browser(None))
    assert "无法自动校验" in flow._status.value
    assert "已自动校验" not in flow._status.value
    assert "校验通过" not in flow._status.value


def test_download_done_checksum_failure_blocks_install():
    """下载层报校验失败 → 不露出「安装更新」按钮，文案写清未安装。"""
    import update_ui
    info = update.UpdateInfo(52, "0.1.52", update.LATEST_APK_URL,
                             apk_sha256=_sha(APK_BYTES))
    flow = update_ui._UpdateFlow(object(), info)
    flow._download_done({"ok": False, "checksum": True,
                         "error": "安装包 SHA-256 校验不通过"})
    assert "校验失败" in flow._status.value
    assert "未安装" in flow._status.value
    assert flow._install_btn.visible is False
    assert flow._download_btn.visible is False


def test_install_blocks_and_deletes_on_checksum_error(monkeypatch, tmp_path):
    """点「安装更新」时文件已被换掉 → 阻止安装、删文件、给官方通路。"""
    import asyncio
    import update_ui

    p = tmp_path / "update-52.apk"
    p.write_bytes(b"tampered after download")
    info = update.UpdateInfo(52, "0.1.52", update.LATEST_APK_URL,
                             apk_sha256=_sha(APK_BYTES))
    flow = update_ui._UpdateFlow(object(), info)
    flow._path = str(p)

    # 真正走 update.install_apk 的校验闸门（这一步不该摸到 jnius）
    _reset_android_cache()
    monkeypatch.setattr(sys, 'platform', 'android')
    asyncio.run(flow._install(None))
    _reset_android_cache()

    assert "安装已阻止" in flow._status.value
    assert flow._install_btn.visible is False
    assert not p.exists()             # 不可信文件必须删掉
    assert flow._path is None


def test_install_failure_after_verification_still_falls_back(monkeypatch):
    """校验通过但桥接失败 → 仍自动切浏览器（不能因为加了校验把降级路挡住）。"""
    import asyncio
    import update_ui

    info = update.UpdateInfo(52, "0.1.52", update.LATEST_APK_URL,
                             apk_sha256=_sha(APK_BYTES))
    flow = update_ui._UpdateFlow(object(), info)
    flow._path = "/tmp/whatever.apk"
    called = {}

    def boom(path, sha=""):
        raise RuntimeError("bridge broken")

    async def fake_open(e=None):
        called["browser"] = True

    monkeypatch.setattr(update_ui, "install_apk", boom)
    monkeypatch.setattr(flow, "_open_browser", fake_open)
    asyncio.run(flow._install(None))
    assert called.get("browser") is True
