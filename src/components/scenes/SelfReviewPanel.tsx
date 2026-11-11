"use client";

import { memo, useEffect, useState } from "react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { ChevronLeft, ChevronRight, LoaderCircle, Play, RefreshCw } from "lucide-react";
import { roofImageLines, ROOF_LINE_COLORS } from "@/lib/scene-roof-lines";
import { scenePathData, type SceneModelSnapshot, type SceneObject } from "@/lib/scene-types";
import LotRoofReview, { type LotReview } from "./LotRoofReview";
import BuildingAddressSummary from "./BuildingAddressSummary";
import { buildingAddressTitle, type BuildingAddress } from "@/lib/building-addresses";

const Scene3D = dynamic(() => import("./Scene3D"), { ssr: false });
const names: Record<string, string> = { pending: "Queued", running: "Checking", candidate_ready: "Candidate ready", no_safe_gain: "No safe gain", needs_evidence: "Needs clearer evidence", skipped: "Protected / changed", failed: "Failed", complete: "Batch complete", interrupted: "Interrupted", idle: "Ready" };
type Audit = { geometry_passed: boolean; fixed_observations: number; boundary_edge_distance_px: number;
  cost?: { crease_runs: number; image_line_mismatch_m: number; supported_fold_fraction: number } };
type Triage = { primary: string; actions: { code: string; title: string; reason: string; action: string }[];
  fuller_captures?: { scene_id: string; object_id: string; capture_title: string; review_url: string; target_coverage: number; note: string }[] };
type Reflection = { title: string; observed: string; question: string; next_experiment: string; category: string };
type Feedback = { state: string; changed_fields: string[]; revision?: number; updated_at?: string };
type BoundaryReview = { accepted: boolean; output_vertices?: number; max_visible_deviation_m?: number; reason: string };
type FamilyReview = { reason: string; boundary_review?: BoundaryReview; alternatives: { family: string; label: string; object: SceneObject; geometry_passed: boolean; reason: string }[] };
type PropertyReview = { status: string; reason: string; plan?: string; numbered_lots?: number; complete_in_image?: boolean;
  lot_review?: LotReview;
  members: { object_id: string; flat_hypothesis?: SceneObject | null; protected?: boolean; boundary_review?: BoundaryReview }[];
  perimeter: number[][][][]; lots: { pfi: string; lot: string; polygons: number[][][][]; horizontal_uncertainty_m: number | null }[];
  source?: { name: string; url: string; attribution: string; license: string } };
type UnitReview = { status: string; reason: string; estimated_sections?: number; boundary_uncertainty_m?: number;
  detector?: string; cuts_pixels: number[][][]; fixtures: { point: number[]; kind: string }[] };
type ImageryReview = { ground_pixel_m: number; acquisition_date: string | null; provider: string; enhancement: string; reason: string };
const feedbackNames: Record<string, string> = { awaiting_review: "Awaiting your review", feedback_saved: "Feedback saved", needs_recovery: "Feedback needs recovery", source_changed: "Source changed", unavailable: "Feedback unavailable" };
const focusNames: Record<string, string> = { data: "Outline / connections", shape: "Roof shape", imagery: "Unclear imagery", solar: "Solar / surface lines" };
type Row = { scene_id: string; object_id: string; source_model: string; label: string; capture_title: string; review_url: string;
  property_address?: BuildingAddress;
  status: string; reason?: string; candidate_band?: string; source_band: string; strategy?: string; before?: Audit; after?: Audit;
  triage?: Triage; rejected_preview?: { object: SceneObject; audit: Audit; strategy: string; reason: string; eligible: false };
  reflection?: Reflection; reflection_warning?: string; feedback?: Feedback;
  family_review?: FamilyReview; unit_review?: UnitReview; imagery_review?: ImageryReview; property_review?: PropertyReview;
  attempts?: { strategy: string; eligible: boolean; reason: string }[] };
type Batch = { run: string | null; status: string; total?: number; completed?: number; counts: Record<string, number>; rows: Row[]; error?: string; selection_skips?: Record<string, number> };
type Detail = { row: Row; scene: SceneModelSnapshot; candidate: SceneObject | null; image_url: string; stale: boolean };

export default function SelfReviewPanel({ live, refreshToken }: { live: boolean; refreshToken: number }) {
  const [batch, setBatch] = useState<Batch | null>(null), [error, setError] = useState("");
  const [starting, setStarting] = useState(false), [refresh, setRefresh] = useState(0);
  const [focus, setFocus] = useState("all"), [awaitingOnly, setAwaitingOnly] = useState(false);
  const [selected, setSelected] = useState<number | null>(null), [loadedDetail, setLoadedDetail] = useState<{ key: string; value: Detail } | null>(null);
  const [failedDetail, setFailedDetail] = useState<{ key: string; message: string } | null>(null), [enhanced, setEnhanced] = useState(false), [mode, setMode] = useState("lines");
  const [comparison, setComparison] = useState("candidate"), [showUnits, setShowUnits] = useState(false);
  const [showBlock, setShowBlock] = useState(true), [showLots, setShowLots] = useState(false);
  const [preferLots, setPreferLots] = useState(true);
  useEffect(() => {
    const controller = new AbortController(); let busy = false;
    const load = async () => {
      if (busy) return; busy = true;
      try {
        const r = await fetch("/api/scenes/self-review", { cache: "no-store", signal: controller.signal });
        const data = await r.json(); if (!r.ok) throw new Error(data.error);
        if (!controller.signal.aborted) { setBatch(data); setError(""); }
      } catch (e) { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : "Could not load self-review."); }
      finally { busy = false; }
    };
    const onFocus = () => { void load(); };
    window.addEventListener("focus", onFocus);
    void load(); const timer = live ? setInterval(() => { if (!document.hidden) void load(); }, 5000) : undefined;
    return () => { controller.abort(); clearInterval(timer); window.removeEventListener("focus", onFocus); };
  }, [refresh, live, refreshToken]);
  const run = batch?.run, rowStatus = selected === null ? null : batch?.rows[selected]?.status;
  const selectedFeedback = selected === null ? null : batch?.rows[selected]?.feedback;
  const selectedAddress = selected === null ? undefined : batch?.rows[selected]?.property_address;
  const detailKey = `${run}:${selected}:${rowStatus}:${selectedFeedback?.state}:${selectedFeedback?.revision}:${selectedAddress?.decision_revision}:${selectedAddress?.evidence_binding}`;
  const detail = loadedDetail?.key === detailKey ? loadedDetail.value : null;
  const lotReview = detail?.row.property_review?.lot_review;
  const hasLotResults = Boolean(lotReview && ["complete", "partial"].includes(lotReview.status) && lotReview.lots.length);
  const useLots = Boolean(preferLots && hasLotResults);
  const flat = detail?.row.family_review?.alternatives.find(a => a.family === "flat");
  const useFlat = Boolean(flat && (comparison === "flat" || (!detail?.candidate && !detail?.row.rejected_preview)));
  const preview = useFlat ? flat?.object : detail?.candidate || detail?.row.rejected_preview?.object;
  const previewAudit = useFlat ? undefined : detail?.row.after || detail?.row.rejected_preview?.audit;
  const previewLabel = useFlat ? "Flat / low-pitch hypothesis" : detail?.candidate ? "Candidate" : preview ? "Rejected hypothesis" : "No comparison available";
  const detailError = failedDetail?.key === detailKey ? failedDetail.message : "";
  useEffect(() => {
    if (selected === null || !run) return;
    const controller = new AbortController();
    void fetch(`/api/scenes/self-review?${new URLSearchParams({ run, object: String(selected) })}`, { signal: controller.signal })
      .then(async r => { const data = await r.json(); if (!r.ok) throw new Error(data.error); return data as Detail; })
      .then(data => { if (!controller.signal.aborted) { setLoadedDetail({ key: detailKey, value: data }); setFailedDetail(null); } })
      .catch(e => { if (!controller.signal.aborted) setFailedDetail({ key: detailKey, message: e.message }); });
    return () => controller.abort();
  }, [selected, run, detailKey]);
  async function start() {
    setStarting(true); setError("");
    try {
      const response = await fetch("/api/scenes/self-review", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ limit: 12, ...(batch?.status === "interrupted" ? { resume: batch.run } : {}) }) });
      const result = await response.json(); if (!response.ok) throw new Error(result.error);
      setBatch(result); setSelected(null); setFocus("all"); setAwaitingOnly(false); setRefresh(n => n+1);
    } catch (e) { setError(e instanceof Error ? e.message : "Could not start the batch."); }
    finally { setStarting(false); }
  }
  const running = ["running", "pending"].includes(batch?.status || "");
  const visibleRows = (batch?.rows || []).map((row, index) => ({ row, index })).filter(({ row }) =>
    (focus === "all" || row.reflection?.category === focus) && (!awaitingOnly || row.feedback?.state === "awaiting_review"));
  const selectedPosition = visibleRows.findIndex(item => item.index === selected);
  const savedCount = batch?.rows.filter(row => row.feedback?.state === "feedback_saved").length || 0;
  const annotatedCount = batch?.rows.filter(row => row.reflection).length || 0;
  return <section className="self-review" aria-label="Roof self-review loop">
    <div className="self-review-heading"><div><h2>Review low-evidence roofs</h2><p>Inspect defects, compare roof hypotheses and choose the next quality improvement.</p></div>
      <button className="processing-primary" disabled={starting || running || !batch} onClick={() => void start()}>{starting ? <LoaderCircle size={16} className="processing-spin"/> : <Play size={16}/>} {starting ? "Preparing review" : batch?.status === "interrupted" ? "Resume batch" : "Review next 12 roofs"}</button></div>
    <p className="processing-evidence-note">Candidates stay separate from the suburb scan and your saved edits. Better scores are diagnostic gains; training and validation still require reviewed labels.</p>
    {error && <div className="processing-alert" role="alert">{error}<button onClick={() => setRefresh(n => n+1)}><RefreshCw size={14}/>Retry</button></div>}
    {!batch ? <p role="status">Reading the self-review batch</p> : <>
      <div className="self-review-progress"><strong>{names[batch.status] || batch.status}</strong><span>{batch.completed || 0} / {batch.total || 0} roofs checked</span>
        <progress value={batch.completed || 0} max={batch.total || 1} aria-label="Self-review roofs checked"/>
        <span>{batch.counts.candidate_ready || 0} candidates, {batch.counts.needs_evidence || 0} need evidence, {batch.counts.no_safe_gain || 0} unchanged, {batch.counts.failed || 0} failed</span></div>
      {batch.error && <p role="alert">{batch.error}</p>}
      {!!batch.rows.length && <div className="self-review-shortlist"><div><h3>{annotatedCount ? "Your review shortlist" : "Batch review queue"}</h3><p>{annotatedCount ? `${annotatedCount} roofs have visual review notes. ` : ""}{savedCount} / {batch.rows.length} have saved feedback. Saved feedback still needs evaluation before training.</p></div>
        <label>Review focus<select aria-label="Review focus" value={focus} onChange={e => { setFocus(e.target.value); setSelected(null); }}><option value="all">All issues</option>{Object.entries(focusNames).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        <label><input type="checkbox" checked={awaitingOnly} onChange={e => { setAwaitingOnly(e.target.checked); setSelected(null); }}/>Awaiting my review</label>
        <button className="processing-secondary" onClick={() => setRefresh(n => n+1)}><RefreshCw size={14}/>Refresh feedback</button></div>}
      {!batch.rows.length ? <div className="processing-empty"><h3>{batch.run ? "No new eligible roofs in this batch" : "A second look at low-evidence roofs"}</h3><p>Checks use unreviewed detector roofs from training areas. Saved feedback and validation/test areas are protected.</p></div> : !visibleRows.length ? <div className="processing-empty">No roofs match these review filters.</div> : <div className="processing-table-scroll"><table className="processing-table"><caption className="sr-only">Self-review candidate results</caption><thead><tr><th>Roof / review focus</th><th>Outcome</th><th>Evidence band</th><th>Reason</th><th>Your feedback</th></tr></thead><tbody>{visibleRows.map(({row: r, index: i}) => <tr key={r.scene_id+r.object_id} className={i === selected ? "is-selected" : ""}>
        <td><button className="processing-roof-name" onClick={() => setSelected(i)}><strong>{buildingAddressTitle(r.property_address)}</strong><span>{r.reflection?.title || r.label}, {r.object_id.slice(0, 8)}</span><small>Capture: {r.capture_title}</small></button></td>
        <td>{names[r.status] || r.status}</td><td>{r.source_band}{r.candidate_band && ` to ${r.candidate_band}`}</td><td>{r.triage?.actions[0]?.title && <strong className="self-review-next-action">{r.triage.actions[0].title}</strong>}{r.reason || "Waiting for the bounded candidate search"}</td><td>{feedbackNames[r.feedback?.state || "awaiting_review"] || "Awaiting your review"}</td></tr>)}</tbody></table></div>}
      {selected !== null && <div className="self-review-detail" aria-live="polite">{detailError ? <p role="alert">{detailError}</p> : !detail ? <p>Reading the roof comparison</p> : <>
        <div className="self-review-heading"><div><h3>{buildingAddressTitle(detail.row.property_address)}</h3><p>{detail.row.label}, {detail.row.object_id.slice(0, 8)}</p><p>{detail.row.reason}</p></div><Link className="processing-secondary" href={detail.row.review_url}>Open source roof</Link></div>
        <BuildingAddressSummary key={`${detail.row.scene_id}/${detail.row.object_id}`} address={detail.row.property_address} sceneId={detail.row.scene_id} objectId={detail.row.object_id}/>
        <div className="self-review-navigation"><button disabled={selectedPosition <= 0} onClick={() => setSelected(visibleRows[selectedPosition-1].index)}><ChevronLeft size={14}/>Previous roof</button><span>{selectedPosition+1} / {visibleRows.length}</span><button disabled={selectedPosition < 0 || selectedPosition >= visibleRows.length-1} onClick={() => setSelected(visibleRows[selectedPosition+1].index)}>Next roof<ChevronRight size={14}/></button></div>
        {detail.row.reflection_warning && <p role="alert">{detail.row.reflection_warning}</p>}
        {detail.row.reflection && <div className="self-review-human"><h4>{detail.row.reflection.title}</h4><p><strong>What I noticed:</strong> {detail.row.reflection.observed}</p><p className="self-review-question"><strong>Please check:</strong> {detail.row.reflection.question}</p><p><strong>Next modelling experiment:</strong> {detail.row.reflection.next_experiment}</p><small>Agent observations for you to verify; these are not approved labels.</small></div>}
        <div className="self-review-feedback"><div><strong>{feedbackNames[detail.row.feedback?.state || "awaiting_review"]}</strong><p>{detail.row.feedback?.state === "feedback_saved" ? `Saved changes: ${detail.row.feedback.changed_fields.map(field => field.replaceAll("_", " ")).join(", ")}.` : "Edit the outline or roof lines, flag issues and save in the roof editor. Return here to see the saved feedback."}</p></div><Link className="processing-primary" href={detail.row.review_url.replace("layer=model", "layer=edited")}>Edit and save feedback</Link></div>
        {detail.stale && <p role="alert">The source model changed. This comparison belongs to the frozen previous version.</p>}
        {detail.row.triage && <div className="self-review-guidance"><h4>Recommended next steps</h4><ul>{detail.row.triage.actions.map(action => <li key={action.code}><strong>{action.title}</strong><p>{action.reason} {action.action}</p></li>)}</ul>
          {!!detail.row.triage.fuller_captures?.length && <><h4>Possible fuller captures</h4><p>Check that these show the same roof before continuing. Overlap is a navigation suggestion.</p><ul>{detail.row.triage.fuller_captures.map(capture => <li key={capture.scene_id+capture.object_id}><Link href={capture.review_url}>{capture.capture_title}, {capture.object_id.slice(0, 8)}</Link></li>)}</ul></>}
        </div>}
        {detail.row.property_review && <div className="self-review-guidance"><h4>Property context{detail.row.property_review.plan ? `, ${detail.row.property_review.plan}` : ""}</h4>
          <p>{detail.row.property_review.reason}</p>
          {!!detail.row.property_review.members.length && <><p>{detail.row.property_review.members.length} detected roof surfaces, {detail.row.property_review.numbered_lots} numbered cadastral lots. Common property is included in the block perimeter.</p>
            <div className="self-review-tools">{!!lotReview?.lots.length && <button disabled={!hasLotResults} aria-pressed={useLots} onClick={() => setPreferLots(true)}>Per-lot roof searches</button>}<button aria-pressed={!useLots && !showBlock} onClick={() => { setPreferLots(false); setShowBlock(false); }}>Selected roof</button><button aria-pressed={!useLots && showBlock} onClick={() => { setPreferLots(false); setShowBlock(true); }}>Whole property block</button>
              {!useLots && <label><input type="checkbox" checked={showLots} onChange={e => setShowLots(e.target.checked)}/>Show cadastral lot divisions</label>}</div>
            {lotReview?.status === "not_run" && <p>Per-lot search regions are prepared. SAM extraction has not run for this site yet.</p>}
            <div className="self-review-tools">{detail.row.property_review.members.map((member, i) => <Link key={member.object_id} className="processing-secondary" href={`/scenes?${new URLSearchParams({scene: detail.scene.id, object: member.object_id, kind: "building", status: "all", layer: "edited"})}`}>Review roof {i+1}, {member.object_id.slice(0, 8)}</Link>)}</div>
            {!detail.row.property_review.complete_in_image && <p role="note">The block extends beyond this capture or lookup. Additional roofs may be missing.</p>}
            <p>Yellow dashed lines are land boundaries; they do not add folds to a roof. {detail.row.property_review.lots.some(l => l.horizontal_uncertainty_m !== null) && `Reported parcel position uncertainty reaches ${Math.max(...detail.row.property_review.lots.map(l => l.horizontal_uncertainty_m || 0)).toFixed(2)} m.`}</p>
            {detail.row.property_review.source && <small><a href={detail.row.property_review.source.url}>{detail.row.property_review.source.name}</a>, {detail.row.property_review.source.attribution}, {detail.row.property_review.source.license}</small>}
          </>}
        </div>}
        {!useLots && detail.row.family_review && <div className="self-review-guidance"><h4>Roof family comparison</h4><p>{detail.row.family_review.reason}</p>
          <div className="self-review-tools"><button aria-pressed={!useFlat} onClick={() => setComparison("candidate")} disabled={!detail.candidate && !detail.row.rejected_preview}>Pitched hypothesis</button>
            <button aria-pressed={useFlat} onClick={() => setComparison("flat")} disabled={!flat}>Flat / low-pitch hypothesis</button></div>
          {useFlat && <><p>{flat?.reason} The level preview does not estimate drainage falls or stepped roof levels.</p>
            {detail.row.family_review.boundary_review?.accepted && <p>Straight-edge proposal: {detail.row.family_review.boundary_review.output_vertices} boundary corners. Maximum movement from the visible raw outline: {detail.row.family_review.boundary_review.max_visible_deviation_m?.toFixed(2)} m. Check setbacks and roof ends before saving feedback.</p>}</>}</div>}
        {!useLots && detail.row.unit_review && <div className="self-review-guidance"><h4>Attached units, estimated divisions</h4><p>{detail.row.unit_review.reason}</p>
          {detail.row.unit_review.estimated_sections && <p>{detail.row.unit_review.estimated_sections} possible sections, boundary uncertainty ±{detail.row.unit_review.boundary_uncertainty_m} m (heuristic range, not a confidence interval).</p>}
          <label><input type="checkbox" checked={showUnits} onChange={e => setShowUnits(e.target.checked)}/>Show fixture candidates and suggested unit divisions</label>
          <p>Orange marks are {detail.row.unit_review.detector === "sam3-equipment-candidates" ? "model air-conditioner / exhaust candidates" : "unclassified fixture candidates"}; verify them in the image. Purple dashed divisions leave the continuous roof surface intact; they are not roof folds or confirmed unit boundaries.</p></div>}
        {!useLots && !useFlat && !detail.candidate && detail.row.rejected_preview && <p className="processing-alert" role="note"><strong>Rejected hypothesis, {detail.row.rejected_preview.strategy}</strong><br/>{detail.row.rejected_preview.reason} Shown for diagnosis only; it did not pass the candidate checks.</p>}
        <div className="self-review-tools"><label><input type="checkbox" checked={enhanced} onChange={e => setEnhanced(e.target.checked)}/>Enhance existing detail</label>
          {!useLots && [["aerial", "Aerial"], ["lines", "Roof lines"], ["3d", "3D"]].map(([v, label]) => <button key={v} aria-pressed={mode === v} onClick={() => setMode(v)}>{label}</button>)}</div>
        {useLots && lotReview ? <LotRoofReview key={`${run}:${selected}`} review={lotReview} scene={detail.scene} imageUrl={detail.image_url} enhanced={enhanced}/> : <div className="self-review-comparison">{[detail.scene.objects[0], preview].map((o, i) => <div key={i}><h4>{i ? previewLabel+(preview ? ", not applied" : "") : "Original model"}</h4>{o ? <CandidateView object={o} detail={detail} enhanced={enhanced} mode={mode} showUnits={showUnits} showBlock={showBlock} showLots={showLots} flatBlock={Boolean(i && useFlat)}/> : <div className="processing-empty">More evidence is needed before comparing another shape.</div>}</div>)}</div>}
        <p className="processing-evidence-note">Enhancement changes contrast only, at the original pixel registration. It does not recover hidden roof detail. Heights remain estimates.</p>
        {detail.row.imagery_review && <div className="self-review-guidance"><h4>Image evidence</h4><p>{Math.round(detail.row.imagery_review.ground_pixel_m*100)} cm per displayed ground pixel, {detail.row.imagery_review.provider}, acquisition date {detail.row.imagery_review.acquisition_date || "unknown"}</p><p>{detail.row.imagery_review.reason}</p></div>}
        {!useLots && detail.row.before && <table className="processing-table"><thead><tr><th>Diagnostic</th><th>Original</th><th>{previewLabel}</th></tr></thead><tbody>
          {([['Geometry checks', (a: Audit) => a.geometry_passed ? 'Pass' : 'Needs review'], ['Fold runs', (a: Audit) => a.cost?.crease_runs ?? '—'], ['Folds supported by image lines', (a: Audit) => a.cost ? `${Math.round(a.cost.supported_fold_fraction*100)}% of length` : '—'], ['Image-line residual', (a: Audit) => a.cost ? `${a.cost.image_line_mismatch_m.toFixed(2)} m` : '—']] as [string, (a: Audit) => string | number][]).map(([label, value]) => <tr key={label}><th>{label}</th><td>{value(detail.row.before!)}</td><td>{previewAudit ? value(previewAudit) : '—'}</td></tr>)}
        </tbody></table>}
        {!!detail.row.attempts?.length && <details><summary>Why alternatives passed or failed</summary><ul>{detail.row.attempts.map((a, i) => <li key={i}><strong>{a.strategy}</strong>: {a.reason}</li>)}</ul></details>}
      </>}</div>}
      {!!Object.keys(batch.selection_skips || {}).length && <details className="self-review-skips"><summary>Protected and previously checked inputs</summary><ul>{Object.entries(batch.selection_skips || {}).map(([reason, n]) => <li key={reason}>{reason}: {n}</li>)}</ul></details>}
    </>}
  </section>;
}

const CandidateView = memo(function CandidateView({ object, detail, enhanced, mode, showUnits, showBlock, showLots, flatBlock }: { object: SceneObject; detail: Detail; enhanced: boolean; mode: string; showUnits: boolean; showBlock: boolean; showLots: boolean; flatBlock: boolean }) {
  const scene = detail.scene;
  const property = showBlock && detail.row.property_review?.members.length ? detail.row.property_review : null;
  const objects = property ? scene.objects.map(o => o.id === object.id ? object : (flatBlock && property.members.find(m => m.object_id === o.id)?.flat_hypothesis) || o) : [object];
  const ring = property ? [...scene.objects.flatMap(o => o.ring), ...property.perimeter.flat(2)] : [...scene.objects[0].ring, ...object.ring, ...(detail.candidate?.ring || detail.row.rejected_preview?.object.ring || [])];
  const xs = ring.map(p => p[0]), ys = ring.map(p => p[1]), span = Math.max(Math.max(...xs)-Math.min(...xs), Math.max(...ys)-Math.min(...ys))*1.2;
  const viewBox = `${(Math.max(...xs)+Math.min(...xs)-span)/2} ${(Math.max(...ys)+Math.min(...ys)-span)/2} ${span} ${span}`;
  if (mode === "3d") return <div className="processing-model self-review-model"><Scene3D key={objects.map(o => o.id).join(":")} scene={scene} objects={objects} selected={object.id} visible={new Set(["building"])} objectIds={new Set(objects.map(o => o.id))} groupIds={property ? objects.map(o => o.id) : undefined} onSelect={() => {}} disabled/></div>;
  return <svg viewBox={viewBox} role="img" aria-label={`${mode === "aerial" ? "Original aerial" : "Roof geometry"}: ${object.label}`}>
    <image href={detail.image_url+(enhanced ? "&view=roof-detail-v1" : "")} width={scene.image_width} height={scene.image_height}/>
    {property && <g aria-label="Cadastral block perimeter">{(showLots ? property.lots.flatMap(l => l.polygons) : property.perimeter).map((polygon, i) => <path key={i} d={polygon.map(ring => `M${ring.map(p => p.join(",")).join("L")}Z`).join(" ")} fill="none" stroke="#fbbf24" strokeWidth={showLots ? 1 : 2} strokeDasharray="6 4" vectorEffect="non-scaling-stroke"/>)}</g>}
    {mode !== "aerial" && objects.map(roof => <g key={roof.id}><path d={scenePathData(roof)} fill="none" stroke="#67d7e7" strokeWidth="1.5" vectorEffect="non-scaling-stroke"/>
      {roofImageLines(roof, scene.meters_to_pixel).map((line, i) => <line key={i} x1={line.a[0]} y1={line.a[1]} x2={line.b[0]} y2={line.b[1]} stroke={ROOF_LINE_COLORS[line.kind]} strokeWidth="1.5" strokeDasharray={line.kind === "valley" ? "4 3" : undefined} vectorEffect="non-scaling-stroke"/>)}</g>)}
    {showUnits && detail.row.unit_review && <g aria-label="Unverified unit partition hypotheses">
      {detail.row.unit_review.cuts_pixels.map((cut, i) => <line key={`cut-${i}`} x1={cut[0][0]} y1={cut[0][1]} x2={cut[1][0]} y2={cut[1][1]} stroke="#f0abfc" strokeWidth="2" strokeDasharray="7 5" vectorEffect="non-scaling-stroke"/>)}
      {detail.row.unit_review.fixtures.map((fixture, i) => <circle key={`fixture-${i}`} cx={fixture.point[0]} cy={fixture.point[1]} r={span*.008} fill="none" stroke="#fbbf24" strokeWidth="1.5" vectorEffect="non-scaling-stroke"/>)}</g>}
  </svg>;
});
