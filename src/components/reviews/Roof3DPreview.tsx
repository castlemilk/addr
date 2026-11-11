"use client";

import React, { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import {
  buildRooflineMesh,
  buildWallStrip,
  RooflinePart,
  RoofRing,
  roofColorFor,
} from "@/lib/roof-geometry";
import { RotateCcw, RotateCw, Plus, Minus, Box } from "lucide-react";
import { RooflineItem } from "./RoofReviewCanvas";

export interface Roof3DPart {
  ring?: RoofRing;
  ring_meters?: RoofRing;
  z?: number[];
  ring_z?: number[];
  kind?: string;
  slope_deg?: number;
}

interface Roof3DPreviewProps {
  parts: (RooflinePart | Roof3DPart)[];
  ringMeters: RoofRing;
  roofMaterial?: string | null;
  roofType?: string;
  eaveHeight?: number;
  ridgeHeight?: number;
  lines?: RooflineItem[];
  propertyBoundaryMeters?: [number, number][] | null;
}

export default function Roof3DPreview({
  parts,
  ringMeters,
  roofMaterial,
  eaveHeight = 3.2,
  lines,
  propertyBoundaryMeters,
}: Roof3DPreviewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [showRoof, setShowRoof] = useState(true);
  const [showWalls, setShowWalls] = useState(true);
  const [showRidges, setShowRidges] = useState(true);
  const [showHips, setShowHips] = useState(true);
  const [showValleys, setShowValleys] = useState(true);
  const [showBoundary, setShowBoundary] = useState(true);
  const [wireframe, setWireframe] = useState(false);
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null);
  const sceneRef = useRef<THREE.Scene | null>(null);
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null);
  const controlsRef = useRef<OrbitControls | null>(null);
  const [renderError, setRenderError] = useState(false);
  const frameRef = useRef({ center: new THREE.Vector3(0, 0, 3), distance: 40 });
  const meshGroupRef = useRef<THREE.Group | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;

    const width = containerRef.current.clientWidth || 400;
    const height = containerRef.current.clientHeight || 340;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0f172a);
    sceneRef.current = scene;

    const camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 1000);
    camera.position.set(24, -28, 22);
    camera.up.set(0, 0, 1);
    cameraRef.current = camera;

    let renderer: THREE.WebGLRenderer;
    try { renderer = new THREE.WebGLRenderer({ antialias: true }); }
    catch { setRenderError(true); return; }
    renderer.setSize(width, height);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.shadowMap.enabled = true;
    containerRef.current.appendChild(renderer.domElement);
    rendererRef.current = renderer;

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.target.set(0, 0, 3);
    controlsRef.current = controls;

    // Lighting
    const ambientLight = new THREE.AmbientLight(0xffffff, 0.7);
    scene.add(ambientLight);

    const dirLight = new THREE.DirectionalLight(0xfffaed, 1.4);
    dirLight.position.set(20, -30, 40);
    dirLight.castShadow = true;
    scene.add(dirLight);

    const fillLight = new THREE.DirectionalLight(0x93c5fd, 0.5);
    fillLight.position.set(-20, 20, 10);
    scene.add(fillLight);

    // Ground plane grid
    const grid = new THREE.GridHelper(50, 50, 0x334155, 0x1e293b);
    grid.rotation.x = Math.PI / 2;
    scene.add(grid);

    const group = new THREE.Group();
    scene.add(group);
    meshGroupRef.current = group;

    let animId: number;
    const animate = () => {
      animId = requestAnimationFrame(animate);
      controls.update();
      renderer.render(scene, camera);
    };
    animate();

    const handleResize = () => {
      if (!containerRef.current || !rendererRef.current || !cameraRef.current) return;
      const w = Math.max(1, containerRef.current.clientWidth);
      const h = Math.max(1, containerRef.current.clientHeight);
      cameraRef.current.aspect = w / h;
      cameraRef.current.updateProjectionMatrix();
      rendererRef.current.setSize(w, h);
    };
    const observer = new ResizeObserver(handleResize);
    observer.observe(containerRef.current);

    const currentContainer = containerRef.current;

    return () => {
      cancelAnimationFrame(animId);
      observer.disconnect();
      controls.dispose();
      scene.traverse(object => {
        const mesh = object as THREE.Mesh;
        mesh.geometry?.dispose();
        if (mesh.material) (Array.isArray(mesh.material) ? mesh.material : [mesh.material]).forEach(material => material.dispose());
      });
      renderer.dispose();
      if (currentContainer) {
        currentContainer.innerHTML = "";
      }
    };
  }, []);

  // Rebuild mesh when parts or options change
  useEffect(() => {
    if (!meshGroupRef.current) return;
    const group = meshGroupRef.current;
    group.traverse(object => {
      const mesh = object as THREE.Mesh;
      mesh.geometry?.dispose();
      if (mesh.material) (Array.isArray(mesh.material) ? mesh.material : [mesh.material]).forEach(material => material.dispose());
    });
    group.clear();

    const normalizedParts: RooflinePart[] = (parts || []).map((p) => ({
      ring: (p.ring || (p as Roof3DPart).ring_meters || []) as RoofRing,
      z: (p.z || (p as Roof3DPart).ring_z || []) as number[],
      kind: p.kind,
      slope_deg: p.slope_deg,
    }));
    const roofPositions = normalizedParts.length > 0 ? buildRooflineMesh(normalizedParts) : [];
    const baseColor = roofColorFor(roofMaterial, 0x94a3b8);

    // 1. Roof Surface Mesh
    if (showRoof && roofPositions.length > 0) {
      const roofGeom = new THREE.BufferGeometry();
      roofGeom.setAttribute("position", new THREE.Float32BufferAttribute(roofPositions, 3));
      roofGeom.computeVertexNormals();

      const roofMat = new THREE.MeshStandardMaterial({
        color: baseColor,
        roughness: 0.5,
        metalness: 0.1,
        wireframe: wireframe,
        side: THREE.DoubleSide,
      });

      const roofMesh = new THREE.Mesh(roofGeom, roofMat);
      group.add(roofMesh);
    }

    // 2. Exterior Walls
    if (showWalls && ringMeters && ringMeters.length >= 3) {
      const wallPositions = buildWallStrip(ringMeters, 0, eaveHeight, normalizedParts);
      const wallGeom = new THREE.BufferGeometry();
      wallGeom.setAttribute("position", new THREE.Float32BufferAttribute(wallPositions, 3));
      wallGeom.computeVertexNormals();

      const wallMat = new THREE.MeshStandardMaterial({
        color: 0xe2e8f0,
        roughness: 0.8,
        metalness: 0.05,
        wireframe: wireframe,
      });

      const wallMesh = new THREE.Mesh(wallGeom, wallMat);
      group.add(wallMesh);
    }

    // 3. 3D Classified Rooflines (Ridges, Hips, Valleys)
    if (lines && lines.length > 0) {
      const lineKindColors: Record<string, number> = {
        ridge: 0x22c55e,
        hip: 0x06b6d4,
        valley: 0xa855f7,
        step_eave: 0xf59e0b,
      };

      const linePointsByColor: Record<number, number[]> = {};

      for (const line of lines) {
        const kind = line.kind || "ridge";
        if (kind === "ridge" && !showRidges) continue;
        if (kind === "hip" && !showHips) continue;
        if (kind === "valley" && !showValleys) continue;

        const hex = lineKindColors[kind] || 0x22c55e;
        const coords = line.line_meters;
        if (!coords || coords.length < 2) continue;
        const zCoords = line.line_z || [eaveHeight, eaveHeight];

        if (!linePointsByColor[hex]) linePointsByColor[hex] = [];
        // Slight z-offset (+0.04m) to elevate cleanly above roof facet triangles
        linePointsByColor[hex].push(
          coords[0][0], coords[0][1], zCoords[0] + 0.04,
          coords[1][0], coords[1][1], zCoords[1] + 0.04
        );
      }

      for (const [hexStr, pts] of Object.entries(linePointsByColor)) {
        if (pts.length === 0) continue;
        const lineGeo = new THREE.BufferGeometry();
        lineGeo.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
        const lineMat = new THREE.LineBasicMaterial({
          color: Number(hexStr),
          linewidth: 3,
        });
        group.add(new THREE.LineSegments(lineGeo, lineMat));
      }
    }

    // 4. Property Boundary (Cadastral Parcel on Ground)
    if (showBoundary && propertyBoundaryMeters && propertyBoundaryMeters.length >= 3) {
      const boundaryPts: THREE.Vector3[] = [];
      for (const [bx, by] of propertyBoundaryMeters) {
        boundaryPts.push(new THREE.Vector3(bx, by, 0.03));
      }
      boundaryPts.push(boundaryPts[0].clone()); // Close loop
      const boundaryGeo = new THREE.BufferGeometry().setFromPoints(boundaryPts);
      const boundaryMat = new THREE.LineBasicMaterial({
        color: 0xf59e0b,
        linewidth: 2,
      });
      group.add(new THREE.Line(boundaryGeo, boundaryMat));
    }
  }, [
    parts,
    ringMeters,
    roofMaterial,
    eaveHeight,
    wireframe,
    lines,
    propertyBoundaryMeters,
    showRoof,
    showWalls,
    showRidges,
    showHips,
    showValleys,
    showBoundary,
  ]);

  useEffect(() => {
    if (!cameraRef.current || !controlsRef.current || !ringMeters.length) return;
    const bounds = new THREE.Box3().setFromPoints(ringMeters.map(([x, y]) => new THREE.Vector3(x, y, eaveHeight)));
    const center = bounds.getCenter(new THREE.Vector3());
    const extent = bounds.getSize(new THREE.Vector3());
    const distance = Math.max(18, Math.max(extent.x, extent.y) * 1.6);
    frameRef.current = { center, distance };
    cameraRef.current.position.copy(center).add(new THREE.Vector3(.65, -.8, .8).multiplyScalar(distance));
    controlsRef.current.target.copy(center);
    controlsRef.current.update();
  }, [ringMeters, eaveHeight]);

  const handleResetCamera = () => {
    if (!cameraRef.current || !controlsRef.current) return;
    const { center, distance } = frameRef.current;
    cameraRef.current.position.copy(center).add(new THREE.Vector3(.65, -.8, .8).multiplyScalar(distance));
    controlsRef.current.target.copy(center);
    controlsRef.current.update();
  };
  const adjustCamera = (angle: number, scale = 1) => {
    if (!cameraRef.current || !controlsRef.current) return;
    const target = controlsRef.current.target;
    cameraRef.current.position.sub(target).applyAxisAngle(new THREE.Vector3(0, 0, 1), angle).multiplyScalar(scale).add(target);
    controlsRef.current.update();
  };

  return (
    <div className="relative w-full h-full min-h-[300px] bg-slate-950 rounded-lg overflow-hidden border border-slate-800 flex flex-col">
      <div ref={containerRef} className="w-full flex-1 min-h-0" aria-label="Interactive estimated roof model" />
      {(renderError || !parts.length) && <p role="status" className="absolute inset-x-6 top-20 text-center text-sm text-slate-300">{renderError ? "3D preview unavailable on this device. Use aerial evidence to review the boundary." : "No roof planes have been extracted for this address yet."}</p>}
      <div className="absolute bottom-3 left-3 flex gap-1 bg-slate-900 rounded p-1">
        <button className="p-2 hover:bg-slate-700 rounded" aria-label="Rotate model left" onClick={() => adjustCamera(Math.PI / 8)}><RotateCcw size={14} /></button>
        <button className="p-2 hover:bg-slate-700 rounded" aria-label="Rotate model right" onClick={() => adjustCamera(-Math.PI / 8)}><RotateCw size={14} /></button>
        <button className="p-2 hover:bg-slate-700 rounded" aria-label="Zoom model in" onClick={() => adjustCamera(0, .8)}><Plus size={14} /></button>
        <button className="p-2 hover:bg-slate-700 rounded" aria-label="Zoom model out" onClick={() => adjustCamera(0, 1.25)}><Minus size={14} /></button>
      </div>

      {/* Floating 3D Layer Controls HUD */}
      <div className="absolute top-2.5 left-2.5 right-2.5 flex items-center justify-between pointer-events-none z-10">
        <div className="flex items-center space-x-1 bg-slate-900/90 backdrop-blur-md px-2 py-1 rounded-lg border border-slate-700/60 shadow-lg text-[11px] pointer-events-auto overflow-x-auto">
          <span className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mr-1">3D Layers:</span>
          
          <button
            onClick={() => setShowRoof((v) => !v)}
            className={`px-1.5 py-0.5 rounded transition-colors ${
              showRoof ? "bg-amber-500/20 text-amber-300 border border-amber-500/40" : "text-slate-500 hover:text-slate-300"
            }`}
            title="Toggle Roof Mesh"
          >
            Roof
          </button>

          <button
            onClick={() => setShowWalls((v) => !v)}
            className={`px-1.5 py-0.5 rounded transition-colors ${
              showWalls ? "bg-slate-700/60 text-slate-200 border border-slate-600" : "text-slate-500 hover:text-slate-300"
            }`}
            title="Toggle Walls"
          >
            Walls
          </button>

          <button
            onClick={() => setShowRidges((v) => !v)}
            className={`px-1.5 py-0.5 rounded transition-colors flex items-center space-x-1 ${
              showRidges ? "bg-emerald-500/20 text-emerald-300 border border-emerald-500/40" : "text-slate-500 hover:text-slate-300"
            }`}
            title="Toggle Ridges"
          >
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 inline-block" />
            <span>Ridges</span>
          </button>

          <button
            onClick={() => setShowHips((v) => !v)}
            className={`px-1.5 py-0.5 rounded transition-colors flex items-center space-x-1 ${
              showHips ? "bg-cyan-500/20 text-cyan-300 border border-cyan-500/40" : "text-slate-500 hover:text-slate-300"
            }`}
            title="Toggle Hips"
          >
            <span className="w-1.5 h-1.5 rounded-full bg-cyan-400 inline-block" />
            <span>Hips</span>
          </button>

          <button
            onClick={() => setShowValleys((v) => !v)}
            className={`px-1.5 py-0.5 rounded transition-colors flex items-center space-x-1 ${
              showValleys ? "bg-purple-500/20 text-purple-300 border border-purple-500/40" : "text-slate-500 hover:text-slate-300"
            }`}
            title="Toggle Valleys"
          >
            <span className="w-1.5 h-1.5 rounded-full bg-purple-400 inline-block" />
            <span>Valleys</span>
          </button>

          <button
            disabled={!propertyBoundaryMeters?.length}
            onClick={() => setShowBoundary((v) => !v)}
            className={`px-1.5 py-0.5 rounded transition-colors flex items-center space-x-1 ${
              showBoundary ? "bg-amber-500/20 text-amber-300 border border-amber-500/40" : "text-slate-500 hover:text-slate-300"
            }`}
            title="Toggle Cadastral Boundary"
          >
            <span className="w-1.5 h-1.5 rounded-full bg-amber-400 inline-block" />
            <span>Boundary</span>
          </button>
        </div>

        <div className="flex items-center space-x-1.5 bg-slate-900/90 backdrop-blur-md px-2 py-1 rounded-lg border border-slate-700/60 shadow-lg text-[11px] pointer-events-auto">
          <button
            onClick={() => setWireframe((prev) => !prev)}
            className={`flex items-center space-x-1 px-1.5 py-0.5 rounded transition-colors ${
              wireframe ? "bg-cyan-500/20 text-cyan-400 border border-cyan-500/40" : "text-slate-400 hover:text-white"
            }`}
            title="Toggle Wireframe"
          >
            <Box className="w-3 h-3" />
            <span>{wireframe ? "Solid" : "Wire"}</span>
          </button>
          <button
            onClick={handleResetCamera}
            className="flex items-center space-x-1 px-1.5 py-0.5 rounded text-slate-400 hover:text-white transition-colors"
            title="Reset View"
          >
            <RotateCcw className="w-3 h-3" />
            <span>Reset</span>
          </button>
        </div>
      </div>
    </div>
  );
}
