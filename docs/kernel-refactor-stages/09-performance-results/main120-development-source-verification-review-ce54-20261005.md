# ce54 十年副本源核验只读诊断（2026-10-05）

本记录只读固定源码、执行helper、既有日志和9ac资格记录，没有打开数据库、启动服务、运行重负载、修改固定源或打断迁移。它与[独立MCP验收](independent-mcp-a5d4-20261005.md)分开，不属于新迁移结果或性能验收。

当前执行器固定源码为`ce54b17fda46c9596c06d96267f525efed3b5dbdd1380a8fec732b0e789d62a8`，目录`.tmp/stage9-build-source-adopted-index-flow-converged-20261005`，775文件、manifest SHA为`cb04026a3a54baaf3577bb88b45faae1f4a4071b0bdbb0942c89a7410b2fc8ac`。helper`.tmp/stage9-upgrade-index-transition-fixture-20261005.py`通过来源守卫后调用真实`offline_development_upgrade.upgrade_company`，仅包裹内容和全表摘要函数记录阶段耗时。目标为正常复制的独占合成main120副本，来源公司draft／0、精确C9指纹`c9f9f7051bca67f1241ee5c89676fb9476bc819dc8f92c1a0c0bd1e940f459cb`，声明索引目标为52556；不是fixed-v1，也不是实际用户资料。

既有日志`.tmp/stage9-index-transition-main120-upgrade-20261005.log`明确记录`content_1`于2026-10-04 19:04:10.919000 UTC开始，实际passed用时`6400991.595900007ms`，即106.68分钟；20:50:51.922811 UTC到达`before_begin`，20:50:51.924835 UTC开始`content_2`。此前约115分钟是墙钟估算，不能当作实际阶段用时。已有完成和后续进展，没有挂起证据；后续阶段结果本记录不提前判断。窗口有回归、包及MCP等并行工作，未受控，不是纯性能窗口。

## 可以从代码确定的重复

实际链为`upgrade_company`→`backup._verify_connection`→来源bundle注册的`verify_company_with_registry`→`integrity.verify_integrity`→`_verify_integrity_snapshot`。`runtime.connect`使用`isolation_level=None`，helper在第一次核验前没有BEGIN；`upgrade_company`也在第一次核验完成后才执行BEGIN IMMEDIATE。因此`content_1`的`connection.in_transaction`为false，未进入同快照lease和已核验来源复用分支。它仍做完整检查，但与当前注册资格入口的事务口径不同。

| 同次核验内的工作 | 非事务分支的实际行为 | 已有事务分支的复用 |
| --- | --- | --- |
| 清偿投影 | 先`require_settlement_projection`；随后`require_frozen_settlement_projection`进入`_authoritative_freezes`，因`_verified_projection=None`再次`compare_settlement_projection` | 已核验projection通过同connection、事务及lease检查后传给冻结核验 |
| 冻结manifest | `_check_closes`不返回已验证close集合；material_watch、report_projection、report_semantics、report_flow、read_indexes等再沿各自范围解码 | `VerifiedCloseArchive`复用已核验正文，仍核对精确close集合与快照 |
| 报表来源及往来 | report_projection重建`_manifest_rows`、party_delta、checkpoint；semantics无`_verified_reports`再次取manifest rows；flow无reports／semantics又重建rows、party／checkpoint和`_prepared_from_source` | reports、semantics在同事务lease内传递，flow保留自己的月度结果及根比较 |
| 报表语义的核算来源 | 无`_verified_source`时`_prepared_from_source`创建QueryReads，prime_calculations并解析所需关系 | 复用此次`_check_sources`已核验计算记录，再做需要的语义解析 |

证据位置为固定ce54的`offline_development_upgrade.py`中首次核验及BEGIN、`schema_bundle.py`的注册verifier、`integrity.py`的事务分支，以及`settlement_freeze._authoritative_freezes`、`report_projection.compare_report_projection`、`report_semantics.compare_report_semantics`／`_prepared_from_source`、`report_flow.compare_report_flow`。这些重复有调用及条件证据；没有阶段profile，不能量化它们占106.68分钟多少，不能断言它们是唯一根因。缺少material版本ledger意味着无法复用已核验定位，但其默认路径按提供的版本集合核对；本次没有证据把它描述成每次必然全表扫描。

同类边界只读核对：`backup.verify_file`、当前便携copy的source核验、`Maintenance.verify_integrity`及注册资格入口均明确先BEGIN，因此不能因同样调用`_verify_connection`就称它们存在上述重复。正式迁移／便携恢复使用`versions._execute_transition_plan`时，源callback在BEGIN IMMEDIATE前及锁内各执行一次，第一次同样属于非事务调用；这一分支是候选同类位置，本轮没有实际released升级计量，不把它的成本当作已测。锁外／锁内两次核验的目的不同，不建议删其中一次。

## 必要的完整工作与未验证项

`_verify_integrity_snapshot`默认include_projections／include_indexes均true，执行SQLite integrity_check／foreign_key_check、当前头、变更日志、所有保留事实／证据／计算／依赖／凭证、全部关账采用与批准、对象与身份更正、重复检查、所有派生投影及读取目录。`_check_sources`不传calculation_ids，所以`_rows`是全表范围；`_check_closes`不传through_period，所以遍历全部关闭月份。这是完整coverage的必要范围，不是五页默认20条的读取契约，也不能为了缩时改成当前月份或最近版本。依赖图遍历有completed／active集合及环检测，现有代码没有无限循环证据。

迁移的锁外来源核验、BEGIN IMMEDIATE内来源复验和DDL后的目标完整核验绑定不同状态，均须保留；全表保留摘要、保留历史摘要及外键检查也不由内容核验替代。ce54中`original_rows`紧接第一步`step_rows`还有同一锁内重复全表摘要，a5d4仅首步复用已修；它发生在`content_2`之后，不能解释此前`content_1`106.68分钟。第二步、每步后摘要、真实迁移回执及目标核验不应删减。

[9ac主120资格](current-main120-browser-9ac1-20261004.md)的完整核验为2951.930秒、约49.2分钟，使用`stage9_verified_open_preview.verify_registered_company`，在注册verifier前明确BEGIN，覆盖四项均verified、limitations=[]。该源码9ac的相应资格函数也有BEGIN。两者主规模都是120个月合成业务，但9ac是事务内注册资格，ce54当前阶段是声明迁移的锁外C9来源核验，源码、运行窗口、介质热度及并行负载不同；不能直接相除形成稳定退化倍数或索引收益结论。9ac资格期间也并行其他工作，已有报告明确不用于纯性能归因。

本轮没有SQL／VM／行字节计数、阶段CPU／I/O／GC／完整RSS采样，没有验证具体查询计划和每类解码次数，没有重新计算大库业务守卫，也没有重新跑9ac或ce54核验。建议后续只在新的明确修改组中评估：为锁外来源核验建立独立只读事务，结束后再取得写锁并保留锁内全量复验；让其消费已有同快照复用机制，而不跨边界缓存或删核验。实现前须核对调用者已有事务约束、失败rollback、身份／来源竞态及正式迁移callback行为。本次不修改正在执行的ce54，也不宣称修正已实现或大库收益已验证。

## 扩展完整核验入口矩阵（修复前只读证据）

以下矩阵检查主仓库全部生产完整核验调用及三处脚本入口；条件在固定ce54的相关函数中一致。它记录调用边界，不是新的执行结果。`BEGIN`是正常SQLite读快照，`BEGIN IMMEDIATE`是原写事务；固定历史context只选择decoder，不创建事务。

| 实际入口 | 完整核验时事务／快照 | 非事务else | 边界及处理依据 |
| --- | --- | --- | --- |
| CLI call／MCP `verify_integrity`→Service.dispatch→Maintenance | 明确BEGIN | 否 | 同一服务读取事务，包含repair revision读取 |
| rebuild／repair_read_indexes | Engine._write的BEGIN IMMEDIATE | 否 | 前核验authority与后核验维修目标目的不同；调用者写事务不可结束 |
| backup.verify_file | 明确BEGIN | 否 | 结构、身份、内容及latest closed在同一读取事务 |
| copy_backup | 源明确BEGIN；目标verify_file另有BEGIN | 否 | 源和独立副本不同载体，不能合并验证 |
| create_portable／滚动／任务重放 | 正常候选经verify_portable→verify_file；重放源经verify_file | 否 | ZIP候选认证、旧包认证及当前源认证为不同边界；capture只核对header并复制，不冒充完整核验 |
| verify_portable／普通restore／Catalog注册及restore_company | 解包后verify_file明确BEGIN | 否 | ZIPhash／内容核验、发布前后核验分别保留；目录事务不代替公司事务 |
| development.upgrade_company首次预验／目标verified_skip | 无事务，函数显式拒绝调用者已有事务 | 是 | 真正缺少完整读取快照；skip没有后续写锁复验 |
| development.upgrade_company锁内来源及目标 | BEGIN IMMEDIATE | 否 | 来源复验、DDL后目标核验、各步摘要／历史／外键均保留 |
| development.upgrade_root保全阶段 | verify_file／create_portable各自明确BEGIN | 否 | 不把已验证备份当作后来写锁内的数据库状态 |
| released offline_upgrade root前检／锁内inspect／skip | 每公司verify_file明确BEGIN | 否 | resident锁限制本服务，不等于对外部SQLite写者持有公司写锁 |
| released versions._execute_steps首次source callback | 无事务 | 是 | callback还保存保留历史基线，必须让整个callback属于同一快照 |
| released versions._execute_steps锁内source／target callbacks | BEGIN IMMEDIATE | 否 | 必要来源竞态防护及目标原子失败回滚 |
| portable forward restore→upgrade_database | 初次verify_file有BEGIN；随后execute_steps首次callback无事务，锁内两callback有写事务 | 首次callback是 | 私有解包目标通常独占，仍缺少公共完整核验快照保证；后续verify_file再核验保留 |
| fixed-v1 registered verify_v1_company | 仅historical_content(1)，继承调用者事务 | 取决于上列入口 | 普通便携核验有事务；正式／forward迁移首次callback无事务；非事务还失去v1 financial-position已核验close prefix复用 |
| schema_bundle registered verifier／直接integrity.verify_integrity | 自身不建立事务 | 调用者无事务时是 | 当前列举的普通公开入口有事务，内部直接调用仍没有统一保证 |
| stage9资格／切公司及stage3历史benchmark | 三处脚本均明确BEGIN | 否 | 不把9ac资格49.2分钟与ce54非事务预验106.68分钟作为稳定成对实验 |

非事务连接的每条SELECT可以在前一游标结束后看到新的外部提交，因此一次完整核验可能混合不同图像；单条语句的一致性、各行digest和resident锁均不证明整个authority／projection核验属于同一时点。可能表现为并发导致的拒绝，或返回无法归属单一快照的verified结果；本次未执行并发反例、没有观察到实际误核验成功。锁内全量复验继续防止未经核验的变化进入迁移，不能用它否认锁外或verified_skip的核验结果缺少快照保证。

拟修职责限定为正常核验快照：公共完整核验边界在无事务时BEGIN并finally rollback；已有读／写事务只借用、不commit或rollback。正式executor首次source callback的完整历史基线采集也纳入该只读快照，读事务结束后才切FK并取原写锁；开发首次来源合同、身份和内容核验同理。原历史decoder、合同、所有核验及写事务回滚规则不改，不跨快照保留proof。需要小型反例验证外部提交时前后读同一状态、失败及成功恢复无事务、已有读写事务保留、损坏拒绝、source race／迁移fault原子回滚及FK、fixed-v1选择、forward restore和同次工作复用。此拟修组另记新源码，不改a5d4／ce54标签。
