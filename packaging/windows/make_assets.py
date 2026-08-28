"""Generate the Windows branding artwork from the TrenchChat design tokens.

Writes the setup icon and the two wizard bitmaps (one per display scaling)
into packaging/windows/assets/, the same mark as the Flutter client's
Windows app icon -- the one Windows shows in the title bar and taskbar --
and as trenchchat/assets/tray.png, which the launcher shows in the tray
while the app runs with no window. Re-run after changing a colour token or
the mark:

    python3 packaging/windows/make_assets.py

Needs Pillow, which is a build-time dependency only -- nothing in the app
imports it.
"""

import colorsys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "assets"
FONTS = REPO / "flutter_ui" / "assets" / "fonts"
CLIENT_ICON = REPO / "flutter_ui" / "windows" / "runner" / "resources" / "app_icon.ico"
TRAY_ICON = REPO / "trenchchat" / "assets" / "tray.png"

SUPERSAMPLE = 8

# Inno Setup picks the bitmap closest to the size the current display scaling
# needs, so ship one per common scaling rather than letting it stretch.
SCALINGS = (1.0, 1.25, 1.5, 1.75, 2.0)
LARGE_BASE = (164, 314)
SMALL_BASE = (55, 58)
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
TRAY_SIZE = 64


def _hsl(h: float, s: float, lightness: float) -> tuple[int, int, int]:
    """A design-token colour: hue in degrees, saturation and lightness in 0..1."""
    r, g, b = colorsys.hls_to_rgb(h / 360.0, lightness, s)
    return round(r * 255), round(g * 255), round(b * 255)


INK_950 = _hsl(140, 0.15, 0.05)
INK_900 = _hsl(140, 0.12, 0.08)
INK_850 = _hsl(140, 0.11, 0.10)
INK_800 = _hsl(140, 0.10, 0.13)
INK_700 = _hsl(140, 0.08, 0.19)
INK_400 = _hsl(140, 0.06, 0.54)
GREEN_600 = _hsl(108, 0.58, 0.26)
GREEN_500 = _hsl(108, 0.60, 0.33)
GREEN_400 = _hsl(108, 0.58, 0.40)
GREEN_300 = _hsl(108, 0.55, 0.50)
GREEN_200 = _hsl(108, 0.42, 0.64)

# The 'hash' glyph from flutter_ui/lib/widgets/tc_icon.dart, on its 16-unit grid.
HASH_STROKES = (
    ((6.5, 3.0), (5.5, 13.0)),
    ((10.5, 3.0), (9.5, 13.0)),
    ((3.25, 6.25), (13.25, 6.25)),
    ((2.75, 9.75), (12.75, 9.75)),
)
GRID = 16.0
NOTCH_RATIO = 0.22


def _notch_polygon(x: float, y: float, size: float) -> list[tuple[float, float]]:
    """The design system's top-right chamfer, as an absolute polygon."""
    n = size * NOTCH_RATIO
    return [
        (x, y),
        (x + size - n, y),
        (x + size, y + n),
        (x + size, y + size),
        (x, y + size),
    ]


def _draw_mark(draw: ImageDraw.ImageDraw, x: float, y: float, size: float,
               border: bool = True) -> None:
    """The TrenchChat mark: the channel hash inside a notched panel."""
    poly = _notch_polygon(x, y, size)
    draw.polygon(poly, fill=INK_900)
    if border:
        draw.line(poly + [poly[0]], fill=GREEN_600, width=max(1, round(size * 0.045)),
                  joint="curve")

    unit = size / GRID
    pad = size * 0.115
    inner = size - 2 * pad
    stroke = max(1, round(unit * 1.15))
    for (x1, y1), (x2, y2) in HASH_STROKES:
        draw.line(
            [
                (x + pad + x1 / GRID * inner, y + pad + y1 / GRID * inner),
                (x + pad + x2 / GRID * inner, y + pad + y2 / GRID * inner),
            ],
            fill=GREEN_300,
            width=stroke,
        )


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / name), size)


def _centred(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
             width: int, y: int, fill: tuple[int, int, int]) -> None:
    left, top, right, _ = draw.textbbox((0, 0), text, font=font)
    draw.text(((width - (right - left)) / 2 - left, y - top), text, font=font, fill=fill)


def render_icon(size: int) -> Image.Image:
    """The setup and uninstall icon: the mark, edge to edge."""
    ss = size * SUPERSAMPLE
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.polygon(_notch_polygon(0, 0, ss), fill=INK_950)
    _draw_mark(draw, ss * 0.06, ss * 0.06, ss * 0.88, border=size >= 32)
    return img.resize((size, size), Image.LANCZOS)


def render_small(width: int, height: int) -> Image.Image:
    """The bitmap in the wizard header, on the header panel's own background."""
    ss_size = (width * SUPERSAMPLE, height * SUPERSAMPLE)
    img = Image.new("RGB", ss_size, INK_850)
    draw = ImageDraw.Draw(img)
    side = min(ss_size) * 0.86
    _draw_mark(draw, (ss_size[0] - side) / 2, (ss_size[1] - side) / 2, side)
    return img.resize((width, height), Image.LANCZOS)


def render_large(width: int, height: int) -> Image.Image:
    """The panel down the left of the welcome and finished pages."""
    ss_size = (width * SUPERSAMPLE, height * SUPERSAMPLE)
    img = Image.new("RGB", ss_size, INK_950)
    draw = ImageDraw.Draw(img)

    step = 3 * SUPERSAMPLE
    for y in range(0, ss_size[1], step):
        draw.rectangle([0, y, ss_size[0], y + SUPERSAMPLE - 1], fill=INK_900)

    side = width * 0.46 * SUPERSAMPLE
    mark_top = (height * 0.44 - width * 0.40) * SUPERSAMPLE
    _draw_mark(draw, (ss_size[0] - side) / 2, mark_top, side)

    img = img.resize((width, height), Image.LANCZOS)
    draw = ImageDraw.Draw(img)

    scale = width / LARGE_BASE[0]
    wordmark = _font("VT323-Regular.ttf", round(30 * scale))
    caption = _font("IBMPlexMono-Medium.ttf", max(6, round(7 * scale)))

    baseline = round(height * 0.44 + width * 0.09 + 14 * scale)
    _centred(draw, "TRENCHCHAT", wordmark, width, baseline, GREEN_200)

    rule_y = baseline + round(30 * scale)
    inset = round(width * 0.24)
    draw.line([(inset, rule_y), (width - inset, rule_y)], fill=GREEN_600,
              width=max(1, round(scale)))

    _centred(draw, "RETICULUM · LXMF", caption, width, rule_y + round(11 * scale), INK_400)
    _centred(draw, "NO SERVERS · NO ACCOUNTS", caption, width,
             height - round(18 * scale), INK_700)
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    icons = [render_icon(s) for s in ICON_SIZES]
    for target in (OUT / "trenchchat.ico", CLIENT_ICON):
        icons[-1].save(target, format="ICO", sizes=[(s, s) for s in ICON_SIZES],
                       append_images=icons[:-1])

    TRAY_ICON.parent.mkdir(parents=True, exist_ok=True)
    render_icon(TRAY_SIZE).save(TRAY_ICON, format="PNG")

    for factor in SCALINGS:
        tag = f"{round(factor * 100)}"
        large = render_large(round(LARGE_BASE[0] * factor), round(LARGE_BASE[1] * factor))
        large.save(OUT / f"wizard-large-{tag}.bmp")
        small = render_small(round(SMALL_BASE[0] * factor), round(SMALL_BASE[1] * factor))
        small.save(OUT / f"wizard-small-{tag}.bmp")

    print(f"wrote {len(list(OUT.iterdir()))} files to {OUT}")
    print(f"wrote {CLIENT_ICON}")
    print(f"wrote {TRAY_ICON}")


if __name__ == "__main__":
    main()
