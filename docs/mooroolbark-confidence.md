# Mooroolbark rollout and roof confidence

The suburb run applies the existing SAM3 and constrained roof inference pipeline
to the complete official Mooroolbark locality polygon. It does not retrain neural
weights or approve its own output. Every result remains a review candidate.

The [Vicmap Admin locality layer](https://services-ap1.arcgis.com/P744lA0wf4LlBZ84/ArcGIS/rest/services/Vicmap_Admin/FeatureServer/11)
provides the boundary. `data/suburbs/mooroolbark/boundary.geojson` records the
returned feature (PFI 205409730). The downloaded extent intersects 257 zoom-17
tiles. Captures use zoom-19 imagery and a 16 m approximate context margin.
Unavailable image children fail the tile rather than producing partial imagery.
Roof boundaries remain whole: centroid ownership assigns each proposal to a
half-open tile core and excludes context-only detections from map coverage.

## Run and resume

```sh
task scene:suburb CLI_ARGS="--prepare"
task scene:suburb CLI_ARGS="--resume 500114985968d40b"
```

Preparation freezes the boundary, tile list, prompts, inference code and weight
hashes. Resume runs from that code snapshot, retains captures, skips completed
tiles and retries failed tiles. The extraction lock prevents overlapping model
workers. An individual tile failure does not stop the remaining suburb. Existing
house scenes and saved feedback are never replaced. Scene model snapshots and
confidence sidecars are indexed as each tile completes.

The active run is in `data/suburbs/mooroolbark/active.json`; its `run.json`,
`worker.log` and `worker.pid` are under `runs/<run>/`. Completion means all planned
tiles have both predictions and an evidence assessment, not that all roofs were
found or their geometry is correct. Interrupted processes can resume the same run.
Leave a running worker alone when restarting the web app.

## Map review

Enable **Roof confidence** under **Analysed scene layers**. This selects model
estimates so a confidence label cannot be mistaken for a score of your edits.
The map shows coloured outlines and dots, including over 3D meshes. Filter a
band, select a roof, or use **Next roof to review**; its popup explains the
evidence and opens the exact model object in the CRM. Review navigation prioritises
low evidence first within the current view. **Show suburb** frames the boundary.
The optional coverage grid distinguishes pending, running, completed and failed
tiles. The confidence view polls every 30 seconds while enabled.

Model surfaces still render in detail when zoomed in. The suburb overview uses a
spatial index rather than loading every detailed mesh. Original scene and review
records remain unchanged; duplicate captures are suppressed only for display.
A changed model invalidates its indexed confidence until it is assessed again.

## What the bands mean

| Band | Evidence rule |
| --- | --- |
| Low | Failed/unavailable surface checks, image clipping, or detector score below 0.65 |
| Mixed | Geometry passes, but additional image, canopy or complexity concerns remain |
| Stronger | Detector score at least 0.8, passing geometry, at least 3 observed lines, mismatch at most 0.35 m and no diagnostic flags |
| Not assessed | Missing or invalid detector support |

These rules describe model evidence, **not probabilities of correctness**.
Matching image texture is not a survey; runoff/planarity checks do not establish
structural or building-code compliance. Roof heights remain estimates. The bands
are an aid to prioritising inspection, not a replacement for it.

The sidecar binds each row to the model signature, immutable source image,
geometry signature and inference profile. `confidence.sqlite` is a derived map
index; it can be rebuilt from the saved models:

```sh
task scene:confidence CLI_ARGS="--scenes house-30286 house-30296"
task scene:confidence CLI_ARGS="--calibrate"
```

## Accuracy intervals and validation

Currently there are no eligible fully reviewed validation building layers, so
accuracy intervals are **unavailable**. The API returns null rather than deriving
an accuracy percentage from the detector score. Existing draft corrections are
not silently promoted to evaluation labels.

Calibration uses the canonical scene-training eligibility and geographic-split
checks. Only fully reviewed validation building layers with matching model
signatures and frozen weight/prompt identities qualify. Legacy captures without
that identity remain uncalibrated. A success is a one-to-one prediction/reference match at
boundary IoU >= 0.85. A deterministic hash selects one roof per spatial group and
evidence band, independently of its outcome; model profiles are never pooled.
At least 20 independent areas are needed before publishing a 95%
[Wilson interval](https://www.itl.nist.gov/div898/handbook/prc/section2/prc241.htm).

Such an interval describes **boundary success in the sampled validation areas**,
under independence and representativeness assumptions. It is not a per-house
interval, roof-line/height accuracy, or detection recall. Convenience-selected
reviews can remain biased even after geographic grouping. Re-run calibration
after relevant reviews or model changes; the calibration artifact records its
dataset version and assessment time. Image captures spanning train/validation/
test groups stay ineligible for training until properly isolated.

Regression coverage includes whole-polygon tile coverage, halo frame registration,
missing imagery, resume after a tile failure, immutable completed captures,
cross-capture deduplication before filtering, stale index invalidation, protected
reviews, Wilson counts, and withholding intervals for insufficient groups.
