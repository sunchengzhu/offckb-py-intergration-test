# Fiber devnet 测试分析

本页说明 Fiber devnet 的测试领域与边界，具体场景见各领域评审文档。

## 目标与版本

验证用户能通过 OffCKB 建立本地 Fiber 环境，完成支付、关闭通道并收回资金，也能继续开发或重置实验。OffCKB 负责安装和选择 FNN、配置链与账户、提供合约依赖及管理进程；FNN 的公开 RPC 用来验证这些配置实际可用。

- OffCKB：`0.5.0-canary-ee0ad6b`，固定提交 [ee0ad6b](https://github.com/ckb-devrel/offckb/tree/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2)。
- FNN：该提交绑定的 `v0.9.0`，固定提交 [e6cb7ac](https://github.com/nervosnetwork/fiber/tree/e6cb7ac7770b1798a1ad5dfb9a8f4ae5db52036f)。分析使用这一版本的 RPC 和配置。

## 测试领域

下表按用户流程组织测试领域，各领域的完整用例表见对应文档。

| 领域 | 主要入口 | 验证重点 | 评审文档 |
| --- | --- | --- | --- |
| 启动到首次 CKB 支付（P0，首批） | `node --fiber`；已有本地链上的 `fiber start`；FNN 通道与支付 RPC | 默认两个 FNN 使用正确版本、链、账户与合约依赖并自动互联；开通道后完成一次 invoice 支付。启动成功必须有实际使用结果支撑。 | [启动与首次支付](startup-payments.md) |
| 结束通道实验（P0，首批） | FNN `shutdown_channel` | 协作关闭通道后，通过公开 RPC 确认关闭状态、链上关闭交易已 committed，资金返回约定地址，完成一轮通道实验。 | [协作关闭与资金返还](channel-close.md) |
| 停止后继续开发（P0） | 前台 Ctrl+C、`node stop`、`fiber stop`、再次启动 | 区分联合管理与独立 FNN 管理的停止范围；进程和端口释放；同版本正常重启保留身份、通道和余额，并能再次支付。 | [停止、重启与清理](lifecycle-state.md) |
| 重置实验与数据保护（P0） | `fiber clean --data`、`fiber clean`、`clean -d`、`clean` | 分清 FNN store、身份/配置和 CKB 链数据的删除范围；运行中拒绝不安全清理，保留范围正确，重建后可重新使用。 | [停止、重启与清理](lifecycle-state.md) |
| 查看与调整环境（P1） | `fiber status/logs`、`config get/set fnn-version`、节点数量与二进制选项、`nodes.yml` | 状态和日志对应实际节点；用户设置在重启后生效；修改配置不能串用网络、账户或节点。默认版本选择与自定义二进制分别验证。 | [配置与诊断](configuration-diagnostics.md) |
| 启动失败后恢复（P1） | 错误 FNN 路径、端口占用、重复启动、余额不足后的修正重试 | 错误可定位；回收本次启动的组件，保护原有服务与数据；修正后在同一隔离环境可正常启动。 | [启动失败与恢复](startup-recovery.md) |
| 使用自己的测试代币（P1） | OffCKB 发行/转账 UDT，FNN 开通道、接受通道及支付 | 使用 account 19 对应 whitelist；双方准备匹配资产和 CKB 容量；验证 type script、数量单位和通道接受方式，完成代表性 UDT 支付。 | [UDT 通道支付](udt-payments.md) |
| 旧环境升级（P1） | 0.4.13 旧 devnet 换装 canary | 旧链可继续普通开发；缺少 Fiber 合约时给出重建指导；`clean -d` 保留旧 spec，不能当作已完成迁移。 | [旧开发环境升级](compatibility.md) |
| 首次下载与平台兼容（独立环境） | FNN 下载、版本校验、交互流程、不同操作系统 | 下载的版本和布局可被 OffCKB 使用；下载失败可恢复；平台与交互行为单独验收。 | [下载、平台与交互](installation-platforms.md) |

## 结合 FNN 后的关键判断

1. `fiber status` 的运行状态不足以证明环境能支付。需用 `node_info` 核对版本、身份、资金账户及与 CKB 一致的 `chain_hash`，再轮询 `list_peers` 确认预期对端。OffCKB 应自行连接节点，测试不能补调 `connect_peer` 掩盖连接失败。
2. `open_channel` 返回临时通道 ID，不表示开通完成。需取得最终通道，确认双方 `state.state_name = ChannelReady`，并确认 funding 交易在 CKB 上为 `committed`。首次上链后仍可能需要等待 FNN 的确认深度。
3. 支付完成以付款方 `get_payment.status = Success`、收款方 `get_invoice.status = Paid` 及通道余额变化共同判断。`payment_hash` 是支付标识，不能拿它当作 CKB 交易哈希等待上链。
4. 正常停止、协作关通道和清空数据库是三种操作。重启场景先限定同版本、支付已完成且无待处理转账；清理场景不能把数据库消失当成通道关闭或资金结算。

依据：[OffCKB 启动与自动互联](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/fiber/manager.ts)、[FNN 通道字段](https://github.com/nervosnetwork/fiber/blob/e6cb7ac7770b1798a1ad5dfb9a8f4ae5db52036f/crates/fiber-json-types/src/channel.rs)、[支付状态](https://github.com/nervosnetwork/fiber/blob/e6cb7ac7770b1798a1ad5dfb9a8f4ae5db52036f/crates/fiber-json-types/src/payment.rs)、[发票状态](https://github.com/nervosnetwork/fiber/blob/e6cb7ac7770b1798a1ad5dfb9a8f4ae5db52036f/crates/fiber-json-types/src/invoice.rs)。

## 测试方式与范围边界

- 按 [PR 分类](../../offckb-v0.5.0-test-scope.md)，专项验收聚焦 #483 的 Fiber 功能和 #510 的 FNN 运行依赖升级；上表中的版本选择、下载与平台兼容验证实际 FNN 的安装和使用。
- #506 的上游单测与 CI 修复不纳入本工程验收；其中身份探测超时的运行代码调整随既有启停用例回归，不新增专项。#505、#507、#508、#509、#511 属于版本、发布与分支维护，在测试准备时核对最终包版本与来源，不单列功能用例。
- 继续使用本工程的 Python/pytest 黑盒方式，通过隔离安装的 OffCKB、真实 CKB/FNN、公开 RPC、文件和进程验证。FNN 上游 Rust/Bruno 测试只作为行为参考，不替代本工程验收。
- 核心场景预备真实 CKB `0.208.0` 与 FNN `0.9.0`，保留默认选择路径；下载另标 `network`。当前 runner 支持 Linux/macOS，Windows 另行安排。所有完成状态使用有截止时间的轮询。
- Fiber 使用独立用户目录和 fixture，串行占用 CKB 端口及对应 FNN 端口。默认两节点的 RPC 为 `21714/21715`、P2P 为 `8344/8345`；其账户为内置 account 3/4，不能与原资产测试共享资金状态。
- 原有账户、资产、项目、系统脚本与清理用例继续回归；它们没有启动 FNN。特别是 `SYS-01/02` 只验证旧脚本代表项，UDT 用例没有验证 account 19 的 Fiber whitelist，普通重启也不能证明通道恢复。
- 首批使用普通本地 devnet，完成双节点 CKB 通道建立、一次普通 invoice 支付、协作关闭通道及资金返还确认。多跳/MPP、跨链与 LND、watchtower 惩罚、强制关闭争议、FNN 跨版本数据库迁移及协议压力矩阵不纳入本轮；公共网络和 fork 的不支持提示仍属于 OffCKB 入口保护。

集成依据：[OffCKB Fiber 使用说明](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/README.md)、[账户与端口](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/fiber/paths.ts)、[合约与 UDT whitelist](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/fiber/scripts.ts)、[FNN RPC 文档](https://github.com/nervosnetwork/fiber/blob/e6cb7ac7770b1798a1ad5dfb9a8f4ae5db52036f/crates/fiber-lib/src/rpc/README.md)。

## 下一步

先集中评审以上各领域的完整用例集合，确认新增或修改的用例后再实现自动化。实现优先串起启动、开通道、支付、协作关闭及资金返还，再覆盖继续开发、清理和其他场景；下载、平台及 TTY 流程使用独立环境。
