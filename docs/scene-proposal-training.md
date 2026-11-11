# Learning from scene corrections

SAM3 sometimes proposes a whole roof as a solar array. A small supervised
proposal scorer now learns from checked positive and negative examples to
filter these masks. It uses detector confidence, footprint area, width, aspect
ratio, rectangle fill and median image colour. It neither changes boundaries
nor learns heights. The app still uses the original detector by default.

## Frozen experiment, 23 September 2026

Dataset `fa15a81e943686de` contains the saved imagery and reviews. Prompt run
`a174675ca1de8d5b` evaluated three wordings on ten training and three validation
images. “Solar panel array” ranked first by validation instance F1: it retained
two matched arrays and produced eight extras. The current “solar panel” run
produced nine extras; the older saved baseline contains ten. Treat this
reproduction difference separately from the wording change.

The learned candidate `0a0db2852ceb9637` fitted regularized logistic weights to
eight usable and nineteen negative proposals across ten training images and
seven area groups. Images share each group's training weight. Normalization is
learned from training examples only. The threshold was fixed at 0.5; validation
selected regularization 1.0 from 0.1, 1 and 10, with stronger regularization
preferred on ties. One-to-one polygon matching still penalizes duplicate
retained proposals.

| Validation result | Array prompt | Learned filter |
|---|---:|---:|
| Images / area groups | 3 / 2 | 3 / 2 |
| Reviewed arrays | 2 | 2 |
| Matched arrays | 2 | 2 |
| Extra proposals | 8 | 0 |
| Missed arrays | 0 | 0 |

Both positive validation arrays are in one image. The other two validation
images are checked negatives. This is insufficient evidence for general
accuracy, and the solar test split currently has no positive arrays. Test
images were not processed for prompt selection or training. The candidate is
not deployed, and original predictions and reviewer labels remain unchanged.
New large arrays, different materials, shadows and partial arrays need broader
validation before adoption.

### Subsequent validation on the expanded review set

The frozen candidate was checked on newly reviewed scene `30298`, which was
absent from its fitting and selection images. Export `eab13678780e6b8b` contains
three visible solar-bank references in that image. Fresh extraction used the
candidate's exact `solar panel array` prompt and the same detector components.
The prompt matched the two garage banks, missed the neighbouring red-roof bank
and emitted one extra. The frozen filter rejected all three proposals, losing
both previously matched garage banks. No weights or thresholds were changed.

This is counterevidence to the earlier three-image result: the candidate should
remain outside production. The saved production-prompt outlines (`solar panel`)
were visually confirmed for these three references, so their exact overlap is
annotation agreement, not an independent boundary-accuracy measurement. The
additional validation, predictions, scores and source hashes are recorded in
`data/references/scene-solar-expansion-validation-2026-09-23.json`. The CRM's
original candidate metrics remain the original frozen evaluation; this later
check is separate and is now inspectable through **Evaluation run → Later
validation**. The candidate selector also flags this later loss while preserving
the original evaluation. **Inspect later regression** takes the reviewer directly
to a filtered reference and its current annotation.

The foreground-only mean outline IoU is unchanged on the positive image
(0.776618). The aggregate mean increases when correctly empty images become
undefined empty/empty comparisons. That increase is not boundary refinement.
The scorer's output is an uncalibrated quality score, not a demonstrated
probability of correctness.

## Reproduce and inspect

```sh
task scene:experiment CLI_ARGS="--dataset data/training/scenes/fa15a81e943686de --kind solar_panel --prompt 'solar panel' --prompt 'solar panel array' --prompt 'photovoltaic panels'"
task scene:train-proposals CLI_ARGS="--dataset data/training/scenes/fa15a81e943686de --experiment data/experiments/scenes/a174675ca1de8d5b --prompt 'solar panel array'"
```

The experiment identifies model components and extraction code by hash. A code
change produces a new run ID; use that run's directory when training again.
Candidate identity includes learned weights, source data, prompt and trainer
provenance. Predictions, per-object scores and reports are generated under
`data/experiments/scenes/` and `data/training/scene-proposals/`.

The compact audit, model coefficients and report are retained in
`data/references/scene-proposal-training-2026-09-23.json`.
Unit tests check split isolation, train-only normalization, repeated-view
weighting, immutable annotations, interior-gap masks, duplicate counting,
changed-image rejection and unavailable positive/negative labels.

Visual comparison: `data/reviews/scene-proposal-training-2026-09-23/validation.png`.

Verification: `task test` passed with 284 Python tests plus Go, TypeScript and
geometry/storage checks; `task lint` has no errors and 31 existing frontend
warnings. `task verify:scene-training WEB_PORT=3008` passed, including updated
checks that allow tree coverage to grow without breaking the empty-state test.
## Inspect candidates in the CRM

Check an existing candidate on newly reviewed validation images without fitting
new weights:

```sh
task scene:validate-candidate CLI_ARGS="--candidate 0a0db2852ceb9637 --dataset data/training/scenes/eab13678780e6b8b"
```

The command excludes images used for fitting or original model selection, blocks
training-area reuse, verifies complete image geography and reuses the exact prompt
and detector components. It freezes weights and threshold. A report under
`data/training/scene-proposals/<candidate>/checks/<check>/` publishes the completed
run. Config identity and per-image checksums bind predictions to that run. Repeating
a completed check verifies its files and preserves them; changed code produces a
different check. Test data is never selected. Only this explicit CLI runs inference;
opening the CRM is read-only.

Open **Training coverage → Model candidates** (`/scenes/candidates`). Select a
trained candidate and evaluation image to inspect both kept and filtered masks,
or hide proposals to inspect the original imagery. **Current review layer**
adds the latest compatible outlines; the validation summary remains frozen.
Scores are uncalibrated outputs, not accuracy probabilities.

An overlap of at least 0.5 suggests an existing same-class annotation to open in
the scene editor, including previously rejected detections. An unmatched mask
can be explicitly added as a needs-edit draft. This preserves prior labels and
history, clears whole-image completion for its class and never marks its height
verified. Save corrections in Scene review, check the whole class for misses,
then export a new dataset for the next training run. Changed image frames or
stale saved reviews must be reconciled in Scene review before importing.

## Learning from individual reviewed instances

`scene:train-proposals --supplemental` accepts a second frozen scene dataset for
training examples from partially reviewed images. Only explicit, unambiguous
matches to confirmed outlines or rejected detections receive labels. Unknown
proposals, conflicting matches, stale reviews, different prompts and reused
images are ignored. The loader independently checks imagery hashes and full-frame
geography; validation and test images cannot contribute fitting examples.

These additional images do not enter detection evaluation. The original fully
reviewed image layers still own precision, recall, misses and extras. Training
weights remain balanced by area and image. `supplemental.json` records each
included original proposal ID and its explicit label; model provenance identifies
both datasets. Candidate inspection discloses the additional data separately.

The 23 September expansion used dataset `7a6570b835fe8f23` to add 19 usable car
matches and five negatives from four training images. Five unresolved proposals
were ignored. Candidate `15623929fbf7f809` fitted 32 usable and ten negative
examples across ten images/seven areas. It still loses one of the five cars in
the original single-image validation set (four matches, one extra). It remains
experimental; more labels have not yet demonstrated better validation results.

```sh
task scene:train-proposals CLI_ARGS="--dataset data/training/scenes/cc35a7cf733dc1f2 --experiment data/experiments/scenes/0fcb12e73c20145d --prompt car --supplemental data/training/scenes/7a6570b835fe8f23"
```

### Expanded car feedback and later validation, 23 September 2026

Fourteen more training images from fourteen areas contributed 50 confirmed
visible car outlines and 18 rejected detections. Twelve uncertain objects remain
needs-edit. Six images have complete car-layer reviews; the others contribute
explicit instance labels only. See
`data/references/scene-car-label-expansion-2026-09-23.json` for each assessment.

Frozen export `e7b4ca6ae6761bc4` fits 82 usable and 28 negative proposals across
24 images and 16 areas. Candidate `846ede92dea12e85` uses geometry and colour;
`c6274041aea30f83` also uses image-context features. Both retain four of the five
cars in the original selection image and remove its two false detections. Both
still lose the partially obscured car `15825800d1b296f5`. Their fixed threshold
is 0.5 and selected regularization is 1.0. Neural detector weights, boundaries
and height estimates are unchanged.

After fitting, the 18 remaining incomplete, geographically eligible validation
images were inspected. New feedback was saved in 13: four confirmed objects,
26 rejected detections and 14 unresolved proposals, plus two unapproved manual
envelopes for missed vehicle-like shapes in scene 30298. Prior ambiguous reviews
were retained. Scene 25320 is another view of the car in 25319 and was left
untouched. Five newly completed layers and the previously completed empty layer
25097 enter the later check. Unresolved layers cannot enter detection evaluation.
The detailed review is in `scene-car-validation-review-2026-09-23.json`.

All four frozen filters were checked against export `094d7fc171d0d1c1`. The
fresh raw predictions are identical across runs:

| Six additional images / two areas | Matched of 1 | Extras | Missed |
|---|---:|---:|---:|
| Unfiltered detector | 1 | 28 | 0 |
| Previous geometry/colour filter, 32 usable / 10 negative | 1 | 8 | 0 |
| Previous image-context filter, 32 usable / 10 negative | 1 | 8 | 0 |
| Expanded geometry/colour filter, 82 usable / 28 negative | 1 | 2 | 0 |
| Expanded image-context filter, 82 usable / 28 negative | 1 | 2 | 0 |

This supports better false-positive suppression on these images. Five of the
six images are empty, and 21 raw false detections occur in one image. Only one
additional positive car is checked, so broader recall is unresolved. Both new
filters retain a whole-roof error in scene 30371 that the previous filters
removed; the other remaining error is a small path fragment. Image-context
features give no aggregate improvement here. No weights or thresholds were
retuned after this check, and no candidate was deployed.

The CRM exposes all four checks under **Evaluation run → Later validation**:
`fcac69f74c426f2f` (previous geometry/colour), `fefd28f6ed142f7a` (previous
context), `1f31e60160d423a3` (expanded geometry/colour), and `aa2b3b1624231294`
(expanded context). Original selection results stay available. The exact
same-prediction comparison, per-image errors, source preservation checks and
verification results are in
`data/references/scene-car-learning-expansion-2026-09-23.json`.

```sh
task scene:train-proposals CLI_ARGS="--dataset data/training/scenes/cc35a7cf733dc1f2 --experiment data/experiments/scenes/0fcb12e73c20145d --prompt car --supplemental data/training/scenes/e7b4ca6ae6761bc4"
task scene:train-proposals CLI_ARGS="--dataset data/training/scenes/cc35a7cf733dc1f2 --experiment data/experiments/scenes/0fcb12e73c20145d --prompt car --supplemental data/training/scenes/e7b4ca6ae6761bc4 --feature-set image-context"
task scene:validate-candidate CLI_ARGS="--candidate c6274041aea30f83 --dataset data/training/scenes/094d7fc171d0d1c1"
```

## Expanded road feedback, 23 September 2026

Fourteen training images from fourteen areas contributed 14 confirmed visible
road outlines and 14 rejected detections. Fourteen proposals remain needs-edit.
Eight road layers are now complete. House/garage access, pedestrian paving and
parking aprons are separate from through carriageways; ambiguous access lanes
remain unlabelled. The audit is
`data/references/scene-road-label-expansion-2026-09-23.json`.

Image inspection also found false gaps around painted road markings. Eighteen
such gaps were removed from four saved masks across training and validation;
vehicle occlusions were retained. Three of these masks still need their outer
boundaries traced and remain unapproved. This is a set of manual image-based
corrections, not a global hole-filling heuristic or a trained boundary refiner.

The frozen `road` prompt run `48b1fdd2335f1984` and base dataset
`cc35a7cf733dc1f2` support a same-prompt comparison. Baseline `fec7c7786e43351f`
uses four usable and sixteen negative proposals from six images/five areas.
Supplemental dataset `b0ac5bf97bb4feb4` increases that to nineteen usable and
thirty-one negative proposals from twenty images/sixteen areas, including the
previously reviewed scene 29537. Candidate `82294d629f57c4b0` uses geometry and
colour; `8d9b39baa8665c2d` adds image context. All use a fixed 0.5 threshold and
validation-selected regularization 1.0. Neural detector weights are unchanged.

| Original validation: four images, six road components | Matched | Extras | Missed |
|---|---:|---:|---:|
| Unfiltered `road` detector | 6 | 6 | 0 |
| Smaller geometry/colour filter | 5 | 0 | 1 |
| Expanded geometry/colour filter | 6 | 0 | 0 |
| Expanded image-context filter | 6 | 2 | 0 |

The expanded geometry/colour filter recovers the lost component. The retained
outlines largely reproduce the original masks used as image-reviewed references;
their perfect overlap here is annotation agreement, not an independent measure
of boundary accuracy.

After fitting, thirteen additional validation layers were reviewed: two outlines
were confirmed, twenty-five detections rejected and eleven left unresolved.
Nine layers became complete. The overlapping view 25320 and previous ambiguous
reviews in 25325 were left unchanged. See
`data/references/scene-road-validation-review-2026-09-23.json`.

Export `7f2630222498b643` supplies ten additional eligible images, including
the previously completed empty layer 25097. Every candidate received identical
fresh raw predictions, and its weights and threshold stayed frozen:

| Later validation: ten images, three areas | Matched of 1 | Extras | Missed |
|---|---:|---:|---:|
| Unfiltered detector | 1 | 17 | 0 |
| Smaller geometry/colour filter | 1 | 3 | 0 |
| Expanded geometry/colour filter | 1 | 3 | 0 |
| Expanded image-context filter | 1 | 2 | 0 |

The two geometry/colour filters retain exactly the same false proposals in
25018, 25326 and 30377. Image context removes the first two but newly retains
accessway `3e95f2b4658a08a1` in 30371; driveway `bda1855bd04a4bf3` in 30377 also
remains. Its later extra count is lower, while its original-validation extra
count is higher. Neither variant demonstrates a consistent overall improvement.

Nine later images are empty and only one positive road component is checked.
Some imagery had previously been inspected for other classes. These are
diagnostic checks, not a blinded test or evidence of general recall. Test images
were not processed; no thresholds were retuned or candidates deployed.

The CRM's **Evaluation run → Later validation** exposes checks
`802fae0b2e2c3f86` (smaller filter), `b0a475b3d3f6213a` (expanded geometry/colour)
and `69f150eed059f32e` (expanded image context). Counts, per-image errors, model
hashes and preservation evidence are in
`data/references/scene-road-learning-expansion-2026-09-23.json`. All 228 original
scene models and 48 unrelated saved reviews were unchanged; 21 existing reviews
were updated and six new reviews created through the revision API. Training
coverage and candidate-review UI verification passed.

```sh
task scene:train-proposals CLI_ARGS="--dataset data/training/scenes/cc35a7cf733dc1f2 --experiment data/experiments/scenes/48b1fdd2335f1984 --prompt road --supplemental data/training/scenes/b0ac5bf97bb4feb4"
task scene:train-proposals CLI_ARGS="--dataset data/training/scenes/cc35a7cf733dc1f2 --experiment data/experiments/scenes/48b1fdd2335f1984 --prompt road --supplemental data/training/scenes/b0ac5bf97bb4feb4 --feature-set image-context"
task scene:validate-candidate CLI_ARGS="--candidate 82294d629f57c4b0 --dataset data/training/scenes/7f2630222498b643"
```

## Small-array feedback and a broader check, 23 September 2026

Seven additional training images were inspected against their original pixels
and registered detector outlines. Ten visible solar banks were confirmed,
including clipped banks, and three roof/background lookalikes were rejected.
Six ambiguous or fragmented detections remain needs-edit and cannot contribute
positive or negative fitting labels. Three images have a completed solar-layer
review. The remaining images contribute explicit instance feedback only.
The review is recorded in `data/references/scene-solar-label-expansion-2026-09-23.json`.
These are assistant imagery assessments available for correction in the CRM;
heights, tilt and hidden extents remain unverified. Source outlines were retained
where visually acceptable, so exact mask overlap is annotation agreement, not an
independent measurement of boundary accuracy.

Candidate `50bd6ad01b5154ab` uses the production `solar panel` wording and frozen
supplemental dataset `1391657c1759eb06`. It learns from 25 usable and 27 negative
proposals across 21 training images and 12 area groups. The same fixed 0.5
threshold and original validation selection procedure were retained. This is a
learned proposal filter; neural SAM3 weights and all boundaries are unchanged.

After those weights were frozen, five further validation images were reviewed.
Four completed negative solar layers added 20 explicit false detections. The
fifth image contains two rejected roof-equipment detections and one unresolved
edge fragment; its incomplete layer is excluded from evaluation. This review is
recorded in `data/references/scene-solar-validation-review-2026-09-23.json`.
Dataset `7653449672180ab3` includes these labels. No validation or test labels
were used to fit the candidate.

Check `33c82ba1be48b9be` evaluates those four new complete images plus the earlier
three-bank image `30298`. Every learned filter below was applied to exactly the
same fresh predictions, with the same prompt and threshold:

| Five additional validation images / three areas | Matched of 3 | Extras | Missed |
|---|---:|---:|---:|
| Unfiltered detector | 3 | 20 | 0 |
| Original-data control: 8 usable / 24 negative fitting examples | 1 | 1 | 2 |
| Existing feedback: 15 usable / 24 negative (`9e0fa3a285e4ca50`) | 1 | 1 | 2 |
| Expanded feedback: 25 usable / 27 negative (`50bd6ad01b5154ab`) | 2 | 4 | 1 |

All three filters retain both labelled arrays and no extras in the original
three-image selection set. The later check exposes the tradeoff: new feedback
recovers the northern garage bank, still misses the southern bank, and increases
false detections on bare roofs. This is not a clean improvement. The filter stays
experimental. Only one image in this additional check contains positive arrays,
so its recall result does not establish general performance. No prompt, weights
or threshold were retuned after inspecting the check.

The same-prediction comparison, exact model parameters, per-image counts and
preservation audit are in
`data/references/scene-solar-learning-ablation-2026-09-23.json`. All 228 original
scene models and all 31 pre-existing scene reviews were preserved. Twelve new
revision-1 reviews were saved through the canonical API.

```sh
task scene:train-proposals CLI_ARGS='--dataset data/training/scenes/fa15a81e943686de --experiment data/experiments/scenes/a174675ca1de8d5b --prompt "solar panel" --supplemental data/training/scenes/1391657c1759eb06'
task scene:validate-candidate CLI_ARGS='--candidate 50bd6ad01b5154ab --dataset data/training/scenes/7653449672180ab3'
```

In **Model candidates**, select `50bd6ad0` and **Later validation**. **Inspect lost
matches** opens the unresolved model failure with a link to its confirmed saved
annotation. The current-review overlay can also reveal kept detections that were
explicitly rejected. In **Scene review**, use **Solar arrays → Needs edit** to
resolve the uncertain bank boundaries and classes before the next frozen export.

## Tree labels and proposal filtering, 23 September 2026

Imagery review added 19 confirmed visible tree masks, five rejected detections
and seven unresolved crown/class boundaries across ten scenes. The negatives
include flat lawn, path-edge specks and a tree shadow. Connected crowns and
uncertain clipped fragments remain unapproved. Four additional complete tree
layers bring the frozen dataset `1207c71fafe7c752` to four training images
(12 masks, one checked empty image, four areas), two validation images
(six masks, one area) and the unchanged held-out test image. Partial reviews
are available for explicit proposal feedback, but not full-image evaluation.
These labels describe visible outlines; no height or full crown shape was
verified. See `data/references/scene-tree-label-expansion-2026-09-23.json`.

Experiment `7a3deda1169adb03` compared the existing **tree canopy** prompt with
**tree** and **individual tree crown** on the six eligible train/validation
images. On validation, the existing wording matched all six references without
extras; **tree** added three extras, while **individual tree crown** missed one
reference. The original wording remains unchanged.

Two experimental filters were fitted with the existing fixed 0.5 threshold:

| Candidate | Training examples | Train matches / extras | Validation matches / extras |
| --- | --- | --- | --- |
| Raw tree canopy proposals | No filter | 12 of 12 / 4 | 6 of 6 / 0 |
| `f6dc24c1e0b7f47a` | 12 usable, 4 negative; 4 images / 4 areas | 12 of 12 / 0 | 6 of 6 / 0 |
| `0eefff842ba5716a` | 23 usable, 5 negative; 10 images / 10 areas | 9 of 12 / 0 | 5 of 6 / 0 |

The second run includes 11 explicit positive and one negative example from six
partially reviewed training images, including two earlier confirmations. Nine
unknown proposals were ignored. Those supplemental images are not evaluated.
Despite the additional labels, this candidate loses the confirmed top-edge
crown `fc2d304de348d1eb` in scene `25098` (score 0.429), as well as three training
references. The CRM marks it **loses matches** and links to the saved outline.
The simpler candidate removes known training errors, but the two positive-only
validation images from one area do not establish false-positive generalisation.
Neither candidate is deployed. Neural detector weights, outlines and height
estimates are unchanged. IoU 1 for accepted unchanged masks measures agreement
with those annotations, not independently measured boundary accuracy.

```sh
task scene:experiment CLI_ARGS='--dataset data/training/scenes/1207c71fafe7c752 --kind tree --prompt "tree canopy" --prompt tree --prompt "individual tree crown"'
task scene:train-proposals CLI_ARGS='--dataset data/training/scenes/1207c71fafe7c752 --experiment data/experiments/scenes/7a3deda1169adb03 --prompt "tree canopy"'
task scene:train-proposals CLI_ARGS='--dataset data/training/scenes/1207c71fafe7c752 --experiment data/experiments/scenes/7a3deda1169adb03 --prompt "tree canopy" --supplemental data/training/scenes/1207c71fafe7c752'
```

`data/references/scene-tree-training-2026-09-23.json` records model hashes,
prompt scores, fitting counts, lost references and preservation checks. All 228
original scene snapshots and 41 unrelated prior reviews remain unchanged; two
existing reviews were extended and eight were created through the revisioned
API. Other classes, source coordinates and height settings were preserved.
Broader validation should include checked empty scenes, isolated crowns,
connected crowns and clipped trees in additional areas before choosing a filter.

## Image context candidates (23 September 2026)

`scene:train-proposals --feature-set image-context` adds seven image-derived
inputs to the existing eight proposal statistics: contact with the image border,
fraction of outline at that border, luminance spread, interior gradient,
green-channel contrast spread, and luminance/green contrast with the outside
neighbourhood. The purpose is to distinguish objects using texture and their
surroundings, rather than only size, detector score and median colour. The
scorer still only keeps or filters existing masks; it does not refine outlines
or infer roof form, height, pitch or depth.

Image bytes are hash-checked against the frozen dataset before feature
extraction. Interior holes are excluded from object and background samples.
Invalid or invisible masks fail instead of receiving fabricated feature values.
The extractor refuses held-out test images. Normalisation is fitted on training
examples only; the threshold remains 0.50 and regularisation is selected using
the original validation set. Explicit partial-image feedback retains its
existing split and unknown-proposal exclusions. The context implementation hash
and per-image feature vectors accompany each frozen candidate; later checks
reject an altered implementation. Existing eight-feature candidates remain
supported and unchanged.

| Image-context candidate | Training examples | Original validation: matches / extras |
| --- | --- | --- |
| Solar `5d25212ae015892a` | 25 usable, 27 negative; 21 images / 12 areas | 2 of 2 / 0 |
| Tree `2ebe39431daa2b3c` | 23 usable, 5 negative; 10 images / 10 areas | 5 of 6 / 0 |
| Car `a5d06ab2f2cb85af` | 32 usable, 10 negative; 10 images / 7 areas | 4 of 5 / 1 |

These original validation counts equal the corresponding supplemented legacy
candidates. The tree and car filters still lose a labelled object and should
not replace the detector. The tree check has two images in one area; the car
experiment has one validation image in one area.

Solar check `cda852b1478d234b` uses the same five additional reviewed validation
images and three areas as legacy check `33c82ba1be48b9be`, dataset
`7653449672180ab3`. Fresh detector outputs were exactly equal in both checks:

| Solar filter | Matches | Extra detections | Missed references |
| --- | ---: | ---: | ---: |
| Raw detector | 3 of 3 | 20 | 0 |
| Legacy `50bd6ad01b5154ab` | 2 of 3 | 4 | 1 |
| Image context `5d25212ae015892a` | 3 of 3 | 2 | 0 |

The new filter retains the previously lost southern solar bank
`59e2588c03781d82` in scene `30298`, with score 0.571. It rejects both remaining
legacy false positives in scene `25018`. In scene `25098`, it rejects one old
false positive but accepts a different one, leaving two extras there. The
improvement is therefore specific and inspectable, not a claim that all errors
have been solved. The additional images were excluded from fitting and the
original regularisation selection, but were already examined during earlier
experiments; this is diagnostic validation, not a new blinded test. All three
positive solar references are in a single image. No threshold or feature changes
were made after this check, and no neural weights or production settings changed.

Open `/scenes/candidates?candidate=5d25212ae015892a&check=cda852b1478d234b&scene=roof_training_250-30298`
to compare the retained solar banks with the current review layer. Choose scene
`25098` to inspect the two remaining extras. Each proposal links to its saved
annotation or can be explicitly imported as an unapproved review draft.

```sh
task scene:train-proposals CLI_ARGS='--dataset data/training/scenes/fa15a81e943686de --experiment data/experiments/scenes/a174675ca1de8d5b --prompt "solar panel" --supplemental data/training/scenes/1391657c1759eb06 --feature-set image-context'
task scene:validate-candidate CLI_ARGS='--candidate 5d25212ae015892a --dataset data/training/scenes/7653449672180ab3'
```

`data/references/scene-image-context-training-2026-09-23.json` records the code,
image and model identities, comparison counts and individual changed decisions.
All candidates remain experimental. More reviewed objects in separate areas
are needed before evaluating a deployable filter; metric 3D accuracy still
requires additional evidence beyond image-based outlines and relative depth.
