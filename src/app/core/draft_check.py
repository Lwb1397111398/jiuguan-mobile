# -*- coding: utf-8 -*-
"""草稿自检模式（2026-08-31）。

参考 SillyTavern 预设 <draft> 机制：生成时先输出 <draft>草稿+逐项自检</draft>，
自检全部通过后再输出正式内容；pipeline.split_draft 在解析层剥离草稿块，
正式内容入库，草稿仅在生成对话框临时查看。与事后审校（checker/style_checker）
互补：那是"写完再查"，这是"写前自检"。

本模块职责：开关判断 + 各阶段内置自检清单（draft_checklist_<stage> 可替换式自定义）+
用户附加检查项拼入（通用 draft_check_extra + 阶段专属 draft_check_extra_<stage>）+
协议全文组装。任务硬性要求（必含要素/章范围/涉及人物）已在任务指令中明确，不在此重复。
"""

# 支持草稿自检的阶段（rewrite/check/style/memory 等审校机械角色不参与：
# check 本身就是自检，rewrite 改写短文本收益低）
DRAFT_STAGES = ("synopsis", "volume", "outline", "detail", "prose")

_STAGE_LABELS = {
    "synopsis": "总纲",
    "volume": "分卷",
    "outline": "大纲",
    "detail": "细纲",
    "prose": "正文",
}

# 各阶段内置自检清单。正文用文风类清单（取自酒馆预设草稿模式的精华条目，
# 去掉吐槽人格）；其余阶段用结构类简化清单。
DRAFT_CHECKLISTS = {
    "prose": [
        "人物名字、称谓与人物状态卡及涉及人物完全一致（无错名、无临时改名），言行符合人设无 OOC",
        "句式健康：无「不是A，是B」式全肯定句、无破折号、无「x了x」叠词（看了看/想了想）、无「那+量词」堆砌，长短句交错",
        "动作描写具体：用可见的具体动词推进画面；无同一动作反复拆解、无套路微动作（攥紧又松开/指节泛白/喉结滚动）",
        "视角单一：严格限定在视点人物可知范围内；配角无心理描写（情绪用动作/语言/表情外显），无「目光扫过/映入眼帘」类越视角写法",
        "对白自然：无「这句话落下/话说完」类余波、无对白后自我解释短句；对白与动作穿插，不整段独白",
        "环境描写服务剧情与氛围：至少一处非视觉感官锚点，无删掉不影响剧情的无效描写，结尾不落等待式收束",
        "剧情推进：信息增量/关系变化/局面改变至少其一；严格按施工单推进无漏块跳步；不替用户角色行动或发言",
    ],
    "synopsis": [
        "主角设定清晰（身份/动机/性格），与创作要求及锁定项完全一致",
        "整体结构与各分卷划分完整，卷间承接自然",
        "保持概述层级，不写成详细剧情",
    ],
    "volume": [
        "章数符合本次分卷要求；每章独立推进、章末带钩子",
        "各章标题与概述按『## 第N章：章名』格式，序号连续",
        "与总纲中本卷的剧情走向一致，不越卷写后续内容",
    ],
    "outline": [
        "章数与本卷章节模块一致、顺序一一对应，不漏章不增章",
        "每章大纲细化充实（序号点完整），承接前文与已埋伏笔",
        "只处理本次目标章节/本卷范围，不越界",
    ],
    "detail": [
        "逐块覆盖本章大纲全部序号点，不漏点、不跳步",
        "必含要素与叙事任务落进对应块，块间衔接自然",
        "认知规则不违背：各块内容不越角色视角的信息边界",
    ],
}

# 开启草稿自检的阶段，auto 兜底输出上限的追加量。草稿 = 本段正文初稿（约正式七成）
# + 逐项自检 + 修改指导，输出翻倍余量约 12000 token；api.py 对超模型上限已有
# 自动减半降级，无报错风险。
DRAFT_EXTRA_TOKENS = 12000


def parse_stages(raw: str) -> set:
    """把 draft_check_stages 存储值（逗号分隔）解析为阶段名集合。"""
    out = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if part in DRAFT_STAGES:
            out.add(part)
    return out


def draft_enabled_for(stage: str, store) -> bool:
    """判断某阶段是否开启草稿自检；stage 不在 DRAFT_STAGES 一律 False。"""
    if stage not in DRAFT_STAGES:
        return False
    try:
        raw = store.get_setting("draft_check_stages") or ""
    except Exception:
        return False
    return stage in parse_stages(raw)


# 各阶段专属附加检查项的全局键（仅对应阶段生效；通用键 draft_check_extra 所有阶段共享）
EXTRA_STAGE_KEYS = {stage: f"draft_check_extra_{stage}" for stage in DRAFT_STAGES}

# 各阶段自定义自检清单的全局键（替换式：填写=完全用自己的清单，留空=回退内置 DRAFT_CHECKLISTS）
CHECKLIST_KEYS = {stage: f"draft_checklist_{stage}" for stage in DRAFT_STAGES}


def _user_checklist(stage: str, store):
    """用户自定义自检清单（draft_checklist_<stage>，每行一条去空）；未设置/全空返回 None。"""
    try:
        raw = store.get_setting(CHECKLIST_KEYS.get(stage, f"draft_checklist_{stage}")) or ""
    except Exception:
        return None
    items = [ln.strip().lstrip("-•·").strip() for ln in raw.splitlines() if ln.strip()]
    return items if items else None


def _extra_items(stage: str, store) -> list:
    """用户附加检查项：通用（draft_check_extra）+ 本阶段专属（draft_check_extra_<stage>）
    合并，每行一条，去空行去重。顺序：通用在前、阶段专属在后。"""
    items, seen = [], set()

    def _append(raw: str):
        for line in (raw or "").splitlines():
            line = line.strip().lstrip("-•·").strip()
            if line and line not in seen:
                seen.add(line)
                items.append(line)

    try:
        _append(store.get_setting("draft_check_extra"))
        _append(store.get_setting(EXTRA_STAGE_KEYS.get(stage, "draft_check_extra")))
    except Exception:
        return items
    return items


def _draft_mode(store) -> str:
    """草稿模式（作用于所有阶段；键名 prose_mode 为历史遗留）：
    full=全量 / interleaved=穿插。非法值一律按 full 处理。"""
    try:
        m = (store.get_setting("draft_check_prose_mode") or "").strip().lower()
    except Exception:
        return "full"
    return m if m in ("full", "interleaved") else "full"


# 穿插草稿模式：字数驱动分段（老板 2026-08-31 方案批准）。各阶段标准档每段字数 +
# 阶段分段提示（帮模型找自然分界）。密度档（dense/standard/sparse）按系数缩放。
_INTERVAL_STANDARD = {
    "synopsis": 1500,   # 总纲：分卷纲要自然边界优先
    "volume": 1500,     # 分卷：章节自然边界优先（一章上千字，不再整章一大块）
    "outline": 800,     # 大纲：每约 2 个序号大单元一组（单章也能插 2~3 次）
    "detail": 600,      # 细纲：每约 2 个细纲块一组（十几块 → 约 5~6 次穿插）
    "prose": 1000,      # 正文：按自然段落切段（整章不分块也能穿插）
}
_INTERVAL_HINT = {
    "synopsis": "分段以分卷纲要自然边界优先",
    "volume": "分段以章节自然边界优先",
    "outline": "每约 2 个序号大单元一组",
    "detail": "每约 2 个细纲块一组",
    "prose": "按正文自然段落切段",
}
_DENSITY_FACTOR = {
    "dense": 0.6,
    "standard": 1.0,
    "sparse": 1.5,
}


def _draft_density(store) -> str:
    """穿插密度档：dense/standard/sparse，非法值按 standard。"""
    try:
        d = (store.get_setting("draft_check_density") or "standard").strip().lower()
    except Exception:
        return "standard"
    return d if d in _DENSITY_FACTOR else "standard"


def _draft_interval(stage: str, store) -> int:
    """当前密度档下该阶段的每段字数（取整到百位，下限 200）。"""
    base = _INTERVAL_STANDARD.get(stage, 1000)
    factor = _DENSITY_FACTOR[_draft_density(store)]
    return max(200, int(round(base * factor / 100.0)) * 100)


def build_draft_text(stage: str, store, context: dict, char_names: list) -> str:
    """组装草稿自检协议模块全文；阶段未开启 → 返回 ""（调用方跳过注入）。

    全阶段支持两种草稿模式（设置键 draft_check_prose_mode，键名历史遗留）：
    - full（默认）= 全量草稿：一个 <draft> 块装全部内容初稿+自检+修改指导，闭合后再输出精修后的正式内容；
    - interleaved = 穿插草稿：字数驱动分段——完整内容分成若干段（每段约 N 字，
      N 按阶段默认值 × 密度档系数），每段先 <draft>该段初稿+自检+修改指导</draft>、
      紧接该段精修后的正式内容，逐段交替。正文整章不分块也能穿插（不再依赖细纲块）。
    两种模式的草稿都是「三步」：正文初稿 → 逐项自检 → 修改指导，正式输出 = 按自检修正的终稿。
    split_draft 对多段成对剥离已天然支持穿插格式。
    """
    if not draft_enabled_for(stage, store):
        return ""
    # 自检清单：用户自定义（draft_checklist_<stage>，替换式）优先，留空回退内置默认
    checklist = _user_checklist(stage, store) or DRAFT_CHECKLISTS.get(stage) or []
    # 任务硬性要求（必含要素/章范围/涉及人物）已在任务指令中明确，不在此重复（老板 2026-09-01）
    extra = _extra_items(stage, store)
    label = _STAGE_LABELS.get(stage, stage)

    # 穿插模式判定：设置开启即穿插（字数驱动对任何长文本适用，无分块前提）
    interleaved = _draft_mode(store) == "interleaved"

    if interleaved:
        interval = _draft_interval(stage, store)
        hint = _INTERVAL_HINT.get(stage, "按内容自然分段")
        lines = [
            "【草稿自检协议（输出格式硬性要求，穿插草稿模式）】",
            f"本次{label}按「分段自检」输出：把完整内容分成若干段，每段约 {interval} 字，逐段交替：",
            "第一段：<draft>本段草稿与自检</draft> → 本段正式内容（精修后）",
            "第二段：<draft>本段草稿与自检</draft> → 本段正式内容（精修后）",
            f"……（{hint}；不得把所有草稿集中堆到开头，每段草稿后必须紧跟该段正式内容）",
            "- <draft> 块必须出现在最终回复的正文文本中；写在思考过程/思考面板里无效"
            "（思考内容用户看不到，思考中出现草稿=未执行本协议）。",
            "- 若任务指令要求「先思考输出、再正式输出」等分段结构，思考段可放在最前，"
            "但每段 <draft> 自检草稿仍必须排在对应正式内容之前。",
            "漏掉 <draft> 标签即视为未遵守本协议。",
            ("每段草稿（<draft> 开头、</draft> 结尾）分三步：\n"
             "第一步【正文初稿】：写出本段完整正文的初稿（覆盖全部内容点与要求，语句未经打磨；"
             "篇幅可约为正式版七成）。\n"
             "第二步【逐项自检】：对照下方全部清单（自检清单、附加检查项）"
             "逐项核对初稿，每项一行「✓ 通过」或「✗ 问题：指出具体违反处」。\n"
             "第三步【修改指导】：对每个 ✗ 项用一两句说明正式版如何修改；全部通过则写「无需修改」。\n"
             "三步齐全后闭合 </draft>，紧接输出本段正式内容：即按自检与修改指导精修后的终稿，"
             "不得与初稿逐字相同。"),
            "草稿是初稿、正式是修改后的终稿，两者应有可见差异——这是本模式的意义。",
            "",
            "【自检清单（逐段核对，一项不落）】",
        ]
    else:
        if stage == "prose" and context.get("block"):
            final_seg = ("末段（正式内容）：紧接 </draft> 之后直接输出本块正文"
                         "（本次只生成本块，即精修后的终稿）。")
        else:
            final_seg = ("末段（正式内容）：紧接 </draft> 之后一次性输出完整正式内容"
                         "（即精修后的终稿），不要重复标签、不要任何说明或标记。")
        lines = [
            "【草稿自检协议（输出格式硬性要求）】",
            f"本次{label}的最终回复文本必须以 <draft> 作为开头：先输出草稿与自检，闭合 </draft> 后再输出精修后的正式内容。",
            "- <draft> 块必须出现在最终回复的正文文本中；写在思考过程/思考面板里无效"
            "（思考内容用户看不到，思考中出现草稿=未执行本协议）。",
            "- 若任务指令要求「先思考输出、再正式输出」等分段结构，一律把 <draft> 自检草稿"
            "排在最终回复文本的最前面，思考段与正式内容依次接在其后。",
            "漏掉 <draft> 标签即视为未遵守本协议。",
            ("草稿（以 <draft> 开头、</draft> 结尾）分三步：\n"
             "第一步【正文初稿】：写出本次完整内容的初稿（覆盖全部内容点与要求，语句未经打磨；"
             "篇幅可约为正式版七成）。\n"
             "第二步【逐项自检】：对照下方全部清单（自检清单、附加检查项）"
             "逐项核对初稿，每项一行「✓ 通过」或「✗ 问题：指出具体违反处」。\n"
             "第三步【修改指导】：对每个 ✗ 项用一两句说明正式版如何修改；全部通过则写「无需修改」。\n"
             "三步齐全后闭合 </draft>；正式内容即精修后的终稿，不得与初稿逐字相同。"),
            final_seg,
            "草稿是初稿、正式是修改后的终稿，两者应有可见差异——这是本模式的意义。",
            "",
            "【自检清单（逐项核对，一项不落）】",
        ]
    lines += [f"- {item}" for item in checklist]
    if extra:
        lines.append("")
        lines.append("【附加检查项（用户自定义，逐项自检）】")
        lines += [f"- {item}" for item in extra]
    return "\n".join(lines)
