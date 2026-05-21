#!/usr/bin/env python3
"""
frame_compositor.py

Pure image-processing module: given a PIL Image and a metadata dict,
return a new 3840×2160 PIL Image with the artwork centered inside a
wood-grain frame with bevel shading, on a near-black background.

Public API
----------
    compose(artwork, meta, *, wood_style="walnut", seed=42) -> Image.Image

The `meta` dict accepts: title, artist, date  (all optional; reserved for
an info card overlay that will be added in a future layer).
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

import wood_texture

# ---------------------------------------------------------------------------
# Layout constants (pixels at 4K)
# ---------------------------------------------------------------------------

TV_W, TV_H = 3840, 2160
FRAME_W    = 100          # frame rail width
MAT_W      = 70           # mat border around artwork
BORDER_MIN = 80           # minimum black gap outside the frame

BG_COLOR  = (17, 17, 17)      # near-black canvas
MAT_COLOR = (240, 234, 220)   # warm off-white mat


# ---------------------------------------------------------------------------
# Bevel shading
# ---------------------------------------------------------------------------

def _molding_profile(n: int) -> np.ndarray:
    """
    1-D brightness offset across a frame rail (outer edge = index 0,
    inner edge = index n−1).  Produces a raised-bevel illusion:
      • sharp highlight at the outer edge
      • broad gradient across the bevel face
      • shadow trough near the inner edge
      • thin bright bead at the innermost edge
    """
    x        = np.linspace(0.0, 1.0, n, dtype=np.float32)
    outer_hi = 0.72 * np.exp(-x * 14)
    bevel    = 0.22 * (1.0 - x)
    shadow   = -0.42 * np.clip((x - 0.38) / 0.52, 0.0, 1.0) ** 1.5
    bead     =  0.52 * np.exp(-(1.0 - x) * 22)
    return outer_hi + bevel + shadow + bead


def _apply_lighting(
    arr: np.ndarray,
    profile: np.ndarray,
    axis: int,
) -> np.ndarray:
    """
    Broadcast *profile* over the cross-section axis of *arr* (H×W×3).
    axis=0  profile varies row-by-row   → top / bottom rails
    axis=1  profile varies column-by-column → left / right rails
    """
    f     = arr.astype(np.float32) / 255.0
    light = (profile[:, np.newaxis, np.newaxis] if axis == 0
             else profile[np.newaxis, :, np.newaxis])
    return (np.clip(f + light, 0.0, 1.0) * 255).astype(np.uint8)


# ---------------------------------------------------------------------------
# Frame rendering
# ---------------------------------------------------------------------------

def _draw_frame(
    canvas: Image.Image,
    fx: int, fy: int,
    fow: int, foh: int,
    wood_style: str,
    seed: int,
) -> None:
    """
    Render four mitered wood-grain rails with bevel shading onto *canvas*.

    Each rail is a trapezoid polygon, giving exact 45° miter joints at
    the corners.  The brightness profile is reversed for the bottom and
    right rails so the outer (bright) edge always faces away from the mat.
    """
    fw      = FRAME_W
    profile = _molding_profile(fw)

    # Eight corner points: outer corners + inner corners
    tl  = (fx,            fy)
    tr  = (fx + fow,      fy)
    bl  = (fx,            fy + foh)
    br  = (fx + fow,      fy + foh)
    itl = (fx + fw,       fy + fw)
    itr = (fx + fow - fw, fy + fw)
    ibl = (fx + fw,       fy + foh - fw)
    ibr = (fx + fow - fw, fy + foh - fw)

    rails = [
        #  polygon               grain          profile
        ([tl,  tr,  itr, itl], "horizontal",  profile),
        ([ibl, ibr, br,  bl],  "horizontal",  profile[::-1]),
        ([tl,  itl, ibl, bl],  "vertical",    profile),
        ([itr, tr,  br,  ibr], "vertical",    profile[::-1]),
    ]

    for poly, direction, prof in rails:
        xs  = [p[0] for p in poly]
        ys  = [p[1] for p in poly]
        bx0 = min(xs);  bx1 = max(xs)
        by0 = min(ys);  by1 = max(ys)
        bw, bh = bx1 - bx0, by1 - by0

        wood = wood_texture.generate(
            bw, bh, style=wood_style, grain_direction=direction, seed=seed
        )
        lit  = _apply_lighting(
            np.array(wood), prof,
            axis=0 if direction == "horizontal" else 1,
        )
        rail = Image.fromarray(lit)

        local_poly = [(x - bx0, y - by0) for x, y in poly]
        mask = Image.new("L", (bw, bh), 0)
        ImageDraw.Draw(mask).polygon(local_poly, fill=255)
        canvas.paste(rail, (bx0, by0), mask=mask)


# ---------------------------------------------------------------------------
# Mat shadow
# ---------------------------------------------------------------------------

def _mat_shadow(
    mat_img: Image.Image,
    art_box: tuple[int, int, int, int],
    spread: int = 22,
) -> Image.Image:
    """
    Darken the mat in a halo around the artwork boundary to simulate
    the artwork sitting in a shallow rebate.
    """
    ax, ay, ax2, ay2 = art_box

    shadow = Image.new("L", mat_img.size, 0)
    ImageDraw.Draw(shadow).rectangle([ax, ay, ax2, ay2], fill=115)
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=spread))

    # Clip shadow to mat only — never darken the artwork itself
    clip   = Image.new("L", mat_img.size, 255)
    ImageDraw.Draw(clip).rectangle([ax, ay, ax2, ay2], fill=0)
    shadow = ImageChops.multiply(shadow, clip)

    mat_f  = np.array(mat_img).astype(np.float32)
    s      = np.array(shadow).astype(np.float32)
    factor = 1.0 - s[:, :, np.newaxis] * 0.45 / 255.0
    return Image.fromarray((mat_f * factor).clip(0, 255).astype(np.uint8))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compose(
    artwork: Image.Image,
    meta: dict,
    *,
    wood_style: str = "walnut",
    seed: int = 42,
) -> Image.Image:
    """
    Return a 3840×2160 PIL Image: *artwork* centered on a near-black
    background, surrounded by a warm off-white mat (~70 px) and a
    wood-grain frame (~100 px) with bevel shading.

    Parameters
    ----------
    artwork    : source image in any mode; converted to RGB internally
    meta       : dict with optional keys title, artist, date
                 (reserved for info-card overlay; not used yet)
    wood_style : 'walnut' (default) or 'oak' — any key in wood_texture.STYLES
    seed       : RNG seed; same seed always produces the same frame texture
    """
    art = artwork.convert("RGB")

    # Resize to fit within the available artwork area, preserving aspect ratio
    max_art_w = TV_W - 2 * (BORDER_MIN + FRAME_W + MAT_W)
    max_art_h = TV_H - 2 * (BORDER_MIN + FRAME_W + MAT_W)
    art.thumbnail((max_art_w, max_art_h), Image.LANCZOS)
    art_w, art_h = art.size

    # Derived dimensions
    mat_w = art_w + 2 * MAT_W
    mat_h = art_h + 2 * MAT_W
    fow   = mat_w + 2 * FRAME_W   # frame outer width
    foh   = mat_h + 2 * FRAME_W   # frame outer height

    # Top-left origins on the 4K canvas
    fx = (TV_W - fow) // 2
    fy = (TV_H - foh) // 2
    mx = fx + FRAME_W              # mat
    my = fy + FRAME_W
    ax = mx + MAT_W                # artwork
    ay = my + MAT_W

    canvas = Image.new("RGB", (TV_W, TV_H), BG_COLOR)

    _draw_frame(canvas, fx, fy, fow, foh, wood_style, seed)

    mat_img    = Image.new("RGB", (mat_w, mat_h), MAT_COLOR)
    art_on_mat = (MAT_W, MAT_W, MAT_W + art_w, MAT_W + art_h)
    mat_img    = _mat_shadow(mat_img, art_on_mat)
    canvas.paste(mat_img, (mx, my))

    canvas.paste(art, (ax, ay))

    return canvas
