import { NextRequest, NextResponse } from "next/server";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { loadScene, objectScope, scenePath, scenePrincipal, sceneReviewTransaction } from "@/lib/scene-store";
import { ReviewError } from "@/lib/review-feedback";
import { automatedPolygonEditIssue, parseObjectPolygonPatch, patchedObjectPolygon } from "@/lib/roof-feedback-patch";
export const dynamic = "force-dynamic";

/** Current saved/source polygons of any scene class, without approval or linework authority. */
export async function GET(request: NextRequest) {
  try {
    const principal = await scenePrincipal(request);
    if (!principal.scopes.includes("crm.read")) throw new ReviewError("Object feedback reading access is required.", 403);
    const id = request.nextUrl.searchParams.get("id"), objectId = request.nextUrl.searchParams.get("object");
    if (!id) throw new ReviewError("Choose a scene id.");
    const data = await loadScene(id), review = data.review;
    if (data.stale) throw new ReviewError("The scene model changed. Recover saved feedback before tracing its objects.", 409);
    const source = review?.objects || data.model.objects;
    if (objectId && !source.some(object => object.id === objectId)) throw new ReviewError("This object does not exist in the current scene.", 404);
    const objects = source.filter(object => !objectId || object.id === objectId).map(object => {
      const issue = automatedPolygonEditIssue(data, object);
      return { object_id: object.id, kind: object.kind, label: object.label, status: object.status,
        visibility: object.visibility || "unassessed", outline_pixels: { ring: object.ring, holes: object.holes || [] },
        geometry_reviewed: object.geometry_reviewed, height_verified: object.model.height_verified,
        notes: object.notes || "", source: object.source,
        provenance: (object as typeof object & { provenance?: unknown }).provenance || null,
        automated_polygon_edit_allowed: !issue, automated_polygon_edit_reason: issue,
        is_saved_review: !!review, training_eligible: false, human_approval: false };
    });
    return NextResponse.json({ schema_version: 1, scene_id: id, revision: review?.revision || 0,
      updated_at: review?.updated_at || null, updated_by: review?.updated_by || null,
      model_signature: data.model.model_signature, review_model_signature: review?.model_signature || null, stale: data.stale,
      image: { url: data.image_url, sha256: data.model.image_sha256, width: data.model.image_width, height: data.model.image_height },
      coordinates: { units: "metres", axes: ["east", "north"], origin: data.model.centroid,
        pixel_to_meters: data.model.pixel_to_meters, meters_to_pixel: data.model.meters_to_pixel }, objects,
      note: "Current saved polygons, or original model objects if no review exists. Agent drafts do not approve geometry, class, heights or training labels." },
    { headers: { "Cache-Control": "no-store" } });
  } catch (error) {
    return NextResponse.json({ error: error instanceof Error ? error.message : "Could not load object feedback." }, { status: error instanceof ReviewError ? error.status : 500 });
  }
}

/** Save only a guarded, source-bound unchecked polygon draft through the editor transaction. */
export async function PATCH(request: NextRequest) {
  try {
    const principal = await scenePrincipal(request);
    let body: unknown;
    try { body = await request.json(); } catch { throw new ReviewError("Use a valid JSON object polygon patch."); }
    const patch = parseObjectPolygonPatch(body);
    const review = await sceneReviewTransaction(patch.scene_id, principal, async (data, commit) => {
      const object = (data.review?.objects || data.model.objects).find(value => value.id === patch.object_id);
      if (!object) throw new ReviewError("This object does not exist in the current scene.", 404);
      const scope = objectScope(object);
      if (!principal.scopes.includes(scope)) throw new ReviewError("Editing access for this object class is required.", 403);
      const draft = patchedObjectPolygon(data, patch, principal.principal);
      let image: Buffer;
      try { image = await readFile(path.join(scenePath(patch.scene_id), `${patch.image_sha256}.png`)); }
      catch { throw new ReviewError("The bound source image is unavailable. Reload before editing.", 409); }
      if (createHash("sha256").update(image).digest("hex") !== patch.image_sha256)
        throw new ReviewError("The source image no longer matches its saved identity. Editing is blocked.", 409);
      return commit(draft, { feedbackImport: true });
    });
    return NextResponse.json({ schema_version: 1, scene_id: patch.scene_id, object_id: patch.object_id, revision: review.revision,
      model_signature: review.model_signature, image_sha256: patch.image_sha256, updated_at: review.updated_at, updated_by: review.updated_by,
      object: review.objects.find(object => object.id === patch.object_id), training_eligible: false, human_approval: false,
      note: "Saved as an unchecked machine polygon draft. Source predictions and human/imported work remain separate; reviewed class and footprint-derived estimates reopen." },
    { headers: { "Cache-Control": "no-store" } });
  } catch (error) {
    return NextResponse.json({ error: error instanceof Error ? error.message : "Could not update object feedback." }, { status: error instanceof ReviewError ? error.status : 500 });
  }
}
