import { NextResponse } from "next/server";
import * as fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";
import { DatabaseSync } from "node:sqlite";
import { feedbackFor, readFeedback } from "@/lib/review-feedback";
import { buildCalibrationQueue, compactCalibration, EYES_INDEX, fileIO, HIDDEN_VERSION, latestPlanId, listCheckpoints, parseJson,
  selfReviewSummary, SUMMARY_NOTE, type QueueIO, type QueueSummary } from "@/lib/queues";

// Hub counts for /queues, all read-only from disk with no Python process: the newest calibration plan
// (live row status), hidden low roofs counted in the eyes index, the self-review loop's compact fields
// (roof_review_loop.compact over its report.json), the roof training cohort's saved decisions and the
// filed checkpoints. Every count carries its denominator; nothing here approves or changes a roof.
export const dynamic = "force-dynamic";
const NO_STORE = { "Cache-Control": "no-store" };
// The hub's "Roof training review" line opens /reviews?cohort=roof_training_250, so its denominator is that
// cohort (250 roofs), not the 12-roof suburban_benchmark the reviews page defaults to.
const COHORT = "roof_training_250";
const sha256 = (text: string) => createHash("sha256").update(text, "utf8").digest("hex");

/** Rows of cross_capture where the hidden roof is band low and the shown one medium/high; null when the index is absent. */
function hiddenLowCount(file: string): number | null {
  let db: DatabaseSync | null = null;
  try {
    db = new DatabaseSync(file, { readOnly: true });
    const row = db.prepare("SELECT COUNT(*) AS n FROM cross_capture WHERE version = ? AND json_extract(payload, '$.band') = 'low'"
      + " AND json_extract(payload, '$.other_band') IN ('medium', 'high')").get(HIDDEN_VERSION) as { n?: unknown } | undefined;
    return typeof row?.n === "number" ? row.n : null;
  } catch {
    return null;
  } finally {
    db?.close();
  }
}

/** Saved decisions over the roof training cohort, through the same feedback store and key lookup /api/reviews uses;
 *  reviewed = a saved status other than unreviewed, the count the reviews page shows as "N of M reviewed". */
async function roofReview(io: QueueIO): Promise<QueueSummary["roof_review"]> {
  const cohort = parseJson(io.readText(`data/cohorts/${COHORT}.json`));
  const entries = cohort && typeof cohort === "object" && Array.isArray((cohort as { entries?: unknown }).entries) ? (cohort as { entries: unknown[] }).entries : [];
  const ids = entries.map(e => (e && typeof e === "object" ? Number((e as { address_id?: unknown }).address_id) : NaN)).filter(Number.isInteger);
  const store = ids.length ? await readFeedback(path.join(process.cwd(), "data", "reviews", "feedback.json")) : {};
  const reviewed = ids.filter(id => { const status = feedbackFor(store, COHORT, id)?.status; return typeof status === "string" && status !== "unreviewed"; }).length;
  return { cohort: COHORT, reviewed, total: ids.length };
}

export async function GET() {
  try {
    const io = fileIO(process.cwd(), fs);
    const planId = latestPlanId(io);
    const body: QueueSummary = {
      calibration: planId ? compactCalibration(buildCalibrationQueue(io, planId)) : null,
      hidden: { count: hiddenLowCount(path.join(process.cwd(), EYES_INDEX)) },
      self_review: selfReviewSummary(io),
      roof_review: await roofReview(io),
      checkpoints: listCheckpoints(io, sha256).map(({ id, title, created_at, summary }) => ({ id, title, created_at, summary })),
      note: SUMMARY_NOTE,
    };
    return NextResponse.json(body, { headers: NO_STORE });
  } catch {
    return NextResponse.json({ error: "The review queue summary could not be read. Saved reviews are unchanged." }, { status: 500, headers: NO_STORE });
  }
}
