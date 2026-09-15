# Fiber 启动失败与恢复用例评审

评审范围：用户遇到常见启动问题时能定位原因，保留已有服务与数据，并在修正后继续使用原隔离环境。

版本依据：OffCKB `0.5.0-canary-ee0ad6b`，默认 CKB `0.208.0`、FNN `0.9.0`。

## 待评审用例

| 用例 | 场景 | 预期结果 | 防止的问题 | 优先级 |
| --- | --- | --- | --- | --- |
| `FIB-ERR-01` | 用户通过 `node --fiber --fnn-binary-path` 或已有链上的 `fiber start --binary-path` 指向不存在的路径或不可执行文件，失败后改为真实 FNN 路径重试 | 非零退出，错误或所指日志能定位路径、启动失败节点；本次启动的组件退出、占用端口释放，原有独立 CKB 保持可用；修正路径后无需清空环境即可启动并自动互联 | 错误路径留下半套服务、阻塞重试，或失败清理误停用户原有链 | P1 |
| `FIB-ERR-02` | 用户启动 Fiber 时，另一个由测试隔离创建的进程已占用节点 1 的 RPC `21714` 或 P2P `8344`；释放冲突端口后原命令重试 | 非零退出并指出节点、端口及冲突原因；不终止占用端口的进程，不中断原有 CKB；本次联合启动的 CKB/miner/proxy 被回收；解除冲突后节点正常启动并互联 | 为释放端口误杀其他程序，或失败残留组件导致每次重试仍冲突 | P1 |
| `FIB-ERR-03` | 用户对已运行的独立 Fiber 环境再次执行 `fiber start` 或 `fiber start --daemon`；另在已有 CKB 上误用前台 `node --fiber` | 重复启动非零退出并指导先停止现有 Fiber；已有 CKB 上的联合启动明确提示改用 `fiber start`；原有进程、节点身份与链数据不变，已有通道仍能支付；按提示使用正确入口后可继续 | 重复启动替换管理记录、抢占服务或重置已有实验 | P1 |
| `FIB-ERR-04` | 用户尚未启动本地 CKB，或仅运行节点但 miner 未工作时执行 `fiber start`；随后按提示启动完整 CKB 环境再试 | 非零退出，分别说明 CKB RPC 不可用或链未产块，提供启动或日志检查指引；不自行新建、替换或停止已有 CKB；恢复产块后在同一环境可启动 Fiber 并互联 | 仅有 RPC 响应就把不能完成链上交易的环境判为可用，或增加 Fiber 时重建用户链 | P1 |
| `FIB-ERR-05` | 用户在独立本地链上已将节点 1 对应 account 3 的可用 CKB 转走，确认交易 committed 且无可用余额后启动 Fiber；补充该账户资金并确认上链后重试 | 首次非零退出并说明节点及账户缺少可用 CKB；本次 FNN 全部退出、原 CKB 继续产块；补充资金被链和 indexer 确认后可直接重试并成功开通 CKB 通道 | 已耗尽资金的环境仍提示就绪，用户直到开通道时才发现不可用，或恢复要求清空已有链 | P1 |
| `FIB-ERR-06` | 用户执行 `node --fiber --network testnet/mainnet`，或在已有 fork 标识的隔离 devnet 执行 `node --fiber`、`fiber start`，包括 daemon 入口 | 非零退出并说明 Fiber 仅支持普通本地 devnet；不启动 Fiber、不接管公共网络进程，不删除或改写原 fork 数据；按提示另建普通本地环境后可正常启动 | 把公共网络或 fork 链当成本地 Fiber 实验环境，错误初始化或破坏原数据 | P1 |
| `FIB-ERR-07` | 用户通过 `node --fiber --fiber-nodes` 或 `fiber start --nodes` 输入 0、17 或 1.5，随后改用有效数量重试 | 非零退出并说明数量必须是 1–16 的整数；JSON 模式给出结构化参数错误，不启动服务、不改写已有节点配置；改为有效数量后能正常启动 | 非法数量仍创建部分节点、越过可用账户范围，或错误输入破坏原配置 | P1 |

## 判定与范围边界

- 错误场景包含 JSON 入口：stdout 为空，stderr 包含一条 `ok: false` 的结构化失败记录，诊断和进度同样通过 stderr 输出 NDJSON；daemon 可以指向所属日志，但该日志必须能定位具体原因。
- `node --fiber` 失败应回收本次创建的 CKB、miner、proxy 和 FNN；`fiber start` 失败只回收本次 FNN。通过进程、端口、公开 RPC 和已确认交易检查归属及数据保留。
- 恢复后至少验证 CKB 持续产块、FNN RPC 就绪及预期节点自动互联；资金恢复场景额外验证通道达到 `ChannelReady` 且 funding 交易 committed。使用截止时间轮询，不用固定等待代替结果。
- fork 拒绝场景只验证隔离目录中的入口保护，不下载完整公共链数据库；公共网络参数应在连接网络前被拒绝。测试不操作开发者真实环境，不移除 fork 标识来伪装普通链。

依据：[已有链就绪要求与 fork 保护](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/fiber/ckb-env.ts)、[端口、余额与失败回收](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/fiber/manager.ts)、[联合启动边界](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/cmd/node.ts)、[独立 daemon](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/fiber/daemon.ts)。
