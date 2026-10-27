import { createHash } from "node:crypto";
import { ReviewError } from "../review-feedback";
import { SCENE_CLASSES, type SceneData, type SceneModelSnapshot, type SceneObject, type SceneReview } from "../scene-types";
import { expansionOffset, recoverSceneReview } from "../scene-feedback";
import { carriesVegetationType } from "../scene-vegetation";
import { annotationAddr, objectGeometry, sameGeometry, sameSceneFrame, sceneObjects } from "./addr-scene";
import type { Item } from "./model";

export interface FeedbackDocument { json: string; sha256: string }
/** Schema 2: how the packet builder bound the entry's baseline (frozen plan document or a push receipt); mirrors Go feedback.Binding. */
export interface FeedbackBinding {
  kind: "frozen" | "push"; runId?: string; receiptSha256?: string; receiptRevision?: number;
  generation?: { modelSignature: string; reviewRevision: number; reviewHeadSha256?: string; reviewHistorySha256?: string; imageSha256: string; width: number; height: number };
}
export interface FeedbackEntry {
  itemId: string; sceneId: string; baseItem: FeedbackDocument; item: FeedbackDocument;
  model: FeedbackDocument; review?: FeedbackDocument; reviewModel?: FeedbackDocument;
  actor?: string; timestamp?: string; binding?: FeedbackBinding;
}
export interface FeedbackBundle {
  schemaVersion: 1 | 2; origin: string; projectId: string; snapshotId: string; manifestSha256: string; cursor: string;
  entries: FeedbackEntry[]; quarantined: { itemId: string; reason: string }[]; selection?: string; digest: string;
}
export type FieldworkRecoveryHopMethod = "expansion-256" | "same-frame";
export type FieldworkRecoveryMethod = FieldworkRecoveryHopMethod | "chain";
/** One verified hop of a recovery chain: the snapshot the edit was carried onto and the frame relation that carried it. */
export interface FieldworkRecoveryHop { modelSignature: string; imageSha256: string; method: FieldworkRecoveryHopMethod }
/** The superseded frame a Fieldwork edit was made on, and how it was carried to the current local frame
 *  (`method: "chain"` with `hops` in order when it crossed more than one snapshot). */
export interface FieldworkRecovery { modelSignature: string; imageSha256: string; method: FieldworkRecoveryMethod; hops?: FieldworkRecoveryHop[] }
export interface FieldworkImport {
  pending: boolean; origin: string; projectId: string; itemId: string; revision: number; cursor: string;
  accepted_at?: string; accepted_by?: string; recoveredFrom?: FieldworkRecovery;
}
export type ImportedSceneObject = SceneObject & { fieldwork_import?: FieldworkImport };
export interface FeedbackReceipt {
  schemaVersion: 1; origin: string; projectId: string; itemId: string; sceneId: string;
  snapshotId: string; manifestSha256: string; bundleDigest: string; cursor: string; planId: string;
  remote: FeedbackDocument; appliedReview: SceneReview; localRevision: number;
  sourceModelSha256: string; sourceReviewSha256?: string; actor?: string; timestamp?: string;
  acceptedBy: string; acceptedAt: string; recoveredFrom?: FieldworkRecovery;
}
export interface FeedbackChange {
  objectId: string | null; fields: { field: string; before: unknown; local: unknown; incoming: unknown }[];
  substantive: boolean; receiptOnly?: boolean; reviewUrl: string; implication: string;
}
export interface FeedbackScenePlan {
  itemId: string; sceneId: string; localRevision: number; remoteRevision: number;
  status: "ready" | "unchanged" | "conflict"; changes: FeedbackChange[]; conflicts: string[]; reviewUrl: string;
  recoveredFrom?: FieldworkRecovery;
}
export interface PreparedFeedbackScene {
  summary: FeedbackScenePlan; review: SceneReview; localHash: string; receiptHash: string;
  receipt?: FeedbackReceipt; entry: FeedbackEntry; item: Item; shouldSave: boolean;
}

const digestPattern = /^[a-f0-9]{64}$/;
const bindingKeys = new Set(["kind", "runId", "receiptSha256", "receiptRevision", "generation"]);
const scenePattern = /^[a-zA-Z0-9_-]{1,100}$/;
const objectFields = new Set(["kind", "label", "ring", "holes", "status", "geometry_reviewed", "visibility", "review_issues", "address_id", "group_id", "parent_id", "notes", "roof_type", "roof_feedback", "roof_line_edits", "roof_geometry_source", "roof_geometry_note", "roof_annotation", "roof_parts", "roof_lines", "model"]);
const computedModelFields = new Set(["width_m", "length_m", "area_m2", "footprint_area_px"]);
const modelFields = new Set(["solar", "surface", "car", "tree", "placement", "height_m", "base_m", "height_source", "height_verified", "width_m", "length_m", "area_m2", "rotation_deg", "color", "footprint_area_px"]);
export const feedbackHash = (value: string) => createHash("sha256").update(value, "utf8").digest("hex");
function sorted(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sorted);
  if (value && typeof value === "object") return Object.fromEntries(Object.keys(value).sort().filter(k => (value as Record<string, unknown>)[k] !== undefined).map(k => [k, sorted((value as Record<string, unknown>)[k])]));
  return value;
}
export const feedbackJSON = (value: unknown) => JSON.stringify(sorted(value));
export const feedbackEqual = (a: unknown, b: unknown) => feedbackJSON(a) === feedbackJSON(b);
export const feedbackContentHash = (value: unknown) => feedbackHash(feedbackJSON(value));
export const reviewContent = (review: SceneReview) => {
  const mode = (review as SceneReview & { basemap_mode?: string }).basemap_mode;
  return { revision: review.revision, model_signature: review.model_signature, objects: review.objects, groups: review.groups, reviewed_classes: review.reviewed_classes, basemap_mode: mode === "raw" || mode === "ai-satellite" ? mode : "unknown" };
};
export const localFeedbackHash = (data: SceneData) => feedbackContentHash({ model: data.model, review: data.review });
export function feedbackBundleDigest(bundle: Omit<FeedbackBundle, "digest">) {
  const entries = [...bundle.entries].sort((a, b) => a.itemId < b.itemId ? -1 : a.itemId > b.itemId ? 1 : 0);
  const quarantined = [...bundle.quarantined].sort((a, b) => a.itemId < b.itemId ? -1 : a.itemId > b.itemId ? 1 : a.reason < b.reason ? -1 : a.reason > b.reason ? 1 : 0);
  // Schema 1 keeps its exact tuple; schema 2 appends the binding kind and receipt sha (mirrored by Go ComputeDigest).
  const tuple = (e: FeedbackEntry) => {
    const fields = [e.itemId, e.sceneId, e.baseItem.sha256, e.item.sha256, e.model.sha256, e.review?.sha256 || "", e.reviewModel?.sha256 || "", e.actor || "", e.timestamp || ""];
    return bundle.schemaVersion === 2 ? [...fields, e.binding?.kind || "", e.binding?.receiptSha256 || ""] : fields;
  };
  const summary = [bundle.schemaVersion, bundle.origin, bundle.projectId, bundle.snapshotId, bundle.manifestSha256, bundle.cursor,
    entries.map(tuple), quarantined.map(q => [q.itemId, q.reason]), bundle.selection || ""];
  return feedbackHash(JSON.stringify(summary).replaceAll("\u2028", "\\u2028").replaceAll("\u2029", "\\u2029"));
}
export function feedbackDocument<T>(value: FeedbackDocument, title: string): T {
  if (!value || typeof value.json !== "string" || value.json.length > 64 * 1024 * 1024 || !digestPattern.test(value.sha256) || feedbackHash(value.json) !== value.sha256)
    throw new ReviewError(`${title}: JSON content hash does not match the packet.`, 400);
  try {
    const result = JSON.parse(value.json);
    if (!result || typeof result !== "object" || Array.isArray(result)) throw new Error("Not a document");
    return result as T;
  }
  catch { throw new ReviewError(`${title}: invalid JSON document.`, 400); }
}
export function validateFeedbackBundle(value: unknown): FeedbackBundle {
  const b = value as FeedbackBundle;
  if (!b || (b.schemaVersion !== 1 && b.schemaVersion !== 2) || typeof b.origin !== "string" || b.origin.length > 2048 || typeof b.projectId !== "string" || !scenePattern.test(b.projectId) ||
      typeof b.snapshotId !== "string" || !scenePattern.test(b.snapshotId) || !digestPattern.test(b.manifestSha256) || typeof b.cursor !== "string" || !/^\d+$/.test(b.cursor) ||
      !Array.isArray(b.entries) || b.entries.length > 1000 || (b.selection !== undefined && (typeof b.selection !== "string" || b.selection.length > 200)) || !Array.isArray(b.quarantined) || b.quarantined.some(q => !q || typeof q.itemId !== "string" || typeof q.reason !== "string"))
    throw new ReviewError("Invalid Fieldwork feedback packet.");
  let origin: URL;
  try { origin = new URL(b.origin); } catch { throw new ReviewError("Invalid Fieldwork origin."); }
  if (origin.username || origin.password || origin.search || origin.hash || !["http:", "https:"].includes(origin.protocol)) throw new ReviewError("Invalid Fieldwork origin.");
  // Item ids are distinct; a scene may appear more than once (its marked frozen item and a generation item both edited),
  // and feedback-store applies such entries in a deterministic order, each with its own receipt.
  const ids = new Set<string>();
  for (const e of b.entries) {
    if (!e || !scenePattern.test(e.sceneId) || typeof e.itemId !== "string" || e.itemId.length > 200 || ids.has(e.itemId) ||
        (e.actor !== undefined && (typeof e.actor !== "string" || e.actor.length > 500)) || (e.timestamp !== undefined && typeof e.timestamp !== "string"))
      throw new ReviewError("Feedback entries need distinct valid item identities and a valid scene identity.");
    ids.add(e.itemId);
    // Same rule as Go feedback.Read: schema 1 entries carry no binding; every schema 2 entry carries one.
    const binding = e.binding;
    if (b.schemaVersion === 1 ? binding !== undefined : (!binding || typeof binding !== "object" || Array.isArray(binding) || !["frozen", "push"].includes(binding.kind) ||
        (binding.runId !== undefined && (typeof binding.runId !== "string" || binding.runId.length > 200)) || (binding.receiptSha256 !== undefined && !digestPattern.test(binding.receiptSha256)) ||
        (binding.receiptRevision !== undefined && (!Number.isSafeInteger(binding.receiptRevision) || binding.receiptRevision < 0)) ||
        (binding.generation !== undefined && (!binding.generation || typeof binding.generation !== "object" || Array.isArray(binding.generation))) ||
        Object.keys(binding).some(key => !bindingKeys.has(key))))
      throw new ReviewError("Feedback entry bindings must match the packet schema: none for schema 1, a frozen or push kind with a valid receipt identity for schema 2.");
    for (const [key, document] of Object.entries({ baseItem: e.baseItem, item: e.item, model: e.model })) feedbackDocument(document, `${e.itemId} ${key}`);
    if (e.review) feedbackDocument(e.review, `${e.itemId} review`);
    if (e.reviewModel) feedbackDocument(e.reviewModel, `${e.itemId} review model`);
  }
  if (b.digest !== feedbackBundleDigest(b)) throw new ReviewError("Feedback packet digest does not match its document bindings.");
  return b;
}
function record(value: unknown): Record<string, unknown> { return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {}; }
function requireEqual(a: unknown, b: unknown, message: string) { if (!feedbackEqual(a, b)) throw new ReviewError(message, 409); }
function frame(model: SceneModelSnapshot) {
  return { id: model.id, model_signature: model.model_signature, image_sha256: model.image_sha256, image_width: model.image_width, image_height: model.image_height,
    pixel_to_meters: model.pixel_to_meters, meters_to_pixel: model.meters_to_pixel, centroid: model.centroid };
}
// A push supersede mark writes exactly these keys under metadata.addr (spec I3); every other metadata change is still a conflict.
const pushOwnedMetadataKeys = new Set(["supersededBy", "stale", "unrecoveredFieldwork"]);
export function pushOwnedMetadata(metadata: unknown): unknown {
  if (!metadata || typeof metadata !== "object" || Array.isArray(metadata) || !("addr" in metadata)) return metadata;
  const { addr, ...rest } = metadata as Record<string, unknown>;
  if (!addr || typeof addr !== "object" || Array.isArray(addr)) return metadata;
  const kept = Object.fromEntries(Object.entries(addr).filter(([key]) => !pushOwnedMetadataKeys.has(key)));
  return Object.keys(kept).length ? { ...rest, addr: kept } : rest;
}
function sourceRef(item: Item, key: string, document: FeedbackDocument | undefined) {
  const ref = record(item.source[key]);
  if (document ? ref.sha256 !== document.sha256 || ref.url !== `/api/v1/assets/${document.sha256}` : item.source[key] !== undefined)
    throw new ReviewError(`The ${key} evidence does not match the frozen item source.`, 409);
}
function validateItem(item: Item, base: Item, entry: FeedbackEntry, bundle: FeedbackBundle, model: SceneModelSnapshot, review?: SceneReview) {
  if (!item || !base || item.id !== entry.itemId || base.id !== entry.itemId || item.projectId !== bundle.projectId || base.projectId !== bundle.projectId ||
      item.adapter !== "addr-scene" || base.adapter !== "addr-scene" || item.source?.sceneId !== entry.sceneId || base.source?.sceneId !== entry.sceneId || !Number.isSafeInteger(item.revision) || item.revision < 1 ||
      !Array.isArray(item.annotations) || !Array.isArray(base.annotations)) throw new ReviewError("Item, project, adapter or scene identity does not match the packet.", 409);
  requireEqual(item.source, base.source, "The cloud source binding changed; reconcile a new model explicitly.");
  requireEqual(pushOwnedMetadata(item.metadata), pushOwnedMetadata(base.metadata), "Unmapped item metadata changed; inspect it before importing feedback.");
  if (item.crop || base.crop) throw new ReviewError("Scene crops cannot be reconciled.", 409);
  sourceRef(base, "model", entry.model); sourceRef(base, "review", entry.review); sourceRef(base, "reviewModel", entry.reviewModel);
  if (base.source.modelSignature !== model.model_signature || base.source.imageSha256 !== model.image_sha256 || base.source.width !== model.image_width || base.source.height !== model.image_height ||
      record(base.source.image).sha256 !== model.image_sha256 || record(base.source.image).url !== `/api/v1/assets/${model.image_sha256}`)
    throw new ReviewError("The imported image/model identity is inconsistent.", 409);
  const source = new Map((review?.objects || model.objects).map(o => [o.id, o]));
  const marks = new Map(base.annotations.map(a => [a.id, a]));
  if (marks.size !== base.annotations.length || item.annotations.length < marks.size || new Set(item.annotations.map(a => a.id)).size !== item.annotations.length || source.size !== marks.size || [...marks.keys()].some(id => !item.annotations.some(mark => mark.id === id)))
    throw new ReviewError("Deleted or unmapped annotations need explicit reconciliation.", 409);
  for (const mark of item.annotations) {
    const baseline = marks.get(mark.id), original = source.get(mark.id), addr = annotationAddr(mark), originalAddr = annotationAddr(baseline);
    if (!scenePattern.test(mark.id) || addr?.objectId !== mark.id || mark.geometry?.type !== "Polygon" || (baseline && (!original || originalAddr?.objectId !== mark.id || baseline.geometry.type !== "Polygon" || mark.labelId !== baseline.labelId)))
      throw new ReviewError(`Object ${mark.id}: identity or immutable source class changed.`, 409);
    if (baseline && original && (!sameGeometry(baseline.geometry, objectGeometry(original, model)) || Object.keys(originalAddr?.overrides || {}).length || originalAddr?.removedFields?.length))
      throw new ReviewError(`Object ${mark.id}: baseline is not the frozen source projection.`, 409);
    if (mark.notes) throw new ReviewError(`Object ${mark.id}: use overrides.notes, not top-level annotation notes.`);
    const immutable = (value: unknown) => Object.fromEntries(Object.entries(record(value)).filter(([key]) => !["overrides", "removedFields", "fieldworkReview"].includes(key)));
    if (baseline) requireEqual(immutable(addr), immutable(originalAddr), `Object ${mark.id}: source provenance changed.`);
    else {
      if (addr?.overrides?.source !== "fieldwork-user-feedback" || !addr.overrides.model || !addr.overrides.kind || !addr.overrides.label ||
          !["candidate", "confirmed", "needs_adjustment", "rejected"].includes(addr.overrides.status || "") || typeof addr.overrides.geometry_reviewed !== "boolean" ||
          Object.keys(immutable(addr)).some(key => key !== "objectId")) throw new ReviewError(`Object ${mark.id}: new objects need complete Fieldwork source fields without asserted provenance.`, 409);
    }
    requireEqual(Object.fromEntries(Object.entries(record(mark.properties)).filter(([key]) => key !== "addr")), Object.fromEntries(Object.entries(record(baseline?.properties)).filter(([key]) => key !== "addr")), `Object ${mark.id}: unmapped annotation properties changed.`);
    for (const key of [...Object.keys(addr?.overrides || {}), ...(addr?.removedFields || [])]) if (!objectFields.has(key) && !(key === "source" && !baseline) && !(key === "id" && addr?.overrides?.id === mark.id))
      throw new ReviewError(`Object ${mark.id}: ${key} is not a mapped feedback field.`, 409);
    if (Object.keys(addr?.overrides?.model || {}).some(key => !modelFields.has(key))) throw new ReviewError(`Object ${mark.id}: unmapped model fields changed.`, 409);
    if (!Array.isArray(mark.geometry.coordinates) || !mark.geometry.coordinates.length || mark.geometry.coordinates.length > 33) throw new ReviewError(`Object ${mark.id}: invalid polygon rings.`);
    for (const ring of mark.geometry.coordinates) if (!Array.isArray(ring) || ring.length < 4 || ring.length > 201 || !feedbackEqual(ring[0], ring.at(-1)) || ring.some(p => !Array.isArray(p) || p.length !== 2 || p.some(n => typeof n !== "number" || !Number.isFinite(n) || n < 0 || n > 1)))
      throw new ReviewError(`Object ${mark.id}: invalid normalized polygon.`);
  }
}
function editable(object: SceneObject): Record<string, unknown> {
  return Object.fromEntries([...objectFields].filter(key => key in object).map(key => [key, key === "model" ? Object.fromEntries(Object.entries(object.model).filter(([name]) => !computedModelFields.has(name))) : object[key as keyof SceneObject]]));
}
function clearIncompatibleProfiles(object: SceneObject) {
  // validateSceneReview refuses a vegetation type on any other kind, so a class correction away from tree / vegetation drops it.
  if (!carriesVegetationType(object.kind)) delete object.vegetation_type;
  if (object.kind !== "tree") delete object.model.tree;
  if (object.kind !== "car") delete object.model.car;
  if (object.kind !== "solar_panel") delete object.model.solar;
  if (!["road", "driveway", "footpath"].includes(object.kind)) delete object.model.surface;
}
interface FieldDelta { path: string[]; before: unknown; after: unknown }
function deltas(before: Record<string, unknown>, after: Record<string, unknown>, prefix: string[] = []): FieldDelta[] {
  return [...new Set([...Object.keys(before), ...Object.keys(after)])].sort().flatMap(key => {
    const a = before[key], b = after[key], path = [...prefix, key];
    if (feedbackEqual(a, b)) return [];
    if (a && b && typeof a === "object" && typeof b === "object" && !Array.isArray(a) && !Array.isArray(b)) return deltas(record(a), record(b), path);
    return [{ path, before: a, after: b }];
  });
}
function get(object: unknown, parts: string[]): unknown { return parts.reduce((value, part) => record(value)[part], object); }
function put(object: unknown, parts: string[], value: unknown) {
  let target = record(object);
  for (const part of parts.slice(0, -1)) { target[part] = { ...record(target[part]) }; target = record(target[part]); }
  const key = parts[parts.length - 1];
  if (value === undefined) delete target[key]; else target[key] = structuredClone(value);
}
const printable = (value: unknown) => value === undefined ? null : feedbackJSON(value).length > 2000 ? { summary: "Large value; retained in the packet and receipt", sha256: feedbackContentHash(value) } : value;
/** Only the verified 512->1024 context expansion or an identical pixel frame can carry an old-frame edit one hop; anything else fails closed. */
function recoveryMethod(source: SceneModelSnapshot, local: SceneModelSnapshot): FieldworkRecoveryHopMethod | null {
  if (expansionOffset(source, local) === 256) return "expansion-256";
  if (source.id === local.id && sameSceneFrame(source, local) && feedbackEqual(source.meters_to_pixel, local.meters_to_pixel)) return "same-frame";
  return null;
}
/** True when the packet's source model and the local model share id, signature, image and coordinate frame (no recovery needed). */
export const sameModelFrame = (a: SceneModelSnapshot, b: SceneModelSnapshot) => feedbackEqual(frame(a), frame(b));
/** An old-frame edit may cross at most this many verified hops (frozen 512 -> same-image re-extraction -> 1024 expansion is two). */
export const MAX_RECOVERY_HOPS = 4;
export interface RecoveryChain { path: SceneModelSnapshot[]; methods: FieldworkRecoveryHopMethod[] }
const matrix = (value: unknown) => Array.isArray(value) && value.length === 3 && value.every(row => Array.isArray(row) && row.length === 2 && row.every(n => typeof n === "number" && Number.isFinite(n)));
/** A data/scenes/<id>/models/<signature>.json document usable as a chain hop: the scene's own id, the file's signature and a complete frame. Anything else is not a hop. */
export function chainSnapshot(value: unknown, sceneId: string, signature: string): SceneModelSnapshot | null {
  const s = value as SceneModelSnapshot;
  if (!s || typeof s !== "object" || Array.isArray(s) || s.id !== sceneId || s.model_signature !== signature || !digestPattern.test(signature) || !digestPattern.test(s.image_sha256 || "") ||
      !Number.isSafeInteger(s.image_width) || s.image_width <= 0 || !Number.isSafeInteger(s.image_height) || s.image_height <= 0 || !matrix(s.pixel_to_meters) || !matrix(s.meters_to_pixel) ||
      !Array.isArray(s.objects) || !Array.isArray(s.groups)) return null;
  return s;
}
/** The shortest chain of verified single hops from the superseded frame to the current one through the scene's model snapshots.
 *  Every hop is exact: a same-image snapshot (same id, image, size, pixel_to_meters and meters_to_pixel) is an id-based passthrough
 *  with no shift; a 512->1024 expansion whose context_expansion names the previous snapshot shifts by 256. Breadth-first with
 *  candidates in signature order, so the result is deterministic; bounded by MAX_RECOVERY_HOPS; null fails closed. */
export function resolveRecoveryChain(source: SceneModelSnapshot, local: SceneModelSnapshot, snapshots: SceneModelSnapshot[] = []): RecoveryChain | null {
  const candidates = new Map<string, SceneModelSnapshot>();
  for (const s of [...snapshots].sort((a, b) => a.model_signature < b.model_signature ? -1 : a.model_signature > b.model_signature ? 1 : 0))
    if (s.id === local.id && s.model_signature !== source.model_signature && !candidates.has(s.model_signature)) candidates.set(s.model_signature, s);
  candidates.set(local.model_signature, local); // the live model is the target, whatever models/ holds under its signature
  const visited = new Set([source.model_signature]);
  let frontier: RecoveryChain[] = [{ path: [source], methods: [] }];
  for (let hops = 1; hops <= MAX_RECOVERY_HOPS && frontier.length; hops++) {
    const next: RecoveryChain[] = [];
    for (const chain of frontier) {
      const last = chain.path[chain.path.length - 1];
      for (const candidate of candidates.values()) {
        if (visited.has(candidate.model_signature)) continue;
        const method = recoveryMethod(last, candidate);
        if (!method) continue;
        const extended = { path: [...chain.path, candidate], methods: [...chain.methods, method] };
        if (candidate.model_signature === local.model_signature) return extended;
        visited.add(candidate.model_signature); next.push(extended);
      }
    }
    frontier = next;
  }
  return null;
}
/** Carry a review saved on the chain's first frame onto its last, one verified hop at a time (the scene editor's own recovery). */
function recoverAlong(chain: RecoveryChain, saved: SceneReview): SceneReview | null {
  let review = saved;
  for (let i = 1; i < chain.path.length; i++) {
    const recovered = recoverSceneReview(chain.path[i - 1], chain.path[i], review);
    if (!recovered) return null;
    review = recovered.review;
  }
  return review;
}
function recoveryRecord(chain: RecoveryChain): FieldworkRecovery {
  const [source] = chain.path;
  if (chain.methods.length === 1) return { modelSignature: source.model_signature, imageSha256: source.image_sha256, method: chain.methods[0] };
  return { modelSignature: source.model_signature, imageSha256: source.image_sha256, method: "chain",
    hops: chain.path.slice(1).map((s, i) => ({ modelSignature: s.model_signature, imageSha256: s.image_sha256, method: chain.methods[i] })) };
}
const describeRecovery = (recovery: FieldworkRecovery) => recovery.method === "chain" ? `chain (${(recovery.hops || []).map(hop => hop.method).join(" -> ")})` : recovery.method;
/** The effective objects of a cloud item on its own (superseded) frame, shaped as a review for recoverSceneReview. */
function carriedReview(item: Item, model: SceneModelSnapshot, review: SceneReview | undefined): SceneReview {
  return { revision: item.revision, model_signature: model.model_signature, objects: sceneObjects(item, model, review), groups: review?.groups ?? model.groups, reviewed_classes: [] };
}
/** The genuine Fieldwork edit on the superseded frame: per object, the editable paths whose values differ between the last accepted
 *  cloud state and the edited state, keyed by the id the recovery lands them on (a replaced seed's words move to the detection that
 *  took it over, as in recoverSceneReview). Every other difference between the two recovered states is drift between the two models
 *  (a parent, group, base or roof state the current model added or dropped on an unedited id), never a Fieldwork edit. */
interface OldFrameEdits { paths: Map<string, string[][]>; existed: Set<string> }
/** Where a seed's decision lands after every hop: each snapshot's `replaces` of an id it no longer proposes, followed transitively. */
function replacementTargets(path: SceneModelSnapshot[]): Map<string, string> {
  const replacedBy = new Map<string, string>();
  for (const snapshot of path.slice(1)) {
    const ids = new Set(snapshot.objects.map(o => o.id));
    for (const o of snapshot.objects) for (const seed of o.replaces || []) if (!ids.has(seed)) replacedBy.set(seed, o.id);
  }
  const resolve = (id: string) => { const seen = new Set<string>(); let current = id; while (replacedBy.has(current) && !seen.has(current)) { seen.add(current); current = replacedBy.get(current)!; } return current; };
  return new Map([...replacedBy.keys()].map(id => [id, resolve(id)]));
}
function oldFrameEdits(previousItem: Item, item: Item, model: SceneModelSnapshot, review: SceneReview | undefined, path: SceneModelSnapshot[]): OldFrameEdits {
  const before = new Map(sceneObjects(previousItem, model, review).map(o => [o.id, o]));
  const replacedBy = replacementTargets(path);
  const paths = new Map<string, Map<string, string[]>>();
  for (const edited of sceneObjects(item, model, review)) {
    const original = before.get(edited.id);
    if (!original) continue; // drawn on Fieldwork: the new-object branch imports it whole
    const changed = deltas(editable(original), editable(edited));
    if (!changed.length) continue;
    for (const target of new Set([edited.id, replacedBy.get(edited.id) ?? edited.id])) {
      const set = paths.get(target) ?? new Map<string, string[]>();
      for (const { path } of changed) set.set(path.join("."), path);
      paths.set(target, set);
    }
  }
  return { paths: new Map([...paths].map(([id, set]) => [id, [...set.values()]])), existed: new Set(before.keys()) };
}
/** `snapshots` are the scene's data/scenes/<id>/models/<sig>.json documents (see chainSnapshot); they only matter when the packet's frame is not the local one. */
export function prepareFeedbackScene(bundle: FeedbackBundle, entry: FeedbackEntry, data: SceneData, receipt?: FeedbackReceipt, snapshots: SceneModelSnapshot[] = []): PreparedFeedbackScene {
  const item = feedbackDocument<Item>(entry.item, "Cloud item"), base = feedbackDocument<Item>(entry.baseItem, "Frozen item"), model = feedbackDocument<SceneModelSnapshot>(entry.model, "Source model");
  const sourceReview = entry.review ? feedbackDocument<SceneReview>(entry.review, "Source review") : undefined;
  const reviewModel = entry.reviewModel ? feedbackDocument<SceneModelSnapshot>(entry.reviewModel, "Review model") : undefined;
  const current = structuredClone(data.review || { revision: 0, model_signature: data.model.model_signature, objects: data.model.objects, groups: data.model.groups, reviewed_classes: [] });
  const url = `/scenes?scene=${encodeURIComponent(entry.sceneId)}&status=all`;
  const summary: FeedbackScenePlan = { itemId: entry.itemId, sceneId: entry.sceneId, localRevision: current.revision, remoteRevision: item.revision, status: "unchanged", changes: [], conflicts: [], reviewUrl: url };
  const prepared: PreparedFeedbackScene = { summary, review: current, localHash: localFeedbackHash(data), receiptHash: feedbackContentHash(receipt || null), receipt, entry, item, shouldSave: false };
  try {
    validateItem(item, base, entry, bundle, model, sourceReview);
    if (data.stale || model.id !== entry.sceneId || data.model.id !== entry.sceneId) throw new ReviewError("Local scene identity or model review is stale.", 409);
    if (sourceReview && (!reviewModel || sourceReview.model_signature !== model.model_signature || reviewModel.model_signature !== model.model_signature)) throw new ReviewError("The frozen source review is stale; recover it explicitly before importing feedback.", 409);
    if (reviewModel) requireEqual(frame(reviewModel), frame(model), "The preserved review uses a different frame.");
    if (receipt) {
      if (receipt.origin !== bundle.origin || receipt.projectId !== bundle.projectId || receipt.itemId !== entry.itemId || receipt.sceneId !== entry.sceneId || receipt.snapshotId !== bundle.snapshotId || receipt.manifestSha256 !== bundle.manifestSha256 || receipt.sourceModelSha256 !== entry.model.sha256 || receipt.sourceReviewSha256 !== entry.review?.sha256)
        throw new ReviewError("The last accepted feedback belongs to another source binding.", 409);
      const previous = feedbackDocument<Item>(receipt.remote, "Last accepted cloud item");
      if (item.revision < previous.revision || (item.revision === previous.revision && entry.item.sha256 !== receipt.remote.sha256)) throw new ReviewError("The cloud revision is older than, or disagrees with, the last accepted feedback.", 409);
    }
    const previousItem = receipt ? feedbackDocument<Item>(receipt.remote, "Last accepted cloud item") : base;
    let remoteBeforeObjects: SceneObject[], remoteAfter: SceneObject[], localBaseline: SceneObject[], recoveredFrom: FieldworkRecovery | undefined, edits: OldFrameEdits | undefined;
    if (sameModelFrame(data.model, model)) {
      requireEqual(data.model, model, "The local immutable model content differs from the imported snapshot.");
      remoteBeforeObjects = sceneObjects(previousItem, model, sourceReview);
      remoteAfter = sceneObjects(item, model, sourceReview);
      localBaseline = receipt?.appliedReview.objects || sourceReview?.objects || model.objects;
    } else {
      // The edit was made on a superseded frame. Carry both the last accepted state and the edited state onto the
      // current frame with the same recovery the scene editor uses, one verified hop at a time along the scene's snapshot
      // chain (rings shift by the verified offset, seed decisions follow `replaces`, carried decisions return as
      // needs_adjustment). Only the paths that changed on the old frame are the Fieldwork edit; their values are read
      // from the recovered objects so outlines land on the current frame.
      const chain = resolveRecoveryChain(model, data.model, snapshots);
      const before = chain && recoverAlong(chain, carriedReview(previousItem, model, sourceReview));
      const after = chain && recoverAlong(chain, carriedReview(item, model, sourceReview));
      if (!chain || !before || !after)
        throw new ReviewError(`frame_changed_unrecoverable: the Fieldwork edit on ${entry.itemId} was made on an older model/image frame that cannot be recovered automatically; it stays on that item and in the change feed.`, 409);
      recoveredFrom = recoveryRecord(chain);
      summary.recoveredFrom = recoveredFrom;
      edits = oldFrameEdits(previousItem, item, model, sourceReview, chain.path);
      remoteBeforeObjects = before.objects; remoteAfter = after.objects; localBaseline = before.objects;
    }
    const remoteBefore = new Map(remoteBeforeObjects.map(o => [o.id, o]));
    if ([...remoteBefore.keys()].some(id => !remoteAfter.some(o => o.id === id))) throw new ReviewError("Previously accepted objects cannot be deleted; import an explicit rejection draft.", 409);
    const localBefore = new Map(localBaseline.map(o => [o.id, o]));
    const localCurrent = new Map(current.objects.map(o => [o.id, o]));
    const marker = (): FieldworkImport => ({ pending: true, origin: bundle.origin, projectId: bundle.projectId, itemId: item.id, revision: item.revision, cursor: bundle.cursor, ...(recoveredFrom ? { recoveredFrom } : {}) });
    const carriedNote = recoveredFrom ? ` Values are shown on the current frame after ${describeRecovery(recoveredFrom)} recovery from the superseded frame; the original edit stays on the Fieldwork item.` : "";
    const changedKinds = new Set<SceneObject["kind"]>();
    for (const incoming of remoteAfter) {
      const before = remoteBefore.get(incoming.id), baseline = localBefore.get(incoming.id), local = localCurrent.get(incoming.id);
      // A recovered state also carries what the Fieldwork item never touched: the current model's own attributes on an unedited
      // id, or a frozen parent re-surfaced for context. Neither is an edit; only an id changed on the old frame is considered.
      if (edits && (before ? !edits.paths.has(incoming.id) : edits.existed.has(incoming.id))) continue;
      if (!before) {
        if (local || baseline) throw new ReviewError(`Object ${incoming.id}: a new cloud object collides with an existing local identity.`, 409);
        const next = structuredClone(incoming) as ImportedSceneObject;
        next.status = "needs_adjustment"; next.geometry_reviewed = false; next.model = { ...next.model, height_verified: false };
        clearIncompatibleProfiles(next);
        delete next.roof_feedback;
        if (next.roof_line_edits) next.roof_line_edits = { ...next.roof_line_edits, checked: false };
        next.fieldwork_import = marker();
        localCurrent.set(next.id, next); changedKinds.add(next.kind); prepared.shouldSave = true;
        summary.changes.push({ objectId: next.id, fields: [{ field: "new_object", before: null, local: null, incoming: printable(incoming) }], substantive: true, reviewUrl: `${url}&object=${encodeURIComponent(next.id)}`, implication: `New cloud object imported as a pending local draft. No human review or training eligibility is implied.${carriedNote}` });
        continue;
      }
      if (!baseline || !local) throw new ReviewError(`Object ${incoming.id}: missing from the frozen or current local review.`, 409);
      const changes = edits
        ? (edits.paths.get(incoming.id) ?? []).map(path => ({ path, before: get(before, path), after: get(incoming, path) })).filter(change => !feedbackEqual(change.before, change.after))
        : deltas(editable(before), editable(incoming));
      if (!changes.length) continue;
      const fields: FeedbackChange["fields"] = [], next = structuredClone(local);
      let applied = false, substantive = false;
      const appliedFields: string[] = [];
      for (const change of changes) {
        const value = get(local, change.path), expected = get(baseline, change.path);
        fields.push({ field: change.path.join("."), before: printable(change.before), local: printable(value), incoming: printable(change.after) });
        if (feedbackEqual(value, change.after)) continue;
        if (!feedbackEqual(value, expected)) { summary.conflicts.push(`${incoming.id}.${change.path.join(".")}: local and Fieldwork edits overlap.`); continue; }
        put(next, change.path, change.after); applied = true;
        appliedFields.push(change.path[0]);
        if (change.path[0] !== "notes") substantive = true;
      }
      summary.changes.push({ objectId: incoming.id, fields, substantive, reviewUrl: `${url}&object=${encodeURIComponent(incoming.id)}`, implication: (substantive ? "Imported as a pending local draft. Check it in scene review before training or reference use; the remote decision stays in the receipt." : "Notes only. Existing local review and roof approvals are preserved.") + carriedNote });
      if (applied && substantive) {
        next.status = "needs_adjustment"; next.geometry_reviewed = false; next.model = { ...next.model, height_verified: false };
        delete next.roof_feedback;
        if (next.roof_line_edits) next.roof_line_edits = { ...next.roof_line_edits, checked: false };
        if (appliedFields.some(field => ["ring", "holes", "kind"].includes(field))) {
          delete next.roof_parts; delete next.roof_lines; next.roof_annotation = null;
          delete next.roof_geometry_source; delete next.roof_geometry_note;
        }
        if (appliedFields.includes("kind")) clearIncompatibleProfiles(next);
        if (next.kind !== "building") { delete next.roof_line_edits; delete next.roof_feedback; }
        (next as ImportedSceneObject).fieldwork_import = marker();
        changedKinds.add(local.kind); changedKinds.add(next.kind);
      }
      if (applied) { localCurrent.set(next.id, next); prepared.shouldSave = true; }
    }
    current.objects = [...localCurrent.values()];
    current.reviewed_classes = current.reviewed_classes.filter(kind => !changedKinds.has(kind));
    // A push supersede mark (tags + allowlisted metadata.addr keys) surfaces here as a receipt-only change, never a conflict.
    const sceneFields = ["notes", "title", "tags", "status", "metadata"].filter(key => !feedbackEqual(previousItem[key as keyof Item], item[key as keyof Item]));
    if (sceneFields.length) summary.changes.push({ objectId: null, substantive: false, receiptOnly: true, reviewUrl: url, implication: "Workspace queue fields have no local scene equivalent; retained in the receipt only.", fields: sceneFields.map(field => ({ field: `item.${field}`, before: printable(previousItem[field as keyof Item]), local: null, incoming: printable(item[field as keyof Item]) })) });
    if (current.objects.some(o => !SCENE_CLASSES.includes(o.kind))) throw new ReviewError("An incoming class is not supported by the local scene editor.", 409);
    summary.status = summary.conflicts.length ? "conflict" : summary.changes.length ? "ready" : "unchanged";
  } catch (error) {
    if (!(error instanceof ReviewError)) throw error;
    summary.status = "conflict"; summary.conflicts.push(error.message);
  }
  return prepared;
}

/** A later ordinary local save may accept a draft; an import cannot assert human review. */
export function acceptPendingFeedback(previous: SceneObject[], next: SceneObject[], principal: string, timestamp: string): SceneObject[] {
  const old = new Map(previous.map(o => [o.id, o as ImportedSceneObject]));
  return next.map(value => {
    const before = old.get(value.id), marker = before?.fieldwork_import;
    if (!marker?.pending) return value;
    const accepted = before?.status === "needs_adjustment" && (value.status === "rejected" || (value.status === "confirmed" && value.geometry_reviewed));
    return { ...value, fieldwork_import: accepted ? { ...marker, pending: false, accepted_at: timestamp, accepted_by: principal } : marker };
  });
}
