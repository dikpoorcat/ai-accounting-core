# 项目文档索引

本文档目录同时保留“当前规范”和“历史审计记录”。新 Agent 应优先读取当前规范，只在追溯决策或复现历史问题时加载旧轮文档，避免把已经关闭的整改项误当作当前任务。

## 当前规范与验收

- [固定资产模块第一期最终验收记录](./fixed-asset-module-acceptance.md)：购置、启用、月折旧、出售/报废和逆序冲正闭环的最终门禁与独立审计结论。
- [固定资产模块开发基线](./fixed-asset-module-development-plan.md)：已完成阶段的稳定契约、官方依据、边界和验收标准。
- [工资模块开发基线](./payroll-module-development-plan.md)：当前业务边界、数据模型、工具契约和验收标准。
- [工资模块第七轮验收整改任务书](./payroll-module-acceptance-remediation-round-7.md)：当前最终验收记录和门禁结果。
- [多 Agent 协作与本地质量验证手册](./agent-collaboration-and-local-verification.md)：任务拆分、共享工作区、独立验收、临时数据库和安全审查误判处理经验。
- [记账引擎选型调研报告](./记账引擎选型调研报告.md)：自研确定性内核的技术选型依据。

## 历史记录

- `payroll-module-acceptance-remediation.md`：首轮整改。
- `payroll-module-acceptance-remediation-round-2.md` 至 `round-6.md`：第二至第六轮整改及当时的阻断项。
- `记账内核现状与第三轮整改解读_2026-08-09.md`：第三轮时点的非技术解读。

历史文档是审计轨迹，正文中的“待整改”“不通过”和测试数量只代表当时状态；当前结论以固定资产第一期最终验收、工资第七轮最终验收及仓库最新测试结果为准。
