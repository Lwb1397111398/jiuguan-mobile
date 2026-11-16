# -*- coding: utf-8 -*-
"""同步载荷加密（纯标准库，桌面/安卓共用）。

为什么不用 AES：安卓 APK 不许带 C 扩展（历史踩坑），`cryptography` 没轮子；
纯 Python 的 AES 比 ChaCha20 慢一截。这里选 **ChaCha20 + HMAC-SHA256**：

    密码 --PBKDF2-SHA256(200k)--> 主密钥(64B)
    主密钥 --HMAC(nonce)--> 每块独立的 enc_key / mac_key
    密文 = ChaCha20(enc_key, nonce, 明文)
    认证 = HMAC-SHA256(mac_key, nonce ‖ 密文)        ← Encrypt-then-MAC
    落盘 = b"JGS1" + nonce(12) + mac(32) + 密文

要点：
- **PBKDF2 只算一次**（每台设备会话内缓存），按 nonce 派生每块子密钥，
  否则同步 20 个对象就要跑 20 次 200k 迭代。
- 校验密码用 `verifier = HMAC(mac_root, b"jgsync-verify")`，存在云端
  meta.json 里；输错密码当场报错，而不是解出一堆垃圾数据。
- 先 gzip 再加密：酒馆正文压缩比很高，真正要加密的字节数能砍到 1/4。
"""
import hashlib
import hmac
import os
import struct

MAGIC = b"JGS1"
NONCE_LEN = 12
MAC_LEN = 32
KEY_LEN = 32
SALT_LEN = 16
DEFAULT_ITERS = 200_000
_VERIFY_LABEL = b"jgsync-verify"


class CryptoError(Exception):
    """解密失败：密码不对、数据被改、或不是本程序的密文。"""


# ---------------- ChaCha20（RFC 7539） ----------------

def _rotl(v: int, n: int) -> int:
    return ((v << n) & 0xFFFFFFFF) | (v >> (32 - n))


def _quarter_round(s, a, b, c, d):
    s[a] = (s[a] + s[b]) & 0xFFFFFFFF
    s[d] = _rotl(s[d] ^ s[a], 16)
    s[c] = (s[c] + s[d]) & 0xFFFFFFFF
    s[b] = _rotl(s[b] ^ s[c], 12)
    s[a] = (s[a] + s[b]) & 0xFFFFFFFF
    s[d] = _rotl(s[d] ^ s[a], 8)
    s[c] = (s[c] + s[d]) & 0xFFFFFFFF
    s[b] = _rotl(s[b] ^ s[c], 7)


def chacha20_block(key: bytes, counter: int, nonce: bytes) -> bytes:
    """加密一轮的 64 字节密钥流块（RFC 7539 §2.3）。"""
    if len(key) != KEY_LEN:
        raise ValueError("ChaCha20 密钥必须是 32 字节")
    if len(nonce) != NONCE_LEN:
        raise ValueError("ChaCha20 nonce 必须是 12 字节")
    const = [0x61707865, 0x3320646E, 0x79622D32, 0x6B206574]
    k = list(struct.unpack("<8I", key))
    n = list(struct.unpack("<3I", nonce))
    state = const + k + [counter & 0xFFFFFFFF] + n
    work = state[:]
    for _ in range(10):  # 10 次双轮 = 20 轮
        _quarter_round(work, 0, 4, 8, 12)
        _quarter_round(work, 1, 5, 9, 13)
        _quarter_round(work, 2, 6, 10, 14)
        _quarter_round(work, 3, 7, 11, 15)
        _quarter_round(work, 0, 5, 10, 15)
        _quarter_round(work, 1, 6, 11, 12)
        _quarter_round(work, 2, 7, 8, 13)
        _quarter_round(work, 3, 4, 9, 14)
    out = [(work[i] + state[i]) & 0xFFFFFFFF for i in range(16)]
    return struct.pack("<16I", *out)


def chacha20_xor(key: bytes, nonce: bytes, data: bytes) -> bytes:
    """ChaCha20 流加密（对称，加解密同一个函数）。

    整块异或用大整数一次做完，比逐字节循环快一个量级——平板上这点差距
    决定了「同步一次要等 3 秒还是 30 秒」。
    """
    if not data:
        return b""
    stream = bytearray()
    blocks = (len(data) + 63) // 64
    for i in range(blocks):
        stream += chacha20_block(key, i + 1, nonce)
    ks = bytes(stream[:len(data)])
    return (int.from_bytes(data, "big") ^ int.from_bytes(ks, "big")).to_bytes(len(data), "big")


# ---------------- 密钥派生与封装 ----------------

class SyncCrypto:
    """一套密码派生出来的主密钥。一个实例可反复加解密多个对象。"""

    def __init__(self, enc_root: bytes, mac_root: bytes, salt: bytes, iters: int):
        self.enc_root = enc_root
        self.mac_root = mac_root
        self.salt = salt
        self.iters = iters

    # -- 构造 --

    @classmethod
    def create(cls, password: str, iters: int = DEFAULT_ITERS) -> "SyncCrypto":
        """首次启用同步：生成盐并派生主密钥。"""
        if not password:
            raise CryptoError("同步密码不能为空")
        return cls.derive(password, os.urandom(SALT_LEN), iters)

    @classmethod
    def derive(cls, password: str, salt: bytes, iters: int = DEFAULT_ITERS) -> "SyncCrypto":
        """由密码 + 盐重新派生（换设备/重启后用）。"""
        if not password:
            raise CryptoError("同步密码不能为空")
        if len(salt) != SALT_LEN:
            raise CryptoError("盐长度不对（应为 %d 字节）" % SALT_LEN)
        material = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                       salt, max(10_000, int(iters)), dklen=64)
        return cls(material[:32], material[32:], salt, int(iters))

    @property
    def kdf_params(self) -> dict:
        """写进云端 meta.json 的公开参数（不含密钥）。"""
        return {"algo": "pbkdf2-sha256", "iters": self.iters,
                "salt": self.salt.hex()}

    def verifier(self) -> str:
        """密码校验值。存在云端 meta.json，新设备输密码时先比对。"""
        return hmac.new(self.mac_root, _VERIFY_LABEL, hashlib.sha256).hexdigest()

    def check_verifier(self, expected: str) -> bool:
        if not expected:
            return True
        return hmac.compare_digest(self.verifier(), expected.lower())

    # -- 加解密 --

    def _sub_keys(self, nonce: bytes):
        enc_key = hmac.new(self.enc_root, b"enc" + nonce, hashlib.sha256).digest()
        mac_key = hmac.new(self.mac_root, b"mac" + nonce, hashlib.sha256).digest()
        return enc_key, mac_key

    def encrypt(self, plaintext: bytes) -> bytes:
        nonce = os.urandom(NONCE_LEN)
        enc_key, mac_key = self._sub_keys(nonce)
        ct = chacha20_xor(enc_key, nonce, plaintext)
        mac = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
        return MAGIC + nonce + mac + ct

    def decrypt(self, blob: bytes) -> bytes:
        if not blob or len(blob) < len(MAGIC) + NONCE_LEN + MAC_LEN:
            raise CryptoError("密文太短或不是本程序的密文")
        if not blob.startswith(MAGIC):
            raise CryptoError("密文标记不匹配（可能是旧格式或明文）")
        pos = len(MAGIC)
        nonce = blob[pos:pos + NONCE_LEN]
        pos += NONCE_LEN
        mac = blob[pos:pos + MAC_LEN]
        pos += MAC_LEN
        ct = blob[pos:]
        enc_key, mac_key = self._sub_keys(nonce)
        expect = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expect):
            raise CryptoError("校验失败：密码不对，或数据在传输中被改动")
        return chacha20_xor(enc_key, nonce, ct)

    # -- 文件级（同步载荷都是文件，小到几 MB，整读即可） --

    def encrypt_file(self, src: str, dst: str) -> dict:
        with open(src, "rb") as fp:
            plain = fp.read()
        blob = self.encrypt(plain)
        with open(dst, "wb") as fp:
            fp.write(blob)
        return {"plain_size": len(plain), "blob_size": len(blob),
                "sha256": hashlib.sha256(blob).hexdigest()}

    def decrypt_file(self, src: str, dst: str) -> dict:
        with open(src, "rb") as fp:
            blob = fp.read()
        plain = self.decrypt(blob)
        with open(dst, "wb") as fp:
            fp.write(plain)
        return {"plain_size": len(plain), "blob_size": len(blob),
                "sha256": hashlib.sha256(blob).hexdigest()}
