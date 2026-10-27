import assert from "node:assert/strict";
import { test } from "node:test";
import { build } from "esbuild";
const bundle = await build({ entryPoints: [new URL("./agent-reviews.ts", import.meta.url).pathname], bundle: true, write: false, platform: "node", format: "esm" });
const { agentReviewSubject, currentAgentReview, latestAgentReviews } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`);
const item = { title: "Tree proposal", adapter: "image", source: { imageSha256: "frozen", width: 100 }, notes: "", revision: 1, status: "unreviewed", annotations: [
  { id: "a", labelId: "tree", geometry: { type: "Polygon", coordinates: [[[0,0],[1,0],[1,1],[0,0]]] }, properties: { addr: { kind: "tree" } } },
  { id: "b", geometry: { type: "Point", coordinates: [0.5,0.5] } },
] };
const review = { id: "r1", reviewer: "codex", annotationId: "a", decision: "approved", source: "agent:fieldwork", humanApproval: false, trainingEligible: false, basedOnRevision: 1, subject: agentReviewSubject(item, "a") };

test("human status and checks stay independent; changed source, outline or class stales an assessment", () => {
  const edited = structuredClone(item);
  edited.status = "rejected"; edited.revision = 4; edited.notes = "Human disagrees";
  edited.annotations[0].properties.addr.overrides = { status: "rejected", geometry_reviewed: true };
  assert.equal(currentAgentReview(review, edited), true);
  edited.annotations[1].geometry.coordinates = [0.1,0.1];
  assert.equal(currentAgentReview(review, edited), true);
  for (const mutate of [
    (copy) => { copy.source.imageSha256 = "changed"; },
    (copy) => { copy.annotations[0].geometry.coordinates[0][1][0] = 0.9; },
    (copy) => { copy.annotations[0].properties.addr.overrides.kind = "building"; },
    (copy) => { copy.annotations = []; },
  ]) { const copy = structuredClone(edited); mutate(copy); assert.equal(currentAgentReview(review, copy), false); }
  assert.equal(currentAgentReview({ ...review, humanApproval: true }, edited), false);
});

test("latest reviews retain separate agent identities and targets with history available", () => {
  const subject = { ...item, agentReviews: [review, { ...review, id: "r2", decision: "uncertain", supersedes: "r1" }, { ...review, id: "r3", reviewer: "other" }, { ...review, id: "r4", annotationId: "b" }] };
  assert.deepEqual(latestAgentReviews(subject, "a").map((r) => r.id), ["r3", "r2"]);
  assert.equal(subject.agentReviews.length, 4);
});
