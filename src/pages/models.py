# -*- coding: utf-8 -*-
"""模型页：破甲概览 / 渠道故障自动切换 / 模型档案（全字段）/ 任务角色路由。

与桌面版「⚙ 模型」页对齐：档案含服务模板、思考模式、输出模式、超时、破甲绑定、
单价；路由含绑定档案、温度、锁定、角色提示词、备用渠道链。
"""
import flet as ft

import theme as T
from widgets import section, hint, toast, empty, confirm, page_header
from bridge import run_bg

# 与桌面 app/ui/dialogs.py 的 SERVICE_TEMPLATES 一致（自动填地址/模型/单价）
SERVICE_TEMPLATES = {
    "自定义": {},
    "DeepSeek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
                 "input_price": 1.0, "output_price": 2.0, "currency": "元"},
    "OpenAI": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini",
               "input_price": 0.15, "output_price": 0.6, "currency": "美元"},
    "智谱 GLM": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash",
                 "input_price": 0.1, "output_price": 0.1, "currency": "元"},
    "阿里通义": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                 "model": "qwen-plus", "input_price": 0.8, "output_price": 2.0,
                 "currency": "元"},
    "Ollama（本地）": {"base_url": "http://localhost:11434/v1", "model": "qwen2.5:14b",
                       "api_key": "ollama"},
    "LM Studio（本地）": {"base_url": "http://localhost:1234/v1", "model": "",
                          "api_key": "lm-studio"},
    "硅基流动": {"base_url": "https://api.siliconflow.cn/v1",
                 "model": "deepseek-ai/DeepSeek-V3",
                 "input_price": 2.0, "output_price": 3.0, "currency": "元"},
}

# 档位只写名字（2026-11-05 老板）：括号里的说明是按单一渠道实测写死的，
# 各 API 渠道对 最大/高/中/低 的定义与思考量差别很大，不能拿一家标注通用下拉。
REASONING_OPTIONS = [
    ("最大", "max"),
    ("高", "high"),
    ("中", "medium"),
    ("低", "low"),
    ("关闭", "off"),
]


class ModelsPage:
    def __init__(self, page, session):
        self.page = page
        self.session = session
        self.on_refresh = None   # main() 注入：整页重建
        self._sub = None         # 子页面（None=本页；"jailbreak"=破甲库）
        self._jb = None

    # ---------- 基础 ----------

    @property
    def gstore(self):
        return self.session.app.gstore

    def _refresh(self):
        if self.on_refresh:
            self.on_refresh()
        else:
            try:
                self.page.update()
            except Exception:
                pass

    def _height(self, ratio=0.86, fallback=640):
        h = getattr(self.page, "height", None)
        try:
            h = int(h)
        except (TypeError, ValueError):
            h = 0
        return max(320, int((h or fallback) * ratio))

    # ---------- 子页面：破甲库 ----------

    def _open_jailbreaks(self, e=None):
        from pages.jailbreaks import JailbreakLibrary
        if self._jb is None:
            self._jb = JailbreakLibrary(self.page, self.session,
                                        on_back=self._close_sub)
            self._jb.on_refresh = self._refresh
        self._sub = "jailbreak"
        self._refresh()

    def _close_sub(self):
        self._sub = None
        self._refresh()

    # =====================================================
    # 档案编辑
    # =====================================================

    @staticmethod
    def _jailbreak_label(profile: dict) -> str:
        v = str((profile or {}).get("jailbreak") or "").strip()
        if not v:
            return "跟随全局"
        if v == "__none__":
            return "不注入"
        return v

    def _jailbreak_options(self):
        """破甲下拉选项：跟随全局（当前：X）/ 不注入 / 各套破甲名。"""
        g = self.gstore
        active, names = "", []
        try:
            active = g.get_active_jailbreak_name() or ""
            names = [p.get("name") for p in g.list_jailbreak_profiles()
                     if p.get("name")]
        except Exception:
            pass
        opts = [ft.dropdown.Option(key="", text=f"跟随全局（当前：{active or '无'}）"),
                ft.dropdown.Option(key="__none__", text="不注入破甲")]
        opts += [ft.dropdown.Option(key=n, text=f"使用：{n}") for n in names]
        return opts

    def _profile_dialog(self, e, profile: dict | None = None):
        g = self.gstore
        p = profile or {}
        key_plain = ""
        if p:
            try:
                from app.core.api import decrypt_key
                key_plain = decrypt_key(p.get("api_key") or "")
            except Exception:
                key_plain = ""

        # 弹层里的控件一律「标签在框外」（T.field_group）：保存时要读 .value，
        # 所以标签单独画一行，不用 Flet 的浮动 label。
        def _lbl(text, ctrl):
            return T.field_group(ctrl, text)

        tpl_dd = T.select("", "自定义", [(k, k) for k in SERVICE_TEMPLATES])
        name = T.field("", p.get("name", ""), dense=True)
        base_url = T.field("", p.get("base_url", ""),
                           hint="https://api.deepseek.com/v1", dense=True)
        api_key = T.field("", key_plain, dense=True)
        api_key.password = True
        api_key.can_reveal_password = True
        model = T.field("", p.get("model", ""),
                        hint="deepseek-chat / gpt-4o / qwen2.5:14b …", dense=True)
        ctx = T.field("", str(p.get("context_length") or 1000000), dense=True)
        reasoning = T.select("", str(p.get("reasoning_mode") or "max"),
                             [(k, t) for t, k in REASONING_OPTIONS])
        out_mode = T.select("", str(p.get("max_output_mode") or "auto"),
                            [("auto", "自动（程序按任务给足上限）"),
                             ("manual", "手动设置上限")])
        max_tokens = T.field("", str(p.get("max_tokens") or 4096), dense=True)
        timeout = T.field("", str(p.get("timeout") or 120), dense=True)
        # 渠道限流（2026-09-19）：部分中转站「1 分钟最多 N 次」且失败也计入，
        # 自动模式失败补救的连发会把配额打光 → 填上限后发送前会排队等待；0=不限。
        rpm = T.field("", str(p.get("rpm") or 0), dense=True)
        jb_dd = T.select("", "", [])
        # 破甲下拉按实际取值配置（避免显示文本与存储值混用）
        jb_cur = str(p.get("jailbreak") or "").strip()
        jb_dd.options = self._jailbreak_options()
        jb_dd.value = jb_cur

        price_in = T.field("", str(p.get("input_price") or 0), dense=True)
        price_out = T.field("", str(p.get("output_price") or 0), dense=True)
        currency = T.select("", str(p.get("currency") or "元"),
                            [("元", "元"), ("美元", "美元")])

        def _apply_template(ev):
            tpl = SERVICE_TEMPLATES.get(tpl_dd.value or "", {})
            if not tpl:
                return
            if tpl.get("base_url"):
                base_url.value = tpl["base_url"]
            if tpl.get("model") is not None:
                model.value = tpl["model"]
                if not (name.value or "").strip():
                    name.value = tpl["model"]
            if tpl.get("input_price") is not None:
                price_in.value = str(tpl["input_price"])
            if tpl.get("output_price") is not None:
                price_out.value = str(tpl["output_price"])
            if tpl.get("currency"):
                currency.value = tpl["currency"]
            if tpl.get("api_key"):
                api_key.value = tpl["api_key"]
            self.page.update()

        tpl_dd.on_select = _apply_template

        def _collect() -> dict:
            """把表单读成可保存的档案 dict（api_key 保持密文/明文原样）。"""
            raw_key = (api_key.value or "").strip()
            if raw_key and raw_key != key_plain:
                from app.core.api import encrypt_key
                enc = encrypt_key(raw_key)
            else:
                enc = p.get("api_key") or ("" if not raw_key else raw_key)
            def _num(v, dv):
                try:
                    return float(str(v).strip())
                except (TypeError, ValueError):
                    return dv
            def _int(v, dv):
                try:
                    return int(float(str(v).strip()))
                except (TypeError, ValueError):
                    return dv
            return {
                "name": (name.value or "").strip() or "未命名档案",
                "base_url": (base_url.value or "").strip(),
                "api_key": enc,
                "model": (model.value or "").strip(),
                "context_length": _int(ctx.value, 1000000),
                "max_tokens": _int(max_tokens.value, 4096),
                "reasoning_mode": reasoning.value or "max",
                "max_output_mode": out_mode.value or "auto",
                "timeout": _int(timeout.value, 120),
                "rpm": max(0, _int(rpm.value, 0)),
                "jailbreak": jb_dd.value or "",
                "temperature": p.get("temperature", 0.8),
                "input_price": _num(price_in.value, 0),
                "output_price": _num(price_out.value, 0),
                "currency": currency.value or "元",
            }

        def _close(ev):
            self.page.pop_dialog()

        def _save(ev):
            data = _collect()
            if not data["base_url"]:
                toast(self.page, "请填写接口地址", error=True)
                return
            try:
                g.save_profile(data, p.get("id"))
            except Exception as ex:
                toast(self.page, f"保存失败：{ex}", error=True)
                return
            self.page.pop_dialog()
            toast(self.page, "档案已保存")
            self._refresh()

        def _test(ev):
            data = _collect()
            if not data["base_url"]:
                toast(self.page, "请先填写接口地址", error=True)
                return
            toast(self.page, "正在测试连接…")

            def job():
                from app.core.api import LLMClient
                return LLMClient({**data, "timeout": 60}).test()

            def done(r):
                ok = isinstance(r, dict) and r.get("ok")
                toast(self.page, str((r or {}).get("message") or r),
                      error=not ok)
            run_bg(self.page, job, done)

        def _fetch(ev):
            data = _collect()
            if not data["base_url"]:
                toast(self.page, "请先填写接口地址", error=True)
                return
            toast(self.page, "正在获取模型列表…")

            def job():
                from app.core.api import LLMClient
                return LLMClient(data).list_models()

            def done(r):
                if not isinstance(r, dict) or not r.get("ok"):
                    toast(self.page, str((r or {}).get("message") or "获取失败"),
                          error=True)
                    return
                models = r.get("models") or []
                if not models:
                    toast(self.page, "服务方未返回任何模型", error=True)
                    return
                self._pick_model(models, ctx, model, name)
            run_bg(self.page, job, done)

        form = ft.Column([
            _lbl("服务模板（自动填地址/模型/单价）", tpl_dd),
            _lbl("档案名称", name),
            _lbl("接口地址 Base URL", base_url),
            _lbl("API Key（本地模型可留空）", api_key),
            _lbl("模型名", ft.Row([ft.Container(model, expand=True),
                                  ft.IconButton(ft.Icons.CLOUD_DOWNLOAD,
                                                tooltip="获取模型列表",
                                                on_click=_fetch)],
                                 spacing=T.GAP_XS)),
            _lbl("上下文长度（token）", ctx),
            section("思考与输出"),
            _lbl("思考模式", reasoning),
            _lbl("输出模式", out_mode),
            _lbl("手动最大输出 token（输出模式=手动时生效）", max_tokens),
            _lbl("超时（秒）", timeout),
            _lbl("每分钟请求上限（0=不限，防渠道限流）", rpm),
            section("破甲与计费"),
            _lbl("破甲", jb_dd),
            hint("「跟随全局」用破甲库里当前生效那套；也可以给这个模型单独指定一套。"),
            ft.Row([ft.Container(_lbl("输入价格（元/百万 token）", price_in),
                                 expand=True),
                    ft.Container(_lbl("输出价格（元/百万 token）", price_out),
                                 expand=True)], spacing=T.GAP_S),
            _lbl("币种", currency),
            ft.Row([
                T.ghost("测试连接", icon=ft.Icons.NETWORK_CHECK, on_click=_test),
                T.ghost("取消", on_click=_close),
                T.primary("保存", on_click=_save),
            ], alignment=ft.MainAxisAlignment.END, spacing=T.GAP_S, wrap=True),
        ], scroll=ft.ScrollMode.AUTO, spacing=T.GAP_S,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

        self.page.show_dialog(ft.AlertDialog(
            modal=True,
            shape=ft.RoundedRectangleBorder(radius=T.R_XL),
            title=ft.Text("编辑模型档案" if p else "新建模型档案", size=17,
                          weight=ft.FontWeight.W_600),
            content=ft.Container(form,
                                 width=min(580, int(getattr(self.page, "width", 580) or 580)),
                                 height=self._height(0.84)),
            actions=[],
        ))

    def _pick_model(self, models: list, ctx_field, model_field, name_field):
        """模型列表选择 sheet：点一条填入模型名（带上下文长度时一并填）。"""
        rows = []
        for m in models[:400]:
            mid = str(m.get("id") or "")
            cl = m.get("context_length")
            rows.append(T.menu_row(
                ft.Icons.MODEL_TRAINING, mid,
                f"上下文 {cl}" if cl else "",
                on_click=lambda e, i=mid, c=cl: self._model_picked(
                    i, c, ctx_field, model_field, name_field)))
        T.sheet(self.page, f"选择模型（{len(rows)}）", rows,
                height=self._height(0.86))

    def _model_picked(self, model_id, ctx_len, ctx_field, model_field, name_field):
        self.page.pop_dialog()
        model_field.value = model_id
        if not (name_field.value or "").strip():
            name_field.value = model_id
        if ctx_len:
            try:
                ctx_field.value = str(int(ctx_len))
            except (TypeError, ValueError):
                pass
        self.page.update()
        toast(self.page, f"已选择模型「{model_id}」")

    def _delete_profile(self, profile: dict):
        def _yes():
            try:
                self.gstore.delete_profile(profile["id"])
            except Exception as ex:
                toast(self.page, f"删除失败：{ex}", error=True)
                return
            toast(self.page, f"已删除档案「{profile.get('name')}」")
            self._refresh()
        confirm(self.page, "删除档案",
                f"确定删除模型档案「{profile.get('name')}」？绑定它的角色会解除绑定，"
                "备用渠道链里的引用一并清理。", _yes, yes_text="删除")

    # =====================================================
    # 路由
    # =====================================================

    def _route_sheet(self, route: dict):
        g = self.gstore
        role = route["role"]
        name = route.get("name") or role
        profiles = g.list_profiles()
        cur_pid = route.get("profile_id")

        # 裸控件 + T.field_group（保存时要读 .value），标签不再走浮动 label
        prof_dd = T.select("", str(cur_pid) if cur_pid else "",
                           [("", "（未绑定）")] +
                           [(str(p["id"]), p["name"]) for p in profiles])
        temp = route.get("temperature")
        temp_field = T.field(
            "", "" if temp in (None, -1, -1.0) else str(temp), dense=True)
        lock_row, locked_sw = T.switch_row(
            "锁定（一键绑定时不变更）", bool(route.get("locked")))

        def _save_bind(ev):
            try:
                pid = int(prof_dd.value) if (prof_dd.value or "").strip() else None
            except (TypeError, ValueError):
                pid = None
            raw = (temp_field.value or "").strip()
            try:
                t = float(raw) if raw else -1.0
            except (TypeError, ValueError):
                toast(self.page, "温度要填数字（留空 = 默认）", error=True)
                return
            try:
                g.save_route(role, pid, t)
            except Exception as ex:
                toast(self.page, f"保存失败：{ex}", error=True)
                return
            self.page.pop_dialog()
            toast(self.page, f"「{name}」已更新")
            self._refresh()

        def _toggle_lock(ev):
            try:
                g.set_route_locked(role, bool(ev.control.value))
            except Exception as ex:
                toast(self.page, f"设置失败：{ex}", error=True)
                return
            toast(self.page, "已锁定（一键绑定全部角色时不变更）" if ev.control.value
                  else "已解锁")

        def _edit_prompt(ev):
            self.page.pop_dialog()
            field = T.field("", route.get("system_prompt") or "",
                            multiline=True, min_lines=8, max_lines=14)

            def _ok(e2):
                try:
                    g.save_route(role, cur_pid, route.get("temperature") or 0.7,
                                 system_prompt=field.value or "")
                except Exception as ex:
                    toast(self.page, f"保存失败：{ex}", error=True)
                    return
                self.page.pop_dialog()
                toast(self.page, f"「{name}」提示词已保存")
                self._refresh()

            self.page.show_dialog(ft.AlertDialog(
                modal=True, title=ft.Text(f"角色提示词 · {name}", size=17,
                                          weight=ft.FontWeight.W_600),
                shape=ft.RoundedRectangleBorder(radius=T.R_XL),
                content=ft.Container(T.field_group(
                    field, f"「{name}」角色提示词（留空 = 用内置）"),
                    width=540, height=self._height(0.6)),
                actions=[T.ghost("取消",
                                 on_click=lambda e2: self.page.pop_dialog()),
                         T.primary("保存", on_click=_ok)],
                actions_alignment=ft.MainAxisAlignment.END))

        def _fallback(ev):
            self.page.pop_dialog()
            self._fallback_sheet(route)

        def _del_role(ev):
            self.page.pop_dialog()

            def _yes():
                try:
                    g.delete_role(role)
                except Exception as ex:
                    toast(self.page, f"删除失败：{ex}", error=True)
                    return
                toast(self.page, f"已删除角色「{name}」")
                self._refresh()
            confirm(self.page, "删除角色",
                    f"确定删除任务角色「{name}」？该角色的绑定与备用渠道配置一并移除。",
                    _yes, yes_text="删除")

        chain = []
        try:
            chain = g.get_fallback_profile_ids(role) or []
        except Exception:
            chain = []
        names = {p["id"]: p["name"] for p in profiles}
        chain_text = ("备用渠道：" + " → ".join(names.get(i, "（已删除）") for i in chain)
                      if chain else "备用渠道：未配置（主渠道故障时不会切换）")

        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column([
                ft.Text(f"任务角色：{name}", size=16, weight=ft.FontWeight.BOLD),
                hint("不同任务可用不同模型：正文用最强模型，记忆/检查用便宜快模型。"),
                T.field_group(prof_dd, "绑定模型档案"),
                T.field_group(temp_field, "温度（留空 = 用档案默认）"),
                lock_row,
                ft.Text(chain_text, size=12, color=T.muted()),
                T.button_row(
                    T.primary("保存绑定", on_click=_save_bind),
                    T.tonal("备用渠道链", icon=ft.Icons.ALT_ROUTE,
                            on_click=_fallback),
                    fill=False),
                T.button_row(
                    T.ghost("编辑角色提示词", icon=ft.Icons.EDIT_NOTE,
                            on_click=_edit_prompt),
                    T.ghost("删除角色", icon=ft.Icons.DELETE_OUTLINE,
                            on_click=_del_role),
                    fill=False),
            ], spacing=T.GAP_M, scroll=ft.ScrollMode.AUTO),
                padding=T.GAP_L, height=min(560, self._height(0.8))),
            show_drag_handle=True))

    def _fallback_sheet(self, route: dict):
        """备用渠道链：主渠道报渠道层故障时按顺序切换。"""
        g = self.gstore
        role = route["role"]
        name = route.get("name") or role
        profiles = g.list_profiles()
        names = {p["id"]: f"{p['name']}（{p.get('model') or ''}）" for p in profiles}
        main_id = route.get("profile_id")
        try:
            cur = list(g.get_fallback_profile_ids(role) or [])
        except Exception:
            cur = []

        holder = {"ids": cur}
        list_col = ft.Column([], spacing=0)
        add_dd = ft.Dropdown(label="添加渠道", dense=True, options=[])

        def _refresh_list():
            list_col.controls.clear()
            if not holder["ids"]:
                list_col.controls.append(ft.Container(
                    ft.Text("链条为空：主渠道失败时不会切换",
                            size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                    padding=8))
            for n, pid in enumerate(holder["ids"]):
                list_col.controls.append(ft.ListTile(
                    dense=True,
                    leading=ft.Text(str(n + 1), size=13),
                    title=ft.Text(names.get(pid, "（档案已删除，保存后自动摘掉）"),
                                  size=13),
                    trailing=ft.Row([
                        ft.IconButton(ft.Icons.ARROW_UPWARD, icon_size=16,
                                      on_click=lambda e, i=n: _move(i, -1)),
                        ft.IconButton(ft.Icons.ARROW_DOWNWARD, icon_size=16,
                                      on_click=lambda e, i=n: _move(i, 1)),
                        ft.IconButton(ft.Icons.CLOSE, icon_size=16,
                                      on_click=lambda e, i=n: _remove(i)),
                    ], spacing=0, tight=True),
                ))
            # 可添加项：排除链内已有与主渠道自身
            add_dd.options = [
                ft.dropdown.Option(key=str(p["id"]), text=names[p["id"]])
                for p in profiles
                if p["id"] not in holder["ids"] and p["id"] != main_id
            ]
            add_dd.value = None
            self.page.update()

        def _move(i, delta):
            j = i + delta
            if 0 <= i < len(holder["ids"]) and 0 <= j < len(holder["ids"]):
                ids = holder["ids"]
                ids[i], ids[j] = ids[j], ids[i]
                _refresh_list()

        def _remove(i):
            if 0 <= i < len(holder["ids"]):
                holder["ids"].pop(i)
                _refresh_list()

        def _add(ev):
            pid = add_dd.value
            if not pid:
                toast(self.page, "请先选择要添加的渠道", error=True)
                return
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                return
            if pid not in holder["ids"]:
                holder["ids"].append(pid)
            _refresh_list()

        def _save(ev):
            try:
                g.set_fallback_profile_ids(role, holder["ids"])
            except Exception as ex:
                toast(self.page, f"保存失败：{ex}", error=True)
                return
            self.page.pop_dialog()
            toast(self.page, f"「{name}」备用渠道已保存（{len(holder['ids'])} 条）")
            self._refresh()

        self.page.show_dialog(ft.BottomSheet(
            content=ft.Container(ft.Column([
                ft.Text(f"备用渠道 · {name}", size=16, weight=ft.FontWeight.BOLD),
                hint("主渠道报渠道层故障（余额耗尽/限流/Key 失效/服务端 5xx/网络不通/"
                     "空响应）且重试仍失败时，按下面顺序自动换渠道重发。"
                     "内容类问题（拒答、被拦截、截断）不会触发切换。"),
                ft.Container(list_col, height=self._height(0.34)),
                ft.Row([ft.Container(add_dd, expand=True),
                        ft.FilledButton("添加", on_click=_add)], spacing=8),
                ft.Row([
                    ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                    ft.FilledButton("保存", on_click=_save),
                ], alignment=ft.MainAxisAlignment.END, spacing=8),
            ], spacing=8, scroll=ft.ScrollMode.AUTO),
                padding=16, height=min(620, self._height(0.86))),
            show_drag_handle=True))
        _refresh_list()

    def _bind_all(self, e):
        g = self.gstore
        profiles = g.list_profiles()
        if not profiles:
            toast(self.page, "请先新建模型档案", error=True)
            return
        pid = profiles[0]["id"]
        n = 0
        for r in g.list_routes():
            if not r.get("locked"):
                g.save_route(r["role"], pid, r.get("temperature") or 0.7)
                n += 1
        toast(self.page, f"已把 {n} 个未锁定角色绑定到「{profiles[0]['name']}」")
        self._refresh()

    def _add_role(self, e):
        g = self.gstore
        key = ft.TextField(label="角色标识（英文，如 custom_check）", dense=True)
        name = ft.TextField(label="显示名称", dense=True)

        def _ok(ev):
            rk = (key.value or "").strip()
            nm = (name.value or "").strip() or rk
            if not rk:
                toast(self.page, "请填写角色标识", error=True)
                return
            try:
                g.add_role(rk, nm)
            except Exception as ex:
                toast(self.page, f"新增失败：{ex}", error=True)
                return
            self.page.pop_dialog()
            toast(self.page, f"已新增角色「{nm}」")
            self._refresh()

        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("新增任务角色"),
            content=ft.Container(ft.Column([key, name], tight=True, spacing=10),
                                 width=440),
            actions=[ft.TextButton("取消", on_click=lambda ev: self.page.pop_dialog()),
                     ft.FilledButton("新增", on_click=_ok)],
            actions_alignment=ft.MainAxisAlignment.END))

    def _toggle_failover(self, e):
        try:
            self.gstore.set_channel_failover_enabled(bool(e.control.value))
        except Exception as ex:
            toast(self.page, f"设置失败：{ex}", error=True)
            return
        toast(self.page, "渠道故障自动切换已" +
              ("开启" if e.control.value else "关闭（即使配了备用链也不切换）"))
        self._refresh()

    # =====================================================
    # 构建
    # =====================================================

    def build(self) -> ft.Control:
        if self._sub == "jailbreak" and self._jb is not None:
            return self._jb.build()
        g = self.gstore

        try:
            failover_on = g.channel_failover_enabled()
        except Exception:
            failover_on = True
        try:
            jb_name = g.get_active_jailbreak_name() or ""
            jb_count = len(g.list_jailbreak_profiles())
            jb_on = g.get_jailbreak_enabled()
        except Exception:
            jb_name, jb_count, jb_on = "", 0, False

        cards = []
        for p in g.list_profiles():
            cards.append(T.card(ft.Column([
                ft.Row([
                    ft.Container(ft.Icon(ft.Icons.SMART_TOY, color=T.brand(),
                                         size=19),
                                 width=34, height=34, border_radius=T.R_MD,
                                 bgcolor=T.soft(T.brand(), 0.10),
                                 alignment=ft.Alignment.CENTER),
                    ft.Text(p.get("name", ""), size=15,
                            weight=ft.FontWeight.W_600, expand=True),
                    ft.IconButton(ft.Icons.EDIT_OUTLINED, icon_size=18,
                                  tooltip="编辑",
                                  on_click=lambda e, pp=p: self._profile_dialog(e, pp)),
                    ft.IconButton(ft.Icons.DELETE_OUTLINE, icon_size=18,
                                  tooltip="删除",
                                  icon_color=T.muted(),
                                  on_click=lambda e, pp=p: self._delete_profile(pp)),
                ], spacing=T.GAP_S, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Text(f"{p.get('model', '')} ｜ {p.get('base_url', '')}",
                        size=11, color=T.muted(),
                        max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                ft.Row([
                    T.pill("破甲：" + self._jailbreak_label(p)),
                    ft.Text(f"上下文 {p.get('context_length') or 0} ｜ "
                            f"单价 {p.get('input_price') or 0}/"
                            f"{p.get('output_price') or 0} {p.get('currency') or '元'}",
                            size=10, color=T.muted()),
                ], spacing=T.GAP_S, wrap=True),
            ], spacing=T.GAP_S - 2)))

        route_rows = []
        for r in g.list_routes():
            bound = r.get("profile_name") or "未绑定"
            try:
                chain_n = len(g.get_fallback_profile_ids(r["role"]) or [])
            except Exception:
                chain_n = 0
            tags = []
            if r.get("locked"):
                tags.append("锁定")
            tags.append(f"备用 {chain_n}" if chain_n else "无备用")
            temp = r.get("temperature")
            temp_txt = "默认" if temp in (None, -1, -1.0) else str(temp)
            route_rows.append(T.menu_row(
                ft.Icons.ACCOUNT_TREE_OUTLINED, r.get("name") or r["role"],
                f"{bound} · 温度 {temp_txt} · " + " · ".join(tags),
                on_click=(lambda e, rr=r: self._route_sheet(rr))))

        body = ft.Column([
            T.page_head("模型", "档案、任务角色与渠道容错",
                        trailing=T.primary("新建档案", icon=ft.Icons.ADD,
                                           on_click=lambda e: self._profile_dialog(e))),
            section("破甲"),
            T.card(ft.Column([
                ft.Row([
                    ft.Container(
                        ft.Icon(ft.Icons.SHIELD_OUTLINED, size=19,
                                color=T.brand() if jb_on else T.muted()),
                        width=34, height=34, border_radius=T.R_MD,
                        bgcolor=T.soft(T.brand() if jb_on else T.muted(), 0.10),
                        alignment=ft.Alignment.CENTER),
                    ft.Text(f"当前生效：{jb_name or '（无）'}", size=14,
                            weight=ft.FontWeight.W_600, expand=True),
                    T.pill("已启用" if jb_on else "已关闭",
                           T.success() if jb_on else T.muted()),
                ], spacing=T.GAP_S, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Text(f"共 {jb_count} 套破甲。按角色注入：创作系全套，"
                        "检查/文风审校只给引用许可，记忆提取不注入。",
                        size=11, color=T.muted()),
                T.tonal("打开破甲库管理", icon=ft.Icons.SHIELD,
                        on_click=self._open_jailbreaks, expand=True),
            ], spacing=T.GAP_S)),
            section("渠道故障自动切换"),
            T.card(ft.Row([
                ft.Container(ft.Icon(ft.Icons.ALT_ROUTE, color=T.brand(), size=19),
                             width=34, height=34, border_radius=T.R_MD,
                             bgcolor=T.soft(T.brand(), 0.10),
                             alignment=ft.Alignment.CENTER),
                ft.Column([
                    ft.Text("主渠道故障时自动换渠道重发", size=14,
                            weight=ft.FontWeight.W_600),
                    ft.Text("余额耗尽/限流/Key 失效/服务端 5xx/网络不通且重试仍失败时，"
                            "按角色配置的备用链顺序切换；内容类问题不切换。",
                            size=11, color=T.muted()),
                ], spacing=2, expand=True),
                ft.Switch(value=bool(failover_on),
                          active_color=T.brand(),
                          on_change=self._toggle_failover),
            ], spacing=T.GAP_S, vertical_alignment=ft.CrossAxisAlignment.CENTER)),
            section("模型档案"),
            *(cards if cards else [empty("还没有模型档案\n点「＋ 新建档案」接入 "
                                         "DeepSeek / OpenAI / 本地 Ollama 等")]),
            section("任务角色路由"),
            hint("点角色可绑定模型、设温度、锁定、编辑提示词、配备用渠道"),
            # 平分两列 + 与下方列表留出间距：原来按钮行紧贴列表首行，像长在一起。
            ft.Container(
                T.button_row(
                    T.tonal("一键绑定全部角色", icon=ft.Icons.LINK,
                            on_click=self._bind_all),
                    T.ghost("＋ 新增角色", icon=ft.Icons.PERSON_ADD_ALT,
                            on_click=self._add_role)),
                margin=ft.Margin(left=0, right=0, top=T.GAP_XS, bottom=T.GAP_M)),
            (T.card(ft.Column(route_rows, spacing=0), padding=0)
             if route_rows else T.card(empty("暂无任务角色"), padding=0)),
        ], scroll=ft.ScrollMode.AUTO, expand=True, spacing=0,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        return ft.Container(body, padding=T.GAP_L, expand=True)
