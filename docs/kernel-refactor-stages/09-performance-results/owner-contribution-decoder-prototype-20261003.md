<!-- @format -->

# 老板看板贡献正文与结构解码原型（2026-10-03）

本轮不采纳贡献正文共享缓存或 native 结构解码改动。前一假设被既有实际调用原件否定；后一原型在指定样本上行为等价，但没有局部收益。生产实现、固定源码、合同、数据库、身份和服务均未修改；这不是整页 500 ms 验收，也不永久排除其他数据或调用组合下的优化。

## 假设一：两个证明职责重复返回同批正文

排查范围为五页默认读取及其公共调用方，重点是 `report_open_contribution.py` 的 `verify_published_source_bindings` 与 `read_open_contributions`。前者读取正文、核对 checksum 与独立 immutable anchor，并提取自身 source binding；它证明选中源的保存字节，不能代替完整贡献语义。后者还必须解码完整贡献、核对依赖 parents、完整 source bindings 和行内容。

证据来自已有 `.tmp/stage9-owner-main12-summary-groups-native-work.json` 和 `.tmp/stage9-owner-main12-summary-groups-dispatch-profile-first.json`，源码 SHA 为 `fe3a0ba43267bb05f3159bf38aeca13bf90f8cdc8c9b774882a6a560f27bc8f6`，合成主 12 月样本、2016-12。这两份旧原件不进入本次独立归档，旧清单保持原样。

| 实际路径 | own source binding 调用 | 完整贡献读取调用 | 正文 SQL 返回 | 完整贡献解码 |
| --- | ---: | ---: | --- | ---: |
| brief | 1 | 0 | 1 次，495 行，1,557,339 字节 | 0 |
| quarterly_report | 0 | 2 | 合计 1,004 行，3,281,824 字节 | 1,004 |
| funds / employees / assets | 0 | 0 | 无这两项正文查询 | 0 |

因此这组真实请求中，两种职责没有同时执行，同请求 IDs 交集的假设不成立。报表全部 SQL 返回 9,495,754 字节，其中贡献查询占 3,281,824 字节；其余包含冻结 flow、分类头、来源头和选中行，不能把约 9 MB 全部归为重复贡献正文。

简报调用位于 `dashboard.py` 的活动分类分支；成功 journal 复用与 SQL 回退两处是互斥路径。报表调用位于 `report_projection._party_delta` 和 `reports._report`。`read_open_contributions` 已按当前读取快照缓存成功验证的贡献，只查询 `missing` IDs，并在整批 checksum、结构、来源与依赖核验完成后更新缓存；两次函数调用不等于同批正文二次读取。`selected_open_line` 还复用已转换的 resolution。现有原件未显示重复完整贡献解码。

处理决定：不新增跨职责 raw body 缓存，不把 own binding 当作完整贡献证明。其他按需分支、不同数据分布或同请求组合未实测；若未来实际调用同时命中这两种职责，须重新提供精确 IDs、返回字节与成功证明范围的证据。

## 假设二：把 Python 结构遍历下沉 native

`_decode` 从 JSON 得到 Python 容器后逐项核对类型、keys 和固定行长度。既有插桩 profile 中累计约 123 ms，而同样 1,004 次贡献的正常 dispatch profile 约 34 ms；前者不能直接当作可节省的页面预算。

独占原型使用 Pydantic core `SchemaValidator.validate_json` 校验窄结构，再保留 publication identity 与 parents 排序／唯一性检查。原 `_decode` 是行为 oracle，native 校验失败回到原验证，保留重复 JSON 成员的 last-wins 行为和原错误码、消息、details。异构固定行数组还原为 list；没有改变正文格式或业务规则。

等价目标保留原有格式边界：空字符串可接受；binding digest 只检查长度 64，不新增 hex 限制；整数不接受 bool 或 float，但不新增金额范围／正负约束；行末 dict 与 resolution 内 list 的任意 JSON 内容保留。它与职责更窄、规则不同的 `_OWN_SOURCE_CONTENT` 不互换。

本次源码 SHA 为 `1c37c576cb0026b0b39c199b74672a60890c43a7d9e2b944416179fe2bf398e9`，脚本 SHA 为 `ef279e2709da34d43931446c3a99c1cd74ccf2a6644160ed99a65e0d53bc417a`。脚本绑定合成主 12 月样本已有 `month-reads-qualified` 回执（SHA `066dff5d70b1ae0a37d6f8f615c4242af2c0f6a6269c507f062a4a7bb739fa58`）、checkpoint、实际 state／identity／schema 和源码模块字节；只读取 2016-12 已有 terminal publication 候选，并核对同份正文与独立 anchor 的精确 checksum。未运行新的完整财务证明。

实际选中 1,006 份正文，共 3,055,335 字节。该候选集合不宣称等同于报表实际调用的 1,004 个源。加上 105 个内存边界向量，1,111 项接受／拒绝、错误语义与完整 Python 容器形状全部等价，dictionary order 也全部一致；正常正文没有 fallback。边界包括重复成员的两种顺序、bool／float／null／巨整数、空字符串、非 hex 与 Unicode digest、任意行字段／resolution 内容、父依赖顺序与重复、缺键、错 identity、异构行与非法 JSON。它们只是解码向量，不是可接受的正式财务事实。

| 独立局部轮次 | 原 `_decode`（ms） | native prototype（ms） | 每轮正文数 |
| --- | ---: | ---: | ---: |
| 1 | 31.4010 | 37.2303 | 1,006 |
| 2 | 26.5222 | 27.4098 | 1,006 |
| 3 | 22.8102 | 33.6007 | 1,006 |
| 4 | 17.0414 | 31.0519 | 1,006 |
| 5 | 21.3532 | 28.1196 | 1,006 |

所有 original 轮次先执行，随后执行 native 轮次；数据库已关闭，SQL、anchor 校验、指纹与报告写入不计入局部解码时间。当时主 48 月完整资格在并行准备，且轮次顺序可能受分配器／GC 影响，墙钟数值仅作 diagnostic。行为和结构次数证据成立，但没有收益证据，更不能推出页面净提速。tuple 转 list 回调等开销是待证解释，不作为已测根因。按本轮决定停止，不继续改变 schema 重试。

## 保留的证明与公共边界

- 日常报表继续保留正文 checksum 与 immutable anchor、完整结构、父依赖、source bindings、保存源字节及身份／采用证明；选中行仍匹配 version、line、account、借贷与 cashflow，冲正、无影响新 basis、缺少／不可用派生正文仍使用原有证明或完整回退。
- CLI／MCP 的看板命令沿相同公共读取职责；本次默认样本不证明所有筛选、分页、按需或人工办理分支的工作量。
- `integrity.py` 的完整核验经 `report_open_contribution_reader().compare_open_contributions` 独立重建正式结果、核对所有 anchors 和派生正文。结构等价原型不能替代这项证明。
- `Maintenance.rebuild_projections` 经 `repair_open_contributions` 先核对不可变源及 anchors，再重建可修复正文。关账 publication／贡献同步与冻结 flow、备份／恢复完整核验继续走原职责，没有由本轮局部结果省略检查。
- `content_history_context` 的历史 version 1 选择固定 `report_open_contribution_v1`、`report_projection_v1`、`report_flow_v1`；本原型没有覆盖或替换 fixed-v1 解释器。

这些公共边界来自代码审阅，本轮没有重跑其测试或业务入口。没有新增 raw body cache、跨请求状态、合同或生产 decoder。当前上下文不采纳两项候选；更换调用组合、库分布或底层实现后，须用新证据再判断。

## 原件与独立归档

[本次独立 manifest](owner-contribution-decoder-prototype-20261003-manifest.json) 只收下列五份合成、无凭据原件，保存原始字节、SHA-256、压缩字节与压缩 SHA-256。gzip `mtime=0`，解压逐字节一致；`.tmp` 原件保留，旧原件清单未修改。

1. `.tmp/stage9-native-contribution-shape.py`：原型及严格绑定入口。
2. `.tmp/stage9-native-contribution-shape-scope.md`：运行前 scope 原件，原样保留其“prepared, not executed”文字；实际完成状态以结果 JSON／日志和本文为准。
3. `.tmp/stage9-native-contribution-shape-main12-1c37.json`：全部 1,111 项及每轮结果，SHA `c16f6352636da4981b6c235bf9c578aeccf6d13895a17d3ea14387f8bb2bd37e`。
4. `.tmp/stage9-native-contribution-shape-main12-1c37.log`：脚本创建的内部日志。
5. `.tmp/stage9-native-contribution-shape-main12-1c37-stdout.log`：root 保存的标准输出。

文档与归档收尾只读取既有源码／原件并做压缩及字节校验，没有运行数据库、测试、浏览器或新的性能／解码测量。
