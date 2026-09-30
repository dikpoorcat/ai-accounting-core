# r26 主 48 月完整核验内存范围审查（只读）

本记录仅据固定 r26 核验进程 PID 80596 的 OS 采样、当前对应调用链和既有 r25 证据，不改动或重跑合成库。采样文件为 `.tmp/stage9-r26-main48-verifier-memory.json`。01:34:52–01:35:45，WorkingSet 从 3,231,862,784 增至 3,660,001,280 字节；01:35:47 降至 2,516,807,680 字节，之后到 01:36:35 为 2,600,165,376 字节。观测峰值 3,678,621,696 字节。PrivateMemory 同时变动，说明这并非单纯别的进程占内存；但采样没有函数阶段、Python 对象、SQLite 或分配器标签，**不能据此断言某个缓存泄漏或把 1.14 GB 回落归因于某个函数**。工作集并非单调持续累积。

## 已证实的生命周期和集合

| 范围 | 代码与调用 | 单次核验内持有及释放边界 | 规模/判断 |
| --- | --- | --- | --- |
| 固定 v1 解码规则 | `content_v1.py:449-555` 的 `v1_registry()` 单元素 LRU，`verify_v1_company()` 经 `historical_content(1)` → `schema_bundle.py:132-143` → `integrity.verify_integrity()` | 进程级仅保存合同 registry/schema 模型；没有公司事实或月数据 | 与业务行数无关，不能解释 48 月增长 |
| 权威来源全集 | `integrity.py:254-365,405-500,1230` 的 `_check_sources`，`_check_facts` 在 `:150-200` | 一次载入所有历史事实版本及解码后的原文、所有 calculation 行及 `decoded` outcome、dependency 集合、publication、voucher 行及完整 lines；返回 `source` 后一直被 `_check_closes`、投影和目录阶段引用，直到 `verify_integrity` 返回。原件 BLOB 在 `_check_evidence` 校验后只留 digest 集合及数量，未见 BLOB 内容留在 `source` | 主 48 月报告为 121,593 facts、50,519 calculations、49,245 vouchers、337 evidence；集合基数随业务历史线性增长，单行正文/结果/凭证大小尚未量测。此为主要长期存活候选，不能直接称多余：关账采用和后续投影读取成功认证的同事务来源 |
| 关账全集 | `integrity.py:765-816` 首先将全部关账解码进 `closes`；`:840-896` 再构建材料清单、引用、证据、每月预期采用集合；`:1141-1144` 返回 `tuple(closes)`；`:1245-1349` 以 `decoded_closes` 交报表/目录/索引核验 | 47 个 manifest 在整个后半程与 `source` 并存，末次核验返回后释放；manifest 内含各月 voucher、adoption、readiness、覆盖等实际冻结内容 | 累计内容大小应按各月 manifest 实测，不可把 47 个 Python tuple 引用当 47 份正文。此前 r25 复用避免索引阶段再逐月完整解码，12 月 22→11 次；见阶段文档第 57 行和 `grouped-close-decode-r24.md` |
| 月度 owner review | `integrity.py:1110-1115` 每月 `closes[:count+1]`，`position_v1.py:118-141` 转 tuple 并校验 prefix；`position_v1.py:160-179` 为本月建立 `V1Reads`，`history_reads_v1.py:17-30` 有事实、计算、凭证等 dict cache | prefix 是 manifest **引用的浅复制**，调用完成可释放；`V1Reads` 属单次 `PositionInputsV1`，未见全月实例存到 `verify_integrity`。每月校验先前 close digest 的工作量可随月份成三角形，但未证实 N² 的长期驻留正文 | 47 月 prefix 最多累计 1,128 个浅引用构造；真实 CPU/临时峰值未单独测。不能仅因 `closes[:n]` 判定 GB 级缓存 |
| 报表及目录派生 | `integrity.py:1270-1320` 依次构造 `settlement`、`reports`、`semantics`、`flow_result`；`report_projection_v1.py:561-769` 的 `expected_rows`/actual_rows/party/checkpoint 全集及 `PreparedReport`，`report_semantics_v1.py:483-554` 的 expected/actual 全集与 prepared months，`report_flow_v1.py:258-427` 的 expected/actual，全在各函数比较期间；`report_classification_directory_v1.py:517-609` 的 expected/actual nodes 全集 | `require_report_projection(...,_return_verified=True)` 仅返回各月 `PreparedReport` tuple，完整 expected/actual 扁平数组在函数返回时失去局部引用；`semantics` 同理返回 prepared tuple。`flow_result['expected_rows']` 留在 `verify_integrity` 供 classification 比对。分类目录函数自身同时保存 expected_nodes 与 actual_nodes，返回后不由 `verify_integrity` 持有。各阶段的临时全表比较可能形成瞬时峰值，结果对象也与 `source`/`decoded_closes` 并存 | 分类 nodes 数和正文 bytes、报表各数组行数及对象峰值未测。这里是解释采样升降的候选，不等于已定位 3.66 GB 峰值；完整比对和冻结根必须保留正确性 |
| 读取快照 cache | `query_reads.py:294-341` 有大量 memo dict，但 `QueryReads.snapshot()` 在 `:346-370` 于 finally 调用 `_reset()`；v1 owner review 使用 `history_reads_v1.V1Reads`，不是把一个共享 `QueryReads` 留到全部 47 月 | 未发现 `verify_integrity` 跨月持有单个 `QueryReads.snapshot()`。v1 局部 `V1Reads` 随调用结束退出 | r25 前的跨月共用 `QueryReads` 私有负面 AB：12 月 VM 仅 -0.34%，Python 峰值约 75→217 MB，已撤回（阶段文档第 280 行）。不能用该原型解释当前 r26 |
| read-index 阶段 | `integrity.py:1332-1353` 经 `read_indexes.py:389-409,791-824`，以同事务 token 和 `decoded_closes` 构建按月 dict，再逐个 source 核验 | dict 值是已有 `(row, manifest)` 的引用；没有另造 47 个 manifest 内容；token 仅在 lease 内有效 | r25 复用减少解码/IO，但保存已解码全集至本阶段。若优化峰值，须仍维持同事务全部冻结来源/读目录检查，不能仅丢解码后跳过核验 |

## N² 与必要数据的区分

代码中可确认的 **重复遍历/浅 prefix**：owner review 每月校验到当月的关账头，CPU/读取工作量可能约 O(月数²)；prefix tuple 本身不持有 N² 份 manifest。`_check_sources` 的依赖图遍历有 `completed` 集合（`integrity.py:365-380`），不是把每个计算的完整祖先图长期保留。各月 manifest 可能本来就保存累计状态，是否使数据字节随月数超线性增长必须量实际 `close_storage_*` 的内容长度，不能从 `closes` 容器形状推断。

需保留的集合包括：所有权威事实/结果/凭证历史与其封签验证、全部冻结关账的原文/根、投影及目录的实际 vs 权威期望多重集。同事务复用不等于可跨事务缓存；不能为了内存从核验中删掉任何来源、历史采用、修复投影或孤儿目录检查。值得进一步量的只是同次比较结束后仍被外层局部变量持有的 *派生结果* 与源全集重叠多少，以及是否可流水比较；当前审查未证明安全的生产改法。

## 未测和后续最低成本诊断

当前采样不含阶段标签。若需判断 3.66 GB 峰值，下一次已授权的**独立合成**完整核验可在 `_check_sources` 返回、`_check_closes` 返回、各 `require_*` 返回、`verify_read_indexes` 返回附近记录同一 PID 的 RSS/PrivateMemory、`tracemalloc` current/peak 和集合 `len`/估算载荷；尤其记录 `source` 各子集合、47 个 manifest 的冻结正文 bytes、报表 PreparedReport/flow rows/classification nodes 的规模。不要在运行中的 r26 进程上注入或重复执行，也不要将 Python heap 峰值与 OS RSS 等同。仅有当前采样可确认“同次全来源与已解码关账/派生阶段存在重叠”和“峰值后显著回落”，尚不能将增长分摊给某一缓存、证明泄漏或证明 N² 内存。
