---
name: addr-crm
description: Browse, inspect and update the hosted addr Fieldwork annotation CRM through its MCP tools. Use for project and item searches, saved feedback, source-image review, object classification and revision-checked edits.
---

Use the connected hosted server. Call `get_info` to establish the workspace and
discover current adapter capabilities. Open `open_crm_workspace` when the user
wants the interactive browser. Projects and items can be attached through
`fieldwork://project/{id}` and `fieldwork://item/{id}` resources and mentions.

## Find and bind the item

Use `list_projects`, then `list_items` with the selected project and title query.
Search matches item titles: use the address or the exact "Map area · lat, lon"
text. Never substitute a frozen item when the requested review names its replacement.
Call `get_item` and bind its ID, project, adapter, current revision, source image
identity and the selected object ID. Page through labels when needed.

Read `get_item_image` with `variant: source` before making visual claims. This
returns the item's immutable image through authenticated MCP; do not request a
workspace token in chat or place one in a URL. A review image is supporting
context; the original image establishes visible evidence. No source image or
model bytes are changed by this workflow.

## Review objects

Keep classification, review status and geometry coverage as separate decisions.
A roof screening positive does not establish a rigid building roof. Tensioned
canvas covers use the explicit `shade-sail` subtype and `Fabric shade sail`
label. For other classes follow [object-types.md](references/object-types.md)
and [vegetation.md](references/vegetation.md). For finer groups or splits read
[object-segmentation.md](references/object-segmentation.md).

Inspect clean imagery first, then the current saved annotation and each edge and
corner. A geometry improvement must reduce a visible mismatch against the
class-specific boundary; smaller area, fewer vertices or model confidence alone
does not establish accuracy. Preserve roof steps, detached structures, foliage
gaps, holes and visible internal seams. Record occluded or unresolved edges.

The hosted server exposes canonical CRM CRUD and image reads. Before polygon
edits, obtain the actual current outline and source coordinate transform from
the canonical item/export. Never guess a transform from image dimensions or
reconstruct missing source polygons. If preview or split tools are available in
the connected tool catalogue, use them and inspect their proposals. Otherwise,
show a reviewable proposal with supported coordinates before a canonical save;
do not claim a preview or split happened when only a classification was saved.

Agent reviews remain unchecked machine feedback. They do not establish human
geometry verification, building measurements or training eligibility. Preserve
checked, imported and reference evidence; honor any canonical protection error.

## Save and verify

For an independent assessment, use `save_agent_review` with the item ID, optional
annotation ID, parent item's current `expectedRevision`, stable agent `reviewer`
(for example `codex`), decision and evidence notes. Decisions are `approved`,
`rejected`, `needs_adjustment` or `uncertain`. An approved vegetation annotation
may include `vegetationType` (`grass`, `hedge`, `shrub` or `tree`).

This appends `item.agentReviews` with a server-stamped `agent:fieldwork:<reviewer>`
actor and a snapshot of the evidence. It does not change human status, notes,
geometry checks or training eligibility. Changed evidence makes the assessment
stale; another assessment retains history and supersedes the previous one from
that agent on the same target. Human reviews remain independent in the CMS UI.
For projects ending in `-review`, this is the only agent review write; ordinary
item, annotation, label and project edits remain refused. Never mark an agent
approval as a human approval or erase either reviewer's history.

Use `save_annotation` for the selected annotation and its **parent item's current
expectedRevision**. Use item/project/label saves only for changes in those
documents. Preserve unrelated fields, geometry, provenance, relationships and
source references. Deletes are destructive and need authorization for that
specific deletion. Existing user authorization governs ordinary edits.

Read-only OAuth connections omit save/delete tools. Editing requires connecting
with `fieldwork:write` and choosing editing in the authorization form. Never try
to bypass a connection scope or a revision/protection rejection.

On a conflict, reload and reconcile the new document; do not retry a stale save
or overwrite concurrent work. After a save, call `get_item` again, check the new
revision and verify the intended object fields. Report actual saved changes,
unchanged geometry and remaining uncertainty accurately. Use before/after
overlays only when they are produced from verified geometry and the original image.

The deployed editor has no per-item URLs. Its fallback path is to choose the
project, search the exact title, open Image view with "Saved feedback + Fieldwork",
filter the object class, find the object ID in Inspector and press Save changes.

## Improve the workflow

Keep item/object identities, revisions, source binding, inspected edges, proposal
coordinates and unresolved findings with the review. Generalize demonstrated
misses into the relevant reference, and validate improvements on another object.
User feedback informs the review but is not automatic ground truth. Do not run
extraction, training or source migration to perform a review.
