# 代币发行与使用用例评审

评审范围：用户在本地 devnet 发行自己的 SUDT/xUDT、查看余额、转给其他账户，以及销毁实验资产
源码版本：`develop@44ab81d`

## 接口说明

- 接口作用：使用 devnet 内置脚本完成可验证的 UDT 生命周期。
- 输入：`udt issue`、UDT `balance`、带 `--udt-kind` 的 `transfer` 和 `udt destroy`。
- 成功结果：返回 kind、type args、receiver 和交易哈希；交易 committed；代币余额按精确整数变化。
- 失败结果：非法数量、type args 或余额不足在提交交易前被拒绝，原代币状态保持不变。
- 不负责：代币元数据、总供应量索引、压力上限和自定义扩展脚本。

`UDT-02` 的自定义资产标识使用合法的 owner hash 加 4-byte 零 flags，完整字节区别于默认 args，不加载扩展脚本。该数据用于确认 OffCKB 尊重用户指定的资产标识；不要求入门用户理解 flags，也不在这里验证 xUDT 扩展协议。其格式依据 [RFC 0052](https://github.com/nervosnetwork/rfcs/blob/master/rfcs/0052-extensible-udt/0052-extensible-udt.md)。

发行和转账按资产类型与持有者汇总数量，验证净增发量及余额变化，不限定单次交易的 cell 拆分或合并方式。

## 待评审用例

| 用例 | 场景 | 预期结果 | 防止的问题 | 优先级 |
| --- | --- | --- | --- | --- |
| `UDT-01` | 用户用开发账户的私钥文件给自己发行指定数量的 SUDT，通过 OffCKB 查看发行前后的余额，确认能找到自己的第一种代币 | 返回 `kind: sudt`、接收地址、由 issuer lock hash 派生的 32-byte type args 和交易哈希；交易 committed 后，普通 `balance` 及按 kind/type args 过滤的查询均能发现该资产，数量与独立 RPC 汇总的 live cells 一致且增加精确发行量 | SUDT owner/type args 计算错误，或链上已有资产但 OffCKB 无法发现、过滤或汇总 | P0 |
| `UDT-02` | 用户用私钥文件发行带指定资产标识的 xUDT，并通过 OffCKB 查询；自定义 args 与默认值不同，以确认 CLI 使用了用户选择 | 返回 `kind: xudt`、完整自定义 args、接收地址和交易哈希；交易 committed，链上与普通及过滤 `balance` 均保留全部 args、正确分类为 xUDT，数量增加精确发行量；默认 args 对应的 xUDT 和 SUDT 余额不变 | 忽略或截断 `--type-args` 仍通过测试，xUDT 使用错误脚本、被误分类或无法被 CLI 发现 | P0 |
| `UDT-03` | 用户把部分 SUDT 或 xUDT 发给第二个开发账户，并分别查看双方操作前后的 OffCKB 余额 | 交易最终 committed；普通及过滤 `balance` 与独立 RPC 汇总一致，接收方增加指定数量、发送方减少同样数量，两方合计守恒，kind 与完整 type args 不变；按同一 lock/type 汇总输出，不限定 cell 拆分数量 | 代币转账发生增发、丢币、串币，或链上正确但 CLI 余额错误 | P0 |
| `UDT-04` | 用户清理实验资产，销毁自己持有的部分 SUDT 或 xUDT，随后通过 OffCKB 确认剩余数量 | 交易最终 committed；普通及过滤 `balance` 与独立 RPC 汇总一致，持有者余额精确减少销毁量，剩余同类 live cells 的数量之和正确，其他地址余额不变 | 销毁数量错误、找零丢失、误消费其他持有者代币，或 CLI 展示未更新 | P1 |
| `UDT-05` | 用户发行、转移或销毁代币时填错数量或资产标识：零值、非十进制、超出支持范围的数量，或长度错误的 type args | 命令非零退出且不给出成功交易哈希；相关 UDT live cells 与账户余额均不变化 | 无效输入进入签名或链上阶段，造成截断、溢出或错误脚本 | P1 |
| `UDT-06` | 用户销毁测试代币时填入超过自己当前持有量的数量 | 命令在提交前明确报告余额不足；原 UDT cell 仍为 live，UDT 与 CKB 余额保持不变 | 余额不足仍构造或提交交易，导致失败路径消费 token 或 capacity | P1 |
| `UDT-07` | 用户结束代币实验，一次销毁自己持有的全部 SUDT 或 xUDT，并通过 OffCKB 查看结果 | 销毁交易最终 committed；该账户的目标代币余额归零，CLI 余额与独立 RPC 汇总一致，其他账户的代币余额不变 | 无法清空实验代币，或命令报告销毁成功但链上仍有余额、误销毁其他账户代币 | P1 |

## 使用边界

- 先完成发行、查看和转移一条资产路线，再覆盖销毁和错误输入。保留 SUDT/xUDT 的分类和完整资产标识核对，不展开大量输入 cell、u128 边界或扩展脚本的组合矩阵。
