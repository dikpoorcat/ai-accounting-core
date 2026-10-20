<!-- @format -->

# 公开接口与标准业务文档

接口采用一个业务办理入口、几个明确的读取/资料/文件入口。会计 AI 不负责计算内核内部标识、依次发布结果、刷新缓存或修复重复请求。MCP 与 CLI 共用同一个应用服务，不分别实现业务。

机器可读真源为 [公共类型](contracts/core.schema.json)、[业务事实](contracts/business-facts.schema.json) 和 [既有看板响应](contracts/dashboard-response-v1.json)。JSON Schema 负责字段形状；本文及 [业务规则](05-business-rules.md) 负责跨字段、引用和核算约束。两类检查都通过才能办理，不能用 `json_valid` 或 Schema 校验代替完整业务校验。

## 通用字段

| 类型 | 规定 |
| --- | --- |
| `Id` | 小写带连字符 UUID。业务、对象、来源、结果和数据库使用不同字段，不能混用。 |
| `Fen` | 线上及标准文档采用规范整数分字符串，如 `"12345"`、`"-100"`；禁止小数点、千分号、前导零和负零。范围为有符号 64 位整数。Python/SQLite 内部为严格整数，不接受 bool/float。正数/非负数使用对应约束。 |
| `Month` | `YYYY-MM`，真实年份 0001–9999、月份 01–12。不能把缺失月份补成当前月。 |
| `Date` | 有效公历 `YYYY-MM-DD`；确实知道日才填写。仅知月份时不填本字段，保留月份。 |
| `Rate` / `Percent` | 规范十进制字符串，最多 18 位小数，分别限 0–1 / 0–100；用 Decimal 或整数比运算。禁止二进制浮点、NaN、Infinity、科学计数法。 |
| `seq` | 当前序号，创建后从 1 开始递增；修改以 `expected_seq` 比较，不作为保留历史正文的理由。 |
| `digest` | 小写 SHA-256 十六进制 64 位，基于明确的规范内容；不是业务身份。 |
| `BusinessRef` | `{business_id, slot}`。`slot` 是该业务公开的结果槽名，如 `net_pay`、`principal`，不能传任意科目。程序按当前关系解析；闭期采用时内部固定到 `result_id`。 |

JSON 禁止重复键，所有对象禁止未声明字段；UTF-8。规范摘要在解析后对键排序、紧凑分隔、禁止 NaN，金额保持字符串，数组只有业务上无序的集合才按合同规定排序。不得使用展示摘要、文件路径、文件行号或姓名作去重键。

## 完整业务办理

`accounting_apply` 接收 `ApplyRequest`：

| 字段 | 含义与约束 |
| --- | --- |
| `contract` | 固定 `ai-accounting-modular/1`。不是现有 `ai-accounting-kernel/2` 的别名，不接受旧库或旧合同。 |
| `company_id` | 本次单公司工作范围，必须与打开的公司库一致。 |
| `command_id` | 本次请求 UUID，用于当前接续和诊断，不建立永久命令流水。 |
| `mutations` | 0–500 项。每项是 create/replace/withdraw/restore 之一；同单一个 business_id 只能出现一次。仅保存对象、来源或当前待办时可为空，但整单不能无任何内容。 |
| `objects` | 可选，最多 500 个本单需要新建/更新的对象。逐个比较对象当前序号，与业务同事务保存。 |
| `sources` | 可选，最多500项本单采用的来源描述。文件须已在指定公司目录，内容摘要须实际匹配；来源 UUID 可跨业务复用。 |
| `notes` | 可选，公司现实说明或月份经营说明的当前正确正文，含 scope、expected_seq、text、evidence_ids；月份说明还须 expected_basis_head 与 AI 实际读到的依据一致，程序比较后绑定该 head，替换不留开放旧版。 |
| `unresolved` | 可选，当前未解事项的完整替换集合；出现时必须同时给 unresolved_scope 和 expected_scope_head，按该范围当前头比较。省略不改，显式空数组清空该范围；不追加过去的问答。 |
| `correction_period` | 本单确需更正闭期时必填，为开放月份，不早于受影响闭期；普通开放期更新不填。 |
| `explanation` | 闭期冲正的简明事实原因，最多 2,000 字；作为正式冲正说明，开放期不要求写纠错经过。 |

`create/replace/restore` 提交完整 `Fact`，不是字段补丁：`business_id, kind, period, evidence_ids, data`，可选 `effective_on`。kind 对应唯一严格 data 变体。`withdraw` 只提交 business_id 和 expected_seq。create 的 expected_seq=0，其余为当前正序号。业务 ID 在整理文档时由客户端库自动生成 UUID 并保存，AI 不手工拼接身份；响应丢失沿用它，不能再生成一个 ID 重试。

`objects` 是真实人员、交易方、账户、项目、资产及产品的稳定身份。名称可以变，身份不能跟着变。对象建档并不自动产生业务凭证；人员在册关系和资产启用仍分别是业务事实。看板所需档案可先保存未知管理项，不能仅因缺身份证或手机号拒绝本已明确的会计金额。

同一原子单包含的相互引用允许指向本单前面或后面的业务，程序统一建图后按拓扑计算。整单成功或失败。一个大月包可包含多个明确独立的单元，但调用方必须逐单接收结果，不能把已处理部分称为整月完成。

### 防重规则

对每个变更计算规范内容摘要，包含业务事实和必要来源身份，不含 command_id 和排序无关的字段顺序。业务引用里的冗余显示标签不参与核算事实摘要；对象自身的 name、说明自身的 text 和各自来源边都参与其内容摘要，不能把真正改名或更正说明误判为幂等。

- create：身份不存在才创建；已有当前内容完全相同则返回实际当前结果；同身份不同内容冲突，不能自动另开一笔。
- replace：当前 seq=expected_seq 时原子替换；当前 seq=expected_seq+1 且操作及内容摘要相同，视为回复丢失后的重试；其余返回当前 seq 和 `conflict`。
- withdraw：当前有效且序号匹配时按业务条件撤去；已撤去且恰为该次重试则返回已撤去。旧创建请求命中撤去墓碑必须冲突。
- restore：必须显式提供撤去身份的当前 seq 和完整正确事实；满足依赖条件后恢复。不能以 create 绕过墓碑。

检查覆盖业务、对象、来源和待办 scope 全部参与者，先分类为“当前已满足且无需修改”“本单需修改”“恰为已提交单的重试”。已有相同 create A 加新 create B 是合法新单，A 复用、B 新建；相同单重试不能重新创建 B。任何需修改项的前提已失效，则整单冲突，不执行剩余项。对象有自己的 seq，待办范围有 scope head；来源固定内容只能复用或另建，不能被新请求覆盖。

重试只保参与者当前头的最新单元标识、前提和摘要，不保存全部请求、旧正文或旧结果。相同事实不代表核算仍有效：采用结果另存 calculation_key/revision 和 basis_digest；规则修订或上游结果变化按明确影响范围重新采用，不能靠变更 command_id 或乱改事实绕过。闭期不会因程序升级自动覆盖。

### 业务响应

`ApplyResult` 包含 contract/company_id/command_id，`accounting`、`documents`、`reading` 三个独立状态，受理业务的当前身份/seq/result_id、影响期间、当前未解事项及 `fact_issues`。participants 总是返回本单对象当前 seq/digest、说明当前 seq/state 及待办 scope 当前 head；首次创建后也能直接接续。金额和凭证存在于正式结果读取，不把巨量分录塞入每次办理响应。

`changes=committed` 表明本单有效内容已保存，`accounting=saved` 表明其中正式核算确有提交；仅存资料/待办或完全幂等的单可 accounting=unchanged。`documents=ready` 表明对应最终文档已持久发布；保存内容与归档都满足才 completion=complete，归档未完为 saved_documents_pending。读取可以 preparing，不阻止这项完整办结。缺项、冲突或提交失败时不返回虚假的成功业务。内部错误不自动生成老板待办。

`fact_issues` 每项含 `code, business_id?, field, meaning, message, allowed_precision, reusable_sources, blocking_scope`。field 使用实际 JSON Pointer；指出会改变核算的缺项、允许精度和可复用的来源身份。AI 先复用资料，再追问真正缺失的现实信息；程序不输出“让老板确认错误码”一类指令。

月份说明依据头由 accounting_context.note_basis_heads 提供，即使该月还没有说明也可取得。它只覆盖该月相关正式业务及核算选择，不包含说明自身、文件任务或读取生成时间。AI 读取该头及对应业务结果后写说明；提交时比较事务最终将采用的依据头，过期或同单改变了依据则冲突，不把基于旧数字的文字绑定到新账。需要先记业务再解释时，先办理并读取实际结果，随后保存说明即可。自动失效同样推进说明 seq 并清掉旧正文/来源边；公司现实背景没有金额依据头，不随普通账务提交失效。

## 其他公开入口

下表是应用层可调用能力；CLI 使用对应连字符命令。MCP 不再暴露原来登记/准备/计算/发布等多段内部手续。

| 入口 | 输入 | 返回及作用 |
| --- | --- | --- |
| `accounting_schema` | SchemaRequest：可选 kinds、contract | SchemaResult：默认仅合同及支持目录，指定 kinds 才返回对应输入 Schema 的自包含依赖闭包，不返回旧接口或内部 PostingPlan。未知 kind 拒绝，不偷偷省略。 |
| `accounting_context` | ContextRequest：可选 company_id、note_scopes、issue_scopes | 当前公司身份、支持范围、开放/关闭月、有效说明与当前待办及其 seq/head。省略范围默认公司说明和当前开放月；不全库加载档案。 |
| `accounting_objects` | ObjectsRequest：get 指定 IDs；find 指定 kind、可选 name_contains、limit/cursor | CurrentObject 含 seq/digest 和完整当前档案；检索名称只供选择，不自动绑定同名人。 |
| `accounting_get` | company_id、1–500 business_ids；可选 result_id+generation、include=facts/results/sources | 当前事实、序号、结果槽及其必要来源；成对指定 result_id/generation 精确查询正式保留结果，开放旧代已替换则 conflict。不得声称能读取已经删除的开放期旧正文。 |
| `accounting_find` | BusinessQuery：company_id、family、period_from/to、可选 object_id/status/kind、limit=1–500、cursor | BusinessFindResult：当前明确身份、kind/period/seq/state、摘要标签、完整数量和同范围 cursor；不作姓名模糊自动绑定。 |
| `accounting_sources` | import/get/find/inspect；company_id；来源描述或来源 ID；有界分页 | 真实原件/老板有效补充的当前索引与定位。import 先核对公司根、文件类型/大小/摘要，不能任意读取主机路径。 |
| `accounting_documents` | DocumentsRequest：company_id、action=status/finish/export；可选 scopes | DocumentsResult：集合标识、待完成范围和失败；ready 必含可供离线读取的 manifest_path 完整定位。finish 按当前事实接续；export 返回已发布清单定位，不新增归档任务或重记账。 |
| `accounting_period` | company_id、action=check/close、period、expected_head(close) | check 返回只读检查及阻断事实；close 在同一事务复查当前 head 后冻结，无老板批准/密码预览。changed 返回 conflict，不悄悄关掉新结果。 |
| `accounting_backup` | company_id、expected_database_id（目标取已配置目录） | 创建备份任务 receipt。成功仅在验证完成后返回包及摘要。 |
| `accounting_restore` | IsolatedTargetRequest：action=restore、source_path、new_target_directory、mode=check/run | 离线同步返回 IsolatedTargetResult，不依赖源业务库/任务。check 不写目标库；run 验证并创建新目标，既有文件即使空库也不覆盖。 |
| `accounting_rebuild` | 同合同 action=rebuild；来源为完整当前标准文档清单 | 离线同步返回检查或新目标结果；run 新建简化账，不接入旧目录、旧运行身份或旧请求。 |
| `accounting_jobs` | company_id、task_id | 仅查询明确文件任务，pending/running/succeeded/failed；结果包含已验证文件，不扫描全部任务。 |
| `accounting_report` | ReportRequest：company_id、year、quarter、可选 expected_head | FinancialSnapshot：共享三表及所得税取数依据；导出沿 [看板规范](06-dashboard-adapter.md) 的三接口绑定同一内容。 |

资料 import 由程序生成/核对来源身份和摘要；inspect 只读支持文件的有界文本块与原件定位，未知格式明确 unsupported_media，不执行文件内容。解析所得不是正式事实，AI 仍需按业务合同整理。SourcesResult、SourceInspection 和 DocumentsResult 是对应返回真源。

ContextResult 区分已选公司 CompanyContext 和无可用公司 EmptyContext；无公司时只返回目录选择，不虚构公司或数据库身份。字段尚未合法解析的请求用 ErrorResponse 返回 rejected/code/message，不能为凑 ApplyResult 伪造 company_id/command_id。accounting_schema 中的 schema_document 是声明性 Schema 元数据，按 Draft2020-12 元合同检查；这项例外不允许任意业务数据扩展。

同源 HTTP 页面只有只读业务能力及报表文件生成/下载，不提供业务修改或审批。会话保护属于本机入口安全：沿用单老板身份与原登录体验，不新增角色授权管理；专业顾问通过只读页面查看。AI 的业务入口由本机 MCP/CLI 接入程序，只接受上述类型化命令，不接收 SQL、脚本或自由分录；来源读取只用登记身份和受控公司相对路径，离线恢复/重建仅接受显式选定包及经过独立路径核验的新目标。

### 错误与接续

| 错误 | 是否写账 | 调用方处理 |
| --- | --- | --- |
| `invalid_contract` / `invalid_input` | 否 | 修正合同/字段；不是缺业务现实，不能追问老板代替程序修错。 |
| `batch_too_large` | 否 | 超过根数、明细数或字节上限；只按真正独立的业务单元拆分，不静默破坏原子关系。 |
| `needs_information` | 否 | 按 fact_issues 复用来源或补现实事实；其他独立业务可以继续。 |
| `conflict` | 否 | 重新读取当前序号和内容，确认目标仍成立后提交正确单，不盲目重放旧请求。 |
| `unsupported_business` / `unsupported_policy` / `unsupported_change_kind` | 否 | 说明具体边界；AI 不能用另一类业务或手算分录绕过。 |
| `dependency_conflict` / `closed_period_required` | 否 | 返回具体关系、已采用期间及所需更正范围；程序负责可确定的内部连带计算。 |
| `busy` / `temporary_failure` | 否或结果需查询 | 有界退避，最多自动 3 次（200/500/1000ms），随后返回实际状态；结果不明先查业务 ID。 |
| `documents_pending` | 已保存 | 查询/接续文档，不再次入账。 |
| `preparing` / `snapshot_changed` | 只读 | 准备中按返回间隔读取；快照变化重开该范围，不拼接不同版本。 |
| `storage_corrupt` / `internal_error` | 不伪称成功 | 停止该受影响工作，返回可定位诊断 ID；无关公司不因此停用。 |

## 模块之间的公开能力

这些是进程内端口，使用相同合同类型；不再为每个模块建立网络 API。批量接口上限均为 500 个身份，分页上限 500，结果必须指出完整范围与版本。

| 提供者 | 固定能力 | 合同含义 |
| --- | --- | --- |
| identity.public | `plan_change(objects, context)`、`apply_current(plan, uow)`、`get_batch(ids, read_context)`、`find(query, read_context)` | 唯一维护对象及当前序号；批量返回 CurrentObject，不暴露私表。 |
| domain.public | `describe_change(changes, context)`、`evaluate_slot(business_id, result_slot, context)`、`validate_candidate(plan, context)`、`apply_current(plan, uow)`、`export_current(ids, read_context)`、`query_current(query, read_context)` | describe 返回 ChangeDescription，声明责任、自然唯一键、完整槽集合、依赖和字段问题，不抢先计算正式结果。应用按槽拓扑求值，收齐完整候选后调用 validate_candidate 核验集合约束，再把 ChangePlan 交 owner 保存。最终核验不能改写计划、金额或采用依据。无自行提交、后台发任务或跨模块私表读取。 |
| ledger.public | `begin_candidate(responsibilities, uow, correction_period=None)`、`resource_definitions(refs, kind, read_context)`、`fund_movements(account_id, period_from, period_to, read_context)`、`adopt(change_plans, uow, correction_period=None)`、正式结果和凭证等查询、`financial_snapshot(scope, read_context)` | 候选逐槽 stage，全部齐备后 complete。求值可读来源资源定义；完整后才能读资源余额及完整资金范围。ResourceDefinition 含公开身份、完整定义及精确采用来源；ResourceState 加入截至指定月份/必要实际日的清偿、转移、消耗及剩余量。采用和事务提交再次统一检查容量；正式读取明确 current 或指定月末 as_posted。完整查询与财务取数按各自合同返回。 |
| money.public | 同领域完整协议；`stable_occurrence_id(business_id, public_slot)`、`register_occurrences(plans, uow)`、`fund_sources(fact, movements, context)`、`validate_occurrences(plans, context)`；账户、资金、原行及核对查询 | 唯一承接所有领域实际资金的稳定身份登记和原行绑定核验；登记身份不另存一套金额。内部划转单独标记；银行原行不自动变成第二次付款。 |
| ledger.opening / ledger.reporting | 同领域 describe/evaluate/apply/export/query 协议 | 分别承接 opening.package、reports.continuation 的事实写入和导出；共享 ledger 采用端口，不由 application 越界代写。 |
| records.public | `plan_change(input, context)`、`apply_current(plan, uow)`、`resolve_sources(ids, read_context)`、`lease_source(source_id, read_context)`、`current_notes/current_issues(scope, read_context)`、`publish_documents(scopes, head)` | 唯一维护来源、说明和待办；lease_source返回受控读取租约，可流式校验/复制已登记原字节或有效说明，不让调用者解析私有路径、任意开文件或改内容。 |
| storage.public | `read_unit(company_id)`、`write_unit(company_id)`、`maintenance_unit(company_id)`、`list_companies`、`company_metadata(read_context)`、目录/公司新建、结构验证及数据库一致快照/副本恢复 | 工作单元和一致点，不开放跨公司任意 SQL。模块仓储只接触自己的表；数据库副本操作不承接完整备份包和文档集合的编排。 |
| files.public | `ensure_current(plan, head, request_id)`、`get(company_id, task_id)`、`update(task_id, expected_state, head, result)`、`verified_download(company_id, task_id)` | 仅备份/报表当前任务及验证，严格 FilePlan 为 BackupPlan 或 ReportExportPlan。具体内容和版本检查由注入的生产者端口负责，files 不反向导入 application/reading。 |
| reading.public | `page(page_key, scope, query)`、`detail(detail_key, scope, query)`、`export_report(scope, head, request_id)`、`report_task(company_id, job_id)`、`report_download(company_id, job_id)` | 月页/季度页及准确业务范围，ready/preparing/failed、范围版本、完整计数和分页；文件计划防重及验证。context 使用仅含可选 company_id 的 ContextScope，其余使用 LedgerScope。 |

reading.page/detail 是读取用例入口，按 ContextScope/LedgerScope 通过 storage 建立一次只读上下文；HTTP 只解析参数，不自行另开连接。ContextScope 无公司时只取目录元数据，选定公司后才建立公司 ReadContext。来源审查 detail 使用 ContextScope，不编造业务月份。

入口内所有跨模块子查询显式使用同一 `ReadContext`：company_id、database_id、head、view/as_of，以及同一快照内的模块仓储。`repository(owner)` 只供各模块取得自身装配的仓储；上下文不公开连接、任意 SQL 或 commit。模块不得另开连接，避免一页混合两次提交。`UnitOfWork` 额外提供 next_head 和事务末检查登记，由 storage 统一结束事务；各数据 owner 的 apply_current/adopt/mark_targets 加入同一工作单元，不由应用层越界写私表。各 owner 专属计划和查询类型放在自身 public，公开线协议不携带这些进程内能力。

业务及当前资料的有效保存一次推进 global_seq；纯读取、文档发布确认、读取模型和文件任务维护不推进它。维护事务只能使用对应派生/任务仓储，不能在不推进业务头的情况下更改事实、来源或说明正文。这样缓存生成、备份进度不会反过来让业务依据失效。

`PostingPlan` 只由领域程序产生，字段包括 business_id、result_slot、accounting_period、rule_refs、受控 lines、obligations、settlements、obligation_reclassifications、cost_lots/consumptions、资金与税务/报表分类结果、agency_proofs、引用依据。每次还声明该业务的完整结果槽集合；未再产生的旧槽必须明确退出，不能遗留旧义务。公开资源键由 business_id+slot 稳定定位，不随 result_id 或计算序号改变；有清偿/消耗/冻结必要引用时不能静默删除。AI 接口不接收计划，科目和方向由固定业务规则选择，只有内部完整平衡的计划才能采用。

求值使用统一 EvaluationContext：当前事实和对象以本单正确内容覆盖；get_result 返回只有 source/target/basis 的 EvaluatedSlot，不把尚未提交的结果伪装成已有凭证。来源引用由 ledger 在同一工作单元绑定完整计划后生成，正式保存使用同一身份和摘要；领域不自行生成结果身份。依赖的 producer_public_slot 指核算节点的 result_slot，义务或成本引用的 BusinessRef.slot 则是该节点内的公开资源，两者不能混用。related_facts 按已经声明的当前依赖关系发现相关事实，再叠加本单新增、替换及撤去，不扫描全部正文猜关系。

代理代收代付采用严格 AgencyProof：只有合作 collection/payment 的正式 Target/Basis 产生，包含业务身份、agency 槽、原交易 source、方向、交易方、经营者、公司金额及期间，可选真实日。应用层按同一读快照或同单计划解析并传入 ledger；不保存第二份可编辑证明表。settlement.method=agency 须 agency_business_id/agency_public_slot，禁止 movement_id；金额合计必须等于有效证明，交易方、方向和原义务容量均须匹配。不产生公司资金发生或现金流。method=money 须真实 movement_id；offset 不伪造资金；转移及超付归类由独立 ObligationReclassification 承接。

同现实事件误分类，仅按业务规则明示的跨 kind 转换办理 replace。首交 expense 与 direct acquisition 都仅 main：旧 owner 的内部 handoff_out 责任 complete_result_slots=[]、不带 fact、不生成计划，只清其私有当前正文，不改主头、当前来源或正式 main；新 owner 的 replace 责任给完整新 Fact 与 main，唯一生成新正式结果。应用层核对一出一入、相同身份/前提和允许转换，完整校验后在一个 UoW 交接，business_id 不变且 seq 只加一次，原凭证号保留。activation 是独立业务，其折旧等按正常依赖同单处理，不因此引入通用跨种类迁移框架。公开资源不兼容则 dependency_conflict；不能任意互换种类，也不能撤去再建绕过冻结引用。handoff_out 不属于 AI Mutation。

每份业务责任必须声明 `natural_key`：确有自然唯一性时填写稳定键，否则为 null。应用统一检查最终责任集合，包含被连带重算的业务；同单互换唯一键合法，未参与的业务不能被挤掉。不得以姓名、数组顺序或资料行位置猜键。

EvaluationContext 提供当前事实、对象和已求值结果，以及显式关系集合 `related_facts/related_facts_many`、指定 kind/月范围 `facts_in_scope`。集合读取叠加本单新建、更正和撤去，不扫描私有 JSON 猜关系。依赖的 fact 边采用事实，result 边才参与求值拓扑；collection 边订阅集合增删，scope 订阅指定业务种类及月份，calendar/work 订阅已进入的工作月份。重复依赖按同一身份合并通知范围，不改变业务本身的核算范围。

ResourceCutoff 的月份是经济截止，实际日只在确需区分先后时填写。义务和成本的 business_period 可以是税款、工资或原成本的关联期；是否已经成立取生产结果的核算期。资金按真实发生日截取，非资金按正式采用的必要实际日截取。只有月份的记录不补日；同月月中确实无法判断的关系才返回精确缺项，按月或月末核验不因此普遍追问日期。

同一计算快照批量读取并复用已验证结果，返回值与调用者相互隔离；候选增加计划时更新相应索引，缓存不能跨提交沿用。公开读取单页500不限制合法事实产生的分录或证明数量；内部输出不能另加较小上限，迫使业务缩减或拆断原子关系。

`ChangeSet` 是本次变化的业务类别、对象、期间、结果槽集合，不保存过去的事实正文。应用层按读取依赖表更新当前目标 head。它不是永久事件流，也不是由领域直接列出页面缓存键。

文件计划采用 core.FilePlan，具体捕获、语义头和重启接续见 [数据设计](04-data-model.md#文件任务与备份一致点)。restore/rebuild 使用 IsolatedTargetRequest/Result：输入从源文件直接取得，不需要旧公司库、旧 task_id 或旧 set_id 反查；check/run 分别返回 checked/created，失败列明检查结果。交付新目标和注册为当前公司是两个动作，离线命令不自动切换现有服务。

义务公开读取返回 ObligationView（正式 Obligation 与同口径 ObligationBalance）。每项义务的 cashflow_parts 是正式分类的唯一保存处，领域产生、ledger 采用，money 从公开义务读取并按累计清偿比例分配到本次资金。普通一项、混合税/本金利息多项共用此机制，不为税另添一套付款算法。已知分类必须为正金额且合计义务额，零义务用空数组；缺少分类时省略，不代表零。真实金额和税额足够仍可记账/付款，只将受影响现金流行标为待补。FundMovement 的分类也可暂缺，补齐后保持实际发生身份重算；不能填假分类换取报表 ready。分类及累计分配、闭期有符号补差见业务和报表规范。

## 标准业务文档

标准文档是独立的最终事实档案，使用人可读 Markdown 索引和机器可读 JSON 正文。Markdown 由 JSON 生成，不能分别编辑成两套事实。AI 提交业务内容，程序根据实际保存的有效事实生成文档；未解项仍在当前待办区，不能把未入账事实假称为正式账务。

公司目录结构固定：

```text
<公司资料根>/<统一社会信用代码>/
  originals/YYYY-MM/<source_id>--<原文件名>
  standard/current.json
  standard/parts/<family>--<YYYY-MM>--<内容摘要>.json
  standard/README.md
  backups/<统一社会信用代码>.finance-company.zip
```

未确定所属月的原件暂存 `originals/unassigned/`，明确后归月；同一原件有多个期间引用时文件只保存一份，由来源索引记录实际业务关联，不能复制金额或伪造月份。老板有效补充可保存为当前事实说明来源，与原件同样使用 source_id。错误识别结果和已经被替代的老板答复不作为独立过程档案保留。

source_id 对应固定内容，字节或有效说明变更必须新建来源，不能同 UUID 覆盖。归月只改变受控位置：先准备新位置、发布新来源清单，再清理旧位置。删除前同时检查当前事实、闭期真正必要的采用引用、当前文档清单和正在读取/备份的文件租约；不能为保留全部旧答复滥用“必要”。sources 输入的旧位置不决定身份，幂等重试先核对已登记内容和当前实际位置。

事实分片保存一个领域和事实所属月的当前有效业务；跨期合同按生效首月归档，后续业务通过稳定业务引用关联，不反复复制合同正文。公司身份在清单，objects/sources/work/notes 为固定分片；政策/期初仍按各自事实家族归月，不另造隐式格式。一个业务只归一个分片，移月时从旧片移除并加入新片。

`current.json` 指向当前完整分片集合，含 format、company_identity、set_id、各分片相对路径/字节数/摘要。company_identity 的 working_period_to 保存已进入的工作月份，撤去该月唯一业务也不倒退；简化重建据此补齐资产等期间结果。缺省时取接账月与全部最终事实末月的较晚者，不采用电脑当前月。StandardPart 明确五种分片：FactShard、ObjectShard、SourceShard、WorkShard、NotesShard。事实片只含格式版本、归属、最终 Fact 数组及必要来源/对象引用，不含数据库连接、result_id、旧序号、冲正动作、请求日志或旧凭证编号。

程序生成所有受影响片到临时文件，核对当前目标未变后，以一次持久原子替换发布 current.json；未变的分片复用。清单切换后删掉不再被当前清单引用的片和临时文件，不保留历次清单。读取/备份持有集合锁直到引用文件读完，清理不能删除正在使用的当前集合。发布中断时按当前清单读取完整旧集合，按数据库当前目标重生成完整新集合；不能读到一半新一半旧。

开放期更正直接改写事实。闭期冲正归并到原业务的最终事实，发现期不再额外保留一条“用于重放的冲正”；真实后续退款、损失、收付款仍作为各自事实保存。凭证更正期间与事实原所属期间不同，这个区别只用于原账正式结果，不把纠错动作带进简化重建。

重建先核对清单、对象、来源和全部引用，按事实依赖顺序创建新库并计算，完成后再交付。缺少必要来源或事实时给清单，不能读取旧库补齐或从旧余额反推。文档不保存历次关账和操作史，重建无需复制旧冻结/冲正链；恢复这些内容应使用数据库备份。
