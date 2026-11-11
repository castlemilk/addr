# Address reviews MCP App

`pnpm build:mcp` bundles the official MCP Apps and OpenAI extension SDKs,
scripts and styles into `scripts/ml/mcp_app.html`. The Python server serves
this committed HTML as `ui://addr/review-workspace`; rebuild it after editing
the app source. No runtime CDN or external network request is required.

The host's initial `open_review_workspace` tool result supplies `{items}`
resource links. Search calls `app_search_resources({query})`; detail selection
reads only allowlisted `addr://addresses/{id}` and `addr://scenes/{id}` resources.
Selecting a scene lists its building objects. **Inspect roof evidence** calls
`scene_roof_review` with saved feedback enabled, then displays the native image
card, separately labelled cached eyes diagnostics, reviewer feedback revision
and source-bound tier-3 agent observations. The queue button calls
`low_confidence_roofs` and opens an eligible scene/object pair. No inference is
requested by this workflow.

The observation form calls `record_roof_observation` only after an explicit
save; the backend verifies its immutable review ID and source binding. A saved
note never edits geometry or grants approval. Scene-editor links use the
server-provided `editor_url`, through the host's open-link capability.
Only the explicit **Add to chat** button updates model context.

The app accepts `openai/deepLink` paths `/addresses/{id}` and
`/scenes/{id}?object={object_id}` and
applies theme/style variables from the host. Composer updates use the OpenAI
extension when advertised, fall back to standard MCP Apps capability support,
and are unavailable when the host advertises neither.

For local protocol verification, serve the repository with a static web server
and open `/apps/mcp-review/test-host.html`. This development fixture simulates
the MCP host without reading the production address dataset or writing reviews.
Use `?capabilities=standard`, `?capabilities=none` or `?deeplink=1` to exercise
capability fallback and initial deep links. The protocol log should show one
`ui/initialize` request before any user action, no automatic composer update,
and resource reads/tool searches only when requested by the user or deep link.

The host's scene/roof cards and observations are synthetic and use no production
review data. Exercise scene → roof evidence → observation save, then select a
different building and verify the notes remain bound to their source. Use
`?legacy=1` to test text-only tool metadata, `?fail-note=1` for a source-conflict
error retaining the form draft, and `?delay-roof=1` for response races when
changing building selection. The protocol log records every request, including
the saved observation and source-editor link, without opening the external URL.
