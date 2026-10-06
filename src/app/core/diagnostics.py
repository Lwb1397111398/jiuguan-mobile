# -*- coding: utf-8 -*-
"""全局健康诊断：问题登记 → 提醒栏展示 → 恢复后自动消除。

设计原则：
- 事件驱动，不做后台轮询、不发探测请求（避免额外 API 消耗）：
  模块在失败时 record_issue() 登记，成功时 clear_issue() 消除；
  提醒栏（UI 层）定时拉取 list_issues() 刷新。
- 未配置的可选功能（如嵌入/向量）不算问题——只有"配置了但调用失败"
  或"配置不完整"才提醒，与各模块的静默降级红线不冲突。
- 本模块只记录，绝不抛异常打断主流程：任何记录失败都静默吞掉。
- 存储在全局库 app.db 的 meta 表（键前缀 diagnostic_，值 JSON），
  每次读写用独立短连接，兼容 AI 工作线程与测试的 APPDATA 隔离。

接入方式（新模块要上报问题时）：
    from .diagnostics import record_issue, clear_issue
    record_issue("my_module", "标题", "详情", hint="怎么修")
    ...  # 问题自愈后
    clear_issue("my_module")
"""
import json
import os
import sqlite3
import threading
from datetime import datetime

_PREFIX = "diagnostic_issue_"
_lock = threading.Lock()

# 严重级别 → 提醒栏展示（severity 越小越靠前）
SEVERITY_ORDER = {"danger": 0, "warn": 1}


def _db_path() -> str:
    """全局库路径（跟随 APPDATA，测试自动隔离）。"""
    from .paths import app_data_dir
    return os.path.join(app_data_dir(), "app.db")


def _conn():
    import os
    path = _db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    # meta 表与 db.py 的全局库定义一致；诊断可能早于 GlobalStore 首次打开，
    # 自建表保证独立可用（IF NOT EXISTS，与 GlobalStore 建表互不冲突）
    conn.execute("CREATE TABLE IF NOT EXISTS meta ("
                 "key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    return conn


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _decode(raw: str) -> dict | None:
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def record_issue(key: str, title: str, detail: str = "",
                 severity: str = "warn", hint: str = ""):
    """登记（或刷新）一条问题。同 key 重复发生只更新时间/次数/最新详情。"""
    try:
        with _lock:
            conn = _conn()
            try:
                row = conn.execute("SELECT value FROM meta WHERE key=?", (_PREFIX + key,)).fetchone()
                prev = _decode(row["value"]) if row else None
                obj = {
                    "key": key,
                    "title": title,
                    "detail": (detail or "")[:400],
                    "severity": severity if severity in SEVERITY_ORDER else "warn",
                    "hint": (hint or "")[:300],
                    "first_seen": (prev or {}).get("first_seen") or _now(),
                    "last_seen": _now(),
                    "count": int((prev or {}).get("count", 0)) + 1,
                }
                conn.execute(
                    "INSERT INTO meta(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (_PREFIX + key, json.dumps(obj, ensure_ascii=False)))
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass  # 诊断记录绝不影响主流程


def clear_issue(key: str):
    """问题已自愈（该处调用成功），删除对应提醒。"""
    try:
        with _lock:
            conn = _conn()
            try:
                conn.execute("DELETE FROM meta WHERE key=?", (_PREFIX + key,))
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


def list_issues() -> list[dict]:
    """当前全部未消除的问题，按严重级别 + 最近发生时间排序。"""
    out = []
    try:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT key, value FROM meta WHERE key LIKE ?", (_PREFIX + "%",)).fetchall()
        finally:
            conn.close()
        for row in rows:
            obj = _decode(row["value"])
            if not obj or not obj.get("title"):
                continue
            out.append(obj)
    except Exception:
        return []
    out.sort(key=lambda x: (SEVERITY_ORDER.get(x.get("severity"), 1),
                            str(x.get("last_seen", "")), x.get("key", "")))
    return out


# ---------- 本地零成本检查项（不发网络请求） ----------

def check_embed_partial(store) -> dict | None:
    """嵌入（向量）配置填了一半：任一项填了但三项不全 → 不生效，提醒补全。

    store 需要 get_setting(key, default)（ProjectStore 自动转发全局键）。
    三项全空 = 未配置可选功能，不算问题；三项全齐交给真实调用结果说话。
    """
    try:
        url = (store.get_setting("embed_base_url", "") or "").strip()
        model = (store.get_setting("embed_model", "") or "").strip()
        key_enc = (store.get_setting("embed_api_key", "") or "").strip()
    except Exception:
        return None
    filled = [bool(url), bool(model), bool(key_enc)]
    if all(filled) or not any(filled):
        return None
    names = [n for n, ok in zip(("接口地址", "模型名", "API Key"), filled) if not ok]
    return {
        "key": "embed_partial",
        "title": "向量（嵌入）配置不完整，语义记忆检索未生效",
        "detail": "还缺：" + "、".join(names) + "（设置 → 嵌入接口）",
        "severity": "warn",
        "hint": "到「模型」页的嵌入接口设置补全后自动消除；不想用语义检索可清空全部三项。",
        "first_seen": _now(), "last_seen": _now(), "count": 1,
    }


def collect_issues(store=None) -> list[dict]:
    """提醒栏数据源：已登记的事件问题 + 本地即时检查项（按 key 去重）。"""
    issues = {i["key"]: i for i in list_issues()}
    if store is not None:
        local = check_embed_partial(store)
        if local and local["key"] not in issues:
            issues[local["key"]] = local
    return sorted(issues.values(),
                  key=lambda x: (SEVERITY_ORDER.get(x.get("severity"), 1),
                                 str(x.get("last_seen", "")), x.get("key", "")))


# ---- 供测试/工具用 ----

def _reset_all():
    """清空全部诊断记录（仅测试用）。"""
    try:
        conn = _conn()
        try:
            conn.execute("DELETE FROM meta WHERE key LIKE ?", (_PREFIX + "%",))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


record_issue.__test__ = False
