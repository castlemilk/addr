import { NextRequest, NextResponse } from "next/server";
import {
  loadScene,
  sceneList,
  scenePrincipal,
  saveScene,
} from "@/lib/scene-store";
import { ReviewError } from "@/lib/review-feedback";
import { sceneQueueFilter } from "@/lib/scene-queue";
import { withAddressNames } from "@/lib/scene-address-store";
export const dynamic = "force-dynamic";
export async function GET(request: NextRequest) {
  try {
    const id = request.nextUrl.searchParams.get("id");
    return NextResponse.json(
      id ? await withAddressNames(await loadScene(id)) : { scenes: await sceneList(sceneQueueFilter({
        kind:request.nextUrl.searchParams.get("kind"),status:request.nextUrl.searchParams.get("status"),query:request.nextUrl.searchParams.get("q"),issue:request.nextUrl.searchParams.get("issue"),
      })) },
    );
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "Could not load scenes." },
      { status: e instanceof ReviewError ? e.status : 500 },
    );
  }
}
export async function POST(request: NextRequest) {
  try {
    const principal = await scenePrincipal(request);
    const body = await request.json();
    const review = await saveScene(body.id, body.review, principal);
    return NextResponse.json({ review });
  } catch (e) {
    return NextResponse.json(
      {
        error:
          e instanceof ReviewError
            ? e.message
            : "Could not save the scene. Your draft is still here.",
      },
      { status: e instanceof ReviewError ? e.status : 500 },
    );
  }
}
