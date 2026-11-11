import {
  readFile,
  writeFile,
  mkdir,
  rename,
  rm,
  readdir,
} from "node:fs/promises";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { NextRequest } from "next/server";
import { ReviewError } from "./review-feedback";
import {
  SCENE_CLASSES,
  SCENE_REVIEW_ISSUES,
  polygonIssues,
  footprintMetrics,
  type SceneModelSnapshot,
  type SceneReview,
  type SceneObject,
  type SceneData,
} from "./scene-types";
import { roofLineEditsIssue } from "./roof-line-edits";
import { validAnnotation } from "./roof-annotations";
import { validRoofFeedback } from "./scene-roof-feedback";
import { expansionOffset, recoverSceneReview } from "./scene-feedback";
import { lineageIssue } from "./scene-instances";
import { hasOutlineIssue } from "./scene-review-issues";
import { vegetationTypeIssue } from "./scene-vegetation";
import { sceneQueueFilter, sceneSummary, type SceneQueueFilter } from "./scene-queue";
import { addressNames } from "./scene-address-store";
import { propertyTitle } from "./scene-addresses";
import { acceptPendingFeedback } from "./annotations/feedback";
import { clearConventionalRoofDraft, isShadeSail } from "./scene-structure";
const root = path.join(process.cwd(), "data/scenes");
const SCENE_ID = /^[a-zA-Z0-9_-]{1,100}$/, SHA256 = /^[a-f0-9]{64}$/;
const OBJECT_STATUSES = ["candidate", "confirmed", "needs_adjustment", "rejected", "superseded"];
/**
 * The Python verifier of a recapture's frame transform (scripts.ml.scene_recapture --verify-source --verify-target): it
 * re-hashes both snapshots and images, recomputes the affine and overlap, checks the recorded design against DESIGN.json and
 * re-measures the residual from the image bytes (well under a second, about 200 MB). loadScene runs it for a recapture that
 * has no review of its own, and sceneList loads every scene, so one verdict per (source, target) signature pair is kept for
 * the process: a pass for good, a failure for a minute (a repaired file is noticed, and a broken pair does not spawn Python on
 * every list). The map is bounded; the oldest pair leaves first.
 */
const FRAME_VERDICTS = new Map<string, { verdict: Promise<void>; until: number | null }>();
const FRAME_VERDICT_LIMIT = 64, FRAME_FAILURE_TTL_MS = 60_000;
function verifyFrameTransform(sourceModelFile: string, targetModelFile: string, key: string): Promise<void> {
  const cached = FRAME_VERDICTS.get(key);
  if (cached && (cached.until === null || cached.until > Date.now())) return cached.verdict;
  FRAME_VERDICTS.delete(key);
  const entry: { verdict: Promise<void>; until: number | null } = { until: null, verdict: (async () => {
    const { stdout } = await promisify(execFile)(path.join(process.cwd(), "scripts/ml/.venv/bin/python"),
      ["-m", "scripts.ml.scene_recapture", "--verify-source", sourceModelFile, "--verify-target", targetModelFile, "--root", process.cwd()],
      { cwd: process.cwd(), timeout: 60000, maxBuffer: 1024 * 1024 });
    let verdict: unknown = null;
    try { verdict = JSON.parse(String(stdout).trim().split("\n").at(-1) || "null"); } catch { verdict = null; }
    if (!verdict || typeof verdict !== "object" || (verdict as { verified?: unknown }).verified !== true)
      throw new ReviewError("the frame transform could not be re-verified against the two images");
  })() };
  entry.verdict.catch(() => { entry.until = Date.now() + FRAME_FAILURE_TTL_MS; });
  FRAME_VERDICTS.set(key, entry);
  while (FRAME_VERDICTS.size > FRAME_VERDICT_LIMIT) FRAME_VERDICTS.delete(FRAME_VERDICTS.keys().next().value as string);
  return entry.verdict;
}
/**
 * A recapture (a scene captured --carry-from another scene, scripts/ml/scene_lot.py) starts with no review of its own. When
 * the scene its snapshot's frame_transform names has a saved review on the very snapshot the transform was measured against,
 * that review is offered re-projected through the georeferenced frame (recoverSceneReview -> scene-frame-transform.ts) after
 * the Python verifier has re-measured the transform from the image bytes. stale comes back true so the editor shows the
 * notice (page.tsx renders the recovery and its buttons only under stale); the carried review goes out at revision 0, so the
 * first save is revision 1 on the recapture's own snapshot and stale reads false afterwards (the save path keeps no object
 * confirmed on a stale scene unless the recovery kept it, and the carry keeps none). Null when this is not that case: no
 * usable frame_transform, or the source scene has no review, so the scene is a plain new one, as today. The reason names the
 * step that failed and never a file path.
 */
async function recaptureRecovery(id: string, model: SceneModelSnapshot): Promise<Pick<SceneData, "stale" | "recovery" | "recovery_reason"> | null> {
  const transform = model.frame_transform;
  if (!transform || typeof transform !== "object" || Array.isArray(transform)) return null;
  const source = transform.source_scene_id, sourceSignature = transform.source_model_signature;
  if (typeof source !== "string" || !SCENE_ID.test(source) || source === id || typeof sourceSignature !== "string" || !SHA256.test(sourceSignature)) return null;
  const refused = (cause: string): Pick<SceneData, "stale" | "recovery" | "recovery_reason"> =>
    ({ stale: true, recovery: null, recovery_reason: `Earlier decisions from ${source} could not be carried onto this image: ${cause}. They stay with the earlier scene.` });
  let sourceReview: SceneReview;
  try {
    sourceReview = JSON.parse(await readFile(path.join(root, source, "review.json"), "utf8"));
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === "ENOENT") return null;
    return refused("the source scene's saved review could not be read");
  }
  try {
    if (!sourceReview || sourceReview.model_signature !== sourceSignature)
      throw new ReviewError("the source scene's review sits on another snapshot than the one this frame was measured against");
    if (!SHA256.test(model.model_signature)) throw new ReviewError("this snapshot has no valid identity");
    const sourceModelFile = path.join(root, source, "models", `${sourceSignature}.json`);
    const sourceSnapshot = JSON.parse(await readFile(sourceModelFile, "utf8")) as SceneModelSnapshot;
    await verifyFrameTransform(sourceModelFile, path.join(root, id, "models", `${model.model_signature}.json`), `${sourceSignature}:${model.model_signature}`);
    const refusal: { reason: string | null } = { reason: null };
    const recovery = recoverSceneReview(sourceSnapshot, model, validateSceneReview(sourceReview, sourceSnapshot),
      { revision: 0, onRefuse: (reason) => { refusal.reason = reason; } });
    if (!recovery) return refused(refusal.reason ?? "the frame transform does not link the two scenes");
    recovery.review = validateSceneReview(recovery.review, model);
    return { stale: true, recovery, recovery_reason: undefined };
  } catch (e) {
    const error = e as NodeJS.ErrnoException & { killed?: boolean; stderr?: string };
    const cause = e instanceof ReviewError ? e.message
      : error.code === "ENOENT" ? "the source snapshot file is missing"
      : error.killed ? "the frame check did not finish in time"
      : error.stderr !== undefined ? "the frame transform could not be re-verified against the two images"
      : "the carried review could not be matched to this snapshot";
    return refused(cause);
  }
}
/**
 * Which basemap the reviewer looked at while saving. The AI-satellite toggle lives in
 * MapComponent (the main map), which never saves scene reviews; the scene review page
 * has no such toggle, so its saves record "unknown" unless a client sends the flag.
 * Recorded for provenance only; it never gates a review status.
 */
export const BASEMAP_MODES = ["ai-satellite", "raw", "unknown"] as const;
export type BasemapMode = (typeof BASEMAP_MODES)[number];
export function basemapMode(value: unknown): BasemapMode {
  const mode = (value as { basemap_mode?: unknown } | null)?.basemap_mode;
  return mode === "ai-satellite" || mode === "raw" ? mode : "unknown";
}
export function scenePath(id: string) {
  if (typeof id !== "string" || !/^[a-zA-Z0-9_-]{1,100}$/.test(id))
    throw new ReviewError("Choose a valid scene.");
  return path.join(root, id);
}
export async function loadScene(
  id: string,
): Promise<SceneData> {
  const directory = scenePath(id);
  let model: SceneModelSnapshot;
  try {
    model = JSON.parse(
      await readFile(path.join(directory, "latest.json"), "utf8"),
    );
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === "ENOENT")
      throw new ReviewError("This scene has not been extracted yet.", 404);
    throw e;
  }
  let review: SceneReview | null = null;
  try {
    review = JSON.parse(
      await readFile(path.join(directory, "review.json"), "utf8"),
    );
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== "ENOENT")
      throw new ReviewError(
        "The saved review could not be read. It has been preserved.",
        503,
      );
  }
  let stale = Boolean(review && review.model_signature !== model.model_signature);
  let recovery: SceneData["recovery"] = null, recovery_reason: string | undefined;
  if (stale && review) {
    try {
      if (!/^[a-f0-9]{64}$/.test(review.model_signature)) throw new Error("Invalid snapshot identity");
      const previous = JSON.parse(await readFile(path.join(directory, "models", `${review.model_signature}.json`), "utf8")) as SceneModelSnapshot;
      if (expansionOffset(previous,model)!==null) {
        if(!/^[a-f0-9]{64}$/.test(model.model_signature)) throw new Error("Invalid current snapshot identity");
        await promisify(execFile)(path.join(process.cwd(),"scripts/ml/.venv/bin/python"),["-m","scripts.ml.scene_expand",
          "--verify-previous",path.join(directory,"models",`${review.model_signature}.json`),
          "--verify-current",path.join(directory,"models",`${model.model_signature}.json`)],{cwd:process.cwd(),timeout:30000,maxBuffer:1024*1024});
      }
      recovery = recoverSceneReview(previous, model, validateSceneReview(review, previous));
      if (recovery) recovery.review = validateSceneReview(recovery.review, model);
      else recovery_reason = "The image or coordinate frame changed. Earlier edits stay in history and cannot be carried onto this image.";
    } catch (e) {
      recovery = null;
      // Name the step that failed so a reviewer (or an agent reading the API) can tell a slow pixel check from a
      // missing snapshot or an invalid saved review; the wording stays free of file paths.
      const error = e as NodeJS.ErrnoException & { killed?: boolean; stderr?: string };
      const cause = e instanceof ReviewError ? e.message
        : error.code === "ENOENT" ? "the earlier snapshot file is missing"
        : error.killed ? "the pixel check did not finish in time"
        : error.stderr !== undefined ? (String(error.stderr).includes("preserves the source pixels") ? "the widened image does not preserve the earlier pixels" : "the pixel check could not run")
        : "the saved review could not be matched to the earlier snapshot";
      recovery_reason = `The earlier model snapshot could not be verified: ${cause}. Previous edits are preserved in history.`;
    }
  } else if (review === null) {
    // A recapture without a review of its own: offer the source scene's decisions re-projected (see recaptureRecovery).
    const carried = await recaptureRecovery(id, model);
    if (carried) ({ stale, recovery, recovery_reason } = carried);
  }
  return {
    model,
    review,
    stale,
    recovery,
    recovery_reason,
    image_url: `/api/scenes/image?id=${id}&version=${model.image_sha256}`,
  };
}
export async function sceneList(filter:SceneQueueFilter=sceneQueueFilter({})) {
  let directories;
  try {
    directories = await readdir(root, { withFileTypes: true });
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === "ENOENT") return [];
    throw e;
  }
  const entries = await Promise.all(
    directories
      .filter(
        (entry) =>
          entry.isDirectory() &&
          !entry.name.startsWith("qa_") &&
          /^[a-zA-Z0-9_-]{1,100}$/.test(entry.name),
      )
      .map(async ({ name: id }) => {
        // An extraction creates its directory before publishing its first snapshot.
        let data;
        try {
          data = await loadScene(id);
        } catch (e) {
          if (e instanceof ReviewError && e.status === 404) return null;
          throw e;
        }
        const current = data.stale
          ? data.model.objects
          : data.review?.objects || data.model.objects;
        return sceneSummary(data.model,current,filter,data.stale);
      }),
  );
  const scenes=entries.filter((entry) => entry !== null);
  const names=await addressNames(scenes.flatMap(s=>s.properties.map(p=>p.address_id)));
  return scenes.map(s=>{
    const properties=s.properties.map(p=>({...p,title:names[p.address_id]||p.title}));
    return {...s,properties,property_title:propertyTitle(properties)};
  });
}
export async function scenePrincipal(request: NextRequest) {
  if (
    request.headers.get("origin") &&
    request.headers.get("origin") !== request.nextUrl.origin
  )
    throw new ReviewError("Save through this app.", 403);
  const service =
    process.env.GEO_SERVICE_URL ||
    process.env.NEXT_PUBLIC_GEO_SERVICE_URL ||
    "http://localhost:8085";
  let response: Response;
  try {
    response = await fetch(`${service}/crm.v1.CrmService/WhoAmI`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(request.headers.get("authorization")
          ? { Authorization: request.headers.get("authorization")! }
          : {}),
      },
      body: "{}",
      signal: AbortSignal.timeout(5000),
    });
  } catch {
    throw new ReviewError(
      "CRM is unavailable. Your draft is still here; retry when it reconnects.",
      503,
    );
  }
  if (!response.ok)
    throw new ReviewError(
      "Sign in through Address CRM with editing access.",
      response.status === 403 ? 403 : 401,
    );
  return response.json() as Promise<{ principal: string; scopes: string[] }>;
}
export const objectScope = (object: SceneObject) =>
  object.kind === "building"
    ? "crm.roof.write"
    : ["tree", "vegetation"].includes(object.kind)
      ? "crm.tree.write"
      : "crm.object.write";
export function validateSceneReview(
  value: SceneReview,
  model: SceneModelSnapshot,
): SceneReview {
  if (
    !value ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 0 ||
    value.model_signature !== model.model_signature
  )
    throw new ReviewError(
      "The scene model changed. Reload before saving.",
      409,
    );
  if (
    !Array.isArray(value.objects) ||
    value.objects.length > 1000 ||
    !Array.isArray(value.groups) ||
    value.groups.length > 250 ||
    !Array.isArray(value.reviewed_classes) ||
    value.reviewed_classes.some((k) => !SCENE_CLASSES.includes(k))
  )
    throw new ReviewError("Invalid scene annotations.");
  // Every client uses the same subtype contract: a fabric membrane cannot keep
  // rigid roof priors or checked hip/gable targets merely by bypassing the UI.
  value = { ...value, objects: value.objects.map(o => o && isShadeSail(o) ? clearConventionalRoofDraft(o) : o) };
  const ids = new Set<string>();
  for (const o of value.objects) {
    if (
      !o ||
      typeof o.id !== "string" ||
      !/^[a-zA-Z0-9_-]{1,100}$/.test(o.id) ||
      ids.has(o.id) ||
      !SCENE_CLASSES.includes(o.kind) ||
      typeof o.label !== "string" ||
      o.label.length > 150 ||
      typeof o.source !== "string" ||
      o.source.length > 100 ||
      typeof o.geometry_reviewed !== "boolean" ||
      !["candidate", "confirmed", "needs_adjustment", "rejected", "superseded"].includes(
        o.status,
      ) ||
      (typeof o.notes !== "undefined" &&
        (typeof o.notes !== "string" || o.notes.length > 5000)) ||
      (typeof o.structure_kind !== "undefined" &&
        (typeof o.structure_kind !== "string" || !/^[a-z][a-z-]{1,39}$/.test(o.structure_kind)))
    )
      throw new ReviewError(
        "Check object labels, identities and review status.",
      );
    ids.add(o.id);
    // A vegetation type is one of grass / hedge / shrub / tree and only on a tree or vegetation outline (scene-vegetation.ts).
    const vegetationIssue = vegetationTypeIssue(o);
    if (vegetationIssue) throw new ReviewError(`${o.label}: ${vegetationIssue}`);
    if(o.address_id !== undefined && (!Number.isSafeInteger(o.address_id)||o.address_id<=0))
      throw new ReviewError(`${o.label}: choose a valid address ID.`);
    if (o.review_issues !== undefined && (!Array.isArray(o.review_issues) ||
        new Set(o.review_issues).size !== o.review_issues.length ||
        o.review_issues.some(issue => !SCENE_REVIEW_ISSUES.includes(issue))))
      throw new ReviewError(`${o.label}: choose valid feedback flags.`);
    if (o.status === "confirmed" && hasOutlineIssue(o))
      throw new ReviewError(`${o.label}: resolve outline, object type and separation flags before confirming.`);
    if (o.review_issues?.includes("height") && o.model?.height_verified)
      throw new ReviewError(`${o.label}: a flagged height cannot be verified.`);
    if(o.visibility!==undefined && !["full","occluded","truncated","occluded_truncated"].includes(o.visibility))
      throw new ReviewError(`${o.label}: choose how much of the object is visible.`);
    // A carried object's provenance (recapture carry): the shape scene-frame-transform.ts writes and review_authority.py
    // reads; the earlier verdict is a hint, so any status is allowed there, and extra keys (the Python carry's `role`) pass.
    if (o.carried_from !== undefined) {
      const c = o.carried_from;
      if (!c || typeof c !== "object" || Array.isArray(c) || typeof c.scene_id !== "string" || !SCENE_ID.test(c.scene_id) ||
          typeof c.model_signature !== "string" || !SHA256.test(c.model_signature) || typeof c.object_id !== "string" || !SCENE_ID.test(c.object_id) ||
          !OBJECT_STATUSES.includes(c.status) || typeof c.geometry_reviewed !== "boolean" ||
          typeof c.iou_with_new !== "number" || !Number.isFinite(c.iou_with_new) || c.iou_with_new < 0 || c.iou_with_new > 1 ||
          (c.matched_id !== null && (typeof c.matched_id !== "string" || !SCENE_ID.test(c.matched_id))) || c.method !== "georeferenced-affine")
        throw new ReviewError(`${o.label}: check where this carried object came from.`);
    }
    const issues = polygonIssues(o.ring, model.image_width, model.image_height, o.holes);
    if (issues.length) throw new ReviewError(`${o.label}: ${issues[0]}`);
    if (o.status === "confirmed" && !o.geometry_reviewed)
      throw new ReviewError("Check the outline before confirming an object.");
    const m = o.model;
    if (
      !m ||
      (m.placement !== undefined && !["roof", "absolute"].includes(m.placement)) ||
      ![m.height_m, m.base_m, m.rotation_deg].every(
        (v) => typeof v === "number" && Number.isFinite(v),
      ) ||
      m.height_m < 0 ||
      m.height_m > 150 ||
      m.base_m < 0 ||
      m.base_m > 150 ||
      typeof m.height_verified !== "boolean" ||
      typeof m.height_source !== "string" ||
      m.height_source.length > 100 ||
      !/^#[0-9a-f]{6}$/i.test(m.color)
    )
      throw new ReviewError(
        `${o.label}: check its height, elevation and colour.`,
      );
    if (o.roof_annotation && !validAnnotation(o.roof_annotation))
      throw new ReviewError("Check the roof points and faces.");
    const lineIssue = roofLineEditsIssue(o, model.pixel_to_meters, model.image_width, model.image_height);
    if (lineIssue) throw new ReviewError(`${o.label}: ${lineIssue}`);
    if (!validRoofFeedback(o)) throw new ReviewError("Check the roof family and roofline feedback.");
    if(m.solar!==undefined){
      const bounded=(v:unknown,min:number,max:number)=>v===undefined||(typeof v==="number"&&Number.isFinite(v)&&v>=min&&v<=max);
      if(!m.solar||typeof m.solar!=="object"||Array.isArray(m.solar)||o.kind!=="solar_panel"||!bounded(m.solar.tilt_deg,0,80)||!bounded(m.solar.azimuth_deg,0,359.99)||
        !bounded(m.solar.clearance_m,0,3)||(m.solar.tilt_deg===undefined)!==(m.solar.azimuth_deg===undefined))
        throw new ReviewError(`${o.label}: check the solar tilt, downhill direction and clearance.`);
    }
    if(m.surface!==undefined) {
      const bounded=(v:unknown,min:number,max:number)=>v===undefined || (typeof v==="number" && Number.isFinite(v) && v>=min && v<=max);
      if(!m.surface || !["road","driveway","footpath"].includes(o.kind) || !["plane","crowned"].includes(m.surface.profile) ||
        !bounded(m.surface.heading_deg,0,359.99) || !bounded(m.surface.grade_percent,-30,30) ||
        !bounded(m.surface.crossfall_percent,m.surface.profile==="crowned"?0:-15,15))
        throw new ReviewError(`${o.label}: check the surface profile, direction, grade and crossfall.`);
    }
    if (m.car !== undefined) {
      const bounded=(v:unknown,min:number,max:number)=>v===undefined || (typeof v==="number" && Number.isFinite(v) && v>=min && v<=max);
      if(!m.car || o.kind!=="car" || !["car","suv","van"].includes(m.car.profile) ||
        !bounded(m.car.heading_deg,0,359.99) || !bounded(m.car.cabin_length_ratio,.25,.85) ||
        !bounded(m.car.cabin_width_ratio,.4,.95) || !bounded(m.car.cabin_offset_ratio,-.3,.3))
        throw new ReviewError(`${o.label}: check the vehicle shape, front direction and cabin proportions.`);
    }
    if (m.tree !== undefined && (!m.tree || o.kind !== "tree" ||
        !["rounded", "conical", "spreading"].includes(m.tree.crown_profile) ||
        typeof m.tree.crown_base_ratio !== "number" || !Number.isFinite(m.tree.crown_base_ratio) ||
        m.tree.crown_base_ratio < 0 || m.tree.crown_base_ratio > .95))
      throw new ReviewError(`${o.label}: choose a crown shape and a crown base below the tree top.`);
    if ((o.roof_geometry_source !== undefined && !["image-guided-prior", "reviewer-template"].includes(o.roof_geometry_source)) ||
        (o.roof_geometry_note !== undefined && (typeof o.roof_geometry_note !== "string" || o.roof_geometry_note.length > 2000)))
      throw new ReviewError("Check the roof proposal source and notes.");
    const point = (p: unknown) =>
      Array.isArray(p) &&
      p.length === 2 &&
      p.every(
        (v) =>
          typeof v === "number" && Number.isFinite(v) && Math.abs(v) <= 10000,
      );
    if (
      o.roof_parts !== undefined &&
      (!Array.isArray(o.roof_parts) ||
        o.roof_parts.length > 300 ||
        o.roof_parts.some((part) => {
          if (!part || typeof part !== "object") return true;
          const ring = part.ring_meters || part.ring,
            z = part.ring_z || part.z;
          return (
            !Array.isArray(ring) ||
            ring.length < 3 ||
            ring.length > 200 ||
            !ring.every(point) ||
            !Array.isArray(z) ||
            z.length !== ring.length ||
            z.some(
              (v) =>
                typeof v !== "number" ||
                !Number.isFinite(v) ||
                v < 0 ||
                v > 150,
            )
          );
        }))
    )
      throw new ReviewError("Check the roof surface geometry.");
    if (
      o.roof_lines !== undefined &&
      (!Array.isArray(o.roof_lines) ||
        o.roof_lines.length > 1500 ||
        o.roof_lines.some(
          (line) =>
            !line ||
            !Array.isArray(line.line_meters) ||
            line.line_meters.length !== 2 ||
            !line.line_meters.every(point),
        ))
    )
      throw new ReviewError("Check the roof lines.");
  }
  const lineage = lineageIssue(value.objects);
  if (lineage) throw new ReviewError(lineage);
  const groups = new Set<string>();
  for (const g of value.groups) {
    if (
      !g ||
      typeof g.id !== "string" ||
      !/^[a-zA-Z0-9_-]{1,100}$/.test(g.id) ||
      groups.has(g.id) ||
      typeof g.label !== "string" ||
      !g.label.trim() ||
      g.label.length > 150
    )
      throw new ReviewError("Use valid group names and unique identities.");
    groups.add(g.id);
  }
  for (const o of value.objects) {
    if (o.group_id && !groups.has(o.group_id))
      throw new ReviewError("Choose an existing object group.");
    let parent = o.parent_id;
    const visited = new Set([o.id]);
    while (parent) {
      if (visited.has(parent) || !ids.has(parent))
        throw new ReviewError(
          "Object parents must exist and cannot form a loop.",
        );
      visited.add(parent);
      parent = value.objects.find((p) => p.id === parent)?.parent_id;
    }
  }
  if (
    value.reviewed_classes.some((k) =>
      value.objects.some(
        (o) =>
          o.kind === k && ["candidate", "needs_adjustment"].includes(o.status),
      ),
    )
  )
    throw new ReviewError(
      "Resolve the remaining objects before completing a layer review.",
    );
  return {
    revision: value.revision,
    model_signature: model.model_signature,
    objects: value.objects.map((o) => ({
      ...o,
      model: { ...o.model, ...footprintMetrics(o.ring, model.pixel_to_meters, o.holes) },
    })),
    groups: value.groups,
    reviewed_classes: [...new Set(value.reviewed_classes)],
  };
}
export async function saveScene(
  id: string,
  value: SceneReview,
  principal: { principal: string; scopes: string[] },
) {
  return sceneReviewTransaction(id, principal, (_data, commit) => commit(value));
}
/** Keep reconciliation checks, journal preparation and canonical saving under the same scene lock. */
export async function sceneReviewTransaction<T>(
  id: string,
  principal: { principal: string; scopes: string[] },
  operation: (data: SceneData, commit: (value: SceneReview, options?: { feedbackImport?: boolean }) => Promise<SceneReview>) => Promise<T>,
): Promise<T> {
  const directory = scenePath(id),
    lock = path.join(directory, "review.lock");
  try {
    await mkdir(lock);
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === "EEXIST")
      throw new ReviewError(
        "Another review is saving. Retry in a moment.",
        503,
      );
    throw e;
  }
  const temp = path.join(directory, `review.${randomUUID()}.tmp`);
  try {
    const data = await loadScene(id);
    const { model, review, stale } = data;
    let committed = false;
    const commit = async (value: SceneReview, options?: { feedbackImport?: boolean }): Promise<SceneReview> => {
    if (committed) throw new ReviewError("This scene transaction already saved.", 409);
    const next = validateSceneReview(value, model);
    if (next.revision !== (review?.revision || 0))
      throw new ReviewError(
        "This scene changed in another session. Reload; your draft is preserved.",
        409,
      );
    const previous = review?.objects || model.objects;
    if (!options?.feedbackImport) next.objects = acceptPendingFeedback(previous, next.objects, principal.principal, new Date().toISOString());
    const newLinks=next.objects.filter(o=>o.address_id&&previous.find(p=>p.id===o.id)?.address_id!==o.address_id);
    if(newLinks.length) {
      const known=await addressNames(newLinks.map(o=>o.address_id));
      if(newLinks.some(o=>!known[o.address_id!]))throw new ReviewError("This address is not in the local address index. Choose an indexed address.");
    }
    for (const o of [...previous, ...next.objects]) {
      const before = previous.find((p) => p.id === o.id),
        after = next.objects.find((p) => p.id === o.id);
      if (
        JSON.stringify(before) !== JSON.stringify(after) &&
        !principal.scopes.includes(objectScope(o))
      )
        throw new ReviewError(
          `Editing ${o.kind.replaceAll("_", " ")} requires ${objectScope(o)} access.`,
          403,
        );
    }
    if (
      (JSON.stringify(next.groups) !==
        JSON.stringify(review?.groups || model.groups) ||
        JSON.stringify(next.reviewed_classes) !==
          JSON.stringify(review?.reviewed_classes || [])) &&
      !principal.scopes.includes("crm.object.write")
    )
      throw new ReviewError(
        "Object editing access is required for groups and layer reviews.",
        403,
      );
    // On a changed model a confirmed decision must be re-made, except where the server's own recovery keeps it: a
    // verified context expansion re-proposing the very same outline under the same id (the evidence did not change).
    const keptConfirmed = new Set((data.recovery?.review.objects || []).filter((o) => o.status === "confirmed").map((o) => `${o.id}:${JSON.stringify([o.ring, o.holes || []])}`));
    if (stale && next.objects.some((o) => o.status === "confirmed" && !keptConfirmed.has(`${o.id}:${JSON.stringify([o.ring, o.holes || []])}`)))
      throw new ReviewError(
        "Start this changed model with unreviewed objects, then recheck them before confirming.",
        409,
      );
    const { history = [], ...old } = review || {};
    const saved = {
      ...next,
      basemap_mode: basemapMode(value),
      revision: next.revision + 1,
      updated_at: new Date().toISOString(),
      updated_by: principal.principal,
      history: review ? [...history, old as Omit<SceneReview, "history">] : [],
    };
    await writeFile(temp, JSON.stringify(saved, null, 2));
    await rename(temp, path.join(directory, "review.json"));
    committed = true;
    return saved;
    };
    return await operation(data, commit);
  } finally {
    await rm(temp, { force: true });
    await rm(lock, { recursive: true, force: true });
  }
}
