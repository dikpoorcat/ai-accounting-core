# owner 分类证明与必要读取工作组（2026-10-04）

本组固定来源为 `.tmp/stage9-build-source-owner-proof-read-work-20261004`，source SHA `c3c3dfe5579521864d7efacba29dcc157357610921d7b825396fa307be880c5c`，manifest SHA `00f133223a9269ed8f232bb66950747250dbfdf48c63c29455aff0b48d7bf66a`，765 文件。相对第二／第三组2e474，生产只改 `business_queries.py`、`close_storage.py`、`report_flow.py`；harness、3个已有测试文件及3个新增测试文件随组固定，755文件字节相同，Vue产品页面、合同、static与fixed-v1未改。本组不是阶段最终来源或正式交付，旧来源的资格、通过数与失败不继承。

## 分类证明与必要成本

前组合成小库复现的private helper缺口已修：owner联合`c.kind`／`s.kind`从activation改为consumption时，不能在membership认证前用可变kind筛掉冻结owner。选定owner独立核对typed fact与member证明，完整owner声明及实际publication／voucher冻结范围保留。官方verify_fact_versions在任何owner-kind过滤前认证全部精确owner source_fact_ID原始typed内容；unknown_fact（已采用owner typed表缺行）映射既有content_integrity_failed／owner_typed_fact_missing，其余官方content_integrity_failed／source／invalid_stored_content原样保留，不扩大错误码。原公开assets与完整core已有其它路径fail closed，不能把局部helper缺口写成已发生整页漏报。

必要证明增加真实主120资产工作量：SQL 358→376（+18）、返回12,185→14,112行（+1,927）、12,091,125→12,248,702B（+157,577）、采样VM 1,201,600→1,228,200（+26,600）。结果读取485行／527,255B、结果解码485、JSON 2,596次／9,090,289B不变。不得为了性能删除此证明。

owner消费者75项结果为73通过／2个增长对照失败／205.20s，修复测试对照后两个增长节点＋kind8共10通过／59.66s；两次准备失败分别保留：正常member误交owner header（34.90s），request ID误当固定月subject ID（33.95s），不伪作生产失败或当前完整资格。修正增长对照typed fact解码保持27／43相等，结果行与解码51→39、195→147下降，但总传输字节实际增加，未要求必要事实证明被缩掉。通过数与既往focused20及历史消费者范围重叠，不相加。focused来自运行时共享源码，agent未自行核对c3c3封存hash，不能后移为封存源完整资格。

## 同月报表与缓存命中

report classification继续按完整4,932个必要header严格同优先级decision，但同月分支不再构造宽head／voucher参数。真实主库12个flow各411头共4,932头全部同月，无缺失或跨月，不能据此取消通用fallback。合成400头：旧400行／36,000B／15,221VM→同月1行／24B／21,639VM（暖态21,636）；voucher数0／1,001／11,001时SQL参数34,009B、VM21,636均不增长；12,000无关对象／120月也维持1行／24B／21,636VM。完整4,932头VM266,388，高于前流式251,568，原`4932*60=295920`门槛保留。减少参数／Python返回分配与增加SQLite工作同时成立，不承诺毫秒收益。

首次focused47通过／1失败／8.94s为VM精确相等断言21,639对21,636的三步编译波动；改为增长边界而保留旧60N门槛后48通过／8.42s。两轮log／XML分别保存，前轮不改写成通过。真实profile报表SQL255、行18,592不变，传输+96B、采样VM+14,800；必要结果1,007行／1,058,063B与JSON513次／4,342,872B不变。

current物理桶group在已完成的完整缓存命中后直接返回，避免重新遍历已认证的子根descriptor；首次扫描及目录／块SHA、顺序、长度、多重集与失败无前缀发布保留。32／128／256 descriptor实际缓存命中访问均0，首次扫描仍32／128／256。两组公共focused116通过／82.12s（cached、descriptor、physical、reference physical、accounting filter、candidate integrity、direct contract）和47通过／120.42s（补connection／registry-v1及asset owner frozen／activation／consumption），只存在agent工具记录，无log／XML，且有交集，不相加、不后移为固定源完整资格。Ruff通过；首次I001／E501仅测试格式修复。

## 未保留的 adopted-head 方案

独立只读诊断仍绑定2e474：limits-before-reference候选240行与baseline multiset完全相同，SQL SHA各自固定，但VM239,500→2,227,000（9.2985倍）。守卫前后相同。未实施该候选；anti lookup原计划已有BLOOM FILTER及两键automatic covering index，无逐publication重扫证据。此负面结果不归c3c3新增优化，直接冻结leaf发现也未完成证明。

## 五页profile与顺序native窗口

profile只用于工作量；instrumented ms含观察器／EXPLAIN／cProfile，不用于因果计时。brief、funds、employees全部work counter与2e474一致；资产与报表差值如上。profile／native三个strict业务SHA逐页一致，每次before／after守卫、source inventory及read pool关闭均通过。两来源company／catalog／非manifest files相等；source manifest按各自位置与SHA独立绑定，不能要求跨源相等。

每来源每入口3 warmups及全部30成功样本均保存，单位ms，median／p95／max：

| 入口 | baseline 2e474 | current c3c3 |
| --- | --- | --- |
| assets | 453.9221／468.7682／473.2378 | 460.0867／470.6652／470.9507 |
| brief | 337.6487／356.6282／364.6654 | 338.80875／362.3063／364.3700 |
| reports | 396.8158／451.6375／469.6474 | 392.38065／465.5927／467.8809 |
| funds | 234.34285／288.7750／300.0236 | 234.77405／292.0974／298.2874 |
| employees | 256.9587／299.4715／300.1987 | 264.8576／289.8095／308.4482 |

本窗口两来源native各150样本全低于500ms，不能证明整页达标、稳定因果或关闭旧主120FAIL36／150。没有HTTP、render、prepared owner TODO、当前完整资格或actual preview。未改三页的波动不归本组优化；旧2e474超线及1836旧慢样本照常保留。

## 浏览器终点与证据边界

新harness热refresh capture click第一步记录performance.now，再清ResourceTiming；在真实response与verifyMainVisible核对成功后、计时外为同公司／期间／固定snapshot建立完整默认可见业务DOM投影（金额、默认20行、待办／owner prompt、资金账户／投资、劳务、资产项目、report摘要及控件状态）。正式samples缺投影明确失败；计时内无第二次response JSON、Node RPC或产品缓存。busy→ready见证、完整投影相等及连续两RAF都计入严格500ms；只有report显示时钟“更新于”被忽略，业务季度与金额保留。后验response／错误检查仍在。Node专项12通过／105.4607ms与syntax通过仅是本地逻辑证据；后续同c3c3主120实际执行新热refresh终点并FAIL19／150，见[真实浏览器记录](owner-proof-main120-browser-c3c3-20261004.md)。navigation／公司切换保持原终点，覆盖不足仍未验证。无需重打Vue产品dist，旧慢样本不改。

[22件证据清单](owner-proof-read-work-20261004-evidence/manifest.json) SHA `7066b1cb231a223002aa3bb0d11401da103df8a87c23256a315956325d22f5b4`。source-diff、profile／native全部warmup与sample、日志、owner消费者及两次对照失败／最终复验、report两轮log／XML、Node12及2e474负面诊断分别保存。包含私有根路径或guard的JSON／日志明确标redacted analysis；原件path／SHA／长度与公开representation分别绑定，guard／read_context只引用SHA，不冒充raw。无根路径的原log／XML及source-diff按原字节保存。gzip mtime=0，SHA与解压回环相等；不复制DB、ZIP、脚本、凭据或完整inventory。无专项文件原件的工具结果明确留作工具记录。历史归档未覆盖。

阶段9仍实施中。当前c3c3主120已完成新鲜完整资格与实际开放月预览：四项覆盖verified、limitations为空，不继承旧资格。真实热刷新150个成功样本、0错误，资产19／30达到或超过500ms，五页总体FAIL；简报、资金、员工、报表在本轮各30次低于500ms。所有慢样本保留，新终点不能与旧1836作同口径因果比较。其他当前主规模、独立／压力、按需详情和GUI公司切换仍未验证。见[主120记录](owner-proof-main120-browser-c3c3-20261004.md)。当前开发包159次实际CLI与独立限定MCP成功48次已完成，见[独立证据组](development-package-mcp-c3c3-20261004.md)；非空资产分页／owner证明、完整AI GUI和浏览器性能未覆盖。各范围单独绑定，不提升为全部通过；正式合同、发布和运行切换延期，资料根、身份与5173不改。
