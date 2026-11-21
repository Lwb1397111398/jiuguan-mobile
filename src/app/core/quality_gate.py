# -*- coding: utf-8 -*-
"""自动模式输出质量闸（双端单一裁决源，能力矩阵 D4）。

为什么单独成一个模块：这些闸原先只写在桌面 `app/ui/auto_pilot.py::_run` 里，
手机 `src/autopilot.py::_run_gen` 只做了「空 / 拒答 / 渠道拦截」三项，
半截稿、缺完成标志、过短稿、部分拒绝 + 降级替代都能在手机落库（矩阵 D4 漂移）。
判据本身早就在 `pipeline` 里（`detect_refusal` / `detect_unfinished` /
`endmark` …），漂移的是「哪几道闸算失败、按什么顺序、给谁豁免」这层编排，
所以把它下沉到这里，两端 UI 只负责把失败转成自己的重试/暂停节拍。

约定：
- 纯函数：不碰数据库、不弹窗、不发网络、不依赖 Qt/Flet；
- **不在这里另立阈值**：`min_chars` 由调用方按步骤传桌面既有口径（300），
  本模块只做 `len(text) < min_chars` 的比较；
- 文案由 `describe_issue()` 统一（不含「已带强化推杆重试」这类端内动作说明），
  各端自己补自己的动作后缀。

顺序即语义（与桌面 2026-09-05 起的实现逐条对齐）：
上游拦截 → 静默拒答 → 空输出 → 部分拒绝 → 拒答 → 破限失败信号 →
字数下限 → 正文结尾未闭合 → 缺完成标志。
"""
from .pipeline import (detect_blocked_output, detect_partial_refusal,
                       detect_refusal, detect_unfinished, state_bar_started)

# 自动模式各生成步骤的字数下限（沿用桌面既有口径，2026-09-05 起就是 300）。
# 不要在这里改成「按步骤分档」的新阈值：桌面每个 _step_* 都显式传 300，
# 只有大纲解析另有一道 MIN_OUTLINE_CHARS=200 的正文门槛（在 Pipeline 里）。
STEP_MIN_CHARS = 300

# 正文类角色：末尾标点启发式与批尾状态栏豁免只对它们成立
# （结构化阶段的末行常是字段行，天然不带句号，套用必误判）。
PROSE_ROLES = ("prose", "prose_rewrite")

# 失败代码（两端按同一份代码做本地化文案与重试动作）
UPSTREAM_BLOCKED = "upstream_blocked"
SILENT_REFUSAL = "silent_refusal"
EMPTY = "empty"
PARTIAL_REFUSAL = "partial_refusal"
REFUSAL = "refusal"
BLOCKED_OUTPUT = "blocked_output"
TOO_SHORT = "too_short"
UNFINISHED = "unfinished"
NO_END_MARK = "no_end_mark"

ALL_CODES = (UPSTREAM_BLOCKED, SILENT_REFUSAL, EMPTY, PARTIAL_REFUSAL,
             REFUSAL, BLOCKED_OUTPUT, TOO_SHORT, UNFINISHED, NO_END_MARK)


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def is_mid_batch(context: dict | None) -> bool:
    """是否「分批正文的非末批」。

    非末批本批结尾停在章中间是正常形态，末尾标点启发式（`detect_unfinished`）
    必然误判，故豁免「结尾未闭合」这一道；完成标志闸**不豁免**（每批末尾都有
    状态包，模型应照常打【生成完毕】）。
    """
    ctx = context or {}
    index = _int(ctx.get("batch_index"))
    total = _int(ctx.get("batch_total"))
    return index > 0 and index < total


def has_state_bar(text: str) -> bool:
    """批尾状态栏是否已开始（= 正文已写完，判据唯一源在 pipeline）。"""
    return state_bar_started(text or "")


def safety_pause_needed(result: dict, text: str) -> bool:
    """平台审核掐断（safety_blocked）是否需要 fatal 暂停（2026-11-21 老板裁定）。

    两条规则合流：审核掐断「不重试、不换渠道」的纪律不变，但**正文已完成时
    收货优先于暂停**——审核掐在批尾状态包半截（【滚动状态包】已出现）说明
    正文已写完，掐掉的只是状态包，整篇作废纯属浪费（2026-11-21 小偷入室
    实测：id=84 正文完整、状态包 to状态 被掐半截，自动模式却 fatal 丢弃）。

    - 正文已完成（pipeline 已标 state_bar_accepted，或末尾状态栏已开始）
      → False：放行收货，照常落库。不重试的纪律不受影响——收货本来就不重试。
    - 正文未完成就被掐 → True：维持 fatal 暂停（换渠道/降素材极端度由人决定）。
    """
    if not result.get("safety_blocked"):
        return False
    if result.get("state_bar_accepted"):
        return False
    return not has_state_bar(text or "")


def output_issue(role: str, text: str, result: dict | None = None,
                 context: dict | None = None, min_chars: int = 0) -> dict | None:
    """判一次生成结果是否有质量问题。

    返回 `None` = 通过；否则返回 `{"code": ..., ...}`：
    - `partial_refusal` / `blocked_output` 附 `detail`（命中的片段，给老板看）；
    - `too_short` 附 `chars` / `min_chars`。

    `text` 必须是**清洗后**的正文（`pipeline.run` 返回的 `text`）；续写轮传
    **拼接后**的全文（与桌面同口径：`min_chars` 按全文计，防「只补 200 字收尾」
    被判失败死循环）。
    """
    result = result or {}
    text = text or ""
    # 渠道输入端拦截：回传的是拦截说明而非正文（2026-09-10 实修）。
    if result.get("upstream_blocked"):
        return {"code": UPSTREAM_BLOCKED}
    # 思考中止（2026-09-11）：请求到了模型，但模型思考阶段做完安全评估后拒写。
    if result.get("silent_refusal"):
        return {"code": SILENT_REFUSAL}
    if not text:
        return {"code": EMPTY}
    # 「部分拒绝 + 降级替代」（2026-11-11）：自带完成标志、长度也够，
    # 只有开头声明的边界 + 替代方案能认出来 → 必须先于普通拒答判定，
    # 好让任务中心说清是哪一句被判失败。
    partial = detect_partial_refusal(text)
    if partial:
        return {"code": PARTIAL_REFUSAL, "detail": partial}
    if detect_refusal(text):
        return {"code": REFUSAL}
    blocked = detect_blocked_output(text)
    if blocked:
        return {"code": BLOCKED_OUTPUT, "detail": blocked}
    if min_chars and len(text) < min_chars:
        return {"code": TOO_SHORT, "chars": len(text), "min_chars": min_chars}
    bar_ok = has_state_bar(text)
    # 只对正文/改写、非末批生效：末批/整章/改写停在章中间 = 半截稿。
    if (role in PROSE_ROLES and not is_mid_batch(context)
            and not bar_ok and detect_unfinished(text)):
        return {"code": UNFINISHED}
    # 输出完成标志缺失（endmark.py，软截断兜底）。开关关闭时 pipeline 已把
    # end_mark 恒置 True；结构完整性复核通过时也已归一为 True。
    if not result.get("end_mark", True):
        # 豁免只给正文/改写（状态包是分批正文独有的批尾结构，大纲/细纲末尾的
        # 字段行不算），与 pipeline 内 state_bar_accepted 的角色范围一致。
        if not (role in PROSE_ROLES and bar_ok):
            return {"code": NO_END_MARK}
    return None


def describe_issue(issue: dict) -> str:
    """把失败代码转成一句给人看的中文（不含端内重试动作）。"""
    code = (issue or {}).get("code")
    if code == UPSTREAM_BLOCKED:
        return "被渠道内容策略拦截（请求没送到模型就被上游拦下）"
    if code == SILENT_REFUSAL:
        return "模型思考后安全评估中止，未产出正文（输出全为英文评估）"
    if code == EMPTY:
        return "输出为空或疑似拒答"
    if code == PARTIAL_REFUSAL:
        return (f"模型部分拒绝并给了降级替代方案"
                f"（命中「{issue.get('detail') or ''}」）")
    if code == REFUSAL:
        return "输出疑似拒答"
    if code == BLOCKED_OUTPUT:
        return f"输出疑似破限失败（命中「{issue.get('detail') or ''}」）"
    if code == TOO_SHORT:
        return (f"输出仅 {issue.get('chars', 0)} 字"
                f"（低于 {issue.get('min_chars', 0)} 字下限），疑似未按要求生成")
    if code == UNFINISHED:
        return "正文结尾未闭合（疑似写一半被截断/自我审查）"
    if code == NO_END_MARK:
        return "输出末尾未检测到完成标志（疑似中途中断/未写完）"
    return "输出质量不合格"
