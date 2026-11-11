export type HistoryCommand = "undo" | "redo";

export function editorHistoryCommand(event: Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey" | "shiftKey" | "altKey" | "isComposing" | "repeat">): HistoryCommand | null {
  if (!(event.ctrlKey || event.metaKey) || event.altKey || event.isComposing || event.repeat) return null;
  const key = event.key.toLowerCase();
  if (key === "z") return event.shiftKey ? "redo" : "undo";
  if (key === "x" && !event.shiftKey) return "redo";
  return null;
}

export function keepsNativeHistory(target: EventTarget | null): boolean {
  return target instanceof Element && !!target.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="textbox"]');
}
