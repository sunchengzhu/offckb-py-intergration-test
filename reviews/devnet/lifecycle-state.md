# Devnet 生命周期与状态用例评审

评审范围：隔离环境中的 devnet daemon 初始化、就绪、产块、停止、重启和清理
源码版本：`develop@44ab81d`

## 接口说明

- 接口作用：使用指定的本地 CKB 二进制管理一套可用、可停止且状态边界明确的开发链。
- 输入：`node --daemon --binary-path`、`node stop`、`devnet info`、`clean` 和 `clean -d`。
- 成功结果：node、miner、Indexer 和 RPC proxy 按阶段就绪；PID、端口、配置及链数据与命令结果一致。
- 失败结果：明确非零退出，不误报 ready，不遗留本次启动的后台进程或运行中 PID。
- 不负责：公共网络、完整 fork 流程、动态端口、并行 devnet 和全屏状态界面。

## 待评审用例

| 用例 | 场景 | 预期结果 | 防止的问题 | 优先级 |
| --- | --- | --- | --- | --- |
| `NODE-01` | 在没有 OffCKB 配置或托管二进制的全新隔离用户目录中，通过安装后的 CLI 执行 `node --daemon --binary-path`，不设置 `OFFCKB_CLI_PATH` | OffCKB 自行生成 node、miner 与 chain spec 配置；所属 node 和 miner 进程使用传入的本地 CKB 路径；命令只在 direct RPC 与 proxy RPC 都通过真实节点健康检查后返回；JSON 包含 daemon PID、RPC、proxy、日志和 PID 文件位置，PID 状态为 running | 首次启动依赖测试预写配置、打包入口无法自启动、忽略 `--binary-path`，或端口刚打开就误报 ready | P0 |
| `NODE-02` | daemon 启动完成后持续观察节点、miner 和 Indexer | tip 在截止时间内增长；Indexer 最终追平节点；`devnet info --json` 报告 `kind: pure-devnet`、`ready: true`、`indexerReady: true` 和 `indexerLag: "0"`，且报告的 RPC 地址与实际一致 | RPC 能连接但 miner 不产块，或余额与 cell 查询建立在落后的 Indexer 上 | P0 |
| `NODE-03` | 同一隔离目录中已有健康 offckb daemon 时再次执行 daemon 启动 | 第二次启动明确非零失败，不覆盖原 PID 元数据、不启动第二组组件；原节点继续响应并产块 | 重复实例争抢数据库和固定端口，或新启动破坏已有服务 | P1 |
| `NODE-04` | 对健康运行的 daemon 执行 `node stop`，在任何测试兜底清理前观察结果 | 返回 `stopped: true` 及原 daemon PID；OffCKB 自行终止 node、miner、proxy 整个服务组，关闭相关监听端口并删除 PID 文件 | 只停止外层 CLI 而遗留组件或 PID 元数据，测试兜底清理掩盖停止失败 | P0 |
| `NODE-05` | daemon 已停止或从未启动时再次执行 `node stop` | 命令幂等地以 0 退出并返回 `stopped: false` 和明确原因；过期 PID 元数据被清理，且不向无关进程发送信号 | teardown 因重复停止失败，或 stale PID 导致错误杀进程 | P1 |
| `NODE-06` | 先产生一笔已提交交易，再停止 daemon 并使用同一目录重新启动 | genesis hash 保持不变，旧交易仍能从链上查询，重启后的 tip 不重建为另一条链并继续增长 | 普通重启静默重置链或丢失开发状态 | P0 |
| `NODE-07` | 停止节点后执行 `clean -d`，再使用原配置启动 | chain data 被删除而 `ckb.toml`、miner 配置和 chain spec 保留；新链使用相同 genesis 从干净状态开始，清理前的交易不再属于链上状态 | 用户只想重置链数据时配置也被删除，或旧数据库未真正清空 | P1 |
| `NODE-08` | 停止节点后执行完整 `clean`，再重新启动 | 整个 devnet 根目录（配置、链数据、fork 状态和交易调试缓存）被删除；再次启动从随包默认 devnet 文件重新初始化，旧交易和原文件改动均不存在 | 完整清理残留旧状态，或下一次启动继续使用损坏配置 | P1 |
| `NODE-09` | 使用不存在、不可执行或启动即退出的 `--binary-path` 启动 daemon | 命令明确非零失败并给出可操作错误；miner 和 proxy 不启动，不留下 running PID、本次 daemon 进程或被占用端口 | 无效二进制被报告为成功，或失败启动污染后续运行 | P1 |

## 本轮需要确认

- 清理类用例都先正常停止 daemon；“运行中执行 clean”留待单独的安全行为定义，不在首期尝试性删除文件。
- `clean -d` 首期只冻结“保留配置、重建链数据”的公开语义，不冻结 sibling 交易调试缓存是否保留。
- 请确认 `NODE-01` 至 `NODE-09` 是否构成首期 devnet 验收范围。
