# Multi-date roof and tree evidence

Open **Compare imagery dates** in the map's analysed-scene panel. The workspace
at `/scenes/imagery?scene=SCENE_ID` discovers up to three independently dated
local Esri Wayback acquisitions and runs an isolated comparison. It does not
replace saved scene models, reviews, training labels, weights, or confidence bands.

## Evidence workflow

1. Resolve local archive changes and query acquisition metadata across the entire
   image extent. Reject mixed or missing acquisition metadata. Publication dates
   are not photo dates; repeated acquisitions and identical pixels are deduplicated.
2. Use the newest successfully downloaded dated image as the comparison reference.
   The original saved scene remains a separate view, since its pixels may come
   from another date/provider with a different alignment.
3. Fit bounded similarity registration using RootSIFT/RANSAC. Verify held-out
   matches, distributed support, scale, rotation and displacement. Reject poor
   alignment; it must not influence the composite or object agreement.
4. Combine only locally supported pixels with similar colour after brightness
   normalization. Retain the reference elsewhere. This is neither super-resolution
   nor inpainting: hidden roof detail cannot be recovered without observations.
5. Run the existing SAM detector independently on each accepted date for roofs,
   trees and solar arrays. Match objects one-to-one; flag ambiguous splits/merges.
   Agreement requires two distinct captures, at least 75% intersection-over-union
   and local registration support across at least 80% of each object's footprint.
6. Keep tree canopies separate by date. Growth, pruning, shadows and seasons can
   change their outlines. No temporal union or automatic accuracy increase.
7. Create bounded roof-boundary proposals from repeat-observed intersections.
   Preserve courtyard holes; reject topology changes, area differences over 8%
   or Hausdorff movement over 1 metre. Refit at most six roofs per comparison using
   the shared roof modelling pipeline and stable composite. These are candidates,
   not verified improvements. Inspect their outlines, ridges and 3D surfaces.

The UI shows each date, rejected alignments, object overlap and local support,
the combined image, and candidate roof surfaces. All measurements are evidence
checks, not calibrated accuracy intervals. Shared errors can persist across dates.

## CLI and API

```sh
scripts/ml/.venv/bin/python -m scripts.ml.scene_temporal --start SCENE_ID
scripts/ml/.venv/bin/python -m scripts.ml.scene_temporal --run RUN_ID
```

`POST /api/scenes/temporal` accepts `{ "scene": "SCENE_ID" }` with the existing
CRM write authorization and origin checks. `GET /api/scenes/temporal?run=RUN_ID`
returns progress/evidence; the `asset` parameter serves explicitly allowed images
and JSON snapshots. The detached worker serializes SAM inference with the shared
extraction lock. Repeated starts for the same source/code/day are idempotent.
Interrupted or failed runs can be restarted; completed runs remain available.
Artifacts are in `data/roof-review-loop/multidate/runs/RUN_ID/`; `worker.log` and
`worker.pid` accompany the report. Source changes mark results stale.

## Pilot, 25 September 2026

Run `5c7f888c1e1c231c`, scene `mooroolbark-50011498-17-118441-80414`:

- Accepted acquisitions: 6 November 2025 and 20 December 2023.
- Rejected acquisition: 28 September 2025, insufficient registration support.
- 133 reference objects: 81 roofs, 50 trees, 2 solar arrays.
- 7 consistent objects: 5 roofs and 2 trees; 126 require inspection.
- 4 bounded roof proposals refitted; one other consistent roof exceeded the
  boundary-change budget.
- Only 2.9% of pixels contributed to the composite; most pixels were retained.
- No evidence yet of improved measured accuracy, nor a suburb-wide rollout.

Tests cover registration failure, duplicates, changing canopies, courtyard
preservation, ambiguous shared roofs, bounded geometry, acquisition provenance,
idempotent starts and unchanged source/review files.

Sources: [Esri Wayback usage and dates](https://www.esri.com/arcgis-blog/products/arcgis-living-atlas/mapping/use-world-imagery-wayback),
[official Wayback metadata and local-change client](https://github.com/Esri/wayback-core).
