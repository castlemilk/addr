import type { AgentReview, Item } from "./model";

const object = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const canonical = (value: unknown): string => JSON.stringify(value, (_key, entry) => entry && typeof entry === "object" && !Array.isArray(entry)
  ? Object.fromEntries(Object.keys(entry).sort().map((key) => [key, entry[key]])) : entry);

export function agentReviewSubject(item: Item, annotationId?: string): Record<string, unknown> {
  const annotations = item.annotations.filter((mark) => !annotationId || mark.id === annotationId).map((mark) => {
    const addr = object(mark.properties?.addr), overrides = object(addr.overrides);
    const entry: Record<string, unknown> = { id: mark.id, labelId: mark.labelId || "", geometry: mark.geometry };
    for (const key of ["kind", "label", "structure_kind", "vegetation_type"]) entry[key] = key in overrides ? overrides[key] : addr[key] ?? null;
    return entry;
  });
  return { adapter: item.adapter, source: item.source, title: item.title, annotations, crop: item.crop ?? null, ...(!annotationId ? { notes: item.notes } : {}) };
}

export function currentAgentReview(review: AgentReview, item: Item): boolean {
  return review.source === "agent:fieldwork" && review.humanApproval === false && review.trainingEligible === false &&
    canonical(review.subject) === canonical(agentReviewSubject(item, review.annotationId));
}

export function latestAgentReviews(item: Item, annotationId?: string): AgentReview[] {
  const latest = new Map<string, AgentReview>();
  for (const review of item.agentReviews || []) {
    if (annotationId && review.annotationId && review.annotationId !== annotationId) continue;
    latest.set(JSON.stringify([review.reviewer, review.annotationId || ""]), review);
  }
  return [...latest.values()].reverse();
}
