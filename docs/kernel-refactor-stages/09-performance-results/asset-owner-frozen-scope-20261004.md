# 第9阶段：资产 owner 冻结读取范围（2026-10-04）

第9阶段仍未完成。第一组固定验证源为 `.tmp/stage9-build-source-asset-owner-scope-20261004`，source SHA `69838e3ceb208033304bd756c2a607888687c8717945205546f22d7310bcce91`，manifest SHA `042d1e2c7cf0267d3a9f548073c5237e7ff84d952499da3b79a71f4b4ccf9ced`，共760文件。相对[必要读取组](needed-read-groups-20261004.md)的f105，仅三份生产文件变化，新增资产owner专项和此前descriptor增长测试，另755文件相同。源码差异回执为 `.tmp/stage9-asset-owner-scope-source-diff-20261004.json`。

本轮保持目录与公司draft／0、现有5173、资料根和身份原样，不改schema、wire、固定v1、正式版本或运行入口。集中消费者166 PASS，五页profile与before／after native顺序窗口完成，五页严格业务SHA一致；资产必要工作下降且该窗口观察到延迟改善，不构成稳定因果或浏览器500ms证明。历史1836的543／45／160实际CLI／38独立MCP及主12／48通过、主120FAIL36／150仍只绑定[原两组记录](two-read-groups-20261004.md)，f105记录保持原范围。

## 根因与保留实现

资产owner候选总集合在每个冻结月都进入accounting filter，导致大量跨月negative tests及其false-positive桶读取。只按mutable kind、当前publication或voucher提前过滤不能证明冻结采用；无voucher结果、no-impact新依据沿旧voucher和丢失来源仍须可见。

`close_storage.read_asset_owner_accounting_many`从认证header中的完整`asset_batch_adoptions`声明取得每月owner范围，严格检查声明形状、唯一owner、membership digest及真实calculation／fact／subject与seal。随后仍针对完整请求universe取得actual publication与voucher authority，并按月union到物理读取范围。完整输出subject集合、实际publication／voucher与冻结采用比较不缩窄；未声明但实际存在的source仍进入读取，后续独立owner集合比较仍拒绝遗漏声明。

`query_reads`只在owned current snapshot启用新路径，owner proof与ordinary proof使用不同cache key，不能相互冒充；整组成功后才发布parts、positions及slice缓存，失败保留已有准确成功scope且不发布新前缀。unowned与fixed-v1保留原完整读取。`business_queries`仅将资产owner metadata消费者接至该专用入口，其他入口和完整verifier职责保留。

## 专项行为与小库工作量

`tests/kernel/test_asset_owner_frozen_scope.py`初次完整专项17 PASS，覆盖与完整slice一致、九类missing／wrong source拒绝、遗漏声明仍由actual publication发现、连续closed corrections、no-impact新adopted basis沿旧voucher、zero-line owner、unowned fallback、owner／ordinary缓存隔离和新snapshot重核验、批尾失败不发布前缀。早期fixture失败涉及不合法`admin`值、owner修订号及no-impact posting_period冲突，均为测试构造问题，修正后才得到上述结果，不隐藏早期失败。

同一growth测试后续扩为三份实际未来owner（含真实publication、members与voucher），单独复验1 PASS。这是原专项中同一node的更新复验，不累计为18个不同测试。

小库专用范围的physical返回为10行／3,450字节，普通JSON为12次／8,964输入字节；没有额外false-positive。三份未来owner增加请求universe后，该physical与JSON工作不增长，结果叶仍与原小库相同；headers SQL增加1次如实保留。该合成工作量证据不推算整页毫秒或大库结果。SQL返回字节不是磁盘IO，JSON输入字节不是额外数据库读取。

## adopted-head同类排查

`dashboard_reads._adopted_heads_sql`的冻结locator双lane扫28,560条readiness references，只取得两组各119个adopted hits；selected为240行，对应约239,500 SQLite VM步骤。SQL姿势候选没有观察到收益，未保留生产改动。直接冻结adopted leaf候选尚未证明bad paired reference与redirect scope情况下的等价拒绝语义，未实施、未验证；不能据旧负面结果将其永久排除，也不能把mutable来源过滤写成冻结分类证明。

## 集中消费者与五页工作量

集中消费者166 PASS／346.72秒，日志／XML为 `.tmp/stage9-asset-owner-scope-consumers-20261004.log`与同名`.xml`；XML suite时间346.564秒单列。13个文件覆盖新owner范围17项、owner outcome／locator／adopted identity、资产identity／activation／consumption／open range、whole-month简报金额、员工adopted results、完整资产关账核验、卡片分页和owner metadata。其中metadata包含跨域业务消费者、unowned／fixed-v1、repair原子拒绝与registered backup verifier；它不是实际CLI／MCP或全部五页transport验收。与早先17项和growth复验重叠，不累计为184项或完整543回归。

同一隔离主120来源的f105 native baseline、69838 native current与69838 profile，五页strict business SHA逐页相同，并与原f105 profile一致。每次before／after的source inventory、files／DB／WAL、company／catalog格式与身份、state／history守卫均相同，read pool关闭；跨source的company／catalog及非manifest files业务守卫相同。source-manifest路径与hash各自绑定源码，不能要求baseline与current manifest相同。

资产profile为358 SQL（+1）、12,185返回行（-70）、12,091,125返回值字节（-230,537）、1,235,200 VM（+4,600）。结果正文仍485行／527,255字节／485 decoded；普通JSON降至2,596次（-308）／9,090,289输入字节（-271,855）。其余四页全部work counters不变。资产instrumented 1,287.8384ms只属诊断，不作为纯native或500ms结果。SQL返回字节不等于磁盘IO，VM为采样步骤，Python分配不等于RSS。

## guarded native顺序窗口

f105 baseline与69838 current各入口3 warmups＋30成功样本，无HTTP、render、profiler或SQL／VM观察器。下表每格为median／p95／max毫秒，全部warmup与逐次样本均归档。

| 入口 | f105 baseline | 69838 current |
| --- | --- | --- |
| assets | 539.16555／573.7752／586.3409 | 463.32035／493.4538／495.5086 |
| brief | 346.68925／402.454／406.5641 | 340.9671／362.6586／373.0314 |
| reports | 382.90705／463.7043／517.8953 | 390.25005／463.2364／490.205 |
| funds | 247.0912／306.5827／307.1753 | 241.2234／296.8138／306.4351 |
| employees | 271.60485／314.9151／327.8549 | 276.05335／315.7195／319.9527 |

资产下降与必要读取减少一致，但顺序有限窗口不证明稳定因果改善；其他入口仍有波动。native没有prepared owner TODO、HTTP或渲染，不能据当前样本全部低于500ms关闭浏览器目标。

root首次比较误把baseline／current的source-manifest文件位置与hash要求相同，触发assert；纯样本和业务守卫本来正确。改为company／catalog／非manifest files比较后相同，各manifest仍核对自身before／after；未重跑pure，也未删除原样本。

## 归档与剩余范围

[本组9件证据清单](asset-owner-frozen-scope-20261004-evidence/manifest.json) SHA `81cc22720d6408745f91ed003188e8b6a64a7eefb809c85a68d831ae04de7d2a`。166 XML／log、native日志及source-diff保留原字节；两个native JSON与profile明确标为redacted analysis，完整样本／counters及SQL诊断保留，绝对根路径去除，guard内容与read_context仅保存SHA。私有原件相对path／SHA／长度和公开representation SHA／长度分别绑定，不冒充raw；summary记录范围、文件逐项计数、cross-source比较与首次assert修正。gzip mtime=0、压缩SHA及解压回环相同。未复制数据库、ZIP、脚本、完整source inventory或凭据，不改旧归档。

第一组69838源尚无新完整资格／actual preview、prepared浏览器、实际开发包或独立CLI／MCP只读验收；历史1836资格只是来源。主120原browser失败未关闭，独立12／48／120、压力12、按需详情及公司切换仍未验。正式冻结、交付和切版延期；旧失败、首次错误与慢样本原样保留，不转移历史通过数，不提交。第二组dashboard_reads.py的精确(head ID, posting_period) lookup实现与专项验证已收敛，已纳入固定候选源2e474，结果见[head locator范围组](head-close-locator-scope-20261004.md)；第三组classification最终candidate及30项消费者完成，流式candidate已保留，pure顺序窗口完成但不证明稳定因果或browser500，共同来源与profile见[有界读取组](bounded-read-groups-20261004.md)。第一组全部结果和归档只绑定69838，不代表更新工作区或本轮最终封存；未验证候选不写成已解决。
