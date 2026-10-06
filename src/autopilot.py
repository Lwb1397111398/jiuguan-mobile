# -*- coding: utf-8 -*-
"""⚡ 自动模式（手机版）：从当前进度自动接力到完本。

状态机逻辑与桌面版 app/ui/auto_pilot.py 同源：总纲→锁纲→逐卷切章→逐章
大纲/细纲/正文/归档。生成一律走 gen.start_generation(preview=False)
（开启即授权），结果直接落库。
"""
import threading
import time

from gen import start_generation

from app.core.assembler import Assembler

STEP_LABELS = {
    "synopsis": "生成总纲", "synopsis_lock": "锁定总纲",
    "volume_split": "分卷切章", "outline": "细化大纲", "detail": "生成细纲",
    "prose": "撰写正文", "archive": "归档记忆",
}


class AutoPilot:
    """手机版自动模式引擎（回调式，无 Qt 依赖）。"""

    def __init__(self, page, session):
        self.page = page
        self.session = session
        self.on_state = None        # callable(state: str, info: str)
        self.on_chunk = None        # callable(chars: int)
        self.on_step = None         # callable(label: str)
        self._running = False
        self._paused = False
        self._stop_event = threading.Event()
        self._task = None
        self._retry = 0
        self._last_fail_blocked = False   # 上次生成是否被渠道内容策略拦下（见 _run_gen）
        self._chars = 0
        self._last_emit = 0.0

    # ---------- 状态 ----------

    @property
    def running(self):
        return self._running

    @property
    def paused(self):
        return self._paused

    def _set_state(self, state: str, info: str = ""):
        if self.on_state:
            try:
                self.on_state(state, info)
            except Exception:
                pass

    # ---------- 控制 ----------

    def start(self):
        if self._running:
            return
        self._running = True
        self._paused = False
        self._stop_event.clear()
        self._set_state("running")
        self._tick()

    def pause(self):
        if self._task:
            self._task.stop()
        self._paused = True
        self._set_state("paused", "已暂停")

    def resume(self):
        if not self._running:
            return
        self._paused = False
        self._set_state("running")
        self._tick()

    def stop(self):
        self._running = False
        self._paused = False
        if self._task:
            self._task.stop()
        try:
            self.session.store.pipeline().abort()
        except Exception:
            pass
        self._set_state("idle")

    # ---------- 主循环 ----------

    def _tick(self):
        """评估下一步并执行（在当前调用线程/工作线程中）。"""
        if not self._running or self._paused:
            return
        from bridge import run_bg
        run_bg(self.page, self._next_step, self._on_step_result)

    def _next_step(self) -> dict | None:
        """状态机：找出下一个待办步骤。"""
        store = self.session.store
        s = store.current_synopsis()
        if not s or not (s.get("content") or "").strip():
            return {"kind": "synopsis"}
        if not s.get("is_locked"):
            return {"kind": "synopsis_lock", "content": s.get("content") or "",
                    "title": s.get("title") or ""}
        chapters = store.list_chapters()
        for v in store.list_volumes():
            vchs = sorted((c for c in chapters if c.get("volume_id") == v["id"]),
                          key=lambda c: c.get("order_index") or 0)
            if not vchs:
                return {"kind": "volume_split", "volume": v}
            for ch in vchs:
                todo = self._chapter_todo(ch)
                if todo:
                    return todo
        for ch in [c for c in chapters if c.get("volume_id") is None]:
            todo = self._chapter_todo(ch)
            if todo:
                return todo
        return None

    def _chapter_todo(self, ch: dict) -> dict | None:
        store = self.session.store
        ch = store.get_chapter(ch["id"])
        if not ch:
            return None
        if (ch.get("status") or "未规划") in ("未规划", "有大纲"):
            return {"kind": "outline", "chapter": ch}
        if ch.get("status") == "有细纲":
            return {"kind": "prose", "chapter": ch}
        if ch.get("status") == "写作中":
            return {"kind": "prose", "chapter": ch}
        if ch.get("status") == "已完成":
            if not store.memory_snapshot_for_chapter(ch["id"]):
                return {"kind": "archive", "chapter": ch}
        return None

    def _on_step_result(self, step: dict):
        if not self._running or self._paused:
            return
        if step is None:
            self._running = False
            self._set_state("done", "🎉 全书完本！")
            return
        kind = step["kind"]
        label = STEP_LABELS.get(kind, kind)
        if step.get("chapter"):
            ch = step["chapter"]
            label = f"第{ch.get('order_index')}章 {ch.get('title') or ''} · {label}"
        elif step.get("volume"):
            v = step["volume"]
            label = f"第{v.get('order_index')}卷 {v.get('title') or ''} · {label}"
        if self.on_step:
            try:
                self.on_step(label)
            except Exception:
                pass
        fn = getattr(self, f"_do_{kind}", None)
        if fn is None:
            self._running = False
            self._set_state("error", f"未知步骤 {kind}")
            return
        fn(step)

    def _run_gen(self, role, context, task, step, max_tokens=None):
        """统一的自动生成调用（免预览、拒答重试、失败暂停）。

        重试推杆（2026-09-19 与桌面版对齐）：**被渠道内容策略拦下时不带**——
        拦下说明请求根本没送到模型，「请务必按原任务要求重新输出」按构造帮不上忙；
        桌面版实测该形态 42 条请求 0 条产出正文，而同时段干净报文 155 条里 26 条出文。
        其余失败类型（拒答/空输出）仍带推杆。

        推杆文案**必须与桌面同源**（`app.core.pipeline.REFUSAL_RETRY_EXTRA`）：
        本文件曾自带一份陈旧副本（文案已与桌面漂移），2026-09-19 改为延迟导入，
        与本工程其余部分一样在函数内 import（避免拖慢安卓冷启动）。
        """
        from app.core.pipeline import REFUSAL_RETRY_EXTRA
        retry_extra = (REFUSAL_RETRY_EXTRA
                       if self._retry > 0 and not self._last_fail_blocked else "")
        ctx = {**context, "extra": retry_extra} if retry_extra else context

        def on_started(tk):
            self._task = tk
            self._chars = 0

        def on_text(buffer: str):
            self._chars = len(buffer)
            now = time.monotonic()
            if self.on_chunk and now - self._last_emit >= 0.24:
                self._last_emit = now
                self.on_chunk(self._chars)

        def on_done(result):
            self._task = None
            text = (result.get("text") or "").strip()
            from app.core.pipeline import detect_refusal, detect_upstream_block
            # 先把「本次是不是被渠道拦下」记下来（下一次重试据此不叠推杆）
            self._last_fail_blocked = (bool(result.get("upstream_blocked"))
                                       or bool(detect_upstream_block(text)))
            if not result.get("ok"):
                self._fail(step, result.get("error", "生成失败"))
                return
            if result.get("status") == "stopped":
                self.pause()
                return
            if self._last_fail_blocked:
                # 拦截占位说明不是正文，绝不能丢给解析器（解析成空只会报
                # 「解析失败」，老板看不出真正原因）
                self._retry += 1
                if self._retry >= 3:
                    self._retry = 0
                    self._fail(step, "被渠道内容策略拦截（请求没送到模型就被上游拦下）")
                else:
                    self._tick()
                return
            if not text or detect_refusal(text):
                self._retry += 1
                if self._retry >= 3:
                    self._retry = 0
                    self._fail(step, "连续 3 次输出为空或疑似拒答")
                else:
                    self._tick()
                return
            self._retry = 0
            self._apply_step(step, result, text)

        start_generation(self.page, self.session, role=role, context=ctx,
                         task=task, title=STEP_LABELS.get(step["kind"], ""),
                         on_done=on_done, on_text=on_text, on_started=on_started,
                         preview=False, max_tokens=max_tokens)

    def _fail(self, step, msg):
        self._paused = True
        self._set_state("error", f"{STEP_LABELS.get(step['kind'], '')}：{msg}")

    def _apply_step(self, step, result, text):
        """解析并落库（工作线程回调里执行；落库后 tick 下一步）。"""
        from app.core.pipeline import Pipeline
        store = self.session.store
        kind = step["kind"]
        try:
            if kind == "synopsis":
                cands, err = Pipeline.parse_synopsis_candidates(text)
                if err or not cands:
                    raise ValueError(f"总纲解析失败：{err or '空'}")
                pick = cands[0]
                parsed = Pipeline.parse_synopsis_volumes(pick["content"])
                if not parsed.get("volumes"):
                    raise ValueError("总纲没有识别到「### 分卷」结构")
                self._lock_with_volumes(pick["content"], pick["title"],
                                        parsed["volumes"])
            elif kind == "synopsis_lock":
                parsed = Pipeline.parse_synopsis_volumes(step["content"])
                if not parsed.get("volumes"):
                    raise ValueError("总纲没有识别到「### 分卷」结构")
                self._lock_with_volumes(step["content"], step.get("title") or "",
                                        parsed["volumes"])
            elif kind == "volume_split":
                chapters, err = Pipeline.parse_volume_chapters(text)
                if err or not chapters:
                    raise ValueError(f"切章解析失败：{err or '空'}")
                vol = step["volume"]
                for c in chapters:
                    store.add_chapter({**c, "status": "有大纲",
                                       "volume_id": vol["id"]})
            elif kind == "outline":
                ch = store.get_chapter(step["chapter"]["id"])
                data, err = Pipeline.parse_outline_chapter(
                    text, ch, field_defs=store.get_outline_fields())
                if err or not data or not (data.get("outline_content") or "").strip():
                    raise ValueError(f"大纲解析失败：{err or '空'}")
                store.update_chapter(ch["id"], data)
                store.set_outline_refined(ch["id"], True)
            elif kind == "prose":
                cid = step["chapter"]["id"]
                batch = step.get("_batch")
                if batch:
                    # 分批：本批解析落批（滚动状态包随批持久化，供续批衔接）
                    prose, state, err = Pipeline.parse_batch_prose(text)
                    if err or not (prose or "").strip():
                        raise ValueError(f"本批正文解析失败：{err or '空'}")
                    if batch["index"] == 1 and result.get("scene_meta"):
                        # 首批剥出的场景头写成「【场景】」本章动态（后续批不收）
                        store.set_scene_note(cid, result["scene_meta"])
                    store.add_prose_batch(cid, batch["index"], batch["ids"],
                                          prose, state)
                    if not batch["final"]:
                        # 非末批：章状态未变 → 下一轮 _next_step 再排 prose，接力下一批
                        self._tick()
                        return
                    merged = store.prose_batch_merged(cid)
                    store.accept_chapter_prose(cid, merged)
                    store.set_chapter_status(cid, "已完成")
                else:
                    _t, prose, err = Pipeline.parse_chapter_prose(text)
                    if err or not (prose or "").strip():
                        raise ValueError(f"正文解析失败：{err or '整章为空'}")
                    store.accept_chapter_prose(cid, prose.strip())
                    store.set_chapter_status(cid, "已完成")
            else:
                raise ValueError(f"未知步骤 {kind}")
        except ValueError as e:
            self._retry += 1
            if self._retry >= 3:
                self._retry = 0
                self._fail(step, str(e))
            else:
                self._tick()
            return
        # 成功 → 归档步骤紧随正文
        if kind == "prose" and step.get("chapter"):
            self._do_archive({"chapter": step["chapter"]})
            return
        self._tick()

    def _lock_with_volumes(self, content, title, volumes):
        store = self.session.store
        fingerprint = store.synopsis_fingerprint(content)
        last = store.get_last_locked_synopsis_fingerprint()
        has_old = bool(store.list_volumes())
        changed = (fingerprint != last) if last else \
            (has_old and store.synopsis_volumes_differ(volumes))
        store.lock_synopsis_with_volumes(content, title, volumes,
                                         replace_existing=bool(changed and has_old))

    # ---------- 各步骤生成 ----------

    def _do_synopsis(self, step):
        from app.core.task_templates import render as render_task
        store = self.session.store
        req = store.get_setting("auto_pilot_brief", "")
        self._run_gen("synopsis", {"user_req": req},
                      render_task("brainstorm", store), step)

    def _do_synopsis_lock(self, step):
        # 无需生成，直接解析落库
        self._apply_step(step, {}, "")

    def _do_volume_split(self, step):
        from app.core.task_templates import render as render_task
        store = self.session.store
        vol = step["volume"]
        mode = str(store.get_setting("volume_chapter_mode", "auto") or "auto")
        raw = str(store.get_setting("volume_chapter_count", "") or "").strip()
        rule = (f"本卷必须正好切分为 {raw} 章。" if mode == "exact" and raw.isdigit()
                else "本卷章数不设硬性规定，由你根据剧情节奏与总纲篇幅自行判断。")
        task = render_task("volume", store, chapter_rule=rule,
                           volume_no=vol.get("order_index") or 1,
                           volume_count=len(store.list_volumes()))
        self._run_gen("volume", {"volume_id": vol["id"]}, task, step)

    def _do_outline(self, step):
        from app.core.task_templates import render as render_task
        store = self.session.store
        ch = store.get_chapter(step["chapter"]["id"])
        vol = store.get_volume(ch.get("volume_id")) if ch.get("volume_id") else None
        vol_summary = (vol.get("summary") if vol else "") or ""
        chapters_in_vol = [c for c in store.list_chapters()
                           if c.get("volume_id") == (vol["id"] if vol else None)]
        chapter_no = len([c for c in chapters_in_vol
                          if (c.get("order_index") or 0) <= (ch.get("order_index") or 0)])
        task = render_task("outline_chapter", store,
                           chapter_index=ch.get("order_index", 1),
                           chapter_title=ch.get("title", "") or "未命名",
                           chapter_no=chapter_no or 1,
                           chapter_count=len(chapters_in_vol) or 1,
                           volume_context=vol_summary,
                           volume_title=(vol.get("title") if vol else "") or "未分卷",
                           ending_note="")
        self._run_gen("outline", {"volume_id": vol["id"] if vol else None,
                                  "chapter": ch, "refine_single": True},
                      task, step)

    def _do_prose(self, step):
        from app.core.task_templates import render as render_task
        store = self.session.store
        ch = store.get_chapter(step["chapter"]["id"])
        cid = ch["id"]
        if store.get_prose_mode() == "batch":
            self._do_prose_batch(step, ch, cid)
            return
        task = render_task("prose_chapter_md", store,
                           chapter_index=ch.get("order_index", "?"),
                           chapter_title=ch.get("title") or "未命名")
        context = {
            "chapter": ch,
            "character_names": ch.get("characters") or [],
            "whole_chapter": True,
            "notes": store.list_notes(cid),
            "previous_end_override": Assembler(store).prev_chapter_end(cid),
        }
        # 输出上限不按目标字数估算（与桌面 auto_pilot._prose_whole 同口径）：
        # 思考链与正文共享 max_tokens 预算，紧凑估算会被思考先耗掉、正文必截断；
        # 走 pipeline 的角色默认（prose 60000），渠道不接受大值时自动减半降级。
        self._run_gen("prose", context, task, step)

    def _do_prose_batch(self, step, ch: dict, cid: int):
        """分批正文：每次调用生成一批（对齐桌面 auto_pilot._prose_batch）。

        步骤机按章状态评估进度：非末批落批后章仍是「有细纲/写作中」→
        _next_step 重新排出 prose 步骤，自然接力到下一批；末批合并接受后
        章状态「已完成」→ 进入归档。中断恢复：已生成批次持久在 prose_batches，
        重新接管从 len(批次)+1 续跑。
        """
        from app.core.pipeline import plan_batches, batch_tail_two_paras
        from app.core.assembler import batch_block_list_text
        from app.core.task_templates import render as render_task, BATCH_FINAL_NOTE
        store = self.session.store
        blocks = store.list_blocks(cid)
        if not blocks:
            self._fail(step, "本章还没有细纲块（分批模式需要先有细纲）")
            return
        # 细纲重新生成后旧批次可能引用已不存在的块 → 全部作废从头分批
        valid_ids = {b["id"] for b in blocks}
        rows = store.list_prose_batches(cid)
        if any(any(i not in valid_ids for i in store.prose_batch_block_ids(r))
               for r in rows):
            store.clear_prose_batches(cid)
            rows = []
        batches = plan_batches(blocks, store.get_setting_int("prose_batch_size", 3))
        next_idx = len(rows) + 1
        if next_idx > len(batches):
            # 批全齐但正文未接受（上次中断在合并前）→ 直接合并收尾
            merged = store.prose_batch_merged(cid)
            if not merged.strip():
                self._fail(step, "批次记录为空且无已写正文")
                return
            store.accept_chapter_prose(cid, merged)
            store.set_chapter_status(cid, "已完成")
            self._tick()
            return
        batch_blocks = batches[next_idx - 1]
        batch_ids = [b["id"] for b in batch_blocks]
        is_final = next_idx == len(batches)
        step["_batch"] = {"index": next_idx, "ids": batch_ids, "final": is_final}
        # 思考覆盖全章 → 施工单注入全部块并标注「本批/后续批次」
        context = {
            "chapter": ch,
            "character_names": ch.get("characters") or [],
            "chapter_plan": [(b, None) for b in blocks],
            "batch_block_ids": set(batch_ids),
            "notes": store.list_notes(cid),
            "batch_index": next_idx,
            "batch_total": len(batches),
        }
        common = dict(chapter_no=ch.get("order_index", "?"),
                      chapter_title=ch.get("title") or "未命名",
                      block_list=batch_block_list_text(blocks, batch_ids),
                      final_note=BATCH_FINAL_NOTE if is_final else "")
        if next_idx == 1:
            task = render_task("prose_batch_first", store, **common)
            # 重写整章时不能拿本章旧正文当「前文结尾」（与桌面同口径）
            context["previous_end_override"] = Assembler(store).prev_chapter_end(cid)
        else:
            task = render_task("prose_batch_next", store, batch_no=next_idx, **common)
            context["batch_state"] = store.prose_batch_state_package(cid)
            context["previous_end_override"] = (
                "【前块结尾（衔接）：这是上一批正文的最后两段，请紧接其后续写本批，"
                "不要重复这段内容】\n" + batch_tail_two_paras(rows[-1].get("content") or ""))
        self._run_gen("prose", context, task, step)

    def _do_archive(self, step):
        from bridge import run_bg
        from app.core.memory_service import MemoryService
        store = self.session.store
        cid = step["chapter"]["id"]

        def job():
            mem = MemoryService(store)
            return mem.archive_auto(cid)

        def done(r):
            if isinstance(r, dict) and not r.get("ok"):
                self._fail(step, f"归档失败：{r.get('error', '')}")
                return
            self._tick()
        run_bg(self.page, job, done)
