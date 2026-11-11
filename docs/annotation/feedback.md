# Pulling Fieldwork feedback

Run one command from the repository root:

```sh
task pull
```

It downloads new Fieldwork edits and syncs compatible feedback into the local
scene reviews. The same command is safe to repeat. Output gives the status,
cursor, counts, local scene links and any conflicts. `feedback:pull` remains an
alias. The equivalent MCP tool is `pull_fieldwork_feedback()` with no arguments.

Incoming geometry stays a draft until checked in the local scene UI. Pull never
starts training or changes the frozen source bundle, imagery or model files.
Notes-only changes preserve existing local checks.

If some scenes conflict, compatible scenes still sync. The command exits nonzero
and reports the unresolved scenes without overwriting them. Resolve the reported
conflict, or restore the local app if it is unavailable, then run `task pull`
again. Downloaded feedback and completed local saves are preserved.

## Options

```sh
# Download and inspect a full preview without applying local feedback.
task pull CLI_ARGS="--dry-run"

# Download only, including while the local app is stopped.
task pull CLI_ARGS="--skip-preview"

# Limit this pull to one cloud scene item.
task pull CLI_ARGS="--id SCENE_ITEM_ID"
```

Dry-run and download-only modes never resume a pending apply. Normal pull resumes
an interrupted apply automatically; no plan ID is needed.

The direct Python command has the same behavior:

```sh
scripts/ml/.venv/bin/python -m scripts.ml.fieldwork_feedback pull
```

The wrapper invokes `go run ./cmd/annotate` using current source and argument
arrays. Use the existing Python environment and Go installation. Defaults target
Mooroolbark and the local app at `http://localhost:3008`; relative paths resolve
from the repository root.

| Setting | Flag | Environment / default |
| --- | --- | --- |
| Project | `--project` | `FIELDWORK_PROJECT` / `mooroolbark` |
| Frozen bundle | `--snapshot` | `FIELDWORK_SNAPSHOT` / `data/annotation-snapshots/mooroolbark-23591d058ab7` |
| Mirror | `--dir` | `FIELDWORK_SYNC_DIR` / `data/fieldwork-sync/PROJECT` |
| Packet | `--file` | `FIELDWORK_FEEDBACK_FILE` / `MIRROR/feedback.json` |
| Cloud URL | `--url` | `ANNOTATION_URL` / `https://fieldwork-annotations.ben-ebsworth.workers.dev` |
| Cloud token file | `--token-file` | `FIELDWORK_TOKEN_FILE`; otherwise `ANNOTATION_TOKEN`, or `deploy/annotation/admin-token.txt` for the default cloud origin only |
| Local app | `--local-url` | `ADDR_APP_URL` / `http://localhost:3008` |
| Local token file | `--local-token-file` | `FIELDWORK_LOCAL_TOKEN_FILE` / none |

Project IDs are validated before default paths are derived. A custom cloud URL
requires explicitly configured authentication; the default production token file
is never automatically selected for another origin. Local requests use a separate
optional bearer file. Tokens are not printed or stored in packets/checkpoints.

## What the result means

`status` is `synced`, `up_to_date`, `conflicts` or `incomplete`. Explicit dry-run
and download-only requests return `preview` and `downloaded`, respectively.
Conflicts and incomplete runs exit nonzero. `counts` includes downloaded changes,
packet items, applied scenes, already-applied scenes recovered on retry,
receipt-only updates and conflicts. `reviewUrls` gives up to twenty local scene
links. Normal success output omits geometry diffs; dry-run and advanced preview
expose the full plan, including draft and notes-only object counts.

The incremental mirror resumes its committed cloud cursor. The packet contains
the current cloud difference from the frozen base, so it may still contain items
when there are no new cloud changes. Existing local receipts prevent repeated
feedback from creating duplicate scene saves. Workspace title, notes, tags and
queue status have no direct local scene equivalent and are retained as receipt
metadata when changed.

All source, model, image, revision, local-edit and training authority checks remain
in force. Unsupported changes, deleted items, source mismatches and overlapping
local edits are reported or quarantined. Compatible scene selection happens
locally from the saved packet without extra cloud pulls.

## Interrupted pulls

A process lock covers the entire pull so concurrent CLI and MCP calls cannot
replace each other's packet or retry checkpoint. The OS releases the lock when a
process exits; the `.pull.lock` file can remain on disk.

Before applying, the wrapper atomically saves a small `.apply.json` checkpoint and
an exact `.pending.json` packet copy beside the normal packet. It binds the packet
SHA-256, server/project, local URL, selected item and plan ID. The next ordinary
pull verifies these files and resumes the saved apply before fetching newer data.
Local per-scene receipts and journals avoid duplicate saves, including a failure
between saving a scene and publishing its receipt. The separate packet copy lets
dry-run/download-only refresh the normal packet while preserving the pending run.

An automatic re-preview requires explicit server evidence: either the initial
plan became stale **before any write**, or the server has recovered earlier
receipts and verified that the remaining scenes have no apply journals. Successful
prefix counts are retained when replacing that plan. An uncertain apply keeps
its original checkpoint. If that run cannot resume because of a real local
conflict, the command reports it and preserves the evidence. Resolve that conflict
or restore the app, then rerun `task pull` with the same options. Do not remove or
edit checkpoints to bypass a conflict.

Commands are bounded to ten minutes for download and two minutes for each local
request. A timeout can occur after a scene was saved; rerunning `task pull` uses
the retained plan safely. Errors include a bounded diagnostic tail. A corrupt
checkpoint, changed preserved packet, or different server/project/local app fails
before a replacement download.

Receipts and journals live under
`data/reviews/fieldwork-feedback/<origin-sha256>/<projectId>/`. This directory and
`data/fieldwork-sync/` are ignored by Git. Original asset blobs use the existing
authenticated HTTP/CLI path in [sync.md](sync.md); this workflow does not send
imagery through MCP.

## Local review and training

Substantive imported changes have `fieldwork_import.pending: true`. A subsequent
explicit local confirmed + geometry-reviewed decision, or explicit rejection,
clears that pending state. Applying feedback itself never grants human authority.

Pending imports and `provenance.source` values beginning `machine:` are excluded
from outline references, explicit false positives, supplemental proposal labels,
roof targets, human reference tiers and calibration counts. These active objects
also keep their class layer pending, preventing accidental all-negative COCO
images. Clearing a pending marker only permits the existing status, geometry,
class-completeness and geography checks to evaluate the object. Roof family,
linework and height approval remain independent; provenance is not invented.

Cloud actor/timestamp and source revision are audit evidence. A shared actor such
as `workspace` does not establish a named human reviewer.

Use the returned links to inspect and check drafts in the local scene UI. When
ready, explicitly use the existing export and training tasks:

```sh
task scene:training
task scene:train-roofs
```

Pull never starts these tasks.

To run training away from this checkout, publish the exported datasets and diffs
with `task training:publish` and fetch them with `task training:pull`; see
[training artefacts](training-artefacts.md). Neither command starts training.

## MCP and advanced diagnostics

Use `pull_fieldwork_feedback()` for normal sync. Optional `dry_run: true`,
`skip_preview: true` and `item_id` correspond to the CLI options. Errors/conflicts
remain explicit in its returned status. Restart the local `addr-review` MCP
server/client connection after updating code so discovery refreshes the tools.
These local Python tools are separate from the cloud annotation MCP tools.
The outbound direction (`plan_fieldwork_push`, `apply_fieldwork_push`,
`fieldwork_push_status`, `task push`) and the training-artefact tools are
described in [local-to-fieldwork.md](local-to-fieldwork.md).

### Push receipts and the schema-2 packet

`annotate feedback` reads the push receipts under `MIRROR/push` (override with
`--push-receipts DIR`). When that directory holds item or scene receipts the
packet is **schema 2**; without receipts (the live mirror today, or a directory
that only holds a dry-run plan) the packet stays schema 1. The packet format is
unchanged in that case, but the local intake is not: old-frame recovery and the
`metadata.addr` allowlist apply to schema-1 entries too, so after local frames
move (the 284 promoted or widened scenes) a live pull writes Fieldwork edits on
frozen items as recovered pending drafts where it used to report a frame
conflict (see "Pull behaviour on the live project changed" below). In schema 2
every mirror item our push wrote is compared against the receipt, not the
frozen bundle:

- **Own writes are skipped**: a mirror item at the receipt's revision whose
  document equals our last write, and carries nothing beyond the baseline plus
  the supersede mark, produces no entry. A push followed by a pull yields
  0 entries, 0 quarantined. A Fieldwork edit that a `--allow-unrecovered` push
  embedded in its mark is still delivered.
- **Baselines**: a generation id is baselined on the receipt's pristine document
  and evidenced from `push/assets/<sha>`; a supersede-marked frozen id keeps the
  frozen projection as baseline. Every entry carries `binding`
  (`kind: push|frozen`, `runId`, `receiptSha256`, `receiptRevision`,
  `generation {modelSignature, reviewRevision, imageSha256, width, height}`);
  kind and receipt sha are part of the packet digest.
- **Quarantine**: a generation id without a receipt is `missing_push_receipt`;
  a remote `source` (or scene binding) that differs from the receipt is
  `receipt_binding_mismatch`. Frozen-id quarantines are unchanged.

The local app side (`src/lib/annotations/feedback.ts`, `feedback-store.ts`)
accepts schema 1 and schema 2. `validateFeedbackBundle` mirrors Go
`feedback.Read`: a schema-1 entry carries no `binding`, every schema-2 entry
carries one, and the digest covers `binding.kind` and `binding.receiptSha256`.
Item metadata is compared after stripping exactly `metadata.addr.supersededBy`,
`metadata.addr.stale` and `metadata.addr.unrecoveredFieldwork` (the keys a push
mark writes), so a supersede-marked frozen item previews as a receipt-only
change (`item.tags`, `item.metadata`) rather than a conflict; any other
metadata change still conflicts. A packet may carry several entries for one
scene (its marked frozen item and a generation item, both edited); only item
ids must be distinct. `feedback-store.ts` groups entries per scene and applies
them in a deterministic order — entries whose source model is the scene's
current frame first, then superseded frames through recovery, ties by item id —
each later entry planned against the review its predecessor commits (the plan
binds that review's content, since a commit's timestamps and history are not
known in advance) and each producing its own journal and receipt. An
interrupted apply resumes in the same order; overlapping edits from the two
items conflict on the second entry instead of overwriting the first. `--id` /
`item_id` still selects a single item when that is wanted.

**Old-frame recovery.** When the packet's model frame differs from the local
scene's, `prepareFeedbackScene` carries both the last accepted cloud state and
the edited state onto the current frame with the scene editor's own
`recoverSceneReview`. The Fieldwork edit is the set of editable paths that
changed **on the old frame** between those two cloud states (a replaced seed's
paths move to the detection whose `replaces` names it); only those paths are
read back from the recovered states, so a dragged outline lands shifted on the
current frame while every other difference between the recovered states is
ignored. The recovered previous state is the local baseline for the overlap
rule. Two frame
relationships are accepted as a single hop: the verified 512→1024 context
expansion (`recoveredFrom.method: "expansion-256"`; rings and captured outlines
shift by +256, `roof_line_edits` vertices/edges stay in metres, seed decisions
follow `replaces`) and an identical pixel frame under a new model signature
(`"same-frame"`, an id-based passthrough with no shift). When the frozen frame
does not reach the current one in a single hop, `resolveRecoveryChain` walks
the scene's `data/scenes/<S>/models/<sig>.json` snapshots breadth-first for the
shortest chain of such hops (bounded by `MAX_RECOVERY_HOPS = 4`, candidates in
signature order, so the result is deterministic) and `recoverSceneReview` runs
once per hop, so an outline still shifts exactly once. Every hop is exact: a
same-image hop needs the same id, image sha, size, `pixel_to_meters` and
`meters_to_pixel`; an expansion hop needs `context_expansion` to name the
previous snapshot's signature and image and the frame relation to check; a
snapshot file is only a hop when its `id` and `model_signature` match the scene
and its file name. This is how the 30 widened `house-*` scenes whose 1024 model
names a same-image 512 re-extraction rather than the frozen model recover
(`house-25833`: `5fa706ea…` → `618fa7db…` same-frame → `8ecf72fc…`
expansion-256). A multi-hop result is recorded as `recoveredFrom: {
modelSignature, imageSha256, method: "chain", hops: [{ modelSignature,
imageSha256, method }, …] }` (the frozen frame first, then each snapshot landed
on in order); a single hop keeps the plain `method`. Anything else — a missing
or unreadable intermediate snapshot, a hop whose image sha or frame does not
chain exactly, an unverified recapture — is reported as the conflict
`frame_changed_unrecoverable: …`; nothing is applied and the edit stays on its
item and in the change feed. Recovered edits land exactly like current-frame
imports: `needs_adjustment`, `geometry_reviewed: false`,
`height_verified: false`, `roof_line_edits.checked: false`, roof derived state
dropped, `fieldwork_import.pending: true` plus `recoveredFrom`; the receipt
records `recoveredFrom` too. Imported geometry stays a draft
(`fieldwork_import.pending: true`) in every schema; pulled values are Fieldwork
edits whose agreement with the local review is pending, never human labels.

**Cross-scene hops are not a chain method yet (2026-09-30).** A recapture is a
new scene (`lot-<address_id>`, written by `scripts/ml/scene_lot.py --capture
--carry-from <source scene>`), not a new snapshot of the source scene, and
old-frame recovery cannot reach it: `recoveryMethod` accepts only
`expansion-256` and `same-frame`, and `resolveRecoveryChain` walks the
`models/<sig>.json` snapshots of one scene id (`chainSnapshot` refuses a
snapshot whose `id` is another scene's). A recapture keeps the source scene
live: the source scene's `latest.json` and `review.json` are never rewritten,
and a Fieldwork edit on its item still lands on the source scene under the
rules above, never on the recapture. The georeferenced-affine carry that would
re-project a saved review onto a recaptured frame is written as a module,
`src/lib/scene-frame-transform.ts` (tested by
`scripts/test-scene-recapture.mjs`), and is not wired into the scene store,
the editor or this feedback path yet. Until it is, a recapture's frame is
recorded (`frame_transform`, measured against the pre-registered design) but
nothing is carried onto it. The design, the pilot and the decisions waiting
on the user are in `docs/recapture-carry-2026-09-30.md`.

**Pull behaviour on the live project changed.** Neither old-frame recovery nor
the `metadata.addr` allowlist is gated on the packet schema, so the live
schema-1 pull (no push receipts; Fieldwork edits on the frozen
`addr-scene-<S>` items) now behaves differently on every scene whose local
frame moved on from the frozen one: the expanded `house-*` scenes (31 in the
frozen inventory, 512→1024; one via `expansion-256`, 30 through the two-hop
`chain`) and the same-image `mooroolbark-*`
promotions (253, `same-frame`) previously failed closed on the frame/model
mismatch; a Fieldwork edit on their frozen item now previews and, on apply,
writes to `data/scenes/<S>/review.json`. What it writes is only the paths the
Fieldwork edit changed on the old frame: a notes-only edit writes the note and
nothing else (no draft, no status change); an outline, status, label or model
edit writes those fields as a pending draft (`needs_adjustment`,
`geometry_reviewed: false`, `height_verified: false`,
`fieldwork_import.pending: true` with `recoveredFrom`). Attributes that differ
between the frozen and the current model on an id the edit did not touch
(`parent_id`, `group_id`, `model.base_m`, `roof_type`, `roof_parts`,
`roof_lines`, `roof_annotation`, `roof_geometry_note`, …) are model drift: they
are never shown as incoming, never written and never deleted, and objects the
recovery re-surfaces only for parent context are not imported. Scenes whose
frame is neither relation still conflict with `frame_changed_unrecoverable` and
nothing is applied.

For diagnostics, `preview_fieldwork_feedback` reads a saved packet without
applying it. `apply_fieldwork_feedback` remains an advanced tool requiring the
exact `plan_id` from that preview. Both support `packet_file`, `local_url`,
`local_token_file` and optional `item_id`; the item selection must match on both
calls. Their CLI equivalents remain available:

```sh
task feedback:preview CLI_ARGS="--file PATH"
task feedback:apply CLI_ARGS="--file PATH --plan-id PLAN_ID"
```

The local API is `POST /api/scenes/feedback`, protected by the existing scene
principal. Preview accepts `{ "bundle": PACKET_JSON, "mode": "preview" }`; apply
accepts `{ "bundle": PACKET_JSON, "mode": "apply", "planId": "PLAN_ID" }`.
Preview returns `schemaVersion`, `planId`, `bundleDigest`, `canApply`, `scenes`,
`quarantined` and `counts`. Apply adds `complete` and per-scene `applied` results;
partial failures also return `retryPlanId`. The Go client preserves a small
sanitized error envelope on stdout with a nonzero exit. Clients must preserve
cursor strings, source hashes and the packet. These operations do not write
cloud annotations.
