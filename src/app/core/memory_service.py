# -*- coding: utf-8 -*-
"""记忆服务：章末差异提取 → 用户逐条确认 → 生成新版本快照（记忆链核心）。

设计红线：差异只提取不落库，用户逐条确认/修改/驳回后才写入新快照。
回滚/级联过期逻辑在 ProjectStore（store.rollback_memory / mark_stale_from）。
"""
import copy
import sqlite3
import threading

from .assembler import _format_card
from .diagnostics import record_issue, clear_issue
from .embedding import embed_texts, load_embed_config
from .jsonx import extract_json, find_field
from .pipeline import Pipeline
from .store import (ProjectStore, new_character_card, sync_dead_flags, _to_bool_dead,
                    detect_deaths_in_prose)
from .task_templates import render as render_task


def _is_closed_db_error(e: Exception) -> bool:
    """后台线程撞上「库已关闭」（归档后切项目/回首页的竞态）→ 属正常放弃而非失败：
    向量会在下次归档或「记忆」页重建时补齐，不登记诊断提醒。"""
    return isinstance(e, sqlite3.ProgrammingError) or "closed database" in str(e)


def _project_tag(store) -> str:
    """当前项目名（诊断提醒按项目过滤显示用）；取不到留空=不过滤。"""
    try:
        return (store.title or "").strip()
    except Exception:
        return ""

# 字段名归一化（模型输出五花八门 → 状态卡标准字段）
FIELD_ALIASES = {
    "identity": "identity", "身份": "identity", "职业": "identity", "职务": "identity", "头衔": "identity",
    "appearance": "appearance", "外貌": "appearance", "外貌特征": "appearance",
    "容貌": "appearance", "长相": "appearance", "体貌": "appearance",
    "clothing": "clothing", "clothes": "clothing", "dress": "clothing",
    "穿着": "clothing", "外观": "clothing", "衣着": "clothing",
    "location": "location", "位置": "location", "place": "location", "所在地": "location",
    "injuries": "injuries", "伤": "injuries", "伤势": "injuries", "wounds": "injuries", "伤口": "injuries",
    "physical_state": "physical_state", "体力": "physical_state", "生理": "physical_state",
    "束缚": "physical_state", "体位": "physical_state",
    "items": "items", "物品": "items", "随身": "items", "携带": "items",
    "posture": "posture", "站位": "posture", "正在做": "posture", "姿势": "posture",
    "emotion": "emotion", "情绪": "emotion", "心情": "emotion", "精神状态": "emotion",
    "knowledge": "knowledge", "知道": "knowledge", "信息": "knowledge", "所知": "knowledge",
    "relationships": "relationships", "关系": "relationships",
    "unknown": "unknown", "不知道": "unknown", "未知": "unknown", "盲区": "unknown",
    "not_know": "unknown", "unaware": "unknown",
    "inferences": "inferences", "推测": "inferences", "怀疑": "inferences",
    "猜测": "inferences", "推断": "inferences",
    "aliases": "aliases", "alias": "aliases", "别名": "aliases", "简称": "aliases",
    "dead": "dead", "死亡": "dead", "已故": "dead", "已死": "dead", "身亡": "dead",
    "生死": "dead", "存活": "dead", "是否死亡": "dead",
    # 2026-09-09 补：模型常用英文/复合写法，漏掉这些别名时死亡会被当成未知字段
    # 写进 card 的杂项键（状态卡实际没死 → 后续章节继续让死者出场）
    "death": "dead", "died": "dead", "deceased": "dead", "is_dead": "dead",
    "death_status": "dead", "dead_status": "dead", "is_death": "dead",
    "死亡状态": "dead", "生死状态": "dead", "是否已故": "dead",
    "阵亡": "dead", "已阵亡": "dead", "已牺牲": "dead",
}
LIST_FIELDS = {"injuries", "items", "knowledge", "relationships", "unknown", "aliases"}

# 死亡兜底识别：模型没输出结构化 dead 字段、只把「已故」写进自由文本时，
# 程序据此补上（避免 AI 让死人出场）
# 死亡相关常量与工具定义在 store.py（store 是最底层，手动编辑状态卡也要用），
# 此处仅作再导出，保持 memory_service 内引用一致。


def memory_units(full_state: dict | None, events: list | None = None) -> list[dict]:
    """从记忆状态提取可检索单元（角色状态卡 / 实体 / 传闻 / 章节事件）。

    每个单元一条文本，向量化后用于语义检索召回。
    """
    units = []
    state = full_state or {}
    chars = state.get("characters") or {}
    for name, card in chars.items():
        if not isinstance(card, dict):
            continue
        text = _format_card(card)
        if text.strip():
            units.append({"unit_key": f"char:{name}", "text": text.strip()})
    ws = state.get("world_state") or {}
    ents = ws.get("entities") or {}
    for name, ent in ents.items():
        if not isinstance(ent, dict):
            continue
        parts = [str(ent[k]) for k in ("name", "status", "state", "描述", "状态")
                 if str(ent.get(k) or "").strip()]
        parts += [f"{k}：{v}" for k, v in ent.items()
                  if k not in ("name", "status", "state", "描述", "状态")
                  and str(v or "").strip()]
        if parts:
            units.append({"unit_key": f"entity:{name}", "text": f"【{name}】{'；'.join(parts)}"})
    for i, r in enumerate(ws.get("rumors") or []):
        if isinstance(r, dict) and str(r.get("content") or "").strip():
            t = str(r.get("time") or "").strip()
            units.append({"unit_key": f"rumor:{i}",
                          "text": f"【传闻】{t + '，' if t else ''}{str(r['content']).strip()}"})
    for i, ev in enumerate(events or []):
        text = ev.get("content") if isinstance(ev, dict) else ev
        if isinstance(text, str) and text.strip():
            units.append({"unit_key": f"event:{i}", "text": f"【本章事件】{text.strip()}"})
    return units


def normalize_field(field) -> str:
    if not field:
        return ""
    key = str(field).strip().lower()
    if key in FIELD_ALIASES:
        return FIELD_ALIASES[key]
    # 部分匹配：如 "clothing_description"
    for k, v in FIELD_ALIASES.items():
        if k in key or key in k:
            return v
    return key


def normalize_list(value) -> list:
    """列表字段容错：list / str（逗号顿号分号分隔）。"""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    s = str(value).strip()
    if not s:
        return []
    for sep in ("、", "，", ",", "；", ";"):
        if sep in s:
            return [x.strip() for x in s.split(sep) if x.strip()]
    return [s]


def normalize_knowledge(value):
    """knowledge 条目归一化：字符串 → {"fact": s, "source": ""}；dict 保留 fact/source。"""
    items = value if isinstance(value, list) else ([value] if value is not None else [])
    out = []
    for v in items:
        if isinstance(v, dict):
            fact = str(v.get("fact") or v.get("content") or "").strip()
            source = str(v.get("source") or "").strip()
            if fact:
                out.append({"fact": fact, "source": source})
        else:
            s = str(v).strip()
            if s:
                out.append({"fact": s, "source": ""})
    return out


def normalize_inferences(value):
    """inferences 条目归一化：字符串 → {"claim": s, "basis": ""}；dict 保留 claim/basis。"""
    items = value if isinstance(value, list) else ([value] if value is not None else [])
    out = []
    for v in items:
        if isinstance(v, dict):
            claim = str(v.get("claim") or v.get("content") or "").strip()
            basis = str(v.get("basis") or "").strip()
            if claim:
                out.append({"claim": claim, "basis": basis})
        else:
            s = str(v).strip()
            if s:
                out.append({"claim": s, "basis": ""})
    return out


def accepted_entity_removals(diff: dict, verdicts: dict) -> list[str]:
    """本次归档中「实体已消失」且未被驳回的实体名列表（供设定库软移除同步）。

    口径必须与 apply_diff 里的实体移除判定一致（kind == entity_remove、
    noop 跳过、reject 跳过），否则会出现「记忆里没了、设定库还留着」的错位。
    verdicts 为空（自动归档）时按全部采纳处理。
    """
    out = []
    for it in diff.get("items") or []:
        if not isinstance(it, dict) or it.get("kind") != "entity_remove":
            continue
        if it.get("noop"):
            continue
        v = verdicts.get(it.get("key"), {"action": "accept"})
        if v.get("action") == "reject":
            continue
        name = str(it.get("name") or "").strip()
        if name:
            out.append(name)
    return out


class MemoryService:
    """记忆链服务：差异提取 / 应用 / 归档。"""

    def __init__(self, store: ProjectStore):
        self.store = store
        self.pipeline = Pipeline(store)

    # ---------- 差异提取 ----------

    def chapter_full_text(self, chapter_id: int) -> str:
        blocks = self.store.list_text_blocks(chapter_id)
        accepted = [b for b in blocks if b["status"] in ("已接受", "已锁定")]
        return "\n\n".join(b["content"] for b in accepted)

    def extract_diff(self, chapter_id: int, on_chunk=None, allow_empty: bool = False,
                     prev_state_override: dict | None = None) -> dict:
        """调用记忆角色提取差异，解析失败自动重试（把错误反馈给模型）。

        prev_state 在开始提取时冻结一次，整轮重试复用同一份——避免等待用户
        确认期间其他归档/手动编辑改动状态导致前后不一致。
        prev_state_override：显式指定基准状态（重归档用「本章之前」的状态，
        否则本章变化会在旧归档结果之上叠加两次）。
        allow_empty：True 时"本章无任何变化"也返回 ok 的空 diff（自动归档/流式
        手动归档用），不触发重试。
        返回 {ok, diff, text, error, log_id}；diff 为统一结构（见 _normalize_diff）。
        """
        ch = self.store.get_chapter(chapter_id)
        if not ch:
            return {"ok": False, "error": "章节不存在"}
        if not self.chapter_full_text(chapter_id).strip():
            return {"ok": False, "error": "本章还没有已接受的正文，无法归档"}
        prev_state = (prev_state_override if prev_state_override is not None
                      else self.store.current_memory())
        retries = max(0, self.store.get_setting_int("memory_retries", 3))
        last_err = ""
        context, base_task = self.build_memory_extract(chapter_id, prev_state)

        for attempt in range(retries + 1):
            task = base_task
            if attempt > 0:
                task += f"\n\n【重要】上次输出解析失败：{last_err}\n请只输出合法的 JSON，不要输出任何解释文字。"
            r = self.pipeline.run("memory", context, task, stream=False, on_chunk=on_chunk)
            if not r["ok"]:
                return r
            diff, err = self.parse_extract_result(r["text"], prev_state, allow_empty=allow_empty)
            if err:
                last_err = err
                continue
            # 程序兜底：模型漏报死亡时，从正文里补一条待确认的死亡项
            diff = self._append_death_hints(diff, prev_state, chapter_id)
            # 冻结基准状态随行返回：归档时以它为基准应用差异
            r["prev_state"] = prev_state
            return {**r, "diff": diff}
        return {"ok": False, "error": f"记忆提取连续 {retries + 1} 次解析失败：{last_err}。"
                                      "请检查记忆角色绑定的模型是否支持 JSON 输出，或调低 max_tokens。"}

    def build_memory_extract(self, chapter_id: int, prev_state: dict,
                             retry_err: str = "") -> tuple[dict, str]:
        """组装记忆提取请求的 context 与 task（流式对话框与普通提取共用同一套）。

        prev_state 由调用方冻结一次；retry_err 非空时把上次解析错误反馈给模型。
        返回 (context, task)。"""
        ch = self.store.get_chapter(chapter_id)
        names = (ch or {}).get("characters") or []
        base = render_task("memory", self.store)
        base += self._foreshadow_extract_rules()
        base = self._death_extract_rules(base)
        if retry_err:
            base += f"\n\n【重要】上次输出解析失败：{retry_err}\n请只输出合法的 JSON，不要输出任何解释文字。"
        context = {"prev_state": prev_state, "prose": self.chapter_full_text(chapter_id),
                   "character_names": names, "chapter": ch}
        return context, base

    def _foreshadow_extract_rules(self) -> str:
        """程序级追加的伏笔台账判定规则（不依赖用户自定义模板，2026-09-08）。

        此前回收识别不准的根因：①提取模型看不到现有台账，只能凭本章内容
        自由发挥；②核销按原文精确匹配，模型照抄走样一个字就永远核销不掉；
        ③回收/埋设格式规则只在用户自定义模板里，内置无兜底。现在把当前
        未回收台账（含预定回收章）注入 task，并明确「照抄原文 + 可废弃 +
        预定回收写章号」。
        """
        try:
            states = self.store.foreshadow_states()
        except Exception:
            states = {}
        open_items = states.get("open") or []
        expired = states.get("expired") or []
        lines = ["\n\n【伏笔台账判定（程序规则，优先级高于自定义模板中的同类规则）】",
                 "归档输出 JSON 的 foreshadow 字段含三栏：planted（本章新埋设）、"
                 "paid_off（本章回收）、dropped（本章明确放弃/证伪）。规则：",
                 "1. 本章埋下新伏笔（悬念、未解之谜、承诺、信物来历等）→ 写进 planted："
                 "content 用一句话概括；预定回收章号写进 due_ch（整数，能预估才写，"
                 "写不了留空）；payoff_target 可写「第N章」形式。",
                 "2. 对照下方现有台账：本章正文实际回收了某条 → 写进 paid_off，"
                 "content 必须逐字照抄该条台账原文（程序按原文核销，改写会导致核销失败）。",
                 "3. 台账中某条剧情已明显放弃、不可能回收 → 写进 dropped（同样照抄原文）。"
                 "拿不准就不要动它——宁缺勿滥，误报会污染台账。",
                 "4. 本章没有涉及任何台账伏笔时，paid_off 与 dropped 留空数组。"]
        if open_items or expired:
            lines.append("现有未回收台账：")
            for p in open_items[:30]:
                bits = []
                if p.get("planted_ch"):
                    bits.append(f"埋设第{p['planted_ch']}章")
                if p.get("payoff_target"):
                    bits.append(f"预定回收{p['payoff_target']}")
                lines.append(f"- {p.get('content', '')}"
                             + (f"（{'；'.join(bits)}）" if bits else ""))
            if expired:
                lines.append(f"（另有 {len(expired)} 条已过预定回收章自动过期，无需处理）")
        else:
            lines.append("（当前台账无未回收伏笔）")
        return "\n".join(lines)

    def _death_extract_rules(self, base: str) -> str:
        """程序级死亡规则（口径同伏笔台账规则）。

        死亡判定以记忆模型为主（它读得懂含蓄写法），程序正则只是备用补漏。
        这里补两段不依赖模板的规则：
        ① 基础规则：自定义模板漏写 dead 字段时兜底追加；
        ② 含蓄写法细则：无论模板有没有死亡条款都追加（老板模板里只有「明示或
           可明确判定」一句，模型遇到「手垂了下去」「此后世上再没有他」这类
           侧面写法往往不敢判 → 漏记）。模板已写细则时不重复追加。
        """
        out = base
        # ① 基础规则
        if '"dead"' not in out and "死亡" not in out and "已故" not in out:
            out += (
            "\n\n【死亡判定（程序规则，优先级高于自定义模板中的同类规则）】"
            "本章有人物死亡（正文明示或可明确判定）时，必须在 changes 中输出 "
            '{"name": 人物名, "field": "dead", "new": true, "reason": "…"}；'
            "同时把死亡写进 physical_state（如「已故（第X章·断魂崖）」）与 injuries（致命伤）作为正文依据。"
            "已故人物此后不再有位置/衣物/体力/姿势等新变化，不要再输出他们的 "
            "location/clothing/physical_state/posture 更新；"
            "尸体、遗物、他人追念等非活性后续可照常记入 entities 或他人状态。")
        # ② 含蓄写法细则（2026-09-09 老板反馈「小说写得含蓄，AI 不敢判死」）
        # 去重只看本段标题：用「暗示/侧面」等词判重会撞上模板里的时间规则措辞
        if "死亡识别" not in out:
            out += (
                "\n\n【死亡识别·含蓄写法（程序规则）】"
                "小说很少直写「他死了」，以下都算已给出可确认的死亡结果，命中即输出 field=dead："
                "1) 生命终结直述：断气、气绝、咽气、没了气息/心跳、停止呼吸、殒命、毙命、"
                "丧命、阵亡、殉职/殉国、身亡、去世、逝世、走了（语境为死）；"
                "2) 致命伤 + 结果描写：要害中刀/中箭/重创后「不再动」「手垂了下去」"
                "「眼睛失去神采」「血止不住」「再没站起来」「再没醒过来」；"
                "3) 侧面/事后确认：入殓、棺木、下葬、坟、墓碑、遗体、尸身、收尸、发丧、"
                "吊唁、追悼、衣冠冢、「此后世上再没有他」「江湖再无此人」；"
                "4) 他人确定性转述：「他已经不在了」「没救回来」「没能救回来」"
                "「他没撑过去」「去时很安详」等已确认死亡的说法。"
                "以下一律不判死：受伤、昏迷、失踪、被带走、命悬一线、生死不明、下落不明、"
                "「不知是死是活」——等后续章节确认死亡后再输出。"
                "含蓄写法与「重伤未死」难分时，看有没有葬/殓/遗体/他人确述这类结果性描写；"
                "仍拿不准就不要输出（误判把活人标死的代价同样很高），"
                "但一旦确认死亡，reason 里必须写明正文依据。")
        return out

    def _append_death_hints(self, diff: dict, prev_state: dict, chapter_id: int) -> dict:
        """备用补漏：正文里死了人但模型没报 → 补一条**默认不勾选**的死亡项。

        死亡判定以记忆模型为主（含蓄写法只有模型读得懂），程序正则只认明写的
        死亡词，识别面窄且容易误判，所以定位是「备用」：
        - 确认界面默认**不勾选**，界面标「程序检测（备用）」，老板核对后自行勾选；
        - 自动归档不采纳（noop 项直接跳过），不会自动把活人标死；
        只补「记忆里已有人物」，已故的、本章已报过 dead 的都不重复报。
        """
        try:
            prose = self.chapter_full_text(chapter_id)
        except Exception:
            return diff
        if not prose.strip():
            return diff
        chars = (prev_state or {}).get("characters") or {}
        names: list[str] = []
        alias_of: dict = {}
        for n, c in chars.items():
            key = str(n).strip()
            if not key or not isinstance(c, dict) or c.get("dead"):
                continue
            names.append(key)
            for a in (c.get("aliases") or []):
                a = str(a).strip()
                if a:
                    names.append(a)
                    alias_of.setdefault(a, key)
        if not names:
            return diff
        reported = {str(it.get("name") or "").strip()
                    for it in (diff.get("items") or [])
                    if it.get("field") == "dead"}
        for hit in detect_deaths_in_prose(prose, names):
            canon = alias_of.get(hit["name"], hit["name"])
            if canon in reported or hit["name"] in reported:
                continue
            reported.add(canon)
            diff.setdefault("items", []).append({
                "key": f"death_hint|{canon}",
                "kind": "death_hint",
                "name": canon,
                "field": "dead",
                "old": "",
                "new": True,
                "reason": f"程序检测（备用）：正文出现「{hit['word']}」——{hit['evidence']}"
                          "（词面匹配可能误判，请核对正文后再勾选）",
                # default_off：确认界面默认**不勾选**（可勾选，勾选即生效），
                # 自动归档跳过（不自动把活人标死）。与 noop 的区别：noop 是
                # 「无实际变化」，勾选也不应用；这条是有变化的，只是要人工确认。
                "default_off": True,
                "noop": False,
            })
        return diff

    def parse_extract_result(self, text: str, prev_state: dict,
                             allow_empty: bool = False) -> tuple[dict | None, str]:
        """模型原文 → (diff, err)。err 非空表示需要重试。

        allow_empty=True 时"无变化"返回 (空 diff, "")；否则返回 (None, 错误信息)。"""
        obj, err = extract_json(text)
        if err:
            return None, err
        if not isinstance(obj, dict):
            return None, "顶层不是 JSON 对象"
        diff = self._normalize_diff(obj, prev_state)
        led = diff.get("foreshadow") or {}
        if (not diff["items"] and not diff["summary"] and not diff["events"]
                and not led.get("planted") and not led.get("paid_off")
                and not led.get("dropped")):
            if not allow_empty:
                return None, "输出为空（没有提取到任何变化）"
            return diff, ""
        return diff, ""

    def _normalize_diff(self, obj: dict, prev_state: dict) -> dict:
        """模型原始输出 → 统一差异结构。"""
        items = []
        for i, raw in enumerate(obj.get("changes") or []):
            if not isinstance(raw, dict):
                continue
            name = str(find_field(raw, "name", "character", default="")).strip()
            if not name:
                continue
            if find_field(raw, "new_character", default=False):
                card = find_field(raw, "card", "state", default={}) or {}
                if not isinstance(card, dict):
                    card = {}
                cleaned = {}
                for k, v in card.items():
                    nk = normalize_field(k)
                    if not nk or nk == "name":
                        continue
                    cleaned[nk] = normalize_list(v) if nk in LIST_FIELDS else (v if v is not None else "")
                items.append({
                    "key": f"new|{name}|{i}",
                    "kind": "new_character",
                    "name": name,
                    "field": "",
                    "old": "",
                    "new": cleaned,
                    "reason": str(find_field(raw, "reason", default="")),
                })
                continue
            field = normalize_field(find_field(raw, "field", "key", default=""))
            if not field or field in ("name", "changes"):
                continue
            new_val = find_field(raw, "new", default="")
            old_val = find_field(raw, "old", default="")
            if field == "knowledge":
                new_val = normalize_knowledge(new_val)
                old_val = normalize_knowledge(old_val)
            elif field == "inferences":
                new_val = normalize_inferences(new_val)
                old_val = normalize_inferences(old_val)
            elif field in LIST_FIELDS:
                new_val = normalize_list(new_val)
                old_val = normalize_list(old_val)
            if field == "relationships" and isinstance(new_val, list):
                rels = []
                for rel in new_val:
                    if isinstance(rel, dict):
                        rels.append({"target": str(rel.get("target", "")),
                                     "status": str(rel.get("status", ""))})
                    else:
                        rels.append({"target": "（关系对象）", "status": str(rel)})
                new_val = rels
            items.append({
                "key": f"{name}|{field}|{i}",
                "kind": "update",
                "name": name,
                "field": field,
                "old": old_val,
                "new": new_val,
                "reason": str(find_field(raw, "reason", default="")),
                "noop": old_val == new_val,  # 无实际变化：确认界面默认驳回、自动归档直接跳过
            })
        # 实体状态：只收本章变化的实体，old 取上一版状态中同名实体
        ws_prev = {}
        if prev_state:
            ws_prev = (prev_state.get("world_state") or {}).get("entities") or {}
        for i, raw in enumerate(obj.get("entities") or []):
            if not isinstance(raw, dict):
                continue
            ename = str(find_field(raw, "name", default="")).strip()
            if not ename:
                continue
            prev_ent = ws_prev.get(ename)
            removed = bool(find_field(raw, "removed", "remove", "consumed", default=False))
            if removed:
                # 实体已消失/消耗/销毁：用户确认后从世界状态中删除该条
                noop = prev_ent is None
                items.append({
                    "key": f"entity_remove|{ename}|{i}",
                    "kind": "entity_remove",
                    "name": ename,
                    "field": "",
                    "old": prev_ent if isinstance(prev_ent, dict) else "",
                    "new": "（已消失，从世界状态移除）",
                    "reason": str(find_field(raw, "reason", default="")),
                    "noop": noop,
                })
                continue
            raw_aliases = find_field(raw, "aliases", "别名", "简称", default=None)
            aliases = [a.strip() for a in normalize_list(raw_aliases) if str(a).strip()]
            new_val = {
                "kind": str(find_field(raw, "kind", default="")),
                "owner": str(find_field(raw, "owner", default="")),
                "location": str(find_field(raw, "location", default="")),
                "status": str(find_field(raw, "status", default="")),
                "aliases": aliases,
            }
            # 实体四字段全等且没有新别名 = 无实际变化（模型把未变实体也报出来时过滤）
            old_alias_set = set()
            if isinstance(prev_ent, dict) and isinstance(prev_ent.get("aliases"), list):
                old_alias_set = {str(a).strip() for a in prev_ent["aliases"] if str(a).strip()}
            noop = isinstance(prev_ent, dict) and all(
                str(prev_ent.get(k) or "") == new_val[k] for k in ("kind", "owner", "location", "status")) \
                and set(aliases) <= old_alias_set
            items.append({
                "key": f"entity|{ename}|{i}",
                "kind": "entity",
                "name": ename,
                "field": "",
                "old": prev_ent if isinstance(prev_ent, dict) else "",
                "new": new_val,
                "reason": str(find_field(raw, "reason", default="")),
                "noop": noop,
            })
        world = find_field(obj, "world_state", "world", default={})
        world = world if isinstance(world, dict) else {}
        ws_norm = {}
        for k, v in world.items():
            nk = {"时间": "time", "地点": "location", "备注": "notes"}.get(k, k)
            if nk == "rumors":
                rumors = []
                for r in (v if isinstance(v, list) else []):
                    if isinstance(r, dict) and str(r.get("content") or "").strip():
                        rumors.append({"content": str(r.get("content")).strip(),
                                       "scope": str(r.get("scope") or "").strip(),
                                       "time": str(r.get("time") or r.get("时间") or "").strip()})
                if rumors:
                    ws_norm["rumors"] = rumors
            elif nk in ("time", "location", "notes"):
                if v is not None:
                    ws_norm[nk] = str(v)
        # 伏笔台账（2026-09-07）：planted/paid_off/dropped 归一化并入 diff；
        # open/expired 由程序推导不解析（foreshadow_states）。due_ch 为预定回收
        # 章号（数字容错），payoff_target 保留原文供显示与解析兜底。
        led = find_field(obj, "foreshadow_ledger", "foreshadow", "伏笔台账", default=None)
        planted: list = []
        paid_off: list = []
        dropped: list = []

        def _norm_due(v) -> int | None:
            try:
                n = int(float(str(v).strip()))
                return n if n > 0 else None
            except Exception:
                return None

        if isinstance(led, dict):
            for p in (led.get("planted") or []):
                if isinstance(p, dict):
                    content = str(p.get("content") or p.get("fact") or "").strip()
                    if content:
                        planted.append({"content": content,
                                        "source": str(p.get("source") or "").strip(),
                                        "payoff_target": str(p.get("payoff_target")
                                                             or p.get("target") or "").strip(),
                                        "due_ch": _norm_due(p.get("due_ch")
                                                            or p.get("payoff_chapter")),
                                        "reason": str(p.get("reason") or "").strip()})
                elif str(p).strip():
                    planted.append({"content": str(p).strip(), "source": "",
                                    "payoff_target": "", "due_ch": None, "reason": ""})
            for p in (led.get("paid_off") or led.get("paidoff") or []):
                if isinstance(p, dict):
                    content = str(p.get("content") or p.get("fact") or "").strip()
                    if content:
                        paid_off.append({"content": content,
                                         "planted_in": str(p.get("planted_in") or "").strip(),
                                         "reason": str(p.get("reason") or "").strip()})
                elif str(p).strip():
                    paid_off.append({"content": str(p).strip(), "planted_in": "", "reason": ""})
            for p in (led.get("dropped") or led.get("abandoned") or led.get("废弃") or []):
                if isinstance(p, dict):
                    content = str(p.get("content") or p.get("fact") or "").strip()
                    if content:
                        dropped.append({"content": content,
                                        "reason": str(p.get("reason") or "剧情放弃").strip()})
                elif str(p).strip():
                    dropped.append({"content": str(p).strip(), "reason": "剧情放弃"})
        return {
            "summary": str(find_field(obj, "summary", default="")),
            "events": [str(x) for x in (find_field(obj, "events", default=[]) or []) if str(x).strip()],
            "world_state": ws_norm,
            "items": items,
            "foreshadow": {"planted": planted, "paid_off": paid_off, "dropped": dropped},
        }

    # ---------- 应用差异 → 新状态 ----------

    def apply_diff(self, diff: dict, verdicts: dict, base_state: dict | None = None) -> dict:
        """按用户逐条确认结果，从基准状态生成新 full_state。

        verdicts: {item.key: {"action": "accept"|"reject", "new": 修改后的值}}
        base_state: 差异提取时冻结的基准状态；None 时取当前有效记忆（旧行为）。
        """
        prev = base_state if base_state is not None else self.store.current_memory()
        state = copy.deepcopy(prev)
        chars = state.setdefault("characters", {})
        # 别名 → 正式名：模型常拿别名（「青令」）输出变化，不解析就会新建一张
        # 同名不同键的状态卡，真正的人物卡永远收不到更新（死亡尤其致命）。
        amap: dict = {}
        for n, c in (prev.get("characters") or {}).items():
            if not isinstance(c, dict) or not isinstance(c.get("aliases"), list):
                continue
            for a in c["aliases"]:
                a = str(a).strip()
                if a and a != n:
                    amap.setdefault(a, str(n).strip())
        for it in diff.get("items") or []:
            if it.get("kind") in ("entity", "entity_remove"):
                continue  # 实体状态单独处理（见下方实体段落）
            if it.get("noop"):
                continue  # 无实际变化项：跳过，避免"假变动"覆盖旧值
            if it.get("default_off") and it["key"] not in verdicts:
                # 程序备用检测项（如死亡补漏）：只在被显式勾选（accept）时应用。
                # 其它项默认「缺失=采纳」是自动归档的全采纳语义，这条必须反过来，
                # 否则没有人工核对的兜底结果会被自动写进记忆。
                continue
            v = verdicts.get(it["key"], {"action": "accept"})
            if v.get("action") == "reject":
                continue
            new_val = v.get("new", it["new"])
            # 防御：用户确认/模型输出可能给 None，回退为字符串空值
            if new_val is None:
                new_val = ""
            if it["kind"] == "new_character":
                card = new_character_card(it["name"])
                for k, val in new_val.items() if isinstance(new_val, dict) else []:
                    if val is not None:
                        card[k] = val
                chars[it["name"]] = card
            else:
                raw_name = it["name"]
                # 已存在同名卡时不动（正式名优先）；只有查无此人才试别名
                name = raw_name if raw_name in chars else amap.get(raw_name, raw_name)
                card = chars.setdefault(name, new_character_card(name))
                if not isinstance(card, dict):
                    # 旧数据/手动编辑把该人物写成了非对象，重建卡片兜底
                    card = new_character_card(name)
                    chars[name] = card
                if it["field"] == "aliases":
                    # 别名只增不减：与既有别名并集合并（整字段替换会丢历史别名）
                    old_aliases = card.get("aliases") if isinstance(card.get("aliases"), list) else []
                    merged = [str(a).strip() for a in old_aliases if str(a).strip()]
                    for a in (new_val if isinstance(new_val, list)
                              else ([new_val] if str(new_val).strip() else [])):
                        a = str(a).strip()
                        if a and a not in merged:
                            merged.append(a)
                    card["aliases"] = merged
                elif it["field"] == "dead":
                    # 死亡标记：模型可能输出 true/"已故"/"死亡"/1，统一成布尔
                    card["dead"] = _to_bool_dead(new_val)
                else:
                    card[it["field"]] = new_val
        # 实体状态：整条记录替换语义，仅采纳未驳回的实体；removed 实体从状态中删除
        ents = state.setdefault("world_state", {}).setdefault("entities", {})
        for it in diff.get("items") or []:
            if it.get("kind") not in ("entity", "entity_remove"):
                continue
            if it.get("noop"):
                continue
            v = verdicts.get(it["key"], {"action": "accept"})
            if v.get("action") == "reject":
                continue
            if it["kind"] == "entity_remove":
                ents.pop(it["name"], None)
                continue
            new_val = v.get("new", it["new"])
            if isinstance(new_val, dict):
                # 别名只增不减：实体是整条替换语义，历史别名必须从旧记录并入（防丢）
                old_ent = ents.get(it["name"])
                if isinstance(old_ent, dict) and isinstance(old_ent.get("aliases"), list):
                    merged = [str(a).strip() for a in old_ent["aliases"] if str(a).strip()]
                    for a in (new_val.get("aliases") or []):
                        a = str(a).strip()
                        if a and a not in merged:
                            merged.append(a)
                    new_val["aliases"] = merged
                ents[it["name"]] = new_val
        ws = diff.get("world_state") or {}
        state_ws = state.setdefault("world_state", {})
        for k, v in ws.items():
            if k == "rumors" and isinstance(v, list):
                existing = {}
                for r in state_ws.get("rumors") or []:
                    if isinstance(r, dict) and r.get("content"):
                        existing[r["content"]] = r
                for r in v:
                    if isinstance(r, dict) and r.get("content"):
                        old = existing.get(r["content"])
                        if old and not r.get("time") and old.get("time"):
                            r["time"] = old["time"]  # 新值漏带时间时继承旧时间，防丢失
                        existing[r["content"]] = r  # 覆盖 scope（范围扩大）
                state_ws["rumors"] = list(existing.values())
            elif v:
                state_ws[k] = v
        sync_dead_flags(state)   # 兜底：模型只把「已故」写进自由文本时补上结构化标记
        return state

    def archive(self, chapter_id: int, diff: dict, verdicts: dict,
                summary_override: str = "", events_override: list | None = None,
                base_state: dict | None = None, replace: bool = False) -> int:
        """归档：应用确认结果 → 写入新快照 → 章节状态→已归档。

        base_state 应传 extract_diff 返回的 prev_state（与提取时展示给模型/用户
        的基准一致）；不传则取当前有效记忆（兼容旧调用）。
        replace=True 为重归档（本章正文重写后再归档）：替换本章原快照而不是追加
        新版本——否则同一章会在记忆链上留下两条快照、状态被叠加。同时把本章及其后
        的快照标记过期，后续章由记忆页的「批量重归档」提示重建。
        """
        full_state = self.apply_diff(diff, verdicts, base_state)
        # 实体「已消失」被采纳 → 设定库同名条目标记软移除（2026-09-08 第七轮）。
        # 必须与记忆同步，否则设定库仍把已销毁的东西注入后续章节；用软移除
        # 而非删除，误判可在设定库页一键恢复。
        try:
            _names = accepted_entity_removals(diff, verdicts)
            if _names:
                order = (self.store.get_chapter(chapter_id) or {}).get("order_index") or 0
                self.store.mark_lore_removed(_names, order)
            # 反向：已移除条目若重新出现在当前世界状态（销毁后修复/重现），
            # 自动取消标记——否则自动导入认为「同名已存在」不会重建，条目永远注入不了
            self.store.unmark_lore_removed(
                (full_state.get("world_state") or {}).get("entities") or {})
        except Exception:
            pass
        summary = summary_override or diff.get("summary", "")
        events = events_override if events_override is not None else diff.get("events", [])
        if replace and self.store.has_memory_snapshot(chapter_id):
            self.store.mark_stale_from(chapter_id)
            version = self.store.replace_snapshot(chapter_id, full_state, diff, summary, events)
            self.store.set_chapter_status(chapter_id, "已归档")
        else:
            version = self.store.archive_chapter(chapter_id, full_state, diff, summary, events)
        # 归档章节补全出场人物：记忆快照里的角色卡片并入本章（右栏状态卡/资料速查/正文注入
        # 都依赖 chapter.characters，此前可能长期为空导致右栏空白）
        self.store.merge_chapter_characters(
            chapter_id, list((full_state.get("characters") or {}).keys()))
        # 归档完成即清空本章动态：章内临时记忆已被摘要/事件取代，避免与下轮动态混淆
        self.store.clear_notes(chapter_id)
        # 伏笔台账并入（幂等去重）：planted 只增、paid_off/dropped 核销累积；失败不阻塞归档
        led = diff.get("foreshadow") or {}
        if led.get("planted") or led.get("paid_off") or led.get("dropped"):
            ch_no = (self.store.get_chapter(chapter_id) or {}).get("order_index")
            try:
                self.store.merge_foreshadow_ledger(led.get("planted"), led.get("paid_off"),
                                                   ch_no, led.get("dropped"))
            except Exception:
                pass
        # 设定库沉睡基准：记录本章提到的设定条目（last_seen_chapter），失败不阻塞
        self.store.touch_lore_mentions(chapter_id)
        self.index_vectors_async(version, full_state, events)
        self._check_lore_coverage(full_state)
        # 任意路径归档成功（含手动）都消除本项目「自动归档失败」提醒：
        # 只在 archive_auto 里清的话，手动补归档成功后提醒会残留不消
        clear_issue("auto_archive", project=_project_tag(self.store))
        return version

    def _check_lore_coverage(self, full_state: dict):
        """归档后设定库联动（记忆 → 设定库闭环，2026-09-08 升级为自动导入）。

        默认（auto_lore_import 开）自动把记忆里设定库缺失的人物/实体导入设定库
        （同名跳过、内容注明来源，用户可再整理）——老板反馈"每次都要手动导入太
        繁琐"，现在归档即同步，无感知。关闭开关时退回"只提醒"模式（挂 lore_sync
        诊断提醒，提醒栏可一键导入）。全部异常不阻塞归档。
        """
        try:
            from .diagnostics import clear_issue, record_issue
            from .store import lore_import_from_memory, lore_import_preview
            project = _project_tag(self.store)
            if self.store.get_setting_int("auto_lore_import", 1):
                imported = lore_import_from_memory(self.store)
                if imported:
                    clear_issue("lore_sync", project=project)
                else:
                    clear_issue("lore_sync", project=project)  # 无缺失也清除旧提醒（防御残留）
            else:
                missing = lore_import_preview(self.store)
                if not missing:
                    clear_issue("lore_sync", project=project)
                    return
                names = "、".join(n for _c, n, _x in missing[:6]) + \
                    ("…" if len(missing) > 6 else "")
                record_issue("lore_sync",
                             "设定资料库缺卡",
                             f"记忆里有 {len(missing)} 个对象还没有设定条目：{names}",
                             hint="设定卡会随状态卡一起注入，人设更完整；"
                                  "可到「设定资料库」页点「从记忆导入全部」补齐，"
                                  "或到设置开启「归档后自动导入设定库」。",
                             project=project)
        except Exception:
            pass  # 覆盖检查是锦上添花，任何异常不阻塞归档

    def archive_auto(self, chapter_id: int, re_archive: bool | None = None) -> dict:
        """完全自动归档：提取差异 → 全接受 → 写新快照，无任何确认（auto_archive 开关用）。

        全流程设计为在 worker 线程调用。无变化章（allow_empty）也归档，只写摘要/事件。
        re_archive：本章已归档过时是否走「重归档」（替换原快照、以本章之前的状态为
        基准）。None = 自动判断（有本章快照即重归档）——正文重新生成后再次自动归档
        走的就是这条路，避免同章快照堆叠与状态叠加。
        返回 {ok, version, error, replaced}；有误时用户通过记忆页回滚/重置处理。
        """
        project = _project_tag(self.store)
        if re_archive is None:
            re_archive = self.store.has_memory_snapshot(chapter_id)
        base = self.store.memory_state_before_chapter(chapter_id) if re_archive else None
        r = self.extract_diff(chapter_id, allow_empty=True, prev_state_override=base)
        if not r["ok"]:
            ch = self.store.get_chapter(chapter_id) or {}
            record_issue("auto_archive",
                         "自动归档失败",
                         f"第{ch.get('order_index', '?')}章：{r.get('error', '提取失败')}"[:400],
                         hint="可在「记忆」页手动归档该章；成功归档后本提醒自动消失。",
                         project=project)
            return {"ok": False, "error": r.get("error", "提取失败")}
        diff = r["diff"]
        # default_off（程序备用检测项）不自动采纳：没有人工核对，宁可不标死
        verdicts = {it["key"]: {"action": "accept", "new": it["new"]}
                    for it in diff.get("items") or []
                    if not it.get("noop") and not it.get("default_off")}
        try:
            version = self.archive(chapter_id, diff, verdicts,
                                   base_state=r.get("prev_state"), replace=re_archive)
        except Exception as e:
            record_issue("auto_archive",
                         "自动归档失败",
                         f"第{chapter_id}章写入失败：{e}"[:400],
                         hint="可在「记忆」页手动归档该章；成功归档后本提醒自动消失。",
                         project=project)
            return {"ok": False, "error": f"归档写入失败：{e}"}
        clear_issue("auto_archive", project=project)  # 本次归档成功：消除本项目「自动归档失败」提醒
        return {"ok": True, "version": version, "error": "", "replaced": bool(re_archive)}

    # ---------- 记忆向量（语义检索索引） ----------

    def index_vectors_async(self, snapshot_version: int, full_state: dict,
                            events: list | None = None):
        """归档后异步生成记忆向量（失败静默不阻塞归档，但登记诊断提醒；未配置嵌入则跳过）。"""
        project = _project_tag(self.store)

        def worker():
            if getattr(self.store, "is_closed", False):
                return  # 项目已关闭/已切换：放弃本次，下次归档或手动重建会补
            try:
                self._index_vectors(snapshot_version, full_state, events)
            except Exception as e:
                if _is_closed_db_error(e):
                    return  # 建索引途中项目被关掉：切换竞态属正常退出，不算失败
                record_issue("vector_index",
                             "记忆向量索引生成失败",
                             f"版本 v{snapshot_version}：{e}"[:400],
                             hint="下次归档会自动重试；也可以到「记忆」页点「重建语义向量」。",
                             project=project)
        threading.Thread(target=worker, daemon=True).start()

    def _index_vectors(self, snapshot_version: int, full_state: dict,
                       events: list | None = None):
        embed = load_embed_config(self.store)
        if not embed:
            return
        units = memory_units(full_state, events)
        if not units:
            return
        texts = [u["text"] for u in units]
        vecs = embed_texts(embed["base_url"], embed["api_key"], embed["model"], texts)
        if not vecs or len(vecs) != len(texts):
            return
        for u, v in zip(units, vecs):
            u["vector"] = v
        self.store.save_memory_vectors(snapshot_version, units)
        clear_issue("vector_index", project=_project_tag(self.store))  # 索引成功：消除本项目的失败提醒

    # ---------- 级联重归档 ----------

    def rearchive_from(self, chapter_id: int, confirm: callable, on_chunk=None) -> dict:
        """中间章改写后：从该章起逐章重新提取差异、逐章确认（confirm 回调由 UI 提供）。

        confirm(chapter, diff, attempt) → {"accepted": bool, "verdicts": {...}, "summary": str, "events": [...]}
        返回 {ok, count, error}
        """
        chapters = self.store.snapshot_chapters_to_rearchive(chapter_id)
        if not chapters:
            return {"ok": False, "error": "没有需要重归档的章节（该章尚未归档）"}
        done = 0
        pending_vectors: list = []  # 收集后串行建向量（避免逐章并发线程打爆嵌入接口）
        prev_state = None  # 第一章从当前记忆取；之后用上一章重归档的结果递进
        for i, ch in enumerate(chapters):
            if on_chunk:
                on_chunk(f"正在重归档第{ch['order_index']}章…")
            # allow_empty：改写后某章确实无状态变化是合法的，不能中断整条重归档链；
            # 空差异时沿用原快照摘要/事件，仅刷新基准（跳过确认弹窗）
            r = self.extract_diff(ch["id"], allow_empty=True)
            if not r["ok"]:
                return {"ok": False, "count": done, "error": f"第{ch['order_index']}章提取失败：{r['error']}"}
            diff = r["diff"]
            if not diff.get("items") and not diff.get("summary") and not diff.get("events"):
                snap = self.store.latest_memory_snapshot_row(ch["id"])
                base = prev_state if prev_state is not None else r.get("prev_state")
                if snap is not None:
                    full_state = self.apply_diff(diff, {}, base)
                    version = self.store.replace_snapshot(
                        ch["id"], full_state, diff,
                        snap.get("summary") or "", snap.get("events") or [])
                    pending_vectors.append((version, full_state, snap.get("events") or []))
                    prev_state = full_state
                done += 1
                continue
            decision = confirm(ch, diff, i)
            if not decision.get("accepted"):
                continue
            base = prev_state if prev_state is not None else r.get("prev_state")
            full_state = self.apply_diff(diff, decision.get("verdicts", {}), base)
            summary = decision.get("summary") or diff.get("summary", "")
            events = decision.get("events") if decision.get("events") is not None else diff.get("events", [])
            version = self.store.replace_snapshot(ch["id"], full_state, diff, summary, events)
            self.store.clear_notes(ch["id"])  # 重归档后本章动态已过时，一并清空
            # 归档时同步补全出场人物（右栏状态卡/资料速查依赖 chapter.characters）
            self.store.merge_chapter_characters(
                ch["id"], list((full_state.get("characters") or {}).keys()))
            # 伏笔台账并入（幂等去重，重归档重复提取不会重复记账）
            led = diff.get("foreshadow") or {}
            if led.get("planted") or led.get("paid_off") or led.get("dropped"):
                try:
                    self.store.merge_foreshadow_ledger(
                        led.get("planted"), led.get("paid_off"), ch.get("order_index"),
                        led.get("dropped"))
                except Exception:
                    pass
            # 设定库沉睡基准（重归档同样记录本章提及）
            self.store.touch_lore_mentions(ch["id"])
            pending_vectors.append((version, full_state, events))
            prev_state = full_state  # 递进：下一章基于本章的新状态继续提取
            done += 1
        if pending_vectors:
            self._index_vectors_serial(pending_vectors)
        return {"ok": True, "count": done, "error": ""}

    def _index_vectors_serial(self, pending: list):
        """串行为多份快照建向量（单个后台线程顺序处理，防并发嵌入请求限流）。"""
        project = _project_tag(self.store)

        def worker():
            for item in pending:
                version, full_state = item[0], item[1]
                events = item[2] if len(item) > 2 else []
                if getattr(self.store, "is_closed", False):
                    return  # 项目已关闭：剩余队列全部放弃（重归档后手动重建可补）
                try:
                    self._index_vectors(version, full_state, events)
                except Exception as e:
                    if _is_closed_db_error(e):
                        return  # 建索引途中项目被关掉：切换竞态属正常退出，不算失败
                    record_issue("vector_index",
                                 "记忆向量索引生成失败",
                                 f"版本 v{version}：{e}"[:400],
                                 hint="下次归档会自动重试；也可以到「记忆」页重建全部向量。",
                                 project=project)
        threading.Thread(target=worker, daemon=True).start()
