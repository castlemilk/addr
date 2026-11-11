import { Container } from "@cloudflare/containers";
import { serveAsset } from "./assets";
import { authenticateFirebaseRequest, firebaseAuthRoute, firebaseHelperRoute, isFirebaseHelperPath } from "./firebase-auth";
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

export default {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    let forwarded: Request = request;
    if (path === "/api/auth/config" || path === "/api/auth/session") return firebaseAuthRoute(request, env);
    if (isFirebaseHelperPath(path)) return firebaseHelperRoute(request, env);
    if (isBackendPath(path) && path !== "/healthz" && path !== "/readyz") {
      const authenticated = await authenticateFirebaseRequest(request,env);
      if (authenticated instanceof Response) return authenticated;
      forwarded = authenticated;
    }
    if (path === "/api/v1/assets" || path.startsWith("/api/v1/assets/")) {
      return serveAsset(forwarded, env);
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
      if (path === "/mcp" || path.startsWith("/mcp/")) {
        // Independent save_agent_review assessments are allowed in review
        // projects. Ordinary MCP edits remain refused (mcp-guard.ts).
        // A tools/call is forwarded exactly as checked (re-serialised). The
        // browser's REST and Connect saves are not inspected.
        const lookup = reviewLookup(backendRpc(backend, request.url, forwarded.headers.get("Authorization") ?? ""));
        const guarded = await guardMcpForward(forwarded, lookup);
        if (guarded instanceof Response) return guarded;
        forwarded = guarded;
      }
      return await backend.fetch(forwarded);
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
