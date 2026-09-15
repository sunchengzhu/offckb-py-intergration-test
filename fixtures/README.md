# 集成测试 Fixture

这里只保存多个测试共同复用的输入。一次性数据优先在用例内直接准备。

- `build_contract_call.cjs`：根据生成项目的系统脚本和部署记录，使用公共 CCC SDK 构造、签名真实合约调用，返回交易哈希和 JSON-RPC 交易。Python fixture 随后通过 OffCKB proxy 提交，供失败调试和日志用例观察。
- `use_system_scripts.cjs`：使用 OffCKB 展示的账户锁和依赖签名、提交一笔消费指定输入的交易，供系统脚本用例验证导出内容确实可用。
- `build_direct_diagnostic_call.cjs`：构造资金交易和包含两种可识别脚本的调用，由 Python 直接提交到 CKB 并确认；用于验证未缓存交易的获取和单脚本调试。
- `parse_system_script_exports.cjs`：用公共 CCC SDK 解析 OffCKB 的 CCC 导出，返回代表脚本供 Python 与真实链核对。Lumos 导出在 Python 中检查 JSON、公开字段格式及链上引用，无需额外安装 Lumos。

这些辅助程序从隔离安装的被测包解析 `@ckb-ccc/core` 公共 SDK；不导入 OffCKB 的 `src/`，也不替 OffCKB 生成 debugger 的完整交易文件。私钥只通过 `--privkey-file` 指定的文件读取，生成的请求 JSON 不携带私钥。pytest 负责准备输入、记录脱敏命令和核对链上结果，无需单独运行辅助程序。

合约样例在临时项目中生成，用原有项目脚本安装依赖、构建和部署。调试与日志用例因此带有 `project` marker，使用真实本地 CKB、原生 `CKB_DEBUGGER_BIN` 和默认离线的 pnpm 项目依赖缓存；运行方式和显式下载选项见 [测试说明](../tests/README.md)。
