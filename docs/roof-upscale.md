# Optional Real-ESRGAN roof discovery

`scripts/ml/roof_upscale.py` runs **realesrgan-x4plus**, the general-photo model,
on a padded crop of an immutable source PNG. The result is a cached derivative
for discovering possible lines. All accepted lines must still be supported by
the original RGB, after projection into the original pixel frame. Generated
texture is not a measured eave, height, roof face or training label.

This uses the official portable ncnn release linked from
[Real-ESRGAN v0.2.5.0](https://github.com/xinntao/Real-ESRGAN/releases/tag/v0.2.5.0).
The [upstream portable CLI](https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan)
also includes anime models; the adapter explicitly selects `realesrgan-x4plus`
instead of relying on its default. No Torch packages or global dependencies
are installed.

## Install once, explicitly

From the repository root on macOS:

```sh
mkdir -p data/reviews/eyes/tools/realesrgan-20220424
curl --fail --location --proto '=https' --tlsv1.2 \
  --connect-timeout 20 --max-time 1800 --max-filesize 60000000 \
  --output data/reviews/eyes/tools/realesrgan-20220424/realesrgan-ncnn-vulkan-20220424-macos.zip \
  https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-macos.zip
scripts/ml/.venv/bin/python -m scripts.ml.roof_upscale \
  --install-archive data/reviews/eyes/tools/realesrgan-20220424/realesrgan-ncnn-vulkan-20220424-macos.zip
```

The downloaded artifact is 51,817,124 bytes. The installer requires SHA-256
`e0ad05580abfeb25f8d8fb55aaf7bedf552c375b5b4d9bd3c8d59764d2cc333a`.
This hash was computed from the named official download and pinned locally;
it is **not** a claim of an upstream-published checksum. The archive contains a
universal Mach-O executable with arm64 and x86_64 slices. Installation verifies
host compatibility before running it, checks safe archive paths, and records
binary/model hashes in `installation.json`. An existing installation directory
is retained; choose another `--install-dir` to install a separate copy.

For a separately installed compatible native runtime, set
`ADDR_REALESRGAN_BIN` and optionally `ADDR_REALESRGAN_MODELS`. That model
directory must contain `realesrgan-x4plus.bin` and `.param`. Explicit runtimes
are recorded with their actual hashes and do not inherit the pinned release's
identity. Inference never downloads or substitutes a different model.

## Run a bounded crop

```sh
scripts/ml/.venv/bin/python -m scripts.ml.roof_upscale \
  --input data/reviews/eyes/shadow-canopy-20260927/05-48e4ea59/input.json \
  --cache-dir data/reviews/eyes/shadow-canopy-20260927/05-48e4ea59/upscale
```

Alternatively use `--image source.png --ring-json ring.json`, where the ring is
an array of source-pixel `[x,y]` points. Defaults: 16 pixels of padding, at most
768 pixels per crop side, 120-second inference timeout, 128-pixel ncnn tiles,
one load/process/save thread each, and 4× output. Crops outside the bounds or
too large are rejected, not resampled. Padding is clipped at image edges and
reported. Source input is limited to 32 megapixels and 128 MiB.

The Python API is:

```python
from scripts.ml.roof_upscale import UpscaleError, upscale_roof_crop

result = upscale_roof_crop(image_path, ring_pixels, cache_dir=cache_dir)
```

Each cache entry contains the lossless RGB `input.png`, `upscaled.png`, bounded
`inference.log`, and `manifest.json`. Identity binds source bytes, crop, code,
model weights/parameters, executable, scale and inference settings. Reuse
verifies every artifact hash and exact 4× dimensions. Source files, model JSON,
reviews and source coordinates are never rewritten. Missing runtimes,
incompatible binaries, inference failure, timeout or corrupted cache return
`UpscaleError` (CLI: `status: unavailable`, nonzero exit). There is no
interpolation fallback presented as Real-ESRGAN.

## Request an MCP review

Call the `scene_roof_review` MCP tool with both options enabled:

```json
{
  "scene_id": "mooroolbark-50011498-17-118441-80415",
  "object_id": "48e4ea59f7d31cb5",
  "include_boundary_guidance": true,
  "boundary_upscale": true
}
```

This runs crop upscaling → an original-RGB-gated boundary candidate → a fresh
roof fit. The response includes a native image card comparing original RGB
and registered overlays, the candidate outline in source pixels, and derivative
paths and crop affines. Inspect the card alongside the acceptance diagnostics;
an ambiguous shadow or canopy comparison can remain unaccepted.

`boundary_upscale` requires `include_boundary_guidance=true`. Both default to
false. This is an optional, experimental review path: it writes derivative
sidecars and previews an unapplied candidate. It does not automatically switch
the production pipeline or change saved source geometry, reviews or training
eligibility.

## Coordinate contract and acceptance

All affines use `[x,y,1] @ matrix`, with a 3×2 matrix. `crop_box` is a half-open
source rectangle `[left,top,right,bottom]`. `source_to_upscaled` and
`upscaled_to_source` map continuous **pixel-edge** coordinates. For OpenCV LSD
pixel-center-index endpoints, use `upscaled_pixel_center_index_to_source`:
`x_source = left + (x_upscaled + 0.5) / 4 - 0.5`, likewise for y. Mixing these
conventions introduces a 0.375 source-pixel shift.

Boundary guidance may discover candidate segments in the native 4× output,
then apply this affine before checking original-RGB contrast, vegetation,
distance and mask support. Do not insert the output as a `layer:` evidence
view: that path can validate a layer against itself. Unavailable upscaling
must remain an explicit diagnostic even when original-RGB discovery continues.

## Local verification, 27 September 2026

Genuine native arm64 inference completed on an Apple M5 Max, without changing
the two scenes' source PNG or `latest.json` hashes. These two scene directories
had no saved `review.json` or `review.model.json`; neither was created:

| Frozen case | Crop | Output | Inference time |
| --- | --- | --- | --- |
| `05-48e4ea59` | 100×106 | 400×424 | 1.5453 s |
| `12-4c40eaee` | 63×75 | 252×300 | 0.4340 s |

Proof and matched-scale comparison images are in
`data/reviews/eyes/shadow-canopy-20260927/upscale-proof.json` and each case's
`upscale-result.json` / `upscale-comparison.png`. Visual inspection showed
sharper roof boundaries and newly synthesized seams, texture and foliage.
That appearance alone does not establish more accurate geometry or recover
the eaves hidden by trees. Paired boundary evaluation must decide whether
these additional proposals survive the independent original-RGB gates.

Focused adapter checks:

```sh
scripts/ml/.venv/bin/python -m pytest scripts/ml/tests/test_roof_upscale.py -q
scripts/ml/.venv/bin/python -m ruff check scripts/ml/roof_upscale.py scripts/ml/tests/test_roof_upscale.py
```

Unit-test inference is explicitly stubbed. The saved two-case proof above
records the separate real-model runs, with artifact and runtime hashes.

## SAM3 application experiment

`roof_upscale_eval.py` compares six cases declared before inference. Every arm
uses the same source crop with 64 pixels of context: native RGB, Lanczos 4×,
and genuine Real-ESRGAN x4plus. The native ROI is the paired baseline; the saved
full-scene mask only identifies the target and fixes the original-image evidence.

```sh
scripts/ml/.venv/bin/python -m scripts.ml.roof_upscale_eval \
  --dataset data/reviews/eyes/upscale-model-eval-20260927/dataset.json \
  --out data/reviews/eyes/upscale-model-eval-20260927/sam3 --seconds 1200
```

One persistent CPU SAM3 session uses `building roof`, score threshold 0.45 and
at most 80 proposals. SAM3 resizes every input to 1008×1008; the 4× arms change
image content and preprocessing, not encoder resolution. All masks decode
directly onto the same native ROI grid. The existing mask vectorizer creates
pixel-cell edge polygons, translated by the source crop origin without the LSD
half-pixel correction. Candidate matching uses fixed IoU ≥ 0.5, with all raw
detections and overlaps retained so missing, split or merged roofs remain visible.

Decoded masks pass through existing extraction, assembly and boundary cleanup
on full original RGB. Roof inference uses the original image/hash, metric frame,
the same saved eave prior, and a 2 m rise prior. A selected mask touching the ROI
edge is reported as cut and does not invent an eave there. Independent checks
use the fixed original-image line observations across arms; zero evidence stays
unavailable. Detector scores, Canny distance and physical audits are diagnostics,
not accuracy or a reason to promote a result automatically.

Per-arm artifacts preserve the input and model hashes, common-grid masks, raw
and fitted outlines, source-bound roof geometry, audits and registered cards.
Real-ESRGAN failure is explicitly unavailable, never a substituted SR result.
The bounded run writes only experiment artifacts; production defaults, saved
scene geometry, reviewer bindings and training labels remain unchanged.
