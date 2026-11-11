# Roof topology and gravity checks

The scene estimator now compares structural hypotheses as well as fitting image
lines. `roof_topology.py` addresses a specific failure mode: the straight
skeleton of a detailed silhouette creates one pitched plane for every eave
segment, including minor tracing bends and occlusion cuts. Moving those vertices
cannot remove the unwanted planes.

Automatic proposals compare the existing fitted shape with bounded construction
grid hypotheses. Long eaves must establish a coherent orthogonal grid (at least
0.75 angular coherence). The support envelope must cover the supplied outline,
add at most 14% area and deviate by no more than 1.5 m or 20% of the narrow span,
whichever is smaller. Major recesses remain; courtyards bypass this hypothesis.
These are modelling bounds, not positional accuracy claims. Surface polygons
are clipped back to the exact captured outline, including its small bends.
The support envelope is never saved as a corrected boundary.

Hip/gable comparison and registered image fitting run on the support roof before
clipping. Support eave corners remain fixed during that fit. Clipped silhouette
corners may have different heights: a visible occlusion boundary need not be a
level physical eave. This distinction is especially relevant to partially hidden
roofs. All generated heights remain estimates.

Selection balances the number of faces and small patches against the distance
from **all** detected image segments to compatible roof edges, including unmatched
segments. A candidate is rejected if mean weighted image mismatch grows by more
than 0.2 m, or its total ranking cost does not improve by 0.1. Complexity weights
are 0.08 per face and 0.12 per sub-0.5-square-metre patch. These costs are explicit
priors, not learned/calibrated probabilities. Visible folds can veto simplification.
The result includes the original and candidate costs, envelope bounds, end-family
evidence, line-fit report and rejection/selection reason.

`roof_physics.py` traces gravity on the actual emitted planar geometry, computing
slopes independently from vertices rather than trusting plane metadata. It starts
at triangulation centroids covering each face, follows its steepest downward
slope, transfers across seams and follows descending valleys. A level internal
valley can trap runoff even when its endpoints lie on the boundary; the old
vertex-only basin check missed this case. Trapped or unresolved traces fail the
pitched-roof audit. Boundary exits include courtyard edges. Flat roofs explicitly
report that runoff was not assessed. Audits of more than 64 faces report that the
runoff complexity limit was reached; their existing mesh checks still apply.

This supplements planarity, seam, coverage, overlap and local basin checks. It is
a sampled gravity model, not a fluid-volume simulation or a proof of drainage
for every point. It does not verify rain intensity, gutter/downpipe capacity,
flashing, loads, materials, internal drains or building-code compliance. A
complicated roof can drain perfectly and still be the wrong reconstruction;
therefore drainage is a validity gate, while simplicity and image agreement
select among plausible hypotheses.

## Reference review

For the user-identified main roof in scene `roof_training_250-29789`, the proposal
reduces 15 planes to six. The tiny top cluster and right-hand fan disappear;
the saved silhouette and L recess remain unchanged. All 26 sampled runoff paths
exit at a roof boundary, with exact projected coverage and no overlaps.

The previous fitted shape also passed runoff (39/39 samples). Its detected-line
mismatch was approximately 0.37 m versus 0.53 m for the simpler candidate: the new
choice trades a small amount of local image matching for much less fragmentation.
Neither value measures surveyed roof accuracy. The reference remains unapproved,
partly hidden and excluded from checked roof-line training labels. Full reports
are in `data/references/roof-physics-2026-09-23.json`; original predictions and
prior saved revisions are retained.

This changes inference and validation, not ML weights. Human-checked roof-family
and linework feedback still uses the existing spatially separated training and
validation process; predictions are never promoted to ground-truth labels.

## Sources

- [CGAL straight skeleton manual](https://doc.cgal.org/latest/Straight_skeleton_2/index.html)
  explains polygon wavefront roof construction and weighted variants.
- [Ren et al., Intuitive and Efficient Roof Modeling for Reconstruction and Synthesis](https://arxiv.org/abs/2109.07683)
  combines roof graph representations and planarity constraints.

Regression checks cover level versus sloping valleys, concave roofs, courtyards,
flat-roof scope, winding/rotation, visible-fold vetoes and preservation of exact
footprints. Run `task test`, `task lint`, `task verify:scene-roof WEB_PORT=3008`
and `task verify:ui WEB_PORT=3008`.

## Pixel-stepped roofs, small openings and image cuts (24 September 2026)

`roof-topology-v2` estimates construction axes from a copy simplified by 35 cm,
while still bounding the proposed support against the exact original silhouette.
This prevents a long eave digitized into many short steps from defeating the
axis-coherence gate. For roofs cut by the image border, support bounds are
checked over the visible image extent. The image cut itself is not assumed to
be a physical hipped end; emitted surfaces remain inside the captured outline.

Small openings (each at most 8 m² and together at most 2% of roof area) no longer
disable exterior support hypotheses. They remain exact holes in the output.
Planar surfaces containing holes are split into simple coplanar storage pieces;
those storage seams are excluded from structural roof lines. Larger courtyards
keep the existing courtyard-aware path. Neither an opening nor an outside
boundary is automatically edited by this change.

Automatic scene inference can prefer a simple gable on an elongated free end
when both adjoining eaves are at least 1.25 times the end width, the image or
trained classifier remains ambiguous, and gable line support is no worse than
hip support by more than 0.05. Confident hip evidence and reviewer form choices
are preserved. The choice is reported as `simple-gable-prior`, and image
uncertainty remains explicit. Training feature extraction still compares both
forms against the unchanged prior; no new observed labels or weights result.
Explicit gabled/hipped proposals remain available. Scene proposals now reject
pitched outputs whose sampled runoff was skipped for excessive complexity.

The reported building in `house-30286` is 27 Cambridge Road inside a capture
centred around 29. Following the user's address correction, its saved object
label/address association and roof proposal were updated through the canonical
API as revision 2. The original model snapshot remains unchanged. The UI uses
the selected associated building's label while retaining the scene capture
address as context, rather than implying every building has that address.

This estimate changed from 146 faces and 292 segments to 8 underlying planes,
represented by 11 surface pieces and 16 fold segments. Both holes and all
outline coordinates are unchanged. Independent annotation validation passes;
all 182 sampled runoff traces exit the roof boundary. The gabled ends remain
unconfirmed estimates, and the shape review flag remains open. This is an
inference improvement, not a measured accuracy result or new model training.
The reproducible report is `data/references/roof-common-sense-2026-09-24.json`.

Verification: `task test` passed (394 Python tests plus Go, TypeScript and
geometry/review checks), `task lint` passed with the existing 30 frontend
warnings, and the production build, map smoke test and roof browser lifecycle
test passed. Browser comparison shows 292 original versus 16 revised segments;
the isolated 3D view was inspected. Of 104 existing scene review files, only
the requested scene changed, and only its selected building changed within it.

## Straight eaves and shallow projecting gables (24 September 2026)

`roof-envelope-v2` fits straight lines to complete runs of segmentation samples,
weighted by distance along the eave. It intersects adjacent fitted lines to make
corners, tries several simplification scales, and selects the fewest corners
within both a 45 cm / four-pixel displacement cap and a 3% exterior symmetric
area difference. These bounds limit changes relative to the detector, not error
relative to a survey. Substantial recesses and diagonal edges remain; each eave
has its own orientation. Invalid, nearly parallel or excessive fits are rejected.
The image crop stays clipped and retained opening boundaries remain exact.
Existing image-conditioned small-hole filtering is unchanged. Raw detector
geometry and fit measurements are saved in `boundary_refinement`.

Gable candidates now include shallow front projections identified by two convex
front corners and concave attachment shoulders. A front width up to five times
the smaller adjacent depth is eligible (minimum depth 0.8 m). This only enables
a hypothesis: it does not force a gable. Continuous ridge evidence, a validated
classifier, or explicit reviewer choices still select its form. Training uses
the same candidate/evidence functions as inference. Blank imagery alone does
not select a shallow gable, and ordinary long side walls are not candidates.

`roof-topology-v3` also keeps an inferred support end with less than 25% of its
length inside the image from inventing an end hip. In automatic scene inference
its side planes continue to the capture boundary. This is recorded as
`image-cut-continuation-prior`, with ambiguous end imagery and raw evidence scores
retained. Visible hip evidence at other ends and reviewer form choices remain
in force. It does not claim the unseen building ends in a gable.

The requested 27 Cambridge object now has 18 proposed outline corners instead
of 137 (maximum movement 0.332 m, symmetric area change 2.43%). Its shallow front
ridge reaches the front edge; imagery support is 0.712 for the gable extension
versus 0.246 for the previous hip fan. These are diagnostic scores, not
probabilities. The proposal retains both uncertain holes and passes coverage,
planarity, seam and sampled-runoff checks (62/62 paths reach a boundary).
The canonical review update retains original predictions/history and leaves
outline and roof-shape feedback open. This changes inference; it does not train
new weights or mark the proposal as checked training data.

Shared-fold extraction now recovers the whole seam when one part intersects
exactly and another differs by clipping roundoff. Previously the exact fragment
could suppress recovery of the rest, leaving a gap in an otherwise continuous
ridge. Coplanar storage seams remain excluded. The final saved correction is
revision 4, with 10 surface pieces and 16 complete fold segments. Its source
frame, before/after outline, fit bounds, end evidence and full geometry audit
are retained in `data/references/roof-straight-eaves-2026-09-24.json`.

Verification: `task test` passed (402 Python tests plus Go/TypeScript/review
checks); `task lint` passed (30 existing frontend warnings). The map and roof
editor browser checks passed, and the actual saved aerial/isolated 3D views were
inspected without browser errors. Original model and other scene objects remain
unchanged.
