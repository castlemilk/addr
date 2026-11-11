# Fieldwork deployment

Fieldwork runs the React/Tailwind application as Cloudflare Worker static assets, routes Connect, REST and MCP requests to the Go `annotation-server` in Cloudflare Containers, and stores annotation documents in Neon PostgreSQL. The browser and API share one origin.

The existing addr geography, roof and ML tooling remains a local application. Fieldwork has separate canonical storage; the Mooroolbark migration preserves the frozen source alongside editable annotations. See the [parity report](annotation/mooroolbark-parity.md) for dataset verification and scope.

## Resources

| Resource | Value |
| --- | --- |
| Application | `https://fieldwork-annotations.ben-ebsworth.workers.dev` |
| Cloudflare Worker | `fieldwork-annotations` |
| Worker version at latest verified deployment | `8f2a48d1-8f9d-432d-bceb-587a1cade64f` |
| Container | `AnnotationBackend`, one `lite` instance, automatic placement, sleeps after 5 idle minutes |
| Container application | `a03e7a53-0294-4cb8-9cf8-5164b7b9a053` |
| Private asset bucket | `fieldwork-private-assets`, Standard class, Oceania placement hint |
| Neon project | `Fieldwork` / `bold-snow-43794461` |
| Neon branch | `br-sweet-sunset-a7t0xq8z` |
| Database | `fieldwork`, PostgreSQL 18, Sydney |
| Database compute | 0.25–1 CU, suspends after 300 idle seconds |

Resource IDs and settings are recorded in `deploy/annotation/neon-project.json`; it contains no credentials. Container disks are disposable. All durable documents, revisions and the change log live in PostgreSQL.

## Private imagery and snapshots

The private R2 bucket `fieldwork-private-assets` was provisioned on 2026-09-27. Its `r2.dev` access is disabled and it has no custom domains. The deployed asset extension binds it as `ANNOTATION_BLOBS`. The user authorized the live dataset upload and credential use on 2026-09-27. Keep public bucket access disabled. The Worker uses its R2 binding directly; no S3 credential is needed by the Go container or browser.

`/api/v1/assets/{sha256}` accepts authenticated `PUT`, `GET` and `HEAD` requests. The path is a lowercase, 64-character SHA-256 digest; it never includes a token or any query parameters. Use the same workspace bearer header as the annotation API. Cross-origin access is restricted to the configured exact origins. Images are fetched with that header before the browser creates a temporary blob URL.

Uncompressed uploads contain raw bytes, a `Content-Length` no greater than 67,108,864 bytes (64 MiB), and a `Content-Type`. Negotiated gzip uploads follow the extension below. R2 verifies the SHA-256 checksum before an immutable conditional create. A new object returns 201; an already verified object with the same digest and length returns 200 without rewriting bytes or metadata. Both responses contain `{url, sha256, bytes, contentType}`. A checksum mismatch returns 400; a conflicting existing object returns 409. Retries therefore resume without altering existing assets.

Downloads support one HTTP byte range, digest-based ETags, and `X-Content-SHA256`. All responses disable caching and transformations. Stored HTML, SVG and other active content is served as an `application/octet-stream` attachment with `nosniff` and a sandbox CSP. Only the small explicit image, JSON and plain-text allowlist is served inline. There is no public listing or deletion endpoint.

Uploads can negotiate gzip through the authenticated HEAD response's
`Accept-Encoding: gzip`, including on a missing object. Compressed requests use
their wire `Content-Length`, `Content-Encoding: gzip`, and
`X-Uncompressed-Length`. Both lengths are bounded at 64 MiB. The server validates
the decoded length and original SHA-256 and stores the original bytes; download
and range semantics are unchanged. The migration CLI opts in automatically,
uses bounded temporary files, and skips compression when savings are small.
[The live 64 MiB canary](annotation/mooroolbark-verification/production-gzip-canary.json)
verified the full downloaded digest after a 4.83 MB upload.

Complete source preservation uses an immutable manifest that maps each original relative path to its byte count and SHA-256. Files larger than 64 MiB are split into ordered chunks; retain the whole-file hash as well as each chunk's hash. Store all raw scene models, reviews, revision history, analysis and imagery through this route, and retain manifest references in the editable annotation records. Keep original model inference, machine decisions, human review and measured labels distinct. A successful normalized import alone does not establish source parity: download and hash every blob, reconstruct chunked files, compare every manifest path and verify document counts and provenance separately.

The current document API limits remain 2 MiB per document, 4 MiB per request, 500 documents and 8 MiB per list/change page, and 32 MiB per whole-project export. The general CLI import buffers up to 64 MiB and is create-only. Large source bundles need the dedicated resumable migration flow; increasing these limits or putting binary data into JSON is unnecessary.

Run `npm --prefix deploy/annotation test` for the private asset route's local Workers/R2 integration checks. The bucket has already been created with `wrangler r2 bucket create fieldwork-private-assets --location oc`; the location is a placement hint. After deploying the Worker, verify unauthenticated denial, a checksum-rejected upload, and an authenticated byte-for-byte round trip. Relevant platform behavior is documented in [R2 private bucket access](https://developers.cloudflare.com/r2/buckets/public-buckets/), [R2 checksum and conditional operations](https://developers.cloudflare.com/r2/api/workers/workers-api-reference/), and [Workers streamed response lengths](https://developers.cloudflare.com/workers/runtime-apis/response/).

The previously verified release on 2026-09-27 uses Go image
`sha256:1bc1e77fb22002fba106cb4bb36e800f828b3448850fbc41e5c4b59ece415c3a`
and frontend `index-CHuhBMYV.js` / `index-Be5f2Y4e.css`. The usability update
adds connection-required onboarding, phone inspector saving, clearer capture
identification and adapter-aware image filtering. It also includes gzip asset uploads, revision-checked
MCP writes, the imported-scene write contract, per-item label-query deduplication
and the pooled PostgreSQL protocol fix. Container version 7 is active in Sydney,
readiness returns 200, and anonymous asset access returns 401.
[Database protocol fix verification](annotation/mooroolbark-verification/production-protocol-fix-verification.json)
records 64/64 production reads without retries after the database fix.
[Earlier UI deployment](annotation/mooroolbark-verification/production-final-ui-deployment.json)
records the initial 20-item queue and loading fixes; the
[preceding deployment checks](annotation/mooroolbark-verification/production-deployment.json)
record asset authorization and the original request failures. Authenticated phone, MCP,
REST and CLI feedback passed the
[live sync round trip](annotation/live-sync-verification.json). Source-data
verification is recorded separately in the
[Mooroolbark parity report](annotation/mooroolbark-parity.md).

PostgreSQL connections use lib/pq's native DSN parser with
`BinaryParameters=true`. This keeps Parse/Bind/Execute in one protocol exchange
so the Neon transaction pool cannot reassign an unnamed statement between
preparation and execution. The pooled URL is unchanged. JSON documents are
passed as strings; the isolated Neon integration verifies writes and revisions.
Sanitized server diagnostics record method, operation stage and PostgreSQL
SQLSTATE without request data, credentials or statement parameters.

## Credentials

Local deployment values are in the Git-ignored `deploy/annotation/.dev.vars`:

- `DATABASE_URL`: pooled PostgreSQL URL, used by the running service.
- `DATABASE_URL_UNPOOLED`: direct PostgreSQL URL, used only for schema migrations.
- `ANNOTATION_TOKEN`: random workspace administrator bearer token, at least 24 characters.

The local launch token is also stored in the Git-ignored `deploy/annotation/admin-token.txt` so it can be copied into the browser connection screen or supplied to the CLI with `--token-file`. These files have mode `0600`. Do not place the token in a URL or in a `VITE_*`/`NEXT_PUBLIC_*` variable.

`npm --prefix deploy/annotation run secrets` uploads the pooled URL and the administrator token to Cloudflare Worker secrets. When both are configured, it also uploads `FIREBASE_WEB_CONFIG` and `FIREBASE_ALLOWED_EMAILS` for Google login. The direct migration URL is not uploaded. The Worker passes only the database and administrator token into the Go container; neither secret is bundled into static assets.

The 9 October 2026 deployment adds Google account login through Firebase alongside the existing workspace administrator credential; see [Google login setup and verification](annotation-google-login.md), including the outstanding browser callback check. Both credential types grant workspace-wide access without separate roles, and the shared backend audit actor is preserved. Browser requests are restricted to the exact application origin in `wrangler.jsonc`. When adding a custom domain, update `ANNOTATION_ALLOWED_ORIGINS` and the Firebase authorized domains, then redeploy.

The Google-login release is Worker version `6d0c1333-bd53-4018-a72b-5bd054ef90fb`, with Go image `sha256:2ffaf182c2e50976fa65f06f0c011712e1a3f2a95db141d6fbc88ce6b034d4a4`. Backend unit tests, Worker authentication tests, frontend build/lint/type checks, isolated browser checks and the deployment dry-run passed. The deployed readiness endpoint and authenticated API returned 200, and anonymous data/asset requests returned 401. No CRM documents were edited during the deployment or verification.

The later 9 October login-design release is Worker `8f2a48d1-8f9d-432d-bceb-587a1cade64f`. It adds a dedicated Google sign-in screen and native animated ASCII neighbourhood, with mobile and reduced-motion support. The [design and verification record](design/fieldwork-login.md) includes the imagegen concept and prompt. The deployed frontend matches the verified build, authenticated readiness/API checks pass, and anonymous/invalid requests remain denied. This deployment used `--containers-rollout=none`, preserving the existing backend image and all saved CRM data.

### Review-queue controls (9 October 2026)

The frontend in Worker `8f2a48d1` (bundle `index-CWTbGQVy.js`, shipped with the login-design release) was the first to carry the review-queue controls; the next frontend deploy carries the fixes listed here. They apply only to projects whose id ends in `-review` (any spelling); main projects such as `mooroolbark` keep the original outline bar, which acts only on an edited building outline.

- **Answer bar** under the picture names the outline it answers. Right (Enter), Partly or Not sure (Shift+Enter) and Wrong (X) write `status` + `geometry_reviewed` to that outline's Fieldwork overrides, save the item once and open the next item. Shadow, cut and join items are answered on the agent's proposal (PROPOSED / JOINED), which is selected when the item opens, also on a revisit; with CURRENT or a PIECE selected the bar answers nothing and offers to select the proposal. A moved outline must be put back or saved as an edit; a click on a point handle without a drag is not an edit.
- **Vegetation items** move outline by outline. Grass, Hedge, Shrub and Tree (keys 1-4) write the Label word the review pull reads; Right then saves it. On `tree or shrub?` and `type?` outlines Right stays off until a type is picked. Wrong and Not sure clear a picked type.
- **Queue**: `tag:<name>` in the search box (or a row's tag chip) filters by tag in the browser, because ListItems has no tag filter. Next loads the following page of the same search. Review projects show an **Unanswered** filter (item status still unreviewed), which Next follows.
- **Deep links**: `?project=&item=&q=&tag=`; the address bar keeps the open item. No credential is ever put in the URL.
- **MCP**: `save_agent_review` appends a separate agent assessment in review projects. Ordinary write tools remain refused by the main Worker's `/mcp` proxy, the hosted MCP gateway and `annotate mcp` (`deploy/annotation/src/mcp-guard.ts`, `cmd/annotate/reviewguard.go`). The Worker forwards a checked tools/call re-serialised, and refuses one that names its arguments more than once, because the Go backend merges repeated `arguments` keys.

### Independent agent and human review

Deployed on 9 October as Worker `a8321dde` and MCP gateway `6f8d2e5a`;
[release verification and saved assessments](annotation/agent-review-2026-10-09.md).

`save_agent_review` / `SaveAgentReview` requires `itemId`, the parent's current
`expectedRevision`, a stable `reviewer` (for example `codex`), `decision` and
evidence `notes`. An optional `annotationId` targets one outline. Decisions are
`approved`, `rejected`, `needs_adjustment` and `uncertain`; an approved vegetation
outline can also carry `vegetationType` (`grass`, `hedge`, `shrub`, `tree`).

The Go service stamps the actor `agent:fieldwork:<reviewer>` and appends immutable
`item.agentReviews`. Records retain the reviewed geometry, source and effective
classification, original revision, time and supersession history. The inspector
shows agent assessments separately from human review and identifies changed
evidence. Agent saves cannot change human status, notes, geometry checks or
training eligibility. Older human clients that omit the new field preserve its
history automatically; generic saves cannot forge or edit assessments.

`task fieldwork:review:pull` excludes agent events from human decisions and writes
`agent-reviews-{cursor}.json` separately. Agent approvals never generate human
answer lines, polygon preview requests or training samples. The export checks
each assessment against its recorded change event and identifies stale evidence.

Frontend-only redeploys keep the running container: use `npx wrangler deploy --config wrangler.pinned.jsonc --containers-rollout=none`, after checking that the digest in `wrangler.pinned.jsonc` matches `wrangler containers info` (independent review release: `sha256:b3ca27b3…`, running container version 9).

## Build and deploy

Run these commands from the repository root with Go 1.27, Node 22 or later, pnpm, a running Docker engine, and authenticated Cloudflare access. `wrangler whoami` verifies the account login. The deployment has its own locked npm package so it does not change the main application's dependency graph.

```sh
pnpm install
npm --prefix deploy/annotation ci
pnpm build:annotations
npm --prefix deploy/annotation run types
npm --prefix deploy/annotation run check
npm --prefix deploy/annotation run migrate
npm --prefix deploy/annotation run secrets
npm --prefix deploy/annotation run deploy
```

`migrate` applies the embedded versioned PostgreSQL schema with the direct connection. It can be rerun. Normal server startup verifies the schema but does not migrate it. Test future schema changes on an isolated Neon branch before applying them to the deployed workspace.

The Dockerfile builds only `cmd/annotation-server`, `internal/annotation`, and generated annotation protocol files. The root `.dockerignore` excludes credentials, local databases, datasets and build output, and the annotation Dockerfile copies only the required Go source. The root ignore file is needed because Wrangler supplies the Dockerfile on stdin. The final image runs as a non-root user and targets `linux/amd64`.

Cloudflare activates the Worker and rolls out the container separately. A first deployment can serve the frontend before the container is ready. Verify `/readyz` and an authenticated API request before considering the deployment complete:

```sh
curl --fail https://fieldwork-annotations.ben-ebsworth.workers.dev/readyz
go run ./cmd/annotate info \
  --url https://fieldwork-annotations.ben-ebsworth.workers.dev \
  --token-file deploy/annotation/admin-token.txt
```

The info response should report `storage: "postgres"` and `authRequired: true`. Unauthenticated data requests must return HTTP 401. After an idle period, the first API request can take longer while the container and database wake.

## Outline decisions and save-and-next (29 September 2026, built, not yet deployed)

The scene editor (`addr-scene` adapter) gained an explicit decision bar under the image canvas, a
save-and-next action in the inspector footer, and keyboard-editable outline points. Commands in this
document are unchanged; the SPA build for this change is `index-Bb9xPyZG.js` (SHA-256
`ac894366ff993ee1322d5f42bc7df9a0f19e518e3bc54c7afb6d6d60622ceba8`; it supersedes `index-GGXnysbz.js`,
whose needs-work button hint did not say that it advances). At the time of writing the live Worker still
serves `index-CHuhBMYV.js`; the deploy is the user's decision.

Wording and behaviour, in the reviewer's own terms (the reviewer's outline, the model estimate, and agreement between them):

- The bar appears in the Image view when a building is selected and the layer is editable. It reads
  `data-outline-pending="true"` as soon as the draft's ring or hole override for that object differs
  from the last saved copy of the item (`outlinePendingFor` in `src/lib/annotations/addr-scene.ts`);
  notes, class and status edits never make an outline pending.
- **Save outline · Still needs work** (Shift+Enter · then next item) writes `status: needs_adjustment`,
  `geometry_reviewed: false` into the object's Fieldwork overrides, saves the item and opens the next
  queue item.
- **Save outline · Looks right to me** (Enter · then next item) writes `status: confirmed`,
  `geometry_reviewed: true`, saves, and opens the next queue item. Both decisions advance (the same
  `decide()` path in `src/components/annotations/AddrSceneEditor.tsx`); on the last item there is no next
  item and the view stays. Each decision is one item revision.
- Caption: "Saved as your outline in Fieldwork. The local review imports it as a pending draft (needs
  adjustment) until a local reviewer decides; your choice stays in the receipt. Nothing here is a label."
- Enter and Shift+Enter act only when focus is on the page body or an outline point handle; the canvas,
  buttons, inputs and disclosure summaries keep their own Enter.
- **Save & next item** (inspector footer, `data-testid="fw-save-next"`) saves the current draft and
  opens the next item; it is disabled when nothing is unsaved, while a drawing is in progress, or on the
  last item. ⌘/Ctrl S saves without moving on.
- Outline points are focusable: a 22 px touch disc, a 7 px dot (10 px, inverted fill, when selected),
  arrow keys nudge 1 image pixel (Shift 5), Backspace or Delete removes the point while the ring keeps
  at least 3 points, and Escape during a drag abandons it without saving.

Policy, accepted and unchanged: the local import (`src/lib/annotations/feedback.ts`) lands every
substantive Fieldwork change as `needs_adjustment`, `geometry_reviewed: false`,
`model.height_verified: false` and `fieldwork_import.pending: true`, and `review_authority_blocker`
reports `fieldwork_pending`. A "Looks right to me" from Fieldwork therefore survives only in the item's
overrides and in the receipt fields (`status: candidate -> confirmed`), which the `task pull` preview
shows. A bearer token is not a human decision in the local review; only a local reviewer's click is.
`src/lib/annotations/feedback.test.mjs` covers this ("a Fieldwork decision-bar save imports as a pending
draft"); `src/lib/annotations/addr-scene.test.mjs` covers `outlinePendingFor` and `removeVertex` and now
runs in `task test:reviews`.

Not ported to Fieldwork: box-selecting roof lines (roof lines stay read-only in the Roof lines view) and
re-anchoring earlier roof-line edits onto an edited outline (`updateSceneObject` never rebinds
`roof_line_edits`; the local reviewer re-anchors in the scene editor).

### Local manual check before deploying

There is no headless harness for the SPA yet (`scripts/verify-fieldwork-edit-flow.mjs` is a follow-up).
Run the local server and the Vite dev server, then import a scratch snapshot with the CLI against the
local server only. Never point this at the deployed Mooroolbark project.

```sh
go run ./cmd/annotation-server --dev --sqlite /tmp/fieldwork-qa.db --listen 127.0.0.1:8095
pnpm dev:annotations
go run ./cmd/annotate import --url http://127.0.0.1:8095 --file <scratch-snapshot>.json
```

Checklist (addr-scene item, Image view, a building selected, Select tool):

1. Click an outline point: the dot grows and inverts its fill, and Tab/Shift+Tab move a visible focus ring
   between points.
2. ArrowRight moves the focused point by 1 image pixel; Shift+ArrowRight by 5; the point never leaves the
   image.
3. Backspace on a triangle's point does nothing; on a 4-point ring it removes the point and the ring
   stays closed.
4. Start a drag, press Escape while holding the pointer: the point returns to where it was and nothing is
   saved.
5. After one drag the decision bar shows `data-outline-pending="true"`; both buttons are enabled.
6. Click the canvas background so focus leaves the handles (or press Escape twice), then press Enter: the
   item saves once, the status bar reports the new revision, and the next item opens.
7. Return to the item: revision incremented exactly once; the object's Fieldwork overrides show the
   moved ring, `status: confirmed`, `geometry_reviewed: true`.
8. Shift+Enter on another pending outline stores `needs_adjustment` and stays on the item.
9. "Save & next item" is disabled when nothing is unsaved and enabled after any edit; ⌘/Ctrl S saves.
10. Undo (⌘/Ctrl Z) still steps back through drags, removals and decisions.

After the user deploys, verify as usual: `curl --fail .../readyz`, `go run ./cmd/annotate info ...`
(storage `postgres`, `authRequired` true), confirm the served `index.html` references the new bundle,
then run a `task pull` dry-run preview and confirm a decision-bar save imports as `needs_adjustment` with
`fieldwork_import.pending: true`.

## CLI and interchange

Build the Go CLI once with `go install ./cmd/annotate`, or run `go run ./cmd/annotate` directly. Configure `ANNOTATION_URL` for the deployed origin and `ANNOTATION_TOKEN` from your secret manager, or use `--url` and `--token-file` on each command.

```sh
annotate projects
annotate items --project PROJECT_ID
annotate get --id ITEM_ID
annotate save-item --file item.json --revision 3
annotate changes --after CURSOR
annotate export --project PROJECT_ID > project.json
annotate export --project PROJECT_ID --format ndjson > project.ndjson
annotate import --file project.json
annotate rpc SaveAnnotation --file annotation-request.json
```

Save files contain the document itself; `--revision 0` creates a new document and a positive revision performs an optimistic update. A conflict exits unsuccessfully and never retries an overwrite. List and change commands return one page, including `nextCursor`; continue with that cursor to consume the remaining pages.

JSON exports contain `schemaVersion`, `project`, `labels`, `items` and `cursor`. Both REST and CLI NDJSON exports contain `{kind, document}` lines in project, label and item order, followed by a `checkpoint` line containing `schemaVersion` and `cursor`. Import supports both formats, creates at revision zero, and stops if a document already exists. Imports are not atomic across documents: a failed import reports how many earlier creates remain saved.

`annotate seed` explicitly adds a small **Example — Fieldwork** project with synthetic map and image annotations. It preserves an existing example project. These records illustrate the adapters and are not address, roof or verified ML data.

## MCP

The authenticated Streamable HTTP endpoint is `/mcp`. MCP clients can connect to it directly with an `Authorization: Bearer …` header. The same workspace token is required for every data tool.

For a client that supports stdio, run `annotate mcp` with `ANNOTATION_URL` and `ANNOTATION_TOKEN` supplied by the client environment. Alternatively configure the command with `--url` and `--token-file`; keep the secret value out of the client configuration itself.

The CLI bridge forwards newline-delimited JSON-RPC messages, handles JSON and SSE HTTP responses, retains a server session header if present, and writes protocol messages only to stdout. It accepts encoded responses up to 128 MiB so export tools can return both text and structured data; input messages remain limited to 8 MiB. Diagnostics go to stderr. Browser, CLI and MCP writes use the same validation, revisions and change log.

## Operations

- `/healthz` checks the server; `/readyz` checks database availability.
- `npm --prefix deploy/annotation exec -- wrangler containers list` shows container deployment state.
- `npm --prefix deploy/annotation exec -- wrangler tail` streams Worker logs.
- To rotate the shared token, replace the local secret, run the secret upload command, and redeploy. Clients must reconnect with the new token.
- For code rollback, use Wrangler's deployment/version commands and verify the resulting container image rollout. Database migrations are managed separately and are not reverted by a Worker rollback.

Current platform references: [static frontend with a container backend](https://developers.cloudflare.com/containers/examples/container-backend/), [container secrets](https://developers.cloudflare.com/containers/examples/env-vars-and-secrets/), [container deployment](https://developers.cloudflare.com/containers/guides/deploy/), [Neon Go connections](https://neon.com/docs/guides/go), and [Neon connection pooling](https://neon.com/docs/connect/connection-pooling).
