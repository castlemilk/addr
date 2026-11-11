import { NextRequest, NextResponse } from "next/server";
import { execFile } from "node:child_process";
import path from "node:path";
import { scenePrincipal } from "@/lib/scene-store";
import { ReviewError } from "@/lib/review-feedback";
import { invalidateAddressEvidence } from "@/lib/scene-address-store";

export const dynamic = "force-dynamic";
async function command(payload: unknown, save = false) {
  const stdout = await new Promise<string>((resolve, reject) => {
    const child = execFile(path.join(process.cwd(), "scripts/ml/.venv/bin/python"),
      ["-m", "scripts.ml.address_resolution", ...(save ? ["--save"] : [])],
      { cwd: process.cwd(), timeout: 30000, maxBuffer: 4 * 1024 * 1024 },
      (error, output) => error ? reject(error) : resolve(output));
    child.stdin?.on("error", reject);
    child.stdin?.end(JSON.stringify(payload));
  });
  const result = JSON.parse(stdout);
  return NextResponse.json(result, { status: result.error ? result.status || 400 : 200, headers: { "Cache-Control": "no-store" } });
}
function failure(error: unknown) {
  return NextResponse.json({ error: error instanceof ReviewError ? error.message : "Address resolution is unavailable. Saved decisions are preserved." },
    { status: error instanceof ReviewError ? error.status : 503 });
}
export async function GET(request: NextRequest) {
  try { return await command({ scene_id: request.nextUrl.searchParams.get("scene"), object_id: request.nextUrl.searchParams.get("object") }); }
  catch (error) { return failure(error); }
}
export async function POST(request: NextRequest) {
  try {
    const principal = await scenePrincipal(request);
    if (!principal.scopes.includes("crm.address.write")) throw new ReviewError("Address editing access is required.", 403);
    const body = await request.json();
    const response = await command({ ...body, actor: principal.principal }, true);
    if (response.ok) invalidateAddressEvidence();
    return response;
  } catch (error) { return failure(error); }
}
