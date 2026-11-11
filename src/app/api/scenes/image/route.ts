import { NextRequest } from "next/server";
import { readFile } from "node:fs/promises";
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { promisify } from "node:util";
import path from "node:path";
import { scenePath } from "@/lib/scene-store";
const execute = promisify(execFile);
const detailImages = new Map<string, Buffer>();
const detailPending = new Map<string, Promise<Buffer>>();
const DETAIL_VIEW = "roof-detail-v1";
// CLAHE window = shorter image side / DETAIL_TILES (the previous cv2 view used an 8x8 tile grid; 144 px on a 1152 px
// scene). libvips maxSlope is 0..100 with 0 = unlimited; the brief's 3 measured as a no-op on 27 Cambridge (gray std
// x1.01, mean |diff| 0.75/255), 100 lifts local contrast x1.3-1.4 against x1.5-1.6 for the old cv2 view.
const DETAIL_TILES = 8;
const DETAIL_MAX_SLOPE = 100;
const MULTIDATE_VIEW = "multidate-median-v1";
const ALIGNED_VIEW = "multidate-aligned";
const COMPOSITE_VIEW = "multidate-composite";
const CROP_VIEW = "crop-v1";
const CROP_MAX_SIZE = 512;
const STACK_ID = /^[a-f0-9]{16}$/;
const SHA256 = /^[a-f0-9]{64}$/;

// roof-detail-v1: a display-only gray contrast view (CLAHE) of the saved PNG rendered in Node. Same size, same
// registration, no resampling: the saved PNG stays the evidence image and roof lines/saves never use this view.
// The Python CLI (scripts.ml.roof_line_fit --image) is the fallback only when sharp throws.
async function roofDetail(filename: string, version: string, signal: AbortSignal | undefined): Promise<Buffer> {
  const bytes = await readFile(filename);
  try {
    const sharp = (await import("sharp")).default;
    const meta = await sharp(bytes).metadata();
    const side = Math.min(meta.width ?? 0, meta.height ?? 0);
    // libvips needs the window to fit inside the image (tiny test images); every saved scene is >= 512 px.
    const window = Math.max(2, Math.min(side, Math.round(side / DETAIL_TILES)));
    // toColourspace("b-w") makes the PNG a real one-channel gray (a third of the bytes); greyscale() alone keeps three.
    return await sharp(bytes).greyscale().toColourspace("b-w").clahe({ width: window, height: window, maxSlope: DETAIL_MAX_SLOPE }).png().toBuffer();
  } catch {
    const { stdout } = await execute(path.join(process.cwd(), "scripts/ml/.venv/bin/python"),
      ["-m", "scripts.ml.roof_line_fit", "--image", filename, "--sha256", version],
      { cwd: process.cwd(), encoding: "buffer", timeout: 10000, maxBuffer: 8 * 1024 * 1024, signal });
    return stdout;
  }
}

// A downscaled crop of the saved scene PNG for thumbnails and inspector previews. The crop
// is a display convenience only: the saved PNG remains the evidence image and geometry is
// never resampled (callers draw outlines in scene pixel coordinates over the crop box).
async function cropView(id: string, version: string, params: URLSearchParams) {
  const integer = (key: string, max: number) => {
    const value = Number(params.get(key));
    return Number.isInteger(value) && value >= 0 && value <= max ? value : null;
  };
  const x = integer("x", 65535), y = integer("y", 65535), w = integer("w", 4096), h = integer("h", 4096), size = integer("size", CROP_MAX_SIZE);
  if (x === null || y === null || w === null || h === null || size === null || w < 8 || h < 8 || size < 16)
    return new Response("Invalid crop", { status: 400 });
  const bytes = await readFile(path.join(scenePath(id), `${version}.png`));
  const sharp = (await import("sharp")).default;
  const image = sharp(bytes);
  const meta = await image.metadata();
  if (!meta.width || !meta.height || x + w > meta.width || y + h > meta.height)
    return new Response("Crop outside the image", { status: 400 });
  const scale = Math.min(1, size / Math.max(w, h));
  const out = await image.extract({ left: x, top: y, width: w, height: h })
    .resize({ width: Math.max(1, Math.round(w * scale)), height: Math.max(1, Math.round(h * scale)), fit: "fill" })
    .jpeg({ quality: 82, mozjpeg: true }).toBuffer();
  return new Response(new Uint8Array(out), {
    headers: { "Content-Type": "image/jpeg", "Cache-Control": "public,max-age=31536000,immutable", "X-Image-Role": "display-crop" },
  });
}
const MULTIDATE_ASSET = "enhanced-median-rgb.png";
const PAIR_ASSET = "enhanced-pair-rgb.png";
// A display/review composite from scripts/ml/scene_local_stack (per-channel median over the saved RGB and the
// registered dates). Not an evidence image: no resolution increase, no inpainting; the saved PNG stays the gate.
const MULTIDATE_ROLE = "display-only; not an evidence image; no resolution increase; no inpainting";
// Aligned dates and two-view composites are inspection views only ("display-view"): outlines and roof lines stay
// in saved-image pixel coordinates and every save still uses the original PNG.
const STACK_VIEW_ROLE = "display-view";
const STACK_VIEW_STATUSES = new Set(["pair", "available"]);
type StackReport = { scene_id?: string; status?: string; source_image_sha256?: string; assets?: Record<string, { path?: string; sha256?: string }> };

// Loads a scene_local_stack report bound to this scene and its current saved PNG; null when the binding fails.
async function stackReport(id: string, version: string, stack: string, statuses: Set<string>): Promise<{ folder: string; report: StackReport } | null> {
  const folder = path.join(process.cwd(), "data/roof-review-loop/multidate/stacks", stack);
  const report: StackReport = JSON.parse(await readFile(path.join(folder, "report.json"), "utf8"));
  const scene = JSON.parse(await readFile(path.join(scenePath(id), "latest.json"), "utf8"));
  if (report?.scene_id !== id || !statuses.has(report?.status ?? "") || report?.source_image_sha256 !== version || scene?.image_sha256 !== version) return null;
  return { folder, report };
}
// Streams one report-listed asset whose bytes hash to the recorded sha256; immutable cache keyed by that sha.
async function stackAsset(folder: string, report: StackReport, name: string, role: string): Promise<Response> {
  const asset = report.assets?.[name];
  if (asset?.path !== name || !SHA256.test(asset?.sha256 ?? "")) return new Response("Image unavailable", { status: 404 });
  const bytes = await readFile(path.join(folder, name));
  if (createHash("sha256").update(bytes).digest("hex") !== asset.sha256) return new Response("Image unavailable", { status: 404 });
  return new Response(new Uint8Array(bytes), {
    headers: { "Content-Type": "image/png", "Cache-Control": "public,max-age=31536000,immutable", ETag: `"${asset.sha256}"`, "X-Image-Role": role },
  });
}
async function multidateMedian(id: string, version: string, stack: string): Promise<Response> {
  if (!/^[a-zA-Z0-9_-]{1,100}$/.test(id)) return new Response("Invalid scene id", { status: 400 });
  if (!STACK_ID.test(stack)) return new Response("Invalid stack id", { status: 400 });
  const bound = await stackReport(id, version, stack, new Set(["available"]));
  if (!bound) return new Response("Image unavailable", { status: 404 });
  return stackAsset(bound.folder, bound.report, MULTIDATE_ASSET, MULTIDATE_ROLE);
}
// view=multidate-aligned&stack=&index= streams aligned-<index>.png (one registered date on the saved grid);
// view=multidate-composite&stack= streams the pair mean or the stack median composite, whichever the report lists.
async function stackView(id: string, version: string, view: string, params: URLSearchParams): Promise<Response> {
  if (!/^[a-zA-Z0-9_-]{1,100}$/.test(id)) return new Response("Invalid scene id", { status: 400 });
  const stack = params.get("stack") || "";
  if (!STACK_ID.test(stack)) return new Response("Invalid stack id", { status: 400 });
  let name = "";
  if (view === ALIGNED_VIEW) {
    const raw = params.get("index");
    const index = raw !== null && /^\d{1,2}$/.test(raw) ? Number(raw) : null;
    if (index === null) return new Response("Invalid aligned index", { status: 400 });
    name = `aligned-${index}.png`;
  }
  const bound = await stackReport(id, version, stack, STACK_VIEW_STATUSES);
  if (!bound) return new Response("Image unavailable", { status: 404 });
  if (view === COMPOSITE_VIEW) name = bound.report.assets?.[MULTIDATE_ASSET] ? MULTIDATE_ASSET : PAIR_ASSET;
  return stackAsset(bound.folder, bound.report, name, STACK_VIEW_ROLE);
}
export async function GET(request: NextRequest) {
  try {
    const id = request.nextUrl.searchParams.get("id") || "",
      version = request.nextUrl.searchParams.get("version") || "";
    if (!SHA256.test(version))
      return new Response("Invalid image version", { status: 400 });
    const view = request.nextUrl.searchParams.get("view");
    if (view === MULTIDATE_VIEW) return await multidateMedian(id, version, request.nextUrl.searchParams.get("stack") || "");
    if (view === ALIGNED_VIEW || view === COMPOSITE_VIEW) return await stackView(id, version, view, request.nextUrl.searchParams);
    if (view === CROP_VIEW) return await cropView(id, version, request.nextUrl.searchParams);
    if (view && view !== DETAIL_VIEW) return new Response("Unknown image view", {status:400});
    const filename = path.join(scenePath(id), `${version}.png`);
    let bytes: Buffer;
    if (view) {
      const key = `${id}:${version}:${view}`;
      const cached = detailImages.get(key);
      if (cached) bytes = cached;
      else {
        // Concurrent requests for the same view (the editor prefetches, then the <image> loads) share one render.
        let pending = detailPending.get(key);
        if (!pending) {
          pending = roofDetail(filename, version, request.signal).finally(() => detailPending.delete(key));
          detailPending.set(key, pending);
        }
        bytes = await pending;
        if (detailImages.size >= 8) detailImages.delete(detailImages.keys().next().value!);
        detailImages.set(key, bytes);
      }
    } else bytes = await readFile(filename);
    return new Response(
      new Uint8Array(bytes),
      {
        headers: {
          "Content-Type": "image/png",
          "Cache-Control": "public,max-age=31536000,immutable",
        },
      },
    );
  } catch {
    return new Response("Image unavailable", { status: 404 });
  }
}
