import type {
  CustomLayerInterface,
  CustomRenderMethodInput,
  Map as MapLibreMap,
} from "maplibre-gl";
import * as maplibregl from "maplibre-gl";
import * as THREE from "three";
import { buildRoofSections } from "@/lib/roof-geometry";

const MAX_BUILDINGS = 1500;
const ROOF_COLORS = [0xa84a2d, 0x4b5563, 0x7c8794, 0x8a6f52];

function numeric(value: unknown, fallback = 0): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
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
  private anchorMerc = maplibregl.MercatorCoordinate.fromLngLat([0, 0], 0);
  private meterScale = this.anchorMerc.meterInMercatorCoordinateUnits();
  private pendingRebuild = false;
  public isVisible = true;

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

    this.scene.add(new THREE.AmbientLight(0xffffff, 1.05));
    const sun = new THREE.DirectionalLight(0xfff4e0, 1.55);
    sun.position.set(180, -140, 280);
    this.scene.add(sun);
    const fill = new THREE.DirectionalLight(0x9ec5ff, 0.4);
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

    const features = map.querySourceFeatures("local-vector", {
      sourceLayer: "building",
    });

    const wallPositions: number[] = [];
    const roofPositions: number[][] = ROOF_COLORS.map(() => []);
    const seen = new Set<string>();
    let count = 0;

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

        const centroid = ring.reduce(
          (acc, [x, y]) => [acc[0] + x / ring.length, acc[1] + y / ring.length],
          [0, 0]
        );
        const key = `${Math.round(centroid[0])},${Math.round(centroid[1])}`;
        if (seen.has(key)) continue;
        seen.add(key);
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

    map.triggerRepaint();
  }

  private clearMeshes() {
    for (const child of [...this.buildingGroup.children]) {
      this.buildingGroup.remove(child);
      if (child instanceof THREE.Mesh) {
        child.geometry.dispose();
      }
    }
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
