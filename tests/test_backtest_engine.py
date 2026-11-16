"""Tests del simulador con datos sintéticos.  Correr:  python -m unittest discover tests"""

import unittest

import numpy as np

from backtest.engine import (Market, Signal, Trade, fee_per_share, kelly_fraction,
                             simulate_market, simulate_portfolio)
from backtest.report import roi_ci
from backtest.strategies import favorite


def mk(ps, payout=1.0, fee=0.0, end=None):
    ts = np.arange(len(ps), dtype=np.int64) * 3600
    return Market("m", "s", "q", "ev", "cat", end or int(ts[-1]), int(ts[-1]) + 1,
                  payout, fee, 1.0, ts, np.array(ps, dtype=float))


def buy_at_bar(k, side="YES"):
    return lambda v: Signal(side) if len(v.ps) == k + 1 and not v.side else None


class EngineTest(unittest.TestCase):
    def test_fills_at_next_bar_and_pnl_is_in_shares(self):
        # decide en barra 0 (0.20), ejecuta en barra 1 (0.40) → 1/0.40 shares → gana 1.5 por $1
        (t,) = simulate_market(mk([0.20, 0.40, 0.90]), buy_at_bar(0), half_spread=0)
        self.assertAlmostEqual(t.entry, 0.40)
        self.assertAlmostEqual(t.ret, 1 / 0.40 - 1)

    def test_loss_is_full_stake(self):
        (t,) = simulate_market(mk([0.5, 0.5, 0.1], payout=0.0), buy_at_bar(0), half_spread=0)
        self.assertAlmostEqual(t.ret, -1.0)

    def test_no_side_uses_complement(self):
        (t,) = simulate_market(mk([0.7, 0.7, 0.2], payout=0.0), buy_at_bar(0, "NO"), half_spread=0)
        self.assertAlmostEqual(t.ret, 1 / 0.3 - 1)

    def test_costs_reduce_return(self):
        (t,) = simulate_market(mk([0.5, 0.5, 0.9], fee=0.05), buy_at_bar(0), half_spread=0.01)
        self.assertAlmostEqual(t.entry, 0.5 + 0.01 + 0.05 * 0.25)
        self.assertAlmostEqual(fee_per_share(0.5, 0.05, 1), 0.0125)

    def test_strategy_never_sees_future_or_outcome(self):
        seen = []
        m = mk([0.1, 0.2, 0.3, 0.4])
        views = []
        simulate_market(m, lambda v: views.append(v) or seen.append((len(v.ps), v.price)) or None)
        self.assertEqual(seen, [(1, 0.1), (2, 0.2), (3, 0.3)])
        for hidden in ("payout_yes", "closed_ts", "volume"):
            self.assertFalse(hasattr(views[0], hidden))

    def test_no_fill_on_last_bar_or_above_cap(self):
        self.assertEqual(simulate_market(mk([0.5, 0.5]), buy_at_bar(1)), [])
        self.assertEqual(simulate_market(mk([0.5, 0.995, 1.0]), buy_at_bar(0)), [])

    def test_exit_sells_at_next_bar(self):
        def strat(v):
            if not v.side and len(v.ps) == 1:
                return Signal("YES")
            if v.side and len(v.ps) == 2:
                return Signal("EXIT")
        (t,) = simulate_market(mk([0.4, 0.4, 0.6, 0.0], payout=0.0), strat, half_spread=0)
        self.assertAlmostEqual(t.ret, 0.6 / 0.4 - 1)
        self.assertFalse(t.held_to_resolution)

    def test_favorite_strategy_picks_high_side(self):
        (t,) = simulate_market(mk([0.5, 0.15, 0.15, 0.0], payout=0.0), favorite(0.8, 0.95, 1e9))
        self.assertEqual(t.side, "NO")
        self.assertGreater(t.ret, 0)


class PortfolioTest(unittest.TestCase):
    def tr(self, o, c, ret, ev="e", prob=None, entry=0.5):
        return Trade("m", ev, "cat", "YES", o, c, entry, ret, prob)

    def test_flat_stake_compounds(self):
        pf = simulate_portfolio([self.tr(0, 1, 1.0), self.tr(2, 3, -1.0)], 100, stake_pct=0.1)
        self.assertAlmostEqual(pf.final, (100 + 10) * 0.9)
        self.assertAlmostEqual(pf.max_drawdown, 0.1)

    def test_capital_is_locked_while_open(self):
        pf = simulate_portfolio([self.tr(0, 10, 0.0), self.tr(1, 10, 0.0)], 100, stake_pct=0.6)
        self.assertEqual((pf.taken, pf.skipped), (1, 1))

    def test_kelly(self):
        self.assertAlmostEqual(kelly_fraction(0.6, 0.5), 0.2)
        self.assertEqual(kelly_fraction(0.4, 0.5), 0.0)
        pf = simulate_portfolio([self.tr(0, 1, 1.0, prob=0.6)], 100, kelly=0.5, max_position=1)
        self.assertAlmostEqual(pf.final, 110)

    def test_bootstrap_clusters_by_event(self):
        same = [self.tr(0, 1, 0.1, ev="a") for _ in range(50)]
        roi, lo, hi = roi_ci(same)
        self.assertAlmostEqual(roi, 0.1)
        self.assertAlmostEqual(lo, hi)  # un solo evento → sin información independiente


if __name__ == "__main__":
    unittest.main()
