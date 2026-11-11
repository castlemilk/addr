# Whole-image object review

## Broader imagery and the first checked gable — 23 September 2026

After screening 24 further training captures dominated by hipped roofs, five
new map scenes were captured in four training-area groups. Their 602 detector
proposals remain separate from reviewed labels. The clear metal gable in
`map-d43871249a7997e8` has a corrected four-corner outline, two planar faces,
and an image-traced ridge extending to both ends. Its 3.2 m eave and 5.4 m apex
are display estimates; the training target contains XY only.

Revision 3 also checks 14 existing car outlines with full/occluded/truncated
visibility. A partly hidden vehicle fragment at the image edge remains flagged
for class and outline review, so the car layer stays incomplete. Two vehicle
roofs incorrectly detected as solar panels and a blue roof detected as a pool
are explicit rejections. Whole-image solar and pool checks export this scene
as a negative image for those classes. Building segmentation remains incomplete.

Frozen export `3bdbd3e134804ab3` contains nine roof-family observations and three
XY linework observations. Roof training run `042e5efbc699c854` finds five usable
hip roofs and one usable gable, with no held-out family labels; it produces no
weights. Existing 303 scene/review files and the five new detector snapshots
were preserved. The per-object audit is
`data/references/scene-gable-object-review-2026-09-23.json`.

The selected roof's 3D preview now outlines its actual surface creases and eaves.
Coplanar triangulation diagonals are omitted. The overlay follows saved or draft
geometry, participates in isolation, and does not create annotations or intercept
object picking. This makes ridge/apex feedback legible without changing a roof's
shape or approving it.

## Train roof ends from saved scene feedback — 23 September 2026

`task scene:train-roofs CLI_ARGS="--dataset data/training/scenes/394c5f1bb50f4a08"`
now connects the frozen scene roof-family observations to the shared roof-end
classifier. Run `task scene:training` after further corrections and supply the
new export directory. The command publishes an immutable report, feature audit
and, only when the prerequisites pass, a candidate `roof-form.json` under
`data/training/scene-roofs/`. It never changes saved reviews or deploys weights.

The consumer independently rechecks the exported review, outline, visibility,
image identity and split geography. Repeated physical roofs count once;
conflicting family observations are withheld. Hip and gable image support is
measured against the same unchanged roof prior in training and prediction.
Installed weights, edited apexes and guessed heights cannot supply features.
Flat, mixed and unchecked forms are not coerced into binary labels.

Each class needs ten usable training roofs in three areas, and three roofs in
two areas for each holdout split. Fitting balances areas and roofs; validation
selects regularization. Test imagery is read only after weights are frozen, for
reporting. This is an experimental roof-end classifier, not dense outline,
apex, solar or neural model training. See `docs/roof-training.md` for details.

The first real run found five usable hip roofs across three areas among the
eight scene family observations. Three have insufficient image support for this
classifier. No gables or held-out family targets are available, so it wrote a
readiness report and no weights. The two XY roofline targets remain exported
separately; this classifier does not learn their point positions.

## First scene roof training observations — 23 September 2026

An assistant imagery review inspected 19 training captures and saved eight
corrected, individually confirmed roof outlines across four geographic groups.
All eight have visible hipped forms; two roofs in the Holmes Road capture also
have image-traced ridge/apex and hip linework. Other rooflines and ambiguous
roof families remain unchecked. These are imagery interpretations, not surveyed
boundaries or independently verified height labels.

Frozen export `394c5f1bb50f4a08` contains eight family targets and two image-XY
linework targets. No whole building segmentation layer was marked complete.
Existing road/class completion and unrelated objects were preserved. The
original 228 model snapshots and held-out reviews were unchanged; only four
scene review files changed through the revisioned API. Estimated display
heights remain excluded from exported roof targets. The audit and per-object
evidence are in `data/references/scene-roof-training-seed-2026-09-23.json`.

This is an imbalanced seed: eight hip examples and no gable, flat or mixed
examples in the new scene feedback. It does not support a balanced roof-family
classifier or a validated roofline learner. No weights were fitted or deployed.
The scene roof-end training command above now consumes these observations;
sufficient varied examples and independent evaluation are still required.

## Roof observations for training — 23 September 2026

For a building, **Object details → Roof training feedback** records the observed
roof family independently of its generated mesh. **Rooflines checked against
imagery** checks the current ridge, hip and valley positions and face layout in
the image plane. Use **Edit roof apexes** to correct those positions first.
Neither choosing a template nor swiping an outline creates these checks.
Arrow-key roof nudges remain draft edits; opening the roof editor suspends the
global left/right swipe shortcuts.

Feedback snapshots retain the outline and XY geometry reviewed. An outline
change makes both checks stale; a roofline/face change invalidates the linework
check. Undo restores the matching observation. Estimated Z changes preserve XY
feedback and never supply measured heights. Changed observations remain in saved
history; recheck explicitly after correcting their geometry.

**Training coverage → Buildings** lists family/roofline counts by split and
links to saved checks with any withholding reasons. Export writes independent
`roof-feedback.jsonl` instance targets alongside schema-6 scene data. These
require a confirmed, fully visible building, current model/image, valid split
geography and matching feedback snapshot. Linework also needs valid projected
face coverage without overlaps and no outstanding shape flag. Incomplete
building mask layers do not withhold a separately checked instance; they remain
incomplete for segmentation. No unchecked building becomes a roof negative.

Targets contain image XY only. Relative apex heights, base elevation, pitch and
material estimates stay in the audit. The scene roof-end training command can
consume explicit hip/gable families; exporting alone does not fit weights.
Verification: `task verify:scene-roof-feedback WEB_PORT=3008`, API, recovery and
training-export tests.

## Framing and steady outline edits — 23 September 2026

The aerial card fits the selected object to the available viewport shape.
Wide roads use the landscape card width, while tall and edge-clipped objects
remain fully in frame. Small objects retain surrounding imagery for context.
Prediction, edits and comparison share the bounds of both outlines, so switching
layers does not hide an offset by silently moving the image.

Dragging an outline point, nudging with arrows or entering image coordinates
keeps the view still. To fit a substantially changed outline again, choose
**Whole scene**, then **Focus object**. Zoom retains the current visible centre;
changing objects recentres the retained zoom. **Add object** still starts with
the whole capture. All coordinates remain registered to the source image, and
view controls never save or approve a label.

`task verify:scene-framing WEB_PORT=3008` checks laptop detail, layer alignment,
centred zoom, selection changes, mouse/touch/numeric/keyboard edits, whole-image
drawing, save/reload, unchanged predictions and visible desktop/mobile decisions.

## Adjust roof points in 3D — 23 September 2026

Select a building with a roof shape, choose **3D scene**, then choose a point
under **3D edit handle**. Drag its vertical handle or enter **Point height above
base (m)**. The highlighted connected horizontal ridge moves together. Point
numbers match the aerial roof editor. **Building height** returns to moving the
whole roof vertically; a point edit instead retains the eaves and building base.
Moving an eave point explicitly changes that point only.

Pointer edits preview before release and create one undo step. Escape and touch
cancellation discard the preview. Handles remain accessible at viewport edges;
arrows adjust by 0.1 m, Shift by 1 m, and numeric entry accepts 0–100 m above the
building base. Non-planar or overly steep drafts display geometry feedback.
Use the aerial roof editor to move ridge points horizontally.

Both editors share the connected-ridge height rule. Roof-shape feedback keeps
independent class/outline approvals and clears height verification. **Save
scene**, history and failed-save recovery use the existing scene revision flow.
Original predictions remain read-only. These edits are estimated geometry,
excluded from measured height and apex training targets.

`task verify:scene-roof-height WEB_PORT=3008` exercises mouse, keyboard, touch,
viewport-edge cancellation, linked ridges, numeric entry, aerial-editor agreement,
planarity feedback, failed-save retry, save/reload and outline approval separation.

## Adjust height in 3D — 23 September 2026

Choose **3D scene** in **Your edits** or **Compare**, then drag the selected
object's vertical handle. The mesh previews immediately with a steady camera.
Release to keep one undoable edit; Escape or a cancelled touch discards the
preview. **Save scene** persists it through the same revision/history workflow
as numeric edits. The handle has a 44-pixel mouse/touch target.

Tab to the handle and use arrows for 0.1 m changes, or Shift plus an arrow for
1 m. Roads, driveways, footpaths, solar banks and skylights edit thickness in
0.01 m steps. Home and End choose the valid lower and upper bounds; the numeric
inspector uses the same bounds. Roof heights stop before a vertex would leave
the saved 0–100 m range. Changing building height shifts its roof vertically
while preserving ridge/eave differences; use roof-point editing for individual
apexes or pitch changes. Base elevation stays fixed.

Tree crown proportions, car body settings, road grade/crossfall and solar tilt
remain intact. Existing outline/class approvals also remain intact. Height
edits are explicitly saved as reviewer estimates, not measured training labels.
Predictions and partial tree/car footprints have no height handle. Group view
remains an inspection surface. From a directly overhead camera, orbit to a side
view to drag vertically, or use the numeric/keyboard controls.

`task verify:scene-height WEB_PORT=3008` checks preview/commit, one-step undo,
mouse/touch cancellation, keyboard, comparison/visibility gates, roof apex
translation, thickness, approvals and save/reload on desktop and mobile.

## Turn objects directly in the image — 23 September 2026

In **Your edits** or **Compare**, drag the round end of a car, road/surface or
tilted solar arrow to adjust its direction against the imagery. The arrow
previews while dragging; release creates one undoable edit. **Escape** or a
cancelled touch discards that preview. Swipe elsewhere in the card to continue
reviewing. The handle stays the same touch size when the image is zoomed.

Tab to the handle and use arrow keys for 1° changes, or Shift plus an arrow for
10°. Home points north; End selects 359.99°. The inspector's bearing field is
still available for exact values. Directions use geographic north, including
when the captured image is rotated. Positive road grade rises along its arrow;
the solar arrow points downhill.

Turning a car retains its body style and cabin proportions. Turning a road
retains its grade and crossfall. Turning a solar bank retains its current tilt
and clearance, and makes an automatically fitted orientation manual. A level
bank has no downhill arrow. Partial cars remain footprint previews and have no
direction handle. Prediction arrows are read-only.

These are estimated 3D settings saved through **Save scene**. Outline/class
approval, groups and parent links are preserved, and no measured height or
3D training label is created. `task verify:scene-direction WEB_PORT=3008`
checks drag/keyboard/touch, cancellation, undo, save/reload and swipe separation.

## Model checks against saved labels — 23 September 2026

**Training coverage** now includes a **Model check** for the selected object
class and split. Counts separate matched references, extra detections and missed
references; precision, recall and outline overlap retain their denominators.
Open **Inspect evaluated images** to return to the underlying reviewed layer.
The layer-status filter does not alter the evaluation cohort. No eligible,
fully scanned layers means no score, rather than a perfect empty result.

`task scene:evaluate` computes a read-only baseline. `task scene:training`
also writes `evaluation.json` beside each exported dataset; live coverage and
the file share the same calculation. Reports retain dataset version, original
model signatures, imagery hashes and review revisions. No detections or labels
are changed by evaluation. Training, validation and test remain separate.

The evaluator inherits all export gates: current review, complete class check,
no pending objects, verified image geography and no conflicting splits. It
also requires a completed detector scan for that class. Unscanned manual images
can provide labels, but cannot establish detector recall. Each detection matches
at most one reference at polygon IoU ≥ 0.5, with maximum match count followed
by maximum summed IoU. Duplicate and invalid predictions count as extras.
Interior gaps participate in intersection/union. Outline overlap is the mean
per-image union IoU; empty/empty images have no overlap denominator and are
excluded from that mean. Precision and recall are undefined when their own
denominator is zero. This is a fixed-threshold diagnostic, not COCO AP.
Matching uses [SciPy linear assignment](https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.linear_sum_assignment.html);
the distinction from AP follows the [COCO evaluator](https://github.com/cocodataset/cocoapi/blob/master/PythonAPI/pycocotools/cocoeval.py).

On dataset `5e0f94c9aa567d83`, solar validation has 12 original detections,
two matched references and ten extras across three checked images in two areas
(16.7% precision). Road validation has six matches and five extras across four
images; car validation has five matches and two extras in one image. These are
diagnostics against the existing imagery-based annotations, not independent
ground truth or a new model benchmark. Repeated areas and small samples limit
generalisation. Heights, roof apexes and complete 3D shape are not evaluated.
Test data should remain reserved for final candidate comparison, not tuning.

The regression suite covers assignment conflicts, duplicate detections, misses,
invalid outlines, interior gaps, empty denominators, class/split separation,
export gates and agreement between the live and saved report.

## Group review workspace — 23 September 2026

Open **Groups** above the scene or **Manage this group** in the object inspector.
Choose a group, click image outlines or use list checkboxes to change membership.
The local class, membership and search filters support bulk additions/removals
with visible counts. Moving an object to a group removes its previous affiliation.
Create and rename groups, or **Remove group** to ungroup its objects. These actions
preserve each object's outline, class review, estimated geometry and physical
parent. Rejected/replaced objects stay in history; removing a group also clears
their current group references. Use **Undo edit** or **Save scene** as usual.

**Prediction**, **Your edits** and **Compare** keep model and reviewer groups
separate. The prediction view is read-only, and reviewer-created groups
explicitly have no model counterpart. Duplicate
group names include a short identity in the selector. Layer checkboxes affect the image
and 3D preview; group list filters only narrow the list. **Inspect** opens one
object for detailed correction, with **Return to group** bringing it back.
**Group 3D** frames and isolates the group's members. A hidden parent remains
available for fitting its solar panels; it is not drawn as an extra member.
**Show surroundings** restores the broader scene. Grouping does not establish
physical attachment or measured height.

Matching-image feedback recovery retains group names and removals. Deleted
groups are not recreated from the next model's stale group references.
`task verify:scene-groups WEB_PORT=3008` covers image, keyboard and bulk actions,
read-only predictions, create/rename/remove, save/reload/undo, interrupted saves,
revision conflicts, inspection return, model recovery, 3D and mobile layout.

At 65 Cambridge Road, revision 4 adds **Southern tree canopies** for the two
previously confirmed southern garden outlines. Only their group affiliation
changed; class/outline approvals, roof and solar geometry, parent relationships
and layer coverage remain as saved at revision 3. The joint 3D view is an
estimated preview, not new shape or height supervision.

## Interior gaps — 23 September 2026

SAM3 proposals retain interior rings instead of dropping the whole object when
a mask contains a gap. A materially different roof mask is retained beside an
inherited pipeline outline for review; essentially identical masks are still
deduplicated. This changes proposal handling, not detector weights.

In **Edit outline**, choose **Outer outline** or an **Interior gap** to move,
insert or remove points. **Add interior gap** supports image clicks and numeric
corner coordinates. **Fill selected gap** explicitly removes a false detector
gap after checking imagery. Gaps cannot touch the outside boundary, overlap or
contain one another. An unfinished gap blocks navigation until finished or
cancelled. Changes reopen outline and class-layer approval, invalidate retained
building roof planes, and support undo, save/reload and model-refresh recovery.

Compare overlays, area measurements, split/merge operations, roof proposals,
apex editing and 3D meshes share these boundaries. Courtyards remain open;
objects inside them are not counted as roof-supported. Cars with gaps retain a
visible-footprint preview. Trees explicitly marked **Fully visible** can render
a closed 3D crown around natural gaps, with the estimated trunk inside the
remaining canopy. Unassessed gapped trees and all hidden/clipped trees keep the
visible-footprint preview; saved crown settings are retained.
Scene JSONL stores the gap rings. COCO uses uncompressed column-major RLE for
gapped outlines, preserving background pixels; ordinary outlines keep polygon
segmentation. Schema 4 retains all previous class, revision and spatial gates.

Fresh inference on 65 Cambridge Road now retains the main mask with ten gaps
and score 0.8771. Its overlap with the separately traced reference is 0.9094 IoU.
This is one-image regression evidence, not a held-out accuracy result. The gaps
around solar/equipment still need interpretation. Its saved model and revision-3
review were preserved. Evidence: `data/references/scene-gaps-2026-09-23.json`.

`task verify:scene-gaps WEB_PORT=3008` checks editing, invalid drafts,
fill/undo, approval reset, immutable predictions, persistence, courtyard roof
proposals, apex overlays, 3D and mobile layout. Geometry and Python tests also
check exact coverage, shell closure, roof support, split/merge and exported pixels.

## Supporting roofs and object relationships — 23 September 2026

The object inspector separates containment in the building outline from coverage
by its roof faces. Roof following failures name the cause: an outline overhang,
a gap, overlapping faces or invalid support. Older roof triangles cannot override
an edited building outline. Percentages describe geometric coverage, not
detection confidence, height accuracy or ownership.

Rooftop parent choices list active buildings, ranked by outline containment.
Self and descendant choices are excluded to prevent parent loops. **Inspect
parent** opens the supporting object and its attached objects; **Return to**
restores the original review filters and comparison view. Draft edits survive
these changes of selection. Related navigation brings the image into view,
including for keyboard users. Prediction relationships remain read-only.

An independent Shapely audit of ten confirmed rooftop objects with building
parents matched the browser geometry calculations within 0.000001 m². Eight have
complete support. The two reviewed solar banks at 65 Cambridge Road expose
approximately 0.07 and 0.95 m² outside their inherited parent outline. That
combined estimate was subsequently superseded at revision 3 by separate main and
northern roof sections. The corrected main outline now fully supports both banks;
the canopy-obscured northern section remains **Needs edit**. Panel outlines and
their class approvals are unchanged. See the correction evidence below. No fitted weights or measured roof geometry are claimed by this work.

`task verify:scene-relationships WEB_PORT=3008` exercises ranked choices,
diagnostics, parent/child navigation, draft retention, saved reassignment,
prediction comparison, independent 2D approvals and mobile layout.

## Image correction at 65 Cambridge Road

Scene revision 3 replaces the inherited combined outline with an image-traced
main roof and a separate northern section in the same group. The main perimeter
is confirmed as an image-based reference; the northern canopy edge remains
unresolved. The old estimate is superseded, not labelled as a false positive.
Both solar-bank outlines are unchanged and now attach to the main roof.

The image-guided structural proposal has 13 faces and passes the independent
geometry audit. Both banks have zero unsupported area and each fits a single
roof plane within numerical tolerance. Family, apexes, eave height and pitch
remain unverified estimates and are excluded from outline training targets.
Building-layer COCO remains withheld until all buildings in the image are reviewed.

Evidence: `data/references/roof-30381-correction-2026-09-23.json` and the matching
outline/shape overlay PNGs. The raw SAM3 roof mask was present (score 0.8771), but
ten interior rings around rooftop equipment caused `extract_objects` to discard
it. The interior-gap changes above fix that omission without globally filling
gaps. The preserved raw mask remains a regression case for this behavior.

## Dataset and review workflow

Open `/scenes?scene=roof_training_250-29790`, or choose **Review scene objects**
in the roof workspace. The set now contains thirteen captured address images and
a Cambridge Road map region: 580 original proposals across fourteen images,
plus four added or replacement outlines (one original is superseded). Fifty-one class/outline references and
35 class-specific rejections are saved separately from the model snapshots.
These are Codex annotations against captured imagery, not measured height or
3D-shape targets. Counts include repeated views of some physical objects.

The five validation captures add 23 confirmed outlines, 19 rejected class
proposals and 30 explicit correction tasks. Three outlines were edited: two
solar banks at address 30381 and the shared edge of adjacent cars at 25299.
An uncertain missed yard object at 25315 prevents a false empty-car label.
Ten tree canopies have individual references; their image layers remain incomplete.

Export `5e0f94c9aa567d83` has 20 masks across 14 checked image layers: seven road,
nine car and four solar masks. Validation contributes six road masks across four
images, five car masks in one image, and two solar masks across three images.
The road/solar image counts include two checked empty images each. Entire images
are checked against geographic splits; three earlier test captures cross into
training areas and are now withheld while their references remain saved. Solar
currently has two training masks, two validation masks and no eligible positive
test mask. These are seed labels, not a sufficient dataset for robust retraining.

## Capture a map area

Choose **Review scene area** on the main map, then drag a rectangle. The capture
form also accepts latitude, longitude and a 10–250 metre width, so map dragging
is optional. Choose Google or ESRI imagery and either **Capture and find objects**
or **Capture for manual annotation**. A square image adds context around a
rectangular selection without stretching it. Smaller areas retain more detail.

`task scene:extract CLI_ARGS="--bbox 145.3121 -37.7908 145.313 -37.7900"`
captures the Cambridge Road region. `--provider esri` changes imagery;
`--capture-only` skips model inference. Re-run object detection on its saved image
with `--scene-id map-a31010822250fb97`, or **Find objects** in the scene workspace.
Missing source tiles fail the capture rather than introducing blank image patches.

## Review and correction

- The progress strip separates **Unreviewed**, **Needs edit**, **Confirmed** and
  **Rejected** counts. Needs-edit objects still require work; they do not count
  as resolved. Layer counts show active objects, excluding rejected detections
  and replaced source outlines. The corresponding history filters expose those
  other counts. Pending changes are labelled **Unsaved counts**.
- **Next image** uses the current class, review status and search across saved
  scenes, skips images without matches, and opens the matching object. It wraps
  around the dataset and flags images with newer model estimates. Switching
  images requires saving/discarding drafts and finishing open outline operations.
  A queue-loading error has its own retry and leaves the current editor usable.
  Swipes advance within the image; changing images remains an explicit action.
  The URL retains filters and an `object` ID so a link reopens the same selection.
- Swipe right / **Looks right** to confirm an object's class and image outline.
  Swipe left / **Needs edit** to put it in the correction queue. Arrow keys work
  outside input fields in review mode. Neither decision verifies its height.
- Use **Prediction**, **Your edits**, and **Compare** to distinguish the original
  model snapshot from saved or pending corrections.
- **Image visibility** records fully visible, partly hidden, clipped by the image,
  or both hidden and clipped. **Not assessed** stays distinct from fully visible.
  The target outline always describes visible pixels, without filling hidden or
  off-image parts. An unassessed outline touching the image border gets a review
  cue; this heuristic never creates a saved visibility label. Partial cars and
  trees show a flat visible footprint in 3D, withholding a complete cabin or
  canopy/trunk fit. The inspector hides those shape controls while retaining all
  saved settings. Their height is labelled **Stored height**: edits are saved but
  do not add height to this footprint preview. Marking the extent fully visible
  restores the shape controls. Prediction
  comparison, undo and history retain their usual meanings. Visibility feedback
  survives same-image model recovery; split/merge replacements need a new check.
  It does not revoke an already approved visible outline or imply measured height.
- **Edit outline** exposes draggable points, keyboard nudges, numeric coordinates,
  and point insertion/removal. **Focus object** crops the view around the object.
- **Add object** draws a missed object in any of the supported classes. Assign
  groups and parents in the inspector; record notes, estimated height, base
  elevation, class, and surface colour.
- **Reject detection** rejects the proposed class, including wrong-class or
  duplicate detections. Notes should explain the reason and identify a retained
  object where relevant. The card always labels the original model class and
  score; a corrected footpath is not shown with an invented footpath confidence.
  Roads describe through carriageways/junctions, driveways cover site access and
  parking aprons, and footpaths cover visible pedestrian surfaces. Occluded
  continuations remain outside visible-surface labels.
- **Correct outlines** corrects instance boundaries. A two-point cut can
  separate a merged detection; its endpoints support pointer and numeric entry.
  Merge joins two to eight touching/overlapping outlines of the same class.
  Exact split and merge preserve source coverage and interior gaps; tiny pieces
  and overly complex results are refused. **Trace replacement** lets a reviewer
  select up to 64 fragments of the same class and draw one visible boundary.
  The fragments may be disconnected, but the traced outline must overlap each
  selection. Pointer and coordinate entry, undo-corner and clear controls are
  available. **Select this group** selects its active same-class fragments.
  Check interior gaps in Edit outline after tracing; they are not copied from
  the old fragments. Separate physical objects should retain separate outlines.
  Nothing is saved until **Save scene**.
  The source outlines enter **Replaced outlines** with links to their replacements;
  they remain available in history and are not false-positive labels. New objects
  return to **Needs edit**, with new identities and source links. Attached objects
  move to a new parent only when their whole outline fits; ambiguous children
  lose the old parent and receive a correction note. Roof surfaces are rebuilt
  after a boundary replacement. Undo/discard and saved history restore the full
  set of outlines and parent links. Swiping is disabled in this editor. An
  unfinished trace also guards image/object/layer navigation, header links and
  saving; **Cancel trace** releases it without changing saved annotations.
- Trees expose **Crown shape** (rounded, conical or spreading) and **Crown base
  above ground**. The base is stored as a proportion of total height, so height
  corrections preserve crown proportions. The crown follows a closed
  triangulation of its exact outline, including concave notches and interior
  gaps on fully visible trees. An estimated trunk is placed inside the remaining
  canopy, with clearance from both outer and gap boundaries. These are editable estimates, not measured
  tree structure or botanical classifications.
- In **3D scene**, **Focus object** frames the selected object and follows later
  selections; **Whole scene** restores context. Geometry edits keep the camera
  steady. Rotate/zoom controls and mouse navigation remain available.
  **Isolate object** hides surrounding geometry while retaining the selected
  object's attached children. **Show surroundings** or **Whole scene** restores
  context. Hidden objects cannot intercept 3D selection clicks.
- Cars expose **Vehicle shape**: car, SUV/estate and van body styles, front
  bearing, cabin length/width and cabin position. **Flip front** resolves the
  two possible directions of an outline; **Follow outline** restores the
  outline-derived axis. A white arrow in the aerial view shows the assumed front.
  Cabin placement uses the oriented outline bounds, not average vertex position.
  The sloped cabin shell fits inside the outline; an explicit notice explains
  reductions or a missing fit. Shape changes, saved history and same-image
  recovery retain these estimates without approving class, outline or height.
  Changing class removes incompatible vehicle settings; split/merge replacements
  start with fresh vehicle proportions. The prediction inspector and image crop
  use original values; added/replacement objects have no original prediction.
- Buildings with roof surfaces expose **Edit roof apexes**. An outline change
  invalidates those old roof surfaces: use **Create roof shape** to propose a
  replacement. Newly detected or manually drawn buildings support the same flow.
  Choose gabled, hipped or flat, or **Compare imagery** to compare hip/gable ends
  using the existing colour/line evidence and optional validated roof-end model.
  Geometry must pass independent coverage, overlap, planarity and drainage checks.
  Dense pixel stair-steps can use a bounded support envelope to fit fewer planes;
  every resulting face is clipped back to the exact saved outline and audited.
  The apex editor opens with the roof enlarged. **Whole image** restores context;
  toggling the view preserves the image-to-metre frame used by pointer edits.
  Proposals preserve the drawn outline and remain unsaved drafts until **Save
  scene**. Uniform colour does not confirm roof form; ambiguous image evidence is
  labelled explicitly. **Try another roof shape**, Undo and history allow revisions.
  Gabled templates create actual gable faces and editable ridges, not just a label.
  The original prediction stays separate; when it has no roof surfaces the
  prediction view says so. The separate roof workspace remains the primary
  workflow for verified roof-shape targets.
- Roads, driveways and footpaths expose **Surface slope**: direction, longitudinal
  grade, cross slope or a raised centre. Positive grade rises along the aerial
  arrow; positive cross slope raises its left side. A raised centre lowers both
  sides from a straight axis. **Level surface** resets both slopes; **Follow
  outline direction** restores the automatic axis. The preview retains the drawn
  outline, constant vertical thickness and a closed underside. Base elevation
  means the lowest underside; the inspector shows the resulting top range.
  These are local estimates: neighbouring ground and objects do not move with
  them. Split/merge replacements clear the former slope profile because the
  outline extent and base reference change. Original settings remain in history.
  Editing 3D settings retains an existing class/outline approval; editing the
  outline or class clears layer approval. Neither action verifies 3D geometry.
- Solar arrays and skylights can **Follow parent roof**. Solar banks use one flat
  mounting plane estimated from the current roof beneath the whole outline.
  **Solar mounting plane** edits tilt, downhill direction and minimum clearance;
  the aerial arrow points downhill. The fit uses surface area, so extra roof
  triangles do not bias its slope. The plane clears the highest supporting roof
  point, including interior ridges. The inspector shows the bank's underside
  range and the estimated minimum/maximum gap above the roof. A variation above
  0.15 m prompts roof/mounting review; this is a modelling cue, not a construction
  standard. Different roof slopes may need separate banks or a corrected roof.
  **Fit parent roof plane** restores the automatic slope; **Level bank** keeps a
  horizontal plane. Turning off roof following permits independent tilt, with
  base elevation setting the lowest underside. Clearance is retained for later
  roof following. Skylights retain the existing roof-surface fit. A
  building without faces supplies its estimated flat top. An unsupported outline,
  overlapping roof faces or missing/rejected parent keeps the independent base
  elevation and displays a correction hint; no part of the outline is silently
  removed. Disable roof following to enter a manual base elevation. Placement and
  vertical thickness remain estimates, not verified height labels. Saved history
  and same-image model recovery preserve mounting feedback; changing class or
  splitting/merging banks clears incompatible settings. Mounting edits preserve
  existing 2D approvals and do not add measured 3D training targets.
- For solar labels, trace the visible photovoltaic bank, including frames and
  narrow internal module spacing. Broad exposed roof strips separate banks.
  Glazing, thermal collectors and ordinary roof material can look similar in
  low-resolution imagery; unresolved types belong in **Needs edit**. A complete
  layer check includes missed equipment and clipped image edges. Correct 2D solar
  labels can remain eligible while the hosting building outline or height still
  requires correction.
- Decisions save before advancing. **Undo last save** creates another revision;
  **Undo edit**, **Discard draft**, and saved history provide local recovery.
  Concurrent saves are rejected with the draft retained.
- Layer review coverage requires checking for missed objects as well as resolving
  every proposal. An explicitly reviewed empty layer is a valid negative image.

## Extraction and provenance

`task scene:extract CLI_ARGS="--cohort roof_training_250 --limit 6"` uses SAM3
text prompts for buildings, trees, roads, cars, solar panels, pools, driveways,
footpaths, grass, and water. The UI can extract an existing captured address
or an arbitrary map region and request an additional named object. Skylights and chimneys currently support
manual annotation in this scene workflow; the roof workflow also has its own
rooftop-object detector.

Images are content-addressed. `data/scenes/<scene>/models/<hash>.json` retains
model snapshots, `latest.json` points to the latest extraction, and `review.json`
retains corrections and revision history. Re-extraction does not overwrite
reviews. Changed model signatures require a fresh review. A process lock prevents
concurrent scene extraction workers from loading duplicate models.

When a new model uses exactly the same image and coordinate frame, **Preview
earlier edits** compares preserved corrections with its proposals. **Carry edits
forward** saves a new revision containing edited outlines, apexes, notes, groups,
manual objects and required parent context. Carried outlines need another check;
layer approvals reset. New detections with different IDs remain visible so the
reviewer can resolve duplicates. If imagery/frame changes or the old snapshot
cannot be verified, automatic carry-forward is unavailable and history is kept.
Concurrent attempts still require matching model and review revisions.

Address-image frames are fitted from the existing roof report. Map captures use
the selected geographic bounds and a north-up local metre frame. Existing CRM
roof corrections with passing structural audits are reprojected into this frame,
including gabled ridge/apex geometry; they remain review candidates on the new
image. Width, length and area
are derived from that frame and the edited outline. Heights are class or roof
model estimates, not measurements. Trees use editable canopy profiles; cars use
fitted cabin shells and editable proportions; buildings without roof surfaces use prisms. These previews are
useful for feedback but are not evidence of accurate depth reconstruction.

The 23 September depth probe sampled 61 objects in three existing scene images
(`data/references/scene-depth-probe-2026-09-23.json`). The local Depth Anything V2
small model returns a relative proxy. A local ground-plane fit reduced broad
image gradients, but existing roof-height priors were not a reliable metre
reference. No probe heights were written into reviews or training labels.
Relative and metric models are separate releases in the
[upstream documentation](https://github.com/DepthAnything/Depth-Anything-V2/blob/main/metric_depth/README.md).
This probe supplies diagnostics, not a measured-height evaluation or a trained
scene-height model.

## Training feedback

`task scene:training` writes versioned JSONL and per-class COCO datasets under
`data/training/scenes/<version>/`. It verifies image hashes and source-model
signatures, keeps predictions separate from references, and preserves the roof
cohort's spatial split. Map regions use the same spatial assignment. Regions that
cross different split assignments remain in the JSONL audit and are excluded
from COCO until assigned a safe split. Only confirmed class/outline pairs become references.
COCO includes only fully reviewed layers. Rejected proposals remain explicit
false positives; unreviewed predictions, estimated heights and apexes do not
become scene outline training targets. Exporting does not update detector weights.
JSONL references retain explicit visibility, and COCO annotations include visible
outline semantics plus `full`, `occluded`, `truncated`, `occluded_truncated` or
`unassessed` visibility attributes. A partial mask is never an amodal/full-body
target. Edge-touch hints are not exported as reviewer assessments.
Replaced source outlines are excluded from both targets and false positives.
The JSONL retains replacement links; replacement objects need their own outline
review before becoming labels. Same-image model recovery preserves this history.

`task verify:scenes WEB_PORT=3008` checks persistence, validation, revision
conflicts and browser interactions using temporary scenes. `task test` also
checks geometry and Python extraction/export gates. Test scene directories use
`qa_` and are omitted from the user scene list and training exports.
`task verify:scene-region WEB_PORT=3008` also exercises map rectangle selection,
real imagery capture, live object extraction and saving a manually drawn object.
`task verify:scene-roof WEB_PORT=3008` exercises new roof creation, decimal apex
entry, save/reload, history and immutable model comparison using a temporary scene.
`task verify:scene-instances WEB_PORT=3008` exercises pointer splitting, merging,
draft/undo/save/reload, replacement history and attached-object parent changes.
`task verify:scene-car WEB_PORT=3008` exercises vehicle style, decimal proportions,
front arrows/heading, original-model comparison, undo and interrupted-save recovery,
save/reload, class changes, same-image model recovery, 3D focus and narrow layouts.
`task verify:scene-surface WEB_PORT=3008` exercises decimal slope controls,
raised centres, prediction comparison, independent outline approvals, failed-save
recovery, undo, class changes, same-image recovery and mobile/3D views.
`task verify:scene-visibility WEB_PORT=3008` exercises partial car/tree previews,
visibility controls, edge hints, preserved shape settings, immutable prediction,
outline approvals, save/undo/recovery and narrow layouts.
`task verify:scene-queue WEB_PORT=3008` exercises filtered cross-image navigation,
object links, progress counts, draft protection, save/undo, queue errors and
out-of-order responses using isolated images.
`task verify:scene-solar WEB_PORT=3008` exercises mounting tilt, direction,
clearance, warnings, comparison, independent elevation, save/recovery and mobile.

## Remaining model work

Overlapping class proposals and false positives remain visible for correction.
Depth calibration, individual solar-panel orientation/spacing,
scene corrections on the main map, and supervised retraining/evaluation from
larger reviewed scene datasets remain work in progress. An experimental learned
proposal-quality scorer is now available below; the production detector is
unchanged. General scene accuracy and measured 3D accuracy remain unestablished.

## Frozen prompt experiments

For the first learned solar proposal filter, its small-cohort results and
reproduction commands, see [Learning from scene corrections](scene-proposal-training.md).

`task scene:experiment CLI_ARGS="--dataset data/training/scenes/VERSION --kind solar_panel --prompt 'solar panel' --prompt 'solar panel array'"`
compares candidate text prompts against one immutable reviewed export. It uses
the production mask filtering, gap preservation, deduplication and polygon
matching. Only eligible training and validation layers are processed; test
images are excluded. Image hashes, saved revisions, class completion and COCO
eligibility are checked before inference.

One image encoding is reused across prompts. Each run records the exact model
component hashes, relevant code hashes and prompt settings under
`data/experiments/scenes/RUN/`. Per-image predictions are saved separately from
the CRM and can be resumed under the same configuration. The shared extraction
lock prevents concurrent heavyweight model loads. `report.json` compares each
prompt with the saved baseline and ranks candidates by validation instance F1,
then mean outline IoU. A validation split without positive references is not
ranked. This is an experiment, not weight training or automatic promotion; a
small reviewed cohort cannot establish general accuracy.

## Tree label review — 23 September 2026

Five existing images were reviewed against their complete imagery and tree
outline overlays. Twelve additional visible-canopy references were confirmed.
The tree layers at 95 Cambridge Road, 66 Ervin Road and 70–72 Cambridge Road are
complete, making 13 masks exportable: five training, four validation and four
test masks, each split currently represented by one image and one area group.
Border canopies explicitly retain truncated visibility. The eastern and western
canopies at 95 Cambridge Road also have editable review groups.

At 39 Ballantyne Crescent, two separate crowns were confirmed while three
clipped or merged masks received specific correction notes. At 52 Marion
Avenue, two missed canopy regions received editable image-traced drafts. Both
of these image layers remain incomplete and are excluded from COCO training.
Original predictions, other object classes and existing height estimates were
preserved. Audit: `data/references/scene-tree-review-2026-09-23.json`.
Export `fa15a81e943686de` contains 72 confirmed references overall, of which 39
masks across 26 complete image layers are exportable. These are agent-reviewed
image references, not independently measured ground truth.

## Verification on 22–23 September 2026

- `task test`: Go suites, 216 Python tests, TypeScript, roof storage/geometry and
  scene geometry checks passed.
- `task lint`: passed with 31 frontend warnings and no errors; Python lint and
  Go vet passed.
- `pnpm build`: production build passed.
- `task verify:scene-solar WEB_PORT=3008`: decimal tilt, downhill bearing and
  clearance, aerial arrows, roof mismatch warnings, immutable prediction,
  independent elevation, approval preservation, failed-save recovery,
  save/reload, undo, class changes, same-image recovery, 3D and mobile passed.
  Geometry checks cover roof clearance at interior ridges, a single mounting
  plane, concave closed meshes and invariance to roof tessellation. API checks
  reject invalid, incomplete and wrong-class mounting parameters.
- `task verify:scene-queue WEB_PORT=3008`: progress and active layer counts,
  keyboard navigation across images, exact-object links/reload, draft and failed
  save protection, save/undo counts, empty/error recovery, stale-response
  protection and the 390px layout passed. API checks compare filtered summaries
  with saved real-scene objects; replaced sources remain history-only.
- `task verify:ui WEB_PORT=3008`: 24 tile requests, studio opened, no page errors.
- `task verify:reviews WEB_PORT=3008`: roof editor and feedback API passed.
- `task verify:scene-roof WEB_PORT=3008`: gable creation, focused-view pointer
  registration, decimal apex entry, model comparison, save/reload/history and
  invalid/stale request handling passed.
- `task verify:scene-instances WEB_PORT=3008`: pointer split, invalid-cut
  recovery, unsaved drafts, merge, undo selection, read-only source history,
  attached-object reparenting, save/reload and 3D passed. API checks reject stale
  revisions, mixed classes and broken lineage. A replacement building can still
  obtain a roof proposal. Replaced outlines stay out of false-positive exports.
- `task verify:scene-car WEB_PORT=3008`: keyboard field navigation, vehicle-style
  and decimal proportion edits, front arrows/flip, original prediction values,
  undo, interrupted-save recovery, save/reload, class changes and same-image
  feedback recovery passed. Object isolation, restoration of surroundings and
  the 390px inspector also passed. Temporary annotations were removed afterward.
- `task verify:scene-surface WEB_PORT=3008`: decimal grade/crossfall/bearing,
  raised centres, direction arrows, top elevation ranges and immutable prediction
  values passed. Save/reload, failed-save recovery, undo, class changes and
  same-image carry-forward passed. 3D-only edits retain complete outline/layer
  approval; outline edits revoke it. The 3D view and 390px controls passed.
  An independent polygon audit exercised 300 planar/crowned variants on all 50
  active captured road, driveway and footpath objects. Projected spill, gaps and
  overlap stayed below 1e-7 m², with closed mesh edges. A reduced captured
  footpath is a regression fixture for near-collinear internal seams. No trial
  slope values were saved to real annotations; these checks do not verify grade.
- `task verify:scene-visibility WEB_PORT=3008`: explicit/unknown visibility,
  edge cues, partial car/tree footprints, retained shape settings and independent
  outline approval passed. Prediction comparison, failed-save recovery,
  save/reload, undo, same-image recovery and the 390px layout passed. Synthetic
  geometry checks verify flat partial previews and an explicit full-visibility
  override; export tests verify unchanged visible masks, visibility metadata and
  no inferred hidden/3D targets.
- `task verify:scenes WEB_PORT=3008`: pointer swipe, undo, decimal outline/height
  edits, interrupted-save recovery, persistence, 3D canvas, search and mobile
  layer disclosure passed. API checks also cover stale revisions/models,
  self-crossing outlines, parent cycles, complete-layer gates, origins and
  corrupt-file preservation. Same-image correction recovery preserves edits and
  history, clears approval, and refuses changed image/coordinate frames.
  Rooftop placement toggles persist across save/reload; geometry checks verify
  full-footprint support, ridge crossings, apex updates and manual fallback.
  Tree-profile and decimal crown-base edits also survive save/reload, while 3D
  focus frames the selected object and returns to the whole scene.
- The independent tree geometry check in
  `data/reviews/scene-trees-2026-09-23.json` covers all 39 trees in the Cambridge
  Road scene: outline spill, missing coverage and overlap are below 1e-7 m²;
  every estimated trunk lies inside its canopy. The most affected old radial
  construction spilled about 11.7 m² across a concave notch. Synthetic checks
  also verify closed crown shells, both polygon windings and edited heights.
- Fully visible canopies retain interior gaps in their closed 3D crowns. The
  independent audit in `data/references/scene-tree-gaps-2026-09-23.json` covers
  all 259 active captured trees, including eight with gaps: projected spill, missing area and overlap are
  below 1e-7 m², every shell edge has two incident triangles, and each trunk
  cross-section lies within the remaining canopy. Near-collinear pixel edges
  are reconciled before subdivision to avoid cracks and duplicate fins.
  The eight gapped detections touch image edges and retain their existing partial
  previews; the audit does not approve their visibility or 3D accuracy.
  Regression tests include a reduced captured outline, both windings, touching
  gaps and all three crown profiles. `verify:scene-visibility` covers the full
  gapped-crown controls, prediction comparison, save/reload, undo and recovery.
- An independent polygon-intersection audit of all 13 original car proposals found that
  12 previous cabin boxes extended beyond their outlines (worst: 0.496 m²).
  The fitted shells have less than 1e-7 m² projected spill or missing cabin
  coverage on those same outlines. Synthetic checks cover uneven vertex density,
  both windings, concave notches, closed shells, height/base bounds and front flips.
  This is a geometry check; vehicle style, front direction and height remain
  unverified estimates.
- `task verify:scene-region WEB_PORT=3008`: real map selection, capture, 56 live
  object proposals on the temporary area, outline tapping, full-image drawing and
  saved manual annotations passed. Test scenes were removed afterward.
- The retained Cambridge Road area contains 104 original proposals, including
  two reprojected CRM roofs. Building `bc814d09fe3ebbdf` now has a saved 17-plane
  image-guided starting shape and explicit ridge/valley correction notes, at
  review revision 1 with **Needs edit** status. Its original 70-point outline is
  unchanged. A direct unsmoothed prior had produced 70 planes on that outline.
  Review revision 2 adds a surface-class pass: one visible carriageway, five
  pedestrian strips and four driveway/access surfaces are image-reviewed.
  Four overlapping road proposals, one driveway proposal on a pedestrian strip
  and one footpath proposal on a fence strip are rejected for their proposed
  classes. A pale crossover is reclassified as a driveway with its boundary
  still requiring review. Every decision records the source image and reviewer
  in its notes. The original outlines, model/image bytes, roof correction and
  unrelated objects are unchanged. Only the road layer is marked complete.
  Scene export `c88686d6027061ac` records seven scenes, 307 original proposals,
  10 image-reviewed targets and six class rejections. Its road COCO file has one
  image and one visible-carriageway annotation in the existing training split.
  Other class layers remain incomplete, and 3D estimates are excluded from
  targets. Exporting does not train model weights.
- The vehicle pass saves Cambridge Road revision 3 and revision 1 on the six
  address scenes. The model/image files, every existing polygon and unrelated
  review feedback are preserved; save-time derived dimensions are recalculated.
  Notes identify the source image and the reason for every decision. The white
  vehicle at the map image edge is explicitly truncated. Four small roadside
  objects are rejected as cars without guessing their exact replacement class;
  four blurred or partly hidden cases stay in Needs edit. One elongated white
  vehicle uses an explicitly estimated van preview. Export `99ecb4af6142d4f3`
  contains 15 references and 10 rejections. Car COCO has three images/four masks:
  the map and address 29790 share the training spatial group, and address 25120
  stays in the test group. A fifth confirmed vehicle outline remains in JSONL
  because another car proposal in its image still needs review. Visibility is
  explicit, partial masks remain partial, and estimated heights/shapes are absent
  from targets. No model weights were fitted or promoted.
- Premium UI strict audit: no findings. DESIGN.md lint: no errors, three unused
  token-reference warnings.

The solar pass is recorded in `data/references/scene-solar-2026-09-23.json`, with
source hashes, before/after annotations, reasons and saved revisions. Four
outlines changed: a missed portion of the Cambridge Road panel bank and three
rectangular rows at address 25136. Twelve captured banks are confirmed, six
plain-roof/awning detections are rejected for the solar class, and six ambiguous
predictions plus one newly found edge object remain unresolved. Two views of the
Cambridge Road bank retain the same training spatial group. Original imagery,
model snapshots and unrelated feedback are preserved.

Addresses 29676 and 30383 add 18 building/solar proposals in two existing training
groups. Their entire image frames were checked to remain in training geography.
Four visible banks are confirmed; 30383 stays out of complete-layer COCO because
its missed, clipped roof equipment still needs classification. Export
`8309f34c634faad7` contains nine scenes, 27 references and 16 rejections. Its solar
COCO file has one training image/two masks, four test images/four masks (including
two valid negative images), and no validation image. Estimated 3D properties and
all unresolved objects are excluded from positive targets. No weights were fitted.
Three of the first eight confirmed banks still lack usable host-roof geometry;
the saved mount audit records the explicit base-elevation fallback.
The focused 29676 preview exposed a bank bending across several estimated roof
faces. `scene-solar.ts` now fits one mounting plane and reports the remaining
roof mismatch. The default fit on that image still spans about 0.74 m of roof
variation; a planar preview does not establish that the underlying roof is right.
The independent polygon/plane audit in
`data/references/scene-solar-plane-2026-09-23.json` exercises automatic, manual
roof-relative and independent mounting on all 12 confirmed captured banks.
All 28 supported/independent meshes are planar, closed and preserve their exact
projected outlines, with spill/gaps/overlap below 1e-7 m² and plane residual below
3e-13 m. Roof-relative planes clear the entire current support surface. Eight
unsupported variants retain explicit fallback hints. No trial tilt or clearance
values were saved to real annotations.

These checks establish workflow and geometry invariants, not detector accuracy.

## Training coverage and export

Open `/scenes/training` or **Training coverage** in the scene header. The table
shows eligible image layers, masks and distinct spatial groups for each class
and split. Confirmed outlines can remain withheld while other objects or missed
instances in the same image are unresolved. **Review layer** opens the pending
object directly, with all statuses visible. Filters survive reloads and links.
A completed scan is not a whole-image review; zero detections do not establish a
negative label. Unscanned classes remain explicit, including image-only or
partially scanned captures.

**Export reviewed dataset** uses the same eligibility calculation as the
coverage view. It creates versioned JSONL/COCO under `data/training/scenes`,
with per-class split counts in the manifest. It requires object-editing access
and does not fit detector weights. Unknown splits, missing spatial groups,
conflicting assignments of the same group or image, stale reviews, unchecked
confirmed outlines and incomplete layers are withheld. Earlier versions remain
available; schema version 3 added a whole-image geographic split audit. Schema 4
preserves reviewed interior gaps in JSONL and COCO masks.

`task verify:scene-training WEB_PORT=3008` covers navigation, exact-object links,
filters, read-only coverage, loading/empty/errors, permission failures, export
feedback, keyboard access and mobile layout. Python training tests check that
coverage and exported masks agree, including checked negatives and split gates.


## Validation annotation and full-image split audit (23 September 2026)

`data/references/scene-validation-2026-09-23.json` records 72 image-review
decisions, before/after polygons, source hashes, saved revisions and geographic
checks for 25096, 25325, 30381, 25299 and 25315. Their 255 proposals cover all ten
extractable classes. Ambiguous roof grids, vehicle-storage objects and access
surfaces remain Needs edit. The two solar banks at 30381 were traced to visible
straight module edges and their stepped ends. At 25299, adjacent cars now have
separate visible masks; street components retain their occlusion gaps and share
a reviewer-created group. Heights and existing roof proposals were preserved.

`scene_split.py` projects the entire pixel frame using its local metre transform
and centroid, then intersects it with the established z17 split tiles. Multiple
tiles are allowed when they share the assigned split. Empty corners of a rotated
bounding box and zero-area edge touches are excluded. Invalid frames, geographic
mismatches and cross-split imagery are withheld from COCO; individual references
and their original split assignments remain in the JSONL audit. The CRM shows
the reason under Review provenance. This catches the previously unguarded
neighbouring context in address captures 25111, 25123 and 25136.

`test_scene_split.py` verifies the whole-frame policy, including boundary touches,
small real crossings, rotated frames and invalid transforms. The training tests
also verify preserved references and withheld positive/empty layers. Neither
annotation export nor this split correction changes detector weights.

## Point-contact validation in captured masks (23 September 2026)

A solar annotation pass exposed three valid model polygons that could not be
saved: two had gaps touching at one vertex, and one vegetation mask had a gap
meeting the outer boundary at a vertex. The TypeScript validator now accepts
isolated point contacts, matching the geometry used by extraction and export.
It still rejects crossings, shared edges, nested/overlapping gaps and contact
chains or cycles that disconnect the interior. A contact graph uses distinct
contact points, so several gaps meeting at the same point remain valid. These
rules follow the [JTS polygon validity definition](https://locationtech.github.io/jts/javadoc/org/locationtech/jts/geom/Polygon.html).
No original prediction rings or gaps were changed to get these saves through.

`test-scene-geometry.mjs` covers shell/gap contacts, multiple gaps sharing a
point, reversed winding, exact triangulated area and empty gap interiors, and
invalid disconnected examples. `test-scenes-api.mjs` checks save/reload without
mask mutation and rejects a disconnected mask. A separate differential audit
agreed with Shapely on 3,000 seeded synthetic cases and 1,037 captured object
polygons; it found no mismatches. This validates polygon handling, not object
class accuracy or measured heights.

## Expanded solar training review (23 September 2026)

Twelve further captures from `roof_training_250` were extracted across ten
object classes: addresses 25113, 25154, 25173, 25183, 25221, 25238, 25247,
25801, 25639, 29854, 30236 and 30362. Their complete image frames pass the
training-area split audit. They add 561 original proposals; the scene workspace
now has 26 images and 1,141 original proposals. The separate roof capture cohort
still has 250 images; these counts do not mean 250 verified roof labels.

Solar review, saved as revision 1 in each new scene:

- 25113, 25154, 25173, 25238 and 25247: five checked negative images containing
  22 rejected awning, sheet-roof, tiled-roof, rooftop-equipment or construction
  detections. Even the 0.8581-confidence roof-edge strip in 25173 is a false
  positive, so a confidence cutoff alone would not solve these errors.
- 25183: one visible bank confirmed on the southeast neighbouring roof.
- 25639, 29854 and 30362: five existing corrected solar bank outlines reprojected
  from roof dataset `a051b9aecb3984e7` and visually checked in these images.
  25639's extra mask is a duplicate module wholly inside the retained bank;
  29854's extra mask is a pale slatted awning/pergola. Both extras were rejected
  with specific notes. These three full solar layers are complete.
- 30236: three legacy array outlines retained, but the shaded fourth proposal
  remains unresolved. All three references are withheld from dense training
  until the whole solar layer is resolved.
- 25221: ten small features on the upper green roof remain ambiguous between
  skylights and solar modules. No positive or negative class labels were forced.
- 25801: 26 true module fragments are grouped into western, eastern and
  edge-clipped southern banks for further tracing. They remain `needs_adjustment`;
  array-bank and individual-module targets must not be mixed. They were not
  rejected merely because their granularity differs from the intended array mask.

The pass saves nine positive references (eight reused observations and one newly
reviewed outline), 24 rejections and 37 pending proposals. Notes identify the
agent image review and legacy reference IDs. Original model snapshots, mask gaps,
non-solar class decisions and height estimates are retained. Audit:
`data/references/scene-solar-review-2026-09-23.json`.

Export `4578648baa88c4c7` has 60 confirmed objects and 59 rejections across all
classes. Its 23 complete class/image layers contain 26 eligible masks. Solar
training comprises ten images from seven spatial groups: eight array masks and
five checked negative images. Solar validation remains three images/two masks;
the one eligible test image is still a negative, so positive test recall cannot
be measured. All held-out class metrics and denominators are unchanged from
`5e0f94c9aa567d83`. No detector weights were fitted or promoted by this review.

Verification: `task test` (258 Python tests plus Go and TypeScript/geometry checks),
`task lint` (no errors; 31 existing frontend warnings), production build,
`test-scenes-api.mjs` and `verify:scene-gaps`. The saved-data audit checks exact
review fields, unchanged model file hashes and gap rings, preserved height
estimates, full-frame training geography and expected class eligibility.
`task verify:scene-training WEB_PORT=3008` and `task verify:ui WEB_PORT=3008`
also pass after the new annotations. In-app browser inspection confirmed the
corrected-vs-predicted solar overlay, revision 1 notes and the three pending
fragment groups (10 western, 7 eastern and 9 southern proposals).

## Manual fragment replacement (23 September 2026)

The **Correct outlines** panel now includes **Trace replacement**, so fragmented
arrays, trees or paved surfaces can be redrawn as one object without pretending
an exact union covers their missing portions. The two-step editor supports
pointer/numeric corners, group selection, corner undo, clear and cancellation.
Up to 64 same-class sources can be replaced; a traced boundary must overlap
all selected sources. Source history stays reciprocal and acyclic. New geometry
resets outline/visibility/height approval, extent-dependent 3D settings and
class coverage. Shared parent/group links survive; attached objects are assigned
only when fully contained. The reviewer must check interior gaps afterward.

An in-app test caught a filtered-selection bug: the visible solar card could
coexist with the initial building selection, and starting a drawing switched
objects and reset the tool. Opening Correct outlines now pins the visible card.
The browser regression opens a class-filtered image without an object link to
exercise this exact trigger.

At 11 Harley Crescent (`roof_training_250-25801`), review revision 3 contains
three image-traced bank drafts replacing 26 original module fragments (10 west,
7 east, 9 south). The western draft was created and saved through the visible
CRM controls. Eastern and southern drafts used the same validated replacement
API. The southern bank is explicitly image-clipped. All three remain
`needs_adjustment`; none are training references. Their original predictions,
non-solar annotations and saved history are preserved. Provenance:
`data/references/scene-traced-banks-2026-09-23.json`.

Export `7f1c5072ed0600af` records this revision. The scene has three pending solar
objects rather than 26; the replaced fragments enter neither reference nor
false-positive targets. All eligible evaluation layers and metrics match
`4578648baa88c4c7`. Total confirmed references (60) and rejected detections (59)
are unchanged. No model weights were updated.

Verification: `task test` (266 Python tests plus Go, TypeScript and geometry
checks), `task lint` (no errors; 31 existing warnings), production build,
`task verify:scene-instances`, `task verify:scene-trace` and `task verify:ui`.
Trace verification covers 12-source selection, pointer and numeric input,
390px mobile layout, incomplete-draft navigation guards, validation failures
with retained corners, retry, undo, save/reload, source history, 3D, cancellation,
stale revisions and invalid/mixed-class requests. The strict UI audit has no
findings. The selected in-app browser confirms revision 3, the three-object
queue and comparison with the original fragment predictions, without app
warnings/errors. Dataset checks verify unchanged original hashes, non-solar
records, evaluation targets and exclusion of all unapproved replacement masks.
The live 3D preview reports 0.25 m of estimated supporting-roof variation beneath
the western flat bank. That is a remaining shape/mounting review issue, not an
approved roof or solar elevation. The scene is left in aerial comparison mode.

## Structured correction feedback

The scene inspector's **Needs attention** checkboxes distinguish outline,
object-type and instance problems from 3D shape/apex, height and placement
problems. Use the **Feedback filter** to return to those cases across images.
Flags, notes and geometry share the ordinary draft, save, undo and revision
history. Original predictions remain separately available.

A 2D flag reopens outline review. Correct the object, clear the resolved flag,
then confirm it. A right swipe cannot confirm an outstanding 2D problem.
A 3D flag preserves a separately approved outline. Flagged heights are unverified;
clearing any flag does not create an approval or measured label. Split/merge
replacements retain flags from all sources until reviewed again.

Dataset schema 5 includes a separate `feedback` list and manifest issue counts.
These describe unresolved corrections and can guide review sampling. They are
not positive 3D targets and do not train a model on their own. The existing
complete-layer, geography, visibility and frozen-evaluation gates still apply.

## Keep attention on the object

Choose a class such as Trees, Roads, Cars or Solar arrays to start a review.
**Only review layer** hides unrelated outlines and 3D meshes. Turn it off to
restore your chosen surrounding layers; **Show all** restores every layer. This
changes the view only. The original prediction remains available in Compare
even after you correct an object's class, and hidden parent roofs still support
solar mounting estimates.

The image and swipe buttons sit together in the laptop view, with image progress
beside the details and **Next image** below the card. On a phone, **Jump to
review** moves focus and the viewport directly to the card. It preserves the
selected object, filters and any unsaved edits. Scoped 3D review initially focuses
on the selected object; use **Whole scene** to restore the surrounding view.

## First complete building layer (23 September 2026)

Scene `map-d34a1fe62200fa75`, revision 1, has six image-traced roof surfaces:
four corrected predictions and two missed roofs. Two yellow fabric shade covers
are reclassified as **Other objects** and remain pending outline/3D review.
The building class includes the permanent attached shopfront roof; this does not
establish enclosed walls beneath it. Roof fixtures and their shadows were removed
from false gap/notch predictions after image inspection. Actual courtyard gaps
must still be retained.

This 23.9 m crop overlaps `map-d43871249a7997e8` in the same training geography.
It is not six additional independent physical roofs or higher-resolution source
imagery. Two roof extents are fully visible and four are image-clipped. Only the
building layer is complete; heights, roof shapes and all other layers remain
unverified. The larger scene's revision 4 adds a pending review region for an
ambiguous stair/covered passage, preserving all 55 existing objects and its
previous class coverage.

Export `ce3af69f1a2f0263` contains the first complete building COCO image and its
six masks. The original detector matched four, missed two and proposed two extra
building masks at IoU 0.5. This one training image is not a validation benchmark.
The five existing class COCO files are byte-identical to the previous export;
roof-family/linework counts remain nine/three. Roof training run
`eebfdfc5025de3b0` reports `needs-reviewed-labels`, with no fitted model.

Audit and before/after objects:
`data/references/scene-building-layer-2026-09-23.json`.
The adjacent PNG shows the reviewed roof extents. API save/reload, exact exported
polygons, visibility, geography, unchanged detector snapshots and live CRM
**Compare** were checked. Of 310 pre-existing model/review files, only the
explicitly edited larger scene review changed.

## Compare original and edited 3D shapes

Choose **3D scene → Compare** to see current solid geometry together with the
original estimate in amber. The amber lines are X-ray lines: they remain visible
inside or behind a changed surface. This makes a changed roof ridge, tree crown,
vehicle cabin or road grade visible without switching views. Original solar
panels stay on the original roof shape, while current panels follow edited roof
support. **Prediction** and **Your edits** still show their individual versions.

Layer visibility and **Isolate object** apply to both versions. **Group 3D →
Compare** shows original geometry for the same current group members. Original
geometry does not intercept clicks or height handles. New objects have an
explicit no-prediction notice; rejected detections can show their original shape
without a current mesh or editable height handle. Comparison is a view action:
it leaves saved annotations, training labels and verification states unchanged.

## Additional gable feedback (23 September 2026)

Scene `map-f76d09b5752bb716`, revision 1, adds two fully visible sheet-metal
shop roofs with checked outlines, gable-family observations and image-XY ridge
linework. Their ridges reach both end boundaries; each editable graph has two
slopes and no hip faces. Existing eave priors remain unchanged, roof rises are
explicit display estimates, and height feedback remains unresolved. The
independent saved-annotation geometry audit passes for both graphs.

Export `38d9cc73be1e5f10` has 11 checked roof-family observations and five checked
linework graphs. Training run `7ffe9cbe4f4d5096` accepts three distinct gabled
roofs across two images/two training areas, including both new roofs. It still
reports `needs-reviewed-labels`: training requires additional examples and
validation/test family labels are absent. No weights were fitted or deployed.

These are object-specific roof observations, not a complete building-layer
review. All six class COCO files are unchanged from the previous export. All
311 pre-existing model/review files and the other 142 scene objects are preserved.
The saved aerial roof editor and 3D gable preview were checked with no browser
errors or unsaved changes. Before/after objects, independent audits and source
provenance are recorded in
`data/references/scene-gable-expansion-2026-09-23.json`; its adjacent PNG shows the
traced boundaries and ridges. Approximate 0.4 m XY uncertainty is an annotator
estimate, not surveyed accuracy.

## Broader roof-family review (23 September 2026)

Ten more checked outlines and roof-family labels are saved across nine scenes.
The three gables retain their straight end boundaries; tracing excludes adjacent
balcony floors and shadows. Seven hip labels record visible sloping end faces,
including two validation examples from different areas. Shape graphs and heights
remain flagged separately. API save/reload checks preserve every unrelated
object, existing class coverage and immutable detector snapshot.

Export `cf83b756a5a69abc` now contains 238 scenes, 249 confirmed objects, 21
roof-family observations and five checked XY graphs. This includes four new
unreviewed holdout-area captures. It does not increase the six class COCO
datasets: the edited building layers still need a complete image scan. Roof
training run `9031ad636371d19a` reports the remaining label requirements without
fitting weights; see `docs/roof-training.md`. Saved gable feedback, comparison,
revision state and training coverage were checked in the live CRM.

Audit and boundary contact sheet:
`data/references/scene-roof-balanced-review-2026-09-23.json` and its adjacent PNG.

## Held-out gable and hip examples (23 September 2026)

Six further roof outlines are saved across five new scenes in two test areas.
They add three explicit gable observations and three hip observations. The
northern metal gable (`map-5e209c5e18c637fc`, `f6c5b3bffed79f43`) has two roof
slopes meeting at a ridge that reaches both ends; the slate hip
(`map-57dff319b7cbdeb9`, `f71d6723183b7dde`) has two side planes and two
triangular hip faces. Both have checked XY graphs editable through **Edit roof
apexes** and the **3D edit handle**. Their heights remain unmeasured estimates.

Export `fc2781c8933d4443` includes 27 family observations and seven checked XY
graphs. Readiness run `1eb3f2f32b019c1f` leaves weights unchanged; additional
training/validation labels are required before fitting or opening test features.
These individual reviews leave whole-image building coverage incomplete.
Source provenance and unchanged-file checks are in
`data/references/scene-roof-holdouts-2026-09-23.json`.

## Roof-mounted solar feedback (23 September 2026)

The third metal gable (`map-1186472743a9e345`, `e6822ecc21fe1fc2`) now has a
checked two-plane ridge graph and two separately traced solar banks. The roof
and both banks belong to the same group, and each bank explicitly names its
parent roof. **Groups → Group 3D → Your edits** shows the banks on opposite
slopes; **Compare** retains the immutable detector output. Saved revision 2 has
three group members and no unsaved draft. The fourth metal gable
(`map-871e6846687c6c32`) also has checked ridge XY. Roof heights, panel tilt and
mounting clearance retain unresolved feedback flags.

Future extraction now puts each building in its own generated group, alongside
attached objects. Previously a group could contain panels but omit its owner.
This change does not rewrite existing detector snapshots or reviewer groups.

Nine new roof-family labels and two manual solar masks are included in export
`22b6bc7fed4248ce`. Only the solar layer was scanned to completion; per-object
roof checks leave building layers incomplete. The classifier still needs
additional gable examples before fitting; see `docs/roof-training.md`.
The saved aerial/3D group and comparison views passed browser checks, and
`task test`, `task lint` and `task verify:ui` passed. Audit:
`data/references/scene-roof-solar-expansion-2026-09-23.json`.

## Traced gables and first fitted candidate (23 September 2026)

Six further gable outlines are saved through revision-checked API writes.
The fifth metal row, red tile roof and tan hall include editable ridge XY;
their Z values remain unmeasured. The tan hall
(`map-d9e2b64b51d32061`, `379182554cb077b5`) was checked in the aerial ridge
editor and 3D view. Its asymmetric image ridge follows the visible line rather
than assuming the footprint midpoint.

The western warehouse bay (`map-f808186b084ee88d`,
`review-west-warehouse-gable`) corrects a partial detection: two northern roof
fragments are superseded by one full gable-bay outline. Their original shapes
and reciprocal replacement links remain available in history. The eastern
bay is still unreviewed. Whole-image building coverage remains incomplete.

Export `a9a8f225f3807c53` has 42 family observations and 12 XY graphs.
Run `9d64ed31c4ee685f` produced the first fitted scene-feedback roof-family
candidate, with improved small-sample validation metrics. It is not deployed:
one further usable held-out gable is required. See `docs/roof-training.md` for
the fixed weights, validation scope and test protocol. The six class COCO
files are unchanged; heights and detailed shapes are supervised separately.
Audit: `data/references/scene-gable-training-candidate-2026-09-23.json`.

## Roof learning results in the CRM (23 September 2026)

Training coverage → Buildings → **Roof shape learning** now displays the saved
roof-classifier runs, reviewed versus usable examples, per-family minimums,
validation comparison and test status. The run selector retains older readiness
reports. The current run uses six validation roofs / ten ends and still lacks
one usable test gable; no test accuracy is displayed.

The read-only `/api/scenes/roof-training` route verifies artifact hashes,
model/report agreement and the frozen dataset hash. Unverifiable runs are
omitted and counted. Correction links lead to the exact training/validation
object; held-out exclusions are only counted. Changed saved datasets are
identified separately from the run's frozen export. No annotation, training
weight or deployed model is changed by opening the panel. Model fitting
remains the documented `scene:train-roofs` command, available in the panel's
provenance disclosure.

Verification for the results panel: 11 live runs loaded with no unavailable
artifacts; historical readiness, refresh and the exact tan-hall correction link
passed in the in-app browser with no console errors. The production build,
`task test` (345 Python tests plus Go/TypeScript/review checks), `task lint`
(0 errors, 31 existing warnings), and `task verify:ui` passed. New isolated
report-store tests cover changed artifacts/datasets, model/report disagreement,
missing runs and held-out score handling. This panel was visually checked at
the desktop viewport; its mobile layout has not been browser-verified.

## Houses on the main map — 23 September 2026

Selecting a house now exposes **Review analysed layers** in the address sidebar,
or **Analyse house & add to training** when no address-linked scene exists.
`/api/scenes/house` reuses existing captures; new addresses are captured in a
65–150 m image under `house-<address_id>`. They need not belong to a curated
cohort. Geography still governs train/validation/test eligibility. Capture and
model proposals do not constitute checked training labels.

The scene header offers **Re-analyse scene** and a Map link back to the image's
location. Extraction retains the captured image and immutable predictions,
runs object segmentation and the shared audited roof-estimation path, and leaves
saved reviews untouched. If predictions change, the existing feedback recovery
workflow carries compatible corrections into a review draft. Save that recovered
draft to publish it. Image/frame changes never silently relocate old feedback.

**Analysed scene layers** on the map selects saved feedback or model estimates,
filters object classes, and refreshes from the same scene store. Clicking an
outline opens that exact scene/object. The custom 3D layer uses the same meshes
as the editor, including roof apexes and roof-mounted solar arrays. Shapes and
heights remain estimates unless separately verified. A viewport shows the 24 captures nearest
the camera focus (not the horizon-biased centre of pitched map bounds); zooming/recentering loads the local set. Unavailable captures are
reported, and a failed request retains the prior visible map.

For display, same-class objects from different captures with IoU >= 0.65 are
deduplicated. Checked geometry and rejections outrank proposals; separate source
instances remain separate. Rejections can suppress duplicate proposals. This
never merges annotations or increases training counts. Earlier saved corrections
use their original frozen coordinate frame until recovered. Old tile roofs/trees
are hidden inside the analysed image extents for the corresponding scanned
classes, avoiding duplicate tile meshes. Turning off CRM layers restores them.

Re-analysis uses the currently installed, validated roof-end classifier when
available, otherwise the image/geometry prior. Candidate training runs are not
automatically deployed. Reviewed exports, fitting, holdout evaluation and model
promotion remain separate from saving or refreshing map layers.

## Property identity and address evidence — 24 September 2026

Capture addresses describe image provenance, not every roof in the image.
Scene headings and menus use the canonical address of an active linked roof;
multi-property scenes keep their area identity. The map's house lookup searches
object links across captures and prefers saved roof feedback. Rejected/replaced
objects and stale review links cannot redirect an address lookup.

`GET /api/scenes/addresses?id=...` returns read-only evidence bound to the model
signature and review revision. `scene_addresses.py` combines local indexed
address points with the existing official Vicmap Parcel reference. A suggestion
needs at least 85% roof overlap with one parcel, at most 15% with another,
complete local address coverage of that parcel, and one address point. Shared
lots, overlapping parcels, common property and boundary pins remain ambiguous.
Without parcel evidence, a unique point strictly inside the roof is a weaker
suggestion. Nearest pins and image centres never establish an address link.

The inspector can link a candidate through the normal revision-checked scene
save. Address links do not approve geometry or create training labels. Original
capture IDs, model snapshots, roof outlines and apex corrections stay intact.
The 27 Cambridge house-and-garage roof resolves to address 29790 through parcel
60984 even when its saved link is withheld during a read-only diagnostic.

The geometry layer is included in scene links (`layer=model|edited|overlay`) so
reloading a prediction does not silently return to saved feedback. When a saved
roof outline differs from the current prediction, the viewer names the displayed
version and explains the comparison colours. Saved apex lines retain the exact
outline they were drawn on; viewing the improved model never rewrites those edits.
