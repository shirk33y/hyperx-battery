#!/usr/bin/env python3
"""
Convert hyperx.png to a transparent ICO for PyInstaller.
- Uses provided transparent PNG; generates multi-size ICO with high-quality resampling.
Usage:
    python hyperx_make_ico.py
This will read C:\\Users\\shirk3y\\hyperx.jpg and write C:\\Users\\shirk3y\\hyperx.ico
Adjust paths below if your image lives elsewhere.
"""
from pathlib import Path
from PIL import Image

SRC = Path(__file__).resolve().parent.parent / "assets" / "hyperx.png"
DST = Path(__file__).resolve().parent.parent / "assets" / "hyperx.ico"
SIZES = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)]


def main():
    img = Image.open(SRC).convert("RGBA")
    # Generate multiple sizes with high-quality downsampling
    imgs = [img.resize(sz, Image.LANCZOS) for sz in SIZES]
    imgs[0].save(DST, format="ICO", sizes=SIZES)
    print(f"Wrote {DST}")


if __name__ == "__main__":
    main()
