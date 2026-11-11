import { NextRequest, NextResponse } from "next/server";
import { parcelBoundsIssue, type ParcelBounds } from "@/lib/property-parcels";
import { cachedParcels } from "@/lib/property-parcel-service";

export const dynamic = "force-dynamic";
export async function GET(request: NextRequest) {
  const raw=request.nextUrl.searchParams.get("bounds") || "";
  const bounds=raw.split(",").map(v=>v.trim()?Number(v):NaN) as ParcelBounds;
  const issue=parcelBoundsIssue(bounds);
  if (issue) return NextResponse.json({error:issue},{status:400});
  try {
    return NextResponse.json(await cachedParcels(bounds),{headers:{"Cache-Control":"private,max-age=3600"}});
  } catch {
    return NextResponse.json({error:"Vicmap boundaries are unavailable or this area has too many parcels. Retry or zoom in. Saved roof data is unchanged."},{status:503});
  }
}
