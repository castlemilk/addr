import { Deep3DBuildingData } from "@/lib/deep-3d-model";

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
}

const METERS_PER_DEGREE = 111_320.0;

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
  };
  return { record, building };
}
