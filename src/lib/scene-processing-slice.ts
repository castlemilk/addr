import { EVIDENCE_BANDS, type EvidenceBand, type ProcessingRoof, type ProcessingRoofRow } from "./scene-processing";
import { buildingAddressTitle } from "./building-addresses";

/**
 * Pure slicing of the cached processing inventory for the roof table. The route holds one
 * ~36 MB inventory in memory; the browser only ever receives one page of trimmed rows.
 * Nothing here reads files, touches the network or changes a roof.
 */
export const ROOF_PAGE_SIZE = 50;
export const MAX_PAGE_SIZE = 200;
export const ROOF_SORT_KEYS = ["evidence", "address", "area", "review"] as const;
export type RoofSortKey = typeof ROOF_SORT_KEYS[number];
/** A sort key, optionally prefixed with "-" for descending. */
export type RoofSort = RoofSortKey | `-${RoofSortKey}`;

export interface RoofSliceParams {
  page?: string | number | null; size?: string | number | null;
  band?: string | null; review?: string | null; address?: string | null; q?: string | null; sort?: string | null;
}
export interface RoofSliceSource {
  features: ProcessingRoof[]; counts?: Record<string, number>; address_counts?: Record<string, number>; fetched_at?: number;
}
export interface RoofSlice {
  rows: ProcessingRoofRow[]; total: number; page: number; size: number; pages: number; sort: RoofSort;
  /** Inventory-wide denominators, deliberately not the filtered total. */
  counts: Record<string, number>; address_counts: Record<string, number>; fetched_at: number;
}
/** A low roof that scene_confidence.query() hides on the map under a higher-band capture of the same footprint. */
export interface HiddenLowRoof {
  key: string; scene_id: string; object_id: string; band: "low"; iou: number | null; review_url: string;
  hidden_by: { key: string; scene_id: string; object_id: string; band: EvidenceBand; address: string };
}

const BAND_RANK: Record<string, number> = Object.fromEntries(EVIDENCE_BANDS.map((b, i) => [b.id, i]));
/** The bands that count as "higher" than low. A not-assessed capture is unknown, not higher. */
const HIGHER_THAN_LOW = new Set<string>(["medium", "high"]);
const REVIEW_RANK: Record<string, number> = { candidate: 0, needs_adjustment: 1, stale: 2, confirmed: 3, rejected: 4, superseded: 5 };
const collator = new Intl.Collator("en-AU", { numeric: true, sensitivity: "base" });

type Props = ProcessingRoof["properties"];
/** Features with a properties object. GeoJSON allows properties: null and a truncated payload may lack the array;
 *  neither may take every part of the route down. */
const usable = (features: ProcessingRoof[] | null | undefined): ProcessingRoof[] => (Array.isArray(features) ? features : []).filter(f => f && f.properties);
/** Null, missing and unknown bands are all "not assessed"; they filter and sort together. */
export const bandOf = (p: Pick<Props, "band">): EvidenceBand => (p.band && p.band in BAND_RANK ? p.band : "unassessed");
const reviewRank = (p: Props) => p.review_status in REVIEW_RANK ? REVIEW_RANK[p.review_status] : Object.keys(REVIEW_RANK).length;
const areaOf = (p: Props) => (typeof p.area_m2 === "number" && Number.isFinite(p.area_m2) ? p.area_m2 : -Infinity);
const keyOf = (p: Props) => p.key || `${p.scene_id}/${p.object_id}`;
const searchTextOf = (p: Props) => p.search_text ?? [p.label, p.capture_title, keyOf(p), p.property_address?.full_address].filter(Boolean).join(" ");

const primary: Record<RoofSortKey, (a: Props, b: Props) => number> = {
  evidence: (a, b) => BAND_RANK[bandOf(a)] - BAND_RANK[bandOf(b)]
    || (a.detector_score == null ? 1 : 0) - (b.detector_score == null ? 1 : 0)
    || (a.detector_score ?? 0) - (b.detector_score ?? 0),
  address: (a, b) => collator.compare(buildingAddressTitle(a.property_address), buildingAddressTitle(b.property_address)),
  area: (a, b) => areaOf(a) - areaOf(b),
  review: (a, b) => reviewRank(a) - reviewRank(b),
};

export function parseSort(value: string | null | undefined): RoofSort {
  const raw = (value || "").trim(), desc = raw.startsWith("-"), key = desc ? raw.slice(1) : raw;
  return (ROOF_SORT_KEYS as readonly string[]).includes(key) ? (desc ? `-${key as RoofSortKey}` : key as RoofSortKey) : "evidence";
}

function integer(value: string | number | null | undefined, fallback: number, min: number, max: number) {
  const n = typeof value === "number" ? value : value == null || value === "" ? NaN : Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, Math.trunc(n)));
}

/** Everything the table and inspector need; only the search text stays on the server. */
export function trimRoof(feature: ProcessingRoof): ProcessingRoofRow {
  const { search_text, ...properties } = feature.properties;
  void search_text;
  return { ...feature, properties };
}

export function sliceRoofs(inventory: RoofSliceSource, params: RoofSliceParams): RoofSlice {
  const band = params.band && params.band in BAND_RANK ? params.band : "all";
  const review = params.review && params.review !== "all" ? params.review : "all";
  const address = params.address && params.address !== "all" ? params.address : "all";
  const q = (params.q || "").trim().toLowerCase();
  const sort = parseSort(params.sort), desc = sort.startsWith("-"), compare = primary[(desc ? sort.slice(1) : sort) as RoofSortKey];
  const matched = usable(inventory.features).filter(f => {
    const p = f.properties;
    return (band === "all" || bandOf(p) === band)
      && (review === "all" || p.review_status === review)
      && (address === "all" || p.property_address?.status === address)
      && (!q || searchTextOf(p).toLowerCase().includes(q));
  });
  // Stable in both directions: the roof key always breaks ties ascending, so a page never shuffles between polls.
  matched.sort((a, b) => (desc ? -compare(a.properties, b.properties) : compare(a.properties, b.properties))
    || (keyOf(a.properties) < keyOf(b.properties) ? -1 : keyOf(a.properties) > keyOf(b.properties) ? 1 : 0));
  const size = integer(params.size, ROOF_PAGE_SIZE, 1, MAX_PAGE_SIZE);
  const pages = Math.max(1, Math.ceil(matched.length / size));
  const page = integer(params.page, 1, 1, pages);
  return {
    rows: matched.slice((page - 1) * size, page * size).map(trimRoof), total: matched.length, page, size, pages, sort,
    counts: inventory.counts || {}, address_counts: inventory.address_counts || {}, fetched_at: inventory.fetched_at ?? 0,
  };
}

export function roofByKey(features: ProcessingRoof[], key: string | null | undefined): ProcessingRoof | undefined {
  return key ? usable(features).find(f => keyOf(f.properties) === key) : undefined;
}

const reviewUrl = (scene: string, object: string) => `/scenes?${new URLSearchParams({ scene, object, kind: "building", status: "all", layer: "model" })}`;

/**
 * Low roofs hidden under higher bands (medium or high). query() keeps one capture per footprint
 * and lists the ones it hid in hidden_overlaps; a low roof hidden by a medium/high capture
 * disappears from the map even though its own evidence was weak. The eyes cross_capture rows
 * can also record a low roof as hidden_by a higher-band capture. Both sources are unioned and
 * deduplicated. hidden_by.address is the footprint's address: the hider's own match when that
 * capture is in this inventory, otherwise the hidden roof's match for the same footprint.
 * Null entries and rows without bands are skipped, never thrown.
 */
export function hiddenLowRoofs(features: ProcessingRoof[]): HiddenLowRoof[] {
  const all = usable(features), byKey = new Map(all.map(f => [keyOf(f.properties), f.properties]));
  const found = new Map<string, HiddenLowRoof>();
  const keep = (row: HiddenLowRoof) => {
    const previous = found.get(row.key);
    if (!previous || (row.iou ?? -1) > (previous.iou ?? -1)) found.set(row.key, row);
  };
  for (const f of all) {
    const p = f.properties, keeper = bandOf(p);
    if (HIGHER_THAN_LOW.has(keeper)) {
      for (const o of p.hidden_overlaps || []) {
        if (!o || o.band !== "low" || !o.scene_id || !o.object_id) continue;
        keep({ key: `${o.scene_id}/${o.object_id}`, scene_id: o.scene_id, object_id: o.object_id, band: "low", iou: o.iou ?? null,
          review_url: reviewUrl(o.scene_id, o.object_id),
          hidden_by: { key: keyOf(p), scene_id: p.scene_id, object_id: p.object_id, band: keeper, address: buildingAddressTitle(p.property_address) } });
      }
    } else if (keeper === "low") {
      const cross = p.eyes?.cross_capture, bands = Array.isArray(cross?.bands) ? cross.bands : [];
      for (const hider of cross?.hidden_by || []) {
        const pair = bands.find(b => b && `${b.scene_id}/${b.object_id}` === hider);
        if (!pair?.band || !HIGHER_THAN_LOW.has(pair.band)) continue;
        const hiderProps = byKey.get(hider);
        keep({ key: keyOf(p), scene_id: p.scene_id, object_id: p.object_id, band: "low", iou: pair.iou ?? null, review_url: p.review_url || reviewUrl(p.scene_id, p.object_id),
          hidden_by: { key: hider, scene_id: pair.scene_id, object_id: pair.object_id, band: pair.band, address: buildingAddressTitle(hiderProps ? hiderProps.property_address : p.property_address) } });
      }
    }
  }
  return [...found.values()].sort((a, b) => BAND_RANK[b.hidden_by.band] - BAND_RANK[a.hidden_by.band]
    || (b.iou ?? -1) - (a.iou ?? -1) || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
}
