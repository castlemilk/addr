# Parcel-conditioned roof review

The unit-row example is now reviewed as eight numbered cadastral lots rather
than one combined development. The shared drive remains common property.
Parcel boundaries constrain allocation, but never supply a roof mask: roof
and yard still need image evidence. The reported parcel position uncertainty
at this site is 1.47 m, so roof evidence just outside a lot is retained as an
explicit uncertainty overlay.

## Inference and review

`scripts/ml/roof_lot_review.py` prepares square, padded original-RGB crops from
approved numbered lots. Common-property lots, overlapping parcels, missing
registration uncertainty and image-clipped lots are excluded or deferred.
The installed SAM3 text ONNX model runs the `roof` prompt on each crop.
This export exposes text prompts, not cadastral polygon or point prompts;
the parcel constraint is implemented by crop selection and mask allocation.
No image is painted with artificial cadastral edges before segmentation.

The resulting roof masks are allocated within exclusive lot cores. Yards are
not filled merely because they belong to a lot. Physical candidate edges are
stored separately from cadastral cuts; the latter never become roof hips,
valleys, measured party walls or height discontinuities. Roof family, slope
and height remain unresolved. Lot numbers are not asserted to be unit addresses.

Boundary proposals reuse the bounded straight-edge fit and original-image veto.
A second scale check prevents a close crop from replacing the wider roof with
missing sections or fragmented seams. If it removes/adds more than 8% of the
context area or substantially increases fragmentation/corners, retain the
wider image-supported outline as a review candidate. This safeguard does not
prove the retained outline is correct. An empty crop detection abstains even
when a context prior exists.

The self-review UI defaults to **Per-lot roof searches** for complete or partial results.
Choose Lot 1–8 or All lots, compare original imagery with the allocation, and
optionally show the close-crop result and uncertain outer roof evidence.
The physical roof and 3D comparison remain available through Selected roof /
Whole property block. Source-roof links open the existing feedback editor.
Per-lot candidates are not silently imported as saved edits or training labels.

## Bounded run

SAM observations are reusable, identity-bound inputs, frozen in each comparison
batch alongside code, imagery/model identities and cadastral snapshots:

```sh
scripts/ml/.venv/bin/python -m scripts.ml.roof_lot_review \
  --run COMPLETED_REVIEW_RUN --objects 0
scripts/ml/.venv/bin/python -m scripts.ml.roof_review_loop \
  --start --compare COMPLETED_REVIEW_RUN
```

The first command accepts 1–4 existing review indices, with at most 12 lots per
site. Interrupted extraction checkpoints each lot; complete observations are
cached under `data/roof-review-loop/lot-segmentation`. Saved feedback and
held-out frames stay protected. Other sites show “search regions prepared”
until their SAM pass runs; this iteration does not claim suburb-wide unit
segmentation. The candidate workflow never publishes replacement scene models.

## Default scene inference and regression policy

New scene extraction, re-analysis and context expansion now call
`scene_property_roofs.add_property_roofs` automatically. The model emits
`property_roofs` beside the original physical building surfaces. Per-lot masks
are allocations of detected roof area, not new folds in the continuous physical
surface. This is a versioned inference policy, not retrained SAM weights.
Existing saved scenes are not silently re-analysed.

`parcel-aware-scene-v1` uses successful frozen public parcel lookups and records
their digest, the image/frame/geometry identity, the active ONNX/tokenizer hashes
and inference code hashes. An edited outline, new capture or changed frame
invalidates inherited allocation results. Failed parcel lookups remain retryable.
The self-review loader consumes these model outputs when their source and policy
still match; there is no required manual parcel-extraction step for new scenes.

The automatic crop budget is 12 searches per scene, with shared multi-lot roofs
prioritised. Unprocessed lots remain explicitly `not_run`, failed or clipped lots
remain visible, and a partial batch cannot be reported complete. A joined roof
can also use directly intersecting numbered lots without a common-property lot;
this does not group other buildings across an entire subdivision plan.
Single-scene API analysis allows ten minutes for the bounded multi-crop workload.

`parcel-roof-allocation-v2` adds these guards:

- Reject invalid/nonfinite geometry, duplicate parcel IDs, implausible positional
  uncertainty, distorted registration and incomplete square crops.
- Keep lot cores exclusive, subtract non-numbered/common property, preserve
  parcel holes, and require positive image evidence before allocating roof area.
- Reject a crop replacement that changes connected components or courtyards,
  moves boundaries over 0.5 m, loses/adds over 8% of context area, adds excessive
  corners, or worsens the independent original-image edge check.
- Bind observed coordinates to their exact crop; reject unknown/duplicate cached
  lots, changed imagery, weights or registration. Concurrent CLI extraction is
  locked, and interrupted checkpoint rows are validated before reuse.
- Coalesce search contexts only when they share actual parcel IDs, with the same
  site-size bounds. A lot is searched and counted once. Empty masks and fully
  overlapping strata are handled without discarding the other lot results.
- Retain saved feedback and physical roof topology; generated allocations never
  become human approvals, training labels or calibrated accuracy intervals.

These thresholds prevent unsupported changes; passing them does not establish
survey accuracy. Cadastral position uncertainty remains explicit rather than
forcing physical roof eaves onto legal parcel lines.

Final verification: paired batch `5265515a696b62b7` retains all eight baseline
allocations exactly, with zero mutual overlap and only floating-point noise
(less than 1e-8 pixel²) in the common driveway. All 257 source scene models and
four Cambridge model/review files retain their hashes. The 12-roof comparison
still has seven `no_safe_gain` and five `needs_evidence` outcomes.

An additional real SAM3 default-entrypoint test is saved under
`data/roof-review-loop/experiments/parcel-default-v3/`: 12 searches completed in
120 seconds, including all eight PS823073 lots, with no failed searches. The
scene has 76 unique lot records: 12 unapproved proposals, 32 deferred for context
and 32 beyond the crop budget. Raw per-lot observations are retained alongside
their allocations for replay/audit. This test uses building extraction plus
the parcel model, with roof surface fitting disabled; it is not another suburb
scan or an accuracy benchmark. `eight-lot-verification.png` compares the original
image and the default model output. Earlier failed experiments are retained.

The full Python suite passed 674 tests; after the last empty-mask guard, all 159
focused geometry/inference regressions passed. Review API tests, processing-poll
concurrency/retry tests, TypeScript, lint and the production build passed. The
processing reader now coalesces simultaneous polling requests, retains a snapshot
for five seconds and permits a 90-second busy-workload read rather than failing
at 30 seconds. This only changes inventory reads, not background model execution.

## Observed results

- Source: `mooroolbark-50011498-17-118442-80415`, roof
  `324dd58f35837dfa`, cadastral plan PS823073.
- Eight fresh SAM crop searches completed, covering both existing roof rows.
- Close crops omitted 16.3–33.3% of the wider candidate area and often created
  fragments around seams or level changes. All eight retained their wider
  image-supported outline. Crop outlines remain inspectable.
- Eight non-overlapping lot allocations; no allocation in the common drive.
  Estimated roof area within each lot ranges from 67.74 to 82.02 m². These are
  candidate plan areas, not surveyed or validated roof measurements.
- Paired 12-roof comparison `cd4b653c671c452d` against
  `f0ab3f493492f683`: existing pitch outcomes unchanged (7 no safe gain,
  5 need evidence). No new accuracy/confidence claim or labels approved.
- All 257 suburb source models and four Cambridge reference model/review files
  retain their recorded hashes.

Artifacts: `data/roof-review-loop/runs/cd4b653c671c452d/lot-comparison.png`,
`lot-verification.json`, `reflections.json`, and `object-0.json`.
The same review data is available through
`/api/scenes/self-review?run=cd4b653c671c452d&object=0`.

Validation includes 110 focused Python tests, TypeScript checking, targeted
Python/ESLint checks, a production build and live review interaction checks.
New regressions cover common drives, yards, parcel holes, crop registration,
ambiguous parcels, missing detections, context-loss rejection and frozen caches.

The next evidence needed is reviewed per-lot roof/yard segmentation and actual
roof level/slope feedback. Cropping alone is not an image enhancement method
and did not improve this example’s outer eaves.
