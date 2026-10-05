"""
crop_decisions.py

Per-image crop decisions made in ArtyPicker's Crop mode, stored in one file,
~/arty/crops.json, and applied by process_collection before analysis.

File format
-----------
    {
      "version": 1,
      "images": {
        "brave/salvador_dalí/the_persistence_of_memory_magnet_00908cecacacb400": {
          "mode":    "manual",            # "original" | "auto" | "manual"
          "box":     [l, t, r, b],        # manual only; source pixels, r/b exclusive
          "size":    [w, h],              # source dimensions when the box was drawn
          "frame":   false,               # optional; false = leave unframed (default: framed)
          "mat":     true,                # optional; true/false forces a mat on/off (default: automatic)
          "updated": "2026-10-04T09:12:00Z"
        }
      }
    }

Keys are "<collection>/<artist_dir>/<stem>", NFC-normalised so macOS's
decomposed filenames and the Swift app agree.  Images with no entry get the
default (automatic matte detection).
"""

import json
import os
import unicodedata
from pathlib import Path

from PIL import Image

import matte_crop

CROPS_PATH = Path(os.environ.get("ARTY_CROPS") or Path.home() / "arty" / "crops.json")

# Tried when an image has no stored decision.  "auto" = automatic detection;
# flip to "original" to make unreviewed images pass through untouched.
DEFAULT_MODE = "auto"

_MIN_MANUAL_SIDE = 8

_cache: dict[str, dict] = {}


def key_for(img_path: Path) -> str | None:
    """'<collection>/<artist_dir>/<stem>' for .../<collection>/<artist>/image/<stem>.jpg, else None."""
    p = Path(img_path)
    if p.parent.name != "image":
        return None
    parts = (p.parent.parent.parent.name, p.parent.parent.name, p.stem)
    return unicodedata.normalize("NFC", "/".join(parts))


def load(path: Path = CROPS_PATH) -> dict[str, dict]:
    """All stored decisions ({} if the file is missing or unreadable).  Cached per process."""
    cache_key = str(path)
    if cache_key not in _cache:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            images = data.get("images", {})
            _cache[cache_key] = {unicodedata.normalize("NFC", k): v for k, v in images.items()}
        except (OSError, ValueError, AttributeError):
            _cache[cache_key] = {}
    return _cache[cache_key]


def decision_for(img_path: Path, path: Path = CROPS_PATH) -> dict | None:
    key = key_for(img_path)
    return load(path).get(key) if key else None


def options(decision: dict | None) -> tuple[bool, bool | None]:
    """
    (frame, mat) from a stored decision.

    frame: False only when the decision says ``"frame": false`` — the image is
           then output alone (no frame, mat or plaque).
    mat:   True / False when the decision forces it; None = let style_selector
           decide.  Ignored when unframed.
    """
    if not decision:
        return True, None
    frame = decision.get("frame") is not False
    mat = decision.get("mat")
    return frame, mat if isinstance(mat, bool) else None


def apply(img: Image.Image, decision: dict | None) -> tuple[Image.Image, str]:
    """
    Apply a stored decision (or the default when *decision* is None).

    Returns (image, label) where label is what happened: "original", "auto",
    "auto-default", "manual", or "stale-manual" (box no longer fits the file,
    so the original is used rather than guessing).
    """
    mode = decision.get("mode") if decision else None
    default = decision is None or mode not in ("original", "auto", "manual")
    if default:
        mode = DEFAULT_MODE

    if mode == "original":
        return img, "original"

    if mode == "manual":
        box, size = decision.get("box"), decision.get("size")
        if (isinstance(box, list) and len(box) == 4 and all(isinstance(v, int) for v in box)
                and size == [img.width, img.height]
                and 0 <= box[0] < box[2] <= img.width and 0 <= box[1] < box[3] <= img.height
                and box[2] - box[0] >= _MIN_MANUAL_SIDE and box[3] - box[1] >= _MIN_MANUAL_SIDE):
            return img.convert("RGB").crop(tuple(box)), "manual"
        return img, "stale-manual"

    cropped, did = matte_crop.crop_matte(img)
    label = "auto-default" if default else "auto"
    return (cropped, label) if did else (img, "original" if not default else "none")
