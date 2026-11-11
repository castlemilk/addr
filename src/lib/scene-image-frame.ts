import type { ScenePoint } from "./scene-types";

export type ImageFrame = { x: number; y: number; width: number; height: number };

/** Fit registered image geometry to the actual viewport, retaining context. */
export function objectImageFrame(rings: readonly (readonly ScenePoint[])[], image: { width: number; height: number }, viewport: { width: number; height: number }, edgeContext = false): ImageFrame {
  const points = rings.flat().filter(p => p.every(Number.isFinite));
  if (!points.length) return { x: 0, y: 0, ...image };
  let left = Math.min(...points.map(p => p[0])), right = Math.max(...points.map(p => p[0]));
  let top = Math.min(...points.map(p => p[1])), bottom = Math.max(...points.map(p => p[1]));
  if (edgeContext) {
    // A cut edge is not the end of the house. Reserve additional viewing room
    // on that side without extending the outline or inventing roof geometry.
    const extra = Math.min(96, Math.max(32, Math.max(right-left, bottom-top)*.3));
    if (left <= .5) left -= extra;
    if (right >= image.width-.5) right += extra;
    if (top <= .5) top -= extra;
    if (bottom >= image.height-.5) bottom += extra;
  }
  // Small objects retain enough nearby pixels to judge their class. Match the
  // viewport aspect where possible; image edges can still require letterboxing.
  let width = Math.max(right-left+24, Math.min(image.width, Math.max(55, right - left) * 1.6));
  let height = Math.max(bottom-top+24, Math.min(image.height, Math.max(55, bottom - top) * 1.6));
  const aspect = viewport.width > 0 && viewport.height > 0 ? viewport.width / viewport.height : 1;
  if (width / height < aspect) width = Math.max(width, Math.min(image.width, height * aspect));
  else height = Math.max(height, Math.min(image.height, width / aspect));
  // Keep the subject centred, including at capture edges. Surrounding imagery
  // or empty margins occupy the extra space; the registered coordinates stay put.
  return { x: (left + right - width) / 2, y: (top + bottom - height) / 2, width, height };
}

export function imageFrameScale(frame: ImageFrame, viewport: { width: number; height: number }) {
  return Math.max(.001, Math.min(viewport.width / frame.width, viewport.height / frame.height));
}

/** Clamp a frame to the image: no wider or taller than the image and shifted inside it. The initial view of a window scene is
 *  capped here (decision card item 3, 6 October 2026); the per-object fit keeps its context margins beyond a cut edge. */
export function capFrameToImage(frame: ImageFrame, image: { width: number; height: number }): ImageFrame {
  const width = Math.min(frame.width, image.width), height = Math.min(frame.height, image.height);
  return {
    x: Math.max(0, Math.min(frame.x, image.width - width)),
    y: Math.max(0, Math.min(frame.y, image.height - height)),
    width,
    height,
  };
}
