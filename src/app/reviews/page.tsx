"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { ArrowUpRight, Check, ChevronLeft, ChevronRight, Download, Loader2, MapPin, Search, ShieldCheck, SlidersHorizontal, X } from "lucide-react";
import RoofReviewCanvas, { type RoofPartItem, type RooflineItem, type AccuracySignals, type RoofReflection } from "@/components/reviews/RoofReviewCanvas";
import Roof3DPreview from "@/components/reviews/Roof3DPreview";
import RoofAnnotationEditor from "@/components/reviews/RoofAnnotationEditor";
import TrainingStatus from "@/components/reviews/TrainingStatus";
import { annotationFromModel, annotationParts, annotationLines, annotationIssues, type RoofObject } from "@/lib/roof-annotations";
import { getCrmToken } from "@/lib/crm-client";
import { REVIEW_ISSUES, type ReviewFeedback, type ReviewStatus } from "@/lib/review-types";
import "./reviews.css";
// After the page CSS so shell.css (via WorkbenchNav) wins the equal-specificity .review-header row.
import { WorkbenchHeader } from "@/components/shell/WorkbenchHeader";

type Entry = {
  address_id: number; full_address: string; category: string; tags: string[];
  area_m2: number; face_count: number; roofline_count: number; roof_type?: string; roof_material?: string;
  ring_meters: [number, number][]; reference_ring_meters: [number, number][]; ring_pixels?: [number, number][];
  centroid?: { lat: number; lon: number }; address_point?: { lat: number; lon: number };
  property_boundary_meters?: [number, number][] | null;
  nearby_pins?: Array<{ label: string; meters?: [number, number] }>;
  tree_canopies_meters?: [number, number][][]; neighbour_buildings_meters?: [number, number][][];
  parts: RoofPartItem[]; lines: RooflineItem[]; site_image_url: string; mask_image_url?: string;
  signals?: AccuracySignals; reflection?: RoofReflection; feedback?: Partial<ReviewFeedback>;
  boundary_source: string; boundary_signature: string; geometry_source: string;
  height_source: string; eave_height?: number; ridge_height?: number;
  structure_audit?: { passed: boolean; issues: string[]; max_planarity_error_m?: number; max_seam_error_m?: number };
  image_structure_audit?: { passed: boolean; issues: string[] };
  stale_review: boolean; imagery_available: boolean; training_eligible: boolean;
  model_signature: string; annotation_stale?: boolean; object_candidates?: RoofObject[]; object_detection_status?: string;
  split?: string; spatial_group?: string;
  roof_form_selection?: {source:string;gable_ends:number;ambiguous_ends:number};
};
type Cohort = { cohort: string; title: string; description: string; total_roofs: number; entries: Entry[];
  cohorts: Array<{ name: string; title: string; count: number }>; contact_sheet_url?: string };
const labels: Record<ReviewStatus, string> = { unreviewed: "To review", needs_adjustment: "Needs correction", approved: "Approved", rejected: "Rejected" };
const issueLabels: Record<string, string> = { boundary: "Roof boundary", apex: "Ridge / apex", roof_planes: "Roof planes", vegetation: "Vegetation", wrong_building: "Wrong building" };
const PAGE_SIZE = 20;
const initialFeedback = (entry: Entry): ReviewFeedback => ({ status: entry.feedback?.status || "unreviewed", notes: entry.feedback?.notes || "", rejected_cuts: entry.feedback?.rejected_cuts || [], issues: entry.feedback?.issues || [], boundary_verified: !entry.stale_review && !entry.annotation_stale && (entry.feedback?.boundary_verified || false), revision: entry.feedback?.revision || 0,
  annotation:entry.annotation_stale?null:entry.feedback?.annotation, objects:entry.annotation_stale ? entry.object_candidates || [] : entry.feedback?.objects || entry.object_candidates || [], model_signature:entry.model_signature,
  roof_form_verified:!entry.annotation_stale&&entry.feedback?.roof_form_verified===true, apex_verified:!entry.annotation_stale&&entry.feedback?.apex_verified===true,objects_verified:!entry.annotation_stale&&entry.feedback?.objects_verified===true, object_reviewed_classes:entry.annotation_stale?[]:entry.feedback?.object_reviewed_classes || (entry.feedback?.objects_verified?["solar_panel","skylight","chimney"]:[]) });
const statusFor = (entry: Entry) => entry.stale_review ? "needs_adjustment" : entry.feedback?.status || "unreviewed";

export default function RoofReviewStudioPage() {
  const [cohort, setCohort] = useState("suburban_benchmark");
  const [ready, setReady] = useState(false);
  const [data, setData] = useState<Cohort | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  const [id, setId] = useState<number | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [page, setPage] = useState(0);
  const [view, setView] = useState("compare");
  const [layer,setLayer]=useState<"model"|"edited"|"overlay">("edited");
  const [editGeometry,setEditGeometry]=useState(false);
  const [drafts, setDrafts] = useState<Record<string, ReviewFeedback>>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [message, setMessage] = useState<{ text: string; error?: boolean } | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const readUrl = () => {
      const params = new URLSearchParams(window.location.search);
      setCohort(params.get("cohort") || "suburban_benchmark");
      setId(Number(params.get("id")) || null);
      setQuery(params.get("q") || ""); setFilter(params.get("status") || "all");
      setPage(Math.max(0, Number(params.get("page")) || 0)); setReady(true);
    };
    readUrl(); window.addEventListener("popstate", readUrl);
    return () => window.removeEventListener("popstate", readUrl);
  }, []);
  useEffect(() => {
    if (!ready) return;
    const controller = new AbortController();
    setLoading(true); setError(""); setData(null);
    fetch(`/api/reviews?cohort=${encodeURIComponent(cohort)}`, { signal: controller.signal })
      .then(async response => { const body = await response.json(); if (!response.ok) throw new Error(body.error || "Could not load reviews."); if (!Array.isArray(body.entries)) throw new Error("Could not load reviews."); return body as Cohort; })
      .then(body => { setData(body); setId(current => body.entries.some(e => e.address_id === current) ? current : body.entries[0]?.address_id ?? null); })
      .catch(e => { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : "Could not load reviews."); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [cohort, ready, reload]);
  useEffect(() => {
    if (!ready) return;
    const params = new URLSearchParams({ cohort });
    if (id) params.set("id", String(id)); if (query) params.set("q", query);
    if (filter !== "all") params.set("status", filter); if (page) params.set("page", String(page));
    window.history.replaceState(null, "", `/reviews?${params}`);
  }, [cohort, id, query, filter, page, ready]);
  const dirtyCount = Object.keys(drafts).length;
  useEffect(() => {
    if (!dirtyCount) return;
    const prevent = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", prevent); return () => window.removeEventListener("beforeunload", prevent);
  }, [dirtyCount]);
  const filtered = useMemo(() => (data?.entries || []).filter(entry => {
    const match = `${entry.full_address} ${entry.address_id} ${entry.tags.join(" ")}`.toLowerCase().includes(query.toLowerCase());
    return match && (filter === "all" || (filter === "training" ? entry.training_eligible : filter === "edited" ? Boolean(entry.feedback?.annotation || entry.feedback?.objects?.length) : filter === "modeled" ? !entry.feedback?.annotation : statusFor(entry) === filter));
  }), [data, filter, query]);
  const safePage = Math.min(page, Math.max(0, Math.ceil(filtered.length / PAGE_SIZE) - 1));
  const entries = filtered.slice(safePage * PAGE_SIZE, (safePage + 1) * PAGE_SIZE);
  const current = data?.entries?.find(e => e.address_id === id) || null;
  const key = `${cohort}:${id}`;
  const draft = current ? drafts[key] || initialFeedback(current) : null;
  const dirty = Boolean(drafts[key]);
  const busy = saving === key;
  const model=useMemo(()=>current?annotationFromModel(current.parts,current.lines,current.roof_type):null,[current]);
  const activeAnnotation=layer!=="model"?draft?.annotation:null;
  const displayedParts=activeAnnotation?annotationParts(activeAnnotation):current?.parts||[];
  const displayedLines=activeAnnotation?annotationLines(activeAnnotation):current?.lines||[];
  const shapeIssues=activeAnnotation&&current?annotationIssues(activeAnnotation,current.ring_meters):[];
  const reviewed = data?.entries?.filter(e => statusFor(e) !== "unreviewed").length || 0;
  const approved = data?.entries?.filter(e => e.training_eligible).length || 0;
  const select = useCallback((entry: Entry) => { setId(entry.address_id); setMessage(null); }, []);
  const update = (change: Partial<ReviewFeedback>) => {
    if (!draft || !current) return;
    const next = { ...draft, ...change };
    if ((next.issues.length || next.rejected_cuts.length || !next.boundary_verified) && next.status === "approved") next.status = "needs_adjustment";
    setDrafts(prev => { const result = { ...prev }; if (JSON.stringify(next) === JSON.stringify(initialFeedback(current))) delete result[key]; else result[key] = next; return result; });
    setMessage(null);
  };
  const discard = () => { setDrafts(prev => { const next = { ...prev }; delete next[key]; return next; }); setMessage(null); };
  const save = async () => {
    if (!current || !draft || saving) return;
    const target = current.address_id, targetKey = key, submitted = draft;
    setSaving(targetKey); setMessage(null);
    try {
      const token = getCrmToken();
      const response = await fetch("/api/reviews", { method: "POST", headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
        body: JSON.stringify({ ...submitted, address_id: target, cohort, boundary_signature: current.boundary_signature, model_signature:current.model_signature }) });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Could not save review. Retry with your draft intact.");
      setData(previous => previous ? { ...previous, entries: previous.entries.map(e => e.address_id === target ? { ...e, feedback: result.feedback, stale_review: false, annotation_stale:false, training_eligible:result.training_eligible } : e) } : previous);
      setDrafts(previous => { const next = { ...previous }; delete next[targetKey]; return next; });
      setMessage({ text: "Review saved. Your corrections are saved for review and training export." });
    } catch (e) { setMessage({ text: e instanceof Error ? e.message : "Could not save review.", error: true }); }
    finally { setSaving(null); }
  };
  const exportReviews = () => {
    const records = (data?.entries || []).filter(e => e.training_eligible).map(e => ({ address_id: e.address_id, full_address: e.full_address, centroid: e.centroid, ring_meters: e.reference_ring_meters, boundary_source: e.boundary_source, height_source: e.height_source, model_signature:e.model_signature, split:e.split, spatial_group:e.spatial_group,
      image_url:e.site_image_url, model_prediction:{parts:e.parts,lines:e.lines}, feedback:e.feedback }));
    const blob = new Blob([JSON.stringify({ schema_version: 2, cohort, reference: "Reviewed annotations with immutable model predictions; heights remain estimates", records }, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob); const anchor = document.createElement("a"); anchor.href = url; anchor.download = `${cohort}-reviewed-boundaries.json`; anchor.click(); URL.revokeObjectURL(url);
  };
  const guardExit = (event: React.MouseEvent<HTMLAnchorElement>) => { if (dirtyCount) { event.preventDefault(); setMessage({ text: "Save or discard your drafts before leaving the review workspace.", error: true }); } };
  const location = current?.address_point || current?.centroid;
  const mapUrl = location ? `/?lat=${location.lat}&lon=${location.lon}` : "/";
  const index = filtered.findIndex(e => e.address_id === id);
  const move = (offset: number) => { const target = filtered[index + offset]; if (target) { select(target); setPage(Math.floor((index + offset) / PAGE_SIZE)); } };

  return <div className="review-shell">
    {/* Shell header: workspace nav plus this page's actions. "Roof review" is the quiet workspace label
        (verify-reviews.mjs pins that body text); the address below remains the page's only h1. */}
    <WorkbenchHeader onNavigate={guardExit} hrefs={{ map: "/" }} actions={<>
      <span className="review-muted">Roof review</span>
      <Link className="review-button" href={`/scenes?scene=${cohort}-${id||29790}`} onClick={guardExit}>Review scene objects</Link>
      <span className="review-muted">{dirtyCount ? `${dirtyCount} unsaved draft${dirtyCount > 1 ? "s" : ""}` : `${reviewed} of ${data?.total_roofs || 0} reviewed`}</span>
      <button className="review-button" onClick={exportReviews} disabled={!approved}><Download size={15} /> Export reviewed ({approved})</button>
    </>} />
    <div className="review-layout">
      <aside className="review-queue" aria-label="Roof review queue">
        <div className="review-queue-top">
          <label htmlFor="cohort">Review set</label>
          <select id="cohort" value={cohort} disabled={Boolean(saving)} onChange={e => { setCohort(e.target.value); setId(null); setPage(0); setFilter("all"); setQuery(""); setMessage(null); }}>
            {(data?.cohorts || [{ name: cohort, title: "Suburban benchmark", count: 12 }]).map(c => <option key={c.name} value={c.name}>{c.title} ({c.count})</option>)}
          </select>
          <div className="review-search"><Search size={16} /><input ref={searchRef} aria-label="Search roofs" placeholder="Address, ID or tag" value={query} onChange={e => { setQuery(e.target.value); setPage(0); }} />{query && <button aria-label="Clear search" onClick={() => { setQuery(""); setPage(0); searchRef.current?.focus(); }}><X size={15} /></button>}</div>
          <div className="review-filter"><SlidersHorizontal size={14} /><label className="sr-only" htmlFor="review-status-filter">Filter review status</label><select id="review-status-filter" value={filter} onChange={e => { setFilter(e.target.value); setPage(0); }}><option value="all">All statuses</option>{Object.entries(labels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}<option value="training">Ready for training</option><option value="edited">Edited roofs / objects</option><option value="modeled">Model only</option></select><span>{filtered.length}</span></div>
        </div>
        <div className="review-queue-list">
          {!loading && !entries.length && <p className="review-empty">No roofs match these filters.<button className="review-text-button" onClick={() => { setQuery(""); setFilter("all"); }}>Clear filters</button></p>}
          {entries.map(entry => <button key={entry.address_id} className={`review-queue-item ${id === entry.address_id ? "is-current" : ""}`} disabled={Boolean(saving)} aria-current={id === entry.address_id ? "true" : undefined} onClick={() => select(entry)}>
            <span className="review-queue-address">{entry.full_address.split(",")[0]}</span>
            <span className="review-queue-locality">{entry.full_address.split(",").slice(1).join(",").trim() || entry.category}</span>
            <span className="review-queue-meta"><span className={`review-status status-${statusFor(entry)}`}>{entry.stale_review ? "Recheck boundary" : labels[statusFor(entry)]}</span><span>{drafts[`${cohort}:${entry.address_id}`] ? "Draft" : `#${entry.address_id}`}</span></span>
          </button>)}
        </div>
        <div className="review-pagination"><span>{filtered.length ? `${safePage * PAGE_SIZE + 1}–${Math.min((safePage + 1) * PAGE_SIZE, filtered.length)} of ${filtered.length}` : "0 results"}</span><button aria-label="Previous queue page" disabled={safePage === 0} onClick={() => setPage(safePage - 1)}><ChevronLeft size={16} /></button><button aria-label="Next queue page" disabled={(safePage + 1) * PAGE_SIZE >= filtered.length} onClick={() => setPage(safePage + 1)}><ChevronRight size={16} /></button></div>
        <p className="review-queue-help"><ShieldCheck size={16} /> Only verified targets enter training. Boundaries, roof shapes and objects can be reviewed separately.</p>
      </aside>
      <main className="review-main">
        {loading ? <div role="status" className="review-state"><Loader2 className="animate-spin" /><h1>Loading roof reviews</h1><p>Preparing boundaries and imagery.</p></div> : error ? <div role="alert" className="review-state"><h1>Reviews could not load</h1><p>{error}</p><button className="review-button" onClick={() => setReload(n => n + 1)}>Try again</button></div> : !current || !draft ? <div className="review-state"><h1>No captured roofs in this set</h1><p>Choose another review set to begin.</p></div> : <>
          <div className="review-title-row"><div><div className="review-address-label"><MapPin size={14} /> {current.category} <span>#{current.address_id}</span></div><h1>{current.full_address.split(",")[0]}</h1><p>{current.full_address.split(",").slice(1).join(",").trim()}</p></div><Link className="review-button" href={mapUrl} onClick={guardExit}>Edit in Address CRM <ArrowUpRight size={15} /></Link></div>
          <div className="review-evidence-bar"><span className="review-source"><ShieldCheck size={15} />{current.boundary_source === "crm" ? "CRM-corrected outline" : "Model-captured outline"}</span><span>{current.area_m2.toFixed(0)} m² footprint</span><span>{current.parts.length} roof faces</span><span>{current.lines.length} rooflines</span><span>Heights estimated</span>{current.split && <span>{current.split} split</span>}{current.spatial_group && <span>Area group {current.spatial_group}</span>}</div>
          {(current.stale_review || current.geometry_source === "legacy-unverified" || current.geometry_source === "manual-template") && <p className="review-notice" role="status">{current.stale_review ? "The boundary changed after approval. Check the updated outline before approving it again." : "This cached reconstruction has unverified provenance. Rebuild the cohort before approving its geometry."}</p>}
          {current.geometry_source === "structural-prior" && <p className="review-notice" role="status"><strong>Roof-form hypothesis.</strong> This preview uses planar slopes and connected ridges fitted to the outline, with image evidence and any reviewer roof-form correction. Confirm the roof type, ridges and heights against imagery before approving.</p>}
          {current.structure_audit && <details className="review-notice"><summary>{current.structure_audit.passed ? "Roof geometry checks passed" : "Roof geometry needs correction"}</summary><p>Checks cover face planarity, coverage, shared-edge heights and interior low points. They establish geometric plausibility, not measured accuracy or structural compliance.</p>{current.structure_audit.issues.length > 0 && <p>Unresolved: {current.structure_audit.issues.join(", ").replaceAll("-", " ")}</p>}{current.geometry_source === "structural-prior" && <p>Original image estimate: {current.image_structure_audit?.issues.join(", ").replaceAll("-", " ")}</p>}</details>}
          <div className="review-workspace-heading"><h2>Inspect the roof</h2><div className="review-view-switch" aria-label="Inspection view">{["compare", "aerial", "3d"].map(v => <button key={v} aria-pressed={view === v} onClick={() => setView(v)}>{v === "3d" ? "3D model" : v === "aerial" ? "Aerial" : "Compare"}</button>)}</div><div className="review-stepper"><button aria-label="Previous roof" disabled={Boolean(saving) || index <= 0} onClick={() => move(-1)}><ChevronLeft size={16} /></button><span>{index >= 0 ? `${index + 1} / ${filtered.length}` : "Outside filter"}</span><button aria-label="Next roof" disabled={Boolean(saving) || index < 0 || index >= filtered.length - 1} onClick={() => move(1)}><ChevronRight size={16} /></button></div></div>
          <TrainingStatus key={cohort} cohort={cohort}/>
          {current.roof_form_selection && <p className="review-field-hint">Roof form: {current.roof_form_selection.source === "reviewer" ? "reviewer correction" : "image comparison"}, {current.roof_form_selection.gable_ends} gable ends, {current.roof_form_selection.ambiguous_ends} ends uncertain in imagery.</p>}
          {current.annotation_stale && <p className="review-notice">The model or boundary changed after your annotations. Previous edits remain in history; recheck the current geometry and object proposals.</p>}
          <div className="roof-editor-toolbar"><span>Geometry layer</span><div className="review-view-switch" aria-label="Geometry layer">{(["model","edited","overlay"] as const).map(mode=><button key={mode} aria-pressed={layer===mode} onClick={()=>{setLayer(mode);if(mode==="overlay"){setEditGeometry(true);if(view==="3d")setView("compare");}}}>{mode==="model"?"Model prediction":mode==="edited"?"Your edits":"Overlay both"}</button>)}</div><button className="review-button" aria-pressed={editGeometry} disabled={!current.imagery_available} onClick={()=>{setEditGeometry(!editGeometry);setLayer("edited");if(view==="3d")setView("compare");}}>{editGeometry?"Inspect imagery":"Edit apexes & objects"}</button><span className="review-muted">{draft.annotation?"Saved or draft geometry correction.":"Using model geometry."} {current.object_detection_status==="complete"?"Object detection complete.":"Objects not yet scanned."}</span></div>
          <div className={`review-viewports view-${view}`}>
            {view !== "3d" && <section className="review-viewport"><div className="review-viewport-caption"><strong>Aerial evidence</strong><span>Inspect outlines and rooflines</span></div><div className={`review-canvas-wrap ${editGeometry?"is-editing":""}`}>{editGeometry && model ? <RoofAnnotationEditor key={`editor:${key}`} imageUrl={current.site_image_url} ring={current.ring_meters} pixels={current.ring_pixels||[]} model={model} value={draft.annotation||model} objects={draft.objects||[]} mode={layer} disabled={busy} onChange={annotation=>update({annotation,apex_verified:false,roof_form_verified:false,status:"needs_adjustment"})} onObjectsChange={objects=>update({objects,objects_verified:false,object_reviewed_classes:[],status:"needs_adjustment"})}/> : <RoofReviewCanvas key={`canvas:${key}`} siteImageUrl={current.imagery_available ? current.site_image_url : undefined} maskImageUrl={current.imagery_available ? current.mask_image_url : undefined} signals={current.signals} reflection={current.reflection} ringMeters={current.ring_meters} ringPixels={current.ring_pixels} propertyBoundaryMeters={current.property_boundary_meters} nearbyPins={current.nearby_pins} treeCanopiesMeters={current.tree_canopies_meters} neighbourBuildingsMeters={current.neighbour_buildings_meters} parts={displayedParts} lines={displayedLines} rejectedCuts={draft.rejected_cuts} onRejectCut={cut => { if (!busy && !draft.rejected_cuts.some(c => JSON.stringify(c) === JSON.stringify(cut))) update({ rejected_cuts: [...draft.rejected_cuts, cut], status: "needs_adjustment" }); }} onRestoreCuts={() => { if (!busy) update({ rejected_cuts: [] }); }} />}</div></section>}
            {view !== "aerial" && <section className="review-viewport"><div className="review-viewport-caption"><strong>Roof reconstruction</strong><span>Drag to orbit, scroll to zoom</span></div><div className="review-canvas-wrap"><Roof3DPreview key={`model:${key}`} parts={displayedParts} ringMeters={current.ring_meters} roofMaterial={current.roof_material} roofType={current.roof_type} lines={displayedLines} eaveHeight={current.eave_height} ridgeHeight={current.ridge_height} propertyBoundaryMeters={current.property_boundary_meters} /></div></section>}
          </div>
          <div className="review-evidence-note"><span>Imagery scores are automated checks, not measured accuracy.</span><span>{current.property_boundary_meters ? "Property boundary supplied" : "No surveyed property boundary supplied"}</span></div>
          <section className="review-assessment" aria-label="Review assessment">
            <div className="review-section-heading"><h2>Assessment</h2><span className="review-muted">{dirty ? "Unsaved changes" : current.feedback?.updated_at ? `Saved ${new Date(current.feedback.updated_at).toLocaleString("en-AU")}${current.feedback.updated_by ? ` by ${current.feedback.updated_by}` : ""}` : "Not yet reviewed"}</span></div>
            <fieldset disabled={busy} className="review-fields"><legend className="sr-only">Roof assessment</legend><div className="review-assessment-grid"><div>
              <label htmlFor="assessment-status">Review status</label><select id="assessment-status" value={draft.status} onChange={e => update({ status: e.target.value as ReviewStatus })}>{Object.entries(labels).map(([value, label]) => <option key={value} value={value} disabled={value === "approved" && (!draft.boundary_verified || shapeIssues.length > 0 || draft.issues.length > 0 || draft.rejected_cuts.length > 0 || ["legacy-unverified", "manual-template"].includes(current.geometry_source))}>{label}</option>)}</select>
              <label className="review-checkbox"><input type="checkbox" checked={draft.boundary_verified} onChange={e => update({ boundary_verified: e.target.checked })} /><span>I checked this roof boundary against imagery.</span></label>
              <p className="review-field-hint">Approval needs a boundary check and no unresolved issues. Heights remain estimates.</p>
            </div><div><span className="review-field-label">Flag for correction</span><div className="review-issues">{REVIEW_ISSUES.map(issue => <label key={issue} className={`review-issue ${draft.issues.includes(issue) ? "is-selected" : ""}`}><input type="checkbox" checked={draft.issues.includes(issue)} onChange={e => update({ issues: e.target.checked ? [...draft.issues, issue] : draft.issues.filter(i => i !== issue), status: "needs_adjustment" })} />{issueLabels[issue]}</label>)}</div><label htmlFor="review-notes">Reviewer notes</label><textarea id="review-notes" className="resize-none" rows={2} maxLength={5000} placeholder="Describe the correction, e.g. ridge apex sits too far east…" value={draft.notes} onChange={e => update({ notes: e.target.value })} /></div></div></fieldset>
            <label className="review-checkbox"><input type="checkbox" checked={draft.roof_form_verified||false} disabled={busy} onChange={e=>update({roof_form_verified:e.target.checked,annotation:draft.annotation||model||undefined})}/>I checked the roof family (gable, hip, flat or mixed).</label>
            <label className="review-checkbox"><input type="checkbox" checked={draft.apex_verified||false} disabled={busy||shapeIssues.length>0} onChange={e=>update({apex_verified:e.target.checked,annotation:draft.annotation||model||undefined})}/>I checked the ridge positions and roof shape. Heights are estimates.</label>
            <div className="review-issues" aria-label="Object review coverage">{(["solar_panel","skylight","chimney"] as const).map(kind=><label className="review-checkbox" key={kind}><input type="checkbox" checked={draft.object_reviewed_classes?.includes(kind)||false} disabled={busy||(draft.objects||[]).some(o=>o.kind===kind&&o.status==="candidate")} onChange={e=>{const classes=e.target.checked?[...(draft.object_reviewed_classes||[]),kind]:(draft.object_reviewed_classes||[]).filter(k=>k!==kind);update({object_reviewed_classes:classes,objects_verified:classes.length>0});}}/>Checked all {kind==="solar_panel"?"solar arrays":kind==="skylight"?"skylights":"chimneys"}, including misses.</label>)}</div>
            {Boolean(current.feedback?.history?.length)&&<details className="review-notice"><summary>Saved revision history ({current.feedback?.history?.length})</summary>{current.feedback?.history?.slice().reverse().map(revision=><p key={revision.revision}>Revision {revision.revision} saved {revision.updated_at}{revision.updated_by ? ` by ${revision.updated_by}` : ""} <button className="review-button" disabled={busy||Boolean(revision.annotation&&revision.model_signature!==current.model_signature)} onClick={()=>update({annotation:revision.annotation,objects:revision.objects,notes:revision.notes,apex_verified:false,roof_form_verified:false,objects_verified:false,object_reviewed_classes:[],status:"needs_adjustment"})}>Restore as draft</button></p>)}</details>}
            <div className="review-save-row"><p className={message?.error ? "review-error" : "review-save-message"} role={message?.error ? "alert" : "status"}>{message?.text || (draft.rejected_cuts.length ? `${draft.rejected_cuts.length} rejected cut${draft.rejected_cuts.length > 1 ? "s" : ""} will be excluded on the next extraction run. The current mesh is unchanged.` : "Corrections improve the reference dataset. Save each assessment when ready.")}</p><div><button className="review-button" disabled={!dirty || busy} onClick={discard}>Discard draft</button><button className="review-button review-primary" disabled={!dirty || Boolean(saving)} onClick={save}>{busy ? <Loader2 size={15} className="animate-spin" /> : <Check size={15} />}<span>{busy ? "Saving review…" : "Save review"}</span></button></div></div>
          </section>
        </>}
      </main>
    </div>
  </div>;
}
