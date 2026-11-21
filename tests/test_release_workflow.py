# -*- coding: utf-8 -*-
"""发版安全回归（2026-11-17 第一阶段）：CI 串行化 + 版本单调守卫 + SHA-256 元数据。

不联网、不跑 CI：直接读 `.github/workflows/build-release.yml` 的文本/结构，
再跑 `tools/release_guard.py` 的纯函数与 CLI。断言写得「语义化」一点
（找步骤名 + 命令关键词），避免改缩进/改注释就红。
"""
import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "build-release.yml")
SRC = os.path.join(ROOT, "src")

if SRC not in sys.path:
    sys.path.insert(0, SRC)

import update  # noqa: E402


def _load_guard():
    spec = importlib.util.spec_from_file_location(
        "release_guard", os.path.join(ROOT, "tools", "release_guard.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


guard = _load_guard()

DIGEST = "c" * 64


def _workflow_text():
    with open(WORKFLOW, "r", encoding="utf-8") as f:
        return f.read()


def _workflow_yaml():
    yaml = pytest.importorskip("yaml")     # 环境没有 PyYAML 时跳过结构断言
    return yaml.safe_load(_workflow_text())


def _steps():
    return _workflow_yaml()["jobs"]["build"]["steps"]


def _step(name_part):
    for s in _steps():
        if name_part in str(s.get("name") or ""):
            return s
    raise AssertionError(f"workflow 里找不到步骤：{name_part}")


# ---------- CI 并发 ----------

def test_workflow_has_release_concurrency():
    """发版必须串行：同一 group 只跑一个 run，且不允许取消正在上传的 run。

    cancel-in-progress=true 会打断 `gh release upload --clobber` 的
    「删旧资产 → 传新资产」，可能留下一个没有 APK 的 latest（App 端会变成
    「检查不到更新」）。所以这里钉死 False，靠排队 + 单调守卫保顺序。
    """
    wf = _workflow_yaml()
    conc = wf.get("concurrency")
    assert conc, "缺少 workflow 级 concurrency，多个 run 会抢同一个 latest"
    assert isinstance(conc, dict)
    assert str(conc.get("group") or "").strip(), "concurrency.group 不能为空"
    assert conc.get("cancel-in-progress") is False


def test_release_step_guards_against_downgrade():
    """Release 步骤必须在真正上传前调用单调守卫，且 skip 时直接退出。"""
    run = str(_step("Release latest").get("run") or "")
    assert "release_guard.py decide" in run
    assert "--build-num" in run and "--remote-body-file" in run
    guard_at = run.index("release_guard.py decide")
    upload_at = run.index("gh release upload")
    assert guard_at < upload_at, "守卫必须在上传之前执行"
    assert 'if [ "$DECISION" = "skip" ]' in run.replace("'", '"')
    assert "exit 0" in run


def test_release_step_skips_verify_when_not_uploaded():
    """跳过上传时不能去校验云端（那时 latest 属于更新的构建，必然不符）。"""
    step = _step("Verify published release metadata")
    assert "release.outputs.uploaded" in str(step.get("if") or "")
    run = str(step.get("run") or "")
    assert "apksha256" in run.lower() and "versioncode" in run.lower()


# ---------- SHA-256 元数据 ----------

def test_workflow_computes_and_publishes_sha256():
    sha_step = _step("Compute APK SHA-256")
    assert sha_step.get("id") == "sha"
    assert "sha256sum" in str(sha_step.get("run") or "")

    notes_step = _step("Write release notes")
    assert "release_guard.py notes" in str(notes_step.get("run") or "")
    env = notes_step.get("env") or {}
    assert "steps.sha.outputs.sha256" in str(env.get("APK_SHA256") or "")

    # 说明生成完必须检查真的含元数据（head 只给 3 行，够看到首行）
    assert "head -3 notes.md" in str(notes_step.get("run") or "")


def test_workflow_still_uploads_single_stable_asset():
    """资产名/tag 不变是 App 端「永远只有一个入口」的前提，别顺手改。"""
    run = str(_step("Release latest").get("run") or "")
    assert "jiuguan-novel.apk" in run
    assert "--clobber" in run
    assert "latest" in run


# ---------- 守卫模块 ----------

def test_parse_version_code_variants():
    assert guard.parse_version_code(
        "<!-- appupdate versionCode=53 versionName=0.1.53 apkSha256=%s -->"
        % DIGEST) == 53
    assert guard.parse_version_code(
        "<!-- appupdate versionCode=53 versionName=0.1.53 -->") == 53
    assert guard.parse_version_code("## 更新内容\n- 修 bug") is None
    assert guard.parse_version_code(
        "<!-- appupdate versionCode=abc versionName=0.1.1 -->") is None
    assert guard.parse_version_code("") is None


def test_build_notes_first_line_is_parseable_metadata():
    notes = guard.build_notes(53, DIGEST.upper(), "- 修 bug (abc1234)")
    lines = notes.splitlines()
    # 首行：旧客户端（0.1.52 及更早）可解析 — 字段顺序与 --> 结尾都不能动
    assert lines[0] == "<!-- appupdate versionCode=53 versionName=0.1.53 -->"
    # 第二行：哈希单独一条注释，新客户端合并解析
    assert lines[1] == "<!-- apkupdate apkSha256=%s -->" % DIGEST
    assert DIGEST in notes              # 人可读的校验段
    assert "abc1234" in notes
    # App 端能原样解析（跨模块契约）
    info = update.parse_release({
        "name": "0.1.53", "body": notes,
        "assets": [{"name": update.ASSET_NAME, "size": 1,
                    "browser_download_url": "https://github.com/x/y.apk"}]})
    assert info is not None
    assert (info.version_code, info.version_name) == (53, "0.1.53")
    assert info.apk_sha256 == DIGEST


def test_old_client_can_still_parse_new_notes():
    r"""兼容红线：新说明的首行必须能被**旧版客户端**的严格正则解析。

    旧版（≤0.1.52）用 `versionCode=(\d+)\s+versionName=([^\s>-]+)\s*-->`
    整行匹配；一旦把 apkSha256 塞进首行，老用户就永远收不到更新了。
    """
    import re
    old_re = re.compile(
        r"<!--\s*appupdate\s+versionCode=(\d+)\s+versionName=([^\s>-]+)\s*-->")
    notes = guard.build_notes(88, DIGEST, "- x")
    first = notes.splitlines()[0].strip()
    m = old_re.search(first)
    assert m, f"旧客户端解析不了首行：{first!r}"
    assert (m.group(1), m.group(2)) == ("88", "0.1.88")


def test_build_notes_refuses_missing_or_bad_sha():
    """没哈希就不许生成发版说明（宁可不发版，也不发无法校验的包）。"""
    for bad in ("", None, "deadbeef", "z" * 64):
        with pytest.raises(ValueError):
            guard.build_notes(53, bad)


def test_guard_cli_notes_fails_closed(tmp_path, capsys):
    out = tmp_path / "notes.md"
    rc = guard.main(["notes", "--build-num", "53", "--sha256", "",
                     "--out", str(out)])
    assert rc == 2
    assert not out.exists()


def test_guard_cli_notes_writes_file(tmp_path):
    out = tmp_path / "notes.md"
    commits = tmp_path / "commits.txt"
    commits.write_text("- 修 bug (abc1234)", encoding="utf-8")
    rc = guard.main(["notes", "--build-num", "53", "--sha256", DIGEST,
                     "--commits-file", str(commits), "--out", str(out)])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert text.splitlines()[0] == guard.metadata_line(53)
    assert text.splitlines()[1] == guard.sha256_line(DIGEST)
    assert "abc1234" in text


def test_decide_matrix():
    """远端更大 → skip；同号 → upload（重跑修复）；更小/缺失 → upload。"""
    assert guard.decide(None, 53) == "upload"
    assert guard.decide(52, 53) == "upload"
    assert guard.decide(53, 53) == "upload"
    assert guard.decide(54, 53) == "skip"
    assert guard.decide("54", "53") == "skip"


def test_guard_cli_decide_stdout_is_bare_verdict(tmp_path, capsys):
    """CI 用 $(...) 抓结论：stdout 只能有一行 verdict，人看的信息走 stderr。"""
    body = tmp_path / "body.txt"
    body.write_text("<!-- appupdate versionCode=54 versionName=0.1.54 -->",
                    encoding="utf-8")
    rc = guard.main(["decide", "--build-num", "53",
                     "--remote-body-file", str(body)])
    out = capsys.readouterr()
    assert rc == 0
    assert out.out.strip() == "skip"
    assert "skip" in out.err

    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    rc = guard.main(["decide", "--build-num", "53",
                     "--remote-body-file", str(empty)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "upload"


def test_guard_cli_decide_missing_file_is_upload(tmp_path, capsys):
    """读不到远端说明（没发过版 / gh 失败）→ 允许上传，别把首次发版卡死。"""
    rc = guard.main(["decide", "--build-num", "1",
                     "--remote-body-file", str(tmp_path / "nope.txt")])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "upload"


# ---------- 2026-11-20 P0：apkSigner 元数据 + 构建期注入/自检 ----------

SIGNER = "d" * 64


def _colon(fp):
    return ":".join(fp[i:i + 2] for i in range(0, len(fp), 2)).upper()


def _step_index(name_part):
    for i, s in enumerate(_steps()):
        if name_part in str(s.get("name") or ""):
            return i
    raise AssertionError(f"workflow 里找不到步骤：{name_part}")


def test_build_notes_includes_signer_on_second_line():
    """签名指纹进第二行；首行一字不动；App 端能解析出来。"""
    notes = guard.build_notes(61, DIGEST, "- x", signer=_colon(SIGNER))
    lines = notes.splitlines()
    assert lines[0] == "<!-- appupdate versionCode=61 versionName=0.1.61 -->"
    assert lines[1] == ("<!-- apkupdate apkSha256=%s apkSigner=%s -->"
                        % (DIGEST, SIGNER))
    assert SIGNER in notes                      # 人可读段也摊开，供人工核对
    info = update.parse_release({
        "name": "0.1.61", "body": notes,
        "assets": [{"name": update.ASSET_NAME, "size": 1,
                    "browser_download_url": "https://github.com/x/y.apk"}]})
    assert info is not None
    assert info.apk_sha256 == DIGEST
    assert info.apk_signer == SIGNER


def test_build_notes_without_signer_keeps_old_second_line():
    """老格式（手工发版/没指纹）第二行仍是纯 apkSha256，App 端当「不知道」。"""
    notes = guard.build_notes(61, DIGEST)
    assert notes.splitlines()[1] == "<!-- apkupdate apkSha256=%s -->" % DIGEST
    info = update.parse_release({
        "name": "x", "body": notes,
        "assets": [{"name": update.ASSET_NAME, "size": 1,
                    "browser_download_url": "u"}]})
    assert info.apk_signer == ""
    assert info.has_signer() is False


def test_guard_refuses_bad_signer():
    """指纹给了但格式非法 → 拒绝发版（不能让 App 端静默忽略，换钥就没预警了）。"""
    for bad in ("deadbeef", "z" * 64, "a" * 63):
        with pytest.raises(ValueError):
            guard.build_notes(61, DIGEST, signer=bad)
        with pytest.raises(ValueError):
            guard.sha256_line(DIGEST, signer=bad)


def test_guard_cli_notes_accepts_signer(tmp_path):
    out = tmp_path / "notes.md"
    rc = guard.main(["notes", "--build-num", "61", "--sha256", DIGEST,
                     "--signer", _colon(SIGNER), "--out", str(out)])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert ("<!-- apkupdate apkSha256=%s apkSigner=%s -->" % (DIGEST, SIGNER)
            in text)


def test_old_client_can_still_parse_notes_with_signer():
    """兼容红线：带 apkSigner 的说明，首行仍能被旧客户端严格正则解析。"""
    import re
    old_re = re.compile(
        r"<!--\s*appupdate\s+versionCode=(\d+)\s+versionName=([^\s>-]+)\s*-->")
    first = guard.build_notes(61, DIGEST, signer=SIGNER).splitlines()[0].strip()
    m = old_re.search(first)
    assert m and (m.group(1), m.group(2)) == ("61", "0.1.61")
    assert "apkSigner" not in first


def test_workflow_injects_signer_before_build():
    """构建前：从 keystore 算指纹 → 写进 _version.py → 再构建。"""
    step = _step("Compute signer fingerprint")
    assert step.get("id") == "signer"
    run = str(step.get("run") or "")
    assert "signer_guard.py keystore" in run
    assert "--keystore" in run and "--alias" in run

    ver_run = str(_step("Generate _version.py").get("run") or "")
    assert "APP_SIGNER_SHA256" in ver_run
    assert "steps.signer.outputs.sha256" in ver_run

    assert _step_index("Compute signer fingerprint") < \
        _step_index("Generate _version.py") < _step_index("Build APK")


def test_workflow_verifies_apk_signer_after_build():
    """构建后：用 apksigner 核对产物签名与注入值一致（不一致拒绝发版）。"""
    run = str(_step("Verify APK signer").get("run") or "")
    assert "signer_guard.py verify" in run
    assert "--expected" in run and "steps.signer.outputs.sha256" in run
    assert _step_index("Stage APK") < _step_index("Verify APK signer")


def test_notes_step_passes_signer():
    step = _step("Write release notes")
    env = step.get("env") or {}
    assert "steps.signer.outputs.sha256" in str(env.get("APK_SIGNER") or "")
    assert "--signer" in str(step.get("run") or "")


def test_published_metadata_asserts_signer():
    step = _step("Verify published release metadata")
    run = str(step.get("run") or "").lower()
    assert "apksigner" in run
    assert 'attrs.get("apksigner") == signer' in run
    env = step.get("env") or {}
    assert "steps.signer.outputs.sha256" in str(env.get("APK_SIGNER") or "")
