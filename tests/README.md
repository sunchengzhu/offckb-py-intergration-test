# 集成测试代码

在仓库根目录执行 `make prepare` 准备环境，`make test` 运行全部核心用例；聚焦单模块使用 `make test TESTS=tests/test_devnet_lifecycle.py`。本机路径和其他参数见 [运行配置](../config/README.md)。

这里存放带有 `TEST-MAP` 注释的集成测试。每条测试以中文说明用户目的，并用 CLI 结果、交易确认和必要的文件或进程观察核验结果。当前本地回归套件由以下部分组成：

- `conftest.py`：版本配置与产物校验、命令行参数、隔离 HOME、固定端口租约和 fixture。
- `harness.py`：CLI 执行、RPC 轮询、Indexer/cell oracle、daemon 精确归属与 teardown。
- `test_devnet_lifecycle.py`：空配置首次启动、实际 CKB 路径、ready、产块、Indexer 和 OffCKB 自行停止并清理 PID。
- `test_default_node.py`：普通 `offckb node` 自行选择包默认版本的真实 CKB，初始化、提供连接地址、产块，Ctrl+C 后退出并可再次启动。
- `test_ckb_value_flow.py`：找到预充值账户、查看资产、给初始余额为零的新地址充值，以及通过所选账户转账；充值和转账后同时核对 CLI 余额与链上结果。
- `test_udt_lifecycle.py`：通过非默认账户发行、转账和部分销毁 SUDT/xUDT，每一步比较 CLI 余额与链上 live cells；销毁前自行给另一持有者准备非零代币，单独运行也能验证其余额不受影响；自定义 xUDT args 区别于默认值。
- `asset_assertions.py`：用 direct RPC 独立汇总余额，再核对 OffCKB 的资产发现、分类和过滤结果。
- `test_contract_deployment.py`：使用非默认账户完成普通部署、Type-ID 首次部署与升级，核对记录和 code cell 的实际归属，升级时保留旧 migration 并新增记录。
- `test_project_scaffolding.py`：默认创建与自动安装、自定义路径和名称、已有项目保护，以及生成项目原有脚本的构建、部署和 mock/devnet 示例调用；部署和调用均等待链上确认，调用交易必须引用本次部署。

自动化行为应通过附近的 `TEST-MAP: <CASE-ID>` 注释映射到评审用例。

`core` 表示当前本地 devnet 回归集合，P0/P1 表示评审文档中的场景优先级，两者不等同。已有升级、部分销毁等 P1 回归继续随 `make test` 执行；新增用例按用户流程补齐，不因降为 P1 而删除已有有效检查。每条用例自己准备必要状态，不依赖其他测试先运行。

资产和部署 CLI 用例通过 JSON 获取结果；前台默认启动和项目创建使用普通文本入口。生成项目的脚本直接运行，测试不改写 build/deploy/test 文件。项目构建、部署 fixture 可以复用准备结果；聚焦任意一条测试时，也会自动准备所需项目和链上状态。

项目用例使用预先准备的原生 `CKB_DEBUGGER_BIN`，默认前台启动用例使用与包默认版本一致的 `DEFAULT_CKB_BIN`。项目创建后由 OffCKB 自行配置 debugger 的 PATH 入口，测试不代替产品写 debugger shim。

项目依赖默认通过 pnpm 离线缓存安装。第一次缺少缓存时，可显式执行 `make test TESTS=tests/test_project_scaffolding.py ARGS='--project-online'` 允许下载；这些测试项会标为 `network`。随后正常 `make test` 保持项目依赖离线安装；不增加新的 Make 目标，也不自动跳过缺依赖的用例。`make prepare` 准备的 OffCKB 包依赖与生成项目的依赖不完全相同。

`make prepare` 根据 `config/offckb.toml` 下载发布包或从所选源码构建包；测试安装该包后执行 `offckb --version`，核对包内版本，并显示实际版本与来源。`scripts/test_offckb_target.py` 是运行设施的版本选择、发布包校验和版本命令回归检查，使用本地 Git 仓库、模拟 npm 响应和临时 CLI 替身，通过 `.venv/bin/python -m unittest scripts.test_offckb_target` 执行，不属于 OffCKB 产品评审用例或其覆盖率。

测试不得依赖固定 sleep。交易必须等到 `committed`，随后等待 Indexer 至少追到提交块；资产数量再由 live cells 独立核算。

显式二进制首次启动用例不预写 OffCKB settings、不提供托管 CKB；默认前台用例同样不预写 settings，只预备包默认版本的托管二进制。其他业务用例预置本地托管二进制，避免辅助命令下载 CKB。均不设置 `OFFCKB_CLI_PATH`；项目通过隔离 PATH 调用同一个已安装 CLI。停止失败时仍执行限定归属的兜底清理，但保留失败结果。

运行 `.venv/bin/python -m unittest scripts.test_acceptance_oracles` 可检查测试设施本身：故意留下 PID、让 CLI 漏报资产或忽略过滤参数、让部署记录与链上一起归属错误账户时，断言必须失败。这些替身检查不计入产品自动化覆盖率。

运行 `.venv/bin/python -m unittest scripts.test_process_runner` 可检查普通文本/JSON 命令执行、敏感信息脱敏，以及脚本超时后其子进程的回收；这些也不计入产品覆盖率。
