# 固定 r26 主 48 月：真实 HTTP 默认五页诊断

原始调用、cProfile top、分段和响应摘要在 `.tmp/stage9-r26-http-five-page-profile.json`；隔离脚本为 `.tmp/stage9-r26-http-five-page-profile.py`（由 r24 诊断脚本复制，改为固定 r26 source、已完整核验 r26 48 月报告和新输出路径）。只访问仓库 `.tmp` 合成根，固定源 `.tmp/stage9-build-source-r26-release`，报告 `.tmp/stage9-release-main-48-verified-r26.json`。包内登记公司版本 1，报告 `complete`/`verified`、48 月、每月 1,000 笔以及末月 2019-12 开放均在脚本前置断言中核对。通过真实 `ThreadingHTTPServer` GET `/api/dashboard/*`、`LocalService.dispatch(response_format=http_json)`，每路预热 1 次，cProfile 和只计分段各 1 次；所有请求 HTTP 200、dispatch `ok`，季度报表返回三张报表且业务状态 `in_progress`。脚本没有运行 `preview_close`。

| 默认页面路由 | 请求准备范围 | cProfile 响应字节 | cProfile dispatch / 只计分段 dispatch（ms） | 只计分段主页面 body（ms） |
| --- | --- | ---: | ---: | ---: |
| context | 公司上下文 | 10,317 | 106.8 / 72.7 | 62.6 |
| brief | `period=2019-12, preparation=complete, limit=100` | 882,716 | 991.2 / 748.4 | 704.4 |
| funds | `preparation=deferred, limit=100` | 348,414 | 433.4 / 389.9 | 360.4 |
| employees | `preparation=deferred, limit=100` | 168,114 | 549.7 / 444.3 | 420.9 |
| assets | `preparation=deferred, limit=100` | 190,766 | 559.8 / 393.5 | 369.5 |
| quarterly-report | 2019 Q4，`preparation=deferred` | 20,944 | 799.9 / 642.7 | 622.8 |

两种模式不是 AB 速度比较：cProfile 有明显插桩开销，且其他建库进程并行。响应 `dump_json` 为约 0.2–6.5 ms，HTTP `reply` 为约 0.3–0.7 ms；`validate_response` 为约 0.3–7.9 ms。此诊断下大部分服务时间确在页面计算/核验，不是 JSON 序列化或网络写回。客户端 Python JSON 解析约 0.3–10.5 ms，不代表浏览器 Ajv、Vue/DOM 或真实整页完成。父进程 cProfile **未覆盖简报 worker 进程的 CPU**；包含父进程等待时间，不能将其 inclusive 数当纯 CPU，也不能与纯浏览器 500 ms 目标直接比较。

## 调用范围与必要/待证工作

- **简报**：`dashboard.py:1577:brief` cProfile inclusive 530 ms，`business_queries.py:2725:_period_readiness` 279 ms，其 `periods.py:473:collect_current_readiness` 230 ms；`_selected_accounting` 6 次 189 ms、`QueryReads.close_accounting` 141 次 184 ms（这两个 inclusive 范围有重叠），资产计数 `_asset_card_sources` 131 ms。`complete` 准备检查和默认 100 条业务摘要是页面合同，不能改为 deferred 或减少内容来解释耗时。worker 结果和父进程需分开量，当前只给父进程与 HTTP 全调用范围。
- **资金**：`dashboard_funds.py:1202:funds` 386 ms；`account_summary` 107 ms，单次 `_selected_accounting` 103 ms，47 次 `close_accounting` 85 ms，账户 events 5 次 74 ms，分页 SQL 3 次约 58 ms。资金历史采用/余额必须验证；同一快照里是否可复用现有选择结果尚未做响应与来源集合 AB，不能凭 inclusive 重叠相加或删查询。
- **员工（B 类重点）**：`dashboard.py:2067:employees` 511 ms；`entity_references.current_role_matches` 1 次约 175 ms，其中 `verify_hits` 总计 2 次约 170 ms，`_expected_rows` 2 次约 149 ms，实际 `references_from_data` 2,500 次。`dashboard_reads.adopted_head_metadata` 2 次约 148 ms，工资头一调用约 146 ms；`_employees` 主体约 119 ms，清偿 `_scope` 约 97 ms。r26 新增的正向角色来源认证覆盖影响名单的命中，属于正确性必要工作，不能直接去掉。两次 `verify_hits` 具体 fact 集合交集、既有同快照成功证明可否复用尚未插桩；`adopted_head_metadata` 两次分别服务工资和本期个人劳务，范围不同，不可当重复查询简单合并。若成组优化，先记录每次 ID 集及认证 lease 后再做等价 AB。
- **资产（B 类重点）**：`dashboard.py:2120:assets` 514 ms；`_selected_accounting` 6 次约 205 ms，`close_accounting` 141 次约 153 ms，`_asset_card_sources` 149 ms，`_selected_asset_member_heads` 1 次约 156 ms，其下 `_selected_asset_owner_events` 5 次约 127 ms；清偿汇总 2 次约 104 ms。141 个 close accounting 调用按 `query_reads.py:1183` 的 `(period, frozenset(subjects))` 精确 cache key，对应 47 月与多个不同 subject scope，**不是已证明同一内容解码 141 次**；共享 verified parts/positions 已在读者里使用。完整卡片、批成员采用、取得/处置、清偿均是默认页来源。后续可先记录六次 selection 的 kinds/subject/period 与 cache hit、五次 owner event 的精确关系，再判断是否实际重叠，不能省去 member 完整性证明。
- **季度报表**：`dashboard.py:2336:quarterly_report` 763 ms，`reports._report` 513 ms，`party_balance_rows` 2 次 409 ms（`_party_balance_rows_uncached` 也 2 次约 281 ms），`_party_delta` 156 ms，分类 2 次约 83 ms，开放来源正文约 79 ms，`read_report_flow` 23 次约 67 ms。两次 party balance 未命中同一 snapshot cache；需先核对两个 cutoff/source key（`report_projection.py:786`）及季度前后口径，不能假定重复。三个月季度完整来源和归类仍需核验。返回仅 20,944 字节，瓶颈候选在来源/派生核验，不在响应大小。

另测 `close-review` 是没有 active preview 的普通开放月读取，约 39 ms dispatch、282 字节、状态另见原始 JSON；它**不是已准备的简报审查**，不能用作完整页时延替身。本轮与 r26 纯浏览器结果、主 48 月 full verifier 证据分开保留；本轮墙钟只供定位，不能宣称通过性能验收或净收益。
