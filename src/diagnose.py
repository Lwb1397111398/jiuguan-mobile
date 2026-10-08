# -*- coding: utf-8 -*-
"""生成结果诊断（手机版）：与桌面 `TaskRunDialog._on_done` 的三态诊断同源同义。

三态互斥（顺序即优先级）：
  1) 拒答/退缩 —— 输出头/尾出现拒答腔（破甲失败）
  2) 上限截断   —— finish_reason=length
  3) 未写完     —— 没截断没拒答腔，但结尾不闭合，或完成标志缺失
另有两类「非模型原因」信号（桌面同款，处置方式不同）：
  · 上游渠道拦截（请求根本没送到模型）
  · 模型思考后安全评估中止（到了模型，模型自己不写正文）

diagnose(result, role, text) -> (level, reason, message)
  level   ""      无异常
          "warn"  可疑：可以继续，但落库前要人工确认
          "error" 严重：建议换渠道/档案重试
  reason  简短原因（写进「落库前确认」；与桌面 `_incomplete_reason` 同语义）
  message 给用户看的完整说明

调用方约定：只有 `ok=True` 且文本非空才做判定（失败路径由生成入口统一报错）。
返回的 `reason` 非空即代表「疑似不完整」，UI 必须在写入前弹一次确认——
这是桌面 2026-09-10 定下的红线（细纲缺一块也能落库那次）。
"""


def diagnose(result, role: str, text: str, batch_continue: bool = False):
    """返回 (level, reason, message)。level 为 "" 表示无异常。

    batch_continue=True（分批续批，桌面 TaskRunDialog._batch_continue 同口径）：
    本批结尾停在章中间是正常形态 → 豁免「未写完」的末尾标点启发式；
    end_mark 缺失检查保留（流式中途被掐断仍要报）。
    """
    if not isinstance(result, dict) or not result.get("ok"):
        return "", "", ""
    text = (text or "").strip()
    if not text:
        return "", "", ""

    from app.core.pipeline import detect_refusal, detect_unfinished

    # 1) 上游输入端拦截：请求没送到模型，回传的是渠道的拦截说明（与破甲无关）
    if result.get("upstream_blocked"):
        return ("error",
                "被渠道内容策略拦截（不是模型拒答，请求没送到模型）",
                "本次内容被渠道的内容策略拦下了——请求还没送到模型就被上游拒绝，"
                "返回的是一段拦截说明（既不是正文，也不是模型拒答）。"
                "这不代表破甲失效：同一份内容换个通道/模型通常就能过。"
                "建议到「模型」页给该任务角色换一个档案再生成。")

    # 2) 安全评估中止：请求到了模型，但模型思考后自己拒写（整段英文评估文字）
    if result.get("silent_refusal"):
        hit = result.get("silent_refusal") or ""
        return ("error",
                "模型思考后安全评估中止，未产出正文",
                f"模型收到请求了，但思考阶段做完安全评估后拒绝写正文——"
                f"返回的整段都是英文评估文字（命中「{hit}」），没有任何中文内容。"
                "这与「被渠道拦截」不同：请求确实到了模型，是模型自己不写。"
                "建议到「模型」页给该角色换一个档案，或稍后重试"
                "（该判定紧贴阈值、会随时间波动）。")

    # 3) 拒答/退缩（破甲失败的第一形态）
    if detect_refusal(text):
        return ("warn",
                "输出疑似拒答，可能没按要求生成",
                "输出疑似拒答（可能未按要求生成正文）。可直接编辑当前内容后落库，"
                "或换个档案重新生成（落库前会再确认一次）。")

    # 4) 连接被服务端/网关中途掐断（EOF 但没收到结束标记）
    if result.get("abrupt"):
        secs = (result.get("duration_ms") or 0) / 1000
        return ("warn",
                f"连接在输出中途被断开（约 {secs:.0f} 秒）",
                f"连接在输出中途被断开（约 {secs:.0f} 秒）——常见是渠道/网关超时限制"
                "（如 100 秒）或网络波动，内容可能没写完。若反复出现，"
                "建议到「模型」页把该档案的超时调大，并确认渠道没有短超时限制。")

    # 5) 上限截断（finish_reason=length）
    if result.get("truncated"):
        cont = int(result.get("continued") or 0)
        reason = (f"输出被长度上限截断（已自动续写 {cont} 轮仍被截断）" if cont > 0
                  else "输出被长度上限截断")
        cont_txt = f"已自动续写 {cont} 轮仍被截断。" if cont > 0 else ""
        return ("warn", reason,
                f"输出被长度上限截断（finish_reason=length），正文可能没写完。{cont_txt}"
                "若反复截断，请到「模型」页把该档案的输出上限调大，"
                "或到设置页调高防截断续写轮数，然后重新生成。")

    # 6) 未写完：完成标志缺失（软截断），或正文/改写的末尾标点启发式命中。
    #    末尾标点启发式只对 prose/rewrite 生效（与桌面、自动模式同口径）：
    #    结构化阶段（大纲/细纲/分卷/总纲）的末行常是 `to：状态行` / 字段行，
    #    天然不带句号 → 必然误判；它们的完整性由完成标志 + 结构备选负责。
    #    分批续批（batch_continue）同样豁免本启发式：本批结尾停在章中间
    #    是正常形态（与桌面 TaskRunDialog._batch_continue 同口径）。
    unfinished = not result.get("end_mark", True)
    if not unfinished and role in ("prose", "prose_rewrite") and not batch_continue:
        unfinished = detect_unfinished(text)
    if unfinished:
        if not result.get("end_mark", True):
            reason = "末尾没有检测到「完成标志」，输出可能中途中断（缺一块）"
        else:
            reason = "结尾没有闭合，输出可能被中途掐断"
        return ("warn", reason,
                "输出可能没写完（结尾未闭合，或末尾未检测到完成标志）——常见原因是"
                "模型写到一半自我审查停下（破甲没完全生效）、输出被静默截断，"
                "或内容中途中断。可直接编辑后落库（会再确认一次），或换个档案重试。")

    # 7) 无异常：结构完整性备选/草稿缺失只作提示，不算不完整
    note = ""
    fb = (result.get("complete_fallback") or "").strip()
    if fb:
        note = f"未见完成标志，已按结构完整性判定为完成（{fb}）。"
    if result.get("draft_expected") and not result.get("draft"):
        extra = "已开启草稿自检，但模型未输出草稿（可能写进了思考面板），本次没有自检过程。"
        note = (note + " " + extra).strip() if note else extra
    return "", "", note
