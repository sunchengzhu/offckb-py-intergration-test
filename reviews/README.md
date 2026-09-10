# offckb 测试域地图

分析依据：本地 `develop` 分支提交 `44ab81d`，重点参考 `package.json`、`src/cli.ts`、`src/util/logger.ts`、`src/cmd/node.ts`、`src/cfg/setting.ts`、`README.md` 和 `.github/workflows/test.yml`。

本文件帮助评审者找到各测试域对应的评审文档，不记录批准状态或自动化状态。首期只覆盖最重要的本地、非交互式黑盒主流程。

首期实现顺序以真实用户价值闭环为准：devnet 启停与就绪、账户和 CKB 流转、SUDT/xUDT 生命周期、合约部署与 Type-ID 升级，然后再覆盖项目脚手架。RPC proxy 在首期仅作为 devnet 的必要依赖接受健康检查，不单独冻结其日志、交易缓存和异常转发细节。

| 测试域 | 负责行为 | 范围边界 | 入口 | 可观察结果 | 计划评审文档 |
| --- | --- | --- | --- | --- | --- |
| CLI 契约 | 验证构建并打包后的 CLI 可以安装、启动、拒绝非法输入，并保持机器输出契约稳定 | 只测试发布产物；不导入 `src/`，不检查帮助文案的排版细节 | `offckb --version`、`--help`、全局 `--json`、非法命令和参数 | 退出码；stdout 只有一个成功 JSON 对象；stderr 输出 NDJSON 进度或结构化失败；错误不重复 | `reviews/cli/command-contract.md` |
| Devnet 生命周期与状态 | 验证全新初始化、daemon 就绪、重启、停止、清理和基础持久化 | 每次使用独立用户目录和预先指定的本地 CKB 二进制；固定端口要求串行执行；不覆盖公共网络和 TTY | `node --daemon --binary-path`、`node stop`、`devnet info --json`、`clean`、`clean -d`、direct/proxy RPC | 配置、PID 和日志文件；direct/proxy RPC 健康；tip 持续增长且 Indexer 追平；重启前后 genesis 不变；停止后端口释放；两种清理边界正确 | `reviews/devnet/lifecycle-state.md` |
| 账户与 CKB 价值流转 | 验证内置开发账户、余额查询、充值、转账及真实链上效果 | 只覆盖本地纯 devnet；私钥通过文件或环境变量传入；不覆盖公共 faucet | `accounts --json`、`balance --no-udt --json`、`deposit --json`、`transfer --json`、`transfer-all --json` | 默认不暴露私钥；地址和初始资金正确；交易哈希有效并最终 committed；余额和 cell 按预期变化 | `reviews/accounts/ckb-value-flow.md` |
| UDT 生命周期 | 验证支持的 SUDT/xUDT 发行、查询、转移和销毁闭环 | 使用本地 devnet 内置脚本；不覆盖压力和资源上限场景 | `udt issue`、UDT 余额查询、UDT `transfer`、`udt destroy`，全部使用 JSON 模式 | 交易 committed；kind、type args 和 receiver 正确；转移前后代币数量守恒；销毁后余额正确减少 | `reviews/tokens/udt-lifecycle.md` |
| 合约部署 | 验证普通部署以及 Type-ID 创建和升级 | 使用小型、确定性的 devnet 二进制 fixture；不重新构建仓库内置 Rust/C 合约子模块 | `deploy --yes --target --output`、direct CKB RPC | 部署交易 committed；部署记录生成且与链上 cell/data hash 一致；升级保持 Type ID 并消费旧 cell | `reviews/contracts/deployment.md` |
| 项目脚手架 | 验证从非交互创建项目到构建、部署和生成 devnet 测试的核心用户旅程 | 首期只使用 TypeScript 模板和临时 npm prefix；不做全局安装 | `create --no-interactive --no-git --no-install -l typescript` 及生成项目的 package scripts | 必需文件齐全；依赖可在沙箱内安装；项目能够构建；合约能够部署；生成的 devnet 测试能够运行 | `reviews/projects/scaffolding.md` |
| RPC 代理与日志 | 后续验证基本转发、交易捕获和有限范围的诊断日志读取 | 首期只随 devnet 启动检查 proxy 健康；独立 proxy 语义与日志诊断延后 | Proxy JSON-RPC；`logs node\|miner\|rpc --tail --grep` | Proxy 与 direct RPC 结果一致；request/error/transaction 事件被记录；交易数据被缓存；过滤正确且控制字符不能注入日志 | `reviews/observability/rpc-proxy-logs.md` |

## 首期暂不覆盖

- 完整历史版本矩阵和完整 Mainnet/Testnet 数据库 fork 属于慢速或发布套件。
- CKB/debugger 下载、公共 RPC、testnet faucet 以及 npm/GitHub 可用性属于网络套件，不作为普通 PR 门禁。
- `status` 和全屏 `devnet config` 编辑器需要 PTY 冒烟测试，并保留人工视觉和键盘交互验收。
- Linux 功能门禁稳定后再加入 macOS、Windows 路径和进程兼容矩阵。
- RPC proxy 的独立转发、交易缓存、错误恢复和日志过滤用例放入后续可观测性批次；首期不把它们设为发布门禁。
- Jest 已负责的纯函数、模拟分支和 TUI 状态逻辑不在此重复。

本工程的评审文档路径：

- `reviews/<area>/<interface-or-behavior>.md`
