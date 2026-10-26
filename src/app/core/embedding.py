# -*- coding: utf-8 -*-
"""嵌入（向量）公共模块：OpenAI 兼容 /embeddings 调用 + 余弦相似度。

供文风审校（语义查重）与记忆语义检索共用。设计红线：
- 配置缺失或调用失败一律静默降级（返回 None/空），绝不阻断主流程；
- 配置统一存全局库（embed_base_url / embed_api_key / embed_model），
  经 ProjectStore.get_setting 转发读取，避免多存储源脱节。
"""
import json
import math
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from .api import decrypt_key
from .diagnostics import record_issue, clear_issue

_EMBEDDING_USER_AGENT = "JiuGuanNovelGenerator/1.0"


def _embedding_resource_url(base_url: str, resource: str) -> str:
    """把根地址或完整资源地址规范成 /embeddings 或 /models 地址。"""
    raw = (base_url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    path = parts.path.rstrip("/")
    last = path.rsplit("/", 1)[-1].lower() if path else ""
    if last in ("embeddings", "models"):
        parent = path.rsplit("/", 1)[0]
        path = f"{parent}/{resource}" if parent else f"/{resource}"
    else:
        path = f"{path}/{resource}" if path else f"/{resource}"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _embedding_headers(api_key: str) -> dict[str, str]:
    """部分兼容网关会拦截 urllib 默认的 Python-urllib User-Agent。"""
    headers = {"Content-Type": "application/json", "User-Agent": _EMBEDDING_USER_AGENT}
    if (api_key or "").strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    return headers


def embed_texts(base_url: str, api_key: str, model: str,
                texts: list[str]) -> list[list[float]] | None:
    """批量文本 → 向量列表。失败返回 None（调用方静默降级），同时登记诊断提醒。"""
    if not texts:
        return []
    try:
        req = urllib.request.Request(
            _embedding_resource_url(base_url, "embeddings"),
            data=json.dumps({"model": model, "input": texts}).encode("utf-8"),
            headers=_embedding_headers(api_key),
            method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
        vecs = [d["embedding"] for d in obj.get("data", [])]
        if vecs and len(vecs) == len(texts):
            clear_issue("embed_call")  # 调用恢复：自动消除「向量接口调用失败」提醒
            return vecs
        record_issue("embed_call",
                     "向量（嵌入）接口调用失败",
                     "接口返回向量数量与输入不一致，语义记忆检索暂时停用",
                     hint="稍后会自动重试；恢复后本提醒自动消失。也可在「模型」页点「测试连接」。")
        return None
    except Exception as e:
        record_issue("embed_call",
                     "向量（嵌入）接口调用失败",
                     f"{type(e).__name__}: {e}"[:400],
                     hint="检查嵌入接口地址/密钥/模型，或等服务商恢复；恢复后本提醒自动消失。")
        return None


def embed_one(base_url: str, api_key: str, model: str, text: str) -> list[float] | None:
    """单条文本 → 向量；失败返回 None。"""
    vecs = embed_texts(base_url, api_key, model, [text])
    return vecs[0] if vecs else None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度（维度不一致返回 0）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def load_embed_config(store) -> dict | None:
    """从项目库读嵌入配置（经 GLOBAL_SETTING_KEYS 转发到全局库）。

    返回 {"base_url", "api_key"(已解密), "model"}；任一缺失返回 None。
    """
    try:
        base_url = (store.get_setting("embed_base_url", "") or "").strip()
        model = (store.get_setting("embed_model", "") or "").strip()
        key_enc = (store.get_setting("embed_api_key", "") or "").strip()
        if not base_url or not model or not key_enc:
            return None
        api_key = decrypt_key(key_enc)
        if not api_key:
            return None
        return {"base_url": base_url, "api_key": api_key, "model": model}
    except Exception:
        return None


def test_embedding(base_url: str, api_key: str, model: str) -> dict:
    """测试 OpenAI 兼容嵌入接口，返回连接状态、错误和向量维度。"""
    result = {"ok": False, "message": "", "error": "", "dimension": None}
    url = (base_url or "").strip()
    if not url:
        result["error"] = "请填写嵌入接口地址"
        result["message"] = result["error"]
        return result
    if not (model or "").strip():
        result["error"] = "请填写嵌入模型名"
        result["message"] = result["error"]
        return result
    request = urllib.request.Request(
        _embedding_resource_url(url, "embeddings"),
        data=json.dumps({"model": model.strip(), "input": ["嵌入接口连接测试"]}).encode("utf-8"),
        headers=_embedding_headers(api_key),
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            obj = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")[:500]
        except Exception:
            body = str(exc)
        result["error"] = f"HTTP {exc.code}：{body}"
        result["message"] = result["error"]
        return result
    except urllib.error.URLError as exc:
        result["error"] = f"网络错误：{exc.reason}"
        result["message"] = result["error"]
        return result
    except (TimeoutError, json.JSONDecodeError, OSError) as exc:
        result["error"] = f"响应解析失败：{exc}"
        result["message"] = result["error"]
        return result
    except Exception as exc:
        result["error"] = f"测试失败：{type(exc).__name__}: {exc}"
        result["message"] = result["error"]
        return result

    data = obj.get("data") if isinstance(obj, dict) else None
    vector = data[0].get("embedding") if isinstance(data, list) and data and isinstance(data[0], dict) else None
    if not isinstance(vector, list) or not vector:
        result["error"] = "接口返回空向量，无法确认嵌入模型是否可用"
        result["message"] = result["error"]
        return result
    result["ok"] = True
    result["dimension"] = len(vector)
    result["message"] = f"连接成功，向量维度 {len(vector)}"
    clear_issue("embed_call")  # 手动测试通过：视为接口已恢复
    return result


def is_rerank_model(m: dict) -> bool:
    """按模型 id / supported_endpoint_types 判断是否重排（rerank）模型。

    嵌入模型列表用它排除重排条目；rerank.list_rerank_models 反向用它只留重排条目。
    """
    mid = str(m.get("id", "")).lower()
    types = " ".join(str(x).lower() for x in (m.get("supported_endpoint_types") or []))
    return any(t in mid or t in types for t in ("rerank", "re-ranker", "ranker"))


def list_embedding_models(base_url: str, api_key: str) -> dict:
    """拉取 OpenAI 兼容服务的嵌入模型列表，过滤明确标为重排模型的条目。

    返回 {"ok", "models": [{"id", "context_length"}], "message"}。
    """
    result = {"ok": False, "models": [], "message": ""}
    url = _embedding_resource_url(base_url, "models")
    if not url:
        result["message"] = "请填写嵌入接口地址"
        return result
    request = urllib.request.Request(url, headers=_embedding_headers(api_key), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            obj = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            body = str(exc)
        result["message"] = f"HTTP {exc.code}：{body}"
        return result
    except urllib.error.URLError as exc:
        result["message"] = f"网络错误：{exc.reason}"
        return result
    except Exception as exc:
        result["message"] = f"获取失败：{type(exc).__name__}: {exc}"
        return result

    def _is_embed(m: dict) -> bool:
        mid = str(m.get("id", "")).lower()
        if any(t in mid for t in ("embed", "embedding")):
            return True
        return isinstance(m.get("object"), str) and "embedding" in m["object"].lower()

    def _context_length(m: dict):
        try:
            return int(m["context_length"])
        except (KeyError, TypeError, ValueError):
            return None

    raw = obj.get("data", []) if isinstance(obj, dict) else []
    models = []
    for m in raw:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        if is_rerank_model(m):
            continue
        if _is_embed(m):
            models.append({"id": m["id"], "context_length": _context_length(m)})
    if not models:
        # 网关可能不标注类型：列出「非对话/非重排」模型供手动选择（保守）
        for m in raw:
            if not isinstance(m, dict) or not m.get("id"):
                continue
            if is_rerank_model(m):
                continue
            mid = str(m["id"]).lower()
            if any(t in mid for t in ("gpt", "chat", "claude", "qwen-turbo", "deepseek")):
                continue
            models.append({"id": m["id"], "context_length": _context_length(m)})
    result["ok"] = True
    result["models"] = models
    result["message"] = f"找到 {len(models)} 个候选模型"
    return result


test_embedding.__test__ = False
