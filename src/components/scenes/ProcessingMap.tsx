"use client";

import { useEffect, useRef, useState } from "react";
import * as maplibregl from "maplibre-gl";
import type { GeoJSONSource } from "maplibre-gl";
import type { Feature, FeatureCollection, Polygon } from "geojson";
import { LocateFixed } from "lucide-react";
import { count, roofCenter, type ProcessingRoofRow, type ProcessingSummary } from "@/lib/scene-processing";

const empty: FeatureCollection = { type: "FeatureCollection", features: [] };
/** Roof outlines load per viewport from the bbox-scoped confidence route at this zoom and above.
 *  The suburb holds about 10,000 roofs; the map never requests them all at once. */
export const OUTLINE_ZOOM = 16.5;
type Viewport = { zoom: number; bounds: [number, number, number, number] | null };
const viewportOf = (m: maplibregl.Map): Viewport => {
  const b = m.getBounds();
  return { zoom: m.getZoom(), bounds: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()] };
};

export default function ProcessingMap({ summary, band, selected, onSelect, onTile }: {
  summary: ProcessingSummary; band: string; selected: ProcessingRoofRow | null;
  onSelect: (key: string) => void; onTile: (id: string) => void;
}) {
  const host = useRef<HTMLDivElement>(null), map = useRef<maplibregl.Map | null>(null);
  const latest = useRef({ summary, onSelect, onTile });
  const [ready, setReady] = useState(false), [error, setError] = useState("");
  const [aerial, setAerial] = useState(false), [grid, setGrid] = useState(true);
  const [viewport, setViewport] = useState<Viewport>({ zoom: 0, bounds: null });
  const [outlines, setOutlines] = useState<{ count: number; error: string }>({ count: 0, error: "" });
  useEffect(() => { latest.current = { summary, onSelect, onTile }; }, [summary, onSelect, onTile]);
  useEffect(() => {
    if (!host.current || !latest.current.summary.suburb) return;
    maplibregl.setWorkerUrl("/maplibre/maplibre-gl-worker.mjs");
    let m: maplibregl.Map;
    try {
      m = new maplibregl.Map({ container: host.current, bounds: latest.current.summary.suburb.bounds,
        fitBoundsOptions: { padding: 30 }, maxZoom: 20, style: { version: 8, sources: {
          streets: { type: "raster", tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], tileSize: 256, maxzoom: 19, attribution: "© OpenStreetMap contributors" },
          aerial: { type: "raster", tiles: ["https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}"], tileSize: 256, maxzoom: 20, attribution: "Imagery © Google" },
        }, layers: [{ id: "base", type: "background", paint: { "background-color": "#182334" } },
          { id: "streets", type: "raster", source: "streets" }, { id: "aerial", type: "raster", source: "aerial", layout: { visibility: "none" } }] } });
    } catch {
      // Map construction is an external WebGL operation and can fail synchronously.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setError("Map could not start. Use the table to review roofs."); return;
    }
    map.current = m; m.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    m.on("load", () => {
      for (const id of ["coverage", "boundary", "roofs", "pins", "selected"]) m.addSource(id, { type: "geojson", data: empty });
      m.addLayer({ id: "coverage", type: "fill", source: "coverage", paint: { "fill-color": ["match", ["get", "status"], "complete", "#087f8c", "running", "#c58913", "failed", "#df5947", "interrupted", "#df5947", "#64748b"], "fill-opacity": .12, "fill-outline-color": "#64748b" } });
      m.addLayer({ id: "boundary", type: "line", source: "boundary", paint: { "line-color": "#0d5367", "line-width": 2, "line-dasharray": [4, 2] } });
      m.addLayer({ id: "roofs", type: "fill", source: "roofs", paint: { "fill-color": ["get", "color"], "fill-opacity": .45 } });
      m.addLayer({ id: "outlines", type: "line", source: "roofs", paint: { "line-color": ["get", "color"], "line-width": 2 } });
      m.addLayer({ id: "pins", type: "circle", source: "pins", paint: { "circle-color": ["get", "color"], "circle-radius": ["interpolate", ["linear"], ["zoom"], 12, 3, 18, 6], "circle-stroke-color": "#fff", "circle-stroke-width": 1 } });
      m.addLayer({ id: "selected", type: "line", source: "selected", paint: { "line-color": "#fff", "line-width": 4 } });
      setViewport(viewportOf(m));
      setReady(true);
    });
    m.on("moveend", () => setViewport(viewportOf(m)));
    m.on("click", event => {
      if (!m.getLayer("pins")) return;
      const hit = m.queryRenderedFeatures(event.point, { layers: ["pins", "roofs", "coverage"] })[0];
      if (hit?.properties?.key) latest.current.onSelect(hit.properties.key);
      else if (hit?.properties?.scene_id) latest.current.onTile(hit.properties.scene_id);
    });
    m.on("mousemove", e => { if (m.getLayer("pins")) m.getCanvas().style.cursor = m.queryRenderedFeatures(e.point, { layers: ["pins", "roofs", "coverage"] }).length ? "pointer" : ""; });
    const resize = new ResizeObserver(() => m.resize()); resize.observe(host.current);
    return () => { resize.disconnect(); map.current = null; m.remove(); };
  }, []);
  // Coverage grid and boundary come from the summary; they are small and refresh with each poll.
  useEffect(() => {
    const m = map.current; if (!m || !ready) return;
    const set = (id: string, value: FeatureCollection) => (m.getSource(id) as GeoJSONSource).setData(value);
    set("coverage", grid ? summary.suburb?.coverage || empty : empty); set("boundary", summary.suburb?.boundary || empty);
    m.setLayoutProperty("aerial", "visibility", aerial ? "visible" : "none");
    m.setLayoutProperty("streets", "visibility", aerial ? "none" : "visible");
  }, [ready, summary.suburb, grid, aerial]);
  // Outlines for the area in view only, from the bbox-scoped confidence route; pans abort the previous read.
  useEffect(() => {
    const m = map.current; if (!m || !ready || !viewport.bounds) return;
    const set = (id: string, value: FeatureCollection) => (m.getSource(id) as GeoJSONSource).setData(value);
    if (viewport.zoom < OUTLINE_ZOOM) { set("roofs", empty); set("pins", empty); return; }
    const controller = new AbortController();
    const timer = setTimeout(() => {
      fetch(`/api/scenes/confidence?${new URLSearchParams({ bounds: viewport.bounds!.join(","), band })}`, { signal: controller.signal })
        .then(async response => { const result = await response.json(); if (!response.ok) throw new Error(result.error); return result as FeatureCollection<Polygon, Record<string, unknown>>; })
        .then(result => {
          if (controller.signal.aborted || !map.current) return;
          // Only closed polygon outlines can be drawn and centred; anything else in the payload is skipped, not thrown.
          const features = (result.features || [])
            .filter(f => f?.geometry?.type === "Polygon" && (f.geometry.coordinates?.[0]?.length ?? 0) >= 3 && f.properties)
            .map(f => ({ ...f, properties: { ...f.properties, key: f.properties.key || `${f.properties.scene_id}/${f.properties.object_id}` } })) as Feature<Polygon, Record<string, unknown>>[];
          set("roofs", { type: "FeatureCollection", features });
          set("pins", { type: "FeatureCollection", features: features.map(f => ({ ...f, geometry: { type: "Point", coordinates: roofCenter(f as unknown as ProcessingRoofRow) } })) });
          setOutlines({ count: features.length, error: "" });
        })
        .catch(e => { if (!controller.signal.aborted) setOutlines({ count: 0, error: e instanceof Error ? e.message : "Roof outlines could not load." }); });
    }, 250);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [ready, viewport, band]);
  useEffect(() => {
    const m = map.current; if (!m || !ready) return;
    (m.getSource("selected") as GeoJSONSource).setData({ type: "FeatureCollection", features: selected ? [selected] : [] });
  }, [ready, selected]);
  const selectedKey = selected?.properties.key;
  useEffect(() => {
    const m = map.current; if (!m || !ready) return;
    if (selected) m.flyTo({ center: roofCenter(selected), zoom: 19, duration: matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 700 });
    // A refreshed score must not recenter a map the reviewer has panned.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, selectedKey]);
  const zoomedOut = viewport.zoom < OUTLINE_ZOOM;
  return <div className="processing-map-wrap">
    <div className="processing-map" ref={host} role="region" aria-label="Mooroolbark roof confidence map"/>
    <div className="processing-map-tools"><button onClick={() => summary.suburb && map.current?.fitBounds(summary.suburb.bounds, { padding: 30, duration: 0 })}><LocateFixed size={14}/>Full suburb</button>
      <button aria-pressed={aerial} onClick={() => setAerial(v => !v)}>Aerial imagery</button><label><input type="checkbox" checked={grid} onChange={e => setGrid(e.target.checked)}/>Processing tiles</label></div>
    <p className="processing-map-note" role="status">{zoomedOut ? "Zoom in to load roof outlines for the area in view. Select a tile to see its processing status."
      : outlines.error || `${count(outlines.count)} roof outlines in view, model bands. Select a roof to inspect it.`}</p>
    {error && <p className="processing-map-error" role="alert">{error}</p>}
  </div>;
}
