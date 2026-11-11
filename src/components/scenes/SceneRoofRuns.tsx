"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { RefreshCw } from "lucide-react";
import { ROOF_FAMILIES, roofRunGaps, type RoofTrainingRuns } from "@/lib/scene-roof-runs";
import { SPLIT_NAMES, TRAINING_SPLITS } from "@/lib/scene-training";

const percent = (n: number) => `${(n * 100).toFixed(1)}%`;
const reasons: Record<string, string> = {
  insufficient_visible_evidence: "Not enough visible image evidence",
  prior_geometry_failed: "Roof hypothesis failed the geometry checks",
  unsupported_geometry: "Outline unsupported by this classifier",
};

export default function SceneRoofRuns({ exportVersion, currentVersion }: { exportVersion?: string; currentVersion: string }) {
  const [result, setResult] = useState<RoofTrainingRuns | null>(null), [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0), [pending, setPending] = useState(true), [selected, setSelected] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    void fetch("/api/scenes/roof-training", { cache: "no-store", signal: AbortSignal.any([controller.signal, AbortSignal.timeout(30000)]) })
      .then(async response => { const value = await response.json(); if (!response.ok) throw new Error(value.error); return value as RoofTrainingRuns; })
      .then(value => { if (!controller.signal.aborted) { setResult(value); setError(""); } })
      .catch(error => { if (!controller.signal.aborted) setError(error instanceof Error && error.name !== "TimeoutError" ? error.message : "Loading training runs took too long. Refresh to retry."); })
      .finally(() => { if (!controller.signal.aborted) setPending(false); });
    return () => controller.abort();
  }, [refresh]);
  const run = result?.runs.find(r => r.id === selected) || result?.runs[0];
  const gaps = run ? roofRunGaps(run) : [];
  const latest = run?.id === result?.runs[0]?.id;
  const validationRoofs = run ? ROOF_FAMILIES.reduce((n, family) => n + run.usable.validation[family].roofs, 0) : 0;
  return <section id="roof-training-runs" className="training-roof-runs" aria-labelledby="roof-runs-heading">
    <div className="training-section-title"><h3 id="roof-runs-heading">Roof shape learning</h3>
      <button className="review-button" disabled={pending} onClick={() => { setPending(true); setError(""); setRefresh(n => n + 1); }}><RefreshCw size={14}/>{pending ? "Loading runs…" : "Refresh training runs"}</button>
    </div>
    <p className="training-explainer">This learner uses imagery and checked roof families to distinguish hipped and gabled ends. Ridge edits are retained as separate labels; this model does not learn boundaries, apex positions or heights.</p>
    {error && <p className="review-notice" role="alert">{error}{result ? " The results below are from the last successful load." : ""}</p>}
    {!!result?.unavailable && <p className="review-notice">{result.unavailable} training {result.unavailable === 1 ? "run could" : "runs could"} not be verified and {result.unavailable === 1 ? "is" : "are"} omitted. A report or its frozen dataset may be missing or changed.</p>}
    {!run && !pending && !error && <p className="review-muted">No verified roof training runs yet. Save roof-family feedback and export a dataset to prepare the first run.</p>}
    {run && <>
      <label className="training-run-select">Training run<select aria-label="Roof training run" value={run.id} onChange={e => setSelected(e.target.value)}>
        {result!.runs.map((r, i) => <option key={r.id} value={r.id}>{i === 0 ? "Latest · " : ""}{r.id} · {r.status === "experimental-candidate" ? "Fitted candidate" : "Needs labels"}</option>)}
      </select></label>
      <p className="training-run-status" role="status">{run.status === "experimental-candidate" ? "Experimental candidate fitted" : "More reviewed labels needed · no model fitted"}</p>
      <p className="review-muted">{run.dataset_version === currentVersion ? "This run uses the current saved dataset." : run.dataset_version === exportVersion ? "This run uses the latest export. Saved reviews have changed since that export." : "This run uses an earlier export; newer saved reviews are not reflected here."}</p>
      <div className="training-table-scroll" tabIndex={0} role="region" aria-label="Roof classifier examples by split">
        <table className="training-table"><caption className="training-sr-only">Usable roof examples compared with checked roof families. Test features stay unopened until model selection is complete.</caption>
          <thead><tr><th scope="col">Split</th>{ROOF_FAMILIES.map(f => <th scope="col" key={f}>{f === "hipped" ? "Hipped" : "Gabled"}</th>)}<th scope="col">Minimum per family</th></tr></thead>
          <tbody>{TRAINING_SPLITS.map(split => <tr key={split}><th scope="row">{SPLIT_NAMES[split]}</th>{ROOF_FAMILIES.map(f => {
            const checked = run.reviewed[split][f], usable = run.usable[split][f], assessed = split !== "test" || run.test_evaluation !== "not-run";
            return <td key={f}><strong>{assessed ? `${usable.roofs} usable / ${checked.roofs} checked` : `${checked.roofs} checked`}</strong>
              <small>{assessed ? `${usable.images} images · ${usable.areas} areas` : "Image features not assessed"}</small></td>;
          })}<td>{run.requirements[split].roofs_per_class} roofs · {run.requirements[split].areas_per_class} areas</td></tr>)}</tbody>
        </table>
      </div>
      {!!gaps.length && <div className="training-run-gaps"><h4>{latest ? "Data still needed" : "Data needed at this run"}</h4><ul>{gaps.map(g => <li key={`${g.split}:${g.family}`}>
        {SPLIT_NAMES[g.split]} · {g.family}: {g.roofs > 0 && `${g.roofs} more ${g.assessed ? "usable" : "checked"} ${g.roofs === 1 ? "roof" : "roofs"}`}{g.roofs > 0 && g.areas > 0 && " and "}{g.areas > 0 && `${g.areas} more ${g.areas === 1 ? "area" : "areas"}`}.
      </li>)}</ul></div>}
      {run.validation && run.baseline && <div className="training-run-results">
        <h4>Validation comparison · {validationRoofs} roofs / {run.validation.ends} roof ends</h4>
        <dl><div><dt>Baseline balanced accuracy</dt><dd>{percent(run.baseline.balanced_accuracy)}</dd></div><div><dt>Candidate balanced accuracy</dt><dd>{percent(run.validation.balanced_accuracy)}</dd></div>
          <div><dt>Log loss · lower is better</dt><dd>{run.baseline.log_loss.toFixed(3)} → {run.validation.log_loss.toFixed(3)}</dd></div></dl>
        <p className="review-muted">Weighted by class, area, roof and end. Validation selected the candidate, so this comparison is not an independent test.</p>
      </div>}
      <p className="review-muted">{run.test_evaluation === "not-run" ? "Test imagery has not been assessed for this run." : run.test ?
        `Frozen candidate test: ${percent(run.test.balanced_accuracy)} balanced accuracy on ${run.test.ends} roof ends.` : "Test evaluation is incomplete; no test accuracy is reported."} Held-out examples must remain separate from model tuning.</p>
      {run.exclusions.length > 0 && <details className="training-run-exclusions"><summary>{run.exclusions.length} training / validation roofs excluded from image features</summary>
        <p className="review-muted">These annotations remain saved. Exclusion does not mean the label is wrong. Review the imagery and outline; keep uncertain observations unchecked.</p>
        <ul>{run.exclusions.map(e => <li key={`${e.scene_id}:${e.object_id}`}><Link href={e.url}>{SPLIT_NAMES[e.split]} · {e.family} roof · {e.object_id.slice(-6)}</Link><span>{reasons[e.reason]}</span></li>)}</ul>
      </details>}
      {run.held_out_exclusions > 0 && <p className="review-muted">{run.held_out_exclusions} held-out {run.held_out_exclusions === 1 ? "roof lacked" : "roofs lacked"} usable image features.</p>}
      <details className="training-run-provenance"><summary>Run provenance and retraining</summary>
        <p>Frozen export <code>{run.dataset_version}</code> · Run <code>{run.id}</code></p>
        <p>After correcting the source images, save and export the reviewed dataset. Model fitting is currently run locally with:</p>
        <code className="training-run-command">{`task scene:train-roofs CLI_ARGS="--dataset data/training/scenes/${exportVersion || "VERSION"}"`}</code>
        <p>Each run retains its dataset and results. This screen reads reports; it does not deploy models. For additional test examples, keep the selected weights fixed.</p>
      </details>
    </>}
  </section>;
}
