import { spawnSync } from "node:child_process";
import { deploymentDirectory, deploymentEnvironment, requireValue } from "./env.mjs";

const env = deploymentEnvironment();
const databaseUrl = requireValue(env, "DATABASE_URL");
const token = requireValue(env, "ANNOTATION_TOKEN");
const firebase = env.FIREBASE_WEB_CONFIG?.trim(), emails = env.FIREBASE_ALLOWED_EMAILS?.trim();
if (!!firebase !== !!emails) throw new Error("Set both FIREBASE_WEB_CONFIG and FIREBASE_ALLOWED_EMAILS to enable Google login.");
if (firebase) {
  const config = JSON.parse(firebase);
  if (!["apiKey","authDomain","projectId","appId"].every(key=>typeof config[key] === "string" && config[key])) throw new Error("Firebase web configuration is incomplete.");
  if (!emails.split(",").every(email=>/^[^\s@*]+@[^\s@*]+\.[^\s@*]+$/.test(email.trim()))) throw new Error("Use exact allowed email addresses, without wildcards.");
}
if (token.trim().length < 24) throw new Error("ANNOTATION_TOKEN must have at least 24 characters.");
const result = spawnSync("npx", ["--no-install", "wrangler", "secret", "bulk"], {
  cwd: deploymentDirectory,
  env: process.env,
  input: JSON.stringify({ DATABASE_URL: databaseUrl, ANNOTATION_TOKEN: token,
    ...(firebase ? { FIREBASE_WEB_CONFIG:firebase, FIREBASE_ALLOWED_EMAILS:emails } : {}) }),
  stdio: ["pipe", "inherit", "inherit"],
});
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
