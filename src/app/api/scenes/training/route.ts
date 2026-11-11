import { NextRequest, NextResponse } from "next/server";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import path from "node:path";
import { scenePrincipal } from "@/lib/scene-store";
import { ReviewError } from "@/lib/review-feedback";
const execute = promisify(execFile);
export const dynamic = "force-dynamic";
async function run(args: string[]) {
  const { stdout } = await execute(path.join(process.cwd(), "scripts/ml/.venv/bin/python"),
    ["-m", "scripts.ml.scene_training", ...args],
    { cwd: process.cwd(), timeout: 180000, maxBuffer: 64 * 1024 * 1024 });
  return JSON.parse(stdout);
}
// Coverage is a 13 MB read over every saved review (10-40 s of Python); concurrent readers share one run
// and a short-lived result so the training page and its verify script never start a second scan.
const COVERAGE_TTL_MS = 60000;
let coverage: { at: number; value: unknown } | null = null, coveragePending: Promise<unknown> | null = null;
async function readCoverage() {
  if (coverage && Date.now() - coverage.at < COVERAGE_TTL_MS) return coverage.value;
  if (!coveragePending) {
    coveragePending = run(["--coverage"]).then(value => { coverage = { at: Date.now(), value }; return value; })
      .finally(() => { coveragePending = null; });
  }
  return coveragePending;
}
function failure(error: unknown) {
  return NextResponse.json({ error: error instanceof ReviewError ? error.message
    : "Training coverage could not be verified. Saved reviews are preserved; retry after checking the local dataset." },
  { status: error instanceof ReviewError ? error.status : 503 });
}
export async function GET() {
  try { return NextResponse.json(await readCoverage()); }
  catch (error) { return failure(error); }
}
export async function POST(request: NextRequest) {
  try {
    const principal = await scenePrincipal(request);
    if (!principal.scopes.includes("crm.object.write"))
      throw new ReviewError("Object editing access is required to export the scene dataset.", 403);
    await run([]);
    coverage = null;
    return NextResponse.json(await readCoverage());
  } catch (error) { return failure(error); }
}
