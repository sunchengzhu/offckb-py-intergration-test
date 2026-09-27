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
| `FIB-ERR-08` | 用户上一次实验的 Fiber daemon 被强制结束（断电、`kill -9` 或上次运行残留），环境中仍留着它的 PID 记录，而该 PID 已被系统复用给一个无关进程；用户随后在已有 CKB 上执行 `fiber start --daemon`，再按提示处理该记录后重试 | 首次非零退出，stdout 为空，stderr 恰有一条 `ok: false` 的结构化失败，指出该 PID 记录指向的进程不是 OffCKB Fiber daemon，并给出可操作的下一步（该记录的位置或应如何处理占用该 PID 的进程）；任何情况下都不向身份未确认的进程发送信号，该进程状态与启动时间不变；不启动任何 FNN、不占用节点端口；按提示处理后无需清空环境即可启动成功并自动互联，原有 CKB 链、节点身份、账户与已有链数据不变；只要无法确认原管理进程是否已经退出，就不得替换或删除该记录，命令必须失败关闭并给出可操作的提示 | 用户被一条无法自助解决的错误永久挡住启动；或为了自愈覆盖仍属于运行中 daemon 的记录、误杀无关进程 | P1 |
| `FIB-ERR-09` | 用户在一个终端执行 `fiber start --daemon`（或 `node --fiber --daemon`）并在它报告就绪之前，在另一个终端对同一环境再次执行同一条启动命令；另在前台 `fiber start` 仍占用环境期间执行 `fiber start --daemon` | 第二次启动非零退出，stdout 为空，stderr 恰有一条 `ok: false`，明确指出已有一个启动正在进行并给出其 PID，或指出环境正被另一个 OffCKB 进程占用并说明其用途；不启动 FNN、不占用节点端口、不改写节点清单与配置、不替换进行中启动的 PID 记录；先发起的启动不受影响，在截止时间内就绪并自动互联，随后可开通道支付；daemon 入口的失败信息（含所指向的日志）必须能区分出“环境被另一个进程占用”，不得只剩“启动未就绪/请查看日志”这类通用原因 | 两个启动互相覆盖记录或抢占端口导致同时失败；或用户只看到笼统的未就绪提示而反复重试 | P1 |
| `FIB-ERR-10` | 用户正常停止已就绪的双节点环境后，节点 1 的 `ckb/key` 或 `password` 单独缺失；分别通过 `node --fiber --daemon` 和已有链上的 `fiber start --daemon` 重启，失败后恢复原文件并重试 | 首次非零退出，结构化错误或所指日志明确指出节点 1、缺失文件及恢复指引；不重新生成缺失密钥或密码，不改写仍存在的密钥、身份和节点清单，不删除链或 FNN store；本次组件被回收，原有独立 CKB 不受影响；恢复原文件后同一目录、同版本可正常启动，公开身份和资金账户不变，节点自动互联 | 缺失单个密钥文件时静默生成新材料，破坏已有账户或身份，或可恢复的文件问题迫使用户清空实验 | P1 |

## 判定与范围边界

- 错误场景包含 JSON 入口：stdout 为空，stderr 包含一条 `ok: false` 的结构化失败记录，诊断和进度同样通过 stderr 输出 NDJSON；daemon 可以指向所属日志，但该日志必须能定位具体原因。
- `node --fiber` 失败应回收本次创建的 CKB、miner、proxy 和 FNN；`fiber start` 失败只回收本次 FNN。通过进程、端口、公开 RPC 和已确认交易检查归属及数据保留。
- 恢复后至少验证 CKB 持续产块、FNN RPC 就绪及预期节点自动互联；资金恢复场景额外验证通道达到 `ChannelReady` 且 funding 交易 committed。使用截止时间轮询，不用固定等待代替结果。
- fork 拒绝场景只验证隔离目录中的入口保护，不下载完整公共链数据库；公共网络参数应在连接网络前被拒绝。测试不操作开发者真实环境，不移除 fork 标识来伪装普通链。
- 密钥保护只覆盖原配对材料中单个文件缺失；恢复使用测试保存的原文件，不打印私钥或密码。不扩展为加密格式、损坏字节、权限或数据库修复矩阵。

依据：[已有链就绪要求与 fork 保护](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/fiber/ckb-env.ts)、[端口、余额与失败回收](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/fiber/manager.ts)、[联合启动边界](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/cmd/node.ts)、[独立 daemon](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/fiber/daemon.ts)、[产品测试锁定“拒绝替换身份不明的存活 PID 元数据”](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/tests/fiber-lifecycle.test.ts)、[运行记录与锁的 fail-closed 规则](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/fiber/runtime.ts)。
