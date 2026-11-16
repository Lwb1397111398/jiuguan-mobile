# -*- coding: utf-8 -*-
"""云同步的手机端粘合层：打开项目自动拉取 + 改动静默后自动上传。

同步引擎在 `app/core/sync_engine.py`（桌面/手机同一份，由 sync_core.py 同步
过来），本模块不改内核，只补两处手机特有的接线：

1. `pull_on_open()` —— 打开一本书时先比对云端 manifest，云端更新才下载合并。
   判断与桌面 `main_window._auto_pull_current` 完全一致：云端 sha 与本机游标
   相同就跳过，别白下载（胶囊下行只有 0.3 MB/s）。
2. `Watcher` —— 一条守护线程，每 8 秒看一眼当前项目的 `write_seq`。
   `ProjectStore` 每次提交都会把它 +1，所以**不需要在每个写入点埋钩子**：
   生成、改写、归档、手改正文全都自然覆盖。停手满 IDLE_SECONDS 才上传。

三条纪律：
- 只在「启用 + 开关打开 + 连接齐 + 本机有密钥」时动手，任一条不满足就静默退出；
- 网络动作全在这条守护线程里做，绝不碰 flet 事件线程（真机卡住＝闪退）；
- 同一时刻只允许一个同步动作（模块级 `_LOCK`），避免上传与拉取互相踩。
"""
import threading
import time

from widgets import toast

_LOCK = threading.Lock()
IDLE_SECONDS = 60       # 最后一次写入后安静多久才上传
POLL_SECONDS = 8        # 守护线程轮询间隔
RETRY_SECONDS = 300     # 上传失败后多久再试（别每次轮询都撞一次网络）


# ---------------- 基础判断 ----------------

def _sc():
    from app.core import sync_config
    return sync_config


def _g(session, key: str, default: str = "") -> str:
    try:
        return str(session.app.gstore.get_global_setting(key, default))
    except Exception:
        return default


def enabled(session) -> bool:
    return _g(session, "sync_enabled", "0") == "1"


def ready(session) -> bool:
    """连接齐了 + 本机有同步密钥，才允许自动同步。"""
    try:
        sc = _sc()
        cfg = sc.load_config()
        return sc.is_configured(cfg) and sc.get_crypto(cfg) is not None
    except Exception:
        return False


def _engine():
    from app.core.sync_engine import SyncEngine
    eng = SyncEngine()
    eng.bind("")        # 本机已有密钥则直接复用
    return eng


def _seconds_since(stamp: str) -> float:
    if not stamp:
        return 1e9
    try:
        from datetime import datetime
        return max(0.0, (datetime.now() - datetime.fromisoformat(stamp)).total_seconds())
    except (TypeError, ValueError):
        return 1e9


def _safe_update(page):
    try:
        page.update()
    except Exception:
        pass


# ---------------- 打开项目时拉取 ----------------

def pull_on_open(page, session, on_pulled=None) -> bool:
    """打开项目后异步拉一次云端（只拉不推）。返回是否已排入后台。"""
    if not enabled(session) or _g(session, "sync_auto_pull", "1") != "1":
        return False
    store = session.store
    if store is None:
        return False
    if not ready(session):
        return False

    def work():
        eng = _engine()
        uid = eng.ensure_uuid(store)
        cur = eng._cursor(uid)               # noqa: SLF001 — 与桌面同一判断
        remote = eng.get_manifest(uid)
        if not remote:
            return {"action": "none"}
        if remote.get("sha256") == cur.get("remote_sha"):
            return {"action": "skip"}        # 云端没变，别白下载
        return eng.pull_project(store, backup=True)

    def run():
        try:
            res = work()
        except Exception as e:               # 网络/解密/密钥任何问题都不能崩线程
            res = {"error": "%s: %s" % (type(e).__name__, e)}
        try:
            if res.get("action") == "pull":
                toast(page, "已从云端同步《%s》（%d 张表）"
                      % (store.title, res.get("tables", 0)))
                if on_pulled:
                    try:
                        on_pulled()
                    except Exception:
                        pass
                _safe_update(page)
            elif res.get("error"):
                toast(page, "云端同步失败：%s" % str(res["error"])[:120], error=True)
                _safe_update(page)
        except Exception:
            pass

    threading.Thread(target=run, daemon=True, name="sync-pull").start()
    return True


# ---------------- 后台上传 ----------------

class Watcher:
    """守护线程：当前项目改动静默 IDLE_SECONDS 后自动上传一次。"""

    def __init__(self, page, session):
        self.page = page
        self.session = session
        self._stop = threading.Event()
        self._pushed = {}       # 项目文件夹 -> 已推送的 write_seq
        self._retry_at = 0.0
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="sync-watcher")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def flush(self, timeout: float = 8.0):
        """退出前尽力推一次。**限时**：网络慢就放弃，不能让人以为应用卡死。"""
        t = threading.Thread(target=self._push, daemon=True, name="sync-flush")
        t.start()
        t.join(timeout)

    # ---------- 内部 ----------

    def _loop(self):
        while not self._stop.wait(POLL_SECONDS):
            try:
                self._tick()
            except Exception:
                pass

    def _tick(self):
        if not enabled(self.session):
            return
        if _g(self.session, "sync_auto_push", "1") != "1":
            return
        store = self.session.store
        if store is None or getattr(store, "is_closed", False):
            return
        key = getattr(store, "folder", "") or ""
        seq = getattr(store, "write_seq", 0)
        if self._pushed.get(key) == seq:
            return                          # 上次推完之后没再写过
        if _seconds_since(getattr(store, "last_write_at", "")) < IDLE_SECONDS:
            return                          # 还在写，等它停手
        if time.monotonic() < self._retry_at:
            return
        if not ready(self.session):
            return
        if self._push(store):
            self._pushed[key] = seq

    def _push(self, store=None) -> bool:
        store = store if store is not None else self.session.store
        if store is None or getattr(store, "is_closed", False):
            return False
        if not ready(self.session):
            return False
        if not _LOCK.acquire(blocking=False):
            return False                    # 有别的同步在跑，下轮再说
        try:
            r = _engine().sync_project(store, direction="push")
            if r.get("action") == "failed":
                self._retry_at = time.monotonic() + RETRY_SECONDS
                return False
            if r.get("action") == "push":
                toast(self.page, "已上传《%s》到云端" % store.title)
                _safe_update(self.page)
            return True
        except Exception as e:
            self._retry_at = time.monotonic() + RETRY_SECONDS
            try:
                print("[sync] 自动上传失败：%s" % e)
            except Exception:
                pass
            return False
        finally:
            _LOCK.release()
