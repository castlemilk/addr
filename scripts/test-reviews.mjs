import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import ts from "typescript";

// Compile the small server-owned store in isolation; never touch live reviews.
const dir = await mkdtemp(path.join(tmpdir(), "addr-review-test-"));
try {
  for (const name of ["roof-annotations", "review-types", "review-feedback"]) {
    const source = await readFile(`src/lib/${name}.ts`, "utf8");
    const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText.replace('"./review-types"', '"./review-types.mjs"').replace('"./roof-annotations"', '"./roof-annotations.mjs"');
    await writeFile(path.join(dir, `${name}.mjs`), code);
  }
  const { parseFeedback, saveFeedback, readFeedback, feedbackFor, validCohort } = await import(pathToFileURL(path.join(dir, "review-feedback.mjs")));
  const file = path.join(dir, "feedback.json");
  const cut = [[0, 0], [8, 0]];
  const input = { status: "needs_adjustment", notes: "Ridge intersects canopy", issues: ["apex"], boundary_verified: true, rejected_cuts: [cut], revision: 0 };
  assert.equal(validCohort("../../private"), false);
  assert.throws(() => parseFeedback({ ...input, status: "approved" }));
  assert.throws(() => parseFeedback({ ...input, rejected_cuts: [[[Infinity, 0], [1, 1]]] }));
  assert.throws(() => parseFeedback({ ...input, notes: "x".repeat(5001) }));
  const saved = await saveFeedback(file, "benchmark", 27, parseFeedback(input), "reviewer");
  assert.equal(saved.revision, 1);
  assert.equal(saved.updated_by, "reviewer");
  assert.deepEqual((await readFeedback(file))["benchmark:27"].rejected_cuts, [cut]);
  await assert.rejects(saveFeedback(file, "benchmark", 27, parseFeedback(input), "stale-reviewer"), e => e.status === 409);
  const restored = await saveFeedback(file, "benchmark", 27, parseFeedback({ ...input, rejected_cuts: [], revision: 1 }), "reviewer");
  assert.deepEqual(restored.rejected_cuts, []);
  assert.equal(restored.history.length, 1);
  assert.deepEqual(restored.history[0].rejected_cuts, [cut]);
  await saveFeedback(file, "another", 27, parseFeedback(input), "other");
  assert.equal(feedbackFor(await readFeedback(file), "benchmark", 27).revision, 2);
  assert.equal(feedbackFor(await readFeedback(file), "another", 27).revision, 1);
  assert.equal(feedbackFor({ 27: input }, "suburban_benchmark", 27), input);
  assert.equal(feedbackFor({ 27: input }, "another", 27), undefined);
  const approved = parseFeedback({ ...input, status: "approved", issues: [], rejected_cuts: [] });
  assert.equal(approved.status, "approved");
  await writeFile(file, "invalid-json");
  await assert.rejects(saveFeedback(file, "benchmark", 27, input, "reviewer"), e => e.status === 503);
  assert.equal(await readFile(file, "utf8"), "invalid-json");
  console.log("Review storage: validation, cuts round-trip, restore, revisions, cohort isolation, approval gate and corruption preservation passed.");
} finally { await rm(dir, { recursive: true, force: true }); }
