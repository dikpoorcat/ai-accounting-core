# 闭月 accounting authority 同请求批读

固定 r32 的主 12、48 月合成库已各自完成完整核验。逐月 `read_accounting` 原本为相同的 `subjects` 和一组已选闭月各查一次发布依据、一次当前凭证依据；主 48 月的简报分支分别执行 141 次。私有 AB 只合并这两类 authority 查询，仍运行原完整 native 简报和资产读取并比较每期原始行多重集。它是并行负载下的 SQL 工作量诊断，不是浏览器纯计时，也不证明整页净收益。

| 入口／范围 | 发布 VM 原→批 | 凭证 VM 原→批 | 两类返回行原／批 | 决定 |
| --- | ---: | ---: | ---: | --- |
| 主 12 简报 | 5.1k→2.2k | 6.2k→1.1k | 45/45、22/22 | 同期行多重集一致 |
| 主 12 资产 | 5.1k→2.2k | 7.5k→1.6k | 44/44、43/43 | 同期行多重集一致 |
| 主 48 简报 | 69.2k→9.1k | 97.6k→4.3k | 189/189、94/94 | 同期行多重集和期内顺序一致 |
| 主 48 资产 | 68.7k→9.1k | 119.9k→6.5k | 188/188、187/187 | 同期行多重集和期内顺序一致 |

主 48 简报这两段 SQL 的诊断时间为 5.28→1.15 ms、12.01→0.57 ms；批结果额外携带期间，返回字节略多。生产选择器在独立合成 SQL 工作量测试中，12/48/120 个期间的进度回调次数（每回调约 100 SQLite VM 指令）为 1220/3411/7790。这项 120 月测试仅验证选择器的增长，不代表已完成 120 月整库验收。

## 生产职责与边界

| 调用方 | 本次处理 | 保留的独立证明 |
| --- | --- | --- |
| `BusinessQueries._selected_accounting`，含简报、资金、资产及其 CLI/MCP 共用入口 | 已选出闭月和精确 `subjects` 后，交同一 `QueryReads` 快照批读 | 原已认证闭月候选、业务筛选、发表和凭证完整集合比较 |
| `QueryReads.close_accounting_many` | current reader 整组暂存；固定 v1 reader 沿原顺序逐月读 | 每个 header 自己的认证；current 全部成功才发布新增 header、slice、parts 和 filter 位置缓存。v1 保留原逐月成功证明，不改历史规则 |
| `close_storage.read_accounting_many` | 在同连接、同 `subjects`、精确 `(period, storage_digest, publication_sequence)` 范围各取一次发表和凭证 authority | 每月继续原 `read_accounting` 的 family、filter、目录、块、成员、摘要、高水位与来源比较；空范围及单月入口仍按原合同 |
| 关账、完整核验、修复、备份恢复、固定 v1 的独立 reader | 本次未改其调用路径 | 原独立证明与冻结 v1 规则不变 |

批包是模块内部一次性结果，绑定同一 SQLite connection、subjects 和各月根身份，不对 HTTP、CLI 或 MCP 开放“跳过证明”参数。current 批处理失败不记本批的新成功前缀；请求此前已经成功核过的部分仍属于原快照缓存。固定 v1 fallback 沿原逐月入口读取，后月失败会抛错，前月已通过的证明仍按原规则存在；失败月不缓存，不能把 current 的整组缓存行为推广为 v1 的新保证。预先认证本组全部 header 可能在多个同时损坏时改变首先报告的错误，不能宣称首错顺序完全相同。

定向回归覆盖真实三个月关账的批读／串行 `BusinessQueries._selected_accounting` 同形输出，以及发表来源删除、额外当前凭证、第二月 filter 损坏、冻结成员正文损坏时的拒绝和失败不发布。命令：`.\.tmp-kernel-venv\Scripts\python.exe -m pytest -q tests/kernel/test_close_accounting_filter.py tests/kernel/test_query_reads.py tests/kernel/test_t7_snapshot_reuse.py`，结果 **67 passed**；Ruff 对这一个测试文件和 `close_storage.py`、`query_reads.py`、`business_queries.py` 检查通过。受影响测试文件仅 `tests/kernel/test_close_accounting_filter.py`；统一组回归另由阶段验收执行。

原始私有 AB、首次主 48 记录、补齐期内顺序的主 48 r2、脚本和增长计数均以 gzip 原字节归档；每件原文与压缩件 SHA-256 见 [归档清单](accounting-authority-batch-r32-archive.json)。首次主 48 记录未包含期内顺序字段，r2 补齐，原始记录没有覆盖或丢弃。
