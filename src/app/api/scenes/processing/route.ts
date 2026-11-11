import { NextResponse, type NextRequest } from "next/server";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import path from "node:path";
import type { InventoryState, ProcessingData, ProcessingSummary } from "@/lib/scene-processing";
import { hiddenLowRoofs, roofByKey, sliceRoofs } from "@/lib/scene-processing-slice";

const execute = promisify(execFile);
export const dynamic = "force-dynamic";
/** The full inventory costs ~5 s in Python and ~36 MB; it is read once and sliced in memory. */
const INVENTORY_TTL = 20000, REFRESH_LEAD = 5000, SUMMARY_TTL = 5000;
/** How long past expiry the last inventory is still served (marked "warming") while one refresh runs behind it.
 *  Beyond it reads wait on the refresh, so a failing Python read becomes an honest 503 instead of silent stale data. */
const STALE_GRACE = 60000;
type Cached<T> = { value: T; until: number };
let pending: Promise<ProcessingData> | undefined;
let cached: Cached<ProcessingData> | undefined;
let summaryPending: Promise<Omit<ProcessingSummary, "inventory">> | undefined;
let summaryCached: Cached<Omit<ProcessingSummary, "inventory">> | undefined;

function python<T>(args: string[], timeout: number): Promise<T> {
  return execute(path.join(process.cwd(), "scripts/ml/.venv/bin/python"),
    ["-m", "scripts.ml.scene_processing", ...args],
    { cwd: process.cwd(), timeout, maxBuffer: 64 * 1024 * 1024 }).then(({ stdout }) => JSON.parse(stdout) as T);
}

/** Single-flight inventory read. Polling tabs share one read-only scan; disconnecting one client
 *  must not cancel it for everyone or cause another expensive scan to start. */
function refresh() {
  pending ??= python<ProcessingData>([], 90000)
    .then(value => { cached = { value, until: Date.now() + INVENTORY_TTL }; return value; })
    .finally(() => { pending = undefined; });
  return pending;
}

function snapshot(): Promise<ProcessingData> {
  const now = Date.now();
  if (cached && cached.until > now) {
    // Close to expiry: start one background refresh so warm reads never wait on a full scan.
    if (cached.until - now < REFRESH_LEAD) refresh().catch(() => undefined);
    return Promise.resolve(cached.value);
  }
  // Expired but read recently: a 20 s poll against a 20 s TTL anchored on Python completion lands here on every
  // other poll, so the last inventory is served at once (inventoryState() says "warming") while exactly one
  // refresh runs behind it. Only a cold start, or a cache long past its grace, waits on Python.
  if (cached && cached.until + STALE_GRACE > now) {
    refresh().catch(() => undefined);
    return Promise.resolve(cached.value);
  }
  return refresh();
}

/** Progress, tile queue and object totals only (~0.3 s in Python); its own coalescing and cache. */
function summary() {
  if (summaryCached && summaryCached.until > Date.now()) return Promise.resolve(summaryCached.value);
  summaryPending ??= python<Omit<ProcessingSummary, "inventory">>(["--summary"], 45000)
    .then(value => { summaryCached = { value, until: Date.now() + SUMMARY_TTL }; return value; })
    .finally(() => { summaryPending = undefined; });
  return summaryPending;
}

/** "ready" only for a cached inventory inside its TTL with no refresh running; a stale or refreshing one is "warming". */
const inventoryState = (): InventoryState => (cached && !pending && cached.until > Date.now() ? "ready" : "warming");

export async function GET(request?: NextRequest) {
  const params = request?.nextUrl?.searchParams ?? new URLSearchParams();
  const part = params.get("part") || "";
  const headers = { "Cache-Control": "no-store" };
  try {
    if (!part) return NextResponse.json(await snapshot(), { headers });
    if (part === "summary") return NextResponse.json({ ...await summary(), inventory: inventoryState() }, { headers });
    if (part === "roofs") {
      const data = await snapshot();
      return NextResponse.json({ ...sliceRoofs(data, Object.fromEntries(params.entries())), inventory: inventoryState() }, { headers });
    }
    if (part === "roof") {
      const key = params.get("key");
      if (!key) return NextResponse.json({ error: "A roof key (<scene>/<object>) is required." }, { status: 400, headers });
      const data = await snapshot(), roof = roofByKey(data.features, key);
      if (!roof) return NextResponse.json({ error: "This roof is no longer in the current inventory. It may have been re-analysed or replaced." }, { status: 404, headers });
      return NextResponse.json({ roof, fetched_at: data.fetched_at, inventory: inventoryState() }, { headers });
    }
    if (part === "hidden-low") {
      const data = await snapshot(), rows = hiddenLowRoofs(data.features);
      return NextResponse.json({ rows, total: rows.length, fetched_at: data.fetched_at, inventory: inventoryState() }, { headers });
    }
    return NextResponse.json({ error: "Unknown processing part. Use summary, roofs, roof or hidden-low." }, { status: 400, headers });
  } catch {
    return NextResponse.json({ error: "Processing status is unavailable. The background run is independent of this page; retry to reconnect." }, { status: 503, headers });
  }
}
