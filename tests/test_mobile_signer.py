# -*- coding: utf-8 -*-
"""换签名预埋单测（2026-11-20 P0 旧钥过渡版）。

覆盖老板点名的四件事：

1. **元数据兼容**：`apkSigner` 只进第二行注释，首行 `appupdate` 一字不动
   （0.1.52 及更早的客户端整行匹配首行，多一个属性就再也收不到更新）；
   老 Release 没指纹 = 「不知道」，不阻断正常更新。
2. **指纹比较**：keytool 的 `FE:A1:…` 与 apksigner 的 `fea1…` 规范化后相等才算同一个；
   只有两端都已知且不同才是「换签名」。
3. **签名变化禁止安装**：UI 不给安装器入口，`install_apk` 自身也拦（纵深防御）。
4. **本机指纹来源**：构建注入的 `_version.APP_SIGNER_SHA256`，**不碰 pyjnius**。
"""
import hashlib
import os
import sys

import pytest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src")
sys.path.insert(0, SRC)

import update  # noqa: E402

SIGNER_A = "a" * 64
SIGNER_B = "b" * 64
APK_BYTES = b"J" * 11


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _colon(fp: str) -> str:
    """小写无分隔 → keytool 的 `AA:BB:…` 大写写法（同一个证书）。"""
    return ":".join(fp[i:i + 2] for i in range(0, len(fp), 2)).upper()


def _reset_android_cache():
    if hasattr(update, "_ANDROID"):
        del update._ANDROID


def _release(code=60, signer=SIGNER_B, sha256=None, first_line=None):
    lines = [first_line or
             "<!-- appupdate versionCode=%d versionName=0.1.%d -->" % (code, code)]
    attrs = []
    if sha256 is not None:
        attrs.append("apkSha256=%s" % sha256)
    if signer is not None:
        attrs.append("apkSigner=%s" % signer)
    if attrs:
        lines.append("<!-- apkupdate %s -->" % " ".join(attrs))
    lines.append("换签名过渡版")
    return {"name": "0.1.%d" % code, "body": "\n".join(lines),
            "assets": [{"name": update.ASSET_NAME, "size": 100,
                        "browser_download_url": "https://github.com/x/y.apk"}]}


def _walk_texts(control):
    out = []

    def walk(c):
        try:
            v = getattr(c, "value", None)
            if isinstance(v, str):
                out.append(v)
            inner = getattr(c, "content", None)
            if isinstance(inner, str):
                out.append(inner)          # Button(content="文案") 的字符串写法
            elif inner is not None and inner is not c and hasattr(inner, "__dict__"):
                walk(inner)
            for x in (getattr(c, "controls", None) or []):
                walk(x)
        except Exception:
            pass

    walk(control)
    return out


# ---------------- 规范化 / 解析 ----------------

def test_normalize_signer_accepts_keytool_and_apksigner_forms():
    """keytool 的 `FE:A1:…` 与 apksigner 的 `fea1…` 必须归一到同一个值。"""
    assert update.normalize_signer(SIGNER_A) == SIGNER_A
    assert update.normalize_signer(SIGNER_A.upper()) == SIGNER_A
    assert update.normalize_signer(_colon(SIGNER_A)) == SIGNER_A
    assert update.normalize_signer("sha256:" + SIGNER_A) == SIGNER_A
    assert update.normalize_signer(" " + _colon(SIGNER_A) + "\n") == SIGNER_A
    for bad in ("", None, "deadbeef", "z" * 64, "a" * 63, "a" * 65):
        assert update.normalize_signer(bad) == ""


def test_parse_release_reads_apk_signer():
    digest = _sha(APK_BYTES)
    info = update.parse_release(_release(60, _colon(SIGNER_B), digest))
    assert info.apk_signer == SIGNER_B          # 冒号写法也被归一
    assert info.has_signer() is True
    assert info.apk_sha256 == digest
    assert info.version_code == 60


def test_parse_release_missing_or_bad_signer_is_unknown():
    """老 Release 没有 apkSigner / 写了半截 → 一律当「不知道」，不阻断更新。"""
    old = update.parse_release(_release(60, signer=None, sha256=_sha(APK_BYTES)))
    assert old.apk_signer == "" and old.has_signer() is False
    bad = update.parse_release(_release(60, signer="deadbeef"))
    assert bad.apk_signer == "" and bad.has_signer() is False


def test_first_line_stays_old_client_parseable_with_signer():
    """签名指纹只能进第二行：旧客户端（≤0.1.52）整行匹配首行。"""
    import re as _re
    old_re = _re.compile(
        r"<!--\s*appupdate\s+versionCode=(\d+)\s+versionName=([^\s>-]+)\s*-->")
    body = _release(61, SIGNER_B, _sha(APK_BYTES))["body"]
    first = body.splitlines()[0].strip()
    m = old_re.search(first)
    assert m and (m.group(1), m.group(2)) == ("61", "0.1.61")
    assert "apkSigner" not in first
    # 新客户端两条注释合并解析，指纹能读出来
    assert update.parse_release({"body": body,
                                 "assets": [{"name": update.ASSET_NAME,
                                             "size": 1,
                                             "browser_download_url": "u"}],
                                 }).apk_signer == SIGNER_B


def test_signer_changed_matrix():
    """只有「两端都已知且不同」才算换签名；缺任一端 = 不知道 = 不阻断。"""
    assert update.signer_changed(SIGNER_A, SIGNER_B) is True
    assert update.signer_changed(_colon(SIGNER_A), SIGNER_B) is True
    assert update.signer_changed(SIGNER_A, SIGNER_A) is False
    assert update.signer_changed(SIGNER_A, "") is False
    assert update.signer_changed("", SIGNER_B) is False
    assert update.signer_changed("", "") is False
    assert update.signer_changed("bad", SIGNER_B) is False


def test_update_info_signer_changed_uses_local_constant(monkeypatch):
    """本机指纹来自构建注入的 _version 常量（不是 pyjnius 读包）。"""
    monkeypatch.setattr(update, "local_signer_sha256", lambda: SIGNER_A)
    assert update.UpdateInfo(60, "0.1.60", "u",
                             apk_signer=SIGNER_B).signer_changed() is True
    assert update.UpdateInfo(60, "0.1.60", "u",
                             apk_signer=SIGNER_A).signer_changed() is False
    assert update.UpdateInfo(60, "0.1.60", "u").signer_changed() is False


def test_local_signer_reads_version_module(monkeypatch):
    """_version.APP_SIGNER_SHA256 是唯一来源；模块缺失/为空 → 空串（未知）。"""
    import types
    monkeypatch.setitem(sys.modules, "_version", types.SimpleNamespace(
        APP_VERSION_CODE=60, APP_VERSION_NAME="0.1.60",
        APP_SIGNER_SHA256=_colon(SIGNER_A)))
    assert update.local_signer_sha256() == SIGNER_A
    monkeypatch.setitem(sys.modules, "_version", types.SimpleNamespace(
        APP_VERSION_CODE=60, APP_VERSION_NAME="0.1.60"))
    assert update.local_signer_sha256() == ""
    monkeypatch.delitem(sys.modules, "_version", raising=False)
    assert update.local_signer_sha256() == ""


def test_local_signer_never_touches_jnius(monkeypatch):
    """pyjnius 挂掉也必须能拿到本机指纹（老板平板整链失败的教训）。"""
    import types
    fake_jnius = types.ModuleType("jnius")

    def boom(name):
        raise AssertionError("读指纹不该碰 pyjnius")

    fake_jnius.autoclass = boom
    monkeypatch.setitem(sys.modules, "jnius", fake_jnius)
    monkeypatch.setitem(sys.modules, "_version", types.SimpleNamespace(
        APP_SIGNER_SHA256=SIGNER_A))
    assert update.local_signer_sha256() == SIGNER_A


# ---------------- 下载 / 安装闸门 ----------------

def test_download_refuses_on_signer_change_before_network(monkeypatch, tmp_path):
    """签名不同 → 连下载都不做（下载了也装不上，浪费流量）。"""
    monkeypatch.setattr(update, "get_download_dir", lambda: str(tmp_path))
    monkeypatch.setattr(update, "local_signer_sha256", lambda: SIGNER_A)

    def boom(url, **kw):
        raise AssertionError("签名不同时不该联网下载")

    monkeypatch.setattr(update.requests, "get", boom)
    info = update.UpdateInfo(60, "0.1.60", "https://github.com/x/y.apk",
                             size=len(APK_BYTES),
                             apk_sha256=_sha(APK_BYTES), apk_signer=SIGNER_B)
    with pytest.raises(update.SignerMismatchError):
        update.download_update(info)


def test_install_blocks_on_signer_change(monkeypatch, tmp_path):
    """签名闸门：不唤起安装器（连 jnius 都不 import）。"""
    _reset_android_cache()
    monkeypatch.setattr(sys, 'platform', 'android')
    monkeypatch.setitem(sys.modules, 'jnius', None)   # 走到 jnius 就会 ImportError
    p = tmp_path / "update-60.apk"
    p.write_bytes(APK_BYTES)
    with pytest.raises(update.SignerMismatchError):
        update.install_apk(str(p), _sha(APK_BYTES),
                           expected_signer=SIGNER_B, local_signer=SIGNER_A)
    _reset_android_cache()


def test_install_allows_when_signer_unknown(monkeypatch, tmp_path):
    """任一端缺指纹 = 不知道：保持原流程（这里表现为继续走到 jnius 才失败）。"""
    _reset_android_cache()
    monkeypatch.setattr(sys, 'platform', 'android')
    monkeypatch.setitem(sys.modules, 'jnius', None)
    p = tmp_path / "update-60.apk"
    p.write_bytes(APK_BYTES)
    for kwargs in ({"expected_signer": "", "local_signer": SIGNER_A},
                   {"expected_signer": SIGNER_B, "local_signer": ""},
                   {"expected_signer": SIGNER_A, "local_signer": SIGNER_A}):
        with pytest.raises(Exception) as ei:
            update.install_apk(str(p), _sha(APK_BYTES), **kwargs)
        assert not isinstance(ei.value, update.SignerMismatchError), kwargs
    _reset_android_cache()


def test_install_still_checks_sha_before_signer(monkeypatch, tmp_path):
    """顺序固定：哈希不过先报 ChecksumError（文件不可信优先于签名问题）。"""
    _reset_android_cache()
    monkeypatch.setattr(sys, 'platform', 'android')
    p = tmp_path / "update-60.apk"
    p.write_bytes(APK_BYTES)
    with pytest.raises(update.ChecksumError):
        update.install_apk(str(p), _sha(b"other"),
                           expected_signer=SIGNER_B, local_signer=SIGNER_A)
    _reset_android_cache()


# ---------------- 弹窗（迁移版式） ----------------

def test_update_sheet_shows_migration_when_signer_changes(monkeypatch):
    """签名不同：弹窗讲清「先备份 → 卸载 → 装新包 → 导入」，不给安装入口。"""
    import update_ui
    monkeypatch.setattr(update, "local_signer_sha256", lambda: SIGNER_A)
    monkeypatch.setattr(update_ui, "local_signer_sha256", lambda: SIGNER_A)
    info = update.UpdateInfo(60, "0.1.60", update.LATEST_APK_URL,
                             apk_sha256=_sha(APK_BYTES), apk_signer=SIGNER_B)
    flow = update_ui._UpdateFlow(object(), info)
    joined = "\n".join(_walk_texts(flow.build()))
    assert "需要卸载重装" in joined
    assert "先备份" in joined and "卸载旧版" in joined and "导入备份" in joined
    assert "打开官方下载页" in joined
    assert update.official_release_page() in joined
    assert SIGNER_B[:12] in joined and SIGNER_A[:12] in joined
    # 不许再给安装器入口
    assert flow._download_btn.visible is False
    assert flow._install_btn.visible is False
    assert flow._download_btn.disabled is True
    assert "用平板浏览器下载安装" not in joined


def test_update_sheet_keeps_normal_flow_when_signer_same(monkeypatch):
    import update_ui
    monkeypatch.setattr(update, "local_signer_sha256", lambda: SIGNER_A)
    monkeypatch.setattr(update_ui, "local_signer_sha256", lambda: SIGNER_A)
    info = update.UpdateInfo(60, "0.1.60", update.LATEST_APK_URL,
                             apk_sha256=_sha(APK_BYTES), apk_signer=SIGNER_A)
    flow = update_ui._UpdateFlow(object(), info)
    assert flow._signer_changed is False
    joined = "\n".join(_walk_texts(flow.build()))
    assert "用平板浏览器下载安装" in joined
    assert "需要卸载重装" not in joined
    assert flow._download_btn.disabled is False


def test_signer_change_never_calls_installer(monkeypatch):
    """即使有人绕过按钮直接调 _install，也不许摸安装器。"""
    import asyncio
    import update_ui
    monkeypatch.setattr(update, "local_signer_sha256", lambda: SIGNER_A)
    monkeypatch.setattr(update_ui, "local_signer_sha256", lambda: SIGNER_A)
    info = update.UpdateInfo(60, "0.1.60", update.LATEST_APK_URL,
                             apk_sha256=_sha(APK_BYTES), apk_signer=SIGNER_B)
    flow = update_ui._UpdateFlow(object(), info)
    flow._path = "/tmp/update-60.apk"

    def boom(*a, **kw):
        raise AssertionError("签名不同时绝不许调安装器")

    monkeypatch.setattr(update_ui, "install_apk", boom)
    asyncio.run(flow._install(None))
    assert "安装已阻止" in flow._status.value
    assert "卸载" in flow._status.value


def test_signer_change_go_backup_calls_callback(monkeypatch):
    import update_ui

    class FakePage:
        def __init__(self):
            self.popped = 0

        def pop_dialog(self):
            self.popped += 1

        def update(self):
            pass

    page = FakePage()
    called = {}
    info = update.UpdateInfo(60, "0.1.60", update.LATEST_APK_URL,
                             apk_signer=SIGNER_B)
    monkeypatch.setattr(update, "local_signer_sha256", lambda: SIGNER_A)
    flow = update_ui._UpdateFlow(
        page, info, on_go_backup=lambda: called.setdefault("ok", True))
    flow._go_backup(None)
    assert called.get("ok") is True and page.popped == 1


def test_signer_change_without_callback_gives_text_guidance(monkeypatch):
    import update_ui
    monkeypatch.setattr(update, "local_signer_sha256", lambda: SIGNER_A)
    info = update.UpdateInfo(60, "0.1.60", update.LATEST_APK_URL,
                             apk_signer=SIGNER_B)
    flow = update_ui._UpdateFlow(object(), info)      # 设置页：没有备份回调
    flow._go_backup(None)
    assert "全量备份" in flow._status.value
    assert "另存到手机" in flow._status.value


def test_signer_change_opens_official_release_page(monkeypatch):
    """「打开官方下载页」打开的是 Release 页（含说明），不是 APK 直链。"""
    import asyncio
    import flet as ft
    import update_ui
    monkeypatch.setattr(update, "local_signer_sha256", lambda: SIGNER_A)
    calls = {}

    class FakeLauncher:
        async def launch_url(self, url, *, mode=None, **kw):
            calls["url"] = url
            calls["mode"] = mode

    monkeypatch.setattr(ft, "UrlLauncher", FakeLauncher)
    info = update.UpdateInfo(60, "0.1.60", update.LATEST_APK_URL,
                             apk_signer=SIGNER_B)
    flow = update_ui._UpdateFlow(object(), info)
    asyncio.run(flow._open_official_page(None))
    assert calls["url"] == update.official_release_page()
    assert calls["mode"] == ft.LaunchMode.EXTERNAL_APPLICATION
    assert "官方下载页" in flow._status.value


def test_signer_change_browser_button_routes_to_release_page(monkeypatch):
    """兜底：即使走到 _open_browser（旧按钮），也不下载装不上的包。"""
    import asyncio
    import update_ui
    monkeypatch.setattr(update, "local_signer_sha256", lambda: SIGNER_A)
    info = update.UpdateInfo(60, "0.1.60", update.LATEST_APK_URL,
                             apk_signer=SIGNER_B)
    flow = update_ui._UpdateFlow(object(), info)
    called = {}

    async def fake_official(e=None):
        called["official"] = True

    monkeypatch.setattr(flow, "_open_official_page", fake_official)
    asyncio.run(flow._open_browser(None))
    assert called.get("official") is True


# ---------------- 构建期指纹工具（tools/signer_guard.py） ----------------

def _load_signer_guard():
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "signer_guard", os.path.join(root, "tools", "signer_guard.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


guard = _load_signer_guard()

KEYTOOL_OUT = """\
Alias name: androiddebugkey
Certificate fingerprints:
\t SHA1: 70:7C:22:87:4F:4C:2C:67:A5:4B:F2:FE:D6:6B:76:A2:CF:18:94:19
\t SHA256: FE:A1:B9:74:EA:58:44:AA:AD:F1:D2:BA:89:36:DB:E5:40:F3:1A:41:82:D2:97:08:26:12:B5:48:76:E2:78:0E
Signature algorithm name: SHA256withRSA
"""

APKSIGNER_OUT = """\
Verifies
Verified using v2 scheme (APK Signature Scheme v2): true
Signer #1 certificate DN: C=US, O=Android, CN=Android Debug
Signer #1 certificate SHA-256 digest: fea1b974ea5844aaadf1d2ba8936dbe540f31a4182d297082612b54876e2780e
Signer #1 certificate SHA-1 digest: 707c22874f4c2c67a54bf2fed66b76a2cf189419
"""


def test_signer_guard_parses_keytool_and_apksigner():
    """两种工具的输出格式不同，解析后必须是同一个 64 位小写 hex。"""
    a = guard.parse_keytool_sha256(KEYTOOL_OUT)
    b = guard.parse_apksigner_sha256(APKSIGNER_OUT)
    assert a == b == "fea1b974ea5844aaadf1d2ba8936dbe540f31a4182d297082612b54876e2780e"
    assert guard.parse_keytool_sha256("no fingerprint here") == ""
    assert guard.parse_apksigner_sha256("unsigned") == ""


def test_signer_guard_normalizes_and_sorts_build_tools(tmp_path):
    """build-tools 目录按数字段排序（36.1.0 > 36.0.0 > 9.0.0）。"""
    bt = tmp_path / "build-tools"
    for name in ("9.0.0", "36.0.0", "36.1.0"):
        d = bt / name
        d.mkdir(parents=True)
        (d / "apksigner.bat").write_text("", encoding="utf-8")
    found = guard.find_apksigner(sdk_root=str(tmp_path))
    assert found.endswith(os.path.join("36.1.0", "apksigner.bat"))
    assert guard.normalize_fingerprint("FE:A1:B9:" + "0" * 58) == "fea1b9" + "0" * 58


def test_signer_guard_verify_cli(monkeypatch, tmp_path, capsys):
    """verify：一致退出 0；不一致退出 3；期望值非法退出 2。"""
    apk = tmp_path / "a.apk"
    apk.write_bytes(b"apk")
    monkeypatch.setattr(guard, "apk_fingerprint", lambda *a, **kw: SIGNER_A)
    assert guard.main(["verify", "--expected", _colon(SIGNER_A),
                       "--apk", str(apk)]) == 0
    assert guard.main(["verify", "--expected", SIGNER_B,
                       "--apk", str(apk)]) == 3
    assert guard.main(["verify", "--expected", "junk",
                       "--apk", str(apk)]) == 2


def test_signer_guard_fails_closed_without_material(monkeypatch, tmp_path):
    """keystore/APK 不存在或工具缺失 → 退出码 2（绝不返回空指纹当成功）。"""
    assert guard.main(["keystore", "--keystore", str(tmp_path / "nope.jks")]) == 2
    assert guard.main(["apk", "--apk", str(tmp_path / "nope.apk")]) == 2
