# 第9阶段：凭证贡献复用候选撤回

2026-10-04。本轮全贡献读取候选已撤回，三个生产文件恢复精确原字节，候选专用测试保存后退出活动测试集。候选与历史r2全贡献方向及r4／r5局部变体相近；本轮没有在编辑前完成全局历史对照，编辑后由根节点发现并补查。这是本次范围审查遗漏及修正，不能把重复尝试写成新根因。历史负面结果保留，详见[金额与身份组](owner-money-identity-group-20261003.md)。

主48同一合成样本在旧固定dd86／753文件和候选d2accba14040d5b7412dc48f721e5753612f50201a4bd2231187b1cc6eae86ad／756文件下，各执行context、brief、funds_first、employees、assets、quarterly_report六入口一次严格只读插桩工作量诊断。不是纯计时、资格、HTTP、浏览器或active preview验收；不产生新的性能、开发包或实际MCP结论。正常正向／反向关系重复的新范围正在只读审查与实现，收益尚未验证。

## 工作量与撤回依据

| 入口 | SQL | SQLite VM | 返回行 | 返回值字节 | 结果load／decode | 贡献decode |
| --- | ---: | ---: | ---: | ---: | --- | ---: |
| brief | 193→201 | 868000→1290300 | 16338→22893 | 7507561→14789808 | 1009→1530／1008→529 | 0→1001 |
| quarterly_report | 255→254 | 948200→948200 | 24869→24868 | 10684375→10684335 | 1007→1007／3→3 | 1004→1004 |

brief少479次结果decode，却新增贡献decode、更多SQL、结果传输与VM工作，不能称为整体省工。report仅少一SQL、一行、40字节，VM与结果／贡献decode未变，不足以保留单独物理事实证明缓存。context工作量相同；funds_first、employees、assets主要实际工作相同，funds_first的VM差100为100指令采样粒度，不能解释为收益。返回值字节不是磁盘读取量，Python分配峰值不是RSS，插桩耗时不作为500ms验收或稳定延迟收益。

两份原始输出均记录前后文件SHA、身份、state、schema_history、登记与repair守卫相等，固定source inventory未变。旧资格回执仅用于确认同一现有合成样本，不作为候选资格。before仍为旧dd86，与candidate还存在已完成的输入格式及非负合计组差异；不能仅据两次观察宣称所有差异都由本候选造成。

## 失败、保护与精确恢复

首次comparison因完整响应SHA断言失败而退出：context相同，五个主响应不同。源码静态核对发现`read_context.read_version`绑定source_build；没有实际payload diff证明其余全部字段一致，因此**完整跨源响应等价未验证**。fix1随后误读回归metadata中不存在的`files_after`而KeyError；实际回执只有`files_before`与`files_unchanged`。两次失败及原脚本保留，fix2只分析已有回执，不把失败改为PASS，不新增业务读取。

候选六文件定向检查曾79 PASS／103.94秒，随后守卫合并与资产排除边界改变未重跑。该数字仅是早先checkpoint，不能称最终候选通过。三个生产文件`dashboard_reads.py`、`query_reads.py`、`report_open_contribution.py`已恢复保存的精确原字节；新增prototype-only测试原字节备份后退出活动测试集，原有业务断言保留。fix2收口时172个生产／受影响测试文件SHA与同次316项成功回归精确一致，未重跑未改变的316项。此身份核对不产生新资格、五页、开发包或实际MCP结果；后续关系组若继续编辑，应独立记录其源码与验证。

## 独立证据归档与阶段状态

[独立原字节归档manifest](journal-contribution-withdrawal-20261004-raw/manifest.json)保存工作before／after、helper、三版comparison及首次失败、候选保全／撤回脚本、capsule manifest／withdrawal、三文件before／candidate及prototype测试。逐件按实际内容审查：SQL仅模板、不保存绑定值或结果正文；guard只保存合成文件路径与身份／状态行SHA，不含身份行内容；源码中的snapshot token为Python上下文对象，未保存访问令牌。合成标识与路径保留，PID、凭据、访问令牌、真实私有资料与数据库原件排除。gzip mtime0，逐件SHA及解压回环核验，原始总量≤5MiB、gzip≤512KiB，不覆盖旧归档或修改原件。

当前第9阶段仍实施中，旧dd86五页FAIL50／150、draft／0、正式冻结／正式包／运行切换延期的状态保留。候选撤回不是最终验收；正常关系重复组尚未验证收益，其余规模、按需读取、最终开发包及实际AI MCP缺口仍按主阶段记录。
