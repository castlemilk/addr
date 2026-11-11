"use client";

import { useEffect, useRef, useState } from "react";
import { Popup, type GeoJSONSource, type Map as MapLibreMap } from "maplibre-gl";
import type { Feature, FeatureCollection, Polygon } from "geojson";

const bands = [
  { id: "low", label: "Low evidence", color: "#df5947" },
  { id: "medium", label: "Mixed evidence", color: "#c58913" },
  { id: "high", label: "Stronger evidence", color: "#087f8c" },
  { id: "unassessed", label: "Not assessed", color: "#64748b" },
] as const;
type Band = typeof bands[number]["id"];
type Roof = Feature<Polygon, {
  scene_id: string; object_id: string; label: string; capture_title: string;
  band: Band; band_label: string; color: string; reasons: string[];
  review_url: string; interval: [number, number] | null; calibration_groups: number;
  /** Same-footprint captures this roof hides on the map (scene_confidence.query). Diagnostic, not accuracy. */
  hidden_overlaps?: { scene_id: string; object_id: string; band: Band; iou: number }[];
  /** Eyes sidecar agreement with a tier-1/2 human reference, when a route supplies it. Not accuracy. */
  eyes?: { reference?: { tier: string; iou: number | null } };
}>;
const bandLabel = (band: Band) => bands.find(b => b.id === band)?.label.toLowerCase() || band;
/** Popup lines about other captures and human references. Wording keeps model estimates apart from accuracy. */
export function otherCaptureLines(props: Roof["properties"]): string[] {
  const lines = (props.hidden_overlaps || []).map(o => `Also seen as ${bandLabel(o.band)} in ${o.scene_id} (IoU ${o.iou.toFixed(2)}) · hidden on this map`);
  const reference = props.eyes?.reference;
  if (reference && reference.iou != null) lines.push(`Human reference IoU ${reference.iou.toFixed(2)} (${reference.tier}) · reference agreement, not accuracy`);
  return lines;
}
type ConfidenceData = FeatureCollection<Polygon, Roof["properties"]> & {
  counts: Record<Band, number>; duplicates: number;
  calibration: { eligible_validation_layers: number; assessed_at?: string };
  suburb: { status: string; total: number; counts: Record<string, number>; bounds: [number, number, number, number];
    boundary: FeatureCollection; coverage: FeatureCollection; updated_at: number } | null;
};
const empty: FeatureCollection = { type: "FeatureCollection", features: [] };
const sourceIds = ["roof-confidence", "roof-confidence-pins", "roof-confidence-coverage", "roof-confidence-boundary"];
const layerIds = ["roof-confidence-grid", "roof-confidence-boundary-line", "roof-confidence-fill", "roof-confidence-line", "roof-confidence-dots"];

function center(roof: Roof): [number, number] {
  const ring = roof.geometry.coordinates[0];
  return [(Math.min(...ring.map(p => p[0])) + Math.max(...ring.map(p => p[0]))) / 2,
    (Math.min(...ring.map(p => p[1])) + Math.max(...ring.map(p => p[1]))) / 2];
}

export default function RoofConfidenceMap({ map, enabled, onEnabled, interactive }: {
  map: MapLibreMap; enabled: boolean; onEnabled: (value: boolean) => void; interactive: boolean;
}) {
  const [data, setData] = useState<ConfidenceData | null>(null);
  const [band, setBand] = useState<Band | "all">("all"), [coverage, setCoverage] = useState(true);
  const [pending, setPending] = useState(false), [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const popup = useRef<Popup | null>(null), previous = useRef("");
  const latestData = useRef(data);
  useEffect(() => { latestData.current = data; }, [data]);
  useEffect(() => {
    if (!enabled) return;
    let controller: AbortController | undefined, debounce: ReturnType<typeof setTimeout>;
    const load = () => {
      clearTimeout(debounce); controller?.abort();
      debounce = setTimeout(() => {
        const request = new AbortController(); controller = request;
        const bounds = map.getBounds(); setPending(true); setError("");
        const query = new URLSearchParams({ bounds: [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()].join(",") });
        void fetch(`/api/scenes/confidence?${query}`, { signal: request.signal, cache: "no-store" })
          .then(async response => { const value = await response.json(); if (!response.ok) throw new Error(value.error); if (!request.signal.aborted) setData(value); })
          .catch(err => { if (!request.signal.aborted) setError(err.message); })
          .finally(() => { if (!request.signal.aborted) setPending(false); });
      }, 250);
    };
    const interval = setInterval(load, 30000);
    map.on("moveend", load); window.addEventListener("focus", load); load();
    return () => { clearInterval(interval); clearTimeout(debounce); controller?.abort(); map.off("moveend", load); window.removeEventListener("focus", load); };
  }, [map, enabled, refresh]);

  useEffect(() => {
    let installed = false;
    const apply = () => {
      if (!map.isStyleLoaded()) return;
      if (!map.getSource("roof-confidence")) {
        sourceIds.forEach(id => map.addSource(id, { type: "geojson", data: empty }));
        map.addLayer({ id: layerIds[0], type: "fill", source: sourceIds[2], paint: {
          "fill-color": ["match", ["get", "status"], "complete", "#087f8c", "failed", "#df5947", "running", "#c58913", "#64748b"],
          "fill-opacity": .08, "fill-outline-color": "#64748b",
        } });
        map.addLayer({ id: layerIds[1], type: "line", source: sourceIds[3], paint: { "line-color": "#075985", "line-width": 2, "line-dasharray": [4, 2] } });
        map.addLayer({ id: layerIds[2], type: "fill", source: sourceIds[0], paint: { "fill-color": ["get", "color"], "fill-opacity": .32 } });
        map.addLayer({ id: layerIds[3], type: "line", source: sourceIds[0], paint: { "line-color": ["get", "color"], "line-width": 2.5 } });
        // Markers stay legible above the existing 3D roofs at any pitch.
        map.addLayer({ id: layerIds[4], type: "circle", source: sourceIds[1], paint: {
          "circle-color": ["get", "color"], "circle-radius": ["interpolate", ["linear"], ["zoom"], 12, 2.5, 18, 6],
          "circle-stroke-color": "white", "circle-stroke-width": 1.5,
        } });
      }
      const features = enabled ? (data?.features || []).filter(f => band === "all" || f.properties.band === band) : [];
      (map.getSource(sourceIds[0]) as GeoJSONSource).setData({ type: "FeatureCollection", features });
      (map.getSource(sourceIds[1]) as GeoJSONSource).setData({ type: "FeatureCollection", features: features.map(f => ({ ...f, geometry: { type: "Point", coordinates: center(f) } })) });
      (map.getSource(sourceIds[2]) as GeoJSONSource).setData(enabled && coverage && data?.suburb ? data.suburb.coverage : empty);
      (map.getSource(sourceIds[3]) as GeoJSONSource).setData(enabled && data?.suburb ? data.suburb.boundary : empty);
      installed = true;
    };
    const ready = () => {
      if (!installed) apply();
      if (enabled && map.getLayer(layerIds[4]) && map.getStyle().layers?.at(-1)?.id !== layerIds[4]) map.moveLayer(layerIds[4]);
    };
    const restyle = () => { installed = false; apply(); };
    map.on("idle", ready); map.on("style.load", restyle); apply();
    return () => { map.off("idle", ready); map.off("style.load", restyle); };
  }, [map, data, enabled, band, coverage]);

  function openRoof(roof: Roof) {
    const props = roof.properties;
    const content = document.createElement("div"), title = document.createElement("strong"), status = document.createElement("p"), reasons = document.createElement("ul"), link = document.createElement("a");
    content.style.cssText = "font:12px system-ui;line-height:1.6;color:#172033;max-width:280px";
    title.textContent = props.label;
    status.textContent = `${props.band_label} · model estimate`;
    const capture = document.createElement("p");
    capture.textContent = `Image context: ${props.capture_title}`;
    props.reasons.forEach(reason => { const item = document.createElement("li"); item.textContent = reason; reasons.append(item); });
    const interval = document.createElement("p");
    interval.textContent = props.interval
      ? `95% boundary-success interval for this band: ${props.interval.map(v => Math.round(v * 100)).join("–")}% (${props.calibration_groups} validation areas). Not per-house or 3D accuracy.`
      : "No calibrated accuracy interval yet. Review the imagery and roof lines.";
    const others = document.createElement("ul");
    others.style.cssText = "margin:6px 0 0;padding-left:14px;color:#475569";
    otherCaptureLines(props).forEach(line => { const item = document.createElement("li"); item.textContent = line; others.append(item); });
    link.textContent = "Review this roof →"; link.href = props.review_url;
    link.style.cssText = "display:block;margin-top:10px;color:#075985;text-decoration:underline;font-weight:600";
    content.append(title, status, capture, reasons, interval, ...(others.childElementCount ? [others] : []), link);
    popup.current?.remove(); popup.current = new Popup({ offset: 12 }).setLngLat(center(roof)).setDOMContent(content).addTo(map);
  }
  const openRef = useRef(openRoof);
  useEffect(() => { openRef.current = openRoof; });
  useEffect(() => {
    popup.current?.remove();
    const select = (event: import("maplibre-gl").MapMouseEvent) => {
      if (!enabled || !interactive || !map.getLayer(layerIds[4])) return;
      const hit = map.queryRenderedFeatures(event.point, { layers: [layerIds[4], layerIds[2]] })[0];
      const roof = latestData.current?.features.find(f => f.properties.scene_id === hit?.properties?.scene_id && f.properties.object_id === hit?.properties?.object_id);
      if (roof) openRef.current(roof);
    };
    map.on("click", select);
    return () => { map.off("click", select); popup.current?.remove(); };
  }, [map, enabled, interactive, band]);
  useEffect(() => () => {
    layerIds.toReversed().forEach(id => { if (map.getLayer(id)) map.removeLayer(id); });
    sourceIds.forEach(id => { if (map.getSource(id)) map.removeSource(id); });
  }, [map]);

  const roofs = (data?.features || []).filter(f => band === "all" || f.properties.band === band)
    .toSorted((a, b) => bands.findIndex(v => v.id === a.properties.band) - bands.findIndex(v => v.id === b.properties.band));
  const suburb = data?.suburb;
  return <section className="border-t border-slate-200 pt-2" aria-label="Roof confidence">
    <label className="flex items-center gap-2 font-semibold"><input type="checkbox" checked={enabled} onChange={e => onEnabled(e.target.checked)}/>Roof confidence</label>
    {enabled && <div className="mt-2 space-y-2">
      <p className="text-[11px] leading-relaxed text-slate-600">Model evidence from geometry and image checks. These bands are not accuracy percentages.</p>
      <div className="grid grid-cols-2 gap-1" role="group" aria-label="Filter roof confidence">
        <button aria-pressed={band === "all"} onClick={() => setBand("all")} className={`col-span-2 rounded border px-2 py-1.5 text-left ${band === "all" ? "border-slate-800 bg-slate-100" : "border-slate-200"}`}>All assessed roofs <span className="float-right tabular-nums">{data?.features.length ?? "—"}</span></button>
        {bands.map(b => <button key={b.id} aria-pressed={band === b.id} onClick={() => setBand(b.id)} className={`flex items-center gap-1.5 rounded border px-2 py-1.5 text-left text-[10px] ${band === b.id ? "border-slate-800 bg-slate-100" : "border-slate-200"}`}><span className="h-2 w-2 shrink-0 rounded-full" style={{ background: b.color }}/>{b.label}<span className="ml-auto tabular-nums">{data?.counts[b.id] ?? "—"}</span></button>)}
      </div>
      <p role="status" className="text-[10px] text-slate-600">{pending ? "Updating confidence…" : `${roofs.length} roofs in this view · click an outline or dot to review`}</p>
      {error && <p role="alert" className="text-[11px] text-red-700">{error} <button className="underline" onClick={() => setRefresh(n => n + 1)}>Retry</button></p>}
      <button disabled={!roofs.length} className="w-full rounded bg-slate-800 px-2 py-2 font-medium text-white disabled:opacity-40" onClick={() => {
        const index = roofs.findIndex(f => `${f.properties.scene_id}/${f.properties.object_id}` === previous.current);
        const roof = roofs[(index + 1) % roofs.length]; previous.current = `${roof.properties.scene_id}/${roof.properties.object_id}`;
        map.flyTo({ center: center(roof), zoom: 19, pitch: 0, offset: [map.getCanvas().clientWidth > 640 ? 150 : 0, 0] }); openRoof(roof);
      }}>Next roof to review →</button>
      {suburb && <div className="space-y-1 border-t border-slate-200 pt-2 text-[11px]">
        <div className="flex justify-between font-medium"><span>Mooroolbark coverage</span><span>{suburb.counts.complete || 0} / {suburb.total} tiles</span></div>
        <progress aria-label="Mooroolbark analysis progress" className="h-1.5 w-full accent-teal-700" value={suburb.counts.complete || 0} max={suburb.total}/>
        <p className="text-slate-600">{suburb.status.replaceAll("_", " ")}{suburb.counts.failed ? ` · ${suburb.counts.failed} tiles need retry` : ""}. Updates every 30 seconds.</p>
        <div className="flex items-center justify-between"><label className="flex items-center gap-1"><input type="checkbox" checked={coverage} onChange={e => setCoverage(e.target.checked)}/>Coverage grid</label><button className="underline underline-offset-2" onClick={() => map.fitBounds(suburb.bounds, { padding: { left: map.getCanvas().clientWidth > 640 ? 335 : 24, right: 24, top: 70, bottom: 80 }, pitch: 0 })}>Show suburb</button></div>
        {coverage && <p className="text-[10px] text-slate-500">Grid: grey pending · gold running · teal complete · red failed</p>}
      </div>}
      <p className="text-[10px] leading-relaxed text-slate-500">{data?.calibration.eligible_validation_layers ? "Accuracy intervals use reviewed validation areas where enough independent samples exist." : "Accuracy intervals await fully reviewed validation roofs."} Saved edits remain separate.</p>
    </div>}
  </section>;
}
