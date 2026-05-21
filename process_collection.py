#!/usr/bin/env python3
"""
process_collection.py

Walk an artic download tree, composite each artwork via frame_compositor,
and save 3840×2160 JPEG results.

Usage
-----
    # Full collection
    python3 process_collection.py [--input DIR] [--output DIR] [--style STYLE]
                                  [--force]

    # Single file (useful for testing)
    python3 process_collection.py --file PATH [--output DIR] [--style STYLE]
                                  [--force]

Input layout:   {input}/{artist}/image/{stem}.jpg
                {input}/{artist}/meta/{stem}.json   (optional)
Output:         {output}/{artist}/{stem}.jpg

Defaults
--------
    --input   /Users/paulf/arty/artic
    --output  /Users/paulf/arty/processed
    --style   walnut
"""

import argparse
import hashlib
import json
import logging
import sys
from pathlib import Path

from PIL import Image

import frame_compositor
import painting_analysis
import style_selector
import styles

log = logging.getLogger(__name__)


def _seed(stem: str) -> int:
    """Deterministic 31-bit seed from filename stem."""
    return int(hashlib.md5(stem.encode()).hexdigest()[:8], 16) & 0x7FFFFFFF


def process_one(
    img_path:       Path,
    meta_path:      Path,
    out_path:       Path,
    override_frame: str | None = None,
    override_mat:   str | None = None,
) -> bool:
    try:
        artwork = Image.open(img_path)
    except Exception as exc:
        log.error("Cannot open %s: %s", img_path, exc)
        return False

    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Cannot read metadata %s: %s", meta_path, exc)

    try:
        analysis = painting_analysis.analyse(artwork)
        chosen   = style_selector.select(analysis, meta)
    except Exception as exc:
        log.error("Style selection failed for %s: %s", img_path.name, exc)
        return False

    frame_style      = override_frame or chosen["frame_style"]
    mat_config       = override_mat   or chosen["mat_config"]
    mat_accent_color = chosen["mat_accent_color"]

    log.info("      style  frame=%-14s  mat=%s", frame_style, mat_config)

    try:
        result = frame_compositor.compose(
            artwork, meta,
            frame_style=frame_style,
            mat_config=mat_config,
            mat_accent_color=mat_accent_color,
            seed=_seed(img_path.stem),
        )
    except Exception as exc:
        log.error("Compositor failed for %s: %s", img_path.name, exc)
        return False

    out_path.parent.mkdir(parents=True, exist_ok=True)
    result.save(str(out_path), "JPEG", quality=95, subsampling=0)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Composite artic artwork into museum-framed 4K wallpapers.",
    )
    parser.add_argument(
        "--input", "-i",
        default="/Users/paulf/arty/artic",
        metavar="DIR",
        help="Root of downloaded artwork tree (default: /Users/paulf/arty/artic)",
    )
    parser.add_argument(
        "--output", "-o",
        default="/Users/paulf/arty/processed",
        metavar="DIR",
        help="Output directory (default: /Users/paulf/arty/processed)",
    )
    parser.add_argument(
        "--override-frame",
        metavar="STYLE",
        choices=list(styles.FRAME_STYLES),
        help="Force a specific frame style instead of the auto-selected one",
    )
    parser.add_argument(
        "--override-mat",
        metavar="CONFIG",
        choices=list(styles.MAT_CONFIGS),
        help="Force a specific mat config instead of the auto-selected one",
    )
    parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="Re-process images that already have output files",
    )
    parser.add_argument(
        "--file",
        metavar="PATH",
        help="Process a single image file instead of the whole collection",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    out_dir = Path(args.output)

    if args.file:
        images = [Path(args.file)]
        if not images[0].exists():
            log.error("File not found: %s", args.file)
            sys.exit(1)
    else:
        in_dir = Path(args.input)
        images = sorted(in_dir.glob("*/image/*.jpg"))
        if not images:
            log.error("No images found under %s", in_dir)
            sys.exit(1)

    log.info("Found %d image(s).  Output: %s", len(images), out_dir)
    if args.override_frame or args.override_mat:
        log.info("Overrides — frame: %s  mat: %s",
                 args.override_frame or "auto", args.override_mat or "auto")

    ok = skipped = failed = 0

    for i, img_path in enumerate(images, 1):
        artist_dir = img_path.parent.parent
        stem       = img_path.stem
        meta_path  = artist_dir / "meta" / f"{stem}.json"
        out_path   = out_dir / artist_dir.name / f"{stem}.jpg"

        if out_path.exists() and not args.force:
            log.info("[%d/%d] skip  %s/%s", i, len(images),
                     artist_dir.name, img_path.name)
            skipped += 1
            continue

        log.info("[%d/%d] %s/%s", i, len(images),
                 artist_dir.name, img_path.name)

        if process_one(img_path, meta_path, out_path,
                       override_frame=args.override_frame,
                       override_mat=args.override_mat):
            log.info("      → %s", out_path)
            ok += 1
        else:
            failed += 1

    log.info(
        "Done — %d processed, %d skipped, %d failed. Output: %s",
        ok, skipped, failed, out_dir,
    )


if __name__ == "__main__":
    main()
