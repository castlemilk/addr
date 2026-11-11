# Hosted Addr CRM MCP App

This app runs inside an MCP host. It reads the existing hosted annotation store through host-proxied MCP calls, with the connection's authorization. It never embeds a workspace token or requests protected source image bytes.

Build from the repository root:

```sh
node apps/mcp-crm/build.mjs
node --test apps/mcp-crm/model.test.mjs
pnpm exec tsc --noEmit --target ES2022 --module ESNext --moduleResolution bundler --lib ES2022,DOM,DOM.Iterable --skipLibCheck --strict apps/mcp-crm/main.ts apps/mcp-crm/model.ts
```

The build bundles the actual `@modelcontextprotocol/ext-apps` and `@openai/mcp-extensions/app` SDKs, including extension styles, into `deploy/annotation/src/mcp-app.ts`. The Worker serves `CRM_APP_HTML` as `text/html;profile=mcp-app` for `ui://addr/crm-workspace`. Rebuild after editing app sources.

`open_crm_workspace` launches with `{projectId?, itemId?}` and returns `{title, projects, nextCursor, selectedProjectId?, item?, labels?, labelsNextCursor?, editorUrl}`. The app supports paginated project search, title search, item selection, saved object classification/status and labels. Large annotation lists render in pages and can be filtered by object ID, class or status. It uses `list_projects`, `list_items`, `get_item` and `list_labels`. Edits remain in the canonical hosted editor, where the user picks the project and searches the item title; the deployed editor has no per-item URL.

Hosts that support model context can attach the selected item's canonical `fieldwork://item/{id}` resource reference to chat. OpenAI model context support is checked after `App.connect()` and uses the real `OpenAIExtensions` wrapper. Hosts with standard `updateModelContext.resourceLink` support use that API instead. Theme and host CSS variables update through the MCP Apps SDK. Incoming canonical project/item deep links are accepted when supplied by the host.

For a browser handshake smoke test, run `node apps/mcp-crm/test-server.mjs` and open `http://127.0.0.1:3018`. This labeled synthetic host serves the generated app, responds to read tools with fixture records, and logs protocol calls. It supports theme changes, a failed item read, model context attachments and external editor opening. It never connects to the production CRM.
