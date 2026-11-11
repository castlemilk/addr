import { NextRequest, NextResponse } from "next/server";
import { readFile, readdir } from "fs/promises";
import { boundarySignature, feedbackFor, parseFeedback, readFeedback, ReviewError, saveFeedback, validCohort, type ReviewFeedback } from "@/lib/review-feedback";
import { existsSync } from "fs";
import path from "path";
import { auditAnnotation, modelSignature } from "@/lib/review-model";

export const dynamic = "force-dynamic";

const REPO_ROOT = process.cwd();
const FEEDBACK_FILE = path.join(REPO_ROOT, "data", "reviews", "feedback.json");

interface CohortEntry {
  address_id: number;
  full_address: string;
  category?: string;
  expected_topology?: string;
  tags?: string[];
}

export async function GET(request: NextRequest) {
  const searchParams = request.nextUrl.searchParams;
  const cohortName = searchParams.get("cohort") || "suburban_benchmark";

  if (!validCohort(cohortName)) return NextResponse.json({ error: "Invalid cohort name." }, { status: 400 });
  const cohortFile = path.join(REPO_ROOT, "data", "cohorts", `${cohortName}.json`);
  const isCorrections = cohortName === "crm_corrections";
  const isAll = cohortName === "all_roofs";
  if (!isCorrections && !isAll && !existsSync(cohortFile)) {
    return NextResponse.json(
      { error: `Cohort '${cohortName}' not found.` },
      { status: 404 }
    );
  }

  try {
    const cohortData = isCorrections || isAll
      ? { title: isCorrections ? "CRM corrections" : "All captured roofs", description: "Review captured boundaries against aerial imagery.", entries: [] }
      : JSON.parse(await readFile(cohortFile, "utf-8"));

    // Read address-roofs.json
    const addressRoofsFile = path.join(REPO_ROOT, "public", "address-roofs.json");
    const recordsById: Record<number, Record<string, unknown>> = {};
    if (existsSync(addressRoofsFile)) {
      const rawRecords = JSON.parse(await readFile(addressRoofsFile, "utf-8"));
      const recordList = Array.isArray(rawRecords) ? rawRecords : rawRecords.records || [];
      for (const r of recordList) {
        recordsById[Number(r.address_id)] = r as Record<string, unknown>;
      }
    }

    // Virtual sets reuse source-backed artifacts from the evaluated benchmark.
    const artifactCohort = isCorrections || isAll ? "suburban_benchmark" : cohortName;
    const reportFile = path.join(REPO_ROOT, "data", "reviews", "cohorts", artifactCohort, "report.json");
    const reportById: Record<number, Record<string, unknown>> = {};
    if (existsSync(reportFile)) {
      try {
        const rawReport = JSON.parse(await readFile(reportFile, "utf-8"));
        for (const item of rawReport.results || []) {
          reportById[Number(item.address_id)] = item as Record<string, unknown>;
        }
      } catch (e) {
        console.warn("Could not read report.json:", e);
      }
    }

    const feedbackStore = await readFeedback(FEEDBACK_FILE);
    const objectFile = path.join(REPO_ROOT,"data","reviews","cohorts",artifactCohort,"objects.json");
    const objectReport = existsSync(objectFile) ? JSON.parse(await readFile(objectFile,"utf8")) : {};
    if (isCorrections || isAll) {
      cohortData.entries = Object.values(recordsById).filter(record => !isCorrections || record.boundary_source === "crm");
    }
    const cohortFiles = (await readdir(path.join(REPO_ROOT, "data", "cohorts"))).filter(name => name.endsWith(".json"));
    const cohorts = await Promise.all(cohortFiles.map(async name => {
      const item = JSON.parse(await readFile(path.join(REPO_ROOT, "data", "cohorts", name), "utf8"));
      return { name: name.slice(0, -5), title: item.title || name.slice(0, -5), count: item.entries?.length ?? 0 };
    }));
    cohorts.push({ name: "crm_corrections", title: "CRM corrections", count: Object.values(recordsById).filter(r => r.boundary_source === "crm").length });
    cohorts.push({ name: "all_roofs", title: "All captured roofs", count: Object.keys(recordsById).length });

    const entries = (cohortData.entries || []).map((entry: CohortEntry) => {
      const id = Number(entry.address_id);
      const record = recordsById[id] || {};
      const rep = reportById[id] || {};
      const fb: Partial<ReviewFeedback> = feedbackFor(feedbackStore, cohortName, id) || {};

      const rawRooflines = (rep.rooflines && rep.parts)
        ? { parts: rep.parts as unknown[], lines: rep.rooflines as unknown[] }
        : (record.rooflines as { parts?: unknown[]; lines?: unknown[] } | undefined);
      const parts = rawRooflines?.parts || [];
      const lines = rawRooflines?.lines || [];
      const rawParts = (parts as Array<Record<string, unknown>>) || [];
      const signature = modelSignature(rawParts);
      const normalizedParts = rawParts.map((p) => ({
        name: typeof p.name === "string" ? p.name : undefined,
        ring_meters: (p.ring_meters || p.ring || []) as [number, number][],
        ring: (p.ring || p.ring_meters || []) as [number, number][],
        ring_pixels: (p.ring_pixels || []) as [number, number][],
        z: (p.z || p.ring_z || []) as number[],
        ring_z: (p.ring_z || p.z || []) as number[],
        area_m2: typeof p.area_m2 === "number" ? p.area_m2 : 0,
        area_3d_m2: typeof p.area_3d_m2 === "number" ? p.area_3d_m2 : undefined,
        pitch_deg: typeof p.pitch_deg === "number" ? p.pitch_deg : typeof p.slope_deg === "number" ? p.slope_deg : undefined,
        slope_deg: typeof p.slope_deg === "number" ? p.slope_deg : undefined,
        pitch_ratio: typeof p.pitch_ratio === "string" ? p.pitch_ratio : undefined,
        azimuth_deg: typeof p.azimuth_deg === "number" ? p.azimuth_deg : undefined,
        z_range: Array.isArray(p.z_range) ? (p.z_range as [number, number]) : undefined,
        kind: typeof p.kind === "string" ? p.kind : "main_pitch",
      }));

      const rawLines = (lines as unknown[]) || [];
      const normalizedLines = rawLines.map((l) => {
        if (Array.isArray(l)) {
          return {
            kind: "ridge",
            line_meters: l as [[number, number], [number, number]],
            line_pixels: [] as unknown as [[number, number], [number, number]],
          };
        }
        const obj = (l || {}) as Record<string, unknown>;
        return {
          kind: typeof obj.kind === "string" ? obj.kind : "ridge",
          line_meters: (obj.line_meters || obj.coords || []) as [[number, number], [number, number]],
          line_pixels: (obj.line_pixels || []) as [[number, number], [number, number]],
          line_z: Array.isArray(obj.line_z) ? (obj.line_z as [number, number]) : undefined,
          length_2d_m: typeof obj.length_2d_m === "number" ? obj.length_2d_m : undefined,
          length_3d_m: typeof obj.length_3d_m === "number" ? obj.length_3d_m : undefined,
          slope_deg: typeof obj.slope_deg === "number" ? obj.slope_deg : undefined,
        };
      });

      const ringPixels = (rep.ring_pixels || []) as [number, number][];

      // Only source-backed context belongs in the assessment. No sample parcels,
      // neighbour pins or decorative vegetation may masquerade as real data.
      const propertyBoundaryMeters = record.property_boundary_source ? record.property_boundary_meters || null : null;
      const nearbyPins = record.nearby_pins || [];
      const treeCanopiesMeters = record.tree_canopies_meters || [];
      const neighbourBuildingsMeters = record.neighbour_buildings_meters || [];
      const boundarySignatureValue = boundarySignature(record);
      const savedSignature = "boundary_signature" in fb ? fb.boundary_signature : undefined;
      const staleReview = "status" in fb && fb.status === "approved" && savedSignature !== boundarySignatureValue;
      const storedRooflines = record.rooflines as { pipeline_version?: number; geometry_source?: string; height_source?: string; structure_audit?: unknown; image_structure_audit?: unknown; parts?: unknown[] } | undefined;
      const legacyReport = Boolean((rep.parts || storedRooflines?.parts) && !(rep.pipeline_version || storedRooflines?.pipeline_version));

      return {
        address_id: id,
        full_address: entry.full_address || record.full_address || `Address #${id}`,
        category: entry.category || record.kind || "House",
        expected_topology: entry.expected_topology,
        tags: entry.tags || [],
        centroid: record.centroid || null,
        address_point: record.address_point || null,
        boundary_source: record.boundary_source || "extracted",
        boundary_signature: boundarySignatureValue,
        model_signature: signature,
        annotation_stale: Boolean((fb.annotation || fb.objects || fb.objects_verified) && (fb.model_signature !== signature || fb.boundary_signature !== boundarySignatureValue)),
        object_candidates: objectReport[String(id)]?.objects || [],
        object_detection_status: objectReport[String(id)]?.status || "not-run",
        roof_form_selection: rep.roof_form_selection || (record.rooflines as Record<string,unknown> | undefined)?.roof_form_selection || null,
        split: (entry as CohortEntry & {split?:string}).split || null,
        spatial_group: (entry as CohortEntry & {spatial_group?:string}).spatial_group || null,
        geometry_source: rep.geometry_source || storedRooflines?.geometry_source || (legacyReport ? "legacy-unverified" : normalizedParts.length ? "image-depth-estimate" : "not-extracted"),
        height_source: rep.height_source || storedRooflines?.height_source || "relative-depth-estimate",
        structure_audit: rep.structure_audit || storedRooflines?.structure_audit || null,
        image_structure_audit: rep.image_structure_audit || storedRooflines?.image_structure_audit || null,
        eave_height: record.eave_height ?? null,
        ridge_height: record.ridge_height ?? null,
        stale_review: staleReview,
        imagery_available: existsSync(path.join(REPO_ROOT, "data", "reviews", "cohorts", artifactCohort, String(id), "site-clean.png")),
        area_m2: rep.footprint_area_m2 || record.area_m2 || 0,
        face_count: rep.face_count || normalizedParts.length || 1,
        roofline_count: rep.roofline_count || normalizedLines.length || 0,
        roof_type: record.roof_type || "hipped",
        roof_material: record.roof_material || null,
        reference_ring_meters: record.ring_meters || [],
        ring_meters: rep.footprint_coords || rep.ring_meters || record.ring_meters || [],
        ring_pixels: ringPixels,
        property_boundary_meters: propertyBoundaryMeters,
        nearby_pins: nearbyPins,
        tree_canopies_meters: treeCanopiesMeters,
        neighbour_buildings_meters: neighbourBuildingsMeters,
        parts: normalizedParts,
        lines: normalizedLines,
        line_kinds: rep.line_kinds || {},
        part_kinds: rep.part_kinds || {},
        site_image_url: `/api/reviews/image?address_id=${id}&cohort=${artifactCohort}&type=site`,
        mesh_image_url: `/api/reviews/image?address_id=${id}&cohort=${artifactCohort}&type=mesh`,
        mask_image_url: `/api/reviews/image?address_id=${id}&cohort=${artifactCohort}&type=mask`,
        signals: rep.signals || null,
        reflection: rep.reflection || null,
        feedback: fb,
        training_eligible: !staleReview && "status" in fb && fb.status === "approved" && fb.boundary_verified === true
          && fb.boundary_signature === boundarySignatureValue && (!fb.annotation || (fb.model_signature === signature && fb.annotation_audit?.passed === true)),
      };
    });

    const contactSheetExists = existsSync(
      path.join(REPO_ROOT, "data", "reviews", "cohorts", cohortName, "contact-sheet.png")
    );

    return NextResponse.json({
      cohort: cohortName,
      cohorts,
      title: cohortData.title || cohortName,
      description: cohortData.description || "",
      contact_sheet_url: contactSheetExists
        ? `/api/reviews/image?cohort=${cohortName}&type=contact_sheet`
        : null,
      total_roofs: entries.length,
      entries,
    });
  } catch (err: unknown) {
    console.error("Failed to load review cohort:", err);
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "Failed to load cohort" },
      { status: err instanceof ReviewError ? err.status : 500 }
    );
  }
}

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    if (!body || typeof body !== "object" || !Number.isSafeInteger(body.address_id) || body.address_id <= 0 || !validCohort(body.cohort || "")) {
      throw new ReviewError("Choose a valid address and cohort.");
    }
    const origin = request.headers.get("origin");
    if (origin && origin !== request.nextUrl.origin) throw new ReviewError("Save reviews from this app.", 403);
    const service = process.env.GEO_SERVICE_URL || process.env.NEXT_PUBLIC_GEO_SERVICE_URL || "http://localhost:8085";
    let who;
    try {
      who = await fetch(`${service}/crm.v1.CrmService/WhoAmI`, { method: "POST", headers: {
        "Content-Type": "application/json", "Connect-Protocol-Version": "1",
        ...(request.headers.get("authorization") ? { Authorization: request.headers.get("authorization")! } : {}),
      }, body: "{}", signal: AbortSignal.timeout(5000) });
    } catch { throw new ReviewError("CRM service is unavailable. Your draft has been kept; retry when it reconnects.", 503); }
    if (who.status === 404 || who.status >= 500) throw new ReviewError("The configured service does not have CRM available. Check the CRM service connection and retry.", 503);
    if (!who.ok) throw new ReviewError("Sign in through Address CRM with roof editing access, then retry.", who.status === 403 ? 403 : 401);
    const principal = await who.json();
    if (!principal.scopes?.includes("crm.roof.write")) throw new ReviewError("Roof editing access is required to save reviews.", 403);
    const recordsRaw = JSON.parse(await readFile(path.join(REPO_ROOT, "public", "address-roofs.json"), "utf8"));
    const records = Array.isArray(recordsRaw) ? recordsRaw : recordsRaw.records || [];
    const record = records.find((r: { address_id: number }) => r.address_id === body.address_id);
    if (!record) throw new ReviewError("This address no longer has a captured roof.", 404);
    if (body.cohort === "crm_corrections") {
      if (record.boundary_source !== "crm") throw new ReviewError("This address is not in CRM corrections.", 404);
    } else if (body.cohort !== "all_roofs") {
      const cohort = JSON.parse(await readFile(path.join(REPO_ROOT, "data", "cohorts", `${body.cohort}.json`), "utf8"));
      if (!cohort.entries?.some((e: {address_id: number}) => e.address_id === body.address_id)) throw new ReviewError("This address is not in this cohort.", 404);
    }
    const signature = boundarySignature(record);
    if (body.boundary_signature !== signature) throw new ReviewError("The captured boundary changed. Reload it before saving your assessment.", 409);
    const feedback = parseFeedback(body);
    const artifactCohort = ["crm_corrections","all_roofs"].includes(body.cohort) ? "suburban_benchmark" : body.cohort;
    const reportFile = path.join(REPO_ROOT,"data","reviews","cohorts",artifactCohort,"report.json");
    const report = existsSync(reportFile) ? JSON.parse(await readFile(reportFile,"utf8")) : {};
    const model = report.results?.find((r:{address_id:number})=>r.address_id===body.address_id);
    const signatureModel = modelSignature(model?.parts || record.rooflines?.parts || []);
    if ((feedback.annotation || feedback.objects) && feedback.model_signature !== signatureModel) throw new ReviewError("The model changed. Reload before saving your geometry edits.",409);
    if (feedback.annotation) {
      feedback.annotation_audit = await auditAnnotation(feedback.annotation,record.ring_meters,record.eave_height || 3);
      if (!feedback.annotation_audit.passed && feedback.status === "approved") throw new ReviewError(`Fix roof geometry before approval: ${feedback.annotation_audit.issues.join(", ")}`);
      if (!feedback.annotation_audit.passed && !feedback.issues.includes("roof_planes")) feedback.issues.push("roof_planes");
    }
    if (feedback.objects_verified && feedback.objects?.some(o=>o.status === "candidate" && (feedback.object_reviewed_classes || ["solar_panel","skylight","chimney"]).includes(o.kind))) throw new ReviewError("Confirm or reject each object before marking the object review complete.");
    if (feedback.status === "approved" && !signature) throw new ReviewError("A captured boundary is required before approval.");
    feedback.boundary_signature = signature;
    const saved = await saveFeedback(FEEDBACK_FILE, body.cohort, body.address_id, feedback, principal.principal || "unknown");
    return NextResponse.json({ success: true, address_id: body.address_id, feedback: saved, training_eligible: saved.status === "approved" && saved.boundary_verified && (!saved.annotation || saved.annotation_audit?.passed) });
  } catch (error) {
    const status = error instanceof ReviewError ? error.status : error instanceof SyntaxError ? 400 : 500;
    return NextResponse.json({ error: error instanceof ReviewError ? error.message : "Could not save this review. Your draft has been kept." }, { status });
  }
}
