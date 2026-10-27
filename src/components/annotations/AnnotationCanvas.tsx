"use client";

import { useCallback, useEffect, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from "react";
import type { Map as LeafletMap } from "leaflet";
import { Check, Crop as CropIcon, Hand, LocateFixed, MapPin, Minus, MousePointer2, Pentagon, Plus, Scan, Spline, Square, X } from "lucide-react";
import { insertVertex, makeGeometry, moveEdge, removeVertex, replaceVertex, vertices, type Crop, type Geometry, type Item, type Label, type Position, type Tool } from "../../lib/annotations/model";

const TOOLS = [
  { id: "select", name: "Select", shortcut: "V", icon: MousePointer2 },
  { id: "pan", name: "Move view", shortcut: "H", icon: Hand },
  { id: "point", name: "Point", shortcut: "P", icon: MapPin },
  { id: "line", name: "Line", shortcut: "L", icon: Spline },
  { id: "rectangle", name: "Rectangle", shortcut: "R", icon: Square },
  { id: "polygon", name: "Polygon", shortcut: "G", icon: Pentagon },
  { id: "crop", name: "Crop", shortcut: "C", icon: CropIcon },
] as const;

type ViewBox = { x: number; y: number; width: number; height: number };
export interface AnnotationEditorProps {
  item: Item;
  labels: Label[];
  selectedId: string | null;
  activeLabelId: string;
  tool: Tool;
  disabled: boolean;
  onToolChange: (tool: Tool) => void;
  onSelect: (id: string | null) => void;
  onAdd: (geometry: Geometry, shape?: string) => void;
  onUpdate: (id: string, geometry: Geometry) => void;
  onCrop: (crop?: Crop) => void;
  onMapView: (center: Position, zoom: number) => void;
  onPendingChange: (pending: boolean) => void;
  onItemChange?: (item: Item) => void;
  onNavigateItem?: (itemId: string, annotationId?: string) => void;
  allowedTools?: Tool[];
  viewState?: Record<string, unknown>;
  onViewStateChange?: (state: Record<string, unknown>) => void;
  readOnly?: boolean;
  showAnnotationLabels?: boolean;
  /** The last server copy of the item; adapters compare it with `item` to know what is unsaved. */
  savedItem?: Item;
  /** Commits `draft` to the session and saves it in one step; resolves true when the server accepted it. */
  onSaveItem?: (draft: Item) => Promise<boolean>;
  /** Opens the next queue item; undefined on the last item. */
  onNextItem?: () => void;
}

/** Screen pixels a point handle must travel before a press becomes a drag (an outline edit). */
const VERTEX_DRAG_PX = 3;
/** Edges shorter than this on screen get no "add point" handle (it would sit on top of the two point handles). */
const EDGE_HANDLE_MIN_PX = 28;

export default function AnnotationCanvas({ item, labels, selectedId, activeLabelId, tool, disabled, onToolChange, onSelect, onAdd, onUpdate, onCrop, onMapView, onPendingChange, allowedTools, readOnly, showAnnotationLabels = true }: AnnotationEditorProps) {
  const stageRef = useRef<HTMLDivElement>(null);
  const mapContainerRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const [map, setMap] = useState<LeafletMap | null>(null);
  const [size, setSize] = useState({ width: 800, height: 600 });
  const [imageSize, setImageSize] = useState({ width: item.source.width || 1200, height: item.source.height || 800 });
  const [imageState, setImageState] = useState<"loading" | "ready" | "error">("loading");
  const [mapError, setMapError] = useState("");
  const [, setMapVersion] = useState(0);
  const [view, setView] = useState<ViewBox | null>(null);
  const [points, setPoints] = useState<Position[]>([]);
  const [hover, setHover] = useState<Position | null>(null);
  const [boxStart, setBoxStart] = useState<{ point: Position; screen: Position } | null>(null);
  // `moved` turns true once the pointer has travelled VERTEX_DRAG_PX screen pixels: a click on a point handle (select, focus) is
  // not an outline edit and never reaches onUpdate.
  // mode "vertex" moves one point; "insert" drags a new point added on an edge (a click alone still adds it at the
  // midpoint); "edge" (Shift on an edge handle) slides the whole side along its normal from `start`.
  const [vertexDrag, setVertexDrag] = useState<{ id: string; index: number; geometry: Geometry; original: Geometry; screen: [number, number]; moved: boolean; mode: "vertex" | "insert" | "edge"; start?: Position } | null>(null);
  const [vertex, setVertex] = useState<{ id: string; index: number } | null>(null);
  const [panStart, setPanStart] = useState<{ screen: Position; view: ViewBox } | null>(null);
  const [wholeImage, setWholeImage] = useState(false);
  const isMap = item.adapter === "map";
  const supported = isMap || item.adapter === "image";
  const imageCrop = !wholeImage && tool !== "crop" ? item.crop : undefined;
  const baseView: ViewBox = imageCrop
    ? { x: imageCrop.x * imageSize.width, y: imageCrop.y * imageSize.height, width: imageCrop.width * imageSize.width, height: imageCrop.height * imageSize.height }
    : { x: 0, y: 0, ...imageSize };
  const imageView = view ?? baseView;
  const scale = isMap ? 1 : Math.max(imageView.width / size.width, imageView.height / size.height);
  const color = labels.find((label) => label.id === activeLabelId)?.color || "#2563eb";
  const pending = points.length > 0 || boxStart !== null || vertexDrag !== null;

  useEffect(() => { onPendingChange(pending); }, [pending, onPendingChange]);

  useEffect(() => {
    const element = stageRef.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => {
      setSize({ width: entry.contentRect.width, height: entry.contentRect.height });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (item.adapter !== "image") return;
    let cancelled = false;
    const img = new Image();
    img.onload = () => {
      if (!cancelled) {
        setImageSize({ width: img.naturalWidth, height: img.naturalHeight });
        setImageState("ready");
      }
    };
    img.onerror = () => { if (!cancelled) setImageState("error"); };
    img.src = item.source.url || "";
    return () => { cancelled = true; };
  }, [item.adapter, item.source.url]);

  useEffect(() => {
    if (!isMap || !mapContainerRef.current) return;
    let cancelled = false;
    let instance: LeafletMap | undefined;
    import("leaflet").then((leaflet) => {
      if (cancelled || !mapContainerRef.current) return;
      const center = item.source.center ?? [0, 20];
      instance = leaflet.map(mapContainerRef.current, {
        center: [center[1], center[0]], zoom: item.source.zoom ?? 3,
        zoomControl: false, attributionControl: true, worldCopyJump: true,
        maxBounds: [[-85.0511, -540], [85.0511, 540]],
      });
      const layer = leaflet.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap contributors</a>',
      }).addTo(instance);
      layer.on("tileerror", () => setMapError("Some map tiles could not load. Check your connection."));
      layer.on("load", () => setMapError(""));
      instance.on("move zoom resize", () => setMapVersion((version) => version + 1));
      setMap(instance);
    }).catch(() => setMapError("The map could not start. Reload this page to try again."));
    return () => { cancelled = true; instance?.remove(); };
    // A canvas is keyed by item id. View changes never replace geographic geometry.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isMap, item.id]);

  useEffect(() => { map?.invalidateSize({ animate: false }); }, [map, size]);

  useEffect(() => {
    // Tool changes cancel transient pointer state; no document edit is made.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setPoints([]); setHover(null); setBoxStart(null); setVertexDrag(null); setVertex(null);
    if (tool === "crop") setView(null);
  }, [tool]);

  function project(point: Position): Position {
    if (!isMap) return [point[0] * imageSize.width, point[1] * imageSize.height];
    if (!map) return [0, 0];
    // Display the nearest wrapped copy while preserving canonical WGS84 storage.
    const longitude = point[0] + Math.round((map.getCenter().lng - point[0]) / 360) * 360;
    const value = map.latLngToContainerPoint([point[1], longitude]);
    return [value.x, value.y];
  }

  // Measured against the drawing surface (not the event's own element), so handle groups inside it can call this too.
  function position(event: { clientX: number; clientY: number }, clamp = false): Position | null {
    return surfacePoint(event, clamp);
  }
  function surfacePoint(event: { clientX: number; clientY: number }, clamp = false): Position | null {
    const surface = svgRef.current;
    if (!surface) return null;
    if (isMap) {
      if (!map) return null;
      const rect = surface.getBoundingClientRect();
      const result = map.containerPointToLatLng([event.clientX - rect.left, event.clientY - rect.top]).wrap();
      return [Math.max(-180, Math.min(180, result.lng)), Math.max(-85.0511, Math.min(85.0511, result.lat))];
    }
    const matrix = surface.getScreenCTM();
    if (!matrix) return null;
    const result = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse());
    const bounds = imageCrop ? baseView : { x: 0, y: 0, ...imageSize };
    if (!clamp && (result.x < bounds.x || result.y < bounds.y || result.x > bounds.x + bounds.width || result.y > bounds.y + bounds.height)) return null;
    return [Math.max(0, Math.min(1, result.x / imageSize.width)), Math.max(0, Math.min(1, result.y / imageSize.height))];
  }

  const cancel = useCallback(() => {
    setPoints([]); setHover(null); setBoxStart(null); setVertexDrag(null); setPanStart(null);
  }, []);

  function finish(nextPoints = points) {
    if (disabled || readOnly) return;
    if (tool === "crop" && nextPoints.length === 2) {
      const [a, b] = nextPoints;
      if (Math.abs(a[0] - b[0]) < 0.002 || Math.abs(a[1] - b[1]) < 0.002) return;
      onCrop({ x: Math.min(a[0], b[0]), y: Math.min(a[1], b[1]), width: Math.abs(a[0] - b[0]), height: Math.abs(a[1] - b[1]) });
      setView(null); setWholeImage(false); onToolChange("select");
    } else {
      const geometry = makeGeometry(tool, nextPoints);
      if (!geometry) return;
      onAdd(geometry, tool === "rectangle" ? "rectangle" : undefined);
    }
    cancel();
  }

  function pointerDown(event: ReactPointerEvent<SVGSVGElement>) {
    if (disabled || (readOnly && tool !== "select" && tool !== "pan") || event.button !== 0 || (item.adapter === "image" && imageState !== "ready")) return;
    const point = position(event, !!vertexDrag || !!boxStart || !!panStart);
    if (!point) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    if (tool === "pan" && !isMap) setPanStart({ screen: [event.clientX, event.clientY], view: imageView });
    else if (tool === "select") onSelect(null);
    else if (tool === "rectangle" || tool === "crop") {
      setBoxStart({ point: points[0] ?? point, screen: [event.clientX, event.clientY] });
      setHover(point);
    }
  }

  function pointerMove(event: ReactPointerEvent<SVGSVGElement>) {
    const point = position(event, !!vertexDrag || !!boxStart || !!panStart);
    if (!point) return;
    if (vertexDrag) {
      const moved = vertexDrag.moved || Math.hypot(event.clientX - vertexDrag.screen[0], event.clientY - vertexDrag.screen[1]) >= VERTEX_DRAG_PX;
      if (moved) setVertexDrag({ ...vertexDrag, moved, geometry: vertexDrag.mode === "edge" && vertexDrag.start
        ? moveEdge(vertexDrag.original, vertexDrag.index, [point[0] - vertexDrag.start[0], point[1] - vertexDrag.start[1]])
        : replaceVertex(vertexDrag.original, vertexDrag.index, point) });
    }
    else if (panStart) {
      setView({ ...panStart.view, x: panStart.view.x - (event.clientX - panStart.screen[0]) * scale, y: panStart.view.y - (event.clientY - panStart.screen[1]) * scale });
    } else setHover(point);
  }

  function pointerUp(event: ReactPointerEvent<SVGSVGElement>) {
    if (disabled) return;
    if (vertexDrag) {
      if (vertexDrag.moved || vertexDrag.mode === "insert") onUpdate(vertexDrag.id, vertexDrag.geometry);
      if (vertexDrag.mode === "insert") setVertex({ id: vertexDrag.id, index: vertexDrag.index });
      setVertexDrag(null); return;
    }
    if (panStart) { setPanStart(null); return; }
    if (readOnly) return;
    if (item.adapter === "image" && imageState !== "ready") return;
    const point = position(event, !!boxStart);
    if (!point) return;
    if (tool === "point") finish([point]);
    else if (tool === "line" || tool === "polygon") setPoints([...points, point]);
    else if (tool === "rectangle" || tool === "crop") {
      if (!boxStart) return;
      const moved = Math.hypot(event.clientX - boxStart.screen[0], event.clientY - boxStart.screen[1]) > 5;
      if (points.length || moved) finish([boxStart.point, point]);
      else setPoints([boxStart.point]);
      setBoxStart(null);
    }
  }

  function zoom(factor: number) {
    if (isMap) { if (factor < 1) map?.zoomIn(); else map?.zoomOut(); return; }
    const width = Math.min(imageSize.width * 3, Math.max(imageSize.width / 32, imageView.width * factor));
    const height = width * imageView.height / imageView.width;
    setView({ x: imageView.x + (imageView.width - width) / 2, y: imageView.y + (imageView.height - height) / 2, width, height });
  }

  function pathFor(geometry: Geometry) {
    if (geometry.type === "Point") return "";
    const rings = geometry.type === "Polygon" ? geometry.coordinates : [geometry.coordinates];
    return rings.map((ring) => ring.map((point, index) => `${index ? "L" : "M"}${project(point).join(",")}`).join(" ") + (geometry.type === "Polygon" ? " Z" : "")).join(" ");
  }

  const previewPoints: Position[] = (tool === "rectangle" || tool === "crop")
    ? ((boxStart || points.length) && hover ? [boxStart?.point ?? points[0], hover] : [])
    : [...points, ...(hover && points.length ? [hover] : [])];
  const preview = makeGeometry(tool === "crop" ? "rectangle" : tool, previewPoints);
  const canFinish = tool === "line" ? points.length >= 2 : tool === "polygon" ? points.length >= 3 : false;
  const hint = tool === "pan" ? (isMap ? "Drag to explore. Pinch or scroll to zoom." : "Drag to move the image. Use + and − to zoom.")
    : tool === "select" ? "Select a mark, then drag its points. Drag a + on an edge to add a point; Shift-drag it to move the whole edge."
      : tool === "point" ? "Tap the canvas to place a point."
        : tool === "rectangle" || tool === "crop" ? "Drag a box, or tap two opposite corners."
          : "Tap to place points. Choose Finish when ready.";

  return <div className="fw-canvas-wrap">
    <div className={`fw-canvas ${isMap ? "fw-map-canvas" : "fw-image-canvas"}`} ref={stageRef}>
      {isMap && <div ref={mapContainerRef} className="fw-map" aria-label="OpenStreetMap world map" />}
      {!supported ? <div className="fw-canvas-message"><Scan size={30} /><h2>Viewer unavailable</h2><p>The “{item.adapter}” adapter has no editor installed. Its saved data is preserved.</p></div> : <>
        {!isMap && imageState === "loading" && <div className="fw-canvas-message" role="status"><span className="fw-spinner" /><p>Loading image…</p></div>}
        {!isMap && imageState === "error" && <div className="fw-canvas-message" role="alert"><Scan size={30} /><h2>Image could not load</h2><p>Check that the image URL is public and still available. Your annotations are preserved.</p><a href={item.source.url} target="_blank" rel="noreferrer" className="fw-button">Open source image</a></div>}
        <svg ref={svgRef}
          className={`fw-drawing-surface fw-tool-${tool} ${isMap && tool === "pan" ? "fw-pass-through" : ""}`}
          viewBox={isMap ? `0 0 ${size.width} ${size.height}` : `${imageView.x} ${imageView.y} ${imageView.width} ${imageView.height}`}
          style={{ "--fw-scale": scale } as CSSProperties}
          tabIndex={0} role="application" aria-label={`${item.adapter === "map" ? "Map" : "Image"} annotation canvas. ${hint} Keyboard editing is available in the inspector.`}
          onPointerDown={pointerDown} onPointerMove={pointerMove} onPointerUp={pointerUp} onPointerCancel={cancel}
          onKeyDown={(event) => {
            if (event.key === "Escape") { cancel(); event.preventDefault(); }
            if (event.key === "Enter" && canFinish) { finish(); event.preventDefault(); }
          }}>
          {!isMap && <defs><clipPath id={`fw-image-clip-${item.id}`}><rect x={imageCrop ? baseView.x : 0} y={imageCrop ? baseView.y : 0} width={imageCrop ? baseView.width : imageSize.width} height={imageCrop ? baseView.height : imageSize.height} /></clipPath></defs>}
          <g clipPath={!isMap ? `url(#fw-image-clip-${item.id})` : undefined}>
          {!isMap && imageState === "ready" && <image href={item.source.url} width={imageSize.width} height={imageSize.height} preserveAspectRatio="none" />}
          {(isMap ? !!map : imageState === "ready") && item.annotations.map((annotation, annotationIndex) => {
            const geometry = vertexDrag?.id === annotation.id ? vertexDrag.geometry : annotation.geometry;
            const label = labels.find((entry) => entry.id === annotation.labelId);
            const selected = selectedId === annotation.id;
            const stroke = label?.color || "#2563eb";
            const position = project(vertices(geometry)[0]);
            return <g key={annotation.id} onPointerDown={(event) => {
              if (disabled || tool !== "select") return;
              event.stopPropagation(); onSelect(annotation.id);
            }}>
              {geometry.type === "Point" ? <>
                <circle cx={position[0]} cy={position[1]} r={(selected ? 10 : 8) * scale} fill="white" stroke={stroke} strokeWidth={3 * scale} />
                <circle cx={position[0]} cy={position[1]} r={3 * scale} fill={stroke} />
              </> : <>
                <path d={pathFor(geometry)} fill={geometry.type === "Polygon" ? stroke : "none"} fillOpacity={selected ? .16 : .09} fillRule="evenodd" stroke="white" strokeWidth={(selected ? 5 : 4) * scale} strokeLinejoin="round" />
                <path d={pathFor(geometry)} fill="none" stroke={stroke} strokeWidth={(selected ? 2.8 : 2) * scale} strokeLinejoin="round" />
                <path d={pathFor(geometry)} fill={geometry.type === "Polygon" ? "transparent" : "none"} stroke="transparent" strokeWidth={18 * scale} className={tool === "select" ? "fw-shape-hit" : ""} />
              </>}
              {(showAnnotationLabels || selected) && <g transform={`translate(${position[0] + 10 * scale},${position[1] - 25 * scale}) scale(${scale})`} pointerEvents="none">
                <rect x={0} y={0} width={Math.min(190, (label?.name.length || 6) * 6.8 + 18)} height={23} rx={4} fill={stroke} />
                <text x={8} y={15.7} fill="white" fontSize={11.5} fontWeight={600}>{label?.name || `Mark ${annotationIndex + 1}`}</text>
              </g>}
              {/* Vertex handles match the scene editor: a focusable <g> with a 22 px touch disc, a 7 px dot that grows
                  to 10 px with inverted fill when selected, arrow keys nudge 1 image px (Shift 5), Backspace removes.
                  Enter and Escape without a drag bubble to the adapter (decision bar) and the canvas (cancel). */}
              {selected && !readOnly && tool === "select" && vertices(geometry).map((point, index) => {
                const [x, y] = project(point);
                const sel = vertex?.id === annotation.id && vertex.index === index;
                return <g key={index} className={`fw-vertex-handle${sel ? " is-selected" : ""}`} tabIndex={0} role="button" aria-label={`Outline point ${index + 1}`}
                  onFocus={() => setVertex({ id: annotation.id, index })}
                  onPointerDown={(event) => {
                    if (disabled) return;
                    event.stopPropagation(); event.currentTarget.setPointerCapture(event.pointerId);
                    setVertex({ id: annotation.id, index }); event.currentTarget.focus({ preventScroll: true });
                    setVertexDrag({ id: annotation.id, index, geometry, original: geometry, screen: [event.clientX, event.clientY], moved: false, mode: "vertex" });
                  }}
                  onKeyDown={(event) => {
                    if (disabled) return;
                    if (event.key === "Escape") {
                      // Abandon an in-progress drag: the original geometry is still in the document, nothing is updated.
                      if (vertexDrag) { event.preventDefault(); event.stopPropagation(); setVertexDrag(null); }
                      return;
                    }
                    if (event.key === "Backspace" || event.key === "Delete") {
                      if (geometry.type !== "Polygon") return;
                      event.preventDefault(); event.stopPropagation();
                      const next = removeVertex(geometry, index);
                      if (next !== geometry) { onUpdate(annotation.id, next); setVertex(null); }
                      return;
                    }
                    const moves: Record<string, Position> = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
                    const move = moves[event.key];
                    if (!move || isMap) return;
                    event.preventDefault(); event.stopPropagation();
                    const step = event.shiftKey ? 5 : 1;
                    const clamp = (value: number) => Math.max(0, Math.min(1, value));
                    onUpdate(annotation.id, replaceVertex(geometry, index, [clamp((point[0] * imageSize.width + move[0] * step) / imageSize.width), clamp((point[1] * imageSize.height + move[1] * step) / imageSize.height)]));
                  }}>
                  <title>{`Point ${index + 1} · drag or arrow keys · Backspace removes`}</title>
                  <circle cx={x} cy={y} r={22 * scale} fill="transparent" className="fw-vertex-hit" />
                  <circle cx={x} cy={y} r={(sel ? 5 : 3.5) * scale} fill={sel ? stroke : "white"} stroke={sel ? "white" : stroke} strokeWidth={1.5 * scale} className="fw-vertex-dot" pointerEvents="none" />
                </g>;
              })}
              {/* Edge handles: a small + at each edge midpoint. Drag (or click, or Enter) adds a point there; Shift-drag slides
                  the whole edge along its normal so a right-angle outline stays right-angled. Hidden while any handle is
                  dragged and on edges too short on screen. Enter is marked handled so the decision bar does not answer. */}
              {selected && !readOnly && tool === "select" && geometry.type !== "Point" && !vertexDrag && vertices(geometry).map((point, index, list) => {
                if (geometry.type === "LineString" && index === list.length - 1) return null;
                const other = list[(index + 1) % list.length];
                const [ax, ay] = project(point), [bx, by] = project(other);
                if (Math.hypot(bx - ax, by - ay) / scale < EDGE_HANDLE_MIN_PX) return null;
                const mid: Position = [(point[0] + other[0]) / 2, (point[1] + other[1]) / 2];
                const [x, y] = [(ax + bx) / 2, (ay + by) / 2];
                const add = () => { onUpdate(annotation.id, insertVertex(geometry, index, mid)); setVertex({ id: annotation.id, index: index + 1 }); };
                const r = 4 * scale;
                return <g key={`edge-${index}`} className="fw-vertex-handle fw-edge-handle" tabIndex={0} role="button" aria-label={`Add a point on edge ${index + 1}`}
                  onPointerDown={(event) => {
                    if (disabled) return;
                    event.stopPropagation(); event.currentTarget.setPointerCapture(event.pointerId);
                    const start = surfacePoint(event, true) ?? mid;
                    if (event.shiftKey) setVertexDrag({ id: annotation.id, index, geometry, original: geometry, screen: [event.clientX, event.clientY], moved: false, mode: "edge", start });
                    else {
                      const inserted = insertVertex(geometry, index, mid);
                      setVertexDrag({ id: annotation.id, index: index + 1, geometry: inserted, original: inserted, screen: [event.clientX, event.clientY], moved: false, mode: "insert" });
                    }
                  }}
                  onKeyDown={(event) => {
                    if (disabled || event.key !== "Enter") return;
                    event.preventDefault(); event.stopPropagation(); add();
                  }}>
                  <title>{`Edge ${index + 1} · drag or Enter adds a point · Shift-drag moves the edge`}</title>
                  <circle cx={x} cy={y} r={14 * scale} fill="transparent" className="fw-edge-hit" />
                  <rect x={x - r} y={y - r} width={2 * r} height={2 * r} rx={1 * scale} fill="white" fillOpacity={.85} stroke={stroke} strokeWidth={1.2 * scale} className="fw-edge-dot" pointerEvents="none" />
                  <path d={`M${x - r * .6},${y}H${x + r * .6}M${x},${y - r * .6}V${y + r * .6}`} stroke={stroke} strokeWidth={1.2 * scale} pointerEvents="none" />
                </g>;
              })}
            </g>;
          })}
          {preview && preview.type !== "Point" && <path d={pathFor(preview)} stroke={tool === "crop" ? "#fff" : color} strokeWidth={2.5 * scale} strokeDasharray={`${7 * scale} ${4 * scale}`} fill={preview.type === "Polygon" ? color : "none"} fillOpacity={.13} pointerEvents="none" />}
          {points.map((point, index) => { const [x, y] = project(point); return <circle key={index} cx={x} cy={y} r={4 * scale} fill="white" stroke={color} strokeWidth={2 * scale} pointerEvents="none" />; })}
          </g>
        </svg>
        <div className="fw-tools" role="toolbar" aria-label="Drawing tools">
          {TOOLS.filter((entry) => (!isMap || entry.id !== "crop") && (!allowedTools || allowedTools.includes(entry.id))).map((entry) => <button key={entry.id} type="button" className={`fw-tool ${tool === entry.id ? "is-active" : ""}`} aria-label={`${entry.name} (${entry.shortcut})`} title={`${entry.name} (${entry.shortcut})`} aria-pressed={tool === entry.id} disabled={disabled} onClick={() => onToolChange(entry.id)}><entry.icon size={19} strokeWidth={1.7} /><span>{entry.name === "Move view" ? "Move" : entry.name === "Rectangle" ? "Box" : entry.name}</span></button>)}
        </div>
        <div className="fw-view-tools" role="group" aria-label="Canvas view">
          <button className="fw-icon-button" aria-label="Zoom in" title="Zoom in" onClick={() => zoom(.8)}><Plus size={18} /></button>
          <button className="fw-icon-button" aria-label="Zoom out" title="Zoom out" onClick={() => zoom(1.25)}><Minus size={18} /></button>
          <button className="fw-icon-button" aria-label="Fit view" title="Fit view" onClick={() => {
            setView(null);
            if (map) { const center = item.source.center ?? [0, 20]; map.setView([center[1], center[0]], item.source.zoom ?? 3); }
          }}><Scan size={17} /></button>
        </div>
        {isMap && <button className="fw-button fw-save-view" disabled={!map || disabled} onClick={() => {
          if (!map) return;
          const center = map.getCenter().wrap(); onMapView([center.lng, center.lat], map.getZoom());
        }}><LocateFixed size={15} />Use this view</button>}
        {!isMap && item.crop && <button className="fw-button fw-save-view" onClick={() => { setWholeImage(!wholeImage); setView(null); }}>{wholeImage ? "Show crop" : "Whole image"}</button>}
        {pending && <div className="fw-drawing-actions"><span>{points.length ? `${points.length} point${points.length === 1 ? "" : "s"}` : "Drawing"}</span>{(tool === "line" || tool === "polygon") && <button className="fw-button fw-primary" disabled={!canFinish} onClick={() => finish()}><Check size={15} />Finish</button>}<button className="fw-icon-button" aria-label="Cancel drawing" onClick={cancel}><X size={17} /></button></div>}
        {mapError && <div className="fw-map-error" role="status">{mapError}</div>}
      </>}
    </div>
    <div className="fw-canvas-caption"><span>{hint}</span><span>{isMap ? "WGS84 · longitude, latitude" : imageState === "ready" ? `${imageSize.width} × ${imageSize.height} px${item.crop ? " · non-destructive crop" : ""}` : "Image coordinates"}</span></div>
  </div>;
}
