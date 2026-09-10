# 集成测试代码

这里存放带有 `TEST-MAP` 注释的集成测试。首期核心套件由以下部分组成：

- `conftest.py`：命令行参数、隔离 HOME、固定端口租约、artifact 注入和 fixture。
- `harness.py`：CLI 执行、RPC 轮询、Indexer/cell oracle、daemon 精确归属与 teardown。
- `test_devnet_lifecycle.py`：启动、ready、产块、Indexer 和停止。
- `test_ckb_value_flow.py`：账户、余额、deposit 和私钥文件转账。
- `test_udt_lifecycle.py`：SUDT/xUDT 发行、转账和部分销毁。
- `test_contract_deployment.py`：普通部署、Type-ID 首次部署与升级。

自动化行为应通过附近的 `TEST-MAP: <CASE-ID>` 注释映射到评审用例。

测试不得依赖固定 sleep。交易必须等到 `committed`，随后等待 Indexer 至少追到提交块；资产数量再由 live cells 独立核算。
