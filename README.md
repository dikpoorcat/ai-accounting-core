# AI Accounting Core

> 当前有效约束见 [AGENTS.md](AGENTS.md)。

面向中国小规模纳税人服务型企业的确定性记账内核。产品目标是让一位不懂财务的小企业负责人亲自使用：负责人只提供原始凭据并说清发生了什么，AI 负责理解、整理、追问并形成明确的结构化业务事实，确定性内核只负责按冻结规则校验、计算和执行。系统不重复实现属于 AI 的开放式理解或格式转换能力，AI 也不能自由编造借贷分录或推测缺失事实；只有资料完整且规则唯一的业务事件才会原子入账，信息不足时返回 `needs_information`。已确认事实优先于实现规则：不能为了满足规则改写事实；发现历史事实错误时，以追加式更正保留原审计记录并让当前状态反映真实事实。

> 当前版本是单企业私有试用内核，不是法定账簿、财务报表、纳税申报或税务意见。历史试用方案曾包含离线专业校准；是否沿用由用户在启动相应步骤时重新决定。

> 正式试用的启停和资料接入进度属于本地运行状态，不在公共仓库记录。未经用户明确启动相应步骤，不导入真实业务资料，也不部署为生产服务。

## 已实现

- 服务现销/赊销/履约、客户回款/预收/退款。
- 费用现付/挂账、供应商付款、员工报销；员工垫付可按受控费用、可退押金和固定资产成本分类，再由一笔银行付款统一核销。
- 股东借款/投入/归还、经证据确认且精确匹配流水的保留验证款营业外收入、银行手续费、内部转账、税款支付。
- 应收应付开放项及严格核销，禁止超额核销制造负数往来。
- 小规模纳税人价税分离、期间起征点、增值税减免和附加税试算。
- SHA-256 内容寻址证据库；正式 CSV 银行导入采用预览、计算哈希和确认提交，支持缺稳定流水号的逐行人工确认、迟到外部证据、逐实际账户对账及追加式处理历史；旧 CSV/XLSX 直接写入口只保留开发回归。
- 幂等入账、期间关闭校验、关联冲正、凭证规则轨迹和审计日志。
- PostgreSQL 延迟借贷平衡约束及已入账凭证不可改删触发器。
- 工资、社保、公积金、累计个税和全年一次性奖金的登记、试算、确认、支付与冲正闭环。
- 非员工个人劳务报酬的人员登记、固定劳务费与佣金试算、计提、扣缴、支付和冲正闭环；一笔已导入银行汇总扣款可原子覆盖工资与劳务子项。
- 固定资产外购、启用、逐月直线折旧、出售/报废、资产卡片和逆序冲正闭环。
- 外购无形资产的取得、可供使用当月起直线摊销、月末零收入报废和逆序冲正闭环。
- 持牌金融机构人民币固定利率借款的放款、合同单利计提、付息、到期还本和逆序冲正闭环。
- 税期试算与计算哈希确认、来源快照封闭和税务事实锁定。
- 自然月会计期间逐月生成、结账前累计完整性检查、计算哈希确认、不可变关闭快照和关闭月写保护。
- 严格参数、逐次认证的本地 STDIO MCP 工具；无聊天 UI、REST API 或内置模型调用。

工资、个人劳务报酬、固定资产、无形资产和借款只能通过各自的专用工具登记、试算和确认；通用事件入口不会接受 Agent 自行组织这些分录。存货事件仍会明确返回 `MODULE_NOT_ENABLED`。

## 本地启动

要求 Python 3.12、PostgreSQL 17；Windows 本地开发推荐使用 Docker Desktop。

### 已安装项目：每次如何启动

下面是日常启动流程。`finance-bootstrap`、负责人账号设置等首次初始化命令不要重复执行。

1. 启动 Docker Desktop，等待界面显示 Docker Engine 已运行。
2. 打开 PowerShell，进入本仓库目录：

   ```powershell
   Set-Location D:\GitHub\ai-accounting-core
   ```

3. 启动本地 PostgreSQL：

   ```powershell
   docker compose up -d
   docker compose ps
   ```

   等待 `postgres` 的 `STATUS` 显示 `healthy` 后再继续。如果仍为 `starting`，稍等几秒后
   重新执行 `docker compose ps`。如果容器未能变成 `healthy`，查看数据库日志：

   ```powershell
   docker compose logs --tail 100 postgres
   ```

4. 启动只读经营概览：

   ```powershell
   .\.venv\Scripts\finance-overview.exe
   ```

   启动成功后会自动打开 `http://127.0.0.1:8765/`。保持该 PowerShell 窗口运行；需要停止
   概览时在窗口中按 `Ctrl+C`。若程序停在 `psycopg.connect()`，通常表示 PostgreSQL
   尚未就绪；回到第 3 步检查状态和日志。

日常使用结束后可以停止数据库容器；数据仍保存在 Docker volume 中：

```powershell
docker compose stop
```

不要使用 `docker compose down -v`，该命令会删除本地数据库 volume。

### 首次安装：只执行一次

以下流程只适用于全新空数据库。要求先安装 Python 3.12 和 Docker Desktop，并确认 Docker
Desktop 已经运行。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
docker compose up -d
docker compose ps
```

等待数据库状态显示 `healthy`，再创建空库结构和首个企业：

```powershell
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\finance-bootstrap.exe --name "测试服务公司" --filing-cycle quarterly
```

`finance-bootstrap` 只运行一次，并会输出企业 `org_id`；请妥善保存。后续所有工具调用都必须
携带该 ID。不要在已经存在试用企业的数据库中重复执行该命令。

`0001_baseline` 是已经冻结的空数据库基线；私有试用启用后的结构变化由后续前向 revision
管理。旧的 15 段迁移链已移除，项目不支持从那些旧 revision 原地升级。对包含试用业务
数据的数据库执行 `alembic upgrade head` 前必须先完成备份和迁移前置检查；因此拉取新代码后
不要把迁移命令当成日常启动命令直接执行。

启动私有试用前，请复制并完成[空库私有试用启动检查单模板](docs/private-pilot-empty-db-startup-checklist-template.md)。实际完成状态和业务进度只保存在本地资料中。

首次试用还需创建唯一的本地负责人账号并登录。密码和一次性恢复码只在本地交互窗口中处理，不得通过命令行参数、环境变量或聊天传递：

```powershell
$trialOrgId = "将 finance-bootstrap 输出的 org_id 粘贴到这里"
.\.venv\Scripts\python.exe -m ai_accounting.identity_cli setup --org-id $trialOrgId --login-name owner
.\.venv\Scripts\python.exe -m ai_accounting.identity_cli login --login-name owner
```

妥善保存设置时显示的一次性恢复码。登录成功后，当前运行中的 MCP 会在下一次企业数据工具
调用时读取最新的本地会话令牌，无需重启 Codex。

项目包含 `.codex/config.toml`，其命令、工作目录和证据目录按本仓库固定路径 `D:\GitHub\ai-accounting-core` 配置；如果仓库位于其他路径，需要同步修改这三个值。在 Codex 中信任并打开本仓库、确认 PostgreSQL 已启动和迁移完成后，重启 Codex 即可加载 `ai_accounting` MCP。Codex 官方配置说明见 [Model Context Protocol](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。

Docker Compose 中的数据库账号仅用于本机开发，不得复用于共享或生产环境。数据库端口只绑定到 `127.0.0.1`。

### 本地老板经营概览

数据库启动并完成迁移后，可在本机打开只读经营概览：

```powershell
.\.venv\Scripts\finance-overview.exe
```

命令只监听 `127.0.0.1:8765`，启动后自动打开
`http://127.0.0.1:8765/`。页面每次刷新都会重新读取当前数据库，支持切换已生成月份、
查看业务闭环、复式凭证、关联凭据文件名和待结事项。页面没有写账操作，也不新增对外
REST API；停止服务可在启动窗口按 `Ctrl+C`。

若本地库包含多个企业，必须显式指定企业：

```powershell
.\.venv\Scripts\finance-overview.exe --org-id $trialOrgId
```

## MCP 工作流

1. `finance_get_profile`：读取企业、科目和税务政策。
2. `finance_get_event_schema`：取得业务事件 JSON Schema。
3. 可选调用 `finance_register_evidence`；银行业务先按下述专用工作流确认实际账户范围并导入流水。`finance_import_bank_statement` 仅保留开发回归，生产模式不可用。
4. `finance_query_context`：查询开放项；银行导入、迟到处理和对账状态使用 `finance_query_bank_statement_state`。
5. `finance_record_event`：提交业务事实。凡涉及银行收付款，必须显式提供已确认范围内的 `bank_account_code`；内部银行转账分别提供来源和目标账户代码。
6. `finance_get_event`：审阅事实、凭证、证据和轨迹。
7. 需要更正时使用 `finance_reverse_event`，不要修改旧凭证。

所有金额均为整数“分”，日期均为 ISO `YYYY-MM-DD`。

### 会计期间与月结专用工作流

产品创建的新企业默认启用期间控制；正式业务必须先生成对应的开放自然月：

1. `finance_generate_accounting_period` 从企业开始记账的任意过去月份起逐月连续生成；不能跳月，也不能生成 Asia/Shanghai 当前月之后的月份。
2. `finance_preview_accounting_period_close` 只读重算本月凭证、账户发生额、固定资产折旧、无形资产摊销、借款计息、工资批次及人工复核计数，并返回 SHA-256 计算哈希；同时在 `data.assistant_review_checklist` 返回面向 AI 的建议清单，区分“系统已完成”“发现待处理”“必须由负责人确认”和“本期尚未到期”。清单按企业申报周期触发：按季事项仅在 3、6、9、12 月检查，企业所得税汇算清缴与工商年报在每年 5 月集中提醒，按年事项在 12 月集中检查；未到期项目不得逐月重复询问。
3. 负责人审阅 AI 展示的完整月末清单并明确同意关账后，必须在本地窗口核对月份及当前预览哈希，并输入负责人密码复核。无需抄写确认短语；密码验证通过后签发一个与企业、月份及预览哈希绑定、30 分钟内且仅可使用一次的 `owner_approval_id`：

   ```powershell
   .\.venv\Scripts\python.exe -m ai_accounting.identity_cli approve-close --org-id $trialOrgId --period-id "预览返回的 period_id" --calculation-hash "预览返回的 calculation_hash" --login-name owner
   ```

4. `finance_confirm_accounting_period_close` 必须携带该 `owner_approval_id`，并在税期企业锁和月份锁内复算同一哈希；系统阻断为零、六项人工复核全真且有确认说明和证据时，保存完整不可变快照并单向关闭期间。AI 不能代替负责人签发确认，也不能仅凭清单字段为真自动关账。
5. `finance_get_accounting_periods` 查询生成、关闭动作和期间状态。空月份不会自动跳过，但允许经过同样的显式复核和负责人本地确认后关闭。
6. 提交关闭前，AI 必须逐项展示建议清单中所有到期且非完成的项目，主动核对未交材料、银行账户、应收应付、资产启用和折旧、新入职与工资、社保公积金个税、到期税额与外部申报、借款及股东往来；不得把数据库空记录推断成没有业务，也不得展示 `not_due` 项的问题。季度和年度清单仍保存在固定日程中，到期时必须触发，不能因普通月份不询问而遗漏。
7. 一期不支持反结账。关闭月原事实和凭证不改；错误只能在后续已生成开放月通过关联冲正及原专用工作流重记。

月末完整性核对不扩张个人账边界：AI 不询问或追踪创始人、股东、员工个人账户中的全部收支。只有公司已经确认承担、准备报销或已形成公司资产、费用、负债的个人垫付款，才属于公司月结核对范围。

所有企业的正式入账日都不得晚于 Asia/Shanghai 当前日期。历史税期更正保留原税务归属期，但调整凭证必须显式记入后续开放会计月。

### 银行流水与逐账户对账工作流

1. 在第一笔银行业务和首次月结前，使用 `finance_preview_bank_reconciliation_scope` 与 `finance_confirm_bank_reconciliation_scope` 显式确认完整实际账户范围；没有银行账户也必须明确确认空范围。每个实际账户对应一个独立银行资产科目，代码、名称和实际启用月份都由负责人提供事实、AI 整理，内核严格验证。
2. 一期正式导入只接受位于批准目录内的 CSV 文件名。Excel 由 AI 在正式导入前转换为 CSV；系统不接收任意路径、文件字节或调用方自报的迟到标记。
3. `finance_preview_bank_statement_import` 只读解析并返回规范行、问题和 SHA-256 计算哈希。缺少银行稳定流水号时，负责人必须逐行确认“新增”或“合并既有”，同时提供说明和证据；系统不得按日期、金额或摘要自动猜测。
4. `finance_confirm_bank_statement_import` 锁后重读同一文件并复算哈希。关闭月之后才到达的流水会冻结为迟到外部证据，旧月结快照、哈希、来源和计数均不改写。
5. 迟到流水通过 `finance_preview_late_bank_evidence` 与 `finance_confirm_late_bank_evidence` 在后续开放月处理。遗漏入账只能复用相应类型化业务工作流；仅补证据不生成凭证。处理结果被冲正时只撤销该动作的直接效果，当前状态恢复为处理前状态，替代处理必须另行明确提交。
6. 每个在用银行账户、每个自然月分别调用 `finance_preview_bank_reconciliation` 与 `finance_confirm_bank_reconciliation`。新流水到达后追加新版本，旧版本和旧月结不覆盖；未处理迟到流水、未匹配流水及追溯范围缺口继续作为可见警告或月结门禁。

### 工资专用工作流

工资不经过 `finance_record_event` 的自由事件路径，按以下顺序调用：

1. `finance_register_employee` 登记员工。
2. `finance_register_employee_profile_version` 和 `finance_register_payroll_policy_version` 登记有效期版本。
3. 仅在系统年中启用或迁移历史累计状态时调用 `finance_register_payroll_opening_state`。
4. `finance_preview_payroll` 试算并取得计算哈希。
5. 用户核对事实后，由 `finance_confirm_payroll` 使用同一哈希确认并入账。
6. 工资发放及社保、公积金、个税缴纳通过受支持的业务事件核销正式开放项。
7. `finance_get_payroll_batch` 查询完整计算、政策、凭证、支付和冲正链；更正仍使用 `finance_reverse_event`。

资料缺失、政策无有效版本、累计状态断层或支付无法唯一归属时，内核返回 `needs_information` 或稳定拒绝原因，不推测会改变会计处理的事实。

### 个人劳务报酬专用工作流

非员工个人劳务人员不进入 `Employee`、工资、社保或公积金模块，按以下受控顺序处理：

1. `finance_register_labor_service_person` 登记自然人劳务身份、关系有效期和证据；`finance_end_labor_service_person` 以追加证据结束关系。后续 `finance_register_employee` 可通过 `prior_labor_person_id` 显式连接同一自然人的历史劳务身份，但员工与劳务往来角色保持分离。
2. `finance_preview_labor_remuneration_batch` 逐人保存服务期间、固定劳务费、佣金、受益费用角色、居民身份、按次或连续收入归组及外部申报状态。缺少任一会改变处理的事实时返回 `needs_information`；非居民和学生实习特殊算法在首期明确拒绝。
3. 内核按业务日期选择有效的普通居民个人劳务报酬政策版本，用整数分和 `Decimal` 计算费用扣除、应纳税所得额、预扣率、速算扣除数、预扣个税和实付净额。`finance_confirm_labor_remuneration_batch` 复核哈希后按固定模板计提：借有限枚举费用/成本，贷个人劳务报酬应付。
4. `finance_preview_unified_payout_run` 与 `finance_confirm_unified_payout_run` 可把一个工资批次的一个或多个工资开放项和一个或多个劳务开放项放入同一父发放批次。所有子项净额必须精确等于一笔已通过受控导入动作进入系统的银行汇总扣款；银行流水只在父事件匹配一次，任何子项失败整批回滚。
5. 劳务支付首期只支持全额结算，不按比例猜测部分支付的个税分配。每个劳务子项必须显式选择 `net_after_withholding` 或 `gross_paid_without_withholding`。前者按政策税额扣缴并支付净额；后者仅表达有单独证据支持的“毛额已全部支付、实际未扣税”历史事实，仍保存理论税额和未扣差异，按毛额匹配银行且不虚构个税应付。支付模板固定，不接受调用方自组分录或自填税额。
6. `finance_pay_labor_withholding_tax` 只能核销逐人劳务扣缴来源的 `labor_individual_income_tax` 开放项，不能冒充工资个税来源。`finance_confirm_labor_external_declaration` 以追加式记录保存外部申报日期、引用和证据，不改写计提快照；本系统不宣称完成报税。
7. `finance_get_labor_remuneration` 查询人员、计提批次或统一发放批次；更正使用 `finance_reverse_event`，并按个税缴款、发放、计提的下游优先顺序冲正。

完整字段、会计模板和边界见[个人劳务报酬工作流](docs/personal-labor-remuneration-workflow.md)。

### 固定资产专用工作流

固定资产同样不经过 `finance_record_event`，只接受有限业务事实：

1. `finance_acquire_fixed_asset` 登记外购资产；已交付可用时同时提供 `ready_for_use`，一张凭证直接记入固定资产并建立折旧卡片。只有明确尚未达到可使用状态时才省略该字段、记入待启用资产。银行现付必须精确匹配流水，挂账会生成受控应付开放项。
2. `finance_activate_fixed_asset` 仅用于前一步明确尚未达到可使用状态的资产；达到可使用状态后冻结直线法、使用寿命月数、预计净残值、受益区域和官方规则来源。
3. `finance_preview_fixed_asset_depreciation_batch` 按启用月份次月起算，试算当月全部到期资产；返回逐资产折旧明细、费用科目汇总和批次计算哈希。逐资产金额按各正式资产卡片独立四舍五入，尾差只在该卡片最后一个月结清。
4. `finance_confirm_fixed_asset_depreciation_batch` 复算同一哈希后原子写入一个月度批次、多条逐资产折旧明细和一张汇总凭证；月份必须连续且入账日必须属于该折旧月份。当前同属管理受益区域时只形成“借管理费用—折旧费、贷累计折旧”两条汇总分录；以后存在不同受益区域时可有多条借方和一条汇总贷方。
5. `finance_dispose_fixed_asset` 处理单项非不动产资产出售或零收入报废，自动读取原值和累计折旧并计算清理损益；出售按有效的旧固定资产专项增值税规则计算。
6. `finance_get_fixed_asset` 查询资产卡片、政策版本、全部历史规范事实、凭证、证据和冲正链。
7. 更正仍使用 `finance_reverse_event`，顺序为处置、最新折旧、启用、购置；原凭证和规范事实不修改。

房屋建筑物、土地、自建/改建、融资租赁、减值、加速折旧、所得税折旧及税会差异仍不在本阶段范围。

### 无形资产专用工作流

无形资产一期只支持外购、已可供使用、可单独识别且不含土地权利的单项资产：

1. `finance_acquire_intangible_asset` 登记取得事实、成本组成、供应商、使用寿命、受益区域和证据；银行现付精确匹配流水，挂账生成受控应付开放项。
2. `finance_preview_intangible_asset_amortization` 从可供使用当月开始试算下一个连续自然月，并返回计算哈希。
3. `finance_confirm_intangible_asset_amortization` 在锁内复算同一哈希并生成固定摊销模板；最后一个月按整分余数闭合。
4. `finance_retire_intangible_asset` 只处理自然月末、收入/赔偿/税费/残料均明确为零的报废；当月必须已摊销或此前已经摊足。
5. `finance_get_intangible_asset` 查询资产、规范事实、凭证、证据、累计摊销和冲正链。
6. 更正顺序为报废、最新摊销、取得；原资产编号不复用。

研究开发、土地、商誉、后续资本化、减值、出售和税务摊销不在一期范围。

### 借款专用工作流

借款一期只支持中国持牌金融机构、人民币、单次全额放款、固定利率、合同单利、到期一次还本且无需资本化的合同：

1. `finance_draw_borrowing` 冻结合同、贷款人、应付息日、利率、日计数基础、支持边界事实、银行流水和证据。
2. `finance_preview_borrowing_interest` 按合同下一应付息日及 `[period_start, period_end)` 实际天数试算利息并返回计算哈希。
3. `finance_confirm_borrowing_interest` 在锁内复算合同和有效计息链后生成财务费用与应付利息凭证。
4. `finance_pay_borrowing_interest` 只清偿唯一关联的有效计息事件，付款日不得早于计息期末或晚于合同到期日。
5. `finance_repay_borrowing_principal` 只在到期日、连续计息至到期且全部有效利息已支付后一次归还全部本金。
6. `finance_get_borrowing` 查询合同、有效余额、规范事实、凭证、证据和冲正链；更正顺序为还本、付息、最新计息、放款。

循环额度、多次提款、浮动利率、复利、罚息、提前或分期还本、展期、外币、非金融企业贷款和借款费用资本化不在一期范围。

### 10,100 元服务现销示例

```json
{
  "org_id": "替换为-bootstrap-输出的-UUID",
  "idempotency_key": "bank-20260808-001",
  "event_type": "service_cash_sale",
  "business_dates": {
    "business_date": "2026-08-08",
    "fulfillment_date": "2026-08-08",
    "payment_date": "2026-08-08",
    "tax_obligation_date": "2026-08-08",
    "posting_date": "2026-08-08"
  },
  "amounts": {"gross_amount_fen": 1010000, "currency": "CNY"},
  "tax_facts": {
    "taxable": true,
    "rate_percent": "1",
    "invoice_type": "ordinary",
    "waive_exemption": false,
    "tax_due_on_event": true
  },
  "bank_account_code": "1002",
  "description": "已完成咨询服务并收到款项"
}
```

该事件生成：借银行存款 10,100 元，贷主营业务收入 10,000 元、应交增值税 100 元。收到款项但无法确定是回款、预收还是收入时，应提交 `customer_receipt`；没有核销或预收分类时，内核会返回 `needs_information`。

## 测试

```powershell
.\.venv\Scripts\pytest.exe -m "not postgres"
.\.venv\Scripts\pytest.exe -m postgres
.\.venv\Scripts\pytest.exe --cov=ai_accounting --cov-report=term-missing
.\.venv\Scripts\ruff.exe check .
```

以上命令是可选验证入口，不构成固定顺序或统一门禁。每次修改运行哪些检查、是否使用 PostgreSQL、迁移、STDIO 或覆盖率，由用户针对当前步骤决定；不要求每个小改动执行全套验证。测试目标仍须由执行者明确选择，不得破坏未经用户授权的数据。

## 当前税务规则边界

- 默认规则只覆盖 2026-01-01 至 2027-12-31 的中国小规模纳税人试点配置。
- 企业按月或按季、城建税率均为显式配置。
- 起征点采用“不含税销售额严格小于阈值”判断；达到阈值时不免税。
- 专用发票或明确放弃免税的销售不进入减免金额。
- 小规模纳税人的采购税额随价税合计进入费用或相关资产成本，不形成进项抵扣。
- 2026-01-01 起出售自己使用过的非不动产固定资产，按 3% 含税基数换算并减按 2% 计算增值税；是否进入期间起征点减免仍取决于发票与放弃免税事实。
- 普通居民个人劳务报酬按有效政策版本执行按次或连续收入按月归组后的预扣预缴：不超过 4,000 元减除 800 元，超过 4,000 元减除 20%，再按 20%/30%/40% 预扣率和对应速算扣除数计算；居民身份和归组事实必须明确。
- 每次政策更新必须新增有效期版本和官方来源，不能覆盖历史规则。

政策依据：

- [2026—2027 年小规模纳税人相关政策](https://fgk.chinatax.gov.cn/zcfgk/c100012/c5247426/content.html)
- [2023—2027 年“六税两费”减半政策](https://www.mof.gov.cn/jrttts/202308/t20230802_3899936.htm)
- [中华人民共和国城市维护建设税法](https://fgk.chinatax.gov.cn/zcfgk/c100009/c5193055/content.html)
- [增值税会计处理规定](https://www.mof.gov.cn/gkml/caizhengwengao/2017wg/wg201703/201707/t20170707_2641107.htm)
- [2026 年小规模纳税人出售自己使用过固定资产专项规则](https://fgk.chinatax.gov.cn/zcfgk/c102416/c5247434/content.html)
- [国家税务总局公告 2018 年第 61 号：个人所得税扣缴申报管理办法（试行）](https://12366.chinatax.gov.cn/bzds/070/070-5-4.html)
- [浙江税务：个人劳务发票仍需依法扣缴个人所得税](https://zhejiang.chinatax.gov.cn/art/2025/3/25/art_13314_634526.html)
- [中华人民共和国个人所得税法：全员全额扣缴与次月十五日申报](https://www.chinatax.gov.cn/n810219/n810744/n3752930/n3752974/c3970366/content.html)
- [小企业会计准则附录](https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852734144.pdf)

## 许可证状态

当前项目元数据标记为 `Proprietary`，未授予开源许可证。公开仓库可见不等于获得复制、修改或再分发授权；如需开源，应另行选择许可证并增加 `LICENSE` 文件。
