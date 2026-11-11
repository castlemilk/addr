import { readFile, readdir, stat } from "node:fs/promises";
import { createHash } from "node:crypto";
import path from "node:path";
import { ReviewError } from "./review-feedback";
import type { CandidateCheck, CandidateLayer } from "./scene-candidates";

type Expected = { id: string; modelHash: string; kind: string; prompt: string; scenes: CandidateLayer[] };
const hash = (text: string) => createHash("sha256").update(text).digest("hex");
const validId = (value: unknown) => typeof value === "string" && /^[a-f0-9]{16}$/.test(value);
const counts = (value: Record<string, number>, aggregate = false) => value &&
  ["predictions", "references", "matched", "extra", "missed", ...(aggregate ? ["images", "groups"] : [])]
    .every(k => Number.isSafeInteger(value[k]) && value[k] >= 0) &&
  value.matched + value.missed === value.references && value.matched + value.extra === value.predictions;

export async function readCandidateCheck(directory: string, id: string, expected: Expected) {
  if (!validId(id)) throw new ReviewError("Choose a valid validation check.");
  const folder = path.join(directory, "checks", id);
  const [encoded, reportText, info] = await Promise.all([
    readFile(path.join(folder, "config.json"), "utf8"), readFile(path.join(folder, "report.json"), "utf8"), stat(path.join(folder, "report.json")),
  ]);
  const config = JSON.parse(encoded), report = JSON.parse(reportText);
  const invalid = () => new ReviewError("The later validation check could not be verified.", 409);
  if (hash(encoded).slice(0, 16) !== id || config.schema_version !== 1 || config.candidate_id !== expected.id ||
    config.candidate_model_sha256 !== expected.modelHash || config.kind !== expected.kind || config.prompt !== expected.prompt ||
    !validId(config.dataset_version) || report.check_id !== id || report.candidate_id !== expected.id ||
    !Array.isArray(config.layers) || !config.layers.length || !Array.isArray(report.layers) || report.layers.length !== config.layers.length ||
    !counts(report.before, true) || !counts(report.after, true)) throw invalid();
  const seen = new Set<string>();
  const scenes: CandidateLayer[] = await Promise.all(report.layers.map(async (row: { before: Record<string, number> & { scene_id: string; split: string; spatial_group: string }; after: Record<string, number> & { scene_id: string; split: string; spatial_group: string } }) => {
    const a = row.before, b = row.after, source = config.layers.find((l: { scene_id: string }) => l.scene_id === a?.scene_id);
    if (!source || !/^[a-zA-Z0-9_-]{1,100}$/.test(source.scene_id) || seen.has(source.scene_id) ||
      expected.scenes.some(s => s.id === source.scene_id) || source.split !== "validation" || a.split !== source.split || b?.split !== source.split ||
      b.scene_id !== source.scene_id || a.spatial_group !== source.spatial_group || b.spatial_group !== source.spatial_group || !source.spatial_group ||
      !/^[a-f0-9]{64}$/.test(source.image_sha256) || !/^[a-f0-9]{64}$/.test(source.model_signature) ||
      !Number.isSafeInteger(source.revision) || source.revision < 1 || !/^[a-f0-9]{64}$/.test(report.scene_files?.[source.scene_id]) ||
      !counts(a) || !counts(b) || a.references !== b.references || b.predictions > a.predictions) throw invalid();
    seen.add(source.scene_id);
    const artifactText = await readFile(path.join(folder, `${source.scene_id}.json`), "utf8");
    if (hash(artifactText) !== report.scene_files[source.scene_id]) throw invalid();
    const artifact = JSON.parse(artifactText), result = artifact.result, original = result?.prompts?.[expected.prompt]?.metrics;
    if (artifact.check_id !== id || !result || ["scene_id", "split", "spatial_group", "revision", "image_sha256", "model_signature"].some(k => result[k] !== source[k]) ||
      !counts(original) || !counts(artifact.filtered) || ["predictions", "references", "matched", "extra", "missed"].some(k => a[k] !== original[k] || b[k] !== artifact.filtered[k])) throw invalid();
    return { id: source.scene_id, split: "validation" as const, retained: b.predictions, filtered: a.predictions - b.predictions,
      matched: b.matched, missed: b.missed, extra: b.extra, lost_matches: Math.max(0, a.matched - b.matched) };
  }));
  for (const side of ["before", "after"] as const) {
    const layers = report.layers.map((l: Record<string, Record<string, number>>) => l[side]);
    if (report[side].images !== scenes.length || report[side].groups !== new Set(config.layers.map((l: { spatial_group: string }) => l.spatial_group)).size ||
      ["predictions", "references", "matched", "extra", "missed"].some(k => report[side][k] !== layers.reduce((n: number, l: Record<string, number>) => n + l[k], 0))) throw invalid();
  }
  const summary: CandidateCheck = { id, created_at: info.mtime.toISOString(), dataset_version: config.dataset_version,
    before: report.before, after: report.after, scenes };
  return { summary, config, sceneFiles: report.scene_files as Record<string, string> };
}

export async function candidateChecks(directory: string, expected: Expected) {
  let entries: string[];
  try { entries = await readdir(path.join(directory, "checks")); }
  catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") return { checks: [], unavailable_checks: 0 }; throw error; }
  const results = await Promise.allSettled(entries.filter(validId).map(async id => {
    // A report publishes the check; an in-progress extraction has only its config.
    try { await stat(path.join(directory, "checks", id, "report.json")); }
    catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") return null; throw error; }
    return (await readCandidateCheck(directory, id, expected)).summary;
  }));
  return { checks: results.flatMap(r => r.status === "fulfilled" && r.value ? [r.value] : []).sort((a, b) => b.created_at.localeCompare(a.created_at)),
    unavailable_checks: results.filter(r => r.status === "rejected").length };
}
