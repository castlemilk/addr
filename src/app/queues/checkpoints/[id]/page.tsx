"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { WorkbenchHeader } from "@/components/shell/WorkbenchHeader";
import { CheckpointReport, type CheckpointEntry } from "@/components/queues/CheckpointList";
import { ErrorState, ReadingState, coerceCheckpoint, coerceCheckpointList, describeError, displayAddress, readJson } from "@/components/queues/QueueTable";
import "../../../reviews/reviews.css";
import "../../queues.css";

/* /queues/checkpoints/[id]: one pipeline checkpoint report, rendered read-only from
   /api/checkpoints/[id] with its index entry (title, anchor address, coordinates) from /api/checkpoints. */

type State = { state: "loading" } | { state: "ready"; markdown: string; entry: CheckpointEntry | null } | { state: "error"; message: string };

/** A malformed percent-encoding in the address bar is shown as written; the route answers 400 for it anyway. */
const decode = (value: string) => { try { return decodeURIComponent(value); } catch { return value; } };

export default function CheckpointPage() {
  const params = useParams<{ id: string }>();
  const id = typeof params?.id === "string" ? decode(params.id) : "";
  const [state, setState] = useState<State>({ state: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!id) return;
    const controller = new AbortController();
    const report = readJson(`/api/checkpoints/${encodeURIComponent(id)}`, controller.signal, coerceCheckpoint);
    const index = readJson("/api/checkpoints", controller.signal, coerceCheckpointList).then(list => list.find(entry => entry.id === id) ?? null).catch(() => null);
    Promise.all([report, index])
      .then(([data, entry]) => { if (!controller.signal.aborted) setState({ state: "ready", markdown: data.markdown, entry }); })
      .catch(error => { if (!controller.signal.aborted) setState({ state: "error", message: describeError(error) }); });
    return () => controller.abort();
  }, [id, attempt]);

  return <main className="review-shell queues-shell">
    <WorkbenchHeader current="queues" />
    <div className="queues-content">
      {state.state === "ready" ? <CheckpointReport id={id} entry={state.entry} markdown={state.markdown} address={state.entry?.address_text ? displayAddress(state.entry.address_text) : null} /> : <article className="queues-report">
        <nav className="queues-report-actions" aria-label="Checkpoint links"><Link href="/queues"><ArrowLeft size={14} aria-hidden="true" />Back to queues</Link></nav>
        <h1>{id ? `Checkpoint ${id}` : "Checkpoint"}</h1>
        {state.state === "error"
          ? <ErrorState message={`Checkpoint ${id} could not be read: ${state.message}`} action="Retry" onRetry={() => { setState({ state: "loading" }); setAttempt(current => current + 1); }} />
          : <ReadingState>{id ? `Reading checkpoint ${id}` : "Reading the checkpoint id from the address"}</ReadingState>}
      </article>}
    </div>
  </main>;
}
