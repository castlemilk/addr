import { NextRequest, NextResponse } from "next/server";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { objectScope, scenePath, scenePrincipal, sceneReviewTransaction } from "@/lib/scene-store";
import { ReviewError } from "@/lib/review-feedback";
import { patchedObjectSplit, type ObjectSplitPreview } from "@/lib/object-split";
export const dynamic = "force-dynamic";

async function verifiedPreview(previewId: string, signal: AbortSignal): Promise<ObjectSplitPreview> {
  return new Promise((resolve, reject) => {
    const child = spawn(path.join(process.cwd(), "scripts/ml/.venv/bin/python"), ["-m", "scripts.ml.scene_object_split", "verify"], { cwd: process.cwd(), signal });
    let stdout = "";
    const timer = setTimeout(() => { child.kill(); reject(new ReviewError("Split verification timed out. Reload before retrying.", 503)); }, 10000);
    child.stdout.on("data", chunk => { stdout += chunk.toString(); if (stdout.length > 2*1024*1024) { child.kill(); reject(new ReviewError("The split preview is too large.", 422)); } });
    child.stderr.resume(); child.stdin.on("error", () => {});
    child.on("error", error => { clearTimeout(timer); reject(error); });
    child.on("close", code => {
      clearTimeout(timer);
      try { const result = JSON.parse(stdout); if (code !== 0) reject(new ReviewError(result.error || "The split preview could not be verified.", 409)); else resolve(result); }
      catch { reject(new ReviewError("The split preview could not be verified. Request a fresh preview.", 409)); }
    });
    child.stdin.end(JSON.stringify({ preview_id: previewId }));
  });
}

/** Only the exact immutable preview can be committed; no caller supplies children or approval fields. */
export async function POST(request: NextRequest) {
  try {
    const principal = await scenePrincipal(request);
    let body: unknown;
    try { body = await request.json(); } catch { throw new ReviewError("Use a valid JSON split preview request."); }
    if (!body || typeof body !== "object" || Array.isArray(body) || Object.keys(body).join() !== "preview_id" ||
        typeof (body as { preview_id?: unknown }).preview_id !== "string" || !/^[a-f0-9]{64}$/.test((body as { preview_id: string }).preview_id))
      throw new ReviewError("Supply only the immutable preview_id returned by preview_object_split.");
    const previewId = (body as { preview_id: string }).preview_id;
    // First bind the scene, then reverify all preview/source/review bytes under its canonical save lock.
    const initial = await verifiedPreview(previewId, request.signal);
    const binding = initial.content.binding;
    const review = await sceneReviewTransaction(binding.scene_id, principal, async (data, commit) => {
      const preview = await verifiedPreview(previewId, request.signal);
      if (JSON.stringify(preview) !== JSON.stringify(initial)) throw new ReviewError("The split preview changed while saving.", 409);
      const source = (data.review?.objects || data.model.objects).find(object => object.id === binding.object_id);
      if (!source) throw new ReviewError("The source object no longer exists.", 404);
      if (!principal.scopes.includes(objectScope(source))) throw new ReviewError("Editing access for this object class is required.", 403);
      const draft = patchedObjectSplit(data, preview, principal.principal);
      const folder = scenePath(binding.scene_id);
      const [modelBytes, imageBytes] = await Promise.all([readFile(path.join(folder, "latest.json")), readFile(path.join(folder, `${binding.image_sha256}.png`))]);
      if (createHash("sha256").update(modelBytes).digest("hex") !== binding.source_model_sha256 || createHash("sha256").update(imageBytes).digest("hex") !== binding.image_sha256)
        throw new ReviewError("Source model or PNG bytes changed during split verification. Inspect a fresh preview.", 409);
      return commit(draft, { feedbackImport: true });
    });
    const childIds = initial.content.children.map(child => child.object_id);
    return NextResponse.json({ schema_version: 1, preview_id: previewId, scene_id: binding.scene_id, object_id: binding.object_id,
      revision: review.revision, model_signature: review.model_signature, image_sha256: binding.image_sha256,
      source_object: review.objects.find(object => object.id === binding.object_id),
      children: childIds.map(id => review.objects.find(object => object.id === id)), group: initial.content.group,
      updated_at: review.updated_at, updated_by: review.updated_by, human_approval: false, training_eligible: false,
      note: "Saved atomically as unchecked same-class T3 split children; source coverage and reciprocal lineage retained. Grouping is flat, visibility unassessed and all seam/subtype hypotheses unapproved." },
    { headers: { "Cache-Control": "no-store" } });
  } catch (error) {
    return NextResponse.json({ error: error instanceof Error ? error.message : "Could not save the split draft. Reload before retrying." }, { status: error instanceof ReviewError ? error.status : 500 });
  }
}
