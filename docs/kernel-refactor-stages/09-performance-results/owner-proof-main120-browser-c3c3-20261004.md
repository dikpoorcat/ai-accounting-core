# c3c3主120真实浏览器结果（2026-10-04）

本次结果为**FAIL：150个成功热刷新样本中19个达到或超过500ms，全部在资产页**。固定源码c3c3，source SHA `c3c3dfe5579521864d7efacba29dcc157357610921d7b825396fa307be880c5c`，manifest SHA `00f133223a9269ed8f232bb66950747250dbfdf48c63c29455aff0b48d7bf66a`，765文件；backend build `local-kernel-2:74442f6594af5ceafd1bcac2c691d4d37c3a89adf8a465e25fa633c03664bf92`。只证明当前主120本轮样本，不覆盖其他主规模、独立分布、压力、按需详情或公司切换，阶段9未完成。

## 新鲜资格与实际预览

计时前由当前固定源完成新鲜完整资格，不继承旧qualified。公司draft／0完整核验status verified，sources、historical_adoption、projections、read_indexes四项均verified，limitations=[]；覆盖308,217 facts、130,619 calculations、123,117 vouchers、119 closes及841 evidence。完整核验3,051,311.6525ms，实际开放月预览2,472,093.2734ms，资格总计5,526,926.964ms，均在纯计时窗口之外。实际开放月2025-12预览digest `4e6803189feed4bfa3a2cb442ba530040307e512d115edfa8b740e3391e4ab8f`，与构造时digest不同，不能用旧完成标记代替本次实际预览。qualified原件SHA `5ddc435b75c81e69b57af1e51ece43b56c8c89d3f33e36470f1cf3e24c4fcb7f`，runner／ready／start／输出分别按保存SHA交叉核对一致。

样本为120个月、每月1,000笔业务／共120,000笔、50名员工，默认每集合20条。控制器ready后释放纯计时，窗口95.219s，仅有界原生等待，未在窗口内做CIM／SQL／HTTP／源码检查。runner退出1因超目标，status measurement_failed；控制器status controlled_window_finished，不能误作程序崩溃或控制器成功即性能通过。

## 热刷新实绩与错误数

五页每页3次暖机、30次正式热刷新；150个正式样本均有限数值，资源记录各30份、关联诊断0，harness后验响应／页面错误核验成功，0浏览器断言错误。所有正式样本和资源时间戳原样保留，没有扣两RAF、删除慢样本或用native替代。单位ms，median／p95／max：

| 页 | median | p95 | max | >=500ms |
| --- | ---: | ---: | ---: | ---: |
| 经营简报 | 372.3 | 433.1 | 434.5 | 0／30 |
| 资金 | 311.6 | 332.4 | 332.7 | 0／30 |
| 员工 | 310.5 | 312.8 | 333.8 | 0／30 |
| 资产 | 501.0 | 581.1 | 605.2 | 19／30 |
| 报表 | 431.0 | 437.6 | 437.8 | 0／30 |

本次实际使用新热refresh终点：capture click先计时，计时外由真实response和verifyMainVisible建立完整默认可见业务DOM投影，忙碌到就绪见证后完整投影一致并连续两RAF结束。两RAF仍计入严格500ms，默认金额、20条内容及附属业务不减。与旧1836等来源的不同终点／运行窗口不能作同口径因果对比，不从旧36超线降为19推断稳定收益；旧失败全部保留。

暖机配置每页3次保存，但harness不输出各次暖机耗时数组，不能补造“全部暖机原始耗时”。启动3,241.0531ms、首次加载1,008.7ms、浏览器启动至首次渲染2,614.5562ms单列；冷页brief500.8／funds884.2／employees624.0／assets2,821.1／reports748.0ms全部保存。冷页沿原navigation终点，不因此宣称新完整内容终点已覆盖冷态。公司切换为unavailable／single_eligible_company，未执行，不作通过。

## 守卫与认证例外

runner保存的业务identity／state／schema history、company DB及WAL、输入与构造文件守卫前后相等；源码完整清单未变。company、catalog非认证身份／registry／history及owner数量相等，未新建负责人或改业务。正常登录使owner认证时间／session／public profile及catalog WAL变化；catalog本体SHA相同、WAL SHA不同，明确保留该例外，不误写全物理文件或所有认证字段完全不变。SHM字节未承诺恒定。合成凭据已撤销，不操作真实资料或5173。

## 超线后的必要读取排查

保存资源逐条核对，五页各30次均恰好context1次＋主API1次、diagnostics为空；context与资产并发且早已完成。资产API duration的median／p95／max为471.3／546.4／572.9ms，TTFB为469.65／544.6／571.1ms，responseEnd到严格可见终点的尾部32.55／39.0／42.9ms，与其它页尾部相近。这些是浏览器资源分段，不能细分服务、认证、SQL或JSON的CPU成本。静态审查确认公共HTTP一次TypeAdapter validate→dump_json，前端一次独立Ajv及必要请求关联校验，harness没有固定wait；没有确认可移除的HTTP重复缺陷，不降低必要校验或500ms口径。

独立c3c3健康样本SQL诊断只假设排除120个可变member locator：baseline240行／239,500采样VM→120行／127,100VM，其余nonmember multiset相等、所有保存守卫前后相同。它不证明其他版本、普通冻结采用或current pointer的完整负面边界；缺少不可变membership／absence proof，不能将该排除条件照抄进生产。首次helper选用了不存在的m.kind而失败，原私有脚本及工具记录保留，未改变业务；不能当作生产错误。

frozen activation current guard组已实施于工作区的`business_queries.py`和`dashboard.py`，未纳入上述c3c3固定来源。根因是同一owned snapshot已经完成冻结activation的精确身份、源与目录证明后，current member guard仍再读取owner＋members全部结果体。caller先构造身份证明，再读取heads；原generic dual-ID SQL及当前来源、owner／publication／voucher检查保留。只有全组精确匹配owner、入账月和每个member的subject／calc／fact／kind／month／digest时，才省重复body读取；其他范围回原路径，不发布body成功缓存。证明复用不等于缩减普通冻结采用、其它版本或缺席验证职责。

后续已固定807来源，集中专项29通过、五页插桩工作量完成，见[同一小组记录](frozen-member-current-guard-20261004.md)。23模块回归314通过、成对原生窗口各150成功已完成，资产两来源各有慢样本，不能证明稳定改善；807尚无新资格／浏览器，约90分钟整库资格暂缓至剩余必要读取成本收敛。插桩与回归并行，不证明时延收益。上述c3c3主120资产19／30超线结论不变，不能宣称新组已解决超线；先前失败与46项状态均保留。

SQL authorizer纯内存功能诊断显示，SQLite3.53.1的None→None也能使statement cache失效，但实际QueryReads snapshot在BEGIN后确实安装keep_snapshot，禁止事务和SAVEPOINT语句，防止COMMIT后再BEGIN绕过owned snapshot token及事务状态，finally必须真实清理。必要安装／清理本身已使prepared SQL失效，仅删ResidentReadPool重复None清理没有已证明的跨请求缓存或500ms收益。本轮不实施tracking、不改statement cache大小或snapshot authorizer。原`.tmp/stage9-authorizer-expire-memory-diagnostic-20261004.py`／`.json`只保存既有工具输出，未重跑，不另造性能或归档结论。

该健康假设单独保存为[2件诊断清单](owner-member-locator-hypothesis-c3c3-20261004-evidence/manifest.json)，SHA `0293152391205ee80c406a8a1aa45b2904572654152dfa02f837139094231a03`。run2 SQL计划／工作量按明确标注的分析脱敏保存，私有完整guard仅引用SHA；汇总中的首次helper与计划是已有工具记录，非新的执行证据。gzip mtime=0、SHA与解压回环核对，未复制数据库、脚本或凭据；下述8件浏览器清单保持原样。

## 独立归档与剩余范围

[8件证据清单](owner-proof-main120-browser-c3c3-20261004-evidence/manifest.json) SHA `913217d2e6667bf7b53018e41850ca9bc65c4f8a22827dddbf63dca1ec99c440`，包括原保存结果、qualified、runner、control、ready、start、日志及汇总。所有热刷新样本／资源记录、启动、暖机配置与冷页结果保留。私有路径／上下文／业务守卫和控制器细节明确标为分析脱敏或SHA引用；私有原件SHA／长度与公开representation分别绑定，不冒充raw。凭据、数据库、ZIP、脚本或完整inventory不复制。gzip mtime=0、SHA与解压回环核对一致；原22件、185件及全部历史档案不改。

当前开发包与[限定MCP](development-package-mcp-c3c3-20261004.md)保持各自完成范围；本轮新鲜资格与实际预览完成，不扩为全部AI GUI或其他样本通过。资产主120仍超线，其他当前规模／独立／压力、按需详情／文件和GUI公司切换仍未验证。正式合同、正式交付和运行入口切换延期，不新增全量门禁或无界优化议程。
