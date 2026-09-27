"""用户通过 OffCKB 创建的环境开通通道、支付并收回可再次使用的 CKB。"""

from __future__ import annotations

import pytest

import json

from ..harness import CKB, hex_int, rpc_script
from .flows import (
    PAYMENT_AMOUNT, FiberEnvironment, assert_close_amounts, assert_returned_funds_spendable,
    close_channel, open_manually_accepted_ckb_channel, opened_ckb_channel, pay_invoice,
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


# TEST-MAP: FIB-06
def test_both_sides_fund_a_manually_accepted_ckb_channel(fiber_env: FiberEnvironment) -> None:
    """手工接受的双向注资：双方都出资并产生找零，资金只落在通道 cell 与两个出资账户。

    发起方只能投入 99 CKB（FNN 要求不低于接受方默认出资额，达到自动接受区间又会被自动接受），
    而每侧预留 99 CKB，因此该通道内发起方可用余额为 0：本用例判定通道与资金落位，不判定支付。
    """
    channel = open_manually_accepted_ckb_channel(fiber_env, 99 * CKB, 5_000 * CKB)
    assert channel.funding_transaction["tx_status"]["status"] == "committed"
    for balance, pledge in zip(channel.initial, (99 * CKB, 5_000 * CKB)):
        assert 0 <= hex_int(balance["local_balance"]) < pledge, "each side keeps its reserve inside the funding cell"
    # 预留吃满发起方的投入：这一侧在通道内没有可用余额（实测边界，不是断言产品应该如此）。
    assert hex_int(channel.initial[0]["local_balance"]) == 0
    funding_index = hex_int(channel.outpoint["index"])
    account_locks = {
        json.dumps(rpc_script(account.lock_script), sort_keys=True)
        for account in (fiber_env.accounts[3], fiber_env.accounts[4])
    }
    for index, output in enumerate(channel.funding_transaction["transaction"]["outputs"]):
        if index == funding_index:
            continue
        assert json.dumps(rpc_script(output["lock"]), sort_keys=True) in account_locks, (
            "the funding transaction must only return change to the two funding accounts")
