import { NextRequest, NextResponse } from "next/server";
import { getNearby } from "@/lib/geo-engine";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const searchParams = request.nextUrl.searchParams;
  const latStr = searchParams.get("lat");
  const lonStr = searchParams.get("lon");
  const radiusStr = searchParams.get("radius");
  const limitStr = searchParams.get("limit");

  if (!latStr || !lonStr) {
    return NextResponse.json(
      { error: "Missing required query parameters: 'lat' and 'lon'" },
      { status: 400 }
    );
  }

  const lat = parseFloat(latStr);
  const lon = parseFloat(lonStr);
  const radius = radiusStr ? parseFloat(radiusStr) : 400;
  const limit = limitStr ? parseInt(limitStr, 10) : 50;

  if (isNaN(lat) || isNaN(lon)) {
    return NextResponse.json(
      { error: "Invalid coordinates format" },
      { status: 400 }
    );
  }

  try {
    const data = getNearby(lat, lon, radius, limit);
    return NextResponse.json(data);
  } catch (err: unknown) {
    console.error("Nearby query error:", err);
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "Failed to query nearby items" },
      { status: 500 }
    );
  }
}
