import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { randomUUID } from "node:crypto";

import { REVIEW_STATUSES, REVIEW_ISSUES, type ReviewStatus, type ReviewFeedback } from "./review-types";
import { validAnnotation, type RoofObject } from "./roof-annotations";
export type { ReviewFeedback } from "./review-types";
export class ReviewError extends Error {
  constructor(message: string, public status = 400) { super(message); }
}
export const validCohort = (name: string) => typeof name === "string" && /^[a-zA-Z0-9_-]{1,80}$/.test(name);
export function boundarySignature(record: Record<string, unknown>): string {
  const centroid = record.centroid as { lat: number; lon: number } | undefined;
  const ring = record.ring_meters as number[][] | undefined;
  if (!centroid || !ring?.length) return "";
  return `${centroid.lon.toFixed(7)},${centroid.lat.toFixed(7)}|${ring.map(p => `${p[0].toFixed(2)},${p[1].toFixed(2)}`).join(";")}`;
}
export function parseFeedback(body: Record<string, unknown>): ReviewFeedback {
  if (!REVIEW_STATUSES.includes(body.status as ReviewStatus)) throw new ReviewError("Choose a valid review status.");
  if (typeof body.notes !== "string" || body.notes.length > 5000) throw new ReviewError("Notes must be at most 5,000 characters.");
  if (typeof body.boundary_verified !== "boolean") throw new ReviewError("Specify whether the boundary was checked.");
  if (!Array.isArray(body.issues) || body.issues.some(x => !REVIEW_ISSUES.includes(x))) throw new ReviewError("Choose valid review issues.");
  if (!Number.isSafeInteger(body.revision) || Number(body.revision) < 0) throw new ReviewError("Reload this review before saving.");
  if (!Array.isArray(body.rejected_cuts) || body.rejected_cuts.length > 500 || body.rejected_cuts.some(cut =>
    !Array.isArray(cut) || cut.length !== 2 || cut.some(p => !Array.isArray(p) || p.length !== 2 || p.some(v => typeof v !== "number" || !Number.isFinite(v) || Math.abs(v) > 10000))
  )) throw new ReviewError("Rejected cuts must contain two finite points in metres.");
  if (body.status === "approved" && (!body.boundary_verified || body.rejected_cuts.length || body.issues.length)) {
    throw new ReviewError("Check the boundary and resolve flagged issues and rejected cuts before approving.");
  }
  if (body.annotation != null && !validAnnotation(body.annotation)) throw new ReviewError("Invalid roof annotation. Reload the model before editing.");
  if (body.objects !== undefined && (!Array.isArray(body.objects) || body.objects.length>200 || body.objects.some((o: RoofObject)=>
    !o || typeof o.id!=="string" || o.id.length>100 || !["solar_panel","skylight","chimney"].includes(o.kind) || !["candidate","confirmed","rejected"].includes(o.status)
    || typeof o.source!=="string" || o.source.length>100 || !Array.isArray(o.ring_meters) || o.ring_meters.length<3 || o.ring_meters.length>150
    || o.ring_meters.some(p=>!Array.isArray(p)||p.length!==2||p.some(v=>typeof v!=="number"||!Number.isFinite(v)||Math.abs(v)>10000))))) throw new ReviewError("Invalid object annotation.");
  if ((body.annotation || body.objects) && (typeof body.model_signature!=="string" || !/^[a-f0-9]{64}$/.test(body.model_signature))) throw new ReviewError("Reload the model before saving annotations.");
  if (body.object_reviewed_classes !== undefined && (!Array.isArray(body.object_reviewed_classes) || body.object_reviewed_classes.some(k => !["solar_panel","skylight","chimney"].includes(k)))) throw new ReviewError("Choose valid reviewed object classes.");
  return { status: body.status as ReviewStatus, notes: body.notes, boundary_verified: body.boundary_verified,
    ...(body.annotation !== undefined ? { annotation: body.annotation as ReviewFeedback["annotation"] } : {}),
    ...(body.objects !== undefined ? {objects: body.objects as RoofObject[]} : {}),
    ...(typeof body.model_signature === "string" ? {model_signature:body.model_signature} : {}),
    ...(Array.isArray(body.object_reviewed_classes) ? {object_reviewed_classes:[...new Set(body.object_reviewed_classes)] as RoofObject["kind"][]} : {}),
    apex_verified: body.apex_verified === true, roof_form_verified: body.roof_form_verified === true, objects_verified: body.objects_verified === true,
    rejected_cuts: body.rejected_cuts, issues: [...new Set(body.issues)] as string[], revision: Number(body.revision) };
}
export async function readFeedback(file: string): Promise<Record<string, ReviewFeedback>> {
  try {
    const store = JSON.parse(await readFile(file, "utf8"));
    if (!store || typeof store !== "object" || Array.isArray(store)) throw new Error("Invalid feedback store");
    return store;
  }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return {};
    throw new ReviewError("Review storage could not be read. Existing reviews have been preserved.", 503);
  }
}
export function feedbackFor(store: Record<string, ReviewFeedback>, cohort: string, id: number): ReviewFeedback | undefined {
  return store[`${cohort}:${id}`] ?? (cohort === "suburban_benchmark" ? store[String(id)] : undefined);
}
export async function saveFeedback(file: string, cohort: string, id: number, feedback: ReviewFeedback, principal: string): Promise<ReviewFeedback> {
  await mkdir(path.dirname(file), { recursive: true });
  const lock = `${file}.lock`;
  try { await mkdir(lock); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === "EEXIST") throw new ReviewError("Another review is being saved. Retry in a moment.", 503);
    throw error;
  }
  const temporary = `${file}.${randomUUID()}.tmp`;
  try {
    const store = await readFeedback(file);
    const current = feedbackFor(store, cohort, id);
    if ((current?.revision ?? 0) !== feedback.revision) throw new ReviewError("This review changed in another session. Reload before saving; your draft is still here.", 409);
    const {history = [], ...previous} = current || {};
    const saved = { ...current, ...feedback, history: current ? [...history, previous as Omit<ReviewFeedback,"history">] : [], revision: feedback.revision + 1, updated_at: new Date().toISOString(), updated_by: principal };
    store[`${cohort}:${id}`] = saved;
    await writeFile(temporary, JSON.stringify(store, null, 2), "utf8");
    await rename(temporary, file);
    return saved;
  } finally {
    await rm(temporary, { force: true });
    await rm(lock, { recursive: true, force: true });
  }
}
