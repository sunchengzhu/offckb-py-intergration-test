# Fiber 配置与诊断用例评审

评审范围：用户查看真实运行状态、定位节点日志，并调整节点数量、持久配置和 FNN 版本来源。

版本依据：OffCKB `0.5.0-canary-ee0ad6b`，FNN `0.9.0`。默认工具已在隔离目录准备，网络下载另见安装评审。

## 待评审用例

| 用例 | 场景 | 预期结果 | 防止的问题 | 优先级 |
| --- | --- | --- | --- | --- |
| `FIB-CFG-01` | 用户在尚未创建 Fiber、后台双节点正常运行、只停止独立 FNN 后分别执行 `offckb --json fiber status` | 每次 stdout 只有一个结果 JSON；未创建时节点列表为空；运行时列出实际节点的 RPC、P2P、账户、版本和身份，状态与直接 RPC 查询一致；只停止 FNN 后显示 FNN stopped、原 CKB running | 把旧记录当作正在运行的环境，或展示错误节点和版本，误导后续操作 | P1 |
| `FIB-CFG-02` | 用户用 `fiber logs --node 1 --tail 5` 查看末尾日志，再用 `--tail 0 --follow` 观察该节点的新日志，随后中断跟随；另外查询不存在的节点 | 历史输出对应节点 1 日志末尾至多 5 行，跟随只显示此后新增的该节点日志；中断跟随不停止 CKB/FNN；不存在的节点返回可定位错误 | 日志串到其他节点、tail 数量错误、跟随不更新，或退出日志查看器误停服务 | P1 |
| `FIB-CFG-03` | 用户在已停止的双节点环境用 `fiber start --nodes 3` 扩为三节点，停止后不带数量重启，再停止并用 `--nodes 2` 缩回双节点 | 三节点各有独立身份、账户和端口，节点 1 自动连接节点 2/3；省略数量沿用三节点；缩减后只运行节点 1/2，保留其身份与配置，提示节点 3 已移出列表但其目录和数据保留 | 节点数量设置无法沿用，扩容串用账户，或缩减节点时静默删除实验数据 | P1 |
| `FIB-CFG-04` | 用户在已停止环境的 `fiber/nodes.yml` 为节点 1 设置合法 `fiber.announced_node_name`，连续两次启动验证；随后尝试覆盖该节点的 `ckb.rpc_url` 并启动，删除该覆盖后重试 | 两次启动的 `node_info.node_name` 均为设置值，节点 2 保持原名称；链、账户和互联保持正确；覆盖托管 RPC 地址时明确指出受限字段并拒绝启动；移除该项后在原环境正常启动 | 持久配置重启丢失、设置串到别的节点，或用户误改链连接后仍被当作正常环境使用 | P1 |
| `FIB-CFG-05` | 用户设置并读取 `fnn-version=0.8.0` 后启动 Fiber，再显式指定 `fiber start 0.9.0`；最后把默认版本改回 `0.9.0` 并省略版本重启 | 默认选择 `0.8.0` 时说明本包仅支持托管 `0.9.0` 并失败；显式 `0.9.0` 覆盖默认选择且不改写持久设置，实际 RPC 版本为 `0.9.0`；修正默认设置后无版本参数也能启动 | 忽略用户版本设置或命令行覆盖，静默运行其他版本，或把语法合法当作本包支持 | P1 |
| `FIB-CFG-06` | 用户将默认版本设为不受托管支持的 `0.8.0`，通过 `fiber start --binary-path` 或 `node --fiber --fnn-binary-path` 指定真实本地 FNN `0.9.0`；分别使用旁边带合法模板和无模板的目录布局 | 实际运行指定路径的 FNN，RPC 报告真实版本，未进入托管版本下载；有模板时采用旁边模板，无模板时使用 OffCKB 随包模板；两种布局均生成可互联、链与资金账户正确的节点 | 指定本地二进制后仍走托管下载，或缺少旁边模板便无法使用本地构建 | P1 |
| `FIB-CFG-07` | 用户只想运行一个本地 FNN 查看接口，通过 `node --fiber --fiber-nodes 1` 或已有链上的 `fiber start --nodes 1` 启动单节点环境 | 只启动节点 1，实际 FNN 版本、链和 account 3 资金账户正确；RPC 可用、CKB 持续产块，`list_peers` 为空仍可正常就绪，不等待不存在的第二个节点 | 支持的单节点配置仍按双节点等待互联，导致用户无法使用单节点环境 | P1 |

## 判定与范围边界

- 节点数调整在环境停止且无未结束通道时进行；联合启动入口对应 `--fiber-nodes`，与 `fiber start --nodes` 均验证数量选择。代表性三节点只验证 OffCKB 自动互联，不扩展为多跳支付矩阵。
- `nodes.yml` 是持久覆盖入口；节点目录的 `config.yml` 为每次启动生成的文件。以实际 RPC 字段、节点身份和账户验证设置生效，不只检查 YAML 文本。
- 日志来自真实 FNN 运行；JSON 模式的日志内容通过 stderr 输出，stdout 保持单个命令结果。状态反映服务和身份，不代替开通道、支付的成功判定。
- 本包托管版本仅有 `0.9.0`；`0.8.0` 只用于验证不支持提示和显式选择优先级，不下载安装，也不承担跨版本数据库兼容测试。

依据：[OffCKB 状态](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/fiber/status.ts)、[日志入口](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/cmd/fiber.ts)、[节点配置](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/fiber/nodes-yml.ts)、[二进制选择](https://github.com/ckb-devrel/offckb/blob/ee0ad6bfd30f5af131da3ad7b4f4a29bf0af8ca2/src/fiber/install.ts)、[FNN node_info 字段](https://github.com/nervosnetwork/fiber/blob/e6cb7ac7770b1798a1ad5dfb9a8f4ae5db52036f/crates/fiber-json-types/src/info.rs)。
