# 多公司运行手册

## 架构与权限

多公司模式在同一 PostgreSQL 17 集群中使用：

- `finance_catalog`：唯一负责人账号、会话、恢复码、身份审计、公司路由、资料投影和生命周期动作。
- `finance`：迁移前已有公司的业务数据库，物理名称保持不变；它不必是当前默认公司。
- `finance_company_<32位UUID>`：系统为后续公司生成的业务数据库。公共 MCP、CLI 和看板均不接受调用者提供数据库名、URL 或 SQL。

每个业务数据库只有一个 `Organization`，并保存不可复制的数据库身份 UUID。业务归因只保存目录实例、负责人账号、会话和凭据版本快照，不跨数据库建立身份外键。

至少区分运行角色与迁移/供应角色，另保留既有只读备份角色：

- 运行角色：连接 `finance_catalog` 和已登记业务库，不拥有 `CREATEDB`。
- 迁移/供应角色：运行 Alembic，并只在本地集群创建公司数据库；拥有 `CREATEDB`，但不是超级用户且不拥有创建角色权限。
- 备份角色：仅可连接目录和已登记业务库并执行只读备份，不拥有建库、建角色或写入权限。

`.env` 的连接设置如下；URL 中的主机、端口和集群必须一致：

```dotenv
DATABASE_URL=postgresql+psycopg://runtime:...@127.0.0.1:5432/finance_catalog
FINANCE_COMPANY_DATABASE_URL=postgresql+psycopg://runtime:...@127.0.0.1:5432/finance
FINANCE_MIGRATION_DATABASE_URL=postgresql+psycopg://migrator:...@127.0.0.1:5432/finance
FINANCE_PROVISIONING_DATABASE_URL=postgresql+psycopg://migrator:...@127.0.0.1:5432/postgres
```

正式环境会拒绝运行角色与迁移角色相同的配置。仓库不保存任何真实密码。

## 从现有 `finance` 迁移

`0001_formal_baseline` 不修改；业务库只通过前向 revision `0002_multi_company_business` 升级，目录库使用独立的 `catalog_alembic` 迁移树。

迁移前停止 MCP 和看板，并确认没有连接目标库的运行进程。先用现有 `finance-backup` 生成完整的停止服务 `pre_upgrade` 备份，在隔离数据库执行一次恢复演练，并保存已验证清单文件的 SHA-256。然后执行：

```powershell
.\.venv\Scripts\finance-company.exe migrate-single-database `
  --backup-root D:\Protected\finance-backups `
  --backup-directory D:\Protected\finance-backups\<backup-id> `
  --restore-drill-manifest-sha256 <恢复演练确认的64位摘要> `
  --env-file .env
```

命令会先只读检查 `finance` 恰有一个企业、revision 为已知正式基线且目录目标为空，再创建和迁移目录、复制并核对身份记录、升级业务库、写入数据库身份和资料基线版本，最后才把 `.env` 的 `DATABASE_URL` 切到目录库。遇到未知历史、数量/摘要差异或不完整复制会立即停止；不会覆盖未知库，也不会自动删除失败时已创建的数据库。

切换后至少核对原企业的业务事件、凭证、开放项、期间关闭快照和执行归因数量及摘要，并验证原负责人登录令牌仍可用。失败恢复只使用切换前已验证备份，不提供自动跨数据库 downgrade。

## 公司生命周期

登录后 AI 使用以下 MCP 工具：

- `finance_list_companies(include_archived)`
- `finance_create_company(request)`
- `finance_preview_company_profile_change` / `finance_confirm_company_profile_change`
- `finance_preview_company_status_change` / `finance_confirm_company_status_change`
- `finance_get_close_backup_configuration`
- `finance_configure_close_backup`

创建必须显式提供幂等键、名称、18 位统一社会信用代码、首个资料生效日、月度/季度申报周期、`0.07`/`0.05`/`0.01` 城建税率和确认说明。数据库名与身份由系统生成。失败数据库不会自动删除，同一幂等键可在 `attention_required` 状态下恢复重试。

创建请求可用 `make_primary=true` 把新公司设为默认公司；目录库只允许一个默认公司。公司列表和未显式指定 `org_id` 的看板上下文优先选择该公司，不通过伪造创建时间控制顺序。

资料确认必须携带 preview 返回的同一计算哈希、幂等键、说明和公司业务库内的证据。资料版本不可变，只能从未来自然月边界生效；涉及季度税务口径时只能从季度边界生效，也不得覆盖已关账或已确认税期。

公司状态只允许 `active` 与 `archived`。归档公司仍可查询、查看看板和备份，但所有业务写入及资料修改稳定拒绝；恢复为 `active` 后重新允许写入。不提供公共物理删除工具。

## 看板

多公司模式的 `/api/dashboard/context` 返回 schema v2、公司列表、当前公司、公司状态和可用期间。所有数据及报表请求必须携带 `org_id`。前端切换公司时会取消旧请求，并清除不适用于新公司的月份或季度；归档公司显示只读标识。

默认显示公司选择器。固定公司兼容模式仍可使用：

```powershell
.\.venv\Scripts\finance-dashboard.exe --org-id <公司UUID>
```

## 独立备份与移交

### 关账自动单公司备份

首次关账前，负责人与 AI 确定一个本机目录，AI 调用
`finance_configure_close_backup`，显式提交绝对路径、幂等键和负责人的确认说明。目录不存在时只会
创建路径的最后一级；父目录必须已经存在。该位置以不可变版本记录在目录库中，后续改位置会追加
新版本；目录库继续保留历史关账所用位置版本及备份尝试的审计归因，文件系统不保留历史包。

`finance_confirm_accounting_period_close` 在写入前检查目录、专用 `finance_backup` 凭据和
PostgreSQL 17 `pg_dump`/`pg_restore`；未就绪时不会关账。关账业务事务提交后，系统使用只读
`REPEATABLE READ` 导出快照，复制该公司实际引用的证据，验证清单和所有摘要，并生成
`<统一社会信用代码>.finance-company.zip`。同一公司始终只有这个当前包：系统先在同一目录生成
并完整验证替换包，再以写穿方式原子替换旧包，最后清理能够验证为同一 `org_id` 的旧命名包；
任一步在替换前失败都会保留旧包。这个包不含目录库身份秘密，可以按下文的 `verify-portable`、
`unpack` 和 `import-company` 流程恢复或移交。

自动备份与业务库事务不能组成跨数据库/文件系统原子事务。若故障发生在关账提交之后，关账仍
明确返回 `posted`，同时 `close_backup.status=failed`；AI 使用完全相同的关账幂等请求重试即可
继续备份，不会生成第二次关账。每个关账的尝试次数、结果、文件摘要和所用位置版本都在目录库审计。

部署可通过 `FINANCE_POSTGRES_BIN_DIR` 指定 PostgreSQL 17 客户端目录；未指定时依次从 `PATH`、
`C:\Program Files\PostgreSQL\17\bin` 和 `C:\PostgreSQL\17\bin` 发现。

### 手工停止服务备份与移交

以下命令分别生成目录包、单公司包或全部相互独立的包：

```powershell
.\.venv\Scripts\finance-backup.exe create --backup-root D:\Protected\finance-backups --purpose daily --catalog --pg-bin-dir C:\PostgreSQL\17\bin
.\.venv\Scripts\finance-backup.exe create --backup-root D:\Protected\finance-backups --purpose handoff --org-id <公司UUID> --pg-bin-dir C:\PostgreSQL\17\bin
.\.venv\Scripts\finance-backup.exe create --backup-root D:\Protected\finance-backups --purpose daily --all --pg-bin-dir C:\PostgreSQL\17\bin
```

公司包只包含该业务数据库和它实际引用的内容寻址证据，清单记录 artifact 类型、`org_id`、数据库身份、schema revision 和摘要；包内不得出现负责人密码哈希、恢复码或会话秘密表。目录包才包含本机身份和生命周期数据。

目录形式的已验证公司包可以封装成单个便携 ZIP，并在传输后逐字节验证、受控解包：

```powershell
.\.venv\Scripts\finance-backup.exe pack `
  --backup-root D:\Protected\finance-backups `
  --backup-directory D:\Protected\finance-backups\<handoff-backup-id>.complete `
  --output E:\company.finance-company.zip

.\.venv\Scripts\finance-backup.exe verify-portable `
  --file E:\company.finance-company.zip

.\.venv\Scripts\finance-backup.exe unpack `
  --file E:\company.finance-company.zip `
  --output-root D:\Protected\finance-import
```

`unpack` 会验证 ZIP 路径、清单摘要、数据库归档和每份证据，并输出可直接传给 `import-company --backup-directory` 的 `.complete` 目录。便携 ZIP 本身不含目录库负责人身份。

目标实例由其当前负责人登录后导入 `handoff` 公司包：

```powershell
.\.venv\Scripts\finance-company.exe import-company `
  --backup-root D:\Protected\finance-backups `
  --backup-directory D:\Protected\finance-backups\<handoff-backup-id> `
  --pg-bin-dir C:\PostgreSQL\17\bin
```

导入会创建新的物理数据库，并验证当前 business head、唯一企业、数据库身份、证据摘要及不存在身份秘密表；经摘要复验的证据会安装到目标内容寻址目录并重写库内路径。目标目录存在相同 `org_id` 或纳税人识别号时拒绝。导出不改变源公司；对方确认导入成功后，源端负责人再单独归档。

手工 `create` 仍用于升级前、整套目录或明确移交场景，并继续要求停止服务和确认无目标运行连接；关账自动单公司备份则在业务事务提交后使用 PostgreSQL 一致性在线快照，不停止 MCP。两类备份都验证 `pg_dump` 归档、证据和内容摘要。备份目录所在介质、加密、异地复制与保留周期由负责人决定；备份角色只应获准连接目录及已登记业务数据库。
