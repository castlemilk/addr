import { NextRequest, NextResponse } from "next/server";
import { existsSync } from "node:fs";
import path from "node:path";
import { DECISION_VERSION, EYES_INDEX, HINTS_VERSION, LOT_TAGS_VERSION, OBJECT_HINTS_VERSION, SCENE_ROW_ID, STRUCTURE_NOTE, STRUCTURE_VERSION, indexHintReader, type DatabaseLike, type ObjectHints } from "@/lib/queues";

// Agent hints for one scene object, read straight from the eyes index (data/reviews/eyes/eyes.sqlite):
// the building-flags-v2 class hints and the structure-kinds-v3 sub-kind from object_quality, and the
// decision-policy-v1 machine label with its tier from decision_policy. The first two are rule-based
// observations with their evidence, never labels; the decision is the policy's assessment, applied to an
// object only when a reviewer chooses to in the editor and saves. This route never writes. It opens the
// index read-only through node:sqlite resolved at call time; when Node has no sqlite or the index has not
// been built here, the answer is the same shape with nulls and source "unavailable" so the editor stays
// silent instead of failing. The decision_policy table may not exist yet: the reader guards that query on
// its own, so its absence reads as decision null. Two newer object_quality versions ride along the same way: the
// object-hints-v2 hints (frame cut, noisy outline, colour and size checks, a solar panel that reads like a rooftop fixture,
// with the squared outline candidate; object-hints-v1 rows are served where no v2 row exists) and the lot-tags-v1 lot tag;
// both are measured observations with their evidence, never labels. An optional `signature` (the caller's model signature,
// 64 hex) prefers the rows computed under that image version. `stale` is true when `stale_reasons` names any reason:
// "model_signature" when a served row came from another image version (judged only with a signature) and "object_hints_version"
// when the object hints came from an older object-hints version (judged always). `object_hints_served_version` names the version
// of the row served (`object_hints_version` stays the current rules), so the editor can say what is out of date. 404 means the index
// holds none of the five for the object. The scene-level lot-tags-v1 row (object_id "_scene") supplies `lot_frame`, the
// lot-frame plan sentence for the whole scene (text only), on every answer and on its own for object=_scene; it never
// turns an unknown object into a 200 and never counts towards `stale`.
export const dynamic = "force-dynamic";
const NO_STORE = { "Cache-Control": "no-store" };
// Same character set as scenePath(); a leading dash would read as a CLI flag.
const SCENE_ID = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$/;
const OBJECT_ID = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$/;
const objectIdOk = (object: string) => object === SCENE_ROW_ID || OBJECT_ID.test(object);  // the scene row is the one leading underscore allowed
const SIGNATURE = /^[a-f0-9]{64}$/;
type Source = "eyes-index" | "unavailable";

/** node:sqlite's DatabaseSync when this Node has it; the lookup happens per request so a bundle never imports it. */
function sqliteModule(): { DatabaseSync: new (file: string, options: { readOnly: boolean }) => DatabaseLike } | null {
  const getBuiltin = (process as { getBuiltinModule?: (id: string) => unknown }).getBuiltinModule;
  const sqlite = typeof getBuiltin === "function" ? getBuiltin("node:sqlite") as { DatabaseSync?: unknown } | undefined : undefined;
  return typeof sqlite?.DatabaseSync === "function" ? sqlite as { DatabaseSync: new (file: string, options: { readOnly: boolean }) => DatabaseLike } : null;
}
/** Both hint rows of one object, or null when hints cannot be read on this machine. Database errors throw. */
export function readObjectHints(file: string, scene: string, object: string, signature?: string): ObjectHints | null {
  const sqlite = sqliteModule();
  if (!sqlite || !existsSync(file)) return null;
  const reader = indexHintReader(new sqlite.DatabaseSync(file, { readOnly: true }));
  try {
    const hints = reader.read(scene, object, signature);
    let lot_frame: string | null;
    try { lot_frame = reader.lotFrame(scene, signature); } catch { lot_frame = null; }  // an index without the scene row hides nothing else
    return { ...hints, lot_frame };
  } finally { reader.close(); }
}
const NONE: ObjectHints = { class_hints: null, structure: null, decision: null, object_hints: null, lot: null, stale: false, stale_reasons: [], object_hints_served_version: null, lot_frame: null };
const empty = (hints: ObjectHints) => !hints.class_hints && !hints.structure && !hints.decision && !hints.object_hints && !hints.lot;
const body = (scene: string, object: string, hints: ObjectHints, source: Source) => ({
  scene_id: scene, object_id: object, class_hints: hints.class_hints, structure: hints.structure, decision: hints.decision,
  object_hints: hints.object_hints ?? null, lot: hints.lot ?? null, stale: hints.stale ?? false, stale_reasons: hints.stale_reasons ?? [],
  object_hints_served_version: hints.object_hints_served_version ?? null, lot_frame: hints.lot_frame ?? null,
  hints_version: HINTS_VERSION, structure_version: STRUCTURE_VERSION, decision_version: DECISION_VERSION, object_hints_version: OBJECT_HINTS_VERSION, lot_tags_version: LOT_TAGS_VERSION,
  source, note: STRUCTURE_NOTE,
});

export async function GET(request: NextRequest) {
  const scene = request.nextUrl.searchParams.get("scene") || "", object = request.nextUrl.searchParams.get("object") || "";
  const requested = request.nextUrl.searchParams.get("signature") || "";
  const signature = SIGNATURE.test(requested) ? requested : undefined;  // anything else is ignored, never an error
  if (!SCENE_ID.test(scene) || !objectIdOk(object)) return NextResponse.json({ error: "Choose a valid scene and object." }, { status: 400, headers: NO_STORE });
  try {
    const hints = readObjectHints(path.join(process.cwd(), EYES_INDEX), scene, object, signature);
    if (!hints) return NextResponse.json(body(scene, object, NONE, "unavailable"), { headers: NO_STORE });
    if (empty(hints) && !(object === SCENE_ROW_ID && hints.lot_frame)) return NextResponse.json({ error: "The eyes index holds no agent hints for this object." }, { status: 404, headers: NO_STORE });
    return NextResponse.json(body(scene, object, hints, "eyes-index"), { headers: NO_STORE });
  } catch {
    return NextResponse.json({ error: "Agent hints could not be read from the eyes index. Saved predictions and reviews are unchanged." }, { status: 500, headers: NO_STORE });
  }
}
