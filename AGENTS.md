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

For roof or scene-object polygon tracing, tightening outlines against aerial imagery, or refining
agent boundary edits from user feedback, also load `addr-roof-tracing`
(`.opencode/skills/addr-roof-tracing/SKILL.md`). It documents complete edge/corner
review and source-pixel MCP inspection, preview and draft saving.

Agents review Fieldwork items with `save_agent_review` (RPC `SaveAgentReview`), including projects ending in `-review`.
This appends an evidence-bound assessment to `item.agentReviews` with a server-assigned `agent:fieldwork:<reviewer>` actor.
Agent decisions and notes are separate from human item status and annotation overrides; they never set human checks or
training eligibility. Preserve human decisions and existing geometry. Ordinary agent saves/deletes in `-review` projects
remain refused; `task fieldwork:review:push` still only creates proposals. `task fieldwork:review:pull` exports human decisions
and agent assessments separately. Inspect original imagery before a visual verdict and record unresolved evidence explicitly.

To improve the tracing agent itself (skill, references, tool guidance) rather than one outline, use the Harness² loop in
`scripts/ml/harnessing/` (`task harness:cases|check|run|judge|report|promote`; `task harness:demo` runs offline).
Rollouts run on frozen case snapshots through the `addr-case` MCP server and never write scenes; held-out human outlines
reach only the judge. Adopted skill edits reach `.opencode/skills/addr-roof-tracing` only through a reviewed
`harness:promote` patch. `task harness:fieldwork` turns `-review` items and pulled human words into assessment cases
and posts adopted assessments back only as `SaveAgentReview` agent history (a plan unless `--live`).
The outer loop is split-disciplined: `harness:cycle` learns from train feedback, `harness:gate` admits a base change only
on repeated validation evidence (`harness:lineage`), and only a `harness:confirm`ed (test-split) version yields a skill
patch. Model-service outages abort runs without caching or recording anything; rerun the same command to resume.

Key verification commands: `task test`, `task lint`, `task verify:ui`.
