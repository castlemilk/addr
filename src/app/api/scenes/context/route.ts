import { NextRequest } from "next/server";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import path from "node:path";
import { scenePath } from "@/lib/scene-store";

const execute = promisify(execFile);
const images = new Map<string, Promise<Buffer>>();
export const dynamic = "force-dynamic";

/** Context only: immutable model/image coordinates; never writes a review. */
export async function GET(request: NextRequest) {
  try {
    const id = request.nextUrl.searchParams.get("id") || "";
    const version = request.nextUrl.searchParams.get("version") || "";
    if (!/^[a-f0-9]{64}$/.test(version)) return new Response("Invalid model version", { status: 400 });
    const directory = scenePath(id), key = `${id}:${version}`;
    let pending = images.get(key);
    if (!pending) {
      pending = execute(path.join(process.cwd(), "scripts/ml/.venv/bin/python"),
        ["-m", "scripts.ml.scene_context", "--model", path.join(directory, "models", `${version}.json`)],
        { cwd: process.cwd(), encoding: "buffer", timeout: 20000, maxBuffer: 8 * 1024 * 1024 })
        .then(({ stdout }) => stdout);
      if (images.size >= 8) images.delete(images.keys().next().value!);
      images.set(key, pending);
      pending.catch(() => { if (images.get(key) === pending) images.delete(key); });
    }
    return new Response(new Uint8Array(await pending), { headers: {
      "Content-Type": "image/png", "Cache-Control": "private,max-age=3600",
    } });
  } catch {
    return new Response("Surrounding imagery unavailable. The saved image is unchanged.", { status: 404 });
  }
}
