# -*- coding: utf-8 -*-
"""SQLite 数据层：schema 定义、迁移、连接管理。

本项目所有持久化都在单个 project.db 中，带 schema_version 供未来升级迁移。
改动 schema 时必须：1) 修改 SCHEMA_SQL 2) 递增 SCHEMA_VERSION 3) 在 MIGRATIONS 加迁移脚本。
"""
import os
import sqlite3
from datetime import datetime

SCHEMA_VERSION = 22

# 新模型档案的默认上下文长度。写小说需要尽量长的上下文（多章记忆、前文注入），
# 所以默认给到 100 万 token；若服务方模型实际上限更低、报"上下文超限"错误，
# 在模型档案里手动调小即可。
DEFAULT_CONTEXT_LENGTH = 1_000_000

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 总纲：版本快照式，每次确认/锁定存新版本
CREATE TABLE IF NOT EXISTS synopsis (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  version    INTEGER NOT NULL,
  title      TEXT NOT NULL DEFAULT '',
  content    TEXT NOT NULL,
  is_locked  INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT ''  -- 本版内容最后变更时间（分卷覆盖仲裁用，_ensure_synopsis_updated_at 补列）
);

-- 分卷：总纲与大纲之间的第二层骨架（卷名 + 分卷描述 + 建议章数）
CREATE TABLE IF NOT EXISTS volumes (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  order_index     INTEGER NOT NULL DEFAULT 0,
  title           TEXT NOT NULL DEFAULT '',
  summary         TEXT NOT NULL DEFAULT '',  -- 分卷描述（本卷主线与扩充深化的情节，供大纲锚定与卷间衔接）
  target_chapters INTEGER,                    -- 建议章数（NULL = 未指定/AI 自定）
  synopsis_synced TEXT NOT NULL DEFAULT '',  -- 上次与总纲分卷段同步的基线 JSON {"t":卷名,"b":段正文}（分卷覆盖仲裁用）
  version         INTEGER NOT NULL DEFAULT 1, -- 当前版本号（每次内容变更旧版自动入 volume_versions）
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

-- 分卷版本快照：旧版在每次内容变更前自动存档（对齐总纲的版本历史与正文块快照模式）
CREATE TABLE IF NOT EXISTS volume_versions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  volume_id       INTEGER NOT NULL,
  version         INTEGER NOT NULL,          -- 被替换下来的旧版版本号
  title           TEXT NOT NULL DEFAULT '',
  summary         TEXT NOT NULL DEFAULT '',
  target_chapters INTEGER,
  created_at      TEXT NOT NULL
);

-- 章节：状态机 未规划→有大纲→有细纲→写作中→已完成→已归档→已锁定
CREATE TABLE IF NOT EXISTS chapters (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  order_index    INTEGER NOT NULL,
  volume_id      INTEGER,                    -- 所属分卷（NULL = 未分卷）
  status         TEXT NOT NULL DEFAULT '未规划',
  title          TEXT NOT NULL DEFAULT '',
  event          TEXT NOT NULL DEFAULT '',
  conflict       TEXT NOT NULL DEFAULT '',
  characters     TEXT NOT NULL DEFAULT '[]',
  outline_custom_fields TEXT NOT NULL DEFAULT '{}',
  info_progress  TEXT NOT NULL DEFAULT '',
  char_change    TEXT NOT NULL DEFAULT '',
  hook           TEXT NOT NULL DEFAULT '',
  target_words   INTEGER NOT NULL DEFAULT 3000,
  outline_content TEXT NOT NULL DEFAULT '',  -- 章节大纲全文（新格式 title+content；旧字段列保留兼容旧书）
  outline_draft  TEXT NOT NULL DEFAULT '',   -- 分卷切分初稿（细化/重生成/手动编辑都不覆盖，重新细化以此为底稿）
  outline_refined INTEGER NOT NULL DEFAULT 0, -- 大纲是否已完成 AI 细化（0=分卷切分初稿，不展示；1=细化后/手动定稿）
  outline_locked INTEGER NOT NULL DEFAULT 0,
  deleted        INTEGER NOT NULL DEFAULT 0,
  deleted_at     TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);

-- 细纲块：单章拆成 3~8 块，块颗粒度决定正文生成精度
CREATE TABLE IF NOT EXISTS scene_blocks (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  chapter_id    INTEGER NOT NULL,
  order_index   INTEGER NOT NULL,
  content       TEXT NOT NULL DEFAULT '',
  target_words  INTEGER NOT NULL DEFAULT 1000,
  narrative_task TEXT NOT NULL DEFAULT '',
  must_have     TEXT NOT NULL DEFAULT '',
  point         TEXT NOT NULL DEFAULT '',  -- 归属大纲序号点（如①）
  characters    TEXT NOT NULL DEFAULT '[]',
  status        TEXT NOT NULL DEFAULT '未生成',
  block_type    TEXT NOT NULL DEFAULT '',  -- 块类型（自定义细纲模板：铺垫/温情等）
  scene_direction TEXT NOT NULL DEFAULT '', -- 场景指示（环境/角色起始状态/感官重点/节奏指示）
  sensory_focus TEXT NOT NULL DEFAULT '',  -- 感官焦点（戏块必填，非重要戏块留空）
  to_state      TEXT NOT NULL DEFAULT '',  -- 本块结束状态（状态链 to）
  event_title   TEXT NOT NULL DEFAULT '',  -- 所属子事件标题（子事件①：…）
  shot_script   TEXT NOT NULL DEFAULT '',  -- 拍级分镜原文（『拍1.1 | 拍类型：…』分段，注入提示词）
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

-- 正文块：scene_block_id 可空（无细纲的自由写作模式 / 整章正文模式）
CREATE TABLE IF NOT EXISTS text_blocks (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  chapter_id     INTEGER NOT NULL,
  scene_block_id INTEGER,
  order_index    INTEGER NOT NULL,
  content        TEXT NOT NULL DEFAULT '',
  status         TEXT NOT NULL DEFAULT '草稿',
  version        INTEGER NOT NULL DEFAULT 1,
  is_style_sample INTEGER NOT NULL DEFAULT 0,
  kind           TEXT NOT NULL DEFAULT '',  -- ''=细纲块正文/自由写作；'chapter'=整章正文（不分块模式，一章一条）
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);

-- 正文块快照：旧版在每次接受新内容前自动存档
CREATE TABLE IF NOT EXISTS text_block_snapshots (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  text_block_id INTEGER NOT NULL,
  version       INTEGER NOT NULL,
  content       TEXT NOT NULL,
  created_at    TEXT NOT NULL
);

-- 正文分批：分批模式（prose_mode=batch）下一章的批次工作区。
-- 每批 = 一段连续细纲块 + 一次 AI 调用；滚动状态包（模型输出在 content 标签外）
-- 原样保存、注入下一批。全部批生成完 → 合并全文经 accept_chapter_prose 落库
-- （kind='chapter'），本表只作生成过程与分批查看的底稿，不参与导出/记忆。
CREATE TABLE IF NOT EXISTS prose_batches (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  chapter_id    INTEGER NOT NULL,
  order_index   INTEGER NOT NULL,           -- 第几批（1-based，连续）
  block_ids     TEXT NOT NULL DEFAULT '[]', -- 本批覆盖的细纲块 id（JSON 数组）
  content       TEXT NOT NULL DEFAULT '',   -- 本批正文
  state_package TEXT NOT NULL DEFAULT '',   -- 本批滚动状态包原文（注入下一批）
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_prose_batches_chapter ON prose_batches(chapter_id, order_index);

-- 文风改写（2026-10-06，流水线第 6 环）：整章文风改写的批次工作区。
-- 分批模式下每批 = 一段连续自然段 + 一次 AI 调用；改写批包（模型输出在
-- content 标签外）原样保存、注入下一批。全部批完成后合并经预览确认替换
-- 编辑器正文，本表只作改写过程底稿与中断续跑依据，不参与导出/记忆。
CREATE TABLE IF NOT EXISTS rewrite_batches (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  chapter_id    INTEGER NOT NULL,
  order_index   INTEGER NOT NULL,            -- 第几批（1-based，连续）
  para_start    INTEGER NOT NULL DEFAULT 0,  -- 本批起始自然段号（初稿编号，含）
  para_end      INTEGER NOT NULL DEFAULT 0,  -- 本批结束自然段号（含）
  content       TEXT NOT NULL DEFAULT '',    -- 本批成稿
  batch_package TEXT NOT NULL DEFAULT '',    -- 本批改写批包原文（注入下一批）
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rewrite_batches_chapter ON rewrite_batches(chapter_id, order_index);

-- 生成结果缓存（2026-10-27）：手动生成按请求指纹复用上次完整结果（内容没变
-- 就不再花钱重跑）。指纹 = 角色+模型+温度+max_tokens+完整报文的 SHA-256；
-- 只缓存「确实写完」的结果（取消/空文本/截断/未过完整性判定不入库）。
CREATE TABLE IF NOT EXISTS generation_cache (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint  TEXT NOT NULL UNIQUE,        -- 请求指纹（sha256 十六进制）
  task_role    TEXT NOT NULL DEFAULT '',    -- 任务角色（排查用冗余）
  model        TEXT NOT NULL DEFAULT '',    -- 模型名（排查用冗余）
  result_text  TEXT NOT NULL DEFAULT '',    -- 清洗后的完整结果
  thinking     TEXT NOT NULL DEFAULT '',    -- 思考过程（可空）
  created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_generation_cache_created ON generation_cache(created_at);

-- 本章动态：每块正文确认后提取的块级状态变化，注入后续所有块
CREATE TABLE IF NOT EXISTS chapter_notes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  chapter_id  INTEGER NOT NULL,
  order_index INTEGER NOT NULL,
  content     TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

-- 记忆链快照 v0..vN：full_state 为角色状态卡全量 + 世界状态
CREATE TABLE IF NOT EXISTS memory_snapshots (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  version     INTEGER NOT NULL,
  chapter_id  INTEGER,
  full_state  TEXT NOT NULL,
  diff        TEXT,
  summary     TEXT NOT NULL DEFAULT '',
  events      TEXT NOT NULL DEFAULT '[]',
  stale       INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL
);

-- 记忆向量索引（语义检索）：归档时生成；snapshot_version 指向 memory_snapshots.version
CREATE TABLE IF NOT EXISTS memory_vectors (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_version INTEGER NOT NULL,
  unit_key         TEXT NOT NULL,       -- 记忆单元标识（char:沈晏 / entity:玉佩 / event:N）
  text             TEXT NOT NULL,       -- 原始文本（检索结果展示用）
  vector           TEXT NOT NULL,       -- JSON 数组（float 列表）
  created_at       TEXT NOT NULL
);

-- 设定资料库：角色/地点/物品/术语（修订版精简分类）
CREATE TABLE IF NOT EXISTS lore_entries (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  category   TEXT NOT NULL,
  name       TEXT NOT NULL,
  content    TEXT NOT NULL DEFAULT '',
  tags       TEXT NOT NULL DEFAULT '[]',
  locked     INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_seen_chapter INTEGER,
  removed_at_chapter INTEGER
);

-- 偏好卡片：全局/阶段/手动 三级作用范围
CREATE TABLE IF NOT EXISTS preference_cards (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  title      TEXT NOT NULL,
  content    TEXT NOT NULL DEFAULT '',
  enabled    INTEGER NOT NULL DEFAULT 1,
  scope      TEXT NOT NULL DEFAULT 'global',
  stage      TEXT NOT NULL DEFAULT '',
  stages     TEXT NOT NULL DEFAULT '',   -- 逗号分隔的注入阶段列表（仅 global 卡用；空=全部阶段）
  priority   INTEGER NOT NULL DEFAULT 5,
  pinned     INTEGER NOT NULL DEFAULT 0,   -- 置顶：global 卡脱离「全局偏好」模块，紧随破限锚定注入（仅 global 卡用）
  note       TEXT NOT NULL DEFAULT '',
  is_builtin INTEGER NOT NULL DEFAULT 0,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

-- 提示词预设：一组条目 + 采样参数 + 顺序
CREATE TABLE IF NOT EXISTS presets (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  data       TEXT NOT NULL,
  is_builtin INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

-- 模型档案：OpenAI 兼容接口
CREATE TABLE IF NOT EXISTS model_profiles (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  name           TEXT NOT NULL,
  base_url       TEXT NOT NULL,
  api_key        TEXT NOT NULL DEFAULT '',
  model          TEXT NOT NULL,
  context_length INTEGER NOT NULL DEFAULT 1000000,  -- 与 DEFAULT_CONTEXT_LENGTH 保持一致
  input_price    REAL NOT NULL DEFAULT 0,
  output_price   REAL NOT NULL DEFAULT 0,
  currency       TEXT NOT NULL DEFAULT '元',
  max_tokens     INTEGER NOT NULL DEFAULT 4096,
  timeout        INTEGER NOT NULL DEFAULT 120,
  temperature    REAL NOT NULL DEFAULT 0.8,
  reasoning_mode TEXT NOT NULL DEFAULT 'max',
  max_output_mode TEXT NOT NULL DEFAULT 'auto',
  rpm            INTEGER NOT NULL DEFAULT 0,  -- 每分钟请求上限（0=不限）；渠道限流小的中转站必须配
  created_at     TEXT NOT NULL
);

-- 任务角色路由：每个角色绑模型档案 + 温度 + 专属系统提示词
CREATE TABLE IF NOT EXISTS task_routes (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  role          TEXT NOT NULL UNIQUE,
  name          TEXT NOT NULL,
  profile_id    INTEGER,
  temperature   REAL NOT NULL DEFAULT 0.8,
  system_prompt TEXT NOT NULL DEFAULT '',
  created_at    TEXT NOT NULL
);

-- 请求日志：全量记录，可追溯
CREATE TABLE IF NOT EXISTS request_logs (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  time              TEXT NOT NULL,
  role              TEXT NOT NULL DEFAULT '',
  model             TEXT NOT NULL DEFAULT '',
  prompt            TEXT,
  response          TEXT,
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  cost              REAL NOT NULL DEFAULT 0,
  status            TEXT NOT NULL DEFAULT 'ok',
  error             TEXT NOT NULL DEFAULT '',
  duration_ms       INTEGER NOT NULL DEFAULT 0,
  finish_reason     TEXT NOT NULL DEFAULT '',
  thinking          TEXT,
  reasoning_tokens  INTEGER NOT NULL DEFAULT 0,
  task_text         TEXT
);

-- 锁定项：注入时带"不可更改"标注
CREATE TABLE IF NOT EXISTS locked_items (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  item_type  TEXT NOT NULL,
  value      TEXT NOT NULL,
  note       TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

-- 检查误报清单：标记误报后不再重复报告
CREATE TABLE IF NOT EXISTS false_positives (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  text       TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- 检查结果记录
CREATE TABLE IF NOT EXISTS check_results (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  chapter_id INTEGER,
  time       TEXT NOT NULL,
  issues     TEXT NOT NULL
);

-- 项目级设置（前文注入配置、本章动态开关等）
CREATE TABLE IF NOT EXISTS project_settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chapters_order ON chapters(order_index);
CREATE INDEX IF NOT EXISTS idx_blocks_chapter ON scene_blocks(chapter_id, order_index);
CREATE INDEX IF NOT EXISTS idx_text_chapter ON text_blocks(chapter_id, order_index);
CREATE INDEX IF NOT EXISTS idx_notes_chapter ON chapter_notes(chapter_id, order_index);
CREATE INDEX IF NOT EXISTS idx_snap_chapter ON memory_snapshots(chapter_id);
"""

# 未来迁移脚本：[(目标版本号, SQL), ...]
#
# 【版本号对照表（改 schema 前先读）】
#   v2-v6   → 本清单 MIGRATIONS（ALTER 脚本）
#   v7-v15  → migrate() 内 _ensure_* 幂等补列函数（历史原因：当时直接改 SCHEMA_SQL，
#             没留 ALTER 脚本；幂等函数对新旧库都安全，等效迁移）
#   ⚠️ 新增列一律走 _ensure_*，**不要**往 MIGRATIONS 加 (n, "ALTER ... ADD COLUMN")：
#      migrate() 第一步就是 executescript(SCHEMA_SQL) 建全表，新表已含该列；
#      版本号比 n 低的老库执行 ALTER 时会报 duplicate column name。
#      （2026-09-19 踩过：rpm 加进 MIGRATIONS → 5 个迁移用例全红。）
#      在本清单加条目的场景只剩「新建表/索引等无法幂等 ALTER 的改动」，
#      并在此处登记，保持「版本号 → 改动位置」可追溯。
MIGRATIONS = [
    (2, """
        ALTER TABLE model_profiles ADD COLUMN reasoning_mode TEXT NOT NULL DEFAULT 'max';
        ALTER TABLE model_profiles ADD COLUMN max_output_mode TEXT NOT NULL DEFAULT 'auto';
    """),
    (3, """
        ALTER TABLE chapters ADD COLUMN outline_custom_fields TEXT NOT NULL DEFAULT '{}';
    """),
    (4, """
        CREATE TABLE IF NOT EXISTS volumes (
          id              INTEGER PRIMARY KEY AUTOINCREMENT,
          order_index     INTEGER NOT NULL DEFAULT 0,
          title           TEXT NOT NULL DEFAULT '',
          summary         TEXT NOT NULL DEFAULT '',
          target_chapters INTEGER,
          created_at      TEXT NOT NULL,
          updated_at      TEXT NOT NULL
        );
        ALTER TABLE chapters ADD COLUMN volume_id INTEGER;
        CREATE INDEX IF NOT EXISTS idx_chapters_volume ON chapters(volume_id);
    """),
    (5, """
        ALTER TABLE chapters ADD COLUMN outline_content TEXT NOT NULL DEFAULT '';
    """),
    (6, """
        CREATE TABLE IF NOT EXISTS memory_vectors (
          id               INTEGER PRIMARY KEY AUTOINCREMENT,
          snapshot_version INTEGER NOT NULL,
          unit_key         TEXT NOT NULL,
          text             TEXT NOT NULL,
          vector           TEXT NOT NULL,
          created_at       TEXT NOT NULL
        );
    """),
    # v16（2026-10-05）：正文分批 prose_batches 表。CREATE TABLE IF NOT EXISTS
    # 幂等（SCHEMA_SQL 已含该表），老库补建、新库跳过。
    (16, """
        CREATE TABLE IF NOT EXISTS prose_batches (
          id            INTEGER PRIMARY KEY AUTOINCREMENT,
          chapter_id    INTEGER NOT NULL,
          order_index   INTEGER NOT NULL,
          block_ids     TEXT NOT NULL DEFAULT '[]',
          content       TEXT NOT NULL DEFAULT '',
          state_package TEXT NOT NULL DEFAULT '',
          created_at    TEXT NOT NULL,
          updated_at    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_prose_batches_chapter ON prose_batches(chapter_id, order_index);
    """),
    # v17（2026-10-06）：文风改写 rewrite_batches 表（同 v16 幂等模式）。
    (17, """
        CREATE TABLE IF NOT EXISTS rewrite_batches (
          id            INTEGER PRIMARY KEY AUTOINCREMENT,
          chapter_id    INTEGER NOT NULL,
          order_index   INTEGER NOT NULL,
          para_start    INTEGER NOT NULL DEFAULT 0,
          para_end      INTEGER NOT NULL DEFAULT 0,
          content       TEXT NOT NULL DEFAULT '',
          batch_package TEXT NOT NULL DEFAULT '',
          created_at    TEXT NOT NULL DEFAULT '',
          updated_at    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rewrite_batches_chapter ON rewrite_batches(chapter_id, order_index);
    """),
    # v18（2026-10-08）：分卷迭代版本化——volume_versions 快照表（同 v16/v17 幂等模式，
    # CREATE TABLE IF NOT EXISTS：新库由 SCHEMA_SQL 建好，老库补建）；
    # volumes.version 列走 _ensure_volume_version 幂等补列，不进本清单（见上方红线注释）。
    (18, """
        CREATE TABLE IF NOT EXISTS volume_versions (
          id              INTEGER PRIMARY KEY AUTOINCREMENT,
          volume_id       INTEGER NOT NULL,
          version         INTEGER NOT NULL,
          title           TEXT NOT NULL DEFAULT '',
          summary         TEXT NOT NULL DEFAULT '',
          target_chapters INTEGER,
          created_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_volume_versions ON volume_versions(volume_id, version);
    """),
    # v19（2026-10-27）：生成结果缓存表（同 v16-v18 幂等模式，SCHEMA_SQL 同步建表）——
    # 手动生成按请求指纹复用上次完整结果；fingerprint 唯一键 upsert，上限清理在 store 层做。
    (19, """
        CREATE TABLE IF NOT EXISTS generation_cache (
          id           INTEGER PRIMARY KEY AUTOINCREMENT,
          fingerprint  TEXT NOT NULL UNIQUE,
          task_role    TEXT NOT NULL DEFAULT '',
          model        TEXT NOT NULL DEFAULT '',
          result_text  TEXT NOT NULL DEFAULT '',
          thinking     TEXT NOT NULL DEFAULT '',
          created_at   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_generation_cache_created ON generation_cache(created_at);
    """),
]

# 版本号对照表补充（v15，2026-09-19）：
#   v15 → model_profiles.rpm（每分钟请求上限）；走 migrate() 的
#         _ensure_model_profile_rpm 幂等补列，**不进上方 MIGRATIONS**——
#         migrate() 先 executescript(SCHEMA_SQL) 建全表，新表已含 rpm 列，
#         老库若再执行 ALTER ADD 会报 duplicate column name。v7-v14 同理。

# =====================================================================
# 全局配置库（%APPDATA%/酒馆小说生成器/app.db）
# 存放跨项目共享的用户级配置：模型档案 / 任务角色路由 / 偏好卡片 / 提示词预设。
# 表结构与项目库对应部分一致；固定 schema，无需版本迁移（表结构稳定）。
# =====================================================================

GLOBAL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 模型档案：OpenAI 兼容接口
CREATE TABLE IF NOT EXISTS model_profiles (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  name           TEXT NOT NULL,
  base_url       TEXT NOT NULL,
  api_key        TEXT NOT NULL DEFAULT '',
  model          TEXT NOT NULL,
  context_length INTEGER NOT NULL DEFAULT 1000000,  -- 与 DEFAULT_CONTEXT_LENGTH 保持一致
  input_price    REAL NOT NULL DEFAULT 0,
  output_price   REAL NOT NULL DEFAULT 0,
  currency       TEXT NOT NULL DEFAULT '元',
  max_tokens     INTEGER NOT NULL DEFAULT 4096,
  timeout        INTEGER NOT NULL DEFAULT 120,
  temperature    REAL NOT NULL DEFAULT 0.8,
  reasoning_mode TEXT NOT NULL DEFAULT 'max',
  max_output_mode TEXT NOT NULL DEFAULT 'auto',
  jailbreak      TEXT NOT NULL DEFAULT '',  -- 该档案使用的破甲名（''=跟随全局当前破甲；'__none__'=不注入）
  rpm            INTEGER NOT NULL DEFAULT 0,  -- 每分钟请求上限（0=不限）；渠道限流小的中转站必须配
  created_at     TEXT NOT NULL
);

-- 任务角色路由：每个角色绑模型档案 + 温度 + 专属系统提示词
CREATE TABLE IF NOT EXISTS task_routes (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  role          TEXT NOT NULL UNIQUE,
  name          TEXT NOT NULL,
  profile_id    INTEGER,
  temperature   REAL NOT NULL DEFAULT 0.8,
  system_prompt TEXT NOT NULL DEFAULT '',
  locked        INTEGER NOT NULL DEFAULT 0,  -- 一键绑定全部角色时不更换该角色档案
  created_at    TEXT NOT NULL
);

-- 偏好卡片：全局/阶段/手动 三级作用范围
CREATE TABLE IF NOT EXISTS preference_cards (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  title      TEXT NOT NULL,
  content    TEXT NOT NULL DEFAULT '',
  enabled    INTEGER NOT NULL DEFAULT 1,
  scope      TEXT NOT NULL DEFAULT 'global',
  stage      TEXT NOT NULL DEFAULT '',
  stages     TEXT NOT NULL DEFAULT '',   -- 逗号分隔的注入阶段列表（仅 global 卡用；空=全部阶段）
  priority   INTEGER NOT NULL DEFAULT 5,
  pinned     INTEGER NOT NULL DEFAULT 0,   -- 置顶：global 卡脱离「全局偏好」模块，紧随破限锚定注入（仅 global 卡用）
  note       TEXT NOT NULL DEFAULT '',
  is_builtin INTEGER NOT NULL DEFAULT 0,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

-- 提示词预设：一组条目 + 采样参数 + 顺序
CREATE TABLE IF NOT EXISTS presets (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  data       TEXT NOT NULL,
  is_builtin INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
"""


def connect_global(path: str) -> sqlite3.Connection:
    """打开（或创建）全局配置库并保证 schema 就绪。

    check_same_thread=False + 调用方连接锁，与项目库一致。
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(GLOBAL_SCHEMA_SQL)
    _ensure_global_model_profile_columns(conn)
    _ensure_global_card_stages(conn)
    _ensure_global_card_pinned(conn)
    _ensure_global_route_locked(conn)
    return conn


def _ensure_global_route_locked(conn: sqlite3.Connection) -> None:
    """旧全局库无版本迁移表：为 task_routes 补 locked 列（一键绑定时锁定角色不换模型）。"""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(task_routes)")}
    if "locked" not in columns:
        conn.execute("ALTER TABLE task_routes ADD COLUMN locked INTEGER NOT NULL DEFAULT 0")
        conn.commit()


def _ensure_global_card_stages(conn: sqlite3.Connection) -> None:
    """旧全局库无版本迁移表：为偏好卡片补齐 stages 列（2026-08-09 新增注入阶段）。"""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(preference_cards)")}
    if "stages" not in columns:
        conn.execute("ALTER TABLE preference_cards ADD COLUMN stages TEXT NOT NULL DEFAULT ''")
        conn.commit()


def _ensure_global_card_pinned(conn: sqlite3.Connection) -> None:
    """旧全局库无版本迁移表：为偏好卡片补齐 pinned 列（2026-08-30 新增置顶注入）。"""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(preference_cards)")}
    if "pinned" not in columns:
        conn.execute("ALTER TABLE preference_cards ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        conn.commit()


def _ensure_global_model_profile_columns(conn: sqlite3.Connection) -> None:
    """为没有版本迁移表的旧全局库补齐模型参数字段。"""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(model_profiles)")}
    if "reasoning_mode" not in columns:
        conn.execute("ALTER TABLE model_profiles ADD COLUMN reasoning_mode TEXT NOT NULL DEFAULT 'max'")
    if "max_output_mode" not in columns:
        conn.execute("ALTER TABLE model_profiles ADD COLUMN max_output_mode TEXT NOT NULL DEFAULT 'auto'")
    if "jailbreak" not in columns:
        # 2026-09-10：破甲由档案直接选择（取代旧的「破甲→适配模型关键词」自动匹配）
        conn.execute("ALTER TABLE model_profiles ADD COLUMN jailbreak TEXT NOT NULL DEFAULT ''")
    if "rpm" not in columns:
        # 2026-09-19：渠道每分钟请求上限（0=不限）。实测部分中转站把「每分钟 N 次」
        # 当硬限制且失败也计入，软件的失败补救连发会把配额打光 → 全 429。
        conn.execute("ALTER TABLE model_profiles ADD COLUMN rpm INTEGER NOT NULL DEFAULT 0")
    conn.commit()


def now() -> str:
    """统一时间戳格式：2026-08-05 13:20:39"""
    return datetime.now().isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    """打开（或创建）项目数据库并保证 schema 最新。

    check_same_thread=False：AI 生成在后台线程运行，UI 线程与服务线程共用连接；
    并发安全由 ProjectStore 的连接锁保证。
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """建表 + 应用迁移。"""
    conn.executescript(SCHEMA_SQL)
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
        conn.commit()
        return
    current = int(row["value"])
    for target, sql in MIGRATIONS:
        if current < target:
            conn.executescript(sql)
            conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(target),))
            current = target
    # 幂等补列（不放进 MIGRATIONS：SCHEMA_SQL 新建的表已含新列，旧库若表由
    # SCHEMA_SQL 首次创建再走 ALTER 会重复 ADD，故用 PRAGMA 检查 + ALTER，任何库都安全）
    _ensure_scene_block_point(conn)
    _ensure_scene_block_detail_fields(conn)
    _ensure_scene_block_shot_script(conn)
    _ensure_chapter_outline_refined(conn)
    _ensure_chapter_outline_draft(conn)
    _ensure_text_block_style_sample(conn)
    _ensure_text_block_kind(conn)
    _ensure_lore_last_seen(conn)
    _ensure_lore_removed_at(conn)
    _ensure_model_profile_rpm(conn)
    _ensure_volume_version(conn)
    _ensure_synopsis_updated_at(conn)
    _ensure_volume_synopsis_synced(conn)
    _ensure_request_log_columns(conn)
    if current < SCHEMA_VERSION:
        conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))
    conn.commit()


def _ensure_scene_block_point(conn: sqlite3.Connection) -> None:
    """为旧库的 scene_blocks 补 point 列（新库由 SCHEMA_SQL 直接建全）。"""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(scene_blocks)")}
    if "point" not in columns:
        conn.execute("ALTER TABLE scene_blocks ADD COLUMN point TEXT NOT NULL DEFAULT ''")


def _ensure_scene_block_detail_fields(conn: sqlite3.Connection) -> None:
    """为旧库的 scene_blocks 补细纲扩展字段列（block_type/scene_direction/sensory_focus/to_state/event_title）。

    自定义任务模板（子事件+块格式）解析出的块会携带这些字段；
    旧块默认空串，不影响既有数据。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(scene_blocks)")}
    for col in ("block_type", "scene_direction", "sensory_focus", "to_state", "event_title"):
        if col not in columns:
            conn.execute(f"ALTER TABLE scene_blocks ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")


def _ensure_scene_block_shot_script(conn: sqlite3.Connection) -> None:
    """为旧库的 scene_blocks 补 shot_script 列（拍级分镜原文）。

    自定义细纲模板（『拍1.1 | 拍类型：转场』分级格式）解析出的块会携带分镜原文；
    旧块默认空串，不影响既有数据。新库由 SCHEMA_SQL 直接建全，故走幂等检查
    而非 MIGRATIONS（否则新库会重复 ADD COLUMN 报错）。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(scene_blocks)")}
    if "shot_script" not in columns:
        conn.execute("ALTER TABLE scene_blocks ADD COLUMN shot_script TEXT NOT NULL DEFAULT ''")


def _ensure_chapter_outline_refined(conn: sqlite3.Connection) -> None:
    """为旧库的 chapters 补 outline_refined 列（大纲页区分「未细化初稿」）。

    存量章节：已有大纲内容的按「已细化」处理（不隐藏既有成果）；
    本次改动后，分卷切分新建的章节默认未细化（outline_refined=0）。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(chapters)")}
    if "outline_refined" not in columns:
        conn.execute("ALTER TABLE chapters ADD COLUMN outline_refined INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE chapters SET outline_refined=1"
                     " WHERE COALESCE(TRIM(outline_content),'') <> ''")
        conn.commit()


def _ensure_chapter_outline_draft(conn: sqlite3.Connection) -> None:
    """为旧库的 chapters 补 outline_draft 列（分卷切分初稿，细化不覆盖）。

    存量章节：初稿历史已丢失，用当前 outline_content 回填（行为同旧版：
    重新细化以当前大纲为底稿）；本次改动后，分卷切分新建的章节会把
    初稿单独存入 outline_draft，细化只写 outline_content。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(chapters)")}
    if "outline_draft" not in columns:
        conn.execute("ALTER TABLE chapters ADD COLUMN outline_draft TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE chapters SET outline_draft=outline_content"
                     " WHERE COALESCE(TRIM(outline_content),'') <> ''"
                     " AND COALESCE(TRIM(outline_draft),'') = ''")
        conn.commit()


def _ensure_text_block_style_sample(conn: sqlite3.Connection) -> None:
    """为旧库的 text_blocks 补 is_style_sample 列（风格样本标记）。

    MIGRATIONS 未覆盖该列（版本 7/8/9 期间直接改的 SCHEMA_SQL），旧库升级
    后 SELECT/UPDATE 该列会抛「no such column」，写作台/风格样本相关功能崩溃。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(text_blocks)")}
    if "is_style_sample" not in columns:
        conn.execute("ALTER TABLE text_blocks ADD COLUMN is_style_sample INTEGER NOT NULL DEFAULT 0")
        conn.commit()


def _ensure_text_block_kind(conn: sqlite3.Connection) -> None:
    """为旧库的 text_blocks 补 kind 列（整章正文模式：一章一条 kind='chapter'）。

    存量正文块一律留空串（细纲块正文/自由写作语义不变，仍按块展示与编辑）；
    整章模式（prose_mode=chapter）新写的正文记 kind='chapter'，与细纲块解耦——
    导出/检查/审校/归档都按 status 读 text_blocks，故对它们透明。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(text_blocks)")}
    if "kind" not in columns:
        conn.execute("ALTER TABLE text_blocks ADD COLUMN kind TEXT NOT NULL DEFAULT ''")
        conn.commit()


def _ensure_lore_last_seen(conn: sqlite3.Connection) -> None:
    """为旧库的 lore_entries 补 last_seen_chapter 列（设定库沉睡机制，2026-09-08）。

    存量条目一律 NULL =「未建立基准」，注入侧不判沉睡（保守：升级后旧条目
    不会集体消失）；首次归档时由 touch_lore_mentions 建立基准（命中的记当章、
    未命中的也记当章作为起点），此后「连续 N 章未被提及」才开始沉睡。
    新库由 SCHEMA_SQL 直接建全，故走幂等检查而非 MIGRATIONS。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(lore_entries)")}
    if "last_seen_chapter" not in columns:
        conn.execute("ALTER TABLE lore_entries ADD COLUMN last_seen_chapter INTEGER")
        conn.commit()


def _ensure_lore_removed_at(conn: sqlite3.Connection) -> None:
    """为旧库的 lore_entries 补 removed_at_chapter 列（设定库软移除，2026-09-08）。

    NULL = 正常；非 NULL = 该实体已被判定「消失/消耗/销毁」，记录判定章号，
    设定条目保留在库中但退出注入。存量条目一律 NULL（行为不变）。
    新库由 SCHEMA_SQL 直接建全，故走幂等检查而非 MIGRATIONS。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(lore_entries)")}
    if "removed_at_chapter" not in columns:
        conn.execute("ALTER TABLE lore_entries ADD COLUMN removed_at_chapter INTEGER")
        conn.commit()


def _ensure_model_profile_rpm(conn: sqlite3.Connection) -> None:
    """为旧库的 model_profiles 补 rpm 列（渠道每分钟请求上限，2026-09-19）。

    存量档案一律 0 = 不限（升级后行为与旧版逐字节一致，零回归）；
    老板按渠道说明手动填真实上限（如某些中转站 1 分钟 3 次）。
    新库由 SCHEMA_SQL 直接建全，故走幂等检查而非 MIGRATIONS——往 MIGRATIONS 里
    加 ALTER ADD COLUMN 会让「版本号偏低但表已由 SCHEMA_SQL 建全」的老库报
    duplicate column name（2026-09-19 实测踩过）。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(model_profiles)")}
    if "rpm" not in columns:
        conn.execute("ALTER TABLE model_profiles ADD COLUMN rpm INTEGER NOT NULL DEFAULT 0")
        conn.commit()


def _ensure_volume_version(conn: sqlite3.Connection) -> None:
    """为旧库的 volumes 补 version 列（分卷迭代版本化，2026-10-08 v18）。

    存量卷一律 v1；内容变更后由 store.save_volume 自动快照旧版并递增。
    与 rpm 同理：新库由 SCHEMA_SQL 建全，幂等检查防 duplicate column name。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(volumes)")}
    if "version" not in columns:
        conn.execute("ALTER TABLE volumes ADD COLUMN version INTEGER NOT NULL DEFAULT 1")


def _ensure_synopsis_updated_at(conn: sqlite3.Connection) -> None:
    """为旧库的 synopsis 补 updated_at 列并回填（2026-10-16 分卷修订生效）。

    updated_at = 该版总纲内容最后一次变更时间：新建版本=created_at，总纲页就地修改
    （update_synopsis_in_place）会刷新。存量行有 synopsis_synced 基线时按基线仲裁；
    无基线（老数据）时才用本列兜底：volumes.updated_at 不早于本版时间 → 分卷改在
    总纲之后 → 注入总纲时用分卷库现行覆盖对应卷段。
    与 rpm/version 同理走幂等补列，不进 MIGRATIONS（防新库 duplicate column）。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(synopsis)")}
    if "updated_at" not in columns:
        conn.execute("ALTER TABLE synopsis ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
    conn.execute("UPDATE synopsis SET updated_at=created_at WHERE updated_at=''")


def _ensure_volume_synopsis_synced(conn: sqlite3.Connection) -> None:
    """为旧库的 volumes 补 synopsis_synced 基线列（2026-10-16 分卷修订生效）。

    基线 = 该卷上次与总纲『分卷N』段同步时的 {卷名, 段正文} JSON：
    锁定/从总纲导入/总纲就地修改后按卷同步时写入。注入仲裁：分卷行现值≠基线
    → 分卷侧在同步后又修订过 → 用分卷库覆盖总纲旧段；分卷行现值=基线且
    总纲段≠基线 → 总纲侧后改（就地修改会同步回分卷行，正常不该出现）→ 以总纲为准。
    空串 = 无基线老数据 → 回退 updated_at 时间仲裁（见 _ensure_synopsis_updated_at）。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(volumes)")}
    if "synopsis_synced" not in columns:
        conn.execute("ALTER TABLE volumes ADD COLUMN synopsis_synced TEXT NOT NULL DEFAULT ''")


def _ensure_request_log_columns(conn: sqlite3.Connection) -> None:
    """为旧库的 request_logs 补两列（2026-11-05，排查取证用）。

    - `finish_reason`：本请求的原始终止原因。此前只认 `length`（长度截断），
      模型/平台给的其它终止原因全被丢掉。智谱 GLM 官方在**生成过程中**被内容
      审核掐断时，流式最后一帧给 `finish_reason: "sensitive"`（不是 HTTP 错误）
      ——旧代码把它当正常结束，下游只能靠末尾标点猜，于是把「平台审核掐断」
      误报成「破限没生效」。记下来才能分清 length / sensitive / stop / 空串。
    - `thinking`：模型的思考内容（reasoning_content / thinking）。
      **旧日志只存正文不存思考**，曾导致排查时把「正文里的思考输出段」当成
      「模型的内部思考」，把两件事看反（2026-11-05 老板当场纠正）。
      思考是判断「模型在干什么」的唯一直接证据，必须留痕。
    - `reasoning_tokens`：思考占用的输出 token（usage 的
      completion_tokens_details.reasoning_tokens）。GLM-5.3 的思考不可关闭、
      默认 reasoning_effort=max，思考 tokens 计入 completion 也占 max_tokens
      预算——不单独记，「正文才 3000 字怎么就停了」就永远看不出真正原因。
    - `task_text`：任务指令原文（2026-11-05 日志续写）。日志的 `prompt` 是
      模块拼接后的全文（`[标题]\n正文` 空行连接），没有单独的任务指令字段；
      从旧日志续写只能按模块边界反推（pipeline.parse_log_task）。存原文更准，
      也免去反推失败时入口不可用。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(request_logs)")}
    if "finish_reason" not in columns:
        conn.execute(
            "ALTER TABLE request_logs ADD COLUMN finish_reason TEXT NOT NULL DEFAULT ''")
    if "thinking" not in columns:
        conn.execute("ALTER TABLE request_logs ADD COLUMN thinking TEXT")
    if "reasoning_tokens" not in columns:
        conn.execute(
            "ALTER TABLE request_logs ADD COLUMN reasoning_tokens INTEGER NOT NULL DEFAULT 0")
    if "task_text" not in columns:
        conn.execute("ALTER TABLE request_logs ADD COLUMN task_text TEXT")
