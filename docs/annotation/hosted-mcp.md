# Hosted Addr CRM MCP

The extension gateway is deployed at
`https://addr-crm-mcp.ben-ebsworth.workers.dev/mcp`.
It uses `@openai/mcp-extensions@0.1.0`, the MCP SDK `1.31.0`, MCP Apps `1.7.5`
and Cloudflare Workers OAuth Provider `1.2.1`.

The existing editor and database remain behind
`https://fieldwork-annotations.ben-ebsworth.workers.dev`. The gateway calls that
Worker through the `FIELDWORK` service binding. It has no database, container,
R2 or training bindings. The original CRM deployment was not rebuilt or rolled
out. Its original `/mcp` remains the older Go endpoint; use the gateway URL for
the SDK-based extensions and OAuth.

## Connection

The private account plugin is
[Addr CRM](https://chatgpt.com/plugins/plugins_6ac47186d5888191b2b7571a10d98e9c).
Install/enable it and complete its OAuth connection to make it available to a
cloud dot. Desktop MCP registration alone does not install a cloud plugin.

The plugin is user scoped and private:

- Plugin ID: `plugins_6ac47186d5888191b2b7571a10d98e9c`
- Release ID: `pluginrel_6ac4756eb3648191af0158b6fdcf80db`
- Package source: `plugins/addr-crm/`
- Package version: `0.1.1`

This Mac has the global Codex MCP registration `addr-crm`, using the hosted URL.
OAuth login completed on 2026-10-06 with owner-confirmed editing access. The
connected MCP tools successfully read workspace capabilities, projects and the
Mooroolbark workspace. No CRM records were changed. A cloud Dot's connection
must be enabled separately; it has not been independently verified here.
Start or restart the local flow with:

```sh
codex mcp login addr-crm --scopes fieldwork:read,fieldwork:write
```

Account sign-in is live. Cloudflare Access protects the gateway's `/authorize`
path with the owner-only policy for `ben.ebsworth@gmail.com`, a restricted
Cloudflare account identity provider, and one-hour sessions. `ACCESS_AUD` is the
actual application's audience. The approved configuration is in
`deploy/annotation/access-plan.json`; persisted resources and validation are
recorded in `docs/annotation/hosted-access-2026-10-06.json`.

The owner-only configuration was explicitly approved on 2026-10-06. Cloudflare
management uses the scoped `addr-crm-access` profile in the official `cf` CLI;
Wrangler retains responsibility for this Worker's deployment. The existing
Zero Trust organization was reused. Its settings were not changed.

The form uses trusted `ctx.access` account identity and shows the
signed-in email, requesting client and callback host. It never accepts a
workspace token or a caller-supplied email/JWT header as consent identity.
Read access is selected; editing starts unchecked and must be selected for
saves/deletes. Without the configured Access identity, authorization fails
closed. The client receives OAuth tokens; the backend workspace credential
stays in the gateway's `ANNOTATION_TOKEN` Worker secret. No credentials are
packaged in the plugin, App HTML or repository source.

Metadata is at `/.well-known/oauth-authorization-server` and
`/.well-known/oauth-protected-resource/mcp`. Dynamic registration, client-ID
metadata documents, S256 PKCE, refresh and resource-bound tokens are supported.
Consent is browser bound, expires after ten minutes and is single use. Grants
identify the signed-in owner and Fieldwork workspace rather than an individual
CRM role. Rotating the upstream token
does not revoke existing OAuth grants: grant revocation is separate.

Consent uses `Referrer-Policy: same-origin`: browsers retain the real Origin on
the form POST and send no Referer to an external callback. Its CSP permits
form submission to self and redirects to only the provider-validated callback
origin, including a native client's exact loopback port. Opaque, wildcard and
IPv6 callback origins fail locally before consent is issued because the CSP
host-source restriction cannot safely represent them. Strict request Origin,
Access identity, cookie binding, redirect validation and PKCE checks remain.

Access tokens last one hour. OAuth token exchange also issues a rotating refresh
token; clients renew through `/oauth/token` without a browser Access session.
Refresh preserves the approved scopes, resource and owner identity and extends
the grant for another 30 days from use. Active connections can keep renewing;
30 days of inactivity requires a new sign-in. Runtime tests verify renewal
without Access context, read/write scope preservation and rejection of older
refresh tokens after the replacement has been used, including the provider's
one-generation overlap for retry recovery.

Existing workspace-bearer clients can also use the gateway directly. OAuth
read-only clients see only read tools; `fieldwork:write` exposes canonical saves
and deletes. No write tool is added merely to exercise authentication.

## Tools and UI

The gateway preserves the fifteen Go tool schemas, annotations,
unmodified arguments and success/error results. Saves still use the current
expected revision; annotation saves use the parent item's revision. Conflicts
are returned without an automatic retry. All domain validation and source
protection remain in the existing Go service.

The additional Go tool `save_agent_review` appends independent assessments,
including in `*-review` projects. It requires the current parent revision and
server-stamps the agent actor, evidence snapshot and unchecked provenance.
Human decisions remain independent. Ordinary MCP edits to review projects stay
blocked; read-only connections cannot call the new tool. The hosted catalogue
now exposes 18 tools to editing connections. See
[independent agent and human review](../annotation-deployment.md#independent-agent-and-human-review).

Three tools are added:

- `search_mentions`: real OpenAI extension registration for project/item links.
- `open_crm_workspace`: global/thread entrypoint for `ui://addr/crm-workspace`.
- `get_item_image`: current item-linked source/review image, native image content
  with revision/source binding; supported PNG/JPEG/WebP, verified digest and
  signature, at most 10 MiB, no arbitrary URL fetches or followed redirects.

Resources read `fieldwork://project/{id}` and `fieldwork://item/{id}`. The App is
self-contained and host proxied: projects, title search, saved object/status
details, annotation filtering, editor opening and chat attachments. Image reads
are available through the tool; the App currently displays annotation details
rather than an image canvas. Its editor link opens the existing editor, which
requires project selection and title search because it has no per-item URLs.

This is the hosted annotation API, not a port of the Mac's local tracing and
inference services. Local preview, geometry split, 3D and pipeline operations
are not advertised as available. The plugin carries the object classification
and fit guidance and explicitly checks capabilities before such operations.

## Maintain and verify

```sh
node apps/mcp-crm/build.mjs
node --test apps/mcp-crm/model.test.mjs
cd deploy/annotation
npm test
npm run typecheck
npm run check:mcp
npm run deploy:mcp
```

`wrangler.mcp.jsonc` deploys only the gateway. Its OAuth namespace is dedicated
to this service. Preserve that namespace and the existing Worker secret across
deployments. `worker-mcp-configuration.d.ts` is generated with `npm run types:mcp`.
The pinned local runtime supports compatibility date `2026-10-02`; the SDK and
image paths are verified without Node compatibility or dynamic evaluation.

From the repository root, the actual SDK production smoke test is read only:

```sh
node scripts/check-hosted-crm-mcp.mjs docs/annotation/hosted-mcp-smoke-2026-10-06.json
```

It reads the existing ignored credential file, checks discovery and exact legacy
schemas, calls mentions/resources/App/current item/image reads, verifies the
native image SHA-256 and compares the saved item before and after. It never
saves records, uploads assets or creates OAuth grants. The recorded check on
2026-10-06 passed, with the sample item's revision staying at 3.

The consent regression repairs passed all 46 Worker tests and a real browser
form submission completed the native Codex OAuth login. The production SDK
smoke passed again, with all 17 tools and the sample item's revision staying at 3.

The gateway deployment is `7e1a5441-8401-486a-b4ca-22d1575db4eb`. The existing CRM
version remains `65420db2-d760-4d6b-a930-49865dce4281`.

To update the private plugin, edit its source and workflow guidance, validate the
portable manifests, package the single `addr-crm` directory and use the plugin
update API with the existing plugin/release IDs. Do not create a duplicate.
Keep the canonical tracing/classification references in
`.opencode/skills/addr-roof-tracing/references/` aligned with the portable copies.
The hosted workflow reference documents the capabilities of this connection.

Product connection behavior is documented in the official
[Codex MCP guide](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) and
[dot apps guide](https://learn.chatgpt.com/docs/dots/computers-and-apps).
