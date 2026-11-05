# -*- coding: utf-8 -*-
"""去 AI 味·可读性护栏（de-AI taste guard，2026-11-05）。

来源：Clark / Martin / Mahowald, *Why is AI-generated language so hard to
understand?*, arXiv:2610.29162v1（2026-10-21）。论文用心理语言学量化了「AI 味」：
后训练为数学/代码/推理优化后，模型形成「信息密度最大化」偏好——把信息压进更少
token，牺牲人类阅读理解；人类语言遵循 UID（均匀信息密度），会主动插入可选词、
把信息摊匀、缩短句法依存以降低读者瞬时负担。八类特征（生造搭配、长依存、省略
关系词的宾语从句、名词堆叠、名词动用、无生命主语配有灵动词、高 perplexity、
整体高密度）语法合法，但对人类语言是 out-of-distribution。

本模块是【生成期事前预防】：在全部创作环节注入一段全局可编辑护栏，只约束
「成品文字的信息压缩 / 瞬时阅读负担」，不改事实、文风与情节。

⚠️ 分工（改本模块前先读 docs/去AI味模块总览.md 与 docs/改写模块总览.md）：
- 本模块 = 事前预防，注入 8 个创作 role，管「写的时候就别压太密」；
- style_rewrite / task_templates.STYLE_REWRITE_PREAMBLE（十四类病灶）/
  style_checker.CLICHES = 事后改写与审校，只作用于 prose_rewrite / style。
两者互补、不重复。本模块不 import 也不修改上述任何文件。

与三条既有协议的边界：
- 草稿自检（order 11.5）：文案显式豁免 <draft>；
- 思考纪律（order 12.5）：本段「以本段为准」仅限「信息压缩」一事，且显式豁免
  思考/推理环节与内部思维链，避免诱导模型在思考里反复查措辞（正撞「只推一遍」）；
- 协议栈（order 12.4）：**不互斥**——协议栈管格式执行/思考预算，本段管成品文字
  阅读负担，二者正交；且本护栏要求所有创作环节都注入，排除会成为缺口。
  互斥判断若以后要改，放在 assembler.build 里，不要塞进本模块。
"""

# 注入角色白名单：8 个创作系角色（与 think_guard.THINK_GUARD_CAPS 同源，
# test_deai.py 有相等断言防漂移）。未登记 = 不注入。
# check/style/memory 是机械 JSON 任务，ai_task（build_bare）无角色，注入只会挤预算。
DEAI_ROLES = frozenset({
    "synopsis", "volume", "volume_iter", "volume_chapters_iter",
    "outline", "detail", "prose", "prose_rewrite",
})

DEAI_MODULE_TITLE = "去 AI 味"
DEAI_KIND = "deai"
DEAI_ORDER = 12.55          # 思考纪律(12.5) 之后、输出完成标志(12.6) 之前

# 默认文案（双端共用同一段文字，用户在设置页可编辑）。
# 措辞四条红线（改动前先读）：
# ① 首段必须声明「不改内容要求/细节密度/文风/情节/偏好卡」——否则纪律外溢成「正文写干」；
# ② 必须显式豁免人物对白、刻意修辞、风格样本——拟人/名词动用/陌生化在小说里是正当修辞；
# ③ 必须显式豁免 <draft>、思考/推理、内部思维链——否则与草稿自检和思考纪律对撞；
# ④ 「以本段为准」只能限定「信息压缩」这一件事——写宽泛口径会与思考纪律互殴。
# 字数 600~1000：critical=True 永不裁剪，每次创作请求都注入，不许膨胀。
DEAI_DEFAULT_TEXT = (
    "【去 AI 味·可读性护栏（生成期预防；只管成品文字的信息密度，不改任务内容）】\n"
    "适用：所有创作阶段的最终输出（总纲/分卷/大纲/细纲/正文/改写）。\n"
    "本段不改变任务的内容要求、细节密度、文风、情节与偏好卡设定；只在「信息压缩」这一件事上，"
    "与任务模板/偏好卡冲突时以本段为准。\n"
    "本段只约束叙述与说明性文字给读者带来的瞬时阅读负担：人物对白、刻意修辞、风格样本明确要求的写法，"
    "不受本段约束。\n"
    "草稿自检的 <draft> 块、任务要求的思考/推理环节、内部思维链，都不是给读者看的成品，一律不受本段约束。\n\n"
    "一、把信息摊匀，别往一个句子里塞。一句话只解决一两件事；长句按语义拆成两三句，"
    "允许补上「的、因为、所以、就、才、也、其实」这类可选关系词，把因果和转折写明，"
    "别把几层因果压进一个短语或用「把」字句硬塞多层信息。宁可多一两个词，也别让读者回头重读。\n"
    "二、别生造搭配。避免生造词对、硬凑四字格、名词临时当动词（如「把这事日程一下」）；用现成说法。"
    "刻意的陌生化比喻照写，只在说明性文字里避免。\n"
    "三、拆开名词串。三个以上名词或概念连缀成「XX—XX—XX」时，拆成短句或加关系词分说，"
    "别让读者自己猜修饰范围。\n"
    "四、缩短修饰距离。主语和谓语、中心词和定语别隔太远；多层插入、长定语套嵌时，"
    "把其中一层挪到后一句另说，别让主谓被大段修饰语冲散。\n"
    "五、抽象概念别抢着当主语做拟人动作。「夜色吞没了巷口」「混乱要求他记住一切」这类写法，"
    "在叙述与说明里换成有人或物做主语的平实句子；风格样本要求的拟人照写不误。\n"
    "六、别省关系词、别用把字句硬塞多层信息。该带「的/因为/所以/虽然」就带上；省字加的是读者负担。"
    "中文无 that/which，但把从句压成名词短语的毛病同样要避免。\n"
    "七、控制整段密度。连续几句都是高信息量概念句时，插一句短句、一个具体动作或一处停顿，"
    "让信息密度起伏均匀；不追求字字带信息，允许适度重复与铺陈帮读者跟上。\n"
    "八、通顺优先于紧凑。写完后按「读者一口气读下来会不会卡」自检：卡住的地方拆句、补词、"
    "换常用搭配，别为精简把句子拧成翻译腔或概念串。"
)


def deai_enabled(store) -> bool:
    """协议开关（默认开）。关闭 = 不注入。

    与 think_guard_enabled 同构，兼容两种后端：
    - ProjectStore：get_setting_int 会按 GLOBAL_SETTING_KEYS 自动转发到全局库；
    - GlobalStore：直接读全局键。
    读不到/类型异常一律视为「开」——护栏默认在岗，宁可多注入一段。
    ⚠️ UI 写这个键只能写 "1"/"0"，绝不能写空串：get_global_setting 用 `if v:`
    判空，空串会悄悄回退默认 "1"（开），表现为「开关拨了没生效」。
    """
    try:
        return int(store.get_setting_int("deai_enabled", 1)) > 0
    except AttributeError:
        pass
    except (TypeError, ValueError):
        return True
    try:
        return int(store.get_global_setting("deai_enabled", "1")) > 0
    except Exception:
        return True


def deai_text(store, role) -> str:
    """按角色返回用户可编辑的护栏文案；非创作角色返回 ""（调用方跳过注入）。

    全局键 `deai_text` 为空/纯空白时回退 DEAI_DEFAULT_TEXT（「留空 = 用内置默认」，
    与 draft_checklist_* / style_cliches 同款惯例）。**刻意不把默认全文写进
    GLOBAL_SETTING_DEFAULTS**：否则用户打开设置什么都没改点保存，就会把当前默认
    固化进 app.db，以后升级内置文案老用户永远跟不上。UI 保存时会把「与内置默认
    相同」的文本归一化成空串（见 settings_dialog._save / 手机 settings._text）。
    """
    try:
        if role not in DEAI_ROLES:
            return ""
    except TypeError:
        return ""
    raw = ""
    for name in ("get_setting", "get_global_setting"):
        getter = getattr(store, name, None)
        if getter is None:
            continue
        try:
            raw = getter("deai_text", "")
            break
        except (AttributeError, TypeError, ValueError):
            continue
    if not raw or not str(raw).strip():
        return DEAI_DEFAULT_TEXT
    return str(raw)
