import { NextRequest, NextResponse } from "next/server";
import { loadScene } from "@/lib/scene-store";
import { sceneAddressEvidence } from "@/lib/scene-address-store";
import { ReviewError } from "@/lib/review-feedback";
export const dynamic="force-dynamic";
export async function GET(request: NextRequest) {
  try {
    const data=await loadScene(request.nextUrl.searchParams.get("id")||"");
    return NextResponse.json(await sceneAddressEvidence(data));
  } catch(error) {
    return NextResponse.json({error:error instanceof ReviewError?error.message:"Address evidence is unavailable. Saved roof links are preserved."},
      {status:error instanceof ReviewError?error.status:503});
  }
}
