# Addr MCP extensions

Addr follows the optional [OpenAI MCP Extensions specification](https://github.com/openai/mcp-extensions/blob/main/docs/spec.md).
The Python review server uses `openai-mcp-extensions==0.1.0` alongside MCP 2.x;
the Go annotation server implements the same composer/resource wire contract.
The deployed hosted gateway now uses the TypeScript extension library over the
existing annotation API. Its OAuth endpoint, private plugin and verified tool/UI
contracts are documented in [Hosted Addr CRM MCP](annotation/hosted-mcp.md).

## Local review server

Install dependencies and build the self-contained MCP App:

```sh
task setup:ml
pnpm install
pnpm build:mcp
scripts/ml/.venv/bin/python -m scripts.ml.mcp_server
```

Run the stdio command from the repository root. `.opencode/opencode.json` already
configures it as `addr-review`. Reconnect your client after updating the server.
The compiled HTML is saved at `scripts/ml/mcp_app.html`; rebuild it after editing
`apps/mcp-review` or upgrading its SDK dependencies. It contains all JavaScript
and CSS and needs no CDN, browser credentials, or direct connection to the web app.

The **Address reviews** tool (`open_review_workspace`) accepts `{}` and advertises
both global sidebar and thread entrypoints. Its UI resource is
`ui://addr/review-workspace`, served as `text/html;profile=mcp-app`. The first
render consumes the initial tool result. Search uses the app-visible
`app_search_resources` tool; opening a result uses `resources/read`. Explicitly
adding a result to the composer supplies its resource link as model context.
The app applies the host theme and supports host deep links at
`/addresses/{address_id}` and `/scenes/{scene_id}?object={object_id}`. Scene
selection lists building objects. Inspecting a roof calls `scene_roof_review`
and displays its native source/overlay card, diagnostic binding state, saved
feedback revision and agent observations. The review queue calls
`low_confidence_roofs`; an explicit form save calls `record_roof_observation`.
The source editor link opens `/scenes?scene=…&object=…` with editing available.

Composer mentions use `search_mentions({"query":"cambridge"})`. The extension SDK
advertises `_meta["openai/extensions"]["mentions/search"]` and
`_meta["ui"]["visibility"]: ["app"]`. Results are returned in
`structuredContent.items` as standard MCP resource links. An empty query returns
a bounded selection; matching is case insensitive, supports address/scene IDs,
and returns up to 20 addresses and saved scene models in total. Unextracted
scenes and temporary QA captures stay out of the picker.

| Resource template | Content |
| --- | --- |
| `addr://addresses/{address_id}` | Generated address record and its provenance note; record fields retain model, CRM and reviewer provenance. |
| `addr://scenes/{scene_id}` | Current saved scene model and its estimate provenance note. |

Search and resources read saved state only. Default roof inspection renders
saved source evidence without inference. Agent notes are immutable tier 3
sidecars; they do not grant approval or enter training exports. Clients without
MCP Apps can read each tool's JSON text fallback. Roof queue, cards, observations
and saved-feedback tools expose direct structured content and output schemas.

`roof_observations(scene_id, object_id)` reads notes without rendering. The CMS
shows the same notes in Object details through the scoped, read-only
`GET /api/scenes/roof-observations?scene=…&object=…` endpoint. Notes for different
model bytes, image or object geometry are excluded; altered note payloads fail
closed. Refresh observations after an agent saves a note.

For bounded scene edits, `update_draft_roof_feedback` delegates to
`PATCH /api/scenes/roof-feedback`. Supply `scene_id`, `object_id`,
`expected_revision`, `model_signature`, `image_sha256`, exact `outline_pixels`
(`ring` and `holes`), and `changes`. Read `roof_line_feedback` for saved revision,
image and outline bindings; an unsaved scene's original object is available
through its scene resource, with revision zero from the scene feedback read.
`changes` allows `notes`, `review_issues`, and `roof_line_edits` containing
`vertices_meters`, edges `{a,b,kind}` (`ridge`, `hip`, `valley`), and optional notes.
It also accepts a replacement `outline_pixels` with optional `polygon_preview_id`;
use the polygon preview/apply workflow below for those edits. Polygon and roof
line changes cannot be combined in one patch.
The API captures/preserves the starting estimate; callers cannot supply a
baseline, checked state, approval or source fields. Issues/lines reopen the
target for review and remain unchecked. Notes-only updates retain prior checks.
Other objects, provenance, pending Fieldwork imports and history are preserved.
Stale revisions, models, image bytes, outlines and inactive objects are refused.
After a conflict or lost save response, reload saved feedback before retrying.
Writes require `crm.roof.write`; reopening a completed building layer also
requires `crm.object.write`, as enforced by the canonical scene transaction.

Before tracing, use `inspect_roof_boundary(scene_id, object_id, edge_index?, preview_id?)`.
Its native card has clean original RGB, a grid labelled in source pixels, and
the current outline with vertex/outgoing-edge indices. Optional `edge_index`
focuses an outer-ring edge and its adjacent corners. Hole indices use `hN:v/e`.
The response contains the exact current ring/holes, source/panel affine maps,
saved revision/image/model bindings, and human-protection state. Nearest-neighbour
display magnification adds no source detail. Inspect the entire perimeter,
including unchanged edges, and retain explicit uncertainty where the eave is
hidden. Only immutable inspection sidecars are written.

The same source-pixel inspection/preview/apply path now supports every scene
class through `object_feedback`, `inspect_object_boundary`,
`preview_object_polygon` and `apply_object_polygon`. Generic bindings and
inspection/preview results explicitly include the current object kind. Their
generic preview IDs must be applied with the matching generic tool. Roof tools
remain building-only. `/api/scenes/object-feedback` GET reads current saved
polygons (or source objects when no review exists); PATCH requires a replacement
outline, with optional notes/issues and preview ID. It uses the shared strict
source/outline/revision/fingerprint protections and canonical scene transaction.
Class changes, roof-line writes and notes-only patches are outside this API.
Generic geometry edits reopen only their object class, clear old footprint-based
class estimates, and remain unchecked T3 machine drafts. Parent/group links,
source models/images, unrelated reviews, history and pending imports are
preserved. Class-specific write scopes use the editor's existing `objectScope`;
reopening a completed layer additionally requires `crm.object.write`.

After creating a polygon preview, pass its `preview_id` to the same inspection
tool to audit every proposed edge and corner with the nearest-neighbour RGB
grid and numbered vertices. The native card is labelled **proposed/unchecked**;
`outline_role` identifies whether `outline_pixels` is current or proposed.
`binding.outline_pixels` always remains the exact current saved outline. The
preview manifest/content/card hashes and complete scene/object/source/revision
binding must match; foreign, stale or altered previews are refused. This
inspection never saves the proposal or changes the current review.

For agent polygon tracing, `preview_roof_polygon(scene_id, object_id,
proposed_outline_pixels, notes?, uncertainty?, reviewer?)` accepts an outer
`ring` and `holes` in original image pixels (x right, y down). Rings are open:
omit the repeated closing vertex. The native card compares original RGB,
current outline and proposed outline in one registered crop. The structured
result retains exact source geometry, image/model hashes, saved revision,
source-to-crop transforms and explicit uncertainty. It creates immutable
diagnostic sidecars only; no model, source PNG or saved review is changed.

After inspecting that card, call `apply_roof_polygon(preview_id)` to save the
exact proposal as an unchecked draft. The tool verifies the sidecar hashes and
unchanged source/current outline/revision, then calls the same canonical PATCH
transaction with `changes.outline_pixels` and `polygon_preview_id`. The server
records machine agent provenance (tier 3), clears outline/height checks and
derived roof estimates, and preserves prior line observations on their old
outline without rebinding them. Human-checked geometry, checked line feedback
and protected imports are refused; `can_apply` and its reason are shown in the
preview. This does not mark a roof reviewed or make it training eligible.
Changing a polygon later requires a fresh preview and revision.

For finer object parts, `preview_object_split(scene_id, object_id, children,
group_label?, notes?, uncertainty?, reviewer?)` accepts two to eight same-class
children, each with a unique `key`, `outline_pixels:{ring,holes}`, and optional
`label`, `notes` and `role` (`object` or `unresolved-residual`). Child class and
saved IDs are server-controlled. Parts must cover the current source exactly,
preserve its holes, avoid positive-area overlap and remain inside it; each needs
at least four source square pixels. Unresolved coverage stays in an explicit
residual part. This conservation check does not prove biological tree instances.

Inspect the native whole-partition card and each child's edges/holes with
`inspect_object_split(preview_id, child_key?, edge_index?)`. After that audit,
`apply_object_split(preview_id)` delegates to `POST /api/scenes/object-split`
with only `{preview_id}`. Immutable artifact/source hashes and the revision are
verified under the canonical save lock. The source remains as `superseded`;
unchecked T3 children carry stable IDs and reciprocal replacement lineage.
Group membership is inherited, or a supplied label creates a new shared flat
group. Groups have no nested hierarchy. Active physically attached objects
block this automatic path until their assignment is resolved separately.

Reload the source and every returned child with `object_feedback`. Trusted
machine split children can be refined with the generic polygon tools and split
again; original-prediction roof tools remain source-object-only. Human-checked,
imported, foreign or stale work stays protected. A split preserves coverage, so
it cannot remove an erroneous connector across a gate/path. The reusable
[tracing skill](../.opencode/skills/addr-roof-tracing/SKILL.md) documents class
targets, vegetation seams and this disconnected-mask replacement limit.

For cohort roof feedback, first call `cohort_roof_feedback(address_id, cohort)`.
Its structured result includes the saved revision, boundary/model signatures,
stale flags and CMS link. `apply_roof_feedback` now requires an explicit cohort,
`expected_revision` and `boundary_signature`; it delegates to the canonical
`/api/reviews` API for authentication, validation, history and locked saving.
Supported actions are `notes`, `reject_cut`, `restore_cuts` and `set_status`.
Only `unreviewed` and `needs_adjustment` drafts can be saved through this tool;
human approval and rejection remain in the CMS. Supply `model_signature` for
cut rejection or when retaining saved roof/object annotations. Existing geometry,
issues and prior checks are retained, while stale/unbound feedback is refused.
Omitting notes preserves them; `notes: ""` explicitly clears them. The tool never
writes `feedback.json` directly, falls back to local files, or starts training.
Old calls lacking these explicit identities must read the new prerequisites
before saving. Reconnect clients to refresh discovery.

## Fieldwork annotation server

The authenticated Streamable HTTP `/mcp` endpoint and `annotate mcp` stdio bridge
expose the same app-visible `search_mentions` contract for projects and items.
Results resolve through `resources/read`:

| Resource template | Content |
| --- | --- |
| `fieldwork://project/{id}` | Current project document with its revision. |
| `fieldwork://item/{id}` | Current item, annotations and labels, with revision information. |

Resource reads pass through the existing authentication and Origin middleware
and shared service validation. Protected source asset bytes remain on their
authenticated HTTP endpoints. Mention search and reads do not write documents
or advance the change feed. Existing revision requirements for edit tools are
unchanged. The Go endpoint retains MCP `2025-11-25`, `2025-06-18` and `2025-03-26`
support; extension metadata does not claim support for a newer protocol.

## Verification

```sh
scripts/ml/.venv/bin/python -m pytest scripts/ml/tests/test_mcp_extensions.py scripts/ml/tests/test_mcp_fieldwork_surface.py -q
scripts/ml/.venv/bin/python -m pytest scripts/ml/tests/test_scene_polygon_feedback.py -q
scripts/ml/.venv/bin/python -m pytest scripts/ml/tests/test_scene_object_split.py -q
node scripts/test-object-split.mjs
go test ./internal/annotation
pnpm build:mcp
task test
task lint
task verify:ui
```

Sidebar and mention controls appear only in clients that implement the
corresponding extensions. Connecting a server and installing/publishing a
ChatGPT plugin are separate steps; this update changes the server and its
embedded resource, without publishing a plugin.
