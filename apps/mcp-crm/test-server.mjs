import { createServer } from "node:http";
import { readFile } from "node:fs/promises";

const generated = await readFile(new URL("../../deploy/annotation/src/mcp-app.ts", import.meta.url), "utf8");
const match = /export const CRM_APP_HTML = (.+);\n?$/.exec(generated);
if (!match) throw new Error("Build the MCP app before starting the synthetic host");
const appHtml = JSON.parse(match[1]);
const fixture = await readFile(new URL("./test-host.html", import.meta.url), "utf8");
const server = createServer((request, response) => {
  if (request.url === "/favicon.ico") { response.writeHead(204); response.end(); return; }
  if (request.method !== "GET" || !["/", "/app.html"].includes(request.url)) { response.writeHead(404); response.end(); return; }
  response.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" });
  response.end(request.url === "/app.html" ? appHtml : fixture);
});
server.listen(3018, "127.0.0.1", () => console.log("Synthetic MCP test host: http://127.0.0.1:3018"));
process.on("SIGTERM", () => server.close());
process.on("SIGINT", () => server.close());
