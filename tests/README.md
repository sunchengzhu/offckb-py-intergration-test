# 集成测试代码

在仓库根目录执行 `make prepare` 准备环境，`make test` 运行全部核心用例；聚焦单模块使用 `make test TESTS=tests/test_devnet_lifecycle.py`。本机路径和其他参数见 [运行配置](../config/README.md)。

这里存放带有 `TEST-MAP` 注释的集成测试。每条测试以中文说明用户目的，并用 CLI 结果、交易确认和必要的文件或进程观察核验结果。当前本地回归套件由以下部分组成：

- `conftest.py`：版本配置与产物校验、命令行参数、隔离 HOME、固定端口租约和 fixture。
- `harness.py`：CLI 执行、RPC 轮询、Indexer/cell oracle、daemon 精确归属与 teardown。
- `test_devnet_lifecycle.py`：空配置首次启动、实际 CKB 路径、ready、产块、Indexer 和 OffCKB 自行停止并清理 PID。
- `test_ckb_value_flow.py`：找到预充值账户、查看资产、给初始余额为零的新地址充值，以及通过所选账户转账；充值和转账后同时核对 CLI 余额与链上结果。
- `test_udt_lifecycle.py`：通过非默认账户发行、转账和部分销毁 SUDT/xUDT，每一步比较 CLI 余额与链上 live cells；销毁前自行给另一持有者准备非零代币，单独运行也能验证其余额不受影响；自定义 xUDT args 区别于默认值。
- `asset_assertions.py`：用 direct RPC 独立汇总余额，再核对 OffCKB 的资产发现、分类和过滤结果。
- `test_contract_deployment.py`：使用非默认账户完成普通部署、Type-ID 首次部署与升级，核对记录和 code cell 的实际归属，升级时保留旧 migration 并新增记录。

自动化行为应通过附近的 `TEST-MAP: <CASE-ID>` 注释映射到评审用例。

`core` 表示当前本地 devnet 回归集合，P0/P1 表示评审文档中的场景优先级，两者不等同。已有升级、部分销毁等 P1 回归继续随 `make test` 执行；新增用例按用户流程补齐，不因降为 P1 而删除已有有效检查。每条用例自己准备必要状态，不依赖其他测试先运行。

当前已映射业务用例通过 JSON 获取稳定结果；前台默认启动、普通文本交互和完整生成项目旅程仍按相应评审场景单独补齐，不能从 daemon 或 JSON 用例的通过推断这些入口已被验证。

`make prepare` 根据 `config/offckb.toml` 下载发布包或从所选源码构建包；测试安装该包后执行 `offckb --version`，核对包内版本，并显示实际版本与来源。`scripts/test_offckb_target.py` 是运行设施的版本选择、发布包校验和版本命令回归检查，使用本地 Git 仓库、模拟 npm 响应和临时 CLI 替身，通过 `.venv/bin/python -m unittest scripts.test_offckb_target` 执行，不属于 OffCKB 产品评审用例或其覆盖率。

测试不得依赖固定 sleep。交易必须等到 `committed`，随后等待 Indexer 至少追到提交块；资产数量再由 live cells 独立核算。

首次启动用例不预写 OffCKB settings、不提供托管 CKB，也不设置 `OFFCKB_CLI_PATH`；其余业务用例预置本地托管二进制，避免辅助命令下载 CKB。安装包的入口路径仅用于确认进程归属，不注入产品运行环境。停止失败时仍执行限定归属的兜底清理，但保留失败结果。

运行 `.venv/bin/python -m unittest scripts.test_acceptance_oracles` 可检查测试设施本身：故意留下 PID、让 CLI 漏报资产或忽略过滤参数、让部署记录与链上一起归属错误账户时，断言必须失败。这些替身检查不计入产品自动化覆盖率。
