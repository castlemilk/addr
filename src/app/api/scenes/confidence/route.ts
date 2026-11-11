import { NextRequest, NextResponse } from "next/server";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import path from "node:path";

const execute = promisify(execFile);
export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const bounds = request.nextUrl.searchParams.get("bounds")?.split(",").map(Number);
  const band = request.nextUrl.searchParams.get("band") || "all";
  if (!bounds || bounds.length !== 4 || !bounds.every(Number.isFinite)
    || !(bounds[0] < bounds[2] && bounds[1] < bounds[3])
    || bounds[2] - bounds[0] > .2 || bounds[3] - bounds[1] > .2
    || !["all", "low", "medium", "high", "unassessed"].includes(band)) {
    return NextResponse.json({ error: "Zoom to a suburb-sized view to load roof confidence." }, { status: 400 });
  }
  try {
    const { stdout } = await execute(path.join(process.cwd(), "scripts/ml/.venv/bin/python"),
      ["-m", "scripts.ml.scene_confidence", "--bounds", ...bounds.map(String), "--band", band],
      { cwd: process.cwd(), timeout: 30000, maxBuffer: 32 * 1024 * 1024, signal: request.signal });
    return NextResponse.json(JSON.parse(stdout));
  } catch {
    return NextResponse.json({ error: "Confidence assessment could not load. Retry shortly." }, { status: 503 });
  }
}
