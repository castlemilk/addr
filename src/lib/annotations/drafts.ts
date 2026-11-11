import type { Item } from "./model";

export interface LocalDraft { item: Item; baseRevision: number; savedAt: string }
const prefix = "fieldwork.draft.v1.";
function draftKey(id: string) {
  const server = localStorage.getItem("fieldwork.api.url") || window.location.origin;
  return prefix + encodeURIComponent(server.replace(/\/$/, "")) + "." + id;
}
export function readDraft(id: string): LocalDraft | null {
  try {
    const raw = localStorage.getItem(draftKey(id));
    if (!raw) return null;
    const value = JSON.parse(raw) as LocalDraft;
    if (value.item?.id !== id || !Array.isArray(value.item.annotations) || !Number.isFinite(value.baseRevision)) return null;
    return value;
  } catch { return null; }
}
export function writeDraft(item: Item, baseRevision: number) {
  localStorage.setItem(draftKey(item.id), JSON.stringify({ item, baseRevision, savedAt: new Date().toISOString() }));
}
export function removeDraft(id: string) { localStorage.removeItem(draftKey(id)); }
/** A clean undo must remove its former draft; an offered recovery copy stays intact. */
export function reconcileDraft(item: Item, dirty: boolean, recoveryPending: boolean) {
  if (recoveryPending) return;
  if (dirty) writeDraft(item, item.revision);
  else removeDraft(item.id);
}
export function downloadJson(value: unknown, name: string) {
  const blob = new Blob([JSON.stringify(value, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url; link.download = name; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
