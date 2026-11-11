import { NextResponse } from "next/server";
import * as fs from "node:fs";
import { createHash } from "node:crypto";
import { fileIO, listCheckpoints } from "@/lib/queues";

// Filed eyes checkpoints (data/references/eyes-checkpoints/<id>/report.json), newest first. Folder names
// must match the checkpoint id pattern, so dry-run/ subfolders never appear. Read-only.
export const dynamic = "force-dynamic";
const NO_STORE = { "Cache-Control": "no-store" };
const sha256 = (text: string) => createHash("sha256").update(text, "utf8").digest("hex");

export async function GET() {
  try {
    return NextResponse.json({ checkpoints: listCheckpoints(fileIO(process.cwd(), fs), sha256) }, { headers: NO_STORE });
  } catch {
    return NextResponse.json({ error: "The checkpoint reports could not be read." }, { status: 500, headers: NO_STORE });
  }
}
