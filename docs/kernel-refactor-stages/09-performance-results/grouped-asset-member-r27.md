# 资产成员目录全范围验证：私有完整函数对照

本项使用固定 r26 源码与已完整核验的主 48 月合成库，只读比较默认资产页的原函数与私有替换函数；未修改生产源码或固定样本。所有计时均有后台建库负载，只用于诊断，不是第 9 阶段纯浏览器计时或发布验收。

## 现有语义与读取范围

`_selected_asset_member_heads` 先通过原 `_selected_asset_owner_events(complete_owners=True)` 认证截至当期的完整 owner 采用集合，再核对每个已采用 owner 的结果摘要和完整物理成员目录。主 48 月有 96 个 owner、1,176 个目录成员；完整目录查询返回 1,015,512 字节。函数产出 1,176 个顺序事件，资产页随后折叠旧消费与后续事件，最终 49 个 owner／95 个成员进入更严格的 `asset_members_many` 验证。后者在同快照先前已验证 48 个 activation owner，仅新增 1 个消费 owner／47 个成员；因此两处严格正文验证并未重复覆盖全部 1,176 行。

不能只核最终 49 个 owner。单独新建的 12 月合成副本中，篡改已被后月覆盖的旧消费成员 `summary`，原资产页仍以 `asset_batch_digest` 拒绝。当前完整范围承担了对旧目录损坏的拒绝职责；闭期采用、冲正及更正也须继续按原 owner event 顺序与发布证明选择。

## 私有完整函数 ABBA

候选仍调用原完整 owner 采用选择，验证全部 96 个 outcome 的锚定摘要、类型、成员数、行范围和 `membership_digest`；SQL 在同一快照将 1,176 个物理成员的身份、顺序、位置、子计算、封印、依赖、外来认领和正文与已认证 outcome 的完整成员目录比较。仅全部成功时由已认证 outcome 产生原顺序事件。任一 SQL 差异、非规范等价 JSON、格式异常或未知字段都回退整个原函数，保留原错误和解码语义；选中 owner 后段的严格证明不变。这是私有原型，并非生产可复用证明。

| 完整默认资产响应，插桩 ABBA | 原函数 | 候选函数 |
| --- | ---: | ---: |
| 完整响应哈希 | 相同 | 相同 |
| SQL 调用 | 1,487 | 1,487 |
| 返回行 | 10,846 | 9,671 |
| 返回值字节 | 8,315,112 | 7,299,624 |
| SQLite VM | 796.1–796.5k | 918.5–919.1k |
| 标准 JSON loads | 2,740 | 1,564 |
| 标准 JSON 输入字节 | 5,130,187 | 4,903,459 |
| 计算结果／typed fact 解码 | 288／96 | 288／96 |

候选减少了 1,175 个返回行、约 1.02 MB 传输和 1,176 次 Python JSON 解码，但 SQLite VM 增加约 122k。插桩本身使整页 CPU 达约 4 秒，不能拿其时间当页面收益。无插桩的 16 次 ABBA（各 8 次）中，原函数整页 CPU 中位数 312.5 ms、wall 322.3 ms；候选分别为 328.1 ms、336.3 ms。另一次较短的 4 次 ABBA 也为 351.6／354.5 ms 对 359.4／359.1 ms。并行环境和 Windows CPU 计时粒度限制绝对时间判断，但**没有稳定的完整函数净收益**。

损坏副本对照：旧消费摘要篡改、未知字段、成员行缺失均同码 `asset_batch_digest`；错行位置同码 `asset_batch_identity`；畸形 JSON 两者均抛 `JSONDecodeError`。等价但非规范 JSON 两者均接受，数据哈希相同，候选正确回退原函数。上述只覆盖这批实例；主 48 月无 owner 冲正，未以此声称冲正、更正和所有闭期坏源分支已完整等价。

**决定：本轮不落地。** 完整范围的目录验证有实证损坏边界，私有快路虽然省传输与解码，却把工作移入更重的 SQL，未证明净 CPU 改善。保留当前领域读取及固定 v1 合同。

原始私有脚本与未压缩报告保留在 `.tmp/stage9-r26-asset-member-*`。本目录的同名 `.json.gz` 为原始 JSON 字节压缩副本：`stage9-r26-asset-member-audit-r4`、`stage9-r26-asset-selected-heads-probe-r2`、`stage9-r26-asset-member-fast-ab-r2`、`stage9-r26-asset-member-fast-native`、`stage9-r26-asset-member-fast-fallbacks-r2`、`stage9-r26-asset-member-fast-negative`。
