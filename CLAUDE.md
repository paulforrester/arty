# arty — developer context

## What this project does

Downloads public-domain impressionist and post-impressionist paintings from the
Art Institute of Chicago (ARTIC) API and composites them into museum-style
framed 3840×2160 JPEG wallpapers for a 4K TV.

## Runtime

Always use `python3`. The system `python` is Python 2.7 and will fail on type
annotations.

```bash
python3 fetch_artic.py                                           # download
python3 process_collection.py [--input DIR] [--output DIR] \
        [--override-frame STYLE] [--override-mat CONFIG] [--force]   # process
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
wood_texture.py         Procedural wood-grain texture module (importable)
styles.py               Frame and mat style catalog — FRAME_STYLES and MAT_CONFIGS dicts
style_selector.py       Auto-selects frame/mat from painting_analysis output + metadata
frame_compositor.py     Core compositing module — PIL Image in, PIL Image out
process_collection.py   CLI runner that walks artic/ and calls frame_compositor
painting_analysis.py    Perceptual colour analysis — PIL Image in, dict out
composite.py            Legacy standalone processor (superseded; kept for reference)
```

Data lives **outside the repo** at `/Users/paulf/arty/`:

```
/Users/paulf/arty/
├── artic/          fetch_artic.py writes here
│   └── {artist}/
│       ├── image/{stem}.jpg
│       └── meta/{stem}.json
└── processed/      process_collection.py writes here
    └── {artist}/{stem}.jpg
```

## Key constants

**fetch_artic.py** — no CLI args, edit these directly:
```python
OUTPUT_DIR    = Path("/Users/paulf/arty/artic")
TARGET_COUNT  = 40          # how many works to download
REQUEST_DELAY = 1.0         # polite delay between HTTP requests (seconds)
```
The styles queried are hardcoded in `collect_artworks()`:
`["Impressionism", "Post-Impressionism"]`

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

`mat_accent_color`: first (highest-C\*) accent from analysis, +35 pp lightness,
−20 pp saturation in HLS. Populated only for `double_accent`; `None` otherwise.

**frame_compositor.py** — layout constants (pixels at 4K):
```python
TV_W, TV_H = 3840, 2160
FRAME_W    = 100        # frame rail width
MAT_W      = 70         # mat border around artwork
BORDER_MIN = 80         # minimum black gap outside the frame
LINER_W    = 18         # inner liner width for double-mat configs
BG_COLOR   = (17, 17, 17)
MAT_COLOR  = (240, 234, 220)
```

Note: `composite.py` uses `MAT_W = 72`; `frame_compositor.py` uses `MAT_W = 70`.
They are independent implementations.

## Extension points

**Add a new frame style** — two steps:

1. Add a rendering entry to `wood_texture.STYLES`:
```python
"ebony": {
    "palette":     [...],   # list of (R,G,B) tuples, dark → light
    "ring_freq":   12,      # higher = tighter ring spacing
    "distortion":  2.0,     # warp amplitude applied to ring phase
    "fine_weight": 0.18,    # blend fraction for fine-grain layer
}
```

2. Add a catalog entry to `styles.FRAME_STYLES`:
```python
"ebony": {"wood_style": "ebony", "warmth": "cool", "tone": "dark"}
```

The style is then available via `--override-frame ebony`. To make
`style_selector` recommend it automatically, add a rule in `select()`.

**Change frame geometry** — edit `FRAME_W`, `MAT_W`, or `BORDER_MIN` in
`frame_compositor.py`. Everything else is derived from those values.

**Change which artworks are fetched** — edit the `styles` list in
`collect_artworks()` in `fetch_artic.py`. The values are Elasticsearch
`match` queries against the ARTIC `style_titles` field.

## Architecture notes

**Pipeline per image** — `process_collection` runs three stages in sequence:
1. `painting_analysis.analyse(artwork)` → perceptual colour dict
2. `style_selector.select(analysis, meta)` → `{frame_style, mat_config, mat_accent_color}`
3. `frame_compositor.compose(artwork, meta, frame_style=…, mat_config=…, mat_accent_color=…)`

`frame_compositor.py` is the pure image-processing core (PIL Image in, PIL
Image out; no file I/O, no CLI). `process_collection.py` is the thin CLI
wrapper that handles discovery, loading, saving, skipping, and logging.
`--override-frame` / `--override-mat` bypass stages 1–2 for that dimension.

**Wood texture pipeline** (`wood_texture.generate`):
1. 5-octave FBM at large scale → warp field (bends ring bands)
2. 3-octave FBM at fine scale → grain streaks
3. `sin(primary * ring_freq * π + warp * distortion * π)` → ring pattern
4. `primary` is Y for horizontal grain, X for vertical grain (rings stack
   perpendicular to fibre direction)
5. Palette interpolation maps [0,1] texture value through the style's colour stops

**Frame corner miters** (`frame_compositor._draw_frame`):
Each of the four rails is defined as a trapezoid polygon. The diagonals from
outer corner to inner corner produce exact 45° miter joints. Each rail is
given its own wood texture at the correct grain direction. The molding
brightness profile is reversed for bottom and right rails so the outer
(bright) edge always faces away from the artwork.

**Info card** (`frame_compositor._info_card`):
Rendered as a standalone PIL Image sized to fit its content (max 500 px wide).
Font: Georgia 30 px (title), 22 px (artist, date). Pasted 8 px inside the
lower-right corner of the mat; will overlap the artwork corner slightly for
typical three-line cards at these font sizes.

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

## There is no test suite

Use `python3 wood_texture.py /tmp/samples` to visually inspect texture changes.
Use a single-image call to `frame_compositor.compose()` to check layout
changes before running the full batch:

```python
from PIL import Image
import json, frame_compositor
img  = Image.open("/Users/paulf/arty/artic/claude_monet/image/water_lilies_16568.jpg")
meta = json.loads(open("/Users/paulf/arty/artic/claude_monet/meta/water_lilies_16568.json").read())
frame_compositor.compose(img, meta).save("/tmp/test.jpg", quality=95)
```
