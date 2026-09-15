# Fiber 启动到首次 CKB 支付用例评审

评审范围：使用 OffCKB 启动默认双节点 Fiber 环境，开通一个普通 CKB 通道并完成首次支付。

版本依据：OffCKB `0.5.0-canary-ee0ad6b`（`ee0ad6b`），默认 CKB `0.208.0`、FNN `0.9.0`（`e6cb7ac`）。

## 接口说明

- 一键启动：`offckb node --fiber`，可加 `--daemon`；已有本地链：`offckb fiber start`，可加 `--daemon`。
- 默认环境：两个 FNN 分别使用内置 account 3/4，RPC 为 `21714/21715`，具有不同网络身份，并自动连接到对方。通过公开 RPC 核对实际版本、`default_funding_lock_script`、资金和与 CKB genesis 一致的 `chain_hash`。
- 核心场景在隔离目录预备默认版本的真实工具及其随附文件；由 OffCKB 自行生成配置、选择二进制并管理进程。开通道和支付均通过 FNN 公开 RPC 完成。

## 待评审用例

| 用例 | 场景 | 预期结果 | 防止的问题 | 优先级 |
| --- | --- | --- | --- | --- |
| `FIB-01` | 用户首次使用 Fiber，在已预备默认工具的全新用户环境执行普通 `offckb node --fiber`，不指定版本、二进制路径、节点数量或 daemon | OffCKB 自动初始化并在前台运行 CKB、miner、proxy 和两个 FNN，显示可用连接地址；CKB 可查询并持续产块，两个 FNN 使用默认版本及各自预充值账户，连接同一 devnet 并自动互联 | 默认上手命令依赖额外配置才能使用，或只启动了 CKB，FNN 的版本、资金账户或网络配置不可用 | P0 |
| `FIB-02` | 用户希望在后台运行完整环境，在全新用户环境执行 `offckb --json node --fiber --daemon` | 命令在 CKB、proxy 和两个 FNN 均就绪且已互联后以 0 退出，stdout 只有一个成功结果 JSON，包含 `daemon: true` 和真实连接信息；返回后默认版本的服务继续运行、产块，FNN 的链和资金账户正确 | 后台启动只等 CKB 就误报整体成功，或启动命令结束后 FNN 随之退出，脚本无法继续使用 | P0 |
| `FIB-03` | 用户已用同版本 OffCKB 启动普通本地 CKB 并完成一笔链上交易，希望增加 Fiber；分别以前台和 `--daemon --json` 方式执行 `offckb fiber start` | 在现有链上启动两个使用默认版本及预充值账户的 FNN，并自动互联；原 CKB、miner、proxy 进程保持运行，genesis 不变、旧交易仍为 committed，链继续产块；前台持续管理 FNN，daemon 模式在 FNN 就绪后返回单个成功 JSON 并以 0 退出 | 增加 Fiber 时重启或替换已有开发链、丢失实验数据，或 FNN 实际连接了另一条链 | P0 |
| `FIB-04` | 用户通过 OffCKB 启动默认双节点环境后，由节点 1 向已自动连接的节点 2 调用 `open_channel`，投入 10,000 CKB 开通普通 CKB 通道，保持默认接受配置 | 节点 2 自动接受；双方可查询到同一最终通道，均达到 `ChannelReady`；通道 funding 交易在该 devnet 上为 committed，待处理转账为空，节点 1 可用余额足以进行 100 CKB 支付 | FNN 能启动和互联，但 OffCKB 配置的资金、合约依赖或链参数使通道无法真正使用 | P0 |
| `FIB-05` | 用户在 OffCKB 默认双节点环境中已有可用的普通 CKB 通道，节点 1 可用余额充足且无待处理转账；节点 2 创建 100 CKB 普通发票，节点 1 按发票完成一次直连支付 | 付款方 `get_payment.status = Success`，收款方 `get_invoice.status = Paid`；结算后付款方本地通道余额精确减少 100 CKB、收款方精确增加 100 CKB，双方无待处理转账且通道仍可用 | 发票或支付 RPC 返回成功信息，但资产未实际转移，或金额单位错误、转账一直未结算 | P0 |

## 判定与范围边界

- 开通道和首次支付在联合启动、已有链上启动两类入口中均验证，并覆盖前台与 daemon；使用各入口实际生成的环境，不由测试补调 `connect_peer`、替换合约或修改配置来修复启动结果。
- 开通返回的 `temporary_channel_id` 不代表完成。以双方 `list_channels` 中的最终 `channel_id` 和 `state.state_name = ChannelReady` 判断可用，并从 `channel_outpoint` 取得 funding 交易核验 committed；不得只等交易首次上链就开始支付。
- 发票使用 devnet 币种 `Fibd`，RPC 金额为十六进制 Shannon（1 CKB = 100,000,000 Shannon），省略 `payment_hash`、`payment_preimage` 和 `final_expiry_delta`，使用普通发票及默认时限。余额以开通完成、`pending_tlcs` 为空时为基线，支付后继续轮询至双方余额和待处理转账都完成结算；不要求初始可用余额等于投入额，也不把 `payment_hash` 当作链上交易哈希。
- 所有就绪、交易和支付结果均使用有截止时间的轮询。正常退出、重启与清理见 [生命周期评审](lifecycle-state.md)；执行仍须按根规范回收测试所属进程和端口。[协作关闭通道与资金返还](channel-close.md) 同属首批 P0。

依据：[OffCKB 启动、账户与自动互联](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/fiber/manager.ts)、[联合启动与 daemon](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/cmd/node.ts)、[已有链上的 Fiber 启动](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/cmd/fiber.ts)、[FNN 默认通道接受配置](https://github.com/nervosnetwork/fiber/blob/e6cb7ac7770b1798a1ad5dfb9a8f4ae5db52036f/crates/fiber-lib/src/fiber/config.rs)、[FNN RPC](https://github.com/nervosnetwork/fiber/blob/e6cb7ac7770b1798a1ad5dfb9a8f4ae5db52036f/crates/fiber-lib/src/rpc/README.md)。
