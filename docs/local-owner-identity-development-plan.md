# 本地单负责人身份与 AI 执行归因开发基线

> 状态：DEC-015 A、DEC-023 A、DEC-024 A、DEC-031 B 已决定；身份基础、Windows Credential Manager 会话与执行归因按正式 STDIO 接入。
> 记录日期：2026-08-11
> 上位决策：[会计期间与月结产品决策记录](./accounting-period-close-decisions.md)

## 1. 产品定位

一期只有一位小企业负责人。负责人提供原始凭据并说清业务事实；AI 和确定性内核负责分类、计算、记账、校验与追问。资料不足必须返回 `needs_information`，AI 不得推测缺失事实或自由编造分录。

不建成员、邀请、角色、权限、SSO 或多用户生命周期。`owner` 是唯一人类授权主体；`ai_agent`、`deterministic_kernel` 和 `system_job` 是执行者类型，不是账户或第二位复核人。

## 2. 密码与登录契约

- 使用 Argon2id v=19，固定 `m=65536 KiB`、`t=3`、`p=4`、16-byte salt 和 32-byte hash；完整 PHC 字符串入库。参数来自 [RFC 9106 第二推荐配置](https://datatracker.ietf.org/doc/html/rfc9106.html)。
- 密码先 NFC 规范化，按 Unicode code point 计数，长度 15–128；允许空格、Unicode、复制粘贴和密码管理器，不强制大小写/数字/符号组合，不周期强制更换，不截断。
- 创建和修改时检查离线常见/已泄露密码 blocklist 及产品名、登录名等上下文弱口令；不向外部服务发送候选密码。
- 未知账号也执行固定 dummy Argon2 验证；外部统一返回 `AUTHENTICATION_FAILED`。登录和恢复各自持久化失败计数，连续五次后指数退避，上限 15 分钟；成功登录或恢复后清零。
- 一期固定使用上述 Argon2id 参数，不包含自动重哈希或参数升级代码路径。

长度、Unicode、blocklist、无组合规则、限速、加盐适应性哈希与恢复码要求依据 [NIST SP 800-63B-4](https://pages.nist.gov/800-63-4/sp800-63b.html)。

## 3. 会话与恢复码

- 会话使用 256-bit CSPRNG 不透明 token，只在登录成功时交付一次；数据库只存 SHA-256。
- 空闲超时 30 分钟，绝对超时 8 小时；每次访问只可延长空闲时间，不超过绝对时间。账户必须仍为 active，会话 `credential_version` 必须与账户相同。
- 登出撤销当前会话；修改密码、恢复或停用账户必须原子增加 `credential_version` 并撤销全部会话。
- 初始化和主动替换时生成一个 128-bit CSPRNG Base32 分组恢复码，只展示一次，只存 SHA-256。使用后原子消费旧码、设新密码、撤销会话、生成一个新恢复码，不自动登录。
- 密码、恢复码、session token 不得出现在 MCP schema、AI 对话、URL、命令行、配置文件、错误或审计细节中。

## 4. 持久化与审计

`0013_local_owner_identity` 只建四类规范数据：

1. `owner_accounts`：全部署最多一个，唯一绑定一个 `Organization`；保存规范登录名、PHC 哈希、凭据版本、active/disabled、登录/恢复限速和时间。
2. `owner_sessions`：保存 token 哈希、发行/最后访问/空闲/绝对过期、撤销时间和稳定原因；主体、企业、密钥哈希和发行时间不可改。
3. `owner_recovery_codes`：保存哈希、发行、消费与作废时间；每个 owner 最多一个未消费且未作废的 current code，历史不删除。
4. `identity_audit_events`：追加式记录 setup、登录成功/失败/限速、登出、撤销、改密、恢复、恢复码替换和停用；只保存固定 event type、outcome、reason code、correlation ID 及可空 owner/session FK，不保存未知登录名、秘密或自由异常正文。

所有身份外键带 `org_id` 复合边界；不能删除已被审计/会话引用的负责人。迁移不猜测负责人或默认密码；zero-owner 是“待初始化”，生产仍关闭。任何身份/会话/恢复/审计历史存在时降级稳定拒绝 `IDENTITY_DOWNGRADE_UNSAFE`。

`0014_execution_attribution` 为每个已认证写调用追加一条不可变归因：负责人、会话、凭据版本、固定服务端 executor、工具名、服务端 correlation ID 与数据库时间。即使调用返回 `needs_information`、`rejected` 或只发生幂等回放，也保留本次调用归因；读调用不创建归因。新业务根在 owner 模式下必须引用同企业且在当前事务创建的归因，既有非空引用不能换绑；迁移前的空引用不猜测回填，只能在合法认证更新时一次性认领。

DEC-035 尚未生效。当前 0014 已能证明正常应用事务没有遗漏归因、没有跨企业或复用旧归因，但不宣称能够抵抗已取得 runtime 数据库凭据者直接构造归因，也不能据此启用依赖机器绑定的正式备份命令。是否把 runtime 数据库账号纳入不可信边界、拆分受控数据库身份或使用 `SECURITY DEFINER` 必须等待 DEC-035；本期不提前实现。

## 5. 负责人授权与执行者必须分开

内部 `ExecutionContext` 至少包含：`org_id`、`owner_account_id`、`owner_session_id`、`executor_kind`、`executor_name`、`executor_version`、`request_correlation_id`。请求不得提交 actor、session 或 executor。

按 DEC-015 A，负责人在有效会话中提供凭据和业务事实，可授权 AI/内核执行已存在的确定性工作流。业务动作必须同时冻结负责人会话与真实 executor；不能把 AI 写成负责人，也不能把自由 `confirmed_by` 文本当作真实身份。

## 6. MCP 与暂停边界

- 登录、改密、恢复不是 MCP 工具，密码不经过模型。
- 除公开只读的 `finance_get_event_schema` 外，所有读写工具都必须在事务开始解析服务端会话，并验证请求 `org_id == context.org_id`；错企业零写入返回 `ORGANIZATION_CONTEXT_MISMATCH`。数据库只要存在任一 owner，所有企业均进入认证模式；仅全库 zero-owner 的 development 环境保留旧测试路径。
- FastMCP `client_id`、公共 `_meta`、命令行和普通环境变量不构成认证。
- 按 DEC-031 B，`finance-login` 在无回显本地控制台中认证，将不透明 session token 存入当前 Windows 账户的 Credential Manager。正式 MCP 启动时只读取一次；启动后登录或换会话必须重启 MCP 才能生效。每次企业工具调用仍在数据库事务起点重新校验账户、凭据版本、过期和撤销。不得回退到命令行、环境变量或 MCP 参数，日志与 schema 也不得出现 token。
- 正式 MCP 在读取凭据前取得当前 Windows 用户专属的跨进程 service lease，并持有到整个 STDIO 生命周期结束；lease 缺失、ACL 不安全或已被备份进程占用时 fail-closed。

## 7. 最低错误与验收门禁

稳定外部码至少包括：`OWNER_SETUP_REQUIRED`、`OWNER_ALREADY_CONFIGURED`、`IDENTITY_CONFIGURATION_INVALID`、`PASSWORD_TOO_SHORT`、`PASSWORD_TOO_LONG`、`PASSWORD_BLOCKLISTED`、`PASSWORD_CONFIRMATION_MISMATCH`、`AUTHENTICATION_FAILED`、`AUTHENTICATION_THROTTLED`、`AUTHENTICATION_REQUIRED`、`ORGANIZATION_CONTEXT_MISMATCH`、`RECOVERY_AUTHENTICATION_FAILED`、`RECOVERY_AUTHENTICATION_THROTTLED`、`IDENTITY_DOWNGRADE_UNSAFE`。

验收必须覆盖：密码参数与长度/Unicode/blocklist；未知账号等时验证；限速持久化；会话哈希、严格过期、撤销和凭据版本；恢复码一次性与原子轮换；秘密/未知账号不泄露；单 owner、跨企业、不可变与 append-only PostgreSQL 负向；`0012 -> 0013 -> 0014 -> 0013 -> 0012`、`base -> head -> base -> head`、`alembic check`；公开 MCP schema 不出现密码、token、actor、executor 或 `confirmed_by`；真实 STDIO 覆盖无 token、伪 token、错企业和撤销会话。
