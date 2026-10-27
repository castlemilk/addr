import { handleFieldworkMcp } from "./mcp";
import {
  createFieldworkOAuth, FIELDWORK_ORIGIN, workspaceTokenMatches,
  type FieldworkOAuthHandler,
} from "./oauth";
import type { McpEnvironment } from "./mcp-env";
import type { AccountSession } from "./shared-firebase";
import { withRequestDiagnostics } from "./request-diagnostics";

function unavailable(): Response {
  return Response.json({ message: "The CRM connection is temporarily unavailable." }, {
    status: 503, headers: { "Cache-Control": "no-store", "Retry-After": "5" },
  });
}

async function mcp(request: Request, env: McpEnvironment, ctx: ExecutionContext, scopes?: string[],session?:AccountSession): Promise<Response> {
  if (!env.ANNOTATION_TOKEN || !env.FIELDWORK) return unavailable();
  return handleFieldworkMcp(request, {
    ANNOTATION_BACKEND: { getByName: () => env.FIELDWORK },
    FIELDWORK_EDITOR_ORIGIN: env.FIELDWORK_EDITOR_ORIGIN,
  }, ctx, { upstreamAuthorization: `Bearer ${session?session.idToken:env.ANNOTATION_TOKEN}`, scopes });
}

const apiHandler: FieldworkOAuthHandler = {
  fetch(request, env, ctx) {
    const props=ctx.props;
    if(props && "uid" in props){
      if(env.FIELDWORK_SHARED_WORKSPACES!=="true" || props.uid!==ctx.auth.userId)return unavailable();
      return mcp(request,env,ctx,ctx.auth.scope,props);
    }
    if(props && "workspace" in props && props.workspace==="fieldwork" && props.userId===ctx.auth.userId)return mcp(request, env, ctx, ctx.auth.scope);
    return unavailable();
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
const sharedOAuth=createFieldworkOAuth(apiHandler,fallback,true);

const worker = {
  async fetch(request:Request, env, ctx) {
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
      if(env.FIELDWORK_SHARED_WORKSPACES==="true"){
        if(!env.FIREBASE_PROJECT_ID || !env.FIREBASE_API_KEY)return unavailable();
        return await sharedOAuth.fetch(request,{...env,MCP_ORIGIN:FIELDWORK_ORIGIN,APPLICATION_ORIGIN:env.FIELDWORK_EDITOR_ORIGIN},ctx);
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
export default {fetch(request,env,ctx){return withRequestDiagnostics(request,incoming=>worker.fetch(incoming,env,ctx));}} satisfies ExportedHandler<McpEnvironment>;
