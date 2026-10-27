import * as THREE from "three";
import { annotationFromModel, withRoofPointHeight, type RoofAnnotation } from "./roof-annotations";
import type { SceneObject } from "./scene-types";
import { hasConventionalRoof } from "./scene-structure";

export const heightIsThickness = (kind: SceneObject["kind"]) =>
  ["road", "driveway", "footpath", "solar_panel", "skylight"].includes(kind);

export function sceneRoofGraph(object: SceneObject) {
  return hasConventionalRoof(object) ? object.roof_annotation || (object.roof_parts?.length
    ? annotationFromModel(object.roof_parts, object.roof_lines || [], object.roof_type) : null) : null;
}

/** Moving a roof vertically must preserve its shape within the saved Z range. */
export function objectHeightRange(object: SceneObject) {
  const graph = sceneRoofGraph(object);
  if (!graph?.vertices.length) return { min: 0, max: 150 };
  const z = graph.vertices.map(p => p[2]);
  return { min: Math.max(0, object.model.height_m - Math.min(...z)), max: Math.min(150, object.model.height_m + 100 - Math.max(...z)) };
}

/** Changes an estimate without granting outline, class or metric approval. */
export function withObjectHeight(object: SceneObject, height: number): SceneObject {
  const range = objectHeightRange(object);
  if (!Number.isFinite(height) || height < range.min || height > range.max || height === object.model.height_m) return object;
  const next = { ...object, model: { ...object.model, height_m: height, height_source: "reviewer-estimate", height_verified: false } };
  const graph = sceneRoofGraph(object);
  if (graph) {
    const delta = height - object.model.height_m;
    // The range check preserves shape; this only absorbs floating-point noise
    // at exactly 0 or 100 m so the saved annotation remains valid.
    next.roof_annotation = { ...graph, vertices: graph.vertices.map(p => [p[0], p[1], Math.max(0, Math.min(100, p[2] + delta))]) };
  }
  return next;
}

/** Roof feedback is separate from the visible outline and class approval. */
export function withObjectRoofAnnotation(object: SceneObject, annotation: RoofAnnotation): SceneObject {
  if (!hasConventionalRoof(object) || object.roof_annotation === annotation) return object;
  return { ...object, roof_annotation: annotation, model: { ...object.model, height_verified: false } };
}

export function withObjectRoofHeight(object: SceneObject, index: number, height: number): SceneObject {
  const graph = sceneRoofGraph(object);
  if (!graph) return object;
  const next = withRoofPointHeight(graph, index, height);
  return next === graph ? object : withObjectRoofAnnotation(object, next);
}

/** A fixed vertical plane keeps screen drags in metres at any camera bearing. */
export function heightDragPlane(camera: THREE.PerspectiveCamera, anchor: THREE.Vector3) {
  const normal = camera.position.clone().sub(anchor).setZ(0);
  // Directly overhead, vertical motion has no useful image direction.
  if (normal.length() < .01) return null;
  return new THREE.Plane().setFromNormalAndCoplanarPoint(normal.normalize(), anchor);
}

export function heightOnPlane(camera: THREE.PerspectiveCamera, point: THREE.Vector2, plane: THREE.Plane) {
  const ray = new THREE.Raycaster();
  ray.setFromCamera(point, camera);
  const hit = ray.ray.intersectPlane(plane, new THREE.Vector3());
  return hit && Number.isFinite(hit.z) ? hit.z : null;
}
