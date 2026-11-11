"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import {
  transformPoint,
  type SceneModelSnapshot,
  type SceneObject,
} from "@/lib/scene-types";
import { populateScene, populateScenePrediction, setSceneIsolation } from "@/lib/scene-geometry";
import { sceneVisibility } from "@/lib/scene-visibility";
import { annotationIssues, roofHeightGroup } from "@/lib/roof-annotations";
import { sceneRoofGraph, withObjectRoofHeight } from "@/lib/scene-height";
import MetricInput from "../reviews/MetricInput";
import SceneHeightControl, { type SceneViewport } from "./SceneHeightControl";
function dispose(group: THREE.Object3D) {
  group.traverse((o) => {
    const mesh = o as THREE.Mesh;
    mesh.geometry?.dispose();
    if (mesh.material)
      (Array.isArray(mesh.material) ? mesh.material : [mesh.material]).forEach(
        (m) => m.dispose(),
      );
  });
}
export default function Scene3D({
  scene,
  objects,
  predictions,
  selected,
  visible,
  objectIds,
  onSelect,
  groupIds,
  onChange,
  onEditingChange,
  disabled = false,
}: {
  scene: SceneModelSnapshot;
  objects: SceneObject[];
  predictions?: SceneObject[];
  selected: string | null;
  visible: Set<string>;
  objectIds?: ReadonlySet<string>;
  onSelect: (id: string) => void;
  groupIds?: string[];
  onChange?: (object: SceneObject) => void;
  onEditingChange?: (editing: boolean) => void;
  disabled?: boolean;
}) {
  const host = useRef<HTMLDivElement>(null),
    group = useRef<THREE.Group | null>(null),
    predictionGroup = useRef<THREE.Group | null>(null),
    navigation = useRef<SceneViewport | null>(null),
    select = useRef(onSelect);
  const [error, setError] = useState(false);
  const [focused, setFocused] = useState(Boolean(groupIds || (objectIds && selected)));
  const [isolated, setIsolated] = useState(Boolean(groupIds));
  const [heightPreview, setHeightPreview] = useState<SceneObject | null>(null);
  const [heightEditing, setHeightEditing] = useState(false);
  const [pointTarget, setPointTarget] = useState<{ objectId: string; index: number } | null>(null);
  const [viewportData, setViewportData] = useState<{ scene: SceneModelSnapshot; viewport: SceneViewport } | null>(null);
  const selectedObject = objects.find(o => o.id === selected);
  const graph = selectedObject ? sceneRoofGraph(selectedObject) : null;
  const roofPoint = pointTarget?.objectId === selected && graph?.vertices[pointTarget.index] ? pointTarget.index : undefined;
  const connectedPoints = graph && roofPoint !== undefined ? roofHeightGroup(graph, roofPoint) : [];
  const previewObject = heightPreview?.id === selected ? heightPreview : selectedObject;
  const previewGraph = previewObject ? sceneRoofGraph(previewObject) : null;
  const roofIssues = roofPoint !== undefined && previewGraph && selectedObject ? annotationIssues(previewGraph,
    selectedObject.ring.map(p => transformPoint(p, scene.pixel_to_meters)),
    selectedObject.holes?.map(h => h.map(p => transformPoint(p, scene.pixel_to_meters)))) : [];
  const renderedObjects = heightPreview ? objects.map(o => o.id === heightPreview.id ? heightPreview : o) : objects;
  const groupKey=groupIds?.join("\0");
  const focusGroup=useMemo(()=>groupKey===undefined?undefined:new Set(groupKey.split("\0").filter(Boolean)),[groupKey]);
  const inScope = (o: SceneObject) => (focusGroup?focusGroup.has(o.id):o.id === selected) && (!objectIds || objectIds.has(o.id)) && !["rejected","superseded"].includes(o.status) && visible.has(o.kind);
  const hasEditedSelection = objects.some(inScope);
  const hasSelection = hasEditedSelection || Boolean(predictions?.some(inScope));
  const fitView = useCallback((objectOnly: boolean) => {
    const n = navigation.current, root = group.current;
    if (!n || !root) return;
    const box = new THREE.Box3();
    if (objectOnly) {
      for (const layer of [root, predictionGroup.current])
        for (const mesh of layer?.children || []) if (mesh.visible && (focusGroup?focusGroup.has(mesh.userData.objectId):mesh.userData.objectId === selected)) box.expandByObject(mesh);
    } else {
      for (const pixel of [[0,0],[scene.image_width,0],[scene.image_width,scene.image_height],[0,scene.image_height]]) {
        const [x,y] = transformPoint(pixel,scene.pixel_to_meters);
        box.expandByPoint(new THREE.Vector3(x,y,0));
      }
    }
    if (box.isEmpty()) return;
    const sphere=box.getBoundingSphere(new THREE.Sphere());
    const halfVertical=THREE.MathUtils.degToRad(n.camera.fov/2);
    const halfFov=Math.min(halfVertical,Math.atan(Math.tan(halfVertical)*n.camera.aspect));
    const distance=Math.max(2,sphere.radius/Math.sin(halfFov)*1.15);
    const direction=n.camera.position.clone().sub(n.controls.target).normalize();
    n.controls.target.copy(sphere.center);
    n.camera.position.copy(sphere.center).addScaledVector(direction,distance);
    n.controls.minDistance=.5;
    n.controls.maxDistance=Math.max(n.controls.maxDistance,distance*4);
    n.controls.update();
  },[scene,selected,focusGroup]);
  useEffect(() => {
    select.current = onSelect;
  }, [onSelect]);
  useEffect(() => {
    const node = host.current;
    if (!node) return;
    const world = new THREE.Scene();
    world.background = new THREE.Color("#111b2b");
    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true });
    } catch {
      setError(true);
      return;
    }
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    node.appendChild(renderer.domElement);
    const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 2000);
    camera.up.set(0, 0, 1);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    const corners = [[0,0],[scene.image_width,0],[scene.image_width,scene.image_height],[0,scene.image_height]]
      .map(p=>transformPoint(p,scene.pixel_to_meters));
    const xs=corners.map(p=>p[0]),ys=corners.map(p=>p[1]);
    const span=Math.max(Math.max(...xs)-Math.min(...xs),Math.max(...ys)-Math.min(...ys)),
      cx=(Math.max(...xs)+Math.min(...xs))/2,cy=(Math.max(...ys)+Math.min(...ys))/2;
    camera.position.set(cx + span * 0.7, cy - span * 0.8, span * 0.9);
    controls.target.set(cx, cy, 0);
    controls.minDistance = 5;
    controls.maxDistance = span * 4;
    // This scene is Z-up. A Y-up sky fill counterlights opposite roof slopes
    // and can make a pitched roof read as a flat surface in the review card.
    const sky = new THREE.HemisphereLight(0xeef6ff, 0x6c7763, 1.1);
    sky.position.set(0, 0, 1);
    world.add(sky);
    const sun = new THREE.DirectionalLight(0xfff0d8, 2.5);
    sun.position.set(-60, -20, 80);
    world.add(sun);
    const grid = new THREE.GridHelper(span, 20, 0x44546a, 0x25364a);
    grid.rotation.x = Math.PI / 2;
    grid.position.set(cx, cy, -0.1);
    world.add(grid);
    const objectsGroup = new THREE.Group();
    world.add(objectsGroup);
    group.current = objectsGroup;
    const originalGroup = new THREE.Group();
    world.add(originalGroup);
    predictionGroup.current = originalGroup;
    let heightFrame: (() => void) | undefined;
    navigation.current = { camera, controls, root: objectsGroup, host: node,
      onHeightFrame: draw => { heightFrame = draw; return () => { if (heightFrame === draw) heightFrame = undefined; }; },
      setOrbitEnabled: enabled => { controls.enabled = enabled; },
    };
    setViewportData({ scene, viewport: navigation.current });
    const resize = () => {
      renderer.setSize(node.clientWidth || 500, node.clientHeight || 500);
      camera.aspect = (node.clientWidth || 500) / (node.clientHeight || 500);
      camera.updateProjectionMatrix();
    };
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(node);
    const ray = new THREE.Raycaster();
    let down = { x: 0, y: 0 };
    const pointerDown = (e: PointerEvent) => {
      down = { x: e.clientX, y: e.clientY };
    };
    const pick = (e: PointerEvent) => {
      if (!controls.enabled) return;
      if (Math.hypot(e.clientX - down.x, e.clientY - down.y) > 5) return;
      const rect = node.getBoundingClientRect();
      ray.setFromCamera(
        new THREE.Vector2(
          ((e.clientX - rect.left) / rect.width) * 2 - 1,
          (-(e.clientY - rect.top) / rect.height) * 2 + 1,
        ),
        camera,
      );
      const hit = ray
        .intersectObjects(objectsGroup.children, true)
        .find((h) => h.object.visible && h.object.userData.objectId);
      if (hit) select.current(hit.object.userData.objectId);
    };
    node.addEventListener("pointerdown", pointerDown);
    node.addEventListener("pointerup", pick);
    let frame = 0;
    const render = () => {
      frame = requestAnimationFrame(render);
      if (controls.enabled) controls.update();
      renderer.render(world, camera);
      heightFrame?.();
    };
    render();
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      node.removeEventListener("pointerdown", pointerDown);
      node.removeEventListener("pointerup", pick);
      controls.dispose();
      dispose(world);
      renderer.dispose();
      renderer.domElement.remove();
      group.current = null;
      predictionGroup.current = null;
      navigation.current = null;
    };
  }, [scene]);
  useEffect(() => {
    const root = group.current;
    if (!root) return;
    dispose(root);
    root.clear();
    populateScene(root, renderedObjects, scene, selected, visible);
  }, [scene, renderedObjects, selected, visible]);
  useEffect(() => {
    const root = predictionGroup.current;
    if (!root) return;
    dispose(root); root.clear();
    if (predictions) populateScenePrediction(root, predictions, scene, visible);
  }, [scene, predictions, visible]);
  useEffect(() => {
    const root=group.current;
    if(!root)return;
    setSceneIsolation(root,objects,isolated && hasSelection?selected:null,isolated?focusGroup:undefined,objectIds);
    if (predictionGroup.current && predictions) setSceneIsolation(predictionGroup.current,predictions,isolated && hasSelection?selected:null,isolated?focusGroup:undefined,objectIds);
  },[scene,renderedObjects,objects,predictions,selected,visible,isolated,hasSelection,focusGroup,objectIds]);
  useEffect(() => { fitView(focused); }, [fitView, focused, hasSelection]);
  const orbit = (delta: number) => {
    const n = navigation.current;
    if (!n) return;
    const offset = n.camera.position.clone().sub(n.controls.target);
    offset.applyAxisAngle(new THREE.Vector3(0, 0, 1), delta);
    n.camera.position.copy(n.controls.target).add(offset);
    n.controls.update();
  };
  return (
    <div className="scene-3d">
      {onChange && graph?.vertices.length && selectedObject && hasEditedSelection && !sceneVisibility(selectedObject, scene).footprintOnly ?
        <fieldset className="scene-roof-height-controls" disabled={disabled || heightEditing}>
          <label>3D edit handle<select aria-label="3D edit handle" value={roofPoint === undefined ? "height" : String(roofPoint)} onChange={event => {
            setPointTarget(event.target.value === "height" ? null : { objectId: selectedObject.id, index: Number(event.target.value) });
          }}>
            <option value="height">Building height</option>
            {graph.vertices.map((p, i) => <option key={i} value={i}>Roof point {i + 1} · {p[2].toFixed(2)} m</option>)}
          </select></label>
          {roofPoint !== undefined && <label>Point height above base (m)<MetricInput aria-label="3D roof point height (m)" key={`${selectedObject.id}:${roofPoint}`}
            value={Number(graph.vertices[roofPoint][2].toFixed(2))} min="0" max="100" step=".1" onValueChange={height => {
              const next = withObjectRoofHeight(selectedObject, roofPoint, height); if (next !== selectedObject) onChange(next);
            }} /></label>}
        </fieldset> : null}
      <div className="scene-3d-viewport">
      {predictions && <div className="scene-3d-comparison" role="group" aria-label="3D comparison legend">
        <span>Your edits · solid surfaces</span><span className="scene-prediction-key">Prediction · amber X-ray lines</span>
        {selected && !predictions.some(o => o.id === selected) && <span>No original prediction for this object.</span>}
        {selectedObject && ["rejected","superseded"].includes(selectedObject.status) && <span>Original only · {selectedObject.status === "rejected" ? "rejected detection" : "replaced outline"}.</span>}
      </div>}
      <div
        ref={host}
        className="scene-3d-canvas"
        role="img"
        aria-label="Interactive 3D scene estimate"
      />
      {!error && !disabled && onChange && viewportData?.scene === scene && selectedObject && hasEditedSelection && !sceneVisibility(selectedObject, scene).footprintOnly &&
        <SceneHeightControl key={`${selectedObject.id}:${roofPoint ?? "height"}`} object={selectedObject} roofPoint={roofPoint} viewport={viewportData.viewport} onPreview={setHeightPreview} onChange={onChange}
          onEditingChange={editing => { setHeightEditing(editing); onEditingChange?.(editing); }} />}
      </div>
      {error && (
        <p role="alert">
          3D is unavailable in this browser. The aerial editor is still
          available.
        </p>
      )}
      <fieldset className="scene-inline-actions scene-camera-controls" disabled={heightEditing}>
        <button className="review-button" aria-pressed={focused} disabled={!hasSelection && !focused} onClick={()=>{
          if(focused)setIsolated(false);
          setFocused(!focused);
          host.current?.scrollIntoView({block:"center"});
        }}>
          {focused ? "Whole scene" : focusGroup?"Focus group":"Focus object"}
        </button>
        <button className="review-button" aria-pressed={isolated} disabled={!hasSelection} onClick={()=>{
          setIsolated(!isolated);
          if(!isolated)setFocused(true);
          host.current?.scrollIntoView({block:"center"});
        }}>{isolated?"Show surroundings":focusGroup?"Isolate group":"Isolate object"}</button>
        <button className="review-button" onClick={() => orbit(-0.25)}>
          Rotate left
        </button>
        <button className="review-button" onClick={() => orbit(0.25)}>
          Rotate right
        </button>
        <button
          className="review-button"
          onClick={() => {
            const n = navigation.current;
            if (n)
              n.camera.position
                .sub(n.controls.target)
                .multiplyScalar(0.8)
                .add(n.controls.target);
          }}
        >
          Zoom in
        </button>
        <button
          className="review-button"
          onClick={() => {
            const n = navigation.current;
            if (n)
              n.camera.position
                .sub(n.controls.target)
                .multiplyScalar(1.25)
                .add(n.controls.target);
          }}
        >
          Zoom out
        </button>
      </fieldset>
      <p className="review-muted">
        {onChange && !disabled && hasEditedSelection && selectedObject && !sceneVisibility(selectedObject, scene).footprintOnly ? "Drag the height handle to preview; release to keep the edit. Escape cancels. " : ""}
        {roofPoint !== undefined ? connectedPoints.length > 1 ? `Moves ${connectedPoints.length} connected ridge points together. ` : "Moves this roof point above the building base. " : ""}
        Heights and shapes remain estimates.
      </p>
      {roofIssues.length > 0 && <p className="review-notice" role="status">Roof geometry needs review: {roofIssues.join("; ")}. Adjust the points before using this shape.</p>}
    </div>
  );
}
