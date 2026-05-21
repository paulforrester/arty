#!/usr/bin/env python3
"""
styles.py

Catalog of available frame and mat styles.

FRAME_STYLES  — keys are the values returned as frame_style by style_selector.
MAT_CONFIGS   — keys are the values returned as mat_config by style_selector.

New frame styles (gilded, silver_leaf, painted_black, painted_cream) require
corresponding entries in wood_texture.STYLES before they can be rendered by
frame_compositor.  The catalog is intentionally broader than the current
renderer so style_selector can be extended without changing this file.
"""

from __future__ import annotations

FRAME_STYLES: dict[str, dict] = {
    "walnut": {
        "wood_style": "walnut",
        "warmth":     "warm",
        "tone":       "medium",
    },
    "oak": {
        "wood_style": "oak",
        "warmth":     "warm",
        "tone":       "light",
    },
    "gilded": {
        "wood_style": "gilded",
        "warmth":     "warm",
        "tone":       "light",
    },
    "silver_leaf": {
        "wood_style": "silver_leaf",
        "warmth":     "cool",
        "tone":       "light",
    },
    "painted_black": {
        "wood_style": "painted_black",
        "warmth":     "neutral",
        "tone":       "dark",
    },
    "painted_cream": {
        "wood_style": "painted_cream",
        "warmth":     "warm",
        "tone":       "light",
    },
}

MAT_CONFIGS: dict[str, dict] = {
    "single_neutral": {
        "layers":        1,
        "uses_accent":   False,
        "primary_color": (240, 234, 220),   # matches MAT_COLOR in frame_compositor
    },
    "single_warm": {
        "layers":        1,
        "uses_accent":   False,
        "primary_color": (245, 238, 224),
    },
    "double_neutral": {
        "layers":          2,
        "uses_accent":     False,
        "primary_color":   (240, 234, 220),
        "secondary_color": (218, 210, 196),
    },
    "double_accent": {
        "layers":        2,
        "uses_accent":   True,
        "primary_color": (240, 234, 220),
        # secondary_color is supplied at runtime as mat_accent_color
    },
}
