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
        # Three-layer anisotropic FBM grain amplitudes.
        # Frequencies are cross-section-relative (see generate()); these control
        # how much each layer contributes to the ±brightness variation.
        "amp_fine":   0.12,   # pore-scale texture (~50× freq across grain)
        "amp_mid":    0.20,   # ring / growth-line variation (~10× freq) — dominant
        "amp_broad":  0.10,   # slow warmth / figure across the rail (~2.5× freq)
    },
    "oak": {
        "palette": [
            (98,  65, 32),
            (145, 105, 55),
            (175, 136, 80),
            (200, 162, 102),
            (218, 183, 128),
        ],
        "amp_fine":   0.12,
        "amp_mid":    0.20,
        "amp_broad":  0.10,
    },
    "gilded": {
        # 3-stop palette: darkest shadow → mid antique gold → brightest highlight.
        # Used by _generate_gilded() for standalone preview and by
        # frame_compositor._apply_gilded_color() for the full framing pipeline.
        # NO ring_freq / distortion — gilded_base() generates the surface map.
        "palette": [
            ( 80,  55,  12),   # value 0.0 — deep shadow warm brown
            (162, 122,  42),   # value 0.5 — mid antique gold
            (220, 185,  95),   # value 1.0 — bright yellow-gold
        ],
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


def _smooth_noise_xy(
    shape: tuple, scale_x: int, scale_y: int, seed: int
) -> np.ndarray:
    """
    Value noise with independent x and y grid scales, bilinearly interpolated.
    scale_x / scale_y are the pixel periods of the coarse grid in each axis.
    """
    h, w   = shape
    ch     = max(2, h // scale_y + 2)
    cw     = max(2, w // scale_x + 2)
    coarse = np.random.default_rng(seed).random((ch, cw)).astype(np.float32)

    yc = np.linspace(0.0, ch - 1, h)
    xc = np.linspace(0.0, cw - 1, w)
    y0 = np.floor(yc).astype(np.int32)
    y1 = np.minimum(y0 + 1, ch - 1)
    x0 = np.floor(xc).astype(np.int32)
    x1 = np.minimum(x0 + 1, cw - 1)

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


def _fbm_aniso(
    shape: tuple,
    scale_perp: int,
    scale_par: int,
    grain_direction: str,
    octaves: int = 3,
    persistence: float = 0.5,
    seed: int = 0,
) -> np.ndarray:
    """
    Anisotropic FBM: features are strongly elongated along the grain direction
    (large scale_par) and fine across it (small scale_perp).
      horizontal grain → scale_par governs X (along), scale_perp governs Y (across)
      vertical   grain → scale_par governs Y (along), scale_perp governs X (across)
    Returns a float32 array in [0, 1] with mean ≈ 0.5.
    """
    total = np.zeros(shape, dtype=np.float32)
    amp, norm = 1.0, 0.0
    for i in range(octaves):
        sp = max(2, scale_perp >> i)
        sa = max(2, scale_par  >> i)
        if grain_direction == "horizontal":
            layer = _smooth_noise_xy(shape, scale_x=sa, scale_y=sp, seed=seed + i * 997)
        else:
            layer = _smooth_noise_xy(shape, scale_x=sp, scale_y=sa, seed=seed + i * 997)
        total += amp * layer
        norm  += amp
        amp   *= persistence
    return total / norm


# ---------------------------------------------------------------------------
# Gilded texture
# ---------------------------------------------------------------------------

def gilded_base(width: int, height: int, seed: int = 42) -> np.ndarray:
    """
    Base value map for gilded rail texture: a flat 0.5 field with subtle
    diagonal streaks (≈15°, period 12 px, ±0.015) simulating brush marks
    in the gesso beneath the gold leaf.

    Returns a float32 (height, width) array.  The streak pattern is purely
    geometric (position-driven, not random); seed is accepted only for API
    consistency.  frame_compositor._apply_gilded_color() applies the molding
    profile, tapered noise, shadow flecks, and 3-stop colour ramp on top.
    """
    Y = np.arange(height, dtype=np.float32)[:, np.newaxis]
    X = np.arange(width,  dtype=np.float32)[np.newaxis, :]
    angle  = 15.0 * np.pi / 180.0
    stripe = X * np.cos(angle) + Y * np.sin(angle)
    return (np.full((height, width), 0.5, dtype=np.float32)
            + np.sin(stripe * (2.0 * np.pi / 12.0)).astype(np.float32) * 0.015)


def _generate_gilded(width: int, height: int, seed: int) -> Image.Image:
    """
    Standalone gold-leaf preview used by generate() and the __main__ path.
    Flat (no molding profile) — for texture inspection only.
    Full quality comes from gilded_base() + frame_compositor._apply_gilded_color().
    """
    rng     = np.random.default_rng(seed)
    val     = np.clip(
        gilded_base(width, height, seed)
        + rng.uniform(-12.0 / 255.0, 12.0 / 255.0, (height, width)).astype(np.float32),
        0.0, 1.0,
    )
    palette = np.array(STYLES["gilded"]["palette"], dtype=np.float32) / 255.0
    n       = len(palette)
    t       = val * (n - 1)
    lo      = np.clip(np.floor(t).astype(np.int32), 0, n - 2)
    frac    = (t - lo)[..., np.newaxis]
    rgb     = palette[lo] * (1.0 - frac) + palette[lo + 1] * frac
    return Image.fromarray((np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8), "RGB")


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
    # Cross-section dimension sets grain frequencies (the rail is fw px wide).
    fw = height if grain_direction == "horizontal" else width

    # Three anisotropic FBM layers; features are elongated ~10:1 along the grain
    # so the texture reads as continuous flowing material, not periodic stripes.
    #
    #   Layer 1 fine   ~50× freq  amplitude amp_fine   — pore-scale texture
    #   Layer 2 medium ~10× freq  amplitude amp_mid    — ring/growth-line variation
    #   Layer 3 broad  ~2.5× freq amplitude amp_broad  — slow figure across the rail
    layers = [
        (_fbm_aniso(shape, max(2, fw // 50), max(2, fw // 5),
                    grain_direction, octaves=2, seed=seed),
         cfg["amp_fine"]),
        (_fbm_aniso(shape, max(2, fw // 10), max(2, fw),
                    grain_direction, octaves=3, seed=seed + 1111),
         cfg["amp_mid"]),
        (_fbm_aniso(shape, max(2, fw //  2), max(2, fw * 4),
                    grain_direction, octaves=2, seed=seed + 2222),
         cfg["amp_broad"]),
    ]

    # Sum centred layers (mean → 0) then shift back to 0.5.
    # Total worst-case range: ±(amp_fine + amp_mid + amp_broad) = ±0.42 → ≈ ±25 palette units.
    texture = np.clip(
        sum((lyr - 0.5) * amp for lyr, amp in layers) + 0.5,
        0.0, 1.0,
    )

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
