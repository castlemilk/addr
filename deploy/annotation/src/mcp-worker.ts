import { handleFieldworkMcp } from "./mcp";
import {
  createFieldworkOAuth, FIELDWORK_ORIGIN, workspaceTokenMatches,
  type FieldworkOAuthHandler,
} from "./oauth";
import type { McpEnvironment } from "./mcp-env";

function unavailable(): Response {
  return Response.json({ message: "The CRM connection is temporarily unavailable." }, {
    status: 503, headers: { "Cache-Control": "no-store", "Retry-After": "5" },
  });
}

async function mcp(request: Request, env: McpEnvironment, ctx: ExecutionContext, scopes?: string[]): Promise<Response> {
  if (!env.ANNOTATION_TOKEN || !env.FIELDWORK) return unavailable();
  return handleFieldworkMcp(request, {
    ANNOTATION_BACKEND: { getByName: () => env.FIELDWORK },
    FIELDWORK_EDITOR_ORIGIN: env.FIELDWORK_EDITOR_ORIGIN,
  }, ctx, { upstreamAuthorization: `Bearer ${env.ANNOTATION_TOKEN}`, scopes });
}

const apiHandler: FieldworkOAuthHandler = {
  fetch(request, env, ctx) {
    return mcp(request, env, ctx, ctx.auth.scope);
  },
};
const fallback: ExportedHandler<McpEnvironment> = {
  fetch(request) {
    const path = new URL(request.url).pathname;
    if (path === "/healthz") return Response.json({ status: "ok", service: "addr-crm-mcp" });
    if (path === "/") return new Response("addr CRM MCP. Connect your client to /mcp using OAuth.", {
      headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" },
    });
    return new Response("Not found", { status: 404 });
  },
};
const oauth = createFieldworkOAuth(apiHandler, fallback);

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const origin = request.headers.get("Origin");
    if (origin && origin !== FIELDWORK_ORIGIN && origin !== env.FIELDWORK_EDITOR_ORIGIN) {
      return new Response("This origin is not permitted.", { status: 403 });
    }
    // Preserve existing workspace-token clients. Public plugin clients use
    // the OAuth provider and scoped grants below.
    try {
      if (url.pathname === "/mcp") {
        const authorization = request.headers.get("Authorization") ?? "";
        if (authorization.startsWith("Bearer ") && await workspaceTokenMatches(authorization.slice(7), env.ANNOTATION_TOKEN)) {
          return await mcp(request, env, ctx);
        }
      }
      return await oauth.fetch(request, env, ctx);
    } catch {
      // Never log credentials, OAuth requests, upstream response bodies or
      // individual record content in gateway errors.
      console.error("addr CRM gateway request failed.");
      return unavailable();
    }
  },
} satisfies ExportedHandler<McpEnvironment>;
