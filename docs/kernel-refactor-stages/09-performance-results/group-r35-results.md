# r35 损坏错误断言收口

源码固定为 `922b3cf5c1fb60d0abe057dfd7e09fd4de6e269e9a2931b2a5882cbe57378e9b`。逐文件比较 r34/r35 清单，唯一差异是 `tests/kernel/test_brief_adopted_basis_reads.py`；生产源码、构建产物及三份合同逐字节相同。r34 的 494 通过／2 失败原件保留，不能改写为全通过。

同类排查覆盖 `tests/kernel` 的 `pytest.raises(AttributeError/ValidationError)`：本文件两项属于已保存内容损坏，其他是输入或模型校验。修正后明确检查 `KernelError.code == content_integrity_failed`，两次读取均拒绝、失败不记成功证明、不返回业务追问的断言保留。测试格式收敛后，固定 r35 仅对该文件运行一次，共 **5 passed**，源码前后未变，总约 10 秒；未重跑其余 41 个文件。日志与 XML 见 [归档清单](group-r35-manifest.json)。

本次依据由 r34 受影响回归和 r35 测试修正回执共同构成；不把次数相加为不同测试数，不称全库或正式版验收完成。后续新结构样本使用 r35 固定源，目标逐表相等、注册完整核验和真实当前预览仍必须实际通过。纯浏览器最新结果仍为 r32 超时，不能将局部工作量下降写成 500 毫秒达标。
