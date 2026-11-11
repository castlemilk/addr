import { NextRequest, NextResponse } from "next/server";
import { readFile } from "fs/promises";
import { existsSync } from "fs";
import path from "path";
import { validCohort } from "@/lib/review-feedback";

export const dynamic = "force-dynamic";

const REPO_ROOT = process.cwd();

export async function GET(request: NextRequest) {
  const searchParams = request.nextUrl.searchParams;
  const addressId = searchParams.get("address_id");
  const type = searchParams.get("type") || "site";
  const cohort = searchParams.get("cohort") || "suburban_benchmark";

  if (!validCohort(cohort) || (addressId !== null && !/^[1-9][0-9]*$/.test(addressId))) {
    return new NextResponse("Invalid image reference", { status: 400 });
  }
  let filePath: string | null = null;

  if (type === "contact_sheet") {
    filePath = path.join(REPO_ROOT, "data", "reviews", "cohorts", cohort, "contact-sheet.png");
  } else if (addressId) {
    if (type === "site") {
      const cohortClean = path.join(
        REPO_ROOT,
        "data",
        "reviews",
        "cohorts",
        cohort,
        addressId,
        "site-clean.png"
      );
      filePath = existsSync(cohortClean) ? cohortClean : null;
    } else if (type === "mesh") {
      const cohortMesh = path.join(
        REPO_ROOT,
        "data",
        "reviews",
        "cohorts",
        cohort,
        addressId,
        "lod2-mesh.png"
      );
      filePath = existsSync(cohortMesh) ? cohortMesh : null;
    } else if (type === "mask") {
      const cohortMask = path.join(
        REPO_ROOT,
        "data",
        "reviews",
        "cohorts",
        cohort,
        addressId,
        "roof-mask.png"
      );
      filePath = existsSync(cohortMask) ? cohortMask : null;
    }
  }

  if (!filePath || !existsSync(/*turbopackIgnore: true*/ filePath)) {
    return new NextResponse("Image not found", { status: 404 });
  }

  try {
    const fileBuffer = await readFile(/*turbopackIgnore: true*/ filePath);

    return new NextResponse(fileBuffer, {
      headers: {
        "Content-Type": "image/png",
        "Cache-Control": "public, max-age=60",
      },
    });
  } catch (err: unknown) {
    console.error("Failed to read image:", err);
    return new NextResponse("Error reading image", { status: 500 });
  }
}
