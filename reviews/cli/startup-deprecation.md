# CLI 启动弃用警告用例评审

评审范围：打包并隔离安装后的 `offckb` 启动输出，针对 [OffCKB PR #543](https://github.com/ckb-devrel/offckb/pull/543) 修复的 `DEP0040 punycode` 警告。

## 导读

- **这个改动要做什么**：用户执行普通 OffCKB 命令时，不再看到与操作无关的 Node.js `punycode` 弃用警告。
- **行为变化**：修复前，较新 Node.js 或启用待弃用检查时，`fiber status` 启动阶段可能输出 `DEP0040`；修复后，同一命令正常返回状态，警告不再出现。
- **最怕出什么问题**：发布包仍加载会触发警告的依赖，使用户误以为命令出错；在 JSON 模式下，原始警告还会破坏 stderr 的 NDJSON 格式。
- **能测到什么**：通过隔离安装后的 `offckb` 可执行程序检查真实启动结果；未创建 Fiber 的环境即可运行 `fiber status`，无需启动 CKB/FNN，也不需要网络下载或链上状态。
- **范围边界**：本条只验证启动警告。PR 涉及的国际化域名转换不在本条观察范围；保留 HTTP 代理请求功能由[已有 Fiber 下载用例](../fiber/installation-platforms.md)中的 `network` 场景验证，普通 PR 门禁不运行这些场景。Node.js 多版本矩阵应作为单独标记的兼容运行。

依据：[PR #543 的问题描述、变更与验证](https://github.com/ckb-devrel/offckb/pull/543)；JSON stderr 契约见本工程 `AGENTS.md`。

## 需要你决定

无。

## 用例

本轮验收使用 PR #543 的固定 head 或确知包含该修复的发布包。命令使用独立的临时用户目录和 npm prefix；通过本工程的固定端口租约确认 devnet 端口空闲，避免把其他节点的 RPC 状态读成当前环境。选用支持 `--pending-deprecation` 与 `--throw-deprecation` 的 Node.js 20+，清除 Node 警告屏蔽设置。`--pending-deprecation` 让旧依赖导入稳定暴露警告，`--throw-deprecation` 使警告不能被成功退出码掩盖。

<!-- TEST-CASES-BEGIN -->
| 用例 | 场景 | 预期结果 | 防止的问题 | 优先级 |
| --- | --- | --- | --- | --- |
| `CLI-08` | 用户在全新隔离环境、devnet 固定端口空闲时用 Node.js 20+ 执行已安装包的 `offckb --json fiber status`；测试启用 `--pending-deprecation --throw-deprecation` 且不屏蔽 Node 警告 | 命令以 0 退出；stdout 恰有一个 `fiber.status` 结果 JSON，显示未创建 Fiber 且 CKB 已停止；stderr 为空或每个非空行都是完整 JSON，且不出现 `DEP0040` 或 `punycode` 弃用警告 | `node-fetch` 依赖链仍导入 Node 内置 `punycode`，导致正常命令出现警告、JSON 日志被污染或严格弃用模式下命令失败 | P2 |
<!-- TEST-CASES-END -->

## 测试树

<!-- TEST-TREE-BEGIN -->
- 已安装 CLI 的启动输出
  - 开启 Node.js 待弃用检查并查询未创建的 Fiber 环境
    - 命令成功、状态可读且无 `DEP0040`：CLI-08 [primary] -> SPEC-01
<!-- TEST-TREE-END -->

## Spec

### SPEC-01：普通命令启动时不输出 `punycode` 弃用警告
- Condition: 已安装包含 PR #543 修复的 OffCKB 包，在支持待弃用检查的 Node.js 上运行；用户环境隔离、未创建 Fiber，固定 devnet 端口空闲。
- Expected: `offckb --json fiber status` 成功报告空的 Fiber 环境；启动不触发 `DEP0040`，JSON 模式的 stderr 仍符合 NDJSON 契约。
- Observable: 进程退出码、stdout 中的单个命令结果 JSON，以及 stderr 的每一行。
- Basis: [OffCKB PR #543](https://github.com/ckb-devrel/offckb/pull/543) 明确要求消除该警告；本工程 `AGENTS.md` 规定 JSON 输出契约；[FIB-CFG-01](../fiber/configuration-diagnostics.md)已有未创建 Fiber 时的状态预期。
- Source: explicit
- Testing: required

## 验证设计

- **独立预期**：无 `DEP0040` 来自 PR 的修复目标；成功的 `fiber.status` 结果和 stderr NDJSON 规则分别来自既有状态评审与本工程 CLI 契约，不从被测实现推导。
- **实际入口与观察**：复用已有 `uninitialized_devnet` 与其固定端口租约，设置 Node.js 待弃用检查后调用隔离安装包的公开 `offckb --json fiber status`，直接检查退出码、stdout JSON 和原始 stderr。
- **证明边界**：证明当前被测包在该 Node.js 运行时的 CLI 启动行为；不证明所有 Node.js 版本、每条子命令、国际化域名转换或 HTTP 代理下载均通过。
