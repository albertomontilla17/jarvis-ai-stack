"""Tests del modelo deportivo.  Correr:  python -m unittest discover tests"""

import unittest

import numpy as np

from sports import data, evaluate, model


def games(results):
    """results: lista de (local, visita, pts_local, pts_visita), un partido por día."""
    return [model.Game(f"g{i}", i * 86400, 2026, h, a, hs, as_)
            for i, (h, a, hs, as_) in enumerate(results)]


class ModelTest(unittest.TestCase):
    def test_features_do_not_use_the_game_result(self):
        base = [("A", "B", 100, 90), ("B", "A", 80, 95), ("A", "B", 110, 100)]
        flipped = base[:2] + [("A", "B", 90, 120)]   # solo cambia el resultado del último
        Xa = model.features(games(base), 20, 50, 10, 10.0)
        Xb = model.features(games(flipped), 20, 50, 10, 10.0)
        np.testing.assert_allclose(Xa, Xb)

    def test_winner_gains_elo_and_first_game_is_only_home_advantage(self):
        X = model.features(games([("A", "B", 100, 90), ("A", "B", 100, 90)]), 20, 40, 10, 10.0)
        self.assertAlmostEqual(X[0, 0], 40 / 400)
        self.assertGreater(X[1, 0], X[0, 0])

    def test_logistic_recovers_known_weights(self):
        rng = np.random.default_rng(0)
        X = rng.normal(size=(20000, 2))
        y = (rng.random(20000) < 1 / (1 + np.exp(-(0.3 + 1.5 * X[:, 0] - 0.7 * X[:, 1])))).astype(float)
        w, se = model.fit_logistic(X, y)
        np.testing.assert_allclose(w, [0.3, 1.5, -0.7], atol=0.08)
        self.assertTrue((se < 0.05).all())


class EvaluateTest(unittest.TestCase):
    def test_bets_pay_ask_plus_fee_and_pick_the_side_with_edge(self):
        # modelo 70% local, se puede comprar a 0.60 → apuesta al local, que gana
        r = evaluate.bets([evaluate.Row(0, 1.0, 0.70, 0.58, 0.60)], 0.05)
        cost = 0.60 + 0.07 * 0.60 * 0.40
        np.testing.assert_allclose(r, [1 / cost - 1])
        # modelo 30% local → compra la visita a 1 - bid = 0.42; pierde
        r = evaluate.bets([evaluate.Row(0, 1.0, 0.30, 0.58, 0.60)], 0.05)
        np.testing.assert_allclose(r, [-1.0])
        # sin ventaja suficiente → no apuesta
        self.assertEqual(len(evaluate.bets([evaluate.Row(0, 1.0, 0.61, 0.58, 0.60)], 0.05)), 0)


class TeamLookupTest(unittest.TestCase):
    def test_kalshi_names_map_to_espn_abbreviations(self):
        con = data.connect(":memory:")
        con.executemany("INSERT INTO teams (league, abbr, location, name) VALUES ('x', ?, ?, ?)", [
            ("LAL", "Los Angeles", "Lakers"), ("LAC", "Los Angeles", "Clippers"),
            ("SAC", "Sacramento", "Kings"), ("CHW", "Chicago", "White Sox"), ("CHC", "Chicago", "Cubs")])
        lk = data.team_lookup(con, "x")
        for name, abbr in (("Los Angeles L", "LAL"), ("Los Angeles C", "LAC"), ("Sacramento", "SAC"),
                           ("Chicago WS", "CHW"), ("Chicago C", "CHC"), ("SAC Kings", "SAC")):
            self.assertEqual(lk.get(data._norm(name)), abbr, name)
        self.assertNotIn("los angeles", lk)   # ciudad ambigua: no se adivina

    def test_college_names(self):
        con = data.connect(":memory:")
        con.executemany("INSERT INTO teams VALUES ('ncaamb', ?, ?, ?, ?)", [
            ("WSU", "Washington State", "Cougars", "Washington St"), ("WASH", "Washington", "Huskies", "Washington"),
            ("UNLV", "UNLV", "Rebels", "UNLV"), ("MIA", "Miami", "Hurricanes", "Miami"),
            ("M-OH", "Miami (OH)", "RedHawks", "Miami OH"), ("MIZ", "Miami", "X", "Miami")])
        lk = data.team_lookup(con, "ncaamb")
        for name, abbr in (("Washington St.", "WSU"), ("Washington State", "WSU"), ("Washington", "WASH"),
                           ("UNLV", "UNLV"), ("Miami (OH)", "M-OH")):
            self.assertEqual(lk.get(data._norm(name)), abbr, name)
        self.assertNotIn("miami", lk)          # dos equipos con el mismo nombre: no se adivina


class NeutralTest(unittest.TestCase):
    def test_neutral_site_has_no_home_advantage(self):
        gs = [model.Game("a", 0, 2026, "A", "B", 0, 0, 1), model.Game("b", 1, 2026, "A", "B", 0, 0, 0)]
        X = model.features(gs, 20, 100, 10, 10.0)
        self.assertEqual(X[0, 0], 0.0)
        self.assertAlmostEqual(X[1, 0], 100 / 400)   # mismo duelo con local: +100 Elo


if __name__ == "__main__":
    unittest.main()
