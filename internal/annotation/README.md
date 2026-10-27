# Fieldwork annotation API

Standalone annotation storage for map and image adapters. The server, React
editor, Go CLI and MCP tools use the same service. It has no dependency on the
existing address, roof, geo or CRM data stores.

## Run locally

```sh
go run ./cmd/annotation-server --dev --sqlite /tmp/fieldwork.db
go run ./cmd/annotate info
go run ./cmd/annotate seed
```

Development mode must bind to loopback and must explicitly select SQLite (or
supply Postgres). SQLite persists across restarts. `seed` is an explicit command
that creates clearly named example map and image content; startup never inserts
content. Add `--static apps/annotation-web/dist` to serve a built SPA from Go.

The standalone frontend development server can proxy the data paths to Go. Set
the frontend's proxy target to the chosen `--listen` address.

## Cloud configuration

| Setting | Purpose |
| --- | --- |
| `DATABASE_URL` | Postgres runtime connection, normally Neon's pooled URL |
| `DATABASE_URL_UNPOOLED` | Direct connection for the explicit migration command |
| `ANNOTATION_TOKEN` | Workspace bearer credential, at least 24 non-whitespace characters |
| `ANNOTATION_LISTEN` | Listen address, default `:8080` |
| `ANNOTATION_ALLOWED_ORIGINS` | Comma-separated exact origins when using a separate frontend |
| `ANNOTATION_STATIC_DIR` | Optional built SPA directory containing `index.html` |

```sh
go run ./cmd/annotation-server --migrate
go run ./cmd/annotation-server
```

Run migrations against a direct Postgres connection before starting the server.
The migration is embedded, idempotent and transactionally serialized. Runtime
startup verifies schema version 1; it does not silently migrate or choose a local
database when Postgres is unavailable. Deploy behind HTTPS.

Every REST, Connect and MCP data call requires `Authorization: Bearer <token>` in
cloud mode. Static client files, `/healthz` and `/readyz` are public. Health reports
process availability; readiness checks the database without exposing its details.
Origin checks reject unapproved cross-origin requests, and data responses use
`Cache-Control: no-store`.

This version has one workspace administrator credential and an audit actor of
`workspace` (`local-dev` in explicit development). Projects organize content;
they are not separate permission domains. Multi-user identity and roles can be
added at the service boundary.

## Document contract

All IDs are strings; new project/item/label IDs may be omitted and will be
generated. Revisions are integers restricted to JavaScript's safe range. Change
cursors are decimal **strings**. `createdAt` and `updatedAt` are UTC timestamps.

```ts
type Base = {
  id: string; revision: number; createdAt: string; updatedAt: string;
};
type Project = Base & { name: string; description: string };
type Label = Base & { projectId: string; name: string; color: string };
type Annotation = {
  id: string;
  labelId: string; // empty means unlabeled
  geometry: { type: "Point" | "LineString" | "Polygon"; coordinates: unknown };
  notes?: string;
  properties?: Record<string, unknown>;
};
type Item = Base & {
  projectId: string;
  title: string;
  adapter: "map" | "image";
  source: Record<string, unknown>;
  metadata: Record<string, unknown>;
  tags: string[];
  status: "unreviewed" | "in_review" | "approved" | "rejected";
  notes: string;
  crop?: { x: number; y: number; width: number; height: number };
  annotations: Annotation[];
};
```

Map coordinates are `[longitude, latitude]` in WGS84, valid worldwide. A map
source can contain `{center: [longitude, latitude], zoom: 15, attribution: "…"}`.
Image sources require `url` (HTTP(S), or an absolute site path), with optional
positive integer `width`/`height` and attribution. The server stores source
references; it does not download or rewrite originals.

Image geometries and crop rectangles use the **original image's** normalized
0–1 coordinate system. A rectangle is a closed Polygon with
`properties: {shape: "rectangle"}`. Cropping changes the view transform only;
it never destroys original pixels or changes stored annotation coordinates.
The image URL and dimensions are immutable after creation: use a new item for
a different image. Project IDs and adapter IDs are also immutable.

Polygons require closed rings with nonzero area. Coordinates, map bounds, crop
bounds, annotation IDs and referenced labels are validated on the server. An
annotation may only use a label in its own project. A referenced label cannot be
deleted until those annotations are relabeled or removed.

An item and all of its annotations save **atomically**. Send the complete item
and the revision read from the server. Updates with a stale revision return HTTP
409 / Connect `aborted`; they do not overwrite another editor's work. Creation
uses `expectedRevision: 0`. Delete also requires the current revision, and IDs
of deleted documents cannot be reused. Changing annotations/crop on an already
approved item resets it to `in_review`; approval requires a subsequent save.
These are generic review states, not approval for a downstream training pipeline.

Store adapter-specific provenance in `metadata`, for example:

```json
{"origin":"address-pipeline","externalId":"source-record-id","modelVersion":"v1","captureDate":"2026-09-01","confidence":0.82}
```

Import source outputs as `unreviewed`; downstream consumers decide their own
eligibility rules. This keeps machine output distinct from reviewed content.

## Connect and REST

The generated service is `annotation.v1.AnnotationService`, from
`proto/annotation/v1/annotation.proto`. Go bindings are in `gen/go/annotation`,
Connect-ES v1 bindings in `src/gen/annotation`. The common envelopes are
`APIRequest{data: google.protobuf.Struct}` and `APIResponse{data: Struct}`. The
envelope permits adapter metadata to evolve without regenerating every client.
Buf naming exceptions are local to these intentional shared RPC envelopes.

```ts
const response = await client.saveItem({
  data: Struct.fromJson({item, expectedRevision: item.revision}),
});
const saved = response.data!.toJson();
```

REST `POST /api/v1/rpc/{Method}` accepts the direct JSON request below and returns
the direct JSON response (without the Connect `data` envelope).

| Method | Request | Response |
| --- | --- | --- |
| `GetInfo` | `{}` | `{name,version,adapters,storage,authRequired,maxDocumentBytes}` |
| `ListProjects` | `{cursor?,limit?,query?}` | `{projects,nextCursor}` |
| `SaveProject` | `{project,expectedRevision}` | `{project}` |
| `ListItems` | `{projectId,cursor?,limit?,query?,status?,adapter?}` | `{items,nextCursor}` |
| `GetItem` | `{id}` | `{item,labels,labelsNextCursor}` |
| `SaveItem` | `{item,expectedRevision}` | `{item}` |
| `DeleteItem` | `{id,expectedRevision}` | `{id,deleted:true}` |
| `ListLabels` | `{projectId,cursor?,limit?,query?}` | `{labels,nextCursor}` |
| `SaveLabel` | `{label,expectedRevision}` | `{label}` |
| `DeleteLabel` | `{id,expectedRevision}` | `{id,deleted:true}` |
| `SaveAnnotation` | `{itemId,annotation,expectedRevision}` | `{item}` |
| `DeleteAnnotation` | `{itemId,id,expectedRevision}` | `{item}` |
| `ListChanges` | `{projectId?,cursor?,limit?}` | `{changes,nextCursor,hasMore}` |
| `ExportProject` | `{projectId}` | `{schemaVersion:1,project,items,labels,cursor}` |

Annotation mutations compare and increment the **parent item's** revision.
`SaveAnnotation` creates an annotation ID when omitted. `SaveItem` requires IDs
for its annotation list. All list calls default to 100 and cap at 500. An empty
`nextCursor` means the document list is exhausted. Change pages retain the last
cursor even when empty; use `hasMore` for continuation. Search is a literal
substring of item titles or project/label names.

Convenience REST routes are `GET /api/v1/info`, `GET|POST /api/v1/projects`,
`GET|POST /api/v1/items`, `GET|POST /api/v1/labels`, `GET /api/v1/items/{id}`,
`PUT /api/v1/{projects|items|labels}/{id}`,
`DELETE /api/v1/{items|labels}/{id}?expectedRevision=N`,
`GET /api/v1/projects/{id}/{items|labels}`, `GET /api/v1/changes`, and
`GET /api/v1/export?projectId=…&format=ndjson`. Query strings follow the same
field names, with `after` accepted as an alias of `cursor` for changes. POST/PUT
use the same `{project|item|label,expectedRevision}` wrapper as the RPC.

## Pulling changes and exporting

Every create, update and delete appends a change in the same transaction as the
document write. Changes contain `{cursor,projectId,kind,id,operation,revision,
actor,timestamp,document}`. Operations are `create`, `update`, or `delete`.
For delete, `document` is the last stored snapshot and `revision` is the deletion
revision. The singleton clock row orders commits across replicas, preventing a
later cursor from becoming visible before an earlier uncommitted write.

An export reads the project and its cursor from one database snapshot. Consume
that snapshot, then request `ListChanges` with its cursor. Persist `nextCursor`
only after processing the page. For a fresh incremental mirror, start at `"0"`.
Keep the project filter constant while reusing a cursor.

```sh
annotate sync --project PROJECT_ID --dir PATH
annotate export --project PROJECT_ID --format ndjson > snapshot.ndjson
annotate changes --project PROJECT_ID --after 42 --limit 100 > changes.json
annotate save-item --file item.json --revision 3
annotate import --file snapshot.ndjson --format ndjson
```

`annotate sync` creates a checksummed `PATH/mirror.json` containing the current
project, labels, items, exact server URL/project binding, and cursor. Later runs
fetch only changes since the last successful run, including deletes. The CLI
locks the directory and atomically replaces the documents and checkpoint
together; failures before replacement leave the prior mirror intact. A no-op
does not rewrite the file. Local mirror edits are not pushed. See the
[sync guide](../../docs/annotation/sync.md) for recovery and API/MCP examples.

CLI JSON exports are compact. NDJSON export emits `{kind,document}` records for project, label and item,
followed by `{kind:"checkpoint",schemaVersion:1,cursor}`. Imports perform
individual revision-checked writes and report failure; they are not an atomic
multi-document transaction. CLI import accepts up to 64 MiB of input. Read
`annotate --help` for all commands.

Requests cap at 4 MiB, documents at 2 MiB, list/change pages at 8 MiB of stored
documents, and snapshots at 32 MiB. Larger datasets should synchronize through
paginated changes; `annotate export` automatically uses that fallback for large
projects. There is no automatic retention deletion of the change feed. Protected
source assets are separate: use `annotation-migrate pull` for a complete bundle.

## MCP

`POST /mcp` supports stateless Streamable HTTP JSON responses with protocol
version `2025-11-25`, plus compatible `2025-03-26`/`2025-06-18` clients.
Initialize, ping, notifications/initialized, tools/list, tools/call, resources/list,
resources/templates/list, and resources/read are implemented. GET returns 405
because the service offers no SSE stream.

Read tools are `get_info`, `list_projects`, `list_items`, `get_item`, `list_labels`,
`list_changes`, `export_project`, and `search_mentions`. Mutation tools are `save_project`,
`save_label`, `save_item`, `save_annotation`, `delete_annotation`, `delete_item`,
and `delete_label`. Each write explicitly requires `expectedRevision`; annotation
operations compare and increment the parent item's revision. All tools use the
same service validation and authentication as REST/Connect, expose read/write
hints and display titles in discovery, and return structured content. Domain tools
also return text JSON. Conflicts
and validation failures return `isError:true` without overwriting newer work.

`search_mentions` follows the [OpenAI composer at-mention extension](https://github.com/openai/mcp-extensions/blob/main/docs/spec.md#composer-at-mentions).
Its tool metadata advertises `openai/extensions.mentions/search` and app visibility;
it accepts exactly `{query:string}`, including an empty query, and returns
`structuredContent.items` with up to 20 resource links. Project names and item
titles are searched across the workspace with bounded queries in stable ID order.
Matching project and item links alternate so both are available
in the picker. Links contain IDs, titles and project context, without source assets
or credentials. `resources/list` is empty because documents are discovered through
search and the two resource templates.

`resources/read` resolves canonical `fieldwork://project/{id}` and
`fieldwork://item/{id}` links to the current JSON documents through the shared
service. Item resources include annotations and labels, matching `get_item`;
they do not fetch protected asset bytes. These resources use the same bearer
authentication and origin checks as every other data endpoint. Reads and searches
do not modify revisions or the change feed. The server does not advertise MCP Apps,
form elicitation, subscriptions, or the newer `server/discover` protocol.

MCP lists default to 20 records per page. `list_changes` optionally accepts
`includeDocuments:false` to return compact change metadata; its default remains
`true`. Complete mirrors require document payloads and a fixed project filter.
The optional project/cursor arguments retain compatibility with global-feed
clients. Bulk consumers should use `annotate sync`; protected source asset bytes
travel separately over HTTP. HTTP clients send
`Accept: application/json, text/event-stream`, `Content-Type: application/json`
and the bearer credential. Subsequent calls send `MCP-Protocol-Version`.

For an editor with stdio MCP support, build `go build -o /path/to/annotate
./cmd/annotate` and configure:

```json
{
  "mcpServers": {
    "fieldwork": {
      "command": "/path/to/annotate",
      "args": ["mcp"],
      "env": {
        "ANNOTATION_URL": "https://your-fieldwork-host",
        "ANNOTATION_TOKEN": "YOUR_WORKSPACE_CREDENTIAL"
      }
    }
  }
}
```

The stdio command forwards JSON-RPC to the authenticated HTTP endpoint. It emits
only MCP messages on stdout; diagnostics go to stderr.

## Extending and verifying

Implement `Adapter.Info()` and `Adapter.Validate(*Item)`, then pass
`WithAdapters(customAdapter)` to `OpenStore`. Register a corresponding frontend
canvas/editor adapter. The core store, optimistic revisions, exports and change
feed remain shared. Registration happens at startup, not from uploaded content.

```sh
go test -race ./internal/annotation ./cmd/annotation-server ./cmd/annotate
go vet ./internal/annotation ./cmd/annotation-server ./cmd/annotate
buf lint --path proto/annotation
buf generate --path proto/annotation
```

Set `ANNOTATION_TEST_DATABASE_URL` to a direct test Postgres URL to enable the
live integration test. It creates and drops a uniquely named temporary schema;
application records are untouched. The test covers compare-and-swap writes,
commit cursor ordering, pagination, snapshot resume, restart persistence and
deleted-ID protection. SQLite tests exercise the same store behavior, and HTTP
tests cover all auth surfaces, origins, real Connect calls and MCP lifecycle.

Protocol references: [MCP Streamable HTTP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports),
[Neon connection pooling](https://neon.com/docs/connect/connection-pooling).

## Frozen local datasets and protected assets

`addr-scene` keeps the original model, review, image and related evidence in
content-addressed assets. Its annotations project every selected source object
into normalized polygons, including holes, rejected objects and superseded
objects. `properties.addr.objectId` links each annotation back to the raw source;
`layer` records `review` or `model`. The frontend records sparse object edits in
`properties.addr.overrides`. Source geometry in pixels and roof geometry in
metres/Z remain intact in the raw assets. Model estimates and machine review
statuses are never promoted to human approval by an import.

Scene source fields are `sceneId`, `image`, `model`, optional `review`,
`reviewModel`, `confidence`, `assets`, `analysisRefs`, plus `modelSignature`,
`imageSha256`, `width`, and `height`. A ref is
`{url:"/api/v1/assets/SHA256",sha256,bytes,contentType,path?}`. The importer
requires the model and review to use the same image hash, dimensions and pixel
matrices before constructing an editable projection. Imported source references
are immutable; a changed source snapshot requires a new item.

`addr-report` provides immutable `manifest`, `overview`, `map`, `summary`,
`boundary`, and `crm` refs and the `sceneItems` lookup. The Mooroolbark report
ID is `000-mooroolbark-report`, so it appears first. Scene IDs are
`addr-scene-SCENE_ID`; class labels are `mooroolbark-kind-KIND`. A different
`--project` namespaces its scene and label IDs. Every imported scene starts
`unreviewed` or `in_review`; original object statuses and actor provenance remain
available in the raw review and annotation properties.

Assets use the same authentication as the API:

```text
PUT  /api/v1/assets/<lowercase SHA-256>  raw bytes, Content-Length, Content-Type
HEAD /api/v1/assets/<lowercase SHA-256>  length, type, ETag, X-Content-SHA256
GET  /api/v1/assets/<lowercase SHA-256>  original bytes, supports Range
```

Uploads validate the digest and cap at 64 MiB. Repeated matching uploads are
safe; existing bytes never change. Unknown or active content types are served as
octet-stream attachments. Cloud deployments route assets to the private R2
binding. An explicit `--asset-dir /path` (or `ANNOTATION_ASSET_DIR`) enables the
same filesystem-backed API on the Go server for local testing. No asset listing,
public URL or deletion endpoint is provided.

Build the migration tool with `go build -o bin/annotation-migrate
./cmd/annotation-migrate`. A frozen bundle contains `manifest.json` and
`blobs/<sha256>`. Each manifest file records its original relative `path`,
whole-file `sha256`, `bytes`, `contentType`, ordered `chunks:[{sha256,bytes}]`,
and optional `mtimeNs` string. Each chunk is at most 64 MiB. Original filenames,
all raw file bytes and recorded timestamps can be restored; sanitized SQLite
snapshots preserve committed WAL state while excluding installation credentials.
The manifest itself is uploaded as a separate immutable asset.

```sh
# Local-only preflight: no credential is read or server contacted.
annotation-migrate plan --bundle PATH --plan-file /tmp/projected-documents.json

# Use the private server selected by ANNOTATION_URL; reads ANNOTATION_TOKEN.
annotation-migrate import --bundle PATH --url https://YOUR_HOST --concurrency 4
annotation-migrate verify --bundle PATH --url https://YOUR_HOST

# Also download and hash every remote asset, rather than checking HEAD metadata.
annotation-migrate verify --bundle PATH --url https://YOUR_HOST --deep

# Preserve the complete remote dataset and all later edits, including history.
annotation-migrate pull --url https://YOUR_HOST --project mooroolbark \
  --output /tmp/fieldwork-pulled
annotation-migrate restore --bundle /tmp/fieldwork-pulled \
  --output /tmp/fieldwork-source
```

`--token-file PATH` is available for remote commands. Local `plan` and `restore`
never read credentials. Each command checks chunk hashes and whole-file
reconstruction. `import` resumes missing assets by digest, preflights all existing
documents, skips identical documents, and creates with `expectedRevision:0`.
It refuses different content instead of overwriting edits. Multi-document imports
can be resumed after interruption; they are not one database transaction.

`pull` writes a self-contained raw bundle plus `fieldwork-state.ndjson` and
`fieldwork-changes.ndjson`. It replays the append-only feed through the returned
checkpoint, retaining later edits and deletions without the 32 MiB export cap.
The state file uses the standard `{kind,document}` and checkpoint format for
`annotate import --format ndjson` into an empty installation. Upload the pulled
bundle assets with `annotation-migrate upload` first. Keep its
`fieldwork-assets.json` catalog alongside `manifest.json` and `blobs/`: upload
validates that catalog and sends every referenced blob, including assets added
after the original snapshot and evidence found only in edited or deleted
revisions. Missing or corrupt catalog blobs fail before any upload. `restore`
reconstructs the frozen source hierarchy separately from the later editable state
and refuses an existing destination. Large Mooroolbark exports use this paginated workflow;
the ordinary atomic JSON export remains bounded at 32 MiB.

The inventory and scope decisions are in
`docs/annotation/mooroolbark-inventory.md`; `scripts/annotation_snapshot.py`
produces the frozen bundle and canonical processing parity evidence. It does not
run inference. Explicit exclusions include credentials, model weights, and
disposable imagery cache files that are not referenced by preserved scene state.
