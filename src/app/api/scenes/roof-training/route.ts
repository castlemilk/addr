import { NextResponse } from "next/server";
import { roofTrainingRuns } from "@/lib/scene-roof-run-store";

export const dynamic = "force-dynamic";
export async function GET() {
  try { return NextResponse.json(await roofTrainingRuns()); }
  catch { return NextResponse.json({ error: "Roof training results could not be loaded. Refresh to retry." }, { status: 503 }); }
}
