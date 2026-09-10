# RPC 代理与日志用例评审

评审范围：后续批次的 devnet RPC proxy 基础转发、交易捕获、错误记录和非交互日志读取
源码版本：`develop@44ab81d`

## 接口说明

- 接口作用：在保持 CKB JSON-RPC 语义的同时记录请求、错误和待调试交易，并通过 `offckb logs` 提供诊断入口。
- 输入：direct/proxy JSON-RPC 请求、经 proxy 提交的交易，以及 `logs node|miner|rpc --tail --grep`。
- 成功结果：proxy 与 direct RPC 指向同一条链；事件日志可追溯；交易 JSON 可按 hash 找到；日志过滤稳定。
- 失败结果：上游 JSON-RPC 错误原样返回并形成一条安全的错误事件，日志本身不影响转发。
- 不负责：`logs --follow`、全屏状态、debugger 安装、长时间日志轮转和高并发压力。

## 待评审用例

| 用例 | 场景 | 预期结果 | 防止的问题 | 优先级 |
| --- | --- | --- | --- | --- |
| `OBS-01` | 对 direct RPC 和 proxy RPC 分别调用节点信息、genesis block hash 与 tip 接口 | 两端都返回合法 JSON-RPC 结果，node identity 和 genesis hash 一致，tip 都是同一持续增长链上的有效高度；`proxy.log` 为每个 proxy 调用追加对应的单行 request 事件 | proxy 连接到错误网络、错误节点或陈旧替身服务，或正常请求无法追溯 | P1 |
| `OBS-02` | 通过 proxy RPC 提交一笔有效交易并等待 committed | proxy 返回的 tx hash 与本地交易 hash及链上 hash一致；交易目录生成 `<txHash>.json` 且内容与提交交易一致；`proxy.log` 各有一条对应 request 和 `send_transaction <txHash>` 事件 | proxy 转发成功却丢失调试交易，或缓存文件与实际提交交易不匹配 | P1 |
| `OBS-03` | 通过 proxy 调用不存在的 JSON-RPC 方法 | 调用方收到与 direct RPC 等价的 JSON-RPC error code/message；`proxy.log` 记录一条 request 和一条对应 error 事件，proxy 随后仍能处理正常请求 | proxy 吞掉或改写上游错误，或一次错误使代理失去服务能力 | P1 |
| `OBS-04` | 在已有 node、miner 和 RPC 事件日志时，分别执行 `logs node\|miner\|rpc --tail N --grep <text> --json` | stdout 只给出命令完成结果；stderr 中的日志消息来自正确目标文件，最多返回最后 N 行且全部包含筛选文本，不混入另一目标的内容 | 诊断命令读错文件、tail 边界失效，或 grep 后仍混入无关日志 | P1 |
| `OBS-05` | 通过 proxy 发送方法名中包含换行、制表符及其他控制字符的 JSON-RPC 请求 | 请求仍按上游语义完成；每个 proxy 事件保持单个物理行，控制字符被替换且不能伪造额外 request/error 记录；随后正常请求仍成功 | 不可信 RPC 内容实施日志注入，伪造审计事件或破坏后续解析与代理可用性 | P1 |

## 后续批次说明

- `OBS-01` 至 `OBS-05` 不进入首期核心流程自动化，也不作为首期发布门禁。
- 首期只在 `NODE-01` 中确认产品启动后 proxy RPC 可用；不会单独断言日志格式、交易缓存或异常恢复。
- 核心流程稳定后，再整体评审并实现本文件的可观测性用例。
