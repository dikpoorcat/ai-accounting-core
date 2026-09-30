# 开放期候选仍扫描关闭历史：A 类补齐

触发：报告需要当前开放期的完整分类来源，历史分类多而开放月很少。`_report_references` 从 `subject.kind` 起步，逐个历史事实反查该月是否关闭；输出已精确限定，但扫描范围仍随历史增长。此前少数类型定位审查只确认了 SQL 有 kind／period 条件，没有测这个高基数分支；本次补齐该遗漏，不将其当成孤立优化。

只读主 12／48 月实测都返回相同 412 个 ID。原路径 VM 为 94,700／376,100；按事实索引定位实际开放月、分类按月读取、少数档案类型沿类型读取的私有路径为约 19,900／22,200。完整执行计划及交替结果保存在 `.tmp/stage9-open-fact-period-scope-r28.json`。并行合成建库期间的耗时仅供诊断，不能作为整页验收。

| 位置与调用方 | 判断和处理 |
| --- | --- |
| `reports._report_references`；完整季度报告、CLI/MCP、导出、浏览器附属依据 | 已证扫描问题。分类按实际开放月定位；档案、税确认、接续等少数类型保留类型索引。完整公开来源集合、精确冻结依据及后续正文核验不变 |
| `reports._rooted_classification_headers`、`dashboard._position`；报告及五页财务分类 | 已有相同开放月递归索引定位，纳入同一窄 SQL 定义，避免重复维护。分类缺 typed 全局反查的拒绝范围保留 |
| `dashboard.Snapshot.fact_ids_of_kind` | 同时包含当前开放与闭期／依赖采用，不能套用仅开放月的规则。本组不改，实际成本另列 |
| `report_projection._party_delta`、`report_flow._classification_refs` | 主 48 月实际计划中，前者按 `report_classification_voucher_revision` 精确凭证定位，再查 current／期间；后者 current／v1 均由精确凭证与本月索引候选 UNION 后核采用。已经有界，不替换 |
| 完整核验、修复、关账、备份恢复及 fixed-v1 | 完整历史职责保持；本组不改固定 v1 解码与核验合同。上层正常报告入口复用同一当前读取修复；不是拿普通页证明代替完整核验 |

三处当前读取的开放月定位已集中修改，冻结来源、接口和 DDL 不变。新增直接调用生产函数的两项测试通过：空库、间隔开放月、被后月覆盖但没有独立关账的月、修订与当前头、未来资料，以及 12／48／120 个月、增加关闭历史和无关对象后的工作量。

固定 r28 的 40 文件受影响组两片 JUnit 分别为 221、233 项，合计 **454 项通过，0 失败、0 跳过**；`result.json` 报 `source_unchanged=true`。主 12 月独立完整核验 `status=complete`；归档时主 48 月报告仍为 `measuring`，不能写通过，120 月尚未完成。与 r27 相同合成库、相同默认五页范围的原生插桩对照中，每档完整响应仅有 8 个生成时间或 `read_version` 字段不同，业务字段无差异。`read_version` 是候选读取版本，不能把差异解释成维修改变了数据库。报表页 VM 在 12 月为 1,222,100→1,147,300（少 74,800），48 月为 1,815,400→1,461,400（少 354,000）；返回行／字节分别保持 26,283／10,043,655 和 27,113／11,267,642。context、简报、资金、员工、资产的 SQL VM／行／字节也均未变化。此处证明的是查询工作量及输出等价，不是纯浏览器时延达标；原生仪器包含 SQL 插桩且不运行简报子进程，后台负载下的 wall time 不能用于 500 毫秒验收。

原始证据：[组回执](group-r28-result.json)、[JUnit 片 1](group-r28-shard-1-junit.xml.gz)、[JUnit 片 2](group-r28-shard-2-junit.xml.gz)、[主 12 月完整核验](main12-verified-r28.json)。五页工作量与响应按 12 月 [r27 工作量](stage9-default-main12-r27-work.json.gz)／[r28 工作量](stage9-default-main12-r28-work.json.gz)／[r27 响应](stage9-default-main12-r27-work.responses.json.gz)／[r28 响应](stage9-default-main12-r28-work.responses.json.gz)／[差异](stage9-default-main12-r27-r28-diff.json.gz)，48 月 [r27 工作量](stage9-default-main48-r27-work.json.gz)／[r28 工作量](stage9-default-main48-r28-work.json.gz)／[r27 响应](stage9-default-main48-r27-work.responses.json.gz)／[r28 响应](stage9-default-main48-r28-work.responses.json.gz)／[差异](stage9-default-main48-r27-r28-diff.json.gz) 保存。先前逐查询私有 AB 的[压缩记录](stage9-open-fact-period-scope-r28.json.gz)仍可复查，但整页结论以上述真实入口证据为准。

后续 r29 的同根因补齐分别见[报表档案与未冻凭证范围](grouped-report-coverage-range-r29.md)及[凭证期间定位](grouped-voucher-period-range-r29.md)。r29 固定源码在同一 12／48 月合成库上的六入口响应与 r28 业务字段完全相同；该证据不改变本页 r28 的原始验收状态，也不代表纯浏览器时延已达标。
