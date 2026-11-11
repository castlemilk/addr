# Worked cases that change tracing decisions

These are agent examples, not approved labels. Exact coordinates/identities live
in case data; do not assume other roofs share these shapes.

## Small light roof: a partial improvement missed the north edge

The first pass tightened a southern wall/shadow fringe and eastern bulge but
retained a northern apex and northwest corner. The apex projected into yard;
the next point clipped roof. A blanket note about a stepped/occluded north edge
was inadequate: raw pixels supported a straighter eave.

Correction: inspect unchanged vertices and all visible eaves/corner intersections.
Check both spill and missing coverage. Use native-pixel/nearest-neighbour views
when smooth magnification obscures the evidence on a small roof. A second
independent pass supported four sustained main-roof eaves; tiny intermediate
bends were within blur. It also identified a tan adjoining rectangle whose
ground/attached-cover interpretation remained unresolved. Name that uncertainty
in the draft rather than equating a tight main roof with a complete roof union.

Initial artifacts: `docs/mcp-polygon-uplift-2026-10-01.md` and
`data/reviews/eyes/polygon-uplift-2026-10-01/report.json`.
Follow-up: `data/reviews/eyes/roof-tracing-2026-10-01/`.

## White roof: darkest boundary lay beyond the roof

The southern bright roof ended above a dark wall/shadow band. Tightening that
edge helped, but the retained east side still included visible wall/fascia beyond
the roof. A front-only correction did not constitute a complete perimeter review.

Correction: distinguish roof surface, fascia/side wall and cast shadow along
each side, and inspect supported steps at their corners. The larger lower
setback is supported; a one-pixel upper/middle zigzag is not resolved and should
not become invented detail. West foliage contact also needs an uncertainty note. Outer-boundary evidence
does not settle roof pitch, level breaks or internal hips.
Artifacts: `docs/mcp-polygon-uplift-2026-10-01.md` and the follow-up directory above.

## Long roof block: outer tracing does not settle interior topology

The perimeter was mostly close while repeated wings/solar complicated internal
ridge/valley fitting. Rewriting the boundary to simplify roof lines would not
resolve that defect.

Correction: route boundary and roof-form errors separately. Trace supported
outer edges; use the addr-3d self-review workflow for topology experiments.
Artifacts: `docs/mcp-roof-review-2026-09-30.md`.

## Low-confidence batch: choose the right correction target

Three roofs had visible southern wall/shadow or paving spill; partial refinements
retained their unresolved canopy/attachment segments. A solar bank's northern
mask included pale roof; sustained module-frame edges supported a tighter trace.
A tree crown was already broadly close, a tiny solar detection was limited by
native blur, and a staggered solar bank needed gap/grouping decisions before a
complete replacement. Low confidence alone did not justify editing each polygon.

Correction: name the class-specific target and distinguish geometry defects
from topology, grouping and evidence limits. An unchanged/deferred outcome still
needs a complete edge/corner audit. Agent sidecars do not become approved labels.
Artifacts: `data/reviews/eyes/tracing-batch-2026-10-01/report.json` and its audit
directories; `docs/addr-tracing-batch-2026-10-01.md`.

## Vegetation granularity: a straight side need not be a hedge

A narrow tree detection had `clipped_by_frame` from an earlier crop; its long
straight edge cut across visible foliage. A separate vegetation detector strip
ran along a blue-grey roof edge. Their shape, kind and prompt did not establish
hedge or ground-cover semantics.

Correction: verify foliage in clean RGB and inspect old crop boundaries before
assigning a vegetation subtype or drawing internal seams. Exact source coverage
after a split preserves the prior mask's errors too. Fix supported boundary or
class findings separately; name residual crown ownership where contacts remain
unresolved. Retained source-bound evidence and recommendations:
`data/reviews/eyes/vegetation-skill-2026-10-01/vegetation-evidence/`.

## Vegetation holes and neighbouring crop fragments

A large crown mask had seven holes; its two largest gaps covered dark textured
foliage, so neither hole size nor darkness proved exposed ground. A compact
crown candidate had no crop flag itself, while its neighbouring detector
fragment still ended at an old crop boundary. The local patch did not establish
independent plant count.

Correction: inspect clean RGB for actual non-foliage gaps and wider context for
neighbouring crop artifacts. Retain unresolved hole findings instead of filling
or accepting every mask hole automatically. The evidence directory above keeps
these cases and source hashes. An independent current-draft review of tree0
also retained one crown patch because no sustained internal seam was supported;
its complete audit is in that checkpoint's `forward-test/` directory.

## Fabric shade sails: a roof-positive fragment is not a rigid roof

Two north-clipped fragments in `house-29785` were screened as generic roofs.
Their carried versions in `lot-29772` exposed curved membranes and corner
anchors, consistent with the user's canvas shade-sail identification. The
earlier crop cuts were now internal straight boundaries, and inherited hipped
meshes did not describe the fabric.

Correction: inspect wider original RGB before assigning a rigid roof family.
Use the explicit `shade-sail` structure subtype and `Fabric shade sail` label,
clear incompatible roof estimates/checks and retain partial outline findings.
Subtype correction alone does not finish the membrane trace or measure its
suspension. See [shade-sails.md](shade-sails.md). Canonical local and linked
Fieldwork saves preserved every polygon, immutable source and unrelated object;
artifacts are in `data/reviews/eyes/shade-sails-2026-10-01/` and
`docs/addr-shade-sails-2026-10-01.md`.
