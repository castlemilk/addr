import { NextRequest, NextResponse } from "next/server";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import path from "node:path";
import { scenePrincipal } from "@/lib/scene-store";
import { ReviewError, validCohort } from "@/lib/review-feedback";
import { regionIssue } from "@/lib/scene-region";
import { loadScene } from "@/lib/scene-store";
const execute = promisify(execFile);
export async function POST(request: NextRequest) {
  try {
    const principal = await scenePrincipal(request);
    if (!principal.scopes.includes("crm.object.write"))
      throw new ReviewError(
        "Object editing access is required for extraction.",
        403,
      );
    const body = await request.json();
    if (
      !body ||
      typeof body !== "object" ||
      (body.prompt !== undefined &&
        (typeof body.prompt !== "string" || body.prompt.length > 150)) ||
      (body.capture_only !== undefined &&
        typeof body.capture_only !== "boolean") ||
      (body.expand_context !== undefined && typeof body.expand_context !== "boolean")
    )
      throw new ReviewError(
        "Choose a map area or captured address and a short object prompt.",
      );
    const args = ["-m", "scripts.ml.scene_extract", "--roof-models"];
    if(body.expand_context && (body.scene_id===undefined || body.bounds!==undefined || body.capture_only))
      throw new ReviewError("Choose a saved scene to expand its analysis area.");
    if (body.capture_only && body.bounds === undefined)
      throw new ReviewError("Choose map bounds for an image-only capture.");
    if (body.bounds !== undefined) {
      const issue = regionIssue(body.bounds);
      if (issue) throw new ReviewError(issue);
      if (
        !["google", "esri"].includes(body.provider) ||
        body.scene_id !== undefined ||
        body.address_id !== undefined
      )
        throw new ReviewError(
          "Choose one area and an aerial imagery provider.",
        );
      args.push(
        "--detector-only",
        "--bbox",
        ...body.bounds.map(String),
        "--provider",
        body.provider,
      );
      if (body.capture_only) args.push("--capture-only");
    } else if (body.scene_id !== undefined) {
      if (body.address_id !== undefined || body.cohort !== undefined)
        throw new ReviewError("Choose a saved map scene or a captured address.");
      const scene = await loadScene(body.scene_id);
      if (!scene.model.region)
        throw new ReviewError("Choose a captured map area.");
      args.push("--scene-id", body.scene_id, "--detector-only");
      if(body.expand_context) {
        if(scene.stale || scene.model.image_width!==512 || scene.model.image_height!==512)
          throw new ReviewError("This scene is already expanded or needs feedback recovery first.",409);
        args.push("--expand-context");
      }
    } else {
      if (
        !validCohort(body.cohort) ||
        !Number.isSafeInteger(body.address_id) ||
        body.address_id <= 0
      )
        throw new ReviewError("Choose a captured address.");
      args.push(
        "--cohort",
        body.cohort,
        "--address-id",
        String(body.address_id),
      );
    }
    if (body.prompt?.trim()) args.push("--prompt", body.prompt.trim());
    const { stdout } = await execute(
      path.join(process.cwd(), "scripts/ml/.venv/bin/python"),
      args,
      // A scene can now include up to 12 registered per-lot SAM crops.
      { cwd: process.cwd(), timeout: 600000, maxBuffer: 1024 * 1024 },
    );
    const output = stdout
      .trim()
      .split("\n")
      .reverse()
      .map((line) => {
        try {
          return JSON.parse(line);
        } catch {
          return null;
        }
      })
      .find((value) => value && typeof value.scene === "string");
    if (!output) throw new Error("Missing scene output");
    return NextResponse.json({ id: output.scene });
  } catch (e) {
    if ((e as { code?: number })?.code === 75)
      return NextResponse.json(
        {
          error: "Another scene extraction is running. Retry when it finishes.",
        },
        { status: 409 },
      );
    return NextResponse.json(
      {
        error:
          e instanceof ReviewError
            ? e.message
            : "Extraction did not complete. Existing reviews are preserved; retry the scene.",
      },
      { status: e instanceof ReviewError ? e.status : 503 },
    );
  }
}
