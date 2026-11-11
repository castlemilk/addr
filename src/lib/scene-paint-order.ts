// Scene editor paint order (road-shape 2026-10-07, UNDERSTANDING.md section 5, approach E1). The reviewer asked for roads to be
// "layered underneath other objects like cars/trees". SceneCanvas draws object paths in this order, bottom to top: ground surfaces
// (CLASS_PAINT_TIER 0), then standing objects (1), then roof fixtures (2). Within a tier the larger outline is drawn first, so the
// smaller object sits on top and wins the browser hit test. Equal keys keep array order. This is display only. The saved
// review.json order, the queue, advance, default selection and React keys all keep draft.objects order, because the function
// returns a new array and never sorts its input.
import { CLASS_PAINT_TIER, ringArea, type SceneObject } from "./scene-types";

type Paintable = Pick<SceneObject, "id" | "kind" | "ring">;

/** The paint tier of a kind. A kind missing from CLASS_PAINT_TIER (legacy data) is drawn as a standing object. */
export function scenePaintTier(kind: string): 0 | 1 | 2 {
  return (CLASS_PAINT_TIER as Partial<Record<string, 0 | 1 | 2>>)[kind] ?? 1;
}

/** Outer-ring area (image px²) per id, from the committed objects. SceneCanvas memoises this over draft.objects (and the model
 *  baseline), not over the vertex-drag preview, so a drag in progress never reshuffles two near-equal outlines. */
export function scenePaintAreas(objects: readonly Paintable[]): Map<string, number> {
  const areas = new Map<string, number>();
  for (const o of objects) areas.set(o.id, ringArea(o.ring));
  return areas;
}

/** Bottom-to-top paint order: tier ascending, then outer-ring area descending, then array index. `areas` (from scenePaintAreas)
 *  overrides the ring area for the ids it holds. Returns a new array; the input is never reordered. */
export function scenePaintOrder<T extends Paintable>(items: readonly T[], areas?: ReadonlyMap<string, number>): T[] {
  return items
    .map((item, index) => {
      const area = areas?.get(item.id) ?? ringArea(item.ring);
      return { item, index, tier: scenePaintTier(item.kind), area: Number.isFinite(area) ? area : 0 };
    })
    .sort((a, b) => a.tier - b.tier || b.area - a.area || a.index - b.index)
    .map(entry => entry.item);
}

/** The object ids under a point, topmost first, one entry per id. `elements` is document.elementsFromPoint(x, y) (topmost first).
 *  Only [data-object-id] elements inside `root` (the scene svg) count. Paths with pointer-events none, such as the selected-outline
 *  echo, are not in that list, and the echo has no data-object-id anyway. */
export function sceneObjectStack(elements: Iterable<Element>, root: Pick<Node, "contains">): string[] {
  const ids: string[] = [];
  for (const element of elements) {
    if (!root.contains(element)) continue;
    const id = element.getAttribute("data-object-id");
    if (id && !ids.includes(id)) ids.push(id);
  }
  return ids;
}

/** Alt/Option-click target: the object beneath the selected one in the topmost-first stack, wrapping round to the top. If the
 *  selected object is not under the pointer, the target is the object beneath the topmost one. With one object under the pointer,
 *  the target is that object. Returns undefined for an empty stack. */
export function sceneObjectBeneath(stack: readonly string[], selected: string | null): string | undefined {
  if (!stack.length) return undefined;
  const at = selected === null ? -1 : stack.indexOf(selected);
  return at < 0 ? stack[Math.min(1, stack.length - 1)] : stack[(at + 1) % stack.length];
}
