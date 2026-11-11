import { NextRequest, NextResponse } from "next/server";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import path from "node:path";
import { ReviewError } from "@/lib/review-feedback";
import { scenePrincipal } from "@/lib/scene-store";

export const dynamic = "force-dynamic";
const execute = promisify(execFile);
const ID = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$/;

/** Immutable agent sidecars stay separate from saved decisions and training feedback. */
export async function GET(request: NextRequest) {
  const headers = { "Cache-Control": "no-store" };
  try {
    const principal = await scenePrincipal(request);
    if (!principal.scopes.includes("crm.read")) throw new ReviewError("Roof observation reading access is required.", 403);
    const scene = request.nextUrl.searchParams.get("scene") || "";
    const object = request.nextUrl.searchParams.get("object") || "";
    if (!ID.test(scene) || !ID.test(object)) throw new ReviewError("Choose a valid scene and object.");
    const { stdout } = await execute(path.join(process.cwd(), "scripts/ml/.venv/bin/python"),
      ["-m", "scripts.ml.scene_roof_observations", scene, object],
      { cwd: process.cwd(), timeout: 20000, maxBuffer: 4 * 1024 * 1024 });
    return NextResponse.json(JSON.parse(stdout), { headers });
  } catch (error) {
    return NextResponse.json({ error: error instanceof ReviewError ? error.message : "The source-bound agent observations could not be read. Reload the scene and retry." },
      { status: error instanceof ReviewError ? error.status : 502, headers });
  }
}
