from dataclasses import replace
from decimal import Decimal
from datetime import timedelta
from pathlib import Path
import json
import numpy as np
import pytest
from gymnasium.utils.env_checker import check_env
from market_env.clock import FrozenClock, utc
from market_env.config import Config
from market_env.data import DataError, load_dataset
from market_env.environment import MarketEnvironment, ReplayGymEnv
from market_env.replay import synthetic_bars


def environment(config=None, bars=None):
    return MarketEnvironment(synthetic_bars() if bars is None else bars, config or Config(),
                             FrozenClock(utc("2026-09-02T00:00Z")))


def test_startup_no_orders_and_no_lookahead():
    env = environment()
    assert env.get_orders() == [] and env.sim.fills == []
    assert env.get_portfolio()["equity"] == Decimal("100000")
    assert all(len(v) == 1 for v in env.get_observation()["history"].values())
    order = env.submit_order("KO", "buy", 5)
    assert order["status"] == "pending" and not env.sim.fills
    env.advance_replay()
    fill = env.sim.fills[0]
    assert fill["price"] == Decimal("60.10")  # Next open, not previous close or next close.
    assert fill["market_time"] == order["submitted_at"]
    assert fill["wall_time"] > fill["market_time"]
    assert all(len(v) == 2 for v in env.get_observation()["history"].values())


def test_fill_before_exposing_next_ohlcv():
    env = environment()
    env.submit_order("KO", "buy", 1)
    original = env.sim.process_open
    def audited(bar):
        assert env.market_time == utc("2026-09-01T13:31Z")
        assert all(len(h) == 1 for h in env.price_history.values())
        original(bar)
    env.sim.process_open = audited
    env.advance_replay()


def test_cash_reservations_cancel_and_idempotency():
    env = environment(replace(Config(), initial_cash="1000"))
    first = env.submit_order("KO", "buy", 10, "client-1")
    assert env.get_portfolio()["available_cash"] == Decimal("397.50")
    assert env.submit_order("KO", "buy", 10, "client-1")["id"] == first["id"]
    with pytest.raises(ValueError, match="different"):
        env.submit_order("KO", "buy", 11, "client-1")
    assert env.submit_order("KO", "buy", 10)["status"] == "rejected"
    assert env.cancel_order(first["id"])
    assert not env.cancel_order(first["id"])
    assert env.get_portfolio()["available_cash"] == 1000
    env.advance_replay()
    assert not env.sim.fills


@pytest.mark.parametrize("symbol,side,quantity,reason", [("BAD", "buy", 1, "unknown"),
                       ("KO", "short", 1, "side"), ("KO", "buy", 0, "positive integer"),
                       ("KO", "buy", 1.5, "positive integer"), ("KO", "buy", True, "positive integer"),
                       ("KO", "buy", 1001, "size limit"), ("KO", "sell", 1, "available shares")])
def test_rejections(symbol, side, quantity, reason):
    order = environment().submit_order(symbol, side, quantity)
    assert order["status"] == "rejected" and reason in order["reason"]


def test_opening_gap_rejection_preserves_other_reservations():
    bars = synthetic_bars()
    bars = [replace(b, open=Decimal(500), high=Decimal(501), low=Decimal(499), close=Decimal(500))
            if b.symbol == "KO" and b.start.minute == 31 else b for b in bars]
    env = environment(replace(Config(), initial_cash="1000"), bars)
    buy = env.submit_order("KO", "buy", 10)
    other = env.submit_order("JPM", "buy", 2)
    env.advance_replay()
    assert env.sim.orders[buy["id"]].status == "rejected"
    assert env.sim.orders[other["id"]].status == "filled"
    assert env.get_portfolio()["cash"] >= 0


def test_accounting_fees_slippage_cost_basis_and_share_reservations():
    env = environment(replace(Config(), fee_per_order="1", fee_per_share="0.01", slippage_bps="10"))
    env.submit_order("KO", "buy", 10)
    env.advance_replay()
    p = env.get_portfolio()
    assert p["cash"] == Decimal("99397.30")  # 60.16*10 + 1.10
    assert p["average_cost"]["KO"] == Decimal("60.27")
    sell = env.submit_order("KO", "sell", 6)
    assert env.get_portfolio()["available_shares"]["KO"] == 4
    assert env.submit_order("KO", "sell", 5)["status"] == "rejected"
    env.advance_replay()
    p = env.get_portfolio()
    assert p["holdings"]["KO"] == 4
    assert p["realized_pnl"] == Decimal("-1.84")  # 60.14*6 -1.06 -60.27*6
    assert p["fees"] == Decimal("2.16") and p["slippage_cost"] == Decimal("0.96")
    assert p["equity"] - Decimal("100000") == p["realized_pnl"] + p["unrealized_pnl"]
    env.submit_order("KO", "sell", 4)
    env.advance_replay()
    assert env.sim.book_cost["KO"] == 0 and env.get_portfolio()["holdings"]["KO"] == 0


def test_missing_prices_not_fabricated_orders_wait_for_actual_bar():
    bars = [b for b in synthetic_bars() if not (b.symbol == "KO" and b.start.minute == 31)]
    env = environment(bars=bars)
    env.submit_order("KO", "buy", 1)
    env.advance_replay()
    assert not env.sim.fills and env.get_observation()["missing"]["KO"]
    assert env.submit_order("KO", "buy", 1)["status"] == "rejected"
    env.advance_replay()
    assert env.sim.fills[0]["market_time"] == utc("2026-09-01T13:32Z")


def test_gym_validation_determinism_reward_endings():
    config = replace(Config(), fee_per_order="1")
    env = ReplayGymEnv(synthetic_bars(), config)
    check_env(env, skip_render_check=True)
    obs, info = env.reset(seed=1)
    assert env.observation_space.contains(obs) and obs["mask"].sum() == 3
    first = env.step(np.array([2, 0, 0], dtype=np.int64))
    assert first[1] == -0.50  # 2*(60.35 - 60.10) - 1 fee, charged once.
    env.reset(seed=1)
    second = env.step(np.array([2, 0, 0], dtype=np.int64))
    assert first[1:] == second[1:]
    for key in first[0]:
        np.testing.assert_array_equal(first[0][key], second[0][key])
    while not env.core.ended:
        result = env.step(np.zeros(3, dtype=np.int64))
    assert result[2] and not result[3] and env.core.get_portfolio()["holdings"]["KO"] == 2
    with pytest.raises(RuntimeError, match="ended"):
        env.step(np.zeros(3, dtype=np.int64))


def test_unfillable_orders_cancel_at_end():
    bars = [b for b in synthetic_bars() if b.symbol != "KO" or b.start.minute == 30]
    env = environment(bars=bars)
    order = env.submit_order("KO", "buy", 1)
    while env.advance_replay():
        pass
    assert env.sim.orders[order["id"]].status == "canceled" and not env.sim.fills
    assert env.get_portfolio()["available_cash"] == env.get_portfolio()["cash"]


def test_gym_missing_mask_invalid_actions_custom_reward():
    bars = [b for b in synthetic_bars() if not (b.symbol == "KO" and b.start.minute == 31)]
    env = ReplayGymEnv(bars, reward_fn=lambda a, b: 7.)
    env.reset()
    with pytest.raises(ValueError):
        env.step([1.2, 0, 0])
    obs, reward, *_ = env.step(np.zeros(3, dtype=np.int64))
    assert reward == 7. and obs["mask"][-1, 0] == 0 and not obs["history"][-1, 0].any()


def test_single_session_corporate_action_boundary():
    bars = synthetic_bars()
    second = [replace(b, start=b.start + timedelta(days=1), end=b.end + timedelta(days=1)) for b in bars]
    with pytest.raises(DataError, match="ONE NYSE session"):
        environment(bars=bars + second)


def test_unique_export_auditable_and_secret_free(tmp_path):
    env = environment()
    env.submit_order("KO", "buy", 1)
    env.advance_replay()
    first, second = env.export_run(tmp_path), env.export_run(tmp_path)
    assert first != second
    bars, meta = load_dataset(first / "observed.csv")
    assert len(bars) == 6 and meta["feed"] == "SYNTHETIC"
    assert json.loads((first / "fills.json").read_text())[0]["wall_time"]
    assert json.loads((first / "versions.json").read_text())["alpaca-py"] == "0.44.0"


def test_no_brokerage_or_learning_path():
    source = Path(__file__).parents[1] / "src" / "market_env"
    text = "\n".join(p.read_text() for p in source.glob("*.py"))
    for forbidden in ("TradingClient", "alpaca.trading", "submit_order_data", "torch", "stable_baselines", "tensorflow", "class Agent"):
        assert forbidden not in text
