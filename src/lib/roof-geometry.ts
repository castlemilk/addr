import { ShapeUtils, Vector2 } from "three";

/**
 * Shared roof geometry: decomposes an extracted roof footprint (metres, x=east,
 * y=north) into convex sections and emits vertex positions (east, north, up)
 * for walls and pitched/flat roof planes. Used by the MapLibre RealRoofLayer
 * and the Deep 3D Studio so both render the same building shape.
 */

export type RoofRing = [number, number][];

/**
 * Roof surface materials to mesh colours. Only corrections set a material
 * today (e.g. terracotta red tile via the CRM roof override); everything
 * else falls back to the caller's type/palette colour. A future pipeline
 * imagery-hue pass can extend this registry without touching the renderers.
 */
export const ROOF_MATERIAL_COLORS: Record<string, number> = {
  terracotta: 0xa84a2d,
};

export function roofColorFor(
  material: string | null | undefined,
  fallback: number
): number {
  if (!material) return fallback;
  return ROOF_MATERIAL_COLORS[material.trim().toLowerCase()] ?? fallback;
}

interface LocalPoint {
  x: number;
  y: number;
  t: number;
}

function addQuad(
  out: number[],
  a: RoofRing[number],
  b: RoofRing[number],
  z0: number,
  z1: number
): void {
  out.push(a[0], a[1], z0, b[0], b[1], z0, b[0], b[1], z1);
  out.push(a[0], a[1], z0, b[0], b[1], z1, a[0], a[1], z1);
}

function crossOf(o: RoofRing[number], a: RoofRing[number], b: RoofRing[number]): number {
  return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
}

function pointInRing(point: RoofRing[number], ring: RoofRing): boolean {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const a = ring[i];
    const b = ring[j];
    if (
      a[1] > point[1] !== b[1] > point[1] &&
      point[0] < ((b[0] - a[0]) * (point[1] - a[1])) / (b[1] - a[1]) + a[0]
    ) {
      inside = !inside;
    }
  }
  return inside;
}

function segmentsProperlyIntersect(
  p1: RoofRing[number],
  p2: RoofRing[number],
  p3: RoofRing[number],
  p4: RoofRing[number]
): boolean {
  const d1 = crossOf(p3, p4, p1);
  const d2 = crossOf(p3, p4, p2);
  const d3 = crossOf(p1, p2, p3);
  const d4 = crossOf(p1, p2, p4);
  return d1 * d2 < 0 && d3 * d4 < 0;
}

function diagonalInside(ring: RoofRing, i: number, j: number): boolean {
  const a = ring[i];
  const b = ring[j];
  const mid: RoofRing[number] = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
  if (!pointInRing(mid, ring)) return false;
  for (let k = 0; k < ring.length; k++) {
    const c = ring[k];
    const d = ring[(k + 1) % ring.length];
    if (k === i || (k + 1) % ring.length === i) continue;
    if (k === j || (k + 1) % ring.length === j) continue;
    if (segmentsProperlyIntersect(a, b, c, d)) return false;
  }
  return true;
}

function ringIsConvex(ring: RoofRing): boolean {
  let sign = 0;
  for (let i = 0; i < ring.length; i++) {
    const value = crossOf(ring[i], ring[(i + 1) % ring.length], ring[(i + 2) % ring.length]);
    if (Math.abs(value) < 1e-9) continue;
    const current = Math.sign(value);
    if (sign === 0) sign = current;
    else if (current !== sign) return false;
  }
  return true;
}

/** Splits a concave footprint into convex roof sections (max 32 pieces). */
export function decomposeConvexRing(ring: RoofRing): RoofRing[] {
  const pieces: RoofRing[] = [];
  const stack: RoofRing[] = [ring];
  let signedArea = 0;
  for (let i = 0; i < ring.length; i++) {
    const a = ring[i];
    const b = ring[(i + 1) % ring.length];
    signedArea += a[0] * b[1] - b[0] * a[1];
  }
  const orientation = signedArea >= 0 ? 1 : -1;

  while (stack.length > 0 && pieces.length < 32) {
    const piece = stack.pop() as RoofRing;
    if (piece.length < 3) continue;
    if (piece.length <= 4 || ringIsConvex(piece)) {
      pieces.push(piece);
      continue;
    }
    let reflex = -1;
    for (let i = 0; i < piece.length; i++) {
      const value = crossOf(piece[i], piece[(i + 1) % piece.length], piece[(i + 2) % piece.length]);
      if (value * orientation < -1e-9) {
        reflex = i;
        break;
      }
    }
    if (reflex < 0) {
      pieces.push(piece);
      continue;
    }
    let target = -1;
    for (let step = 2; step < piece.length - 1; step++) {
      const j = (reflex + step) % piece.length;
      if (diagonalInside(piece, reflex, j)) {
        target = j;
        break;
      }
    }
    if (target < 0) {
      pieces.push(piece);
      continue;
    }
    const partA =
      target > reflex
        ? piece.slice(reflex, target + 1)
        : piece.slice(reflex).concat(piece.slice(0, target + 1));
    const partB =
      target > reflex
        ? piece.slice(target).concat(piece.slice(0, reflex + 1))
        : piece.slice(target, reflex + 1);
    stack.push(partA, partB);
  }
  return pieces;
}

export function minAreaRect(ring: RoofRing): { angle: number; aspect: number } {
  let best = { area: Infinity, angle: 0, aspect: 1 };
  for (let i = 0; i < ring.length; i++) {
    const a = ring[i];
    const b = ring[(i + 1) % ring.length];
    const theta = Math.atan2(b[1] - a[1], b[0] - a[0]);
    const cos = Math.cos(theta);
    const sin = Math.sin(theta);
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const [x, y] of ring) {
      const rx = x * cos + y * sin;
      const ry = -x * sin + y * cos;
      minX = Math.min(minX, rx);
      maxX = Math.max(maxX, rx);
      minY = Math.min(minY, ry);
      maxY = Math.max(maxY, ry);
    }
    const width = maxX - minX;
    const height = maxY - minY;
    const area = width * height;
    if (area < best.area) {
      const longSide = Math.max(width, height);
      const shortSide = Math.max(0.5, Math.min(width, height));
      best = {
        area,
        angle: width >= height ? theta : theta + Math.PI / 2,
        aspect: longSide / shortSide,
      };
    }
  }
  return best;
}

export function minAreaRectRing(ring: RoofRing): RoofRing {
  let best: {
    area: number;
    corners: [number, number][];
  } | null = null;
  for (let i = 0; i < ring.length; i++) {
    const a = ring[i];
    const b = ring[(i + 1) % ring.length];
    const theta = Math.atan2(b[1] - a[1], b[0] - a[0]);
    const cos = Math.cos(theta);
    const sin = Math.sin(theta);
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const [x, y] of ring) {
      const rx = x * cos + y * sin;
      const ry = -x * sin + y * cos;
      minX = Math.min(minX, rx);
      maxX = Math.max(maxX, rx);
      minY = Math.min(minY, ry);
      maxY = Math.max(maxY, ry);
    }
    const area = (maxX - minX) * (maxY - minY);
    if (!best || area < best.area) {
      const corners: [number, number][] = [
        [minX, minY],
        [maxX, minY],
        [maxX, maxY],
        [minX, maxY],
      ].map(([rx, ry]) => [
        rx * cos - ry * sin,
        rx * sin + ry * cos,
      ]) as [number, number][];
      best = { area, corners };
    }
  }
  return best ? best.corners : ring;
}

function addRoofTent(
  out: number[],
  ring: RoofRing,
  eave: number,
  ridgeHeight: number,
  axisAngle: number,
  roofType: string
): void {
  const count = ring.length;
  if (count < 3) return;

  const centroid = ring.reduce(
    (acc, [x, y]) => [acc[0] + x / count, acc[1] + y / count],
    [0, 0]
  );

  const dir = [Math.cos(axisAngle), Math.sin(axisAngle)];
  const normal = [-dir[1], dir[0]];
  const points: LocalPoint[] = ring.map(([x, y]) => {
    const dx = x - centroid[0];
    const dy = y - centroid[1];
    return { x, y, t: dx * dir[0] + dy * dir[1] };
  });

  const sValues = ring.map(([x, y]) => {
    const dx = x - centroid[0];
    const dy = y - centroid[1];
    return dx * normal[0] + dy * normal[1];
  });
  const minT = Math.min(...points.map((point) => point.t));
  const maxT = Math.max(...points.map((point) => point.t));
  const minS = Math.min(...sValues);
  const maxS = Math.max(...sValues);

  const halfWidth = Math.max(0.2, (maxS - minS) / 2);
  const ridgeMid = (minT + maxT) / 2;
  const ridgeHalf =
    roofType === "gabled"
      ? Math.max(0.05, (maxT - minT) / 2)
      : Math.max(0, (maxT - minT) / 2 - halfWidth);

  const project = (t: number): RoofRing[number] => {
    const clamped = Math.max(ridgeMid - ridgeHalf, Math.min(ridgeMid + ridgeHalf, t));
    return [centroid[0] + dir[0] * clamped, centroid[1] + dir[1] * clamped];
  };

  for (let index = 0; index < count; index++) {
    const current = points[index];
    const next = points[(index + 1) % count];
    const currentRidge = project(current.t);
    const nextRidge = project(next.t);
    const length = Math.hypot(nextRidge[0] - currentRidge[0], nextRidge[1] - currentRidge[1]);
    out.push(
      current.x, current.y, eave,
      next.x, next.y, eave,
      nextRidge[0], nextRidge[1], ridgeHeight
    );
    if (length < 0.05) continue;
    out.push(
      current.x, current.y, eave,
      nextRidge[0], nextRidge[1], ridgeHeight,
      currentRidge[0], currentRidge[1], ridgeHeight
    );
  }
}

function addFlatCap(out: number[], ring: RoofRing, height: number): void {
  const contour = ring.map(([x, y]) => ({ x, y }));
  const faces = triangulateRing(contour);
  for (const [a, b, c] of faces) {
    out.push(ring[a][0], ring[a][1], height);
    out.push(ring[b][0], ring[b][1], height);
    out.push(ring[c][0], ring[c][1], height);
  }
}

export function triangulateRing(points: { x: number; y: number }[]): [number, number, number][] {
  // Quantised partition junctions may duplicate consecutive vertices. The old
  // ear clipper treated those duplicates as points inside every candidate ear,
  // stopped early, and silently left most of the roof unrendered.
  const indices: number[] = [];
  for (let i = 0; i < points.length; i++) {
    const point = points[i];
    if (!Number.isFinite(point.x) || !Number.isFinite(point.y)) return [];
    const previous = points[indices[indices.length - 1]];
    if (!previous || Math.hypot(point.x - previous.x, point.y - previous.y) > 1e-8) indices.push(i);
  }
  if (indices.length > 2) {
    const first = points[indices[0]], last = points[indices[indices.length - 1]];
    if (Math.hypot(first.x - last.x, first.y - last.y) < 1e-8) indices.pop();
  }
  if (indices.length < 3) return [];
  const contour = indices.map(index => new Vector2(points[index].x, points[index].y));
  return ShapeUtils.triangulateShape(contour, []).map(([a, b, c]) => [indices[a], indices[b], indices[c]]);
}

/** Triangulate a footprint without covering courtyard or occlusion gaps. */
export function triangulateFootprint(ring: RoofRing, holes: RoofRing[] = []) {
  const points = [ring,...holes].flat();
  if(!holes.length)return {points,triangles:triangulateRing(ring.map(([x,y])=>({x,y})))};
  const contours=[ring,...holes].map(r=>r.map(p=>new Vector2(...p)));
  return {points,triangles:ShapeUtils.triangulateShape(contours[0],contours.slice(1)) as [number,number,number][]};
}

export function buildWallStrip(
  ring: RoofRing,
  base: number,
  top: number,
  parts: RooflinePart[] = []
): number[] {
  const positions: number[] = [];
  const signedArea = ring.reduce((sum, p, i) => sum + p[0] * ring[(i + 1) % ring.length][1] - ring[(i + 1) % ring.length][0] * p[1], 0);
  const boundary = signedArea < 0 ? [...ring].reverse() : ring;
  for (let i = 0; i < boundary.length; i++) {
    const [x1, y1] = boundary[i];
    const [x2, y2] = boundary[(i + 1) % boundary.length];
    const dx = x2 - x1, dy = y2 - y1, length2 = dx * dx + dy * dy;
    if (length2 < 1e-12) continue;
    const edges: { t1: number; t2: number; z1: number; z2: number }[] = [];
    const breaks = [0, 1];
    for (const part of parts) {
      if (part.ring.length !== part.z.length) continue;
      for (let j = 0; j < part.ring.length; j++) {
        const a = part.ring[j], b = part.ring[(j + 1) % part.ring.length];
        if (![...a, ...b, part.z[j], part.z[(j + 1) % part.z.length]].every(Number.isFinite)) continue;
        const distance = (p: number[]) => Math.abs(dx * (p[1] - y1) - dy * (p[0] - x1)) / Math.sqrt(length2);
        if (distance(a) > 1e-4 || distance(b) > 1e-4) continue;
        const t1 = ((a[0] - x1) * dx + (a[1] - y1) * dy) / length2;
        const t2 = ((b[0] - x1) * dx + (b[1] - y1) * dy) / length2;
        if (Math.abs(t1 - t2) < 1e-8 || Math.max(t1, t2) < 0 || Math.min(t1, t2) > 1) continue;
        edges.push({ t1, t2, z1: part.z[j], z2: part.z[(j + 1) % part.z.length] });
        breaks.push(Math.max(0, Math.min(1, t1)), Math.max(0, Math.min(1, t2)));
      }
    }
    const sorted = [...new Set(breaks)].sort((a, b) => a - b);
    for (let j = 0; j < sorted.length - 1; j++) {
      const a = sorted[j], b = sorted[j + 1], mid = (a + b) / 2;
      if (b - a < 1e-8) continue;
      const edge = edges.find(e => mid >= Math.min(e.t1, e.t2) - 1e-8 && mid <= Math.max(e.t1, e.t2) + 1e-8);
      const height = (t: number) => edge ? edge.z1 + (edge.z2 - edge.z1) * (t - edge.t1) / (edge.t2 - edge.t1) : top;
      const ax = x1 + a * dx, ay = y1 + a * dy, bx = x1 + b * dx, by = y1 + b * dy;
      const az = Math.max(base, height(a)), bz = Math.max(base, height(b));
      positions.push(ax, ay, base, bx, by, base, bx, by, bz);
      positions.push(ax, ay, base, bx, by, bz, ax, ay, az);
    }
  }
  return positions;
}

export interface RooflinePart {
  ring: RoofRing;
  z: number[];
  kind?: string;
  slope_deg?: number;
}

/**
 * Builds per-plane roof surfaces from a roofline partition: each part ring is
 * triangulated (earcut-style, concave-safe) and lifted to its per-vertex plane
 * heights. Where adjacent parts meet with a vertical step delta, vertical connector
 * quads are generated to produce a watertight LoD2 solid model.
 * Positions are metres, x east / y north / z up.
 */
export function buildRooflineMesh(parts: RooflinePart[]): number[] {
  const roof: number[] = [];
  const normalizedParts: { ring: RoofRing; z: number[] }[] = [];

  for (const part of parts) {
    if (part.ring.length < 3 || part.ring.length !== part.z.length) continue;
    let signedArea = 0;
    for (let i = 0; i < part.ring.length; i++) {
      const [x1, y1] = part.ring[i];
      const [x2, y2] = part.ring[(i + 1) % part.ring.length];
      signedArea += x1 * y2 - x2 * y1;
    }
    const flip = signedArea < 0;
    const ring = flip ? [...part.ring].reverse() : part.ring;
    const z = flip ? [...part.z].reverse() : part.z;
    normalizedParts.push({ ring, z });

    const faces = triangulateRing(ring.map(([x, y]) => ({ x, y })));
    for (const [a, b, c] of faces) {
      roof.push(ring[a][0], ring[a][1], z[a]);
      roof.push(ring[b][0], ring[b][1], z[b]);
      roof.push(ring[c][0], ring[c][1], z[c]);
    }
  }

  // Detect shared edge height steps between parts and emit vertical connector walls
  for (let i = 0; i < normalizedParts.length; i++) {
    const partA = normalizedParts[i];
    for (let eA = 0; eA < partA.ring.length; eA++) {
      const p1A = partA.ring[eA];
      const p2A = partA.ring[(eA + 1) % partA.ring.length];
      const z1A = partA.z[eA];
      const z2A = partA.z[(eA + 1) % partA.ring.length];
      const avgA = (z1A + z2A) / 2;

      for (let j = 0; j < normalizedParts.length; j++) {
        if (i === j) continue;
        const partB = normalizedParts[j];
        for (let eB = 0; eB < partB.ring.length; eB++) {
          const p1B = partB.ring[eB];
          const p2B = partB.ring[(eB + 1) % partB.ring.length];
          const z1B = partB.z[eB];
          const z2B = partB.z[(eB + 1) % partB.ring.length];

          // Check if edge A matches reversed edge B (p1A ~= p2B and p2A ~= p1B)
          const d1 = Math.hypot(p1A[0] - p2B[0], p1A[1] - p2B[1]);
          const d2 = Math.hypot(p2A[0] - p1B[0], p2A[1] - p1B[1]);

          if (d1 < 0.15 && d2 < 0.15) {
            const avgB = (z1B + z2B) / 2;
            // Only emit from higher edge to lower edge to prevent duplicate walls
            if (avgA - avgB > 0.25) {
              // Quad: (p1A, z1A) -> (p2A, z2A) -> (p2A, z1B) -> (p1A, z2B)
              roof.push(p1A[0], p1A[1], z1A, p2A[0], p2A[1], z2A, p2A[0], p2A[1], z1B);
              roof.push(p1A[0], p1A[1], z1A, p2A[0], p2A[1], z1B, p1A[0], p1A[1], z2B);
            }
          }
        }
      }
    }
  }

  return roof;
}

export interface RoofSectionOptions {
  ring: RoofRing;
  eave: number;
  ridge: number;
  roofType: string;
  ridgeAngleDeg: number | null;
  minSectionArea?: number;
  /** Extends walls below ground so sloped terrain never shows a gap. */
  baseDrop?: number;
  /**
   * Douglas-Peucker tolerance (metres) applied before roof decomposition.
   * Smooths detector jitter so blobby rings produce fewer, cleaner planes.
   */
  simplifyTolerance?: number;
  /**
   * When convex decomposition yields more sections than this, fall back to a
   * single fitted rectangle for the roof so blobby rings render as one clean
   * plane instead of a jagged multi-gable mesh.
   */
  maxSections?: number;
}

function perpendicularDistance(
  point: RoofRing[number],
  start: RoofRing[number],
  end: RoofRing[number]
): number {
  const dx = end[0] - start[0];
  const dy = end[1] - start[1];
  const length = Math.hypot(dx, dy);
  if (length < 1e-9) return Math.hypot(point[0] - start[0], point[1] - start[1]);
  return Math.abs(dy * point[0] - dx * point[1] + end[0] * start[1] - end[1] * start[0]) / length;
}

function simplifyRing(ring: RoofRing, tolerance: number): RoofRing {
  if (tolerance <= 0 || ring.length < 5) return ring;
  const keep = new Array<boolean>(ring.length).fill(false);
  keep[0] = true;
  keep[ring.length - 1] = true;
  const stack: [number, number][] = [[0, ring.length - 1]];
  while (stack.length > 0) {
    const [first, last] = stack.pop() as [number, number];
    let maxDistance = 0;
    let index = -1;
    for (let i = first + 1; i < last; i++) {
      const distance = perpendicularDistance(ring[i], ring[first], ring[last]);
      if (distance > maxDistance) {
        maxDistance = distance;
        index = i;
      }
    }
    if (index !== -1 && maxDistance > tolerance) {
      keep[index] = true;
      stack.push([first, index], [index, last]);
    }
  }
  const simplified = ring.filter((_, index) => keep[index]);
  return simplified.length >= 3 ? simplified : ring;
}

/**
 * Builds wall and roof vertex positions (metres, x=east, y=north, z=up) for a
 * single footprint. Concave footprints are split into convex roof sections.
 */
export function buildRoofSections(options: RoofSectionOptions): {
  wall: number[];
  roof: number[];
} {
  const { ring, eave, ridge, roofType, ridgeAngleDeg } = options;
  const minSectionArea = options.minSectionArea ?? 3;
  const simplifiedRing = simplifyRing(ring, options.simplifyTolerance ?? 0);
  const baseDrop = options.baseDrop ?? 0;
  const wall: number[] = [];
  const roof: number[] = [];
  if (ring.length < 3) return { wall, roof };

  for (let index = 0; index < simplifiedRing.length; index++) {
    addQuad(
      wall,
      simplifiedRing[index],
      simplifiedRing[(index + 1) % simplifiedRing.length],
      -baseDrop,
      eave
    );
  }

  if (roofType === "flat" || ridge - eave < 0.4) {
    addFlatCap(roof, simplifiedRing, ridge);
    return { wall, roof };
  }

  let pieces = decomposeConvexRing(simplifiedRing);
  const maxSections = options.maxSections ?? 0;
  if (maxSections > 0 && pieces.length > maxSections) {
    const rect = minAreaRectRing(simplifiedRing);
    if (rect.length >= 3) {
      pieces = [rect];
    }
  }
  for (const piece of pieces) {
    if (piece.length < 3) continue;
    let doubleArea = 0;
    for (let index = 0; index < piece.length; index++) {
      const a = piece[index];
      const b = piece[(index + 1) % piece.length];
      doubleArea += a[0] * b[1] - b[0] * a[1];
    }
    if (Math.abs(doubleArea) / 2 < minSectionArea) continue;
    const fitted = minAreaRect(piece);
    const axis =
      pieces.length === 1 && ridgeAngleDeg !== null
        ? (ridgeAngleDeg * Math.PI) / 180
        : fitted.angle;
    const sectionType =
      pieces.length === 1 ? roofType : fitted.aspect >= 1.7 ? "gabled" : "hipped";
    addRoofTent(roof, piece, eave, ridge, axis, sectionType);
  }
  if (roof.length === 0) {
    addFlatCap(roof, ring, ridge);
  }
  return { wall, roof };
}
