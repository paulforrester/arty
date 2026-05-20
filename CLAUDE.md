# arty — developer context

## What this project does

Downloads public-domain impressionist and post-impressionist paintings from the
Art Institute of Chicago (ARTIC) API and composites them into museum-style
framed 3840×2160 JPEG wallpapers for a 4K TV.

## Runtime

Always use `python3`. The system `python` is Python 2.7 and will fail on type
annotations.

```bash
python3 fetch_artic.py
python3 composite.py [--input DIR] [--output DIR] [--style walnut|oak]
python3 wood_texture.py [outdir]   # generates 4 PNG samples for visual check
```

## Dependencies

```
pip install requests Pillow numpy
```

Pillow 10+ is required (`ImageFont.load_default(size=…)` and `textlength`).

## File map

```
fetch_artic.py    Download artwork + metadata from the ARTIC API
wood_texture.py   Procedural wood-grain texture module (importable)
composite.py      Composite artwork into framed 4K JPEGs (imports wood_texture)
```

Data lives **outside the repo** at `/Users/paulf/arty/`:

```
/Users/paulf/arty/
├── artic/          fetch_artic.py writes here
│   └── {artist}/
│       ├── image/{stem}.jpg
│       └── meta/{stem}.json
└── processed/      composite.py writes here
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

**composite.py** — layout constants (pixels at 4K):
```python
TV_W, TV_H  = 3840, 2160
FRAME_W     = 100       # frame rail width
MAT_W       = 72        # mat border around artwork
BORDER_MIN  = 80        # minimum black gap outside frame
BG_COLOR    = (17, 17, 17)
MAT_COLOR   = (240, 234, 220)
```

## Extension points

**Add a new wood style** — add an entry to `wood_texture.STYLES`:
```python
STYLES = {
    "walnut": { ... },
    "oak":    { ... },
    "ebony":  {           # new style
        "palette":     [...],   # list of (R,G,B) tuples, dark → light
        "ring_freq":   12,      # higher = tighter ring spacing
        "distortion":  2.0,     # warp amplitude applied to ring phase
        "fine_weight": 0.18,    # blend fraction for fine-grain layer
    },
}
```
`composite.py` picks up new styles automatically via `choices=list(wood_texture.STYLES)`.

**Change frame geometry** — edit `FRAME_W`, `MAT_W`, or `BORDER_MIN` in
`composite.py`. Everything else is derived from those values.

**Change which artworks are fetched** — edit the `styles` list in
`collect_artworks()` in `fetch_artic.py`. The values are Elasticsearch
`match` queries against the ARTIC `style_titles` field.

## Architecture notes

**Wood texture pipeline** (`wood_texture.generate`):
1. 5-octave FBM at large scale → warp field (bends ring bands)
2. 3-octave FBM at fine scale → grain streaks
3. `sin(primary * ring_freq * π + warp * distortion * π)` → ring pattern
4. `primary` is Y for horizontal grain, X for vertical grain (rings stack
   perpendicular to fibre direction)
5. Palette interpolation maps [0,1] texture value through the style's colour stops

**Frame corner miters** (`composite._draw_frame`):
Each of the four rails is defined as a trapezoid polygon. The diagonals from
outer corner to inner corner produce exact 45° miter joints. Each rail gets
its own independently-seeded wood texture so grain never tiles visually across
corners. The molding brightness profile is reversed for bottom and right rails
so the outer (bright) edge always faces away from the artwork.

**Seeding** — `_seed_from_name(stem)` = `md5(filename)[:8]` as a 31-bit int.
Same artwork always gets the same frame texture across re-runs.

**Mat shadow** — Gaussian blur of a filled rectangle at the artwork boundary,
clipped to mat-only pixels via `ImageChops.multiply`. Simulates the artwork
sitting slightly above the mat surface.

## There is no test suite

Use `python3 wood_texture.py /tmp/samples` to visually inspect texture changes.
Use a single-image invocation of `composite.composite()` to check layout changes
before running the full batch.
