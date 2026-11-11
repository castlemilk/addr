# Pushing local scenes to Fieldwork

`task push` publishes changed local scene frames and review state to Fieldwork
as **new immutable `addr-scene` items** and marks the older items of the same
scene `superseded-frame`. It is the local-to-cloud half of the loop described in
[local-to-fieldwork.md](local-to-fieldwork.md); `task pull`
([feedback.md](feedback.md)) is the cloud-to-local half.

A pushed item is the **model estimate plus the local review state as of
publication**. Nothing it carries is a human label: `review.updated_by`
(`local`, `machine:*`) and `provenance.source` are copied verbatim as audit
strings, object statuses are carried as they are, and Fieldwork values remain
"Fieldwork edits" whose agreement with the local review is pending until
`task pull` imports them as drafts.

## What push never does

- It never writes `properties.addr.overrides` (Fieldwork's own layer).
- It never calls `DeleteItem`, `DeleteLabel` or `DeleteAnnotation`
  (grep-tested in `internal/annotation/push`).
- It never replaces an existing item. The only write to an existing item is the
  supersede mark: a revision-checked `SaveItem` built from a freshly read
  document, changing `tags` (`+superseded-frame`) and
  `metadata.addr.{supersededBy,stale}` only; `source`, `annotations`, `status`,
  `notes` and `title` are re-sent byte-for-byte. The mark is attempted only
  while the freshly read item is still at the revision the plan observed (or
  at our own later write, or a document the local app already accepted via a
  feedback receipt); a Fieldwork edit that landed since planning yields
  `mark_conflict`, is left untouched and is never recorded as our write.
- It never pushes over unpulled Fieldwork edits unless `--allow-unrecovered`
  is given, in which case the new item records them under
  `metadata.addr.unrecoveredFieldwork`.
- It never writes to a non-loopback origin unless explicitly allowed. The Go
  CLI refuses `push` without `--dry-run` (and `save-*`, `delete-*`, `import`,
  `seed`, `rpc` write methods; `annotation-migrate upload|import` likewise) for
  any origin that is not localhost or a loopback IP unless `--allow-live` is
  passed or `FIELDWORK_PUSH_ALLOW_LIVE=1` is set. The Python wrapper refuses
  every spelling of the live host (case, `:443`, other ports, trailing slash)
  without `FIELDWORK_PUSH_ALLOW_LIVE=1`.

## Layers

```
annotate push            Go CLI (cmd/annotate/push.go) over internal/annotation/push
  └─ plan.go             classify scenes, project items, write runs/<runId>/items/*.request.json
  └─ apply.go            labels → assets → re-HEAD → SaveItem rev 0 → receipts → marks
  └─ receipts.go         data/fieldwork-sync/<P>/push/{items,scenes,assets,runs}
scripts/ml/fieldwork_push.py   task push / task push:status, live-origin guard, wrapper lock
scripts/ml/mcp_server.py       plan_fieldwork_push / apply_fieldwork_push / fieldwork_push_status
```

`migration.ProjectScene` is shared with the frozen import: the same projection
produces generation 0 (frozen bundle) and generation N (local
`LocalSceneFiles`), so annotations, normalised polygons and immutable source
bindings have one definition. `migration.GenerationSceneID(project, scene,
modelSignature, reviewRevision)` yields
`addr-scene-<S>-m<sig[:12]>-r<rev>` (`r0` without `review.json`).

## Commands

```sh
# Plan only. Network: ListChanges (mirror sync) and HEAD (asset existence). Nothing is written remotely.
task push -- --dry-run --url http://127.0.0.1:18131 --dir /tmp/addr-fieldwork-dev/mirror
task push -- --dry-run --scenes house-25822,roof_training_250-25018

# Plan and apply the clean part (create / skip_identical). Blocked scenes are reported, not written.
task push -- --url http://127.0.0.1:18131 --dir /tmp/addr-fieldwork-dev/mirror

# Resume or apply a saved plan. Refused when the local inputs of any planned scene changed.
task push -- --run 20260929T104740Z-0964132b --url http://127.0.0.1:18131 --dir /tmp/addr-fieldwork-dev/mirror

# Receipts only, no network.
task push:status -- --dir /tmp/addr-fieldwork-dev/mirror
```

Go CLI underneath (the table goes to stderr, the JSON summary to stdout so the
wrapper can parse it):

```sh
go run ./cmd/annotate push --project mooroolbark --dir MIRROR [--root .] [--scenes a,b] [--dry-run] [--run RUNID]
  [--mark-superseded=true] [--skip-outside-inventory] [--allow-unrecovered]
  [--review-history omit|split] [--concurrency 4] [--feedback-receipts PATH] [--all] [--limit 100] [--allow-live]
go run ./cmd/annotate push-status --dir MIRROR [--scenes a,b]
```

`--root` is the checkout that holds `data/scenes` (the verification below used a
read-only copy of two scenes). `--limit` is the mirror sync page size. A custom
`--url` never receives the implicit production token; pass `--token-file` or
`ANNOTATION_TOKEN`. A loopback `--dev` server (127.0.0.1, localhost, ::1) needs
neither, because it never checks a bearer.

## Actions

| Action | Meaning | Remote write |
| --- | --- | --- |
| `create` | new generation item; lists the live items it supersedes | SaveItem rev 0 + marks |
| `current` | the latest published generation (receipt, else frozen item) already carries `latest.json.model_signature` at `review.revision` | none |
| `skip_identical` | the id already exists with an identical document (ignoring `metadata.addr.push`); receipt written, marks proceed | marks only |
| `conflict_id_reuse` | the id exists with different content; never overwritten; both documents kept under `runs/<runId>/items/` | none |
| `blocked_remote_edits` | a live item of the scene is past its receipt revision and no local `FeedbackReceipt` (`data/reviews/fieldwork-feedback/<sha(origin)>/<project>/receipts/<sha(itemId)>.json`, `remote.sha256` = sha256 of the mirror document) matches; hint: `task pull, resolve conflicts, then task push` | none |
| `blocked_stale_review` | `review.json.model_signature != latest.json.model_signature`, or the review model uses another image frame; carry edits forward first | none |
| `blocked_review_lock` | `data/scenes/<S>/review.lock` exists | none |
| `too_large` | document > 2 MiB − 512 B or > 5,000 annotations; byte and annotation counts are reported, nothing is trimmed | none |
| `invalid_frame` | PNG bytes do not hash to `image_sha256`, IHDR size differs from `image_width/height`, or `latest.json` is malformed | none |
| `asset_mismatch` | an asset's `HEAD` length or sha disagreed during or after upload; the scene is aborted before `SaveItem` | assets only |
| `stale_plan` | `latest.json`, `review.json` or the PNG changed between planning and applying | none |
| `needs_label` | count of `<project>-kind-<kind>` labels the apply will create first | SaveLabel rev 0 |

Per superseded item the mark outcome is `marked`, `mark_conflict` (409 twice
or another error; non-fatal, a later run marks it) or `mark_skipped` (already
marked).

The remote-edit check runs before `current`: every live item of the scene
(the frozen id plus every generation listed in `push/scenes/<S>.json`) must sit
at its receipt revision, be our own write, or be covered by a local
`FeedbackReceipt`. A Fieldwork edit on the current generation therefore
surfaces as `blocked_remote_edits` (with `unrecoveredFieldwork` naming the item
and revision, and the pull hint) even when the local model/review has nothing
new to publish; `current` is reported only when nothing moved. With
`--allow-unrecovered` such a scene stays `current` (there is no new item to
carry `unrecoveredFieldwork`), but the plan still lists the moved items under
`unrecoveredFieldwork` and in `reasons`.

## What one item carries

`source` (immutable): `sceneId`, `image`, `model`, `modelSignature`,
`imageSha256`, `width`, `height`, `review` (the **review head**, `review.json`
minus `history`, re-encoded; its sha is recorded in
`metadata.addr.generation.reviewHeadSha256` and its bytes under
`push/assets/<sha>`), `reviewModel`, `confidence` when present, and `assets[]`
listing exactly the refs uploaded. `--review-history split` uploads the history
array as a separate (chunked above 64 MiB) asset listed in `assets[]` only.

`annotations`: one per effective object (review layer, else model layer),
`labelId = <project>-kind-<kind>`, closed polygon normalised by width/height,
`properties.addr = {objectId, layer, status, geometry_reviewed, source,
provenance}` plus the immutable bindings `replaces`, `clipped_by_frame`,
`structure_kind`, `has_roof_line_edits`, and a verbatim `fieldwork_import`
marker when the local object is itself an unaccepted Fieldwork draft. No
`overrides`.

`metadata.addr`: `{schemaVersion: 2, kind: 'generation', sceneId,
generation: {modelSignature, reviewRevision, reviewHeadSha256,
reviewHistorySha256?, reviewActor, actorKind: local|machine|unknown,
reviewUpdatedAt, imageSha256, width, height, contextExpansion|null,
supersedes: [{itemId, revision, modelSignature, imageSha256}],
inventory: frozen|outside-frozen-inventory}, push: {runId, pushedAt (plan
time), tool: 'annotate push', mirrorCursor}, unrecoveredFieldwork?,
sourceReviewRevision, reviewModelSignature, reviewActor, reviewUpdatedAt,
stale: false, summary: {objects, classes, statuses, layer}}`. Tags:
`[project, pushed, gen, <layer>, expanded-1024?, outside-inventory?]`. Status
`in_review` with a review, else `unreviewed`.

## Apply order and resume

Per scene, each step is appended to `runs/<runId>/progress.ndjson` before the
next: (a) create missing labels, (b) upload missing assets (`HEAD` skip inside
the upload, `PUT` otherwise, chunked above 64 MiB), (c) re-`HEAD` every ref
(the server accepts dangling refs, so this is the only gate), (d) `SaveItem
{item, expectedRevision: 0}`; a 409 is compared against the request, (e) write
`push/items/<itemId>.json` and `push/scenes/<S>.json`, (f) mark each superseded
item from a fresh `GetItem` with the revision just read, retrying once on 409.

Apply re-syncs the mirror first and re-checks every live item of the scene
against the fresh mirror; a remote revision change since planning blocks the
scene. Re-running the same `--run` converges: assets are `HEAD`-skipped,
identical items resolve to `skip_identical`, unmarked old items get marked, and
the mirror cursor does not move when there is nothing to do.

The saved plan's `planHash` covers the local inputs only (per-scene sha of
`latest.json` and `review.json`, PNG name), not the mirror cursor, because our
own writes advance the cursor between an interrupted run and its resume. Remote
drift is caught by the per-scene re-check instead.

## Receipts

```
<mirror>/push/
  push.lock                      # flock held by annotate push; the Python wrapper uses push.wrapper.lock
  runs/<runId>/plan.json         # the plan, table input, counts
  runs/<runId>/progress.ndjson   # one line per step
  runs/<runId>/result.json       # counts, per-scene outcomes, marks
  runs/<runId>/items/<itemId>.{request,response}.json
  runs/<runId>/items/<oldItemId>.mark.{request,response}.json
  items/<itemId>.json            # ItemReceipt: latest own document, pristine baseline (nil for frozen ids), generation, supersedes, marks
  scenes/<sceneId>.json          # latestItemId and every generation id of the scene
  assets/<sha256>                # bytes of every JSON uploaded (latest.json, model, review head, confidence)
```

`ReceiptIndex.OwnWrite(itemId, revision, doc)` (revision equal and document
equal ignoring timestamps) is how push recognises its own writes in the mirror;
the change-feed actor is `workspace` for humans and for our token alike, so
loop safety is by content. `task pull` reads these receipts: `annotate
feedback` loads `MIRROR/push` and emits a schema-2 packet whose entries are
baselined on the receipts, with own writes skipped (see
[feedback.md](feedback.md)).

## MCP equivalents

Local addr MCP (`scripts/ml/mcp_server.py`): `plan_fieldwork_push` (read-only:
sync, plan, HEAD), `apply_fieldwork_push` (`run_id` from the plan;
`allow_unrecovered`), `fieldwork_push_status` (receipts only). Fieldwork MCP
(`POST /mcp`) can audit or hand-replay the individual writes a push performs
(`save_label`, `save_item` with `expectedRevision 0`, `get_item` +
`save_item` with the current revision for a mark) and reviewers keep using
`save_annotation`; there is no MCP tool for asset bytes, so an MCP-only push is
impossible. The full request examples are in
[local-to-fieldwork.md](local-to-fieldwork.md#mcp).

## Verified against a local dev server (2026-09-29)

Server: `annotation-server --dev --sqlite /tmp/addr-fieldwork-dev/push.sqlite
--listen 127.0.0.1:18131 --asset-dir …`, seeded with
`annotation-migrate import --bundle data/annotation-snapshots/mooroolbark-23591d058ab7`
(518 documents: 507 items at revision 1). Local root: a read-only copy of
`house-25822` (1024 px expansion, review revision 35, 242 objects) and
`roof_training_250-25018` (outside the frozen inventory).

- Dry-run: `create` ×2 (`house-25822` → `addr-scene-house-25822-m8657343a6e23-r35`,
  271,687 bytes, supersedes `addr-scene-house-25822@1`;
  `roof_training_250-25018` → `…-m02c1e8936764-r3`, 54,863 bytes, outside
  inventory); only `ListChanges` and `HEAD` on the wire.
- Push: created 2, marked 1, uploaded 7 assets (8,245,090 bytes); the frozen item
  went to revision 2 with `superseded-frame` and `supersededBy`, its `source`
  byte-identical to the frozen import.
- Second push: `current` ×2, zero writes, change cursor unchanged (521).
- A reviewer `save_annotation` via `/mcp` on the new generation (overrides
  `needs_adjustment` + notes, revision 1→2) and one on the frozen item
  (revision 2→3); dry-run at the same local revision: `current` (nothing to
  publish; observed before the 2026-09-29 reorder of the remote-edit check, which
  now reports `blocked_remote_edits` for this case, see Actions); after the
  local review advanced to revision 36: `blocked_remote_edits`
  naming both items and revisions; with `--allow-unrecovered`: gen `r36` created,
  `unrecoveredFieldwork: [addr-scene-house-25822@3, …-r35@2]`, gen `r35` marked
  by patch at revision 3 with its Fieldwork overrides intact.
- `SaveItem` / `SaveAnnotation` with a stale `expectedRevision` → HTTP 409.
- Re-running the `r36` push: `current`, zero writes.
- `annotate feedback` (schema-1 packet) after the pushes: 1 entry (the marked
  frozen item) and 3 generation ids quarantined `unknown_or_noncanonical_scene`.
  That run predates the receipt-reading pull; with `MIRROR/push` present the
  same mirror now yields a schema-2 packet in which own writes produce no entry
  and generation ids are baselined on their receipts (feedback.md).

Re-verified 2026-09-29 (later run, fresh `--dev --sqlite` server on
127.0.0.1:18141, `data/annotation-snapshots/mooroolbark-23591d058ab7` imported:
518 documents, 10,027 assets; push root a read-only copy of `house-25822`):

- Guard: `annotate push` (non-dry-run), `annotate save-item` with an explicit
  `--token-file`, and `annotation-migrate import` against
  `https://fieldwork-annotations.example.invalid` (also spelled upper-case with
  `:443/`) all exit before any request with `refusing … against non-loopback
  Fieldwork origin …; pass --allow-live (or set FIELDWORK_PUSH_ALLOW_LIVE=1)`;
  the Python wrapper refuses the `:443` spelling; the mirror directory was not
  created.
- Push 1 (`--scenes house-25822`): `create` r35, 4 assets uploaded, frozen item
  marked (revision 1→2, `+superseded-frame`, `supersededBy r35`, `stale`).
  Re-run: `current`, zero writes.
- Push 2 after the copy's review revision moved 35→36: `create` r36, r35 marked
  (revision 1→2); the frozen item was not touched again; `push-status`: 3 items,
  2 marked, latest r36.
- `annotate feedback` (schema-1 packet): 1 entry (the marked frozen item) and
  the 2 generation ids quarantined `unknown_or_noncanonical_scene` (run before
  the pull read receipts; see the note above); the server was stopped
  afterwards.
- The mark's revision check (a reviewer edit landing between the mirror sync
  and the mark's fresh `GetItem` yields `mark_conflict` and is not absorbed) is
  covered by `TestMarkRefusesFieldworkEditSincePlanningWithoutAllowUnrecovered`
  in `internal/annotation/push`; it cannot be timed by hand against a server.

## Open follow-ups

- The pull side reads push receipts (`feedback` packet schema 2, own-write
  skip, receipt baselines) and recovers old-frame edits in
  `prepareFeedbackScene`. Apply through `task pull` against a pushed mirror has
  been exercised only on tmp roots (`feedback.test.mjs`); against the dev server
  the round trip stops at `feedback-preview` through the review app because
  `data/scenes` is read-only. Note that old-frame recovery also changed what the
  live schema-1 pull writes (see "Pull behaviour on the live project changed"
  in feedback.md).
- Only the sqlite dev server has been exercised; live R2/Neon behaviour and a
  first live apply (`--scenes` subset, `FIELDWORK_PUSH_ALLOW_LIVE=1`) remain
  separate, explicitly authorised steps.
- The Python wrapper serialises on `push/push.wrapper.lock` because a second
  `flock` on `push.lock` from the parent process would block the Go child.
