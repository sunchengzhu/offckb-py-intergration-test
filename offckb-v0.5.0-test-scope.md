# OffCKB v0.5.0 canary：PR 分类与测试覆盖

本次分析 [`0.5.0-canary-ee0ad6b`](https://www.npmjs.com/package/@offckb/cli/v/0.5.0-canary-ee0ad6b) 相对 `v0.4.13` 的变化，共 8 个 PR。按验收责任归为三类：功能与运行兼容、上游测试维护、版本与发布维护。

既有自动化可回归原有 CKB 流程；Fiber 专项范围和用例见 [完整评审目录](reviews/fiber/README.md)。下表用于划分验收责任；实际自动化覆盖由 `TEST-MAP` 检查器计算，执行方式见 [Fiber 测试说明](tests/fiber/README.md)。

| 类别与关注程度 | PR | 改动说明 | 本工程测试关注 |
| --- | --- | --- | --- |
| 功能与运行兼容：重点验收 | [#483](https://github.com/ckb-devrel/offckb/pull/483)、[#510](https://github.com/ckb-devrel/offckb/pull/510) | 新增 Fiber devnet 的启停、状态、日志、配置、清理及合约；配套 FNN 从 `0.9.0-rc7` 升至 `0.9.0`，更新实际运行依赖和下载校验值。 | 回归普通 CKB 流程；Fiber 按评审范围验证 FNN 版本、启动互联、通道支付、关闭与资金返还、重启和清理。 |
| 上游测试维护为主：不单列专项 | [#506](https://github.com/ckb-devrel/offckb/pull/506) | 修复单测导致 Jest worker 退出的问题；另将运行代码中的身份探测超时从 5 秒增至 15 秒。 | 单测与 CI 修复由上游验证，不属于本工程黑盒验收范围；运行代码调整随现有启停用例回归。 |
| 版本、发布与分支维护：简要核验 | [#505](https://github.com/ckb-devrel/offckb/pull/505)、[#507](https://github.com/ckb-devrel/offckb/pull/507)、[#508](https://github.com/ckb-devrel/offckb/pull/508)、[#509](https://github.com/ckb-devrel/offckb/pull/509)、[#511](https://github.com/ckb-devrel/offckb/pull/511) | 更新 OffCKB 包版本和发布说明、修正 canary 版本命名、将已有改动同步到发布分支。 | 核对最终包版本与来源即可；不单独增加功能用例，不重复计算同步 PR 的功能覆盖。 |

首批 P0 流程：启动 → 节点互联 → 开通道 → 支付 → 协作关闭通道 → 确认资金返回。随后补齐重启、清理、配置与错误恢复。
