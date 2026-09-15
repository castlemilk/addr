/**
 * Shared roof geometry: decomposes an extracted roof footprint (metres, x=east,
 * y=north) into convex sections and emits vertex positions (east, north, up)
 * for walls and pitched/flat roof planes. Used by the MapLibre RealRoofLayer
 * and the Deep 3D Studio so both render the same building shape.
 */

export type RoofRing = [number, number][];

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

function triangulateRing(points: { x: number; y: number }[]): [number, number, number][] {
  const faces: [number, number, number][] = [];
  const indices = points.map((_, index) => index);
  const area = (a: number, b: number, c: number) =>
    (points[b].x - points[a].x) * (points[c].y - points[a].y) -
    (points[c].x - points[a].x) * (points[b].y - points[a].y);
  let guard = 0;
  while (indices.length > 3 && guard < points.length * points.length) {
    let clipped = false;
    for (let i = 0; i < indices.length; i++) {
      const prev = indices[(i + indices.length - 1) % indices.length];
      const current = indices[i];
      const next = indices[(i + 1) % indices.length];
      if (area(prev, current, next) <= 0) continue;
      let contains = false;
      for (const other of indices) {
        if (other === prev || other === current || other === next) continue;
        const d1 = area(prev, current, other);
        const d2 = area(current, next, other);
        const d3 = area(next, prev, other);
        if (d1 >= 0 && d2 >= 0 && d3 >= 0) {
          contains = true;
          break;
        }
      }
      if (contains) continue;
      faces.push([prev, current, next]);
      indices.splice(i, 1);
      clipped = true;
      break;
    }
    guard += 1;
    if (!clipped) break;
  }
  if (indices.length === 3) {
    faces.push([indices[0], indices[1], indices[2]]);
  }
  return faces;
}

export interface RoofSectionOptions {
  ring: RoofRing;
  eave: number;
  ridge: number;
  roofType: string;
  ridgeAngleDeg: number | null;
  minSectionArea?: number;
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
  const wall: number[] = [];
  const roof: number[] = [];
  if (ring.length < 3) return { wall, roof };

  for (let index = 0; index < ring.length; index++) {
    addQuad(wall, ring[index], ring[(index + 1) % ring.length], 0, eave);
  }

  if (roofType === "flat" || ridge - eave < 0.4) {
    addFlatCap(roof, ring, ridge);
    return { wall, roof };
  }

  const pieces = decomposeConvexRing(ring);
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
