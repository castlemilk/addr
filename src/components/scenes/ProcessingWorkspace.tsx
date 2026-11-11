"use client";
import "@/app/reviews/reviews.css";
import BuildingAddressSummary from "./BuildingAddressSummary";
import { buildingAddressTitle, addressStatus } from "@/lib/building-addresses";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import dynamic from "next/dynamic";
import { Activity, ArrowDown, ArrowRight, ArrowUp, ArrowUpDown, Check, ChevronLeft, ChevronRight, Clock3, Layers, ListOrdered, LoaderCircle, Map as MapIcon, RefreshCw, Search, Table2, X } from "lucide-react";
import { count, duration, EVIDENCE_BANDS, reviewLabels, type EvidenceBand, type ProcessingRoof, type ProcessingRoofRow, type ProcessingRoofsPage, type ProcessingSummary, type ProcessingTile } from "@/lib/scene-processing";
import { parseSort, ROOF_PAGE_SIZE, type RoofSort, type RoofSortKey } from "@/lib/scene-processing-slice";
import { WorkbenchHeader } from "@/components/shell/WorkbenchHeader";
import ProcessingPreview from "./ProcessingPreview";
import SelfReviewPanel from "./SelfReviewPanel";

const ProcessingMap = dynamic(() => import("./ProcessingMap"), { ssr: false, loading: () => <div className="processing-empty">Loading the coverage map</div> });
const statusNames: Record<string, string> = { running: "Processing", complete: "Complete", pending: "Queued", failed: "Failed", interrupted: "Interrupted", planned: "Ready", needs_retry: "Needs retry" };
const addressFilters: [string, string][] = [["matched", "Property / unit match"], ["linked", "Saved address"], ["shared", "Mapped shared roof"], ["reviewed", "Reviewer resolved"], ["ambiguous", "Needs address review"], ["unmatched", "Unresolved address"]];
const views = ["table", "map", "queue", "self-review"];
const endpoint = (params: Record<string, string>) => `/api/scenes/processing?${new URLSearchParams(params)}`;
async function read<T>(url: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(url, { cache: "no-store", signal });
  // A crashed route or a proxy can answer with HTML; that must read as a feed error, never as a JSON parse trace.
  const result: (T & { error?: string }) | null = await response.json().catch(() => null);
  if (!response.ok) throw Object.assign(new Error(result?.error || `The processing feed answered ${response.status}.`), { status: response.status });
  if (result == null) throw new Error("The processing feed returned an unreadable response.");
  return result;
}

function Evidence({ roof }: { roof: ProcessingRoofRow }) {
  return <span className={`processing-evidence band-${roof.properties.band || "unassessed"}`}><i/>{roof.properties.band_label || "Not assessed"}</span>;
}
const bandName = (band: EvidenceBand | string | null | undefined) => (band && EVIDENCE_BANDS.find(b => b.id === band)?.label) || band || "unknown band";
const metres = (v: number | null | undefined, unit = " m") => v == null ? "—" : `${v.toFixed(2)}${unit}`;
const ratio = (v: number | null | undefined) => v == null ? "—" : v.toFixed(2);
const yesNo = (v: boolean | null | undefined) => v == null ? "—" : v ? "Yes" : "No";
const percent = (v: number | null | undefined) => v == null ? "—" : `${Math.round(v * 100)}%`;
const imageryName = (identity: string | null | undefined) => identity === "same_image" ? "same imagery" : identity === "different_image" ? "different imagery" : "imagery unknown";

/** Eyes sidecar groups: model evidence, human-reference agreement and other captures. Diagnostics, never accuracy or labels. */
export function EyesGroups({ roof }: { roof: ProcessingRoofRow }) {
  const p = roof.properties, eyes = p.eyes || {}, diagnostics = eyes.diagnostics, reference = eyes.reference, cross = eyes.cross_capture;
  const seen = new Set<string>();
  const others = [
    ...(p.hidden_overlaps || []).map(o => ({ key: `${o.scene_id}/${o.object_id}`, scene_id: o.scene_id, band: o.band, iou: o.iou, note: "hidden on the coverage map under this roof" })),
    ...(cross?.bands || []).map(o => ({ key: `${o.scene_id}/${o.object_id}`, scene_id: o.scene_id, band: o.band, iou: o.iou, note: cross?.hidden_by.includes(`${o.scene_id}/${o.object_id}`) ? "hides this roof on the coverage map" : "eyes index cross-capture pair" })),
  ].filter(o => !seen.has(o.key) && seen.add(o.key));
  const evidenceSummary = [
    p.detector_score == null ? "No detector score" : `Detector ${ratio(p.detector_score)}`,
    p.observed_segments ? `${p.observed_segments} ${p.observed_segments === 1 ? "line" : "lines"} observed` : "no lines observed",
    p.image_line_mismatch_display == null ? null : `mismatch ${metres(p.image_line_mismatch_display)}`,
  ].filter(Boolean).join(", ");
  const referenceSummary = reference
    ? `IoU ${ratio(reference.iou)} with a tier ${reference.tier.slice(1)} human reference, ${imageryName(reference.image_identity)}`
    : "No tier 1 or tier 2 human reference is recorded for this roof";
  const othersSummary = others.length
    ? `Also seen as ${others[0].band || "an unknown band"} in ${others[0].scene_id}${others.length > 1 ? ` and ${others.length - 1} more ${others.length > 2 ? "captures" : "capture"}` : ""}`
    : "No overlapping capture is recorded for this roof";
  return <>
    <section className="processing-inspector-section processing-eyes-group" aria-labelledby="eyes-model"><h3 id="eyes-model">Model evidence</h3>
      <p className="processing-group-summary">{evidenceSummary}</p>
      <dl><div><dt>Detector score</dt><dd>{ratio(p.detector_score)}</dd></div>
        <div><dt>Lines observed</dt><dd>{p.observed_segments ?? "—"}</dd></div>
        <div><dt>Line mismatch</dt><dd>{p.image_line_mismatch_display == null ? "no lines observed" : metres(p.image_line_mismatch_display)}</dd></div>
        <div><dt>Clipped by image edge</dt><dd>{yesNo(p.clipped)}</dd></div>
        {eyes.shadow_band_v2 && eyes.shadow_band_v2 !== p.band && <div><dt>Shadow rule v2</dt><dd>{bandName(eyes.shadow_band_v2)} (would change from {bandName(p.band).toLowerCase()})</dd></div>}
        {diagnostics && !diagnostics.error && <>
          <div><dt>Planes</dt><dd>{diagnostics.planes ?? "—"}</dd></div>
          <div><dt>Crease runs</dt><dd>{diagnostics.crease_runs ?? "—"}</dd></div>
          <div><dt>Short junction links</dt><dd>{diagnostics.short_junction_links ?? "—"}</dd></div>
          <div><dt>Image edges matched</dt><dd>{diagnostics.matched_edges ?? "—"} of {diagnostics.detected_segments ?? "—"}</dd></div>
          {diagnostics.line_refinement_reason && <div><dt>Line refinement</dt><dd>{diagnostics.line_refinement_reason}</dd></div>}
          <div><dt>Creases with a brightness step</dt><dd>{percent(diagnostics.crease_contrast_share)}</dd></div>
          <div><dt>Outline edges without image support</dt><dd>{percent(diagnostics.edge_unsupported_fraction)}</dd></div>
          <div><dt>Frozen geometry reproduced</dt><dd>{yesNo(diagnostics.recomputed_matches_stored)}</dd></div></>}
        {diagnostics?.error && <div><dt>Recomputed diagnostics</dt><dd>Unavailable</dd></div>}</dl>
      <small>Read from the saved scene image. Every figure is a model estimate, not accuracy.</small>
    </section>
    <section className="processing-inspector-section processing-eyes-group" aria-labelledby="eyes-reference"><h3 id="eyes-reference">Reference agreement</h3>
      <p className="processing-group-summary">{referenceSummary}</p>
      {reference && <dl><div><dt>Human reference</dt><dd>Tier {reference.tier.slice(1)}{reference.image_identity && reference.image_identity !== "same_image" && <>, <span className="processing-review review-stale">{imageryName(reference.image_identity)}</span></>}</dd></div>
        <div><dt>Outline IoU</dt><dd>{ratio(reference.iou)}</dd></div><div><dt>Hausdorff distance</dt><dd>{metres(reference.hausdorff_m)}</dd></div>
        <div><dt>Roof type matches</dt><dd>{yesNo(reference.roof_type_match)}</dd></div>
        <div><dt>Roof-line precision</dt><dd>{ratio(reference.line_precision)}</dd></div>
        <div><dt>Roof-line recall</dt><dd>{ratio(reference.line_recall)}</dd></div></dl>}
      <small>{reference?.image_identity && reference.image_identity !== "same_image" ? "The reference was drawn over different imagery; distances include orthorectification differences. " : ""}Agreement with one human-drawn reference, not calibrated accuracy. Agent traces are never references.</small>
    </section>
    <section className="processing-inspector-section processing-eyes-group" aria-labelledby="eyes-captures"><h3 id="eyes-captures">Other captures</h3>
      <p className="processing-group-summary">{othersSummary}</p>
      {others.length > 0 && <ul>{others.map(o => <li key={o.key}>Also seen as {o.band || "an unknown band"} in <span className="processing-id">{o.key}</span>, IoU {ratio(o.iou)}, {o.note}</li>)}</ul>}
      <small>Each band of an overlapping capture is a model estimate, not accuracy.</small>
    </section>
  </>;
}

export default function ProcessingWorkspace({ initial }: { initial: Record<string, string> }) {
  const [summary, setSummary] = useState<ProcessingSummary | null>(null), [summaryError, setSummaryError] = useState(""), [attempt, setAttempt] = useState(-1);
  const [live, setLive] = useState(true), [refresh, setRefresh] = useState(0), [tick, setTick] = useState(0), [now, setNow] = useState(0);
  const [view, setView] = useState(views.includes(initial.view) ? initial.view : "table");
  const [query, setQuery] = useState(initial.q || ""), [search, setSearch] = useState(initial.q || "");
  const [band, setBand] = useState(["all", ...EVIDENCE_BANDS.map(b => b.id)].includes(initial.band) ? initial.band : "all");
  const [review, setReview] = useState(["all", ...Object.keys(reviewLabels)].includes(initial.review) ? initial.review : "all");
  const [addressState, setAddressState] = useState(["all", ...addressFilters.map(([id]) => id)].includes(initial.address) ? initial.address : "all");
  const [sort, setSort] = useState<RoofSort>(parseSort(initial.sort));
  const [page, setPage] = useState(Math.max(1, Math.trunc(Number(initial.page)) || 1));
  const [roofs, setRoofs] = useState<(ProcessingRoofsPage & { request: string }) | null>(null), [roofsError, setRoofsError] = useState("");
  const [selected, setSelected] = useState<string | null>(initial.roof || null), [selectedTile, setSelectedTile] = useState<string | null>(initial.tile || null);
  const [fullRoof, setFullRoof] = useState<ProcessingRoof | null>(null), [roofMissing, setRoofMissing] = useState<string | null>(null);
  const [hiddenLow, setHiddenLow] = useState<number | null>(null);
  const [queueStatus, setQueueStatus] = useState("all");
  useEffect(() => {
    const p = new URLSearchParams(); p.set("view", view);
    if (search) p.set("q", search); if (band !== "all") p.set("band", band); if (review !== "all") p.set("review", review);
    if (addressState !== "all") p.set("address", addressState); if (sort !== "evidence") p.set("sort", sort); if (page > 1) p.set("page", String(page));
    if (selected) p.set("roof", selected); if (selectedTile) p.set("tile", selectedTile);
    window.history.replaceState(null, "", `?${p}`);
  }, [view, search, band, review, addressState, sort, page, selected, selectedTile]);
  // Typing filters the server-side page after a short pause; the URL and request follow the settled text.
  useEffect(() => { const timer = setTimeout(() => setSearch(query.trim()), 250); return () => clearTimeout(timer); }, [query]);
  // Summary first: the progress strip, tile queue and object counts arrive from a cheap read (~0.3 s) and are polled every 20 s.
  useEffect(() => {
    let controller: AbortController | null = null;
    const load = () => {
      if (controller) return;
      const request = new AbortController(); controller = request;
      read<ProcessingSummary>(endpoint({ part: "summary" }), request.signal)
        .then(result => { if (!request.signal.aborted) { setSummary(result); setSummaryError(""); setNow(Date.now() / 1000); } })
        .catch(e => { if (!request.signal.aborted) setSummaryError(e instanceof Error ? e.message : "Could not read the suburb progress."); })
        .finally(() => { controller = null; if (!request.signal.aborted) setAttempt(refresh); });
    };
    load();
    const again = () => { if (!document.hidden) { load(); setTick(n => n + 1); } };
    const focus = () => { if (live) again(); };
    const interval = live ? setInterval(again, 20000) : undefined;
    window.addEventListener("focus", focus);
    return () => { controller?.abort(); clearInterval(interval); window.removeEventListener("focus", focus); };
  }, [live, refresh]);
  useEffect(() => { const t = setInterval(() => setNow(Date.now() / 1000), 10000); return () => clearInterval(t); }, []);
  const roofsRequest = useMemo(() => {
    const p: Record<string, string> = { part: "roofs", page: String(page), size: String(ROOF_PAGE_SIZE), sort };
    if (band !== "all") p.band = band; if (review !== "all") p.review = review; if (addressState !== "all") p.address = addressState; if (search) p.q = search;
    return endpoint(p);
  }, [page, sort, band, review, addressState, search]);
  // One page of trimmed rows from the route's cached inventory; re-read on every poll so live counts stay current.
  useEffect(() => {
    const controller = new AbortController();
    read<ProcessingRoofsPage>(roofsRequest, controller.signal)
      .then(result => { if (!controller.signal.aborted) { setRoofs({ ...result, request: roofsRequest }); setRoofsError(""); } })
      .catch(e => { if (!controller.signal.aborted) setRoofsError(e instanceof Error ? e.message : "Could not read the roof inventory."); });
    return () => controller.abort();
  }, [roofsRequest, tick, refresh]);
  // The inspector reads the complete feature for the selected roof; the table row stands in until it arrives.
  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    read<{ roof: ProcessingRoof }>(endpoint({ part: "roof", key: selected }), controller.signal)
      .then(result => { if (!controller.signal.aborted) { setFullRoof(result.roof); setRoofMissing(null); } })
      .catch(e => { if (!controller.signal.aborted && (e as { status?: number }).status === 404) setRoofMissing(selected); });
    return () => controller.abort();
  }, [selected, tick, refresh]);
  useEffect(() => {
    const controller = new AbortController();
    read<{ total: number }>(endpoint({ part: "hidden-low" }), controller.signal)
      .then(result => { if (!controller.signal.aborted) setHiddenLow(result.total); })
      .catch(() => undefined);
    return () => controller.abort();
  }, [tick, refresh]);
  const rowRoof = roofs?.rows.find(r => r.properties.key === selected);
  const roof: ProcessingRoofRow | null = selected ? (fullRoof?.properties.key === selected ? fullRoof : rowRoof) ?? null : null;
  const tile = summary?.queue.find(t => t.id === selectedTile) || null;
  const selectRoof = useCallback((key: string) => { setSelected(key); setSelectedTile(null); }, []);
  const selectTile = useCallback((id: string) => { setSelectedTile(id); setSelected(null); }, []);
  const suburb = summary?.suburb, completed = suburb?.counts.complete || 0, total = suburb?.total || 0;
  const active = summary?.queue.find(t => t.status === "running"), percentDone = total ? Math.round(completed / total * 1000) / 10 : 0;
  const low = summary?.counts.low || 0, failures = (suburb?.counts.failed || 0) + (suburb?.counts.interrupted || 0);
  const inventoryTotal = roofs ? Object.values(roofs.counts).reduce((a, b) => a + b, 0) : null;
  const warming = summary?.inventory === "warming" || roofs?.inventory === "warming";
  // The refresh control stays disabled only until the read it started has settled, successfully or not.
  const syncing = attempt !== refresh;
  const roofsLoading = !roofs || roofs.request !== roofsRequest;
  const filtered = Boolean(search || band !== "all" || review !== "all" || addressState !== "all");
  const filter = <T,>(setter: (value: T) => void) => (value: T) => { setter(value); setPage(1); };
  const clearFilters = () => { setQuery(""); setSearch(""); setBand("all"); setReview("all"); setAddressState("all"); setPage(1); };
  return <div className="processing-shell">
    <WorkbenchHeader current="processing" actions={<>
      <label className="processing-live"><input type="checkbox" checked={live} onChange={e => setLive(e.target.checked)}/><span className={`processing-live-dot ${live && !summaryError ? "is-live" : ""}`} aria-hidden="true"/>{live ? "Live updates" : "Updates paused"}</label>
      <button type="button" className="processing-icon-button" onClick={() => setRefresh(n => n + 1)} disabled={syncing} aria-label="Refresh processing workspace"><RefreshCw size={15} className={syncing ? "processing-spin" : ""}/></button>
      <span className="processing-synced">{summaryError ? "Connection interrupted" : summary ? `Synced ${duration(now - summary.fetched_at)} ago` : "Reading suburb progress"}</span>
    </>}/>
    <main className="processing-content">
      <div className="processing-heading"><h1>Mooroolbark</h1>{suburb && <p className="processing-title-line">Frozen scan {suburb.run}, {count(suburb.total)} tiles</p>}</div>
      {summaryError && <div className="processing-alert" role="alert">{summaryError} {summary && "Showing the last received snapshot."}<button onClick={() => setRefresh(n => n + 1)} disabled={syncing}>Reconnect</button></div>}
      {!summary ? <div className="processing-loading" role="status"><LoaderCircle className="processing-spin" size={24}/>{summaryError ? "The processing feed is unavailable." : "Reading suburb progress from the frozen scan"}</div> : !suburb ? <div className="processing-empty"><Layers size={28}/><h2>No suburb run yet</h2><p>A prepared suburb batch will appear here once it starts.</p><Link href="/scenes">Open scene review</Link></div> : <>
        <section className="processing-progress" aria-label="Suburb processing progress">
          <div className="processing-progress-main"><div className="processing-progress-title"><h2>Full suburb scan</h2><span className={`processing-status status-${suburb.status}`}><i/>{statusNames[suburb.status] || suburb.status}</span></div>
            <div className="processing-progress-numbers"><strong>{count(completed)}<span> of {count(total)} tiles</span></strong><span>{percentDone}%</span></div>
            <progress value={completed} max={total || 1} aria-label="Completed processing tiles"/>
            <p>{count(suburb.counts.pending || 0)} queued, {suburb.counts.running || 0} processing, {failures} need retry</p></div>
          <div className="processing-metrics"><div><span>Roof candidates found</span><strong>{count(summary.objects.building || 0)}</strong><small>In completed scan tiles</small></div>
            <div><span>Solar arrays found</span><strong>{count(summary.objects.solar_panel || 0)}</strong><small>Model detections</small></div>
            <button onClick={() => { setView("table"); setBand("low"); setQuery(""); setSearch(""); setReview("all"); setPage(1); }}><span>Low-evidence roofs</span><strong className="processing-low">{count(low)}<ArrowRight size={18}/></strong><small>Model band in completed scan tiles</small></button></div>
          <div className="processing-current"><span>{active ? <LoaderCircle size={14} className="processing-spin"/> : suburb.status === "complete" ? <Check size={14}/> : <Clock3 size={14}/>}</span>
            <p>{active ? <><strong>Tile {active.order} of {total}</strong><span>{active.has_capture ? "Imagery captured, analysing objects and roof geometry" : "Preparing imagery"}</span><span>{duration(now - suburb.updated_at)} since checkpoint</span></> : <><strong>{statusNames[suburb.status] || suburb.status}</strong><span>{failures ? "Failed or interrupted tiles remain in the queue." : "Completed predictions are ready to inspect."}</span></>}</p>
            <button onClick={() => { setView("queue"); setQueueStatus("all"); if (active) selectTile(active.id); }}>View queue<ArrowRight size={14}/></button>
          </div>
        </section>
        <p className="processing-scan-note">Tile progress measures area scanned. House totals grow as roofs are detected; no fixed house total is assumed.{warming && <span className="processing-inventory-note" role="status"> Inventory updating, counts may change.</span>}</p>
        <div className="processing-viewbar"><div className="processing-tabs" aria-label="Workspace view">
          {[["table", "Roof inventory", Table2], ["map", "Coverage map", MapIcon], ["queue", "Processing queue", ListOrdered], ["self-review", "Self-review", Activity]].map(([id, label, Icon]) => {
            const Glyph = Icon as typeof Table2;
            return <button key={id as string} aria-pressed={view === id} onClick={() => setView(id as string)}><Glyph size={16}/>{label as string}{id === "table" && inventoryTotal != null && <span>{count(inventoryTotal)}</span>}{id === "queue" && failures > 0 && <span>{failures}</span>}</button>;
          })}</div></div>
        {view !== "queue" && view !== "self-review" && <div className="processing-filters"><label className="processing-search"><Search size={16}/><input aria-label="Search roofs" placeholder="Search address, label or roof ID" value={query} onChange={e => { setQuery(e.target.value); setPage(1); }}/>{query && <button onClick={() => { setQuery(""); setSearch(""); setPage(1); }} aria-label="Clear search"><X size={13}/></button>}</label>
          <select aria-label="Confidence evidence" value={band} onChange={e => filter(setBand)(e.target.value)}><option value="all">All confidence bands</option>{EVIDENCE_BANDS.map(b => <option value={b.id} key={b.id}>{b.label}{roofs ? ` (${count(roofs.counts[b.id] || 0)})` : ""}</option>)}</select>
          <select aria-label="Review status" value={review} onChange={e => filter(setReview)(e.target.value)}><option value="all">All review states</option>{Object.entries(reviewLabels).map(([v, label]) => <option key={v} value={v}>{label}</option>)}</select>
          <select aria-label="Address mapping" value={addressState} onChange={e => filter(setAddressState)(e.target.value)}><option value="all">All address matches</option>{addressFilters.map(([id, label]) => <option key={id} value={id}>{label} ({count(roofs?.address_counts[id] || 0)})</option>)}</select>
          <span>{roofs && inventoryTotal != null ? `${count(roofs.total)} of ${count(inventoryTotal)} roofs${filtered ? " match" : ""}` : "Reading roof inventory"}</span></div>}
        {view === "self-review" ? <SelfReviewPanel live={live} refreshToken={refresh}/> : <div className={`processing-body ${roof || tile || roofMissing ? "has-detail" : ""}`}>
          <div className="processing-results">
            {view === "table" && <RoofTable data={roofs} loading={roofsLoading} error={roofsError} sort={sort} onSort={filter(setSort)} onPage={setPage} selected={selected} onSelect={selectRoof} clearFilters={clearFilters} filtered={filtered}/>}
            {view === "map" && <><ProcessingMap summary={summary} band={band} selected={roof} onSelect={selectRoof} onTile={selectTile}/><div className="processing-legend">{EVIDENCE_BANDS.map(b => <span key={b.id}><i style={{ background: b.color }}/>{b.label}</span>)}</div></>}
            {view === "queue" && <QueueTable queue={summary.queue} status={queueStatus} onStatus={setQueueStatus} selected={selectedTile} onSelect={selectTile}/>}
          </div>
          {roof && <aside className="processing-inspector" aria-label="Selected roof details"><div className="processing-inspector-title"><span>Roof inspector</span><button aria-label="Close roof details" onClick={() => setSelected(null)}><X size={16}/></button></div>
            <h2>{buildingAddressTitle(roof.properties.property_address)}</h2><p className="processing-id">{roof.properties.label}, {roof.properties.object_id.slice(0, 12)}</p>
            <BuildingAddressSummary key={roof.properties.key} address={roof.properties.property_address} sceneId={roof.properties.scene_id} objectId={roof.properties.object_id}/><ProcessingPreview key={`${roof.properties.key}/${roof.properties.model_signature}`} roof={roof} detail/>
            <section className="processing-inspector-section"><Evidence roof={roof}/><ul>{roof.properties.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
              <dl><div><dt>Footprint</dt><dd>{Math.round(roof.properties.area_m2)} m²</dd></div><div><dt>Geometry checks</dt><dd>{roof.properties.geometry_passed === undefined ? "Unavailable" : roof.properties.geometry_passed ? "Passed" : "Needs review"}</dd></div>
                <div><dt>Review state</dt><dd>{reviewLabels[roof.properties.review_status] || roof.properties.review_status}</dd></div></dl>
              <p className="processing-evidence-note">{roof.properties.interval ? `95% boundary-success interval for this band: ${roof.properties.interval.map(n => Math.round(n * 100)).join("–")}% across ${roof.properties.calibration_groups} validation areas. Not per-house or 3D accuracy.` : "Evidence bands are diagnostic checks, not accuracy percentages. A calibrated interval needs more independent reviewed areas."}</p>
            </section><EyesGroups roof={roof}/><section className="processing-inspector-section" aria-labelledby="image-context"><h3 id="image-context">Image context</h3><p>{roof.properties.capture_title}</p><small>The capture title does not assign an address to this roof.</small></section>
            <Link className="processing-primary" href={roof.properties.review_url}>Review and edit this roof<ArrowRight size={15}/></Link>
            {view !== "map" && <button className="processing-secondary" onClick={() => setView("map")}><MapIcon size={14}/>Locate on coverage map</button>}
          </aside>}
          {tile && <TileInspector tile={tile} onClose={() => setSelectedTile(null)}/>}
          {selected && !roof && roofMissing === selected && <aside className="processing-inspector"><p>This roof is no longer in the current inventory. It may have been re-analysed or replaced.</p><button onClick={() => setSelected(null)}>Close details</button></aside>}
        </div>}
        <footer className="processing-footer"><span><Activity size={13}/>Predictions remain unreviewed until confirmed in scene review. The inventory includes previously analysed captures.</span>
          <Link href="/queues#hidden">Low roofs hidden under higher bands{hiddenLow != null && ` (${count(hiddenLow)})`}</Link></footer>
      </>}
    </main>
  </div>;
}

const columns: { id: RoofSortKey | "preview" | "open"; label: string; sortable: boolean }[] = [
  { id: "preview", label: "Model preview", sortable: false }, { id: "address", label: "Property address / roof", sortable: true },
  { id: "evidence", label: "Confidence", sortable: true }, { id: "area", label: "Area", sortable: true },
  { id: "review", label: "Review", sortable: true }, { id: "open", label: "", sortable: false },
];

export function RoofTable({ data, loading, error, sort, onSort, onPage, selected, onSelect, clearFilters, filtered }: {
  data: ProcessingRoofsPage | null; loading: boolean; error: string; sort: RoofSort; onSort: (sort: RoofSort) => void; onPage: (page: number) => void;
  selected: string | null; onSelect: (key: string) => void; clearFilters: () => void; filtered: boolean;
}) {
  const desc = sort.startsWith("-"), sortKey = desc ? sort.slice(1) : sort;
  const toggle = (id: RoofSortKey) => onSort(sortKey === id && !desc ? `-${id}` : id);
  if (!data) return error ? <div className="processing-empty" role="alert"><p>{error}</p><p>The route reads the frozen scan again on the next poll.</p></div>
    : <div className="processing-loading" role="status"><LoaderCircle className="processing-spin" size={24}/>Reading the roof inventory. The first read of a full suburb takes a few seconds.</div>;
  if (!data.total) return <div className="processing-empty"><Search size={28}/><h2>No matching roofs</h2><p>Try another search or confidence band. New results appear as tiles finish.</p><button onClick={clearFilters}>Clear filters</button></div>;
  const start = (data.page - 1) * data.size + 1, end = Math.min(data.total, data.page * data.size);
  return <>{error && <div className="processing-alert" role="alert">{error} Showing the last received page.</div>}
    <div className="processing-table-scroll" tabIndex={0} role="region" aria-label="Roof inventory table" aria-busy={loading}><table className="processing-table"><caption className="sr-only">Roof predictions with model previews, confidence evidence and review status</caption>
      <thead><tr>{columns.map(c => <th key={c.id} aria-sort={c.sortable && sortKey === c.id ? (desc ? "descending" : "ascending") : undefined}>{c.sortable ? <button type="button" onClick={() => toggle(c.id as RoofSortKey)}>{c.label}{sortKey === c.id ? (desc ? <ArrowDown size={12}/> : <ArrowUp size={12}/>) : <ArrowUpDown size={12}/>}</button> : c.label}</th>)}</tr></thead>
      <tbody>{data.rows.map(r => <tr key={r.properties.key} className={selected === r.properties.key ? "is-selected" : ""}>
        <td><button className="processing-thumb" onClick={() => onSelect(r.properties.key)} aria-label={`Inspect roof ${r.properties.object_id}`}><ProcessingPreview key={`${r.properties.key}/${r.properties.model_signature}`} roof={r}/></button></td>
        <td><button className="processing-roof-name" onClick={() => onSelect(r.properties.key)}><strong>{buildingAddressTitle(r.properties.property_address)}</strong><span>{addressStatus(r.properties.property_address)}</span><small>{r.properties.label}, {r.properties.object_id.slice(0, 8)}</small></button></td>
        <td><Evidence roof={r}/></td>
        <td><span className="processing-number">{Math.round(r.properties.area_m2)} m²</span></td>
        <td><span className={`processing-review review-${r.properties.review_status}`}>{reviewLabels[r.properties.review_status] || r.properties.review_status}</span></td>
        <td><button className="processing-row-open" aria-label={`Open details for ${r.properties.object_id}`} onClick={() => onSelect(r.properties.key)}><ChevronRight size={17}/></button></td>
      </tr>)}</tbody>
    </table></div>
    <div className="processing-pagination"><span>Showing {count(start)}–{count(end)} of {count(data.total)}{filtered ? " matching roofs" : " roofs"}</span>{loading && <span role="status">Updating</span>}<span>Page {count(data.page)} of {count(data.pages)}</span>
      <button disabled={data.page <= 1} onClick={() => onPage(data.page - 1)} aria-label="Previous page"><ChevronLeft size={16}/></button><button disabled={data.page >= data.pages} onClick={() => onPage(data.page + 1)} aria-label="Next page"><ChevronRight size={16}/></button></div></>;
}

function QueueTable({ queue, status, onStatus, selected, onSelect }: { queue: ProcessingTile[]; status: string; onStatus: (s: string) => void; selected: string | null; onSelect: (id: string) => void }) {
  const [page, setPage] = useState(0);
  const rows = queue.filter(t => status === "all" || t.status === status).toSorted((a, b) => {
    const order = ["running", "interrupted", "failed", "pending", "complete"];
    return order.indexOf(a.status) - order.indexOf(b.status) || a.order - b.order;
  });
  const maxPage = Math.max(0, Math.ceil(rows.length / 12) - 1), current = Math.min(page, maxPage);
  return <><div className="processing-queue-filter"><h2>Capture queue <span>{count(queue.length)} tiles</span></h2><select aria-label="Processing state" value={status} onChange={e => { onStatus(e.target.value); setPage(0); }}><option value="all">All processing states</option>{["running", "pending", "complete", "failed", "interrupted"].map(s => <option key={s} value={s}>{statusNames[s]} ({queue.filter(t => t.status === s).length})</option>)}</select></div>
    {!rows.length ? <div className="processing-empty"><Check size={26}/><h2>No {statusNames[status]?.toLowerCase()} tiles</h2><button onClick={() => onStatus("all")}>Show all tiles</button></div> : <><div className="processing-table-scroll" tabIndex={0} role="region" aria-label="Processing tile queue"><table className="processing-table processing-queue-table"><thead><tr><th>Tile</th><th>State</th><th>Roofs</th><th>Solar</th><th>Duration</th><th>Attempts</th></tr></thead><tbody>{rows.slice(current * 12, current * 12 + 12).map(t => <tr key={t.id} className={t.id === selected ? "is-selected" : ""}><td><button className="processing-roof-name" onClick={() => onSelect(t.id)}><strong>Tile {t.order}</strong><small>{t.tile.join(" / ")}</small></button></td><td><span className={`processing-status status-${t.status}`}><i/>{statusNames[t.status]}</span></td><td>{t.status === "complete" ? t.objects.building || 0 : "—"}</td><td>{t.status === "complete" ? t.objects.solar_panel || 0 : "—"}</td><td>{t.elapsed_seconds == null ? "—" : duration(t.elapsed_seconds)}</td><td>{t.attempts || "—"}</td></tr>)}</tbody></table></div>
    <div className="processing-pagination"><span>Showing {count(current * 12 + 1)}–{count(Math.min(rows.length, current * 12 + 12))} of {count(rows.length)} tiles</span><span>Page {current + 1} of {maxPage + 1}</span><button disabled={current === 0} onClick={() => setPage(current - 1)} aria-label="Previous queue page"><ChevronLeft size={16}/></button><button disabled={current >= maxPage} onClick={() => setPage(current + 1)} aria-label="Next queue page"><ChevronRight size={16}/></button></div></>}
  </>;
}

function TileInspector({ tile, onClose }: { tile: ProcessingTile; onClose: () => void }) {
  return <aside className="processing-inspector" aria-label="Selected tile details"><div className="processing-inspector-title"><span>Capture details</span><button onClick={onClose} aria-label="Close tile details"><X size={16}/></button></div><h2>Tile {tile.order}</h2><p className="processing-id">{tile.tile.join(" / ")}</p>
    <span className={`processing-status status-${tile.status}`}><i/>{statusNames[tile.status]}</span>
    <ol className="processing-steps"><li className={tile.has_capture || tile.status === "complete" ? "is-done" : ""}><span>1</span><div><strong>Capture aerial imagery</strong><small>{tile.has_capture || tile.status === "complete" ? "Imagery available" : "Waiting for capture"}</small></div></li>
      <li className={tile.status === "complete" ? "is-done" : ""}><span>2</span><div><strong>Analyse objects and roofs</strong><small>{tile.status === "running" && tile.has_capture ? "Processing, stage detail unavailable" : tile.status === "complete" ? "Predictions saved" : "Awaiting completion"}</small></div></li>
      <li className={tile.status === "complete" ? "is-done" : ""}><span>3</span><div><strong>Assess confidence evidence</strong><small>{tile.status === "complete" ? "Ready for review" : "Awaiting completion"}</small></div></li></ol>
    {tile.error && <div role="alert" className="processing-tile-error"><strong>Processing error</strong><p>{tile.error}</p><small>The failed tile remains recorded for retry by the suburb worker.</small></div>}
    <dl><div><dt>Attempts</dt><dd>{tile.attempts}</dd></div><div><dt>Processing time</dt><dd>{tile.elapsed_seconds == null ? "Pending" : duration(tile.elapsed_seconds)}</dd></div>
      {Object.entries(tile.objects).map(([kind, n]) => <div key={kind}><dt>{({ building: "Roof candidates", tree: "Trees", solar_panel: "Solar arrays" })[kind] || kind}</dt><dd>{count(n)}</dd></div>)}</dl>
    {tile.review_url ? <Link href={tile.review_url} className="processing-primary">Review this capture<ArrowRight size={15}/></Link> : <p className="processing-evidence-note">Review becomes available after analysis and confidence checks finish. You can leave this page while the worker continues.</p>}
  </aside>;
}
