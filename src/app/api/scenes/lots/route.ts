import { NextRequest, NextResponse } from "next/server";
import { existsSync } from "node:fs";
import path from "node:path";
import { EYES_INDEX, LOT_TAGS_VERSION, readSceneLotTags, type DatabaseLike, type SceneLotTags } from "@/lib/queues";

// The lot tags of one whole scene, read straight from the eyes index (data/reviews/eyes/eyes.sqlite): every lot-tags-v1 row
// of the scene in one query. The "_scene" row is served as `address` (the lot and plan the address point resolved to, or
// nulls on a subject window that has no plan) and each object row as {dominant, flags, lots}, so the editor's lot scope can
// keep the review queue to the address's land. These are measured observations with their evidence, never labels: nothing
// here changes an object's status, kind, ring or counts, and this route never writes. It opens the index read-only through
// node:sqlite resolved at call time; when Node has no sqlite or the index has not been built here, the answer is the same
// shape, empty, with source "unavailable" so the editor's scope stays inert instead of failing. An optional `signature` (the
// caller's model signature, 64 hex) prefers the rows computed under that image version; `stale` is true when a served object
// row came from another version (judged only with a signature; the scene row never counts). A scene the index does not know
// is an empty map (200), not a 404.
export const dynamic = "force-dynamic";
const NO_STORE = { "Cache-Control": "no-store" };
// Same character set as scenePath(); a leading dash would read as a CLI flag.
const SCENE_ID = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$/;
const SIGNATURE = /^[a-f0-9]{64}$/;
type Source = "eyes-index" | "unavailable";

/** node:sqlite's DatabaseSync when this Node has it; the lookup happens per request so a bundle never imports it. */
function sqliteModule(): { DatabaseSync: new (file: string, options: { readOnly: boolean }) => DatabaseLike } | null {
  const getBuiltin = (process as { getBuiltinModule?: (id: string) => unknown }).getBuiltinModule;
  const sqlite = typeof getBuiltin === "function" ? getBuiltin("node:sqlite") as { DatabaseSync?: unknown } | undefined : undefined;
  return typeof sqlite?.DatabaseSync === "function" ? sqlite as { DatabaseSync: new (file: string, options: { readOnly: boolean }) => DatabaseLike } : null;
}
/** The lot tags of one scene, or null when the index cannot be read on this machine. Database errors throw. */
export function readLotTags(file: string, scene: string, signature?: string): SceneLotTags | null {
  const sqlite = sqliteModule();
  if (!sqlite || !existsSync(file)) return null;
  const db = new sqlite.DatabaseSync(file, { readOnly: true });
  try { return readSceneLotTags(db, scene, signature); } finally { try { db.close(); } catch { /* already closed */ } }
}
const NONE: SceneLotTags = { address: null, objects: {}, stale: false };
const body = (scene: string, tags: SceneLotTags, source: Source) => ({ scene_id: scene, lot_tags_version: LOT_TAGS_VERSION, source, address: tags.address, objects: tags.objects, stale: tags.stale });

export async function GET(request: NextRequest) {
  const scene = request.nextUrl.searchParams.get("scene") || "";
  const requested = request.nextUrl.searchParams.get("signature") || "";
  const signature = SIGNATURE.test(requested) ? requested : undefined;  // anything else is ignored, never an error
  if (!SCENE_ID.test(scene)) return NextResponse.json({ error: "Choose a valid scene." }, { status: 400, headers: NO_STORE });
  try {
    const tags = readLotTags(path.join(process.cwd(), EYES_INDEX), scene, signature);
    if (!tags) return NextResponse.json(body(scene, NONE, "unavailable"), { headers: NO_STORE });
    return NextResponse.json(body(scene, tags, "eyes-index"), { headers: NO_STORE });
  } catch {
    return NextResponse.json({ error: "Lot tags could not be read from the eyes index. Saved predictions and reviews are unchanged." }, { status: 500, headers: NO_STORE });
  }
}
