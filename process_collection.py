#!/usr/bin/env python3
"""
process_collection.py

Walk an artic download tree, composite each artwork via frame_compositor,
and save 3840×2160 JPEG results using all available CPU cores.

Usage
-----
    python3 process_collection.py [--input DIR] [--output DIR]
                                  [--override-frame STYLE] [--override-mat CONFIG]
                                  [--no-mat] [--workers N] [--force]

    python3 process_collection.py --file PATH [--output DIR] [--force]

Defaults
--------
    --input    ~/arty/artic
    --output   ~/arty/processed
    --workers  os.cpu_count() - 1
"""

import argparse
import gc
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from PIL import Image

import frame_compositor
import painting_analysis
import style_selector
import styles


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed(stem: str) -> int:
    """Deterministic 31-bit seed from filename stem."""
    return int(hashlib.md5(stem.encode()).hexdigest()[:8], 16) & 0x7FFFFFFF


def _fmt_time(seconds: float) -> str:
    """Format a duration as Xm00s."""
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s"


# ---------------------------------------------------------------------------
# Worker (module-level so ProcessPoolExecutor can pickle it)
# ---------------------------------------------------------------------------

def process_one(
    img_path:       Path,
    meta_path:      Path,
    out_path:       Path,
    override_frame: str | None = None,
    override_mat:   str | None = None,
    no_mat:         bool = False,
) -> dict:
    """
    Process a single artwork end-to-end: analyse → select style →
    composite → save.  Never raises; all errors are captured in the
    returned dict so the pool can deliver them safely to the main process.

    Returns
    -------
    dict with keys:
        success          bool
        img_path         str
        label            str   "artist/filename.jpg" for display
        frame_style      str
        mat_config       str
        use_mat          bool
        elapsed_seconds  float
        error            str | None
    """
    t0 = time.perf_counter()

    p = Path(img_path)
    result: dict = {
        "success":         False,
        "img_path":        str(img_path),
        "label":           f"{p.parent.parent.name}/{p.name}",
        "frame_style":     "",
        "mat_config":      "",
        "use_mat":         True,
        "elapsed_seconds": 0.0,
        "error":           None,
    }

    try:
        artwork = Image.open(img_path)

        meta: dict = {}
        if Path(meta_path).exists():
            try:
                meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
            except Exception as exc:
                # Non-fatal — continue with empty meta; note it in error field.
                result["error"] = f"metadata read failed: {exc}"

        analysis = painting_analysis.analyse(artwork)
        chosen   = style_selector.select(analysis, meta)

        frame_style      = override_frame or chosen["frame_style"]
        mat_config       = override_mat   or chosen["mat_config"]
        mat_accent_color = chosen["mat_accent_color"]
        use_mat          = False if no_mat else chosen.get("mat", True)

        result["frame_style"] = frame_style
        result["mat_config"]  = mat_config
        result["use_mat"]     = use_mat

        composed = frame_compositor.compose(
            artwork, meta,
            frame_style=frame_style,
            mat_config=mat_config,
            mat_accent_color=mat_accent_color,
            seed=_seed(p.stem),
            mat=use_mat,
        )

        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        composed.save(str(out_path), "JPEG", quality=95, subsampling=0)
        result["success"] = True

        # Release large objects before the process returns to the pool so
        # peak RSS stays reasonable when many workers run concurrently.
        del composed, artwork, analysis
        gc.collect()

    except Exception as exc:
        result["error"] = str(exc)

    result["elapsed_seconds"] = time.perf_counter() - t0
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    cpu_count       = os.cpu_count() or 1
    default_workers = max(1, cpu_count - 1)

    parser = argparse.ArgumentParser(
        description="Composite artic artwork into museum-framed 4K wallpapers.",
    )
    parser.add_argument(
        "--input", "-i",
        default=str(Path.home() / "arty" / "artic"),
        metavar="DIR",
        help="Root of downloaded artwork tree (default: ~/arty/artic)",
    )
    parser.add_argument(
        "--output", "-o",
        default=str(Path.home() / "arty" / "processed"),
        metavar="DIR",
        help="Output directory (default: ~/arty/processed)",
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
        "--no-mat",
        action="store_true",
        help="Omit the mat; artwork sits directly against the frame rail",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=default_workers,
        metavar="N",
        help=f"Parallel worker processes (default: {default_workers}, max: {cpu_count})",
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

    n_workers = max(1, min(args.workers, cpu_count))
    out_dir   = Path(args.output)

    # ── Discover images ──────────────────────────────────────────────────────

    if args.file:
        img_file = Path(args.file)
        if not img_file.exists():
            print(f"Error: file not found: {args.file}", file=sys.stderr)
            sys.exit(1)
        all_images = [img_file]
    else:
        in_dir = Path(args.input)
        all_images = sorted(in_dir.glob("*/image/*.jpg"))
        if not all_images:
            print(f"Error: no images found under {in_dir}", file=sys.stderr)
            sys.exit(1)

    # ── Build job list ────────────────────────────────────────────────────────

    jobs:    list[tuple] = []
    skipped: int         = 0

    for img_path in all_images:
        artist_dir = img_path.parent.parent
        stem       = img_path.stem
        meta_path  = artist_dir / "meta" / f"{stem}.json"
        out_path   = out_dir / artist_dir.name / f"{stem}.jpg"

        if out_path.exists() and not args.force:
            skipped += 1
            continue

        jobs.append((img_path, meta_path, out_path,
                     args.override_frame, args.override_mat, args.no_mat))

    total = len(jobs)

    print(f"Images: {len(all_images)} found, {skipped} skipped (already processed), "
          f"{total} to process")
    print(f"Workers: {n_workers} of {cpu_count} cores")

    if n_workers > 8:
        print(f"Note: {n_workers} workers may require ~{n_workers * 250} MB RAM. "
              "Monitor memory usage.")

    if args.override_frame or args.override_mat:
        print(f"Overrides — frame: {args.override_frame or 'auto'}  "
              f"mat: {args.override_mat or 'auto'}")

    if total == 0:
        print(f"\nCompleted: 0  Skipped: {skipped}  Failed: 0  "
              f"Total time: 0m00s")
        return

    # ── Process ───────────────────────────────────────────────────────────────

    ok            = 0
    failed        = 0
    failed_list:  list[dict] = []
    start_time    = time.perf_counter()

    print()  # blank line before the rolling output

    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        future_to_job = {
            executor.submit(process_one, *job): job
            for job in jobs
        }

        completed = 0
        for future in as_completed(future_to_job):
            try:
                result = future.result()
            except Exception as exc:
                # Should not happen — process_one catches internally — but
                # handle defensively so the pool never crashes the main process.
                job = future_to_job[future]
                result = {
                    "success":         False,
                    "img_path":        str(job[0]),
                    "label":           f"{Path(job[0]).parent.parent.name}/{Path(job[0]).name}",
                    "frame_style":     "",
                    "mat_config":      "",
                    "elapsed_seconds": 0.0,
                    "error":           str(exc),
                }

            completed += 1
            elapsed   = time.perf_counter() - start_time

            if result["success"]:
                ok += 1
                print(f"[{completed}/{total}] {result['label']}"
                      f" → frame:{result['frame_style']} mat:{result['mat_config']}"
                      f" ({result['elapsed_seconds']:.1f}s)")
            else:
                failed += 1
                failed_list.append(result)
                print(f"[{completed}/{total}] FAILED {result['label']}: "
                      f"{result['error']}")

            pct = completed / total * 100
            eta = (elapsed / completed) * (total - completed) if completed < total else 0.0
            print(
                f"Progress: {completed}/{total} ({pct:.0f}%) | "
                f"Elapsed: {_fmt_time(elapsed)} | "
                f"ETA: {_fmt_time(eta)}          ",
                end="\r", flush=True,
            )

    print()  # newline past the final progress line

    total_elapsed = time.perf_counter() - start_time
    avg           = total_elapsed / ok if ok else 0.0

    print(f"\nCompleted: {ok}  Skipped: {skipped}  Failed: {failed}  "
          f"Total time: {_fmt_time(total_elapsed)}")
    print(f"Average: {avg:.1f}s/image  Cores used: {n_workers}")

    if failed_list:
        print("\nFailed images:")
        for r in failed_list:
            print(f"  {r['img_path']}: {r['error']}")


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.set_start_method("spawn", force=True)
    main()
