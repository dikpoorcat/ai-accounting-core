# 银行批量代发

当前内核从已正式确认的未付工资、奖金、劳务和明确归属员工的报销生成银行付款指令。金额与剩余应付来自公司库，不接受请求方临时覆盖金额或提交自由分录。代发文件生成、重试、下载均不代表实际付款；银行扣款发生后仍要依据实际流水登记付款及核销。完整的读取和任务规则见[新内核的银行代发文件](kernel-payment-exports.md)。

## 当前办理顺序

1. 按运行时 `finance_local_schema` 查 `save_payee`、`preview_export`、`confirm_export` 的字段合同。先留存银行模板原件及收款姓名、文本账号与证据；`save_payee` 为收款资料追加版本，不把收款账号当作公司资金账户。
2. `preview_export` 从当前正式来源、未结余额和资料完整性生成核对内容。省略来源 ID 为该归集月完整范围；指定来源 ID 时明确为部分范围。核对人员、金额、用途、原件及缺项，不能靠缩小范围掩盖应处理事项。
3. `confirm_export` 核对同一预览摘要和核算、资料、管理版本，保存冻结计划与持久文件任务。用 `jobs` 查看结果；需要处理文件任务时走 `run_export_jobs`。只有任务成功且文件内容已核验，才交付输出目录中的银行工作簿和核对报告。
4. 后续付款、更正或收款资料变化不会改写已确认的旧任务。需要反映最新状态时重新预览、核对并生成新任务；实际付款另行入账。

收款资料和公司业务说明分别承担收款指令与管理说明的职责，不能代替正式账务来源。账号保留前导零，不从金额、名称或用途猜测身份。生成的四列 XLSX 仍须以银行实际接收结果核对。

## 历史边界

旧 `finance-mybank`、`python -m ai_accounting.mybank_export`、`finance_preview_mybank_export`、`finance_generate_mybank_export`、`finance_import_mybank_payment_source` 和 `0008_mybank_payment_metadata` 属于退役体系，不是当前运行、迁移或恢复指令。旧的 `org_id`、`template_path`、`recipients_path` 请求文件及 `source_hash` 生成流程不能传给当前内核。原设计和命令样本由 Git 历史保留；当前只使用 `finance-local` 的类型化命令和运行时 Schema。本阶段仍是 `ai-accounting-kernel/2` 的开发合同，不宣称已发布正式 v1。
