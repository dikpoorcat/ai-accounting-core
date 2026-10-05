<!-- @format -->

# r69：角色索引驱动及报表头复用的固定诊断

r69 仍不能验收。资金读取已恢复到 225–232 毫秒，但简报为 532–603 毫秒、员工为 542–593 毫秒，报表三次中两次超过 500 毫秒。以下是固定源码的原生读取、profile 和工作量诊断，没有浏览器整页刷新，也没有本候选的注册完整核验。

仓库仍为 draft/0。隔离 released/1 候选清单摘要为 `600fc82ccf6437c28c81c03ff56d04ddb0abaf07446782535ffb0dc29baa8af8`，候选日志与源清单中的记录一致；不把生成隔离合同当作正式发布或完整业务核验。复用原 r63 已核验的同结构主 48 月合成根 `.tmp/stage9-owner-main48-r63`，实际 JSON 明确记录 `current_source_full_integrity=not_run`，其旧 r63 核验不能认证 r69 当前源码。

## 三组修改及真实回执

| 同根因组 | 修改与必要保护 | 实际回执，不合并通数 |
| --- | --- | --- |
| 银行候选的 fact ID＋角色关联 | `dashboard_funds.py` 原 LEFT JOIN 为每个 fact 扫整个公司银行角色集。指定既有 `(fact_id,path)` 主键前缀，仍返回该事实的所有匹配角色；错 path／重复角色、实际 scalar、角色摘要对事实摘要、封存／发布、正覆盖及完整回退全部保留。 | 最小组 **11 passed／1 failed，63.40 秒**。唯一失败是增长 fixture 的第 13 份同月同金额草稿触发重复业务审核，未到工作量断言。改为 600 个不同金额的真实未来事实后，只补跑增长单例，**1 passed，212.64 秒**。 |
| 员工名单 locator 的重复冻结候选与角色索引 | `dashboard_reads.py` 按实际所选事实驱动角色查询；已有正常发布的冻结 calculation 不再从同一 reference locator 重复加入，缺源定位仍保留，沿精确 `(calculation_id,close_period)` 查缺口，复用已取得的真实 high-water 参数。完整来源／封存／采用核验和受影响业务边界保留。 | 员工／资产定向组 **40 passed，148.64 秒**。包含真实人员／月份增长、零金额、缺 publication／calculation／subject、损坏 reference、合法撤去、纠错、固定 v1 和预热边界。 |
| 报表开放月已核头的重复传输 | `report_projection.py` 及 `query_reads.py` 只在同一 owned、当前读取规则下复用本 selector 的真实开放月行与已核精确 publication；保留 voucher/source 完整关系、完整 17 字段来源 tuple、原始／冲正／更正／无影响复核以及独立和 v1 回退。仅需 mode/sequence 等字段的核验采用紧凑头，事实、正文和金额贡献 scope 不缩小。 | focused 首轮 **17 passed／1 failed，12.67 秒**；fixture 用了违反数据库 CHECK 的 `replace_open`，尚未到业务断言。修正合法 mode 后 repair1 **20 passed，16.61 秒**；converged 组合 **23 passed，28.83 秒**；后续 compact 定向 **15 passed，12.55 秒**。这些有重叠，不相加为一次完整套。 |

银行原失败及修正的 raw command/stdout/JUnit 是 `.tmp/stage9-bank-role-locator-r69.*` 和 `stage9-bank-role-locator-r69-growth-repair.*`。员工为 `stage9-r68-employee-locator-directed.log/.xml/.command.txt`；名称含 r68，实际是本轮修正后的定向回执。报表为 `stage9-report-selector-r69-focused/repair1/converged/compact.log/.xml`；不补造原日志中没有的命令文本。此前银行 scope 的 48 项、UNIQUE fixture 失败、两项 pointer 修正及被中止的扩大组也保留，但不是本轮 r69 合并回归。详见 [资金范围及遗漏记录](owner-bank-identity-witness-scope.md)。

## 真实参数、计划与工作量

| 实际诊断 | 原路径 | 修正路径 | 保留的输出／范围 |
| --- | ---: | ---: | --- |
| 银行角色 locator，主 48 月 96 个实际 ID | 13,884,500 VM | 5,200 VM | 同 6,625B 参数、96 行、26,448B 和完整结果 digest。 |
| 银行 locator 增加 600 个无关对象／12 个未来月 | 原 SQL 300→14,700 VM | 200→200 VM | 增长前后都是 1 SQL、4 行、850B；生产 SQL 按 fact ID 主键。 |
| 员工 locator，准确 `2019-12` 参数 | 1,889,700 VM | 1,370,800 VM | 两路径均 100 行，全部字段 tuple 相等。 |
| 报表开放月 source selector 组合 | 307,400 VM／5,306 行／1,545,832B | 289,200 VM／4,302 行／1,315,344B | 都是 6 SQL、1,005 个已核 publication、2,292 个完整 17 字段 tuple，相同 source digest。 |

银行主 48 月原 SQL 实际按 `entity_recorded_role(role=?)` 对每个候选重复扫描公司 24,097 条银行角色，修正按 `sqlite_autoindex_entity_reference_recorded_1(fact_id=?)` 查该事实的全部角色。前 scope 组的小样本 VM 已增长，但没有触发真实领域规模上的计划检查，这是本轮发现的同类排查遗漏。少返回正文并不能证明实际访问有界。共同 `verify_hits`、业务详情、资金退役 opening 见证和员工登记 membership 的真实计划已分别走 fact ID／entity ID 索引；没有相同扫描证据，保留原职责。完整 verify／backup／repair 需要的全引用范围没有缩小。

员工旧 r67／原 r68 手算 probe 把 `2019-12` 写成 **24239**，该数字错误。`YearMonth('2019-12').ordinal` 实际为 **24227**。旧 explain、VM 和 dedupe JSON／日志原件保留为撤回的错误截止月实验，不能再支持相同参数或本月收益结论；早先约 1,889,800VM 也不能替代本轮准确参数的 1,889,700VM。只使用 `.tmp/stage9-r68-employee-locator-vm-typed.py/.json/.log`：它从类型化年月及最终 production query builder 取得参数，明确记录 24227 和全部字段相等。已有业务测试使用类型化年月，不受手算 probe 错误影响。

报表第一份 plan 只证明初始选中头复用，selected 为 293,800VM／1,466,616B；后续实际 compact 计划为表中 289,200VM／1,315,344B。两份计划分别保留，不把后者改写成第一次测量。SQL/VM probe 是只读计划诊断，不是业务正文成功标记，也不是原生计时。

## 固定主 48 月六入口诊断

实际 wrapper 读取 `stage9-resident-r64-main48-profile.py` 后仅替换轮次为 r69。单常驻读取池中，每入口预热三次、原生采样三次，再分别执行一次 cProfile 和一次工作量插桩；三种测量互相分开。以下 min–max 来自保留的三次原生数值，不是 median/p95 或浏览器刷新。

| 入口 | 原生 min–max ms | SQLite VM | 返回行 | 返回值字节 | 本轮样本 |
| --- | ---: | ---: | ---: | ---: | --- |
| 上下文 | 10.47–11.56 | 11,000 | 96 | 850 | 3 次均低于 500ms |
| 简报 | 532.19–603.01 | 1,471,100 | 19,042 | 9,498,221 | 3 次均超 500ms |
| 资金 | 225.65–231.79 | 1,122,300 | 8,574 | 4,296,677 | 3 次均低于 500ms |
| 员工 | 541.58–592.89 | 1,800,300 | 5,431 | 3,473,865 | 3 次均超 500ms |
| 资产 | 340.46–362.65 | 703,700 | 11,501 | 8,448,529 | 3 次均低于 500ms |
| 报表 | 479.35–512.86 | 888,100 | 23,049 | 10,401,285 | 2 次超 500ms |

原生慢样本全部保留，没有通过省去 profile／插桩中的正常必要工作伪造计时。资金从 r68 的 600–675ms 明显恢复；员工 locator VM 降低仍没有使该入口达标，不能把计划收益直接写成页面时间收益。简报、员工和报表必须继续基于实际调用／必要内容证据收口。

## 证据保存及剩余范围

[r69 归档清单](owner-r69-fixed-diagnostic-manifest.json) 新建了 55 个 gzip 副本，共原文 **2,665,437B**、压缩 **792,736B**。包括候选日志、622 文件的源清单、profile wrapper 和 base、JSON／log／六个 `.prof`、银行角色计划及失败／修正回执、员工 typed plan 和 40 项测试、报表 first/final plan 及全部回执；旧错误 24239 的员工实验另标撤回。各 raw 文件和 gzip 的 SHA256、字节数、原文时间及解压逐字节等价均记录，全部等价；原始 `.tmp` 文件保留，没有覆盖已有归档，也没有修改 r68 canonical。

本候选完整核验、浏览器五页刷新、主 120 月、正式独立运行包及实际 AI MCP 的最终验收仍未完成。这轮不以三个定向组的通过数、计划/VM 改善或部分入口低于 500ms 代替阶段完成。仓库主阶段文档未改，本页仅保存 r69 的实际诊断与验证边界。
