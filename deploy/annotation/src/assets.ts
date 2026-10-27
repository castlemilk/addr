const MAX_ASSET_BYTES = 64 * 1024 * 1024;
const assetPrefix = "/api/v1/assets/";

type AssetEnv = Pick<Env, "ANNOTATION_BLOBS" | "ANNOTATION_TOKEN" | "ANNOTATION_ALLOWED_ORIGINS">;

const inlineTypes = new Set([
  "image/png", "image/jpeg", "image/webp", "image/avif", "image/gif",
  "application/json", "application/x-ndjson", "text/plain",
]);
const downloadTypes = new Set([
  "application/octet-stream", "application/gzip", "application/zip",
  "application/vnd.sqlite3", "application/pdf",
]);

function safeType(value: string | null | undefined): string {
  const type = value?.split(";", 1)[0].trim().toLowerCase() ?? "";
  return inlineTypes.has(type) || downloadTypes.has(type) ? type : "application/octet-stream";
}

function digestBytes(hex: string): Uint8Array {
  return Uint8Array.from(hex.match(/../g) ?? [], (byte) => parseInt(byte, 16));
}

function digestHex(bytes: ArrayBuffer): string {
  return Array.from(new Uint8Array(bytes), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function isVerified(object: R2Object, digest: string): boolean {
  return object.checksums.sha256 !== undefined && digestHex(object.checksums.sha256) === digest;
}

function baseHeaders(): Headers {
  return new Headers({
    "Cache-Control": "private, no-store, no-transform",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "Referrer-Policy": "no-referrer",
  });
}

function failure(headers: Headers, status: number, code: string, message: string): Response {
  headers.set("Content-Type", "application/json");
  headers.delete("Content-Disposition");
  return Response.json({ code, message }, { status, headers });
}

function originAllowed(origin: string, requestURL: URL, configured: string): boolean {
  try {
    const parsed = new URL(origin);
    if (parsed.origin !== origin || !["http:", "https:"].includes(parsed.protocol)) return false;
    return origin === requestURL.origin || configured.split(",").map((value) => value.trim()).includes(origin);
  } catch {
    return false;
  }
}

async function authenticated(request: Request, token: string): Promise<boolean> {
  const header = request.headers.get("Authorization") ?? "";
  if (!header.startsWith("Bearer ") || header.length > 4096) return false;
  const encoder = new TextEncoder();
  const [expected, supplied] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(token)),
    crypto.subtle.digest("SHA-256", encoder.encode(header.slice(7))),
  ]);
  return crypto.subtle.timingSafeEqual(expected, supplied);
}

function assetHeaders(headers: Headers, object: R2Object, digest: string): void {
  const contentType = safeType(object.httpMetadata?.contentType);
  headers.set("Content-Type", contentType);
  headers.set("Content-Encoding", "identity");
  headers.set("Content-Length", String(object.size));
  headers.set("X-Content-SHA256", digest);
  headers.set("ETag", `"sha256-${digest}"`);
  headers.set("Accept-Ranges", "bytes");
  if (!inlineTypes.has(contentType)) headers.set("Content-Disposition", `attachment; filename="${digest}"`);
}

function uploadedResponse(headers: Headers, object: R2Object, digest: string, created: boolean): Response {
  headers.set("X-Content-SHA256", digest);
  return Response.json({
    url: `${assetPrefix}${digest}`,
    sha256: digest,
    bytes: object.size,
    contentType: safeType(object.httpMetadata?.contentType),
  }, { status: created ? 201 : 200, headers });
}

function parseRange(value: string, size: number): { offset: number; length: number } | null {
  const match = /^bytes=(\d*)-(\d*)$/.exec(value);
  if (!match || (!match[1] && !match[2]) || size === 0) return null;
  if (!match[1]) {
    const suffix = Number(match[2]);
    if (!Number.isSafeInteger(suffix) || suffix <= 0) return null;
    const length = Math.min(suffix, size);
    return { offset: size - length, length };
  }
  const offset = Number(match[1]);
  const end = match[2] ? Number(match[2]) : size - 1;
  if (!Number.isSafeInteger(offset) || !Number.isSafeInteger(end) || offset >= size || end < offset) return null;
  return { offset, length: Math.min(end, size - 1) - offset + 1 };
}

/** Private immutable blobs. Annotation documents retain the digest and source provenance. */
export async function serveAsset(request: Request, env: AssetEnv, verifiedScope?:{workspaceId:string}): Promise<Response> {
  const headers = baseHeaders();
  const url = new URL(request.url);
  const origin = request.headers.get("Origin");
  if (origin) {
    if (!originAllowed(origin, url, env.ANNOTATION_ALLOWED_ORIGINS ?? "")) {
      return failure(headers, 403, "permission_denied", "Origin is not allowed.");
    }
    headers.set("Access-Control-Allow-Origin", origin);
    headers.set("Vary", "Origin");
    headers.set("Access-Control-Allow-Methods", "GET, HEAD, PUT, OPTIONS");
    headers.set("Access-Control-Allow-Headers", "Authorization, Content-Type, Content-Encoding, X-Uncompressed-Length, Range, If-Range, If-None-Match");
    headers.set("Access-Control-Expose-Headers", "Content-Length, Content-Range, X-Content-SHA256, ETag, Accept-Encoding");
  }
  if (!env.ANNOTATION_BLOBS || !env.ANNOTATION_TOKEN || env.ANNOTATION_TOKEN.length < 24 || /\s/.test(env.ANNOTATION_TOKEN)) {
    return failure(headers, 503, "unavailable", "Asset storage is not configured.");
  }
  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers });
  if (verifiedScope && verifiedScope.workspaceId!=="legacy" && !/^ws_[a-f0-9]{32}$/.test(verifiedScope.workspaceId)) return failure(headers,503,"unavailable","The private asset workspace could not be resolved.");
  if (!verifiedScope && !(await authenticated(request, env.ANNOTATION_TOKEN))) {
    headers.set("WWW-Authenticate", 'Bearer realm="annotation"');
    return failure(headers, 401, "unauthenticated", "A valid annotation bearer token is required.");
  }
  // This advertises accepted request encodings, including on a missing HEAD.
  headers.set("Accept-Encoding", "gzip");
  const digest = url.pathname.slice(assetPrefix.length);
  if (!url.pathname.startsWith(assetPrefix) || !/^[a-f0-9]{64}$/.test(digest) || url.search) {
    return failure(headers, 400, "invalid_argument", "Use an asset SHA-256 path without query parameters.");
  }
  if (!["GET", "HEAD", "PUT"].includes(request.method)) {
    headers.set("Allow", "GET, HEAD, PUT, OPTIONS");
    return failure(headers, 405, "invalid_argument", "Asset method is not supported.");
  }
  // Scope comes only from the authenticated backend's workspace receipt, never
  // from URL parameters, headers, document metadata or a supplied digest.
  const prefix=verifiedScope && verifiedScope.workspaceId!=="legacy" ? `workspaces/${verifiedScope.workspaceId}/` : "";
  const key = `${prefix}sha256/${digest}`;
  try {
    if (request.method === "PUT") {
      const rawLength = request.headers.get("Content-Length");
      if (rawLength === null || !/^\d+$/.test(rawLength)) {
        return failure(headers, 411, "invalid_argument", "Content-Length is required for asset uploads.");
      }
      const wireLength = Number(rawLength);
      if (!Number.isSafeInteger(wireLength) || wireLength > MAX_ASSET_BYTES) {
        return failure(headers, 413, "resource_exhausted", "Assets may be at most 64 MiB; split larger snapshot files into chunks.");
      }
      const encoding = request.headers.get("Content-Encoding")?.trim().toLowerCase() ?? "identity";
      if (encoding !== "identity" && encoding !== "gzip") {
        return failure(headers, 415, "invalid_argument", "Asset uploads support identity or gzip Content-Encoding.");
      }
      let length = wireLength;
      if (encoding === "gzip") {
        if (wireLength === 0) return failure(headers, 400, "invalid_argument", "Gzip uploads require a valid encoded body.");
        const originalLength = request.headers.get("X-Uncompressed-Length");
        if (originalLength === null || !/^\d+$/.test(originalLength)) {
          return failure(headers, 400, "invalid_argument", "Gzip uploads require X-Uncompressed-Length.");
        }
        length = Number(originalLength);
        if (!Number.isSafeInteger(length) || length > MAX_ASSET_BYTES) {
          return failure(headers, 413, "resource_exhausted", "Decoded assets may be at most 64 MiB.");
        }
      }
      if (wireLength > 0 && request.body === null) {
        return failure(headers, 400, "invalid_argument", "Asset body does not match Content-Length.");
      }
      const existing = await env.ANNOTATION_BLOBS.head(key);
      if (existing) {
        if (!isVerified(existing, digest) || existing.size !== length) {
          return failure(headers, 409, "already_exists", "An incompatible object already exists at this digest.");
        }
        // A verified object already exists. Do not rewrite its bytes or metadata.
        return uploadedResponse(headers, existing, digest, false);
      }
      // Workers cannot directly chain two native identity transforms. Gzip's
      // decompressor separates wire-length and decoded-length enforcement.
      const wireBody = encoding === "gzip" ? request.body?.pipeThrough(new FixedLengthStream(wireLength)) : request.body;
      const decoded = encoding === "gzip" ? wireBody?.pipeThrough(new DecompressionStream("gzip")) : wireBody;
      const body = decoded?.pipeThrough(new FixedLengthStream(length)) ?? new Uint8Array(0);
      const object = await env.ANNOTATION_BLOBS.put(key, body, {
        sha256: digestBytes(digest),
        onlyIf: new Headers({ "If-None-Match": "*" }),
        httpMetadata: { contentType: safeType(request.headers.get("Content-Type")) },
      });
      if (object) return uploadedResponse(headers, object, digest, true);
      // A concurrent upload won the conditional create. Confirm identical content.
      const winner = await env.ANNOTATION_BLOBS.head(key);
      if (!winner || !isVerified(winner, digest) || winner.size !== length) {
        return failure(headers, 409, "already_exists", "Concurrent asset upload could not be verified.");
      }
      return uploadedResponse(headers, winner, digest, false);
    }

    const metadata = await env.ANNOTATION_BLOBS.head(key);
    if (!metadata) return failure(headers, 404, "not_found", "Asset does not exist.");
    if (!isVerified(metadata, digest)) return failure(headers, 409, "data_loss", "Stored asset checksum could not be verified.");
    assetHeaders(headers, metadata, digest);
    if (request.method === "HEAD") return new Response(null, { headers });
    if (request.headers.get("If-None-Match") === headers.get("ETag")) {
      headers.delete("Content-Length");
      return new Response(null, { status: 304, headers });
    }
    let range: { offset: number; length: number } | undefined;
    const requestedRange = request.headers.get("Range");
    const ifRange = request.headers.get("If-Range");
    if (requestedRange && (!ifRange || ifRange === headers.get("ETag"))) {
      const parsed = parseRange(requestedRange, metadata.size);
      if (!parsed) {
        headers.set("Content-Range", `bytes */${metadata.size}`);
        headers.delete("Content-Length");
        return failure(headers, 416, "invalid_argument", "Requested byte range is not satisfiable.");
      }
      range = parsed;
      headers.set("Content-Length", String(range.length));
      headers.set("Content-Range", `bytes ${range.offset}-${range.offset + range.length - 1}/${metadata.size}`);
    }
    const object = await env.ANNOTATION_BLOBS.get(key, range ? { range } : undefined);
    if (!object) {
      headers.delete("Content-Length");
      return failure(headers, 404, "not_found", "Asset does not exist.");
    }
    // Workers derives Content-Length from the stream; a header alone is ignored.
    const body = object.body.pipeThrough(new FixedLengthStream(range?.length ?? metadata.size));
    return new Response(body, { status: range ? 206 : 200, headers });
  } catch (error) {
    headers.delete("Content-Length");
    headers.delete("Content-Range");
    // Provider diagnostics may contain internal request details; return only a category.
    if (request.method === "PUT" && error instanceof Error && /checksum|digest|sha-?256|content.?length|fixedlength|gzip|decompress|compress|too (few|many) bytes/i.test(error.message)) {
      return failure(headers, 400, "invalid_argument", "Asset checksum or byte length does not match the upload.");
    }
    console.error("Fieldwork asset storage request failed.");
    return failure(headers, 503, "unavailable", "Asset storage is temporarily unavailable.");
  }
}
