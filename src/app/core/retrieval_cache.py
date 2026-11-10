# -*- coding: utf-8 -*-
"""语义检索结果缓存（进程级、按项目隔离；2026-11-09）。

老板需求：提示词没变时，向量模型 / rerank 模型算出来的召回结果不该每点一次生成
（尤其分批写作每一批）都重新跑一遍。库侧记忆向量本来就是归档时持久化好的，
每次真正重算的只是「本次 query 的向量 → 余弦宽召回 → 重排精挑」这几步。

设计红线：
- **只缓存完整成功的结果**。嵌入接口失败、重排调用失败回退纯余弦、无命中，
  一律不写跨次缓存，保证下一次生成仍会重试接口（诊断栏「稍后会自动重试」
  的承诺不能破，也不能把「rerank 挂了」的降级结果固化下来）。
- 键 = 章级 query + 嵌入配置 + 重排配置 + 向量库版本签名。归档 / 回滚 / 重建
  向量、换模型、换 query 任意一项变化都自动失效，不会拿旧记忆糊弄新章节。
- 按项目库路径隔离：切项目不会串味；测试用 tmp_path 天然各自独立。
- 进程内有效（重开软件最多重算一次），不落库、不改 schema。
- 线程安全：预览 build 在 GUI 线程、执行 build 在 StreamWorker 线程，可能并发。
"""
import hashlib
import json
import threading

_LOCK = threading.RLock()
_CACHE: dict[str, dict[str, dict]] = {}   # project_key -> {cache_key: payload}
_MAX_PER_PROJECT = 32


def project_key(store) -> str:
    """项目命名空间：优先用项目库绝对路径（切项目/多开天然隔离）。"""
    try:
        return str(getattr(store, "db_path", "") or id(store))
    except Exception:
        return str(id(store))


def make_key(query: str, embed_cfg: dict | None, rerank_cfg: dict | None,
             vector_revision: str) -> str:
    """跨次缓存键。只放影响结果的字段；api_key 绝不进键（也不进任何日志）。"""
    rk = rerank_cfg or {}
    payload = {
        "query": query or "",
        "embed_model": (embed_cfg or {}).get("model", ""),
        "embed_url": (embed_cfg or {}).get("base_url", ""),
        "rerank_model": rk.get("model", ""),
        "rerank_url": rk.get("base_url", ""),
        "rerank_top_n": rk.get("top_n", 0),
        "rerank_min_score": rk.get("min_score", 0),
        "vectors": vector_revision or "",
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get(project: str, key: str) -> dict | None:
    with _LOCK:
        bucket = _CACHE.get(project)
        return bucket.get(key) if bucket else None


def put(project: str, key: str, payload: dict) -> None:
    with _LOCK:
        bucket = _CACHE.setdefault(project, {})
        if len(bucket) >= _MAX_PER_PROJECT:
            try:
                bucket.pop(next(iter(bucket)))   # 简单淘汰最早一条
            except (StopIteration, KeyError):
                pass
        bucket[key] = payload


def clear(project: str | None = None) -> None:
    """清空缓存（测试隔离 / 项目关闭时用）；project=None 清全部。"""
    with _LOCK:
        if project is None:
            _CACHE.clear()
        else:
            _CACHE.pop(project, None)
