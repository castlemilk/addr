---
name: addr-3d
description: Use when working on the addr repo's 3D building pipeline — AI roof extraction (scripts/ml), per-address records, MBTiles tiles, MapLibre RealRoofLayer, Deep 3D Studio, heights/trees, or when debugging missing 3D geometry in the web UI.
---

# addr 3D roof & building pipeline

Repo: Go tile/geo service + Next.js 16 frontend + Python 3.12 ML pipeline.
Flow: aerial imagery → AI segmentation/depth → tile props (roof type, eave/ridge,
angle, area, kind) → per-address records → MapLibre roof meshes + Deep 3D Studio.

Local services are currently managed by user-session `launchd` jobs
`com.geoau.addr.{web,geo,ml}` after shell-started workers repeatedly exited.
Check the exact job arguments before process actions; remove the specific job
before replacing it, since killing its PID alone can respawn it. `task stop`
does not unregister these jobs. See `docs/local-services.md` and
`.run/managed-services.json` for ports, logs and lifecycle details.

For low-confidence tile review and iterative quality improvement, load
[the review-and-reflection workflow](references/self-review.md). It routes real
visual defects to context recovery, segmentation, roof-family, line-fitting or
surface fixes, and explains how to test changes without turning agent guesses
into labels. `task scene:review-pack CLI_ARGS="--run RUN_ID"` renders original,
model and detected-line comparisons for a completed candidate batch.

## App shell and review queues (26 September 2026)

For user-directed roof polygon tracing or tighter image boundary edits, load
[addr-roof-tracing](../addr-roof-tracing/SKILL.md). Its full perimeter audit and
source-pixel inspection/refinement loop complement this pipeline guide; agent
drafts remain separate from approved references and model inputs.

Workspace navigation is owned by `src/components/shell/{WorkbenchNav,WorkbenchHeader}.tsx`
(+ `shell.css`); pages keep their own h1 and pass their drafts guard via `onNavigate`.
`/queues` lists the calibration sample, low roofs hidden under higher bands, the
self-review batch and pipeline checkpoints; all read-only. `/api/scenes/processing`
serves `?part=summary` first, then paged `?part=roofs`. Rebuild via `task build:web` and
replace the launchd web job per `docs/local-services.md` (never kill only the PID).
Screenshots for UI review: `APP_URL=http://localhost:3008 node scripts/ui-shots.mjs`.

## Roof "eyes" sidecars (25 September 2026)

Observability signals live outside production in `data/reviews/eyes/eyes.sqlite`
(`scripts/ml/eyes_index.py`): tiered human-reference registry, reference
agreement (IoU/Hausdorff/roof-line P-R), cross-capture disagreement (also live as
`properties.hidden_overlaps` from `scene_confidence.query()`), recomputed roof
diagnostics, object plausibility, shadow band v2, isolated re-inference and paired
comparison. `task eyes:guard` must pass before and after any checkpoint; checkpoints
publish to the CRM only via `CreatePoi` + read-modify-write `UpdateAddressMeta`
(never ROOF/OBJECT features) using the `pipeline` principal. Read
`docs/roof-eyes.md` and the spec `docs/superpowers/specs/2026-09-25-roof-eyes-design.md`
before extending them. Agent observations are tier 3 and never labels.

## Coordinate and roof-form checks (22 September 2026)

Decode Python vector tiles with `default_options={"y_coord_down": True}` before
calling `tile_local_to_lonlat`. The decoder's default y-up output mirrors roofs
within a tile. `wgs84_to_mvt_local` now returns native y-down coordinates without
another flip. Earlier visual findings of roofs on trees/roads can be artifacts
of that bug; re-export records before interpreting them as segmentation errors.

`scripts/ml/roof_structure.py` checks emitted faces for planarity, coverage,
overlap, seam continuity, missing apexes and interior drainage basins. Failed
image/depth reconstructions can use a validated straight-skeleton hipped prior
(two-plane gable for rectangular gabled footprints). Output provenance is
`structural-prior`, not image-confirmed geometry; retain both audits. Never
interpret a passing geometry audit as survey accuracy or building compliance.
Details and sources: `docs/roof-structure.md`. Four agent-traced image references
are in `data/references/roof-outlines-2026-09-22.json`; these are not survey labels.

`roof_assembly.py` joins attached SAM roof sections before shared roof fitting.
It requires overlapping raw detector masks, at least 1.5 m of supported contact,
and similar local material chromaticity. Proximity/parcel membership never joins
detached roofs. Raw components and junction evidence are retained under
`boundary_refinement.assembly`. Cleanup fits the raw union once (not repeatedly
fitted component edges), preserving existing holes and the metric/area limits.
Expanded scans can replace clipped SAM model seeds of ANY class with fuller
predictions (`scene_extract.extract_objects`): a larger candidate covering .8 of
a seed takes it over (.95 for assembled roofs), and records every seed it took in
`replaces` (a list; one whole roof may replace several parts). A carried seed that
nobody completed and whose outline runs along the old frame line gets
`clipped_by_frame: [x0, y0, x1, y1]` (`scene_expand.analyse_expansion`); the
editor shows it as a frame cut. Review/CRM geometry is excluded from both. This
is an inference prior, not trained weights.
`test_roof_assembly.py` covers attachment, separation, courtyards, parent identity
and continuous audited roof planes. Confirmed labels still require human review.

`roof_junctions.py` adds bounded common-ridge alternatives for short internal
hip clusters. `roof_topology.py` ranks physical planes/creases/junctions, not
mesh pieces or editor segments, with an independent image-evidence veto.
Unequal wing pitches can meet at one ridge junction; eaves, footprint coverage,
planarity, seams and sampled runoff remain checked. This is a general inference
prior, not retrained neural weights or building compliance. Version/hash live in
`scene_house.add_roof_estimates`; never insert reviewer coordinates into pure
model runs. See `docs/roof-training.md` for the revision-6 feedback comparison.

`roof_wings.py` adds overlapping pitched-wing candidates so ridges can continue
through L/T junctions to end walls, with optional end hips. Branches stop at a
through-ridge; image-supported roof ends and the image-mismatch veto constrain
simplification. `roof_line_fit.py` nodes shared T-junctions before fitting and
ties coplanar fragments together. Surface coverage, seams and runoff remain
independently audited. Revision-11 feedback and before/after measurements are
in `data/references/roof-wing-feedback-2026-09-24.json`. This changes inference,
not neural weights; saved line edits remain separate and unchecked drafts.

`roof-envelope-v4` fits shared eave corners jointly within the existing raw-mask
distance/area budgets, splitting only unsupported runs instead of abandoning
all straight edges after one unstable intersection. Image-cut endpoints stay
fixed and holes/recesses remain checked. Always refit from retained raw masks,
not an already simplified outline. `registered-roof-lines-v3` polishes shared
plane constraints so numerical storage seams do not become extra folds.
Boundary changes still require fresh surface/runoff checks and visual review:
passing those checks does not establish apex accuracy. See
`data/references/roof-boundary-feedback-2026-09-24.json`. Preserve saved line
feedback and its original outline via canonical API recovery; never silently
rebind the user's lines to a changed boundary or mark them training-eligible.

`roof-envelope-v5` adds `roof_boundary.py` / `canopy-eaves-v1`: same-frame SAM
tree detections downweight obscured eave samples; visible sides establish a
shared angle grid while substantial visible diagonals remain independent.
Hidden continuation has separate metric/area gates and is recorded under
`boundary_refinement.construction.inferred_edges`; it is not observed truth.
Crop cuts, wholly hidden sides, insufficient visible support and mixed axes
retain the bounded mask fit. Extract trees before buildings and pass model
canopies through both component and assembled-roof fitting. Never use reviewer
tree geometry or ground vegetation as pure-model occlusion evidence.
`registered-roof-lines-v4` keeps level eaves fixed but allows above-eave corner
heights (clipped/inferred slopes) to refit with shared planes; every fit gets
planarity polishing before later clipping. This avoids unnecessary hips caused
by locking estimated slope heights. Geometry/runoff checks still apply.
See `data/references/roof-canopy-feedback-2026-09-24.json` and regression tests.

## 3D-mode eyes (`studio_check`)

`registered-roof-lines-v5` adds conditional parallel-eave ridge constraints to
the shared vertex/plane fit: symmetric seed roofs stay centred unless a long
parallel image observation supports an offset. Unequal seed pitches, nonlevel
eaves and unsupported pairs are excluded. The outside-eave L-junction valley is
covered by rotated synthetic and cached-observation regressions. Model reruns
must preserve straight outlines and recover saved feedback through the canonical
API without rebinding its source outline. See `docs/roof-training.md` and
`data/references/roof-ridge-feedback-2026-09-24.json`. These are inference priors,
not trained weights or verified construction geometry.

`task studio:check CLI_ARGS="--address-id 29790 --expect concave"` (module
`scripts/ml/studio_check.py`, headless driver `scripts/studio-check.mjs`, MCP
`studio_check`) validates the Deep 3D Studio per address and returns
PASS/REVIEW: (1) shape metrics from the record — solidity, max notch angle
(concave needs solidity < 0.95 or a notch ≥ 225°; digitizer wobble sits
~209°), partition coverage; `--expect concave|convex` pins the belief, so a
bar ring checked against an L expectation fails LOUDLY instead of passing
quietly; (2) the app's own `[Deep3D]` console tri counts (wall/roof —
deterministic empty-mesh signal); (3) canvas mesh-fill tripwire + the shot
itself under `data/reviews/<id>/studio-check.png`. Pixel checks are coarse
by design (UI chrome overlaps the canvas box) — tris + shape carry the
verdict, the shot carries the review.

## Roof surface material (colour follows corrections)

Mesh colour is type-hashed by default, so a corrected roof can render the
wrong colour (27 Cambridge's terracotta tile came out slate). CRM roof
overrides may carry `properties.roof_material` (e.g. `terracotta`, set via
UpsertFeature); `apply_crm_roof_overrides` copies it onto the record as
`roof_material`, and both renderers honour it through `roofColorFor()` in
`src/lib/roof-geometry.ts` (`ROOF_MATERIAL_COLORS` registry — extend there,
never in the renderers). Geometry untouched: material rides the existing
properties-preserved update path, covered by
`test_apply_crm_carries_roof_material`. Deliberately correction-only for
now — pipeline-wide imagery-hue classification is a separate project.

## Per-address review loop

`scripts/ml/address_review.py` (and the `addr-review` MCP server) renders
`data/reviews/<id>/site.png` (overlays), `3d.png` (studio canvas via the
`/?lat=&lon=&studio=1` deep link + `scripts/studio-shot.mjs`) and metadata JSON.Use it to QA boundaries after pipeline changes: neighbouring MVT outlines in
the site image are the control — if they hug roofs, the overlay projection is
right and any red misalignment is archive data. Known finding (2026-09):
archive footprints in tile 40208 are merged/offset, SAM3 masks are materially
tighter; rebuild with `--sam3 --sam3-trees`, then re-run `task addresses` and
the review. Caveat (2026-09): SAM3 refinement fixes boundary precision but not
vegetation false positives (e.g. address 29790's ring sits on a verge);
`--sam3-min-evidence 0.2` drops those but also halves address coverage
(352 vs 675 records) — it is a review tool, not a default. Per-address CRM
 corrections are the fix for individual bad footprints. MCP config lives in `.opencode/opencode.json`; restart opencode to
load it.

## Attribution audit (whose roof is this?)

`task attribution` (`scripts/ml/attribution.py`, MCP `attribution_audit`)
checks every record's ring against all G-NAF pins: if another address's pin
fits the ring better than the record's own pin by >= 10 m, it's a suspect
(cross-address tier first). Calibrated on the demo bbox: median 0.0 m, p99
8.9 m — the 27-Cambridge/Unit-4 mixup was the lone max at 13.3 m, so the
threshold is high-precision, near-zero false positives. Site renders label
nearby pins with house numbers (`3/29`, `27`), so a misattribution reads at
a glance (wrong pin inside the red ring). Detection is automatic; corrections
stay human-confirmed via Propose box / Draw roof — never auto-applied.

## Roof-box proposals (user-drawn feedback)

Address CRM -> "Propose box": press-drag-release a rectangle on the map
(requires `crm.object.write`). It saves as a CRM OBJECT feature labelled
"roof-box proposal" — deliberately not a roof override, so the committed
extraction is untouched until a proposal is measured and applied. Boxes render
dashed magenta on the map and in site reviews. Agent side:
`scripts/ml/proposal.py show --address ...` (box/record areas, IoU,
centroid shift, adopt >= 0.7 / adjust >= 0.4 / reject verdict),
`... apply --address-id N` (promotes to a ROOF override via `CRM_TOKEN`,
then `task addresses`), MCP `proposal_feedback`, and
`scripts/box-draw-test.mjs` (headless drag-draw check asserting
`window.__addrProposal`).

## CRM & RBAC

`crm.v1.CrmService` (proto `proto/crm/v1/crm.proto`) edits per-address metadata,
roof/tree/object boundary overrides, and POIs. RBAC: roles (viewer/editor/admin)
grant scopes (`crm.read`, `crm.address.write`, `crm.roof.write`, `crm.tree.write`,
`crm.object.write`, `crm.poi.write`); API keys are SHA-256 hashed and may be
restricted to a scope subset. The auth interceptor fails closed on unknown
procedures; feature writes check kind-specific scopes in the handler.
Local dev needs no keys: `task start:server` runs the Go service with
`-crm-noauth` (also `CRM_NOAUTH=1`), so the CRM panel signs in as local admin
and writes work with no token (audited as `local`). Never pass `-crm-noauth`
outside loopback dev — the binary defaults to auth-on. Panel auth details:
sign-in probes anonymous-first (heals stale stored keys by clearing them when
the server takes noauth); a dead service reads as `Code.Unknown` in
connect-web v1 (not Unavailable) — both map to the "service unreachable" hint,
verified by `scripts/errmap-test.mjs` + `scripts/crm-auth-check.mjs`.
Manage credentials with `task crm:user` / `task crm:key` / `task crm:list`
(CLI `cmd/crmctl`). The CORS config must allow the `Authorization` header or
browser Connect calls fail with code 2. CRM data lives in `crm_*` tables in the
same SQLite file (WAL; the geo store is read-only, the CRM store is the single
writer). UI: "Address CRM" in the address card hides the modal during drawing
(the map must be clickable), and saves via the generated Connect-ES client in
`src/lib/crm-client.ts`.

## Services & commands

Scene export schema 6 adds `roof-feedback.jsonl`: explicit observed roof-family
and image-XY linework checks, separate from segmentation and measured 3D labels.
`scene-roof-feedback.ts` binds checks to copied outline/XY snapshots; changing Z
preserves XY feedback, changing an outline invalidates both, and changing lines
invalidates linework. `scene_roof_feedback.py` independently checks full visibility,
confirmed outlines, projected face coverage and all geographic split gates.
Original models, template selection and outline swipes cannot create these labels.
Building inspection and Training coverage expose the checks/counts. Exporting
does not fit weights. `task scene:train-roofs CLI_ARGS="--dataset data/training/scenes/VERSION"`
consumes explicit hip/gable families through `scene_roof_dataset.py`, independently
checking frozen review snapshots, imagery/split provenance and repeated physical
roofs. `train_scene_roofs.py` balances areas and roofs; validation selects weights
before test images are opened. Insufficient labels publish a readiness report,
not a model. No deployment or neural/apex/boundary training is implicit.
`roof_form.roof_end_evidence` is shared with inference: all end comparisons use
the same unchanged prior, before applying choices, without installed-model or
reference-graph leakage. See `docs/roof-training.md` for gates and run artifacts.
`task verify:scene-roof-feedback` verifies the saved-feedback/export lifecycle.
`task verify:scene-roof-lines` covers the roof-line workspace (box selection, one-step bulk
delete/undo, "Edit outline" with re-anchored pending lines) on a scratch `qa_rooflines_*` scene;
`task eyes:line-corrections` writes the reviewer line-corrections rows (`line_corrections` table).

`task scene:validate-candidate CLI_ARGS="--candidate ID --dataset data/training/scenes/VERSION"`
checks frozen proposal weights on additional fully reviewed validation images.
It preserves the original selection evaluation and all source annotations. The
candidate CRM's **Evaluation run** selector loads the independent checks; check
identity must survive selection, filtering, reload, draft import and retry. See
`docs/scene-proposal-training.md` for scope and immutable output provenance.

Whole-image scene review now lives at `/scenes`; see `docs/scene-review.md`.
`task scene:evaluate` compares original scene estimates with eligible, fully
scanned and reviewed image layers. It uses maximum one-to-one matches at 0.5
polygon IoU and reports extras/misses plus per-image union overlap. This is not
COCO AP or 3D accuracy. `scene_training` exposes the same report in coverage and
writes `evaluation.json` on export. Preserve empty denominators, hole geometry,
model/review provenance and all geographic/class gates; never tune on test data.
`SceneGroupEditor` provides cross-layer image/checkbox membership, rename/remove
and group 3D inspection. `scene-groups.ts` changes affiliation only; preserve
outline approvals and physical parents. Group isolation passes an exact ID set
to `setSceneIsolation`; hidden parents remain available to `roofMount`.
`scene-feedback.ts` carries group removals through re-extraction and clears stale
references. `task verify:scene-groups` checks the saved-feedback lifecycle.
`SceneObject.holes` retains interior gaps from SAM3 through editing, area,
split/merge, roof support and meshes. Never discard a whole detection because it
has holes or fill all holes globally. Gap edits clear building roof proposals
and reopen 2D review. `triangulateFootprint` and `parentRoofMesh` keep courtyards
open; proposed roof skeletons and the independent annotation audit accept holes.
Cars with gaps and partial/unassessed gapped trees use their visible-footprint
preview. Explicitly fully visible trees use `treeSurfaces(ring,model,holes)`;
both crown surfaces leave gaps open, and trunk clearance includes inner rings.
Never change visibility automatically to enable a complete crown. Scene export schema 5
uses uncompressed COCO RLE for gapped outlines. `task verify:scene-gaps` exercises
editing, validation, save/undo, roof generation and mobile behavior.
`scene-queue.ts` shares filters and progress counts between the saved-image API
and editor. Needs-edit is unresolved; active counts exclude rejected/replaced
objects. **Next image** preserves filters and opens a matching object, skipping
empty images. Drafts/open operations block navigation; model changes remain
flagged. Object IDs persist in URLs. `task verify:scene-queue` checks counts,
navigation, save/undo, failed saves, retry, response races and mobile layout.
`task scene:extract` proposes trees, roads, cars, buildings, solar panels and
surface objects from captured cohort images or `--bbox WEST SOUTH EAST NORTH`
map areas (10–250 metres). `/scenes/new` and the main map's **Review scene area**
button expose capture; `--scene-id ID` re-runs inference on a saved region image.
`--scene-id ID --expand-context` (also **Analyse surrounding area** in the parcel
controls) scans a 1024px registered image with the original 512px pixels pasted
verbatim at (256,256). It seeds only the prior MODEL proposals, never review
geometry, and can discover garages outside the earlier crop. Same-scene recovery
permits this exact integer translation only after verifying image hashes and
byte-identical central RGB pixels; the metre origin and roof-line vertices stay
unchanged. Image-coordinate outline bindings shift together. History remains
immutable, carried outlines need review, and the expanded spatial split is held
unassigned. Arbitrary recaptures/resampling still cannot transfer feedback.
Known CRM roof faces are reprojected into the new local metre frame. Missing
imagery tiles fail capture; mixed spatial split regions are withheld from COCO.
Outside the verified padding case, re-extracted scene feedback can be previewed
and carried forward only when the immutable old snapshot has identical imagery
and coordinate frames.
`scene-feedback.ts` retains edited objects, apexes and parent/group context,
clears outline/layer approval and leaves changed-ID detections for review.
Scene solar arrays/skylights use `scene-roof-mount.ts` to fit their footprint to
the parent's current roof triangles. `outlineSupport` independently checks the
current building outline, so stale roof faces cannot bypass an edited boundary.
`SceneRelationships` separates outline containment from roof-face coverage,
offers building parents ranked by containment, and excludes descendants from
parent choices. Related-object navigation preserves drafts and provides a return
to the original filters and comparison view. `task verify:scene-relationships`
checks this flow. Geometric containment never establishes measured elevation.
Solar banks delegate to `scene-solar.ts` for
one area-weighted plane, with editable tilt, downhill azimuth and minimum
clearance. Fit quadrature is independent of roof tessellation; the plane clears
all clipped support vertices. Large roof variation is a modelling warning, not
permission to bend a bank across faces. `pavedSurface` supplies the shared closed
constant-thickness mesh. Absolute placement anchors the lowest underside and
retains manual tilt. `SceneModel.solar` survives model recovery, clears on class
changes and resets on split/merge; 2D label approval remains independent.
`task verify:scene-solar` covers controls, comparison, save/undo and recovery.
Skylights retain their roof-surface fit. Missing support or overlapping faces keeps
absolute-base rendering with an inspector hint. `model.placement = "absolute"`
is the explicit override. These derived elevations are never measured labels.
New scene buildings can use **Create roof shape** to obtain editable gabled,
hipped, flat or image-guided roof proposals. `scene_roof.py` uses the shared
physical audit; bounded simplification fits planes around pixel stair-steps and
clips them back to the exact saved outline. The API proposes a draft only,
checking roof permissions and scene/model revisions. Image ambiguity and template
provenance remain explicit. Scene apex editing starts focused on the roof;
**Whole image** restores context. `task verify:scene-roof` checks creation,
focused pointer registration, decimal apex editing and save/reload/history.
`task scene:training` exports only
current reviewed outlines, with complete-layer gates for COCO. `/scenes/training` uses the same Python eligibility calculation for live class/split coverage and exact-object review links. Scan completion never implies a checked negative. Unknown/missing split groups and cross-split group/image repetition are withheld. `scene_split.py` also verifies the entire pixel frame against z17 split geography, so an address pin cannot approve neighbouring context across a split boundary. Export publishes versioned labels atomically without fitting weights; `task verify:scene-training` verifies the coverage workflow. Immutable model
snapshots and revisioned reviews live separately in `data/scenes`. Scene
confirmations do not verify heights or roof apexes. `task verify:scenes` covers
API and browser interactions with isolated fixtures. Scene geometry is shared
in `src/lib/scene-geometry.ts`; retain metre-space/base-elevation consistency.
`scene-tree.ts` triangulates concave canopies into closed upper/lower surfaces;
do not restore a radial fan, which spills across notches. `SceneModel.tree`
stores editable crown profile and base ratio; these remain estimated structure.
`Scene3D` can focus the selected object without changing annotation coordinates.
`scene-car.ts` fits closed, sloped vehicle cabins inside the metre-space outline.
Use oriented bounds rather than a vertex-average centre. `SceneModel.car` stores
reviewer body-style, front-bearing and cabin-proportion estimates; the aerial
arrow makes the front assumption visible. They persist/recover separately from
outline approvals and are excluded from measured-height targets. `task
verify:scene-car` covers editing, comparison, undo, saving and model recovery.
`polygon-clipping.ts` is shared by cabin fitting and roof-mounted objects.
`scene-visibility.ts` distinguishes reviewed full/hidden/clipped extents from
unassessed outlines. A border touch is only a cue, not a saved label. Partial
cars and trees render a flat visible footprint rather than fitting a full cabin
or canopy/trunk. Saved shape settings remain intact. Visibility survives history
and same-image recovery but resets on split/merge. Training exports retain
visible-mask semantics and explicit visibility; no hidden geometry is invented.
`task verify:scene-visibility` covers the controls, preview and persistence.
`scene-surface.ts` also uses it for planar/crowned road, driveway and footpath
meshes. `SceneModel.surface` stores grade, crossfall and bearing estimates. The
lowest underside is `base_m`; `height_m` is constant vertical thickness. Match
subdivided/near-collinear triangle edges before adding skirts to avoid internal
walls on pixel outlines. Editing 3D metadata preserves 2D class/layer approval;
outline/class/decision edits clear it. Split/merge resets slope settings. No
surface estimate becomes a measured-height target. `task verify:scene-surface`
covers controls, approval independence, persistence and model recovery.
`Scene3D` can isolate a selected object and its attached descendants to remove
occlusion. `setSceneIsolation` changes mesh visibility without changing saved
review/layer state. Picking must ignore hidden meshes; **Whole scene** restores
the surroundings as well as the camera frame.
`scene_instances.py` and `/api/scenes/instances` create split/merge drafts with
exact source coverage. `scene-instances.ts` owns replacement lineage: reciprocal,
acyclic `derived_from`/`replacement_ids` links. `superseded` sources are hidden
from active rendering and excluded from both training targets and false positives.
Do not convert them to rejected detections. Attached objects require complete
containment for automatic reparenting. `task verify:scene-instances` verifies the
draft/save/undo/history flow. Existing roof proposals validate replacement
objects without discarding their persisted lineage.

```bash
task start            # ML sidecar :8090, Go :8085, web :3004 (PIDs in .run/)
task status           # includes health checks
task stop             # stops ALL services — there is no stop:web/stop:server task
task test             # go test + pytest + tsc
task lint             # go vet + eslint + ruff
task verify:ui        # headless Chrome smoke test, screenshots -> /tmp/addr-verify
task tiles -- --bbox 145.308,-37.797,145.320,-37.786 --zooms 16 --heights --trees --imagery-offset 3 --simplify-px 0.75 --min-area-m2 12 --no-osm --out data/australia_tiles.mbtiles
task addresses -- --bbox ...   # per-address roofs -> public/address-roofs.json
task sam3:review -- --bbox ... --zooms 16 --existing data/australia_tiles.mbtiles  # standalone SAM3 boundary review
```

`task scene:experiment CLI_ARGS="--dataset data/training/scenes/VERSION --kind solar_panel --prompt 'solar panel' --prompt 'solar panel array'"`
runs frozen train/validation prompt comparisons through the production extractor.
It excludes test images, verifies hashes/class completion/COCO eligibility, and
writes predictions and reports only to `data/experiments/scenes/RUN/`. Results
are not annotations or trained weights. Component/code hashes identify a run;
completed per-image results support resuming it. Keep the shared extraction
lock and frozen split boundaries intact. Do not promote a prompt from a small
validation cohort without checking its per-image precision/recall tradeoffs.
`task scene:train-proposals` fits an experimental logistic mask-quality scorer
from one frozen prompt run and its dataset. It uses training-only scaling and
area-group weights, a fixed threshold, and validation-selected regularization.
Its scores are uncalibrated; retained boundaries and all heights are unchanged.
The initial solar candidate remains outside the production detector. Details
and limits: `docs/scene-proposal-training.md`.
`/scenes/candidates` exposes frozen train/validation masks, kept/filtered decisions
and a separate current-review overlay. Geometry overlap suggests an existing
annotation; unmatched proposals can be explicitly imported through the canonical
scene save path as unapproved drafts. Imports preserve prior edits/history, reopen
class completion, and reject changed frames or stale reviews. Never automatically
replace reviewed masks with candidate masks or promote candidate scores to labels.
`task verify:scene-candidates` covers provenance, imports, retries and browser states.

`task scene:batch CLI_ARGS="--cohort roof_training_250 --limit 250 --plan"` freezes
a plan for unextracted, split-safe train/validation images. Resume with
`--resume BATCH_ID`; `data/scene-batches/<id>/run.json` checkpoints each image.
Existing scenes/reviews and held-out test images are preserved. Inspect actual
process liveness before resuming an interrupted batch; a stale state file alone
does not prove work is stopped. See `docs/scene-batch-review.md`.
`scene:train-proposals --supplemental DATASET` can fit explicit reviewed instance
matches from additional training images. Unknown proposals stay unlabelled and
partial images never enter evaluation. Candidate provenance records both frozen
datasets. See `docs/scene-proposal-training.md` for limits and the current result.

Note: this Taskfile declares a global `CLI_ARGS: ''` var, which shadows go-task's
`--` passthrough — invoke tasks as `task <name> CLI_ARGS="--flag value"`.

Dual-path parity warning: when the Go service is unreachable the UI silently
falls back to the in-browser engine (`src/lib/geo-engine.ts` over `/api/
reverse`, sql.js data). Any reverse-geocode rule change MUST be mirrored in
both (`headlinePoiRadiusMeters` in `internal/geo/store.go` == 25 m,
`HEADLINE_POI_RADIUS_M` in `geo-engine.ts`; `place` is headline-only in
both). A drift here once re-joined LKPT Fitness onto 27 Cambridge whenever
the server died. The sidebar shows an amber "Fallback data" badge while on
the fallback path; the Go process itself is fragile under box load (several
unexplained deaths, no panics — check `task status`, restart with
`task start:server`). Verified both paths: `scripts/live-check.mjs`,
`scripts/fallback-check.mjs`.

Boundary-accuracy knobs: `--imagery-offset 3` (z19 source, tightest outlines,
~2-3x runtime), `--simplify-px 0.75`, `--min-area-m2 12`. SAM3 (`--sam3`) is
opt-in: ~40 s/tile extra, marginal gains where DINOv3 outlines are already tight.

## SAM3 review loop (`scripts/ml/sam3_review.py`)

Run SAM3 independently to validate boundaries before rebuilding. Writes per tile:
`preview_z_x_y.png` (red = existing archive footprints, yellow = SAM3 would
replace, green = SAM3 mask outlines, cyan = detection boxes with prompt/score/
area labels), `sam3_proposals.geojson` (WGS84 + `prompt`/`status`/`match_iou`),
and `report.json` (replace/extra/missed + mean best IoU, split by prompt).
`--prompts house,tree` reviews roofs and canopies together; SAM3 boxes are
normalized xyxy in model-input space, so multiply by the window image size.
High `missed` + low mean IoU = archive footprints don't line up with the imagery
(offset/merged/vegetation); the preview shows where. Bake in with
`task tiles -- --sam3 --sam3-trees`, then re-run `task addresses`.

## Rooflines (TU Delft port) — inputs

`scripts/ml/ortho.py` builds the closest substitute for the paper's true
orthophoto: a median composite of Google + ESRI imagery (phase-correlation
aligned, CLAHE grey) used for line detection; measured 1,986 vs 1,514 LSD
segments on one window. `scripts/ml/pointcloud.py` is the paper-style height
source: a dense cloud unprojected from the depth model, fitted per partition
part with robust metre-space least squares (median/MAD gating) — planes are
only a *height estimator*, as in the paper, never the source of the rooflines.
`scripts/ml/silhouette.py` snaps a ring onto image edges along vertex normals
(opt-in via `task addresses CLI_ARGS="--silhouette-snap"`; acceptance requires
an edge-score gain and IoU >= 0.5) and reports split candidates for merged
blobs via adjacent-part height drops. Measured snap: 25008 mean shift 2.07 m,
IoU 0.60 (the terrace blob is 40% too large, so snapping alone cannot fix it);
29790 mean 1.74 m, IoU 0.79. Do not enable snapping globally without reviewing
the affected rings.

## Rooflines (TU Delft port)

`scripts/ml/rooflines.py` ports the tudelft3d Roofline-extraction pipeline to
our stack: cv2 LSD -> mean-shift angle/offset regularization -> ray propagation
(k intersections, clipped to footprint) -> shapely planar partition -> sliver
coalescing -> per-face depth plane fits -> physical-scale coplanar grouping ->
plane reconciliation (shared vertices get one height, each part re-fitted, the
footprint boundary pinned to the eave) -> rooflines + triangulated per-plane
surfaces. Detection is tuned fine (1.8 m min segment, 3 deg / 0.8 m mean-shift
bandwidths) so the partition is dense enough to follow roof edges. Run
`task rooflines CLI_ARGS="--address-id 29790"`, or the MCP `rooflines` tool (writes
`data/reviews/<id>/rooflines-site.png` + `rooflines-mesh.png`). Records carry
the partition after `task addresses CLI_ARGS="--rooflines"` (compact parts: ring
+ per-vertex z + rooflines; ~1.3 KB/record, 6 workers, ~10 min for 665
records). `Deep3DStudio` builds the per-plane mesh via
`buildRooflineMesh`/`buildWallStrip` for any address with parts. The map keeps
the pre-existing default (MapLibre prisms + detector meshes); the per-plane
roofline surfaces are opt-in behind Map Enhancements -> "Rooflines
(experimental)" (`RealRoofLayer.setRooflines`). Do NOT make them the map
default and do NOT hide the MapLibre fill-extrusions: hiding the prisms exposes
the raw detector meshes (blobby rings, dark slate palette) and the map reads as
worse "bounding boxes". Also do not pass `maxSections: 1` on the map path -
the fitted-rectangle fallback draws literal oversized boxes over buildings.

## Studio alignment rules (`Deep3DStudio.tsx`, `deep-3d-model.ts`)

- One scale for the whole scene: `k = 32 / placement.metersPerWidth` (z18 tile
  width in metres) is used for the heightfield translation, the heightfield
  displacement, and the roof mesh (`sceneScaleFor`). The vertical gain is
  `k * depthScale * 1.5` for both; moving the relief slider rebuilds both.
- The heightfield is carved flat (0.12 m) inside the footprint: rendering both
  the displaced roof profile and the roof mesh at the same heights caused
  z-fighting that tore the mesh into sawtooth fragments. The mesh is the
  reconstruction; `peakRoofH` still reports `ridgeH` for the metrics panel.
- Footprint resolution: map selection first, then the studio's own nearest
  address-record lookup (45 m), then an explicit no-footprint notice. Without
  this, unresolved locations silently showed the synthetic parcel (area metric
  288 = 16x18, ridge metric = canopy max) with no mesh — the tell-tale sign.
- `runIdRef` guards async model runs; without it a stale synthetic run can
  complete last and clear the real building's mesh (`rebuildBuildingMesh(null)`).
  `[Deep3D] roof mesh: N wall tris, M roof tris` is logged on every rebuild.
- `generateDeep3DHeightmap` receives footprint UV (tile frame); it centres the
  ring on its own centroid and subtracts the same ring centre from each query
  point. If you change one frame, change the other or the bump stamps at the
  tile centre instead of the building.
- Camera framing targets the building at scene origin (plane translated by
  `-offsetEast * k`, `+offsetNorth * k`); keep `handleResetCamera` in sync.

Coordinate checks that settle 3D/2D alignment questions fast:
- Decoded MVT local coords are y-down and feed `tile_local_to_lonlat` directly.
- `ImageryFetcher.window(...).local_to_pixel()` is the one conversion from
  pipeline/MVT local space to preview pixels — draw in pixel space once (a
  second conversion compresses overlays towards the top-left corner).
- Validate overlays with G-NAF address dots (address points are property
  access points, not roof centres, but they must sit along street frontages).
- The archive + address association round-trips correctly: a mirrored/off
  conversion shows up as multi-hundred-metre match distances, not 0–10 m.

## Per-address algorithm (`scripts/ml/addresses.py`)

1. Decode the MBTiles `building` layer to WGS84, **dedupe tile-buffer copies by
   rounded centroid** (essential — without it one building can match dozens of
   addresses).
2. Match each G-NAF address to the containing/nearest footprint (default 12 m).
3. **Unit inheritance**: unmatched unit addresses inherit their street-number
   sibling's ring within 30 m (`inherit_unit_rings`; measured 58 rescues on the
   demo bbox, e.g. Unit 1/29 Cambridge). Clones carry an informational
   `inherited-ring` flag (no penalty) and the sibling gains them as neighbours.
4. Count **distinct dwelling points** per footprint (base+unit G-NAF records
   share a point and must not inflate counts).
4. Classify kind via exact premises categories (opt-in `--places`, the bundled
   feed has mobile services at home addresses) + geometry: `house`, `units`,
   `business`, `school`, `industrial`, `civic`, `large`.
5. Emit JSONL/JSON: roof ring metres relative to building centroid, eave/ridge,
   type, angle, area, address point, neighbours.

## 3D rendering

- `src/lib/roof-geometry.ts` — shared `buildRoofSections()`: convex decomposition
  of the roof footprint + hipped/gabled/flat tents, positions in metres
  (x=east, y=north, z=up). Used by both the map layer and the studio.
- `src/lib/real-roof-layer.ts` — MapLibre custom layer (`real-roof-3d`), walls +
  coloured roof sections from live tile features. Attach only when
  `map.isStyleLoaded()`; retry on `idle` (style not done loading is a real
  failure mode).
- `src/components/Deep3DStudio.tsx` — resolves the selected address against
  `public/address-roofs.json` (`src/lib/address-roofs.ts`), falls back to tile
  features, renders the real roof mesh on top of a satellite-displaced
  heightfield, metrics from the address record.
- `src/lib/deep-3d-model.ts` — heightfield: real footprint drives the roof
  planes/positive slopes; outside the footprint only dense/dark vegetation
  rises (lawns stay low) so the roof silhouette stays legible.

## Critical gotchas

- **MapLibre worker**: the Next production bundle resolves the worker URL to
  `""`, so no vector/GeoJSON source ever loads (only rasters show). The build
  copies `maplibre-gl-worker.mjs` + `maplibre-gl-shared.mjs` to `public/maplibre`
  (`scripts/copy-maplibre-worker.mjs`, wired into `pnpm dev/build`) and
  `MapComponent` calls `setWorkerUrl("/maplibre/maplibre-gl-worker.mjs")`.
  Symptom: satellite imagery renders but no outlines/trees/roofs.
- **Custom layer projection**: use `options.defaultProjectionData.mainMatrix`
  (column-major, mercator 0..1 → clip). `modelViewProjectionMatrix` is a
  different matrix in MapLibre v6 and projects geometry off-screen.
- **Extrusion visibility**: `enable3D` must be
  `show3DBuildings && !enableLoD2Houses` so MapLibre prisms hide when the roof
  mesh layer owns buildings; `tree-3d-vector` visibility depends only on
  `enableTrees` or trees vanish when roof meshes are on.
- **eave_height units**: `roof.model_roof()` needs `meters_per_unit` (pipeline
  passes `meters_per_px`); wrong units clamp every eave to the 2.4 m minimum.
- **Stale `.next` chunks**: after rebuilding while an old `next start` holds the
  port you get 500s on hashed chunks and a dead map. Always `task stop` (not
  `stop:web`) before `task start`; if in doubt `rm -rf .next` and rebuild.
- **Killed builds empty `.next`**: `next build` was SIGKILLed (exit 137) twice on
  2026-09-29 while scene extraction and the MPS fine-tune ran beside it, and the
  launchd server then served pages whose chunks all 500ed. Rebuild with
  `task web:rebuild` (`scripts/rebuild-web.sh`): it pauses the fine-tune with
  SIGSTOP, refuses to run beside `scene_extract`, and restarts only on success
  (3 min compile on a quiet machine, 20+ min contended). Never `pkill -f "next
  build"` from a shell whose own command line contains that string. When the
  host is out of memory (Docker VMs 50-70 GB, swap full) the OS kills the
  build at ~22 GB regardless; fall back to `next dev -p 3008` under launchd
  until memory frees. `launchctl submit` needs ABSOLUTE `-o/-e` log paths:
  relative paths make every spawn exit 78 (EX_CONFIG) with nothing logged.
- The `modelOutput.metrics.peakHeightMeters` uses the roof peak when a footprint
  exists, otherwise the heightfield max.

Round-5 teacher, review and host gotchas (2026-09-30):

- **Teacher windows are frozen per source**: never run
  `eyes:teacher-sources` `select` or `select --windows-from` with
  `--source linz-chc-2025-0075`. It rewrites the finished 60-window
  `windows.json` in place, and every crop, judge and ledger row keyed on those
  windows moves with it. Growing a source means a new registry entry, never a
  re-select.
- **5 cm crop gate**: before any judge against `linz-chc-2025-005`,
  `distill-linz-chc-2025-005/teacher/crops.json` must read `gsd_m 0.05`,
  `native_window_px 5436` and four windows of shape `[5436, 5436, 3]`.
  `select_from` used to copy the 7.5 cm geometry (3624 px windows: the 5 cm
  pixels area-averaged to 7.5 cm), and `crop`'s freshness test now checks
  `shape`. A `register` on 3624 px crops runs "successfully" and means nothing.
- **Judge files overwrite by name**: back up `distill-*/teacher/judges/` with a
  `shasum -a 256` manifest before any `register`, `ladder` or `boundary` run;
  `data/reviews/` is gitignored, so git cannot restore them. Never run plain
  `boundary --cells all` beside a ladder at the same tolerance (they shared one
  filename tag; `ladder` now writes `ladder_*`). Tell ladder files apart by the
  `ladder_` prefix or their variants, never by `.judge` (both read
  `boundary`). `boundary()` mutates `J1['peak_tol_m']`: one judge per process.
- **Never write into an old `latest.json`**: `save_scene` recomputes
  `model_signature` from content, so any write stales that scene's review in
  the app, and a new image under the old id also drops its confirmed outlines
  from every export (`export_dataset.review_model` finds no model for a review
  whose snapshot image differs from the scene's). A recapture is a new
  `lot-<address_id>` scene (`task recapture:capture`, guarded by
  `CONFIRM=1`); forward pointers live under `data/reviews/eyes/recapture/`.
  The reviewer's saves write `review.json` only, so an `eyes:guard`
  `confidence_mtimes` trip means something wrote a `latest.json`: stop and list
  the scenes. Never run `scene_confidence` to clear it (it rewrites
  `confidence.json` / `confidence.sqlite`).
- **Chat class-only words**: `scripts/record-chat-feedback.mjs` never sets
  `geometry_reviewed` for a decision carrying `class_only: true` (a word about
  roof or not roof is not an outline check, so it must not mint a T1
  reference). The app refuses `confirmed` on an unreviewed outline
  (`validateSceneReview`: "Check the outline before confirming an object."), so
  `--apply` of such a confirmation fails that scene's whole save: the reviewer
  confirms that object in the editor. Never drop `class_only` to get past it.
- **Live SAM3 tile server**: the launchd job `com.geoau.addr.ml`
  (`scripts.ml.server`, keepalive; `pgrep -f scripts.ml.server`) is a live SAM3
  ONNX process generating AI tiles, and any map viewer can wake it. Never stop
  it, and start no second SAM3 process (teach, fine-tune, `reference_eval`)
  while it runs. Its neighbour prefetch used to crawl without bound (fixed
  2026-09-30: only a client-requested tile enqueues its eight neighbours, and a
  prefetched tile never fans out again). Before blaming other jobs for load,
  read its `/stats` (`queue`, `prefetched`, `generated`; the port is on its
  command line, 18090 under launchd): `curl -s 127.0.0.1:18090/stats`.
- **One CPU job at a time while swap is full**: source `.run/jobgate.sh` and
  run `jobgate <budget_mb>` immediately before any heavy job (fetch 500, crop
  1500, judges chain 1500, measure 2000, rejudge 1000, recapture pilot + plan
  1500, full pytest 2500, full `tsc` 2500, `task test:reviews` 1000), and copy
  its header line into the job's log. Full pytest gets 2500, not the round-5
  plan's 1500: the 2026-09-30 full run peaked at a 1.49 GB memory footprint
  and 3.89 GB max RSS (`/usr/bin/time -l`, `.run/w4int-pytest.log`), so 1500
  left it no headroom. Test jobgate's own exit status: `jobgate ... | tee`
  returns tee's.
- **Venvs**: `pytest` runs only under `scripts/ml/.venv`. The training venv
  (`scripts/ml/training/.venv`) has rasterio but no pytest, so rasterio imports
  stay inside functions. Never `pip install` to make a test run.
- **Assistant-written reviews are observations, never human decisions**: a
  review whose notes carry "Assistant visually traced ..." or "Assistant image
  review ..." (for example `map-d34a1fe62200fa75`) is the assistant's, and
  `updated_by: "local"` marks nothing (the UI writes it on every save). Such an
  object counts as the reviewer's only after a fresh decision whose notes start
  `[reviewer] roof` or `[reviewer] not a roof`, agree with the status and are
  saved after revision 1; its outline stays agent-traced, never human-drawn.
- **Round-5 human precision block**: it lives under
  `data/reviews/eyes/training/sam3-mac/round5/` only and reads a share only at
  N >= 30 decisions with >= 8 per class across >= 6 scenes (below that it
  prints `n/a` and the per-object table). Every count comes from
  `task eyes:small-roofs` at the moment it is used, never typed. Its figures
  are agreement between model estimates and the reviewer's decisions, never
  accuracy, and nothing is promoted on any outcome.

## Verification workflow

- `task verify:ui` — tiles requested, no page errors, map + studio screenshots.
- For a specific address: open the app (default centre is 27 Cambridge Road,
  Mooroolbark), click Satellite, then the "LoD2 3D" control, and compare the
  studio mesh with the satellite imagery.
- Check props quickly: `curl -s --compressed localhost:8085/tiles/16/x/y.pbf`
  and decode with `mapbox_vector_tile` (see `scripts/ml/tests`).
- Data sanity: `scripts/ml/.venv/bin/python -m scripts.ml.addresses ...` prints
  matched counts and kind distribution; `--format json` writes the web asset.

## File map

- `scripts/ml/pipeline.py` — tile orchestration, SAM3 refinement, roof model.
- `scripts/ml/addresses.py` — per-address extraction (algorithm above).
- `scripts/ml/roof.py` — `model_roof` (type/eave/ridge/angle), SAR fit.
- `scripts/ml/server.py` — realtime sidecar (LRU, prefetch, `--sam3` opt-in).
- `scripts/ml/rooflines.py` — TU Delft kinetic-partition port (rooflines + per-plane LoD2).
- `scripts/map-shot.mjs` — headless map screenshots (`--zoom`, `--pitch`); `scripts/studio-shot.mjs` — studio render.
- `src/components/MapComponent.tsx` — map init, layer lifecycle, building lookup.
- `src/lib/google-maps-style.ts` — 2D/3D layer styling and visibility flags.
- `data/australia_tiles.mbtiles` — pre-built demo archive; `data/australia_geo.db`
  — G-NAF addresses + places SQLite.
- `public/address-roofs.json` — generated per-address records (gitignored).

## Roof annotation and training workspace

The `roof_training_250` cohort holds 250 distinct roofs and spatial train,
validation and test groups. `/reviews?cohort=roof_training_250` compares model
predictions with separately saved editable ridge/apex graphs and object masks.
`RoofAnnotationEditor` supports ridge movement, heights, undo/redo and solar,
skylight and chimney polygons. Saving keeps model/boundary signatures, server
geometry audits and revision history. Class-specific object review coverage
prevents unreviewed classes from becoming false negatives.

`task roof:objects` creates SAM3 solar/skylight candidates; `task training:prepare`
exports immutable image/annotation versions and fits a regularized roof-end
classifier only when reviewed labels satisfy spatial split minimums. Dense neural
weights are not changed by that command. See `docs/roof-training.md` for gates.
`task verify:reviews WEB_PORT=3008` checks review UI and isolated API persistence.

`roof_form.py` compares hip/gable hypotheses using cross-line colour/brightness
support. Uniform colour remains ambiguous. Explicit `roof_form_source: reviewer`
can select a gable without pretending the imagery proved it. Complex gable
conversion preserves the captured outline and the physical geometry audit;
`buildWallStrip` fills vertical triangular end walls up to the roof surface.

`Correct outlines → Trace replacement` supports manually redrawing up to 64
same-class detector fragments as one review draft. It preserves source lineage;
exact split/merge still use at most eight outlines and preserve their coverage.
Trace targets must overlap every source. Approvals and extent-dependent 3D
settings reset; gaps need a fresh check. Verify with `task verify:scene-trace`.
Unapproved traces, such as the three solar banks in scene 25801 revision 3,
remain out of training targets. See `docs/scene-review.md`.

## Unified house review on the main map

The Processing workspace now has a bounded **Self-review** tab at
`/scenes/processing?view=self-review`. `task scene:review-loop CLI_ARGS="--start --limit 12"`
produces isolated candidate comparisons under `data/roof-review-loop/runs`.
It never publishes to scene models/reviews, changes the frozen suburb worker,
or approves generated labels. Use `/api/scenes/self-review` for progress and
`?run=ID&object=INDEX` for frozen comparisons. The older `scene:self-review --repair`
can publish repairs: do not confuse it with this candidate-only loop, especially
while the full-suburb run is frozen. See `docs/roof-review-loop.md` for gates,
resume behavior and imagery/accuracy limitations.

`HouseReviewLink` and `/api/scenes/house` open an existing address-linked scene or
capture `house-<address_id>` on demand. `scene_extract --house-id ID --roof-models`
uses the existing extraction lock, immutable imagery and audited roof proposal
path. Re-analysis from `/scenes` keeps review history and requires the existing
recovery flow when predictions change. Never directly edit real review files.

`/api/scenes/map` / `scene_map.py` supply georeferenced saved/model objects,
scanned-class coverage and display-only overlap suppression. `SceneMapOverlay`
restores base layers when hidden; `SceneMapLayer` uses `populateScene` and
`defaultProjectionData.mainMatrix`. The optional `objectIds` mask suppresses
meshes while preserving all parent objects for solar roof support. Tests include
`test_scene_map.py`, `test-scene-geometry.mjs` and the required map smoke test.

## Continuous roof surface proposals

`roof_surface.py` is a conservative post-segmentation prior, not trained weights.
Only building proposals use it: small irregular holes may fill when their local
colour matches the surrounding roof, while substantial, regular or contrasting
openings remain. Exterior simplification is bounded by 25 cm / three pixels and
2% area difference. The proposal retains `boundary_refinement.raw_ring`,
`raw_holes` and per-hole decisions; IDs derive from the raw detector geometry.
Do not apply this prior to saved feedback or grant review/visibility approval.
Roof candidates are ordered largest first for suppression; sub-square-metre
building speckles are excluded without filtering small solar/skylight objects.
Near-contained roof sections (>95%) are not separate building instances. Keep
adjacent roofs and real courtyard buildings; test both before changing NMS.

`scene_roof.py` fits broad planes to a bounded support envelope and clips them
back to the exact captured outline. Physical coverage, planarity and seam audits
still apply; holes bypass envelope smoothing. `scene-roof-lines.ts` displays the
current annotation graph (or legacy model lines) in the aerial review, with
separate ridge/hip/valley colours. It must respect Prediction/Your edits and
never turn estimated linework into checked training labels.

`roof_line_fit.py` refines automatic scene roof proposals against registered
original/CLAHE line segments. Original RGB gates material/vegetation evidence;
repeated texture families are suppressed. Joint vertex/plane fitting locks
outline corner XY and Z, allows gable points to slide only along their eaves,
and uses positive projected triangles to prevent inversions. Exact coverage,
planarity, seams, drainage and pitch bounds gate acceptance. Weak or unstable
fits retain the prior. This updates inference, not trained weights or observed
roof-line labels. Matching residuals are not measured accuracy.

`/api/scenes/image?…&view=roof-detail-v1` is a hash-verified, cached photometric
view at the original dimensions. **Enhance roof detail** switches aerial and
apex-editor imagery together without resampling coordinates or saving labels.
The original PNG remains immutable. See `docs/roof-line-fitting.md` and the
enhancement checks in `task verify:scene-roof`.

`roof_topology.py` now compares simpler construction-grid support hypotheses for
**automatic** scene roof proposals. Image fitting and hip/gable selection happen
before clipping back to the exact captured outline; clipped boundary heights may
vary and are not measured eaves. Courtyards bypass support simplification. Both
candidate and original line evidence are scored, with a veto for substantially
worse image matching. No outline or training approval is implied.

`roof_physics.py` adds sampled gravity/runoff tracing to `audit_roof`, including
face transitions and valley flow. Level internal channels can fail even if their
end nodes lie on the footprint boundary. Flat roofs and >64-face roofs explicitly
report runoff as unassessed. This is not load, gutter-capacity or building-code
validation. Reports and thresholds: `docs/roof-physics.md`.

**Edit roof lines** now saves independent XY ridge/hip/valley observations in
`SceneObject.roof_line_edits`. It supports drawing, deleting, shared junctions,
point/whole-line movement, type changes, undo/redo and baseline comparison. This
is not a replacement 3D mesh or automatic training approval. Source outlines
bind the observations; stale outlines must not render/apply them as current.
Split/merge clears them; same-image/frame recovery preserves them. The canonical
scene save handles permissions, revision checks and history.

Fetch saved feedback through `/api/scenes/roof-feedback?id=ID&object=OBJECT_ID`,
`python -m scripts.ml.scene_line_feedback --scene ID --object OBJECT_ID`, or the
project MCP `roof_line_feedback` tool (client reconnect after adding tools).
Responses separate saved line constraints, starting estimates, original model
and 3D estimates, with frame/image/revision provenance. Never read an unsaved
browser draft as persisted feedback. See `docs/roof-line-feedback.md`.

`roof-topology-v2` handles pixel-stepped eaves and small openings without making
each contour segment a pitched face. Openings remain exact cuts in coplanar
surface pieces; shared coplanar seams are not roof lines. Visible-image bounds
handle artificial crop edges without assuming physical hips. Scene auto mode
may use explicitly uncertain `simple-gable-prior` ends on elongated wings;
confident image/classifier hip evidence and reviewer choices still win. Final
pitched scene proposals require assessed/passing runoff, not a complexity skip.
See `docs/roof-physics.md` and the clipped stepped-roof regression fixture.

Map-area and house extraction APIs now pass `--detector-only`: source imagery
→ SAM3 → shared boundary fitter → audited roof inference, with no imported CRM
geometry. `scene_extract.analyse_scene` is the shared fresh-prediction path;
reviews remain separate. ML map tiles also run `refine_roof_surface`. Prediction
metadata identifies `sam3-scene-roof-v4` and hashes `roof_structure.py` as well
as the boundary/end/topology components. Reproduce a scene with
`python -m scripts.ml.scene_extract --scene-id ID --roof-models --detector-only`.
A new model snapshot makes prior reviews stale; the canonical recovery path
carries feedback without changing the new model's geometry. Show **Prediction**
when demonstrating model improvements, not **Your edits**. Never call inferred
geometry a learned weight update. Current classifier candidate/readiness:
`docs/roof-training.md`.

### Improvement round 1 additions (26 September 2026)

- Flat hypothesis in auto mode: `roof_form.flat_evidence` (`roof-form-flat-v2`, conservative gate sets, ridge veto, 0.5 m equipment-blob halo). `propose_roof(context_hint=)` and `scene_house.add_roof_estimates(context_hints=)` carry the building-class hint (`scene_object_quality.class_hints_for_scene`, cached-only context via `scene_house.pipeline_context_hints`, never raises) into the evidence; `FLAT_RULES['context']['waived_gates']` is empty on purpose (see docs/roof-eyes.md, round 1). Pass `--no-context-hints` to `roof_recompute_eval` for the paired control.
- Topology support stage `roof_topology._image_supported_simplification` (`roof-topology-support-v1`): replaces the v5 selection only with a strictly simpler, at least as image-supported candidate; `report['support']` records candidates and gates.
- Class hints `building-flags-v2` (`task eyes:building-flags`, `eyes:building-flags:captures`): secondary-structure ratio 2x on the same Vicmap property, 3x through the 25 m proxy; `/queues` reads `HINTS_VERSION` in `src/lib/queues.ts`, so bump both together and rebuild the web app.
- `task eyes:units` (per-lot unit-split candidates), `task verify:scene-parcels`, `task verify:scene-imagery`, `task eyes:roof-eval` (flagged set vs frozen baseline), `task eyes:line-corrections` (reviewer roof-line corrections vs the model estimate; `--dry-run` via the module previews without writing), `task verify:scene-roof-lines`.
- Web relaunch under launchd must set the working directory: `launchctl submit -l com.geoau.addr.web -o .run/web.log -e .run/web.log -- /bin/sh -c 'cd <repo> && exec node node_modules/next/dist/bin/next start -p 3008'`; without the `cd`, `next start` reports "Could not find a production build". Run ruff from `scripts/ml` (`cd scripts/ml && .venv/bin/python -m ruff check .`).
- Structure sub-kinds (`scripts/ml/structure_kinds.py`, `structure-kinds-v1`, `task eyes:structure-kinds`, optional `--sam3`): shipping-container, shed, garage, carport, pergola-or-shade, water-tank, fragment, outbuilding, not-secondary; rows in object_quality; surfaced by `/api/scenes/hints`, the editor's "Agent hints" line and /queues badges (`STRUCTURE_VERSION` in src/lib/queues.ts). `pipeline_context_hints` passes flat-topped sub-kinds (`FLAT_RULES['context']['flat_hints']`) to `propose_roof`. SAM3 prompt scores at 4 px/m do not separate sub-kinds; the negative "no house proposal" signal only raises confidence.
- Measured and rejected (26 Sep): CLAHE-enhanced frame as the SAM3 detector input (`scripts/ml/clahe_detector_experiment.py`; jittered outlines, lower scores, roofs typed as solar arrays, worse 27 Cambridge agreement). Do not re-run without a new hypothesis; the next candidate is edge images for boundary refinement only.
- Round 3 (26 Sep): `task eyes:address-allocation` (address-allocation-v1 rows + data/reviews/eyes/address-allocation/summary.json; rule chain saved link, Vicmap property + G-NAF, numbered property containment, secondary inherits primary, unit lots, address point; unnumbered site polygons and common-property lots never vouch) then `task eyes:address-enrichment` (per-address JSON under data/reviews/eyes/address-enrichment/addresses/<gnaf id>.json, served by `/api/address/enrichment?address=`, shown in the map sidebar as "Property structures"; `task verify:address-enrichment`). Run structure kinds over capture scenes too (`python -m scripts.ml.structure_kinds --scenes $(ls data/scenes | grep -v mooroolbark-50011498-17-)`) BEFORE allocation, because enrichment reads the sub-kinds from the allocation rows. Calibration plans are `reference-calibration-v2` (min 6 m²; the hub picks the newest plan folder). Rejected by measurement: enhanced/edge frames for outline refinement (`edge_boundary_experiment.py`; the production refiner reads no pixels for exteriors).
- Round 4 (26 Sep): `task eyes:decision-policy` (machine labels + auto/assist/review tiers in table decision_policy; `--reflect` writes REFLECTION.md and proposals only; `--apply-auto` is dry-run by default and live writes review.json only with the explicit flag and the user's go). `task eyes:optical-layers` builds hash-bound layers; `roof_line_fit.image_segments` accepts `layer:`-prefixed evidence views (v8 gate). Measured and NOT adopted as pipeline evidence (see docs/roof-eyes.md round 4). Browser verify scripts are unreliable above load 50; verify-machine-labels has no screenshots for that reason.
- Round 5 (27 Sep): machine labelling is LIVE for the auto tier (`decision_policy --apply-auto --i-understand-this-writes-review-json`; backups, audit and `--rollback-auto` under data/reviews/eyes/decision-policy/). Protected reference captures (house-30286, house-30296, T1/T2 scenes) must not receive machine writes; the guard catches it. Machine provenance (`provenance.source` = machine:*) is excluded by `reference_registry.review_tier` (T3) and `reference_calibration.classify` (not counted); keep any new consumer of review.json statuses consistent with that. Labels carry `interval` (Wilson human agreement per tier/kind) and `score_interval` (design range); /queues "Machine labelled" table (`/api/queues/machine-labelled`) is the review surface.

- `next build` file tracing: the repo root holds tens of GB under data/ and Python venvs under scripts/; without the
  `outputFileTracingExcludes` in next.config.ts the tracer balloons past 90 GB and the build is OOM-killed (seen
  2026-09-29). Keep the excludes when adding large folders. Build with `NODE_OPTIONS=--max-old-space-size=6144`.
- Review-guide links must not carry `layer=model`: the model layer disables every decision control in the scene editor.
- Fieldwork in both directions: `task pull` imports Fieldwork edits as drafts (`docs/annotation/feedback.md`); `task push -- --dry-run` / `task push` publish changed local scene frames and review state as NEW immutable items (never overrides, never `DeleteItem`; live origin needs `FIELDWORK_PUSH_ALLOW_LIVE=1`); `task training:publish|manifests|pull` move content-addressed training bundles through the `addr-training` project. Contract, actions table, receipts and the MCP equivalents on both servers: `docs/annotation/local-to-fieldwork.md`. Local MCP tools: `plan_fieldwork_push`, `apply_fieldwork_push`, `fieldwork_push_status`, `publish_training_artefacts`, `list_training_manifests`, `pull_training_artefacts` (client reconnect after adding tools). The training engine (`scripts/ml/training/{publish,pull}_artefacts.py`, `docs/annotation/training-artefacts.md`) and the push engine (`scripts/ml/fieldwork_push.py`, `internal/annotation/push`, `annotate push`; `docs/annotation/push.md`) are in the tree and verified against a local dev server; the pull side does not read push receipts yet (generation items are quarantined by today's feedback packet).

### Round 5 morning additions (1 October 2026)

- `ImageryFetcher.window` (`scripts/ml/imagery.py`) pastes each child tile at `int(round(tx*CHILD_TILE_PX - left))` while its georeferencing (`left = x*base_px - margin_img`, `local_to_pixel`) stays continuous. Whenever `margin_px * base_px / out_px` is not an integer, every pixel of the window sits a constant fraction of a native pixel off the ground its affine assigns: the cohort extractor defaults (zoom 18, imagery_offset 1, out_px 576, margin_px 256) give margin_img 227.5556, so every `roof_training_250-*` fitted frame is +0.4444 native z19 px (about 0.105 m) east and south; suburb windows (margin_img 64.0) and `region_frame` captures are exact. Check the margin before trusting `ImageryWindow.pixel_to_local` or `rooflines.Frame.pixel_to_metre`. Diagnosis, not corrected (reviewer decision): `data/reviews/eyes/recapture/offset-diagnosis-2026-10-01.md`.
- `review_authority.carried(obj)` / `carried_undecided(obj)`: a recaptured object with any non-empty `carried_from` and status `needs_adjustment` is the reviewer's TARGET, never a human tier (`review_tier` gives T3 `review:carried_undecided`; `reference_calibration` does not count it; the small-roof counter classes it `undecided`). It is deliberately not a `review_authority_blocker`, because a blocker means excluded. A carried object the reviewer confirms, rejects or supersedes on the new image is the reviewer's own decision again.
- The teacher ledger selects fold instances in `distill_dataset._policy_ignored` (hard-coded `judges/ledger_rows.jsonl`, key `recommendation`, drops only `ignore`); `prepare_coco` never reads a ledger and only floors (`--min-area-px-for`) and caps. `recommendation_v2` needs a new `POLICIES` / `POLICY_SUFFIX` entry plus a fresh `squared_gated` export (a host job). With the 216 px (12 m2) floor kept, the candidate policy changes the SAM3 fold by zero instances: the floor is the lever (`.run/round5-readiness-2026-10-01.md`).
- `/api/review-progress` spawns the counter (about 5 s and 700 MB); under host load above 40 it takes 8 to 19 s and the route kills it (`counter failed (SIGTERM)`, a 500 that clears itself). The scene editor is unaffected. Read counts with `task eyes:small-roofs`. Live `review.json` reads can fail with ENFILE (errno 23, the system file table) while other projects' VMs are busy: retry before blaming the code.
- Other sessions' MCP tooling (`scripts/ml/mcp_server.py`, provenance `machine:mcp-polygon-draft-v1`) saves agent drafts into live `review.json` through the app. A `find -newer` on `review.json` is therefore not evidence of a reviewer save: read `provenance.source`. The recapture wiring's quiet-window gate must see that stream idle too.
- The main Python venv is `scripts/ml/.venv` (there is no `.venv` at the repo root); `scripts/ml/training/.venv` and `scripts/ml/training/.venv-sam3` are the training venvs.
- Most of `scripts/ml/*.py`, all of `scripts/ml/tests` and the `docs/*.md` handovers are untracked in git, so a "rest of the file unchanged" claim rests on reading or on a saved copy, never on `git diff`.
- Focus recapture for frame-cut targets (1 Oct 2026): `scripts/ml/scene_focus.py` (`task focus:plan TARGETS=... DATE=...`, `task focus:capture ... CONFIRM=1`) plans one z20 window per house/lot from a `{scene_id: [object ids]}` list and carries the frozen MODEL object into the window as `needs_adjustment` + `carried_from`; it refuses to plan or capture without `data/reviews/eyes/recapture/WINDOW.json` (pre-registered rule; exclusive create via `register`). The small-roof counter follows `carried_from` to the frozen key only when `--recapture-dir` holds a registry row with `frame_transform_verified` true and the current DESIGN sha (`carry_unregistered` otherwise); one decision per frozen key, source wins, two copies = `ambiguous_carry`. Parcel pitfall: an unnumbered parcel whose plan starts CP/CM is a cluster plan-block (CP153157 is 311 m wide), never a lot. Subject-centred windows often predict `overlap.fraction` below the pre-registered 0.6 (the fraction is SOURCE pixels inside the TARGET frame), so a far-centred window is refused at capture until the reviewer settles the floor.
- go-task 3.53.1 echoes `task: [name] cmd` to STDERR (and `Taskfile.yml` sets `silent: true` on `default`), so `task eyes:small-roofs > file.json` is valid JSON; the direct `scripts/ml/.venv/bin/python -m scripts.ml.training.sam3_mac.r5_precision count --json --block v2` is equivalent.
- `.run/jobgate.sh` closes with "a job is running" when its pgrep pattern matches another project's GitHub-runner build under `~/.cuttlefish/` (`next build`, `tsc --noEmit`); read the printed process before treating a closed gate as memory pressure.
- Any carry or import that writes `review.json` objects with `group_id` (or `parent_id`) must also write the matching `groups` records (and carried parents); `validateSceneReview` in `src/lib/scene-store.ts` rejects the save with "Choose an existing object group." otherwise, and the editor shows the failure only as a draft kept. Frozen model objects carry singleton groups named after themselves. DESIGN.json shas recorded in transforms and registry rows are accepted through `scripts/ml/design_chain.py` (current sha or an ancestor behind `thresholds_changed: false` amendments).
- SAM3 extraction on a 512 or 1024 px window (`scene_extract --scene-id <window>`) peaks at 7.6 to 10.6 GB resident and takes 14 to 92 s; budget `jobgate 11500` and run one at a time. `save_scene` keeps every top-level key except objects/groups/scans/analysis/model_signature/property_roofs, so `frame_transform` and `window_frame` survive re-extraction while the review turns stale (identical image: the editor's "Keep earlier objects" recovery applies). Three of 53 runs crashed at process teardown after a complete save (exit 134/139); check `models/` and `latest.json` before re-running. `.run/focus-recapture/run-detect-d.sh` needs zsh (`$pipestatus`).
- Window reviews (lot-*/win-*): the editor's save validator (`src/lib/scene-instances.ts` lineageIssue) accepts `status: superseded` only with a non-empty `replacement_ids`, and `replacement_ids` only when every named object points back through `derived_from`; any script that supersedes an object must write the reciprocal or the review becomes unsaveable in the app. The Fieldwork pusher keys generation items on model signature + review revision, so a revision-1 content edit (carry-forward, takeover) is reported `current` and never republished; publish by revision bump or a content-hash key, not by re-running push.
