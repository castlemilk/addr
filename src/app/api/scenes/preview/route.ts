import { NextRequest, NextResponse } from "next/server";
import { readFile, stat } from "node:fs/promises";
import path from "node:path";
import { scenePath } from "@/lib/scene-store";
import type { SceneModelSnapshot } from "@/lib/scene-types";

export const dynamic = "force-dynamic";
const models = new Map<string, { stamp: number; model: SceneModelSnapshot }>();

export async function GET(request: NextRequest) {
  const p = request.nextUrl.searchParams;
  if (!/^[\w-]{1,160}$/.test(p.get("id") || "") || !/^[\w-]{1,160}$/.test(p.get("object") || "")
    || !/^[a-f0-9]{64}$/.test(p.get("version") || "")) {
    return NextResponse.json({ error: "Invalid preview identity" }, { status: 400 });
  }
  try {
    const id = p.get("id")!, file = path.join(scenePath(id), "latest.json"), stamp = (await stat(file)).mtimeMs;
    let model = models.get(id)?.stamp === stamp ? models.get(id)!.model : undefined;
    if (!model) {
      model = JSON.parse(await readFile(file, "utf8")) as SceneModelSnapshot;
      if (models.size >= 40) models.delete(models.keys().next().value!);
      models.set(id, { stamp, model });
    }
    if (model.model_signature !== p.get("version")) return NextResponse.json({ error: "Model changed. Refresh the inventory." }, { status: 409 });
    const obj = model.objects.find(o => o.id === p.get("object") && o.kind === "building");
    if (!obj) return NextResponse.json({ error: "Roof unavailable" }, { status: 404 });
    // Only the selected model object is sent; review edits are never substituted.
    const scene: SceneModelSnapshot = {
      schema_version: model.schema_version, id, address_id: null, cohort: model.cohort, title: model.title,
      centroid: model.centroid, image_sha256: model.image_sha256, image_width: model.image_width, image_height: model.image_height,
      pixel_to_meters: model.pixel_to_meters, meters_to_pixel: model.meters_to_pixel, model_signature: model.model_signature,
      objects: [obj], groups: [], scans: {}, height_note: model.height_note,
    };
    // Thumbnail crop around the roof (25% padding, square-ish, clamped to the image) so the
    // table never downloads whole 2-3 MB scene images. Outlines are still drawn in scene pixels.
    const xs = obj.ring.map(p => p[0]), ys = obj.ring.map(p => p[1]);
    const span = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys)) * 1.25 + 16;
    const cx = (Math.min(...xs) + Math.max(...xs)) / 2, cy = (Math.min(...ys) + Math.max(...ys)) / 2;
    const crop = {
      x: Math.max(0, Math.floor(cx - span / 2)), y: Math.max(0, Math.floor(cy - span / 2)),
      w: 0, h: 0,
    };
    crop.w = Math.max(8, Math.min(model.image_width - crop.x, Math.ceil(span)));
    crop.h = Math.max(8, Math.min(model.image_height - crop.y, Math.ceil(span)));
    const cropUrl = (size: number) => `/api/scenes/image?id=${encodeURIComponent(id)}&version=${model.image_sha256}&view=crop-v1&x=${crop.x}&y=${crop.y}&w=${crop.w}&h=${crop.h}&size=${size}`;
    return NextResponse.json({ scene, image_url: `/api/scenes/image?id=${encodeURIComponent(id)}&version=${model.image_sha256}`,
                               crop, crop_url: cropUrl(192), detail_crop_url: cropUrl(512) },
      { headers: { "Cache-Control": "private, max-age=30" } });
  } catch {
    return NextResponse.json({ error: "Preview unavailable. Refresh or open scene review." }, { status: 404 });
  }
}
