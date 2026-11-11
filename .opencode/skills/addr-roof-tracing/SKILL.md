---
name: addr-roof-tracing
description: Review, trace and refine addr scene polygons against original aerial imagery. Use for roof and object boundary feedback, fabric shade sails and canvas covers, trees, hedges, bushes and other vegetation, solar banks, and evidence-supported finer segmentation or object grouping through revision-bound MCP drafts.
---

# Addr roof tracing

Produce a reviewable outline following the visible object's boundary; state what
the imagery cannot resolve. The Codex installation points to this project skill
so improvements stay with the checkout. Load `../addr-3d/SKILL.md` for protected
data/pipeline context when it is not already loaded. Tracing an existing object
does not require rerunning extraction or changing weights.

## Full perimeter review

1. Bind scene/object, class, saved revision, current outline, original PNG and source
   frame. `inspect_roof_boundary` or `inspect_object_boundary` reads the current saved object.
   `scene_roof_review` shows the source prediction and wider RGB; its polygon
   does not include subsequent edits.
2. View clean original RGB first. Use the native-pixel grid/numbered vertices
   and optional `edge_index` inspection to map evidence to source coordinates.
   Display magnification and photometric enhancement add no independent detail.
3. Inspect **every edge and corner**, including unchanged vertices. Check object
   omitted by the trace and background included by it. Do not keep a model
   point merely because it exists. Audit criteria are in
   [boundary-fit.md](references/boundary-fit.md).
4. Trace open outer/hole rings in original PNG pixels. Fit the class's supported
   boundary: straight eaves for roofs, organic foliage contours for crowns,
   and the targets in [object-types.md](references/object-types.md). Preserve supported steps and gaps.
   Neither fewer vertices nor a smaller polygon establishes a better fit.
5. Call `preview_roof_polygon` or `preview_object_polygon` and actually inspect the original/current/proposed
   image. Call the corresponding boundary inspector with that `preview_id` for the proposed
   native-pixel grid and numbered edges; repeat the full perimeter audit. Refine
   resolvable residual mismatches instead of presenting a partial correction as
   a finished fit. A second preview should contain an evidence-supported change.
6. If edits are authorized, apply exactly the inspected `preview_id`. Reload
   saved geometry, compare it with the proposal, verify unchecked machine status
   and show before/after overlays. Refused/uncertain saves require reloading;
   never bypass source, revision or human-authorship checks by writing files.

## Related scene objects

Use `object_feedback`, `inspect_object_boundary`, `preview_object_polygon` and
`apply_object_polygon` for non-building classes. Their binding includes object
kind as well as source/revision/outline. Roof tools remain building-only.
The same complete current/proposed perimeter audit applies. Read
[object-types.md](references/object-types.md) for the target and common false
boundaries of the selected class. For trees, hedges, shrubs/bushes or mixed
vegetation, also read [vegetation.md](references/vegetation.md): distinguish a
visible crown, continuous hedge and ground-cover patch before changing geometry.
`hedge` and `bush` are descriptions, not current `SceneKind` values. Record them
in labels/notes; preserve the source class unless a separate class edit is authorized.

For tensioned fabric or canvas covers, read
[shade-sails.md](references/shade-sails.md). Use the explicit `shade-sail`
structure subtype and `Fabric shade sail` label; a roof-positive screening
decision does not establish a rigid building roof. Keep material/type, outline
coverage and measured 3D geometry as separate findings.

Review need not produce a geometry edit. Keep an already supported outline;
defer changes whose magnitude is comparable to native blur or whose target
requires a grouping/class decision. Retain those edge findings in case evidence.
Generic polygon PATCH accepts replacement geometry and optional notes/issues;
it is not a note-only or class-change tool. No agent review sets human checks.

For hosted Fieldwork projects ending in `-review`, record assessments through
`save_agent_review` with the current parent revision, annotation ID (or a whole
item assessment), stable agent identity, decision and visible evidence notes.
The server appends independent agent history and binds the evidence; human
statuses and checks remain unchanged. Ordinary edits to these review projects
remain refused. Use `uncertain` when imagery cannot support a complete verdict.

## Finer segmentation and subgroups

Choose between refining one boundary, grouping existing objects, and splitting
one object into separately editable parts. Read
[object-segmentation.md](references/object-segmentation.md) before a split.
Use `preview_object_split`, `inspect_object_split` and `apply_object_split` for
two to eight same-class parts of a current source object.
The scene supports flat groups and replacement lineage; it has no nested group
schema. A split can create sibling parts linked back to the retained source;
further splits form lineage, not invented physical parents.

Split where original imagery supports the internal seam or where the user
explicitly requests an allocation partition. Distinguish visible foliage patches
from confirmed individual trees. More vertices or parts do not establish more
detail. Preview every child, shared seam and hole; preserve source coverage,
source/revision bindings, grouping and unchecked provenance. If ownership is
unresolved, keep an aggregate patch or a clearly labelled uncertain proposal.
Do not commit an arbitrary tree count because a geometric cut succeeds.

Tool contracts and the real SDK fallback are in
[mcp-workflow.md](references/mcp-workflow.md). Browser interaction uses the
available Browser skill; geometry saves use canonical API/MCP transactions.

## Fit criteria and stopping

Tight fit means following an identifiable class-specific boundary within original-image
blur/occlusion uncertainty. Candidate-to-prior IoU, area reduction and model
scores are not accuracy. Check native pixels before trusting a smooth crop.
Use source pixels for offsets, metres only via the supplied affine; a rough
1–2 pixel band can guide inspection but is not a universal acceptance threshold
or measured positional accuracy.

Name unresolved edges/corners and their evidence limit. Distinguish uninspected
edges from inspected but unresolved ones. A blanket 'uncertain north' note cannot
excuse a visibly displaced point. When evidence is insufficient, retain an
explicit partial draft or defer that segment; do not claim complete fit.
Put those remaining limits in the polygon's saved `notes` as well as the audit;
the preview's separate `uncertainty` field is diagnostic evidence and is not
automatically copied into the CMS notes.

Stop after the final pass finds no further resolvable perimeter discrepancy.
Usually a few focused rounds suffice; repeated unchanged proposals and guesses
about hidden eaves do not improve the result. Polygons remain tier 3/unchecked;
tracing does not attest roof family, heights or training eligibility. This skill
adds no authority to overwrite checked/imported/protected work or publish
elsewhere. Existing user authorization governs applying edits.

## Keep improving from real cases

Retain source identities, revision, inspection/preview IDs, before/after rings,
per-edge findings, unresolved segments and user feedback alongside the case in
`data/reviews/eyes/`. Case coordinates belong in data, never model code.
Read [worked-examples.md](references/worked-examples.md) for demonstrated misses.

For a demonstrated failure, identify the failed decision, make a narrow reusable
instruction/tool correction, then try it on another object or an independent
read-only review. Preserve earlier artifacts for comparison. User feedback is
guidance, not automatic ground truth. Update this project skill, not a detached
installed copy; validate metadata and checks appropriate to helper/tool changes.
