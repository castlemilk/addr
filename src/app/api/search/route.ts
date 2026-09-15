import { NextRequest, NextResponse } from "next/server";
import { search } from "@/lib/geo-engine";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const searchParams = request.nextUrl.searchParams;
  const q = searchParams.get("q");
  const latStr = searchParams.get("lat");
  const lonStr = searchParams.get("lon");
  const limitStr = searchParams.get("limit");

  if (!q || q.trim().length === 0) {
    return NextResponse.json({ results: [] });
  }

  const lat = latStr ? parseFloat(latStr) : undefined;
  const lon = lonStr ? parseFloat(lonStr) : undefined;
  const limit = limitStr ? parseInt(limitStr, 10) : 15;

  try {
    const results = search(q, {
      lat: !isNaN(lat as number) ? lat : undefined,
      lon: !isNaN(lon as number) ? lon : undefined,
      limit,
    });
    return NextResponse.json({ results });
  } catch (err: unknown) {
    console.error("Search error:", err);
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "Failed to search" },
      { status: 500 }
    );
  }
}
