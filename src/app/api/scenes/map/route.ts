import { NextRequest, NextResponse } from "next/server";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import path from "node:path";
const execute = promisify(execFile);
export const dynamic = "force-dynamic";
export async function GET(request: NextRequest) {
  const bounds = request.nextUrl.searchParams.get("bounds")?.split(",").map(Number), layer = request.nextUrl.searchParams.get("layer") || "edited";
  if (!bounds || bounds.length !== 4 || !bounds.every(Number.isFinite) || !["edited", "model"].includes(layer)) return NextResponse.json({ error: "Choose map bounds and a review layer." }, { status: 400 });
  const focus = request.nextUrl.searchParams.get("focus")?.split(",").map(Number);
  if (focus && (focus.length !== 2 || !focus.every(Number.isFinite) || Math.abs(focus[0]) >= 180 || Math.abs(focus[1]) >= 85)) return NextResponse.json({ error: "Choose a valid map focus." }, { status: 400 });
  try {
    const { stdout } = await execute(path.join(process.cwd(), "scripts/ml/.venv/bin/python"), ["-m", "scripts.ml.scene_map", "--bounds", ...bounds.map(String), "--layer", layer, ...(focus ? ["--focus", ...focus.map(String)] : [])],
      { cwd: process.cwd(), timeout: 60000, maxBuffer: 24 * 1024 * 1024, signal: request.signal });
    return NextResponse.json(JSON.parse(stdout));
  } catch (error) {
    const failure = error as { code?: string; killed?: boolean; name?: string };
    if (!request.signal.aborted) console.error("Analysed map load failed", { code: failure.code, killed: failure.killed, name: failure.name });
    return NextResponse.json({ error: failure.killed
      ? "Roof models took too long to load. Zoom in or retry."
      : "Analysed layers could not load. Retry loading the roof models." }, { status: 503 });
  }
}
