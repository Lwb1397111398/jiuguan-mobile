# -*- coding: utf-8 -*-
r"""签名证书指纹守卫（构建期工具；纯标准库，可被单测直接 import）。

P0「旧钥过渡版」只做两件事，两件都由本模块提供唯一实现：

1. **构建前**：从**实际签名材料**（keystore）算出证书 SHA-256，注入
   `src/_version.py` 的 `APP_SIGNER_SHA256`。App 端拿它和 Release 元数据里的
   `apkSigner` 比对，决定「能不能直接覆盖安装」——**不用 pyjnius 读运行中的
   签名**（老板平板 pyjnius 整链失败，见 `手机自更新模块总览.md` §七）。
2. **构建后**：从**构建产物**（APK，`apksigner verify --print-certs`）再算一次，
   与注入值比对；不一致立即失败（防止误用旧钥/空钥签出「能装但装不上」的包）。

为什么两处都要做：keystore 是「打算用哪把钥」，APK 是「实际签成了什么」；
只有两者相等，App 端那个常量才可信。

用法（本机 bat 与 CI 同一套命令）：

    python tools/signer_guard.py keystore --keystore android/keystore/app.keystore \
        --alias androiddebugkey --storepass android
    python tools/signer_guard.py verify --expected <64hex> --apk jiuguan-novel.apk

输出：成功时 stdout 只有一行 64 位小写 hex（供脚本 `for /f` / `$(...)` 抓取），
诊断信息走 stderr。退出码：0 成功/一致；2 取不到指纹（缺工具、解析失败、参数错）；
3 指纹不一致（verify）。

**不打印任何口令**：命令里带 `-storepass`，但错误消息只回显 keytool 的 stderr。
"""
import argparse
import os
import re
import shutil
import subprocess
import sys

KEYSTORE_ALIAS = "androiddebugkey"
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
# keytool -list -v 的证书指纹行：`\t SHA256: FE:A1:...`（同一段里还有 SHA1）
_KEYTOOL_SHA256_RE = re.compile(r"^\s*SHA256:\s*([0-9A-Fa-f:]+)\s*$", re.M)
# apksigner verify --print-certs：`Signer #1 certificate SHA-256 digest: fea1...`
_APKSIGNER_SHA256_RE = re.compile(
    r"certificate SHA-256 digest:\s*([0-9a-fA-F]+)")


def normalize_fingerprint(value) -> str:
    """规范化证书指纹：小写、去空白/冒号/短横线，允许 `sha256:` 前缀。

    keytool 打印 `FE:A1:B9:…`，apksigner 打印 `fea1b974…` —— 同一个证书两种
    写法，比较前必须归一。非法值返回空串（= 取不到），**绝不**把半截哈希
    当有效值（半截值会让「相等」变成假阴性/假阳性）。
    """
    s = str(value or "").strip().lower()
    if s.startswith("sha256:"):
        s = s[len("sha256:"):]
    s = re.sub(r"[\s:\-]", "", s)
    return s if _HEX64_RE.match(s) else ""


def parse_keytool_sha256(text) -> str:
    """从 `keytool -list -v` 的输出里取 SHA-256 证书指纹；没有返回空串。"""
    m = _KEYTOOL_SHA256_RE.search(str(text or ""))
    return normalize_fingerprint(m.group(1)) if m else ""


def parse_apksigner_sha256(text) -> str:
    """从 `apksigner verify --print-certs` 的输出里取 SHA-256；没有返回空串。

    多签名（密钥轮换）时取第一个 Signer —— 与「装得上与否只看当前签名证书」
    一致；本项目的包只有一个签名者。
    """
    m = _APKSIGNER_SHA256_RE.search(str(text or ""))
    return normalize_fingerprint(m.group(1)) if m else ""


def _version_key(name):
    """build-tools 目录名排序键：`36.1.0` > `36.0.0` > `9.0.0`（按数字段）。"""
    parts = [int(p) for p in re.findall(r"\d+", str(name or ""))]
    return tuple(parts) if parts else (0,)


def find_apksigner(explicit=None, sdk_root=None):
    """定位 apksigner（优先显式路径 → 环境变量 SDK → PATH）；找不到返回空串。"""
    if explicit and os.path.isfile(explicit):
        return explicit
    if explicit:
        found = shutil.which(explicit)
        if found:
            return found
    root = (sdk_root or os.environ.get("ANDROID_SDK_ROOT")
            or os.environ.get("ANDROID_HOME") or "")
    roots = [root] if root else []
    # CI 兜底：GitHub ubuntu runner 的 Android SDK 常见位置（env 缺失时也不至于失败）
    roots += ["/usr/local/lib/android/sdk"]
    for r in roots:
        bt = os.path.join(r, "build-tools")
        if not os.path.isdir(bt):
            continue
        for name in sorted(os.listdir(bt), key=_version_key, reverse=True):
            for exe in ("apksigner.bat", "apksigner"):
                p = os.path.join(bt, name, exe)
                if os.path.isfile(p):
                    return p
    return shutil.which("apksigner") or ""


def find_keytool(explicit=None):
    """定位 keytool（优先显式路径 → JAVA_HOME → PATH）；找不到返回空串。"""
    if explicit and os.path.isfile(explicit):
        return explicit
    if explicit:
        found = shutil.which(explicit)
        if found:
            return found
    java_home = os.environ.get("JAVA_HOME") or ""
    if java_home:
        for exe in ("keytool.exe", "keytool"):
            p = os.path.join(java_home, "bin", exe)
            if os.path.isfile(p):
                return p
    return shutil.which("keytool") or ""


def _run(cmd):
    """跑一个命令 → (returncode, stdout+stderr)。测试里 monkeypatch 这个函数。

    不把 cmd 拼进错误消息（里面有 `-storepass`）：只回显工具自己的输出。
    """
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, encoding="utf-8", errors="replace")
        return int(p.returncode), (p.stdout or "")
    except OSError as e:
        return 127, f"{type(e).__name__}: {e}"


def keystore_fingerprint(keystore, alias=KEYSTORE_ALIAS, storepass="",
                         keytool=None):
    """keystore 里指定 alias 的证书 SHA-256；失败抛 RuntimeError（中文原因）。"""
    if not keystore or not os.path.isfile(str(keystore)):
        raise RuntimeError(f"签名材料不存在：{keystore}")
    tool = find_keytool(keytool)
    if not tool:
        raise RuntimeError("找不到 keytool（检查 JAVA_HOME 或 Android Studio 自带 JDK）")
    cmd = [tool, "-list", "-v", "-keystore", str(keystore), "-alias", str(alias),
           # 强制英文输出：中文 JDK 会把「证书指纹」本地化，正则会失配
           "-J-Duser.language=en", "-J-Duser.country=US"]
    if storepass:
        cmd += ["-storepass", str(storepass)]
    rc, out = _run(cmd)
    if rc != 0:
        # 输出里可能含 alias/路径，但不含口令；截断避免刷屏
        raise RuntimeError(f"keytool 读取签名材料失败（退出码 {rc}）：{out.strip()[:400]}")
    fp = parse_keytool_sha256(out)
    if not fp:
        raise RuntimeError("keytool 输出里找不到 SHA256 证书指纹（输出格式变了？）")
    return fp


def apk_fingerprint(apk, apksigner=None):
    """APK 实际签名证书的 SHA-256（v1/v2/v3 由 apksigner 统一处理）。"""
    if not apk or not os.path.isfile(str(apk)):
        raise RuntimeError(f"APK 不存在，无法核对签名：{apk}")
    tool = find_apksigner(apksigner)
    if not tool:
        raise RuntimeError(
            "找不到 apksigner（检查 ANDROID_SDK_ROOT/build-tools 或 PATH）")
    rc, out = _run([tool, "verify", "--print-certs", str(apk)])
    if rc != 0:
        raise RuntimeError(f"apksigner 校验失败（退出码 {rc}）：{out.strip()[:400]}")
    fp = parse_apksigner_sha256(out)
    if not fp:
        raise RuntimeError("apksigner 输出里找不到 SHA-256（APK 可能没签名）")
    return fp


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="签名证书指纹：读取 / 注入前校验")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_ks = sub.add_parser("keystore", help="从 keystore 读证书 SHA-256")
    p_ks.add_argument("--keystore", required=True)
    p_ks.add_argument("--alias", default=KEYSTORE_ALIAS)
    p_ks.add_argument("--storepass", default=os.environ.get(
        "JIUGUAN_KEYSTORE_PASS", ""))
    p_ks.add_argument("--keytool", default="")

    p_apk = sub.add_parser("apk", help="从已签名 APK 读证书 SHA-256")
    p_apk.add_argument("--apk", required=True)
    p_apk.add_argument("--apksigner", default="")

    p_v = sub.add_parser("verify", help="核对 APK 签名与期望值是否一致")
    p_v.add_argument("--expected", required=True)
    p_v.add_argument("--apk", required=True)
    p_v.add_argument("--apksigner", default="")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "keystore":
            fp = keystore_fingerprint(args.keystore, args.alias,
                                      args.storepass, args.keytool)
        elif args.cmd == "apk":
            fp = apk_fingerprint(args.apk, args.apksigner)
        else:
            expected = normalize_fingerprint(args.expected)
            if not expected:
                print("signer_guard: 期望指纹格式非法（需要 64 位 hex）",
                      file=sys.stderr)
                return 2
            fp = apk_fingerprint(args.apk, args.apksigner)
            if fp != expected:
                print(f"signer_guard: 签名不一致！期望 {expected[:12]}…，"
                      f"实际 {fp[:12]}…（构建用了别的钥？拒绝发版）",
                      file=sys.stderr)
                return 3
            print(fp)
            return 0
    except RuntimeError as e:
        print(f"signer_guard: {e}", file=sys.stderr)
        return 2
    print(fp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
