import type { Item } from "../../lib/annotations/model";
import { currentAgentReview, latestAgentReviews } from "../../lib/annotations/agent-reviews";

export default function AgentReviews({ item, annotationId }: { item: Item; annotationId?: string }) {
  const reviews = latestAgentReviews(item, annotationId);
  return <section className="fw-inspector-section" data-testid="fw-agent-reviews">
    <h3>Agent review</h3>
    <p className="fw-field-help">Independent assessments. Your review below remains your own decision.</p>
    {!reviews.length ? <p className="fw-field-help">No agent assessment saved{annotationId ? " for this outline" : ""}.</p> : reviews.map((review) => <article key={review.id} className="fw-agent-review">
      <div><strong>{review.reviewer} · {review.decision.replaceAll("_", " ")}</strong><span>{currentAgentReview(review, item) ? "Matches current evidence" : "Earlier evidence — review again"}</span></div>
      <small>{review.annotationId ? `Outline ${review.annotationId}` : "Whole item"} · reviewed revision {review.basedOnRevision}{review.vegetationType ? ` · proposed ${review.vegetationType}` : ""}</small>
      <p>{review.notes}</p><small>{new Date(review.createdAt).toLocaleString()}</small>
    </article>)}
    {!!item.agentReviews?.length && <details><summary>Agent review history ({item.agentReviews.length})</summary><pre className="fw-agent-history">{JSON.stringify(item.agentReviews.map(({ subject, ...review }) => { void subject; return review; }), null, 2)}</pre></details>}
  </section>;
}
