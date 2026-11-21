# -*- coding: utf-8 -*-
"""酒馆小说生成器 · 手机版入口（Flet）。

页面装配、底部导航、云同步守护、全量导入的整机重建都在 `runtime.AppRuntime`；
这里只做 Page 级外观设置 + 交给运行时挂载。
"""
import bootstrap  # noqa: F401  必须最先执行：sys.path 注入

import flet as ft

import crashguard


def _version_name() -> str:
    try:
        from _version import APP_VERSION_NAME
        return str(APP_VERSION_NAME)
    except Exception:
        return ""


def main(page: ft.Page):
    # 闪退留痕（APK 无控制台）：装在 main 第一步，之后的异常都能落到
    # 数据目录的 crash.log，入口在「我的 → 崩溃诊断」。
    crashguard.install(_version_name())
    page.title = "酒馆小说生成器"
    page.padding = 0
    page.theme_mode = ft.ThemeMode.SYSTEM
    # 品牌深蓝（与 APP 图标底色 #2A3550 一致）：Material 3 会从 seed 自动派生
    # 整套配色；比旧橙色更贴合「书本/创作」气质，明暗两套主题都稳。
    seed = "#2A3550"
    page.theme = ft.Theme(color_scheme_seed=seed)
    page.dark_theme = ft.Theme(color_scheme_seed=seed)
    # 页面底色走设计系统的语义色（theme.py）：卡片浮在底色上才分层。
    import theme as T
    page.bgcolor = T.bg()
    # 不指定 font_family：安卓自动用系统中文字体（旧值 "Microsoft YaHei UI"
    # 在安卓上不存在，会触发不可控 fallback）

    # 运行时统一持有 Session / 四个主页 / 底部导航，并负责全量导入后的整机重建
    # （旧 Session 与旧页面对象必须一起作废，见 runtime.py 顶部说明）。
    from runtime import AppRuntime
    AppRuntime(page).mount()


if __name__ == "__main__":
    ft.run(main)
