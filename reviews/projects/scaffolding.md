# 项目脚手架用例评审

评审范围：从发布包非交互创建 TypeScript 项目，到构建、部署并运行生成的测试
源码版本：`develop@44ab81d`

## 接口说明

- 接口作用：生成一套开箱可用的 CKB JavaScript VM 合约项目。
- 输入：`create --no-interactive --no-git --no-install --manager pnpm --language typescript` 和生成项目的 package scripts。
- 成功结果：模板完整、无残留占位符；依赖可安装；合约可构建、部署；生成的 mock/devnet 测试可运行。
- 失败结果：目标目录冲突时拒绝覆盖已有内容。
- 不负责：JavaScript 模板、交互式问答、Git 初始化、包注册表故障和 Windows WASI 兼容性。

## 待评审用例

| 用例 | 场景 | 预期结果 | 防止的问题 | 优先级 |
| --- | --- | --- | --- | --- |
| `PROJ-01` | 使用隔离安装的发布包非交互创建 TypeScript 项目，并指定合约名、`--no-install` 与 `--no-git` | 命令以 0 退出；生成 `package.json`、TypeScript 配置、构建/部署脚本、合约源码、mock/devnet 测试、`.env`、`deployment/scripts.json` 和 `system-scripts.json`；文件中没有未替换模板变量，且未创建 `node_modules` 或 `.git` | 发布包漏带模板、变量替换失败，或禁用选项仍产生非预期副作用 | P0 |
| `PROJ-02` | 目标目录已经存在并包含用户文件时再次执行 `create` | 命令非零退出并明确说明目录已存在；已有文件内容和目录结构完全不变 | 脚手架覆盖或混入用户已有项目，造成不可恢复的数据损失 | P1 |
| `PROJ-03` | 在 `PROJ-01` 生成的项目中安装锁定范围内的依赖并执行 build script | 依赖安装成功；build script 以 0 退出并生成指定合约的 `dist/<contract>.js` 与 `dist/<contract>.bc`，二进制非空且可被部署命令读取 | 新模板引用不兼容依赖、构建脚本路径错误，或生成项目只能创建不能构建 | P0 |
| `PROJ-04` | devnet ready 后，在生成项目中使用同一隔离 `offckb` 执行 deploy script | deploy script 以 0 退出；部署交易 committed；`deployment/scripts.json` 的 devnet 合约项、txHash 和 outpoint 与链上 live code cell 一致 | 生成脚本找错 CLI、目标目录或网络，出现表面成功但没有有效部署记录 | P0 |
| `PROJ-05` | 构建和部署完成后运行生成项目的 mock 与 devnet 测试 | 两类生成测试均被发现并以 0 退出；devnet 测试实际引用本次部署记录并提交、确认合约交易，而不是仅做静态导入 | 示例测试与模板、部署记录或当前依赖版本脱节，用户创建项目后无法验证合约 | P0 |

## 本轮需要确认

- `PROJ-03` 至 `PROJ-05` 属于较慢的工具链验收；实现时可使用依赖缓存，但不会把外部注册表中断判定为 offckb 产品失败。
- 现有 `scripts/create-test.sh` 作为这些用例的迁移来源；pytest 不会套壳调用该 shell 脚本。
- 请确认 `PROJ-01` 至 `PROJ-05` 是否构成首期脚手架验收范围。
