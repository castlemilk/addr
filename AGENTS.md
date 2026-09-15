<!-- BEGIN:nextjs-agent-rules -->

# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` (resolved from this file's directory; in monorepos the `next` package may not be visible from the repo root) before writing any code. Heed deprecation notices.

This block is written and re-added by `next dev` — verify at `node_modules/next/dist/server/lib/generate-agent-files.js`. Removing it from a diff only re-creates the uncommitted change; committing it with your work keeps the tree clean.

<!-- END:nextjs-agent-rules -->

## Project-specific agent notes

For any work on the 3D building/roof pipeline, AI tiles, per-address extraction,
MapLibre roof meshes, or the Deep 3D Studio, load the `addr-3d` skill
(`.opencode/skills/addr-3d/SKILL.md`). It documents the pipeline commands,
boundary-accuracy knobs, the shared roof-geometry module, and the critical
gotchas (MapLibre worker assets, `defaultProjectionData.mainMatrix`, stale
`next start` chunk 500s, `task stop` vs non-existent `stop:web`).

Key verification commands: `task test`, `task lint`, `task verify:ui`.
