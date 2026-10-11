#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成飞牛 FPK 应用图标：圆角深色底 + 5 叶风扇。

本仓库不存二进制，需要图标时现生成。依赖 Pillow：

    pip install Pillow
    python3 tools/make-icons.py                       # 输出到 ../qu805-fancontrol
    python3 tools/make-icons.py --out ../qu805-fancontrol
    python3 tools/make-icons.py --size 512            # 只生成一张 512 预览

生成 4 个文件（飞牛的约定：项目根两个 + ui/images 两个）：

    ICON.PNG                  64×64
    ICON_256.PNG              256×256
    app/ui/images/icon_64.png
    app/ui/images/icon_256.png

画法：4 倍超采样（1024×1024）后 LANCZOS 缩小，边缘干净。
5 片叶子由「前缘弧 + 叶尖弧 + 后缘弧 + 根部弧」围成，每片明度略不同以显立体。
"""

import argparse
import math
import os
import sys

W = 1024                      # 工作分辨率
C = W / 2.0
R_OUT = W * 0.400             # 叶尖半径
R_IN = W * 0.152              # 根部半径
N = 5                         # 叶片数
SPAN = math.radians(50)       # 单片张角
LAG = math.radians(26)        # 内外缘角差 → 叶片弯曲
STEPS = 64                    # 每段采样点数


def polar(rad, ang):
    return (C + rad * math.cos(ang), C + rad * math.sin(ang))


def build():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))

    # 圆角方块底：竖直渐变 #0f131a → #1d2735
    pad, rad_corner = int(W * 0.03), int(W * 0.215)
    bg = Image.new("RGBA", (W, W))
    bd = ImageDraw.Draw(bg)
    for y in range(W):
        t = y / (W - 1)
        bd.line([(0, y), (W, y)], fill=(
            int(15 + (29 - 15) * t),
            int(19 + (39 - 19) * t),
            int(26 + (53 - 26) * t), 255))
    mask = Image.new("L", (W, W), 0)
    ImageDraw.Draw(mask).rounded_rectangle([pad, pad, W - pad, W - pad],
                                           radius=rad_corner, fill=255)
    img.paste(bg, (0, 0), mask)
    d = ImageDraw.Draw(img)

    # 风扇叶片
    for k in range(N):
        base = 2 * math.pi * k / N - math.pi / 2
        pts = []
        for i in range(STEPS + 1):                    # 前缘：轮毂 → 外缘
            f = i / STEPS
            pts.append(polar(R_IN + (R_OUT - R_IN) * f, base + LAG * (1 - f)))
        for i in range(1, STEPS + 1):                 # 叶尖圆弧
            pts.append(polar(R_OUT, base + SPAN * i / STEPS))
        for i in range(STEPS + 1):                    # 后缘：外缘 → 轮毂
            f = i / STEPS
            pts.append(polar(R_OUT - (R_OUT - R_IN) * f, base + SPAN + LAG * f))
        for i in range(1, STEPS + 1):                 # 根部圆弧
            pts.append(polar(R_IN, base + SPAN + LAG - LAG * i / STEPS))
        t = k / N
        col = (int(96 + 46 * t), int(210 + 24 * t), int(246 - 10 * t), 255)
        d.polygon(pts, fill=col)

    # 轮毂
    hub = R_IN * 0.94
    d.ellipse([C - hub, C - hub, C + hub, C + hub], fill=(238, 248, 255, 255))
    d.ellipse([C - hub * 0.52, C - hub * 0.52, C + hub * 0.52, C + hub * 0.52],
              fill=(30, 42, 58, 255))
    return img


def main():
    ap = argparse.ArgumentParser(description="生成 QU805 风扇温控应用图标")
    ap.add_argument("--out", default=None,
                    help="项目根目录，默认 ../qu805-fancontrol（相对本脚本）")
    ap.add_argument("--size", type=int, default=None,
                    help="只生成一张指定尺寸的预览图（不写 4 个标准文件）")
    args = ap.parse_args()

    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        sys.exit("缺少 Pillow：pip install Pillow")

    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.abspath(args.out or os.path.join(here, "..", "qu805-fancontrol"))

    print("绘制中（%d×%d 超采样）..." % (W, W))
    img = build()

    if args.size:
        dst = os.path.join(out, "ICON_preview_%d.png" % args.size)
        os.makedirs(out, exist_ok=True)
        img.resize((args.size, args.size), Image.LANCZOS).save(dst, "PNG", optimize=True)
        print("  %s" % dst)
        return

    targets = [
        (os.path.join(out, "ICON.PNG"), 64),
        (os.path.join(out, "ICON_256.PNG"), 256),
        (os.path.join(out, "app/ui/images/icon_64.png"), 64),
        (os.path.join(out, "app/ui/images/icon_256.png"), 256),
    ]
    for path, size in targets:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        img.resize((size, size), Image.LANCZOS).save(path, "PNG", optimize=True)
        print("  %-46s %4d×%-4d %6d 字节"
              % (os.path.relpath(path, out), size, size, os.path.getsize(path)))
    print("\n完成。四个文件都在 %s 下。" % out)


if __name__ == "__main__":
    main()
