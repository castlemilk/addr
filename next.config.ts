import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  serverExternalPackages: ["node:sqlite"],
  // The repo root carries tens of GB of imagery, model weights and Python environments that the server-output file
  // tracer must never crawl: a build on 2026-09-29 grew past 90 GB of memory and stalled the machine before this
  // exclusion existed.
  outputFileTracingExcludes: {
    "*": ["./data/**", "./scripts/**", "./deploy/**", "./docs/**", "./bin/**", "./gen/**", "./.pnpm-store/**", "./.run/**", "./.venv/**"],
  },
};

export default nextConfig;
