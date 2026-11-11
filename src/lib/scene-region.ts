export type SceneBounds = [number, number, number, number];
const WORLD_METERS = 40075016.68557849;

export function regionBounds(
  lat: number,
  lon: number,
  size: number,
): SceneBounds {
  const north = size / 2 / (WORLD_METERS / 360);
  const east = north / Math.cos((lat * Math.PI) / 180);
  return [lon - east, lat - north, lon + east, lat + north];
}

export function regionIssue(bounds: unknown): string | null {
  if (
    !Array.isArray(bounds) ||
    bounds.length !== 4 ||
    bounds.some((v) => typeof v !== "number" || !Number.isFinite(v))
  )
    return "Enter four finite map coordinates.";
  const [west, south, east, north] = bounds;
  if (
    !(
      west > -180 &&
      east < 180 &&
      west < east &&
      south > -85 &&
      north < 85 &&
      south < north
    )
  )
    return "Choose an area within the supported map bounds.";
  const lat = (south + north) / 2,
    width =
      (((east - west) * WORLD_METERS) / 360) * Math.cos((lat * Math.PI) / 180);
  const y = (v: number) => Math.asinh(Math.tan((v * Math.PI) / 180));
  const height =
    (((y(north) - y(south)) * WORLD_METERS) / (2 * Math.PI)) *
    Math.cos((lat * Math.PI) / 180);
  // Seven-decimal geographic coordinates can shift a side by about 1 cm.
  if (Math.min(width, height) < 9.98 || Math.max(width, height) > 250.02)
    return "Choose an area between 10 and 250 metres on each side.";
  return null;
}
