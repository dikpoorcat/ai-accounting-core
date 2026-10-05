# 9ac保留大BLOB资料的正确性与资源桥接（2026-10-04）

本组在当前9ac运行来源上核对已保留的240／1536MiB合成资料及备份恢复，完成限定正确性与单worker资源观察。状态为`passed_with_resource_sampling_limits`，耗时口径为`diagnostic_concurrent_qualification`：运行与主120资格并行，不是纯计时、前台影响、整页500ms或绝对RSS峰值验收。两档均没有关账记录，不构成完整冻结大BLOB样本。旧24b9结果保留其原来源；本轮没有重建大资料或继承旧性能通过结论。

固定运行源码 `9ac1ddcc2ee3b4d5d4758aff091d5b33c12e1f54a482c4f6ab83ecd6853bcd2d`，770文件，源码manifest SHA `5398ce88ec9b8bc1878cc425046f1d9764d42383720e46706a8aca4dfeea2a1b`。本记录仅整理完成原件，不运行数据库、构建、测试、浏览器或性能流程；公司库／目录库仍为draft／0，无正式交付、真实资料、OS凭据或现有5173服务操作。

## 保留输入与四阶段正确性

240档成功来源为attempt2／240，1536档为attempt3／1536。使用24b9时期已经保全的两种混合可压缩性资料，主要BLOB分别12／77件、251658240／1610612736字节；全部evidence分别20／85件，其余为小资料。不把ZIP压缩字节当BLOB原文大小，也不另造空库或重新登记业务。

两档都由当前registered verifier完成sourceverify、创建便携备份、恢复便携备份及restoredverify；每阶段sources／historical_adoption／projections／read_indexes四项均verified，limitations为空。每阶段业务计数均为134 facts、48 calculations、43 vouchers、0 closes；evidence分别20／85。historical_adoption覆盖标志不代表该样本具有非空冻结月。

保存的before／after核对源DB、WAL、journal、catalog、service元数据和原ZIP的SHA／大小／mtime一致，身份、state、schema_history、逐资料digest／size及SQLite实际schema SQL不变；恢复后的logical guard与源完全相同。fixed源码清单与manifest不变。新便携ZIP是本轮生成并验证的独立产物，不宣称新旧ZIP物理字节相同；原24b9 ZIP保持原字节。

| 项目 | 240档 | 1536档 |
| --- | --- | --- |
| 成功receipt SHA | `52862ad445a5daf8c2e06d819fa985b9fcd6de6cf3e174cbdf6ab333fd9249b4` | `1f8f653d0f5fb1aa48e8ef5b1235afcecec03856701305c66f7680828a1fe54c` |
| 新便携ZIP SHA | `1bdd62c319b0326bcffc88c9ce1e86bfb2c0061b07d4d7563c6b36f1d8a0b1c3` | `908d935c183ce39f482534df1f67f9fc247e4fd70c04ea1b60df5f403b6100e7` |
| 新ZIP字节 | 126954174 | 801691932 |
| 恢复DB SHA | `5058e177f22208eca135a167cf1434f2a42aab8656f47124356a240f2e1d3130` | `438b35f7f25fcfda69f2aed7a6acee28f5a202ff198d136003f6bdba3e2ff56f` |
| 恢复DB字节 | 256184320 | 1616527360 |

本次归档作者通过原件和索引核对上述保存结果，没有再次读取DB／ZIP或重新核验其内容；数据库及ZIP均保留私有，不复制到公开证据目录。

## 单真实worker的RSS与诊断耗时

采样者已确认实际执行worker，而不是父launcher；观察该worker完整进程的working-set RSS，名义周期10ms。sampler／supervisor、browser、并行qualification进程及前台不在该RSS范围，不是完整service tree或系统内存峰值。下面每格为“诊断wall秒／观察RSS峰MiB／原phase样本数”，不是纯时延基准。

| 阶段 | 240档 | 1536档 |
| --- | --- | --- |
| registered source verify | 1.1122／152.906／32 | 6.1399／155.543／560 |
| backup create portable | 10.0078／153.578／923 | 52.1595／155.328／4773 |
| backup restore portable | 2.6674／155.832／246 | 9.3643／155.176／851 |
| restored registered verify | 0.6724／155.195／61 | 3.7712／154.949／342 |

完整原RSS分别1900／8208行。实际相邻采样间隔240档mean11.2396ms、max766ms，1536档mean11.0082ms、max500ms，包含launcher attach及调度gap；有0ms间隔，不能称均匀10ms，也不能排除未采到更高峰值。

1536档追加phase事件11条全部完整。原RSS存在两处sample与phase事件时刻恰好相等的边界歧义；复核保守排除3个恰在边界的sample后，四阶段观察峰值均不变，非边界错分为0。对应sourceverify样本559、restore849，其余4773／342；原始8208行全部保留，不删除歧义样本或改写原phase归属。边界安全复核属于补充分析，不能冒充原raw没有歧义。

## 首次失败与保全范围

首次supervisor把launcher当执行owner，owner匹配断言失败；其supervisor-receipt保留failed，worker原receipt没有阶段结果，未改成passed。attempt2的1536档在phase marker共享删除／替换时发生Windows PermissionError；failure、phase.new、原RSS、日志和仍为running的receipt全部保留。后续attempt3用追加事件核对成功，不覆盖首次失败，也不把失败过程中已完成的阶段拼成成功整案。

清理证据记录本任务worker退出、无剩余owned进程，输入源及OS凭据／服务未改变；源合成资料与原ZIP不作为失败清理目标。清理、成功receipt及原资料保护只说明本轮实际范围，不扩大为真实资料业务操作或迁移验收。

[独立归档manifest](current-evidence-risk-bridge-9ac1-20261004-evidence/manifest.json) SHA `85dca893e21c48b412e5bfe476308de8249806c5e4670dbb969ea712ce474e52`。原索引61件全部核对SHA／长度一致；另加索引本身和9ac源码manifest，共63件输入，53件发布脱敏gzip分析、10件私有helper仅保存相对路径／SHA／长度，不复制内容。summary原SHA `9e8a1c9c4d66fd0c3bc1a5e574c1fb370f2631442c0eaea2a7faca02dc0711f5`，原index SHA `58eae2e7582202f550b0ecc134ca4cf85600603abe2b4dcbff8cf820c072b3f9`。

公开分析移除PID及私有根路径，保持全部RSS行、phase事件、失败状态、业务计数与采样gap；每项记录原件SHA、脱敏SHA、gzip SHA及解压一致性，gzip mtime为0，不冒充未脱敏raw。没有复制DB、ZIP、凭据或密码helper。旧24b9及已封42／59／185／179件归档不变。当前主120资格和GUI正确性仍进行中、纯计时尚未开始，本记录不声明新浏览器通过或第9阶段完成。
