# Fiber 测试代币支付用例评审

评审范围：用户通过 OffCKB 发行、分发测试代币，在其默认 Fiber 配置中开通 UDT 通道并完成一次支付。

版本依据：OffCKB `0.5.0-canary-ee0ad6b`、FNN `0.9.0`，与 [Fiber 领域地图](README.md) 一致。

## 待评审用例

| 用例 | 场景 | 预期结果 | 防止的问题 | 优先级 |
| --- | --- | --- | --- | --- |
| `FIB-UDT-01` | 用户在 OffCKB 双节点 devnet 中，以内置 account 19 通过 `udt issue` 发行默认资产标识的测试代币，再通过 `transfer` 分发给节点 1/2 的 account 3/4；分别使用 SUDT 和 xUDT 完成这条准备流程 | 发行、分发交易均 committed；两节点账户的 OffCKB 余额与链上 live cells 汇总一致，数量精确且资产类型、完整 type args 保持不变；资产的 type script 匹配两节点由 OffCKB 生成的对应 whitelist，双方仍有开通道所需的 CKB 容量 | 代币在 OffCKB 内能发行和转账，却因发行账户、资产标识或 whitelist 不匹配而无法用于 Fiber | P1 |
| `FIB-UDT-02` | 用户已将 account 19 发行的 SUDT 或 xUDT 分发给两个 FNN 账户，并为双方保留充足 CKB；节点 1 使用该资产的完整 `funding_udt_type_script` 开通道，节点 2 在默认配置下手工 `accept_channel` 并投入同类代币 | 节点 2 可查询待接受请求，并可用其 `temporary_channel_id` 完成接受；双方最终查询到同一 `ChannelReady` 通道，资产 type script 与投入数量正确，funding 交易在 devnet 上为 committed；无需修改 OffCKB 管理的 whitelist 或合约依赖 | 误把 CKB 自动接受规则用于 UDT 导致流程停滞，或生成配置缺少正确 UDT 依赖使通道无法上链 | P1 |
| `FIB-UDT-03` | 用户在 OffCKB 创建的 SUDT 或 xUDT 通道中已有充足可用余额且无待处理转账；节点 2 创建该资产 100 个最小整数单位的普通发票，节点 1 按发票完成直连支付 | 付款方 `get_payment.status = Success`，收款方 `get_invoice.status = Paid`；双方通道余额分别精确减少、增加 100 个同类代币单位，完整资产 type script 不变，待处理转账清空且通道仍可用 | 代币发票与通道资产不一致、误按 Shannon 缩放数量，或支付报告成功但代币未完成结算 | P1 |

## 判定与范围边界

- SUDT、xUDT 各完成一条发行到支付的代表性流程，使用 account 19 的默认 issuer lock hash 作为 type args。OffCKB 的 whitelist 对完整 args 精确匹配；不扩展自定义 xUDT args、其他发行账户或扩展脚本。私钥仅通过文件或指定环境变量传入。
- OffCKB 生成的 whitelist 未设置 `auto_accept_amount`，FNN 因此不会自动接受这些 UDT 通道；节点 2 查询待接受请求后调用 `accept_channel`，`funding_amount` 为同类代币数量。两节点另需 CKB 支付 cell 容量与链上费用，不能用 UDT 余额替代 CKB 资金准备。
- OffCKB CLI 数量使用十进制整数，FNN UDT 金额使用十六进制编码的同一整数，不乘 CKB 的 `100,000,000`。发票使用 `currency: Fibd` 和相同的 `udt_type_script`，省略 `payment_hash`、`payment_preimage`、`final_expiry_delta`；等待实际 funding 交易 committed、双方 `ChannelReady` 后再支付。
- 开通和支付完成均通过有截止时间的 RPC 轮询确认；余额以 `pending_tlcs` 为空时为基线。仅核验直连普通发票支付，不扩展多跳、MPP 或 UDT 协议边界矩阵。

依据：[OffCKB 默认 UDT 发行](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/sdk/ckb.ts#L442-L492)、[Fiber whitelist](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/fiber/scripts.ts#L134-L153)、[FNN UDT 自动接受条件](https://github.com/nervosnetwork/fiber/blob/e6cb7ac7770b1798a1ad5dfb9a8f4ae5db52036f/crates/fiber-lib/src/ckb/contracts.rs#L454-L460)、[手工接受通道参数](https://github.com/nervosnetwork/fiber/blob/e6cb7ac7770b1798a1ad5dfb9a8f4ae5db52036f/crates/fiber-json-types/src/channel.rs#L294-L308)、[UDT 发票参数](https://github.com/nervosnetwork/fiber/blob/e6cb7ac7770b1798a1ad5dfb9a8f4ae5db52036f/crates/fiber-json-types/src/invoice.rs#L116-L146)。
