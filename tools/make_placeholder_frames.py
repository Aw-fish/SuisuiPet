"""Generate placeholder sprite sets for the desktop pet.

Source of truth: ``data/img/Default.png`` (the artist's line art).
Outputs two interchangeable variants:

* ``data/img1`` — detail preserved, only matted / cropped / downscaled.
* ``data/img2`` — line art simplified (binarised, denoised, slightly bolded)
  so it stays readable when the pet is shown at ~224px tall.

Both folders follow the flat naming convention consumed by
``app.pet.character_sprite``::

    Default.png  Idle1..3  Move1..4  Talk1..3  Focus1..4  Drag1..2
    Blink.png  Happy.png  Sad.png  Angry.png  Surprised.png  Think.png

The script is idempotent: ``data/img/Default.png`` is never modified and every
derived file is simply rewritten.

Usage::

    python tools/make_placeholder_frames.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT_DIR = Path(__file__).resolve().parent.parent
SOURCE_PATH = ROOT_DIR / "data" / "img" / "Default.png"
DETAIL_DIR = ROOT_DIR / "data" / "img1"
SIMPLE_DIR = ROOT_DIR / "data" / "img2"

TARGET_HEIGHT = 512
CROP_MARGIN = 0.04
MATTE_TOLERANCE = 24
BINARY_THRESHOLD = 150

# 帧生成配方：scale 缩放 / dx,dy 像素偏移（负值向上）/ angle 旋转角度
ANIMATIONS: dict[str, list[dict[str, float]]] = {
    "Idle": [{}, {"scale": 1.02, "dy": -3}, {}],
    "Move": [{}, {"dy": -12}, {}, {"dy": -7}],
    "Talk": [{}, {"scale": 1.01, "dy": -2}, {}],
    "Focus": [{"dy": 2}, {"scale": 0.99, "dy": 3}, {"dy": 2}, {"scale": 0.99, "dy": 3}],
    "Drag": [{"angle": -4}, {"angle": 4}],
}

# 整张全身立绘的表情（暂用 Default 副本占位，等美术出图后同名覆盖）
EXPRESSIONS = ("Blink", "Happy", "Sad", "Angry", "Surprised", "Think")


def _matte(image: Image.Image) -> Image.Image:
    """Turn the white background into transparency, keeping inner white fills."""
    rgba = image.convert("RGBA")
    if rgba.getchannel("A").getextrema()[0] < 255:
        return rgba
    rgb = rgba.convert("RGB")
    work = rgb.copy()
    magic = (255, 0, 255)
    width, height = work.size
    for seed in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
        if work.getpixel(seed) != magic:
            ImageDraw.floodfill(work, seed, magic, thresh=MATTE_TOLERANCE)
    background = (
        ImageChops.difference(work, Image.new("RGB", work.size, magic))
        .convert("L")
        .point(lambda value: 255 if value < 8 else 0)
    )
    alpha = ImageChops.invert(background)
    light = rgb.convert("L").point(lambda value: 255 if value >= 225 else 0)
    halo = ImageChops.multiply(background.filter(ImageFilter.MaxFilter(3)), light)
    rgba.putalpha(Image.composite(Image.new("L", work.size, 120), alpha, halo))
    return rgba


def _crop(image: Image.Image) -> Image.Image:
    """Trim to the character bounds so the pet fills the frame."""
    bbox = image.getchannel("A").getbbox()
    if bbox is None:
        return image
    width, height = image.size
    pad_x = int((bbox[2] - bbox[0]) * CROP_MARGIN)
    pad_y = int((bbox[3] - bbox[1]) * CROP_MARGIN)
    return image.crop(
        (
            max(0, bbox[0] - pad_x),
            max(0, bbox[1] - pad_y),
            min(width, bbox[2] + pad_x),
            min(height, bbox[3] + pad_y),
        )
    )


def _resize(image: Image.Image) -> Image.Image:
    width, height = image.size
    return image.resize((max(1, round(TARGET_HEIGHT * width / height)), TARGET_HEIGHT), Image.LANCZOS)


def _simplify(image: Image.Image) -> Image.Image:
    """Binarise and clean the line art so it survives downscaling."""
    binary = image.convert("L").point(lambda value: 0 if value < BINARY_THRESHOLD else 255)
    binary = binary.filter(ImageFilter.MedianFilter(3)).filter(ImageFilter.MinFilter(3))
    simplified = Image.merge("RGB", (binary, binary, binary)).convert("RGBA")
    simplified.putalpha(image.getchannel("A"))
    return simplified


def _frame(base: Image.Image, scale: float = 1.0, dx: int = 0, dy: int = 0, angle: float = 0.0) -> Image.Image:
    """Build one frame from the base art, anchored at the bottom centre."""
    width, height = base.size
    layer = base
    if scale != 1.0:
        layer = layer.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.LANCZOS)
    if angle:
        layer = layer.rotate(angle, resample=Image.BICUBIC, expand=False)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    canvas.alpha_composite(layer, ((width - layer.width) // 2 + dx, height - layer.height + dy))
    return canvas


def _save(directory: Path, name: str, index: int | None, image: Image.Image) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    image.save(directory / f"{name}{'' if index is None else index}.png", optimize=True)


def build_variant(directory: Path, base: Image.Image) -> list[str]:
    written = [f"Default.png ({base.width}x{base.height})"]
    _save(directory, "Default", None, base)
    for name, recipe in ANIMATIONS.items():
        for index, options in enumerate(recipe, start=1):
            _save(directory, name, index, _frame(base, **options))
        written.append(f"{name}1..{len(recipe)}")
    for name in EXPRESSIONS:
        _save(directory, name, None, base)
    written.append(f"{' '.join(f'{name}.png' for name in EXPRESSIONS)} (Default 占位)")
    return written


def main() -> None:
    if not SOURCE_PATH.exists():
        raise SystemExit(f"找不到源立绘：{SOURCE_PATH}")
    with Image.open(SOURCE_PATH) as source:
        prepared = _crop(_matte(source))
    variants = {
        DETAIL_DIR: _resize(prepared),
        SIMPLE_DIR: _resize(_simplify(prepared)),
    }
    for directory, base in variants.items():
        for line in build_variant(directory, base):
            print(f"[{directory.name}] {line}")


if __name__ == "__main__":
    main()
