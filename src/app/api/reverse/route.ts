import { NextRequest, NextResponse } from "next/server";
import { reverseGeocode } from "@/lib/geo-engine";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const searchParams = request.nextUrl.searchParams;
  const latStr = searchParams.get("lat");
  const lonStr = searchParams.get("lon");
  const radiusStr = searchParams.get("radius");

  if (!latStr || !lonStr) {
    return NextResponse.json(
      { error: "Missing required query parameters: 'lat' and 'lon'" },
      { status: 400 }
    );
  }

  const lat = parseFloat(latStr);
  const lon = parseFloat(lonStr);
  const maxRadiusMeters = radiusStr ? parseFloat(radiusStr) : 300;

  if (isNaN(lat) || isNaN(lon)) {
    return NextResponse.json(
      { error: "Invalid coordinates format for 'lat' or 'lon'" },
      { status: 400 }
    );
  }

  try {
    const result = reverseGeocode(lat, lon, { maxRadiusMeters });
    return NextResponse.json(result);
  } catch (err: unknown) {
    console.error("Reverse geocoding error:", err);
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "Failed to reverse geocode" },
      { status: 500 }
    );
  }
}
