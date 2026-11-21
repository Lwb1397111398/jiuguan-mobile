# -*- coding: utf-8 -*-
"""LLM API 客户端：OpenAI 兼容接口，SSE 流式输出。

- 云端服务（DeepSeek/OpenAI/GLM 等）与本地模型（LM Studio/Ollama）统一走本模块
- API Key 用 Windows DPAPI 加密存储（不明文落盘）
- 断线/拒答不静默失败：错误信息原样上抛，由 UI 明确提示
"""
import base64
import ctypes
import json
import select
import sys
import threading
import time
from typing import Callable

import requests

from .throttle import acquire as _throttle_acquire

# 输出上限自动降级地板：渠道拒绝 max_tokens（超模型最大输出）时减半重发，
# 降到该值以下就不再带 max_tokens（走渠道默认）。设为 8192 而非更小值——
# 渠道默认常是 2048/4096，地板过低会让降级后的实际上限反而更小，正文更容易截断。
_MAX_TOKENS_DOWNGRADE_FLOOR = 8192

# 安全审核类终止原因（2026-11-05）：平台/渠道的内容审核在**生成过程中**掐断输出时
# 不会返回 HTTP 错误，而是在流式最后一帧给一个 finish_reason。智谱 GLM 官方文档
# 明确：流式生成中被判违法/不良信息时返回 `finish_reason: "sensitive"`，同帧带
# `content_filter` 明细（输入侧 role=user、输出侧 role=assistant）；OpenAI 兼容层
# 用 `content_filter`，Gemini 走候选的 SAFETY。
#
# 识别它有三个用处：① 如实告知「是平台审核掐的，不是破限失效、不是网络掐线」；
# ② 内容审核类**重试同报文必然再被掐**（还烧配额），上层要据此停止重试；
# ③ 事后可取证（日志记 finish_reason）。
#
# ⚠ 破限（jailbreak）改的是模型自身的对齐，**管不了平台侧审核层**——同一份报文
# 酒馆能过而本程序被掐、或官方渠道比中转站更容易被掐，都属这一层的差异。
SAFETY_FINISH_REASONS = frozenset({
    "sensitive", "content_filter", "content_filtered", "safety",
    "prohibited_content", "blocked",
})


def normalize_finish_reason(raw: str) -> str:
    """归一化终止原因（小写、去空白）；没收到返回 ''。"""
    return (raw or "").strip().lower()


def classify_finish(raw: str) -> tuple:
    """终止原因 → (归一化值, 是否长度截断, 是否内容审核掐断)。

    抽成纯函数是为了可测：这三分支决定下游是「自动续写」「提示调大上限」
    还是「停止重试、换渠道」，判错方向就会把平台审核误报成破限失效
    （2026-11-05 老板实测就是这么被误导的）。
    """
    fr = normalize_finish_reason(raw)
    return fr, fr == "length", fr in SAFETY_FINISH_REASONS


def _request_with_proxy_fallback(http_call, url, **kw):
    """环境变量代理指向死端口时自动改直连重发一次（2026-10-21）。

    机器残留 HTTPS_PROXY/HTTP_PROXY=http://127.0.0.1:7897（Clash 类代理退出后的
    残留环境变量）而该端口无程序监听时，requests 默认 trust_env 会读环境变量，
    所有 AI 请求先撞本地死端口 →「WinError 10061 被对方计算机积极拒绝」，
    表面上像服务方拒绝，实为本地代理死端口。只对 ProxyError（代理本身连不上）
    触发直连重试：代理活着的用户请求正常走代理，完全不受影响。"""
    try:
        return http_call(url, **kw)
    except requests.exceptions.ProxyError:
        kw = dict(kw)
        kw["proxies"] = {"http": None, "https": None}
        return http_call(url, **kw)


# ---------------- 工具交稿（2026-11-21，复刻酒馆 SPreset 报文）----------------
#
# 有的破甲预设不让模型写 content，而是让它调一个工具、把思考与正文分别塞进
# 工具参数的字段里（小猫之神 Meowssiah 的 `soliumbra_reply{think_nya, game_content}`）。
# 这条通道的全部权重来自「模型以为自己在一个进行到一半的工具流程里」，所以必须
# 真发 tools、真收 tool_calls，不能改成「在正文里输出 JSON」——那样形态就变了。
#
# 三个纯函数负责把这条通道**归一化成普通正文/思考**：上层 pipeline、quality_gate、
# 归档、记忆一行都不用改，它们看到的仍是 (text, thinking)。
#
# 增量提取的动机：tool_call.arguments 是**一整串 JSON 片段**流下来的，不像 content
# 那样天然分块。等生成完再一次性回吐，界面会长时间空白（模型在写 JSON 转义），
# 老板会以为卡死。所以按字段做「半截 JSON 取值」，能确定多少吐多少。


def _partial_json_string(buf: str, field: str) -> str | None:
    """从半截（或完整）JSON 文本里取 ``field`` 字段当前已确定的字符串值。

    找不到该字段 / 值还没开始 → None；值已结束 → 完整值；值写到一半被网络
    切片截断 → 已经确定的那部分（转义序列被切断时**不猜**，留给下一片）。

    只认「上一非空白字符是 { 或 ,」的 key，避免命中别的字符串值里恰好出现的
    同名字段（正文里写一句 `"game_content"` 不算字段）。
    """
    key = '"%s"' % field
    i = buf.find(key)
    while i >= 0:
        j = i - 1
        while j >= 0 and buf[j] in " \t\r\n":
            j -= 1
        if j >= 0 and buf[j] in "{,":
            break
        i = buf.find(key, i + len(key))
    if i < 0:
        return None
    i += len(key)
    n = len(buf)
    while i < n and buf[i] in " \t\r\n":
        i += 1
    if i >= n or buf[i] != ":":
        return None
    i += 1
    while i < n and buf[i] in " \t\r\n":
        i += 1
    if i >= n or buf[i] != '"':
        return None
    i += 1
    out: list[str] = []
    while i < n:
        ch = buf[i]
        if ch == "\\":
            if i + 1 >= n:
                break                    # 转义对被切断：等下一片
            esc = buf[i + 1]
            if esc == "u":
                if i + 6 > n:
                    break                # \uXXXX 被切断
                try:
                    out.append(chr(int(buf[i + 2:i + 6], 16)))
                except ValueError:
                    break
                i += 6
                continue
            mapping = {'"': '"', "\\": "\\", "/": "/", "b": "\b",
                       "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
            out.append(mapping.get(esc, buf[i:i + 2]))
            i += 2
            continue
        if ch == '"':
            break                        # 值结束
        out.append(ch)
        i += 1
    return "".join(out)


class _ToolArgStream:
    """按字段增量回吐 tool_call.arguments 里的字符串值。

    一个实例只管一个字段：`feed(新增片段)` 返回「相对上次多出来的正文」。
    """

    def __init__(self, field: str):
        self.field = field
        self.buf = ""
        self.emitted = 0

    def feed(self, chunk: str) -> str:
        if not chunk or not self.field:
            return ""
        self.buf += chunk
        value = _partial_json_string(self.buf, self.field)
        if not value or len(value) <= self.emitted:
            return ""
        new = value[self.emitted:]
        self.emitted = len(value)
        return new


def extract_tool_reply(tool_calls: dict, spec: dict) -> tuple[str, str, str]:
    """从累积的 tool_calls 里取出（思考，正文），返回 (thinking, text, error)。

    tool_calls: {index: {"id","name","args"}}（流式按 index 累积的结果）
    spec:       {"name","think_field","text_field"}（破甲骨架 @reply 段声明）

    - 优先认 ``spec["name"]`` 指定的那个工具；同名多个时取第一个能解析出正文的。
    - 参数是半截 JSON（模型被掐断）时走 ``_partial_json_string`` 兜底，**不丢已写部分**
      ——截断时正文往往已经写了大半，扔掉比留着糟。
    - 返回 error 非空表示「调了工具但拿不到正文」，上层应报错而不是静默成功。
    """
    if not tool_calls:
        return "", "", ""
    want = str((spec or {}).get("name") or "")
    think_field = str((spec or {}).get("think_field") or "")
    text_field = str((spec or {}).get("text_field") or "")
    if not text_field:
        return "", "", "破甲骨架 @reply 没有指定正文字段"
    ordered = [tool_calls[k] for k in sorted(tool_calls)]
    matched = [s for s in ordered
               if not want or not s.get("name") or s.get("name") == want]
    if not matched:
        names = "、".join(sorted({str(s.get("name") or "?") for s in ordered}))
        return "", "", (f"模型调用的是 {names}，与破甲档案声明的交稿工具 "
                        f"{want or '(未声明)'} 不一致")
    for slot in matched:
        raw = slot.get("args") or ""
        if not raw.strip():
            continue
        obj = None
        try:
            loaded = json.loads(raw)
            obj = loaded if isinstance(loaded, dict) else None
        except json.JSONDecodeError:
            obj = None
        if obj is None:
            # 半截 JSON：逐字段兜底提取
            obj = {}
            for f in (think_field, text_field):
                if f:
                    v = _partial_json_string(raw, f)
                    if v is not None:
                        obj[f] = v
        text = obj.get(text_field)
        if isinstance(text, str) and text.strip():
            think = obj.get(think_field) if think_field else ""
            return (think if isinstance(think, str) else ""), text, ""
    return "", "", ("模型调用了交稿工具但参数里没有 %s ——多为中途被掐断或"
                    "工具参数格式被渠道改写" % text_field)


class ApiError(Exception):
    """API 调用错误，携带人类可读信息。"""
    def __init__(self, message: str, status_code: int | None = None, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class StopGeneration(Exception):
    """用户主动停止生成。"""


# ---------------- DPAPI 密钥加密（Windows） ----------------

def _dpapi_encrypt(data: bytes) -> bytes:
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    raise OSError("DPAPI 加密失败")


def _dpapi_decrypt(data: bytes) -> bytes:
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    raise OSError("DPAPI 解密失败")


def encrypt_key(plain: str) -> str:
    """加密 API Key → 密文 base64 串。非 Windows 平台退化为可逆混淆（仍不明文）。"""
    if not plain:
        return ""
    if sys.platform == "win32":
        try:
            return "d1:" + base64.b64encode(_dpapi_encrypt(plain.encode("utf-8"))).decode("ascii")
        except OSError:
            pass
    return "x1:" + base64.b64encode(plain.encode("utf-8")).decode("ascii")[::-1]


def decrypt_key(encrypted: str) -> str:
    """解密 API Key。失败返回空串（调用方按「未配置」处理）。

    注意：只打印失败原因，绝不打印密钥本身。
    """
    if not encrypted:
        return ""
    try:
        if encrypted.startswith("d1:"):
            if sys.platform == "win32":
                return _dpapi_decrypt(base64.b64decode(encrypted[3:])).decode("utf-8")
            # Windows DPAPI 加密的密钥换到别的系统/用户就读不出来
            print("[api] 该密钥由 Windows DPAPI 加密，当前系统无法解密（请重新填写）")
            return ""
        if encrypted.startswith("x1:"):
            return base64.b64decode(encrypted[3:][::-1]).decode("utf-8")
        if encrypted.startswith(("p3:", "k1:")):
            print("[api] 该密钥是备份包格式，需在本机重新保存一次才能使用")
            return ""
    except Exception as e:
        # 解密失败 ≠ 没配 key：此前静默返回空串，只会表现为「鉴权失败」掩盖根因
        print(f"[api] 密钥解密失败（可能换了机器或 Windows 用户）："
              f"{type(e).__name__}: {e}")
        return ""
    return ""


def mask_key(key: str) -> str:
    """显示用掩码。"""
    if len(key) <= 8:
        return "****"
    return key[:4] + "****" + key[-4:]


# ---------------- 客户端 ----------------

class LLMClient:
    """单个模型档案的调用客户端（无状态，每次调用新建或复用均可）。"""

    # 思考去重认定的最小重复长度（2026-10-07）：低于该长度的重复一律不剥。
    # 实测 GLM-5.3 长思考（3~6 万 tokens）的正常增量里，短片段与已累积文本
    # 「巧合重复」极常见（高频词如「生死状」「我们」「应该」），旧逻辑 b in a
    # 无长度门槛、首尾重叠 1 字就剥——正常流被静默吃字，长思考吃掉成百上千
    # 字，剩下的低频碎片拼出来就是「乱七八糟的字」。宁可放过病态渠道的小片
    # 重发（重复但内容全、可读），也不能吞正常渠道的内容。
    _MERGE_REPEAT_MIN = 6

    def __init__(self, profile: dict):
        self.profile = profile or {}
        self.base_url = (self.profile.get("base_url") or "").strip().rstrip("/")
        self.api_key = decrypt_key(self.profile.get("api_key") or "")
        self.model = self.profile.get("model") or ""
        self.timeout = int(self.profile.get("timeout") or 120)
        self._active_resp = None  # 当前流式响应（供 abort 中断阻塞读）
        self._aborted = False     # abort 标记：区分「主动中断」与「真网络错误」（决定是否重试）

    @staticmethod
    def _is_timeout(e) -> bool:
        """判断异常是否为读超时（心跳用）。urllib3 ReadTimeoutError 也继承 OSError，
        需用字符串兜底识别。"""
        if isinstance(e, (TimeoutError, requests.exceptions.Timeout)):
            return True
        try:
            return "timed out" in str(e).lower() or "timeout" in str(e).lower()
        except Exception:
            return False

    def abort(self):
        """中断当前挂起的网络请求（线程安全）。

        读取循环用 select 心跳轮询（1s），abort 只需置标志 + 关闭连接：
        连接关闭后 fd 立即变为可读（EOF/异常），select 立刻返回，循环随即退出。
        """
        self._aborted = True
        resp = self._active_resp
        if resp is None:
            return
        try:
            resp.close()
        except Exception:
            pass

    # -- 内部 --

    def _chat_url(self) -> str:
        return self.base_url + "/chat/completions"

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json",
             # 流式读取直读 resp.raw._fp 绕过了解压层：禁用压缩，
             # 防止渠道对 SSE 启用 gzip 时读到压缩字节（乱码/解析失败）
             "Accept-Encoding": "identity",
             # requests 默认 UA 是 python-requests/x.y，部分中转站前置 WAF（Cloudflare 等）
             # 会按 UA 直接 403 拦截（连 chat 都不通）；与 embedding 模块统一用自定义 UA
             "User-Agent": "JiuGuanNovelGenerator/1.0"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _effective_max_tokens(self, max_tokens: int | None) -> int | None:
        """解析本次输出上限：任务显式值优先，档案手动模式其次，自动模式留给渠道。"""
        if max_tokens is not None:
            try:
                value = int(max_tokens)
                return value if value > 0 else None
            except (TypeError, ValueError):
                return None
        if self.profile.get("max_output_mode", "auto") != "manual":
            return None
        try:
            value = int(self.profile.get("max_tokens") or 0)
            return value if value > 0 else None
        except (TypeError, ValueError):
            return None

    def _reasoning_parameters(self) -> dict:
        """按常见 OpenAI 兼容渠道生成思考参数。

        ``max`` = **不显式指定档位**，走渠道默认（渠道默认就是它允许的最高档）。
        这一条是 2026-09-23 实测改出来的，别再改回去。

        历史实现把 ``max`` 映射成标准枚举 ``high`` —— 那是没有依据的猜测，而且
        **会稳定触发拒答**：给 GLM-5.3 这类思考模型显式抬高思考档位后，它把思考
        预算花在「深度审查素材」上，拒答文案常见「吾有一梦…我无法执行此请求」
        这种「先走思考协议再否决」的形态；不发则回到渠道默认，稳定出正文。

        证据（`docs/酒馆破限对照排查SOP.md` §4.6：同一份酒馆原报文、同一域名、
        同一 key、同一 UA，交叉配对）：
          · 酒馆形态（不发 reasoning_effort）→ 出正文 **5/6**
          · 我方形态（reasoning_effort=high）→ 出正文 **0/6**
          · 逐项隔离：只加 ``reasoning_effort=high`` → **0/3**；
            只加 ``stream_options`` → 2/3；去掉 ``top_p`` → 2/3（后两者与对照 2/3 无差别）
        ``high``/``medium``/``low`` 仍显式下发，供确实要强制档位的场景；渠道不认该
        字段时，请求层仍会自动摘除并重发一次。
        """
        mode = str(self.profile.get("reasoning_mode", "max") or "max").lower()
        if mode not in {"max", "high", "medium", "low", "off"}:
            mode = "max"
        provider = f"{self.base_url} {self.model}".lower()
        uses_thinking = any(name in provider for name in (
            "deepseek", "dashscope", "aliyun", "bigmodel", "siliconflow"))
        # 智谱 GLM-5.x 走单独分支（2026-11-05 修）：官方支持
        # reasoning_effort=low/high/max（默认 max），但下面 uses_thinking 分支
        # 会把 high/medium/low 一律吞成 `thinking.enabled` —— 四个档位发出完全
        # 相同的参数，档案页的「思考强度」对智谱形同虚设，永远走最深档。
        #
        # ⚠ 实测（2026-11-05 二次实测，官方 glm-5.3，同一短任务 n=1/档）：
        #   max  → 思考 2294 tok / 5057 字，正文 652 字，42.3s
        #   high → 思考 **43 tok / 200 字**，正文 645 字，12.5s
        # 即智谱的 high 不是「想得少一点」，而是把思考压到几乎象征性（25 倍差距）。
        # 官方文档写 high=「增强推理」，实测与之不符，**以实测为准**。
        # 需要长程规划（情节铺排、伏笔埋设、跨章一致）的场景用 max；
        # 短任务（检查/改写/简单问答）才考虑 high。`max` 仍保持「什么都不传」走渠道
        # 默认，这条红线不变（曾把 max 映射成 high → GLM-5.3 稳定拒答）。
        zhipu = ("bigmodel" in provider) or ("glm-5" in provider)
        if mode == "off":
            if uses_thinking:
                if "glm-5.3" in self.model.lower():
                    # GLM-5.3 系列**强制思考**：官方文档明确「不再支持 disabled，
                    # 传入 disabled 会导致 API 请求失败」（阿里云百炼同文）。
                    # 选「关闭」时降到最低档 low，至少不 400——真关不掉。
                    return {"thinking": {"type": "enabled"},
                            "reasoning_effort": "low"}
                return {"thinking": {"type": "disabled"}}
            return {}
        if mode == "max":
            # 不显式指定档位：渠道默认即「它允许的最高思考级别」
            return {"thinking": {"type": "enabled"}} if uses_thinking else {}
        level = {"high": "high", "medium": "medium", "low": "low"}[mode]
        if zhipu:
            # 智谱只认 low/high/max（没有 medium）→ medium 就近取 high
            return {"thinking": {"type": "enabled"},
                    "reasoning_effort": "high" if level == "medium" else level}
        if uses_thinking:
            return {"thinking": {"type": "enabled"}}
        return {"reasoning_effort": level}

    def _payload(self, messages: list[dict], temperature: float | None,
                 max_tokens: int | None, stream: bool,
                 include_reasoning: bool = True,
                 tools: list[dict] | None = None,
                 tool_choice=None) -> dict:
        p = {"model": self.model, "messages": messages, "stream": stream}
        if temperature is not None:
            p["temperature"] = temperature
        effective_max = self._effective_max_tokens(max_tokens)
        if effective_max is not None:
            p["max_tokens"] = effective_max
        if stream:
            # 流式也要真实用量：OpenAI 兼容标准扩展，终止帧附带 usage。
            # 不认该字段的渠道多数静默忽略（个别 422 → complete 里按
            # 参数不支持摘除后重发，与 reasoning 参数同一处理路径）。
            p["stream_options"] = {"include_usage": True}
        if tools:
            p["tools"] = tools
            if tool_choice is not None:
                p["tool_choice"] = tool_choice
            # 工具交稿模式**不发**任何思考档位参数（2026-11-21，酒馆实测口径）：
            # 小猫之神预设的 FinalRequestProcessing 明确 `delete request.reasoning_effort`，
            # 整条报文的权重靠伪造工具历史，不靠推高思考档位；而按项目 SOP §4.6，
            # 显式抬高档位（尤其 max→high）会让部分模型把预算花在「先审查素材」
            # 上，稳定拒答。工具模式下不发送 = 走渠道默认，两边都安全。
        elif include_reasoning:
            p.update(self._reasoning_parameters())
        return p

    @staticmethod
    def _merge_thinking(a: str, b: str) -> str:
        """合并两处来源的思考文本，避免同一段思考被重复表达导致翻倍。

        部分渠道会把同一段思考同时放在结构化 content 块和顶层
        reasoning_content/thinking 字段，两处取一份即可。

        流式增量场景：病态渠道会把同一段思考重发成「尾部与下一片头部重叠」的
        小片（如 "好的" → "好的用户" → "的用户要求"），不去重就是整段翻倍。
        但正常渠道（OpenAI 兼容/GLM/Anthropic 官方流）的增量是零重叠干净衔接的，
        自然语言里「新片开头恰好等于已累积尾部/已出现短语」的巧合极多——
        去重判据必须够大才不误伤（2026-10-07 实测：GLM-5.3 三万字长思考被
        旧逻辑吃掉成百上千字，剩下的低频碎片拼出来就是乱码）。因此完全包含
        与首尾重叠都只认 ≥6 字的重复，小片巧合重复一律放行（重复可读、
        内容全 > 静默吃字）。窗口上限 64 字。
        """
        if not a:
            return b
        if not b:
            return a
        if a == b:
            return a
        if len(b) >= LLMClient._MERGE_REPEAT_MIN and b in a:
            return a
        if a in b:
            return b
        limit = min(len(a), len(b), 64)
        for n in range(limit, LLMClient._MERGE_REPEAT_MIN - 1, -1):
            if a[-n:] == b[:n]:
                return a + b[n:]
        return a + b

    @classmethod
    def _append_thinking(cls, parts: list[str], delta: str) -> str:
        """把一片思考增量并入累积列表，返回本次真正新增的部分（供 on_thinking 回调）。

        流式思考是跨 SSE 事件累积的：每个事件的增量都要与「已累积的尾部」做去重，
        否则渠道重发重叠片段时（思考吐得越快分片越碎）会整段翻倍——界面上就是
        「的地方的地方」「1. 拍 1. 拍」。只与最后一片比较即可（重叠只可能发生在衔接处），
        避免每次都把整段思考重扫一遍。
        """
        if not delta:
            return ""
        if not parts:
            parts.append(delta)
            return delta
        merged = cls._merge_thinking(parts[-1], delta)
        if merged == parts[-1]:
            return ""          # 该片完全被已累积内容覆盖：纯重复，丢弃
        added = merged[len(parts[-1]):]
        parts[-1] = merged
        return added

    @staticmethod
    def _block_parts(value) -> tuple[str, str]:
        """把字符串或结构化 content 拆成（正文，思考）。"""
        if isinstance(value, str):
            return value, ""
        if not isinstance(value, list):
            return "", ""
        text_parts, thinking = [], ""
        thinking_types = {"thinking", "reasoning", "reasoning_content"}
        for part in value:
            if isinstance(part, str):
                text_parts.append(part)
                continue
            if not isinstance(part, dict):
                continue
            kind = str(part.get("type") or "text").lower()
            value_text = part.get("text")
            if not isinstance(value_text, str):
                value_text = part.get("thinking") or part.get("reasoning")
            if not isinstance(value_text, str):
                value_text = part.get("content") if isinstance(part.get("content"), str) else ""
            if not value_text:
                continue
            if kind in thinking_types or "think" in kind or "reason" in kind:
                thinking = LLMClient._merge_thinking(thinking, value_text)
            else:
                text_parts.append(value_text)
        return "".join(text_parts), thinking

    @classmethod
    def _container_parts(cls, container: dict | None) -> tuple[str, str]:
        """读取 message/delta 中常见的正文与思考字段。

        防重复：同一段思考可能同时出现在结构化 content 块和顶层
        reasoning_content/thinking 字段，两处只取一份。
        """
        if not isinstance(container, dict):
            return "", ""
        text, thinking = cls._block_parts(container.get("content"))
        alt = ""
        for key in ("reasoning_content", "reasoning", "thinking_content", "reasoning_text"):
            value = container.get(key)
            if isinstance(value, str):
                alt += value
        value = container.get("thinking")
        if isinstance(value, str):
            alt += value
        return text, cls._merge_thinking(thinking, alt)

    @classmethod
    def _response_parts(cls, obj: dict) -> tuple[str, str]:
        """解析非流式响应，兼容 reasoning_content、thinking 和结构化 content。"""
        choices = obj.get("choices") or [] if isinstance(obj, dict) else []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = choice.get("message") or choice.get("delta") or {}
        text, thinking = cls._container_parts(message)
        if isinstance(choice, dict):
            extra_text, extra_thinking = cls._container_parts(choice)
            text += extra_text
            thinking = cls._merge_thinking(thinking, extra_thinking)
        return text, thinking

    @staticmethod
    def _decode_sse_line(raw: bytes) -> str:
        """SSE 行解码：严格 UTF-8 优先，坏字节分级救援。

        个别坏字节（转发链把字符切坏）：保留 U+FFFD 可见标记——数据已丢，
        瞎猜比留标记更糟。大面积坏字节（渠道按 GBK 编码整行）：尝试 GB18030
        整行救援，仅在完全干净时采用，否则退回标记。
        """
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            pass
        replaced = raw.decode("utf-8", "replace")
        if replaced.count("�") <= 3:
            return replaced
        try:
            rescued = raw.decode("gb18030")
            if "�" not in rescued:
                return rescued
        except UnicodeDecodeError:
            pass
        return replaced

    @classmethod
    def _parse_sse_events(cls, line: str) -> list[tuple[str, str]]:
        """解析 SSE 行，返回 ``[(content|thinking|done, text)]`` 事件。"""
        line = line.strip()
        if not line.startswith("data:"):
            return []
        data = line[5:].strip()
        if data == "[DONE]":
            return [("done", "")]
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            return []
        if not isinstance(obj, dict):
            return []
        events: list[tuple[str, str]] = []

        def add(kind: str, value):
            if isinstance(value, str) and value:
                events.append((kind, value))

        # 终止帧的 usage（stream_options.include_usage 开启时，OpenAI 兼容渠道
        # 在 [DONE] 前发一帧 choices 为空、只带 usage 的 chunk）：提为 usage 事件，
        # 调用方据此把真实 prompt/completion tokens 记进日志。
        usage = obj.get("usage")
        if isinstance(usage, dict) and usage:
            pt = int(usage.get("prompt_tokens") or 0)
            ct = int(usage.get("completion_tokens") or 0)
            # 思考 token（2026-11-05）：GLM-5.3 的思考不可关闭、默认 reasoning_effort=max，
            # 思考 tokens **计入 completion_tokens 也占 max_tokens 预算**（官方文档：
            # 深度思考的思考过程计入输出 Token 计费）。不单独记下来的话，
            # 「正文才 3000 字怎么就停了」永远看不出真实原因是思考吃光了预算。
            details = usage.get("completion_tokens_details")
            rt = 0
            if isinstance(details, dict):
                try:
                    rt = int(details.get("reasoning_tokens") or 0)
                except (TypeError, ValueError):
                    rt = 0
            if pt or ct or rt:
                events.append(("usage", f"{pt}/{ct}/{rt}"))

        # 平台内容安全审核明细（2026-11-05）：智谱 V4 在拦截帧里带
        # `content_filter: [{"role":"assistant","level":1}]`；部分 OpenAI 兼容渠道
        # 只给这个字段、不给 finish_reason。必须独立识别——否则输出被审核掐断时
        # 我们只看到「流正常结束」，把它当成功收下（本次故障的根因）。
        cf = obj.get("content_filter")
        if isinstance(cf, list) and cf:
            events.append(("safety", "content_filter"))

        # Anthropic 原生：thinking_delta 使用 thinking 字段，text_delta 使用 text。
        if obj.get("type") == "content_block_delta":
            delta = obj.get("delta") or {}
            delta_type = str(delta.get("type") or "") if isinstance(delta, dict) else ""
            if "think" in delta_type or "reason" in delta_type:
                add("thinking", delta.get("thinking") or delta.get("text"))
            elif isinstance(delta, dict):
                add("content", delta.get("text"))

        if obj.get("type") == "content_block_start":
            block = obj.get("content_block") or {}
            text, thinking = cls._container_parts(block)
            add("thinking", thinking)
            add("content", text)

        for choice in obj.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            # finish_reason：最后一个增量 chunk 里会带（stop/length/...），length=被输出上限截断
            fr = choice.get("finish_reason")
            if isinstance(fr, str) and fr:
                events.append(("finish", fr))
            text, thinking = "", ""
            # delta 与 message 二选一：部分渠道首帧带 message、后续带 delta，
            # 或同一帧两者并存，无条件遍历会把同一段内容收两遍（思考翻倍的主因之二）。
            # 优先 delta（流式增量语义），仅当 delta 为空时才回退 message。
            container = choice.get("delta") or choice.get("message") or {}
            if isinstance(container, dict):
                text, thinking = cls._container_parts(container)
                # 工具交稿：tool_calls 是**增量片段**（同 index 反复追加 arguments），
                # 统一序列化成事件交给 complete 按 index 累积。非流式的 message 也走
                # 这里（容器二选一），两种情况的下游处理因此完全一致。
                tc = container.get("tool_calls")
                if isinstance(tc, list) and tc:
                    events.append(("tool_delta", json.dumps(tc, ensure_ascii=False)))
            add("thinking", thinking)
            add("content", text)
        return events

    @classmethod
    def _parse_sse(cls, line: str) -> str | None:
        """兼容旧调用方：只返回 SSE 中的正文增量，思考事件会被跳过。"""
        events = cls._parse_sse_events(line)
        for kind, text in events:
            if kind == "done":
                return ""
            if kind == "content":
                return text
        return None

    @staticmethod
    def _reasoning_unsupported(status: int, body: str) -> bool:
        if status not in (400, 404, 405, 422):
            return False
        text = (body or "").lower()
        markers = ("reasoning", "thinking", "unsupported", "unknown parameter",
                   "unrecognized parameter", "not permitted", "不支持", "未知参数")
        return any(marker in text for marker in markers)

    @staticmethod
    def _max_tokens_unsupported(status: int, body: str) -> bool:
        """渠道拒绝 max_tokens 过大（超过模型最大输出）→ 需要降级重发。

        只认输出上限相关错误（400/422 参数校验类），不认上下文超长
        （那是输入问题，降输出上限无用）。
        """
        if status not in (400, 422):
            return False
        text = (body or "").lower()
        markers = ("max_tokens", "maximum output", "max output", "output token",
                   "token limit", "too many tokens", "最大输出", "输出上限",
                   "超出模型", "超过模型", "tokens 超限", "token 数超出")
        return any(marker in text for marker in markers)

    @staticmethod
    def _tools_unsupported(status: int, body: str) -> bool:
        """渠道拒绝 tools / tool_choice 字段（工具交稿模式专用降级判据）。

        只认参数校验类状态码，不认 401/403（那是额度/认证）与 5xx（上游故障）——
        把那些当成「不支持工具」会导致白白降级掉破甲形态。
        """
        if status not in (400, 404, 405, 422):
            return False
        text = (body or "").lower()
        markers = ("tool_choice", "tools", "tool_calls", "tool call",
                   "function call", "function_call", "不支持", "未知参数",
                   "unrecognized", "unsupported", "unknown parameter")
        return any(marker in text for marker in markers)

    # -- 主调用 --

    def complete(self, messages: list[dict], temperature: float | None = None,
                 max_tokens: int | None = None, stream: bool = True,
                 on_chunk: Callable | None = None, stop_event: threading.Event | None = None,
                 retries: int = 1, on_thinking: Callable | None = None,
                 _include_reasoning: bool = True,
                 _read_timeout: int | None = None,
                 tools: list[dict] | None = None,
                 tool_choice=None, tool_reply: dict | None = None,
                 _tool_choice_override=None, _drop_tools: bool = False) -> dict:
        """调用模型，返回正文、思考内容和用量信息。

        - stream=True 且给 on_chunk：逐块回调增量文本
        - on_thinking：独立接收思考增量，不会混入正文
        - stop_event 被设置：中断并返回已生成部分（status='stopped'）
        - 网络异常自动重试 retries 次
        - _read_timeout：内部参数，覆盖档案超时（仅供流式空响应降级非流式时
          放宽读超时用——非流式要等整个生成完成，120 秒默认值必死）
        - tools / tool_choice：破甲档案声明的工具集（工具交稿模式）。给了就真发，
          并自动接收 tool_calls——**思考与正文从工具参数字段里拆回来**，上层看到的
          `text`/`thinking` 与普通模式完全一样（见 extract_tool_reply 注释）。
        - tool_reply：{"name","think_field","text_field"}，交稿字段映射。
        - _tool_choice_override / _drop_tools：内部降级用（渠道不认 required 或
          干脆不认 tools 时逐级退让，见下方 400 分支）。
        """
        if not self.model:
            raise ApiError("模型档案未填写模型名（model）")
        if not self.base_url:
            raise ApiError("模型档案未填写接口地址（base_url）")
        if _drop_tools:
            tools, tool_choice = None, None
        elif _tool_choice_override is not None:
            tool_choice = _tool_choice_override
        if not tools:
            tool_reply = None
        url = self._chat_url()
        payload = self._payload(messages, temperature, max_tokens, stream,
                                include_reasoning=_include_reasoning,
                                tools=tools, tool_choice=tool_choice)
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        # 工具交稿（2026-11-21）：按 index 累积 tool_calls 的 id/name/arguments。
        # 同一段 arguments 会被渠道切成很多片、反复以同 index 送来，只能追加不能覆盖。
        tool_calls: dict[int, dict] = {}
        arg_stream = (_ToolArgStream(str((tool_reply or {}).get("text_field") or ""))
                      if tool_reply else None)
        tool_error = ""
        prompt_tokens = completion_tokens = 0
        truncated = False
        last_error = None
        # 本次请求的终止原因（2026-11-05）：只认 length 时，「内容审核掐断」
        # （GLM 的 sensitive）和「模型自己收尾」（stop）在下游完全无法区分，
        # 只能按末尾标点猜 → 把平台审核说成「破限没生效」，误导处置方向。
        finish_reason = ""
        safety_blocked = False
        # 思考占用的输出 token（来自 usage.completion_tokens_details.reasoning_tokens）。
        # 与 completion_tokens 是包含关系：思考越多，留给正文的预算越少。
        reasoning_tokens = 0
        # 本次失败是否「网络/中断类且 0 字节」：这种失败换非流式重发也救不回
        # （掐线是渠道网关干的，非流式响应更长只会再被掐；读超时说明渠道慢，
        # 非流式要等更久）。立刻重发纯属白烧配额——免费站限流按次数计且失败
        # 也计入。置位的场景：流式读循环抛网络异常（IncompleteRead 等）、
        # requests 层网络异常（超时/连接失败）、abort 中断。渠道不支持流式
        # （正常 EOF 但 0 字节）不置位，仍走非流式降级。
        zero_byte_network_failure = False
        t0 = time.time()

        def _ret(**kw) -> dict:
            """统一出口：所有返回分支都带上终止原因与审核标记。

            此前每个 return 各写一份字段，新增字段必然漏掉某几支（abrupt 就只在
            正常 EOF 分支出现过）。集中一处后，下游拿到的形态恒定。

            工具交稿的归一化也在这里做：流式阶段 text_parts 收的是「半截 JSON 里
            已能确定的正文」（供界面边写边显示），收尾时用完整参数覆盖一次，
            避免半截提取与完整提取在转义边界上的细微差异留下脏字符。
            """
            nonlocal tool_error
            if tool_reply and tool_calls:
                th, tx, terr = extract_tool_reply(tool_calls, tool_reply)
                if tx:
                    text_parts[:] = [tx]
                    if th:
                        thinking_parts[:] = [th]
                    tool_error = ""
                elif terr:
                    tool_error = terr
            out = {"text": "".join(text_parts),
                   "thinking": "".join(thinking_parts),
                   "prompt_tokens": prompt_tokens,
                   "completion_tokens": completion_tokens,
                   "truncated": truncated,
                   "finish_reason": finish_reason,
                   "safety_blocked": safety_blocked,
                   "reasoning_tokens": reasoning_tokens,
                   "duration_ms": int((time.time() - t0) * 1000),
                   "tool_error": tool_error,
                   "tool_name": next((str(s.get("name") or "")
                                      for s in tool_calls.values()
                                      if s.get("name")), "")}
            out.update(kw)
            return out

        for attempt in range(retries + 1):
            self._aborted = False
            zero_byte_network_failure = False
            # 每次重试清空累积缓冲：网络中断重试时，上一次已收到的文本/思考仍留在
            # text_parts / thinking_parts 里，会原样拼在重试结果前面（内容翻倍）。
            # 已有正文的错误分支在循环内直接 return（不再重试），故这里清空不会丢成果。
            if attempt > 0:
                text_parts.clear()
                thinking_parts.clear()
                tool_calls.clear()
                tool_error = ""
                if arg_stream is not None:
                    arg_stream = _ToolArgStream(arg_stream.field)
                completion_tokens = 0
                truncated = False
                finish_reason = ""
                safety_blocked = False
                reasoning_tokens = 0
            try:
                # 渠道节流闸（2026-09-19）：档案配了「每分钟请求上限」时按窗口排队，
                # 避免失败补救（上游拦截变体重发）与网络重试连发把渠道配额一次打光
                # ——实测渠道把「1 分钟 N 次」当硬限制且失败次数也计入
                # （详见 core/throttle.py）。未配上限（rpm=0）时零等待，行为不变。
                _throttle_acquire(
                    self.profile,
                    should_stop=lambda: bool(
                        self._aborted
                        or (stop_event is not None and stop_event.is_set())))
                if self._aborted or (stop_event is not None and stop_event.is_set()):
                    # 等配额期间用户点了停止：一个字节都没发出去，按「已停止」返回
                    return _ret(ok=True, status="stopped", error="")
                with _request_with_proxy_fallback(
                        requests.post, url, headers=self._headers(), json=payload,
                        stream=stream,
                        timeout=(15, min(_read_timeout or self.timeout, 600))) as resp:
                    # SSE 事实标准为 UTF-8（text/event-stream 无 charset 时 requests 会猜错）
                    resp.encoding = "utf-8"
                    if resp.status_code != 200:
                        body = resp.text[:2000]
                        if (_include_reasoning and
                                self._reasoning_unsupported(resp.status_code, body)):
                            # 未知兼容渠道常把思考参数当成非法字段；去掉后重发一次，
                            # 保证正文生成不被渠道能力差异阻断。
                            # tools 必须原样带上：漏传就等于静默退回纯文本模式，
                            # 模型不调工具 → 交稿字段永远拿不到。
                            return self.complete(
                                messages, temperature=temperature, max_tokens=max_tokens,
                                stream=stream, on_chunk=on_chunk, stop_event=stop_event,
                                retries=retries, on_thinking=on_thinking,
                                _include_reasoning=False,
                                tools=tools, tool_choice=tool_choice,
                                tool_reply=tool_reply)
                        if tools and self._tools_unsupported(resp.status_code, body):
                            # 渠道不认 tools / tool_choice：逐级退让——
                            # ① required → auto（多数渠道只认 auto/none 两个枚举）
                            # ② 还失败 → 摘掉 tools，退回纯文本交稿。
                            # 退到纯文本就不再是预设那套形态了（权重会掉），
                            # 所以只在渠道确实拒绝时才走，且按「text 有内容就算成功」
                            # 兜底：那时 tool_calls 为空，_ret 不会去拆字段。
                            drop = str(tool_choice) != "required"
                            return self.complete(
                                messages, temperature=temperature, max_tokens=max_tokens,
                                stream=stream, on_chunk=on_chunk, stop_event=stop_event,
                                retries=retries, on_thinking=on_thinking,
                                _include_reasoning=_include_reasoning,
                                tools=tools, tool_choice=tool_choice,
                                tool_reply=tool_reply,
                                _tool_choice_override=None if drop else "auto",
                                _drop_tools=drop)
                        mt = payload.get("max_tokens")
                        if mt and self._max_tokens_unsupported(resp.status_code, body):
                            # 输出上限超模型最大输出 → 减半重发（递归直到渠道可接受）。
                            # 减半后低于地板（8192）时不再继续减半，改为**不带 max_tokens**
                            # 走渠道默认——一路减半到极小值会让输出上限反而更小，正文更容易
                            # 被截断（渠道默认常是 2048/4096，比显式大值的一半还大）。
                            # mt 自身已低于地板时放弃重试，直接报错（防无限递归）。
                            halved = int(mt) // 2
                            next_mt = (halved if halved >= _MAX_TOKENS_DOWNGRADE_FLOOR
                                       else None)
                            if next_mt is not None or int(mt) > _MAX_TOKENS_DOWNGRADE_FLOOR:
                                return self.complete(
                                    messages, temperature=temperature,
                                    max_tokens=next_mt,
                                    stream=stream, on_chunk=on_chunk, stop_event=stop_event,
                                    retries=retries, on_thinking=on_thinking,
                                    _include_reasoning=_include_reasoning,
                                    tools=tools, tool_choice=tool_choice,
                                    tool_reply=tool_reply)
                        raise ApiError(f"HTTP {resp.status_code}：{body}", resp.status_code, body)
                    if not stream:
                        # 非流式同样登记活动响应：abort 才能中断挂起的 json() 阻塞读
                        self._active_resp = resp
                        try:
                            obj = resp.json()
                        finally:
                            self._active_resp = None
                        content, thinking = self._response_parts(obj)
                        # 工具交稿：非流式响应里 tool_calls 是完整的一条，直接登记
                        # （流式与它共用 _ret 里的 extract_tool_reply 归一化）。
                        if tool_reply:
                            choices0 = obj.get("choices") or []
                            msg0 = (choices0[0].get("message") or {}) \
                                if choices0 and isinstance(choices0[0], dict) else {}
                            for i, item in enumerate(msg0.get("tool_calls") or []):
                                if not isinstance(item, dict):
                                    continue
                                fn = item.get("function") if isinstance(
                                    item.get("function"), dict) else {}
                                tool_calls[i] = {
                                    "id": str(item.get("id") or ""),
                                    "name": str(fn.get("name") or ""),
                                    "args": str(fn.get("arguments") or ""),
                                }
                        if thinking:
                            thinking_parts.append(thinking)
                            if on_thinking:
                                on_thinking(thinking)
                        if content:
                            text_parts.append(content)
                            if on_chunk:
                                on_chunk(content)
                        usage = obj.get("usage") or {}
                        choices = obj.get("choices") or []
                        first = choices[0] if choices and isinstance(choices[0], dict) else {}
                        # 非流式同样要分判终止原因：此前只认 length，
                        # finish_reason=sensitive（GLM 官方内容审核）被当成正常完成。
                        finish_reason, is_len, safety_blocked = classify_finish(
                            first.get("finish_reason") or "")
                        truncated = is_len
                        if obj.get("content_filter"):
                            safety_blocked = True
                            finish_reason = finish_reason or "content_filter"
                        details = usage.get("completion_tokens_details")
                        if isinstance(details, dict):
                            try:
                                reasoning_tokens = int(details.get("reasoning_tokens") or 0)
                            except (TypeError, ValueError):
                                reasoning_tokens = 0
                        return _ret(
                            ok=True, status="ok", error="",
                            prompt_tokens=int(usage.get("prompt_tokens", 0)),
                            completion_tokens=int(usage.get("completion_tokens", 0)),
                        )
                    # 流式 SSE：select 心跳轮询 + read1 分块读取。
                    # Windows 上无法从另一线程可靠中断阻塞中的 recv（close 不唤醒、
                    # 超时还会毒化 socket 对象不可再读），改用 select 短超时轮询 fd 可读性：
                    # 每次心跳检查停止/abort 标志，abort 后延迟 ≤1s；fd 可读时用 read1
                    # 单次读取（流式不攒块，chunked 解码由 http.client 层完成）。
                    saw_data = False
                    got_finish = False  # 收到过结束标记（[DONE] 或 finish_reason）
                    self._active_resp = resp
                    buf = b""
                    outcome = "ok"
                    done = False

                    def _consume(evs) -> bool:
                        """消化一批 SSE 事件，返回是否收到 [DONE]。

                        完整行与 EOF 残行共用同一套逻辑——此前两处各写一遍，
                        EOF 那份因为 break 提前跳出循环而成了永不执行的死代码。
                        """
                        nonlocal saw_data, got_finish, truncated, prompt_tokens
                        nonlocal completion_tokens, done
                        nonlocal finish_reason, safety_blocked, reasoning_tokens
                        for kind, delta in evs:
                            if kind == "done":
                                saw_data = True
                                got_finish = True
                                done = True
                                return True
                            if kind == "finish":
                                got_finish = True
                                fr, is_len, is_safety = classify_finish(delta)
                                if fr:
                                    finish_reason = fr
                                if is_len:
                                    truncated = True  # 被输出上限截断
                                elif is_safety:
                                    # 平台内容审核掐断（GLM 官方 = sensitive）：
                                    # 已生成部分保留，但**不是**模型自己收尾，
                                    # 也不该按「破限没生效」处置（见常量注释）。
                                    safety_blocked = True
                                continue
                            if kind == "safety":
                                # 只有 content_filter 字段、没有 finish_reason 的渠道
                                saw_data = True
                                got_finish = True
                                safety_blocked = True
                                if not finish_reason:
                                    finish_reason = "content_filter"
                                continue
                            if kind == "tool_delta":
                                # 工具交稿：按 index 累积 id/name/arguments。
                                # arguments 是**分片**的——同 index 会反复来，
                                # 只有 id/name 可能在首片出现、之后只给 arguments。
                                saw_data = True
                                try:
                                    arr = json.loads(delta)
                                except json.JSONDecodeError:
                                    arr = []
                                for item in arr:
                                    if not isinstance(item, dict):
                                        continue
                                    slot_idx = item.get("index")
                                    if not isinstance(slot_idx, int):
                                        slot_idx = 0
                                    slot = tool_calls.setdefault(
                                        slot_idx, {"id": "", "name": "", "args": ""})
                                    if isinstance(item.get("id"), str) and item["id"]:
                                        slot["id"] = item["id"]
                                    fn = item.get("function")
                                    if not isinstance(fn, dict):
                                        continue
                                    if isinstance(fn.get("name"), str) and fn["name"]:
                                        slot["name"] = fn["name"]
                                    piece = fn.get("arguments")
                                    if isinstance(piece, str) and piece:
                                        slot["args"] += piece
                                        completion_tokens += max(1, len(piece))
                                        # 正文边写边显示：从半截 JSON 里取已能确定
                                        # 的字段值，只回吐新增部分。
                                        if arg_stream is not None:
                                            new_text = arg_stream.feed(piece)
                                            if new_text:
                                                text_parts.append(new_text)
                                                if on_chunk:
                                                    on_chunk(new_text)
                                continue
                            if kind == "usage":
                                # include_usage 终止帧：真实用量覆盖逐字估算值。
                                # 格式 pt/ct/rt（rt=思考 token；旧格式只有两段，兼容）。
                                parts = delta.split("/")
                                try:
                                    prompt_tokens = int(parts[0])
                                    completion_tokens = int(parts[1])
                                    if len(parts) > 2:
                                        reasoning_tokens = int(parts[2])
                                except (ValueError, IndexError):
                                    pass
                                continue
                            if kind == "thinking":
                                saw_data = True
                                # 跨事件累积必须走 _merge_thinking：渠道重发重叠
                                # 小片时（思考吐得快），裸 append 会把同一段拼两遍。
                                emitted = self._append_thinking(
                                    thinking_parts, delta)
                                if on_thinking and emitted:
                                    on_thinking(emitted)
                                continue
                            if kind == "content" and delta:
                                saw_data = True
                                text_parts.append(delta)
                                if on_chunk:
                                    on_chunk(delta)
                                completion_tokens += max(1, len(delta))
                        return False
                    try:
                        fp = resp.raw._fp  # http.client.HTTPResponse（read1 走其解码逻辑）
                        fd = None
                        try:
                            buff = getattr(fp, "fp", None)  # BufferedReader
                            if buff is not None:
                                fd = buff.fileno()
                        except Exception:
                            fd = None
                        while True:
                            if stop_event is not None and stop_event.is_set():
                                outcome = "stopped"
                                break
                            if self._aborted:
                                outcome = "stopped"
                                break
                            if fd is not None:
                                try:
                                    ready, _, _ = select.select([fd], [], [], 1.0)
                                except (OSError, ValueError):
                                    ready = [fd]  # fd 探测失败：直接读，异常走下方处理
                            else:
                                ready = [1]  # 拿不到 fd（极端情况）：直接读，阻塞由网络超时兜底
                            if not ready:
                                continue  # 心跳：本次无数据，回到循环顶检查停止/abort
                            try:
                                chunk = fp.read1(4096)
                            except Exception as e:
                                if self._is_timeout(e):
                                    continue
                                if self._aborted or (stop_event is not None and stop_event.is_set()):
                                    outcome = "stopped"
                                else:
                                    outcome = "error"
                                    last_error = f"网络错误：{e}"
                                break
                            if not chunk:
                                break  # EOF：连接正常结束（abort 关闭连接也走这里）
                            buf += chunk
                            done = False
                            # 只消化「读到了换行符」的完整行；没读到换行的残行必须
                            # 留在 buf 里等下一块补齐。
                            # （2026-09-09 修复：此前每轮读完都无条件 buf = b""，
                            #  SSE 事件常被 4096 字节的读取边界切成两半——前半段
                            #  JSON 不完整解析失败被静静丢掉，后半段丢了 "data:"
                            #  前缀也解析不了，整条事件永久消失，界面上就是漏字；
                            #  切在多字节汉字中间时还会产出 U+FFFD 乱码。）
                            while b"\n" in buf:
                                line, buf = buf.split(b"\n", 1)
                                if _consume(self._parse_sse_events(
                                        self._decode_sse_line(line))):
                                    break
                            if done:
                                break
                        # EOF 后仍留在 buf 里的残行（服务端最后一个事件没带结尾换行）：
                        # 补解析一次，否则尾部增量被丢弃。
                        if outcome == "ok" and buf.strip():
                            _consume(self._parse_sse_events(
                                self._decode_sse_line(buf)))
                    finally:
                        self._active_resp = None
                    if outcome == "stopped":
                        return _ret(ok=True, status="stopped", error="")
                    if self._aborted or (stop_event is not None and stop_event.is_set()):
                        # abort 关闭连接导致 EOF 退出：同样按「已停止」处理
                        return _ret(ok=True, status="stopped", error="")
                    if outcome == "error":
                        if text_parts:
                            # 网络中断但已有部分文本：保留已生成内容，不丢弃
                            return _ret(ok=True, status="stopped",
                                        error=last_error or "网络中断")
                        zero_byte_network_failure = True
                        break  # 无任何内容：交给循环外统一处理（重试/raise）
                    # 请求成功但一个字都没解析出来：多半是该接口不支持/未开启流式，
                    # 或服务方返回了我们没适配的格式。跳出循环去降级非流式（不占网络重试次数）。
                    if not text_parts and not saw_data:
                        break
                    return _ret(
                        ok=True, status="ok", error="",
                        # 没收到 [DONE]/finish_reason 就 EOF → 连接被服务端/网关中途掐断
                        # （常见：渠道 100 秒超时限制、网络波动），正文多半没写完
                        abrupt=bool(text_parts and not got_finish),
                    )
            except StopGeneration:
                raise
            except (requests.RequestException, requests.ConnectionError, TimeoutError) as e:
                if stop_event is not None and stop_event.is_set():
                    # 用户主动停止（停止按钮/关窗）：保留已生成部分，不重试、不降级
                    return _ret(ok=True, status="stopped", error="")
                if self._aborted:
                    last_error = "已中断"  # abort 关闭连接（无 stop_event，如 run_busy 关进度框）：不重试、不降级
                    zero_byte_network_failure = True
                    break
                # requests 层网络异常（等响应头超时/连接失败）发生时必然一个字都没
                # 收到——同样不值得降级非流式重发（见 zero_byte_network_failure 注释）
                last_error = f"网络错误：{e}"
                zero_byte_network_failure = True
                if attempt < retries:
                    time.sleep(1.0)
                    continue
                break
            except ApiError:
                raise
            except Exception as e:  # 其他解析错误
                # abort 关闭读取缓冲后 iter_lines/resp.json 可能抛 ValueError/OSError 等
                # 任意异常：这属于主动中断而非解析失败，返回已生成部分
                if self._aborted or (stop_event is not None and stop_event.is_set()):
                    return _ret(ok=True, status="stopped", error="")
                raise ApiError(f"响应解析失败：{e}")
        # 流式拿到了连接却 0 字 → 自动降级为非流式再试一次（独立于网络重试，retries=0 也生效）。
        # 不降级的三种情况：①用户停止/abort（再发一次只会重新挂起，非流式路径无法被 abort 中断）；
        # ②网络/中断类 0 字节失败（掐线是渠道网关干的、读超时说明渠道慢——非流式响应更长，
        # 重发只会再死一次还白烧配额，免费站限流按次数计且失败也计入；上层 30 秒后重试更划算）。
        # 降级时读超时放宽（档案超时 ×3，至少 300 秒）：非流式必须等整个生成完成，
        # 沿用 120 秒默认值的话长思考模型必死（2026-10-23 实测：降级请求
        # 100% 死于同款读超时，等于每次掐线多烧一次配额）。
        if (stream and not text_parts and not thinking_parts
                and not zero_byte_network_failure
                and not safety_blocked
                and not self._aborted
                and not (stop_event is not None and stop_event.is_set())):
            return self.complete(messages, temperature=temperature, max_tokens=max_tokens,
                                 stream=False, on_chunk=on_chunk, stop_event=stop_event,
                                 retries=0, on_thinking=on_thinking,
                                 _include_reasoning=_include_reasoning,
                                 _read_timeout=min(max(self.timeout * 3, 300), 600),
                                 tools=tools, tool_choice=tool_choice,
                                 tool_reply=tool_reply)
        msg = last_error or "未知错误"
        if "(read timeout=" in msg:
            # 读超时九成是思考模型等首字节超档案超时（等头阶段无心跳无重试放大），
            # 报错里直接给解法，省得老板当成「自己网络断了」
            msg += "。长思考模型常思考几分钟才吐第一个字，可在模型档案里调大「超时」（建议 300~600 秒）。"
        raise ApiError(msg)

    def list_models(self) -> dict:
        """拉取服务方的模型列表（OpenAI 兼容 GET /models），供档案编辑时选择模型。

        返回 {ok, models: [{"id": str, "context_length": int|None}], message}。
        兼容常见返回格式：{"data": [...]}（OpenAI 标准）、{"models": [...]}、纯数组；
        条目可以是字符串或含 id/name 的对象。部分服务方（如 OpenRouter）会附带
        context_length/context_window，一并提取。失败不抛异常，ok=False + message。
        """
        if not self.base_url:
            return {"ok": False, "models": [], "message": "请先填写接口地址（base_url）"}
        try:
            with _request_with_proxy_fallback(
                    requests.get, self.base_url + "/models", headers=self._headers(),
                    timeout=(15, 30)) as resp:
                resp.encoding = "utf-8"
                if resp.status_code != 200:
                    return {"ok": False, "models": [],
                            "message": f"HTTP {resp.status_code}：{resp.text[:500]}"}
                obj = resp.json()
        except (requests.RequestException, TimeoutError) as e:
            return {"ok": False, "models": [], "message": f"网络错误：{e}"}
        except Exception as e:
            return {"ok": False, "models": [], "message": f"响应解析失败：{e}"}

        items = obj
        if isinstance(obj, dict):
            items = obj.get("data") or obj.get("models") or []
        if not isinstance(items, list):
            return {"ok": False, "models": [], "message": "接口返回格式无法识别，未找到模型列表"}

        models: list[dict] = []
        for it in items:
            if isinstance(it, str):
                if it.strip():
                    models.append({"id": it.strip(), "context_length": None})
            elif isinstance(it, dict):
                mid = it.get("id") or it.get("name") or it.get("model") or ""
                if not str(mid).strip():
                    continue
                ctx = it.get("context_length") or it.get("context_window") or it.get("max_context")
                try:
                    ctx = int(ctx) if ctx else None
                except (TypeError, ValueError):
                    ctx = None
                models.append({"id": str(mid).strip(), "context_length": ctx})
        models.sort(key=lambda m: m["id"].lower())
        if not models:
            return {"ok": False, "models": [], "message": "接口连通但未返回任何模型，请检查接口地址"}
        return {"ok": True, "models": models, "message": f"共获取到 {len(models)} 个模型"}

    def test(self) -> dict:
        """测试连接：发送最小请求，返回 (ok, message)。

        注意 max_tokens 不能太小：默认开启推理的模型会把输出预算全花在思考上，
        正文为空被误判为「不兼容」。给足 128 并区分「只思考未产出正文」。
        """
        try:
            r = self.complete(
                [{"role": "user", "content": "你好，请回复：OK"}],
                temperature=0, max_tokens=128, stream=False, retries=0)
            text = (r.get("text") or "").strip()
            thinking = (r.get("thinking") or "").strip()
            if text:
                return {"ok": True, "message": f"连接成功，模型回复：{text[:50]}"}
            if thinking:
                # 推理模型把 128 token 全花在思考上，正文为空——不是连接/格式问题
                return {"ok": False, "message":
                        "连接成功，但模型只输出了思考（推理模型）未产出正文。"
                        "请在档案里把「推理模式」改为关闭，或调大该模型的输出上限后重试。"}
            return {"ok": False, "message":
                    "连接成功但回复为空，请检查模型名是否正确，或该接口是否兼容 OpenAI 格式"}
        except ApiError as e:
            return {"ok": False, "message": str(e)}
        except Exception as e:
            return {"ok": False, "message": f"{type(e).__name__}: {e}"}


# ---------------- token 估算 ----------------

def estimate_tokens(text: str, ratio: float = 0.75) -> int:
    """粗略 token 估算：字符数 × 系数（中文约 0.6~1 token/字，默认 0.75）。"""
    if not text:
        return 0
    return max(1, int(len(text) * ratio))


def estimate_cost(prompt_tokens: int, completion_tokens: int,
                  input_price: float, output_price: float) -> float:
    """费用估算（每百万 token 单价）。"""
    return (prompt_tokens / 1e6 * float(input_price or 0)
            + completion_tokens / 1e6 * float(output_price or 0))
