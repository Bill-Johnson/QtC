# QtC v0.13.2-beta — make_splash.py  (built 2026-05-24)
# Copyright (C) 2025-2026 Bill Johnson, KC9MTP
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
make_splash.py — Generate qtc_splash.png using Pillow.

Mirrors the design language of qtc_splash_v3_accurate.svg (the visual
mockup) and reuses the same drawing approach as the qtc_icon.ico
generator documented in BUILD_WINDOWS.md. Reads APP_VERSION from
main_window.py so the splash always shows the current release version.

Run before packing, or as part of install.sh / install.ps1:
    python3 make_splash.py

Output: qtc_splash.png in the same directory as this script.
"""
import os
import re
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "qtc_splash.png")

# Splash canvas size — sized for a desktop splash, not a full-screen overlay.
W, H = 600, 400

# Palette pulled from qtc_splash_v3_accurate.svg / qtc_icon.svg
BG        = (13, 26, 13, 255)       # #0d1a0d splash card
BORDER    = (0, 255, 136, 90)       # mint @ ~0.35 alpha
RING_FILL = (26, 42, 26, 255)       # icon background circle
RING_LINE = (58, 90, 58, 255)       # icon ring
WAVE      = (0, 255, 136, 255)
WAVE_M    = (0, 255, 136, 165)      # 0.65 alpha
WAVE_F    = (0, 255, 136, 89)       # 0.35 alpha
QTC_GREEN = (0, 255, 136, 255)
TITLE     = (68, 187, 119, 255)     # #44bb77 tagline
SUB       = (51, 102, 51, 255)      # #336633 secondary
FOOT      = (40, 80, 40, 255)


def app_version() -> str:
    """Read APP_VERSION = "X.Y.Z-beta" from main_window.py."""
    try:
        with open(os.path.join(HERE, "main_window.py"), encoding="utf-8") as f:
            for line in f:
                m = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return "?-beta"


def font(*names, size=12):
    """Try a list of font filenames; fall back to Pillow's default font."""
    for name in (*names,
                 "DejaVuSans-Bold.ttf", "DejaVuSans.ttf",
                 "arialbd.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def make_icon(size: int) -> Image.Image:
    """
    Draw the QtC logo (radio waves + "QtC") on a transparent canvas.
    Uses the same primitives as the qtc_icon.ico generator in
    BUILD_WINDOWS.md so the splash icon and the .ico stay visually
    consistent.
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    m = max(1, int(size * 0.03))
    d.ellipse([m, m, size - m - 1, size - m - 1],
              fill=RING_FILL, outline=RING_LINE, width=max(1, size // 64))

    cx, cy = int(size * 0.67), int(size * 0.50)
    for r, col, w in [
        (int(size * 0.12), WAVE,   max(2, size // 20)),
        (int(size * 0.20), WAVE_M, max(1, size // 28)),
        (int(size * 0.29), WAVE_F, max(1, size // 40)),
    ]:
        d.arc([cx - r, cy - r, cx + r, cy + r],
              start=-60, end=60, fill=col, width=w)

    # "QtC" text — to keep the C readable when its right edge overlaps
    # the inner wave arc, mask the wave behind the text by filling the
    # text bbox with the ring colour first (mirrors the SVG mask in
    # qtc_splash_v3_accurate.svg / qtc_icon.svg).
    fs = max(6, int(size * 0.26))
    f = font("DejaVuSans-Bold.ttf", "Helvetica-Bold.ttf", "arialbd.ttf", size=fs)
    tx, ty = int(size * 0.17), int(size * 0.36)
    bbox = d.textbbox((tx, ty), "QtC", font=f)
    pad = max(2, size // 60)
    d.rectangle((bbox[0] - pad, bbox[1] - pad,
                 bbox[2] + pad, bbox[3] + pad), fill=RING_FILL)
    d.text((tx, ty), "QtC", font=f, fill=QTC_GREEN)
    return img


def centre_text(d, y, text, fnt, fill):
    bbox = d.textbbox((0, 0), text, font=fnt)
    tw = bbox[2] - bbox[0]
    d.text(((W - tw) // 2, y), text, font=fnt, fill=fill)


def main():
    img = Image.new("RGBA", (W, H), BG)
    d = ImageDraw.Draw(img, "RGBA")

    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=14,
                        outline=BORDER, width=2)

    icon = make_icon(220)
    img.paste(icon, ((W - 220) // 2, 25), icon)

    centre_text(d, 270, "BBS CLIENT FOR AMATEUR RADIO",
                font("DejaVuSans-Bold.ttf", size=16), TITLE)
    centre_text(d, 298, f"v{app_version()}",
                font("DejaVuSansMono-Bold.ttf", "DejaVuSansMono.ttf",
                     "courbd.ttf", size=14), SUB)
    centre_text(d, 330, "Loading...",
                font("DejaVuSansMono.ttf", size=12), SUB)
    centre_text(d, 365, "73 de KC9MTP - Bill Johnson - Valparaiso, IN",
                font("DejaVuSansMono.ttf", size=11), FOOT)

    img.save(OUT)
    print(f"Wrote {OUT} ({W}x{H})")


if __name__ == "__main__":
    main()
