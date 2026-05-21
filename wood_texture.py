#!/usr/bin/env python3
"""
Procedural wood grain texture generator.

Grain is synthesised from fractional Brownian motion (stacked smooth-noise
octaves) warped into ring bands, then mapped through a colour palette.

Supported styles: 'walnut', 'oak', 'gilded'
grain_direction:  'horizontal' (grain fibres run left-right)
                  'vertical'   (grain fibres run top-bottom)
"""

import sys
from pathlib import Path

import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Style definitions
# ---------------------------------------------------------------------------

STYLES = {
    "walnut": {
        # palette from deep heartwood → warm sapwood highlights
        "palette": [
            (20, 10,  5),
            (48, 24, 11),
            (76, 40, 18),
            (104, 58, 28),
            (130, 78, 44),
        ],
        "ring_freq":    80,    # tight rings: ~1-2 px wide at 100 px rail
        "distortion":   1.4,   # warp amplitude reduced for subtle grain
        "fine_weight":  0.12,  # fibre-streak blend fraction
        "pore_weight":  0.04,  # high-frequency pore detail blend fraction
    },
    "oak": {
        "palette": [
            (98,  65, 32),
            (145, 105, 55),
            (175, 136, 80),
            (200, 162, 102),
            (218, 183, 128),
        ],
        "ring_freq":    48,    # slightly wider rings than walnut
        "distortion":   1.0,
        "fine_weight":  0.17,
        "pore_weight":  0.06,  # oak pores slightly more prominent
    },
    "gilded": {
        # Gold palette: shadow recess → base gold → catch-light
        # NO ring_freq / distortion — rendered by _generate_gilded, not the grain path
        "palette": [
            (120,  85, 20),   # shadow recess
            (165, 125, 38),   # shadow face
            (205, 162, 55),   # base gold (mid-tone)
            (235, 195, 95),   # illuminated face
            (255, 240, 160),  # catch-light
        ],
        "leaf_noise":  8,     # ±brightness units of random leaf variation (0–255)
        "fleck_rate":  0.04,  # fraction of pixels darkened to simulate worn gilding
        "fleck_depth": 0.14,  # maximum darkening for age flecks (fraction of 1.0)
    },
}


# ---------------------------------------------------------------------------
# Noise primitives
# ---------------------------------------------------------------------------

def _smooth_noise(shape: tuple, scale: int, seed: int) -> np.ndarray:
    """Value noise: coarse random grid bilinearly interpolated to *shape*."""
    h, w = shape
    ch = max(2, h // scale + 2)
    cw = max(2, w // scale + 2)
    coarse = np.random.default_rng(seed).random((ch, cw)).astype(np.float32)

    yc = np.linspace(0.0, ch - 1, h)
    xc = np.linspace(0.0, cw - 1, w)
    y0 = np.floor(yc).astype(np.int32)
    y1 = np.minimum(y0 + 1, ch - 1)
    x0 = np.floor(xc).astype(np.int32)
    x1 = np.minimum(x0 + 1, cw - 1)

    # smoothstep weights
    fy = (yc - y0).astype(np.float32)
    fx = (xc - x0).astype(np.float32)
    fy = fy * fy * (3.0 - 2.0 * fy)
    fx = fx * fx * (3.0 - 2.0 * fx)
    fy = fy[:, np.newaxis]
    fx = fx[np.newaxis, :]

    return (coarse[np.ix_(y0, x0)] * (1 - fy) * (1 - fx)
            + coarse[np.ix_(y1, x0)] * fy       * (1 - fx)
            + coarse[np.ix_(y0, x1)] * (1 - fy) * fx
            + coarse[np.ix_(y1, x1)] * fy       * fx)


def _fbm(shape: tuple, base_scale: int, octaves: int = 4,
         persistence: float = 0.5, lacunarity: float = 2.0,
         seed: int = 0) -> np.ndarray:
    """Fractional Brownian motion: summed smooth-noise octaves."""
    total = np.zeros(shape, dtype=np.float32)
    amp, scale, norm = 1.0, base_scale, 0.0
    for i in range(octaves):
        total += amp * _smooth_noise(shape, max(2, int(scale)), seed + i * 997)
        norm  += amp
        amp   *= persistence
        scale /= lacunarity
    return total / norm


# ---------------------------------------------------------------------------
# Gilded texture
# ---------------------------------------------------------------------------

def _generate_gilded(width: int, height: int, seed: int) -> Image.Image:
    """
    Gold leaf texture: flat mid-gold base with subtle random variation and
    scattered age flecks.  No ring pattern — bevel shading in frame_compositor
    provides all the depth and faceting.
    """
    cfg = STYLES["gilded"]
    rng = np.random.default_rng(seed)

    # Base colour: solid mid-gold (palette index 2) with ±leaf_noise variation
    base  = np.array(cfg["palette"][2], dtype=np.float32) / 255.0
    noise = rng.integers(-cfg["leaf_noise"], cfg["leaf_noise"] + 1,
                         (height, width, 3), dtype=np.int16).astype(np.float32) / 255.0
    canvas = np.broadcast_to(base, (height, width, 3)).copy() + noise

    # Scattered age flecks: slightly darkened pixels simulating worn gilding
    fleck_mask = rng.random((height, width)) < cfg["fleck_rate"]
    if fleck_mask.any():
        depths = rng.uniform(0.05, cfg["fleck_depth"], (height, width)) * fleck_mask
        canvas -= depths[:, :, np.newaxis]

    return Image.fromarray((np.clip(canvas, 0.0, 1.0) * 255.0).astype(np.uint8), "RGB")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate(
    width: int,
    height: int,
    style: str = "walnut",
    grain_direction: str = "horizontal",
    seed: int = 42,
) -> Image.Image:
    """
    Return a PIL RGB Image of wood grain texture.

    Parameters
    ----------
    width, height     : output pixel dimensions
    style             : 'walnut', 'oak', or 'gilded'
    grain_direction   : 'horizontal' or 'vertical'
    seed              : RNG seed for reproducibility
    """
    if style not in STYLES:
        raise ValueError(f"style must be one of {list(STYLES)}, got '{style}'")

    if style == "gilded":
        return _generate_gilded(width, height, seed)

    cfg   = STYLES[style]
    shape = (height, width)
    dim   = max(width, height)

    # Large-scale warp bends and distorts the ring bands
    warp = _fbm(shape, base_scale=max(8, dim // 4),
                octaves=5, persistence=0.55, seed=seed)

    # Fine grain: high-frequency streaks along the fibres
    fine_base = max(2, min(16, dim // 20))
    fine = _fbm(shape, base_scale=fine_base,
                octaves=3, persistence=0.65, seed=seed + 7777)

    # Pore layer: 3× the fine grain frequency, simulates fine wood pores
    pore = _fbm(shape, base_scale=max(2, fine_base // 3),
                octaves=2, persistence=0.50, seed=seed + 3333)

    x_lin = np.linspace(0.0, 1.0, width,  dtype=np.float32)
    y_lin = np.linspace(0.0, 1.0, height, dtype=np.float32)
    X, Y  = np.meshgrid(x_lin, y_lin)

    # Ring pattern varies perpendicular to the grain direction:
    #   horizontal grain → rings stack top-to-bottom → driven by Y
    #   vertical   grain → rings stack left-to-right → driven by X
    primary = Y if grain_direction == "horizontal" else X

    rings = np.sin(primary * cfg["ring_freq"] * np.pi
                   + warp  * cfg["distortion"] * np.pi)
    rings = (rings + 1.0) * 0.5  # [0, 1]

    pw      = cfg["pore_weight"]
    texture = (rings * (1.0 - cfg["fine_weight"] - pw)
               + fine  * cfg["fine_weight"]
               + pore  * pw)
    texture = np.clip(texture, 0.0, 1.0)

    # Palette interpolation
    palette = np.array(cfg["palette"], dtype=np.float32) / 255.0
    n   = len(palette)
    idx = texture * (n - 1)
    lo  = np.clip(np.floor(idx).astype(np.int32), 0, n - 1)
    hi  = np.minimum(lo + 1, n - 1)
    t   = (idx - lo)[..., np.newaxis]
    rgb = palette[lo] * (1.0 - t) + palette[hi] * t

    return Image.fromarray((rgb * 255.0).clip(0, 255).astype(np.uint8), "RGB")


# ---------------------------------------------------------------------------
# Sample generation for visual inspection
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    out.mkdir(parents=True, exist_ok=True)
    for style in ("walnut", "oak"):
        for direction in ("horizontal", "vertical"):
            img  = generate(400, 400, style=style, grain_direction=direction)
            path = out / f"wood_{style}_{direction}.png"
            img.save(str(path))
            print(f"Saved {path}")
    # Gilded has no grain direction — save one sample
    img  = generate(400, 400, style="gilded")
    path = out / "wood_gilded.png"
    img.save(str(path))
    print(f"Saved {path}")
