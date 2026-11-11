# Flat roofs, attached units and image evidence

Follow-up: [property-block grouping and straight-edge comparisons](roof-property-blocks-2026-09-25.md)
now include both detected rows on PS823073 and bounded boundary-refitting hypotheses.

The self-review search previously compared pitched roofs only. The new review
layers separate three problems: roof family, allocation between attached units,
and image quality. They do not change the frozen Mooroolbark predictions, saved
Cambridge Road models or human feedback.

## Roof family

Every eligible roof now gets an independently audited flat-surface hypothesis,
including cases with too few detected lines for pitched fitting. It preserves
the complete outline and courtyard holes, and has no invented apex, hips or
valleys. A no-fold surface is no longer flagged for unsupported fold length.
The UI offers **Flat / low-pitch hypothesis** alongside the pitched comparison,
including the 3D view.

The preview is level: it does not determine drainage falls, shallow slope,
parapets or separate height levels. The family remains unresolved and its
candidate cannot pass simply because it has fewer lines. The low-level flat
geometry audit already worked; the missing comparison and fold-specific scoring
were the actual blockers. Pitched-roof gates remain unchanged.

Saved family feedback currently contains 22 hipped and 20 gabled observations,
and no flat observations. These counts do not imply training approval. Reviewed
flat/low-pitch and mixed-level examples are needed before claiming a learned
classifier improvement. Use **Edit and save feedback** and the source roof's
roof-family controls to provide that evidence. No weights were retrained here.

## Continuous roofs over multiple units

`roof_unit_patterns.py` adds roof-scoped air-conditioner and exhaust proposals
using the existing SAM text model, plus an explicitly unclassified contrast-island
fallback. Model solar and tree regions are excluded; predictions from different
prompts are deduplicated. No manual object coordinates are embedded in inference.

A regular row of at least three similar fixtures can suggest perpendicular
partitions at the midpoints between fixtures. Separate parallel equipment rows
do not double the count. Irregular/sparse layouts, courtyard-crossing cuts,
small remnants and disconnected cuts abstain. Partition areas preserve the full
continuous footprint without overlaps or gaps. The original surface and roof
lines are not split. The partitions live in experiment metadata and a separate
purple dashed review overlay.

Repeated long transverse image seams supply an additional pattern when equipment
is missing. Duplicate edge fragments cannot inflate coverage; seams need regular
spacing and plausible end sections. Solar/tree overlaps are excluded from this
cue. These divisions may be roof steps or material joints, rather than units.

These are possible sections, not established dwelling counts or party walls.
Industrial equipment can repeat on one building. Midpoint placement assumes a
repeated layout; the displayed ±25% spacing range is a heuristic, not a measured
confidence interval. Entrances, address points and subdivision plans are needed
to corroborate actual unit count and boundary placement. There is no automatic
unit address assignment or parcel split.

The seam-based overlay displays a 0.6 m grouping tolerance instead of the
fixture-spacing range. This too is a heuristic, not a calibrated interval.

The two-roof semantic pilot used frozen batch `f54ba9de16abf993`, indices 0 and
9. It found one and three equipment candidates respectively. These are model
observations, not verified equipment labels. Equipment model hashes, image
identities and coordinates are stored under `data/roof-review-loop/equipment`.
They are fingerprinted and copied into subsequent frozen comparison runs.

```sh
scripts/ml/.venv/bin/python -m scripts.ml.roof_unit_patterns --run f54ba9de16abf993 --objects 0 9
scripts/ml/.venv/bin/python -m scripts.ml.roof_review_loop --start --compare f54ba9de16abf993
```

## Improving grainy imagery

The existing enhancement already combines bilateral denoising and local contrast
(CLAHE) at unchanged pixel registration. The new image-evidence panel reports
pixel scale, provider and acquisition-date availability. Texture measurements
are diagnostics, not accuracy estimates. Reviewed Google captures have about
24 cm ground pixels and no recorded acquisition date.

`roof_image_evidence.fuse_registered` implements conservative native-grid
multi-capture fusion. It requires 3–8 distinct acquisitions, known dates within
90 days, and a common orthorectified frame. It rejects duplicate pixels and
acquisition IDs, poor alignment and excessive image change. After a bounded
subpixel translation fit, stable pixels are averaged; changed/occluded regions
retain the reference image. It does not fill hidden roof regions or generate
higher-resolution geometry. Synthetic tests demonstrate reduced independent
noise, not real-roof accuracy or recovered detail.

No real Mooroolbark images were fused: acquisition dates and independent imagery
have not been established. Multiple zoom levels or neighbouring tiles from one
mosaic do not count as independent views. Fetching another provider also does
not prove a different acquisition.

A manifest can be supplied once independently sourced captures are available:

```json
{"captures": [
  {"image_path": "flight-a.png", "acquisition_id": "flight-a", "acquisition_date": "2026-09-01", "frame_id": "registered-roof-grid"},
  {"image_path": "flight-b.png", "acquisition_id": "flight-b", "acquisition_date": "2026-09-02", "frame_id": "registered-roof-grid"},
  {"image_path": "flight-c.png", "acquisition_id": "flight-c", "acquisition_date": "2026-09-03", "frame_id": "registered-roof-grid"}
]}
```

```sh
scripts/ml/.venv/bin/python -m scripts.ml.roof_image_evidence --manifest /path/to/captures.json
```

Inputs must actually be registered; matching `frame_id` strings alone are not
registration. Output and a provenance report are saved separately under
`data/roof-review-loop/imagery`. Source images remain untouched.

## Additional evidence to pursue

Multi-image super-resolution is a real technique: ESA's
[PROBA-V challenge](https://kelvins.esa.int/proba-v-super-resolution/)
evaluated fusion against separately captured high-resolution reference images.
That establishes a method, not its effectiveness for our roofs. A next experiment
should compare native higher-resolution imagery, denoising and registered fusion
against independently reviewed boundaries and roof lines, holding geography out.

Victoria provides access routes for
[high-resolution aerial imagery and LiDAR point clouds](https://www.land.vic.gov.au/maps-and-spatial/spatial-data/how-to-access-spatial-data)
through licensed providers. Exact Mooroolbark coverage, dates and access remain
to be checked. Building returns in LiDAR or a DSM could help distinguish levels
and low slopes; a bare-earth terrain model cannot supply roof elevations.

[3D Gaussian Splatting](https://repo-sam.inria.fr/fungraph/3d-gaussian-splatting/)
uses calibrated multi-view imagery and a sparse point-cloud initialization for
novel-view rendering. My recommendation is to prioritize registered aerial
captures and roof-height evidence first. Splatting a single grainy orthophoto
would not establish unseen eaves, party walls or measured roof heights.

## Paired check and current limits

The final run `fef1cc9ae04b43a3` uses the same 12 frozen roofs as
`f54ba9de16abf993`. All 12 flat alternatives passed geometry checks; all still
need independent family evidence. Seven pitched searches found no passing
alternative and five lacked evidence, matching the baseline outcome. None of
these 12 supplied a reliable regular fixture/seam pattern for proposed unit
partitions. No model replacements or confidence upgrades were applied.

I visually inspected the new flat preview and equipment overlays for cases 1,
4, 10 and 12. The white roof row has visible section changes but the equipment
detector found only one fixture there. The solar-heavy roof yielded three
candidate fixtures clustered locally, not one regular unit row. Contrast-only
candidates also include tile highlights on the orange roof. These failures
justify keeping the detections and partitions unverified; more pixels, roof
level segmentation and independent unit anchors are still needed.

Images are in the intermediate experiment's `family-inspection` directory:
`data/roof-review-loop/runs/3942f9184a89ebaf/family-inspection`. The later seam
strategy also abstained on these cases. Rotated synthetic layouts demonstrate
partition construction and exact area preservation; they do not demonstrate
unit accuracy on real houses.

92 targeted tests pass, covering flat geometry/courtyards, independent evidence
gates, fixture registration/deduplication, solar exclusion, partition coverage,
repeated seams, frozen equipment inputs and image fusion rejection conditions.
All 257 suburb models and four Cambridge Road model/review files match their
original checksums. Machine-readable results:
`data/roof-review-loop/flat-units-imagery-verification-2026-09-25.json`.

The production build and TypeScript check pass. The local API returns HTTP 200;
the browser comparison, equipment overlay, enhancement toggle and both 3D
surfaces were inspected with no browser console errors. The review is open at
`http://localhost:3008/scenes/processing?view=self-review`.

The build also exposed Tailwind scanning generated ML data for utility classes.
`globals.css` now limits discovery to `src`, where the application components
live. The standard Turbopack build then compiled in 6.4 seconds. Only the web
service was restarted; the geo and ML services were left running.
