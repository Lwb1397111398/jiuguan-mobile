# -*- coding: utf-8 -*-
r"""全量备份「搬出应用私有目录」：另存到手机（SAF）+ 系统分享 + 包内清单。

**为什么要有这个模块（2026-11-20，P0 旧钥过渡版）**

换应用签名后，安卓不允许「签名不同」的包覆盖安装，用户必须先卸载再装新版；
而卸载会删掉 `/data/data/<包名>/`（含本应用数据目录与里面的 `exports/*.zip`）。
所以在换钥之前，必须先把「能备份到卸载不会删的地方」送到用户手上：

- **另存到手机**：`FilePicker.save_file`，安卓侧是 SAF（`ACTION_CREATE_DOCUMENT`），
  由用户选「下载」或网盘目录，写入动作由系统完成，卸载 App 不影响那份文件。
  Flet 0.86 在 Android 要求把文件字节一起传（`src_bytes`），字节必然过一次
  内存，所以设上限 `SAF_MAX_BYTES`：超了就改用分享（share_plus 是原生复制，
  不占 Python 内存），绝不静默失败。
- **系统分享**：`ft.Share.share_files`（share_plus），可发到微信/网盘/文件管理器。
  share_plus 会把文件复制到自己的缓存再用 FileProvider 交给目标应用，因此
  应用私有目录里的文件也能分享。
- **包内清单**：数字来自 `app.core.exporter.backup_summary`（与
  `export_full_backup` 同一筛选口径），导出后让用户确认「书/破甲/API Key」
  都在，别把「导出成功但漏了项目」当成备份成功。

**不新增明文绕路**：这里的两个能力都只搬运「已经由导出流程生成的那份文件」，
加密与否仍由 `export_full_backup` 的密码规则决定（默认 JGB1 加密）。
"""
import os

import flet as ft

# 另存通道要把整个文件读进 Python 内存（Flet 要求 src_bytes），128 MB 是
# 平板可承受的上限；超过它就引导用户走「分享」或云同步，绝不 OOM 崩掉 App。
SAF_MAX_BYTES = 128 * 1024 * 1024

BACKUP_EXT = "zip"

SAFE_HINT = (
    "卸载 App 会删掉应用私有目录里的备份；「另存到手机」（下载/网盘）或"
    "「分享」出去的那一份不会删。备份里能解出 API Key：只放自己的网盘或本地，"
    "别发公开群、别用公开分享链接。")


def mount_service(page, service):
    """把 Flet 服务控件挂到 page.services（幂等）。

    flet 0.86：FilePicker/Share 是服务控件，必须挂 `page.services`；
    挂到 `page.overlay` 会被前端画成「Unknown control」红块（2026-11-15 真机）。
    """
    try:
        if service not in page.services:
            page.services.append(service)
    except Exception:
        pass


def unmount_service(page, service):
    """从 page.services 摘掉服务控件（页面被整体丢弃时调用），失败不抛。"""
    try:
        if service in page.services:
            page.services.remove(service)
    except Exception:
        pass


# ---------------- 包内清单 ----------------

def receipt_text(summary) -> str:
    """把 `backup_summary` 的结果写成给用户看的两三行清单。"""
    if not isinstance(summary, dict):
        return "包内清单：读取失败（以导出结果的提示为准）"
    n = int(summary.get("projects") or 0)
    jb = int(summary.get("jailbreaks") or 0)
    key = ("包含（已加密在文件里）" if summary.get("has_api_key")
           else "未包含（本机没有配置 API Key）")
    line = f"包内清单：{n} 本书、{jb} 套破甲；API Key {key}"
    titles = [str(t) for t in (summary.get("titles") or []) if str(t).strip()]
    if titles:
        preview = "、".join(titles[:3])
        if len(titles) > 3:
            preview += f" 等 {len(titles)} 本"
        line += f"\n书：{preview}"
    return line


# ---------------- 另存到手机（SAF） ----------------

def read_backup_bytes(path, limit=None):
    """读备份文件字节 → (bytes|None, 错误说明)。

    超限/读不到一律返回 None + 中文原因（调用方把原因原样告诉用户），
    绝不截断、绝不返回半份内容。
    """
    limit = SAF_MAX_BYTES if limit is None else int(limit)
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return None, f"读不到备份文件：{e}"
    if size <= 0:
        return None, "备份文件是空的（导出可能没成功），请重新导出"
    if size > limit:
        return None, (
            f"备份有 {size / 1048576:.1f} MB，超过「另存到手机」的 "
            f"{limit / 1048576:.0f} MB 上限（这个通道要把整个文件读进内存）。"
            "请改用「分享」（可直接发到网盘/微信），或先云同步。")
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return None, f"读不到备份文件：{e}"
    if len(data) != size:
        return None, "备份文件在读取时被改动（大小不一致），请重新导出后再试"
    return data, ""


async def save_backup_copy(picker, src_path, file_name=None, limit=None) -> dict:
    """把备份另存到用户选的位置（安卓 = SAF，卸载不丢）。

    返回 {"ok": bool, "cancelled": bool, "path": str, "size": int, "error": str}
    - `cancelled=True`：用户自己取消，不是错误（界面不要报红）；
    - `path`：系统返回的文档路径（安卓是 `/document/...` 形状，只作凭据展示，
      不代表本应用还能按这个路径读回来）；
    - `ok=False` 且没取消：`error` 是给用户看的中文原因。
    """
    data, err = read_backup_bytes(src_path, limit=limit)
    if data is None:
        return {"ok": False, "cancelled": False, "path": "", "size": 0,
                "error": err}
    name = str(file_name or os.path.basename(str(src_path)) or
               f"全量备份.{BACKUP_EXT}")
    if not name.lower().endswith("." + BACKUP_EXT):
        name += "." + BACKUP_EXT
    try:
        dest = await picker.save_file(
            dialog_title="另存到手机（选「下载」或网盘目录）",
            file_name=name,
            file_type=ft.FilePickerFileType.ANY,
            src_bytes=data,
        )
    except Exception as e:          # 平台通道异常：原样上报，别谎称成功
        return {"ok": False, "cancelled": False, "path": "", "size": 0,
                "error": f"另存失败：{type(e).__name__}: {e}"}
    if not dest:
        return {"ok": False, "cancelled": True, "path": "", "size": 0,
                "error": ""}
    return {"ok": True, "cancelled": False, "path": str(dest),
            "size": len(data), "error": ""}


# ---------------- 系统分享 ----------------

async def share_backup(share, src_path, *, title="", text="") -> dict:
    """把备份交给系统分享面板（微信/网盘/文件管理器）。

    share_plus 自己把文件复制到缓存再交出去，所以私有目录里的文件也能分享。
    返回 {"ok": bool, "dismissed": bool, "status": str, "error": str}；
    用户关掉分享面板（dismissed）不算失败。
    """
    if not src_path or not os.path.isfile(str(src_path)):
        return {"ok": False, "dismissed": False, "status": "",
                "error": "备份文件不在了（可能被清理），请重新导出"}
    try:
        result = await share.share_files(
            [ft.ShareFile(path=str(src_path),
                          name=os.path.basename(str(src_path)),
                          mime_type="application/zip")],
            title=title or "分享全量备份",
            text=text or "",
        )
    except Exception as e:
        return {"ok": False, "dismissed": False, "status": "",
                "error": f"分享失败：{type(e).__name__}: {e}"}
    status = str(getattr(getattr(result, "status", ""), "value", "")
                 or getattr(result, "status", "") or "")
    return {"ok": True, "dismissed": status == "dismissed",
            "status": status, "error": ""}
