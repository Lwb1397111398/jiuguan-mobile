# -*- coding: utf-8 -*-
"""渠道请求节流：按模型档案配置的「每分钟请求上限」排队，唯一入口。

背景（2026-09-19 实测，Naota / gw.naoya.kdns.fr）：
部分中转渠道有「1 分钟内最多 N 次请求」的硬限制，且**失败次数也计入**。软件的
失败补救机制（上游拦截变体重发、自动模式节拍重试）会在几秒内连发 4~9 次，
把配额一次打光 → 之后每次请求都被 429 拒绝 → 自动模式表现成「一直失败」；
而手动逐次点击（人操作间隔几十秒）反而每次都能成功。两者差在**请求密度**。

做法：
- 模型档案新增 `rpm` 字段（0 = 不限）。每次真实 HTTP 请求发送前调用 `acquire()`；
- 窗口（60 秒）内已达上限 → 等到最早那次请求滑出窗口再发，不硬撞；
- 无论是否配了上限，每次请求都记时间戳，方便「把上限调小」后立刻按已有节奏生效；
- 状态只在内存（进程内），重启即清空——限流窗口本身就是短时的，不做持久化，
  也不引入「临时拉黑/冷却」这类隐藏状态（渠道切换模块的红线）。
"""
import threading
import time

# 限流窗口（秒）。渠道文案「1分钟内最多请求3次」→ 滑动 60 秒窗口。
WINDOW_SECONDS = 60.0
# 等待循环的轮询步长：分片 sleep，保证 stop_event / 中断标志能被及时看到。
_POLL_INTERVAL = 0.2

_lock = threading.Lock()
_history: dict[str, list[float]] = {}


def profile_key(profile: dict) -> str:
    """节流键：优先档案 id（同一档案 = 同一渠道）；无 id 时退回「地址+模型」。"""
    profile = profile or {}
    pid = profile.get("id")
    if pid:
        return "id:%s" % pid
    return "%s|%s" % (profile.get("base_url", ""), profile.get("model", ""))


def profile_rpm(profile: dict) -> int:
    """读档案的每分钟请求上限（0 = 不限；非法值按 0 处理，绝不中断生成）。"""
    try:
        return max(0, int((profile or {}).get("rpm") or 0))
    except (TypeError, ValueError):
        return 0


def _prune(key: str, now: float) -> list[float]:
    hist = [t for t in _history.get(key, []) if now - t < WINDOW_SECONDS]
    _history[key] = hist
    return hist


def acquire(profile: dict, should_stop=None) -> float:
    """发送前调用：必要时等待到可以发送，返回等待秒数（0 = 无需等待）。

    should_stop：可调用对象，返回 True 时立刻放弃等待（用户停止生成 / 中断请求）。
    放弃等待时**不占用配额**（本次请求根本没发出去）。
    """
    key = profile_key(profile)
    if not key:
        return 0.0
    rpm = profile_rpm(profile)
    waited = 0.0
    while True:
        now = time.time()
        with _lock:
            hist = _prune(key, now)
            if rpm <= 0 or len(hist) < rpm:
                hist.append(now)
                return waited
            wait = WINDOW_SECONDS - (now - hist[0]) + 0.05
        if wait <= 0.01:
            wait = _POLL_INTERVAL
        deadline = time.time() + wait
        while True:
            if should_stop is not None and should_stop():
                return waited
            left = deadline - time.time()
            if left <= 0:
                break
            time.sleep(min(_POLL_INTERVAL, left))
        waited += wait


def wait_gap(seconds: float, should_stop=None) -> float:
    """可中断的固定间隔等待（供变体重发之间留间隔用），返回实际等待秒数。"""
    deadline = time.time() + max(0.0, seconds)
    while True:
        if should_stop is not None and should_stop():
            return max(0.0, seconds - max(0.0, deadline - time.time()))
        left = deadline - time.time()
        if left <= 0:
            return seconds
        time.sleep(min(_POLL_INTERVAL, left))


def reset(profile: dict | None = None) -> None:
    """清空节流记录（测试用；profile=None 清全部）。"""
    with _lock:
        if profile is None:
            _history.clear()
        else:
            _history.pop(profile_key(profile), None)
