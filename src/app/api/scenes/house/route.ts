import { NextRequest, NextResponse } from "next/server";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import path from "node:path";
import { loadScene, sceneList, scenePrincipal } from "@/lib/scene-store";
import { ReviewError } from "@/lib/review-feedback";
import { activeBuilding, rankAddressScenes } from "@/lib/scene-addresses";
import { sceneAddressEvidence } from "@/lib/scene-address-store";
const execute = promisify(execFile);
export const dynamic = "force-dynamic";
function addressId(value: unknown) {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value <= 0) throw new ReviewError("Choose a valid map address.");
  return value;
}
async function existing(id: number) {
  const scenes = rankAddressScenes(await sceneList(),id);
  const found = scenes[0];
  if (!found) return { scene: null };
  const data = await loadScene(found.id), objects = data.stale ? data.model.objects : data.review?.objects || data.model.objects;
  const buildings = objects.filter(activeBuilding);
  const linked=found.properties.filter(p=>p.address_id===id).sort((a,b)=>Number(b.has_feedback)-Number(a.has_feedback));
  let object=buildings.find(o=>o.id===linked[0]?.object_id);
  let match="saved-link";
  if(!object) {
    const evidence=await sceneAddressEvidence(data).catch(()=>null);
    const matches=evidence?.objects.filter(o=>o.status==="suggested"&&o.address_id===id)||[];
    if(matches.length===1)object=buildings.find(o=>o.id===matches[0].object_id);
    match=object?"suggested":"unresolved";
  }
  const params = new URLSearchParams({ scene: found.id, status: "all",kind:"building" });
  if (object) params.set("object", object.id);
  else params.set("address",String(id));
  return { scene: { ...found, address_match:match, revision: data.review?.revision || 0, classes: [...new Set(objects.map(o => o.kind))], review_url: `/scenes?${params}` } };
}
function failure(error: unknown) {
  return NextResponse.json({ error: error instanceof ReviewError ? error.message : (error as {code?:number})?.code === 75 ? "Another analysis is running. Retry when it finishes." : "Analysis did not complete. Saved scenes and feedback are preserved. Retry to continue." },
    { status: error instanceof ReviewError ? error.status : (error as {code?:number})?.code === 75 ? 409 : 503 });
}
export async function GET(request: NextRequest) {
  try { return NextResponse.json(await existing(addressId(Number(request.nextUrl.searchParams.get("address_id"))))); }
  catch (error) { return failure(error); }
}
export async function POST(request: NextRequest) {
  try {
    const principal = await scenePrincipal(request);
    if (!principal.scopes.includes("crm.object.write")) throw new ReviewError("Object editing access is required for analysis.", 403);
    const body = await request.json(), id = addressId(body?.address_id);
    const found = await existing(id);
    if (found.scene) return NextResponse.json(found); // Opening a house never silently reruns its saved analysis.
    await execute(path.join(process.cwd(), "scripts/ml/.venv/bin/python"), ["-m", "scripts.ml.scene_extract", "--house-id", String(id), "--roof-models", "--detector-only"],
      { cwd: process.cwd(), timeout: 600000, maxBuffer: 1024 * 1024 });
    return NextResponse.json(await existing(id));
  } catch (error) { return failure(error); }
}
