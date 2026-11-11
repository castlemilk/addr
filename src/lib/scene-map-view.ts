/** A tilted map's distant horizon must not disable meshes around its focus. */
export function sceneMapWindow(bounds: [number, number, number, number], focus: [number, number]) {
  const [west, south, east, north] = bounds;
  const metresPerLongitude = 111320 * Math.cos(focus[1] * Math.PI / 180);
  const limited = Math.max((east - west) * metresPerLongitude, (north - south) * 111320) > 2400;
  if (!limited) return { bounds, limited };
  const dx = 600 / metresPerLongitude, dy = 600 / 111320;
  return { bounds: [Math.max(west, focus[0] - dx), Math.max(south, focus[1] - dy),
    Math.min(east, focus[0] + dx), Math.min(north, focus[1] + dy)] as typeof bounds, limited };
}
