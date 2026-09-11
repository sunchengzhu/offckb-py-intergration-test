# 发现与使用内置脚本用例评审

评审范围：用户取得 OffCKB 提供的内置脚本及依赖，用于本地交易或项目配置
接口依据：`@offckb/cli@0.4.13`（`1551ef9`），并对照开发版 `0.5.0`（`b03577c`）的共同接口。

## 使用场景与验证边界

- 查看：`offckb system-scripts --network devnet`；SDK 配置使用 `--export-style ccc` 或 `--export-style lumos`。已核对版本没有 `--list` 选项。
- 保存文件：`offckb system-scripts --output <path>`。该入口输出包含 devnet、testnet、mainnet 的通用 JSON；指定 `--output` 时不会按 `--network` 或 `--export-style` 裁剪文件。
- 从当前运行的 devnet 核对代表性的常用账户锁和 xUDT 信息；引用为 dep group 时先解析其成员，再检查对应 code cell，不能把依赖组本身当作合约代码。
- 只用一个典型交易证明导出的脚本引用可用，不遍历全部内置脚本的协议规则。交易必须使用命令给出的脚本和依赖，不能由测试硬编码另一套正确引用来替代。
- SDK 导出的信息由各自支持的脚本集合决定，不要求所有格式包含完全相同的条目；公共网络信息只检查文件结构，不连接公共网络。

## 待评审用例

| 用例 | 场景 | 预期结果 | 防止的问题 | 优先级 |
| --- | --- | --- | --- | --- |
| `SYS-01` | 用户启动本地链后执行 `system-scripts --network devnet` 查找现成脚本，用导出的常用账户锁匹配自己的测试账户输入 cell，并使用导出的 cell deps 签名和花费这些输入 | 输出明确属于 devnet，代表性脚本的名称、code hash、hash type 和依赖与当前链一致；使用这些引用的交易最终 committed，原输入已被消费，无需用户自己寻找部署位置 | 列表能显示却混入其他网络或过期引用，用户照着配置仍无法发送交易；只给新输出填写锁而未实际执行它会漏报 | P0 |
| `SYS-02` | 用户把内置脚本配置带入自己的项目，分别查看 CCC、Lumos 导出，并用 `--output` 保存到自己选择的项目文件路径 | SDK 配置内容可解析，共同代表脚本的引用与当前 devnet 一致；指定位置生成非空 JSON 对象并包含 devnet、testnet、mainnet，devnet 内容与查看结果及本地链一致 | SDK 字段或导出路径错误，生成空文件或错误网络配置而阻碍项目接入 | P1 |
