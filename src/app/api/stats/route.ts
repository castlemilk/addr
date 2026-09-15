import { NextResponse } from "next/server";
import { getDbStats } from "@/lib/geo-engine";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const stats = getDbStats();
    return NextResponse.json(stats);
  } catch (err: unknown) {
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "Failed to retrieve stats" },
      { status: 500 }
    );
  }
}
