import { NextResponse } from "next/server";
import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { promisify } from "node:util";
import { EYES_INDEX, HIDDEN_VERSION, hiddenQueue, QueueError } from "@/lib/queues";

// Low-band roofs the map hides under a medium/high capture of the same roof, read straight from the eyes
// sidecar index (data/reviews/eyes/eyes.sqlite, table cross_capture) with Node's SQLite so a loaded
// machine never has to spawn Python for this page. The document mirrors what
// `python -m scripts.ml.cross_capture --hidden-low` prints; that CLI remains the fallback when Node's
// SQLite is unavailable. Cross-capture agreement is a diagnostic, not accuracy; rows only link into the
// canonical editor. An absent index is 503 (the summary reports hidden.count null for the same state),
// never "0 hidden roofs".
export const dynamic = "force-dynamic";
const execute = promisify(execFile);
const NO_STORE = { "Cache-Control": "no-store" };
const HIGHER = new Set(["medium", "high"]);
const NOTE = "hidden roof band low under a shown medium/high capture of the same roof; both share one orthomosaic, so agreement is consistency, not independent evidence; bands are model estimates, not accuracy";
const reviewUrl = (scene: string, object: string) => `/scenes?scene=${encodeURIComponent(scene)}&object=${encodeURIComponent(object)}&kind=building&status=all&layer=model`;

type Row = { scene_id: string; object_id: string; other_scene_id: string; other_object_id: string; payload: string };

/** The `--hidden-low` document, read from the index without Python. */
export function readHiddenLow(file: string) {
  if (!existsSync(file)) return { version: HIDDEN_VERSION, count: 0, rows: [], index_available: false, note: NOTE };
  let db: DatabaseSync | null = null;
  try {
    db = new DatabaseSync(file, { readOnly: true });
    const records = db.prepare("SELECT scene_id, object_id, other_scene_id, other_object_id, payload FROM cross_capture WHERE version = ?"
      + " AND json_extract(payload, '$.band') = 'low' AND json_extract(payload, '$.other_band') IN ('medium', 'high')")
      .all(HIDDEN_VERSION) as unknown as Row[];
    const rows = [];
    for (const record of records) {
      let payload: Record<string, unknown> = {};
      try { const parsed = JSON.parse(record.payload); if (parsed && typeof parsed === "object") payload = parsed as Record<string, unknown>; } catch { payload = {}; }
      if (payload.band !== "low" || !HIGHER.has(String(payload.other_band))) continue;
      rows.push({
        scene_id: record.scene_id, object_id: record.object_id, band: payload.band,
        detector_score: payload.score ?? null, observed_segments: payload.segments ?? null,
        other_scene_id: record.other_scene_id, other_object_id: record.other_object_id,
        other_band: payload.other_band, other_detector_score: payload.other_score ?? null, iou: payload.iou ?? null,
        hidden_url: reviewUrl(record.scene_id, record.object_id), shown_url: reviewUrl(record.other_scene_id, record.other_object_id),
      });
    }
    rows.sort((a, b) => `${a.scene_id}/${a.object_id}/${a.other_scene_id}/${a.other_object_id}`.localeCompare(`${b.scene_id}/${b.object_id}/${b.other_scene_id}/${b.other_object_id}`));
    return { version: HIDDEN_VERSION, count: rows.length, rows, index_available: true, note: NOTE };
  } finally {
    db?.close();
  }
}

async function readWithPython() {
  const { stdout } = await execute(path.join(process.cwd(), "scripts/ml/.venv/bin/python"),
    ["-m", "scripts.ml.cross_capture", "--hidden-low"], { cwd: process.cwd(), timeout: 60000, maxBuffer: 16 * 1024 * 1024 });
  return JSON.parse(stdout) as { index_available?: unknown };
}

export async function GET() {
  try {
    let document: { index_available?: unknown };
    try {
      document = readHiddenLow(path.join(process.cwd(), EYES_INDEX));
    } catch {
      document = await readWithPython();
    }
    if (document?.index_available === false)
      throw new QueueError("The eyes index has not been built on this machine, so hidden roofs cannot be listed. Saved predictions and reviews are unchanged.", 503);
    return NextResponse.json(hiddenQueue(document), { headers: NO_STORE });
  } catch (error) {
    return NextResponse.json({ error: error instanceof QueueError ? error.message : "The hidden-roof queue could not be read from the eyes index. Saved predictions and reviews are unchanged." },
      { status: error instanceof QueueError ? error.status : 502, headers: NO_STORE });
  }
}
