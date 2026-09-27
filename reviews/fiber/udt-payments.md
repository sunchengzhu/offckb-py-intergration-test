# Fiber 测试代币支付用例评审

评审范围：用户通过 OffCKB 发行、分发测试代币，在其默认 Fiber 配置中开通 UDT 通道并完成一次支付。

版本依据：OffCKB `0.5.0-canary-ee0ad6b`、FNN `0.9.0`，与 [Fiber 领域地图](README.md) 一致。

## 待评审用例

| 用例 | 场景 | 预期结果 | 防止的问题 | 优先级 |
| --- | --- | --- | --- | --- |
| `FIB-UDT-06` | 用户按 canary 使用说明体验正常 UDT 支付：在 OffCKB 默认双节点 devnet 中，以 account 19 发行 SUDT 或 xUDT，通过 OffCKB 分发给 account 3/4 各 10,000 个最小整数单位，并为双方保留充足 CKB；节点 1 使用默认费率投入 5,000 个代币开通道，节点 2 查询待接受请求后以 `funding_amount: 0x0` 手工接受，再创建 100 个同类代币单位的发票，由节点 1 支付 | 发行、分发及 funding 交易均 committed；双方查询到同一 ChannelReady 通道，完整资产 type script 匹配 OffCKB 默认 whitelist，初始通道余额分别为 5,000 和 0；链上双方剩余该资产分别为 5,000 和 10,000，CLI 余额与独立 live cells 汇总一致；付款状态为 Success、发票为 Paid，支付后通道余额分别为 4,900 和 100，待处理转账清空且通道仍可用；SUDT、xUDT 各完成该流程，无需修改托管配置、合约依赖或提高费率 | 只测试双方出资的缺陷场景，遗漏文档推荐的零出资接受路线，导致用户按说明仍无法完成首次 UDT 支付；或零出资接受仍误扣接受方资产、支付单位错误 | P0 |
| `FIB-UDT-01` | 用户在 OffCKB 双节点 devnet 中，以内置 account 19 通过 `udt issue` 发行默认资产标识的测试代币，再通过 `transfer` 分发给节点 1/2 的 account 3/4；分别使用 SUDT 和 xUDT 完成这条准备流程 | 发行、分发交易均 committed；两节点账户的 OffCKB 余额与链上 live cells 汇总一致，数量精确且资产类型、完整 type args 保持不变；资产的 type script 匹配两节点由 OffCKB 生成的对应 whitelist，双方仍有开通道所需的 CKB 容量 | 代币在 OffCKB 内能发行和转账，却因发行账户、资产标识或 whitelist 不匹配而无法用于 Fiber | P1 |
| `FIB-UDT-02` | 用户已将 account 19 发行的 SUDT 或 xUDT 分发给两个 FNN 账户，并为双方保留充足 CKB；节点 1 使用该资产的完整 `funding_udt_type_script` 开通道，节点 2 在默认配置下手工 `accept_channel` 并投入同类代币 | 节点 2 可查询待接受请求，并可用其 `temporary_channel_id` 完成接受；双方最终查询到同一 `ChannelReady` 通道，资产 type script 与投入数量正确，funding 交易在 devnet 上为 committed；无需修改 OffCKB 管理的 whitelist 或合约依赖 | 误把 CKB 自动接受规则用于 UDT 导致流程停滞，或生成配置缺少正确 UDT 依赖使通道无法上链 | P1 |
| `FIB-UDT-03` | 用户在 OffCKB 创建的 SUDT 或 xUDT 通道中已有充足可用余额且无待处理转账；节点 2 创建该资产 100 个最小整数单位的普通发票，节点 1 按发票完成直连支付 | 付款方 `get_payment.status = Success`，收款方 `get_invoice.status = Paid`；双方通道余额分别精确减少、增加 100 个同类代币单位，完整资产 type script 不变，待处理转账清空且通道仍可用 | 代币发票与通道资产不一致、误按 Shannon 缩放数量，或支付报告成功但代币未完成结算 | P1 |
| `FIB-UDT-04` | 用户已把同一种 SUDT 或 xUDT 分多笔转给两个 FNN 账户，使**每个账户的该资产分布在多个 cell**（每个账户合计 10,000，例如 4 个各 2,500），并为双方保留充足 CKB；节点 1 投入 5,000 开通道，节点 2 手工接受并投入 3,000 —— 双方都必须选中多个 UDT 输入，且双方都会产生找零 | 节点 2 可查询到待接受请求（`is_acceptor`、`NegotiatingFunding`、`remote_balance` 为 5,000、完整 type script 一致），并可用其 `temporary_channel_id` 完成接受；双方最终查询到同一通道并达到 `ChannelReady` 且无待处理转账，funding 交易在该 devnet 上为 committed；通道内双方余额分别为 5,000 和 3,000；双方账户在链上该资产的 live cells 汇总分别只减少 5,000 和 3,000，剩余部分（含各自找零）仍在自己锁下，资产 type script 不变 | 多 cell 下凑不足出资额，或接受方找零输出占用已有输出位置，使发起方按位置校验拒绝更新、通道无法开通、资金滞留在中间状态 | P1 |
| `FIB-UDT-05` | 同上的多 cell 布局，节点 1 仍投入 5,000，而节点 2 **投入其全部 10,000**（选中该资产全部输入、不产生接受方找零）；`funding_fee_rate` 显式设为 2000，以免默认费率下已提报的手续费问题掩盖本用例要判定的变量 | 双方达到 `ChannelReady` 且无待处理转账，funding 交易 committed；通道内双方余额分别为 5,000 和 10,000；节点 2 链上该资产清零、节点 1 汇总减少 5,000，资产 type script 不变。该用例是 `FIB-UDT-04` 的对照：只改变「接受方是否有找零」这一个变量（费率因素已排除），用于判断失败来自多输入选择还是找零落位 | 把「必须多选输入」与「接受方找零」两件事混在一起，导致修复或回归时无法判断失败原因；或接受方全额出资时输入选择与余额结算出错 | P1 |

## 判定与范围边界

- SUDT、xUDT 各完成一条发行到支付的代表性流程，使用 account 19 的默认 issuer lock hash 作为 type args。OffCKB 的 whitelist 对完整 args 精确匹配；不扩展自定义 xUDT args、其他发行账户或扩展脚本。私钥仅通过文件或指定环境变量传入。
- OffCKB 生成的 whitelist 未设置 `auto_accept_amount`，FNN 因此不会自动接受这些 UDT 通道；节点 2 查询待接受请求后调用 `accept_channel`，`funding_amount` 为同类代币数量。两节点另需 CKB 支付 cell 容量与链上费用，不能用 UDT 余额替代 CKB 资金准备。
- `FIB-UDT-06` 按 canary README 使用零出资接受；接受方仍提前持有该 UDT。既有双方出资用例继续作为进阶场景保留。正常主流程的预期保持成功，不因已有 issue 而预先接受任意失败；只有实际失败证据匹配已报告缺陷时，才按对话中约定归类。
- OffCKB CLI 数量使用十进制整数，FNN UDT 金额使用十六进制编码的同一整数，不乘 CKB 的 `100,000,000`。发票使用 `currency: Fibd` 和相同的 `udt_type_script`，省略 `payment_hash`、`payment_preimage`、`final_expiry_delta`；等待实际 funding 交易 committed、双方 `ChannelReady` 后再支付。
- 开通和支付完成均通过有截止时间的 RPC 轮询确认；余额以 `pending_tlcs` 为空时为基线。仅核验直连普通发票支付，不扩展多跳、MPP 或 UDT 协议边界矩阵。

依据：[OffCKB 默认 UDT 发行](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/sdk/ckb.ts#L442-L492)、[Fiber whitelist](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/fiber/scripts.ts#L134-L153)、[FNN UDT 自动接受条件](https://github.com/nervosnetwork/fiber/blob/e6cb7ac7770b1798a1ad5dfb9a8f4ae5db52036f/crates/fiber-lib/src/ckb/contracts.rs#L454-L460)、[手工接受通道参数](https://github.com/nervosnetwork/fiber/blob/e6cb7ac7770b1798a1ad5dfb9a8f4ae5db52036f/crates/fiber-json-types/src/channel.rs#L294-L308)、[UDT 发票参数](https://github.com/nervosnetwork/fiber/blob/e6cb7ac7770b1798a1ad5dfb9a8f4ae5db52036f/crates/fiber-json-types/src/invoice.rs#L116-L146)。
