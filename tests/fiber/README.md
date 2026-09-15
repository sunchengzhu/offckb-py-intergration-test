# Fiber 集成测试

本目录集中保存 Fiber 用例及专属环境、RPC 辅助代码；复用上层的包安装、隔离用户目录和 CKB RPC 设施。评审依据见 [Fiber 用例目录](../../reviews/fiber/README.md)。

## 准备工具

首批使用 `@offckb/cli@0.5.0-canary-ee0ad6b`、CKB `0.208.0`、FNN `0.9.0`。提前准备被测 npm 包、包安装依赖缓存和真实工具；FNN 必须保留完整发布目录，`fnn` 旁边需有 `config/testnet/config.yml`。

`FNN_BIN`（或 `--fnn-bin`）只告诉测试去哪里取工具；测试会把工具放入隔离的默认托管位置，由 OffCKB 自行选择。不会给产品命令添加二进制或版本覆盖参数，也不会由测试生成 FNN 配置或补连节点。

已有 `.venv` 时可直接运行；首次使用先按 [工程说明](../../README.md) 执行 `make prepare`，通过 `OFFCKB_PACKAGE` 指定已准备的 canary 包。

## 运行

```bash
make test TESTS=tests/fiber ARGS='-m fiber' \
  OFFCKB_PACKAGE=/absolute/path/to/offckb-cli-0.5.0-canary-ee0ad6b.tgz \
  CKB_BIN=/absolute/path/to/ckb-0.208.0/ckb \
  DEFAULT_CKB_BIN=/absolute/path/to/ckb-0.208.0/ckb \
  FNN_BIN=/absolute/path/to/fnn-0.9.0/fnn
```

也可以在忽略的 `config/local.mk` 中设置工具路径。使用 `TESTS=tests/fiber/test_startup.py` 只检查启动入口，使用 `TESTS=tests/fiber/test_channel_flow.py` 检查开通道、支付和协作关闭。每条用例都能独立准备所需状态。

`fiber` 标记用于明确选择这组新增功能；默认 `make test` 仍运行既有 `core` 集合。只收集用例可使用 `make test TESTS=tests/fiber ARGS='-m fiber --collect-only'`，收集成功不表示产品测试通过。

## 执行边界

- 联合启动和已有 CKB 上启动均包含前台、daemon 两种方式。除 CKB 端口外，FNN 的 `21714`、`21715`、`8344`、`8345` 也需空闲；测试串行持有端口租约。
- 环境通过 OffCKB 启动和停止；前台以 Ctrl+C 对应的进程组信号退出。产品停止失败会保留失败结果，再执行限定归属的兜底回收。
- 失败时保留公开 RPC 快照、配置和脱敏日志；所属进程退出后删除私钥、通道签名数据库及其自动备份。
- 成功结果必须由公开 RPC 的最终状态与资金变化证明；不能用临时通道 ID、支付请求受理或日志中的 ready 代替。
- 首批不下载 CKB/FNN；首次下载、平台与 TTY 流程有独立评审范围。运行缺少必需工具时明确失败，不通过跳过用例掩盖。
- 自动化映射使用测试旁的 `TEST-MAP` 注释，由 `python3 scripts/check_test_map.py` 计算；不在文档维护覆盖台账。
