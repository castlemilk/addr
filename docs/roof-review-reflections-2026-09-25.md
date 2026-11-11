# Low-evidence roofs: human review shortlist, 25 September 2026

Twelve roofs were visually inspected against registered original RGB, model lines
and detected image lines; ten were new since the preceding visual review. These
are training-area cases, not an independent accuracy benchmark. The active queue
is [Self-review](http://localhost:3008/scenes/processing?view=self-review).

## What to review first

Start with **Outline / connections**. Three cases may join different buildings or
roof levels through a narrow strip. Confirm the real connection before changing
apex lines. The fourth has a poorly supported pointed outline. These are possible
segmentation/data defects; the image files themselves decoded correctly.

Next, use **Roof shape** for long-ridge continuity, unnecessary folds and genuine
multi-wing counterexamples. **Solar / surface lines** separates panel boundaries
from roof folds. **Unclear imagery** keeps hidden eaves visibly uncertain.

For each case, use **Edit and save feedback**, correct the outline/roof lines or
record an issue and notes, save, then return to the shortlist. Saved feedback is
tracked per object, not per scene. The notes below are assistant observations for
the user to verify, not human labels or training approvals.

## Paired experiment and limits

Baseline `8759f75421960226` and comparison `16f69952b517c6b4` contain the exact same
12 model/image inputs. Gates v4 check for narrow connections before fitting one
continuous surface. Three cases now request a connection decision; two previously
exhausted the fitter, while one already lacked enough observed lines.

| Outcome | Baseline | Comparison |
| --- | ---: | ---: |
| Need more evidence | 2 | 4 |
| No passing alternative | 10 | 8 |
| Passing candidates | 0 | 0 |
| Failed worker attempts | 0 | 0 |

The guard is deliberately conservative: a real breezeway can be narrow, so it
never splits or joins roofs automatically. No production geometry, evidence bands,
neural weights or saved reviews were changed. All 257 scan-model hashes and the
four Cambridge Road reference model/review hashes match their baselines. This is
an improvement to diagnosis and feedback handling; no accuracy gain is measured.

Verification: 585 Python tests plus the Go and TypeScript suites pass. Lint has no
errors (30 existing warnings). Production build passes. Browser checks and the
paired artifact are recorded in
`data/roof-review-loop/reflection-verification-2026-09-25.json`.

## Loading later feedback

`GET /api/scenes/self-review` exposes per-object reflection and feedback states
for the active batch. Use `?run=16f69952b517c6b4&object=INDEX` for a frozen
comparison with live feedback. Read full saved
edits through `GET /api/scenes?id=SCENE_ID`; use its model binding/recovery state.
Compare corrected outlines and line graphs against the frozen input, then test a
general strategy with counterexamples using `--start --compare BASELINE_RUN_ID`.
Do not reclassify assistant notes as approved labels or use held-out areas to tune.

## Cases

### 1. Low-pitch roof or several levels?

[Review roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118442-80415&object=cbb791207ec6d650&kind=building&status=all&layer=edited) · `cbb791207ec6d650` · shape

**Observed:** The light roof has equipment and material boundaries, but the model draws diagonal hips across most sections.

**Please check:** Which surfaces are pitched, flat or at different levels? Please mark only ridges and valleys you can identify.

**Next experiment:** Compare roof-family and level hypotheses rather than imposing hips at every corner.

![Original, model and detected-line comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/e4bc85e0e5e6233f/roof-00.png)

### 2. Hidden eave beside the trees

[Review roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118449-80405&object=70d58f070929177b&kind=building&status=all&layer=edited) · `70d58f070929177b` · imagery

**Observed:** Several ridge and hip runs are visible, while trees obscure the upper/right boundary and the fitted junctions diverge from the visible seams.

**Please check:** Mark the visible ridges and valleys, and flag the hidden edge as partly hidden. Does the long left strip belong to the same roof?

**Next experiment:** Fit visible junctions while keeping canopy-obscured eaves uncertain.

![Original, model and detected-line comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/e4bc85e0e5e6233f/roof-01.png)

### 3. Small roof with an uncertain outline

[Review roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118446-80409&object=aab92e5518aa2af2&kind=building&status=all&layer=edited) · `aab92e5518aa2af2` · data

**Observed:** The tan roof has little visible internal structure; the prediction tapers into a sharp lower point that is not clearly supported by the image.

**Please check:** Correct the visible footprint first. Is the pointed lower section actually roof, and can you identify a ridge?

**Next experiment:** Separate outline error from roof-family uncertainty before fitting new faces.

![Original, model and detected-line comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/e4bc85e0e5e6233f/roof-02.png)

### 4. Solar bank and projecting wings

[Review roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118449-80406&object=0c4da3234cca2539&kind=building&status=all&layer=edited) · `0c4da3234cca2539` · solar

**Observed:** The brown roof has clear diagonal seams and a solar bank near the upper edge. The model adds junctions and diverges from several visible seams.

**Please check:** Trace the real ridges and valleys and check that panel edges are not included as roof folds.

**Next experiment:** Evaluate structural-line precision separately from solar-panel edges.

![Original, model and detected-line comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/e4bc85e0e5e6233f/roof-03.png)

### 5. Several roof wings and solar panels

[Review roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118441-80415&object=c49cce79e93e7ad3&kind=building&status=all&layer=edited) · `c49cce79e93e7ad3` · solar

**Observed:** Multiple junctions and panel rows are visible on the dark roof. The model uses a long central run and simplifies some visible branches.

**Please check:** Where should the real junctions meet, and which lines belong to panels or adjoining roof levels?

**Next experiment:** Preserve supported multi-wing structure while avoiding panel-edge matches.

![Original, model and detected-line comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/e4bc85e0e5e6233f/roof-04.png)

### 6. Keep the real valleys

[Review roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118441-80416&object=2145e60496a4d066&kind=building&status=all&layer=edited) · `2145e60496a4d066` · shape

**Observed:** Several roof wings are visible, with a light feature near the upper centre. Small footprint details produce extra model faces.

**Please check:** Mark the main ridges and valleys; distinguish the light rooftop feature from a separate roof plane.

**Next experiment:** Use this as a counterexample to excessive simplification while reducing unsupported small faces.

![Original, model and detected-line comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/e4bc85e0e5e6233f/roof-05.png)

### 7. Solar banks across a joined footprint

[Review roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118446-80410&object=ec7e5e2d53031cb3&kind=building&status=all&layer=edited) · `ec7e5e2d53031cb3` · solar

**Observed:** Two solar banks and several pale adjoining structures are visible. The model folds pass through areas whose roof level is unclear.

**Please check:** Check the outline of the main roof and whether the pale sections are attached roofs, then mark structural lines independently of panel rows.

**Next experiment:** Compare roof-level segmentation and image-supported creases.

![Original, model and detected-line comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/e4bc85e0e5e6233f/roof-06.png)

### 8. Two roofs joined at a corner?

[Review roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118438-80408&object=e85b42e0b6cecac6&kind=building&status=all&layer=edited) · `e85b42e0b6cecac6` · data

**Observed:** Two tan rectangular surfaces are outlined together through a narrow corner connection; the imagery does not clearly establish a shared roof there.

**Please check:** Are these separate buildings, or is there a real covered connection? If separate, correct the merged outline before editing apexes.

**Next experiment:** Defer continuous-roof fitting when substantial sections meet only through a narrow neck.

![Original, model and detected-line comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/e4bc85e0e5e6233f/roof-07.png)

### 9. Roof details or extra folds?

[Review roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118440-80414&object=2f4a1b466955e12a&kind=building&status=all&layer=edited) · `2f4a1b466955e12a` · shape

**Observed:** Dormer-like light features and several material seams are visible on the dark roof. Model junctions do not consistently follow the prominent seams.

**Please check:** Trace the primary ridge and valleys, and note which smaller features are dormers or rooftop objects.

**Next experiment:** Compare primary roof planes separately from secondary roof objects.

![Original, model and detected-line comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/e4bc85e0e5e6233f/roof-08.png)

### 10. Straight main ridge, complex outline

[Review roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118438-80409&object=df1d6b05f4476fb8&kind=building&status=all&layer=edited) · `df1d6b05f4476fb8` · shape

**Observed:** The pale roof shows a long straight line, while the predicted ridge bends around outline jogs and fans into short folds at the lower end.

**Please check:** Should the main ridge continue straight? Please correct its endpoints and check whether the lower dark section shares this roof level.

**Next experiment:** Test long-ridge continuity with a level/attachment check instead of following each boundary jog.

![Original, model and detected-line comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/e4bc85e0e5e6233f/roof-09.png)

### 11. Connected wings or separate roof levels?

[Review roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118443-80415&object=a898ef9d78ef2572&kind=building&status=all&layer=edited) · `a898ef9d78ef2572` · data

**Observed:** Three prominent dark roof sections meet through narrow regions in the outline. The image does not establish that all are one continuous surface.

**Please check:** Which sections are connected, and which have separate heights? Please flag separation or add notes before changing the apexes.

**Next experiment:** Require connection evidence before fitting all sections as one roof.

![Original, model and detected-line comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/e4bc85e0e5e6233f/roof-10.png)

### 12. Possible shed merged with the house

[Review roof](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118450-80405&object=6f8b87d00d965117&kind=building&status=all&layer=edited) · `6f8b87d00d965117` · data

**Observed:** A small pale structure at the upper left is joined to the brown main roof by a very thin outline strip. A visible gap may separate them.

**Please check:** Is the pale structure a detached shed? Split or correct the boundary if needed, and retain the main roof ridge.

**Next experiment:** Detect narrow bridges between substantial roof sections and defer a single-surface fit.

![Original, model and detected-line comparison](/Users/benebsworth/projects/addr/data/roof-review-loop/inspections/e4bc85e0e5e6233f/roof-11.png)
