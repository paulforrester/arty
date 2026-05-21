# arty

Fetches public-domain impressionist and post-impressionist paintings from the
Art Institute of Chicago and composites them into museum-style framed
presentations sized for a 4K TV (3840×2160).

---

## Project layout

```
arty/
├── fetch_artic.py          # Download artwork + metadata from the ARTIC API
├── wood_texture.py         # Procedural wood-grain texture module
├── styles.py               # Frame and mat style catalog (FRAME_STYLES, MAT_CONFIGS)
├── style_selector.py       # Auto-selects frame/mat style from painting analysis + metadata
├── frame_compositor.py     # Core compositing module (PIL Image in → PIL Image out)
├── process_collection.py   # CLI runner: walks artic/, calls frame_compositor, saves output
├── painting_analysis.py    # Perceptual colour analysis (temperature, accent colours, brightness, contrast)
└── composite.py            # Legacy standalone processor (superseded by the two above)

/Users/paulf/arty/
├── artic/              # Raw downloads
│   └── {artist}/
│       ├── image/      # Full-size JPEGs from IIIF
│       └── meta/       # Companion JSON metadata
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

Downloads public-domain impressionist and post-impressionist works from the
[Art Institute of Chicago open-access API](https://api.artic.edu/docs).

```bash
python3 fetch_artic.py
```

The script has no CLI arguments. Edit the constants at the top of the file to
change behaviour:

| Constant | Default | Description |
|----------|---------|-------------|
| `OUTPUT_DIR` | `/Users/paulf/arty/artic` | Download destination |
| `TARGET_COUNT` | `40` | Number of works to fetch |
| `REQUEST_DELAY` | `1.0` | Seconds between HTTP requests |

Images and metadata land under `OUTPUT_DIR` mirrored by artist name.
Re-runs are idempotent — existing files are skipped.

### 2 — Generate framed presentations

```bash
python3 process_collection.py [--input DIR] [--output DIR] \
        [--override-frame STYLE] [--override-mat CONFIG] [--force]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--input`  / `-i`    | `/Users/paulf/arty/artic`     | Root of downloaded artwork |
| `--output` / `-o`    | `/Users/paulf/arty/processed` | Output directory |
| `--override-frame`   | auto | Force a specific frame style (key from `styles.FRAME_STYLES`) |
| `--override-mat`     | auto | Force a specific mat config (key from `styles.MAT_CONFIGS`) |
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
  "mat_accent_color": [223, 197, 186]
}
```

`frame_style` is a key from `styles.FRAME_STYLES`; `mat_config` is a key from
`styles.MAT_CONFIGS`. `mat_accent_color` is the most vivid accent colour from
the painting, lightened 35% and desaturated 20% for mat use, or `null` if the
painting has no suitable accent or the mat config doesn't use one.

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
│  │  wood frame rail  (100 px, mitered 45° corners)   │  │
│  │  ┌─────────────────────────────────────────────┐  │  │
│  │  │  warm off-white mat  (70 px)                │  │  │
│  │  │  ┌───────────────────────────────────────┐  │  │  │
│  │  │  │                                       │  │  │  │
│  │  │  │             artwork                   │  │  │  │
│  │  │  │                                       │  │  │  │
│  │  │  └───────────────────────────────────────┘  │  │  │
│  │  │                          ┌────────────────┐  │  │  │
│  │  │                          │ Title          │  │  │  │
│  │  │                          │ Artist · Date  │  │  │  │
│  │  │                          └────────────────┘  │  │  │
│  │  └─────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

**Wood texture** — `wood_texture.py`

Grain is synthesised from two layered fractional Brownian motion passes, each
built from bilinearly-interpolated value noise: a 5-octave large-scale warp
that bends the annual-ring bands, and a 3-octave fine-grain layer that adds
fibre streaks. Ring bands run perpendicular to the grain direction so
horizontal rails show horizontal banding and vertical rails show vertical
banding. Two styles are supported:

- **walnut** — dark heartwood palette, tight rings, low fibre weight
- **oak** — golden-tan palette, wider rings, more visible fibre

**Molding profile** — simulated with a 1-D brightness curve applied across
each rail's cross-section: sharp outer highlight → broad bevel gradient →
shadow trough → thin inner bead. The profile is mirrored for the bottom and
right rails so the bright edge always faces outward.

**Mat shadow** — a Gaussian-blurred dark halo at the artwork boundary,
clipped to the mat area, simulates the artwork sitting in a shallow rebate.

**Info card** — Georgia serif, warm cream background, positioned on the
lower-right mat below the artwork.

---

## Data source

Artwork sourced from the
[Art Institute of Chicago](https://www.artic.edu/) via their
[open-access API](https://api.artic.edu/docs) and
[IIIF image service](https://iiif.io/).  All works are in the public domain.
Metadata and images are provided under
[CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/).
