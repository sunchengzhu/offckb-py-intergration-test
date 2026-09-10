# offckb-py-intergration-test

独立的 OffCKB Python 黑盒集成测试工程，可单独作为 GitHub 仓库维护。测试调用打包安装后的 `offckb` CLI，启动真实 CKB devnet，并通过 RPC、交易确认和链上 cell 验证结果。

首期覆盖节点生命周期、账户和 CKB 流转、SUDT/xUDT 生命周期、合约部署与 Type ID 升级。项目脚手架、RPC proxy 与日志的独立验收留待后续批次。

## 目录与源码

```text
project/
├── offckb/                       # 产品源码，单独的 Git 仓库
└── offckb-py-intergration-test/   # 本测试仓库
    ├── reviews/                 # 中文评审用例
    ├── tests/                   # pytest 用例与运行设施
    ├── scripts/                 # TEST-MAP 检查工具
    ├── config/                  # 测试配置说明
    ├── fixtures/                # 测试数据说明
    ├── templates/               # 用例评审模板
    └── source/offckb            # 可选本地源码链接，不提交 Git
```

被测源码按以下顺序选择：

1. `--offckb-source /absolute/path/to/offckb`，或环境变量 `OFFCKB_SOURCE`。
2. 本仓库内的 `source/offckb/`。
3. 与本仓库同级的 `../offckb/`。

需要链接其他位置的源码时，在测试仓库根目录执行（目标必须尚不存在）：

```bash
ln -s /absolute/path/to/offckb source/offckb
```

上述目录必须是 `@offckb/cli` 源码根。也可通过 `--offckb-package` 验收 tarball，无须在测试仓库中保存产品源码。

## 首次准备

- Linux 或 macOS，Python 3.11+。
- Node.js 20+，pnpm 10（已用 10.12.4 验证）。同一终端中的安装、构建和测试应使用同一 pnpm；必要时传 `--pnpm-bin`。
- 本地可执行的 CKB 0.205+ 二进制（已用 0.207.0 验证）；核心测试不会下载 CKB。
- 本地端口 `8114`、`8115`、`18114`、`28114` 空闲；不可使用 pytest-xdist 并行运行。

在测试仓库根目录创建环境。迁移或重新克隆后应重建 `.venv`，不要复制旧目录的虚拟环境：

```bash
cd /absolute/path/to/offckb-py-intergration-test
python3.11 -m venv .venv
.venv/bin/python -m pip install -e .
```

在产品源码目录安装依赖并构建：

```bash
pnpm -C /absolute/path/to/offckb install --frozen-lockfile
pnpm -C /absolute/path/to/offckb build
```

安装会填充 pnpm store。套件使用 `--prefer-offline --ignore-scripts` 安装被测 tarball，优先复用缓存，缺失的 metadata 或依赖仍可能访问 registry；真实链上测试不需要公共网络服务。

## 运行核心测试

从测试仓库根目录执行，显式指定 `-c pyproject.toml` 与 `tests`，防止 pytest 因二进制参数指向其他工程而加载错误配置：

```bash
.venv/bin/pytest -c pyproject.toml -vv -m core tests \
  --ckb-bin /absolute/path/to/ckb
```

默认流程为：找到产品源码 → `pnpm pack` → 隔离 prefix 安装 → 调用安装后的 CLI → 启动 devnet → 执行用例 → 停止所属进程并释放端口。源码变更后应先重新构建。

指定其他源码及 pnpm：

```bash
.venv/bin/pytest -c pyproject.toml -vv -m core tests \
  --offckb-source /absolute/path/to/offckb \
  --pnpm-bin /absolute/path/to/pnpm \
  --ckb-bin /absolute/path/to/ckb
```

验收已有发布包：

```bash
.venv/bin/pytest -c pyproject.toml -vv -m core tests \
  --offckb-package /absolute/path/to/offckb-cli.tgz \
  --ckb-bin /absolute/path/to/ckb
```

如 pnpm 缓存位于非默认位置，可补充 `--pnpm-store-dir /absolute/path/to/store/v10` 和 `--pnpm-cache-dir /absolute/path/to/pnpm`。纯 tarball 环境需先准备依赖缓存。

本地快速调试时，可用 `--offckb-entry /absolute/path/to/offckb/build/index.js` 跳过打包安装；该参数与 `--offckb-package` 互斥。发布验收应使用默认打包流程或 tarball。

按模块聚焦执行：

```bash
.venv/bin/pytest -c pyproject.toml -vv tests/test_devnet_lifecycle.py --ckb-bin /absolute/path/to/ckb
.venv/bin/pytest -c pyproject.toml -vv tests/test_ckb_value_flow.py --ckb-bin /absolute/path/to/ckb
.venv/bin/pytest -c pyproject.toml -vv tests/test_udt_lifecycle.py --ckb-bin /absolute/path/to/ckb
.venv/bin/pytest -c pyproject.toml -vv tests/test_contract_deployment.py --ckb-bin /absolute/path/to/ckb
```

pytest 结束时显示总耗时；在命令中加 `--durations=0 --durations-min=0` 可查看所有用例的 setup/call/teardown 耗时。只收集用例、不启动节点时加 `--collect-only`。

## 隔离与失败诊断

每次运行建立独立 HOME、XDG 根目录、临时 npm prefix 和 OffCKB settings；子进程只继承运行所需的环境变量白名单。测试通过私钥文件签名，并在日志中脱敏，不使用开发者真实的 OffCKB 数据。

固定端口由跨进程文件锁保护；检测到已有服务时会报错退出。测试优先通过产品 `node stop` 清理所属服务，确认进程和端口退出后结束。

成功后临时运行目录自动删除；失败时输出保留路径，包含 artifact 版本与 SHA、命令输出、daemon/node/miner/proxy 日志和 RPC 快照。加 `--keep-runtime` 可在成功后也保留目录。

## 产品内部测试与评审

OffCKB 自身的 `pnpm typecheck`、`pnpm lint`、`pnpm test:ci` 和 shell 测试继续作为产品仓库门禁。本工程不套壳运行 Jest，只复用稳定的测试数据语义，并独立核验真实副作用。

评审用例位于 `reviews/`。按 `AGENTS.md` 先提交行为变更供人工确认，再实现对应自动化；每个映射使用 `TEST-MAP: <CASE-ID>`。检查当前覆盖：

```bash
.venv/bin/python scripts/check_test_map.py
```

`--require-complete` 要求全部评审用例都已自动化，首期尚未实现的后续批次会使该模式非零退出。

## 独立 GitHub 仓库

本目录可以直接作为独立仓库根目录推送。`.gitignore` 已排除源码链接、虚拟环境、缓存、二进制包和运行报告；版本管理只包含测试代码、评审文档及配置。

后续创建远程仓库后，为本地仓库添加该远程地址并推送 `main` 分支即可。GitHub CI 中分别准备产品源码、Node/pnpm、Python 和 CKB artifact，再使用上面的显式参数执行测试，无需将测试放回产品仓库。

本工程从 OffCKB 的 `integration-tests/` 拆出，保留原 MIT 许可证，见 `LICENSE`。
