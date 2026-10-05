# 完整核验快照边界修正（2026-10-05）

本组修正完整核验在调用者没有事务时可能读到多个外部提交，以及不能复用同快照已核验来源的问题。主仓库增加正常 SQLite 读取事务；已有读、写事务只借用。已完成小型合成反例与受影响模块验证，修正固定为525f来源；后续[新开发包及MCP内容桥](development-package-verification-snapshot-525f-20261005.md)分别记录。没有修改或重跑ce54／a5d4固定源、真实资料、服务、前端或性能样本，不继承a5d4的包、MCP、资格或500ms结果。固定源码不是released合同冻结。

修复前的完整入口矩阵、必要边界、ce54 实际106.68分钟及未受控比较限制见[只读核验链诊断](main120-development-source-verification-review-ce54-20261005.md)。该记录的“拟修”内容保留为发现时证据；实现与测试结果以本记录为准。已完成的[独立实际MCP验收](independent-mcp-a5d4-20261005.md)仍只属于原 a5d4 包。

## 实现边界

`runtime.verification_snapshot`在连接没有事务时执行`BEGIN`，成功或异常退出均`rollback`结束自己的读取事务；已有事务不执行BEGIN、commit或rollback。不改变journal_mode、foreign_keys、query_only，也不建立跨快照缓存。

| 修改入口 | 纳入同一读取快照的内容 | 保留边界 |
| --- | --- | --- |
| `backup._verify_connection` | header、三身份、注册完整核验、latest close与返回结果 | verify_file／copy_backup等已有事务继续借用；不同源、副本与ZIP核验保留 |
| `schema_bundle.verify_company_with_registry` | Store来源、身份、正式注册内容核验 | 合同精确匹配、fixed-v1选择及历史decoder原样 |
| `integrity.verify_integrity` | 直接完整核验调用及既有lease选择 | 所有authority、projection、index检查原样；调用者outer lease不失效 |
| `versions._execute_steps` | 首次完整source callback，包含callback采集的历史保留基线 | 读事务结束后才切FK并取BEGIN IMMEDIATE；锁内source、target、每步摘要／历史、fault回滚仍执行 |
| `offline_development_upgrade.upgrade_company` | 初次来源bundle选择、声明source、schema与完整内容核验；current verified_skip | 原拒绝已有事务规则保留；锁内三身份/source复验、真实步骤回执与目标检查保留 |

生产仅修改上述五个职责位置所在六个文件。首次预验、写锁内复验和DDL后目标核验属于不同状态，未删任何一次；第二步保留摘要仍包含第一步真实回执及新增表。没有修改冻结历史模块、v1合同或业务读取实现。

## 实际验证与失败保全

所有Python命令使用仓库`.tmp-kernel-venv/Scripts/python.exe -I -B -X utf8`；basetemp、日志及XML均在忽略`.tmp`。所有测试进程已经结束。本组没有大库计时、浏览器计时或服务操作。

| 实际运行 | 结果 | 说明 |
| --- | --- | --- |
| 新测试早验 | 31通过、1失败 | 测试错误引用函数的模块位置；保留失败原件后修正 |
| 函数位置修正定向 | 1通过 | 不与其他运行累加 |
| 小型历史增长定向 | 2通过 | 保留实际原函数执行 |
| 页面路径早验 | 1通过、1失败 | 测试误传资金account_id；改成真实显式movement_account_type／movement_account_id |
| 非空业务早验 | 1通过、2失败 | 页面路径已通过；新测试误写report_line_source列名，业务／冻结断言未删 |
| 非空业务修正定向 | 2通过 | 记录实际解码／结果／行量 |
| 收敛后的15模块整组 | **248通过、1失败**，249项，232.33秒 | 唯一失败是原迁移测试hook用in_transaction识别写锁callback；新37项全部通过 |
| 修改hook后的迁移模块 | **28通过**，1.10秒 | 只重跑实际修改模块；没有第二次249项全组通过记录 |

旧hook改为第二次callback触发来源竞态；原source、data、history、snapshot与FK恢复断言保留。首次callback现在处于正常读事务，第二次仍是FK关闭后的写锁事务，断言相应为`[(True, 1), (True, 0)]`。各运行重叠，不能累计通过数或拼成一次全组通过。已知失败均已修正并通过相应定向重跑，原始失败日志与XML全部保留。

受影响整组为`test_verification_snapshot`、`test_integrity_content`、`test_development_upgrade`、`test_direct_adoption_development_upgrade`、`test_migration_steps`、`test_offline_upgrade`、`test_offline_upgrade_history_preservation`、`test_stage9_backup_upgrade_history`、`test_foundation_backup`、`test_runtime_backup`、`test_direct_adoption_v1_isolation`、`test_v1_stored_json_isolation`、`test_v1_verified_close_prefix`、`test_verified_source_lease`、`test_report_flow_integrity`。未重复279项、159项包自检或前端组。

新37项包含三入口的外部并发commit：首次读取后的外部evidence提交不会混入当前完整核验，核验结束后的读取才看到新行；成功／失败均恢复无事务状态。已有BEGIN及BEGIN IMMEDIATE在成功／失败后仍存在，outer verified lease保留。实际投影损坏拒绝、开发首次与verified_skip、正式迁移首次callback历史基线与锁内竞态拒绝、三处未来迁移fault回滚／FK、真实portable forward restore及fixed-v1 decoder选择均有反例。

非空工作量场景复用正常类型化业务：出资50000分、费用10000分、3月实际支付10000分、三个冻结月份；现金40000分、损益权益-10000分、利润费用10000分、现金流费用10000分。额外增加12条无关证据及12个管理修订后，报表业务值与三份完整冻结响应仍相同。两场景完整核验均实际解码close 3次、计算结果3次、重建report source rows 6行，等于实际关闭来源行数；没有因无关证据／管理修订重复重建同一manifest／semantic。原函数真实执行，计数只用于诊断。完整核验仍检查新增证据和修订，必要历史增长不宣称恒定成本；小场景不证明大库收益。

## 五页与关账路径

静态实际链和动态阻断反例共同检查：Dashboard的brief、funds、employees、assets、quarterly_report正常读取，CloseReview读取已关账摘要，Periods.preview_close读取开放期预览，均不进入新增完整核验wrapper。资金使用显式cash账户和limit=20，季度采用实际deferred准备路径。`Periods._manifest`继续调用既有`verify_close_integrity`，CloseReview继续按采用计算调用`verify_sources`；这些局部完整性函数没有改成全库核验。

因此正常五页热刷新与关账摘要没有因本组增加BEGIN。上述小型业务消费者反例不等同实际浏览器或纯性能验收；显式完整verify、备份核验、迁移及恢复入口会经过修正边界。未来新源码的build／read_version仍须独立绑定，不能用文件未改证明响应版本编码或计时与a5d4完全相同。

## 字节守卫与原件

静态核对固定a5d4来源SHA `a5d4e1ac7d41814bb7a74d30410e0dbe45e6a691c85bd69f71deff10f57bd1d1`的776项manifest，所有固定原件仍与manifest相同。与当前树相比只有六个生产文件和一个旧测试文件变化，另增新测试。逐字节未变的守卫有33个frozen-v1文件、139个frontend文件、33个generated artifacts、26个static文件、4个合同文件及7个实际五页／关账消费者文件；分类有重叠。完整路径／SHA和精确diff保存在下列归档，未据此扩展旧Q资格。

| 文件 | 本组收敛SHA256 |
| --- | --- |
| `src/ai_accounting/kernel/backup.py` | `8e9973268a70c38d4411a62806e64073f2cb4572771468e815c70f09be816524` |
| `src/ai_accounting/kernel/integrity.py` | `5b1864db893934c76b502b46ba799eef0b3cce104be7d628272b915452410816` |
| `src/ai_accounting/kernel/offline_development_upgrade.py` | `375ec64c3a910c99fd1da38dd6107e6fbf0aeddd5f007c74aa956f7d65f9724b` |
| `src/ai_accounting/kernel/runtime.py` | `38ea3656852c48aad2347a7e2aacb555ca4108673f2c1db37ec69b3018c5df18` |
| `src/ai_accounting/kernel/schema_bundle.py` | `8d05ce00a5e730d37e5351bfb8c4031f59f1b424107e8788518e6d7185bcdbe2` |
| `src/ai_accounting/kernel/versions.py` | `1a03828afb6973c8cf816127f21fab470ffaaabe10b80dd9714dbb0d6a9b8fe6` |
| `tests/kernel/test_verification_snapshot.py` | `168d0a33d228bd260ad722deac25f4fb5728c249b9c026fc1405941fd3feff20` |
| `tests/kernel/test_migration_steps.py` | `c6a78b617c2011ba64c07c64e7b585e3f288c3a03352ec12d6f4fe9be829cfb2` |

[证据manifest](verification-snapshot-20261005-evidence/manifest.json) SHA为`16664036df2ed20f8b1680fa4db33b516e0e6bb6d5ec52dbfe7e9de86ff04bfa`：19份gzip包含八轮全部日志／XML、最终逐字节守卫、精确生产／测试diff及派生JUnit分析。每份解压内容与忽略目录原件精确核对，记录原件与gzip的SHA和长度；不含数据库、凭据或业务材料。最终静态守卫原件SHA为`3fb14044662df2506616c13f08260e0a11f01f1e09a5cd4abb71ab49511eb7a7`。归档不改原件；没有提交Git。
