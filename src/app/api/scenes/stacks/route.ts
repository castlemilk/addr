import { NextRequest, NextResponse } from "next/server";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { ReviewError } from "@/lib/review-feedback";
import { scenePath } from "@/lib/scene-store";

// Lists scripts/ml/scene_local_stack reports bound to a scene's current saved PNG (scene_id and
// source_image_sha256 match latest.json). Everything listed is a display view for inspection: the saved PNG
// stays the sole evidence gate and roof lines, outlines and saves never use an aligned date or a composite.
export const dynamic = "force-dynamic";
const SCENE_ID = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$/;
const STACK_ID = /^[a-f0-9]{16}$/;
const STACKS_FOLDER = "data/roof-review-loop/multidate/stacks";
const COMPOSITES = ["enhanced-median-rgb.png", "enhanced-pair-rgb.png"];
const STATUS_RANK: Record<string, number> = { available: 0, pair: 1 };
const NOTE = "Display views registered onto the saved scene grid for inspection only; not evidence images, not labels. Roof lines and saves still use the original saved PNG.";
type Capture = { index?: number; date?: string | null; sensor?: string | null; status?: string; registration?: { coverage?: number | null } | null };
type Report = { stack_id?: string; status?: string; scene_id?: string; source_image_sha256?: string; n_accepted?: number; computed_at?: string; reason?: string;
  captures?: Capture[]; assets?: Record<string, { path?: string; sha256?: string }>; stack?: { mode?: string } | null };
export type StackSummary = {
  stack_id: string; status: string; mode: string | null; n_accepted: number; computed_at: string | null; reason: string | null;
  dates: { index: number; date: string | null; sensor: string | null; accepted: boolean; coverage: number | null }[];
  assets: string[]; composite: string | null;
};

function summarise(stack: string, report: Report): StackSummary {
  const assets = Object.keys(report.assets ?? {}).filter(name => report.assets?.[name]?.path === name).sort();
  return {
    stack_id: stack,
    status: String(report.status ?? "unknown"),
    mode: report.stack?.mode ?? (report.status === "pair" ? "pair" : report.status === "available" ? "stack" : null),
    n_accepted: Number.isFinite(report.n_accepted) ? Number(report.n_accepted) : 0,
    computed_at: typeof report.computed_at === "string" ? report.computed_at : null,
    reason: typeof report.reason === "string" ? report.reason : null,
    dates: (Array.isArray(report.captures) ? report.captures : []).map((capture, position) => ({
      index: Number.isInteger(capture?.index) ? Number(capture.index) : position,
      date: typeof capture?.date === "string" ? capture.date : null,
      sensor: typeof capture?.sensor === "string" ? capture.sensor : null,
      accepted: capture?.status === "accepted",
      coverage: typeof capture?.registration?.coverage === "number" ? capture.registration.coverage : null,
    })),
    assets,
    composite: COMPOSITES.find(name => assets.includes(name)) ?? null,
  };
}

export async function listStacks(scene: string, imageSha256: string): Promise<StackSummary[]> {
  const folder = path.join(process.cwd(), STACKS_FOLDER);
  let names: string[] = [];
  try { names = await readdir(folder); } catch { return []; }
  const stacks: StackSummary[] = [];
  for (const name of names.filter(name => STACK_ID.test(name)).sort()) {
    try {
      const report: Report = JSON.parse(await readFile(path.join(folder, name, "report.json"), "utf8"));
      if (report?.scene_id !== scene || report?.source_image_sha256 !== imageSha256) continue;
      stacks.push(summarise(name, report));
    } catch { continue; }
  }
  const rank = (status: string) => STATUS_RANK[status] ?? 2;
  return stacks.sort((a, b) => rank(a.status) - rank(b.status) || (b.computed_at ?? "").localeCompare(a.computed_at ?? "") || a.stack_id.localeCompare(b.stack_id));
}

export async function GET(request: NextRequest) {
  const headers = { "Cache-Control": "no-store" };
  try {
    const scene = request.nextUrl.searchParams.get("scene") || "";
    if (!SCENE_ID.test(scene)) throw new ReviewError("Choose a valid scene.");
    let imageSha256 = "";
    try {
      const latest = JSON.parse(await readFile(path.join(scenePath(scene), "latest.json"), "utf8"));
      imageSha256 = typeof latest?.image_sha256 === "string" ? latest.image_sha256 : "";
    } catch { throw new ReviewError("That scene has no saved image.", 404); }
    if (!/^[a-f0-9]{64}$/.test(imageSha256)) throw new ReviewError("That scene has no saved image.", 404);
    return NextResponse.json({ scene_id: scene, image_sha256: imageSha256, stacks: await listStacks(scene, imageSha256), note: NOTE }, { headers });
  } catch (error) {
    return NextResponse.json({ error: error instanceof ReviewError ? error.message : "Multi-date views could not be listed. The original image is unchanged." },
      { status: error instanceof ReviewError ? error.status : 500, headers });
  }
}
