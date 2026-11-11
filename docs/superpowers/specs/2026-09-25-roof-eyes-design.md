# Roof "eyes": reference-anchored observability, multi-image evidence, and a measured improvement loop

Date: 2026-09-25. Status: approved for autonomous execution; the user reviews at
CRM checkpoints CP1/CP2/CP3 (their stated review mechanism). Design chosen by a
judge panel (measurement-first design, with grafts from the pipeline-integration
design). Ground truth for every claim below: `/tmp/addr-eyes/understand.json`
(subsystem maps) and `/tmp/addr-eyes/design.json` (designs + judges).

## 1. Problem

The Mooroolbark scan (run `500114985968d40b`, 257 z17 tiles, 10,072 owned roofs)
has evidence bands but no accuracy: 0 calibrated intervals, 31 high-band roofs,
and two disjoint signal systems (scene sidecars vs per-address records) that
never cross-reference. The flagship human-corrected roof, 27 Cambridge Road
(address 29790), is band **low** in the scan (object `44c724fd3480c167`, tile
`mooroolbark-50011498-17-118442-80417`) while the CRM record is 100/A and the
overlapping house-30286 capture (`37ff5c02f77869d5`, human-edited outline with 86
vertices / 32 roof-line edges) is band **medium** and hides the low one in the UI.
92% of low roofs are unreachable by the review loop because split auditing runs
over the whole 1152 px frame. `propose_roof` diagnostics (line-fit residuals,
per-end hip/gable support, topology cost) are discarded at persistence. Trees and
solar have no quality signal at all.

## 2. Principles (non-negotiable)

1. Labels are only human decisions (`review.json` status confirmed + geometry_reviewed,
   rejected; CRM rows with `roof_form_source`/`boundary_reference_source` = reviewer).
   Agent traces (CRM rows with `boundary_reference_source` = agent-imagery-trace,
   review-loop rows, `candidate`/`needs_adjustment` model objects) are tier-3
   observations and never enter an accuracy denominator.
2. Bands, cross-capture agreement, multi-date consistency and hip/gable support
   are diagnostics, never accuracy. The only accuracy statements are
   reference-anchored IoU / Hausdorff / line precision-recall and Wilson intervals
   from a pre-registered fixed-hash sample.
3. Every metric carries its population and denominator (owned scan roofs 10,072 under
   profile `53ea49d5…`; indexed 10,531; inventory 10,339).
4. New signals are **sidecars** in a separate index `data/reviews/eyes/eyes.sqlite`
   (writers via `scripts/ml/eyes_index.py`), keyed by (scene_id, object_id,
   model_signature, version). Nothing rewrites `latest.json`, `models/*.json`,
   `review.json`, `confidence.json` or `confidence.sqlite` rows until a CP3
   promotion decision by the user. The one production code change in CP1 is the
   additive `hidden_overlaps` annotation in `scene_confidence.query()`.
5. The saved scene PNG (`image_sha256`) stays the sole evidence gate. Fused or
   enhanced arrays may only add LSD/edge *views* whose lines still pass
   `line_evidence` on the original RGB. No super-resolution, sharpening or
   inpainting output is evidence. Gaussian splatting is **not applicable**: there
   are no camera poses or viewpoints in nadir orthomosaic crops; different dates
   differ by orthorectification, sun angle and content, not parallax.
6. Before/after is paired and isolated: identical input bytes, one variable changed,
   objects joined by lineage (id equality, else raw-ring IoU >= .8), deltas with
   bootstrap CIs and gained/lost counts; production never touched.
7. Protected references (house-30286 manifest `a983ad7a89b19808`, house-30296
   manifests `e055d6fd61e63b10`/`3322869b48a69d51`/`d4f3d42547234b76`, frozen run
   `plan.json`/`run.json`/snapshot, CRM feature 9 geometry) are hash-verified by
   `eyes_guard` at the start and end of every checkpoint; drift fails the checkpoint.
8. CRM reporting uses only `CreatePoi` (append-only, category `pipeline-checkpoint`)
   and read-modify-write `UpdateAddressMeta` with `eyes:` namespaced tags and a
   newest-first note line. Never `UpsertFeature` kind ROOF (would clobber the manual
   29790 outline via `apply_crm_roof_overrides`) or OBJECT (appears as an editable
   override in CrmPanel and in `export_training`/`address_review`). Writes use a
   dedicated `pipeline` principal (Bearer token, honoured under `-crm-noauth`).
   Never write `status='suppressed'`; tolerate the literal tags value `'null'`.
9. Nothing auto-approves; no neural weights are trained; the review loop's honest
   baseline is 0 candidate_ready / 252 rows.

## 3. Shared index: `scripts/ml/eyes_index.py`

`path(root) = root/'data/reviews/eyes/eyes.sqlite'`; `connection(root, readonly=False)`
(readonly opens `file:…?mode=ro`); `ensure_schema(db)`; `replace_rows(db, table, rows)`
(dict rows, INSERT OR REPLACE); `rows(db, table, **where)`; `fetch_object(root,
scene_id, object_id)` returns every table's rows for one object;
CLI `python -m scripts.ml.eyes_index --object SCENE OBJECT | --tables`.

Tables (all payloads are JSON text; every row carries `version` and payload keys
`code_sha256`, `computed_at`):

| table | key columns |
| --- | --- |
| references | ref_id PK, tier, source_kind, scene_id, object_id, crm_feature_id, address_id, spatial_group, split, west, south, east, north, version, payload |
| agreement | ref_id, scene_id, object_id, model_signature, version, payload; PK(ref_id, scene_id, object_id, version) |
| cross_capture | scene_id, object_id, other_scene_id, other_object_id, version, payload; PK(all but payload) |
| roof_diagnostics / object_quality / shadow_bands / multidate | scene_id, object_id, model_signature, version, payload; PK(scene_id, object_id, model_signature, version) |
| lineage | experiment_id, scene_id, baseline_object_id, candidate_object_id, version, payload; PK(experiment_id, scene_id, baseline_object_id, candidate_object_id) |
| snapshots / checkpoints | id PK, created_at, version, payload |

## 4. CP1 modules (27 Cambridge cluster: tile 118442-80417, house-30286, house-30296, address 29790)

| module | contract | version |
| --- | --- | --- |
| `eyes_guard.py` | `check(root) -> {ok, checks[]}`: manifests for house-30286/30296 (latest.json, PNG, review.json sha256), frozen run `plan.json`/`run.json`/`snapshot/` sha256s (recorded on first run to `data/reviews/eyes/guard-baseline.json`, compared after), CRM `crm_features` id 9 `geometry_json` sha256, `confidence.sqlite` scenes.mtime == latest.json st_mtime_ns for all indexed scenes. Non-zero exit on drift. | eyes-guard-v1 |
| `scene_confidence.query()` | additive: inside the dedupe loop, append `{scene_id, object_id, band, detector_score, image_line_mismatch_m, observed_segments, iou}` of each suppressed feature to the kept feature's `properties['hidden_overlaps']`; extract the pairing into a helper `suppressed_overlaps(features, polygons) -> list[(kept_index, suppressed_index, iou)]` reused by cross_capture. Existing test extended. | roof-evidence-v1 unchanged |
| `reference_registry.py` | `build(root) -> rows` over all `review.json` + CRM `crm_features` (mode=ro). Tiers: T1 confirmed+geometry_reviewed building; T2 human edit (`roof_line_edits` present, `needs_adjustment` with ring != model ring, CRM roof rows with reviewer provenance e.g. id 9); T3 agent observation (CRM `agent-imagery-trace` rows, review-loop rows, candidate objects); T4 context only (CRM id 22, kind `boundary`). Fields per §3 plus `image_identity_source` (scene image_sha256 or CRM boundary_image_sha256; `drawn_over='unknown'` when it matches no scene PNG), `label_property_conflict`, `spatial_group` from centroid z17 key, `split=group_split`. Writes `references` table + `data/reviews/eyes/registry/<registry_id>/summary.json` (registry_id = sha256(sorted rows)[:16]). | reference-registry-v1 |
| `reference_agreement.py` | `candidates(ref, root)` (confidence.sqlite bbox mode=ro + non-indexed scenes' latest.json, IoU >= .3); `metrics(ref, obj, scene) -> {iou, hausdorff_m, area_ratio, centroid_shift_m, roof_type_match, eave_delta_m, line_precision, line_recall, line_kind_precision, n_model_lines, n_ref_edges, image_identity same_image/different_image/unknown}` in local metres (CRM WGS84 -> scene frame via inverse of `scene_confidence.geographic`); line matching at <= .5 m / 10 deg. Writes `agreement`. | reference-agreement-v1 |
| `cross_capture.py` | `build(root, bounds)` reads confidence.sqlite (mode=ro), calls `suppressed_overlaps` (single source of truth), writes `cross_capture` rows `{iou, hausdorff_m, band, other_band, score, other_score, mismatch, other_mismatch, segments, other_segments, hidden_by, band_disagreement}`; summary rates with denominators. | cross-capture-v1 |
| `scene_roof_diagnostics.py` | `diagnose_object(obj, scene, image) -> payload`: calls `scene_roof.propose_roof` exactly as `scene_house.add_roof_estimates` does; keeps compact `audit`, `line_refinement` (accepted, detected_segments, matched_edges, residual before/after, max_shift_m, conflicting_matches_removed), `topology` before/after cost, per-end `{hip_support, gable_support, image_choice, reason}`, `form_uncertain`, `recomputed_matches_stored` (annotation equality); plus `edge_support` per outline edge (fraction of samples within 1.5 px of a Canny edge on original gray, CLAHE reported separately), per-vertex nearest-edge distance, `crease_contrast` per roof line (|mean L left band - mean L right band| in a 1 m band on original image; share >= 6 L units), `unsupported_fraction`. Failures recorded as rows with `error`. Pure function of PNG + ring + affine; no SAM3, no lock. | roof-diagnostics-v1 |
| `scene_object_quality.py` | per owned non-building object: `{kind, score, support_tier (<.5/.5-.65/.65-.8/>=.8 labelled 'detector support'), area_m2, log_area_z vs class median/MAD, parent_coverage, unparented (<.10 inside any building), building_overlap_fraction, same_class_overlap_max, edge_touch, plausibility_flags[]}`; scene/suburb rates with denominators. | object-quality-v1 |
| `shadow_bands.py` | `evidence_v2(obj, audit)`: drainage passed None -> info issue `drainage_not_assessed` (not invalid_surface); `image_line_mismatch_m=None` when observed_segments == 0; `mismatch_saturated` when == 1.5; info flags do not block `high`. Writes `shadow_bands` with `{band_v1, band_v2, flip, reasons_v2}`; flip matrix. Production `evidence()`/`audit_object` unchanged until CP3. | roof-evidence-v2-shadow |
| `scene_isolated.py` + `scene_extract --out-root` | `run(scene_id, out_root, *, detector_only=True, roof_models=True, evidence_views=None, image_variant=None)`: flock `data/scenes/.extraction.lock` (exit 75 if busy), sha-verify PNG, `analyse_scene(..., root=out_root)`, `save_scene(..., root=out_root)`, `assess(scene_id, root=out_root)`; refuses out_root '.' or under data/scenes; parcels land under out_root. `scene_lineage.link(baseline, candidate)`; `paired_compare.compare(baseline_root, candidate_root, scene_ids, registry)` with bootstrap CIs, gained/lost, band transition matrix, claims/cannot_claim. Reads candidate objects from model files, not only sqlite (owned-id gap). Tests mock the segmenter; no SAM3 run in CP1. | scene-isolated-v1 |
| `crm_checkpoint.py` | reads `GEO_SERVICE_URL` from `.env.local`; token from `.run/eyes-crm-token`; `whoami()`; `create_poi()`; `update_address_meta()` read-modify-write preserving status/owner fields, merging `eyes:` tags, prepending one note line, refusing status `suppressed`, tolerating tags `'null'`; `file_checkpoint(cp)` writes `data/references/eyes-checkpoints/<cp_id>/{report.json,REVIEW.md}` + `crm-writes.json` read-back diff; never emits UpsertFeature. | crm-checkpoint-v1 |
| `eyes_report.py` | `snapshot(root) -> {population, bands v1 & shadow v2, null-aware/saturation-aware mismatch quantiles (segments>=3), cross_capture rates, reference agreement by tier, object quality rates, code drift, protected_reference_checks}`; content-addressed `snapshot_id`; `diff(a, b)`. | eyes-report-v1 |

Taskfile: `eyes:guard`, `eyes:registry`, `eyes:cluster` (agreement + cross-capture + diagnostics + objects + shadow for the cluster), `eyes:snapshot`, `eyes:checkpoint CLI_ARGS="--id CP1"`.

CP1 success: registry tiers listed; agreement rows for every T1/T2 reference in the
cluster (CRM 9 vs `44c724fd3480c167` vs `37ff5c02f77869d5` as a table); cross-capture
rows reproduce `query()`'s suppressed pairs and mark the hidden low; diagnostics for
>= 100 of ~106 cluster roofs; object quality for the cluster's trees/solar; shadow
flip list; guard passes before/after; `task test` + `task lint` green; CP1 POI visible
via `ListPois addressId=29790`; production files byte-identical.

## 5. CP2 (suburb expansion) and CP3 (measured effect)

CP2: diagnostics for all 10,072 owned roofs (background, timed on a 50-roof stratified
sample first), object quality for all trees/solar, shadow bands for all, cross-capture
suburb-wide, sampled calibration plan (`reference_calibration.py`: one fixed-hash owned
roof per (band, validation core-tile group) using `group_split` on the object's centroid
z17 key; calibration-only policy; SAM3 64 px margin caveat stated; review queue with
`/scenes` links; Wilson only at >= 20 groups), `/api/scenes/eyes` route, processing
workspace/map surfacing (null-aware mismatch, hidden overlaps, reference IoU with a
cross-imagery badge), CrmPanel rendering of `pipeline-checkpoint` POIs and a read-only
feature-properties toggle, `review.basemap_mode` on saves, one web rebuild via the
documented launchd replacement, snapshot diff, CP2 POI + address tags.

CP3: `scene_stack.py` registers Esri Wayback dates **onto the saved scene grid**
(saved PNG is the reference; existing RANSAC gates plus median hold-out residual <= 2 px;
per-roof local ECC refinement recorded), stack median/min/max/count/disagreement and an
edge-consensus raster only when N >= 3; `multidate_agreement.py` (edge support by date,
boundary_edge_consensus, ridge support; no SAM3); `roof_line_fit.image_segments(extra_views=…)`
with `view_origin` and the original-RGB gate intact (default off; threaded through
`refine_roof_lines`, `roof_topology`, `propose_roof`, `add_roof_estimates(evidence_views=…)`);
experiments via `eyes_experiments.py` (code_drift, band_v2, extra_views, split_policy_core
opt-in in `roof_review_loop.protected_reason`, roof-form test evaluation if the one missing
human test-gable label arrives, SAM3-on-median isolated detection for the cluster); sampled
Wilson intervals if >= 20 groups; CP3 POI with claims / cannot_claim; promotion decisions
(band v2 re-assess + new rollout-verification JSON; extra_views into production needs a new
`--prepare` ~7 h; roof-form.json promotion changes `profile()`).

## 6. What not to do

Rewrite any `latest.json`/`models/`/`review.json`; re-run `assess()` on production scenes
or bump `roof-evidence` VERSION before CP3 approval; write ROOF/OBJECT CRM features for
reports; count agent traces as labels; present bands/agreement as accuracy; mix profiles
or populations; start a new suburb `--prepare` or suburb-wide SAM3 multi-date runs during
CP1/CP2; feed fused arrays without the original-RGB gate; attempt Gaussian splatting;
change `image_split_audit`, training eligibility or default loop selection (opt-in only);
run `scene_self_review --repair`; average mismatch over zero-segment roofs; kill only the
web PID when rebuilding (use `task stop` + launchd replacement).
