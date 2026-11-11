# Review, reflect and improve low-evidence roofs

Use this workflow when asked to review low-confidence tiles, explain roof
defects, improve the self-review loop, or turn recurring failures into modelling
experiments. The goal is better geometry supported by evidence, not a higher
score on the same uncertain imagery.

## Start from a reproducible batch

Work from the `addr` repository root. Read `docs/roof-review-loop.md` for the
candidate gates and artifact schema. Check the active suburb checkpoint before
any extraction or process action. During a frozen coverage run, leave its code,
weights and saved outputs unchanged. Candidate work belongs under
`data/roof-review-loop`; the legacy `scene:self-review --repair` publishes to
scenes and is a different operation.

```sh
task scene:review-loop
task scene:review-loop CLI_ARGS="--start --limit 12"
task scene:review-pack CLI_ARGS="--run RUN_ID"
task scene:review-loop CLI_ARGS="--start --compare COMPLETED_BASELINE_RUN_ID"
```

The loop selects low-evidence detector roofs in training geography. It protects
saved reviews, reference houses and validation/test images. Batches retain code
and input identities, attempts and failures. Do not keep rerunning the same
inputs just to find a pass. If code changes, use a fresh candidate batch and
compare the same physical roofs without counting repeats as new samples.
Use `--compare` to preserve the exact baseline inputs after a code change. It
checks source hashes and review protection and refuses unchanged reviewer code.

The visual pack contains a machine-readable `report.json`, a `REVIEW.md`, and
one registered image card per roof. Actually open the cards with an image viewer;
reading metrics alone is not a visual review. Use the in-app browser when
interactive comparison or 3D inspection is needed. The UI and
`GET /api/scenes/self-review?run=RUN_ID&object=INDEX` expose the same candidates.

## Inspect the source before accepting an explanation

Begin with original RGB, then compare the outline/roof overlay and extracted
line overlay. Contrast enhancement changes appearance but adds no new view or
height measurement. Black padding in a card is outside the source image.
The capture title is image context, not the address of every roof in it.

Route the defect according to the evidence:

| Observation | Useful response | Avoid |
| --- | --- | --- |
| Roof touches the image edge | Inspect suggested fuller cached captures; confirm the same roof before using one. Otherwise request/capture more context in an isolated experiment. | Treating the crop edge as a gable end, eave or measured boundary. |
| Tree masks overlap eaves | Check visible collinear sides and alternate dates/views. Keep inferred sections uncertain. | Bending eaves around foliage or asserting a hidden continuation is observed. |
| White/metal roof has only perimeter or equipment lines | Consider flat, shed, stepped and pitched hypotheses. Record which alternatives cannot be distinguished. | Imposing a hip at every boundary corner, or calling a roof flat just because a ridge was not detected. |
| Many short folds over a joined building | Inspect distinct wings, stepped heights, shared walls and material changes before simplifying topology. | Assuming the minimum possible number of lines is always the correct roof. |
| Substantial roof sections meet through a very narrow neck | Inspect a possible detector merge or real covered connector. Defer a single-surface fit and ask for a separation/connection decision. | Automatically splitting buildings or assuming a narrow join proves continuity. |
| Image lines overlap solar detections | Check the RGB and detector mask. Panel edges can contaminate ridge matching. | Silently deleting observations using unreviewed solar masks. |
| Mesh/graph disagree or faces have conflicting heights | Fix the representation with coverage, shared-height, planarity and drainage regressions. | Letting a lower visual score override a geometric failure. |
| A simpler candidate looks plausible but fails evidence checks | Inspect the explicitly labelled rejected preview and its reason. | Importing it as a passing candidate, human correction or training target. |

The diagnostic router is a triage aid, not a roof classifier. Nearby overlapping
detections are navigation suggestions; overlap does not prove building identity,
ownership or a physically connected roof. Never merge them automatically.

For a multi-building property, inspect `row.property_review` and its frozen
`parcels-INDEX.json` response. Common-property plans can group several detached
roof surfaces without filling drives or treating cadastral divisions as folds.
Incomplete blocks remain flagged. The flat comparison may include a bounded
straight-edge hypothesis over retained raw detector outlines; its wider movement
limits are for review only, with an original-image veto. They do not change the
strict extraction limits or establish roof accuracy. The block example and
regressions are recorded in `docs/roof-property-blocks-2026-09-25.md`.

When numbered lots are available, prefer parcel-conditioned roof allocation over
whole-block aggregation. `roof_lot_review` runs bounded SAM searches from frozen
inputs and retains yards/common property as non-roof unless image evidence says
otherwise. Cadastral cuts are allocation edges, never physical folds; uncertain
outer eaves remain separate. Compare close crops with the wider image before
accepting them: the eight-lot pilot lost 16–33% of roof coverage in close crops.
Commands, provenance and the no-regression checks are documented in
`docs/roof-lot-searches-2026-09-25.md`. This is a candidate inference workflow,
not retrained weights or permission to publish generated labels.

New scene inference now emits `property_roofs` through `scene_property_roofs.py`.
It runs up to 12 per-lot SAM searches automatically and preserves continuous
physical surfaces separately. Reuse only outputs whose source image, frame,
geometry, parcel evidence and allocation policy match; `inherited_review` checks
this. Deferred lots are explicit, including partial model results in the UI.
The no-regression policy additionally protects courtyards, component topology,
boundary displacement and original-image edge support. Never remove these gates
to raise confidence or silently fill lots that returned no roof evidence.

## Record a reflection that can drive an experiment

Write an `observations.json` or Markdown reflection alongside the review pack.
Keep it separate from CRM reviews and training exports. For each visually
inspected case, retain:

- Scene/object, model/image identities and the card used.
- Visible observations (for example, “two ridge runs are visible in RGB”),
  separately from a hypothesis (“the detector may have merged two levels”).
- Remaining ambiguity and whether another crop, date or depth source is needed.
- One general improvement to test, its expected effect and its possible failure
  mode on other roofs. No address-specific coordinates in model code.
- Before/after measurements and a decision: keep as an experimental candidate,
  reject, or defer for evidence. Agent observations are not human approvals.

For a new model strategy, freeze a small paired training-geography sample plus
counterexamples: straightforward gables/hips, genuine multi-wing roofs, holes,
canopy, solar panels and clipped images as relevant to the change. Preserve the
Cambridge reference models and saved edits; compare hashes before and after.
Use existing human line feedback only through its declared training role, not
as hidden inference coordinates or held-out tuning data.

Test the particular hypothesis, not only its average score. Boundary changes
need footprint/courtyard/crop checks; apex changes need junction, surface and
image-line checks. Lower complexity helps only while supported ridges/valleys
and the original outline evidence survive. Report failure counts and unsupported
regions. A synthetic positive-path test proves capability, not real-roof accuracy.

Stop that iteration when its tests pass and the paired comparison is recorded,
or when the available evidence cannot decide. Leave unresolved cases in the
queue with a specific next action. Additional labels or imagery are a useful
outcome; raising confidence without them is not. Do not spend repeated batches
trying unchanged strategies on identical inputs.

## Verification and reporting

For the human review queue, write identity-bound `reflections.json` alongside a
completed batch using the schema in `roof_review_feedback.py`. Keep observation,
review question and next experiment separate. Read the same self-review API after
the user saves: `row.feedback` reports changed fields for that exact object. A
scene save alone is not feedback; a saved correction is not automatic approval.
Retrieve full edits through the canonical scene API, respecting its model binding
and recovery state. Never rewrite feedback merely to fit a newer prediction.

Targeted checks live in `test_roof_review_loop.py` and
`test_roof_review_diagnostics.py`. Use `task test`, `task lint` and
`task verify:ui` for the relevant code/UI changes, with the project service
ports. Rebuild and restart only the verified web worker when necessary; leave
the frozen suburb worker alone while it progresses.

State what actually improved: recovered viewing context, clearer diagnosis,
fewer unsupported faces, better image fit, or measured validation performance.
Separate inference changes from neural training, candidates from applied models,
and model-evidence bands from calibrated accuracy intervals. If no candidate
passes, state that plainly and identify the evidence bottleneck.

For concrete visual examples and current outcomes, read the inspection packs
linked from `docs/roof-review-reflections-2026-09-24.md`. Treat their notes as
agent hypotheses, not reusable ground-truth labels.
The next 12-case queue and connection check are documented in
`docs/roof-review-reflections-2026-09-25.md`.
