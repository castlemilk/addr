import type { FeatureCollection, MultiPolygon } from "geojson";
import type { SceneModelSnapshot } from "./scene-types";
export interface SceneMapData {
  scenes: { model: SceneModelSnapshot; revision: number; stale: boolean; visible_ids: string[] }[];
  outlines: FeatureCollection;
  coverage: { building: MultiPolygon; tree: MultiPolygon };
  duplicates: number; available_scenes: number; limited: boolean; unavailable: number;
}
