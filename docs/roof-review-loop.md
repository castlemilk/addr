# Low-evidence roof candidate loop

Open `/scenes/processing?view=self-review`. **Review next 12 roofs** starts a
durable bounded worker, with per-roof progress, before/after metrics, every
attempt's rejection reason, and original/candidate aerial, line and 3D views.
The contrast toggle uses the existing registered CLAHE image endpoint. Closing
the page does not stop the worker. Pausing live updates stops polling only.

Each result includes recommended next steps for missing capture context, canopy,
solar-line interference, roof-family ambiguity, topology and surface consistency.
These are diagnostic cues, not classifications. For clipped roofs, the read-only
confidence index can suggest fuller cached captures. Confirm identity and
completeness before using them; the loop does not merge detections.

When no candidate passes, a separately labelled **rejected hypothesis** remains
inspectable with its rejection reason and metrics. It has `eligible: false`, is
excluded from the passing candidate field, and never changes an evidence band.

This is inference search and diagnostics, not neural weight training. Results
are unverified hypotheses. The loop deliberately cannot publish to scene
models, saved reviews, confidence indexes or training labels. The full-suburb
run remains frozen. A high evidence band is not a calibrated accuracy interval.

## Input and comparison controls

- Select low-evidence, untouched SAM detector objects from the deduplicated
  Mooroolbark inventory, one roof per capture per batch. Prioritise topology
  findings and footprints between 40 and 700 m² without excluding other sizes.
- Exclude saved reviews, reviewer edits, CRM-derived objects, Cambridge Road
  reference scenes, and frames that touch validation/test geography. Verify
  both affine transforms, model identity, source PNG checksum and dimensions.
- Freeze full source scenes and reviewer code/model hashes. Re-check live
  source contents before returning a candidate. Changed code interrupts the
  batch; changed inputs are reported as skipped. Timeouts and exceptions remain
  visible per roof. Resume does not repeat finished rows.
- Derive one fixed set of observed lines from the original registered image.
  Every hypothesis is compared against the same observations. Enhanced lines
  must retain original-image support through the existing line extractor.
- Try automatic, gabled and hipped forms; refit eaves from the retained raw mask;
  consider a rectangle only for near-rectangular raw masks. Broad two/four-plane
  alternatives are clipped back to the exact outline so minor silhouette
  corners need not create a fan of roof faces.
- Check outline coverage, planarity, shared seams, sampled drainage and editable
  graph/rendered mesh agreement independently. Detect conflicting heights even
  in small face overlaps. These are consistency checks, not engineering or
  building-code certification.
- Measure both image-to-model residual and model-to-image supported fold length.
  Duplicate image segments cannot inflate supported length. Unsupported folds
  keep a review flag, including when the old complexity threshold passes.
- Reject candidates that lose observed lines, move outlines too far, worsen
  original-image boundary support or increase unsupported complexity. Require
  a material combined score improvement when the original geometry is valid.
  Clipped roofs and roofs with fewer than three usable image segments request
  more evidence. This conservative segment threshold can defer a real simple
  roof; it is an operational threshold, not a rule about construction.

Current candidate gates require at least 98% observed-line retention, outline
IoU >= .95, maximum boundary displacement <= .5 m, no more than .15 pixels of
mean boundary-support regression, supported fold fraction >= .35 and no more
than .02 below baseline, and image residual regression <= .03 m. Valid baselines
also require a score gain of at least .15 and 8%. These thresholds require
evaluation on reviewed validation data before any production promotion.

## API and CLI

```sh
task scene:review-loop
task scene:review-loop CLI_ARGS="--start --limit 12"
task scene:review-loop CLI_ARGS="--start --resume RUN_ID"
task scene:review-loop CLI_ARGS="--start --compare COMPLETED_BASELINE_RUN_ID"
task scene:review-loop CLI_ARGS="--run RUN_ID --detail 0"
task scene:review-pack CLI_ARGS="--run RUN_ID"
```

`GET /api/scenes/self-review` returns compact active batch progress.
`GET /api/scenes/self-review?run=RUN_ID&object=INDEX` returns the frozen scene,
candidate if any, metrics and attempt reasons. `POST` with `{"limit":12}` starts
the next batch; include `resume` for an interrupted run. POST uses the existing
CRM write scope and origin checks. Only one loop worker runs at a time, with a
120-second budget per roof. Completed inputs are skipped when both model and
reviewer fingerprints are unchanged.

`--compare` runs changed reviewer code on the exact frozen inputs of a completed
batch (maximum 24 roofs). It rechecks live model/image identities and saved-review
protection before preparing the comparison. Repeats are paired observations, not
new independent samples. Identical comparison requests reuse the existing run.

Detail responses also expose `row.triage` and, where available,
`row.rejected_preview`; compact polling responses omit the rejected geometry.
Visual packs are immutable snapshots under `data/roof-review-loop/inspections/`
with a manifest, review notes and same-crop RGB/model/detected-line cards. Source
identity, frame registration and image checksums are checked again before rendering.

Artifacts: `data/roof-review-loop/runs/RUN_ID/{report.json,input-N.json,
object-N.json,worker.log,worker.pid,code/}`. There is no apply/approve endpoint.
The older `scene:self-review --repair` command has a separate publication path;
do not use it to modify the ongoing frozen suburb scan.

## Initial observations, 24 September 2026

The first two v2 batches (`8223b97dc308eee0` and `442a320d74282b69`) inspected 24
distinct low-evidence roofs. A third (`07e03151ba5b1dec`) verifies registration,
clipped-frame and code-change protection; it overlaps earlier inputs and picks
up one newly available roof from the advancing scan. Across **25 distinct
roofs**, taking each roof's latest result, five need more image-line evidence,
five need a larger capture, and fifteen have no alternative passing all gates.
Zero candidates were accepted and zero worker attempts failed. Unsupported
folds are the remaining candidates' rejection reason. These batches demonstrate
defect triage and reproducible comparison, **not an observed accuracy improvement**.

A synthetic segmented ridge image verifies the positive path: an unnecessary
hipped topology becomes a simpler image-supported candidate with unchanged
outline, valid faces and preserved original model. This test is not part of
the real-roof sample or an accuracy benchmark. Cambridge Road source model and
review checksums are checked separately for preservation.

The subsequent [visual reflection](roof-review-reflections-2026-09-24.md) covers
13 distinct roofs and a recovered fuller capture. The latest diagnostic batch
`c351b97cda2501d3` has six cases needing evidence and six with no passing
alternative. It produced no accepted replacements; repeated cases are not new
independent samples.

## Better evidence and Gaussian splatting

See also the [25 September review shortlist](roof-review-reflections-2026-09-25.md)
for the next human feedback cases and the paired narrow-connection experiment.

The immediate enhancement path preserves pixel registration and limits local
contrast amplification. [OpenCV's CLAHE documentation](https://docs.opencv.org/4.13.0/d5/daf/tutorial_py_histogram_equalization.html)
explains the contrast/noise tradeoff. It cannot recover unseen eaves, create
measured heights or make generated detail ground truth. Avoid repeated
sharpening, inpainting or super-resolution outputs as independent evidence.

[3D Gaussian Splatting](https://repo-sam.inria.fr/fungraph/3d-gaussian-splatting/)
is designed for novel-view rendering from multiple photographs/video and
camera-calibration points. [2D Gaussian Splatting](https://surfsplatting.github.io/)
targets more consistent surface reconstruction using multiview images, oriented
surface elements and depth/normal constraints. Neither has been integrated in
this change. The application currently has registered orthophotos, not a
calibrated multiview capture set; overlapping crops of one orthomosaic do not
add a new viewpoint.

Recommended next experiments, each with separate train/validation areas:

1. Improve the observation extractor's distinction between structural seams,
   tile texture, solar arrays and shadows using reviewed line labels. Evaluate
   edge precision/recall, not just how many lines are found.
2. Evaluate higher ground-resolution or different-date imagery for obscured
   eaves. Use larger captures for clipped roofs. Keep acquisition date and
   registration uncertainty with every observation.
3. Fuse registered LiDAR or calibrated stereo depth with explicit planar roof
   fitting. Compare boundary distance, ridge/valley displacement, topology
   errors and height errors against independent references where available.
4. If overlapping oblique imagery and camera poses become available, compare
   multiview stereo and surface-focused splatting on a small measured area.
   Keep editable roof planes as the modelling output; attractive splat renders
   alone do not validate a watertight roof or hidden geometry.
5. Train only from deliberately reviewed labels; freeze a candidate release
   and evaluate once on held-out validation geography. Keep the Cambridge Road
   reference/edit checks and only calibrate accuracy intervals when sufficient
   independently reviewed examples exist.

## Human review shortlist and connection guard

Filter **Your review shortlist** by outline/connections, roof shape, unclear
imagery or solar/surface lines. Each annotated case states the observation, a
concrete question and a next experiment. **Edit and save feedback** opens its CRM
editor; return to the queue or press **Refresh feedback** after
saving.

Both API responses expose `row.reflection` and `row.feedback`. Notes live in
`runs/RUN_ID/reflections.json` and must match the scene, object, source model and
frozen input hash. They are assistant observations, not approved labels. Feedback
tracking compares the exact object's outline, line edits, notes and review fields
against its frozen baseline. A scene-level save or another object's correction
does not count. Changed source models and stale reviews are reported explicitly;
the reader never restores, rebinds, approves or modifies them. `feedback_saved`
means an edit exists, not that it is training eligible.

Candidate gates v4 defer single-surface fitting when a 0.75 m inward offset leaves
at least two substantial polygon sections (each at least 6 m² and 6% of source
area). This is a conservative narrow-connection diagnostic, not proof of separate
buildings: real covered connectors are possible. It preserves all source geometry
and requests review of the connection. Broad joins, courtyards, small protrusions,
rotated examples and a narrow standalone roof have regression coverage.

Paired runs `8759f75421960226` → `16f69952b517c6b4` use the same 12 images and model
snapshots: outcomes change from 10 no-safe-gain / 2 need-evidence to 8 / 4. Three
cases now identify the connection issue, including one that already lacked image
lines. There are zero accepted candidates and zero failed attempts. This improves
defect routing; it does not demonstrate more accurate geometry or trained weights.
