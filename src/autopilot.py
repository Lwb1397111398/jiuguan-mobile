# -*- coding: utf-8 -*-
"""⚡ 自动模式（手机版）：从当前进度自动接力到完本。

状态机逻辑与桌面版 app/ui/auto_pilot.py 同源：总纲→锁纲→逐卷切章→逐章
大纲/细纲/正文/归档。生成一律走 gen.start_generation(preview=False)
（开启即授权），结果直接落库。

两种范围（2026-11-15 从桌面移植 scope，桌面 2026-11-10「一键细化全部大纲」）：
- scope="all"（默认）：上面那条完整流水线，全书完本才停；
- scope="outline"：只跑「细化章节大纲」，全书章节细化完即停，**不进切章/
  正文/归档**。进度记在 chapters.outline_refined 上：停机、关程序后再点一次，
  已细化的章自动跳过，从没细化的那一章接着做；已锁定大纲（outline_locked）
  一律不写。暂停/继续/断连续写与 all 范围共用同一份代码。

断连续写（2026-11-15 从桌面移植，桌面 2026-11-06 / 6fcbba9）：
- 正文生成中途断连（status=stopped 且非用户暂停）不再整步重写，也不再原地
  停机：成规模的已产出攒成「断点」（内存态，与桌面同款不落盘），重试自动换
  「继续生成」接力（build_continue_task + 上轮思考 + 降思考档）；
- 断点最小规模 500 字（太短的碎段不值得续，按普通失败重写）；
- 续写新增段单独体检（continuation_segment_issue）：第一次不合格丢弃该段、
  保留底稿下轮重续，连续 2 次放弃断点整步重写；
- 平台内容审核掐断（safety_blocked）是 fatal：断点一并作废，立即暂停。

失败重试节拍（2026-11-15 对齐桌面）：30秒→1分→隔5分→30秒→1分→隔10分→
30秒→1分，共 3 轮 9 次尝试，全败才暂停等用户（旧版是立即重试 3 次）。
"""
import threading
import time

from gen import start_generation

from app.core.assembler import Assembler
from app.core.dual_rules import (chapter_todo as core_chapter_todo,
                                 parse_volume_split, volume_chapter_rule)

STEP_LABELS = {
    "synopsis": "生成总纲", "synopsis_lock": "锁定总纲",
    "volume_split": "分卷切章", "outline": "细化大纲", "detail": "生成细纲",
    "prose": "撰写正文", "archive": "归档记忆",
}

# scope="outline" 时细化大纲步骤的显示名（与 all 范围区分，让界面/日志看得出当前范围）
OUTLINE_STEP_NAME = "⚡一键细化大纲"

# 断连续写最小断点规模（对齐桌面 AUTO_CONTINUE_MIN_CHARS）：断片达到才攒断点
# 走续写，太短的碎段按普通失败重写（碎片续写容易整篇重起）。
AUTO_CONTINUE_MIN_CHARS = 500

# 续写软失败容忍次数（对齐桌面 CONT_SOFT_FAIL_LIMIT）：新增段被判
# 「空/拒答/省略收尾/过短」时，第一次只丢弃该段、保留底稿继续从上次正常处续；
# 连续第 2 次就放弃断点、整步重写（宁可整章重写，不要自我审查的产物落库）。
CONT_SOFT_FAIL_LIMIT = 2

# 失败重试节拍（秒，与桌面 RETRY_DELAYS 同源）：轮内 30s→60s；第 3 次失败后
# 隔 5 分钟进第二轮，第 6 次失败后隔 10 分钟进第三轮；第三轮 30s/60s 仍失败
# → 暂停等用户。共 3 轮 × 3 次 = 9 次尝试。
RETRY_DELAYS = (30, 60, 300, 30, 60, 600, 30, 60)
MAX_RETRY = len(RETRY_DELAYS) + 1


def _fmt_delay(sec: int) -> str:
    """重试等待的人话时长：30 →「30 秒」，60/300/600 →「1/5/10 分钟」。"""
    if sec >= 60 and sec % 60 == 0:
        return f"{sec // 60} 分钟"
    return f"{sec} 秒"


def _step_owner(step: dict) -> str:
    """断点归属：kind#章节id（对齐桌面）。断点只跟着同一步骤的重试走，
    换步骤（暂停期间工程被手动改动等）即作废，防串章拼接。"""
    ch = step.get("chapter")
    cid = ch.get("id") if isinstance(ch, dict) else ""
    return f"{step.get('kind', '')}#{cid}"


class AutoPilot:
    """手机版自动模式引擎（回调式，无 Qt 依赖）。"""

    def __init__(self, page, session, scope: str = "all"):
        self.page = page
        self.session = session
        # 执行范围（2026-11-15 从桌面移植）：all=完整流水线（默认）；
        # outline=只细化章节大纲，细化完即停（大纲页「一键细化全部大纲」）
        self.scope = scope if scope in ("all", "outline") else "all"
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
        # 失败重试节拍定时器（对齐桌面 RETRY_DELAYS）：手机无 Qt，用
        # threading.Timer 延时下一拍；暂停/停止时取消，防暂停后还触发。
        self._retry_timer = None
        # 断连续写断点（2026-11-15 从桌面移植）：断连时攒下的部分正文/上轮思考，
        # 跨连续断连累加；成功/换步骤/停机/致命失败即作废。**内存态**，与桌面
        # 同款不落盘（跨重启不续，重新接管从整步重写开始）。
        self._cont_base = ""          # 已生成前文（断点底稿）
        self._cont_thinking = ""      # 上轮思考（英文安全评估不攒）
        self._cont_rounds = 0         # 续写模式请求数（成功说明用）
        self._cont_step_id = ""       # 断点归属（kind#章节id），换步骤即作废
        self._cont_soft_fails = 0     # 续写新增段连续不合格次数（第 2 次放弃断点）
        self._cont_last_new_len = 0   # 最近一次合格续写新增字数
        self._cont_last_cut = 0       # 最近一次续写拼接剪掉的接缝重复字数

    # ---------- 断点生命周期 ----------

    def _cont_reset(self):
        """断点整体作废：前文/思考/轮数清零（成功说明、换步骤、停机、fatal）。"""
        self._cont_base = ""
        self._cont_thinking = ""
        self._cont_rounds = 0
        self._cont_soft_fails = 0
        self._cont_last_new_len = 0
        self._cont_last_cut = 0

    def _cont_breakpoint_clear(self):
        """断点使命完成（新增段体检合格，底稿已并入正文）后清底稿；
        轮数留给成功说明（_apply_step 成功后播报）。"""
        self._cont_base = ""
        self._cont_thinking = ""
        self._cont_soft_fails = 0

    def _cancel_retry_timer(self):
        """取消待触发的重试定时器（暂停/停止/重新安排时调用）。"""
        t = getattr(self, "_retry_timer", None)
        if t is not None:
            t.cancel()
        self._retry_timer = None

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
        self._retry = 0
        self._stop_event.clear()
        self._cont_reset()
        self._cont_step_id = ""
        self._cancel_retry_timer()
        if self.scope == "outline":
            self._set_state("running", "⚡一键细化大纲：逐章细化还没细化的大纲，完成后即停")
        else:
            self._set_state("running")
        self._tick()

    def pause(self):
        if self._task:
            self._task.stop()
        self._cancel_retry_timer()   # 取消待执行的重试（继续后由 _tick 立即接手）
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
        self._cancel_retry_timer()
        self._cont_reset()   # 停机：断点一并作废（对齐桌面）
        self._cont_step_id = ""
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
        """状态机：找出下一个待办步骤。

        outline 范围（「一键细化全部大纲」）走另一条分支：只推进大纲细化，
        不做总纲/锁定/切章/细纲/正文/归档。
        """
        if self.scope == "outline":
            return self._next_outline_step()
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

    def _next_outline_step(self) -> dict | None:
        """outline 范围专用：按章节顺序返回第一个「大纲还没细化」的章；全做完 → None。

        不做总纲/锁定/切章/细纲/正文/归档——那个入口只买「细化大纲」这一件事。

        跳过规则（都是「用户已经定过稿」的章，绝不覆盖）：
        - `outline_refined=1`：已细化（或手动保存/粘贴过）= 本次任务的**进度标记**，
          停机、关程序后重新点一次，就从第一个没细化的章接着做；
        - `outline_locked=1`：本章大纲已锁定，视为定稿。

        注意：这里自己就把这两类排除了，`_do_outline` 执行前还会再判一次——
        两处都判，不会出现「跳过 → 又被评估成待办」的空转。
        """
        chapters = self.session.store.list_chapters()
        total = len(chapters)
        for i, ch in enumerate(chapters, 1):
            if ch.get("outline_refined") or ch.get("outline_locked"):
                continue
            return {"kind": "outline", "chapter": ch, "pos": (i, total)}
        return None

    def _chapter_todo(self, ch: dict) -> dict | None:
        """单章待办（规则见 core.dual_rules.chapter_todo）。

        手机不生成细纲（supports_detail=False），大纲细化后直接写正文。
        不能按 chapters.status 判完本：正文没写、状态仍停在「有大纲」时
        必须返回 prose，否则会把整本书误报成「已完本」。
        """
        store = self.session.store
        ch = store.get_chapter(ch["id"])
        if not ch:
            return None
        return core_chapter_todo(store, ch, supports_detail=False)

    def _on_step_result(self, step: dict):
        if not self._running or self._paused:
            return
        if step is None:
            self._running = False
            if self.scope == "outline":
                self._set_state("done", "✅ 全部章节大纲已细化完成")
            else:
                self._set_state("done", "🎉 全书完本！")
            return
        if not isinstance(step, dict) or not step.get("kind"):
            # _next_step 抛异常时 run_bg 会把异常包装成 {"ok": False, "error": ...}
            # 传回来（没有 kind）：不处理会 KeyError 后被 run_bg 吞掉、状态机
            # 静默卡死（界面停在旧状态不动），这里转为错误交还用户。
            self._running = False
            err = step.get("error", "步骤评估失败") if isinstance(step, dict) \
                else str(step)
            self._set_state("error", f"自动模式出错：{err}")
            return
        # 断点归属校验（对齐桌面）：步骤变了（暂停期间工程被手动改动等）就作废
        # 旧断点，防止把上一章的前文接到这一章头上。
        owner = _step_owner(step)
        if owner != self._cont_step_id:
            self._cont_reset()
            self._cont_step_id = owner
        kind = step["kind"]
        label = self._step_title(step)
        if step.get("pos"):
            # outline 范围带全书进度（i/total 章），让老板看得出还剩多少
            label = f"{label}（{step['pos'][0]}/{step['pos'][1]} 章）"
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

    def _step_title(self, step: dict) -> str:
        """步骤显示名：outline 范围的细化大纲用专属名，界面/日志能看出当前范围。"""
        if self.scope == "outline" and step.get("kind") == "outline":
            return OUTLINE_STEP_NAME
        return STEP_LABELS.get(step.get("kind", ""), "")

    def _run_gen(self, role, context, task, step, max_tokens=None, min_chars=None):
        """统一的自动生成调用（免预览、重试节拍、断连续写、失败暂停）。

        重试推杆（2026-09-19 与桌面版对齐）：**被渠道内容策略拦下时不带**——
        拦下说明请求根本没送到模型，「请务必按原任务要求重新输出」按构造帮不上忙；
        桌面版实测该形态 42 条请求 0 条产出正文，而同时段干净报文 155 条里 26 条出文。
        其余失败类型（拒答/空输出）仍带推杆；**续写轮不带**（推杆治「不肯写」，
        续写轮指令已明确写什么，与桌面同款）。

        推杆文案**必须与桌面同源**（`app.core.pipeline.REFUSAL_RETRY_EXTRA`）：
        本文件曾自带一份陈旧副本（文案已与桌面漂移），2026-09-19 改为延迟导入，
        与本工程其余部分一样在函数内 import（避免拖慢安卓冷启动）。

        断连续写接力（2026-11-15 对齐桌面 _run 头部）：此前断连攒下的断点
        （部分正文/上轮思考）→ 任务指令换成 build_continue_task 产物（前文/思考段
        是 critical 模块，超预算不裁）；continue_round 只在真带上了上轮思考时给
        （内核据此临时降思考档位，判据与手动「继续生成」同一口径：bool(思考)）。

        输出质量闸（2026-11-19，能力矩阵 D4）：判据与顺序在
        `app.core.quality_gate.output_issue`（与桌面 `_run` 同一个函数）——
        空输出/拒答/部分拒绝+降级替代/破限失败信号/字数下限/正文结尾未闭合/
        缺完成标志，外加两处豁免（分批非末批不跑未闭合；批尾状态栏已开始则两道
        闸都放行）。这里只负责把不合格转成既有重试节拍，**绝不进 _apply_step**：
        不合格的稿不落库、不置「已完成」、不归档。
        `min_chars` 默认取 `quality_gate.STEP_MIN_CHARS`（桌面每步显式传 300 的同值），
        个别步骤要差异时显式传，别在手机侧另立一套阈值。
        """
        from app.core.pipeline import REFUSAL_RETRY_EXTRA
        from app.core.pipeline import (build_continue_task,
                                       continuation_segment_issue,
                                       join_continuation)
        from app.core.quality_gate import (STEP_MIN_CHARS, describe_issue,
                                           output_issue)
        if min_chars is None:
            min_chars = STEP_MIN_CHARS
        cont_base = self._cont_base
        cont_think = self._cont_thinking
        cont_round = bool(cont_base.strip() or cont_think.strip())
        cont_think_attached = bool(cont_think.strip())
        cont_task = task  # 独立名字：不覆盖参数（闭包晚绑定教训，见 AGENTS.md）
        if cont_round:
            cont_task = build_continue_task(task, cont_base, cont_think)
            retry_extra = ""
        else:
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
            # 已停机（用户点停止 / 全量导入把旧会话作废）：旧任务的结果一律不落库、
            # 不排重试——否则迟到的成功结果会写进**已经被替换过**的资料库。
            if not self._running:
                return
            text = (result.get("text") or "").strip()
            from app.core.pipeline import detect_upstream_block
            # 先把「本次是不是被渠道拦下」记下来（下一次重试据此不叠推杆）
            self._last_fail_blocked = (bool(result.get("upstream_blocked"))
                                       or bool(detect_upstream_block(text)))
            if not result.get("ok"):
                self._fail(step, result.get("error", "生成失败"))
                return
            if result.get("status") == "stopped" or (
                    result.get("status") == "ok" and result.get("abrupt")):
                # 「停止」区分（对齐桌面 2026-09-08）：用户主动暂停就停在当前
                # 进度；流式半路网络中断/服务端掐断按断连处置——攒断点下一拍
                # 自动续写接力，不再原地停机也不再整章重写。
                # abrupt（2026-11-21 对齐桌面同款修复）：流式读到 EOF 但没收到
                # [DONE]/finish_reason = 渠道网关中途掐线，正文多半没写完——此前
                # 只认 stopped，abrupt 轮被质量闸 no_end_mark 判失败后全新重写。
                if self._paused:
                    self._set_state("paused", "已暂停")
                    return
                # 批尾状态栏收货豁免优先于断点（对齐桌面 2026-11-21）：正文已写完、
                # 只差批尾状态包的 abrupt 稿按「完整收货」放行，不攒断点续写状态栏。
                from app.core.pipeline import state_bar_started
                if not (result.get("abrupt")
                        and state_bar_started(text or "")):
                    self._accumulate_breakpoint(step, role, result)
                    return
            if result.get("safety_blocked"):
                # 平台内容审核掐断（2026-11-05，与桌面版对齐）：finish_reason=
                # sensitive / content_filter。审核看的是内容本身，不看推杆，
                # 同报文重试必然再被掐 → 不重试，直接停下交给人换渠道/降素材。
                # fatal（对齐桌面）：断点一并作废。
                self._cont_reset()
                self._retry = 0
                fr = result.get("finish_reason") or "content_filter"
                self._fail(step, f"被平台内容审核在生成中途掐断（finish_reason={fr}）——"
                                 "不是破限失效也不是网络问题；同报文重试无效，"
                                 "请换渠道/档案，或降低该段素材的极端度")
                return
            if self._last_fail_blocked:
                # 拦截占位说明不是正文，绝不能丢给解析器（解析成空只会报
                # 「解析失败」，老板看不出真正原因）
                self._retry += 1
                self._bump_retry_or_fail(
                    step, "被渠道内容策略拦截（请求没送到模型就被上游拦下）")
                return
            # 空输出 / 拒答不再在这里单独判：交给下方质量闸（与桌面同一个函数），
            # 这样「部分拒绝 + 降级替代」才会被判成 partial_refusal 而不是笼统的
            # 「空或拒答」，任务中心能看出命中哪一句。
            # 续写轮：先**单独体检这一轮新写出来的那段**，合格才拼回前文
            # （对齐桌面 2026-11-10）。旧闸门看的是合并稿：前文够长就必然过闸，
            # 「续写 0 字」「只补一句此处省略」都会落库。现在不合格只丢弃该段、
            # 底稿保留（下轮仍从上次正常处续）；连续 2 次放弃断点整步重写。
            if cont_round:
                issue = continuation_segment_issue(role, text, cont_base)
                if issue:
                    self._cont_soft_fails += 1
                    fails = self._cont_soft_fails
                    self._retry += 1
                    if fails >= CONT_SOFT_FAIL_LIMIT:
                        self._cont_reset()   # 放弃断点：下一拍按原始任务整步重写
                        self._bump_retry_or_fail(
                            step, f"续写连续 {fails} 次不合格（{issue}），"
                                  "已放弃断点，下一拍整步重写")
                    else:
                        self._bump_retry_or_fail(
                            step, f"续写新增段不合格（{issue}）：已丢弃该段，"
                                  f"保留前文 {len(cont_base)} 字，下轮从断点重续")
                    return
                self._cont_last_new_len = len(text)
                head = cont_base.strip()[:60]
                if head and text.startswith(head):
                    # 整篇重写护栏（对齐桌面）：模型以断点结尾开头整篇重写 → 以新输出为准
                    pass
                else:
                    joined = join_continuation(cont_base, text)
                    # 接缝重复统计（2026-11-21 对齐桌面）：窗口已扩 400 字，
                    # 条目级重复（模型重写断点尾部整条再续）也能剪干净。
                    self._cont_last_cut = max(0,
                        len(cont_base) + len(text) - len(joined))
                    text = joined
                self._cont_rounds += 1
            # 输出质量闸（能力矩阵 D4，判据/顺序/豁免全在 core，与桌面同一个函数）：
            # 不合格 → 走既有重试节拍，**不进 _apply_step**（不落库、不置已完成、
            # 不归档）。续写轮的底稿此时**不清**（对齐桌面：质量闸失败留给下一拍
            # 继续接力，真正成功才清）——见下方 gate 通过后的 _cont_breakpoint_clear。
            gate = output_issue(role, text, result, context=context,
                                min_chars=min_chars)
            if gate:
                self._retry += 1
                self._bump_retry_or_fail(step, describe_issue(gate))
                return
            if cont_round:
                self._cont_breakpoint_clear()  # 真正成功：断点使命完成，轮数留给说明
            self._retry = 0
            self._apply_step(step, result, text)

        start_generation(self.page, self.session, role=role, context=ctx,
                         task=cont_task, title=self._step_title(step),
                         on_done=on_done, on_text=on_text, on_started=on_started,
                         preview=False, max_tokens=max_tokens,
                         continue_round=cont_think_attached)

    def _accumulate_breakpoint(self, step, role, result):
        """断连处置（对齐桌面）：成规模的已产出攒成断点，下一拍重试自动续写
        而不是从头重写。断点跨连续断连累加（join_continuation 接缝去重 +
        trim_unfinished_tail 裁半句），思考同理累加；英文安全评估不攒。

        断片也要体检：模型在断连前可能已经开始写「抱歉」「此处省略」，把这种
        断片攒进底稿 = 下次续写从垃圾后面接，越接越歪。命中即丢弃该断片，
        底稿保持在上一次正常内容上。太短的断片不攒（碎片续写容易整篇重起），
        按普通失败走既有重写节拍。
        """
        from app.core.pipeline import (detect_blocked_output, detect_refusal,
                                       detect_soft_evasion, detect_upstream_block,
                                       is_safety_thinking, join_continuation,
                                       trim_unfinished_tail)
        part = (result.get("text") or "").strip()
        if part and (detect_refusal(part) or detect_blocked_output(part)
                     or detect_upstream_block(part) or detect_soft_evasion(part)):
            self._retry += 1
            self._bump_retry_or_fail(
                step, "生成中途断连，但断片疑似拒答/自我审查，已丢弃"
                      "（底稿保留上次正常内容）")
            return
        pthink = (result.get("thinking") or "")
        keep_think = bool(pthink.strip()) and not is_safety_thinking(pthink)
        # 思考断点豁免（2026-11-21 对齐桌面同款修复）：思考是「结论」，非安全
        # 评估就照攒——断片碎（<500 字门槛，碎片续写容易整篇重起）只影响断片
        # 入不入底稿，不影响思考。碎断片+长思考（思考型模型思考 1~4 万字、正文
        # 刚吐几十字被渠道掐线）此前思考白烧不攒 = 每轮重写都从头思考又死同处。
        if len(part) >= AUTO_CONTINUE_MIN_CHARS:
            if part:
                self._cont_base = join_continuation(
                    self._cont_base, trim_unfinished_tail(part))
            if keep_think:
                self._cont_thinking = (self._cont_thinking + "\n" + pthink).strip()
            self._cont_rounds += 1
            self._retry += 1
            extra = f"，思考 {len(self._cont_thinking)} 字" if self._cont_thinking else ""
            self._bump_retry_or_fail(
                step, "生成中途断连（疑似网络中断/服务端掐断）｜已保留断点"
                      f"（前文 {len(self._cont_base)} 字{extra}），稍后自动续写")
            return
        if keep_think:
            # 只攒思考：碎断片不入底稿（防碎片续写整篇重起），思考断点照攒——
            # 下一拍 build_continue_task 走「无正文思考续写」模式，模型照结论写。
            self._cont_thinking = (self._cont_thinking + "\n" + pthink).strip()
            self._cont_rounds += 1
            self._retry += 1
            self._bump_retry_or_fail(
                step, "生成中途断连｜已保留思考断点（思考 "
                      + str(len(self._cont_thinking)) + " 字），稍后从思考结论"
                        "续写（碎断片不入底稿）")
            return
        self._retry += 1
        self._bump_retry_or_fail(
            step, "生成中途断连（断片太短，不值得续写），按普通失败重试")

    def _bump_retry_or_fail(self, step, err):
        """计入本次失败并按节拍安排下一拍（与桌面 RETRY_DELAYS 同源）。

        手机无 Qt 定时器，用 threading.Timer 延时下一拍（daemon 线程，
        暂停/停止时取消）。超过 MAX_RETRY（3 轮 9 次）才暂停交还用户；
        **断点保留**（点「继续」从断点接着接力，对齐桌面）。
        """
        self._cancel_retry_timer()
        if self._retry >= MAX_RETRY:
            self._retry = 0
            self._fail(step, f"{err}｜连续 {MAX_RETRY} 次失败，已暂停")
            return
        delay = RETRY_DELAYS[self._retry - 1]
        self._set_state("retrying",
                        f"{err}；{_fmt_delay(delay)}后自动重试"
                        f"（第 {self._retry + 1}/{MAX_RETRY} 次）")
        t = threading.Timer(delay, self._tick)
        t.daemon = True
        self._retry_timer = t
        t.start()

    def _fail(self, step, msg):
        self._paused = True
        self._set_state("error", f"{self._step_title(step)}：{msg}")

    def _apply_step(self, step, result, text):
        """解析并落库（工作线程回调里执行；落库后 tick 下一步）。

        只处理**已过 `_run_gen` 质量闸**的文本：闸门不合格的结果在这里根本到不了，
        所以本函数里的 `accept_chapter_prose` / `set_chapter_status("已完成")` /
        写批次 / 置 `outline_refined` 都只对合格稿生效。
        """
        from app.core.pipeline import MIN_OUTLINE_CHARS, Pipeline
        store = self.session.store
        kind = step["kind"]
        # 断点接力成功说明（对齐桌面 ok note：「断点续写 N 轮接力」）：
        # 续写轮走到这里就算使命完成（底稿已并入 text），无论本步解析成败
        # 轮数都归零——解析失败的重试是不带断点的普通重试。
        cont_note = ""
        if self._cont_rounds:
            tail = (f"（末轮新增 {self._cont_last_new_len} 字"
                    + (f"，剪掉接缝重复 {self._cont_last_cut} 字"
                       if self._cont_last_cut else "") + "）"
                    if self._cont_last_new_len else "")
            cont_note = f"断点续写 {self._cont_rounds} 轮接力{tail}"
        self._cont_rounds = 0
        self._cont_last_new_len = 0
        self._cont_last_cut = 0
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
                # 解析门槛 + 章数校验与桌面同源（2026-11-17）
                chapters, err = parse_volume_split(
                    text, step.get("expect_chapters"))
                if err:
                    raise ValueError(err)
                vol = step["volume"]
                for c in chapters:
                    store.add_chapter({**c, "status": "有大纲",
                                       "volume_id": vol["id"]})
            elif kind == "outline":
                ch = store.get_chapter(step["chapter"]["id"])
                # 空壳大纲门槛与桌面同源（MIN_OUTLINE_CHARS=200）：只回标题/一段
                # 衔接段也算失败 → 走重试，不置 outline_refined（否则续跑跳过该章）。
                data, err = Pipeline.parse_outline_chapter(
                    text, ch, field_defs=store.get_outline_fields(),
                    min_chars=MIN_OUTLINE_CHARS)
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
            self._bump_retry_or_fail(step, str(e))
            return
        if cont_note:
            self._set_state("running", f"{self._step_title(step)}：{cont_note}")
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
        rule, expected = volume_chapter_rule(store)
        # 期望章数在请求时快照进 step：重试/落库都按同一个数校验
        step["expect_chapters"] = expected
        task = render_task("volume", store, chapter_rule=rule,
                           volume_no=vol.get("order_index") or 1,
                           volume_count=len(store.list_volumes()))
        self._run_gen("volume", {"volume_id": vol["id"],
                                 "expect_chapters": expected}, task, step)

    def _do_outline(self, step):
        from app.core.task_templates import render as render_task
        store = self.session.store
        ch = store.get_chapter(step["chapter"]["id"])
        if not ch:
            # 章已被删除（评估到执行之间）：跳过，直接评估下一步（对齐桌面）
            self._tick()
            return
        if self.scope == "outline" and ch.get("outline_locked"):
            # 只细化范围：锁定=用户已定稿，绝不覆盖。_next_outline_step 同样
            # 排除锁定章，两处都判不会「跳过→又被评估成待办」空转
            # （对齐桌面 _step_outline）；all 范围保持原行为（照常细化）。
            self._tick()
            return
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
        mode = store.get_prose_mode()
        if mode == "batch":
            self._do_prose_batch(step, ch, cid)
            return
        if mode == "outline" and store.get_outline_batch_split() != "whole":
            self._do_prose_outline_batch(step, ch, cid)
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
        if mode == "outline":
            # 按大纲直写（整章）：组装器跳过细纲，只用本章大纲
            context["outline_only"] = True
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
        # 两种分批共用 prose_batches：别的写法留下的批次批号与新计划错位，整章作废
        foreign = store.drop_foreign_prose_batches(cid, "batch")
        rows = store.list_prose_batches(cid)
        if not foreign and any(
                any(i not in valid_ids for i in store.prose_batch_block_ids(r))
                for r in rows):
            store.clear_prose_batches(cid)
            rows = []
        value, by_count = store.get_prose_batch_plan_value()
        batches = plan_batches(blocks, value, by_count)
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
            # 重写整章时不能拿本章旧正文当「前文结尾」（与桌面同口径）；
            # 跨章首批带上一章最后一批原文（没有批次则带上一章正文结尾）
            context["previous_end_override"] = Assembler(store).prose_prev_context(cid, [])
        else:
            task = render_task("prose_batch_next", store, batch_no=next_idx, **common)
            context["batch_state"] = store.prose_batch_state_package(cid)
            context["previous_end_override"] = Assembler(store).prose_prev_context(cid, rows)
        self._run_gen("prose", context, task, step)

    def _do_prose_outline_batch(self, step, ch: dict, cid: int):
        """按大纲分批（2026-10-17，对齐桌面）：切段依据是本章大纲节拍，
        不需要细纲块；滚动状态包与细纲分批共用。

        步骤机按章状态评估进度：非末批落批后章仍是「写作中」→ 重新排出
        prose 步骤，自然接力；末批合并接受后进入归档。
        """
        from app.core.chapter_outline import chapter_outline_text
        from app.core.pipeline import (plan_outline_batches, batch_tail_two_paras,
                                       outline_batch_scope_text)
        from app.core.task_templates import (render as render_task,
                                             OUTLINE_BATCH_FINAL_NOTE)
        store = self.session.store
        outline = chapter_outline_text(ch)
        if not outline.strip():
            self._fail(step, "本章还没有大纲，无法按大纲写正文")
            return
        free = store.get_outline_batch_split() == "free"
        value, by_count = store.get_outline_batch_plan_value()
        plans = plan_outline_batches(outline, value, by_count)
        if not plans:
            self._fail(step, "本章大纲为空，无法分批")
            return
        # 细纲分批留下的批次（block_ids 非空）按大纲批号接续会串批 → 整章作废
        store.drop_foreign_prose_batches(cid, "outline")
        rows = store.list_prose_batches(cid)
        next_idx = len(rows) + 1
        if next_idx > len(plans):
            # 批全齐但正文未接受（上次中断在合并前）→ 直接合并收尾
            merged = store.prose_batch_merged(cid)
            if not merged.strip():
                self._fail(step, "批次记录为空且无已写正文")
                return
            store.accept_chapter_prose(cid, merged)
            store.set_chapter_status(cid, "已完成")
            self._tick()
            return
        is_final = next_idx == len(plans)
        if free:
            scope_brief = (f"本章共 {len(plans)} 批，本批是第 {next_idx} 批："
                           f"推进到全章约 {next_idx}/{len(plans)} 处，"
                           "停在一个自然场景的收束点，后面的内容留给下一批。")
        else:
            cur = plans[next_idx - 1]
            scope_brief = (f"本批覆盖：{cur['label']}"
                           f"（全章 {cur['total_beats']} 个节拍中的第 {cur['first']}~{cur['last']} 个）。"
                           "只写这一段，后面的节拍留给下一批。")
        if is_final:
            scope_brief += "本批是最后一批：把大纲剩下的内容全部写完，并收好章尾。"
        # 本模式没有细纲块：ids 存空数组（_apply_step 落批逻辑通用）
        step["_batch"] = {"index": next_idx, "ids": [], "final": is_final}
        context = {
            "chapter": ch,
            "character_names": ch.get("characters") or [],
            "outline_only": True,
            "outline_batch_text": outline_batch_scope_text(plans, next_idx, free=free),
            "notes": store.list_notes(cid),
            "batch_index": next_idx,
            "batch_total": len(plans),
        }
        common = dict(chapter_no=ch.get("order_index", "?"),
                      chapter_title=ch.get("title") or "未命名",
                      batch_no=next_idx,
                      batch_total=len(plans),
                      batch_scope=scope_brief,
                      final_note=OUTLINE_BATCH_FINAL_NOTE if is_final else "")
        task = render_task("prose_outline_first" if next_idx == 1 else "prose_outline_next",
                           store, **common)
        if next_idx == 1:
            context["previous_end_override"] = Assembler(store).prose_prev_context(cid, [])
        else:
            context["batch_state"] = store.prose_batch_state_package(cid)
            context["previous_end_override"] = Assembler(store).prose_prev_context(cid, rows)
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
