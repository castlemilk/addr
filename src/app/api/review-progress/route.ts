import { NextResponse } from "next/server";
import { execFile } from "node:child_process";
import path from "node:path";

/* GET /api/review-progress: the live count behind /queues/small-roofs. Runs the round-5 counter
   `scripts/ml/.venv/bin/python -m scripts.ml.training.sam3_mac.r5_precision count --json --block v2` from the repo root
   and returns its JSON unchanged. Every count is the reviewer's own decisions as saved in the app, read at the instant
   of the run; the counter prints no share below its pre-registered rule, and nothing here is a share or a score.
   Read-only: this route never writes a review, and Python is asked not to write bytecode. One Python process per 10 s
   window at most and never two at once: every request in a window, and every request while a run is still going,
   shares one module-level promise; a failed run is shared for the rest of its window too. 20 s timeout, 4 MB output
   ceiling. Until the counter module exists the route answers 200 {available: false, reason: "counter not installed"}. */

export const dynamic = "force-dynamic";
const NO_STORE = { "Cache-Control": "no-store" };
const MODULE = "scripts.ml.training.sam3_mac.r5_precision";
const WINDOW_MS = 10000, TIMEOUT_MS = 20000, MAX_BUFFER = 4 * 1024 * 1024;
// `python -m` names the module itself when it cannot find it; an import failing inside an existing module names another.
const NOT_INSTALLED = /No module named '?scripts\.ml\.training\.sam3_mac\.r5_precision'?(?![\w.])/;
const COULD_NOT_RUN = "The small-roof counter could not run. Saved reviews are unchanged.";

type Outcome = { kind: "counted"; body: Record<string, unknown> } | { kind: "not-installed" } | { kind: "failed"; message: string };
let latest: { window: number; settled: boolean; outcome: Promise<Outcome> } | null = null;

function runCounter(): Promise<Outcome> {
  const root = process.cwd();
  return new Promise(resolve => {
    try {
      execFile(path.join(root, "scripts/ml/.venv/bin/python"), ["-m", MODULE, "count", "--json", "--block", "v2"],
        { cwd: root, timeout: TIMEOUT_MS, maxBuffer: MAX_BUFFER, encoding: "utf8", env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" } },
        (error, stdout, stderr) => {
          if (error) {
            if (NOT_INSTALLED.test(stderr)) return resolve({ kind: "not-installed" });
            console.error(`review-progress: counter failed (${error.code ?? error.signal ?? "error"}): ${stderr.trim().split("\n").pop()?.slice(0, 300) ?? ""}`);
            if (error.code === "ERR_CHILD_PROCESS_STDIO_MAXBUFFER") return resolve({ kind: "failed", message: `The small-roof counter's answer was larger than ${MAX_BUFFER / 1024 / 1024} MB. Saved reviews are unchanged.` });
            if (error.killed) return resolve({ kind: "failed", message: `The small-roof counter did not answer within ${TIMEOUT_MS / 1000} seconds. Saved reviews are unchanged.` });
            return resolve({ kind: "failed", message: COULD_NOT_RUN });
          }
          try {
            const body: unknown = JSON.parse(stdout);
            if (body && typeof body === "object" && !Array.isArray(body)) return resolve({ kind: "counted", body: body as Record<string, unknown> });
          } catch { /* answered below */ }
          resolve({ kind: "failed", message: "The small-roof counter answered with something other than a JSON object. Saved reviews are unchanged." });
        });
    } catch {
      resolve({ kind: "failed", message: COULD_NOT_RUN });
    }
  });
}

/** The run for this 10 s window, started by the first request in it; a run still going when the window turns is shared. */
function counted(): Promise<Outcome> {
  const window = Math.floor(Date.now() / WINDOW_MS);
  if (latest && (latest.window === window || !latest.settled)) return latest.outcome;
  const entry = { window, settled: false, outcome: runCounter() };
  void entry.outcome.then(() => { entry.settled = true; });
  latest = entry;
  return entry.outcome;
}

export async function GET() {
  const outcome = await counted();
  if (outcome.kind === "counted") return NextResponse.json(outcome.body, { headers: NO_STORE });
  if (outcome.kind === "not-installed") return NextResponse.json({ available: false, reason: "counter not installed" }, { headers: NO_STORE });
  return NextResponse.json({ error: outcome.message }, { status: 500, headers: NO_STORE });
}
