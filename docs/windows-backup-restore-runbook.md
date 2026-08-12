# Windows 停服备份与隔离恢复演练边界

> 状态：DEC-035 A 已落实的本地单机技术边界，不是私有试用发布授权。`finance-backup create`
> 信任经过生产模式校验的本机 Settings、应用运行数据库账号和当前 Windows 账户；取得这些账号或
> 当前 Windows 账户控制权的人不在一期防护边界内。命令仍会逐项检查独立备份凭据、停服 lease、
> 专用数据库 cluster、加密可移动介质、ACL 和完整性。正式试用前必须完成实际介质上的备份与隔离恢复
> 验收；目前不得创建计划任务，也不得把代码门禁通过视为发布授权。

## 已冻结的安全边界

- 仅适用于 DEC-025 A 的本地 Windows 单机和独立、已完全加密的 BitLocker To Go 移动介质。
- 每日及每次升级前必须先停止正式服务，再确认运行数据库账户在目标数据库中没有任何剩余连接。
- `finance_backup` 只用于完整逻辑读取与连接状态检查，不复用运行或迁移账户。初始化 SQL 位于
  `deploy/windows/finance_backup.sql`，并且只允许连接当前财务数据库；对任何其他 `datallowconn` 数据库
  具有直接或经 `PUBLIC` 继承的 `CONNECT` 都会使初始化及每次快照稳定拒绝。
- 密码只能通过 `finance-backup credential-set` 的无回显交互设置，并由 Windows Credential
  Manager target `ai-accounting-core/finance-backup-password/v1` 租借为
  仅当前 Windows 用户可读的临时 pgpass 文件。密码不得进入参数、环境变量、URL、日志、清单、
  仓库文件或任务计划 XML。
- 备份目标必须通过 removable drive、BitLocker `FullyEncrypted`、Protection `On`、100% 加密、
  无 reparse point 以及同目录 write-through rename/reopen 预检。任一失败都不得开始备份。
- 数据库证据清单在 `REPEATABLE READ READ ONLY` 事务中读取；该事务导出的 snapshot 由
  `pg_dump --snapshot` 使用。服务停机期间再复制并逐字节校验证据文件。
- 只有清单、数据库归档和全部证据通过验证，并且 Windows write-through 目录 rename 成功后，
  `.partial` 才能成为 `.complete`。发布失败必须保留 `.partial` 供调查。

## 首次配置与启用前验收

1. 先由部署负责人确认该 PostgreSQL cluster 只服务本机财务私有试用。不得在共享 cluster 执行下述
   SQL。以数据库部署所有者连接唯一的私有试用数据库，并显式确认专用 cluster：

   ```powershell
   psql --set=finance_dedicated_local_cluster=on -d finance -f deploy/windows/finance_backup.sql
   ```

   缺少确认变量时脚本不执行。脚本在一个事务内撤销 `postgres`、`template1` 对 `PUBLIC` 与
   `finance_backup` 的 `CONNECT`，再要求 `finance_backup` 可连接的 `datallowconn` 数据库集合精确为
   当前数据库。遇到其他数据库会 fail-fast 并回滚，不会擅自撤销未知共享数据库的权限。
2. 在同一个无回显 `psql` 控制台中执行 `\password finance_backup`。不要把密码写进 SQL、PowerShell
   历史、环境变量或命令参数。
3. 执行 `finance-backup credential-set`，在无回显提示中输入同一密码两次。CLI 不接受密码参数，
   也不从环境变量、URL 或配置文件读取该密码。
4. 核验 `finance_backup` 为 `NOSUPERUSER/NOCREATEDB/NOCREATEROLE/NOREPLICATION/NOBYPASSRLS`，
   且直接继承集合精确为 `pg_read_all_data` 与 `pg_monitor`。SQL 和每次备份都会 fail-fast 拒绝同名
   角色的额外 membership 或高权限属性，不会擅自撤销未知授权。若数据库出现启用 RLS 的业务表，停止配置并重新
   决定完整备份权限；不得自行授予 `BYPASSRLS`。
5. 为运行账户、迁移账户和 `finance_backup` 使用三个不同凭据。介质恢复码由企业负责人脱离电脑
   单独保管。
6. 确认正式 MCP 和 `finance-backup create` 使用同一份经过生产模式校验的本机 Settings，并由其中的
   `FINANCE_SERVICE_LOCK_FILE` 给出同一个绝对路径 service lock。备份与 MCP 都持
   Windows `LockFileEx` 独占锁，不需要也不接受 Windows 服务名；MCP 未退出时备份稳定拒绝，备份
   发布 `.complete` 前 MCP 也不能重新取得锁。
7. 代码已经允许 `create` 进入正式门禁，但这不授权在当前开发验收中访问真实凭据或实际介质。正式
   试用前，部署负责人必须在实际加密移动介质执行一次停服备份和一次隔离恢复演练并保存结果，随后
   才可另行评估是否启用计划任务。

## 每日及升级前顺序

以下是完成首次配置与物理验收后使用的命令形状。它会读取经过生产模式校验的本机 Settings，使用其中
的 loopback 数据库目标、运行账号和 service lock；再从当前 Windows 账户的 Credential Manager
取得独立 `finance_backup` 密码并检查实际介质。不得在开发验证、默认 Compose、真实数据导入或尚未
完成首次物理验收时运行：

```powershell
finance-backup create --backup-root E:\FinanceBackups --purpose daily --pg-bin-dir 'C:\Program Files\PostgreSQL\17\bin'
```

执行顺序如下：

1. 连接并解锁独立加密移动介质，执行只读卷状态检查及 durable publish preflight。
2. 正常退出正式 MCP；备份必须成功取得与 MCP 共用的跨进程独占 lease，不能只相信操作者声明。
3. 使用 `finance_backup` 查询 `pg_stat_activity`，确认指定运行账户在当前数据库中的连接数为零。
   `idle`、`idle in transaction` 和 `active` 都算剩余连接。
4. 在同一只读事务中读取唯一 Alembic revision、全量 evidence 清单并导出 snapshot；保持事务，完成
   custom-format `pg_dump`。
5. 将 evidence 内容寻址文件复制到 `.partial`，核验数据库归档可列出、全部哈希/大小和 canonical
   manifest，然后用 write-through rename 发布 `.complete`。
6. 只有新的 `.complete` 再次验证成功后，才可列出超过 30 天且自身也验证成功的清理候选。当前内核
   只返回候选，不自动删除；不得删除 corrupt 或 `.partial` 目录。
7. 安全弹出并断开介质，记录备份 ID、时间、验证结果和异常；随后才可重新启动正式服务。

## 隔离恢复演练

- 正式试用前、每季度以及升级前按需使用全新 PostgreSQL 17 隔离实例；推荐一次性 Testcontainers
  容器。不得把运行数据库、默认 Compose 数据库或其数据卷作为恢复目标。
- 工具先比较 host、port、database；恢复前还必须查询 `server_version_num` 与
  `pg_control_system().system_identifier`。目标必须为 PostgreSQL 17，且 system identifier 必须与
  清单冻结的备份源不同，因此同 cluster 的不同数据库及 host alias 都不能绕过隔离。目标中的非系统
  schema、relation、函数、类型、扩展、全文检索对象、FDW、publication/subscription、event trigger、
  large object、default ACL 等必须全为空。
- 恢复前先验证 `.complete`、归档目录和备份 revision。备份 revision 必须等于仓库当前唯一
  Alembic head；演练不会执行 `alembic upgrade`，也不会把旧备份静默改造成新 schema。
- `.complete` 验证后，数据库归档必须通过稳定源句柄复制到本地、仅当前 Windows 用户 Allow ACE 的
  临时目录；复制时及复制后复核大小和 SHA-256，并在整个 `pg_restore` 期间持有拒绝 write/delete 的
  句柄。只能对这个本地副本执行 archive list 与 restore，不能直接消费可移动介质路径。
- `pg_restore --exit-on-error --no-owner --no-privileges` 后，重新核对恢复库的唯一 revision，运行真实
  `alembic check`，并将恢复库 evidence `(id, sha256, size_bytes)` 与 manifest 及备份文件逐项交叉验证。
- 保存演练日期、备份 ID、仓库提交、PostgreSQL 主版本、验证结果和异常。容器只含演练副本，完成后
  可销毁；移动介质上的已验证备份仍按 30 天边界处理。

## 试用边界与退出

本系统仍是 Phase 1 私有试用，不是法定账簿、税务申报或法定留存期限判断系统。试用结束时先交付
可验证导出；数据与备份的删除、继续保留或移交必须取得用户书面确认，不能由自动清理推断。
