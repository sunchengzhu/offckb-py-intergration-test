"""Default OffCKB-issued SUDT and xUDT end-to-end Fiber flows."""
import pytest

from tests.asset_assertions import assert_udt_balance

from .udt_flows import KnownUdtFundingDefect, open_udt_channel, pay_udt_invoice, prepare_udt

pytestmark = pytest.mark.fiber

# Known FNN 0.9.0 defect: the acceptor's UDT change output is inserted at outputs[1] and
# shifts the outputs the initiator already put in place, so funding collaboration fails.
# Upstream report: https://github.com/nervosnetwork/fiber/issues/1667
ACCEPTOR_CHANGE_DEFECT = "known FNN 0.9.0 defect: acceptor UDT change shifts existing outputs (fiber#1667)"


# TEST-MAP: FIB-UDT-06
@pytest.mark.parametrize("kind", ["sudt", "xudt"])
def test_documented_zero_acceptor_funding_completes_first_udt_payment(fiber_environment, kind):
    """按使用说明零出资接受 UDT 通道，以默认费率完成首次支付。"""
    try:
        with fiber_environment("combined-daemon") as env:
            asset = prepare_udt(env, kind)
            channel = open_udt_channel(env, asset, funded_units=(5_000, 0))
            pay_udt_invoice(env, asset, channel)
    except KnownUdtFundingDefect as error:
        # Only an evidence-backed reported issue is expected; successful flows remain PASSED.
        pytest.xfail(str(error))


# TEST-MAP: FIB-UDT-01
@pytest.mark.parametrize("kind", ["sudt", "xudt"])
def test_issue_and_distribute_default_fiber_asset(fiber_environment, kind):
    with fiber_environment("combined-daemon") as env:
        prepare_udt(env, kind)


# TEST-MAP: FIB-UDT-02
@pytest.mark.xfail(reason=ACCEPTOR_CHANGE_DEFECT, raises=KnownUdtFundingDefect, strict=False)
@pytest.mark.parametrize("kind", ["sudt", "xudt"])
def test_manually_accept_default_udt_channel(fiber_environment, kind):
    with fiber_environment("combined-daemon") as env:
        asset = prepare_udt(env, kind)
        open_udt_channel(env, asset)


# TEST-MAP: FIB-UDT-03
@pytest.mark.xfail(reason=ACCEPTOR_CHANGE_DEFECT, raises=KnownUdtFundingDefect, strict=False)
@pytest.mark.parametrize("kind", ["sudt", "xudt"])
def test_pay_one_hundred_raw_udt_units(fiber_environment, kind):
    with fiber_environment("combined-daemon") as env:
        asset = prepare_udt(env, kind)
        channel = open_udt_channel(env, asset)
        pay_udt_invoice(env, asset, channel)


# TEST-MAP: FIB-UDT-04
@pytest.mark.parametrize("kind", ["sudt", "xudt"])
@pytest.mark.xfail(reason=ACCEPTOR_CHANGE_DEFECT, raises=KnownUdtFundingDefect, strict=False)
def test_multi_cell_asset_funds_a_dual_funded_udt_channel(fiber_environment, kind):
    """资产分散在多个 cell 时，双方多输入出资与双方找零都要正确落位。"""
    with fiber_environment("combined-daemon") as env:
        asset = prepare_udt(env, kind, batches=4)
        open_udt_channel(env, asset, funded_units=(5_000, 3_000))
        type_args = asset.script["args"]
        assert assert_udt_balance(env.runner, env.rpc, env.accounts[3], kind, type_args) == 5_000
        assert assert_udt_balance(env.runner, env.rpc, env.accounts[4], kind, type_args) == 7_000


# TEST-MAP: FIB-UDT-05
# Observed 2026-09-26 on FNN 0.9.0 at funding_fee_rate 2000: the funding transaction is built and
# broadcast, then CKB rejects it with `TransactionFailedToVerify … ScriptNotFound:
# code_hash 0x9bd7e06f… (secp256k1_blake160)`, i.e. the standard lock's cell dep is missing when
# the acceptor contributes its whole balance across several cells. The positional check passes
# here, which is what isolates fiber#1667 from this second defect. Pending an upstream report.
@pytest.mark.parametrize("kind", ["sudt", "xudt"])
def test_multi_cell_full_balance_funding_isolates_the_change_variable(fiber_environment, kind):
    """同布局的对照：接受方投入全部余额、不产生接受方找零，只有发起方有找零。

    费率显式设为 2000 以避开已提报的手续费缺陷（fiber#1663）：默认费率下该缺陷会先于
    本用例要判定的变量触发，使结果无法归因。
    """
    with fiber_environment("combined-daemon") as env:
        asset = prepare_udt(env, kind, batches=4)
        open_udt_channel(env, asset, funded_units=(5_000, 10_000), funding_fee_rate=2_000)
        type_args = asset.script["args"]
        assert assert_udt_balance(env.runner, env.rpc, env.accounts[3], kind, type_args) == 5_000
        assert assert_udt_balance(env.runner, env.rpc, env.accounts[4], kind, type_args) == 0
