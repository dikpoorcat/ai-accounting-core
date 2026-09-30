# r26 同快照清偿封签行交接：私有负面 AB

固定 `.tmp/stage9-build-source-r26-release` 与已完整核验主 48 月合成库；不修改固定源码或数据库。原始工作量及原生 CPU 诊断分别压缩复制为本目录 `stage9-r26-settlement-tail-handoff-ab.json.gz`、`stage9-r26-settlement-tail-handoff-native.json.gz`，未压缩原件与私有脚本仍保留在 `.tmp`。两种脚本只在进程内临时替换当前 `settlement_projection._read_seals`、`verify_settlement_periods` 与 `settlement_freeze._tail_rows`，退出恢复原函数。后台建库并行，任何 wall 都不是性能验收。

## 公共范围与版本

当前在线路径为 `BusinessQueries.settlement_summary` → `settlement_projection.settlement_summary` → `settlement_freeze.frozen_subject_summary/_scope`；`Dashboard.prepare_settlements` 的历史/当前两种口径共用于资产、员工、简报及按需资金、CLI/MCP 业务读取。`QueryReads.verify_settlement_periods` 在同一只读快照内只复用成功期间封签，`_scope` 同时复用 root、block 和尾部，已经避免第二次完整尾部读取。本次主 48 月默认资金页并未走 `_tail_rows`，其工作量作为零触发对照。固定 v1 的 `settlement_projection_v1`、`settlement_freeze_v1` 负责完整历史投影及冻结内容核验，不调用当前在线 `_read_seals/_tail_rows` scoped summary；本 AB 不改变 v1 解释或全库核验。

原读取先以 SQL `json_group_array` 将一个开放期间 1,246 条 `settlement_change` 行序列化成约 451,653 字节，并与按 sequence 排序的发布 ID 一起验证期间封签；`verify_settlement_periods` 同时检查来源计算摘要及发布身份。通过后 `_tail_rows` 再从原表联接发布和来源元数据，供清偿状态 reducer 使用。原始两次表访问承担不同职责，不可仅删封签查询。

原型保留原发布、来源、期间 seal 比对和 `_tail_rows` 的高水位、期间集合选择；只有原 verifier 成功时才把其实际封签 JSON 行限本调用交给尾部。尾部解码 1,246 行，另外按 ID 查 994 个发布和 995 个来源的序号/类型字段，恢复原排序；无证明时可回退原尾部读取。四个完整默认页面响应剔除 `generated_at/read_at` 后，基线与原型哈希各自完全相同。

| 页面 | 变化后 SQL 次数 | SQLite VM 变化 | 返回行变化 | SQL 返回值字节变化 | Python JSON 变化 |
| --- | ---: | ---: | ---: | ---: | --- |
| 资金（未触发） | 586→586 | 1.5863M→1.5902M，计数噪声 | 5,339→5,339 | 3,925,336→3,925,336 | 无变化 |
| 员工 | 281→282 | 1.7925M→1.7403M | 22,612→23,355 | 4,883,573→4,605,966 | loads 445→446；输入 +451,653 字节 |
| 资产 | 1,487→1,488 | 793.5k→742.0k | 10,846→11,589 | 8,315,112→8,037,505 | loads 2,740→2,741；输入 +451,653 字节 |
| 简报 | 1,946→1,947 | 4.3065M→4.2512M | 85,482→86,225 | 26,941,453→26,663,846 | loads 11,208→11,209；输入 +451,653 字节 |

业务 fact/计算结果解码数在四页不变。原型虽少约 52–55k SQLite VM 和 278k 返回字节，却把封签 JSON 的 452k 字节搬到 Python 完整解码，并增加 743 行元数据返回。原生 ABBA（每页预热、两次各版本）CPU 中位数：员工 382.8→375.0 ms，资产 367.2→382.8 ms，简报 1546.9→1617.2 ms；Windows 计时粒度及并行建库使这些数只供诊断，但没有一致的净收益，简报明显变差。

**决定：不落地。** 现有同快照期间 seal 与尾部 cache 已避免重复验证/装载同一尾部；本原型的两种表示交接减少 SQLite 工作却增加 Python 大 JSON 解析和跨模块 proof 协议。未做损坏库 AB，不能将完整响应等价扩称损坏边界等价。若以后改用流式原始行一次核验/消费，必须保持发布列表及全期间封签（包括缺失行）、来源摘要和高水位、跨期修订顺序，另做真实损坏和当前/v1 各调用方验收；目前没有实现依据。
