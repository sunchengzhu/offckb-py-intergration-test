.DEFAULT_GOAL := test
.NOTPARALLEL:
.PHONY: prepare test

-include config/local.mk

PYTHON ?= python3.11
TESTS ?= tests
ARGS ?=

export CKB_BIN OFFCKB_SOURCE OFFCKB_PACKAGE OFFCKB_ENTRY
export OFFCKB_REPO OFFCKB_REF
export PNPM_BIN PNPM_STORE_DIR PNPM_CACHE_DIR NODE_BIN
export DEFAULT_CKB_BIN CKB_DEBUGGER_BIN FNN_BIN

prepare:
	@set -e; \
	if [ -z "$${OFFCKB_PACKAGE:-}$${OFFCKB_ENTRY:-}" ]; then \
		pnpm_version=$$("$${PNPM_BIN:-pnpm}" --version); \
		case "$$pnpm_version" in \
			10.*) printf '准备 OffCKB：pnpm %s (%s)\n' "$$pnpm_version" "$${PNPM_BIN:-pnpm}" ;; \
			*) printf '需要 pnpm 10，当前为 %s。请在 config/local.mk 中设置 PNPM_BIN 为 pnpm 10 的绝对路径。\n' "$$pnpm_version" >&2; exit 1 ;; \
		esac; \
	fi
	"$(PYTHON)" -m venv .venv
	.venv/bin/python -m pip install -e .
	.venv/bin/python scripts/offckb_target.py prepare

test:
	@test -x .venv/bin/python || { echo '请先运行 make prepare'; exit 1; }
	@.venv/bin/python -m pytest -c pyproject.toml -vv -m core $(TESTS) $(ARGS)
