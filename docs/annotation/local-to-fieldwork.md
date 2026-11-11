# Local to Fieldwork: push, pull and training artefacts

This is the end-to-end contract for moving records between the local addr
checkout and the Fieldwork annotation server (Go `annotation-server`, Cloudflare
deploy in `deploy/annotation`, Neon Postgres; or `--dev --sqlite` locally) in
both directions, over the same REST RPC / MCP the reviewers use, and for making
training runs pull versioned datasets and diffs from the same place.

Status (29 September 2026): the surface, both engines (push, training
artefacts) and the pull extension (push receipts as baselines, schema-2
packets, old-frame recovery) are in the tree on both the Go and the local-app
side. Live pull behaviour changed with it, even without receipts: see "Pull
behaviour on the live project changed" in [feedback.md](feedback.md).

| Piece | State |
| --- | --- |
| `task push`, `task push:status`, `task training:publish`, `task training:manifests`, `task training:pull` | present in `Taskfile.yml`; each shells out to the module named below |
| Local MCP tools `plan_fieldwork_push`, `apply_fieldwork_push`, `fieldwork_push_status`, `publish_training_artefacts`, `list_training_manifests`, `pull_training_artefacts` | present in `scripts/ml/mcp_server.py` as thin, lazily importing wrappers; while a module is missing the tool answers with a tool error naming it |
| `scripts/ml/training/publish_artefacts.py` (`plan`, `stage`, `publish`), `scripts/ml/training/pull_artefacts.py` (`list_manifests`, `pull`) | in the tree; round-tripped against a local `--dev --sqlite` server (see [training artefacts](training-artefacts.md)) |
| `scripts/ml/fieldwork_push.py` (`push_fieldwork`, `push_status`), Go `internal/annotation/push` + `annotate push` / `annotate push-status` | in the tree; exercised end to end against a local `--dev --sqlite` server with two real scenes (see [push.md](push.md)) |
| Pull with push receipts (`feedback` packet schema 2, old-frame recovery) | in the tree on both sides: `annotate feedback` loads `MIRROR/push` (`cmd/annotate/feedback.go`) and emits schema 2 with per-entry bindings; the local app (`src/lib/annotations/feedback.ts`, `feedback-store.ts`, `POST /api/scenes/feedback`) validates schema 2, strips the push-owned `metadata.addr` keys before comparing, and carries old-frame edits through `recoverSceneReview`. Neither recovery nor the allowlist is gated on the schema, so the live schema-1 pull changed too (feedback.md). Apply against a pushed mirror is covered by `feedback.test.mjs` on tmp roots; against the dev server only preview has been run through the review app |

Nothing on this page writes to the live project as a side effect of reading
it. Every write path is exercised only against a local dev server until a live
apply is explicitly authorised (see "Verification").

## Invariants

- **The server never rewrites history.** `addr-scene` / `addr-report` `source`
  is immutable after create; annotations cannot be removed; the
  `properties.addr.{objectId,layer,status,geometry_reviewed,source,provenance}`
  keys and `labelId` are immutable; `DeleteItem` tombstones forever. Documents
  are capped at 2 MiB, requests at 4 MiB, assets at 64 MiB.
- **Local truth is published only as a new item.** Model snapshot, review
  decisions, provenance, `roof_line_edits`, `replaces`, `clipped_by_frame` and
  `fieldwork_import` markers travel as immutable `source` bindings and assets of
  a **new** generation item. `properties.addr.overrides` is written only by
  Fieldwork; push never writes it; pull is the only way overrides reach a local
  `review.json`, and always as drafts (`needs_adjustment`,
  `geometry_reviewed:false`, `height_verified:false`,
  `roof_line_edits.checked:false`, `fieldwork_import.pending:true`).
- **Existing items are touched by push in one way only:** the supersede mark
  (`tags += superseded-frame`, `metadata.addr.supersededBy`,
  `metadata.addr.stale`), revision-checked, patched from a freshly read
  document, never touching `source`, `annotations`, `status`, `notes`, `title`.
- **Loop safety is by content.** The change-feed actor is `workspace` for
  humans and for our token alike, so own writes are recognised by revision plus
  document equality against receipts, not by actor.
- **Wording.** A pushed item is "the model estimate plus the local review state
  as of publication". A Fieldwork value is a "Fieldwork edit" whose agreement
  with the local review is pending. `review.updated_by` `local` / `machine:*`
  are audit strings, never a person. `provenance.source machine:*` is copied
  verbatim (review-authority `machine_label`). Superseding never uses status
  `rejected`.

## The flow

```
data/scenes/<S>/{latest.json, review.json, models/, <sha>.png}     data/reviews/eyes/...
        │                                                                  │
        │ task push  (plan → assets PUT → SaveItem rev 0 → mark old)       │ task training:publish
        ▼                                                                  ▼
Fieldwork project mooroolbark                                     Fieldwork project addr-training
  addr-scene-<S>                    (gen 0, frozen import)          training-manifest-<date>-<sha12>
  addr-scene-<S>-m<sig12>-r<rev>    (gen N, immutable)                (addr-report, immutable)
        │                                                                  │
        │ reviewer save_annotation → overrides                             │ task training:pull
        ▼                                                                  ▼
task pull  (feedback packet → preview → apply as DRAFTS)          <out>/data/... + TRAINING_MANIFEST.json
        │                                                                  │
        ▼                                                                  ▼
data/scenes/<S>/review.json  (fieldwork_import.pending:true)      export_dataset / prepare_coco --copy / edit_diff
```

Receipts for both directions live under `data/fieldwork-sync/<project>/`
(`mirror.json`, `feedback.json`, `push/…`) and
`data/fieldwork-sync/addr-training/publish/`.

## Push

### Identity

Generation id: `addr-scene-<S>-m<modelSignature[:12]>-r<reviewRevision>`
(`migration.GenerationSceneID`; `r0` without `review.json`). Example:
`addr-scene-house-25822-m8657343a6e23-r35`. The frozen ids `addr-scene-<S>`
are generation 0 and are never re-sourced.

### What one publication carries

- `source` (immutable): `{sceneId, image, model, modelSignature, imageSha256,
  width, height, review?, reviewModel?, confidence?, assets[]}`. Assets are
  content-addressed (`HEAD` then `PUT /api/v1/assets/{sha256}`): the scene PNG,
  `latest.json`, `models/<review.model_signature>.json`, `confidence.json` when
  present, and the **review head** (`review.json` minus `history`). Review
  history is omitted by default; `--review-history split` uploads it as chunked
  refs listed in `source.assets` only.
- `annotations`: one per effective object (review layer if `review.json`,
  else model), `labelId = <project>-kind-<kind>`, polygon normalised 0..1 and
  closed, `properties.addr = {objectId, layer, status, geometry_reviewed,
  source, provenance}` plus `replaces`, `clipped_by_frame`, `structure_kind`,
  `has_roof_line_edits` and a verbatim `fieldwork_import` marker when the local
  object is itself an unaccepted Fieldwork draft. These extra keys are outside
  `overrides`; Fieldwork cannot mutate them.
- `metadata.addr`: `{schemaVersion:2, kind:'generation', sceneId, generation:{
  modelSignature, reviewRevision, reviewHeadSha256, reviewActor, actorKind,
  reviewUpdatedAt, imageSha256, width, height, contextExpansion,
  supersedes:[{itemId, revision, modelSignature, imageSha256}],
  inventory:'frozen'|'outside-frozen-inventory'}, push:{runId, pushedAt, tool,
  mirrorCursor}, unrecoveredFieldwork?, summary:{objects, classes, statuses,
  layer}}`.
- `tags`: `[project, 'pushed', 'gen', layer, 'expanded-1024' when
  contextExpansion, 'outside-inventory' for scenes absent from the frozen
  import]`; `status` `in_review` when a review exists, else `unreviewed`. No
  `overrides` anywhere.

### Actions

| Action | Meaning | Write? |
| --- | --- | --- |
| `create` | new generation item; lists the items it supersedes | yes |
| `current` | latest published generation already matches `latest.json.model_signature` and `review.revision` | no |
| `skip_identical` | the id exists remotely with an identical comparable document (resume) | no |
| `conflict_id_reuse` | the id exists remotely with a different document; never overwritten | no |
| `blocked_remote_edits` | a live item of the scene moved past its receipt revision and no local `FeedbackReceipt` matches; hint: `task pull`, resolve, `task push` | no |
| `blocked_stale_review` | `review.model_signature != latest.model_signature`; carry edits forward first | no |
| `blocked_review_lock` | `data/scenes/<S>/review.lock` exists | no |
| `too_large` | marshalled item > 2 MiB − 512 or > 5,000 annotations; reported with byte count, never trimmed | no |
| `invalid_frame` | PNG sha or IHDR size disagrees with `latest.json` | no |
| `asset_mismatch` | post-upload `HEAD` length/sha disagree; scene aborted before `SaveItem` | no |
| `stale_plan` | local inputs changed since planning | no |
| `needs_label` | labels to create first (informational) | labels only |

Per superseded item the mark outcome is `marked`, `mark_conflict` (409 after
one retry, non-fatal) or `mark_skipped`.

Expected first run against the frozen state (mirror cursor 538, 507 items at
revision 1): 284 `create` with supersedes (31 house-* 512→1024 expansions with
a new PNG, 253 mooroolbark-* same-image promotions), 56 `create` tagged
`outside-inventory` (30 map-*, 26 roof_training_250-*), 222 `current`,
0 blocked, 284 marks.

### Commands

```sh
# Plan only. Remote calls: ListChanges, GetItem, GetInfo, HEAD. Writes the plan under
# <mirror>/push/runs/<runId>/plan.json and prints a table plus {runId, counts, planFile}.
task push -- --dry-run --url http://127.0.0.1:18131 --dir /tmp/addr-fieldwork-dev/mirror
task push -- --dry-run --scenes house-25822,house-30286

# Plan and apply the clean part (create / skip_identical); blocked scenes are reported.
task push -- --url http://127.0.0.1:18131 --dir /tmp/addr-fieldwork-dev/mirror

# Resume or apply a saved plan; refuses when the plan hash no longer matches the inputs.
task push -- --run 20260929T101500Z-1a2b3c4d --url http://127.0.0.1:18131 --dir ...

# Receipts only, no network.
task push:status
task push:status -- --dir /tmp/addr-fieldwork-dev/mirror
```

Other flags: `--no-mark-superseded`, `--skip-outside-inventory`,
`--allow-unrecovered` (push past `blocked_remote_edits`, recording
`metadata.addr.unrecoveredFieldwork` on the new item), `--review-history split`,
`--token-file`. Environment: `ANNOTATION_URL`, `FIELDWORK_TOKEN_FILE`,
`FIELDWORK_PROJECT`, `FIELDWORK_SYNC_DIR`. A custom `--url` never receives the
implicit production token. **Any non-dry-run push whose origin is
`https://fieldwork-annotations.ben-ebsworth.workers.dev` is refused unless
`FIELDWORK_PUSH_ALLOW_LIVE=1`.** The Go CLI underneath is
`annotate push --project P --dir D [--dry-run|--run ID] …`.

### Apply order and resume

Per scene, each step is appended to `runs/<runId>/progress.ndjson` before the
next: create missing labels → upload missing assets (`HEAD` skip, `PUT`,
chunked above 64 MiB) → re-`HEAD` every ref → `SaveItem {item,
expectedRevision:0}` (409 → compare; identical = `skip_identical`, different =
`conflict_id_reuse`) → write receipts → mark each superseded item from a
fresh `GetItem` with its current revision. Apply re-syncs the mirror first and
re-evaluates the remote-edit check per scene. Re-running the same `--run`
converges: assets are `HEAD`-skipped, identical items resolve to
`skip_identical`, unmarked old items get marked. Push never calls `DeleteItem`.

### Receipts

```
data/fieldwork-sync/<P>/push/
  runs/<runId>/{plan.json, progress.ndjson, result.json, items/<itemId>.{request,response}.json}
  items/<itemId>.json      # ItemReceipt: latest own document, pristine baseline, generation, supersedes, marks
  scenes/<sceneId>.json    # latest generation item per scene
  assets/<sha256>          # bytes of every JSON uploaded (latest.json, model, compact review, confidence)
  push.lock
```

`runId = <UTC yyyymmddTHHMMSSZ>-<8 hex>`. PNGs are not duplicated; they stay
under `data/scenes`.

## Pull with push receipts

`task pull` (`docs/annotation/feedback.md`) keeps its behaviour and grows a
schema-2 packet once `internal/annotation/feedback/packet.go` reads
`push/` receipts:

- **Own writes are skipped**: a mirror item whose revision and document equal
  the receipt (`ReceiptIndex.OwnWrite`) and that carries nothing beyond the
  baseline plus the supersede mark produces no entry, so a push followed by a
  pull yields 0 entries, 0 quarantined. An edit embedded by a
  `--allow-unrecovered` mark is still delivered.
- **Baselines**: for a generation id the baseline is the receipt's pristine
  document (`Binding{kind:'push', runId, receiptSha256, receiptRevision,
  generation}`); frozen ids keep the frozen plan document
  (`Binding{kind:'push'}` when the id carries a supersede mark and receipt,
  `Binding{kind:'frozen'}` otherwise). Evidence (model, review, review model)
  for generation ids is read from `push/assets/<sha>`.
- **Quarantine**: a generation id without a receipt → `missing_push_receipt`;
  a remote `source` that differs from the receipt baseline →
  `receipt_binding_mismatch`.
- **Metadata allowlist** (`src/lib/annotations/feedback.ts`): only
  `metadata.addr.supersededBy`, `metadata.addr.stale` and
  `metadata.addr.unrecoveredFieldwork` may differ from the baseline; any other
  metadata change is a conflict.
- **Old-frame recovery**: a Fieldwork edit made on a superseded frame is
  carried to the current frame through verified hops only: the 512→1024
  expansion (`recoveredFrom.method:'expansion-256'`, rings shift by +256 and
  seed decisions follow `replaces`) or an identical frame (`'same-frame'`,
  id-based, no shift). When one hop does not reach the current frame the pull
  walks `data/scenes/<S>/models/<sig>.json` for the shortest chain of such
  hops (at most 4; every snapshot's image sha and pixel frame must chain
  exactly) and records `recoveredFrom {method:'chain', hops:[…]}` — the shape
  of 30 of the 31 widened `house-*` scenes (frozen 512 → same-image 512
  re-extraction → 1024 expansion). Anything else is
  `frame_changed_unrecoverable`: the edit stays on its item and in the change
  feed, receipt only, nothing applied. Recovered edits still land as drafts;
  `geometry_reviewed`, `height_verified` and `roof_*` derived state are never
  copied; `roof_line_edits` vertices/edges (metres) are invariant.
- **Several entries per scene**: a schema-2 packet may hold both the marked
  frozen item and the generation item of one scene (item ids distinct). The
  local app applies them in a deterministic order — current-frame entries
  first, then superseded frames through recovery, ties by item id — each
  planned against the review the previous one commits and each with its own
  receipt; overlapping edits conflict on the second entry. `--id` still selects
  one item.

A pending schema-1 apply checkpoint (`<packet>.apply.json`) must be finished or
removed before a schema-2 packet is used.

## Training artefacts

### Representation

Project `addr-training` (created by the publisher when `ListProjects` lacks
it); one immutable `addr-report` item per manifest,
`training-manifest-<yyyymmdd>-<manifestSha12>`, `expectedRevision 0`:

```json
{
  "source": {
    "manifest": {"url": "/api/v1/assets/<sha>", "sha256": "<sha>", "bytes": 123456, "contentType": "application/json"},
    "kind": "training-manifest",
    "groups": {"dataset-v2": ["data/reviews/eyes/training/dataset-v2/..."], "eyes": ["data/reviews/eyes/eyes.sqlite"]},
    "parents": ["<previous manifest sha256>"],
    "scenes": [{"sceneId": "house-25822", "itemId": "addr-scene-house-25822-m8657343a6e23-r35",
                "modelSignature": "8657343a6e23…", "reviewRevision": 35}]
  },
  "metadata": {"addr": {"kind": "training-manifest", "publishedAt": "…", "codeSha256": "…", "gitHead": "…",
                        "counts": {"files": 0, "bytes": 0, "scenes": 0}, "pushRunIds": []}},
  "tags": ["training-manifest", "dataset-v2"], "status": "unreviewed", "annotations": []
}
```

The manifest asset is a `migration.Manifest` (schemaVersion 1, `files[{path,
sha256, bytes, contentType, chunks[{sha256, bytes}], mtimeNs}]`) with extra
keys `kind:'addr-training-manifest'`, `groups`, `parents`, `codeSha256`, so
`annotation-migrate upload` and `Bundle.Verify/Restore` work unchanged.

### Groups

| Group | Root |
| --- | --- |
| `dataset-vN` | `data/reviews/eyes/training/dataset-vN/` (images dedupe across versions; `--tarball` adds the `.tar`) |
| `edit-diffs/<date>` | `data/reviews/eyes/edit-diffs/<date>/` |
| `line-corrections/<date>` | `data/reviews/eyes/line-corrections/<date>/` |
| `edit-diff` | `data/reviews/eyes/training/edit-diff/` when present |
| `sweep` | `sam3-mac/sweep/results.jsonl` + run manifests (`*.pt` only with `--include-checkpoints`) |
| `coco-v1` | `sam3-mac/coco-v1/**/manifest.json` and `*_annotations.coco.json` (images with `--with-images`) |
| `eyes` | `VACUUM INTO <staging>/source/data/reviews/eyes/eyes.sqlite` (never a raw copy; the WAL holds recent writes) |

### Commands

```sh
# Dry run (default): per-group file counts and bytes; no network, no HEAD.
task training:publish -- --select dataset-v2 edit-diffs/2026-09-29 line-corrections/2026-09-29 sweep coco-v1 eyes --dry-run

# Apply against a local dev server: stage under .run/training-bundles/<sha12>/, upload blobs
# (HEAD-skip dedupe, manifest last), SaveItem revision 0, receipt under
# data/fieldwork-sync/addr-training/publish/<sha12>.json. Live origin needs FIELDWORK_PUSH_ALLOW_LIVE=1.
task training:publish -- --select dataset-v2 eyes --apply --url http://127.0.0.1:18131

# List and pull.
task training:manifests -- --url http://127.0.0.1:18131
task training:pull -- --url http://127.0.0.1:18131 --latest --out /tmp/addr-training-pull --only dataset-v2,edit-diffs,eyes
task training:pull -- --url http://127.0.0.1:18131 --manifest training-manifest-20260929-<sha12> --out /tmp/addr-training-pull-scenes --with-scenes
```

`pull` verifies every chunk and whole file by sha256 before anything under
`<out>/data` is written, restores with `O_EXCL` (the destination must not exist
unless `--resume`), and writes `<out>/TRAINING_MANIFEST.json` plus
`<out>/.pull-receipt.json` `{origin, project, itemId, revision, manifestSha256,
codeSha256, files, pulledAt}`. `--with-scenes` requires the `eyes` group in the
manifest (hard failure otherwise: `export_dataset.eyes_references` silently
drops registry references when `eyes.sqlite` is absent) and restores each
referenced generation to `<out>/data/scenes/<id>/{<sha>.png, latest.json,
review.json, models/<sig>.json}`.

### Consumers on a pulled root `R`

```sh
scripts/ml/.venv/bin/python -m scripts.ml.training.export_dataset --root R --out R/data/reviews/eyes/training/dataset-v3 --limit 5   # needs --with-scenes
scripts/ml/.venv/bin/python -m scripts.ml.training.sam3_mac.prepare_coco --dataset R/data/reviews/eyes/training/dataset-v2 --out <coco> --copy
scripts/ml/.venv/bin/python -m scripts.ml.training.sam3_mac.finetune_mps --coco <coco>
scripts/ml/.venv/bin/python -m scripts.ml.training.edit_diff --root R
```

`--copy` is mandatory for `prepare_coco`: the published coco-v1 JSONs carry
absolute `/Users/benebsworth` paths. `reference_eval.py` stays repo-bound.

## MCP

### Local addr MCP (`scripts/ml/mcp_server.py`, stdio)

All six tools are thin wrappers; they never return a token, never start
training, never edit Fieldwork overrides, and the write tools refuse the live
origin unless `FIELDWORK_PUSH_ALLOW_LIVE=1`. Reconnect the MCP client after
updating code so discovery refreshes.

| Tool | readOnly | Calls | Example arguments |
| --- | --- | --- | --- |
| `plan_fieldwork_push` | yes | `fieldwork_push.push_fieldwork(dry_run=True)` | `{"scenes": ["house-25822"], "remote_url": "http://127.0.0.1:18131", "mirror_dir": "/tmp/addr-fieldwork-dev/mirror"}` → `{runId, counts, blocked, tooLarge, bytesToUpload, planFile}` |
| `apply_fieldwork_push` | no | `push_fieldwork(run_id=…, dry_run=False)` | `{"run_id": "20260929T101500Z-1a2b3c4d", "remote_url": "http://127.0.0.1:18131", "allow_unrecovered": false}` → `{runId, created, skippedIdentical, marked, markConflicts, blocked, receiptsDir, mirrorCursor}` |
| `fieldwork_push_status` | yes (no network) | `push_status(project_id=…)`; `scene_id` narrows `scenes` in the wrapper | `{"scene_id": "house-25822"}` |
| `publish_training_artefacts` | no | `training.publish_artefacts.publish(select, dry_run, url, token_file, with_images, tarball)` | `{"select": ["dataset-v2", "eyes"], "dry_run": true}` |
| `list_training_manifests` | yes | `training.pull_artefacts.list_manifests(url, token_file, project, limit)` | `{"remote_url": "http://127.0.0.1:18131", "limit": 5}` |
| `pull_training_artefacts` | no (writes under `out`) | `pull(url, token_file, out, project, manifest, latest, only, with_scenes, resume)` | `{"out": "/tmp/addr-training-pull", "latest": true, "only": ["dataset-v2", "edit-diffs"], "with_scenes": false}` |

Anticipated refusals (missing module, live-origin guard, stale plan hash,
lock, hash mismatch) come back as tool errors with the reason; a stub test
(`scripts/ml/tests/test_mcp_fieldwork_surface.py`) pins the forwarding.

### Fieldwork MCP (`POST /mcp`, or `annotate mcp --url … --token-file …`)

Headers: `Content-Type: application/json`, `Accept: application/json,
text/event-stream`, `MCP-Protocol-Version: 2025-06-18`, bearer token.

Reads that back a plan:

```json
{"name": "get_info", "arguments": {}}
{"name": "list_changes", "arguments": {"projectId": "mooroolbark", "cursor": "538", "limit": 500, "includeDocuments": false}}
{"name": "get_item", "arguments": {"id": "addr-scene-house-25822"}}
{"name": "list_items", "arguments": {"projectId": "mooroolbark", "adapter": "addr-scene", "query": "house-25822", "limit": 20}}
```

Writes a push performs (shown so a reviewer can audit or hand-replay one):

```json
{"name": "save_label", "arguments": {"label": {"projectId": "mooroolbark", "id": "mooroolbark-kind-building", "name": "building", "color": "#D19A69"}, "expectedRevision": 0}}
{"name": "save_item", "arguments": {"item": {"id": "addr-scene-house-25822-m8657343a6e23-r35", "projectId": "mooroolbark", "adapter": "addr-scene", "title": "…", "source": {"…": "…"}, "metadata": {"addr": {"…": "…"}}, "tags": ["mooroolbark", "pushed", "gen", "review", "expanded-1024"], "status": "in_review", "annotations": []}, "expectedRevision": 0}}
{"name": "get_item", "arguments": {"id": "addr-scene-house-25822"}}
{"name": "save_item", "arguments": {"item": {"…same source/annotations/status/notes/title…": "…", "tags": ["…", "superseded-frame"], "metadata": {"addr": {"supersededBy": {"itemId": "addr-scene-house-25822-m8657343a6e23-r35", "runId": "…"}, "stale": true}}}, "expectedRevision": 1}}
```

A 409 `aborted: revision conflict` means reload and re-plan. Reviewer edits
keep using
`save_annotation {itemId, annotation:{id, geometry, labelId, properties:{addr:{objectId, overrides:{…}}}}, expectedRevision}`.

Training on the Fieldwork side:

```json
{"name": "save_project", "arguments": {"project": {"id": "addr-training", "name": "addr training artefacts"}, "expectedRevision": 0}}
{"name": "save_item", "arguments": {"item": {"id": "training-manifest-20260929-<sha12>", "projectId": "addr-training", "adapter": "addr-report", "title": "…", "source": {"manifest": {"url": "/api/v1/assets/<sha>", "sha256": "<sha>", "bytes": 0, "contentType": "application/json"}, "kind": "training-manifest", "groups": {}, "scenes": []}}, "expectedRevision": 0}}
{"name": "list_items", "arguments": {"projectId": "addr-training", "adapter": "addr-report"}}
```

**Limits, stated plainly:** there is no MCP tool for assets (`PUT/HEAD/GET
/api/v1/assets/{sha256}` is HTTP only, and the initialize instructions say not
to send bytes through MCP), so an MCP-only push is impossible; `save_item`
needs the whole document (≤ 2 MiB) in-conversation. MCP is for reads,
single-annotation edits and status/tag marking; bulk goes through `annotate` /
`annotation-migrate` over HTTP.

## Conflict handling

| Situation | Outcome |
| --- | --- |
| Fieldwork edited a scene's item after the last push/pull | `blocked_remote_edits` naming item ids and revisions; run `task pull`, resolve, `task push`; or `--allow-unrecovered` to record `unrecoveredFieldwork` |
| Local review is behind the model (`review.model_signature != latest`) | `blocked_stale_review`; carry edits forward in the editor or `scripts/carry-forward-reviews.mjs` |
| The generation id already exists with different content | `conflict_id_reuse`; never overwritten; inspect both documents under `runs/<runId>/items/` |
| Mark hits 409 twice, or the item moved since planning (a reviewer edit landed between the mirror sync and the mark's fresh read) | `mark_conflict`, non-fatal, recorded; the edit is left untouched and not claimed as our write; the next plan blocks until `task pull` |
| Pull sees a Fieldwork edit on a superseded frame | recovered only through exact `expansion-256` / `same-frame` hops, single or chained through `models/<sig>.json` (`recoveredFrom.method:'chain'`, at most 4 hops); otherwise `frame_changed_unrecoverable`, receipt only |
| Pull sees a generation id with no receipt / mismatched source | quarantined `missing_push_receipt` / `receipt_binding_mismatch` |
| Push and pull both want to run | `push.lock` and the packet pull lock serialise them |

## Verification

All writes go to a local dev server:

```sh
mkdir -p /tmp/addr-fieldwork-dev/push-assets
go run ./cmd/annotation-server --dev --sqlite /tmp/addr-fieldwork-dev/push.sqlite \
  --listen 127.0.0.1:18131 --asset-dir /tmp/addr-fieldwork-dev/push-assets
go run ./cmd/annotation-migrate import --bundle data/annotation-snapshots/mooroolbark-23591d058ab7 \
  --project mooroolbark --url http://127.0.0.1:18131          # 507 items at revision 1, like live
task push -- --dry-run --url http://127.0.0.1:18131 --dir /tmp/addr-fieldwork-dev/mirror
task push -- --url http://127.0.0.1:18131 --dir /tmp/addr-fieldwork-dev/mirror
go run ./cmd/annotate feedback --project mooroolbark --dir /tmp/addr-fieldwork-dev/mirror \
  --snapshot data/annotation-snapshots/mooroolbark-23591d058ab7 \
  --file /tmp/addr-fieldwork-dev/mirror/feedback.json --url http://127.0.0.1:18131   # expect 0 entries
```

Against the live origin only read-only checks are allowed: `task push --
--dry-run --url https://fieldwork-annotations.ben-ebsworth.workers.dev --dir
.run/fieldwork-mirror-plan-<date>` (assert the mirror cursor is still 538
afterwards) and `task training:manifests -- --url … --token-file
deploy/annotation/admin-token.txt` (an absent `addr-training` project is
reported, not created). A first live apply is a separate, explicitly
authorised step, started with a `--scenes` subset and
`FIELDWORK_PUSH_ALLOW_LIVE=1`.

## Known limits

The server accepts dangling asset refs and unchecked dimensions (the client
gate is the only check); 2 MiB / 5,000-annotation documents are reported as
`too_large`, never trimmed; every pushed review revision is a new item (list
clutter mitigated by the `gen` / `superseded-frame` tags); the compact review
sha differs from the on-disk `review.json` (evidence comes from `push/assets`);
recovery is narrow and object ids may not survive re-extraction (house-25822
109 → 242), so some Fieldwork edits legitimately strand on the old item,
readable forever; coco-v1 needs `prepare_coco --copy`; `reference_eval.py`
stays repo-bound; only the sqlite dev server is exercised, so R2/Neon
behaviour (gzip advertisement on 404, dedupe status codes, latency for 2 MiB
documents, > 4 MiB bodies) is unverified until a live write is authorised.

Related: [sync.md](sync.md) (mirror, revisions, MCP discovery),
[feedback.md](feedback.md) (pull), [addr-scene-api.md](addr-scene-api.md)
(overrides contract), `docs/annotation-deployment.md`.
