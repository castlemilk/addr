import type { Feature, FeatureCollection, MultiPolygon, Polygon, Position } from "geojson";
import type { SceneModelSnapshot, SceneObject, ScenePoint } from "./scene-types";

export const PARCEL_SOURCE = {
  name: "Vicmap Parcel", attribution: "© State of Victoria (Department of Transport and Planning)",
  url: "https://discover.data.vic.gov.au/dataset/vicmap-property-rest-api",
  service: "https://services-ap1.arcgis.com/P744lA0wf4LlBZ84/ArcGIS/rest/services/Vicmap_Parcel/FeatureServer/0",
  license: "CC BY 4.0", license_url: "https://creativecommons.org/licenses/by/4.0/",
};
export type ParcelBounds = [number, number, number, number];
export type ParcelProperties = {
  pfi: string; spi: string; lot: string; plan: string; status: "approved";
  horizontal_uncertainty_m: number | null; geometry_record_created: string | null;
};
export type ParcelFeature = Feature<Polygon | MultiPolygon, ParcelProperties>;
export type ParcelData = FeatureCollection<Polygon | MultiPolygon, ParcelProperties> & {
  source: typeof PARCEL_SOURCE; retrieved_at: string; bounds: ParcelBounds;
};
const WORLD = 40075016.68557849;
const mercatorY = (lat: number) => Math.asinh(Math.tan(lat * Math.PI / 180)) / (2 * Math.PI);

export function parcelBoundsIssue(bounds: unknown, maxSpanMeters = 2000): string | null {
  if (!Array.isArray(bounds) || bounds.length !== 4 || bounds.some(v => typeof v !== "number" || !Number.isFinite(v))) return "Enter four finite bounds.";
  const [w,s,e,n] = bounds;
  if (w >= e || s >= n || w < 140.9 || e > 150.1 || s < -39.3 || n > -33.9) return "Property boundaries are available for Victoria. Move the map into Victoria.";
  if (Math.max((e-w)/360, mercatorY(n)-mercatorY(s)) * WORLD * Math.cos((s+n)/2*Math.PI/180) > maxSpanMeters) return `Zoom in to view property boundaries (up to ${maxSpanMeters>=1000?`${maxSpanMeters/1000} km`:`${maxSpanMeters} m`} across).`;
  return null;
}

export function parcelPolygons(geometry: Polygon | MultiPolygon): Position[][][] {
  return geometry.type === "Polygon" ? [geometry.coordinates] : geometry.coordinates;
}

/** WGS84 -> the same local Mercator metre frame used by scene_region.py. */
export function parcelImagePoint(point: Position, scene: SceneModelSnapshot): ScenePoint {
  const scale = WORLD * Math.cos(scene.centroid.lat * Math.PI / 180);
  const x = (point[0] - scene.centroid.lon) / 360 * scale;
  const y = (mercatorY(point[1]) - mercatorY(scene.centroid.lat)) * scale;
  const m = scene.meters_to_pixel;
  return [x*m[0][0]+y*m[1][0]+m[2][0], x*m[0][1]+y*m[1][1]+m[2][1]];
}

export function parcelImagePolygons(feature: ParcelFeature, scene: SceneModelSnapshot): ScenePoint[][][] {
  return parcelPolygons(feature.geometry).map(polygon => polygon.map(ring => ring.map(p => parcelImagePoint(p, scene))));
}

const mercatorLat = (y: number) => Math.atan(Math.sinh(y * 2 * Math.PI)) * 180 / Math.PI;

/** Pixel -> WGS84: the exact inverse of parcelImagePoint (and of scripts/ml/scene_addresses.image_projection). */
export function sceneLonLat(point: ScenePoint, scene: Pick<SceneModelSnapshot, "centroid" | "pixel_to_meters">): Position {
  const a = scene.pixel_to_meters, [x, y] = point;
  const mx = x*a[0][0] + y*a[1][0] + a[2][0], my = x*a[0][1] + y*a[1][1] + a[2][1];
  const lat0 = scene.centroid.lat, scale = WORLD * Math.cos(lat0 * Math.PI / 180);
  return [scene.centroid.lon + mx/scale*360, mercatorLat(mercatorY(lat0) + my/scale)];
}

export type SceneGeoBounds = { bounds: ParcelBounds; registration: "region" | "fitted" };

/**
 * Geographic bounds of the frame. A registered map capture carries them; a cohort frame (no region) has affines fitted
 * from the CRM roof by least squares, so its corners are inverted through them and marked "fitted": boundaries drawn
 * from them may sit a few pixels off the imagery. Null when the scene carries neither.
 */
export function sceneGeoBounds(scene: SceneModelSnapshot): SceneGeoBounds | null {
  if (scene.region?.bounds) return { bounds: scene.region.bounds, registration: "region" };
  const a = scene.pixel_to_meters, w = scene.image_width, h = scene.image_height;
  if (!scene.centroid || !Array.isArray(a) || a.length < 3 || !(w > 0) || !(h > 0)) return null;
  const corners = ([[0,0],[w,0],[w,h],[0,h]] as ScenePoint[]).map(p => sceneLonLat(p, scene));
  const lons = corners.map(c => c[0]), lats = corners.map(c => c[1]);
  const bounds: ParcelBounds = [Math.min(...lons), Math.min(...lats), Math.max(...lons), Math.max(...lats)];
  return bounds.every(Number.isFinite) ? { bounds, registration: "fitted" } : null;
}

export function sceneParcelBounds(scene: SceneModelSnapshot): ParcelBounds | null {
  const geo = sceneGeoBounds(scene);
  if (!geo) return null; // Images with neither a capture nor fitted affines cannot carry reference geography.
  const [w,s,e,n] = geo.bounds, padX = (e-w)/2, padY = (n-s)/2;
  return [w-padX,s-padY,e+padX,n+padY];
}

function insideRing(p: Position, ring: Position[]): boolean {
  let inside = false;
  for (let i=0,j=ring.length-1;i<ring.length;j=i++) {
    const a=ring[i], b=ring[j];
    if ((a[1]>p[1]) !== (b[1]>p[1]) && p[0]<(b[0]-a[0])*(p[1]-a[1])/(b[1]-a[1])+a[0]) inside=!inside;
  }
  return inside;
}

export function parcelContains(point: Position, polygons: Position[][][]): boolean {
  return polygons.some(([outer,...holes]) => insideRing(point,outer) && !holes.some(ring => insideRing(point,ring)));
}

/** A viewing hint only. Ambiguous overlaps do not establish an address/owner. */
export function parcelNearOutline(features: ParcelFeature[], ring: ScenePoint[], scene: SceneModelSnapshot): ParcelFeature | null {
  if (ring.length<3) return null;
  const samples = ring.map((p,i) => [(p[0]+ring[(i+1)%ring.length][0])/2,(p[1]+ring[(i+1)%ring.length][1])/2]);
  const candidates = features.filter(f => {
    const polygons=parcelImagePolygons(f,scene);
    return samples.filter(p => parcelContains(p,polygons)).length / samples.length >= .8;
  });
  return candidates.length === 1 ? candidates[0] : null;
}

const NUMBERED_LOT = /^\d+[A-Za-z]?$/;
// Tolerates a missing lot (Vicmap leaves parcel_lot_number null on unnumbered parcels) so one record cannot break the overlay.
const lotText = (lot: unknown): string => typeof lot === "string" ? lot : lot == null ? "" : String(lot);
const lotOrder = (lot: unknown): [number, string] => { const text = lotText(lot), n = text.match(/^\d+/); return [n ? Number(n[0]) : Number.POSITIVE_INFINITY, text]; };
const byLot = (a: ParcelFeature, b: ParcelFeature) => {
  const [na, sa] = lotOrder(a.properties.lot), [nb, sb] = lotOrder(b.properties.lot);
  return na === nb ? sa.localeCompare(sb) : na - nb;
};
const geometryKey = (feature: ParcelFeature) => JSON.stringify(parcelPolygons(feature.geometry).map(poly => poly.map(ring => ring.map(p => [Math.round(p[0]*1e7), Math.round(p[1]*1e7)]))));

export type ParcelTouch = { feature: ParcelFeature; share: number };

/** Every lot that covers at least `minShare` of the outline (sampled), largest share first. Allocation context, never ownership. */
export function parcelsTouchingOutline(features: ParcelFeature[], ring: ScenePoint[], scene: SceneModelSnapshot, minShare = .02): ParcelTouch[] {
  if (ring.length < 3) return [];
  const xs = ring.map(p => p[0]), ys = ring.map(p => p[1]);
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  const step = Math.max((maxX-minX)/24, (maxY-minY)/24, 1e-6);
  const samples: Position[] = [];
  for (let x = minX+step/2; x < maxX; x += step) for (let y = minY+step/2; y < maxY; y += step) if (insideRing([x,y], ring)) samples.push([x,y]);
  if (!samples.length) return [];
  const touching: ParcelTouch[] = [];
  for (const feature of features) {
    const polygons = parcelImagePolygons(feature, scene), points = polygons.flat(2);
    if (Math.min(...points.map(p => p[0])) > maxX || Math.max(...points.map(p => p[0])) < minX || Math.min(...points.map(p => p[1])) > maxY || Math.max(...points.map(p => p[1])) < minY) continue;
    const share = samples.filter(p => parcelContains(p, polygons)).length / samples.length;
    if (share >= minShare) touching.push({ feature, share: Math.round(share*1e4)/1e4 });
  }
  return touching.sort((a, b) => b.share - a.share || byLot(a.feature, b.feature));
}

export type ParcelGroup = { key: string; feature: ParcelFeature; features: ParcelFeature[] };

/** Stacked strata lots share one polygon in Vicmap Parcel; draw each distinct polygon once and label its lots together. */
export function parcelGeometryGroups(features: ParcelFeature[]): ParcelGroup[] {
  const groups = new Map<string, ParcelFeature[]>();
  for (const feature of features) {
    const key = geometryKey(feature);
    groups.set(key, [...(groups.get(key) || []), feature]);
  }
  return [...groups].map(([key, members]) => { const sorted = [...members].sort(byLot); return { key, feature: sorted[0], features: sorted }; });
}

/** Sentence-case lot label for one drawn polygon. */
export function parcelLotLabel(features: ParcelFeature[]): string {
  if (!features.length) return "";
  const lots = [...features].sort(byLot).map(f => lotText(f.properties.lot));
  if (features.length === 1) {
    const lot = lots[0];
    if (!lot) return `Parcel ${features[0].properties.spi || features[0].properties.pfi}`;
    return lot.toUpperCase().startsWith("CM") ? `Common property ${lot}` : `Lot ${lot}`;
  }
  const numbered = lots.filter(l => NUMBERED_LOT.test(l)), others = lots.filter(l => !NUMBERED_LOT.test(l)).map(l => l || "unnumbered");
  if (!numbered.length) return `Parcels ${others.join(", ")} (stacked)`;
  return `Lots ${numbered.join(", ")}${others.length ? ` + ${others.join(", ")}` : ""} (stacked)`;
}

/** Status line for the lots under an outline; null when none touches it. */
export function parcelOutlineSummary(touching: ParcelTouch[]): string | null {
  if (!touching.length) return null;
  const plans = [...new Set(touching.map(t => t.feature.properties.plan).filter(Boolean))].sort().join(", ") || "plan unknown";
  if (touching.length === 1) return `${parcelLotLabel([touching[0].feature])} · ${plans}`;
  const distinct = new Set(touching.map(t => geometryKey(t.feature))).size;
  return distinct === 1
    ? `${touching.length} stacked lots share one boundary · ${plans} · no unit cuts in Vicmap`
    : `${touching.length} lots touch this outline · ${plans}`;
}

/** A point inside the largest part of a parcel for its label: the centroid when inside, else the widest horizontal chord; null for empty geometry. */
export function parcelLabelPoint(polygons: ScenePoint[][][]): ScenePoint | null {
  const area = (ring: ScenePoint[]) => Math.abs(ring.reduce((sum, p, i) => { const q = ring[(i+1)%ring.length]; return sum + p[0]*q[1] - q[0]*p[1]; }, 0)) / 2;
  const candidates = polygons.filter(poly => poly.length && poly[0].length);
  if (!candidates.length) return null;
  const [outer, ...holes] = candidates.reduce((best, poly) => area(poly[0]) > area(best[0]) ? poly : best);
  const twice = outer.reduce((sum, p, i) => { const q = outer[(i+1)%outer.length]; return sum + (p[0]*q[1] - q[0]*p[1]); }, 0);
  const centroid: ScenePoint = twice ? [
    outer.reduce((sum, p, i) => { const q = outer[(i+1)%outer.length]; return sum + (p[0]+q[0])*(p[0]*q[1]-q[0]*p[1]); }, 0) / (3*twice),
    outer.reduce((sum, p, i) => { const q = outer[(i+1)%outer.length]; return sum + (p[1]+q[1])*(p[0]*q[1]-q[0]*p[1]); }, 0) / (3*twice),
  ] : outer[0];
  if (parcelContains(centroid, [[outer, ...holes]])) return centroid;
  const ys = outer.map(p => p[1]), y = (Math.min(...ys)+Math.max(...ys))/2;
  const crossings: number[] = [];
  for (let i = 0, j = outer.length-1; i < outer.length; j = i++) {
    const a = outer[i], b = outer[j];
    if ((a[1] > y) !== (b[1] > y)) crossings.push((b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]);
  }
  crossings.sort((a, b) => a-b);
  let best: ScenePoint = centroid, width = -1;
  for (let i = 0; i+1 < crossings.length; i += 2) if (crossings[i+1]-crossings[i] > width) { width = crossings[i+1]-crossings[i]; best = [(crossings[i]+crossings[i+1])/2, y]; }
  return best;
}

const LIVE_STATUSES = new Set(["candidate", "confirmed", "needs_adjustment"]);
const toMetres = ([x, y]: ScenePoint, m: number[][]): ScenePoint => [x*m[0][0]+y*m[1][0]+m[2][0], x*m[0][1]+y*m[1][1]+m[2][1]];
const ringAreaM2 = (ring: ScenePoint[], scene: SceneModelSnapshot): number => {
  const metres = ring.map(p => toMetres(p, scene.pixel_to_meters));
  return Math.abs(metres.reduce((sum, p, i) => { const q = metres[(i+1)%metres.length]; return sum + p[0]*q[1] - q[0]*p[1]; }, 0)) / 2;
};
const touchesFrame = (ring: ScenePoint[], scene: SceneModelSnapshot, tolerance = 1): boolean =>
  ring.some(([x, y]) => x <= tolerance || y <= tolerance || x >= scene.image_width-tolerance || y >= scene.image_height-tolerance);
const KIND_NOUNS: Record<string, string> = { building: "building", tree: "tree", vegetation: "vegetation", pool: "pool", car: "car", solar_panel: "panel" };

/**
 * One sentence about the selected object's dominant lot: the lot and plan with its share of the outline, whether the frame
 * covers that lot (a frame cut is the frame's doing, not the object's), and the largest live roof on the same lot with the
 * size ratio. A viewing hint computed from cadastral polygons and outlines; it names no owner and changes no label.
 */
export function parcelLotSentence(touching: ParcelTouch[], object: SceneObject | undefined, objects: SceneObject[], scene: SceneModelSnapshot): string | null {
  if (!object || !touching.length || object.ring.length < 3) return null;
  const { feature, share } = touching[0], plan = feature.properties.plan || "plan unknown";
  const label = `${parcelLotLabel([feature])} ${plan}`;
  const noun = KIND_NOUNS[object.kind] || "object";
  const points = parcelImagePolygons(feature, scene).flat(2);
  const complete = points.every(([x, y]) => x >= 0 && y >= 0 && x <= scene.image_width && y <= scene.image_height);
  // The largest live roof on the same lot (>= half of its outline inside it) and this roof's size against it: the
  // primary/secondary evidence a reviewer asked for. Buildings only; other kinds get the lot alone.
  const roofClause = (): string | null => {
    if (object.kind !== "building") return null;
    const members = objects
      .filter(o => o.kind === "building" && LIVE_STATUSES.has(o.status) && o.ring.length >= 3 && parcelsTouchingOutline([feature], o.ring, scene, .5).length > 0)
      .map(o => ({ id: o.id, area: ringAreaM2(o.ring, scene) }))
      .sort((a, b) => b.area - a.area || a.id.localeCompare(b.id));
    const area = ringAreaM2(object.ring, scene), largest = members[0];
    if (!largest || largest.id === object.id) return `the largest roof on this lot (${area.toFixed(1)} m²)`;
    return `largest roof here is ${largest.id.slice(0, 8)}… ${largest.area.toFixed(1)} m²; this is ${area.toFixed(1)} m² (ratio ${(area/largest.area).toFixed(2)})`;
  };
  const clause = roofClause();
  if (!complete) {
    const cut = touchesFrame(object.ring, scene) ? `the frame cut it, not the ${noun}.` : "only part of the lot is visible.";
    return `Sits on ${label}, which leaves this image; ${cut}${clause ? ` ${clause[0].toUpperCase()}${clause.slice(1)}.` : ""}`;
  }
  const head = `${label} (${Math.round(share*100)}%)`;
  return clause ? `${head}; ${clause}.` : `${head}.`;
}
