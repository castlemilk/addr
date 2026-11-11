import { readFile, readdir, stat } from "node:fs/promises";
import { createHash } from "node:crypto";
import { isDeepStrictEqual } from "node:util";
import path from "node:path";
import { TRAINING_SPLITS } from "./scene-training";
import { ROOF_FAMILIES, type RoofTrainingRun, type RoofTrainingRuns, type RoofRunScore } from "./scene-roof-runs";

const hash = (bytes: string | Buffer) => createHash("sha256").update(bytes).digest("hex");
const version = /^[a-f0-9]{16}$/, digest = /^[a-f0-9]{64}$/;
const count = (n: unknown): n is number => typeof n === "number" && Number.isSafeInteger(n) && n >= 0;
const score = (s: RoofRunScore | undefined) => s && count(s.ends) && s.ends > 0 && Number.isFinite(s.log_loss) && s.log_loss >= 0 &&
  Number.isFinite(s.balanced_accuracy) && s.balanced_accuracy >= 0 && s.balanced_accuracy <= 1;
const invalid = () => { throw new Error("The roof training artifacts could not be verified."); };

async function readRun(root: string, id: string): Promise<RoofTrainingRun> {
  const directory = path.join(root, "scene-roofs", id);
  const artifacts: Record<string, string> = JSON.parse(await readFile(path.join(directory, "artifacts.json"), "utf8"));
  const files = Object.keys(artifacts).sort();
  const candidate = files.includes("roof-form.json");
  const expected = ["config.json", "features.json", "training-report.json", ...(candidate ? ["roof-form.json"] : [])].sort();
  if (!isDeepStrictEqual(files, expected)) invalid();
  const contents = await Promise.all(files.map(async name => {
    const text = await readFile(path.join(directory, name), "utf8");
    if (!digest.test(artifacts[name]) || hash(text) !== artifacts[name]) invalid();
    return [name, JSON.parse(text)] as const;
  }));
  const values = Object.fromEntries(contents), config = values["config.json"], report = values["training-report.json"];
  if (report.schema_version !== 1 || config.schema_version !== 1 || report.run_id !== id ||
      report.model_kind !== "regularized-logistic-roof-end-classifier" || !version.test(report.dataset_version) ||
      report.dataset_version !== config.dataset_version || !digest.test(config.dataset_sha256) ||
      !isDeepStrictEqual(report.requirements, config.requirements) ||
      report.status !== (candidate ? "experimental-candidate" : "needs-reviewed-labels") ||
      !["not-run", "incomplete-visible-evidence", "frozen-weights-report-only"].includes(report.test_evaluation) ||
      !Array.isArray(report.review_links) || !Array.isArray(report.feature_exclusions)) invalid();
  // Link every result to the immutable export, rather than today's mutable review.
  const dataset = await readFile(path.join(root, "scenes", report.dataset_version, "dataset.jsonl"));
  if (hash(dataset) !== config.dataset_sha256 || config.dataset_sha256.slice(0, 16) !== report.dataset_version) invalid();
  for (const split of TRAINING_SPLITS) {
    const requirement = report.requirements?.[split];
    if (!count(requirement?.roofs_per_class) || requirement.roofs_per_class === 0 || !count(requirement?.areas_per_class) || requirement.areas_per_class === 0) invalid();
    for (const family of ROOF_FAMILIES) {
      const reviewed = report.reviewed_roofs?.[split]?.[family], usable = report.usable_roofs?.[split]?.[family];
      for (const c of [reviewed, usable]) {
        if (!c || !count(c.roofs) || !count(c.images) || !count(c.areas) || c.images > c.roofs || c.areas > c.images) invalid();
      }
      if (usable.roofs > reviewed.roofs || usable.images > reviewed.images || usable.areas > reviewed.areas) invalid();
    }
  }
  if (candidate) {
    const model = values["roof-form.json"];
    if (!score(report.validation) || !score(report.validation_baseline) ||
        report.validation.ends !== report.validation_baseline.ends || !digest.test(report.selection_weights_sha256) ||
        !isDeepStrictEqual(model.validation, report.validation) ||
        !isDeepStrictEqual(model.provenance, { run_id: id, ...config }) ||
        model.validated !== report.promotion_eligible || report.test_evaluation === "not-run") invalid();
    if (report.test_evaluation === "frozen-weights-report-only") {
      if (!score(report.test) || !isDeepStrictEqual(model.test, report.test)) invalid();
    } else if (report.test !== undefined || model.test !== undefined || report.promotion_eligible !== false) invalid();
  } else if (report.validation !== undefined || report.test !== undefined || report.test_evaluation !== "not-run") invalid();
  const links = new Map<string, { scene_id: string; object_id: string; family: string; split: "train" | "validation" | "test" }>();
  for (const link of report.review_links) {
    if (typeof link.scene_id !== "string" || !/^[a-zA-Z0-9_-]{1,160}$/.test(link.scene_id) ||
        typeof link.object_id !== "string" || !link.object_id || link.object_id.length > 200 ||
        !TRAINING_SPLITS.includes(link.split) || !ROOF_FAMILIES.includes(link.family)) invalid();
    const key = `${link.scene_id}:${link.object_id}`;
    if (links.has(key)) invalid();
    links.set(key, link);
  }
  const exclusions: RoofTrainingRun["exclusions"] = [];
  let heldOut = 0;
  for (const excluded of report.feature_exclusions) {
    const link = links.get(`${excluded.scene_id}:${excluded.object_id}`);
    if (!link || !["prior_geometry_failed", "unsupported_geometry", "insufficient_visible_evidence"].includes(excluded.reason)) invalid();
    if (link!.split === "test") { heldOut++; continue; }
    // Construct internal links ourselves. Stored URLs are not navigation authority.
    exclusions.push({ ...link!, split: link!.split as "train" | "validation", reason: excluded.reason,
      url: `/scenes?${new URLSearchParams({ scene: link!.scene_id, object: link!.object_id, kind: "building", status: "all" })}` });
  }
  return { id, created_at: (await stat(path.join(directory, "training-report.json"))).mtime.toISOString(),
    dataset_version: report.dataset_version, status: report.status, requirements: report.requirements,
    reviewed: report.reviewed_roofs, usable: report.usable_roofs, test_evaluation: report.test_evaluation,
    ...(candidate ? { validation: report.validation, baseline: report.validation_baseline, ...(report.test ? { test: report.test } : {}) } : {}),
    exclusions, held_out_exclusions: heldOut };
}

export async function roofTrainingRuns(root = path.join(process.cwd(), "data/training")): Promise<RoofTrainingRuns> {
  let entries;
  try { entries = await readdir(path.join(root, "scene-roofs"), { withFileTypes: true }); }
  catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") return { runs: [], unavailable: 0 }; throw error; }
  const results = await Promise.allSettled(entries.filter(e => e.isDirectory() && version.test(e.name)).map(e => readRun(root, e.name)));
  return { runs: results.flatMap(r => r.status === "fulfilled" ? [r.value] : []).sort((a, b) => b.created_at.localeCompare(a.created_at) || a.id.localeCompare(b.id)),
    unavailable: results.filter(r => r.status === "rejected").length };
}
