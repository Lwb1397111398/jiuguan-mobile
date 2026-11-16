# -*- coding: utf-8 -*-
"""S3 兼容客户端（纯标准库 + requests，桌面/安卓共用）。

只有这一个模块知道「中国科技云·数据胶囊」的特殊脾气（2026-11-16 实测）：

1. **User-Agent 闸门**：s3.cstcloud.cn 按 AccessKey 绑定的「应用类型」校验
   User-Agent，UA 不匹配一律返回 **401 空包**——响应里没有 Server、没有
   x-amz-request-id、没有 XML，看起来像网关故障，实际是 UA 不对。
   AK 建在「S3Drive」应用下 → UA 必须含 ``S3Drive``；建在 Rclone 下就必须
   用 ``rclone/vX``。所以 UA 做成可配置项，不要写死。
2. **不返回 x-amz-request-id**：排障只能靠自己记日志。
3. **必须路径式寻址**：桶名是 32 位十六进制（不是合法 DNS label），
   虚拟主机式 ``<bucket>.s3.cstcloud.cn`` 直接连接重置。
4. region 固定 ``us-east-1``：服务端不校验归属，但 SigV4 的 scope 必须填。

本模块只做「单对象 PUT/GET/HEAD/LIST/DELETE」，不实现分片上传——
胶囊单文件不设限（Web 端限制 4G 是前端限制），一次 PUT 走完即可。
"""
import datetime
import hashlib
import hmac
import os
import re
import time
import urllib.parse

import requests

DEFAULT_REGION = "us-east-1"
DEFAULT_UA = "S3Drive/1.0.0"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_UNRESERVED = "-_.~"
_BODY_MAX = 2048  # 错误响应体只留这么多，避免把整段 XML 塞进日志


# ---------------- 纯函数：签名（单测直接用，不碰网络） ----------------

def sha256_hex(data) -> str:
    """bytes / 文件对象 → sha256 十六进制。"""
    h = hashlib.sha256()
    if hasattr(data, "read"):
        pos = data.tell()
        while True:
            chunk = data.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
        data.seek(pos)
    else:
        h.update(data)
    return h.hexdigest()


def _amz_now() -> tuple:
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d"))


def canonical_query(params) -> str:
    """查询串规范化：按 key（同 key 再按 value）排序，键值都做严格编码。"""
    if not params:
        return ""
    pairs = []
    for k, v in params:
        pairs.append((str(k), "" if v is None else str(v)))
    pairs.sort()
    return "&".join(urllib.parse.quote(k, safe=_UNRESERVED) + "=" +
                     urllib.parse.quote(v, safe=_UNRESERVED) for k, v in pairs)


def canonical_uri(bucket: str, key: str) -> str:
    """路径式规范 URI：/桶/键（键按段编码，保留斜杠）。"""
    path = "/" + urllib.parse.quote(bucket, safe=_UNRESERVED)
    if key:
        path += "/" + urllib.parse.quote(key, safe="/" + _UNRESERVED)
    else:
        path += "/"
    return path


def sigv4_authorization(method: str, host: str, uri: str, query_params,
                        payload_sha: str, access_key: str, secret_key: str,
                        region: str, amz_date: str, datestamp: str) -> tuple:
    """算 Authorization 头。返回 (头值, signed_headers)。

    参与签名的头固定三个：host / x-amz-content-sha256 / x-amz-date。
    Content-Type、User-Agent 等一律不签（S3 允许），减少踩空。
    """
    headers = {"host": host,
               "x-amz-content-sha256": payload_sha,
               "x-amz-date": amz_date}
    names = sorted(headers)
    canonical_headers = "".join(n + ":" + headers[n] + "\n" for n in names)
    signed_headers = ";".join(names)
    canonical_request = "\n".join([
        method, uri, canonical_query(query_params),
        canonical_headers, signed_headers, payload_sha,
    ])
    scope = datestamp + "/" + region + "/s3/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    key = ("AWS4" + secret_key).encode("utf-8")
    for part in (datestamp, region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode("utf-8"), hashlib.sha256).digest()
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    value = ("AWS4-HMAC-SHA256 Credential=" + access_key + "/" + scope +
             ", SignedHeaders=" + signed_headers + ", Signature=" + signature)
    return value, signed_headers


def _parse_code(body: bytes) -> str:
    """从错误 XML 里抠 <Code>；抠不到返回空串。"""
    try:
        text = body.decode("utf-8", "replace")
    except Exception:
        return ""
    m = re.search(r"<Code>([^<]+)</Code>", text)
    return m.group(1) if m else ""


class S3Error(Exception):
    """S3 请求失败。status=None 表示网络层失败（连接/超时）。

    code 语义：服务端 <Code>（NoSuchKey / AccessDenied / SignatureDoesNotMatch…）；
    本地判定出来的特殊值是 ``AuthGate``（401 空包 = UA 与应用类型不匹配）
    与 ``NetworkError``。
    """

    def __init__(self, message: str, status=None, code: str = "", body: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.body = body

    @property
    def retryable(self) -> bool:
        """网络失败 / 5xx / 429 可重试；4xx 一律不重试（重试也是白烧）。"""
        return self.status is None or self.status >= 500 or self.status == 429


class S3Client:
    """极简 S3 客户端。线程安全：每个实例持一个 Session，别跨线程共享。"""

    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str,
                 region: str = DEFAULT_REGION, user_agent: str = DEFAULT_UA,
                 timeout=(10, 120), retries: int = 3, log=None):
        raw = (endpoint or "").strip()
        if raw and "://" not in raw:
            raw = "https://" + raw
        self.endpoint = raw.rstrip("/")
        parsed = urllib.parse.urlsplit(self.endpoint)
        self.scheme = parsed.scheme or "https"
        self.host = parsed.netloc
        self.bucket = (bucket or "").strip()
        self.access_key = (access_key or "").strip()
        self.secret_key = (secret_key or "").strip()
        self.region = (region or DEFAULT_REGION).strip()
        self.user_agent = (user_agent or DEFAULT_UA).strip()
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self._log = log or (lambda msg: None)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = self.user_agent

    # ---- 底层 ----

    def _url(self, key: str, params) -> str:
        url = self.scheme + "://" + self.host + canonical_uri(self.bucket, key)
        if params:
            url += "?" + canonical_query(params)
        return url

    def _request(self, method: str, key: str = "", params=None, body=None,
                 payload_sha: str = None, extra_headers=None,
                 stream_path: str = None) -> requests.Response:
        params = list(params or [])
        if payload_sha is None:
            payload_sha = EMPTY_SHA256
        last = None
        for attempt in range(self.retries):
            amz_date, datestamp = _amz_now()
            auth, _ = sigv4_authorization(
                method, self.host, canonical_uri(self.bucket, key), params,
                payload_sha, self.access_key, self.secret_key,
                self.region, amz_date, datestamp)
            headers = {"Host": self.host, "x-amz-date": amz_date,
                       "x-amz-content-sha256": payload_sha, "Authorization": auth}
            if extra_headers:
                headers.update(extra_headers)
            url = self._url(key, params)
            try:
                resp = self.session.request(
                    method, url, headers=headers, data=body, timeout=self.timeout,
                    stream=stream_path is not None)
            except requests.exceptions.ProxyError:
                # 与本项目 api.py 同源处理：环境变量指向死端口时改直连重发
                try:
                    resp = self.session.request(
                        method, url, headers=headers, data=body, timeout=self.timeout,
                        stream=stream_path is not None,
                        proxies={"http": None, "https": None})
                except requests.exceptions.RequestException as e:
                    last = S3Error("连接失败：" + type(e).__name__ + " " + str(e)[:160],
                                   code="NetworkError")
                    self._log("[s3] " + method + " " + key + " 网络异常（第 " +
                              str(attempt + 1) + " 次）：" + type(e).__name__)
                    time.sleep(min(6.0, 0.5 * (2 ** attempt)))
                    continue
            except requests.exceptions.RequestException as e:
                last = S3Error("连接失败：" + type(e).__name__ + " " + str(e)[:160],
                               code="NetworkError")
                self._log("[s3] " + method + " " + key + " 网络异常（第 " +
                          str(attempt + 1) + " 次）：" + type(e).__name__)
                time.sleep(min(6.0, 0.5 * (2 ** attempt)))
                continue

            if resp.status_code < 400:
                return resp

            raw = resp.content if not stream_path else b""
            body_text = raw[:_BODY_MAX].decode("utf-8", "replace") if raw else ""
            code = _parse_code(raw) if raw else ""
            if resp.status_code == 401 and not raw:
                # 空包 401 是这个服务的 UA 闸门特征，不是密钥错
                raise S3Error(
                    "鉴权被网关拦下（401 空响应）。这个服务按 AccessKey 绑定的"
                    "「应用类型」校验 User-Agent，当前 UA=\"" + self.user_agent +
                    "\" 可能与之不匹配——到胶囊后台看一眼该 AK 的「应用」列，"
                    "把 User-Agent 改成同一类型（如 S3Drive/1.0.0）。",
                    status=401, code="AuthGate")
            err = S3Error(method + " " + key + " 失败：HTTP " + str(resp.status_code) +
                          (" " + code if code else "") + " " + body_text[:160],
                          status=resp.status_code, code=code, body=body_text)
            if err.retryable:
                last = err
                self._log("[s3] 重试 " + method + " " + key + "（" +
                          str(resp.status_code) + "，第 " + str(attempt + 1) + " 次）")
                time.sleep(min(6.0, 0.5 * (2 ** attempt)))
                continue
            raise err
        raise last or S3Error(method + " " + key + " 失败：重试用尽", code="RetryExhausted")

    # ---- 对象操作 ----

    def head_bucket(self) -> dict:
        """验证连接与权限。返回原始响应头字典。"""
        r = self._request("HEAD", "")
        return dict(r.headers)

    def head(self, key: str):
        """对象元信息；不存在返回 None。"""
        try:
            r = self._request("HEAD", key)
        except S3Error as e:
            if e.status == 404:
                return None
            raise
        return {"size": int(r.headers.get("Content-Length") or 0),
                "etag": (r.headers.get("ETag") or "").strip('"'),
                "modified": r.headers.get("Last-Modified") or ""}

    def exists(self, key: str) -> bool:
        return self.head(key) is not None

    def put_bytes(self, key: str, data: bytes, content_type=None) -> dict:
        """上传内存里的字节。返回 {size, etag}。"""
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        r = self._request("PUT", key, body=data,
                          payload_sha=hashlib.sha256(data).hexdigest(),
                          extra_headers=headers)
        return {"size": len(data), "etag": (r.headers.get("ETag") or "").strip('"')}

    def put_file(self, key: str, path: str) -> dict:
        """上传本地文件（流式，不读进内存）。返回 {size, etag}。"""
        size = os.path.getsize(path)
        with open(path, "rb") as fp:
            digest = sha256_hex(fp)
            fp.seek(0)
            r = self._request("PUT", key, body=fp, payload_sha=digest)
        return {"size": size, "etag": (r.headers.get("ETag") or "").strip('"')}

    def get_bytes(self, key: str, limit: int = 64 << 20) -> bytes:
        """下载到内存（只给小对象用，别拿它下 30MB 的库）。"""
        r = self._request("GET", key)
        data = r.content
        if len(data) > limit:
            raise S3Error("对象过大（" + str(len(data)) + " 字节），请用 download_to",
                          code="TooLarge")
        return data

    def download_to(self, key: str, path: str) -> dict:
        """流式下载到本地文件，顺便算 sha256。返回 {size, sha256}。"""
        r = self._request("GET", key, stream_path=path)
        h = hashlib.sha256()
        size = 0
        tmp = path + ".part"
        try:
            with open(tmp, "wb") as fp:
                for chunk in r.iter_content(1 << 20):
                    if not chunk:
                        continue
                    fp.write(chunk)
                    h.update(chunk)
                    size += len(chunk)
            os.replace(tmp, path)
        finally:
            try:
                r.close()
            except Exception:
                pass
            if os.path.isfile(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return {"size": size, "sha256": h.hexdigest()}

    def delete(self, key: str) -> None:
        try:
            self._request("DELETE", key)
        except S3Error as e:
            if e.status not in (404,):
                raise

    def copy(self, src_key: str, dst_key: str) -> dict:
        """服务端复制（归档历史版本用），不走本机上下行流量。

        ⚠ 必须校验结果体积：有的 S3 网关不支持 x-amz-copy-source，
        却仍返回 200 并在目标位置留下 0 字节对象（比报错更危险）。
        体积对不上就删掉目标并抛 CopyUnsupported，让上层改走下载+上传。
        """
        src = self.head(src_key)
        if src is None:
            raise S3Error("源对象不存在：" + src_key, status=404, code="NoSuchKey")
        headers = {"x-amz-copy-source": "/" + self.bucket + "/" +
                   urllib.parse.quote(src_key, safe="/" + _UNRESERVED)}
        self._request("PUT", dst_key, extra_headers=headers)
        dst = self.head(dst_key)
        if dst is None or dst["size"] != src["size"]:
            try:
                self.delete(dst_key)
            except S3Error:
                pass
            raise S3Error("网关不支持服务端复制（目标体积 %s ≠ 源 %s）"
                          % (dst["size"] if dst else "缺失", src["size"]),
                          code="CopyUnsupported")
        return {"size": dst["size"], "etag": dst["etag"]}

    def list_page(self, prefix: str = "", delimiter: str = "", token: str = "",
                  max_keys: int = 1000) -> dict:
        """ListObjectsV2 单页。返回 {keys:[{key,size,modified}], prefixes:[], next: str}。"""
        params = [("list-type", "2"), ("max-keys", str(max_keys))]
        if prefix:
            params.append(("prefix", prefix))
        if delimiter:
            params.append(("delimiter", delimiter))
        if token:
            params.append(("continuation-token", token))
        try:
            r = self._request("GET", "", params=params)
        except S3Error as e:
            # 老网关只认 v1 时降级重试一次
            if e.status in (400, 501):
                return self._list_page_v1(prefix, delimiter, token, max_keys)
            raise
        return _parse_list(r.content.decode("utf-8", "replace"))

    def _list_page_v1(self, prefix, delimiter, token, max_keys) -> dict:
        params = [("max-keys", str(max_keys))]
        if prefix:
            params.append(("prefix", prefix))
        if delimiter:
            params.append(("delimiter", delimiter))
        if token:
            params.append(("marker", token))
        r = self._request("GET", "", params=params)
        return _parse_list(r.content.decode("utf-8", "replace"))

    def list_all(self, prefix: str = "", limit: int = 20000) -> list:
        """按前缀列出全部对象（自动翻页）。"""
        out, token, seen = [], "", 0
        while True:
            page = self.list_page(prefix=prefix, token=token)
            out.extend(page["keys"])
            seen += len(page["keys"])
            token = page.get("next") or ""
            if not token or not page["keys"] or seen >= limit:
                return out

    def delete_prefix(self, prefix: str) -> int:
        """删掉某前缀下所有对象，返回删除个数（历史版本裁剪用）。"""
        n = 0
        for item in self.list_all(prefix):
            self.delete(item["key"])
            n += 1
        return n


def _tag(xml: str, name: str, start: int = 0):
    """取第一个 <name>…</name> 的内容，返回 (内容, 结束位置)。"""
    m = re.search(r"<" + name + r">(.*?)</" + name + r">", xml[start:], re.S)
    if not m:
        return "", start
    return m.group(1), start + m.end()


def _parse_list(xml: str) -> dict:
    """解析 ListBucketResult（v1/v2 通用，只取 key/size/前缀/续页标记）。"""
    keys, prefixes = [], []
    for m in re.finditer(r"<Contents>(.*?)</Contents>", xml, re.S):
        block = m.group(1)
        key = re.search(r"<Key>(.*?)</Key>", block, re.S)
        size = re.search(r"<Size>(\d+)</Size>", block)
        mod = re.search(r"<LastModified>(.*?)</LastModified>", block)
        if key:
            name = _unescape(key.group(1))
            # 网关会给每个前缀自动建 0 字节的「目录占位对象」（键以 / 结尾），
            # 不滤掉会污染用量统计、也会让历史版本裁剪数错数。
            if name.endswith("/"):
                continue
            keys.append({"key": name,
                         "size": int(size.group(1)) if size else 0,
                         "modified": mod.group(1) if mod else ""})
    for m in re.finditer(r"<CommonPrefixes>(.*?)</CommonPrefixes>", xml, re.S):
        p = re.search(r"<Prefix>(.*?)</Prefix>", m.group(1), re.S)
        if p:
            prefixes.append(_unescape(p.group(1)))
    next_token = ""
    m = re.search(r"<NextContinuationToken>(.*?)</NextContinuationToken>", xml, re.S)
    if m:
        next_token = _unescape(m.group(1))
    truncated = bool(re.search(r"<IsTruncated>true</IsTruncated>", xml))
    return {"keys": keys, "prefixes": prefixes,
            "next": next_token if truncated else ""}


def _unescape(text: str) -> str:
    return (text.replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&amp;", "&"))
