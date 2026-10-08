# -*- coding: utf-8 -*-
"""GlobalStore：应用级全局配置库（模型档案 / 任务角色路由 / 偏好卡片 / 提示词预设）。

这些是用户级配置，跨项目共享同一套，存放在 %APPDATA%/酒馆小说生成器/app.db。
ProjectStore 的对应方法转发到这里（方法名不变），上层（UI/Pipeline/Assembler/PresetService）
无需感知数据来源，天然实现「统一管理」。

首次建库时：先迁移已有项目的旧配置（只执行一次），再补内置卡片与默认路由兜底——
保证用户旧项目的模型绑定/自定义卡片不丢失，未迁移到的角色才用默认值。
"""
import json
import os
import re
import shutil
import sqlite3
import threading

from . import db
from .outline_fields import effective_field_defs
from .paths import app_data_dir
from .sysprompts import (DEFAULT_ROLES, BUILTIN_CARDS,
                         PROSE_REWRITE_LEGACY_NAME, PROSE_REWRITE_LEGACY_PROMPT)

MIGRATE_MARK = "config_migrated"

# 迁移到全局的工具设置键（2026-08-08）：一个小说一个项目，破甲/生成偏好等与书无关，
# 全部全局共享；只有内容数据（总纲/分卷/大纲/细纲/正文/记忆/设定/锁定项/伏笔笔记）留项目级。
# 值为「未设置时」的内置默认，调用方传入的 default 仅在非迁移键上生效。
GLOBAL_SETTING_DEFAULTS = {
    "context_inject_k": "1",
    "context_inject_mode": "tail",       # full=完整全文 / tail=章末截取 / summary=仅摘要
    "context_inject_chars": "3000",
    "chapter_dynamics": "1",             # 本章动态开关
    "token_ratio": "0.75",               # token 估算系数（字符→token）
    "ctx_reserve_ratio": "0.75",         # 可用上下文比例
    "memory_retries": "3",
    "api_retries": "1",
    "log_retention": "500",              # 请求日志保留条数（超出自动删最旧；每条含完整 prompt+response，不限制会撑爆 project.db）
    "style_cliches": "",                 # 文风审校：陈词滥调清单
    "volume_chapter_mode": "auto",       # 分卷每卷章数：auto=AI 自定 / exact=手动指定
    "volume_chapter_count": "4",
    "volume_chapter_range": "4-6",       # 旧版本兼容读取；新 UI 不再写入
    "bound_preset_id": "",               # 当前绑定的提示词预设
    "jailbreak_completion_guard": "1",   # 收尾护栏（模型级 assistant 起手+user 收尾）：1 开 / 0 关
                                         # 酒馆成功报文实锤带这两条（2026-09-26 第八次对照）→ 默认开；
                                         # 个别渠道拒收 assistant 结尾报文时可关（Horizen 2026-09-19 实测）
    "anti_truncate_enabled": "0",        # 截断自动续写（防截断）开关：0 关 / 1 开
    "anti_truncate_rounds": "2",         # 截断自动续写最多续写轮数（1~5）
    "end_mark_enabled": "1",             # 输出完成标志协议（软截断检测）：注入【生成完毕】收尾要求，无标志=疑似未写完（0=不注入不判定）
    "embed_base_url": "",                # 嵌入（向量）接口地址：文风审校语义查重 + 记忆语义检索
    "embed_api_key": "",                 # 嵌入接口 API Key（密文存储）
    "embed_model": "",                   # 嵌入模型名
    "prose_mode": "chapter",             # 正文写法：chapter=一次性（整章一次写完，默认）/ batch=分批（接力写作）；旧值 block 已并入 chapter
    "prose_batch_size": "3",             # 分批模式每批覆盖的细纲块数（1~8，末批可少；仅 prose_mode=batch 生效）
    "auto_archive": "1",                 # 正文接受后自动归档记忆（重新接受=重归档）
    "keep_awake": "1",                   # 防休眠：0=关 / 1=应用运行期间 / 2=仅自动模式运行时
    "keep_awake_display": "0",           # 防休眠时是否同时阻止屏幕关闭（1=屏幕常亮，笔记本更耗电）
    "draft_check_stages": "",            # 草稿自检模式：逗号分隔阶段白名单（synopsis,volume,outline,detail,prose），空=全关
    "draft_check_extra": "",             # 草稿自检：通用附加检查项（多行，每行一条，追加进所有开启阶段的自检清单）
    "draft_check_extra_synopsis": "",    # 草稿自检：总纲专属附加检查项（仅该阶段生效）
    "draft_check_extra_volume": "",      # 草稿自检：分卷专属附加检查项
    "draft_check_extra_outline": "",     # 草稿自检：大纲专属附加检查项
    "draft_check_extra_detail": "",      # 草稿自检：细纲专属附加检查项
    "draft_check_extra_prose": "",       # 草稿自检：正文专属附加检查项
    "draft_check_prose_mode": "full",    # 草稿模式（键名历史遗留，实际作用于所有阶段）：full=全量草稿 / interleaved=穿插草稿（字数驱动分段，各阶段默认每段字数不同）
    "draft_check_density": "standard",   # 草稿穿插密度：dense=密集 / standard=标准 / sparse=稀疏（按系数缩放各阶段每段字数）
    "draft_checklist_synopsis": "",      # 草稿自检：总纲自定义自检清单（替换式，留空=内置默认，每行一条）
    "draft_checklist_volume": "",        # 草稿自检：分卷自定义自检清单
    "draft_checklist_outline": "",       # 草稿自检：大纲自定义自检清单
    "draft_checklist_detail": "",        # 草稿自检：细纲自定义自检清单
    "draft_checklist_prose": "",         # 草稿自检：正文自定义自检清单
    # ---------- 记忆联动（2026-09-08）----------
    "auto_lore_import": "1",             # 归档后自动把记忆新人物/新实体导入设定库（0=只提醒不导入）
    # ---------- 酒馆预设导入（2026-09-10）----------
    # 分类规则属于"导入工具配置"，与破甲同类（一个小说一个项目，工具配置跨项目共享）。
    # 旧版存在项目 project_settings 里，换项目即丢失；现走全局，所有项目同一套。
    "tavern_rules": "",                  # 分类映射规则 JSON（空 = 用内置默认）；presets.PresetService
    "tavern_rules_version": "2",         # 规则版本号（用于自动升级旧版宽松破限正则）
    # ---------- 渠道故障自动切换（2026-09-18）----------
    # 某个渠道明确报渠道层错误（余额耗尽/限流/Key 失效/服务端故障/网络不通）且重试
    # 仍失败时，按角色配置的备用渠道顺序自动换渠道重发（见 pipeline.classify_channel_failure）。
    "channel_failover_enabled": "1",     # 总开关：0=关闭自动切换（备用链配了也不用）
    "fallback_channels": "",             # 备用渠道链 JSON：{"角色": [档案 id, ...]}（空 = 不切换）
    # ---------- 协议栈增强（2026-09-23）----------
    # 「酒馆能过我们不能」对照实验的产物（见 docs/酒馆破限对照排查SOP.md §4.9）：
    # 给「总纲」任务的 user 末尾追加一整套格式/场景/叙事者/思考协议，把任务从
    # 「内容审查」轨道拉到「按格式执行」轨道。实测：我方基线族 1/30 出正文 →
    # 带协议栈 4/6（8 个单段臂全 0，只测「整套挂」有效，故不可拆段）。
    # 默认关（省 token、也不干扰正常生成）；遇模型拒答时一键打开再试。
    "protocol_stack_enabled": "0",       # 协议栈增强：0 关 / 1 开（仅作用于总纲任务）
}
GLOBAL_SETTING_KEYS = frozenset(GLOBAL_SETTING_DEFAULTS)

# 破甲相关设置键（迁移到全局 meta，不走 project_settings）
_JAILBREAK_META_KEYS = ("active_jailbreak", "jailbreak_enabled",
                        "jailbreak_strength", "jailbreak_profile_id")

# 模型档案里「破甲」字段的取值约定：
#   ''            = 跟随全局「当前生效破甲」
#   JAILBREAK_NONE = 该档案不注入破甲（如纯机械 JSON 任务用的本地小模型）
#   其他字符串     = 指定的破甲名（不存在时回退全局当前生效）
JAILBREAK_NONE = "__none__"


def _default_db_path() -> str:
    return os.path.join(app_data_dir(), "app.db")


# ---------- 渠道故障自动切换：备用渠道链（2026-09-18） ----------

CHANNEL_FAILOVER_ENABLED_KEY = "channel_failover_enabled"
FALLBACK_CHANNELS_KEY = "fallback_channels"


def parse_fallback_channels(raw) -> dict:
    """把 fallback_channels 设置值解析为 {role: [profile_id, ...]}。

    容错优先：非 JSON / 非字典 / 值非法（非正整数）一律丢弃该项——
    配置坏掉绝不能中断生成（宁可退化成「不切换」，行为与旧版一致）。
    """
    if not raw:
        return {}
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(str(raw))
        except (TypeError, ValueError):
            return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, list[int]] = {}
    for role, ids in data.items():
        if not isinstance(role, str) or not isinstance(ids, (list, tuple)):
            continue
        clean: list[int] = []
        for pid in ids:
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                continue
            if pid > 0 and pid not in clean:
                clean.append(pid)
        if clean:
            out[role] = clean
    return out


def dump_fallback_channels(mapping) -> str:
    """序列化备用渠道链；空表存空串（不占 meta 存储）。"""
    clean = parse_fallback_channels(mapping or {})
    if not clean:
        return ""
    return json.dumps(clean, ensure_ascii=False, sort_keys=True)


class GlobalStore:
    """全局配置库门面（进程内按路径单例，连接/锁复用）。"""

    _instances: dict[str, "GlobalStore"] = {}

    @classmethod
    def for_path(cls, path: str) -> "GlobalStore":
        path = os.path.abspath(path)
        if path not in cls._instances:
            cls._instances[path] = cls(path)
        return cls._instances[path]

    @classmethod
    def default(cls) -> "GlobalStore":
        return cls.for_path(_default_db_path())

    def reopen(self):
        """底层 app.db 文件被整体替换（全量恢复）后重开连接，重跑幂等自愈。

        本类是按路径单例：外部调用方持有的引用在 reopen 后继续有效。
        """
        try:
            self.conn.close()
        except sqlite3.Error:
            pass
        self.conn = db.connect_global(self.db_path)
        self._lock = threading.RLock()
        self._ensure_seed(True)
        self._migrate_task_templates()
        self._migrate_global_settings()
        self._migrate_jailbreaks()
        self._ensure_jailbreak_seed()
        self._migrate_jailbreak_match_to_profiles()
        self._ensure_roles_each_open()
        self._backup_global_db()

    def __init__(self, db_path: str | None = None, migrate_legacy: bool = True):
        self.db_path = db_path or _default_db_path()
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.conn = db.connect_global(self.db_path)
        self._lock = threading.RLock()  # UI 线程 + AI 工作线程共用
        self._ensure_seed(migrate_legacy)
        self._migrate_task_templates()
        self._migrate_global_settings()
        self._migrate_jailbreaks()
        self._ensure_jailbreak_seed()
        self._migrate_jailbreak_match_to_profiles()
        self._ensure_roles_each_open()
        self._backup_global_db()

    def _backup_global_db(self):
        """每日首次打开时备份全局配置库（模型/路由/偏好/预设），保留最近 7 份。

        防止全局配置意外丢失/损坏（如 app.db 被误删或写坏）后无法恢复。
        """
        try:
            backup_dir = os.path.join(os.path.dirname(self.db_path), "backups")
            os.makedirs(backup_dir, exist_ok=True)
            today = db.now()[:10].replace("-", "")
            dest = os.path.join(backup_dir, f"app_{today}.db")
            if os.path.isfile(dest):
                return  # 今天已备份
            with self._lock:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            shutil.copy2(self.db_path, dest)
            files = sorted(f for f in os.listdir(backup_dir)
                           if f.startswith("app_") and f.endswith(".db"))
            for f in files[:-7]:
                try:
                    os.remove(os.path.join(backup_dir, f))
                except OSError:
                    pass
        except OSError:
            pass

    # ---------- 基础 ----------

    def _rows(self, sql, params=()) -> list[dict]:
        with self._lock:
            cur = self.conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def _one(self, sql, params=()) -> dict | None:
        with self._lock:
            cur = self.conn.execute(sql, params)
            r = cur.fetchone()
            return dict(r) if r else None

    def _exec(self, sql, params=()):
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def get_meta(self, key: str, default="") -> str:
        r = self._one("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else default

    def set_meta(self, key: str, value: str):
        self._exec("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, value))

    # ---------- 大纲字段默认配置 ----------

    def get_outline_fields_default(self) -> list[dict]:
        """读取全局大纲字段默认配置；旧配置缺失时使用内置默认。"""
        raw = self.get_meta("outline_fields_default", "")
        if raw:
            try:
                return effective_field_defs(json.loads(raw))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        return effective_field_defs()

    def set_outline_fields_default(self, fields) -> list[dict]:
        normalized = effective_field_defs(fields)
        self.set_meta("outline_fields_default", json.dumps(normalized, ensure_ascii=False))
        return normalized

    # ---------- 任务模板（全局共享，2026-08-08 改） ----------

    def get_task_template(self, key: str) -> str:
        """全局任务模板覆盖（task_template_<key>），空 = 用内置默认。"""
        return self.get_meta(f"task_template_{key}", "")

    def set_task_template(self, key: str, text: str) -> None:
        """保存全局任务模板覆盖；空文本 = 恢复内置默认。"""
        self.set_meta(f"task_template_{key}", text)

    def _migrate_task_templates(self):
        """一次性（2026-08-08）：把旧项目库里的任务模板覆盖合并进全局库。

        任务模板改为全局共享后，旧版本存在各项目 project_settings 的
        task_template_* 覆盖需要搬进全局库，否则换项目就"丢模板"。
        按 key 去重：全局已有值则保留全局（新保存优先），项目里第一次见到的才搬。
        """
        if self.get_meta("task_template_migrated"):
            return
        cfg_path = os.path.join(app_data_dir(), "config.json")
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
        for item in cfg.get("recent_projects", []) or []:
            proj_db = os.path.join(str(item.get("path", "")), "project.db")
            if not os.path.isfile(proj_db):
                continue
            try:
                conn = sqlite3.connect(proj_db)
                try:
                    rows = conn.execute(
                        "SELECT key,value FROM project_settings WHERE key LIKE 'task_template_%'"
                    ).fetchall()
                finally:
                    conn.close()
            except sqlite3.Error:
                continue
            for key, value in rows:
                if value and not self.get_meta(key, ""):
                    self.set_meta(key, value)
        self.set_meta("task_template_migrated", "1")

    # ---------- 全局工具设置（破甲/生成偏好等，跨项目共享，2026-08-08） ----------

    def get_global_setting(self, key: str, default=""):
        """读取全局工具设置；未设置时回退内置默认值（迁移键），否则回退调用方默认。"""
        v = self.get_meta(f"global_setting_{key}", "")
        if v:
            return v
        if key in GLOBAL_SETTING_DEFAULTS:
            return GLOBAL_SETTING_DEFAULTS[key]
        return default

    def set_global_setting(self, key: str, value):
        """保存全局工具设置；None/空串按空值存储（调用方 default 兜底）。"""
        self.set_meta(f"global_setting_{key}", "" if value is None else str(value))

    def _migrate_global_settings(self):
        """一次性（2026-08-08）：把旧项目库 project_settings 里的工具设置键搬进全局库。

        破甲/上下文注入/token 估算/重试/文风清单/分卷章数/绑定预设与单本小说内容无关，
        改为全局共享后，旧项目里各自的配置按 key 去重合并：全局已有则保留（新保存优先），
        第一次见到的才搬。幂等标记 global_settings_migrated 控制，不重复执行。
        """
        if self.get_meta("global_settings_migrated"):
            return
        cfg_path = os.path.join(app_data_dir(), "config.json")
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
        keys = sorted(GLOBAL_SETTING_KEYS)
        placeholders = ",".join("?" * len(keys))
        sql = f"SELECT key,value FROM project_settings WHERE key IN ({placeholders})"
        for item in cfg.get("recent_projects", []) or []:
            proj_db = os.path.join(str(item.get("path", "")), "project.db")
            if not os.path.isfile(proj_db):
                continue
            try:
                conn = sqlite3.connect(proj_db)
                try:
                    rows = conn.execute(sql, keys).fetchall()
                finally:
                    conn.close()
            except sqlite3.Error:
                continue
            for key, value in rows:
                if value and not self.get_meta(f"global_setting_{key}", ""):
                    self.set_meta(f"global_setting_{key}", value)
        self.set_meta("global_settings_migrated", "1")

    # ---------- 破甲（全局共享，2026-08-08 改：一个小说一个项目，破甲是创作工具配置） ----------

    def _global_jailbreaks_dir(self) -> str:
        """全局破甲库目录：与 app.db 同级的 jailbreaks/（%APPDATA%/酒馆小说生成器/jailbreaks）。"""
        d = os.path.join(os.path.dirname(self.db_path), "jailbreaks")
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
        return d

    def _legacy_jailbreak_path(self) -> str:
        """旧版单文件破甲（兼容「默认」）：app.db 同级 jailbreak_prompt.md。"""
        return os.path.join(os.path.dirname(self.db_path), "jailbreak_prompt.md")

    def list_jailbreak_profiles(self) -> list[dict]:
        """全局破甲库：所有已保存破甲（含内容与生效标记）。兼容旧版单文件为「默认」。"""
        d = self._global_jailbreaks_dir()
        items: list[dict] = []
        seen: set[str] = set()
        try:
            names = os.listdir(d)
        except OSError:
            names = []
        for fn in sorted(names):
            if not fn.endswith(".md"):
                continue
            name = fn[:-3]
            seen.add(name)
            try:
                with open(os.path.join(d, fn), "r", encoding="utf-8") as f:
                    content = f.read().strip()
            except OSError:
                content = ""
            items.append({"name": name, "content": content})
        legacy = self._legacy_jailbreak_path()
        if os.path.isfile(legacy) and "默认" not in seen:
            try:
                with open(legacy, "r", encoding="utf-8") as f:
                    legacy_text = f.read().strip()
            except OSError:
                legacy_text = ""
            items.insert(0, {"name": "默认", "content": legacy_text})
        active = self.get_active_jailbreak_name()
        for it in items:
            it["active"] = (it["name"] == active)
        return items

    def get_active_jailbreak_name(self) -> str:
        active = self.get_meta("active_jailbreak", "")
        if active and active != "默认" and os.path.isfile(
                os.path.join(self._global_jailbreaks_dir(), f"{active}.md")):
            return active
        return "默认" if os.path.isfile(self._legacy_jailbreak_path()) else ""

    def set_active_jailbreak(self, name: str):
        """切换当前生效破甲。name 为空 → 回退旧版单文件。"""
        if not name:
            self.set_meta("active_jailbreak", "")
            return
        if name == "默认" or os.path.isfile(os.path.join(self._global_jailbreaks_dir(), f"{name}.md")):
            self.set_meta("active_jailbreak", name)

    def save_jailbreak_profile(self, name: str, text: str) -> str:
        """保存一个新破甲到全局库（同名覆盖）。「默认」绑定旧版单文件以保持兼容。返回规范化名字。"""
        name = (name or "").strip() or "破甲"
        if name == "默认":
            path = self._legacy_jailbreak_path()
        else:
            safe = re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "破甲"
            path = os.path.join(self._global_jailbreaks_dir(), f"{safe}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text or "")
        return "默认" if name == "默认" else re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "破甲"

    def delete_jailbreak_profile(self, name: str) -> bool:
        """删除全局库中一个破甲；若正生效则清除活动标记。返回是否删除成功。"""
        if name == "默认":
            path = self._legacy_jailbreak_path()
        else:
            path = os.path.join(self._global_jailbreaks_dir(), f"{name}.md")
        try:
            os.remove(path)
        except OSError:
            return False
        # 直接比对该键的原始值：文件已删后 get_active_jailbreak_name() 会回退到
        # 「默认」/""，与要删除的名字不再相等，导致活动标记残留
        if self.get_meta("active_jailbreak", "") == name:
            self.set_active_jailbreak("")
        return True

    def get_jailbreak_prompt(self) -> str:
        """当前生效破甲的文本（有活动破甲读库；否则读旧版单文件）。"""
        active = self.get_active_jailbreak_name()
        if active and active != "默认":
            path = os.path.join(self._global_jailbreaks_dir(), f"{active}.md")
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read().strip()
            except OSError:
                pass
        try:
            with open(self._legacy_jailbreak_path(), "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def set_jailbreak_prompt(self, text: str):
        """写入当前生效破甲（有活动破甲写库；否则写旧版单文件）。"""
        active = self.get_active_jailbreak_name()
        if active and active != "默认":
            path = os.path.join(self._global_jailbreaks_dir(), f"{active}.md")
        else:
            path = self._legacy_jailbreak_path()
        with open(path, "w", encoding="utf-8") as f:
            f.write(text or "")

    def get_jailbreak_profile_id(self) -> int:
        """（兼容旧版）破限专用 API 档案 id；未设置返回 0。已废弃，UI 不再使用。"""
        try:
            return int(self.get_meta("jailbreak_profile_id", "0"))
        except (TypeError, ValueError):
            return 0

    def set_jailbreak_profile_id(self, pid: int | None):
        try:
            pid = int(pid) if pid is not None else None
        except (TypeError, ValueError):
            pid = None
        self.set_meta("jailbreak_profile_id", "" if pid is None else str(pid))

    def get_jailbreak_enabled(self) -> bool:
        """破限开关（按角色路由注入：创作系 full/审稿系 light/memory off，默认开）。"""
        return str(self.get_meta("jailbreak_enabled", "1")).strip() == "1"

    def set_jailbreak_enabled(self, enabled: bool):
        self.set_meta("jailbreak_enabled", "1" if enabled else "0")

    def get_jailbreak_strength(self) -> str:
        """破甲强度档位：standard=锚定+推杆全套（默认）/ light=只推杆不锚定。"""
        v = str(self.get_meta("jailbreak_strength", "standard")).strip().lower()
        return v if v in ("standard", "light") else "standard"

    def set_jailbreak_strength(self, strength: str):
        strength = str(strength or "").strip().lower()
        self.set_meta("jailbreak_strength",
                      strength if strength in ("standard", "light") else "standard")

    def get_jailbreak_completion_guard(self) -> bool:
        """收尾护栏开关（模型级 assistant 起手 + user 收尾，见 MODEL_COMPLETION_GUARDS）。

        默认开：酒馆成功报文实锤带这两条形态锁（2026-09-26 第八次对照）。
        个别渠道拒收 assistant 结尾报文（HTTP 400）时可关。
        """
        return str(self.get_meta("jailbreak_completion_guard", "1")).strip() == "1"

    def set_jailbreak_completion_guard(self, enabled: bool):
        self.set_meta("jailbreak_completion_guard", "1" if enabled else "0")

    # ---------- 破甲·由模型档案直接选择（2026-09-10 取代旧的「按模型自动匹配」） ----------
    # 旧机制：每套破甲标注「适配模型关键词」，请求时按模型名反查 → 配置分散在两处，
    # 加个模型就得回来补关键词，实测麻烦且容易配错。
    # 现机制：破甲写在模型档案里（model_profiles.jailbreak），打开档案就能看到「这个模型用哪套」。

    def get_profile_jailbreak(self, profile: dict | None) -> str:
        """档案选择的破甲名：''=跟随全局当前破甲；JAILBREAK_NONE=该档案不注入破甲。"""
        return str((profile or {}).get("jailbreak") or "").strip()

    def resolve_jailbreak_text_for_profile(self, profile: dict | None) -> str:
        """按档案选择解析本次要注入的破甲文本。

        - 档案指定了破甲名且该破甲存在 → 用它的文本；
        - 档案标记「不注入破甲」→ 空串（本次请求不带破甲）；
        - 档案未选（''）或指定的破甲已被删除 → 回退全局「当前生效破甲」。
        """
        name = self.get_profile_jailbreak(profile)
        if name == JAILBREAK_NONE:
            return ""
        if name:
            text = self.get_jailbreak_prompt_by_name(name)
            if text:
                return text
        return self.get_jailbreak_prompt()

    def rename_profile_jailbreak(self, old_name: str, new_name: str) -> int:
        """破甲改名时，把指定了旧名的模型档案改指向新名（否则会静默回退跟随全局）。

        返回被更新的档案数。
        """
        old_name = (old_name or "").strip()
        new_name = (new_name or "").strip()
        if not old_name or not new_name or old_name == new_name:
            return 0
        cur = self._exec("UPDATE model_profiles SET jailbreak=? WHERE jailbreak=?",
                         (new_name, old_name))
        return cur.rowcount or 0

    def _migrate_jailbreak_match_to_profiles(self):
        """一次性（2026-09-10）：旧「破甲 → 适配模型关键词」映射 → 落到各模型档案上。

        旧机制按请求模型名匹配破甲，新机制由档案直接选。迁移时对每个档案沿用旧规则
        算出它原本会用到的破甲，写进 model_profiles.jailbreak（只填还没选过的档案），
        保证老板已有的配置不用重配。原 meta 保留不删，便于回溯。
        """
        if self.get_meta("jailbreak_match_migrated"):
            return
        raw = self.get_meta("jailbreak_matches", "")
        try:
            matches = json.loads(raw) if raw else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            matches = {}
        if isinstance(matches, dict) and matches:
            sep = re.compile(r"[，,;；\s、]+")
            kws_by_name: dict[str, list[str]] = {}
            for name, kws in matches.items():
                parts = [k.strip().lower() for k in sep.split(str(kws or ""))]
                kws_by_name[str(name)] = [k for k in parts if len(k) > 1]  # 单字符易误匹配
            existing = {p["name"] for p in self.list_jailbreak_profiles()}
            for p in self._rows("SELECT id, model, jailbreak FROM model_profiles"):
                if str(p.get("jailbreak") or "").strip():
                    continue  # 用户已手动选过，不动
                model = str(p.get("model") or "").strip().lower()
                best, best_score = "", 0
                for name, kws in kws_by_name.items():
                    if name not in existing:
                        continue
                    score = max((len(k) for k in kws if k in model), default=0)
                    if score > best_score:
                        best, best_score = name, score
                if best:
                    self._exec("UPDATE model_profiles SET jailbreak=? WHERE id=?", (best, p["id"]))
        self.set_meta("jailbreak_match_migrated", "1")

    def get_jailbreak_prompt_by_name(self, name: str) -> str:
        """读指定破甲的文本（不存在/空名返回空串）。"""
        name = (name or "").strip()
        if not name:
            return ""
        if name == "默认":
            path = self._legacy_jailbreak_path()
        else:
            safe = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
            path = os.path.join(self._global_jailbreaks_dir(), f"{safe}.md")
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def _migrate_jailbreaks(self):
        """一次性（2026-08-08）：把旧项目文件夹里的破甲库搬进全局目录。

        一个小说一个项目，破甲是创作工具配置。旧项目里的 jailbreaks/*.md 与
        jailbreak_prompt.md 需要搬进全局数据目录（同名跳过，recent 最新在前优先）。
        设置键 active_jailbreak / jailbreak_enabled / jailbreak_strength /
        jailbreak_profile_id 一并搬进全局 meta（空值不搬）。源文件保留在项目文件夹
        （只复制不删，保守；新代码读写一律走全局库）。
        """
        if self.get_meta("jailbreak_migrated"):
            return
        cfg_path = os.path.join(app_data_dir(), "config.json")
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
        target = self._global_jailbreaks_dir()
        for item in cfg.get("recent_projects", []) or []:
            folder = str(item.get("path", ""))
            if not folder:
                continue
            # 1) jailbreaks/*.md → 全局目录（同名跳过）
            src_dir = os.path.join(folder, "jailbreaks")
            if os.path.isdir(src_dir):
                try:
                    for fn in os.listdir(src_dir):
                        if not fn.endswith(".md"):
                            continue
                        src = os.path.join(src_dir, fn)
                        dst = os.path.join(target, fn)
                        if os.path.isfile(src) and not os.path.isfile(dst):
                            shutil.copy2(src, dst)
                except OSError:
                    pass
            # 2) 旧版单文件 jailbreak_prompt.md → 「默认」破甲（全局同名不存在才搬）
            legacy = os.path.join(folder, "jailbreak_prompt.md")
            if os.path.isfile(legacy):
                dst = os.path.join(target, "默认.md")
                if not os.path.isfile(dst):
                    try:
                        shutil.copy2(legacy, dst)
                    except OSError:
                        pass
            # 3) 破甲设置键 → 全局 meta（空值不搬；active 指向的破甲已随 1/2 搬入）
            proj_db = os.path.join(folder, "project.db")
            if not os.path.isfile(proj_db):
                continue
            try:
                conn = sqlite3.connect(proj_db)
                try:
                    rows = conn.execute(
                        "SELECT key,value FROM project_settings WHERE key IN"
                        " ('active_jailbreak','jailbreak_enabled','jailbreak_strength','jailbreak_profile_id')"
                    ).fetchall()
                finally:
                    conn.close()
            except sqlite3.Error:
                continue
            for key, value in rows:
                if value and not self.get_meta(key, ""):
                    self.set_meta(key, value)
        self.set_meta("jailbreak_migrated", "1")

    def _ensure_jailbreak_seed(self):
        """全局破甲库兜底 + 内置破甲版本化补种。

        两件事：
        1) 补种：BUILTIN_JAILBREAKS 里没种过的（按名字记在 meta.seeded_jailbreaks）
           写入破甲库。老用户升级后也能拿到新增的内置破甲；用户删掉后不会再被种回来
           （名字已在已种清单里），与角色/卡片的补种行为一致。
        2) 兜底：无生效项时把第一套内置破甲设为当前生效（新装/用户删光后）。
        """
        try:
            from .jailbreak_builtin import BUILTIN_JAILBREAK_NAME, BUILTIN_JAILBREAKS
        except Exception:
            return
        seeded = self._load_seeded("seeded_jailbreaks")
        if seeded is None:
            # 首次建立清单：老库里已存在的内置破甲文件视为「已种」，不覆盖用户改过的内容
            seeded = [n for n, _ in BUILTIN_JAILBREAKS
                      if os.path.isfile(os.path.join(self._global_jailbreaks_dir(), f"{n}.md"))]
        changed = False
        for name, text in BUILTIN_JAILBREAKS:
            if name in seeded:
                continue
            path = os.path.join(self._global_jailbreaks_dir(), f"{name}.md")
            try:
                if not os.path.isfile(path):
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(text)
                seeded.append(name)
                changed = True
            except OSError:
                pass
        if changed:
            self._save_seeded("seeded_jailbreaks", seeded)
        if not self.get_meta("active_jailbreak", ""):
            path = os.path.join(self._global_jailbreaks_dir(), f"{BUILTIN_JAILBREAK_NAME}.md")
            if os.path.isfile(path):
                self.set_meta("active_jailbreak", BUILTIN_JAILBREAK_NAME)

    # ---------- 种子 + 迁移 ----------

    def _ensure_seed(self, migrate_legacy: bool):
        if self.get_meta(MIGRATE_MARK):
            return
        # 1) 先迁移旧项目配置（用户旧配置优先）
        if migrate_legacy:
            self._migrate_legacy_projects()
        # 2) 内置偏好卡片（title 不存在才补，避免覆盖迁移/用户自定义）
        for i, card_def in enumerate(BUILTIN_CARDS):
            title, content, scope, stage, prio = card_def[:5]
            stages = card_def[5] if len(card_def) > 5 else ""
            if not self._one("SELECT id FROM preference_cards WHERE title=?", (title,)):
                self._exec(
                    "INSERT INTO preference_cards(title,content,enabled,scope,stage,stages,priority,is_builtin,sort_order,created_at)"
                    " VALUES(?,?,?,?,?,?,?,1,?,?)",
                    (title, content, 1 if scope != "manual" else 0, scope, stage, stages, prio, i, db.now()))
        # 3) 默认任务路由（role 唯一，已有则跳过）
        for role, (name, temp, prompt) in DEFAULT_ROLES.items():
            self._exec("INSERT OR IGNORE INTO task_routes(role,name,profile_id,temperature,system_prompt,created_at)"
                       " VALUES(?,?,NULL,?,?,?)", (role, name, temp, prompt, db.now()))
        self.set_meta(MIGRATE_MARK, "1")

    def _ensure_roles_each_open(self) -> None:
        """版本化补种（用户数据优先，2026-08-08 修订）。

        历史 bug：每次打开都补种「库里不存在」的内置卡/角色，导致用户删除的
        内置偏好卡复活、删除的角色路由被重建。现改为：
        - meta 记录已种过的集合（seeded_roles / seeded_cards，JSON 列表）；
        - 只补「相对上次记录新增」的默认角色/内置卡（老库升级也能拿到新内容）；
        - 已记录过的内容无论是否仍在库中，一律不再补——用户删除的永远不回来。
        - 首次建立记录时把当前版本全部标记为已种（缺失的一律不补，尊重用户删除）。
        """
        # 废弃角色清理（幂等）：volume_detail（卷纲已舍弃）、brainstorm（与总纲重复，已并入
        # synopsis）、rewrite（2026-10-08 改写二合一：老「改写润色」角色删除，其唯一消费方
        # checker 审校修复链路并入 prose_rewrite，后者显示名改为「改写」）
        for obsolete in ("volume_detail", "brainstorm", "rewrite"):
            self._exec("DELETE FROM task_routes WHERE role=?", (obsolete,))

        # ---- 默认角色：版本化补种 ----
        seeded = self._load_seeded("seeded_roles")
        if seeded is None:
            self._save_seeded("seeded_roles", list(DEFAULT_ROLES.keys()))
        else:
            changed = False
            for role, (name, temp, prompt) in DEFAULT_ROLES.items():
                if role not in seeded:
                    if not self._one("SELECT id FROM task_routes WHERE role=?", (role,)):
                        self._exec(
                            "INSERT INTO task_routes(role,name,profile_id,temperature,system_prompt,created_at)"
                            " VALUES(?,?,NULL,?,?,?)", (role, name, temp, prompt, db.now()))
                    seeded.append(role)
                    changed = True
            if changed:
                self._save_seeded("seeded_roles", seeded)

        # ---- 内置偏好卡：版本化补种 ----
        seeded_cards = self._load_seeded("seeded_cards")
        if seeded_cards is None:
            self._save_seeded("seeded_cards", [c[0] for c in BUILTIN_CARDS])
        else:
            changed = False
            for i, card_def in enumerate(BUILTIN_CARDS):
                title, content, scope, stage, prio = card_def[:5]
                stages = card_def[5] if len(card_def) > 5 else ""
                if title not in seeded_cards:
                    if not self._one("SELECT id FROM preference_cards WHERE title=?", (title,)):
                        self._exec(
                            "INSERT INTO preference_cards(title,content,enabled,scope,stage,stages,priority,is_builtin,sort_order,created_at)"
                            " VALUES(?,?,?,?,?,?,?,1,?,?)",
                            (title, content, 1 if scope != "manual" else 0, scope, stage, stages, prio,
                             (self._one("SELECT COALESCE(MAX(sort_order),0)+1 AS s FROM preference_cards") or {"s": 1})["s"],
                             db.now()))
                    seeded_cards.append(title)
                    changed = True
            if changed:
                self._save_seeded("seeded_cards", seeded_cards)

        # 文风改写档案继承（一次性，2026-10-06）：prose_rewrite 在老库里多半
        # 已被补种过但档案是空的——开箱即用要求它默认跟随「正文」的绑定。
        # 只有正文已绑档案时才继承并落标记；正文未绑（新装分阶段配置）不落
        # 标记，等正文绑上后下次启动仍可继承。标记落定后用户改绑/解绑永不干预。
        if not self.get_meta("prose_rewrite_profile_inherited", ""):
            ref = self._one("SELECT profile_id FROM task_routes"
                            " WHERE role='prose' AND profile_id IS NOT NULL")
            if ref is not None:
                self._exec("UPDATE task_routes SET profile_id=?"
                           " WHERE role='prose_rewrite'"
                           " AND (profile_id IS NULL OR profile_id=0)",
                           (ref["profile_id"],))
                self.set_meta("prose_rewrite_profile_inherited", "1")

        self._patch_stale_prior_sentence()
        self._upgrade_prose_rewrite_identity()

    def _upgrade_prose_rewrite_identity(self) -> None:
        """prose_rewrite 存量升级（幂等，2026-10-08 改写二合一）。

        老库里这行路由是补种时写死的旧默认（显示名「文风改写」、提示词偏整章差量
        协议）。现在它是唯一改写角色（兼 checker 审校修复），把仍是旧默认值的行
        升级成新默认；用户自己改过名/提示词的不动——与「用户数据优先」同纪律。
        """
        cur = DEFAULT_ROLES.get("prose_rewrite")
        if not cur:
            return
        new_name, _temp, new_prompt = cur
        row = self._one("SELECT name, system_prompt FROM task_routes WHERE role='prose_rewrite'")
        if not row:
            return
        if (row.get("name") or "") == PROSE_REWRITE_LEGACY_NAME:
            self._exec("UPDATE task_routes SET name=? WHERE role='prose_rewrite'", (new_name,))
        if (row.get("system_prompt") or "") == PROSE_REWRITE_LEGACY_PROMPT:
            self._exec("UPDATE task_routes SET system_prompt=? WHERE role='prose_rewrite'",
                       (new_prompt,))

    def _patch_stale_prior_sentence(self) -> None:
        """存量库修补（幂等，2026-09-05）：正文已移除「前文注入」板块，
        种子机制不会更新已写入的提示词，这里把残留的旧句子从
        prose 系统提示词与「正文要求」内置卡里摘除（只动这一句，用户其余自定义保留）。"""
        for table, key_col, key_val, col in (
                ("task_routes", "role", "prose", "system_prompt"),
                ("preference_cards", "title", "正文要求", "content")):
            row = self._one(f"SELECT {col} AS v FROM {table} WHERE {key_col}=?", (key_val,))
            text = (row or {}).get("v") or ""
            if "前文注入的内容" not in text:
                continue
            fixed = (text
                     .replace("；前文注入的内容不可前后矛盾。", "。")
                     .replace("前文注入的内容不可前后矛盾。", "")
                     .replace("；前文注入的内容不得前后矛盾。", "。")
                     .replace("前文注入的内容不得前后矛盾。", ""))
            self._exec(f"UPDATE {table} SET {col}=? WHERE {key_col}=?", (fixed, key_val))

    def _load_seeded(self, key: str) -> list | None:
        """读取已种集合：meta 无记录返回 None（表示首次建立），否则返回列表。"""
        raw = self.get_meta(key, "")
        if not raw:
            return None
        try:
            value = json.loads(raw)
            return value if isinstance(value, list) else None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def _save_seeded(self, key: str, value: list):
        self.set_meta(key, json.dumps(value, ensure_ascii=False))

    def remigrate(self) -> None:
        """手动重跑旧项目配置迁移（幂等：按 name/role/title 去重，不会重复导入）。"""
        self._migrate_legacy_projects()

    def _migrate_legacy_projects(self):
        """扫描最近项目，把旧 project.db 里的 4 张配置表去重合并进全局库。

        首次建库自动跑一次（MIGRATE_MARK 控制）；也可用 remigrate() 手动重跑。
        recent 最新在前，优先保留新项目配置。
        """
        cfg_path = os.path.join(app_data_dir(), "config.json")
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            return
        for item in cfg.get("recent_projects", []) or []:
            proj_db = os.path.join(str(item.get("path", "")), "project.db")
            if os.path.isfile(proj_db):
                try:
                    self._migrate_from_project(proj_db)
                except sqlite3.Error:
                    continue

    def _migrate_from_project(self, proj_db: str):
        """从单个旧项目库读配置表，去重合并（按 name/role/title 唯一键）。

        注意：旧库里 task_routes.profile_id 指向旧库的档案 id，迁移后必须按
        「档案名」重映射为全局库的新 id，否则绑定会错乱。
        """
        conn = sqlite3.connect(proj_db)
        conn.row_factory = sqlite3.Row
        try:
            # 模型档案：按 name 去重；记录 旧id → 全局id 映射供路由重映射
            id_map: dict[int, int] = {}
            for r in conn.execute("SELECT * FROM model_profiles"):
                existed = self._one("SELECT id FROM model_profiles WHERE name=?", (r["name"],))
                if existed:
                    id_map[r["id"]] = existed["id"]
                else:
                    old_columns = set(r.keys())
                    cur = self._exec(
                        "INSERT INTO model_profiles(name,base_url,api_key,model,context_length,input_price,output_price,"
                        " currency,max_tokens,timeout,temperature,reasoning_mode,max_output_mode,jailbreak,created_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (r["name"], r["base_url"], r["api_key"], r["model"], int(r["context_length"]),
                         float(r["input_price"]), float(r["output_price"]), r["currency"],
                         int(r["max_tokens"]), int(r["timeout"]), float(r["temperature"]),
                         r["reasoning_mode"] if "reasoning_mode" in old_columns else "max",
                         r["max_output_mode"] if "max_output_mode" in old_columns else "auto",
                         r["jailbreak"] if "jailbreak" in old_columns else "",
                         r["created_at"]))
                    id_map[r["id"]] = cur.lastrowid
            # 任务角色路由：按 role 去重；profile_id 按档案名重映射
            for r in conn.execute("SELECT * FROM task_routes"):
                if not self._one("SELECT id FROM task_routes WHERE role=?", (r["role"],)):
                    new_pid = id_map.get(r["profile_id"]) if r["profile_id"] else None
                    self._exec(
                        "INSERT INTO task_routes(role,name,profile_id,temperature,system_prompt,created_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (r["role"], r["name"], new_pid, float(r["temperature"]),
                         r["system_prompt"], r["created_at"]))
                elif r["profile_id"]:
                    # 路由已存在（内置默认）但旧项目有绑定 → 把绑定带过来（不覆盖温度/提示词）
                    new_pid = id_map.get(r["profile_id"])
                    if new_pid:
                        self._exec("UPDATE task_routes SET profile_id=? WHERE role=? AND profile_id IS NULL",
                                   (new_pid, r["role"]))
            # 偏好卡片：按 title 去重
            for r in conn.execute("SELECT * FROM preference_cards"):
                if not self._one("SELECT id FROM preference_cards WHERE title=?", (r["title"],)):
                    self._exec(
                        "INSERT INTO preference_cards(title,content,enabled,scope,stage,priority,note,is_builtin,sort_order,created_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (r["title"], r["content"], r["enabled"], r["scope"], r["stage"],
                         r["priority"], r["note"], r["is_builtin"], r["sort_order"], r["created_at"]))
            # 提示词预设：按 name 去重
            for r in conn.execute("SELECT * FROM presets"):
                if not self._one("SELECT id FROM presets WHERE name=?", (r["name"],)):
                    self._exec("INSERT INTO presets(name,data,is_builtin,created_at) VALUES(?,?,?,?)",
                               (r["name"], r["data"], r["is_builtin"], r["created_at"]))
        finally:
            conn.close()

    # ---------- 偏好卡片 ----------

    def list_cards(self, scope: str | None = None, stage: str | None = None) -> list[dict]:
        sql = "SELECT * FROM preference_cards WHERE 1=1"
        params = []
        if scope:
            sql += " AND scope=?"
            params.append(scope)
        if stage:
            sql += " AND stage=?"
            params.append(stage)
        sql += " ORDER BY sort_order, id"
        return self._rows(sql, params)

    def active_cards(self, scope: str | None = None, stage: str | None = None) -> list[dict]:
        """启用且作用域匹配的卡片，按优先级降序（同优先级按库内顺序稳定排）。

        priority 只影响注入时的先后（高者在前）；列表展示顺序仍走 list_cards
        的 sort_order，两者互不干扰。
        """
        rows = self.list_cards(scope, stage)
        rows = [r for r in rows if r["enabled"]]
        return sorted(rows, key=lambda r: (-int(r.get("priority") or 0),
                                           int(r.get("sort_order") or 0),
                                           int(r.get("id") or 0)))

    def add_card(self, title: str, content: str, scope: str = "global", stage: str = "",
                 priority: int = 5, enabled: bool = True, note: str = "", stages: str = "",
                 pinned: bool = False) -> dict:
        maxsort = self._one("SELECT COALESCE(MAX(sort_order),0)+1 AS s FROM preference_cards")
        cur = self._exec(
            "INSERT INTO preference_cards(title,content,enabled,scope,stage,stages,priority,pinned,note,is_builtin,sort_order,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,0,?,?)",
            (title, content, 1 if enabled else 0, scope, stage, stages, priority,
             1 if pinned else 0, note,
             int(maxsort["s"]) if maxsort else 1, db.now()))
        return self._one("SELECT * FROM preference_cards WHERE id=?", (cur.lastrowid,))

    def update_card(self, card_id: int, patch: dict):
        sets, params = [], []
        for key in ("title", "content", "note"):
            if key in patch:
                sets.append(f"{key}=?")
                params.append(patch[key])
        if "enabled" in patch:
            sets.append("enabled=?")
            params.append(1 if patch["enabled"] else 0)
        if "pinned" in patch:
            sets.append("pinned=?")
            params.append(1 if patch["pinned"] else 0)
        if "scope" in patch:
            sets.append("scope=?")
            params.append(patch["scope"])
        if "stage" in patch:
            sets.append("stage=?")
            params.append(patch["stage"])
        if "stages" in patch:
            sets.append("stages=?")
            params.append(patch["stages"])
        if "priority" in patch:
            sets.append("priority=?")
            params.append(int(patch["priority"]))
        if sets:
            params.append(card_id)
            self._exec(f"UPDATE preference_cards SET {', '.join(sets)} WHERE id=?", params)

    def delete_card(self, card_id: int):
        self._exec("DELETE FROM preference_cards WHERE id=?", (card_id,))

    # ---------- 预设 ----------

    def list_presets(self) -> list[dict]:
        return self._rows("SELECT * FROM presets ORDER BY name")

    def get_preset(self, preset_id: int) -> dict | None:
        return self._one("SELECT * FROM presets WHERE id=?", (preset_id,))

    def save_preset(self, name: str, data: dict, preset_id: int | None = None) -> int:
        payload = json.dumps(data, ensure_ascii=False)
        if preset_id:
            self._exec("UPDATE presets SET name=?, data=?, is_builtin=0 WHERE id=?", (name, payload, preset_id))
            return preset_id
        cur = self._exec("INSERT INTO presets(name,data,is_builtin,created_at) VALUES(?,?,0,?)",
                         (name, payload, db.now()))
        return cur.lastrowid

    def delete_preset(self, preset_id: int):
        self._exec("DELETE FROM presets WHERE id=?", (preset_id,))

    # ---------- 模型档案 ----------

    def list_profiles(self) -> list[dict]:
        return self._rows("SELECT * FROM model_profiles ORDER BY id")

    def get_profile(self, profile_id: int) -> dict | None:
        return self._one("SELECT * FROM model_profiles WHERE id=?", (profile_id,))

    def save_profile(self, data: dict, profile_id: int | None = None) -> int:
        # 每分钟请求上限（0=不限）：脏值一律折算为 0（不限），不让配置错误中断生成
        try:
            rpm = max(0, int(data.get("rpm") or 0))
        except (TypeError, ValueError):
            rpm = 0
        if profile_id:
            self._exec(
                "UPDATE model_profiles SET name=?,base_url=?,api_key=?,model=?,context_length=?,"
                " input_price=?,output_price=?,currency=?,max_tokens=?,timeout=?,temperature=?,"
                " reasoning_mode=?,max_output_mode=?,jailbreak=?,rpm=? WHERE id=?",
                (data.get("name", ""), data.get("base_url", ""), data.get("api_key", ""),
                 data.get("model", ""), int(data.get("context_length", db.DEFAULT_CONTEXT_LENGTH)),
                 float(data.get("input_price", 0)), float(data.get("output_price", 0)),
                 data.get("currency", "元"), int(data.get("max_tokens", 4096)),
                 int(data.get("timeout", 120)), float(data.get("temperature", 0.8)),
                 data.get("reasoning_mode", "max"), data.get("max_output_mode", "auto"),
                 data.get("jailbreak", ""), rpm, profile_id))
            return profile_id
        cur = self._exec(
            "INSERT INTO model_profiles(name,base_url,api_key,model,context_length,input_price,output_price,"
            " currency,max_tokens,timeout,temperature,reasoning_mode,max_output_mode,jailbreak,rpm,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (data.get("name", ""), data.get("base_url", ""), data.get("api_key", ""),
             data.get("model", ""), int(data.get("context_length", db.DEFAULT_CONTEXT_LENGTH)),
             float(data.get("input_price", 0)), float(data.get("output_price", 0)),
             data.get("currency", "元"), int(data.get("max_tokens", 4096)),
             int(data.get("timeout", 120)), float(data.get("temperature", 0.8)),
             data.get("reasoning_mode", "max"), data.get("max_output_mode", "auto"),
             data.get("jailbreak", ""), rpm, db.now()))
        return cur.lastrowid

    def delete_profile(self, profile_id: int):
        """删除档案；绑定的路由解除绑定，备用渠道链里的引用一并清理。"""
        self._exec("UPDATE task_routes SET profile_id=NULL WHERE profile_id=?", (profile_id,))
        self._exec("DELETE FROM model_profiles WHERE id=?", (profile_id,))
        self._purge_fallback_refs(profile_id)

    # ---------- 渠道故障自动切换：备用渠道链（2026-09-18） ----------
    # 一个角色 = 主渠道（绑定的档案）+ 备用渠道顺序表。主渠道报「渠道层错误」
    # （余额耗尽/限流/Key 失效/服务端故障/网络不通）且重试仍失败时，按顺序换下一个，
    # 直到成功或备用链跑完（详见 pipeline.classify_channel_failure）。

    def channel_failover_enabled(self) -> bool:
        """自动切换总开关（默认开；未配备用渠道时行为与旧版完全一致）。"""
        raw = str(self.get_global_setting(CHANNEL_FAILOVER_ENABLED_KEY) or "").strip().lower()
        return raw not in ("0", "false", "off", "no")

    def set_channel_failover_enabled(self, enabled: bool):
        self.set_global_setting(CHANNEL_FAILOVER_ENABLED_KEY, "1" if enabled else "0")

    def fallback_channels(self) -> dict:
        """全部角色的备用渠道链 {role: [profile_id, ...]}。"""
        return parse_fallback_channels(self.get_global_setting(FALLBACK_CHANNELS_KEY))

    def get_fallback_profile_ids(self, role: str) -> list[int]:
        """某角色的备用渠道档案 id 顺序表（空 = 该角色不自动切换）。"""
        return self.fallback_channels().get(role, [])

    def set_fallback_profile_ids(self, role: str, ids) -> list[int]:
        """保存某角色的备用渠道链（保持传入顺序、去重、丢弃非法值）；空表即删除该角色配置。"""
        clean = parse_fallback_channels({role: list(ids or [])}).get(role, [])
        data = self.fallback_channels()
        if clean:
            data[role] = clean
        else:
            data.pop(role, None)
        self.set_global_setting(FALLBACK_CHANNELS_KEY, dump_fallback_channels(data))
        return clean

    def _purge_fallback_refs(self, profile_id: int):
        """档案被删除后，把备用渠道链里对它的引用一并摘掉（防链里留死 id）。"""
        data = self.fallback_channels()
        changed = False
        for role in list(data):
            if profile_id in data[role]:
                data[role] = [i for i in data[role] if i != profile_id]
                changed = True
            if not data[role]:
                data.pop(role)
        if changed:
            self.set_global_setting(FALLBACK_CHANNELS_KEY, dump_fallback_channels(data))

    # ---------- 任务角色 ----------

    def list_routes(self) -> list[dict]:
        """角色路由 + 绑定档案信息。"""
        return self._rows(
            "SELECT r.*, p.name AS profile_name, p.model AS profile_model, p.base_url AS profile_base_url"
            " FROM task_routes r LEFT JOIN model_profiles p ON r.profile_id=p.id ORDER BY r.id")

    def get_route(self, role: str) -> dict | None:
        return self._one(
            "SELECT r.*, p.name AS profile_name, p.model AS profile_model, p.base_url AS profile_base_url"
            " FROM task_routes r LEFT JOIN model_profiles p ON r.profile_id=p.id WHERE r.role=?", (role,))

    def save_route(self, role: str, profile_id: int | None, temperature: float,
                   system_prompt: str | None = None, name: str | None = None,
                   locked: int | None = None):
        sets = ["profile_id=?", "temperature=?"]
        params = [profile_id, float(temperature)]
        if system_prompt is not None:
            sets.append("system_prompt=?")
            params.append(system_prompt)
        if name is not None:
            sets.append("name=?")
            params.append(name)
        if locked is not None:
            sets.append("locked=?")
            params.append(1 if locked else 0)
        params.append(role)
        self._exec(f"UPDATE task_routes SET {', '.join(sets)} WHERE role=?", params)

    def set_route_locked(self, role: str, locked: bool):
        """锁定/解锁任务角色：一键绑定全部角色时不更换该角色档案。"""
        self._exec("UPDATE task_routes SET locked=? WHERE role=?", (1 if locked else 0, role))

    def add_role(self, role: str, name: str, temperature: float = 0.7):
        # role 有 UNIQUE 约束：重复添加不崩溃（UI 已先查后增，此处做防御）
        self._exec("INSERT OR IGNORE INTO task_routes(role,name,profile_id,temperature,system_prompt,created_at)"
                   " VALUES(?,?,NULL,?,'',?)", (role, name, temperature, db.now()))

    def delete_role(self, role: str):
        self._exec("DELETE FROM task_routes WHERE role=?", (role,))

    # ---------- 维护 ----------

    def close(self):
        try:
            with self._lock:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        try:
            self.conn.close()
        except sqlite3.Error:
            pass
