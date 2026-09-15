# Fiber 协作关闭通道用例评审

评审范围：用户在 OffCKB 本地环境完成 CKB 支付后，关闭通道并收回资金，结束一轮实验。

版本依据：OffCKB `0.5.0-canary-ee0ad6b`、FNN `0.9.0`，与 [Fiber 领域地图](README.md) 一致。

## 待评审用例

| 用例 | 场景 | 预期结果 | 防止的问题 | 优先级 |
| --- | --- | --- | --- | --- |
| `FIB-CLOSE-01` | 用户在 OffCKB 双节点环境已完成一次 CKB 通道支付，双方在线且无待处理转账；节点 1 通过 `shutdown_channel` 发起普通协作关闭，双方使用已约定的开发账户地址收回资金 | 双方通过 `list_channels` 查询到该通道为 `Closed`、关闭标记为 `COOPERATIVE`；同一 `shutdown_transaction_hash` 对应的交易在该 devnet 上为 committed，消费原 funding cell；关闭交易向双方约定地址返还各自本金加减已结算支付后的 CKB，发起方扣除实际关闭费用，接收方金额不被误扣，返还资金可通过 OffCKB 查询并再次转账 | 关闭请求受理就误报实验完成，通道数据消失但资金未结算，或关闭交易金额、收款地址及合约依赖错误导致资金无法取回 | P0 |

## 判定与范围边界

- 关闭使用 `force: false`、有效的 secp256k1 开发账户收款脚本及正常费用率；发起方通过 `close_script` 指定地址，接收方使用开通时约定的 `shutdown_script`（默认对应自身资金账户）。不删除数据库来模拟关闭，不纳入强制关闭与争议处理。
- 使用 `list_channels` 的 `include_closed: true` 查询最终通道；默认列表中找不到通道或 `shutdown_channel` 返回空结果均不代表关闭完成。关闭状态、交易 committed、输出可用及后续转账 committed 都使用有截止时间的轮询。
- 本金是双方实际投入通道的 CKB，包含预留容量；不能直接把关闭前的 `local_balance` 当作全部返还额。以本金加减已结算支付核算双方应得金额，从关闭交易输入、输出 capacity 差取得实际费用；按收款 lock 汇总输出，不依赖输出顺序。核验后使用各收款账户通过 OffCKB 转出一笔正常金额，证明返还资金可继续使用。

依据：[FNN 关闭参数与费用承担](https://github.com/nervosnetwork/fiber/blob/e6cb7ac7770b1798a1ad5dfb9a8f4ae5db52036f/crates/fiber-json-types/src/channel.rs#L556-L573)、[关闭记录查询](https://github.com/nervosnetwork/fiber/blob/e6cb7ac7770b1798a1ad5dfb9a8f4ae5db52036f/crates/fiber-lib/src/rpc/channel.rs#L292-L382)、[CKB 本金与预留容量返还](https://github.com/nervosnetwork/fiber/blob/e6cb7ac7770b1798a1ad5dfb9a8f4ae5db52036f/crates/fiber-lib/src/fiber/channel.rs#L9603-L9644)、[对端自动协作关闭](https://github.com/nervosnetwork/fiber/blob/e6cb7ac7770b1798a1ad5dfb9a8f4ae5db52036f/crates/fiber-lib/src/fiber/channel.rs#L10082-L10119)。
