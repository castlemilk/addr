export type Position = [number, number];
export type Geometry =
  | { type: "Point"; coordinates: Position }
  | { type: "LineString"; coordinates: Position[] }
  | { type: "Polygon"; coordinates: Position[][] };

export type ReviewStatus = "unreviewed" | "in_review" | "approved" | "rejected";
export type Tool = "select" | "pan" | "point" | "line" | "rectangle" | "polygon" | "crop";
export interface Annotation {
  id: string;
  labelId: string;
  geometry: Geometry;
  notes?: string;
  properties?: Record<string, unknown>;
}
export interface Crop { x: number; y: number; width: number; height: number }
export interface Project {
  id: string; name: string; description: string; revision: number;
  createdAt?: string; updatedAt?: string;
}
export interface Label {
  id: string; projectId: string; name: string; color: string; revision: number;
  createdAt?: string; updatedAt?: string;
}
export interface Item {
  id: string;
  projectId: string;
  title: string;
  adapter: string;
  source: { url?: string; width?: number; height?: number; center?: Position; zoom?: number; attribution?: string; [key: string]: unknown };
  metadata: Record<string, unknown>;
  tags: string[];
  status: ReviewStatus;
  notes: string;
  crop?: Crop;
  annotations: Annotation[];
  agentReviews?: AgentReview[];
  /** List summaries only (ListItems summary=true): the annotation count; the entry carries no annotations, notes or metadata. */
  annotationCount?: number; summary?: boolean;
  revision: number;
  createdAt?: string; updatedAt?: string;
}
export interface AgentReview {
  id: string; annotationId?: string; reviewer: string;
  decision: "approved" | "rejected" | "needs_adjustment" | "uncertain";
  notes: string; vegetationType?: string; source: "agent:fieldwork";
  humanApproval: false; trainingEligible: false; basedOnRevision: number;
  createdAt: string; supersedes?: string; subject: Record<string, unknown>;
}
export interface AdapterInfo { id: string; name: string; coordinateSystem: string }
export const STATUSES: { value: ReviewStatus; label: string }[] = [
  { value: "unreviewed", label: "Not reviewed" },
  { value: "in_review", label: "In review" },
  { value: "approved", label: "Approved" },
  { value: "rejected", label: "Rejected" },
];
export const LABEL_COLORS = ["#2563eb", "#0891b2", "#059669", "#d97706", "#e11d48", "#7c3aed", "#475569"];
export const uid = () => crypto.randomUUID();
export const cloneItem = (item: Item): Item => JSON.parse(JSON.stringify(item));
export const isDirty = (saved: Item, draft: Item) => JSON.stringify(saved) !== JSON.stringify(draft);
export function vertices(geometry: Geometry): Position[] {
  if (geometry.type === "Point") return [geometry.coordinates];
  if (geometry.type === "LineString") return geometry.coordinates;
  return geometry.coordinates[0].slice(0, -1);
}
export function replaceVertex(geometry: Geometry, index: number, position: Position): Geometry {
  if (geometry.type === "Point") return { type: "Point", coordinates: position };
  if (geometry.type === "LineString") return { type: "LineString", coordinates: geometry.coordinates.map((p, i) => i === index ? position : p) };
  const ring = geometry.coordinates[0].slice(0, -1).map((p, i) => i === index ? position : p);
  return { type: "Polygon", coordinates: [[...ring, ring[0]], ...geometry.coordinates.slice(1)] };
}
// Removes one outer-ring vertex of a Polygon and re-closes the ring. The ring must keep at
// least 3 points; below that, and for other geometry types, the input is returned unchanged.
export function removeVertex(geometry: Geometry, index: number): Geometry {
  if (geometry.type !== "Polygon") return geometry;
  const ring = geometry.coordinates[0].slice(0, -1);
  if (ring.length <= 3 || index < 0 || index >= ring.length) return geometry;
  const next = ring.filter((_, i) => i !== index);
  return { type: "Polygon", coordinates: [[...next, next[0]], ...geometry.coordinates.slice(1)] };
}
// Inserts a point on the edge that starts at `index` (for a Polygon, the edge from the last point closes the ring) and
// re-closes the ring. Out-of-range indexes and Points return the input unchanged.
export function insertVertex(geometry: Geometry, index: number, position: Position): Geometry {
  if (geometry.type === "Point") return geometry;
  const points = vertices(geometry);
  const edges = geometry.type === "Polygon" ? points.length : points.length - 1;
  if (index < 0 || index >= edges) return geometry;
  const next = [...points.slice(0, index + 1), position, ...points.slice(index + 1)];
  if (geometry.type === "LineString") return { type: "LineString", coordinates: next };
  return { type: "Polygon", coordinates: [[...next, next[0]], ...geometry.coordinates.slice(1)] };
}
// Moves the edge that starts at `index` by the part of `delta` along the edge's normal, so its neighbours keep their
// direction and a right-angle outline stays right-angled. Out-of-range indexes and Points return the input unchanged.
export function moveEdge(geometry: Geometry, index: number, delta: Position): Geometry {
  if (geometry.type === "Point") return geometry;
  const points = vertices(geometry);
  const edges = geometry.type === "Polygon" ? points.length : points.length - 1;
  if (index < 0 || index >= edges) return geometry;
  const j = (index + 1) % points.length;
  const [ax, ay] = points[index], [bx, by] = points[j];
  const length = Math.hypot(bx - ax, by - ay);
  if (!length) return geometry;
  const [nx, ny] = [-(by - ay) / length, (bx - ax) / length];
  const along = delta[0] * nx + delta[1] * ny;
  const shift = (p: Position): Position => [p[0] + nx * along, p[1] + ny * along];
  const next = points.map((p, i) => i === index || i === j ? shift(p) : p);
  if (geometry.type === "LineString") return { type: "LineString", coordinates: next };
  return { type: "Polygon", coordinates: [[...next, next[0]], ...geometry.coordinates.slice(1)] };
}
export function makeGeometry(tool: Tool, points: Position[]): Geometry | null {
  if (tool === "point" && points.length) return { type: "Point", coordinates: points[0] };
  if (tool === "line" && points.length >= 2) return { type: "LineString", coordinates: points };
  if (tool === "polygon" && points.length >= 3) return { type: "Polygon", coordinates: [[...points, points[0]]] };
  if (tool === "rectangle" && points.length === 2) {
    const [a, b] = points;
    if (a[0] === b[0] || a[1] === b[1]) return null;
    const ring: Position[] = [a, [b[0], a[1]], b, [a[0], b[1]], a];
    return { type: "Polygon", coordinates: [ring] };
  }
  return null;
}
export function normaliseItem(item: Item): Item {
  return { ...item, metadata: item.metadata ?? {}, tags: item.tags ?? [], notes: item.notes ?? "", annotations: item.annotations ?? [] };
}
