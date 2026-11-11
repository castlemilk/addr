import { NextRequest, NextResponse } from "next/server";
import * as fs from "node:fs";
import { buildCalibrationQueue, fileIO, QueueError } from "@/lib/queues";

// Live status of the sampled-roof calibration plan (docs/roof-eyes.md): plan.json and addresses.json are
// read with node:fs and every row is classified against the scene's current review.json/latest.json.
// Bands are model estimates, not accuracy; only human decisions are counted; this route never writes.
export const dynamic = "force-dynamic";
const NO_STORE = { "Cache-Control": "no-store" };

export async function GET(request: NextRequest) {
  try {
    const plan = request.nextUrl.searchParams.get("plan") || "";
    return NextResponse.json(buildCalibrationQueue(fileIO(process.cwd(), fs), plan), { headers: NO_STORE });
  } catch (error) {
    return NextResponse.json({ error: error instanceof QueueError ? error.message : "The calibration plan could not be read. Saved reviews are unchanged." },
      { status: error instanceof QueueError ? error.status : 500, headers: NO_STORE });
  }
}
