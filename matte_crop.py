"""
matte_crop.py

Detect and remove a matte (or product-photo background) that is baked into a
source image, so the compositor frames the painting itself rather than a
picture of a painting inside a border.

PIL Image in, (PIL Image, bool) out.  No file I/O.
"""

import numpy as np
from PIL import Image

_MAX_SIDE   = 600    # analysis is done on a downsampled copy
_RING_FRAC  = 0.02   # outer ring (fraction of each side) used to sample the border colour
_RING_STD   = 26.0   # max per-channel std of the ring for it to count as a uniform border
_MIN_LIGHT  = 185    # border must be at least this bright (avoids cropping dark paintings)
_MAX_CHROMA = 45     # border must be near-neutral (max-min channel spread)
_DIFF_TOL   = 55     # pixel differs from the border colour by more than this = content
_LINE_FRAC  = 0.004  # a row/column is content if more than this fraction of it differs
_MAX_PASSES = 3      # nested borders (background, then magnet/mat body, ...)
_MIN_AREA   = 0.25   # never crop to less than this fraction of the original area
_MIN_SHRINK = 0.01   # a pass must remove at least this fraction per side to count
_MIN_SIDES  = 3      # first pass: a real matte surrounds the picture on this many sides
_INSET      = 2      # extra source-pixels trimmed to drop anti-aliased edges


def _side_inset(strip: np.ndarray, line_axis: int, full: np.ndarray, from_end: bool) -> int:
    """
    Pixels to trim from one side.  *strip* is the outer ring on that side; when
    it is a light, near-uniform, neutral colour, scan inward until a row/column
    (of *full*) differs from that colour.  Returns 0 when the side has no matte.
    """
    px = strip.reshape(-1, 3).astype(np.float32)
    if px.std(axis=0).max() > _RING_STD:
        return 0
    bg = np.median(px, axis=0)
    if bg.min() < _MIN_LIGHT or (bg.max() - bg.min()) > _MAX_CHROMA:
        return 0
    diff = np.abs(full.astype(np.float32) - bg).max(axis=2) > _DIFF_TOL
    frac = diff.mean(axis=1 - line_axis)          # per-row (axis 0) or per-column (axis 1)
    hits = np.where(frac > _LINE_FRAC)[0]
    if hits.size == 0:
        return 0
    return int(frac.size - 1 - hits[-1]) if from_end else int(hits[0])


def _crop_box(small: np.ndarray, min_sides: int) -> tuple[int, int, int, int] | None:
    """Content bounding box (l, t, r, b) in *small* coords, or None if no border."""
    h, w, _ = small.shape
    ring = max(2, int(min(h, w) * _RING_FRAC))
    t = _side_inset(small[:ring],        0, small, False)
    b = _side_inset(small[-ring:],       0, small, True)
    l = _side_inset(small[:, :ring],     1, small, False)
    r = _side_inset(small[:, -ring:],    1, small, True)
    if sum(1 for v in (l, t, r, b) if v) < min_sides:
        return None
    return l, t, w - r, h - b


def crop_matte(img: Image.Image) -> tuple[Image.Image, bool]:
    """
    Return (image, cropped).  Repeatedly strips light, near-uniform borders
    until none remain.  Returns the original image untouched when nothing is
    found or the crop would be implausibly small.
    """
    rgb = img.convert("RGB")
    ow, oh = rgb.size
    box = (0, 0, ow, oh)          # current crop in source pixels

    for n in range(_MAX_PASSES):
        cur = rgb.crop(box)
        cw, ch = cur.size
        scale = min(1.0, _MAX_SIDE / max(cw, ch))
        small = np.asarray(cur.resize((max(1, round(cw * scale)), max(1, round(ch * scale))),
                                      Image.BILINEAR))
        found = _crop_box(small, _MIN_SIDES if n == 0 else 1)
        if found is None:
            break
        l, t, r, b = (round(v / scale) for v in found)
        l, t = l + _INSET, t + _INSET
        r, b = min(cw, r) - _INSET, min(ch, b) - _INSET
        if r - l < 1 or b - t < 1:
            break
        if max(l, t, cw - r, ch - b) < _MIN_SHRINK * min(cw, ch):
            break
        box = (box[0] + l, box[1] + t, box[0] + r, box[1] + b)

    if box == (0, 0, ow, oh):
        return img, False
    if (box[2] - box[0]) * (box[3] - box[1]) < _MIN_AREA * ow * oh:
        return img, False
    return rgb.crop(box), True
