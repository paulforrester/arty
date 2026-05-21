#!/usr/bin/env python3
"""Fetch impressionist and post-impressionist artworks from the Art Institute of Chicago API."""

import json
import logging
import re
import time
from pathlib import Path

import requests

OUTPUT_DIR = Path("/Users/paulf/arty/artic")
IIIF_BASE = "https://www.artic.edu/iiif/2"
API_BASE = "https://api.artic.edu/api/v1"
REQUEST_DELAY = 1.0  # seconds between requests
TARGET_COUNT = 40

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "arty/1.0 (personal art display project)"})


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "_", text)
    return text[:60].strip("_") or "unknown"


def fetch_artworks(style_label: str, page: int = 1, limit: int = 50) -> dict:
    params = {
        "query[bool][must][0][match][style_titles]": style_label,
        "query[bool][must][1][term][is_public_domain]": "true",
        "fields": "id,title,artist_display,artist_title,date_display,place_of_origin,"
                  "image_id,style_titles,classification_titles,dimensions,medium_display,"
                  "artwork_type_title",
        "limit": limit,
        "page": page,
    }
    resp = SESSION.get(f"{API_BASE}/artworks/search", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


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
        "artic_id": artwork.get("id"),
        "title": artwork.get("title"),
        "artist": artwork.get("artist_display") or artwork.get("artist_title"),
        "date": artwork.get("date_display"),
        "origin": artwork.get("place_of_origin"),
        "styles": artwork.get("style_titles", []),
        "medium": artwork.get("medium_display"),
        "dimensions": artwork.get("dimensions"),
        "artwork_type": artwork.get("artwork_type_title"),
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


def collect_artworks(target: int) -> list[dict]:
    """Return up to *target* artworks that are not yet on disk."""
    styles = ["Impressionism", "Post-Impressionism"]
    seen_ids: set[int] = set()
    results = []

    for style in styles:
        page = 1
        while len(results) < target:
            log.info("Fetching '%s' page=%d ...", style, page)
            try:
                data = fetch_artworks(style, page=page, limit=50)
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
            fetched_so_far = page * 50
            if fetched_so_far >= total:
                break
            page += 1
            time.sleep(REQUEST_DELAY)

        if len(results) >= target:
            break

    return results[:target]


def process_artwork(artwork: dict) -> bool:
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


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Collecting artwork metadata (target: %d works) ...", TARGET_COUNT)

    artworks = collect_artworks(TARGET_COUNT)
    log.info("Found %d candidates with images", len(artworks))

    saved = 0
    for i, artwork in enumerate(artworks, 1):
        log.info("[%d/%d]", i, len(artworks))
        if process_artwork(artwork):
            saved += 1
        time.sleep(REQUEST_DELAY)

    log.info("Done. %d/%d works saved to %s", saved, len(artworks), OUTPUT_DIR)


if __name__ == "__main__":
    main()
