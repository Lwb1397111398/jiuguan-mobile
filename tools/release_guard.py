# -*- coding: utf-8 -*-
r"""发版守卫（CI 与单测共用，纯标准库、不 import 项目代码）。

两个职责，都是「发版正确性」的唯一源：

1. **Release 说明生成**（`notes` 子命令）：把 App 端解析用的元数据注释和
   人可读的安装包校验信息写到 notes.md。元数据格式在 App 侧由
   `src/update.py:parse_meta_attrs` 解析，两边必须同步改（单测里用
   `update.parse_release` 反解本模块生成的说明，格式一歪就红）。
   SHA-256 缺失或格式非法 → 直接非零退出：宁可不发版，也不发一个
   「App 无法校验」的包（2026-11-17 第一阶段）。

   **格式红线（向后兼容）**：第一行必须是
   `<!-- appupdate versionCode=N versionName=0.1.N -->`，
   字段顺序与 `-->` 结尾都不能动 —— 0.1.52 及更早的客户端用
   `versionCode=(\d+)\s+versionName=([^\s>-]+)\s*-->` 整行匹配，多一个属性
   就会让它们解析失败、以为「已是最新版」，从此收不到任何更新。
   所以哈希单独放第二行 `<!-- apkupdate apkSha256=<hex> -->`。
   单测 `test_old_client_can_still_parse_new_notes` 钉死这条。

2. **版本单调判定**（`decide` 子命令）：云端 latest 的 versionCode 比本次
   构建更大时，说明有更新的构建已经发布 → 本次构建放弃上传。
   workflow 级 concurrency 已经让同一组排队执行，但 `workflow_dispatch`
   重跑旧提交、或有人手工改 Release，仍可能让旧包后到；守卫是第二道防线。

用法（CI 里就这么调）：
    python tools/release_guard.py notes  --build-num 53 --sha256 <hex> \\
        --commits-file commits.txt --out notes.md
    python tools/release_guard.py decide --build-num 53 --remote-body-file body.txt
"""
import argparse
import re
import sys

REPO = "Lwb1397111398/jiuguan-mobile"
ASSET_NAME = "jiuguan-novel.apk"

_META_RE = re.compile(r"<!--\s*appupdate\b([^>]*)-->")
_ATTR_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^\s>]+)")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

VERDICT_UPLOAD = "upload"
VERDICT_SKIP = "skip"


def normalize_sha256(value) -> str:
    """小写化并去掉空白/`sha256:` 前缀；非法返回空串。"""
    s = str(value or "").strip().lower()
    if s.startswith("sha256:"):
        s = s[len("sha256:"):].strip()
    return s if _SHA256_RE.match(s) else ""


def parse_version_code(body):
    """从 Release 说明里读 versionCode；没有元数据/格式不对返回 None。"""
    m = _META_RE.search(str(body or ""))
    if not m:
        return None
    attrs = {}
    for key, value in _ATTR_RE.findall(m.group(1)):
        attrs.setdefault(key.lower(), value)
    code = str(attrs.get("versioncode") or "")
    return int(code) if code.isdigit() else None


def metadata_line(build_num) -> str:
    """**旧客户端可解析**的首行元数据（字段顺序与结尾都不能动，见模块说明）。"""
    code = int(build_num)
    return (f"<!-- appupdate versionCode={code} versionName=0.1.{code} -->")


def sha256_line(sha256) -> str:
    """哈希单独一行：新客户端会把这条注释和首行合并解析。"""
    digest = normalize_sha256(sha256)
    if not digest:
        raise ValueError("SHA-256 缺失或格式非法，拒绝生成发版说明")
    return f"<!-- apkupdate apkSha256={digest} -->"


def build_notes(build_num, sha256, commits="") -> str:
    """生成完整 Release 说明：两行元数据 + 更新内容 + 校验值 + 人工安装提示。"""
    digest = normalize_sha256(sha256)
    if not digest:
        raise ValueError("SHA-256 缺失或格式非法，拒绝生成发版说明")
    body = str(commits or "").strip()
    if not body:
        body = "- （本次提交没有可用说明）"
    return "\n".join([
        metadata_line(build_num),
        sha256_line(digest),
        "",
        "## 更新内容",
        "",
        body,
        "",
        "## 安装包校验（SHA-256）",
        "",
        f"文件：`{ASSET_NAME}`",
        "",
        digest,
        "",
        "App 内更新会自动校验这个值，校验不通过不会安装；"
        "用浏览器手动下载的，安装前请自行核对文件 SHA-256 与上面一致。",
        "",
        "官方地址："
        f"https://github.com/{REPO}/releases/download/latest/{ASSET_NAME}",
        "",
    ])


def decide(remote_code, build_num) -> str:
    """远端更大的 versionCode 已存在 → skip；否则 upload（同号允许重传修复）。"""
    build_num = int(build_num)
    if remote_code is None:
        return VERDICT_UPLOAD
    return VERDICT_SKIP if int(remote_code) > build_num else VERDICT_UPLOAD


def _read(path) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="发版守卫：说明生成 / 版本单调判定")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_notes = sub.add_parser("notes", help="生成 Release 说明（含 SHA-256 元数据）")
    p_notes.add_argument("--build-num", required=True, type=int)
    p_notes.add_argument("--sha256", required=True)
    p_notes.add_argument("--commits-file", default="")
    p_notes.add_argument("--out", required=True)

    p_dec = sub.add_parser("decide", help="判断本次构建该不该上传 latest")
    p_dec.add_argument("--build-num", required=True, type=int)
    p_dec.add_argument("--remote-body-file", required=True)

    args = ap.parse_args(argv)

    if args.cmd == "notes":
        try:
            text = build_notes(args.build_num, args.sha256,
                               _read(args.commits_file) if args.commits_file else "")
        except ValueError as e:
            print(f"release_guard: {e}", file=sys.stderr)
            return 2
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print(f"release_guard: notes 已写入 {args.out}"
              f"（versionCode={args.build_num}, "
              f"sha256={normalize_sha256(args.sha256)}）")
        return 0

    remote = parse_version_code(_read(args.remote_body_file))
    verdict = decide(remote, args.build_num)
    # 人看的信息走 stderr，stdout 只留裸结论（CI 用 $(...) 抓它）
    print(f"release_guard: remote={remote} build={args.build_num} -> {verdict}",
          file=sys.stderr)
    print(verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main())
