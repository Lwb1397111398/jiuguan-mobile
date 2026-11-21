# -*- coding: utf-8 -*-
"""手机版会话状态：AppState 单例 + 当前项目访问。"""
import bootstrap  # noqa: F401  noqa必须先于其他本地导入执行 sys.path 注入


class Session:
    """进程级会话：一个 AppState（全局配置库 + 最近项目 + 当前打开项目）。

    全会话有一个**失效闸门**（2026-11-20）：全量导入成功后会整体换掉 app.db 与
    projects/，旧 Session 必须立刻作废——旧页面、旧定时器、旧 worker 手里都拿着
    这个对象，只要 `store` 返回 None、`open_project/create_project/pipeline`
    直接拒绝，它们就再也写不进（被替换过的）库。这是「旧页面不能写库」的唯一
    判据，别在页面里各写一份 if。
    """

    def __init__(self):
        from app.core.appstate import AppState
        self.app: "AppState" = AppState()
        self.page = None  # flet Page，main() 里赋值
        self._alive = True

    # ---------- 生命周期 ----------

    @property
    def alive(self) -> bool:
        """会话是否仍然有效（全量导入成功后旧会话置 False）。"""
        return self._alive

    def deactivate(self):
        """让本会话永久失效（全量导入成功时由 runtime 调用，只调用一次）。

        先落旗再关项目：并发中的后台线程下一次问 `store` 就会拿到 None，
        不会在半路继续读写旧连接。之后本对象**不可复用**——要用新库请新建
        Session（runtime 会做）。
        """
        if not self._alive:
            return
        self._alive = False
        try:
            self.app.close_project()
        except Exception:
            pass

    def _ensure_alive(self):
        if not self._alive:
            raise RuntimeError("会话已失效：本机资料已被全量导入替换，"
                               "请使用导入后重建的页面")

    # ---------- 项目 ----------

    @property
    def store(self):
        """当前打开的项目（未打开为 None；旧会话恒为 None）。"""
        if not self._alive:
            return None
        return self.app.store

    @property
    def has_project(self) -> bool:
        return self.store is not None

    def open_project(self, path: str):
        self._ensure_alive()
        return self.app.open_project(path)

    def close_project(self):
        if not self._alive:
            return
        self.app.close_project()

    def create_project(self, name: str):
        """手机上新建项目：统一放应用数据目录 projects/<安全名>/。"""
        self._ensure_alive()
        import os
        root = os.path.join(self.app.data_dir, "projects")
        os.makedirs(root, exist_ok=True)
        folder = os.path.join(root, safe_folder_name(name))
        return self.app.create_project(folder, name)

    def pipeline(self):
        self._ensure_alive()
        from app.core.pipeline import Pipeline
        return Pipeline(self.app.store)


def safe_folder_name(name: str) -> str:
    import re
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", (name or "").strip())[:40]
    return s or "未命名"
