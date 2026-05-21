#!/usr/bin/env python3
"""
style_selector.py

Choose frame and mat styles for a painting given its perceptual analysis
and ARTIC metadata.

Public API
----------
    select(analysis, meta) -> dict with keys:
        frame_style       str                  key from styles.FRAME_STYLES
        mat_config        str                  key from styles.MAT_CONFIGS
        mat_accent_color  tuple[int,int,int] | None
"""

from __future__ import annotations

import colorsys
import logging

import styles

log = logging.getLogger(__name__)


def _mat_accent(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Lighten by +20 pp and desaturate by −10 pp (HLS) for mat use.
    Falls back to warm ivory if the result is still too grey to read."""
    r, g, b = (v / 255.0 for v in rgb)
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    l = min(1.0, l + 0.20)
    s = max(0.0, s - 0.10)
    r2, g2, b2 = colorsys.hls_to_rgb(h, l, s)
    _, s_hsv, _ = colorsys.rgb_to_hsv(r2, g2, b2)
    if s_hsv * 255 < 15:
        return (245, 238, 220)   # warm ivory — better than a grey liner
    return (round(r2 * 255), round(g2 * 255), round(b2 * 255))


def _classification(meta: dict) -> str:
    """Lower-case string of all style/classification text from meta."""
    raw = meta.get("styles") or []
    if isinstance(raw, list):
        return " ".join(raw).lower()
    return str(raw).lower()


def select(analysis: dict, meta: dict) -> dict:
    """
    Select frame and mat styles for a painting.

    Parameters
    ----------
    analysis : output of painting_analysis.analyse()
    meta     : ARTIC metadata dict (title, artist, date, styles, …)

    Returns
    -------
    dict with keys frame_style, mat_config, mat_accent_color
    """
    temp       = analysis["palette_temperature"]
    brightness = analysis["brightness"]
    contrast   = analysis["contrast"]
    accents    = analysis.get("accent_colors", [])

    clsf = _classification(meta)

    # "post-impressioni" must be tested before "impressioni": the former is
    # a substring of the latter (e.g. "post-impressionism" contains "impressioni"),
    # so order here determines which rule fires for post-impressionist works.
    is_post_imp = "post-impressioni" in clsf
    is_imp      = "impressioni" in clsf and not is_post_imp

    frame_style: str | None = None
    mat_config:  str | None = None

    # --- Rule 1-2: classification (highest priority) ------------------------

    if is_post_imp:
        frame_style = "painted_black" if temp < -0.1 else "oak"
        mat_config  = "double_neutral"

    elif is_imp:
        # Warm palette suits gold; cool or neutral suits natural oak.
        frame_style = "gilded" if temp >= 0.0 else "oak"
        mat_config  = "double_accent"

    # --- Rules 3-4: perceptual temperature (only when classification missed) -

    if frame_style is None:
        if temp > 0.3:
            frame_style = "gilded" if temp > 0.55 else "walnut"
        elif temp < -0.3:
            frame_style = "painted_black" if temp < -0.55 else "silver_leaf"

    # --- Rule 5: brightness guard — dark frames compete with dark canvases --

    if brightness < 0.35 and frame_style in ("painted_black", "walnut", None):
        frame_style = "painted_cream" if brightness < 0.2 else "oak"

    # --- Defaults -----------------------------------------------------------

    if frame_style is None:
        frame_style = "walnut"
    if mat_config is None:
        # Warm paintings that didn't match a classification rule get a
        # slightly warmer mat to complement the frame choice.
        mat_config = "single_warm" if temp > 0.3 else "single_neutral"

    # --- Rule 6: high-contrast paintings get a simpler single mat -----------

    if contrast > 0.6:
        mat_config = "single_neutral"

    # --- Accent colour for double-accent mats --------------------------------

    mat_accent_color: tuple[int, int, int] | None = None
    if styles.MAT_CONFIGS[mat_config]["uses_accent"] and accents:
        mat_accent_color = _mat_accent(accents[0])
        log.info("      accent %s", mat_accent_color)

    return {
        "frame_style":      frame_style,
        "mat_config":       mat_config,
        "mat_accent_color": mat_accent_color,
    }


# ---------------------------------------------------------------------------
# CLI for development inspection
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    from PIL import Image
    import painting_analysis

    if len(sys.argv) < 2:
        print("Usage: python3 style_selector.py <image_path> [meta.json]",
              file=sys.stderr)
        sys.exit(1)

    img      = Image.open(sys.argv[1])
    analysis = painting_analysis.analyse(img)

    meta: dict = {}
    if len(sys.argv) >= 3:
        with open(sys.argv[2], encoding="utf-8") as fh:
            meta = json.load(fh)

    result = select(analysis, meta)

    # Tuples aren't JSON-native; convert accent colour to a list for output.
    if result["mat_accent_color"] is not None:
        result = dict(result, mat_accent_color=list(result["mat_accent_color"]))

    print(json.dumps(result, indent=2))
