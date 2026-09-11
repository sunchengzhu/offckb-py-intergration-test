# 产品源码工作区

本目录用于放置或链接被测 OffCKB 源码，不存放测试代码。`offckb/` 和 `.prepared/` 已被 `.gitignore` 排除。

被测版本在 `../config/offckb.toml` 配置，默认 `latest` 下载 npm 最新发布包，无需源码。改为 `develop` 等源码模式时，`make prepare` 复用本地仓库中的 Git 对象，必要时获取缺失对象，并从选定 commit 导出临时源码进行构建打包；不会切换或重置已有工作区。没有可复用源码目录时才克隆。显式使用 `working-tree` 才构建本地未提交内容。

`.prepared/` 只缓存一个已准备的发布包及其来源元数据。`make test` 校验目标配置和包 SHA256 后复制该包到本次隔离目录，不重新获取分支或使用别的构建产物。

在测试工程根目录执行（仅在目标不存在时）：

```bash
ln -s /absolute/path/to/offckb source/offckb
```

也可以不创建链接，通过 `--offckb-source /absolute/path/to/offckb` 或 `OFFCKB_SOURCE` 指定源码。未显式指定时，依次查找本目录的 `offckb/`、测试工程同级的 `../offckb/`。

默认 `latest`、`--offckb-package` 或 `--offckb-entry` 不要求源码存在。遵循 `../AGENTS.md`，不要覆盖已有目录，也不要把产品源码提交到测试仓库。
