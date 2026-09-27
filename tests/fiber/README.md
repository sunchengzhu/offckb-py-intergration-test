# Fiber 集成测试

本目录集中保存 Fiber 用例及专属环境、RPC 辅助代码；复用上层的包安装、隔离用户目录和 CKB RPC 设施。评审依据见 [Fiber 用例目录](../../reviews/fiber/README.md)。

## 准备工具

使用 `@offckb/cli@0.5.0-canary-ee0ad6b`、CKB `0.208.0`、FNN `0.9.0`。提前准备被测 npm 包、包安装依赖缓存和真实工具；FNN 必须保留完整发布目录，`fnn` 旁边需有 `config/testnet/config.yml`。

`FNN_BIN`（或 `--fnn-bin`）告诉测试去哪里取真实工具；默认场景把工具放入隔离的托管位置，由 OffCKB 自行选择。版本覆盖、自定义二进制、配置和下载场景分别使用对应的公开入口；节点互联由产品完成。

已有 `.venv` 时可直接运行；首次使用先按 [工程说明](../../README.md) 执行 `make prepare`，通过 `OFFCKB_PACKAGE` 指定已准备的 canary 包。

## 运行

```bash
make test TESTS=tests/fiber ARGS='-m "fiber and not network and not tty"' \
  OFFCKB_PACKAGE=/absolute/path/to/offckb-cli-0.5.0-canary-ee0ad6b.tgz \
  CKB_BIN=/absolute/path/to/ckb-0.208.0/ckb \
  DEFAULT_CKB_BIN=/absolute/path/to/ckb-0.208.0/ckb \
  FNN_BIN=/absolute/path/to/fnn-0.9.0/fnn
```

也可以在忽略的 `config/local.mk` 中设置工具路径。使用 `TESTS=tests/fiber/test_startup.py` 只检查启动入口，使用 `TESTS=tests/fiber/test_channel_flow.py` 检查开通道、支付和协作关闭。每条用例都能独立准备所需状态。

`fiber` 标记用于明确选择这组新增功能；默认 `make test` 仍运行既有 `core` 集合。只收集用例可使用 `make test TESTS=tests/fiber ARGS='-m fiber --collect-only'`，收集成功不表示产品测试通过。

按文件聚焦：`test_lifecycle.py`（停止、继续与清理）、`test_configuration.py`（状态、日志、配置）、`test_recovery.py`（失败后恢复）、`test_udt.py`（SUDT/xUDT 通道）、`test_compatibility.py`（旧环境升级）。旧版本升级需要已发布的 `@offckb/cli@0.4.13` 包；通过 `OFFCKB_LEGACY_PACKAGE=/absolute/path/to/offckb-cli-0.4.13.tgz` 指定，或放在 `source/.prepared/packages/offckb-cli-0.4.13.tgz`。测试会校验版本并安装到独立 prefix；也接受仍为 0.4.13 的 `source/.prepared/offckb-cli.tgz`。

聚焦文档推荐的 UDT 正常主流程（发行、分发、零出资接受、首次支付）：沿用上面的包和工具参数，执行 `make test TESTS=tests/fiber/test_udt.py ARGS='-m fiber -k documented_zero_acceptor'`。SUDT、xUDT 各在独立环境运行，使用默认费率，不修改 OffCKB 管理的配置或合约依赖。

聚焦新增合约查询、FNN 意外退出与单个密钥材料缺失后的恢复：沿用上面的包和工具参数，执行 `make test TESTS=tests/fiber ARGS='-m fiber -k "fiber_scripts or unexpected_fnn_exit or incomplete_key_material"'`。三个用例分别覆盖真实 genesis 引用、联合/独立前台管理范围，以及两种 daemon 入口下缺少 `ckb/key`／`password` 后恢复原文件；均不下载工具、不操作真实用户数据。

## 执行边界

- 联合启动和已有 CKB 上启动均包含前台、daemon 两种方式。除 CKB 端口外，FNN 的 `21714`–`21716`、`8344`–`8346` 也需空闲；测试串行持有端口租约。
- 环境通过 OffCKB 启动和停止；前台以 Ctrl+C 对应的进程组信号退出。产品停止失败会保留失败结果，再执行限定归属的兜底回收。
- 失败时保留公开 RPC 快照、配置和脱敏日志；所属进程退出后删除私钥、通道签名数据库及其自动备份。
- 成功结果必须由公开 RPC 的最终状态与资金变化证明；不能用临时通道 ID、支付请求受理或日志中的 ready 代替。
- 普通 Fiber 集合不下载 CKB/FNN；网络下载与 TTY 流程须按下方说明显式启用。运行缺少必需工具时明确失败，不通过跳过用例掩盖。
- 自动化映射使用测试旁的 `TEST-MAP` 注释，由 `python3 scripts/check_test_map.py` 计算；不在文档维护覆盖台账。

UDT 开通道只有在 RPC 仍健康、等待 `ChannelReady` 超时且真实节点日志匹配 [fiber#1663](https://github.com/nervosnetwork/fiber/issues/1663) 或 [fiber#1667](https://github.com/nervosnetwork/fiber/issues/1667) 的条件时，才按已知缺陷报告 `XFAIL`；其他断言、连接或收尾失败仍报告失败。`FIB-UDT-05` 的 `ScriptNotFound` 不属于这两个 issue，保留真实失败。

复核其他流程时可在命令前加 `LC_ALL=C`，隔离 [offckb#519](https://github.com/ckb-devrel/offckb/issues/519) 的非英文 locale 停止缺陷。该条件必须随结果说明，不能据此宣称原 locale 下启停通过。

## 下载与终端专项

安装测试在真实 Linux/macOS x64/arm64 runner 上选择对应平台包，不修改平台标识。当前工程的进程和终端设施不支持 Windows；Windows x64 的实际下载、启动仍需另备 runner，不能据此声称跨平台全部通过。

下载专项需要联网和 `openssl`，通过 `--fiber-network` 明确启用：

```bash
make test TESTS=tests/fiber/test_installation.py \
  ARGS='-m "fiber and network" --fiber-network' \
  OFFCKB_PACKAGE=/absolute/path/to/offckb-cli-0.5.0-canary-ee0ad6b.tgz \
  CKB_BIN=/absolute/path/to/ckb-0.208.0/ckb \
  DEFAULT_CKB_BIN=/absolute/path/to/ckb-0.208.0/ckb \
  FNN_BIN=/absolute/path/to/fnn-0.9.0/fnn
```

测试先删除隔离目录中的托管 FNN，再通过 OffCKB 公开代理设置触发真实安装。测试自有 HTTPS 代理首次从官方 release 获取当前平台包，按固定 SHA-256 校验；同一次 pytest 进程中的后续场景按 URL 和校验值复用已验证字节，不写持久缓存。每次被测安装仍通过自己的代理连接实际下载，断连、中断和损坏仅施加到该下载连接。临时 CA 仅通过隔离进程的 `NODE_EXTRA_CA_CERTS` 信任，不安装到系统、不关闭 TLS 校验。修复后再次启动会拒绝全部代理请求，确认完整安装可离线复用。`FNN_BIN` 仍用于公共 fixture 的工具校验及构造待修复安装，不代替首次下载结果。

终端专项通过真实 PTY 操作默认取消、明确取消和明确确认：

```bash
make test TESTS=tests/fiber/test_installation.py \
  ARGS='-m "fiber and tty" --fiber-tty' \
  OFFCKB_PACKAGE=/absolute/path/to/offckb-cli-0.5.0-canary-ee0ad6b.tgz \
  CKB_BIN=/absolute/path/to/ckb-0.208.0/ckb \
  DEFAULT_CKB_BIN=/absolute/path/to/ckb-0.208.0/ckb \
  FNN_BIN=/absolute/path/to/fnn-0.9.0/fnn
```

离线非交互批次使用 `ARGS='-m "fiber and not network and not tty"'`。若选择下载或终端用例却未提供对应开关，测试明确报配置错误，不跳过或标记为预期失败。

代理设施可独立自测，不启动产品、不访问官方 release，也不计入验收覆盖：

```bash
.venv/bin/python -m unittest scripts.test_fiber_download_proxy
```
