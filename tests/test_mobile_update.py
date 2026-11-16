# -*- coding: utf-8 -*-
"""手机版自更新单测（精简版：fake requests 闭包，monkeypatch update.requests.get）。"""
import os
import sys
import time

import pytest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src")
sys.path.insert(0, SRC)

import update  # noqa: E402


class Resp:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


    def raise_for_status(self):
        if self.status_code >= 400:
            raise update.requests.HTTPError(str(self.status_code))


def _release(code=39):
    return {
        "name": "0.1.%d" % code,
        "body": "<!-- appupdate versionCode=%d versionName=0.1.%d -->\n修复若干问题" % (code, code),
        "assets": [{
            "name": update.ASSET_NAME,
            "size": 50_000_000,
            "browser_download_url": "https://github.com/OWNER/REPO/releases/download/latest/x.apk",
        }],
    }


def test_parse_release():
    info = update.parse_release(_release(39), source_prefix="P")
    assert info.version_code == 39
    assert info.version_name == "0.1.39"
    assert info.size == 50_000_000
    assert info.source_prefix == "P"
    assert "修复" in info.notes


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
    info = update.UpdateInfo(39, "0.1.39", "https://github.com/x.apk", size=11)
    dest = os.path.join(str(tmp_path), "update-39.apk")
    with open(dest, "wb") as f:
        f.write(b"J" * 11)
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
    """弹窗必须有不依赖 pyjnius 的浏览器按钮 + 可长按复制的直链。"""
    import update_ui
    info = update.UpdateInfo(52, "0.1.52", update.LATEST_APK_URL)
    flow = update_ui._UpdateFlow(object(), info)
    assert flow.build() is not None
    assert flow._link.value == update.browser_download_url(info)
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

    def boom(path):
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
