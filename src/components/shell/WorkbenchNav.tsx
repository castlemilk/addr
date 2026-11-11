"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Layers3 } from "lucide-react";
import type { MouseEvent } from "react";
import "./shell.css";

export type Workspace = "map" | "scenes" | "queues" | "processing" | "training";

/** The five workspaces, in nav order. Labels are sentence case and name the job, not the system. */
export const WORKSPACES: ReadonlyArray<{ key: Workspace; label: string; href: string }> = [
  { key: "map", label: "Map", href: "/" },
  { key: "scenes", label: "Scene review", href: "/scenes" },
  { key: "queues", label: "Review queues", href: "/queues" },
  { key: "processing", label: "Processing", href: "/scenes/processing" },
  { key: "training", label: "Training", href: "/scenes/training" },
];

/** Which workspace a route belongs to. Roof review, candidates and training coverage are one workspace. */
export function workspaceForPath(pathname: string | null | undefined): Workspace {
  const path = (pathname || "/").replace(/\/+$/, "") || "/";
  if (path === "/") return "map";
  if (path.startsWith("/queues")) return "queues";
  if (path.startsWith("/scenes/processing")) return "processing";
  if (path.startsWith("/scenes/training") || path.startsWith("/scenes/candidates") || path.startsWith("/reviews")) return "training";
  if (path.startsWith("/scenes")) return "scenes";
  return "map";
}

export type WorkbenchNavProps = {
  /** Per-workspace href overrides, e.g. a Map link that returns to the captured location. */
  hrefs?: Partial<Record<Workspace, string>>;
  /** Pages with drafts pass their existing guard so leaving is blocked with the page's own message. */
  onNavigate?: (event: MouseEvent<HTMLAnchorElement>) => void;
  tone?: "dark" | "light";
  /** Override the workspace derived from the pathname. */
  current?: Workspace;
};

export function WorkbenchNav({ hrefs = {}, onNavigate, tone = "dark", current }: WorkbenchNavProps) {
  const pathname = usePathname();
  const active = current ?? workspaceForPath(pathname);
  return (
    <nav aria-label="Workspaces" className={`workbench-nav workbench-nav-${tone}`}>
      <span className="workbench-wordmark"><Layers3 size={18} aria-hidden="true" />GeoAU</span>
      {WORKSPACES.map(workspace => (
        <Link
          key={workspace.key}
          className="workbench-link"
          href={hrefs[workspace.key] ?? workspace.href}
          aria-current={active === workspace.key ? "page" : undefined}
          onClick={onNavigate}
        >
          {workspace.label}
        </Link>
      ))}
    </nav>
  );
}
