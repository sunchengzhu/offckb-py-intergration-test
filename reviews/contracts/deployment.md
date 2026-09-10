# 合约部署用例评审

评审范围：纯 devnet 上确定性小型二进制的普通部署、Type-ID 首次部署和升级
源码版本：`develop@44ab81d`

## 接口说明

- 接口作用：把本地合约字节写入链上 code cell，并保存后续加载或升级所需的部署记录。
- 输入：`deploy --yes --target --output`，可选 `--type-id`，以及私钥文件。
- 成功结果：部署交易 committed；`deployment.toml`、migration 和 `scripts.json` 与链上 live cell 一致。
- 失败结果：升级前置记录无效时不消费旧 cell、不写入误导性新记录。
- 不负责：重新构建仓库内 Rust/C 合约子模块、超过 500 KiB 的二进制和多网络发布矩阵。

## 待评审用例

| 用例 | 场景 | 预期结果 | 防止的问题 | 优先级 |
| --- | --- | --- | --- | --- |
| `DEPLOY-01` | 使用私钥文件和 `--yes` 部署一个小型确定性二进制，且不启用 Type ID | 部署交易最终 committed；输出目录生成该合约的 `deployment.toml`、migration 和 `scripts.json`；记录的 outpoint 为 live，cell data 与输入字节完全一致，data hash 与链上一致，记录的 occupied capacity 与合约 data 长度一致，code cell 没有 Type-ID type script | CLI 报告部署成功但字节未上链，或部署记录与真实 cell 不一致 | P0 |
| `DEPLOY-02` | 对尚无部署记录的同名二进制执行首次 `deploy --type-id` | 交易最终 committed；记录包含非空 Type ID，链上 code cell 的 Type-ID script hash 与记录一致，cell data 与输入字节一致 | 可升级部署没有创建有效 Type ID，导致后续无法安全定位旧 code cell | P0 |
| `DEPLOY-03` | 修改同名二进制内容，并复用原输出目录再次执行 `deploy --type-id` | 升级交易最终 committed；旧 code cell 被消费，新 code cell 保存新字节且保持相同 Type-ID args/hash；新增 migration 和 `scripts.json` 指向新 live outpoint | “升级”实际创建了另一份身份不同的合约，或记录仍指向已消费 cell | P0 |
| `DEPLOY-04` | 首次 Type-ID 部署后篡改最新 migration 中的 Type ID，再尝试升级 | 命令非零退出且不生成新的成功 migration；旧 code cell 仍为 live、内容不变，错误明确指出记录与链上 Type ID 不匹配 | 损坏或被替换的部署记录导致消费错误 cell，或失败后留下伪成功记录 | P1 |

## 本轮需要确认

- 首期部署 fixture 使用小型确定性字节，不把合约语言编译器正确性混入 `deploy` 验收。
- 请确认 `DEPLOY-01` 至 `DEPLOY-04` 是否构成首期部署验收范围。
