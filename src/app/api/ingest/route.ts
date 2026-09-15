import { NextRequest, NextResponse } from "next/server";
import { spawn } from "node:child_process";
import path from "node:path";

export const dynamic = "force-dynamic";

let activeIngestion: {
  status: "idle" | "running" | "completed" | "error";
  logs: string[];
  startTime?: number;
  endTime?: number;
  region?: string;
} = {
  status: "idle",
  logs: [],
};

export async function GET() {
  return NextResponse.json(activeIngestion);
}

export async function POST(request: NextRequest) {
  if (activeIngestion.status === "running") {
    return NextResponse.json(
      { error: "An ingestion process is already running." },
      { status: 409 }
    );
  }

  const body = await request.json().catch(() => ({}));
  const preset = body.preset || "melbourne_cbd";
  const limit = body.limit ? parseInt(body.limit, 10) : 3000;

  const scriptPath = path.resolve(process.cwd(), "scripts", "ingest.py");
  const args = ["-u", scriptPath, "--preset", preset];
  if (limit) {
    args.push("--limit", limit.toString());
  }

  activeIngestion = {
    status: "running",
    logs: [`[Start] Launching ingestion for preset: ${preset} (limit: ${limit})...`],
    startTime: Date.now(),
    region: preset,
  };

  const child = spawn("python3", args, {
    cwd: process.cwd(),
    env: { ...process.env, PYTHONUNBUFFERED: "1" },
  });

  child.stdout?.on("data", (data) => {
    const lines = data.toString().split("\n").filter(Boolean);
    activeIngestion.logs.push(...lines);
    if (activeIngestion.logs.length > 200) {
      activeIngestion.logs = activeIngestion.logs.slice(-200);
    }
  });

  child.stderr?.on("data", (data) => {
    const lines = data.toString().split("\n").filter(Boolean);
    activeIngestion.logs.push(...lines);
  });

  child.on("close", (code) => {
    activeIngestion.status = code === 0 ? "completed" : "error";
    activeIngestion.endTime = Date.now();
    activeIngestion.logs.push(
      code === 0
        ? `[Success] Ingestion completed successfully in ${((Date.now() - (activeIngestion.startTime || 0)) / 1000).toFixed(1)}s`
        : `[Failed] Ingestion exited with code ${code}`
    );
  });

  return NextResponse.json({
    message: "Ingestion started",
    region: preset,
  });
}
