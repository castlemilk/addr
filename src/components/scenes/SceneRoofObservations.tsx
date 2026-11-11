"use client";
import { useEffect, useState } from "react";
import { getCrmToken } from "@/lib/crm-client";

type Observation = {
  observation_id: string; reviewer: string; recorded_at: string;
  observed: string; hypothesis: string; ambiguity: string; next_action: string;
};
type Result = {
  identity: { scene_id: string; object_id: string; model_signature: string; image_sha256: string };
  observations: Observation[];
};

export default function SceneRoofObservations({ sceneId, objectId, modelSignature, imageSha256 }: {
  sceneId: string; objectId: string; modelSignature: string; imageSha256: string;
}) {
  const [refresh, setRefresh] = useState(0);
  const requestKey = `${sceneId}/${objectId}/${modelSignature}/${imageSha256}/${refresh}`;
  const [result, setResult] = useState<{ key: string; data: Result | null; error: string } | null>(null);
  const data = result?.key === requestKey ? result.data : null;
  const error = result?.key === requestKey ? result.error : "";
  const loading = result?.key !== requestKey;
  useEffect(() => {
    const controller = new AbortController();
    const token = getCrmToken();
    fetch(`/api/scenes/roof-observations?scene=${encodeURIComponent(sceneId)}&object=${encodeURIComponent(objectId)}`, {
      signal: controller.signal, cache: "no-store", headers: token ? { Authorization: `Bearer ${token}` } : {},
    }).then(async response => {
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Could not load agent observations.");
      if (result.identity?.scene_id !== sceneId || result.identity?.object_id !== objectId
          || result.identity?.model_signature !== modelSignature || result.identity?.image_sha256 !== imageSha256) {
        throw new Error("The source model or image changed. Reload the scene to view current observations.");
      }
      if (!controller.signal.aborted) setResult({ key: requestKey, data: result, error: "" });
    }).catch(error => {
      if (!controller.signal.aborted) setResult({ key: requestKey, data: null, error: error instanceof Error ? error.message : "Could not load agent observations." });
    });
    return () => controller.abort();
  }, [sceneId, objectId, modelSignature, imageSha256, requestKey]);

  return <details className="scene-roof-feedback scene-agent-observations" data-testid="scene-roof-observations" open={loading || !!error || !!data?.observations.length}>
    <summary>Agent roof observations{data?.observations.length ? ` (${data.observations.length})` : ""}</summary>
    <p className="review-muted">Notes on the saved source model and image. Tier 3 observations remain separate from your edits, approvals and training labels.</p>
    {loading && <p role="status">Loading observations…</p>}
    {error && <p role="alert">{error}</p>}
    {!loading && !error && !data?.observations.length && <p className="review-muted">No agent observations for this source.</p>}
    {data?.observations.map(note => <article key={note.observation_id}>
      <p className="review-muted">{note.reviewer} · {new Date(note.recorded_at).toLocaleString("en-AU")}</p>
      <p><strong>Observed:</strong> {note.observed}</p>
      {note.hypothesis && <p><strong>Hypothesis:</strong> {note.hypothesis}</p>}
      {note.ambiguity && <p><strong>Ambiguity:</strong> {note.ambiguity}</p>}
      {note.next_action && <p><strong>Next action:</strong> {note.next_action}</p>}
    </article>)}
    <button type="button" className="review-button" disabled={loading} onClick={() => setRefresh(value => value + 1)}>Refresh observations</button>
  </details>;
}
