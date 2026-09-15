import type {
  CustomLayerInterface,
  CustomRenderMethodInput,
  Map as MapLibreMap,
} from "maplibre-gl";
import * as THREE from "three";
import * as maplibregl from "maplibre-gl";
import { createLod2HouseGroup, createProceduralTree } from "./house-reconstruction";
import type { Address } from "./geo-engine";

/**
 * ThreeHousesAndTreesLayer
 *
 * Implements a MapLibre CustomLayer that renders real-time 3D LoD2 architectural houses,
 * pitched roofs, solar arrays, and volumetric organic 3D trees directly into MapLibre's WebGL pipeline.
 */
export class ThreeHousesAndTreesLayer implements CustomLayerInterface {
  id = "three-3d-houses-trees";
  type = "custom" as const;
  renderingMode = "3d" as const;

  private map: MapLibreMap | null = null;
  private camera = new THREE.Camera();
  private scene = new THREE.Scene();
  private renderer: THREE.WebGLRenderer | null = null;

  // Origin anchor point in Mercator coordinates
  private anchorLon = 145.31252;
  private anchorLat = -37.79044;
  private anchorMerc = maplibregl.MercatorCoordinate.fromLngLat([this.anchorLon, this.anchorLat], 0);
  private meterScale = this.anchorMerc.meterInMercatorCoordinateUnits();

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

    this.setupLighting();
  }

  private setupLighting() {
    // 1. Natural Ambient Skylight
    const ambientLight = new THREE.AmbientLight(0xffffff, 0.95);
    this.scene.add(ambientLight);

    // 2. Directional Sun (matching Melbourne afternoon sun angle: NW illumination)
    const sunLight = new THREE.DirectionalLight(0xfff5e6, 1.5);
    sunLight.position.set(150, 280, 420);
    this.scene.add(sunLight);

    // 3. Cool blue fill light for architectural depth
    const fillLight = new THREE.DirectionalLight(0x90b8f0, 0.55);
    fillLight.position.set(-180, -200, 160);
    this.scene.add(fillLight);
  }

  /**
   * Cleans up scene geometries and materials to avoid WebGL resource leaks.
   */
  private clearScene() {
    while (this.scene.children.length > 0) {
      const obj = this.scene.children[0];
      this.scene.remove(obj);

      obj.traverse((child) => {
        if (child instanceof THREE.Mesh) {
          child.geometry.dispose();
          if (Array.isArray(child.material)) {
            child.material.forEach((material) => material.dispose());
          } else {
            child.material.dispose();
          }
        }
      });
    }
  }

  /**
   * Populates the 3D scene with LoD2 houses and volumetric trees around the target location.
   */
  public updateScene(
    selectedLocation: { lat: number; lon: number } | null,
    addresses: Address[]
  ) {
    this.clearScene();
    this.setupLighting();

    const targetLat = selectedLocation?.lat ?? -37.79044;
    const targetLon = selectedLocation?.lon ?? 145.31252;

    // Anchor Mercator coordinate at the target
    this.anchorLat = targetLat;
    this.anchorLon = targetLon;
    this.anchorMerc = maplibregl.MercatorCoordinate.fromLngLat([this.anchorLon, this.anchorLat], 0);
    this.meterScale = this.anchorMerc.meterInMercatorCoordinateUnits();

    const cosLat = Math.cos((targetLat * Math.PI) / 180);

    // 1. Render Target House (e.g. 27 Cambridge Road, Mooroolbark)
    const targetHouse = createLod2HouseGroup({
      isTarget: true,
      addressTitle: "Target Address",
      roofType: "executive",
      wallColor: 0xf8fafc,
      roofColor: 0x1e293b,
      width: 17.5,
      depth: 18.0,
      wallHeight: 7.5,
      roofHeight: 4.2,
    });
    targetHouse.position.set(0, 0, 0);
    this.scene.add(targetHouse);

    // Target Front & Back Trees
    const targetTree1 = createProceduralTree({ type: "flowering_gum", height: 8.5, canopyRadius: 4.5 });
    targetTree1.position.set(-10.5, -13.0, 0);
    this.scene.add(targetTree1);

    const targetTree2 = createProceduralTree({ type: "eucalyptus", height: 9.8, canopyRadius: 4.8 });
    targetTree2.position.set(11.0, 13.5, 0);
    this.scene.add(targetTree2);

    // 2. Render Neighboring Houses & Trees
    const seenLocations = new Set<string>();
    seenLocations.add(`${targetLat.toFixed(5)},${targetLon.toFixed(5)}`);

    let houseCount = 0;
    for (const addr of addresses) {
      if (houseCount > 45) break;

      const key = `${addr.lat.toFixed(5)},${addr.lon.toFixed(5)}`;
      if (seenLocations.has(key)) continue;
      seenLocations.add(key);

      const dxMeters = (addr.lon - targetLon) * (111320 * cosLat);
      const dyMeters = (addr.lat - targetLat) * 111320;

      const dist = Math.hypot(dxMeters, dyMeters);
      if (dist < 8 || dist > 380) continue;

      const hash = Math.abs(addr.id || Math.round(dist * 100));
      const roofType = hash % 2 === 0 ? "hipped" : "gabled";
      const roofColor = hash % 3 === 0 ? 0xaa4428 : hash % 3 === 1 ? 0x2a323d : 0x5a6775;
      const wallColor = hash % 2 === 0 ? 0xded8cc : 0xeee8de;

      const houseMesh = createLod2HouseGroup({
        isTarget: false,
        roofType,
        roofColor,
        wallColor,
        width: 14.5 + (hash % 5),
        depth: 16.0 + ((hash >> 2) % 5),
        wallHeight: 4.0,
        roofHeight: 3.2,
      });

      houseMesh.position.set(dxMeters, dyMeters, 0);
      this.scene.add(houseMesh);

      // Trees around this property
      const tree1 = createProceduralTree({
        type: "eucalyptus",
        height: 6.5 + (hash % 5) * 0.6,
        canopyRadius: 3.2 + (hash % 3) * 0.4,
      });
      tree1.position.set(dxMeters - 8.5, dyMeters - 9.5, 0);
      this.scene.add(tree1);

      if (hash % 2 === 0) {
        const tree2 = createProceduralTree({
          type: "canopy_shade",
          height: 7.8,
          canopyRadius: 3.8,
        });
        tree2.position.set(dxMeters + 8.5, dyMeters + 10.0, 0);
        this.scene.add(tree2);
      }

      houseCount++;
    }

    // 3. Fallback synthesis if fewer than 10 addresses loaded yet
    if (houseCount < 8) {
      const fallbackOffsets = [
        { dx: -28, dy: 16, rot: 0.2 },
        { dx: 28, dy: 18, rot: 0.25 },
        { dx: -32, dy: -24, rot: 0.3 },
        { dx: 30, dy: -26, rot: 0.25 },
        { dx: -60, dy: 24, rot: 0.15 },
        { dx: 62, dy: 26, rot: 0.2 },
        { dx: -65, dy: -35, rot: 0.35 },
        { dx: 66, dy: -38, rot: 0.28 },
      ];

      fallbackOffsets.forEach((off, i) => {
        const houseMesh = createLod2HouseGroup({
          isTarget: false,
          roofType: i % 2 === 0 ? "hipped" : "gabled",
          roofColor: i % 2 === 0 ? 0xaa4428 : 0x2a323d,
          wallColor: 0xeee8de,
          width: 15.0,
          depth: 16.5,
          wallHeight: 4.2,
          roofHeight: 3.4,
          rotation: off.rot,
        });
        houseMesh.position.set(off.dx, off.dy, 0);
        this.scene.add(houseMesh);

        const tree = createProceduralTree({ height: 7.2, canopyRadius: 3.6 });
        tree.position.set(off.dx - 8.0, off.dy - 9.0, 0);
        this.scene.add(tree);
      });
    }

    // CRITICAL: Disable Three.js frustum culling for all meshes so MapLibre's camera matrix does not cull them
    this.scene.traverse((obj) => {
      obj.frustumCulled = false;
    });

    if (this.map) {
      this.map.triggerRepaint();
    }
  }

  public setVisible(visible: boolean) {
    this.isVisible = visible;
    if (this.map) {
      this.map.triggerRepaint();
    }
  }

  render(gl: WebGL2RenderingContext, options: CustomRenderMethodInput) {
    if (!this.renderer || !this.map || !this.isVisible) return;

    // Convert MapLibre camera matrix to Three.js camera projection
    const matrixArray = Array.from(options.modelViewProjectionMatrix);

    const m = new THREE.Matrix4().fromArray(matrixArray as number[]);
    const l = new THREE.Matrix4()
      .makeTranslation(this.anchorMerc.x, this.anchorMerc.y, this.anchorMerc.z)
      .scale(new THREE.Vector3(this.meterScale, -this.meterScale, this.meterScale));

    this.camera.projectionMatrix = m.multiply(l);

    if (gl && gl.enable) {
      gl.enable(gl.DEPTH_TEST);
      gl.depthFunc(gl.LEQUAL);
    }

    this.renderer.resetState();
    this.renderer.render(this.scene, this.camera);
  }

  onRemove() {
    this.clearScene();
    if (this.renderer) {
      this.renderer.dispose();
      this.renderer = null;
    }
  }
}
