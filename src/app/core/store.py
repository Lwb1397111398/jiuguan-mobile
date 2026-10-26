# -*- coding: utf-8 -*-
"""ProjectStore：项目数据服务层。

UI 层（页面/对话框）只允许调用本模块的方法访问数据，禁止直接写 SQL。
每个功能独立方法，改动只影响本模块。所有写操作立即提交（自动保存）。
行统一返回 dict（sqlite3.Row → dict），JSON 字段自动解析。
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
from contextlib import contextmanager

from .qt_compat import QObject, Signal  # 桌面=真Qt信号，安卓=纯Python回退（qt_compat.py）

from . import db
from .global_store import GlobalStore, GLOBAL_SETTING_KEYS
from .outline_fields import effective_field_defs
from .chapter_outline import legacy_outline_text
from .recall import norm as lore_norm

# 章节状态机
CHAPTER_STATUSES = ["未规划", "有大纲", "有细纲", "写作中", "已完成", "已归档", "已锁定"]
BLOCK_STATUSES = ["未生成", "已生成", "已确认"]
TEXT_STATUSES = ["草稿", "已接受", "已锁定"]

# 设定分类（修订版精简：角色/地点/物品/术语）
LORE_CATEGORIES = ["角色", "地点", "物品", "术语"]


def lore_context_hit(name: str, tags: list | None, hay: str) -> bool:
    """设定条目是否被上下文点名：名字归一化子串 / 标签命中 / 名字前 4 字命中。

    注入侧（assembler._lore_text 的沉睡与节流）与归档侧（store.touch_lore_mentions
    记录「最近被提及章号」）共用同一口径——两边判定必须一致，否则会出现
    「注入说命中、基准没记上」的错位（2026-09-08 设定库沉睡机制）。
    hay 必须已经 lore_norm 归一化（调用方归一次，条目逐个判定不重复归一长文本）。
    """
    n = lore_norm(str(name or ""))
    if not n or not hay:
        return False
    if n in hay:
        return True
    for t in (tags or []):
        tn = lore_norm(str(t or ""))
        if tn and tn in hay:
            return True
    return bool(len(n) >= 2 and n[:4] and n[:4] in hay)


def _foreshadow_key(content) -> str:
    """伏笔条目匹配键：归一化后取前 12 字。

    模型照抄台账原文时常改写尾段（换标点/换措辞/截断），归一化前缀匹配
    可把「走样的回收」核销到原条目上；两条不同伏笔前 12 字相同的概率极低。
    """
    return lore_norm(str(content or ""))[:12]


def _parse_due_ch(item: dict) -> int | None:
    """从台账条目解析预定回收章号：due_ch 字段优先，其次 payoff_target 里的「第N章」。"""
    if not isinstance(item, dict):
        return None
    v = item.get("due_ch")
    if v is not None:
        try:
            n = int(float(str(v).strip()))
            if n > 0:
                return n
        except Exception:
            pass
    t = str(item.get("payoff_target") or "")
    m = re.search(r"第\s*(\d+)\s*章", t) or re.search(r"^\s*(\d+)\s*$", t.strip())
    if m:
        n = int(m.group(1))
        if n > 0:
            return n
    return None
# 锁定项类型
LOCK_TYPES = ["人名", "关键设定", "结局走向", "状态卡字段", "其他"]
# 偏好作用范围
CARD_SCOPES = ["global", "stage", "manual"]
STAGE_NAMES = {
    "synopsis": "总纲", "volume": "分卷", "outline": "大纲", "detail": "细纲",
    "prose": "正文", "rewrite": "改写", "check": "检查", "style": "文风审校",
}


def jload(text: str, default=None):
    """安全解析 JSON 字段。"""
    if not text:
        return default
    try:
        obj = json.loads(text)
    except Exception:
        return default if default is not None else []
    # 仅当解析出 null 且提供了非 None 默认值时回退默认（如 characters 被写成 "null"）。
    # default=None 保持返回 None 的语义——调用方（如 get_rules）依赖它区分「未设置」。
    if obj is None and default is not None:
        return default
    return obj


def jdump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _to_int(value, default: int) -> int:
    """安全转 int：None/空串/非法值回退默认，避免数据层因脏数据抛异常。"""
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def empty_memory_state() -> dict:
    """v0 初始记忆状态。"""
    return {
        "characters": {},      # 人物名 → 状态卡
        "world_state": {"time": "", "location": "", "notes": "", "entities": {},
                        "rumors": []},
    }


def new_character_card(name: str) -> dict:
    """新人物状态卡模板（字段按修订版规格，custom_fields 按项目自定义）。"""
    return {
        "name": name,
        "identity": "",
        "appearance": "",
        "aliases": [],
        "location": "",
        "clothing": "",
        "injuries": [],
        "physical_state": "",
        "items": [],
        "posture": "",
        "emotion": "",
        "knowledge": [],
        "unknown": [],
        "inferences": [],
        "relationships": [],
        "custom_fields": {},
        # 已故标记：人物死亡后置 True，状态卡退出常规注入（防 AI 让死人出场），
        # 被点名时以「已故」简报形式出现（回忆/尸体/遗产场景仍然可用）。
        "dead": False,
    }


# 死亡兜底识别：模型没输出结构化 dead 字段、只把「已故」写进自由文本时，
# 程序据此补上（没有标记 → AI 下一章就可能让死人出场）
DEATH_HINTS = ("已故", "已死", "身亡", "死亡", "殉国", "殉职", "战死", "遇害",
               "被杀", "死在", "死去", "断气", "仙逝", "驾崩", "逝世", "去世",
               "殒命", "丧命", "暴毙", "自尽", "处死", "斩首", "尸骨", "尸体",
               "已葬", "葬于", "安葬", "下葬")
# 参与兜底识别的字段（不查 custom_fields：设定原文里的死亡剧透不算当前状态）
DEATH_TEXT_FIELDS = ("physical_state", "location", "posture", "emotion")


def _to_bool_dead(v) -> bool:
    """把模型五花八门的死亡输出统一成布尔（true / "已故" / "死亡" / 1 / yes）。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v or "").strip().lower()
    if s in ("true", "1", "yes", "y", "是", "有", "死", "dead", "deceased"):
        return True
    return any(h in s for h in ("死亡", "已故", "已死", "身亡"))


# 程序级死亡检测（2026-09-09）：模型不报、或只把死亡写进正文没写进状态卡时，
# 由程序从正文中捞出「本章死了谁」，作为待确认项补进差异清单。
# 分两类词是因为中文里主语/宾语位置决定死的是谁：「张三死了」死的是张三，
# 「一刀捅死了张三」死的也是张三——两种句式都要覆盖，但不能反过来误判
# （「张三捅死了李四」不能把张三也判死）。
#
# 不及物/状态词：死的是**词前面**的人名（主语）
DEATH_INTRANSITIVE = ("断了气", "断气", "咽了气", "咽气", "气绝", "没了气息",
                      "没有了气息", "没了声息", "没了呼吸", "停止了呼吸",
                      "停止呼吸", "心脏停跳", "断了呼吸", "当场身亡", "身亡",
                      "身死", "死透了", "死透", "死掉了", "死去", "死了", "死在",
                      "暴毙", "毙命", "殒命", "丧命", "遇害", "殉国", "殉职",
                      "战死", "自尽", "自刎", "服毒自尽", "倒地不起再无",
                      "再也没能站起来", "没了性命", "丢了性命", "丢了命",
                      "丧了命", "送了命", "一命呜呼")
# 及物动词：死的是**词后面**的人名（宾语）
DEATH_TRANSITIVE = ("杀死", "杀掉", "杀害", "屠杀", "捅死", "砍死", "刺死",
                    "毒死", "打死", "勒死", "掐死", "淹死", "烧死", "饿死",
                    "冻死", "摔死", "碾死", "压死", "射杀", "击杀", "斩杀",
                    "斩首", "处死", "处决", "害死", "弄死", "干掉", "结果了",
                    "了结了", "取了性命", "要了命", "夺走性命", "击毙",
                    "咬死", "绞死", "闷死", "炸死")
# 「死了/死去」前面的这些字说明它只是**补语**，不代表这个人死：
# ① 及物动词尾：「王五捅死了张三」→ 死的是宾语张三，凶手王五不能判死；
# ② 夸张补语：「张三累死了/笑死了/急死了」是口语夸张，不是真死。
KILL_VERB_CHARS = set("捅砍杀刺打毒勒掐淹烧饿冻摔碾压射击斩咬绞闷炸结果害弄掉死")
EXAGGERATE_CHARS = set("累笑急气吓美香甜爽挤忙渴热冷烦困馋乐爱想羞疼痛痒撑吵忙")
# 排除语境：假设/否定/虚惊一场，出现这些词的句子不判死
DEATH_NEGATE = ("差点", "险些", "几乎", "差一点", "若是", "如果", "假如",
                "要是", "以为", "仿佛", "好像", "似乎", "像是", "做梦",
                "幻觉", "幻想", "没死", "未死", "没死成", "侥幸", "幸而",
                "幸好", "险些没", "险些就", "本该死", "该死", "想死", "找死",
                "死不了", "死不掉", "诈死", "假死", "装死", "死人", "死尸")


def detect_deaths_in_prose(prose: str, names) -> list[dict]:
    """从正文里捞出「本章死了谁」（程序兜底，不依赖模型上报）。

    names：候选人物名（记忆里已有的人物，含正式名与别名）。
    返回 [{"name": 正式名/别名, "evidence": 命中句, "word": 命中词}]，
    同一人物只回一条（取最先命中的依据句）。
    """
    import re as _re
    text = str(prose or "")
    if not text.strip():
        return []
    # 候选名去重并按长度降序（长名优先，避免「张三」抢先命中「张三丰」）
    cands = sorted({str(n).strip() for n in names if str(n).strip()},
                   key=len, reverse=True)
    if not cands:
        return []
    sents = _re.split(r"[。！？；!?\n…]+", text)
    out: list[dict] = []
    seen: set = set()
    for sent in sents:
        s = sent.strip()
        if not s:
            continue
        if any(w in s for w in DEATH_NEGATE):
            continue
        for name in cands:
            if name in seen or name not in s:
                continue
            hit = None
            for i in range(len(s)):
                if not s.startswith(name, i):
                    continue
                after = s[i + len(name): i + len(name) + 8]
                before = s[max(0, i - 8): i]
                # 被动句（「张三被打死了」）里死亡词也在人名之后，不能按补语排除
                passive = ("被" in after[:2]) or ("遭" in after[:2])
                for w in DEATH_INTRANSITIVE:
                    pos = after.find(w)
                    if pos < 0:
                        continue
                    if (not passive and pos > 0
                            and after[pos - 1] in KILL_VERB_CHARS | EXAGGERATE_CHARS):
                        continue    # 及物动词补语 / 口语夸张：不是真死
                    hit = w
                    break
                if hit:
                    break
                for w in DEATH_TRANSITIVE:
                    if w in before:
                        hit = w
                        break
                if hit:
                    break
            if hit:
                seen.add(name)
                out.append({"name": name, "evidence": s[:60], "word": hit})
    return out


def sync_dead_flags(state: dict) -> dict:
    """给状态里所有实际已死、但缺结构化 dead 标记的人物补上标记（原地修改）。

    调用点：apply_diff（归档/重归档）、set_current_memory（手动编辑状态卡）。
    """
    chars = (state or {}).get("characters") or {}
    for card in chars.values():
        if not isinstance(card, dict) or card.get("dead"):
            continue
        if card.get("revived"):
            # 用户手动撤销过死亡标记：不再自动补标（否则状态卡里残留的
            # 「已故（第X章·断魂崖）」文本会让撤销永远失效）
            continue
        blob_parts = [str(card.get(f) or "") for f in DEATH_TEXT_FIELDS]
        inj = card.get("injuries")
        if isinstance(inj, (list, tuple)):
            blob_parts += [str(x) for x in inj]
        elif inj:
            blob_parts.append(str(inj))
        blob = " ".join(blob_parts)
        if blob and any(h in blob for h in DEATH_HINTS):
            card["dead"] = True
    return state


class ProjectStore(QObject):
    """单个小说项目的数据门面。

    chapters_changed 信号：章节集合发生任何变更（新增/编辑/删除/锁定/状态升级/
    回收站恢复或清除）后发出，供各页面实时同步章节树，无需手动点「新增」刷新。

    texts_changed(int) 信号：某章正文被「接受」落库后发出（参数=章节 id），
    供写作台在后台/自动模式落库后安全刷新编辑器。草稿保存不发（那是内存态）。
    """

    chapters_changed = Signal()
    texts_changed = Signal(int)

    def __init__(self, folder: str, global_store: GlobalStore | None = None):
        super().__init__()
        self.folder = os.path.abspath(folder)
        os.makedirs(self.folder, exist_ok=True)
        self.db_path = os.path.join(self.folder, "project.db")
        self.conn = db.connect(self.db_path)
        self._lock = threading.RLock()  # 连接锁：UI 线程 + AI 工作线程共用
        self._closed = False  # close() 后置位：后台线程（建向量等）据此放弃，不撞已关连接
        # 补齐新库缺失的分卷索引（旧库迁移已有、幂等安全）
        self._exec("CREATE INDEX IF NOT EXISTS idx_chapters_volume ON chapters(volume_id)")
        # 全局配置（模型/路由/偏好/预设）统一放在 GlobalStore，跨项目共享
        self.gstore = global_store or GlobalStore.default()
        self._ensure_seed()
        self._outline_backfill_error = ""
        self._backfill_legacy_outline()

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

    @contextmanager
    def _transaction(self):
        """写事务：多条语句原子提交（BEGIN IMMEDIATE 串行化写），异常自动回滚。

        事务内请直接使用 self.conn.execute（勿调 _exec/_rows/_one，
        它们的 commit 会破坏事务边界）。
        """
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self.conn.rollback()
                raise
            self.conn.commit()

    def _ensure_seed(self):
        """首次建库时写入种子数据（元信息/内置卡片/默认路由/v0 快照）。"""
        if self.get_meta("seeded"):
            return
        created = db.now()
        for key, val in [("project_title", "未命名作品"), ("created_at", created),
                         ("seeded", "1"), ("schema_version", "1")]:
            self._exec("INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)", (key, val))
        # v0 记忆快照
        self._exec("INSERT INTO memory_snapshots(version,chapter_id,full_state,diff,summary,events,stale,created_at)"
                   " VALUES(0,NULL,?,NULL,'初始状态','[]',0,?)",
                   (jdump(empty_memory_state()), created))
        # 工具设置（破甲/上下文注入/token 估算/分卷章数等）已全局共享（2026-08-08），
        # 不再写项目库；GlobalStore 负责默认值与内置破甲种子，新项目开箱即用。

    def _backfill_legacy_outline(self) -> None:
        """一次性把旧字段兼容写入 outline_content，失败时保留只读回退。"""
        if self.get_meta("chapter_outline_compat_v1", "") == "1":
            return
        rows = self._rows(
            "SELECT * FROM chapters WHERE deleted=0 "
            "AND COALESCE(TRIM(outline_content),'')=''"
        )
        updates = []
        for row in rows:
            chapter = dict(row)
            chapter["characters"] = jload(chapter.get("characters"), [])
            chapter["custom_fields"] = jload(chapter.get("outline_custom_fields"), {})
            content = legacy_outline_text(chapter)
            if content:
                updates.append((content, chapter["id"]))

        if not updates:
            self.set_meta("chapter_outline_compat_v1", "1")
            return

        try:
            # 备份发生在事务前，恢复点一定是回填前的数据库状态。
            self.backup()
            with self._lock:
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    for content, chapter_id in updates:
                        self.conn.execute(
                            "UPDATE chapters SET outline_content=?, outline_draft=?, outline_refined=1 WHERE id=?",
                            (content, content, chapter_id),
                        )
                    self.conn.execute(
                        "INSERT INTO meta(key,value) VALUES('chapter_outline_compat_v1','1') "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                    )
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
                    raise
        except Exception as exc:
            # 旧字段仍在，chapter_outline_text/outline_summary 会继续提供兼容展示。
            self._outline_backfill_error = str(exc)

    # ---------- 元信息 ----------

    def get_meta(self, key: str, default="") -> str:
        r = self._one("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else default

    def set_meta(self, key: str, value: str):
        self._exec("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, value))

    @property
    def title(self) -> str:
        return self.get_meta("project_title", "未命名作品")

    def set_title(self, title: str):
        self.set_meta("project_title", title)

    # ---------- 项目设置 ----------
    # 破甲/生成偏好等工具设置键（GLOBAL_SETTING_KEYS）已全局共享（2026-08-08），
    # 读写自动转发到 GlobalStore；其余键（伏笔笔记/大纲字段覆盖等）仍按项目存储。

    def get_setting(self, key: str, default="") -> str:
        if key in GLOBAL_SETTING_KEYS:
            return self.gstore.get_global_setting(key, default)
        r = self._one("SELECT value FROM project_settings WHERE key=?", (key,))
        return r["value"] if r else default

    def set_setting(self, key: str, value: str):
        if key in GLOBAL_SETTING_KEYS:
            self.gstore.set_global_setting(key, value)
            return
        self._exec("INSERT INTO project_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, value))

    # ---------- 大纲字段配置 ----------

    def get_outline_fields(self) -> list[dict]:
        """返回当前项目实际使用的字段：项目覆盖优先，否则跟随全局默认。"""
        raw = self.get_setting("outline_fields_override", "")
        if raw:
            try:
                return effective_field_defs(json.loads(raw))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        return self.gstore.get_outline_fields_default()

    def has_outline_field_override(self) -> bool:
        return bool(self.get_setting("outline_fields_override", ""))

    def set_outline_fields(self, fields) -> list[dict]:
        normalized = effective_field_defs(fields)
        self.set_setting("outline_fields_override", jdump(normalized))
        return normalized

    def reset_outline_fields(self) -> None:
        self.set_setting("outline_fields_override", "")

    # ---------- 任务模板（全局共享，2026-08-08 改：存全局库，所有项目通用） ----------

    def get_task_template(self, key: str) -> str:
        """用户自定义任务模板（空 = 使用内置默认）。全局共享，不再按项目存储。"""
        return self.gstore.get_task_template(key)

    def set_task_template(self, key: str, text: str) -> None:
        """保存任务模板；传空文本恢复内置默认。全局共享，所有项目生效。"""
        self.gstore.set_task_template(key, text)

    def get_setting_int(self, key: str, default: int) -> int:
        try:
            return int(self.get_setting(key, str(default)))
        except Exception:
            return default

    # ---------- 破限（jailbreak） ----------
    # 破甲是创作工具配置，与单本小说无关（2026-08-08 起全局共享）。
    # 全部方法转发 GlobalStore：库文件在全局数据目录 jailbreaks/，设置键在全局 meta。
    # 旧项目文件夹里遗留的破甲文件/设置键由 GlobalStore 一次性迁移，这里不再读写。

    def list_jailbreak_profiles(self) -> list[dict]:
        return self.gstore.list_jailbreak_profiles()

    def get_active_jailbreak_name(self) -> str:
        return self.gstore.get_active_jailbreak_name()

    def set_active_jailbreak(self, name: str):
        self.gstore.set_active_jailbreak(name)

    def save_jailbreak_profile(self, name: str, text: str) -> str:
        return self.gstore.save_jailbreak_profile(name, text)

    def delete_jailbreak_profile(self, name: str) -> bool:
        return self.gstore.delete_jailbreak_profile(name)

    def get_jailbreak_prompt(self) -> str:
        return self.gstore.get_jailbreak_prompt()

    def set_jailbreak_prompt(self, text: str):
        self.gstore.set_jailbreak_prompt(text)

    def get_jailbreak_profile_id(self) -> int:
        return self.gstore.get_jailbreak_profile_id()

    def set_jailbreak_profile_id(self, pid: int | None):
        self.gstore.set_jailbreak_profile_id(pid)

    def get_jailbreak_enabled(self) -> bool:
        return self.gstore.get_jailbreak_enabled()

    def set_jailbreak_enabled(self, enabled: bool):
        self.gstore.set_jailbreak_enabled(enabled)

    def get_jailbreak_strength(self) -> str:
        return self.gstore.get_jailbreak_strength()

    def set_jailbreak_strength(self, strength: str):
        self.gstore.set_jailbreak_strength(strength)

    def get_jailbreak_completion_guard(self) -> bool:
        return self.gstore.get_jailbreak_completion_guard()

    def set_jailbreak_completion_guard(self, enabled: bool):
        self.gstore.set_jailbreak_completion_guard(enabled)

    # ---- 破甲·由模型档案直接选择（2026-09-10 取代旧的「按模型自动匹配」） ----

    def get_profile_jailbreak(self, profile: dict | None) -> str:
        return self.gstore.get_profile_jailbreak(profile)

    def get_jailbreak_prompt_by_name(self, name: str) -> str:
        return self.gstore.get_jailbreak_prompt_by_name(name)

    def resolve_jailbreak_text_for_profile(self, profile: dict | None) -> str:
        return self.gstore.resolve_jailbreak_text_for_profile(profile)

    def rename_profile_jailbreak(self, old_name: str, new_name: str) -> int:
        return self.gstore.rename_profile_jailbreak(old_name, new_name)

    # ---------- 总纲 ----------

    def list_synopsis(self) -> list[dict]:
        """总纲全部版本，新版本在前。"""
        return self._rows("SELECT * FROM synopsis ORDER BY version DESC")

    def current_synopsis(self) -> dict | None:
        return self._one("SELECT * FROM synopsis ORDER BY version DESC LIMIT 1")

    def save_synopsis(self, content: str, title: str = "", lock: bool = False) -> dict:
        """保存新版本总纲（旧版自动成为快照历史）。"""
        ver = self.get_synopsis_next_version()
        created = db.now()
        self._exec(
            "INSERT INTO synopsis(version,title,content,is_locked,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (ver, title, content, 1 if lock else 0, created, created))
        return self._one("SELECT * FROM synopsis WHERE version=?", (ver,))

    def get_synopsis_next_version(self) -> int:
        r = self._one("SELECT COALESCE(MAX(version),0)+1 AS v FROM synopsis")
        return int(r["v"]) if r else 1

    def lock_synopsis(self, locked: bool):
        r = self.current_synopsis()
        if r:
            self._exec("UPDATE synopsis SET is_locked=? WHERE id=?", (1 if locked else 0, r["id"]))

    def update_synopsis_in_place(self, content: str, title: str = "") -> dict | None:
        """锁定态就地修改：只更新当前版本的内容/标题（不产生新版本）。

        总纲注入提示词读的是当前版本（current_synopsis），就地更新即时生效；
        同时刷新 updated_at（分卷覆盖仲裁）、同步锁定指纹（避免后续解锁重锁把
        本次小改误判为「换纲」——换纲会清分卷）。
        分卷同步按卷进行（_resync_volumes_from_synopsis）：总纲页改了哪一卷的
        『分卷N』段，哪一卷的分卷行就跟着以总纲为准（被覆盖的卷现行自动入
        版本历史可找回）；没改的卷段不动分卷行，分卷页修订继续经注入覆盖生效。
        """
        r = self.current_synopsis()
        if not r:
            return None
        self._exec("UPDATE synopsis SET content=?, title=?, updated_at=? WHERE id=?",
                   (content, title, db.now(), r["id"]))
        self._resync_volumes_from_synopsis(r["content"], content)
        self.set_meta("last_locked_synopsis_fingerprint", self.synopsis_fingerprint(content))
        return self._one("SELECT * FROM synopsis WHERE id=?", (r["id"],))

    def _resync_volumes_from_synopsis(self, old_content: str, new_content: str) -> None:
        """总纲就地修改后按卷把「段内容变了」的卷同步回分卷行（总纲是父文档口径）。

        标题/正文分别比对旧总纲段与新总纲段：只回写总纲侧变了的字段，分卷行
        另一侧的现行修订保留（如分卷迭代改过卷名、总纲就地只改段正文 → 卷名不动）。
        基线一律推进到新段值，注入覆盖据此继续正确仲裁未动卷的分卷修订。
        """
        from .pipeline import Pipeline  # 延迟导入避免循环依赖

        def _secs(text: str) -> dict:
            return {int(v.get("index") or 0): v
                    for v in (Pipeline.parse_synopsis_volumes(text or "").get("volumes") or [])}
        old_secs, new_secs = _secs(old_content), _secs(new_content)
        if not new_secs:
            return
        for i, row in enumerate(self.list_volumes(), 1):
            nv = new_secs.get(i)
            if nv is None:
                continue
            t1 = (nv.get("title") or "").strip()
            b1 = (nv.get("content") or "").strip()
            ov = old_secs.get(i)
            if ov is not None:
                title_changed = t1 != (ov.get("title") or "").strip()
                body_changed = b1 != (ov.get("content") or "").strip()
            else:
                title_changed = body_changed = True  # 总纲新增的段：以新段为准
            if not (title_changed or body_changed):
                continue  # 总纲里本卷段没动：分卷行保持（分卷修订继续有效）
            row_title = (row.get("title") or "").strip()
            row_summary = (row.get("summary") or "").strip()
            new_title = t1 if title_changed else row_title
            new_body = b1 if body_changed else row_summary
            base = self._volume_synced_base(t1, b1)
            if row_title == new_title and row_summary == new_body:
                # 分卷行已与应同步值一致：只推进基线，不升版不快照（幂等）
                self._exec("UPDATE volumes SET synopsis_synced=? WHERE id=?",
                           (base, row["id"]))
            else:
                self._update_volume_row(row, new_title, new_body,
                                        row.get("target_chapters"), synced=base)

    def push_volume_to_synopsis(self, volume_id: int) -> bool:
        """分卷侧变更（手改/AI 迭代/载入历史保存）后，把卷名+描述写回总纲『分卷N』段。

        各分卷是相对独立段落：只原位改写本卷段，其他卷与整体层逐字不动
        （Pipeline.replace_synopsis_volume）。走 update_synopsis_in_place——
        不产生总纲新版本、刷新 updated_at 与锁定指纹，其按卷 resync 会把本卷
        synopsis_synced 基线自动推进到新段值：写回后总纲与分卷一致，
        注入仲裁自然收敛（不再需要覆盖）。
        以下情形返回 False、总纲不动（调用方提示用户）：无总纲/本卷描述为空/
        总纲用 ■ 或裸行形态卷段无法定位/总纲里没有本卷『分卷N』段。
        """
        s = self.current_synopsis()
        if not s or not (s.get("content") or "").strip():
            return False
        rows = self.list_volumes()
        idx = next((i for i, x in enumerate(rows, 1) if x["id"] == volume_id), None)
        if idx is None:
            return False
        row = rows[idx - 1]
        title = (row.get("title") or "").strip()
        body = (row.get("summary") or "").strip()
        if not body:
            return False
        from .pipeline import Pipeline  # 延迟导入避免循环依赖
        new_content = Pipeline.replace_synopsis_volume(s["content"], idx, title, body)
        if new_content is None:
            return False
        if new_content == (s.get("content") or ""):
            return True  # 总纲该卷段已是现行值：幂等无写
        self.update_synopsis_in_place(new_content, s.get("title") or "")
        return True

    # ---------- 章节 ----------

    def list_chapters(self, include_trash: bool = False) -> list[dict]:
        """章节平铺列表（每行含 volume_id，UI 自行分组），附带记忆快照版本号。"""
        rows = self._rows(
            "SELECT c.*, "
            " (SELECT COUNT(*) FROM scene_blocks b WHERE b.chapter_id=c.id) AS block_count,"
            " (SELECT COUNT(*) FROM text_blocks t WHERE t.chapter_id=c.id AND t.status!='草稿') AS done_count,"
            " (SELECT version FROM memory_snapshots s WHERE s.chapter_id=c.id AND s.stale=0 ORDER BY s.version DESC LIMIT 1) AS mem_version"
            " FROM chapters c" + ("" if include_trash else " WHERE c.deleted=0")
            + " ORDER BY c.order_index")
        for r in rows:
            r["characters"] = jload(r["characters"], [])
            r["custom_fields"] = jload(r.get("outline_custom_fields"), {})
        return rows

    # ---------- 分卷 ----------

    def list_volumes(self) -> list[dict]:
        return self._rows("SELECT * FROM volumes ORDER BY order_index")

    def get_volume(self, volume_id: int) -> dict | None:
        return self._one("SELECT * FROM volumes WHERE id=?", (volume_id,))

    def _snapshot_volume(self, conn, volume: dict) -> None:
        """把分卷当前内容存档为一版历史（volume_versions）。

        与 text_block_snapshots 同模式：旧版在被替换前自动存档。
        只供持锁路径调用（事务内 conn=self.conn；独立调用走 _update_volume_row 的事务）。
        """
        conn.execute(
            "INSERT INTO volume_versions(volume_id,version,title,summary,target_chapters,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (volume["id"], int(volume.get("version") or 1),
             volume.get("title", ""), volume.get("summary", ""),
             volume.get("target_chapters"), db.now()))

    def _update_volume_row(self, volume: dict, title: str, summary: str,
                           target_chapters, conn=None, synced=None) -> None:
        """更新一卷内容；内容真的变了才快照旧版并升版本（幂等：原样保存不留空版本）。

        volume 需为改前完整行（含 version）。conn 传入表示在调用方事务里执行
        （lock_synopsis_with_volumes 用）；不传则自开单事务，快照与更新原子落库。
        synced 传入时一并写 synopsis_synced 基线（=总纲分卷段同步值，覆盖仲裁用）；
        分卷页手改/AI 迭代不传——基线保留旧同步值，注入时据此识别「分卷侧修订」。
        """
        old_title = (volume.get("title") or "").strip()
        old_summary = (volume.get("summary") or "").strip()
        changed = ((title or "").strip() != old_title
                   or (summary or "").strip() != old_summary
                   or target_chapters != volume.get("target_chapters"))
        version = int(volume.get("version") or 1)
        if changed:
            version += 1
        stamp = db.now()
        sql = ("UPDATE volumes SET title=?, summary=?, target_chapters=?, version=?,"
               " updated_at=?")
        params = [title, summary, target_chapters, version, stamp]
        if synced is not None:
            sql += ", synopsis_synced=?"
            params.append(synced)
        sql += " WHERE id=?"
        params.append(volume["id"])
        if conn is not None:
            if changed:
                self._snapshot_volume(conn, volume)
            conn.execute(sql, params)
            return
        with self._transaction():
            if changed:
                self._snapshot_volume(self.conn, volume)
            self.conn.execute(sql, params)

    @staticmethod
    def _volume_synced_base(title: str, body: str) -> str:
        """synopsis_synced 基线：上次与该卷总纲段同步时的 {卷名, 段正文}。"""
        return json.dumps({"t": (title or "").strip(), "b": (body or "").strip()},
                          ensure_ascii=False)

    def save_volume(self, data: dict) -> dict:
        """新建或更新分卷。data 可含 id/title/summary/target_chapters。

        更新时：data 未带 target_chapters 则保留原值（分卷页只编辑卷名+描述）；
        内容有任何变化自动把旧版存入 volume_versions 并递增 version
        （分卷迭代/手改都走此入口，历史可回看、可载入）。
        """
        created = db.now()
        if data.get("id"):
            old = self.get_volume(data["id"])
            if old is None:
                return None
            target = (data["target_chapters"] if "target_chapters" in data
                      else old.get("target_chapters"))
            self._update_volume_row(old, data.get("title", ""),
                                    data.get("summary", ""), target)
            return self.get_volume(data["id"])
        maxidx = self._one("SELECT COALESCE(MAX(order_index),0) AS m FROM volumes")
        order = int(maxidx["m"]) + 1 if maxidx else 1
        cur = self._exec(
            "INSERT INTO volumes(order_index,title,summary,target_chapters,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (order, data.get("title", ""), data.get("summary", ""),
             data.get("target_chapters"), created, created))
        return self.get_volume(cur.lastrowid)

    def list_volume_versions(self, volume_id: int) -> list[dict]:
        """本卷历史版本（被替换下来的旧版，按版本号降序；无则空表）。"""
        return self._rows("SELECT * FROM volume_versions WHERE volume_id=?"
                          " ORDER BY version DESC", (volume_id,))

    def get_volume_version(self, volume_id: int, version: int) -> dict | None:
        return self._one("SELECT * FROM volume_versions WHERE volume_id=? AND version=?",
                         (volume_id, version))

    def restore_volume_version(self, volume_id: int, version: int) -> dict | None:
        """载入历史版本为当前内容（旧当前版先入历史，与总纲「载入历史→保存出新版本」同语义）。"""
        snap = self.get_volume_version(volume_id, version)
        vol = self.get_volume(volume_id)
        if not snap or not vol:
            return None
        return self.save_volume({"id": volume_id, "title": snap.get("title", ""),
                                 "summary": snap.get("summary", "")})

    def sync_volumes_from_synopsis(self, volumes: list[dict]) -> list[dict]:
        """按 order_index 幂等对齐导入总纲分卷：更新已有卷 / 追加缺失卷，不删除多余卷。

        volumes: [{index, title, content}]，index 从 1 开始（总纲「■ 分卷N」序号）。
        保护章节归属：只动 volumes 表，不改 chapters 的 volume_id。
        已有卷内容被总纲覆盖时旧版自动入 volume_versions（总纲迭代重锁可回溯）。
        """
        if not volumes:
            return []
        rows = {v["order_index"]: v for v in self.list_volumes()}
        saved = []
        created = db.now()
        for v in volumes:
            idx = int(v.get("index") or 0)
            title = v.get("title") or ""
            summary = v.get("content") or ""
            base = self._volume_synced_base(title, summary)
            exist = rows.get(idx)
            if exist:
                # 已有卷：更新标题/描述（保留 target_chapters），内容变则以新总纲为准
                self._update_volume_row(exist, title, summary,
                                        exist.get("target_chapters"), synced=base)
                saved.append(self.get_volume(exist["id"]))
            else:
                cur = self._exec(
                    "INSERT INTO volumes(order_index,title,summary,target_chapters,"
                    "synopsis_synced,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (idx, title, summary, None, base, created, created))
                saved.append(self.get_volume(cur.lastrowid))
        return [s for s in saved if s]

    @staticmethod
    def synopsis_fingerprint(content: str) -> str:
        """总纲内容指纹（换行/首尾空白归一后取 SHA-256），用于判断是否换纲。"""
        normalized = str(content or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def get_last_locked_synopsis_fingerprint(self) -> str:
        """上次成功锁定总纲的内容指纹（空 = 尚未锁定过/旧项目无记录）。"""
        return self.get_meta("last_locked_synopsis_fingerprint", "")

    # ---------- 生成结果缓存（2026-10-27，手动生成按指纹复用） ----------

    def get_generation_cache(self, fingerprint: str) -> dict | None:
        """按请求指纹读缓存，无则 None。任何异常都返回 None（缓存绝不影响生成）。"""
        try:
            row = self.conn.execute(
                "SELECT task_role, model, result_text, thinking, created_at "
                "FROM generation_cache WHERE fingerprint = ?",
                (str(fingerprint or ""),)).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        return {"task_role": row[0], "model": row[1], "result_text": row[2],
                "thinking": row[3], "created_at": row[4]}

    def list_generation_cache_entries(self) -> list[dict]:
        """全部缓存条目（测试与排查用，不含正文只含元信息）。"""
        try:
            rows = self.conn.execute(
                "SELECT id, fingerprint, task_role, model, created_at "
                "FROM generation_cache ORDER BY id").fetchall()
        except sqlite3.Error:
            return []
        return [{"id": r[0], "fingerprint": r[1], "task_role": r[2],
                 "model": r[3], "created_at": r[4]} for r in rows]

    def save_generation_cache(self, fingerprint: str, task_role: str, model: str,
                              result_text: str, thinking: str = "",
                              keep: int = 100) -> None:
        """写入/覆盖缓存（同指纹 upsert，重新生成的结果覆盖旧行），并只保留
        最近 keep 条（按时间倒序清理最老的）。失败静默——缓存是纯增强，坏了也不能碰生成。"""
        try:
            now = db.now()
            self.conn.execute(
                "INSERT INTO generation_cache "
                "(fingerprint, task_role, model, result_text, thinking, created_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET "
                "task_role=excluded.task_role, model=excluded.model, "
                "result_text=excluded.result_text, thinking=excluded.thinking, "
                "created_at=excluded.created_at",
                (str(fingerprint or ""), str(task_role or ""), str(model or ""),
                 str(result_text or ""), str(thinking or ""), now))
            self.conn.execute(
                "DELETE FROM generation_cache WHERE id NOT IN ("
                "SELECT id FROM generation_cache ORDER BY created_at DESC, id DESC LIMIT ?)",
                (max(1, int(keep)),))
            self.conn.commit()
        except sqlite3.Error:
            pass


    def synopsis_volumes_differ(self, volumes: list[dict]) -> bool:
        """当前库中分卷与总纲解析结果的差异判断（旧项目无指纹时的换纲依据）。

        比较顺序、卷名与内容，不只比较卷数。
        """
        expected = [
            (int(v.get("index") or i), str(v.get("title") or "").strip(),
             str(v.get("content") or "").strip())
            for i, v in enumerate(volumes, 1)
        ]
        actual = [
            (int(v.get("order_index") or i), str(v.get("title") or "").strip(),
             str(v.get("summary") or "").strip())
            for i, v in enumerate(self.list_volumes(), 1)
        ]
        return actual != expected

    def _purge_chapters_in_transaction(self, chapter_ids: list[int]):
        """事务内彻底删除章节及其全部关联数据（细纲/正文/快照/动态/检查/记忆/向量）。

        只供 _transaction 内调用：直接用 self.conn.execute（_exec 的 commit 会破坏事务边界）。
        与 purge_chapter 覆盖的数据一致，但不发 chapters_changed（由调用方统一发）。
        """
        if not chapter_ids:
            return
        marks = ",".join("?" for _ in chapter_ids)
        snapshot_versions = [
            row[0] for row in self.conn.execute(
                f"SELECT version FROM memory_snapshots WHERE chapter_id IN ({marks})",
                chapter_ids,
            ).fetchall()
        ]
        text_ids = [
            row[0] for row in self.conn.execute(
                f"SELECT id FROM text_blocks WHERE chapter_id IN ({marks})",
                chapter_ids,
            ).fetchall()
        ]
        if snapshot_versions:
            version_marks = ",".join("?" for _ in snapshot_versions)
            self.conn.execute(
                f"DELETE FROM memory_vectors WHERE snapshot_version IN ({version_marks})",
                snapshot_versions,
            )
        self.conn.execute(f"DELETE FROM check_results WHERE chapter_id IN ({marks})", chapter_ids)
        self.conn.execute(f"DELETE FROM scene_blocks WHERE chapter_id IN ({marks})", chapter_ids)
        if text_ids:
            text_marks = ",".join("?" for _ in text_ids)
            self.conn.execute(
                f"DELETE FROM text_block_snapshots WHERE text_block_id IN ({text_marks})",
                text_ids,
            )
        self.conn.execute(f"DELETE FROM text_blocks WHERE chapter_id IN ({marks})", chapter_ids)
        self.conn.execute(f"DELETE FROM chapter_notes WHERE chapter_id IN ({marks})", chapter_ids)
        self.conn.execute(f"DELETE FROM memory_snapshots WHERE chapter_id IN ({marks})", chapter_ids)
        self.conn.execute(f"DELETE FROM chapters WHERE id IN ({marks})", chapter_ids)

    def lock_synopsis_with_volumes(
        self,
        content: str,
        title: str,
        volumes: list[dict],
        *,
        replace_existing: bool,
    ) -> list[dict]:
        """保存并锁定总纲 + 导入分卷，单事务原子完成。

        replace_existing=True（换纲重锁）：清除旧分卷下章节的全部创作数据后重建分卷表；
        False（首次锁定/内容未变）：按 order_index 幂等更新/追加（同 sync_volumes_from_synopsis）。
        任一步失败整体回滚：旧数据、锁定状态与指纹均保持原状。
        """
        if not volumes:
            raise ValueError("当前总纲没有可识别的分卷")
        fingerprint = self.synopsis_fingerprint(content)
        saved = []
        created = db.now()
        with self._transaction():
            current_row = self.conn.execute(
                "SELECT * FROM synopsis ORDER BY version DESC LIMIT 1"
            ).fetchone()
            current = dict(current_row) if current_row else None
            if not current or current["content"] != content or current["title"] != title:
                next_version = self.conn.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM synopsis"
                ).fetchone()[0]
                cursor = self.conn.execute(
                    "INSERT INTO synopsis(version,title,content,is_locked,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (next_version, title, content, 0, created, created),
                )
                synopsis_id = cursor.lastrowid
            else:
                synopsis_id = current["id"]

            if replace_existing:
                chapter_ids = [
                    row[0] for row in self.conn.execute(
                        "SELECT id FROM chapters WHERE volume_id IS NOT NULL"
                    ).fetchall()
                ]
                self._purge_chapters_in_transaction(chapter_ids)
                # 换纲重锁 = 整层推倒重建：旧卷及其版本历史一并清除
                # （与「永久清除旧分卷创作数据」的确认文案一致，不留孤儿快照）
                self.conn.execute("DELETE FROM volume_versions")
                self.conn.execute("DELETE FROM volumes")
                for i, volume in enumerate(volumes, 1):
                    base = self._volume_synced_base(volume.get("title", ""),
                                                    volume.get("content", ""))
                    cursor = self.conn.execute(
                        "INSERT INTO volumes(order_index,title,summary,target_chapters,"
                        "synopsis_synced,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (i, volume.get("title", ""), volume.get("content", ""), None,
                         base, created, created),
                    )
                    saved.append(dict(self.conn.execute(
                        "SELECT * FROM volumes WHERE id=?", (cursor.lastrowid,)
                    ).fetchone()))
            else:
                rows = {
                    row["order_index"]: dict(row)
                    for row in self.conn.execute("SELECT * FROM volumes ORDER BY order_index").fetchall()
                }
                for volume in volumes:
                    index = int(volume.get("index") or 0)
                    base = self._volume_synced_base(volume.get("title", ""),
                                                    volume.get("content", ""))
                    existing = rows.get(index)
                    if existing:
                        # 与 sync_volumes_from_synopsis 同口径：内容被总纲覆盖时
                        # 旧版自动入 volume_versions 并升版本（保留 target_chapters）
                        self._update_volume_row(
                            existing, volume.get("title", ""),
                            volume.get("content", ""), existing.get("target_chapters"),
                            conn=self.conn, synced=base)
                        row = self.conn.execute(
                            "SELECT * FROM volumes WHERE id=?", (existing["id"],)
                        ).fetchone()
                    else:
                        cursor = self.conn.execute(
                            "INSERT INTO volumes(order_index,title,summary,target_chapters,"
                            "synopsis_synced,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                            (index, volume.get("title", ""), volume.get("content", ""), None,
                             base, created, created),
                        )
                        row = self.conn.execute(
                            "SELECT * FROM volumes WHERE id=?", (cursor.lastrowid,)
                        ).fetchone()
                    saved.append(dict(row))

            self.conn.execute("UPDATE synopsis SET is_locked=0")
            self.conn.execute("UPDATE synopsis SET is_locked=1 WHERE id=?", (synopsis_id,))
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("last_locked_synopsis_fingerprint", fingerprint),
            )
        self.chapters_changed.emit()
        return saved

    def delete_volume(self, volume_id: int):
        """删除分卷：其下章节归入未分卷（volume_id=NULL）；版本历史一并清除。"""
        # 事务内必须用 self.conn.execute：_exec 自带 commit 会提前结束事务，
        # 导致「章节已脱离卷」先落库而「删卷」失败时留下孤儿数据。
        with self._transaction():
            self.conn.execute("UPDATE chapters SET volume_id=NULL WHERE volume_id=?", (volume_id,))
            self.conn.execute("DELETE FROM volume_versions WHERE volume_id=?", (volume_id,))
            self.conn.execute("DELETE FROM volumes WHERE id=?", (volume_id,))
        self.chapters_changed.emit()

    def reorder_volumes(self, ids: list[int]):
        with self._transaction():
            for i, vid in enumerate(ids, 1):
                self.conn.execute("UPDATE volumes SET order_index=? WHERE id=?", (i, vid))

    def set_chapter_volume(self, chapter_id: int, volume_id: int | None):
        self._exec("UPDATE chapters SET volume_id=?, updated_at=? WHERE id=?",
                   (volume_id, db.now(), chapter_id))
        self.chapters_changed.emit()

    def get_volume_summary(self, volume_id: int | None) -> str:
        """本分卷上下文：卷名 + 分卷描述（大纲生成锚点；未分卷返回空串）。"""
        if not volume_id:
            return ""
        v = self.get_volume(volume_id)
        if not v:
            return ""
        title = v.get("title") or ""
        summary = v.get("summary") or ""
        if not title and not summary:
            return ""
        return f"【本分卷（本卷大纲必须承接此分卷推进）】\n卷名：{title}\n{summary}"

    def get_prior_volumes_summary(self, volume_id: int | None) -> str:
        """某卷之前的所有已确认分卷概要（大纲生成时注入卷间衔接）。

        无卷 / 第一卷 / 卷不存在 → 返回空串（不注入卷模块）。
        """
        if not volume_id:
            return ""
        rows = self.list_volumes()
        idx = next((i for i, x in enumerate(rows) if x["id"] == volume_id), None)
        if idx is None or idx == 0:
            return ""
        rows = rows[:idx]
        lines = [f"【已确认分卷（下面共 {len(rows)} 卷，是前面所有分卷；本卷大纲必须承接其发展，不得断层或重复）】"]
        for i, v in enumerate(rows, 1):
            title = v.get("title") or f"第{i}卷"
            summary = v.get("summary") or ""
            if not title and not summary:
                continue
            lines.append(f"第{i}卷｜{title}" + (f"\n{summary[:2000]}" if summary else ""))
        return "\n\n".join(lines)

    def get_prev_volume_last_chapter(self, volume_id: int | None) -> str:
        """前一卷最后一章的衔接信息（章名 + 已写正文结尾）。

        分卷切分时注入：本卷从上一卷的结尾章之后接着写，衔接最精确、不重复总纲卷内容。
        不注入大纲（含初稿与细化版）：大纲内容会诱导切章把上卷剧情带进本卷重复展开。
        无前一卷 / 前一卷无章节 / 卷不存在 → 返回空串（不注入模块）。
        """
        if not volume_id:
            return ""
        rows = self.list_volumes()
        idx = next((i for i, x in enumerate(rows) if x["id"] == volume_id), None)
        if idx is None or idx == 0:
            return ""
        prev = rows[idx - 1]
        prev_chs = [c for c in self.list_chapters() if c.get("volume_id") == prev["id"]]
        if not prev_chs:
            return ""
        last = prev_chs[-1]  # list_chapters 已按 order_index 排序，取最后一章
        vol_label = prev.get("title") or f"第{prev['order_index']}卷"
        lines = [f"【前一卷最后一章（{vol_label}的结尾章；本卷从它之后接着写，不得重复或断层）】"]
        lines.append(f"第{last['order_index']}章 {last.get('title', '') or '未命名'}")
        blocks = self.list_text_blocks(last["id"])
        accepted = [b["content"] for b in blocks
                    if b["status"] in ("已接受", "已锁定") and (b.get("content") or "").strip()]
        if accepted:
            lines.append("（本章正文结尾，本卷接着这里续写）\n……" + accepted[-1].strip()[-800:])
        return "\n".join(lines)

    def get_all_volumes_summary(self) -> str:
        """全部已确认分卷（生成下一个分卷时注入：扩充深化、衔接不重复）。

        注：只截断到单卷 2000 字防超长；整体上下文超预算由 assembler 兜底裁剪。
        """
        rows = self.list_volumes()
        if not rows:
            return ""
        lines = [f"【已确认分卷（下面共 {len(rows)} 卷，是前面所有分卷；本卷必须承接其发展，不得断层或重复）】"]
        for i, v in enumerate(rows, 1):
            title = v.get("title") or f"第{i}卷"
            summary = v.get("summary") or ""
            if not title and not summary:
                continue
            lines.append(f"第{i}卷｜{title}" + (f"\n{summary[:2000]}" if summary else ""))
        return "\n\n".join(lines)

    def replace_all_volumes(self, volumes: list[dict]) -> list[dict]:
        """整体替换分卷方案：删除现有全部卷（其下章节归未分卷）后批量插入新卷。"""
        # 同上：事务内一律用 self.conn.execute（_exec/_one 的 commit 会拆散事务）
        with self._transaction():
            self.conn.execute("UPDATE chapters SET volume_id=NULL")
            self.conn.execute("DELETE FROM volumes")
            saved = []
            for i, v in enumerate(volumes, 1):
                created = db.now()
                cur = self.conn.execute(
                    "INSERT INTO volumes(order_index,title,summary,target_chapters,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (i, v.get("title", ""), v.get("summary", ""),
                     v.get("target_chapters"), created, created))
                row = self.conn.execute(
                    "SELECT * FROM volumes WHERE id=?", (cur.lastrowid,)).fetchone()
                saved.append(dict(row) if row else None)
        self.chapters_changed.emit()
        return saved

    def list_trash(self) -> list[dict]:
        rows = self._rows("SELECT * FROM chapters WHERE deleted=1 ORDER BY deleted_at DESC")
        for r in rows:
            r["characters"] = jload(r.get("characters"), [])
            r["custom_fields"] = jload(r.get("outline_custom_fields"), {})
        return rows

    def get_chapter(self, chapter_id: int) -> dict | None:
        r = self._one("SELECT * FROM chapters WHERE id=?", (chapter_id,))
        if r:
            r["characters"] = jload(r["characters"], [])
            r["custom_fields"] = jload(r.get("outline_custom_fields"), {})
        return r

    def add_chapter(self, data: dict) -> dict:
        """新建章节。data 可含各字段；顺序自动排最后；volume_id 可空（未分卷）。"""
        maxidx = self._one("SELECT COALESCE(MAX(order_index),0) AS m FROM chapters WHERE deleted=0")
        order = int(maxidx["m"]) + 1 if maxidx else 1
        created = db.now()
        chars = jdump(data.get("characters", []))
        custom_fields = data.get("custom_fields", {})
        if not isinstance(custom_fields, dict):
            custom_fields = {}
        volume_id = data.get("volume_id")
        outline_refined = 1 if data.get("outline_refined") else 0
        # 分卷切分初稿单独落列：未显式给 outline_draft 时以创建时的大纲为初稿，
        # 之后细化/重生成只改 outline_content，初稿永久保留（重新细化的底稿）。
        outline_draft = str(data.get("outline_draft") or data.get("outline_content") or "")
        cur = self._exec(
            "INSERT INTO chapters(order_index,volume_id,status,title,event,conflict,characters,outline_custom_fields,"
            " info_progress,char_change,hook,target_words,outline_content,outline_draft,outline_refined,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order, volume_id, data.get("status", "未规划"), data.get("title", ""), data.get("event", ""),
             data.get("conflict", ""), chars, jdump(custom_fields), data.get("info_progress", ""),
             data.get("char_change", ""), data.get("hook", ""),
             _to_int(data.get("target_words"), 3000), data.get("outline_content", ""),
             outline_draft, outline_refined, created, created))
        self.chapters_changed.emit()
        return self.get_chapter(cur.lastrowid)

    CHAPTER_FIELDS = {"title", "event", "conflict", "info_progress", "char_change", "hook",
                      "outline_content", "outline_draft"}

    def update_chapter(self, chapter_id: int, patch: dict):
        """更新章节字段。patch 白名单；characters 特殊处理；状态变化走 set_chapter_status。

        初稿兜底：章节首次写入非空 outline_content 且 outline_draft 仍为空时
        （空章创建后手动写大纲的路径），顺手把这份内容记为初稿——细化/重生成
        之后再来改 outline_content 就不再动初稿了。
        """
        if ("outline_content" in patch and "outline_draft" not in patch
                and str(patch.get("outline_content") or "").strip()):
            cur = self._one("SELECT outline_draft FROM chapters WHERE id=?", (chapter_id,))
            if cur is not None and not str(cur.get("outline_draft") or "").strip():
                patch = {**patch, "outline_draft": patch["outline_content"]}
        sets, params = [], []
        for key, val in patch.items():
            if key in self.CHAPTER_FIELDS:
                sets.append(f"{key}=?")
                params.append(val)
            elif key == "target_words":
                sets.append("target_words=?")
                params.append(_to_int(val, 0))
            elif key == "characters":
                sets.append("characters=?")
                params.append(jdump(val))
            elif key == "custom_fields":
                sets.append("outline_custom_fields=?")
                params.append(jdump(val if isinstance(val, dict) else {}))
        if sets:
            sets.append("updated_at=?")
            params.append(db.now())
            params.append(chapter_id)
            self._exec(f"UPDATE chapters SET {', '.join(sets)} WHERE id=?", params)
            self.chapters_changed.emit()

    def merge_chapter_characters(self, chapter_id: int, names: list):
        """把新出现的人物名并入章节出场人物（保持已有顺序、去重，不删已有人名）。

        细纲块「涉及人物」/单块编辑的出场人物聚合到这里——右栏人物状态卡、
        资料速查、正文/细纲注入都依赖 chapter.characters，缺失会导致右栏长期为空。
        """
        names = [str(n).strip() for n in (names or []) if str(n or "").strip()]
        if not names:
            return
        ch = self.get_chapter(chapter_id)
        existing = list(ch.get("characters") or []) if ch else []
        merged = list(existing)
        for n in names:
            if n not in merged:
                merged.append(n)
        if merged != existing:
            self.update_chapter(chapter_id, {"characters": merged})

    def backfill_chapter_characters(self) -> int:
        """把各章细纲块「涉及人物」并入章节出场人物（union，安全幂等）。

        老项目升级/早期版本章节 characters 为空，导致右栏状态卡/资料速查长期空白。
        只在有新增人名时写库。返回补齐的章节数。
        """
        count = 0
        for ch in self.list_chapters():
            existing = list(ch.get("characters") or [])
            names = list(existing)
            for b in self.list_blocks(ch["id"]):
                for n in (b.get("characters") or []):
                    n = str(n).strip()
                    if n and n not in names:
                        names.append(n)
            if names != existing:
                self.update_chapter(ch["id"], {"characters": names})
                count += 1
        return count

    def backfill_chapter_characters_from_memory(self) -> int:
        """把已归档章节记忆快照里的角色并入章节出场人物（历史书升级用）。

        早期章节 characters 为空，右栏状态卡/资料速查长期空白；归档时其实已生成
        记忆卡，这里按快照 chapter_id 回填该章角色。只在章内人名为空时写库，
        幂等安全。返回补齐的章节数。
        """
        count = 0
        for ch in self.list_chapters():
            existing = list(ch.get("characters") or [])
            if existing:
                continue
            snap = self.memory_snapshot_for_chapter(ch["id"])
            if not snap:
                continue
            names = list((snap.get("full_state") or {}).get("characters") or {})
            if names:
                self.update_chapter(ch["id"], {"characters": names})
                count += 1
        return count

    def set_chapter_status(self, chapter_id: int, status: str):
        if status not in CHAPTER_STATUSES:
            raise ValueError(f"非法章节状态: {status}")
        self._exec("UPDATE chapters SET status=?, updated_at=? WHERE id=?",
                   (status, db.now(), chapter_id))
        self.chapters_changed.emit()

    def lock_outline(self, chapter_id: int, locked: bool):
        self._exec("UPDATE chapters SET outline_locked=?, updated_at=? WHERE id=?",
                   (1 if locked else 0, db.now(), chapter_id))
        self.chapters_changed.emit()

    def set_outline_refined(self, chapter_id: int, refined: bool = True):
        """标记章节大纲已完成 AI 细化（或手动定稿）。

        分卷切分产生的初稿大纲默认未细化（outline_refined=0），大纲页不展示内容；
        只有细化/重生成/手动保存后才置 1，让用户能分辨哪些章还没细化。
        """
        self._exec("UPDATE chapters SET outline_refined=?, updated_at=? WHERE id=?",
                   (1 if refined else 0, db.now(), chapter_id))
        self.chapters_changed.emit()

    def reorder_chapters(self, ordered_ids: list[int]):
        with self._transaction():
            for i, cid in enumerate(ordered_ids, start=1):
                self.conn.execute("UPDATE chapters SET order_index=? WHERE id=?", (i, cid))
        self.chapters_changed.emit()

    def delete_chapter(self, chapter_id: int):
        """软删除进回收站；该章及之后快照标记过期（链不变）。

        用 mark_stale_from（版本号>=该章最新快照）而非仅该章自己的快照：
        - 尾部删除 → 后续快照全部过期，current_memory 自动回落到最后存活章；
        - 中间删除 → 后续存活章快照过期，触发批量重归档提示，重建其记忆状态。
        """
        self._exec("UPDATE chapters SET deleted=1, deleted_at=? WHERE id=?", (db.now(), chapter_id))
        self.mark_stale_from(chapter_id)
        self.chapters_changed.emit()

    def restore_chapter(self, chapter_id: int):
        """从回收站恢复；快照取消过期标记（中间章恢复后仍可能需重归档）。"""
        self._exec("UPDATE chapters SET deleted=0, deleted_at=NULL WHERE id=?", (chapter_id,))
        self._exec("UPDATE memory_snapshots SET stale=0 WHERE chapter_id=?", (chapter_id,))
        self.chapters_changed.emit()

    def purge_chapter(self, chapter_id: int):
        """回收站彻底删除：章节 + 细纲/正文/快照/动态 + 记忆快照 + 记忆向量 + 检查记录。

        记忆向量必须随快照一并清掉：否则已删除章节的记忆单元仍留在向量索引里，
        语义检索会把「已删内容」当成相关记忆注入后续正文（幽灵记忆）。
        """
        # 该章各快照的版本号（memory_vectors.snapshot_version 指向它）
        snap_versions = [r["version"] for r in
                         self._rows("SELECT version FROM memory_snapshots WHERE chapter_id=?", (chapter_id,))]
        for v in snap_versions:
            self._exec("DELETE FROM memory_vectors WHERE snapshot_version=?", (v,))
        self._exec("DELETE FROM check_results WHERE chapter_id=?", (chapter_id,))
        self._exec("DELETE FROM scene_blocks WHERE chapter_id=?", (chapter_id,))
        for tb in self._rows("SELECT id FROM text_blocks WHERE chapter_id=?", (chapter_id,)):
            self._exec("DELETE FROM text_block_snapshots WHERE text_block_id=?", (tb["id"],))
        self._exec("DELETE FROM text_blocks WHERE chapter_id=?", (chapter_id,))
        self._exec("DELETE FROM chapter_notes WHERE chapter_id=?", (chapter_id,))
        self._exec("DELETE FROM memory_snapshots WHERE chapter_id=?", (chapter_id,))
        self._exec("DELETE FROM chapters WHERE id=?", (chapter_id,))
        self.chapters_changed.emit()

    def purge_trash(self):
        for c in self.list_trash():
            self.purge_chapter(c["id"])

    # ---------- 细纲块 ----------

    def list_blocks(self, chapter_id: int) -> list[dict]:
        rows = self._rows("SELECT * FROM scene_blocks WHERE chapter_id=? ORDER BY order_index", (chapter_id,))
        for r in rows:
            r["characters"] = jload(r["characters"], [])
        return rows

    def get_block(self, block_id: int) -> dict | None:
        r = self._one("SELECT * FROM scene_blocks WHERE id=?", (block_id,))
        if r:
            r["characters"] = jload(r["characters"], [])
        return r

    def add_block(self, chapter_id: int, data: dict) -> dict:
        maxidx = self._one("SELECT COALESCE(MAX(order_index),0) AS m FROM scene_blocks WHERE chapter_id=?",
                           (chapter_id,))
        order = int(maxidx["m"]) + 1 if maxidx else 1
        created = db.now()
        cur = self._exec(
            "INSERT INTO scene_blocks(chapter_id,order_index,content,target_words,narrative_task,"
            " must_have,point,characters,status,block_type,scene_direction,sensory_focus,to_state,event_title,"
            "shot_script,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (chapter_id, order, data.get("content", ""), _to_int(data.get("target_words"), 0),
             data.get("narrative_task", ""), data.get("must_have", ""),
             data.get("point", ""), jdump(data.get("characters", [])),
             data.get("status", "未生成"),
             data.get("block_type", ""), data.get("scene_direction", ""),
             data.get("sensory_focus", ""), data.get("to_state", ""),
             data.get("event_title", ""), data.get("shot_script", ""),
             created, created))
        return self.get_block(cur.lastrowid)

    def update_block(self, block_id: int, patch: dict):
        sets, params = [], []
        for key, val in patch.items():
            if key in ("content", "narrative_task", "must_have", "point", "status",
                       "block_type", "scene_direction", "sensory_focus", "to_state",
                       "event_title", "shot_script"):
                sets.append(f"{key}=?")
                params.append(val)
            elif key == "target_words":
                sets.append("target_words=?")
                params.append(_to_int(val, 0))
            elif key == "characters":
                sets.append("characters=?")
                params.append(jdump(val))
        if sets:
            sets.append("updated_at=?")
            params.append(db.now())
            params.append(block_id)
            self._exec(f"UPDATE scene_blocks SET {', '.join(sets)} WHERE id=?", params)

    def delete_block(self, block_id: int):
        """删除细纲块并级联删除其关联正文（含快照）。

        否则残留正文块会成为「孤儿」：重新生成细纲/正文时被前文衔接误读，
        生成正文会错误出现已删内容的结尾。
        """
        tbs = self._rows("SELECT id FROM text_blocks WHERE scene_block_id=?", (block_id,))
        for tb in tbs:
            self._exec("DELETE FROM text_block_snapshots WHERE text_block_id=?", (tb["id"],))
        self._exec("DELETE FROM text_blocks WHERE scene_block_id=?", (block_id,))
        self._exec("DELETE FROM scene_blocks WHERE id=?", (block_id,))

    def move_block(self, block_id: int, new_index: int, chapter_id: int):
        blocks = self.list_blocks(chapter_id)
        ids = [b["id"] for b in blocks]
        if block_id not in ids:
            return
        ids.remove(block_id)
        new_index = max(0, min(new_index, len(ids)))
        ids.insert(new_index, block_id)
        for i, bid in enumerate(ids, start=1):
            self._exec("UPDATE scene_blocks SET order_index=? WHERE id=?", (i, bid))

    def replace_blocks(self, chapter_id: int, blocks: list[dict]):
        """AI 批量生成后整章替换细纲块（事务：删除与新增同成败，中途异常回滚不丢旧块）。

        整章替换意味着旧细纲及其正文整体作废——同时删除本章全部旧细纲正文块
        （含快照），避免旧正文残留被前文衔接误读；自由写作块（scene_block_id 为空）
        不受影响，仍属本章已写正文。
        """
        with self._transaction():
            self.conn.execute("DELETE FROM text_block_snapshots WHERE text_block_id IN"
                              " (SELECT id FROM text_blocks WHERE chapter_id=? AND scene_block_id IS NOT NULL)",
                              (chapter_id,))
            self.conn.execute("DELETE FROM text_blocks WHERE chapter_id=? AND scene_block_id IS NOT NULL",
                              (chapter_id,))
            self.conn.execute("DELETE FROM scene_blocks WHERE chapter_id=?", (chapter_id,))
            created = db.now()
            for order, b in enumerate(blocks, start=1):
                self.conn.execute(
                    "INSERT INTO scene_blocks(chapter_id,order_index,content,target_words,narrative_task,"
                    " must_have,point,characters,status,block_type,scene_direction,sensory_focus,to_state,event_title,"
                    "shot_script,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (chapter_id, order, b.get("content", ""), _to_int(b.get("target_words"), 0),
                     b.get("narrative_task", ""), b.get("must_have", ""),
                     b.get("point", ""), jdump(b.get("characters", [])),
                     b.get("status", "未生成"),
                     b.get("block_type", ""), b.get("scene_direction", ""),
                     b.get("sensory_focus", ""), b.get("to_state", ""),
                     b.get("event_title", ""), b.get("shot_script", ""),
                     created, created))

    def set_block_status(self, block_id: int, status: str):
        self._exec("UPDATE scene_blocks SET status=?, updated_at=? WHERE id=?",
                   (status, db.now(), block_id))

    # ---------- 正文块 ----------

    def list_text_blocks(self, chapter_id: int) -> list[dict]:
        return self._rows(
            "SELECT t.*, b.order_index AS block_order FROM text_blocks t"
            " LEFT JOIN scene_blocks b ON t.scene_block_id=b.id"
            " WHERE t.chapter_id=? ORDER BY t.order_index", (chapter_id,))

    def get_text_block(self, block_id: int) -> dict | None:
        return self._one("SELECT * FROM text_blocks WHERE id=?", (block_id,))

    def add_text_block(self, chapter_id: int, scene_block_id: int | None = None,
                       content: str = "", order_index: int | None = None,
                       kind: str = "") -> dict:
        if order_index is None:
            maxidx = self._one("SELECT COALESCE(MAX(order_index),0) AS m FROM text_blocks WHERE chapter_id=?",
                               (chapter_id,))
            order_index = int(maxidx["m"]) + 1 if maxidx else 1
        created = db.now()
        cur = self._exec(
            "INSERT INTO text_blocks(chapter_id,scene_block_id,order_index,content,status,version,kind,created_at,updated_at)"
            " VALUES(?,?,?,?,?,1,?,?,?)",
            (chapter_id, scene_block_id, order_index, content, "草稿", kind, created, created))
        return self.get_text_block(cur.lastrowid)

    # ---------- 整章正文（一章一条 kind='chapter'，两种写法最终都落到这里） ----------

    def get_prose_mode(self) -> str:
        """正文写法：'chapter'=一次性整章写完（默认）/ 'batch'=按细纲块分批接龙
        / 'outline'=按大纲直写（不生成细纲，整章或分批均按本章大纲）。

        旧值 'block'（按细纲块逐块，已删除的写法）读入时归一为 'chapter'。
        """
        mode = self.get_setting("prose_mode", "chapter") or "chapter"
        return mode if mode in ("chapter", "batch", "outline") else "chapter"

    def set_prose_mode(self, mode: str):
        self.set_setting("prose_mode",
                         mode if mode in ("chapter", "batch", "outline") else "chapter")

    def get_outline_batch_split(self) -> str:
        """按大纲直写模式的分批方式：'whole'=整章一次 / 'beat'=按大纲节拍 / 'free'=AI 自由划分。"""
        split = self.get_setting("outline_batch_split", "beat") or "beat"
        return split if split in ("whole", "beat", "free") else "beat"

    def set_outline_batch_split(self, split: str):
        self.set_setting("outline_batch_split",
                         split if split in ("whole", "beat", "free") else "beat")

    # ---------- 分批口径（2026-10-24：按计划总批数 / 按每批数量） ----------

    def get_batch_split_by(self) -> str:
        """分批口径：'count'=按计划总批数（默认）/ 'size'=按每批数量（旧口径）。"""
        split_by = self.get_setting("batch_split_by", "count") or "count"
        return split_by if split_by in ("count", "size") else "count"

    def set_batch_split_by(self, split_by: str):
        self.set_setting("batch_split_by",
                         split_by if split_by in ("count", "size") else "count")

    def get_prose_batch_plan_value(self) -> tuple[int, bool]:
        """细纲分批的界面数值与口径：(计划总批数|每批块数, 是否按总批数)。"""
        by_count = self.get_batch_split_by() == "count"
        key, default = (("prose_batch_count", 3) if by_count
                        else ("prose_batch_size", 3))
        return self.get_setting_int(key, default), by_count

    def set_prose_batch_plan_value(self, value: int):
        by_count = self.get_batch_split_by() == "count"
        value = max(1, min(20 if by_count else 8, int(value or 1)))
        self.set_setting("prose_batch_count" if by_count else "prose_batch_size",
                         str(value))

    def get_outline_batch_plan_value(self) -> tuple[int, bool]:
        """按大纲分批的界面数值与口径：(计划总批数|每批节拍数, 是否按总批数)。"""
        by_count = self.get_batch_split_by() == "count"
        key, default = (("outline_batch_count", 3) if by_count
                        else ("outline_batch_size", 2))
        return self.get_setting_int(key, default), by_count

    def set_outline_batch_plan_value(self, value: int):
        by_count = self.get_batch_split_by() == "count"
        value = max(1, min(20 if by_count else 8, int(value or 1)))
        self.set_setting("outline_batch_count" if by_count else "outline_batch_size",
                         str(value))

    def get_chapter_prose_block(self, chapter_id: int) -> dict | None:
        """本章整章正文块（kind='chapter'）；未写过返回 None。

        历史上一章只应有一条；万一有多条（并发/异常）取内容最长的那条，
        避免拿到空壳把已写正文藏起来。
        """
        rows = self._rows("SELECT * FROM text_blocks WHERE chapter_id=? AND kind='chapter'"
                          " ORDER BY id", (chapter_id,))
        if not rows:
            return None
        return max(rows, key=lambda r: len(r.get("content") or ""))

    def chapter_prose_text(self, chapter_id: int) -> str:
        """本章整章正文文本（未写返回空串）。"""
        tb = self.get_chapter_prose_block(chapter_id)
        return (tb.get("content") or "") if tb else ""

    def accept_chapter_prose(self, chapter_id: int, content: str) -> dict | None:
        """接受整章正文：不存在则创建 kind='chapter' 块，已存在则旧版存快照后覆盖。

        走 accept_text_block 的快照/版本语义（可回滚），故与「接受」红线一致：
        用户确认后才落库，每次落库自动存旧版快照。返回该正文块。
        """
        content = content or ""
        tb = self.get_chapter_prose_block(chapter_id)
        if tb is None:
            # 首版直接按「已接受」建块（version 保持 1）：先建空壳再 accept 会把
            # 第一版正文记成「第 2 版」，UI 版本号对不上。
            tb = self.add_text_block(chapter_id, None, content, kind="chapter")
            self._exec("UPDATE text_blocks SET status='已接受', updated_at=? WHERE id=?",
                       (db.now(), tb["id"]))
            self.texts_changed.emit(chapter_id)
        else:
            self.accept_text_block(tb["id"], content)
        # 整章正文生效 → 该章旧的分块正文降为草稿（避免导出/检查/记忆读到双份）
        self.demote_block_prose_texts(chapter_id)
        return self.get_text_block(tb["id"])

    def demote_block_prose_texts(self, chapter_id: int) -> int:
        """把该章「分块正文」降为草稿，返回降级条数（整章正文被接受后调用）。

        一章的正文只有一种形态生效：整章正文 或 分块正文。两者同时「已接受」会让
        导出/检查/文风/记忆读到重复正文（整章一遍 + 各块再一遍）。这里只改状态、
        不删内容（块列表仍可见，重新点「接受」即可恢复）；已锁定的块不动。
        """
        rows = self._rows("SELECT id FROM text_blocks WHERE chapter_id=? AND kind<>'chapter'"
                          " AND scene_block_id IS NOT NULL AND status='已接受'", (chapter_id,))
        for r in rows:
            self._exec("UPDATE text_blocks SET status='草稿', updated_at=? WHERE id=?",
                       (db.now(), r["id"]))
        return len(rows)

    def demote_chapter_prose_block(self, chapter_id: int) -> int:
        """把该章「整章正文」降为草稿，返回降级条数（细纲块正文被接受后调用）。

        与 demote_block_prose_texts 互为反向：最后被接受的那种正文形态生效，
        另一种降为草稿（内容保留、可恢复）；已锁定的整章正文不动。
        """
        rows = self._rows("SELECT id FROM text_blocks WHERE chapter_id=? AND kind='chapter'"
                          " AND status='已接受'", (chapter_id,))
        for r in rows:
            self._exec("UPDATE text_blocks SET status='草稿', updated_at=? WHERE id=?",
                       (db.now(), r["id"]))
        return len(rows)

    # ---------- 正文分批（prose_mode=batch：批次工作区，全部批完成后合并落库） ----------

    def list_prose_batches(self, chapter_id: int) -> list[dict]:
        """本章批次（按批序旧→新）。"""
        return self._rows("SELECT * FROM prose_batches WHERE chapter_id=?"
                          " ORDER BY order_index", (chapter_id,))

    def get_prose_batch(self, batch_id: int) -> dict | None:
        return self._one("SELECT * FROM prose_batches WHERE id=?", (batch_id,))

    def prose_batch_block_ids(self, row: dict) -> list[int]:
        """解析批次行的 block_ids JSON（损坏/缺失返回空列表）。"""
        try:
            ids = json.loads(row.get("block_ids") or "[]")
            return [int(i) for i in ids] if isinstance(ids, list) else []
        except Exception:
            return []

    def add_prose_batch(self, chapter_id: int, order_index: int, block_ids: list[int],
                        content: str, state_package: str = "") -> dict:
        """登记一批正文（用户确认后调用）。同章同批号已存在 → 覆盖（重生成即替换）。"""
        content = content or ""
        state_package = state_package or ""
        ids_json = json.dumps([int(i) for i in (block_ids or [])], ensure_ascii=False)
        existing = self._one("SELECT id FROM prose_batches WHERE chapter_id=? AND order_index=?",
                             (chapter_id, order_index))
        now = db.now()
        if existing:
            self._exec("UPDATE prose_batches SET block_ids=?, content=?, state_package=?,"
                       " updated_at=? WHERE id=?",
                       (ids_json, content, state_package, now, existing["id"]))
        else:
            self._exec("INSERT INTO prose_batches(chapter_id,order_index,block_ids,content,"
                       "state_package,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                       (chapter_id, order_index, ids_json, content, state_package, now, now))
        row = self._one("SELECT * FROM prose_batches WHERE chapter_id=? AND order_index=?",
                        (chapter_id, order_index))
        # 通知写作台刷新（批次列表/合并预览随批次变化）
        self.texts_changed.emit(chapter_id)
        return row

    def clear_prose_batches(self, chapter_id: int):
        """清空本章全部批次（重新开始分批）。已落库的整章正文不受影响。"""
        self._exec("DELETE FROM prose_batches WHERE chapter_id=?", (chapter_id,))
        self.texts_changed.emit(chapter_id)

    def prose_batch_belongs_to(self, row: dict, mode: str) -> bool:
        """批次行是否属于指定写法（prose_batches 表没有写法字段，按 block_ids 判别）。

        细纲分批（batch）每批都引用至少一个细纲块；按大纲分批（outline）恒定写
        空数组（本模式没有细纲块，2026-10-17）。两种分批共用同一张批次表，
        切写法后不区分就会把旧批当自己的批接着写/合并。
        """
        ids = self.prose_batch_block_ids(row)
        return bool(ids) if mode == "batch" else not ids

    def drop_foreign_prose_batches(self, chapter_id: int, mode: str) -> int:
        """作废不属于当前写法的批次，返回清掉的批数（无残留返回 0）。

        只删外来行会留下断档批号（next_idx = len(rows)+1 会覆盖或跳批），
        所以发现一条外来批次就整章作废——语义同「细纲已变化，旧批次全部作废」。
        已接受的整章正文不受影响。
        """
        rows = self.list_prose_batches(chapter_id)
        if not rows or all(self.prose_batch_belongs_to(r, mode) for r in rows):
            return 0
        self.clear_prose_batches(chapter_id)
        return len(rows)

    def truncate_prose_batches(self, chapter_id: int, from_index: int):
        """从第 from_index 批起作废（「从本批重新生成」：该批及其后全部重来）。"""
        self._exec("DELETE FROM prose_batches WHERE chapter_id=? AND order_index>=?",
                   (chapter_id, int(from_index)))
        self.texts_changed.emit(chapter_id)

    def prose_batch_merged(self, chapter_id: int) -> str:
        """全部批次按序合并成整章正文（空串 = 一批都还没写）。"""
        rows = self.list_prose_batches(chapter_id)
        return "\n\n".join((r.get("content") or "").strip()
                           for r in rows if (r.get("content") or "").strip())

    def prose_batch_state_package(self, chapter_id: int) -> str:
        """最新一批的滚动状态包（注入下一批；无批次或全空返回空串）。"""
        rows = self.list_prose_batches(chapter_id)
        for r in reversed(rows):
            sp = (r.get("state_package") or "").strip()
            if sp:
                return sp
        return ""

    def save_text_block_draft(self, block_id: int, content: str):
        """保存草稿（不产生快照，可反复改）。"""
        self._exec("UPDATE text_blocks SET content=?, updated_at=? WHERE id=?", (content, db.now(), block_id))

    def accept_text_block(self, block_id: int, content: str):
        """接受正文：旧版自动存快照，version+1，状态→已接受（事务：读旧版+存快照+推进版本原子）。

        幂等（2026-10-08 修复）：内容与当前版完全一致时不存快照、不升版本号——
        分批「合并批次写入正文」重复点、写作台重复点「接受」、自动模式重试循环
        重新合并落库，此前都会把一字未改的正文空升一版（卡片显示「第 2 版」）。
        """
        with self._transaction():
            old = self.conn.execute("SELECT * FROM text_blocks WHERE id=?", (block_id,)).fetchone()
            if old is None:
                return
            old = dict(old)
            if (old["content"] or "") == (content or ""):
                if old["status"] != "草稿":
                    return      # 内容没变且已接受/已锁定：幂等空操作，不发信号
                # 草稿内容未变：只把状态推进到已接受，不升版本、不存快照
                self.conn.execute(
                    "UPDATE text_blocks SET status='已接受', updated_at=? WHERE id=?",
                    (db.now(), block_id))
            else:
                if old["content"]:
                    self.conn.execute(
                        "INSERT INTO text_block_snapshots(text_block_id,version,content,created_at) VALUES(?,?,?,?)",
                        (block_id, old["version"], old["content"], db.now()))
                self.conn.execute(
                    "UPDATE text_blocks SET content=?, status='已接受', version=version+1, updated_at=? WHERE id=?",
                    (content, db.now(), block_id))
            chapter_id = old["chapter_id"]
        if chapter_id:
            # 事务外发信号：写作台据此刷新当前章编辑器（自动模式后台落库可见）
            self.texts_changed.emit(chapter_id)

    def lock_text_block(self, block_id: int, locked: bool):
        if locked:
            self._exec("UPDATE text_blocks SET status='已锁定', updated_at=? WHERE id=?",
                       (db.now(), block_id))
        else:
            # 只解锁已锁定的块（不会把草稿块误改成「已接受」）
            self._exec("UPDATE text_blocks SET status='已接受', updated_at=? WHERE id=? AND status='已锁定'",
                       (db.now(), block_id))

    def snapshot_history(self, block_id: int) -> list[dict]:
        """快照历史（旧→新）。"""
        return self._rows("SELECT * FROM text_block_snapshots WHERE text_block_id=? ORDER BY version", (block_id,))

    def rollback_text_block(self, block_id: int, snapshot_version: int):
        """回滚到指定快照版本（当前版先存快照）。"""
        snap = self._one("SELECT * FROM text_block_snapshots WHERE text_block_id=? AND version=?",
                         (block_id, snapshot_version))
        if not snap:
            return False
        self.accept_text_block(block_id, snap["content"])
        return True

    def delete_text_block(self, block_id: int):
        self._exec("DELETE FROM text_block_snapshots WHERE text_block_id=?", (block_id,))
        self._exec("DELETE FROM text_blocks WHERE id=?", (block_id,))

    def mark_style_sample(self, block_id: int, on: bool):
        self._exec("UPDATE text_blocks SET is_style_sample=? WHERE id=?", (1 if on else 0, block_id))

    def list_style_samples(self, chapter_id: int | None = None) -> list[dict]:
        if chapter_id is not None:
            return self._rows("SELECT * FROM text_blocks WHERE is_style_sample=1 AND chapter_id=?", (chapter_id,))
        return self._rows("SELECT * FROM text_blocks WHERE is_style_sample=1")

    # ---------- 本章动态 ----------

    def list_notes(self, chapter_id: int) -> list[dict]:
        return self._rows("SELECT * FROM chapter_notes WHERE chapter_id=? ORDER BY order_index", (chapter_id,))

    def add_note(self, chapter_id: int, content: str) -> dict:
        maxidx = self._one("SELECT COALESCE(MAX(order_index),0) AS m FROM chapter_notes WHERE chapter_id=?",
                           (chapter_id,))
        order = int(maxidx["m"]) + 1 if maxidx else 1
        created = db.now()
        cur = self._exec("INSERT INTO chapter_notes(chapter_id,order_index,content,created_at) VALUES(?,?,?,?)",
                         (chapter_id, order, content, created))
        return {"id": cur.lastrowid, "chapter_id": chapter_id, "order_index": order,
                "content": content, "created_at": created}

    def delete_note(self, note_id: int):
        """删除单条本章动态。"""
        self._exec("DELETE FROM chapter_notes WHERE id=?", (note_id,))

    def clear_notes(self, chapter_id: int):
        self._exec("DELETE FROM chapter_notes WHERE chapter_id=?", (chapter_id,))

    def set_scene_note(self, chapter_id: int, raw: str):
        """把正文剥离出的场景头写成一条「【场景】…」本章动态（替换式：先删旧条目）。

        场景头（『日期-星期-时间-地点-天气』）从 AI 正文剥离后交给记忆系统：
        分块写作时随「本章动态」注入后续块（接力）；归档提取时由 assembler 作为
        「本章开头场景」模块喂给记忆模型（world_state.time / location 推理参考）；
        归档完成后随本章动态一并清空。raw 为空时不动作（不动已有条目）。
        """
        content = (raw or "").strip()
        if not content:
            return
        for n in self.list_notes(chapter_id):
            if (n.get("content") or "").startswith("【场景】"):
                self.delete_note(n["id"])
        self.add_note(chapter_id, f"【场景】{content}")

    # ---------- 伏笔台账 ----------

    def foreshadow_ledger(self) -> dict:
        """伏笔台账：{"planted": [...], "paid_off": [...], "dropped": [...]}（项目设置键 JSON 存储）。

        planted 条目：{content, source, payoff_target, due_ch, reason, planted_ch}；
        paid_off 条目：{content, planted_in, reason}；
        dropped 条目：{content, chapter, reason}（剧情放弃/作废，2026-09-08 新增）。
        状态推导（open/expired/paid）见 foreshadow_states——由展示层/注入层调用。
        """
        raw = self.get_setting("foreshadow_ledger", "")
        try:
            obj = json.loads(raw) if raw else {}
        except Exception:
            obj = {}
        out = {"planted": [], "paid_off": [], "dropped": []}
        if isinstance(obj, dict):
            for k in ("planted", "paid_off", "dropped"):
                v = obj.get(k)
                if isinstance(v, list):
                    out[k] = [dict(x) for x in v
                              if isinstance(x, dict) and str(x.get("content") or "").strip()]
        return out

    def merge_foreshadow_ledger(self, planted: list | None, paid_off: list | None,
                                chapter_no: int | None = None,
                                dropped: list | None = None) -> dict:
        """归档差异并入台账（幂等）：planted 只增；paid_off/dropped 核销累积。

        2026-09-08 升级：①去重与核销改按归一化前缀键（_foreshadow_key）——
        模型照抄台账原文时改写尾段也能正确去重/配对，此前精确匹配导致
        「回收走样一条字，原条目永远未回收」；②支持 dropped（废弃）栏。
        返回 {"planted_added", "paid_added", "dropped_added"}。
        """
        led = self.foreshadow_ledger()
        known_plant = {_foreshadow_key(p.get("content")) for p in led["planted"]}
        known_paid = {_foreshadow_key(p.get("content")) for p in led["paid_off"]}
        known_drop = {_foreshadow_key(p.get("content")) for p in led["dropped"]}
        added_p = added_o = added_d = 0
        for p in (planted or []):
            c = str((p or {}).get("content") or "").strip()
            k = _foreshadow_key(c)
            if not k or k in known_plant:
                continue
            item = dict(p)
            if chapter_no is not None:
                item.setdefault("planted_ch", chapter_no)
            led["planted"].append(item)
            known_plant.add(k)
            added_p += 1
        for p in (paid_off or []):
            c = str((p or {}).get("content") or "").strip()
            k = _foreshadow_key(c)
            if not k or k in known_paid:
                continue
            led["paid_off"].append(dict(p))
            known_paid.add(k)
            added_o += 1
        for p in (dropped or []):
            c = str((p or {}).get("content") or "").strip()
            k = _foreshadow_key(c)
            if not k or k in known_drop:
                continue
            led["dropped"].append(dict(p))
            known_drop.add(k)
            added_d += 1
        if added_p or added_o or added_d:
            self.set_setting("foreshadow_ledger", jdump(led))
        return {"planted_added": added_p, "paid_added": added_o,
                "dropped_added": added_d}

    def foreshadow_states(self) -> dict:
        """伏笔台账状态推导（注入 _foreshadow_text 与记忆页管理板块共用）。

        每条 planted 推导为：
        - paid：content 与 paid_off 匹配（归一化前缀键 + difflib 相似度兜底，
          模型照抄走样也能核销）；
        - dropped：与 dropped 栏匹配（剧情放弃/作废）；
        - open：其余；其中预定回收章（due）已过且全书已写到该章之后的标
          expired（老板规则：过了回收规定时间还没回收就自动过期不管）。
        返回 {"open": [...], "expired": [...], "paid": [...], "dropped": [...]}，
        元素为台账原条目 + status 键；各组保持台账原序（注入「截最近」依赖顺序）。
        """
        led = self.foreshadow_ledger()
        cur = self.max_chapter_order()
        out = {"open": [], "expired": [], "paid": [], "dropped": []}
        # 第一轮：归一化前缀键配对
        paid_keys = {_foreshadow_key(p.get("content")): i for i, p in enumerate(led["paid_off"])}
        drop_keys = {_foreshadow_key(p.get("content")): i for i, p in enumerate(led["dropped"])}
        used_paid: set[int] = set()
        used_drop: set[int] = set()
        pending: list[tuple[int, dict, str]] = []   # (index, item, unresolved_key)
        for i, p in enumerate(led["planted"]):
            k = _foreshadow_key(p.get("content"))
            if k in paid_keys:
                used_paid.add(paid_keys[k])
                out["paid"].append({**p, "status": "paid"})
            elif k in drop_keys:
                used_drop.add(drop_keys[k])
                out["dropped"].append({**p, "status": "dropped"})
            else:
                pending.append((i, p, k))
        # 第二轮：模糊相似度兜底（照抄走样幅度大、前 12 字也对不上时）。
        # 只对足够长的内容启用（归一化后 ≥ 6 字）且阈值 0.6——短条目字形相近
        # 极易误配（「伏笔0」vs「旧伏笔X」ratio≈0.57），短伏笔走样请用右键
        # 「标记回收」手动核销。
        if pending and (used_paid or used_drop or paid_keys or drop_keys):
            from difflib import SequenceMatcher
            free_paid = [j for j in range(len(led["paid_off"])) if j not in used_paid]
            free_drop = [j for j in range(len(led["dropped"])) if j not in used_drop]
            still: list[tuple[int, dict, str]] = []
            for i, p, k in pending:
                kn = lore_norm(str(p.get("content") or ""))
                hit = False
                if kn and len(kn) >= 6:
                    for j in free_paid:
                        t = lore_norm(str(led["paid_off"][j].get("content") or ""))
                        if len(t) >= 6 and SequenceMatcher(None, kn, t).ratio() >= 0.6:
                            used_paid.add(j)
                            free_paid.remove(j)
                            out["paid"].append({**p, "status": "paid"})
                            hit = True
                            break
                    if not hit:
                        for j in free_drop:
                            t = lore_norm(str(led["dropped"][j].get("content") or ""))
                            if len(t) >= 6 and SequenceMatcher(None, kn, t).ratio() >= 0.6:
                                used_drop.add(j)
                                free_drop.remove(j)
                                out["dropped"].append({**p, "status": "dropped"})
                                hit = True
                                break
                if not hit:
                    still.append((i, p, k))
            pending = still
        for _i, p, _k in pending:
            due = _parse_due_ch(p)
            if due and cur and cur > due:
                out["expired"].append({**p, "status": "expired", "due_ch": due})
            else:
                out["open"].append({**p, "status": "open"})
        # 孤儿核销条目（planted 已被手动删除，但 paid_off/dropped 记录还在）：
        # 同样属于「已回收/已废弃」事实，不能丢（否则注入段与 UI 少显示）
        for j, p in enumerate(led["paid_off"]):
            if j not in used_paid:
                out["paid"].append({**p, "status": "paid"})
        for j, p in enumerate(led["dropped"]):
            if j not in used_drop:
                out["dropped"].append({**p, "status": "dropped"})
        return out

    def foreshadow_mark_paid(self, content: str, chapter_no: int | None = None) -> bool:
        """手动把一条未回收伏笔标记为已回收（核销入 paid_off，planted 原条目保留）。

        content 传台账原文（或能前缀匹配上的开头），核销条目 content 用
        planted 原文——保证状态推导的键匹配。"""
        led = self.foreshadow_ledger()
        k = _foreshadow_key(content)
        src = next((p for p in led["planted"] if _foreshadow_key(p.get("content")) == k), None)
        if not src:
            return False
        if any(_foreshadow_key(p.get("content")) == k for p in led["paid_off"]):
            return True     # 已核销过，幂等
        paid = {"content": src["content"],
                "planted_in": f"第{src['planted_ch']}章" if src.get("planted_ch") else "",
                "reason": "手动标记回收"}
        if chapter_no:
            paid["paid_ch"] = chapter_no
        led["paid_off"].append(paid)
        self.set_setting("foreshadow_ledger", jdump(led))
        return True

    def foreshadow_drop(self, content: str, chapter_no: int | None = None,
                        reason: str = "手动废弃") -> bool:
        """手动废弃一条伏笔（剧情已放弃/不打算回收）：核销入 dropped 栏。"""
        led = self.foreshadow_ledger()
        k = _foreshadow_key(content)
        src = next((p for p in led["planted"] if _foreshadow_key(p.get("content")) == k), None)
        if not src:
            return False
        if any(_foreshadow_key(p.get("content")) == k for p in led["dropped"]):
            return True     # 已废弃过，幂等
        led["dropped"].append({"content": src["content"],
                               "chapter": chapter_no or "",
                               "reason": reason})
        self.set_setting("foreshadow_ledger", jdump(led))
        return True

    def foreshadow_update_target(self, content: str, target: str) -> bool:
        """修改一条伏笔的预定回收章（延期/提前）。target 支持「第N章」或纯数字。"""
        led = self.foreshadow_ledger()
        k = _foreshadow_key(content)
        src = next((p for p in led["planted"] if _foreshadow_key(p.get("content")) == k), None)
        if not src:
            return False
        src["payoff_target"] = str(target or "").strip()
        src["due_ch"] = _parse_due_ch({"payoff_target": src["payoff_target"]})
        self.set_setting("foreshadow_ledger", jdump(led))
        return True

    def remove_foreshadow_entry(self, kind: str, content: str):
        """手动删除台账条目（误记清理）。kind: planted / paid_off / dropped。"""
        led = self.foreshadow_ledger()
        if kind not in led:
            return
        led[kind] = [p for p in led[kind] if str(p.get("content")) != content]
        self.set_setting("foreshadow_ledger", jdump(led))

    # ---------- 记忆链 ----------

    def memory_chain(self) -> list[dict]:
        """全链快照（含 hidden/过期标记），按版本号升序。"""
        return self._rows("SELECT * FROM memory_snapshots ORDER BY version")

    def memory_snapshot(self, version: int) -> dict | None:
        return self._one("SELECT * FROM memory_snapshots WHERE version=?", (version,))

    def memory_snapshot_for_chapter(self, chapter_id: int) -> dict | None:
        """该章最近一次有效记忆快照（含解析好的 full_state/events）；无则 None。

        右栏归档事件展示 / 历史章出场人物回填共用。
        """
        r = self._one("SELECT * FROM memory_snapshots WHERE chapter_id=?"
                      " AND stale=0 ORDER BY version DESC LIMIT 1", (chapter_id,))
        if not r:
            return None
        return {**r,
                "full_state": jload(r["full_state"], empty_memory_state()),
                "events": jload(r["events"], [])}

    def latest_memory_snapshot_row(self, chapter_id: int) -> dict | None:
        """该章最新快照行（**含过期**，重归档空差异路径用——过期章也要能沿用原快照）。"""
        r = self._one("SELECT * FROM memory_snapshots WHERE chapter_id=?"
                      " ORDER BY version DESC LIMIT 1", (chapter_id,))
        if not r:
            return None
        return {**r, "full_state": jload(r["full_state"], None),
                "events": jload(r["events"], [])}

    def current_memory(self) -> dict:
        """当前有效状态 = 最新 未过期且未隐藏 的快照。"""
        r = self._one("SELECT full_state FROM memory_snapshots WHERE stale=0 ORDER BY version DESC LIMIT 1")
        return jload(r["full_state"], empty_memory_state()) if r else empty_memory_state()

    def character_appearances(self) -> dict[str, int]:
        """人物名 → 最近一次出场的章序（从未出场的人物不在结果里）。

        出场依据两路并取、取最大章序：
        - 各章细纲块的「涉及人物」名单（scene_blocks.characters）；
        - 归档差异中被记录了变化/新建状态卡的人物（memory_snapshots.diff 的
          items[].name）——正文写到了谁，记忆提取就会动谁的状态。
        不能用 chapters.characters 判断"最近"：归档时会把记忆里的全部人物并进
        该名单（merge_chapter_characters），它只增不减、没有时间含义。
        每次注入提示词都会调用，两条 JOIN 一次取完，不做逐章查询。
        """
        last: dict[str, int] = {}
        rows = self._rows(
            "SELECT c.order_index AS ord, b.characters AS chars"
            " FROM scene_blocks b JOIN chapters c ON c.id = b.chapter_id")
        for r in rows:
            order = r["ord"] or 0
            if not order:
                continue
            for n in jload(r["chars"], []):
                n = str(n).strip()
                if n:
                    last[n] = max(last.get(n, 0), order)
        rows = self._rows(
            "SELECT c.order_index AS ord, s.diff AS diff"
            " FROM memory_snapshots s JOIN chapters c ON c.id = s.chapter_id"
            " WHERE s.stale = 0")
        for r in rows:
            order = r["ord"] or 0
            if not order:
                continue
            for it in (jload(r["diff"], {}).get("items") or []):
                n = str(it.get("name") or "").strip()
                if n:
                    last[n] = max(last.get(n, 0), order)
        return last

    def entity_appearances(self) -> dict[str, int]:
        """实体名 → 最近一次状态发生变化的章序（依据归档差异，未过期快照）。

        实体只在「状态变了」时才进差异（易主/位移/损毁/removed…），所以
        "很久没进差异" ≈ "剧情很久没碰它"——自动隐藏沉睡实体的依据。
        """
        last: dict[str, int] = {}
        rows = self._rows(
            "SELECT c.order_index AS ord, s.diff AS diff"
            " FROM memory_snapshots s JOIN chapters c ON c.id = s.chapter_id"
            " WHERE s.stale = 0")
        for r in rows:
            order = r["ord"] or 0
            if not order:
                continue
            for it in (jload(r["diff"], {}).get("items") or []):
                if it.get("kind") not in ("entity", "entity_remove"):
                    continue
                n = str(it.get("name") or "").strip()
                if n:
                    last[n] = max(last.get(n, 0), order)
        return last

    def memory_state_before_chapter(self, chapter_id: int) -> dict:
        """该章归档之前的记忆状态（重归档基准）。

        重归档（正文重写后再归档）必须以「本章之前」的状态为基准重新应用差异：
        若沿用 current_memory（已含本章上一次归档的结果），本章变化会被叠加两次
        （伤势/物品/知识越归档越多）。取版本号小于本章最新快照、且不属于本章的
        最近一条未过期快照；该章尚无快照（首次归档）时退回当前有效状态。
        """
        r = self._one("SELECT version FROM memory_snapshots WHERE chapter_id=?"
                      " ORDER BY version DESC LIMIT 1", (chapter_id,))
        if not r:
            return self.current_memory()
        prev = self._one(
            "SELECT full_state FROM memory_snapshots WHERE version<? AND stale=0"
            " AND (chapter_id IS NULL OR chapter_id<>?) ORDER BY version DESC LIMIT 1",
            (r["version"], chapter_id))
        return jload(prev["full_state"], empty_memory_state()) if prev else empty_memory_state()

    def has_memory_snapshot(self, chapter_id: int) -> bool:
        """该章是否已归档过（存在记忆快照，含已过期的）——决定归档走新增还是重归档。"""
        return bool(self._one("SELECT 1 FROM memory_snapshots WHERE chapter_id=? LIMIT 1",
                              (chapter_id,)))

    def chapter_prose_newer_than_snapshot(self, chapter_id: int) -> bool:
        """本章最新已接受正文是否晚于该章最新记忆快照。

        正文重写/手动编辑后未归档时返回 True（自动模式据此补一次归档）；
        该章无快照时返回 False（由 has_memory_snapshot 分支负责）。
        时间戳格式统一（db.now()），字符串比较即时间比较。
        """
        r = self._one("SELECT created_at FROM memory_snapshots WHERE chapter_id=?"
                      " ORDER BY version DESC LIMIT 1", (chapter_id,))
        if not r:
            return False
        row = self._one(
            "SELECT MAX(updated_at) AS t FROM text_blocks WHERE chapter_id=?"
            " AND status IN ('已接受','已锁定')", (chapter_id,))
        t = (row or {}).get("t")
        return bool(t and r["created_at"] and t > r["created_at"])

    def archive_chapter(self, chapter_id: int, full_state: dict, diff: dict | None,
                        summary: str, events: list) -> int:
        """章末归档：写入新版本快照并推进链。返回新版本号。

        版本号用 INSERT...SELECT 原子计算（MAX+1 与插入同语句），
        并发归档/手动编辑不会算出重复版本。
        """
        with self._transaction():
            cur = self.conn.execute(
                "INSERT INTO memory_snapshots(version,chapter_id,full_state,diff,summary,events,stale,created_at)"
                " SELECT COALESCE(MAX(version),0)+1,?,?,?,?,?,0,? FROM memory_snapshots",
                (chapter_id, jdump(full_state), jdump(diff) if diff else None,
                 summary, jdump(events), db.now()))
            self.conn.execute("UPDATE chapters SET status='已归档', updated_at=? WHERE id=?",
                              (db.now(), chapter_id))
            row = self.conn.execute("SELECT version FROM memory_snapshots WHERE id=?",
                                    (cur.lastrowid,)).fetchone()
        return int(row["version"]) if row else 1

    def replace_snapshot(self, chapter_id: int, full_state: dict, diff: dict | None,
                         summary: str, events: list) -> int:
        """重归档（中间章改写后）：替换该章快照，返回版本号。"""
        r = self._one("SELECT id, version FROM memory_snapshots WHERE chapter_id=?"
                      " ORDER BY version DESC LIMIT 1", (chapter_id,))
        if r:
            self._exec("UPDATE memory_snapshots SET full_state=?, diff=?, summary=?, events=?, stale=0, created_at=? WHERE id=?",
                       (jdump(full_state), jdump(diff) if diff else None, summary, jdump(events), db.now(), r["id"]))
            return int(r["version"])
        return self.archive_chapter(chapter_id, full_state, diff, summary, events)

    def set_current_memory(self, full_state: dict, summary: str = "手动编辑",
                           events: list | None = None):
        """手动覆盖当前记忆：保存为最新版本快照（不走差异提取），供
        「编辑当前状态」「从角色卡生成 v0」等手动路径使用。返回版本号。

        版本号同样用 INSERT...SELECT 原子计算，与归档/手动编辑并发不冲突。
        """
        full_state = sync_dead_flags(full_state or {})   # 手动编辑也要补已故标记
        with self._transaction():
            diff = {"kind": "manual_edit"}
            cur = self.conn.execute(
                "INSERT INTO memory_snapshots(version,chapter_id,full_state,diff,summary,events,stale,created_at)"
                " SELECT COALESCE(MAX(version),0)+1,NULL,?,?,?,?,0,? FROM memory_snapshots",
                (jdump(full_state), jdump(diff), summary or "手动编辑",
                 jdump(events or []), db.now()))
            row = self.conn.execute("SELECT version FROM memory_snapshots WHERE id=?",
                                    (cur.lastrowid,)).fetchone()
        return int(row["version"]) if row else 1

    def rollback_memory(self, version: int):
        """回滚到 vK：K 之后所有快照标记过期（红色），当前状态回到 vK。"""
        self._exec("UPDATE memory_snapshots SET stale=1 WHERE version>?", (version,))
        self._exec("UPDATE memory_snapshots SET stale=0 WHERE version<=?", (version,))

    def delete_memory_snapshot(self, version: int):
        """删除单条记忆快照（连带其向量）。

        - full_state 均为完整状态、互不依赖：删中间条不影响其余快照内容；
        - 删最新有效条 = current_memory 自动回落到上一条（等效回滚）；
        - 该章因此无任何快照且状态为「已归档」→ 章状态降回「已完成」，
          自动归档幂等逻辑即可重新归档该章；
        - 版本号留洞不重排（memory_vectors 按 version 关联，重排会断链）。
        破坏性操作，调用方必须二次确认。
        """
        r = self._one("SELECT chapter_id FROM memory_snapshots WHERE version=?", (version,))
        with self._lock:
            self.conn.execute("DELETE FROM memory_snapshots WHERE version=?", (version,))
            self.conn.execute("DELETE FROM memory_vectors WHERE snapshot_version=?", (version,))
            self.conn.commit()
        if r and r["chapter_id"]:
            left = self._one("SELECT COUNT(*) AS n FROM memory_snapshots WHERE chapter_id=?",
                             (r["chapter_id"],))
            if not left or not int(left["n"] or 0):
                ch = self.get_chapter(r["chapter_id"])
                if ch and ch.get("status") == "已归档":
                    self.set_chapter_status(r["chapter_id"], "已完成")

    def reset_memory(self):
        """重置记忆链：删除全部快照与向量，重新生成空 v0（记忆回到初始状态）。

        已归档章节的「已归档」状态不变，但下次归档将从空状态开始累积。
        破坏性操作，调用方必须二次确认。
        """
        with self._lock:
            self.conn.execute("DELETE FROM memory_vectors")
            self.conn.execute("DELETE FROM memory_snapshots")
            self.conn.commit()
        created = db.now()
        self._exec("INSERT INTO memory_snapshots(version,chapter_id,full_state,diff,summary,events,stale,created_at)"
                   " VALUES(0,NULL,?,NULL,'初始状态','[]',0,?)",
                   (jdump(empty_memory_state()), created))

    def mark_stale_from(self, chapter_id: int):
        """某章及其后所有已归档快照标记过期（中间章删除/改写时）。"""
        r = self._one("SELECT version FROM memory_snapshots WHERE chapter_id=?"
                      " ORDER BY version DESC LIMIT 1", (chapter_id,))
        if r:
            self._exec("UPDATE memory_snapshots SET stale=1 WHERE version>=?", (r["version"],))

    def snapshot_chapters_to_rearchive(self, chapter_id: int) -> list[dict]:
        """返回自该章起（含）需要重归档的章节列表。"""
        r = self._one("SELECT version FROM memory_snapshots WHERE chapter_id=?"
                      " ORDER BY version DESC LIMIT 1", (chapter_id,))
        if not r:
            return []
        return self._rows(
            "SELECT c.* FROM chapters c JOIN memory_snapshots s ON s.chapter_id=c.id"
            " WHERE s.version>=? AND c.deleted=0 AND c.status='已归档' ORDER BY c.order_index",
            (r["version"],))

    def has_rearchivable_after(self, chapter_id: int) -> bool:
        """过期快照之后是否还有「存活且已归档」的章节需要重归档。

        尾部删除/纯回滚到链尾 → False（记忆已自动回退，无需重归档，隐藏警告条）；
        中间删除/回滚后仍有后续存活章 → True（需要批量重归档）。
        """
        return bool(self.snapshot_chapters_to_rearchive(chapter_id))

    # ---------- 记忆向量（语义检索索引） ----------

    def save_memory_vectors(self, snapshot_version: int, units: list[dict]):
        """保存某版本快照的记忆向量单元（先删旧再插，幂等）。

        units: [{"unit_key", "text", "vector"(list[float])}]
        """
        with self._lock:
            self.conn.execute("DELETE FROM memory_vectors WHERE snapshot_version=?",
                              (snapshot_version,))
            for u in units:
                text = (u.get("text") or "").strip()
                vec = u.get("vector")
                if not text or not isinstance(vec, list) or not vec:
                    continue
                self.conn.execute(
                    "INSERT INTO memory_vectors(snapshot_version,unit_key,text,vector,created_at)"
                    " VALUES(?,?,?,?,?)",
                    (snapshot_version, str(u.get("unit_key") or "")[:64], text,
                     jdump(vec), db.now()))
            self.conn.commit()

    def delete_memory_vectors(self, snapshot_version: int):
        """删除某版本快照的向量（回滚/重归档时随快照清理）。"""
        self._exec("DELETE FROM memory_vectors WHERE snapshot_version=?",
                   (snapshot_version,))

    def all_memory_vectors(self) -> list[dict]:
        """全部记忆向量单元（语义检索用），仅含「未过期」快照的向量。

        回滚 / 中间章删除（mark_stale_from）只把快照标记过期、不删向量：
        若不按 stale 过滤，被回滚/删除章节的旧记忆仍会被语义检索召回，
        当成「相关记忆」注入正文（幽灵记忆）。与 current_memory 的 stale 口径一致。
        """
        rows = self._rows(
            "SELECT v.snapshot_version, v.unit_key, v.text, v.vector"
            " FROM memory_vectors v JOIN memory_snapshots s ON s.version=v.snapshot_version"
            " WHERE s.stale=0 ORDER BY v.id")
        out = []
        for r in rows:
            vec = jload(r["vector"], None)
            if isinstance(vec, list) and vec:
                out.append({"snapshot_version": r["snapshot_version"],
                            "unit_key": r["unit_key"], "text": r["text"],
                            "vector": vec})
        return out

    def memory_vector_count(self) -> int:
        r = self._one("SELECT COUNT(*) AS n FROM memory_vectors")
        return int(r["n"]) if r else 0

    def rebuild_memory_vectors(self, embed_fn) -> int:
        """重建全部未过期快照的记忆向量（记忆页「重建语义向量」用）。

        embed_fn(base_url, api_key, model, texts) -> vecs|None，由调用方注入
        embedding.embed_texts（core 层不直接 import embedding，保持解耦可测试）。
        返回重建的快照数量；未配置嵌入时抛 RuntimeError（UI 层转提示）。
        """
        from .embedding import load_embed_config  # 局部导入避免循环依赖
        from .memory_service import memory_units  # 局部导入（memory_service 反向依赖本模块）
        embed = load_embed_config(self)
        if not embed:
            raise RuntimeError("尚未配置嵌入（向量）模型，请先到「设置 → 模型与嵌入」配置")
        rebuilt = 0
        for s in self.memory_chain():
            if s.get("stale"):
                continue
            full_state = jload(s["full_state"], None)
            if not isinstance(full_state, dict):
                continue
            snap = self.memory_snapshot(s["version"]) or {}
            try:
                events = jload(snap.get("events"), [])
            except Exception:
                events = []
            units = memory_units(full_state, events)
            if not units:
                continue
            texts = [u["text"] for u in units]
            vecs = embed_fn(embed["base_url"], embed["api_key"], embed["model"], texts)
            if not vecs or len(vecs) != len(texts):
                continue  # 该版本失败跳过，不影响其他版本
            for u, v in zip(units, vecs):
                u["vector"] = v
            self.save_memory_vectors(s["version"], units)
            rebuilt += 1
        return rebuilt

    # ---------- 设定资料 ----------

    def list_lore(self, category: str | None = None) -> list[dict]:
        if category:
            rows = self._rows("SELECT * FROM lore_entries WHERE category=? ORDER BY name", (category,))
        else:
            rows = self._rows("SELECT * FROM lore_entries ORDER BY category, name")
        for r in rows:
            r["tags"] = jload(r["tags"], [])
        return rows

    def add_lore(self, category: str, name: str, content: str = "",
                 tags: list | None = None, locked: bool = False) -> dict:
        created = db.now()
        # 沉睡基准：已有章节 = 当前最大章序（从录入起计时）；还没有章节 = NULL
        #（未建立基准，首次归档时由 touch_lore_mentions 统一建立——「第 0 章」
        # 不存在，0 会被当成「已见过」而提前进入沉睡计时，语义不对）
        max_order = self.max_chapter_order()
        cur = self._exec(
            "INSERT INTO lore_entries(category,name,content,tags,locked,created_at,updated_at,"
            "last_seen_chapter) VALUES(?,?,?,?,?,?,?,?)",
            (category, name, content, jdump(tags or []), 1 if locked else 0, created, created,
             max_order if max_order > 0 else None))
        return self._one("SELECT * FROM lore_entries WHERE id=?", (cur.lastrowid,))

    def update_lore(self, entry_id: int, patch: dict):
        sets, params = [], []
        for key in ("category", "name", "content"):
            if key in patch:
                sets.append(f"{key}=?")
                params.append(patch[key])
        if "tags" in patch:
            sets.append("tags=?")
            params.append(jdump(patch["tags"]))
        if "locked" in patch:
            sets.append("locked=?")
            params.append(1 if patch["locked"] else 0)
        if sets:
            sets.append("updated_at=?")
            params.append(db.now())
            params.append(entry_id)
            self._exec(f"UPDATE lore_entries SET {', '.join(sets)} WHERE id=?", params)

    def delete_lore(self, entry_id: int):
        """物理删除（用户手动操作，不可恢复）。"""
        self._exec("DELETE FROM lore_entries WHERE id=?", (entry_id,))

    def mark_lore_removed(self, names, chapter: int) -> int:
        """把设定库同名条目标记为「已移除」（软删除，2026-09-08 第七轮）。

        归档时模型判定某实体「已消失/被消耗/被销毁」且用户采纳后调用。背景：
        实体从记忆世界状态移除后，设定库同名条目若继续注入，会出现「剑已断、
        提示词里剑还在」的自相矛盾——更糟的是销毁那一章正文反复点名它，
        touch_lore_mentions 会把 last_seen 刷成当前章，沉睡机制反而让它更活跃。

        软移除而非物理删除的三个理由：① 自动归档不经确认界面，模型误判可撤销；
        ② 条目可能含用户手写设定，删了就没了；③ 重归档/回档时能恢复。
        锁定条目不标记（用户显式钉住 = 明确要它一直在）。只标记非「角色」分类
        ——实体移除来自世界状态，角色卡按出场匹配且有独立的死亡标记通道。
        返回实际标记条数。
        """
        if not names or not chapter:
            return 0
        want = {str(n).strip() for n in names if str(n).strip()}
        if not want:
            return 0
        n = 0
        for e in self.list_lore():
            if e.get("category") == "角色" or e.get("locked"):
                continue
            if e.get("removed_at_chapter"):
                continue
            if str(e.get("name") or "").strip() not in want:
                continue
            self._exec("UPDATE lore_entries SET removed_at_chapter=?, updated_at=? WHERE id=?",
                       (chapter, db.now(), e["id"]))
            n += 1
        return n

    def unmark_lore_removed(self, names) -> int:
        """已移除条目若重新出现在记忆世界状态里，自动取消标记（实体「复活」）。

        与 mark_lore_removed 对称：只标记不撤销的话，一个被判定销毁、后来剧情
        又让它回来（修复/重现/同名新物件）的实体，会因为设定条目仍带标记而
        永远注入不了——而自动导入又认为「同名已存在」不会重建条目，形成死角。
        返回实际恢复条数。
        """
        want = {str(n).strip() for n in names if str(n).strip()}
        if not want:
            return 0
        n = 0
        for e in self.list_lore():
            if e.get("category") == "角色" or not e.get("removed_at_chapter"):
                continue
            if str(e.get("name") or "").strip() in want:
                self._exec("UPDATE lore_entries SET removed_at_chapter=NULL, updated_at=? WHERE id=?",
                           (db.now(), e["id"]))
                n += 1
        return n

    def restore_lore(self, entry_id: int):
        """取消「已移除」标记（软删除的撤销），条目重新参与注入。"""
        self._exec("UPDATE lore_entries SET removed_at_chapter=NULL, updated_at=? WHERE id=?",
                   (db.now(), entry_id))

    def max_chapter_order(self) -> int:
        """全书最大章序（含未归档章，回收站不计）；无章节返回 0。"""
        try:
            return max((c.get("order_index") or 0 for c in self.list_chapters()), default=0)
        except Exception:
            return 0

    def touch_lore_mentions(self, chapter_id: int):
        """归档时更新设定库条目的「最近被提及章号」（设定库沉睡基准，2026-09-08）。

        判定口径与注入侧（assembler._lore_text）共用 lore_context_hit：
        - 本章细纲 + 已接受正文中点名（名字/标签/前4字归一化命中）的非角色条目
          → last_seen = 本章章号；
        - 尚无基准（NULL，存量条目/升级前录入）的条目无论是否命中都记本章章号
          ——从首次归档起建立基准，之后「连续 N 章未被提及」才开始沉睡
          （保守：升级后存量条目不会集体消失）。
        角色条目不参与沉睡（按出场匹配天然有界）。任何异常不阻塞归档。
        """
        try:
            order = (self.get_chapter(chapter_id) or {}).get("order_index") or 0
            if not order:
                return
            parts: list[str] = []
            for b in self.list_blocks(chapter_id):
                for k in ("content", "event_title", "block_type", "scene_direction",
                          "shot_script", "must_have", "narrative_task",
                          "sensory_focus", "to_state"):
                    v = str(b.get(k) or "").strip()
                    if v:
                        parts.append(v)
                for c in (b.get("characters") or []):
                    if str(c).strip():
                        parts.append(str(c))
            accepted = [str(t.get("content") or "").strip()
                        for t in self.list_text_blocks(chapter_id)
                        if t.get("status") in ("已接受", "已锁定")
                        and str(t.get("content") or "").strip()]
            if accepted:
                parts.append("\n".join(accepted)[-3000:])
            hay = lore_norm("\n".join(parts))
            for e in self.list_lore():
                if e.get("category") == "角色":
                    continue
                if e.get("removed_at_chapter"):
                    continue  # 已判定消失：不再刷新基准（否则销毁章的正文点名会把它「复活」）
                seen = e.get("last_seen_chapter")
                if seen is None:
                    self._exec("UPDATE lore_entries SET last_seen_chapter=? WHERE id=?",
                               (order, e["id"]))
                elif seen != order and lore_context_hit(str(e.get("name") or ""),
                                                        e.get("tags") or [], hay):
                    self._exec("UPDATE lore_entries SET last_seen_chapter=? WHERE id=?",
                               (order, e["id"]))
        except Exception as e:
            # 归档 best-effort：失败不阻塞归档流程，但要留痕——否则沉睡基准
            # 静默失准，设定库沉睡/唤醒会莫名出错且无从排查
            print(f"[store] touch_lore_mentions 失败（章 order={order}）: "
                  f"{type(e).__name__}: {e}")

    def lore_for_characters(self, names: list[str]) -> list[dict]:
        """按出场人物相关性取角色卡 + 全部地点/物品/术语（设定库不全量塞，但地点物品术语体量小）。

        已移除条目（removed_at_chapter 非空）在此过滤掉——注入侧唯一入口，
        保证「记忆里已消失的东西不再进提示词」；角色卡不受影响（人物死亡有
        独立的 dead 通道，其设定卡对回忆/旁白仍有价值）。
        """
        rows = []
        for r in self.list_lore("角色"):
            if r["name"] in names:
                rows.append(r)
        for cat in ("地点", "物品", "术语"):
            rows.extend([r for r in self.list_lore(cat) if not r.get("removed_at_chapter")])
        return rows

    # ---------- 偏好卡片（全局） ----------
    # （2026-09-08 修复记录：下方方法全部属于 146 行起的 ProjectStore 类体。
    #  曾因在类中间插入模块级函数导致类体在这里被截断，list_cards 之后的方法
    #  全部脱离类，运行期才暴露 AttributeError——store.close/add_log 等消失。
    #  此处必须保持类内缩进；新增模块级函数请放在文件末尾，勿插进类体。）

    def list_cards(self, scope: str | None = None, stage: str | None = None) -> list[dict]:
        return self.gstore.list_cards(scope, stage)

    def active_cards(self, scope: str | None = None, stage: str | None = None) -> list[dict]:
        return self.gstore.active_cards(scope, stage)

    def add_card(self, title: str, content: str, scope: str = "global", stage: str = "",
                 priority: int = 5, enabled: bool = True, note: str = "", stages: str = "",
                 pinned: bool = False) -> dict:
        return self.gstore.add_card(title, content, scope, stage, priority, enabled,
                                    note, stages, pinned)

    def update_card(self, card_id: int, patch: dict):
        return self.gstore.update_card(card_id, patch)

    def delete_card(self, card_id: int):
        return self.gstore.delete_card(card_id)

    # ---------- 预设（全局） ----------

    def list_presets(self) -> list[dict]:
        return self.gstore.list_presets()

    def get_preset(self, preset_id: int) -> dict | None:
        return self.gstore.get_preset(preset_id)

    def save_preset(self, name: str, data: dict, preset_id: int | None = None) -> int:
        return self.gstore.save_preset(name, data, preset_id)

    def delete_preset(self, preset_id: int):
        return self.gstore.delete_preset(preset_id)

    # ---------- 模型档案（全局） ----------

    def list_profiles(self) -> list[dict]:
        return self.gstore.list_profiles()

    def get_profile(self, profile_id: int) -> dict | None:
        return self.gstore.get_profile(profile_id)

    def save_profile(self, data: dict, profile_id: int | None = None) -> int:
        return self.gstore.save_profile(data, profile_id)

    def delete_profile(self, profile_id: int):
        """删除档案；绑定的路由解除绑定。"""
        return self.gstore.delete_profile(profile_id)

    # ---------- 任务角色（全局） ----------

    def list_routes(self) -> list[dict]:
        """角色路由 + 绑定档案信息。"""
        return self.gstore.list_routes()

    def get_route(self, role: str) -> dict | None:
        return self.gstore.get_route(role)

    def save_route(self, role: str, profile_id: int | None, temperature: float,
                   system_prompt: str | None = None, name: str | None = None,
                   locked: int | None = None):
        return self.gstore.save_route(role, profile_id, temperature, system_prompt, name, locked)

    def set_route_locked(self, role: str, locked: bool):
        """锁定/解锁任务角色：一键绑定全部角色时不更换该角色档案。"""
        return self.gstore.set_route_locked(role, locked)

    def add_role(self, role: str, name: str, temperature: float = 0.7):
        return self.gstore.add_role(role, name, temperature)

    def delete_role(self, role: str):
        return self.gstore.delete_role(role)

    # ---------- 渠道故障自动切换（备用渠道链，全局；2026-09-18） ----------

    def channel_failover_enabled(self) -> bool:
        return self.gstore.channel_failover_enabled()

    def set_channel_failover_enabled(self, enabled: bool):
        self.gstore.set_channel_failover_enabled(enabled)

    def get_fallback_profile_ids(self, role: str) -> list[int]:
        """某角色的备用渠道顺序表（空 = 不自动切换）。"""
        return self.gstore.get_fallback_profile_ids(role)

    def set_fallback_profile_ids(self, role: str, ids) -> list[int]:
        return self.gstore.set_fallback_profile_ids(role, ids)

    def fallback_channels(self) -> dict:
        return self.gstore.fallback_channels()

    # ---------- 请求日志 ----------

    def add_log(self, role: str, model: str, prompt: str, response: str,
                prompt_tokens: int, completion_tokens: int, cost: float,
                status: str = "ok", error: str = "", duration_ms: int = 0) -> int:
        cur = self._exec(
            "INSERT INTO request_logs(time,role,model,prompt,response,prompt_tokens,completion_tokens,"
            " cost,status,error,duration_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (db.now(), role, model, prompt, response, prompt_tokens, completion_tokens,
             cost, status, error, duration_ms))
        self._prune_logs()
        return cur.lastrowid

    # 每插入 LOG_PRUNE_EVERY 条才清理一次：DELETE + 子查询有成本，不必每次请求都做
    LOG_PRUNE_EVERY = 50

    def _prune_logs(self):
        """按保留条数裁剪最旧的日志（尽力而为，失败不影响主流程）。

        请求日志每条都含完整 prompt 与 response，长篇跑自动模式会有几千条、每条几万字，
        project.db 会膨胀到数百 MB，拖慢自动备份与全量同步。默认保留最近 500 条。
        """
        try:
            keep = self.get_setting_int("log_retention", 500)
        except Exception:
            return
        if keep <= 0:      # 0 = 不自动清理（用户想全量留痕）
            return
        try:
            row = self._one("SELECT COUNT(*) AS n FROM request_logs")
            total = int(row["n"]) if row else 0
        except Exception:
            return
        if total <= keep or total % self.LOG_PRUNE_EVERY != 0:
            return
        try:
            self._exec("DELETE FROM request_logs WHERE id NOT IN ("
                       "SELECT id FROM request_logs ORDER BY id DESC LIMIT ?)", (keep,))
        except Exception:
            pass

    def list_logs(self, limit: int = 200, role: str | None = None) -> list[dict]:
        if role:
            return self._rows("SELECT * FROM request_logs WHERE role=? ORDER BY id DESC LIMIT ?", (role, limit))
        return self._rows("SELECT * FROM request_logs ORDER BY id DESC LIMIT ?", (limit,))

    def get_log(self, log_id: int) -> dict | None:
        return self._one("SELECT * FROM request_logs WHERE id=?", (log_id,))

    def clear_logs(self):
        self._exec("DELETE FROM request_logs")

    def cost_stats(self) -> dict:
        """本月/总计费用统计。"""
        month = db.now()[:7]
        total = self._one("SELECT COALESCE(SUM(cost),0) AS c FROM request_logs")
        mon = self._one("SELECT COALESCE(SUM(cost),0) AS c, COUNT(*) AS n FROM request_logs WHERE time LIKE ?",
                        (month + "%",))
        return {"total": round(total["c"], 4) if total else 0.0,
                "month": round(mon["c"], 4) if mon else 0.0,
                "month_count": mon["n"] if mon else 0}

    # ---------- 锁定项 ----------

    def list_locks(self) -> list[dict]:
        return self._rows("SELECT * FROM locked_items ORDER BY item_type, id")

    def add_lock(self, item_type: str, value: str, note: str = "") -> dict:
        cur = self._exec("INSERT INTO locked_items(item_type,value,note,created_at) VALUES(?,?,?,?)",
                         (item_type, value, note, db.now()))
        return self._one("SELECT * FROM locked_items WHERE id=?", (cur.lastrowid,))

    def remove_lock(self, lock_id: int):
        self._exec("DELETE FROM locked_items WHERE id=?", (lock_id,))

    def locks_text(self) -> str:
        """锁定项注入文本。"""
        locks = self.list_locks()
        if not locks:
            return ""
        lines = ["【锁定设定（不可更改）】"]
        for l in locks:
            lines.append(f"- {l['item_type']}：{l['value']}" + (f"（{l['note']}）" if l["note"] else ""))
        return "\n".join(lines)

    # ---------- 误报 ----------

    def list_false_positives(self) -> list[str]:
        return [r["text"] for r in self._rows("SELECT text FROM false_positives")]

    def add_false_positive(self, text: str):
        self._exec("INSERT INTO false_positives(text,created_at) VALUES(?,?)", (text, db.now()))

    def clear_false_positives(self):
        self._exec("DELETE FROM false_positives")

    # ---------- 检查记录 ----------

    def save_check(self, chapter_id: int | None, issues: list) -> int:
        cur = self._exec("INSERT INTO check_results(chapter_id,time,issues) VALUES(?,?,?)",
                         (chapter_id, db.now(), jdump(issues)))
        return cur.lastrowid

    def list_checks(self, limit: int = 50) -> list[dict]:
        return self._rows("SELECT * FROM check_results ORDER BY id DESC LIMIT ?", (limit,))

    # ---------- 统计/维护 ----------

    def stats(self) -> dict:
        ch = self._one("SELECT COUNT(*) AS n FROM chapters WHERE deleted=0")
        arch = self._one("SELECT COUNT(*) AS n FROM chapters WHERE deleted=0 AND status='已归档'")
        # 只统计未删除章节的正文（回收站不计）
        words = self._one(
            "SELECT COALESCE(SUM(LENGTH(t.content)),0) AS n FROM text_blocks t"
            " JOIN chapters c ON c.id=t.chapter_id AND c.deleted=0"
            " WHERE t.status='已接受' OR t.status='已锁定'")
        return {"chapters": ch["n"] if ch else 0, "archived": arch["n"] if arch else 0,
                "words": words["n"] if words else 0}

    def backup(self, backup_dir: str | None = None) -> str:
        """复制 db 到 backups/（时间戳命名），返回备份文件路径。

        破甲/生成偏好等工具配置已全局共享（2026-08-08），不在项目文件夹内，
        项目备份只带走内容数据（db），全局配置随全局库另行备份。
        """
        backup_dir = backup_dir or os.path.join(self.folder, "backups")
        os.makedirs(backup_dir, exist_ok=True)
        # WAL 模式下先 checkpoint，确保复制完整；复制全程持连接锁——
        # 否则 FULL checkpoint 与 copy2 之间的并发写会落入 WAL 而漏进备份
        # （备份文件仍是有效 db，只是缺最近几笔写入）。
        stamp = db.now().replace(":", "").replace("-", "")
        dest = os.path.join(backup_dir, f"project_{stamp}.db")
        with self._lock:
            self.conn.execute("PRAGMA wal_checkpoint(FULL)")
            shutil.copy2(self.db_path, dest)
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return dest

    @property
    def is_closed(self) -> bool:
        return self._closed

    def close(self):
        self._closed = True  # 先立旗再关连接：并发线程看到旗标即放弃，不报「库已关闭」
        try:
            with self._lock:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        try:
            self.conn.close()
        except sqlite3.Error:
            pass


# ---------- 记忆 → 设定库导入（UI「从记忆导入全部」与归档自动导入共用，2026-09-08） ----------

# 状态卡字段 → 中文标签（角色卡摘要用；lore_page 沿用同款字段表）
_CARD_FIELD_CN = {"location": "所在", "clothing": "穿着", "injuries": "伤势",
                  "physical_state": "体力/生理", "items": "随身物品", "posture": "姿态",
                  "emotion": "情绪", "knowledge": "所知", "relationships": "关系"}

_ENTITY_PLACE_HINTS = ("地", "城", "殿", "楼", "阁", "园", "府", "院",
                       "山", "林", "谷", "洞", "寺", "观", "庙", "国",
                       "州", "郡", "宅", "屋", "街", "巷", "桥", "关")
_ENTITY_ITEM_HINTS = ("物", "器", "剑", "刀", "丹", "符", "印", "令",
                      "珠", "宝", "药", "书", "经", "诀", "甲", "衣",
                      "兵", "械", "材", "食", "酒")


def _card_summary(card) -> str:
    """状态卡 → 可读摘要（主要字段拼接，空字段跳过；与注入状态卡同信息）。"""
    if not isinstance(card, dict):
        return str(card)
    lines = []
    for k, label in _CARD_FIELD_CN.items():
        v = card.get(k)
        if not v:
            continue
        if isinstance(v, list):
            v = "、".join(str(x) for x in v)
        lines.append(f"{label}：{v}")
    cf = card.get("custom_fields")
    if isinstance(cf, dict):
        for k, v in cf.items():
            if v:
                lines.append(f"{k}：{v}")
    if lines:
        return "\n".join(lines)
    return json.dumps(card, ensure_ascii=False, indent=1)


def _entity_category(e: dict) -> str:
    """世界实体 → 设定分类（kind 是自由文本，按关键词归类，认不出归「术语」）。"""
    kind = str(e.get("kind") or "").strip()
    if kind in LORE_CATEGORIES:
        return kind
    if any(k in kind for k in _ENTITY_PLACE_HINTS):
        return "地点"
    if any(k in kind for k in _ENTITY_ITEM_HINTS):
        return "物品"
    return "术语"


def _entity_summary(name: str, e: dict) -> str:
    """世界实体 → 可读设定正文（四字段 + 别名）。"""
    bits = []
    for k, label in (("kind", "类别"), ("owner", "归属"), ("location", "位于"),
                     ("status", "状态")):
        v = str(e.get(k) or "").strip()
        if v:
            bits.append(f"{label}：{v}")
    als = [str(a).strip() for a in (e.get("aliases") or []) if str(a).strip()]
    if als:
        bits.append("别名：" + "、".join(als))
    bits.append(f"（导入自记忆·{name}）")
    return "\n".join(bits)


def lore_import_preview(store: ProjectStore) -> list[tuple[str, str, str]]:
    """扫描当前记忆，返回待导入的 (分类, 名称, 正文) 列表（已排除同名已有条目）。

    供 UI 预览确认与自动导入共用——同源保证「看到的」与「导进去的」一致。
    """
    mem = store.current_memory()
    existing = {(cat, str(e.get("name") or "").strip())
                for cat in LORE_CATEGORIES for e in store.list_lore(cat)}
    pending: list[tuple[str, str, str]] = []
    for name, card in (mem.get("characters") or {}).items():
        name = str(name or "").strip()
        if name and ("角色", name) not in existing:
            pending.append(("角色", name, _card_summary(card)))
    ents = ((mem.get("world_state") or {}).get("entities")) or {}
    for name, e in ents.items():
        name = str(name or "").strip()
        if not name or not isinstance(e, dict):
            continue
        cat = _entity_category(e)
        if (cat, name) in existing:
            continue
        pending.append((cat, name, _entity_summary(name, e)))
    return pending


def lore_import_from_memory(store: ProjectStore) -> list[tuple[str, str]]:
    """把记忆中设定库缺失的人物/实体批量导入（同名跳过，幂等可反复调用）。

    返回 [(分类, 名称), …]。导入内容注明「（导入自记忆·XX）」，用户可在设定库
    再整理成真正的设定描述。UI 手动导入与归档后自动导入走同一函数。
    """
    pending = lore_import_preview(store)
    for cat, name, content in pending:
        store.add_lore(cat, name, content=content, tags=[])
    return [(cat, name) for cat, name, _c in pending]
