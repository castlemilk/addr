import { NextResponse } from "next/server";
import { existsSync } from "node:fs";
import path from "node:path";
import { DECISION_FALLBACK_VERSIONS, DECISION_LIST_SQL, DECISION_VERSION, EYES_INDEX, MACHINE_LABELLED_NOTE, indexHintSource, machineLabelledQueue, type DatabaseLike, type MachineLabelledQueue } from "@/lib/queues";

// The Machine labelled queue: every roof the decision policy placed in the auto tier, read straight from the
// eyes index (data/reviews/eyes/eyes.sqlite, table decision_policy) with one query over the current and
// fallback versions and joined to nothing else; class and structure hints are attached through the existing
// hint source. Read-only through node:sqlite resolved at call time, like the hints route: when Node has no
// sqlite, the index has not been built here or the table does not exist yet, the answer is the same shape
// with no rows and source "unavailable" so /queues says so instead of failing. This route never writes.
export const dynamic = "force-dynamic";
const NO_STORE = { "Cache-Control": "no-store" };
type Source = "eyes-index" | "unavailable";

function sqliteModule(): { DatabaseSync: new (file: string, options: { readOnly: boolean }) => DatabaseLike } | null {
  const getBuiltin = (process as { getBuiltinModule?: (id: string) => unknown }).getBuiltinModule;
  const sqlite = typeof getBuiltin === "function" ? getBuiltin("node:sqlite") as { DatabaseSync?: unknown } | undefined : undefined;
  return typeof sqlite?.DatabaseSync === "function" ? sqlite as { DatabaseSync: new (file: string, options: { readOnly: boolean }) => DatabaseLike } : null;
}
/** The decision rows of the current and fallback versions, or null when the index or the table is not here. Other database errors throw. */
export function readDecisionRows(file: string): unknown[] | null {
  const sqlite = sqliteModule();
  if (!sqlite || !existsSync(file)) return null;
  const db = new sqlite.DatabaseSync(file, { readOnly: true });
  try {
    const statement = db.prepare(DECISION_LIST_SQL);
    if (typeof statement.all !== "function") return null;
    const rows = statement.all(DECISION_VERSION, DECISION_FALLBACK_VERSIONS[0] ?? DECISION_VERSION);
    return Array.isArray(rows) ? rows : [];
  } catch (error) {
    if (/no such table/i.test(String((error as { message?: string })?.message))) return null;
    throw error;
  } finally { try { db.close(); } catch { /* already closed */ } }
}
const body = (queue: MachineLabelledQueue, source: Source) => ({ ...queue, source });
const EMPTY: MachineLabelledQueue = { version: DECISION_VERSION, versions: [DECISION_VERSION, ...DECISION_FALLBACK_VERSIONS], count: 0, reviewed_by_person: 0, rows: [], note: MACHINE_LABELLED_NOTE };

export async function GET() {
  try {
    const file = path.join(process.cwd(), EYES_INDEX);
    const rows = readDecisionRows(file);
    if (!rows) return NextResponse.json(body(EMPTY, "unavailable"), { headers: NO_STORE });
    const sqlite = sqliteModule();
    const hints = sqlite ? (() => { try { return indexHintSource(new sqlite.DatabaseSync(file, { readOnly: true })); } catch { return null; } })() : null;
    return NextResponse.json(body(machineLabelledQueue(rows, hints), "eyes-index"), { headers: NO_STORE });
  } catch {
    return NextResponse.json({ error: "Machine-labelled roofs could not be read from the eyes index. Saved predictions and reviews are unchanged." }, { status: 500, headers: NO_STORE });
  }
}
