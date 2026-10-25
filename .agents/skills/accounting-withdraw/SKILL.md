---
name: accounting-withdraw
description: Withdraw selected accounting businesses through accounting_apply, or cancel one whole open month and clear its disposable source materials through accounting_cancel_month. Use for 撤回记账、撤销记账、撤消记账. Requests to implement or change this instruction are repository development.
---

# 撤回与整月撤销记账

在会计操作模式中执行老板已明确交办的范围。先读取当前 MCP 的 `accounting://guides/work`、`accounting://guides/commands` 和 `accounting://contracts`，使用 `accounting_context` 核对当前公司、工作月份和关闭范围。公司或范围已经明确就直接办理，只澄清仍会影响对象的歧义，不要求老板提供内部编号或重复批准。不修改仓库实现，不直接操作数据库或删除资料文件。

## 单笔、指定多笔撤回：保留原件

单笔先用 `accounting_get` 读取当前身份、序号、事实及结果。多笔按指定范围用 `accounting_find` 完整接续分页，再核对必要关联。通过 `accounting_new_ids` 分配 `command_id`，调用 `accounting_apply`；每项提交 `op="withdraw"`、原 `business_id` 和当前 `expected_seq`。同一身份只出现一次，相互依赖且均在已授权范围内的业务放进同一原子单。

这类撤回用于 AI 纠正误记，默认保留原件和来源登记。不能为通过依赖检查自行扩大范围、撤掉其他月份业务或拆散原子关系。闭期按原业务指南通过开放更正期形成必要关联冲正，保留冻结历史。提交结果不明时按原身份查询，确认结果后接续，不换身份重复办理。

## 整月撤销：清理本月资料后重新开始

只有老板明确要求“撤回／撤销某公司整月全部记账”时，才使用 `accounting_cancel_month`。“撤回9月工资”等一类或指定多笔仍属批量撤回，不能仅见月份就清理原件。它由程序查齐并撤去该开放月份所属的业务、清理本月待办、直接替换最终月份说明，并退出该月已无必要引用的原件和来源；不能让 AI 分领域查齐后自己拼批量请求。公司、数据库、负责人身份、其他月份、长期安排和必要冻结依据保留。

先调用 `action="inspect"` 取得实际月份范围和 `head`，再用新 `command_id`、同一公司月份及该 `expected_head` 调用 `action="cancel"`。这是防止旧请求误撤后来新记业务的并发检查，不是老板批准步骤。整月办理沿用已有公司备份安排；不新增后台备份门禁。关闭月份不适用此入口，不能清除冻结账务及原件。

`source_ids` 是本月归档候选，inspect 没有执行删除，`discarded_source_ids`、`retained_source_ids` 为空，`file_cleanup.state=not_started`。cancel 在撤去本月引用后判断实际可退出的资料；仍用于跨月业务、长期说明、模板、冻结或其他有效保护的来源列入 `retained_source_ids`。这些资料继续保留，向老板直白说明，不能宣称本月资料已全部清空。

确认 `state=cancelled`、`documents=ready` 和 `file_cleanup.state=complete` 后才报告全部接续完成。文档 pending 沿 `accounting_documents` 接续；文件清理 pending 由后台沿旧清理标识自动接续，不能重复discard来催清理，以免退出后来重新导入的新登记。响应丢失先 inspect 核对当前范围，不用新 head 自动再撤一次。冲突时重新读取实际范围，不能拿旧 head 撤掉新业务。既有资产等自动延续到本月的结果仍按原业务接续，不能误删其其他月份源业务；工作月份不会因此自动倒退。

## 再次记账与误撤恢复

只要求撤回或撤销时，完成核验与接续后停止，不自行重记。单笔或指定多笔撤回用于自纠，原件直接复用，只重新核对受影响业务和清单项，继续原办理位置，不把整月退回第 1 项。整月撤销后重新开始，才从第 1 项收集和核对本期资料；已清理资料由老板重新提供，长期事实和其他有效事项继续复用。现有月份说明会明确本月待重新提供资料、从第 1 项办理；因其他工作仍引用而保留的来源，不代表本期资料已经收齐。旧撤回结果、待办里的 `known_fact`、旧完成标记及失效月份说明不能作为本轮完成依据，不自动快进到第 4 项。

开放期重新整理的业务可以用新身份 `create`，不要求找回已删除的旧正文或旧编号映射，不为此向老板提问或挂阻断待办。旧墓碑继续阻止迟到的旧创建复活。明确恢复某笔误撤业务时，查明原身份和当前序号，以完整正确事实执行 `restore`。新请求与回复丢失后的重试分别处理：重试始终沿用原请求和身份；涉及冻结历史或仍有效关联时，不能用新身份绕过。
