# Visual findings: 24 roofs, 25 September 2026

I inspected 24 original RGB / model / detected-line cards across batches
`2ac29fe3be4556f4` and `f54ba9de16abf993`. The corresponding visual packs are
`636b6bb42497e14f` and `10f06337a29ef1b4`. These are distinct roof objects,
with some shared capture areas, not 24 independent validation areas. These
observations and testable hypotheses are not surveyed or human-approved labels.

The most consequential errors are **wrong roof family** and **incorrect grouping
of adjoining roof sections**. Several pale roofs have no visible support for the
many generated hips. Conversely, a row of three visibly hipped sections is
oversimplified into one continuous ridge. Minimising lines indiscriminately would
make that example worse. Trees, shadows and panel/material edges account for
additional uncertainty; contrast enhancement does not resolve hidden geometry.

Across both completed batches: **15 no passing alternative, 9 need more evidence,
0 accepted replacements, 0 failed attempts**. No model output was published and
no weights were retrained. There is no measured accuracy improvement to report.

Prioritise cases 6 (repeated roofs collapsed together), 4 (stepped/low-pitch
sections), 9 (main roof plus cover) and 8 (uncertain connector). After their
section boundaries and families are established, line fitting has a more useful
problem to solve. Preserve case 6 as a counterexample to over-simplification.

## 1. Unsupported folds on a small dark roof

[Open roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118446-80409&object=cc22cc433bafe31a&kind=building&status=all&layer=model) · `cc22cc433bafe31a`

**Observed:** The dark surface has clear perimeter edges but no equally clear evidence for the model's many diagonal hips and transverse valleys.

**Unresolved:** Is this a simple low-pitch or shed roof? Confirm the roof family before adding internal folds.

**Modelling experiment:** Compare flat/shed and pitched families with an explicit uncertain result when only perimeter lines are visible.

![Registered comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/636b6bb42497e14f/roof-00.png)

## 2. Small tan roof: shape remains ambiguous

[Open roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118446-80410&object=7ad7a6acec0084b5&kind=building&status=all&layer=model) · `7ad7a6acec0084b5`

**Observed:** The tan roof is blurred and mostly uniform. A small silhouette notch has produced an internal valley and extra planes in the model.

**Unresolved:** Check the upper notch and roof family; this image does not reliably locate an apex.

**Modelling experiment:** Keep weak-evidence small roofs unresolved rather than deriving internal valleys from every boundary jog.

![Registered comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/636b6bb42497e14f/roof-01.png)

## 3. Rectangular metal roof with a distorted outline

[Open roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118442-80415&object=d8a5bbb6f6aaaf81&kind=building&status=all&layer=model) · `d8a5bbb6f6aaaf81`

**Observed:** The pale metal-looking surface is broadly rectangular. The model cuts its upper corners and extends into the dark lower strip, while adding several diagonal folds.

**Unresolved:** Check the roof-versus-shadow boundary first, then identify any actual ridge.

**Modelling experiment:** Evaluate footprint corner regularisation against visible edges; keep shadows separate from roof extent.

![Registered comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/636b6bb42497e14f/roof-02.png)

## 4. Roof levels mistaken for pitched hips

[Open roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118441-80416&object=a932e3e3eb4e2790&kind=building&status=all&layer=model) · `a932e3e3eb4e2790`

**Observed:** Three adjoining light/dark rectangular sections and rooftop equipment are visible. The model fits a continuous pitched ridge with diagonal hips across these sections.

**Unresolved:** Which sections are separate low-pitch levels? Treat equipment and material changes separately from roof folds.

**Modelling experiment:** Add stepped and low-pitch roof-family hypotheses before fitting a single pitched surface.

![Registered comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/636b6bb42497e14f/roof-03.png)

## 5. White roof: perimeter jogs create false complexity

[Open roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118441-80415&object=48e4ea59f7d31cb5&kind=building&status=all&layer=model) · `48e4ea59f7d31cb5`

**Observed:** The white surface has straight parallel material seams and small rooftop fixtures. Most model hips and valleys have no corresponding strong seam in the original image.

**Unresolved:** Check whether this is low-pitch, and correct the boundary where the model follows the dark lower edge.

**Modelling experiment:** Separate roof family, shadow boundaries and small footprint errors before generating a ridge graph.

![Registered comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/636b6bb42497e14f/roof-04.png)

## 6. Several hipped roofs collapsed into one long ridge

[Open roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118440-80414&object=103bd23746abddfc&kind=building&status=all&layer=model) · `103bd23746abddfc`

**Observed:** The long footprint contains three repeated hipped roof sections with visible independent junctions. Solar banks are visible near the upper section. The model spans them with one long centre ridge.

**Unresolved:** Mark the real joins between the repeated roofs and retain each visible ridge/valley pattern.

**Modelling experiment:** Segment attached roof sections or levels before fitting; use this as a counterexample to excessive ridge simplification.

![Registered comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/636b6bb42497e14f/roof-05.png)

## 7. Clear roof seams, displaced junctions

[Open roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118449-80406&object=06752256a3d77780&kind=building&status=all&layer=model) · `06752256a3d77780`

**Observed:** The brown roof has a prominent diagonal seam on the right and a real inset at the upper left. The model junctions and short corner folds do not follow all visible seams.

**Unresolved:** Trace the clear diagonal and its meeting point first; distinguish the grey rear cover from the tiled roof.

**Modelling experiment:** Fit shared junctions to long supported lines while suppressing short corner folds caused by outline noise.

![Registered comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/636b6bb42497e14f/roof-06.png)

## 8. Metal annex and tiled house share an uncertain connector

[Open roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118443-80415&object=3119e92ed7fef010&kind=building&status=all&layer=model) · `3119e92ed7fef010`

**Observed:** The tiled main roof and pale metal upper section are joined through an irregular narrow region. Trees obscure part of the connection. The model fits them as one continuous pitched roof.

**Unresolved:** Is there a covered connector, separate height level, or an actual gap? Keep the hidden portion uncertain.

**Modelling experiment:** Use image/material and level evidence alongside the narrow-neck check; width alone misses some merged or stepped assemblies.

![Registered comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/636b6bb42497e14f/roof-07.png)

## 9. Rectangular cover given a pitched roof fan

[Open roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118438-80409&object=e9e7b15f3ef69516&kind=building&status=all&layer=model) · `e9e7b15f3ef69516`

**Observed:** A dark rectangular striped cover adjoins the main hipped roof on the right. The model extends a multi-hip structure across the cover although its visible lines are largely parallel.

**Unresolved:** Check whether the cover is a lower shed/flat surface, and keep its junction separate from the main roof apex.

**Modelling experiment:** Fit mixed roof families per attached section rather than assigning one topology to the entire footprint.

![Registered comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/636b6bb42497e14f/roof-08.png)

## 10. Straight seam, unnecessary corner folds

[Open roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118438-80408&object=f4171d29da314a78&kind=building&status=all&layer=model) · `f4171d29da314a78`

**Observed:** The pale rectangular roof has a long near-straight central seam. The model has a bent ridge and extra small corner triangles. The image alone does not establish the pitch or height.

**Unresolved:** Confirm whether the central seam is a ridge; if so, mark its straight endpoints and correct the corner outline.

**Modelling experiment:** Test straight-ridge continuity and rectangular eaves, while distinguishing roof seams from actual creases.

![Registered comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/636b6bb42497e14f/roof-09.png)

## 11. Canopy and shadow obscure a small roof

[Open roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118449-80405&object=79e5a10dcc3a868d&kind=building&status=all&layer=model) · `79e5a10dcc3a868d`

**Observed:** Trees obscure the upper edge of a dark roof. A few straight surface lines are visible, but the model infers several short hips at corners that are hard to verify.

**Unresolved:** Mark visible roof lines and hidden extent; avoid confirming an exact boundary behind foliage.

**Modelling experiment:** Use visible eaves as constraints and retain uncertainty for hidden corners instead of treating an inferred continuation as observed.

![Registered comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/636b6bb42497e14f/roof-10.png)

## 12. Solar panels and pale additions complicate the roof graph

[Open roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118445-80410&object=4028634f68ceda3b&kind=building&status=all&layer=model) · `4028634f68ceda3b`

**Observed:** Two panel banks and several pale attached surfaces are visible on a brown multi-wing roof. The model runs hips and valleys across material/attachment boundaries that may be separate levels.

**Unresolved:** Separate panel edges from roof folds and identify the pale additions before correcting the main ridge network.

**Modelling experiment:** Compare per-section roof families and solar-aware line matching without erasing genuine multi-wing valleys.

![Registered comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/636b6bb42497e14f/roof-11.png)


# Follow-on batch: 12 further roofs

Batch `f54ba9de16abf993`, pack `10f06337a29ef1b4`. All 12 cards visually inspected. Seven had no passing alternative and five needed more evidence; no replacements or failed worker attempts. These are different object IDs from the first 12, with some shared capture areas, so this is not 24 independent validation areas.

Across both batches: **24 inspected roofs, 15 without a passing alternative, 9 needing evidence, zero accepted replacements and zero worker failures.**

## 13. Repeated white roofs treated as one pitched strip

[Open roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118442-80415&object=324dd58f35837dfa&kind=building&status=all&layer=model) · `324dd58f35837dfa`

**Observed:** Several repeated pale roof sections are visible along the drive. The model spans their boundaries with a continuous ridge and adds hip fans at the two ends.

**Unresolved:** Identify separate roof levels and repeated section boundaries before fitting ridges.

**Modelling experiment:** Detect repeated attached sections and compare low-pitch/stepped families per section.

![Registered comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/10f06337a29ef1b4/roof-00.png)

## 14. Another row of roof levels collapsed together

[Open roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118441-80416&object=fe7f7252c4581be6&kind=building&status=all&layer=model) · `fe7f7252c4581be6`

**Observed:** The long white row has repeated rooftop equipment and transverse boundaries. The generated central ridge and end hips do not follow that repeated layout.

**Unresolved:** Check where the roof levels separate; do not treat every crosswise material edge as a valley.

**Modelling experiment:** Use this independent row as a counterexample to the single-long-roof assumption.

![Registered comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/10f06337a29ef1b4/roof-01.png)

## 15. Simple L surface with uncertain pitch

[Open roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118446-80409&object=466e517595f1754f&kind=building&status=all&layer=model) · `466e517595f1754f`

**Observed:** The beige L-shaped surface shows a long seam and fairly regular perimeter, but the corner hip fan is not clearly supported in RGB.

**Unresolved:** Confirm pitch and whether the larger right section shares a plane or level with the long wing.

**Modelling experiment:** Compare simple mixed/shed roof sections with the default hipped L model.

![Registered comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/10f06337a29ef1b4/roof-02.png)

## 16. Real pitched wings joined to a pale cover

[Open roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118449-80406&object=8a551b00e4070445&kind=building&status=all&layer=model) · `8a551b00e4070445`

**Observed:** Visible tile seams define several roof wings. A large pale rectangular cover in the lower section is also fitted with diagonal pitched faces.

**Unresolved:** Separate the cover from the real tiled roof planes; preserve the visible diagonal seams.

**Modelling experiment:** Fit a roof family for each material/level section, then solve its connection to adjacent pitched wings.

![Registered comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/10f06337a29ef1b4/roof-03.png)

## 17. Solar bank and lower annex distort the ridge

[Open roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118441-80415&object=4d8e4d2cb5db0fb9&kind=building&status=all&layer=model) · `4d8e4d2cb5db0fb9`

**Observed:** A long main roof with panels joins a lower-looking rectangular section. The model ridge kinks at the material/junction boundary and adds transverse valleys.

**Unresolved:** Check the actual roof connection and panel edges; the lower section may need a separate height plane.

**Modelling experiment:** Separate solar observations and annex level changes from the main roof ridge fit.

![Registered comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/10f06337a29ef1b4/roof-04.png)

## 18. Multi-wing roof needs correctly placed junctions

[Open roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118446-80410&object=6594e09b41958c66&kind=building&status=all&layer=model) · `6594e09b41958c66`

**Observed:** Several long ridges and diagonal seams are visible. The model shifts their junctions and creates extra corner faces instead of following the prominent lines.

**Unresolved:** Trace the supported ridges and where they meet. This is a genuine complex roof, not a simple box.

**Modelling experiment:** Constrain shared junctions to strong image lines while retaining supported branches.

![Registered comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/10f06337a29ef1b4/roof-05.png)

## 19. Long yellow roof hidden behind canopy

[Open roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118440-80414&object=5add75a8f41b1783&kind=building&status=all&layer=model) · `5add75a8f41b1783`

**Observed:** Trees obscure much of the long roof perimeter. Only a short internal line is detected, while the model supplies multiple end hips and a transverse valley.

**Unresolved:** Keep obscured edges uncertain and confirm whether the long centre seam is a ridge.

**Modelling experiment:** Use a different date/view for hidden edges; do not turn boundary irregularities into roof valleys.

![Registered comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/10f06337a29ef1b4/roof-06.png)

## 20. Small red roof too blurred for precise apexes

[Open roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118449-80405&object=58fd462bcfd701c8&kind=building&status=all&layer=model) · `58fd462bcfd701c8`

**Observed:** The red roof is low-resolution and lacks clearly resolved internal seams. The model adds many small corner folds to its irregular outline.

**Unresolved:** Correct only clearly visible eaves and leave exact ridge/hip placement unconfirmed.

**Modelling experiment:** Reduce unsupported small-face hypotheses and retain roof-family uncertainty at this resolution.

![Registered comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/10f06337a29ef1b4/roof-07.png)

## 21. Two pale structures joined across a dark band

[Open roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118438-80408&object=1d9d8a5cc510fd9b&kind=building&status=all&layer=model) · `1d9d8a5cc510fd9b`

**Observed:** Two distinct pale rectangles are separated by a dark transverse region but share one outline and a continuous model ridge network.

**Unresolved:** Is the dark band a gap, covered connector or height step? Check separation before apex editing.

**Modelling experiment:** Use section-boundary and shadow evidence alongside footprint connectivity.

![Registered comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/10f06337a29ef1b4/roof-08.png)

## 22. Panel rows dominate lines on a long pale roof

[Open roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118456-80415&object=366c0dc6d3010004&kind=building&status=all&layer=model) · `366c0dc6d3010004`

**Observed:** The image shows long parallel solar-panel banks and rooftop equipment. Most detected lines follow the banks; the model adds end hips and crosswise folds without comparable visible support.

**Unresolved:** Identify true structural seams separately from panel rows and establish the underlying roof family.

**Modelling experiment:** Evaluate solar-aware structural-line extraction and a low-pitch roof hypothesis; panel alignment is not ridge evidence.

![Registered comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/10f06337a29ef1b4/roof-09.png)

## 23. White rectangular roof given unsupported end hips

[Open roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118438-80409&object=6fbe3e4a881d3d30&kind=building&status=all&layer=model) · `6fbe3e4a881d3d30`

**Observed:** The pale rectangle has equipment and fine surface texture, but no confidently resolved ridge in the original image. The model invents a short ridge and multiple hips.

**Unresolved:** Confirm whether the roof is flat, shed or pitched before accepting internal geometry.

**Modelling experiment:** Allow low-pitch and unknown roof-family outcomes rather than forcing a pitched prior.

![Registered comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/10f06337a29ef1b4/roof-10.png)

## 24. Long orange multi-wing roof: real hips, wrong joins

[Open roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118443-80415&object=c558bea70eafc7d8&kind=building&status=all&layer=model) · `c558bea70eafc7d8`

**Observed:** The orange roof has visible hips and valleys across several wings. The generated graph ignores or shifts some joins and adds small triangular faces at footprint jogs.

**Unresolved:** Retain genuine wings and correct the junction positions; do not globally minimise all roof lines.

**Modelling experiment:** Use supported multi-wing intersections as a regression against over-simplified long ridges.

![Registered comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/10f06337a29ef1b4/roof-11.png)
