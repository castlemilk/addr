import { NextRequest, NextResponse } from "next/server";
import { candidateList, candidateScene, importCandidate } from "@/lib/scene-candidate-store";
import { scenePrincipal } from "@/lib/scene-store";
import { ReviewError } from "@/lib/review-feedback";
export const dynamic = "force-dynamic";
function failure(e: unknown) {
  return NextResponse.json({ error: e instanceof ReviewError ? e.message : "Candidate data is unavailable. Please retry." },
    { status: e instanceof ReviewError ? e.status : (e as NodeJS.ErrnoException)?.code === "ENOENT" ? 404 : 503 });
}
export async function GET(request: NextRequest) {
  try {
    const query = request.nextUrl.searchParams, candidate = query.get("candidate"), scene = query.get("scene");
    if (candidate || scene) {
      if (!candidate || !scene) throw new ReviewError("Choose a candidate and image.");
      return NextResponse.json(await candidateScene(candidate, scene, query.get("check") || undefined));
    }
    return NextResponse.json(await candidateList());
  } catch (e) { return failure(e); }
}
export async function POST(request: NextRequest) {
  try {
    const principal = await scenePrincipal(request);
    return NextResponse.json(await importCandidate(await request.json(), principal));
  } catch (e) { return failure(e); }
}
