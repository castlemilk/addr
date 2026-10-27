# Finer parts, logical groups and replacement lineage

This reference describes the full local tracing workflow. Check the hosted
[tool contract](mcp-workflow.md) before using it: hosted annotation CRUD does not
provide local preview/split operations or their protected lineage validation.

Choose the operation that matches the requested granularity:

| Need | Operation | Meaning |
| --- | --- | --- |
| One object spills into background or misses visible surface | Refine its polygon with the ordinary preview/apply tools. | Same object, corrected outer ring/holes. |
| Several existing objects belong to one garden, hedge line or installation | Link them with a flat group through the scene's group editor. | Logical collection; geometry and physical parents stay intact. |
| One merged outline contains separately reviewable visible parts | Preview and apply a source-bound split. | Source is retained as superseded; new sibling objects carry reciprocal lineage. |
| User requests inspection/allocation sections within a continuous object | Preview a labelled allocation partition. | Administrative subdivision; internal cuts are not physical object boundaries. |

Scene groups are flat: each object has at most one `group_id`, and groups have
no parent field. Do not promise arbitrary nested subgroups. Split children can
be linked in the existing source group, and later child splits can retain
multistage replacement lineage. Neither operation establishes biological tree
count. `parent_id` remains physical attachment/support, not split ancestry.

## Geometry and evidence

Bind the current source object, kind, model/PNG hashes, source frame, current
outline and saved revision. Use same-kind children; classify a hedge/bush
description in notes rather than inventing a `SceneKind`. Clear derived shape
estimates and leave geometry/height review and training eligibility unchecked.

Inspect children together with the original/current outline. Audit both the
external perimeter and each new internal seam, its endpoints and any hole it
meets. Avoid gaps, overlap slivers, duplicate children and unnoticed swallowed
holes. Shared seam points must agree in source pixels. Exact geometric source
coverage is a conservation check, not proof the seam follows a real object.

A pure split preserves the current parent's coverage. If the aggregate includes
visible path, roof or soil, first make a separately audited boundary/hole
correction, save/reload it, then bind the split to that new revision. Do not
silently discard background during a split or fill holes to make a cut easier.
Disconnected foliage stretches need distinct polygons rather than a connector
through a gate/path. Children must not extend outside the current source.

One important limit: removing a bad connector across a gate/path may make the
corrected mask disconnected. The ordinary single-ring correction cannot save
that as one object, and a pure split preserves the bad connector's pixels. Do
not invent an interior hole or a zero-width connection to force the result.
Retain a labelled unresolved-residual part only as an explicitly partial
partition, or keep a multi-object replacement proposal plus its background
evidence and defer its save until a guarded replacement path supports coverage
change. The current editor's one-outline retrace is not a general disconnected
replacement save. This limit is distinct from splitting an already correct
connected mask into supported parts.

Prefer fewer defensible parts to a guessed complete decomposition. An internal
boundary hidden by touching foliage may be deferred while visible exterior
edges are tightened. More detailed parts do not mean greater measured accuracy.

## Canonical draft lifecycle

Apply only an inspected immutable split preview. The transaction must compare
source/revision/outline bindings, validate all children together, and save the
superseded source plus all children atomically. Retain the original geometry
and reciprocal `derived_from`/`replacement_ids`, rather than deleting or
rejecting the source. Keep children in the source's existing flat group when
present; do not introduce a nested group schema.

Preserve physical relationships. If attached objects need reassignment, inspect
full containment in exactly one new part; a centroid alone is insufficient.
If the split tool refuses attached-object handling, use the existing scene
instance editor to inspect its explicit reparenting proposal or defer. A
successful parent cut must not leave approved children attached to a superseded
source or silently overwrite their provenance.

Reload the source and every child by the IDs returned from the save. Confirm
exact inspected rings/holes, stable distinct IDs, source `superseded` status,
reciprocal acyclic lineage, preserved group/physical-parent links, history,
unchecked machine provenance and unchanged unrelated objects. Show a registered
original/aggregate/parts comparison with labelled children and uncertainty.
After a refusal or an uncertain save, reload before retrying; keep stale
previews rejected instead of rebinding them to a new source.

Use the implemented tool contract in [mcp-workflow.md](mcp-workflow.md). The
existing `/api/scenes/instances` endpoint creates editor drafts; it does not
itself persist them. Do not call it a saved split or use unrestricted full-review
saves to bypass the agent path's protected-work checks.
