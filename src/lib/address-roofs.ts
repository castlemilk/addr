import { Deep3DBuildingData } from "@/lib/deep-3d-model";

export interface RooflinePart {
  ring: [number, number][];
  z: number[];
  area_m2: number;
  slope_deg: number | null;
}

export interface RooflinePayload {
  parts: RooflinePart[];
  lines: [number, number][][];
}

export interface AddressRoofRecord {
  address_id: number;
  full_address: string;
  unit: string | null;
  category: string | null;
  kind: string;
  height: number;
  eave_height: number;
  ridge_height: number;
  roof_type: "flat" | "hipped" | "gabled";
  ridge_angle: number;
  area_m2: number;
  levels: number | null;
  source: string;
  address_point: { lat: number; lon: number };
  centroid: { lat: number; lon: number };
  ring_meters: [number, number][];
  width_meters: number;
  depth_meters: number;
  distance_meters: number;
  address_count: number;
  neighbours: string[];
  boundary_source?: "extracted" | "crm";
  suppressed_centroid?: { lat: number; lon: number } | null;
  suppressed_ring_meters?: [number, number][] | null;
  rooflines?: RooflinePayload | null;
  /** Other address ids claiming (near-)identical geometry. */
  shared_with?: number[];
  /** Roof surface material from a CRM correction (e.g. "terracotta"). */
  roof_material?: string | null;
  /** Property / parcel boundary in metres relative to centroid. */
  parcel_ring_meters?: [number, number][] | null;
  isolation?: string;
  canopy_over_roof_share?: number;
  quality_score?: number;
  quality_grade?: string;
  quality_flags?: string[];
}

const METERS_PER_DEGREE = 111_320.0;

/**
 * Roofline-engine gate: only use the per-plane partition when the model is
 * coherent (>= 2 parts, coverage 70-115% of the record area, no near-vertical
 * planes). Blobby or merged detector rings fail the gate and keep the previous
 * roof mesh, so the new engine can be on by default.
 */
export function shouldUseRooflineMesh(record: AddressRoofRecord): boolean {
  const parts = record.rooflines?.parts ?? [];
  if (parts.length < 2) return false;
  const area = record.area_m2 || 0;
  const covered = parts.reduce(
    (sum, part) => sum + (Number.isFinite(part.area_m2) ? part.area_m2 : 0),
    0
  );
  if (!area || covered / area < 0.7 || covered / area > 1.15) return false;
  const maxSlope = parts.reduce(
    (worst, part) => Math.max(worst, Number.isFinite(part.slope_deg) ? (part.slope_deg as number) : 0),
    0
  );
  return maxSlope <= 60;
}

let recordsPromise: Promise<AddressRoofRecord[]> | null = null;

export function loadAddressRoofs(url = "/address-roofs.json"): Promise<AddressRoofRecord[]> {
  recordsPromise ??= fetch(url)
    .then((response) => (response.ok ? response.json() : []))
    .catch(() => []);
  return recordsPromise;
}

export function findAddressRoof(
  records: AddressRoofRecord[],
  lon: number,
  lat: number,
  maxDistanceM = 20
): { record: AddressRoofRecord; building: Deep3DBuildingData } | null {
  let best: { distance: number; record: AddressRoofRecord } | null = null;
  for (const record of records) {
    const cosLat = Math.cos((lat * Math.PI) / 180);
    const dx = (record.address_point.lon - lon) * METERS_PER_DEGREE * cosLat;
    const dy = (record.address_point.lat - lat) * METERS_PER_DEGREE;
    const distance = Math.hypot(dx, dy);
    if (!best || distance < best.distance) {
      best = { distance, record };
    }
  }
  if (!best || best.distance > maxDistanceM) return null;
  const record = best.record;
  const building: Deep3DBuildingData = {
    height: record.height,
    eaveHeight: record.eave_height,
    ridgeHeight: record.ridge_height,
    roofType: record.roof_type,
    ridgeAngle: record.ridge_angle,
    areaM2: record.area_m2,
    levels: record.levels,
    kind: record.kind,
    centroid: record.centroid,
    ringMeters: record.ring_meters,
    widthMeters: record.width_meters,
    depthMeters: record.depth_meters,
    roofMaterial: record.roof_material ?? null,
    rooflineParts: shouldUseRooflineMesh(record)
      ? record.rooflines?.parts?.map((part) => ({ ring: part.ring, z: part.z }))
      : undefined,
    parcelRingMeters: record.parcel_ring_meters ?? null,
  };
  return { record, building };
}
