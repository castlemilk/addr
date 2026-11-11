"use client";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import Link from "next/link";
import { ArrowUpRight, Download, RefreshCw } from "lucide-react";
import { CLASS_NAMES, SCENE_CLASSES, type SceneKind } from "@/lib/scene-types";
import { getCrmToken } from "@/lib/crm-client";
import SceneEvaluationPanel from "@/components/scenes/SceneEvaluation";
import SceneRoofTraining from "@/components/scenes/SceneRoofTraining";
import SceneRoofRuns from "@/components/scenes/SceneRoofRuns";
import { TRAINING_SPLITS, SPLIT_NAMES, layerAction, type SceneTrainingCoverage, type TrainingLayer } from "@/lib/scene-training";
import "../../reviews/reviews.css";
import "../scenes.css";
import "./training.css";
// After the page CSS so shell.css (via WorkbenchNav) wins the equal-specificity .review-header row.
import { WorkbenchHeader } from "@/components/shell/WorkbenchHeader";

type SplitFilter = TrainingLayer["split"] | "all";
type WorkFilter = "all" | "pending" | "ready";
const number = (n?: number | null) => (n ?? 0).toLocaleString("en-AU");
const subscribeLocation = (notify: () => void) => {
  window.addEventListener("popstate", notify);
  return () => window.removeEventListener("popstate", notify);
};
const readLocation = () => window.location.search;
const serverLocation = () => "";
async function readCoverage(operation: "refresh" | "export", signal: AbortSignal): Promise<SceneTrainingCoverage> {
  const token = operation === "export" ? getCrmToken() : "";
  const response = await fetch("/api/scenes/training", { method: operation === "export" ? "POST" : "GET",
    signal: AbortSignal.any([signal, AbortSignal.timeout(70000)]), cache: "no-store",
    headers: token ? { Authorization: `Bearer ${token}` } : {} });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Coverage is unavailable. Please retry.");
  // A body without class coverage is a broken response, not an empty dataset; surface it as the retryable error.
  if (!Array.isArray(result.classes)) throw new Error("Coverage is unavailable. Please retry.");
  return result;
}
export default function SceneTrainingPage() {
  const [coverage, setCoverage] = useState<SceneTrainingCoverage | null>(null);
  const query = new URLSearchParams(useSyncExternalStore(subscribeLocation, readLocation, serverLocation));
  const k = query.get("kind"), s = query.get("split"), w = query.get("work");
  const kind = SCENE_CLASSES.includes(k as SceneKind) ? k as SceneKind : "solar_panel";
  const split: SplitFilter = ["all", "train", "validation", "test", "unassigned"].includes(s || "") ? s as SplitFilter : "all";
  const work: WorkFilter = ["all", "pending", "ready"].includes(w || "") ? w as WorkFilter : "pending";
  const [pending, setPending] = useState<"refresh" | "export" | null>("refresh");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const request = useRef<AbortController | null>(null);
  const detail = useRef<HTMLElement | null>(null);
  const fetchCoverage = (operation: "refresh" | "export", controller: AbortController) => {
    void readCoverage(operation, controller.signal).then(result => {
      if (controller.signal.aborted) return;
      setCoverage(result);
      setMessage(operation === "export" ? "Saved labels exported. Detector weights have not changed." : "Coverage refreshed from saved reviews.");
    }).catch(e => {
      if (!controller.signal.aborted) setError(e instanceof Error && e.name !== "TimeoutError" ? e.message : "Coverage took too long. Refresh to check the latest saved state.");
    }).finally(() => {
      if (request.current === controller) { request.current = null; if (!controller.signal.aborted) setPending(null); }
    });
  };
  const load = (operation: "refresh" | "export") => {
    if (request.current) return;
    const controller = new AbortController(); request.current = controller;
    setPending(operation); setError(""); setMessage("");
    void fetchCoverage(operation, controller);
  };
  useEffect(() => {
    const controller = new AbortController(); request.current = controller;
    void fetchCoverage("refresh", controller);
    return () => { controller.abort(); request.current?.abort(); request.current = null; };
  }, []);
  function choose(k: SceneKind, s: SplitFilter, w: WorkFilter, focus = false) {
    const url = new URL(window.location.href);
    url.search = new URLSearchParams({ kind: k, split: s, work: w }).toString();
    window.history.replaceState(null, "", url);
    window.dispatchEvent(new PopStateEvent("popstate"));
    if (focus) { detail.current?.focus({ preventScroll: true }); detail.current?.scrollIntoView({ block: "start", behavior: "instant" }); }
  }
  const selected = coverage?.classes?.find(c => c.kind === kind);
  const layers = selected?.layers.filter(l => (split === "all" || l.split === split) &&
    (work === "all" || (work === "ready" ? l.eligible : !l.eligible)))
    .sort((a, b) => Number(a.eligible) - Number(b.eligible) || b.confirmed - a.confirmed || a.pending - b.pending || a.title.localeCompare(b.title, "en-AU")) || [];
  const masks = coverage?.classes?.reduce((total, c) => total + TRAINING_SPLITS.reduce((n, s) => n + c.splits[s].masks, 0), 0) || 0;
  const readyLayers = coverage?.classes?.reduce((total, c) => total + TRAINING_SPLITS.reduce((n, s) => n + c.splits[s].images, 0), 0) || 0;
  // The shell header (a nav landmark) sits beside <main>, not inside it, as on the other workspaces.
  return <div className="review-shell scene-shell training-shell">
    {/* Shell header: the nav owns Map/Scene review/Processing; only training-workspace pages and the refresh stay here. */}
    <WorkbenchHeader actions={<>
      <Link className="review-button" href="/scenes/candidates">Model candidates</Link>
      <Link className="review-button" href="/reviews?cohort=roof_training_250">Roof training set</Link>
      <button className="review-button" disabled={!!pending} onClick={() => void load("refresh")}><RefreshCw size={15} />{pending === "refresh" ? "Refreshing…" : "Refresh coverage"}</button>
    </>} />
    <main className="training-content">
      <div className="training-heading">
        <div><h1>Training coverage</h1>
          <p>Saved scene annotations by object class and split. Confirm visible outlines, then check the whole image for missing objects in each layer.</p></div>
        <button className="review-button review-primary" disabled={!!pending || !coverage || !coverage.scenes || !!error} onClick={() => void load("export")}>
          <Download size={16} />{pending === "export" ? "Exporting…" : "Export reviewed dataset"}
        </button>
      </div>
      <div className="training-feedback" aria-live="polite">
        {error ? <div className="review-notice" role="alert">{error} {coverage && "The counts below are from the last successful refresh."} <button className="review-button" disabled={!!pending} onClick={() => void load("refresh")}>Retry coverage</button></div>
          : <p role="status">{pending ? pending === "export" ? "Preparing a versioned dataset from saved reviews…" : "Checking saved labels and image coverage…" : message}</p>}
      </div>
      {!coverage ? !pending && !error ? <p>No coverage available.</p> : null : <>
        <dl className="training-metrics" aria-label="Saved dataset coverage">
          <div><dt>Captured scenes</dt><dd>{number(coverage.scenes)}</dd></div>
          <div><dt>Confirmed outlines</dt><dd>{number(coverage.reviewed_objects)}</dd></div>
          <div><dt>Exportable masks</dt><dd>{number(masks)}</dd></div>
          <div><dt>Complete image layers</dt><dd>{number(readyLayers)}</dd></div>
        </dl>
        <p className="training-explainer">Confirmed outlines remain saved even when a layer is incomplete. Only fully checked layers contribute segmentation masks or checked empty images. Heights and roof apexes stay outside these outline targets.</p>
        {!coverage.scenes ? <div className="training-empty"><h2>No scenes captured yet</h2><p>Capture an area to propose and review objects, or open the roof training set.</p><Link className="review-button" href="/scenes/new">Capture map area</Link></div> : <>
          <div className="training-section-title"><h2>Coverage by object class</h2><span>Each cell: images / masks, then distinct area groups</span></div>
          <div className="training-table-scroll" tabIndex={0} role="region" aria-label="Training coverage by class and split">
            <table className="training-table"><caption className="training-sr-only">Exportable image layers and visible masks, grouped by training split. Select a class or split to review its images.</caption>
              <thead><tr><th scope="col">Object class</th>{TRAINING_SPLITS.map(s => <th scope="col" key={s}>{SPLIT_NAMES[s]}</th>)}<th scope="col">Confirmed but withheld</th></tr></thead>
              <tbody>{coverage.classes.map(c => <tr key={c.kind} data-kind={c.kind} className={c.kind === kind ? "is-selected" : ""}>
                <th scope="row"><button onClick={() => choose(c.kind, "all", "pending", true)} aria-pressed={kind === c.kind}>{CLASS_NAMES[c.kind]}<small>{number(c.pending)} to review</small></button></th>
                {TRAINING_SPLITS.map(s => <td key={s}><button onClick={() => choose(c.kind, s, "all", true)} aria-label={`${CLASS_NAMES[c.kind]}, ${SPLIT_NAMES[s]}: ${c.splits[s].images} images, ${c.splits[s].masks} masks, ${c.splits[s].groups} area groups`}>
                  <strong>{number(c.splits[s].images)} <span>/</span> {number(c.splits[s].masks)}</strong><small>{number(c.splits[s].groups)} area {c.splits[s].groups === 1 ? "group" : "groups"}</small>
                </button></td>)}
                <td><button onClick={() => choose(c.kind, "all", "pending", true)} aria-label={`Review ${CLASS_NAMES[c.kind]}: ${c.withheld} confirmed outlines withheld`}>{number(c.withheld)} <ArrowUpRight size={13} /></button></td>
              </tr>)}</tbody>
            </table>
          </div>
          <p className="training-explainer">Overlapping views can show the same object. Area groups help expose this repetition; counts alone do not establish model accuracy. An empty validation column needs more reviewed images from the existing validation areas.</p>
          <section ref={detail} tabIndex={-1} className="training-layer-section" aria-labelledby="training-layer-heading">
            <div className="training-section-title"><h2 id="training-layer-heading">Review image layers</h2><span>{layers.length} matching {layers.length === 1 ? "image" : "images"}</span></div>
            <div className="training-filters">
              <label>Object class<select value={kind} onChange={e => choose(e.target.value as SceneKind, split, work)}>{SCENE_CLASSES.map(k => <option key={k} value={k}>{CLASS_NAMES[k]}</option>)}</select></label>
              <label>Dataset split<select value={split} onChange={e => choose(kind, e.target.value as SplitFilter, work)}><option value="all">All splits</option>{[...TRAINING_SPLITS, "unassigned" as const].map(s => <option key={s} value={s}>{SPLIT_NAMES[s]}</option>)}</select></label>
              <label>Layer status<select value={work} onChange={e => choose(kind, split, e.target.value as WorkFilter)}><option value="pending">Needs work</option><option value="ready">Ready for export</option><option value="all">All layers</option></select></label>
            </div>
            {!!selected?.withheld && <p className="training-explainer">{selected.withheld} confirmed outlines in this class are withheld until their image layer or split is resolved.</p>}
            {kind === "building" && <><SceneRoofTraining feedback={coverage.roof_feedback}/><SceneRoofRuns exportVersion={coverage.latest_export?.version} currentVersion={coverage.version}/></>}
            <SceneEvaluationPanel evaluation={coverage.evaluation} kind={kind} split={split}/>
            {!layers.length ? <div className="training-empty"><h3>No image layers match these filters</h3><p>{split === "validation" && !selected?.layers.some(l => l.split === "validation") ? "No validation scenes have been captured for this dataset yet. Open the roof training set to choose an address in a validation area." : "Choose another split or show all layers to continue reviewing."}</p><button className="review-button" onClick={() => choose(kind, "all", "all")}>Show all layers</button></div> :
              <ul className="training-layer-list">{layers.map(l => <li key={l.scene_id} data-scene={l.scene_id}>
                {/* Captured local images retain their exact hash and need no resampling service. */}
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={l.image_url} alt="" width={112} height={112} loading="lazy" />
                <div className="training-layer-info"><h3>{l.title}</h3><p>{SPLIT_NAMES[l.split]}: {l.confirmed} confirmed, {l.pending} unresolved, {l.rejected} rejected</p>
                  <p className="training-scan">{l.scan === "complete" ? `Scan finished, ${l.scan_count} proposals.` : l.scan === "not_scanned" ? "Not scanned for this class." : "Scan incomplete — inspect the image."}{l.complete ? " Whole image checked." : " Whole image check still needed."}</p>
                  <details><summary>Review provenance</summary><p>{l.spatial_group ? `Area group ${l.spatial_group}.` : "No spatial group assigned."} Saved revision {l.revision}.</p>
                    {l.blockers.includes("image_geography") && <p>{l.image_geography.reason}</p>}
                    {l.blockers.includes("stale") && <p>The model changed. Reconcile the saved edits before confirming this layer again.</p>}
                    {l.blockers.includes("split_conflict") && <p>This area or image appears in more than one split. It is withheld to prevent evaluation leakage.</p>}
                    {(l.blockers.includes("unassigned") || l.blockers.includes("missing_group")) && <p>Resolve the capture’s geographic split before using it for training or evaluation.</p>}
                  </details>
                </div>
                <div className="training-layer-action"><span className={l.eligible ? "status-approved" : "review-muted"}>{layerAction(l)}</span>
                  <Link className="review-button" href={l.review_url} aria-label={`Review ${CLASS_NAMES[kind]} in ${l.title}`}>Review layer <ArrowUpRight size={14} /></Link>
                </div>
              </li>)}</ul>}
          </section>
        </>}
        <section className="training-export" aria-labelledby="training-export-heading"><h2 id="training-export-heading">Latest dataset export</h2>
          {coverage.latest_export ? <><p>{coverage.export_current ? "Matches these saved reviews." : "Saved reviews or export rules have changed. Export again to include the current eligible labels."}</p><code>{coverage.latest_export.version}</code><p className="training-path">{coverage.latest_export.dataset}</p></> : <p>No scene dataset has been exported yet.</p>}
          <p>Exporting creates versioned label files. It does not train or update the detector.</p>
        </section>
      </>}
    </main>
  </div>;
}
