# -*- coding: utf-8 -*-
"""手机版 App 渲染级集成测试（flet test）：真控件树构建。"""
import flet.testing as ftt


async def test_app_launch(flet_app: ftt.FletTestApp):
    """App 启动后应显示项目页（空状态引导文案）。"""
    tester = flet_app.tester
    await tester.pump_and_settle()
    assert (await tester.find_by_text("我的作品")).count == 1
