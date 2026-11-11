"use client";
import { useEffect, useState } from "react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { ArrowUpRight } from "lucide-react";
import { WorkbenchHeader } from "@/components/shell/WorkbenchHeader";
import type { SceneModelSnapshot, SceneObject } from "@/lib/scene-types";
import { roofImageLines, ROOF_LINE_COLORS } from "@/lib/scene-roof-lines";
const Scene3D = dynamic(() => import("./Scene3D"), { ssr: false });
type Capture = { index: number; acquisition_date: string; release_title: string; provider: string; source: string;
  resolution_m: number; status: string; reason?: string; registration?: { reason?: string; check_median_residual_px?: number }; counts?: Record<string, number> };
type Row = { object_id: string; kind: string; status: string; reason: string; surface_fitted?: boolean;
  observations: {date: string; iou: number | null; local_registration_fraction: number; ambiguous: boolean}[] };
type Report = { run: string; scene_id: string; title: string; status: string; stage: string; stale: boolean;
  captures: Capture[]; rows: Row[]; reference_date: string | null; image_width: number; image_height: number;
  fusion?: {combined_fraction: number}; summary?: {consistent: number; needs_review: number; roof_surfaces_fitted: number; roof_surfaces_queued: number} };
const roofClasses = new Set(["building", "tree", "solar_panel"]);
export default function TemporalWorkspace({ scene, initialRun }: {scene: string; initialRun: string}) {
  const [run, setRun] = useState(initialRun), [report, setReport] = useState<Report | null>(null);
  const [pending, setPending] = useState(false), [error, setError] = useState("");
  const [view, setView] = useState("reference"), [kind, setKind] = useState("building"), [threeD, setThreeD] = useState(false);
  const [snapshot, setSnapshot] = useState<SceneModelSnapshot | null>(null), [objects, setObjects] = useState<SceneObject[]>([]);
  const [showOutlines, setShowOutlines] = useState(true), [selected, setSelected] = useState<string | null>(null);
  const [overlayError, setOverlayError] = useState("");
  const [overlayKey, setOverlayKey] = useState(""), [refresh, setRefresh] = useState(0);
  const api = (asset?: string) => `/api/scenes/temporal?${new URLSearchParams({run, ...(asset ? {asset} : {})})}`;
  useEffect(() => {
    if (!run) return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    async function load() {
      try {
        const response = await fetch(`/api/scenes/temporal?run=${encodeURIComponent(run)}`, {signal: controller.signal, cache: "no-store"});
        const value = await response.json(); if (!response.ok) throw new Error(value.error);
        if (!controller.signal.aborted) { setReport(value); setError(""); }
        if (value.status === "running" && !controller.signal.aborted) timer = setTimeout(load, 4000);
      } catch (e) { if (!controller.signal.aborted) setError((e as Error).message); }
    }
    void load();
    return () => {controller.abort(); clearTimeout(timer);};
  }, [run, refresh]);
  const complete = report?.status === "complete";
  const dated = view.startsWith("date-") ? report?.captures?.find(c => c.index === Number(view.slice(5))) : undefined;
  const unavailableDate = view.startsWith("date-") && dated?.status !== "accepted";
  useEffect(() => {
    if (!run) return;
    const controller = new AbortController();
    const asset = view === "candidate" ? "candidate.json" : view.startsWith("date-") ? `objects-${view.slice(5)}.json` : view === "saved" ? "input.json" : "reference.json";
    void (async () => {
      try {
        if (unavailableDate) {setObjects([]); setOverlayError(""); return;}
        const [base, overlay] = await Promise.all([fetch(`/api/scenes/temporal?run=${run}&asset=${view === "saved" ? "input" : "reference"}.json`, {signal:controller.signal}),
          fetch(`/api/scenes/temporal?run=${run}&asset=${asset}`, {signal:controller.signal})]);
        if (!base.ok || !overlay.ok) throw new Error("Outlines are not ready for this image yet.");
        const [frame, content] = await Promise.all([base.json(), overlay.json()]);
        if (!controller.signal.aborted) { setSnapshot(view === "candidate" ? content : frame); setObjects(content.objects); setOverlayKey(`${run}:${view}`); setOverlayError(""); }
      } catch(e) { if (!controller.signal.aborted) {setObjects([]); setOverlayError((e as Error).message);} }
    })();
    return () => controller.abort();
  }, [run, view, complete, unavailableDate]);
  async function start() {
    setPending(true); setError("");
    try {
      const response = await fetch("/api/scenes/temporal", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({scene: scene || report?.scene_id})});
      const value = await response.json(); if (!response.ok) throw new Error(value.error);
      setReport(value); setRun(value.run); setRefresh(n=>n+1);
      window.history.replaceState(null, "", `/scenes/imagery?${new URLSearchParams({scene: value.scene_id, run: value.run})}`);
    } catch(e) {setError((e as Error).message);} finally {setPending(false);}
  }
  const image = view === "candidate" || view === "combined" ? "combined.png" : view === "saved" ? "saved-source.png" : dated ? `${dated.status === "accepted" ? "aligned" : "capture"}-${dated.index}.png` : report?.reference_date ? "reference.png" : "saved-source.png";
  const filtered = overlayKey === `${run}:${view}` ? objects.filter(o => roofClasses.has(o.kind) && (kind === "all" || o.kind === kind) && !["rejected", "superseded"].includes(o.status)) : [];
  const focused = snapshot?.objects.find(o=>o.id === selected);
  const bounds = focused ? [Math.max(0,Math.min(...focused.ring.map(p=>p[0]))-40), Math.max(0,Math.min(...focused.ring.map(p=>p[1]))-40),
    Math.min(report?.image_width || 1536,Math.max(...focused.ring.map(p=>p[0]))+40), Math.min(report?.image_height || 1536,Math.max(...focused.ring.map(p=>p[1]))+40)] : null;
  const viewBox = bounds ? `${bounds[0]} ${bounds[1]} ${bounds[2]-bounds[0]} ${bounds[3]-bounds[1]}` : `0 0 ${report?.image_width} ${report?.image_height}`;
  const location = snapshot?.centroid;
  // A report is read straight from disk; a partial or interrupted write must not take the page down with it.
  const captures = report?.captures ?? [], rows = report?.rows ?? [];
  // Imagery dates is a view of scene review: the scene stays the h1 and the Map link returns to its location once the frame is known.
  return <div className="review-shell temporal-workspace">
    <WorkbenchHeader current="scenes" hrefs={{ map: location ? `/?lat=${location.lat}&lon=${location.lon}` : "/" }}
      actions={<Link className="review-button" href="/scenes/processing?view=self-review">Self-review queue</Link>} />
    <main className="temporal-content">
    <div className="temporal-heading"><div><h1>{report?.title || "Imagery dates"}</h1>
      <p>{report ? "Independently dated views of this area, registered to the saved scene image." : scene ? `Scene ${scene}. No imagery comparison has run yet.` : "Open this view from a scene to compare its imagery dates."}</p></div>
      <button className="review-button review-primary" onClick={start} disabled={pending || report?.status === "running" || (!scene && !report?.scene_id)}>{pending ? "Starting…" : report ? "Run comparison again" : "Compare imagery dates"}</button></div>
    <p className="temporal-note">Roofs use repeat observations and bounded geometry checks. Trees keep separate outlines for each date, so older canopies cannot fill newer gaps.</p>
    {error && <p className="review-error" role="alert">{error}</p>}
    {report && <><section className="temporal-progress" aria-label="Comparison progress"><div role="status">{report.stage}</div>
      {report.status === "running" && <progress aria-label="Comparing imagery"/>}
      <span>{captures.filter(c=>c.status === "accepted").length} of {captures.length} dates registered{report.summary ? `, ${report.summary.consistent} consistent objects, ${report.summary.needs_review} to inspect, ${report.summary.roof_surfaces_fitted} roof surfaces fitted` : ""}</span>
      {!report.reference_date && <p>The saved image has no verified acquisition date. These comparisons do not establish which view represents today.</p>}
      {report.stale && <p className="temporal-stale" role="alert">The saved model has changed since this comparison. Start a new comparison before using its proposals.</p>}</section>
    <div className="temporal-grid"><aside aria-label="Imagery sources"><h2>Views</h2>
      <button aria-pressed={view === "reference"} onClick={()=>{setView("reference");setThreeD(false);}}>Newest dated reference <small>{report.reference_date || "Finding acquisition date…"}</small></button>
      <button aria-pressed={view === "saved"} onClick={()=>{setView("saved");setThreeD(false);setSelected(null);}}>Original saved scene<small>Original model frame, unchanged</small></button>
      {captures.map(c=><button key={c.index} aria-pressed={view === `date-${c.index}`} disabled={c.status === "failed"} onClick={()=>{setView(`date-${c.index}`);setThreeD(false);}}>
        {c.acquisition_date}<small>{c.provider}, {c.resolution_m} m per pixel</small><small>{c.status === "accepted" ? "Registered" : c.status}: {c.registration?.reason || c.reason || c.release_title}</small></button>)}
      {report.fusion && Number.isFinite(report.fusion.combined_fraction) && <button aria-pressed={view === "combined"} onClick={()=>{setView("combined");setThreeD(false);}}>Combined stable detail<small>{(report.fusion.combined_fraction*100).toFixed(1)}% of pixels combined; remainder retained</small></button>}
      {complete && !!report.summary?.roof_surfaces_fitted && <button aria-pressed={view === "candidate"} onClick={()=>setView("candidate")}>Roof proposals<small>{report.summary.roof_surfaces_fitted} fitted, {report.summary.roof_surfaces_queued} queued, not applied</small></button>}
      <p>Esri World Imagery Wayback. Dates above are photo acquisition dates; archive publication dates are separate.</p>
      <a href="https://livingatlas.arcgis.com/wayback/" target="_blank" rel="noreferrer">Imagery attribution and archive <ArrowUpRight size={13} aria-hidden="true" /></a>
    </aside><section className="temporal-canvas"><div className="temporal-tools">
      <label>Objects <select value={kind} onChange={e=>setKind(e.target.value)}><option value="building">Roofs</option><option value="tree">Trees</option><option value="solar_panel">Solar arrays</option><option value="all">All</option></select></label>
      <label><input type="checkbox" checked={showOutlines} onChange={e=>setShowOutlines(e.target.checked)}/> Outlines</label>
      {selected && <button onClick={()=>setSelected(null)}>Whole image</button>}
      {view === "candidate" && <button aria-pressed={threeD} onClick={()=>setThreeD(v=>!v)}>{threeD ? "Aerial view" : "Inspect 3D proposals"}</button>}
    </div>{overlayError && <p role="status">{overlayError}</p>}
    {threeD && snapshot && overlayKey === `${run}:${view}` ? <div className="temporal-three"><Scene3D scene={snapshot} objects={objects} selected={selected} visible={new Set(kind === "all" ? roofClasses : [kind])} onSelect={setSelected} disabled/></div> :
      <svg viewBox={viewBox} aria-label="Registered imagery comparison">
        <image href={api(image)} width={report.image_width} height={report.image_height}/>
        {showOutlines && (!dated || dated.status === "accepted") && filtered.map(o=><path key={o.id} d={[o.ring,...(o.holes||[])].map(r=>`M${r.map(p=>p.join(",")).join("L")}Z`).join(" ")} fill="none" stroke={o.kind === "tree" ? "#85e2a6" : o.kind === "solar_panel" ? "#f4ce6b" : "#68dfea"} strokeWidth={2} vectorEffect="non-scaling-stroke"><title>{o.label}</title></path>)}
        {showOutlines && view === "candidate" && snapshot && filtered.flatMap(o=>roofImageLines(o,snapshot.meters_to_pixel).map((line,i)=><line key={`${o.id}-${i}`} x1={line.a[0]} y1={line.a[1]} x2={line.b[0]} y2={line.b[1]} stroke={ROOF_LINE_COLORS[line.kind]} strokeWidth={1.5} strokeDasharray={line.kind === "valley" ? "4 3" : undefined} vectorEffect="non-scaling-stroke"/>))}
      </svg>}
    <p className="temporal-caption">{view === "combined" ? "Stable pixels combined at the original resolution. Outlines show the reference-date estimates for comparison." : view === "candidate" ? "Experimental roof surfaces. Green: ridges; cyan: hips; purple: valleys. Heights remain estimates; these have not replaced your saved models." : view === "saved" ? "Original saved image and estimates, retained for comparison." : dated && dated.status !== "accepted" ? "Alignment failed. This source is shown separately without model overlays." : "Each dated image has its own object detections."}</p></section></div>
    {!!rows.length && <section className="temporal-results"><h2>Evidence to review</h2><p>Agreement is an evidence check, not a calibrated accuracy percentage. It requires at least two dates with 75% outline overlap and alignment support over 80% of the object. Missing detections may reflect occlusion or change.</p><table><thead><tr><th scope="col">Object</th><th scope="col">Evidence</th><th scope="col">Observations</th><th scope="col">Next check</th></tr></thead><tbody>{rows.filter(r=>kind === "all" || r.kind === kind).map(row=><tr key={row.object_id}><td><button onClick={()=>{setView(row.surface_fitted ? "candidate" : "reference");setSelected(row.object_id);setThreeD(false);document.querySelector('.temporal-canvas')?.scrollIntoView({behavior:"smooth",block:"start"});}}>{row.kind.replaceAll("_"," ")} {row.object_id.slice(0,8)}</button></td><td>{row.status === "consistent" ? "Consistent across dates" : "Needs inspection"}{row.surface_fitted ? ", roof proposal ready" : ""}</td><td>{row.observations.map(o=><div key={o.date}>{o.date}: {o.iou === null ? "not detected" : `${Math.round(o.iou*100)}% overlap`}, {Math.round(o.local_registration_fraction*100)}% alignment support{o.ambiguous ? ", ambiguous match" : ""}</div>)}</td><td>{row.reason}</td></tr>)}</tbody></table></section>}</>}
    </main>
  </div>;
}
