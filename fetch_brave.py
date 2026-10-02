#!/usr/bin/env python3
"""Find artwork images with the Brave Search image API, for review before use.

Two steps, so nothing reaches the collection without a human look:

  1. search  — query Brave for each artist, keep only results from allowlisted
               domains, drop junk and near-duplicates, and download the
               candidates into ~/arty/inbox/{artist}/ with a review page.
  2. accept  — after you delete the candidates you don't want (and optionally
               fix titles/dates in their .json sidecars), move the rest into
               ~/arty/brave/{artist}/image|meta, ready for
               `python3 process_collection.py --input ~/arty/brave`.

Examples:
  python3 fetch_brave.py search --artist "Joan Miró"
  python3 fetch_brave.py search --artists-file modern_artists.txt --keep 30
  python3 fetch_brave.py search --artist "Keith Haring" --dry-run
  python3 fetch_brave.py accept

API key: set BRAVE_API_KEY, or put the key alone in ~/.config/arty/brave_api_key.
The key is never written into the repo or the logs.

Images found this way are generally still under copyright. They are tagged
copyrighted/private in their metadata and are meant for private display only.
"""

from __future__ import annotations

import argparse
import html
import io
import json
import logging
import os
import re
import shutil
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image

from fetch_artic import slugify

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO_DIR       = Path(__file__).resolve().parent
INBOX_DIR      = Path.home() / "arty" / "inbox"
OUTPUT_DIR     = Path.home() / "arty" / "brave"
LEDGER_PATH    = INBOX_DIR / ".seen.json"
KEY_FILE       = Path.home() / ".config" / "arty" / "brave_api_key"
DEFAULT_ALLOW  = REPO_DIR / "brave_domains.txt"
DEFAULT_SITES  = REPO_DIR / "brave_search_sites.txt"

API_URL        = "https://api.search.brave.com/res/v1/images/search"
REQUEST_DELAY  = 1.0          # seconds between HTTP requests
DEFAULT_COUNT  = 100          # results requested per artist (API max 200)
DEFAULT_KEEP   = 40           # candidates kept per artist after filtering
DEFAULT_MIN_PX = 800          # minimum long edge in pixels
LOW_RES_PX     = 1500         # below this long edge, metadata gets low_res=True
MAX_BYTES      = 40 * 1024 * 1024
MAX_ASPECT     = 4.0          # skip banners and strips
HASH_DISTANCE  = 6            # dHash Hamming distance treated as a duplicate

# Words in a result title or URL that usually mean merchandise, not artwork
JUNK_WORDS = (
    "mug", "t-shirt", "tshirt", "shirt", "tee", "hoodie", "pillow", "cushion",
    "phone case", "iphone", "puzzle", "tote", "sticker", "mask", "socks",
    "blanket", "shower curtain", "doormat", "poster frame", "mockup",
    "wallpaper", "logo", "book cover", "exhibition view", "installation view",
    "framed print", "art print", "canvas print", "poster", "hardcover",
    "paperback", "jigsaw", "meet the artist", "gift card",
)
# Shop pages on otherwise trusted sites (e.g. MoMA Design Store)
_SHOP_URL = re.compile(r"https?://(?:[\w-]+\.)*(?:store|shop)\.|/(?:shop|store|products?)/",
                       re.IGNORECASE)
_TITLE_ENDS_PRINT = re.compile(r"\bprints?\s*$", re.IGNORECASE)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fetch_brave")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) arty/1.0 "
                  "(personal art display project)",
})


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def load_api_key() -> str:
    key = os.environ.get("BRAVE_API_KEY", "").strip()
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
    if not key:
        sys.exit(
            "No Brave API key found. Set BRAVE_API_KEY or save the key in "
            f"{KEY_FILE}"
        )
    return key


def load_allowlist(path: Path) -> list[str]:
    domains = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if line:
            domains.append(line.removeprefix("www."))
    return domains


def domain_allowed(host: str, allow: list[str]) -> bool:
    host = (host or "").lower().removeprefix("www.")
    return any(host == d or host.endswith("." + d) for d in allow)


def parse_artists_file(path: Path) -> list[tuple[str, str]]:
    """Lines are 'Artist Name' or 'Artist Name | custom search query'."""
    artists = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name, _, query = line.partition("|")
        artists.append((name.strip(), query.strip()))
    return artists


def load_ledger() -> dict:
    if LEDGER_PATH.exists():
        try:
            return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("Ledger unreadable, starting a new one: %s", LEDGER_PATH)
    return {"urls": [], "hashes": {}}


def save_ledger(ledger: dict) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(ledger, indent=1), encoding="utf-8")


def dhash(img: Image.Image, size: int = 8) -> int:
    """Difference hash: robust to rescaling and recompression."""
    g = img.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(g.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            left = px[row * (size + 1) + col]
            right = px[row * (size + 1) + col + 1]
            bits = (bits << 1) | (left > right)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


_SEPARATORS = re.compile(r"\s+[-|–—:•]\s+|\s*\|\s*")
_YEAR = re.compile(r"\b(?:(?:c|ca|circa)\.?\s*)?(1[5-9]\d\d|20[0-2]\d)(?:\s*[–-]\s*(\d{2,4}))?\b")
_SITE_WORDS = (
    "museum", "musée", "museo", "museu", "gallery", "galerie", "galleries",
    "wikipedia", "wikimedia", "commons", "moma", "christie", "sotheby",
    "phillips", "bonhams", "auction", "collection", "foundation", "fundació",
    "fondation", "institute", "tate", "guggenheim", "art store", "shop",
)


def guess_title_and_date(raw_title: str, artist: str, domain: str = "") -> tuple[str, str]:
    """Best-effort cleanup of a web page title into an artwork title and date.

    'Joan Miró - Dutch Interior (I), 1928 - MoMA' -> ('Dutch Interior (I)', '1928')
    'The Farm, 1921–1922 by Joan Miró - National Gallery of Art'
                                                  -> ('The Farm', '1921–1922')
    Titles that can't be cleaned are kept as-is; fix them in the sidecar
    during review.
    """
    text = html.unescape(raw_title or "").strip()
    match = _YEAR.search(text)
    date = ""
    if match:
        date = match.group(1) + (f"–{match.group(2)}" if match.group(2) else "")

    names = {n.lower() for n in (artist, artist.split()[-1] if artist else "") if n}
    if artist and " " in artist:                       # 'Matisse, Henri'
        first, last = artist.rsplit(" ", 1)
        names.add(f"{last}, {first}".lower())
    site_label = (domain or "").lower().split(".")[0]

    def is_noise(seg: str) -> bool:
        low = seg.lower().strip(" ,.()")
        if not low or _YEAR.fullmatch(low):
            return True
        if low in names or low.removeprefix("by ") in names:
            return True
        if site_label and site_label in low.replace(" ", ""):
            return True
        return any(w in low for w in _SITE_WORDS)

    segments = [s for s in _SEPARATORS.split(text) if s.strip()]
    kept = [s for s in segments if not is_noise(s)]
    title = kept[0] if kept else (segments[0] if segments else "")

    # Strip 'by Artist' / leading 'Artist,' 'Artist:' 'Artist.' (MoMA style)
    for name in sorted(names, key=len, reverse=True):
        title = re.sub(rf"(?i)[,\s]*\bby\s+{re.escape(name)}\b", "", title)
        title = re.sub(rf"(?i)^{re.escape(name)}\s*[,:.]\s*", "", title)
    # 'Title. Place, Date' (MoMA style): keep the part before the first '. '
    # when what follows contains the date
    parts = re.split(r"(?<!\bc)(?<!\bca)\.\s", title, maxsplit=1)   # not 'c. 1952'
    if len(parts) == 2 and _YEAR.search(parts[1]) and len(parts[0]) >= 3:
        title = parts[0]
    title = _YEAR.sub("", title).replace("*", "")
    title = re.sub(r"\(\s*\)", "", title)
    title = re.sub(r"\s{2,}", " ", title).strip(" ,.-–—:|")
    return (title or "Untitled"), date


def looks_like_junk(title: str, page_url: str = "", image_url: str = "") -> bool:
    """Merchandise, books, posters, and shop pages — not the artwork itself."""
    if _SHOP_URL.search(page_url or "") or _TITLE_ENDS_PRINT.search(title or ""):
        return True
    blob = " ".join(t.lower() for t in (title, page_url, image_url) if t)
    return any(re.search(rf"\b{re.escape(w)}\b", blob) for w in JUNK_WORDS)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def brave_image_search(query: str, count: int, key: str) -> list[dict]:
    params = {
        "q": query,
        "count": min(max(count, 1), 200),
        "safesearch": "strict",
        "spellcheck": "false",
    }
    headers = {"X-Subscription-Token": key, "Accept": "application/json"}
    resp = SESSION.get(API_URL, params=params, headers=headers, timeout=30)
    if resp.status_code == 401:
        sys.exit("Brave rejected the API key (HTTP 401).")
    if resp.status_code == 429:
        sys.exit("Brave rate limit or monthly credit reached (HTTP 429).")
    resp.raise_for_status()
    return resp.json().get("results", [])


def normalise_result(r: dict) -> dict:
    """Pull the fields we use out of a Brave image result, defensively."""
    props = r.get("properties") or {}
    page_url = r.get("url") or ""
    host = (r.get("meta_url") or {}).get("hostname") or urlparse(page_url).hostname or ""
    return {
        "page_title": r.get("title") or "",
        "page_url":   page_url,
        "domain":     host.lower().removeprefix("www."),
        "image_url":  props.get("url") or "",
        "width":      props.get("width") or 0,
        "height":     props.get("height") or 0,
    }


def download_candidate(url: str, referer: str) -> Image.Image | None:
    try:
        resp = SESSION.get(url, timeout=60, stream=True,
                           headers={"Referer": referer} if referer else None)
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "")
        if ctype and not ctype.startswith("image/"):
            log.info("  not an image (%s): %s", ctype, url)
            return None
        buf = io.BytesIO()
        for chunk in resp.iter_content(65536):
            buf.write(chunk)
            if buf.tell() > MAX_BYTES:
                log.info("  too large, skipped: %s", url)
                return None
        buf.seek(0)
        img = Image.open(buf)
        img.load()
        return img
    except Exception as exc:          # network, HTTP or decode errors
        log.info("  download failed: %s (%s)", url, exc)
        return None


def search_artist(name: str, query: str, args, key: str, allow: list[str],
                  ledger: dict) -> dict:
    query = query or f"{name} {args.query_suffix}".strip()
    slug = slugify(name)
    out_dir = INBOX_DIR / slug
    stats = {"artist": name, "results": 0, "seen": 0, "domain": 0, "junk": 0,
             "size": 0, "allowed": 0, "saved": 0, "domains": Counter(), "raw": []}

    queries = [f"{query} site:{site}" for site in args.site_list] or [query]
    results, seen_in_run = [], set()
    for i, q in enumerate(queries):
        if i:
            time.sleep(REQUEST_DELAY)
        log.info("Searching Brave: %r", q)
        for r in map(normalise_result, brave_image_search(q, args.count, key)):
            if r["image_url"] and r["image_url"] in seen_in_run:
                continue                      # same image from two queries
            seen_in_run.add(r["image_url"])
            results.append(r)
    stats["results"] = len(results)
    stats["searches"] = len(queries)
    stats["raw"] = results

    # Filter on what the search result already tells us, counting each reason
    seen_urls = set(ledger["urls"])
    candidates = []
    for r in results:
        stats["domains"][r["domain"] or "(none)"] += 1
        if not r["image_url"] or r["image_url"] in seen_urls:
            stats["seen"] += 1
            continue
        if not args.any_domain and not domain_allowed(r["domain"], allow):
            stats["domain"] += 1
            continue
        if looks_like_junk(r["page_title"], r["page_url"], r["image_url"]):
            stats["junk"] += 1
            continue
        w, h = r["width"], r["height"]
        if w and h and (max(w, h) < args.min_size or max(w, h) / min(w, h) > MAX_ASPECT):
            stats["size"] += 1
            continue
        candidates.append(r)
    stats["allowed"] = len(candidates)

    # Largest first, so duplicates resolve in favour of the bigger copy
    candidates.sort(key=lambda r: r["width"] * r["height"], reverse=True)

    if args.dry_run:
        for r in candidates[: args.keep]:
            log.info("  %5d×%-5d %-28s %s", r["width"], r["height"],
                     r["domain"][:28], r["page_title"][:70])
        return stats

    artist_hashes = [int(h) for h in ledger["hashes"].get(slug, [])]
    for r in candidates:
        if stats["saved"] >= args.keep:
            break
        time.sleep(REQUEST_DELAY)
        img = download_candidate(r["image_url"], r["page_url"])
        ledger["urls"].append(r["image_url"])
        if img is None:
            continue
        w, h = img.size
        if max(w, h) < args.min_size or max(w, h) / min(w, h) > MAX_ASPECT:
            continue
        hsh = dhash(img)
        if any(hamming(hsh, other) <= HASH_DISTANCE for other in artist_hashes):
            log.info("  duplicate skipped: %s", r["page_title"][:70])
            continue
        artist_hashes.append(hsh)

        title, date = guess_title_and_date(r["page_title"], name, r["domain"])
        stem = f"{slugify(title)}_{hsh:016x}"[:80]
        out_dir.mkdir(parents=True, exist_ok=True)
        img.convert("RGB").save(out_dir / f"{stem}.jpg", quality=95)
        sidecar = {
            "title": title,
            "artist": name,
            "date": date,
            "page_title": r["page_title"],
            "source_page_url": r["page_url"],
            "source_image_url": r["image_url"],
            "domain": r["domain"],
            "width": w,
            "height": h,
        }
        (out_dir / f"{stem}.json").write_text(
            json.dumps(sidecar, indent=2, ensure_ascii=False), encoding="utf-8")
        stats["saved"] += 1
        log.info("  saved %d×%d %s", w, h, stem)

    ledger["hashes"][slug] = [str(h) for h in artist_hashes]
    return stats


def write_review_page() -> Path:
    """A static contact sheet of everything waiting in the inbox."""
    sections = []
    for artist_dir in sorted(p for p in INBOX_DIR.iterdir() if p.is_dir()):
        cards = []
        for img_path in sorted(artist_dir.glob("*.jpg")):
            meta_path = img_path.with_suffix(".json")
            meta = {}
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            rel = f"{artist_dir.name}/{img_path.name}"
            cards.append(
                f'<figure><a href="{html.escape(rel)}"><img loading="lazy" '
                f'src="{html.escape(rel)}"></a><figcaption>'
                f'<b>{html.escape(meta.get("title", ""))}</b> '
                f'{html.escape(meta.get("date", ""))}<br>'
                f'{meta.get("width", "?")}×{meta.get("height", "?")} · '
                f'<a href="{html.escape(meta.get("source_page_url", ""))}">'
                f'{html.escape(meta.get("domain", ""))}</a><br>'
                f'<code>{html.escape(img_path.name)}</code></figcaption></figure>'
            )
        if cards:
            sections.append(f"<h2>{html.escape(artist_dir.name)} "
                            f"({len(cards)})</h2><div class=grid>{''.join(cards)}</div>")
    page = f"""<!doctype html><meta charset=utf-8><title>arty inbox</title>
<style>
body{{font:14px -apple-system,sans-serif;margin:24px;background:#111;color:#ddd}}
h2{{margin-top:32px}} a{{color:#9cf}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px}}
figure{{margin:0;background:#1c1c1c;padding:8px;border-radius:6px}}
img{{width:100%;height:220px;object-fit:contain;background:#000}}
figcaption{{margin-top:6px;line-height:1.4;word-break:break-word}}
code{{color:#888;font-size:11px}}
</style>
<h1>arty inbox</h1>
<p>Delete the files you don't want from the inbox folder (both the .jpg and its .json),
fix any titles or dates in the .json files, then run
<code>python3 fetch_brave.py accept</code>.</p>
{''.join(sections) or '<p>The inbox is empty.</p>'}
"""
    path = INBOX_DIR / "review.html"
    path.write_text(page, encoding="utf-8")
    return path


def run_search(args) -> None:
    if args.artist:
        artists = [(args.artist, args.query or "")]
    else:
        artists = parse_artists_file(Path(args.artists_file).expanduser())
    allow = [] if args.any_domain else load_allowlist(Path(args.allow_file).expanduser())
    args.site_list = (load_allowlist(Path(args.sites_file).expanduser())
                      if args.sites else [])
    off = [d for d in args.site_list if allow and not domain_allowed(d, allow)]
    if off:
        log.warning("Sites not on the allowlist (their results will be dropped): %s",
                    ", ".join(off))
    if args.site_list:
        n = len(artists) * len(args.site_list)
        log.info("Site search: %d artists × %d sites = %d searches",
                 len(artists), len(args.site_list), n)
    key = load_api_key()
    ledger = load_ledger()

    all_stats = []
    for i, (name, query) in enumerate(artists):
        if i:
            time.sleep(REQUEST_DELAY)
        try:
            all_stats.append(search_artist(name, query, args, key, allow, ledger))
        finally:
            if not args.dry_run:
                save_ledger(ledger)

    print()
    cols = [("Results", "results"), ("Seen", "seen"), ("Off-list", "domain"),
            ("Junk", "junk"), ("Size", "size"), ("Allowed", "allowed"),
            ("Saved", "saved")]
    print(f"{'Artist':<24}" + "".join(f"{h:>9}" for h, _ in cols))
    for s in all_stats:
        print(f"{s['artist'][:23]:<24}" + "".join(f"{s[k]:>9}" for _, k in cols))

    print(f"\nBrave searches used: {sum(s['searches'] for s in all_stats)}")

    if args.dry_run or args.show_domains:
        totals = Counter()
        for s in all_stats:
            totals.update(s["domains"])
        print(f"\nTop {args.show_domains or 30} source sites across all results "
              "(✓ = on the allowlist):")
        for dom, n in totals.most_common(args.show_domains or 30):
            mark = "✓" if args.any_domain or domain_allowed(dom, allow) else " "
            print(f"  {mark} {n:>4}  {dom}")

    if args.dry_run:
        dump = INBOX_DIR / ".dry_run_results.json"
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_text(json.dumps({s["artist"]: s["raw"] for s in all_stats},
                                   indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"\nRaw results saved to {dump}")
    elif INBOX_DIR.exists():
        print(f"\nReview page: {write_review_page()}")


# ---------------------------------------------------------------------------
# Accept
# ---------------------------------------------------------------------------

def run_accept(args) -> None:
    if not INBOX_DIR.exists():
        sys.exit(f"No inbox at {INBOX_DIR}")
    moved = 0
    for artist_dir in sorted(p for p in INBOX_DIR.iterdir() if p.is_dir()):
        for img_path in sorted(artist_dir.glob("*.jpg")):
            meta_path = img_path.with_suffix(".json")
            side = {}
            if meta_path.exists():
                try:
                    side = json.loads(meta_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    log.warning("Bad JSON, skipped (fix and re-run): %s (%s)",
                                meta_path, exc)
                    continue
            with Image.open(img_path) as img:
                w, h = img.size
            artist = side.get("artist") or artist_dir.name.replace("_", " ").title()
            meta = {
                "title":            side.get("title") or "Untitled",
                "artist":           artist,
                "date":             side.get("date") or "",
                "styles":           side.get("styles", []),
                "source":           "brave",
                "source_page_url":  side.get("source_page_url"),
                "source_image_url": side.get("source_image_url"),
                "domain":           side.get("domain"),
                "copyrighted":      side.get("copyrighted", True),
                "private":          True,
                "low_res":          max(w, h) < LOW_RES_PX,
            }
            dest_slug = slugify(artist)
            dest_img = OUTPUT_DIR / dest_slug / "image" / img_path.name
            dest_meta = OUTPUT_DIR / dest_slug / "meta" / f"{img_path.stem}.json"
            dest_img.parent.mkdir(parents=True, exist_ok=True)
            dest_meta.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(img_path), dest_img)
            dest_meta.write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                                 encoding="utf-8")
            meta_path.unlink(missing_ok=True)
            moved += 1
            log.info("Accepted %s / %s", dest_slug, img_path.stem)
        if artist_dir.exists() and not any(artist_dir.iterdir()):
            artist_dir.rmdir()
    write_review_page()
    print(f"\nAccepted {moved} works into {OUTPUT_DIR}")
    if moved:
        print(f"Next: python3 process_collection.py --input {OUTPUT_DIR}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="find candidates and download them to the inbox")
    who = s.add_mutually_exclusive_group(required=True)
    who.add_argument("--artist", metavar="NAME", help="one artist")
    who.add_argument("--artists-file", metavar="PATH",
                     help="one artist per line; 'Name | custom query' allowed")
    s.add_argument("--query", help="custom search query (with --artist)")
    s.add_argument("--query-suffix", default="painting",
                   help="appended to the artist name when no custom query (default: painting)")
    s.add_argument("--count", type=int, default=DEFAULT_COUNT,
                   help=f"results requested per artist, max 200 (default {DEFAULT_COUNT})")
    s.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                   help=f"max candidates saved per artist (default {DEFAULT_KEEP})")
    s.add_argument("--min-size", type=int, default=DEFAULT_MIN_PX,
                   help=f"minimum long edge in px (default {DEFAULT_MIN_PX})")
    s.add_argument("--allow-file", default=str(DEFAULT_ALLOW),
                   help="domain allowlist (default: brave_domains.txt in the repo)")
    s.add_argument("--sites", action="store_true",
                   help="search each artist on each site in the sites file "
                        "(one search per artist per site)")
    s.add_argument("--sites-file", default=str(DEFAULT_SITES),
                   help="sites for --sites (default: brave_search_sites.txt)")
    s.add_argument("--any-domain", action="store_true",
                   help="ignore the allowlist (not recommended)")
    s.add_argument("--dry-run", action="store_true",
                   help="list what would be downloaded and which sites results came "
                        "from; downloads nothing")
    s.add_argument("--show-domains", type=int, nargs="?", const=30, default=0,
                   metavar="N", help="print the top N source sites (default 30; "
                                     "always shown with --dry-run)")

    sub.add_parser("accept", help="move reviewed inbox candidates into ~/arty/brave")

    args = p.parse_args()
    if args.command == "search":
        run_search(args)
    else:
        run_accept(args)


if __name__ == "__main__":
    main()
