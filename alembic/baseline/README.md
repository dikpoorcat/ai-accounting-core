# Schema baseline assets

`0001_baseline` 从本目录读取已经冻结的初始完整数据库结构：

- `postgresql.sql`：PostgreSQL 17 的表、约束、函数和触发器。
- `sqlite.sql`：测试与本地轻量运行所需的 SQLite 结构。

基线脚本另行写入必要的初始税则和扩展所有权记录。后续结构变化由前向 revision 管理，不回写这里的 SQL 文件。两个 SQL 文件都不包含用户、业务事实或历史 `alembic_version` 数据。

本基线只面向空数据库。已删除的旧迁移链不再受支持；从本基线建立并包含试用数据的数据库必须按当前 revision 链前向升级。
