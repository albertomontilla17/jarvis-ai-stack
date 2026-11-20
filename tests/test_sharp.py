"""Tests de Kalshi contra las casas.  Correr:  python -m unittest discover tests"""

import unittest

from sports import data, sharp


class OddsTest(unittest.TestCase):
    def test_implied_probability(self):
        self.assertAlmostEqual(sharp.implied(-150), 0.6)
        self.assertAlmostEqual(sharp.implied(150), 0.4)
        self.assertAlmostEqual(sharp.implied(100), 0.5)

    def test_fair_removes_margin(self):
        # -110 / -110: 52,4% + 52,4% con margen → 50% / 50% justo
        self.assertAlmostEqual(sharp.fair_home(-110, -110), 0.5)
        p = sharp.fair_home(-200, 170)
        self.assertAlmostEqual(p + sharp.fair_home(170, -200), 1.0)
        self.assertLess(p, sharp.implied(-200))

    def test_parse_pickcenter_skips_incomplete_lines(self):
        pc = [{"provider": {"name": "X", "priority": 0}, "homeTeamOdds": {"moneyLine": -130}, "awayTeamOdds": {}},
              {"provider": {"name": "DraftKings", "priority": 1},
               "homeTeamOdds": {"moneyLine": -130}, "awayTeamOdds": {"moneyLine": 110}}]
        self.assertEqual(sharp.parse_pickcenter(pc), ("DraftKings", -130.0, 110.0))
        self.assertIsNone(sharp.parse_pickcenter([]))
        self.assertIsNone(sharp.parse_pickcenter(None))


class BetsTest(unittest.TestCase):
    def test_buys_only_with_edge_after_fee(self):
        from sports import evaluate
        rows = [evaluate.Row(0, 1.0, 0.60, 0.53, 0.55),    # 0.60 − (0.55 + comisión 0.017) = 0.033
                evaluate.Row(1, 0.0, 0.57, 0.53, 0.55)]    # 0.57 − 0.567 = 0.003: sin apuesta
        r = sharp.bets(rows, 0.03)
        self.assertEqual(len(r), 1)
        self.assertAlmostEqual(r[0], 1 / (0.55 + 0.07 * 0.55 * 0.45) - 1)

    def test_paper_strategy_uses_book_probability(self):
        import paper
        self.assertEqual(paper.sports_pick("deportes_casa", 0.1, 0.1, 0.53, 0.55, 0.60)[0], "home")
        self.assertIsNone(paper.sports_pick("deportes_casa", 0.9, 0.9, 0.53, 0.55, 0.57))
        self.assertIsNone(paper.sports_pick("deportes_casa", 0.9, 0.9, 0.53, 0.55, None))


class ParseEventTest(unittest.TestCase):
    def test_team_without_abbreviation_is_skipped(self):
        e = {"id": "1", "date": "2026-05-01T00:00Z", "season": {"year": 2026, "type": 3},
             "status": {"type": {"completed": False, "state": "pre"}},
             "competitions": [{"competitors": [
                 {"homeAway": "home", "team": {"abbreviation": "BOS"}},
                 {"homeAway": "away", "team": {"displayName": "TBD"}}]}]}
        self.assertIsNone(data.parse_event("nba", e))


if __name__ == "__main__":
    unittest.main()
