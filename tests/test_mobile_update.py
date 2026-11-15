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
