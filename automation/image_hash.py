"""Tiny perceptual hashes (Pillow only) used to prove a disguised photo no
longer matches its source.

pHash (DCT low frequencies) and dHash (neighbour gradients) are the same family
TinEye-style reverse image search uses for near-duplicates. Two 64-bit hashes
within ~10 bits of each other read as "the same picture"; two unrelated photos
land around 32. `image_disguise` requires its output to clear
`MIN_DISTANCE` on both hashes against the source.
"""
from __future__ import annotations

import math

from PIL import Image

MIN_DISTANCE = 12

_N = 32
_LOW = 8


def _dct_rows(n: int, keep: int) -> list[list[float]]:
    """First `keep` rows of the orthonormal DCT-II matrix of size n."""
    rows = []
    for k in range(keep):
        scale = math.sqrt(1 / n) if k == 0 else math.sqrt(2 / n)
        rows.append([scale * math.cos(math.pi * (2 * i + 1) * k / (2 * n)) for i in range(n)])
    return rows


_D = _dct_rows(_N, _LOW)


def _bits(values: list[float], threshold: float) -> int:
    out = 0
    for v in values:
        out = (out << 1) | (1 if v > threshold else 0)
    return out


def phash(img: Image.Image) -> int:
    """64-bit DCT hash: 32x32 grey, keep the 8x8 low band, threshold at median."""
    g = img.convert("L").resize((_N, _N), Image.LANCZOS)
    px = list(g.tobytes())
    grid = [px[r * _N:(r + 1) * _N] for r in range(_N)]
    # T = D[:8] @ G, then C = T @ D[:8].T — only the low band is ever needed.
    t = [[sum(d[i] * grid[i][j] for i in range(_N)) for j in range(_N)] for d in _D]
    c = [[sum(row[j] * d[j] for j in range(_N)) for d in _D] for row in t]
    flat = [c[k][l] for k in range(_LOW) for l in range(_LOW)]
    median = sorted(flat[1:])[len(flat[1:]) // 2]  # DC term skews the median
    return _bits(flat, median)


def dhash(img: Image.Image) -> int:
    """64-bit gradient hash: 9x8 grey, is each pixel brighter than its left?"""
    g = img.convert("L").resize((9, 8), Image.LANCZOS)
    px = list(g.tobytes())
    out = 0
    for r in range(8):
        row = px[r * 9:(r + 1) * 9]
        for c in range(8):
            out = (out << 1) | (1 if row[c + 1] > row[c] else 0)
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def distance(a: Image.Image, b: Image.Image) -> tuple[int, int]:
    """(pHash distance, dHash distance) between two images."""
    return hamming(phash(a), phash(b)), hamming(dhash(a), dhash(b))
