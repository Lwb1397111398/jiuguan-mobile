# -*- coding: utf-8 -*-
"""AppState：应用全局状态（最近项目、全局配置库）。

模块化约定：应用级配置（模型/路由/偏好/预设）在 GlobalStore，项目级配置在 ProjectStore。
"""
import json
import os
import shutil

from .global_store import GlobalStore
from .paths import app_data_dir
from .store import ProjectStore


class AppState:
    """全局应用状态（进程内单例）。"""

    def __init__(self):
        self.data_dir = app_data_dir()
        self.config_path = os.path.join(self.data_dir, "config.json")
        self.config = self._load_config()
        # 全局配置库（跨项目共享同一套模型/路由/偏好/预设）
        self.gstore = GlobalStore.default()
        self._store: ProjectStore | None = None
        self._store_folder: str | None = None
        # 停靠区：还有后台任务在跑的项目库暂存在这里（不关连接），任务归零后由
        # UI 层调 close_parked 关闭。key = normcase(绝对路径)。
        self._parked: dict[str, ProjectStore] = {}

    @staticmethod
    def _folder_key(folder: str) -> str:
        return os.path.normcase(os.path.abspath(folder))

    # ---------- 配置 ----------

    def _load_config(self) -> dict:
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                raise ValueError("config 不是对象")
            return cfg
        except Exception:
            # 读取失败（损坏/被占用）：先把坏文件备份下来，再返回默认。
            # 否则后续 save_config() 会用默认配置覆盖，用户的项目记录等数据将丢失。
            try:
                if os.path.isfile(self.config_path):
                    shutil.copy2(self.config_path, self.config_path + ".corrupt.bak")
            except OSError:
                pass
            return {"recent_projects": [], "auto_backup": True,
                    "backup_interval_min": 10, "backup_keep": 10}

    def save_config(self):
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def get_setting(self, key: str, default=None):
        return self.config.get(key, default)

    def set_setting(self, key: str, value):
        self.config[key] = value
        self.save_config()

    # ---------- 最近项目 ----------

    def list_recent(self) -> list[dict]:
        """全部最近项目记录（新打开在前），附 exists 标记（文件夹是否仍存在）。

        不再过滤失效项：项目记录应持久保留，文件夹被移动/清理后记录不能静默消失，
        由 UI 显示灰色 + 提供「移除记录」入口。
        """
        items = []
        for item in self.config.get("recent_projects", []):
            path = item.get("path", "")
            it = dict(item)
            it["exists"] = bool(path) and os.path.isdir(path)
            items.append(it)
        return items

    def add_recent(self, path: str, title: str = ""):
        items = [i for i in self.config.get("recent_projects", [])
                 if os.path.normcase(i.get("path", "")) != os.path.normcase(path)]
        items.insert(0, {"path": path, "title": title, "last_open": __import__("datetime").datetime.now().isoformat(timespec="seconds")})
        self.config["recent_projects"] = items[:30]
        self.save_config()

    def remove_recent(self, path: str):
        self.config["recent_projects"] = [
            i for i in self.config.get("recent_projects", [])
            if os.path.normcase(i.get("path", "")) != os.path.normcase(path)]
        self.save_config()

    # ---------- 项目 ----------

    def create_project(self, folder: str, title: str = "") -> ProjectStore:
        """新建项目：创建文件夹 + 项目数据库 + 种子数据。"""
        os.makedirs(folder, exist_ok=True)
        self.close_project()  # 先关闭旧项目连接，避免句柄堆积/双连接指向同一库
        store = ProjectStore(folder, global_store=self.gstore)
        if title:
            store.set_title(title)
        self._store = store
        self._store_folder = folder
        self.add_recent(folder, store.title)
        return store

    def open_project(self, folder: str) -> ProjectStore:
        """打开已有项目文件夹。

        若目标项目有停靠中的库（后台任务在用它），直接复用那条连接重新绑定，
        避免同一数据库出现双连接；否则新建。
        调用前 UI 层负责决定当前项目的去留：还有后台任务时先调 park_current_store()
        把它挪进停靠区，这里 close_project() 自然变成空操作。
        """
        if not os.path.isdir(folder):
            raise FileNotFoundError(f"项目文件夹不存在: {folder}")
        self.close_project()
        key = self._folder_key(folder)
        store = self._parked.pop(key, None)
        if store is None:
            store = ProjectStore(folder, global_store=self.gstore)
        self._store = store
        self._store_folder = folder
        self.add_recent(folder, store.title)
        return store

    def close_project(self):
        if self._store:
            self._store.close()
        self._store = None
        self._store_folder = None

    def park_current_store(self):
        """当前项目库转入停靠区（不关连接）：后台任务（批量改写/生成）继续用。

        同一路径已有停靠库属异常（正常流程同一时刻只有一个活动库），防御性关掉旧的。
        """
        if self._store is None:
            return
        key = self._folder_key(self._store_folder)
        old = self._parked.get(key)
        self._parked[key] = self._store
        self._store = None
        self._store_folder = None
        if old is not None:
            old.close()

    def parked_store(self, folder: str) -> ProjectStore | None:
        """查询某路径是否有停靠中的库（不取出）。"""
        return self._parked.get(self._folder_key(folder))

    def close_parked(self, folder: str):
        """关闭某路径的停靠库（后台任务归零后由 UI 层调用）。"""
        key = self._folder_key(folder)
        store = self._parked.pop(key, None)
        if store is not None:
            store.close()

    def close_all_parked(self):
        """应用退出前清空停靠区。"""
        for store in self._parked.values():
            store.close()
        self._parked.clear()

    @property
    def store(self) -> ProjectStore | None:
        return self._store

    @property
    def project_folder(self) -> str | None:
        return self._store_folder

    @property
    def global_store(self) -> GlobalStore:
        return self.gstore
