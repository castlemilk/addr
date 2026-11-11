"use client";

import { useEffect, useEffectEvent } from "react";
import { editorHistoryCommand, keepsNativeHistory } from "@/lib/editor-history-shortcuts";

/** Only the mounted, editable roof workspace owns geometry history. */
export function useEditorHistoryShortcuts(active: boolean, ready: boolean, undo: () => void, redo: () => void) {
  const run = useEffectEvent((command: "undo" | "redo") => {
    if (ready) (command === "undo" ? undo : redo)();
  });
  useEffect(() => {
    if (!active) return;
    const keydown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || keepsNativeHistory(event.target)) return;
      const command = editorHistoryCommand(event);
      if (!command) return;
      // Consume empty/busy history too: never fall through to browser actions.
      event.preventDefault();
      run(command);
    };
    // Desktop webviews may dispatch their native Edit > Cut command without
    // a keydown. The requested X shortcut still means redo on the roof canvas.
    // preventDefault on keydown suppresses cut, so ordinary browsers run once.
    const cut = (event: ClipboardEvent) => {
      if (event.defaultPrevented || keepsNativeHistory(event.target) || keepsNativeHistory(document.activeElement)) return;
      event.preventDefault();
      run("redo");
    };
    window.addEventListener("keydown", keydown);
    window.addEventListener("cut", cut);
    return () => {window.removeEventListener("keydown", keydown);window.removeEventListener("cut", cut);};
  }, [active]);
}
