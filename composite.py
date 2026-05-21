#!/usr/bin/env python3
"""
composite.py — Process downloaded artwork images into museum-framed 4K presentations.

Usage:
    python3 composite.py [--input DIR] [--output DIR] [--style walnut|oak]

Input layout:   {input}/{artist}/image/{stem}.jpg
                {input}/{artist}/meta/{stem}.json
Output:         {output}/{artist}/{stem}.jpg  (3840×2160 JPEG)
"""

import argparse
import hashlib
import json
import logging
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

import wood_texture

# ---------------------------------------------------------------------------
# Layout constants (pixels at 4K)
# ---------------------------------------------------------------------------

TV_W, TV_H  = 3840, 2160
FRAME_W     = 100          # frame rail width
MAT_W       = 72           # mat border around artwork
BG_COLOR    = (17, 17, 17) # near-black canvas background
MAT_COLOR   = (240, 234, 220)  # warm off-white
BORDER_MIN  = 80           # minimum black gap outside frame

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typography helpers
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


# ---------------------------------------------------------------------------
# Molding profile
# ---------------------------------------------------------------------------

def _molding_profile(n: int) -> np.ndarray:
    """
    1-D brightness offset (float) from outer edge (index 0) to inner edge (index n−1).
    Simulates a raised bevel with outer highlight and inner shadow + bead.
    """
    x = np.linspace(0.0, 1.0, n, dtype=np.float32)
    outer_hi = 0.72 * np.exp(-x * 14)                          # sharp outer highlight
    bevel    = 0.22 * (1.0 - x)                                 # broad surface gradient
    shadow   = -0.42 * np.clip((x - 0.38) / 0.52, 0, 1) ** 1.5 # shadow trough
    bead     =  0.52 * np.exp(-(1.0 - x) * 22)                  # thin inner bead
    return outer_hi + bevel + shadow + bead


def _apply_lighting(arr: np.ndarray, profile: np.ndarray, axis: int) -> np.ndarray:
    """
    Add a 1-D brightness profile along the rail's cross-section axis.
      axis=0 → profile runs down rows   (top/bottom rails)
      axis=1 → profile runs across cols (left/right rails)
    """
    f = arr.astype(np.float32) / 255.0
    light = profile[:, np.newaxis, np.newaxis] if axis == 0 \
            else profile[np.newaxis, :, np.newaxis]
    return (np.clip(f + light, 0.0, 1.0) * 255).astype(np.uint8)


# ---------------------------------------------------------------------------
# Frame rendering
# ---------------------------------------------------------------------------

def _draw_frame(canvas: Image.Image,
                fx: int, fy: int, fow: int, foh: int,
                wood_style: str, seed: int) -> None:
    """Render four mitered wood-grain rails onto *canvas* in place."""
    fw      = FRAME_W
    profile = _molding_profile(fw)

    # All eight corner points
    tl  = (fx,        fy)
    tr  = (fx + fow,  fy)
    bl  = (fx,        fy + foh)
    br  = (fx + fow,  fy + foh)
    itl = (fx + fw,   fy + fw)
    itr = (fx + fow - fw, fy + fw)
    ibl = (fx + fw,   fy + foh - fw)
    ibr = (fx + fow - fw, fy + foh - fw)

    # (polygon_vertices, grain_direction, brightness_profile)
    rails = [
        ([tl,  tr,  itr, itl], "horizontal", profile),
        ([ibl, ibr, br,  bl],  "horizontal", profile[::-1]),
        ([tl,  itl, ibl, bl],  "vertical",   profile),
        ([itr, tr,  br,  ibr], "vertical",   profile[::-1]),
    ]

    for poly, direction, prof in rails:
        xs   = [p[0] for p in poly]
        ys   = [p[1] for p in poly]
        bx0  = min(xs);  bx1 = max(xs)
        by0  = min(ys);  by1 = max(ys)
        bw, bh = bx1 - bx0, by1 - by0

        wood = wood_texture.generate(bw, bh, style=wood_style,
                                     grain_direction=direction, seed=seed)
        lit  = _apply_lighting(np.array(wood), prof,
                                axis=0 if direction == "horizontal" else 1)
        rail = Image.fromarray(lit)

        # Polygon mask for 45-degree miter cut
        local_poly = [(x - bx0, y - by0) for x, y in poly]
        mask = Image.new("L", (bw, bh), 0)
        ImageDraw.Draw(mask).polygon(local_poly, fill=255)

        canvas.paste(rail, (bx0, by0), mask=mask)


# ---------------------------------------------------------------------------
# Mat shadow
# ---------------------------------------------------------------------------

def _mat_shadow(mat_img: Image.Image, art_box: tuple,
                spread: int = 22) -> Image.Image:
    """
    Darken the mat near the artwork edges, simulating the artwork sitting
    in a shallow rebate.
    """
    ax, ay, ax2, ay2 = art_box

    shadow = Image.new("L", mat_img.size, 0)
    ImageDraw.Draw(shadow).rectangle([ax, ay, ax2, ay2], fill=115)
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=spread))

    # Keep shadow only on the mat, not over the artwork itself
    clip = Image.new("L", mat_img.size, 255)
    ImageDraw.Draw(clip).rectangle([ax, ay, ax2, ay2], fill=0)
    shadow = ImageChops.multiply(shadow, clip)

    mat_f  = np.array(mat_img).astype(np.float32)
    s      = np.array(shadow).astype(np.float32)
    factor = 1.0 - s[:, :, np.newaxis] * 0.45 / 255.0
    return Image.fromarray((mat_f * factor).clip(0, 255).astype(np.uint8))


# ---------------------------------------------------------------------------
# Info card
# ---------------------------------------------------------------------------

def _info_card(meta: dict, max_width: int = 400) -> Image.Image:
    raw_title  = meta.get("title")  or "Untitled"
    raw_artist = meta.get("artist") or ""
    raw_date   = meta.get("date")   or ""

    # artist_display can contain newlines — take only the first line
    artist = raw_artist.split("\n")[0].strip()[:55]

    title = raw_title[:48] + ("…" if len(raw_title) > 48 else "")

    pad      = 16
    f_title  = _font(26)
    f_sub    = _font(20)
    line_gap = 6

    dummy = Image.new("RGB", (1, 1))
    d     = ImageDraw.Draw(dummy)

    def tw(text, font):
        return int(d.textlength(text, font=font))

    def th(font):
        bb = font.getbbox("Ag")
        return bb[3] - bb[1]

    lines = [(title, f_title)]
    if artist:
        lines.append((artist, f_sub))
    if raw_date:
        lines.append((raw_date, f_sub))

    card_w   = min(max_width, max(tw(t, f) for t, f in lines) + pad * 2)
    heights  = [th(f) for _, f in lines]
    card_h   = pad * 2 + sum(heights) + line_gap * (len(lines) - 1)

    card  = Image.new("RGB", (card_w, card_h), (246, 240, 226))
    draw  = ImageDraw.Draw(card)
    draw.rectangle([0, 0, card_w - 1, card_h - 1],
                   outline=(168, 152, 132), width=1)

    y = pad
    for i, (text, font) in enumerate(lines):
        color = (28, 18, 10) if i == 0 else (65, 50, 35)
        draw.text((pad, y), text, font=font, fill=color)
        y += heights[i] + line_gap

    return card


# ---------------------------------------------------------------------------
# Main compositing function
# ---------------------------------------------------------------------------

def composite(
    img_path:   Path,
    meta_path:  Path,
    out_path:   Path,
    wood_style: str = "walnut",
    seed:       int = 42,
) -> bool:
    # Load inputs
    try:
        artwork = Image.open(img_path).convert("RGB")
    except Exception as e:
        log.error("Cannot open image %s: %s", img_path, e)
        return False

    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("Cannot parse metadata %s: %s", meta_path, e)

    # Resize artwork to fit within available area (preserving aspect ratio)
    max_art_w = TV_W - 2 * (BORDER_MIN + FRAME_W + MAT_W)
    max_art_h = TV_H - 2 * (BORDER_MIN + FRAME_W + MAT_W)
    artwork.thumbnail((max_art_w, max_art_h), Image.LANCZOS)
    art_w, art_h = artwork.size

    # Derived dimensions
    mat_w  = art_w  + 2 * MAT_W
    mat_h  = art_h  + 2 * MAT_W
    fow    = mat_w  + 2 * FRAME_W   # frame outer width
    foh    = mat_h  + 2 * FRAME_W   # frame outer height

    # Canvas coordinates
    fx  = (TV_W - fow) // 2
    fy  = (TV_H - foh) // 2
    mx  = fx + FRAME_W              # mat top-left
    my  = fy + FRAME_W
    ax  = mx + MAT_W                # artwork top-left
    ay  = my + MAT_W

    canvas = Image.new("RGB", (TV_W, TV_H), BG_COLOR)

    # 1. Frame rails
    _draw_frame(canvas, fx, fy, fow, foh, wood_style, seed)

    # 2. Mat
    mat_img   = Image.new("RGB", (mat_w, mat_h), MAT_COLOR)
    art_on_mat = (MAT_W, MAT_W, MAT_W + art_w, MAT_W + art_h)
    mat_img   = _mat_shadow(mat_img, art_on_mat)
    canvas.paste(mat_img, (mx, my))

    # 3. Artwork
    canvas.paste(artwork, (ax, ay))

    # 4. Info card — lower-right of mat, aligned to artwork's right edge
    card  = _info_card(meta)
    cx    = ax + art_w - card.width
    cy    = ay + art_h + (MAT_W - card.height) // 2
    cy    = max(ay + art_h + 4,
                min(cy, my + mat_h - card.height - 4))
    canvas.paste(card, (cx, cy))

    # Save
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(str(out_path), "JPEG", quality=95, subsampling=0)
    log.info("→ %s", out_path.name)
    return True


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _seed_from_name(stem: str) -> int:
    """Deterministic 31-bit seed from filename stem (stable across runs)."""
    return int(hashlib.md5(stem.encode()).hexdigest()[:8], 16) & 0x7FFFFFFF


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Composite artwork into museum-framed 4K wallpapers."
    )
    parser.add_argument("--input",  "-i", default=str(Path.home() / "arty" / "artic"),
                        help="Input directory tree (default: ~/arty/artic)")
    parser.add_argument("--output", "-o", default=str(Path.home() / "arty" / "processed"),
                        help="Output directory (default: ~/arty/processed)")
    parser.add_argument("--style",  "-s", default="walnut",
                        choices=list(wood_texture.STYLES),
                        help="Wood frame style (default: walnut)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    in_dir  = Path(args.input)
    out_dir = Path(args.output)

    images = sorted(in_dir.glob("*/image/*.jpg"))
    if not images:
        log.error("No images found under %s", in_dir)
        sys.exit(1)

    log.info("Found %d images. Wood style: %s", len(images), args.style)
    ok = err = 0

    for i, img_path in enumerate(images, 1):
        artist_dir = img_path.parent.parent
        stem       = img_path.stem
        meta_path  = artist_dir / "meta" / f"{stem}.json"
        out_path   = out_dir / artist_dir.name / f"{stem}.jpg"
        seed       = _seed_from_name(stem)

        log.info("[%d/%d] %s / %s", i, len(images), artist_dir.name, img_path.name)
        if composite(img_path, meta_path, out_path, wood_style=args.style, seed=seed):
            ok += 1
        else:
            err += 1

    log.info("Done — %d succeeded, %d failed. Output: %s", ok, err, out_dir)


if __name__ == "__main__":
    main()
