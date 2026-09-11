# 集成测试代码

在仓库根目录执行 `make prepare` 准备环境，`make test` 运行全部核心用例；聚焦单模块使用 `make test TESTS=tests/test_devnet_lifecycle.py`。本机路径和其他参数见 [运行配置](../config/README.md)。

这里存放带有 `TEST-MAP` 注释的集成测试。首期核心套件由以下部分组成：

- `conftest.py`：版本配置与产物校验、命令行参数、隔离 HOME、固定端口租约和 fixture。
- `harness.py`：CLI 执行、RPC 轮询、Indexer/cell oracle、daemon 精确归属与 teardown。
- `test_devnet_lifecycle.py`：空配置首次启动、实际 CKB 路径、ready、产块、Indexer 和 OffCKB 自行停止并清理 PID。
- `test_ckb_value_flow.py`：账户、余额、deposit 和私钥文件转账。
- `test_udt_lifecycle.py`：SUDT/xUDT 发行、转账和部分销毁，每一步比较 CLI 余额与链上 live cells；自定义 xUDT args 区别于默认值。
- `asset_assertions.py`：用 direct RPC 独立汇总余额，再核对 OffCKB 的资产发现、分类和过滤结果。
- `test_contract_deployment.py`：使用非默认账户完成普通部署、Type-ID 首次部署与升级，核对记录和 code cell 的实际归属。

自动化行为应通过附近的 `TEST-MAP: <CASE-ID>` 注释映射到评审用例。

`make prepare` 根据 `config/offckb.toml` 下载发布包或从所选源码构建包；测试安装该包后执行 `offckb --version`，核对包内版本，并显示实际版本与来源。`scripts/test_offckb_target.py` 是运行设施的版本选择、发布包校验和版本命令回归检查，使用本地 Git 仓库、模拟 npm 响应和临时 CLI 替身，通过 `.venv/bin/python -m unittest scripts.test_offckb_target` 执行，不属于 OffCKB 产品评审用例或其覆盖率。

测试不得依赖固定 sleep。交易必须等到 `committed`，随后等待 Indexer 至少追到提交块；资产数量再由 live cells 独立核算。

首次启动用例不预写 OffCKB settings、不提供托管 CKB，也不设置 `OFFCKB_CLI_PATH`；其余业务用例预置本地托管二进制，避免辅助命令下载 CKB。安装包的入口路径仅用于确认进程归属，不注入产品运行环境。停止失败时仍执行限定归属的兜底清理，但保留失败结果。

运行 `.venv/bin/python -m unittest scripts.test_acceptance_oracles` 可检查测试设施本身：故意留下 PID、让 CLI 漏报资产或忽略过滤参数、让部署记录与链上一起归属错误账户时，断言必须失败。这些替身检查不计入产品自动化覆盖率。
