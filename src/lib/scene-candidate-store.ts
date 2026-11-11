import { readFile, readdir, stat } from "node:fs/promises";
import { createHash } from "node:crypto";
import path from "node:path";
import { ReviewError } from "./review-feedback";
import { loadScene, saveScene, scenePath, validateSceneReview, objectScope } from "./scene-store";
import { SCENE_CLASSES, type SceneModelSnapshot, type SceneObject } from "./scene-types";
import { candidateDraft, candidateDraftId, candidateMatch, candidateReviewURL, sameCandidateFrame, type CandidateScene, type CandidateSummary } from "./scene-candidates";
import { candidateChecks, readCandidateCheck } from "./scene-candidate-checks";

const root = path.join(process.cwd(), "data/training/scene-proposals");
const version = (id: string) => {
  if (typeof id !== "string" || !/^[a-f0-9]{16}$/.test(id)) throw new ReviewError("Choose a valid model candidate.");
  return id;
};
const hash = (data: string | Buffer) => createHash("sha256").update(data).digest("hex");
async function json(file: string) { return JSON.parse(await readFile(file, "utf8")); }

async function candidate(id: string): Promise<CandidateSummary> {
  const directory = path.join(root, version(id));
  const [modelText, report, info] = await Promise.all([readFile(path.join(directory, "model.json"), "utf8"), json(path.join(directory, "report.json")), stat(path.join(directory, "model.json"))]);
  const model = JSON.parse(modelText);
  const p = model.provenance;
  if (model.model_kind !== "regularized-logistic-proposal-quality" || report.candidate_id !== id ||
    report.status !== "experimental-candidate" || JSON.stringify(p) !== JSON.stringify(report.provenance) ||
    !SCENE_CLASSES.includes(p.kind) || !Array.isArray(report.metrics?.layers) || !Number.isFinite(model.threshold))
    throw new ReviewError("The candidate report could not be verified.", 503);
  version(p.dataset_version); version(p.experiment_run);
  const counts = (value: unknown, keys: string[]) => typeof value === "object" && value !== null && keys.every(k => {
    const n = (value as Record<string, unknown>)[k]; return typeof n === "number" && Number.isSafeInteger(n) && n >= 0;
  });
  if (typeof p.prompt !== "string" || model.threshold < 0 || model.threshold > 1 ||
    !counts(report.training_proposals, ["usable", "negative", "images", "groups"]) ||
    ["train", "validation"].some(s => !counts(report.metrics.splits?.[s], ["images", "groups", "references", "matched", "extra", "missed"]) ||
      !counts(report.baseline?.splits?.[s], ["images", "groups", "references", "matched", "extra", "missed"])) ||
    !Array.isArray(report.baseline?.layers)) throw new ReviewError("The candidate evaluation is incomplete.", 503);
  const scenes = report.metrics.layers.map((l: { scene_id: string; split: string; predictions: number; matched: number; missed: number; extra: number }) => {
    scenePath(l.scene_id);
    const baseline = report.baseline.layers.find((b: { scene_id: string }) => b.scene_id === l.scene_id);
    if (!["train", "validation"].includes(l.split) || !baseline || baseline.split !== l.split ||
      !counts(l, ["predictions", "matched", "missed", "extra"]) ||
      !Number.isSafeInteger(baseline.matched) || !Number.isSafeInteger(baseline.predictions) || baseline.predictions < l.predictions)
      throw new ReviewError("The candidate split or counts could not be verified.", 503);
    return { id: l.scene_id, split: l.split, retained: l.predictions, filtered: baseline.predictions - l.predictions,
      matched: l.matched, missed: l.missed, extra: l.extra, lost_matches: Math.max(0, baseline.matched - l.matched) };
  });
  let supplemental: CandidateSummary["supplemental_training"];
  if (p.supplemental_dataset_version !== undefined) {
    version(p.supplemental_dataset_version);
    if (!counts(report.supplemental_training, ["usable", "negative", "images"]) ||
        report.supplemental_training.dataset_version !== p.supplemental_dataset_version)
      throw new ReviewError("The additional training feedback could not be verified.", 503);
    supplemental = { dataset_version: p.supplemental_dataset_version, images: report.supplemental_training.images,
      usable: report.supplemental_training.usable, negative: report.supplemental_training.negative };
  }
  return { id, created_at: info.mtime.toISOString(), kind: p.kind, prompt: p.prompt, dataset_version: p.dataset_version,
    ...await candidateChecks(directory, { id, modelHash: hash(modelText), kind: p.kind, prompt: p.prompt, scenes }),
    supplemental_training: supplemental,
    experiment_run: p.experiment_run, threshold: model.threshold, training_proposals: report.training_proposals,
    before: report.baseline.splits, after: report.metrics.splits, scenes };
}

export async function candidateList() {
  let entries;
  try { entries = await readdir(root, { withFileTypes: true }); }
  catch (e) { if ((e as NodeJS.ErrnoException).code === "ENOENT") return { candidates: [], unavailable: 0 }; throw e; }
  const results = await Promise.allSettled(entries.filter(e => e.isDirectory() && /^[a-f0-9]{16}$/.test(e.name)).map(e => candidate(e.name)));
  return { candidates: results.flatMap(r => r.status === "fulfilled" ? [r.value] : []).sort((a, b) => b.created_at.localeCompare(a.created_at)),
    unavailable: results.filter(r => r.status === "rejected").length };
}

async function checkedScene(candidateId: string, sceneId: string, checkId?: string) {
  if (checkId !== undefined && (typeof checkId !== "string" || !/^[a-f0-9]{16}$/.test(checkId))) throw new ReviewError("Choose a valid validation check.");
  const summary = await candidate(candidateId), directory = scenePath(sceneId);
  const check = checkId ? await readCandidateCheck(path.join(root, candidateId), checkId, { ...summary, modelHash: hash(await readFile(path.join(root, candidateId, "model.json"))) }) : undefined;
  const layer = (check?.summary.scenes || summary.scenes).find(s => s.id === sceneId);
  if (!layer) throw new ReviewError("This image is not part of the candidate evaluation.", 404);
  const datasetVersion = check?.summary.dataset_version || summary.dataset_version;
  const dataset = await readFile(path.join(process.cwd(), "data/training/scenes", datasetVersion, "dataset.jsonl"), "utf8");
  if (hash(dataset).slice(0, 16) !== datasetVersion) throw new ReviewError("The frozen training dataset changed.", 409);
  if (summary.supplemental_training) {
    const extra = await readFile(path.join(process.cwd(), "data/training/scenes", summary.supplemental_training.dataset_version, "dataset.jsonl"), "utf8");
    if (hash(extra).slice(0, 16) !== summary.supplemental_training.dataset_version)
      throw new ReviewError("The frozen additional feedback changed.", 409);
  }
  const row = dataset.trim().split("\n").map(l => JSON.parse(l)).find(r => r.scene_id === sceneId);
  const savedText = await readFile(check ? path.join(root, candidateId, "checks", checkId!, `${sceneId}.json`) : path.join(process.cwd(), "data/experiments/scenes", summary.experiment_run, `${sceneId}.json`), "utf8");
  if (check && hash(savedText) !== check.sceneFiles[sceneId]) throw new ReviewError("The later validation predictions changed.", 409);
  const saved = JSON.parse(savedText);
  const result = saved.result;
  if (!row || (check ? saved.check_id !== checkId : saved.run_id !== summary.experiment_run) || row.split !== layer.split ||
    ["scene_id", "split", "spatial_group", "image_sha256", "model_signature"].some(k => row[k] !== result[k]) || row.review.revision !== result.revision ||
    !/^[a-f0-9]{64}$/.test(row.model_signature) || !/^[a-f0-9]{64}$/.test(row.image_sha256))
    throw new ReviewError("The candidate image provenance could not be verified.", 409);
  const [snapshot, data, image, scores] = await Promise.all([
    json(path.join(directory, "models", `${row.model_signature}.json`)) as Promise<SceneModelSnapshot>, loadScene(sceneId),
    readFile(path.join(directory, `${row.image_sha256}.png`)), check ? { candidate_id: candidateId, scene_id: sceneId, image_sha256: result.image_sha256, scores: saved.scores } : json(path.join(root, candidateId, `${sceneId}.json`)),
  ]);
  if (hash(image) !== row.image_sha256 || snapshot.image_sha256 !== row.image_sha256 || snapshot.model_signature !== row.model_signature ||
    JSON.stringify(snapshot.pixel_to_meters) !== JSON.stringify(row.pixel_to_meters) || snapshot.id !== sceneId ||
    scores.candidate_id !== candidateId || scores.scene_id !== sceneId || scores.image_sha256 !== row.image_sha256)
    throw new ReviewError("The candidate image or coordinate frame changed.", 409);
  const predictions: SceneObject[] = result.prompts?.[summary.prompt]?.predictions;
  if (!Array.isArray(predictions) || !Array.isArray(scores.scores) || scores.scores.length !== predictions.length ||
    predictions.length !== layer.retained + layer.filtered || new Set(scores.scores.map((s: { id: string }) => s.id)).size !== predictions.length ||
    scores.scores.filter((s: { retained: boolean }) => s.retained).length !== layer.retained)
    throw new ReviewError("The candidate proposals are incomplete.", 503);
  validateSceneReview({ revision: 0, model_signature: snapshot.model_signature, objects: predictions, groups: [], reviewed_classes: [] }, snapshot);
  const compatible = sameCandidateFrame(snapshot, data.model), current = data.stale ? data.model.objects : data.review?.objects || data.model.objects;
  const proposals = predictions.map(object => {
    const score = scores.scores.find((s: { id: string }) => s.id === object.id);
    if (object.kind !== summary.kind || !score || typeof score.retained !== "boolean" || !Number.isFinite(score.quality_score) || score.quality_score < 0 || score.quality_score > 1 ||
      Math.abs(score.quality_score - summary.threshold) > .000001 && score.retained !== (score.quality_score >= summary.threshold))
      throw new ReviewError("The candidate scores could not be verified.", 503);
    const match = result.prompts[summary.prompt].metrics?.matches?.find((m: { prediction_id: string }) => m.prediction_id === object.id);
    const reference = match && row.reference?.find((o: SceneObject) => o.id === match.reference_id && o.kind === object.kind);
    const label = reference && row.review.objects.find((o: SceneObject) => o.id === reference.id)?.label;
    return { object, quality_score: score.quality_score, retained: score.retained,
      frozen_match: reference && Number.isFinite(match.iou) && match.iou >= .5 && match.iou <= 1 ? { id: reference.id, label: label || "Reviewed outline", iou: match.iou } : null,
      match: compatible && !data.stale ? candidateMatch(object, current, snapshot.pixel_to_meters) : null };
  });
  const scene: CandidateScene = { candidate_id: candidateId, check_id: checkId, id: sceneId, title: snapshot.title, split: layer.split,
    width: snapshot.image_width, height: snapshot.image_height, image_sha256: snapshot.image_sha256,
    image_url: `/api/scenes/image?id=${sceneId}&version=${snapshot.image_sha256}`, compatible, stale: data.stale,
    revision: data.review?.revision || 0, model_signature: data.model.model_signature, proposals,
    annotations: compatible && !data.stale ? current.filter(o => o.kind === summary.kind && !["rejected", "superseded"].includes(o.status)) : [] };
  return { scene, data };
}
export async function candidateScene(candidateId: string, sceneId: string, checkId?: string) { return (await checkedScene(candidateId, sceneId, checkId)).scene; }

export async function importCandidate(body: { candidate: string; check?: string; scene: string; object: string; revision: number; model_signature: string }, principal: { principal: string; scopes: string[] }) {
  const { scene, data } = await checkedScene(body.candidate, body.scene, body.check);
  if (!scene.compatible || scene.stale) throw new ReviewError("Open scene review to reconcile the changed image or model first.", 409);
  const proposal = scene.proposals.find(p => p.object.id === body.object);
  if (!proposal) throw new ReviewError("Choose a candidate outline.");
  if (!principal.scopes.includes(objectScope(proposal.object))) throw new ReviewError("Editing access is required to add this draft.", 403);
  const current = data.review?.objects || data.model.objects;
  // Repeated submissions return the original draft, including its later edits or replacement.
  const imported = current.find(o => o.id === candidateDraftId(body.candidate, body.object));
  if (imported || proposal.match) {
    let id = imported?.id || proposal.match!.id;
    const seen = new Set<string>();
    while (!seen.has(id)) {
      seen.add(id);
      const object = current.find(o => o.id === id);
      if (object?.status !== "superseded" || !object.replacement_ids?.length) break;
      id = object.replacement_ids[0];
    }
    return { review_url: candidateReviewURL(body.scene, proposal.object.kind, id), created: false };
  }
  if (body.revision !== scene.revision || body.model_signature !== scene.model_signature)
    throw new ReviewError("This scene changed. Refresh the candidate before adding a draft.", 409);
  const next = candidateDraft(data, proposal.object, body.candidate, body.check);
  await saveScene(body.scene, next, principal);
  return { review_url: candidateReviewURL(body.scene, proposal.object.kind, candidateDraftId(body.candidate, body.object)), created: true };
}
