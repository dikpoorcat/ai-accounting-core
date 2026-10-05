# a5d4 两公司独立实际 MCP 验收（2026-10-05）

本组已完成两家合成公司的真实会计操作、正常接续、备份及全新目录恢复。执行智能体为 GPT-6.1 Sol，高推理；根任务扮演合成测试老板，逐公司提供和批准事实。本组绑定固定源码 `a5d4e1ac7d41814bb7a74d30410e0dbe45e6a691c85bd69f71deff10f57bd1d1`，不继承旧 MCP 计数，也不把开发包159次CLI自检、279项回归或种子准备计为独立操作。它不是正式发布、真人密码窗口、强制崩溃恢复或500ms验收。

包manifest SHA为`bd802ceb8d370151ad3a36366f734eced7700dbd80e547cc06451666cd58cc49`，实际build为`local-kernel-2:5c689d17374baad801e4d73778715f9f5c9a71642a990d682ec7dba99575d3e2`。原harness SHA为`b6ba71077eeed6a1768c3b5466bf46f6000bbd4651d05927f9ad7124b8813e85`。所有业务操作走该包`runtime/python.exe -I -B -X utf8`的真实stdio MCP→生产HTTP，参数独立JSON，协议走`finance_local_schema`。没有调用已安装的真实连接器，没有直接Engine／SQL业务写入，没有修改产品、包、原harness或固定源码，也未访问真实资料、原主样本或5173。

## 实际操作与核对

智能体先读运行协议、两公司的context／workflow、对象、事实及已有六份资料，再向测试老板分别问缺失的付款日、员工档案、政策、工资批准及无业务范围。原话分别保全为证据，答案只绑定各自2026-09，不跨公司或月份沿用。已有现金出资甲125000分、乙87500分被核对和保留，不称为本轮重新登记。

两家公司分别登记供应商、员工、工资档案、社保／公积金和个税政策、首月依据及明确零累计期初。不参保的零政策来自老板明确字段，不是默认推断。方案尚未批准时只准备资料；老板分别批准完整输入后，仍实际尝试没有类型化plan的工资预览，两次均被`needs_information`阻断。随后保存各公司获准plan，绑定自身档案及两政策revision1，正式计算并发布月工资60000分、净额60000分、税及双方缴费0。没有登记工资付款，也没有把税0当作已经申报。

文具费用及现金支付均通过类型化登记、统一预览和正式发布：甲9月8日、乙9月12日各实际支付3000分。甲开放月费用按新确认由3000改为3500分，原凭证编号及实际3000分付款保留，供应商未付500分；旧资料链接真实阻断后经公开处置更新。补充确认不是新增发票、费用或支付。

两公司分别读取完整readiness、同版月度摘要及digest，测试老板核对后只批准对应公司、期间和这一版digest；再请求synthetic native批准并执行正式`close_period`。native只证明合成密码传输，不证明真人密码窗口。冻结摘要如下，金额单位为分：

| 公司／2026-09 | 现金 | 费用 | 负债 | 权益 | 实际未完成 |
| --- | ---: | ---: | ---: | ---: | --- |
| 甲 | 122000 | 63500 | 60500 | 61500 | 工资60000未付，文具500未付 |
| 乙 | 84500 | 63000 | 60000 | 24500 | 工资60000未付，尚未申报 |

甲冻结digest为`2929cda0801fc95b9c5a42d9fa233af57dc8c4e2e27280c969c930d1c246d50c`，乙为`7ceeb9337dc6f9a5da2eb8b47295d761fcbbdb691d1c9c4aa6b621b67a44d21a`。

甲另外明确建立9月个税义务及10月2日实际申报事实，记录月为10月。保存时进度仍pending；正式发布后为completed／not_reviewed，再独立发布精确采用依据核对后为reviewed。采用正式工资计算和已确认提交资料，实际办理回执单独保全；乙没有沿用甲的办理事实。申报不等于工资已支付。

原host由根任务正常stop、exit0，再用同一包resume；智能体真实重读schema、context、workflow、原request_result及jobs，原身份、闭期和提交回执保持，未重新建立负责人。此后甲同一笔9月费用在两次分别收到新确认后，由3500→4000→4500分追加事实，明确posting_period=2026-10并正式发布。10月费用先差额500，最终差额1000，没有重复加成1500；现金仍122000，流入／流出／净变动均0，供应商未付1500。9月整份MCP冻结报告前后逐字段相等，原付款fact_id／revision／data／evidence相等；没有宣称直接读取数据库SQL原文字节。闭期资料精确链接及分类公开承接到10月，10月仍缺六类全月覆盖和新工资，没有借9月回答补齐或关账。

## 响应丢失、失败与恢复

甲一次正式发布前在独占测试目录设置丢响应标记，实际得到`service_unavailable`未知送达。智能体先查询真实request_result，确认committed，再用原载荷、原request_id重放；digest及凭证2／3相同，没有另开请求假定失败。首次查询误写`request_id`而非`submitted_request_id`被`invalid_command`拒绝，按schema纠正后继续，原失败保留。

另外两项操作错误保留：脚本误以为发布mode为`replace_open`而断言失败，实际成功预览为`open_replace`；同一份混合老板回答误登记为两个source身份，`material_duplicate_source`阻断。后者通过公开preview_delete／delete撤去重复source及处置，原证据、清单、审计保留，一个来源分别处理明确费用纠错和季度所得税0段。这些是智能体操作错误，不写成产品缺陷。乙首次正式工资预览缺`payroll_opening_state`，依据老板已明确零累计事实登记后通过；没有合成更早的失败。

两家公司真实备份任务均经历pending→run_jobs→succeeded，成功结果各指向经过完整核验的独立格式2 ZIP。经根任务审查，恢复relay在原先不存在的新root启动，空companies及177个包文件／实际模块来源、manifest和build守卫通过；恢复操作helper只执行一次，仍走同包stdio MCP→HTTP的生产`restore_company`及`operations`。两恢复任务均succeeded、attempts=1，新路径保留各自三ID，不覆盖原公司。恢复后两份完整冻结报告及完整verify响应分别与原响应相等，甲10月差额1000、现金122000、收付0也实际核对。源两ZIP执行前后SHA相同。

| 原公司及恢复公司 | 完整核验 | facts／calculations／vouchers／closes／evidence |
| --- | --- | --- |
| 甲 | sources、historical_adoption、projections、read_indexes全部verified；limitations=[]；read_repair_revision=0 | 45／12／12／1／10 |
| 乙 | 同上 | 25／4／4／1／7 |

原目录263次实际MCP工具调用，恢复目录17次，共280次，包含真实失败及读取，不能称为280次成功业务写入。两台最终host均由根任务正常stop，工具退出码0，进程不存在已核对；没有kill或强制崩溃验证。本次归档只读保存的回执，不重跑业务或核验。

## 原件与公开归档

[公开证据清单](independent-mcp-a5d4-20261005-evidence/manifest.json) SHA为`d33d0008cec000dea6219106800afa1c4370a502af88721711b29c7426113803`。六件gzip分别为明确标记的汇总分析、四件原样完整核验响应、1162件私有原件的路径／SHA／长度索引；全部解压内容一致，mtime=0，每件内容及gzip SHA可追溯。私有ZIP逐件SHA／长度也重新核对。公开不复制数据库、便携备份内容、凭据、服务能力、原话材料、helper或完整业务载荷，派生分析不冒充raw。

| 私有原件（忽略.tmp内保留，未修改） | SHA256 |
| --- | --- |
| operator-acceptance-report.txt | 4151eb6fc422e617677d94bac4a306c13ed4772e77bf72c816b88f10958b5903 |
| operator-acceptance-summary.json | 30244c41c6aba65972af50df0a5ffaf7c12606b1fd7e1933ec7c8d16e5432010 |
| operator-restore-receipts.json | 0591338a732464a727ad212ea5b3dabb6e7e2141ecb06aa0569bd20ee605031c |
| operator-restore-run-20261005.log | d1e03ffb1b29cab821a58686bef989c1aadc13b37f0ec6c4c9206d9923806c7c |
| 两root合并私有records ZIP（3445622字节／1162件） | d8d0826ea0d8d9996bcbd9ca543156131611a2b9276e92b6bc8637f13cb35c94 |
| 甲公司便携ZIP（只读来源） | d8c1b0a96a77185005a27629efafe6dfd1e3be1303913f04d109dbc7ab06893a |
| 乙公司便携ZIP（只读来源） | 996927ca9186eac0a2d424f0dadd964d3a4d0e87f1c7b5845c48cd05d667b61b |

未验证真人密码窗口、强制crash／kill、真实企业／5173／已安装连接器、500ms及规模性能、两公司实际工资付款、乙外部申报、个税导入映射及工资文件、甲10月完整覆盖／工资／关账。没有观察到本次实际范围内的产品缺陷，不扩大为所有路径正确；保持draft／0，正式冻结、发布和运行入口切换延期。
