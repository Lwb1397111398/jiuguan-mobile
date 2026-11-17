# -*- coding: utf-8 -*-
"""CI 门禁：读 pytest 的 JUnit XML，禁止 skip / xfail / failure / error。

「不静默 skip/xfail」不是一句口号，而是一道会红的检查：
- 任何 <skipped> / <xfailed>（含 importorskip、平台条件跳过）都判红；
- 任何 <failure> / <error> 也判红（pytest 步骤本身已红，这里再兜一次）；
- 用例数必须不低于下限，防「收集失败、0 测试通过」的假绿。

跳过若真有必要，必须改测试让它在新环境里跑起来（例如 CI 里检出手机仓），
而不是在 CI 命令里加 -k / --ignore / 放宽条件把它们藏起来。

用法：python tools/ci/check_pytest_results.py pytest-results.xml
"""
import os
import sys
import xml.etree.ElementTree as ET

# 本机全量约 900+ 用例；下限只用来拦「一个都没收集到」的假绿
MIN_TESTS = int(os.environ.get("CI_MIN_TESTS", "500"))
BAD_TAGS = {"skipped", "xfailed", "xpassed", "failure", "error"}


def main(path):
    if not os.path.isfile(path):
        print(f"[CI] 找不到 JUnit 结果文件：{path}", file=sys.stderr)
        return 2
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    cases = [c for s in suites for c in s.iter("testcase")]
    bad = []
    for c in cases:
        name = f"{c.get('classname') or ''}::{c.get('name') or ''}"
        for child in c:
            tag = child.tag.lower().split("}")[-1]
            if tag in BAD_TAGS:
                bad.append((tag, name, (child.get("message") or "")[:140]))
    print(f"[CI] 用例数={len(cases)} 下限={MIN_TESTS} 异常条数={len(bad)}")
    for tag, name, msg in bad[:25]:
        print(f"[CI]   {tag}: {name} — {msg}")
    if len(bad) > 25:
        print(f"[CI]   … 另有 {len(bad) - 25} 条")
    if bad:
        print("[CI] 存在 skipped/xfail/xpass/failure/error，判红（不允许静默跳过）",
              file=sys.stderr)
        return 1
    if len(cases) < MIN_TESTS:
        print(f"[CI] 用例数 {len(cases)} < {MIN_TESTS}，疑似收集失败，判红",
              file=sys.stderr)
        return 1
    print("[CI] 完整 pytest：无跳过、无 xfail、无失败 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "pytest-results.xml"))
