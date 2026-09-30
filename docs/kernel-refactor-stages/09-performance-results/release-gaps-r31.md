# 正式交付待验清单（工作区 r31）

这份清单只区分**已有实现**和**已经完成的交付验证**。固定 r29／r30 是隔离的 `released / 1` 合成候选；仓库生产选择器仍为 `draft / 0`（`src/ai_accounting/kernel/schema_bundle.py:18-19`），正式三份合同文件仍不存在。r30 受影响组原回执为 511 通过、1 失败，夹具修正后相关 2 项通过；不能改称原组全过。当前 G 组仍在收敛，不能把工作区结果记到已冻结候选。

| 交付项 | 已有实现和证据 | 最小剩余动作 |
| --- | --- | --- |
| 首个正式目录／公司／内容 v1 合同 | `scripts/export_kernel_schema_contracts.py` 有只读候选、三件一次性独占创建和只读比对；`tests/kernel/test_schema_contract_export.py` 覆盖预计算失败、不覆盖已存在文件及部分写入回滚。`content_v1.py:361-510` 的规则源码摘要、字段解码和固定描述符已实现，`schema_bundle.py:145-182` 在生产 released 模式要求所有登记历史公司版本有核验器。固定候选的非空 v1 主 12／48 月核验通过。 | 性能与 G 组收敛后把生产 `STATUS/VERSION` 切为 `released/1`，先运行 `--candidate-v1` 审阅，再只执行一次 `--freeze-v1` 创建 catalog、company、content 三文件，接着 `--check`。保存三份字节摘要和 `production_bundle()` 指纹；以正式源码重新核验既有同结构合成库，测试新建、重开、备份恢复及 draft／旧体系拒绝。候选库核验不等于正式冻结。 |
| 离线前向升级和历史保留 | `offline_upgrade.py:131-303` 的显式入口先核对来源、操作状态和服务锁，再逐公司迁移、目录最后迁移，源目标在事务内按登记版本核验。`test_offline_upgrade.py` 覆盖活锁、未完成操作、源核验、目标回滚、重试及非空 v1→隔离 v2 规则漂移；`test_offline_upgrade_history_preservation.py`、`test_stage9_backup_upgrade_history.py` 覆盖权威历史、目录和任务回执保留／损坏回滚。`verify_local_package.py:83-106,2088-2125` 已实现包内真实 CLI 同版升级和运行 daemon 拒绝检查。 | 正式包生成后实际运行这些包内烟测并留完整回执，确认同版重复执行、运行服务独占锁拒绝、目录／公司不变及混合版本启动拒绝；未来 v2 仍只能用隔离合成合同测试，不向正式包注册假版本。大库迁移成本和正式版跨版本包尚未验证。 |
| 独立运行包与实际 MCP | `package_local_kernel.py:274-377` 复制受控软件、校验文件清单、打 ZIP、重定位并在包内 Python 启动前复查；`verify_local_package.py:995-2161` 有密码、工资／确认、实际办理、资料、单月关账、闭期更正、任务丢响应、备份恢复、HTTP 页面与 stdio MCP 的合成自检。两次开发包重定位及一次 GPT-6 Sol MCP 操作已有记录（阶段 9 主文档验证表），都不是最终正式包。 | 以新的空目标执行 `.tmp-kernel-venv/Scripts/python.exe scripts/package_local_kernel.py --output .tmp/<新包名>`，不得用 `--skip-validation`。保存包和 manifest SHA、包内自检报告；再由实际 GPT-6 Sol 通过**该包**隔离 MCP 复演并记录工具回执。仅 stdio 握手／脚本自检不能冒充实际代理验收。 |
| 两档原件与规模 | 固定 r14 的约 240 MiB 混合原件诊断有原始报告；约 1.5 GiB 候选的核验／备份／恢复及内存诊断也已保留，但不是最终源码。固定 r30 主 12／48 月完整核验与当前预览通过；主 120 月已构造、尚非最终完整核验。固定 r29 独立 120 月核验曾在高内存时中断。 | 正式 v1 源码下分别新建 240／1536 MiB 合成原件根，运行 `scripts/benchmark_stage9_evidence.py --source <正式源码> --root <全新.tmp根> --output <全新报告> --size-mib 240|1536 --profile mixed` 的完整核验、备份、恢复和前台读取；两档分别保存报告与包摘要。另完成主／独立 120 月注册完整核验，并用对应已核报告做独占五页 30 次正式计时。已有 r29 两档浏览器均未达标，不能借旧诊断替代。 |

最终验证顺序：固定待验源码与受影响组 → 同结构正式候选的规模核验和独占浏览器验收 → 性能与结构收敛后冻结三份正式合同并只读比对 → 正式源码所需核验及两档原件备份恢复 → 独立包构建与包内自检 → 实际 GPT-6 Sol MCP → 46 项状态收口。若冻结、构建或其后修正改变了被测代码或运行条件，再补相应性能验证；不在性能仍失败时先冻结正式版。只对同一源码和相应报告作结论；旧失败、占用来源未明的备份 WinError 5 与未通过的纯计时继续保留。
