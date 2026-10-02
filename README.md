# arty

Fetches public-domain impressionist and post-impressionist paintings from the
Art Institute of Chicago and composites them into museum-style framed
presentations sized for a 4K TV (3840×2160).

---

## Project layout

```
arty/
├── fetch_artic.py          # Download artwork + metadata from the ARTIC API
├── fetch_brave.py          # Find candidates via Brave image search, review, then accept
├── review_server.py        # Local review page for the inbox (fetch_brave.py review)
├── brave_domains.txt       # Domain allowlist for fetch_brave.py
├── brave_search_sites.txt  # Sites searched one by one with fetch_brave.py --sites
├── modern_artists.txt      # Artist list for fetch_brave.py
├── wood_texture.py         # Procedural wood-grain texture module
├── styles.py               # Frame and mat style catalog (FRAME_STYLES, MAT_CONFIGS)
├── style_selector.py       # Auto-selects frame/mat style from painting analysis + metadata
├── frame_compositor.py     # Core compositing module (PIL Image in → PIL Image out)
├── process_collection.py   # CLI runner: walks artic/, calls frame_compositor, saves output
├── painting_analysis.py    # Perceptual colour analysis (temperature, accent colours, brightness, contrast)
└── composite.py            # Legacy standalone processor (superseded by the two above)

~/arty/
├── artic/              # Raw downloads
│   └── {artist}/
│       ├── image/      # Full-size JPEGs from IIIF
│       └── meta/       # Companion JSON metadata
├── inbox/              # fetch_brave.py candidates awaiting review (+ review.html)
├── brave/              # Accepted Brave finds, same {artist}/image|meta layout
└── processed/          # Framed 4K output
    └── {artist}/
        └── {stem}.jpg
```

---

## Requirements

```
pip install requests Pillow numpy scikit-learn
```

Python 3.11+ is required (type-annotation syntax).

---

## Usage

### 1 — Fetch artwork

Downloads public-domain works from the
[Art Institute of Chicago open-access API](https://api.artic.edu/docs).

**Style mode** (default) — fetches Impressionist and Post-Impressionist works:

```bash
python3 fetch_artic.py
```

Edit the constants at the top of the file to change behaviour:

| Constant | Default | Description |
|----------|---------|-------------|
| `OUTPUT_DIR` | `~/arty/artic` | Download destination |
| `TARGET_COUNT` | `40` | Number of works to fetch |
| `REQUEST_DELAY` | `1.0` | Seconds between HTTP requests |

**Artist mode** — fetch works by specific artists:

```bash
python3 fetch_artic.py --artist 'Claude Monet' --limit 30
python3 fetch_artic.py --artists-file impressionists.txt --limit 20
python3 fetch_artic.py --artists-file favorites.txt --limit 25
python3 fetch_artic.py --artist 'Georges Seurat' --style 'Post-Impressionism'
```

| Flag | Default | Description |
|------|---------|-------------|
| `--artist NAME` | — | Fetch works by a single named artist |
| `--artists-file PATH` | — | Fetch works for each artist in a text file (one per line; `#` comments ok) |
| `--limit N` | `25` | Max works to fetch per artist |
| `--style STYLE` | — | Additional style filter (artist mode only) |

Images and metadata land under `OUTPUT_DIR` mirrored by artist name.
Re-runs are idempotent — existing files are skipped.

### 1b — Find copyrighted works with Brave image search (private use)

For artists whose work isn't public domain, `fetch_brave.py` searches the
[Brave Search API](https://api-dashboard.search.brave.com/) and only downloads
from domains in `brave_domains.txt` (museums, foundations, auction houses,
reputable galleries). Nothing reaches the collection without review.

These images are generally still under copyright. They are tagged
`"copyrighted": true, "private": true` in their metadata and are for private
display at home only — don't publish, share or commit them.

**API key** — a Brave Search plan includes $5 of free credit a month (about
1,000 searches; one search per artist). Keep the key out of this public repo:

```bash
export BRAVE_API_KEY=...            # or:
mkdir -p ~/.config/arty && echo '...' > ~/.config/arty/brave_api_key
```

**Workflow**

```bash
python3 fetch_brave.py search --artist 'Joan Miró' --dry-run    # preview, downloads nothing
python3 fetch_brave.py search --artists-file modern_artists.txt --sites   # candidates → ~/arty/inbox
python3 fetch_brave.py review                                    # keep / reject / edit in the browser
python3 process_collection.py --input ~/arty/brave
```

**Review page** — `fetch_brave.py review` opens the inbox in your browser
(served from this Mac only, at `http://127.0.0.1:8765`). Each candidate has
**Keep** and **Reject** buttons and editable title and date fields that save as
soon as you leave them. **Reject the rest** rejects everything in an artist not
marked Keep, and **Accept kept** moves the kept works into `~/arty/brave`.
Rejected files go to `~/arty/inbox/.rejected/` (Undo brings them back); they
are never re-downloaded, so that folder can be emptied any time. Press Ctrl-C in
the terminal when done.

Without the review page: delete unwanted `.jpg` + `.json` pairs from
`~/arty/inbox/{artist}/`, fix titles in the `.json` files, then run
`python3 fetch_brave.py accept` (or `accept --kept-only` to take only works
marked Keep).

| Flag (search) | Default | Description |
|------|---------|-------------|
| `--artist NAME` / `--artists-file PATH` | — | One artist, or a file of `Name` or `Name \| custom query` lines |
| `--query Q` | — | Custom search query (with `--artist`) |
| `--query-suffix S` | `painting` | Appended to the artist name when there's no custom query |
| `--count N` | `100` | Results requested per artist (API max 200) |
| `--keep N` | `40` | Max candidates saved per artist |
| `--min-size PX` | `800` | Minimum long edge |
| `--allow-file PATH` | `brave_domains.txt` | Domain allowlist |
| `--sites` | off | Search each artist on each site in the sites file (`<query> site:<site>`) — recommended |
| `--sites-file PATH` | `brave_search_sites.txt` | Sites for `--sites` |
| `--any-domain` | off | Ignore the allowlist (not recommended) |
| `--dry-run` | off | List what would be downloaded, the top source sites, and save raw results to `~/arty/inbox/.dry_run_results.json` |
| `--show-domains [N]` | off (30) | Print the top N source sites on a real run too |

The summary table shows how many results each filter removed (Seen, Off-list,
Junk, Size). If Off-list is high, the top-sites list shows which sites to
consider adding to `brave_domains.txt`.

**Why `--sites`:** a plain image search for an artist returns mostly shops
(eBay, Amazon, poster sites), so almost nothing passes the allowlist. Asking
each museum or auction site directly returns their collection images instead.
It costs one search per artist per site — 12 × 12 = 144 searches, about $0.72
of the free monthly credit.

Filtering: allowlisted domain → no merchandise words (mug, t-shirt, puzzle…)
and no shop pages (`store.` / `shop.` hosts, `/shop/` paths, titles ending in
"Print") → size and aspect checks → near-duplicate removal (dHash), keeping the largest
copy. `~/arty/inbox/.seen.json` remembers every image URL already fetched, so
candidates you delete don't come back on the next search. Accepted works with a
long edge under 1500 px get `"low_res": true`.

### 2 — Generate framed presentations

```bash
python3 process_collection.py [--input DIR] [--output DIR] \
        [--override-frame STYLE] [--override-mat CONFIG] [--force]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--input`  / `-i`    | `~/arty/artic`     | Root of downloaded artwork |
| `--output` / `-o`    | `~/arty/processed` | Output directory |
| `--override-frame`   | auto | Force a specific frame style (key from `styles.FRAME_STYLES`) |
| `--override-mat`     | auto | Force a specific mat config (key from `styles.MAT_CONFIGS`) |
| `--no-mat`           | off  | Omit the mat; artwork sits directly against the frame |
| `--workers N`        | `cpu_count - 1` | Number of parallel worker processes |
| `--force`  / `-f`    | off  | Re-process existing outputs |

Each image is analysed for colour temperature, brightness, and contrast;
`style_selector` then picks a frame style and mat configuration automatically.
Pass `--override-frame` or `--override-mat` to force specific choices.
Existing outputs are skipped unless `--force` is passed. Processing all 40
images takes roughly 25 seconds on Apple Silicon.

`composite.py` is an earlier standalone processor with the same CLI surface;
it is superseded by `frame_compositor.py` + `process_collection.py`.

### 3 — Analyse a painting

```bash
python3 painting_analysis.py <image_path>
```

Prints a JSON dict of perceptual colour properties:

| Key | Range | Description |
|-----|-------|-------------|
| `palette_temperature` | −1.0 … +1.0 | Hue balance: −1 = pure cool (cyan/blue), +1 = pure warm (red/orange) |
| `accent_colors` | list of RGB tuples | Up to 3 vivid, mid-tone colours not dominant in the image |
| `brightness` | 0.0 … 1.0 | Perceptual average luminance |
| `contrast` | 0.0 … 1.0 | Standard deviation of luminance |
| `edge_brightness` | 0.0 … 1.0 | Mean luminance of the outermost 10 % border pixels |

Useful for picking mat colours and understanding a painting's mood before compositing.

### 4 — Inspect style selection

```bash
python3 style_selector.py <image_path> [meta.json]
```

Prints the frame and mat styles that would be chosen for an image:

```json
{
  "frame_style": "gilded",
  "mat_config": "double_accent",
  "mat_accent_color": [223, 197, 186],
  "mat": true
}
```

`frame_style` is a key from `styles.FRAME_STYLES`; `mat_config` is a key from
`styles.MAT_CONFIGS`. `mat_accent_color` is the most vivid accent colour from
the painting, lightened 20 pp and desaturated 10 pp (HLS) for mat use, or `null`
if the painting has no suitable accent or the mat config doesn't use one.
`mat` is `false` when the painting's edge region is very dark (`edge_brightness < 0.25`);
pass `--no-mat` to force it off regardless.

### 5 — Inspect wood textures

```bash
python3 wood_texture.py [outdir]
```

Saves four 400×400 PNG samples (walnut/oak × horizontal/vertical grain) to
`outdir` (default: current directory) for visual inspection.

---

## Frame design

```
┌─────────────────────────────────────────────────────────┐  ← near-black (#111111)
│  ┌───────────────────────────────────────────────────┐  │
│  │  wood frame rail  (160 px, mitered 45° corners)   │  │
│  │  ┌─────────────────────────────────────────────┐  │  │
│  │  │  warm off-white mat  (70 px)                │  │  │
│  │  │  ┌───────────────────────────────────────┐  │  │  │
│  │  │  │                                       │  │  │  │
│  │  │  │             artwork                   │  │  │  │
│  │  │  │                                       │  │  │  │
│  │  │  └───────────────────────────────────────┘  │  │  │
│  │  └─────────────────────────────────────────────┘  │  │
│  │          ┌───────────────────────┐                 │  │
│  │          │  Title                │ ← brass plaque  │  │
│  │          │  Artist · Date        │                 │  │
│  │          └───────────────────────┘                 │  │
│  └───────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

**Wood texture** — `wood_texture.py`

Grain is synthesised from three anisotropic fractional Brownian motion layers,
each built from bilinearly-interpolated value noise with independent x/y grid
scales so features are elongated ~10:1 along the grain direction.  The three
layers capture pore-scale texture, ring/growth-line variation, and slow
cross-rail figure.  Two styles are supported:

- **walnut** — dark heartwood palette, tighter rings
- **oak** — golden-tan palette, wider rings

**Molding profile** — simulated with a 1-D brightness curve applied across
each rail's cross-section: sharp outer highlight → broad bevel gradient →
shadow trough → thin inner bead. The profile is mirrored for the bottom and
right rails so the bright edge always faces outward.

**Mat shadow** — a Gaussian-blurred dark halo at the artwork boundary,
clipped to the mat area, simulates the artwork sitting in a shallow rebate.

**Brass plaque** — Georgia serif, antique brass background `(180,145,60)` with
subtle brushed-metal noise, 1 px dark brass border `(140,108,30)`. Centered on
the bottom frame rail, 10 px below the inner edge, visible in both mat and
no-mat modes. Title wraps to multiple lines; font sizes step down if needed to
fit within the frame width.

---

## Data source

Artwork sourced from the
[Art Institute of Chicago](https://www.artic.edu/) via their
[open-access API](https://api.artic.edu/docs) and
[IIIF image service](https://iiif.io/).  All works are in the public domain.
Metadata and images are provided under
[CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/).

Works found with `fetch_brave.py` come from the sites listed in their metadata
(`source_page_url`), are generally under copyright, and are for private use only.
