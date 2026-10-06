# -*- coding: utf-8 -*-
"""记忆页 + 归档确认（Flet 版，语义对齐桌面 memory_page.py / dialogs.py）。

桌面 `app/core` 是唯一业务源，本模块只做 Flet 界面编排，读写全部走 core：

- **记忆链**：一串版本快照（v0 初始状态 + 每章归档一次），diff 记录本次变化；
- **归档**：提取「本章相对上一版的变化」→ **逐条确认** → 写新快照。
  红线：差异不确认不入库（停止/取消/提取失败都不写）；
- **当前状态** = 最新未过期快照（`store.current_memory()`）。

双端数据统一：记忆全部存在**项目库 project.db**（一部小说 = 一个项目），
桌面与手机打开同一个项目文件夹就是同一份记忆；跨设备靠「我的 → 全量备份」
的 zip（内含每个项目的 project.db）搬运——不存在各存一份的问题。
"""
import json

import flet as ft

from widgets import section, hint, empty, toast, confirm, error_card
from bridge import run_bg

# 归档幂等提示（与桌面 _after_prose_done 同口径）
DEATH_HINTS_SRC = "程序备用检测"


# =====================================================
# 小工具
# =====================================================

def _jload(s, default=None):
    """容错解析 JSON 字段（数据库里这些列都是 JSON 字符串）。"""
    if isinstance(s, (dict, list)):
        return s
    try:
        return json.loads(s) if s else (default if default is not None else {})
    except (ValueError, TypeError):
        return default if default is not None else {}


def _short(v, n=200) -> str:
    if isinstance(v, (dict, list)):
        try:
            v = json.dumps(v, ensure_ascii=False)
        except (TypeError, ValueError):
            v = str(v)
    v = str(v or "").strip().replace("\n", " ")
    return v if len(v) <= n else v[:n] + "…"


def chapter_label(store, chapter_id) -> str:
    """章标签：无 chapter_id = 初始状态/手动编辑，依桌面 refresh 同口径。"""
    if not chapter_id:
        return "初始状态"
    ch = store.get_chapter(chapter_id)
    if not ch:
        return f"章节#{chapter_id}"
    t = str(ch.get("title") or "").strip()
    return f"第{ch.get('order_index')}章" + (f" {t}" if t else "")


def is_dead(v) -> bool:
    """已故判定的宽松版（与 core `_to_bool_dead` 同口径）。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v or "").strip().lower() in ("1", "true", "yes", "y", "是", "已故")


def _sheet(page, content: ft.Control, height_ratio=0.82):
    page.show_dialog(ft.BottomSheet(
        content=ft.Container(content, padding=14),
        show_drag_handle=True, use_safe_area=True,
    ))


def _sheet_height(page) -> int:
    return int(getattr(page, "height", 720) or 720)


# =====================================================
# 差异逐条确认（对齐桌面 DiffConfirmWidget）
# =====================================================

class DiffConfirm:
    """记忆差异逐条确认区。

    勾选 = 采纳（可改值），取消勾选 = 驳回；无变化项（noop）默认不勾且灰显不可勾；
    default_off（程序备用检测）可勾但默认不勾。collect() → (verdicts, summary, events)。
    """

    def __init__(self, store, chapter: dict, diff: dict):
        self.store = store
        self.chapter = chapter or {}
        self.diff = diff or {}
        self._rows: list = []           # [(checkbox, item)]
        self.summary_field = ft.TextField(
            label="本章摘要", value=self.diff.get("summary") or "",
            multiline=True, min_lines=2, max_lines=4, dense=True)
        self.events_field = ft.TextField(
            label="本章事件（每行一条）",
            value="\n".join(self.diff.get("events") or []),
            multiline=True, min_lines=2, max_lines=5, dense=True)
        self.control = self._build()

    # ---------- 构建 ----------

    def _build(self) -> ft.Control:
        warns = []
        rumors = ((self.diff.get("world_state") or {}).get("rumors")) or []
        no_time = [r for r in rumors if isinstance(r, dict)
                   and str(r.get("content") or "").strip()
                   and not str(r.get("time") or "").strip()]
        if no_time:
            warns.append(error_card(
                f"⚠ 本章提取到 {len(no_time)} 条传闻缺时间标注"
                "（time = 传闻所述事件发生的日期，不是被提起的时间）。"
                "缺时间的传闻无法判断新旧，建议回正文核对后手动补记；仍可继续归档。"))
        cards = [self._item_card(it) for it in (self.diff.get("items") or [])]
        if not cards:
            cards = [hint("（未提取到人物状态变化——只有摘要与事件）")]
        return ft.Column([
            hint("记忆模型从本章正文提取了以下状态变化。逐条确认：勾选=采纳（可改值），"
                 "取消勾选=驳回。确认后生成新记忆快照，旧版永久保留。"
                 "灰色项为无实际变化，默认驳回。"),
            *warns,
            ft.Column(cards, spacing=6),
            ft.Divider(height=1),
            self.summary_field,
            self.events_field,
        ], spacing=8, scroll=ft.ScrollMode.AUTO)

    def _item_card(self, it: dict) -> ft.Control:
        noop = bool(it.get("noop"))
        backup = bool(it.get("default_off")) and not noop
        chk = ft.Checkbox(value=(not noop and not backup), disabled=noop)
        kind = it.get("kind")
        field = it.get("field")
        name = it.get("name") or ""
        if kind == "new_character":
            title = f"新出场人物：{name}"
        elif kind == "entity":
            title = f"实体：{name}"
        elif kind == "entity_remove":
            title = f"实体移除：{name}（已消失，将从世界状态删除）"
        elif kind == "death_hint" or field == "dead":
            src = DEATH_HINTS_SRC if kind == "death_hint" else "AI 提取"
            title = f"☠ {name} · 标记已故（{src}）"
        else:
            title = f"{name} · {field}"
        if backup:
            title += "（待核对）"
        elif noop:
            title += "（无变化）"

        rows = [ft.Row([
            chk,
            ft.Text(title, size=13, weight=ft.FontWeight.W_600, expand=True),
        ], vertical_alignment=ft.CrossAxisAlignment.CENTER)]

        sub = ft.Colors.ON_SURFACE_VARIANT
        if kind == "entity_remove":
            rows.append(ft.Text(
                f"当前记录：{_short(it.get('old'))}"
                + (f"\n原因：{it.get('reason')}" if it.get("reason") else ""),
                size=11, color=sub))
        elif kind == "new_character":
            rows.append(ft.Text("完整状态卡：" + _short(it.get("new"), 600),
                                size=11, color=sub))
        elif kind == "entity":
            ent = it.get("new") if isinstance(it.get("new"), dict) else {}
            rows.append(ft.Text(
                f"类别：{ent.get('kind') or ''} ｜ 归属：{ent.get('owner') or ''}"
                f" ｜ 位于：{ent.get('location') or ''} ｜ 状态：{ent.get('status') or ''}",
                size=11, color=sub))
        else:
            old, new = _short(it.get("old")), _short(it.get("new"))
            if field == "dead":
                old = "已故" if is_dead(it.get("old")) else "存活"
                new = "已故" if is_dead(it.get("new")) else "存活"
            rows.append(ft.Text(
                f"旧：{old}\n新：{new}"
                + (f"\n原因：{it.get('reason')}" if it.get("reason") else ""),
                size=11, color=sub))
            if field == "knowledge" and isinstance(it.get("new"), list):
                missing = [f for f in it["new"] if isinstance(f, dict)
                           and str(f.get("fact") or "").strip()
                           and not str(f.get("source") or "").strip()]
                if missing:
                    rows.append(ft.Text(
                        f"⚠ {len(missing)} 条 knowledge 缺少来源"
                        "（应为 听说/亲眼看到/被告知/读到/亲身经历/公共传闻 之一）",
                        size=11, color=ft.Colors.ORANGE_700))
        if kind == "death_hint":
            rows.append(ft.Text(
                "⚠ 备用检测：AI 本次未上报死亡，程序按词面在正文里发现了死亡迹象，"
                "默认未勾选——确有其事请勾选，拿不准或系误判请忽略。",
                size=11, color=ft.Colors.ORANGE_700))

        if (kind not in ("new_character", "entity", "entity_remove")
                and not noop and not backup):
            rows.append(ft.Row([
                ft.TextButton("改值", on_click=lambda e, i=it: self._ask_value(i)),
            ], alignment=ft.MainAxisAlignment.END))

        self._rows.append((chk, it))
        return ft.Container(
            ft.Column(rows, spacing=2),
            bgcolor=(ft.Colors.with_opacity(0.04, ft.Colors.ON_SURFACE)
                     if (noop and not backup) else None),
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=8, padding=8)

    def _ask_value(self, it: dict):
        """改值：把该项的 new 换成用户输入（桌面「改值」同义）。"""
        page = getattr(self, "page_ref", None)
        if page is None:
            return
        tf = ft.TextField(label=f"{it.get('name')} · {it.get('field')} 的新值",
                          value=_short(it.get("new"), 400), multiline=True,
                          min_lines=1, max_lines=4, autofocus=True)

        def _ok(e):
            it["new"] = tf.value or ""
            page.pop_dialog()
            pair = self._detail.get(it.get("key"))
            if pair is not None:            # 就地刷新详情，勾选与收集逻辑不变
                ctrl, render = pair
                ctrl.value = render()
            try:
                page.update()
            except Exception:
                pass
            toast(page, f"已改为：{_short(it.get('new'), 40)}")

        page.show_dialog(ft.AlertDialog(
            title=ft.Text("修改新值"), content=tf,
            actions=[ft.TextButton("取消", on_click=lambda e: page.pop_dialog()),
                     ft.FilledButton("确定", on_click=_ok)]))

    # ---------- 收集 ----------

    def collect(self):
        """→ (verdicts, summary, events)"""
        verdicts = {}
        for chk, it in self._rows:
            verdicts[it["key"]] = {
                "action": "accept" if (chk.value and not chk.disabled) else "reject",
                "new": it.get("new")}
        summary = (self.summary_field.value or "").strip()
        events = [e.strip() for e in (self.events_field.value or "").splitlines() if e.strip()]
        return verdicts, summary, events


def _confirm_sheet(page, store, chapter: dict, diff: dict, on_accept, title="记忆归档确认"):
    """弹差异确认 sheet；用户点「确认归档」才回调 on_accept(verdicts, summary, events)。"""
    state = DiffConfirm(store, chapter, diff)
    state.page_ref = page       # 供「改值」弹窗用

    def _ok(e):
        page.pop_dialog()
        v, s, ev = state.collect()
        on_accept(v, s, ev)

    _sheet(page, ft.Column([
        ft.Text(title, size=16, weight=ft.FontWeight.BOLD),
        ft.Container(state.control, height=_sheet_height(page) * 0.62),
        ft.Row([
            ft.TextButton("取消（不入库）", on_click=lambda e: page.pop_dialog()),
            ft.FilledButton("确认归档", on_click=_ok),
        ], alignment=ft.MainAxisAlignment.END),
    ], spacing=8, scroll=ft.ScrollMode.AUTO))
    return state


# =====================================================
# 归档流程（提取 → 逐条确认 → 落库）
# =====================================================

def archive_flow(page, session, chapter_id: int, on_done=None, already_archived_ok=False):
    """章末归档完整流程（对齐桌面 MemoryArchiveDialog / run_archive_flow）。

    任何一步失败都不写入 —— 红线：差异不确认不入库。
    """
    from app.core.memory_service import MemoryService
    store = session.store
    ch = store.get_chapter(chapter_id)
    if not ch:
        toast(page, "章节不存在", error=True)
        return
    mem = MemoryService(store)
    if not mem.chapter_full_text(chapter_id).strip():
        toast(page, "本章还没有已接受的正文，无法归档", error=True)
        return
    re_archive = bool(store.has_memory_snapshot(chapter_id))
    prev_state = (store.memory_state_before_chapter(chapter_id)
                  if re_archive else store.current_memory())

    toast(page, f"正在提取第{ch.get('order_index')}章记忆差异…")
    run_bg(page,
           lambda: mem.extract_diff(chapter_id, allow_empty=True,
                                    prev_state_override=prev_state),
           lambda r: _archive_extracted(page, store, mem, chapter_id, ch, r,
                                        prev_state, re_archive, on_done))


def _archive_extracted(page, store, mem, chapter_id, ch, r, prev_state,
                       re_archive, on_done):
    if not isinstance(r, dict) or not r.get("ok"):
        err = (r or {}).get("error", "提取失败") if isinstance(r, dict) else "提取失败"
        toast(page, f"记忆提取失败：{err}", error=True)
        return
    diff = r.get("diff") or {}

    def _accept(verdicts, summary, events):
        try:
            version = mem.archive(chapter_id, diff, verdicts, summary, events,
                                  base_state=prev_state, replace=re_archive)
        except Exception as e:
            toast(page, f"归档写入失败：{e}", error=True)
            return
        toast(page, f"第{ch.get('order_index')}章已归档（记忆 v{version}）")
        if on_done:
            on_done(version)

    _confirm_sheet(page, store, ch, diff, _accept)


# =====================================================
# 批量重归档（异步逐章，避开跨线程阻塞确认）
# =====================================================

def rearchive_from(page, session, chapter_id: int, on_done=None):
    """自该章起逐章重新提取差异并逐章确认（对齐桌面 rearchive_from）。

    桌面走 worker 线程 + 主线程阻塞队列弹框；手机端改成「主线程驱动的异步链」：
    每章提取完成回到 UI 弹确认，确认后再进入下一章 —— 语义一致，避免跨线程阻塞。
    """
    from app.core.memory_service import MemoryService
    store = session.store
    mem = MemoryService(store)
    chapters = store.snapshot_chapters_to_rearchive(chapter_id)
    if not chapters:
        toast(page, "没有需要重归档的章节（该章尚未归档）", error=True)
        return
    total = len(chapters)

    def _step(i, prev_state):
        if i >= total:
            toast(page, f"重归档完成（共 {i} 章）")
            if on_done:
                on_done()
            return
        ch = chapters[i]
        toast(page, f"正在重归档第{ch.get('order_index')}章…（{i + 1}/{total}）")

        def _extracted(r):
            if not isinstance(r, dict) or not r.get("ok"):
                err = (r or {}).get("error", "提取失败") if isinstance(r, dict) else "提取失败"
                toast(page, f"第{ch.get('order_index')}章提取失败：{err}", error=True)
                if on_done:
                    on_done()
                return
            diff = r.get("diff") or {}
            base = prev_state if prev_state is not None else r.get("prev_state")
            # 空差异章：沿用原快照摘要/事件，仅刷新基准（与桌面同口径，不弹确认）
            if not (diff.get("items") or diff.get("summary") or diff.get("events")):
                snap = store.latest_memory_snapshot_row(ch["id"])
                if snap is not None:
                    full_state = mem.apply_diff(diff, {}, base)
                    store.replace_snapshot(ch["id"], full_state, diff,
                                           snap.get("summary") or "",
                                           _jload(snap.get("events"), []))
                    prev_state_local = full_state
                else:
                    prev_state_local = base
                _step(i + 1, prev_state_local)
                return

            def _accept(verdicts, summary, events):
                try:
                    full_state = mem.apply_diff(diff, verdicts, base)
                    version = store.replace_snapshot(ch["id"], full_state, diff,
                                                     summary, events)
                    store.clear_notes(ch["id"])
                    store.merge_chapter_characters(
                        ch["id"], list((full_state.get("characters") or {}).keys()))
                    led = diff.get("foreshadow") or {}
                    if led.get("planted") or led.get("paid_off") or led.get("dropped"):
                        try:
                            store.merge_foreshadow_ledger(
                                led.get("planted"), led.get("paid_off"),
                                ch.get("order_index"), led.get("dropped"))
                        except Exception:
                            pass
                    store.touch_lore_mentions(ch["id"])
                    mem.index_vectors_async(version, full_state, events)
                except Exception as e:
                    toast(page, f"第{ch.get('order_index')}章写入失败：{e}", error=True)
                    if on_done:
                        on_done()
                    return
                _step(i + 1, full_state)

            _confirm_sheet(page, store, ch, diff, _accept,
                           title=f"重归档确认 · 第{ch.get('order_index')}章")

        run_bg(page, lambda: mem.extract_diff(ch["id"], allow_empty=True), _extracted)

    _step(0, None)


# =====================================================
# 快照详情（全量状态 / 差异记录 / 摘要与事件 + 回滚 / 删除）
# =====================================================

def snapshot_sheet(page, session, version: int, on_refresh=None):
    store = session.store
    row = store.memory_snapshot(version)
    if not row:
        toast(page, "快照不存在", error=True)
        return
    full = _jload(row.get("full_state"), {})
    diff = _jload(row.get("diff"), {})
    events = _jload(row.get("events"), [])
    stale = bool(row.get("stale"))
    # 三段用 ExpansionTile（Flet 0.86 的 ft.Tab 不收 text 参数，且 Tabs 在手机窄屏
    # 里三个页签挤不开；折叠区更稳也更好点）
    def _json_panel(label: str, obj) -> ft.Control:
        return ft.ExpansionTile(
            title=ft.Text(label, size=13),
            maintain_state=True,
            controls=[ft.Container(
                ft.TextField(value=json.dumps(obj, ensure_ascii=False, indent=1),
                             multiline=True, read_only=True, text_size=11,
                             border_color=ft.Colors.TRANSPARENT, min_lines=6),
                padding=6)])

    panels = ft.Column([
        _json_panel("全量状态", full),
        _json_panel("差异记录", diff),
        ft.ExpansionTile(
            title=ft.Text("摘要与事件", size=13), maintain_state=True,
            controls=[ft.Container(ft.Column([
                ft.Text(row.get("summary") or "（无摘要）", size=12, selectable=True),
                ft.Divider(height=1),
                ft.Text("\n".join(str(e) for e in events) or "（无事件）",
                        size=12, selectable=True),
            ], scroll=ft.ScrollMode.AUTO, spacing=6), padding=6)]),
    ], spacing=0)

    def _rollback(e):
        page.pop_dialog()

        def _yes():
            store.rollback_memory(version)
            toast(page, f"已回滚到 v{version}（之后的快照标记为过期）")
            if on_refresh:
                on_refresh()
        confirm(page, "回滚记忆",
                f"回滚到 v{version}？该版本之后的快照会全部标记为过期（红色），"
                "当前状态回到这一版。", _yes, yes_text="回滚")

    def _delete(e):
        page.pop_dialog()

        def _yes():
            store.delete_memory_snapshot(version)
            toast(page, f"已删除快照 v{version}"
                        + ("（该章状态降回「已完成」）" if row.get("chapter_id") else ""))
            if on_refresh:
                on_refresh()
        confirm(page, "删除快照",
                f"删除 v{version}？版本号留洞不重排。"
                + ("删最新有效条 = 记忆自动回退到上一条。" if not stale else ""),
                _yes, yes_text="删除")

    _sheet(page, ft.Column([
        ft.Text(f"快照 v{version} ｜ {chapter_label(store, row.get('chapter_id'))}"
                + ("（已过期）" if stale else ""),
                size=16, weight=ft.FontWeight.BOLD),
        ft.Text(row.get("created_at") or "", size=11, color=ft.Colors.ON_SURFACE_VARIANT),
        ft.Container(panels, height=_sheet_height(page) * 0.55),
        ft.Row([
            ft.TextButton("删除此快照", on_click=_delete),
            ft.FilledTonalButton("回滚到此版本", on_click=_rollback),
        ], alignment=ft.MainAxisAlignment.END),
    ], spacing=6))


# =====================================================
# 记忆页主体
# =====================================================

def memory_view(page, session, on_refresh=None) -> ft.Control:
    store = session.store
    chain = store.memory_chain()
    cur_ver = max((s["version"] for s in chain if not s.get("stale")), default=None)

    def refresh():
        if on_refresh:
            on_refresh()

    head = ft.Row([
        ft.Text("记忆链", size=17, weight=ft.FontWeight.BOLD, expand=True),
        ft.PopupMenuButton(
            icon=ft.Icons.MORE_VERT,
            items=[
                ft.PopupMenuItem(content="编辑当前状态",
                                 on_click=lambda e: edit_state_sheet(page, session, refresh)),
                ft.PopupMenuItem(content="人物管理",
                                 on_click=lambda e: characters_sheet(page, session, refresh)),
                ft.PopupMenuItem(content="时间与恢复设置",
                                 on_click=lambda e: time_recovery_sheet(page, session)),
                ft.PopupMenuItem(content="从角色卡生成 v0",
                                 on_click=lambda e: gen_v0_from_lore(page, session, refresh)),
                ft.PopupMenuItem(content="设定补进状态卡",
                                 on_click=lambda e: merge_lore_into_cards(page, session, refresh)),
                ft.PopupMenuItem(content="手动更新记忆（重提取最近章）",
                                 on_click=lambda e: manual_update(page, session, refresh)),
                ft.PopupMenuItem(content="重建语义向量",
                                 on_click=lambda e: rebuild_vectors(page, session)),
                ft.PopupMenuItem(content="重置记忆链",
                                 on_click=lambda e: reset_memory(page, session, refresh)),
            ]),
    ])

    controls = [
        head,
        hint("每章归档一次状态变化：角色状态卡 / 世界实体 / 公共传闻。"
             "点快照可看详情、回滚或删除。"),
    ]

    # 过期警告条（需重归档 → 可点批量重归档；尾部删除 → 只提示）
    stale = [s for s in chain if s.get("stale")]
    chapter_stale = [s for s in stale if s.get("chapter_id")]
    if stale:
        cid = chapter_stale[0]["chapter_id"] if chapter_stale else None
        ch = store.get_chapter(cid) if cid else None
        chap_no = ch.get("order_index") if ch else cid
        if cid and hasattr(store, "has_rearchivable_after") \
                and store.has_rearchivable_after(cid):
            controls.append(ft.Container(
                ft.Column([
                    ft.Text(f"存在过期快照（中间章删除/改写或回滚导致）。"
                            f"自第{chap_no}章起需要重新归档：",
                            size=12, color=ft.Colors.RED_700),
                    ft.FilledTonalButton(
                        f"从第{chap_no}章起批量重归档",
                        icon=ft.Icons.RESTART_ALT,
                        on_click=lambda e, c=cid: rearchive_from(page, session, c, refresh)),
                ], spacing=4),
                bgcolor=ft.Colors.with_opacity(0.08, ft.Colors.RED_700),
                border=ft.Border.all(1, ft.Colors.RED_300),
                border_radius=8, padding=8))
        else:
            controls.append(ft.Container(
                ft.Text("部分记忆快照已过期（章节删除/回滚导致）。当前记忆已自动回退到"
                        "最新有效版本，无需重归档；如需恢复删除的章节可到回收站恢复。",
                        size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                bgcolor=ft.Colors.with_opacity(0.05, ft.Colors.ON_SURFACE),
                border_radius=8, padding=8))

    # 当前状态（状态卡视图 + JSON 切换）
    state = store.current_memory()
    mode = getattr(page, "_mem_state_mode", "card")
    if mode == "json":
        state_body = ft.TextField(
            value=json.dumps(state, ensure_ascii=False, indent=1), multiline=True,
            read_only=True, text_size=11, min_lines=8, max_lines=16,
            border_color=ft.Colors.TRANSPARENT)
    else:
        state_body = _state_cards(state)

    def _toggle(e):
        page._mem_state_mode = "json" if mode == "card" else "card"
        refresh()

    controls.append(ft.Container(
        ft.Column([
            ft.Row([
                ft.Text("当前状态（最新有效快照）", size=14,
                        weight=ft.FontWeight.W_600, expand=True),
                ft.TextButton("查看 JSON" if mode == "card" else "状态卡视图",
                              on_click=_toggle),
            ]),
            state_body,
        ], spacing=4),
        bgcolor=ft.Colors.with_opacity(0.04, ft.Colors.ON_SURFACE),
        border_radius=10, padding=10))

    # 快照列表
    rows = []
    for node in chain:
        ver = node.get("version")
        label = f"v{ver} ｜ {chapter_label(store, node.get('chapter_id'))}"
        suffix = "　（已过期）" if node.get("stale") else ""
        star = "★当前 ｜ " if ver == cur_ver else ""
        rows.append(ft.ListTile(
            dense=True,
            leading=ft.Icon(ft.Icons.HISTORY, size=16,
                            color=(ft.Colors.RED_400 if node.get("stale")
                                   else ft.Colors.ON_SURFACE_VARIANT)),
            title=ft.Text(star + label + suffix, size=12,
                          color=(ft.Colors.RED_400 if node.get("stale") else None)),
            subtitle=ft.Text(node.get("created_at") or "", size=10,
                             color=ft.Colors.ON_SURFACE_VARIANT,
                             max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            on_click=lambda e, v=ver: snapshot_sheet(page, session, v, refresh)))
    if rows:
        controls.append(section("快照时间线（点击查看 / 回滚）"))
        controls.append(ft.Card(ft.Container(ft.Column(rows, spacing=0), padding=2)))
    else:
        controls.append(empty("还没有记忆快照\n写完一章并「接受本章」后会自动归档"))

    # 伏笔台账
    controls.append(section("伏笔台账（归档时自动记账，点击可管理）"))
    controls.extend(_foreshadow_block(page, session, refresh))

    # 伏笔备忘
    notes_field = ft.TextField(
        label="伏笔备忘（手写，每行一条）",
        value=store.get_setting("foreshadow_notes", ""),
        multiline=True, min_lines=2, max_lines=5, dense=True)

    def _save_notes(e):
        store.set_setting("foreshadow_notes", notes_field.value or "")
        toast(page, "伏笔备忘已保存")

    controls.append(ft.Row([
        ft.FilledTonalButton("保存伏笔备忘", icon=ft.Icons.SAVE, on_click=_save_notes)]))

    return ft.Column(controls, scroll=ft.ScrollMode.AUTO, expand=True, spacing=4)


def _state_cards(state: dict) -> ft.Control:
    """状态卡视图（小白友好）：人物卡 + 世界状态。"""
    chars = (state or {}).get("characters") or {}
    ws = (state or {}).get("world_state") or {}
    blocks = []
    if not chars and not ws:
        return hint("（暂无记忆快照）")
    for name, card in chars.items():
        card = card if isinstance(card, dict) else {}
        bits = []
        if card.get("identity"):
            bits.append(f"身份：{card['identity']}")
        if card.get("location"):
            bits.append(f"位于{card['location']}")
        if card.get("physical_state"):
            bits.append(f"状态：{card['physical_state']}")
        if card.get("emotion"):
            bits.append(f"情绪：{card['emotion']}")
        inj = card.get("injuries")
        if isinstance(inj, list) and inj:
            bits.append("伤势：" + "、".join(str(x) for x in inj if str(x).strip()))
        tags = []
        if is_dead(card.get("dead")):
            tags.append("☠ 已故")
        if card.get("sealed"):
            tags.append("已封存")
        aliases = card.get("aliases")
        if isinstance(aliases, list) and aliases:
            tags.append("别名：" + "、".join(str(a) for a in aliases if str(a).strip()))
        line = "；".join(bits) if bits else "（尚无身份/外貌/位置记录）"
        if tags:
            line += "【" + "；".join(tags) + "】"
        blocks.append(ft.Container(
            ft.Column([ft.Text(name, size=13, weight=ft.FontWeight.W_600),
                       ft.Text(line, size=11, color=ft.Colors.ON_SURFACE_VARIANT)],
                      spacing=2),
            padding=ft.Padding(left=6, right=6, top=4, bottom=4)))
    if ws:
        ws_bits = []
        for k, label in (("time", "时间"), ("location", "地点"), ("notes", "备注")):
            if ws.get(k):
                ws_bits.append(f"{label}：{_short(ws[k], 80)}")
        ents = ws.get("entities") or {}
        if isinstance(ents, dict) and ents:
            ws_bits.append("世界实体：" + "、".join(list(ents.keys())[:8]))
        rumors = ws.get("rumors") or []
        if isinstance(rumors, list) and rumors:
            ws_bits.append(f"公共传闻 {len(rumors)} 条")
        if ws_bits:
            blocks.append(ft.Container(
                ft.Column([ft.Text("世界状态", size=13, weight=ft.FontWeight.W_600),
                           ft.Text("；".join(ws_bits), size=11,
                                   color=ft.Colors.ON_SURFACE_VARIANT)], spacing=2),
                padding=ft.Padding(left=6, right=6, top=6, bottom=4)))
    return ft.Column(blocks, spacing=0) if blocks else hint("（暂无记忆快照）")


def _foreshadow_block(page, session, on_refresh) -> list:
    """伏笔台账四组展示 + 点击管理。"""
    store = session.store
    states = store.foreshadow_states()
    if not any(states.values()):
        return [hint("（暂无伏笔记录；归档时自动从正文提取「埋设/回收/废弃」）")]

    def _rows(group, prefix, items, color=None):
        out = []
        for p in items:
            p = p if isinstance(p, dict) else {"content": str(p)}
            bits = []
            if p.get("planted_ch"):
                bits.append(f"埋设：第{p['planted_ch']}章")
            if group == "expired" and p.get("due_ch"):
                bits.append(f"预定：第{p['due_ch']}章（已过）")
            elif p.get("payoff_target"):
                bits.append(f"预定回收：{p['payoff_target']}")
            if p.get("source"):
                bits.append(f"来源：{p['source']}")
            text = f"{prefix}｜{p.get('content', '')}"
            if bits:
                text += f"（{'；'.join(bits)}）"
            out.append(ft.ListTile(
                dense=True,
                title=ft.Text(text, size=12, color=color,
                              max_lines=3, overflow=ft.TextOverflow.ELLIPSIS),
                on_click=lambda e, x=p, g=group: foreshadow_sheet(
                    page, session, g, x, on_refresh)))
        return out

    sub = ft.Colors.ON_SURFACE_VARIANT
    rows = (_rows("planted", "● 未回收", states.get("open") or [])
            + _rows("expired", "⚠ 已过期", states.get("expired") or [],
                    color=ft.Colors.ORANGE_700)
            + _rows("paid_off", "✓ 已回收", states.get("paid") or [], color=sub)
            + _rows("dropped", "✕ 已废弃", states.get("dropped") or [], color=sub))
    return [ft.Card(ft.Container(ft.Column(rows, spacing=0), padding=2))]


# =====================================================
# 伏笔台账操作（标记回收 / 改预定章 / 废弃 / 删除误记）
# =====================================================

def foreshadow_sheet(page, session, group: str, entry: dict, on_refresh=None):
    store = session.store
    content = str(entry.get("content") or "")
    if not content:
        return

    def _close():
        page.pop_dialog()

    def _mark_paid(e):
        _close()
        store.foreshadow_mark_paid(content)
        toast(page, "已标记为已回收")
        if on_refresh:
            on_refresh()

    def _change_target(e):
        _close()
        tf = ft.TextField(label="预定回收章（如「第30章」或 30）",
                          value=str(entry.get("payoff_target") or ""), autofocus=True)

        def _ok(ev):
            page.pop_dialog()
            store.foreshadow_update_target(content, tf.value or "")
            toast(page, "已更新预定回收章")
            if on_refresh:
                on_refresh()
        page.show_dialog(ft.AlertDialog(
            title=ft.Text("修改预定回收章"), content=tf,
            actions=[ft.TextButton("取消", on_click=lambda ev: page.pop_dialog()),
                     ft.FilledButton("保存", on_click=_ok)]))

    def _drop(e):
        _close()
        store.foreshadow_drop(content)
        toast(page, "已废弃该伏笔（不再提示回收）")
        if on_refresh:
            on_refresh()

    def _remove(e):
        _close()
        kind = "paid_off" if group == "paid_off" else (
            "dropped" if group == "dropped" else "planted")

        def _yes():
            store.remove_foreshadow_entry(kind, content)
            toast(page, "已删除误记条目")
            if on_refresh:
                on_refresh()
        confirm(page, "删除误记", f"从台账删除「{_short(content, 40)}」？", _yes,
                yes_text="删除")

    actions = []
    if group in ("planted", "expired"):
        actions.append(ft.FilledTonalButton("标记已回收", icon=ft.Icons.CHECK,
                                            on_click=_mark_paid))
        actions.append(ft.TextButton("修改预定回收章", icon=ft.Icons.EDIT_CALENDAR,
                                     on_click=_change_target))
        actions.append(ft.TextButton("废弃该伏笔", icon=ft.Icons.BLOCK, on_click=_drop))
    actions.append(ft.TextButton("删除误记", icon=ft.Icons.DELETE_OUTLINE,
                                 on_click=_remove))
    _sheet(page, ft.Column([
        ft.Text("伏笔台账条目", size=16, weight=ft.FontWeight.BOLD),
        ft.Text(_short(content, 300), size=12, selectable=True),
        hint(f"埋设：第{entry.get('planted_ch')}章" if entry.get("planted_ch") else ""),
        *actions,
    ], spacing=6))


# =====================================================
# 人物管理（封存 / 已故 / 编辑单卡）
# =====================================================

def characters_sheet(page, session, on_refresh=None):
    store = session.store
    state = store.current_memory()
    chars = state.get("characters") or {}
    if not chars:
        toast(page, "当前记忆中还没有人物，先归档章节或从角色卡生成 v0", error=True)
        return
    appearances = store.character_appearances()
    try:
        cur_order = store.max_chapter_order()
    except Exception:
        cur_order = 0
    gap = store.get_setting_int("auto_seal_gap", 8)

    rows = []
    seals: dict = {}        # 人物名 -> 封存 Checkbox（构建时同步存引用，别靠控件树索引取）
    deads: dict = {}        # 人物名 -> 已故 Checkbox
    for name, card in chars.items():
        card = card if isinstance(card, dict) else {}
        bits = []
        if card.get("identity"):
            bits.append(f"身份：{card['identity']}")
        if card.get("location"):
            bits.append(f"位于{card['location']}")
        seen = appearances.get(name) or 0
        extra = []
        if seen and cur_order:
            extra.append(f"最近出场：第{seen}章")
            if not card.get("sealed") and seen <= cur_order - gap:
                extra.append("自动隐藏")
        summary = f"（{'；'.join(bits)}）" if bits else "（尚无身份/位置记录）"
        if extra:
            summary += "【" + "；".join(extra) + "】"
        seal_cb = ft.Checkbox(label="封存（省 token，数据保留）",
                              value=bool(card.get("sealed")))
        dead_cb = ft.Checkbox(label="已故", value=is_dead(card.get("dead")))
        seals[name] = seal_cb
        deads[name] = dead_cb
        rows.append(ft.Container(ft.Column([
            ft.Row([ft.Text(name, size=13, weight=ft.FontWeight.W_600, expand=True),
                    ft.TextButton("编辑", on_click=lambda e, n=name: _edit_card(
                        page, session, n, on_refresh))]),
            ft.Text(summary, size=11, color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Row([seal_cb, dead_cb], spacing=8, wrap=True),
        ], spacing=2), padding=6))

    def _save(e):
        page.pop_dialog()
        changed = False
        for name, card in chars.items():
            if not isinstance(card, dict):
                continue
            want_seal = bool(seals[name].value)
            if want_seal and not card.get("sealed"):
                card["sealed"] = True
                changed = True
            elif not want_seal and card.get("sealed"):
                card.pop("sealed", None)
                changed = True
            want_dead = bool(deads[name].value)
            if is_dead(card.get("dead")) != want_dead:
                card["dead"] = want_dead
                if want_dead:
                    card.pop("revived", None)   # 重新判死：撤销豁免
                else:
                    # 撤销死亡必须打豁免，否则状态卡里「已故（第X章）」文本
                    # 会被 sync_dead_flags 再次标回已故，撤销永不生效
                    card["revived"] = True
                changed = True
        if changed:
            version = store.set_current_memory(state)
            _reindex(page, session, version, state)
            toast(page, "人物封存/已故设置已保存（生成新记忆快照）")
        else:
            toast(page, "没有改动")
        if on_refresh:
            on_refresh()

    _sheet(page, ft.Column([
        ft.Text("人物管理", size=16, weight=ft.FontWeight.BOLD),
        hint("封存后不再注入正文提示词（省 token），数据保留；"
             "已故标记后不再安排其出场行动（误判可取消勾选恢复）。"),
        ft.Container(ft.Column(rows, spacing=2, scroll=ft.ScrollMode.AUTO),
                     height=_sheet_height(page) * 0.6),
        ft.Row([ft.TextButton("取消", on_click=lambda e: page.pop_dialog()),
                ft.FilledButton("保存", on_click=_save)],
               alignment=ft.MainAxisAlignment.END),
    ], spacing=6))


def _edit_card(page, session, name: str, on_refresh=None):
    """编辑单张状态卡的 JSON（表单化的等价替代：字段更全、更不易漏）。"""
    store = session.store
    state = store.current_memory()
    card = (state.get("characters") or {}).get(name)
    if not isinstance(card, dict):
        toast(page, "该人物卡不存在", error=True)
        return
    tf = ft.TextField(value=json.dumps(card, ensure_ascii=False, indent=1),
                      multiline=True, min_lines=10, max_lines=18, text_size=11)

    def _ok(e):
        try:
            obj = json.loads(tf.value or "{}")
        except (ValueError, TypeError) as ex:
            toast(page, f"JSON 解析失败：{ex}", error=True)
            return
        if not isinstance(obj, dict):
            toast(page, "顶层必须是 JSON 对象", error=True)
            return
        state.setdefault("characters", {})[name] = obj
        page.pop_dialog()
        version = store.set_current_memory(state)
        _reindex(page, session, version, state)
        toast(page, f"已保存 {name} 的状态卡")
        if on_refresh:
            on_refresh()

    page.show_dialog(ft.AlertDialog(
        title=ft.Text(f"编辑状态卡 · {name}"),
        content=ft.Container(tf, width=340),
        actions=[ft.TextButton("取消", on_click=lambda e: page.pop_dialog()),
                 ft.FilledButton("保存并生效", on_click=_ok)]))


# =====================================================
# 当前状态编辑 / 从角色卡生成 v0 / 设定补进状态卡
# =====================================================

def edit_state_sheet(page, session, on_refresh=None):
    store = session.store
    cur = json.dumps(store.current_memory(), ensure_ascii=False, indent=2)
    tf = ft.TextField(value=cur, multiline=True, min_lines=12, max_lines=22,
                      text_size=11)

    def _ok(e):
        text = (tf.value or "").strip()
        if not text:
            toast(page, "状态 JSON 为空", error=True)
            return
        try:
            obj = json.loads(text)
        except (ValueError, TypeError) as ex:
            toast(page, f"JSON 解析失败：{ex}", error=True)
            return
        if not isinstance(obj, dict):
            toast(page, "顶层必须是 JSON 对象", error=True)
            return
        page.pop_dialog()
        version = store.set_current_memory(obj)
        _reindex(page, session, version, obj)
        toast(page, f"已保存为当前状态（记忆 v{version}）")
        if on_refresh:
            on_refresh()

    _sheet(page, ft.Column([
        ft.Text("编辑当前状态", size=16, weight=ft.FontWeight.BOLD),
        hint('直接编辑 JSON 会立即成为新的当前状态（不走差异确认）。'
             '结构：{"characters": {人物名: 状态卡}, "world_state": {...}}。'),
        ft.Container(tf, height=_sheet_height(page) * 0.55),
        ft.Row([ft.TextButton("取消", on_click=lambda e: page.pop_dialog()),
                ft.FilledButton("保存并生效", on_click=_ok)],
               alignment=ft.MainAxisAlignment.END),
    ], spacing=6))


def _state_from_lore(store) -> dict:
    """角色卡 → 初始状态（对齐桌面 _state_from_lore）。

    设定原文放 custom_fields['设定原文']，**不能**放 clothing：那是「当前穿着」，
    整段设定塞进去会让模型把性格/背景当穿着写。
    """
    from app.core.store import new_character_card
    state = {"characters": {}, "world_state": {"time": "", "location": "", "notes": "",
                                               "entities": {}, "rumors": []}}
    for entry in store.list_lore("角色"):
        card = new_character_card(entry["name"])
        card["custom_fields"] = {"设定原文": entry.get("content") or ""}
        state["characters"][entry["name"]] = card
    return state


def gen_v0_from_lore(page, session, on_refresh=None):
    store = session.store
    chain = store.memory_chain()
    if not (len(chain) == 1 and not chain[0].get("chapter_id")):
        toast(page, "仅当记忆链上只有 v0 初始状态时可用", error=True)
        return

    def _yes():
        state = _state_from_lore(store)
        if not state["characters"]:
            toast(page, "设定库里还没有「角色」条目，先到「设定」页添加", error=True)
            return
        version = store.set_current_memory(state, summary="从角色卡生成 v0")
        _reindex(page, session, version, state)
        toast(page, f"已生成 v0 初始状态（{len(state['characters'])} 位角色，记忆 v{version}）")
        if on_refresh:
            on_refresh()
    confirm(page, "从角色卡生成 v0",
            "将根据设定库中的角色卡生成 v0 初始状态（覆盖当前 v0）。确定？",
            _yes, yes_text="生成")


def merge_lore_into_cards(page, session, on_refresh=None):
    """设定库「角色」→ 已有人物状态卡的 custom_fields['设定原文']（不重置记忆链）。"""
    store = session.store
    rows = store.list_lore("角色")
    if not rows:
        toast(page, "设定库里还没有「角色」分类的条目", error=True)
        return
    by_key = {}
    for r in rows:
        n = str(r.get("name") or "").strip()
        if n and (r.get("content") or "").strip():
            by_key.setdefault(n, r["content"].strip())
    if not by_key:
        toast(page, "设定库的角色条目都没有正文内容，无可合并", error=True)
        return
    mem = store.current_memory()
    chars = mem.get("characters") or {}
    matched, no_lore = [], []
    for name, card in chars.items():
        if not isinstance(card, dict):
            continue
        content = by_key.get(str(name).strip())
        if not content:
            for a in (card.get("aliases") or []):
                content = by_key.get(str(a).strip())
                if content:
                    break
        if not content:
            no_lore.append(name)
            continue
        cf = card.get("custom_fields") if isinstance(card.get("custom_fields"), dict) else {}
        if cf.get("设定原文") == content:
            continue        # 已同步过，不产生无意义快照
        cf["设定原文"] = content
        card["custom_fields"] = cf
        matched.append(name)
    if not matched:
        msg = "没有需要合并的设定（人物卡都已同步）"
        if no_lore:
            msg += f"；{len(no_lore)} 位人物无匹配设定"
        toast(page, msg)
        return
    version = store.set_current_memory(mem)
    _reindex(page, session, version, mem)
    msg = f"已把 {len(matched)} 位人物的设定原文补进状态卡"
    if no_lore:
        msg += f"；{len(no_lore)} 位无匹配设定"
    toast(page, msg)
    if on_refresh:
        on_refresh()


def manual_update(page, session, on_refresh=None):
    """手动更新记忆：重提取最近已归档章 → 逐条确认 → 写新快照。"""
    store = session.store
    archived = [c for c in store.list_chapters() if c.get("status") == "已归档"]
    if not archived:
        toast(page, "还没有已归档的章节，请先归档一章", error=True)
        return
    ch = max(archived, key=lambda c: c.get("order_index") or 0)
    archive_flow(page, session, ch["id"], on_done=lambda v: on_refresh and on_refresh())


# =====================================================
# 时间与恢复设置 / 重建向量 / 重置
# =====================================================

def time_recovery_sheet(page, session):
    store = session.store
    origin = ft.TextField(
        label="故事时间基准（第一章开头的故事内时间）",
        value=store.get_setting("story_time_origin", ""),
        hint_text="留空 = AI 归档第一章时按剧情自动设定（推荐）",
        multiline=True, min_lines=2, max_lines=4, dense=True)
    recovery = ft.TextField(
        label="人物恢复能力设定（决定伤势何时痊愈）",
        value=store.get_setting("recovery_rule", ""),
        hint_text="例：陆沉：体质远超常人，皮肉伤半日结痂……（留空 = 按常人体质判断）",
        multiline=True, min_lines=2, max_lines=5, dense=True)

    def _save(e):
        store.set_setting("story_time_origin", (origin.value or "").strip())
        store.set_setting("recovery_rule", (recovery.value or "").strip())
        page.pop_dialog()
        toast(page, "时间与恢复设置已保存（注入记忆提取/正文/细纲/检查四类请求）")

    _sheet(page, ft.Column([
        ft.Text("时间与恢复设置", size=16, weight=ft.FontWeight.BOLD),
        hint("两项都是项目级设置（每本书各自一份），随项目库同步。"),
        origin, recovery,
        ft.Row([ft.TextButton("取消", on_click=lambda e: page.pop_dialog()),
                ft.FilledButton("保存", on_click=_save)],
               alignment=ft.MainAxisAlignment.END),
    ], spacing=8, scroll=ft.ScrollMode.AUTO))


def rebuild_vectors(page, session):
    """为全部未过期快照重建语义向量（未配嵌入时给出提示）。"""
    from app.core.embedding import embed_texts
    store = session.store

    def job():
        return store.rebuild_memory_vectors(embed_texts)

    def done(r):
        if isinstance(r, dict) and r.get("ok") is False:
            toast(page, f"重建失败：{r.get('error')}", error=True)
        elif isinstance(r, Exception):
            toast(page, f"重建失败：{r}", error=True)
        else:
            toast(page, f"已重建 {r} 个快照的语义向量" if isinstance(r, int)
                  else "重建完成")

    toast(page, "正在重建语义向量…")
    run_bg(page, job, done)


def reset_memory(page, session, on_refresh=None):
    store = session.store

    def _yes():
        store.reset_memory()
        toast(page, "记忆链已重置（回到空 v0）")
        if on_refresh:
            on_refresh()
    confirm(page, "重置记忆链",
            "将删除全部记忆快照与向量，记忆回到空状态（可重新从角色卡生成 v0）。\n"
            "已归档章节的「已归档」状态不变，但下次归档将从空状态开始累积。确定继续？",
            _yes, yes_text="重置")


def _reindex(page, session, version, full_state, events=None):
    """手动编辑/生成 v0 后补建向量（失败静默，不影响主流程）。"""
    try:
        from app.core.memory_service import MemoryService
        MemoryService(session.store).index_vectors_async(version, full_state,
                                                        events or [])
    except Exception:
        pass
