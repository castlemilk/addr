"use client";
import type { ReactNode } from "react";
import { WorkbenchNav, type WorkbenchNavProps } from "./WorkbenchNav";

/**
 * Header row for the navy workspaces: the workspace nav at the left and the page's own
 * actions at the right. There is deliberately no title row: the object of work (address,
 * scene, suburb) stays the page's only h1, rendered by the page itself.
 */
export function WorkbenchHeader({ actions, children, ...nav }: WorkbenchNavProps & { actions?: ReactNode; children?: ReactNode }) {
  return (
    <header className="review-header workbench-header">
      <WorkbenchNav {...nav} />
      {children}
      {actions ? <div className="review-header-actions">{actions}</div> : null}
    </header>
  );
}
