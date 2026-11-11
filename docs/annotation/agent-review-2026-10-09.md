# Independent agent and human reviews — 9 October 2026

The hosted Fieldwork editor and addr MCP now support independent agent assessments alongside existing human decisions. Agents use `save_agent_review`; human reviewers use the editor's human review controls.

An assessment appends to `item.agentReviews` with a server-stamped `agent:fieldwork:<reviewer>` actor, decision, evidence notes, source/geometry/classification snapshot and original revision. Human status, geometry checks, notes and training eligibility are preserved. History is immutable through ordinary saves, including saves from older browsers that omit the new field. Later assessments supersede earlier assessments by the same agent for the same target without erasing them. Changed evidence is identified in the inspector and pull output.

The tool requires `itemId`, current parent `expectedRevision`, stable `reviewer`, `decision` and `notes`; `annotationId` optionally targets one outline. Decisions: `approved`, `rejected`, `needs_adjustment`, `uncertain`. An approved vegetation outline can include `vegetationType`. The endpoint rejects geometry, human flags, actor overrides and unknown arguments. Read-only OAuth connections cannot call it. Ordinary agent edits to `*-review` projects remain blocked.

## Deployment and verification

- Fieldwork Worker: `a8321dde-82fb-43c0-95f4-4a74aa5d0ed2`.
- Backend: running container version 9, image `sha256:b3ca27b3865058e6cc01d95c17488ba00fd889158de56246a4827e59a75dafe1`.
- MCP gateway: `6f8d2e5a-ee34-449e-9a7a-98d65abe8bd0` (18 editing tools).
- Readiness, live discovery and authenticated MCP saves/readbacks verified.
- Final live change cursor 1539 contains all 268 assessments attributed to `agent:fieldwork:codex`; pull verified every assessment's provenance and current evidence and exported them separately from human decisions.
- All Go tests, 133 Fieldwork pull tests, 76 Worker/gateway tests, TypeScript checks, lint, review UI tests and both deployment dry-runs passed.
- Isolated browser verification passed: MCP assessment roundtrip, independent human rejection, history retention, stale geometry indication, mobile inspector and no page errors.
- The full Python suite had 2,586 passes, one skip and three failures outside the changed modules: real-repository timing assertions in `test_eyes_report` and `test_reference_registry`, and a macOS recursive-mutex abort during ONNX export in `test_training_validate_local`.
- `task verify:ui` hit depth-service 502s and a Chrome screenshot timeout. The isolated Fieldwork browser verification passed.

## Saved visual assessments

Original imagery was read through addr MCP and compared with the saved canonical geometry. All 268 requested items now have an independent agent assessment. Every save was read back and verified to preserve all human fields, source references and geometry. Human answer text stayed byte-identical; the other human pull outputs stayed semantically identical apart from document/pull revision and cursor metadata. Human feedback sentences retain the human save revision.

| Batch | Items | Approved | Needs adjustment | Uncertain | Rejected |
| --- | ---: | ---: | ---: | ---: | ---: |
| Shadows | 70 | 29 | 11 | 30 | 0 |
| Cuts | 116 | 33 | 33 | 50 | 0 |
| Vegetation | 52 | 0 | 28 | 24 | 0 |
| Joins | 27 | 5 | 12 | 9 | 1 |
| Round 5 | 3 | 3 | 0 | 0 | 0 |
| **Total** | **268** | **70** | **84** | **113** | **1** |

Vegetation entries are scene-level type assessments, with specific outline findings; they do not approve all 1,808 outlines or establish precise geometry, height or tree count. Round 5 approvals support the recommendations, including retaining a candidate without promoting weights; they do not execute training or labelling. Occluded or unresolved geometry remains uncertain.

The [complete review report](../../data/reviews/eyes/fieldwork-agent-review-2026-10-09/README.md) links every saved assessment, source binding and compressed before/after proof. Existing human approvals and earlier proposal hints were not relabelled as new agent reviews.
