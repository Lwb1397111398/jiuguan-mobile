# -*- coding: utf-8 -*-
"""云同步引擎：电脑 ↔ 平板 的项目/全局配置双向同步（桌面/安卓共用）。

## 同步的是什么
- 一个项目 = 一本小说 = 一个 `project.db`，云端一个对象。
- 全局配置 = `app.db`（模型档案/路由/偏好卡/预设/任务模板/全局设置）+ `jailbreaks/*.md`。

## 同步不是什么
- **不是章节级合并**，也不是把 SQLite 放到云上直接读写（物理上做不到）。
- 冲突按老板拍板的「谁最后同步谁覆盖」（LWW）处理，但**绝不静默丢数据**：
  推送前把云端旧版归档进 history（保留 N 版），拉取前把本机旧库存进
  `backups/sync_overwrite_*.db`。

## 云端布局
```
jiuguan-sync/v1/
├── meta.json                          # kdf 参数 + 密码校验值 + 版本
├── devices/<id>.json                  # 心跳（谁最后动过云端）
├── global/manifest.json + app.db.enc  # 全局库（gzip → 加密）
├── global/jailbreaks/<名>.md.enc
├── projects/<uuid>/manifest.json      # {title, sha256, size, updated_at, device, schema}
├── projects/<uuid>/project.db.enc
└── history/projects/<uuid>/<ts>.db.enc
```

## 项目删除 = 墓碑（tombstone），不是删对象
删一本书时只把它的 manifest 标成 `deleted=true`，**数据对象和历史版本都留着**。
另一端同步看到墓碑，把本地文件夹「移动」到 `.sync_trash/`（绝不 rmtree）并
从最近项目移除。云端对象不删，才能随时反悔；push/pull 遇到墓碑一律拒绝，
否则本地旧版会把已删除的书「复活」。

## 三个关键决定（都踩过坑）
1. **不用 ETag 判版本**：网关的 ETag 可能是 multipart 格式，也可能压根不给，
   一律用 manifest 里的 sha256。
2. **写顺序**：先 PUT 数据对象，成功后再 PUT manifest。反过来会在断网时留下
   「清单指着一个不存在的对象」，另一端每次同步都报错。
3. **同步是整库覆盖，不是逐条合并**：拉取按表 `DELETE` + 整表 `INSERT`，
   所以「删除行」能传播；但两台同时改时按 LWW 整本赢，不会合并两边改动。
"""
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile

from . import db, sync_config
from .exporter import _rekey_db_copy
from .paths import app_data_dir
from .s3 import S3Error
from .store import ProjectStore, merge_table_copy, _sql_name
from .sync_crypto import CryptoError

HISTORY_DEFAULT = 5
_PROJECT_SKIP = ProjectStore.SYNC_SKIP_TABLES
TOMBSTONE_KEY = "deleted"        # manifest 上的「这本书已删」标记
TRASH_DIR = ".sync_trash"        # 另一端收到墓碑后把文件夹挪到这里，绝不真删


def db_content_fingerprint(conn, skip=()) -> str:
    """库的「内容指纹」：逐表逐行算 sha256，与页布局无关。

    为什么不用文件 sha：全局库要先转成可移植密钥格式才能上传，而
    `encrypt_key` 的 DPAPI 输出**每次都不一样**（内含随机盐），拿密文
    或转格式后的文件做比对，会永远判成「本地有改动」，每次同步都白传一遍。
    所以指纹只认逻辑内容。
    """
    h = hashlib.sha256()
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for tbl in tables:
        if tbl in skip:
            continue
        h.update(b"T:" + tbl.encode("utf-8"))
        cols = [c[1] for c in conn.execute("PRAGMA main.table_info(%s)" % _sql_name(tbl))]
        if not cols:
            continue
        collist = ", ".join(_sql_name(c) for c in cols)
        for row in conn.execute("SELECT %s FROM main.%s ORDER BY rowid"
                                % (collist, _sql_name(tbl))):
            for v in row:
                if v is None:
                    h.update(b"\x00N")
                elif isinstance(v, bytes):
                    h.update(b"\x00B" + v)
                else:
                    h.update(b"\x00S" + str(v).encode("utf-8"))
    return h.hexdigest()


class NeedPassword(Exception):
    """本机没有可用的同步密钥（首次使用 / 换了机器 / DPAPI 失效）。"""


class NotConfigured(Exception):
    """连接参数不全或云端未初始化。"""


# ---------------- 文件小工具 ----------------

def gzip_file(src: str, dst: str, level: int = 6) -> int:
    """压缩到 dst，返回体积。

    mtime=0 让同样的内容永远压出同样的字节——这样「有没有变」可以靠 sha 判断，
    不会因为压缩时间戳不同而每次都误判成新版本。
    """
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        with gzip.GzipFile(filename="", mode="wb", fileobj=fo,
                           compresslevel=level, mtime=0) as gz:
            shutil.copyfileobj(fi, gz, 1 << 20)
    return os.path.getsize(dst)


def gunzip_file(src: str, dst: str) -> int:
    with gzip.open(src, "rb") as fi, open(dst, "wb") as fo:
        shutil.copyfileobj(fi, fo, 1 << 20)
    return os.path.getsize(dst)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        while True:
            chunk = fp.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def read_meta_ro(db_path: str) -> dict:
    """只读打开一个 project.db 只取 meta（不建库、不跑迁移、不写 WAL）。"""
    if not os.path.isfile(db_path):
        return {}
    uri = "file:%s?mode=ro" % db_path.replace("\\", "/")
    try:
        con = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return {}
    try:
        rows = con.execute("SELECT key, value FROM meta").fetchall()
        return {str(k): str(v) for k, v in rows}
    except sqlite3.Error:
        return {}
    finally:
        con.close()


def project_uuid_of(title: str, created_at: str) -> str:
    """由「书名 + 建库时间」确定性算出项目身份。

    两端现有项目都是从同一份备份包派生的，标题与 created_at 一致，所以
    首次绑定必然算出同一个 uuid——这是「同一本书不会变成两本」的关键。
    已写进 meta 的项目永不重算，之后改名也不影响身份。
    """
    raw = (str(title or "") + "|" + str(created_at or "")).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def list_local_projects(app_state) -> list:
    """列出本机所有可用项目（含 uuid / 标题 / 路径）。"""
    out, seen = [], set()
    for r in app_state.list_recent():
        path = r.get("path") or ""
        if not path or not r.get("exists"):
            continue
        norm = os.path.normcase(os.path.abspath(path))
        if norm in seen:
            continue
        seen.add(norm)
        meta = read_meta_ro(os.path.join(path, "project.db"))
        title = meta.get("project_title") or r.get("title") or os.path.basename(path)
        uid = meta.get("project_uuid") or project_uuid_of(title, meta.get("created_at", ""))
        out.append({"path": path, "title": title, "uuid": uid})
    return out


def sync_uid_for_path(path: str) -> str:
    """只读 project.db 拿项目身份。必须在删文件夹**之前**调用。"""
    meta = read_meta_ro(os.path.join(path, "project.db"))
    return meta.get("project_uuid") or project_uuid_of(
        meta.get("project_title") or os.path.basename(path.rstrip("/\\")),
        meta.get("created_at", ""))


def mark_deleted_bg(app_state, path: str, uid: str = "", title: str = "") -> None:
    """UI 删除项目后调：后台写墓碑。任何失败都只记日志，绝不影响删除动作。

    云同步没开 / 没配好 / 断网时直接返回——本地删除照样生效，只是不传播。
    uid 在这里**同步**算好再交给线程：桌面的「删除文件夹」会立刻 rmtree，
    后台线程再去读 project.db 就已经读不到了。
    """
    import threading
    uid = uid or sync_uid_for_path(path)

    def run():
        try:
            cfg = sync_config.load_config()
            if not sync_config.is_configured(cfg):
                return
            g = getattr(app_state, "gstore", None)
            if g is not None and g.get_global_setting("sync_enabled", "0") != "1":
                return
            eng = SyncEngine(cfg=cfg)
            eng.bind("")
            eng.mark_project_deleted(app_state, path, uid=uid, title=title)
        except Exception as e:      # 网络/密钥/密码任何问题都不能弹到用户面前
            try:
                sync_config.SyncLog()("[sync] 写删除标记失败：%s %s"
                                      % (type(e).__name__, str(e)[:160]))
            except Exception:
                pass

    threading.Thread(target=run, daemon=True, name="sync-tombstone").start()


class SyncEngine:
    """一次同步会话。用完即弃，别跨线程共享（S3Client 持 Session）。"""

    def __init__(self, cfg: dict = None, state: dict = None, log=None,
                 progress=None):
        self.cfg = cfg if cfg is not None else sync_config.load_config()
        self.state = state if state is not None else sync_config.load_state()
        self.log = log or sync_config.SyncLog()
        self.progress = progress or (lambda msg: None)
        self._cli = None

    # ---------- 基础设施 ----------

    @property
    def client(self):
        if self._cli is None:
            self._cli = sync_config.make_client(self.cfg, log=self.log)
        return self._cli

    @property
    def base(self) -> str:
        return (self.cfg.get("prefix") or "jiuguan-sync/v1").strip("/")

    def key(self, *parts) -> str:
        seg = [self.base] + [str(p).strip("/") for p in parts if p != ""]
        return "/".join(seg)

    def crypto(self):
        c = sync_config.get_crypto(self.cfg)
        if c is None:
            raise NeedPassword("本机还没有同步密钥，请先输入一次同步密码")
        return c

    def save_state(self) -> None:
        sync_config.save_state(self.state)

    def _touch(self, message: str) -> None:
        self.log("[sync] " + message)
        self.progress(message)

    # ---------- 云端元信息与绑定 ----------

    def read_meta(self) -> dict:
        try:
            return json.loads(self.client.get_bytes(self.key("meta.json")).decode("utf-8"))
        except S3Error as e:
            if e.status == 404:
                return {}
            raise

    def write_meta(self, meta: dict) -> None:
        self.client.put_bytes(self.key("meta.json"),
                              json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"),
                              "application/json")

    def bind(self, password: str = "") -> dict:
        """绑定云端空间。

        第一次（云端没有 meta.json）：用 password 建空间。
        第二台设备：按云端 meta 里的盐派生同一把主密钥，并校验 verifier。
        本机已有可用密钥且能通过云端校验：直接复用，不需要密码。
        """
        meta = self.read_meta()
        if not meta:
            if not password:
                raise NeedPassword("云端还没有同步空间，请先设置同步密码")
            crypto = sync_config.set_password(self.cfg, password)
            meta = {"version": 1, "app": "jiuguan-novel", "created_at": db.now(),
                    "kdf": crypto.kdf_params, "verifier": crypto.verifier()}
            self.write_meta(meta)
            self._touch("已在云端建立同步空间")
            return meta
        verifier = str(meta.get("verifier") or "")
        current = sync_config.get_crypto(self.cfg)
        if current is not None and current.check_verifier(verifier):
            if not self.cfg.get("password_verifier"):
                self.cfg["password_verifier"] = current.verifier()
                sync_config.save_config(self.cfg)
            return meta
        if not password:
            raise NeedPassword("本机没有同步密钥，请输入同步密码完成绑定")
        sync_config.adopt_password(self.cfg, password, meta.get("kdf") or {}, verifier)
        self._touch("已用同步密码绑定云端空间")
        return meta

    def heartbeat(self) -> None:
        """写一条设备心跳。另一端据此显示「电脑 3 分钟前同步过」。"""
        data = {"device_id": self.cfg.get("device_id", ""),
                "device_name": self.cfg.get("device_name", ""),
                "at": db.now(), "schema": db.SCHEMA_VERSION}
        try:
            self.client.put_bytes(self.key("devices", self.cfg.get("device_id", "unknown") + ".json"),
                                  json.dumps(data, ensure_ascii=False).encode("utf-8"),
                                  "application/json")
        except S3Error as e:
            self.log("[sync] 心跳写入失败：" + str(e)[:120])

    # ---------- 项目身份 ----------

    def ensure_uuid(self, store: ProjectStore) -> str:
        uid = store.get_meta("project_uuid", "")
        if uid:
            return uid
        title = store.title
        uid = project_uuid_of(title, store.get_meta("created_at", ""))
        store.set_meta("project_uuid", uid)
        return uid

    def get_manifest(self, uid: str) -> dict:
        try:
            return json.loads(self.client.get_bytes(
                self.key("projects", uid, "manifest.json")).decode("utf-8"))
        except S3Error as e:
            if e.status == 404:
                return {}
            raise

    def put_manifest(self, uid: str, manifest: dict) -> None:
        self.client.put_bytes(self.key("projects", uid, "manifest.json"),
                              json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
                              "application/json")

    def list_remote_projects(self) -> list:
        """列出云端所有项目（按 manifest 前缀解析，不需要额外索引文件）。"""
        out = []
        for item in self.client.list_all(self.key("projects") + "/"):
            parts = item["key"].split("/")
            if len(parts) >= 2 and parts[-1] == "manifest.json":
                uid = parts[-2]
                try:
                    m = json.loads(self.client.get_bytes(item["key"]).decode("utf-8"))
                except (S3Error, ValueError):
                    continue
                m.setdefault("uuid", uid)
                out.append(m)
        return out

    # ---------- 历史版本 ----------

    def archive_remote(self, uid: str, manifest: dict) -> None:
        """把云端当前版本归档进 history，再裁剪到 N 版。失败只记日志，不阻断同步。"""
        try:
            keep = int(self.cfg.get("history_keep") or HISTORY_DEFAULT)
        except (TypeError, ValueError):
            keep = HISTORY_DEFAULT
        if keep <= 0:
            return
        src = self.key("projects", uid, "project.db.enc")
        stamp = (manifest.get("updated_at") or db.now()).replace(
            ":", "").replace("-", "").replace(" ", "T")
        # 尾缀带上版本 sha 前 8 位：时间戳只到秒，同一秒内连推两次会撞成同一个
        # key，后一次把前一次的历史覆盖掉（2026-11-16 单测抓到）。
        tag = (manifest.get("sha256") or "")[:8] or "v"
        dst = self.key("history", "projects", uid, stamp + "-" + tag + ".db.enc")
        try:
            self.client.copy(src, dst)
        except S3Error as e:
            if e.code != "CopyUnsupported":
                self.log("[sync] 归档旧版失败：" + str(e)[:120])
                return
            # 网关不支持服务端复制：小对象退回下载+上传，大对象就放弃归档
            try:
                blob = self.client.get_bytes(src, limit=8 << 20)
                self.client.put_bytes(dst, blob)
            except S3Error as e2:
                self.log("[sync] 归档旧版失败（回退同样失败）：" + str(e2)[:120])
                return
        self.trim_history(uid, keep)

    def trim_history(self, uid: str, keep: int) -> None:
        try:
            items = self.client.list_all(self.key("history", "projects", uid) + "/")
        except S3Error:
            return
        items.sort(key=lambda x: x["key"])
        for item in items[:-keep] if keep > 0 else items:
            try:
                self.client.delete(item["key"])
            except S3Error:
                pass

    def history_versions(self, uid: str) -> list:
        try:
            items = self.client.list_all(self.key("history", "projects", uid) + "/")
        except S3Error:
            return []
        items.sort(key=lambda x: x["key"], reverse=True)
        return items

    # ---------- 单个项目 ----------

    def _cursor(self, uid: str) -> dict:
        return sync_config.cursor_for(self.state, uid)

    def is_dirty(self, store: ProjectStore, cur: dict) -> bool:
        base = cur.get("local_seq")
        if base is None:
            return True  # 从没同步过
        try:
            return int(base) != int(store.write_seq)
        except (TypeError, ValueError):
            return True

    def push_project(self, store: ProjectStore, uid: str = "") -> dict:
        uid = uid or self.ensure_uuid(store)
        title = store.title
        remote = self.get_manifest(uid)
        if remote.get(TOMBSTONE_KEY):
            # 这本书已在别的设备删除。绝不能让本地旧版推回去把它「复活」——
            # 这是无条件 push 最危险的口子，必须在这里堵死。
            self.log("[sync] 《%s》云端已标记删除，拒绝上传" % title)
            return {"action": "tombstoned", "uuid": uid, "title": title}
        self._touch("上传《" + title + "》…")
        tmp = tempfile.mkdtemp(prefix="jgsync_push_")
        try:
            snap = os.path.join(tmp, "project.db")
            info = store.snapshot_to(snap, _PROJECT_SKIP)
            gz = os.path.join(tmp, "project.db.gz")
            gzip_file(snap, gz)
            crypto = self.crypto()
            enc = os.path.join(tmp, "project.db.enc")
            crypto.encrypt_file(gz, enc)
            digest = sha256_file(enc)
            size = os.path.getsize(enc)

            if remote and remote.get("sha256") and remote["sha256"] != digest:
                # 覆盖前先把云端旧版收进 history：这是「覆盖」唯一的后悔药
                self.archive_remote(uid, remote)

            self.client.put_file(self.key("projects", uid, "project.db.enc"), enc)
            manifest = {"uuid": uid, "title": title, "sha256": digest, "size": size,
                        "plain_size": info.get("size", 0),
                        "updated_at": db.now(), "device": self.cfg.get("device_id", ""),
                        "device_name": self.cfg.get("device_name", ""),
                        "schema_version": db.SCHEMA_VERSION}
            self.put_manifest(uid, manifest)

            cur = self._cursor(uid)
            cur.update({"remote_sha": digest, "remote_at": manifest["updated_at"],
                        "local_seq": store.write_seq, "title": title,
                        "pushed_by": self.cfg.get("device_id", "")})
            self.save_state()
            self._touch("已上传《" + title + "》%.1f KB" % (size / 1024.0))
            return {"action": "push", "uuid": uid, "size": size, "sha256": digest}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def pull_project(self, store: ProjectStore, uid: str = "", backup=None) -> dict:
        uid = uid or self.ensure_uuid(store)
        manifest = self.get_manifest(uid)
        if not manifest:
            return {"action": "none", "reason": "云端没有这本书"}
        if manifest.get(TOMBSTONE_KEY):
            return {"action": "tombstoned", "uuid": uid,
                    "title": manifest.get("title", store.title)}
        if backup is None:
            # 本机没改动就不必留快照（内容与云端一致，留了也是垃圾文件）
            backup = self.is_dirty(store, self._cursor(uid))
        self._touch("下载《" + store.title + "》…")
        tmp = tempfile.mkdtemp(prefix="jgsync_pull_")
        try:
            enc = os.path.join(tmp, "project.db.enc")
            got = self.client.download_to(self.key("projects", uid, "project.db.enc"), enc)
            if manifest.get("sha256") and got["sha256"] != manifest["sha256"]:
                raise S3Error("下载内容校验失败（sha256 与清单不符），已放弃本次拉取",
                              code="Checksum")
            gz = os.path.join(tmp, "project.db.gz")
            crypto = self.crypto()
            crypto.decrypt_file(enc, gz)
            snap = os.path.join(tmp, "project.db")
            gunzip_file(gz, snap)

            if backup:
                path = store.backup(prefix="sync_overwrite")
                self.log("[sync] 覆盖前已存快照：" + os.path.basename(path))

            res = store.merge_from(snap)
            if not res.get("ok"):
                return {"action": "failed", "reason": res.get("reason", "合并失败"),
                        "uuid": uid}

            cur = self._cursor(uid)
            cur.update({"remote_sha": manifest.get("sha256", ""),
                        "remote_at": manifest.get("updated_at", ""),
                        "local_seq": store.write_seq,
                        "title": manifest.get("title", store.title),
                        "pushed_by": manifest.get("device", "")})
            self.save_state()
            self._touch("已合并云端《" + store.title + "》" +
                        str(res.get("tables", 0)) + " 张表")
            return {"action": "pull", "uuid": uid, "tables": res.get("tables", 0),
                    "size": got["size"]}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def sync_project(self, store: ProjectStore, direction: str = "auto",
                     uid: str = "", remote: dict = None) -> dict:
        """按决策表处理一本书。direction: auto / push / pull / safe_push。

        remote 由 sync_all 传入（它已经查过一次），避免每本书多一次网络往返。
        safe_push 只给后台自动同步用：云端有更新时让位，绝不静默覆盖。
        """
        uid = uid or self.ensure_uuid(store)
        cur = self._cursor(uid)
        remote = self.get_manifest(uid) if remote is None else remote
        dirty = self.is_dirty(store, cur)

        if remote.get(TOMBSTONE_KEY):
            return {"action": "tombstoned", "uuid": uid,
                    "title": remote.get("title", store.title)}

        if direction == "safe_push":
            # 自动同步专用：绝不静默覆盖任何一边。
            # 云端被另一台设备动过就一律 defer —— 不推（否则本机旧版会把对方
            # 刚写的整本盖掉，即「删除复活」），也不拉（用户可能正在写作台打字，
            # 后台整表替换会吞掉未存内容）。冲突留给手动同步按 LWW 裁决。
            if remote and remote.get("sha256") != cur.get("remote_sha"):
                self.log("[sync] 《%s》云端有更新，自动同步让位给手动同步" % store.title)
                return {"action": "deferred", "uuid": uid,
                        "reason": "云端有更新，请点「立即同步」"}
            return self.push_project(store, uid)

        if direction == "push":
            return self.push_project(store, uid)
        if direction == "pull":
            return self.pull_project(store, uid) if remote else \
                {"action": "none", "reason": "云端没有这本书"}

        if not remote:
            return self.push_project(store, uid)
        remote_changed = remote.get("sha256") != cur.get("remote_sha")
        if not remote_changed:
            if dirty:
                return self.push_project(store, uid)
            return {"action": "skip", "balance": "up-to-date"}
        if not dirty:
            return self.pull_project(store, uid)
        # 双改：谁最后动过谁赢
        remote_at = str(remote.get("updated_at") or "")
        local_at = str(store.last_write_at or "")
        if self._remote_wins(remote_at, local_at):
            self.log("[sync] 冲突：云端 %s 晚于本机 %s，按 LWW 拉取" % (remote_at, local_at))
            return self.pull_project(store, uid)
        self.log("[sync] 冲突：本机 %s 晚于云端 %s，按 LWW 推送" % (local_at, remote_at))
        return self.push_project(store, uid)

    @staticmethod
    def _remote_wins(remote_at: str, local_at: str) -> bool:
        """云端是否更晚。时钟差 90 秒以内判本机赢（人在本机，别抢他刚写的）。"""
        if not remote_at:
            return False
        if not local_at:
            return True
        if remote_at > local_at:
            try:
                from datetime import datetime
                d = (datetime.fromisoformat(remote_at) -
                     datetime.fromisoformat(local_at)).total_seconds()
                if abs(d) < 90:
                    return False
            except ValueError:
                pass
            return True
        return False

    # ---------- 全局配置 ----------

    def _gstore_snapshot(self, app_state, dst: str = "") -> str:
        """全局库一致性快照（读事务里生成，WAL 里的最新写入也会包含）。"""
        gstore = app_state.gstore
        if not dst:
            dst = os.path.join(tempfile.mkdtemp(prefix="jgsync_global_"), "app.db")
        if os.path.isfile(dst):
            os.remove(dst)
        with gstore._lock:
            gstore.conn.execute("VACUUM INTO ?", (dst,))
        return dst

    def push_global(self, app_state) -> dict:
        self._touch("上传全局配置…")
        tmp = tempfile.mkdtemp(prefix="jgsync_gpush_")
        try:
            local_sha = self.global_fingerprint(app_state)
            raw = self._gstore_snapshot(app_state, os.path.join(tmp, "app.raw.db"))
            # 换成可移植密钥格式（DPAPI 密文换机解不开），再整体加密
            portable = os.path.join(tmp, "app.portable.db")
            _rekey_db_copy(raw, portable, to_portable=True)
            gz = os.path.join(tmp, "app.db.gz")
            gzip_file(portable, gz)
            crypto = self.crypto()
            enc = os.path.join(tmp, "app.db.enc")
            crypto.encrypt_file(gz, enc)
            digest = sha256_file(enc)

            old = self._get_json(self.key("global", "manifest.json"))
            if old and old.get("sha256") and old["sha256"] != digest:
                stamp = (old.get("updated_at") or db.now()).replace(
                    ":", "").replace("-", "").replace(" ", "T")
                try:
                    self.client.copy(self.key("global", "app.db.enc"),
                                     self.key("history", "global",
                                              stamp + "-" + old["sha256"][:8] + ".db.enc"))
                except S3Error as e:
                    self.log("[sync] 全局旧版归档失败：" + str(e)[:120])

            self.client.put_file(self.key("global", "app.db.enc"), enc)
            manifest = {"sha256": digest, "size": os.path.getsize(enc),
                        "updated_at": db.now(), "device": self.cfg.get("device_id", ""),
                        "device_name": self.cfg.get("device_name", "")}
            self.client.put_bytes(self.key("global", "manifest.json"),
                                  json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
                                  "application/json")
            files = self._push_jailbreaks(crypto)
            self.state.setdefault("global", {}).update(
                {"remote_sha": digest, "remote_at": manifest["updated_at"],
                 "files": files, "local_sha": local_sha})
            self.save_state()
            self._touch("全局配置已上传（%.1f KB，破甲 %d 个）"
                        % (manifest["size"] / 1024.0, len(files)))
            return {"action": "push", "size": manifest["size"], "files": len(files)}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _push_jailbreaks(self, crypto) -> dict:
        """上传破甲库。返回 {文件名: sha256}。单个失败不影响整体。"""
        folder = os.path.join(app_data_dir(), "jailbreaks")
        out = {}
        if not os.path.isdir(folder):
            return out
        for name in sorted(os.listdir(folder)):
            src = os.path.join(folder, name)
            if not name.lower().endswith(".md") or not os.path.isfile(src):
                continue
            tmpdir = tempfile.mkdtemp(prefix="jgsync_jb_")
            try:
                gz = os.path.join(tmpdir, "a.gz")
                gzip_file(src, gz)
                enc = os.path.join(tmpdir, "a.enc")
                crypto.encrypt_file(gz, enc)
                self.client.put_file(self.key("global", "jailbreaks", name + ".enc"), enc)
                out[name] = sha256_file(enc)
            except (S3Error, OSError) as e:
                self.log("[sync] 破甲 %s 上传失败：%s" % (name, str(e)[:100]))
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
        return out

    def pull_global(self, app_state) -> dict:
        manifest = self._get_json(self.key("global", "manifest.json"))
        if not manifest:
            return {"action": "none", "reason": "云端没有全局配置"}
        tmp = tempfile.mkdtemp(prefix="jgsync_gpull_")
        try:
            self._touch("下载全局配置…")
            enc = os.path.join(tmp, "app.db.enc")
            got = self.client.download_to(self.key("global", "app.db.enc"), enc)
            if manifest.get("sha256") and got["sha256"] != manifest["sha256"]:
                raise S3Error("全局配置下载校验失败（sha256 不符）", code="Checksum")
            gz = os.path.join(tmp, "app.db.gz")
            crypto = self.crypto()
            crypto.decrypt_file(enc, gz)
            portable = os.path.join(tmp, "app.portable.db")
            gunzip_file(gz, portable)
            local_db = os.path.join(tmp, "app.local.db")
            _rekey_db_copy(portable, local_db, to_portable=False)

            backup_dir = os.path.join(app_state.data_dir, "backups")
            os.makedirs(backup_dir, exist_ok=True)
            stamp = db.now().replace(":", "").replace("-", "")
            try:
                shutil.copy2(os.path.join(app_state.data_dir, "app.db"),
                             os.path.join(backup_dir, "app_before_sync_%s.db" % stamp))
            except OSError:
                pass

            with app_state.gstore._lock:
                res = merge_table_copy(app_state.gstore.conn, local_db, (), 0)
            if not res.get("ok"):
                return {"action": "failed", "reason": res.get("reason", "合并失败")}
            files = self._pull_jailbreaks(crypto)
            # 合并完立刻取指纹：否则接着判「本机有改动」又白推一次
            local_sha = self.global_fingerprint(app_state)
            self.state.setdefault("global", {}).update(
                {"remote_sha": manifest.get("sha256", ""),
                 "remote_at": manifest.get("updated_at", ""), "files": files,
                 "local_sha": local_sha})
            self.save_state()
            self._touch("全局配置已合并（%d 张表，破甲 %d 个）"
                        % (res.get("tables", 0), files))
            return {"action": "pull", "tables": res.get("tables", 0), "files": files}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _pull_jailbreaks(self, crypto) -> int:
        """下载云端破甲。只覆盖/新增，**不删本机文件**（删文件太危险）。"""
        folder = os.path.join(app_data_dir(), "jailbreaks")
        os.makedirs(folder, exist_ok=True)
        n = 0
        try:
            items = self.client.list_all(self.key("global", "jailbreaks") + "/")
        except S3Error as e:
            self.log("[sync] 列举破甲失败：" + str(e)[:120])
            return 0
        for item in items:
            name = item["key"].rsplit("/", 1)[-1]
            if not name.endswith(".md.enc"):
                continue
            tmpdir = tempfile.mkdtemp(prefix="jgsync_jb_")
            try:
                enc = os.path.join(tmpdir, "a.enc")
                self.client.download_to(item["key"], enc)
                gz = os.path.join(tmpdir, "a.gz")
                crypto.decrypt_file(enc, gz)
                dst = os.path.join(folder, name[:-4])
                gunzip_file(gz, dst)
                n += 1
            except (S3Error, CryptoError, OSError) as e:
                self.log("[sync] 破甲 %s 下载失败：%s" % (name, str(e)[:100]))
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
        return n

    def global_fingerprint(self, app_state) -> str:
        gstore = app_state.gstore
        with gstore._lock:
            return db_content_fingerprint(gstore.conn)

    @staticmethod
    def _app_db_mtime(app_state) -> str:
        try:
            ts = os.path.getmtime(os.path.join(app_state.data_dir, "app.db"))
        except OSError:
            return ""
        from datetime import datetime
        return datetime.fromtimestamp(ts).isoformat(timespec="seconds")

    def sync_global(self, app_state, direction: str = "auto") -> dict:
        """全局配置同步。项目用 write_seq 判脏，全局库只能用内容指纹。"""
        cur = self.state.setdefault("global", {})
        manifest = self._get_json(self.key("global", "manifest.json"))
        if direction == "push" or not manifest:
            return self.push_global(app_state)
        local_sha = self.global_fingerprint(app_state)
        last_sha = str(cur.get("local_sha") or "")
        remote_changed = manifest.get("sha256") != cur.get("remote_sha")
        local_changed = (not last_sha) or (local_sha != last_sha)

        if direction == "pull":
            return self.pull_global(app_state) if remote_changed else {"action": "skip"}
        if not remote_changed and not local_changed:
            return {"action": "skip"}
        if remote_changed and local_changed:
            # 两边都动过：谁最后改谁赢（与项目同一套规则）
            remote_at = str(manifest.get("updated_at") or "")
            local_at = self._app_db_mtime(app_state)
            self.log("[sync] 全局配置冲突：云端 %s / 本机 %s" % (remote_at, local_at))
            return self.pull_global(app_state) if self._remote_wins(remote_at, local_at) \
                else self.push_global(app_state)
        if remote_changed:
            return self.pull_global(app_state)
        return self.push_global(app_state)

    # ---------- 编排 ----------

    def _open_for_sync(self, app_state, path: str):
        """打开一个项目用于同步。返回 (store, 是否临时打开)。

        当前正在用的那个项目必须复用现有连接——再开一条会变成双写。
        """
        cur = getattr(app_state, "store", None)
        if cur is not None and not getattr(cur, "is_closed", False):
            if os.path.normcase(os.path.abspath(cur.folder)) == \
                    os.path.normcase(os.path.abspath(path)):
                return cur, False
        return ProjectStore(path, getattr(app_state, "gstore", None)), True

    def sync_all(self, app_state, include_global: bool = True,
                 direction: str = "auto") -> dict:
        """同步全部项目（+全局配置）。返回统计。

        一轮做三件事：① 云端标了删除的书 → 本地移进回收站；② 本地每本书按
        LWW 推/拉；③ 云端有、本机没有的书 → 自动落到本机（另一端新建的书
        不用再手动点「从云端恢复」）。
        """
        stats = {"push": 0, "pull": 0, "skip": 0, "deleted": 0,
                 "adopted": 0, "failed": 0, "errors": []}
        projects = list_local_projects(app_state)
        total = len(projects)
        for i, item in enumerate(projects, 1):
            tag = "(%d/%d) " % (i, total)
            try:
                remote = self.get_manifest(item["uuid"])
            except S3Error as e:
                stats["failed"] += 1
                stats["errors"].append(item["title"] + "：读清单失败 " + str(e)[:100])
                self.log("[sync] " + tag + item["title"] + " 读清单失败：" + str(e)[:150])
                continue
            if remote.get(TOMBSTONE_KEY):
                r = self.quarantine_project(app_state, item)
                stats["deleted"] += 1
                self.log("[sync] " + tag + "《%s》云端已删除 → %s"
                         % (item["title"], "移进回收站" if r.get("ok")
                            else r.get("reason", "仅移除记录")))
                if not r.get("ok"):
                    stats["errors"].append(item["title"] + "：" + r.get("reason", ""))
                continue
            store, temp = self._open_for_sync(app_state, item["path"])
            try:
                r = self.sync_project(store, direction=direction,
                                      uid=item["uuid"], remote=remote)
                act = r.get("action", "skip")
                if act == "tombstoned":
                    stats["deleted"] += 1
                    continue
                if act == "failed":
                    stats["failed"] += 1
                    stats["errors"].append(item["title"] + "：" + r.get("reason", ""))
                elif act in stats:
                    stats[act] += 1
                self.log("[sync] " + tag + item["title"] + " → " + act)
            except (S3Error, CryptoError, NeedPassword) as e:
                stats["failed"] += 1
                stats["errors"].append(item["title"] + "：" + str(e)[:160])
                self.log("[sync] " + item["title"] + " 失败：" + str(e)[:200])
            except Exception as e:  # noqa: BLE001 — 单本失败不能带崩整轮同步
                stats["failed"] += 1
                stats["errors"].append(item["title"] + "：" + type(e).__name__ + str(e)[:160])
                self.log("[sync] " + item["title"] + " 异常：" + type(e).__name__ + str(e)[:200])
            finally:
                if temp:
                    store.close()

        # ③ 另一端新建的书自动落地（只往本机拿，push 方向不拉新书）
        if direction in ("auto", "pull"):
            self._adopt_remote_projects(app_state, projects, stats)

        if include_global:
            try:
                g = self.sync_global(app_state, direction=direction)
                stats["global"] = g
                if g.get("action") == "failed":
                    stats["failed"] += 1
                    stats["errors"].append("全局配置：" + g.get("reason", ""))
            except (S3Error, CryptoError, NeedPassword) as e:
                stats["failed"] += 1
                stats["errors"].append("全局配置：" + str(e)[:160])
                self.log("[sync] 全局配置失败：" + str(e)[:200])

        self.state["last_sync_at"] = db.now()
        self.state["last_error"] = "；".join(stats["errors"])[:400]
        self.save_state()
        self.heartbeat()
        self._touch("同步完成：上传 %d、下载 %d、新书 %d、删除 %d、跳过 %d、失败 %d"
                    % (stats["push"], stats["pull"], stats["adopted"],
                       stats["deleted"], stats["skip"], stats["failed"]))
        return stats

    # ---------- 从云端恢复 / 用量 ----------

    # ---------- 项目删除：墓碑 / 回收站 / 云端新书落地 ----------

    def mark_project_deleted(self, app_state, path: str,
                             uid: str = "", title: str = "") -> dict:
        """把一本书的云端 manifest 标成墓碑。数据对象与历史版本一律保留。

        uid 必须在删文件夹**之前**算好：文件夹没了就读不到 meta.project_uuid，
        再用 title|created_at 重算会算出另一个身份，墓碑就贴到别的书上了。
        """
        if not uid:
            meta = read_meta_ro(os.path.join(path, "project.db"))
            uid = meta.get("project_uuid") or project_uuid_of(
                title or meta.get("project_title", ""), meta.get("created_at", ""))
        if not uid:
            return {"ok": False, "reason": "算不出项目身份，未写墓碑"}
        remote = self.get_manifest(uid)
        if remote.get(TOMBSTONE_KEY):
            return {"ok": True, "already": True, "uuid": uid}
        if remote.get("sha256"):
            self.archive_remote(uid, remote)   # 删之前再留一份，方便反悔
        manifest = dict(remote)
        manifest.update({
            "uuid": uid,
            "title": remote.get("title") or title
                     or os.path.basename(path.rstrip("/\\")),
            TOMBSTONE_KEY: True,
            "deleted_at": db.now(),
            "deleted_by": self.cfg.get("device_id", ""),
            "updated_at": db.now(),
            "device": self.cfg.get("device_id", ""),
            "device_name": self.cfg.get("device_name", ""),
            "schema_version": db.SCHEMA_VERSION,
        })
        # 只写清单，数据对象 project.db.enc 原样留着：反悔时把 deleted 去掉即可
        self.put_manifest(uid, manifest)
        sync_config.drop_cursor(self.state, uid)
        self.save_state()
        self.log("[sync] 《%s》已标记删除（数据仍留在云端）" % manifest["title"])
        return {"ok": True, "uuid": uid, "title": manifest["title"]}

    def quarantine_project(self, app_state, item: dict) -> dict:
        """收到墓碑：把本地文件夹**移动**进 .sync_trash，绝不 rmtree。

        移完从最近项目移除；若正打开这本书，先关连接再移，否则 Windows 上
        文件句柄会挡住移动。移动失败也至少把记录摘掉，别让它一直挂在列表里。
        """
        path = item.get("path", "")
        uid = item.get("uuid", "")
        title = item.get("title", "")
        cur = getattr(app_state, "store", None)
        if cur is not None and not getattr(cur, "is_closed", False):
            try:
                if os.path.normcase(os.path.abspath(cur.folder)) == \
                        os.path.normcase(os.path.abspath(path)):
                    app_state.close_project()
            except Exception:
                pass
        ok, reason = False, ""
        if path and os.path.isdir(path):
            stamp = db.now().replace(":", "").replace("-", "").replace(" ", "T")
            trash = os.path.join(app_state.data_dir, TRASH_DIR)
            dest = os.path.join(trash, (uid or "unknown") + "-" + stamp)
            n = 1
            while os.path.exists(dest):
                dest = os.path.join(trash, (uid or "unknown")
                                   + "-" + stamp + "-%d" % n)
                n += 1
            try:
                os.makedirs(trash, exist_ok=True)
                shutil.move(path, dest)
                ok, reason = True, dest
            except OSError as e:
                reason = "移动回收站失败：" + str(e)[:120]
        else:
            reason = "文件夹已不存在"
        try:
            app_state.remove_recent(path)
        except Exception as e:
            reason += "；移除记录失败：" + str(e)[:80]
        self.log("[sync] 《%s》云端已删 → %s" % (title, reason))
        return {"ok": ok, "reason": reason}

    def _materialize_remote(self, app_state, m: dict) -> dict:
        """云端有、本机没有：建目录 + 空库 → 拉整表 → 挂进最近项目。"""
        uid = m.get("uuid", "")
        if not uid:
            return {"ok": False, "reason": "清单缺 uuid"}
        title = m.get("title") or uid
        dest = os.path.join(app_state.data_dir, "projects", uid)
        os.makedirs(dest, exist_ok=True)
        store = ProjectStore(dest, getattr(app_state, "gstore", None))
        try:
            r = self.pull_project(store, uid, backup=False)
            if r.get("action") == "failed":
                return {"ok": False, "reason": r.get("reason", "合并失败")}
            if r.get("action") == "tombstoned":
                return {"ok": False, "reason": "云端已删除"}
            if r.get("action") == "none":
                return {"ok": False, "reason": "云端没有这本书的数据"}
            app_state.add_recent(dest, store.title or title)
            return {"ok": True, "path": dest, "tables": r.get("tables", 0)}
        finally:
            store.close()

    def _adopt_remote_projects(self, app_state, local_projects, stats) -> None:
        """A：把另一端新建的书自动落到本机，不用再手动点「从云端恢复」。"""
        try:
            remotes = self.list_remote_projects()
        except S3Error as e:
            stats["errors"].append("列举云端项目失败：" + str(e)[:120])
            self.log("[sync] 列举云端项目失败：" + str(e)[:150])
            return
        known = {p["uuid"] for p in local_projects}
        for m in remotes:
            uid = m.get("uuid", "")
            if not uid or uid in known or m.get(TOMBSTONE_KEY):
                continue
            title = m.get("title") or uid
            try:
                r = self._materialize_remote(app_state, m)
            except (S3Error, CryptoError, NeedPassword, OSError) as e:
                r = {"ok": False, "reason": str(e)[:140]}
            except Exception as e:
                r = {"ok": False, "reason": type(e).__name__ + str(e)[:140]}
            if r.get("ok"):
                stats["adopted"] += 1
                known.add(uid)
                self.log("[sync] 新书《%s》已从云端落地（%s 张表）"
                         % (title, r.get("tables", 0)))
            else:
                stats["failed"] += 1
                stats["errors"].append(title + "：" + r.get("reason", ""))
                self.log("[sync] 新书《%s》落地失败：%s"
                         % (title, r.get("reason", "")))

    def restore_all(self, app_state) -> dict:
        """新设备/重装：把云端全部项目落到本机并挂进最近项目列表（跳过墓碑）。"""
        remotes = [m for m in self.list_remote_projects()
                   if not m.get(TOMBSTONE_KEY)]
        if not remotes:
            return {"ok": False, "reason": "云端还没有任何项目"}
        local = {p["uuid"]: p for p in list_local_projects(app_state)}
        done, errors = 0, []
        for i, m in enumerate(remotes, 1):
            uid = m.get("uuid", "")
            title = m.get("title") or uid
            self._touch("恢复 (%d/%d)《%s》" % (i, len(remotes), title))
            try:
                target = local.get(uid)
                if target:
                    store, temp = self._open_for_sync(app_state, target["path"])
                    try:
                        self.pull_project(store, uid)
                    finally:
                        if temp:
                            store.close()
                else:
                    r = self._materialize_remote(app_state, m)
                    if not r.get("ok"):
                        errors.append(title + "：" + r.get("reason", ""))
                        continue
                done += 1
            except (S3Error, CryptoError, OSError) as e:
                errors.append(title + "：" + str(e)[:120])
        return {"ok": not errors, "done": done, "total": len(remotes), "errors": errors}

    def usage(self, refresh: bool = False) -> dict:
        """云端用量（按前缀累加对象体积）。结果缓存 5 分钟，别每次刷界面都去 LIST。"""
        cached = self.state.get("usage") or {}
        if not refresh and cached.get("at"):
            from datetime import datetime, timedelta
            try:
                at = datetime.fromisoformat(cached["at"])
                if datetime.now() - at < timedelta(minutes=5):
                    return cached
            except ValueError:
                pass
        try:
            items = self.client.list_all(self.base + "/")
        except S3Error as e:
            return {"ok": False, "reason": str(e)[:120]}
        total = sum(i["size"] for i in items)
        data = {"ok": True, "at": db.now(), "objects": len(items), "bytes": total,
                "human": "%.1f MB" % (total / 1048576.0)}
        self.state["usage"] = data
        self.save_state()
        return data

    def _get_json(self, key: str) -> dict:
        try:
            return json.loads(self.client.get_bytes(key).decode("utf-8"))
        except S3Error as e:
            if e.status == 404:
                return {}
            raise
        except ValueError:
            return {}
