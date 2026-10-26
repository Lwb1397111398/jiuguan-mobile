# -*- coding: utf-8 -*-
"""重排（rerank）公共模块：记忆语义召回的二次精排。

余弦相似度只衡量「用词像不像」，分不清「同一话题」和「同一写作意图」——
召回阶段按相似度宽取候选后，由 rerank 模型逐条打「与本次写作意图的相关度」分，
按分精挑。与 embedding.py 同一套设计红线：
- 开关默认关；未启用 / 未配置 / 调用失败一律静默降级回纯余弦路径，绝不阻断生成；
- 配置统一存全局库（rerank_* 系列），经 ProjectStore.get_setting 转发读取；
  地址与密钥留空时跟随嵌入模型配置（最小配置只填一个模型名）。
- 接口格式双兼容："standard" = Jina / Cohere / 硅基流动 / vllm 等通用 /v1/rerank；
  "dashscope" = 阿里云百炼原生 /services/rerank/rerank（请求体结构不同，自动适配）。
"""
import json
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from .api import decrypt_key
from .diagnostics import record_issue, clear_issue
from .embedding import _embedding_resource_url, _embedding_headers, is_rerank_model

# 宽召回候选数（余弦 top N，不看 0.25 阈值）：重排只对少量候选做，代价可控
RERANK_CANDIDATES = 24
# 每条候选发给重排模型的文本截断长度（判断相关性头部足够，防请求超长）
RERANK_DOC_MAX_CHARS = 500


def _rerank_target(base_url: str) -> tuple[str, str]:
    """根地址 / 完整地址 → (请求地址, 格式)。

    - 地址已含 /rerank：原样使用（含 /services/rerank 的视为 DashScope 原生端点）；
    - dashscope.aliyuncs.com：兼容模式根地址没有 /rerank 端点，换算成原生
      /api/v1/services/rerank/rerank 并用 DashScope 请求体；
    - 其余：空路径补 /v1/rerank，/v1 结尾补 /rerank，别的路径补 /v1/rerank。
    """
    raw = (base_url or "").strip()
    if not raw:
        return "", "standard"
    parts = urlsplit(raw)
    path = parts.path.rstrip("/")
    host = (parts.netloc or "").lower()
    if path.lower().endswith("/rerank"):
        fmt = "dashscope" if "/services/rerank" in path.lower() else "standard"
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, "")), fmt
    if "dashscope.aliyuncs.com" in host:
        return urlunsplit((parts.scheme, parts.netloc,
                           "/api/v1/services/rerank/rerank", "", "")), "dashscope"
    if not path:
        path = "/v1/rerank"
    elif path.lower().endswith("/v1"):
        path = f"{path}/rerank"
    else:
        path = f"{path}/v1/rerank"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, "")), "standard"


def _parse_rank_items(obj) -> list[dict]:
    """兼容各家响应结构，提 [{index, score}]（按 score 降序；非法项丢弃）。

    - standard：results 或 data 数组，分数键 relevance_score / score；
    - dashscope：output.results 数组，分数键 relevance_score。
    """
    items = None
    if isinstance(obj, dict):
        out = obj.get("output")
        if isinstance(out, dict) and isinstance(out.get("results"), list):
            items = out["results"]
        elif isinstance(obj.get("results"), list):
            items = obj["results"]
        elif isinstance(obj.get("data"), list):
            items = obj["data"]
    ranked: list[dict] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        try:
            idx = int(it.get("index"))
            score = float(it.get("relevance_score", it.get("score")))
        except (TypeError, ValueError):
            continue
        ranked.append({"index": idx, "score": score})
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked


def rerank_documents(base_url: str, api_key: str, model: str, query: str,
                     documents: list[str], timeout: int = 30) -> list[dict] | None:
    """query + documents → 按相关度降序的 [{"index", "score"}]（index 指向 documents 下标）。

    失败返回 None（调用方静默回退纯余弦路径），同时登记诊断提醒。
    """
    if not documents:
        return []
    url, fmt = _rerank_target(base_url)
    if not url:
        return None
    if fmt == "dashscope":
        body = {"model": model,
                "input": {"query": query, "documents": list(documents)},
                "parameters": {"return_documents": False, "top_n": len(documents)}}
    else:
        body = {"model": model, "query": query, "documents": list(documents)}
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers=_embedding_headers(api_key), method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
        ranked = _parse_rank_items(obj)
    except Exception as e:
        record_issue("rerank_call",
                     "重排（rerank）接口调用失败",
                     f"{type(e).__name__}: {e}"[:400],
                     hint="本次召回暂时退回纯向量排序，不影响写作；检查重排接口地址/密钥/模型，"
                          "或关闭「重排精排」开关。恢复后本提醒自动消失。")
        return None
    if not ranked:
        record_issue("rerank_call",
                     "重排（rerank）接口调用失败",
                     "接口返回结果为空，无法完成二次精排",
                     hint="本次召回暂时退回纯向量排序；请核对重排模型名是否正确。")
        return None
    clear_issue("rerank_call")
    return ranked


def load_rerank_config(store) -> dict | None:
    """读重排配置（经 GLOBAL_SETTING_KEYS 转发到全局库）。

    开关未开 / 模型名缺失 → None；地址或密钥留空时跟随嵌入模型配置。
    返回 {"base_url", "api_key"(已解密), "model", "top_n"(1~10), "min_score"}。
    min_score=0 表示只排序不淘汰（0 作哨兵：部分服务商返回负分/非 0~1 分，不能拿 0 当下限）。
    """
    try:
        enabled = str(store.get_setting("rerank_enabled", "0") or "0").strip()
        if enabled != "1":
            return None
        model = (store.get_setting("rerank_model", "") or "").strip()
        if not model:
            return None
        base_url = (store.get_setting("rerank_base_url", "") or "").strip()
        key_enc = (store.get_setting("rerank_api_key", "") or "").strip()
        if not base_url:
            base_url = (store.get_setting("embed_base_url", "") or "").strip()
        if not key_enc:
            key_enc = (store.get_setting("embed_api_key", "") or "").strip()
        if not base_url or not key_enc:
            return None
        api_key = decrypt_key(key_enc)
        if not api_key:
            return None
        try:
            top_n = int(str(store.get_setting("rerank_top_n", "5")).strip())
        except (TypeError, ValueError):
            top_n = 5
        try:
            min_score = float(str(store.get_setting("rerank_min_score", "0")).strip())
        except (TypeError, ValueError):
            min_score = 0.0
        return {"base_url": base_url, "api_key": api_key, "model": model,
                "top_n": max(1, min(10, top_n)), "min_score": min_score}
    except Exception:
        return None


def test_rerank(base_url: str, api_key: str, model: str) -> dict:
    """测试重排接口：发一条 query + 两条示例文档，返回连接状态与最高相关分。"""
    result = {"ok": False, "message": "", "error": "", "top_score": None}
    url, fmt = _rerank_target(base_url)
    if not url:
        result["error"] = "请填写重排接口地址（或先在嵌入模型里填好地址，此处留空跟随）"
        result["message"] = result["error"]
        return result
    if not (model or "").strip():
        result["error"] = "请填写重排模型名"
        result["message"] = result["error"]
        return result
    if fmt == "dashscope":
        body = {"model": model.strip(),
                "input": {"query": "主角现在的伤势与所在位置",
                          "documents": ["主角左臂中弹，躲进废弃医院三楼包扎",
                                        "南城集市商队讨价还价的日常喧嚣"]},
                "parameters": {"return_documents": False, "top_n": 2}}
    else:
        body = {"model": model.strip(), "query": "主角现在的伤势与所在位置",
                "documents": ["主角左臂中弹，躲进废弃医院三楼包扎",
                              "南城集市商队讨价还价的日常喧嚣"]}
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers=_embedding_headers(api_key), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            obj = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body_txt = exc.read().decode("utf-8", "replace")[:500]
        except Exception:
            body_txt = str(exc)
        result["error"] = f"HTTP {exc.code}：{body_txt}"
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

    ranked = _parse_rank_items(obj)
    if not ranked:
        result["error"] = "接口返回结果为空，无法确认重排模型是否可用"
        result["message"] = result["error"]
        return result
    result["ok"] = True
    result["top_score"] = ranked[0]["score"]
    result["message"] = f"连接成功，示例 query 最高相关分 {ranked[0]['score']:.3f}"
    clear_issue("rerank_call")  # 手动测试通过：视为接口已恢复
    return result


def list_rerank_models(base_url: str, api_key: str) -> dict:
    """拉取 /models 里标注了重排能力的模型（与 list_embedding_models 相反：只留 rerank）。"""
    result = {"ok": False, "models": [], "message": ""}
    url = _embedding_resource_url(base_url, "models")
    if not url:
        result["message"] = "请填写重排接口地址"
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

    raw = obj.get("data", []) if isinstance(obj, dict) else []
    models = []
    for m in raw:
        if isinstance(m, dict) and m.get("id") and is_rerank_model(m):
            models.append({"id": m["id"]})
    result["ok"] = True
    result["models"] = models
    if models:
        result["message"] = f"找到 {len(models)} 个重排候选模型"
    else:
        result["message"] = ("网关未标注重排模型，可手动输入模型名"
                             "（常见：bge-reranker-v2-m3 / gte-rerank-v2 / jina-reranker-v2）")
    return result


test_rerank.__test__ = False
