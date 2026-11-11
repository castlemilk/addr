# Syncing Fieldwork data

Use `annotate sync` to keep a local copy of editable documents. Use revision-checked
CLI, API, or MCP writes to submit changes. For imported frozen-bundle projects
such as Mooroolbark, `annotation-migrate pull` also retrieves original imagery,
frozen source files, and history. Generic map/image projects use document sync
or export and retain their existing image URLs.

Run `task pull` to sync cloud edits into local scene reviews. The
[Fieldwork feedback intake](feedback.md) applies compatible changes automatically
and reports conflicts. Imported geometry remains a draft until checked locally;
training stays a separate action.

Training artefacts (datasets, edit diffs, line corrections, sweep results, the eyes
index) travel the other way: `task training:publish` records them as one immutable
content-addressed manifest item in the `addr-training` project and
`task training:pull` restores them, hash-verified, on any machine. See
[training artefacts](training-artefacts.md); published values are model estimates
and review agreement, never human labels.

## Upload or resume the frozen Mooroolbark dataset

```sh
go build -o bin/annotation-migrate ./cmd/annotation-migrate
bin/annotation-migrate import \
  --bundle data/annotation-snapshots/mooroolbark-23591d058ab7 \
  --project mooroolbark --concurrency 32 \
  --url https://fieldwork-annotations.ben-ebsworth.workers.dev \
  --token-file deploy/annotation/admin-token.txt
```

This validates the frozen files, uploads only missing content hashes, and creates
the projected documents after checking existing records. Repeating it skips
identical documents and assets; an interruption can be retried with the same
command. Imports are resumable, not a single transaction. Different existing
content causes a conflict instead of overwriting feedback. Once people start
editing, use the incremental pull and revision-checked writes below. A newer
local analysis snapshot requires a separate project or an explicit reconciliation;
this command does not merge new model output into reviewed annotations.

For a metadata and document comparison against the untouched frozen import:

```sh
bin/annotation-migrate verify \
  --bundle data/annotation-snapshots/mooroolbark-23591d058ab7 \
  --url https://fieldwork-annotations.ben-ebsworth.workers.dev \
  --token-file deploy/annotation/admin-token.txt
```

Add `--deep` to download and hash every asset. After feedback edits, document
differences from the frozen projection are expected; `sync` retrieves that new
state, and `pull` preserves it alongside the original archive.

## CLI: incremental document pull

Build the CLI once, then repeat the same sync command for updates:

```sh
go build -o bin/annotate ./cmd/annotate
bin/annotate sync --project mooroolbark --dir data/fieldwork-sync/mooroolbark \
  --url https://fieldwork-annotations.ben-ebsworth.workers.dev \
  --token-file deploy/annotation/admin-token.txt
```

`ANNOTATION_URL` and `ANNOTATION_TOKEN` are also supported. Remote connections
require HTTPS; localhost may use HTTP. Tokens are never written into the mirror.

The first run reads the committed change feed from cursor `"0"`. Later runs
request only changes after the saved cursor. Creates and updates replace their
local documents; deletes remove them. The compact JSON summary reports the old
and new cursor, changes applied, upserts, deletions, current item/label counts,
and the mirror path. An unchanged project reports `"changes":0,"updated":false`.

`mirror.json` contains `origin`, `projectId`, `schemaVersion`, `project`, `labels`,
`items`, `cursor`, and `sha256`, plus optional checksummed `itemProvenance` with
the latest item actor/timestamp observed during sync. Older mirrors without that
field remain valid. Actor values such as `workspace` are audit evidence, not
named human approval. The document collections have the same shape as
an export. Source references are retained; referenced asset bytes are separate.
For example, `jq '.items[] | {id,revision,status}' PATH/mirror.json` reads the
current item state. Treat the mirror as generated data: copy an item out before
editing it for a save.

The mirror is bound to the exact server base URL (ignoring trailing slashes) and
project ID. A different URL, project, invalid document, or checksum mismatch
fails before fetching changes. Use a separate directory for another server or
project. To rebuild a damaged mirror, preserve its directory and sync into a new
one; do not change its cursor by hand.

Documents and checkpoint are published together by one atomic file replacement
after every page has been fetched and validated. A network failure or write
failure before that replacement preserves the last successful mirror. Retry the
same command; it resumes from the previous successful run. If the initial pull
fails, the retry starts at `"0"`. If directory durability cannot be confirmed
after replacement, the command explicitly reports that the new documents and
checkpoint were committed together; a retry is safe.

A process lock prevents concurrent writers to the same mirror. The OS releases
it when the process exits; the `.sync.lock` file remains and should not be deleted
to bypass a running sync. Local sync supports macOS, Linux, and BSD. `--limit N`
sets page size from 1 to 500 (default 20). Smaller pages keep large scene documents
within the server's response deadline. Local state is held in memory while
applying a batch, and the mirror file is rewritten only when changes arrive.

## Publish local scene changes as new items

`task push -- --dry-run` plans and `task push` publishes changed local scene
frames and review state as new immutable `addr-scene` items
(`addr-scene-<S>-m<sig12>-r<rev>`), marking the older items `superseded-frame`
with a revision-checked patch. Push never writes `overrides`, never deletes,
and refuses scenes whose Fieldwork items carry unpulled edits. Receipts live
under `data/fieldwork-sync/<project>/push/`; see [push.md](push.md).

## Submit edits with an explicit revision

```sh
bin/annotate get --id ITEM_ID \
  --url https://fieldwork-annotations.ben-ebsworth.workers.dev \
  --token-file deploy/annotation/admin-token.txt > current.json
jq '.item' current.json > item.json
# Edit item.json, preserving the source and fields you want to retain.
bin/annotate save-item --file item.json --revision 3 \
  --url https://fieldwork-annotations.ben-ebsworth.workers.dev \
  --token-file deploy/annotation/admin-token.txt
```

Connection flags apply to one command; alternatively configure `ANNOTATION_URL`
and `ANNOTATION_TOKEN` for the shell session. Replace `ITEM_ID` with the item's
ID and `3` with the revision returned by the read. A stale revision fails with HTTP
409 / Connect `aborted`; reload the latest item and reconcile the edit before
retrying. No tool blindly overwrites newer work. Revision `0` creates a new
document. `SaveItem` replaces the complete item, so omitted optional fields
reset; `SaveAnnotation` and `DeleteAnnotation` edit one annotation while
preserving the rest of its parent.

The local mirror is a pull destination. Editing it does not upload changes, and
sync rejects a modified checksum. API/MCP/CLI saves use the same validation and
appear in subsequent syncs. Document sync and cloud saves do not rewrite local ML
reviews. `task pull` additionally reconciles compatible feedback through the
canonical local scene save path; frozen source snapshots remain unchanged.

The reverse direction is `task push` (`docs/annotation/local-to-fieldwork.md`):
changed local scene frames and review state are published as **new** immutable
`addr-scene` items (`addr-scene-<S>-m<sig12>-r<rev>`, `expectedRevision 0`)
with content-addressed assets; the superseded item receives only a
revision-checked `superseded-frame` tag and `metadata.addr.supersededBy`.
Push never writes `properties.addr.overrides`, never calls `DeleteItem`, plans
with `-- --dry-run` (ListChanges/GetItem/GetInfo/HEAD only) and refuses the
live origin without `FIELDWORK_PUSH_ALLOW_LIVE=1`. A pushed item is the model
estimate plus the local review state as of publication, not a human label.

## API contract

REST RPC accepts direct JSON at `POST /api/v1/rpc/{Method}`. Connect uses the same
methods at `/annotation.v1.AnnotationService/{Method}`, with the existing
`{data: ...}` envelope. Both use the same workspace bearer credential.

```sh
curl -fsS "$ANNOTATION_URL/api/v1/rpc/ListChanges" \
  -H "Authorization: Bearer $ANNOTATION_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"projectId":"mooroolbark","cursor":"0","limit":20}'
```

The response is `{changes,nextCursor,hasMore}`. Each change has
`{cursor,projectId,kind,id,operation,revision,actor,timestamp,document}`:

- `kind` is `project`, `label`, or `item`; `operation` is `create`, `update`, or
  `delete`. Upserts carry the complete current document.
- A delete carries the last document snapshot. Its change revision is one
  greater than that snapshot's revision; remove the document by kind and ID.
- Cursors are decimal **strings**, including values above JavaScript's exact
  integer range. Keep the project filter fixed. Global cursor gaps are valid.
- Apply a page and its `nextCursor` together. Keep paging while `hasMore` is true.
  An empty final page retains the input cursor; unlike document lists, an empty
  `nextCursor` is not the change-feed completion signal.

API `projectId` and `cursor` remain optional for compatibility: omission reads
the global feed from `"0"`. Supply both for a resumable project mirror.

| Operation | Request |
| --- | --- |
| `SaveProject` | `{project,expectedRevision}` |
| `SaveLabel` | `{label,expectedRevision}` |
| `SaveItem` | `{item,expectedRevision}` |
| `SaveAnnotation` | `{itemId,annotation,expectedRevision}` |
| `DeleteAnnotation` | `{itemId,id,expectedRevision}` |
| `DeleteItem`, `DeleteLabel` | `{id,expectedRevision}` |

Annotation operations use the **parent item's revision**, increment it, and
return the updated parent item. For an annotation create, its ID may be omitted;
for an update, include the existing ID and complete annotation. Read current
documents with `GetItem`, `ListProjects`, or `ListLabels`. `GetInfo` exposes
supported adapters, coordinate systems, geometry types, and limits.

## MCP: discovery, reads, and writes

The remote endpoint is `/mcp`. It supports stateless Streamable HTTP for MCP
`2025-11-25` and compatible `2025-03-26` / `2025-06-18` clients. Use the existing
bearer token, `Content-Type: application/json`, and
`Accept: application/json, text/event-stream`. The local stdio bridge can read
the token from a file without embedding its value in client configuration:

```json
{
  "mcpServers": {
    "fieldwork": {
      "command": "/Users/benebsworth/projects/addr/bin/annotate",
      "args": [
        "mcp",
        "--url", "https://fieldwork-annotations.ben-ebsworth.workers.dev",
        "--token-file", "/Users/benebsworth/projects/addr/deploy/annotation/admin-token.txt"
      ]
    }
  }
}
```

`tools/list` exposes 14 tools with input schemas and read/write annotations:

- Discovery/reads: `get_info`, `list_projects`, `list_items`, `get_item`,
  `list_labels`, `list_changes`, `export_project`.
- Writes: `save_project`, `save_label`, `save_item`, `save_annotation`,
  `delete_annotation`, `delete_item`, `delete_label`.

Mutation tools require `expectedRevision` explicitly. They call the same service
operations as REST/Connect and return tool errors with `isError:true` on
validation failures and revision conflicts. Successful results include JSON text
and `structuredContent`. Nested source, metadata, and annotation properties stay
open JSON objects, including adapter-specific fields.

MCP list tools default to 20 records per page to keep replies manageable. For
compact change inspection, omit documents explicitly:

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "list_changes",
    "arguments": {
      "projectId": "mooroolbark",
      "cursor": "0",
      "limit": 20,
      "includeDocuments": false
    }
  }
}
```

`includeDocuments` defaults to `true`, preserving the original payload contract.
Set it to `true` when applying a full mirror; metadata-only responses cannot
reconstruct documents. Use `annotate sync` for bulk pulls so large annotation
payloads do not need to pass through an agent's conversation. Individual edits
can use `save_annotation` / `delete_annotation` without resending a whole item.
Geometry supports points, lines, and polygons according to the item's adapter.

Check each adapter's `writeHints` from `get_info` before editing imported data.
For Mooroolbark, object notes live in `properties.addr.overrides.notes`, and
rejection retains the original annotation. See the
[address-scene write contract](addr-scene-api.md) for a complete example.

## Exports and original assets

`annotate export --project ID --format json|ndjson` returns compact JSON or
NDJSON. When the server's 32 MiB snapshot limit is exceeded, the CLI automatically
replays paginated changes from `"0"` and produces the same snapshot format with
its final cursor. MCP/API `ExportProject` retain their 32 MiB limit; large
consumers use `ListChanges`. CLI import is create-only and limited to 64 MiB of
input; large continuous workflows should use document sync and individual
revision-checked writes.

MCP handles structured documents and feedback. Original assets and frozen files
use authenticated `PUT`, `GET`, and `HEAD` at `/api/v1/assets/{sha256}`, where the
lowercase SHA-256 digest identifies immutable content. Uploads validate that
digest and the declared content length; downloads return `X-Content-SHA256`.

For faster uploads, an authenticated asset `HEAD` response advertises
`Accept-Encoding: gzip`, including when the asset does not exist yet. The
migration CLI then compresses missing non-image blobs larger than 256 KiB when
gzip saves more than 10%. Older servers without that advertisement receive
original bytes. A compressed `PUT` uses `Content-Encoding: gzip`, the compressed
wire size as `Content-Length`, and the original size as `X-Uncompressed-Length`.
Both sizes are bounded at 64 MiB. The server decodes as a stream and checks the
original size and SHA-256 before publishing the original bytes. Downloads,
manifest digests, and resumed uploads keep the same contract. Temporary
compressed files are removed after each attempt, and retries resend complete
bodies. Progress byte counters describe original source bytes, not wire bytes.

Use `annotation-migrate import` / `pull` for the complete dataset workflow,
including chunked files, rather than putting asset bytes into base64 MCP
payloads. For a complete portable download, choose a fresh directory:

```sh
bin/annotation-migrate pull --project mooroolbark \
  --url https://fieldwork-annotations.ben-ebsworth.workers.dev \
  --token-file deploy/annotation/admin-token.txt \
  --output /private/tmp/mooroolbark-download
```

This command requires an imported frozen-bundle project with a dataset report
and manifest. It includes the raw bundle, protected assets referenced by
later edits, current documents, and change history. Its `restore` command
reconstructs the frozen original files; editable feedback remains separate. See
the [handover](handover.md) for restore and parity verification commands.
