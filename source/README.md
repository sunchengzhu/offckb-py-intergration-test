# 产品源码工作区

本目录用于放置或链接被测 OffCKB 源码，不存放测试代码。`offckb/` 已被 `.gitignore` 排除。

在测试工程根目录执行（仅在目标不存在时）：

```bash
ln -s /absolute/path/to/offckb source/offckb
```

也可以不创建链接，通过 `--offckb-source /absolute/path/to/offckb` 或 `OFFCKB_SOURCE` 指定源码。未显式指定时，依次查找本目录的 `offckb/`、测试工程同级的 `../offckb/`。

使用 `--offckb-package` 或 `--offckb-entry` 时不要求源码存在。遵循 `../AGENTS.md`，不要覆盖已有目录，也不要把产品源码提交到测试仓库。
