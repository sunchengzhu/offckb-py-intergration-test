# UDT 生命周期用例评审

评审范围：纯 devnet 上 SUDT 与 xUDT 的发行、发现、转账和销毁
源码版本：`develop@44ab81d`

## 接口说明

- 接口作用：使用 devnet 内置脚本完成可验证的 UDT 生命周期。
- 输入：`udt issue`、UDT `balance`、带 `--udt-kind` 的 `transfer` 和 `udt destroy`。
- 成功结果：返回 kind、type args、receiver 和交易哈希；交易 committed；代币余额按精确整数变化。
- 失败结果：非法数量、type args 或余额不足在提交交易前被拒绝，原代币状态保持不变。
- 不负责：代币元数据、总供应量索引、压力上限和自定义扩展脚本。

自定义 xUDT args 使用 [RFC 0052](https://github.com/nervosnetwork/rfcs/blob/master/rfcs/0052-extensible-udt/0052-extensible-udt.md) 定义的零 flags，无扩展脚本；它与省略 flags 的默认 args 字节不同，用于检验 CLI 参数传递。

发行和转账按资产类型与持有者汇总数量，验证净增发量及余额变化，不限定单次交易的 cell 拆分或合并方式。

## 待评审用例

| 用例 | 场景 | 预期结果 | 防止的问题 | 优先级 |
| --- | --- | --- | --- | --- |
| `UDT-01` | 使用私钥文件向签名账户发行一个确定数量的 SUDT，并通过 OffCKB 查询发行前后的余额 | 返回 `kind: sudt`、接收地址、由 issuer lock hash 派生的 32-byte type args 和交易哈希；交易 committed 后，普通 `balance` 及按 kind/type args 过滤的查询均能发现该资产，数量与独立 RPC 汇总的 live cells 一致且增加精确发行量 | SUDT owner/type args 计算错误，或链上已有资产但 OffCKB 无法发现、过滤或汇总 | P0 |
| `UDT-02` | 使用私钥文件和区别于默认 owner hash 的合法自定义 args（owner hash 后追加 4-byte 零 flags）发行 xUDT，并通过 OffCKB 查询余额 | 返回 `kind: xudt`、完整自定义 args、接收地址和交易哈希；交易 committed，链上与普通及过滤 `balance` 均保留全部 args、正确分类为 xUDT，数量增加精确发行量；默认 args 对应的 xUDT 和 SUDT 余额不变 | 忽略或截断 `--type-args` 仍通过测试，xUDT 使用错误脚本、被误分类或无法被 CLI 发现 | P0 |
| `UDT-03` | 从持有者向第二个地址转移部分已发行 SUDT 或 xUDT，并通过 OffCKB 查询双方操作前后的余额 | 交易最终 committed；普通及过滤 `balance` 与独立 RPC 汇总一致，接收方增加指定数量、发送方减少同样数量，两方合计守恒，kind 与完整 type args 不变；按同一 lock/type 汇总输出，不限定 cell 拆分数量 | 代币转账发生增发、丢币、串币，或链上正确但 CLI 余额错误 | P0 |
| `UDT-04` | 持有者销毁小于其余额的 SUDT 或 xUDT 数量，并通过 OffCKB 查询操作前后的余额 | 交易最终 committed；普通及过滤 `balance` 与独立 RPC 汇总一致，持有者余额精确减少销毁量，剩余同类 live cells 的数量之和正确，其他地址余额不变 | 销毁数量错误、找零丢失、误消费其他持有者代币，或 CLI 展示未更新 | P0 |
| `UDT-05` | 分别使用零值、非十进制、超过 u128 的数量，以及长度错误的 SUDT/xUDT type args | 命令非零退出且不给出成功交易哈希；相关 UDT live cells 与账户余额均不变化 | 无效输入进入签名或链上阶段，造成截断、溢出或错误脚本 | P1 |
| `UDT-06` | 尝试销毁超过当前余额的 UDT 数量 | 命令在提交前明确报告余额不足；原 UDT cell 仍为 live，UDT 与 CKB 余额保持不变 | 余额不足仍构造或提交交易，导致失败路径消费 token 或 capacity | P1 |
| `UDT-07` | 尝试销毁账户持有的全部 UDT 余额 | 待确认：维持当前行为，在提交前拒绝全额销毁并要求至少保留 1 token；或者将产品改为支持把该账户余额销毁到 0。无论选择哪种，CLI 结果与链上最终状态必须一致 | 命令名暗示可以销毁任意持有量，但当前实现明确拒绝全额销毁，自动化可能固化错误的产品语义 | P1 |

## 本轮需要确认

- SUDT 和 xUDT 均列为首期主流程；首期不覆盖超过 100 个输入 cell 的资源上限。
- `UDT-07` 需要产品决定：首期是验收“拒绝全额销毁”，还是先修改产品以支持销毁到 0。
- 请确认 `UDT-01` 至 `UDT-07` 是否构成首期 UDT 验收范围。
