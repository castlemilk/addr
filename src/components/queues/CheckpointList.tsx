"use client";
import Link from "next/link";
import { ArrowLeft, MapPin } from "lucide-react";
import { parse, render } from "@/lib/markdown-lite";

/* Pipeline checkpoints: the list on /queues and the single report on /queues/checkpoints/[id].
   Both are read-only views of data/references/eyes-checkpoints/<id>/REVIEW.md via /api/checkpoints. */

export type CheckpointSummary = { id: string; title: string; created_at: string | null; summary: string | null };
export type CheckpointEntry = CheckpointSummary & {
  address_text?: string | null; address_id?: number | string | null; latitude?: number | null; longitude?: number | null; report_sha256?: string | null; review_path?: string | null; report_url?: string | null;
};

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
/** Locale-independent date so server and client markup agree; the ISO value stays on the element. */
export function formatDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return `${date.getUTCDate()} ${MONTHS[date.getUTCMonth()]} ${date.getUTCFullYear()}`;
}

/** Report titles arrive joined with middle dots and shouting the program name (EYES CP2, then the suburb, then the date); the workspace reads them as
 *  comma-separated sentence-case phrases. Only whole words of four or more capitals change, so ids, state codes and codes
 *  with digits (CP2-CALIB, SAM3, VIC, CRM) stay as written. */
export const displayTitle = (title: string | null | undefined, fallback: string) =>
  (title || fallback).replace(/\s+\u00b7\s+/g, ", ").replace(/(?<![\w-])([A-Z])([A-Z]{3,})(?![\w-])/g, (_, first: string, rest: string) => first + rest.toLowerCase());

export function CheckpointList({ checkpoints }: { checkpoints: CheckpointSummary[] }) {
  if (checkpoints.length === 0) return <p className="queues-state" role="status">No checkpoints have been filed yet. Reports appear here after task eyes:checkpoint runs.</p>;
  return <ol className="queues-checkpoints">
    {checkpoints.map(checkpoint => <li className="queues-checkpoint" key={checkpoint.id}>
      <h3>{displayTitle(checkpoint.title, checkpoint.id)}</h3>
      {checkpoint.created_at ? <time dateTime={checkpoint.created_at}>{formatDate(checkpoint.created_at)}</time> : <span className="queues-muted">Date not recorded</span>}
      {checkpoint.summary ? <p>{checkpoint.summary}</p> : null}
      <Link href={`/queues/checkpoints/${encodeURIComponent(checkpoint.id)}`}>Read report</Link>
    </li>)}
  </ol>;
}

/** Title comes from the checkpoint index, then the report's own first heading, then the id. */
export function reportTitle(id: string, entry: CheckpointEntry | null, markdown: string): { title: string; body: string } {
  const heading = /^#\s+(.+?)\s*$/m.exec(markdown.trimStart().split("\n")[0] ?? "");
  const body = heading ? markdown.trimStart().split("\n").slice(1).join("\n").replace(/^\s*\n/, "") : markdown;
  return { title: displayTitle(entry?.title || heading?.[1], id), body };
}

/** `address` is the anchor address already in display form (the page formats it with displayAddress). */
export function CheckpointReport({ id, entry, markdown, address }: { id: string; entry: CheckpointEntry | null; markdown: string; address?: string | null }) {
  const { title, body } = reportTitle(id, entry, markdown);
  const location = entry && typeof entry.latitude === "number" && typeof entry.longitude === "number" ? { lat: entry.latitude, lon: entry.longitude } : null;
  return <article className="queues-report">
    <nav className="queues-report-actions" aria-label="Checkpoint links">
      <Link href="/queues"><ArrowLeft size={14} aria-hidden="true" />Back to queues</Link>
      {location ? <Link href={`/?lat=${location.lat}&lon=${location.lon}&crm=1`}><MapPin size={14} aria-hidden="true" />Open on map</Link> : null}
    </nav>
    <h1>{title}</h1>
    <p className="queues-report-meta">
      {entry?.created_at ? <time dateTime={entry.created_at}>{formatDate(entry.created_at)}</time> : null}
      {address ? <span>Anchor address {address}</span> : null}
      <span>Diagnostics and reference agreement for review; bands and agreement are model estimates, not accuracy.</span>
    </p>
    {/* The report's own title is the h1 above, so its ## sections render as h2; if the body still holds
        another # heading, keep markdown-lite's default demotion so the page never has two h1s. */}
    {body.trim() === ""
      ? <p className="queues-state" role="status">Checkpoint {id} has no REVIEW.md report on this machine; only its index entry above was filed.</p>
      : <div className="queues-report-body">{render(parse(body), { headingOffset: /^#\s/m.test(body) ? 1 : 0 })}</div>}
  </article>;
}
