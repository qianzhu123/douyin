"""Generate clean Lucide-style extension icons for the two extensions.

Outputs to:
- tools/douyin_extensions/downloader/icons/icon-{16,32,48,128}.png
- tools/douyin_extensions/live-overlay/icons/icon-{16,32,48,128}.png

The downloader icon is a downward arrow into a tray (lucide "download").
The live-overlay icon is an eye + viewers count (lucide "eye" + "users").

All icons are rendered as round-corner square PNGs in the brand color
of the project (#fe2c55 -- Douyin brand red) on a white-to-pink circle
background, with monochrome white glyph.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parent  # tools/douyin_extensions/
BRAND_RED = (254, 44, 85, 255)
WHITE = (255, 255, 255, 255)
SHADOW = (0, 0, 0, 24)


def round_rect(size: int, color: tuple[int, int, int, int]) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    radius = int(size * 0.22)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=color)
    return img


def draw_download_glyph(img: Image.Image, color: tuple[int, int, int, int]) -> None:
    """Lucide download icon: tray at the bottom, vertical stem + arrowhead above."""
    size = img.size[0]
    draw = ImageDraw.Draw(img)
    s = size
    # Tray
    tray_y0 = int(s * 0.74)
    tray_y1 = int(s * 0.80)
    tray_inset = int(s * 0.20)
    draw.rounded_rectangle(
        (tray_inset, tray_y0, s - tray_inset, tray_y1),
        radius=max(2, int(s * 0.02)),
        fill=color,
    )
    # Stem
    stem_w = max(2, int(s * 0.10))
    stem_top = int(s * 0.20)
    stem_bot = int(s * 0.68)
    cx = s // 2
    draw.rectangle((cx - stem_w // 2, stem_top, cx + stem_w // 2, stem_bot), fill=color)
    # Arrowhead (down-pointing chevron)
    a_w = max(3, int(s * 0.18))
    a_h = max(3, int(s * 0.10))
    tip_y = int(s * 0.74)
    draw.polygon(
        (
            (cx - a_w, stem_bot - a_h),
            (cx + a_w, stem_bot - a_h),
            (cx, tip_y),
        ),
        fill=color,
    )


def draw_eye_glyph(img: Image.Image, color: tuple[int, int, int, int]) -> None:
    """Lucide eye icon: ellipse outline + circle pupil."""
    size = img.size[0]
    draw = ImageDraw.Draw(img)
    s = size
    # Outer eye shape (lemon)
    cy = s // 2
    outer_w = int(s * 0.62)
    outer_h = int(s * 0.34)
    cx = s // 2
    # almond via polygon
    draw.polygon(
        (
            (cx - outer_w // 2, cy),
            (cx - outer_w // 4, cy - outer_h // 2),
            (cx + outer_w // 4, cy - outer_h // 2),
            (cx + outer_w // 2, cy),
            (cx + outer_w // 4, cy + outer_h // 2),
            (cx - outer_w // 4, cy + outer_h // 2),
        ),
        outline=color,
        width=max(2, int(s * 0.08)),
    )
    # Pupil
    pupil_r = int(s * 0.12)
    draw.ellipse(
        (cx - pupil_r, cy - pupil_r, cx + pupil_r, cy + pupil_r),
        fill=color,
    )


def render(out_dir: Path, draw_glyph) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    sizes = [16, 32, 48, 128]
    for size in sizes:
        # Slight padding/shadow base then glyph on top
        base = round_rect(size, BRAND_RED)
        glyph_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw_glyph(glyph_layer, WHITE)
        base.alpha_composite(glyph_layer)
        out_path = out_dir / f"icon-{size}.png"
        base.save(out_path, format="PNG", optimize=True)
        print(f"wrote {out_path}")


def main() -> None:
    d_root = ROOT / "downloader" / "icons"
    l_root = ROOT / "live-overlay" / "icons"
    render(d_root, draw_download_glyph)
    render(l_root, draw_eye_glyph)


if __name__ == "__main__":
    main()
