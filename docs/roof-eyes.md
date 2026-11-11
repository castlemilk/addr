# Roof "eyes": reference-anchored observability sidecars

Started 25 September 2026. Design: `docs/superpowers/specs/2026-09-25-roof-eyes-design.md`.
Every signal here is a **diagnostic or a reference agreement**, never calibrated
accuracy, and never a label. Agent observations (CRM agent-imagery traces,
review-loop rows, candidate model objects) are tier 3 and never enter a
denominator. Production artifacts (`latest.json`, `models/`, `review.json`,
`confidence.json`, `confidence.sqlite` rows) are not rewritten by any command
below; `eyes_guard` proves that before and after each checkpoint.

## Index and commands

Signals live in `data/reviews/eyes/eyes.sqlite` (module `scripts/ml/eyes_index.py`),
keyed by scene, object, model signature and version, with `code_sha256` and
`computed_at` in every payload. Read one object with
`python -m scripts.ml.eyes_index --object SCENE OBJECT`.

```sh
task eyes:guard CLI_ARGS="--write cp1-start"   # protected manifests, frozen run, CRM #9 geometry, index mtimes; exit 1 on drift
task eyes:registry                             # tiered human-reference registry -> references table + data/reviews/eyes/registry/<id>/
task eyes:cluster                              # agreement, cross-capture (suburb), roof diagnostics, object quality, shadow bands for the Cambridge cluster
task eyes:snapshot                             # content-addressed suburb snapshot with denominators; CLI_ARGS="--diff A B" compares two
task eyes:checkpoint CLI_ARGS="--id CP1 --dry-run"   # then without --dry-run: CRM POI + address tags/notes via the 'pipeline' principal
task eyes:calibration                          # sampled calibration plan + review queue; CLI_ARGS="--progress PLAN_ID" for progress/intervals
task eyes:eligibility                          # loop reach under frame vs core split policies
task eyes:stack CLI_ARGS="SCENE_ID"              # multi-date stack onto the saved grid (or a not_available registration report)
task eyes:multidate CLI_ARGS="--scene ID --stack STACK_ID"   # per-roof multi-date agreement rows
task eyes:experiment CLI_ARGS="--band-v2"       # paired/join experiments with claims + cannot_claim
task eyes:line-corrections CLI_ARGS="--objects house-25822:4fa078ff5ac6a8d4"   # reviewer roof-line corrections vs the model estimate -> line_corrections rows; preview with `python -m scripts.ml.line_corrections --dry-run`
```

| module | signal | meaning |
| --- | --- | --- |
| `reference_registry` | tiers T1 confirmed+geometry_reviewed, T2 human edits (roof_line_edits, edited outlines, CRM rows with reviewer provenance), T3 agent observations, T4 context | which geometries may anchor an accuracy statement |
| `reference_agreement` | IoU, Hausdorff, centroid shift, roof-type match, line precision/recall (<= 0.5 m / 10 deg) vs human roof-line edges, image identity | how far the model sits from a human reference; cross-imagery rows carry a caveat |
| `cross_capture` | every duplicate `scene_confidence.query()` suppresses, with both bands, scores and IoU | disagreement between captures of the same roof; also exposed live as `properties.hidden_overlaps` |
| `scene_roof_diagnostics` | recomputed `propose_roof` diagnostics (line-fit residuals, matched edges, topology cost, planes/creases/short links, end decisions), per-edge outline support on image edges, crease contrast across roof lines, `recomputed_matches_stored` | how well the 3D roof is supported by the image, without heights (all heights are class priors) |
| `scene_object_quality` | detector support tier, size sanity, solar parenting, tree/roof overlap, same-class overlap, edge touch, plausibility flags | tree and solar plausibility; no band, no accuracy |
| `shadow_bands` | evidence rule v2 as a shadow column (runoff-not-assessed is informational; null mismatch when no lines observed; saturation flag) | who would flip before any production re-assessment |
| `scene_isolated` + `paired_compare` + `scene_lineage` | isolated re-inference under `--out-root`, object lineage, paired deltas with bootstrap CIs and claims/cannot_claim | the mechanism for every before/after claim (CP3) |
| `eyes_report` | suburb snapshot pinned to the 10,072 owned scan roofs of run `500114985968d40b`; null-aware mismatch quantiles | trend between checkpoints |
| `reference_calibration` | one fixed-hash owned roof per (band, validation core tile of the object centroid); review queue with `/scenes` links; Wilson interval only at >= 20 reviewed groups | sampled-roof protocol; boundary IoU >= 0.85 only; calibration split policy only (SAM3 encoded 64 px of neighbouring tiles); the high band cannot reach 20 groups (6 validation tiles hold a high roof) |
| `eyes_eligibility` + `roof_review_loop --split-policy core` | low roofs reachable by the loop under the frame policy (139) vs the opt-in object-centroid core policy (1,303); default batches unchanged | reach of the diagnostic loop; core batches carry `split_policy` in their report |
| `scene_stack` | Esri Wayback dates registered ONTO the saved scene grid (saved PNG is the reference; existing RANSAC gates plus median hold-out residual <= 2 px; per-roof ECC translation recorded); stack median/min/max/count/disagreement and an edge-consensus raster only when >= 2 dates are accepted | evidence views for the line fitter; a `not_available` report is an honest negative |
| `multidate_agreement` | detector-free per-roof outline and roof-line support on each accepted date, boundary edge consensus across >= 2 dates, dates with support | consistency across dates on a shared orthomosaic grid, not accuracy; payload states the reference grid |
| `roof_line_fit.image_segments(extra_views=…)` and `propose_roof/add_roof_estimates(evidence_views=…)` | gated extra LSD views (default off, byte-identical without them); every extra-view line must still pass `line_evidence` on the original RGB; `view_origin` recorded only when views are used | how fused evidence enters inference without replacing the saved image |
| `eyes_experiments` | declarative paired isolated experiments (code_drift, band_v2, extra_views, split_policy_core, sam3_on_median) with claims and cannot_claim generated from denominators; `--run` refuses SAM3 unless `--allow-inference` | the only route to a before/after claim |
| `crm_checkpoint` | `CreatePoi` (category `pipeline-checkpoint`) + read-modify-write `UpdateAddressMeta` with `eyes:` tags and one note line; report under `data/references/eyes-checkpoints/<id>/` | the user's review surface |
| `line_corrections` (`line-corrections-v1`) | per saved review revision of a building with `roof_line_edits`: the repo line rule (0.5 m / 10 deg) between `roof_line_edits.baseline` (the model estimate the editor seeded) and the reviewer's saved lines, a distance sweep (0.35 / 0.5 / 1.0 m), 1 m length coverage shares, per-kind matches, graph topology on both sides (edges, orphan vertices, components, short lines), `ref_checked`, `edited_at_revision`; PK (scene_id, object_id, review_revision, version) | agreement between the model's starting lines and the reviewer's lines, never accuracy; a draft while `ref_checked` is false; read by `scene_processing` (`properties.eyes.line_corrections`) and `scene_roof_review` |

Pair mode (`scene_local_stack --allow-pair`, i.e. `build(min_accepted=1)`): when the per-window local registration accepts exactly one Wayback date (tile 118442-80417: only 2025-11-06 Vantor passes, 40/121 windows, coverage .72, hold-out residual .80 px) the report is status `pair`, never `available`. It holds the saved PNG plus that one aligned date: `aligned-<i>/valid-<i>/shift-field-<i>.png`, `edge-consensus.png` counted over the two views (0..2, same eroded mask and 3 px border exclusion as the full stack), `valid-count.png` and `enhanced-pair-rgb.png`, a per-channel mean of the saved RGB and the brightness-normalised date where valid (saved pixels elsewhere) for review only. Its limits are the point: with two views there is no median, min/max or disagreement raster (those need >= 3 views, so `sam3_on_median` records `unavailable: needs >= 3 views`), a mean cannot reject an outlier the way a median does, and `multidate_agreement` rows carry `mode: pair` / `two views only` with `dates_with_support` bounded to {0, 1, 2} and boundary consensus over exactly two views. The shared dispatcher `scene_local_stack.views` hands `extra_views` the `edge-consensus` and `aligned-date-<date>` gray views (never a `stack-median`); the saved PNG stays the sole evidence gate and every extra-view line must still pass `line_evidence` on the original RGB. With the flag absent, every function behaves exactly as before and one accepted date remains the honest `not_available` negative; an `available` stack always outranks a pair.

## Live surfacing

`scene_processing.inventory()` (already live on `/api/scenes/processing`) enriches every
feature read-only from the index: `properties.hidden_overlaps` (from `query()`),
`properties.eyes` = {reference (best T1/T2 agreement), diagnostics, shadow_band_v2,
cross_capture} and `image_line_mismatch_display` (null when no lines were observed).
`GET /api/scenes/eyes?scene=&object=` returns every sidecar row for one object.
`GET /api/scenes/image?…&view=crop-v1&x=&y=&w=&h=&size=` streams a downscaled display crop of the saved PNG (sharp) for thumbnails; `view=multidate-median-v1&stack=` streams a local-stack composite. Neither replaces the saved image as the evidence source.
The processing inspector ("Model evidence" vs "Reference agreement" vs "Other
captures"), the confidence popup ("Also seen as …", "Human reference IoU …") and the
CRM panel (pipeline-checkpoint POIs, read-only feature properties) render these after
the next web rebuild. Review saves record `review.basemap_mode` (`ai-satellite`, `raw`
or `unknown`) so future human references carry drawing-surface provenance.

## MCP visual review handoff

`scene_roof_review(..., include_boundary_guidance=true)` now adds an immutable,
unapplied boundary-and-roof comparison (`rgb-eave-comparison-v1`). It starts from
the retained detector mask, reuses bounded eave-run fitting, and compares a finite
set of signed edge offsets against the original RGB. Fixed inward material
anchors and paired samples reject an outer dark band as roof-side evidence;
illumination-normalized chromaticity prevents a shadow on a coloured roof from
being called a new material. Brightness-only and vegetation-adjacent transitions
remain explicitly ambiguous. A coherent painted strip or equipment can still
resemble an eave, so even a material-supported result requires visual review.

This comparison has separate, reported bounds (visible displacement at most
2 m or 8 original pixels, whichever is smaller; hidden displacement at most 2 m;
20% total area change). It does not relax production `regularize_eaves` or the
existing generic candidate gate. Genuine visible recesses, exact saved holes,
image cuts and protected detector/reviewer boundaries remain constrained.
Candidate roof geometry is refitted with original RGB and physically audited.
`changed` and `accepted` are distinct: an uncertain changed outline is only an
alternative. The packet exposes its own source-pixel ring and holes; the card's
fourth panel draws that ring, with orange dashes for inferred hidden spans.

Add `boundary_upscale=true` to nominate line positions from a source-bound
Real-ESRGAN x4plus crop before those checks. The crop uses the retained raw ring.
OpenCV LSD endpoints map back with the adapter's pixel-centre affine; generated
pixels never supply material, roof-form, line-support or height measurements.
Runtime/weights/output/source hashes and both coordinate conventions accompany
the review. Cache-hit status is operational and excluded from immutable content.
Unavailable SR is reported explicitly while the RGB-only comparison remains
available. No scene model, reviewer geometry, feedback binding or labels are
changed by either option.

The local `addr-review` MCP server now carries the whole visual feedback loop:

1. `low_confidence_roofs(limit=12, offset=0)` reads the current confidence index,
   checks model and object geometry identities, and applies the existing strict
   training-frame and protected-review gates. It selects one owned detector roof
   per capture; pagination is deterministic for the same source population.
2. `scene_roof_review(scene_id, object_id)` returns a native MCP image of the
   existing registered review card: original RGB, model outline/roof lines, and
   detected image segments. Its compact JSON includes source-pixel geometry,
   exact crop/source affine transforms, model/image hashes and cached eyes rows
   marked current, stale or unbound. `include_source=true` also attaches the
   original full PNG; `include_feedback=true` reads saved line edits through the
   canonical scene API and labels them separately. The default read does not run
   roof fitting. `include_optical_guidance=true` computes a fresh unapplied
   automatic proposal and adds its geometry and scoped optical/valley cues in a
   fourth panel, bound to the same source and the modelling/layer code hashes.
   Its third panel separates yellow base detections from cyan optical additions;
   the packet projects the additions into source pixels, while the report retains
   exact metre coordinates, discovery views and original-RGB support scores.
3. After actually inspecting the image, `record_roof_observation(review_id,
   observed, hypothesis, ambiguity, next_action, reviewer)` creates an immutable
   tier-3 observation. The observed facts stay separate from the hypothesis and
   unresolved questions. Changed source/image/geometry or altered handoff
   artifacts are refused, and identical retries return the original note.

Artifacts live under `data/reviews/eyes/roof-review/<review_id>/`: `card.png`,
`manifest.json` (exact model object, source identity, detected segments and eyes
snapshot), and `observations/*.json`. A review returns earlier notes for the
same source/model/image/object identity even when code or cached eyes changes
produce a new review ID. Zero detected segments show an unavailable (`null`)
line mismatch; raw sidecar values remain in the manifest.
These files are never inserted into `object_quality`, scene review files or
training exports. Display coordinates are rounded to 0.001 source pixel; the
manifest retains exact model geometry. Detected segments can be texture,
shadows or panel edges, and cached diagnostics are not revalidated by reading
them. Nothing here promotes an agent observation to a human label.

The [27 September MCP visual pass](../data/reviews/eyes/mcp-low-confidence-20260927/REVIEW.md)
contains 12 inspected roofs, native image/geometry responses, saved observations
and successful MCP readbacks. All 744 scene model/review files remained unchanged.
An existing saved review also returned its 20 draft line edits through the MCP
handoff. The report distinguishes possible detector merges, unsupported hips,
panel/occlusion effects, repeated roof sections and missing image context.
All 12 source identities match cloud mirror cursor 538; one item was additionally
verified through the live hosted MCP. Native image blocks are currently provided
by local `addr-review`; hosted Fieldwork MCP supplies structured data and asset
references.

## Optical guidance and valley hypotheses (27 September 2026)

New automatic proposals use `roof-optical-guidance-v1` by default;
`propose_roof(..., optical_guidance=False)` and the same CLI JSON field provide
the unchanged baseline for ablation. Existing saved scenes and reviews are not
rewritten. `scene_house` retains the compact decision, contradictions and source
evidence on new roof objects, and records the guidance/layer code hashes in
analysis provenance.

The stage freezes every surviving original-image segment and its support,
including the full 100-segment capacity. DoG and CLAHE 12 discover additional
lines using separate seam filtering. Every addition must pass the existing
original-RGB contrast/material-sample gate. Repeated detections are deduplicated;
these views share pixels and never count as independent votes. The separate
24-addition cap cannot evict an original segment. Evidence records the source
RGB pixels, layer arrays, metre/pixel affine and ordered original/preserved IDs.

Guidance reuses at most six already generated roof plans and refits at most two,
with no second topology search. A change requires exact footprint coverage,
physical geometry and runoff checks, preservation of each previously explained
original line, retained original explained count/length, no increase in
unsupported folds, and a bounded plane count. It also requires at least 0.08 m
less combined residual and 0.5 m more explained added line length. Unchanged
geometry, clipped imagery, weak evidence, complex roofs and failed checks keep
the baseline with an explicit reason. These internal scores do not measure roof
accuracy or height improvement.

A `valley_supported` hypothesis requires a concave fold between model planes and
sufficient support from original-RGB-gated image lines. Image creases alone stay
ambiguous. Existing wing/L/T alternatives can expose potential missing valleys.
`contradictions_scope=comparison_baseline` describes the baseline before guidance;
`valley_candidates_scope=pre_refit_candidate_plans` describes retained alternatives;
`selected_valleys_scope=selected_proposal` describes an accepted output. The MCP
card shows selected geometry/valley hypotheses after acceptance, so resolved
baseline contradictions are not presented as current. The full JSON retains
candidate comparisons and gate decisions for review.

Call local MCP `scene_roof_review` with `include_optical_guidance: true` for a
fresh, unapplied proposal plus a native review image. Its third panel shows
yellow original detections and cyan optical additions; its fourth panel marks
image-supported valley hypotheses purple and geometry-only estimates grey.
`geometry.additional_optical_segments` returns source-pixel coordinates, segment
IDs, discovery layer and original-RGB support; the report preserves exact metre
coordinates. `include_source: true` also returns the original image.

The [15-case evaluation and validation handover](/Users/benebsworth/projects/addr/data/reviews/eyes/optical-valleys-20260927/HANDOVER.md)
records paired proposals, native MCP checks, uncertainty and source preservation.

## Flat and low-slope family alignment (27 September 2026)

`roof-form-flat-v3` keeps the existing numeric flat-rule thresholds. Plane
brightness now uses the same material pixels as texture, excluding detected
equipment and its existing 0.5 m halo. Pitched-crease contrast uses paired
samples that both lie on the visible roof, outside holes and equipment; fewer
than three valid pairs is unavailable. Pixel and pair counts stay in the form
evidence so a missing measurement cannot appear as perfect agreement.

The large and bright rule sets can disagree even when every large-rule
appearance gate passes: the previous uncertainty check overlooked that case
when area was the only failed large gate. A measurable complementary-rule gap,
above the existing area minimum, can now return an explicitly unresolved
coplanar starting surface. Central ridge evidence and coherent diagonal folds
veto that uncertainty branch. The diagonal veto does not change the existing
positive flat rules. The coplanar representation has no internal folds and
preserves the exact outline and holes; it does not claim measured horizontality
or establish whether the roof is flat, shed or low-pitch.

Below-area or incomplete family evidence gets an unselected level-surface
comparison while the primary surface remains an uncertain pitched prior.
Optical guidance preserves its detections but cannot promote new folds or
valleys for this unresolved family. New scene objects retain
`roof_form_evidence`, `roof_form_uncertain` and any `roof_form_comparison`.
Manual templates and saved scene/review files are untouched.

MCP binds `roof_form.py` in its code hashes and displays a fifth comparison
panel when needed. Coplanar/uncertain titles and reasons replace the valley
legend for those surfaces. The saved annotation graph is authoritative for
displayed lines, including an explicitly empty graph. An explicitly flat model
without that graph does not fall back to stale cached fold lines; canonical
human feedback remains separate.

The [paired evaluation](../data/reviews/eyes/flat-alignment-20260927/paired-summary.json)
completed all 15 cases: one changed from 13 pitched lines to a flat hypothesis,
one gained only a level-surface comparison, and the other primary geometries
were unchanged. Original line-detection hashes were identical. The real flat
school and two visibly pitched controls retained their families, as did the
faint-ridge and lit-plane synthetic controls. These are bounded regression
checks, not measured roof-height accuracy.

## Address coverage limit for CRM tagging

The local G-NAF table (`addresses` in `data/australia_geo.db`) holds about 4,400 Mooroolbark
records, so only roofs whose Vicmap property match resolves to one of those can be tagged
per address in the CRM. Every sampled roof is still reachable through its `/scenes` link in
the calibration review queue.

## CP1 findings (27 Cambridge cluster, 25 September 2026)

- The scan's own 27 Cambridge roof (`mooroolbark-50011498-17-118442-80417/44c724fd3480c167`) is band low and was hidden in the map by the house-30286 capture (medium) at IoU 0.90. Suburb-wide, 55 low roofs are hidden under higher bands (5 under high).
- Outline agreement with the human CRM override is IoU 0.82 (scan) and 0.86 (house capture). The two human sources agree with each other only at IoU 0.85. The cohort object `roof-29790` is a copy of the CRM override and is excluded as circular.
- The 3D model, not the outline, is the defect: 27 planes and 51 creases against 32 human edges; roof-line precision 0.12 (scan) and 0.31 (house); only 53% of the scan creases show an image brightness step. Outline edge support is 100%.
- 106/106 cluster roofs diagnosed at a median 0.78 s per roof; 100 reproduce the frozen geometry exactly with current code. Suburb-wide recomputation (~10,000 roofs) is CP2 background work.
- Objects: 4 of 21 cluster solar panels sit outside any building; 19 of 104 trees have detector support below 0.5.
- Shadow rule v2 flips 0 of 87 cluster roofs; the suburb flip matrix is a CP2 item.
- Human-reference registry: T1 49 (train 34 / validation 9 / test 6), T2 6, T3 1,428, T4 12. Tier-1 agreement 40/49 at IoU >= 0.85 (many are confirmed-as-is model outlines). Calibration intervals remain unavailable.

## CP2 / CP3 status (26 September 2026)

- Suburb-wide sidecars are computed for all 10,072 owned roofs (diagnostics: 10,053 succeeded, 19 recorded failures; current code reproduces the frozen geometry for every successful roof; median 1.05 s per roof), all 7,874 trees and 2,163 solar panels.
- Roof form is the weak layer: the fitter flags 97.5% of roofs as form-uncertain and assigns hipped to 89% (human labels so far: 22 hipped vs 20 gabled); median 18 crease runs per roof; half of all creases show no brightness step in the image; 21% of roofs matched zero image edges in the line fit. Outlines are well supported (94% have under 20% unsupported edge).
- Calibration plan `7cfed4d9e02f1c8a`: 67 fixed-hash roofs (low 30, medium 31, high 6 groups); the high band cannot reach 20 groups from this population.
- Review-loop reach: 139 low roofs under the frame policy vs 1,303 under the opt-in core policy.
- Code drift experiment (isolated SAM3 re-run of tile 118442-80417 on identical bytes): 127/127 objects reproduced by id, 78 roofs paired, identical bands, IoU 1.0.
- Shadow band rule v2 flips 0 of 10,072 owned roofs (no roof in the scan has runoff unassessed above 64 parts once geometry passes), so promotion would change nothing today; the null-aware mismatch display is the useful part.
- Checkpoints filed in the CRM by the `pipeline` principal: POI 2 (CP1), POI 4 (CP2) on 27 Cambridge Road; POI 3 (calibration queue) on 19 Larnook Crescent with `eyes:calib-sample` tags on the nine sampled addresses present in the local G-NAF table. Reports: `data/references/eyes-checkpoints/{CP1,CP2,CP2-CALIB}/REVIEW.md`.
- Multi-image evidence, honest negative: all three Wayback dates fail registration against the saved Google image (scene_stack) and two of three fail Wayback-to-Wayback (scene_temporal run `7184a99f459bbdc6`, 0 consistent objects). Cross-provider registration, not fusion, is the bottleneck; per-roof local registration is the next paired experiment.

## Workbench UI, 26 September 2026

The web app was rebuilt with one app shell (`src/components/shell`, five workspaces: Map,
Scene review, Review queues, Processing, Training), a scene hub at `/scenes`, a Review
queues workspace at `/queues` (calibration sample with live status and Wilson wording,
low roofs hidden under higher bands read from the eyes index with Node SQLite, self-review
batch, roof training set, pipeline checkpoints with a markdown reader at
`/queues/checkpoints/<id>`), and a summary-first processing feed
(`/api/scenes/processing?part=summary|roofs|roof|hidden-low`, 20 s inventory TTL with a
lead refresh) whose thumbnails are 11 KB JPEG crops (`/api/scenes/image?view=crop-v1`).
Rebuild procedure: `task build:web`, then replace the launchd web job per
`docs/local-services.md`; never kill only the PID. Browser suites: `task verify:shell`,
`verify:map-shell`, `verify:scene-start`, `verify:processing`, `verify:queues` plus the
existing `verify:ui`, `verify:reviews`, `verify:scenes`, `verify:scene-*`. On a loaded
machine (an external VM or compiler at load average 30+) the hub and queues suites can time
out on their first-paint budgets; `HUB_BUDGET_MS` widens the hub budget and the run should
say so. `APP_URL=… node scripts/ui-shots.mjs` screenshots every page for review.

## Multi-date evidence, 26 September 2026

- Per-window local registration (`scene_local_stack`) accepts the 2025-11-06 Vantor date for tile 118442-80417 (40/121 windows, coverage 0.72, hold-out 0.80 px) where the whole-tile similarity gate failed; 2025-09-28 (shadows) and 2023-12-20 Maxar (blur) stay rejected without tuning. Pair mode (`--allow-pair`) writes the aligned date, a two-view edge consensus and a display-only mean composite (stack `c26ee81c761dd075`); 59 `multidate` rows: boundary edge consensus median 0.56, 52 of 59 roofs supported on both views.
- The 2025 Vantor captures show 27 Cambridge as bright terracotta with straight ridges (matching the CRM material label); the saved Google image shows it dark brown with weak ridge contrast (`data/references/eyes-checkpoints/cambridge-dates-crops.png`).
- Paired isolated experiment `extra_views` (run `ed471fe66b1bc666`, SAM3 re-inference with the aligned date and consensus as extra LSD views): 59 roofs paired, 0 gained/lost, identical bands, mismatch, segments and line precision/recall for every roof. Cause: the `line_evidence` gate on the original RGB rejects lines the Google image cannot corroborate, by design (spec principle 5). Making a registered, hash-bound dated capture an evidence source in its own right is a policy decision, not a code fix; it would need its own provenance (`evidence_image_sha256`, acquisition date) on every line and a new inference profile.
- The dense Canny edge-consensus raster saturates (77% of pixels have an edge in at least one view) and is not a discriminating signal as built; line-segment consensus is the better next form.

Next: CP2 expands every signal suburb-wide, adds the sampled calibration plan
(object-centroid core-tile split, calibration only), surfaces the fields in the
processing workspace and map with one web rebuild, and files CP2. CP3 runs the
paired isolated experiments (code drift, band v2, multi-date evidence views
registered onto the saved scene grid, split policy) and hands promotion
decisions to the user with measured deltas.

## Improvement round 1, 26 September 2026 (after the user's queue review)

Five items were built against the user's 13 flagged roofs (`data/references/eyes-checkpoints/CP3/user-feedback-2026-09-26.json`), each measured with the paired recompute harness (`task eyes:roof-eval`, `scripts/ml/roof_recompute_eval.py`; baseline `data/reviews/eyes/roof-eval/baseline-user-flagged.json`). Every number below is a model estimate or reference agreement, not accuracy.

| item | version | flagged-set effect | wider effect |
| --- | --- | --- | --- |
| Flat roofs (image evidence) | `roof-form-flat-v2` (`roof_form.flat_evidence`, `FLAT_RULES`) | 1f022af0 (school, 1,189 m²) hipped to flat; d3d8a166, 7c18d669, 654dd0e3 stay pitched (texture 14.0 vs 14 with 216 plant blobs; crease steps 0.62; outline share 0.71) | 29 of the 226 commercial-hint roofs are flat image-only; 0 of 45 T1 houses or 110 suburb roofs over 200 m² flip |
| Topology that follows visible lines | `roof-topology-support-v1` (`roof_topology._image_supported_simplification`) | cbc33fd3 20 planes/37 creases to 2 planes/1 ridge (gabled) | suburb-200 pair: 20 of 200 simplified, 11 roof-type changes, errors identical; family-42: 7 simplified, 6 type changes (two hipped labels to mixed via the gable prior); output byte-identical wherever the stage does not fire |
| Building class hints | `building-flags-v2` (`scene_object_quality`, `context_landuse`) | oval shelter 2d2fde07 flagged non-residential; backyard 7fdd138f now `outbuilding` (same-property ratio 2.39 with the v2 tiered rule); tree f5c74b7e is a tree object, no building row | suburb: secondary-structure 1,287, non-residential-context 2,129 (21 %, noisy), tree-like 11, commercial 226 of 10,072 |
| Units and lot boundaries | `unit-split-candidates-v2` (`unit_split_candidates.py`), SceneCanvas lot labels | e13e7a2d: 10 lots touch the outline, per-lot candidates 56 to 91 m²; 324d3af9: five stacked strata lots, no cadastral cut, `propose_roof` still fails on the lobed outline | boundaries render with dashed lot lines and labels; `task verify:scene-parcels` |
| Editor imagery | sharp CLAHE `roof-detail-v1`, `/api/scenes/stacks`, Imagery select | contrast view 0.2 s instead of a 10 s timeout; aligned 2025-11-06 and two-view composite on tile 118442-80417 | other flagged tiles have no accepted second date, so their editors list only Original and Enhanced contrast; `task verify:scene-imagery` |

Flat-roof context hint (evaluated, not adopted). `propose_roof(..., context_hint=)` now carries the building-class hint into `flat_evidence`, and the pipeline (`scene_house.add_roof_estimates(context_hints=)`, fed by `scene_house.pipeline_context_hints` from cached context only) records it in `analysis.context_hints` and `form_evidence.context`. A variant that let a `commercial` hint waive the plane-brightness gate (the one gate shown not to separate multi-section decks from lit pitched planes) flipped 15 more of the 226 commercial-hint roofs to flat (`data/reviews/eyes/roof-eval/flat-v2/commercial-226-{control,hinted}.json`); on the contact sheet about half of those show ridges or hips, and the flagged d3d8a166 still fails the texture gate. `FLAT_RULES['context']['waived_gates']` is therefore empty by default; the hint is visible to reviewers, decisions are image-only. Two image-only measures were also tried and rejected as non-separating: between-face over within-face brightness spread, and whether face brightness follows slope azimuth (`/tmp/addr-eyes/flat_features*.py` in the session; numbers in the CP4 report).

Known limits after this round: L/T over-complex roofs (d2e96c3a, 666256df, 19e7744e, 27 Cambridge) are untouched because no orthogonal support can be built from their traces; 324d3af9 still raises in the skeleton; the tree object f5c74b7e reaches the fitter only through the calibration plan; non-residential-context is too broad for a queue filter until land use is required to intersect the roof.

## Improvement round 2, 26 September 2026 (structure sub-kinds and the enhanced-image detector test)

The user asked for the enhanced imagery to be pushed into SAM3 detection as pre-roof analysis, and for finer structure labels ("shipping container", "shed") as property enrichment. Two measured results:

Structure sub-kinds (`scripts/ml/structure_kinds.py`, `structure-kinds-v1`, `task eyes:structure-kinds`). For every live building under 150 m² or flagged secondary-structure, geometry (minimum rectangle, rectilinearity, circularity), appearance on the CLAHE crop (rib anisotropy and period, brightness, see-through ratio) and context (attached, distance to the largest roof, same-property flag) give a sub-kind with confidence and evidence; SAM3 prompt scores are optional (`--sam3`, about 18 s per tile) and on 48 roofs changed no sub-kind, only raised confidence for 15 through the "no house proposal" negative signal. Rules were corrected once before first publication after a visual review of a random per-kind sample: the double-width container branch caught ordinary sheds, "water tank" caught square pyramid-roof sheds (a square has circularity 0.79; SAM outlines round to 0.90 to 0.95), and the see-through carport/pergola measure fired on solid roofs. Suburb after correction (5,248 eligible of 10,072 owned roofs): not-secondary 3,254, garage 1,241, fragment 331, shed 284, outbuilding 112, shipping-container 14, pergola-or-shade 9, carport 3, water-tank 0 (circularity now 0.96; no round tank is detected as a building at 4 px/m). A fresh random sample of 48 flagged roofs (seed 7): garage 30, shed 7, outbuilding 7, fragment 4, with the sheet under `data/reviews/eyes/structure-kinds/`. The user's example 118440-80408/7fdd138f is `garage (strong)`: 7.6 x 9.4 m ribbed box, same-property secondary; a single shipping container is 2.4 m wide, so the geometry rules do not call it one. Three of the 14 container hits in tile 118454-80421 look like a caravan and boats, which share container dimensions.

Surfacing: `/api/scenes/hints?scene=&object=` returns class hints and the structure sub-kind; the editor's Object details shows "Agent hints: Secondary structure. Garage (strong). Not labels; change the Class above if you disagree."; /queues shows structure badges and a count line; `task verify:scene-hints`. Pipeline: `scene_house.pipeline_context_hints` now runs `structure_kinds.assess_scene` (no SAM3) after the class hints and, for sub-kinds in `FLAT_RULES['context']['flat_hints']` (shipping-container, water-tank) with at least moderate confidence, passes the sub-kind as the context hint so `flat_evidence` selects the flat template by structure identity (rule `structure`, ridge veto still applies); recorded in `analysis.context_hints.by_structure`.

Enhanced image into SAM3 (`scripts/ml/clahe_detector_experiment.py`, report `data/reviews/eyes/experiments/clahe-detector-v1/REPORT.md`). Paired isolated re-detection on four tiles with the CLAHE frame fed only to the whole-frame encode; the control reproduced production 150 of 150 objects byte-identical. Buildings: 218 of 233 matched at median IoU 0.96 with no identical ring, 15 lost and 16 gained with no consistent direction, detector scores fell on 142 and rose on 76; the user's example survived unchanged (IoU 0.95, score 0.501 to 0.502); on the school tile six roofs of 300 to 800 m² came back as "solar_panel"; 27 Cambridge agreement with the CRM override fell from 0.82 to 0.70. Decision: not adopted, neither as a replacement nor as a second pass. Enhanced contrast helps the reviewer's eyes, not SAM3's. Next candidate experiment: feed the CLAHE or Canny edge image only to boundary refinement, judged against the T1/T2 references; for non-roof candidates the measured lever is the class and structure hints, not imagery.

## Improvement round 3, 26 September 2026 (address aggregation, structure kinds v2, fragments, two negative imagery results)

Structure kinds v2 (`structure-kinds-v2`). New sub-kinds container-stack (2, 3 or 4 container widths within 0.3 m and a 20 ft or 40 ft length within 0.5 m, rectilinearity 0.90), portable-classroom (2.8 to 4.2 m by 6 to 13 m with non-residential context), pool-cover (bright saturated blue share 0.5 or a SAM3 pool proposal) and carport (12 to 45 m², beside a larger roof, driveway-like paving share 0.15 within 3 m, always weak). A first pass let container-stack fire on 530 ordinary corrugated garages because rib anisotropy aliases at 4 px/m (a container's 0.28 m corrugation is one pixel), and pool-cover on 54 dark blue Colorbond roofs; both were tightened before publication. Final suburb counts of 5,248 eligible: garage 1,233, fragment 331, shed 279, outbuilding 112, container-stack 23, shipping-container 14, pergola-or-shade 12, carport 3, portable-classroom 1, pool-cover 0, water-tank 0; 30 rows differ from v1. The user's example 118440-80408/7fdd138f stays garage (strong): 7.63 m is 0.13 m outside the three-width band and 9.41 m is neither a 20 ft nor a 40 ft length; 618 suburb objects share that "width near three containers, non-standard length" profile. Capture scenes (house, map and training) now carry rows too (3,226). The queues hide fragments by default with a "Show fragments (N)" control and honest denominators; future calibration plans skip roofs under 6 m² (`reference-calibration-v2`, plan 4912f52a7334ecbd replaces the one 1.2 m² sliver of plan 7cfed4d9 and keeps the same 67-slot design; the old plan folder remains).

Address allocation (`scripts/ml/address_allocation.py`, `address-allocation-v1`, `task eyes:address-allocation`) and per-address enrichment (`scripts/ml/address_enrichment.py`, `address-enrichment-v1`, `task eyes:address-enrichment`). Over the 10,339 inventory roofs the rule chain (saved link, Vicmap property with one G-NAF address, numbered property polygon holding 0.85 of the roof vouched by its own address point or unique name, secondary structures inheriting the primary roof's address, unit lots, address point inside the roof) moves the statuses from matched 8,161 / ambiguous 1,985 / shared 187 / unmatched 5 to matched 8,812 / ambiguous 1,282 / suggested 53 / shared 187 / unmatched 5. The main data finding: 1,935 ambiguous roofs sat inside a numbered Vicmap property stacked under an unnumbered "CAMBRIDGE ROAD" site polygon; unnumbered sites, common-property lots and multi-point polygons never vouch for an allocation. Agreement checks (not accuracy): saved link 1 of 1, CRM feature 9 for 27 Cambridge 1 of 1, T1/T2 references with an address 4 agree, 1 disagree (a neighbouring unit at 63 Cambridge), 4 unallocated of 9. G-NAF coverage caps G-NAF ids at about 2,900 roofs; Vicmap names are kept as full_address otherwise. Per-address documents (2,336 files under `data/reviews/eyes/address-enrichment/addresses/`) carry primary roof, structures with sub-kinds, solar, trees, pool and the allocation sentence; `GET /api/address/enrichment?address=` serves them and the map sidebar shows "Property structures" for the selected address (`task verify:address-enrichment`). Example, 24 Cambridge Road: hipped primary roof 129 m² (high band), three garages, one shipping container 22 m², two sheds, five solar detections 22 m².

Two negative imagery results close the enhancement line for now: the CLAHE frame into SAM3 detection (round 2) and, this round, enhanced or edge frames into outline refinement (`scripts/ml/edge_boundary_experiment.py`, `data/reviews/eyes/experiments/edge-boundary-v1/`): the production refiner reads no pixels for the exterior (50 of 50 outlines byte-identical under every frame), and an experimental bounded edge-snap moved agreement with 50 human references by a median of 0.000 on the original frame and by −0.003 on CLAHE, with every confidence interval straddling zero. Where references disagree with the model (43 of 50 beyond 0.45 m Hausdorff) the cause is metre-scale detector misses that no bounded refiner reaches.

L/T topology (`roof-topology-support-v2`, `roof_topology.two_rectangle_supports`). A two-rectangle L or T support is offered when no single rectangle covers 85 percent of the outline; gates as before plus IoU 0.8 and a 4 m boundary deviation (traced corners are chamfered; 1.5 m yields nothing). Paired: suburb-200 32 of 200 changed (planes median 10 to 9), cluster 9 of 59, family-42 1 of 36 (label agreement unchanged), flagged 1 of 11 (units block). The four flagged L/T roofs are unchanged: their fitted hips carry no brightness step (0.2 to 3 L), so the supported-crease share gate (0.5 floor, at least the current share) refuses the simpler plan while the crease fan collects spurious contrast credit. Decision pending: relax the floor for two-rectangle plans or accept human edits for these roofs. Sheet `data/reviews/eyes/roof-eval/support-v2/changed-before-after.png`.

Topology support-v3 (user decision: relax the L/T gate on optical validation). A two-rectangle plan is accepted when either gate set passes: the v2 contrast-share set, or the optical set in which a crease counts as supported when the image shows a brightness step across it or a detected line along it (1 m, 10 degrees, half the crease covered), the plan explains at least as much observed roof-line length as the fan within 0.10, leaves no more unsupported creases, and has at least one supported crease. Paired against v2: flagged set 3 of 11 change (666256df 22 planes/44 creases to a 6-plane L; 19e7744e 12/21 to a 7-plane T, line precision 0.24 to 0.27 and recall 0.42 to 0.25 against the user's edit; 654dd0e3 gets a gabled T on what is a flat deck), d2e96c3a still fails both sets (share 0.31 vs 0.52) and 27 Cambridge has no two-rectangle plan; suburb-200 24 more roofs change (planes median 9 to 6, creases 15 to 9), cluster 6, family-42 none with label agreement unchanged; every v2 acceptance is kept (`accepted_by` records contrast-share, optical or both). Wrong-looking on the sheet: the tree object f5c74b7e and two flat commercial roofs now receive pitched plans they previously errored out of; those are class and form questions, not topology ones. `data/reviews/eyes/roof-eval/support-v3/`.

Structure review loop (`scripts/ml/structure_kinds_review.py`, `task eyes:structure-review`): review cards, tiered observations in the index (`structure-kinds-agent-review-v1`; agent = tier 3, never training-eligible; human-observed 2; human-confirmed 1), agreement counts and a training export gated on tier. The agent's own pass agreed with the v2 hints on 32 of 48 flagged roofs; "outbuilding" behaved as a catch-all (0 of 7 agreed, mostly garages) and 4 of 16 single-width container hits were caravans or boats. SAM3 fine-tuning is not possible from the frozen int8 export and has no label source yet; a classifier head over the frozen embedding, trained on human-confirmed rows, is the cheaper first step.

## Improvement round 4, 26 September 2026 (confidence tiers, reflections, optical layers)

Decision policy (`scripts/ml/decision_policy.py`, `decision-policy-v1`, `task eyes:decision-policy`; index table `decision_policy`). Every owned building gets a machine label (kind, structure sub-kind, class hint, roof form, roof type), a stated confidence and a tier: auto (proceed without a human), assist (show the label, a human glances), review. Auto requires band high, a house/commercial/units hint with no conflicting sub-kind, allocation matched at strong or moderate, no clip and a passing geometry audit, or a strong secondary-structure sub-kind. Over 14,044 owned buildings: auto 102 (22 primary roofs, 80 garages/sheds/containers), assist 7,386, review 6,556; only 31 roofs sit in the high band, so the primary auto path is inherently small. Reflection (`--reflect`, `REFLECTION.md`, `proposals.json`): humans have decided 62 buildings with a machine row; assist 6 of 9 confirmed (Wilson 0.35 to 0.88), review 40 of 48 (0.70 to 0.91), auto 0 judged. The review tier scores higher because 53 of the 57 judged roofs are training captures that were never band-assessed and land in review by construction; the humans confirmed them. Proposals, not applied: assess those captures, then review a sample of at least 10 auto and 10 assist roofs before any threshold moves. `--apply-auto --dry-run` lists 102 labels the auto tier would write into review.json as confirmed with provenance `machine:decision-policy-v1` (65 garages, 18 hipped house roofs, 13 sheds, 4 unit roofs, 1 container, 1 stack); live writes need an explicit flag and the user's go. The editor shows "Machine label: garage, assist tier, confidence 0.74" with a "Use machine label" button that applies the label without saving; /queues shows tier badges, a count line and a "Hide auto tier" filter (`task verify:machine-labels`; the /queues half of that check times out under machine load above 50).

Optical layers (`scripts/ml/optical_layers.py`, `optical-layers-v1`, `task eyes:optical-layers`; `roof_line_fit` EXTRA_VIEWS_VERSION `registered-roof-lines-v8-layer-evidence`). Eight deterministic, hash-bound layers of the saved image (bilateral, CLAHE 12, unsharp, difference of Gaussians, gradient, shadow-normalised, guided, and the multi-date median where an accepted stack exists). Segments detected on a `layer:` view may pass the evidence gate scored on that layer when the original image samples at least six roof points there. Paired against no layers on five sets: 19e7744e improves against the user's edit (line precision 0.27 to 0.46, recall 0.25 to 0.50, two difference-of-Gaussians segments), house-30296 worsens (0.88/0.75 to 0.82/0.70), house-30286 unchanged; family-42 agreement flat at 27 of 38 with four type flips cancelling; suburb-200 120 of 200 annotations change with 18 type flips and no reference to sign them; propose_roof takes 3 to 4 times longer; and layer detections enlarge dense parallel families so the seam filter drops genuine original segments (two suburb roofs lost all evidence). Decision: not adopted as pipeline evidence, neither all layers nor the cheap subset (which changes no reference agreement at all). The measured lever, if pursued, is difference-of-Gaussians and CLAHE 12 with an origin-aware seam filter, and it needs more than the three paired human roof-line edits that exist. Gaussian splatting remains inapplicable to single-view orthomosaics. Report `data/reviews/eyes/experiments/optical-layers-v1/summary.json`.

## Round 5, 27 September 2026 (machine labelling switched on, intervals on labels)

The user approved live machine labelling. `decision-policy-v2` attaches two intervals to every machine label: `interval`, the Wilson 95 % human-agreement interval for the label's stratum (tier and label kind, from review.json decisions and T1/T2 references, with machine-provenance objects excluded so labels never feed their own interval; null bounds and an explicit note when nobody has judged the stratum), and `score_interval`, a stated design range around the confidence point (half the gap to the neighbouring band or structure-confidence value, widened by 0.03 per missing sidecar). `--apply-auto` live wrote the 102 auto-tier labels as `confirmed` with provenance `machine:decision-policy-v2` (65 garages, 18 hipped house roofs, 13 sheds, 4 unit roofs, 1 shipping container, 1 container stack) into 79 review files (73 created with every model object present, 6 updated in place), under the web's review lock, with a backup folder, a per-object audit (`data/reviews/eyes/decision-policy/apply-auto-live.json`) and `--rollback-auto`. One write landed in the protected reference capture house-30296 and tripped the eyes guard; that file was restored from the backup, so 101 machine labels stand and the guard passes. Two consumers were fixed the same day so machine labels never masquerade as human decisions: `reference_registry.review_tier` grades machine provenance as T3 (`review:machine_label`, 101 rows; T1 stays human), and `reference_calibration.classify` does not count them toward calibration progress (6 machine-labelled roofs sit in plan 4912f52a). No auto-tier stratum has a human judgement yet, so every written label shows null agreement bounds and only its model range. The user reviews them on the /queues "Machine labelled" table (102 rows, links into the editor, no controls); `task verify:machine-labels`.

Label supply and a classifier head (27 September, built on a direct request). `scripts/ml/structure_kinds_signals.py` (`structure-kinds-signals-v1`, `task eyes:structure-signals`) adds per-structure signals the rules lacked: rib period measured on crops rotated to the outline's long axis (265 of 1,222 objects on the reviewed tiles, where the axis-agnostic measure returned almost nothing), the see-through ratio in LAB on the original image, roof continuity across a shared boundary for attached candidates (chromaticity step and eave-line strength; the 6-unit threshold flags 1 of 229 and is probably too strict), multi-date presence from the agreement sidecar, and optional pooled SAM3 embeddings per object (`--embed`, one encode per tile). `scripts/ml/structure_kinds_model.py` (`structure-kinds-model-v1`, `task eyes:structure-model`) is a multinomial logistic head over hint evidence, signals and embeddings, evaluated with tile-grouped folds so neighbours do not leak; it trains, evaluates and scores only on human tiers 1 and 2, and `--allow-agent-labels` is a labelled smoke mode. Smoke on the agent's own 47 tier-3 observations: 32 of 47 agree (garage 23 of 26, shed 1 of 9), which shows the pipeline works and nothing about quality. The binding constraint remains label supply: zero human-confirmed sub-kind labels exist; `task eyes:structure-review CLI_ARGS="record --tier human-confirmed ..."` is how they enter.

Optical cascade for structures (27 September, built on a direct request; `scripts/ml/structure_kinds_optical.py`, `structure-kinds-optical-v1`, `task eyes:structure-optical`). Roof-localised deskewed crops, optionally re-fetched at provider zoom 20 (0.118 m per pixel, cached under `data/imagery/google/20`) then CLAHE, Lanczos 2x and unsharp (no learned super-resolution: reconstructed edges are not evidence); scene-text recognition with RapidOCR (ONNX, CPU) at two orientations and a watermark filter, because the first run's "roof text" was the tiled Google watermark; SAM3 vehicle prompts (car, caravan, boat, truck) whose first result (4 of 4 observed vehicles, 0 of 11 non-vehicles) did not replicate on a fresh 48 (22 of 48 flagged, since SAM3 returns a high-IoU proposal for almost any box under almost any prompt), so the boolean is conservative and the raw scores are stored for a learned head; and relative shadow heights, which sit at the noise floor in this near-nadir imagery (median primary-roof shadow 0.47 m, two pixels). Measured on this suburb: no legible roof text on 16 small structures or 6 commercial roofs. The dependency `rapidocr-onnxruntime` is now pinned in `scripts/ml/requirements.txt`.

## Round 6, 27 September 2026 (bugs from the first review session; subagent capacity exhausted until 1 October)

The user's review of house-25822 (7 Greenville Street) surfaced six issues; fixes were made inline. Structure kinds v3 (`structure-kinds-v3`): a small solid roof attached to the primary roof is an `attached-cover` (label "Patio or carport cover"; 6 to 30 m², rectangle fill 0.55, after the carport and see-through rules), and the shed and garage rectangle-fill floors drop from 0.75 to 0.65 because SAM outlines of small structures are wobbly (a 28.6 m² shed traced with 36 vertices fills 0.69). On the reviewed capture the detached box becomes shed (strong) and the patio cover and carport become attached covers (strong); suburb counts of 5,248 eligible: garage 1,354, shed 302, fragment 331, attached-cover 35, outbuilding 29; captures gain 459 attached covers. Allocation, enrichment and the decision policy were re-run on v3 (the decision policy now reads v3, v2 and v1 with the newest row per object winning). Editor: a "Structure kind" select on buildings writes `structure_kind` and the matching label (validated in the review schema); "Square up outline" replaces the selected boundary with its minimum rectangle; drawing an outline shows the live segment from the last corner to the cursor and the closing edge; in the roof-line editor the draw tool no longer lets existing lines and points capture the click, and a notice explains when a roof reaches the capture edge (the two "cut off" roofs sit on the 512 px capture's edge; the fix is Analyse surroundings, which expands the capture with the saved pixels preserved). The Enhance roof detail toggle worked headlessly in 0.5 s; the page now warms the contrast-view cache when a scene loads so the toggle answers at once. Lint ignores `.run/` and `data/` copies of the source tree that had started to inflate the count.

Reflection gap closed (27 September, evening): a reviewer pressing Looks right on a machine-labelled roof left no trace, because the status was already confirmed. The editor now stamps `provenance.human_confirmed` and `human_confirmed_at` on such objects, `decision_policy.is_machine_labelled` treats them as human decisions, and the review signal carries `human_confirmed_machine_label`; the Machine labelled table then reads "Confirmed by reviewer" after the next policy run. `task eyes:review-cycle` runs the whole assess, refresh, policy, reflection, proposals sequence idempotently; its first cycle recorded 658 human decisions as the baseline (auto tier: 0 of 1 judged confirmed; review tier 46 of 55).

Higher-resolution imagery (27 September; `scripts/ml/imagery_sources.py`, `task eyes:imagery-sources`). Measured around 27 Cambridge Road: the saved scenes are limited by their 0.236 m pixel (edge sharpness 0.28 m); Google zoom 20 reaches 0.17 m edge and 0.13 m smallest resolvable bar, zoom 21 0.12 m and 0.10 m, zoom 22 is interpolation; Esri World Imagery is 0.34 m satellite resampled; Vicmap Basemaps aerial stops at level 20 (0.118 m, about Google z20) and its licence carries an annual fee; Nearmap (5.5 to 7.5 cm, quote, reported AUD 2,000 to 5,000 per year) and MetroMap (AUD 2,640 per year, API on Enterprise only) are the paid steps up; Mapbox, Azure Maps, MapTiler, Planet, Maxar Open Data, OpenAerialMap add nothing free here. Winner for review rasters: Google z21 at 4x the saved grid, registered to the saved PNG by per-window phase correlation with a hold-out residual of 0.03 saved pixels (7 mm) and no warp. Licensing caveat: direct tile access is not licensed outside Google's Maps APIs; the Map Tiles API free tier (100,000 tiles a month) would cover the suburb at z21 once a month. Paired on the 48 fresh-sample structures: oriented rib period measurable on 7, 11 and 14 of 48 at saved, z20 and z21 (periods under 0.6 m: 0, 3 and 8, of which 5 are real ribbed roofs), rib anisotropy higher on 32 of 45 at z21; the outline edge-support check does not improve because the outlines were traced at 0.236 m (the true-versus-displaced margin falls from 0.13 to 0.09). No option resolves 76 mm corrugated iron, which needs pixels of 3.8 cm or finer. Hi-res rasters are review material, not evidence.

Outline-model training kit (27 September; `scripts/ml/training/`, `task training:validate`, GPU run via `scripts/ml/training/deploy/run.sh`). Dataset-v1: 562 images split by z17 tile (seed 20260927): train 300 images with 12,798 SAM3 pseudo-labels, val 49 with 2,216, held-out test 213 with 3,022 pseudo-labels plus all 65 human outline placements (every tile holding a human reference is test, so training currently sees pseudo-labels only and the higher human weights have no effect yet); the 101 machine-provenance objects are ignore pixels, and a one-pixel polygon-fill bias worth about 0.3 m of Hausdorff was removed. Model: torchvision Mask R-CNN ResNet-50 FPN v2 with a 56 by 56 mask head per roof (SAM3's masks are 288 by 288 for a 1152 px tile, one cell per 0.94 m, which is why corners round); YOLO-seg rejected on licence, SAM2 on prompt cost, Mask2Former on export; SAM3's PyTorch checkpoint is gated. Smoke training runs on CPU and MPS in under a minute, ONNX export matches torch to 3e-5 px, inference runs in the main venv in about 12 s per tile through a SAM3-shaped proposal interface, not wired into production. SAM3 control on the 58 same-image references: 53 matched, 11 within 0.45 m Hausdorff, median 0.85 m. Expected GPU cost about USD 1 to 3 with hard caps of 180 minutes (A10) or 155 (A100); the dataset tarball is hash-pinned (846 MB). Risks: training on pseudo-labels alone may reproduce SAM3's rounding; early stopping measures agreement with SAM3, not people; 58 references give wide intervals.

Building heights (27 September; `scripts/ml/heights_source.py`, `task eyes:heights`, rows `heights-v1` in object_quality). Result: no open source gives per-building heights here. The Greater Melbourne LiDAR (2017, 1 m) and the Digital Twin Victoria LiDAR and 3D buildings are licensed; ELVIS lists nothing finer than the 10 m Vicmap DEM and SRTM for this box; SRTM and Copernicus 30 m are useless for buildings. Other open sources were fetched, cached under data/elevation/ with provenance, and rejected as height evidence with reasons: OSM tags (174 owned roofs, 209 of 312 are exactly 4.0 m), Microsoft ML footprint heights (7,278 of 10,072 covered, compressed to 4.2 to 5.0 m, Spearman 0.08 against OSM), ICESat-2 photons (median roof minus ground 1.9 m, registration held on 1 of 28 passes), Vicmap Features (no heights), shadows (noise floor). The Depth Anything fallback did not earn adoption: error 0.515 m against Microsoft heights versus 0.517 m for a constant, worse than constant against OSM, and the relief tracks footprint size (Spearman 0.48 with area); the 964 heights in public/address-roofs.json come from the same depth model with a forced 7.0 m tile median and are circular as a reference. Every owned roof therefore keeps the 3.2 m prior except 174 with an OSM tag. The module ranks LiDAR first: drop licensed DSM and DEM tiles into data/elevation/, register them in the manifest, install rasterio, and rerun `--assign`; roof height is the median of the top half inside the outline eroded 1 m, eave the lower quartile. Contact sheet and fit files under data/reviews/eyes/heights/.

## Profile promotion, 27 September 2026 (`scripts/ml/promote_profile.py`, `roof-promotion-v1`, `task eyes:promote`)

The sidecar-evaluated fitter (roof-form-flat-v3 with structure flat hints, roof-topology-support-v3, class hints building-flags-v2 and structure-kinds-v3 as context hints) was promoted into production latest.json for 283 of 291 scenes with roof models (253 scan tiles, 30 captures), without re-running SAM3, with per-file backups, a per-object audit, a rollback command and a passing eyes guard. Skipped by rule: three scenes with human decisions (house-25822, 118443-80416, 118443-80417), the protected reference captures house-30286 and house-30296, and the T1/T2 reference scenes 118443-80412, 118443-80415 and roof_training_250-29789. New profile id f42a9d62…; confidence profile a8dba1dd… for scan tiles and b207e650… for captures; the frozen population (10,531 rows under 53ea49d5…) is kept intact in the new `profile_roofs` and `profile_scenes` tables, and `reference_calibration.owned_roofs` now reads the frozen profile from there. Effect on 15,547 buildings in the rewritten files: geometry changed on 5,821; roof_type changed on 2,079 (hipped to gabled 767, hipped to mixed 765, hipped to flat 315, gabled to flat 91 in the dry run); the flat hypothesis fired on 416 roofs (bright 162, large 146, near-flat unresolved surface 79 from flat-v3, structure 29); topology support accepted on 5,405 (both gate sets 2,165, optical 1,726, contrast-share 1,514; shapes L 3,391, rectangle 1,393, T 699); median planes 10 to 7 and creases 17 to 10; confidence bands moved for 32 of 10,312 owned roofs. Where neither stage fired, geometry is byte-identical; notes and form evidence change everywhere. Two flat calls on the changed-24 sheet look wrong to the implementer (a terracotta roof and one other); a 24-roof sheet of flat flips is at data/reviews/eyes/promotion/flat-flips-24.png for the user. Consequences handled after the apply: the 67 review files that hold only machine labels trailed the new model version (carried forward, see below); the eyes-index sidecars for the rewritten scenes were signed by the old model and are refreshed by `task eyes:review-cycle`; optical guidance stays disabled in this profile (`roof_optical_guidance: disabled`). Rollback: `python -m scripts.ml.promote_profile --rollback data/reviews/eyes/promotion/apply-live-20260927T122802Z.json`.

Carry-forward after promotion: `node scripts/carry-forward-reviews.mjs [--dry-run] [--include-human] [--scenes ...]` reuses the editor's own recovery path (GET /api/scenes?id= gives recovery.review, POST /api/scenes saves it) for review files whose model version trails the scene; it touched the 67 machine-labelled-only files (revision 2 each) and skipped every file with a human decision. Flat flips from the promotion: 281 owned roofs (bright 106, large 104, near-flat appearance 52, structure 19); on a random 24 of the 256 suburb ones four are clearly pitched houses (my observation), so the flat-v3 rule set is under review by the user before it is trusted; the list is data/reviews/eyes/promotion/flat-flips-owned.json.

Incident, 27 September evening: carrying the 67 machine-labelled review files onto the promoted models through the editor's recovery path downgraded each machine label to needs_adjustment with geometry_reviewed false, because the recovery treats a changed roof geometry as something a person must re-check. For a machine label that fabricates a human "needs edit" decision; the next reflection reported 85 auto-tier roofs judged and none confirmed. The 88 objects were restored to the machine's own status with provenance intact and a carry note (backup and audit under data/reviews/eyes/decision-policy/carry-restore-20260927T130634Z.json), and `scripts/carry-forward-reviews.mjs` now keeps the machine status when it carries a file. Reflections computed between the carry-forward and the restore are not to be trusted.

## Free fine pixels and the teacher-student bridge, 28 September 2026

No free aerial imagery exists for Mooroolbark: Yarra Ranges publishes none, Victoria's programme is licensed, and Nearmap flies its own aircraft with no upstream (Google's Melbourne tiles are Vexcel aircraft imagery). The only open Victorian fine-pixel orthos are the City of Melbourne's CC BY 4.0 true orthophotos (2018 at a measured 0.100 m per pixel despite the catalogue's 7.5 cm, used here; 2019 at 5 cm and 2020 at 1.9 cm also CC BY, found later the same day; the CBD municipality only), cached under data/imagery/com-2018/ with hash and attribution. `task training:distill` (scripts/ml/training/hires_teacher.py and distill_dataset.py) cuts 60 building-rich windows of 272 m, runs SAM3 on the native 10 cm pixels in nine overlapping sub-tiles as the teacher and on the area-averaged 0.236 m student image as the control, and emits the teacher masks as labels for the student images in the dataset-v1 format (distill-v1: train 42 windows with 4,980 instances, val 9 with 911, test 9 with 938; provenance hires-teacher-com-2018, tier 3, weight 1.0; multi-part remainders are ignore pixels). Premise measured on 2,679 matched pairs (agreement between two SAM3 runs, not accuracy): right-angle corner share 0.251 teacher versus 0.114 control at 0.5 m simplification (0.123 versus 0.070 at 0.25 m), a median of one more square corner per building, nine fewer vertices at a common tolerance, rectangle fill 0.866 versus 0.846; downsampling the teacher label to the student grid keeps most of the squareness (0.230). The teacher finds 2.4 times as many instances as the control, most unmatched and unchecked. Blur: none applied; the area-averaged students are already softer than the saved Google tiles (edge width 0.325 m versus 0.265 m; an unsharp mask of about 0.76 would match and is recorded, not applied). Domain gap: inner-Melbourne 2018 terraces and industrial roofs, only 50 matched pairs under 50 m². Next: train the outline model on dataset-v1 plus distill-v1 and judge on the 58 Mooroolbark references; licence for a commercial product still needs licensed imagery because Google's terms forbid deriving datasets from its tiles.

## SAM3 on the Mac, 28 September 2026

The user asked whether SAM3 itself can be trained on the M5 Max instead of a rented GPU, using the gated
`facebook/sam3` checkpoint and the `MaximeLglr/sam3-apple-silicon` fork. Findings, all under
`scripts/ml/training/sam3_mac/` (fork vendored and gitignored, pinned in `VENDOR.json`):

- The fork is upstream SAM3 at 84cc43bc (19 November 2025) plus a packaging commit and an image-only Mac inference
  commit (Triton EDT optional, coordinate caches on CUDA-or-CPU, `model.to(device)` for any device string). Video,
  evaluation and test code are still in the tree. Nothing targets MPS; the trainer is CUDA + torch.distributed +
  Hydra + submitit, and the focal loss imports Triton unconditionally. Upstream at 2026-09-18 has no MPS references.
- Two patches make the model and loss build on Apple Silicon (`patches/`): Triton made optional in
  `sigmoid_focal_loss.py` with the reference torch focal loss as fallback; the decoder's box-RPB coordinate cache
  created on MPS and moved to the reference boxes' device on a mismatch.
- `finetune_mps.py` is our own single-process loop over the fork's dataset, transforms, matcher and loss (loss
  normalisation `local`, no process group). `prepare_coco.py` turns dataset-v1 + distill-v1 into SAM3 COCO folders
  with the production prompt `building roof` as the category name: train 342 images / 17,778 instances, val 58 /
  3,127, test 222 / 4,025 (all human references stay in test).
- Dry-run with random weights at the native 1008 px, batch 1, backbones frozen (807.8M frozen, 32.7M trainable):
  forward, backward and AdamW step complete on MPS with the CPU fallback disabled. Uncontended: 2.1 to 2.3 s per
  step in fp32 and 1.3 to 1.5 s in bf16 after a 10 to 15 s first step, 3.9 GiB allocated, 10.7 GiB driver memory.
  With three SAM3 teacher processes saturating the CPU at the same time the same steps took 2.2 to 4.5 s. Per-step
  losses on random init (963, 476, 693, 512) are image-to-image variation, not a learning signal. A 300-step
  fine-tune is therefore 7 to 12 minutes on this machine. A 504 px shortcut is not possible: the ViT's rotary
  embeddings are precomputed for 1008 px.
- Two false starts worth recording. Ten of the first twelve dry runs died with non-finite Hungarian costs; the cause
  was the builder allocating CLIP's positional embedding and text projection with `torch.empty` and relying on the
  checkpoint to fill them, so `--random-init` now initialises any non-finite parameter. Along the way the matcher's
  `stable` flag was flipped on a misreading (with the fork's `stable=False` the class cost already uses logsigmoid);
  it is back at fork parity. After both fixes fp32 and bf16 runs complete with zero skipped batches on two seeds.
  The loop also skips a step whose gradient norm is non-finite, as the fork's GradScaler would.
- Blocked on one thing: the checkpoint is gated and `hf auth login` has not been run on this machine. After login,
  `task training:sam3-mac:fetch` (3.45 GB), then `task training:sam3-mac:smoke` measures the fp32 PyTorch model on
  MPS against the production int8 ONNX on the same saved tiles (counts, matched IoU, right-corner share, seconds per
  image; the two arms threshold different scores, so counts are not like-for-like and the summary says so), and
  `task training:sam3-mac:finetune -- --steps 300 --dtype bf16` runs the first real fine-tune into
  `data/reviews/eyes/training/sam3-mac/run-001`. The judge of any checkpoint remains the 58 Mooroolbark human
  references, never the teacher masks.

## Teacher sources beyond Melbourne, 28 September 2026 (`teacher_sources.py`, `teacher-sources-v1`)

The user set the direction: no paid imagery; find open fine-pixel sources of roofs, trees and residential objects,
fly their own drone over 27 Cambridge Road, and get SAM3 training onto the Mac. A research agent checked 47 sources
on their official pages (`docs/teacher-sources-catalogue-2026-09-28.md`, JSON under
`data/references/teacher-sources/`). Strongest ML-ok finds: City of Melbourne 2020 true ortho at 1.9 cm (CC BY 4.0,
one 49 GB JPEG2000), the CoM 2018 point cloud and photomesh (the only open sub-metre heights in metro Melbourne),
Vicmap's 16,848 machine-derived tree points with LiDAR heights inside the Mooroolbark bbox (CC BY 4.0), ACT
imagery at 5 to 7.5 cm (CC BY 4.0, ArcGIS ImageServer export) with ACT LiDAR, NSW imagery (CC BY 3.0 AU), and LINZ's
public S3 bucket: Christchurch, Auckland and Wellington at 5 to 7.5 cm plus a 1 m DSM, all CC BY 4.0. Microsoft's
footprints turned out to be CDLA-Permissive-2.0, not ODbL (`heights_source.py` corrected). Research-only sets
(Semantic Drone, UAVid, ISPRS, iSAID, RID v1) are listed and excluded.

`scripts/ml/training/teacher_sources.py` is the registry and window cutter that makes any of them a teacher for
`hires_teacher.py`: `catalog` (LINZ STAC tiles with bounds fitted from the Topo50 1:500 sheet names, 0.0 m error on
four sheets; ArcGIS service metadata; a local or remote raster), `select` (seeded window lattice classified on a 1 m
overview with a suburb rule: vegetation between 8 and 45 percent, structure between 30 and 75 percent), `fetch`
(eight parallel checksummed downloads) and `crop` (native mosaic with the alpha band, or an ImageServer export in
1812 px pieces, or a finer raster area-averaged to a `teacher_gsd_m`). `hires_teacher --source ID` then runs
calibrate, teach, finalize and measure unchanged, and `distill_dataset --source ID` exports in dataset-v1 format with
source-specific provenance. Ten sources are registered; three have run end to end (City of Melbourne 2018, LINZ
Christchurch 2025 0.075 m, and one ACT window as a reader test).

First non-Melbourne premise, LINZ Christchurch 2025 at 0.075 m, 60 suburban windows, 6,288 matched pairs (agreement
between two SAM3 runs, not accuracy):

| measure | teacher native 0.075 m | teacher label on the student grid | control (SAM3 on 0.236 m) |
|---|---|---|---|
| right-corner share, 0.5 m simplification | 0.488 (95% window bootstrap 0.467 to 0.507) | 0.394 | 0.101 (0.089 to 0.115) |
| right-corner share, 0.25 m simplification | 0.226 | 0.134 | 0.047 |
| right corners per building, median | 4 | 3 | 1 |
| vertices at a common 0.25 m tolerance, median | 12 | 20 | 23 |
| rectangle fill, median | 0.863 | 0.848 | 0.825 |

Paired: +3 right corners per building (interval 3 to 3), 10 fewer vertices, fill +0.029; by area the gap holds for
sheds under 50 m² (0.286 vs 0.061 on 849 pairs), houses (0.232 vs 0.045 on 4,477) and large roofs (0.195 vs 0.047
on 962). The Melbourne 2018 premise was 0.251 vs 0.114: detached suburban housing at 7.5 cm gives SAM3 nearly five
times the square-corner share of the pipeline's pixels, against about two times in the inner city. The teacher again
finds twice as many instances as the control (12,679 vs 6,447), unmatched ones unchecked. Blur calibration matched
Melbourne: none (students already softer than the saved tiles). Export `distill-linz-chc-2025-0075`: train 42
windows / 8,547 instances, val 9 / 2,157, test 9 / 1,974, provenance `hires-teacher-linz-chc-2025-0075`, tier T3.

Costs: the LINZ bucket delivers about 1 MB/s to this machine (195 tiles, 3.7 GB, about 40 minutes with eight
workers); teaching a 0.075 m window takes 16 SAM3 sub-tiles, about 300 s with three shards sharing the CPU. The ACT
2023 run (37 windows selected out of 400 candidates; Canberra is leafier, 362 rejected for low structure) started
after Christchurch and its exports run at about three minutes per window.

A review workflow (four reviewers, six adversarial checks) over the day's code confirmed and fixed: window caches
keyed only by id while ids depend on the selection bbox (now keyed by tiles and georeference, and teach re-checks the
native hash), `rasterio.merge` dropping the last row or column of a requested box (padded and cut back),
three-band sources getting a constant alpha (now from the data mask), the smoke test calling a method the ONNX
segmenter does not have, and the SAM3 loop's matcher flag flipped on a misreading. Two drone findings changed the
plan (`docs/drone-capture-27-cambridge.md`): CASA's populous-area rule and its 30 m-from-a-house guidance mean a
business-purpose flight over the lot needs a licensed operator with an over-people approval today, and the
`drone-ortho` source needs a lot-sized window mode before a single-lot orthomosaic can be a teacher.

Addendum, later on 28 September: the ACT 2023 run finished (37 windows, 2,243 matched pairs; ImageServer exports
were quick once the LINZ download had stopped competing for the link). Right-corner share at 0.5 m simplification
0.390 teacher versus 0.112 control (95% window bootstrap 0.368 to 0.413 versus 0.100 to 0.124), +3 right corners
per building, 16 versus 26 vertices, fill about equal (0.826 versus 0.817); sheds under 50 m² 0.308 versus 0.072 on
227 pairs. Export `distill-act-2023-urban-75mm`: train 25 windows / 3,241 instances, val 6 / 952, test 6 / 849.
Three sources, three positive premises: Melbourne 2018 inner city 0.251 vs 0.114, Christchurch 2025 suburbs 0.488
vs 0.101, Canberra 2023 suburbs 0.390 vs 0.112. Still agreement between two SAM3 runs, not accuracy.

## Teacher enhancement round, 28 September 2026 (`teacher_judges.py`, `teacher-judges-v1`)

The user asked to enhance the teacher. A design panel (three designs, two judges, one synthesis) set the rule that
every enhancement is scored by a judge that is not the shape prior it optimises, and ranked six items
(`docs/teacher-enhancement-plan-2026-09-28.md`). Built tonight: the judge kit (`scripts/ml/training/teacher_judges.py`
with `teacher_outlines.py`, tasks `eyes:teacher-outlines` and `eyes:teacher-judges`) and its first verdicts on the
two Christchurch captures, which now share one cell-keyed split (`distill_dataset` hashes the shared cell id).

- Open building outlines (LINZ, CC BY 4.0, 10,917 features over the 60 cells, clipped to the window): among teacher
  instances of 10 m² and more, 64 percent are whole buildings, 16 percent parts of a mapped roof, 13 percent touch no
  outline; the control is 84 / 8 / 4 but covers only 68 percent of mapped buildings against the teacher's 91.
- Edge support on the source pixels: teacher 0.88, control 0.53, random circles 0.27, teacher higher on all 60 windows.
- Registration between captures is image-only (phase correlation per window, blurred-gradient correlation per
  building). Roofs move 0.39 m median between the 2020-21 and 2025 orthos, so per-building offsets are required;
  correlating raw gradients matched under 1 percent of buildings, blurred gradients 56 percent.
- Cross-capture stability after registration: 74.5 percent of 2025 teacher instances have a one-to-one twin in
  2020-21 at IoU 0.5 (0.72 to 0.77), median IoU 0.89, boundary distance 0.21 m. The 2020-21 teacher's premise
  replicates 2025 (0.486 against 0.109).
- Perturbation ladder on the J1 boundary judge (outline drawn on one capture, gradient profile along the normal in the
  other capture): a 0.075 m shift separates from raw with non-overlapping intervals; a 0.30 m corner rounding does
  not lower the corner-zone score. The judge resolves offsets to one native pixel but not corner sharpness at 7.5 cm,
  so the zoom and upsampling items wait for a finer judge.
- Verdict on the shipped `segmentation_regularised` target (ungated `common.orthogonalise`): on 1,375 outline-matched
  buildings in the 18 judge cells, J1 0.380 (0.361 to 0.398) against 0.301 raw, 837 buildings higher and 262 lower;
  gated on the source capture's own edge support, 519 accepted at 0.408 with 435 higher and 84 lower. Replicated with
  the captures' roles swapped (0.387 against 0.306, 854 / 193; gated 0.413, 462 / 65). Squaring is supported by the
  second capture; the gate trades coverage for fewer regressions.
- A per-instance evidence ledger (`teacher_judges ledger`) joins score, area, nesting, window cut, outline class,
  edge support, registration and cross-capture presence with a recorded keep / part / ignore / review recommendation;
  no label changes until a policy is judged by a signal it never used (the nDSM leg is the next build).

All figures are agreement between model runs, captures and open outlines, never accuracy.

## SAM3 with the real weights, 28 September 2026 (evening)

The gated `facebook/sam3` checkpoint landed once a classic read token was passed to the CLI explicitly (the saved
token was not picked up; the fetch script now passes it). First results, all agreement or model estimates, never
accuracy:

- Production int8 ONNX against the fp32 checkpoint on 10 saved Mooroolbark tiles (`smoke-fp32`): 593 matched
  instances at median IoU 0.979, the same right-corner share (0.057), 70 against 62 detections per tile (the PyTorch
  processor thresholds a presence-weighted score, so counts are not like-for-like). The int8 export is not costing
  outline quality. Speed on MPS: 0.69 s per tile in fp32, 0.30 s in bf16, against 4.8 s for the ONNX on CPU.
- First fine-tune (`run-001`): 600 steps, bf16, batch 1, backbones frozen, on the teacher-only set
  `coco-teachers-v1` (151 training windows, 25,032 teacher instances from Melbourne 2018, Christchurch 2025 and
  2020-21, Canberra 2023). 3.6 s per step on real windows of about 180 instances, zero skipped batches, nine dataset
  rows skipped by the fork's own loader retry. The loss did not trend over 600 steps (about 2 percent of the fork's
  own schedule at its default learning rate); this run validates the loop, not a recipe.
- Reference judge (`sam3_mac/reference_eval.py`, 63 human boundary references on 44 dataset-v1 test tiles):

| arm | matched at IoU 0.5 | best IoU median | boundary distance median, matched | predictions per tile |
|---|---|---|---|---|
| base checkpoint | 60 of 63 | 0.905 | 0.22 m | 12 |
| run-001 tuned | 63 of 63 | 0.911 | 0.24 m | 25 |

  Paired per reference: tuned higher on 28, lower on 35; mean best-IoU change +0.029 with a 95 percent image
  bootstrap of -0.002 to +0.052. The tuned model detects twice as much (recall up, precision unjudged here) and its
  outlines agree slightly less on most references. Not adopted; the harness is what this run delivers.
- Next for this stream: a longer run (several thousand steps) with a learning-rate sweep, a negative-aware judge
  (predictions that match no reference on the same tiles), and the squared teacher label as a second arm.

## Approved SAM3 sweep and label arms, 28 September 2026 (night)

The user approved a longer fine-tune with a learning-rate sweep and two seeds, a negative-aware judge, and a second
arm on the squared teacher labels, and asked to proceed with the standing CP14 decisions. Running detached on MPS
(`scripts/ml/training/sam3_mac/sweep.sh`, results in `data/reviews/eyes/training/sam3-mac/sweep/results.jsonl`):

- Stage 1: raw teacher labels (`coco-teachers-v1`), 1,500 steps, learning rates 8e-5, 3e-4 and 1e-3, seed 1,
  backbones frozen, bf16. About 1.5 s per step with the CPU free.
- Stage 2, launched automatically by `stage2_waiter.sh` when stage 1 and the squared set are ready: the stage-1
  learning rate chosen by `sweep_next.py` under a pre-registered rule (highest paired mean best-IoU change on the
  human references among runs whose novel-detection share rose by at most 0.10 and that skipped no batches; ties to
  the lower rate), then raw and squared-label arms, seeds 1 and 2, 3,000 steps each.
- Judge (`reference_eval.py`): the 63 human boundary references on 44 dataset-v1 test tiles, paired per reference
  with an image bootstrap, plus a negative-aware proxy: predictions overlapping no human outline and no production
  pseudo label at IoU 0.3 are counted as novel. No human rejections exist in any review file today (the review UI has a
  Reject action that has never been used), so novel share is a proxy for false positives, not a measurement of them.
- The squared-label arm uses `distill_dataset --label-target squared_gated`: the teacher outline is replaced by
  `common.orthogonalise` at the native pixel when the source image's own edge support accepts it
  (`teacher_judges.gated_orthogonalise`), raw otherwise; each annotation records `label_variant` and keeps
  `segmentation_raw`. Exports land beside the raw ones as `distill-<source>-squared/`, so the raw exports stay
  byte-identical. `--policy ledger-v1` is also available: it removes instances the evidence ledger recommends
  ignoring and paints their pixels IGNORE (for the student kit, which understands ignore pixels; SAM3 has no ignore
  class, so that arm is not part of this sweep).

All figures the sweep produces are agreement with the listed human outlines or between model runs, never accuracy.

## Sweep stage 1 verdicts and the reviewer's first negatives, 29 September 2026

- Reviewer decisions given in chat (11 objects) were recorded through the review API with provenance
  `human_decision.source = human:chat` (`scripts/record-chat-feedback.mjs`, ledger
  `data/references/eyes-checkpoints/REVIEW-FEEDBACK-2026-09-28.json`): one flat flip confirmed, one patio detection
  rejected (the first human rejection in the project), seven structure kinds set (sheds, covers, an outbuilding, an
  extension recorded as part of the primary roof). Four candidates the tree-likeness hint had ranked as likely
  non-roofs were real sheds, so that hint over-fires on small green-shaded structures. Lot 72 of house-25822
  (101 Hawthory Road) has only a 28.6 m² shed detected while the main house is visible: a recall gap of the 0.236 m
  detector, now in the ledger. Review cycle `cycle-20260928T183132Z` counted the decisions.
- Stage 1 of the SAM3 sweep (raw teacher labels, 1,500 steps, one seed), judged on the 63 human boundary references
  with a footprint-aware negative proxy (a prediction overlapping no human outline, no production pseudo label and no
  Microsoft footprint is the closest thing to a false positive we can count):

| learning rate | matched at IoU 0.5 | best IoU median | paired change (95% image bootstrap) | predictions | novel without footprint (share) |
|---|---|---|---|---|---|
| base checkpoint | 60 of 63 | 0.905 | reference | 918 | 53 (0.06) |
| 8e-5 | 63 of 63 | 0.917 | +0.036 (+0.004 to +0.060), 35 higher / 28 lower | 1,430 | 384 (0.27) |
| 3e-4 | 61 of 63 | 0.888 | -0.003 (-0.033 to +0.023) | 1,295 | 250 (0.19) |
| 1e-3 | 0 of 63 | diverged | | 352 | |

  At 8e-5 the tuned model agrees better with the human outlines (the interval excludes zero) and finds all 63, but it
  predicts half again as many objects and its unbacked novel detections rise from 6 to 27 percent of predictions:
  it has learned the teacher's habit of labelling every roof piece and small structure. The pre-registered selector
  refused every run on that ground. Stage 2 (3,000 steps, seeds 1 and 2) was started at 8e-5 anyway by a logged
  override so the two-seed picture exists; its squared-label arm runs after the raw seeds from the new
  `coco-teachers-squared-v1` (25 to 31 percent of teacher outlines squared by the edge-support gate, same 151 windows
  and 25,032 instances as the raw set). A threshold sweep on the tuned model tests whether the extra detections are a
  calibration shift (recoverable by a higher cut) or learned noise (not).
- All figures are agreement with the listed human outlines or with other model outputs, never accuracy.
- Threshold sweep on the 8e-5 model (same 44 tiles): raising the confidence cut from 0.45 to 0.60 and 0.75 lowers its
  unbacked novel detections from 384 to 246 to 118 while it still matches 63, 63 and 61 of the 63 references. The base
  model at 0.75 keeps 54 references with 7 unbacked detections. At recall parity (base at 0.45: 60 matched, 53
  unbacked; tuned at 0.75: 61 matched, 118 unbacked) the tuned model still produces about twice the unbacked
  detections, so the extra objects are partly a calibration shift and partly learned over-detection. The lever for
  the next stage is the label side: the ledger's part and ignore classes and the squared-label arm.

## Quick reject and one-key relabel in the scene editor, 29 September 2026

The reviewer asked for a clear way to reject roof candidates and to relabel fast. The scene editor's decision bar now
has a third decision, Not a roof (Backspace or Delete), between Needs edit and Skip; it saves the rejection and moves
on, stamping `human_rejected` on a machine-written label. Under the bar, building objects show ten quick-kind chips:
1 shed, 2 garage, 3 carport, 4 patio or carport cover, 5 pergola or shade, 6 shipping container, 7 water tank,
8 pool cover, 9 fragment, 0 primary roof. A key or a click sets the structure kind and label and saves at once
(status needs_adjustment, as the select does), so a confirm or a reject can follow without leaving the keyboard.
Keys are ignored while typing in a field or in any drawing mode, as the arrows already were. The verification is
`task verify:scene-reject`. One trap found on the way: review-guide links that carry `layer=model` open the model
layer, where every decision control is disabled; the guide links were rewritten without it.

Incident, 29 September 2026 morning. Rebuilding the web bundle for the quick-reject change was OOM-killed twice: the
Next.js server-output file tracer crawled the repo root, which now carries about 70 GB of imagery, model weights and
Python environments, and the build process grew past 90 GB while the stage-2 fine-tune's MPS allocator had grown to
58 GB of cached blocks (4 GB allocated). Swap filled, the site was down for about 40 minutes. Fixes:
`outputFileTracingExcludes` in `next.config.ts` for data, scripts, deploy, docs, bin, gen and the caches; the
fine-tune returns the MPS cache every step and the sweep scripts cap the allocator at 60 percent. The headless check
for the new controls then relabelled one live object because its keypress test reached the page body (no text field
had taken focus); the object was restored within a minute with the reviewer's decision, and the check now presses a
key only when focus is verifiably inside a field. Stage 2 resumed with the memory fixes (`stage2_resume.sh`).

## Frame-cut outlines in house scenes, 29 September 2026

The reviewer found tree outlines on house-25822 that stop dead along a straight line inside the image. Root cause:
the house scenes were extracted on a 512 px per-address frame and later shown on the 1024 px registered context
(`scene_extract --expand-context`), and the expansion kept every old model seed. Only roofs could be taken over by a
fuller detection; a canopy cut by the old frame edge stayed, and the fuller canopy the detector now saw on the wider
image was suppressed as its duplicate. Measured on house-25822 with the detector re-run on the 1024 px frame: the two
reported trees had fuller canopies covering 94 and 93 percent of the clipped seed, below the 95 percent the takeover
needed and above the 80 percent at which the duplicate rule drops the newcomer. Fixes, all under test:

- `extract_objects` lets a larger candidate of ANY class take over an unreviewed model seed it covers (80 percent for
  trees and the other classes, matching the duplicate rule; 95 percent for assembled roofs). The candidate records
  every seed it took in `replaces` (a list: one whole roof on house-25822 took over three roof parts the 512 px
  frame had split). Reviewed geometry, roof-line edits and roof feedback are never taken over.
- A carried seed that nobody completed and whose outline runs along the old frame line gets
  `clipped_by_frame: [x0, y0, x1, y1]` (`scene_expand.analyse_expansion`). The editor shows it as a frame cut with a
  reject-or-redraw prompt; the dataset export writes it as IGNORE instead of a pseudo-label (`export_dataset`,
  ignore reason `clipped_by_frame`), so a straight frame line is never taught as a roof edge or as background.
- Review recovery across an expansion keeps a confirmed decision only when the new model re-proposes the very same
  outline under the same id; everything else returns to needs_adjustment (`scene-feedback.ts`, preserveUnchanged).
  A decision on a seed that a fuller detection took over moves onto that detection (`replaces`): the clipped outline
  does not survive as a ghost in the edited layer, the reviewer's label, structure kind, roof type and notes travel,
  the new outline returns unchecked as needs_adjustment, and only seeds that were all rejected reject the whole.
  Children of a replaced parent point at the replacement. Superseded seeds (human-drawn replacements) keep their
  lineage untouched. Headless check: `task verify:frame-clip` (frame-cut note shown on a flagged seed of house-25822,
  the two reported tree ids absent from the edited layer, decisions sitting on the detections that took over).

house-25822 re-expanded from its 512 px snapshot (`5673d946…`) with the fixed extractor: 109 objects became 242
(the first, unfixed expansion gave 259 because clipped seeds stayed beside their fuller twins); 38 detections replaced
62 seeds, 11 of them merges; both reported trees are gone and replaced by full canopies; 10 seeds remain flagged as
frame cuts (4 trees, 3 footpaths, a driveway, a vegetation patch and one small building the wider detector did not
see). All nine human decisions on the scene map onto replacements through `replaces`. The 512 px snapshot stays
under `models/` and in `/tmp/addr-eyes/house-25822.latest.before.json`.

Training scheme: the remaining 30 house scenes at 512 px are expanded the same way (`scripts/ml/expand_house_scenes.sh`,
protected checkpoints house-30286 and house-30296 excluded; the script refuses to run beside this repo's web build),
then `scripts/ml/after_house_expansion.sh` refreshes the confidence index for the expanded scenes (the eyes guard
otherwise blocks every checkpoint on `confidence_mtimes`), carries machine-only reviews through the editor's own
recovery path (`carry-forward-reviews.mjs`; human-decided scenes are the reviewer's call), runs the guard and exports
the outline dataset as dataset-v2 on the 1024 px images. The SAM3 sweep keeps judging against dataset-v1 until stage 2
finishes, so its arms stay comparable. CP16 (POI 18) carries the review links.

Batch result (29 September 2026, 01:01 to 02:24 UTC, one scene at a time beside the fine-tune): all 30 scenes widened
to 1024 px; 28 clean exits and 2 exits of 134 (house-29888, house-30235) where the ONNX runtime aborted at interpreter
teardown after every write had landed (summary line present, image hash and preserved-pixel check pass; the post-batch
script therefore selects scenes by summary line plus pixel check, never by exit code). Totals over the 30 scenes:
2,387 objects became 6,287; 1,176 detections took over 1,284 old seeds (80 merges of several seeds into one); 344
carried seeds are flagged as frame cuts (71 footpaths, 53 roads, 51 vegetation patches, 47 trees, 46 driveways,
35 buildings, 15 pools, 11 solar arrays, 10 water, 5 cars), on every one of the 30 scenes. The 35 flagged buildings
are exactly the outlines the export now writes as IGNORE.

Post-batch (02:24 to 02:31 UTC): confidence refreshed for the 30 scenes (the guard passes again), 5 machine-only
reviews carried, and dataset-v2 exported: 562 images as before, 32 of them now 1024 px, splits unchanged within one
image (test 214, val 49, train 299), 36 frame-cut polygons written as IGNORE. Three carry-forwards were refused by the
server with "Start this changed model with unreviewed objects": those reviews hold machine labels
(`machine:decision-policy-v2`, confirmed) and the save rule refused any confirmed object on a changed model, while the
recovery had kept four of the seven because the widened model re-proposes the very same outline. Two fixes: the save
rule now admits a confirmed object exactly when the server's own recovery keeps it (same id, same outline, verified
expansion), and a machine label whose seed a fuller detection took over is dropped rather than inherited (nobody
decided anything about the new outline; the decision policy can label it again). `carry-forward-reviews.mjs` reverts
a downgraded machine label to the model's plain candidate instead of re-asserting a confirmed the server refuses.
The three scenes were then carried (their four same-outline machine labels stand; the three on replaced seeds were
dropped) and dataset-v2 re-exported with every review on its current frame (review_frame_mismatch 0):

| dataset | images (test / val / train) | 1024 px images | instances | boundary references | frame-cut IGNORE polygons |
|---|---|---|---|---|---|
| v1 | 562 (213 / 49 / 300) | 1 | 18,096 | 63 (52 T1, 11 T2) | 0 |
| v2 | 562 (214 / 49 / 299) | 32 | 18,692 | 62 (51 T1, 11 T2) | 36 |

The reference change is exactly the expected one: house-25822's two confirmed roofs and one edited outline now sit as
needs_adjustment on the fuller detections until the reviewer re-checks them (CP16 links), while house-29785 gained an
edited outline and one Mooroolbark tile a confirmation from the reviewer's recent decisions. The SAM3 sweep still
judges against dataset-v1 until stage 2 finishes; dataset-v2 becomes the judge for the next round.

Incident, 29 September 2026 midday. The rebuild carrying the frame-cut hint was SIGKILLed (exit 137) while the
house-25822 re-expansion and the stage-2 fine-tune ran beside it; a killed `next build` had already emptied `.next`,
so the running server kept answering pages while every JavaScript chunk returned 500 until a build succeeded. With
the fine-tune paused (SIGSTOP) the same build compiled in 3.2 minutes instead of twenty-plus. `task web:rebuild`
(`scripts/rebuild-web.sh`) now pauses the fine-tune, refuses to run beside scene extraction and restarts the service
only on success. A second lesson: `pkill -f "next build"` from a shell whose own command line contains that string
kills the shell; the second attempt failed on Next's build lock and had to be relaunched.

## Post-edit save flow and screen-constant vertex handles, 29 September 2026

The reviewer's complaint: after moving an outline point it was unclear whether "Needs edit" or "Looks right" was the
button that stores the edit, and the selected vertex handle grew with the zoom (a 40 to 200 px blue ring at 400 %).
Two causes, both fixed in the scene editor:

- The decision bar did not know an outline was pending. It now compares the inspected object's ring and holes with
  the saved review (`outlinePending`, independent of edit mode). While an edit is unsaved the two decisions read
  "Save outline · Looks right" (Enter; stores the reviewer's outline as the reviewed reference outline: `confirmed`,
  `geometry_reviewed`) and "Save outline · Still needs work" (Shift+Enter; stores it and keeps the object open:
  `needs_adjustment`). Either one saves and moves to the next object in the current queue without widening the
  filter (Edit outline no longer switches the review filter to All objects, and an Undo during an edit keeps it too).
  Skip and the object arrows are disabled while an outline is unsaved, so an edit cannot ride along unnoticed into a
  later save. Escape finishes editing with nothing saved (the draft stays; Discard draft restores the saved review);
  Enter with no pending edit is inert. Save scene is unchanged: it stores everything with no decision. Nothing infers a
  decision from the act of editing: the only writers of `status` remain the decisions, the reject controls, the relabel
  and `updateBoundary`'s `needs_adjustment`. The model estimate the reviewer edited from stays in
  `models/<model_signature>.json` for comparison; the review stores the reviewer's outline; the numbers derived from
  the pair are agreement between the two outlines, not accuracy.
- The vertex handles were SVG circles with radii in image pixels, so they scaled with the zoom, and nothing styled a
  focused SVG circle, so Chrome painted its own scaled focus band. Handles now follow the direction-handle pattern:
  radii divided by the image scale and a non-scaling stroke (7 px dot, 10 px selected with inverted fill, 24 px hit
  disc at every zoom), a focusable group carrying the label, and an accent stroke on the dot for keyboard focus.
  Arrow keys nudge one image pixel (Shift: five); Backspace removes the focused point when more than three remain.

The inspector's "Reject detection" now writes the same rejected form as Not a roof (with the `human_rejected` stamp on a
machine-written label); before it wrote `rejected` without the stamp. Verification: `task verify:scene-edit-flow`
(`scripts/verify-scene-edit-flow.mjs`) on a scratch scene `qa_editflow_*` that it creates and removes: pointer, field
and arrow edits; handle widths at 100 % and 400 %; Enter and Shift+Enter saves with revision, history and an untouched
`latest.json`; Escape, Discard and the inert Enter; mobile width.

Verification and outage, 29 September 2026 (late afternoon). `task verify:scene-edit-flow` passes end to end against the
development server on :3008: filter kept on edit, pointer, field and arrow edits, handles at 100 and 400 percent zoom
measured at the same screen size, Enter saves the reviewer outline as the reference and advances, Enter on a focused
panel summary opens the panel and writes nothing, Shift+Enter saves and keeps the object open, Escape leaves edit mode;
the scratch scene is removed afterwards. Three test-only fixes were needed on the way (SVG handles cannot be focused
through Puppeteer's page.focus, the drag lands on the pointer so both coordinates are pinned through the fields before
asserting the stored ring, a 3 px drag tolerance). The quick-reject and frame-cut checks pass on the same server.

The production bundle could not be rebuilt: the host is out of memory (a Docker virtual machine at 46 GB, three more
virtual machines near 18 GB, other projects' dev servers and linters, swap full), and `next build` was killed by the
OS at a 22 GB footprint even with the fine-tune paused and the Turbopack cache moved aside. Two earlier kills had
emptied `.next`, so the site served pages whose scripts returned 500. The fallback is a detached `next dev -p 3008`
started from an interactive shell (launchd cannot run it: under launchd Turbopack's PostCSS worker fails to spawn even
with the shell environment sourced, and relative `-o/-e` log paths make launchd exit 78 with nothing logged). The
launchd job is removed until a production build succeeds again; restore with `scripts/rebuild-web.sh` and a
`launchctl submit` using absolute log paths. Reviewer edits on house-25822 (revision 34, three moved tree outlines at
agreement 0.58, 0.58 and 0.91 with the model) were stored throughout.

## Roof-line cleanup, outline editing from the line workspace and the line_corrections sidecar, 29 September 2026

The reviewer's lines are the reviewer's lines; the graph the editor seeded from the model snapshot stays as
`roof_line_edits.baseline` (the model estimate); every number below is agreement between the two, never accuracy, and
`roof_line_edits.checked` is set only by a human click on "I checked these lines against the visible roof". Nothing here
infers a decision from an edit.

**Line workspace (`src/components/scenes/RoofLineEditor.tsx`).** Selection is a set of lines. In the move tool a drag
on empty roof draws a box (`[data-testid=roof-line-marquee]`); on release the lines whose **both** projected endpoints
lie inside the box are selected (a line that merely crosses the box is not; Shift-click adds or removes one line;
Shift+drag adds a box to the selection). A click on empty roof without moving clears the selection; Escape cancels a
box. The canvas takes focus after a box, so Delete/Backspace needs no focused handle. One bulk delete is one commit and
therefore one "Undo line edit" step (Redo re-deletes them together); the line-type select applies to every selected line
in one commit; arrow keys move the whole selection 10 cm. The heading reads "N lines selected", the button "Delete N
lines" (`aria-label` "Delete selected lines"; the single-line label "Delete selected line" is unchanged for
`verify-scene-roof`). The marquee never mutates the graph and never marks the page busy.

**Orphan compaction (`compactRoofLineGraph`, `src/lib/roof-line-edits.ts`).** `connectRoofLines` now ends by keeping
only vertices that an edge references, reindexing `a`/`b` and preserving edge order and junction sharing. Readers key by
coordinates (`roofImageLines`, `roofLineChanges`, the roof-feedback route, `reference_registry` resolves indices at
build time), so nothing else changes. Effect on house-25822/4fa078ff5ac6a8d4: its saved graph stores 52 vertices of
which 15 are referenced (37 orphans). They compact at the reviewer's **next geometry commit**, not on load, so the next
save of that object differs in vertex count even for a one-line edit; the 9 edges and their coordinates are unchanged.

**Edit outline from the line workspace without losing pending line edits.** The line workspace nav has "Edit outline"
(`[data-testid=roof-line-edit-outline]`, the same name as the review card's button: one name for one action). It
enters edit mode only; the pending line edits stay in the draft. When a building's ring or holes change,
`changeObject` (`src/app/scenes/page.tsx`) calls `reanchorRoofLineEdits`: edges that still lie inside the new ring and
outside its gaps are kept, the captured `outline` is rebased to the new ring/holes, the graph is compacted, `checked`
resets to false (the containment context changed); `baseline` and `notes` are untouched. Edges that leave the roof are
dropped rather than kept because the server validates every edge against the captured outline (a kept-but-outside edge
would make the next Save fail with "Keep each roof line inside the visible outline and outside gaps."). The drop is
reported in the status space with exactly

    N roof line(s) fell outside the edited outline and was/were removed from your pending line edits · Undo restores it/them.

and the page "Undo edit" restores ring and lines together. Way back is "Edit roof lines" (the edited ring stays pending;
the decision bar reads "Save outline · Looks right" / "Save outline · Still needs work"). Enter or Shift+Enter from the
page body or a vertex handle saves ring + re-anchored lines in one revision; `checked` stays false. The notice "Earlier
lines belong to a different outline and remain in saved history" (a Fieldwork import that changed the ring, or a
historical edit) now carries **Carry earlier lines onto this outline**: a human click rebinds the earlier lines to the
current outline in the draft (kept edges only, `checked` false, editor Undo available).

**One page change per gesture (fix after the first review, same day).** The first build re-anchored on every pointer
move of an outline-vertex drag and on every keystroke in the "Outline point X/Y" fields, so a line that was only
passingly outside an intermediate ring was dropped for good, the drop message was wiped by the next move and each move
was its own page-undo entry (a long drag exhausted the 40-entry undo). `SceneCanvas` now previews the ring locally
while a vertex is dragged or a coordinate is typed (handles, fields and the outline path follow the preview) and emits
one `onChange` on pointer-up, field blur or Enter in the field; a cancelled drag (pointercancel, lost capture) discards
the preview. The page therefore sees one change per gesture: one undo entry, one re-anchoring against the final ring
only, and the drop message stays until the next change. Arrow-key nudges, Insert/Remove point and Square up remain one
change each. `MetricInput` forwards the caller's `onFocus`/`onBlur` for this (it used to override them silently).

**Verification.** `node scripts/test-roof-line-edits.mjs` (compaction, connect output, re-anchoring identity/shrunk
ring/new hole/non-building); `task verify:scene-roof-lines` (`scripts/verify-scene-roof-lines.mjs`) on a scratch
`qa_rooflines_*` scene it creates and removes: box selection with both-endpoints rule, Shift-click toggles, one-step bulk
delete/undo/redo, Escape cancels a box, a compacted save (every stored vertex referenced), a drag through a position
where two lines would lie outside (all survive, no report, one Undo), a drag that ends with one line outside (reported
once, report persists after the pointer moves on, one Undo restores ring and line), the Shift+ArrowLeft drop, Enter
saving ring + lines in one revision with history intact and `latest.json` byte-identical, mobile width. Regression:
`task verify:scene-roof`, `task verify:scene-edit-flow`, `task verify:scene-roof-feedback`. house-25822 is only read.

**`line_corrections` sidecar (`scripts/ml/line_corrections.py`, `VERSION = 'line-corrections-v1'`).** Table
`line_corrections(scene_id, object_id, review_revision INTEGER, model_signature, version, payload)`, primary key
`(scene_id, object_id, review_revision, version)`, index `line_corrections_object(scene_id, object_id)`; `INSERT OR
REPLACE` keyed by revision, so a rebuild is idempotent and never deletes. One row per current review revision of a
building with non-empty `roof_line_edits`, pairing `baseline` (model estimate) with the reviewer's `vertices`/`edges`:
the repo line rule (`reference_agreement.line_metrics`, 0.5 m / 10 deg, `line_match_rule.source
reference-agreement-v1`), a distance sweep at 0.35 / 0.5 / 1.0 m, per-kind matches, 1 m length coverage shares
(labelled a coverage share, not the repo rule), `topology_model` / `topology_ref` (edges, stored vs referenced vertices,
orphans, degree histogram, components, lengths, lines under 1.5 m), `count_ratio`, `ref_checked`, `edited_at_revision`
from history, `outline_matches_object`, `baseline_matches_snapshot`, `pair` (snapshot | latest | unverified), pipeline,
roof type, note flags and confidence issue codes. Commands: `python -m scripts.ml.line_corrections --objects
SCENE:OBJECT --dry-run` prints rows and writes nothing; `task eyes:line-corrections -- --objects SCENE:OBJECT`
(`--build --summary`) writes the table and `data/reviews/eyes/line-corrections/<UTC date>/rows.jsonl + summary.json`.
Readers: `python -m scripts.ml.eyes_index --object SCENE OBJECT`; `scene_processing._eyes_lookup` puts the highest
revision per object into `properties.eyes.line_corrections` (review_revision, ref_checked, line_precision, line_recall,
n_model_lines, n_ref_edges, count_ratio, n_components_ref) on `/api/scenes/processing`; `scene_roof_review._eyes` binds
the same fields plus `baseline_matches_snapshot` by model signature. Not built: a `--history` flag, and calibrating the
model-side `complex_folds` signal against rows with `count_ratio > 2` (the named follow-up). Loop candidate selection is
unchanged (house-25822 stays excluded by the review.json rule).

First row, house-25822/4fa078ff5ac6a8d4, **dry-run preview** (the sqlite write waits for the user's go; no
`data/reviews/eyes/line-corrections/` folder exists yet): review revision 35, edited at revision 35 by `local`, pair
`snapshot`, tier T2 `review:roof_line_edits`, `ref_checked` false, notes empty, status candidate, `geometry_reviewed`
false, no authority blocker, outline matches the object and is unchanged since the model, baseline equals the snapshot's
35 lines. Model: 35 lines (hip 20, ridge 8, valley 7), 109.8 m, median 2.52 m, shortest 0.42 m, 8 under 1.5 m, 34
nodes (degree 1: 16, degree 3: 18), one component. Reviewer: 9 edges (ridge 5, valley 4), 52.0 m, median 4.27 m,
none under 1.5 m, 15 of 52 stored vertices referenced (37 orphans), degree {1: 13, 2: 1, 3: 1}, 6 components. Repo
rule: precision 0.0, recall 0.0 (0/35, 0/9 at 0.35 m and 0.5 m); at 1.0 m 3/35 model lines and 2/9 reviewer edges
match; 86 % of the reviewer's length lies within 1 m of a model line and 58 % of the model's length within 1 m of a
reviewer line; `count_ratio` 3.89; pipeline `sam3-scene-roof-v12`, roof type hipped, `form_uncertain` true, 6 jointly
fitted edges, confidence codes `complex_folds`, `image_line_disagreement`. Reading (not stored as a field): placement
roughly agrees at 1 m while the repo rule scores zero because the model fans the roof into many short hips; the
disagreement is topology count, the user's over-complex-topology class, not placement. The row is a draft while
`ref_checked` is false.

**Fieldwork carry-over (`apps/annotation-web`, see `docs/annotation-deployment.md`).** Decision bar "Save outline ·
Still needs work" (Shift+Enter) and "Save outline · Looks right to me" (Enter), both saving one item revision and
opening the next item; "Save & next item" and Cmd/Ctrl+S in the inspector; focusable outline points with arrow nudges,
Backspace removal above 3 points and Escape abandoning a drag. Policy caveat, accepted and unchanged: the local import
(`feedback.ts`) lands every substantive Fieldwork change as `needs_adjustment`, `geometry_reviewed` false,
`fieldwork_import.pending` true, so a Fieldwork "Looks right" survives only in the item's overrides and the receipt
fields; a bearer token is not a human decision in the local review. Marquee and re-anchoring are not ported (roof lines
are read-only in Fieldwork; `updateSceneObject` never rebinds `roof_line_edits`). The SPA bundle is built and
dry-run-checked; the deploy is the user's decision.

## SAM3 stage-2 verdict, 29 September 2026 (evening)

All four stage-2 arms are judged on the 63 human boundary references of dataset-v1 (lr 8e-5, 3,000 steps, frozen
backbones): raw teacher labels seeds 1 and 2, squared-gated labels seeds 1 and 2. Every arm matches 63 of 63 references
(base 60 of 63), raises the paired best-IoU by 0.02 to 0.03 with the image-bootstrap interval touching or crossing
zero, and multiplies unbacked novel detections from 6 percent of predictions to 21 to 24 percent (median novel area
about 8.5 m², predictions per tile 12 to 20). The squared arms hold the matched boundary distance at 0.23 m against the
base 0.22 m, where the raw arms drift to 0.25 to 0.26 m. Reading: the teacher sets teach recall of small parts and
structures far more than they sharpen outlines; squaring removes most of the boundary degradation but not the
over-detection. Nothing is promoted. Next candidates, in order of expected value: a negative-aware objective or a
per-image cap on unbacked small detections (the human rejections now being recorded make precision judgeable), a
ledger-filtered label set (ignore arm), and dataset-v2 as the judge for the next round so the widened house scenes and
the reviewer's edited outlines count.

## Local-to-Fieldwork sync surface, 29 September 2026 (late)

The user asked for "a solid flow for syncing records from local to the fieldwork project, all via mcp/api" with
training runs able to pull diffs and datasets. The contract is `docs/annotation/local-to-fieldwork.md`; this round
lands the surface only. `Taskfile.yml`: `push` (plan with `-- --dry-run`, apply, resume with `-- --run ID`),
`push:status`, `training:publish` (dry-run unless `-- --apply`), `training:manifests`, `training:pull`. Local MCP
(`scripts/ml/mcp_server.py`): `plan_fieldwork_push` (read-only), `apply_fieldwork_push`, `fieldwork_push_status`
(receipts, no network), `publish_training_artefacts` (dry-run default), `list_training_manifests` (read-only),
`pull_training_artefacts`; all six import `scripts.ml.fieldwork_push` / `scripts.ml.training.{publish,pull}_artefacts`
lazily and forward to `push_fieldwork`, `push_status`, `publish`, `list_manifests`, `pull`, so the server loads before
those modules exist and answers with a tool error naming the missing module until they do (pinned by
`scripts/ml/tests/test_mcp_fieldwork_surface.py`). Wording carried into every description: a pushed or pulled value is
the model estimate plus the local review state as of publication, never a human label; push never edits Fieldwork
overrides; no training is started; tokens stay in files; write tools refuse the live origin unless
`FIELDWORK_PUSH_ALLOW_LIVE=1`. Design points fixed by the contract: one NEW immutable item per published generation
(`addr-scene-<S>-m<sig12>-r<rev>`), the only touch on an existing item is the revision-checked `superseded-frame` mark,
loop safety by revision + document equality against push receipts (the change-feed actor is `workspace` for everyone),
and training artefacts as one immutable `addr-report` manifest per publish in an `addr-training` project with
content-addressed blobs. Nothing has been written to the live project; the Go push package, `fieldwork_push.py`,
`publish_artefacts.py`, `pull_artefacts.py` and the schema-2 feedback packet are the next steps.

## Vicmap aerial tiles and MapShare Vic, 29 September 2026 (night)

The reviewer asked to pull every tile from the Victorian aerial imagery pages and MapShare Vic, building a Chrome
extension if needed. Findings, measured or quoted:

- Both sites draw their imagery from one service, the Vicmap Basemaps WMTS at base.maps.vic.gov.au (layer
  `AERIAL_WM_256`, Web Mercator, levels 0 to 20, PNG). MapShare Vic's Geocortex map configuration lists that URL as
  its basemap, so an extension over MapShare would only re-fetch the same tiles the API serves directly.
- The endpoint answers without a key, but its own capabilities document states the terms: "Vicmap Basemaps is a
  licensed service … An annual license fee is payable and is based on usage by the clients business", unit "map page
  impressions"; a 1 to 3 month evaluation licence is offered on request (vicmap@transport.vic.gov.au). The
  land.vic.gov.au licensing pages refuse automated fetches (HTTP 403). The underlying Coordinated Imagery Program
  captures (7.5 to 10 cm) are DTP intellectual property sold through Data Service Providers; they are not on data.vic.
- Two tiles were fetched over 27 Cambridge Road to measure, nothing more: z19 and z20 answer (tile dates 2026-01-10
  and 2026-02-14), z21 is refused, so 0.118 m/px nominal is the ceiling. On the same z20 tile the Google tile the
  pipeline already uses is sharper on every measure (gradient p90 54.6 against 39.0, Laplacian variance 2,784 against
  1,455, top-octave energy ratio 1.27 against 1.04); the basemap aerial is a soft resample of an older capture. Side
  by side: data/references/imagery-probes/vicmap-vs-google-z20-27cambridge-2026-09-29.png.
- Scale if a licence were taken: the 5.1 by 5.1 km Mooroolbark scan area is 7,396 tiles (0.9 GB) at z19 and 28,900
  tiles (3.9 GB) at z20.

Decision: no bulk pull and no extension. Bulk extraction from the viewer or the WMTS would breach a licence whose
fee is measured in page impressions, and the tiles would add no resolution over the saved Google tiles. The provider
entry `vicmap-aerial` in `scripts/ml/imagery_sources.py` stays gated on `VICMAP_BASEMAPS_LICENCE` and carries these
measurements. The route that would add pixels is the CIP source capture through a reseller, or a Vicmap Basemaps
evaluation licence for a fair 200 m probe, both paid or by request and therefore the reviewer's call.

## Google aerial dates at Mooroolbark, 19 October 2026

Google puts no capture date in tile headers, so the date was read from Google Earth's own time-machine metadata
(GEHistoricalImagery v0.7.1, read-only `info` at five points across the 5.1 km scan; probe
`data/references/imagery-probes/google-tm-dates-mooroolbark-2026-10-19.json`):

- z18, z19 and z20 all resolve to **12 December 2025**, provider "Image © 2026 Airbus", epoch 367. That is the
  current Google aerial over Mooroolbark at the zoom the pipeline saves (0.118 m/px).
- z21 resolves to **18 October 2023**, epoch 1021. The extra z21 detail is an older, finer capture, not a newer one:
  z20 carries the December 2025 imagery, z21 the 2023 imagery.
- The mt1 z19 and z20 tiles are byte-identical to the tiles cached on 24 and 27 September 2026, so Google has not
  republished this mosaic since at least then.

Vicmap publishes only tile Last-Modified (z18 13 December 2025, z19 10 January 2026, z20 14 February 2026), not an
acquisition date, and its own service page says capture dates are typically at least two years old. The timestamps
arrive in per-row publish waves, one day per zoom, so the three zoom levels need not share one capture. Vicmap
Last-Modified values must not be read as flight dates.

A separate internal review archive uses the public mt1 endpoint at one request per second
(`scripts/ml/google_tiles.py`, `task imagery:google-archive`, output `/Volumes/gamma-systems-2/addr-tiles/google`).
This is outside the Map Tiles API licence; the licensed route remains the API. The crawl stops on 401/403 and keeps a
per-tile sha256/ETag manifest. It is complete: 35,960 tiles, 575 MB, z18-20, with a `tile_index` R*Tree in `tiles.db`;
the archive is review material, not a training corpus. The canonical archive/index reference is
`docs/imagery-archive.md`.

Same-grid comparison at 27 Cambridge (5x5 z20 tiles, no reprojection;
`data/references/imagery-probes/vicmap-vs-google-z20-27cambridge-2026-10-19.{png,json}`): Google's Laplacian variance
is 1,699 against Vicmap's 1,107, gradient p90 0.95 against 0.74, while grayscale NCC is 0.46 with a phase shift of
2.9/-4.0 px. They are different captures with different tone and season; Google is the sharper of the two at native
z20, and it is the only one with an explicit acquisition date (12 December 2025). Vicmap's later tile dates are
publish times.

Across z18-20 (`vicmap-vs-google-multizoom-2026-10-19.json`) grayscale NCC stays 0.46-0.53. Vicmap's z18/z19 PNGs
carry more high-frequency energy than Google's JPEGs (Laplacian 9,852/7,189 against 2,543/3,623), which is a
pyramid/compression difference, not evidence of more real detail; at native z20 the order reverses. The phase shift
grows with zoom (0.1/-1.8 px at z18, 0.7/-2.6 at z19, 2.9/-4.0 at z20), so the two pyramids are not registered.
A second z20 site 2.7 km north (`vicmap-vs-google-z20-north-2026-10-19.json`) repeats the native result: Google
Laplacian 1,410 against Vicmap 816, gradient p90 0.86 against 0.62, NCC 0.26, shift 3.9/-3.2 px.

The wider source map, including Nearmap, Vexcel, EagleView, HxGN, Planet, Maxar/Airbus tasking, Sentinel-2 and the
national open orthos, is `docs/global-aerial-sources-2026-10-19.md`.

## SAM3 round 3, label and judge side prepared, 29 September 2026 (night)

Nothing in this section is a result: no round-3 fine-tune has started, nothing is promoted, production
`latest.json` / `review.json` / confidence are untouched. Wording: agreement, model estimate, reference.

- Stage-2 sampling finding (H0): the four stage-2 train logs each carry 2,105-2,109 `got loading error` lines on
  exactly 60 distinct datapoints, and those 60 indices are the 60 images of `coco-teachers-squared-v1` with more than
  200 annotations (14,064 labels). The fork's `FilterFindQueriesWithTooManyOut(max 200)` dropped them, so stage 2
  trained on 91 of 151 windows. Round 3 caps every fold at 200 (largest annotations kept, sorted before the cap) and
  gates `images_over_fork_max == 0` before training.
- Folds under `data/reviews/eyes/training/sam3-mac/round3/` (manifests carry per-image density and median area, per-source
  counts, flipped-to-background counts and a judged-scene distance check): `coco-r3-sq-c200-v1` 151 images / 22,968
  annotations (2,064 over-cap on 60 images become background); `coco-r3-sq-f12-ledger-c200-v1` 151 / 21,710 (12 m²
  floor = 216 px drops 2,297; 531 still over the cap on 30 images; the ledger-v1 export of the 2025 set removed 494
  train instances as IGNORE); `coco-r3-sq-c200-mixv2-v1` 385 / 33,264 (adds `dataset-v2-trainclean`: 234 Mooroolbark /
  roof_training_250 / map tiles, 10,296 production pseudo labels, 54 excluded for ignored test tiles and 11 for an
  ignore share of 1 percent or more; tile_group / scene_id / sha256 disjoint from both test splits). The distance check
  flags two `distill-v1-squared` TRAIN windows within 300 m of judged inner-Melbourne map scenes (`com2018-r007-c024`
  15.5 m from `map-78bde04b0b7ce06d`, `com2018-r009-c023` 196 m from `map-ebb9671e2964ee91`); 144 of 263 judged
  scenes (roof_training_250) carry no bounds and are unchecked. `coco-r3-sq-merge-c200-v1` 151 / 22,447 (1,343 over-cap on 49
  images): the whole-building exports merged 1,014 (2025) and 934 (2020) outline groups from 2,529 / 2,285 pieces, of which
  322 / 254 groups were disconnected on the student grid and reverted to single pieces; 24 / 12 pieces span two outlines;
  2025 train instances 8,547 -> 7,921, 2020 8,264 -> 7,648.
- Label code: `distill_dataset.py` gains `--label-target whole_squared_gated` (teacher pieces at least 0.8 inside the
  same LINZ building outline are unioned, then squared; a group whose union is disconnected on the student grid
  reverts to single pieces rather than painting the smaller one IGNORE, which SAM3 would read as background; `merged_from` /
  `outline_id` per annotation; manifest `merge` counts) and `--policy outline-v1` (ledger ignore rows plus small unmatched instances not seen in the other capture,
  painted IGNORE; parts and only-here instances of 25 m² or more are never dropped).
- Judge: `reference_eval.py` (v2) takes `--dataset --split --pair --footprints`, writes every prediction above the
  collection threshold to `predictions.jsonl` with its overlaps, so `r3_postfilter.py` rebuilds any higher cut without
  inference; the image bootstrap weights each image by its draw count; per-hectare rates, pseudo-label recall at IoU
  0.5, novel-area histogram, area / pixel / tier / image-identity strata, shape agreement and edit-diff strata are
  reported. On 5 dataset-v2 test images the 0.45 post-filter of the 0.2 rows reproduced a direct 0.45 run exactly
  (77 rows, identical per-reference best IoU). `r3_calibrate.py` picks the matched-density t* on dataset-v2 val from
  prediction counts and pseudo agreement only. Rules are pre-registered in `round3/RULES.json`.
- Footprints: a second Microsoft cache `ms-buildings/bbox-inner-melb.geojsonl.gz` (2,204 features, CDLA-Permissive,
  bbox 144.9709-144.9885 / -37.8005 to -37.7887 around 22 judged map tiles) sits beside the Mooroolbark cache (17,121
  features); shas in `round3/footprints.json`. Footprint backing is agreement with another ML output.
- Correction to the stage-2 verdict above: the reviewer's rejections (car 62, solar_panel 67, road 57, tree 5,
  building 1) are 'not X' decisions on other prompts and do not make roof precision judgeable; roof precision stays
  a proxy (unbacked novel share, novel per hectare) until confirm / reject decisions on small roof-prompt detections
  are recorded.

## Teacher loop round 3, first row (control), 29 September 2026 14:45 UTC

Round 3 tests label curation with one-rule arms at a 200-annotation cap, after the readers found that every stage-1
and stage-2 arm had silently trained on 91 of 151 teacher windows (the fork drops images with more than 200
annotations). Spec: `.run/teacher-round3-spec-2026-09-29.md`; pre-registered rules `sam3-mac/round3/RULES.json`;
ledger `sam3-mac/round3/results.jsonl`. Judge blocks: dataset-v2 test primary (62 references), dataset-v1 continuity,
a validation-only density-matched threshold per arm.

Control arm `r3-ctrl-sq-c200-seed1` (squared teacher labels, cap 200, all 151 windows, 3,000 steps in 1 h 59 min):

| view (dataset-v2 test) | matched refs | median agreement | boundary distance | predictions per tile | unbacked novel share | novel per ha |
|---|---|---|---|---|---|---|
| base at 0.45 | 59 of 62 | 0.902 | 0.224 m | 12 | 5.7 percent | 2.5 |
| control at 0.45 | 62 of 62 | 0.901 | 0.257 m | 24 | 32.6 percent | 13.3 |
| control at its validation threshold 0.80 | 58 of 62 | 0.901 | 0.251 m | 14 | 10.8 percent | 2.9 |

Paired best-agreement delta against base +0.023 at 0.45 (interval -0.010 to 0.066) and -0.020 at 0.80 (-0.070 to
0.028). No threshold on the grid up to 0.80 brought the control's density within 1.1 times the base's while keeping
pseudo-label recall, so its threshold is the flagged fallback. Reading: recovering the 60 dense windows made
over-detection worse than stage 2 (unbacked share 21 to 24 percent there, 33 percent here); cutting the threshold to
match density costs the matched references. The label prior (small parts at 24 instances per hectare), not the
sampling, drives the over-detection. The floor arm (teacher instances under 12 m² as background, ledger ignores) is
training next and tests exactly that.

Second row, floor arm `r3-floor12-sq-ledger-c200-seed1` (control labels with teacher instances under 12 m² as
background, plus the approved ledger ignores; 1 h 52 min), dataset-v2 test:

| view | matched refs | median agreement | boundary distance | predictions per tile | unbacked novel share | novel per ha |
|---|---|---|---|---|---|---|
| base at 0.45 | 59 of 62 | 0.902 | 0.224 m | 12 | 5.7 percent | 2.5 |
| control at 0.45 | 62 of 62 | 0.901 | 0.257 m | 24 | 32.6 percent | 13.3 |
| floor at 0.45 | 61 of 62 | 0.904 | 0.238 m | 22 | 27.6 percent | 11.1 |
| control at 0.80 (matched view) | 58 of 62 | 0.901 | 0.251 m | 14 | 10.8 percent | 2.9 |
| floor at 0.80 (matched view) | 59 of 62 | 0.904 | 0.234 m | 12 | 8.9 percent | 2.4 |

Paired against the control: -0.004 agreement (interval -0.016 to 0.008), so indistinguishable; against base +0.019
(-0.011 to 0.061). Pre-registered verdict: keeps matched references, keeps boundary, agreement inconclusive, does not
remove the over-detection (the rule asks for novel per hectare within 1.5 times base at the arm's threshold and the
unbacked share within 5 points of base at 0.45). Reading: removing the small teacher instances trims a fifth of the
over-detection and recovers about a third of the boundary drift, and at matched density it brings novel detections
per hectare back to the base's level while keeping the base's 59 matches. The small-label prior is part of the
story, not most of it; the density and domain prior (mix arm, training now) and instance granularity (merge arm)
remain to be tested.

Third row, mix arm `r3-mixv2-sq-c200-seed1` (control labels plus the dataset-v2 train pseudo-labels from the
student's own Mooroolbark tiles, 234 images and 10,296 instances at 8.8 per hectare; 1 h 35 min), dataset-v2 test:

| view | matched refs | median agreement | boundary distance | predictions per tile | unbacked novel share | novel per ha |
|---|---|---|---|---|---|---|
| base at 0.45 | 59 of 62 | 0.902 | 0.224 m | 12 | 5.7 percent | 2.5 |
| control at 0.45 | 62 of 62 | 0.901 | 0.257 m | 24 | 32.6 percent | 13.3 |
| mix at 0.45 | 61 of 62 | 0.893 | 0.239 m | 14 | 16.3 percent | 5.7 |
| mix at its validation threshold 0.65 | 60 of 62 | 0.893 | 0.239 m | 13 | 11.4 percent | 3.6 |

The mix arm is the first whose validation rule found a qualifying threshold (0.65, chosen on the 13 non-Mooroolbark
validation images because its training tiles neighbour the Mooroolbark validation tiles). Paired against the control
-0.014 (interval -0.046 to 0.006); against base +0.009 (-0.038 to 0.057). Pre-registered verdict: keeps matched
references, keeps boundary, agreement inconclusive, over-detection not removed (novel per hectare 5.7 against the
rule's 3.8 ceiling at 0.45). Reading: seeing the student domain halves the over-detection at the same threshold and
brings prediction density close to the base's, at a small cost in outline agreement (0.893 against 0.902, consistent
with the rounded production outlines it learned from). Phase 1 is complete; the merge arm (whole-building labels from
the LINZ outlines) is training as phase 2.

Fourth row, merge arm `r3-merge-sq-c200-seed1` (both Christchurch sets relabelled as whole buildings per LINZ outline;
1 h 57 min), dataset-v2 test at 0.45: 62 of 62 matched, median agreement 0.902, boundary 0.258 m, 26 predictions per
tile, unbacked share 31.5 percent, novel 12.7 per hectare; paired against the control +0.004 (interval -0.002 to
0.012), the round's first pre-registered "non-inferior" agreement verdict; at its fallback threshold 0.80 it keeps 59
matches at 0.244 m with 10.1 percent unbacked. Reading: merging the teacher's parts into whole buildings changes
nothing measurable about over-detection or boundary; instance granularity is not what the student is learning wrong.

Round-3 phase 1 and 2 summary (dataset-v2 test at 0.45, base 59 of 62 / 0.902 / 0.224 m / 12 per tile / 5.7 percent):

| arm | one rule | matched | agreement | boundary | per tile | unbacked share | novel per ha | verdict |
|---|---|---|---|---|---|---|---|---|
| control | cap 200, all 151 windows | 62 | 0.901 | 0.257 m | 24 | 32.6 percent | 13.3 | reference for the others |
| floor | under 12 m² to background + ledger ignores | 61 | 0.904 | 0.238 m | 22 | 27.6 percent | 11.1 | keeps matched and boundary; agreement inconclusive |
| mix | + student-domain pseudo-labels | 61 | 0.893 | 0.239 m | 14 | 16.3 percent | 5.7 | keeps matched and boundary; only arm with a qualifying threshold (0.65) |
| merge | whole-building labels | 62 | 0.902 | 0.258 m | 26 | 31.5 percent | 12.7 | non-inferior agreement; over-detection unchanged |

No arm met the "removes over-detection" bar. The order of effect on over-detection is domain mix (about half), small-label
floor (about a fifth), merge (none); the floor arm alone keeps the sharpest boundary. Cumulative training wall 7.4 h,
so the runner's reserve decision is to build the outline-backed floor exports and run the reserve arm. Round 4, as
pre-registered: combine floor and mix (with merge optional), then the objective knobs (presence and negative
weighting) on the control fold, seed-2 replicates of any survivor, and dataset-v2 remains the judge.

Fifth row, reserve arm `r3-outline-f12-sq-c200-seed1` (floor fold with both Christchurch sets from the outline-v1
policy: a small teacher instance becomes background only when no mapped outline and no cross-capture backs it; 2 h
14 min), dataset-v2 test at 0.45: 61 of 62 matched, median agreement 0.906 and boundary 0.234 m (both the round's
best), 23 predictions per tile, unbacked share 29.3 percent, novel 11.9 per hectare; at its fallback threshold 0.80:
60 of 62, 0.237 m, 10.8 percent unbacked, 2.8 novel per hectare. Paired against the control -0.009 (interval -0.048 to
0.014); verdict keeps matched and boundary, agreement inconclusive, over-detection not removed. Against the plain
floor arm it is within noise on every measure (0.906 against 0.904, 0.234 against 0.238 m, 29.3 against 27.6 percent
unbacked), so backing the floor with the outlines adds nothing the area floor did not already give.

Round 3 is closed (five arms, 9 h 31 min of training, results in `sam3-mac/round3/results.jsonl`, rendered report
`sam3-mac/round3/REPORT.md`). Findings the next round rests on: (1) the over-detection is a density prior from the
teacher windows, cut about half by the student-domain mix, a fifth by the small-label floor, not at all by whole-building
merging or outline backing; (2) no curated label set alone reaches the base's density without a threshold above 0.65,
and thresholding costs matched references; (3) the floor arms keep the sharpest boundary (0.234 to 0.238 m against the
control's 0.257 m) but none beats the base's 0.224 m; (4) the mix arm trades outline agreement (0.893) for density.
Round 4, pre-registered: floor plus mix in one fold (the two levers that moved the number), then the objective knobs
(presence and negative weighting) on that fold, a seed-2 replicate of any arm that clears the density rule, and the
reviewer's small-roof decisions as the human precision number in place of the footprint proxy.

## Teacher loop round 4, first row (combined arm), 30 September 2026 02:03 UTC

Round 4 combines the two round-3 levers in one fold: the 12 m² floor on the four teacher sets only (216 px, with the
ledger ignores on Christchurch 2025) plus the student-domain pseudo-labels at their own 16 px floor, squared labels,
cap 200 (385 images, 32,006 annotations). Three new fine-tune flags (presence weight, positive weight, one-to-many
weight) default to the constants every earlier run used. Runner `round4.sh all`, ledger `sam3-mac/round4/results.jsonl`.

| view (dataset-v2 test) | matched refs | median agreement | boundary | predictions per tile | unbacked novel share | novel per ha |
|---|---|---|---|---|---|---|
| base at 0.45 | 59 of 62 | 0.902 | 0.224 m | 12 | 5.7 percent | 2.5 |
| round-3 control at 0.45 | 62 of 62 | 0.901 | 0.257 m | 24 | 32.6 percent | 13.3 |
| combined at 0.45 | 61 of 62 | 0.896 | 0.228 m | 15 | 13.6 percent | 4.7 |
| combined at its validation threshold 0.60 | 59 of 62 | 0.896 | 0.224 m | 13 | 10.0 percent | 3.2 |

The combined arm is the first to hold the base's boundary distance (0.228 m at 0.45, 0.224 m at its threshold) while
cutting the control's over-detection by more than half at the same threshold and qualifying on the validation rule
(0.60, no fallback). Paired against base +0.011 (interval -0.004 to 0.033); against the control -0.012 (-0.049 to
0.011); against the mix arm +0.002. Its agreement sits between the floor arm's 0.904 and the mix arm's 0.893. The
objective arms (positive weight 3, then one-to-many weight 0) train next on the same fold.

## Reviewer set of 30 September 2026: lot-aware frames, object hints and the teacher check

The reviewer flagged 16 objects across four scenes (recorded with provenance human:chat; regression set
`data/references/eyes-checkpoints/CP20/user-feedback-2026-09-30.json`): objects cut by the frame and not tied to
their lot ("where are the property boundaries?"), sub-division blocks on a unit site, one bush split into pieces,
pavement labelled building, grass labelled pool, garages and a shed marked primary, roofs with dozens of noisy
vertices, and one "went really bad". Root facts: the 227 `roof_training_250` cohort scenes have no registered map
frame, so neither context widening nor the property-boundary layer could run on them; house scenes are centred on the
address point; the hints service carried nothing for house scenes. A design panel and three implementers delivered:

- Property boundaries on every georeferenced scene: `sceneGeoBounds` fits bounds from the cohort frame's affines
  when no registered region exists (notice "alignment unverified"), the layer times out with a retry message instead
  of staying at "Loading Vicmap…", every parcel is drawn faintly with the touching lots highlighted, and each object
  carries a one-sentence lot statement. Vicmap parcels give lots, plans and common property; they do not give fence
  lines, party walls or stacked-strata cuts, and the editor says so.
- Per-object lot tags (`scene_lot_tags.py`, lot-tags-v1) and object hints (`scene_object_hints.py`, object-hints-v1; object-hints-v2
  since the evening review below, which adds solar_fixture_like and changes none of these):
  frame_cut, lot_incomplete_in_frame, lot_unassociated, spans_lots, unit_site, tiny_area_for_kind, degenerate_outline,
  noisy_outline (corners per metre and off-axis share), canopy_fragment_inside, pool_not_blue and building blends,
  secondary_by_lot (the largest roof on the lot is primary). Every hint carries a measured sentence; false-fire rates
  on unflagged objects are frozen in the module (frame 22 percent of objects, noisy 4.8 percent of buildings, canopy
  fragment 0.16 percent). Hints are never labels: the editor shows them on the Agent hints line with a Why-flagged
  panel, a suggested Not-a-roof and quick-kind chip, and a squared-outline overlay that is off by default and carries
  the measured J1 evidence in its tooltip.
- A containment rule in extraction merges same-class fragments inside a canopy of 5 m² or more (the seven sub-1 m²
  vegetation specks inside the 141 m² tree on roof_training_250-30371); `--merge-plan` reports it without writing.
- Reflection records for the three roofs named (`outline_reflection.py`): the 87 m² roof has 54 corners along 43 m
  with 49 off its axes; the 0.04 m² polygon is 0.24 by 0.26 m; squaring passes on the first (agreement 0.94,
  Hausdorff 0.8 m) once its three speck holes are dropped.
- The teacher check (`task eyes:teacher-checks`, `teacher_checks.py`): agreement between fired hints and the
  reviewer's themes over the reviewer sets of 26 and 28 September and today. Fresh computation catches 16 of 16 today's
  objects and 35 of 36 overall (the miss is the stacked-strata unit site 324d3af9); the two reviewer negatives are
  over-flagged by pre-existing layers (n = 2). Wilson intervals are printed with every share; a lot-frame recapture is
  planned as a dry run only, because a new image hash cannot pass review recovery. Checks: `verify:scene-hints-outline`
  (read-only) and `verify:teacher-hints` (scratch scenes); 98 Python tests, node tests, tsc and eslint green.

Second round-4 row, positive-weight arm `r4-combo-posw3-seed1` (the combined fold and line with the detection loss's
positive-query weight lowered from 10 to 3; 1 h 59 min), dataset-v2 test at 0.45: 61 of 62 matched, median agreement
0.894, boundary 0.236 m, 14 predictions per tile, unbacked share 16.3 percent, novel 5.2 per hectare; at its validation
threshold 0.55: 61 matched, 14.1 percent unbacked, 4.1 novel per hectare. Paired against the combined arm +0.004
(interval -0.001 to 0.010, the pre-registered non-inferiority), against base +0.015 (-0.003 to 0.043). Reading: the
objective knob moves nothing the labels did not already move; over-detection is slightly higher than the combined arm
(16.3 against 13.6 percent) and the boundary slightly softer (0.236 against 0.228 m), both within noise. The
one-to-many weight arm trains next; the seed-2 replicate follows by the pre-registered selection rule.

Third round-4 row, one-to-many arm `r4-combo-o2m0-seed1` (the combined fold and line with the auxiliary one-to-many
supervision weight set to 0; 1 h 40 min), dataset-v2 test at 0.45: 59 of 62 matched (two fewer than the combined arm),
median agreement 0.894, boundary 0.237 m, 15 predictions per tile, unbacked share 13.2 percent, novel 5.2 per hectare;
validation threshold 0.60. Paired against the combined arm -0.012 (interval -0.052 to 0.035), against base -0.001.
Pre-registered verdict: agreement inconclusive, keeps boundary, does NOT keep matched. Reading: removing the auxiliary
assignment loses two references for no change in over-detection; the second objective knob, like the first, moves
nothing the labels did not move. The replicate selection rule found no arm clearing the density bar and chose its
default, the combined arm; `r4-combo-seed2` is training as the fourth run.

Fourth round-4 row and close, replicate `r4-combo-seed2` (the combined arm with seed 2; 2 h 45 min under host load),
dataset-v2 test at 0.45: 60 of 62 matched, median agreement 0.894, boundary 0.223 m, 15 predictions per tile, unbacked
share 15.8 percent, novel 5.3 per hectare; validation threshold 0.55. Paired against its seed-1 twin -0.009 (interval
-0.025 to 0.002), against base +0.002 (-0.005 to 0.009). The seed spread on this fold is therefore about 0.01 in
agreement, 0.005 m in boundary and 2 points of unbacked share, the same order as the stage-2 seed spread; every
difference between round-4 arms sits inside it except the one-to-many arm's two lost matches.

Round 4 is closed (four arms, 8 h 20 min of training, ledger `sam3-mac/round4/results.jsonl`, report `REPORT.md`).
Combined with round 3 (eleven arms in all): (1) over-detection is a label-density prior, cut about half by the
student-domain mix and a fifth more by the small-label floor; merging parts, outline backing, positive-weight balance
and one-to-many supervision change nothing beyond seed noise; (2) the combined labels are the only recipe that holds
the base's boundary (0.223 to 0.228 m) at about half the control's over-detection, and both seeds agree; (3) no arm
clears the pre-registered density bar (novel per hectare within 1.5 times the base) or sharpens outlines past the base
0.224 m. The recipe levers are exhausted for this judge; the next gains sit in the judge and the source data: the
reviewer's small-roof decisions as a human precision judge (six recorded so far against the 30 to 50 asked for), the
Christchurch 5 cm capture as a corner judge, and the LINZ 1 m surface model as the ledger's second leg. Nothing is
promoted.

## Reviewer evening set of 30 September 2026: solar_fixture_like (object-hints-v2)

The reviewer flagged four more objects on roof_training_250-29761 (recorded with provenance human:chat; set
`data/references/eyes-checkpoints/CP22/user-feedback-2026-09-30-evening.json`): two outlines cut by the frame although
their lot continues past it (9a207761109f808e, eade5df8fdf722f2), and two solar_panel detections that are rooftop
fixtures (5bac1a267bec9d1f, "a hot water or aircon system, not solar"; ca848734b13d480d, "a skylight or something").
The hint layer caught the frame cuts and missed both class errors: nothing judged whether a solar panel is plausible.
The frame cut itself is not fixed here; the scene image still cuts those two objects.

- object-hints-v2 adds one code, solar_fixture_like (review severity, solar_panel only), and changes no v1 code,
  threshold or sentence. It fires on any of three design values, chosen with the two reviewer objects visible and
  not fitted: rectangle fill (area over the minimum rotated rectangle) below 0.70, median grey inside the outline above
  the scene's own 60th-percentile grey (image only; an absolute 150 would miss 5bac1a267bec9d1f at 148), or area below
  1.2 m². The hint carries its evidence (area, fill, off-axis share, vertices per metre, median grey, the scene ceiling,
  median blue minus red, pixel count, the reasons met); the sentence names what to check ("check for a hot-water unit,
  air-conditioner or skylight"), never what the object is, and nothing is suggested or applied.
- Served state: the hints route reports `object_hints_served_version` (the version of the row actually served;
  `object_hints_version` stays the current rules) and `stale_reasons`: `model_signature` for rows of an earlier image
  version (judged when the editor names its signature) and `object_hints_version` for a row of earlier rules (judged
  always). The editor prints one notice per reason, and the rules notice only where the older rules differ for the
  object's kind ("the solar-panel check is not included" on a solar panel; nothing on a building, whose v1 hints are
  what v2 says); the Why-flagged footer names an older served version and /queues badge titles say "from earlier hint
  rules". Rewriting a scene (`scene_object_hints --write` or `teacher_checks --write-index`) replaces its v2 rows and
  drops its v1 rows.
- teacher-checks-v2: 'class error' expects solar_fixture_like, and each catch records how many objects of its kind
  in the scene the catching hint also fires on; a catch where every catching hint fires on all of them is marked
  non-specific. Evening set: fresh 4 of 4 (Wilson 95% 0.51 to 1.0); both class-error catches are non-specific, because
  the hint fires on all five solar panels of the scene (the two rejected and three saved as needs_adjustment with no
  recorded provenance), all on the bright reason. Served before the write 2 of 4 (the index held only object-hints-v1
  rows for the scene); after the one permitted `--write-index --checkpoint CP22` run on this set (44 object-hints-v2
  rows written, the scene's 44 v1 rows dropped, 45 lot-tags-v1 rows replaced, nothing else in the index touched) 4 of
  4, and the editor shows the hint on both objects. The three older sets are unchanged: fresh 35 of 36, served 30 of
  36, the same miss (324d3af9).
- Population flag rate, the over-flag proxy and never accuracy: 153 of 478 solar panels in the seeded sample (0.32,
  Wilson 95% 0.28 to 0.36; 0.79 below 0.08 m/px, 0.35 at 0.08 to 0.2, 0.21 at 0.2 and coarser) and 1,429 of 4,655
  across all 524 scenes with a live solar panel (0.307, 0.294 to 0.320). The bright reason does most of it, and a
  visual spot check found mostly real panel arrays among its fires at 0.2 m/px and coarser. Three refinements keep both
  reviewer cases and were measured, not applied (any of them is object-hints-v3, the user's decision): bright only with a
  neutral colour, blue minus red below 5 (0.217 of all solar panels); a ceiling from roof pixels instead of the whole
  frame (0.198); no bright reason at 0.2 m/px or coarser (0.180). Check: `verify:scene-hints-solar` (read-only).
