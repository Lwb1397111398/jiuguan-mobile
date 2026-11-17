# -*- coding: utf-8 -*-
"""CI 门禁：核对实际安装版本 == requirements*.txt 里的 pin。

「不随意升级到未实测版本」不能只靠约定：CI 用
`pip install -c constraints.txt -r requirements-dev.txt` 安装后，这里逐个
比对，任何版本漂移（含传递依赖被悄悄升到新版）都直接判红。

用法：python tools/ci/check_locked_versions.py [仓库根]
"""
import importlib.metadata as md
import os
import re
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
REQ_FILES = ("requirements.txt", "requirements-dev.txt")
PIN = re.compile(r"^([A-Za-z0-9_.\-]+)\s*==\s*([^\s;#]+)")


def pinned():
    out = {}
    for name in REQ_FILES:
        path = os.path.join(ROOT, name)
        if not os.path.isfile(path):
            print(f"[CI] 缺少依赖文件：{path}", file=sys.stderr)
            sys.exit(2)
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                m = PIN.match(line)
                if m:
                    out[m.group(1)] = m.group(2)
    return out


def main():
    pins = pinned()
    bad = []
    for name, want in sorted(pins.items()):
        try:
            got = md.version(name)
        except md.PackageNotFoundError:
            bad.append((name, want, "未安装"))
            continue
        if got != want:
            bad.append((name, want, got))
        print(f"[CI] {name}: 锁定 {want} / 实装 {got}")
    if bad:
        for name, want, got in bad:
            print(f"[CI] 版本漂移 {name}: 应为 {want}，实际 {got}", file=sys.stderr)
        return 1
    print(f"[CI] {len(pins)} 个依赖版本与锁文件一致 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
