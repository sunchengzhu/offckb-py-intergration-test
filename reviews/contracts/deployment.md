# 合约部署与升级用例评审

评审范围：用户将已有合约发布到本地链、取得可复用部署信息，以及修改合约后的升级
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
| `DEPLOY-01` | 用户已有一个小型合约二进制，希望用自己选择的非默认开发账户部署到本地链，通过私钥文件执行 `deploy --yes` 并选择普通部署 | 部署交易最终 committed；输出目录生成该合约的 `deployment.toml`、migration 和 `scripts.json`；记录与链上 code cell 的 lock 均等于指定账户的 lock；记录的 outpoint 为 live，cell data 与输入字节完全一致，data hash 与链上一致，记录的 occupied capacity 与合约 data 长度一致，code cell 没有 Type-ID type script | 忽略私钥参数后记录与链上一起归属错误账户，CLI 报告成功但字节未上链，或记录失真 | P0 |
| `DEPLOY-02` | 用户计划以后升级合约，使用非默认开发账户的私钥文件，为尚无部署记录的同名二进制首次执行 `deploy --type-id` | 交易最终 committed；记录与链上 code cell 的 lock 均等于指定账户的 lock；记录包含非空 Type ID，链上 Type-ID script hash 与记录一致，cell data 与输入字节一致 | 可升级部署归属错误账户或没有创建有效 Type ID，导致后续无法升级 | P1 |
| `DEPLOY-03` | 用户修改已经发布的可升级合约，保持产物名称和部署输出目录不变，用首次部署时同一个非默认账户的私钥文件再次执行 `deploy --type-id` | 升级交易最终 committed；升级前后的记录与 code cell lock 均等于指定账户的 lock；旧 code cell 被消费，新 code cell 保存新字节且保持相同 Type-ID args/hash；新增 migration 和 `scripts.json` 指向新 live outpoint | 升级归属错误账户、创建另一份身份不同的合约，或记录仍指向已消费 cell | P1 |
| `DEPLOY-04` | 用户已有 Type-ID 合约，但保存的最新 migration 中 Type ID 损坏或被改动，此时尝试继续升级 | 命令非零退出且不生成新的成功 migration；旧 code cell 仍为 live、内容不变，错误明确指出记录与链上 Type ID 不匹配 | 损坏或被替换的部署记录导致消费错误 cell，或失败后留下伪成功记录 | P1 |

## 使用边界

- 已有二进制的直接部署与生成项目的构建—部署—调用都属于用户路线；这里采用小型确定性产物检验部署命令，生成项目的真实调用见[项目创建与运行](../projects/scaffolding.md)。
- 首次普通部署是入门主线；Type-ID 创建与升级属于进阶流程，保留账户归属、身份不变、字节和记录更新的完整核对。
- 不把合约语言编译器正确性或任意字节可否作为业务合约执行，混入单独的部署命令验收。
