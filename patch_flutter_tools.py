# -*- coding: utf-8 -*-
"""给 flutter_tools 的 symlink 检查打补丁：无开发者模式时跳过而非报错。

仅影响 IDE 的插件符号链接，不影响安卓构建（gradle 用 .flutter-plugins-dependencies）。
"""
import io
import sys

P = r"E:\engine\flutter\3.44.8\packages\flutter_tools\lib\src\flutter_plugins.dart"

src = io.open(P, encoding="utf-8").read()
target = "throwToolExit('Building with plugins requires symlink support.\\n\\n$instructions');"
replacement = (
    "globals.logger.printWarning(\n"
    "        'WARNING(jiuguan patch): symlink 特权不足(1314), '\n"
    "        '已跳过插件 symlink(仅影响 IDE, 不影响安卓构建)。$instructions');\n"
    "      return;"
)
if "jiuguan patch" in src:
    print("补丁已存在，跳过")
    sys.exit(0)
assert target in src, "未找到目标文本（flutter_tools 可能已更新）"
io.open(P, "w", encoding="utf-8", newline="").write(src.replace(target, replacement))
print("补丁已写入:", P)
