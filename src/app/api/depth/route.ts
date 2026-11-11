import { NextResponse } from "next/server";

// Proxies the ML generator's relative-depth grid so the studio can model real
// terrain relief without exposing the sidecar or hitting browser CORS.
const GENERATOR_URL =
  process.env.TILE_GENERATOR_URL ||
  process.env.ML_GENERATOR_URL ||
  "http://127.0.0.1:8090";

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const z = Number.parseInt(searchParams.get("z") ?? "", 10);
  const x = Number.parseInt(searchParams.get("x") ?? "", 10);
  const y = Number.parseInt(searchParams.get("y") ?? "", 10);
  if (![z, x, y].every(Number.isFinite)) {
    return NextResponse.json({ error: "z, x, y are required" }, { status: 400 });
  }
  try {
    const response = await fetch(`${GENERATOR_URL}/depth/${z}/${x}/${y}.json`, {
      signal: AbortSignal.timeout(90000),
    });
    if (!response.ok) {
      return NextResponse.json(
        { error: "depth unavailable", status: response.status },
        { status: 502 }
      );
    }
    const payload = await response.json();
    return NextResponse.json(payload);
  } catch {
    return NextResponse.json({ error: "depth request failed" }, { status: 502 });
  }
}
