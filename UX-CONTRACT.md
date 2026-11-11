# Roof review workflow

Visual ownership is recorded in DESIGN.md. Domain sources are the CRM protobuf scopes, CRM handlers and the address extraction pipeline.

## Canonical UI Map

| Capability | Canonical owner | Source of truth | Allowed variants | Verification |
|---|---|---|---|---|
| Select/Listbox | Native select | DESIGN.md | native OS popup | browser keyboard and popup |
| Form | Roof review assessment and review-feedback validation | review-types.ts, CRM scopes | metadata, assessment | scripts/test-reviews.mjs |
| Scrollbar | app/globals.css | DESIGN.md | geometry-only overrides | computed style and narrow viewport |
| Toast | Review inline status region | UX-CONTRACT.md | success, error | save/failure browser checks |
| CRUD | review-feedback.ts and reviews route | CRM authorization and revisions | save, discard | scripts/test-reviews.mjs |
| Dataset navigation | reviews/page.tsx | UX-CONTRACT.md | 20-row queue | filter, clear, URL selection |
| Workspace navigation | WorkbenchNav / WorkbenchHeader (src/components/shell) | DESIGN.md App shell | dark, light tone; hrefs.map override | scripts/test-workbench-nav.mjs, scripts/verify-shell.mjs |
| Review queues | app/queues (read-only routes under api/queues, api/checkpoints) | docs/roof-eyes.md | calibration, hidden-low, checkpoints | scripts/test-queues*.mjs, scripts/verify-queues.mjs |
| Processing feed | api/scenes/processing ?part=summary/roofs/roof/hidden-low | docs/roof-eyes.md | summary-first, 20 s inventory TTL | scripts/test-processing-poll.mjs, scripts/test-processing-slice.mjs, scripts/verify-processing.mjs |


Review feedback belongs to a cohort and an address. Existing numeric-key feedback belongs to suburban_benchmark only. A save requires crm.roof.write. The latest saved revision wins only through an explicit revision check; stale writers must reload. Boundary signatures invalidate approvals after geometry changes. Corrupt review storage is never silently reset.

The four review statuses are to review, needs correction, approved and rejected. Approval requires a boundary imagery check, no flagged issues and no rejected cuts. Review approval is not a surveyed height claim. Training export applies separate gates for checked boundaries, roof shapes and object classes, documented below. Model estimates and manual templates retain their provenance.

Drafts survive queue navigation and filtering within the mounted workspace. Leaving the workspace with drafts is blocked with an inline recovery message; actual page unload uses the browser's unsaved-changes guard. Save retains the selected address. Discard restores the saved assessment. Failed or conflicting saves retain the draft.

Search is local over a loaded review set, with an immediate clear control. Fetches are aborted on set changes; a failed load has a retry action. Empty/no-result states are explicit. Native buttons and checkboxes retain keyboard semantics. Every 3D drag operation has rotation/zoom buttons. Live status does not move the save controls. No automatic approval, bulk deletion or auto-publishing is part of this workflow.

Review queues (`/queues`) and checkpoint reports (`/queues/checkpoints/<id>`) are read-only
surfaces: every row links into the canonical editor and nothing on them approves, rejects or
changes a roof. Status labels are derived live from `review.json` against the current model
signature; stale reviews read "Feedback needs recovery". The scene hub (`/scenes` without a
scene) lists saved scenes and never auto-selects one.

## Scene review variant

`SceneRoofFeedback` owns independent observed-family and image-linework checks
inside the building inspector. Native select and checkbox controls inherit the
inspector's read-only/busy gates. `scene-roof-feedback.ts` binds checks to copied
footprint/XY snapshots; derived currentness avoids stale approval booleans. Z,
group and placement edits preserve those image observations. Outline changes
invalidate both; linework changes invalidate only the XY check. Split/merge and
class changes clear incompatible feedback. History and same-image recovery
retain provenance; recovery still requires the outline recheck.

`scene_roof_feedback.py` independently checks saved XY coverage and scene/split
eligibility. `SceneRoofTraining` uses the training API's exact target counts and
review links under Buildings. No export mutates source reviews or updates model
weights. `scripts/verify-scene-roof-feedback.mjs` covers save failure/retry,
history, phone controls and API-save → isolated real export → coverage rendering.
Global swipe keyboard shortcuts ignore already-handled events and interactive
SVG controls, and are disabled while the roof editor is open. Nudging a roof
point must never save a review decision or advance the queue.

`SceneCanvas` owns registered aerial framing; `scene-image-frame.ts` fits the
selected outline to the viewport's aspect ratio with nearby image context.
Prediction, edits and overlay use the combined model/edited bounds so comparing
layers does not move the image. Whole-scene view shows the complete capture.
The edit frame stays fixed during pointer, keyboard and numeric point changes;
an explicit **Whole scene → Focus object** refits the revised outline. Zoom
preserves the visible centre, and selection changes recenter the retained zoom.
Viewport changes refit without modifying coordinates. Drawing a new object
still opens the complete image. No view action saves or approves an annotation.
Verification: `scripts/verify-scene-framing.mjs` and frame containment/scale cases
in `scripts/test-scene-geometry.mjs`.

`Scene3D` owns the **3D edit handle** selector for roof-bearing buildings. The
existing `SceneHeightControl` edits either whole-object height or a selected roof
point, with one preview/commit/cancel lifecycle. Point heights are relative to
the building base; the control's projected position includes base elevation.
`roofHeightGroup` / `withRoofPointHeight` in `roof-annotations.ts` are shared with
the aerial editor so connected horizontal ridges move together. Roof point
numbers are identical across editors. The existing MetricInput is the numeric
alternative, and the slider retains arrow/Shift/Home/End keys and visible focus.
At viewport edges the handle stays visible with a connector to the projected
point, preserving pointer capture and Escape cancellation. The edit selector,
camera controls, navigation and saving are disabled during a pointer preview.

`withObjectRoofAnnotation` in `scene-height.ts` preserves the separate scene
class/outline decision, outline geometry, base and eave estimates while clearing
height verification. Explicit eave-point edits can change that point's roof
elevation; whole-building height shifts every roof point instead. Draft geometry
issues remain visible without silently rewriting the mesh. Scene roof feedback
still uses the canonical revision/history API and is excluded from measured 3D
training targets. Verification: `scripts/verify-scene-roof-height.mjs` and shared
geometry tests. The dedicated roof workspace keeps its separate roof-form/apex
approval gates.

`/scenes/candidates` owns the experimental proposal inspection variant, reached
from Training coverage. `scene-candidate-store.ts` verifies frozen dataset,
image/frame and score provenance; `scene-candidates.ts` owns overlap suggestions
and explicit draft construction. Kept and filtered proposals stay inspectable on
the same image. Current review outlines are an optional, separately labelled
overlay; frozen validation counts never change when a live review changes.
Validation matches and extras are compared separately with the unfiltered prompt.
Losing a reference is explicitly flagged even when precision or F1 improves.
**Inspect lost matches** opens an affected validation image and selects a filtered
proposal with a frozen reference match. That historic match is kept distinct
from the latest saved annotation. The warning never rejects, restores or approves
an object automatically; the canonical editor owns those decisions.
Overlap suggestions open the canonical `/scenes` editor and never replace a
saved annotation. **Add as review draft** imports only an unmatched proposal,
resets its approval and height verification, and reopens its class review. It
uses `scene-store.ts` permissions, revision locking and history; retries resolve
the same draft. Changed image frames and stale reviews disable imports. No
candidate automatically changes production inference or enters training labels.
Native filters, image selection, focus/whole-image controls and the equivalent
keyboard button list share existing review styles. Navigation and selection
are disabled during draft submission; failed saves offer retry/refresh. The
read-only candidate canvas is distinct from the canonical editable SceneCanvas.
Verification: `scripts/verify-scene-candidates.mjs`, plus geometry/draft cases in
`scripts/test-scene-geometry.mjs`. Evaluation scope and provenance originate in
`scripts/ml/train_scene_proposals.py` and `docs/scene-proposal-training.md`.

`/scenes` owns whole-image object reviews. `scene-types.ts` defines classes,
geometry and estimation provenance; `scene-store.ts` owns validation, revision
checks and atomic storage. Model snapshots are immutable, with reviews separate.
Roof/tree/object writes use the corresponding existing CRM scopes; groups and
layer completion require object editing access. Extraction requires object
editing access and keeps earlier snapshots and reviews.

`scene-queue.ts` owns shared review filters and progress semantics. Needs-edit
objects remain unresolved; unchecked confirmations also count as needs-edit.
Active layer counts exclude rejected and replaced objects. Replaced sources
appear only in their history filter. `SceneQueueNavigator` finds saved images
matching the current class, status and search, opening the first matching object
with **Next image**. Navigation wraps and skips empty queues, while new model
estimates retain a stale-review flag. Drafts and open operations block navigation.
Debounced requests abort on filter changes; queue errors have a local retry and
must not discard the current editor. Selection and filters persist in the URL.
Swipes advance within the current image; cross-image navigation is explicit.

`/scenes/new` owns map capture, entered from **Capture map area** or a rectangle
drawn with **Review scene area** on the main map. Coordinate entry is the keyboard
and touch alternative to dragging. A successful capture opens its scene; pending
requests disable duplicate submission, and errors retain the selected bounds.
The 10–250 metre limit and geographic validation are defined in `scene-region.ts`
and `scene_region.py`. Missing imagery fails rather than producing blank training
pixels. Model candidates are unverified, including reprojected CRM roof shapes.

Swipe right / Looks right confirms the selected object's visible outline and
class. Swipe left / Needs edit flags it for correction. Neither action verifies
metric height. **Reject detection** records a false positive for that proposal's
class; an overlapping object of another class may still be valid. Reclassification
keeps the source prediction available, and the card names its original class
beside its original detector score. Road, driveway and footpath descriptions
guide consistent surface labels. Decisions save
before advancing; failures retain the same object and draft. Undo writes a new
revision. Edit mode disables swipes; drawing and dragging have numeric/keyboard
alternatives. Drafts remain when selecting objects and filters. Save/discard is
required before switching scenes or navigating away. Every layer has independent
visibility; groups and parent relationships may cross layers without flattening
geometry. Complete-layer feedback requires all its proposals to be resolved.
Tapping an outline selects it without triggering a swipe decision. Adding a
missed object opens the whole image rather than retaining the previous object's
crop. The scene header wraps its actions at narrow viewport widths.

The existing approved-roof export remains distinct. Training target eligibility
is independent: a checked boundary can be used while roof-shape issues remain,
and checked object classes can be exported while heights remain estimates.

Changed-model recovery is an explicit scene-review action, defined in
`scene-feedback.ts` and validated by `scene-store.ts`. It requires the previous
immutable model snapshot, identical image bytes and identical image/metric frames.
The reviewer can preview corrections over the new predictions, then carry them
into a revision or start from predictions. Carry-forward retains manual objects,
geometry, apexes and groups; changed-ID detections remain visible. Carried outlines
return to needs-edit, completed layers reset, and prior revisions remain intact.
Different imagery or frames require a new review. Normal CRM scopes and revision
checks apply to the resulting save.

Solar-array and skylight placement is owned by `scene-roof-mount.ts`. With a
building parent, roof following uses edited roof geometry. `scene-solar.ts` fits
one area-weighted mounting plane for each solar bank, raising it above all support
points by its editable minimum clearance. Tilt and downhill azimuth use a shared
decimal input, with an aerial direction arrow and no arrow for a level bank.
The inspector shows bank underside elevations and minimum/maximum roof gaps.
Large roof variation gets a review cue rather than bending the bank across faces.

`SceneRelationships` exposes building-outline containment separately from roof
face coverage. Roof following requires both: retained roof faces may not bypass
an edited building outline. Overlap is reported explicitly, without treating
double-counted area as complete support. Parent choices exclude self and
descendants; rooftop objects choose active buildings ranked by containment.
Inspecting a parent or attached object preserves the current draft. Return
restores the originating object, filters and comparison view (relaxing filters
only if that object's decision changed). Related navigation focuses the image.
Prediction relationships remain read-only; neither containment nor reassignment
constitutes a verified height or revokes an unchanged 2D outline approval.
Automatic fitting, manual orientation and independent base elevation remain
explicit estimates. Independent elevation anchors the lowest underside; it
retains the roof clearance setting for later use. Skylights keep the existing
roof-surface fit. Neither mode treats estimated roof geometry as measured height.
Missing/rejected parents or incomplete/overlapping support produce an inline
correction hint and retain absolute-base rendering. The reviewer can explicitly
select independent elevation; `SceneModel.placement` persists that decision and
model recovery preserves it. This does not change class/outline label eligibility.
`SceneModel.solar` stores mounting feedback independently of 2D approvals. Its
manual tilt and azimuth must be supplied together. Class changes clear incompatible
settings; split/merge banks reset their mounting plane, retaining originals in
history. Saved mounting estimates do not become supervised height targets.

Buildings without roof planes use **Create roof shape**; existing planes use
**Try another roof shape**. `/api/scenes/roof` requires roof-editing access and a
current scene/model revision. `scene_roof.py` validates proposals with the shared
physical roof audit and preserves the supplied boundary. The result changes only
the local draft until the usual save. Changed selections abort pending proposals;
failures retain edits. Template and image-guided provenance remain visible, and
ambiguous image evidence is explicit. Generation does not approve class, outline,
apex or height labels. The roof editor shares `MetricInput` for decimal heights.
Scene object creation belongs to **Add object**; the shared roof editor's separate
object controls are hidden in scenes rather than presenting disconnected actions.
Scene roof-point editing opens with a crop around the existing outline and
scrolls the editor into view. **Whole image** / **Focus roof** changes only the
SVG view; pointer coordinates still use the registered image-to-metre frame.

Tree shape feedback belongs to `SceneModel.tree`. Crown profile and crown-base
proportion are validated on save and preserved during model recovery. They do
not imply height verification or become outline training targets. `scene-tree.ts`
owns the closed canopy mesh; concave outlines must retain their empty notches.
Fully visible canopies also retain interior gaps in both crown surfaces. Trunk
placement and radius use clearance from outer and inner boundaries, so no trunk
fills a gap. Heights and trunk positions remain estimates.
3D **Focus object** changes only the camera and follows the selection. Ordinary
geometry edits leave the camera steady; **Whole scene** restores image context.
`SceneHeightControl` owns direct height feedback on the selected 3D object.
Its pointer preview uses a fixed vertical plane in world metres, with camera
orbit and scene navigation suspended until release or cancellation. Release
creates one ordinary draft/undo entry. Escape, touch cancellation and lost
capture discard the preview. Arrow keys adjust 0.1 m (0.01 m for thickness),
Shift uses ten times that step, and Home/End use the valid height range.
`scene-height.ts` also owns the numeric height update: base elevation, outline
approval, profile proportions and mounting settings are preserved. Building
edits shift all roof vertices together within the saved Z limits, keeping roof
rise intact. Values remain reviewer estimates and are excluded from supervised
height targets. Prediction, rejected/replaced, hidden and partial-footprint
objects have no editable handle; group inspection remains read-only. Verification:
`scripts/verify-scene-height.mjs` and `scripts/test-scene-geometry.mjs`.
**Isolate object** focuses the selection and temporarily hides unrelated meshes,
retaining attached descendants. It does not change layer visibility or review
data. Hidden meshes cannot intercept selection. **Whole scene** also exits
isolation; **Show surroundings** restores context at the existing focus.

`SceneObject.holes` owns interior-gap rings in the same pixel frame as `ring`.
`SceneCanvas` edits either boundary and supports explicit add/fill with numeric
corner entry. Unfinished gaps block object/image navigation and saving; finish
or cancel releases that state. `polygonIssues` validates containment and
disjointness. Gap changes reopen outline/class review and clear building roof
proposals. Compare, save/history, model recovery, split/merge, area and 3D must
preserve the gaps. `triangulateFootprint` and `parentRoofMesh` prevent courtyard
coverage; the roof editor accepts gaps as boundaries. Scene export schema 5 uses
COCO RLE for gapped outlines. Tests must check pixels and geometry, not only JSON.
`task verify:scene-gaps` owns browser verification of this lifecycle.

**Groups** opens `SceneGroupEditor` inside the scene workspace. It owns the
bounded, locally filtered member list and registered-image membership controls;
the scene is already loaded and limited to 1,000 objects by the API. Local group
search/class/member filters are transient within this editor, while ordinary
object-review filters retain their URL state. Sidebar checkboxes still govern
image/3D visibility. `scene-groups.ts` owns cross-layer membership, rename and
remove actions. Actions create ordinary scene drafts; Save scene, Undo edit,
history, permissions and stale-write checks retain their canonical owners.
Removing a group clears current references, including inactive records, while
preserving objects, their approvals, parents and saved history. Read-only
prediction mode uses original groups; reviewer-created groups show an empty
counterpart explicitly rather than switching to an unrelated prediction.
Duplicate group labels show an identity suffix in the selector.
`scene-feedback.ts` preserves renamed and
removed groups across matching-frame re-extraction without dangling references.
**Inspect** opens an object with its drafts intact; **Return to group** restores
the chosen group. Group 3D reuses `Scene3D` focus and isolation, showing exactly
the selected members while all scene objects remain available for roof support.
Native checkboxes and image keyboard targets provide equivalent membership
actions. `task verify:scene-groups` checks the full lifecycle and mobile layout.

`SceneObject.visibility` owns explicit extent feedback. `scene-visibility.ts`
distinguishes unassessed, full, occluded and image-clipped outlines. Border
touches are an unsaved cue; reviewers can resolve coincidental edge touches.
Partial cars/trees show their flat visible footprint in 3D, retaining but hiding
whole-body/crown settings. Their direction arrow is also withheld. Visibility
must be explicitly full to show a gapped tree crown; unassessed gapped trees and
cars with any gaps retain their footprint preview. Visibility
alone preserves 2D approval. Saved history and same-image recovery retain it;
replacement instances reset it. JSONL/COCO preserve visible-mask semantics and
explicit visibility without claiming an unassessed mask is a complete object.

Paved surface feedback belongs to `SceneModel.surface`. `scene-surface.ts`
constructs a plane or raised centre inside the road/driveway/footpath outline.
Direction, grade and crossfall are editable estimates; base means the lowest
underside, with constant vertical thickness. The aerial arrow shows the positive
grade direction, and the inspector shows the top elevation range. Neighbouring
objects are not repositioned. Split/merge clears the old extent-dependent profile.
3D-only edits preserve existing class/outline approvals. Changes to outline,
class or review decision revoke layer approval. These controls never establish
measured heights or add 3D training targets.

Vehicle shape feedback belongs to `SceneModel.car`. `scene-car.ts` fits a
closed cabin shell inside the current outline using its oriented bounds.
Front bearing is clockwise from north; an aerial arrow exposes its ambiguity.
Style, heading and cabin proportions remain estimates, use ordinary saved
revisions and recover with other model feedback. Cabin fit limitations appear
inline. Changing class or replacing an instance clears incompatible vehicle
settings. The model inspector and its crop must use the original object, while
the edited layer uses the current draft. Newly added objects have no prediction.

In the aerial review card, vehicle, paved-surface and tilted solar arrows have
direction handles. `scene-direction.ts` converts pointer positions through the
scene's pixel-to-metre transform before calculating a north-based bearing.
SceneCanvas keeps the anchor fixed during a drag, previews the arrow locally,
and commits one ordinary object edit on release. Escape, pointer cancellation
or lost capture discards the preview. Handle gestures never become deck swipes.
Tab reaches the handle; arrow keys turn by 1°, Shift by 10°, Home points north
and End selects 359.99°. Inspector fields remain the numeric alternative.
The handle is absent for predictions, hidden/inactive objects, partial vehicle
footprints, level solar banks and outline/drawing modes. A tilted automatic
solar plane retains its current tilt when its direction becomes manual.
Direction edits preserve outline/class approval, dimensions, relationships and
other shape settings; they revoke height verification and never create measured
3D labels. Save/reload, failure recovery and undo use the existing scene draft.
`task verify:scene-direction` covers desktop pointer/keyboard and phone touch,
including exact-centre hit testing, cancellation and approval preservation.

`SceneInstanceEditor` owns split/merge and manual trace drafts, with numeric
coordinates and same-class checkboxes as alternatives to image clicks.
**Correct outlines → Trace replacement** selects up to 64 fragments, optionally
from their existing group, then draws one visible outer boundary. Traces may
replace disconnected fragments but must overlap every selection. Interior gaps
need review afterward in the existing outline editor. **Undo last corner** and
**Clear drawing** preserve the selection. Switching to split/merge requires an
empty drawing; **Cancel trace** discards the local trace. An unfinished trace
guards object/image/layer navigation, header links, swipes and saves, and warns
on browser unload. The visible filtered object is pinned before entering this
editor so drawing cannot switch back to the initial object from another class.
`scene_instances.py` performs exact split/union or validates the supplied trace;
`/api/scenes/instances` checks scene revisions
and access to every affected class. `scene-instances.ts` builds replacement and
parent relationships. Sources become read-only `superseded` objects; their
`replacement_ids` and the new objects' `derived_from` links must be reciprocal,
complete and acyclic. A trace can link 64 sources; split results remain bounded
to eight replacements. They remain available through **Replaced outlines** and
history. New geometry resets its review and affected layer coverage. Replaced
sources never become negative labels. The ordinary Save scene transaction owns
persistence; errors preserve the draft and cut settings. Pending requests abort
when the editor closes or its selected object changes. Deck swipes are disabled
while the split/merge editor is open.

Scene training coverage lives at `/scenes/training`, linked from the review
header through the existing draft-navigation guard. It reuses the navy review
shell, native selects, buttons and inline feedback. Class/split/status filters
persist in the URL; matrix actions move focus to the filtered layer list.
Coverage distinguishes saved confirmed outlines from exportable masks, checked
empty images, unresolved objects and unscanned classes. Counts describe images
and spatial groups, not unique physical objects or measured accuracy. Review
links reopen the exact pending object with the class filter and all statuses.
Empty, loading and retry states retain navigation; failed refreshes explicitly
label retained counts as stale and disable export until coverage is verified.

`scripts/ml/scene_training.py` owns both live coverage and export eligibility.
Only current reviews with checked outlines and complete class coverage enter
COCO. Unknown/missing splits and cross-split repetition of a spatial group or
image are withheld. `val` is a display alias for validation; source provenance
retains its original value. Scan completion, including zero detections, cannot
establish an empty training label. Scene heights and apexes remain excluded.
The coverage GET is read-only; dataset export requires the same origin and
object-editing access, serializes concurrent exports and publishes files
atomically. The last exported version is compared with current saved data.
Export success explicitly states that detector weights have not changed.

`scene_evaluation.py` adds read-only model checks to that same coverage result
and to each export. Only fully eligible, completed scans are scored. Selected
class/split controls the checks; layer-status filtering controls the review
list only. Undefined precision, recall or overlap is a dash, never an invented
perfect score. Per-split counts, image/area denominators, method disclosure and
links to evaluated layers accompany every score. Matching uses original model
objects and confirmed references, retaining holes and duplicate penalties;
it does not substitute edited geometry into predictions. Failed refreshes mark
these results stale together with coverage. The evaluated-image disclosure is
locally transient. The existing native controls and scrollable table own this
interaction; no new mutation or permission is introduced.

Whole-image geographic eligibility is owned by `scene_split.py`. Coverage and
exports check the entire affine pixel frame against the existing z17 train,
validation and test policy. A valid host-roof split does not approve neighbouring
image context. Crossings, mismatches and invalid frames appear as **Image
geography — withheld**, with the cause under Review provenance. Saved reference
outlines are preserved; a smaller capture is the recovery path for crossings.

`SceneObject.review_issues` stores explicit unresolved feedback. The shared
`scene-review-issues.ts` owns labels and changes; `SceneIssueControls` uses native
checkboxes inside the existing inspector fieldset. Outline, object type and
separation flags reopen 2D review. 3D shape, height and placement flags preserve
independent outline approval; a height flag revokes height verification.
Clearing flags does not approve either geometry or measurements. Right swipes
and the save API reject confirmed outlines with outstanding 2D flags.

The native **Feedback filter** persists as `issue` in the scene URL and applies
to both local objects and the next-image queue. Selecting a feedback category
uses all review statuses so confirmed outlines with 3D concerns remain visible.
Explicit object navigation clears incompatible filters; related-object return
restores them. Saved history, same-frame recovery and instance replacements
retain unresolved feedback; replacements combine flags from all source objects.
Prediction mode shows only original feedback and stays read-only.

Scene export schema 5 stores unresolved issues separately from visible-outline
targets. 2D flags withhold contradictory confirmed objects and their incomplete
class layers. 3D flags never supply height, apex or shape ground truth. The
manifest counts feedback by issue without claiming model learning from an export.

Candidate **Training and evaluation details** separately discloses any frozen
supplemental instance dataset and its usable/negative counts. These training
images never appear as fully evaluated layers. Candidate loading verifies that
the supplemental dataset still matches its content identity before opening masks.

Candidate **Evaluation run** separates original selection results from later
validation on additional reviewed images. A newly selected candidate opens its
latest check; existing links to original images retain their original scope.
Later regressions remain visible while inspecting the original evaluation.
**Inspect later regression** opens the affected image and a lost reference match;
proposal filters, focus, reload and annotation imports retain the check identity.
Imports still use the canonical revisioned editor and deduplicate the same
candidate/proposal across checks. Evaluation never approves or restores outlines.

`scene:validate-candidate` fixes the trained prompt, detector components, weights
and threshold. It excludes training/test and previously selected image identities,
checks full-frame validation geography and preserves source labels. Checks publish
only after every image finishes. `scene-candidate-checks.ts` verifies config/model,
per-image file hashes, scope and report counts; damaged runs are unavailable without
hiding intact original evaluations. A damaged or missing selected check never falls
back to a different run. Small later checks remain evidence for inspection, not a
general-accuracy claim or an automatic deployment gate.

### Focused class review

SceneReviewPage owns the transient **Only review layer** view preference. It is
on by default for a specific class filter and inactive for All layers. The
identity scope is derived from the review objects, including the selected object,
so reclassification does not hide that object's original comparison outline.
SceneCanvas applies this scope to both comparison layers; Scene3D applies it to
mesh visibility and picking while retaining all objects for parent-roof support.
Layer isolation does not mutate annotations, groups, approvals, filters or saved
visibility choices. **Show all** restores all layer checkboxes and exits the
isolation preference. Tapping a same-class outline keeps the active class filter.
Groups and explicit parent navigation retain their existing cross-layer context.
A scoped 3D view starts focused on the selected object; **Whole scene** restores
the camera context within the current layer scope.

The card appears before the cross-image queue; that queue remains in the same
column, with its existing draft guards, failure/retry and exact-object navigation.
Image review counts sit in the object inspector. Laptop verification checks the
image and decision buttons together in the first viewport; mobile preserves
natural scrolling, visible editing controls and the collapsed layer chooser.
Verification lives in `scripts/verify-scenes.mjs`, `scripts/verify-scene-solar.mjs`
and the existing queue, relationship and group workflow checks.
On narrow screens, **Jump to review** scrolls to and focuses the existing review
card without changing mode, selection, filters or draft. It has no desktop
counterpart because the card and actions already fit in the initial laptop view.

### Compare 3D predictions with edits

`Scene3D` renders **Compare** as solid current surfaces plus a separate original
prediction group, built by `populateScenePrediction` from the immutable model.
Amber X-ray lines remain visible through edited surfaces and are labelled as
such; faint original surfaces retain curved shapes. Original roofs, tree crowns,
vehicle bodies, road slopes, bases and roof-mounted objects retain their own
geometry and original parent context. Editing a roof must not move its original
solar-panel comparison onto the edited roof.

Prediction geometry cannot intercept picking or editing handles. Height handles
and measurements use only the current mesh group; hiding or changing the
comparison never saves, approves or modifies an annotation. Original geometry
is cached across edit previews. Class visibility, focused-layer identity scope,
object/descendant isolation and exact group membership apply to both layers.
Focus bounds include both displayed versions; ordinary edits and layer switches
retain the camera. Rejected/replaced objects can be inspected as original-only
geometry without acquiring an editing handle. Reviewer-added objects explicitly
state that they have no original prediction.

Roof shape learning in Training coverage reads frozen roof-classifier runs.
Each displayed report must match its artifact hashes, fitted model and exact
dataset export. Missing or changed runs are omitted with an explicit count;
a failed refresh preserves the last successful display with a stale notice.
Reviewed and usable examples remain separate. Unopened test features display
as unassessed, and incomplete test coverage never displays an accuracy score.
Run selection compares historical readiness without changing current labels.
Correction links target training/validation objects; held-out exclusions are
aggregate counts. Reading this panel cannot fit, deploy or promote a model.
The retraining instructions distinguish export, fitting and fixed-weight test
assessment, and state the classifier's limited hip/gable scope.

## Main map and scene review

A selected address opens its existing scene, or starts on-demand analysis without
requiring cohort membership. New scenes use `house-<address_id>` and the same
revision-controlled review store. The primary action is in `HouseReviewLink`;
legacy cohort-only review links are absent from the address card. Clicking a
saved outline offers a deep link to its exact object. Re-analysis is explicit,
disabled during dirty/stale drafts, and retains saved feedback and model history.
The editor's Map link returns to the captured location.

`SceneMapOverlay` owns the saved/model source selector, object filter and refresh.
`scene_map.py` projects saved frames and deduplicates cross-capture proposals for
display; `SceneMapLayer` reuses the editor's 3D geometry. Scanned-class extents
suppress legacy tile shapes while CRM layers are shown. Class changes, toggling,
style replacement and aborted loads must not leave old meshes visible. The map
never treats a render, detector proposal or refresh as a human-approved label.

### Focused roof line editing

`RoofLineEditor` is the scene editor's focused line-workspace variant. Opening
it replaces the ordinary review card and inspector; Back to review retains the
same object and draft. Changing to 3D scene or another scene class returns to
the standard view. The existing outline/apex editors keep their owners. There
is one visible save action while the line workspace is active, calling the same
revisioned `persist` path. Failed saves keep the draft and allow retry. Image
enhancement uses the existing registered photometric view.

Line-kind visibility, edited/baseline/boundary toggles and line selection are
transient viewing choices and never create labels or revisions. Selecting a
hidden line from Find a line reveals its type and permits inspection. The list
is made of native buttons; selects and disclosure controls retain native
keyboard behavior. Drawing and dragging retain their pending-operation guards,
Escape cancellation and keyboard alternatives. Geometry/type edits invalidate
the explicit image check; notes and visibility do not.

`scene-store.ts`, `roof-line-edits.ts` and the existing scene/roof-feedback API
remain the validation, provenance and permission authorities. A saved line edit
is an observation, independent of outline approval, estimated 3D faces and
measured heights. `scripts/verify-scene-roof.mjs` covers focused layout,
selection/visibility, save failure/retry and the API handoff on isolated scenes.
