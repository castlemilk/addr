import { NextRequest, NextResponse } from "next/server";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { ENRICHMENT_ROOT, parseEnrichment } from "@/lib/address-enrichment";

// GET /api/address/enrichment?address=<G-NAF id>: the per-address enrichment document the Python
// pipeline wrote under data/reviews/eyes/address-enrichment/addresses/<id>.json, validated leniently
// (src/lib/address-enrichment.ts). Agent hints and model estimates, never labels; this route never
// writes. 400 for anything but a plain integer id, 404 when no document exists for the address, 500
// when a document exists but cannot be read; no local path ever reaches a body.
export const dynamic = "force-dynamic";
const NO_STORE = { "Cache-Control": "no-store" };
const ADDRESS_ID = /^\d{1,15}$/;

export async function GET(request: NextRequest) {
  const raw = (request.nextUrl.searchParams.get("address") ?? "").trim();
  if (!ADDRESS_ID.test(raw)) return NextResponse.json({ error: "Choose an address by its integer G-NAF id." }, { status: 400, headers: NO_STORE });
  const addressId = Number(raw);
  let source: string;
  try {
    source = await readFile(path.join(process.cwd(), ENRICHMENT_ROOT, `${addressId}.json`), "utf8");
  } catch (error) {
    const code = (error as { code?: string } | null)?.code;
    if (code === "ENOENT" || code === "ENOTDIR") return NextResponse.json({ error: "No enrichment has been computed for this address." }, { status: 404, headers: NO_STORE });
    return NextResponse.json({ error: "The address enrichment could not be read. Saved reviews are unchanged." }, { status: 500, headers: NO_STORE });
  }
  const document = parseEnrichment(source);
  if (!document) return NextResponse.json({ error: "The address enrichment document is malformed. Saved reviews are unchanged." }, { status: 500, headers: NO_STORE });
  return NextResponse.json(document, { headers: NO_STORE });
}
