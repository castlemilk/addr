# Expanding the scene review set

`scene:batch` prepares object proposals from captured cohort images. It selects
unextracted train/validation images whose whole frame fits the declared split,
and alternates spatial groups so the first review pages cover several areas.
Existing scenes and reviews are preserved. Test images remain held out.

```sh
task scene:batch CLI_ARGS="--cohort roof_training_250 --limit 250 --plan"
task scene:batch CLI_ARGS="--resume BATCH_ID"
```

The first command saves a plan under `data/scene-batches/<id>/plan.json` without
running inference. The plan freezes source image/frame signatures, prompts,
model-component hashes and extraction code. The second command uses the shared
extraction lock, reuses one detector, and checkpoints each image in `run.json`.
Resume skips published scenes and retries failed/unpublished jobs. Changed source
frames, model weights or extraction code require a new plan; existing work still
wins. Source errors and geographic exclusions are reported explicitly.

The initial `bcaa8ccc00ecff4d` plan selects 202 new images (187 train, 15
validation), preserves 25 existing cohort scenes, leaves seven remaining test
images held out, and excludes 16 images crossing split boundaries. It targets
227 cohort scenes in total, alongside the separately captured map scene.

The run completed all 202 new images with no failures on 23 September 2026:
9,728 proposals across ten classes, including 2,379 building proposals and 1,033
solar proposals. These are proposed instances in overlapping imagery, not counts
of independently verified physical buildings or panels. The CRM now has 228
scenes. Image hashes, polygon validity/bounds, parent/group references, complete
scans and full-frame split geography passed, as did the server's review validator
for all 202 snapshots. The 26 prior model/review pairs are byte-for-byte unchanged.
See `data/references/scene-expansion-batch-2026-09-23.json` for the audit.

The initial expansion export `eab13678780e6b8b` records all 228 scenes, 109 confirmed visible
outlines and 78 rejected detections. Its 40 complete class layers supply 56
segmentation masks; the other confirmations remain saved as instance feedback.
Captured images and model proposals must not be counted as reviewed training
labels. Roof apexes and height estimates remain outside these outline targets.

Objects arrive as unreviewed proposals in `/scenes`, with empty reviewed-class
coverage. Extraction never supplies confirmed labels, checked negatives or
measured heights. Use the existing class and feedback filters, swipe review,
outline tools, groups and shape editors to correct them. The training coverage
page tracks reviewed and exportable labels separately from extracted images.

The first reviewed expansion contains 18 car outlines and one road outline,
nine rejected detections, and four unresolved vehicle fragments. Two complete
car layers are checked negatives; two complete road layers contain one visible
road and one checked negative. The densely parked cars and edge fragments also
exercise partial-image instance feedback without inventing background labels.

An additional validation image, scene `30298`, supplies three checked solar
banks: two separated garage rows and one bank on the neighbouring red roof.
Its complete solar layer was checked against the original imagery and registered
crops. Vehicle-like fragments remain unreviewed. The five scene reviews contain
22 confirmed visible outlines, nine rejections and four unresolved objects;
their per-object provenance is in
`data/references/scene-expansion-review-2026-09-23.json`.

The supplemental car training run uses a frozen earlier export, independently
of later batch progress or solar review. Its training examples, model hashes
and validation regression are recorded in
`data/references/scene-expansion-training-2026-09-23.json`.

Subsequent solar and tree review brings export `1207c71fafe7c752` to 138
confirmed visible objects and 108 rejected detections across the same 228
scenes. It has seven complete tree layers: four training, two validation and one
unchanged held-out test image. The latest tree review adds 19 visible masks,
five negatives and seven unresolved objects; only complete class layers enter
full-image evaluation. The proposal-filter experiments and their limitations
are recorded in `docs/scene-proposal-training.md`. Use **Trees → Needs edit**
for the connected crowns and uncertain fragments before the next label export.
