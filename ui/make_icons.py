# -*- coding: utf-8 -*-
"""favicon.svg から PNG / ICO を作る。

SVG だけでも今のブラウザなら足りるが、
  ・Windows のタスクバー／ショートカット（アプリとして入れたとき）
  ・PWA のマニフェスト
は PNG を要求するので、こちらで用意しておく。

⚠️ **手で描き直さないこと。** 元は favicon.svg の1枚だけにして、ここから機械的に作る。
   （2枚を手で合わせると必ずズレる）

    python ui/make_icons.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "static"

# favicon.svg と同じ形。SVGを解釈するより、同じ寸法で描き直すほうが依存が少ない
# （数値は 64x64 の viewBox 基準。下で任意サイズに拡大する）
BG = "#1b64c4"
BARS = [
    (14, 15, 24, 8, (251, 191, 36, 255)),    # 大見出し
    (14, 29, 36, 6, (255, 255, 255, 255)),   # 本文
    (14, 40, 36, 6, (255, 255, 255, 255)),
    (14, 51, 19, 6, (255, 255, 255, 166)),
]


def render(size: int) -> Image.Image:
    ss = 8                                    # 8倍で描いて縮める（角丸をなめらかにする）
    n = size * ss
    k = n / 64
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, n - 1, n - 1], radius=14 * k, fill=BG)
    for x, y, w, h, color in BARS:
        d.rounded_rectangle([x * k, y * k, (x + w) * k, (y + h) * k],
                            radius=h * k / 2, fill=color)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    for size in (192, 512):
        render(size).save(OUT / f"icon-{size}.png")
    render(180).save(OUT / "apple-touch-icon.png")
    # .ico は複数サイズを1つに束ねる。16/32 はタブとタスクバー、48 はエクスプローラー
    render(256).save(OUT / "favicon.ico",
                     sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("作成:", *(p.name for p in sorted(OUT.glob("*.png"))), "favicon.ico")


if __name__ == "__main__":
    main()
