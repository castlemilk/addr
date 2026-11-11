import { NextRequest, NextResponse } from "next/server";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import path from "node:path";
import { ReviewError } from "@/lib/review-feedback";

// Every row in the eyes sidecar index is a diagnostic or a reference agreement.
// None of it is calibrated accuracy or a label, and this route never writes.
export const dynamic = "force-dynamic";
const execute = promisify(execFile);
// Same character set as scenePath(); a leading dash would read as a CLI flag.
const SCENE_ID = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$/;
const OBJECT_ID = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$/;
const EYES_NOTE = "Diagnostics and reference agreement from the eyes sidecar index (data/reviews/eyes/eyes.sqlite). Not accuracy, not labels; agent observations are tier 3 and never enter a denominator.";

export async function GET(request: NextRequest) {
  try {
    const scene = request.nextUrl.searchParams.get("scene") || "", object = request.nextUrl.searchParams.get("object") || "";
    if (!SCENE_ID.test(scene) || !OBJECT_ID.test(object)) throw new ReviewError("Choose a valid scene and object.");
    const { stdout } = await execute(path.join(process.cwd(), "scripts/ml/.venv/bin/python"),
      ["-m", "scripts.ml.eyes_index", "--object", scene, object], { cwd: process.cwd(), timeout: 20000, maxBuffer: 16 * 1024 * 1024 });
    return NextResponse.json({ ...JSON.parse(stdout), note: EYES_NOTE }, { headers: { "Cache-Control": "no-store" } });
  } catch (error) {
    return NextResponse.json({ error: error instanceof ReviewError ? error.message : "The eyes index could not be read. Saved predictions and reviews are unchanged." },
      { status: error instanceof ReviewError ? error.status : 502, headers: { "Cache-Control": "no-store" } });
  }
}
