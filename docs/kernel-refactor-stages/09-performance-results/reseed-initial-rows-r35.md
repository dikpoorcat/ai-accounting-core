# r35 合成库 reseed 初始化行审查

r35 的 main12 reseed 在 `_copy_rows` 复制 `source_change_head` 时因主键 1 已存在而失败；事务已回滚，失败 staging、原始公司库和日志保留，main48 未启动。固定 r35 的原始失败记录见 [失败证据清单](reseed-r35-failure-manifest.json)。此问题属于**合成夹具**对 `Store.create` 初始行分类不完整，不是业务库升级失败。

按 `schema.schema_sql`、`change_journal.journal_ddl`、`versions.install_metadata` 和真实 `production_bundle` / `Store.create` 新建空公司逐表枚举，唯一初始非空表为：

| 类别 | 表及新建行 | reseed 处理 |
| --- | --- | --- |
| 新目标身份与版本证据 | `identity` 1 行、`schema_meta` 1 行、`schema_history` 1 行 | 保留目标生成值；`identity` 必须与源逐值相同，后两表允许因新增索引而不同。 |
| 来源需要精确替换的种子 | `state` 1 行，初值 `(rowid,id,accounting,material,management,next_number,read_repair_revision)=(1,1,0,0,0,1,0)`；`source_change_head` 1 行，初值 `(rowid,id,sequence)=(1,1,0)` | 先核对新目标初值，再在禁用业务 trigger 的同一复制事务里按固定主键更新为源全部保存列；逐表核对 rowid、类型和值摘要。 |
| 其余业务、投影与封存表 | 新建时皆 0 行 | 只从源逐行插入并核对；若将来 `Store.create` 多生成未知行，门禁拒绝，不以冲突忽略或覆盖代替审查。 |

新增真实生产 registry/`Store.create` 的合成测试：仅从源 DDL 移除两项获准索引，创建非空业务事实、发布和闭月源；核对除新目标版本元数据外的全部表原始行、`source_change_head`、关闭月、外键、目标结构，并在复制中途注入异常验证完整回滚和 trigger 恢复。原 toy 测试仍用于精确 DDL 拒绝和外键失败。**本修复截至文档编写仅完成静态/Ruff 检查，真实定向测试待 r36 固定候选统一执行。**
