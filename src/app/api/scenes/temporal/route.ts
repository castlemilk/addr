import { NextRequest, NextResponse } from "next/server";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { readFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import path from "node:path";
import { scenePath, scenePrincipal } from "@/lib/scene-store";
import { ReviewError } from "@/lib/review-feedback";

export const dynamic = "force-dynamic";
const execute = promisify(execFile);
export async function GET(request: NextRequest) {
  try {
    const run = request.nextUrl.searchParams.get("run") || "";
    if (!/^[a-f0-9]{16}$/.test(run)) throw new ReviewError("Choose an imagery comparison.");
    const directory = path.join(process.cwd(), "data/roof-review-loop/multidate/runs", run);
    const asset = request.nextUrl.searchParams.get("asset");
    if (asset) {
      if (!/^(reference|saved-source|combined|combined-mask|capture-[0-2]|aligned-[0-2]|valid-[0-2])\.png$/.test(asset) &&
          !/^(input|reference|candidate|objects-[0-2])\.json$/.test(asset)) throw new ReviewError("Unknown comparison asset.");
      const bytes = await readFile(path.join(directory, asset));
      return new Response(bytes, { headers: { "Content-Type": asset.endsWith("png") ? "image/png" : "application/json", "Cache-Control": "no-store" } });
    }
    const report = JSON.parse(await readFile(path.join(directory, "report.json"), "utf8"));
    if (report.status === "running" && Date.now()/1000-report.updated_at > 90) {
      const pid = (await readFile(path.join(directory,"worker.pid"),"utf8").catch(()=>"")).trim();
      let alive = false;
      if (/^\d+$/.test(pid)) {
        const result = await execute("/bin/ps", ["-p",pid,"-o","command="], {timeout:3000}).catch(()=>null);
        alive = !!result?.stdout.includes("scripts.ml.scene_temporal") && !!result.stdout.includes(`--run ${run}`);
      }
      if (!alive) { report.status = "interrupted"; report.stage = "Worker stopped. Run the comparison again to resume."; }
    }
    const source = await readFile(path.join(scenePath(report.scene_id), "latest.json"));
    report.stale = createHash("sha256").update(source).digest("hex") !== report.source_sha256;
    return NextResponse.json(report, { headers: { "Cache-Control": "no-store" } });
  } catch (error) {
    return NextResponse.json({ error: error instanceof ReviewError ? error.message : "This comparison or image is not available yet." },
      { status: error instanceof ReviewError ? error.status : 404 });
  }
}
export async function POST(request: NextRequest) {
  try {
    const principal = await scenePrincipal(request);
    if (!principal.scopes.includes("crm.roof.write")) throw new ReviewError("Roof editing access is required.", 403);
    const body = await request.json();
    scenePath(body.scene);
    const { stdout } = await execute(path.join(process.cwd(), "scripts/ml/.venv/bin/python"),
      ["-m", "scripts.ml.scene_temporal", "--start", body.scene], { cwd: process.cwd(), timeout: 60000, maxBuffer: 4*1024*1024 });
    return NextResponse.json(JSON.parse(stdout));
  } catch (error) {
    return NextResponse.json({ error: error instanceof ReviewError ? error.message : "Could not start the imagery comparison. Saved predictions are unchanged." },
      { status: error instanceof ReviewError ? error.status : 503 });
  }
}
