"use client";
import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import type { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { MoveVertical } from "lucide-react";
import { heightDragPlane, heightIsThickness, heightOnPlane, objectHeightRange, sceneRoofGraph, withObjectHeight, withObjectRoofHeight } from "@/lib/scene-height";
import { roofHeightGroup } from "@/lib/roof-annotations";
import type { SceneObject } from "@/lib/scene-types";

export type SceneViewport = {
  camera: THREE.PerspectiveCamera; controls: OrbitControls; root: THREE.Group; host: HTMLDivElement;
  onHeightFrame: (draw: () => void) => () => void;
  setOrbitEnabled: (enabled: boolean) => void;
};

export default function SceneHeightControl({ object, roofPoint, viewport, onPreview, onChange, onEditingChange }: {
  object: SceneObject; viewport: SceneViewport;
  roofPoint?: number;
  onPreview: (object: SceneObject | null) => void; onChange: (object: SceneObject) => void;
  onEditingChange?: (editing: boolean) => void;
}) {
  const handle = useRef<HTMLDivElement>(null), line = useRef<SVGLineElement>(null), tether = useRef<SVGLineElement>(null), ridge = useRef<SVGPathElement>(null);
  const gesture = useRef<{ pointer: number; plane: THREE.Plane; startZ: number; height: number; enabled: boolean } | null>(null);
  const callbacks = useRef({ onPreview, onChange, onEditingChange });
  const [previewHeight, setPreviewHeight] = useState<number | null>(null);
  const graph = sceneRoofGraph(object), vertex = roofPoint === undefined ? undefined : graph?.vertices[roofPoint];
  const thickness = heightIsThickness(object.kind), step = thickness ? .01 : .1;
  const originalHeight = vertex?.[2] ?? object.model.height_m;
  const height = previewHeight ?? originalHeight;
  const range = vertex ? { min: 0, max: 100 } : objectHeightRange(object);
  const label = vertex ? `Roof point ${roofPoint! + 1}` : thickness ? "Thickness" : "Height";
  const update = (value: number) => vertex ? withObjectRoofHeight(object, roofPoint!, value) : withObjectHeight(object, value);
  useEffect(() => { callbacks.current = { onPreview, onChange, onEditingChange }; }, [onPreview, onChange, onEditingChange]);
  useEffect(() => {
    const n = viewport;
    const box = new THREE.Box3(), top = new THREE.Vector3(), bottom = new THREE.Vector3();
    const graph = sceneRoofGraph(object), vertex = roofPoint === undefined ? undefined : graph?.vertices[roofPoint];
    const members = new Set(vertex && graph ? roofHeightGroup(graph, roofPoint!) : []);
    const ridgeEdges = graph?.edges.filter(e => e.kind === "ridge" && members.has(e.a) && members.has(e.b)) || [];
    const unsubscribe = n.onHeightFrame(() => {
      if (!handle.current || !line.current) return;
      box.makeEmpty();
      for (const mesh of n.root.children) if (mesh.visible && mesh.userData.objectId === object.id) box.expandByObject(mesh);
      if (box.isEmpty()) { handle.current.hidden = true; line.current.style.display = "none"; if (tether.current) tether.current.style.display = "none"; ridge.current?.setAttribute("d", ""); return; }
      const estimate = gesture.current?.height ?? vertex?.[2] ?? object.model.height_m;
      if (vertex) top.set(vertex[0], vertex[1], object.model.base_m + estimate);
      else { box.getCenter(top); top.z = heightIsThickness(object.kind) ? box.max.z : object.model.base_m + estimate; }
      bottom.copy(top).setZ(top.z - estimate);
      top.project(n.camera); bottom.project(n.camera);
      const width = n.host.clientWidth, height = n.host.clientHeight;
      const x = (top.x + 1) * width / 2, y = (1 - top.y) * height / 2;
      // Keep the focused control inside the viewport as the roof rises. Hiding
      // it during a drag drops keyboard focus, so Escape would stop cancelling.
      const shown = Number.isFinite(x) && Number.isFinite(y) && (Boolean(gesture.current) || top.z > -1 && top.z < 1);
      const handleX = Math.max(22, Math.min(width - 22, x)), handleY = Math.max(22, Math.min(height - 22, y));
      handle.current.hidden = !shown;
      handle.current.style.left = `${handleX}px`; handle.current.style.top = `${handleY}px`;
      line.current.style.display = shown ? "" : "none";
      line.current.setAttribute("x1", String((bottom.x + 1) * width / 2)); line.current.setAttribute("y1", String((1 - bottom.y) * height / 2));
      line.current.setAttribute("x2", String(x)); line.current.setAttribute("y2", String(y));
      if (tether.current) {
        tether.current.style.display = shown && (x !== handleX || y !== handleY) ? "" : "none";
        for (const [key, value] of Object.entries({ x1: x, y1: y, x2: handleX, y2: handleY })) tether.current.setAttribute(key, String(value));
      }
      if (ridge.current) {
        const project = (i: number) => {
          const p = graph!.vertices[i], z = gesture.current ? estimate : p[2];
          const v = new THREE.Vector3(p[0], p[1], object.model.base_m + z).project(n.camera);
          return { x: (v.x + 1) * width / 2, y: (1 - v.y) * height / 2, shown: v.z > -1 && v.z < 1 };
        };
        ridge.current.setAttribute("d", ridgeEdges.map(e => {
          const a = project(e.a), b = project(e.b);
          return a.shown && b.shown ? `M ${a.x} ${a.y} L ${b.x} ${b.y}` : "";
        }).join(" "));
      }
    });
    return () => {
      unsubscribe();
      if (gesture.current) {
        n.setOrbitEnabled(gesture.current.enabled); gesture.current = null;
        callbacks.current.onPreview(null); callbacks.current.onEditingChange?.(false);
      }
    };
  }, [object, roofPoint, viewport]);

  const point = (x: number, y: number) => {
    const rect = viewport.host.getBoundingClientRect();
    return new THREE.Vector2((x - rect.left) / rect.width * 2 - 1, 1 - (y - rect.top) / rect.height * 2);
  };
  const finish = (commit: boolean) => {
    const active = gesture.current;
    if (!active) return;
    gesture.current = null;
    viewport.setOrbitEnabled(active.enabled);
    setPreviewHeight(null); callbacks.current.onPreview(null); callbacks.current.onEditingChange?.(false);
    if (handle.current?.hasPointerCapture(active.pointer)) handle.current.releasePointerCapture(active.pointer);
    if (commit && active.height !== originalHeight) callbacks.current.onChange(update(active.height));
  };
  return <div className="scene-height-overlay">
    <svg aria-hidden="true"><path ref={ridge} className="scene-roof-ridge-highlight" /><line ref={line} className="scene-height-line" /><line ref={tether} className="scene-height-line" /></svg>
    <div ref={handle} tabIndex={0} className="scene-height-handle" role="slider"
      aria-label={`3D ${label.toLowerCase()}${vertex ? " height" : ""} handle`} aria-orientation="vertical" aria-valuemin={range.min} aria-valuemax={range.max}
      aria-valuenow={height} aria-valuetext={`${height.toFixed(2)} metres, estimated ${label.toLowerCase()}`}
      title={`Drag to adjust ${label.toLowerCase()}. Arrow keys: ${step} m; Shift: ${step * 10} m. Escape cancels a drag.`}
      onPointerDown={e => {
        if (!e.isPrimary || e.button !== 0 || gesture.current) return;
        e.preventDefault(); e.stopPropagation();
        const n = viewport, box = new THREE.Box3();
        for (const mesh of n.root.children) if (mesh.visible && mesh.userData.objectId === object.id) box.expandByObject(mesh);
        if (box.isEmpty()) return;
        const anchor = vertex ? new THREE.Vector3(vertex[0], vertex[1], object.model.base_m + originalHeight) : box.getCenter(new THREE.Vector3()).setZ(box.max.z);
        const plane = heightDragPlane(n.camera, anchor);
        if (!plane) return;
        const startZ = heightOnPlane(n.camera, point(e.clientX, e.clientY), plane);
        if (startZ === null) return;
        gesture.current = { pointer: e.pointerId, plane, startZ, height: originalHeight, enabled: n.controls.enabled };
        n.setOrbitEnabled(false); e.currentTarget.focus({ preventScroll: true }); e.currentTarget.setPointerCapture(e.pointerId);
        callbacks.current.onEditingChange?.(true);
      }}
      onPointerMove={e => {
        const active = gesture.current, n = viewport;
        if (!active || !n || active.pointer !== e.pointerId) return;
        e.preventDefault(); e.stopPropagation();
        const z = heightOnPlane(n.camera, point(e.clientX, e.clientY), active.plane);
        if (z === null) return;
        const next = Math.max(range.min, Math.min(range.max, Math.round((originalHeight + z - active.startZ) * 100) / 100));
        if (next === active.height) return;
        active.height = next; setPreviewHeight(next); callbacks.current.onPreview(update(next));
      }}
      onPointerUp={e => { if (gesture.current?.pointer === e.pointerId) { e.preventDefault(); e.stopPropagation(); finish(true); } }}
      onPointerCancel={e => { if (gesture.current?.pointer === e.pointerId) finish(false); }}
      onLostPointerCapture={e => { if (gesture.current?.pointer === e.pointerId) finish(false); }}
      onKeyDown={e => {
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(false); return; }
        if (!["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
        e.preventDefault(); e.stopPropagation();
        if (gesture.current || e.ctrlKey || e.metaKey || e.altKey) return;
        const delta = step * (e.shiftKey ? 10 : 1) * (["ArrowUp", "ArrowRight"].includes(e.key) ? 1 : -1);
        const next = e.key === "Home" ? range.min : e.key === "End" ? range.max : Math.max(range.min, Math.min(range.max, Math.round((height + delta) * 100) / 100));
        if (next !== height) callbacks.current.onChange(update(next));
      }}>
      <MoveVertical size={18} aria-hidden="true" />
      <span aria-hidden="true">{label} {height.toFixed(thickness ? 2 : 1)} m</span>
    </div>
  </div>;
}
