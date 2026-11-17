# -*- coding: utf-8 -*-
"""手机版设计系统：间距 / 圆角 / 语义色 / 组件工厂。

改这个文件等于改全站观感，动之前先读这三条原则：

1. **只用 Material 3 语义色**（`SURFACE_CONTAINER_*` / `ON_SURFACE_VARIANT` /
   `OUTLINE_VARIANT` …），明暗主题自动适配。**禁止硬编码 #RRGGBB**——
   夜间模式下会变成白底白字（本项目踩过：桌面 `theme.COLORS` 恒为日间值）。
2. **间距一律走 8pt 栅格常量**（GAP_XS…GAP_XL），页面里不写魔法数字，
   改全站疏密只改一处。
3. **一个页面只允许一个主操作**。其余降成 tonal / ghost / 收进底部弹层——
   手机屏幕就那么宽，按钮一多就都找不到了（老板 2026-11-16 点名痛点）。

组件工厂都返回普通 Container/Row，不用自定义类：Flet 里自定义控件会多一层
`build()` 调用，重建页面时容易拿到旧引用。
"""
import flet as ft

# ---------------- 间距（8pt 栅格） ----------------
GAP_XS = 4
GAP_S = 8
GAP_M = 12
GAP_L = 16
GAP_XL = 24

# ---------------- 圆角 ----------------
R_SM = 8
R_MD = 12
R_LG = 14
R_XL = 18
R_PILL = 999

# ---------------- 语义色 ----------------
# 名字说明用途，不说明色值：这样换主题不用改调用方。
#
# 层次约定（2026-11-16 第三轮，按「纸」的直觉重排）：
#   页面底 SURFACE_CONTAINER_LOW（浅主色灰）→ 卡片 SURFACE_CONTAINER_LOWEST（纯白）
#   卡片内嵌块 SURFACE_CONTAINER_HIGH（再深一档）
#
# 上一轮把卡片放 SURFACE_CONTAINER、页面放 SURFACE，浅色下两者只差一点点亮度，
# 卡片看着是「压进页面里的灰块」而不是「浮在页面上的纸」——平板上满屏卡片时
# 整页糊成一片灰紫（老板 2026-11-16 第二轮反馈「还是丑」的直接原因）。
# 现在反过来：底色带灰、卡片纯白，靠 1px 描边收边，不用阴影也能分层。


def bg():
    """页面底色（浅灰，卡片才浮得起来）。"""
    return ft.Colors.SURFACE_CONTAINER_LOW


def card_bg():
    """卡片底面（纯白）。"""
    return ft.Colors.SURFACE_CONTAINER_LOWEST


def paper_bg():
    """正文编辑器底面：与卡片同色，靠描边和内边距区分「可写的纸」。"""
    return ft.Colors.SURFACE_CONTAINER_LOWEST


def inset_bg():
    """卡片内嵌块 / 只读文本区（再深一档）。"""
    return ft.Colors.SURFACE_CONTAINER_HIGH


def line():
    """细分隔线 / 卡片描边。"""
    return ft.Colors.with_opacity(0.5, ft.Colors.OUTLINE_VARIANT)


def line_strong():
    return ft.Colors.OUTLINE_VARIANT


def text():
    return ft.Colors.ON_SURFACE


def muted():
    return ft.Colors.ON_SURFACE_VARIANT


def brand():
    """主色（导航栏、主按钮、进度条）。"""
    return ft.Colors.PRIMARY


def success():
    return ft.Colors.GREEN


def warn():
    return ft.Colors.ORANGE


def danger():
    return ft.Colors.ERROR


def soft(color, opacity=0.12):
    """半透明底（徽章、提示条）。"""
    return ft.Colors.with_opacity(opacity, color)


# ---------------- 文本层级 ----------------

def title(text: str, size: int = 20) -> ft.Text:
    return ft.Text(text, size=size, weight=ft.FontWeight.BOLD, color=None)


def subtitle(text: str) -> ft.Text:
    return ft.Text(text, size=12, color=muted())


def mini_label(text: str) -> ft.Text:
    """控件上方的一行小灰字标签（下拉框专用：Flet 的 Dropdown 浮动 label
    在本机会压在描边里被切掉半截，只能自己画一行字）。"""
    return ft.Text(text, size=11, color=muted())


def label(text: str) -> ft.Text:
    return ft.Text(text, size=12, weight=ft.FontWeight.W_600)


def body(text: str, size: int = 13) -> ft.Text:
    return ft.Text(text, size=size)


# ---------------- 容器 ----------------

def card(content, *, padding=GAP_M, on_click=None, tone: str = "low",
         margin=None, radius=R_LG, border: bool = True, expand: bool = False):
    """统一卡片：细边框 + 无阴影 + 大圆角。

    tone: "low"(白) / "inset"(灰) / "brand"(主色淡底，用于强调块)。
    """
    if tone == "inset":
        bgcolor = inset_bg()
    elif tone == "brand":
        bgcolor = soft(brand(), 0.07)
    else:
        bgcolor = card_bg()
    return ft.Container(
        content,
        padding=padding,
        bgcolor=bgcolor,
        border=ft.Border.all(1, line()) if border else None,
        border_radius=radius,
        margin=margin or ft.Margin(left=0, right=0, top=0, bottom=GAP_S),
        on_click=on_click,
        ink=bool(on_click),
        expand=expand,
    )


def section(text: str, trailing=None) -> ft.Control:
    """分组标题：竖条 + 粗体，右侧可挂一个「查看全部」之类的小动作。"""
    left = ft.Row([
        ft.Container(width=3, height=14, bgcolor=brand(), border_radius=2),
        ft.Text(text, size=13, weight=ft.FontWeight.W_700),
    ], spacing=GAP_S, expand=True)
    return ft.Container(
        ft.Row([left] + ([trailing] if trailing else [])),
        margin=ft.Margin(left=2, right=2, top=GAP_M, bottom=GAP_XS),
    )


def pill(text: str, color=None, *, size=10, opacity=0.14) -> ft.Container:
    """状态徽章。color 缺省=主色。"""
    color = color or brand()
    return ft.Container(
        ft.Text(text, size=size, color=color),
        bgcolor=ft.Colors.with_opacity(opacity, color),
        border_radius=R_PILL,
        padding=ft.Padding(left=8, right=8, top=2, bottom=2),
    )


def chip(label_text: str, *, active=False, on_click=None) -> ft.Container:
    """可点小胶囊（筛选/切换用）。"""
    return ft.Container(
        ft.Text(label_text, size=12,
                weight=ft.FontWeight.W_600 if active else ft.FontWeight.NORMAL,
                color=ft.Colors.ON_PRIMARY_CONTAINER if active else text()),
        bgcolor=ft.Colors.PRIMARY_CONTAINER if active else None,
        border=ft.Border.all(1, brand() if active else line_strong()),
        border_radius=R_PILL,
        padding=ft.Padding(left=14, right=14, top=7, bottom=7),
        on_click=on_click,
        ink=bool(on_click),
    )


def inset(content, *, padding=GAP_S, radius=R_SM):
    """卡片内的浅色内嵌块（大纲摘要、只读预览）。"""
    return ft.Container(content, bgcolor=inset_bg(), border_radius=radius,
                        padding=padding)


def divider(top=GAP_S, bottom=GAP_S):
    return ft.Container(height=1, bgcolor=line(),
                        margin=ft.Margin(left=0, right=0, top=top, bottom=bottom))


def hint_text(text: str, *, size=11) -> ft.Container:
    """辅助说明（灰字小号，不抢焦点）。"""
    return ft.Container(
        ft.Text(text, size=size, color=muted()),
        margin=ft.Margin(left=2, right=2, top=2, bottom=2),
    )


# ---------------- 按钮 ----------------

def _btn_kwargs(expand, disabled, tooltip) -> dict:
    """按钮的公共关键字参数。

    ⚠ **expand 只在为 True 时才写进参数**：Flet 会把 `expand=False` 也下发给
    Flutter，按钮因此被包一层 Flexible；一旦这行按钮放在 `Row(wrap=True)`
    里（Wrap 不接受 Flexible），整块渲染失败、手机上表现为一大块灰
    （2026-11-16 在偏好卡片页/模型页实测定位）。
    """
    kw = {}
    if expand:
        kw["expand"] = True
    if disabled:
        kw["disabled"] = True
    if tooltip:
        kw["tooltip"] = tooltip
    return kw


def primary(text: str, on_click=None, icon=None, *, expand=False,
            disabled=False, tooltip=None) -> ft.FilledButton:
    return ft.FilledButton(
        text, icon=icon, on_click=on_click,
        style=ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=R_MD),
            padding=ft.Padding(left=18, right=18, top=14, bottom=14),
        ), **_btn_kwargs(expand, disabled, tooltip))


def tonal(text: str, on_click=None, icon=None, *, expand=False,
          disabled=False, tooltip=None) -> ft.FilledTonalButton:
    return ft.FilledTonalButton(
        text, icon=icon, on_click=on_click,
        style=ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=R_MD),
            padding=ft.Padding(left=16, right=16, top=14, bottom=14),
        ), **_btn_kwargs(expand, disabled, tooltip))


def ghost(text: str, on_click=None, icon=None, *, expand=False,
          disabled=False, tooltip=None) -> ft.TextButton:
    return ft.TextButton(
        text, icon=icon, on_click=on_click,
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=R_MD)),
        **_btn_kwargs(expand, disabled, tooltip))


def button_row(*buttons, fill=True, align=None) -> ft.Row:
    """按钮行：**不用 wrap**（见 _btn_kwargs 的说明），靠 expand 平分宽度。

    fill=True：每个按钮等分整行宽度，手指好点；
    fill=False：按钮按内容宽度靠对齐边排布。
    """
    items = []
    for i, b in enumerate(buttons):
        if b is None:
            continue
        if i:
            items.append(ft.Container(width=GAP_S))
        if fill:
            b.expand = True
        items.append(b)
    return ft.Row(items, spacing=0, alignment=align,
                  vertical_alignment=ft.CrossAxisAlignment.CENTER)


# ---------------- 输入 ----------------

def field(label_text: str = "", value: str = "", *, hint="", multiline=False,
          min_lines: int | None = None, max_lines: int | None = None,
          read_only=False, on_change=None, size=13, expand=False,
          dense=False, autofocus=False, helper="",
          keyboard_type=None) -> ft.TextField:
    """统一输入框：M3 填充式，聚焦描边，不写死边框色。

    旧代码用 `border_color=TRANSPARENT` 制造「无框感」，副作用是**聚焦时也
    看不出来**（手机上手写正文不知道光标在哪）。这里统一用 filled。
    """
    kw = dict(
        label=label_text or None,
        label_style=ft.TextStyle(size=11, color=muted()),
        value=value,
        hint_text=hint or None,
        helper=helper or None,
        helper_style=ft.TextStyle(size=10, color=muted()),
        multiline=multiline,
        read_only=read_only,
        text_size=size,
        filled=True,
        fill_color=inset_bg(),
        border=ft.InputBorder.OUTLINE,
        border_radius=R_MD,
        border_color=line(),
        focused_border_color=brand(),
        content_padding=ft.Padding(left=12, right=12, top=10, bottom=10),
        on_change=on_change,
        expand=expand,
        dense=dense,
        autofocus=autofocus,
        keyboard_type=keyboard_type,
    )
    if multiline:
        kw["min_lines"] = min_lines if min_lines is not None else 4
        kw["max_lines"] = max_lines
    return ft.TextField(**kw)


def field_group(control, label_text: str = "", helper: str = "") -> ft.Column:
    """「上面一行小标签 + 控件 + 下面一行灰字说明」的三段式。

    为什么不直接给 TextField 传 `label`：那是浮在描边上的 floating label，
    11px 中文压在 1px 描边上本就糊，设置页里它还会和下方的 helper 说明一起
    摞成三层字（章节标题 / 浮标签 / 说明），读起来分不清哪行是标题。
    改成「标签在上、说明在下」两条同字号灰字，与下拉框（T.select）排法一致。

    控件引用由调用方自己留着读写 value（本函数只负责套壳）。
    """
    parts = []
    if label_text:
        parts.append(ft.Text(label_text, size=11, color=muted()))
    parts.append(control)
    if helper:
        parts.append(ft.Text(helper, size=11, color=muted()))
    # 顶部留一点外边距：条目之间（label→框→说明→下一条 label）全是同号灰字，
    # 不留空隙就糊成一片，看不出哪行说明属于哪个框。
    return ft.Column(parts, spacing=GAP_XS, tight=True,
                     margin=ft.Margin(left=0, right=0, top=GAP_XS, bottom=0),
                     horizontal_alignment=ft.CrossAxisAlignment.STRETCH)


def switch_row(label_text: str, value: bool, on_change=None, *, desc="") -> tuple:
    """开关行：**文字在左、开关在右**（放 .value 给调用方读）。

    不用 `ft.Switch(label=...)`：那会把文字画在开关右边，整行左重右轻，
    和「渠道故障切换」卡（图标+文字+右侧开关）的排法相反。
    """
    sw = ft.Switch(value=bool(value), active_color=brand(), on_change=on_change)
    head = ft.Row([ft.Text(label_text, size=13, expand=True), sw],
                  spacing=GAP_S, vertical_alignment=ft.CrossAxisAlignment.CENTER)
    parts = [head]
    if desc:
        parts.append(ft.Text(desc, size=11, color=muted()))
    # 同 field_group：顶部留白，否则「上一行说明」和「下一行标题」挤成一片。
    return ft.Column(parts, spacing=GAP_XS, tight=True,
                     margin=ft.Margin(left=0, right=0, top=GAP_XS, bottom=0),
                     horizontal_alignment=ft.CrossAxisAlignment.STRETCH), sw


def editor(value: str = "", *, hint="", expand=True, min_lines=10) -> ft.TextField:
    """正文编辑器：字大一点、行高松一点，长期盯着看不累。

    加一圈极淡描边——纯靠填充色分层时，正文区和卡片背景在暗色主题下几乎
    贴在一起，看不出「这里是可写的纸」。
    """
    return ft.TextField(
        value=value,
        hint_text=hint or None,
        multiline=True,
        min_lines=min_lines if not expand else None,
        max_lines=None,
        text_size=15,
        filled=True,
        fill_color=paper_bg(),
        border=ft.InputBorder.OUTLINE,
        border_color=line(),
        focused_border_color=brand(),
        border_radius=R_LG,
        content_padding=ft.Padding(left=14, right=14, top=12, bottom=12),
        expand=expand,
    )


def select(label_text: str = "", value=None, options=None, *, on_select=None,
           width=None, size=12, expand=False):
    """统一下拉：小标题 + 淡描边下拉框。

    不用 Flet 自带的 `label`：filled/outlined 风格下 label 是浮在描边上的，
    本机渲染会把字压在边框里切掉半截（截图验证）。改成「一行小灰字 + 控件」
    两段式，任何主题下都清楚。
    """
    dd = ft.Dropdown(
        value=value,
        # 撑满父级只能靠 Dropdown 自己的 expand（Column 的 STRETCH 对 Dropdown
        # 的宽度不起作用，探针验证过：去掉之后缩成「字宽 + 箭头」一小截）。
        # 代价是 expand 会让它比旁边「定宽 + 无 expand」的下拉略高，所以下面
        # 统一钉死 height，两种下拉并排时上下边才齐平。
        width=width,
        height=46,
        expand=True if width is None else False,
        dense=True,
        text_size=size,
        filled=True,
        fill_color=card_bg(),
        border=ft.InputBorder.OUTLINE,
        border_color=line(),
        focused_border_color=brand(),
        border_width=1,
        border_radius=R_MD,
        content_padding=ft.Padding(left=10, right=6, top=8, bottom=8),
        options=[ft.dropdown.Option(k, v) for k, v in (options or [])],
        on_select=on_select,
    )
    if not label_text:
        return dd
    col = field_group(dd, label_text)
    # expand / width 要落在外面这层 Column 上：写作台把两个下拉并排塞进 Row，
    # 靠 expand 让左侧那个吃掉剩余宽度（只给 Dropdown 传没用，它在 Column 里）。
    # ⚠ 只在为真时才写：Flet 会把 expand=False 一并下发（见 _btn_kwargs）。
    if expand:
        col.expand = True
    if width:
        col.width = width
    return col


# ---------------- 响应式（手机 / 平板） ----------------

# 写作台两栏的断点：低于它按手机单栏排（正文纸占满高度），高于它把
# 章节信息/批次列表放左栏、正文纸放右栏。
# 取 920 而不是 760：两栏要塞下「左栏 380 + 间隙 16 + 正文纸最低 480 + 边距」，
# 760 时正文纸只剩 380 宽（每行 17 字，比手机还窄，两栏反而更难看）。
# 800dp 的竖屏平板走单栏满宽；横屏 1024/1180 才进两栏。
WIDE_BREAKPOINT = 920


def view_width(page, default: int = 412) -> int:
    """当前视口逻辑宽（取不到时按手机 412 兜底）。"""
    try:
        w = int(getattr(page, "width", 0) or 0)
    except (TypeError, ValueError):
        w = 0
    return w or default


def view_height(page, default: int = 880) -> int:
    try:
        h = int(getattr(page, "height", 0) or 0)
    except (TypeError, ValueError):
        h = 0
    return h or default


def is_wide(page, breakpoint: int = WIDE_BREAKPOINT) -> bool:
    """是否按两栏（平板）排。"""
    return view_width(page) >= breakpoint


def editor_lines(page, reserved_px: int, *, min_lines: int = 8,
                 max_lines: int = 46, line_px: int = 28) -> int:
    """按视口换算编辑器行数（TextField 的高度只认 min_lines，见模块文档坑 6）。

    reserved_px = 顶栏/章节卡/操作栏等固定占位；line_px = 16px 字号 × 1.75 行距。
    """
    n = (usable_height(page) - reserved_px) // line_px
    return max(min_lines, min(max_lines, n))


# ---------------- 整屏骨架 / 系统栏安全区 ----------------
# 底部导航高度：Material 3 标准 80。**别往下调** —— 68 时图标+文字正好贴到
# 屏幕最下沿，系统导航栏一压，整排「下半截」就看不见了（老板 2026-11-17 平板反馈）。
APP_NAV_HEIGHT = 80


def _inset_px(value) -> int:
    try:
        return max(0, int(round(float(value or 0))))
    except (TypeError, ValueError):
        return 0


def system_insets(page) -> tuple:
    """(上, 下) 系统栏占位：状态栏 / 系统导航栏（三键导航或手势条）。

    安卓 15 起强制 edge-to-edge，页面内容会铺到状态栏和系统导航栏底下，
    不主动内缩就是「最下面一排图标的下半部分缺失」。平板上系统栏更宽更明显。
    `padding` 与 `view_padding` 取**两者较大的**：弹键盘 / 系统栏自动隐藏时
    `padding` 会变小甚至变 0，而 `SafeArea(maintain_bottom_view_padding=True)`
    常驻的是 `view_padding`；哪个上报了值就以哪个为准，纸的高度才不会跟着
    键盘一开一合跳。
    """
    media = getattr(page, "media", None)
    pads = [p for p in (getattr(media, "view_padding", None),
                        getattr(media, "padding", None)) if p is not None]
    top = max([_inset_px(getattr(p, "top", 0)) for p in pads] or [0])
    bottom = max([_inset_px(getattr(p, "bottom", 0)) for p in pads] or [0])
    return top, bottom


def usable_height(page, default: int = 880) -> int:
    """真正能放内容的视口高度 = 整屏高 − 系统栏占位（含底部导航上下的那两截）。

    `page.height` 是**整屏**逻辑高（含系统栏），按它换算行数会多算，
    纸就长出可用区域、末尾半截行被裁。
    """
    top, bottom = system_insets(page)
    return max(240, view_height(page, default) - top - bottom)


def app_shell(body, nav) -> ft.Control:
    """整屏骨架：内容区（避让状态栏/侧边系统栏）+ 贴底导航。

    两处 SafeArea 分工不同，别合成一个：
    - 内容区只避让**上/左右**，底部让给导航（否则导航上面多出一条页面底色的缝）；
    - 导航外框自己再套一层：导航的**背景色一直铺到屏幕最下沿**，只有图标和
      文字被抬到系统栏之上（Material 导航栏的常规观感）。
    `maintain_bottom_view_padding=True`：弹键盘时 `padding.bottom` 会被吃掉，
    这个开关让底部那一截保持常驻，导航不会因为键盘一开一合来回跳。
    """
    nav_block = ft.Container(
        ft.SafeArea(content=nav, avoid_intrusions_top=False,
                    maintain_bottom_view_padding=True),
        bgcolor=card_bg(),
    )
    return ft.Column([
        ft.SafeArea(content=body, expand=True, avoid_intrusions_bottom=False),
        nav_block,
    ], expand=True, spacing=0)


# ---------------- 空态 ----------------

def empty(text: str, icon=None, action=None) -> ft.Container:
    items = [
        ft.Icon(icon or ft.Icons.AUTO_STORIES, size=40,
                color=ft.Colors.with_opacity(0.3, ft.Colors.ON_SURFACE)),
        ft.Text(text, color=muted(), size=13, text_align=ft.TextAlign.CENTER),
    ]
    if action:
        items.append(action)
    return ft.Container(
        ft.Column(items, horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                  spacing=GAP_M),
        alignment=ft.Alignment.CENTER,
        padding=GAP_XL + 12,
    )


# ---------------- 页头 ----------------

def page_head(title_text: str, subtitle_text: str = "", trailing=None) -> ft.Control:
    """统一页头：大标题 + 可选副标题 + 右侧动作区。"""
    col = ft.Column(
        [ft.Text(title_text, size=22, weight=ft.FontWeight.BOLD)] +
        ([ft.Text(subtitle_text, size=12, color=muted())] if subtitle_text else []),
        spacing=2, expand=True)
    return ft.Container(ft.Row([col] + ([trailing] if trailing else [])),
                        margin=ft.Margin(left=2, right=2, top=GAP_XS, bottom=GAP_M))


def back_head(title_text: str, on_back, trailing=None) -> ft.Control:
    """二级页头：返回箭头 + 标题。"""
    return ft.Container(
        ft.Row([
            ft.IconButton(ft.Icons.ARROW_BACK, on_click=lambda e: on_back(),
                          icon_size=20),
            ft.Text(title_text, size=20, weight=ft.FontWeight.BOLD, expand=True),
        ] + ([trailing] if trailing else []), spacing=0),
        margin=ft.Margin(left=0, right=2, top=0, bottom=GAP_S))


# ---------------- 底部弹层 ----------------

def sheet(page, sheet_title: str, controls: list, *, height=None,
          actions=None, subtitle_text: str = "") -> None:
    """统一底部弹层：标题行 + 内容（可滚） + 底部动作行。

    全站的 sheet 都该走这里：尺寸、圆角、拖动条、安全区一次对齐，
    页面里各写一份会出现「同样的弹层高矮不一」。
    """
    header = [ft.Text(sheet_title, size=16, weight=ft.FontWeight.BOLD)]
    if subtitle_text:
        header.append(ft.Text(subtitle_text, size=11, color=muted()))
    body = ft.Column(
        [ft.Column(header, spacing=2, tight=True), *controls] +
        ([divider(), actions] if actions else []),
        spacing=GAP_M, scroll=ft.ScrollMode.AUTO, tight=True)
    content = ft.Container(body, padding=ft.Padding(left=16, right=16,
                                                    top=GAP_S, bottom=GAP_L))
    if height:
        content.height = height
    page.show_dialog(ft.BottomSheet(
        content=content,
        show_drag_handle=True,
        use_safe_area=True,
        bgcolor=card_bg(),
    ))


def action_row(page, primary_btn=None, cancel_text="取消", close=True):
    """弹层底部动作行：右对齐，主操作在最右。"""

    def _close(e):
        page.pop_dialog()

    items = [ft.Container(expand=True)]
    if cancel_text:
        items.append(ghost(cancel_text, on_click=_close))
    if primary_btn is not None:
        items.append(primary_btn)
    return ft.Row(items, spacing=GAP_S)


def nav_tile(icon, title_text: str, subtitle_text: str = "", on_click=None,
             *, trailing=None, color=None, dense=False) -> ft.Control:
    """列表导航条目：圆角图标底座 + 标题 + 一行说明 + 右箭头。

    比 `ft.ListTile` 好在：图标有底色（列表里不再是一排裸线条图标）、
    行高按内容自适应、说明文字不会被挤成两行截断。
    """
    c = color or brand()
    row = [
        ft.Container(ft.Icon(icon, size=19, color=c),
                     width=34, height=34, border_radius=R_MD,
                     bgcolor=soft(c, 0.10),
                     alignment=ft.Alignment.CENTER),
        ft.Column(
            [ft.Text(title_text, size=14, weight=ft.FontWeight.W_500)] +
            ([ft.Text(subtitle_text, size=11, color=muted(),
                      max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)]
             if subtitle_text else []),
            spacing=1, expand=True, tight=True),
        trailing or ft.Icon(ft.Icons.CHEVRON_RIGHT, size=17,
                            color=ft.Colors.with_opacity(0.45, ft.Colors.ON_SURFACE)),
    ]
    return ft.Container(
        ft.Row(row, spacing=GAP_M, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        padding=ft.Padding(left=GAP_M, right=GAP_M,
                           top=GAP_S if dense else 10,
                           bottom=GAP_S if dense else 10),
        on_click=on_click,
        ink=bool(on_click),
    )


def menu_row(icon, title_text: str, subtitle_text: str = "", on_click=None,
             color=None) -> ft.Control:
    """弹层/列表里的一行菜单项：图标 + 标题 + 说明。整行可点。

    ⚠ 图标底座必须**同时给 width 和 height**：只给 width 再配 alignment 时，
    Flutter 会让带 alignment 的 Container 去填满父级约束，父级高度又由子项
    决定 → 撑出无限高，真机上整块渲染成灰（模型页路由列表踩过）。
    """
    col = [ft.Text(title_text, size=14,
                   color=color or text())]
    if subtitle_text:
        col.append(ft.Text(subtitle_text, size=11, color=muted()))
    # 图标底座与 nav_tile 一致（淡色底 + 同色图标）：只画裸线条图标时，
    # 一列排下来看着比旁边有底座的列表「重」一档（模型页路由列表截图）。
    c = color or brand()
    return ft.Container(
        ft.Row([
            ft.Container(ft.Icon(icon, size=19, color=c),
                         width=34, height=34, border_radius=R_MD,
                         bgcolor=soft(c, 0.10),
                         alignment=ft.Alignment.CENTER),
            ft.Column(col, spacing=2, expand=True),
            ft.Icon(ft.Icons.CHEVRON_RIGHT, size=16,
                    color=ft.Colors.with_opacity(0.45, ft.Colors.ON_SURFACE)),
        ], spacing=GAP_M, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        padding=ft.Padding(left=GAP_S, right=GAP_M, top=10, bottom=10),
        on_click=on_click,
        ink=bool(on_click),
        border_radius=R_SM,
    )
