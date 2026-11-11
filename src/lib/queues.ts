// Shared review-queue data layer: types, live calibration status derivation and readers.
//
// Every band or score here is a model estimate, never accuracy; only human decisions saved in
// data/scenes/<scene>/review.json enter a count, and nothing in this module writes. The status rules
// are a faithful port of scripts/ml/reference_calibration.classify (read that docstring for the
// protocol); wilson() reproduces scripts/ml/scene_confidence.wilson to the last digit.
//
// The module has no Node imports so client components may import its types and pure helpers. File
// access goes through a QueueIO the route provides (fileIO(process.cwd(), fs) on the server), which
// also keeps the readers testable against an in-memory map. Paths handed to the IO are repo-relative.
// The one server-only capability, reading building class hints and structure-kind hints from the eyes
// index, resolves node:sqlite at call time through process.getBuiltinModule so no import reaches a client
// bundle. Both hint versions are agent hints, not labels: they never change a status or a count.
// structure-kinds-v2 adds container stacks, portable classrooms and pool covers to the v1 kinds; the
// evidence readers below accept either version's payload, so an index still holding v1 rows reads fine.
// A third table, decision_policy, holds the machine label and its tier: auto proceeds without a human, assist
// and review need one. decision-policy-v2 rows add the human-agreement interval for the label's stratum and the
// model's score range; v1 rows (no intervals) are still read where no v2 row exists yet, so nothing vanishes
// during a recompute. The table may not exist yet; its reader is guarded on its own so a missing table never
// hides class or structure hints, and a machine label is applied to an object only when a reviewer presses
// "Use machine label" in the editor and then saves the scene. The Machine labelled queue lists the auto tier
// from one query over decision_policy; it is the reviewer's surface for those labels and changes nothing.

export type Band = "low" | "medium" | "high";
export type RowStatus = "confirmed" | "rejected" | "needs_adjustment" | "pending" | "stale";
export const ROW_STATUSES: readonly RowStatus[] = ["confirmed", "rejected", "needs_adjustment", "pending", "stale"];
export const MIN_CALIBRATION_GROUPS = 20;
export const IOU_SUCCESS = 0.85;
export const HIDDEN_VERSION = "cross-capture-v1";
export const PLAN_ID = /^[a-f0-9]{16}$/;
export const SCENE_ID = /^[a-zA-Z0-9_-]{1,100}$/;
export const RUN_ID = /^[a-f0-9]{16}$/;
export const CHECKPOINT_ID = /^[A-Z0-9][A-Z0-9-]{0,31}$/;
export const CALIBRATION_ROOT = "data/reviews/eyes/calibration";
export const CHECKPOINTS_ROOT = "data/references/eyes-checkpoints";
export const SELF_REVIEW_ROOT = "data/roof-review-loop";
export const EYES_INDEX = "data/reviews/eyes/eyes.sqlite";
export const QUEUE_NOTE = "Evidence bands and detector scores are model estimates, not accuracy. Only human decisions saved in scene reviews are counted; this queue never approves, rejects or edits a roof.";
export const HINTS_VERSION = "building-flags-v2";
export const HINTS_NOTE = "Agent hints, not labels: tree-like, secondary structure, non-residential context and commercial are rule-based observations with their evidence, read from the eyes index (scene_object_quality building-flags-v2). They help a reviewer skip or reclassify; nothing is decided by them.";
export const STRUCTURE_VERSION = "structure-kinds-v3";
/** Older structure-kind rows still readable while an index is being recomputed; the current version always wins. */
export const STRUCTURE_FALLBACK_VERSIONS: readonly string[] = ["structure-kinds-v2", "structure-kinds-v1"];
export const STRUCTURE_NOTE = "Agent hints, not labels: class hints (building-flags-v2) and structure kinds (structure-kinds-v3) are rule-based observations with their evidence, read from the eyes index. They help a reviewer skip or reclassify; nothing is decided by them.";
export const OBJECT_HINTS_VERSION = "object-hints-v2";
/** Older object-hints rows still served while an index is being recomputed (a rewrite of the scene drops them); the current version
 *  always wins, and an answer that serves an older row is stale with the reason "object_hints_version", whether or not the caller names
 *  a signature (the row predates the current rules: v1 has no solar_fixture_like). */
export const OBJECT_HINTS_FALLBACK_VERSIONS: readonly string[] = ["object-hints-v1"];
const OBJECT_HINTS_VERSION_PATTERN = /^object-hints-v\d+$/;
/** What each older object-hints version lacks under the current rules and the object kinds that difference touches: object-hints-v2
 *  added solar_fixture_like for solar panels and changed no v1 code, threshold or sentence, so for every other kind a v1 row says what
 *  a v2 row would. */
export const OBJECT_HINTS_RULE_CHANGES: Readonly<Record<string, { kinds: readonly string[]; missing: string }>> = { "object-hints-v1": { kinds: ["solar_panel"], missing: "the solar-panel check" } };
/** Why a hints answer is stale, in this fixed order: "model_signature" when a served row was computed under another model signature
 *  than the caller named (an earlier image version; judged only when the caller names one), "object_hints_version" when the object
 *  hints came from an older object-hints version (judged always: the row predates the current rules). */
export type HintStaleReason = "model_signature" | "object_hints_version";
export const HINT_STALE_REASONS: readonly HintStaleReason[] = ["model_signature", "object_hints_version"];
export const LOT_TAGS_VERSION = "lot-tags-v1";
/** The scene-level lot-tags-v1 row: it carries the lot-frame plan sentence (scene_lot_tags SCENE_OBJECT_ID), the one object id with a leading underscore. */
export const SCENE_ROW_ID = "_scene";
export const OBJECT_HINTS_NOTE = "Agent hints, not labels: object hints (object-hints-v2; v1 rows are read where no v2 row exists and marked stale) and lot tags (lot-tags-v1) are measured observations with their evidence, read from the eyes index. A frame cut, a noisy outline, a pool that reads green or a solar panel that reads like a rooftop fixture is something to check in the image; nothing is decided by them.";
export const DECISION_VERSION = "decision-policy-v2";
/** Older decision rows still readable while the index is being recomputed; the current version always wins. */
export const DECISION_FALLBACK_VERSIONS: readonly string[] = ["decision-policy-v1"];
export const DECISION_NOTE = "Machine labels and tiers (decision-policy-v2, v1 rows read where no v2 row exists) are the decision policy's assessment read from the eyes index: the auto tier proceeds without a human, assist and review need one. Human agreement intervals count reviewer decisions in the label's stratum; model ranges are estimates, not accuracy. A machine label is not a reviewer label; it reaches an object only when a reviewer applies it and saves the scene.";
export const MACHINE_LABELLED_NOTE = "Every roof the decision policy placed in the auto tier, read from the eyes index (decision_policy). Confidence and model ranges are model estimates, not accuracy; human agreement intervals count reviewer decisions in the label's stratum. Each row links into the editor; nothing on this table approves, rejects or edits a roof.";
export const HIDDEN_NOTE = "Cross-capture disagreement between captures of the same roof (eyes sidecar index). Both captures share one orthomosaic, so agreement is consistency, not independent evidence, and bands are model estimates, not accuracy.";
export const SUMMARY_NOTE = "Counts carry their denominators. Bands, scores and self-review outcomes are model estimates, not accuracy; nothing on this page changes a roof.";
const IOU_STEP_PX = 0.25;
const IOU_MAX_SAMPLES = 4_000_000;

export class QueueError extends Error {
  status: number;
  constructor(message: string, status = 400) {
    super(message);
    this.status = status;
  }
}

export interface QueueIO {
  /** UTF-8 text of a repo-relative file, or null when it does not exist. Other failures throw. */
  readText(relativePath: string): string | null;
  /** Entry names of a repo-relative directory, or [] when it does not exist. */
  listDir(relativePath: string): string[];
  exists(relativePath: string): boolean;
  /** Building class hints from the eyes index, or null when it is absent or unreadable. The caller closes the source. */
  hints?(): HintSource | null;
}
interface FsLike {
  readFileSync(file: string, encoding: "utf8"): string;
  readdirSync(directory: string): string[];
  existsSync(file: string): boolean;
}
const missing = (error: unknown) => ["ENOENT", "ENOTDIR", "EISDIR"].includes(String((error as { code?: string })?.code));
/** QueueIO over node:fs rooted at `root`; the route passes its own fs so this module stays Node-free. */
export function fileIO(root: string, fs: FsLike): QueueIO {
  const join = (relative: string) => `${root.replace(/\/+$/, "")}/${relative}`;
  return {
    readText(relative) {
      try { return fs.readFileSync(join(relative), "utf8"); } catch (error) { if (missing(error)) return null; throw error; }
    },
    listDir(relative) {
      try { return fs.readdirSync(join(relative)); } catch (error) { if (missing(error)) return []; throw error; }
    },
    exists(relative) { return fs.existsSync(join(relative)); },
    hints() { return sqliteHintSource(join(EYES_INDEX)); },
  };
}

// --- Building class hints (scene_object_quality building-flags-v2 rows in the eyes index) ------------------

export interface Hint { flag: string; evidence: Record<string, unknown> }
/** Structure-kind hint for a small or secondary building (structure-kinds-v2; v1 payloads parse the same). An agent hint, never a label. */
export interface StructureKind { sub_kind: StructureSubKind; confidence: StructureConfidence; rule: string; evidence: Record<string, unknown> }
/** structure, decision and object_hints are present only when the index holds that row for the object (null or absent otherwise);
 *  object_hints_served_version rides with object_hints and names the object-hints version of the row they came from. */
export interface ClassHints { hints: Hint[]; class_hint: string | null; structure?: StructureKind | null; decision?: Decision | null; object_hints?: ObjectHintsPayload | null; object_hints_served_version?: string | null }
/** One object hint (object-hints-v2; the v1 codes are unchanged): a code, its severity (review asks for a look, context explains), a plain sentence and the numbers behind it. */
export interface ObjectHint { code: string; severity: "context" | "review"; sentence: string; evidence: Record<string, unknown> }
/** The orthogonalised outline the object-hints module computed for a building of 20 m² or more: a pixel ring with its agreement
 *  (IoU) and Hausdorff distance against the stored ring, or the gate's refusal reason. A model estimate a reviewer may apply
 *  from the editor; nothing applies it by itself. */
export interface SquaredCandidate { ring: [number, number][] | null; iou: number | null; hausdorff_m: number | null; refused: string | null }
export interface ObjectHintsPayload { hints: ObjectHint[]; squared_candidate: SquaredCandidate | null }
/** A lot the object overlaps (lot-tags-v1): Vicmap lot and plan with the share of the object's area on it. */
export interface LotShare { lot: string | null; plan: string | null; share: number }
/** lot-tags-v1: the lot the object mostly sits on, whether that lot leaves the frame, the module's flags and its sentences. Every
 *  field may be null when the module could not say; the sentences are the only part the editor prints. */
export interface LotTag { dominant: { pfi: string | null; lot: string | null; plan: string | null; share: number | null; kind: "lot" | "common_property" | null } | null; lots: LotShare[]; flags: string[]; sentences: string[]; lot_complete_in_image: boolean | null; address_plan: string | null }
/** What the index knows about one object: each part is null when its row is absent or unreadable. stale is true when stale_reasons
 *  holds any reason: a served object_quality row computed under another model signature than the one the caller named (the row
 *  describes an earlier image version of the scene), or object hints served from an older object-hints version (they predate the
 *  current rules; object_hints_served_version names it). The newer parts are optional so documents from before they existed still
 *  type as ObjectHints. */
export interface ObjectHints { class_hints: ClassHints | null; structure: StructureKind | null; decision: Decision | null; object_hints?: ObjectHintsPayload | null; lot?: LotTag | null; stale?: boolean;
  /** Why stale is true (HINT_STALE_REASONS order); empty when nothing is stale. */
  stale_reasons?: HintStaleReason[];
  /** The object-hints version of the row the object hints were served from (OBJECT_HINTS_VERSION or an older fallback); null when none was served. */
  object_hints_served_version?: string | null;
  /** The scene's lot-frame plan sentence from the lot-tags-v1 "_scene" row (text only: what a lot-sized frame would cover and cost); null when the index has none. */
  lot_frame?: string | null }
/** The decision policy's label for an object (decision-policy-v1): every field may be null when the policy could not say. */
export interface MachineLabel { kind: string; structure_sub_kind: string | null; class_hint: string | null; roof_form: "flat" | "pitched" | null; roof_type: string | null }
export const DECISION_TIERS = ["auto", "assist", "review"] as const;
export type DecisionTier = (typeof DECISION_TIERS)[number];
/** Badge labels for the tiers, sentence case. */
export const TIER_LABELS: Record<DecisionTier, string> = { auto: "Auto", assist: "Assist", review: "Review" };
/** Human agreement for the label's stratum (decision-policy-v2): reviewer decisions counted, with a Wilson 95% interval, or null bounds when nothing has been judged. */
export interface AgreementInterval { kind: string; stratum: string; confirmed: number; judged: number; lower: number | null; upper: number | null; note: string }
/** The model's score range for the decision (decision-policy-v2): an estimate, not accuracy. */
export interface ScoreInterval { lower: number; upper: number; note: string }
/** What the policy recorded about the human review of this object (signals.review.status from review.json); null when the row carries none. */
export interface DecisionReview { status: string | null }
/** A parsed decision_policy row: the machine label, its tier and confidence (0 to 1), the policy's reasons in its own words, and
 *  (v2) the agreement and score intervals; v1 rows carry null intervals. */
export interface Decision { tier: DecisionTier; confidence: number; machine_label: MachineLabel; reasons: string[]; interval: AgreementInterval | null; score_interval: ScoreInterval | null; review: DecisionReview | null }
export const CLASS_HINTS = ["house", "units", "commercial", "outbuilding", "tree-like", "non-residential"] as const;
export const STRUCTURE_KINDS = ["shipping-container", "container-stack", "shed", "garage", "carport", "attached-cover", "shade-sail", "pergola-or-shade", "water-tank", "portable-classroom", "pool-cover", "fragment", "outbuilding", "not-secondary"] as const;
export type StructureSubKind = (typeof STRUCTURE_KINDS)[number];
export const STRUCTURE_CONFIDENCES = ["weak", "moderate", "strong"] as const;
export type StructureConfidence = (typeof STRUCTURE_CONFIDENCES)[number];
export const HINT_LABELS: Record<string, string> = { "tree-like": "Tree-like", "secondary-structure": "Secondary structure", "non-residential-context": "Non-residential context", commercial: "Commercial" };
/** Badge labels for the sub-kinds that say something; outbuilding and not-secondary add nothing beyond the class hint. */
export const STRUCTURE_LABELS: Partial<Record<StructureSubKind, string>> = { "shipping-container": "Shipping container", "container-stack": "Container stack", shed: "Shed", garage: "Garage", carport: "Carport", "attached-cover": "Patio or carport cover", "shade-sail": "Fabric shade sail", "pergola-or-shade": "Pergola or shade", "water-tank": "Water tank", "portable-classroom": "Portable classroom", "pool-cover": "Pool cover", fragment: "Fragment" };
const STRUCTURE_PLURALS: Partial<Record<StructureSubKind, [string, string]>> = { "shipping-container": ["shipping container", "shipping containers"], "container-stack": ["container stack", "container stacks"], shed: ["shed", "sheds"], garage: ["garage", "garages"], carport: ["carport", "carports"], "attached-cover": ["patio or carport cover", "patio or carport covers"], "shade-sail": ["fabric shade sail", "fabric shade sails"], "pergola-or-shade": ["pergola or shade structure", "pergola or shade structures"], "water-tank": ["water tank", "water tanks"], "portable-classroom": ["portable classroom", "portable classrooms"], "pool-cover": ["pool cover", "pool covers"], fragment: ["fragment", "fragments"] };
/** A fragment says the outline is a piece of something, not a building to classify; /queues hides these rows until asked. */
export const isFragment = (row: Pick<ClassHints, "structure">) => row.structure?.sub_kind === "fragment";
export interface HintBadge { key: string; label: string; title: string; group: "class" | "structure" | "decision" | "object" }
export interface HintSource { hintFor(sceneId: string, objectId: string): ClassHints | null; close(): void }
/** The hint rows of one object. classHints, structure and decision throw on database errors (the caller decides), never
 *  on payloads; read() guards the decision on its own, because the decision_policy table may not exist yet and its absence
 *  must not hide class or structure hints. */
export interface HintReader {
  classHints(sceneId: string, objectId: string, currentSignature?: string): ClassHints | null;
  structure(sceneId: string, objectId: string, currentSignature?: string): StructureKind | null;
  decision(sceneId: string, objectId: string): Decision | null;
  objectHints(sceneId: string, objectId: string, currentSignature?: string): ObjectHintsPayload | null;
  /** The object hints with the object-hints version of the row they came from (the current version first, then the fallbacks). */
  objectHintsServed(sceneId: string, objectId: string, currentSignature?: string): { payload: ObjectHintsPayload; version: string } | null;
  lot(sceneId: string, objectId: string, currentSignature?: string): LotTag | null;
  /** The scene's lot-frame plan sentence from the lot-tags-v1 "_scene" row, or null; not part of read() and never judged for staleness. */
  lotFrame(sceneId: string, currentSignature?: string): string | null;
  /** Every part for one object; with currentSignature the row for that signature is preferred. stale_reasons says whether any served
   *  object_quality row came from another signature (judged only with currentSignature) and whether the object hints came from an older
   *  object-hints version (judged always); object_hints_served_version names the version served. */
  read(sceneId: string, objectId: string, currentSignature?: string): ObjectHints;
  close(): void;
}
interface StatementLike { get(...params: unknown[]): unknown; all?(...params: unknown[]): unknown }
export interface DatabaseLike { prepare(sql: string): StatementLike; close(): void }
/** One object_quality row per version: the row computed under the caller's model signature first, else any (the index keeps rows
 *  of earlier image versions side by side; ORDER BY the equality puts the matching one on top). */
const HINT_SQL = "SELECT payload, model_signature FROM object_quality WHERE version = ? AND scene_id = ? AND object_id = ? ORDER BY (model_signature = ?) DESC LIMIT 1";
const DECISION_SQL = "SELECT payload FROM decision_policy WHERE version = ? AND scene_id = ? AND object_id = ? LIMIT 1";
/** The one query behind the Machine labelled queue: every decision row of the current and fallback versions; the tier is read from the payload. */
export const DECISION_LIST_SQL = "SELECT scene_id, object_id, version, payload FROM decision_policy WHERE version = ? OR version = ? ORDER BY scene_id, object_id";

/** {hints, class_hint} from an object_quality payload (object or JSON text); malformed parts are dropped, never thrown. */
export function parseClassHints(payload: unknown): ClassHints {
  const doc = typeof payload === "string" ? parseJson(payload) : payload;
  const raw = isRecord(doc) && isRecord(doc.class_hints) ? doc.class_hints : {};
  const hints: Hint[] = [];
  if (Array.isArray(raw.flags)) for (const flag of raw.flags) {
    if (!isRecord(flag) || typeof flag.flag !== "string") continue;
    hints.push({ flag: flag.flag, evidence: isRecord(flag.evidence) ? flag.evidence : {} });
  }
  const hint = text(raw.class_hint);
  return { hints, class_hint: hint && (CLASS_HINTS as readonly string[]).includes(hint) ? hint : null };
}
/** {sub_kind, confidence, rule, evidence} from a structure-kinds payload (object or JSON text); unknown sub-kinds are null, never thrown. */
export function parseStructureKind(payload: unknown): StructureKind | null {
  const doc = typeof payload === "string" ? parseJson(payload) : payload;
  if (!isRecord(doc)) return null;
  const kind = text(doc.sub_kind);
  if (!kind || !(STRUCTURE_KINDS as readonly string[]).includes(kind)) return null;
  const confidence = text(doc.confidence);
  return { sub_kind: kind as StructureSubKind, confidence: confidence && (STRUCTURE_CONFIDENCES as readonly string[]).includes(confidence) ? confidence as StructureConfidence : "weak",
    rule: text(doc.rule) ?? "", evidence: isRecord(doc.evidence) ? doc.evidence : {} };
}
const OBJECT_HINT_SEVERITIES = ["context", "review"] as const;
/** {hints, squared_candidate} from an object-hints payload of any version (object or JSON text, the flat index payload or a route body).
 *  Malformed hints are dropped, a malformed candidate is null; a document with neither a hints array nor a candidate is null, never a throw. */
export function parseObjectHints(payload: unknown): ObjectHintsPayload | null {
  const doc = typeof payload === "string" ? parseJson(payload) : payload;
  if (!isRecord(doc) || (!Array.isArray(doc.hints) && !isRecord(doc.squared_candidate))) return null;
  const hints: ObjectHint[] = [];
  if (Array.isArray(doc.hints)) for (const hint of doc.hints) {
    if (!isRecord(hint) || typeof hint.code !== "string" || !hint.code) continue;
    const severity = text(hint.severity);
    hints.push({ code: hint.code, severity: severity && (OBJECT_HINT_SEVERITIES as readonly string[]).includes(severity) ? severity as ObjectHint["severity"] : "context",
      sentence: text(hint.sentence) ?? "", evidence: isRecord(hint.evidence) ? hint.evidence : {} });
  }
  return { hints, squared_candidate: parseSquaredCandidate(doc.squared_candidate) };
}
/** {ring, iou, hausdorff_m, refused} from a squared-candidate record; a ring needs at least three finite pixel pairs, else it is
 *  null and the record reads as a refusal when it names one. Anything that is not an object is null. */
export function parseSquaredCandidate(value: unknown): SquaredCandidate | null {
  if (!isRecord(value)) return null;
  const pairs = Array.isArray(value.ring) ? value.ring.filter((p): p is [number, number] => Array.isArray(p) && p.length >= 2 && finite(p[0]) !== null && finite(p[1]) !== null).map(p => [p[0], p[1]] as [number, number]) : [];
  const ring = pairs.length >= 3 && Array.isArray(value.ring) && pairs.length === value.ring.length ? pairs : null;
  const refused = text(value.refused);
  if (!ring && !refused) return null;
  return { ring, iou: ring ? unitOrNull(value.iou) : null, hausdorff_m: ring ? nonNegative(value.hausdorff_m) : null, refused: ring ? null : refused };
}
const unitOrNull = (value: unknown): number | null => { const n = finite(value); return n !== null && n >= 0 && n <= 1 ? n : null; };
const nonNegative = (value: unknown): number | null => { const n = finite(value); return n !== null && n >= 0 ? n : null; };
const strings = (value: unknown): string[] => (Array.isArray(value) ? value.filter((v): v is string => typeof v === "string" && v.trim().length > 0) : []);
/** A lot tag from a lot-tags-v1 payload (the index payload {tag, ...} or the route's flat shape). Not an object, or an object
 *  without any of the tag's fields, is null, never a throw. */
export function parseLotTag(payload: unknown): LotTag | null {
  const doc = typeof payload === "string" ? parseJson(payload) : payload;
  if (!isRecord(doc)) return null;
  const tag = isRecord(doc.tag) ? doc.tag : doc;
  if (!("dominant" in tag) && !("lots" in tag) && !("flags" in tag) && !("sentences" in tag) && !("sentence" in tag)) return null;
  const raw = isRecord(tag.dominant) ? tag.dominant : null;
  const rawKind = raw ? text(raw.kind) : null;
  const kind: "lot" | "common_property" | null = rawKind === "lot" || rawKind === "common_property" ? rawKind : null;
  const dominant = raw ? { pfi: text(raw.pfi), lot: text(raw.lot), plan: text(raw.plan), share: unitOrNull(raw.share), kind } : null;
  const lots: LotShare[] = Array.isArray(tag.lots) ? tag.lots.flatMap(lot => (isRecord(lot) && unitOrNull(lot.share) !== null ? [{ lot: text(lot.lot), plan: text(lot.plan), share: unitOrNull(lot.share) as number }] : [])) : [];
  const sentences = strings(tag.sentences);
  const single = text(tag.sentence);
  if (single && !sentences.includes(single)) sentences.push(single);
  return { dominant, lots, flags: strings(tag.flags), sentences, lot_complete_in_image: typeof tag.lot_complete_in_image === "boolean" ? tag.lot_complete_in_image : null, address_plan: text(tag.address_plan) };
}
/** One query behind GET /api/scenes/lots: every lot-tags-v1 row of a scene (object rows and the "_scene" row), all image versions. */
export const LOT_TAG_LIST_SQL = "SELECT object_id, payload, model_signature FROM object_quality WHERE version = ? AND scene_id = ? ORDER BY object_id";
/** The address of a scene as its lot-tags-v1 "_scene" row records it: the lot and plan the address point resolved to (null on a
 *  subject window that has no plan) and the address id. */
export interface SceneLotAddress { lot: string | null; plan: string | null; address_id: number | null }
export interface SceneLotShare extends LotShare { kind: "lot" | "common_property" | null }
/** The part of one object's lot tag the editor's lot scope reads: the dominant lot, the flags and the lot shares with their kind. */
export interface SceneLotEntry { dominant: LotTag["dominant"]; flags: string[]; lots: SceneLotShare[] }
export interface SceneLotTags { address: SceneLotAddress | null; objects: Record<string, SceneLotEntry>; stale: boolean }
const lotKind = (value: unknown): SceneLotShare["kind"] => { const kind = text(value); return kind === "lot" || kind === "common_property" ? kind : null; };
/** The lot id as text: the index records "3" or "CM1"; a number is read as its digits. */
const lotText = (value: unknown): string | null => (typeof value === "number" && Number.isFinite(value) ? String(value) : text(value));
/** {lot, plan, address_id} from a lot-tags-v1 "_scene" payload (object or JSON text); not a scene row is null, never a throw. */
export function parseSceneLotAddress(payload: unknown): SceneLotAddress | null {
  const doc = typeof payload === "string" ? parseJson(payload) : payload;
  if (!isRecord(doc) || !isRecord(doc.scene)) return null;
  const id = typeof doc.scene.address_id === "string" ? Number(doc.scene.address_id) : doc.scene.address_id;
  return { lot: lotText(doc.scene.address_lot), plan: text(doc.scene.address_plan), address_id: Number.isSafeInteger(id) && (id as number) > 0 ? id as number : null };
}
/** {dominant, flags, lots} from a lot-tags-v1 object payload (object or JSON text): parseLotTag's dominant and flags, with the lot
 *  shares read again so each keeps its kind (lot or common property). A payload without a tag is null, never a throw. */
export function parseSceneLotEntry(payload: unknown): SceneLotEntry | null {
  const doc = typeof payload === "string" ? parseJson(payload) : payload;
  if (!isRecord(doc) || !isRecord(doc.tag)) return null;
  const tag = parseLotTag(doc);
  if (!tag) return null;
  const lots: SceneLotShare[] = Array.isArray(doc.tag.lots) ? doc.tag.lots.flatMap(lot => (isRecord(lot) && unitOrNull(lot.share) !== null ? [{ lot: lotText(lot.lot), plan: text(lot.plan), share: unitOrNull(lot.share) as number, kind: lotKind(lot.kind) }] : [])) : [];
  return { dominant: tag.dominant, flags: tag.flags, lots };
}
/** Object ids the lots map may carry as keys: the scene-path character set, so a row named "__proto__" or "a/b" never reaches the map. */
const LOT_MAP_KEY = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$/;
/** Every lot-tags-v1 row of one scene from one query over any object with prepare().all() (node:sqlite DatabaseSync or a test
 *  double; a statement without all() reads as an empty scene). Per object the row computed under currentSignature wins, else the
 *  first seen. The "_scene" row is the address; object rows become the map. stale is true when a served object row came from
 *  another signature (judged only with currentSignature; the scene row describes the frame, not an outline, and never counts).
 *  Malformed payloads and unsafe ids are skipped. Database errors throw; the caller closes the database. */
export function readSceneLotTags(db: DatabaseLike, sceneId: string, currentSignature?: string): SceneLotTags {
  const statement = db.prepare(LOT_TAG_LIST_SQL);
  const rows = typeof statement.all === "function" ? statement.all(LOT_TAGS_VERSION, sceneId) : [];
  const chosen = new Map<string, { payload: string; signature: string | null }>();
  for (const row of Array.isArray(rows) ? rows : []) {
    if (!isRecord(row) || typeof row.object_id !== "string" || typeof row.payload !== "string") continue;
    if (row.object_id !== SCENE_ROW_ID && !LOT_MAP_KEY.test(row.object_id)) continue;
    const signature = typeof row.model_signature === "string" ? row.model_signature : null;
    const existing = chosen.get(row.object_id);
    if (!existing || (!!currentSignature && signature === currentSignature && existing.signature !== currentSignature)) chosen.set(row.object_id, { payload: row.payload, signature });
  }
  let address: SceneLotAddress | null = null, stale = false;
  const objects: Record<string, SceneLotEntry> = {};
  for (const [objectId, row] of chosen) {
    const doc = parseJson(row.payload);
    if (objectId === SCENE_ROW_ID) { address = parseSceneLotAddress(doc); continue; }
    const entry = parseSceneLotEntry(doc);
    if (!entry) continue;
    objects[objectId] = entry;
    if (currentSignature && row.signature !== null && row.signature !== currentSignature) stale = true;
  }
  return { address, objects, stale };
}
/** A lots route body read back on the client: null when the index was unavailable or the body is not the route's shape; the
 *  address and each object entry are re-parsed, so a malformed entry is dropped rather than trusted and unsafe keys never land. */
export function coerceSceneLotTags(document: unknown): SceneLotTags | null {
  if (!isRecord(document) || document.source !== "eyes-index" || !isRecord(document.objects)) return null;
  const address = isRecord(document.address) ? parseSceneLotAddress({ scene: { address_lot: document.address.lot, address_plan: document.address.plan, address_id: document.address.address_id } }) : null;
  const objects: Record<string, SceneLotEntry> = {};
  for (const [objectId, entry] of Object.entries(document.objects)) {
    if (!LOT_MAP_KEY.test(objectId)) continue;
    const parsed = parseSceneLotEntry({ tag: entry });
    if (parsed) objects[objectId] = parsed;
  }
  return { address, objects, stale: document.stale === true };
}
/** The lot-frame plan sentence of a lot-tags-v1 "_scene" payload (scene.lot_frame.sentence), or null; a recorded error is not a sentence. */
export function parseLotFrameSentence(payload: unknown): string | null {
  const doc = typeof payload === "string" ? parseJson(payload) : payload;
  if (!isRecord(doc) || !isRecord(doc.scene) || !isRecord(doc.scene.lot_frame)) return null;
  return text(doc.scene.lot_frame.sentence);
}
/** The lot tag's sentences as one line, or null when it has none. */
export const lotSentence = (lot: LotTag | null | undefined): string | null => (lot && lot.sentences.length ? lot.sentences.map(s => s.trim().replace(/\.?$/, ".")).join(" ") : null);
/** Object-hint sentences in reading order: review severity first, then context; blank sentences dropped, repeats removed, each ending in a full stop. */
export function objectHintSentences(hints: readonly ObjectHint[] | null | undefined): string[] {
  const ordered = [...(hints ?? [])].sort((a, b) => Number(b.severity === "review") - Number(a.severity === "review"));
  const out: string[] = [];
  for (const hint of ordered) { const sentence = hint.sentence.trim(); if (sentence && !out.includes(sentence.replace(/\.?$/, "."))) out.push(sentence.replace(/\.?$/, ".")); }
  return out;
}
/** {tier, confidence, machine_label, reasons} from a decision_policy payload (object or JSON text). The tier must be auto, assist
 *  or review and the confidence a number from 0 to 1, or the row is null; label fields and reasons degrade to null or [], never throw. */
export function parseDecision(payload: unknown): Decision | null {
  const doc = typeof payload === "string" ? parseJson(payload) : payload;
  if (!isRecord(doc)) return null;
  const tier = text(doc.tier);
  if (!tier || !(DECISION_TIERS as readonly string[]).includes(tier)) return null;
  const confidence = finite(doc.confidence);
  if (confidence === null || confidence < 0 || confidence > 1) return null;
  const raw = isRecord(doc.machine_label) ? doc.machine_label : {};
  const form = text(raw.roof_form);
  const machine_label: MachineLabel = { kind: text(raw.kind) ?? "", structure_sub_kind: text(raw.structure_sub_kind), class_hint: text(raw.class_hint),
    roof_form: form === "flat" || form === "pitched" ? form : null, roof_type: text(raw.roof_type) };
  const reasons = Array.isArray(doc.reasons) ? doc.reasons.filter((r): r is string => typeof r === "string" && r.length > 0) : [];
  // An index payload records the review under signals.review; a route body (the parsed shape, serialised) carries it as review.
  const review = isRecord(doc.review) ? doc.review : isRecord(doc.signals) && isRecord(doc.signals.review) ? doc.signals.review : null;
  return { tier: tier as DecisionTier, confidence, machine_label, reasons, interval: parseAgreementInterval(doc.interval), score_interval: parseScoreInterval(doc.score_interval),
    review: review ? { status: text(review.status) } : null };
}
const unit = (value: unknown): number | null => { const n = finite(value); return n !== null && n >= 0 && n <= 1 ? n : null; };
const count = (value: unknown): number => (Number.isInteger(value) && (value as number) >= 0 ? (value as number) : 0);
/** {kind, stratum, confirmed, judged, lower, upper, note} from a v2 interval; bounds are null unless both are numbers in [0, 1] in order and
 *  something was judged. Anything that is not an object is null, never a throw. */
export function parseAgreementInterval(value: unknown): AgreementInterval | null {
  if (!isRecord(value)) return null;
  const judged = count(value.judged), confirmed = Math.min(count(value.confirmed), judged);
  const lower = judged ? unit(value.lower) : null, upper = judged ? unit(value.upper) : null;
  const both = lower !== null && upper !== null && lower <= upper;
  return { kind: text(value.kind) ?? "wilson-95", stratum: text(value.stratum) ?? "", confirmed, judged, lower: both ? lower : null, upper: both ? upper : null, note: text(value.note) ?? "" };
}
/** {lower, upper, note} from a v2 score interval; null unless both bounds are numbers in [0, 1] in order. */
export function parseScoreInterval(value: unknown): ScoreInterval | null {
  if (!isRecord(value)) return null;
  const lower = unit(value.lower), upper = unit(value.upper);
  if (lower === null || upper === null || lower > upper) return null;
  return { lower, upper, note: text(value.note) ?? "" };
}
/** Sentence-case words from a machine-readable token: "shipping-container" or "non_residential" read as "Shipping container". */
const sentence = (token: string): string => { const w = token.replace(/[-_]+/g, " ").trim(); return w ? w.charAt(0).toUpperCase() + w.slice(1) : w; };
/** The object label a machine label proposes, in sentence case: the structure sub-kind first, then the class hint, then the kind;
 *  null when the label says nothing. */
export function machineLabelName(label: MachineLabel): string | null {
  const sub = label.structure_sub_kind;
  if (sub && !["outbuilding", "not-secondary"].includes(sub)) return STRUCTURE_LABELS[sub as StructureSubKind] ?? sentence(sub);
  if (label.class_hint) return CLASS_HINT_LABELS[label.class_hint] ?? sentence(label.class_hint);
  if (sub) return sentence(sub);
  return label.kind ? sentence(label.kind) : null;
}
/** Object labels for the class hints (hyphenated spellings kept). */
const CLASS_HINT_LABELS: Record<string, string> = { house: "House", units: "Units", commercial: "Commercial", outbuilding: "Outbuilding", "tree-like": "Tree-like", "non-residential": "Non-residential" };
const confidenceText = (value: number) => value.toFixed(2);
const lowerName = (decision: Decision) => (machineLabelName(decision.machine_label) ?? "unspecified").toLowerCase();
const number = (n: number) => n.toLocaleString("en-AU");
/** " (model range 0.86 to 0.95)" when the row carries a score interval; "" for v1 rows. */
export const modelRangeText = (decision: Decision): string => (decision.score_interval ? ` (model range ${confidenceText(decision.score_interval.lower)} to ${confidenceText(decision.score_interval.upper)})` : "");
/** "6 of 9 confirmed, 95% interval 0.35 to 0.88" or "not yet judged by a reviewer"; null for v1 rows without an interval. */
export function agreementText(decision: Decision): string | null {
  const interval = decision.interval;
  if (!interval) return null;
  if (!interval.judged || interval.lower === null || interval.upper === null) return "not yet judged by a reviewer";
  const kind = interval.kind === "wilson-95" ? "95% interval" : `${interval.kind.replace(/[-_]+/g, " ")} interval`;
  return `${number(interval.confirmed)} of ${number(interval.judged)} confirmed, ${kind} ${confidenceText(interval.lower)} to ${confidenceText(interval.upper)}`;
}
/** "Human agreement for auto/garage: 6 of 9 confirmed, 95% interval 0.35 to 0.88." (the stratum as the policy named it); null without an interval. */
export function agreementSentence(decision: Decision): string | null {
  const agreement = agreementText(decision);
  if (!agreement) return null;
  return `Human agreement for ${decision.interval?.stratum || `${decision.tier}/${lowerName(decision)}`}: ${agreement}.`;
}
/** "Machine label: garage; auto tier, confidence 0.91 (model range 0.86 to 0.95). Human agreement for auto/garage: 6 of 9 confirmed,
 *  95% interval 0.35 to 0.88. Reasons: small footprint beside the driveway." Plain words, for a tooltip. */
export function describeDecision(decision: Decision): string {
  const parts = [`Machine label: ${lowerName(decision)}; ${decision.tier} tier, confidence ${confidenceText(decision.confidence)}${modelRangeText(decision)}.`];
  const agreement = agreementSentence(decision);
  if (agreement) parts.push(agreement);
  if (decision.reasons.length) parts.push(`Reasons: ${decision.reasons.join(", ").replace(/\.+$/, "")}.`);
  return parts.join(" ");
}
/** The editor's sentences: "Machine label: garage, auto tier, confidence 0.91 (model range 0.86 to 0.95). Human agreement for auto/garage: ..." */
export function describeDecisionLine(decision: Decision): string {
  const head = `Machine label: ${lowerName(decision)}, ${decision.tier} tier, confidence ${confidenceText(decision.confidence)}${modelRangeText(decision)}.`;
  const agreement = agreementSentence(decision);
  return agreement ? `${head} ${agreement}` : head;
}
/** One quiet tier badge for a row with a decision: the title carries the machine label and the reasons. */
export function decisionBadge(decision: Decision): HintBadge {
  return { key: `tier-${decision.tier}`, label: TIER_LABELS[decision.tier], group: "decision", title: `Machine label, not a reviewer label. ${describeDecision(decision)}` };
}
/** Rows whose decision puts them in the auto tier; /queues can hide these once the reviewer decides to. */
export const isAutoTier = (row: Pick<ClassHints, "decision">) => row.decision?.tier === "auto";
/** "Machine labels: 3 auto, 1 assist, 2 review" over a set of rows (every tier, zeros included so the denominators add up), or null when no row has one. */
export function decisionSummary(rows: readonly Pick<ClassHints, "decision">[]): string | null {
  const counts: Record<DecisionTier, number> = { auto: 0, assist: 0, review: 0 };
  let total = 0;
  for (const row of rows) if (row.decision) { counts[row.decision.tier]++; total++; }
  return total ? `Machine labels: ${DECISION_TIERS.map(tier => `${counts[tier]} ${tier}`).join(", ")}` : null;
}
/** True when a reviewer has already shaped this object's roof (line edits, an annotation, roof feedback or a reviewer template);
 *  "Use machine label" then leaves roof_type alone. Loose shape so the editor and tests can pass any object. */
export function hasReviewerRoofEdits(object: { roof_line_edits?: unknown; roof_annotation?: unknown; roof_feedback?: unknown; roof_geometry_source?: unknown }): boolean {
  return object.roof_line_edits != null || object.roof_annotation != null || object.roof_feedback != null || object.roof_geometry_source === "reviewer-template";
}
/** The sub-kind label when the structure hint says something a reviewer may act on; null for outbuilding, not-secondary or no hint. */
export const structureLabel = (structure: StructureKind | null | undefined): string | null => (structure ? STRUCTURE_LABELS[structure.sub_kind] ?? null : null);
export const isFlagged = (row: ClassHints) => row.hints.length > 0 || row.class_hint === "commercial" || structureLabel(row.structure) !== null;

const metres = (value: unknown) => (typeof value === "number" && Number.isFinite(value) ? `${Math.round(value)} m` : null);
const area = (value: unknown) => (typeof value === "number" && Number.isFinite(value) ? `${Math.round(value)} m²` : "area unknown");
const num = (value: unknown, digits = 2) => (typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "not measured");
const words = (value: unknown) => (typeof value === "string" ? value.replace(/_/g, " ") : "");
const titleCase = (value: string) => value.split(" ").map(w => (w === w.toUpperCase() && /[A-Z]/.test(w) ? w.charAt(0) + w.slice(1).toLowerCase() : w)).join(" ");
/** Plain-words evidence for one hint, for a badge tooltip. Numbers keep their units; nothing here is a verdict. */
export function describeHint(hint: Hint): string {
  const e = hint.evidence;
  if (hint.flag === "tree-like") return `green share ${num(e.exg_green_share)} of pixels, solidity ${num(e.solidity)}, tree overlap IoU ${num(e.tree_overlap_iou)}, texture ${num(e.texture, 0)}`;
  if (hint.flag === "secondary-structure") {
    const where = e.source === "vicmap-property" ? `largest roof on the same property (Vicmap property ${e.property_pfi ?? "unknown"})` : "largest roof within 25 m (no property match)";
    return `${area(e.area_m2)} vs ${area(e.largest_same_property_area)} ${where}, ratio ${num(e.ratio, 1)}`;
  }
  if (hint.flag === "non-residential-context") {
    const parts: string[] = [];
    if (Array.isArray(e.poi_within_30m)) for (const poi of e.poi_within_30m.slice(0, 3)) if (isRecord(poi)) parts.push(`${poi.name ?? "unnamed place"} (${words(poi.category) || "uncategorised"}) ${metres(poi.distance_m) ?? ""}`.trim());
    if (Array.isArray(e.landuse)) for (const use of e.landuse.slice(0, 3)) if (isRecord(use)) parts.push([use.tag, use.name, metres(use.distance_m)].filter(Boolean).join(" "));
    if (typeof e.address_kind === "string") parts.push(`address kind ${e.address_kind}`);
    if (Array.isArray(e.site_names) && e.site_names.length) parts.push(`site name ${e.site_names.filter((n): n is string => typeof n === "string").map(titleCase).join(", ")}`);
    if (e.landuse_status === "unavailable") parts.push("land use unavailable offline");
    if (e.landuse_status === "outside coverage") parts.push("outside the land-use and places coverage area");
    return parts.join("; ") || "context evidence recorded in the index";
  }
  return Object.entries(e).filter(([k]) => k !== "rule").map(([k, v]) => `${words(k)} ${typeof v === "number" ? num(v) : String(v)}`).join(", ");
}
const sub = (value: unknown, key: string): Record<string, unknown> => (isRecord(value) && isRecord(value[key]) ? value[key] : {});
/** SAM3 "house" prompt score when the structure evidence carries one (prompt names vary; the first containing "house" wins). */
function sam3HouseScore(evidence: Record<string, unknown>): number | null {
  const prompts = sub(evidence.sam3, "prompts");
  for (const [name, value] of Object.entries(prompts)) if (/house/i.test(name) && isRecord(value)) { const score = finite(value.score) ?? finite(value.best_iou); if (score !== null) return score; }
  return null;
}
/** The first finite value among candidate evidence keys (v2 payloads may nest a measure under geometry, appearance or context). */
const firstFinite = (...values: unknown[]): number | null => { for (const value of values) { const n = finite(value); if (n !== null) return n; } return null; };
/** Plain-words evidence for a structure hint: size, ribbing, see-through ratio, attachment and the SAM3 house score when present.
 *  Kind-specific measures (structure-kinds-v2): container widths for a stack, the blue share for a pool cover, the driveway share for a carport. */
export function describeStructure(structure: StructureKind): string {
  const geometry = sub(structure.evidence, "geometry"), appearance = sub(structure.evidence, "appearance"), context = sub(structure.evidence, "context");
  const parts: string[] = [];
  const width = finite(geometry.rect_width_m), length = finite(geometry.rect_length_m);
  if (width !== null && length !== null) parts.push(`${width.toFixed(1)} by ${length.toFixed(1)} m`);
  else if (finite(geometry.area_m2) !== null) parts.push(area(geometry.area_m2));
  if (structure.sub_kind === "container-stack") {
    const widths = firstFinite(geometry.container_widths, structure.evidence.container_widths, context.container_widths) ?? (width !== null ? width / 2.44 : null);
    if (widths !== null) { const n = Math.max(1, Math.round(widths)); parts.push(`about ${n} container ${n === 1 ? "width" : "widths"}`); }
  }
  if (structure.sub_kind === "pool-cover") {
    const blue = firstFinite(appearance.blue_share, structure.evidence.blue_share, appearance.blue_share_median);
    if (blue !== null) parts.push(`blue share ${num(blue)} of pixels`);
  }
  if (structure.sub_kind === "carport") {
    const driveway = firstFinite(appearance.driveway_like_share_3m, context.driveway_like_share_3m, appearance.driveway_share, context.driveway_share, structure.evidence.driveway_share);
    if (driveway !== null) parts.push(`driveway-like share ${num(driveway)} within 3 m`);
  }
  if (finite(appearance.rib_anisotropy) !== null) parts.push(`rib anisotropy ${num(appearance.rib_anisotropy)}`);
  if (finite(appearance.see_through_ratio) !== null) parts.push(`see-through ratio ${num(appearance.see_through_ratio)}`);
  if (typeof context.attached_to_larger_roof === "boolean") parts.push(context.attached_to_larger_roof ? "attached to a larger roof" : "not attached to a larger roof");
  const house = sam3HouseScore(structure.evidence);
  if (house !== null) parts.push(`SAM3 house score ${num(house)}`);
  return parts.join(", ") || (structure.rule ? `rule ${structure.rule}` : "evidence recorded in the index");
}
/** Badge labels for the object-hint codes /queues shows; the other codes stay in the editor's Why-flagged list only. */
export const OBJECT_HINT_LABELS: Record<string, string> = { frame_cut: "Frame cut", noisy_outline: "Noisy outline", pool_not_blue: "Pool reads green", building_blends_into_surroundings: "Blends into ground", canopy_fragment_inside: "Canopy piece", secondary_by_lot: "Secondary on lot", spans_lots: "Spans lots", solar_fixture_like: "Panel reads bright or irregular" };
/** One quiet badge per flag, Commercial when the class hint says so, one structure badge for a resolved sub-kind and one badge per
 *  object-hint code in OBJECT_HINT_LABELS; titles carry the evidence or the module's sentence, and object-hint titles name an older
 *  object-hints version when the hints were served from one. */
export function hintBadges(row: ClassHints): HintBadge[] {
  const out: HintBadge[] = row.hints.map(hint => ({ key: hint.flag, label: HINT_LABELS[hint.flag] ?? hint.flag, title: `Agent hint, not a label. ${describeHint(hint)}.`, group: "class" }));
  if (row.class_hint === "commercial") {
    const size = row.hints.map(h => h.evidence.area_m2).find(v => typeof v === "number");
    out.push({ key: "commercial", label: HINT_LABELS.commercial, group: "class",
      title: `Agent hint, not a label. ${size === undefined ? "Roof of 400 m² or more" : `Roof of ${area(size)}`} with non-residential context or a business/large address kind.` });
  }
  const label = structureLabel(row.structure);
  if (row.structure && label) out.push({ key: row.structure.sub_kind, label, group: "structure",
    title: `Agent hint, not a label. ${label}, ${row.structure.confidence} confidence: ${describeStructure(row.structure)}.` });
  const older = row.object_hints_served_version && row.object_hints_served_version !== OBJECT_HINTS_VERSION ? row.object_hints_served_version : null;
  for (const hint of row.object_hints?.hints ?? []) {
    const badgeLabel = OBJECT_HINT_LABELS[hint.code];
    if (badgeLabel && !out.some(b => b.key === hint.code)) out.push({ key: hint.code, label: badgeLabel, group: "object",
      title: `Agent hint, not a label${older ? `, from earlier hint rules (${older})` : ""}. ${hint.sentence.trim() || hint.code.replace(/_/g, " ")}` });
  }
  if (row.decision) out.push(decisionBadge(row.decision));
  return out;
}
/** "Structure hints: 2 shipping containers, 1 container stack, 1 shed" over a set of rows (only non-zero kinds, every v2 kind), or null when there are none. */
export function structureSummary(rows: readonly ClassHints[]): string | null {
  const counts = new Map<StructureSubKind, number>();
  for (const row of rows) if (row.structure && STRUCTURE_LABELS[row.structure.sub_kind]) counts.set(row.structure.sub_kind, (counts.get(row.structure.sub_kind) ?? 0) + 1);
  const parts = (Object.keys(STRUCTURE_PLURALS) as StructureSubKind[]).filter(kind => counts.get(kind)).map(kind => { const n = counts.get(kind) ?? 0, [one, many] = STRUCTURE_PLURALS[kind] ?? [kind, kind]; return `${n} ${n === 1 ? one : many}`; });
  return parts.length ? `Structure hints: ${parts.join(", ")}` : null;
}
/** The editor's read-only line: class hints and the structure sub-kind with its confidence, in plain words, then the object-hint
 *  sentences (review severity first, then context), then the lot sentence, closed by "Not labels; change the Class above if you
 *  disagree."; the machine label sentence follows when a decision exists. Null when there is nothing to say. Quiet copy: no
 *  separators, nothing shouted. */
export function describeObjectHints(hints: Pick<ObjectHints, "class_hints" | "structure" | "decision"> & Partial<Pick<ObjectHints, "object_hints" | "lot">>): string | null {
  const parts = hintBadges({ hints: hints.class_hints?.hints ?? [], class_hint: hints.class_hints?.class_hint ?? null }).map(badge => badge.label);
  const label = structureLabel(hints.structure);
  if (hints.structure && label) parts.push(`${label} (${hints.structure.confidence})`);
  const head = [...(parts.length ? [`${parts.join(". ")}.`] : []), ...objectHintSentences(hints.object_hints?.hints), ...(lotSentence(hints.lot) ? [lotSentence(hints.lot) as string] : [])];
  const sentences = head.length ? [`${head.join(" ")} Not labels; change the Class above if you disagree.`] : [];
  if (hints.decision) sentences.push(describeDecisionLine(hints.decision));
  return sentences.length ? sentences.join(" ") : null;
}
const NO_HINTS: ObjectHints = { class_hints: null, structure: null, decision: null, object_hints: null, lot: null, stale: false, stale_reasons: [], object_hints_served_version: null, lot_frame: null };
/** An object-hints version string ("object-hints-v<n>"), or null for anything else. */
const objectHintsVersion = (value: unknown): string | null => { const version = text(value); return version && OBJECT_HINTS_VERSION_PATTERN.test(version) ? version : null; };
/** Coerce a GET /api/scenes/hints body (or any document) to ObjectHints; malformed parts are null, unknown stale reasons are dropped, never thrown. */
export function coerceObjectHints(document: unknown): ObjectHints {
  if (!isRecord(document)) return { ...NO_HINTS, stale_reasons: [] };
  const raw = document.class_hints;
  const class_hints = isRecord(raw) ? parseClassHints({ class_hints: { flags: raw.hints, class_hint: raw.class_hint } }) : null;
  const reasons = Array.isArray(document.stale_reasons) ? document.stale_reasons : [];
  return { class_hints, structure: parseStructureKind(document.structure), decision: parseDecision(document.decision),
    object_hints: parseObjectHints(document.object_hints), lot: parseLotTag(document.lot), stale: document.stale === true,
    stale_reasons: HINT_STALE_REASONS.filter(reason => reasons.includes(reason)), object_hints_served_version: objectHintsVersion(document.object_hints_served_version),
    lot_frame: text(document.lot_frame) };
}
/** The editor's stale notices for one hints answer and the inspected object's kind, one per reason in HINT_STALE_REASONS order: the
 *  image-version notice, then the rules notice when the object hints came from an older object-hints version whose rules differ for
 *  this kind (OBJECT_HINTS_RULE_CHANGES; every kind when the version or the kind is not known). An answer without reasons (from a server
 *  before stale_reasons) reads as the image-version notice. Plain text; the route's stale flag and reasons stay as served. */
export function hintStaleNotes(hints: Pick<ObjectHints, "stale"> & Partial<Pick<ObjectHints, "stale_reasons" | "object_hints_served_version">>, kind?: string | null): string[] {
  const reasons: readonly HintStaleReason[] = hints.stale_reasons?.length ? hints.stale_reasons : hints.stale ? ["model_signature"] : [];
  const notes: string[] = [];
  if (reasons.includes("model_signature")) notes.push("Hints computed on an earlier image version.");
  if (reasons.includes("object_hints_version")) {
    const served = hints.object_hints_served_version || "an earlier version";
    const change = OBJECT_HINTS_RULE_CHANGES[served];
    if (!change || !kind || change.kinds.includes(kind)) notes.push(`Object hints predate the current rules (${served})${change ? `: ${change.missing} is not included` : ""}.`);
  }
  return notes;
}
/** The hint rows of one object over any object with prepare().get() (node:sqlite DatabaseSync or a test double): one prepared
 *  statement per object_quality version and a third over decision_policy, each prepared lazily so a table that does not exist
 *  yet only fails the query that needs it. */
export function indexHintReader(db: DatabaseLike): HintReader {
  const statements = new Map<string, StatementLike>();
  const statement = (sql: string, version: string): StatementLike => {
    let prepared = statements.get(version);
    if (!prepared) { prepared = db.prepare(sql); statements.set(version, prepared); }
    return prepared;
  };
  /** One object_quality row: its parsed payload and the model signature it was computed under (null on a double without the column). */
  const hintRow = (version: string, sceneId: string, objectId: string, currentSignature = ""): { doc: unknown; signature: string | null } | null => {
    const row = statement(HINT_SQL, version).get(version, sceneId, objectId, currentSignature);
    if (!isRecord(row) || typeof row.payload !== "string") return null;
    return { doc: parseJson(row.payload), signature: typeof row.model_signature === "string" ? row.model_signature : null };
  };
  const decisionPayload = (version: string, sceneId: string, objectId: string): unknown => {
    const row = statement(DECISION_SQL, version).get(version, sceneId, objectId);
    return isRecord(row) && typeof row.payload === "string" ? parseJson(row.payload) : null;
  };
  /** Signatures of the rows served in one read(); stale compares them with the caller's. */
  const servedSignatures: (string | null)[] = [];
  /** The object-hints version the object hints of one read() came from; an older one makes the answer stale too. */
  let servedObjectHintsVersion: string | null = null;
  const classHints = (sceneId: string, objectId: string, currentSignature?: string) => {
    const row = hintRow(HINTS_VERSION, sceneId, objectId, currentSignature);
    if (!row || !isRecord(row.doc)) return null;
    servedSignatures.push(row.signature);
    return parseClassHints(row.doc);
  };
  const structure = (sceneId: string, objectId: string, currentSignature?: string) => {
    for (const version of [STRUCTURE_VERSION, ...STRUCTURE_FALLBACK_VERSIONS]) {
      const row = hintRow(version, sceneId, objectId, currentSignature);
      const parsed = row ? parseStructureKind(row.doc) : null;
      if (parsed) { servedSignatures.push(row!.signature); return parsed; }
    }
    return null;
  };
  // The current object-hints version first, then the older ones: nothing vanishes while an index is recomputed, and an older row
  // marks the answer stale (it predates the current rules).
  const objectHintsServed = (sceneId: string, objectId: string, currentSignature?: string) => {
    for (const version of [OBJECT_HINTS_VERSION, ...OBJECT_HINTS_FALLBACK_VERSIONS]) {
      const row = hintRow(version, sceneId, objectId, currentSignature);
      const payload = row ? parseObjectHints(row.doc) : null;
      if (payload) { servedSignatures.push(row!.signature); servedObjectHintsVersion = version; return { payload, version }; }
    }
    return null;
  };
  const objectHints = (sceneId: string, objectId: string, currentSignature?: string) => objectHintsServed(sceneId, objectId, currentSignature)?.payload ?? null;
  const lot = (sceneId: string, objectId: string, currentSignature?: string) => {
    const row = hintRow(LOT_TAGS_VERSION, sceneId, objectId, currentSignature);
    const parsed = row ? parseLotTag(row.doc) : null;
    if (parsed) servedSignatures.push(row!.signature);
    return parsed;
  };
  // The scene row describes the frame, not an outline: its signature never marks an object's hints stale.
  const lotFrame = (sceneId: string, currentSignature?: string) => {
    const row = hintRow(LOT_TAGS_VERSION, sceneId, SCENE_ROW_ID, currentSignature);
    return row ? parseLotFrameSentence(row.doc) : null;
  };
  const reader: HintReader = {
    classHints, structure, objectHints, objectHintsServed, lot, lotFrame,
    decision(sceneId, objectId) {
      for (const version of [DECISION_VERSION, ...DECISION_FALLBACK_VERSIONS]) { const decision = parseDecision(decisionPayload(version, sceneId, objectId)); if (decision) return decision; }
      return null;
    },
    read(sceneId, objectId, currentSignature) {
      let decision: Decision | null;
      try { decision = reader.decision(sceneId, objectId); } catch { decision = null; }
      servedSignatures.length = 0; servedObjectHintsVersion = null;
      const class_hints = classHints(sceneId, objectId, currentSignature), kind = structure(sceneId, objectId, currentSignature);
      // The two newer tables of hints are read on their own: an index built before their modules ran must not hide the rest.
      let hints: ObjectHintsPayload | null, tag: LotTag | null;
      try { hints = objectHints(sceneId, objectId, currentSignature); } catch { hints = null; }
      try { tag = lot(sceneId, objectId, currentSignature); } catch { tag = null; }
      const stale_reasons: HintStaleReason[] = [];
      if (currentSignature && servedSignatures.some(signature => signature !== null && signature !== currentSignature)) stale_reasons.push("model_signature");
      if (servedObjectHintsVersion && servedObjectHintsVersion !== OBJECT_HINTS_VERSION) stale_reasons.push("object_hints_version");
      return { class_hints, structure: kind, decision, object_hints: hints, lot: tag, stale: stale_reasons.length > 0, stale_reasons, object_hints_served_version: servedObjectHintsVersion };
    },
    close() { try { db.close(); } catch { /* already closed */ } },
  };
  return reader;
}
/** A hint source over a reader: class hints plus `structure`, `decision` and `object_hints` (with `object_hints_served_version`) only
 *  when the index holds those rows. Each query is guarded on its own, so a failing structure or decision query never hides class hints
 *  (and the reverse); database errors read as no hint. */
export function indexHintSource(db: DatabaseLike): HintSource {
  const reader = indexHintReader(db);
  function guarded<T>(read: () => T | null): T | null { try { return read(); } catch { return null; } }
  return {
    hintFor(sceneId, objectId) {
      const classHints = guarded(() => reader.classHints(sceneId, objectId)), structure = guarded(() => reader.structure(sceneId, objectId)), decision = guarded(() => reader.decision(sceneId, objectId)),
        objectHints = guarded(() => reader.objectHintsServed(sceneId, objectId));
      if (!classHints && !structure && !decision && !objectHints) return null;
      const hints: ClassHints = { ...(classHints ?? { hints: [], class_hint: null }) };
      if (structure) hints.structure = structure;
      if (decision) hints.decision = decision;
      if (objectHints) { hints.object_hints = objectHints.payload; hints.object_hints_served_version = objectHints.version; }
      return hints;
    },
    close: reader.close,
  };
}
/** Open the eyes index read-only through node:sqlite (resolved at call time); null when unavailable. */
export function sqliteHintSource(file: string): HintSource | null {
  try {
    const getBuiltin = (globalThis as { process?: { getBuiltinModule?: (id: string) => unknown } }).process?.getBuiltinModule;
    const sqlite = typeof getBuiltin === "function" ? getBuiltin("node:sqlite") as { DatabaseSync?: new (file: string, options: { readOnly: boolean }) => DatabaseLike } | undefined : undefined;
    if (!sqlite?.DatabaseSync) return null;
    return indexHintSource(new sqlite.DatabaseSync(file, { readOnly: true }));
  } catch { return null; }
}
const serverHintSource = (): HintSource | null => {
  const proc = (globalThis as { process?: { cwd?: () => string } }).process;
  return typeof proc?.cwd === "function" ? sqliteHintSource(`${proc.cwd()}/${EYES_INDEX}`) : null;
};
/** Rows gain hints + class_hint (and structure) only when the index holds a hint row for them; the source is closed afterwards. */
export function attachHints<T extends { scene_id: string; object_id: string }>(rows: T[], source: HintSource | null): (T & Partial<ClassHints>)[] {
  if (!source) return rows;
  try {
    return rows.map(row => { const found = source.hintFor(row.scene_id, row.object_id); return found ? { ...row, ...found } : row; });
  } finally { source.close(); }
}
/** Hints keyed by scene/object from an API document's rows, for the client after the shared coercers dropped them. */
export function pickHints(document: unknown): Map<string, ClassHints> {
  const out = new Map<string, ClassHints>();
  if (!isRecord(document) || !Array.isArray(document.rows)) return out;
  for (const row of document.rows) {
    if (!isRecord(row) || typeof row.scene_id !== "string" || typeof row.object_id !== "string" || (row.hints === undefined && row.class_hint === undefined && row.structure === undefined && row.decision === undefined && row.object_hints === undefined)) continue;
    const hints: ClassHints = parseClassHints({ class_hints: { flags: row.hints, class_hint: row.class_hint } }), structure = parseStructureKind(row.structure), decision = parseDecision(row.decision), objectHints = parseObjectHints(row.object_hints);
    if (structure) hints.structure = structure;
    if (decision) hints.decision = decision;
    if (objectHints) {
      hints.object_hints = objectHints;
      const served = objectHintsVersion(row.object_hints_served_version);
      if (served) hints.object_hints_served_version = served;
    }
    out.set(`${row.scene_id}/${row.object_id}`, hints);
  }
  return out;
}

// --- Machine labelled roofs (the auto tier of decision_policy) ---------------------------------------------

/** Plain words for the review status the policy recorded, and whether a person made it. */
export const REVIEW_STATUS_TEXT: Record<string, { text: string; by_person: boolean }> = {
  confirmed: { text: "Confirmed by reviewer", by_person: true }, needs_adjustment: { text: "Needs edit", by_person: true },
  rejected: { text: "Rejected by reviewer", by_person: true }, superseded: { text: "Replaced by reviewer", by_person: true }, candidate: { text: "Candidate", by_person: false },
};
/** The review-status cell: "Confirmed by machine" when no reviewer decision was recorded for an auto row; null when the row carries no review data at all. */
export function reviewStatusText(decision: Decision): { text: string; by_person: boolean } | null {
  if (!decision.review) return null;
  if (decision.review.status === null) return { text: "Confirmed by machine", by_person: false };
  return REVIEW_STATUS_TEXT[decision.review.status] ?? { text: decision.review.status.replace(/_/g, " "), by_person: false };
}
export interface MachineLabelledRow {
  scene_id: string;
  object_id: string;
  /** The decision row's version (v2, or v1 while a recompute is under way). */
  version: string;
  decision: Decision;
  /** The label the policy proposes, sentence case, as "Use machine label" would apply it. */
  label: string | null;
  review_url: string;
  /** Null when the decision row recorded no review data (the column is then blank for this row). */
  review_status: string | null;
  reviewed_by_person: boolean;
  hints?: Hint[];
  class_hint?: string | null;
  structure?: StructureKind | null;
}
export interface MachineLabelledQueue {
  version: string;
  /** Versions read, current first. */
  versions: string[];
  count: number;
  reviewed_by_person: number;
  rows: MachineLabelledRow[];
  note: string;
}
/** The auto tier over the raw rows of DECISION_LIST_SQL (any objects with scene_id, object_id, version, payload). A v2 row wins over a v1 row
 *  for the same object; malformed payloads and other tiers are skipped, never thrown. Class and structure hints attach through the source when given
 *  (the source is closed afterwards); the decision itself always comes from the list query. */
export function machineLabelledQueue(indexRows: unknown, hints: HintSource | null = null): MachineLabelledQueue {
  const versions = [DECISION_VERSION, ...DECISION_FALLBACK_VERSIONS];
  const best = new Map<string, { scene_id: string; object_id: string; version: string; decision: Decision }>();
  if (Array.isArray(indexRows)) for (const raw of indexRows) {
    if (!isRecord(raw)) continue;
    const sceneId = text(raw.scene_id), objectId = text(raw.object_id), version = text(raw.version);
    if (!sceneId || !objectId || !version || !versions.includes(version)) continue;
    const decision = parseDecision(raw.payload);
    if (!decision || decision.tier !== "auto") continue;
    const key = `${sceneId}/${objectId}`, current = best.get(key);
    if (current && versions.indexOf(current.version) <= versions.indexOf(version)) continue;
    best.set(key, { scene_id: sceneId, object_id: objectId, version, decision });
  }
  const rows: MachineLabelledRow[] = [...best.values()].map(entry => {
    const status = reviewStatusText(entry.decision);
    return { ...entry, label: machineLabelName(entry.decision.machine_label), review_url: reviewUrl(entry.scene_id, entry.object_id), review_status: status?.text ?? null, reviewed_by_person: status?.by_person ?? false };
  });
  const attached = attachHints(rows, hints).map((row, i) => ({ ...row, decision: rows[i].decision }));
  return { version: DECISION_VERSION, versions, count: attached.length, reviewed_by_person: attached.filter(row => row.reviewed_by_person).length, rows: attached, note: MACHINE_LABELLED_NOTE };
}
/** Coerce a GET /api/queues/machine-labelled body (or any document) to a MachineLabelledQueue; malformed rows are dropped, never thrown. Throws only when there are no rows at all. */
export function coerceMachineLabelledQueue(document: unknown): MachineLabelledQueue {
  if (!isRecord(document) || !Array.isArray(document.rows)) throw new Error("The machine-labelled document has no rows");
  const rows: MachineLabelledRow[] = [];
  for (const raw of document.rows) {
    if (!isRecord(raw)) continue;
    const sceneId = text(raw.scene_id), objectId = text(raw.object_id), decision = parseDecision(raw.decision);
    if (!sceneId || !objectId || !decision || decision.tier !== "auto") continue;
    const status = reviewStatusText(decision), structure = parseStructureKind(raw.structure);
    const row: MachineLabelledRow = { scene_id: sceneId, object_id: objectId, version: text(raw.version) ?? DECISION_VERSION, decision, label: machineLabelName(decision.machine_label),
      review_url: text(raw.review_url) ?? reviewUrl(sceneId, objectId), review_status: status?.text ?? null, reviewed_by_person: status?.by_person ?? false };
    if (raw.hints !== undefined || raw.class_hint !== undefined) { const hints = parseClassHints({ class_hints: { flags: raw.hints, class_hint: raw.class_hint } }); row.hints = hints.hints; row.class_hint = hints.class_hint; }
    if (structure) row.structure = structure;
    rows.push(row);
  }
  return { version: text(document.version) ?? DECISION_VERSION, versions: Array.isArray(document.versions) ? document.versions.filter((v): v is string => typeof v === "string") : [DECISION_VERSION, ...DECISION_FALLBACK_VERSIONS],
    count: rows.length, reviewed_by_person: rows.filter(row => row.reviewed_by_person).length, rows, note: text(document.note) ?? MACHINE_LABELLED_NOTE };
}
/** "102 machine-labelled roofs, 0 reviewed by a person" */
export const machineLabelledSummary = (queue: Pick<MachineLabelledQueue, "count" | "reviewed_by_person">): string =>
  `${number(queue.count)} machine-labelled ${queue.count === 1 ? "roof" : "roofs"}, ${number(queue.reviewed_by_person)} reviewed by a person`;

export interface Classification {
  status: RowStatus;
  counted: boolean;
  success: boolean | null;
  iou: number | null;
  ring_edited: boolean | null;
  detail: string | null;
  model_signature: string | null;
  review_model_signature: string | null;
  review_status: string | null;
}
export interface CalibrationRow {
  band: string;
  group: string;
  scene_id: string;
  object_id: string;
  review_url: string;
  area_m2: number | null;
  detector_score: number | null;
  status: RowStatus;
  counted: boolean;
  success: boolean | null;
  address: string | null;
  address_id: number | null;
  /** Rule that produced the status (reference_calibration.classify detail), for the row's tooltip. */
  detail: string | null;
  /** Pixel IoU of the reviewed ring against the model ring when a ring was edited (approximate, see ringIou). */
  iou: number | null;
  /** Agent hints from the eyes index (building-flags-v2); present only when the index holds a row for this roof. */
  hints?: Hint[];
  class_hint?: string | null;
  /** Structure-kind hint (structure-kinds-v2); present only when the index holds that row. */
  structure?: StructureKind | null;
  /** Machine label and tier (decision-policy-v1); present only when the index holds that row. */
  decision?: Decision | null;
}
export interface CalibrationBand {
  groups_total: number;
  groups_reviewed: number;
  successes: number;
  failures: number;
  interval: [number, number] | null;
  minimum_groups: number;
  remaining_to_interval: number;
  statuses: Record<RowStatus, number>;
}
export interface CalibrationQueue {
  version: string | null;
  plan_id: string;
  created_at: string | null;
  policy: string | null;
  statement: string | null;
  iou_success: number;
  minimum_groups: number;
  bands: Record<string, CalibrationBand>;
  rows: CalibrationRow[];
  note: string;
  hints_version: string;
  hints_note: string;
}
export interface HiddenRow {
  scene_id: string;
  object_id: string;
  band: string | null;
  detector_score: number | null;
  observed_segments: number | null;
  other_scene_id: string;
  other_object_id: string;
  other_band: string | null;
  other_detector_score: number | null;
  iou: number | null;
  hidden_url: string;
  shown_url: string;
  address: string | null;
  hints?: Hint[];
  class_hint?: string | null;
  structure?: StructureKind | null;
  decision?: Decision | null;
}
export interface HiddenQueue {
  version: string;
  count: number;
  rows: HiddenRow[];
  note: string;
  hints_version: string;
  hints_note: string;
}
export interface SelfReviewSummary {
  run: string | null;
  status: string;
  total: number;
  completed: number;
  outcomes: { no_safe_gain: number; needs_evidence: number; candidate_ready: number };
  counts: Record<string, number>;
}
export interface CheckpointSummary {
  id: string;
  title: string;
  created_at: string | null;
  summary: string | null;
  address_text: string | null;
  address_id: number | null;
  latitude: number | null;
  longitude: number | null;
  report_sha256: string;
  /** Repo-relative REVIEW.md path when the folder has one (what /api/checkpoints/[id] returns as markdown). */
  review_path: string | null;
  /** The checkpoint's own report_url (may point elsewhere, e.g. the calibration review queue). */
  report_url: string | null;
}
export interface CheckpointDetail {
  id: string;
  markdown: string;
  report: Record<string, unknown>;
}
export interface QueueSummary {
  calibration: {
    plan_id: string;
    rows: number;
    groups_total_by_band: Record<string, number>;
    groups_reviewed_by_band: Record<string, number>;
    intervals_by_band: Record<string, [number, number] | null>;
  } | null;
  hidden: { count: number | null };
  self_review: SelfReviewSummary;
  roof_review: { cohort: string; reviewed: number; total: number };
  checkpoints: Pick<CheckpointSummary, "id" | "title" | "created_at" | "summary">[];
  note: string;
}

type Record_ = Record<string, unknown>;
const isRecord = (value: unknown): value is Record_ => typeof value === "object" && value !== null && !Array.isArray(value);
const text = (value: unknown): string | null => (typeof value === "string" ? value : null);
const finite = (value: unknown): number | null => (typeof value === "number" && Number.isFinite(value) ? value : null);
const integer = (value: unknown): number | null => (Number.isInteger(value) ? (value as number) : null);
export function parseJson(source: string | null): unknown {
  if (source === null) return null;
  try { return JSON.parse(source); } catch { return null; }
}
export const reviewUrl = (sceneId: string, objectId: string) =>
  `/scenes?scene=${encodeURIComponent(sceneId)}&object=${encodeURIComponent(objectId)}&kind=building&status=all&layer=model`;

// --- Wilson interval (scene_confidence.wilson) ---------------------------------------------------------

export function wilson(successes: number, total: number): [number, number] | null {
  if (!Number.isInteger(total) || !Number.isInteger(successes) || successes < 0 || successes > total) throw new Error("Use valid binomial counts");
  if (!total) return null;
  const z = 1.959963984540054, p = successes / total, d = 1 + z * z / total;
  const center = (p + z * z / (2 * total)) / d;
  const half = z * Math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / d;
  return [Math.max(0, center - half), Math.min(1, center + half)];
}

// --- Ring comparison and pixel IoU -----------------------------------------------------------------------

type Point = [number, number];
const point = (value: unknown): Point | null =>
  Array.isArray(value) && value.length >= 2 && Number.isFinite(value[0]) && Number.isFinite(value[1]) ? [value[0], value[1]] : null;
/** reference_registry._same_ring: same shape and np.allclose(atol=1e-6, rtol=1e-5); anything non-numeric differs. */
export function sameRing(a: unknown, b: unknown): boolean {
  if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    const p = point(a[i]), q = point(b[i]);
    if (!p || !q || (Array.isArray(a[i]) && Array.isArray(b[i]) && a[i].length !== b[i].length)) return false;
    for (let k = 0; k < 2; k++) if (Math.abs(p[k] - q[k]) > 1e-6 + 1e-5 * Math.abs(q[k])) return false;
  }
  return true;
}
const validHoles = (obj: Record_): unknown[] => (Array.isArray(obj.holes) ? obj.holes : []).filter(h => Array.isArray(h) && h.length >= 3);
/** reference_calibration._edited: the outer ring or any interior gap differs. */
export function ringEdited(model: Record_, reviewed: Record_): boolean {
  if (!sameRing(reviewed.ring, model.ring)) return true;
  const holes = validHoles(model), edited = validHoles(reviewed);
  return holes.length !== edited.length || !holes.every((h, i) => sameRing(h, edited[i]));
}
const ring = (value: unknown, what: string): Point[] => {
  if (!Array.isArray(value) || value.length < 3) throw new Error(`Invalid ${what}: at least three points are required`);
  return value.map(v => { const p = point(v); if (!p) throw new Error(`Invalid ${what}: non-numeric point`); return p; });
};
const polygon = (obj: Record_): Point[][] => [ring(obj.ring, "ring"), ...validHoles(obj).map(h => ring(h, "ring hole"))];
/** Even-odd crossings of every ring with the horizontal line y, sorted. */
function crossings(rings: Point[][], y: number, out: number[]): number[] {
  out.length = 0;
  for (const r of rings) {
    for (let i = 0, n = r.length; i < n; i++) {
      const [px, py] = r[i], [qx, qy] = r[(i + 1) % n];
      if (py === qy || y < Math.min(py, qy) || y >= Math.max(py, qy)) continue;
      out.push(px + (y - py) * (qx - px) / (qy - py));
    }
  }
  return out.sort((a, b) => a - b);
}
function fillRow(xs: number[], minX: number, step: number, row: Uint8Array) {
  row.fill(0);
  for (let k = 0; k + 1 < xs.length; k += 2) {
    const start = Math.max(0, Math.ceil((xs[k] - minX) / step - 0.5)), end = Math.min(row.length - 1, Math.floor((xs[k + 1] - minX) / step - 0.5));
    for (let i = start; i <= end; i++) row[i] = 1;
  }
}
/**
 * Pixel-space IoU of two objects' outlines (ring minus interior gaps), sampled on a 0.25 px grid over the
 * shared bounding box with even-odd filling. reference_calibration.ring_iou uses exact shapely areas after a
 * buffer(0) repair; this raster is within about 0.005 of that for house-sized outlines and treats
 * self-intersecting rings by even-odd fill rather than repair. Throws on rings with fewer than three points or
 * non-numeric coordinates, which classify() reports as invalid_ring exactly like the Python SKIPPABLE path.
 */
export function ringIou(model: Record_, reviewed: Record_): number {
  const a = polygon(model), b = polygon(reviewed);
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const r of [...a, ...b]) for (const [x, y] of r) { minX = Math.min(minX, x); maxX = Math.max(maxX, x); minY = Math.min(minY, y); maxY = Math.max(maxY, y); }
  if (!(maxX > minX) || !(maxY > minY)) return 0;
  let step = IOU_STEP_PX;
  while (((maxX - minX) / step) * ((maxY - minY) / step) > IOU_MAX_SAMPLES) step *= 2;
  const width = Math.ceil((maxX - minX) / step), height = Math.ceil((maxY - minY) / step);
  const rowA = new Uint8Array(width), rowB = new Uint8Array(width), xs: number[] = [];
  let intersection = 0, union = 0;
  for (let j = 0; j < height; j++) {
    const y = minY + (j + 0.5) * step;
    fillRow(crossings(a, y, xs), minX, step, rowA);
    fillRow(crossings(b, y, xs), minX, step, rowB);
    for (let i = 0; i < width; i++) { if (rowA[i] && rowB[i]) intersection++; if (rowA[i] || rowB[i]) union++; }
  }
  return union ? intersection / union : 0;
}

// --- Live status of one planned roof (reference_calibration.classify) ----------------------------------

const findObject = (objects: unknown, objectId: string): Record_ | null => {
  if (!Array.isArray(objects)) return null;
  for (const obj of objects) if (isRecord(obj) && obj.id === objectId) return obj;
  return null;
};
/**
 * latest: parsed data/scenes/<scene>/latest.json (any non-object means unavailable); review: parsed review.json
 * (any non-object means no review). Rules, in order: stale when the review's model_signature differs from
 * latest; rejected counts as a failure even with a malformed ring; confirmed + geometry_reviewed counts with
 * success = IoU >= 0.85 (1.0 when the ring is unchanged); needs_adjustment counts only once the ring is edited;
 * everything else (candidate, superseded, confirmed without geometry_reviewed, missing objects) is pending.
 */
export function classify(latest: unknown, review: unknown, objectId: string): Classification {
  const out: Classification = { status: "pending", counted: false, success: null, iou: null, ring_edited: null, detail: null,
    model_signature: null, review_model_signature: null, review_status: null };
  if (!isRecord(latest)) return { ...out, detail: "latest_unavailable" };
  out.model_signature = text(latest.model_signature);
  if (!isRecord(review)) return { ...out, detail: "no_review" };
  out.review_model_signature = text(review.model_signature);
  if ((review.model_signature ?? null) !== (latest.model_signature ?? null)) return { ...out, status: "stale", detail: "review_model_signature_differs_from_latest" };
  const model = findObject(latest.objects, objectId), reviewed = findObject(review.objects, objectId);
  if (!model) return { ...out, detail: "object_not_in_latest" };
  if (!reviewed) return { ...out, detail: "object_not_in_review" };
  const status = reviewed.status;
  out.review_status = text(status);
  let edited: boolean | null, iou: number | null;
  try { edited = ringEdited(model, reviewed); iou = edited ? ringIou(model, reviewed) : 1; } catch { edited = null; iou = null; }
  out.ring_edited = edited;
  out.iou = iou;
  if (status === "rejected") return { ...out, status: "rejected", counted: true, success: false, detail: "rejected_by_reviewer" };
  if (status === "confirmed" && reviewed.geometry_reviewed === true) {
    if (iou === null) return { ...out, detail: "invalid_ring" };
    return { ...out, status: "confirmed", counted: true, success: iou >= IOU_SUCCESS, detail: edited ? "ring_edited" : "ring_unchanged" };
  }
  if (status === "needs_adjustment") {
    if (edited === false) return { ...out, status: "needs_adjustment", detail: "ring_unedited: tier-3 observation, not counted until the ring is edited" };
    if (iou === null) return { ...out, detail: "invalid_ring" };
    return { ...out, status: "needs_adjustment", counted: true, success: iou >= IOU_SUCCESS, detail: "ring_edited" };
  }
  if (status === "confirmed") return { ...out, detail: "confirmed_without_geometry_reviewed" };
  return { ...out, detail: `status:${typeof status === "string" ? status : String(status)}` };
}

export function aggregateBands(rows: Pick<CalibrationRow, "band" | "counted" | "success" | "status">[], bands: readonly string[], minimum = MIN_CALIBRATION_GROUPS): Record<string, CalibrationBand> {
  const out: Record<string, CalibrationBand> = {};
  for (const band of bands) {
    const mine = rows.filter(r => r.band === band), counted = mine.filter(r => r.counted);
    const n = counted.length, wins = counted.filter(r => r.success === true).length;
    const statuses = Object.fromEntries(ROW_STATUSES.map(s => [s, mine.filter(r => r.status === s).length])) as Record<RowStatus, number>;
    out[band] = { groups_total: mine.length, groups_reviewed: n, successes: wins, failures: n - wins,
      interval: n >= minimum ? wilson(wins, n) : null, minimum_groups: minimum, remaining_to_interval: Math.max(0, minimum - n), statuses };
  }
  return out;
}

// --- Calibration queue --------------------------------------------------------------------------------------

interface SceneFiles { latest: unknown; review: unknown }
/** latest.json (often megabytes) is only parsed once a review exists; without one the row is pending anyway. */
function readScene(io: QueueIO, sceneId: string, cache: Map<string, SceneFiles>): SceneFiles {
  const cached = cache.get(sceneId);
  if (cached) return cached;
  const folder = `data/scenes/${sceneId}`;
  const review = parseJson(io.readText(`${folder}/review.json`));
  const latest = isRecord(review) ? parseJson(io.readText(`${folder}/latest.json`)) : io.exists(`${folder}/latest.json`) ? {} : null;
  const files = { latest, review };
  cache.set(sceneId, files);
  return files;
}
export function latestPlanId(io: QueueIO): string | null {
  let best: { id: string; created: string } | null = null;
  for (const id of io.listDir(CALIBRATION_ROOT)) {
    if (!PLAN_ID.test(id)) continue;
    const plan = parseJson(io.readText(`${CALIBRATION_ROOT}/${id}/plan.json`));
    if (!isRecord(plan) || plan.plan_id !== id) continue;
    const created = text(plan.created_at) ?? "";
    if (!best || created > best.created || (created === best.created && id > best.id)) best = { id, created };
  }
  return best?.id ?? null;
}
export function buildCalibrationQueue(io: QueueIO, planId: string): CalibrationQueue {
  if (!PLAN_ID.test(planId)) throw new QueueError("Choose a valid calibration plan id.", 400);
  const folder = `${CALIBRATION_ROOT}/${planId}`;
  const plan = parseJson(io.readText(`${folder}/plan.json`));
  if (!isRecord(plan) || plan.plan_id !== planId || !Array.isArray(plan.rows)) throw new QueueError("That calibration plan is not on this machine.", 404);
  const addresses = parseJson(io.readText(`${folder}/addresses.json`));
  const resolved = isRecord(addresses) && isRecord(addresses.resolved) ? addresses.resolved : {};
  const budget = isRecord(plan.label_budget) ? integer(plan.label_budget.per_band_minimum) : null;
  const minimum = budget ?? MIN_CALIBRATION_GROUPS;
  const cache = new Map<string, SceneFiles>();
  const rows: CalibrationRow[] = [];
  for (const planned of plan.rows) {
    if (!isRecord(planned)) continue;
    const sceneId = text(planned.scene_id) ?? "", objectId = text(planned.object_id) ?? "";
    const valid = SCENE_ID.test(sceneId) && objectId.length > 0;
    const state = valid ? (({ latest, review }) => classify(latest, review, objectId))(readScene(io, sceneId, cache))
      : { ...classify(null, null, objectId), detail: "invalid_scene_id" };
    const match = resolved[`${sceneId}/${objectId}`];
    const address = isRecord(match) ? match : {};
    rows.push({ band: text(planned.band) ?? "", group: text(planned.group) ?? "", scene_id: sceneId, object_id: objectId,
      review_url: text(planned.review_url) ?? reviewUrl(sceneId, objectId), area_m2: finite(planned.area_m2), detector_score: finite(planned.detector_score),
      status: state.status, counted: state.counted, success: state.success, address: text(address.full_address) ?? text(address.vicmap_address),
      address_id: integer(address.address_id), detail: state.detail, iou: state.iou === null ? null : Math.round(state.iou * 1e4) / 1e4 });
  }
  const bands = Array.isArray(plan.bands) ? plan.bands.filter((b): b is string => typeof b === "string") : ["low", "medium", "high"];
  return { version: text(plan.version), plan_id: planId, created_at: text(plan.created_at), policy: text(plan.policy), statement: text(plan.statement),
    iou_success: finite(plan.iou_success) ?? IOU_SUCCESS, minimum_groups: minimum, bands: aggregateBands(rows, bands, minimum),
    rows: attachHints(rows, io.hints?.() ?? null), note: QUEUE_NOTE, hints_version: HINTS_VERSION, hints_note: HINTS_NOTE };
}
export function compactCalibration(queue: CalibrationQueue): NonNullable<QueueSummary["calibration"]> {
  const entries = Object.entries(queue.bands);
  return { plan_id: queue.plan_id, rows: queue.rows.length,
    groups_total_by_band: Object.fromEntries(entries.map(([band, b]) => [band, b.groups_total])),
    groups_reviewed_by_band: Object.fromEntries(entries.map(([band, b]) => [band, b.groups_reviewed])),
    intervals_by_band: Object.fromEntries(entries.map(([band, b]) => [band, b.interval])) };
}

// --- Hidden roofs (rows printed by scripts.ml.cross_capture --hidden-low) ---------------------------------

/** hints: an explicit HintSource (or null for none); by default the eyes index under process.cwd() on the server. */
export function hiddenQueue(document: unknown, hints: HintSource | null = serverHintSource()): HiddenQueue {
  if (!isRecord(document) || !Array.isArray(document.rows)) { hints?.close(); throw new Error("The hidden-roof document has no rows"); }
  const rows: HiddenRow[] = [];
  for (const raw of document.rows) {
    if (!isRecord(raw)) continue;
    const sceneId = text(raw.scene_id) ?? "", objectId = text(raw.object_id) ?? "", otherScene = text(raw.other_scene_id) ?? "", otherObject = text(raw.other_object_id) ?? "";
    rows.push({ scene_id: sceneId, object_id: objectId, band: text(raw.band), detector_score: finite(raw.detector_score), observed_segments: finite(raw.observed_segments),
      other_scene_id: otherScene, other_object_id: otherObject, other_band: text(raw.other_band), other_detector_score: finite(raw.other_detector_score), iou: finite(raw.iou),
      hidden_url: text(raw.hidden_url) ?? reviewUrl(sceneId, objectId), shown_url: text(raw.shown_url) ?? reviewUrl(otherScene, otherObject), address: text(raw.address) });
  }
  return { version: text(document.version) ?? HIDDEN_VERSION, count: rows.length, rows: attachHints(rows, hints), note: HIDDEN_NOTE, hints_version: HINTS_VERSION, hints_note: HINTS_NOTE };
}

// --- Self-review compact fields (roof_review_loop.compact over active.json + report.json) ----------------

const idle = (run: string | null, status: string): SelfReviewSummary =>
  ({ run, status, total: 0, completed: 0, outcomes: { no_safe_gain: 0, needs_evidence: 0, candidate_ready: 0 }, counts: {} });
const TERMINAL = new Set(["candidate_ready", "no_safe_gain", "needs_evidence", "skipped", "failed"]);
/** Compact fields of GET /api/scenes/self-review read from disk; the run's stored status is passed through as-is. */
export function selfReviewSummary(io: QueueIO): SelfReviewSummary {
  const active = parseJson(io.readText(`${SELF_REVIEW_ROOT}/active.json`));
  const run = isRecord(active) ? text(active.run) : null;
  if (!run || !RUN_ID.test(run)) return idle(null, "idle");
  const report = parseJson(io.readText(`${SELF_REVIEW_ROOT}/runs/${run}/report.json`));
  if (!isRecord(report) || !Array.isArray(report.rows)) return idle(run, "unavailable");
  const counts: Record<string, number> = {};
  for (const row of report.rows) { const status = isRecord(row) ? text(row.status) : null; if (status) counts[status] = (counts[status] ?? 0) + 1; }
  return { run, status: text(report.status) ?? "unknown", total: integer(report.total) ?? report.rows.length,
    completed: Object.entries(counts).filter(([s]) => TERMINAL.has(s)).reduce((sum, [, n]) => sum + n, 0),
    outcomes: { no_safe_gain: counts.no_safe_gain ?? 0, needs_evidence: counts.needs_evidence ?? 0, candidate_ready: counts.candidate_ready ?? 0 }, counts };
}

// --- Checkpoints (data/references/eyes-checkpoints/<id>/report.json + REVIEW.md) ---------------------------

export function checkpointSummary(id: string, reportText: string, sha256: (text: string) => string, reviewExists: boolean): CheckpointSummary | null {
  const report = parseJson(reportText);
  if (!isRecord(report)) return null;
  const cp = isRecord(report.checkpoint) ? report.checkpoint : {};
  return { id, title: text(cp.title) ?? id, created_at: text(report.computed_at) ?? text(cp.computed_at), summary: text(cp.summary),
    address_text: text(cp.address_text), address_id: integer(cp.address_id), latitude: finite(cp.latitude), longitude: finite(cp.longitude),
    report_sha256: sha256(reportText), review_path: reviewExists ? `${CHECKPOINTS_ROOT}/${id}/REVIEW.md` : null, report_url: text(cp.report_url) };
}
export function listCheckpoints(io: QueueIO, sha256: (text: string) => string): CheckpointSummary[] {
  const out: CheckpointSummary[] = [];
  for (const id of io.listDir(CHECKPOINTS_ROOT)) {
    if (!CHECKPOINT_ID.test(id)) continue;
    const reportText = io.readText(`${CHECKPOINTS_ROOT}/${id}/report.json`);
    if (reportText === null) continue;
    const summary = checkpointSummary(id, reportText, sha256, io.exists(`${CHECKPOINTS_ROOT}/${id}/REVIEW.md`));
    if (summary) out.push(summary);
  }
  return out.sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? "") || a.id.localeCompare(b.id));
}
export function readCheckpoint(io: QueueIO, id: string): CheckpointDetail | null {
  if (!CHECKPOINT_ID.test(id)) throw new QueueError("Choose a valid checkpoint id.", 400);
  const report = parseJson(io.readText(`${CHECKPOINTS_ROOT}/${id}/report.json`));
  if (!isRecord(report)) return null;
  return { id, markdown: io.readText(`${CHECKPOINTS_ROOT}/${id}/REVIEW.md`) ?? "", report };
}
