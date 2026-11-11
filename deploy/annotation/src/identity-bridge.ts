import type { ServiceIdentity } from "./firebase-auth";

const encode = (data: Uint8Array) =>
  btoa(String.fromCharCode(...data))
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/g, "");
const hex = (data: ArrayBuffer) =>
  Array.from(new Uint8Array(data), (b) => b.toString(16).padStart(2, "0")).join(
    "",
  );

/** Sign only after final MCP guard serialization, and separately for each
 * internal RPC. Incoming identity headers and cookies are always overwritten. */
export async function signBackendRequest(
  request: Request,
  identity: ServiceIdentity,
  key: string,
): Promise<Request> {
  if (key.length < 32) throw new Error("Identity bridge is not configured.");
  const body = await request.clone().arrayBuffer();
  if (body.byteLength > 4 * 1024 * 1024)
    throw new Error("Backend request is too large.");
  const url = new URL(request.url),
    iat = Math.floor(Date.now() / 1000),
    encoder = new TextEncoder();
  const claims = {
    ...identity,
    method: request.method,
    uri: url.pathname + url.search,
    bodySha256: hex(await crypto.subtle.digest("SHA-256", body)),
    iat,
    exp: iat + 60,
  };
  const payload = encode(encoder.encode(JSON.stringify(claims)));
  const signingKey = await crypto.subtle.importKey(
    "raw",
    encoder.encode(key),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = encode(
    new Uint8Array(
      await crypto.subtle.sign("HMAC", signingKey, encoder.encode(payload)),
    ),
  );
  const headers = new Headers(request.headers);
  headers.delete("Cookie");
  headers.delete("Authorization");
  headers.set("X-Fieldwork-Identity", `${payload}.${signature}`);
  return new Request(request, { headers });
}
