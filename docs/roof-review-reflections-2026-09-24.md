# Visual review of low-evidence roofs — 24 September 2026

I inspected original imagery, model overlays and extracted-line overlays for
**13 distinct roofs**, then checked one fuller cached observation of a clipped
roof. These are agent observations, not approved labels or measured roof shapes.
Capture titles identify image context; they do not assign addresses to every
building in the image.

The latest automated batch, `c351b97cda2501d3`, checked 12 roofs: **six need more
evidence, six have no passing replacement, zero candidates passed and zero
attempts failed**. This overlaps the visually reviewed sample. Repeated batches
and overlapping captures must not be counted as additional independent roofs.

## What the images show

| Cases (object ID prefix) | Visible observation | Modelling implication to test |
| --- | --- | --- |
| `cbb79120`, `a932e3e3`, `cc22cc43` | Pale or narrow roof surfaces have material/perimeter/equipment lines, with little clear support for the proposed hip networks. | Compare pitched, shed, low-pitch and stepped-level hypotheses. An absent ridge does not establish flatness. |
| `2145e604` | Several hipped wings are visible; tiny boundary jogs introduce extra faces and some junctions are displaced. | Reduce unsupported folds while preserving the visible wings and valleys. |
| `103bd237` | Repeated hip and valley patterns are visible along a joined roof, while the model uses a mostly continuous central ridge. | A counterexample to always minimising roof lines: retain supported repeated structure. |
| `57f68612`, `4da963fa`, `adc36479` | Roofs continue beyond the source image. One three-way junction is also displaced. | Recover image context before fitting endpoints; a crop edge is not an observed eave. |
| `783b0a32`, `4028634f` | Solar arrays, material transitions and adjoining pale roof sections are visible. | Separate structural seams from panel edges and investigate roof-level changes. Model solar masks remain unverified cues. |
| `3119e92e` | Two contrasting roof sections are joined through a narrow tree-obscured area in the model. | Investigate the connection before fitting a continuous surface; hidden pixels cannot establish it. |
| `7ad7a6ac`, `d8a5bbb6` | Small, mostly uniform roofs have no clearly resolved multi-hip pattern at this resolution. | Defer the roof-family choice until clearer imagery or a reviewed label is available. |

The [machine-readable reflection](../data/roof-review-loop/visual-reflections-2026-09-24.json)
retains each scene/object, model and image identity, observation, hypothesis and
next experiment. All records explicitly remain ineligible for training.

## Recovering useful context

For `house-30235 / 57f68612f0d3320a`, the original crop cuts the lower/right roof.
The index found `house-30223 / f50d349ad5c27ab8`, and visual comparison of the
ridge, driveway and pale neighbouring structure supports it being another
observation of the same roof. This remains an agent identity assessment.

The fuller polygon contains 99.98% of the original polygon and has 1.6653 times
its area. That measures recovered detection extent, **not accuracy improvement**.
No detections were merged and neither scene was overwritten.

- [Original, model and detected-line cards](../data/roof-review-loop/inspections/246f956e93aef0d9/REVIEW.md)
- [Fuller-context comparison image](../data/roof-review-loop/inspections/246f956e93aef0d9/fuller-context-f50d349a.png)
- [Latest batch visual pack](../data/roof-review-loop/inspections/b1171f9e77589031/REVIEW.md)

## Implemented review improvements

The Self-review workspace now suggests concrete next actions for clipped roofs,
canopy, solar-line interference, unsupported roof families, segmentation and
surface consistency. Fuller cached views are navigation suggestions with an
identity check, not automatic joins. Rejected hypotheses can be inspected in
aerial and 3D views with their rejection reason; they are never presented as
passing candidates or higher-confidence predictions.

The project [review skill](../.opencode/skills/addr-3d/references/self-review.md)
now requires source-image inspection, separate observations and hypotheses,
counterexamples, protected reference checks and a bounded paired experiment.
`task scene:review-pack CLI_ARGS="--run RUN_ID"` creates immutable, registered
comparison cards and a manifest from a completed batch.

No neural weights, production scene models, saved reviews or training labels
were changed. Candidate acceptance gates were not relaxed. The improvement in
this iteration is actionable diagnosis and recovered viewing context; no real
roof geometry or accuracy gain has yet been established.

## Next bounded modelling experiments

1. Add roof-family and stepped-level alternatives for the unresolved narrow and
   pale roofs. Compare against visible geometry and reviewed family labels;
   do not reward flatness merely because fewer lines are detectable.
2. Test structural-line extraction with reviewed solar/material/shadow examples.
   Measure both missed real creases and spurious folds. Keep fixed original-image
   observations for candidate comparison instead of deleting awkward evidence.
3. Use confirmed fuller observations for clipped cases in isolated experiments.
   Check registration, roof identity, saved-edit protection and geography before
   fitting; inspect junction displacement as well as footprint overlap.
4. Include `103bd237` and `2145e604` as visible-complexity counterexamples when
   testing simpler surfaces. A lower fold count alone is not success.

Publication or training requires a separately evaluated model release and
deliberately reviewed labels. Evidence bands remain distinct from calibrated
accuracy intervals. The frozen Mooroolbark coverage run continues independently.
