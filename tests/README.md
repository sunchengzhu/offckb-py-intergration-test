# 集成测试代码

在仓库根目录执行 `make prepare` 准备环境，`make test` 运行全部核心用例；聚焦单模块使用 `make test TESTS=tests/test_devnet_lifecycle.py`。本机路径和其他参数见 [运行配置](../config/README.md)。

这里存放带有 `TEST-MAP` 注释的集成测试。每条测试以中文说明用户目的，并用 CLI 结果、交易确认和必要的文件或进程观察核验结果。当前本地回归套件由以下部分组成：

- `conftest.py`：版本配置与产物校验、命令行参数、隔离 HOME、固定端口租约和 fixture。
- `harness.py`：CLI 执行、RPC 轮询、Indexer/cell oracle、daemon 精确归属与 teardown。
- [fiber/](fiber/README.md)：Fiber 启动、开通道、支付与协作关闭用例，以及专属环境和 RPC 辅助代码。
- `test_devnet_lifecycle.py`：空配置首次启动、实际 CKB 路径、ready、产块、Indexer 和 OffCKB 自行停止并清理 PID。
- `test_node_recovery.py`：重复启动保留原开发链；错误二进制路径与真实端口冲突失败后，检查产品自行清理、冲突服务不受影响，修正后能在同一环境正常启动。
- `test_node_stop_safety.py`：重复停止、已退出的 PID 和指向无关进程的 PID，检查停止结果、元数据及测试自建进程的响应和信号记录。
- `test_devnet_state.py`：已完成真实转账后停止并重启，验证配置和开发进度保留；对比 `clean -d` 与完整 `clean` 的清理边界，验证运行中拒绝清理，同时保护配置、全局设置、托管二进制和目录外文件。
- `test_default_node.py`：普通 `offckb node` 自行选择包默认版本的真实 CKB，初始化、提供连接地址、产块，Ctrl+C 后退出并可再次启动。
- `test_global_settings.py`：查看默认设置、管理代理、拒绝错误输入、隔离两套用户环境；配置文件损坏或读写失败时保护原设置；选择非默认 CKB 版本后验证新进程读取及实际 node/miner 二进制。
- `test_devnet_configuration.py`：批量保存选项的值和类型，拒绝非法输入、缺失或损坏文件及非终端交互；核对配置和链数据保护，并通过 `warn → info → warn` 重启验证日志生效及原交易保留。
- `test_cli_contract.py`：帮助和版本入口、成功结果与进度的 JSON 分流、全局参数位置，以及参数解析和业务失败的输出契约与副作用。
- `test_ckb_value_flow.py`：账户发现、余额、充值、所选账户转账与临时账户清扫；错误私钥和未充值账户失败后，核对资产保护及后续正常转账。
- `test_udt_lifecycle.py`：通过非默认账户发行、转账、部分及全额销毁 SUDT/xUDT，比较 CLI 余额与链上 live cells；验证自定义 args、其他持有者保护，以及非法输入或销毁量超额时资产不变。
- `asset_assertions.py`：用 direct RPC 独立汇总余额，再核对 OffCKB 的资产发现、分类和过滤结果。
- `asset_failure_support.py`：为失败路径捕获 CLI 余额、live cells 和真实代理交易记录；用 direct RPC 排除 Indexer 滞后掩盖输入被消费的情况。
- `test_contract_deployment.py`：使用非默认账户完成普通部署、Type-ID 首次部署与升级，核对记录和 code cell 归属；损坏 Type ID 记录时保护旧合约及部署文件。
- `test_project_scaffolding.py`：默认创建与自动安装、自定义路径和名称、已有项目保护，以及生成项目原有脚本的构建、部署和 mock/devnet 示例调用；部署和调用均等待链上确认，调用交易必须引用本次部署。
- `project_support.py`：项目用例共用的隔离工具准备、项目创建和原有脚本执行。
- `diagnostic_support.py`：通过原有项目脚本构建和部署带唯一输出的合约，准备经 proxy 的调用及直接上链的双脚本调用。
- `test_transaction_debugging.py`：按哈希调试真实失败交易；直接上链且未缓存的交易只调试所选脚本，并用完整调试确认另一脚本的输出确实可见；均核对产品自行补齐的真实输入与依赖。
- `test_logs.py`：后台运行时查找本次合约输出，并核对 node/script/miner/rpc 的真实日志来源、文本筛选和行数限制。
- `test_rpc_proxy.py`：核对代理和直连属于同一开发链、成功交易缓存与链上一致、错误原样转发且可恢复，以及控制字符不能伪造日志。
- `test_system_scripts.py`：用展示的账户锁及依赖签名、消费真实输入；核对 CCC/Lumos 导出和指定位置的多网络 JSON 文件，公共网络部分只检查结构。

自动化行为应通过附近的 `TEST-MAP: <CASE-ID>` 注释映射到评审用例。

`core` 表示当前本地 devnet 回归集合，P0/P1 表示评审文档中的场景优先级，两者不等同。已有升级、部分销毁等 P1 回归继续随 `make test` 执行；新增用例按用户流程补齐，不因降为 P1 而删除已有有效检查。每条用例自己准备必要状态，不依赖其他测试先运行。

Fiber 用例使用独立的 `fiber` marker，需显式提供 canary 包和匹配的 CKB/FNN 工具；`FNN_BIN` 或 `--fnn-bin` 指向完整发布目录中的 `fnn`。按 [Fiber 运行配置](../config/README.md#fiber-专项) 设置后，在仓库根目录执行：

```bash
make test TESTS=tests/fiber ARGS='-m fiber'
make test TESTS=tests/fiber/test_startup.py ARGS='-m fiber'
make test TESTS=tests/fiber/test_channel_flow.py ARGS='-m fiber'
```

默认 `core` 集合不变；工具布局、进程隔离与执行边界见 [Fiber 测试说明](fiber/README.md)。

资产和部署 CLI 用例通过 JSON 获取结果；前台默认启动、项目创建和失败交易调试使用普通文本入口，日志以文本查看为主并补充 JSON 等价读取。生成项目的脚本直接运行，测试不改写 build/deploy/test 文件；诊断用例只修改用户合约，产生可识别的成功或失败调用。项目构建、部署 fixture 可以复用准备结果；聚焦任意一条测试时，也会自动准备所需项目和链上状态。

`project` marker 包括项目创建、失败交易调试和日志用例，均需预先准备原生 `CKB_DEBUGGER_BIN`；涉及构建和调用时还要安装生成项目的依赖。系统脚本用例不需要创建项目或 debugger。默认前台启动用例使用与包默认版本一致的 `DEFAULT_CKB_BIN`。项目创建后由 OffCKB 自行配置 debugger 的 PATH 入口，测试不代替产品写 debugger shim。

项目依赖默认通过 pnpm 离线缓存安装。第一次缺少缓存时，可显式执行 `make test TESTS=tests/test_project_scaffolding.py ARGS='--project-online'` 允许下载；这些测试项会标为 `network`。随后正常 `make test` 保持项目依赖离线安装；不增加新的 Make 目标，也不自动跳过缺依赖的用例。`make prepare` 准备的 OffCKB 包依赖与生成项目的依赖不完全相同。

聚焦“使用与排查”流程：

```bash
make test TESTS='tests/test_transaction_debugging.py tests/test_logs.py tests/test_system_scripts.py'
```

`TESTS` 也可只保留一个模块；缺少项目依赖缓存时，追加 `ARGS='--project-online'` 显式允许下载。SDK 交易辅助程序见 [fixtures 说明](../fixtures/README.md)。

聚焦“继续开发与重置环境”：`make test TESTS=tests/test_devnet_state.py`。各用例分别准备独立环境、通过 CLI 保存非默认配置并完成转账；不需要项目构建或 debugger，也不依赖其他测试先运行。

聚焦“配置生效与版本入口”：`make test TESTS='tests/test_global_settings.py tests/test_devnet_configuration.py tests/test_cli_contract.py'`。版本选择用例需要两个真实本地 CKB：`CKB_BIN` 与包默认版本不同，`DEFAULT_CKB_BIN` 与包默认版本一致；两者都放入隔离托管目录，验证实际选中的二进制。日志配置用例不需要项目构建或 debugger。

配置权限故障用例需以普通用户运行，使文件权限能够实际阻止读写；root 用户会绕过该前置条件。

聚焦“启动失败后的恢复与配置保护”：`make test TESTS='tests/test_node_recovery.py tests/test_node_stop_safety.py tests/test_devnet_configuration.py'`。端口冲突和无关进程均由测试创建，失败清理在任何兜底操作前判定；批量配置检查使用已经产块并停止的真实链数据。这些场景不需要项目构建或 debugger。

`make prepare` 根据 `config/offckb.toml` 下载发布包或从所选源码构建包；测试安装该包后执行 `offckb --version`，核对包内版本，并显示实际版本与来源。`scripts/test_offckb_target.py` 是运行设施的版本选择、发布包校验和版本命令回归检查，使用本地 Git 仓库、模拟 npm 响应和临时 CLI 替身，通过 `.venv/bin/python -m unittest scripts.test_offckb_target` 执行，不属于 OffCKB 产品评审用例或其覆盖率。

测试不得依赖固定 sleep。成功交易必须等到 `committed`，依赖 Indexer 的后续操作再等待其追到提交块；资产数量由 live cells 独立核算。故意失败的合约交易可能在提交时就被拒绝、无法查询交易状态，此时核对明确的合约错误、proxy 保存的实际交易与 debugger 诊断，不能把任意 RPC 或网络错误当作预期失败。

显式二进制首次启动用例不预写 OffCKB settings、不提供托管 CKB；默认前台用例同样不预写 settings，只预备包默认版本的托管二进制。其他业务用例预置本地托管二进制，避免辅助命令下载 CKB。均不设置 `OFFCKB_CLI_PATH`；项目通过隔离 PATH 调用同一个已安装 CLI。停止失败时仍执行限定归属的兜底清理，但保留失败结果。

运行 `.venv/bin/python -m unittest scripts.test_acceptance_oracles` 可检查测试设施本身：故意留下 PID、让 CLI 漏报资产或忽略过滤参数、让部署记录与链上一起归属错误账户时，断言必须失败；启动失败后的进程识别还会排除共用二进制的其他开发链及日志阅读进程。这些替身检查不计入产品自动化覆盖率。

运行 `.venv/bin/python -m unittest scripts.test_process_runner` 可检查普通文本/JSON 命令执行、敏感信息脱敏，以及脚本超时后其子进程的回收；这些也不计入产品覆盖率。
