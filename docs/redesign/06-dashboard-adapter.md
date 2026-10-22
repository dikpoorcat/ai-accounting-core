<!-- @format -->

# 看板适配与读取交付

本模块把新体系的公开业务结果交给现有 Vue 看板。经营简报、资金、员工、资产、财务报表五页的主要内容、布局、样式、筛选、展开和刷新用法保持；不以后台重构为由重做页面。合作项目投资只接通记账结果，不新增页面、板块或项目管理展示。顾问来源查阅按第11节提供独立只读入口，不加入原五页的默认读取链。

职责依照 [整体架构](02-architecture.md)：`reading` 通过公开入口批量取得并整理业务结果，`dashboard` 适配 HTTP 格式、范围和错误；领域与账务模块不依赖页面字段、前端类型或看板缓存。共同类型以 [接口合同](03-contracts.md) 为准，本文规定现有页面的外部兼容要求。

五页、展开详情及来源查阅均通过同一本机服务提供，文档、读取和文件任务由后台接续。资金账户是否停用、是否发生归属调整没有正式依据时保留未知；银行原文缺失就留空，不能拿业务用途替代原文。资产编号采用可选当前档案，缺省为空，不要求补齐。员工页按正式人员来源和实际关系展示，缺少期初时累计欠薪为null，已有本月工资及真实付款照常显示。

## 1. 唯一响应合同

[dashboard-response-v2.json](contracts/dashboard-response-v2.json) 是前后端共同采用的响应真源，保留现有字段和页面用法。资产取得及启用时间允许真实月份或日期，未明确实际日就保留月份；长期资产净值未知时为null，老板待办没有明确所属月时同样保持null，由页面说明待核对。按 UTF-8、LF 行尾计算的 SHA-256 为 `0e52bcf09c315e500cb0e8c055695d8369ff68d1bc16f85d42f81b501219aa53`；Windows 检出的 CRLF 先规范为 LF。顶层是以命令名索引的 Schema 集合，每项单独作为 JSON Schema 使用；其中 `$ref` 从该项根解析。

本文下表列出的响应以该合同为字段、必填项、枚举、空值、金额格式及 `additionalProperties` 的唯一真源。它不替代新数据库合同，也不随旧内核模型自动覆盖。前端生成器从此真源生成 `dashboardResponseSchemas.json`、TypeScript类型和Ajv校验，后端生成包内同份合同；不维护两套当前响应定义。允许资产月份没有改变原字段、内层版本常量或对已有日期的接受范围。确需变更时发布新合同并同步消费者。不能凭字段名称另写近似响应，不能为通过校验伪造零值或旧处理状态。合同还包含其他旧命令，保存它们不表示新体系实现这些入口，尤其不恢复旧审批、重放和审计机制。

| HTTP GET 端点 | 响应 Schema 键 | 内层 schema_version |
| --- | --- | --- |
| `/api/dashboard/context` | `dashboard_context` | 3 |
| `/api/dashboard/brief` | `dashboard_brief`；首读外壳 `dashboard_brief_delivery` | 19；外壳 6 |
| `/api/dashboard/brief-group` | `dashboard_brief_group` | 3 |
| `/api/dashboard/funds` | `dashboard_funds`；首读外壳 `dashboard_funds_delivery` | 9；外壳 6 |
| `/api/dashboard/employees` | `dashboard_employees`；首读外壳 `dashboard_employees_delivery` | 13；外壳 6 |
| `/api/dashboard/assets` | `dashboard_assets`；首读外壳 `dashboard_assets_delivery` | 10；外壳 6 |
| `/api/dashboard/quarterly-report` | `dashboard_quarterly_report`；首读外壳 `dashboard_quarterly_report_delivery` | 5；外壳 6 |
| `/api/dashboard/business-status` | `dashboard_business_status` | 10 |
| `/api/dashboard/close-review` | `dashboard_close_review` | 2 |

保留 `frontend/src/api/` 客户端和配对 Ajv 校验的消费方式。编码时前后端均从冻结合同生成类型和校验器，再与现有客户端核对；新后台运行时不导入旧内核的response_contracts或总服务。不能通过 TypeScript 断言或关闭校验隐藏不兼容。现有生成类型中的命名缺陷也不成为目标合同，生成结果须通过类型检查。

`period-preparation` 当前没有五页实际调用，新适配器不提供此旧检查入口。期间状态和顾问读取使用新的公开合同；不能因冻结快照中存在同名 Schema 就恢复整套旧准备流程。

## 2. 请求与范围

请求参数不得重复。未列出的参数返回 `invalid_command`。GET 参数先按下列类型解析，再调用公开读取入口；除下表明确允许的无凭证定位，不能把空字符串当成缺省值。`company_id` 以最终 URL 为准，客户端可从当前地址补入，服务端不按名称猜公司。

共同参数如下；各端点只接受下表明确适用的项目：

| 字段 | 类型与规则 |
| --- | --- |
| `company_id` | 不透明公司身份。除 `context` 可省略以选择目录中的默认公司，其余必须提供。 |
| `period` | 有效 `YYYY-MM`。月页省略时选择 `context.default_period`；组成员、业务进展及月度核对必须提供。 |
| `limit` | 整数 1–500，默认 20。首读和手动凭证分页为 20，自动续页为 500。 |
| `section` | 端点声明的集合名。省略表示该端点首屏，不表示返回全部明细。 |
| `cursor` | 不透明非空字符串；只能与 `section` 一起使用，续页同时提供 `expected_version`。 |
| `expected_version` | 已取得的结果标识。只允许读取相同范围的该版内容，过期返回 409，不能悄悄切到新版本。 |
| `preparation` | 兼容现有客户端，可省略或为 `deferred`。不提供旧 `complete` 诊断模式。 |

没有可查看月份时，月页返回合同允许的 `selected_period=null、snapshot_version=null、data=null`。空公司仍可取得 `context`，不补造建账月份。对明确但不属于本公司的对象、组或凭证，返回定位错误；不换成别的公司或别的月份。

### 2.1 各页参数

| 端点 | 共同参数之外的输入及约束 |
| --- | --- |
| `context` | 只接受可选 `company_id`。返回可选公司、月份、季度及默认值。月份和季度按新到旧排列，默认最新有效项；已选择公司不可用时明确报错。 |
| `brief` | `section=activity/open_items/vouchers`；可用 `activity_key:string`、`voucher_version_id:string` 或 `voucher_number:正整数` 精确定位，三者互斥且不得与 `section` 合用。首次主请求直接定位，不翻遍分页。`activity_group=business/cash/people/assets/tax/investment/correction` 仅筛选本月发生集合。接受 limit/cursor/expected_version/preparation。 |
| `brief-group` | 必须提供 `section=activity/open_items`、`group_key:string`、`expected_version`；接受 limit/cursor，不接受 preparation、精确凭证定位或其他筛选。 |
| `funds` | `section=accounts/movements/statements/investment_products/investment_events`；`movement_account_type=bank/cash/payment_platform` 与 `movement_account_id:string` 成对；`statement_account_id:string` 独立筛选银行流水；`movement_account_selection=all/first` 默认 all，first 只用于未指定账户、section、cursor的首屏。 |
| `employees` | `section=employees/labor_workers/labor_sources`；`employee_filter=all/in_period/unknown/ended/employment_active/employment_unpaid_leave/employment_departed/employment_unknown`，默认 employment_active；可选 `employee_id:string`，查询 labor_sources 时必须提供对应人员身份。 |
| `assets` | `section=assets/projects`；`asset_filter=all/active/fixed/intangible/pending/exited`，API 默认 all；可选 `asset_id:string`、`project_id:string`，分别定位对应集合。页面普通默认 active，精确资产定位使用 all，不能混淆 API 与页面默认。 |
| `quarterly-report` | 只接受 `company_id`、`year:1..9999整数`、`quarter:1..4整数`、可选 preparation；季度为 `YYYY-Qn`，不能套用月页的 period、section或cursor。 |
| `business-status` | 必须提供 `subject_id:string`；`settlement_view=historical/current` 默认 current；可选 `section=settlement_events`、limit/cursor/expected_version；`detail_scope_key:string` 与 `voucher_version_id:string` 成对，选择精确业务分项。真实无凭证成员的后者为空字符串，仍须由精确范围核验，不退回整项业务。 |
| `close-review` | 必须提供 `company_id`、`period`；首读可省略 `preview_digest:64位小写十六进制字符串`，提供时核验相同内容。此字段是第 7 节的只读核对标识，没有批准效力。 |

资金页面第一次未指定账户时请求 first，并消费 `selected_movement_account`；随后用明确账户身份分页。账户筛选先作用于完整月份，再分页。员工精确定位越过未指定的默认关系筛选，显式筛选仍取交集；资产身份与显式类型筛选同样取交集。同名人员和账户不合并。employee_filter筛选员工和劳务人员名单，不按当前关系抹去已定位人员的历史labor_sources。

### 2.2 公开读取入口映射

HTTP 处理器只做解析、校验和字段适配，按下表调用 `reading.public`；入口参数及 `ContextScope/MonthScope/LedgerScope` 使用共同合同，不另建一份隐含范围。

| HTTP 范围 | 公开入口及范围 |
| --- | --- |
| `context` | `page("context", ContextScope, {})`；通过storage.list_companies及company_metadata读取公开元数据，无公司时只读目录中的可选范围，不打开全部公司业务库。 |
| `brief/funds/employees/assets` | `page(同名 page_key, MonthScope, query)`；MonthScope含company_id、可选period及expected_head。reading在同一读快照中选定实际默认月，再形成period_from=period_to、view=as_posted的内部LedgerScope；没有可查看月份就返回空页面，不补造月份。 |
| `quarterly-report` | `page("quarterly_report", LedgerScope, {})`；期间范围为所选季度的三个自然月，view=as_posted。 |
| `brief-group/close-review` | `detail("brief_group"/"close_review", MonthScope, query)`；必须提供明确月份，query仅含本节列明的定位、筛选或核对头。 |
| `business-status` | `detail("business_status", LedgerScope, query)`；period_from=period_to为指定月份，view=as_posted，query含明确事项及本节列明的分项范围。 |
| 报表生成、状态、下载 | `export_report(scope, head, request_id)`、`report_task(company_id, job_id)`、`report_download(company_id, job_id)`；head由已取得的预览标识和epochs共同校验。 |

`expected_version` 转成同范围的 expected_head；业务进展首读不套用父页面版本，续页沿进展自身返回版本；`preview_digest` 转成对应只读检查或报表计划头。业务进展中的 historical/current 只选择清偿的查询截止，不改变主月采用口径；同次响应需要当前后续信息时，在同一个 `ReadContext` 中明确读取两个视图。跨模块读取共用该上下文及只读事务，不能由每个提供者另开连接。没有指定月时先从context确定范围，再查询，不把缺省期当作当前自然月。

### 2.3 分页约定

集合统一为 `collections.<section>.items` 与 `page`。`page` 包含完整 `total_count`、当前筛选完整 `filtered_count`、本页 `returned_count`、`has_more`、`next_cursor`，可选 `collection_version`。要求 `total_count ≥ filtered_count ≥ returned_count = items.length`；有后页时游标非空，无后页时为 null。

汇总、分类金额和完整计数覆盖全部业务，不能由已加载页反推。空页不能同时声称有后续内容而不给可推进游标。游标绑定公司、数据库、月份或季度、结果标识、端点、集合、筛选、业务分项及排序，不能跨范围复用。服务端全范围排序后分页，客户端不重新排列已读局部页。

排序保持现有用法：独立凭证按公开凭证号升序；本月业务和资金业务按业务月份、明确日期、对象拼音、稳定身份升序，同月只明确月份的记录排在明确日的记录之后，未知月份最后。员工以入职月份和日按相同精度规则排序，再按姓名；劳务人员、资产和基金产品按名称拼音及稳定身份。资金账户先按最早正式记账月份升序，同月按名称，未知起点最后。往来组按对象及事项拼音、稳定组身份，组成员按核算所属月、款项分项、稳定身份。中文拼音比较忽略声调与拉丁字母大小写，同音用原文字、同名用稳定身份兜底；不从管理日期或登记时间补造排序依据。

`brief` 的业务组、往来组、独立凭证，以及 `brief-group` 的组成员各有独立游标。成员页同时返回本页引用的完整凭证，按首次引用顺序去重；引用凭证的身份集合和配对凭证集合一致。它们只进入预览索引，不替代独立凭证列表首屏。

## 3. 交付状态、版本和错误

五个主页面在未提供 `section`、`expected_version` 时返回交付外壳。提供任一参数时返回裸页响应，组成员、业务进展、context和close-review始终返回各自裸响应。不能把所有请求统一套一层外壳。

| 外壳状态 | 必须返回与消费的内容 |
| --- | --- |
| `ready` | schema_version=6、company_id、period、model_version、state、response。月页 model_version等于内层snapshot_version；季度页等于内层read_context.read_version。 |
| `preparing` | 相同范围身份、model_version=null、retry_after_ms，范围100–30000毫秒；没有response或金额。客户端按此间隔重试，取消时终止计时器。 |
| `failed` | 相同范围身份、model_version=null、reason=corrupt/production_failed；没有response或旧金额。客户端显示稳定错误，提供重试。 |

`snapshot_version` 和 `read_context.read_version` 是不透明的当前结果头，不是程序构建号，也不要求保存开放期旧正文。读取与查询的结果头由当前有效业务、相关档案、冻结选择、规则和展示合同组成；不受无关业务或无关模块构建变化影响。`read_context` 同时提供company_id/database_id/as_of，as_of为本地 Asia/Shanghai 实际查询日期。一次响应及其分页内容使用一致的读取范围。

线协议中的subject_id对应新business_id，voucher_version_id是包含正式result_id及generation的不透明定位令牌，employee_id/person_id等对应明确对象身份。它们保留旧字段名而不继承旧表结构。开放头原位替换时result_id保持、generation前进，旧generation正文不保存，旧令牌返回版本变化；闭期令牌按冻结引用精确保留。旧kind字符串仅作为相同业务含义的显示别名，转换集中在适配器，不能让领域规则依赖旧前端名称或用旧类型掩盖新业务。

旧 `read_semantics` 字段保留冻结 Schema 中的形状：开放期采用 current_known，已关期采用 frozen_adoption；当前展示或冻结附带当前管理资料分别标明；system_time_replay固定false。recorded_at/recording_period等字面量只说明字段含义，不要求建立操作日记。闭期金额及采用结果不能被当前档案或当前计算覆盖。

金额是规范 int64 整数分字符串；不得经过 Number、float或JSON小数。`null`为实际未知，省略仅适用Schema的非必填字段，`"0"`为已知零值。要求金额但新领域缺少必要事实时保留具体未解决状态；不得为了填满看板Schema补造金额。preparing只表示相关读取模型正在生成；事实缺失是稳定的业务缺项，不能靠无限重试假装仍在生成。报表缺项按第4.1节返回可查看的部分结果；其他页面按其严格合同返回明确未知或失败，不发送不满足合同的半页。

错误 JSON 含 `status:"rejected"、code:string、message:简体中文`；事实缺项按共同合同附结构化说明，不能泄漏 SQL、路径、凭据或原始请求正文。HTTP映射如下：

| 状态 | code与处理 |
| --- | --- |
| 400 | invalid_command：字段、参数组合或范围错误；保留所选范围供用户重试。 |
| 401 | owner_session_required或共同会话失效码；页面触发finance-session-expired。登录和同源传输不产生业务审批。 |
| 403 | same_origin_required或共同访问范围错误；不发送账务。 |
| 404 | dashboard_business_not_found/dashboard_voucher_not_found/dashboard_group_not_found/unknown_report_job：仅当前公司定位失败。dashboard_business_detail_unavailable：业务存在，但当前没有适用的进展明细，中文明确说明“本项暂无可展示的业务进展，请查看凭证或向 AI 会计询问。”；不表示记账失败。 |
| 409 | dashboard_snapshot_changed：重新读取主范围后再分页；preview_expired：重新取得只读核对或报表计划，不重复记账；report_job_not_ready：文件尚未成功；conflict：同一重试键指向不同计划；report_download_invalid：文件验证失败。 |
| 500 | response_contract_mismatch/content_integrity_failed/internal_error：停止使用受影响响应，不转成空业务。 |
| 503 | 读取准备失败或暂时繁忙；交付外壳failed由客户端转为稳定中文提示，重试仍查原范围。 |

## 4. 五页必须提供的内容及公开结果依赖

完整字段、子类型和必填项使用第 1 节冻结 Schema。本节规定业务含义及负责提供结果的模块；`dashboard`不得从领域私表重算这些字段。

| 页面／字段族 | 必须保持的业务含义 | 公开结果依赖 |
| --- | --- | --- |
| 简报 position | 当月收入、费用、账面盈亏和完整性；发生额不等于收付款额。 | ledger共享利润取数及正式期间结果。 |
| 简报 funds_overview、financial_position、long_term_assets | 月末资金、对外收付、内部划转；资产负债权益、银行与长期资产、构成、平衡状态及长期资产项数。资产负债构成浮窗为原有业务内容。 | ledger共享财务快照、money资金结果、assets正式资产卡片。 |
| 简报 workforce_cost | 员工和个人劳务成本、工资奖金、公司社保公积金、资本化劳务；个人代扣不重复计公司成本。 | people正式薪酬及ledger共享成本。 |
| 简报 activity_groups、collections.activity、activity_count/group_count/voucher_count | 同一正式对象、同一事项归组，混合收付按实际业务分项，收入与代收分开；活动分项、业务组和完整凭证分别计数。 | ledger正式凭证和义务、各领域业务标签、money精确收付关系。 |
| 简报 open_items、collections.open_items | 截至所选月末的完整应收、应付和分类；不同对象应收应付不抵消，期初及跨月余额不遗漏。社保公积金按人员、工资月、个人／单位分项。投资产生的正式往来按相同账务含义接入，不另做项目资金管理表。 | ledger义务、清偿及正式账面余额，people身份和薪酬关系。 |
| 简报 management_commentary_details、risks、owner_tasks | 当前有效经营说明、实际风险及老板确需补资料、决定或外部办理的事项。AI内部核对不生成老板任务。 | records当前说明及未解决事项、领域状态；只读月度核对另按第7节处理。 |
| 简报 focused_*、collections.vouchers | 精确原凭证和业务定位、完整分录、对象、资产链接；独立凭证清单与业务配对凭证分开。 | ledger公开凭证查询、对象及assets公开身份。 |
| 资金顶层及collections.accounts | 全公司月初／月末资金、对外收付及内部划转；账户卡片流入流出包含本账户划转，不能把账户金额当全公司对外金额。 | money账户、实际资金发生与核对，ledger资金余额。 |
| 资金collections.movements/statements | 按业务与按原始银行流水两种清单；原用途、摘要、对象、日期及方向，批次保留真实成员与合计。流水原文不能换成AI猜测。 | money实际收付、银行行及批次关系，领域业务说明。 |
| 资金investments及investment_products/events | 现有货币基金账面成本、申购／赎回确认、确认收益、实际付款／到账分别表达；成本不是市值。 | financing投资结果、money真实收付及ledger成本。合作投资不混入货币基金旧列，其正式金额通过既有财务汇总、真实资金和凭证体现。 |
| 员工employees、workforce_cost、outstanding_remuneration_fen | 本月员工薪酬成本与在册人数、工资奖金、公司社保公积金、实际发薪；截至月末未付包含以前月份工资和个人劳务及资本化劳务。 | people月度关系／薪酬、ledger完整清偿、money实际付款。 |
| 员工collections.employees/labor_workers/labor_sources | 正式人员身份及所选月关系区间、工资奖金和代扣各项、报酬费用与资本化分开、当月及跨月清偿。月度日期不补造日。 | people当前有效关系及薪酬、ledger义务和分配，identity必要显示档案。 |
| 资产汇总、fixed/intangible、collections.assets/projects | 资产及项目成本、累计与本月折旧摊销、所选月末价值、在用／待启用／退出及本月变动，未进入卡片的项目投入分别表达。 | assets正式生命周期与成本、ledger资产余额及成本消耗。 |
| 资产payment_summary、disposal/retirement | 区分本项、整批和项目来源款项，不无依据分摊整批付款；真实处置金额、收益损失和退出时价值。 | assets正式处置、ledger义务清偿、money实际收付。 |
| 报表organization/period/summary/statements/export | 同一季度的资产负债、利润、现金流三表及摘要；税务局模板与默认样式使用同一金额，生成文件对应所见结果。 | storage.company_metadata、ledger共享财务快照、taxes已实现计税／实际税额，records报表编制资料，reading报表文件计划。 |

`reading.public`交给适配器的语义结果必须包含：公司与期间上下文、当前结果头、完整财务摘要、业务组及成员、精确凭证、账户与流水、人员及劳务、资产及项目、业务义务与真实清偿、季度报表、只读关账检查和单任务文件状态。查询请求及公共类型按 [接口合同](03-contracts.md)集中定义；本文的页面字段不得反向进入领域规则。

合作项目是投资理财的一种记账方式。出资、合同约定的收益和损失、返还及退出由投资业务规则形成正式结果，既有总账、报表、资金和凭证按同一结果读取，不漏记、不重复汇总。经营者代收代付与公司账户真实收付分别核算，不能把合作方代管资金算成公司银行存款，也不能凭空增加公司现金流。看板不新增合作专用查询、项目资金展示或独立详情页。

### 4.1 三表完整读取与事实缺项

三表唯一金额来源为[统一报表合同](contracts/reporting.schema.json)的FinancialSnapshot，规则见[财务报表](05b-financial-reporting.md)。balance_sheet、profit_statement、cash_flow_statement分别映射旧statements中同名key；行号及行名原样保留。资产负债表ending/beginning对应旧列ending_fen/beginning_fen；利润和现金流current/year_to_date对应current_fen/year_to_date_fen。KnownAmount只取amount_fen；UnestablishedAmount映射该单元格null，不能转0或省略整行。摘要直接取BS30/47/53的ending、PL32的current/year_to_date、CF20.current和CF22.current，依次对应旧summary的资产、负债、负债权益、季度利润、累计利润、现金变动和期末现金，不重新汇总凭证。

FinancialSnapshot.status=ready时旧status/readiness_state均ready。status=partial时模型仍已生成，首读外壳为ready，内层status=blocked、readiness_state=blocked、status_label="资料待核对"、headline="部分报表资料待核对"；message列出issues中的具体缺项和失败勾稽，不暗示后台仍在计算。已知三表行和摘要照常显示，未知单元格沿现有null显示；has_amount按已知非零单元格确定，含null行仍保留。现有ReportsView已经保留含null行，无需改变主布局或另建部分报表页。

partial时export.available=false、preview_digest=null、epochs=null；不能创建完整XLSX计划，也不能把缺失年初或现金流分类伪装成完整报表。draft在开放季度或partial时为true，全部月份冻结且完整时为false；它只表达报表性质，不表示待批准。checked_at为本次读取时间。只有真实模型尚在准备时使用preparing，生成损坏/失败才使用failed。

同一读快照内先定位本页身份，再批量补充凭证、identity.public对象、依据和必要余额。不能每张卡片单独调用领域，也不能先取全部历史正文再让页面丢弃。期间结果、跨月清偿和冻结选择由负责模块决定；看板只使用它们的明确结果。

## 5. 展开、筛选与局部状态

### 5.1 必须接通的原有展开

- 本月发生和应收应付先读组摘要，展开组才读成员。成员首次20、自动500；组收起取消未完成请求。逐成员业务进展必须另行打开，不因展开组、缓存重开或刷新自动读取。
- 组内凭证预览和“按凭证”展开显示完整原凭证及分录；已加载凭证直接本地打开，不请求全月简报、不预读下一页、不改变独立凭证游标。独立清单手动首次切入才初始化，分页20，“全部”沿500条有界游标完整读取。
- 业务进展保留所选月末义务原额、实际支付、抵销／代付、剩余及状态，也保留后续真实清偿的当前截止期。被替代的精确旧分项从其原采用月核对原义务，不能拿当前新分项代替；后续清偿仍按实际截止期查询。`settlement_view=historical`选择所选截止期事件，current选择当前截止期事件；二者均不表示开放期操作日志。
- 精确分项先限制业务、对象、用途、金额、义务及收付范围，再计数分页。同一笔混合支付的其他人员或其他事项不得进入所选分项。没有分项参数时才返回整笔业务。
- 员工薪酬补充、资产卡片／处置／付款补充和完整三表已在本页结果中；点击只改变显示。劳务人卡展开才读该人的labor_sources，再打开具体收付款事项才读business-status。
- 银行批次悬停或展开复用流水行已返回的成员、个人金额和批次合计；没有额外请求或第二份批次缓存。未能逐行分摊时保留范围说明，不能假配。
- 资产负债构成浮窗与用工成本拆分继续使用正式金额；不归类为可删诊断信息。

`business-status`响应以冻结Schema中的Owner窄投影为准：`latest_source`只含period/deleted，`review`只含status；当前结果及冻结结果保留金额和对应期间，`settlements/current_followups`与`settlement_events`表达真实义务、付款和闭期更正。不得恢复原始事实修订、所有计算版本、逐次处置或操作日记来凑这些字段。

合作等新业务尚无适用的通用进展明细时，沿现有可空定位和入口存在性规则不提供该项进展按钮；旧链接或直接请求返回第3节明确的明细不可用说明。不能把投资资金假装成普通债务进度，不能显示为“AI 会计核对中”来掩盖没有该项展示，也不跳转到新增页面。正常记账、业务金额、完整凭证预览及精确凭证定位继续可用。

### 5.2 本地状态与取消

公司或月份变化立即取消旧请求并清空旧数据；同范围刷新可暂存已有内容，但新context和主响应均通过范围核对后才采用。失败、公司／期间变化或快照变化时不继续使用旧分页。即使A→B→A，旧A请求也不能写回新的A选择。

账户、员工和资产筛选只替换相关集合首屏，保留全公司汇总和无关集合。卡片／列表、按业务／流水、三表及税务模板切换保持本地切换。切走模块取消其自动续页；已经打开的业务详情可以继续后台读取，收起取消；凭证进展浮窗在刷新或模块切换时关闭并取消。

已成功的同范围详情可以复用；缓存键包含公司、期间、结果头、业务、精确凭证／分项及收付视图。关闭取消的未完成响应不写缓存。新增页通过独立模块挂载和按需读取，不触发旧页重新初始化。

续页仅向响应式集合追加本页记录，凭证Map按精确结果版本新增一次。无关续页不复制累计列表、不遍历整个预览索引、不清除当前浮窗。自动续页若游标未推进，停止并显示可重试错误，不无限循环。

## 6. 更新与一致性

正式结果、相关组件目标版本和待更新范围在同公司事务中一起生效。提交后后台唤醒`reading`，它通过公开入口生成自己的当前模型；账务模块不调用看板分页、缓存或重建步骤。只维护当前目标与待完成范围，旧任务不能覆盖新目标或清除新待办。

生成与发布均核对目标结果头。发布前来源又变化时丢弃过时产物并接续最新目标；无变化的组件继续复用。页面组合标识只说明一致读取范围，不等于每次重新计算整页或整个公司。

首读缺少当前模型返回preparing；失败返回failed，普通GET不自动重启。老板主动刷新通过对应主页面路径加`/retry`的POST重新准备该页失败组件，正文采用同页查询的字符串参数，明确公司及月份或季度，不接受分页、游标或旧版本。响应仍用同页交付外壳；重复刷新不重启已就绪或正在生成的组件，随后只用GET等待结果。续页的expected_version过期返回409，由客户端重读主范围。不重发记账命令来修复读取，不能把旧模型挂上新标识。冻结期使用当时正式结果及必要依据，后续冲正和真实付款按实际入账期间展示。

## 7. 月度核对改为只读关账检查

保留 `owner_review_request.preview_digest` 和 `/api/dashboard/close-review` 的外部字段名，内部改为当前只读检查头。标识由公司、数据库、月份、相关正式结果头、检查合同及检查内容计算为64位小写十六进制字符串；不绑定批准、密码、授权或老板“已确认”，不保存开放期旧预览正文，也不作为关账许可。

reading复用同版简报的完整金额、实际资金核对状态和当前未解事项形成`PublicOwnerReview`；关账及覆盖状态只取真实冻结边界。只读检查可查看本月六项金额、按业务性质分组的数量金额及当前问题状态；`status=ready/ai_reviewing`表示检查结果和仍有问题，不表示负责人已批准。

只有开放月份已经有与主响应同版的完整金额和当前检查内容时，简报返回owner_review_request；没有则为null。此引用只是可查看入口，不另造老板审批待办。真实缺项仍由既有owner_tasks及AI办理状态表达。读取核对内容不要求用户先执行旧preview_close命令。

| 旧字段或状态 | 新体系语义 |
| --- | --- |
| `prepared` | 当前只读检查内容已准备好，owner_review为同版内容；无批准含义。 |
| `unprepared` | 当前未形成可供显示的完整内容；owner_review与preview_digest为null，reason说明实际原因。不把未知的必填金额填零。 |
| `stale` | 请求的只读标识已过期；owner_review=null，preview_digest回显请求标识，提示刷新，不自动跟随新内容。 |
| `closed/covered` | 所选月已关闭或由后续冻结覆盖，owner_review=null；close_digest及covered_by仅引用真实冻结结果，不是批准凭据，无真实覆盖关系不返回covered。closed的preview_digest回显本次请求标识，以通过现有客户端关联检查；covered可为null。 |
| `owner_review.materials` | 当前已声明资料范围和接收情况；没有明确数量定义的资料不列项，不伪造expected/received。 |
| `owner_review.followup_count` | 当前有实际影响的未解事项数量，同一事项去重；不计算“待批准人数”或文件生成次数。 |

简报中的“确认后告知AI会计”表示老板核对现实后的自愿反馈，不形成阻断。核对面板说明为“请核对以下金额和业务。发现差异时，请告知AI会计继续核查。”，保留原有标题、六项金额、列表、布局及样式；不请求批准或密码。

## 8. 报表生成、状态和下载

预览返回的`export`包含available、file_name、preview_digest、epochs。available只有在该季度所需业务结果完整且可生成文件时为true；无关申报或老板批准不是导出前提。preview_digest是准确报表计划标识；epochs的accounting取该季度financial组件的ready_head，material固定0，management取公司profile_seq，不增加旧审计lane或全库扫描。导出只提供固定tax_bureau三表模板；页面默认样式与税务模板是同一金额的本地展示切换，不分别生成工作簿。显式提交default文件方案返回report_template_unsupported。

### 8.1 创建任务

`POST /api/local/report-export`，JSON只含以下必填字段：

| 字段 | 类型和含义 |
| --- | --- |
| company_id | 当前公司身份。 |
| year、quarter | 与预览一致的整数年份及1–4季度。 |
| preview_digest | 64位小写十六进制字符串，绑定该公司的报表计划、输入范围和输出内容。 |
| epochs | `{accounting:非负安全整数, material:非负安全整数, management:非负安全整数}`，原样回传预览。 |
| request_id | 非空不透明重试键，同一计划的重试使用同一键；不是老板批准。 |

成功HTTP200，响应采用冻结Schema的`report_export_receipt`，精确字段为`status:"queued"、job_id:string、preview_digest:string`。queued表示任务已经登记，不保证尚未执行。当前同一计划已有pending/running/succeeded任务时，所有客户端直接复用原job_id，不因request_id不同替换或重复排队；同请求重试也返回它。同键不同计划返回conflict。failed均为已经停止的任务，新的显式生成请求可以替换；原失败请求重试仍返回原任务，不暗中重新执行。计划过期返回409/preview_expired，不创建第二份任务、不导出当前新结果冒充旧预览。

### 8.2 单任务状态

`GET /api/local/report-export/{job_id}/status?company_id=...`，只接受company_id查询参数，job_id为URL编码后的不透明身份，解码后非空且不含路径分隔符。

响应使用冻结Schema的`report_export_status`：`schema_version:1、company_id、database_id、job_id、status:pending/running/succeeded/failed、attempts:非负整数、error_code:string|null、error_message:string|null`。只查询当前公司这个报表任务；不读取全公司任务、不展开来源和文件正文。可自动重试的等待使用pending，最多实际尝试3次；failed始终表示已经停止，包括不能重试的错误，因此实际attempts可以小于3。页面收到failed即停止轮询，由用户原按钮重新生成；刷新不重复排队。

现有前端每1200毫秒查询一次；公司、季度或刷新变化时取消轮询，迟到状态不能触发旧文件下载。轮询仅为观察任务，不是核算和文件重试命令。失效任务返回409/preview_expired，不把内部expired状态塞入不接受该枚举的旧Schema。

### 8.3 已验证下载

`GET /api/local/report-export/{job_id}/download?company_id=...`，参数范围同上；成功返回XLSX二进制，Content-Type为`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`，Content-Disposition使用该计划的file_name。客户端不提供服务器文件路径。

任务必须succeeded且计划仍然有效，文件公司／数据库／任务／计划身份、文件摘要、工作簿和任务保存结果全部对应后才交付。未成功返回409/report_job_not_ready，任务不存在返回404/unknown_report_job，校验失败返回409/report_download_invalid。

创建时固定所见报表的计划。开放期相关账务被更正或撤去后，旧计划与任务失效，取消未完成生成并清理旧文件；仍在当前任务槽中的过期任务，其状态查询和下载返回409/preview_expired，客户端重新读取报表再生成，不能用原任务偷偷交付新的数字。当前过期任务只保留身份及失败原因，不保存旧错误金额和正文；被新任务替换并清理后，原job_id按未知任务返回404，不永久积累失效身份。每公司、季度及报表格式只维护当前有效计划和交付，成功文件可保留当前一份。

闭期计划以精确冻结结果为业务依据，不因无关开放期变化而失效；若实际采用的公司名称等当前补充资料变化，仍按计划真实依赖判断是否失效，不能把当前新显示套到旧计划。生成完成、发布前再次核对计划头；下载读取并核验完整文件后，在发送响应前最后核对计划头，已失效的不发送。该核对时点界定响应快照，之后的新业务使后续请求失效，不回收已经交付的字节。生成、文件读取及网络发送均不占用业务写事务。用户已下载到外部的文件不在程序自动删除范围内。

源数据已正确保存而生成失败，只重试仍有效的计划，不重新记账；可重试错误最多尝试3次，不能重试的错误可以提前进入failed。已成功文件后来确定缺失或损坏时，任务也转为failed，保留原计划与实际尝试次数，由新的显式请求重新生成；不把仍有效的业务计划说成过期，也不改动外部有效备份或previous。暂时无法访问且不能确认损坏时，保留原交付供重试下载。过期文件清理失败保留当前待清理状态并禁止下载旧文件，后续正常后台轮次继续幂等尝试；清理次数饱和记录，不构成永久停止。导出计划和最小交付回执只服务当前有效交付，不成为开放期账务或纠错过程档案。

## 9. 明确的展开差异与用户说明

以下处理是依新保存原则作出的具体适配，不是把所有详情列为暂不实现：

| 位置 | 接入处理及影响 |
| --- | --- |
| 月度核对中的密码批准文案 | 按第7节改为只读检查及差异反馈。普通凭证、金额、核对内容、布局和样式继续提供。 |
| “之前的经营说明已过期”展开 | 不保留开放期过期旧正文。当前有效说明仍正常显示；当前说明失效且无新有效正文时，current/latest为null、status=stale，旧正文展开不出现。向用户列明“无法比较过期说明原文”的具体差异，用户以后可决定是否增加该保留能力。 |
| 已关后的有效补充说明 | 保留实际采用的冻结说明及仍有效的补充内容，supplements据此提供；不能把它们误当开放期纠错日志删除。 |
| 开放期被撤去业务及旧凭证的历史提示 | 旧错误正文不恢复；重读列表后不再列旧行，持有旧结果标识先返回409，刷新后的无效精确业务返回404。已有原凭证编号用于当前有效修改时保留；闭期原凭证、冲正及后续真实收付继续可查。向用户说明不再从已撤去开放期误记查旧正文。 |
| 普通凭证分录、组成员、真实清偿、劳务逐笔、资产补充 | 必须接通，不属于上列缺口。若某具体字段确实缺少来源，开发者列明位置、原因和老板／顾问影响，由用户决定补齐；不能因此交付空列表或推翻本节必须提供的能力。 |
| 合作投资的业务进展 | 本轮只实现记账及既有汇总、资金和凭证接入，不增加合作专页、板块或定制进度。既有通用详情确无适用内容时按第5.1节明确说明；不阻断记账、不伪造债务状态。 |
| 顾问必要来源查阅 | 按第11节独立只读入口取得具体业务的必要来源；不修改或批准，不向老板主界面加入内部哈希和过程JSON。 |

没有依据的显示字段不能通过恢复旧过程日志来补齐。实际缺口记录随受影响范围维护，说明用户可得到什么、暂时不能比较什么；不反复提出同一取舍，也不把无关业务开发暂停。

## 10. 性能目标和验收

验收重点是架构合理、工作量与实际业务范围相称、看板流畅，以及后台状态清楚、失败能够接续。后台整月处理不设10秒门槛；未经老板确认的耗时数字、样本规模和运行次数均不作为交付硬门槛。已有数值只作历史测量记录，不据此继续堆叠优化。

验证使用真实模块和Vue页面，记录实际环境、代码来源、查询范围、业务数量、原始耗时及失败。样本规模和次数根据改动风险选择并说明理由，不要求每次跑完全部组合。可使用现有12／48／120个月、每月1000笔业务的压力样本；旧源码成绩不代替当前验证。需要比较耗时时，保持起止点和工作内容一致，不与建库、全量测试或插桩争用资源。恢复后从空读取模型开始的冷准备单列，不能混入已就绪页面的刷新结果。

| 场景 | 合成规模、终点与目标 |
| --- | --- |
| 五页打开和刷新 | 检查点击至共享context、主数字、默认20条及全部默认可见内容更新并稳定的完整过程。页面操作流畅，不预读未展开的成员和进展，不因无关历史增加扫描或重算。 |
| 局部交互 | 检查组及成员首开、业务进展、凭证分页／全部、精确直跳、筛选、银行批次、劳务展开和三表／模板切换。迟到响应不串范围，续页只追加新记录，实际Vue渲染和DOM工作一并观察。 |
| 单笔提交后的相关模型更新 | 从正式提交完成观察相关模型到ready；只更新实际受影响范围。preparing明确表示尚未完成，失败有具体状态并能接续，不用旧金额或空白页冒充成功。 |
| 整月整理和归档 | 观察正式账务提交后至最终文档和所需模型就绪的完整过程，包含首次月页准备及实际需要的季度更新。允许后台稍慢，检查重复读取、解析、计算和无必要改写；涉及历史业务时说明实际涉及范围，不省略必要处理来换取数字。 |

“全部凭证”必须真实覆盖完整所选月份，不能只检查首次响应。重建读模型、生成XLSX、备份分别记录。保留全部慢样本、失败和超时；需要统计P95时说明样本数并采用nearest-rank。后台未就绪、缺少内容、金额错误或失败不能恢复，属于功能问题，不能以耗时放宽为由忽略。

同时验证实际工作量：与当前查询无关的历史增加，不应增加正文扫描、解析或重算；确实涉及的历史采用、余额、付款和冻结依据按实际范围处理。默认页不预读组成员、业务进展和独立凭证；续页只追加新记录，凭证索引不重复克隆。新增页、记账及文件任务同时工作时，原有页面仍应流畅且状态明确。每项优化先从反面检查必要性，优先合并重复工作和简化职责；收益不足且增加复杂度的特殊路径应退出。

### 10.1 必须先有预期的功能样例

1. 空公司；两公司同名对象；公司／月份切换及A→B→A迟到响应；失效版本、错误筛选和游标跨范围均不混入数据。
2. 25笔同账户收付只加载20笔但汇总覆盖25笔；内部划转不算对外收付；银行缺资料、明确零业务及读取失败分别表达；整批成员不串人。
3. 同一凭证含工资、个税、社保等分项，分类金额各自准确，配对预览仍是完整原凭证；组首次20、续500和尾页无重复遗漏。
4. 上月费用本月付款、部分清偿、退款和抵销；所选月末义务及当前后续进展分别正确；社保个人／单位跨月按正式人员身份拆解；无凭证义务仍可查。
5. 开放期修改沿原凭证编号，旧标识失效；撤去后不能从旧页取出旧错误；闭期原结果、后续冲正及真实付款均按各自期间保留。
6. 同月劳务转员工、停薪及日期仅明确月份；劳务工资不同口径不重复计；月末未付覆盖以前月份和资本化劳务；精确人员不受默认筛选遮蔽。
7. 在用、待启用、已退出资产，整批／项目付款，折旧摊销及成本调整；精确退出资产能定位，切换列表不重复请求。
8. 同一报表三表、模板、摘要和XLSX逐字段一致；旧计划、同请求重试、最终失败、取消轮询、跨公司job_id及被改文件下载均有预期错误。
9. 只读月度核对的prepared/unprepared/stale/closed/covered，无批准记录、密码前置或关账副作用；unknown金额不变零，缺少资料计数不伪造。
10. 调整账务内部实现而不变公开合同，五页仍正确且不全量失效；优化读取不能修改正式账务。独立新增页面不改变旧页默认请求和缓存生命周期。
11. 合作投资出资、收益、损失和退出的正式金额进入既有总账、报表、真实资金及完整凭证，不增合作页面或专用展示；没有适用进展明细时明确说明，不伪造债务状态，也不影响记账和凭证读取。

现有合成测试可按上述预期复用：`dashboard-context-v2`、`dashboard-delivery`、`brief-group-members`、`brief-vouchers-loading`、`voucher-progress`、`funds-details`、`assets-filters`、`reports-template-ui`，以及后端分区／精确业务范围／跨月清偿／读模型输出对照测试。旧批准、操作历史或私有表计数断言按新方向退出，不用修改测试预期掩盖金额或行为缺失。

### 10.2 模块交付内容

交付 `src/ai_accounting_modular/dashboard/public.py`、reading公开结果到本快照的适配、严格请求及响应校验、独立HTTP测试和共同合成样例；不得导入旧总服务或领域私有代码。五页保持既有主要内容、布局和样式；只读核对文案、精确业务范围、版本接续、失败刷新和任务轮询按本合同接通，必要API/生成类型及第11节顾问来源入口同步调整，不为合作另设dispatch。

验证包含冻结合同校验、前端类型检查与正式构建、对应行为测试、真实模块集成，以及上述体验和工作量检查。交付以实际证据为准，说明结果、未解决问题及有来源的展开缺口；设计文件存在或旧测试曾通过不代表交付完成。

## 11. 顾问必要来源的独立只读入口

现有五页已能看凭证、分录、业务明细和报表，但没有原件打开页面；旧HTTP trace不是现成的顾问页面。首交新增`/review/materials?company_id=...&business_id=...`深链，由AI在整理具体问题时提供，不修改主导航或五页布局。查精确正式采用时再带result_id和generation，必须成对；不增加角色、批准、修改或关账能力。

请求/响应唯一真源为[review-materials.schema.json](contracts/review-materials.schema.json)，合成预期见[来源查阅样例](examples/review-materials-cases.json)。页面GET`/api/review/materials`，接受ReviewMaterialsQuery的company_id/business_id、可选result_id/generation、limit默认20且上限500、cursor及expected_version；拒绝重复或未列参数。cursor必须带expected_version。dashboard调用`reading.detail("review_materials", ContextScope{company_id}, query)`，该读取入口建立一次ReadContext并传给内部全部公开子查询；先使用公开业务查询核对公司与业务，再经ledger精确结果、records.resolve_sources取得必要来源，不能查私表或由HTTP分别开数据库。

未指定结果时列当前事实来源及当前有效正式结果的必要来源；指定时只列该精确采用真正保留的来源，不把当前改用的新原件套到旧冻结凭证。响应Selection明确current/frozen/open_result，当前带fact_seq，精确结果带result_id/generation/posting_period。开放旧generation已被替换时409/dashboard_snapshot_changed，不恢复错误正文；其他公司、不属于该业务的结果或不存在业务404/review_materials_not_found。冻结结果的必要来源不因当前事实变化失效；实际使用的显示资料变化仍使相应显示头变化。

来源按source_id稳定排序，一来源一行，locations合并去重；完整count为唯一来源数，不是位置或本页行数。page.collection_version等于snapshot_version，续页绑定同一公司/数据库/业务/精确结果；失效409。默认只读元数据和受控位置的存在/大小，不读取全部来源字节。available表示可以尝试打开，不代替完整内容校验；已发现的缺文件或摘要不符在unavailable_reason列明，不能隐去该行。

打开某行才GET`/api/review/sources/{source_id}/content`。source_id只取路径，其余参数为SourceContentQuery中的company_id/business_id、同一精确结果对（如有）和expected_version。服务端在相同选择中核对该source确实被引用；找不到或越公司均404/review_source_not_found。records从登记身份解析当前受控路径，取得读取租约；禁止客户端给路径、绝对路径/父目录/符号链接越界。移动归档时仍读取同一内容。发送前流式读取到受控临时缓冲并核对完整SHA-256/大小及范围头，缺文件409/source_unavailable，内容不符409/source_content_mismatch，范围变更409/dashboard_snapshot_changed；错误字节不发送，文件读取不占业务写事务。

owner_statement返回HTTP200及SourceTextContent JSON，UTF-8正文摘要必须相符，Vue仅按文本渲染。file返回HTTP200原字节，统一Content-Type=application/octet-stream、Content-Disposition=attachment（安全编码original_name）、Content-Length为实际大小，并设X-Content-Type-Options=nosniff、Cache-Control=no-store。X-Accounting-Company-Id/Database-Id/Source-Id及X-Content-SHA256提供SourceFileReceipt测试回执；不返回服务器路径。首交文件通过浏览器下载打开，HTML/SVG/PDF/Office均不注入页面或执行；不另造文件预览/转码系统。正文请求取消及页面切换释放租约，迟到响应不写回旧范围。

顾问可看到具体业务、当前或冻结选择、来源名称、页/表/行位置、老板补充原文及原件下载，并结合原有凭证和三表提出问题。内部摘要/ID只作校验和关联，不要求顾问学习。验收覆盖25来源20+5完整分页、当前改B而冻结仍可读A、开放旧代409、跨公司/未引用来源404、缺原件/篡改409、归档移动接续及文本/文件不执行。这个入口只在打开深链时请求，不增加五页默认请求或影响其流畅使用。
