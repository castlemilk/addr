/** Stable request receipts without credentials, account IDs, query strings,
 * record IDs, bodies or arbitrary exception text in operational logs. */
export async function withRequestDiagnostics(
  request: Request,
  run: (request: Request) => Promise<Response>,
): Promise<Response> {
  const requestId = crypto.randomUUID(),
    start = performance.now(),
    incoming = new Headers(request.headers);
  incoming.set("X-Fieldwork-Parent-Request-ID", requestId);
  const path = new URL(request.url).pathname;
  const route =
    path === "/mcp"
      ? "mcp"
      : path.startsWith("/connect/")
        ? "connection"
        : path.includes("authorize") || path.startsWith("/oauth/")
          ? "oauth"
          : path.startsWith("/api/v1/assets/")
            ? "asset"
            : path.startsWith("/api/") || path.startsWith("/annotation.v1.")
              ? "api"
              : "other";
  let response: Response;
  try {
    response = await run(new Request(request, { headers: incoming }));
  } catch {
    response = Response.json(
      {
        code: "unavailable",
        message: "Fieldwork could not complete this request. Retry shortly.",
      },
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }
  const headers = new Headers(response.headers);
  headers.set("X-Request-ID", requestId);
  const exposed = headers.get("Access-Control-Expose-Headers");
  headers.set(
    "Access-Control-Expose-Headers",
    exposed ? `${exposed}, X-Request-ID` : "X-Request-ID",
  );
  console.log(
    JSON.stringify({
      event: "fieldwork_http",
      requestId,
      route,
      method: request.method,
      status: response.status,
      elapsedMs: Math.round(performance.now() - start),
    }),
  );
  return new Response(response.body, { status: response.status, headers });
}
