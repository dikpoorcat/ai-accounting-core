<!-- @format -->

# r70主48月：纯浏览器刷新性能失败

固定r70主48月五页各30次刷新完成，150个样本完整保留；**37／150次达到或超过500ms，性能失败**。简报25、资产8、报表4次超线；资金与员工各0次。JSON status=`over_target`，stdout同时保存timing_ready和over_target，不改写为通过。此前六入口native均小于500ms不能替代这份实际浏览器失败。

来源为`.tmp/stage9-build-source-owner-r70-release`，源码SHA `402024802800938685bfb59983e6d717ae32fa55c555bad0a3ced3f3d8a8e404`，backend build=`local-kernel-2:cb6098923bedd4fe09510fb402e6d0b8c1c88e5dde86f00e98c6c4342e92bd89`。样本root=`.tmp/stage9-owner-main48-r63`，2019-12、50员工、每月1000业务、总48000、mixed_cumulative。计时scope=main_page_candidate、mode=page_timing、正式release静态、默认20条、每页warmups3/repeats30。HTTP/dispatch插桩数组为空，不以插桩耗时或原生值替换浏览器值。

## 全部热刷新结果

| 页面 | median ms | p95 ms | max ms | ≥500ms |
| --- | ---: | ---: | ---: | ---: |
| brief | 536.50 | 576.60 | 576.60 | 25 / 30 |
| funds | 316.30 | 375.90 | 395.00 | 0 / 30 |
| employees | 255.40 | 316.30 | 334.90 | 0 / 30 |
| assets | 476.10 | 574.80 | 594.40 | 8 / 30 |
| reports | 476.50 | 517.10 | 575.40 | 4 / 30 |

所有逐次samples保留在原始JSON与gzip，不删慢样本、不取最小值宣称达标。资金／员工仅在本固定主48样本及本轮30次满足热刷新目标，不能外推其他规模或整阶段。

## 冷开和切公司独立范围

本轮JSON另保存导航观察：first_load=4021.600ms，browser_launch_to_first_render=5913.850ms，startup=3752.108ms；cold_open分别brief1001.200、funds560.200、employees493.600、assets719.400、reports714.700ms。它们是本轮导航观察，不是独立`--navigation-only cold`重复分布，不混入150次热刷新样本或冒称冷态通过。

company_switch.status=unavailable，reason=single_eligible_company；本轮没有公司切换样本，不能视作0ms或成功。synthetic_credentials_revoked=true是报告记录的测试凭据收尾，不构成性能或业务核验。

## 核验前提、控制文件与归档

主12／主48／独立12的当前r70注册完整核验均四coverage verified、limitations=[]，各有真实当前开放月preview，见 [内容与边界组](owner-r70-content-and-boundaries.md)。本次browser在正式计时前实际再次preview并使用ready/start门控。start文件记录heavy_work_stopped=true，仅是计时协调声明；ready含PID/output，仅为准备信号。两个文件均按控制材料保存，不能单独证明完整核验或浏览器通过。

[专属manifest](owner-main48-browser-r70-manifest.json)排他保存完整JSON/stdout、实际PS1、ready/start五个原件的gzip。每项raw/archive SHA、bytes、mtime及解压逐字节等价记录。来源清单和current proof引用既有组，不重复搬迁；旧失败及native档不回写。

主48完整内容核验通过而浏览器性能失败，两个结果分别成立。大样本承接dry-run不是目标完整核验；主/独立120、独立48、压力、独立冷开/切换、原件规模、正式v1三合同、最终包和实际AI MCP仍需对应回执。仓库仍draft/0。本次仅归档/文档更新，没有测试、SQL、服务、生产源码修改或新增计时。
