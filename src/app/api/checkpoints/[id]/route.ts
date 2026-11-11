import { NextRequest, NextResponse } from "next/server";
import * as fs from "node:fs";
import path from "node:path";
import { CHECKPOINT_ID, CHECKPOINTS_ROOT, fileIO, QueueError, readCheckpoint } from "@/lib/queues";

// One checkpoint: its REVIEW.md text (rendered client-side by markdown-lite) and report.json. The id must
// match the checkpoint pattern and resolve inside the checkpoints folder; otherwise 400/404. Read-only.
export const dynamic = "force-dynamic";
const NO_STORE = { "Cache-Control": "no-store" };

export async function GET(_request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  try {
    const id = String((await params).id ?? "");
    const folder = path.resolve(process.cwd(), CHECKPOINTS_ROOT);
    if (!CHECKPOINT_ID.test(id) || path.dirname(path.resolve(folder, id)) !== folder) throw new QueueError("Choose a valid checkpoint id.", 400);
    const checkpoint = readCheckpoint(fileIO(process.cwd(), fs), id);
    if (!checkpoint) throw new QueueError("That checkpoint is not on this machine.", 404);
    return NextResponse.json(checkpoint, { headers: NO_STORE });
  } catch (error) {
    return NextResponse.json({ error: error instanceof QueueError ? error.message : "The checkpoint report could not be read." },
      { status: error instanceof QueueError ? error.status : 500, headers: NO_STORE });
  }
}
