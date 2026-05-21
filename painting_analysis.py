#!/usr/bin/env python3
"""
painting_analysis.py

Derive perceptual colour properties from a PIL Image.

Public API
----------
    analyse(image) -> dict with keys:
        palette_temperature  float  -1.0 (cool) … +1.0 (warm)
        accent_colors        list   up to 3 RGB tuples
        brightness           float  0.0 (dark) … 1.0 (bright)
        contrast             float  0.0 … 1.0

All computations happen on a downsampled copy (≤ 40 000 px) for speed.
Results are deterministic for a given image.
"""

from __future__ import annotations

import numpy as np
from PIL import Image
from sklearn.cluster import KMeans

# Maximum pixels used for analysis (downsampled if exceeded)
_MAX_PIXELS = 40_000
# k-means clusters; more clusters → finer palette resolution
_K = 12


# ---------------------------------------------------------------------------
# Colour-space helpers
# ---------------------------------------------------------------------------

def _srgb_linearise(x: np.ndarray) -> np.ndarray:
    """Apply sRGB inverse gamma to an array in [0, 1]."""
    return np.where(x > 0.04045, ((x + 0.055) / 1.055) ** 2.4, x / 12.92)


def _rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """
    Convert an (..., 3) float32 array in [0, 1] from sRGB to CIE L*a*b*.
    Uses the D65 illuminant / 2° observer.
    """
    linear = _srgb_linearise(np.clip(rgb, 0.0, 1.0))

    # Linear sRGB → CIE XYZ
    M = np.array([
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ], dtype=np.float32)
    xyz = linear @ M.T

    # Normalise by D65 white point
    xyz /= np.array([0.95047, 1.00000, 1.08883], dtype=np.float32)

    # CIE f() function
    delta = 6.0 / 29.0
    f = np.where(xyz > delta ** 3,
                 xyz ** (1.0 / 3.0),
                 xyz / (3.0 * delta ** 2) + 4.0 / 29.0)

    L = 116.0 * f[..., 1] - 16.0
    a = 500.0 * (f[..., 0] - f[..., 1])
    b = 200.0 * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], axis=-1)


def _lab_to_rgb(lab: np.ndarray) -> np.ndarray:
    """
    Convert an (..., 3) array from CIE L*a*b* back to sRGB [0, 1].
    """
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    fy = (L + 16.0) / 116.0
    fx = a / 500.0 + fy
    fz = fy - b / 200.0
    f  = np.stack([fx, fy, fz], axis=-1)

    delta = 6.0 / 29.0
    xyz = np.where(f > delta,
                   f ** 3.0,
                   (f - 4.0 / 29.0) * 3.0 * delta ** 2)

    xyz *= np.array([0.95047, 1.00000, 1.08883], dtype=np.float32)

    # CIE XYZ → linear sRGB
    M_inv = np.array([
        [ 3.2404542, -1.5371385, -0.4985314],
        [-0.9692660,  1.8760108,  0.0415560],
        [ 0.0556434, -0.2040259,  1.0572252],
    ], dtype=np.float32)
    linear = xyz @ M_inv.T

    # Apply sRGB gamma
    rgb = np.where(linear > 0.0031308,
                   1.055 * np.clip(linear, 0, None) ** (1.0 / 2.4) - 0.055,
                   12.92 * linear)
    return np.clip(rgb, 0.0, 1.0)


def _rgb_to_hsv(rgb: np.ndarray) -> np.ndarray:
    """
    Convert an (N, 3) float32 array in [0, 1] from RGB to HSV.
    H is in [0, 1) representing 0–360°.
    """
    r, g, b = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    maxc = rgb.max(axis=1)
    minc = rgb.min(axis=1)
    v    = maxc
    diff = maxc - minc
    s    = np.where(maxc > 0, diff / maxc, 0.0)

    safe_diff = np.where(diff > 0, diff, 1e-9)
    rc = (maxc - r) / safe_diff
    gc = (maxc - g) / safe_diff
    bc = (maxc - b) / safe_diff

    h = np.where(r == maxc, bc - gc,
        np.where(g == maxc, 2.0 + rc - bc,
                             4.0 + gc - rc))
    h = (h / 6.0) % 1.0
    h = np.where(s > 0, h, 0.0)

    return np.stack([h, s, v], axis=-1)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyse(image: Image.Image) -> dict:
    """
    Return perceptual colour properties of *image*.

    Parameters
    ----------
    image : PIL Image in any mode

    Returns
    -------
    dict with keys:
        palette_temperature  float  -1.0 … +1.0
        accent_colors        list[tuple[int,int,int]]  up to 3 entries
        brightness           float  0.0 … 1.0
        contrast             float  0.0 … 1.0
    """
    img = image.convert("RGB")

    # Downsample to ≤ _MAX_PIXELS, preserving aspect ratio
    w, h = img.size
    n_px = w * h
    if n_px > _MAX_PIXELS:
        scale = (_MAX_PIXELS / n_px) ** 0.5
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                         Image.LANCZOS)

    pixels = np.array(img, dtype=np.float32) / 255.0   # (H, W, 3)
    flat   = pixels.reshape(-1, 3)                      # (N, 3)

    # --- LAB conversion (used for brightness, contrast, clustering) ---------
    lab = _rgb_to_lab(flat)   # (N, 3)

    # brightness: perceptual mean luminance, normalised to [0, 1]
    brightness = float(np.clip(np.mean(lab[:, 0]) / 100.0, 0.0, 1.0))

    # contrast: std of luminance; theoretical max std for L* ∈ [0,100] is 50
    contrast = float(np.clip(np.std(lab[:, 0]) / 50.0, 0.0, 1.0))

    # edge_brightness: mean luminance of the outer 10 % border — used by
    # style_selector to decide whether a mat would add unwanted lightness
    ph, pw  = pixels.shape[:2]
    e_h, e_w = max(1, ph // 10), max(1, pw // 10)
    edge_m  = np.zeros((ph, pw), dtype=bool)
    edge_m[:e_h, :]  = True
    edge_m[-e_h:, :] = True
    edge_m[:, :e_w]  = True
    edge_m[:, -e_w:] = True
    edge_brightness = float(np.clip(np.mean(lab[edge_m.ravel(), 0]) / 100.0, 0.0, 1.0))

    # --- palette_temperature -------------------------------------------------
    # cos(hue_radians) maps red→+1, cyan→−1, yellow→+0.5, blue→−0.5.
    # Weight by saturation so achromatic pixels contribute nothing.
    hsv         = _rgb_to_hsv(flat)
    hue_rad     = hsv[:, 0] * 2.0 * np.pi
    sat         = hsv[:, 1]
    total_sat   = sat.sum()
    if total_sat > 1e-6:
        temp = float(np.sum(np.cos(hue_rad) * sat) / total_sat)
    else:
        temp = 0.0
    palette_temperature = float(np.clip(temp, -1.0, 1.0))

    # --- accent_colors -------------------------------------------------------
    # K-means in LAB space; keep clusters that are mid-tone, saturated,
    # and not dominant (≥ 1 % but < 25 % of pixels).
    km = KMeans(n_clusters=_K, n_init=10, random_state=42)
    km.fit(lab)

    labels  = km.labels_
    centers = km.cluster_centers_          # (K, 3) in L*a*b*
    sizes   = np.bincount(labels, minlength=_K) / len(labels)

    L_c = centers[:, 0]
    C_c = np.sqrt(centers[:, 1] ** 2 + centers[:, 2] ** 2)

    accent_mask = (
        (L_c > 15.0) & (L_c < 88.0) &   # not near-black or near-white
        (C_c > 20.0) &                    # meaningfully saturated
        (sizes > 0.01) &                  # present (> 1 % of pixels)
        (sizes < 0.25)                    # not dominant (< 25 %)
    )
    candidate_idx = np.where(accent_mask)[0]

    accent_colors: list[tuple[int, int, int]] = []
    if len(candidate_idx) > 0:
        # Most vivid first — these are the most perceptually "pop" colours
        candidate_idx = candidate_idx[np.argsort(-C_c[candidate_idx])][:3]
        for i in candidate_idx:
            rgb_f = _lab_to_rgb(centers[i : i + 1])[0]
            # Discard near-grey candidates — they wash out to nothing on a mat liner
            hsv = _rgb_to_hsv(rgb_f.reshape(1, 3))
            if hsv[0, 1] * 255 < 25:
                continue
            accent_colors.append(
                tuple(int(v) for v in (rgb_f * 255.0).round().clip(0, 255))
            )

    return {
        "palette_temperature": round(palette_temperature, 3),
        "accent_colors":       accent_colors,
        "brightness":          round(brightness, 3),
        "contrast":            round(contrast, 3),
        "edge_brightness":     round(edge_brightness, 3),
    }


# ---------------------------------------------------------------------------
# CLI for development inspection
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("Usage: python3 painting_analysis.py <image_path>", file=sys.stderr)
        sys.exit(1)

    result = analyse(Image.open(sys.argv[1]))
    # accent_colors are tuples; make them lists for clean JSON output
    result["accent_colors"] = [list(c) for c in result["accent_colors"]]
    print(json.dumps(result, indent=2))
