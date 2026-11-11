# Recapture carry path: design, pilot and the decisions waiting on you (2026-09-30)

Written 2026-09-30T15:08:42Z from the round-5 recapture outputs. Every figure below is read at write time from these files and
never typed:

- `data/reviews/eyes/recapture/DESIGN.json`: written 2026-09-30T12:14:32Z, sha256 `29fba5a30d9465ebed06554b4cdd389bdc2ec21cdab878c5c40d5ad60b506de7`
- `data/reviews/eyes/recapture/pilot-2026-09-30.json`: computed_at 2026-09-30T13:47:52Z, sha256 `08ad5ffccdd33e792b95536349bad65e5a59d86d69dc5c22a140c2d08e9c4acb`
- `data/reviews/eyes/recapture/plan-2026-09-30.jsonl`: computed_at 2026-09-30T13:47:56Z, sha256 `f476448f9ead594d812bdc33289d89c2d1175ab4b667af807b850cb5903d7202`

The counts are snapshots: the reviewer saves live, and `task recapture:plan` / `task recapture:pilot` recompute them
into `plan-latest.jsonl` / `pilot-latest.json`. The dated files above are the pre-registered runs and are never
rewritten: both tasks refuse a `DATE` whose file already exists.

This revision (after review) completes the object-id formula in section 1 and states in section 6 what `loadScene`
must return for a recapture with no review of its own.

**Status: dry run only.** No scene was recaptured, no review was carried, nothing under `data/scenes/` was written,
and nothing is promoted. Every figure is agreement between two frames, or between a re-projected human outline and a
detection, never accuracy. An earlier verdict travels to a new image only as a hint, never as a label.

| piece | where | state |
|---|---|---|
| frame transform, residual, byte-level verifier, offline plan, pilot | `scripts/ml/scene_recapture.py` | done (dry run; tests in `scripts/ml/tests/test_scene_recapture.py`) |
| capture of a new `lot-<address_id>` scene with its measured `frame_transform` | `scripts/ml/scene_lot.py --capture --carry-from` | written, never run (`task recapture:capture`, guarded by `CONFIRM=1`) |
| pre-registered design values | `data/reviews/eyes/recapture/DESIGN.json` | written 2026-09-30T12:14:32Z, before any pilot or plan run |
| the carry in TypeScript (re-derive the transform, re-project, status policy) | `src/lib/scene-frame-transform.ts` | module and test only (`node scripts/test-scene-recapture.mjs`, in `task test:reviews`) |
| app wiring (scene store, recovery, validation, editor banner) | `scene-types.ts`, `scene-feedback.ts`, `scene-store.ts`, `src/app/scenes/page.tsx` | **not live**: waits for a quiet window you declare (section 6) |
| headless check of the wired carry | `scripts/verify-scene-recapture.mjs` (`task verify:scene-recapture`) | written, not run (needs the wiring and a host with memory) |

## 1. Why a carry path

A recapture changes `image_sha256` and both affines. Object ids hash the image: `scene_extract` takes
`sha256(identity)[:16]` with `identity = f'{image_hash}:{kind}:{raw_coordinates}'`, plus `f':holes:{raw_holes}'`
when the outline has holes. So no id survives a new image, and the
editor's `recoverSceneReview` (verified 512 to 1024 expansion, or an identical pixel frame) returns null for it. The
only link between the two frames is the georeferencing, so every match across frames is geometric: best IoU per kind.

## 2. The contract (`frame-transform-v1`)

```
frame_transform = { version: 'frame-transform-v1', method: 'georeferenced-affine', source_scene_id, source_model_signature,
  source_image_sha256, source_size: [w, h], source_registration: 'fitted' | 'region', metre_offset: [dx, dy],
  pixel_affine: [[a, b], [c, d], [tx, ty]], overlap: { source_px_inside_target, fraction },
  residual: { method: 'gradient-ncc+phase-correlation', warped_from, shift_px: [sx, sy], shift_m, ncc, samples },
  design: { max_shift_m, min_ncc, min_overlap_fraction, match_iou, close_iou, design_sha256 }, verified: boolean }
carried_from = { scene_id, model_signature, object_id, status, geometry_reviewed, iou_with_new, matched_id | null,
  method: 'georeferenced-affine' }
verifier stderr on failure (stable): "Recaptured frame does not agree with the source frame"
```

- `metre_offset` is the source's local-metre origin expressed in the target's local metres (Web-Mercator scaled by
  the cosine of the target latitude, the maths of `scene_addresses.image_projection`); `pixel_affine` is
  `target.meters_to_pixel` after `+metre_offset` after `source.pixel_to_meters`, in the repo's row-vector layout.
- The residual warps the source image into the target frame, compares 2 px blurred gradient magnitudes, locates the
  shift by phase correlation refined at the gradient-NCC peak and measures NCC after removing that shift.
  `verified` = overlap, shift and NCC all within the design values (section 3). A measured agreement against
  pre-registered values, never a proof of identical pixels.
- The Python verifier (`scripts.ml.scene_recapture --verify-source <models/sig.json> --verify-target
  <models/sig.json>`) re-hashes both snapshots and both images, recomputes the offset, affine and overlap (to 1e-6),
  checks the recorded design against `DESIGN.json` by sha and re-measures the residual from the bytes (to 1e-3).
- The TypeScript module recomputes the offset, affine and overlap from the two snapshots and checks the recorded
  names, sizes, registration, verdict and residual against the recorded design values. It reads no files, so the
  `DESIGN.json` sha and the re-measured residual are the Python verifier's checks: the wiring must run it and refuse
  the carry when it fails.

## 3. Design values (pre-registered, never tuned on the pilot or the cohort reviews)

| value | DESIGN.json |
|---|---|
| `max_shift_m` | 0.7 |
| `min_ncc` | 0.35 |
| `min_overlap_fraction` | 0.6 |
| `match_iou` | 0.5 |
| `close_iou` | 0.9 |

Justification recorded in the file: 0.7 m = the 0.3 m selection uncertainty of the fitted affine plus the 0.39 m per-building residual measured between Christchurch sorties (about 6 px at z20, 3 px at z19); 0.35 is the blurred-gradient NCC floor teacher_judges.REG already uses; metres so the same design reads at z19 and z20.

Amendments (each timestamped, none changes a threshold):
- 2026-09-30T12:39:58Z: `residual_method` (no threshold changed); previous sha256 `8e6dc09677a0...`
- 2026-09-30T13:41:36Z: `counting_rule` (no threshold changed); previous sha256 `ed1ec8acc2b4...`

## 4. Status policy on a changed image

- Every carried decision comes back `needs_adjustment` with `geometry_reviewed: false` and
  `model.height_verified: false`; superseded members of a lineage set stay `superseded` (the replacement history
  needs them). **Nothing comes back `confirmed`, and nothing is rejected automatically**: the all-rejected rule of
  the same-image recovery (`scene-feedback.ts`) rests on identical pixels, which a new image never has.
- The earlier verdict travels as a hint in `carried_from.status` and in the notes. A review the assistant wrote is
  named there as an observation, never as a reviewer decision.
- Machine-only labels and unaccepted Fieldwork imports are dropped (neither is a human decision); unedited
  candidates are not carried; no provenance travels and **no `machine:*` provenance is written** (it would hide a
  later human confirmation from `export_dataset`); `carried_from.method` is `georeferenced-affine` on every carried
  object.
- Every object of the new model stays, unchanged and under its own id, beside the carried objects for duplicate
  review; a carried object or group whose id the new model already uses is carried as `<id>-carried` and listed in
  `renamed` / `renamed_groups`.
- Pixel fields (ring, holes, the outline snapshots of `roof_feedback` and `roof_line_edits`,
  `boundary_refinement.raw_*`, `roof_parts.ring_pixels`, `roof_lines.line_pixels`) go through `pixel_affine`; metre
  fields (`roof_line_edits.vertices` and baseline, `roof_annotation.vertices`, `roof_parts.ring_meters|ring|coords|
  triangles|gable_ends`, `roof_lines.line_meters`, `roof_feedback.linework`) move by `+metre_offset` with z unchanged;
  roof planes change only their constant term.
- An outline the new image edge cuts is clipped and noted; one that leaves the frame is dropped with a reason
  (`outside_new_frame`, `too_small_in_new_frame`, `split_by_new_frame_edge`, `invalid_in_new_frame`,
  `too_many_points_in_new_frame`); a lineage set (`derived_from` / `replacement_ids` / superseded members) is carried
  whole or the carry is refused with its reason.
- The round-5 precision judge and the small-roof counter treat `carried_from` plus `needs_adjustment` as undecided.

## 5. Decisions waiting on you

### 5.1 Identity: the same scene id, or a new `lot-<address_id>`

The code defaults to a **new scene id `lot-<address_id>`** (what `scene_lot.lot_source` writes): the source scene, its
`latest.json` and its `review.json` stay as they are, and a forward pointer would live under
`data/reviews/eyes/recapture/`, never in the old `latest.json`.

- **Same id, the cost:** a new image under the old id makes `save_scene` write a new `model_signature`, so every
  human-decided review on a recaptured scene goes stale (84 cohort scenes hold at least one human decision
  in the plan). `export_dataset.review_model` returns no model for a review whose snapshot image differs from the
  scene's image, so the 237 confirmed outlines on 65 scenes drop out of every export until
  they are confirmed again on the new image.
- **`lot-<address_id>`, the cost:** two scenes per recaptured address (the source stays live beside its recapture, so
  queues and exports need a rule for which one counts); the wiring must resolve the source scene across ids; and a
  cross-scene hop is not a Fieldwork recovery method yet (`docs/annotation/feedback.md`), so a Fieldwork edit on the
  source item lands on the source scene only.

### 5.2 May an earlier `rejected` verdict ever transfer automatically?

The plan says no. A rejection on the old image travels as a hint only; the reviewer rejects again on the new image.

### 5.3 When to declare the app quiet window for the wiring

The wiring edits files the running review app hot-reloads (section 6); a broken save path would lose your next
decision. It waits for a window you declare in chat.

## 6. Wiring, deferred to a reviewer-declared quiet window

Planned edits, each file written whole in one save: `scene-types.ts` (`frame_transform?` on the snapshot,
`carried_from?` on the object, `recovery.method` gains `georeferenced-affine`); `scene-feedback.ts` (one branch in
`recoverSceneReview` before the identical-frame check, delegating to the module, and `carried_from` in the
edit-comparison serializer); `scene-store.ts` `loadScene` (resolve the source snapshot from
`data/scenes/<source_scene_id>/models/<sig>.json` after validating the id, run the Python verifier behind a promise
cache, name the failed step in `recovery_reason`; `validateSceneReview` shape-checks `carried_from`); `page.tsx` (a
third banner sentence for a recapture, never "The image is unchanged.").

What `loadScene` must return for a recapture (the contract `scripts/verify-scene-recapture.mjs` codes against): a
`lot-*` recapture starts with no `review.json` of its own, and today `loadScene` sets `stale` only when the scene's own
review sits on another snapshot (`Boolean(review && review.model_signature !== model.model_signature)`) and builds a
recovery only under `stale && review`, so as written it never offers the carry. The wiring adds one branch: when the
scene has no `review.json` and its snapshot carries a `frame_transform` whose `source_scene_id` has a saved review,
`loadScene` reads that review (it must sit on `source_model_signature`) with the source snapshot and returns
`review: null`, `stale: true` and the `georeferenced-affine` recovery, or `recovery: null` with a `recovery_reason`
naming the failed step. `stale` must be true because `page.tsx` renders the recovery notice, with its "Carry edits
forward" and "Start from predictions" buttons, only under `data.stale`. The carried review goes out at revision 0
(the module's `options.revision`: the recapture's own revision), so the first save is revision 1 on the recapture's
own snapshot and `stale` reads false afterwards; the save path's existing guard (on a stale scene no object is saved
`confirmed` unless the recovery kept it) covers that first save, and the carry keeps none confirmed. The alternative,
rendering the notice on `data.recovery` alone, widens the deferred `page.tsx` edit beyond the banner sentence.

Gates before each save: no `review.json` modified in the last 10 minutes, the reviewer confirmed idle, a counter
snapshot taken; after each save the editor answers 200 and the counter's PRIMARY rows are unchanged, else the file is
reverted at once.

Also before the wiring: `scripts/ml/reference_registry.py` `review_tier` grades a `needs_adjustment` object with an
edited outline (or with roof-line edits) as a human T2 reference, and `review_authority_blocker` does not know
`carried_from`, so a carried object would enter the human tiers as if the reviewer had drawn it. Both files were
closed to edits tonight. Landed 2026-10-01: `scripts/ml/review_authority.py` gained the shared predicate
`carried_undecided(obj)` (any non-empty `carried_from` with status `needs_adjustment`, per the frozen round-5 RULES
text "any object carrying carried_from"; the contract writes a dict, but nothing on the save path rejects another
shape yet, so the first landing's dict-only test was widened the same day after verification; not a blocker, since a
blocker means excluded and a carried object is a target the reviewer has yet to decide), `review_tier` returns
`('T3', 'review:carried_undecided', False)` right after the `review_authority_blocker` check so neither the
re-projected ring nor carried roof-line edits make T2 (a carried object the reviewer later confirms, rejects or
supersedes on the new image keeps T1, rejected or T4), and `r5_precision.classify` and `reference_calibration.classify`
reuse the predicate, with `r5_precision`'s `carried` fact column reading the same `review_authority.carried` test so no
row can read carried with status `needs_adjustment` yet be classified by the status rule; tests sit in
`scripts/ml/tests/test_reference_registry.py`, `test_sam3_mac_precision.py` and `test_reference_calibration.py`.

## 7. Pilot on frame pairs that already exist (computed_at 2026-09-30T13:47:52Z)

The pairs rule, as written into the pilot file:

> the 8 cohort->house z20 pairs [29772, 30227, 30237, 30280, 30288, 30289, 30295, 30296] (model objects of the two frames; house-side human decision on the matched object) plus every cohort scene whose review.json holds at least one human confirmed / rejected / needs_adjustment decision (no machine_label or fieldwork_pending blocker, no assistant note) and whose centroid lies inside a registered mooroolbark-* region.bounds (target: the frame holding most of the cohort frame, then the nearest centre); every reviewed cohort scene left out is listed in selection.excluded with its reason

- Selection: 227 cohort scenes; 86 with a review; 84 with a human
  decision; 84 with the centroid inside one of the 257 registered
  `mooroolbark-*` frames; 82 cohort to mooroolbark pairs.
- Left out: `roof_training_250-25173` (outside_every_registered_frame); `roof_training_250-25183` (outside_every_registered_frame); `roof_training_250-26310` (no_human_decision); `roof_training_250-28656` (no_human_decision).

| group | pairs | verified | median overlap | median shift | median NCC | objects compared | IoU median | IoU median, unscanned as 0 | matched share | close share |
|---|---|---|---|---|---|---|---|---|---|---|
| house | 8 | 8 | 1 | 0.1618 m (1.274 px) | 0.8266 | 386 of 386 | 0.4586 | 0.4586 | 0.4741 | 0.0389 |
| mooroolbark | 82 | 82 | 1 | 0.1457 m (0.6172 px) | 0.8959 | 93 of 230 | 0.3159 | 0.0000 | 0.4409 | 0.0108 |
| all | 90 | 90 | 1 | 0.1491 m (0.6383 px) | 0.8931 | 479 of 616 | 0.4583 | 0.1879 | 0.4676 | 0.0334 |

- IoU median by kind, house: building 0.6225 over 79; car 0.5187 over 34; driveway 0.2458 over 31; footpath 0.2006 over 16; pool 0 over 13; road 0.457 over 23; solar_panel 0.2489 over 60; tree 0.6293 over 61; vegetation 0.5125 over 58; water 0 over 11.
- IoU median by kind, mooroolbark: building 0.7157 over 20; solar_panel 0.0896 over 34; tree 0.0812 over 39.
- Largest shift: 0.3695 m (`roof_training_250-29668` to `mooroolbark-50011498-17-118444-80416`); lowest
  NCC: 0.5093 (`roof_training_250-29668` to `mooroolbark-50011498-17-118444-80416`).

Signed shift (where the target shows the content relative to the transform's prediction, metres east and north; a
description only, its cause is not established, nothing is corrected):

| group | median east | median north | IQR east | IQR north |
|---|---|---|---|---|
| house | -0.118 | 0.1174 | [-0.1293, -0.0872] | [0.1112, 0.1214] |
| mooroolbark | -0.0905 | 0.1049 | [-0.1308, -0.0626] | [0.0732, 0.1345] |
| all | -0.0925 | 0.1062 | [-0.1308, -0.0644] | [0.0747, 0.1297] |

Saved statuses of the matched target objects (a human decision is a saved confirmed, needs_adjustment or rejected
status with no authority blocker and no assistant note; a candidate is undecided):

| group | raw saved status | human decisions among them | authority blockers | overlapping a human decision |
|---|---|---|---|---|
| house | candidate 106, confirmed 1 | none | machine_label 1 | 6 (needs_adjustment 6; IoU median 0.1275) |
| mooroolbark | candidate 11, needs_adjustment 1 | needs_adjustment 1 | none | 1 (needs_adjustment 1; IoU median 0.7080) |

The pilot's own wording, as written into the pilot file:

> Every figure here is agreement between two frames' objects, or between a re-projected human outline and a detection, never accuracy.
> The residual is a measured agreement against the pre-registered design values in DESIGN.json, never a proof of identical pixels.
> z19 (0.236 m/px) residuals from the cohort->mooroolbark pairs do not transfer 1:1 to a z20 (0.118 m/px) recapture: that grid is twice as fine and a new capture may come from another flight.
> The 8 house pairs compare model objects of the two frames only (no cohort review.json exists on them); the house-side human status (target_human_status) is a saved human decision on the matched house object, and a matched candidate is undecided and reads null (target_review_status keeps the raw saved status).
> A human decision is a saved confirmed, needs_adjustment or rejected status with no review_authority blocker (machine_label, fieldwork_pending) and no assistant note; a candidate is undecided and a superseded member is lineage, never a decision.
> The cohort crops are a Lanczos upsample of a z19 composite (median of up to two providers by design; 0.236 m/px native): their fine pixel spacing is not native detail. Cohort latest.json records no imagery provenance and rooflines falls back to a single provider when the composite fails, so the provider count is the pipeline design, not a per-scene fact.
> shift_east_north_m is where the registered frame shows the content relative to where the transform puts it (metres east, north, read through the target frame's own axes); a direction shared across pairs is consistent with a registration offset between the fitted cohort frames and the registered frames (or a pixel-centre convention difference); its cause is not established here: reported, never corrected (the design is not tuned on the pilot).
> Nothing was captured, carried or promoted and nothing was written under data/scenes/; the design was written before this pilot and is not tuned on it; hints are never labels.
> house: the IoU median 0.4586 is over all 386 cohort model objects (each of their kinds was scanned in the target). Matched target objects with a saved status: 107; human decisions among them: 0 (none); the rest are undecided candidates or not the reviewer's own.
> mooroolbark: the IoU median 0.3159 is over the 93 of 230 re-projected human outlines (confirmed, geometry_reviewed) of kinds the target scanned (of other kinds, with no IoU: 137; median 0.0 when they count as 0). Matched target objects with a saved status: 12; human decisions among them: 1 (needs_adjustment 1); the rest are undecided candidates or not the reviewer's own.

## 8. Plan over the cohort (computed_at 2026-09-30T13:47:56Z)

| item | value |
|---|---|
| cohort scenes / with a review | 227 / 86 |
| human decisions (confirmed / needs_adjustment / rejected) | 237 / 159 / 186 |
| all five statuses over every review object | candidate 3622, confirmed 241, needs_adjustment 159, rejected 186, superseded 33 |
| the same, outside machine_only, fieldwork_pending and agent_authored | candidate 3622, confirmed 237, needs_adjustment 159, rejected 186, superseded 33 |
| machine_only / machine_confirmed_by_human / agent_authored / fieldwork_pending | 4 / 0 / 0 / 0 |
| objects a carry would carry / dropped as machine-only / dropped outside the new frame | 582 / 4 / 0 |
| predicted post-carry status | confirmed 0, needs_adjustment 582, rejected 0 |
| metre-frame fields on carried objects | roof_annotation 15, roof_feedback 19, roof_line_edits 1, roof_lines 89, roof_parts 92 |
| lot frames planned offline / parcels unavailable / errors | 47 / 180 / 0 |
| tiles a capture would fetch / SAM3 extractions later | 691 / 227 |
| lineage sets | unplanned 4, whole_inside 1 |

The counting rule (DESIGN.json): all five statuses (candidate, confirmed, needs_adjustment, rejected, superseded) over every review.json object of the cohort scenes; objects with review_authority_blocker == machine_label counted separately as machine_only; objects whose notes match ^Assistant (visually traced|image review) counted separately as agent_authored; the plan file records the numbers it computes; no acceptance pins them.

## 9. Not claimed

- No accuracy claim of any kind: residuals, IoU and shares are agreement between two frames or between a re-projected
  outline and a detection, measured against pre-registered values.
- The z19 (0.236 m/px) residuals of the cohort to mooroolbark pairs do not transfer 1:1 to a z20 (0.118 m/px)
  recapture: that grid is twice as fine, and a new capture may come from another flight.
- The 8 house pairs compare the two frames' model objects only; no cohort human outline exists on them.
- No scene was recaptured and no decision was carried; no carried decision can return `confirmed` or `rejected`; the
  app's carry wiring is not live; nothing is promoted; no production `latest.json`, `review.json` or `confidence.*`
  was written.
