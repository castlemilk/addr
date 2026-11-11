import { footprintMetrics, type SceneModelSnapshot, type SceneObject, type SceneReview } from "../scene-types";
import type { Annotation, Geometry, Item, Position } from "./model";
import type { AssetRef, NamedAsset } from "./assets";

export interface AddrSceneSource {
  sceneId: string; image: AssetRef; model: AssetRef; review?: AssetRef; reviewModel?: AssetRef;
  reviewImage?: AssetRef; confidence?: AssetRef; modelSignature: string; imageSha256: string;
  width: number; height: number; analysisRefs?: NamedAsset[]; assets?: NamedAsset[];
}
export interface AddrProperties {
  objectId: string; layer?: string; overrides?: Partial<SceneObject>; removedFields?: string[]; fieldworkReview?: { origin: string; sourceModelSignature: string };
}
export function sceneSource(item: Item) { return item.source as unknown as AddrSceneSource; }
export function annotationAddr(annotation?: Annotation): AddrProperties | undefined { return annotation?.properties?.addr as AddrProperties | undefined; }
const closed = (ring: Position[]) => ring.length && (ring[0][0] !== ring[ring.length - 1][0] || ring[0][1] !== ring[ring.length - 1][1]) ? [...ring, ring[0]] : ring;
export function objectGeometry(object: SceneObject, model: SceneModelSnapshot): Geometry {
  return { type: "Polygon", coordinates: [object.ring, ...(object.holes || [])].map((ring) => closed(ring.map(([x, y]) => [x / model.image_width, y / model.image_height]))) };
}
// Protobuf Struct / Go JSON may reorder object keys; coordinate order remains meaningful.
export function sameGeometry(a: Geometry | undefined, b: Geometry) {
  return !!a && a.type === b.type && JSON.stringify(a.coordinates) === JSON.stringify(b.coordinates);
}
export function sameSceneFrame(a: SceneModelSnapshot, b: SceneModelSnapshot) {
  return a.image_sha256 === b.image_sha256 && a.image_width === b.image_width && a.image_height === b.image_height && JSON.stringify(a.pixel_to_meters) === JSON.stringify(b.pixel_to_meters);
}
function geometryRings(geometry: Extract<Geometry, { type: "Polygon" }>, model: SceneModelSnapshot) {
  return geometry.coordinates.map((ring) => ring.slice(0, -1).map(([x, y]): Position => [x * model.image_width, y * model.image_height]));
}
export function sceneObjects(item: Item, model: SceneModelSnapshot, review?: SceneReview | null): SceneObject[] {
  const base = review?.objects || model.objects;
  const marks = new Map(item.annotations.map((mark) => [annotationAddr(mark)?.objectId || mark.id, mark]));
  const objects = base.map((original) => {
    const mark = marks.get(original.id);
    if (!mark) return original;
    const overrides = annotationAddr(mark)?.overrides || {};
    const next = { ...original, ...overrides, model: { ...original.model, ...overrides.model } };
    for (const field of annotationAddr(mark)?.removedFields || []) if (!["id", "model", "ring", "kind"].includes(field)) delete (next as unknown as Record<string, unknown>)[field];
    if (mark.geometry.type === "Polygon") {
      // The API geometry is authoritative, including when a client retains old
      // pixel-ring overrides. Preserve exact source arrays for unchanged shapes.
      if (sameGeometry(mark.geometry, objectGeometry(original, model))) {
        next.ring = original.ring;
        if ("holes" in original) next.holes = original.holes;
        else delete next.holes;
      } else {
        const rings = geometryRings(mark.geometry, model);
        next.ring = rings[0]; next.holes = rings.slice(1);
        next.model = { ...next.model, ...footprintMetrics(next.ring, model.pixel_to_meters, next.holes) };
      }
    }
    return next;
  });
  // Newly drawn objects are represented once, as an explicit Fieldwork override.
  for (const mark of item.annotations) {
    const value = annotationAddr(mark)?.overrides;
    const id = annotationAddr(mark)?.objectId || mark.id;
    if (value && !objects.some((object) => object.id === id) && value.model && value.kind && mark.geometry.type === "Polygon") {
      const rings = geometryRings(mark.geometry, model);
      objects.push({ ...value, id, ring: rings[0], holes: rings.slice(1), model: { ...value.model, ...footprintMetrics(rings[0], model.pixel_to_meters, rings.slice(1)) } } as SceneObject);
    }
  }
  return objects;
}
export function updateSceneObject(item: Item, original: SceneObject, next: SceneObject, model: SceneModelSnapshot): Item {
  const existing = item.annotations.find((mark) => (annotationAddr(mark)?.objectId || mark.id) === next.id);
  const previousGeometry = existing?.geometry;
  const sourceRings = original.ring ? [original.ring, ...(original.holes || [])] : undefined;
  // Compare exactly in the pixel space the editor displayed. Multiplication and
  // division are not lossless for API-authored normalized coordinates, so a
  // notes/height edit must retain the original wire geometry without re-encoding.
  const displayedRings = previousGeometry?.type === "Polygon"
    ? sourceRings && sameGeometry(previousGeometry, objectGeometry(original, model)) ? sourceRings : geometryRings(previousGeometry, model)
    : sourceRings;
  const changedOutline = !displayedRings || JSON.stringify(displayedRings) !== JSON.stringify([next.ring, ...(next.holes || [])]);
  const geometry = !changedOutline && previousGeometry?.type === "Polygon" ? previousGeometry : objectGeometry(next, model);
  if (changedOutline) next = { ...next, geometry_reviewed: false, model: { ...next.model, ...footprintMetrics(next.ring, model.pixel_to_meters, next.holes), height_verified: false }, roof_parts: undefined, roof_lines: undefined, roof_annotation: null, roof_geometry_source: undefined, roof_geometry_note: undefined };
  // Keep old outline-bound feedback intact in the immutable source, never rebind it.
  if (next.kind !== "building") next = { ...next, roof_feedback: undefined, roof_line_edits: undefined, roof_parts: undefined, roof_lines: undefined, roof_annotation: null };
  const fields = [...new Set([...Object.keys(original), ...Object.keys(next)])] as (keyof SceneObject)[];
  const overrides = Object.fromEntries(fields.filter((key) => next[key] !== undefined && JSON.stringify(next[key]) !== JSON.stringify(original[key])).map((key) => [key, next[key]]));
  const removedFields = fields.filter((key) => next[key] === undefined && original[key] !== undefined);
  const mark: Annotation = {
    ...(existing || { id: next.id, labelId: "" }), geometry,
    properties: { ...existing?.properties, addr: { ...annotationAddr(existing), objectId: next.id, overrides, removedFields, fieldworkReview: { origin: "fieldwork-user-feedback", sourceModelSignature: model.model_signature } } },
  };
  return { ...item, status: editedItemStatus(item.status, isReviewProject(item.projectId)), annotations: existing ? item.annotations.map((value) => value.id === existing.id ? mark : value) : [...item.annotations, mark] };
}
// An edit moves an unreviewed item into review. In a review queue (keepExplicit) an explicit item status (in review, approved,
// rejected) is the reviewer's own item-level answer and is kept, so a later object edit never silently undoes it; main projects
// keep their original rule: every edit puts the item back in review.
export const editedItemStatus = (status: Item["status"], keepExplicit = true): Item["status"] => !keepExplicit || status === "unreviewed" || !status ? "in_review" : status;
// True while the draft item carries a ring/holes override for the object that the saved item
// does not (or vice versa). A missing mark or a mark without overrides counts as "no outline
// override", so notes/status-only edits never make an outline pending.
export function outlinePendingFor(item: Item, savedItem: Item | undefined, objectId: string): boolean {
  const outline = (document?: Item) => {
    const mark = document?.annotations.find((entry) => (annotationAddr(entry)?.objectId || entry.id) === objectId);
    const overrides = annotationAddr(mark)?.overrides;
    return JSON.stringify([overrides?.ring ?? null, overrides?.holes ?? null]);
  };
  return outline(item) !== outline(savedItem);
}
export function sceneOrigin(object: SceneObject) {
  const provenance = (object as unknown as { provenance?: { source?: string } }).provenance?.source || object.source || "Unknown source";
  if (/machine:|decision-policy/i.test(provenance)) return `Machine decision · ${provenance}`;
  if (/agent|tier.?3/i.test(provenance)) return `Agent observation · ${provenance}`;
  return provenance;
}

// ---- Review-queue helpers (Fieldwork UI, 9 Oct 2026) ----

/** The spelling every review-project check compares: Unicode NFKC, format characters dropped, every dash read as "-", lowercased,
 *  whitespace and control characters trimmed. Same steps as normalizeProjectId in deploy/annotation/src/mcp-guard.ts, Go
 *  migration.NormalizeProjectID and Python normalize_project_id (the regular expressions are built at run time for the ES2017 target). */
const FORMAT_CHARACTERS = new RegExp("\\p{Cf}", "gu"), DASHES = new RegExp("[\\p{Pd}\\u2212]", "gu"), ID_EDGES = new RegExp("^[\\s\\p{Cc}]+|[\\s\\p{Cc}]+$", "gu");
export const normalizeProjectId = (id: string) => id.normalize("NFKC").replace(FORMAT_CHARACTERS, "").replace(DASHES, "-").toLowerCase().replace(ID_EDGES, "");
/** Projects whose id ends in "-review" (in any spelling) are agent-built review queues (e.g. mooroolbark-review); everything else
 *  is a main project. */
export const isReviewProject = (projectId: string | undefined) => !!projectId && normalizeProjectId(projectId).endsWith("-review");
/** The review family of a queue item (item.metadata.addr.family: shadow, cut, veg, join, round5), if it names one. */
export function reviewFamily(item: Pick<Item, "metadata">): string | undefined {
  const family = (item.metadata?.addr as { family?: unknown } | undefined)?.family;
  return typeof family === "string" ? family : undefined;
}
/** True for an outline the agent proposed (source "agent-proposal:..."): PROPOSED in shadow and cut items, JOINED in join items. */
export const isAgentProposal = (object: Pick<SceneObject, "source">) => /^agent-proposal(:|$)/.test(String(object.source ?? ""));
const markedObjects = (item: Item, objects: SceneObject[]) => {
  const byId = new Map(objects.map((object) => [object.id, object]));
  return item.annotations.map((mark) => byId.get(annotationAddr(mark)?.objectId || mark.id)).filter((object): object is SceneObject => !!object);
};
/** A vegetation-typing item of a review queue: family "veg", or (older items without a family) any outline with a vegetation label. */
export function isVegetationItem(item: Item, objects: SceneObject[]): boolean {
  const family = reviewFamily(item);
  return family ? family === "veg" : markedObjects(item, objects).some((object) => isVegetationLabel(object.label));
}

/** The object id each annotation stands for (properties.addr.objectId, else the annotation id) mapped to its labelId. */
export function objectLabelIds(item: Item): Map<string, string> {
  return new Map(item.annotations.filter((mark) => !!mark.labelId).map((mark) => [annotationAddr(mark)?.objectId || mark.id, mark.labelId]));
}

/** The outline a review-queue item (shadow, cut, join) is answered on, selected when it opens: the one agent proposal (PROPOSED or
 *  JOINED), whatever its status, so a revisit re-selects the answered proposal and shows its answer. Never the CURRENT outline or a
 *  join PIECE. None in vegetation items (every outline is answered), in main projects (nothing is auto-selected there) and when the
 *  item holds no single proposal. `objects` are the effective scene objects (sceneObjects). */
export function focusObjectId(item: Item, objects: SceneObject[]): string | undefined {
  if (!item.annotations.length || !isReviewProject(item.projectId) || isVegetationItem(item, objects)) return undefined;
  const proposals = markedObjects(item, objects).filter(isAgentProposal);
  return proposals.length === 1 ? proposals[0].id : undefined;
}

/** In a review queue's vegetation item (one item holds 3-102 outlines, each answered on its own), the decision bar moves to the next
 *  open outline instead of the next item: the next of `objects` (display order, wrapping) after `currentId` that is one of the item's
 *  annotations, still "candidate" and carries no vegetation answer word in its Label. None otherwise (other items move to the next
 *  item). */
export function nextOpenObjectId(item: Item, objects: SceneObject[], currentId: string): string | undefined {
  if (!isReviewProject(item.projectId) || !objects.length || !isVegetationItem(item, objects)) return undefined;
  const marked = new Set(item.annotations.map((mark) => annotationAddr(mark)?.objectId || mark.id));
  const start = objects.findIndex((object) => object.id === currentId);
  for (let step = 1; step <= objects.length; step++) {
    const object = objects[(start + step) % objects.length];
    if (object.id !== currentId && marked.has(object.id) && object.status === "candidate" && !vegetationLabelType(object.label)) return object.id;
  }
  return undefined;
}

export type SceneDecision = "right" | "partly" | "wrong";
/** What each decision-bar action writes into the object's Fieldwork overrides (properties.addr.overrides). */
export const SCENE_DECISIONS: Record<SceneDecision, Pick<SceneObject, "status" | "geometry_reviewed">> = {
  right: { status: "confirmed", geometry_reviewed: true },
  partly: { status: "needs_adjustment", geometry_reviewed: false },
  wrong: { status: "rejected", geometry_reviewed: false },
};
/** The item with one decision applied to `object` (its current effective state, outline included), as one draft. */
export function decideSceneObject(item: Item, original: SceneObject, object: SceneObject, decision: SceneDecision, model: SceneModelSnapshot): Item {
  return updateSceneObject(item, original, { ...object, ...SCENE_DECISIONS[decision] }, model);
}

export type SceneColourMode = "class" | "label";
export const sceneColourKey = (projectId: string) => `fieldwork.sceneColour.${projectId}`;
/** A stored choice wins; otherwise review queues colour by label (their labels say CURRENT / PROPOSED) and main projects by class
 *  (their labelIds are the immutable source class, so label colours would hide edited classes). */
export function sceneColourMode(projectId: string, stored: string | null | undefined): SceneColourMode {
  return stored === "class" || stored === "label" ? stored : isReviewProject(projectId) ? "label" : "class";
}

/** The vegetation words the review pull reads from an outline's Label (scripts/ml/fieldwork_review.py VEG_LABEL). */
const VEGETATION_WORD = /^\s*(grass|hedge|shrub|tree)\s*(?:[·.:\-]?\s*\d{1,4})?\s*$/i;
/** A proposal label from the vegetation queue ("tree? · 0012", "tree or shrub? · 0003", "type? · 0430"). */
const VEGETATION_PROPOSAL = /^\s*(grass|hedge|shrub|tree|tree or shrub|type)\?\s*(?:[·.:\-]?\s*\d{1,4})?\s*$/i;
/** The type a label states as an answer ("tree · 0012" -> "tree"); proposals with "?" and other labels state none. */
export function vegetationLabelType(label: string | undefined): string | undefined {
  return VEGETATION_WORD.exec(label ?? "")?.[1]?.toLowerCase();
}
/** True when a label is a vegetation proposal or answer, i.e. the outline belongs to a vegetation-typing queue. */
export const isVegetationLabel = (label: string | undefined) => VEGETATION_PROPOSAL.test(label ?? "") || !!vegetationLabelType(label);
/** The word a vegetation proposal label proposes ("tree or shrub? · 0003" -> "tree or shrub"); none for other labels. */
export function vegetationProposal(label: string | undefined): string | undefined {
  return VEGETATION_PROPOSAL.exec(label ?? "")?.[1]?.toLowerCase();
}
/** True when Right (confirmed) alone stores no type for an outline: its proposal is "tree or shrub?" or "type?" and its current
 *  Label states no type word yet. The review pull lists such a confirm as an exception ("type a word in the Label"). */
export function vegetationNeedsType(proposalLabel: string | undefined, currentLabel: string | undefined): boolean {
  const proposal = vegetationProposal(proposalLabel);
  return (proposal === "tree or shrub" || proposal === "type") && !vegetationLabelType(currentLabel);
}
/** Geometry equal to `b` within `tolerance` (normalised image units): a vertex handle released where it was picked up re-encodes the
 *  outline with float noise only, which is not an edit. */
export function nearlySameGeometry(a: Geometry | undefined, b: Geometry, tolerance = 1e-9): boolean {
  if (!a || a.type !== b.type) return false;
  const flat = (value: unknown): number[] => Array.isArray(value) ? value.flatMap(flat) : [Number(value)];
  const left = flat(a.coordinates), right = flat(b.coordinates);
  return left.length === right.length && left.every((value, index) => Math.abs(value - right[index]) <= tolerance);
}
/** The Label that states `type`, keeping the outline's trailing number: ("grass? · 0001", "tree") -> "tree · 0001". */
export function vegetationLabel(label: string | undefined, type: string): string {
  const number = /(\d{1,4})\s*$/.exec(label ?? "")?.[1];
  return number ? `${type} · ${number}` : type;
}
