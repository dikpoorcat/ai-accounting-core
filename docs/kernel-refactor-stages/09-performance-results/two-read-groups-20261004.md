# 第9阶段：保留两组读取修改（2026-10-04）

第9阶段仍在实施，当前主12／48五页热态各150／150通过500ms，主120FAIL36／150；其余规定规模尚未验收。当前固定源为 `.tmp/stage9-build-source-two-read-groups-20261004`，758文件，source SHA `1836585b091d79999bce32980a42ed919246449d7f493593633cf4cbd0768459`，manifest SHA `09945da84769efe4fc75acfecbb7ff252ddff610ec38a3fa8e473d86bca508e6`。本轮只保留简报重复读取／gross复用和结算group密集表示；开放月物理头carrier已撤回。目录及公司库保持draft／0，不操作真实资料、5173服务或负责人身份，不冻结正式合同、不切换运行入口。

## 问题与修复范围

简报在同一snapshot重复读取月度gross，资金事件在已有完整认证结果后又执行账户locator。现在复用实际debit／credit原值及已认证voucher IDs；gross逐项比较、完整来源和金额核验继续执行。资金复用只用于相同活动snapshot、current、未限制subjects及开放当前月；未满足条件仍使用原filtered selector。没有跨请求成功缓存，也没有用net相等替代gross相等。

结算读取把已认证root的每个group重新扩成15-key dict，随后只按固定字段汇总。内部改用15元素list，保留五项key、20项root行的严格长度检查、独立slice、逐项整数溢出检查、unknown及零group语义。实际source、tail、subject、state、leaf、page证明及完整重建输入不减；磁盘canonical、root digest、DDL、合同及固定v1不变。4096个相同group的共同outer dict和value容器从2,048,088降至868,440字节；相同keys和integer引用不重复计入。原35项最小定向测试属于dense早期收敛版本，不能替代当前合并源回归；不据单次profile推算节省104ms。

## 实际工作量

撤回后的固定1836源只做一次六入口插桩对照，参考为固定24b9旧work receipt。context与五业务入口的strict business response SHA均相同，source inventory及文件／身份／state／history／registration／repair守卫均为true。仅归一化明确的generated_at／checked_at时钟和顶层source-bound read_version；完整响应字节不是全部相同，其余版本、ID、cursor、金额及状态保留。

| 入口 | SQL差值 | VM差值 | 返回行差值 | 返回值字节差值 |
| --- | ---: | ---: | ---: | ---: |
| brief | -2 | -22,800 | -525 | -33,700 |
| context、funds_first、employees、assets、quarterly_report | 0 | 0 | 0 | 0 |

必要结果正文读取和decode未减。容器表示下降与SQL工作下降分别记录，不能相加推算整页时间。`funds_first`是native参数路径，不证明浏览器实际selected-account刷新；本次没有HTTP认证、浏览器渲染、关账preview或新语义资格，instrumented_ms和分配peak不作为纯计时或RSS。

## 已撤回的开放月物理头候选

三组候选固定c4fd源（759文件，SHA `c4fd1cb6a845626204805901f5fe59c4f5ef5db654b16f9ba1155b799788a93a`）的六入口business SHA与守卫也相同，但没有任何实际carrier消费。brief／report各铸造1006行后整月fallback一次；其他三业务页只铸造不用。每业务页多56,304返回字节和约25,100～25,200 VM；即使简报已有另两组减工作，仍净增2,300 VM／22,604字节。report的SQL、行、结果正文与decode没有减少，原型内部forced selector小测试不能推翻真实整页负面结果。

一次正常只读连接的小聚合确认：实际2019-12开放月有initial984、open_replace21、review_no_impact1；唯一review的baseline_calculation_id非空，违反原明确整月eligibility gate。未把未观察的signature缺失或动态首个失败行当根因，也没有删除baseline保护。三个专属生产文件从24b9逐字节恢复；候选测试先保全原SHA再删除，既有filtered测试的过时计数修订保留，其他两组不变。c4fd、旧XML及失败原件保留为撤回前历史。

## 证据与待验

[六件原字节归档清单](two-read-groups-20261004-raw/manifest.json)保存两次work JSON、撤回小诊断及final、独立只读review与dense审计。每件先检查文本，未发现密码、私有批准／capability／token值、SID／SDDL或运行PID；仅保存安全诊断文本，未打包被引用的数据库、ZIP、helpers或资料原件。gzip mtime=0，原path／SHA／长度与压缩SHA逐项绑定，解压回环相同；没有redacted analysis冒充raw。清单SHA `7259f22102a671a58087233e9e92e4fc6937bfa719ad46c9d4dbbd45427ba68d`。

当前受影响28整文件＋9节点统一回归已结束：543 PASS、4个record_property warnings，pytest904.30秒、wrapper906.632秒，exit0；原receipt／XML／log保留，运行期关联文件守卫不变。1836源的新45个fresh后端HTTP样本及生成前端消费者已通过，receipt `.tmp/stage9-two-group-live-consumer-validation-20261004.json` SHA `168812b75afdfd7f2b142309d0da95aec489b216b6ca0e7c8f070ad383a3fa91`，样本SHA `672c5f4f19c9338b3717e9b289973dfc2d00623c7d719c3ad2dee79c9145b9f7`；没有覆盖旧fixtures。DB export及response schema export的`--check`均exit0，分别输出Draft contracts verified／synchronized；证据为root工具stdout，没有额外log。新旧封存源的frontend、static、schema_contracts共168文件全部字节相同，无新增scope文件；旧相同前端产物保留，不重复184／type／build大套，也不称新源重跑通过。当前主12新资格及浏览器结果见下文；其余规模仍未验。24b9的345 PASS、前端184、两组45、type／build、包自检、两档证据、MCP与主12／48资格仅属[旧源组记录](open-scope-reverse-head-group-20261004.md)，不继承到1836。旧浏览器主12 FAIL10／150、主48 FAIL28／150，每规模150成功／0错误，仍为历史未达标证据；其余五规模及按需详情仍未验。回归结束后，仅将新brief测试3行E501换行；前后AST SHA同为 `c8c4668d0ee182dc7357bb7d318d4bcaf9b6f5d963821d6915ed03886ee940ba`，test SHA由 `a597d7acbdfe665313c4b6a1badb3cb86b7e89beed9564f907457f1b5ee9424b`变为 `3d9d3b7fcfbb5feeaee9ed4ce25d05b5918ece956a98a7550e6e6b5220062f55`。543结果绑定封存源原测试字节；格式修改不宣称原全清单仍字节相同，没有重复完整回归。生产／前端／schema仍与1836一致，当前8文件Ruff通过，证据为root工具stdout。

## 主12实际浏览器与同空闲条件旧源对照

当前1836在同一隔离合成主12库上重新完整资格核验并实际resident preview后才计时，五页各预热后30样本，共150成功／0错误，全部<500ms，status=passed；source及业务身份／state／history守卫为true，合成凭据已撤销。随后固定24b9在同一库、同类受控空闲条件运行一次旧源对照：明确复用该精确旧源资格，实际runtime preview及完整source inventory重新核对，**没有新完整资格Q**；150成功／0错误、报表1条≥500ms，status=over_target。两个窗口均结束，owned进程已关闭。旧runner因exit1使用generic measurement_failed标签，实际browser是150成功／0错误、报表1条超线，并非请求技术失败。两窗口catalog physical SHA相同。当前新Q SHA `2a36088e57760745cb2751941862d5c53401e3881563d68ee26c5dfebc17832b`，当前browser SHA `69d24732c1ddddfc02eab2dd30db27b74ed2b92936e4535351c6d2c8808f398e`，旧空闲browser SHA `f3c1d12f3e27ff049de4b9eeb1b26fee6f995c86d1abf93f245c268d52f8f06b`。

| 页面 | 当前1836 median／p95／max ms | 旧24b9空闲 median／p95／max ms | ≥500ms 当前／旧 |
| --- | --- | --- | ---: |
| brief | 317.6／377.5／417.5 | 316.4／339.4／357.7 | 0／0 |
| funds | 277.5／397.1／397.2 | 257／276.9／277.5 | 0／0 |
| employees | 197.5／317.4／397.5 | 197／277.3／277.4 | 0／0 |
| assets | 177.3／217.3／217.4 | 157.7／236.7／277.6 | 0／0 |
| reports | 337.7／457.6／497.5 | 356.9／437.3／555.8 | 0／1 |

当前主12阈值通过不等于所有页面稳定改善；简报、资金、员工和资产的median没有下降，report的p95也没有下降。两次顺序窗口不是交替实验，之前busy环境的慢样本仍保留，不归因全部差异于两组代码。没有连续OS CPU归因，旧主12FAIL10及主48FAIL28不改写。

新controller首轮preflight出现argparse递归，发生于计时前；原脚本另行保全，修正仅为argparse module proxy，不改产品。旧源controller也在实际运行前修正，不删去首次失败、不算浏览器产品错误。[本轮12件证据清单](two-read-groups-main12-20261004-evidence/manifest.json)原字节保留两browser全部samples／ResourceTiming、新完整资格、回归三件、test-style及新45消费者receipt；runner／control明确移除private PID、command及environment快照，按redacted analysis保存原path／SHA，不能冒充raw。每件gzip mtime0、SHA及解压回环核对；不发布数据库、ZIP或helper内容。清单SHA `91d3d67ac908fa57cc2c6c7216f35890fc0537dd2b7f7bb0db4e12336f4f8b75`。

## 主48实际浏览器与同空闲条件旧源对照

当前1836主48重新完整资格核验，四coverage verified、limitations=[]，实际preview后才计时。资格过程1,019,989.8592ms约17分钟，是准备耗时，不是刷新延迟。新Q SHA `4b7c3eab696925aa8d628480ee36afce4c3393820f6b46efe068a26180448dd7`，browser SHA `91bd4d42581a08701ffcd360a273141545ba8113a0f82406f21c7ef6f31dff40`。五页各30样本，150成功／0错误、全部<500ms，status=passed；业务身份／state／history及source inventory守卫为true。**catalog physical SHA不同**，正常owner认证目录变化另列，不能宣称所有文件字节不变；合成凭据已撤销，owned进程已关闭。

同库、同类空闲条件旧24b9主48也150成功／0错误、全部<500ms，status=passed，browser SHA `dcd9a534d895b05bee2c98c5e520a326a350944877a35493471ecbbfbe8f3fae`。明确复用旧精确源完整Q SHA `1a822f7f45934e9eeb52647292f1e10bd9c4bfe601953fd03ecddbf9eb545494`，本次实际native resident preview重新执行，不称新完整Q。业务／source及catalog physical SHA守卫为true，凭据撤销／owned退出；两个纯窗口均结束，没有其他测试或重工作并行。

| 页面 | 当前1836 median／p95／max ms | 旧24b9空闲 median／p95／max ms | ≥500ms 当前／旧 |
| --- | --- | --- | ---: |
| brief | 337.4／357.7／378 | 376.7／397／416.6 | 0／0 |
| funds | 277.2／316.9／337.4 | 296.9／336.5／375.9 | 0／0 |
| employees | 236.4／277.2／317.2 | 256.8／377.3／397.4 | 0／0 |
| assets | 277.1／317.2／337.5 | 277.1／297.3／297.4 | 0／0 |
| reports | 377.4／457.3／457.4 | 377.1／397.6／417.2 | 0／0 |

两源都达阈值；当前report median基本相同、p95及max更高，资产median相同而尾部更高。前面三页单次观察下降不证明稳定代码因果收益，也不由这两次顺序窗口解释之前busy环境FAIL28／150。全部样本与旧失败保留。

[主48七件独立清单](two-read-groups-main48-20261004-evidence/manifest.json)安全raw保留新旧browser全部samples／ResourceTiming及新完整Q；四份runner／controller明确去private PID、command和environment快照，以redacted analysis绑定原path／SHA，不冒充raw。每件gzip mtime0、SHA及解压回环相同；未发布DB、ZIP、helper或资料原件。清单SHA `43a5c04f14083fe4ff9d8a44095f16740e745da9cdb8b32ad49fadd204ba96bd`。

## 当前draft开发包实际构建与默认搬移自检

固定1836源完成一次生产packager及默认搬移自检，没有跳过验证。160实际inputs对应160次CLI调用；包4212个software files、109种fact kind、171个application modules在source／package／ZIP／relocated逐件SHA相同，33个固定v1及3份合同同版。source／package／实际runtime build均为 `local-kernel-2:355c4ccd302303755489f94db850e65533b16d7b30cfa287dc71b99d474ee90c`。758 selected＋manifest共759文件完整inventory前后相同，inventory SHA `9ad3597d34a0396e55f3acc234edb93ee73423f97481498d0be0621501fbb2c5`。所有owned进程已结束，process-exit evidence residual=[]，合成凭据正常撤销及清理。

receipt SHA `b44fe2cfda76865a26988d6bdb3857d7f3288aa3ff1e74702fe03b87452a1c87`，verification SHA `9d6a16d6fea424373f16df43ca6e3d0235d2aff786d760e454eb7f8aee1c7dcb`，package manifest SHA `99eba197e8bc614cf6ae0cfb99159c3e6b2d89b1fe451548416296dd20b0ca93`，ZIP SHA `31978ce467aa5ff7ba1990916512ec47f13e017626c09435286950ad4a03108f`。本轮实际draft包及搬移自检已完成，不因为未来正式包延期而继续标为未验。

包内stdio MCP handshake＋query属于产品自检，不是独立AI实际业务整案，不能继承旧24b9的AI124次操作。`packaged_offline_upgrade=null`如实保留；正式版本与真实当前库前向调整未覆盖，仍按本轮延期边界处理，不把它们扩大成已完成draft包的重复门禁。

[开发包五件安全原字节清单](two-read-groups-development-package-20261004-evidence/manifest.json)保存实际receipt、verification、package manifest、空residual进程退出记录及完整stdout；逐件检查没有密码、私有批准／capability／token值、owner private key、SID／SDDL或运行PID。gzip mtime0、原path／SHA／长度与压缩SHA绑定并回环相同；不公开ZIP、含凭据helpers或验证库。清单SHA `d485ad336deaaa9c9816d23e41a31cd6e0491e0e93e70936eaf939c2c9734d0e`。

## 当前包独立AI实际MCP只读验证

独立AI通过当前开发包的生产stdio MCP完成38次实际tools/call，另计3次initialize、3次tools/list；三个stdio generation涉及两个owned host和client重连，第三轮补齐business_status。171个实际Python模块与1836封存源相同，当前reader build为355c；使用已有明确合成双公司资料，没有重放业务或操作真实根／5173。receipt状态为 `passed_with_filter_parameter_acceptance_limit`，原SHA `8deb9e524a265ffe96354b4896e203e232890c45d46d877ff213a75962fbb9bf`。

38次不全部称成功：32次返回业务／查询结果（23个无顶层status、ready2、blocked1、needs_information1、committed2、unknown3）；5次为预期cursor拒绝（dashboard_snapshot_changed4、fact_cursor_stale1）；1次为调用者漏subject_id的invalid_command，随后按实际schema和发现的expense subject纠正。known缺企业所得税事实的needs_information／blocked保持原语义，不当作产品错误。helper API拼写及receipt schema列名错误发生于测试辅助代码，原失败引用保留，不计入38次产品调用错误。

两公司各247张表，在四个前后snapshot中全部逻辑SHA相同，包含jobs／audit／state／history／repair。甲资金122000、利润-3000整数分，冻结3凭证／3采用；乙资金87500、利润-69600整数分，已知缺所得税事实仍needs_information。客户端重连前后closed report逐字相同，冻结producer原 `local-kernel-2:d9d5ed53a4d7f7cded8930f462801d556717c08903e7a3e40a94b862314c5b28`保留，没有用当前355c reader改写历史。

跨公司dashboard cursors及find_facts cursor明确拒绝；已知请求在甲为committed、乙为unknown，随机未知请求重连前后仍unknown。foreign account filter参数实际接受并echo，乙movement items空、filtered_count=0、无甲业务数据，**不称参数被拒绝**。两个owned HTTP host及所有MCP服务进程正常关闭，残留0，全部session撤销，合成credential token absent，read pools关闭。旧已知合成根的.service JSON及catalog正常认证变化另列，不称目录物理文件不变。

[独立只读MCP三件公开分析清单](two-read-groups-independent-mcp-read-20261004-evidence/manifest.json)保存receipt、完整38调用transcript及selected-read stdout的明确redacted analysis；移除PID和私有owner approval／confirmation／review对象，原path／SHA仍绑定，不冒充raw。清单还列出三份首轮失败原件的私有path／SHA，没有发布helpers、密码、capability／token／owner private keys、native actor commands、全源码清单、数据库或ZIP。gzip mtime0、SHA／回环核对，清单SHA `3eaed3ca51e14e47f7fec6a3832dbaba161b99bd8f5edbe15eb067e5d38c050b`。

本轮是当前包独立AI只读消费，不是旧124次完整写入流程重跑，也未覆盖真人GUI／原生批准、关账写入或500ms。## 当前主120：五页热态FAIL36／150

1836主120新完整资格status=complete，四coverage sources／historical_adoption／projections／read_indexes均verified、limitations=[]；事实308217／计算130619／凭证123117／关账119／证据841。qualification_ms=5,385,264.0612，约89.75分钟；其中完整注册integrity_ms=2,917,152.7783、实际preview_ms=2,464,501.6128，另有preparation_ms=4,046.4234。这些是计时前准备成本，不是刷新延迟。新Q SHA `00651e39653017b6dffc94b3a4758c78b52714cdb14c8af5ac1639aabadf8073`，browser SHA `d5fa52194ef05c0a19434c58f779c54aa57644ae6b0eb5b8787d68b91b786445`。

纯窗口实际结束，五页各30成功样本、共150成功／0错误，status=over_target，runner／controller exit1对应超线，**FAIL36／150**。不能用median或150请求成功表示性能通过，全部慢样本保留。

| 页面 | median／p95／max ms | ≥500ms |
| --- | --- | ---: |
| brief | 378／457.2／517.6 | 1 |
| funds | 317.1／377.3／395.4 | 0 |
| employees | 317.2／357／357.1 | 0 |
| assets | 597.1／676.6／697.3 | 30 |
| reports | 457.7／537.1／576.9 | 5 |

首次first_load=1138.5ms、browser_launch_to_first_render=2160.743408ms、startup=3086.5751ms按harness各自定义分列，不包装成同一whole cold。五页cold_open依次611.8／834.4／675.4／1253.2／775.1ms。company_switch=unavailable／single_eligible_company，不记通过。source inventory及业务身份／state／history守卫为true，catalog physical SHA相同；catalog WAL physical SHA不同，正常认证变化单列，SHM字节不声明不变。合成凭据已撤销，窗口／进程结束。

[主120四件证据清单](two-read-groups-main120-20261004-evidence/manifest.json)安全raw保留完整browser samples／ResourceTiming及新Q；runner／controller明确移除private PID、command与environment快照，以redacted analysis绑定原path／SHA，不公开未脱敏日志。gzip mtime0、压缩SHA／回环核对，不公开DB、ZIP、helper或private通道。清单SHA `870c8fcd7156c237b7ffe0cd1c15983bb5132dc875a5a9b93698d0a4c4718c33`。

主12／48各150／150通过保留；主120的资产全30超线及简报／报表超线须先归组审查，不机械启动独立／压力测量。独立12／48／120、压力12、按需详情及公司切换仍未验。543回归、45消费者、160CLI开发包及38只读MCP仍仅表示其已完成范围，第9阶段未完成，不提交。

## 两档证据的本轮改动风险桥接

240／1536MiB核验、备份恢复及资源／前台轻样本观察仍绑定旧24b9，原结果与限制不改写。本轮简报／gross复用未改BLOB、stream、chunk、ZIP或restore；结算表示保留完整checked顺序、canonical、root／digest和boundary等价oracle，风险随group／状态变化，不随证据字节增加。当前543项数据保全与160实际CLI中的真实备份恢复足以桥接本轮改动，没有新增证据路径风险，因此原件全链不再作为重复门禁。

当前1836两档耗时／RSS未直接量测，不称当前规模通过或改善。只有BLOB／chunk／ZIP、任务并发、对象lifetime变更或实际OOM／新峰值时，再按变化风险复测。本判断不改变主规模五页全部成功样本<500ms的承诺；主120已测FAIL36／150，待归组审查；独立三规模、压力及按需详情／公司切换仍未验。

当前主120已测FAIL36／150、待归组审查；独立12／48／120、压力12及按需详情未验；旧双档／完整MCP／硬边界证据不转移到1836。目录和公司库仍draft／0，正式冻结、正式交付及入口切换延期。第9阶段未完成，不提交。
