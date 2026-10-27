# Fabric shade sails and canvas covers

Identify the kind of cover before treating a roof-positive object as a house,
shed, carport or rigid patio roof. Suspended fabric commonly has stretched
corner anchors, inward-curved sides and a tensioned membrane spanning open
space. Its broad light/dark bands can be membrane curvature or seams rather
than a roof ridge, hip or valley. A cropped triangular portion can resemble one
plane of a hipped roof: inspect the wider current source image before deciding.
Colour, a rectangular mask or an inferred roof mesh alone cannot prove fabric.

When the user identifies a fabric/canvas shade sail and the visible evidence
is compatible, preserve that observation separately from unknown textile
composition, waterproofing, post heights or engineering details. Do not infer
canvas versus polyester/PVC product specification from aerial RGB alone.

## Representation and editing

The scene keeps covers in the broad `building` collection. Set
`structure_kind: "shade-sail"` and label `Fabric shade sail`. This is a durable
subtype, distinct from the ambiguous `pergola-or-shade` hint and from a rigid
carport/patio roof. Use **Structure kind → Fabric shade sail** in the local
editor, or an authorized, revision-bound canonical scene API update. The polygon
MCP tools do not accept subtype-only edits; do not smuggle classification fields
through their geometry patches. Review-only carried objects may have no source
prediction, so bind the canonical saved review and original PNG rather than
claiming the original-object polygon inspector can inspect them.

Changing this subtype invalidates the inherited rigid-roof interpretation:
clear conventional roof family, parts, hip/ridge/valley lines, annotation and
family/linework feedback. Leave outline and height approval unchecked until
their separate review. Preserve IDs, rings/holes, grouping, physical links,
carry/split lineage, source prediction and review history. The 3D reader shows
only the footprint; it does not extrude building walls, fit a conventional roof
or treat a sail as a solar-mounting roof. Curved fabric and suspension heights
need an appropriate measured membrane model, not a flat or hipped substitute.

Source `structure_kind` is preserved in a pushed Fieldwork generation item.
On an existing cloud item, immutable source subtype cannot be rewritten as a
mutable override; a supported label/notes correction and machine review audit
can describe the subtype while retaining the original source. A later canonical
push carries the local structured subtype into a new generation. Do not overwrite
the old item source to make its original classifier appear correct.

## Boundary and approval

Trace the visible membrane fringe and resolvable anchor corners, including
sustained inward curvature. Exclude cast shadow, ropes, poles and supporting
walls unless separately requested. Do not impose straight eaves or right-angle
cleanup on curved fabric. Adjacent touching sails can stay grouped, with their
supported separate surfaces retained; one membrane does not become several
objects merely because it has light/shade bands.

Check current capture context and earlier crop flags. A carried outline may
represent only the visible portion of the earlier image, even when the new
image now shows the full sail. Naming its subtype does not complete that
polygon. Record the partial coverage, retain an outline review issue and avoid
confirming geometry during a classification-only task. A later authorized
outline correction must use the same full current/proposed perimeter audit.

## Demonstrated case

The two carried objects `7ddfb55acd70b9fa` and `4e615c2e23cb5015` in
`lot-29772` were reviewed as generic covers in the small-roof batch. The wider
lot imagery exposed curved tensioned membranes and corner anchors; the user
identified both as canvas shade sails. Their earlier crop cuts had become
internal straight boundaries in the larger lot image, while inherited hipped
meshes remained unrelated to the fabric surface. The correction distinguishes
subtype from unresolved full-membrane outline coverage. Case images and saved
bindings belong in `data/reviews/eyes/shade-sails-2026-10-01/`.
