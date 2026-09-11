# offckb-py-intergration-test

独立的 OffCKB Python 黑盒集成测试工程，可单独作为 GitHub 仓库维护。测试调用打包安装后的 `offckb` CLI，启动真实 CKB devnet，并通过 RPC、交易确认和链上 cell 验证结果。

首期覆盖节点生命周期、账户和 CKB 流转、SUDT/xUDT 生命周期、合约部署与 Type ID 升级。项目脚手架、RPC proxy 与日志的独立验收留待后续批次。

## 选择被测 OffCKB 版本

版本统一配置在 [config/offckb.toml](config/offckb.toml)，默认测试 npm 上最新发布的 `@offckb/cli`：

```toml
repo = "https://github.com/ckb-devrel/offckb.git"
ref = "latest"
```

`latest` 在每次 `make prepare` 时解析 npm 的 `latest` 标签，下载原始发布包并校验完整性，无需产品源码。准备完成时用一行显示包内版本和来源。每次 `make test` 固定使用已准备的包，隔离安装后执行被测程序的 `offckb --version`，核对包内版本，在日志最前面显示实际版本、来源和包 SHA256，再输出映射检查结果及 pytest 日志。

测试开发分支时将 `ref` 改为 `"develop"`，随后执行相同的 `make prepare`、`make test`。也支持其他远端分支、tag（如 `v0.4.13`）、具体 commit；测试本地源码及未提交修改时设为 `working-tree`。源码模式额外显示实际 commit 和是否包含本地修改。

修改版本配置后必须重新准备。分支/tag/commit 模式从 Git 内容导出临时源码，不切换开发者工作区；本地分支即使也叫 `develop`，其未推送提交也不会混入远端 `develop` 测试。

## 快速开始

需要 Linux 或 macOS、Python 3.11+、Node.js 20+、pnpm 10 和本地 CKB 0.205+ 二进制（已验证 pnpm 10.12.4、CKB 0.207.0）。端口 `8114`、`8115`、`18114`、`28114` 应空闲，测试串行执行。

在仓库根目录把 `config/local.mk.example` 复制为 `config/local.mk`，将 `CKB_BIN` 改为本机 CKB 的绝对路径。该配置只需填写一次，已被 Git 忽略。选择 `develop` 等源码模式时，优先复用 `source/offckb/` 或同级的 `../offckb/` Git 仓库；其他位置设置 `OFFCKB_SOURCE`。源码目录不存在时才克隆配置的仓库。

日常只使用两个入口：

```bash
make prepare  # 准备环境和所选版本，默认下载最新发布包
make test     # 检查 TEST-MAP，然后运行全部核心测试
```

`make prepare` 创建 `.venv`、安装 Python 依赖，再下载发布包并准备 pnpm 依赖缓存；源码模式则安装构建依赖、构建并打包。准备版本和依赖时可能访问网络，不会自动下载 CKB。迁移或重新克隆后应重建虚拟环境，不要复制旧 `.venv`。Python 不叫 `python3.11` 时，可用 `make prepare PYTHON=python3` 指定 3.11+ 解释器。

`make test` 自动固定 pytest 配置、核心 marker 和测试目录，流程为：校验已准备包及版本配置 → 将包复制到本次临时目录 → 隔离 prefix 安装 → 执行 `offckb --version` 并核对版本 → 启动 devnet → 执行用例 → 停止所属进程并释放端口。测试退出码会使 Make 成功或失败。

单模块运行仍用同一个入口：

```bash
make test TESTS=tests/test_devnet_lifecycle.py
```

`TESTS` 也接受 pytest node ID，额外参数通过 `ARGS` 传入。已有发布包验收、调试入口、依赖缓存、耗时和仅收集用例等选项统一见 [运行配置](config/README.md)。

## 目录与源码

```text
project/
├── offckb/                       # 产品源码，单独的 Git 仓库
└── offckb-py-intergration-test/   # 本测试仓库
    ├── reviews/                 # 中文评审用例
    ├── tests/                   # pytest 用例与运行设施
    ├── scripts/                 # 版本准备与 TEST-MAP 检查工具
    ├── Makefile                 # prepare / test 统一入口
    ├── config/                  # offckb.toml 版本配置、本机配置示例
    ├── fixtures/                # 测试数据说明
    ├── templates/               # 用例评审模板
    └── source/                  # 本地源码、.prepared 发布包缓存，不提交 Git
```

源码构建模式按以下顺序选择本地 Git 仓库：

1. `--offckb-source /absolute/path/to/offckb`，或环境变量 `OFFCKB_SOURCE`。
2. 本仓库内的 `source/offckb/`。
3. 与本仓库同级的 `../offckb/`。

上述目录必须是 `@offckb/cli` 源码根。默认 `latest` 或通过 `OFFCKB_PACKAGE` 验收 tarball 时无须产品源码。源码目录和链接的说明见 [source/README.md](source/README.md)。

## 隔离与失败诊断

每次运行建立独立 HOME、XDG 根目录、临时 npm prefix 和 OffCKB settings；子进程只继承运行所需的环境变量白名单。测试通过私钥文件签名，并在日志中脱敏，不使用开发者真实的 OffCKB 数据。

固定端口由跨进程文件锁保护；检测到已有服务时会报错退出。测试优先通过产品 `node stop` 清理所属服务，确认进程和端口退出后结束。

成功后临时运行目录自动删除；失败时输出保留路径，包含 artifact 版本与 SHA、命令输出、daemon/node/miner/proxy 日志和 RPC 快照。加 `--keep-runtime` 可在成功后也保留目录。

## 产品内部测试与评审

OffCKB 自身的 `pnpm typecheck`、`pnpm lint`、`pnpm test:ci` 和 shell 测试继续作为产品仓库门禁。本工程不套壳运行 Jest，只复用稳定的测试数据语义，并独立核验真实副作用。

评审用例位于 `reviews/`。按 `AGENTS.md` 先提交行为变更供人工确认，再实现对应自动化；每个映射使用 `TEST-MAP: <CASE-ID>`。`make test` 自动计算覆盖并检查重复评审 ID、孤立代码映射；未映射的后续用例只报告，不阻止运行。

## 独立 GitHub 仓库

`.gitignore` 已排除源码链接、虚拟环境、缓存、二进制包和运行报告；版本管理只包含测试代码、评审文档及配置。

修改通过独立分支向 `main` 发起 PR，评审后合并。CI 环境准备 Node/pnpm、Python 和 CKB artifact，通过环境变量传入路径后同样执行 `make prepare`、`make test`，源码模式按需获取产品源码。
