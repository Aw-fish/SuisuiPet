"""生成应用图标：``data/icon.png`` 与 ``data/icon.ico``。

图案与界面主色保持一致（紫色圆角底板 + 线稿风格的团子脸），
小尺寸下简化为高对比的色块，保证任务栏/tray 里能看清。

Usage::

    python tools/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"

SIZE = 256
SUPERSAMPLE = 4
ACCENT = (145, 125, 232, 255)
LINE = (76, 71, 89, 255)
WHITE = (255, 255, 255, 255)
BLUSH = (255, 198, 208, 255)
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def draw_icon(size: int = SIZE) -> Image.Image:
    scale = SUPERSAMPLE
    canvas = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    def box(*values: int) -> tuple[int, int, int, int]:
        return tuple(value * scale for value in values)  # type: ignore[return-value]

    def line(*values: int) -> tuple[int, int]:
        return (values[0] * scale, values[1] * scale)

    # 圆角底板
    draw.rounded_rectangle(box(10, 10, 246, 246), radius=58 * scale, fill=ACCENT)
    # 耳朵（画在脑袋之前，让脑袋的描边压在上面）
    draw.polygon([line(88, 78), line(66, 32), line(120, 58)], fill=WHITE, outline=LINE, width=7 * scale)
    draw.polygon([line(168, 78), line(190, 32), line(136, 58)], fill=WHITE, outline=LINE, width=7 * scale)
    # 脑袋
    draw.ellipse(box(52, 58, 204, 212), fill=WHITE, outline=LINE, width=7 * scale)
    # 眼睛
    draw.ellipse(box(94, 118, 116, 144), fill=LINE)
    draw.ellipse(box(140, 118, 162, 144), fill=LINE)
    # 腮红
    draw.ellipse(box(74, 142, 100, 158), fill=BLUSH)
    draw.ellipse(box(156, 142, 182, 158), fill=BLUSH)
    # 微笑
    draw.arc(box(108, 134, 148, 176), start=20, end=160, fill=LINE, width=6 * scale)
    return canvas.resize((size, size), Image.LANCZOS)


def main() -> None:
    icon = draw_icon()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    png_path = DATA_DIR / "icon.png"
    ico_path = DATA_DIR / "icon.ico"
    icon.save(png_path, optimize=True)
    icon.save(ico_path, sizes=ICO_SIZES)
    print(f"wrote {png_path}")
    print(f"wrote {ico_path}")


if __name__ == "__main__":
    main()
