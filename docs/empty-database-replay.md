<!-- @format -->

# 双基线空库回放手册

本手册只适用于空的 PostgreSQL 17 目录库和空的公司业务库。它不会升级旧 revision，
也不会覆盖已有表或已有数据库。正式源库在导出期间只读；回放包位于 Git 忽略的
`outputs/`，不得提交到仓库。

当前唯一基线分别是：

- 目录库：`0001_catalog_baseline_v2`
- 业务库：`0001_business_baseline_v2`

旧业务 revision `0001`–`0022` 和旧目录 revision `0001`–`0004` 已删除，不能原地升级到
这两个新基线。遇到旧库、未知表、非空目标或无法确认的数据库身份时立即停止。

## 0. 前置条件

1. 停止连接目标库的 MCP、看板和其他写入进程。
2. 配置 `.env`：`DATABASE_URL` 指向拟创建的目录库；公司运行、迁移和供应 URL 指向同一
   本地 PostgreSQL 17 集群。不要在命令行或文档中写入真实密码。
3. 安装仓库虚拟环境依赖，并准备经负责人确认的私有回放包目录。
4. 记录包外目录和公司关账备份位置；它们不得位于回放包内。

需要刷新包时，在源系统环境执行只读导出：

```powershell
.\.venv\Scripts\python.exe -m ai_accounting.replay_cli export-system `
  --output .\outputs\system-replay-YYYYMMDD
```

只有负责人明确要求整理历史操作顺序时，才可额外传入 Git 忽略的
`--normalizations <文件>`。规范化文件必须绑定源关账或业务事实中的逐字说明、精确员工或凭证行
范围和对应证据；导出器不会从缺失工资批次自行推断“本月无工资”，也不会自行猜测报表明细分类。

## 1. 离线验证回放包

```powershell
.\.venv\Scripts\python.exe -m ai_accounting.replay_cli verify-package `
  --package .\outputs\system-replay-YYYYMMDD
```

命令校验格式版本、总清单、每份证据的字节数和 SHA-256、所有稳定引用以及操作白名单。
任何文件被修改、引用缺失、出现源技术 UUID 或任意凭证行时都会拒绝。

## 2. 创建目录与业务双基线

```powershell
$replayState = ".\outputs\.system-replay.state.json"
.\.venv\Scripts\python.exe -m ai_accounting.replay_cli prepare-empty `
  --package .\outputs\system-replay-YYYYMMDD `
  --state-file $replayState
```

`prepare-empty` 按“目录库 → 所有登记公司业务库”的顺序创建结构，并核对两个 Alembic
revision。目标数据库不存在时才创建；已存在但非空、存在未知表或身份不匹配时不会继续。

## 3. 设置新负责人并登录

使用 `prepare-empty` 输出的 `primary_org_id` 设置一次新负责人。密码和一次性恢复码只在本地
无回显窗口中处理：

```powershell
$primaryOrgId = "粘贴 prepare-empty 输出的 primary_org_id"
.\.venv\Scripts\python.exe -m ai_accounting.identity_cli setup `
  --org-id $primaryOrgId --login-name owner
.\.venv\Scripts\python.exe -m ai_accounting.identity_cli login `
  --login-name owner
```

旧密码哈希、恢复码、会话、批准和数据库身份 UUID 不回放。

## 4. 回放两家公司

```powershell
.\.venv\Scripts\python.exe -m ai_accounting.replay_cli replay `
  --package .\outputs\system-replay-YYYYMMDD `
  --state-file $replayState
```

执行器只调用公开的类型化业务入口，固定先回放非默认公司、最后回放默认公司；当前包因此先跑
屋舍心声，再跑魂道，使高风险公司尽早暴露问题。业务事实、工作流确认、对账、报表前置事实和
关账控制均按操作键写入状态，可用完全相同的命令断点续跑。
历史月份只在关账阶段启用受控历史重建模式，并在成功或失败时关闭。拒绝动作、临时审计失败、
任意借贷分录和 SQL 数据导入不回放。

若包中包含经源关账说明逐字绑定的 `no_payroll_accrual` 规范化控制，员工会先按真实生效日期
登记，再逐人确认当月无工资、奖金和个税事项，最后关账。该控制不生成零金额业务事件或凭证，
也不能掩盖任何正数工资或不完整员工范围。

若源业务事实已经明确费用口径但旧关账早于报表分类硬门禁，包可包含
`financial_statement_classification` 规范化控制。它必须绑定一条稳定业务事件、精确凭证行、金额、
源说明及证据，只补充类型化报表明细，不修改凭证科目或金额。

## 5. 验证终态

```powershell
.\.venv\Scripts\python.exe -m ai_accounting.replay_cli verify `
  --package .\outputs\system-replay-YYYYMMDD `
  --state-file $replayState
```

验证报告写在状态文件旁，至少核对：

- 目录恰有包内公司，默认公司正确，业务库彼此隔离；
- 有效事件和凭证数量、逐张借贷平衡、科目余额、开放项与检查点一致；
- 银行流水和有效匹配、工资、社保公积金、税费、资产及报表前置条件一致；
- 期间关闭状态和经营解读一致，指定开放月份仍保持开放；
- 证据数量、字节数和 SHA-256 全部一致；
- 两棵迁移树分别只有一个 revision 和一个 head。

报告不通过时不得启动正式服务，也不得手工修改目标库来绕过检查。

## 6. 配置两家公司关账备份

登录后分别选择每家公司，通过 `finance_configure_close_backup` 提交各自的绝对备份目录、幂等键
和负责人确认说明，再用 `finance_get_close_backup_configuration` 复核生效版本。备份位置属于目录
库中的公司级配置。回放包的公司 `source_projection.close_backup_directory` 只作为源配置核对参考，
不会从源机路径自动照搬；负责人必须确认目标机实际目录后重新设置。

## 7. 启动服务

确认历史重建模式已关闭、`verify` 报告为 `verified`、两家公司备份位置均已生效后，再启动 MCP
和只读看板。首次业务写入前先执行一次公司列表和负责人会话只读检查。

## 私有归档

旧试用文档、临时脚本和旧魂道包应移动到新回放包的 `archive/` 子目录并重新生成
`MANIFEST.sha256`。归档只读、继续受 Git 忽略；不得作为新的回放输入，也不得直接删除原始资料。
