# -*- coding: utf-8 -*-
"""导出：正文（TXT/Markdown/DOCX）+ 项目打包（zip 跨设备导入）+ 全量备份（双端同步）。"""
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from xml.sax.saxutils import escape

from .store import ProjectStore
from .chapter_outline import chapter_outline_text
from .api import encrypt_key, decrypt_key

EXPORT_FORMATS = ["TXT", "Markdown", "DOCX"]


def _chapter_text(store: ProjectStore, chapter: dict, include_outline: bool) -> str:
    blocks = store.list_text_blocks(chapter["id"])
    accepted = [b for b in blocks if b["status"] in ("已接受", "已锁定")]
    parts = []
    if include_outline:
        summary = chapter_outline_text(chapter)
        if summary:
            parts.append("（本章概要：\n" + summary + "）")
        include_outline = False
    parts.extend(b["content"] for b in accepted)
    return "\n\n".join(parts)


def _group_by_volume(store: ProjectStore, chapters: list[dict]) -> list[tuple[str, list[dict]]]:
    """把章节按分卷分组，返回 [(卷标题, [章节...]), ...]。

    项目已有分卷结构（volumes 表 + chapters.volume_id），导出时保留它才符合长篇阅读
    习惯。无分卷数据时整本书归为一组（标题空串）——调用方据此跳过卷标题渲染，
    行为与改动前完全一致（向后兼容）。未分卷的章节（volume_id 为空）归到最后的
    「未分卷」组，不会因建了分卷就丢失。
    """
    volumes = store.list_volumes()
    if not volumes:
        return [("", chapters)]
    by_id: dict[int, list[dict]] = {v["id"]: [] for v in volumes}
    orphans: list[dict] = []
    for c in chapters:
        vid = c.get("volume_id")
        if vid in by_id:
            by_id[vid].append(c)
        else:
            orphans.append(c)
    groups = []
    for v in volumes:
        if by_id[v["id"]]:
            idx = v.get("order_index") or len(groups) + 1
            title = (v.get("title") or "").strip()
            groups.append((f"第{idx}卷 {title}".strip(), by_id[v["id"]]))
    if orphans:
        groups.append(("未分卷", orphans))
    return groups


def export_prose(store: ProjectStore, fmt: str, path: str,
                 chapter_ids: list[int] | None = None,
                 include_synopsis: bool = False,
                 include_outline: bool = False) -> tuple[bool, str]:
    """导出正文。fmt: TXT/Markdown/DOCX；chapter_ids None=全部。返回 (ok, message)。"""
    fmt = fmt.upper()
    if fmt not in ("TXT", "MARKDOWN", "DOCX"):
        return False, f"不支持的格式: {fmt}"
    chapters = store.list_chapters()
    if chapter_ids:
        chapters = [c for c in chapters if c["id"] in chapter_ids]
    title = store.title

    try:
        if fmt == "TXT":
            return _export_txt(store, chapters, title, path, include_synopsis, include_outline)
        if fmt == "MARKDOWN":
            return _export_md(store, chapters, title, path, include_synopsis, include_outline)
        return _export_docx(store, chapters, title, path, include_synopsis, include_outline)
    except ImportError:
        return False, "缺少 python-docx 库，无法导出 DOCX（TXT/Markdown 不受影响）"
    except OSError as e:
        return False, f"写入失败：{e}"


def _heading_prefix(store: ProjectStore, include_synopsis: bool) -> str:
    if not include_synopsis:
        return ""
    s = store.current_synopsis()
    if not s:
        return ""
    return (f"总纲：{s.get('title', '')}\n{s['content']}\n\n{'=' * 40}\n\n")


def _export_txt(store, chapters, title, path, include_synopsis, include_outline) -> tuple[bool, str]:
    lines = [f"{title}", "=" * 30, ""]
    head = _heading_prefix(store, include_synopsis)
    if head:
        lines.append(head)
    for vol_title, group in _group_by_volume(store, chapters):
        if vol_title:
            lines += ["", vol_title, "=" * 30, ""]
        for c in group:
            lines.append(f"第{c['order_index']}章 {c.get('title', '')}")
            lines.append("-" * 24)
            lines.append(_chapter_text(store, c, include_outline))
            lines.append("")
            lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return True, f"已导出 {len(chapters)} 章 → {path}"


def _export_md(store, chapters, title, path, include_synopsis, include_outline) -> tuple[bool, str]:
    lines = [f"# {title}", ""]
    head = _heading_prefix(store, include_synopsis)
    if head:
        lines.append(head)
    for vol_title, group in _group_by_volume(store, chapters):
        if vol_title:
            lines += [f"# {vol_title}", ""]
        for c in group:
            lines.append(f"## 第{c['order_index']}章 {c.get('title', '')}")
            lines.append("")
            lines.append(_chapter_text(store, c, include_outline))
            lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return True, f"已导出 {len(chapters)} 章 → {path}"


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
    doc.add_heading(title, 0)
    head = _heading_prefix(store, include_synopsis)
    if head:
        doc.add_paragraph(head)
    for vol_title, group in _group_by_volume(store, chapters):
        if vol_title:
            doc.add_heading(vol_title, 1)
        for c in group:
            doc.add_heading(f"第{c['order_index']}章 {c.get('title', '')}", 2)
            for para in _chapter_text(store, c, include_outline).split("\n\n"):
                doc.add_paragraph(para)
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
    """零依赖 DOCX 导出（结构与 _export_docx 一致：书名/卷/章/正文）。"""
    parts = [_docx_para(title, "Title")] if title else []
    head = _heading_prefix(store, include_synopsis)
    if head:
        parts.append(_docx_paragraphs(head))
    for vol_title, group in _group_by_volume(store, chapters):
        if vol_title:
            parts.append(_docx_para(vol_title, "Heading1"))
        for c in group:
            parts.append(_docx_para(f"第{c['order_index']}章 {c.get('title', '')}", "Heading2"))
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
    """
    with store._lock:
        store.conn.execute("PRAGMA wal_checkpoint(FULL)")
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(store.db_path, arcname="project.db")
            if include_backups:
                backup_dir = os.path.join(store.folder, "backups")
                if os.path.isdir(backup_dir):
                    for fn in os.listdir(backup_dir):
                        fp = os.path.join(backup_dir, fn)
                        if os.path.isfile(fp):
                            zf.write(fp, arcname=f"backups/{fn}")
            meta = {"type": "jiuguan_project", "version": 1,
                    "title": store.title, "exported_at": __import__("datetime").datetime.now().isoformat(timespec="seconds")}
            zf.writestr("manifest.json", json.dumps(meta, ensure_ascii=False, indent=2))
        return True, f"已导出 → {zip_path}"
    except OSError as e:
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

KEY_PORTABLE = "p3:"
KEY_KEEP = "k1:"
_EMBED_KEY_META = "global_setting_embed_api_key"


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


def _checkpoint_db(path: str):
    """把 WAL 日志合回主库文件，保证随后复制/打包出的库是完整的。"""
    if os.path.isfile(path):
        con = sqlite3.connect(path)
        try:
            con.execute("PRAGMA wal_checkpoint(FULL)")
        finally:
            con.close()


def _remove_sqlite_sidecars(db_path: str):
    """删除 -wal/-shm/-journal 残留：覆盖 db 文件后旧 sidecar 会污染新库。"""
    for suffix in ("-wal", "-shm", "-journal"):
        side = db_path + suffix
        if os.path.isfile(side):
            try:
                os.remove(side)
            except OSError:
                pass


def export_full_backup(zip_path: str, app_state) -> tuple[bool, str]:
    """全量备份到 zip_path。备份包含 API Key 明文（p3: 格式），须妥善保管。"""
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
        try:  # 全局库 checkpoint，保证 WAL 落盘
            # 必须持 _lock：与 store.py 的写法一致，否则并发写时 checkpoint
            # 可能抛 locked/直接跳过，导致备份包漏掉最近写入
            with app_state.gstore._lock:
                app_state.gstore.conn.execute("PRAGMA wal_checkpoint(FULL)")
        except Exception as e:
            # 不静默吞掉：checkpoint 失败 = 备份可能不含最新数据，得留痕可查
            print(f"[export] 全局库 WAL checkpoint 失败（备份可能落后于最新写入）: "
                  f"{type(e).__name__}: {e}")
        entries = []
        with tempfile.TemporaryDirectory() as tmp:
            rekeyed = os.path.join(tmp, "app.db")
            _rekey_db_copy(app_db, rekeyed, to_portable=True)
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(rekeyed, arcname="app.db")
                config_src = os.path.join(data_dir, "config.json")
                if os.path.isfile(config_src):
                    zf.write(config_src, arcname="config.json")
                jb_dir = os.path.join(data_dir, "jailbreaks")
                if os.path.isdir(jb_dir):
                    for fn in sorted(os.listdir(jb_dir)):
                        fp = os.path.join(jb_dir, fn)
                        if os.path.isfile(fp):
                            zf.write(fp, arcname=f"jailbreaks/{fn}")
                for i, r in enumerate(projects, 1):
                    key = f"{i:02d}_{_safe_name(r.get('title') or '')}"
                    _checkpoint_db(os.path.join(r["path"], "project.db"))
                    zf.write(os.path.join(r["path"], "project.db"),
                             arcname=f"projects/{key}/project.db")
                    entries.append({"key": key, "title": r.get("title", ""),
                                    "original_path": r["path"]})
                zf.writestr("manifest.json", json.dumps({
                    "type": "jiuguan_full_backup", "version": 1,
                    "exported_at": datetime.now().isoformat(timespec="seconds"),
                    "projects": entries,
                }, ensure_ascii=False, indent=2))
        return True, (f"已备份 {len(entries)} 个项目 + 全局配置（含 API Key，勿外传）→ {zip_path}")
    except (OSError, sqlite3.Error, zipfile.BadZipFile) as e:
        return False, f"备份失败：{e}"


def import_full_backup(zip_path: str, app_state) -> tuple[bool, str]:
    """全量恢复：整体替换本机全局库/破甲库/项目列表。完成后需重启应用。"""
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            if "manifest.json" not in names or "app.db" not in names:
                return False, "不是有效的全量备份文件（缺少 manifest.json / app.db）"
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            if manifest.get("type") != "jiuguan_full_backup":
                return False, "备份包类型不匹配"
            entries = manifest.get("projects", [])
            config_bytes = zf.read("config.json") if "config.json" in names else None
            jb_names = [n for n in names if n.startswith("jailbreaks/") and not n.endswith("/")]
            proj_keys = [e for e in entries
                         if f"projects/{e.get('key')}/project.db" in names]

        data_dir = app_state.data_dir
        # 安全网：导入前备份本机现状（当前项目连接先关掉，checkpoint 才完整）
        app_state.close_project()
        backup_dir = os.path.join(data_dir, "backups")
        os.makedirs(backup_dir, exist_ok=True)
        export_full_backup(os.path.join(
            backup_dir, f"full_before_import_{datetime.now():%Y%m%d_%H%M%S}.zip"), app_state)

        app_db = os.path.join(data_dir, "app.db")
        with tempfile.TemporaryDirectory() as tmp:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extract("app.db", tmp)
            rekeyed = os.path.join(tmp, "app_local.db")
            _rekey_db_copy(os.path.join(tmp, "app.db"), rekeyed, to_portable=False)
            try:
                app_state.gstore.conn.close()
            except sqlite3.Error:
                pass
            _remove_sqlite_sidecars(app_db)
            shutil.copy2(rekeyed, app_db)
            # GlobalStore 是按路径单例：reopen 让既有引用继续有效
            app_state.gstore.reopen()

            # 破甲库整体替换
            jb_dir = os.path.join(data_dir, "jailbreaks")
            if os.path.isdir(jb_dir):
                shutil.rmtree(jb_dir)
            os.makedirs(jb_dir, exist_ok=True)
            if jb_names:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    for n in jb_names:
                        zf.extract(n, data_dir)

            # 项目恢复：同标题覆盖本机原位，其余落到统一项目根 data_dir/projects/<key>
            projects_root = os.path.join(data_dir, "projects")
            os.makedirs(projects_root, exist_ok=True)
            by_title = {}
            for r in app_state.list_recent():
                if r.get("title") and r["title"] not in by_title:
                    by_title[r["title"]] = r["path"]
            path_map = {}
            with zipfile.ZipFile(zip_path, "r") as zf:
                for e in proj_keys:
                    key = e["key"]
                    dest = by_title.get(e.get("title", "")) \
                        or os.path.join(projects_root, key)
                    os.makedirs(dest, exist_ok=True)
                    _remove_sqlite_sidecars(os.path.join(dest, "project.db"))
                    zf.extract(f"projects/{key}/project.db", tmp)
                    shutil.copy2(os.path.join(tmp, f"projects/{key}/project.db"),
                                 os.path.join(dest, "project.db"))
                    path_map[e.get("original_path", "")] = dest

            # config：recent_projects 用备份包的（路径重写），其余设备本地设置保留
            if config_bytes:
                cfg = json.loads(config_bytes.decode("utf-8"))
                recents = []
                for item in cfg.get("recent_projects", []):
                    new_path = path_map.get(item.get("path", ""))
                    if new_path:
                        recents.append({"path": new_path, "title": item.get("title", ""),
                                        "last_open": item.get("last_open", "")})
                app_state.config["recent_projects"] = recents
            app_state.save_config()
        return True, (f"导入完成：{len(path_map)} 个项目 + 全局配置 + 破甲库。\n"
                      "请重启应用使全部更改生效。")
    except (OSError, sqlite3.Error, zipfile.BadZipFile, json.JSONDecodeError, KeyError) as e:
        return False, f"导入失败：{e}"
