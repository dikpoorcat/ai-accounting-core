# 完整核验：重复范围与同次报表行复用组

当前四个生产文件的独立修改组已完成一次集中回归：**176通过、0失败、0跳过，366.89秒**。范围为 `settlement_freeze.py`、`report_classification_directory.py`、`report_semantics.py`、`integrity.py`，以及新增范围工作量和报表行租约测试。该组位于148d之后，尚未固定新源执行fresh完整资格或页面性能；不与此前148项公共回归、148d内容资格或150个浏览器样本相加，也不继承其结论。本轮仍为draft／0，不进行正式发行或运行切换。

清偿重建将同一批publication按实际posting_period一次分桶，逐月仍按各自highwater核对late reviews与权威冻结结果。分类目录在同次compare内只对自主重建且已完整展开的共享子树停止重复枚举，每月root以及最终全部实际node payload／set仍精确比较。报表语义通过同连接、活动事务、有效lease、精确关账集合及digest绑定的 `_VerifiedReports` 复用原始manifest选出的行；未提供该证明时保持独立来源读取。它不从可维修派生表取行，不跨连接、备份前后、修复状态或固定v1复用当前token。

| 合成工作量对照 | 旧→新实际计数 | 保留的证明 |
| --- | ---: | --- |
| 22行publication、3个closed月的period字段访问 | 66→22 | 完整prepared roots／blocks／revisions及全部实际freeze集合相同；late reviews原highwater和来源继续核验 |
| 6个月小树可达枚举 | 327→58 | 每月独立JSON图期望及最终root节点并集相同；52个不可达插入中间节点仍排除 |
| 真实3月classification compare枚举 | 2→1 | 全部roots、实际node payload／set和完整输出相同，缺失、篡改、多余节点及root损坏拒绝 |

这些计数是字段访问和同图collector读取次数，不是页面毫秒或十年规模收益。每月root／filter构造、hash、插入和最终全node比较仍保留，publication仍一次装载全表；额外分桶只保存行引用，RSS未测。无共享子树或仅一个closed月时，相关减少可为零。固定v1实现未修改，其独立reader与历史token边界仍纳入回归。

176项覆盖新增 `test_stage9_integrity_range_work.py`、`test_stage9_verified_report_rows.py`，关联report projection／semantics／flow及flow-integrity、current classification／settlement／late reviews、独立v1 settlement／classification／verified-close-prefix、完整核验与维修，以及在线WAL snapshot、初始便携包恢复／滚动、源损坏、坏重放、篡改snapshot／ZIP、FK及close manifest损坏、released来源到目标复核及缺失source verifier原子失败。完整默认集合、原凭证损坏、row租约、完整核验、修复、备份和独立v1断言保留。

范围测试此前首轮7通过／1失败、27.93秒；新增16主体用了同日期同金额资金事实，被真实重复业务规则拒绝，尚未进入工作量断言。仅区分实际日期和金额，未绕过规则；后续176项已跑到修正后的完整工作量与业务断言。首轮失败和最终完整组分别保留，不改称首轮全过。原scope同时保留静态排查及当时建议；其中末尾将report rows桥接列为后续候选的叙述属于该排查时点，实际四文件组已包含主代理完成的同次report token桥接，本记录按最终实现与176项范围说明。

[独立归档5份原件](owner-integrity-repeated-ranges-20261003-raw/manifest.json)包含完整组XML／log、原scope及范围测试首轮XML／log。固定64 KiB预算，原始58,326 B、gzip18,075 B；mtime=0，每份原件SHA归档前后相同、读回解压逐字节一致，并拒绝秘密值，不含数据库、ZIP、服务文件或私有渠道。manifest SHA `1ac31e59c9f01c45fdd726e10d0dba375abffa2eecf5bff3abf3eac594ae092c`；176项XML SHA `b08b908805b220bec84712eb562c54e81a366818cf0bf0d1e5ec0ecf39299aff`，log SHA `63b421db43bf917b9873b8d90d618b303c06c9609f74df6c5df9be66e9c7f5e4`。原148d密封源、浏览器和checkpoint归档均未改。

尚未验证新代码的fresh主规模内容资格、页面时延、完整资格耗时和RSS；累计分类prefix、owner party-key输入及更广修复桥接也不能由本组推为已优化。不依据静态复杂度或上述计数认定main120当前耗时根因。
