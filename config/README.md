# 运行配置

日常入口统一使用根目录的 `make prepare` 和 `make test`，见 [README](../README.md)。

## 被测版本

共享版本配置在 [offckb.toml](offckb.toml)，默认 `ref = "latest"`，测试 npm 上最新发布的 `@offckb/cli`。`repo = "https://github.com/ckb-devrel/offckb.git"` 只在选择源码构建模式时使用。这是被测产品的版本；`pyproject.toml` 中的版本属于测试工程自身。

| `ref` 的值 | 实际选择 |
| --- | --- |
| `latest`（默认） | npm `@offckb/cli` 的 `latest` 标签对应的原始发布包，每次准备时更新，无需源码 |
| `develop` 或其他分支名 | 配置仓库的远端分支，在每次准备时解析最新 commit |
| `v0.4.13` 等 tag | 配置仓库中的 tag 对应 commit |
| 具体 commit SHA | 固定该提交；本地已有对象可直接复用，远端获取时要求完整 SHA |
| `working-tree` | 构建所选本地源码的当前内容，包含未提交修改 |

`latest` 直接从官方 npm registry 下载发布包，校验 registry 提供的 SHA512 和包内版本，再在临时 prefix 中准备依赖缓存，禁用安装脚本。分支/tag/commit 模式在临时源码目录构建，开发者工作区的分支、index 和 `FETCH_HEAD` 保持不变；缺失的 Git 对象会写入已有源码仓库的对象库。单个已准备包及来源元数据保存在 Git 忽略的 `source/.prepared/`，运行时将包复制到新的临时目录，再隔离安装。

修改 `ref` 后仍执行 `make prepare`、`make test`。临时测试开发分支可运行 `make prepare OFFCKB_REF=develop`，随后 `make test OFFCKB_REF=develop`；长期默认直接修改 `offckb.toml` 或在 `local.mk` 配置。测试会拒绝配置与已准备包不匹配的情况，防止误测旧包；即使 npm 发布新版本或源码随后变化，也只测试已准备的包，更新目标需要再次准备。

准备成功后才替换缓存，测试会校验包与元数据是否一致。测试输出显示实际包版本、来源和 SHA256；源码模式额外显示完整 commit、是否包含本地修改。`working-tree` 模式更新本地代码后也需重新准备；要复现完整源码内容，优先固定 commit。

每次 `make test` 都先完成本次隔离安装并执行实际被测程序的 `offckb --version`，以 stdout 输出作为 `OffCKB 版本`，并与包内 `package.json` 版本核对。命令失败、输出为空或多行、版本不一致都会在用例收集前报错；stdout、stderr 和命令记录保存在本次运行目录的 `commands/artifact-version.*`。后续全部用例复用这一次安装和同一个可执行入口。

确认版本后在日志最前面打印独立的 `OffCKB 被测版本与来源` 区块，随后显示 `TEST-MAP` 检查结果和 pytest 日志。`RELEASE / npm 官方发布包` 表示 npm 发布版，`SOURCE / 源码编译（develop）` 表示从 `develop` 构建，并列出完整 Git commit。来源由准备包时的记录提供；`--version` 本身不提供分支信息。显式传入的 tarball 显示为 `TARBALL`，调试入口显示为 `ENTRY`，同样执行版本命令；调试入口没有包内版本可供核对。测试日志在 `-q` 或 `--no-header` 下也保留这个区块。

`make prepare` 完成时仅用一行显示包内版本和来源，例如 `OffCKB 已准备：0.4.13 | RELEASE / npm latest（包内版本）`。`--collect-only` 不安装或执行 CLI，显示 `OffCKB 待测包信息（尚未执行 --version）`。

## 本机配置

复制 `config/local.mk.example` 为 `config/local.mk`，填写本机 CKB 二进制路径。`local.mk` 已被 Git 忽略，不提交本机路径。变量值使用绝对路径，不加引号；保留示例中的 `?=`，即可用终端环境变量覆盖默认值。

Makefile 读取此文件，并把下表中的环境变量传给 pytest；直接运行 pytest 时不会读取 `local.mk`。一次性覆盖可以写成 `make test CKB_BIN=/absolute/path/to/ckb`。

| 配置变量 | pytest 参数 | 默认行为 / 用途 |
| --- | --- | --- |
| `OFFCKB_REPO` | `--offckb-repo` | 覆盖源码仓库地址，可选择 fork；`latest` 不使用该值 |
| `OFFCKB_REF` | `--offckb-ref` | 覆盖 `offckb.toml` 中的 `latest`、分支、tag、commit 或 `working-tree` |
| `CKB_BIN` | `--ckb-bin` | 指定本地 CKB 二进制；未设置时尝试产品源码同级的 `ckb/target/release/ckb` |
| `DEFAULT_CKB_BIN` | `--default-ckb-bin` | 默认前台启动使用的真实 CKB，版本必须与被测包的默认版本一致；未设置时复用 `CKB_BIN` 并检查版本 |
| `CKB_DEBUGGER_BIN` | `--ckb-debugger-bin` | 项目及合约调试、日志用例使用的本地原生 `ckb-debugger`；未设置时从 `PATH` 查找，复制到隔离工具目录使用 |
| `OFFCKB_SOURCE` | `--offckb-source` | 指定复用的本地 Git 仓库；自动查找 `source/offckb/`、`../offckb/`；该路径不决定分支版本 |
| `OFFCKB_PACKAGE` | `--offckb-package` | 验收已有 `.tgz`，跳过源码打包；`make prepare` 只准备 Python 环境 |
| `OFFCKB_ENTRY` | `--offckb-entry` | 仅用于调试：直接运行 `build/index.js`，跳过打包安装；与 package 互斥 |
| `PNPM_BIN` | `--pnpm-bin` | 从 `PATH` 查找；覆盖时指定可执行文件的绝对路径 |
| `PNPM_STORE_DIR` | `--pnpm-store-dir` | 自动查询 pnpm store；只在缓存位于其他位置时指定 |
| `PNPM_CACHE_DIR` | `--pnpm-cache-dir` | 使用系统默认 pnpm metadata 缓存目录 |
| `NODE_BIN` | `--node-bin` | 直接运行 JavaScript 入口时使用的 Node.js；默认从 `PATH` 查找 |

默认前台启动用例通过已安装 CLI 的 `config list` 读取包默认 CKB 版本，再用本地二进制的 `--version` 核对。测试只在新用户目录中准备该版本的托管二进制，随后执行普通 `offckb node`，不预写版本设置。若日常业务用例的 `CKB_BIN` 版本不同，应另外配置 `DEFAULT_CKB_BIN`；更换被测 OffCKB 包后，其默认 CKB 版本也可能变化，不匹配时测试会明确报错。

项目创建、构建和运行，以及使用真实项目合约的调试、日志用例另需原生 `ckb-debugger`，用 `CKB_DEBUGGER_BIN` 指定路径；系统脚本等其他用例不要求此工具。这两个配置都指向预先准备好的可执行文件，`make prepare` 不下载它们，核心用例也不会在缺少工具时自动下载。工具副本或链接仅放入本次隔离目录，不修改开发者的 OffCKB 配置或工具缓存。

源码安装、构建和测试应使用同一个 pnpm。机器上有多个版本时，在 `config/local.mk` 固定 `PNPM_BIN`，避免不同终端的 `PATH` 选择了不同版本。`make prepare` 在安装前检查 pnpm 10，不匹配时直接报错并提示配置路径；安装使用 `CI=true` 和逐行输出，不等待重装 `node_modules` 的交互确认，需要重建依赖目录时由 pnpm 自动处理。

`make prepare` 为 `latest` 准备包和依赖缓存，源码模式安装构建依赖并构建。测试时使用 `--prefer-offline --ignore-scripts` 安装被测 tarball，缓存缺失时仍可能访问 registry。直接指定 `OFFCKB_PACKAGE` 时须提前准备依赖缓存。

生成项目的依赖与 CLI 自身不同。项目用例默认设置 `npm_config_offline=true`，通过 `PNPM_STORE_DIR` / `PNPM_CACHE_DIR` 复用 pnpm 缓存；OffCKB 的 HOME/XDG 仍保持隔离。首次缺少项目依赖时，显式运行 `make test TESTS=tests/test_project_scaffolding.py ARGS='--project-online'` 允许下载并填充缓存，该次项目测试带 `network` marker，随后恢复普通 `make test`。只选择 `network` marker 本身不会授权联网，必须提供 `--project-online`。

直接提供 `OFFCKB_PACKAGE` 或 `OFFCKB_ENTRY` 时，它们优先于版本配置。tarball 显示自身的包版本和 SHA256，不附加无依据的 Git commit；entry 明确显示为调试入口。版本配置本身不依赖 `make`，直接 pytest 也读取 `offckb.toml`，但本机的 `local.mk` 仅由 Makefile 读取。

## 聚焦和诊断

`TESTS` 选择模块或 pytest node ID，`ARGS` 透传额外 pytest 参数。所有场景都复用同一个入口：

```bash
make test TESTS=tests/test_devnet_lifecycle.py
make test TESTS=tests/test_default_node.py DEFAULT_CKB_BIN=/absolute/path/to/package-default-ckb/ckb
make test ARGS='--collect-only'
make test ARGS='--durations=0 --durations-min=0 --keep-runtime'
```

默认运行 `core`，需要选择其他 marker 时用 `ARGS='-m <marker>'` 覆盖。其他参数包括 `--startup-timeout`（默认 120 秒）和 `--tx-timeout`（默认 180 秒）；可通过 `make test ARGS='--help'` 查看完整 pytest 帮助。

pytest 启动时自动检查 `TEST-MAP`，检查失败则直接退出，成功结果在版本区块之后显示。直接运行 pytest 也执行同样检查。维护评审文档时，也可单独运行 `python3 scripts/check_test_map.py`；其 `--require-complete` 参数要求所有评审用例都有映射，尚未实现的后续批次会使该模式非零退出。

需要直接调用 pytest 的 CI 或工具仍可使用 `.venv/bin/python -m pytest -c pyproject.toml -vv -m core tests --ckb-bin /absolute/path/to/ckb --default-ckb-bin /absolute/path/to/package-default-ckb/ckb --ckb-debugger-bin /absolute/path/to/ckb-debugger`。只运行不需要相应工具的模块时，可以省略后两个参数。保留 `-c pyproject.toml` 和测试路径，避免外部路径参数影响 pytest 配置发现。
