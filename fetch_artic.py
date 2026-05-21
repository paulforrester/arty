#!/usr/bin/env python3
"""Fetch artworks from the Art Institute of Chicago API.

Default mode (no --artist / --artists-file): fetches by style classification,
preserving the original behavior.

Artist mode: fetches up to --limit works per named artist, with an exact-match
query that falls back to fuzzy matching when fewer than 5 results are found.
"""

import argparse
import json
import logging
import re
import time
from pathlib import Path

import requests

OUTPUT_DIR    = Path.home() / "arty" / "artic"
IIIF_BASE     = "https://www.artic.edu/iiif/2"
API_BASE      = "https://api.artic.edu/api/v1"
REQUEST_DELAY = 1.0   # seconds between HTTP requests
TARGET_COUNT  = 40    # works to fetch in style mode
DEFAULT_LIMIT = 25    # default per-artist limit in artist mode

# Fields requested from every search — shared between both query modes
_FIELDS = (
    "id,title,artist_title,artist_display,date_display,place_of_origin,"
    "medium_display,style_titles,classification_titles,artwork_type_title,"
    "is_public_domain,image_id,dimensions"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "arty/1.0 (personal art display project)"})


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "_", text)
    return text[:60].strip("_") or "unknown"


def image_url(image_id: str) -> str:
    return f"{IIIF_BASE}/{image_id}/full/full/0/default.jpg"


def download_image(url: str, dest: Path) -> bool:
    try:
        resp = SESSION.get(url, timeout=60, stream=True)
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        return True
    except requests.RequestException as e:
        log.warning("Image download failed %s: %s", url, e)
        return False


def save_metadata(artwork: dict, image_url_str: str, dest: Path) -> None:
    meta = {
        "artic_id":        artwork.get("id"),
        "title":           artwork.get("title"),
        "artist":          artwork.get("artist_display") or artwork.get("artist_title"),
        "date":            artwork.get("date_display"),
        "origin":          artwork.get("place_of_origin"),
        "styles":          artwork.get("style_titles", []),
        "classifications": artwork.get("classification_titles", []),
        "medium":          artwork.get("medium_display"),
        "dimensions":      artwork.get("dimensions"),
        "artwork_type":    artwork.get("artwork_type_title"),
        "source_image_url": image_url_str,
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)


def _artwork_paths(artwork: dict) -> tuple[Path, Path]:
    """Return (img_path, meta_path) for an artwork dict."""
    artist_slug   = slugify(artwork.get("artist_title") or "unknown_artist")
    artwork_id    = artwork.get("id")
    filename_stem = f"{slugify(artwork.get('title') or str(artwork_id))}_{artwork_id}"
    img_path  = OUTPUT_DIR / artist_slug / "image" / f"{filename_stem}.jpg"
    meta_path = OUTPUT_DIR / artist_slug / "meta"  / f"{filename_stem}.json"
    return img_path, meta_path


def process_artwork(artwork: dict) -> bool:
    """Download image + save metadata for one artwork. Returns True on success."""
    image_id = artwork.get("image_id")
    if not image_id:
        return False

    img_path, meta_path = _artwork_paths(artwork)

    if img_path.exists() and meta_path.exists():
        log.info("Already exists, skipping: %s", img_path.stem)
        return True

    url = image_url(image_id)
    log.info("Downloading: %s — %s", artwork.get("title"),
             artwork.get("artist_title") or "unknown")

    if not download_image(url, img_path):
        log.warning("No image available for %s (id=%s)",
                    artwork.get("title"), artwork.get("id"))
        return False

    save_metadata(artwork, url, meta_path)
    log.info("Saved %s", img_path.stem)
    return True


# ---------------------------------------------------------------------------
# Style-based fetch (original behavior, unchanged)
# ---------------------------------------------------------------------------

def _fetch_by_style(style_label: str, page: int = 1, limit: int = 50) -> dict:
    params = {
        "query[bool][must][0][match][style_titles]": style_label,
        "query[bool][must][1][term][is_public_domain]": "true",
        "fields": _FIELDS,
        "limit": limit,
        "page": page,
    }
    resp = SESSION.get(f"{API_BASE}/artworks/search", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def collect_artworks(target: int) -> list[dict]:
    """Return up to *target* style-matched artworks not yet on disk."""
    styles = ["Impressionism", "Post-Impressionism"]
    seen_ids: set[int] = set()
    results = []

    for style in styles:
        page = 1
        while len(results) < target:
            log.info("Fetching '%s' page=%d ...", style, page)
            try:
                data = _fetch_by_style(style, page=page, limit=50)
            except requests.RequestException as e:
                log.error("API fetch failed: %s", e)
                break

            items = data.get("data", [])
            if not items:
                break

            for item in items:
                artwork_id = item.get("id")
                if artwork_id in seen_ids:
                    continue
                if not item.get("image_id"):
                    continue
                seen_ids.add(artwork_id)
                img_path, meta_path = _artwork_paths(item)
                if img_path.exists() and meta_path.exists():
                    log.info("Already on disk, skipping: %s", img_path.stem)
                    continue
                results.append(item)
                if len(results) >= target:
                    break

            total = data.get("pagination", {}).get("total", 0)
            if page * 50 >= total:
                break
            page += 1
            time.sleep(REQUEST_DELAY)

        if len(results) >= target:
            break

    return results[:target]


def run_style_mode(target: int) -> None:
    log.info("Collecting artwork metadata (target: %d works) ...", target)
    artworks = collect_artworks(target)
    log.info("Found %d new candidates", len(artworks))

    saved = 0
    for i, artwork in enumerate(artworks, 1):
        log.info("[%d/%d]", i, len(artworks))
        if process_artwork(artwork):
            saved += 1
        time.sleep(REQUEST_DELAY)

    log.info("Done. %d/%d works saved to %s", saved, len(artworks), OUTPUT_DIR)


# ---------------------------------------------------------------------------
# Artist-based fetch
# ---------------------------------------------------------------------------

def _fetch_by_artist(
    artist_name: str,
    style:  str | None = None,
    page:   int = 1,
    limit:  int = 50,
    exact:  bool = True,
) -> dict:
    """
    Query the ARTIC search endpoint for works by *artist_name*.

    exact=True  uses term[artist_title.keyword] — exact stored value only
    exact=False uses match_phrase[artist_title] — all query words must appear
                in order, handling partial names like "Van Gogh" → "Vincent
                van Gogh" without pulling unrelated artists
    """
    must: dict[str, str] = {}
    idx = 0

    if exact:
        must[f"query[bool][must][{idx}][term][artist_title.keyword]"] = artist_name
    else:
        must[f"query[bool][must][{idx}][match_phrase][artist_title]"] = artist_name
    idx += 1

    must[f"query[bool][must][{idx}][term][is_public_domain]"] = "true"
    idx += 1

    if style:
        must[f"query[bool][must][{idx}][match][style_titles]"] = style

    params = {**must, "fields": _FIELDS, "limit": limit, "page": page}
    resp = SESSION.get(f"{API_BASE}/artworks/search", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def collect_artworks_by_artist(
    artist_name: str,
    limit: int,
    style: str | None = None,
) -> tuple[list[dict], dict]:
    """
    Collect up to *limit* new artworks for *artist_name*.

    Returns (artworks_to_download, stats) where stats has keys:
      found, skipped_exists, skipped_no_image, skipped_not_public
    """
    stats: dict[str, int] = {
        "found": 0, "skipped_exists": 0,
        "skipped_no_image": 0, "skipped_not_public": 0,
        "skipped_wrong_artist": 0,
    }
    seen_ids:    set[int] = set()
    results:     list[dict] = []
    query_words: list[str] = artist_name.lower().split()

    # Probe with exact match to decide whether to use exact or fuzzy for all pages.
    # Fewer than 5 total results suggests the stored name differs from our query.
    try:
        probe = _fetch_by_artist(artist_name, style=style, page=1, limit=1, exact=True)
    except requests.RequestException as e:
        log.error("API probe failed for '%s': %s", artist_name, e)
        return results, stats

    exact = probe.get("pagination", {}).get("total", 0) >= 5
    if not exact:
        log.info(
            "Exact match returned < 5 results for '%s'; switching to fuzzy match",
            artist_name,
        )

    page = 1
    while len(results) < limit:
        try:
            data = _fetch_by_artist(
                artist_name, style=style, page=page, limit=50, exact=exact
            )
        except requests.RequestException as e:
            log.error("API fetch failed for '%s': %s", artist_name, e)
            break

        items = data.get("data", [])
        if not items:
            break

        for item in items:
            artwork_id = item.get("id")
            if artwork_id in seen_ids:
                continue
            seen_ids.add(artwork_id)

            # Post-filter: confirm every query word appears in the stored
            # artist_title.  Catches any noise that slips past match_phrase
            # (e.g. a different "van Gogh" family member).
            item_artist = (item.get("artist_title") or "").lower()
            if not all(w in item_artist for w in query_words):
                log.debug(
                    "Artist mismatch, skipping: '%s' (query: '%s')",
                    item.get("artist_title"), artist_name,
                )
                stats["skipped_wrong_artist"] += 1
                continue

            stats["found"] += 1

            if not item.get("is_public_domain"):
                log.debug("Not public domain, skipping: %s (id=%s)",
                          item.get("title"), artwork_id)
                stats["skipped_not_public"] += 1
                continue

            if not item.get("image_id"):
                log.info("No image, skipping: %s (id=%s)", item.get("title"), artwork_id)
                stats["skipped_no_image"] += 1
                continue

            img_path, meta_path = _artwork_paths(item)
            if img_path.exists() and meta_path.exists():
                log.info("Already on disk, skipping: %s", img_path.stem)
                stats["skipped_exists"] += 1
                continue

            results.append(item)
            if len(results) >= limit:
                break

        total = data.get("pagination", {}).get("total", 0)
        if page * 50 >= total:
            break
        page += 1
        time.sleep(REQUEST_DELAY)

    return results, stats


def run_artist_mode(artists: list[str], limit: int, style: str | None) -> None:
    summary: list[tuple[str, int, int]] = []   # (name, downloaded, total_skipped)

    for artist in artists:
        log.info("--- %s ---", artist)
        artworks, stats = collect_artworks_by_artist(artist, limit=limit, style=style)
        extras = ""
        if stats["skipped_wrong_artist"]:
            extras = f"  wrong artist: {stats['skipped_wrong_artist']}"
        log.info(
            "  %d new works to download  (on disk: %d  no image: %d  not public: %d%s)",
            len(artworks),
            stats["skipped_exists"],
            stats["skipped_no_image"],
            stats["skipped_not_public"],
            extras,
        )

        downloaded = 0
        for i, artwork in enumerate(artworks, 1):
            log.info("  [%d/%d] %s", i, len(artworks), artwork.get("title"))
            if process_artwork(artwork):
                downloaded += 1
            time.sleep(REQUEST_DELAY)

        total_skipped = (
            stats["skipped_exists"]
            + stats["skipped_no_image"]
            + stats["skipped_not_public"]
        )
        summary.append((artist, downloaded, total_skipped))

    # Summary table
    col = max((len(a) for a, *_ in summary), default=20)
    col = max(col, 6)
    sep = f"{'-' * col}  {'-' * 10}  {'-' * 7}"
    print()
    print(f"{'Artist':<{col}}  {'Downloaded':>10}  {'Skipped':>7}")
    print(sep)
    total_dl = total_sk = 0
    for name, dl, sk in summary:
        print(f"{name:<{col}}  {dl:>10}  {sk:>7}")
        total_dl += dl
        total_sk += sk
    print(sep)
    print(f"{'Total':<{col}}  {total_dl:>10}  {total_sk:>7}")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch artwork from the Art Institute of Chicago.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python3 fetch_artic.py                                      "
            "# style mode (Impressionism / Post-Impressionism)\n"
            "  python3 fetch_artic.py --artist 'Claude Monet' --limit 30\n"
            "  python3 fetch_artic.py --artists-file impressionists.txt --limit 20\n"
            "  python3 fetch_artic.py --artist 'Georges Seurat' "
            "--style 'Post-Impressionism'"
        ),
    )
    parser.add_argument(
        "--artist",
        metavar="NAME",
        help="Fetch works by a single artist",
    )
    parser.add_argument(
        "--artists-file",
        metavar="PATH",
        help="Fetch works for all artists in a text file (one name per line)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        metavar="N",
        help=f"Max works to fetch per artist (default: {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--style",
        metavar="STYLE",
        help="Additional style filter, e.g. 'Impressionism' (artist mode only)",
    )
    args = parser.parse_args()

    if args.artist and args.artists_file:
        parser.error("--artist and --artists-file are mutually exclusive")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.artist:
        run_artist_mode([args.artist], limit=args.limit, style=args.style)
    elif args.artists_file:
        path = Path(args.artists_file)
        if not path.exists():
            parser.error(f"Artists file not found: {args.artists_file}")
        artists = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        if not artists:
            parser.error(f"No artists found in {args.artists_file}")
        log.info("Loaded %d artists from %s", len(artists), path)
        run_artist_mode(artists, limit=args.limit, style=args.style)
    else:
        run_style_mode(target=TARGET_COUNT)


if __name__ == "__main__":
    main()
