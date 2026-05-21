#!/usr/bin/env python3
"""
frame_compositor.py

Pure image-processing module: given a PIL Image and a metadata dict,
return a new 3840×2160 PIL Image with the artwork centered inside a
wood-grain frame with bevel shading, on a near-black background.

Public API
----------
    compose(artwork, meta, *, frame_style="walnut", mat_config="single_neutral",
            mat_accent_color=None, seed=42) -> Image.Image

meta keys used: title, artist, date  (all optional)
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

import wood_texture
import styles

# ---------------------------------------------------------------------------
# Layout constants (pixels at 4K)
# ---------------------------------------------------------------------------

TV_W, TV_H = 3840, 2160
FRAME_W    = 160          # frame rail width
MAT_W      = 70           # mat border around artwork
BORDER_MIN = 80           # minimum black gap outside the frame
LINER_W    = 18           # inner liner width for double-mat configs

BG_COLOR  = (17, 17, 17)      # near-black canvas
MAT_COLOR = (240, 234, 220)   # warm off-white mat

# 3-stop gold colour ramp shared by _apply_gilded_color() and wood_texture.STYLES
_GILDED_PALETTE = np.array([
    [ 80,  55,  12],   # value 0.0 — deep shadow warm brown
    [162, 122,  42],   # value 0.5 — mid antique gold
    [220, 185,  95],   # value 1.0 — bright yellow-gold
], dtype=np.float32) / 255.0


# ---------------------------------------------------------------------------
# Bevel shading
# ---------------------------------------------------------------------------

def _molding_profile(n: int) -> np.ndarray:
    """
    1-D brightness offset across a frame rail (outer edge = index 0,
    inner edge = index n−1).  Piecewise-linear approximation of carved
    impressionist-era frame molding with distinct faceted planes:

      Zone 1  0–12 %   outer flat   — modest positive offset, nearly flat
      Zone 2 12–18 %   outer step   — sharp drop (vertical face in shadow)
      Zone 3 18–53 %   broad cove   — brightness rises across the main face
      Zone 4 53–68 %   sight edge   — near-peak, flat (most directly lit)
      Zone 5 68–76 %   inner step   — sharp drop (vertical face in shadow)
      Zone 6 76–97 %   inner rabbet — darkest zone
              97–100%  inner bead   — subtle catch-light at the inner edge

    Transitions at Zone 2 and Zone 5 are ~1.5 px wide to preserve the
    faceted look.  The outer-edge catch-light is added per-rail in
    _draw_frame() at strengths appropriate for the light-source direction.
    """
    x   = np.linspace(0.0, 1.0, n, dtype=np.float32)
    eps = 1.5 / n  # ~1.5-pixel transition for sharp zone boundaries

    xp = [
        0.00,        0.12,        # Zone 1  (outer flat)
        0.12 + eps,  0.18,        # Zone 2  (outer step — sharp drop)
        0.53,                     # Zone 3→4 (cove peak / sight edge start)
        0.68,                     # Zone 4→5 (sight edge end)
        0.68 + eps,  0.76,        # Zone 5  (inner step — sharp drop)
        0.97,        1.00,        # Zone 6 + inner bead
    ]
    fp = [
        0.08,   0.06,             # Zone 1
       -0.10,  -0.10,             # Zone 2
        0.28,                     # Zone 3→4
        0.28,                     # Zone 4→5
       -0.24,  -0.26,             # Zone 5
       -0.28,   0.10,             # Zone 6 + bead
    ]

    return np.interp(x, xp, fp).astype(np.float32)


def _gilded_molding_profile(n: int) -> np.ndarray:
    """
    1-D brightness offset for gilded frame rails (outer edge = index 0).
    Flat facets meeting at sharp angles — no smooth curves:
      • outer catch-light: narrow high-brightness zone
      • upper bevel face:  flat, moderately lit
      • shadow cove:       recessed flat zone
      • inner bead:        thin wire of bright gold at the inner edge
    """
    x = np.linspace(0.0, 1.0, n, dtype=np.float32)
    k = 50.0   # transition sharpness — much steeper than the wood profile

    def up(edge: float) -> np.ndarray:
        return np.clip((x - edge) * k + 0.5, 0.0, 1.0)

    def dn(edge: float) -> np.ndarray:
        return 1.0 - up(edge)

    catch_light =  0.52 * dn(0.06)                  # narrow outer highlight
    upper_face  =  0.18 * (up(0.12) - up(0.40))     # flat lit bevel plane
    shadow_cove = -0.32 * (up(0.44) - up(0.74))     # recessed dark zone
    inner_bead  =  0.44 * up(0.92)                  # thin bright inner wire

    return catch_light + upper_face + shadow_cove + inner_bead


def _apply_lighting(
    arr: np.ndarray,
    profile: np.ndarray,
    axis: int,
    ambient: float | np.ndarray = 0.0,
) -> np.ndarray:
    """
    Broadcast *profile* over the cross-section axis of *arr* (H×W×3).
    axis=0  profile varies row-by-row   → top / bottom rails
    axis=1  profile varies column-by-column → left / right rails
    *ambient* may be a scalar or (H,W) float array for smooth corner blending.
    """
    f     = arr.astype(np.float32) / 255.0
    light = (profile[:, np.newaxis, np.newaxis] if axis == 0
             else profile[np.newaxis, :, np.newaxis])
    a = ambient[:, :, np.newaxis] if isinstance(ambient, np.ndarray) else float(ambient)
    return (np.clip(f + light + a, 0.0, 1.0) * 255).astype(np.uint8)


def _apply_gilded_color(
    base: np.ndarray,
    profile: np.ndarray,
    axis: int,
    ambient: np.ndarray,
    rng_seed: int = 42,
) -> np.ndarray:
    """
    Gilded rail renderer: applies profile + ambient to *base* (the float32
    value map from wood_texture.gilded_base), then maps through the 3-stop
    gold colour ramp (_GILDED_PALETTE).

    Effects applied in value space before colour mapping:
      • tapered noise   ±12/255 at midtones, tapering to ±4/255 at extremes
      • shadow flecks   ~4 % of pixels with val < 0.25 receive a ≈ +0.14
                        lift, simulating gold-leaf-edge catch-lights in the
                        shadow recesses
    """
    rng = np.random.default_rng(rng_seed)

    light = profile[:, np.newaxis] if axis == 0 else profile[np.newaxis, :]
    val   = np.clip(base + light + ambient, 0.0, 1.0)

    # Tapered noise: ±12 at midtone, ±4 at extremes (units out of 255)
    amp   = (4.0 + 8.0 * (1.0 - np.abs(val * 2.0 - 1.0))) / 255.0
    val   = np.clip(val + rng.uniform(-1.0, 1.0, val.shape).astype(np.float32) * amp,
                    0.0, 1.0)

    # Shadow flecks: ~4 % of deep-shadow pixels brightened to simulate
    # gold-leaf edges catching light inside the recessed zones
    shadow = val < 0.25
    if shadow.any():
        fleck = shadow & (rng.random(val.shape) < 0.04)
        val[fleck] = np.minimum(val[fleck] + 0.14, 0.32)

    # 3-stop colour ramp: re-scale val to [0, 2] to index two palette segments
    t    = val * 2.0
    lo   = np.clip(np.floor(t).astype(np.int32), 0, 1)
    hi   = np.minimum(lo + 1, 2)
    frac = (t - lo)[..., np.newaxis]
    rgb  = _GILDED_PALETTE[lo] * (1.0 - frac) + _GILDED_PALETTE[hi] * frac

    return (np.clip(rgb, 0.0, 1.0) * 255).astype(np.uint8)


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
    fw = FRAME_W

    tl  = (fx,            fy)
    tr  = (fx + fow,      fy)
    bl  = (fx,            fy + foh)
    br  = (fx + fow,      fy + foh)
    itl = (fx + fw,       fy + fw)
    itr = (fx + fow - fw, fy + fw)
    ibl = (fx + fw,       fy + foh - fw)
    ibr = (fx + fow - fw, fy + foh - fw)

    p = _gilded_molding_profile(fw) if wood_style == "gilded" else _molding_profile(fw)

    if wood_style != "gilded":
        # Outer-edge catch-light scaled by rail orientation under overhead lighting.
        # Gilded uses its own catch_light component so we leave that path untouched.
        x_hi     = np.linspace(0.0, 1.0, fw, dtype=np.float32)
        outer_hi = 0.72 * np.exp(-x_hi * 14)
        p_top    = p        + outer_hi               # full catch-light — faces light source
        p_left   = p        + 0.30 * outer_hi        # 30% — oblique angle
        p_right  = p[::-1]  + 0.10 * outer_hi[::-1] # 10% — turned away
        p_bottom = p[::-1]  - 0.12 * outer_hi[::-1] # slight deepen — in shadow
    else:
        p_top    = p
        p_left   = p
        p_right  = p[::-1]
        p_bottom = p[::-1]

    rails = [
        ([tl,  tr,  itr, itl], "horizontal", p_top),
        ([ibl, ibr, br,  bl],  "horizontal", p_bottom),
        ([tl,  itl, ibl, bl],  "vertical",   p_left),
        ([itr, tr,  br,  ibr], "vertical",   p_right),
    ]

    # 2D ambient gradient — upper-left overhead light source.
    # Vertical:   +0.08 at top  → −0.08 at bottom.
    # Horizontal: +0.05 at left → −0.05 at right.
    # Summed as separable components so miter corners blend without a hard cut.
    y_amb  = np.linspace( 0.08, -0.08, foh, dtype=np.float32)
    x_amb  = np.linspace( 0.05, -0.05, fow, dtype=np.float32)
    amb_2d = y_amb[:, np.newaxis] + x_amb[np.newaxis, :]   # (foh, fow)

    for rail_idx, (poly, direction, prof) in enumerate(rails):
        xs  = [pt[0] for pt in poly]
        ys  = [pt[1] for pt in poly]
        bx0 = min(xs);  bx1 = max(xs)
        by0 = min(ys);  by1 = max(ys)
        bw, bh = bx1 - bx0, by1 - by0

        rail_amb = amb_2d[by0 - fy : by0 - fy + bh, bx0 - fx : bx0 - fx + bw]
        ax       = 0 if direction == "horizontal" else 1

        if wood_style == "gilded":
            base = wood_texture.gilded_base(bw, bh, seed=seed)
            lit  = _apply_gilded_color(
                base, prof, axis=ax, ambient=rail_amb,
                rng_seed=seed + rail_idx * 997,
            )
        else:
            wood = wood_texture.generate(
                bw, bh, style=wood_style, grain_direction=direction, seed=seed
            )
            lit  = _apply_lighting(np.array(wood), prof, axis=ax, ambient=rail_amb)

        rail = Image.fromarray(lit)
        local_poly = [(pt[0] - bx0, pt[1] - by0) for pt in poly]
        mask = Image.new("L", (bw, bh), 0)
        ImageDraw.Draw(mask).polygon(local_poly, fill=255)
        canvas.paste(rail, (bx0, by0), mask=mask)


# ---------------------------------------------------------------------------
# Rabbet shadow (no-mat mode)
# ---------------------------------------------------------------------------

def _rabbet_shadow(art: Image.Image, depth: int = 12) -> Image.Image:
    """
    Darken the outermost *depth* pixels of *art* on all four sides to
    simulate the artwork sitting in a shallow frame rebate.
    Factor: 0.55× at the very edge, linearly rising to 1.0× at *depth* px in.
    """
    arr  = np.array(art, dtype=np.float32) / 255.0
    h, w = arr.shape[:2]
    dy   = np.minimum(
        np.arange(h, dtype=np.float32),
        np.arange(h - 1, -1, -1, dtype=np.float32),
    )
    dx   = np.minimum(
        np.arange(w, dtype=np.float32),
        np.arange(w - 1, -1, -1, dtype=np.float32),
    )
    dist   = np.minimum(dy[:, np.newaxis], dx[np.newaxis, :])
    factor = np.clip(0.55 + 0.45 * dist / depth, 0.55, 1.0)[:, :, np.newaxis]
    return Image.fromarray((arr * factor * 255).clip(0, 255).astype(np.uint8))


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
# Info card
# ---------------------------------------------------------------------------

def _font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Georgia.ttf",
        "/Library/Fonts/Georgia.ttf",
        "/System/Library/Fonts/Times.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _wrap_text(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_px: int,
    draw: ImageDraw.ImageDraw,
) -> list[str]:
    """Greedy word-wrap *text* to fit within *max_px* pixels wide."""
    words   = text.split()
    lines:  list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if int(draw.textlength(candidate, font=font)) <= max_px:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def _info_card(meta: dict, max_width: int) -> Image.Image:
    """
    Render a brass museum plaque showing title, artist, and date.

    The plaque expands to fit content up to *max_width* pixels wide.
    If font sizes would still overflow, they are reduced in 2-point steps
    until everything fits.  Title text wraps rather than truncating.
    """
    raw_title  = meta.get("title")  or "Untitled"
    raw_artist = meta.get("artist") or ""
    raw_date   = meta.get("date")   or ""

    artist = raw_artist.split("\n")[0].strip()

    pad_x, pad_y = 16, 10
    gap          = 8
    title_size   = 28
    sub_size     = 20

    # Shrink fonts until every line fits within the available content width.
    content_w = max_width - pad_x * 2
    while title_size >= 14:
        f_title = _font(title_size)
        f_sub   = _font(sub_size)
        probe   = ImageDraw.Draw(Image.new("RGB", (1, 1)))

        title_lines = _wrap_text(raw_title, f_title, content_w, probe)
        all_lines   = [(t, f_title) for t in title_lines]
        if artist:
            all_lines.append((artist, f_sub))
        if raw_date:
            all_lines.append((raw_date, f_sub))

        if max(int(probe.textlength(t, font=f)) for t, f in all_lines) <= content_w:
            break
        title_size -= 2
        sub_size    = max(12, sub_size - 2)

    # Final measurement
    probe       = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    title_lines = _wrap_text(raw_title, f_title, content_w, probe)
    all_lines   = [(t, f_title) for t in title_lines]
    if artist:
        all_lines.append((artist, f_sub))
    if raw_date:
        all_lines.append((raw_date, f_sub))

    widths  = [int(probe.textlength(t, font=f)) for t, f in all_lines]
    heights = [f.getbbox("Ag")[3] - f.getbbox("Ag")[1] for _, f in all_lines]

    card_w = max(widths) + pad_x * 2
    card_h = pad_y * 2 + sum(heights) + gap * (len(all_lines) - 1)

    # Antique brass background with subtle brushed-metal noise
    card  = Image.new("RGB", (card_w, card_h), (180, 145, 60))
    noise = np.random.default_rng(42).integers(-6, 7, (card_h, card_w, 3), dtype=np.int16)
    card  = Image.fromarray(
        np.clip(np.array(card, dtype=np.int16) + noise, 0, 255).astype(np.uint8)
    )

    draw = ImageDraw.Draw(card)
    draw.rectangle([0, 0, card_w - 1, card_h - 1], outline=(140, 108, 30), width=1)

    y = pad_y
    for text, font in all_lines:
        draw.text((pad_x, y), text, font=font, fill=(30, 18, 5))
        y += font.getbbox("Ag")[3] - font.getbbox("Ag")[1] + gap

    return card


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compose(
    artwork: Image.Image,
    meta: dict,
    *,
    frame_style: str = "walnut",
    mat_config: str = "single_neutral",
    mat_accent_color: tuple[int, int, int] | None = None,
    seed: int = 42,
    mat: bool = True,
) -> Image.Image:
    """
    Return a 3840×2160 PIL Image: *artwork* centered on a near-black
    background inside a wood-grain frame with bevel shading.

    Parameters
    ----------
    artwork          : source image in any mode; converted to RGB internally
    meta             : dict with optional keys title, artist, date
    frame_style      : key from styles.FRAME_STYLES (default 'walnut');
                       falls back to 'walnut' for styles not yet in wood_texture
    mat_config       : key from styles.MAT_CONFIGS (default 'single_neutral')
    mat_accent_color : pre-processed RGB tuple used as the inner liner on
                       double-accent mats; ignored for single-mat configs
    seed             : RNG seed; same seed always produces the same frame texture
    mat              : when False the mat is omitted and the artwork sits directly
                       against the frame with a rabbet shadow at the edges
    """
    art = artwork.convert("RGB")

    if mat:
        max_art_w = TV_W - 2 * (BORDER_MIN + FRAME_W + MAT_W)
        max_art_h = TV_H - 2 * (BORDER_MIN + FRAME_W + MAT_W)
    else:
        max_art_w = TV_W - 2 * (BORDER_MIN + FRAME_W)
        max_art_h = TV_H - 2 * (BORDER_MIN + FRAME_W)
    art.thumbnail((max_art_w, max_art_h), Image.LANCZOS)
    art_w, art_h = art.size

    if mat:
        mat_w = art_w + 2 * MAT_W
        mat_h = art_h + 2 * MAT_W
        fow   = mat_w + 2 * FRAME_W
        foh   = mat_h + 2 * FRAME_W
        fx    = (TV_W - fow) // 2
        fy    = (TV_H - foh) // 2
        mx    = fx + FRAME_W
        my    = fy + FRAME_W
        ax    = mx + MAT_W
        ay    = my + MAT_W
    else:
        fow = art_w + 2 * FRAME_W
        foh = art_h + 2 * FRAME_W
        fx  = (TV_W - fow) // 2
        fy  = (TV_H - foh) // 2
        ax  = fx + FRAME_W
        ay  = fy + FRAME_W

    # Resolve wood style — fall back to walnut for styles not yet rendered
    _wood = styles.FRAME_STYLES.get(frame_style, styles.FRAME_STYLES["walnut"])["wood_style"]
    if _wood not in wood_texture.STYLES:
        _wood = "walnut"

    canvas = Image.new("RGB", (TV_W, TV_H), BG_COLOR)
    _draw_frame(canvas, fx, fy, fow, foh, _wood, seed)

    if mat:
        _mat_cfg = styles.MAT_CONFIGS.get(mat_config, styles.MAT_CONFIGS["single_neutral"])
        if _mat_cfg["layers"] == 2:
            _liner = mat_accent_color if _mat_cfg["uses_accent"] else _mat_cfg.get("secondary_color")
        else:
            _liner = None

        mat_img = Image.new("RGB", (mat_w, mat_h), MAT_COLOR)
        if _liner is not None:
            ImageDraw.Draw(mat_img).rectangle(
                [MAT_W - LINER_W, MAT_W - LINER_W,
                 MAT_W + art_w + LINER_W - 1, MAT_W + art_h + LINER_W - 1],
                fill=_liner,
            )
        art_on_mat = (MAT_W, MAT_W, MAT_W + art_w, MAT_W + art_h)
        mat_img    = _mat_shadow(mat_img, art_on_mat)
        canvas.paste(mat_img, (mx, my))
    else:
        art = _rabbet_shadow(art)

    canvas.paste(art, (ax, ay))

    # Brass plaque — centered on the bottom rail, 10 px below the inner edge
    card   = _info_card(meta, max_width=fow - 40)
    card_x = fx + (fow - card.width) // 2
    card_y = fy + foh - FRAME_W + 10
    canvas.paste(card, (card_x, card_y))

    return canvas
