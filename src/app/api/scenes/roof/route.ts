import { NextRequest, NextResponse } from "next/server";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import path from "node:path";
import { loadScene, scenePath, scenePrincipal, validateSceneReview } from "@/lib/scene-store";
import { ReviewError } from "@/lib/review-feedback";
import { transformPoint } from "@/lib/scene-types";
import { validAnnotation } from "@/lib/roof-annotations";
import { hasConventionalRoof } from "@/lib/scene-structure";
const execute = promisify(execFile);
export async function POST(request: NextRequest) {
  try {
    const principal = await scenePrincipal(request);
    if (!principal.scopes.includes("crm.roof.write")) throw new ReviewError("Roof editing access is required.", 403);
    const body = await request.json();
    if (!body || !["auto", "gabled", "hipped", "flat"].includes(body.form) ||
        typeof body.rise !== "number" || !Number.isFinite(body.rise) || body.rise < .1 || body.rise > 30 ||
        body.object?.kind !== "building") throw new ReviewError("Choose a building, roof form and rise from 0.1 to 30 metres.");
    if (!hasConventionalRoof(body.object)) throw new ReviewError("Fabric shade sails have no conventional roof proposal. Edit their visible boundary instead.");
    const data = await loadScene(body.id);
    if (data.stale || body.model_signature !== data.model.model_signature || body.revision !== (data.review?.revision || 0))
      throw new ReviewError("This scene changed. Reload or recover its edits before proposing a roof.", 409);
    const savedObject = (data.review?.objects || data.model.objects).find(o => o.id === body.object.id);
    if (savedObject && !hasConventionalRoof(savedObject))
      throw new ReviewError("This saved object has no conventional roof. Save its corrected structure kind before proposing one.");
    const object = validateSceneReview({ revision: body.revision, model_signature: body.model_signature,
      objects: [{...body.object, parent_id: undefined, group_id: undefined, derived_from: undefined, replacement_ids: undefined}], groups: [], reviewed_classes: [] }, data.model).objects[0];
    if (!/^[a-f0-9]{64}$/.test(data.model.image_sha256)) throw new ReviewError("The source image could not be verified.", 503);
    const payload = { ring: object.ring.map(p => transformPoint(p, data.model.pixel_to_meters)), eave: object.model.height_m,
      holes: object.holes?.map(h=>h.map(p=>transformPoint(p,data.model.pixel_to_meters))),
      rise: body.rise, form: body.form, image_path: path.join(scenePath(body.id), `${data.model.image_sha256}.png`),
      image_sha256: data.model.image_sha256, meters_to_pixel: data.model.meters_to_pixel };
    const { stdout } = await execute(path.join(process.cwd(), "scripts/ml/.venv/bin/python"),
      ["-m", "scripts.ml.scene_roof", "--request", JSON.stringify(payload)],
      { cwd: process.cwd(), timeout: 20000, maxBuffer: 1024 * 1024, signal: request.signal });
    const proposal = JSON.parse(stdout);
    if (!validAnnotation(proposal.annotation) || !proposal.audit?.passed) throw new ReviewError("The roof proposal did not pass geometry checks.", 422);
    return NextResponse.json({ proposal });
  } catch (error) {
    let issue = error instanceof ReviewError ? error : new ReviewError("A roof proposal could not be made. Your outline and edits are preserved; try a simpler outline or a different form.", 422);
    const failed = error as { code?: number; stdout?: string };
    if (failed.code === 2 && failed.stdout) {
      try { const result = JSON.parse(failed.stdout); if (typeof result.error === "string") issue = new ReviewError(result.error, 422); } catch { /* Preserve the useful fallback. */ }
    }
    return NextResponse.json({ error: issue.message }, { status: issue.status });
  }
}
