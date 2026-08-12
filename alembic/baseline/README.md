# Schema baseline assets

`0001_baseline` 从本目录读取当前完整数据库结构：

- `postgresql.sql`：PostgreSQL 17 的表、约束、函数和触发器。
- `sqlite.sql`：测试与本地轻量运行所需的 SQLite 结构。

迁移脚本另行写入必要的初始税则和扩展所有权记录。两个 SQL 文件都不包含用户、业务事实或历史 `alembic_version` 数据。

本基线只面向空数据库。旧 revision 和历史数据库的原地升级不再属于支持范围。
