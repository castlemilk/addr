import { NextRequest, NextResponse } from "next/server";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import path from "node:path";
import { scenePrincipal } from "@/lib/scene-store";
import { ReviewError } from "@/lib/review-feedback";

export const dynamic = "force-dynamic";
const execute = promisify(execFile);
async function command(args: string[]) {
  const { stdout } = await execute(path.join(process.cwd(), "scripts/ml/.venv/bin/python"),
    ["-m", "scripts.ml.roof_review_loop", ...args], {
      cwd: process.cwd(), timeout: args.includes("--start") ? 90000 : 30000, maxBuffer: 16 * 1024 * 1024,
    });
  return NextResponse.json(JSON.parse(stdout), { headers: { "Cache-Control": "no-store" } });
}
export async function GET(request: NextRequest) {
  try {
    const run = request.nextUrl.searchParams.get("run"), index = request.nextUrl.searchParams.get("object");
    if (run !== null || index !== null) {
      if (!/^[a-f0-9]{16}$/.test(run || "") || !/^\d{1,2}$/.test(index || "") || Number(index) > 23)
        throw new ReviewError("Invalid candidate identity.");
      return await command(["--run", run!, "--detail", index!]);
    }
    return await command([]);
  } catch (error) {
    return NextResponse.json({ error: error instanceof ReviewError ? error.message : "Self-review results are unavailable." },
      { status: error instanceof ReviewError ? error.status : 503 });
  }
}
export async function POST(request: NextRequest) {
  try {
    const principal = await scenePrincipal(request);
    if (!principal.scopes.includes("crm.roof.write")) throw new ReviewError("Roof editing access is required.", 403);
    const body = await request.json();
    if (body.limit !== 12 || (body.resume && !/^[a-f0-9]{16}$/.test(body.resume)))
      throw new ReviewError("Start a bounded batch of 12 roofs.");
    return await command(["--start", "--limit", "12", ...(body.resume ? ["--resume", body.resume] : [])]);
  } catch (error) {
    if (!(error instanceof ReviewError)) {
      const failure = error as { code?: string; signal?: string; killed?: boolean };
      console.error("Self-review start failed", { code: failure.code, signal: failure.signal, killed: failure.killed });
    }
    const timedOut = Boolean((error as { killed?: boolean })?.killed);
    return NextResponse.json({ error: error instanceof ReviewError ? error.message : timedOut
      ? "Preparing the review batch timed out. Refresh the queue before retrying; a worker may already have started."
      : "Could not start self-review. Existing predictions are unchanged." },
      { status: error instanceof ReviewError ? error.status : 503 });
  }
}
