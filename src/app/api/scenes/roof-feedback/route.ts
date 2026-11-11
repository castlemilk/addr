import { NextRequest, NextResponse } from "next/server";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { loadScene, scenePath, scenePrincipal, sceneReviewTransaction } from "@/lib/scene-store";
import { ReviewError } from "@/lib/review-feedback";
import { automatedPolygonEditIssue, parseRoofFeedbackPatch, patchedRoofFeedback } from "@/lib/roof-feedback-patch";
import { currentRoofLineEdits, estimatedLineGraph, roofLineChanges, roofLineEditsIssue, type RoofLineGraph } from "@/lib/roof-line-edits";
import { transformPoint } from "@/lib/scene-types";
import { hasConventionalRoof } from "@/lib/scene-structure";
export const dynamic = "force-dynamic";

/** A read-only, revisioned handoff of observations, never implicit training labels. */
export async function GET(request: NextRequest) {
  try {
    const principal=await scenePrincipal(request);
    if(!principal.scopes.includes("crm.read"))throw new ReviewError("Roof feedback reading access is required.",403);
    const id=request.nextUrl.searchParams.get("id"),objectId=request.nextUrl.searchParams.get("object");
    if(!id)throw new ReviewError("Choose a scene id.");
    const data=await loadScene(id),review=data.review;
    if(data.stale)throw new ReviewError("The scene model changed. Recover the saved feedback before loading it as current roof constraints.",409);
    const includeModel=!review&&(!objectId||request.nextUrl.searchParams.get("include_model")==="true");
    if(objectId&&!(review?.objects||(includeModel?data.model.objects:[])).some(o=>o.id===objectId))throw new ReviewError("This object has no saved review yet.",404);
    const graph=(g:RoofLineGraph)=>({vertices_meters:g.vertices,vertices_pixels:g.vertices.map(p=>transformPoint(p,data.model.meters_to_pixel)),edges:g.edges});
    const objects=(review?.objects||(includeModel?data.model.objects:[])).filter(o=>o.kind==="building"&&(objectId?o.id===objectId:includeModel||!!o.roof_line_edits)).map(o=>{
      const saved=o.roof_line_edits,current=currentRoofLineEdits(o),original=data.model.objects.find(item=>item.id===o.id);
      const conventional=hasConventionalRoof(o);
      const invalid=saved?roofLineEditsIssue(o,data.model.pixel_to_meters,data.model.image_width,data.model.image_height):null;
      const usable=conventional&&!!current&&!data.stale&&!invalid&&!['rejected','superseded'].includes(o.status);
      const polygonEditIssue=automatedPolygonEditIssue(data,o);
      return {object_id:o.id,label:o.label,structure_kind:o.structure_kind||null,conventional_roof_supported:conventional,status:o.status,visibility:o.visibility||"unassessed",outline_pixels:{ring:o.ring,holes:o.holes||[]},
        geometry_reviewed:o.geometry_reviewed,height_verified:o.model.height_verified,notes:o.notes||"",source:o.source,
        provenance:(o as typeof o & {provenance?:unknown}).provenance||null,automated_polygon_edit_allowed:!polygonEditIssue,automated_polygon_edit_reason:polygonEditIssue,
        is_saved_review:!!review,
        feedback_status:!conventional?"unsupported-structure":!saved?"not-saved":data.stale?"stale-model":!current?"stale-outline":invalid?"invalid":saved.checked?"checked":"draft",
        usable_as_review_constraints:usable,training_eligible:false,
        feedback:saved?{...graph(saved),checked:saved.checked,notes:saved.notes,outline_pixels:saved.outline,starting_estimate:graph(saved.baseline),changes:roofLineChanges(saved.baseline,saved)}:null,
        original_prediction:original?graph(estimatedLineGraph(original)):null,
        saved_3d_estimate:conventional?o.roof_annotation||null:null,
        guidance:!conventional?"This fabric shade sail has no conventional roof model. Retained line data is historical; use object boundary tools instead.":usable?"Use these visible XY lines as reviewer constraints. Refit and independently audit 3D geometry; preserve this feedback. Export/training needs separate eligibility checks.":"Feedback is absent, stale, invalid or inactive; do not apply it to the current roof."};
    });
    return NextResponse.json({schema_version:1,scene_id:id,revision:review?.revision||0,updated_at:review?.updated_at||null,updated_by:review?.updated_by||null,
      model_signature:data.model.model_signature,review_model_signature:review?.model_signature||null,stale:data.stale,
      image:{url:data.image_url,sha256:data.model.image_sha256,width:data.model.image_width,height:data.model.image_height},
      coordinates:{units:"metres",axes:["east","north"],origin:data.model.centroid,pixel_to_meters:data.model.pixel_to_meters,meters_to_pixel:data.model.meters_to_pixel},
      objects,note:includeModel?"No saved review exists. Rows expose original model objects for source binding, with no saved feedback or training authority.":"Saved feedback only; unsaved browser drafts are not included. XY observations do not verify heights, roof faces or building compliance."}, {headers:{"Cache-Control":"no-store"}});
  }catch(error){return NextResponse.json({error:error instanceof Error?error.message:"Could not load roof feedback."},{status:error instanceof ReviewError?error.status:500});}
}

/** A bounded draft update uses the same lock, permissions, history and revision checks as the editor. */
export async function PATCH(request: NextRequest) {
  try {
    const principal = await scenePrincipal(request);
    if (!principal.scopes.includes("crm.roof.write")) throw new ReviewError("Roof editing access is required.", 403);
    let body: unknown;
    try { body = await request.json(); } catch { throw new ReviewError("Use a valid JSON roof feedback patch."); }
    const patch = parseRoofFeedbackPatch(body);
    const review = await sceneReviewTransaction(patch.scene_id, principal, async (data, commit) => {
      const draft = patchedRoofFeedback(data, patch, principal.principal);
      let image: Buffer;
      try { image = await readFile(path.join(scenePath(patch.scene_id), `${patch.image_sha256}.png`)); }
      catch { throw new ReviewError("The bound source image is unavailable. Reload before editing.", 409); }
      if (createHash("sha256").update(image).digest("hex") !== patch.image_sha256)
        throw new ReviewError("The source image no longer matches its saved identity. Editing is blocked.", 409);
      // A draft update must never accept pending Fieldwork imports on another object.
      return commit(draft, { feedbackImport: true });
    });
    return NextResponse.json({ schema_version: 1, scene_id: patch.scene_id, object_id: patch.object_id, revision: review.revision,
      model_signature: review.model_signature, image_sha256: patch.image_sha256, updated_at: review.updated_at, updated_by: review.updated_by,
      object: review.objects.find(object => object.id === patch.object_id), training_eligible: false, human_approval: false,
      note: "Saved as a draft. Polygon, roof issues and line edits reopen review; human checks and export eligibility remain separate." }, { headers: { "Cache-Control": "no-store" } });
  } catch (error) {
    return NextResponse.json({ error: error instanceof Error ? error.message : "Could not update roof feedback." }, { status: error instanceof ReviewError ? error.status : 500 });
  }
}
