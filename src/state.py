# -*- coding: utf-8 -*-
"""手机版会话状态：AppState 单例 + 当前项目访问。"""
import bootstrap  # noqa: F401  noqa必须先于其他本地导入执行 sys.path 注入


class Session:
    """进程级会话：一个 AppState（全局配置库 + 最近项目 + 当前打开项目）。"""

    def __init__(self):
        from app.core.appstate import AppState
        self.app: "AppState" = AppState()
        self.page = None  # flet Page，main() 里赋值

    # ---------- 项目 ----------

    @property
    def store(self):
        """当前打开的项目（未打开为 None）。"""
        return self.app.store

    @property
    def has_project(self) -> bool:
        return self.app.store is not None

    def open_project(self, path: str):
        return self.app.open_project(path)

    def close_project(self):
        self.app.close_project()

    def create_project(self, name: str):
        """手机上新建项目：统一放应用数据目录 projects/<安全名>/。"""
        import os
        root = os.path.join(self.app.data_dir, "projects")
        os.makedirs(root, exist_ok=True)
        folder = os.path.join(root, safe_folder_name(name))
        return self.app.create_project(folder, name)

    def pipeline(self):
        from app.core.pipeline import Pipeline
        return Pipeline(self.app.store)


def safe_folder_name(name: str) -> str:
    import re
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", (name or "").strip())[:40]
    return s or "未命名"
