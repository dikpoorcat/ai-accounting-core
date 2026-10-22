<!-- @format -->

# 公开接口与标准业务文档

接口采用一个业务办理入口、几个明确的读取/资料/文件入口。会计 AI 不负责计算内核内部标识、依次发布结果、刷新缓存或修复重复请求。MCP 与 CLI 共用同一个应用服务，不分别实现业务。

机器可读真源为 [公共类型](contracts/core.schema.json)、[业务事实](contracts/business-facts.schema.json) 和 [既有看板响应](contracts/dashboard-response-v2.json)。JSON Schema 负责字段形状；本文及 [业务规则](05-business-rules.md) 负责跨字段、引用和核算约束。两类检查都通过才能办理，不能用 `json_valid` 或 Schema 校验代替完整业务校验。进程内 `validate_contract_items` 可批量校验完整类型，也可明确选择已安装对象类型的顶层属性；属性仍使用原合同和必填规则，不能额外夹带字段。属性投影只证明所选内容，不能作为完整事实、完整采用正文或跨字段核算已经通过的依据。

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

`create/replace/restore` 提交完整 `Fact`，不是字段补丁：`business_id, kind, period, evidence_ids, data`，可选 `effective_on`。kind 对应唯一严格 data 变体。`withdraw` 只提交 business_id 和 expected_seq。create 的 expected_seq=0，其余为当前正序号。先查询并复用已有身份；新业务、对象和请求身份使用 `accounting_new_ids(count)` 生成标准 UUID，CLI 为 `finance-modular ids --count N`，Python 为 `agent.public.generate_ids(N)`。N为1–1001的严格整数，覆盖一单500业务、500对象和1个请求；不连接目录或数据库，不创建或预留业务。调用方将编号填入并保存在当前完整请求上下文，不手工拼接身份；提交响应丢失沿用原编号查询和重试，不重新编号再记一笔。工具不保存未提交工作。

`objects` 是真实人员、交易方、账户、项目、资产及产品的稳定身份。名称可以变，身份不能跟着变。对象建档并不自动产生业务凭证；人员在册关系和资产启用仍分别是业务事实。资产档案的可选code保存企业已有编号，不要求编号唯一、不自动编造，也不改变核算身份或正式金额。看板所需档案可先保存未知管理项，不能仅因缺身份证或手机号拒绝本已明确的会计金额。

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

下表是应用层可调用能力。在线 CLI 使用 `finance-modular --catalog <绝对目录库> call accounting-apply --input <完整请求.json>` 这样的 `call` 形式，命令名接受连字符或下划线；MCP 使用 `accounting_command(command="accounting_apply", payload=完整请求)`。只有 Schema、UUID 和身份窗口另有专用工具，离线恢复／重建有独立入口；完整示例见[安装与接入](09-runtime-and-ai.md)。MCP 不暴露登记/准备/计算/发布等多段内部手续。

`accounting://guides` 是小型只读操作目录，按需提供工作引导、公开命令、`business/{kind}` 业务规则、报表含义和运行说明。正文从本设计唯一来源生成并随包安装，不从工作区读取；主题及 kind 为明确目录，不能用任意路径读取文档。规则目录不是当前实例的支持承诺，仍先用 `accounting_schema` 选择实际支持的 kind，再读取对应规则和输入合同。离线 MCP 只提供适用的运行／恢复／重建说明及两个离线命令合同。

| 入口 | 输入 | 返回及作用 |
| --- | --- | --- |
| `accounting_schema` | SchemaRequest：可选 kinds、contract | SchemaResult：默认仅合同及支持目录，指定 kinds 才返回对应输入 Schema 的自包含依赖闭包，不返回旧接口或内部 PostingPlan。未知 kind 拒绝，不偷偷省略。 |
| `accounting_context` | ContextRequest：可选 company_id、note_scopes、issue_scopes | 当前公司身份、支持范围、开放/关闭月、有效说明与当前待办及其 seq/head。省略范围默认公司说明和当前开放月；不全库加载档案。 |
| `accounting_objects` | ObjectsRequest：get 指定 IDs；find 指定 kind、可选 name_contains、limit/cursor | CurrentObject 含 seq/digest 和完整当前档案；检索名称只供选择，不自动绑定同名人。 |
| `accounting_get` | company_id、1–500 business_ids；可选 result_id+generation、include=facts/results/sources | 当前事实、序号、结果槽及其必要来源；成对指定 result_id/generation 精确查询正式保留结果，开放旧代已替换则 conflict。不得声称能读取已经删除的开放期旧正文。 |
| `accounting_find` | BusinessQuery：company_id、family、period_from/to、可选 object_id/status/kind、limit=1–500、cursor | BusinessFindResult：当前明确身份、kind/period/seq/state、摘要标签、完整数量和同范围 cursor；不作姓名模糊自动绑定。撤去身份只按最后归属月、种类及身份检索，不保存旧正文或旧对象关系。object_id只匹配当前有效关系，与status=withdrawn同时提供时明确拒绝。 |
| `accounting_sources` | SourcesRequest：import/get/find/inspect/discard；company_id；来源描述或来源 ID；有界分页；discard 必须明确1–500个不重复source_ids | SourcesCommandResult：import/get/find/inspect返回SourcesResult，discard返回SourceDiscardResult。真实原件/老板有效补充的当前索引与定位；import核对公司根、文件类型/大小/摘要，discard只退出无当前或正式采用引用的指定无用来源，不任意读删主机文件。 |
| `accounting_documents` | DocumentsRequest：company_id、action=status/finish/export；可选 scopes | DocumentsResult：集合标识、待完成范围和失败；ready 必含可供离线读取的 manifest_path 完整定位。finish 按当前事实接续；export 返回已发布清单定位，不新增归档任务或重记账。 |
| `accounting_period` | company_id、action=check/close、period、expected_head(close) | 检查或关闭截至指定月尚未关闭的整段账务，不要求逐月处理空月。check 返回只读检查及阻断事实；close 在同一事务复查同范围 head 后冻结，无老板批准/密码预览。范围变化返回 conflict，不悄悄关掉新结果。 |
| `accounting_backup` | company_id、expected_database_id（目标取已配置目录） | 创建备份任务 receipt。成功仅在验证完成后返回包及摘要。 |
| `accounting_restore` | IsolatedTargetRequest：action=restore、source_path、new_target_directory、mode=check/run | 离线同步返回 IsolatedTargetResult，不依赖源业务库/任务。check 不写目标库；run 验证并创建新目标，既有文件即使空库也不覆盖。 |
| `accounting_rebuild` | 同合同 action=rebuild；来源为完整当前标准文档清单 | 离线同步返回检查或新目标结果；run 新建简化账，不接入旧目录、旧运行身份或旧请求。 |
| `accounting_jobs` | company_id、task_id | 仅查询明确文件任务，pending/running/succeeded/failed；结果包含已验证文件，不扫描全部任务。 |
| `accounting_report` | ReportRequest：company_id、year、quarter、可选 expected_head | FinancialSnapshot：共享三表及所得税取数依据；导出沿 [看板规范](06-dashboard-adapter.md) 的三接口绑定同一内容。 |

资料 import 由程序生成/核对来源身份和摘要；inspect 只读支持文件的有界文本块与原件定位，未知格式明确 unsupported_media，不执行文件内容。解析所得不是正式事实，AI 仍需按业务合同整理。SourcesResult、SourceInspection 和 DocumentsResult 是对应返回真源。来源目录按所属月与来源身份稳定分页，只观察当前页文件状态，不以文件存在冒充摘要已经核对；inspect对所选原件核对完整字节后提供有界文本块。支持文本、CSV、XLS/XLSX隐藏行列及PDF文本；XLSX提供公式及缓存值，XLS仅提供缓存值，每页检查均按实际工作表提示公式表达式不能提取、空缓存不代表业务事实为空。未知格式或无法提取的PDF页明确说明，长行和单元格可以接续。自动来源身份按公司、种类和内容摘要确定，同一原件的并发导入不会另造两份。

discard 不改账务和已有来源正文。SourceDiscardResult 固定包含company_id、discarded_source_ids、missing_source_ids和file_cleanup:{state,issues}；issues每项只有code、message。state=complete表示本批清理已完成，pending表示登记已退出但文件清理尚待程序接续，不能当作全部办结；原source_ids可重复提交，已退出的身份列入missing_source_ids，程序仍接续待清理文件。资料整理和退出选择见[资料如何进入系统](08-ai-work-and-materials.md#资料如何进入系统)。

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

这些是进程内端口，使用相同合同类型；不再为每个模块建立网络 API。批量查询及分页每次最多500个身份，结果必须指出完整范围与版本。单笔业务的完整采用结果、来源定位，以及关账汇总的全部问题不截断，也不另设比合法输入更小的返回上限。

| 提供者 | 固定能力 | 合同含义 |
| --- | --- | --- |
| contracts.public | `analyze_fact(fact)` | 严格验证完整事实后冻结规范正文、完整摘要及来源身份和路径，返回FactAnalysis；读取fact/source_paths得到隔离副本。同一次完整核验可复用该分析，不缓存跨任务的业务接受结论，也不把当前事实证明代替冻结历史核验。 |
| application.public | `work_scope(read_context)` | 返回公司已保存的建账月、工作末月、正式结果存在性和关账末月；与其他查询共用同一快照，不读取全部业务或标准文档。工作范围是当前公司元数据，不改变调用方的历史账务选择；完整备份核验检查其覆盖真实事实、正式采用和关账范围。 |
| ledger.public | `referenced_source_ids(source_ids, read_context)` | 在同一快照返回指定的0–500个唯一来源中，全部保留的正式结果实际采用的来源身份，排序并去重；覆盖当前开放结果和仍保留的冻结结果，不仅检查关账记录，也不扫描全部采用正文。当前事实等其他引用由各自 owner 核对。 |
| identity.public | `plan_change(objects, context)`、`apply_current(plan, uow)`、`get_batch(ids, read_context)`、`find(query, read_context)` | 唯一维护对象及当前序号；批量返回 CurrentObject，不暴露私表。 |
| domain.public | `declare_relations(changes, context)`、`describe_change(changes, context)`、`evaluate_slot(business_id, result_slot, context)`、`validate_candidate(plan, context)`、`apply_current(plan, uow)`、`export_current(ids, read_context)`、`query_current(query, read_context)` | declare_relations只按本模块完整事实及直接引用声明关系，不读取派生关系或计算金额。应用收齐直接关系后，describe返回ChangeDescription，声明责任、自然唯一键、完整槽、核算依赖和字段问题。应用按槽拓扑求值，收齐完整候选后调用validate_candidate核验集合约束，再交owner保存。最终核验不改写计划、金额或依据；模块不自行提交、不越界读私表。 |
| ledger.public | `begin_candidate(responsibilities, uow, correction_period=None, publications=(), resource_uses=None, retained_results=())`、`resource_definitions(refs, kind, read_context)`、`fund_movements(account_id, period_from, period_to, read_context)`、`adopt(change_plans, uow, correction_period=None, explanation=None)`、正式结果和凭证等查询、`financial_snapshot(scope, read_context)` | 候选逐槽 stage，待计算槽齐备后 complete；保留槽经精确核对后已就绪，原资源仍可用。声明完整使用范围后，相关槽就绪即可读取精确资源窗口；完整资金范围仍须等全部候选齐备。ResourceDefinition 含公开身份、完整定义及精确采用来源；ResourceState 加入截至指定月份/必要实际日的清偿、转移、消耗及剩余量。采用和事务提交再次统一检查容量；正式读取明确 current 或指定月末 as_posted。完整查询与财务取数按各自合同返回。冻结槽退出由ledger统一生成必要空目标，原因原样采用请求说明；领域不虚报新的有效槽。 |
| money.public | 同领域完整协议；`stable_occurrence_id(business_id, public_slot)`、`register_occurrences(plans, uow)`、`actual_settled_fen(source, cutoff, context)`、`fund_sources(fact, movements, context)`、`validate_occurrences(plans, context)`、`check_period(period_from, period_to, read_context)`；账户、资金、原行及核对查询 | 唯一承接所有领域实际资金的稳定身份登记和原行绑定核验；登记身份不另存一套金额。实际清偿按同单最终事实、本义务分配及经济截止计算，不含抵销、转债或代办。关账复用登记时的完整银行匹配和平台原行规则，返回范围依据头及全部阻断项；未付款或闲置账户不要求虚构原行。 |
| ledger.opening / ledger.reporting | 同领域 describe/evaluate/apply/export/query 协议 | 分别承接 opening.package、reports.continuation 的事实写入和导出；共享 ledger 采用端口，不由 application 越界代写。期初仍是其唯一事实 owner，资产卡片及后续摊折由注入的 OpeningAssetAccounting 计算，不能另造启用事实。 |
| records.public | `prepare_source_imports(imports, read_context)`、`sources(request, read_context)`、`prepare_sources(sources, read_context)`、`discard_sources(source_ids, uow, retained_source_ids=..., protected_paths=...)`、`has_source_discards(read_context)`、`finish_source_discards(uow, protected_paths=..., marker_ids=None, limit=500, after_marker_id=None)`、`plan_change/apply_current`、`resolve_sources/source_status`、`lease_source`、当前说明/待办及期间分页、`capture_documents/prepare_documents/publish_documents`、`capture_backup_materials/lease_backup_inputs/discard_backup_capture`、`open_standard_bundle/copy_standard_bundle`、`verify_snapshot` | 唯一维护来源、说明、待办和最终文档。来源内容预核验在写事务外完成，活跃证明绑定公司、位置及完整输入，并在采用和提交前复核；下载租约另提供固定字节。明确选择的来源只在当前引用、正式采用和原件保护检查通过后退出；文件清理须在登记退出提交后使用独立新写事务，当前标识可分批接续，完成即删，不留过程历史。期间查询不把局部页当完整替换集合。备份捕获绑定同一数据库快照并保护原件和分片；离线核验把完整当前事实、对象、待办、说明及来源索引与已发布文档精确对应，同时检查未使用及冻结依据的已登记原件。完整资料可以脱离源库核验和复制。 |
| storage.public | `read_unit/write_unit/preview_unit/maintenance_unit`、`list_companies/company_metadata`、`create_company_database`、`open_database_unit`、`publish_company_directory`、`company_structure_contract`、结构核验及一致数据库副本 | 在线和离线共用工作单元、模块仓储及实际结构核验，不开放跨公司任意 SQL。离线明确数据库和资料根，不创建虚假目录或负责人。试算复用完整核算及事务末检查，正常结束也回滚；新目标发布须原子且不覆盖已存在位置。数据库复制不承接完整包和文档编排。 |
| files.public | `ensure_current(plan, head, request_id)`、`get(company_id, task_id, check_current=False)`、`update(task_id, expected_state, head, result, company_id=...)`、`verified_download(company_id, task_id)`、`retained_input_plans(kind, read_context)`、`discard_unaccepted_capture(company_id, capture_id)` | 仅备份/报表当前任务及验证，严格FilePlan为BackupPlan或ReportExportPlan。get默认只观察任务；check_current=True经生产者核对当前计划，失效才标为expired，不执行生产。retained_input_plans只用调用方快照返回pending/running/failed仍保留的完整严格计划，不恢复租约、加执行锁或读取文件；输入含义由生产者所属模块解释。生产者完整核验候选独立字节后，Files以绑定任务、范围头和执行代次的VerifiedArtifact交给publish，再对实际发布文件完整摘要；维护事务内复核当前计划并保存成功结果。下载以独立实际字节匹配该成功摘要，并在前后核对计划，不重新核算同一文件。未接纳捕获通过独立CaptureContext回收；当前任务或实际租约仍引用时保留，清理失败明确报错。files不反向导入application/reading。 |
| reading.public | `page(page_key, scope, query)`、`detail(detail_key, scope, query)`、`retry(page_key, scope, query?)`、`export_report(scope, head, request_id, *, epochs)`、`report_task(company_id, job_id)`、`report_download(company_id, job_id)` | 月页/季度页及准确业务范围，ready/preparing/failed、范围版本、完整计数和分页；文件计划防重及验证。context使用ContextScope；月页使用MonthScope(company_id, period?, expected_head?)；季度和业务进展使用LedgerScope，简报组及只读月度核对使用明确月份的MonthScope，来源审查使用ContextScope。主动重试须明确月份，只重启该页面失败的派生组件，不改正式账务。 |

reading.page/detail通过storage建立一次只读上下文；HTTP只解析参数，不自行另开连接。月页省略月份时，在该上下文内按真实账务及工作月份选择默认月，不先另读一次context；空账套没有虚构的月份或金额。页面context可以按目录默认选择一家公司，尚无公司时只返回目录；不打开其他公司业务库。AI的accounting_context未指定公司时只列可选公司，不代替AI选定工作范围。来源审查detail使用ContextScope，不编造业务月份。

入口内所有跨模块子查询显式使用同一 `ReadContext`：company_id、database_id、start_period、head、view/as_of，以及同一快照内的模块仓储。start_period 是公司已有建账边界，不表示此前余额或发生额为零。`repository(owner)` 只供各模块取得自身装配的仓储；上下文不公开连接、任意 SQL 或 commit。模块不得另开连接，避免一页混合两次提交。`UnitOfWork` 额外提供 next_head 和事务末检查登记，由 storage 统一结束事务；各数据 owner 的 apply_current/adopt/mark_targets 加入同一工作单元，不由应用层越界写私表。各 owner 专属计划和查询类型放在自身 public，公开线协议不携带这些进程内能力。

读取依赖由实际取数模块声明。公开活动返回进程内ReadInputs，说明查询采用的正式种类、科目、资源类别和实际引用；账务采用返回FormalChange，给出真实新旧结果及所影响的月份和资源。Application把它与当前对象、管理资料、关账和工作月份变化合并，在原事务内通知读取模块。新成员按查询范围发现，已用业务及对象按精确引用匹配；余额延续到后续静月，当月活动只更新对应月份，历史未结事项另接当前后续。依赖随可重建投影保存，不增加账务过程记录，也不由账务模块识别页面名称。

expected_head核对同公司、同范围、同读取口径的实际结果，不使用global_seq，也不把它自身计入摘要。FinancialSnapshot使用selection_digest，正式目录和活动使用各自version，月页使用所需组件内容与月份状态的组合版本；无关文件任务和范围外业务不能让相同结果失效。应用比较采用变化时使用`ledger.get_result_refs(business_ids, read_context)`批量取得窄引用，不为读取ID、代次及期间加载全部采用正文和凭证分录。

报表的scope显式选择current/as_posted，可在同一ReadContext中分别读取；其他未单独指定口径的查询沿上下文默认值。选择口径不另开事务，不复制存储上下文或连接。ledger按业务月、业务ID和结果槽遍历完整目标及存量，正式和候选税前利润共用此顺序；as_posted发生流按采用月、业务ID、结果槽、代次和结果ID确定正采用，再按首次引用次序加入基线扣减，净系数为零的来源不重复累计。分录沿line_no，余额按科目、可空交易方和对象归集，财务资源沿各自身份；不能用SQL返回顺序或投影存放顺序改变逐步整数分核验和未知项含义，已保存的冻结报告不因此重算。

普通公开查询使用 QuerySelection 明确 current 或 as_posted/as_of，省略时沿同一 ReadContext 默认值；已经接收 LedgerScope 的入口不再叠第二份选择。正式采用截止与 ResourceCutoff 的经济月份、必要实际日分别表达，不能用一个日期代替。PageQuery 只携带每页数量、游标及预期版本；ReadPage 返回完整数量、筛选后完整数量、本页项目和后续游标。筛选、计数与汇总在分页前完成，版本绑定真实查询范围，不用无关全局提交号代替。

### 正式账务的窄查询

以下端口由 ledger 提供，字段类型放在 ledger.public。它们交付正式账务含义，页面字段转换留给 reading/dashboard；不开放任意 JSON 字段查询，也不先加载全历史正文再分页。

| 端口 | 输入与返回 |
| --- | --- |
| `financial_summary(scope, read)` | FinancialSummary 含版本、范围、全部损益收入/费用/净利润、资产/负债（含流动与非流动）/权益/资本/留存利润、银行/现金/平台，以及固定和无形资产各自原价/累计/净值及长期资产合计；每项为 AmountCell，附具体问题及采用来源。共用三表选择与余额，不以主营收入或税前利润替代完整结果。 |
| `financial_balances(scope, names, read)` | FinancialBalances 只返回所选受控余额字段的 values、问题、必要来源、范围及版本。可选银行／现金／平台，以及固定资产、无形资产和长期资产的原价／累计／净值；字段与 `financial_summary_inputs(names)` 共用唯一科目声明。资产模块一次取9个资产余额，不读取完整损益和三表。 |
| `account_balances(keys, period, read, selection=...)` | 最多500个 AccountBalanceKey(account_code, object_id?)；对象省略表示该科目全部对象。返回每项有据月初/月末余额、首个正式记账月、问题及必要来源。current 使用最终有效事实；as_posted 的 as_of 必须等于所查月份，月初沿用上月正式余额。1101 与基金身份按同一口径读取成本，不从购买事实累计反推。余额读取按实际账户行定位业务，再选择正式有效凭证；完整核验期初，保留无凭证采用的凭证继承和相抵为零的实际贡献。 |
| `query_obligation_positions(query, page, read, selection=...)` | query 含 cutoff_period 月末、可选业务/对象/类别/方向及 include_settled。纳入以前月份截至该月的完整义务及清偿，返回 PositionPage。 |
| `capture_obligation_directory(query, read)` | 轻目录返回所选口径的全部未结义务及其当前精确匹配、整数分 PositionAmounts 和去重的正式来源。query 明确两个经济截止月及 include_origins，selected_view 默认为 as_posted，也可选 current；可按业务、类别和方向限定。include_vouchers 默认 False；启用后 selected_vouchers 只按选中项的唯一 result_id 返回实际有效凭证或明确的 None，未请求为 None，空选择为 {}；继承凭证保留真实 owner，不扩大 sources/origins。None 业务范围表示全部，空元组表示空范围。当前匹配可已结清或不存在；需要发现全部当前未结项时必须选择 current。此口不提供旧 PositionPage 的全公司统计、版本或排序。 |
| `obligation_cutoff_candidates(previous_cutoff_period, cutoff_period, read)` | 要求建账月≤前截止月<后截止月；在同一读取中比较两个截止月实际 as_posted 选中结果，返回不可变 ObligationCutoffCandidates，其中 business_ids 为完整、去重并排序的候选业务超集，含变化义务及清偿、转移和重分类实际涉及的来源／目标业务。仅读采用头和资源引用，不读取 Basis／Target 正文或重算金额；不是已删除历史的变更日志，也不能证明另一事务中旧索引仍有效。 |
| `get_obligation_origins(locators, read)` | 按精确 ResultRef 返回 AdoptedOriginProjection 或完整 AdoptedContent。费用、服务销售、销售退回、费用收回、代收代付及三种实际付款使用有限投影，严校声明的 kind、冻结业务身份、付款类型及完整 adopted_quantities；工资备用金同时核对备用金费用。其余复杂类型保留完整正文及摘要核验。投影不证明未返回的正文，不能冒充完整 AdoptedContent；消费者核对精确来源并只使用声明字段，凭证展开等完整读取仍沿原入口。轻目录可在同次捕获中附带这些来源，避免逐资源重复读取。 |
| `query_cost_positions(query, page, read, selection=...)` | query 含 cutoff_period 月末、可选业务/对象/类别及 include_consumed。已耗尽来源仍有正式身份，不因剩余为零从完整目录消失；页面可以明确筛掉。 |
| `capture_cost_directory(query, read, selection=...)` | 使用同一 CostPositionQuery，一次返回全部匹配的 ResourcePosition 元组，不计算其他类别的统计。按正式选中的业务、对象和类别先定位成本来源，再读取这些来源的实际消耗；include_consumed 控制是否列出已耗尽来源。金额、精确来源和采用规则与完整位置查询一致，缺失返回空元组。基金和项目只需各自成本时使用此入口。 |
| `get_obligation_positions(cutoff_period, read, business_ids=(), references=(), selection=...)`、`get_cost_positions(...)` | 按各最多500个已知业务身份或BusinessRef资源键，完整返回其正式资源和截至该月的实际使用量；不计算全公司统计、不分页，缺失保持空而不是补零。SQL在聚合前限定来源键，仍按current/as_posted选择实际采用；调用方核对返回的精确来源。 |
| `query_settlement_events(query, page, read, selection=...)` | 明确发生月份范围，可按原义务、原义务业务种类、消费业务、真实资金身份及处理分量筛选。source_business_kinds 按所选正式义务的原 kind 过滤，不取当前事实标签。返回 ResourceUse 和仅在实际付款时对应的 FundMovement。实际分配取 use 金额，不能用整笔混合付款冒充本项金额。 |
| `query_business_settlements(query, page, read, selection=...)` | 必须有source_refs、consumer_business_ids或movement_ids中的至少一种已知身份；这些身份及期间界定完整计数和版本范围，components/source_business_kinds再在范围内筛选。复用同一实际清偿查询，无关业务变化不使本范围游标过期；需要发现身份及全公司统计时使用上一项宽查询。 |
| `query_voucher_directory(scope, kinds, page, read, business_ids=..., roles=...)` | 按正式采用月份及凭证号提供窄目录，包含精确结果引用、凭证身份、摘要、角色及冲正关联。current 同样保留正式入账历史，不按当前目标丢掉必要原凭证。`get_vouchers(locators, read)` 只批量读取指定页的完整分录，定位含 voucher_id/result_id/generation。 |
| `query_adopted_values(scope, kinds, page, read, business_ids=...)` | 返回精确引用、正式来源、原业务种类、Target 及有效 baselineTarget；可按业务身份缩小读取范围，版本和游标绑定完整筛选。current 按经济月最终目标，as_posted 按采用月目标与基线表达发生差额。baseline_reference 与 baseline_target 同有同无，可按旧精确引用补读必要 Basis，不能把新日期或对象套到旧负项。退休结果按原业务种类过滤，资产与基金共用此选择。 |
| `query_adopted_targets(scope, kinds, page, read, *, business_ids=(), result_slots=())` | 按与正式目录相同的截止选择，返回每个业务／槽的一项完整Target及精确引用和来源；只读取本页Target，不读取历史Basis或凭证明细。供人员名单等提取正式类型和身份；不是按采用月展开的发生额。 |
| `get_adopted_sources(sources, read)`、`account_names(codes)` | 前者按最多500个精确ResultSourceRef核对身份、代次及内容摘要，只返回正式目录头；可定位消费者实际采用的较早冻结来源，不用当前头替代。后者无需数据库，返回受控科目的唯一中文名称，不由看板另抄科目表。 |
| `period_closure(period, read)` | 只读取指定月份的关账状态、实际覆盖关账月、冻结范围和头摘要，不读取整套三表；完整冻结报告另由财务报告入口核验和提供。 |
| `get_effective_voucher_directory(refs, read)` | 按最多500个精确ResultRef定位完整目标实际采用的凭证头；无新增凭证的正式采用沿必要基线继承，撤去或无替代冲正停止继承。每项返回实际凭证所属ResultRef及凭证头，或明确null；不取历史Basis、分录或凭证增量。冷生成可只存引用，展开实际页时再读取完整凭证。 |
| `query_adopted_directory(scope, kinds, page, read, business_ids=..., result_slots=...)` | 返回月末所选正式结果的窄目录，包含无凭证结果和退休结果。`get_adopted_contents(locators, read)` 按最多500个精确 ResultRef 返回 source/target/basis，不附凭证或全部资源。 |
| `fund_movements(account_id, from, to, read, selection=...)` | 账户为 None 时明确取全公司。按真实发生月份、实际日及稳定 movement_id 返回真实资金定义；正式冲正、替代凭证不制造第二次资金。 |

PositionPage同时包含有界ReadPage、完整范围totals和筛选后totals；义务按方向及类别分组，成本按类别分组，不能把不同对象的应收应付净抵。每项ResourcePosition含正式定义、采用来源及PositionAmounts：确认额、money/agency/offset清偿、transfer转出、excess_reclassified、cost消耗和剩余，均为整数分。汇总也使用相同分量和完整来源数量，在分页前形成。程序生成完整模型时使用`capture_obligation_positions(query, read, selection=...)`或`capture_cost_positions(...)`，一次返回PositionSelection的完整筛选items、版本、计数及同口径汇总；有界分页与完整捕获共用选择算法，避免逐页重算全范围，不形成跨事务缓存。这里的cutoff_period只表示经济月末；核算需要精确实际日时继续使用既有ResourceCutoff/ResourceWindow。

目录先在所属模块中限定月份、种类、对象及筛选，计数后沿稳定键取页，再批量读取本页正文。游标和版本绑定公司、数据库、选择口径、范围、集合及筛选；相关变化返回 snapshot_changed。当前对象名称可更新显示，不能改变冻结采用中的类型、金额和实际日期。

### 财务准备与读取

以下是 `ledger.public` 向 `reading` 提供的程序内交接，不是 AI 业务命令。会计规则和保存内容的格式属于 ledger；reading 只负责保存、待办接续和原子发布。

| 端口 | 输入与返回 |
| --- | --- |
| `financial_projection_request(kind, scope, read)` | kind 为 `summary_values` 或 `snapshot`；返回 FinancialProjectionRequest，含规范 scope、稳定 key 和描述符定位。身份绑定模型格式、公司、数据库、种类及范围，不含全局业务头或请求的 expected_head；后续端口再次核对请求，不能修改其中的字典后继续沿用旧 key。 |
| `prepare_financial_projection(requests, changes, closure_ranges, read, *, base=None, rebuild=False)` | 在同一读快照中消费实际 FormalChange 和关账起止月对，返回 FinancialProjectionPlan：规范 header_json、其 SHA256、逐项 writes(collection,key,payload_json) 和 deletes(collection,key)。正常更新核对变化业务槽的完整保留链。新增范围先固定兼容基底，再按实际月份边界发现候选槽；没有兼容基底时发现目标完整范围，不拿待办代替历史发现。首次冷建及显式 rebuild 完整发现；rebuild=True 时 base 必须为 None，并提供全部登记请求，发布核对后才替换旧准备资料。 |
| `read_financial_projection(request, read, base)` | 从已准备的范围返回 FinancialProjectionArtifact，含请求身份、实际描述符定位及摘要、范围版本和结果。金额摘要为 FinancialSummaryValues(version,scope,values,issues)，不枚举完整公开 Sources；三表为严格 FinancialSnapshot。未覆盖请求返回 None，已声明内容缺失或损坏返回错误。金额摘要与完整 FinancialSummary 使用同一版本工厂；完整来源只在需要完整公开结果时读取。 |

base 遵守 FinancialProjectionReader：提供 header_json 和按输入顺序返回正文或 None 的 get_blocks。内部 JSON 由 ledger 验证，reading 不解释余额、正式来源或索引节点。基底通过了格式核验不表示已经包含后来业务；reading 必须在正式提交中登记相关待办，完成准备后才交给消费者。

同公司只保存一份共享的精确来源正文和正式采用链；建立后，每次正式变化都在同一事务进入其当前合并待办，包括消费者范围以外的变化。各范围只持有实际选中的余额、发生额及必要边界，正文按本次真实采用确定。准备发布核对目标、格式、就绪点、原基底摘要及待办摘要；消费者仍按自身依赖更新，发布核对自己的描述符和格式，不采用整个公司准备头。失败使用同样的比较条件，迟到失败不能覆盖新结果。成功后清除待办，不形成操作历史；旧格式须显式重建后才能沿用。

### 资金与投资的批量读取

| 公开能力 | 结果含义 |
| --- | --- |
| `money.account_activity(scope, read)` | 一次返回完整实际资金、原行、匹配组和整公司／逐账户汇总，包含流入、流出、划转、数量、最后发生日及原行核对状态。各账户含其划转两端，公司对外金额排除内部划转，划转金额按同一 transfer_group_id 计一次。账面期初、期末由同快照的 ledger.account_balances 提供。 |
| `money.query_movements(scope, query, page, read)` | 查询可选账户，None 明确表示全公司；返回 ReadPage、稳定资金身份、正式采用、账户、真实日、方向、金额、交易方及精确清偿分项。整笔资金和各义务分配分别表达。普通收付、工资组合及抵销在Basis保存实际采用义务的公开引用、科目、方向、原金额及收款人；读取实际分项沿消费时的采用量，不从后来被改写的来源猜收款人。 |
| `money.query_statement_rows(scope, query, page, read)` | 按可选账户及 bank/platform 类型返回正式原行、稳定行身份、资料位置、原文及真实匹配组。冻结金额和日期从所选正式 Basis 取得；当前原行文字只补充已核对的同一行，不替换冻结数据。 |
| `investment.money_fund_activity(scope, read)` | 一次返回完整产品、确认／清偿事件和整公司／逐基金汇总，包含有源期初、正式成本、申购确认、赎回成本和确认损益。成本由 ledger 余额及成本结果提供；实际付款和到账由正式清偿提供。所有 AmountCell 的缺项索引都指向同一 issues 问题池。 |
| `investment.query_money_fund_events(scope, page, read)` | 返回有界确认与实际清偿事件，包含稳定事件键、基金、业务、精确采用、经济月、采用月及可空真实日。闭期更正的新旧分腿分别读取自身 Target/Basis，不把确认收益再次当作到账收益。 |
| `investment.cooperation_positions(scope, read, contract_ids=())` | 返回截至明确月每合同最近有效正式位置、实际采用合同中的经营者和项目、代管金额及双方应补、状态和精确来源。只读取既有正式结果，不重算、不造普通义务；静月沿最近位置接续，as_of_period保留真实原月。所选月的历史采用与当前最终事实分别读取，应补减少不等于实际付款。 |

这些入口生成完整月度组件，reading 从已形成的组件筛选、计数和分页；续页不重新枚举历史业务。生成完整金额时须读完实际相关的窄分页，不能用首20项代表全月。基金按原义务种类筛真实清偿，金额取分配额；合作投资沿正式资金和凭证展示，不混入货币基金专用字段。

`people.relationships(person_ids, period, read)`一次读取最多500个明确人员的当前正确关系时间线和月内成员状态。没有关系事实时返回未知；每项含可空业务身份、版本、保持月或日精度的关系段、月末状态、确定与可能状态、员工及劳务成员、来源和具体精度问题。relationship_start 只标明确确认的关系起点，不把首次记录或恢复上班自动当入职。不附事件修订版本，不借缴费参数推断在职，也不由关系资料重新计算工资。

`people.remuneration_activity(scope, read)`一次返回完整月度工资、奖金、劳务和资本化劳务，含正式发生额、月末义务、真实清偿及必要来源。闭期更正旧负项和新正项各用自己的依据；仅补同额证明不再造薪酬活动，等额改变费用或项目归属仍保留。工资及劳务累计未付缺少必要期初时分别返回null及实际问题，本月已知计提和付款照常返回。超付追偿单独保留，不重复清偿原工资。

roster按截至所选月的正式Target保留人员和员工／劳务类别，即使本月没有活动、过去工资已付清也不漏人；通用person档案不自动成为员工。sources只含本月发生、未付或本月清偿实际引用的精确薪酬来源，不加载全部历史Basis。期初模块统一核对精确正式正文及成员目录：`opening_payroll_references`解释本人、收款人、工资月及组成，不从收款人反猜员工，也不凭期初欠款补造税前工资；`opening_obligation_references`解释普通往来的原业务说明及税费原税月，借款成员分别解释合同、出借人及实际非零的本金／利息资源。所有入口返回真实公开资源引用，不猜造复合成员槽。

### 资产的完整读取

`assets.asset_activity(scope, read)`一次返回完整卡片、项目成本和固定／无形资产汇总。卡片保留当前档案、正式成本及启用来源、截至所选月的累计摊折、当月发生、退出时账面价值和付款范围；未知保持未知，期初没有取得日就不补造。更正发生额的正负两腿各用自己的正式类别，当前档案不覆盖历史资产类型。

整批取得的付款保留整批范围，项目转资产的付款保留全部所用来源范围，不按单卡原价猜分摊。真实付款、抵销等清偿和超付追偿分别表达。AssetLedgerAmounts只含九项资产余额和本地issues，所有缺项索引指向此池；卡片问题独立放在card_issues，不夹带无关利润或现金流。

`query_cards(scope, AssetCardQuery, PageQuery, read)`按all/active/fixed/intangible/pending/exited和可选asset_id读取；明确的身份与筛选同时生效。`query_project_costs(scope, ProjectCostQuery, PageQuery, read)`按可选project_id及include_consumed读取，默认排除已耗尽成本，返回分页前完整范围和筛选范围合计。它们是同一投影的便利查询；reading生成月度组件只调用一次完整活动，热续页从自己的集合读取。

期初资产协作只增加一个进程内计算端口：`opening_asset_cards(fact,context)` 核验并生成公开卡片；`describe_opening_assets` 返回附加结果槽、依赖、发布及问题；`evaluate_opening_asset_slot` 计算指定摊折槽；`validate_opening_asset_candidate` 只读核验完整候选。期初合并完整责任，保留自己的 main 槽并委托资产槽。各方仍只保存自身事实和表，资产公式只有一份；未装配端口时明确说明资产接续不可用，不静默遗漏。

业务及当前资料的有效保存一次推进 global_seq；纯读取、文档发布确认、读取模型和文件任务维护不推进它。维护事务只能使用对应派生/任务仓储，不能在不推进业务头的情况下更改事实、来源或说明正文。这样缓存生成、备份进度不会反过来让业务依据失效。

整公司离线办理复用同一核算执行流程，进程内完整输入和结果不伪装成一次有界的 AI 请求或回执。单次 AI 请求仍执行其批量与字节限制；标准资料按文档自身合同核验，一笔事务处理全部对象及业务，不能因每 500 项一次提交而留下半本账。离线完整问题集合逐项返回，顶层给简明原因，不把大量来源身份拼成长句，也不因输出项数截断遗漏问题。

`PostingPlan` 只由领域程序产生，字段包括 business_id、result_slot、accounting_period、rule_refs、受控 lines、obligations、settlements、obligation_reclassifications、cost_lots/consumptions、资金与税务/报表分类结果、agency_proofs、引用依据。每次还声明该业务的完整结果槽集合；未再产生的旧槽必须明确退出，不能遗留旧义务。公开资源键由 business_id+slot 稳定定位，不随 result_id 或计算序号改变；有清偿/消耗/冻结必要引用时不能静默删除。AI 接口不接收计划，科目和方向由固定业务规则选择，只有内部完整平衡的计划才能采用。

`ChangePlan.retained_results`使用完整ResultRef声明沿用的当前正式结果，仅用于事实未变的recalculate责任。plans与retained_results互斥，其并集必须等于complete_result_slots；新增、替换、恢复及分类交接不能靠保留引用跳过计算。保留引用须与当前头完全相同，不能拿冻结基线冒充当前头。ledger在采用及事务末再次核对，允许本单把原开放头正常冻结；不为保留槽重造PostingPlan或清掉其资源。新采用来源须引用本单新结果或当前正式头，只有本消费者必要冻结依据中已采用的精确引用可以继承。

历史合同、政策等事实可以早于建账月。账务层统一拒绝在建账月以前产生凭证、资金、义务、成本消耗或正式财报贡献；建账前余额及未清组成通过期初接续，不由各业务模块一律拒绝较早事实。

求值使用统一 EvaluationContext：当前事实和对象以本单正确内容覆盖；get_result 返回 source/target/basis，不把候选伪装成已有凭证。ledger 绑定完整计划后生成来源引用；领域不自行生成结果身份。DirectRelation 严格只有 consumer_business_id、producer_kind（business/object）、producer_id、relation_kind 四项，声明完整事实的直接引用，独立于是否产生结果；撤去和交出责任不留直接关系。related_facts 只按此完整目录叠加本单新增、替换和撤去来发现成员，不从计算依赖或全部正文猜关系。

Dependency 仍指向真实承担的结果槽。result 依赖指生产结果槽；use/balance 指义务或成本的公开资源槽，定义另由 result 边声明，不能混用 main 与 primary 等名称。必要的推导求值边不要求进入 DirectRelation，零结果的管理事实也不伪造 main 计划。事实中的对象角色及业务含义由所属 owner 检查，正式采用引用由 ledger 核对。

代理代收代付采用严格 AgencyProof：只有合作 collection/payment 的正式 Target/Basis 产生，包含业务身份、agency 槽、原交易 source、方向、交易方、经营者、公司金额及期间，可选真实日。应用层按同一读快照或同单计划解析并传入 ledger；不保存第二份可编辑证明表。settlement.method=agency 须 agency_business_id/agency_public_slot，禁止 movement_id；金额合计必须等于有效证明，交易方、方向和原义务容量均须匹配。不产生公司资金发生或现金流。method=money 须真实 movement_id；offset 不伪造资金；转移及超付归类由独立 ObligationReclassification 承接。

同现实事件误分类，仅按业务规则明示的跨 kind 转换办理 replace。首交 expense 与 direct acquisition 都仅 main：旧 owner 的内部 handoff_out 责任 complete_result_slots=[]、不带 fact、不生成计划，只清其私有当前正文，不改主头、当前来源或正式 main；新 owner 的 replace 责任给完整新 Fact 与 main，唯一生成新正式结果。应用层核对一出一入、相同身份/前提和允许转换，完整校验后在一个 UoW 交接，business_id 不变且 seq 只加一次，原凭证号保留。activation 是独立业务，其折旧等按正常依赖同单处理，不因此引入通用跨种类迁移框架。公开资源不兼容则 dependency_conflict；不能任意互换种类，也不能撤去再建绕过冻结引用。handoff_out 不属于 AI Mutation。

每份业务责任必须声明 `natural_key`：确有自然唯一性时填写稳定键，否则为 null。应用统一检查最终责任集合，包含被连带重算的业务；同单互换唯一键合法，未参与的业务不能被挤掉。不得以姓名、数组顺序或资料行位置猜键。

EvaluationContext 提供当前事实、对象和已求值结果，以及显式关系集合 `related_facts/related_facts_many`、指定 kind/月范围 `facts_in_scope`。集合读取叠加本单新建、更正和撤去，不扫描私有 JSON 猜关系。依赖的 fact 边采用事实，result 与 balance 边参与求值拓扑，use 声明被余额读取等待的槽；collection 边订阅集合增删，scope 订阅指定业务种类及月份，calendar/work 订阅已进入的工作月份。重复依赖按同一身份合并通知范围，不改变业务本身的核算范围。

collection依赖可填notify_effect=result或description，省略为result。description只通知重新核对完整责任和依赖目录，不把未变化的既有槽当作金额变化；其他依赖不能使用该标记。相同依赖合并时result优先，纯目录通知不参与数值输入签名。应用仍重算新增槽、数值依赖变化及实际受影响的下游。

ChangeDescription 与 ChangePlan 的 publications 明确每个结果槽可能影响的 `tax_sales` 或 `pre_tax` 月份范围；没有贡献时为空。ResultPublication 只含业务、槽、集合和起止月，不保存金额。同槽同集合合并为一个覆盖范围，实际贡献不得越界。税销售按 TaxSale.tax_period 发布，税前利润按完整目标的核算月发布，不能拿事实归档月或发现期代替。

读取这些集合时，依赖用 producer_kind=aggregate、producer_id=集合名、producer_public_slot=main、read_mode=result、relation_kind=calculation 和明确月份范围。应用对本单相关发布槽建立拓扑边，并按旧、新发布范围找消费者；槽被连带重算，即使事实和范围未变也通知。退出槽只通知旧范围，不继续等它求值。未受影响的持久结果直接参与取数，不为求集合重新处理全部历史。

`tax_sales(from,to)` 返回带精确采用来源的 TaxSaleView；`pre_tax(from,to)` 返回 PretaxView，含统一利润字段、必要来源和具体缺项。候选只在相关发布槽齐备后提供这些读取，不要求包括所得税自身在内的全部候选完成；资源余额按下述明确使用范围等待相关槽；完整资金范围仍等全部候选。所得税不发布税前利润，税点本身不发布税销售，避免重复或自我依赖。正式读取和同单候选共用账务选择器；各消费者不另写一套收入或利润汇总。

资源使用和余额读取复用Dependency：use声明可能使用的精确资源及月份范围，settlement表示义务，cost表示成本，不保存金额；balance声明从建账起点到截止期的读取范围，另用result边保证定义就绪。可用resource_components选择money、agency、offset、transfer、reclassify_excess或cost，省略表示完整。通知、求值等待和实际窗口使用相同选择，例如实际追偿只等待money/agency，不等待无关抵销。返回量带components，部分读取不冒充完整余额。纳入重算的使用者无论边是否变化，都按旧、新范围通知余额观察者；普通事实关系不混入这些声明。

必要的 effective_on 只能来自真实采用日，未知同月仍须等待并保留窗口上下界。balance 的 excluded_result_slots 明确排除自身及业务规则确定的内部结果，调度与实际读取使用同一名单，不能笼统排除下游或真实资金来消除循环。不同精确日或排除名单的读取不能静默合并。暂存与正式采用再次检查实际使用均被声明覆盖；完整候选仍核对全部容量。

ResourceCutoff 的月份是经济截止，实际日只在确需区分先后时填写。义务和成本的 business_period 可以是税款、工资或原成本的关联期；是否已经成立取生产结果的核算期。资金按真实发生日截取，非资金按正式采用的必要实际日截取。只有月份的记录不补日；同月月中确实无法判断的关系才返回精确缺项，按月或月末核验不因此普遍追问日期。

`resource_windows` 返回确定数量、同月未知日的变动及其可能余额上下界。领域可据最不利边界证明结果不受先后影响，就无需追问日期；只有成立与否确实随未知先后改变时才补资料。需要精确数值的 `resource_states` 仍拒绝含未知边界的日截面。两种读取共用相同时序筛选，不把未知变动默认为已发生或未发生；上下界只是计算比较，不是另存的正式金额。

同一计算快照批量读取并复用已验证结果，返回值与调用者相互隔离；候选增加计划时更新相应索引，缓存不能跨提交沿用。公开读取单页500不限制合法事实产生的分录或证明数量；内部输出不能另加较小上限，迫使业务缩减或拆断原子关系。

`ChangeSet` 是对调用者说明本次变化的回执，不保存过去的事实正文，也不负责读取失效。读取通知使用账务模块的 `FormalChange` 和应用层的 `ReadingImpact`，与各模块声明的 `ReadInputs` 匹配；它们不形成永久事件流，领域不列出页面缓存键。

文件计划采用core.FilePlan，具体捕获、语义头和重启接续见[数据设计](04-data-model.md#文件任务与备份一致点)。restore/rebuild使用IsolatedTargetRequest/Result：输入从源文件直接取得，不需要旧公司库、旧task_id或旧set_id反查；check/run分别返回checked/created，失败列明检查结果。恢复check完整核验源包；run严格检查包与实际复制字节，在隔离目标清理可重建任务和投影后，完整核验目标结构、正式账务、冻结依据及资料，成功才发布。重建也完整核验新计算的目标。交付新目标和注册为当前公司是两个动作，离线命令不自动切换现有服务。

义务公开读取返回 ObligationView（正式 Obligation 与同口径 ObligationBalance）。分类唯一保存在 cashflow_weights：每项为 category 与正整数字符串 weight，同一类别只能一项，来源留在必要 Basis。权重表示相对构成，不要求等于义务金额；已知单类使用1，更正为零也保留。混合构成全部为零且无来源支持比例时省略分类，不能猜均分或找回旧错误版本。money 按累计清偿额和统一权重产生本次 FundMovement.cashflow_parts，后者才是实际金额且合计必须等于真实收付。分类不放宽义务容量，超付仍须有明确追偿等关系。缺分类不妨碍已明确金额的记账/付款，仅将受影响现金流标为未知；补齐后保持实际资金身份重算。累计分配、退款继承及闭期补差见业务和报表规范。

## 标准业务文档

标准文档是独立的最终事实档案，使用人可读 Markdown 索引和机器可读 JSON 正文。Markdown 由 JSON 生成，不能分别编辑成两套事实。AI 提交业务内容，程序根据实际保存的有效事实生成文档；未解项仍在当前待办区，不能把未入账事实假称为正式账务。

公司内部目录结构固定，根目录直接采用明确指定的`company_root`，不再自动追加信用代码目录：

```text
<明确指定的company_root>/
  originals/YYYY-MM/<source_id>/<原文件名>
  standard/current.json
  standard/parts/<family>--<YYYY-MM>--<内容摘要>.json
  standard/README.md
  backups/<统一社会信用代码>.finance-company.zip  # 默认位置，可配置独立备份目录
```

未确定所属月的原件暂存 `originals/unassigned/`，明确后归月；同一原件有多个期间引用时文件只保存一份，由来源索引记录实际业务关联，不能复制金额或伪造月份。老板有效补充可保存为当前事实说明来源，与原件同样使用 source_id。错误识别结果和已经被替代的老板答复不作为独立过程档案保留。

source_id 对应固定内容，字节或有效说明变更必须新建来源，不能同 UUID 覆盖。归月只改变受控位置：先准备新位置、发布新来源清单，再清理旧位置。明确无用的来源通过discard退出，程序检查当前引用、正式采用、当前文档及读取/备份保护后，才移除登记并安全清理受控文件；清理未完明确返回pending。不能为保留全部旧答复滥用“必要”，也不能以退出资料为由改写冻结依据。sources 输入的旧位置不决定身份，幂等重试先核对已登记内容和当前实际位置。

事实分片保存一个领域和事实所属月的当前有效业务；跨期合同按生效首月归档，后续业务通过稳定业务引用关联，不反复复制合同正文。公司身份在清单，objects/sources/work/notes 为固定分片；政策/期初仍按各自事实家族归月，不另造隐式格式。一个业务只归一个分片，移月时从旧片移除并加入新片。

`current.json` 指向当前完整分片集合，含 format、company_identity、set_id、各分片相对路径/字节数/摘要。company_identity 的 working_period_to 保存已进入的工作月份，撤去该月唯一业务也不倒退；简化重建据此补齐资产等期间结果。缺省时取接账月与全部最终事实末月的较晚者，不采用电脑当前月。StandardPart 明确五种分片：FactShard、ObjectShard、SourceShard、WorkShard、NotesShard。事实片只含格式版本、归属、最终 Fact 数组及必要来源/对象引用，不含数据库连接、result_id、旧序号、冲正动作、请求日志或旧凭证编号。

程序生成所有受影响片到临时文件，核对当前目标未变后，以一次持久原子替换发布 current.json；未变的分片复用。清单切换后删掉不再被当前清单引用的片和临时文件，不保留历次清单。读取/备份持有集合锁直到引用文件读完，清理不能删除正在使用的当前集合。发布中断时按当前清单读取完整旧集合，按数据库当前目标重生成完整新集合；不能读到一半新一半旧。

开放期更正直接改写事实。闭期冲正归并到原业务的最终事实，发现期不再额外保留一条“用于重放的冲正”；真实后续退款、损失、收付款仍作为各自事实保存。凭证更正期间与事实原所属期间不同，这个区别只用于原账正式结果，不把纠错动作带进简化重建。

重建先核对清单、对象、来源和全部引用，按事实依赖顺序创建新库并计算，完成后再交付。缺少必要来源或事实时给清单，不能读取旧库补齐或从旧余额反推。文档不保存历次关账和操作史，重建无需复制旧冻结/冲正链；恢复这些内容应使用数据库备份。
