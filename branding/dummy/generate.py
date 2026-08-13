#!/usr/bin/env python3
"""Generate clearly-fake KIN dummy brand PNGs and a favicon (stdlib only).

These assets are a Timeline 6.2 placeholder — not a customer identity.
Regenerate with:  python3 branding/dummy/generate.py
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent

# Distinct from stock Zimbra blue/orange. Terracotta on dark slate.
ACCENT = (0xC4, 0x5C, 0x26)
ACCENT_HI = (0xE0, 0x7A, 0x3D)
BG = (0x1B, 0x24, 0x30)
FG = (0xF4, 0xED, 0xE4)
INK = (0x2A, 0x14, 0x0A)

# 5x7 caps for the few glyphs we need. 1 = foreground.
FONT: dict[str, tuple[str, ...]] = {
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
}


def png_rgb(width: int, height: int, pixels: list[list[tuple[int, int, int]]]) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    raw = bytearray()
    for row in pixels:
        raw.append(0)
        for r, g, b in row:
            raw.extend((r, g, b))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")


def canvas(width: int, height: int, color: tuple[int, int, int]) -> list[list[tuple[int, int, int]]]:
    return [[color for _ in range(width)] for _ in range(height)]


def fill_rect(
    px: list[list[tuple[int, int, int]]],
    x: int,
    y: int,
    w: int,
    h: int,
    color: tuple[int, int, int],
) -> None:
    height = len(px)
    width = len(px[0])
    for yy in range(max(0, y), min(height, y + h)):
        row = px[yy]
        for xx in range(max(0, x), min(width, x + w)):
            row[xx] = color


def blit_text(
    px: list[list[tuple[int, int, int]]],
    x: int,
    y: int,
    text: str,
    color: tuple[int, int, int],
    scale: int = 2,
    gap: int = 1,
) -> int:
    cx = x
    for ch in text.upper():
        glyph = FONT.get(ch, FONT[" "])
        for gy, bits in enumerate(glyph):
            for gx, bit in enumerate(bits):
                if bit != "1":
                    continue
                fill_rect(px, cx + gx * scale, y + gy * scale, scale, scale, color)
        cx += (5 + gap) * scale
    return cx


def draw_mark(px: list[list[tuple[int, int, int]]], x: int, y: int, size: int) -> None:
    fill_rect(px, x, y, size, size, ACCENT)
    inset = max(2, size // 8)
    fill_rect(px, x + inset, y + inset, size - 2 * inset, size - 2 * inset, ACCENT_HI)
    blit_text(px, x + inset + 2, y + size // 3, "K", INK, scale=max(1, size // 16))


def write_png(path: Path, px: list[list[tuple[int, int, int]]]) -> None:
    path.write_bytes(png_rgb(len(px[0]), len(px), px))


def login_banner() -> list[list[tuple[int, int, int]]]:
    w, h = 450, 60
    px = canvas(w, h, BG)
    fill_rect(px, 0, h - 5, w, 5, ACCENT)
    draw_mark(px, 10, 8, 44)
    blit_text(px, 66, 10, "KIN DUMMY BRAND", FG, scale=2)
    blit_text(px, 66, 34, "PLACEHOLDER - NOT A CUSTOMER", ACCENT_HI, scale=1)
    blit_text(px, 320, 34, "ZIMBRA FOSS", FG, scale=1)
    return px


def app_banner() -> list[list[tuple[int, int, int]]]:
    w, h = 200, 35
    px = canvas(w, h, BG)
    fill_rect(px, 0, h - 3, w, 3, ACCENT)
    draw_mark(px, 6, 5, 25)
    blit_text(px, 38, 11, "KIN DUMMY", FG, scale=2)
    return px


def favicon_png() -> bytes:
    size = 32
    px = canvas(size, size, ACCENT)
    fill_rect(px, 3, 3, 26, 26, ACCENT_HI)
    blit_text(px, 9, 10, "K", INK, scale=2)
    return png_rgb(size, size, px)


def png_to_ico(png: bytes) -> bytes:
    # Single-image PNG-in-ICO (Vista+). Width/height 32 fit in the 1-byte fields.
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 32, 32, 0, 0, 1, 32, len(png), 22)
    return header + entry + png


def main() -> None:
    write_png(OUT / "login-banner.png", login_banner())
    write_png(OUT / "app-banner.png", app_banner())
    png32 = favicon_png()
    (OUT / "favicon.ico").write_bytes(png_to_ico(png32))
    print(f"wrote dummy brand assets in {OUT}")


if __name__ == "__main__":
    main()
