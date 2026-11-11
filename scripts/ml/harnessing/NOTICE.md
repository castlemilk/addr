# Third-party notice

Portions of this package are adapted from Harness² — Recursive Agent Harnessing for an Open World
(https://github.com/google-research/harness2, commit 1cfe254), Copyright 2026 Google LLC, licensed
under the Apache License, Version 2.0; the license text is in `LICENSE-harness2`.

Each adapted module says so in its docstring and was modified for addr:

| addr module | Harness² source |
|---|---|
| `surface.py` | `harness2/core/tree.py` |
| `spec.py` | `harness2/core/spec.py` |
| `context.py` | `harness2/core/context.py` |
| `library.py` | `harness2/core/library.py` |
| `ledger.py` | `harness2/core/ledger.py` |
| `channels.py` | `harness2/core/proposer.py` |
| `prompts.py` | `harness2/core/prompts.py` |
| `recursion.py` | `harness2/core/recursion.py` |
| `selection.py` | `harness2/core/selection.py` |
| `schedule.py` | `harness2/core/schedule.py` |

Changes: the editable surface follows the addr skill layout (references, scripts and agent metadata
travel with a skill; hooks use the Claude Code schema); improver evidence carries overlay images of
submitted outlines; prompts address outline tracing and detection review on aerial imagery; the
benchmarks, substrates and model bridge are replaced by the addr tracing adapter, the offline case
server and the agent-CLI executors; the judge measures against held-out human reviews.
