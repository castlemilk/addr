import { NextRequest, NextResponse } from "next/server";
import { scenePrincipal } from "@/lib/scene-store";
import { ReviewError } from "@/lib/review-feedback";
import { applyFeedback, previewFeedback } from "@/lib/annotations/feedback-store";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";
export async function POST(request: NextRequest) {
  try {
    const principal = await scenePrincipal(request);
    if (Number(request.headers.get("content-length") || 0) > 128 * 1024 * 1024) throw new ReviewError("Select fewer feedback items per packet.", 413);
    const text = await request.text();
    if (Buffer.byteLength(text, "utf8") > 128 * 1024 * 1024) throw new ReviewError("Select fewer feedback items per packet.", 413);
    let body;
    try { body = JSON.parse(text); } catch { throw new ReviewError("Send a valid JSON feedback request."); }
    if (body.mode === "preview") return NextResponse.json(await previewFeedback(body.bundle, principal), { headers: { "Cache-Control": "no-store" } });
    if (body.mode !== "apply") throw new ReviewError("Choose preview or apply.");
    const result = await applyFeedback(body.bundle, body.planId, principal);
    return NextResponse.json(result, { status: "status" in result ? result.status : 200, headers: { "Cache-Control": "no-store" } });
  } catch (error) {
    return NextResponse.json({ error: error instanceof ReviewError ? error.message : "Feedback reconciliation could not finish. Saved scenes and apply journals are preserved; retry the same plan." },
      { status: error instanceof ReviewError ? error.status : 503, headers: { "Cache-Control": "no-store" } });
  }
}
