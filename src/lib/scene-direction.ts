import { carSettings } from "./scene-car";
import { isPavedSurface, surfaceSettings } from "./scene-surface";
import { transformPoint, type SceneObject, type ScenePoint } from "./scene-types";

export const normalizedBearing = (degrees: number) => {
  const bearing = degrees % 360;
  return bearing < 0 ? bearing + 360 : bearing === 0 ? 0 : bearing;
};

/** Bearings use east/north metres, including rotated or skewed image frames. */
export function imageBearing(center: ScenePoint, point: ScenePoint, pixelToMeters: number[][]) {
  const target = transformPoint(point, pixelToMeters);
  const east = target[0] - center[0], north = target[1] - center[1];
  if (!Number.isFinite(east + north) || Math.hypot(east, north) < 1e-9) return null;
  return normalizedBearing(Math.atan2(east, north) * 180 / Math.PI);
}

/** Direction feedback changes estimated structure, never outline approval. */
export function withObjectDirection(object: SceneObject, degrees: number, solarTilt?: number): SceneObject {
  if (!Number.isFinite(degrees)) return object;
  const heading = normalizedBearing(Math.round(normalizedBearing(degrees) * 100) / 100);
  const model = { ...object.model, height_verified: false };
  if (object.kind === "car") model.car = { ...carSettings(model), heading_deg: heading };
  else if (isPavedSurface(object.kind)) model.surface = { ...surfaceSettings(model), heading_deg: heading };
  else if (object.kind === "solar_panel" && solarTilt !== undefined && Number.isFinite(solarTilt) && solarTilt > 0 && solarTilt <= 80) {
    model.solar = { ...model.solar, tilt_deg: solarTilt, azimuth_deg: heading };
  } else return object;
  return { ...object, model };
}
