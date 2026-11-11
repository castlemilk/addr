import { useEffect, useMemo, useRef, useState } from "react";
import type { Feature, FeatureCollection, MultiPolygon, Polygon } from "geojson";
import type { Map as LeafletMap } from "leaflet";
import { BarChart3, FileSearch, Layers3, Map as MapIcon, PackageOpen } from "lucide-react";
import { asAsset, useAssetJson, type AssetRef, type NamedAsset } from "../../lib/annotations/assets";
import type { AnnotationEditorProps } from "./AnnotationCanvas";
import { AssetCollection, AssetPreview, SourceState, StructuredData } from "./SourceData";

type RoofProperties = { key: string; scene_id: string; object_id: string; band: string; label: string; review_status?: string; area_m2?: number; detector_score?: number; hidden_overlaps?: Array<{ scene_id: string; object_id: string; band: string; iou?: number }>; [key: string]: unknown };
type RoofFeature = Feature<Polygon | MultiPolygon, RoofProperties>;
type RoofCollection = FeatureCollection<Polygon | MultiPolygon, RoofProperties>;
interface Tile { id: string; order: number; status: string; objects?: Record<string, number>; confidence?: Record<string, number>; elapsed_seconds?: number; attempts?: number; error?: string | null }
interface Summary {
  counts?: Record<string, number>; featureCount?: number; fetched_at?: number; duplicates?: number;
  objects?: Record<string, number>; calibration?: { eligible_validation_layers?: number; minimum_groups?: number; target?: string; method?: string };
  queue?: Tile[]; suburb?: { status: string; total: number; bounds?: number[]; boundary?: FeatureCollection; coverage?: FeatureCollection; counts?: Record<string, number>; updated_at?: number; run?: string };
  scanSummary?: { counts?: Record<string, number>; objects?: Record<string, number> }; address_counts?: Record<string, number>;
}
const bandColors: Record<string, string> = { low: "#d97706", medium: "#628bb8", high: "#087f8c", unassessed: "#94a3b8" };
const bandNames: Record<string, string> = { low: "Needs closer review", medium: "Mixed evidence", high: "Stronger evidence", unassessed: "Unassessed" };
const count = (value?: number) => value === undefined ? "Not recorded" : value.toLocaleString();

export default function AddrReportEditor(props: AnnotationEditorProps) {
  const { item, viewState = {}, onViewStateChange } = props;
  const tab = String(viewState.reportTab || "overview");
  const source = item.source;
  const summary = useAssetJson<Summary>(asAsset(source.summary));
  const setTab = (reportTab: string) => onViewStateChange?.({ ...viewState, reportTab });
  const navigate = (scene: string, object?: string) => props.onNavigateItem?.((item.source.sceneItems as Record<string, string> | undefined)?.[scene] || `addr-scene-${scene}`, object);
  return <div className="fw-report-editor"><div className="fw-scene-controls"><div className="fw-segmented" aria-label="Dataset report view">{[{ id: "overview", name: "Overview", Icon: BarChart3 }, { id: "map", name: "Roof map", Icon: MapIcon }, { id: "processing", name: "Processing", Icon: Layers3 }, { id: "analysis", name: "Analysis", Icon: FileSearch }, { id: "archive", name: "Source inventory", Icon: PackageOpen }].map(({ id, name, Icon }) => <button key={id} aria-pressed={tab === id} onClick={() => setTab(id)}><Icon size={15} /><span>{name}</span></button>)}</div></div>
    {summary.error ? <SourceState error={summary.error} /> : !summary.data ? <SourceState /> : tab === "map" ? <ReportMap source={asAsset(source.map)} summary={summary.data} onNavigate={navigate} overview={asAsset(source.overview)} />
      : tab === "processing" ? <ProcessingTable summary={summary.data} onNavigate={navigate} />
      : tab === "overview" ? <div className="fw-rich-scroll"><section className="fw-source-intro"><div className="fw-overline">Frozen local state</div><h2>Mooroolbark, in context</h2><p>Scenes, source feedback and analysis from the preserved local snapshot. These counts describe different populations; they are not added together.</p></section><div className="fw-report-metrics"><Metric label="Combined roof inventory" value={count(summary.data.featureCount)} detail="Geographic inventory after canonical overlap handling" /><Metric label="Completed scan tiles" value={count(summary.data.suburb?.total)} detail={`Processing state: ${summary.data.suburb?.status || "not recorded"}`} /><Metric label="Eligible validation layers" value={count(summary.data.calibration?.eligible_validation_layers)} detail="Source calibration eligibility · no accuracy claim" /></div><section className="fw-report-section"><h3>Combined roof evidence</h3><div className="fw-band-cards">{Object.entries(summary.data.counts || {}).map(([band, amount]) => <button key={band} onClick={() => setTab("map")}><i style={{ background: bandColors[band] }} /><strong>{count(amount)}</strong><span>{bandNames[band] || band}</span><small>{band} band</small></button>)}</div><p className="fw-field-help">Evidence bands describe available signals. A confirmed object can have a machine actor; status alone does not establish human review.</p></section><section className="fw-report-section"><h3>Scan ownership population</h3><div className="fw-report-metrics fw-compact-metrics">{Object.entries(summary.data.objects || {}).map(([kind, amount]) => <Metric key={kind} label={kind.replaceAll("_", " ")} value={count(amount)} detail="Objects owned by the 257-tile scan" />)}</div><StructuredData value={summary.data.scanSummary?.counts} label="Scan-only confidence counts" /><p className="fw-field-help">The scan ownership population differs from the combined inventory, which includes overlapping captures and prior scenes. Hidden alternatives stay attached to their retained roof.</p></section><section className="fw-report-section"><h3>Calibration and source scope</h3><StructuredData value={summary.data.calibration} label="Calibration snapshot" /><StructuredData value={summary.data.address_counts} label="Address attribution counts" /><StructuredData value={item.metadata} label="Import scope, exclusions and parity" /></section></div>
      : tab === "analysis" ? <div className="fw-rich-scroll"><section className="fw-source-intro"><h2>Processing and review evidence</h2><p>Inspect retained findings, overlap relationships, calibration, and diagnostics. Loading detailed evidence is explicit because the full report is larger than the map.</p></section>{asAsset(source.overview) && <DeferredAsset title="Detailed roof evidence, hidden overlaps and eyes sidecars" asset={asAsset(source.overview)!} />}<AssetCollection assets={(source.analysisRefs || []) as NamedAsset[]} title="Self-review and analysis snapshots" />{asAsset(source.crm) && <DeferredAsset title="Original address metadata, features and POIs" asset={asAsset(source.crm)!} />}<StructuredData value={summary.data} label="Complete processing summary" /></div>
      : <div className="fw-rich-scroll"><section className="fw-source-intro"><h2>Preserved source inventory</h2><p>Content-addressed files and ordered chunks retain the original bytes. Opening an asset checks its hash. Source state stays separate from new Fieldwork feedback.</p></section>{asAsset(source.manifest) ? <ManifestInventory asset={asAsset(source.manifest)!} /> : <StructuredData value={item.metadata} label="Available import metadata" />}</div>}
  </div>;
}
function Metric({ label, value, detail }: { label: string; value: string; detail: string }) { return <article className="fw-report-metric"><span>{label}</span><strong>{value}</strong><small>{detail}</small></article>; }
function DeferredAsset({ title, asset }: { title: string; asset: AssetRef }) {
  const [open, setOpen] = useState(false);
  return <section className="fw-report-section"><h3>{title}</h3>{open ? <AssetPreview asset={asset} /> : <button className="fw-button" onClick={() => setOpen(true)}>Load stored evidence{asset.bytes ? ` · ${(asset.bytes / 1024 / 1024).toFixed(1)} MB` : ""}</button>}</section>;
}
function RoofEvidence({ asset, roofKey, onNavigate }: { asset?: AssetRef; roofKey: string; onNavigate: (scene: string, object?: string) => void }) {
  const [open, setOpen] = useState(false);
  const report = useAssetJson<RoofCollection>(open ? asset : undefined);
  const feature = report.data?.features.find((entry) => entry.properties.key === roofKey);
  if (!asset) return null;
  return <div className="fw-roof-evidence">{!open ? <button className="fw-text-button" onClick={() => setOpen(true)}>Load this roof’s detailed evidence and overlap alternatives · {((asset.bytes || 0) / 1024 / 1024).toFixed(1)} MB</button> : !report.data ? <SourceState error={report.error} /> : feature ? <><StructuredData value={feature.properties} label="Exact roof evidence, calibration and diagnostics" />{feature.properties.hidden_overlaps?.map((overlap) => <button className="fw-button" key={`${overlap.scene_id}/${overlap.object_id}`} onClick={() => onNavigate(overlap.scene_id, overlap.object_id)}>Open {overlap.band} overlap · IoU {overlap.iou ?? "not recorded"}</button>)}</> : <p className="fw-field-help">This roof is absent from the detailed inventory; the map record remains preserved.</p>}</div>;
}
function ManifestInventory({ asset }: { asset: AssetRef }) {
  const state = useAssetJson<{ files?: Array<AssetRef & { path: string }>; [key: string]: unknown }>(asset);
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(80);
  const [selected, setSelected] = useState<(AssetRef & { path: string }) | null>(null);
  if (!state.data) return <SourceState error={state.error} />;
  const files = (state.data.files || []).filter((file) => file.path.toLowerCase().includes(query.toLowerCase()));
  const { files: _files, ...metadata } = state.data;
  void _files;
  return <><StructuredData value={metadata} label="Snapshot metadata and exclusions" /><div className="fw-report-filters"><input aria-label="Find preserved source file" value={query} placeholder="Find self-review, analysis, image, report…" onChange={(event) => { setQuery(event.target.value); setLimit(80); }} /><span>{files.length.toLocaleString()} files</span></div><div className="fw-asset-list fw-manifest-list">{files.slice(0, limit).map((file) => <button key={file.path} className={selected?.path === file.path ? "is-active" : ""} onClick={() => setSelected(file)}><span>{file.path}</span><small>{((file.bytes || 0) / 1024).toFixed(1)} KB</small></button>)}{files.length > limit && <button onClick={() => setLimit(limit + 100)}>Show more files</button>}</div>{selected && <section className="fw-report-section"><h3 className="fw-source-path">{selected.path}</h3><AssetPreview key={selected.sha256} asset={selected} /></section>}</>;
}
function ProcessingTable({ summary, onNavigate }: { summary: Summary; onNavigate: (scene: string) => void }) {
  const [query, setQuery] = useState(""); const [status, setStatus] = useState("all"); const [limit, setLimit] = useState(50);
  const tiles = (summary.queue || []).filter((tile) => (status === "all" || tile.status === status) && tile.id.toLowerCase().includes(query.toLowerCase()));
  return <div className="fw-rich-scroll"><section className="fw-source-intro"><h2>Processing snapshot</h2><p>{summary.suburb?.run} · {summary.suburb?.status}. The imported queue is historical; no workers run from this screen.</p></section><div className="fw-report-filters"><input aria-label="Find processing tile" placeholder="Find scene / tile…" value={query} onChange={(event) => { setQuery(event.target.value); setLimit(50); }} /><select aria-label="Processing status" value={status} onChange={(event) => { setStatus(event.target.value); setLimit(50); }}><option value="all">All processing states</option>{[...new Set((summary.queue || []).map((tile) => tile.status))].map((value) => <option key={value}>{value}</option>)}</select><span>{tiles.length} tiles</span></div><div className="fw-report-table-wrap"><table className="fw-report-table"><thead><tr><th>Scene</th><th>State</th><th>Roofs</th><th>Attempts</th><th>Elapsed</th><th /></tr></thead><tbody>{tiles.slice(0, limit).map((tile) => <tr key={tile.id}><td><strong>{tile.id}</strong>{tile.error && <small>{tile.error}</small>}</td><td>{tile.status}</td><td>{tile.objects?.building ?? "—"}</td><td>{tile.attempts ?? "—"}</td><td>{tile.elapsed_seconds ? `${tile.elapsed_seconds.toFixed(1)} s` : "—"}</td><td><button className="fw-text-button" onClick={() => onNavigate(tile.id)}>Open scene</button></td></tr>)}</tbody></table></div>{tiles.length > limit && <button className="fw-button" onClick={() => setLimit(limit + 100)}>Show more tiles</button>}</div>;
}
function ReportMap({ source, summary, onNavigate, overview }: { source?: AssetRef; summary: Summary; onNavigate: (scene: string, object?: string) => void; overview?: AssetRef }) {
  const data = useAssetJson<RoofCollection>(source);
  const [band, setBand] = useState("all"), [status, setStatus] = useState("all"), [query, setQuery] = useState("");
  const [selected, setSelected] = useState<RoofFeature | null>(null);
  const [limit, setLimit] = useState(30);
  const features = useMemo(() => (data.data?.features || []).filter((feature) => {
    const p = feature.properties; return (band === "all" || p.band === band) && (status === "all" || p.review_status === status) && `${p.label} ${p.scene_id} ${p.object_id}`.toLowerCase().includes(query.toLowerCase());
  }), [data.data, band, status, query]);
  if (!source || !data.data) return <SourceState error={!source ? "No protected map asset is attached." : data.error} label="Loading and verifying the roof map…" />;
  return <div className="fw-report-map-view"><div className="fw-report-filters"><select aria-label="Evidence band" value={band} onChange={(event) => { setBand(event.target.value); setLimit(30); }}><option value="all">All evidence bands</option>{["low", "medium", "high", "unassessed"].map((value) => <option value={value} key={value}>{value} · {summary.counts?.[value] ?? 0}</option>)}</select><select aria-label="Source object status" value={status} onChange={(event) => { setStatus(event.target.value); setLimit(30); }}><option value="all">All source statuses</option>{[...new Set(data.data.features.map((feature) => feature.properties.review_status || "unreviewed"))].map((value) => <option key={value}>{value}</option>)}</select><input aria-label="Find roof" placeholder="Find roof or scene…" value={query} onChange={(event) => { setQuery(event.target.value); setLimit(30); }} /><span>{features.length.toLocaleString()} / {data.data.features.length.toLocaleString()} roofs</span></div><GeographicRoofs features={features} summary={summary} selected={selected?.properties.key} onSelect={setSelected} /><div className="fw-report-map-bottom">{selected ? <article className="fw-map-selection"><div><strong>{selected.properties.label}</strong><p>{selected.properties.scene_id} · {selected.properties.object_id}</p><span style={{ color: bandColors[selected.properties.band] }}>{selected.properties.band} evidence</span> · source status: {selected.properties.review_status || "not recorded"}</div><button className="fw-button fw-primary" onClick={() => onNavigate(selected.properties.scene_id, selected.properties.object_id)}>Open exact object</button><button className="fw-text-button" onClick={() => setSelected(null)}>Show roof list</button><StructuredData value={selected.properties} label="Map record" /><RoofEvidence key={selected.properties.key} asset={overview} roofKey={selected.properties.key} onNavigate={onNavigate} /></article> : <><div className="fw-map-roof-list">{features.slice(0, limit).map((feature) => <button key={feature.properties.key} onClick={() => setSelected(feature)}><i style={{ background: bandColors[feature.properties.band] }} /><span>{feature.properties.label}<small>{feature.properties.scene_id}</small></span><small>{feature.properties.band}</small></button>)}</div>{features.length > limit && <button className="fw-text-button" onClick={() => setLimit(limit + 50)}>Show next roofs</button>}</>}</div></div>;
}
function GeographicRoofs({ features, summary, selected, onSelect }: { features: RoofFeature[]; summary: Summary; selected?: string; onSelect: (feature: RoofFeature) => void }) {
  const host = useRef<HTMLDivElement>(null);
  const [map, setMap] = useState<LeafletMap | null>(null);
  const [error, setError] = useState("");
  const onSelectRef = useRef(onSelect);
  useEffect(() => { onSelectRef.current = onSelect; }, [onSelect]);
  useEffect(() => {
    let cancelled = false, instance: LeafletMap | undefined;
    import("leaflet").then((leaflet) => {
      if (!host.current || cancelled) return;
      instance = leaflet.map(host.current, { preferCanvas: true, zoomControl: true, minZoom: 2, maxZoom: 21 });
      const bounds = summary.suburb?.bounds;
      if (bounds?.length === 4) instance.fitBounds([[bounds[1], bounds[0]], [bounds[3], bounds[2]]]); else instance.setView([0, 0], 2);
      leaflet.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 21, maxNativeZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>' }).addTo(instance).on("tileerror", () => setError("Background tiles unavailable. Imported roof geometry remains visible."));
      if (summary.suburb?.boundary) leaflet.geoJSON(summary.suburb.boundary, { interactive: false, style: { color: "#334155", weight: 1.5, fillOpacity: 0 } }).addTo(instance);
      setMap(instance);
    }).catch(() => setError("The map renderer could not start."));
    return () => { cancelled = true; instance?.remove(); };
  }, [summary]);
  useEffect(() => {
    if (!map) return;
    let layer: ReturnType<typeof import("leaflet").geoJSON> | undefined, cancelled = false;
    import("leaflet").then((leaflet) => {
      if (cancelled) return;
      const collection: RoofCollection = { type: "FeatureCollection", features };
      layer = leaflet.geoJSON(collection, { style: (feature) => ({ color: feature?.properties.key === selected ? "#1d4ed8" : bandColors[String(feature?.properties.band)] || "#94a3b8", weight: feature?.properties.key === selected ? 3 : 1, fillOpacity: .28 }), onEachFeature: (feature, polygon) => polygon.on("click", () => onSelectRef.current(feature as RoofFeature)) }).addTo(map);
      if (selected) { const feature = features.find((feature) => feature.properties.key === selected); if (feature) { const box = leaflet.geoJSON(feature).getBounds(); if (!map.getBounds().contains(box)) map.fitBounds(box.pad(2), { maxZoom: 19 }); } }
    });
    return () => { cancelled = true; layer?.remove(); };
  }, [map, features, selected]);
  useEffect(() => { if (!map || !host.current) return; const observer = new ResizeObserver(() => map.invalidateSize()); observer.observe(host.current); return () => observer.disconnect(); }, [map]);
  return <div className="fw-report-map"><div ref={host} />{error && <p className="fw-scene-warning" role="status">{error}</p>}<div className="fw-map-band-legend">{["low", "medium", "high"].map((band) => <span key={band}><i style={{ background: bandColors[band] }} />{band}</span>)}</div></div>;
}
export function AddrReportInspector({ item }: AnnotationEditorProps) {
  const summary = useAssetJson<Summary>(asAsset(item.source.summary));
  return <div className="fw-scene-inspector"><section className="fw-inspector-section"><h3>Dataset snapshot</h3><p className="fw-field-help">Immutable source state, with a separate Fieldwork feedback layer on each scene.</p><dl className="fw-facts"><dt>Combined roofs</dt><dd>{count(summary.data?.featureCount)}</dd><dt>Scan tiles</dt><dd>{count(summary.data?.suburb?.total)}</dd><dt>Calibration layers</dt><dd>{count(summary.data?.calibration?.eligible_validation_layers)}</dd><dt>Source timestamp</dt><dd>{summary.data?.fetched_at ? new Date(summary.data.fetched_at * 1000).toLocaleString() : "Not recorded"}</dd></dl></section><section className="fw-inspector-section"><h3>Reading the evidence</h3><p className="fw-field-help">Combined inventory and scan ownership counts have different scopes. Overlapping captures retain their provenance. A model score is not calibrated accuracy; null intervals remain unavailable.</p><p className="fw-field-help">Source review status does not identify the reviewer. Each scene shows its saved actor and object provenance so machine decisions and human feedback remain distinguishable.</p></section><section className="fw-inspector-section"><StructuredData value={item.metadata} label="Scope and import provenance" /></section></div>;
}
