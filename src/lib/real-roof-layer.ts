import type {
  CustomLayerInterface,
  CustomRenderMethodInput,
  Map as MapLibreMap,
} from "maplibre-gl";
import * as maplibregl from "maplibre-gl";
import * as THREE from "three";
import {
  buildRoofSections,
  buildRooflineMesh,
  buildWallStrip,
  roofColorFor,
} from "@/lib/roof-geometry";
import { shouldUseRooflineMesh } from "@/lib/address-roofs";
import type { AddressRoofRecord } from "@/lib/address-roofs";

const MAX_BUILDINGS = 1500;
const ROOF_COLORS = [0xa84a2d, 0x4b5563, 0x7c8794, 0x8a6f52];

function numeric(value: unknown, fallback = 0): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function pointInRing(point: [number, number], ring: number[][]): boolean {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    const intersects =
      yi > point[1] !== yj > point[1] &&
      point[0] < ((xj - xi) * (point[1] - yi)) / (yj - yi || 1e-9) + xi;
    if (intersects) inside = !inside;
  }
  return inside;
}

function ringCentroid(ring: number[][]): [number, number] {
  let x = 0;
  let y = 0;
  for (const [px, py] of ring) {
    x += px / ring.length;
    y += py / ring.length;
  }
  return [x, y];
}

function geometryFrom(positions: number[]): THREE.BufferGeometry | null {
  if (positions.length === 0) return null;
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute(
    "position",
    new THREE.Float32BufferAttribute(positions, 3)
  );
  geometry.computeVertexNormals();
  return geometry;
}

/**
 * Renders pitched-roof building meshes built from real vector-tile footprints.
 * Consumes the `building` source layer produced by the AI tile pipeline
 * (height, eave_height, ridge_height, roof_type, ridge_angle).
 */
export class RealRoofLayer implements CustomLayerInterface {
  id = "real-roof-3d";
  type = "custom" as const;
  renderingMode = "3d" as const;

  private map: MapLibreMap | null = null;
  private renderer: THREE.WebGLRenderer | null = null;
  private camera = new THREE.Camera();
  private scene = new THREE.Scene();
  private buildingGroup = new THREE.Group();
  private wallMaterial: THREE.MeshStandardMaterial | null = null;
  private roofMaterials: THREE.MeshStandardMaterial[] = [];
  /** Per-rebuild materials for corrected surface colours; disposed on rebuild. */
  private correctionMaterials: THREE.MeshStandardMaterial[] = [];
  private anchorMerc = maplibregl.MercatorCoordinate.fromLngLat([0, 0], 0);
  private meterScale = this.anchorMerc.meterInMercatorCoordinateUnits();
  private pendingRebuild = false;
  private addressRecords: AddressRoofRecord[] = [];
  private rooflinesEnabled = false;
  public isVisible = true;
  private sceneCoverage: number[][][][] = [];

  setSceneCoverage(polygons: number[][][][]) {
    this.sceneCoverage = polygons;
    this.scheduleRebuild();
  }

  /**
   * All address records; the active render set depends on the roofline mode.
   * CRM-corrected records always replace the stale extracted footprint on the
   * map (their corrected roof is drawn, the MVT footprint is skipped).
   */
  setAddressRecords(records: AddressRoofRecord[]) {
    this.addressRecords = records.filter(
      (record) =>
        Array.isArray(record.ring_meters) && record.ring_meters.length >= 3
    );
    this.scheduleRebuild();
  }

  /**
   * Experimental: render the per-plane roofline partition for every record
   * (not just CRM-corrected ones). Off by default: blobby detector footprints
   * and monocular-depth plane fits make the meshes look worse than the tile
   * prisms for most buildings.
   */
  setRooflines(enabled: boolean) {
    if (this.rooflinesEnabled === enabled) return;
    this.rooflinesEnabled = enabled;
    this.scheduleRebuild();
  }

  onAdd(map: MapLibreMap, gl: WebGL2RenderingContext) {
    this.map = map;
    this.renderer = new THREE.WebGLRenderer({
      canvas: map.getCanvas(),
      context: gl as unknown as WebGLRenderingContext,
      antialias: true,
    });
    this.renderer.autoClear = false;
    this.camera.matrixAutoUpdate = false;

    this.wallMaterial = new THREE.MeshStandardMaterial({
      color: 0xe9e5dd,
      roughness: 0.85,
      metalness: 0.02,
      side: THREE.DoubleSide,
      flatShading: true,
    });
    this.roofMaterials = ROOF_COLORS.map(
      (color) =>
        new THREE.MeshStandardMaterial({
          color,
          roughness: 0.7,
          metalness: 0.08,
          side: THREE.DoubleSide,
          flatShading: true,
        })
    );
    // Material buckets for corrected surface colours are built per rebuild
    // from the shared registry (see below); nothing to pre-create here.

    this.scene.add(new THREE.AmbientLight(0xffffff, 1.05));
    const sun = new THREE.DirectionalLight(0xfff4e0, 1.55);
    sun.position.set(180, -140, 280);
    this.scene.add(sun);
    const fill = new THREE.DirectionalLight(0x9ec5ff, 0.3);
    fill.position.set(-160, 120, 90);
    this.scene.add(fill);
    this.scene.add(this.buildingGroup);
  }

  setVisible(visible: boolean) {
    this.isVisible = visible;
    if (!visible) {
      this.clearMeshes();
    } else {
      this.scheduleRebuild();
    }
    this.map?.triggerRepaint();
  }

  scheduleRebuild() {
    if (this.pendingRebuild) return;
    this.pendingRebuild = true;
    requestAnimationFrame(() => {
      this.pendingRebuild = false;
      this.rebuild();
    });
  }

  rebuild() {
    const map = this.map;
    if (!map || !this.isVisible || !this.renderer) return;

    const center = map.getCenter();
    const lon0 = center.lng;
    const lat0 = center.lat;
    this.anchorMerc = maplibregl.MercatorCoordinate.fromLngLat([lon0, lat0], 0);
    this.meterScale = this.anchorMerc.meterInMercatorCoordinateUnits();
    const cosLat = Math.cos((lat0 * Math.PI) / 180);
    const coveredByScene = (lon: number, lat: number) => this.sceneCoverage.some(polygon =>
      pointInRing([lon, lat], polygon[0]) && !polygon.slice(1).some(hole => pointInRing([lon, lat], hole)));

    const features = map.querySourceFeatures("local-vector", {
      sourceLayer: "building",
    });

    const wallPositions: number[] = [];
    const roofPositions: number[][] = ROOF_COLORS.map(() => []);
    // Records with a corrected surface material render in registry colours
    // instead of the hashed palette bucket: colour -> positions.
    const materialBuckets = new Map<number, number[]>();
    const seen = new Set<string>();
    let count = 0;

    const activeRecords = this.addressRecords.filter(
      (record) =>
        record.boundary_source === "crm" ||
        (this.rooflinesEnabled && (record.rooflines?.parts?.length ?? 0) > 0)
    );

    // Records in the local metres frame; drawing records replace the MVT
    // footprints they match (grid lookup keeps the suppression test cheap
    // across 600+ records). Records that draw nothing (e.g. gated-out
    // roofline partitions) must not suppress their tile prism, or enabling
    // the experimental layer would punch holes in the map.
    const entries = activeRecords.map((record) => {
      const toLocal = (x: number, y: number): [number, number] => [
        (record.centroid.lon - lon0) * 111320 * cosLat + x,
        (record.centroid.lat - lat0) * 111320 + y,
      ];
      const parts = (shouldUseRooflineMesh(record) ? record.rooflines?.parts ?? [] : [])
        .filter((part) => part.ring.length >= 3 && part.ring.length === part.z.length)
        .map((part) => ({
          ring: part.ring.map(([x, y]) => toLocal(x, y)),
          z: part.z,
        }));
      // parts is already gate-filtered above, so non-empty implies coherent.
      const drawsRoofline =
        parts.length > 0 && (this.rooflinesEnabled || record.boundary_source === "crm");
      return {
        record,
        ring: record.ring_meters.map(([x, y]) => toLocal(x, y)),
        parts,
        draws: drawsRoofline || record.boundary_source === "crm",
      };
    });
    const suppressed = activeRecords
      .map((record) => record.suppressed_centroid)
      .filter((value): value is { lat: number; lon: number } => Boolean(value))
      .map(
        (value) =>
          [
            (value.lon - lon0) * 111320 * cosLat,
            (value.lat - lat0) * 111320,
          ] as [number, number]
      );

    const CELL_METERS = 12;
    const cellKey = (x: number, y: number) =>
      `${Math.round(x / CELL_METERS)},${Math.round(y / CELL_METERS)}`;
    const recordGrid = new Map<
      string,
      { cx: number; cy: number; ring: [number, number][] }[]
    >();
    for (const entry of entries) {
      if (!entry.draws) continue;
      const [cx, cy] = ringCentroid(entry.ring);
      const key = cellKey(cx, cy);
      const bucket = recordGrid.get(key) ?? [];
      bucket.push({ cx, cy, ring: entry.ring });
      recordGrid.set(key, bucket);
    }

    const isReplaced = (centroid: [number, number]) => {
      if (
        suppressed.some(
          (point) => Math.hypot(point[0] - centroid[0], point[1] - centroid[1]) < 12
        )
      ) {
        return true;
      }
      const gx = Math.round(centroid[0] / CELL_METERS);
      const gy = Math.round(centroid[1] / CELL_METERS);
      for (let dx = -1; dx <= 1; dx++) {
        for (let dy = -1; dy <= 1; dy++) {
          for (const entry of recordGrid.get(`${gx + dx},${gy + dy}`) ?? []) {
            if (Math.hypot(entry.cx - centroid[0], entry.cy - centroid[1]) < 12) {
              return true;
            }
            if (pointInRing(centroid, entry.ring)) return true;
          }
        }
      }
      return false;
    };

    for (const { record, ring, parts, draws } of entries) {
      if (!draws) continue;
      if (coveredByScene(record.centroid.lon, record.centroid.lat)) continue;
      const ridge = Math.max(numeric(record.ridge_height), numeric(record.height), 2);
      const eave = Math.min(numeric(record.eave_height, ridge * 0.6) || ridge * 0.6, ridge);
      const centroid = ringCentroid(ring);
      const colorIndex =
        Math.abs(Math.round(centroid[0] * 7 + centroid[1] * 13)) % ROOF_COLORS.length;
      // A corrected surface material wins over the hashed palette colour.
      const materialColor =
        record.roof_material != null
          ? roofColorFor(record.roof_material, ROOF_COLORS[colorIndex])
          : ROOF_COLORS[colorIndex];
      const pushRoof = (positions: number[]) => {
        if (materialColor === ROOF_COLORS[colorIndex]) {
          roofPositions[colorIndex].push(...positions);
          return;
        }
        const bucket = materialBuckets.get(materialColor) ?? [];
        bucket.push(...positions);
        materialBuckets.set(materialColor, bucket);
      };
      if (parts.length > 0 && (this.rooflinesEnabled || record.boundary_source === "crm")) {
        pushRoof(buildRooflineMesh(parts));
        wallPositions.push(...buildWallStrip(ring, 0, Math.max(1.5, eave), parts));
      } else if (record.boundary_source === "crm") {
        const { wall, roof } = buildRoofSections({
          ring,
          eave,
          ridge,
          roofType: String(record.roof_type ?? "hipped"),
          ridgeAngleDeg: record.ridge_angle ?? null,
        });
        wallPositions.push(...wall);
        pushRoof(roof);
      }
      count++;
    }

    for (const feature of features) {
      if (count >= MAX_BUILDINGS) break;
      const geometry = feature.geometry;
      const properties = feature.properties ?? {};
      const ridgeHeight =
        numeric(properties.ridge_height) ||
        numeric(properties.render_height) ||
        numeric(properties.height, 6);
      if (ridgeHeight < 2) continue;
      const eaveHeight = Math.min(
        numeric(properties.eave_height, ridgeHeight * 0.6) || ridgeHeight * 0.6,
        ridgeHeight
      );
      const roofType = String(
        properties.roof_type ?? (ridgeHeight - eaveHeight < 0.8 ? "flat" : "hipped")
      );
      const ridgeAngle = properties.ridge_angle != null
        ? numeric(properties.ridge_angle)
        : null;

      const polygons =
        geometry?.type === "Polygon"
          ? [geometry.coordinates]
          : geometry?.type === "MultiPolygon"
          ? geometry.coordinates
          : [];

      for (const polygon of polygons) {
        const rawRing = polygon[0] as [number, number][];
        if (!rawRing || rawRing.length < 3) continue;
        const geographicCenter = ringCentroid(rawRing);
        if (coveredByScene(...geographicCenter)) continue;
        const ring = rawRing.map(
          ([lon, lat]) =>
            [
              (lon - lon0) * 111320 * cosLat,
              (lat - lat0) * 111320,
            ] as [number, number]
        );
        const first = ring[0];
        const last = ring[ring.length - 1];
        if (Math.abs(first[0] - last[0]) < 1e-6 && Math.abs(first[1] - last[1]) < 1e-6) {
          ring.pop();
        }
        if (ring.length < 3) continue;

        const centroid = ringCentroid(ring);
        const key = `${Math.round(centroid[0])},${Math.round(centroid[1])}`;
        if (seen.has(key)) continue;
        seen.add(key);
        if (isReplaced(centroid)) continue;
        count++;

        const colorIndex =
          Math.abs(Math.round(centroid[0] * 7 + centroid[1] * 13)) % ROOF_COLORS.length;
        const { wall, roof } = buildRoofSections({
          ring,
          eave: eaveHeight,
          ridge: ridgeHeight,
          roofType,
          ridgeAngleDeg: ridgeAngle,
        });
        wallPositions.push(...wall);
        roofPositions[colorIndex].push(...roof);
      }
    }

    this.clearMeshes();
    const wallGeometry = geometryFrom(wallPositions);
    if (wallGeometry && this.wallMaterial) {
      this.buildingGroup.add(new THREE.Mesh(wallGeometry, this.wallMaterial));
    }
    roofPositions.forEach((positions, index) => {
      const geometry = geometryFrom(positions);
      if (geometry) {
        this.buildingGroup.add(new THREE.Mesh(geometry, this.roofMaterials[index]));
      }
    });
    materialBuckets.forEach((positions, color) => {
      const geometry = geometryFrom(positions);
      if (geometry) {
        const material = new THREE.MeshStandardMaterial({
          color,
          roughness: 0.7,
          metalness: 0.08,
          side: THREE.DoubleSide,
          flatShading: true,
        });
        this.correctionMaterials.push(material);
        this.buildingGroup.add(new THREE.Mesh(geometry, material));
      }
    });

    map.triggerRepaint();
  }

  private clearMeshes() {
    for (const child of [...this.buildingGroup.children]) {
      this.buildingGroup.remove(child);
      if (child instanceof THREE.Mesh) {
        child.geometry.dispose();
      }
    }
    for (const material of this.correctionMaterials) {
      material.dispose();
    }
    this.correctionMaterials = [];
  }

  render(gl: WebGL2RenderingContext, options: CustomRenderMethodInput) {
    if (!this.renderer || !this.map || !this.isVisible) return;
    const matrixArray = Array.from(options.defaultProjectionData.mainMatrix) as number[];
    const matrix = new THREE.Matrix4().fromArray(matrixArray);
    const local = new THREE.Matrix4()
      .makeTranslation(this.anchorMerc.x, this.anchorMerc.y, this.anchorMerc.z)
      .scale(new THREE.Vector3(this.meterScale, -this.meterScale, this.meterScale));
    this.camera.projectionMatrix = matrix.multiply(local);

    gl.enable(gl.DEPTH_TEST);
    gl.depthFunc(gl.LEQUAL);
    this.renderer.resetState();
    this.renderer.render(this.scene, this.camera);
  }

  onRemove() {
    this.clearMeshes();
    this.wallMaterial?.dispose();
    this.roofMaterials.forEach((material) => material.dispose());
    if (this.renderer) {
      this.renderer.dispose();
      this.renderer = null;
    }
  }
}
