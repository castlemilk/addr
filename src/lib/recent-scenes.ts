/**
 * Recently opened scenes for the /scenes hub.
 *
 * A short, newest-first list kept in localStorage. It is a convenience for finding the
 * way back to a scene; it is not review state, never affects saved feedback, and any
 * corrupt or foreign value under the key simply reads as an empty list.
 */
export const RECENT_SCENES_KEY = "addr.scenes.recent";
export const RECENT_SCENES_LIMIT = 8;

export type RecentScene = { id: string; title: string; opened: string };

/** The subset of Storage this module needs, so tests can pass a plain object. */
export type RecentSceneStorage = Pick<Storage, "getItem" | "setItem">;

const browserStorage = (): RecentSceneStorage | null => {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
};

const isRecentScene = (value: unknown): value is RecentScene =>
  typeof value === "object" && value !== null &&
  typeof (value as RecentScene).id === "string" && (value as RecentScene).id.trim() !== "" &&
  typeof (value as RecentScene).title === "string";

/** Newest first. Corrupt JSON, non-arrays and malformed entries are dropped rather than thrown. */
export function readRecentScenes(storage: RecentSceneStorage | null | undefined = browserStorage()): RecentScene[] {
  if (!storage) return [];
  let raw: string | null;
  try {
    raw = storage.getItem(RECENT_SCENES_KEY);
  } catch {
    return [];
  }
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(isRecentScene).map(entry => ({
      id: entry.id, title: entry.title, opened: typeof entry.opened === "string" ? entry.opened : "",
    })).slice(0, RECENT_SCENES_LIMIT);
  } catch {
    return [];
  }
}

/** Record that a scene was opened in the editor. Returns the updated list so callers can set state directly. */
export function recordRecentScene(
  id: string, title: string,
  storage: RecentSceneStorage | null | undefined = browserStorage(),
  now: () => string = () => new Date().toISOString(),
): RecentScene[] {
  const existing = readRecentScenes(storage);
  if (typeof id !== "string" || id.trim() === "") return existing;
  const entry: RecentScene = { id, title: typeof title === "string" && title.trim() ? title : id, opened: now() };
  const next = [entry, ...existing.filter(scene => scene.id !== id)].slice(0, RECENT_SCENES_LIMIT);
  if (storage) {
    try {
      storage.setItem(RECENT_SCENES_KEY, JSON.stringify(next));
    } catch {
      // Quota or private-mode failures only lose the convenience list.
    }
  }
  return next;
}
