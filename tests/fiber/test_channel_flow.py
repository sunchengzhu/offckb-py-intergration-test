"""用户通过 OffCKB 创建的环境开通通道、支付并收回可再次使用的 CKB。"""

from __future__ import annotations

import pytest

from ..harness import hex_int
from .flows import (
    PAYMENT_AMOUNT, FiberEnvironment, assert_close_amounts, assert_returned_funds_spendable,
    close_channel, opened_ckb_channel, pay_invoice,
)


pytestmark = pytest.mark.fiber


# TEST-MAP: FIB-04
def test_default_nodes_open_a_confirmed_ckb_channel(fiber_env: FiberEnvironment) -> None:
    """默认节点自动接受并就绪，资金交易真实上链且有足够可用余额。"""
    with opened_ckb_channel(fiber_env) as channel:
        assert channel.funding_transaction["tx_status"]["status"] == "committed"
        assert all(c["state"]["state_name"] == "ChannelReady" for c in channel.initial)
        assert hex_int(channel.initial[0]["local_balance"]) >= PAYMENT_AMOUNT


# TEST-MAP: FIB-05
def test_invoice_payment_settles_exact_balances(fiber_env: FiberEnvironment) -> None:
    """普通发票支付完成后，双方余额精确变化且没有未结算转账。"""
    with opened_ckb_channel(fiber_env) as channel:
        paid = pay_invoice(fiber_env, channel)
        assert hex_int(paid[0]["local_balance"]) == hex_int(channel.initial[0]["local_balance"]) - PAYMENT_AMOUNT
        assert hex_int(paid[1]["local_balance"]) == hex_int(channel.initial[1]["local_balance"]) + PAYMENT_AMOUNT
        assert paid[0]["pending_tlcs"] == paid[1]["pending_tlcs"] == []


# TEST-MAP: FIB-CLOSE-01
def test_cooperative_close_returns_spendable_principal(fiber_env: FiberEnvironment) -> None:
    """支付后协作关闭，双方本金按实际费用返还，并消费返还 cell 完成转账。"""
    with opened_ckb_channel(fiber_env) as channel:
        pay_invoice(fiber_env, channel)
        fiber_env.rpc.wait_indexer(timeout_s=fiber_env.timeout_s)
        # A distinct address proves that close_script is honored, not ignored.
        recipients = (fiber_env.accounts[5], fiber_env.accounts[4])
        assert recipients[0].lock_script != fiber_env.accounts[3].lock_script
        before_close = tuple(fiber_env.rpc.ckb_balance(account.lock_script) for account in recipients)
        closing = close_channel(fiber_env, channel, recipient=recipients[0])
        returned = assert_close_amounts(fiber_env.rpc, channel, closing, recipients)
        assert_returned_funds_spendable(fiber_env, closing, before_close, returned, recipients)
