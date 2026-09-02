# Schema baseline assets

`0001_business_baseline_v2` 从本目录读取拉平后的完整业务数据库结构：

- `postgresql.sql`：PostgreSQL 17 的表、约束、函数和触发器。
- `sqlite.sql`：测试与本地轻量运行所需的 SQLite 结构。

基线脚本另行写入全部版本化税则、个人劳务税收政策和扩展所有权记录。PostgreSQL 结构直接采用当前每公司一库、身份与目录分离的终态；SQLite 保留本地轻量兼容结构。后续结构变化由前向 revision 管理，不回写这里的 SQL 文件。两个 SQL 文件都不包含用户、业务事实或历史 `alembic_version` 数据。

本基线只面向空数据库。`0001_formal_baseline` 至 `0022_owner_close_gate_v3` 的旧迁移链不再支持原地升级；旧库应按回放手册导出业务语义后从本基线新建。
