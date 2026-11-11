import { build } from "esbuild";
import { readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

const root = fileURLToPath(new URL("../", import.meta.url));
const result = await build({
  absWorkingDir: root,
  entryPoints: ["apps/mcp-review/main.ts"],
  bundle: true,
  minify: true,
  format: "iife",
  target: "es2022",
  write: false,
  outfile: "mcp-app.js",
  legalComments: "inline",
});
const javascript = result.outputFiles.find((file) => file.path.endsWith(".js"))?.text;
const css = result.outputFiles.find((file) => file.path.endsWith(".css"))?.text;
if (!javascript || !css) throw new Error("MCP App bundle did not include JavaScript and styles");
const template = await readFile(resolve(root, "apps/mcp-review/index.html"), "utf8");
const html = template
  .replace("<!-- MCP_APP_STYLES -->", () => `<style>${css.replace(/<\/style/gi, "<\\/style")}</style>`)
  .replace("<!-- MCP_APP_SCRIPT -->", () => `<script>${javascript.replace(/<\/script/gi, "<\\/script")}</script>`);
const output = resolve(root, "scripts/ml/mcp_app.html");
await writeFile(output, html);
console.log(`Built self-contained MCP App: ${output} (${Buffer.byteLength(html)} bytes)`);
