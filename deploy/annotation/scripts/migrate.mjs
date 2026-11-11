import { spawnSync } from "node:child_process";
import { deploymentEnvironment, repositoryDirectory, requireValue } from "./env.mjs";

const env = deploymentEnvironment();
let url;
try {
  url = new URL(requireValue(env, "DATABASE_URL_UNPOOLED"));
} catch {
  throw new Error("Set a valid direct PostgreSQL URL in DATABASE_URL_UNPOOLED.");
}
if (url.hostname.includes("-pooler.")) {
  throw new Error("Migrations require the direct DATABASE_URL_UNPOOLED connection.");
}
const result = spawnSync("go", ["run", "./cmd/annotation-server", "--migrate"], {
  cwd: repositoryDirectory,
  env,
  stdio: "inherit",
});
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
