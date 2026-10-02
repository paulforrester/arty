@~/development/dev-practices/branching.md
@~/development/dev-practices/doc-conventions.md

# arty — developer context

**Version:** 1.2 — Last updated 2026-10-02

## What this project does

Downloads public-domain impressionist and post-impressionist paintings from the
Art Institute of Chicago (ARTIC) API and composites them into museum-style
framed 3840×2160 JPEG wallpapers for a 4K TV.

## Runtime

Always use `python3`. The system `python` is Python 2.7 and will fail on type
annotations.

```bash
python3 fetch_artic.py                                           # style mode (40 works)
python3 fetch_artic.py --artist 'Claude Monet' --limit 30       # artist mode
python3 fetch_artic.py --artists-file artists.txt --limit 20    # multi-artist

python3 process_collection.py [--input DIR] [--output DIR] \
        [--override-frame STYLE] [--override-mat CONFIG] \
        [--no-mat] [--workers N] [--force]                       # process (parallel)
python3 wood_texture.py [outdir]   # save 4 PNG samples for visual inspection
```

## Dependencies

```
pip install requests Pillow numpy scikit-learn
```

Pillow 10+ is required (`ImageFont.load_default(size=…)` and `textlength`).

## File map

```
fetch_artic.py          Download artwork + metadata from the ARTIC API
fetch_brave.py          Brave image search → ~/arty/inbox (review) → accept → ~/arty/brave
brave_domains.txt       Domain allowlist for fetch_brave.py (one per line, subdomains match)
brave_search_sites.txt  Sites queried one by one by fetch_brave.py --sites (must be on the allowlist)
modern_artists.txt      Artist list for fetch_brave.py ('Name' or 'Name | query')
wood_texture.py         Procedural wood-grain texture module (importable)
styles.py               Frame and mat style catalog — FRAME_STYLES and MAT_CONFIGS dicts
style_selector.py       Auto-selects frame/mat from painting_analysis output + metadata
frame_compositor.py     Core compositing module — PIL Image in, PIL Image out
process_collection.py   CLI runner that walks artic/ and calls frame_compositor
painting_analysis.py    Perceptual colour analysis — PIL Image in, dict out
composite.py            Legacy standalone processor (superseded; kept for reference)
```

Data lives **outside the repo** at `~/arty/`:

```
~/arty/
├── artic/          fetch_artic.py writes here
│   └── {artist}/
│       ├── image/{stem}.jpg
│       └── meta/{stem}.json
└── processed/      process_collection.py writes here
    └── {artist}/{stem}.jpg
```

## Key constants

**fetch_artic.py** — two modes:

*Style mode* (default, no `--artist` arg) — edit these constants directly:
```python
OUTPUT_DIR    = Path("~/arty/artic")
TARGET_COUNT  = 40          # how many works to download
REQUEST_DELAY = 1.0         # polite delay between HTTP requests (seconds)
```
The styles queried are hardcoded in `collect_artworks()`:
`["Impressionism", "Post-Impressionism"]`

*Artist mode* (`--artist NAME` or `--artists-file PATH`) — CLI flags:
```
--artist NAME        fetch works by a single named artist
--artists-file PATH  fetch works for each artist listed in a text file (one per line, # comments ok)
--limit N            max works to fetch per artist (default: 25)
--style STYLE        additional style filter, e.g. 'Impressionism' (artist mode only)
```
Query strategy: exact match (`term[artist_title.keyword]`) when ≥ 5 results exist, otherwise
`match_phrase[artist_title]` (phrase order required — avoids OR semantics of bare `match`).
Python post-filter: `all(w in artist_title for w in query_words)` catches residual noise.
Prints a summary table (Artist / Downloaded / Skipped) on completion.

**painting_analysis.py** — tuning constants:
```python
_MAX_PIXELS = 40_000    # downsample ceiling before all analysis
_K          = 12        # k-means cluster count for accent colour extraction
```
Accent colour thresholds are inline in `analyse()`: L\* 15–88 (mid-tone),
C\* > 20 (saturated), cluster size 1–25% (present but not dominant).

**styles.py** — style catalogs (extension points; see below):
```python
# FRAME_STYLES keys: walnut, oak, gilded, silver_leaf, painted_black, painted_cream
# MAT_CONFIGS  keys: single_neutral, single_warm, double_neutral, double_accent
```
`gilded`, `silver_leaf`, `painted_black`, and `painted_cream` are defined in
`FRAME_STYLES` but fall back to walnut rendering until added to `wood_texture.STYLES`.

**style_selector.py** — selection rules (evaluated in priority order):
1. `post-impressioni` in styles → `oak` (or `painted_black` if temp < −0.1); `double_neutral`
2. `impressioni` in styles → `gilded` (or `oak` if temp < 0); `double_accent`
3. temp > 0.3 → `gilded` (> 0.55) or `walnut`; `single_warm`
4. temp < −0.3 → `painted_black` (< −0.55) or `silver_leaf`
5. brightness < 0.35 → override dark frames → `painted_cream` (< 0.2) or `oak`
6. contrast > 0.6 → force `single_neutral` mat
7. Default → `walnut`, `single_neutral`
8. `mat`: `False` when `edge_brightness < 0.25` (very dark edges — mat would add unwanted lightness)

`mat_accent_color`: first (highest-C\*) accent from analysis, +20 pp lightness,
−10 pp saturation in HLS. Populated only for `double_accent`; `None` otherwise.

**frame_compositor.py** — layout constants (pixels at 4K):
```python
TV_W, TV_H = 3840, 2160
FRAME_W    = 160        # frame rail width
MAT_W      = 70         # mat border around artwork
LINER_W    = 18         # inner liner width for double-mat configs
BG_COLOR   = (17, 17, 17)
MAT_COLOR  = (240, 234, 220)
```
There is no `BORDER_MIN`; frame outer edges fill the canvas flush on the
constraining axis (fill-to-edge sizing — see architecture notes below).

Note: `composite.py` uses `MAT_W = 72`; `frame_compositor.py` uses `MAT_W = 70`.
They are independent implementations.

**fetch_brave.py** — two subcommands:
```
python3 fetch_brave.py search --artist NAME | --artists-file PATH [--sites] [--dry-run] [--keep N]
python3 fetch_brave.py accept
```
API key from `BRAVE_API_KEY` or `~/.config/arty/brave_api_key` — never in the repo
(it's public). Candidates go to `~/arty/inbox/{artist}/{stem}.jpg|.json` plus
`inbox/review.html`; the user deletes rejects and edits sidecars, then `accept`
moves the rest to `~/arty/brave/{artist}/image|meta` with `source: "brave"`,
`copyrighted: true`, `private: true`, `low_res` (long edge < 1500 px).
`inbox/.seen.json` records every fetched image URL and per-artist dHashes so
rejected candidates never return.
`--dry-run` also prints per-filter rejection counts (Seen / Off-list / Junk /
Size), the top source domains, and writes raw results to
`inbox/.dry_run_results.json` — use these to tune `brave_domains.txt`.
`--sites` runs `<query> site:<site>` for every site in `brave_search_sites.txt`
and merges results (deduped by image URL); plain searches return mostly shops.
`looks_like_junk()` also rejects shop hosts/paths and titles ending in "Print". Title/date come from `guess_title_and_date()`,
a best-effort parse of the page title. Imports `slugify` from `fetch_artic`.
Never commit fetched images — they're copyrighted (`.gitignore` covers jpg/json).

## Extension points

**Add a new frame style** — two steps:

1. Add a rendering entry to `wood_texture.STYLES`:
```python
"ebony": {
    "palette":    [...],   # list of (R,G,B) tuples, dark → light
    "amp_fine":   0.12,    # amplitude of pore-scale FBM layer
    "amp_mid":    0.20,    # amplitude of ring/growth-line FBM layer (dominant)
    "amp_broad":  0.10,    # amplitude of slow cross-rail figure layer
}
```

2. Add a catalog entry to `styles.FRAME_STYLES`:
```python
"ebony": {"wood_style": "ebony", "warmth": "cool", "tone": "dark"}
```

The style is then available via `--override-frame ebony`. To make
`style_selector` recommend it automatically, add a rule in `select()`.

**Change frame geometry** — edit `FRAME_W` or `MAT_W` in
`frame_compositor.py`. Everything else is derived from those values.

**Change which artworks are fetched** — edit the `styles` list in
`collect_artworks()` in `fetch_artic.py`. The values are Elasticsearch
`match` queries against the ARTIC `style_titles` field.

## Architecture notes

**Pipeline per image** — three stages run inside each worker:
1. `painting_analysis.analyse(artwork)` → perceptual colour dict (`palette_temperature`, `accent_colors`, `brightness`, `contrast`, `edge_brightness`)
2. `style_selector.select(analysis, meta)` → `{frame_style, mat_config, mat_accent_color, mat}`
3. `frame_compositor.compose(artwork, meta, frame_style=…, mat_config=…, mat_accent_color=…, mat=…)`

`frame_compositor.py` is the pure image-processing core (PIL Image in, PIL
Image out; no file I/O, no CLI). `process_collection.py` is the CLI wrapper
that handles discovery, loading, saving, skipping, logging, and parallel dispatch.
`--override-frame` / `--override-mat` bypass stages 1–2 for that dimension.
`--no-mat` forces `mat=False` regardless of the auto-detected value.

**Parallel processing** (`process_collection`): images are processed using
`concurrent.futures.ProcessPoolExecutor` with `--workers N` parallel processes
(default: `os.cpu_count() - 1`). `process_one()` is the unit of work — it
returns a result dict and never raises; all exceptions are captured so the pool
cannot crash the main process. Workers call `gc.collect()` and delete large
objects (composed image, artwork, analysis) before returning to keep peak RSS
reasonable. macOS requires `multiprocessing.set_start_method("spawn")` before
starting the pool.

**Wood texture pipeline** (`wood_texture.generate`):
Three anisotropic FBM layers are summed, each with features elongated ~10:1 along
the grain direction (large `scale_par`) and fine across it (small `scale_perp`):
- Layer 1 fine   (~fw/50 perp, ~fw/5 par,   amp_fine=0.12)  — pore-scale texture
- Layer 2 medium (~fw/10 perp, ~fw par,     amp_mid=0.20)   — ring/growth-line variation
- Layer 3 broad  (~fw/2  perp, ~fw×4 par,  amp_broad=0.10) — slow figure across the rail

Layers are centred (mean → 0), weighted by amplitude, summed, and shifted back to 0.5.
Palette interpolation maps [0,1] through the style's colour stops.

**Frame corner miters** (`frame_compositor._draw_frame`):
Each of the four rails is defined as a trapezoid polygon. The diagonals from
outer corner to inner corner produce exact 45° miter joints. Each rail is
given its own wood texture at the correct grain direction. The molding
brightness profile is reversed for bottom and right rails so the outer
(bright) edge always faces away from the artwork.

**Info card / brass plaque** (`frame_compositor._info_card`):
Always rendered as an antique brass plaque, centered on the bottom frame rail,
10 px below the inner edge of the rail — visible regardless of mat mode.
- Background: `(180,145,60)` brass + ±6 numpy noise for brushed-metal texture
- Border: 1 px `(140,108,30)` dark brass outline
- Text: `(30,18,5)` near-black warm, Georgia font 28 px title / 20 px sub
- Title wraps to multiple lines (greedy word-wrap); font sizes step down
  in 2-point increments (min 14/12 px) until all lines fit `max_width - 32 px`
- `max_width` passed from `compose()` as `fow - 40` (frame outer width minus padding)

**Seeding** — `process_collection._seed(stem)` = `md5(stem)[:8]` as a 31-bit
int. Same filename always produces the same frame texture across re-runs.
`composite.py` uses an identical approach in `_seed_from_name`.

**Double-mat liner** (`frame_compositor.compose`): for `double_accent` and
`double_neutral` configs, an 18 px inner liner is drawn on the mat image
before the shadow pass. `double_accent` uses `mat_accent_color` (pre-processed
by `style_selector`); `double_neutral` uses `secondary_color` from
`styles.MAT_CONFIGS`. No liner is drawn if the colour is absent.

**Mat shadow** — Gaussian blur of a filled rectangle at the artwork boundary,
clipped to mat-only pixels via `ImageChops.multiply`. Simulates the artwork
sitting slightly above the mat surface. Applied after the liner so the shadow
falls naturally on both mat and liner.

**No-mat mode** (`compose(mat=False)`): mat is omitted entirely; the artwork
sits directly against the frame's inner edge. `_rabbet_shadow()` darkens the
outermost 12 px of the artwork (0.55× at the edge, linear ramp to 1.0× at
12 px depth) to simulate the rebate shadow. The brass plaque remains on the
bottom frame rail as in mat mode. `style_selector` sets `mat=False` automatically
when `edge_brightness < 0.25`; `--no-mat` forces it from the CLI.

## There is no test suite

Use `python3 wood_texture.py /tmp/samples` to visually inspect texture changes.
Use a single-image call to `frame_compositor.compose()` to check layout
changes before running the full batch:

```python
from PIL import Image
import json, frame_compositor
img  = Image.open("~/arty/artic/claude_monet/image/water_lilies_16568.jpg")
meta = json.loads(open("~/arty/artic/claude_monet/meta/water_lilies_16568.json").read())
frame_compositor.compose(img, meta).save("/tmp/test.jpg", quality=95)
```

## Version History

| Version | Date | Changes |
|---|---|---|
| 1.0 | 2026-09-10 | Initial version tracking for this file; added `~/development/dev-practices/branching.md` and `doc-conventions.md` imports so this project follows the same branching/merge and living-doc discipline as the rest of the ecosystem. |
| 1.1 | 2026-10-02 | Added `fetch_brave.py` (Brave image search → inbox review → accept into `~/arty/brave`), `brave_domains.txt` and `modern_artists.txt`; dry-run diagnostics (per-filter counts, top source domains, raw-results dump); MoMA-style and circa title parsing. |
| 1.2 | 2026-10-02 | `fetch_brave.py --sites` (per-site `site:` searches from `brave_search_sites.txt`); shop-page and print/poster/book junk filtering; auction catalogue and dealer domains added to the allowlist. |
