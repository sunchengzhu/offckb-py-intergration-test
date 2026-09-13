# 集成测试代码

在仓库根目录执行 `make prepare` 准备环境，`make test` 运行全部核心用例；聚焦单模块使用 `make test TESTS=tests/test_devnet_lifecycle.py`。本机路径和其他参数见 [运行配置](../config/README.md)。

这里存放带有 `TEST-MAP` 注释的集成测试。每条测试以中文说明用户目的，并用 CLI 结果、交易确认和必要的文件或进程观察核验结果。当前本地回归套件由以下部分组成：

- `conftest.py`：版本配置与产物校验、命令行参数、隔离 HOME、固定端口租约和 fixture。
- `harness.py`：CLI 执行、RPC 轮询、Indexer/cell oracle、daemon 精确归属与 teardown。
- `test_devnet_lifecycle.py`：空配置首次启动、实际 CKB 路径、ready、产块、Indexer 和 OffCKB 自行停止并清理 PID。
- `test_devnet_state.py`：已完成真实转账后停止并重启，验证配置和开发进度保留；对比 `clean -d` 与完整 `clean` 的数据、配置及真实调试缓存边界，同时保护全局设置、托管二进制和目录外文件。
- `test_default_node.py`：普通 `offckb node` 自行选择包默认版本的真实 CKB，初始化、提供连接地址、产块，Ctrl+C 后退出并可再次启动。
- `test_global_settings.py`：通过 CLI 选择非默认 CKB 版本，核对持久化及新进程读取，再让 OffCKB 自行选择托管二进制启动 node/miner，保留其他设置。
- `test_devnet_configuration.py`：将日志级别按 `warn → info → warn` 调整并正常重启，只核对本轮新增日志，同时验证原交易保留、链继续产块。
- `test_cli_contract.py`：复用已安装的 CLI，核对 `offckb --version` 与该安装包的 `package.json` 一致。
- `test_ckb_value_flow.py`：找到预充值账户、查看资产、给初始余额为零的新地址充值，以及通过所选账户转账；充值和转账后同时核对 CLI 余额与链上结果。
- `test_udt_lifecycle.py`：通过非默认账户发行、转账和部分销毁 SUDT/xUDT，每一步比较 CLI 余额与链上 live cells；销毁前自行给另一持有者准备非零代币，单独运行也能验证其余额不受影响；自定义 xUDT args 区别于默认值。
- `asset_assertions.py`：用 direct RPC 独立汇总余额，再核对 OffCKB 的资产发现、分类和过滤结果。
- `test_contract_deployment.py`：使用非默认账户完成普通部署、Type-ID 首次部署与升级，核对记录和 code cell 的实际归属，升级时保留旧 migration 并新增记录。
- `test_project_scaffolding.py`：默认创建与自动安装、自定义路径和名称、已有项目保护，以及生成项目原有脚本的构建、部署和 mock/devnet 示例调用；部署和调用均等待链上确认，调用交易必须引用本次部署。
- `project_support.py`：项目用例共用的隔离工具准备、项目创建和原有脚本执行。
- `diagnostic_support.py`：创建用户合约并输出唯一标识，使用原有脚本构建、部署，再通过公共 SDK 签名并经 OffCKB proxy 提交真实调用。
- `test_transaction_debugging.py`：合约调用因预期错误被拒绝后，仅给出交易哈希调用 OffCKB debugger；核对脚本标识、失败结果和 OffCKB 自行补齐的真实输入与依赖。
- `test_logs.py`：后台运行时查找本次合约输出，并核对 node/script/miner/rpc 的真实日志来源、文本筛选和行数限制。
- `test_system_scripts.py`：核对 OffCKB 展示的系统脚本与本地链一致，再使用导出的账户锁及依赖签名、消费真实输入，等待交易确认。

自动化行为应通过附近的 `TEST-MAP: <CASE-ID>` 注释映射到评审用例。

`core` 表示当前本地 devnet 回归集合，P0/P1 表示评审文档中的场景优先级，两者不等同。已有升级、部分销毁等 P1 回归继续随 `make test` 执行；新增用例按用户流程补齐，不因降为 P1 而删除已有有效检查。每条用例自己准备必要状态，不依赖其他测试先运行。

资产和部署 CLI 用例通过 JSON 获取结果；前台默认启动、项目创建和失败交易调试使用普通文本入口，日志以文本查看为主并补充 JSON 等价读取。生成项目的脚本直接运行，测试不改写 build/deploy/test 文件；诊断用例只修改用户合约，产生可识别的成功或失败调用。项目构建、部署 fixture 可以复用准备结果；聚焦任意一条测试时，也会自动准备所需项目和链上状态。

`project` marker 包括项目创建、失败交易调试和日志用例，均需预先准备原生 `CKB_DEBUGGER_BIN`；涉及构建和调用时还要安装生成项目的依赖。系统脚本用例不需要创建项目或 debugger。默认前台启动用例使用与包默认版本一致的 `DEFAULT_CKB_BIN`。项目创建后由 OffCKB 自行配置 debugger 的 PATH 入口，测试不代替产品写 debugger shim。

项目依赖默认通过 pnpm 离线缓存安装。第一次缺少缓存时，可显式执行 `make test TESTS=tests/test_project_scaffolding.py ARGS='--project-online'` 允许下载；这些测试项会标为 `network`。随后正常 `make test` 保持项目依赖离线安装；不增加新的 Make 目标，也不自动跳过缺依赖的用例。`make prepare` 准备的 OffCKB 包依赖与生成项目的依赖不完全相同。

聚焦“使用与排查”流程：

```bash
make test TESTS='tests/test_transaction_debugging.py tests/test_logs.py tests/test_system_scripts.py'
```

`TESTS` 也可只保留一个模块；缺少项目依赖缓存时，追加 `ARGS='--project-online'` 显式允许下载。SDK 交易辅助程序见 [fixtures 说明](../fixtures/README.md)。

聚焦“继续开发与重置环境”：`make test TESTS=tests/test_devnet_state.py`。三条用例各自准备独立环境、通过 CLI 保存非默认配置并完成转账；不需要项目构建或 debugger，也不依赖其他测试先运行。

聚焦“配置生效与版本入口”：`make test TESTS='tests/test_global_settings.py tests/test_devnet_configuration.py tests/test_cli_contract.py'`。版本选择用例需要两个真实本地 CKB：`CKB_BIN` 与包默认版本不同，`DEFAULT_CKB_BIN` 与包默认版本一致；两者都放入隔离托管目录，验证实际选中的二进制。日志配置用例不需要项目构建或 debugger。

`make prepare` 根据 `config/offckb.toml` 下载发布包或从所选源码构建包；测试安装该包后执行 `offckb --version`，核对包内版本，并显示实际版本与来源。`scripts/test_offckb_target.py` 是运行设施的版本选择、发布包校验和版本命令回归检查，使用本地 Git 仓库、模拟 npm 响应和临时 CLI 替身，通过 `.venv/bin/python -m unittest scripts.test_offckb_target` 执行，不属于 OffCKB 产品评审用例或其覆盖率。

测试不得依赖固定 sleep。成功交易必须等到 `committed`，依赖 Indexer 的后续操作再等待其追到提交块；资产数量由 live cells 独立核算。故意失败的合约交易可能在提交时就被拒绝、无法查询交易状态，此时核对明确的合约错误、proxy 保存的实际交易与 debugger 诊断，不能把任意 RPC 或网络错误当作预期失败。

显式二进制首次启动用例不预写 OffCKB settings、不提供托管 CKB；默认前台用例同样不预写 settings，只预备包默认版本的托管二进制。其他业务用例预置本地托管二进制，避免辅助命令下载 CKB。均不设置 `OFFCKB_CLI_PATH`；项目通过隔离 PATH 调用同一个已安装 CLI。停止失败时仍执行限定归属的兜底清理，但保留失败结果。

运行 `.venv/bin/python -m unittest scripts.test_acceptance_oracles` 可检查测试设施本身：故意留下 PID、让 CLI 漏报资产或忽略过滤参数、让部署记录与链上一起归属错误账户时，断言必须失败。这些替身检查不计入产品自动化覆盖率。

运行 `.venv/bin/python -m unittest scripts.test_process_runner` 可检查普通文本/JSON 命令执行、敏感信息脱敏，以及脚本超时后其子进程的回收；这些也不计入产品覆盖率。
