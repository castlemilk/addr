"use client";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import Link from "next/link";
import { ArrowUpRight } from "lucide-react";
import { CLASS_NAMES, scenePathData } from "@/lib/scene-types";
import { candidateAssessment, CANDIDATE_ASSESSMENT_LABELS, candidateReviewURL, type CandidateScene, type CandidateSummary } from "@/lib/scene-candidates";
import { getCrmToken } from "@/lib/crm-client";
import "../../reviews/reviews.css";
import "../scenes.css";
import "../training/training.css";
import "./candidates.css";
// After the page CSS so shell.css (via WorkbenchNav) wins the equal-specificity .review-header row.
import { WorkbenchHeader } from "@/components/shell/WorkbenchHeader";

const subscribe = (notify: () => void) => { window.addEventListener("popstate", notify); return () => window.removeEventListener("popstate", notify); };
const locationSearch = () => window.location.search;
const serverSearch = () => "";
async function read<T>(url: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(url, { cache: "no-store", signal: AbortSignal.any([signal, AbortSignal.timeout(30000)]) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Candidate data is unavailable. Please retry.");
  return data;
}

export default function CandidatePage() {
  const query = new URLSearchParams(useSyncExternalStore(subscribe, locationSearch, serverSearch));
  const [catalog, setCatalog] = useState<{ candidates: CandidateSummary[]; unavailable: number } | null>(null);
  const [catalogError, setCatalogError] = useState("");
  const [attempt, retry] = useState(0);
  const candidateId = query.get("candidate") || catalog?.candidates?.[0]?.id || "";
  const candidate = catalog?.candidates?.find(c => c.id === candidateId);
  const checkId = query.get("check") || (!query.has("scene") ? candidate?.checks?.[0]?.id : "") || "";
  const check = candidate?.checks?.find(c => c.id === checkId);
  const available = candidate && (!checkId || check);
  const layers = available ? check?.scenes || candidate.scenes : [];
  const before = check?.before || candidate?.before.validation, after = check?.after || candidate?.after.validation;
  const assessment = before && after ? candidateAssessment(before, after) : null;
  const laterLoss = !checkId || assessment?.status !== "loses-matches"
    ? candidate?.checks?.find(c => c.id !== checkId && candidateAssessment(c.before, c.after).status === "loses-matches") : undefined;
  const sceneId = query.get("scene") || layers.find(s => s.split === "validation")?.id || layers[0]?.id || "";
  const [loaded, setLoaded] = useState<{ key: string; data?: CandidateScene; error?: string } | null>(null);
  const key = `${candidateId}:${checkId}:${sceneId}:${attempt}`;
  const scene = loaded?.key === key ? loaded.data : undefined, error = loaded?.key === key ? loaded.error : "";
  const selected = query.get("proposal");
  const visibility = ["kept", "filtered"].includes(query.get("show") || "") ? query.get("show")! : "all";
  const [outlines, showOutlines] = useState(true), [annotations, showAnnotations] = useState(false), [focused, focus] = useState(false);
  const [imageError, setImageError] = useState("");
  const [saving, setSaving] = useState(false), [saveError, setSaveError] = useState("");
  const savingRef = useRef(false);
  useEffect(() => {
    const controller = new AbortController();
    read<{ candidates: CandidateSummary[]; unavailable: number }>("/api/scenes/candidates", controller.signal).then(data => {
      // A body without a candidate list is a broken response, not an empty catalogue; keep it out of state.
      if (!Array.isArray(data.candidates)) throw new Error("Candidate data is unavailable. Please retry.");
      if (!controller.signal.aborted) { setCatalog(data); setCatalogError(""); }
    }).catch(e => { if (!controller.signal.aborted) setCatalogError(e.name === "TimeoutError" ? "Loading candidates took too long. Please retry." : e.message); });
    return () => controller.abort();
  }, [attempt]);
  useEffect(() => {
    if (!candidateId || !sceneId || !available) return;
    const controller = new AbortController();
    read<CandidateScene>(`/api/scenes/candidates?${new URLSearchParams({ candidate: candidateId, scene: sceneId, ...(checkId ? { check: checkId } : {}) })}`, controller.signal).then(data => {
      if (!controller.signal.aborted) setLoaded({ key, data });
    }).catch(e => { if (!controller.signal.aborted) setLoaded({ key, error: e.name === "TimeoutError" ? "Loading this image took too long. Please retry." : e.message }); });
    return () => controller.abort();
  }, [candidateId, checkId, sceneId, available, key]);

  function choose(nextCandidate: string, nextScene: string, inspectLoss = false, nextCheck = checkId) {
    if (savingRef.current) return;
    window.history.replaceState(null, "", `/scenes/candidates?${new URLSearchParams({ candidate: nextCandidate, scene: nextScene, ...(nextCheck ? { check: nextCheck } : {}), ...(inspectLoss ? { show: "filtered", inspect: "loss" } : {}) })}`);
    window.dispatchEvent(new PopStateEvent("popstate"));
    focus(inspectLoss); setSaveError("");
  }
  function select(id: string | null, filter = visibility) {
    if (savingRef.current) return;
    const params = new URLSearchParams({ candidate: candidateId, scene: sceneId, show: filter, ...(checkId ? { check: checkId } : {}), ...(id ? { proposal: id } : {}) });
    window.history.replaceState(null, "", `/scenes/candidates?${params}`);
    window.dispatchEvent(new PopStateEvent("popstate"));
    setSaveError("");
  }
  const proposals = scene?.proposals.filter(p => visibility === "all" || (visibility === "kept" ? p.retained : !p.retained)) || [];
  const filteredMatch = scene?.proposals.find(p => !p.retained && p.frozen_match);
  const lossImage = layers.find(s => s.split === "validation" && s.lost_matches > 0);
  const current = proposals.find(p => selected === p.object.id) || (query.get("inspect") === "loss" ? proposals.find(p => !p.retained && p.frozen_match) : null) || proposals[0];
  const currentIndex = scene?.proposals.findIndex(p => p.object.id === current?.object.id) ?? -1;
  let viewBox = `0 0 ${scene?.width || 512} ${scene?.height || 512}`;
  if (focused && current && scene) {
    const xs = current.object.ring.map(p => p[0]), ys = current.object.ring.map(p => p[1]);
    const size = Math.min(Math.max(60, Math.max(...xs) - Math.min(...xs) + 40, Math.max(...ys) - Math.min(...ys) + 40), scene.width, scene.height);
    const x = Math.max(0, Math.min(scene.width - size, (Math.min(...xs) + Math.max(...xs) - size) / 2));
    const y = Math.max(0, Math.min(scene.height - size, (Math.min(...ys) + Math.max(...ys) - size) / 2));
    viewBox = `${x} ${y} ${size} ${size}`;
  }
  async function addDraft() {
    if (!scene || !current || savingRef.current) return;
    savingRef.current = true; setSaving(true); setSaveError("");
    try {
      const token = getCrmToken();
      const response = await fetch("/api/scenes/candidates", { method: "POST", signal: AbortSignal.timeout(30000),
        headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
        body: JSON.stringify({ candidate: candidateId, check: checkId || undefined, scene: sceneId, object: current.object.id, revision: scene.revision, model_signature: scene.model_signature }) });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Could not add the draft.");
      window.location.assign(result.review_url);
    } catch (e) {
      setSaveError(e instanceof Error && e.name !== "TimeoutError" ? e.message : "The response timed out. Retry safely to open the same draft.");
      savingRef.current = false; setSaving(false);
    }
  }
  // The shell header (a nav landmark) sits beside <main>, not inside it, as on the other workspaces.
  return <div className="review-shell scene-shell training-shell candidate-shell">
    {/* Shell header: Training and Scene review live in the nav; navigation stays blocked while a draft is being added. */}
    <WorkbenchHeader onNavigate={e => { if (saving) e.preventDefault(); }} />
    <main className="training-content">
      <div className="training-heading"><div><h1>Model candidates</h1>
        <p>Experimental image-based feedback. Compare proposed outlines with the image and your current review layer. Open a saved annotation to correct it, or add a new draft.</p></div></div>
      {catalogError ? <div className="review-notice" role="alert">{catalogError} <button className="review-button" onClick={() => retry(n => n + 1)}>Retry candidates</button></div> : !catalog ? <p role="status" className="candidate-message">Loading model candidates…</p> : !catalog.candidates?.length ?
        <div className="training-empty"><h2>No model candidates available</h2><p>{catalog.unavailable ? "Candidate files could not be verified. Check the training output and retry." : "Export reviewed labels and train a proposal candidate to inspect its results here."}</p><Link className="review-button" href="/scenes/training">Review training coverage</Link></div> : <>
        {!!catalog.unavailable && <p className="review-notice">{catalog.unavailable} candidate reports could not be verified and are unavailable.</p>}
        <div className="training-filters candidate-filters">
          <label>Trained candidate<select disabled={saving} value={candidateId} onChange={e => { const c = catalog.candidates.find(c => c.id === e.target.value)!; const next = c.checks?.[0]; choose(c.id, next?.scenes[0]?.id || c.scenes.find(s => s.split === "validation")?.id || c.scenes[0]?.id || "", false, next?.id || ""); }}>
            {!candidate && <option value={candidateId}>Unavailable candidate</option>}{catalog.candidates.map(c => <option key={c.id} value={c.id}>{CLASS_NAMES[c.kind]} {c.id.slice(0, 8)}{candidateAssessment(c.before.validation, c.after.validation).status === "loses-matches" || c.checks?.some(v => candidateAssessment(v.before, v.after).status === "loses-matches") ? ", loses matches" : ""}</option>)}
          </select></label>
          <label>Evaluation run<select disabled={saving || !candidate} value={checkId} onChange={e => { const next = candidate?.checks?.find(c => c.id === e.target.value); const options = next?.scenes || candidate?.scenes || []; choose(candidateId, options.find(s => s.split === "validation" && s.lost_matches > 0)?.id || options.find(s => s.split === "validation")?.id || options[0]?.id || "", false, e.target.value); }}>
            <option value="">Original evaluation</option>{checkId && !check && <option value={checkId}>Unavailable validation check</option>}{candidate?.checks?.map(c => <option key={c.id} value={c.id}>Later validation {c.id.slice(0, 8)}, {c.after.images} {c.after.images === 1 ? "image" : "images"}</option>)}
          </select></label>
          <label>Evaluation image<select disabled={saving || !available} value={sceneId} onChange={e => choose(candidateId, e.target.value)}>
            {!layers.some(s => s.id === sceneId) && <option value={sceneId}>Unavailable image</option>}{layers.map(s => <option key={s.id} value={s.id}>{s.id.replace("roof_training_250-", "Scene ")}, {s.split}: {s.retained} kept / {s.filtered} filtered{s.lost_matches ? `, ${s.lost_matches} lost ${s.lost_matches === 1 ? "match" : "matches"}` : ""}</option>)}
          </select></label>
        </div>
        {!!candidate?.unavailable_checks && <p className="review-notice" role="alert">{candidate.unavailable_checks} later validation {candidate.unavailable_checks === 1 ? "check could" : "checks could"} not be verified. Their results are unavailable.</p>}
        {laterLoss && <div className="candidate-assessment has-regression candidate-later-warning" role="status"><div><strong>Another validation check loses labelled objects</strong><p>{laterLoss.before.matched} matches before filtering, {laterLoss.after.matched} after, in {laterLoss.after.images} additional {laterLoss.after.images === 1 ? "image" : "images"}.</p></div><button className="review-button" disabled={saving} onClick={() => choose(candidateId, laterLoss.scenes.find(s => s.lost_matches > 0)!.id, true, laterLoss.id)}>Inspect later regression</button></div>}
        {!available ? <p className="review-notice" role="alert">{candidate ? "This validation check is unavailable. Choose another evaluation run." : "This candidate is unavailable. Choose another candidate."}</p> : <>
          <div className={`candidate-assessment ${assessment!.status === "loses-matches" ? "has-regression" : ""}`} role="status">
            <div><strong>{CANDIDATE_ASSESSMENT_LABELS[assessment!.status]}</strong><p>{check ? "Later validation" : "Validation"} matches: {before!.matched} of {after!.references} before filtering, {after!.matched} of {after!.references} after. Extra detections: {before!.extra} before, {after!.extra} after.</p></div>
            {lossImage && <button className="review-button" disabled={saving} onClick={() => { if (sceneId === lossImage.id && filteredMatch) { select(filteredMatch.object.id, "filtered"); focus(true); } else choose(candidateId, lossImage.id, true); }}>Inspect lost matches</button>}
          </div>
          {error ? <div className="review-notice" role="alert">{error} <button className="review-button" onClick={() => retry(n => n + 1)}>Retry image</button></div> : !scene ? <p role="status" className="candidate-message">Loading candidate outlines and saved annotations…</p> : <>
            <div className="candidate-scene-heading"><div><h2>{scene.title}</h2><p>{scene.split === "validation" ? "Validation" : "Training"} image, {scene.proposals.length} proposals, current review revision {scene.revision}</p></div>
              <Link className="review-button" href={candidateReviewURL(sceneId, candidate.kind)} onClick={e => { if (saving) e.preventDefault(); }}>Review whole image <ArrowUpRight size={14}/></Link></div>
            {(!scene.compatible || scene.stale) && <p className="review-notice" role="alert">{!scene.compatible ? "The current scene uses a different image or coordinate frame." : "The saved review belongs to an earlier model."} This shows the original experiment image. Open scene review to reconcile it before adding drafts.</p>}
            <div className="candidate-workspace">
              <section className="candidate-image-panel" aria-label="Candidate imagery">
                <div className="candidate-image-controls"><button className="review-button" aria-pressed={focused} disabled={!current || saving} onClick={() => focus(!focused)}>{focused ? "Whole image" : "Focus selected"}</button>
                  <label><input type="checkbox" checked={outlines} disabled={saving} onChange={e => showOutlines(e.target.checked)}/> Proposal outlines</label>
                  <label><input type="checkbox" checked={annotations} disabled={saving || !scene.compatible || scene.stale} onChange={e => showAnnotations(e.target.checked)}/> Current review layer</label></div>
                <svg className="candidate-image" viewBox={viewBox} role="group" aria-label="Image with candidate outlines">
                  <image key={scene.image_url} href={scene.image_url} width={scene.width} height={scene.height} onError={() => setImageError(scene.image_url)}/>
                  {annotations && scene.annotations.map(o => <path key={`saved-${o.id}`} d={scenePathData(o)} fill="none" stroke="white" strokeWidth="2" strokeDasharray="2 4" vectorEffect="non-scaling-stroke" pointerEvents="none"/>)}
                  {outlines && proposals.map(p => <path key={p.object.id} data-candidate-object={p.object.id} d={scenePathData(p.object)} fillRule="evenodd"
                    className={`${p.retained ? "candidate-kept" : "candidate-filtered"}${current?.object.id === p.object.id ? " is-selected" : ""}`}
                    role="button" tabIndex={saving ? -1 : 0} aria-label={`Select proposal ${scene.proposals.indexOf(p) + 1}, ${p.retained ? "kept" : "filtered"}`} aria-pressed={current?.object.id === p.object.id}
                    onClick={() => { if (!saving) select(p.object.id); }} onKeyDown={e => { if (!saving && ["Enter", " "].includes(e.key)) { e.preventDefault(); select(p.object.id); } }}/>) }
                </svg>
                {imageError === scene.image_url && <p className="review-notice" role="alert">The experiment image could not load. Reload before reviewing these outlines.</p>}
                <div className="candidate-legend"><span className="candidate-kept-key">Solid green: kept</span><span className="candidate-filtered-key">Dashed amber: filtered</span><span>White dots: current review layer</span></div>
              </section>
              <aside className="candidate-inspector" aria-label="Proposal inspection">
                <label>Show proposals<select value={visibility} disabled={saving} onChange={e => { select(null, e.target.value); focus(false); }}><option value="all">All proposals</option><option value="kept">Kept by candidate</option><option value="filtered">Filtered by candidate</option></select></label>
                {!current ? <div className="candidate-empty"><h3>No {visibility === "all" ? "" : visibility} proposals</h3><p>Inspect the whole image for missed objects. Add missing outlines in scene review.</p><button className="review-button" onClick={() => select(null, "all")}>Show all proposals</button></div> : <>
                  <div className="candidate-selection"><h3>Proposal {currentIndex + 1}</h3><span className={current.retained ? "status-approved" : "candidate-filtered-key"}>{current.retained ? "Kept" : "Filtered"}, score {current.quality_score.toFixed(3)}</span><p className="candidate-hint">Uncalibrated model score. Keep threshold {candidate.threshold.toFixed(2)}.</p>
                    {!current.retained && current.frozen_match && <p className="candidate-loss">Filtered a labelled object: {current.frozen_match.label}. It matched a reference in this frozen evaluation.</p>}
                    <p>{current.match ? `Closest current annotation: ${current.match.label}, ${current.match.status.replaceAll("_", " ")}, ${Math.round(current.match.iou * 100)}% outline overlap.` : "No current annotation has at least 50% outline overlap."}</p>
                    {current.match ? <Link className="review-button review-primary" href={candidateReviewURL(sceneId, candidate.kind, current.match.id)}>Open annotation to correct <ArrowUpRight size={14}/></Link> : <button className="review-button review-primary" disabled={saving || !scene.compatible || scene.stale || imageError === scene.image_url} onClick={() => void addDraft()}>{saving ? "Adding draft…" : "Add as review draft"}</button>}
                    <p className="candidate-hint">{current.match ? "Opens the existing outline in the scene editor. Its saved review stays intact." : "Adds this outline as needs edit, then opens the editor. Check it before confirming; the class will need another whole-image review."}</p>
                    {saveError && <p className="review-notice" role="alert">{saveError} <button className="review-button" onClick={() => retry(n => n + 1)}>Refresh candidate</button></p>}
                  </div>
                  <ul className="candidate-proposal-list" aria-label="Candidate proposals">{proposals.map(p => <li key={p.object.id}><button disabled={saving} aria-pressed={current.object.id === p.object.id} onClick={() => { select(p.object.id); setSaveError(""); }}><span>Proposal {scene.proposals.indexOf(p) + 1}</span><span>{p.retained ? "Kept" : "Filtered"}, {p.quality_score.toFixed(3)}</span></button></li>)}</ul>
                </>}
              </aside>
            </div>
          </>}
          <div className="candidate-evidence"><strong>Validation misses: {before!.missed} before filtering, {after!.missed} after</strong><span>{after!.images} {after!.images === 1 ? "image" : "images"}, {after!.groups} area {after!.groups === 1 ? "group" : "groups"}</span></div>
          <p className="candidate-caveat">{check ? "These additional images were absent from training and original model selection. Weights and threshold stayed fixed." : "Validation helped select this candidate."} These counts do not establish general accuracy. Filtering leaves outlines and height estimates unchanged; the production detector is unchanged.</p>
          {check && <p className="candidate-caveat">Validation check {check.id}, dataset {check.dataset_version}. Original evaluation results remain available above.</p>}
          <details className="candidate-provenance"><summary>Training and evaluation details</summary><p>Trained on {candidate.training_proposals.usable} usable and {candidate.training_proposals.negative} negative proposals from {candidate.training_proposals.images} images in {candidate.training_proposals.groups} area groups. No test images were used.</p><p>Candidate {candidate.id}. Original dataset {candidate.dataset_version}. Prompt: {candidate.prompt}. Keep threshold: {candidate.threshold.toFixed(2)}.</p>{candidate.supplemental_training && <p>Includes {candidate.supplemental_training.usable} usable and {candidate.supplemental_training.negative} negative instance matches from {candidate.supplemental_training.images} additional training images (dataset {candidate.supplemental_training.dataset_version}). Unreviewed proposals in those images were ignored. These additional training images are excluded from evaluation.</p>}<p>Scores are uncalibrated model outputs, not confidence percentages. Current annotations may have changed since this frozen evaluation.</p></details>
        </>}
      </>}
    </main>
  </div>;
}
