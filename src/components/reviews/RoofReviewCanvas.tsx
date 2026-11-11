"use client";

import React, { useState } from "react";
import {
  Eye,
  ZoomIn,
  ZoomOut,
  Maximize2,
  Trash2,
  AlertTriangle,
} from "lucide-react";

export interface RooflineItem {
  kind?: string;
  line_meters: [[number, number], [number, number]];
  line_pixels?: [[number, number], [number, number]];
  line_z?: [number, number];
  length_2d_m?: number;
  length_3d_m?: number;
  slope_deg?: number;
}

export interface RoofPartItem {
  name?: string;
  ring_meters: [number, number][];
  ring_pixels?: [number, number][];
  ring?: [number, number][];
  ring_z?: number[];
  z?: number[];
  area_m2: number;
  area_3d_m2?: number;
  kind?: string;
  slope_deg?: number;
  pitch_deg?: number;
  pitch_ratio?: string;
  azimuth_deg?: number;
  z_range?: [number, number];
}

export interface AccuracySignals {
  iou: number;
  purity: number;
  coverage: number;
  edge_score: number;
  is_concave: boolean;
  notch_angles_deg: number[];
  solidity: number;
  topology_match: boolean;
  area_m2: number;
  quality_grade: string;
  quality_score: number;
  straightness?: number;
  orthogonality?: number;
  wonky_lines?: number;
  warnings: string[];
}

export interface RoofReflection {
  dominant_angle_deg: number;
  straightness_score: number;
  orthogonality_score: number;
  parallelism_score: number;
  uncaptured_roof_m2: number;
  false_intrusion_m2: number;
  wonky_edges_count: number;
  critique_items: string[];
  reflection_verdict: string;
  recommendations: string[];
}

interface RoofReviewCanvasProps {
  siteImageUrl?: string;
  maskImageUrl?: string;
  signals?: AccuracySignals | null;
  reflection?: RoofReflection | null;
  ringMeters: [number, number][];
  ringPixels?: [number, number][];
  propertyBoundaryMeters?: [number, number][] | null;
  nearbyPins?: Array<{ label: string; meters?: [number, number] }>;
  treeCanopiesMeters?: [number, number][][];
  neighbourBuildingsMeters?: [number, number][][];
  parts: RoofPartItem[];
  lines: RooflineItem[];
  rejectedCuts: [number, number][][];
  onRejectCut: (cutCoords: [[number, number], [number, number]]) => void;
  onRestoreCuts: () => void;
}

const LINE_COLORS: Record<string, string> = {
  ridge: "#22c55e",     // green
  hip: "#06b6d4",       // cyan
  step_eave: "#f59e0b", // amber
  valley: "#a855f7",    // purple
  eave: "#ef4444",      // red
};

function azimuthToCardinal(deg: number): string {
  const directions = [
    "N", "NNE", "NE", "ENE",
    "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW",
    "W", "WNW", "NW", "NNW",
  ];
  const index = Math.round((((deg % 360) + 360) % 360) / 22.5) % 16;
  return directions[index];
}

export default function RoofReviewCanvas({
  siteImageUrl,
  maskImageUrl,
  signals,
  reflection,
  ringMeters,
  ringPixels,
  propertyBoundaryMeters,
  nearbyPins,
  treeCanopiesMeters,
  neighbourBuildingsMeters,
  parts,
  lines,
  rejectedCuts,
  onRejectCut,
  onRestoreCuts,
}: RoofReviewCanvasProps) {
  // Five-layer toggles
  const [showPropertyBoundary, setShowPropertyBoundary] = useState(true);
  const [showRoofFootprint, setShowRoofFootprint] = useState(true);
  const [showConfidenceHUD, setShowConfidenceHUD] = useState(false);
  const [imageError, setImageError] = useState(false);
  const [showOtherLayers, setShowOtherLayers] = useState(true);
  const [showFaces, setShowFaces] = useState(false);
  const [showLines, setShowLines] = useState(true);
  const [showImagery, setShowImagery] = useState(true);
  const [showMask, setShowMask] = useState(false);

  // Granular modelling layer toggles
  const [showFacetMeshes, setShowFacetMeshes] = useState(true);
  const [showRidges, setShowRidges] = useState(true);
  const [showHips, setShowHips] = useState(true);
  const [showValleys, setShowValleys] = useState(true);
  const showStepEaves = true;
  const [showFacetLabels, setShowFacetLabels] = useState(false);
  const [wireframeFacets, setWireframeFacets] = useState(false);

  const [zoomLevel, setZoomLevel] = useState(1);
  const [selectedLine, setSelectedLine] = useState<RooflineItem | null>(null);
  const [selectedPart, setSelectedPart] = useState<{ part: RoofPartItem; index: number } | null>(null);


  const hasPixelCoords = Boolean(ringPixels && ringPixels.length >= 3);

  let minX = -15, maxX = 15, minY = -15, maxY = 15;
  if (ringMeters && ringMeters.length > 0) {
    minX = Math.min(...ringMeters.map((p) => p[0]));
    maxX = Math.max(...ringMeters.map((p) => p[0]));
    minY = Math.min(...ringMeters.map((p) => p[1]));
    maxY = Math.max(...ringMeters.map((p) => p[1]));
  }
  const cxM = (minX + maxX) / 2;
  const cyM = (minY + maxY) / 2;
  const spanM = Math.max(maxX - minX, maxY - minY, 10);
  const padM = Math.max(10, spanM * 0.35);
  const radiusM = spanM / 2 + padM;

  const projectMeterToPx = (x: number, y: number): [number, number] => {
    // Recover the actual crop transform from the corresponding captured ring,
    // rather than guessing its margins for parcel/context layers.
    if (hasPixelCoords && ringPixels && ringMeters.length >= 3) {
      const count = Math.min(ringPixels.length, ringMeters.length);
      const fit = (axis: number) => {
        const source = ringMeters.slice(0, count).map(p => p[axis]);
        const target = ringPixels.slice(0, count).map(p => p[axis]);
        const a = source.reduce((sum, v) => sum + v, 0) / count;
        const b = target.reduce((sum, v) => sum + v, 0) / count;
        const variance = source.reduce((sum, v) => sum + (v - a) ** 2, 0);
        const scale = source.reduce((sum, v, i) => sum + (v - a) * (target[i] - b), 0) / Math.max(variance, 1e-9);
        return (value: number) => b + (value - a) * scale;
      };
      return [fit(0)(x), fit(1)(y)];
    }
    // Schematic only when no source crop coordinates are available.
    const u = ((x - (cxM - radiusM)) / (radiusM * 2)) * 512;
    const v = (((cyM + radiusM) - y) / (radiusM * 2)) * 512;
    return [Math.round(u * 10) / 10, Math.round(v * 10) / 10];
  };

  const getBoundaryPoints = (): [number, number][] => {
    if (hasPixelCoords && ringPixels) return ringPixels;
    return (ringMeters || []).map(([x, y]) => projectMeterToPx(x, y));
  };

  const getPartPoints = (part: RoofPartItem): [number, number][] => {
    if (part.ring_pixels && part.ring_pixels.length >= 3) return part.ring_pixels;
    const ring = part.ring_meters || part.ring || [];
    return ring.map(([x, y]) => projectMeterToPx(x, y));
  };

  const getLinePoints = (line: RooflineItem): [[number, number], [number, number]] | null => {
    if (line.line_pixels && line.line_pixels.length >= 2) {
      return [line.line_pixels[0], line.line_pixels[1]];
    }
    const coords = line.line_meters;
    if (!coords || coords.length < 2) return null;
    return [projectMeterToPx(coords[0][0], coords[0][1]), projectMeterToPx(coords[1][0], coords[1][1])];
  };

  const facePalette = [
    "rgba(56, 189, 248, 0.40)",  // sky
    "rgba(251, 191, 36, 0.40)",  // amber
    "rgba(167, 139, 250, 0.40)", // purple
    "rgba(52, 211, 153, 0.40)",  // emerald
    "rgba(248, 113, 113, 0.40)", // rose
    "rgba(96, 165, 250, 0.40)",  // blue
  ];

  const isCutRejected = (lineCoords: [[number, number], [number, number]]) => {
    if (!lineCoords || lineCoords.length < 2) return false;
    const p1 = lineCoords[0];
    const p2 = lineCoords[1];
    const midX = (p1[0] + p2[0]) / 2;
    const midY = (p1[1] + p2[1]) / 2;
    return rejectedCuts.some((rej) => {
      if (rej.length < 2) return false;
      const rmidX = (rej[0][0] + rej[1][0]) / 2;
      const rmidY = (rej[0][1] + rej[1][1]) / 2;
      return Math.hypot(midX - rmidX, midY - rmidY) < 1.0;
    });
  };

  return (
    <div className="relative w-full h-full min-h-[420px] bg-slate-950 rounded-lg overflow-hidden border border-slate-800 flex flex-col">
      {/* Top Toolbar: Five Layer Hierarchy */}
      <div className="review-canvas-toolbar bg-slate-900/95 border-b border-slate-800 px-3 py-2 flex items-center justify-between z-10">
        <div className="flex items-center space-x-1.5 text-xs">
          {/* Layer 1: Boundaries */}
          <button
            disabled={!propertyBoundaryMeters?.length}
            aria-pressed={showPropertyBoundary && Boolean(propertyBoundaryMeters?.length)}
            onClick={() => setShowPropertyBoundary((v) => !v)}
            className={`px-2.5 py-1 rounded transition-colors flex items-center space-x-1.5 font-medium ${
              showPropertyBoundary
                ? "bg-amber-500/20 text-amber-300 border border-amber-500/50"
                : "text-slate-400 hover:text-white border border-transparent"
            }`}
            title={propertyBoundaryMeters?.length ? "Show supplied property boundary" : "No property boundary supplied"}
          >
            <span className="w-2 h-2 rounded-full bg-amber-400 inline-block" />
            <span>Property</span>
          </button>

          {/* Layer 2: Roof Polygons */}
          <button
            onClick={() => setShowRoofFootprint((v) => !v)}
            className={`px-2.5 py-1 rounded transition-colors flex items-center space-x-1.5 font-medium ${
              showRoofFootprint
                ? "bg-cyan-500/20 text-cyan-300 border border-cyan-500/50"
                : "text-slate-400 hover:text-white border border-transparent"
            }`}
            title="Layer 2: 2D roof footprint eave polygon"
          >
            <span className="w-2 h-2 rounded-full bg-cyan-400 inline-block" />
            <span>Outline</span>
          </button>

          {/* Layer 3: Confidence HUD */}
          <button
            onClick={() => setShowConfidenceHUD((v) => !v)}
            className={`px-2.5 py-1 rounded transition-colors flex items-center space-x-1.5 font-medium ${
              showConfidenceHUD
                ? "bg-purple-500/20 text-purple-300 border border-purple-500/50"
                : "text-slate-400 hover:text-white border border-transparent"
            }`}
            title="Layer 3: Accuracy confidence HUD & reflection diagnostics"
          >
            <span className="w-2 h-2 rounded-full bg-purple-400 inline-block" />
            <span>Diagnostics</span>
          </button>

          {/* Layer 4: Other Layers */}
          <button
            disabled={!nearbyPins?.length && !treeCanopiesMeters?.length && !neighbourBuildingsMeters?.length}
            onClick={() => setShowOtherLayers((v) => !v)}
            className={`px-2.5 py-1 rounded transition-colors flex items-center space-x-1.5 font-medium ${
              showOtherLayers
                ? "bg-emerald-500/20 text-emerald-300 border border-emerald-500/50"
                : "text-slate-400 hover:text-white border border-transparent"
            }`}
            title="Layer 4: Tree canopies, G-NAF address pins, neighbour footprints"
          >
            <span className="w-2 h-2 rounded-full bg-emerald-400 inline-block" />
            <span>Context</span>
          </button>

          {/* Layer 5: 3D Layering */}
          <button
            onClick={() => {
              const next = !(showFaces && showLines);
              setShowFaces(next);
              setShowLines(next);
            }}
            className={`px-2.5 py-1 rounded transition-colors flex items-center space-x-1.5 font-medium ${
              showFaces && showLines
                ? "bg-green-500/20 text-green-300 border border-green-500/50"
                : "text-slate-400 hover:text-white border border-transparent"
            }`}
            title="Layer 5: 3D pitched facets, ridges, hips, and valleys"
          >
            <span className="w-2 h-2 rounded-full bg-green-400 inline-block" />
            <span>Faces & lines</span>
          </button>

          {maskImageUrl && (
            <button
              onClick={() => setShowMask((v) => !v)}
              className={`px-2 py-1 rounded transition-colors flex items-center space-x-1 ${
                showMask
                  ? "bg-teal-600/30 text-teal-300 border border-teal-500/40"
                  : "text-slate-500 hover:text-white"
              }`}
              title="Toggle image colour evidence mask"
            >
              <span>Image mask</span>
            </button>
          )}

          <button
            onClick={() => setShowImagery((v) => !v)}
            className={`px-2 py-1 rounded transition-colors flex items-center space-x-1 ${
              showImagery ? "text-slate-300 hover:text-white" : "text-slate-600"
            }`}
            title="Toggle background aerial imagery"
          >
            <Eye className="w-3.5 h-3.5" />
          </button>
        </div>

        <div className="flex items-center space-x-2 text-xs">
          {rejectedCuts.length > 0 && (
            <button
              onClick={onRestoreCuts}
              className="px-2 py-1 bg-red-900/40 hover:bg-red-800/60 text-red-300 border border-red-700/50 rounded flex items-center space-x-1"
            >
              <Trash2 className="w-3 h-3" />
              <span>Restore Cuts ({rejectedCuts.length})</span>
            </button>
          )}
          <button
            onClick={() => setZoomLevel((z) => Math.min(2.5, z + 0.25))}
            className="p-1 hover:bg-slate-800 text-slate-400 hover:text-white rounded"
            title="Zoom in"
          >
            <ZoomIn className="w-4 h-4" />
          </button>
          <button
            onClick={() => setZoomLevel((z) => Math.max(0.75, z - 0.25))}
            className="p-1 hover:bg-slate-800 text-slate-400 hover:text-white rounded"
            title="Zoom out"
          >
            <ZoomOut className="w-4 h-4" />
          </button>
          <button
            onClick={() => setZoomLevel(1)}
            className="p-1 hover:bg-slate-800 text-slate-400 hover:text-white rounded"
            title="Reset Zoom"
          >
            <Maximize2 className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Secondary Sub-Bar: Granular Modelling Layer Toggles */}
      <div className="review-canvas-toolbar bg-slate-900/90 border-b border-slate-800 px-3 py-1.5 flex items-center justify-between z-10 text-xs overflow-x-auto">
        <div className="flex items-center space-x-1.5 flex-nowrap">
          <span className="text-[10px] font-bold uppercase tracking-wider text-slate-400 mr-1 flex items-center space-x-1">
            <span>Modelling Layers:</span>
          </span>

          {/* Ridges Toggle */}
          <button
            onClick={() => setShowRidges((v) => !v)}
            className={`px-2 py-0.5 rounded text-[11px] font-medium transition-colors flex items-center space-x-1 border ${
              showRidges
                ? "bg-emerald-500/20 text-emerald-300 border-emerald-500/40"
                : "bg-slate-900/40 text-slate-500 border-slate-800 hover:text-slate-300"
            }`}
            title="Toggle Roof Ridges (green apex lines)"
          >
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 inline-block" />
            <span>Ridges</span>
          </button>

          {/* Hips Toggle */}
          <button
            onClick={() => setShowHips((v) => !v)}
            className={`px-2 py-0.5 rounded text-[11px] font-medium transition-colors flex items-center space-x-1 border ${
              showHips
                ? "bg-cyan-500/20 text-cyan-300 border-cyan-500/40"
                : "bg-slate-900/40 text-slate-500 border-slate-800 hover:text-slate-300"
            }`}
            title="Toggle Roof Hips (cyan sloping external junctions)"
          >
            <span className="w-1.5 h-1.5 rounded-full bg-cyan-400 inline-block" />
            <span>Hips</span>
          </button>

          {/* Valleys Toggle */}
          <button
            onClick={() => setShowValleys((v) => !v)}
            className={`px-2 py-0.5 rounded text-[11px] font-medium transition-colors flex items-center space-x-1 border ${
              showValleys
                ? "bg-purple-500/20 text-purple-300 border-purple-500/40"
                : "bg-slate-900/40 text-slate-500 border-slate-800 hover:text-slate-300"
            }`}
            title="Toggle Roof Valleys (purple internal drainage creases)"
          >
            <span className="w-1.5 h-1.5 rounded-full bg-purple-400 inline-block" />
            <span>Valleys</span>
          </button>

          {/* Facet Planes Toggle */}
          <button
            onClick={() => setShowFacetMeshes((v) => !v)}
            className={`px-2 py-0.5 rounded text-[11px] font-medium transition-colors flex items-center space-x-1 border ${
              showFacetMeshes
                ? "bg-amber-500/20 text-amber-300 border-amber-500/40"
                : "bg-slate-900/40 text-slate-500 border-slate-800 hover:text-slate-300"
            }`}
            title="Toggle 3D Pitched Facet Planes & Shading"
          >
            <span className="w-1.5 h-1.5 rounded-full bg-amber-400 inline-block" />
            <span>Facet Planes</span>
          </button>

          {/* Wireframe Toggle */}
          <button
            onClick={() => setWireframeFacets((v) => !v)}
            className={`px-2 py-0.5 rounded text-[11px] font-medium transition-colors flex items-center space-x-1 border ${
              wireframeFacets
                ? "bg-sky-500/20 text-sky-300 border-sky-500/40"
                : "bg-slate-900/40 text-slate-500 border-slate-800 hover:text-slate-300"
            }`}
            title="Toggle Facet Wireframe Outlines"
          >
            <span>Wireframe</span>
          </button>

          {/* Labels Toggle */}
          <button
            onClick={() => setShowFacetLabels((v) => !v)}
            className={`px-2 py-0.5 rounded text-[11px] font-medium transition-colors flex items-center space-x-1 border ${
              showFacetLabels
                ? "bg-blue-500/20 text-blue-300 border-blue-500/40"
                : "bg-slate-900/40 text-slate-500 border-slate-800 hover:text-slate-300"
            }`}
            title="Toggle Facet Slope Degrees & Names"
          >
            <span>Labels</span>
          </button>
        </div>

        {/* Quick layer counter info */}
        <div className="hidden sm:flex items-center space-x-2 text-[10px] text-slate-400 font-mono">
          <span>{parts?.length || 0} Facets</span>
          <span>•</span>
          <span>{lines?.length || 0} Rooflines</span>
        </div>
      </div>

      {/* Main Canvas Area */}
      <div className="relative flex-1 min-h-0 overflow-hidden flex items-center justify-center p-2" style={{ containerType: "size" }}>
        <div
          className="relative transition-transform duration-150 ease-out"
          style={{ transform: `scale(${zoomLevel})`, width: "min(100cqw, 100cqh)", aspectRatio: "1" }}
        >
          {/* Layer 0: Aerial Site Image */}
          {showImagery && siteImageUrl && !imageError && hasPixelCoords && (
            <img
              src={siteImageUrl}
              onError={() => setImageError(true)}
              alt="Aerial orthophoto site view"
              className="w-full h-full object-cover rounded shadow-2xl pointer-events-none select-none opacity-90"
            />
          )}

          {(!siteImageUrl || imageError || !hasPixelCoords) && <p role="status" className="absolute top-2 left-2 right-2 text-center text-xs text-slate-300 bg-slate-900 p-2 rounded z-10">Aerial crop unavailable. Showing schematic geometry.</p>}
          {/* Layer 0b: image colour evidence mask */}
          {showMask && maskImageUrl && (
            <img
              src={maskImageUrl}
              alt="Roof evidence mask"
              className="absolute inset-0 w-full h-full object-cover rounded pointer-events-none select-none opacity-60 mix-blend-screen"
            />
          )}

          {/* SVG Vector Overlays */}
          <svg
            viewBox="0 0 512 512"
            className="absolute inset-0 w-full h-full pointer-events-auto"
          >
            {/* Layer 4: Other Layers - Neighbouring Buildings */}
            {showOtherLayers &&
              (neighbourBuildingsMeters || []).map((poly, nbIdx) => {
                const pts = poly.map(([x, y]) => projectMeterToPx(x, y));
                if (pts.length < 3) return null;
                return (
                  <polygon
                    key={`nb-${nbIdx}`}
                    points={pts.map(([u, v]) => `${u},${v}`).join(" ")}
                    fill="rgba(100, 116, 139, 0.25)"
                    stroke="rgba(148, 163, 184, 0.50)"
                    strokeWidth="1.5"
                  />
                );
              })}

            {/* Layer 4: Other Layers - Tree Canopies */}
            {showOtherLayers &&
              (treeCanopiesMeters || []).map((poly, trIdx) => {
                const pts = poly.map(([x, y]) => projectMeterToPx(x, y));
                if (pts.length < 3) return null;
                return (
                  <polygon
                    key={`tr-${trIdx}`}
                    points={pts.map(([u, v]) => `${u},${v}`).join(" ")}
                    fill="rgba(16, 185, 129, 0.30)"
                    stroke="rgba(52, 211, 153, 0.65)"
                    strokeWidth="1.5"
                  />
                );
              })}

            {/* Layer 4: Other Layers - Nearby G-NAF Address Pins */}
            {showOtherLayers &&
              (nearbyPins || []).map((pin, pinIdx) => {
                if (!pin.meters) return null;
                const [pu, pv] = projectMeterToPx(pin.meters[0], pin.meters[1]);
                const isOwn = pin.label === "27";
                return (
                  <g key={`pin-${pinIdx}`}>
                    <circle
                      cx={pu}
                      cy={pv}
                      r={isOwn ? 8 : 6}
                      fill={isOwn ? "#2563eb" : "#ffffff"}
                      stroke="#1e293b"
                      strokeWidth={1.5}
                    />
                    <text
                      x={pu + (isOwn ? 11 : 9)}
                      y={pv + 4}
                      fill="#ffffff"
                      stroke="#0f172a"
                      strokeWidth={3}
                      paintOrder="stroke"
                      fontSize={isOwn ? "12" : "10"}
                      fontWeight="bold"
                    >
                      {pin.label}
                    </text>
                  </g>
                );
              })}

            {/* Layer 1: Boundaries - Cadastral Property Parcel */}
            {showPropertyBoundary && propertyBoundaryMeters && propertyBoundaryMeters.length >= 3 && (
              <g key="layer1-parcel">
                <polygon
                  points={propertyBoundaryMeters
                    .map(([x, y]) => projectMeterToPx(x, y))
                    .map(([u, v]) => `${u},${v}`)
                    .join(" ")}
                  fill="rgba(245, 158, 11, 0.15)"
                  stroke="#f59e0b"
                  strokeWidth="2.5"
                  strokeDasharray="6 4"
                />
                {propertyBoundaryMeters.map(([x, y], bIdx) => {
                  const [bu, bv] = projectMeterToPx(x, y);
                  return (
                    <circle
                      key={`b-peg-${bIdx}`}
                      cx={bu}
                      cy={bv}
                      r={3.5}
                      fill="#ffffff"
                      stroke="#f59e0b"
                      strokeWidth={2}
                    />
                  );
                })}
              </g>
            )}

            {/* Layer 2: Roof Polygons - 2D Eave Footprint */}
            {showRoofFootprint && (
              <g key="layer2-footprint">
                <polygon
                  points={getBoundaryPoints().map(([u, v]) => `${u},${v}`).join(" ")}
                  fill="none"
                  stroke="#06b6d4"
                  strokeWidth="2.5"
                  strokeDasharray="4 3"
                />
                {getBoundaryPoints().map(([bu, bv], vIdx) => (
                  <circle
                    key={`v-peg-${vIdx}`}
                    cx={bu}
                    cy={bv}
                    r={3}
                    fill="#06b6d4"
                    stroke="#ffffff"
                    strokeWidth={1.5}
                  />
                ))}
              </g>
            )}

            {/* Layer 5: 3D Layering - 3D Facet Meshes */}
            {showFaces && showFacetMeshes &&
              (parts || []).map((part, pIdx) => {
                const points = getPartPoints(part);
                if (points.length < 3) return null;
                const pointsStr = points.map(([u, v]) => `${u},${v}`).join(" ");
                const isSelected = selectedPart?.index === pIdx;
                
                // Centroid for label
                const cx = points.reduce((acc, p) => acc + p[0], 0) / points.length;
                const cy = points.reduce((acc, p) => acc + p[1], 0) / points.length;

                return (
                  <g key={`face-group-${pIdx}`}>
                    <polygon
                      points={pointsStr}
                      fill={wireframeFacets ? "none" : facePalette[pIdx % facePalette.length]}
                      stroke={isSelected ? "#38bdf8" : wireframeFacets ? "rgba(56, 189, 248, 0.85)" : "rgba(255, 255, 255, 0.45)"}
                      strokeWidth={isSelected ? "2.5" : wireframeFacets ? "1.8" : "1.2"}
                      strokeDasharray={wireframeFacets ? "4 3" : undefined}
                      className="cursor-pointer hover:opacity-80 transition-opacity"
                      onClick={() => {
                        setSelectedPart({ part, index: pIdx });
                        setSelectedLine(null);
                      }}
                    />
                    {showFacetLabels && (
                      <text
                        x={cx}
                        y={cy + 3}
                        textAnchor="middle"
                        fill="#ffffff"
                        fontSize="9"
                        fontWeight="bold"
                        stroke="#0f172a"
                        strokeWidth="2.5"
                        paintOrder="stroke"
                        className="pointer-events-none select-none font-mono"
                      >
                        {part.pitch_deg ? `${part.pitch_deg}°` : part.name || `P${pIdx}`}
                      </text>
                    )}
                  </g>
                );
              })}

            {/* Layer 5: 3D Layering - Classified Rooflines */}
            {showLines &&
              (lines || []).map((line, lIdx) => {
                const kind = line.kind || "ridge";
                if (kind === "ridge" && !showRidges) return null;
                if (kind === "hip" && !showHips) return null;
                if (kind === "valley" && !showValleys) return null;
                if (kind === "step_eave" && !showStepEaves) return null;

                const pts = getLinePoints(line);
                if (!pts) return null;
                const [p1, p2] = pts;
                const rejected = isCutRejected(line.line_meters);
                const color = rejected ? "#ef4444" : LINE_COLORS[kind] || "#22c55e";

                return (
                  <g key={`line-${lIdx}`}>
                    <line
                      x1={p1[0]}
                      y1={p1[1]}
                      x2={p2[0]}
                      y2={p2[1]}
                      stroke="transparent"
                      strokeWidth="14"
                      className="cursor-pointer"
                      onClick={() => {
                        setSelectedLine(line);
                        setSelectedPart(null);
                      }}
                    />
                    <line
                      x1={p1[0]}
                      y1={p1[1]}
                      x2={p2[0]}
                      y2={p2[1]}
                      stroke={color}
                      strokeWidth={rejected ? "2.5" : "3.5"}
                      strokeDasharray={rejected ? "4 4" : undefined}
                      className="cursor-pointer transition-all hover:stroke-white"
                      onClick={() => {
                        setSelectedLine(line);
                        setSelectedPart(null);
                      }}
                    />
                  </g>
                );
              })}
          </svg>

          {/* Layer 3: Confidence Level Floating HUD */}
          {showConfidenceHUD && signals && (
            <div className="absolute top-2 right-2 bg-slate-900/90 border border-slate-700/80 backdrop-blur-md rounded-lg p-2.5 shadow-2xl text-[11px] max-w-[210px] pointer-events-auto z-20">
              <div className="flex items-center justify-between border-b border-slate-700/60 pb-1 mb-1.5">
                <span className="font-semibold text-purple-300">Confidence HUD</span>
                <span className="px-1.5 py-0.5 rounded bg-emerald-950 text-emerald-300 border border-emerald-800 text-[10px] font-bold">
                  Grade {signals.quality_grade} ({signals.quality_score})
                </span>
              </div>
              <div className="space-y-1 text-slate-300">
                <div className="flex justify-between">
                  <span className="text-slate-400">IoU Overlap:</span>
                  <span className="font-mono text-emerald-400 font-semibold">{Math.round(signals.iou * 100)}%</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-400">Purity:</span>
                  <span className="font-mono text-emerald-400 font-semibold">{Math.round(signals.purity * 100)}%</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-400">Solidity:</span>
                  <span className="font-mono text-cyan-300 font-semibold">{Math.round(signals.solidity * 100)}%</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-400">Wonky Lines:</span>
                  <span className="font-mono text-emerald-400 font-semibold">{signals.wonky_lines ?? 0}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-400">Orthogonality:</span>
                  <span className="font-mono text-emerald-400 font-semibold">{Math.round((signals.orthogonality ?? 1.0) * 100)}%</span>
                </div>
                {reflection && (
                  <div className="pt-1 border-t border-slate-700/60 flex justify-between items-center">
                    <span className="text-slate-400">Reflection:</span>
                    <span className={`font-semibold px-1 rounded text-[10px] ${
                      reflection.reflection_verdict === "PASS"
                        ? "bg-emerald-950 text-emerald-300 border border-emerald-800"
                        : "bg-amber-950 text-amber-300 border border-amber-800"
                    }`}>
                      {reflection.reflection_verdict}
                    </span>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>

        {/* Selected Face Action Popover */}
        {selectedPart && (
          <div className="absolute bottom-4 left-4 bg-slate-900/95 border border-slate-700 shadow-2xl rounded-lg p-3 text-xs z-20 flex flex-col space-y-2 max-w-xs backdrop-blur-sm">
            <div className="flex items-center justify-between border-b border-slate-800 pb-1.5">
              <span className="font-semibold text-slate-200">
                Facet #{selectedPart.index + 1}:{" "}
                <span className="text-cyan-400 capitalize">
                  {selectedPart.part.name || selectedPart.part.kind?.replace("_", " ") || "Main Pitch"}
                </span>
              </span>
              <button
                onClick={() => setSelectedPart(null)}
                className="text-slate-400 hover:text-white"
              >
                ✕
              </button>
            </div>
            <div className="text-slate-300 space-y-1.5">
              <div className="flex justify-between items-center">
                <span className="text-slate-400">Pitch Angle:</span>
                <span className="font-mono text-emerald-400 font-semibold">
                  {(selectedPart.part.pitch_deg ?? selectedPart.part.slope_deg ?? 0).toFixed(1)}°
                  {selectedPart.part.pitch_ratio && (
                    <span className="text-slate-400 text-[11px] ml-1">({selectedPart.part.pitch_ratio})</span>
                  )}
                </span>
              </div>
              <div className="flex justify-between items-center">
                <span className="text-slate-400">Compass Azimuth:</span>
                <span className="font-mono text-cyan-300 font-semibold flex items-center space-x-1">
                  <span>{(selectedPart.part.azimuth_deg ?? 0).toFixed(1)}°</span>
                  <span className="px-1 py-0.2 bg-cyan-950 border border-cyan-800 rounded text-[10px] text-cyan-400">
                    {azimuthToCardinal(selectedPart.part.azimuth_deg ?? 0)}
                  </span>
                </span>
              </div>
              <div className="flex justify-between items-center">
                <span className="text-slate-400">Footprint Area (2D):</span>
                <span className="font-mono text-slate-200">{selectedPart.part.area_m2.toFixed(1)}m²</span>
              </div>
              {selectedPart.part.area_3d_m2 && (
                <div className="flex justify-between items-center">
                  <span className="text-slate-400">Surface Area (3D):</span>
                  <span className="font-mono text-amber-300 font-medium">
                    {selectedPart.part.area_3d_m2.toFixed(1)}m²
                    <span className="text-slate-400 text-[10px] ml-1">
                      (+{Math.round(((selectedPart.part.area_3d_m2 / selectedPart.part.area_m2) - 1) * 100)}%)
                    </span>
                  </span>
                </div>
              )}
              {selectedPart.part.z_range && (
                <div className="flex justify-between items-center">
                  <span className="text-slate-400">Elevation:</span>
                  <span className="font-mono text-slate-300 text-[11px]">
                    {selectedPart.part.z_range[0].toFixed(1)}m → {selectedPart.part.z_range[1].toFixed(1)}m
                  </span>
                </div>
              )}
            </div>
          </div>
        )}

        {/* Selected Line Action Popover */}
        {selectedLine && (
          <div className="absolute bottom-4 left-4 bg-slate-900/95 border border-slate-700 shadow-2xl rounded-lg p-3 text-xs z-20 flex flex-col space-y-2 max-w-xs backdrop-blur-sm">
            <div className="flex items-center justify-between border-b border-slate-800 pb-1.5">
              <span className="font-semibold text-slate-200">
                Line: <span className="text-cyan-400 capitalize">{selectedLine.kind || "Ridge"}</span>
              </span>
              <button
                onClick={() => setSelectedLine(null)}
                className="text-slate-400 hover:text-white"
              >
                ✕
              </button>
            </div>
            <div className="text-slate-300 space-y-1.5">
              <div className="flex justify-between items-center">
                <span className="text-slate-400">2D Length:</span>
                <span className="font-mono text-slate-200">
                  {selectedLine.length_2d_m
                    ? `${selectedLine.length_2d_m.toFixed(1)}m`
                    : `${Math.hypot(
                        selectedLine.line_meters[1][0] - selectedLine.line_meters[0][0],
                        selectedLine.line_meters[1][1] - selectedLine.line_meters[0][1]
                      ).toFixed(1)}m`}
                </span>
              </div>
              {selectedLine.length_3d_m && (
                <div className="flex justify-between items-center">
                  <span className="text-slate-400">3D Sloped Length:</span>
                  <span className="font-mono text-amber-300 font-medium">
                    {selectedLine.length_3d_m.toFixed(1)}m
                  </span>
                </div>
              )}
              {selectedLine.slope_deg !== undefined && (
                <div className="flex justify-between items-center">
                  <span className="text-slate-400">Crease Slope:</span>
                  <span className="font-mono text-emerald-400">
                    {selectedLine.slope_deg.toFixed(1)}°
                  </span>
                </div>
              )}
              {selectedLine.line_z && (
                <div className="flex justify-between items-center">
                  <span className="text-slate-400">Elevation:</span>
                  <span className="font-mono text-slate-300 text-[11px]">
                    {selectedLine.line_z[0].toFixed(1)}m → {selectedLine.line_z[1].toFixed(1)}m
                  </span>
                </div>
              )}
            </div>
            <div className="pt-1 flex items-center space-x-2">
              <button
                onClick={() => {
                  onRejectCut(selectedLine.line_meters);
                  setSelectedLine(null);
                }}
                className="w-full px-2 py-1.5 bg-rose-600/30 hover:bg-rose-600/50 text-rose-200 border border-rose-500/40 rounded flex items-center justify-center space-x-1"
              >
                <AlertTriangle className="w-3.5 h-3.5 text-rose-400" />
                <span>Reject Cut (Merge)</span>
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Accuracy Signals Bar */}
      {signals && (
        <div className="bg-slate-950/90 border-t border-slate-800 px-3 py-1.5 flex flex-wrap items-center gap-2 text-xs text-slate-300">
          <span className="font-semibold text-slate-400">Image checks:</span>
          <span
            className={`px-1.5 py-0.5 rounded font-bold text-[11px] ${
              signals.quality_grade === "A"
                ? "bg-emerald-950 text-emerald-300 border border-emerald-800"
                : signals.quality_grade === "B"
                ? "bg-blue-950 text-blue-300 border border-blue-800"
                : "bg-amber-950 text-amber-300 border border-amber-800"
            }`}
          >
            {signals.quality_grade} ({signals.quality_score} pts)
          </span>
          <span className="px-1.5 py-0.5 rounded bg-slate-900 border border-slate-800 text-[11px]">
            IoU:{" "}
            <strong className={signals.iou >= 0.7 ? "text-emerald-400" : "text-amber-400"}>
              {Math.round(signals.iou * 100)}%
            </strong>
          </span>
          <span className="px-1.5 py-0.5 rounded bg-slate-900 border border-slate-800 text-[11px]">
            Purity:{" "}
            <strong className={signals.purity >= 0.8 ? "text-emerald-400" : "text-amber-400"}>
              {Math.round(signals.purity * 100)}%
            </strong>
          </span>
          <span className="px-1.5 py-0.5 rounded bg-slate-900 border border-slate-800 text-[11px]">
            Coverage:{" "}
            <strong className={signals.coverage >= 0.8 ? "text-emerald-400" : "text-amber-400"}>
              {Math.round(signals.coverage * 100)}%
            </strong>
          </span>
          <span className="px-1.5 py-0.5 rounded bg-slate-900 border border-slate-800 text-[11px]">
            Topology:{" "}
            <strong className={signals.topology_match ? "text-emerald-400" : "text-rose-400"}>
              {signals.is_concave
                ? `Concave (${signals.notch_angles_deg.filter((a) => a >= 225).join("°, ")}°)`
                : "Convex"}
            </strong>
          </span>
          {signals.straightness !== undefined && (
            <span className="px-1.5 py-0.5 rounded bg-slate-900 border border-slate-800 text-[11px]">
              Straightness:{" "}
              <strong className={signals.straightness >= 0.95 ? "text-emerald-400" : "text-amber-400"}>
                {Math.round(signals.straightness * 100)}%
              </strong>
            </span>
          )}
          {signals.wonky_lines !== undefined && signals.wonky_lines > 0 && (
            <span className="px-1.5 py-0.5 rounded bg-rose-950/80 border border-rose-800 text-rose-300 text-[11px]">
              Wonky: <strong>{signals.wonky_lines}</strong>
            </span>
          )}
          {reflection && (
            <span
              className={`px-1.5 py-0.5 rounded font-bold text-[11px] border cursor-help ${
                reflection.reflection_verdict === "PASS"
                  ? "bg-emerald-950/90 text-emerald-300 border-emerald-800"
                  : "bg-amber-950/90 text-amber-300 border-amber-800"
              }`}
              title={
                reflection.recommendations.length > 0
                  ? `Self-Critique Verdict: ${reflection.reflection_verdict}\n\nCritiques:\n${reflection.critique_items.join("\n")}\n\nRecommendations:\n${reflection.recommendations.join("\n")}`
                  : `Automated image checks passed. This is not an independent accuracy measurement.`
              }
            >
              Reflection: {reflection.reflection_verdict}
            </span>
          )}
          {signals.warnings && signals.warnings.length > 0 && (
            <span
              className="flex items-center gap-1 text-amber-400 ml-auto font-medium text-[11px]"
              title={signals.warnings.join("\n")}
            >
              <AlertTriangle className="w-3 h-3 text-amber-400" />
              <span>
                {signals.warnings.length} warning{signals.warnings.length > 1 ? "s" : ""}
              </span>
            </span>
          )}
        </div>
      )}

      <div className="flex gap-2 border-t border-slate-800 px-3 py-2">
        <select aria-label="Inspect roofline" className="min-w-0" value={selectedLine ? lines.indexOf(selectedLine) : ""} disabled={!lines.length} onChange={event => { setSelectedLine(event.target.value === "" ? null : lines[Number(event.target.value)]); setSelectedPart(null); }}>
          <option value="">Inspect a roofline…</option>
          {lines.map((line, index) => <option key={index} value={index}>{index + 1}. {line.kind || "Roofline"}{line.length_2d_m != null ? ` · ${line.length_2d_m.toFixed(1)} m` : ""}</option>)}
        </select>
        <select aria-label="Inspect roof face" className="min-w-0" value={selectedPart?.index ?? ""} disabled={!parts.length} onChange={event => { const index = Number(event.target.value); setSelectedPart(event.target.value === "" ? null : { part: parts[index], index }); setSelectedLine(null); }}>
          <option value="">Inspect a roof face…</option>
          {parts.map((part, index) => <option key={index} value={index}>{index + 1}. {part.name || part.kind?.replaceAll("_", " ") || "Roof face"}</option>)}
        </select>
      </div>
      {/* Legend footer */}
      <div className="bg-slate-900/80 border-t border-slate-800 px-3 py-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-slate-400">
        <span className="font-medium text-slate-300">Legend:</span>
        <span className="flex items-center space-x-1">
          <span className="w-2.5 h-1 bg-green-500 inline-block rounded" />
          <span>Ridge</span>
        </span>
        <span className="flex items-center space-x-1">
          <span className="w-2.5 h-1 bg-cyan-500 inline-block rounded" />
          <span>Hip</span>
        </span>
        <span className="flex items-center space-x-1">
          <span className="w-2.5 h-1 bg-amber-500 inline-block rounded" />
          <span>Step Eave</span>
        </span>
        <span className="flex items-center space-x-1">
          <span className="w-2.5 h-1 bg-purple-500 inline-block rounded" />
          <span>Valley</span>
        </span>
        <span className="flex items-center space-x-1">
          <span className="w-2.5 h-1 border border-dashed border-red-500 inline-block rounded" />
          <span>Outer Ring</span>
        </span>
      </div>
    </div>
  );
}
