"use client";
import Link from "next/link";
import type { ReactNode } from "react";
import { CheckpointList, type CheckpointEntry, type CheckpointSummary } from "@/components/queues/CheckpointList";
import { reviewUrl } from "@/lib/queues";

/* Read-only views for the Review queues workspace. Every row links into the canonical /scenes
   editor; nothing here approves, rejects or changes a roof. Payload types mirror the read-only
   routes under /api/queues (see UX-CONTRACT.md "Review queues" and docs/roof-eyes.md). */

export type Band = "low" | "medium" | "high";
export const BANDS: readonly Band[] = ["low", "medium", "high"];
export type CalibrationStatus = "pending" | "confirmed" | "rejected" | "needs_adjustment" | "stale";

export type QueuesSummary = {
  /** null until a calibration plan has been sampled. */
  calibration: { plan_id: string; rows: number; groups_total_by_band?: Record<string, number>; groups_reviewed_by_band: Record<string, number>; intervals_by_band: Record<string, [number, number] | null> } | null;
  /** count is null when the eyes index is absent. */
  hidden: { count: number | null };
  self_review: { run: string | null; status: string; total: number; completed?: number; outcomes: { no_safe_gain: number; needs_evidence: number; candidate_ready: number } };
  roof_review: { cohort?: string; reviewed: number; total: number };
  checkpoints: CheckpointSummary[];
  note?: string;
};
export type BandProgress = { groups_total: number; groups_reviewed: number; successes: number; failures: number; interval: [number, number] | null; minimum_groups: number; remaining_to_interval?: number };
export type CalibrationRow = {
  band: Band; group: string; scene_id: string; object_id: string; review_url: string; area_m2: number | null; detector_score: number | null;
  status: CalibrationStatus | string; counted: boolean; success: boolean | null; address: string | null; address_id: number | string | null; detail?: string | null; iou?: number | null;
};
export type CalibrationQueue = { plan_id: string; policy: string | null; statement: string | null; iou_success: number; minimum_groups?: number; bands: Partial<Record<Band, BandProgress>>; rows: CalibrationRow[]; note?: string };
export type HiddenRow = {
  scene_id: string; object_id: string; band: string | null; detector_score: number | null; observed_segments: number | null;
  other_scene_id: string; other_object_id: string; other_band: string | null; other_detector_score: number | null; iou: number | null;
  hidden_url: string; shown_url: string; address: string | null;
};
export type HiddenQueue = { version: string; count: number; rows: HiddenRow[]; note?: string };
export type Loadable<T> = { state: "loading" } | { state: "ready"; data: T } | { state: "error"; message: string };
export type QueuePart = "summary" | "calibration" | "hidden";

// --- Payload guards ------------------------------------------------------------------------------------------
// A 200 whose body is not the contract (a proxy's HTML page, an empty object) must land in the section's error
// state with a retry, never in a blank page. Essentials are required; counts default to 0, texts to null, and a
// row without both ids is dropped because it could not link into the editor.
const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const str = (value: unknown): string | null => typeof value === "string" ? value : null;
const finite = (value: unknown): number | null => typeof value === "number" && Number.isFinite(value) ? value : null;
const whole = (value: unknown): number => Number.isInteger(value) && (value as number) >= 0 ? value as number : 0;
const pair = (value: unknown): [number, number] | null => Array.isArray(value) && value.length === 2 && finite(value[0]) !== null && finite(value[1]) !== null ? [value[0], value[1]] : null;
const counts = (value: unknown): Record<string, number> => isRecord(value) ? Object.fromEntries(Object.entries(value).map(([k, v]) => [k, whole(v)])) : {};
const unexpected = (url: string) => new Error(`${url} answered with an unexpected shape, so nothing was read from it`);

export function coerceCheckpointList(value: unknown, url: string): CheckpointEntry[] {
  if (!isRecord(value)) throw unexpected(url);
  if (!Array.isArray(value.checkpoints)) return [];
  const out: CheckpointEntry[] = [];
  for (const raw of value.checkpoints) {
    if (!isRecord(raw)) continue;
    const id = str(raw.id);
    if (!id) continue;
    out.push({ id, title: str(raw.title) ?? id, created_at: str(raw.created_at), summary: str(raw.summary), address_text: str(raw.address_text),
      address_id: typeof raw.address_id === "number" || typeof raw.address_id === "string" ? raw.address_id : null,
      latitude: finite(raw.latitude), longitude: finite(raw.longitude), report_sha256: str(raw.report_sha256), review_path: str(raw.review_path), report_url: str(raw.report_url) });
  }
  return out;
}
export function coerceSummary(value: unknown, url: string): QueuesSummary {
  if (!isRecord(value) || value.calibration === undefined || !isRecord(value.hidden) || !isRecord(value.self_review) || !isRecord(value.roof_review)) throw unexpected(url);
  const calibration = value.calibration;
  if (calibration !== null && (!isRecord(calibration) || !str(calibration.plan_id))) throw unexpected(url);
  const self = value.self_review, outcomes = isRecord(self.outcomes) ? self.outcomes : {};
  return {
    calibration: calibration === null ? null : { plan_id: calibration.plan_id as string, rows: whole(calibration.rows),
      ...(calibration.groups_total_by_band === undefined ? {} : { groups_total_by_band: counts(calibration.groups_total_by_band) }),
      groups_reviewed_by_band: counts(calibration.groups_reviewed_by_band),
      intervals_by_band: isRecord(calibration.intervals_by_band) ? Object.fromEntries(Object.entries(calibration.intervals_by_band).map(([k, v]) => [k, pair(v)])) : {} },
    hidden: { count: Number.isInteger(value.hidden.count) ? value.hidden.count as number : null },
    self_review: { run: str(self.run), status: str(self.status) ?? "unknown", total: whole(self.total), ...(self.completed === undefined ? {} : { completed: whole(self.completed) }),
      outcomes: { no_safe_gain: whole(outcomes.no_safe_gain), needs_evidence: whole(outcomes.needs_evidence), candidate_ready: whole(outcomes.candidate_ready) } },
    roof_review: { ...(value.roof_review.cohort === undefined ? {} : { cohort: str(value.roof_review.cohort) ?? undefined }), reviewed: whole(value.roof_review.reviewed), total: whole(value.roof_review.total) },
    checkpoints: coerceCheckpointList(value, url).map(({ id, title, created_at, summary }) => ({ id, title, created_at, summary })),
    ...(value.note === undefined ? {} : { note: str(value.note) ?? undefined }),
  };
}
export function coerceCalibration(value: unknown, url: string): CalibrationQueue {
  if (!isRecord(value) || !str(value.plan_id) || !Array.isArray(value.rows)) throw unexpected(url);
  const minimum = Number.isInteger(value.minimum_groups) && (value.minimum_groups as number) > 0 ? value.minimum_groups as number : 20;
  const rows: CalibrationRow[] = [];
  for (const raw of value.rows) {
    if (!isRecord(raw)) continue;
    const sceneId = str(raw.scene_id), objectId = str(raw.object_id);
    if (!sceneId || !objectId) continue;
    rows.push({ band: (str(raw.band) ?? "") as Band, group: str(raw.group) ?? "", scene_id: sceneId, object_id: objectId, review_url: str(raw.review_url) ?? reviewUrl(sceneId, objectId),
      area_m2: finite(raw.area_m2), detector_score: finite(raw.detector_score), status: str(raw.status) ?? "pending", counted: raw.counted === true,
      success: typeof raw.success === "boolean" ? raw.success : null, address: str(raw.address),
      address_id: typeof raw.address_id === "number" || typeof raw.address_id === "string" ? raw.address_id : null,
      ...(raw.detail === undefined ? {} : { detail: str(raw.detail) }), ...(raw.iou === undefined ? {} : { iou: finite(raw.iou) }) });
  }
  // A band record without groups_total falls back to the rows it holds, so the intro never says "0 low" above a low table.
  const bands: Partial<Record<Band, BandProgress>> = {};
  if (isRecord(value.bands)) for (const band of BANDS) {
    const raw = value.bands[band];
    if (!isRecord(raw)) continue;
    bands[band] = { groups_total: Number.isInteger(raw.groups_total) ? whole(raw.groups_total) : rows.filter(row => row.band === band).length,
      groups_reviewed: whole(raw.groups_reviewed), successes: whole(raw.successes), failures: whole(raw.failures),
      interval: pair(raw.interval), minimum_groups: Number.isInteger(raw.minimum_groups) && (raw.minimum_groups as number) > 0 ? raw.minimum_groups as number : minimum,
      ...(raw.remaining_to_interval === undefined ? {} : { remaining_to_interval: whole(raw.remaining_to_interval) }) };
  }
  return { plan_id: value.plan_id as string, policy: str(value.policy), statement: str(value.statement), iou_success: finite(value.iou_success) ?? 0.85,
    ...(value.minimum_groups === undefined ? {} : { minimum_groups: minimum }), bands, rows, ...(value.note === undefined ? {} : { note: str(value.note) ?? undefined }) };
}
export function coerceHidden(value: unknown, url: string): HiddenQueue {
  if (!isRecord(value) || !Array.isArray(value.rows)) throw unexpected(url);
  const rows: HiddenRow[] = [];
  for (const raw of value.rows) {
    if (!isRecord(raw)) continue;
    const sceneId = str(raw.scene_id), objectId = str(raw.object_id), otherScene = str(raw.other_scene_id), otherObject = str(raw.other_object_id);
    if (!sceneId || !objectId || !otherScene || !otherObject) continue;
    rows.push({ scene_id: sceneId, object_id: objectId, band: str(raw.band), detector_score: finite(raw.detector_score), observed_segments: finite(raw.observed_segments),
      other_scene_id: otherScene, other_object_id: otherObject, other_band: str(raw.other_band), other_detector_score: finite(raw.other_detector_score), iou: finite(raw.iou),
      hidden_url: str(raw.hidden_url) ?? reviewUrl(sceneId, objectId), shown_url: str(raw.shown_url) ?? reviewUrl(otherScene, otherObject), address: str(raw.address) });
  }
  return { version: str(value.version) ?? "", count: Number.isInteger(value.count) && (value.count as number) >= rows.length ? value.count as number : rows.length, rows,
    ...(value.note === undefined ? {} : { note: str(value.note) ?? undefined }) };
}
export function coerceCheckpoint(value: unknown, url: string): { id: string; markdown: string } {
  if (!isRecord(value) || !str(value.id) || typeof value.markdown !== "string") throw unexpected(url);
  return { id: value.id as string, markdown: value.markdown };
}

/** GET one read-only route with the page's abort signal and a 60 s ceiling; the coercer turns a mis-shaped 200 into an error the section can show. */
export async function readJson<T>(url: string, signal: AbortSignal, coerce: (value: unknown, url: string) => T): Promise<T> {
  const ceiling = AbortSignal.timeout(60000);
  const response = await fetch(url, { signal: typeof AbortSignal.any === "function" ? AbortSignal.any([signal, ceiling]) : signal, cache: "no-store" });
  const result: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new Error((isRecord(result) && str(result.error)) || `${url} answered ${response.status}`);
  return coerce(result, url);
}
export const describeError = (error: unknown) => error instanceof Error ? (error.name === "TimeoutError" ? "the read took longer than 60 seconds" : error.message) : String(error);

const STATES = new Set(["ACT", "NSW", "NT", "QLD", "SA", "TAS", "VIC", "WA"]);
/** G-NAF addresses arrive in capitals; show them in sentence case, keeping state codes and unit numbers such as 3A. */
export function displayAddress(address: string | null | undefined): string {
  if (!address) return "No address match";
  return address.split(" ").map(word => {
    const core = word.replace(/[^A-Za-z]/g, "");
    if (!core || /\d/.test(word) || STATES.has(core) || core !== core.toUpperCase()) return word;
    return word.replace(/[A-Z]+/g, letters => letters.charAt(0) + letters.slice(1).toLowerCase());
  }).join(" ");
}
export const number = (n: number) => n.toLocaleString("en-AU");
export const plural = (n: number, one: string, many: string) => `${number(n)} ${n === 1 ? one : many}`;
export const score = (n: number | null | undefined) => typeof n === "number" && Number.isFinite(n) ? n.toFixed(2) : "—";
/** The z17 tile a scene was cut from (last two id parts), falling back to the group key. */
export function tileOf(sceneId: string, group?: string): string {
  const match = /(\d+)-(\d+)$/.exec(sceneId);
  return match ? `${match[1]}-${match[2]}` : (group ? group.replace(/^z\d+:/, "").replace(":", "-") : sceneId);
}

const STATUS_LABELS: Record<string, { label: string; tone: "pending" | "approval" | "rejection" | "correction" }> = {
  pending: { label: "Pending", tone: "pending" },
  confirmed: { label: "Confirmed", tone: "approval" },
  rejected: { label: "Rejected", tone: "rejection" },
  needs_adjustment: { label: "Needs correction", tone: "correction" },
  stale: { label: "Feedback needs recovery", tone: "correction" },
};
export function StatusLabel({ status }: { status: string }) {
  const entry = STATUS_LABELS[status] ?? { label: status.replace(/_/g, " "), tone: "pending" as const };
  return <span className={`queues-status queues-status-${entry.tone}`}>{entry.label}</span>;
}

/** A bordered 8 px panel whose table scrolls within it on narrow screens. */
export function QueueTable({ columns, children, caption, ...attributes }: { columns: string[]; children: ReactNode; caption: string; "data-band"?: string; "data-queue"?: string }) {
  return <div className="queues-panel">
    <table className="queues-table" {...attributes}>
      <caption className="queues-visually-hidden">{caption}</caption>
      <thead><tr>{columns.map(column => <th key={column} scope="col">{column}</th>)}</tr></thead>
      <tbody>{children}</tbody>
    </table>
  </div>;
}

export function ReadingState({ children }: { children: ReactNode }) {
  return <p className="queues-state" role="status">{children}</p>;
}
export function ErrorState({ message, action, onRetry }: { message: string; action: string; onRetry: () => void }) {
  return <div className="review-notice queues-error" role="alert">{message} <button type="button" className="review-button" onClick={onRetry}>{action}</button></div>;
}

function intervalText(progress: BandProgress, iouSuccess: number): string {
  if (progress.interval) {
    const [low, high] = progress.interval;
    return `Wilson 95%: ${low.toFixed(2)}–${high.toFixed(2)} (boundary IoU >= ${iouSuccess}, sampled roofs, not per-house accuracy)`;
  }
  if (progress.groups_total < progress.minimum_groups) return `Interval not available: ${number(progress.groups_total)} groups cannot reach ${number(progress.minimum_groups)}`;
  const remaining = Math.max(0, progress.minimum_groups - progress.groups_reviewed);
  return `Interval not yet available (${number(remaining)} more reviewed groups needed)`;
}
const EMPTY_BAND: BandProgress = { groups_total: 0, groups_reviewed: 0, successes: 0, failures: 0, interval: null, minimum_groups: 20 };

export function CalibrationSection({ summary, calibration, onRetry, onRetrySummary }: { summary: Loadable<QueuesSummary>; calibration: Loadable<CalibrationQueue>; onRetry: () => void; onRetrySummary: () => void }) {
  const planned = summary.state === "ready" ? summary.data.calibration : undefined;
  const planId = calibration.state === "ready" ? calibration.data.plan_id : planned?.plan_id ?? null;
  return <section className="queues-section" aria-labelledby="queues-calibration">
    <h2 id="queues-calibration">{planId ? `Calibration sample, plan ${planId}` : "Calibration sample"}</h2>
    {calibration.state === "ready" ? <CalibrationTables queue={calibration.data} />
      : calibration.state === "error" ? <ErrorState message={`The calibration queue could not be read: ${calibration.message}`} action="Retry calibration" onRetry={onRetry} />
      : planned === null ? <ReadingState>No calibration plan has been sampled yet. Run task eyes:calibration to draw the fixed-hash sample; its review queue appears here.</ReadingState>
      : summary.state === "error" ? <ErrorState message={`The queue summary could not be read, so the calibration plan is unknown: ${summary.message}`} action="Retry summary" onRetry={onRetrySummary} />
      : <ReadingState>{planned ? `Reading plan ${planned.plan_id} and ${number(planned.rows)} review files` : "Reading the queue summary"}</ReadingState>}
  </section>;
}

function CalibrationTables({ queue }: { queue: CalibrationQueue }) {
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
      const rows = queue.rows.filter(row => row.band === band), p = progress(band);
      return <div className="queues-band" key={band}>
        <div className="queues-band-heading">
          <h3 id={`queues-band-${band}`}>{band.charAt(0).toUpperCase() + band.slice(1)} band</h3>
          <span className="queues-progress">{number(p.groups_reviewed)} of {number(p.groups_total)} reviewed</span>
          <span className="queues-interval">{intervalText(p, queue.iou_success)}</span>
        </div>
        {rows.length === 0 ? <ReadingState>No sampled roofs in the {band} band.</ReadingState>
          : <QueueTable caption={`${band} band calibration roofs`} data-band={band} columns={["Address", "Tile", "Detector (model estimate)", "Status", "Open"]}>
            {rows.map(row => <tr key={`${row.scene_id}/${row.object_id}`}>
              <td><span className={row.address ? undefined : "queues-muted"}>{displayAddress(row.address)}</span><span className="queues-secondary">{row.object_id}</span></td>
              <td>{tileOf(row.scene_id, row.group)}</td>
              <td>{score(row.detector_score)}</td>
              <td><StatusLabel status={row.status} />{row.counted && row.success !== null ? <span className="queues-secondary">{row.success ? "counts as a success" : "counts as a failure"}</span> : null}</td>
              <td><Link href={row.review_url} aria-label={`Open ${row.address ? displayAddress(row.address) : `roof ${row.object_id}`} in the editor`}>Open</Link></td>
            </tr>)}
          </QueueTable>}
      </div>;
    })}
  </>;
}

export function HiddenSection({ summary, hidden, onRetry }: { summary: Loadable<QueuesSummary>; hidden: Loadable<HiddenQueue>; onRetry: () => void }) {
  const known = hidden.state === "ready" ? hidden.data.count : summary.state === "ready" ? summary.data.hidden.count : null;
  const bandText = (band: string | null) => band ?? "band unknown";
  return <section className="queues-section" id="hidden" aria-labelledby="queues-hidden">
    <h2 id="queues-hidden">Low roofs hidden under higher bands</h2>
    {hidden.state === "error" ? <ErrorState message={`The hidden-roof index could not be read: ${hidden.message}`} action="Retry hidden roofs" onRetry={onRetry} />
      : hidden.state === "loading" ? <ReadingState>{known === null ? "Reading hidden roofs from the cross-capture index" : `Reading ${number(known)} hidden roofs`}</ReadingState>
      : <>
        <p className="queues-intro">The map shows only the highest-band capture of a roof. {hidden.data.count === 1 ? "This 1 low-evidence scan roof sits" : `These ${number(hidden.data.count)} low-evidence scan roofs sit`} under a medium or high capture of the same roof; both are model estimates.</p>
        {hidden.data.rows.length === 0 ? <ReadingState>No low roofs are hidden under a higher band.</ReadingState>
          : <QueueTable caption="Low roofs hidden under higher bands" data-queue="hidden" columns={["Address", "Hidden roof (band, detector, lines)", "Shown as (band, detector, capture)", "IoU", "Open hidden", "Open shown"]}>
            {hidden.data.rows.map(row => <tr key={`${row.scene_id}/${row.object_id}`}>
              <td><span className={row.address ? undefined : "queues-muted"}>{displayAddress(row.address)}</span><span className="queues-secondary">{row.scene_id} {row.object_id}</span></td>
              <td>{bandText(row.band)}, detector {score(row.detector_score)}, {row.observed_segments === null ? "no lines observed" : `${number(row.observed_segments)} lines`}</td>
              <td>{bandText(row.other_band)}, detector {score(row.other_detector_score)}, {row.other_scene_id}</td>
              <td>{score(row.iou)}</td>
              <td><Link href={row.hidden_url}>Open hidden</Link></td>
              <td><Link href={row.shown_url}>Open shown</Link></td>
            </tr>)}
          </QueueTable>}
      </>}
  </section>;
}

export function SummarySections({ summary, onRetry }: { summary: Loadable<QueuesSummary>; onRetry: () => void }) {
  if (summary.state === "error") return <ErrorState message={`The queue summary could not be read: ${summary.message}`} action="Retry summary" onRetry={onRetry} />;
  if (summary.state === "loading") return <ReadingState>Reading the queue summary</ReadingState>;
  const { self_review: self, roof_review: roof, checkpoints } = summary.data;
  return <>
    <section className="queues-section" aria-labelledby="queues-self-review">
      <h2 id="queues-self-review">Self-review batch</h2>
      <p className="queues-line">
        <span>{self.run ? `Run ${self.run}: ${self.status}, ${number(self.total)} roofs; ${number(self.outcomes.no_safe_gain)} no safe gain, ${number(self.outcomes.needs_evidence)} need evidence, ${number(self.outcomes.candidate_ready)} candidate ready.` : "No self-review batch has run yet."}</span>
        {self.run ? <span className="queues-muted">Outcomes are the loop&apos;s own model estimates, not human reviews.</span> : null}
        <Link href="/scenes/processing?view=self-review">Open the self-review view</Link>
      </p>
    </section>
    <section className="queues-section" aria-labelledby="queues-roof-review">
      <h2 id="queues-roof-review">Roof training review</h2>
      <p className="queues-line">
        <span className="queues-progress">{number(roof.reviewed)} of {number(roof.total)} reviewed</span>
        <Link href="/reviews?cohort=roof_training_250">Open roof review</Link>
      </p>
    </section>
    <section className="queues-section" aria-labelledby="queues-checkpoints">
      <h2 id="queues-checkpoints">Pipeline checkpoints</h2>
      <CheckpointList checkpoints={checkpoints} />
    </section>
  </>;
}

/** The whole hub body below the h1, given each payload's load state. */
export function QueuesBody({ summary, calibration, hidden, onRetry }: { summary: Loadable<QueuesSummary>; calibration: Loadable<CalibrationQueue>; hidden: Loadable<HiddenQueue>; onRetry: (part: QueuePart) => void }) {
  return <>
    <CalibrationSection summary={summary} calibration={calibration} onRetry={() => onRetry("calibration")} onRetrySummary={() => onRetry("summary")} />
    <HiddenSection summary={summary} hidden={hidden} onRetry={() => onRetry("hidden")} />
    <SummarySections summary={summary} onRetry={() => onRetry("summary")} />
  </>;
}
