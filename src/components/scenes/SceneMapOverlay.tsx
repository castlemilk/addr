"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import { Popup, type Map as MapLibreMap, type FilterSpecification, type GeoJSONSource } from "maplibre-gl";
import { SceneMapLayer } from "@/lib/scene-map-layer";
import { CLASS_COLORS, CLASS_NAMES, SCENE_CLASSES } from "@/lib/scene-types";
import type { SceneMapData } from "@/lib/scene-map-types";
import type { RealRoofLayer } from "@/lib/real-roof-layer";
import { sceneMapWindow } from "@/lib/scene-map-view";
import PropertyMapLayer from "./PropertyMapLayer";
import RoofConfidenceMap from "./RoofConfidenceMap";

export default function SceneMapOverlay({ map, roofLayer, threeDimensional, onShow3D, onSelectLocation, interactive }: {
  map: MapLibreMap; roofLayer: React.RefObject<RealRoofLayer | null>; threeDimensional: boolean; interactive: boolean;
  onShow3D: () => void;
  onSelectLocation: (lat: number, lon: number) => void;
}) {
  const [enabled, setEnabled] = useState(true), [layer, setLayer] = useState("edited"), [kind, setKind] = useState("all");
  const [confidence, setConfidence] = useState(false), [overview, setOverview] = useState(false);
  const [data, setData] = useState<SceneMapData | null>(null), [pending, setPending] = useState(false), [error, setError] = useState(""), [refresh, setRefresh] = useState(0);
  const mesh = useRef<SceneMapLayer | null>(null), filters = useRef(new Map<string, FilterSpecification | null>()), popup = useRef<Popup | null>(null);
  const visible = useMemo(() => new Set<string>(kind === "all" ? SCENE_CLASSES : [kind]), [kind]);
  const roofCount = visible.has("building") ? data?.outlines.features.filter(f => f.properties?.kind === "building").length ?? 0 : 0;
  useEffect(() => {
    if (!enabled) return;
    let request: AbortController | undefined, timer: ReturnType<typeof setTimeout>;
    const load = () => {
      clearTimeout(timer); request?.abort();
      timer = setTimeout(() => {
        const controller = new AbortController(); request = controller; setPending(true); setError("");
        const b = map.getBounds(), center = map.getCenter();
        const window = sceneMapWindow([b.getWest(), b.getSouth(), b.getEast(), b.getNorth()], [center.lng, center.lat]);
        setOverview(window.limited);
        const query = new URLSearchParams({ bounds: window.bounds.join(","), focus: [center.lng, center.lat].join(","), layer });
        void fetch(`/api/scenes/map?${query}`, { signal: controller.signal, cache: "no-store" }).then(async r => {
          const value = await r.json(); if (!r.ok) throw new Error(value.error); if (!controller.signal.aborted) setData(value);
        }).catch(e => { if (!controller.signal.aborted) setError(e.message); }).finally(() => { if (!controller.signal.aborted) setPending(false); });
      }, 250);
    };
    map.on("moveend", load); window.addEventListener("focus", load); load();
    return () => { clearTimeout(timer); request?.abort(); map.off("moveend", load); window.removeEventListener("focus", load); };
  }, [map, enabled, layer, refresh]);
  useEffect(() => {
    let applied = false, coveredRoof: RealRoofLayer | null = null;
    const install = () => {
      if (!map.isStyleLoaded()) return;
      if (!map.getSource("scene-review")) {
        map.addSource("scene-review", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
        const colors = ["match", ["get", "kind"], ...SCENE_CLASSES.flatMap(k => [k, CLASS_COLORS[k]]), "#67d7e7"] as unknown as import("maplibre-gl").ExpressionSpecification;
        map.addLayer({ id: "scene-review-fill", type: "fill", source: "scene-review", paint: { "fill-color": colors, "fill-opacity": .12 } });
        map.addLayer({ id: "scene-review-line", type: "line", source: "scene-review", paint: { "line-color": colors, "line-width": 2 } });
      }
      const shown = enabled && data;
      (map.getSource("scene-review") as GeoJSONSource).setData(shown ? { ...data.outlines, features: data.outlines.features.filter(f => visible.has(f.properties?.kind)) } : { type: "FeatureCollection", features: [] });
      for (const [objectKind, ids] of [["building", ["building-3d-vector", "building-2d"]], ["tree", ["tree-3d-vector", "tree-2d"]]] as const) {
        const geometry = shown && visible.has(objectKind) ? data.coverage[objectKind] : null;
        for (const id of ids) if (map.getLayer(id)) {
          if (!filters.current.has(id)) filters.current.set(id, map.getFilter(id) || null);
          const original = filters.current.get(id)!;
          map.setFilter(id, geometry?.coordinates.length ? ["all", ...(original ? [original] : []), ["!", ["within", geometry]]] as FilterSpecification : original);
        }
      }
      coveredRoof = roofLayer.current;
      coveredRoof?.setSceneCoverage(shown && visible.has("building") ? data.coverage.building.coordinates : []);
      if (!mesh.current) mesh.current = new SceneMapLayer();
      if (!map.getLayer(mesh.current.id)) map.addLayer(mesh.current);
      mesh.current.setData(data ?? undefined, visible, !!shown && threeDimensional);
      applied = true;
    };
    const ready = () => {
      if (!applied) install();
      else if (coveredRoof !== roofLayer.current) {
        coveredRoof = roofLayer.current;
        coveredRoof?.setSceneCoverage(enabled && data && visible.has("building") ? data.coverage.building.coordinates : []);
      }
    };
    const restyled = () => { filters.current.clear(); applied = false; install(); };
    map.on("render", ready); map.on("idle", ready); map.on("style.load", restyled); install();
    return () => { map.off("render", ready); map.off("idle", ready); map.off("style.load", restyled); };
  }, [map, data, enabled, visible, roofLayer, threeDimensional]);
  useEffect(() => {
    const select = (event: import("maplibre-gl").MapMouseEvent) => {
      if (!interactive || confidence || !map.getLayer("scene-review-fill")) return;
      const feature = map.queryRenderedFeatures(event.point, { layers: ["scene-review-fill"] })[0];
      if (!feature?.properties) return;
      const p = feature.properties;
      onSelectLocation(event.lngLat.lat, event.lngLat.lng);
      const content = document.createElement("div"), title = document.createElement("strong"), info = document.createElement("p"), link = document.createElement("a");
      title.textContent = p.label;
      // Sentences, not mid-dot joins; the popup text follows the interface face (Geist), the popup card keeps the map theme.
      const status = String(p.status ?? "").replaceAll("_", " ");
      info.textContent = `${status.charAt(0).toUpperCase()}${status.slice(1)}. ${p.revision ? `Saved revision ${p.revision}` : "Model proposal"}.${p.stale ? " Earlier saved analysis." : ""}`;
      link.textContent = "Review this object";
      link.href = `/scenes?${new URLSearchParams({ scene: p.scene_id, object: p.object_id, status: "all", kind: p.kind })}`;
      link.style.cssText = "display:block;margin-top:10px;color:#075985;text-decoration:underline";
      content.style.cssText = "font-size:12px;font-family:var(--font-geist-sans),system-ui,sans-serif;line-height:1.6;color:#172033;max-width:240px";
      content.append(title, info, link); popup.current?.remove();
      popup.current = new Popup({ offset: 12 }).setLngLat(event.lngLat).setDOMContent(content).addTo(map);
    };
    map.on("click", select);
    return () => { map.off("click", select); popup.current?.remove(); };
  }, [map, onSelectLocation, interactive, confidence]);
  useEffect(() => () => { mesh.current?.onRemove(); }, []);
  return <details className="absolute bottom-36 left-4 z-20 max-h-[calc(100%_-_180px)] w-[300px] max-w-[calc(100%_-_32px)] overflow-y-auto rounded-lg border border-slate-300 bg-white p-3 text-slate-800 shadow-sm" open>
    <summary className="cursor-pointer text-xs font-semibold">Analysed scene layers</summary>
    <div className="mt-2 space-y-2 text-xs">
      <a href="/scenes/processing" className="block rounded border border-cyan-700 bg-cyan-50 px-2 py-2 font-semibold text-cyan-900">Open processing</a>
      {data?.scenes[0] && <a href={`/scenes/imagery?scene=${encodeURIComponent(data.scenes[0].model.id)}`} className="block rounded border border-slate-300 px-2 py-2">Compare imagery dates for this area</a>}
      <p className="text-[11px] text-slate-600">Live scan progress, roof inventory and confidence review.</p>
      <label className="flex items-center gap-2"><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)}/>Show CRM layers on map</label>
      {enabled && <><div className="flex gap-2">
        <label className="min-w-0 flex-1">Source<select aria-label="Map analysis source" disabled={confidence} className="mt-1 w-full rounded border border-slate-300 p-1.5 disabled:bg-slate-100" value={layer} onChange={e => { setData(null); setLayer(e.target.value); }}><option value="edited">Saved feedback</option><option value="model">Model estimates</option></select></label>
        <label className="min-w-0 flex-1">Objects<select aria-label="Map analysed object class" className="mt-1 w-full rounded border border-slate-300 p-1.5" value={kind} onChange={e => setKind(e.target.value)}><option value="all">All classes</option>{SCENE_CLASSES.map(k => <option key={k} value={k}>{CLASS_NAMES[k]}</option>)}</select></label>
      </div><p role="status" className="text-[11px] text-slate-600">{pending ? "Loading roof models for this area…" : data ? `${roofCount} roof models loaded as ${threeDimensional ? "3D" : "2D outlines"} from ${data.scenes.length} captures` : "Roof models load as you navigate the map."}</p>
      {(overview || data?.limited) && <p className="text-[11px] text-slate-600">Detailed models load near the map centre. Zoom in or move the map to inspect more roofs.</p>}
      {!threeDimensional && <button className="rounded border border-cyan-700 bg-cyan-50 px-2 py-1.5 font-semibold text-cyan-900" onClick={onShow3D}>Show roof models in 3D</button>}
      {data && <p className="text-[11px] text-slate-500">{data.duplicates} overlapping proposals hidden. Heights are model estimates.{data.scenes.some(s => s.stale) ? " Some feedback awaits recovery after re-analysis." : ""}{data.unavailable ? ` ${data.unavailable} captures unavailable.` : ""}</p>}
      {error && <p role="alert" className="text-[11px] text-red-700">{error}{data ? " Previously loaded models remain visible." : ""}</p>}
      <button className="rounded border border-slate-300 px-2 py-1.5 disabled:opacity-50" disabled={pending} onClick={() => setRefresh(n => n + 1)}>Refresh analysed map</button></>}
      <RoofConfidenceMap map={map} enabled={confidence} interactive={interactive} onEnabled={value => { setConfidence(value); if (value) { setLayer("model"); setData(null); } }}/>
      <PropertyMapLayer map={map}/>
    </div>
  </details>;
}
