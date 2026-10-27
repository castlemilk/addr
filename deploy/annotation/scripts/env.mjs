import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { parseEnv } from "node:util";

export const deploymentDirectory = fileURLToPath(new URL("../", import.meta.url));
export const repositoryDirectory = fileURLToPath(new URL("../../../", import.meta.url));

export function deploymentEnvironment() {
  let local = {};
  try {
    local = parseEnv(readFileSync(new URL("../.dev.vars", import.meta.url), "utf8"));
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  return { ...local, ...process.env };
}

export function requireValue(env, name) {
  if (!env[name]) throw new Error(`Set ${name} in deploy/annotation/.dev.vars or the environment.`);
  return env[name];
}
