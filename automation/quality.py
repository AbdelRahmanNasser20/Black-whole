from pathlib import Path
from PIL import Image, ImageChops


def _br_crop(img: Image.Image, frac: float = 0.35) -> Image.Image:
    w, h = img.size
    return img.crop((int(w * (1 - frac)), int(h * (1 - frac)), w, h))


def watermark_likely_present(
    original: Path, cleaned: Path, min_delta: float = 8.0
) -> bool:
    """Heuristic: compare bottom-right quadrant of original vs cleaned.

    If the mean absolute difference is below min_delta, the cleaning barely
    changed the watermark region and dewatermark likely failed.
    """
    try:
        o = Image.open(original).convert("RGB")
        c = Image.open(cleaned).convert("RGB").resize(o.size)
    except Exception:
        return True
    o_br = _br_crop(o)
    c_br = _br_crop(c)
    diff = ImageChops.difference(o_br, c_br)
    stat = diff.getbbox()
    if stat is None:
        return True
    hist = diff.histogram()
    total = sum(i * v for i, v in enumerate(hist[:256]))
    count = sum(hist[:256]) or 1
    mean_delta = total / count
    return mean_delta < min_delta
