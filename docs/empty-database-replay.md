<!-- @format -->

# SQLite 空库重录、完整恢复与结果核对

当前入口是 `finance-local` 和 `finance_local_*` MCP，目录库 v3、公司库 v11；读取结构与前向兼容见 [有界看板查询](bounded-dashboard-queries.md)。
两类数据库分别检查结构合同并前向升级；不需要 PostgreSQL、ORM 或 Alembic。
旧 `finance-replay`、`finance-backup`、`ai_accounting.replay_cli`、`finance_record_event`
及组合协议回放包执行器已经退役，本文替代它们的操作说明。现在没有通用的“导出事实包后自动重放”命令。

## 1. 先选择本次任务

| 任务 | 使用什么来源 | 当前操作 | 会保留什么 |
| --- | --- | --- | --- |
| 完整公司备份恢复 | 已验证的 `<统一社会信用代码>.finance-company.zip` | `restore_company` | 备份中的公司/数据库身份、事实版本、凭证、证据、管理资料及关账记录 |
| 按原始资料空库重记 | 原始来件、明确补充事实和逐条来源清单 | `create_company` 后逐项 `evidence`、类型化保存、预览、确认 | 原件与已确认业务含义；新身份、事实版本和计算摘要在新库重新建立 |
| 已有库重建汇总 | 该库已有正式凭证与计算结果 | `rebuild` | 原凭证和事实不变，重建月度发生额、现金流、往来及期初汇总 |

`rebuild` 不会补回缺失原件、姓名、业务事实或经营说明，也不能把旧错误结果重算成正确账务。
完整恢复不等于重新记账；不得先创建同一公司，再把便携包覆盖进去。
便携包只包含公司资料库，负责人身份和会话在目录库管理，不通过公司包迁移。

执行真实重录前必须已经明确目标公司、资料范围及独立目标根目录。整理文件、修改程序或跑本页合成测试，
都不等于授权向真实公司写入，也不会自动删除任何原库或原件。

## 2. 保存原件和稳定引用

逐公司保存私有来源索引，至少记录：原始相对路径、原文件名、字节数、SHA-256、来源、允许的日期精度、
稳定业务引用、依赖引用、明确确认的核算事实与尚待核对事项。身份证件、工资、账号、真实说明和完整响应
只保存在已忽略的私有资料目录，不提交源码。

- 业务稳定引用可以是私有清单中的 `expense:2026-01:office:001`；它不是旧数据库 UUID。
  为每个目标保存 `稳定引用 → 新 subject_id` 映射，员工、往来方、资金账户、资产引用也保持一致。
- `fact_id`、计算 ID、凭证版本 ID、代发依据版本、报表依据版本、预览摘要和 `epochs` 以新库返回为准。
  下游字段若要求版本 ID，就使用映射中的新返回值；若要求稳定业务 ID，则使用新 `subject_id`。
  不把旧工资批次、资产来源、冲正引用或 `carry_forward_fact_id` 直接带入另一数据库。
- `evidence` 首次登记时传原件字节及**原文件名**。当前按内容摘要去重，同字节再次登记不会覆盖首次名称；
  同一内容的其他来件路径/别名保留在私有来源索引。不得为改名改变文件字节或伪造新原件。
- 文件说明不能替代核算事实。原件只明确月份就保留月份；实际收付款需要真实日期时按来源提供，
  不能补造月末日期。计算税额、实际扣税、实际申报、实际付款分别重建，不能互相代替。
- 旧凭证、旧余额和旧计算结果可以解释历史差异，不作为原始事实重录输入。
  只有本次明确选择正式期初接续时，才按 `opening_*` 和 `opening_package` 的当前类型化合同录入
  有依据的期初项目；这不生成虚构的历史交易。

## 3. 绑定隔离服务并取得当前合同

以下示例用于 Windows PowerShell。把路径替换为本次已选定的独立资料根目录；整个操作期间都显式传同一 `--root`。
开发环境使用仓库虚拟环境中的 `finance-local.exe`；独立运行包使用包根目录的 `finance-local.ps1` 或 `finance-local.cmd`，参数相同。
输入 JSON 保存为 UTF-8 无 BOM。不要把密码、恢复码或 Cookie 写入 JSON、终端参数或断点文件。

```powershell
$replayRoot = "D:\accounting-replay\本次已确认的隔离目录"
$financeLocal = (Resolve-Path ".\.tmp-kernel-venv\Scripts\finance-local.exe").Path
# 使用独立运行包时改为：(Resolve-Path "D:\本地会计运行包\finance-local.ps1").Path
& $financeLocal --root $replayRoot call schema
& $financeLocal --root $replayRoot security status
& $financeLocal --root $replayRoot security setup
```

`schema` 返回 `command_schemas`、`facts`、`security_request_schema` 和 `agent_operating_protocol`。
如果当前目录已经设置负责人，使用 `security login`；首次设置和登录均在本机安全窗口完成。
窗口请求返回不等于已登录，再用 `security status` 核对。首次使用新目录会初始化目录库，
因此先确认路径，不能用默认正式目录代替隔离目标。

MCP 的对应操作是先 `finance_local_schema()`，再
`finance_local_security(action="request", payload={"kind":"bootstrap_owner"})` 或 `{"kind":"login"}`；
用 `finance_local_security(action="session_status", payload={})` 查会话。
MCP 进程必须由 `finance-local --root <同一目标> mcp` 启动，不能用仍绑定正式目录的 MCP 执行隔离重录。

取得公司列表；新录使用 `create_company`，完整恢复跳到第 8 节：

```powershell
& $financeLocal --root $replayRoot call companies
& $financeLocal --root $replayRoot call create_company --input .\inputs\company.json
& $financeLocal --root $replayRoot call operations
```

`company.json` 的字段是 `{"taxpayer_id":"明确的统一社会信用代码","name":"明确的公司名称"}`。
保存返回的公司 `id` 为之后的 `company_id`，核对 `companies` 的身份与路径。
`create_company` / `restore_company` 通过持久操作记录处理重复调用，不接受 `request_id`；
相同身份和参数的调用可查询原结果，非空目标或身份冲突不能用改文件、删记录绕过。

MCP 业务调用统一为
`finance_local_command(command="create_company", payload={...})`；后面的公司命令同样调用该工具，
并在 `payload` 显式包含本公司的 `company_id`。MCP 对列表返回值加 `items` 包装。

## 4. 原件、类型化事实和管理信息一起接回

登记原件的 `evidence.json`：

```json
{
  "company_id": "本次返回的公司ID",
  "content_base64": "原件完整字节的Base64",
  "media_type": "text/plain",
  "name": "原始文件名.txt",
  "request_id": "source:original:001:v1"
}
```

```powershell
& $financeLocal --root $replayRoot call evidence --input .\inputs\evidence.json
```

Base64 是字节传输格式，不是脱敏；不要打印真实内容。单份原件上限 20 MiB。
保存返回的 `digest`，后续 `evidence` 列表或 `evidence_digest` 引用这个摘要。
多页/多行资料还要按当前 `inspect_material`、`receive_material`、`resolve_material` / `resolve_material_group`
合同登记和处置；仅调用 `evidence` 不代表每行已处理。跨月原件先以 `preview_material_allocation` /
`confirm_material_allocation` 明确逐项月份，再在各月处理相应业务。

下例是**合成测试费用**的 `expense.json`，不是实际公司事实模板：

```json
{
  "company_id": "本次返回的公司ID",
  "kind": "expense",
  "subject_id": "target-expense-2026-01-office-001",
  "data": {
    "period": "2026-01",
    "counterparty_id": "target-supplier-office",
    "amount_fen": 12500,
    "expense_class": "administration",
    "creditor_kind": "supplier"
  },
  "evidence": ["本次登记原件返回的digest"],
  "expected_revision": 0,
  "request_id": "fact:office:001:v1"
}
```

```powershell
& $financeLocal --root $replayRoot call save_fact --input .\inputs\expense.json
```

金额输入是整数分，不使用浮点元金额。批量保存使用 `save_facts`，把同结构记录放入 `facts`，
每条含 `kind`、`subject_id`、`data`、`evidence`、`expected_revision`；批次统一 `company_id` 和 `request_id`。
事实保存与正式发布是两步，不把 `saved` 当作已入账。不同公司的记录不能混成一批。

管理资料在正式事实之外追加；核算不需要的姓名、用途、显示编号等不塞入会计字段：

| 内容 | 当前命令与关键字段 | 使用顺序 |
| --- | --- | --- |
| 公司业务背景 | `update_company_note`：`text`、`expected_revision`、`request_id`，可附 `evidence_digest` | 先 `company_context` 取得当前版本，按原件保存 |
| 人员、往来方、资金账户、资产、业务展示档案 | `save_display_profile`：`profile`、`expected_revision`、`request_id` | `profile.kind` 为 `employee` / `counterparty` / `fund_account` / `asset` / `business`，`entity_id` 使用新映射 |
| 名称、用途和说明 | 档案中的 `display_name`、`display_number`、`purpose`、`note`、`source`，可附 `evidence_digest` | 使用已提供资料；员工入离职精度与状态只按明确来源填写 |
| 既有业务归集说明 | `management`：`subject_id`、`note`、`payment_period`、`payment_category`、`expected_revision`、`request_id` | 业务先存在；归集月份不改变入账月份 |
| 实际收款人姓名与账户 | `save_payee`：`party_id`、`name`、`account`、`evidence_digest`、`expected_revision`、`request_id` | 只有确有收款账户依据时保存，不为填姓名编造账号 |

`display_profiles` 可查现行档案；带 `period` 时是对应历史月份的封存读取。
看板会复用内核已有名称资料，展示档案补充其不足。未知人员状态保持 `unknown`，
不由工资生效月份推断入职日期，也不为关账补造管理资料。

## 5. 按依赖预览和确认，保存断点

每笔先登记并发布所需来源，再处理依赖它的业务：合同/制度和期初来源 → 成本、工资、资产启用等 →
付款/核销/折旧等后续事实。具体依赖取当前 `facts` 合同和预览返回的 `fact_issues`，不沿用旧组合组件协议。

```powershell
& $financeLocal --root $replayRoot call preview --input .\inputs\preview.json
& $financeLocal --root $replayRoot call confirm --input .\inputs\confirm.json
```

`preview.json` 为 `{"company_id":"本公司ID","subjects":["新业务subject_id"]}`。
逐项复核预览结果后，`confirm.json` 包含同一 `company_id`、`subjects`，以及刚返回的
`preview_digest`（取预览的 `digest`）、完整 `epochs` 和本次稳定 `request_id`。
浏览器看板只负责查看；上述命令经 CLI/MCP 提交类型化事实，不接受任意借贷分录。

私有断点记录至少保存目标根目录、公司/数据库身份、原件摘要、稳定引用映射、每步输入、返回 ID、状态，
以及**正式调用前**已保存的完整确认请求。逐步成功后再标记完成；写临时文件后原子替换可避免半份记录。
这些是本次操作记录的做法，不代表仓库提供了旧版回放状态文件执行器。

- 网络中断或进程退出，尚不能确定是否提交时，先在同一公司重发**原 payload 和原 request_id**。
  已提交返回原结果，不会重复产生凭证；不得立即换请求号再做一笔。
- 收到 `preview_expired` 且尚未成功时，重新读取来源并预览，复核新结果后保存新的确认步骤。
  不把旧库或旧目标的预览摘要换个 `company_id` 后提交。
- `idempotency_conflict` 表示同一请求号带了不同内容，先核对断点与原输入。
  实际更正走 `amend_fact` 或当前更正合同；不能通过改稳定引用把同一笔业务伪装成新业务。
- `expected_revision` 冲突先读取当前版本。相同请求重试保留原版本号，不盲目增加版本重写。
- 登录失效重新在本机窗口登录，再继续同一目标和同一请求。会话密钥不写断点。

## 6. 经营结论与关账顺序

业务、资料处置和管理名称都更新完成后，再编写经营结论：

```powershell
& $financeLocal --root $replayRoot call preview_period_commentary --input .\inputs\period.json
& $financeLocal --root $replayRoot call update_period_commentary --input .\inputs\commentary.json
```

`period.json` 为 `{"company_id":"本公司ID","period":"2026-01"}`。
预览中的 `basis.accounting_summary` 给出当月收入、费用、损益、现金/银行/平台余额和业务分组，
`basis.identity` 绑定本公司和本数据库。结合 `dashboard_brief` 逐笔资料、业务说明与实际原件写正文，
不把数字模板、错误码或未确认的猜测当经营原因。汇总为空不证明没有业务。

`commentary.json` 包含 `company_id`、`period`、`text`、`source`、预览的 `context_digest`、
预览的 `revision` 作为 `expected_revision`、稳定 `request_id`，以及可选 `evidence_digest`。
旧正文可作为待复核参考，但不能复用旧 `context_digest`；上下文变化先刷新再编写。
v9 将提交并发令牌与已存说明的内容有效性分开，保存时仍严格检查当前预览；
旧说明不得补绑当前内容依据，后补来源及时间含义见 [历史来源与内容版本](history-content-versions.md)。
这是智能体应完成的阅读和分析工作，不要求负责人替智能体写一段经营总结。

关账前依次完成：本期正式业务 → 来源逐项处置、资料完整性和实际对账 → 已知管理资料 →
有效经营结论 → `preview_close` → 本机密码确认 → `close`。经营结论后再有来源/管理变动，
应重新预览结论；失效正文不会冒充关账时有效的经营说明。管理缺项不新增会计关账阻断。

```powershell
& $financeLocal --root $replayRoot call material_completeness --input .\inputs\period.json
& $financeLocal --root $replayRoot call preview_close --input .\inputs\close-preview.json
& $financeLocal --root $replayRoot security close --input .\inputs\close-window.json
& $financeLocal --root $replayRoot call close --input .\inputs\close.json
& $financeLocal --root $replayRoot call closed_report --input .\inputs\period.json
```

`close-preview.json` 增加 `owner_confirmation`，引用本次实际完整性确认的证据摘要。
`close-window.json` 提供 `company_id`、`database_id`、`period`、`calculation_hash`（填关账预览
`digest`）和完整 `epochs`；`database_id` 从本目标 `company_context.identity` 读取。
CLI 的 `security close` 自动指定操作类型。完成窗口取得 `approval_id`；MCP 可用
`finance_local_security(action="status", payload={"request_id":"窗口请求ID"})` 读取结果。
`close.json` 使用同一月份、`owner_confirmation`、预览 `digest` 作为 `preview_digest`、`epochs`、
`approval_id` 和 `request_id`。缺资料或核对失败按事实补齐，不通过空清单、零额或伪造无业务确认绕过。

连续历史月份可用 `preview_close_range` →
`finance_local_security(action="request", payload={"kind":"approve_close_batches","batches":[...]})`
→ `close_range`，公司与范围必须与每份预览及批准完全一致。具体窗口结构取当前 schema，
不得把一份公司的批准用于另一公司或扩大月份范围。
每个 `batches` 项包含本目标 `company_id`、`database_id`、`from_period`、`through_period`、
`calculation_hash`（范围预览 `digest`）及完整 `epochs`。

新关账封存当时已有管理版本；闭期之后的 `update_period_commentary` 明确作为后补说明保存。
既有关闭月份及其摘要不改写。完整恢复保留原封存；原件重记形成的是新库本次封存，
不能宣称重现了旧库当时未保留的管理快照。

重录清单还应区分“原时点已知事实/说明”“后来核实的录入修订”“后来实际发生的新业务”“关账后补充的管理资料”。
有明确历史版本来源时按其实际顺序重录和核对，必要的会计更正使用当前类型化更正入口；
不能只导入最新事实值就宣称原修订、撤销、冲正审计也已恢复。
原有经营说明与名称先用本目标的新上下文复核；原时点已知资料才能放在该次关账之前。
**最新才补齐的名称、用途或经营解读只能作为后补资料，不能为了看板完整把它重录成原关账前已知信息。**
历史顺序或来源无法证明时保留缺口，不补造版本时间；需要原封保留全部历史应选择完整公司备份恢复。

## 7. 看板核对与汇总重建

```powershell
& $financeLocal --root $replayRoot call dashboard_brief --input .\inputs\period.json
& $financeLocal --root $replayRoot call dashboard_funds --input .\inputs\period.json
& $financeLocal --root $replayRoot call dashboard_employees --input .\inputs\period.json
& $financeLocal --root $replayRoot call dashboard_assets --input .\inputs\period.json
& $financeLocal --root $replayRoot call overview --input .\inputs\period.json
```

按真实业务范围核对金额、往来名称、资金账户名、原件名、月份/日期精度、工资实际扣税和付款、
资产及业务说明；经营结论必须与本次上下文一致。凭证追溯用 `trace` 并传对应 `voucher_version_id`。
长明细按返回游标继续，并沿用首屏 `snapshot_version` 作为 `expected_version`；版本过期重读，
不能把前 500 条当全量。季度查询 `dashboard_quarterly_report` 使用 `year` 和 `quarter`，
不能直接传月度 `period.json`；需要接账前资料时选择新库实际登记的来源版本。

只重建既有汇总时调用 `rebuild`，输入 `{"company_id":"本公司ID","request_id":"rebuild:本次操作:v1"}`。
前后核对 `overview`、看板汇总和凭证；不把“重建成功”当原件已补齐、经营结论已生成或业务已重新计算。
不要为追求旧数字一致修改新内核核算结果，差异按证据和计算依据说明。

## 8. 逐公司备份和完整恢复

先 `company_settings` 查询配置，再 `configure_backup` 提交本机明确选择的 `backup_directory` 和
当前 `expected_revision`。配置调用不接受 `request_id`，中断时先读取配置核实；源机路径不自动照搬。
初始备份调用：

```powershell
& $financeLocal --root $replayRoot call backup --input .\inputs\backup.json
& $financeLocal --root $replayRoot call jobs --input .\inputs\backup-job.json
```

`backup.json` 为 `{"company_id":"本公司ID","directory":"本公司已配置的备份目录","request_id":"backup:initial:v1"}`。
把返回的 `job_id` 放进 `backup-job.json`：`{"company_id":"本公司ID","job_id":"返回的job_id"}`。
常驻服务自动执行任务；合成测试用公开 `run_jobs` 推进同一工作器。
只有 `jobs.status == "succeeded"` 且 `result.path` 指向已验证 ZIP 才能交付。
`pending`、中间 SQLite 或失败任务不算完成；首次文件为 `<统一社会信用代码>.finance-company.zip`，
后续明确 `rollover:true` 的新备份才产生 `.previous.finance-company.zip`。
失败后先核对该任务的 `last_error`；处理原因后可对失败 job_id 调用 `retry_job` 并提供新的稳定
`request_id`，随后继续查询原任务。不要把创建任务时返回的 pending 响应反复当成最终结果。

完整恢复使用另一个明确选定的目标根目录、目标负责人会话，然后执行：

```powershell
$restoreRoot = "D:\accounting-replay\本次已确认的恢复目录"
& $financeLocal --root $restoreRoot security status
& $financeLocal --root $restoreRoot security setup
```

先按第 3 节在本机窗口完成目标设置/登录，再执行：

```powershell
& $financeLocal --root $restoreRoot call restore_company --input .\inputs\restore.json
& $financeLocal --root $restoreRoot call companies
& $financeLocal --root $restoreRoot call operations
```

`restore.json` 字段是 `archive`（已成功生成的 ZIP 绝对路径）、`taxpayer_id` 和 `name`。
恢复命令内部验证便携文件、结构、身份和证据，拒绝覆盖已有公司。
随后核对看板、`company_context`、`display_profiles`、`closed_report` 和实际业务终态，重新选择目标机备份目录。
公司备份中包含任务记录，但不保证旧机器的外部导出文件路径仍有效；需要交付的报表重新生成并验证。

## 9. 当前已验证范围

`tests/kernel/test_dashboard_empty_replay.py` 使用完全合成的临时 SQLite 公司，通过公开
`LocalService.dispatch` 验证原件→出资/费用/付款→展示档案/公司说明→经营结论→汇总重建→便携恢复。
它覆盖重复提交、确认响应丢失后继续、不同空目标的新 ID 映射、旧经营结论摘要拒绝，以及
恢复后的金额、往来名称、资金账户名、原件名和经营说明一致。另一个复用隔离 `Display` / `Periods` 的
无业务月份案例验证原时点说明→合成关账→后补说明的顺序，新目标必须重新预览，原时点说明保持冻结、
后来资料仅保留为后补版本，源库前后两种上下文摘要均不能直接用于新目标。
原生密码窗口、复杂公司全业务重录不属于这些案例；关账管理封存、失效上下文和迁移另由
`test_display.py`、`test_display_migrations.py` 补充覆盖。

```powershell
.\.tmp-kernel-venv\Scripts\python.exe -m pytest tests/kernel/test_dashboard_empty_replay.py tests/kernel/test_display.py tests/kernel/test_display_migrations.py -q
```

这些测试证明程序路径，不证明任何真实公司资料完整或已重录。交付分别记录“私有资料已整理”、
“隔离合成/公司重录核对通过”、“正式公司已实际写入核对”以及“备份已验证恢复”。
逐公司核对清单见 [空库重录与恢复检查单](formal-empty-db-startup-checklist-template.md)。
