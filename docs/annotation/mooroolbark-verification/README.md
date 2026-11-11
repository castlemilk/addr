# Mooroolbark migration verification

The final frozen snapshot is `6d8811d04524eeb196c654ac3647fb32f11d14813f4ff58eb1fb5a747f238572`. Unprefixed data parity reports cover the isolated local API fixture checked before browser edits. Files prefixed `production-` cover the live Cloudflare/Neon deployment. Source upload, production document comparison, incremental sync, complete cloud download, exact restoration and restored analysis replay have all passed.

## Production

- [Complete parity summary](production-parity-summary.json): final deployment, source/document totals and linked evidence.
- [Import](production-import-summary.json): all 518 documents created; all 10,027 assets accounted for across the resumed uploads.
- [Repeat import](production-reimport-summary.json): zero documents or assets created on an identical rerun.
- [Verification](production-verify-summary.json): all 518 documents and all 10,027 asset digests/lengths match the frozen bundle.
- [Independent HTTP comparison](production-http-semantic-verification.json): all items exactly match the plan, with geometry, holes, statuses and provenance preserved; original example projects unchanged.
- [Incremental sync](production-incremental-sync.json): exact frozen-plan document parity, 507 items, 10 labels, 43,316 annotations; 518 changes through global cursor `535`, followed by a no-op repeat.
- [Fresh cloud pull](production-pull-summary.json), [restore](production-restore-summary.json), and [independent file comparison](production-restore-file-verification.json): all 10,027 assets downloaded without local reuse; all 14,330 source files recovered with exact SHA-256, sizes and nanosecond timestamps.
- [History replay](production-state-history-verification.json): 518 initial create events independently reproduce the exact current documents at cursor `535`.
- [Restored analysis](production-restored-analysis.json): cloud-restored reader and data produce the exact canonical JSON and SHA-256, including 10,339 roofs across 257 tiles.
- [Live browser](../mooroolbark-production-browser.json): phone overview and 10,339-roof map, evidence filtering, exact-object navigation, source imagery, roof lines and 3D; no production dataset edits.
- [Final runtime and protocol fix](production-protocol-fix-verification.json): Worker `eb12acbb`, Container v7; all 64 production reads pass without retries after correcting the pooled PostgreSQL protocol exchange.
- [Database error diagnosis](production-read-error-diagnosis.json), [local pooled-read comparison](neon-pool-fix-proof.json), and [isolated Neon integration](neon-pool-fix-integration.txt): original failures recorded, 96/96 fixed local reads, and successful write/revision/persistence checks.
- [UI deployment](production-final-ui-deployment.json), [preceding Go rollout](production-label-cache-deployment.json), and [asset/runtime checks](production-deployment.json): deployment history and authenticated storage checks.
- [Protocol diagnostics](production-protocol-diagnostics.json): successful REST and Connect comparisons; the large response measurement led to smaller queue and client pages.
- [64 MiB gzip canary](production-gzip-canary.json): compressed upload and full original-byte download agree on SHA-256 and size.

## Local source and application verification

- [Planned scope](plan-summary.json): 506 scenes, 1 report, 10 labels, 43,316 annotations, 14,330 preserved files and 10,026 deduplicated source blobs.
- [Import](import-summary.json), [identical repeat](reimport-summary.json), and [verification](verify-summary.json): 518 documents created once; the repeat created or uploaded nothing; all documents and 10,027 assets including the manifest verified.
- [Independent API and semantic checks](http-semantic-verification.json): complete project enumeration and every item GET match the plan; source geometry, holes, statuses and provenance remain intact. Existing machine-confirmed objects remain machine-derived.
- [State and history](state-history-verification.json): exact projected documents, 518 initial create events, checkpoint 518, and no duplicate history from the repeated import.
- [Full API pull](pull-summary.json), [restore](restore-summary.json), and [independent restored-file checks](restore-file-verification.json): all 14,330 files recovered with exact SHA256 hashes, sizes and nanosecond timestamps, with no missing or additional files.
- [Snapshot correction](snapshot-correction-verification.json): additions and changes from the superseded diagnostic snapshot. Only the final snapshot above is the migration source.
- [Restored analysis replay](../mooroolbark-restored-analysis.json): the restored reader and data reproduce the exact canonical 10,339-roof inventory across 257 tiles.
- [Mobile browser save](../mooroolbark-browser-save.json): a notes-only edit persists across reload while retaining all geometry and source metadata.
- [Incremental local sync](incremental-local-sync.json): all 507 items and 43,316 annotations replay into a checksummed mirror; a second sync is a no-op, and the compact CLI export exactly matches it.
- [Live feedback sync](../live-sync-verification.json): phone, MCP and direct API feedback reach a CLI mirror; stale writes fail, deletions replay, and temporary verification records are removed without changing original example documents.
- [MCP deployment](sync-deployment-verification.json): the deployed server exposes revision-checked MCP writes and the existing authenticated API.
- [Live deployment](deployment-verification.json): the container is active on the new image, the frontend matches the local build, database readiness succeeds, and private assets deny anonymous and untrusted-origin access. These checks used no workspace credential.

The bundle preserves 5,618,694,792 logical bytes using 3,725,222,450 bytes of unique source blobs. The application manifest adds one asset. Credentials, dependency installations, downloaded model weights, runtime processes, unrelated scene frames and disposable imagery cache are outside the documented snapshot scope. The manifest records every exclusion and sanitized database table.
