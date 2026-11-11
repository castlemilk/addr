"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { WorkbenchHeader } from "@/components/shell/WorkbenchHeader";
import { BANDS, ErrorState, QueueTable, ReadingState, StatusLabel, SummarySections, coerceCalibration, coerceHidden, coerceSummary,
  describeError as describe, displayAddress, number, plural, readJson, score, tileOf,
  type Band, type BandProgress, type CalibrationQueue, type CalibrationRow, type HiddenQueue, type HiddenRow, type Loadable, type QueuePart, type QueuesSummary } from "@/components/queues/QueueTable";
import { agreementText, coerceMachineLabelledQueue, decisionSummary, hintBadges, isAutoTier, isFlagged, isFragment, machineLabelledSummary, modelRangeText, pickHints, structureSummary, type ClassHints, type MachineLabelledQueue } from "@/lib/queues";
import "../reviews/reviews.css";
import "./queues.css";

/* /queues: the Review queues hub. Reads three read-only routes and links every row into the
   canonical /scenes editor. Nothing here approves, rejects or changes a roof. Calibration and hidden
   rows carry building class hints (building-flags-v2) and structure-kind hints (structure-kinds-v3)
   from the eyes index; each renders as a quiet badge whose tooltip holds the evidence. They are agent
   hints, not labels, and the "Hide flagged" filter is a client-side view choice that decides nothing.
   Rows whose structure hint is a fragment (a piece of an outline, not a building) stay out of each table
   until its "Show fragments (N)" checkbox is ticked; the denominators always state both counts. Rows with a
   decision-policy-v1 row carry a tier badge (Auto, Assist, Review) whose tooltip holds the machine label and
   reasons; each table counts its machine labels and offers "Hide auto tier (N)", unchecked until the reviewer
   decides otherwise. The Machine labelled table lists every auto-tier roof from the index (one query over
   decision_policy) with its confidence, model range, human agreement interval and recorded review status; it is
   the reviewer's surface for those labels. A machine label is applied to an object only in the editor, by the
   reviewer, on save. */

const LOADING = { state: "loading" } as const;
type Hinted<T> = T & ClassHints;
type HintedCalibration = Omit<CalibrationQueue, "rows"> & { rows: Hinted<CalibrationRow>[] };
type HintedHidden = Omit<HiddenQueue, "rows"> & { rows: Hinted<HiddenRow>[] };
const EMPTY_BAND: BandProgress = { groups_total: 0, groups_reviewed: 0, successes: 0, failures: 0, interval: null, minimum_groups: 20 };

/** The shared coercers drop unknown fields, so hints are read off the raw document and re-attached by scene/object. */
function withHints<T extends { scene_id: string; object_id: string }>(rows: T[], raw: unknown): Hinted<T>[] {
  const hints = pickHints(raw);
  return rows.map(row => ({ ...row, ...(hints.get(`${row.scene_id}/${row.object_id}`) ?? { hints: [], class_hint: null }) }));
}
const coerceHintedCalibration = (value: unknown, url: string): HintedCalibration => { const queue = coerceCalibration(value, url); return { ...queue, rows: withHints(queue.rows, value) }; };
const coerceHintedHidden = (value: unknown, url: string): HintedHidden => { const queue = coerceHidden(value, url); return { ...queue, rows: withHints(queue.rows, value) }; };

function HintBadges({ row }: { row: ClassHints }) {
  const badges = hintBadges(row);
  if (!badges.length) return null;
  return <span className="queues-hints">{badges.map(badge => <span key={badge.key} className={`queues-hint queues-hint-${badge.key}${badge.group === "structure" ? " queues-hint-structure" : badge.group === "decision" ? " queues-hint-decision" : ""}`} title={badge.title}>{badge.label}</span>)}</span>;
}
/** Per-table inventory of machine labels by tier, before any filter, with the rows that have none; nothing when no row has one. */
function DecisionCount({ rows }: { rows: ClassHints[] }) {
  const line = decisionSummary(rows);
  if (!line) return null;
  const without = rows.filter(row => !row.decision).length;
  return <p className="queues-intro queues-decision-count">{line}{without ? `; ${plural(without, "roof", "roofs")} without one` : ""}. Machine labels, not reviewer labels.</p>;
}
/** Compact inventory of structure hints across a queue's rows, before any filter; nothing when there are none. */
function StructureCount({ rows }: { rows: ClassHints[] }) {
  const line = structureSummary(rows);
  return line ? <p className="queues-intro queues-structure-count">{line}. Agent hints, not labels.</p> : null;
}
interface Filtered<T> { rows: T[]; fragments: number; auto: number; note: string | null; fragmentNote: string | null; autoNote: string | null }
/** Rows the view keeps, plus honest wording. Fragments leave first unless shown ("31 roofs, 2 fragments hidden"); then the
 *  auto tier leaves when asked ("3 auto-tier rows of 30 hidden"); then the flagged filter runs over what remains and says
 *  how many it removed, with that denominator. Fragments count as flagged, so showing them while hiding flagged rows hides
 *  them again by the filter, and the note says so. The auto count is taken over the rows in view after the fragment step,
 *  so the checkbox and its note agree. */
function filtered<T extends ClassHints>(rows: T[], hideFlagged: boolean, showFragments: boolean, hideAuto = false): Filtered<T> {
  const fragments = rows.filter(isFragment).length;
  const fragmentNote = fragments ? `${plural(rows.length, "roof", "roofs")}, ${plural(fragments, "fragment", "fragments")} ${showFragments ? "shown" : "hidden"}` : null;
  const inView = showFragments ? rows : rows.filter(row => !isFragment(row));
  const auto = inView.filter(isAutoTier).length;
  const visible = hideAuto ? inView.filter(row => !isAutoTier(row)) : inView;
  const autoNote = hideAuto && auto ? `${number(auto)} auto-tier ${auto === 1 ? "row" : "rows"} of ${number(inView.length)} hidden` : null;
  if (!hideFlagged) return { rows: visible, fragments, auto, note: null, fragmentNote, autoNote };
  const kept = visible.filter(row => !isFlagged(row));
  return { rows: kept, fragments, auto, fragmentNote, autoNote, note: visible.length === kept.length ? null : `${number(visible.length - kept.length)} of ${number(visible.length)} flagged rows hidden by the filter` };
}
/** The quiet per-table checkbox; nothing when a table has no fragments to show. */
function FragmentToggle({ count, checked, onChange }: { count: number; checked: boolean; onChange: (checked: boolean) => void }) {
  if (!count) return null;
  return <label className="queues-filter queues-fragments"><input type="checkbox" checked={checked} onChange={event => onChange(event.target.checked)} /> Show fragments ({number(count)})</label>;
}
/** The quiet per-table "Hide auto tier (N)" checkbox, unchecked until the reviewer decides; nothing when a table has no auto rows. */
function AutoToggle({ count, checked, onChange }: { count: number; checked: boolean; onChange: (checked: boolean) => void }) {
  if (!count) return null;
  return <label className="queues-filter queues-auto-tier" title="Hides rows the decision policy placed in the auto tier. A view choice only; nothing is decided by it."><input type="checkbox" checked={checked} onChange={event => onChange(event.target.checked)} /> Hide auto tier ({number(count)})</label>;
}
type FragmentViews = Record<string, boolean>;
type AutoViews = Record<string, boolean>;

function intervalText(progress: BandProgress, iouSuccess: number): string {
  if (progress.interval) {
    const [low, high] = progress.interval;
    return `Wilson 95%: ${low.toFixed(2)}–${high.toFixed(2)} (boundary IoU >= ${iouSuccess}, sampled roofs, not per-house accuracy)`;
  }
  if (progress.groups_total < progress.minimum_groups) return `Interval not available: ${number(progress.groups_total)} groups cannot reach ${number(progress.minimum_groups)}`;
  const remaining = Math.max(0, progress.minimum_groups - progress.groups_reviewed);
  return `Interval not yet available (${number(remaining)} more reviewed groups needed)`;
}

function CalibrationTables({ queue, hideFlagged, fragmentViews, onShowFragments, autoViews, onHideAuto }: { queue: HintedCalibration; hideFlagged: boolean; fragmentViews: FragmentViews; onShowFragments: (table: string, shown: boolean) => void; autoViews: AutoViews; onHideAuto: (table: string, hidden: boolean) => void }) {
  const progress = (band: Band) => queue.bands[band] ?? EMPTY_BAND;
  const minimum = queue.minimum_groups ?? progress("low").minimum_groups ?? 20;
  return <>
    <p className="queues-intro">
      {plural(queue.rows.length, "fixed-hash roof", "fixed-hash roofs")}: {BANDS.map(band => `${number(progress(band).groups_total)} ${band}`).join(", ")}. Review each boundary from the image, then confirm with geometry reviewed or reject. Intervals appear at {number(minimum)} reviewed groups per band.
    </p>
    <p className="queues-intro">
      Any interval covers boundary IoU {">="} {queue.iou_success} on these sampled roofs only: not apex, height or recall, and not per-house accuracy. Bands are model estimates.{queue.policy ? ` Split policy: ${queue.policy}.` : ""}
    </p>
    {BANDS.map(band => {
      const showFragments = fragmentViews[band] === true, hideAuto = autoViews[band] === true;
      const bandRows = queue.rows.filter(row => row.band === band);
      const view = filtered(bandRows, hideFlagged, showFragments, hideAuto), { rows, note } = view, p = progress(band);
      return <div className="queues-band" key={band}>
        <div className="queues-band-heading">
          <h3 id={`queues-band-${band}`}>{band.charAt(0).toUpperCase() + band.slice(1)} band</h3>
          <span className="queues-progress">{number(p.groups_reviewed)} of {number(p.groups_total)} reviewed</span>
          <span className="queues-interval">{intervalText(p, queue.iou_success)}</span>
          {view.fragmentNote ? <span className="queues-filter-note">{view.fragmentNote}</span> : null}
          {view.autoNote ? <span className="queues-filter-note">{view.autoNote}</span> : null}
          {note ? <span className="queues-filter-note">{note}</span> : null}
          <FragmentToggle count={view.fragments} checked={showFragments} onChange={shown => onShowFragments(band, shown)} />
          <AutoToggle count={view.auto} checked={hideAuto} onChange={hidden => onHideAuto(band, hidden)} />
        </div>
        {rows.length === 0 ? <ReadingState>{note ? `Every sampled roof in the ${band} band is flagged and hidden by the filter.` : view.autoNote ? `Every sampled roof in the ${band} band is in the auto tier, hidden until shown.` : view.fragments && !showFragments ? `Every sampled roof in the ${band} band is a fragment, hidden until shown.` : `No sampled roofs in the ${band} band.`}</ReadingState>
          : <QueueTable caption={`${band} band calibration roofs`} data-band={band} columns={["Address", "Tile", "Detector (model estimate)", "Agent hints (not labels)", "Status", "Open"]}>
            {rows.map(row => <tr key={`${row.scene_id}/${row.object_id}`}>
              <td><span className={row.address ? undefined : "queues-muted"}>{displayAddress(row.address)}</span><span className="queues-secondary">{row.object_id}</span></td>
              <td>{tileOf(row.scene_id, row.group)}</td>
              <td>{score(row.detector_score)}</td>
              <td><HintBadges row={row} /></td>
              <td><StatusLabel status={row.status} />{row.counted && row.success !== null ? <span className="queues-secondary">{row.success ? "counts as a success" : "counts as a failure"}</span> : null}</td>
              <td><Link href={row.review_url} aria-label={`Open ${row.address ? displayAddress(row.address) : `roof ${row.object_id}`} in the editor`}>Open</Link></td>
            </tr>)}
          </QueueTable>}
        <DecisionCount rows={bandRows} />
      </div>;
    })}
    <StructureCount rows={queue.rows} />
  </>;
}

function CalibrationSection({ summary, calibration, hideFlagged, fragmentViews, onShowFragments, autoViews, onHideAuto, onRetry, onRetrySummary }: { summary: Loadable<QueuesSummary>; calibration: Loadable<HintedCalibration>; hideFlagged: boolean; fragmentViews: FragmentViews; onShowFragments: (table: string, shown: boolean) => void; autoViews: AutoViews; onHideAuto: (table: string, hidden: boolean) => void; onRetry: () => void; onRetrySummary: () => void }) {
  const planned = summary.state === "ready" ? summary.data.calibration : undefined;
  const planId = calibration.state === "ready" ? calibration.data.plan_id : planned?.plan_id ?? null;
  return <section className="queues-section" aria-labelledby="queues-calibration">
    <h2 id="queues-calibration">{planId ? `Calibration sample, plan ${planId}` : "Calibration sample"}</h2>
    {calibration.state === "ready" ? <CalibrationTables queue={calibration.data} hideFlagged={hideFlagged} fragmentViews={fragmentViews} onShowFragments={onShowFragments} autoViews={autoViews} onHideAuto={onHideAuto} />
      : calibration.state === "error" ? <ErrorState message={`The calibration queue could not be read: ${calibration.message}`} action="Retry calibration" onRetry={onRetry} />
      : planned === null ? <ReadingState>No calibration plan has been sampled yet. Run task eyes:calibration to draw the fixed-hash sample; its review queue appears here.</ReadingState>
      : summary.state === "error" ? <ErrorState message={`The queue summary could not be read, so the calibration plan is unknown: ${summary.message}`} action="Retry summary" onRetry={onRetrySummary} />
      : <ReadingState>{planned ? `Reading plan ${planned.plan_id} and ${number(planned.rows)} review files` : "Reading the queue summary"}</ReadingState>}
  </section>;
}

function HiddenSection({ summary, hidden, hideFlagged, showFragments, onShowFragments, hideAuto, onHideAuto, onRetry }: { summary: Loadable<QueuesSummary>; hidden: Loadable<HintedHidden>; hideFlagged: boolean; showFragments: boolean; onShowFragments: (shown: boolean) => void; hideAuto: boolean; onHideAuto: (hidden: boolean) => void; onRetry: () => void }) {
  const known = hidden.state === "ready" ? hidden.data.count : summary.state === "ready" ? summary.data.hidden.count : null;
  const bandText = (band: string | null) => band ?? "band unknown";
  const shown = hidden.state === "ready" ? filtered(hidden.data.rows, hideFlagged, showFragments, hideAuto) : null;
  const sentence = (note: string) => ` ${note.charAt(0).toUpperCase()}${note.slice(1)}.`;
  return <section className="queues-section" id="hidden" aria-labelledby="queues-hidden">
    <h2 id="queues-hidden">Low roofs hidden under higher bands</h2>
    {hidden.state === "error" ? <ErrorState message={`The hidden-roof index could not be read: ${hidden.message}`} action="Retry hidden roofs" onRetry={onRetry} />
      : hidden.state !== "ready" || !shown ? <ReadingState>{known === null ? "Reading hidden roofs from the cross-capture index" : `Reading ${number(known)} hidden roofs`}</ReadingState>
      : <>
        <p className="queues-intro">The map shows only the highest-band capture of a roof. {hidden.data.count === 1 ? "This 1 low-evidence scan roof sits" : `These ${number(hidden.data.count)} low-evidence scan roofs sit`} under a medium or high capture of the same roof; both are model estimates.{shown.fragmentNote ? sentence(shown.fragmentNote) : ""}{shown.autoNote ? sentence(shown.autoNote) : ""}{shown.note ? sentence(shown.note) : ""}</p>
        {shown.fragments || shown.auto ? <div className="queues-table-filters">
          <FragmentToggle count={shown.fragments} checked={showFragments} onChange={onShowFragments} />
          <AutoToggle count={shown.auto} checked={hideAuto} onChange={onHideAuto} />
        </div> : null}
        {shown.rows.length === 0 ? <ReadingState>{shown.note ? "Every hidden roof is flagged and hidden by the filter." : shown.autoNote ? "Every hidden roof is in the auto tier, hidden until shown." : shown.fragments && !showFragments ? "Every hidden roof is a fragment, hidden until shown." : "No low roofs are hidden under a higher band."}</ReadingState>
          : <QueueTable caption="Low roofs hidden under higher bands" data-queue="hidden" columns={["Address", "Hidden roof (band, detector, lines)", "Agent hints (not labels)", "Shown as (band, detector, capture)", "IoU", "Open hidden", "Open shown"]}>
            {shown.rows.map(row => <tr key={`${row.scene_id}/${row.object_id}`}>
              <td><span className={row.address ? undefined : "queues-muted"}>{displayAddress(row.address)}</span><span className="queues-secondary">{row.scene_id} {row.object_id}</span></td>
              <td>{bandText(row.band)}, detector {score(row.detector_score)}, {row.observed_segments === null ? "no lines observed" : `${number(row.observed_segments)} lines`}</td>
              <td><HintBadges row={row} /></td>
              <td>{bandText(row.other_band)}, detector {score(row.other_detector_score)}, {row.other_scene_id}</td>
              <td>{score(row.iou)}</td>
              <td><Link href={row.hidden_url}>Open hidden</Link></td>
              <td><Link href={row.shown_url}>Open shown</Link></td>
            </tr>)}
          </QueueTable>}
        <DecisionCount rows={hidden.data.rows} />
        <StructureCount rows={hidden.data.rows} />
      </>}
  </section>;
}

/** The auto tier from the index: one row per machine-labelled roof, honest denominators, links into the editor and nothing else. */
function MachineLabelledSection({ queue, onRetry }: { queue: Loadable<MachineLabelledQueue & { source?: string }>; onRetry: () => void }) {
  return <section className="queues-section" id="machine-labelled" aria-labelledby="queues-machine-labelled">
    <h2 id="queues-machine-labelled">Machine labelled</h2>
    {queue.state === "error" ? <ErrorState message={`The machine-labelled roofs could not be read: ${queue.message}`} action="Retry machine labelled" onRetry={onRetry} />
      : queue.state !== "ready" ? <ReadingState>Reading machine-labelled roofs from the eyes index</ReadingState>
      : <>
        <p className="queues-intro queues-machine-summary">{machineLabelledSummary(queue.data)}. Auto-tier roofs proceed without a human; confidence and model ranges are model estimates, not accuracy, and human agreement counts reviewer decisions in the label&apos;s stratum. Each row opens the editor, where Use machine label applies the label and you still save the scene.</p>
        {queue.data.rows.length === 0 ? <ReadingState>{queue.data.source === "unavailable" ? "The eyes index has no decision policy rows on this machine yet." : "No roof is in the auto tier."}</ReadingState>
          : <QueueTable caption="Machine-labelled roofs (auto tier)" data-queue="machine-labelled" columns={["Roof", "Machine label", "Confidence (model range)", "Human agreement", "Review status", "Open"]}>
            {queue.data.rows.map(row => <tr key={`${row.scene_id}/${row.object_id}`}>
              <td><Link href={row.review_url}>{row.object_id}</Link><span className="queues-secondary">{row.scene_id}</span></td>
              <td>{row.label ?? <span className="queues-muted">unspecified</span>}<HintBadges row={{ hints: row.hints ?? [], class_hint: row.class_hint ?? null, structure: row.structure }} /></td>
              <td>{row.decision.confidence.toFixed(2)}{modelRangeText(row.decision) ? <span className="queues-secondary">{modelRangeText(row.decision).trim().replace(/^\(|\)$/g, "")}</span> : null}</td>
              <td>{agreementText(row.decision) ?? <span className="queues-muted">not yet judged</span>}</td>
              <td>{row.review_status ?? ""}</td>
              <td><Link href={row.review_url} aria-label={`Open roof ${row.object_id} in the editor`}>Open</Link></td>
            </tr>)}
          </QueueTable>}
      </>}
  </section>;
}

export default function QueuesPage() {
  const [summary, setSummary] = useState<Loadable<QueuesSummary>>(LOADING);
  const [calibration, setCalibration] = useState<Loadable<HintedCalibration>>(LOADING);
  const [hidden, setHidden] = useState<Loadable<HintedHidden>>(LOADING);
  const [attempts, setAttempts] = useState<Record<QueuePart, number>>({ summary: 0, calibration: 0, hidden: 0 });
  const [hideFlagged, setHideFlagged] = useState(false);
  // Per-table fragment views, keyed by band or "hidden"; unchecked (fragments out of view) until asked.
  const [fragmentViews, setFragmentViews] = useState<FragmentViews>({});
  const showFragments = (table: string, shown: boolean) => setFragmentViews(current => ({ ...current, [table]: shown }));
  // Per-table auto-tier views, keyed the same way; unchecked (auto rows in view) until the reviewer decides otherwise.
  const [autoViews, setAutoViews] = useState<AutoViews>({});
  const hideAuto = (table: string, hidden: boolean) => setAutoViews(current => ({ ...current, [table]: hidden }));
  const [machine, setMachine] = useState<Loadable<MachineLabelledQueue & { source?: string }>>(LOADING);
  const [machineAttempts, setMachineAttempts] = useState(0);
  const planId = summary.state === "ready" ? summary.data.calibration?.plan_id ?? null : null;

  useEffect(() => {
    const controller = new AbortController();
    readJson("/api/queues/summary", controller.signal, coerceSummary)
      .then(data => { if (!controller.signal.aborted) setSummary({ state: "ready", data }); })
      .catch(error => { if (!controller.signal.aborted) setSummary({ state: "error", message: describe(error) }); });
    return () => controller.abort();
  }, [attempts.summary]);

  useEffect(() => {
    if (!planId) return;
    const controller = new AbortController();
    readJson(`/api/queues/calibration?plan=${encodeURIComponent(planId)}`, controller.signal, coerceHintedCalibration)
      .then(data => { if (!controller.signal.aborted) setCalibration({ state: "ready", data }); })
      .catch(error => { if (!controller.signal.aborted) setCalibration({ state: "error", message: describe(error) }); });
    return () => controller.abort();
  }, [planId, attempts.calibration]);

  useEffect(() => {
    const controller = new AbortController();
    readJson("/api/queues/hidden", controller.signal, coerceHintedHidden)
      .then(data => { if (!controller.signal.aborted) setHidden({ state: "ready", data }); })
      .catch(error => { if (!controller.signal.aborted) setHidden({ state: "error", message: describe(error) }); });
    return () => controller.abort();
  }, [attempts.hidden]);

  useEffect(() => {
    const controller = new AbortController();
    readJson("/api/queues/machine-labelled", controller.signal, value => ({ ...coerceMachineLabelledQueue(value), source: typeof (value as { source?: unknown })?.source === "string" ? (value as { source: string }).source : undefined }))
      .then(data => { if (!controller.signal.aborted) setMachine({ state: "ready", data }); })
      .catch(error => { if (!controller.signal.aborted) setMachine({ state: "error", message: describe(error) }); });
    return () => controller.abort();
  }, [machineAttempts]);

  const retry = (part: QueuePart) => {
    if (part === "summary") { setSummary(LOADING); setCalibration(LOADING); }
    if (part === "calibration") setCalibration(LOADING);
    if (part === "hidden") setHidden(LOADING);
    setAttempts(current => ({ ...current, [part]: current[part] + 1 }));
  };

  return <main className="review-shell queues-shell">
    <WorkbenchHeader current="queues" />
    <div className="queues-content">
      <div className="queues-title">
        <div className="queues-title-row">
          <h1>Review queues</h1>
          <label className="queues-filter"><input type="checkbox" checked={hideFlagged} onChange={event => setHideFlagged(event.target.checked)} /> Hide flagged</label>
        </div>
        <p>Each row links into the editor. Nothing on this page approves, rejects or changes a roof.</p>
        <p>Badges are agent hints, not labels: tree-like, secondary structure, non-residential context and commercial come from rules over the image, Vicmap properties, nearby places and OpenStreetMap land use; structure kinds (shipping container, container stack, shed, garage, carport, pergola or shade, water tank, portable classroom, pool cover, fragment) come from the shape and appearance of small roofs. Hover a badge for its evidence; the filter only changes this view. Fragments are pieces of an outline rather than buildings, so each table keeps them out of view until its Show fragments checkbox is ticked. Tier badges (Auto, Assist, Review) carry the decision policy&apos;s machine label: the auto tier proceeds without a human, assist and review need one. They are machine labels, not reviewer labels, and stay in view until a table&apos;s Hide auto tier checkbox is ticked; applying one to an object happens only in the editor.</p>
      </div>
      <MachineLabelledSection queue={machine} onRetry={() => { setMachine(LOADING); setMachineAttempts(n => n + 1); }} />
      <CalibrationSection summary={summary} calibration={calibration} hideFlagged={hideFlagged} fragmentViews={fragmentViews ?? {}} onShowFragments={showFragments} autoViews={autoViews ?? {}} onHideAuto={hideAuto} onRetry={() => retry("calibration")} onRetrySummary={() => retry("summary")} />
      <HiddenSection summary={summary} hidden={hidden} hideFlagged={hideFlagged} showFragments={fragmentViews?.hidden === true} onShowFragments={shown => showFragments("hidden", shown)} hideAuto={autoViews?.hidden === true} onHideAuto={hidden => hideAuto("hidden", hidden)} onRetry={() => retry("hidden")} />
      <SummarySections summary={summary} onRetry={() => retry("summary")} />
    </div>
  </main>;
}
