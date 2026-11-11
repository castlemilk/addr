# Publishing and pulling training artefacts

Training runs should not depend on this laptop's working tree. `task training:publish`
freezes a chosen set of local artefacts (datasets, edit diffs, line corrections,
sweep results, the eyes index) into a content-addressed bundle, uploads the bytes
to Fieldwork's asset store and records one immutable `addr-report` item in the
`addr-training` project. `task training:pull` fetches such a manifest anywhere
with a URL and a token, verifies every byte, and materialises the files under a
new root in their original repository-relative paths, so the existing consumers
(`export_dataset`, `prepare_coco`, `edit_diff`) run unchanged on the pulled root.

Wording matters: everything published here is a **model estimate or review
agreement as of publication**. A pulled file is never a human label, the
`publishedAt`/`gitHead` fields are audit evidence, and nothing in this flow starts
a training run. Fieldwork edits reach local reviews only through
[`task pull`](feedback.md), never through these commands.

```sh
# Plan only: reads local files, sends nothing, HEADs nothing.
task training:publish -- --select dataset-v2 edit-diffs/2026-09-29 line-corrections/2026-09-29 sweep --dry-run

# Publish to a server (refuses the live origin without FIELDWORK_PUSH_ALLOW_LIVE=1).
task training:publish -- --select dataset-v2 edit-diffs/2026-09-29 sweep --apply \
  --url https://fieldwork-annotations.ben-ebsworth.workers.dev --token-file deploy/annotation/admin-token.txt

# On the training box:
task training:manifests -- --url URL --token-file TOKEN
task training:pull -- --url URL --token-file TOKEN --latest --out /data/addr-pull --only dataset-v2,edit-diffs
```

## Groups

| group | what is staged | omitted unless asked |
| --- | --- | --- |
| `dataset-vN`, `distill-<set>` | `data/reviews/eyes/training/<name>/` (manifest, DATASET_CARD, split JSON, `labels/`), plus `<name>.export.json` | `images/` (`--with-images`); `<name>.tar` (`--tarball`, recorded as excluded when absent) |
| `edit-diffs/<date>` | `data/reviews/eyes/edit-diffs/<date>/` | |
| `line-corrections/<date>` | `data/reviews/eyes/line-corrections/<date>/` | |
| `edit-diff` | `data/reviews/eyes/training/edit-diff/` when present | |
| `sweep` | `sam3-mac/sweep/results.jsonl` and every run's `run.json`, `summary.json`, `reference-eval*/summary.json` | `*.pt` (`--include-checkpoints`); `*.log`, `steps.jsonl` always stay local |
| `coco-<set>` | `sam3-mac/<set>/manifest.json`, `*/_annotations.coco.json`, `*/sidecar.json` | `*/images/` (`--with-images`) |
| `eyes` | `data/reviews/eyes/eyes.sqlite` via `VACUUM INTO` (committed WAL included) | never a raw copy of the live file |

Every omission is listed in the manifest's `exclusions` with file counts and bytes,
so a puller can see what was left behind. Files over 64 MiB are split into
content-addressed chunks exactly like the frozen Mooroolbark bundle; the eyes
index (~380 MB) therefore uploads as several chunks and is reassembled on pull.

## What one publication is

Staging lives under `.run/training-bundles/<manifestSha12>/`:

```
manifest.json      migration.Manifest (schemaVersion 1, sceneIds [], files[{path, sha256, bytes, contentType, chunks, mtimeNs}],
                   snapshotId = sha256 of the stable part, createdAt, summary, exclusions)
                   + kind 'addr-training-manifest', groups{name: {paths, files, bytes}}, parents[], codeSha256, gitHead, scenes[]
blobs/<sha256>     every chunk (identical bytes across groups are stored once)
item.json          the addr-report document, project.json the addr-training project
```

The manifest is uploaded last by `annotation-migrate upload` (HEAD-skip dedupe,
retries, gzip negotiation, at most two workers over 8 MiB), then
`annotate save-item --revision 0` creates:

```json
{
  "id": "training-manifest-20260929-ce7c648753bb",
  "projectId": "addr-training", "adapter": "addr-report", "status": "unreviewed", "annotations": [],
  "source": {
    "manifest": {"url": "/api/v1/assets/<sha>", "sha256": "<sha>", "bytes": 253259, "contentType": "application/json"},
    "kind": "training-manifest", "snapshotId": "<sha of the stable file list>",
    "groups": {"dataset-v2": ["data/reviews/eyes/training/dataset-v2"], "sweep": ["…/sam3-mac/sweep"]},
    "parents": ["<previous manifest sha>"], "scenes": [{"sceneId", "itemId", "modelSignature", "reviewRevision", "binding"}]
  },
  "metadata": {"addr": {"kind": "training-manifest", "publishedAt": "…", "codeSha256": "…", "gitHead": "…",
                        "counts": {"files": 601, "bytes": 40207761, "scenes": 0}, "pushRunIds": []}},
  "tags": ["training-manifest", "dataset-v2", "edit-diffs/2026-09-29", "line-corrections/2026-09-29", "sweep"]
}
```

`addr-report` validation only requires `source.manifest`; the other keys are
free-form and immutable once created (revision 0 only). Re-running `--apply`
with identical files produces a new manifest (the `createdAt` differs) and a new
item; the `snapshotId` is identical across such re-publications and is the value
to compare. A 409 on `save-item` is treated as already published only when the
existing item references the same manifest sha; otherwise nothing is overwritten.
The receipt is `data/fieldwork-sync/addr-training/publish/<manifestSha12>.json`.

`--with-scenes` records the latest Fieldwork item per scene (push receipts under
`data/fieldwork-sync/mooroolbark/push/scenes/*.json` first, otherwise the frozen
item from `mirror.json`), so a pull can rebuild `data/scenes/` from Fieldwork.

## Pulling

`pull_artefacts.py` uses only the standard library. Order of operations:

1. Resolve the item (`--manifest ITEMID_OR_SHA` or `--latest`), download the
   manifest asset and check its sha and length.
2. Download every chunk of the selected groups into `<out>/blobs/<sha>`, each
   verified in memory before it touches disk (gzip transfer encoding is decoded
   first). With `--resume`, blobs already present and verified are reused.
3. Only then reassemble files under `<out>/<path>` with `O_EXCL`, re-checking the
   whole-file sha and restoring `mtimeNs`. `--resume` keeps identical existing
   files and refuses to overwrite different ones. `<out>` must not exist without
   `--resume`.
4. Write `<out>/TRAINING_MANIFEST.json` (verbatim bytes) and
   `<out>/.pull-receipt.json` (origin, item, revision, manifest sha, codeSha256,
   verified file list, counts, scenes).

A hash mismatch aborts before any file under `<out>/data` exists. `--only`
matches group names by prefix (`edit-diffs` selects every `edit-diffs/<date>`).

`--with-scenes` requires the `eyes` group in both the manifest and the selection,
because `export_dataset.eyes_references` silently drops registry references when
`eyes.sqlite` is absent. For each scene entry it fetches the addr-scene item and
writes `data/scenes/<id>/{<imageSha>.png, latest.json, review.json, models/<reviewSig>.json}`
(plus `confidence.json` when present), so the pulled root looks like the local one.

## Consumers on a pulled root `R`

```sh
scripts/ml/.venv/bin/python -m scripts.ml.training.export_dataset --root R --out R/data/reviews/eyes/training/dataset-v3 --limit 5   # needs --with-scenes and eyes
scripts/ml/.venv/bin/python -m scripts.ml.training.sam3_mac.prepare_coco --dataset R/data/reviews/eyes/training/dataset-v2 --out COCO --copy
scripts/ml/.venv/bin/python -m scripts.ml.training.edit_diff --root R
```

`prepare_coco` needs `--copy` and a dataset published with `--with-images`: the
split JSON refers to `images/<scene>.png`, and the published `coco-v1` JSON
carries absolute `/Users/benebsworth/...` paths, so always regenerate COCO from
the pulled dataset rather than consuming the published `coco-*` group directly.
`reference_eval.py` stays repo-bound (documented, not fixed).

## MCP equivalents and limits

Reads over the Fieldwork MCP (`POST /mcp` or `annotate mcp`): `list_items
{projectId:'addr-training', adapter:'addr-report'}`, `get_item {id}`. Creating
the project or item is `save_project` / `save_item` with `expectedRevision: 0`.
There is no MCP tool for asset bytes: `PUT/HEAD/GET /api/v1/assets/{sha256}` are
HTTP only, so publishing and pulling always go through the CLI over HTTPS.

Known limits: the server accepts dangling references, so the puller's sha checks
are the only integrity gate; `<out>/blobs` doubles disk use until you delete it;
re-publishing identical content creates a new item; only the sqlite dev server
has been exercised for writes (see the verification notes in
`docs/annotation/handover.md` if a live publish is ever authorised).
