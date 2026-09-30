# r25 报表 worker A/B/C 只读诊断

范围：固定源码 `.tmp/stage9-build-source-r25-final-release`、合成主 48 月；独立完整核验报告 `.tmp/stage9-release-main-48-verified-r25.json` 已为 `complete`。下列 CPU、wall 和 SQLite 插桩数据仅用于定位，后台曾有并行负载，均非 500 ms 验收值。未改生产代码、数据库或固定源码。

## 实际调用与证据

| 路径 | 本次实测/静态边界 | 决定 |
| --- | --- | --- |
| `check_report_readiness` → `Reports._report(_issues_only=True)` → 年初/期末 `party_balance_rows` → 本月 `_party_delta` | 同一 `QueryReads.snapshot` 的两次 `read_open_contributions` 输入分别为 938、1003 个计算 ID，交集 937、并集 1004；实际 `_decode` 1004 次，约 3,059,132 字节。第二次只核未缓存的新 ID。`read_open_contributions` 对整批先验 anchor/body SHA、精确 source bindings、parents、事实及结果摘要，全部成功才 `stored.update`。 | 本月 1004 份真实报表归属依据是必要命中范围；目前没有 937 份重复物理解码或二次来源核验。不得再做「937 命中 proof 缓存」或跳正文的快路。 |
| `_report` cash/profit 完整行 | 已认证本月 party 所选开放凭证行在同快照以 `report_open_source_rows` 复用；`read_open_contributions` 再请求时命中上项缓存。`selected_open_line` 仍核对凭证行的账户、借贷、现金分类，当前 1995 次调用在 cProfile 中约 16 ms。 | 不存在可由再次缓存消除的大量结果重解码；若更改，必须维持凭证行和直接来源保护。 |
| `read_report_flow` 月根 | readiness 调用 23 次，但 `reads._report_snapshot_cache[("report_period_flow",period)]` 成功后复用，物理月根读取约 11 次。覆盖上一年期末与年内关账月，不能把调用次数当物理重复。 | 保留认证根/封签与月份覆盖。跨月共享 `QueryReads` 的完整重建方案已有负结果，不重试。 |
| readiness 后的 `Dashboard._position` | 同一 worker/snapshot 主 48 月 `_position` 无 `read_open_contributions`、`verify_selected_content`、`fact_versions`、`relations_many` 调用；期末 party balance 成功命中缓存。其 10 条 SQL 返回 413 行/44,059 字节、约 540,100 VM，主要为仍须全局拒绝的缺 typed 分类反查。 | 没有 readiness→position 的 1004 份开放贡献重复。分类全局损坏保护另属查询计划问题，本轮不缩范围。 |
| 分类事实正文 | `_report_classifications` 两次调用已有 `verified_report_classification_facts` 成功集合；第一批约 403 条，第二批约 411 条，只为新命中补核约 8 条。`reads.fact_versions` 仍为实际消费分类提供 typed 对象。 | raw 来源 proof 与 typed hydration 各有职责；`Store.facts` 5 次约 15 ms 为理论上限，并无已证明的安全净收益。 |

一次 readiness cProfile（`.tmp/stage9-r25-main48-report-readiness-profile.txt`）共 506,095 次调用、profiled 0.643 s：party balance 0.481 s、`_party_delta` 0.263 s、`read_open_contributions` 0.154 s、SQLite execute 595 次 0.128 s、`_report_classifications` 0.095 s、`read_report_flow` 0.090 s、`_verify_anchored_source_bytes` 0.062 s、1004 次 `_decode` 0.036 s。累计时间嵌套，**不可相加**。SQL 仪器的 readiness 总量为 595 条、26,168 返回行、11,236,166 返回值字节、约 1,056,300 VM；`_position` 另为 10 条、413 行、44,059 字节、约 540,100 VM。原始数据 `.tmp/stage9-r25-report-position-main48-overlap.json`；main12 对照 `.tmp/stage9-r25-report-position-main12-overlap.json`。

本月权威 `report_open_contribution` 共有 1,028 份、正文 3,146,866 字节，其中 `rows` 1,217,297、`resolution` 1,005,466、`source_bindings` 366,622、`parents` 117,423 字节（SQL JSON 子字段长度不应直接加总当作独立 I/O）。当前 worker 实际选中 1,004 份。`resolution`、bindings/parents 不是可任意丢的多余字段：party split、cash/profit、来源与直接依赖均消费；已撤回的行表/瘦身投影 AB 不因字段较大而重新成为方案。字段计数 `.tmp/stage9-r25-main48-contribution-field-bytes.json`。

## 入口职责矩阵

| 入口 | 当前读取责任 | 与本轮 worker 的关系/未验证 |
| --- | --- | --- |
| 五页准备、brief reports worker、`Periods.collect_current_readiness` | `check_report_readiness` 只取 `fact_issues`，仍由同一 `_report` 核未知科目、年初/期末 party、分类、现金/利润、跨表问题。 | 本次只动态覆盖 brief worker 的 2019-12 开放期；其他五页通过同一 readiness 注册器，未逐页插桩或重新跑损坏反例。 |
| CLI/MCP `Reports.report`、Dashboard季度报表、浏览器导出预览 | `_report(_issues_only=False)` 保留完整 statements、`report_fact_ids` 与已消费原文/来源核验；`preview_export` 走 `source="closed"`。 | 公开完整返回不能依 issues-only 缩掉历史元数据。该入口本次静态核对，未做完整响应 AB。 |
| 关账、close review | `Periods` 正式关账经完整关账完整性和 readiness；不得接受 worker 的跨事务 proof。 | 未在本次跑真实关账；继续沿原检查器/同事务边界。 |
| 完整核验、修复、备份/恢复 | `integrity.verify_integrity` 的 `_check_sources` 先独立核权威源，然后 `report_open_contribution_reader().compare_open_contributions(...verified_source=source)` 逐正式发布重建锚与正文；同事务 verified lease 供 report projection/semantics/flow 的 full comparer。`Maintenance` 维修仅派生 body，先/后完整核验。备份 `backup._verify_connection` 通过版本注册的 company verifier，不走普通页面缓存。 | 独立全范围重建和普通命中范围不同，不能把 full 的两次不同职责解析认作本页重复。此矩阵为源码路径核对；本次未重新完整核验或备份。 |
| 固定 v1 | `content_history_context` 以版本分派 `report_open_contribution_v1`、`report_flow_v1`、`report_projection_v1`；v1 comparer从已核 `verified_source` 独立重建，检查锚集合/摘要及正文集合/内容。 | 不让 current 的 `QueryReads` 快路渗入历史规则；本次未运行 offline-upgrade/v1 回归。 |

## 结论与下一组边界

本轮没有发现可支撑生产改动的 A/B/C 同请求重复贡献解码或跨层来源重验。最大的被测单体仍是真实本月 1004 份贡献的读取与所选来源证明；从 cProfile 看，`_decode` 本身约 36 ms，简单字段解析微调不会解释 48 月页面的全部超时。另一个值得归入查询形状组的是 `_position` 全局缺 typed 反查 VM 随历史增长，但它承担全局损坏拒绝，不能用仅命中目录证明无损坏。本轮不改生产，不重试已否决的新行表、party SQL 聚合、JSON 快路、跨月 QueryReads 或跨事务证明。
