# -*- coding: utf-8 -*-
"""导出：正文（TXT/Markdown/DOCX）+ 项目打包（zip 跨设备导入）+ 全量备份（双端同步）。"""
import copy
import hashlib
import json
import os
import re
import shutil
import sqlite3
import struct
import tempfile
import zipfile
from datetime import datetime
from xml.sax.saxutils import escape

from .store import ProjectStore
from .chapter_outline import chapter_outline_text
from .api import encrypt_key, decrypt_key
from .paths import is_cache_path
from .sync_crypto import CryptoError, SyncCrypto, DEFAULT_ITERS

EXPORT_FORMATS = ["TXT", "Markdown", "DOCX"]


def chapter_effective_text(store: ProjectStore, chapter_id: int) -> str:
    """本章「当前生效」的正文（导出/统计都用这一份，保证导出与界面一致）。

    取用优先级（越高越优先）：
    1. 整章正文块 `kind='chapter'` 且已接受/已锁定 —— 整章写法和分批合并都落这里；
    2. 已接受/已锁定的分块正文（细纲块写法，一块一段）；
    3. 分批工作区 `prose_batches` 的合并稿 —— 分批写满但**还没点合并**时，
       正文只在工作区里；不兜这一层，导出就会漏掉最新写完的章节。

    整章块仍是「草稿」（AI 刚生成、用户未点接受）时按红线不导出，改用下一优先级。
    """
    tb = store.get_chapter_prose_block(chapter_id)
    tb_id = tb.get("id") if tb else None
    if tb and tb.get("status") in ("已接受", "已锁定") and (tb.get("content") or "").strip():
        return tb["content"].strip()
    accepted = [b for b in store.list_text_blocks(chapter_id)
                if b.get("id") != tb_id
                and b.get("status") in ("已接受", "已锁定")
                and (b.get("content") or "").strip()]
    if accepted:
        return "\n\n".join(b["content"].strip() for b in accepted)
    return store.prose_batch_merged(chapter_id).strip()


def _chapter_text(store: ProjectStore, chapter: dict, include_outline: bool) -> str:
    parts = []
    if include_outline:
        summary = chapter_outline_text(chapter)
        if summary:
            parts.append("（本章概要：\n" + summary + "）")
    body = chapter_effective_text(store, chapter["id"])
    if body:
        parts.append(body)
    return "\n\n".join(parts)


def chapter_heading(chapter: dict) -> str:
    """章节标题行：`第N章 标题`（标题已自带章号时不再重复）。"""
    idx = chapter.get("order_index", 0)
    name = (chapter.get("title") or "").strip()
    if name.startswith("第") and "章" in name[:8]:
        return name          # 标题本身已含「第N章」，避免「第3章 第3章 入城」
    return f"第{idx}章 {name}".rstrip()


def export_warnings(store: ProjectStore, chapter_ids: list[int] | None = None) -> list[str]:
    """导出前的确定性提示（不阻断导出）：告诉用户哪些章可能不是「最终稿」。

    两类：
    · 草稿态 —— 整章正文块是「草稿」（被另一种写法取代，如改用了细纲块写法），
      导出按红线跳过它、改用已接受的那一份，提示一句避免用户以为丢了内容。
    · 未合并 —— 分批写完但没合并落库，正文只在分批工作区里（导出已用工作区兜底，
      提示一句让用户知道来源）。

    注意：AI 生成/改写后**还没点「接受」**的稿子只存在写作台内存里、不在库中，
    core 层看不到；界面上那句「未落库，点『接受』写入」才是它的唯一提示。
    """
    chapters = store.list_chapters()
    if chapter_ids:
        chapters = [c for c in chapters if c["id"] in chapter_ids]
    pending, batch_only = [], []
    for c in chapters:
        tb = store.get_chapter_prose_block(c["id"])
        label = chapter_heading(c)
        if tb and tb.get("status") == "草稿" and (tb.get("content") or "").strip():
            pending.append(label)
        elif not (tb and (tb.get("content") or "").strip()) \
                and store.prose_batch_merged(c["id"]).strip():
            batch_only.append(label)
    out = []
    if pending:
        out.append(f"这 {len(pending)} 章的整章正文还是草稿（没点「接受」），"
                   f"导出会用已接受的版本：\n"
                   + "、".join(pending[:8]) + ("…" if len(pending) > 8 else ""))
    if batch_only:
        out.append(f"这 {len(batch_only)} 章只有分批工作区、还没合并进正文，"
                   f"导出将直接使用工作区内容：\n"
                   + "、".join(batch_only[:8]) + ("…" if len(batch_only) > 8 else ""))
    return out


def export_prose(store: ProjectStore, fmt: str, path: str,
                 chapter_ids: list[int] | None = None,
                 include_synopsis: bool = False,
                 include_outline: bool = False) -> tuple[bool, str]:
    """导出正文。fmt: TXT/Markdown/DOCX；chapter_ids None=全部。返回 (ok, message)。

    产物只含「章节名 + 正文」（2026-11-13 起不再写书名/分卷标题/分隔横线）；
    正文取 `chapter_effective_text`（整章正文 > 已接受分块 > 未合并的分批工作区），
    保证导出与写作台看到的最新稿一致。父目录不存在会自动创建。
    """
    fmt = fmt.upper()
    if fmt not in ("TXT", "MARKDOWN", "DOCX"):
        return False, f"不支持的格式: {fmt}"
    chapters = store.list_chapters()
    if chapter_ids:
        chapters = [c for c in chapters if c["id"] in chapter_ids]
    if not chapters:
        return False, "没有可导出的章节"
    title = store.title
    parent = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as e:
        return False, f"无法创建目录：{e}"

    try:
        if fmt == "TXT":
            return _export_txt(store, chapters, title, path, include_synopsis, include_outline)
        if fmt == "MARKDOWN":
            return _export_md(store, chapters, title, path, include_synopsis, include_outline)
        return _export_docx(store, chapters, title, path, include_synopsis, include_outline)
    except ImportError:
        return False, "缺少 python-docx 库，无法导出 DOCX（TXT/Markdown 不受影响）"
    except OSError as e:
        return False, f"写入失败（文件可能正被 Word/WPS 打开）：{e}"


def _heading_prefix(store: ProjectStore, include_synopsis: bool) -> str:
    """勾选「包含总纲」时置顶的总纲段（不再用 ==== 分隔线）。"""
    if not include_synopsis:
        return ""
    s = store.current_synopsis()
    if not s:
        return ""
    return (f"【总纲】{s.get('title', '')}\n{s['content']}\n").strip()


def _export_txt(store, chapters, title, path, include_synopsis, include_outline) -> tuple[bool, str]:
    """TXT：章节名 + 正文，不加书名/分卷/分隔横线（2026-11-13 老板要求「简简单单」）。"""
    blocks = []
    head = _heading_prefix(store, include_synopsis)
    if head:
        blocks.append(head.strip())
    for c in chapters:
        blocks.append(chapter_heading(c))
        body = _chapter_text(store, c, include_outline)
        if body:
            blocks.append(body)
    text = "\n\n".join(blocks).strip() + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return True, f"已导出 {len(chapters)} 章（{len(text)} 字）→ {path}"


def _export_md(store, chapters, title, path, include_synopsis, include_outline) -> tuple[bool, str]:
    """Markdown：`## 章节名` + 正文，不写书名/分卷标题。"""
    blocks = []
    head = _heading_prefix(store, include_synopsis)
    if head:
        blocks.append(head.strip())
    for c in chapters:
        blocks.append("## " + chapter_heading(c))
        body = _chapter_text(store, c, include_outline)
        if body:
            blocks.append(body)
    text = "\n\n".join(blocks).strip() + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return True, f"已导出 {len(chapters)} 章（{len(text)} 字）→ {path}"


def _export_docx(store, chapters, title, path, include_synopsis, include_outline) -> tuple[bool, str]:
    try:
        from docx import Document
    except ImportError:
        # 安卓端（Flet/APK）装不上 python-docx：它依赖 lxml 的 C 扩展，PyPI 无
        # android-aarch64 官方 wheel。这里回退到零依赖的极简 OOXML 写入器，
        # 产出的 .docx 同样能被 Word / WPS 打开；桌面有 python-docx 时不会走到这。
        return _export_docx_minimal(store, chapters, title, path,
                                    include_synopsis, include_outline)
    doc = Document()
    head = _heading_prefix(store, include_synopsis)
    if head:
        doc.add_paragraph(head.strip())
    for c in chapters:
        doc.add_heading(chapter_heading(c), 1)   # 章即文档一级标题（导航窗格可跳转）
        body = _chapter_text(store, c, include_outline)
        for para in body.split("\n"):
            if para.strip():
                doc.add_paragraph(para.strip())
    doc.save(path)
    return True, f"已导出 {len(chapters)} 章 → {path}"


# ---------------- 零依赖 DOCX（安卓回退） ----------------
#
# .docx 本质是个 zip：只要 [Content_Types].xml / _rels/.rels / word/document.xml
# （+ styles.xml 与其 rels）齐备，就是合法 OOXML，Word / WPS / Google Docs 均可打开。
# 这里手写最小包，避免在 APK 里引入 python-docx→lxml 的 C 扩展依赖。

_DOCX_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    '</Types>')

_DOCX_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    '</Relationships>')

_DOCX_DOC_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    '</Relationships>')

_DOCX_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:docDefaults><w:rPrDefault><w:rPr>'
    '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="\u5b8b\u4f53"/>'
    '<w:sz w:val="24"/></w:rPr></w:rPrDefault></w:docDefaults>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
    '<w:name w:val="Normal"/><w:qFormat/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/>'
    '<w:basedOn w:val="Normal"/><w:qFormat/>'
    '<w:pPr><w:jc w:val="center"/><w:spacing w:after="240"/></w:pPr>'
    '<w:rPr><w:b/><w:sz w:val="44"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>'
    '<w:basedOn w:val="Normal"/><w:qFormat/>'
    '<w:pPr><w:spacing w:before="240" w:after="120"/></w:pPr>'
    '<w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/>'
    '<w:basedOn w:val="Normal"/><w:qFormat/>'
    '<w:pPr><w:spacing w:before="200" w:after="80"/></w:pPr>'
    '<w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>'
    '</w:styles>')

_DOCX_DOCUMENT = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:body>%s'
    '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
    '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>'
    '</w:body></w:document>')


def _docx_para(text: str, style: str = "Normal") -> str:
    """单个段落。空段用 <w:p/> 保留空行（段间距靠它表现）。"""
    text = str(text if text is not None else "")
    if not text.strip():
        return "<w:p/>"
    return ('<w:p><w:pPr><w:pStyle w:val="%s"/></w:pPr>'
            '<w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>'
            % (style, escape(text)))


def _docx_paragraphs(text: str) -> str:
    """多行文本 → 多个段落（换行符在 w:t 里非法，必须拆成段）。"""
    return "".join(_docx_para(line) for line in str(text or "").split("\n"))


def _export_docx_minimal(store, chapters, title, path,
                         include_synopsis, include_outline) -> tuple[bool, str]:
    """零依赖 DOCX 导出（结构与 _export_docx 一致：章节名 + 正文，无书名/分卷）。"""
    parts = []
    head = _heading_prefix(store, include_synopsis)
    if head:
        parts.append(_docx_paragraphs(head.strip()))
    for c in chapters:
        parts.append(_docx_para(chapter_heading(c), "Heading1"))
        parts.append(_docx_paragraphs(_chapter_text(store, c, include_outline)))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _DOCX_CONTENT_TYPES)
        zf.writestr("_rels/.rels", _DOCX_ROOT_RELS)
        zf.writestr("word/document.xml", _DOCX_DOCUMENT % "".join(parts))
        zf.writestr("word/styles.xml", _DOCX_STYLES)
        zf.writestr("word/_rels/document.xml.rels", _DOCX_DOC_RELS)
    return True, f"已导出 {len(chapters)} 章 → {path}"


# ---------------- 项目打包 ----------------

def export_project_zip(store: ProjectStore, zip_path: str,
                       include_backups: bool = False) -> tuple[bool, str]:
    """项目导出：project.db + 可选 backups → zip。

    破甲/生成偏好等工具配置已全局共享（2026-08-08），不在项目文件夹内，
    zip 只打包内容数据（project.db），全局配置随全局库另行备份/迁移。

    project.db 走一致性快照（`snapshot_to` + `exclude_tables=()`），不再
    checkpoint+copy：后者在 checkpoint 与 copy 之间的写入会留在 WAL 里被丢掉，
    出现「导出的 zip 内容比界面旧」。全量备份一张表都不剥（request_logs 也要）。
    """
    try:
        with tempfile.TemporaryDirectory(prefix="jgprojzip_") as tmp:
            snap = os.path.join(tmp, "project.db")
            store.snapshot_to(snap, exclude_tables=())
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(snap, arcname="project.db")
                if include_backups:
                    backup_dir = os.path.join(store.folder, "backups")
                    if os.path.isdir(backup_dir):
                        for fn in os.listdir(backup_dir):
                            fp = os.path.join(backup_dir, fn)
                            if os.path.isfile(fp):
                                zf.write(fp, arcname=f"backups/{fn}")
                meta = {"type": "jiuguan_project", "version": 1,
                        "title": store.title, "exported_at": datetime.now().isoformat(timespec="seconds")}
                zf.writestr("manifest.json", json.dumps(meta, ensure_ascii=False, indent=2))
        return True, f"已导出 → {zip_path}"
    except (OSError, sqlite3.Error) as e:
        return False, f"导出失败：{e}"


def import_project_zip(zip_path: str, dest_folder: str) -> ProjectStore | None:
    """导入项目 zip 到目标文件夹，返回打开的 ProjectStore。

    旧版 zip 里可能带 jailbreaks/（破甲已全局共享，导入时忽略，避免污染项目文件夹）。
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        if "project.db" not in names:
            raise ValueError("不是有效的项目文件（缺少 project.db）")
        os.makedirs(dest_folder, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            zf.extract("project.db", tmp)
            db_src = os.path.join(tmp, "project.db")
            db_dst = os.path.join(dest_folder, "project.db")
            shutil.copy2(db_src, db_dst)
            if "manifest.json" in names:
                zf.extract("manifest.json", tmp)
    return ProjectStore(dest_folder)


# ---------------- 全量备份（双端同步：Windows ↔ 安卓） ----------------
#
# 一个 zip = 全局库 app.db（模型档案/API Key/路由/偏好/预设/任务模板/全局设置）
#          + config.json（最近项目）+ jailbreaks/*.md（破甲库）+ 全部最近项目 project.db。
# 导入 = 全量替换本机对应数据，实现"电脑 ↔ 手机 导入即一模一样"。
#
# API Key：本机存储格式跨平台不可解（Windows DPAPI 绑定机器/用户），所以包内统一转
# 可移植格式，导入端按本机平台重新加密：
#   p3:<明文>  —— 导出端成功解密的明文，导入端 encrypt_key 重加密
#   k1:<原样>  —— 导出端解不开的密文（如跨机 DPAPI），原样保留，导入后可能需重填
#
# 2026-11-17「全量备份一致性与原子恢复」改造（四条不许回退）：
#   1. 导出不再 checkpoint+copy：那两步之间的写入会落在 WAL 里被 copy 丢掉，
#      包看着成功、内容却落后。现在一律 SQLite 一致性快照（VACUUM INTO）。
#      项目库复用 `ProjectStore.snapshot_to`，**必须传 exclude_tables=()**——
#      云同步的默认值会剥 request_logs，全量备份剥了就不叫全量。
#   2. 每个快照落地后立刻 `PRAGMA integrity_check` + 记 SHA-256 进 manifest，
#      导入端照单复核；坏包在「替换」之前就被拦下。
#   3. 导入分两阶段：先在临时目录把 manifest / 路径安全 / 全部数据库 / 破甲文件
#      校验完（本机数据一字未动），再进替换阶段；替换阶段每步都留回滚点。
#   4. 导入前的安全备份必须成功，否则直接中止——没有后悔药就不动刀。
#   旧版 v1 包（无 sha256 / 无完整性记录）仍可导入：校验照跑，只跳过哈希比对。
#
# 2026-11-17「默认密码加密」改造（v3 加密容器，四条不许回退）：
#   1. 默认产物不是 zip，而是 JGB1 加密容器（整个 zip 当明文加密）。密码学一行
#      没新写：KDF/HMAC/verifier 全部复用 sync_crypto.SyncCrypto。
#   2. 不传密码就不导出（`allow_plain=True` 才给明文包）——包里有 API Key 明文。
#   3. 导入自动识别加密新版与旧版明文 zip；缺密码/密码错/密文被篡改都在第一
#      阶段返回，本机数据一字不动，也不写安全备份。
#   4. 密码与密钥一律不进日志、不进错误消息（错误只说「密码不对」）。

KEY_PORTABLE = "p3:"
KEY_KEEP = "k1:"
_EMBED_KEY_META = "global_setting_embed_api_key"
BACKUP_VERSION = 2

# ---------------- 加密容器（JGB1） ----------------
#
# 为什么不是「在 zip 里加密某个成员」：zip 的成员名/清单是明文，能看出有几个
# 项目、叫什么；而库里的 API Key 在包内是可移植明文（p3:），只加密一部分等于
# 没加密。这里把**整个 zip 当明文**加密，落盘文件里连目录结构都看不到：
#
#     b"JGB1" + 头长度(4 字节大端) + 头 JSON(utf-8) + SyncCrypto.encrypt(zip 字节)
#
# 密码学全部复用 sync_crypto（PBKDF2-SHA256 → ChaCha20 + HMAC-SHA256，
# Encrypt-then-MAC），桌面与安卓共用同一套纯标准库实现（APK 不许 C 扩展）。
# 头里只有公开参数：盐、迭代次数、verifier、明文长度与 SHA-256、导出时间——
# 密码和密钥材料一个都不在里面。
ENCRYPTED_MAGIC = b"JGB1"
BACKUP_CONTAINER_VERSION = 1
BACKUP_KDF_ITERS = DEFAULT_ITERS      # 200_000，与云同步同一档
MIN_BACKUP_PASSWORD = 6               # 太短的口令配 PBKDF2 也扛不住穷举
_HDR_MAX = 64 * 1024                  # 正常头不到 1KB；超大说明文件被构造过


def _safe_name(title: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", (title or "").strip())[:40] or "未命名"


def _key_to_portable(v: str) -> str:
    """本机存储的密钥值 → 备份包内可移植值。"""
    if not v:
        return ""
    m = decrypt_key(v)
    if m:
        return KEY_PORTABLE + m
    return KEY_KEEP + v  # 解不开（跨机 d1:）或本就明文：原样保留


def _key_from_portable(v: str) -> str:
    """备份包内密钥值 → 本机存储值（按本机平台重新加密）。"""
    if not v:
        return ""
    if v.startswith(KEY_PORTABLE):
        return encrypt_key(v[len(KEY_PORTABLE):])
    if v.startswith(KEY_KEEP):
        return v[len(KEY_KEEP):]
    return encrypt_key(v)  # 兼容：无前缀明文


def _rekey_db_copy(src_path: str, dst_path: str, to_portable: bool):
    """复制 sqlite 库并转换其中的 API Key（模型档案表 + meta 嵌入密钥）。"""
    shutil.copy2(src_path, dst_path)
    conv = _key_to_portable if to_portable else _key_from_portable
    con = sqlite3.connect(dst_path)
    try:
        for pid, v in con.execute("SELECT id, api_key FROM model_profiles").fetchall():
            nv = conv(v or "")
            if nv != (v or ""):
                con.execute("UPDATE model_profiles SET api_key=? WHERE id=?", (nv, pid))
        row = con.execute("SELECT value FROM meta WHERE key=?", (_EMBED_KEY_META,)).fetchone()
        if row:
            nv = conv(row[0] or "")
            if nv != (row[0] or ""):
                con.execute("UPDATE meta SET value=? WHERE key=?", (nv, _EMBED_KEY_META))
        con.commit()
    finally:
        con.close()


def _sha256_file(path: str) -> str:
    """分块算 SHA-256（单本小说库几十 MB，不整读进内存）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _vacuum_snapshot(db_path: str, dest: str) -> str:
    """对没打开连接的项目库做 VACUUM INTO 一致性快照。

    不用 checkpoint+copy：那两步之间的写入会落在 WAL 里被 copy 丢掉。
    VACUUM INTO 在读事务里生成新库，WAL 里已提交的写入自动带上。
    """
    if os.path.isfile(dest):
        os.remove(dest)
    folder = os.path.dirname(os.path.abspath(dest))
    if folder:
        os.makedirs(folder, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA busy_timeout=15000")
        con.execute("VACUUM INTO ?", (dest,))
    finally:
        con.close()
    return dest


def _snapshot_global(app_state, dest: str) -> None:
    """全局库一致性快照（与 `ProjectStore.snapshot_to` 等价的 VACUUM INTO）。

    app.db 是 GlobalStore 的连接在管，没有 snapshot_to 可用，所以这里对同一条
    连接执行同一条语句——**必须持 `_lock`**，与其它写法一致；失败退回新连接直连。
    """
    if os.path.isfile(dest):
        os.remove(dest)
    folder = os.path.dirname(os.path.abspath(dest))
    if folder:
        os.makedirs(folder, exist_ok=True)
    g = app_state.gstore
    with g._lock:
        try:
            g.conn.execute("VACUUM INTO ?", (dest,))
            return
        except sqlite3.Error as e:
            # 不静默：连接态快照失败意味着要退回文件级快照，留痕可查
            print(f"[export] 全局库 VACUUM INTO 失败，改用文件级快照："
                  f"{type(e).__name__}: {e}")
    _vacuum_snapshot(g.db_path, dest)


def _verify_db(path: str, expect_sha: str = "", required_tables=()) -> dict:
    """打开库跑 integrity_check、核对 SHA-256、检查必要表，返回 {sha256, size}。

    任何一项不过都抛 ValueError（中文原因），由导出/导入转成 (False, msg)。
    """
    digest = _sha256_file(path)
    if expect_sha and digest != expect_sha:
        raise ValueError(f"文件校验和不符（包内可能已损坏）：{os.path.basename(path)}")
    try:
        con = sqlite3.connect(path)
        try:
            row = con.execute("PRAGMA integrity_check").fetchone()
            state = (row[0] if row else "") or ""
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
    except sqlite3.DatabaseError as e:
        raise ValueError(f"数据库损坏或不是有效的 SQLite 文件：{e}")
    if state.strip().lower() != "ok":
        raise ValueError(f"数据库完整性校验失败：{state[:120]}")
    missing = [t for t in required_tables if t not in tables]
    if missing:
        raise ValueError(f"数据库结构不对（缺少 {', '.join(missing)}）")
    return {"sha256": digest, "size": os.path.getsize(path)}


def _verify_file_hash(path: str, expect_sha: str) -> None:
    if expect_sha and _sha256_file(path) != expect_sha:
        raise ValueError(f"文件校验和不符（包内可能已损坏）：{os.path.basename(path)}")


def _install_file(src: str, dst: str) -> None:
    """把暂存文件装到目标位置：先写同目录临时名再 os.replace（同盘原子）。

    单独抽成一个函数是为了故障注入测试能只在这一处失败，从而验证回滚。
    """
    tmp = dst + ".jgs_import_tmp"
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


class _ImportUndo:
    """替换阶段的回滚账本：撤销新建的路径、复原被移走的原件。

    回滚区落在数据目录内（同一个盘），stash 就是 rename，不动内容、不占空间。
    """

    def __init__(self, root: str):
        self.root = root
        self.moved = []     # [(原位置, 回滚区位置)]
        self.created = []   # 本次新建的文件/目录
        self._seq = 0

    def stash(self, path: str):
        """把已存在的文件/目录移进回滚区并登记（不存在则跳过）。"""
        if not os.path.exists(path):
            return None
        self._seq += 1
        saved = os.path.join(self.root, f"{self._seq:03d}_{os.path.basename(path)}")
        shutil.move(path, saved)
        self.moved.append((path, saved))
        return saved

    def track(self, path: str) -> None:
        self.created.append(path)

    def undo(self) -> list:
        """撤销。返回没能复原的项（空 list = 完全复原）。"""
        failed = []
        for p in reversed(self.created):
            try:
                if os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p, ignore_errors=True)
                elif os.path.exists(p):
                    os.remove(p)
            except OSError:
                failed.append(p)
        for orig, saved in reversed(self.moved):
            try:
                if os.path.exists(orig):
                    # 半成品先清掉（正常流程走到这里不会存在，防御性处理）
                    if os.path.isdir(orig) and not os.path.islink(orig):
                        shutil.rmtree(orig, ignore_errors=True)
                    else:
                        os.remove(orig)
                os.makedirs(os.path.dirname(orig) or ".", exist_ok=True)
                shutil.move(saved, orig)
            except OSError:
                failed.append(orig)
        return failed


def _remove_sqlite_sidecars(db_path: str):
    """删除 -wal/-shm/-journal 残留：覆盖 db 文件后旧 sidecar 会污染新库。"""
    for suffix in ("-wal", "-shm", "-journal"):
        side = db_path + suffix
        if os.path.isfile(side):
            try:
                os.remove(side)
            except OSError:
                pass


def _live_store(app_state, folder: str):
    """这本书若正被打开/停靠，返回它的 ProjectStore（用活连接做快照最稳）。

    只复用**已经存在**的连接，绝不为了备份临时打开一个 ProjectStore：
    打开会跑建索引/补种子/回填迁移，等于在「导出」里改用户的书。
    """
    key = os.path.normcase(os.path.abspath(folder))
    st = getattr(app_state, "store", None)
    cur = getattr(app_state, "project_folder", None)
    if st is not None and not getattr(st, "is_closed", False) and cur \
            and os.path.normcase(os.path.abspath(cur)) == key:
        return st
    try:
        parked = app_state.parked_store(folder)
    except Exception:
        return None
    if parked is not None and getattr(parked, "is_closed", False):
        return None
    return parked


def _drop_quietly(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _copy_atomic(src: str, dst: str) -> None:
    """同目录临时名 + os.replace，避免「写一半」留下半个备份文件。"""
    parent = os.path.dirname(os.path.abspath(dst))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = dst + ".jgtmp"
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    except OSError:
        _drop_quietly(tmp)      # 失败别在老板的文件夹里留半截 .jgtmp
        raise


def _write_atomic(path: str, data: bytes) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".jgtmp"
    try:
        with open(tmp, "wb") as fp:
            fp.write(data)
        os.replace(tmp, path)
    except OSError:
        _drop_quietly(tmp)
        raise


def _write_container_file(path: str, header: dict, blob: bytes) -> None:
    """落盘容器：三段写完再 os.replace，避免在内存里再拼一份（大备份省一倍内存）。"""
    raw = json.dumps(header, ensure_ascii=False, sort_keys=True).encode("utf-8")
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".jgtmp"
    try:
        with open(tmp, "wb") as fp:
            fp.write(ENCRYPTED_MAGIC)
            fp.write(struct.pack(">I", len(raw)))
            fp.write(raw)
            fp.write(blob)
        os.replace(tmp, path)
    except OSError:
        _drop_quietly(tmp)
        raise


def _read_container(path: str) -> tuple:
    """读加密容器 → (头 dict, 密文块)。只读头，不解密、不碰本机数据。"""
    with open(path, "rb") as fp:
        magic = fp.read(4)
        if magic != ENCRYPTED_MAGIC:
            raise ValueError("不是加密的全量备份（文件头不匹配）")
        raw_len = fp.read(4)
        if len(raw_len) != 4:
            raise ValueError("备份文件不完整（缺少头部长度）")
        size = struct.unpack(">I", raw_len)[0]
        if size <= 0 or size > _HDR_MAX:
            raise ValueError("备份文件头部长度异常，可能已损坏")
        raw = fp.read(size)
        if len(raw) != size:
            raise ValueError("备份文件不完整（头部被截断）")
        blob = fp.read()
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"备份文件头部读不出来：{e}")
    if not isinstance(header, dict):
        raise ValueError("备份文件头部格式不对")
    if header.get("type") != "jiuguan_full_backup" or header.get("format") != "encrypted":
        raise ValueError("备份文件类型不匹配")
    try:
        ver = int(header.get("container_version"))
    except (TypeError, ValueError):
        raise ValueError("备份文件缺少容器版本号")
    if ver > BACKUP_CONTAINER_VERSION:
        raise ValueError(f"这个备份是更新版本的程序导出的（容器 v{ver}），请先更新本程序")
    kdf = header.get("kdf") or {}
    if kdf.get("algo") != "pbkdf2-sha256":
        raise ValueError("备份文件的加密算法不受支持")
    if header.get("cipher") != "chacha20-hmac-sha256":
        raise ValueError("备份文件的加密方式不受支持")
    if not blob:
        raise ValueError("备份文件不完整（没有密文）")
    return header, blob


def is_encrypted_backup(path: str) -> bool:
    """文件是不是 JGB1 加密容器（只看 4 字节，读不出来算 False）。"""
    try:
        with open(path, "rb") as fp:
            return fp.read(4) == ENCRYPTED_MAGIC
    except OSError:
        return False


def describe_backup_file(path: str) -> dict:
    """判断备份文件是哪一种（不解密、不写盘），供界面决定要不要问密码。

    返回 {"kind": "encrypted"|"plain"|"unknown", ...}
    · encrypted → need_password=True，附容器版本/导出时间/内容版本
    · plain     → 旧版明文 zip（v1/v2），或用户明确选择的明文包
    · unknown   → 不是本程序的备份；reason 里是中文原因
    """
    try:
        with open(path, "rb") as fp:
            head = fp.read(4)
    except OSError as e:
        return {"kind": "unknown", "reason": f"文件读不出来：{e}"}
    if head == ENCRYPTED_MAGIC:
        try:
            header, _blob = _read_container(path)
        except (ValueError, OSError) as e:
            return {"kind": "unknown", "reason": str(e)}
        return {"kind": "encrypted", "need_password": True,
                "container_version": header.get("container_version"),
                "content_version": header.get("content_version"),
                "exported_at": header.get("exported_at") or "",
                "kdf": header.get("kdf") or {},
                "plain_size": (header.get("plain") or {}).get("size")}
    if head[:2] == b"PK":
        try:
            with zipfile.ZipFile(path) as zf:
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except (zipfile.BadZipFile, KeyError, UnicodeDecodeError,
                json.JSONDecodeError, OSError):
            return {"kind": "unknown", "reason": "zip 里缺少全量备份清单（manifest.json）"}
        if not isinstance(manifest, dict) or manifest.get("type") != "jiuguan_full_backup":
            return {"kind": "unknown", "reason": "zip 里缺少全量备份清单（类型不匹配）"}
        try:
            version = int(manifest.get("version") or 1)
        except (TypeError, ValueError):
            version = 1
        return {"kind": "plain", "need_password": False, "version": version,
                "exported_at": manifest.get("exported_at") or "",
                "manifest_encrypted_flag": bool(manifest.get("encrypted", False))}
    return {"kind": "unknown", "reason": "既不是加密备份，也不是 zip 文件"}


def encrypt_backup_zip(zip_path: str, out_path: str, password: str) -> dict:
    """把明文 zip 加密成 JGB1 容器写到 out_path。返回 {size, sha256}（明文的）。"""
    if not password or not password.strip():
        raise ValueError("备份密码不能为空")
    if len(password) < MIN_BACKUP_PASSWORD:
        raise ValueError(f"备份密码至少 {MIN_BACKUP_PASSWORD} 位")
    with open(zip_path, "rb") as fp:
        plain = fp.read()
    crypto = SyncCrypto.create(password, BACKUP_KDF_ITERS)
    header = {
        "type": "jiuguan_full_backup",
        "format": "encrypted",
        "container_version": BACKUP_CONTAINER_VERSION,
        "content_version": BACKUP_VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "cipher": "chacha20-hmac-sha256",
        "kdf": crypto.kdf_params,          # 只有 algo/iters/salt，无密钥材料
        "verifier": crypto.verifier(),     # 密码校验值（输错当场报错，不解出垃圾）
        "plain": {"size": len(plain), "sha256": hashlib.sha256(plain).hexdigest()},
    }
    blob = crypto.encrypt(plain)
    _write_container_file(out_path, header, blob)
    return header["plain"]


def backup_plain_zip(src: str, password, dest_zip: str) -> str:
    """把备份文件（加密容器 / 旧版明文 zip）落成一份可读的明文 zip。

    只做「解开」，不做业务校验——解出来的 zip 仍要过 `_stage_backup` 第一阶段。
    密码错、密文被改、明文与清单不符都抛 CryptoError，调用方原样转成中文提示；
    本函数不写除 dest_zip 之外的任何文件，所以失败时本机数据必然未动。
    """
    if not is_encrypted_backup(src):
        if os.path.abspath(src) != os.path.abspath(dest_zip):
            _copy_atomic(src, dest_zip)
        return dest_zip
    header, blob = _read_container(src)
    kdf = header.get("kdf") or {}
    try:
        salt = bytes.fromhex(str(kdf.get("salt") or ""))
        iters = int(kdf.get("iters") or 0)
    except (TypeError, ValueError):
        raise CryptoError("备份文件的加密参数损坏，无法解密")
    if not password:
        raise CryptoError("这是加密的全量备份，请输入备份密码")
    crypto = SyncCrypto.derive(password, salt, iters)
    if not crypto.check_verifier(str(header.get("verifier") or "")):
        raise CryptoError("备份密码不对（本机数据没有做任何改动）")
    try:
        plain = crypto.decrypt(blob)
    except CryptoError:
        raise CryptoError("备份文件校验失败：密文被改动或文件损坏"
                          "（本机数据没有做任何改动）")
    expect = str((header.get("plain") or {}).get("sha256") or "")
    if expect and hashlib.sha256(plain).hexdigest() != expect:
        raise CryptoError("备份内容校验失败：解密出来的数据与清单不符"
                          "（本机数据没有做任何改动）")
    _write_atomic(dest_zip, plain)
    return dest_zip


def backup_summary(app_state) -> dict:
    """全量备份**将要包含什么**（纯读，不落盘、不加锁、不动任何文件）。

    与 `export_full_backup` 同一筛选口径：只算 `list_recent()` 里文件夹仍存在
    且去重后的项目；破甲数只算安全文件名（`_safe_key`）。UI 用它给用户一张
    「包内清单」，别在两端各写一份筛选逻辑——清单与实物不一致比没有清单更糟。

    返回 {"projects": int, "titles": [str], "jailbreaks": int, "has_api_key": bool}
    """
    seen, titles = set(), []
    for r in app_state.list_recent():
        if not r.get("exists"):
            continue
        norm = os.path.normcase(r.get("path") or "")
        if norm in seen:
            continue
        seen.add(norm)
        titles.append(str(r.get("title") or os.path.basename(
            str(r.get("path") or "")) or "未命名"))
    jb_count = 0
    jb_dir = os.path.join(app_state.data_dir, "jailbreaks")
    if os.path.isdir(jb_dir):
        for fn in sorted(os.listdir(jb_dir)):
            if _safe_key(fn) and os.path.isfile(os.path.join(jb_dir, fn)):
                jb_count += 1
    # 口径与 _rekey_db_copy 对齐：导出不只看模型档案的 api_key，还会把 meta 里的
    # 全局嵌入 Key 一起转格式带走。只配了 embed Key 的设备必须报「包里有 Key」，
    # 否则清单会说「未包含」而实物里有——清单与实物不一致比没有清单更糟。
    # 纯读、短路：档案里已有 Key 就不再读全局；不解密、不连库写、不加新异常路径。
    has_key = False
    try:
        for p in app_state.gstore.list_profiles():
            if str(p.get("api_key") or "").strip():
                has_key = True
                break
        if not has_key:
            has_key = bool(str(app_state.gstore.get_global_setting(
                "embed_api_key", "") or "").strip())
    except Exception:
        # 档案/全局设置读不出来不影响备份本身；清单就报「未知」，界面按「未包含」处理
        pass
    return {"projects": len(titles), "titles": titles,
            "jailbreaks": jb_count, "has_api_key": has_key}


def export_full_backup(zip_path: str, app_state, password=None,
                       allow_plain: bool = False) -> tuple[bool, str]:
    """全量备份到 zip_path（**默认加密**，落盘为 JGB1 容器）。

    密码是默认要求：包里的 API Key 是可移植明文（p3:），明文 zip 传到网盘
    等于把密钥一起送人。`allow_plain=True` 才允许出明文包，且返回消息带警示，
    界面必须先把风险讲清楚再问用户要不要。参数不合规时一个字节都不写。

    库一律走一致性快照（VACUUM INTO），快照落地即校验完整性并记 SHA-256；
    任何一本库校验不过就整体失败——宁可没备份，也不要一个「成功」的坏包。
    """
    if not password or not str(password).strip():
        if not allow_plain:
            return False, ("请先设置备份密码：全量备份里有 API Key 明文，"
                           "不加密的 zip 用记事本都能搜到密钥。"
                           "确实要明文包，请显式选择「不使用密码」。")
        password = None
    elif len(password) < MIN_BACKUP_PASSWORD:
        return False, f"备份密码至少 {MIN_BACKUP_PASSWORD} 位（现在 {len(password)} 位）"
    data_dir = app_state.data_dir
    app_db = os.path.join(data_dir, "app.db")
    if not os.path.isfile(app_db):
        return False, "找不到全局库 app.db"
    # 最近项目去重、只取文件夹仍存在的
    seen, projects = set(), []
    for r in app_state.list_recent():
        if not r.get("exists"):
            continue
        norm = os.path.normcase(r["path"])
        if norm not in seen:
            seen.add(norm)
            projects.append(r)
    try:
        with tempfile.TemporaryDirectory(prefix="jgbackup_") as tmp:
            snap_root = os.path.join(tmp, "snap")
            os.makedirs(snap_root)
            # ---- 全局库：一致性快照 + 密钥转可移植格式 ----
            raw_app = os.path.join(snap_root, "app_raw.db")
            _snapshot_global(app_state, raw_app)
            snap_app = os.path.join(snap_root, "app.db")
            _rekey_db_copy(raw_app, snap_app, to_portable=True)
            app_info = _verify_db(snap_app, required_tables=("meta", "model_profiles"))
            # ---- 每个项目各自一条快照 ----
            entries = []
            for i, r in enumerate(projects, 1):
                key = f"{i:02d}_{_safe_name(r.get('title') or '')}"
                dest = os.path.join(snap_root, "projects", key, "project.db")
                folder = r["path"]
                db_file = os.path.join(folder, "project.db")
                if not os.path.isfile(db_file):
                    return False, (f"备份失败：项目《{r.get('title') or folder}》"
                                   "的数据库不见了（文件夹里没有 project.db）")
                store = _live_store(app_state, folder)
                try:
                    if store is not None:
                        # ⚠ exclude_tables=() 必须显式传：不传就是云同步的
                        # SYNC_SKIP_TABLES（剥 request_logs），全量备份不能剥
                        store.snapshot_to(dest, exclude_tables=())
                    else:
                        _vacuum_snapshot(db_file, dest)
                    info = _verify_db(dest, required_tables=("meta", "chapters"))
                except (OSError, sqlite3.Error, ValueError) as e:
                    return False, (f"备份失败：项目《{r.get('title') or folder}》"
                                   f"的快照校验没通过（{e}）")
                entries.append({"key": key, "title": r.get("title", ""),
                                "original_path": folder, **info})
            # ---- 破甲库：逐个记哈希，导入端照单复核 ----
            jb_src = os.path.join(data_dir, "jailbreaks")
            snap_jb = os.path.join(snap_root, "jailbreaks")
            jb_entries = []
            if os.path.isdir(jb_src):
                os.makedirs(snap_jb, exist_ok=True)
                for fn in sorted(os.listdir(jb_src)):
                    fp = os.path.join(jb_src, fn)
                    if not os.path.isfile(fp) or not _safe_key(fn):
                        continue
                    dest = os.path.join(snap_jb, fn)
                    shutil.copy2(fp, dest)
                    jb_entries.append({"name": fn, "sha256": _sha256_file(dest),
                                       "size": os.path.getsize(dest)})
            plain_zip = os.path.join(tmp, "backup.zip")
            with zipfile.ZipFile(plain_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(snap_app, arcname="app.db")
                config_src = os.path.join(data_dir, "config.json")
                if os.path.isfile(config_src):
                    zf.write(config_src, arcname="config.json")
                for jb in jb_entries:
                    zf.write(os.path.join(snap_jb, jb["name"]),
                             arcname=f"jailbreaks/{jb['name']}")
                for e in entries:
                    zf.write(os.path.join(snap_root, "projects", e["key"], "project.db"),
                             arcname=f"projects/{e['key']}/project.db")
                zf.writestr("manifest.json", json.dumps({
                    "type": "jiuguan_full_backup", "version": BACKUP_VERSION,
                    "exported_at": datetime.now().isoformat(timespec="seconds"),
                    "encrypted": bool(password),
                    "app_db": app_info,
                    "projects": entries,
                    "jailbreaks": jb_entries,
                }, ensure_ascii=False, indent=2))
            # ---- 落盘：默认加密；allow_plain 才给明文包（消息里带警示） ----
            if password:
                encrypt_backup_zip(plain_zip, zip_path, password)
                tail = "已加密，密码不保存、无法找回，请自己记牢"
            else:
                _copy_atomic(plain_zip, zip_path)
                tail = "⚠️ 未加密：包内 API Key 是明文，别放到公共网盘"
        return True, (f"已备份 {len(entries)} 个项目 + 全局配置 → {zip_path}"
                      f"（{tail}）")
    except (OSError, sqlite3.Error, zipfile.BadZipFile, ValueError) as e:
        return False, f"备份失败：{e}"


# ---------- 导入：第一阶段（临时目录里校验，本机数据一字未动） ----------

# 成员/文件夹名里的危险字符：路径分隔符、盘符冒号、通配符、控制字符
_UNSAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safe_key(name) -> bool:
    """成员/文件夹名是否安全：单段、无分隔符/控制字符、非 . 或 ..。"""
    if not isinstance(name, str) or not name or len(name) > 120:
        return False
    if name in (".", "..") or name.startswith("."):
        return False
    return not _UNSAFE_NAME_RE.search(name)


def _safe_member(name) -> str | None:
    """zip 成员名 → 规范化的安全相对路径；不安全返回 None。

    恶意包常见三种：绝对路径 `/x`、盘符 `C:\\x`、上跳 `../x`。三种一律拒绝，
    而且**整包拒绝**——包里出现一条越界路径就说明它不是本程序导出的。
    """
    if not isinstance(name, str) or not name or "\x00" in name:
        return None
    raw = name.replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        return None
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    if any(_UNSAFE_NAME_RE.search(p) for p in parts):
        return None
    return "/".join(parts)


def _extract_member(zf: zipfile.ZipFile, member: str, dst: str) -> None:
    """按字节流写到自己算出的路径（不用 zf.extract，落点完全由我们决定）。"""
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    with zf.open(member, "r") as src, open(dst, "wb") as out:
        shutil.copyfileobj(src, out, 1 << 20)


def _stage_backup(zip_path: str, stage_root: str) -> dict:
    """第一阶段：解到临时目录并逐项校验。返回导入计划；任何一项不合格抛 ValueError。"""
    with zipfile.ZipFile(zip_path, "r") as zf:
        # ---- 路径安全（整包先过一遍，防止 zip-slip） ----
        raw_to_safe = {}
        for info in zf.infolist():
            safe = _safe_member(info.filename)
            if safe is None:
                raise ValueError(f"备份包里有不安全的路径，已拒绝导入：{info.filename[:80]}")
            if safe in raw_to_safe:
                raise ValueError(f"备份包里有重复条目：{safe[:80]}")
            raw_to_safe[safe] = info.filename
        if "manifest.json" not in raw_to_safe or "app.db" not in raw_to_safe:
            raise ValueError("不是有效的全量备份文件（缺少 manifest.json / app.db）")
        # ---- manifest ----
        try:
            manifest = json.loads(zf.read(raw_to_safe["manifest.json"]).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ValueError(f"manifest 读不出来：{e}")
        if not isinstance(manifest, dict) or manifest.get("type") != "jiuguan_full_backup":
            raise ValueError("备份包类型不匹配")
        try:
            version = int(manifest.get("version") or 1)
        except (TypeError, ValueError):
            version = 1

        # ---- 全局库：完整性 + 哈希 + 结构，再把密钥换回本机格式 ----
        staged_app = os.path.join(stage_root, "app.db")
        _extract_member(zf, raw_to_safe["app.db"], staged_app)
        expect = ""
        if isinstance(manifest.get("app_db"), dict):
            expect = manifest["app_db"].get("sha256") or ""
        _verify_db(staged_app, expect, ("meta", "model_profiles"))
        local_app = os.path.join(stage_root, "app_local.db")
        _rekey_db_copy(staged_app, local_app, to_portable=False)
        _verify_db(local_app, "", ("meta", "model_profiles"))

        # ---- 破甲库：文件名安全 + UTF-8 可读 + 哈希 ----
        jailbreaks = []
        stage_jb = os.path.join(stage_root, "jailbreaks")
        os.makedirs(stage_jb, exist_ok=True)
        jb_meta = {j.get("name"): j for j in (manifest.get("jailbreaks") or [])
                   if isinstance(j, dict)}
        for safe in sorted(raw_to_safe):
            if not safe.startswith("jailbreaks/"):
                continue
            name = safe[len("jailbreaks/"):]
            if "/" in name or not _safe_key(name):
                raise ValueError(f"备份包里有不安全的破甲文件名：{name[:80]}")
            dst = os.path.join(stage_jb, name)
            _extract_member(zf, raw_to_safe[safe], dst)
            try:
                with open(dst, "r", encoding="utf-8") as f:
                    f.read()
            except (UnicodeDecodeError, OSError) as e:
                raise ValueError(f"破甲文件《{name}》读不出来（不是 UTF-8 文本）：{e}")
            _verify_file_hash(dst, (jb_meta.get(name) or {}).get("sha256") or "")
            jailbreaks.append((name, dst))

        # ---- 项目库：逐个完整性 + 哈希 + 结构 ----
        projects = []
        for i, e in enumerate(manifest.get("projects") or [], 1):
            if not isinstance(e, dict):
                continue
            key = e.get("key")
            if not _safe_key(key):
                raise ValueError(f"备份包里的项目标识不合法：{str(key)[:80]}")
            member = f"projects/{key}/project.db"
            if member not in raw_to_safe:
                continue          # 旧包可能少库：跳过，不影响其余项目
            dst = os.path.join(stage_root, "projects", f"{i:03d}", "project.db")
            _extract_member(zf, raw_to_safe[member], dst)
            _verify_db(dst, e.get("sha256") or "", ("meta", "chapters"))
            projects.append({"key": key, "title": e.get("title", "") or "",
                             "original_path": e.get("original_path", "") or "",
                             "db": dst})

        # ---- config.json（可选；坏包不能带着坏配置进替换阶段） ----
        config = None
        if "config.json" in raw_to_safe:
            try:
                cfg = json.loads(zf.read(raw_to_safe["config.json"]).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise ValueError(f"config.json 读不出来：{e}")
            if isinstance(cfg, dict):
                config = cfg
    return {"version": version, "app_db": local_app, "config": config,
            "jailbreaks": jailbreaks, "projects": projects}


# ---------- 导入：第二阶段（替换，每步可回退） ----------

def _close_global_conn(app_state) -> None:
    """关掉全局库连接（换文件前必须关，Windows 下打开着就换不掉）。"""
    g = app_state.gstore
    with g._lock:
        try:
            g.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        try:
            g.conn.close()
        except sqlite3.Error:
            pass


def _plan_project_dests(app_state, projects: list, projects_root: str) -> list:
    """每个项目落到哪：同标题覆盖本机原位，其余落 data_dir/projects/<key>。"""
    by_title = {}
    for r in app_state.list_recent():
        p = r.get("path") or ""
        # 落点是缓存目录的一律不复用：那是旧版把数据目录算错时留下的
        # 死记录（安卓清缓存即丢），照它恢复等于又把书放进会被清的地方
        if r.get("title") and p and not is_cache_path(p) \
                and r["title"] not in by_title:
            by_title[r["title"]] = p
    used, out = set(), []
    for p in projects:
        base = by_title.get(p.get("title") or "") or os.path.join(projects_root, p["key"])
        dest = base
        n = 2
        # 同名两本书不能落同一个文件夹（否则后一本整本盖掉前一本）
        while os.path.normcase(os.path.abspath(dest)) in used:
            dest = f"{base}-{n}"
            n += 1
        used.add(os.path.normcase(os.path.abspath(dest)))
        out.append((p, dest))
    return out


def _install_global(app_state, staged_db: str, app_db: str, undo: _ImportUndo) -> None:
    _close_global_conn(app_state)
    _remove_sqlite_sidecars(app_db)
    undo.stash(app_db)          # 旧库先进回滚区，装坏了能原样拎回来
    _install_file(staged_db, app_db)


def _install_jailbreaks(jailbreaks: list, jb_dir: str, undo: _ImportUndo) -> None:
    undo.stash(jb_dir)          # 整目录移进回滚区（同盘 rename，内容不动）
    os.makedirs(jb_dir, exist_ok=True)
    undo.track(jb_dir)
    for name, staged in jailbreaks:
        _install_file(staged, os.path.join(jb_dir, name))


def _install_projects(dests: list, undo: _ImportUndo) -> dict:
    path_map = {}
    for p, dest in dests:
        if not os.path.isdir(dest):
            os.makedirs(dest, exist_ok=True)
            undo.track(dest)
        db = os.path.join(dest, "project.db")
        _remove_sqlite_sidecars(db)
        undo.stash(db)
        _install_file(p["db"], db)
        path_map[p.get("original_path") or ""] = dest
    return path_map


def _apply_config(app_state, config, path_map: dict) -> None:
    """config：recent_projects 用备份包的（路径重写），其余设备本地设置保留。"""
    if config:
        recents = []
        for item in config.get("recent_projects") or []:
            if not isinstance(item, dict):
                continue
            new_path = path_map.get(item.get("path", ""))
            if new_path:
                recents.append({"path": new_path, "title": item.get("title", ""),
                                "last_open": item.get("last_open", "")})
        app_state.config["recent_projects"] = recents
    app_state.save_config()


def import_full_backup(zip_path: str, app_state, password=None) -> tuple[bool, str]:
    """全量恢复：整体替换本机全局库/破甲库/项目列表。完成后需重启应用。

    自动识别两种包：**加密容器（JGB1，默认产物）** 与 **旧版明文 zip**。
    `password` 只对加密包有意义；缺密码/密码错/密文被篡改都在第一阶段
    （临时目录校验期）就返回，本机数据一字不动，也不会写安全备份。
    """
    try:
        with tempfile.TemporaryDirectory(prefix="jgimport_") as stage_root:
            info = describe_backup_file(zip_path)
            kind = info.get("kind")
            if kind == "unknown":
                return False, f"不是有效的全量备份文件：{info.get('reason') or '无法识别'}"
            encrypted = kind == "encrypted"
            if encrypted and not password:
                return False, ("这是加密的全量备份，请输入备份密码"
                               "（密码丢了文件就打不开，只能从别的备份重来）")
            # 加密包先解成明文 zip 落在临时目录（跟着 TemporaryDirectory 一起删）
            plain_zip = os.path.join(stage_root, "backup.zip")
            try:
                backup_plain_zip(zip_path, password, plain_zip)
            except (CryptoError, ValueError, OSError) as e:
                return False, f"导入失败：{e}"
            # ---- 第一阶段：全部校验完再动本机（顺序不许调） ----
            plan = _stage_backup(plain_zip, stage_root)
            data_dir = app_state.data_dir
            # ---- 安全网：先把本机现状整包备份，失败即中止；加密状态与来源一致 ----
            app_state.close_project()
            backup_dir = os.path.join(data_dir, "backups")
            os.makedirs(backup_dir, exist_ok=True)
            safety = os.path.join(
                backup_dir, f"full_before_import_{datetime.now():%Y%m%d_%H%M%S_%f}.zip")
            ok, msg = export_full_backup(safety, app_state, password=password,
                                         allow_plain=not encrypted)
            if not ok:
                return False, f"导入已中止：导入前的安全备份没做成（{msg}）"

            # ---- 第二阶段：替换 ----
            app_db = os.path.join(data_dir, "app.db")
            jb_dir = os.path.join(data_dir, "jailbreaks")
            projects_root = os.path.join(data_dir, "projects")
            dests = _plan_project_dests(app_state, plan["projects"], projects_root)
            # 停靠区里的库还开着连接：Windows 下换不掉文件，后台任务也会读到半新
            # 半旧的数据 → 提前拒绝（此时本机数据仍是一字未动）
            for p, dest in dests:
                if app_state.parked_store(dest) is not None:
                    return False, (f"导入已中止：《{p['title']}》正在后台任务中使用，"
                                   "等它跑完再导入")
            saved_cfg = copy.deepcopy(app_state.config)
            rollback_dir = tempfile.mkdtemp(prefix=".import_rollback_", dir=data_dir)
            undo = _ImportUndo(rollback_dir)
            try:
                _install_global(app_state, plan["app_db"], app_db, undo)
                app_state.gstore.reopen()       # 按路径单例：reopen 让旧引用继续有效
                _install_jailbreaks(plan["jailbreaks"], jb_dir, undo)
                path_map = _install_projects(dests, undo)
                _apply_config(app_state, plan["config"], path_map)
            except Exception as e:              # 回滚不能挑异常类型
                _close_global_conn(app_state)   # 先关连接，否则原件换不回来
                failed = undo.undo()
                app_state.config = saved_cfg
                app_state.save_config()
                try:
                    app_state.gstore.reopen()
                except Exception:
                    pass
                if failed:
                    return False, (f"导入失败，且以下内容没能自动复原："
                                   f"{'、'.join(failed)}\n回滚备份保留在：{rollback_dir}\n"
                                   f"原始错误：{type(e).__name__}: {e}")
                shutil.rmtree(rollback_dir, ignore_errors=True)
                return False, f"导入失败，本机数据已恢复原状：{type(e).__name__}: {e}"
            shutil.rmtree(rollback_dir, ignore_errors=True)
            return True, (f"导入完成：{len(path_map)} 个项目 + 全局配置 + 破甲库。\n"
                          "请重启应用使全部更改生效。")
    except (OSError, sqlite3.Error, zipfile.BadZipFile, json.JSONDecodeError,
            ValueError, KeyError, CryptoError) as e:
        return False, f"导入失败：{e}"

