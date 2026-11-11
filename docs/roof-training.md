# Roof and object review / training loop

The `roof_training_250` cohort contains 250 distinct captured roofs. It is a
review queue with model suggestions, not 250 verified training labels. Images,
predicted outlines, plane graphs and object candidates remain distinct from
reviewer corrections. Explicit roof-family corrections can supervise that target
without pretending the apex height was measured.

## Review

Open `/reviews?cohort=roof_training_250`. Use **Model prediction**, **Your edits**
or **Overlay both**, then **Edit apexes & objects**. Ridge points support dragging
and arrow keys. The height field changes a connected horizontal ridge together.
Footprint corners stay fixed; use Address CRM for outline changes. Boundary
ridge points slide along the boundary. Changing a roof-family label alone does
not rewrite the mesh: it is separate classifier feedback.

Use undo/redo while editing. Save review persists the graph, object polygons,
verification flags, reviewer identity, model signature and revision. Previous
saved versions can be restored as drafts. Rebuilt model signatures invalidate
old geometry annotations instead of silently applying them to a different mesh.
A server-side Shapely audit checks planarity, coverage, overlap, seam heights and
apexes before shape approval. Invalid drafts may be saved as correction feedback.

Solar-array and skylight proposals come from SAM3 on existing imagery, with one
image encoding per tile. They are clipped/assigned to a roof only with at least
80% overlap. Confidence is a detector score, not proof. Confirm, reject, or edit
the polygon; draw missed panels, skylights and chimneys. A completed object review
also supplies an explicit negative when no objects are present. Unscanned roofs
are never labelled negative automatically. Object verification is independent
of an unresolved roof apex.

## Reproducible commands

- `task training:cohort` selects 250 roofs by roof type, size and concavity,
  preferring existing CRM examples and grouping duplicate address points.
- `task roof:cohort CLI_ARGS="--cohort roof_training_250"` creates source imagery,
  model faces, rooflines, audits and review contact sheets.
- `task roof:objects` detects solar arrays and skylights across that cohort;
  completed tiles are resumable, failures remain marked as failures.
- `task training:prepare` creates an immutable dataset version and a training
  readiness/evaluation report. The **Export labels & evaluate retraining** button
  performs the same workflow with CRM authorization.

Exports live in `data/training/<cohort>/<content-hash>/`. Image bytes are copied
to a content-addressed image store. JSONL includes the predicted mesh, edited
vertex/face graph, roof family, object polygons, rejected detections, provenance,
image-to-metre affine transform and review revision. COCO instance annotations
contain only confirmed objects from completed object reviews. Verified negative
images remain in COCO with no positive annotation. Boundary, roof-shape and
object eligibility are separate; relative edited heights never become surveyed
height labels.

## Training

For scene reviews, run:

```sh
task scene:training
task scene:train-roofs CLI_ARGS="--dataset data/training/scenes/<export-version>"
```

`train_scene_roofs` consumes schema-6 `roof-feedback.jsonl`, verifies it against
the content-addressed full snapshot and rechecks the saved family/outline
observations. The immutable snapshot supplies labels; current mutable reviews
are never silently substituted. Image hashes, dimensions, the whole image's
split geography, group membership and duplicate views are checked independently.
Overlapping views of one physical roof count once; conflicting families in such
a cluster are withheld. The higher-resolution view is retained, with identity
breaking ties independently of prediction scores.

Features are hip/gable colour-line support from the shared `roof_end_evidence`
function, scored against one unchanged hipped prior for every end. Inference
uses the same comparison before applying any selected conversions. No trained
choices, reference linework, manual heights or template roof types become
features. The edited outline is an input: this classifier is conditional on a
captured outline and does not learn the boundary itself. Explicit pure hip/gable
families supervise ends; flat/mixed or unchecked roofs remain excluded. Ends
below the inference visibility threshold of 0.2 supply no fitting example.

The scene path requires, per class, ten usable training roofs across three area
groups, plus three validation and three test roofs, each across two areas.
Weights balance classes, areas within each class, roofs within an area, and ends
within a roof. Train data alone sets normalization and model coefficients;
validation selects regularization from fixed candidates. Test pixels are opened
only after model selection, then evaluated with the frozen coefficients. Test
outcomes do not select weights or regularization. Insufficient visible test
examples leave a fitted candidate unvalidated, without an invented metric.

Runs live in `data/training/scene-roofs/<run-id>/`. Their identity includes the
input snapshot, code, library versions and fixed training settings. Reports link
to exact review objects and distinguish reviewed roofs from usable evidence.
Publication is atomic; repeated runs verify existing artifacts before reuse.
Damaged inputs or outputs fail without overwriting the original results. When
labels are insufficient, the run writes a readiness report and feature audit,
without a model. A candidate can be explicitly evaluated via `ROOF_FORM_MODEL`;
this command never promotes it. These minimums support a local experiment, not
a claim of broad accuracy. Neural segmentation/depth weights and XY apex
regression are outside this classifier's scope.

On scene export `394c5f1bb50f4a08`, the eight reviewed hip roofs yielded five
usable roofs in three areas. Three had weak image cues; no gable or holdout
family targets were present. The real run therefore produced no model and did
not open test imagery. The separately exported XY linework remains available
for a future geometry learner.

The older address-cohort path remains available:

`train_roof_form` fits a regularized logistic roof-end classifier from image
support for competing hip and gable lines. It learns from verified family labels,
not its own predictions. Hyperparameters use the validation split only; test
results are reported separately. Minimum unique-roof counts per class are ten
training, three validation and three test roofs. Multiple ends of one building
do not count as independent roofs for these gates. Insufficient labels create a
readiness report and no model artifact.

The 250-roof split is grouped by zoom-17 cells, so each original zoom-18 image
tile and its shared address aliases remain in one split. These local suburban
holdouts are not evidence of generalization to other towns or imagery providers.

Validated candidate weights are saved beside the immutable dataset. Set
`ROOF_FORM_MODEL` to that `roof-form.json` when evaluating a new cohort; the
estimator ignores unvalidated, malformed or incompatible weights. A learned
selection still needs visible image evidence and still passes the independent
geometry audit. Reviewer roof-form instructions take precedence. No new weights
are promoted when labels are insufficient. The graph and COCO exports also
provide supervision for later dense boundary, apex and panel-model fine-tuning;
the existing SegFormer/depth/SAM3 neural weights are unchanged by this trainer.

Object review coverage is explicit per class. Checking solar arrays does not
assert that no skylights or chimneys exist. `<class>-coco.json` contains only
images fully reviewed for that class; the combined `objects-coco.json` requires
all three classes to be reviewed. Rejected proposals stay in the JSONL for hard
negative mining. Edits clear coverage until the image is checked again.

Training gives each roof equal weight within its class so complex roofs with
many ends do not dominate the fit. Promotion requires validation accuracy of at
least 75%, no accuracy regression against the frozen image-evidence baseline,
and improved validation log loss. Test roofs never select hyperparameters.

A current boundary explicitly checked against imagery can enter the boundary
training target while ridge/plane issues remain unresolved. Boundary and wrong-
building flags block it. Full shape supervision still requires approval and a
passing independent geometry audit. This preserves useful existing CRM outline
corrections without treating the associated estimated mesh as ground truth.

## Reviewed expansion (23 September 2026)

Scene export `cf83b756a5a69abc` contains 21 explicit roof-family observations
(15 hips, six gables) and five checked image-XY graphs. Ten additional outlines
were traced and saved through the revision-checked scene API: three gables and
five hips in training, plus two hips in separate validation areas. Family-only
reviews retain unresolved shape and height flags. The 250-roof cohort is still
a review queue, not 250 verified labels.

Run `9031ad636371d19a` finds usable image cues for eight training hips in four
areas, five training gables in three areas, and one validation hip. Seven of the
21 family observations have insufficient visible end evidence. The remaining
requirements are two training hips, five training gables, two validation hips
(including another area), three validation gables in two areas, and three roofs
of each class in two test areas. No weights were fitted or deployed.

Four new captures contain 154 unreviewed building proposals in two validation
and two test areas. Their entire image frames pass the geographic split audit.
They remain review candidates; they contribute no labels or negative examples.
All six class COCO files are unchanged because no whole-image class review was
completed in this expansion. Source hashes, original/edited outlines, review
revisions and capture provenance are recorded in
`data/references/scene-roof-balanced-review-2026-09-23.json`; its adjacent PNG
shows the checked boundaries.

## Held-out roof observations (23 September 2026)

Export `fc2781c8933d4443` contains 243 scenes, 255 confirmed objects, 27 checked
roof-family observations (18 hips, nine gables) and seven checked XY graphs.
Six newly traced test roofs cover both families in two geographic areas. Two
include explicit ridge/hip graphs; the other four retain unresolved 3D-shape
flags. All six retain unresolved height flags. Their entire image frames pass
the geographic split audit. These annotations are image observations, with
approximately 0.4 m annotator-estimated tracing uncertainty, not survey truth.

Run `1eb3f2f32b019c1f` reports `needs-reviewed-labels`. The six test observations
satisfy the metadata-count gate, but test features and evaluation have not run:
training and validation still need sufficient usable image evidence first.
Remaining requirements are two training hips, five training gables, two
validation hips including another area, and three validation gables in two
areas. Test images must not be used to tune the feature extractor or choose
weights. No fitted model was produced or deployed.

The five new scene reviews were saved through revision-checked API writes. All
327 pre-existing model/review files and 82 unrelated objects were preserved.
All six class COCO files are byte-identical to the previous export; an
object-specific roof check does not complete its whole building layer.
Before/after annotations, capture provenance, independent geometry checks and
training readiness are recorded in
`data/references/scene-roof-holdouts-2026-09-23.json`; the adjacent PNG shows all
six traced outlines and the two checked graphs.

## Roof and solar expansion (23 September 2026)

Export `22b6bc7fed4248ce` contains 255 scenes, 266 confirmed objects, 36 checked
roof-family observations (22 hips, 14 gables) and nine checked XY graphs. This
pass adds nine roof outlines/families, two gable ridge graphs and two manually
traced solar banks. Twelve additional captures contain 227 building proposals;
unreviewed proposals do not count as labels. Ambiguous mixed roofs, lean-tos and
tree-occluded ends remain unreviewed.

Run `1997985dce25c6d5` reports `needs-reviewed-labels`. Usable training evidence
now covers ten hips in five areas and seven gables in four areas. Validation has
three hips in three areas and two gables in one area. Three more usable training
gables and one validation gable in another area are needed before fitting.
Eight family observations have insufficient visible image cues. The existing
test metadata gate passes, but test features/evaluation remain unopened. No
weights were fitted or deployed.

The complete solar scan adds one training image and two masks (15 images,
14 masks, ten areas total). Its original extraction scanned buildings only, so
baseline evaluation correctly skips solar with `not_scanned`. This is useful
manual supervision, not evidence of missed detector predictions. The other
five class COCO files are byte-identical to the previous export. Building
layers remain incomplete. All 344 pre-existing model/review files and 109
unrelated objects were preserved. Eave/ridge heights and panel mounting remain
unmeasured estimates. Before/after objects, geography checks and training
readiness are recorded in
`data/references/scene-roof-solar-expansion-2026-09-23.json`; its adjacent PNG
shows the nine outlines, two ridges and two solar banks.

## First scene-feedback roof classifier (23 September 2026)

Export `a9a8f225f3807c53` contains 270 scenes, 272 confirmed objects, 42 checked
roof families (22 hips, 20 gables) and 12 checked XY graphs. Six further gable
observations include metal, tiled and warehouse roofs. Three have traced ridge
graphs. The western warehouse bay replaces two partial detector fragments with
one full roof outline, retaining both originals in replacement history. This
bay is one roof instance; the adjacent eastern bay remains unreviewed.

Run `9d64ed31c4ee685f` fitted an experimental roof-end classifier from ten usable
hips and ten gables. Training balances class, geographic area, roof and roof
end, so repeated local architecture does not dominate. Validation uses six
roofs with ten usable ends. Area/class-weighted balanced accuracy is 0.91667
(baseline 0.75); weighted log loss is 0.18693 (baseline 0.34064). Regularization
0.01 was selected using validation only. These are small local validation
results, not boundary, apex-height, full-roof or regional accuracy claims.

The fitted candidate is
`data/training/scene-roofs/9d64ed31c4ee685f/roof-form.json`.
Weights/normalization were frozen before test features were opened, with
selection hash
`c71743083a12231053adc69c0b8324b367fc72e0ce52cf5485a553ee51a8e2d7`.
Test has three usable hips but only two usable gables: the northern metal gable
`map-5e209c5e18c637fc/f6c5b3bffed79f43` lacks sufficient end evidence for this
feature extractor. No test score is reported. The candidate is `validated:
false`, not eligible for promotion, and not deployed. Further test annotation
must evaluate these frozen weights without retuning features or selection on
test results. This trains a conditional roof-family classifier, not neural
segmentation, apex coordinates, height or solar weights.

Fifteen additional captures produced 293 building proposals; only the six
explicitly reviewed roofs contribute family supervision. All six class COCO
files are byte-identical to export `22b6bc7fed4248ce`. Before/after outlines,
ridge audits, replacement history, immutable-file checks and training results
are in `data/references/scene-gable-training-candidate-2026-09-23.json` and its
adjacent PNG. The 250-roof cohort remains a review queue; these counts must not
be presented as 250 verified roof labels.

Two subsequent test-area close-ups remain unreviewed because their visible
lines did not establish a confident gable family. They add nine building
proposals and no labels; they are not included in the frozen export above.
The candidate bytes and selection hash remain unchanged. The audit records
this follow-up separately, including two clean stock extraction CLI runs on
isolated image copies after a temporary batch-script shutdown fault.

## Imagery-only prediction and current training run (24 September 2026)

The current map-area and house extraction APIs use `--detector-only`. This runs
SAM3 on the registered image, fits the shared straight-eave boundary model,
then estimates roof ends/planes through the image and geometry model. Saved
scene reviews and imported address CRM geometry are not inputs. The original
predictions and review history remain separate; carrying feedback onto a new
snapshot does not alter its model geometry. The legacy optional CRM-prior path
remains available to explicit CLI callers. ML map-tile footprint generation
also uses the same bounded boundary fitter.

Reproduce the reported scene with:

```sh
scripts/ml/.venv/bin/python -m scripts.ml.scene_extract \
  --scene-id house-30286 --roof-models --detector-only
```

This produced 84 objects, including 13 roofs with 13 successful roof fits. The
reported roof was detected again by SAM3 under the same object ID: its raw
137-point outline became 18 corners and its projecting end was inferred as a
gable without loading the previous correction. The generated outline overlaps
the prior review by 99.992%; that is reproducibility evidence, not independently
measured accuracy. Prediction metadata identifies `sam3-scene-roof-v4`, boundary
and topology versions, active roof-classifier identity and all geometry code
hashes, including the previously missing shared roof-structure hash.

Frozen export `3fe5d7ee6adf4228` was used to retrain the conditional roof-end
classifier as run `c2ea228a5fc006bd`. Usable train data contains 10 hip and 11
gable roofs, with three per class in validation. Validation balanced accuracy
is 0.91667 on 10 end examples and log loss 0.21454, versus the fixed heuristic
baseline's 0.75 / 0.34064. The test split has three usable hip roofs and two
gable roofs; one more usable test gable is required. No aggregate test metric
is reported and the candidate remains `validated: false` / not deployed. The
weights were frozen before opening test features. Do not tune against that
missing/test result or relabel predictions as observed roofs.

The active improvements to boundaries and apex topology are inference model
components. This training run changes candidate hip/gable classifier weights;
it does not fine-tune SAM3, learn apex coordinates, or claim those neural
weights changed. Full run artifacts are under
`data/training/scene-roofs/c2ea228a5fc006bd/`.

The automatic run and all 13 before/after roof summaries are recorded in
`data/references/roof-automatic-inference-2026-09-24.json`. The source model
snapshot remains archived; revision 5 carries existing feedback onto the new
snapshot without changing its model geometry. The running UI was checked in
**Prediction**, with a continuous projecting ridge and straight eaves visible.
Verification: 404 Python tests plus Go/TypeScript/review checks, production
build, map browser test and roof editor lifecycle test passed.

## Ridge junction feedback and construction prior (24 September 2026)

Saved revision 6 for `house-30286` / object `327b8916800376ee` was fetched through
`GET /api/scenes/roof-feedback`. Relative to its own starting estimate, the user
moved eight vertices and introduced four crossing nodes. The projecting-gable
junction cluster shrank from 1.85 m to 0.32 m; the L-bend connector shrank from
1.92 m to 0.18 m. Stored edges increased from 16 to 24 because crossings split
the graph. The useful feedback is continuous ridges and fewer offset junctions,
not a target serialized edge count.

`roof_junctions.py` now supplies a general construction prior: contract short
internal hip clusters, allow unequal wing pitches to meet at a common ridge
height, and jointly solve planar faces while fixing support corners and eaves.
This is bounded candidate generation, not an assertion that all roofs must have
minimal framing. It retains free-end hips, excludes courtyards and distinct roof
levels, limits displacement and slope, and independently checks exact coverage,
overlap, planarity, seam continuity and sampled runoff. Fitting tolerances are
geometric consistency checks, not measured positional accuracy. Optimization
has an iteration budget; a candidate may be considered before solver convergence
only if the independent geometry checks pass.

`roof-topology-v4` scores physical planes, continuous crease runs, junctions and
short junction links. Coplanar storage fragments and collinear subdivisions do
not change that complexity. Candidates on an already-straight outline can win
without reducing face count. Image mismatch against the best valid candidate
(including the original roof) can veto simplification. No scene IDs, reviewer
coordinates or saved corrections are inputs to this inference path.

A fresh `--detector-only --roof-models` run produced all 13 roofs successfully.
For the reported roof, physical planes stayed at seven; continuous crease runs
fell from 11 to eight, junctions from five to two and short internal links from
three to zero. All outlines and the other 12 roof topologies were unchanged.
Mean one-way distance to the draft feedback is essentially unchanged at 0.237 m;
this demonstrates simpler topology, not improved measured roof-line accuracy.
Only three image segments passed the evidence filters for this roof.

The model snapshot records `sam3-scene-roof-v5` and the junction implementation
version/hash. Canonical API recovery saved revision 7 with the user's line edits
unchanged. Revision 6 and the previous model remain archived. Draft linework was
not promoted to verified training data. SAM3 and conditional roof-end classifier
weights are unchanged; this update changes the shared automatic construction
and image inference model. The classifier candidate described above remains
undeployed.

The reproducible diff, before/after metrics, provenance and all 13 independent
roof audits are in `data/references/roof-junction-feedback-2026-09-24.json`; the
adjacent PNG compares the previous prediction, user feedback and new prediction.
Verification: 415 Python tests, Go/TypeScript/review checks, lint (30 existing
frontend warnings), map browser check and complete roof editor/API lifecycle
check passed. Rotation, winding, coplanar-fragment/subdivision invariance,
image-evidence veto and numerical fallback have dedicated regression coverage.

## Connected wing feedback (24 September 2026, revision 11)

Fetched the saved line graph for `house-30286` / `37ff5c02f77869d5` through
`GET /api/scenes/roof-feedback`. Revision 11 lengthens the main and garage
ridges, continues the right wing's ridge through the L junction to its end,
and brings the projecting ridge into a shared apex. Its 31 serialized edges
include crossings and boundary-aligned strokes, so their count is not a target
number of physical roof folds. This remains unchecked draft feedback.

`roof_wings.py` generates bounded overlapping rectangular-wing hypotheses from
the automatically fitted support outline. Through-wings can continue past a
junction; branches stop at the through-ridge. Two pitched planes per wing and
optional free-end hips meet using their upper envelope. Courtyards,
non-orthogonal outlines and unsupported decompositions retain the existing
hypotheses. No address IDs or reviewer coordinates enter inference. Strong
image evidence for a hip or gable prevents a conflicting wing alternative.
All candidates retain the independent image-mismatch veto and final footprint,
planarity, seam and runoff checks.

`registered-roof-lines-v2` inserts shared T-junction vertices on both adjoining
faces before image fitting and ties disconnected fragments of a physical plane
to one parameter set. This prevents an image fit from opening gaps or treating
storage fragments as independent roof planes. Nanometre clipping artifacts and
sub-square-millimetre slivers are removed before auditing candidate surfaces.

A fresh inference over all 14 existing detector footprints succeeded, with all
outlines unchanged. For the edited roof, physical planes fell from 12 to eight,
continuous internal folds from 19 to nine, and short junction links from three
to zero. Image-line mismatch decreased from 0.679 m to 0.566 m. Mean distance
from the saved ridges to predicted ridges decreased from 0.844 m to 0.386 m;
the reverse direction decreased from 0.383 m to 0.353 m. These are fitting
measurements on one development reference, not surveyed or held-out accuracy.
The extra predicted valley at the lower L junction still needs visual review;
saved boundary-aligned hip strokes are not silently discarded from the report.
One neighbouring roof has minor fitted-coordinate changes with the same line
count; the other 12 roofs retain their previous roof-line geometry.

The new model is versioned as `sam3-scene-roof-v7` / `roof-topology-v5`, including
wing and fitting source hashes. API recovery saved revision 12 with the user's
revision-11 roof-line edits verbatim and the previous revision in history.
SAM3 and roof-form classifier weights are unchanged; this is an improvement to
the shared inference model, with no draft labels promoted for training.

Artifacts: `data/references/roof-wing-feedback-2026-09-24.json` and its adjacent
comparison PNG. Validation: 440 Python tests, Go/TypeScript/review suites, lint
(30 existing frontend warnings), and the map browser check passed. The live
CRM was verified to show 11 serialized model edges in Prediction and all 31
saved feedback edges in Your edits. Regression cases cover rotated/reversed
L/T roofs, continuous through-ridges, hip alternatives, observed-hip veto,
courtyard fallback and shared T-junction fitting.

## Straight boundary fitting (24 September 2026)

The revision-11 roof-line update retained the previous 50-corner outline. That
outline still came from a chord fallback: intersecting independently fitted
eaves displaced one corner beyond the 45 cm budget, rejecting otherwise useful
long straight runs across the whole roof.

`roof-envelope-v4` adds bounded joint fitting of shared corners. It samples
source edges by length, fixes image-cut endpoints exactly, and splits only
runs that cannot satisfy the original displacement and area limits. Candidate
selection still checks polygon validity, holes, crop bounds, maximum 45 cm
displacement (or four pixels at finer resolution), and 3% symmetric-difference
area. Edges may have independent directions; rectangles are not forced on
diagonal wings or real recesses. The fallback remains available if fitting
fails. Always start from retained raw segmentation rather than accumulating
successive simplifications.

Reprocessing all 14 existing raw building masks produced valid audited roofs.
The selected contiguous house/garage outline went from 50 to 23 corners, with
38.51 cm maximum displacement from the raw outline and 2.854% symmetric-
difference area. Footprint area changed from 330.67 to 330.29 m². The long eaves
use several straight runs where merging them into one chord would exceed the
existing uncertainty limits. These are segmentation fitting measurements, not
surveyed accuracy.

`registered-roof-lines-v3` polishes coplanarity after image alignment; the crease
extractor compares physical tilt and height rather than coordinate-dependent
plane intercepts. This avoids false creases between numerical fragments of
one roof plane. Eight physical planes remain in the selected new proposal.
Its automatic topology still has an extra valley hypothesis at the upper left
end: continuous crease runs increased from nine to ten. Boundary improvement
does not imply all apexes are now correct; this remains visible for review.

The pure model snapshot is `d36a218a48c883b4ce00acd42006f1064f27633ce394a40148795bbc1ca63e45`
(`sam3-scene-roof-v8`). Canonical API recovery saved revision 13, preserving both
objects' line feedback verbatim, including all 31 target edits and their original
50-corner outline, with revision 12 retained in history. Prediction shows the
new boundary; Your edits retains the saved feedback. No draft was promoted to
a training label, and SAM3/classifier weights were not retrained.

Validation: 448 Python tests, Go/TypeScript/review checks, lint (30 existing
frontend warnings), map/studio verification, and live CRM inspection passed.
Regression cases include rotated/reversed stepped footprints, holes, recesses,
crop endpoints, numerical optimizer failure and coplanar fragment extraction.
Artifacts: `data/references/roof-boundary-feedback-2026-09-24.json` and its
adjacent before/after PNG.

## Canopy-aware angular boundaries (24 September 2026)

The mask-only displacement budget retained the tree-cut bend on the right
eave and independent corner angles at the central projection. The new shared
inference stage, `roof_boundary.py` / `canopy-eaves-v1`, uses registered SAM
canopy masks to downweight obscured boundary samples. Visible long eaves must
establish a coherent angle system. Parallel/perpendicular fitted lines share
corner intersections; clearly visible oblique sides stay independent. It
does not take a convex hull, fill courtyards or join nearby buildings.

Tree detection runs before building fitting regardless of requested class
order. Only unreviewed `sam3-text-onnx` tree geometry supports completion;
ground vegetation and reviewer edits are excluded. A side requires at least
one metre or 20% of its length in observed support. Wholly hidden sides,
image-edge cuts, mixed orientations and failed geometry gates retain the
existing mask fit. Raw masks remain immutable and recoverable.

Construction completion has explicit limits distinct from mask denoising:
bidirectional visible-boundary displacement at most 1 m (or eight pixels),
95th-percentile visible displacement at most 45 cm (or four pixels), canopy
displacement at most 2 m, visible symmetric-difference area at most 4%, and
total symmetric-difference area at most 10%. These constrain hypotheses, not
measured accuracy. Metadata records observed fitting error, canopy masks used,
inferred edge locations/lengths, limits and review requirement. The CRM's
existing model note identifies hidden boundary sections as inferred geometry.

`registered-roof-lines-v4` distinguishes fixed level-eave heights from estimated
above-eave corner heights. The latter can move vertically with their shared
planes while XY remains fixed. Every image fit now gets a planarity-polishing
pass before clipping can split faces. Locking these estimated slope heights
previously disadvantaged a simpler gabled candidate and introduced unnecessary
hip hypotheses after a boundary change. Final coverage, seams and runoff are
still independently checked.

For the development reference, the boundary has 16 corners (previously 23),
14 sides aligned to the shared axes and two retained visible angled sides.
The long right-hand eave continues straight across 7.08 m of canopy overlap;
the central projecting wing is square. Footprint area is 331.88 m² versus
330.29 m² before. Relative to raw segmentation, visible 95th-percentile
displacement is 0.2901 m, maximum visible displacement 0.8703 m, maximum canopy
displacement 1.0953 m, and symmetric-difference area 4.553% overall / 3.088%
outside the buffered canopy masks. Eight roof planes and ten continuous folds
remain; automatic valleys and inferred hidden extents still need review.

All 14 scene roofs passed geometry/runoff checks. The other 13 outlines were
retained by the evidence gates. This is general inference refinement, not
neural-weight retraining or a held-out accuracy result. No address identifiers
or saved reviewer coordinates enter the new fitting code.

Model `04763d7f34147f528c5fb41315189575a4e39a9b91d07b7b78fb4316f477b0f2`
uses `sam3-scene-roof-v9` / `roof-envelope-v5`. Canonical API recovery saved
revision 14 with both saved feedback payloads unchanged, all 31 target edits,
their original boundary and revision 13 in history. Drafts remain unchecked
and ineligible for training. Prediction shows the inferred geometry; Your
edits preserves saved observations.

Validation: 463 Python tests, Go/TypeScript/review checks, lint (30 existing
frontend warnings), and the map/studio browser smoke check passed. New tests
cover rotated/reversed canopy cuts, absence of canopy evidence, real diagonals,
deep recesses, courtyards, fully hidden roofs, crops, detector ordering,
exclusion of reviewer geometry and refitting clipped-slope heights while level
eaves stay fixed. Artifacts: `data/references/roof-canopy-feedback-2026-09-24.json`
and its before/after PNG.
## Parallel eaves and saved valley feedback (24 September 2026)

`registered-roof-lines-v5` retains parallel ridges for symmetric pitched pairs
supported by level, parallel eave runs. Short image fragments cannot rotate or
decentre the entire wing. A parallel observation covering at least 60% of the
ridge can release the midpoint assumption. Unequal seed pitches and unsupported
eave pairs are excluded. These constraints operate inside the shared plane fit;
they do not move roof lines independently of their surfaces.

Revision 15 of the saved Cambridge feedback added an outside-eave valley. The
current pure prediction already contained that fold; regression tests now cover
both L-junction valleys, including the one that exits a straight boundary.
The latest inference keeps the 16-point boundary exactly, reduces the tested
wing's ridge/eave angle from 1.51° to 0.22°, and reduces the largest sampled
eave-distance imbalance from 2.17 m to 0.044 m. These are internal geometric
checks, not measured roof accuracy. All 14 active scene buildings passed surface
and sampled runoff checks. Saved outlines, line edits and address links were
preserved through canonical recovery as revision 16.

This updates inference constraints, not neural weights. The unchecked feedback
remains draft material. Reproduction inputs and comparison are recorded in
`data/references/roof-ridge-feedback-2026-09-24.json`; the cached-observation
regression fixture contains pure-model input and no reviewer coordinates.

## Frozen baseline and neighbouring-house batches

Use `python -m scripts.ml.scene_house_batch --addresses ID ID ...` from the
repository root (with the ML virtualenv) to capture and analyse up to 30 new
houses with the shared detector-only roof pipeline. The default reference is
`house-30286`; `--baseline SCENE` selects another saved reference. This archives
the reference model, imagery, feedback, inference code and regression inputs,
and records the active SAM3/roof-form weight hashes. It does not approve feedback
or train weights.

The command prints a batch ID. `--resume ID` reuses its frozen captures and skips
every published scene or existing review, including feedback saved while a job
was running. Changes to the reference, code, frames or model weights stop reuse
of that checkpoint. Create a new batch to intentionally compare another model.
`--plan` freezes the checkpoint without capturing or analysing new houses.

Each new `house-ID` scene retains image geography, class scan coverage and model
provenance. Failures to fit a roof remain visible as unfitted candidates for
review. Image overlap and geographic split rules still apply before training;
the number of detected roofs across overlapping captures is not a count of
independent training examples. Address capture titles are context, not verified
links to every building in the frame. Use parcel/address evidence before linking.

The September 24 reference is in
`data/references/roof-checkpoints/a983ad7a89b19808`, with batch
`data/scene-batches/b0844e97310822a5`. The frozen observation regression in
`test_scene_roof.py` guards the parallel ridge, centred wing, exact footprint
coverage and outer valley; `test_scene_house_batch.py` guards preservation,
resume, input drift and accidental label approval.

## Independent crease evidence from the next review

The first saved roof-line feedback in the 24 Cambridge capture belongs to a
neighbouring roof suggested as **22 Cambridge Road** by parcel evidence; capture
titles do not identify every object. Revision 2 contains the latest saved XY
observations, kept exactly through canonical recovery as revision 3.

`registered-roof-lines-v6` / `sam3-scene-roof-v11` prevents one observed image
segment from fitting separate roof creases simultaneously. A continuous,
collinear run can share a segment across storage subdivisions. Competing runs
retain only the strongest overlap correspondence. In this case a short hip was
borrowing the long valley's evidence, displacing both its junction and end apex.

Without reading reviewer coordinates, the refreshed prediction reduces the
short-end apex distance to the saved XY point from 1.403 m to 0.379 m, and the
upper junction distance from 1.571 m to 0.071 m. This measures agreement with
draft feedback, not surveyed accuracy. It does not reproduce every saved line
or infer checked heights. Neural weights and training eligibility are unchanged.

The eight nearby scenes were refreshed from their original image/model outlines:
20 of 123 roof proposals changed, 103 stayed geometrically identical, all outlines
were retained exactly, and all emitted roofs passed independent geometry/runoff
checks. The 27 Cambridge model, imagery and saved review remain byte-identical
to their reference snapshot. New tests cover rotated and reversed competing
matches, distinct visible features, collinear subdivisions, and a frozen pure
image/seed fixture. Full validation: 501 Python tests plus Go/TypeScript/review
checks; lint has no errors (30 existing frontend warnings).

Evidence and before/after images:
`data/references/roof-independent-evidence-2026-09-24/`.
The new reference snapshot is
`data/references/roof-checkpoints/e055d6fd61e63b10`.
