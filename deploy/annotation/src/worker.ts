import { Container } from "@cloudflare/containers";
import { serveAsset } from "./assets";
import { authenticateFirebaseRequest, firebaseAuthRoute, firebaseHelperRoute, isFirebaseHelperPath, verifiedIdentity } from "./firebase-auth";
import { signBackendRequest } from "./identity-bridge";
import { withRequestDiagnostics } from "./request-diagnostics";
import { backendRpc, guardMcpForward, reviewLookup } from "./mcp-guard";

type WorkerEnv = Env;

export class AnnotationBackend extends Container<WorkerEnv> {
  defaultPort = 8080;
  sleepAfter = "5m";
  enableInternet = true;
  envVars = {
    ANNOTATION_LISTEN: ":8080",
    DATABASE_URL: this.env.DATABASE_URL,
    ANNOTATION_TOKEN: this.env.ANNOTATION_TOKEN,
    ANNOTATION_ALLOWED_ORIGINS: this.env.ANNOTATION_ALLOWED_ORIGINS,
    FIELDWORK_SHARED_WORKSPACES:this.env.FIELDWORK_SHARED_WORKSPACES??"false",
    FIELDWORK_LEGACY_OWNER_EMAIL:this.env.FIELDWORK_LEGACY_OWNER_EMAIL??"",
    FIELDWORK_IDENTITY_BRIDGE_KEY:this.env.FIELDWORK_IDENTITY_BRIDGE_KEY??"",
  };
}

function isBackendPath(path: string): boolean {
  return (
    path.startsWith("/api/") ||
    path.startsWith("/annotation.v1.AnnotationService/") ||
    path === "/mcp" ||
    path.startsWith("/mcp/") ||
    path === "/healthz" ||
    path === "/readyz"
  );
}

const worker = {
  async fetch(request:Request, env) {
    const path = new URL(request.url).pathname;
    let forwarded: Request = request;
    if (path === "/api/auth/config" || path === "/api/auth/session") return firebaseAuthRoute(request, env);
    if (isFirebaseHelperPath(path)) return firebaseHelperRoute(request, env);
    if (isBackendPath(path) && path !== "/healthz" && path !== "/readyz") {
      const authenticated = await authenticateFirebaseRequest(request,env);
      if (authenticated instanceof Response) return authenticated;
      forwarded = authenticated;
    }
    if (!isBackendPath(path)) {
      return env.ASSETS.fetch(request);
    }

    if (!env.DATABASE_URL || !env.ANNOTATION_TOKEN) {
      return Response.json(
        { code: "unavailable", message: "Workspace configuration is incomplete." },
        { status: 503, headers: { "Cache-Control": "no-store" } },
      );
    }

    try {
      // One idle-suspending instance is sufficient for the initial workspace.
      // PostgreSQL holds all durable state; container disk is disposable.
      const backend = env.ANNOTATION_BACKEND.getByName("fieldwork");
      const identity=verifiedIdentity(forwarded);
      const scopedBackend={fetch:async(input:RequestInfo|URL,init?:RequestInit)=>{
        const actual=input instanceof Request && !init?input:new Request(input,init);
        return backend.fetch(identity?await signBackendRequest(actual,identity,env.FIELDWORK_IDENTITY_BRIDGE_KEY??""):actual);
      }};
      if(path==="/api/v1/assets" || path.startsWith("/api/v1/assets/")){
        if(!identity)return serveAsset(forwarded,env);
        if(request.method==="OPTIONS")return serveAsset(forwarded,env);
        const receipt=await scopedBackend.fetch(new Request(new URL("/api/v1/info",request.url)));
        if(!receipt.ok){await receipt.body?.cancel();return Response.json({code:"unavailable",message:"Your private asset workspace could not be opened."},{status:503});}
        const info=await receipt.json() as {workspace?:{id?:unknown}};
        if(typeof info.workspace?.id!=="string")throw new Error("Missing authenticated workspace receipt.");
        return serveAsset(forwarded,env,{workspaceId:info.workspace.id});
      }
      if (path === "/mcp" || path.startsWith("/mcp/")) {
        // Independent save_agent_review assessments are allowed in review
        // projects. Ordinary MCP edits remain refused (mcp-guard.ts).
        // A tools/call is forwarded exactly as checked (re-serialised). The
        // browser's REST and Connect saves are not inspected.
        const lookup = reviewLookup(backendRpc(scopedBackend, request.url, forwarded.headers.get("Authorization") ?? ""));
        const guarded = await guardMcpForward(forwarded, lookup);
        if (guarded instanceof Response) return guarded;
        forwarded = guarded;
      }
      return await scopedBackend.fetch(forwarded);
    } catch {
      console.error("Fieldwork container request failed.");
      return Response.json(
        { code: "unavailable", message: "The workspace is starting. Retry shortly." },
        {
          status: 503,
          headers: { "Cache-Control": "no-store", "Retry-After": "5" },
        },
      );
    }
  },
} satisfies ExportedHandler<WorkerEnv>;
export default {fetch(request,env,ctx){return withRequestDiagnostics(request,incoming=>worker.fetch(incoming,env));}} satisfies ExportedHandler<WorkerEnv>;
