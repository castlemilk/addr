import * as THREE from "three";
import { MercatorCoordinate, type CustomLayerInterface, type CustomRenderMethodInput, type Map as MapLibreMap } from "maplibre-gl";
import { populateScene } from "./scene-geometry";
import { SCENE_CLASSES } from "./scene-types";
import type { SceneMapData } from "./scene-map-types";

/** Render the exact same saved object geometry as the CRM in its geographic frame. */
export class SceneMapLayer implements CustomLayerInterface {
  id = "scene-review-3d";
  type = "custom" as const;
  renderingMode = "3d" as const;
  private renderer?: THREE.WebGLRenderer;
  private camera = new THREE.Camera();
  private scene = new THREE.Scene();
  private group = new THREE.Group();
  private map?: MapLibreMap;
  private anchor = MercatorCoordinate.fromLngLat([0, 0]);
  private scale = 1;
  private data?: SceneMapData;
  private visible = new Set<string>(SCENE_CLASSES);
  private shown = true;
  onAdd(map: MapLibreMap, gl: WebGL2RenderingContext) {
    this.map = map;
    this.renderer = new THREE.WebGLRenderer({ canvas: map.getCanvas(), context: gl, antialias: true });
    this.renderer.autoClear = false;
    this.scene.clear(); this.scene.add(this.group);
    const sky = new THREE.HemisphereLight(0xffffff, 0x718077, 1.1); sky.position.set(0, 0, 1); this.scene.add(sky);
    const sun = new THREE.DirectionalLight(0xffffff, 2.5); sun.position.set(-60, -20, 80); this.scene.add(sun);
    this.rebuild();
  }
  setData(data: SceneMapData | undefined, visible: Set<string>, shown: boolean) {
    this.data = data; this.visible = visible; this.shown = shown; this.rebuild();
  }
  private clear() {
    const materials = new Set<THREE.Material>();
    this.group.traverse(child => {
      if (child instanceof THREE.Mesh || child instanceof THREE.LineSegments) {
        child.geometry.dispose(); for (const material of Array.isArray(child.material) ? child.material : [child.material]) materials.add(material);
      }
    });
    materials.forEach(m => m.dispose()); this.group.clear();
  }
  private rebuild() {
    this.clear();
    const first = this.data?.scenes[0];
    if (!first || !this.shown || !this.renderer) { this.map?.triggerRepaint(); return; }
    this.anchor = MercatorCoordinate.fromLngLat([first.model.centroid.lon, first.model.centroid.lat]);
    this.scale = this.anchor.meterInMercatorCoordinateUnits();
    for (const {model, visible_ids} of this.data!.scenes) {
      const objects = new THREE.Group(), anchor = MercatorCoordinate.fromLngLat([model.centroid.lon, model.centroid.lat]);
      populateScene(objects, model.objects, model, null, this.visible, new Set(visible_ids));
      objects.position.set((anchor.x - this.anchor.x) / this.scale, -(anchor.y - this.anchor.y) / this.scale, 0);
      objects.scale.setScalar(anchor.meterInMercatorCoordinateUnits() / this.scale);
      this.group.add(objects);
    }
    this.map?.triggerRepaint();
  }
  render(gl: WebGL2RenderingContext, input: CustomRenderMethodInput) {
    if (!this.renderer || !this.shown) return;
    this.camera.projectionMatrix = new THREE.Matrix4().fromArray(Array.from(input.defaultProjectionData.mainMatrix))
      .multiply(new THREE.Matrix4().makeTranslation(this.anchor.x, this.anchor.y, 0).scale(new THREE.Vector3(this.scale, -this.scale, this.scale)));
    gl.enable(gl.DEPTH_TEST); gl.depthFunc(gl.LEQUAL); this.renderer.resetState(); this.renderer.render(this.scene, this.camera);
  }
  onRemove() { this.clear(); this.renderer?.dispose(); this.renderer = undefined; }
}
