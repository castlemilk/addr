import type { SceneModelSnapshot, SceneObject } from "./scene-types";
import { isShadeSail } from "./scene-structure";

export const VISIBILITY_LABELS = {
  full: "Fully visible",
  occluded: "Partly hidden",
  truncated: "Clipped by image edge",
  occluded_truncated: "Hidden and clipped",
} as const;

/** An edge touch is a review cue, never a saved visibility label. */
export function sceneVisibility(object: SceneObject, scene: Pick<SceneModelSnapshot,"image_width"|"image_height">) {
  const touchesEdge=Number.isFinite(scene.image_width)&&Number.isFinite(scene.image_height)&&object.ring.some(([x,y])=>
    x<=.5||y<=.5||x>=scene.image_width-.5||y>=scene.image_height-.5);
  const partial=object.visibility ? object.visibility!=="full" : touchesEdge;
  // A reviewed full canopy may have natural gaps. Until its extent is assessed,
  // those gaps may instead represent occlusion, so retain the partial preview.
  const gapsPreventBody=!!object.holes?.length&&(object.kind==="car"||object.visibility!=="full");
  return {touchesEdge,partial,footprintOnly:isShadeSail(object)||((partial||gapsPreventBody)&&["car","tree"].includes(object.kind))};
}
