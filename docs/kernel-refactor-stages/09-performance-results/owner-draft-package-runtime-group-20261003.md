<!-- @format -->

# e0e 开发包与隔离 MCP 恢复读取证据

本轮开发包保持目录库、公司库 `draft / 0`，正式合同冻结与首个正式版本延期。现有 5173、真实资料根、公司及负责人身份不切换。本报告记录 e0e 包的实际自检、旧 97b9 业务来源和当前包恢复读取；不宣称全部第 9 阶段门槛或独立 AI 完整流程已通过。

## 固定源与实际开发包

| 项目 | 当前实际值 |
| --- | --- |
| 密封源 | `.tmp/stage9-build-source-draft-runtime-query-group-20261003`，743 文件 |
| source SHA | `e0c8cd4d7c03db0743c8e2b3d15c3c0f668e4e0c690b3cb388942fc2b55e0871` |
| source manifest SHA | `af0fb0e0ea624d992b76821098516cff930c5f5db22b008e798876e8254c8ef9` |
| 软件包 | `.tmp/stage9-development-delivery-runtime-group-20261003` |
| 软件 ZIP | 34,623,254 B，SHA `b68ce226cc33a7ce2d2510fb98ecb57aad99c58fb6c3bc37333dc8d9db82449f` |
| package manifest SHA | `cab9fa402fe3e627b5eae7be0ec365495d037f2cf092e12844239ec2b91e021b` |
| calculator build | `local-kernel-2:7a102debec1398aa96fe3cb916d7af2264c7c50c66cd5ed1f9c396871317927c` |
| runtime | 包内 Python 3.12.13、SQLite 3.53.1，隔离导入 |
| catalog / company | draft / 0；结构指纹分别 `b484d315272bb64de3856b458f04d2d4881da05db7552250864260916c69a4ee` / `c9f9f7051bca67f1241ee5c89676fb9476bc819dc8f92c1a0c0bd1e940f459cb` |
| verification JSON SHA | `3041e3e4974dda2e4ad6ed49f2235d48a947b5ca2e830b0e028978a056a817c7` |

运行 [实际构包 runner](../../../.tmp/stage9-development-delivery-runtime-group-20261003-runner.py) 前绑定 sealed `src`，核对完整 source inventory、实际 draft factory 与此前成功前端构建的 71 个源码文件和 27 个静态文件，再执行该源的 `scripts/package_local_kernel.py --output`。未传 `--skip-validation`。构包完成后再次核 source inventory/manifest，字节一致。runner 使用仓库 `.tmp-kernel-venv`，包自检使用新 relocated 目录及包内解释器，没有修改 seal、产品代码或既有包。

## 实际自检覆盖

[必须的 relocated 自检回执](../../../.tmp/stage9-development-delivery-runtime-group-20261003-verification.json) 为 `passed`：4212 个软件文件完整校验，160 次实际 CLI 调用，真实生产 stdio MCP 握手、HTTP 鉴权与看板读取、类型化业务发布、更正、负责人精确批准、单月关账、冻结保全、ZIP 备份恢复、任务失败三次上限及明确重试均已执行。源码/包/运行时 build 相同；[source receipt](../../../.tmp/stage9-development-delivery-runtime-group-20261003-source-receipt.json) 与[包结果](../../../.tmp/stage9-development-delivery-runtime-group-20261003-result.json) 保存具体身份和 SHA。

原生窗口项包括实际复制的 Pythonw/Tk 窗口、私有 HTTP transport 和自动输入的合成测试密码；同版批准也使用测试专用 transport。它们证明工具与批准绑定，不能写成真实负责人手工密码验收。`packaged_offline_upgrade=null` 是 draft 路由未执行正式升级，不能写成 released 升级通过。`template_bytes=24322` 只证明模板读取，不能代替真实季度文件成功。本包后续两处 Worklist 修复以独立 AI 的当前包响应另行核对，不从自检的旧场景断言推导。

## 原来源与隔离运行位置

| 位置 | 包与实际职责 | 已有证据边界 |
| --- | --- | --- |
| `stage9-final-draft-agent-fix1-20261003`，原 host 51132 | 旧 97b9 开发包；独立 AI 在原甲/乙实际登记、工资确认/发布、关账和生成季度文件 | 是业务与备份来源，不证明 e0e 行为；本次没有切包或替换身份 |
| `stage9-final-draft-agent-restore-20261003`，3912 正常退出后同身份 host 27232 | 同一旧 97b9 包；恢复初始 `3c04...` ZIP，随后保持身份和业务接续读取 | 原恢复 29 facts/5 calculations/4 vouchers/1 close/8 evidence；247 公司表、6 个非安全 catalog 表与既有 32 份通道文件 SHA 相同。正常 logout/login 的会话与审计变化独立处理 |
| `stage9-final-draft-agent-current-runtime-20261003`，host 39716 | 当前 e0e 包；独立新测试 catalog/owner，经生产登录和生产 stdio MCP，AI 自己恢复 `9089...` ZIP | 初始 empty ready 仅握手；实际恢复/读取看下述 13 份回执。未启动 JobRunner，未重登记或重算 |
| `stage9-final-draft-agent-current-runtime-published-20261003`，host 5244 | 同一 e0e 包；仅为已正式采用的新 `a91...` 来源建立全新隔离测试 catalog/owner 与生产 MCP | AI实际恢复42 facts/7 calculations/4 vouchers/1 close/11 evidence，四项coverage verified、limitations=[]；14项必要生产MCP读取完成，旧三个host/root不变 |

新 root 的安全负责人独立建立，不复制原口令；恢复公司及数据库仍保持来源身份。原甲 company `7830bf7b192946b8b22f96bf27746c51` / database `684f6b25490542cd84c6ec86186f0d01` 在9089和a91的e0e恢复后均相同。各位置均为明确合成验收，不涉及真实资料根。

当前外置 [restore helper 元数据](../../../.tmp/stage9-runtime-restore-agent-tools-20261003/helper-receipt.json) SHA `2eaad0e3dc3cc4a05981570ba1d4a3b2f343d2fe8318a16ad5e328d02d325928`，共同错误响应/resume relay SHA `b6ba71077eeed6a1768c3b5466bf46f6000bbd4651d05927f9ad7124b8813e85`。它精确绑定当前 target/package 和旧来源 package manifest，并沿原公开公司身份核对；不属于 e0e seal 或软件包。8 项 path/manifest/mock guard 只证明工具边界，不计入业务验收。非 JSON MCP 错误返回和旧 host 接续原件保留，未把未知或失响应自动写成业务失败。

## 9089 快照的当前包复验

独立 AI 经原 host 的真实生产手工 backup 得到新的 206,607 B ZIP，SHA `9089ba5ac4f441a5ef7838b31b60bf6a04900e29304c29a998267976f87e2a50`；原初始 ZIP 不变。e0e 的真实 `restore_company` 回执 `82d3b2db762242a989c73dbda93d47af` 返回 verified：39 facts、5 calculations、4 vouchers、1 close、10 evidence，sources/historical_adoption/projections/read_indexes 四项 coverage 均 verified。

本目标实际记录 13 个 MCP 请求及响应：schema；companies；restore_company；operations；company_context；dashboard_close_review；preview_report_export；workflow；period_readiness；两个 find_facts；两个 jobs。它们用于恢复和必要读取，未重新执行旧 142 次业务流程。[AI 汇总](../../../.tmp/stage9-draft-independent-ai-20261003.current-runtime-verification.json) 对照了目标实际回执；闭期 close/preview digest、owner review、Q3 三表与 digest 等于来源，身份保持。这是当前 e0e 包实际读取结果，不是沿用旧包成功。

| 本次 bug | 当前包实际响应 | 结论 |
| --- | --- | --- |
| 已发布工资中的管理确认事实无 evaluator，却被当成缺计算 | `workflow` 回执 `70a4e1fef421407f9117d61ca75a3d7d`：payroll accounting ready，4 facts/1 calculation/0 pending/issues=[]；实际工资发布已存在 | 该快照上的误判已消除；没有用清 pending 或补造计算制造 ready |
| 真正 succeeded 的季度文件任务 period 缺 label，严格响应失败 | 同份 workflow 和 `period_readiness` 回执 `d8361ac5c6a44ba6b76386ee2bb5c0d1`，同 job `446d3ae1cfc742c4a3f3654e6e692cee` 返回 `{year:2026,quarter:3,quarter_start:2026-07-01,quarter_end:2026-09-30,label:2026 年第 3 季度}`，status=succeeded | 两份完整生产响应正常返回，当前快照验证修复；月份型其他任务仍使用原字符串合同 |

该季度 XLSX 实际在原来源生成，文件 SHA 与真实任务 result 均为 `c5b50a8aa5c44156ba1493e4b1b639540aea5948e484ff60bfeef9d88e74179e`。恢复库 result 仍指向原来源文件，`current_file_availability=not_checked`；不得写成 e0e 目标已重新生成文件或已校验目标目录文件存在。

9089 **不是完整外部办理采用验收**：当时 external_completion 和 external_basis_review 仅 save/find，两个都有 evaluator，目标读取均 `pending=true`、没有正式 calculation；9 月义务的 actual_completion_status=pending、basis_review_status=not_reviewed。5 个 calculations 没有因此增加。个税办理日期及 matched 管理事实存在，不等于两类正式采用完成。该快照与其 13 份回执继续保留，不被后续成功覆盖。

原独立 AI 随后通过公开 preview/confirm 正式发布 external_completion 与新 external_basis_review，合法 withdraw 原误记 review；源 9 月冻结完整 JSON 相等。[新来源证明](../../../.tmp/stage9-draft-independent-ai-20261003.external-published-source-backup-proof.json) 索引了实际提交、撤去、发布与备份回执：job `9d68ff6bdc7545c5a363bb1223844f38` succeeded，42 facts/7 calculations/4 vouchers/1 close/11 evidence，四项 coverage verified、limitations=[]。新 ZIP 为 220640 B，SHA `a91cf879434863593dd9dc3c41e38a047e62bf75ef55b01f82a4dd5fd7cfc321`；旧 9089 和初始 3c04 归档保持。

当前包的新目标host5244使用独立catalog `d5796bddbe8f473babc7e8875fae4d6a`。外置strict helper SHA `fccded36e050c0bc56086c4a2fa3a73cb9a2089ed8e1e08be422b8fd7cfb705d`，只替换accepted target精确目录名，其余安全、包、来源、resume和错误transport不变；8项metadata/mock guard只证明工具边界。诊断窗口结束后，独立AI实际执行a91恢复和14项必要读取，参见[独立目标证明](../../../.tmp/stage9-draft-independent-ai-20261003.published-runtime-verification.json)。生产恢复返回四项coverage verified、limitations=[]，目录操作succeeded；身份保持，三业务版本为19/30/17。

目标两项外部事实都有直接发布，pending=false；新核对引用本次正式办理，个税分别显示actual completion completed和basis review reviewed。原误记核对保留deleted历史及null calculation，未伪造成新计算。源／目标外部事实完整JSON相等，9月冻结完整JSON及Q3三表／摘要相等；季度文件原任务仍succeeded，期间标签、year、quarter和起止日完整，工资、普通业务、税务的工作清单不再误报管理事实缺计算。报表文件仍为原来源产物，未在目标重新生成或下载。该14项读取时最新继承备份仍running/1/null；该历史状态和原48份归档保留。本次随后仅接续该任务的真实结果见下节。这些响应验证固定e0e包，不能扩大成后续工作树包或全部任务恢复通过。

## 恢复快照中的 running 任务

源 backup 先提交 running/attempt+1，然后 clone 公司快照、生成 ZIP，最后在源库写 succeeded/result。因此 ZIP 内包含该备份自身 running/attempt1/result=null 是当时一致快照，不可能包含生成后才写入的成功状态。9089 目标的 jobs 回执 `6e452478f4274c6b8e94c710187db3c8` 仍为 `fa82f087344e495e83708710b4ae365e` running/1/null；来源后续 succeeded 不能替换目标状态。初始 3c04 恢复也保留其自身 `5fc0...` running。

生产 `restore_company` 保留不可变 jobs payload，`LocalService` 连接不启动 worker；两个测试恢复 host 也没有 JobRunner。生产 daemon 启动时则会回收/继续 portable_backup、payment_export、report_export、tax_import，按原 payload.directory/output_directory 执行，最多三次。catalog 的 company_operation 恢复另属目录发布流程，不等于公司任务接续。

跨 root 恢复后启动 daemon、run_jobs 或 retry_job，可能让继承的旧任务写回原来源路径；配置新备份目录不会改写历史不可变计划。本轮在只读预检、完整jobs候选核对及对该组既有合成ZIP的正常权限／锁检查获明确授权后，仅执行一次公开 `run_jobs(company_id=甲,limit=1)`，没有重启host、启动JobRunner、retry、改payload或重定位。`limit=1`是每队列最多1项；执行前实际完整任务表只有该备份为可执行候选，付款导出、报表导出均无候选，不能泛称此命令只执行指定job。

预检生产jobs回执 `bfe7c8138f8d4aa98f475f19677ff3eb` 与执行前完整jobs回执 `a5ad6b0f88e84f0786cec7b38b0535f6` 均保留running/attempt1；执行回执 `4826c35467a9466ab65c92975067e6dc` 的backups数组实际1项，exports和reports均为空。精确jobs回执 `aaba4f9eea324dc2baf01a173bf53876` 证明同一 `9d68ff6bdc7545c5a363bb1223844f38` 为succeeded/attempt2、idempotent_replay=true：生产核验既有a91 ZIP及当前输入库后幂等复用，42 facts／7 calculations／4 vouchers／1 close／11 evidence，四项coverage verified、limitations=[]。没有生成新快照或ZIP；结果仍精确指向原a91产物，身份、draft／0和不可变I/O payload保持。

独立前后保护记录证明原来源公司主库／WAL、a91 ZIP及既有publication lock的字节SHA相同，目标11张必要业务／审计表的行数及摘要相同，目标结束后无活动任务。此比较不声称ACL或锁元数据零写入，也不把目标jobs状态更新隐作全库字节不变。原14项读取时的running与本次attempt1→2成功分别记录；已被后续读取覆盖的 `inherited-job-preflight-read.json` 不作为执行前证据。

该真实接续只证明固定e0e包、当前唯一继承备份、原输出位置已存在匹配且验证成功的a91 ZIP这一场景。跨目录接续／重定位、缺少原ZIP、其他任务及一般产品政策仍未验证，不能推广为恢复后自动启动worker的普遍通过结论。

## 剩余门槛与避免重复

| 项目 | 当前证据 / 下一步 |
| --- | --- |
| 同一 e0e 包构包与必须自检 | 已通过；源码、包、工具未变时不重复 160 CLI 全套 |
| 两个 Worklist 修复 | 9089 当前包真实响应通过；保留 narrow 证据，不重复登记/关账/文件生成 |
| 外部办理与核对正式采用 | 来源已真实发布／合法撤去误记review，新a91备份及当前e0e新目标真实恢复、14项必要读取通过；9089 pending旧证据保持 |
| host 中断恢复 | 旧 97b9 目标真实同身份接续已核；不能扩大成当前 e0e host 的 restart 或所有失响应写入均通过 |
| 备份恢复任务行为 | 当前唯一继承备份在既有匹配a91 ZIP上一次真实run_jobs成功幂等接续；原running证据保留。跨目录／缺ZIP／其他任务及普遍策略仍未验证 |
| 规模/性能/evidence 容量 | 以 root 当前固定源的独立资格、浏览器和容量原件为准，本包 selfcheck 不替代这些门槛 |
| 真人批准与正式交付 | 测试 native 不代替真人；released freeze/upgrade/真实入口切换本轮不执行，正式版延期 |

## 原件与归档范围

本组只归档已核公开合成 JSON/日志和本目标明确的 13 份请求/响应；软件 ZIP、业务 ZIP、SQLite、`.service.json`、Windows credential store、owner 口令、session token、native 私有通道和含测试口令的 helper 源码不纳入此证据压缩包。大软件/业务包保留在 `.tmp`，本文与回执记录其 SHA。

本组[独立manifest](owner-draft-package-runtime-group-20261003-raw/manifest.json)已在纯计时窗口结束后生成：43份原件，共3,473,753 B；gzip共444,644 B，mtime=0，逐份保存原始／压缩SHA并解压逐字节核对一致。包括e0e source receipt/result/verification、source/software manifest、实际构包runner与stdout/stderr；9089 target初始public ready／结果、helper元数据与8项guard结果；AI current-runtime verification/source-backup-proof；旧restore-resume verification与保全result；9089 target requests/responses的13个固定ID。该归档固定在9089阶段，所有`.tmp`原件保留，旧manifest未改。后续a91正式来源与新目标14项读取另行归档，不追加或覆盖这份manifest。

后续a91组现已单独[归档48份原件](owner-draft-published-package-20261003-raw/manifest.json)，manifest SHA `40d2bf14957ddb329c995aa4041743c664f2700d4cceae7e215b513cd4129fb9`。包括两个独立证明、新目标14项请求／响应、原来源9项正式采用／撤去／冻结比较／备份回执；公开schema约2.5MiB也保留。原始共2,664,899 B，gzip195,002 B，mtime=0，逐字节解压比较且归档前后原件相同；不含数据库、ZIP、helper秘密通道或任何口令。9089的43份旧manifest和失败记录未改。

本次继承任务接续另有[13份小组原件归档](owner-draft-inherited-job-resume-20261003-raw/manifest.json)，manifest SHA `272a888ebacb4cec7642613f4337eed884c8906f138aa5cc6c9afd0d6c37f6c1`。固定256 KiB预算，原始39,396 B、gzip13,183 B；仅保留只读预检说明、resume before／after／io-before／verification，以及上述4项实际生产MCP请求／响应。mtime=0、前后原件SHA相同、解压逐字节一致，并拒绝password／session_token／capability／secret／privatekey等秘密标量。原48份manifest及14项读取时的running证据未追加或覆盖；数据库、ZIP和私有渠道均不压缩。

首次归档预检因临时工具的 3 MiB 上限拒绝，尚未创建归档目录；原失败 stdout/stderr 保留在 `.tmp/stage9-owner-draft-package-runtime-archive*`。公开 MCP schema 本身 2,547,865 B，软件 inventory manifest 725,551 B；将该小组元数据预算明确调为 4 MiB 后成功，未删证据或放宽秘密值拒绝。收尾只读取既有公开 JSON/日志、压缩与字节核对，没有测试、MCP、数据库、服务操作或大包重新压缩。
