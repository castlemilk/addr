"use client";
import Link from "next/link";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { ArrowLeft, RefreshCw } from "lucide-react";
import { WorkbenchHeader } from "@/components/shell/WorkbenchHeader";
import "../../reviews/reviews.css";
import "../queues.css";

/* /queues/small-roofs: the reviewer's live queue for round-5 small-roof decisions. Reads GET /api/review-progress
   (the round-5 counter, run at most once per 10 s) and shows exactly what it returns: no count on this page is typed
   by hand, and the rule's thresholds come from the counter's n_rule. Every link opens the canonical /scenes editor at
   a scene or an object; the reviewer's own decisions there are the only writes, and nothing on this page approves,
   rejects or saves anything. The figures count the reviewer's decisions and are never a share or a score; a review
   the assistant wrote is an observation and counts only once the reviewer decides the object. The page refetches when
   its window regains focus and on Refresh. */

/** The plan's next milestone after the rule minimum: the second bar's target, not a count. */
const NEXT_TARGET = 50;

type Rule = { primary_decided_min: number; per_class_min: number; scenes_min: number; passes: boolean };
type Target = { id: string; area_m2: number | null; note: string | null };
type SceneTargets = { scene_id: string; remaining: number | null; objects: Target[] };
type Entry = { scene: string | null; object: string | null; area_m2: number | null; decision: string | null; via: string | null; note: string | null; text: string | null };
type Progress = {
  primary_n: number; roof: number; not_roof: number; scenes: number; rule: Rule;
  ui: number | null; chat: number | null; agent_authored_excluded: number | null; human_drawn_lt_25: number | null;
  targets_total: number | null; targets_remaining: number | null; next_scenes: SceneTargets[];
  already_counted: Entry[]; agent_authored_required: Entry[]; deferred_unjudged: Entry[];
  computed_at: string | null; review_snapshot_sha256: string | null; block: string | null;
};
type Read = { reading: boolean; progress: Progress | null; missing: boolean; error: string | null };
type Answer = { missing: true } | { missing: false; progress: Progress };
const INITIAL: Read = { reading: true, progress: null, missing: false, error: null };

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const record = (value: unknown): Record<string, unknown> => isRecord(value) ? value : {};
const num = (value: unknown): number | null => typeof value === "number" && Number.isFinite(value) ? value : null;
const str = (value: unknown): string | null => typeof value === "string" && value.trim() ? value : null;
const list = (value: unknown): unknown[] => Array.isArray(value) ? value : [];
/** A count the counter may give as a number, a list, or a record of numbers (summed); anything else is unknown. */
function tally(value: unknown): number | null {
  if (Array.isArray(value)) return value.length;
  if (isRecord(value)) {
    const parts = Object.values(value).map(num);
    return parts.length && parts.every(part => part !== null) ? parts.reduce<number>((sum, part) => sum + (part ?? 0), 0) : null;
  }
  return num(value);
}
/** The number of scenes: a number, or the length of a list or of a record keyed by scene (never the sum of its values). */
function sceneCount(value: unknown): number | null {
  if (Array.isArray(value)) return value.length;
  if (isRecord(value)) return Object.keys(value).length;
  return num(value);
}
function toTarget(value: unknown): Target | null {
  if (!isRecord(value)) return null;
  const id = str(value.id) ?? str(value.object_id);
  return id ? { id, area_m2: num(value.area_m2), note: str(value.note) ?? str(value.notes) } : null;
}
function toScene(value: unknown): SceneTargets | null {
  if (!isRecord(value)) return null;
  const scene_id = str(value.scene_id) ?? str(value.scene);
  if (!scene_id) return null;
  return { scene_id, remaining: num(value.remaining), objects: list(value.objects).map(toTarget).filter((target): target is Target => target !== null) };
}
function toEntry(value: unknown): Entry {
  const blank: Entry = { scene: null, object: null, area_m2: null, decision: null, via: null, note: null, text: null };
  if (typeof value === "string") return { ...blank, text: value };
  if (!isRecord(value)) return { ...blank, text: String(JSON.stringify(value)) };
  const scene = str(value.scene_id) ?? str(value.scene), object = str(value.object_id) ?? str(value.id) ?? str(value.object);
  return {
    scene, object, area_m2: num(value.area_m2),
    decision: str(value.class) ?? str(value.status) ?? str(value.decision), via: str(value.provenance_kind),
    note: str(value.note) ?? str(value.notes) ?? str(value.reason),
    text: scene || object ? null : JSON.stringify(value).slice(0, 300),
  };
}
/** The counter's JSON (count --json) as the page reads it; the headline fields are required, never guessed. */
function toProgress(value: unknown): Progress {
  if (!isRecord(value)) throw new Error("The counter's answer was not a JSON object.");
  const byClass = record(value.primary_by_class), byVia = record(value.primary_by_provenance), rule = record(value.n_rule);
  const primary_n = num(value.primary_n), roof = num(byClass.roof), not_roof = num(byClass.not_roof), scenes = sceneCount(value.scenes);
  const minimum = num(rule.primary_decided_min), perClass = num(rule.per_class_min), scenesMin = num(rule.scenes_min);
  if (primary_n === null || roof === null || not_roof === null || scenes === null || minimum === null || perClass === null || scenesMin === null) {
    const absent = Object.entries({ primary_n, "primary_by_class.roof": roof, "primary_by_class.not_roof": not_roof, scenes,
      "n_rule.primary_decided_min": minimum, "n_rule.per_class_min": perClass, "n_rule.scenes_min": scenesMin }).filter(([, field]) => field === null).map(([key]) => key);
    throw new Error(`The counter's answer has no ${absent.join(", ")}, so nothing is shown rather than a guess.`);
  }
  return {
    primary_n, roof, not_roof, scenes, rule: { primary_decided_min: minimum, per_class_min: perClass, scenes_min: scenesMin, passes: rule.passes === true },
    ui: num(byVia.ui), chat: num(byVia.chat), agent_authored_excluded: tally(value.agent_authored_excluded), human_drawn_lt_25: tally(value.human_drawn_lt_25),
    targets_total: num(value.targets_total), targets_remaining: num(value.targets_remaining),
    next_scenes: list(value.next_scenes).map(toScene).filter((scene): scene is SceneTargets => scene !== null),
    already_counted: list(value.already_counted).map(toEntry), agent_authored_required: list(value.agent_authored_required).map(toEntry),
    deferred_unjudged: list(value.deferred_unjudged).map(toEntry),
    computed_at: str(value.computed_at), review_snapshot_sha256: str(value.review_snapshot_sha256), block: str(value.block),
  };
}
/** GET the progress route with the effect's abort signal and a 60 s ceiling. */
async function readProgress(signal: AbortSignal): Promise<Answer> {
  const ceiling = AbortSignal.timeout(60000);
  const response = await fetch("/api/review-progress", { signal: typeof AbortSignal.any === "function" ? AbortSignal.any([signal, ceiling]) : signal, cache: "no-store" });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new Error((isRecord(body) && str(body.error)) || `The progress route answered ${response.status}.`);
  if (isRecord(body) && body.available === false) return { missing: true };
  return { missing: false, progress: toProgress(body) };
}
const describe = (error: unknown) => error instanceof Error ? (error.name === "TimeoutError" ? "The counter did not answer within 60 seconds." : error.message) : String(error);

const count = (n: number) => n.toLocaleString("en-AU");
const plural = (n: number, one: string, many: string) => `${count(n)} ${n === 1 ? one : many}`;
const area = (m2: number | null) => m2 === null ? "—" : `${m2.toFixed(1)} m²`;
const sceneHref = (scene: string) => `/scenes?scene=${encodeURIComponent(scene)}&status=all&layer=edited`;
const objectHref = (scene: string, object: string) => `${sceneHref(scene)}&object=${encodeURIComponent(object)}`;
const when = (iso: string) => { const at = new Date(iso); return Number.isNaN(at.getTime()) ? iso : at.toLocaleString("en-AU", { dateStyle: "medium", timeStyle: "medium" }); };
const DECISIONS: Record<string, string> = { confirmed: "Roof", roof: "Roof", rejected: "Not a roof", not_roof: "Not a roof", roof_part: "Part of a roof" };
const VIA: Record<string, string> = { ui: "decided in the app", chat: "your words in chat" };

function Bar({ label, value, max }: { label: string; value: number; max: number }) {
  return <div className="queues-band">
    <div className="queues-band-heading"><h3>{label}</h3><span className="queues-progress">{count(value)} of {count(max)}</span></div>
    <progress value={Math.min(value, max)} max={max} aria-label={`${label}: ${count(value)} of ${count(max)}`} style={{ width: "100%", height: 8, accentColor: "var(--review-accent)" }} />
  </div>;
}
function SceneBlock({ scene }: { scene: SceneTargets }) {
  return <div className="queues-band">
    <div className="queues-band-heading">
      <h3>{scene.scene_id}</h3>
      {scene.remaining !== null ? <span className="queues-progress">{plural(scene.remaining, "target", "targets")} left</span> : null}
      <span className="queues-report-actions"><Link href={sceneHref(scene.scene_id)}>Open scene</Link></span>
    </div>
    {scene.objects.length ? <div className="queues-panel"><table className="queues-table">
      <caption className="queues-visually-hidden">{`Remaining targets on ${scene.scene_id}`}</caption>
      <thead><tr><th scope="col">Object</th><th scope="col">Area</th></tr></thead>
      <tbody>{scene.objects.map((target, index) => <tr key={`${target.id}/${index}`}>
        <td><Link href={objectHref(scene.scene_id, target.id)}>{target.id}</Link>{target.note ? <span className="queues-secondary">{target.note}</span> : null}</td>
        <td>{area(target.area_m2)}</td>
      </tr>)}</tbody>
    </table></div> : null}
  </div>;
}
function EntryName({ entry }: { entry: Entry }) {
  if (entry.scene && entry.object) return <><Link href={objectHref(entry.scene, entry.object)}>{entry.object}</Link><span className="queues-secondary">{entry.scene}</span></>;
  if (entry.scene) return <Link href={sceneHref(entry.scene)}>{entry.scene}</Link>;
  return <>{entry.object ?? entry.text ?? "—"}</>;
}
/** One read-only list from the counter. Only "Already counted" shows a decision, and only the reviewer's own. */
function EntrySection({ id, title, intro, entries, decisions = false, empty }: { id: string; title: string; intro: ReactNode; entries: Entry[]; decisions?: boolean; empty: string }) {
  return <section className="queues-section" aria-labelledby={id}>
    <h2 id={id}>{title}</h2>
    <p className="queues-intro">{intro}</p>
    {entries.length ? <div className="queues-panel"><table className="queues-table">
      <caption className="queues-visually-hidden">{title}</caption>
      <thead><tr><th scope="col">Object</th>{decisions ? <th scope="col">Decision</th> : null}<th scope="col">Area</th></tr></thead>
      <tbody>{entries.map((entry, index) => <tr key={`${entry.scene ?? ""}/${entry.object ?? entry.text ?? ""}/${index}`}>
        <td><EntryName entry={entry} />{entry.note ? <span className="queues-secondary">{entry.note}</span> : null}</td>
        {decisions ? <td>{entry.decision ? DECISIONS[entry.decision] ?? entry.decision.replace(/_/g, " ") : "—"}{entry.via ? <span className="queues-secondary">{VIA[entry.via] ?? entry.via}</span> : null}</td> : null}
        <td>{area(entry.area_m2)}</td>
      </tr>)}</tbody>
    </table></div> : <p className="queues-state">{empty}</p>}
  </section>;
}
function KeyLegend() {
  return <section className="queues-section" aria-labelledby="small-roofs-keys">
    <h2 id="small-roofs-keys">Keys in the scene editor</h2>
    <div className="queues-report-body"><ul>
      <li><strong>Right</strong> (Looks right): a roof. Sheds, carports, garages and tank covers count as roofs.</li>
      <li><strong>Backspace</strong> (Not a roof): not a roof.</li>
      <li><strong>Left</strong> with &ldquo;Separate or merged objects&rdquo; ticked: part of a roof already outlined, counted separately and never as not a roof.</li>
      <li><strong>Left</strong> alone: undecided; it does not count.</li>
    </ul></div>
  </section>;
}
function ProgressView({ progress: p }: { progress: Progress }) {
  const facts = [
    p.computed_at ? `Counted ${when(p.computed_at)}` : null,
    p.block ? `Block ${p.block}` : null,
    p.ui !== null ? `${count(p.ui)} decided in the app` : null,
    p.chat !== null ? `${count(p.chat)} from your words in chat` : null,
    p.review_snapshot_sha256 ? `Review snapshot ${p.review_snapshot_sha256.slice(0, 12)}` : null,
  ].filter((fact): fact is string => fact !== null);
  const strata = [
    p.agent_authored_excluded !== null ? `${plural(p.agent_authored_excluded, "assistant-authored review is", "assistant-authored reviews are")} left out until you decide those objects` : null,
    p.human_drawn_lt_25 !== null ? `${plural(p.human_drawn_lt_25, "small outline", "small outlines")} you drew yourself sit in a separate stratum, never pooled with these` : null,
  ].filter((line): line is string => line !== null);
  const remaining = p.targets_remaining === null ? null
    : p.targets_total === null ? `${plural(p.targets_remaining, "target remains", "targets remain")}. ` : `${count(p.targets_remaining)} of ${plural(p.targets_total, "target", "targets")} remain. `;
  return <>
    <section className="queues-section" aria-labelledby="small-roofs-progress">
      <h2 id="small-roofs-progress">Progress</h2>
      <Bar label="Towards the rule minimum" value={p.primary_n} max={p.rule.primary_decided_min} />
      <Bar label="Towards the next target" value={p.primary_n} max={NEXT_TARGET} />
      <p className="queues-intro">{`The rule reads a share only at ${count(p.rule.primary_decided_min)} usable decisions with at least ${count(p.rule.per_class_min)} of each class on at least ${count(p.rule.scenes_min)} scenes (${p.rule.passes ? "met" : "not met yet"}); below it the counter shows counts and per-object facts, never a share.`}</p>
      {strata.length ? <p className="queues-intro">{`${strata.join("; ")}.`}</p> : null}
      {facts.length ? <p className="queues-report-meta">{facts.map(fact => <span key={fact}>{fact}</span>)}</p> : null}
    </section>
    <KeyLegend />
    <section className="queues-section" aria-labelledby="small-roofs-next">
      <h2 id="small-roofs-next">Next scenes</h2>
      <p className="queues-intro">{`${remaining ?? ""}Each target is a live, undecided small model outline on a judged tile; an object you decide never comes back.`}</p>
      {p.next_scenes.length ? p.next_scenes.map((scene, index) => <SceneBlock key={`${scene.scene_id}/${index}`} scene={scene} />) : <p className="queues-state">No remaining targets in the counter&apos;s answer.</p>}
    </section>
    <EntrySection id="small-roofs-counted" title="Already counted" decisions entries={p.already_counted} empty="No usable decisions yet."
      intro="Your decisions that the counter uses now; they are never asked again." />
    <EntrySection id="small-roofs-agent" title="Required fresh decisions (agent-authored review)" entries={p.agent_authored_required} empty="None in the counter's answer."
      intro="These objects carry the assistant's tracing notes. That review is an observation, not your decision: each object counts only once you decide it yourself in the editor." />
    <EntrySection id="small-roofs-deferred" title="Deferred: un-judged tiles" entries={p.deferred_unjudged} empty="None in the counter's answer."
      intro="Objects on tiles where no model predictions were collected. A decision there cannot join the comparison until the models run on those tiles, so they wait." />
  </>;
}

export default function SmallRoofsPage() {
  const [read, setRead] = useState<Read>(INITIAL);
  const [attempt, setAttempt] = useState(0);
  const refresh = useCallback(() => { setRead(current => ({ ...current, reading: true })); setAttempt(current => current + 1); }, []);

  useEffect(() => {
    const controller = new AbortController();
    readProgress(controller.signal)
      .then(answer => { if (!controller.signal.aborted) setRead(answer.missing ? { reading: false, progress: null, missing: true, error: null } : { reading: false, progress: answer.progress, missing: false, error: null }); })
      .catch(error => { if (!controller.signal.aborted) setRead(current => ({ ...current, reading: false, error: describe(error) })); });
    return () => controller.abort();
  }, [attempt]);

  // Refetch whenever this window regains focus, e.g. back from the editor; the route shares one counter run per 10 s.
  useEffect(() => {
    window.addEventListener("focus", refresh);
    return () => window.removeEventListener("focus", refresh);
  }, [refresh]);

  const progress = read.progress;
  return <main className="review-shell queues-shell">
    <WorkbenchHeader current="queues" />
    <div className="queues-content">
      <div className="queues-title">
        <nav className="queues-report-actions" aria-label="Queue links"><Link href="/queues"><ArrowLeft size={14} aria-hidden="true" />Back to queues</Link></nav>
        <div className="queues-title-row">
          <h1>{progress ? `Usable small-roof decisions: ${count(progress.primary_n)} of ${count(progress.rule.primary_decided_min)} (roof ${count(progress.roof)} / not-roof ${count(progress.not_roof)}) on ${plural(progress.scenes, "scene", "scenes")} (rule needs ${count(progress.rule.scenes_min)})` : "Usable small-roof decisions"}</h1>
          <button type="button" className="review-button" onClick={refresh} disabled={read.reading}><RefreshCw size={14} aria-hidden="true" />{read.reading ? "Reading" : "Refresh"}</button>
        </div>
        <p>Every figure is read from the round-5 counter at the moment shown and refreshes when you return to this window. Links open the scene editor; your decisions there are the only writes, and nothing on this page changes a review.</p>
      </div>
      {read.error ? <div className="review-notice queues-error" role="alert">{read.error} <button type="button" className="review-button" onClick={refresh}>Retry</button></div> : null}
      {progress ? <ProgressView progress={progress} /> : <>
        {read.missing ? <p className="queues-state" role="status">Small-roof counter not installed yet. This page fills in once it lands; nothing is counted by hand meanwhile.</p>
          : read.reading ? <p className="queues-state" role="status">Reading the small-roof counter</p> : null}
        <KeyLegend />
      </>}
    </div>
  </main>;
}
