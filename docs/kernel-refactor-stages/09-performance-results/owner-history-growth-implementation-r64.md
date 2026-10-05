# r64 历史增长：处理范围与实际结果

本组由 r63 主 48 月整页失败触发。五页均排查；证据来自相同固定源码的 12／48 月工作量、实际调用链和主 48 月带真实绑定参数的执行计划。计划原件为 `.tmp/stage9-owner-plans-r64.json`；额外 EXPLAIN 工作不作为原基线或时延验收，未保存参数值。r64 已完成固定源码的 276 项受影响回归和候选构建；独立审查发现的 metadata 混合批次发布边界缺口已在 r65 修复，并建立固定候选。r65 主 48 月 native／工作量／profile 仅作诊断，简报、员工和报表仍超过 500ms；第 9 阶段老板看板性能仍未完成，不代表正式 v1 交付。r66 正在修改，尚未统一回归或测量性能。

| 根因 | 实际问题、调用方与处理决定 | 保留或未解决的边界 |
| --- | --- | --- |
| 全历史候选交给每一个历史月 | 简报／资金／资产的 `BusinessQueries._selected_accounting` 将同类全部主体交给每个关闭月，subject membership 探测随历史超线性增长。由实际发布和实际凭证月份建立保守月范围，公共读取保留全候选的独立发布／凭证 authority，核对各月完整命中。 | 空范围也核对独立 authority；无影响复核、冲正、无行状态不能漏；固定 v1 和非当前 reader 保留原路径。 |
| 精确 ID 查询仍从大角色集合出发 | 员工 `current_role_matches` 实际计划选择 `entity_current_role(role=?)`，主 48 月该语句约 93,700 VM。改由请求 fact IDs 定位；所有 current／recorded 同类引用查询一并检查。 | 对源正文、全部角色及精确纠错链的核验继续；不能只留下每人的最新工资，或把 identity proof 记为全文 proof。 |
| 同一消费重复取得、解释结果正文 | 资产采用选择为状态／行数严格核验 owner 正文，成员读取随后再取得并解释同一 owner；`verify_outcome_bytes` 后又完整 `digest(outcome)`。本调用局部携带经过同一严格 decoder 的 owner 对象，metadata／正文一次取得，复用既有结果摘要比较。 | 不增跨请求缓存。整批成员、独立摘要、序号、封签、依赖、外 owner、行范围继续核验。固定 v1 不使用新私有携带。混合 reusable／fresh metadata 批次的发布边界已在 r65 修复，两种方向的真实存储负例覆盖原缺口。 |
| 少量结果仍需历史目录完整性 | 员工历史身份／冻结叶、清偿最新根的全未结目录、报表冻结准备来源有实际历史增长。索引命中不等于可省正文。 | 暂保留。报表只需少量 profile 却先展开历史 readiness 是候选，尚无等价 absence／membership 证明，不删除必要来源。工作量下降和五页验收仍待本组固定后测量。 |

上下文、有效月度核对、默认明细和按需详情已沿同类调用链排查；未给出新的独立时延结果。CLI／MCP、关账、完整核验、修复、备份恢复和固定 v1 的公共对应路径按各自完整核验职责保留。当前范围不改结构、公开响应或冻结内容，不重试审查第 24 项、已回退方案或跨请求业务缓存。

| 实际定向回执 | 结果与证据边界 |
| --- | --- |
| identity directed | 45 passed，184.23 秒。精确 current／recorded 引用计划均按请求 fact ID 定位；加入无关引用后 current 身份读取为 2,300→2,400 采样 VM、144 行不变，开放／关闭工资身份均保持 200 采样 VM 及相同返回量。身份 proof 与全文 proof 的差别保留。只有日志，未提供本轮 JUnit XML。 |
| accounting scopes first／second | first 为 1 failed、13 passed、1 warning，21.96 秒：新增开放期修订／移月测试读取结果字段错误，`KeyError: subject_id`。second 为 1 failed、36 passed、1 warning，48.16 秒：现有测试包装 lambda 未接新 `subjects_by_period` 参数。失败原件保留。 |
| accounting scopes final／cache-final | final 为 67 passed、2 warnings，83.27 秒；其后缓存边界补齐的 cache-final 为 17 passed、2 warnings，35.04 秒。两次 record_property/xunit2 警告保留，不累加为 84 项统一固定回归。 |
| asset owner directed／frozen／diagnostic／complete／final | directed 为 15 passed，24.14 秒；新冻结 owner 用例在 frozen、frozen-diagnostic 均 1 failed，分别 5.36／5.33 秒。complete 为 1 failed、4 passed，9.42 秒：state 与 voucher 路径对同一 owner 两次 inline decode，组合与独立路径均读取 3,172 bytes，收益断言失败，业务语义一致。补齐本调用已成功 state 元数据的复用范围后，final 为 5 passed，9.74 秒；冻结 owner 正文行数及 decode 2→1、bytes 3,172→1,586。开放期 1 月组合／独立读取正文 2／4 行、2,712／5,424 bytes；3 月为 4／8 行、6,565／13,130 bytes。更强正文 cache 的保留不是这次已确认冻结收益的根因；先前失败原件保持。 |
| 固定源码 required group | 276 passed、9 warnings，pytest 608.15 秒；JUnit 为 276 tests、0 failures、0 errors、0 skipped，607.931 秒。wrapper 为 609.7062377999828 秒、exit_code 0、source_unchanged true、changed_files 为空；前后源码 SHA-256 均为 `d54cf875a20b7333b0f63e5cc27281542c5e1851045d6e8a0aa04cd69e47b353`。这是本轮列明目标的统一回归，不是全部测试或后续修复源码的验证。 |
| r64 候选构建 | 日志记录 released contracts created／verified；provenance 的隔离发布快照 SHA-256 为 `59107d8719c95f12d71a433c2dc7ca2e2fab35cd266a7fc8d8d6e7f319d6766c`。候选路径为 `.tmp/stage9-build-candidate-owner-r64`，快照为 `.tmp/stage9-build-source-owner-r64-release`。构建先于独立审查发现的批次边界修复，只记录历史，不作为最终当前候选或性能达标依据。 |

执行计划保留 159／107／63／116／117 条五页唯一 SELECT。实际表名是 `entity_reference_current`；r63 员工 `role + fact_id` 查询选择 `entity_current_role(role=?)`，是本组精确 ID 优化的实际依据。历史工资头与资产 owner 选择仍有 ORDER BY／DISTINCT 临时 B-tree；结算根、family subroot、报表 checkpoint seal 使用现有索引。未观察到 report checkpoint payload 查询；大量 SCAN 来自 JSON 虚拟表或中间集合，不能据数量断言源表全扫。计划仅保存每页首次相同 SQL／参数数量组合的实际绑定计划，不保留 family 参数值，不能区分同一 SQL 后续 accounting／settlement family 实参。

独立审查发现：在同一 metadata 批次混合已有完整正文证明的 reusable owner 与 fresh 正文时，原 private reusable 递归先把可复用部分写入缓存，再验证 fresh 部分。后者失败会留下半批 metadata，违反该调用整批成功后发布的边界；这不等同于坏正文已被认证。r65 已修复该边界，定向回归为 12 passed、21.51 秒，范围为 7 项 owner 用例、asset batch 与固定 v1 warm cache。独立只读审查认为两个方向的真实存储负例覆盖原缺口，未发现新增缺陷；该结论不替代统一回归或性能验收。

r65 固定候选为 `.tmp/stage9-build-candidate-owner-r65`，发布快照为 `.tmp/stage9-build-source-owner-r65-release`，源码／快照 SHA-256 为 `7e048d17c2a76f501d75bd8c33ce6e06c7eb2bb8733b5529456ae5a71f87a936`。主 48 月使用此前经 r63 完整核验的合成库；r65 当前源码的完整核验明确为 `not_run`。以下为同一诊断的 3 次 native 样本；另有工作量和 profile 原件，插桩时延不能替代 native 或浏览器验收。

| 操作 | native 样本范围（ms） | 当前证据边界 |
| --- | --- | --- |
| context | 9.783–11.980 | 上下文诊断，不是整页验收。 |
| brief | 515.768–561.676 | 超过 500ms。 |
| funds | 242.336–252.592 | native 诊断低于 500ms，尚无当前浏览器验收。 |
| employees | 885.730–1091.970 | 超过 500ms。 |
| assets | 288.568–297.784 | native 诊断低于 500ms，尚无当前浏览器验收。 |
| reports | 529.448–533.128 | 超过 500ms。 |

由于本轮 native 诊断仍超标，未运行昂贵的当前源码完整构造／完整核验和 30 次浏览器验收；不能将此前 r63 的 92／150 纯计时失败记为 r65 新验收结果。下一组 [r66 集中处理范围](owner-required-groups-r66.md)仍在开发，未统一回归、未测性能。慢样本、脚本失败及测试失败全部保留，最终验收口径不降低。

[r64 原件清单](owner-history-growth-r64-manifest.json)保留此前定向回执；独立的 [r64 统一回执清单](owner-r64-unified-manifest.json)保存已结束的 required-group JSON／日志／JUnit，以及历史候选 provenance／构建日志。[r65 诊断清单](owner-r65-diagnostic-manifest.json)保存固定候选、批次边界定向回归、主 48 月 native／工作量日志与 JSON，以及 context／五页的 6 份 profile。清单均保存逐文件原始／压缩 SHA-256、源 mtime、大小和解压字节等价结果；归档只读取已结束原件，不执行测试、测量或构建。
