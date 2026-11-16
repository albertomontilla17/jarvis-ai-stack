"""Tests del paper trading.  Correr:  python -m unittest discover tests"""

import sqlite3
import unittest
from unittest import mock

import paper


class PickTest(unittest.TestCase):
    def test_model_buys_side_with_edge_after_fee(self):
        side, ask, prob = paper.sports_pick("deportes_modelo", 0.70, None, 0.58, 0.60)
        self.assertEqual((side, ask, prob), ("home", 0.60, 0.70))
        side, ask, prob = paper.sports_pick("deportes_modelo", 0.30, None, 0.58, 0.60)
        self.assertEqual(side, "away")
        self.assertAlmostEqual(ask, 0.42)
        # 0.64 − (0.60 + comisión 0.0168) < 0.05 → no opera
        self.assertIsNone(paper.sports_pick("deportes_modelo", 0.64, None, 0.58, 0.60))

    def test_adjusted_needs_adjustment_and_control_buys_favorite(self):
        self.assertIsNone(paper.sports_pick("deportes_ajustado", 0.9, None, 0.5, 0.52))
        self.assertEqual(paper.sports_pick("deportes_favorito", None, None, 0.30, 0.32)[0], "away")
        self.assertEqual(paper.sports_pick("deportes_favorito", None, None, 0.70, 0.72)[0], "home")


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = __import__("tempfile").mkdtemp()
        self.log = f"{self.tmp}/log.db"
        con = sqlite3.connect(self.log)
        con.execute("CREATE TABLE forecasts (gid TEXT, league TEXT, start_ts INTEGER, home TEXT, away TEXT,"
                    " p_model REAL, p_adj REAL, home_bid REAL, home_ask REAL, home_won INTEGER)")
        con.execute("INSERT INTO forecasts VALUES ('g1', 'nba', 1000, 'BOS', 'ORL', 0.80, 0.85, 0.60, 0.62, NULL)")
        con.commit()
        self.patch = mock.patch.multiple(paper, SPORTS_LOG=self.log, SCAN_DB=f"{self.tmp}/none.db")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_open_before_game_freeze_after_and_settle(self):
        con = paper.sync(paper.connect(":memory:"), now=500)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 3)
        # el precio cambia después de empezar: la operación ya no se toca
        log = sqlite3.connect(self.log)
        log.execute("UPDATE forecasts SET home_ask = 0.95, home_bid = 0.94, home_won = 1")
        log.commit()
        paper.sync(con, now=2000)
        rows = con.execute("SELECT strategy, price, payout FROM trades ORDER BY strategy").fetchall()
        self.assertTrue(all(price == 0.62 and payout == 1.0 for _, price, payout in rows), rows)
        r = paper.stats(con, "deportes_modelo")
        self.assertEqual(r["cerradas"], 1)
        self.assertAlmostEqual(r["pnl"], 20 * (1 / paper.kalshi_cost(0.62) - 1))


if __name__ == "__main__":
    unittest.main()
