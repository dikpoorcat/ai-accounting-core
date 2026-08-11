# 正式私有试用工程控制基线

> 状态：工程基线；不是发布授权。DEC-015、DEC-018、DEC-023 至 DEC-031 已决定；本地单负责人身份、执行归因与供应链门禁已实现。备份恢复集成已实现但正式创建命令受 DEC-035 门禁暂停；DEC-032 至 DEC-035 的其余受影响实现同样保持暂停。

## 启动模式与数据库账户

本地开发使用 `FINANCE_ENVIRONMENT=development`，可以使用 `.env.example` 中仅限本机的
Compose 账户。不得把该文件中的 `finance:finance` 凭据、相对目录或 Compose 配置用于生产。

生产进程必须设置 `FINANCE_ENVIRONMENT=production`，并提供以下显式值：

- `DATABASE_URL`：运行时数据库账户；不得使用 `finance:finance`。
- `FINANCE_MIGRATION_DATABASE_URL`：仅供 Alembic 迁移使用，且必须与运行 URL 不同。
- `FINANCE_STORAGE_DIR`、`FINANCE_EVIDENCE_DIR`、`FINANCE_EVIDENCE_IMPORT_DIR`、
  `FINANCE_BANK_IMPORT_DIR`：绝对路径。证据存储目录必须位于存储根目录内。

两个 URL 必须指向同一个 PostgreSQL 数据库，但使用不同的非空账号；开发默认账号在任一 URL
中都被拒绝。应用进程只读取运行 URL；Alembic 优先读取迁移 URL。在开发模式，Alembic 仍允许测试通过
`DATABASE_URL` 或 `alembic.ini` 传入隔离临时库。运行和迁移数据库账户的实际最小权限由部署
负责人配置，本仓库不创建账户或授予数据库权限。

## 文件边界

证据文件路径会在读入前验证：文件必须在允许根内、是普通文件、未超过配置大小，并且路径上
不允许符号链接或 Windows junction/reparse point。证据内容寻址存储目录也拒绝该类重解析点。
Base64 证据同样只能写入安全的证据存储目录。

`FINANCE_BANK_IMPORT_DIR` 与 `FINANCE_MAX_BANK_IMPORT_BYTES` 已作为生产配置边界保留。银行导入
调用点的接入不在本次受限改动范围内，不能据此宣称银行导入白名单已完成；在该接入完成并有
独立验收前，生产试用不得使用文件路径银行导入。

DEC-023/024 的本地单负责人身份已经接入正式 MCP：除公开事件 schema 外，所有企业数据工具都必须在
同一事务内验证由 Windows Credential Manager 提供的不透明 session，并校验请求企业与会话企业一致；
请求不能自报负责人或执行者。旧的直接银行导入函数在生产模式稳定返回
`BANK_STATEMENT_PREVIEW_CONFIRM_REQUIRED`。这只是银行写入口的安全暂停，不代表新的预检、确认、
迟到证据或对账写流程已经可用。

## CI 基线

GitHub Actions 对推送和拉取请求执行：非 PostgreSQL 测试、Ruff、`pip check`、PostgreSQL 17
迁移升级与 `alembic check`、以及 PostgreSQL 不变量测试。CI 通过 `uv sync --locked --all-extras`
安装根目录 `uv.lock` 中冻结且带文件哈希的 Python 依赖；`pyproject.toml` 要求同一受审计的 uv
版本。所有第三方 GitHub Action 均固定到完整提交 SHA，并在同一行保留可读版本注释。Dependabot
每周提出 Python 依赖、`uv.lock` 和 GitHub Action 的更新。

DEC-026 A 与 DEC-030 A 已落实为无豁免门禁：`pip-audit` 不再允许失败，也没有忽略列表或豁免文件；
它报告的任何已知依赖漏洞都会使 CI 失败。CycloneDX SBOM 和扫描 JSON 仍作为工件上传，便于定位和修复。

DEC-027 A 已落实为 `uv.lock`、Action SHA 和 PostgreSQL 17 镜像 digest 锁定。依赖、Action 或测试
镜像的升级必须由审查过的 Dependabot 变更（或等价的显式更新）同时更新锁文件、完整 SHA/digest，
再通过相同门禁；不能改回浮动标签。

## 仍待完成的本地部署验收

DEC-025 A 已固定为本地 Windows 单机、系统卷静态加密和独立加密移动备份介质，不上云。
仓库已经实现停服 lease、BitLocker/可移动介质前置检查、数据库与证据一致快照、可验证清单、30 天
保留候选和隔离 PostgreSQL 17 恢复演练，并为独立 `finance_backup` 账户提供最小权限部署 SQL。
正式 `finance-backup create` 仍在任何配置、凭据或文件访问前稳定返回
`BACKUP_DEC035_DEPLOYMENT_BINDING_UNDECIDED`；只有 DEC-035 生效并完成同一机器配置来源接入后才能解除。
此外，正式试用前仍须在实际启用 BitLocker 的独立移动介质上完成一次停服备份与隔离恢复，并记录书面结果；
仓库测试或 Testcontainers 演练不能代替这项物理部署验收。
