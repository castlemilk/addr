import * as THREE from "three";
import {
  transformPoint,
  type SceneModelSnapshot,
  type SceneObject,
} from "./scene-types";
import { annotationParts } from "./roof-annotations";
import { buildWallStrip } from "./roof-geometry";
import { roofMount, parentRoofMesh } from "./scene-roof-mount";
import { treeSurfaces } from "./scene-tree";
import { carSurfaces } from "./scene-car";
import { isPavedSurface, pavedSurface } from "./scene-surface";
import { sceneVisibility } from "./scene-visibility";
import { isShadeSail } from "./scene-structure";

/** Hide unrelated meshes without changing annotation or layer visibility state. */
export function setSceneIsolation(root: THREE.Group, objects: SceneObject[], selected: string|null, groupIds?:ReadonlySet<string>, objectIds?:ReadonlySet<string>) {
  const included=new Set(groupIds??(selected?[selected]:[]));
  for(let changed=!groupIds;changed;) {
    changed=false;
    for(const object of objects)if(object.parent_id && included.has(object.parent_id) && !included.has(object.id)) {
      included.add(object.id);changed=true;
    }
  }
  for(const mesh of root.children)mesh.visible=((!selected&&!groupIds) || included.has(mesh.userData.objectId)) && (!objectIds || objectIds.has(mesh.userData.objectId));
}

/** Frozen predictions in their own metre frame, independent of edited parents. */
export function populateScenePrediction(
  root: THREE.Group,
  objects: SceneObject[],
  scene: SceneModelSnapshot,
  visible: Set<string>,
) {
  populateScene(root, objects, scene, null, visible);
  const kinds = new Map(objects.map(o => [o.id, o.kind]));
  const discarded = new Set<THREE.Material>();
  for (const child of [...root.children]) {
    if (!(child instanceof THREE.Mesh)) continue;
    for (const material of Array.isArray(child.material) ? child.material : [child.material]) discarded.add(material);
    child.material = new THREE.MeshBasicMaterial({
      color: 0xf4cc89, transparent: true, opacity: .08, depthWrite: false,
      side: THREE.DoubleSide, toneMapped: false,
    });
    child.userData.prediction = true;
    child.raycast = () => {};
    // Creases/eaves, not every triangulation diagonal. X-ray lines keep a
    // smaller original shape visible inside a larger edited surface.
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(child.geometry, kinds.get(child.userData.objectId) === "tree" ? 30 : 1),
      new THREE.LineBasicMaterial({ color: 0xbe6f13,
        depthTest: false, depthWrite: false, toneMapped: false }));
    edges.position.copy(child.position); edges.quaternion.copy(child.quaternion); edges.scale.copy(child.scale);
    edges.userData = { ...child.userData, geometryRole: "prediction-edges" };
    edges.renderOrder = 2;
    edges.raycast = () => {};
    root.add(edges);
  }
  for (const material of discarded) material.dispose();
}

export function populateScene(
  root: THREE.Group,
  objects: SceneObject[],
  scene: SceneModelSnapshot,
  selected: string | null,
  visible: Set<string>,
  objectIds?: ReadonlySet<string>,
) {
  for (const o of objects) {
    if (["rejected","superseded"].includes(o.status) || !visible.has(o.kind) || (objectIds && !objectIds.has(o.id))) continue;
    const ring = o.ring.map((p) => transformPoint(p, scene.pixel_to_meters));
    const holes=o.holes?.map(h=>h.map(p=>transformPoint(p,scene.pixel_to_meters)))||[];
    const shape=new THREE.Shape(ring.map(p=>new THREE.Vector2(...p)));
    shape.holes=holes.map(h=>new THREE.Path(h.map(p=>new THREE.Vector2(...p))));
    if (ring.length < 3) continue;
    const material = new THREE.MeshStandardMaterial({
      color: o.model.color,
      roughness: 0.85,
      side: THREE.DoubleSide,
      emissive: o.id === selected ? 0x194a59 : 0,
      emissiveIntensity: 0.18,
    });
    const add = (geometry: THREE.BufferGeometry, z = 0) => {
      geometry.computeVertexNormals();
      const mesh = new THREE.Mesh(geometry, material);
      mesh.position.z = z;
      mesh.userData.objectId = o.id;
      root.add(mesh);
      return mesh;
    };
    const parts = o.roof_annotation
      ? annotationParts(o.roof_annotation)
      : o.roof_parts;
    const mounted = roofMount(o, objects, scene.pixel_to_meters);
    if(isShadeSail(o)) {
      // Membrane curvature and suspension heights are unmeasured. Never turn a
      // stale rigid-roof prior into a solid building or a purported sail surface.
      const mesh=add(new THREE.ShapeGeometry(shape),o.model.base_m);
      mesh.userData.geometryRole="visible-footprint";
    } else if(sceneVisibility(o,scene).footprintOnly) {
      const mesh=add(new THREE.ShapeGeometry(shape),o.model.base_m);
      mesh.userData.geometryRole="visible-footprint";
    } else if (mounted.positions.length) {
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.Float32BufferAttribute(mounted.positions, 3));
      add(geometry);
    } else if (o.kind === "building" && parts?.length) {
      const roof = parentRoofMesh(o,scene.pixel_to_meters);
      if (roof.length) {
        const geometry = new THREE.BufferGeometry();
        geometry.setAttribute(
          "position",
          new THREE.Float32BufferAttribute(roof, 3),
        );
        add(geometry, o.model.base_m);
        if (o.id === selected) {
          // Show the actual surface creases and eaves, without triangulation
          // diagonals. These lines are a view aid, never annotation geometry.
          material.polygonOffset = true;
          material.polygonOffsetFactor = 1;
          material.polygonOffsetUnits = 1;
          const edges = new THREE.LineSegments(
            new THREE.EdgesGeometry(geometry),
            new THREE.LineBasicMaterial({ color: 0x073f4c, toneMapped: false, depthWrite: false }),
          );
          edges.position.z = o.model.base_m;
          edges.userData.objectId = o.id;
          edges.userData.geometryRole = "roof-edges";
          edges.renderOrder = 1;
          edges.raycast = () => {}; // Keep picking tied to the solid surface.
          root.add(edges);
        }
      }
      // Use the clipped surface edges around gaps, including their actual
      // heights. Retained faces may originally have crossed a new gap.
      const wallParts = holes.length ? Array.from({length:roof.length/9},(_,i)=>({
        ring: [0,3,6].map(k=>[roof[i*9+k],roof[i*9+k+1]] as [number,number]),
        z: [roof[i*9+2],roof[i*9+5],roof[i*9+8]],
      })) : parts.map(p=>({ring:p.ring_meters||p.ring||[],z:p.ring_z||p.z||[]}));
      const walls = buildWallStrip(
        ring,
        0,
        o.model.height_m,
        wallParts,
      );
      for(const hole of holes) {
        const inner=buildWallStrip(hole,0,o.model.height_m,wallParts);
        for(let i=0;i<inner.length;i+=9)walls.push(...inner.slice(i+6,i+9),...inner.slice(i+3,i+6),...inner.slice(i,i+3));
      }
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute(
        "position",
        new THREE.Float32BufferAttribute(walls, 3),
      );
      const mesh = add(geometry, o.model.base_m);
      mesh.material = new THREE.MeshStandardMaterial({
        color: 0xcbd1d3,
        roughness: 1,
      });
    } else if (isPavedSurface(o.kind)) {
      const surface=pavedSurface(ring,o.model,holes),geometry=new THREE.BufferGeometry();
      geometry.setAttribute("position",new THREE.Float32BufferAttribute(surface.positions,3));
      add(geometry);
    } else if (o.kind === "tree") {
      const tree = treeSurfaces(ring, o.model, holes);
      if (!tree) { material.dispose(); continue; }
      const crown = new THREE.BufferGeometry();
      crown.setAttribute("position", new THREE.Float32BufferAttribute(tree.positions, 3));
      add(crown).userData.geometryRole = "tree-crown";
      const trunk = new THREE.CylinderGeometry(tree.trunk_radius*.7, tree.trunk_radius, tree.trunk_height, 10);
      trunk.rotateX(Math.PI / 2);
      trunk.translate(...tree.anchor, o.model.base_m + tree.trunk_height/2);
      const mesh = add(trunk);
      mesh.userData.geometryRole = "tree-trunk";
      mesh.material = new THREE.MeshStandardMaterial({ color: 0x695340 });
    } else {
      const car = o.kind === "car" ? carSurfaces(ring,o.model) : null;
      add(
        new THREE.ExtrudeGeometry(shape, {
          depth: car ? car.body_height : Math.max(0.025,o.model.height_m),
          bevelEnabled: false,
        }),
        o.model.base_m,
      );
      if (car?.roof.length) {
        const roof=new THREE.BufferGeometry(),glass=new THREE.BufferGeometry();
        roof.setAttribute("position",new THREE.Float32BufferAttribute(car.roof,3));
        glass.setAttribute("position",new THREE.Float32BufferAttribute(car.glass,3));
        add(roof);
        add(glass).material=new THREE.MeshStandardMaterial({color:0x243744,roughness:.3,
          emissive:o.id===selected?0x194a59:0,emissiveIntensity:.6});
      }
    }
  }
}
