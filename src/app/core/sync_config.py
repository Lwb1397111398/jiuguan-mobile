# -*- coding: utf-8 -*-
"""云同步的设备本地配置与状态（桌面/安卓共用）。

两份文件，都放在 `app_data_dir()`，**都不进 app.db**：

- `sync.json`       连接参数 + 设备身份 + 同步密钥
- `sync_state.json` 每本书「上次同步到云端哪一版」的游标（可随时丢弃重建）

为什么不放 app.db：app.db 本身是要同步的对象。若把 SK 的 DPAPI 密文放进去，
传到另一端会变成解不开的坏 key；把密钥放进 app.db 更会把主密钥一起上传。
所以连接凭据只留在本机，每台设备各配一次。

密钥存储沿用项目既有姿势（`api.encrypt_key`）：Windows 走 DPAPI `d1:`，
安卓退化为可逆混淆 `x1:`。所以**不需要每次启动都输同步密码**；换机器或
DPAPI 失效时，`get_crypto()` 返回 None，上层再让用户重新输一次。
"""
import base64
import datetime
import json
import os
import tempfile
import uuid

from .api import decrypt_key, encrypt_key
from .paths import app_data_dir
from .sync_crypto import DEFAULT_ITERS, SyncCrypto

CONFIG_NAME = "sync.json"
STATE_NAME = "sync_state.json"
LOG_NAME = "sync.log"
LOG_MAX = 512 * 1024

DEFAULTS = {
    "endpoint": "https://s3.cstcloud.cn",
    "bucket": "",
    "region": "us-east-1",
    "user_agent": "S3Drive/1.0.0",
    "path_style": True,
    "prefix": "jiuguan-sync/v1",
    "access_key": "",
    "secret_key": "",      # encrypt_key 加密后的串
    "device_id": "",
    "device_name": "",
    "kdf": {},             # {"algo","iters","salt"}
    "key_blob": "",        # encrypt_key(base64(enc_root+mac_root))
    "password_verifier": "",
}


# ---------------- 通用读写 ----------------

def _path(name: str) -> str:
    return os.path.join(app_data_dir(), name)


def _load_json(name: str, default):
    p = _path(name)
    if not os.path.isfile(p):
        return default
    try:
        with open(p, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else default
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(name: str, data: dict) -> None:
    """原子写：先写临时文件再 replace，避免掉电/崩溃留下半截 JSON。"""
    p = _path(name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".sync_", suffix=".json",
                               dir=os.path.dirname(p))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ---------------- 连接配置 ----------------

def load_config() -> dict:
    cfg = dict(DEFAULTS)
    cfg.update(_load_json(CONFIG_NAME, {}))
    if not cfg.get("device_id"):
        cfg["device_id"] = uuid.uuid4().hex[:16]
        if not cfg.get("device_name"):
            cfg["device_name"] = _default_device_name()
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    _save_json(CONFIG_NAME, cfg)


def _default_device_name() -> str:
    try:
        import platform
        node = platform.node() or ""
    except Exception:
        node = ""
    from .paths import is_android
    base = "平板" if is_android() else "电脑"
    return (node[:20] + " " + base).strip() if node else base


def set_secret(cfg: dict, plain: str) -> None:
    """写 SK（加密后落盘）。传空串表示清空。"""
    cfg["secret_key"] = encrypt_key(plain) if plain else ""
    save_config(cfg)


def get_secret(cfg: dict) -> str:
    return decrypt_key(cfg.get("secret_key") or "")


def is_configured(cfg: dict) -> bool:
    return bool(cfg.get("bucket") and cfg.get("access_key") and get_secret(cfg))


def make_client(cfg: dict, retries: int = 3, log=None):
    """按当前配置造一个 S3Client（未配置时抛 ValueError）。"""
    from .s3 import S3Client
    if not is_configured(cfg):
        raise ValueError("云同步还没配置完整（需要桶名 + AccessKey + Secret）")
    return S3Client(cfg["endpoint"], cfg["bucket"], cfg["access_key"],
                    get_secret(cfg), region=cfg.get("region") or "us-east-1",
                    user_agent=cfg.get("user_agent") or "S3Drive/1.0.0",
                    retries=retries, log=log)


# ---------------- 同步密码 / 主密钥 ----------------

def set_password(cfg: dict, password: str, iters: int = DEFAULT_ITERS) -> SyncCrypto:
    """首次设置同步密码：派生主密钥并把（加密后的）主密钥留在本机。"""
    crypto = SyncCrypto.create(password, iters)
    _remember_crypto(cfg, crypto)
    cfg["password_verifier"] = crypto.verifier()
    save_config(cfg)
    return crypto


def adopt_password(cfg: dict, password: str, kdf_params: dict,
                   verifier: str = "") -> SyncCrypto:
    """按云端 meta.json 的盐重新派生（第二台设备绑定 / 换机重装）。"""
    if not kdf_params or not kdf_params.get("salt"):
        raise ValueError("云端缺少密钥派生参数，无法绑定")
    crypto = SyncCrypto.derive(password, bytes.fromhex(kdf_params["salt"]),
                               int(kdf_params.get("iters") or DEFAULT_ITERS))
    if verifier and not crypto.check_verifier(verifier):
        raise ValueError("同步密码不对")
    _remember_crypto(cfg, crypto)
    cfg["password_verifier"] = crypto.verifier()
    save_config(cfg)
    return crypto


def _remember_crypto(cfg: dict, crypto: SyncCrypto) -> None:
    cfg["kdf"] = crypto.kdf_params
    cfg["key_blob"] = encrypt_key(base64.b64encode(
        crypto.enc_root + crypto.mac_root).decode("ascii"))


def get_crypto(cfg: dict) -> SyncCrypto:
    """取本机缓存的主密钥；没有或解不开返回 None（上层应让用户重输密码）。"""
    blob = decrypt_key(cfg.get("key_blob") or "")
    if not blob:
        return None
    try:
        raw = base64.b64decode(blob)
    except Exception:
        return None
    if len(raw) != 64:
        return None
    kdf = cfg.get("kdf") or {}
    try:
        salt = bytes.fromhex(kdf.get("salt") or "")
    except ValueError:
        return None
    if len(salt) != 16:
        return None
    return SyncCrypto(raw[:32], raw[32:], salt, int(kdf.get("iters") or DEFAULT_ITERS))


def clear_password(cfg: dict) -> None:
    cfg["kdf"] = {}
    cfg["key_blob"] = ""
    cfg["password_verifier"] = ""
    save_config(cfg)


# ---------------- 同步游标 ----------------

def load_state() -> dict:
    data = _load_json(STATE_NAME, {})
    data.setdefault("cursor", {})
    data.setdefault("last_sync_at", "")
    data.setdefault("last_error", "")
    return data


def save_state(state: dict) -> None:
    _save_json(STATE_NAME, state)


def cursor_for(state: dict, project_uuid: str) -> dict:
    cur = state["cursor"].get(project_uuid)
    if not isinstance(cur, dict):
        cur = {}
        state["cursor"][project_uuid] = cur
    return cur


def drop_cursor(state: dict, project_uuid: str) -> None:
    state["cursor"].pop(project_uuid, None)


# ---------------- 排障日志 ----------------

class SyncLog:
    """滚动的同步日志。

    这个服务**不返回 x-amz-request-id**，出问题时服务端帮不上忙，
    只能靠本机留痕。超过 LOG_MAX 自动轮转一份 .1。
    """

    def __init__(self, path: str = ""):
        self.path = path or _path(LOG_NAME)
        self._size = os.path.getsize(self.path) if os.path.isfile(self.path) else 0

    def __call__(self, msg: str) -> None:
        line = datetime.datetime.now().strftime("%m-%d %H:%M:%S ") + str(msg)
        try:
            if self._size > LOG_MAX:
                old = self.path + ".1"
                try:
                    if os.path.isfile(old):
                        os.remove(old)
                    os.replace(self.path, old)
                except OSError:
                    pass
                self._size = 0
            with open(self.path, "a", encoding="utf-8") as fp:
                fp.write(line + "\n")
            self._size += len(line.encode("utf-8")) + 1
        except OSError:
            pass

    def tail(self, lines: int = 40) -> str:
        if not os.path.isfile(self.path):
            return ""
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as fp:
                return "".join(fp.readlines()[-lines:])
        except OSError:
            return ""
