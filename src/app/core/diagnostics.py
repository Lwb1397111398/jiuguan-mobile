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
_MIGRATE_FLAG = "diagnostic_scoping_v1"
# 项目级事件提醒（由某个项目的动作触发）会带 project 标签，只在该项目打开时显示；
# embed_call / embed_partial 属全局配置健康，不带标签、所有项目都显示。
_PROJECT_KEYS = ("vector_index", "auto_archive", "lore_sync")
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
                 severity: str = "warn", hint: str = "", project: str = ""):
    """登记（或刷新）一条问题。同 key 重复发生只更新时间/次数/最新详情。

    project：项目名（store.title）。项目级事件提醒（如某项目的向量索引失败）
    必须带标签，否则会在所有项目里误显示；全局配置问题（嵌入接口等）留空。
    """
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
                    "project": (project or "").strip(),
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


def clear_issue(key: str, project: str = ""):
    """问题已自愈（该处调用成功），删除对应提醒。

    project 传项目名时只删该项目自己的记录（防止 A 项目的提醒被 B 项目的成功清掉）；
    留空则整条删除（全局键 embed_call 等不带项目，沿用原行为）。
    """
    try:
        with _lock:
            conn = _conn()
            try:
                if not (project or "").strip():
                    conn.execute("DELETE FROM meta WHERE key=?", (_PREFIX + key,))
                else:
                    row = conn.execute("SELECT value FROM meta WHERE key=?",
                                       (_PREFIX + key,)).fetchone()
                    obj = _decode(row["value"]) if row else None
                    if obj is not None and (obj.get("project") or "").strip() == project.strip():
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


# ---------- 项目过滤（项目级事件提醒只在其项目打开时显示） ----------

def _migrate_legacy_project_issues():
    """一次性迁移（带 meta 旗标，幂等）：项目级事件键的旧记录没有 project 标签，
    会在所有项目（包括新建的空项目）里误显示——正是老板遇到的
    「新项目一打开就提示记忆向量索引生成失败」。清掉这些无主旧记录，
    相关功能下次触发会带项目标签重新登记，不存在信息永久丢失。

    只在桌面启动时调用一次（main.py）：绝不挂在 collect_issues 等读取路径上——
    那会把升级后新写入的无标签记录（测试/移动端直写）一并吃掉（2026-10-08 教训）。"""
    try:
        with _lock:
            conn = _conn()
            try:
                done = conn.execute("SELECT value FROM meta WHERE key=?",
                                    (_MIGRATE_FLAG,)).fetchone()
                if done:
                    return
                for key in _PROJECT_KEYS:
                    row = conn.execute("SELECT value FROM meta WHERE key=?",
                                       (_PREFIX + key,)).fetchone()
                    if row:
                        obj = _decode(row["value"])
                        if obj is not None and not (obj.get("project") or "").strip():
                            conn.execute("DELETE FROM meta WHERE key=?", (_PREFIX + key,))
                conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (_MIGRATE_FLAG, _now()))
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass  # 迁移绝不阻塞主流程


def collect_issues(store=None) -> list[dict]:
    """提醒栏数据源：已登记的事件问题 + 本地即时检查项（按 key 去重）。纯读取，无副作用。

    项目过滤：带 project 标签的问题只在标签项目打开时显示（store 为 None 的
    首页/未打开项目状态下一律隐藏）；不带标签的全局问题照旧全项目显示。
    """
    issues = {i["key"]: i for i in list_issues()}
    cur_title = ""
    if store is not None:
        try:
            cur_title = (store.title or "").strip()
        except Exception:
            cur_title = ""
    visible = {}
    for key, item in issues.items():
        proj = (item.get("project") or "").strip()
        if proj and proj != cur_title:
            continue  # 别的项目的问题：当前项目不显示
        visible[key] = item
    if store is not None:
        local = check_embed_partial(store)
        if local and local["key"] not in visible:
            visible[local["key"]] = local
    return sorted(visible.values(),
                  key=lambda x: (SEVERITY_ORDER.get(x.get("severity"), 1),
                                 str(x.get("last_seen", "")), x.get("key", "")))


# ---- 供测试/工具用 ----

def _reset_all():
    """清空全部诊断记录（仅测试用），含迁移旗标——让每条测试从干净状态开始。"""
    try:
        conn = _conn()
        try:
            conn.execute("DELETE FROM meta WHERE key LIKE ?", (_PREFIX + "%",))
            conn.execute("DELETE FROM meta WHERE key = ?", (_MIGRATE_FLAG,))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


record_issue.__test__ = False
