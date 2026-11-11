# Source-bound MCP workflow

Run in the user's addr checkout. Prefer connected addr tools; reconnect an
existing client if discovery lacks newly added tools. Inspect the returned
native image blocks, not only structured numbers.

## Relevant calls

- `low_confidence_roofs(limit, offset)` excludes saved/protected/held-out captures.
  For an existing saved draft, use its explicit scene/object IDs.
- `scene_roof_review(scene_id, object_id, include_source=true)` reads the source
  prediction and wider original RGB, not subsequent saved polygons.
- `inspect_roof_boundary(scene_id, object_id, edge_index?, preview_id?)` reads the canonical
  current outline/revision, clean RGB, source-pixel grid and numbered vertices.
  `edge_index` focuses one outer edge with neighboring corners. With an
  immutable `preview_id`, it renders the proposed boundary in amber with
  native pixels and indices; `outline_role` is `proposed`, while `binding`
  still identifies the current saved polygon. Stale/foreign previews refuse
  inspection. Use this after preview, before applying.
- `preview_roof_polygon(scene_id, object_id, proposed_outline_pixels, notes,
  uncertainty, reviewer)` accepts `{ring:[[x,y],...],holes:[[[x,y],...],...]}`:
  open rings in original PNG pixels, x-right/y-down. It returns registered
  comparison images, exact bindings, `preview_id` and `can_apply`.
  Include unresolved edge/corner limits in `notes` so they remain visible in the
  CMS after saving; the separate `uncertainty` field stays in the preview sidecar.
- `apply_roof_polygon(preview_id)` saves that inspected polygon as an unchecked
  machine draft; source/revision/authorship changes refuse the write. Reload
  after an uncertain save instead of blindly retrying.
- `roof_line_feedback(scene_id, object_id)` reloads saved boundaries, revision,
  source identities, notes/provenance and editing protection state.

For other scene classes, `object_feedback(scene_id, object_id?)` reads canonical
source-pixel outlines and class/protection state. `inspect_object_boundary`,
`preview_object_polygon` and `apply_object_polygon` use the same arguments and
native-image audit flow as their roof equivalents; their binding additionally
includes `object_kind`. They preserve checked/imported/protected work and save
unchecked machine tier 3 drafts. Generic geometry transactions reopen only the
edited class and clear estimates derived from its previous footprint. Existing
roof tools accept buildings only; do not relabel another object to use them.

## Same-class finer segmentation

- `preview_object_split(scene_id, object_id, children, group_label?, notes?,
  uncertainty?, reviewer?)` accepts two to eight children, each with a stable
  `key` and `outline_pixels:{ring,holes}`. Optional fields are `label`, `notes`
  and `role` (`object` or `unresolved-residual`). Child class and saved IDs are
  server-controlled. Rings are open and remain in original image pixels.
- Children must preserve the current source's exact covered area and holes,
  without positive-area overlaps or extending outside it. Shared seams are
  allowed. Every child needs at least four source square pixels. A geometric
  area tolerance is numerical, not a boundary-accuracy claim.
- Optional `group_label` proposes a new shared flat group. Without it, children
  retain the source's existing group, if any. Labels can distinguish `Crown
  patch A`, `Hedge section B` or an unresolved remainder without inventing a
  plant subtype enum. No nested group hierarchy is created.
- The preview contains the clean original, native-pixel grid and indexed
  proposed parts, exact source/revision bindings, stable child IDs, coverage
  diagnostics and `can_apply`. Actually inspect its image.
- `inspect_object_split(preview_id, child_key?, edge_index?)` shows the whole
  partition or a native-pixel inspection of one child. Select `child_key`
  before `edge_index`. Audit every child, hole and new internal seam before save.
- `apply_object_split(preview_id)` uses canonical `POST /api/scenes/object-split`
  with only `{preview_id}`. It revalidates immutable artifacts and source
  bytes under the revision lock, retains the source as `superseded`, and saves
  all same-class children atomically with reciprocal replacement lineage.
  Saved notes include the split's uncertainty and explicit residual role.
- Active physically attached objects block automatic split application. Do not
  detach/reparent them through raw review writes; inspect the existing instance
  editor's proposal or defer. Protected/human/imported feedback remains guarded.
- Reload the retained source and each returned child with `object_feedback`.
  Saved machine split children use the generic object inspector/polygon tools
  for further refinement; later same-class splits retain their ancestry.
  Source-model roof-review cards do not contain review-created child predictions.

Read [object-segmentation.md](object-segmentation.md) for operation selection and
the disconnected-mask correction limit. This is an unchecked partition draft,
not an individual-tree inventory or measured-height update.

In `update_draft_roof_feedback`, top-level `outline_pixels` binds the existing
outline; replacement geometry is `changes.outline_pixels`. Prefer preview/apply.
Detailed contracts: `docs/mcp-extensions.md`, `scripts/ml/scene_polygon_feedback.py`
and `src/lib/roof-feedback-patch.ts`. Canonical saves preserve history, reject
invalid polygons and invalidate estimates derived from the old boundary; source
predictions remain in the CMS's Prediction/Compare views.

## Real SDK fallback

When no addr tool namespace is connected, `scripts/mcp_call.py` uses the project's
official MCP SDK and stdio server. It discovers tools, calls one named polygon review tool
and saves structured responses/native images in the chosen case directory. It
does not write review files directly or print image base64/credentials.
`--out-dir` selects client receipts/copies only; immutable server inspection
and preview sidecars still live under `data/reviews/eyes/`. For isolated
temporary-only tests, use existing evidence or pure render helpers directed
to temporary paths; do not mistake that for a canonical API binding test.

From the checkout, use JSON argument files to avoid shell escaping:

```sh
scripts/ml/.venv/bin/python .opencode/skills/addr-roof-tracing/scripts/mcp_call.py \
  inspect_roof_boundary --arguments-file /tmp/roof-inspect.json \
  --out-dir data/reviews/eyes/roof-tracing-example
```

Inspection arguments: `{"scene_id":"SCENE_ID","object_id":"OBJECT_ID"}`.
For preview, include the ring and holes inside `proposed_outline_pixels` along
with those IDs; top-level `ring`/`holes` are not tool arguments.
For apply, the argument file contains only the inspected `preview_id`.
`--repo` selects another checkout; existing `CRM_TOKEN`/`ADDR_APP_URL` are
forwarded without printing them. Local API access may require the normal sandbox
escalation; an unavailable API is not permission to edit data files directly.
Read `docs/local-services.md` before lifecycle actions; preserve frozen workers.

Keep case arguments/receipts under the case directory. Do not put a particular
roof's coordinates into general skill instructions or model implementation.
