# -*- coding: utf-8 -*-
"""电脑版 图标.png → 手机版应用图标（icon.png / icon_android.png）。

用法：主项目 .venv 的 python make_mobile_icon.py [--preview]
主项目路径可用环境变量 JIUGUAN_REPO 覆盖。

产物（写入 src/assets/，flet build 自动取用）：
- icon.png           1024×1024 满幅深蓝底（补圆角为直角），方形图标/商店展示用；
                     各平台通用兜底（iOS/Windows/macOS/web 也认它）
- icon_android.png   1024×1024 深蓝画布 + 整图缩至 66% 居中（边缘羽化），
                     作为安卓自适应图标前景层——圆形裁切只显示中心约 66%，
                     不缩放书本会被切边；配合构建参数
                     --android-adaptive-icon-background <底色> 使用。
- build/icon_sim_*.png  模拟启动器方形/圆形裁切的预览图（人工核对用）。
"""
import argparse
import os
import sys

from PIL import Image, ImageDraw, ImageFilter

SRC_DEFAULT = r"E:\AI Agent\work area\酒馆小说生成器\图标.png"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src", "assets")
PREVIEW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build")
SIZE = 1024
# 自适应图标安全区：可见直径 ≈ 66/108，书本横向撑满，前景层缩到 66% 才完整
FG_SCALE = 0.66
# 书本+铅笔包围盒（1024×1024 裁方坐标；含周围一圈深蓝底便于边缘融合）
# 换图标时需重新目测调整
BOOK_BBOX = (40, 140, 980, 880)


def load_square(src_path: str) -> Image.Image:
    """等比铺满 1024×1024 再中心裁剪（与电脑版 make_icon.py 同逻辑）。"""
    im = Image.open(src_path).convert("RGBA")
    scale = max(SIZE / im.width, SIZE / im.height)
    im = im.resize((round(im.width * scale), round(im.height * scale)),
                   Image.LANCZOS)
    x = (im.width - SIZE) // 2
    y = (im.height - SIZE) // 2
    im = im.crop((x, y, x + SIZE, y + SIZE))
    return _strip_white_border(im)


def _strip_white_border(im: Image.Image) -> Image.Image:
    """去掉图标外圈白色描边与四角白底：从四角洪泛填充，白色连通区变透明。

    书本/铅笔的白色在深蓝方块内部、与外部白不连通，不会被误伤。
    """
    im = im.copy()
    for seed in ((0, 0), (SIZE - 1, 0), (0, SIZE - 1), (SIZE - 1, SIZE - 1)):
        ImageDraw.floodfill(im, seed, (1, 2, 3, 0), thresh=60)
    px = im.load()
    n = 0
    for yy in range(SIZE):
        for xx in range(SIZE):
            if px[xx, yy] == (1, 2, 3, 0):
                px[xx, yy] = (0, 0, 0, 0)
                n += 1
    print(f"已去除白色描边/四角：{n} 像素变透明")
    return im


def pick_bg_color(im: Image.Image) -> tuple[int, int, int]:
    """取图标底色：四角向内 8% 采样点的中位数（避开圆角外透明像素）。"""
    m = int(SIZE * 0.08)
    pts = [(m, m), (SIZE - m, m), (m, SIZE - m), (SIZE - m, SIZE - m),
           (SIZE // 2, m), (SIZE // 2, SIZE - m), (m, SIZE // 2),
           (SIZE - m, SIZE // 2)]
    samples = [im.getpixel(p)[:3] for p in pts if im.getpixel(p)[3] > 200]
    if not samples:
        raise RuntimeError("采样不到底色（图标边缘全透明？）")
    ch = list(zip(*samples))
    return tuple(int(sorted(c)[len(c) // 2]) for c in ch)


def make_full(im: Image.Image, bg: tuple) -> Image.Image:
    """方形满幅：深蓝底画布 + 原图居中叠加（圆角外透明处露出同色底）。"""
    canvas = Image.new("RGBA", (SIZE, SIZE), bg + (255,))
    canvas.alpha_composite(im)
    return canvas


def make_foreground(im: Image.Image, bg: tuple) -> Image.Image:
    """自适应前景：深蓝画布 + 书本区域缩放居中。

    不整图缩放（白描边/立体边缘会在圆内形成「图中图」），改为裁出书本
    包围盒（BOOK_BBOX，含周围一圈深蓝底），缩放至安全区居中，边缘
    拉平+羽化融入画布底色——圆内只见深蓝底 + 完整书本。
    """
    inner = int(SIZE * FG_SCALE)
    box = im.crop(BOOK_BBOX)
    scale = min(inner / box.width, inner / box.height)
    box = box.resize((round(box.width * scale), round(box.height * scale)),
                     Image.LANCZOS)
    box = _flatten_bg(box, bg, tol=28)
    alpha = box.getchannel("A").filter(ImageFilter.GaussianBlur(6))
    box.putalpha(alpha)
    canvas = Image.new("RGBA", (SIZE, SIZE), bg + (255,))
    off = ((SIZE - box.width) // 2, (SIZE - box.height) // 2)
    canvas.alpha_composite(box, off)
    return canvas


def _flatten_bg(im: Image.Image, bg: tuple, tol: int) -> Image.Image:
    """把接近底色 bg 的像素替换为纯 bg（欧氏色距 < tol），消除区域边界。"""
    im = im.copy()
    px = im.load()
    n = 0
    for yy in range(im.height):
        for xx in range(im.width):
            r, g, b, a = px[xx, yy]
            if a == 0:
                continue
            d2 = (r - bg[0]) ** 2 + (g - bg[1]) ** 2 + (b - bg[2]) ** 2
            if d2 < tol * tol:
                px[xx, yy] = (bg[0], bg[1], bg[2], 255)
                n += 1
    print(f"底色拉平：{n} 像素统一为 #{bg[0]:02X}{bg[1]:02X}{bg[2]:02X}")
    return im


def previews(full: Image.Image, fg: Image.Image) -> list[tuple[str, Image.Image]]:
    """模拟启动器显示：方形满幅、圆形裁自适应前景。"""
    mask_circle = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask_circle).ellipse((0, 0, SIZE - 1, SIZE - 1), fill=255)
    circled_fg = Image.new("RGBA", (SIZE, SIZE), (255, 255, 255, 255))
    circled_fg.paste(fg, (0, 0), mask_circle)
    return [("icon_sim_square", full), ("icon_sim_circle", circled_fg)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true", help="只出预览不写 assets")
    args = ap.parse_args()

    src = os.environ.get("JIUGUAN_ICON_SRC") or SRC_DEFAULT
    if not os.path.isfile(src):
        print(f"错误：找不到源图标 {src}", file=sys.stderr)
        return 1

    square = load_square(src)
    bg = pick_bg_color(square)
    print(f"底色采样：#{bg[0]:02X}{bg[1]:02X}{bg[2]:02X}")

    full = make_full(square, bg)
    fg = make_foreground(square, bg)

    os.makedirs(PREVIEW_DIR, exist_ok=True)
    for name, img in previews(full, fg):
        img.convert("RGB").resize((512, 512), Image.LANCZOS).save(
            os.path.join(PREVIEW_DIR, f"{name}.png"))
    print("预览已出：build/icon_sim_*.png")

    if args.preview:
        return 0
    for name, img in (("icon.png", full), ("icon_android.png", fg)):
        img.save(os.path.join(OUT_DIR, name))
        print(f"已写入 src/assets/{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
