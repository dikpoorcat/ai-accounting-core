# Stage 9: assets readiness on the existing materials worker (private evaluation)

This is a diagnostic proposal, not a production change or pure browser timing. The source is the sealed r29 reader (`stage9-build-source-r29-release`); the synthetic main48 book was independently verified with the sealed r28 reader. The source's company/content contracts are unchanged. All probes are in `.tmp`, and no source or book was modified.

## Exact work and current callers

The ordinary complete, unfiltered dashboard brief with a period uses `LocalService._dashboard_brief` and the existing three-worker group. `Dashboard.brief` starts the group under `BriefReadGuard`, then its parent snapshot computes journal, funds, workforce, long-term assets, open items, and the position settlement input. `snap.preparation` calls `BusinessQueries._period_readiness` → `Periods.check_readiness` → `collect_current_readiness`. That collector takes one union of all readiness `Read` specifications, calls `QueryReads.prime_select`, evaluates each readiness rule, attaches `work_area`, and records exact `Context.trace()` fact/calculation IDs. Its materials packet, duplicate packet, report issues, and position are already worker results; the returned readiness trace feeds `current_followups.close_requirements`, not just a screen issue count.

Only this eligible brief parallel path is a candidate. `Dashboard.period_preparation` and `quarterly_report`, worklist, close review, `Periods.close`, CLI/MCP, full integrity, repair and backup continue through the original collector. Deferred, filtered/detail, closed-period, no-report-check, busy-pool and disabled-pool briefs also retain the serial rule. Fixed v1 is independent and unchanged.

The `assets_and_financing` readiness entry has 31 read specifications, 29 unique relative to the other current readiness entries. On main48 it traces 1,463 fact IDs and 1,415 calculation IDs; the facts are dominated by 1,128 historical `asset_consumption` facts. Its evaluator itself takes around 1ms; source selection and typed validation are the work. In a materials-worker snapshot, the existing completeness read took 285–353ms in two background-load probes and had 2,342 typed facts. Adding this asset entry required 1,460 new typed facts and 1,415 typed calculations, around 90–107ms CPU/wall. Even after priming *all other* readiness specifications first, it still loaded those 1,460 facts and 1,415 calculations. Thus there is almost no same-worker proof overlap: the proposal shifts necessary work, it does not remove it. A pickle of asset issues plus exact trace IDs was 192,432 bytes; it does not include typed objects or a `QueryReads` proof.

## Guarded design if pursued

The materials task would return an atomic internal packet containing the existing `MaterialReadSummary` and the complete `assets_and_financing` result calculated in the *same* `QueryReads.snapshot` and with the same `required_reads`, `Context`, evaluator, issue `work_area` annotation, and `Context.trace`. `Periods.collect_current_readiness` would remove this one entry from its parent union only for a live eligible `_parallel_checks` attempt, then insert the worker issues and exact trace into the original sorted readiness position. It must preserve material/accounting/close issue order, readiness status, and the full IDs, including empty selections. A single shared evaluator helper would prevent rule drift between serial and worker paths.

The packet remains one of the existing three read lanes. It is bound by the submitted company/path/period task and by the worker file-identity checks and `BriefReadGuard`'s autocommit `data_version` check around the whole group. The parent does not inject a worker `QueryReads` cache or claim its proof inside its own transaction; it only accepts the *complete group business result* after the guard succeeds. A concurrent write, worker failure/timeout, corrupt or absent packet, or partial group must discard everything and run the unchanged one-snapshot serial brief. An unchanged-source `KernelError` propagates as the current worker errors do. The materials result must be consumed before the parent skips its asset work; `finish()` must still require all three worker packets and existing position hand-off. The large materials packet and possible serialization wait need explicit tests; no new queue or fourth worker is required.

This differs from the rejected fourth-overview worker and the rejected move of funds to the duplicate worker: it uses the existing materials process, leaves `_funds` and the entire report/position lane where they are, and moves one internally named readiness check with its own exact source selection. The earlier negative results still caution that an extra concurrent read can slow the parent through CPU contention.

## Actual guarded full-response A/B

The private script `stage9-r29-asset-material-worker-ab.py` used the production service, real three-worker pool, and complete `Dashboard.brief` response. It only monkeypatched the materials task and the one collector branch in its process. Every A and B response matched byte-for-byte after removal of the actual `generated_at` timestamp, with the same response SHA. The original guard and serial fallback paths remained in place. The ABBA runs occurred while other synthetic work was active, so their wall times are diagnostic, not acceptance timing.

| Run | Mode | Parent wall ms | Parent CPU ms | Materials wall/CPU ms | Total parent+workers CPU ms | Parent readiness prime ms / new facts / new calculations |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| 1.1 | A | 906.6 | 890.6 | 319.4 / 312.5 | 2140.6 | not separately instrumented |
| 1.2 | B | 623.3 | 625.0 | 399.7 / 390.6 | 1875.0 | not separately instrumented |
| 1.3 | B | 645.9 | 656.3 | 427.3 / 421.9 | 2000.0 | not separately instrumented |
| 1.4 | A | 708.4 | 671.9 | 275.1 / 281.3 | 1828.1 | not separately instrumented |
| 2.1 | A | 776.3 | 750.0 | 250.2 / 250.0 | 1812.5 | 137.6 / 1979 / 1476 |
| 2.2 | B | 752.3 | 734.4 | 399.9 / 375.0 | 2078.1 | 75.3 / 524 / 61 |
| 2.3 | B | 882.1 | 812.5 | 618.8 / 500.0 | 2500.0 | 44.5 / 524 / 61 |
| 2.4 | A | 818.0 | 750.0 | 300.5 / 312.5 | 1984.4 | 165.0 / 1979 / 1476 |

In the second run, the parent reached the materials result at 512/563/740/575ms respectively and waited at most 0.03ms to receive it, so packet completion did not directly gate the parent in these trials. The collector itself fell from 182/199ms in A to 136/90ms in B, but the parent work before that point stretched in the slow B trial. The first ABBA favors B by roughly 173ms average parent wall; the second gives B roughly 20ms worse parent wall and 391ms more average total CPU. This is not a stable net benefit under the current shared-machine load. Source work was shifted accurately: A's parent bulk selection loaded 1,979 facts/1,476 calculations; B's parent loaded 524/61 while B's materials worker added 1,460/1,415. That is approximately the same total source population plus process separation and a 192KB result packet.

**Decision:** do not implement this in production from these results. The candidate has a sound bounded guard design and complete-response parity on the healthy main48 sample, but total resource cost and response improvement are not yet established. It cannot be claimed as a fix for the 48/120-month browser latency. If a later quiet window is available, rerun an identical fixed-source A/B with fault cases (asset issue/error, packet failure, concurrent commit, timeout, busy fallback, and closed/CLI serial paths) before adoption. Do not replace independent source validation with a passed trace list.

The original JSON evidence is archived beside this note as `asset-readiness-material-worker-r29-scope1.json`, `-scope2.json`, `-ab1.json`, and `-ab2.json`. The private prototype and probe scripts remain in `.tmp` and were never applied to production.
