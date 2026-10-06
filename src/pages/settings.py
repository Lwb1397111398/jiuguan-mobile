# -*- coding: utf-8 -*-
"""设置页：全局工具设置（与桌面版 settings_dialog 一一对应）。

存储后端三种，与桌面完全一致：
- 全局（app.db meta）：绝大多数设置，跨项目共享；
- 项目级（project.db settings）：人物/实体沉睡章数、设定库每类上限，随作品走，
  未打开项目时禁用（禁用而非隐藏，让用户知道「有这么个设置、只是还没项目」）；
- 应用级（config.json）：自动备份三项（跟随本机，不随项目）。
"""
import flet as ft

from widgets import hint, toast
from bridge import run_bg

# 草稿自检阶段（键值与 core 的 draft_check_stages 一致）
STAGES = [("synopsis", "总纲"), ("volume", "分卷"), ("outline", "大纲"),
          ("detail", "细纲"), ("prose", "正文")]


def _clamp_int(text, lo, hi, default):
    """数字框容错：空/非数字回默认，越界夹到区间内（与桌面 setRange 同效）。"""
    try:
        v = int(float(str(text).strip()))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _clamp_float(text, lo, hi, default):
    try:
        v = float(str(text).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


class SettingsPage:
    def __init__(self, page, session, on_back=None, on_open_templates=None):
        self.page = page
        self.session = session
        self.on_back = on_back
        self.on_open_templates = on_open_templates
        self.on_refresh = None
        self._writers = []              # [(target, key, 取值函数)]
        self._widgets = {}              # key -> 控件（测试与单控件刷新用）
        self._embed_key = None          # 嵌入密钥框（build 时创建）
        self._embed_result = ft.Text("", size=12)   # 跨重建保留测试结果

    # ---------- 读 ----------

    def _g(self, key, default=""):
        try:
            return self.session.app.gstore.get_global_setting(key, default)
        except Exception:
            return default

    def _p(self, key, default=""):
        st = self.session.store
        if st is None:
            return default
        try:
            return st.get_setting(key, default)
        except Exception:
            return default

    def _a(self, key, default=""):
        try:
            return self.session.app.get_setting(key, default)
        except Exception:
            return default

    def _gi(self, key, default):
        try:
            return int(float(str(self._g(key, default) or default)))
        except (TypeError, ValueError):
            return default

    def _gf(self, key, default):
        try:
            return float(str(self._g(key, default) or default))
        except (TypeError, ValueError):
            return default

    def _pi(self, key, default):
        try:
            return int(float(str(self._p(key, default) or default)))
        except (TypeError, ValueError):
            return default

    # ---------- 控件工厂（构造即登记保存动作） ----------

    def _reg(self, target, key, fn, ctrl=None):
        self._writers.append((target, key, fn))
        if ctrl is not None:
            self._widgets[key] = ctrl

    def _sw(self, target, key, label, value, desc=""):
        sw = ft.Switch(label=label, value=bool(value))
        self._reg(target, key, lambda: "1" if sw.value else "0", sw)
        if not desc:
            return sw
        return ft.Column([sw, hint(desc)], spacing=0)

    def _num(self, target, key, label, value, lo, hi, desc="", is_float=False):
        tf = ft.TextField(label=label, value=str(value), dense=True,
                          keyboard_type=ft.KeyboardType.NUMBER)
        if is_float:
            self._reg(target, key,
                      lambda: str(_clamp_float(tf.value, lo, hi, value)), tf)
        else:
            self._reg(target, key,
                      lambda: str(_clamp_int(tf.value, lo, hi, value)), tf)
        if not desc:
            return tf
        return ft.Column([tf, hint(desc)], spacing=2)

    def _dd(self, target, key, label, value, options, desc=""):
        dd = ft.Dropdown(label=label, value=str(value), dense=True,
                         options=[ft.dropdown.Option(key=str(k), text=str(t))
                                  for k, t in options])
        self._reg(target, key, lambda: str(dd.value or value), dd)
        if not desc:
            return dd
        return ft.Column([dd, hint(desc)], spacing=2)

    def _text(self, target, key, label, value, desc="", multiline=False,
              lines=3, password=False, on_clear=False):
        tf = ft.TextField(
            label=label, value=str(value or ""), dense=not multiline,
            multiline=multiline, min_lines=lines if multiline else None,
            max_lines=lines if multiline else None,
            password=password, can_reveal_password=password,
            text_size=12 if multiline else None)
        self._reg(target, key, lambda: tf.value or "", tf)
        parts = [tf]
        if desc:
            parts.append(hint(desc))
        if on_clear:
            parts.append(ft.Row([
                ft.Text("留空 = 使用内置默认", size=11,
                        color=ft.Colors.ON_SURFACE_VARIANT),
                ft.TextButton("清空", on_click=lambda e: self._clear(tf)),
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN))
        return ft.Column(parts, spacing=2)

    def _clear(self, tf):
        tf.value = ""
        self.page.update()

    @staticmethod
    def _card(title, controls, desc=""):
        body = []
        if desc:
            body.append(hint(desc))
        body.extend(controls)
        return ft.Card(ft.Container(
            ft.Column([ft.Text(title, size=14, weight=ft.FontWeight.W_600)] + body,
                      spacing=8),
            padding=12))

    # ---------- 保存 ----------

    def _save(self, e=None):
        skipped = 0
        written = 0
        for target, key, fn in self._writers:
            if target == "p" and self.session.store is None:
                skipped += 1
                continue
            try:
                val = fn()
            except Exception:
                continue
            try:
                if target == "g":
                    self.session.app.gstore.set_global_setting(key, val)
                elif target == "p":
                    self.session.store.set_setting(key, val)
                else:
                    self.session.app.set_setting(key, val)
                written += 1
            except Exception as ex:
                toast(self.page, f"「{key}」保存失败：{ex}", error=True)
                return
        # 嵌入密钥密文存储：留空表示保留原值（与桌面一致）
        key_txt = (self._embed_key.value or "").strip() if self._embed_key else ""
        if key_txt:
            try:
                from app.core.api import encrypt_key
                self.session.app.gstore.set_global_setting(
                    "embed_api_key", encrypt_key(key_txt))
                written += 1
                self._embed_key.value = ""
            except Exception as ex:
                toast(self.page, f"嵌入密钥保存失败：{ex}", error=True)
                return
        msg = f"设置已保存（{written} 项）"
        if skipped:
            msg += f"；{skipped} 项需先打开项目"
        toast(self.page, msg)

    # ---------- 嵌入接口动作 ----------

    def _embed_key_value(self):
        """当前生效的嵌入密钥明文：输入框优先，否则解密库里的密文。"""
        txt = (self._embed_key.value or "").strip()
        if txt:
            return txt
        try:
            from app.core.api import decrypt_key
            return decrypt_key(self._g("embed_api_key", ""))
        except Exception:
            return ""

    def _fetch_embed(self, e):
        url = (self._embed_url.value or "").strip()
        if not url:
            toast(self.page, "请先填写嵌入接口地址", error=True)
            return
        key = self._embed_key_value()
        toast(self.page, "正在获取嵌入模型列表…")

        def job():
            from app.core.embedding import list_embedding_models
            return list_embedding_models(url, key)

        def done(r):
            if not isinstance(r, dict) or not r.get("ok"):
                toast(self.page, str((r or {}).get("message") or "获取失败"),
                      error=True)
                return
            models = r.get("models") or []
            if not models:
                toast(self.page, "服务方未返回任何模型", error=True)
                return
            self._pick_embed_model(models)
        run_bg(self.page, job, done)

    def _pick_embed_model(self, models):
        def _pick(item):
            self._embed_model.value = item
            self.page.pop_dialog()
            self.page.update()

        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column([
                ft.Text("选择嵌入模型", size=16, weight=ft.FontWeight.BOLD),
                ft.Column([
                    ft.ListTile(title=ft.Text(m.get("id", ""), size=13),
                                on_click=lambda ev, i=m.get("id", ""): _pick(i))
                    for m in models
                ], scroll=ft.ScrollMode.AUTO, expand=True),
            ], spacing=8), padding=16, height=420),
            show_drag_handle=True, use_safe_area=True))

    def _test_embed(self, e):
        url = (self._embed_url.value or "").strip()
        model = (self._embed_model.value or "").strip()
        key = self._embed_key_value()
        self._embed_result.value = "正在测试嵌入接口…"
        self._embed_result.color = ft.Colors.ON_SURFACE_VARIANT
        self.page.update()

        def job():
            from app.core.embedding import test_embedding
            return test_embedding(url, key, model)

        def done(r):
            r = r if isinstance(r, dict) else {}
            if r.get("ok"):
                self._embed_result.color = ft.Colors.GREEN_700
                self._embed_result.value = r.get("message") or "连接成功"
            else:
                self._embed_result.color = ft.Colors.RED_700
                self._embed_result.value = (r.get("error") or r.get("message")
                                            or "测试失败")
            try:
                self.page.update()
            except Exception:
                pass
        run_bg(self.page, job, done)

    # ---------- 构建 ----------

    def build(self) -> ft.Control:
        self._writers = []      # 每次重建重置登记表（避免重复保存）
        self._widgets = {}
        g = self._g

        # ---- 前文注入 ----
        inject = self._card("前文注入", [
            self._num("g", "context_inject_k", "最近 K 章", self._gi("context_inject_k", 1),
                      1, 3, "生成/改写时携带前文的范围"),
            self._dd("g", "context_inject_mode", "每章模式",
                     g("context_inject_mode", "tail"),
                     [("full", "完整全文"), ("tail", "章末截取"),
                      ("summary", "仅摘要")]),
            self._num("g", "context_inject_chars", "截取字数（章末截取时）",
                      self._gi("context_inject_chars", 3000), 500, 20000,
                      "每章最多带多少字"),
        ])

        # ---- 章节动态与记忆联动 ----
        has_proj = self.session.has_project
        memdesc = "" if has_proj else "（需先打开项目：这些设置随作品保存）"
        mem = self._card("章节动态与记忆联动", [
            self._sw("g", "chapter_dynamics", "本章动态",
                     g("chapter_dynamics", "1") == "1",
                     "每块确认后自动提取本章动态"),
            self._sw("g", "auto_lore_import", "归档后自动导入设定库",
                     g("auto_lore_import", "1") == "1",
                     "把记忆里的新人物/新实体同步进设定库（关掉则只提醒）"),
            self._num("p", "auto_seal_gap", "人物沉睡章数", self._pi("auto_seal_gap", 8),
                      0, 50, "连续 N 章未出场则不再注入（0=从不沉睡）" + memdesc),
            self._num("p", "auto_entity_gap", "实体沉睡章数", self._pi("auto_entity_gap", 3),
                      0, 50, "地点/物品/术语同理" + memdesc),
            self._num("p", "lore_per_category_cap", "设定每类上限",
                      self._pi("lore_per_category_cap", 40), 0, 500,
                      "每类最多注入条数（0=不限）" + memdesc),
        ])

        # ---- 估算与重试 ----
        est = self._card("估算与重试", [
            self._num("g", "token_ratio", "token 估算系数",
                      self._gf("token_ratio", 0.75), 0.3, 2.0,
                      "字符数 × 系数 = 估算 token", is_float=True),
            self._num("g", "ctx_reserve_ratio", "上下文可用比例",
                      self._gf("ctx_reserve_ratio", 0.75), 0.3, 1.0,
                      "按模型上限留多少比例用于输入", is_float=True),
            self._num("g", "memory_retries", "记忆提取解析重试", self._gi("memory_retries", 3),
                      0, 6, "AI 返回的 JSON 解析失败时重试次数"),
            self._num("g", "api_retries", "网络失败重试", self._gi("api_retries", 1), 0, 5),
            self._num("g", "log_retention", "日志保留条数", self._gi("log_retention", 500),
                      0, 5000, "超出自动删最旧（0=不清理）"),
        ])

        # ---- 防截断 ----
        anti = self._card("防截断", [
            self._sw("g", "anti_truncate_enabled", "AI 截断时自动续写",
                     g("anti_truncate_enabled", "0") == "1",
                     "仅对正文与改写生效（JSON 类任务不续写）"),
            self._num("g", "anti_truncate_rounds", "最多续写轮数",
                      self._gi("anti_truncate_rounds", 2), 1, 5),
            self._sw("g", "end_mark_enabled", "要求输出完成标志",
                     g("end_mark_enabled", "1") == "1",
                     "要求 AI 以【生成完毕】收尾，无标志即判定未写完（自动剥离）"),
        ])

        # ---- 草稿自检 ----
        stages_cur = [s.strip() for s in
                      str(g("draft_check_stages", "") or "").split(",") if s.strip()]
        boxes = {}
        for key, label in STAGES:
            boxes[key] = ft.Checkbox(label=label, value=(key in stages_cur))
        self._reg("g", "draft_check_stages",
                  lambda: ",".join(k for k, _ in STAGES if boxes[k].value), boxes)
        draft = self._card("草稿自检", [
            hint("勾选的阶段会让 AI 先写「草稿」再出自检，减少跑偏与夹带解释。"),
            ft.Row(list(boxes.values()), wrap=True, spacing=10),
            self._text("g", "draft_check_extra", "通用附加检查项",
                       g("draft_check_extra", ""),
                       "每行一条，追加进所有勾选阶段的自检清单",
                       multiline=True, lines=3),
            self._dd("g", "draft_check_prose_mode", "草稿模式",
                     g("draft_check_prose_mode", "full"),
                     [("full", "全量草稿（先写完草稿再正文）"),
                      ("interleaved", "穿插草稿（分段边写边查）")]),
            self._dd("g", "draft_check_density", "穿插密度",
                     g("draft_check_density", "standard"),
                     [("dense", "密集"), ("standard", "标准"), ("sparse", "稀疏")]),
        ])
        stage_cards = []
        for key, label in STAGES:
            stage_cards.append(self._card(f"「{label}」专属", [
                self._text("g", f"draft_check_extra_{key}", "附加检查项",
                           g(f"draft_check_extra_{key}", ""),
                           "只对该阶段生效，每行一条",
                           multiline=True, lines=2),
                self._text("g", f"draft_checklist_{key}", "自定义自检清单",
                           g(f"draft_checklist_{key}", ""),
                           "替换内置清单，每行一条", multiline=True, lines=3,
                           on_clear=True),
            ]))

        # ---- 文风审校 ----
        style = self._card("文风审校", [
            self._text("g", "style_cliches", "套话清单", g("style_cliches", ""),
                       "每行一条，审校时命中即提示（留空使用内置常见套话）",
                       multiline=True, lines=4, on_clear=True),
        ])

        # ---- 嵌入模型 ----
        self._embed_url = ft.TextField(label="接口地址", dense=True,
                                       value=g("embed_base_url", ""))
        self._embed_key = ft.TextField(
            label="API Key", dense=True, password=True, can_reveal_password=True,
            hint_text="已保存则留空（留空 = 不修改）")
        self._embed_model = ft.TextField(label="模型名", dense=True,
                                         value=g("embed_model", ""))
        self._reg("g", "embed_base_url", lambda: self._embed_url.value or "",
                  self._embed_url)
        self._reg("g", "embed_model", lambda: self._embed_model.value or "",
                  self._embed_model)
        self._widgets["embed_api_key"] = self._embed_key
        embed = self._card("嵌入接口（语义检索 / 语义查重）", [
            hint("可选功能：不填则不做语义检索，其余功能不受影响。"),
            self._embed_url, self._embed_key,
            ft.Row([ft.Container(self._embed_model, expand=True),
                    ft.IconButton(ft.Icons.CLOUD_DOWNLOAD, tooltip="获取模型列表",
                                  on_click=self._fetch_embed)], spacing=4),
            ft.Row([
                ft.OutlinedButton("测试连接", on_click=self._test_embed),
                ft.Text("（三项需同时填写才生效）", size=11,
                        color=ft.Colors.ON_SURFACE_VARIANT),
            ], spacing=8, wrap=True),
            self._embed_result,
        ])

        # ---- 自动备份 ----
        backup = self._card("自动备份（跟随本机）", [
            self._sw("a", "auto_backup", "启用定时自动备份",
                     bool(self._a("auto_backup", True))),
            self._num("a", "backup_interval_min", "备份间隔（分钟）",
                      int(self._a("backup_interval_min", 10) or 10), 1, 240),
            self._num("a", "backup_keep", "保留最近备份数",
                      int(self._a("backup_keep", 10) or 10), 1, 100),
        ])

        # ---- 电源（桌面专属） ----
        power = self._card("电源（仅电脑端生效）", [
            hint("手机系统自行管理休眠，这里只影响电脑版；配置随全量备份同步。"),
            self._dd("g", "keep_awake", "阻止电脑睡眠",
                     g("keep_awake", "1"),
                     [("0", "关闭"), ("1", "应用运行期间"),
                      ("2", "仅自动模式运行时")]),
            self._sw("g", "keep_awake_display", "同时保持屏幕常亮",
                     g("keep_awake_display", "0") == "1",
                     "笔记本更耗电，一般不用开"),
        ])

        # ---- 任务模板入口 ----
        tpl_row = ft.Card(ft.Container(ft.Column([
            ft.Row([ft.Icon(ft.Icons.DESCRIPTION, color=ft.Colors.PRIMARY),
                    ft.Text("任务模板", weight=ft.FontWeight.W_600, expand=True)]),
            ft.Text("16 个任务提示词（总纲/分卷/大纲/细纲/正文/改写/审校…）"
                    "已独立成页，在「我的 → 任务模板」编辑。", size=12,
                    color=ft.Colors.ON_SURFACE_VARIANT),
        ], spacing=6), padding=12))

        head = ft.Row([
            ft.IconButton(ft.Icons.ARROW_BACK, on_click=lambda e: (
                self.on_back() if self.on_back else None)),
            ft.Text("设置", size=20, weight=ft.FontWeight.BOLD, expand=True),
            ft.FilledButton("保存", on_click=self._save),
        ])

        body = ft.Column(
            [head, inject, mem, est, anti, draft] + stage_cards +
            [style, embed, backup, power, tpl_row,
             ft.Row([ft.FilledButton("保存设置", on_click=self._save)],
                    alignment=ft.MainAxisAlignment.CENTER)],
            scroll=ft.ScrollMode.AUTO, expand=True, spacing=8)
        return ft.Container(body, padding=14, expand=True)
