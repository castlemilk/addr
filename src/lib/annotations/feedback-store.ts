import { lstat, mkdir, open, readdir, readFile, rename, rm } from "node:fs/promises";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { ReviewError } from "../review-feedback";
import { basemapMode, loadScene, objectScope, scenePath, sceneReviewTransaction, validateSceneReview } from "../scene-store";
import type { SceneData, SceneReview, SceneModelSnapshot } from "../scene-types";
import {
  chainSnapshot, feedbackContentHash, feedbackDocument, feedbackHash, localFeedbackHash, prepareFeedbackScene, reviewContent, sameModelFrame, validateFeedbackBundle,
  type FeedbackBundle, type FeedbackEntry, type FeedbackReceipt, type FeedbackScenePlan, type PreparedFeedbackScene,
} from "./feedback";

type Principal = { principal: string; scopes: string[] };
interface FeedbackPlan {
  schemaVersion: 1; planId: string; bundleDigest: string; canApply: boolean;
  scenes: FeedbackScenePlan[]; quarantined: FeedbackBundle["quarantined"];
  counts: { scenes: number; objects: number; drafts: number; notesOnly: number; receiptOnly: number; conflicts: number };
}
/** `chained`: an entry prepared against the review an earlier entry of the same scene commits first; its localHash binds
 *  that review's content (reviewContent) rather than the raw file, whose timestamps and history are only known after the commit. */
interface RunScene {
  sceneId: string; itemId: string; localHash: string; receiptHash: string; review: SceneReview;
  shouldSave: boolean; summary: FeedbackScenePlan; chained?: true;
}
type PreparedEntry = PreparedFeedbackScene & { chained?: true };
interface FeedbackRun { schemaVersion: 1; bundleDigest: string; planId: string; plan: FeedbackPlan; scenes: RunScene[] }
interface SceneJournal {
  schemaVersion: 1; bundleDigest: string; planId: string; sceneId: string; itemId: string;
  localHash: string; receiptHash: string; expected: SceneReview; receipt: FeedbackReceipt;
}
interface FeedbackStoreOptions { afterSceneSave?: (sceneId: string) => Promise<void> }
const root = () => path.join(process.cwd(), "data/reviews/fieldwork-feedback");
const directory = (bundle: FeedbackBundle) => path.join(root(), feedbackHash(bundle.origin), bundle.projectId);
const receiptFile = (bundle: FeedbackBundle, itemId: string) => path.join(directory(bundle), "receipts", `${feedbackHash(itemId)}.json`);
const runDirectory = (bundle: FeedbackBundle, planId: string) => path.join(directory(bundle), "runs", planId);
const sceneJournalFile = (bundle: FeedbackBundle, planId: string, itemId: string) => path.join(runDirectory(bundle, planId), `${feedbackHash(itemId)}.json`);
const missing = (error: unknown) => (error as NodeJS.ErrnoException)?.code === "ENOENT";
const validatedReview = (review: SceneReview, model: SceneModelSnapshot) => ({ ...validateSceneReview(review, model), basemap_mode: basemapMode(review) });
const byItemId = (a: { itemId: string }, b: { itemId: string }) => a.itemId < b.itemId ? -1 : a.itemId > b.itemId ? 1 : 0;
const chainedHash = (data: SceneData) => feedbackContentHash({ chained: 1, model: data.model, review: data.review ? reviewContent(data.review) : null });
const stateHash = (data: SceneData, chained?: true) => chained ? chainedHash(data) : localFeedbackHash(data);
function planFingerprint(bundleDigest: string, scenes: RunScene[]) {
  return feedbackContentHash({ schemaVersion: 1, bundleDigest, scenes: scenes.map(s => ({ itemId: s.itemId, localHash: s.localHash, receiptHash: s.receiptHash, review: reviewContent(s.review), summary: s.summary, shouldSave: s.shouldSave, ...(s.chained ? { chained: true } : {}) })) });
}
/** The scene's models/<sig>.json snapshots that can serve as old-frame recovery hops. A missing directory means no hops; an
 *  unreadable or mismatched file is simply not a hop (the chain then fails closed as frame_changed_unrecoverable). */
async function chainSnapshots(sceneId: string): Promise<SceneModelSnapshot[]> {
  const folder = path.join(scenePath(sceneId), "models");
  let names: string[];
  try { names = await readdir(folder); }
  catch (error) { if (missing(error)) return []; throw new ReviewError("The scene's earlier model snapshots could not be read. They have been preserved.", 503); }
  const snapshots: SceneModelSnapshot[] = [];
  for (const name of names.sort()) {
    const match = /^([a-f0-9]{64})\.json$/.exec(name);
    if (!match) continue;
    let parsed: unknown;
    try { parsed = JSON.parse(await readFile(path.join(folder, name), "utf8")); } catch { continue; }
    const snapshot = chainSnapshot(parsed, sceneId, match[1]);
    if (snapshot) snapshots.push(snapshot);
  }
  return snapshots;
}
async function readJSON<T>(file: string): Promise<T | undefined> {
  try { return JSON.parse(await readFile(file, "utf8")) as T; }
  catch (error) { if (missing(error)) return undefined; throw new ReviewError("A local feedback receipt could not be verified. It has been preserved.", 503); }
}
async function atomicJSON(file: string, value: unknown) {
  await mkdir(path.dirname(file), { recursive: true });
  const temp = `${file}.${randomUUID()}.tmp`;
  try {
    const handle = await open(temp, "wx", 0o600);
    try { await handle.writeFile(JSON.stringify(value)); await handle.sync(); } finally { await handle.close(); }
    await rename(temp, file);
    const parent = await open(path.dirname(file), "r");
    try { await parent.sync(); } finally { await parent.close(); }
  } finally { await rm(temp, { force: true }); }
}
function requireRead(principal: Principal) {
  if (!principal.scopes.includes("crm.read")) throw new ReviewError("Local CRM reading access is required to inspect feedback.", 403);
}
async function checkedImage(data: SceneData) {
  if (!/^[a-f0-9]{64}$/.test(data.model.image_sha256)) throw new ReviewError("The local scene image identity is invalid.", 409);
  let bytes: Buffer;
  try { bytes = await readFile(path.join(scenePath(data.model.id), `${data.model.image_sha256}.png`)); }
  catch { throw new ReviewError("The bound local scene image is missing. Restore the original image before applying feedback.", 409); }
  const { createHash } = await import("node:crypto");
  if (createHash("sha256").update(bytes).digest("hex") !== data.model.image_sha256) throw new ReviewError("The local image bytes do not match the imported image.", 409);
}
const sourceModel = (entry: FeedbackEntry) => feedbackDocument<SceneModelSnapshot>(entry.model, "Source model");
function unavailable(bundle: FeedbackBundle, entry: FeedbackEntry, error: unknown): PreparedEntry {
  const fallback: SceneData = { model: sourceModel(entry), review: null, stale: false, image_url: "" };
  const failed: PreparedEntry = prepareFeedbackScene(bundle, entry, fallback);
  failed.summary.status = "conflict";
  failed.summary.conflicts.push(error instanceof ReviewError ? error.message : "The local scene could not be read. Restore it before reconciling feedback.");
  failed.localHash = "unavailable"; failed.receiptHash = "unavailable"; failed.shouldSave = false;
  return failed;
}
async function prepare(bundle: FeedbackBundle) {
  const scenes: PreparedEntry[] = [];
  const groups = new Map<string, FeedbackEntry[]>();
  for (const entry of [...bundle.entries].sort(byItemId)) groups.set(entry.sceneId, [...(groups.get(entry.sceneId) || []), entry]);
  for (const [sceneId, entries] of [...groups].sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)) {
    let data: SceneData;
    try { data = await loadScene(sceneId); }
    catch (error) { for (const entry of entries) scenes.push(unavailable(bundle, entry, error)); continue; }
    // Several live items of one scene (its generation item and the marked frozen item) are applied in sequence: entries on
    // the current frame first, then superseded frames through recovery (ties by item id); each later entry is prepared
    // against the review its predecessor commits and each produces its own receipt.
    const onOldFrame = (entry: FeedbackEntry) => !sameModelFrame(sourceModel(entry), data.model);
    const ordered = [...entries].sort((a, b) => Number(onOldFrame(a)) - Number(onOldFrame(b)) || byItemId(a, b));
    let state = data, chained: true | undefined;
    for (const entry of ordered) {
      let receipt: FeedbackReceipt | undefined, snapshots: SceneModelSnapshot[] = [];
      try {
        receipt = await readJSON<FeedbackReceipt>(receiptFile(bundle, entry.itemId));
        if (onOldFrame(entry)) snapshots = await chainSnapshots(sceneId);
      } catch (error) { scenes.push(unavailable(bundle, entry, error)); continue; }
      const prepared: PreparedEntry = prepareFeedbackScene(bundle, entry, state, receipt, snapshots);
      if (chained) { prepared.chained = chained; prepared.localHash = chainedHash(state); }
      if (prepared.summary.status !== "conflict") {
        try {
          await checkedImage(data);
          // An old-frame recovery binds to a superseded model by design; prepareFeedbackScene verified the frame relationship.
          if (!prepared.summary.recoveredFrom && feedbackContentHash(data.model) !== feedbackContentHash(sourceModel(entry)))
            throw new ReviewError("The local source model changed; recover it explicitly before retrying feedback.", 409);
          prepared.review = validatedReview(prepared.review, data.model);
        } catch (error) {
          if (!(error instanceof ReviewError)) throw error;
          prepared.summary.status = "conflict"; prepared.summary.conflicts.push(error.message);
        }
      }
      scenes.push(prepared);
      if (prepared.summary.status !== "conflict" && prepared.shouldSave) { state = { ...state, review: { ...prepared.review, revision: prepared.review.revision + 1 } }; chained = true; }
    }
  }
  const changes = scenes.flatMap(s => s.summary.changes), conflicts = scenes.reduce((total, s) => total + s.summary.conflicts.length, 0) + bundle.quarantined.length;
  const planId = planFingerprint(bundle.digest, scenes.map(s => ({ itemId: s.item.id, sceneId: s.summary.sceneId, localHash: s.localHash, receiptHash: s.receiptHash, review: s.review, summary: s.summary, shouldSave: s.shouldSave, chained: s.chained })));
  const plan: FeedbackPlan = { schemaVersion: 1, planId, bundleDigest: bundle.digest, canApply: conflicts === 0,
    scenes: scenes.map(s => s.summary), quarantined: bundle.quarantined,
    counts: { scenes: scenes.length, objects: changes.filter(c => c.objectId).length, drafts: changes.filter(c => c.substantive).length,
      notesOnly: changes.filter(c => c.objectId && !c.substantive).length, receiptOnly: changes.filter(c => c.receiptOnly).length, conflicts } };
  return { plan, scenes };
}
export async function previewFeedback(value: unknown, principal: Principal): Promise<FeedbackPlan> {
  requireRead(principal);
  return (await prepare(validateFeedbackBundle(value))).plan;
}
function storedRunMatches(run: FeedbackRun, bundle: FeedbackBundle, planId: string) {
  return run.schemaVersion === 1 && run.planId === planId && run.bundleDigest === bundle.digest && run.plan.planId === planId && planFingerprint(bundle.digest, run.scenes) === planId &&
    run.scenes.length === bundle.entries.length && run.scenes.every(s => bundle.entries.some(e => e.itemId === s.itemId && e.sceneId === s.sceneId));
}
function matchingSavedReview(data: SceneData, expected: SceneReview): SceneReview | undefined {
  const reviews = data.review ? [data.review, ...(data.review.history || [])] : [];
  return reviews.find(r => feedbackContentHash(reviewContent(r)) === feedbackContentHash(reviewContent(expected)));
}
function requireWrite(principal: Principal, scene: RunScene) {
  for (const change of scene.summary.changes) {
    if (!change.objectId) continue;
    const object = scene.review.objects.find(o => o.id === change.objectId);
    if (object && !principal.scopes.includes(objectScope(object))) throw new ReviewError(`Applying this feedback requires ${objectScope(object)} access.`, 403);
  }
}
// Called under the first stale scene's lock. An ordered retry cannot reach the
// suffix; any journal entry or filesystem uncertainty keeps the original plan.
async function journalsAbsent(bundle: FeedbackBundle, planId: string, scenes: RunScene[]) {
  for (const scene of scenes) {
    try { await lstat(sceneJournalFile(bundle, planId, scene.itemId)); return false; }
    catch (error) { if (!missing(error)) return false; }
  }
  return true;
}
export async function applyFeedback(value: unknown, planId: string, principal: Principal, options: FeedbackStoreOptions = {}) {
  requireRead(principal);
  const bundle = validateFeedbackBundle(value);
  if (!/^[a-f0-9]{64}$/.test(planId || "")) throw new ReviewError("Preview this packet and supply its planId before applying.", 409);
  const runFile = path.join(runDirectory(bundle, planId), "plan.json");
  let run = await readJSON<FeedbackRun>(runFile);
  if (!run) {
    const prepared = await prepare(bundle);
    if (prepared.plan.planId !== planId) throw new ReviewError("The local review, receipt or packet changed since preview. Preview again; no feedback was applied.", 409);
    if (!prepared.plan.canApply) throw new ReviewError("Resolve the preview conflicts or select one compatible item before applying.", 409);
    run = { schemaVersion: 1, bundleDigest: bundle.digest, planId, plan: prepared.plan,
      scenes: prepared.scenes.map(s => ({ sceneId: s.summary.sceneId, itemId: s.item.id, localHash: s.localHash, receiptHash: s.receiptHash, review: s.review, shouldSave: s.shouldSave, summary: s.summary, ...(s.chained ? { chained: true as const } : {}) })) };
    for (const scene of run.scenes) requireWrite(principal, scene);
    // Equal plan IDs describe equal content; concurrent first writers publish the same immutable plan.
    await atomicJSON(runFile, run);
  }
  if (!storedRunMatches(run, bundle, planId)) throw new ReviewError("The saved apply plan does not match this packet.", 409);
  const applied: { sceneId: string; itemId: string; localRevision: number; alreadyApplied: boolean; receiptOnly: boolean; reviewUrl: string }[] = [];
  for (const [index, scene] of run.scenes.entries()) {
    requireWrite(principal, scene);
    const entry = bundle.entries.find(e => e.itemId === scene.itemId)!;
    const journalFile = sceneJournalFile(bundle, planId, scene.itemId);
    let canReplan = false;
    try {
      const result = await sceneReviewTransaction(scene.sceneId, principal, async (data, commit) => {
        await checkedImage(data);
        if (!scene.summary.recoveredFrom && feedbackContentHash(data.model) !== feedbackContentHash(feedbackDocument(entry.model, "Bound source model")))
          throw new ReviewError("The local source model changed; recover it explicitly before retrying feedback.", 409);
        const snapshots = scene.summary.recoveredFrom ? await chainSnapshots(scene.sceneId) : [];
        const currentReceipt = await readJSON<FeedbackReceipt>(receiptFile(bundle, scene.itemId));
        let journal = await readJSON<SceneJournal>(journalFile);
        if (journal) {
          if (journal.planId !== planId || journal.bundleDigest !== bundle.digest || journal.itemId !== scene.itemId || journal.sceneId !== scene.sceneId)
            throw new ReviewError("The prepared receipt does not match this apply plan.", 409);
          const expected = { ...scene.review, revision: scene.review.revision + (scene.shouldSave ? 1 : 0) };
          if (feedbackContentHash(reviewContent(journal.expected)) !== feedbackContentHash(reviewContent(expected)) || feedbackContentHash(reviewContent(journal.receipt.appliedReview)) !== feedbackContentHash(reviewContent(expected)) ||
              journal.receipt.remote.sha256 !== entry.item.sha256 || journal.receipt.planId !== planId || journal.receipt.localRevision !== expected.revision || journal.receipt.origin !== bundle.origin || journal.receipt.projectId !== bundle.projectId || journal.receipt.itemId !== scene.itemId || journal.receipt.sceneId !== scene.sceneId || journal.receipt.snapshotId !== bundle.snapshotId || journal.receipt.manifestSha256 !== bundle.manifestSha256 || journal.receipt.sourceModelSha256 !== entry.model.sha256 || journal.receipt.sourceReviewSha256 !== entry.review?.sha256)
            throw new ReviewError("The prepared receipt content differs from the saved preview. Inspect the preserved journal.", 409);
          feedbackDocument(journal.receipt.remote, "Prepared cloud receipt");
          const existing = matchingSavedReview(data, journal.expected);
          if (existing || (!scene.shouldSave && stateHash(data, scene.chained) === scene.localHash)) {
            if (feedbackContentHash(currentReceipt || null) === scene.receiptHash) await atomicJSON(receiptFile(bundle, scene.itemId), journal.receipt);
            else {
              const newer = currentReceipt && feedbackDocument<ItemRevision>(currentReceipt.remote, "Current receipt"), accepted = feedbackDocument<ItemRevision>(journal.receipt.remote, "Prepared receipt");
              if (!currentReceipt || currentReceipt.origin !== bundle.origin || currentReceipt.projectId !== bundle.projectId || currentReceipt.sceneId !== scene.sceneId || currentReceipt.itemId !== scene.itemId || currentReceipt.snapshotId !== bundle.snapshotId || currentReceipt.manifestSha256 !== bundle.manifestSha256 || currentReceipt.sourceModelSha256 !== entry.model.sha256 || currentReceipt.sourceReviewSha256 !== entry.review?.sha256 ||
                  !newer || newer.revision < accepted.revision || (newer.revision === accepted.revision && currentReceipt.remote.sha256 !== journal.receipt.remote.sha256))
                throw new ReviewError("The accepted-feedback receipt changed unexpectedly. Inspect the preserved apply journal.", 409);
            }
            return { localRevision: journal.receipt.localRevision, alreadyApplied: true };
          }
        }
        if (stateHash(data, scene.chained) !== scene.localHash || feedbackContentHash(currentReceipt || null) !== scene.receiptHash) {
          // Every earlier result returned only after its durable receipt was
          // saved or verified. Replan only before any remaining journal exists.
          canReplan = applied.length === index && await journalsAbsent(bundle, planId, run.scenes.slice(index));
          throw new ReviewError("This scene changed after preview. Earlier applied scenes are preserved; preview again for the remaining conflict.", 409);
        }
        const check = prepareFeedbackScene(bundle, entry, data, currentReceipt, snapshots);
        if (check.summary.status === "conflict" || check.shouldSave !== scene.shouldSave || feedbackContentHash(reviewContent(validatedReview(check.review, data.model))) !== feedbackContentHash(reviewContent(scene.review)))
          throw new ReviewError("The scene or source binding no longer matches the preview.", 409);
        const expected = { ...scene.review, revision: scene.review.revision + (scene.shouldSave ? 1 : 0) };
        const receipt: FeedbackReceipt = { schemaVersion: 1, origin: bundle.origin, projectId: bundle.projectId, itemId: scene.itemId, sceneId: scene.sceneId,
          snapshotId: bundle.snapshotId, manifestSha256: bundle.manifestSha256, bundleDigest: bundle.digest, cursor: bundle.cursor, planId,
          remote: entry.item, appliedReview: expected, localRevision: expected.revision, sourceModelSha256: entry.model.sha256, sourceReviewSha256: entry.review?.sha256,
          actor: entry.actor, timestamp: entry.timestamp, acceptedBy: principal.principal, acceptedAt: new Date().toISOString(),
          ...(scene.summary.recoveredFrom ? { recoveredFrom: scene.summary.recoveredFrom } : {}) };
        journal = { schemaVersion: 1, bundleDigest: bundle.digest, planId, sceneId: scene.sceneId, itemId: scene.itemId, localHash: scene.localHash, receiptHash: scene.receiptHash, expected, receipt };
        await atomicJSON(journalFile, journal);
        // Reload after file IO as extraction uses a separate lock from review saves.
        if (stateHash(await loadScene(scene.sceneId), scene.chained) !== scene.localHash) throw new ReviewError("The model or review changed while preparing the receipt. Preview again.", 409);
        if (scene.shouldSave) {
          await commit(scene.review, { feedbackImport: true });
          await options.afterSceneSave?.(scene.sceneId);
        }
        await atomicJSON(receiptFile(bundle, scene.itemId), receipt);
        return { localRevision: expected.revision, alreadyApplied: false };
      });
      applied.push({ sceneId: scene.sceneId, itemId: scene.itemId, ...result, receiptOnly: !scene.shouldSave, reviewUrl: scene.summary.reviewUrl });
    } catch (error) {
      if (!(error instanceof ReviewError)) throw error;
      return { ...run.plan, applied, complete: false, error: error.message, status: error.status, retryPlanId: planId, ...(canReplan ? { canReplan: true } : {}) };
    }
  }
  return { ...run.plan, applied, complete: true };
}
interface ItemRevision { revision: number }
