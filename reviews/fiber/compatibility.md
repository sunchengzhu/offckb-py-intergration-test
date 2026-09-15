# 旧开发环境升级用例评审

评审范围：将 OffCKB `0.4.13` 升级为 `0.5.0-canary-ee0ad6b`，继续普通 CKB 开发，或按提示重建为支持 Fiber 的环境。

## 接口说明

- 旧环境必须由 `0.4.13` 实际初始化，保存普通交易和自定义配置，再在同一隔离用户目录换用 canary。两版使用同一真实 CKB，聚焦 OffCKB 升级行为。
- 升级 CLI 不会给旧 genesis 补充 Fiber 合约；`clean -d` 保留旧 chain spec，完整 `clean` 后才从新包重建。测试只操作可丢弃的本地实验数据。

## 待评审用例

| 用例 | 场景 | 预期结果 | 防止的问题 | 优先级 |
| --- | --- | --- | --- | --- |
| `FIB-COMP-01` | 用户的 0.4.13 普通 devnet 已有交易和自定义配置，停止后升级到 canary，执行普通 `node` 继续开发，不启用 Fiber | 原配置和 genesis 保留，旧交易仍为 committed，节点持续产块，并能通过 OffCKB 完成一笔新的 CKB 转账；不自动启动 FNN | 用户仅升级 CLI 就丢失已有开发环境，或新功能破坏普通 CKB 开发流程 | P1 |
| `FIB-COMP-02` | 用户升级后，在缺少 Fiber 合约的旧 devnet 上尝试增加 Fiber：已有链运行时执行 `fiber start`，旧链停止时执行 `node --fiber` | 启动非零失败，明确指出缺少合约，给出停止服务、完整 `offckb clean` 后重建的指导及数据删除提示；旧 spec 和链数据保留，无本次启动的 FNN 或残留组件；原本运行的 CKB 继续可用 | 启动长期卡住或错误不可操作，或为了启用 Fiber 静默重建用户旧链 | P1 |
| `FIB-COMP-03` | 用户在 0.4.13 旧环境停止服务后换用 canary，只执行 `clean -d`，随后尝试 `node --fiber` | 旧 chain spec 和自定义配置保留，旧链数据被清除；Fiber 仍因缺少合约非零拒绝启动并提示完整重建，不宣称数据清理已完成 Fiber 升级 | 将只清数据库误当作升级合约，导致用户在旧 spec 上反复启动失败 | P1 |
| `FIB-COMP-04` | 用户确认旧实验数据可丢弃，停止旧 devnet，使用 canary 完整 `clean` 后执行 `node --fiber`，再开通普通 CKB 通道并支付 | 使用新包生成配置和含 Fiber 合约的新 genesis；旧交易及旧自定义 spec 不再沿用；默认双节点互联，通道双方达到 ChannelReady，funding 交易 committed，发票支付结算成功且双方余额准确变化 | 按迁移指导操作后仍加载旧合约或配置，环境看似重建却不能使用 Fiber | P1 |

范围边界：这里只验证旧 OffCKB devnet 的使用与重建，不承诺保留旧链数据启用 Fiber，也不覆盖 FNN 跨版本数据库迁移。通道与支付沿用 [首次支付](startup-payments.md) 的公开 RPC 判定。

依据：[保留既有链配置](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/node/init-chain.ts)、[缺失合约与迁移提示](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/fiber/scripts.ts)、[数据清理与完整清理](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/cmd/clean.ts)。
