# Fiber 停止、继续开发与清理用例评审

评审范围：用户暂停后继续通道实验，或按预期范围重置本地环境。版本沿用 [Fiber 测试分析](README.md)，使用同版本 FNN `0.9.0`。

## 接口说明

- `node --fiber` 联合管理 CKB、miner、proxy 和 FNN；`fiber start` 只管理附加在已有链上的 FNN。前台使用 Ctrl+C；后台通过对应停止命令管理。
- 正常重启使用已完成支付、无待处理转账的通道。清理使用所有通道均已协作关闭、关闭交易 committed 且资金已返还的环境；这是测试前置条件，清理命令不会代替用户关闭通道或结算资金。
- 所有命令和文件检查仅针对隔离测试目录。退出、端口释放、链上交易和支付结果使用有截止时间的轮询。

## 待评审用例

| 用例 | 场景 | 预期结果 | 防止的问题 | 优先级 |
| --- | --- | --- | --- | --- |
| `FIB-LIFE-01` | 用户通过前台 `offckb node --fiber` 启动完整环境，完成支付并等待转账结算后按 Ctrl+C | 管理进程、CKB、miner、proxy 和全部 FNN 均退出，所属端口释放、运行元数据删除；CKB 链数据、FNN 通道数据及身份配置保留 | 退出终端后仍有节点占用端口，或正常停止丢失通道实验数据 | P0 |
| `FIB-LIFE-02` | 用户通过 `offckb node --fiber --daemon` 启动完整环境；分别使用 `offckb --json node stop` 和 `offckb --json fiber stop` 结束后台运行 | 两种入口均停止整个联合环境并以 0 退出，stdout 为一个成功 JSON；`fiber stop` 明确包含 CKB；所属进程和端口释放、运行元数据删除，链与通道数据保留 | 停止命令遗漏 FNN，或用户以为联合环境中的 `fiber stop` 只停止 FNN | P0 |
| `FIB-LIFE-03` | 用户在运行中的 CKB 上单独启动 FNN；前台先执行 `fiber stop`，再按 Ctrl+C，后台则执行 `offckb --json fiber stop` | 前台的 `fiber stop` 提示到所属终端停止且不终止服务；Ctrl+C 或后台停止只退出 FNN 及其管理进程，释放 FNN 端口并删除运行元数据；原 CKB、miner、proxy 保持运行和产块，旧交易仍为 committed | 单独停止 Fiber 时连带停止已有开发链，或错误处理前台管理进程 | P0 |
| `FIB-LIFE-04` | 用户在双节点 CKB 通道完成一次支付且无待处理转账后正常停止，再以同一入口、同一 FNN 版本和原数据目录重新启动；分别使用联合管理和独立 FNN 管理 | 原节点身份、CKB 资金账户、最终通道 ID 及结算余额保留；节点自动互联，原通道恢复 `ChannelReady`；通过该通道再次支付得到 `Success`／`Paid`，双方余额按支付额变化且无待处理转账 | 重启表面成功却创建新身份、丢失原通道，或原通道无法继续支付 | P0 |
| `FIB-LIFE-05` | CKB 由 daemon 运行，FNN 由另一个前台或后台管理进程运行；用户先执行普通 `node stop`，再明确执行 `node stop --force` | 普通停止非零退出并指导先停止 FNN，原环境保持可用；明确强制停止时提示 FNN 将留在已停止的链上，只停止 CKB、miner、proxy，FNN 仍运行且数据保留 | 默认停止使独立 FNN 失去链服务，或强制停止越过管理范围终止其他服务 | P1 |
| `FIB-LIFE-06` | 所有通道已关闭并完成资金返还，用户停止 FNN、保持原 CKB 运行，执行 `offckb --json fiber clean --data --yes` 后重新启动 FNN | 仅 FNN store 被删除；节点清单、配置、账户密钥、身份密钥、密码及日志保留，原 CKB 进程和已确认交易不变；FNN 重启沿用原身份、不保留旧通道或支付记录，能够重新开通道并支付 | 仅重置 Fiber 数据却误删身份或开发链，或重建后无法继续实验 | P0 |
| `FIB-LIFE-07` | 所有通道已关闭并完成资金返还，用户停止 FNN、保持原 CKB 运行，执行 `offckb --json fiber clean --yes` 后重新启动 FNN | 整个 Fiber 环境被删除，已下载 FNN 和 CKB 配置、链数据保留；原 CKB 进程及已确认交易不变；重建生成新的 Fiber 网络身份，仍按节点编号分配内置 CKB 账户，能够重新开通道并支付 | 全量清理 Fiber 时误删 CKB 实验，或重建错误复用旧网络身份和通道记录 | P0 |
| `FIB-LIFE-08` | 所有通道已关闭并完成资金返还，用户停止 CKB 和全部 FNN，执行 `offckb --json clean -d` 后重新联合启动 | CKB 链数据及全部 FNN store 删除；CKB 配置与 spec、Fiber 节点配置及密钥保留；新链不含旧实验交易，FNN 沿用原身份但无旧通道记录，能够重新开通道并支付 | 开发链已重置而 FNN 仍使用旧通道数据库，或数据清理误删用户配置 | P0 |
| `FIB-LIFE-09` | 所有通道已关闭并完成资金返还，用户停止 CKB 和全部 FNN，执行 `offckb --json clean` 后重新联合启动 | 整个 devnet 目录及其中的 Fiber 配置、密钥、store 和日志被删除，目录外工具缓存保留；重新生成可用链与新的 Fiber 网络身份，旧实验交易和通道不再存在，能够重新开通道并支付 | 全量重置残留旧 Fiber 状态导致新链不可用，或越界删除已安装工具 | P0 |
| `FIB-LIFE-10` | 用户在 FNN 仍由联合管理或独立管理运行时，分别尝试 `fiber clean --data --yes`、`fiber clean --yes`、`clean -d` 和 `clean` | 四种清理均非零退出并给出先停止服务的提示；运行中的链、通道数据、身份和配置未被删除，原服务及通道仍可完成支付；正常停止后可执行对应清理 | 运行中的 FNN 数据库被删除，造成通道实验损坏或资金状态丢失 | P0 |

## 判定与范围边界

- 停止只表示进程退出；以原通道恢复和再次支付证明数据可继续使用。保留身份通过 `node_info` 的公开身份和资金账户判断，密钥文件只比较是否保留，不输出内容。
- 清理后的“旧实验交易不存在”通过 CKB RPC 查询专门创建且不会由初始化重建的交易判断；不能仅用 genesis 或块高判断新旧链。删除 FNN store 不要求链上已经结算的旧交易消失。
- FNN 单独停止和运行中清理保护涵盖前台、daemon；不扩展为进程崩溃、PID 复用、数据库锁实现或 FNN 跨版本迁移矩阵。交互确认和取消另见安装与交互领域评审。

依据：[OffCKB 联合启动与停止](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/cmd/node.ts)、[Fiber 停止的管理范围](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/fiber/daemon.ts)、[Fiber 清理](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/fiber/clean.ts)、[devnet 清理](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/cmd/clean.ts)、[Fiber 目录与账户分配](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/fiber/paths.ts)。
