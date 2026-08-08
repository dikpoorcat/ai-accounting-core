# Repository guidance

- This repository is a deterministic accounting kernel. Never add an API that accepts arbitrary debit/credit lines from an agent.
- Monetary posting values are integer fen. Use `Decimal` only for rates and tax calculations; never use binary floating point.
- A posted voucher is immutable. Corrections must create linked reversal vouchers.
- Missing business facts must return `needs_information`; do not introduce inference defaults that change accounting treatment.
- Every new event type needs balanced-entry tests, idempotency coverage, reversal coverage, and an explanation trace.
- Tax rules must be effective-dated and include a primary official source URL.
- Phase 1 is a private pilot, not a legal book, tax return, or filing system.


## Git 格式规则

- Git 提交信息统一使用中文。
- 提交信息禁止只写一行标题。第一行写简洁的中文标题，空一行后使用 `1、...`、`2、...` 的编号正文，正文间无空行，逐项说明本次实际改动；即使只有一项改动也必须写正文。例如：

```text
完善公众号消息处理

1、实际改动1
2、实际改动2
```

## Git 安全规则

- 每次操作前检查当前分支、上游、工作区状态，以及待推送和待拉取提交。
- 保留已有用户改动，不得擅自回滚、覆盖、清理、暂存或混入本次提交；有冲突风险时停止并说明。
- 只提交本次任务相关文件，提交前检查差异，排除无关文件、凭据、临时文件和运行产物。
- 本地分支必须跟踪同名远端分支。未经许可，不得强制推送、改写已发布历史或删除远端分支。
- 部署前确认两端仓库分支正确、工作区干净、提交符合预期；部署后确认 GitHub、Windows 和 Linux 的提交哈希一致。