import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { gzipSync } from "node:zlib";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";
import ts from "typescript";

const token = "local-test-token-with-at-least-24-characters";
const origin = "https://fieldwork.example";
const sha256 = (value) => createHash("sha256").update(value).digest("hex");
const code = ts.transpileModule(await readFile(new URL("./assets.ts", import.meta.url), "utf8"), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText;

test("private immutable asset storage in the Workers runtime", async (t) => {
  const mf = new Miniflare(convertV4MiniflareOptions({
    modules: true,
    script: `${code.replace('console.error("Fieldwork asset storage request failed.");', 'console.error("Local fixture asset error:", error instanceof Error ? error.message : "unknown");')}\nexport default { fetch(request, env) {
 const scopes={"Bearer fixture-a":{workspaceId:"ws_"+"a".repeat(32)},"Bearer fixture-b":{workspaceId:"ws_"+"b".repeat(32)}};
 return serveAsset(request,env,scopes[request.headers.get("Authorization")]);
 } };`,
    compatibilityDate: "2026-09-25",
    r2Buckets: ["ANNOTATION_BLOBS"],
    bindings: { ANNOTATION_TOKEN: token, ANNOTATION_ALLOWED_ORIGINS: origin },
  }));
  t.after(() => mf.dispose());

  const request = (digest, options = {}) => mf.dispatchFetch(`https://fieldwork.example/api/v1/assets/${digest}`, {
    ...options,
    headers: { Authorization: `Bearer ${token}`, ...options.headers },
  });
  const put = (value, options = {}) => request(sha256(value), {
    method: "PUT", body: value,
    ...options,
    headers: { "Content-Length": String(Buffer.byteLength(value)), "Content-Type": "application/json", ...options.headers },
  });
  const putGzip = (value, options = {}) => {
    const body = options.encoded ?? gzipSync(value, { level: 1 });
    return request(options.digest ?? sha256(value), {
      method: "PUT", body,
      headers: {
        "Content-Length": String(body.length), "Content-Type": "application/json",
        "Content-Encoding": "gzip", "X-Uncompressed-Length": String(value.length),
        ...options.headers,
      },
    });
  };

  await t.test("rejects missing tokens, hostile origins, and query credentials", async () => {
    assert.equal((await request("0".repeat(64), { headers: { Authorization: "" } })).status, 401);
    assert.equal((await request("0".repeat(64), { headers: { Authorization: "" } })).headers.get("Accept-Encoding"), null);
    assert.equal((await request("0".repeat(64), { headers: { Origin: "https://hostile.example" } })).status, 403);
    assert.equal((await request(`${"0".repeat(64)}?token=redacted`)).status, 400);
    const preflight = await request("0".repeat(64), { method: "OPTIONS", headers: { Origin: origin, Authorization: "" } });
    assert.equal(preflight.status, 204);
    assert.equal(preflight.headers.get("Access-Control-Allow-Origin"), origin);
  });

  await t.test("advertises gzip only after authentication and stores original bytes", async () => {
    const bytes = Buffer.from("gzip source evidence\n".repeat(10000));
    const hash = sha256(bytes);
    const missing = await request(hash, { method: "HEAD" });
    assert.equal(missing.status, 404);
    assert.equal(missing.headers.get("Accept-Encoding"), "gzip");
    const uploaded = await putGzip(bytes);
    assert.equal(uploaded.status, 201, await uploaded.clone().text());
    assert.equal((await uploaded.json()).bytes, bytes.length);
    const downloaded = await request(hash);
    assert.equal(downloaded.status, 200, await downloaded.clone().text());
    assert.notEqual(downloaded.headers.get("Content-Encoding"), "gzip");
    assert.equal(downloaded.headers.get("Content-Length"), String(bytes.length));
    assert.deepEqual(Buffer.from(await downloaded.arrayBuffer()), bytes);
    assert.equal((await put(bytes)).status, 200);
    assert.equal((await putGzip(bytes)).status, 200);
  });

  await t.test("rejects malformed gzip, wrong decoded lengths and wrong digests", async () => {
    const cases = [
      () => ({ encoded: Buffer.from("not gzip") }),
      (value) => ({ encoded: gzipSync(value).subarray(0, -5) }),
      (value) => { const encoded = gzipSync(value); encoded[encoded.length - 8] ^= 1; return { encoded }; },
      (value) => ({ encoded: Buffer.concat([gzipSync(value), Buffer.from("trailing")]) }),
      (value) => ({ headers: { "X-Uncompressed-Length": String(value.length - 1) } }),
      (value) => ({ headers: { "X-Uncompressed-Length": String(value.length + 1) } }),
      () => ({ headers: { "X-Uncompressed-Length": "" } }),
      () => ({ digest: "2".repeat(64) }),
    ];
    for (const [index, optionsFor] of cases.entries()) {
      const original = Buffer.from(`compressed evidence ${index}\n`.repeat(200));
      const options = optionsFor(original);
      const result = await putGzip(original, options);
      assert.equal(result.status, 400, await result.clone().text());
      const downloaded = await request(options.digest ?? sha256(original));
      // R2 can commit verified bytes before a later gzip trailer error arrives.
      // An ambiguous failure may only leave the exact expected original object.
      if (downloaded.status === 200) {
        assert.equal(downloaded.headers.get("X-Content-SHA256"), sha256(original));
        assert.deepEqual(Buffer.from(await downloaded.arrayBuffer()), original);
      } else {
        assert.equal(downloaded.status, 404);
      }
    }
    const original = Buffer.from("oversized declared gzip");
    const oversized = await putGzip(original, { headers: { "X-Uncompressed-Length": String(64 * 1024 * 1024 + 1) } });
    assert.equal(oversized.status, 413);
  });

  await t.test("handles empty and maximum decoded gzip assets", async () => {
    for (const bytes of [Buffer.alloc(0), Buffer.alloc(64 * 1024 * 1024, 9)]) {
      const result = await putGzip(bytes);
      assert.equal(result.status, 201, await result.clone().text());
      const head = await request(sha256(bytes), { method: "HEAD" });
      assert.equal(head.status, 200);
      assert.equal(head.headers.get("Content-Length"), String(bytes.length));
      assert.equal(head.headers.get("X-Content-SHA256"), sha256(bytes));
    }
  });

  await t.test("verifies uploaded bytes and preserves exact downloads", async () => {
    const bytes = Buffer.from('{"scene":"Mooroolbark","units":"metres"}\n');
    const digest = sha256(bytes);
    const uploaded = await put(bytes);
    assert.equal(uploaded.status, 201, await uploaded.clone().text());
    assert.deepEqual(await uploaded.json(), { url: `/api/v1/assets/${digest}`, sha256: digest, bytes: bytes.length, contentType: "application/json" });
    const downloaded = await request(digest);
    assert.equal(downloaded.status, 200);
    assert.equal(downloaded.headers.get("X-Content-SHA256"), digest);
    assert.equal(downloaded.headers.get("Content-Length"), String(bytes.length), JSON.stringify([...downloaded.headers]));
    assert.equal(downloaded.headers.get("X-Content-Type-Options"), "nosniff");
    assert.match(downloaded.headers.get("Cache-Control"), /no-store/);
    assert.deepEqual(Buffer.from(await downloaded.arrayBuffer()), bytes);
    const head = await request(digest, { method: "HEAD" });
    assert.equal(head.status, 200);
    assert.equal(head.headers.get("Content-Length"), String(bytes.length));
    assert.equal(await head.text(), "");
  });

  await t.test("rejects digest mismatches without creating the object", async () => {
    const response = await request("0".repeat(64), { method: "PUT", body: "bad", headers: { "Content-Length": "3" } });
    assert.equal(response.status, 400, await response.clone().text());
    assert.equal((await request("0".repeat(64))).status, 404);
  });

  await t.test("resumes uploads and handles racing immutable creates", async () => {
    const bytes = Buffer.from("identical immutable bytes");
    const responses = await Promise.all([put(bytes), put(bytes)]);
    assert.deepEqual(responses.map((response) => response.status).sort(), [200, 201]);
    const repeated = await put(bytes, { headers: { "Content-Type": "text/html" } });
    assert.equal(repeated.status, 200);
    assert.equal((await repeated.json()).contentType, "application/json");
  });

  await t.test("supports byte ranges and immutable ETags", async () => {
    const bytes = Buffer.from("0123456789");
    await put(bytes);
    const digest = sha256(bytes);
    const part = await request(digest, { headers: { Range: "bytes=2-5" } });
    assert.equal(part.status, 206);
    assert.equal(part.headers.get("Content-Range"), "bytes 2-5/10");
    assert.equal(await part.text(), "2345");
    const suffix = await request(digest, { headers: { Range: "bytes=-2" } });
    assert.equal(suffix.status, 206);
    assert.equal(await suffix.text(), "89");
    const invalid = await request(digest, { headers: { Range: "bytes=20-30" } });
    assert.equal(invalid.status, 416);
    assert.equal(invalid.headers.get("Content-Range"), "bytes */10");
    const unchanged = await request(digest, { headers: { "If-None-Match": `"sha256-${digest}"` } });
    assert.equal(unchanged.status, 304);
  });

  await t.test("serves active content as an inert attachment", async () => {
    const bytes = Buffer.from("<script>alert('untrusted')</script>");
    await put(bytes, { headers: { "Content-Type": "text/html" } });
    const response = await request(sha256(bytes));
    assert.equal(response.headers.get("Content-Type"), "application/octet-stream");
    assert.match(response.headers.get("Content-Disposition"), /^attachment;/);
    assert.match(response.headers.get("Content-Security-Policy"), /sandbox/);
  });

  await t.test("private user assets, existence checks and writes do not cross workspace prefixes", async()=>{
    const bytes=Buffer.from("private new user asset"),digest=sha256(bytes);
    const asUser=(identity,options={})=>request(digest,{...options,headers:{Authorization:`Bearer fixture-${identity}`,...options.headers}});
    assert.equal((await asUser("a",{method:"PUT",body:bytes,headers:{"Content-Length":String(bytes.length)}})).status,201);
    assert.equal((await asUser("b")).status,404);
    assert.equal((await asUser("b",{method:"HEAD",headers:{"X-Fieldwork-Workspace":"ws_"+"a".repeat(32)}})).status,404);
    assert.equal((await request(digest)).status,404);
    assert.equal((await asUser("b",{method:"PUT",body:bytes,headers:{"Content-Length":String(bytes.length)}})).status,201);
    assert.deepEqual(Buffer.from(await (await asUser("a")).arrayBuffer()),bytes);
    assert.deepEqual(Buffer.from(await (await asUser("b")).arrayBuffer()),bytes);
  });

  await t.test("rejects oversized files before reading their body", async () => {
    const response = await request("1".repeat(64), {
      method: "PUT", body: Buffer.alloc(64 * 1024 * 1024 + 1),
      headers: { "Content-Length": String(64 * 1024 * 1024 + 1) },
    });
    assert.equal(response.status, 413);
    assert.equal((await request("1".repeat(64))).status, 404);
  });

  await t.test("accepts empty files and the exact 64 MiB chunk boundary", async () => {
    for (const bytes of [Buffer.alloc(0), Buffer.alloc(64 * 1024 * 1024, 7)]) {
      const response = await put(bytes, { headers: { "Content-Type": "application/octet-stream" } });
      assert.equal(response.status, bytes.length === 0 ? 200 : 201, await response.clone().text());
      const digest = sha256(bytes);
      const head = await request(digest, { method: "HEAD" });
      assert.equal(head.status, 200);
      assert.equal(head.headers.get("Content-Length"), String(bytes.length));
      assert.equal(head.headers.get("X-Content-SHA256"), digest);
    }
  });
});
