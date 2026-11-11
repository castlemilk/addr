// Per-address enrichment (address-enrichment-v1): one JSON per G-NAF address written by the Python
// pipeline at data/reviews/eyes/address-enrichment/addresses/<address_id>.json. Everything in it is an
// agent hint or a model estimate (allocation, structure kinds, bands, areas), never a label or accuracy.
// This module parses a document leniently (malformed parts degrade to null or empty, nothing throws)
// and turns it into plain-words copy for the map sidebar. It has no Node imports so client components
// may import it; the route reads the file and hands the text here.

export const ENRICHMENT_VERSION = "address-enrichment-v1";
export const ENRICHMENT_ROOT = "data/reviews/eyes/address-enrichment/addresses";
export const ENRICHMENT_NOTE = "Agent hints and model estimates, not labels or accuracy.";
export const ALLOCATION_STATUSES = ["matched", "suggested", "ambiguous", "shared", "unmatched"] as const;
export type AllocationStatus = (typeof ALLOCATION_STATUSES)[number];
export const ALLOCATION_CONFIDENCES = ["strong", "moderate", "weak"] as const;
export type AllocationConfidence = (typeof ALLOCATION_CONFIDENCES)[number];

export interface EnrichmentAlternative { address_id: number | null; full_address: string }
export interface EnrichmentAllocation { status: AllocationStatus; method: string; confidence: AllocationConfidence; reason: string; alternatives: EnrichmentAlternative[] }
export interface EnrichmentRoof { scene_id: string | null; object_id: string | null; area_m2: number | null; roof_type: string | null; band: string | null; review_url: string | null }
export interface EnrichmentStructure { scene_id: string | null; object_id: string | null; sub_kind: string; confidence: string | null; area_m2: number | null; review_url: string | null }
export interface AddressEnrichment {
  version: string | null;
  note: string | null;
  address_id: number;
  full_address: string | null;
  property_pfi: string | null;
  lot: { plan: string | null; lot: string | null } | null;
  allocation: EnrichmentAllocation | null;
  primary_roof: EnrichmentRoof | null;
  structures: EnrichmentStructure[];
  solar: { count: number; area_m2: number };
  trees: { count_on_lot: number | null };
  pool: { present: boolean | null };
  roofs_total: number | null;
  computed_at: string | null;
}

type Record_ = Record<string, unknown>;
const isRecord = (value: unknown): value is Record_ => typeof value === "object" && value !== null && !Array.isArray(value);
const text = (value: unknown): string | null => (typeof value === "string" ? value : null);
const finite = (value: unknown): number | null => (typeof value === "number" && Number.isFinite(value) ? value : null);
const integer = (value: unknown): number | null => (typeof value === "number" && Number.isInteger(value) ? value : typeof value === "string" && /^-?\d+$/.test(value) ? Number(value) : null);
const count = (value: unknown): number | null => { const n = integer(value); return n !== null && n >= 0 ? n : null; };
const oneOf = <T extends string>(value: unknown, allowed: readonly T[]): T | null => (typeof value === "string" && (allowed as readonly string[]).includes(value) ? value as T : null);
/** A review link is kept only when it is a same-origin editor path; anything else is dropped rather than rendered. */
const reviewUrl = (value: unknown): string | null => { const url = text(value); return url && /^\/scenes(\?|$)/.test(url) ? url : null; };
/** JSON text to a value; unparseable text is null, never a throw. */
export function parseEnrichmentJson(source: string): unknown { try { return JSON.parse(source); } catch { return null; } }

function parseRoof(value: unknown): EnrichmentRoof | null {
  if (!isRecord(value)) return null;
  return { scene_id: text(value.scene_id), object_id: text(value.object_id), area_m2: finite(value.area_m2), roof_type: text(value.roof_type), band: text(value.band), review_url: reviewUrl(value.review_url) };
}
function parseStructures(value: unknown): EnrichmentStructure[] {
  if (!Array.isArray(value)) return [];
  const out: EnrichmentStructure[] = [];
  for (const item of value) {
    if (!isRecord(item)) continue;
    const kind = text(item.sub_kind);
    if (!kind) continue;
    out.push({ scene_id: text(item.scene_id), object_id: text(item.object_id), sub_kind: kind, confidence: text(item.confidence), area_m2: finite(item.area_m2), review_url: reviewUrl(item.review_url) });
  }
  return out;
}
function parseAllocation(value: unknown): EnrichmentAllocation | null {
  if (!isRecord(value)) return null;
  const status = oneOf(value.status, ALLOCATION_STATUSES);
  if (!status) return null;
  const alternatives: EnrichmentAlternative[] = [];
  if (Array.isArray(value.alternatives)) for (const item of value.alternatives) {
    if (!isRecord(item)) continue;
    const full = text(item.full_address);
    if (full) alternatives.push({ address_id: integer(item.address_id), full_address: full });
  }
  return { status, method: text(value.method) ?? "", confidence: oneOf(value.confidence, ALLOCATION_CONFIDENCES) ?? "weak", reason: text(value.reason) ?? "", alternatives };
}
/** An enrichment document from JSON text or a parsed value. Null only when there is no record with an integer address_id;
 *  every other part degrades on its own (a malformed roof is null, malformed structures are skipped, a bad count is 0). */
export function parseEnrichment(input: unknown): AddressEnrichment | null {
  const doc = typeof input === "string" ? parseEnrichmentJson(input) : input;
  if (!isRecord(doc)) return null;
  const addressId = integer(doc.address_id);
  if (addressId === null || addressId < 0) return null;
  const solar = isRecord(doc.solar) ? doc.solar : {}, trees = isRecord(doc.trees) ? doc.trees : {}, pool = isRecord(doc.pool) ? doc.pool : {};
  const lot = isRecord(doc.lot) ? { plan: text(doc.lot.plan), lot: text(doc.lot.lot) } : null;
  return {
    version: text(doc.version), note: text(doc.note), address_id: addressId, full_address: text(doc.full_address),
    property_pfi: text(doc.property_pfi) ?? (integer(doc.property_pfi) !== null ? String(doc.property_pfi) : null),
    lot: lot && (lot.plan || lot.lot) ? lot : null,
    allocation: parseAllocation(doc.allocation), primary_roof: parseRoof(doc.primary_roof), structures: parseStructures(doc.structures),
    solar: { count: count(solar.count) ?? 0, area_m2: Math.max(0, finite(solar.area_m2) ?? 0) },
    trees: { count_on_lot: count(trees.count_on_lot) }, pool: { present: typeof pool.present === "boolean" ? pool.present : null },
    roofs_total: count(doc.roofs_total), computed_at: text(doc.computed_at),
  };
}

// --- Plain-words copy -----------------------------------------------------------------------------------------
const KIND_WORDS: Record<string, string> = { "shipping-container": "Shipping container", "container-stack": "Container stack", shed: "Shed", garage: "Garage", carport: "Carport", "pergola-or-shade": "Pergola or shade", "water-tank": "Water tank", "portable-classroom": "Portable classroom", "pool-cover": "Pool cover", fragment: "Fragment", outbuilding: "Outbuilding", "not-secondary": "Secondary roof" };
const SOURCE_WORDS: Record<string, string> = { vicmap: "Vicmap", gnaf: "G-NAF", osm: "OpenStreetMap", sam3: "SAM3", poi: "places", pfi: "PFI", id: "id" };
const sentence = (value: string) => (value ? value.charAt(0).toUpperCase() + value.slice(1) : value);
/** "vicmap_property-lot" reads as "Vicmap property lot"; unknown tokens keep their letters, lower case. */
export function methodWords(method: string): string {
  const words = method.split(/[\s_\-./]+/).filter(Boolean).map(token => SOURCE_WORDS[token.toLowerCase()] ?? token.toLowerCase());
  return words.join(" ");
}
/** "Garage" for a known kind, else the raw kind with its dashes as spaces in sentence case. */
export const structureWords = (kind: string): string => KIND_WORDS[kind] ?? sentence(kind.replace(/[-_]+/g, " "));
export const roofTypeWords = (type: string | null): string | null => (type ? type.replace(/[-_]+/g, " ").toLowerCase() : null);
export const areaText = (value: number | null): string | null => (value === null ? null : `${Math.round(value)} m²`);
const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/** One structure line: "Garage, 63 m² (strong)"; area or confidence drop out when unknown. */
export function structureLine(structure: EnrichmentStructure): string {
  const area = areaText(structure.area_m2);
  return `${structureWords(structure.sub_kind)}${area ? `, ${area}` : ""}${structure.confidence ? ` (${structure.confidence})` : ""}`;
}
/** The primary roof in one line: "Hip roof, 180 m², medium band (model estimate)"; parts drop out when unknown. */
export function primaryRoofLine(roof: EnrichmentRoof | null): string | null {
  if (!roof) return null;
  const type = roofTypeWords(roof.roof_type), area = areaText(roof.area_m2);
  const parts = [type ? sentence(`${type} roof`) : "Roof", area, roof.band ? `${roof.band} band (model estimate)` : null].filter((part): part is string => !!part);
  return parts.join(", ");
}
/** Solar in one line, or null when nothing was detected: "Solar: 2 detections, 18 m²". */
export function solarLine(solar: AddressEnrichment["solar"]): string | null {
  if (!solar.count && !solar.area_m2) return null;
  const area = solar.area_m2 ? `, ${Math.round(solar.area_m2)} m²` : "";
  return `Solar: ${plural(solar.count, "detection", "detections")}${area}`;
}
/** Trees on the lot and the pool estimate, when the document says anything about them. */
export function lotLines(doc: AddressEnrichment): string[] {
  const out: string[] = [];
  if (doc.trees.count_on_lot !== null) out.push(`Trees on the lot: ${doc.trees.count_on_lot}`);
  if (doc.pool.present !== null) out.push(doc.pool.present ? "Pool: present (model estimate)" : "Pool: none detected");
  return out;
}
/** Inventory in plain words: "3 roofs: 1 garage, 1 shed" or "1 roof, no other structures"; null when the document knows nothing. */
export function structureSummary(doc: AddressEnrichment): string | null {
  const counts = new Map<string, number>();
  for (const structure of doc.structures) counts.set(structure.sub_kind, (counts.get(structure.sub_kind) ?? 0) + 1);
  const kinds = [...counts.entries()].map(([kind, n]) => `${n} ${structureWords(kind).toLowerCase()}${n === 1 ? "" : "s"}`);
  const roofs = doc.roofs_total ?? (doc.primary_roof ? 1 + doc.structures.length : null);
  if (roofs === null && !kinds.length) return null;
  const head = roofs === null ? null : plural(roofs, "roof", "roofs");
  if (!kinds.length) return head ? `${head}, no other structures` : null;
  return head ? `${head}: ${kinds.join(", ")}` : `Structures: ${kinds.join(", ")}`;
}
/** The allocation in plain words: "Allocated to this address by Vicmap property lot; moderate. Alternatives: 3 HERMES CLOSE." */
export function describeAllocation(allocation: EnrichmentAllocation | null): string | null {
  if (!allocation) return null;
  const by = allocation.method ? ` by ${methodWords(allocation.method)}` : "";
  const lead = allocation.status === "matched" ? `Allocated to this address${by}`
    : allocation.status === "suggested" ? `Suggested for this address${by}`
    : allocation.status === "ambiguous" ? `Allocation is ambiguous${by}`
    : allocation.status === "shared" ? `Shared with a neighbouring address${by}`
    : `No roof allocated to this address${by}`;
  const alternatives = allocation.alternatives.map(alt => alt.full_address);
  return `${lead}; ${allocation.confidence}.${alternatives.length ? ` Alternatives: ${alternatives.join(", ")}.` : ""}`;
}
