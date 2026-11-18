"""Tests del analizador de temperatura.  Correr:  python -m unittest discover tests"""

import math
import unittest

import numpy as np

from weather import data, evaluate, model


class BracketTest(unittest.TestCase):
    def test_bounds_follow_kalshi_strikes(self):
        self.assertEqual(model.bracket_bounds("between", 58, 59), (57.5, 59.5))
        self.assertEqual(model.bracket_bounds("less", None, 58), (-math.inf, 57.5))      # "57° o menos"
        self.assertEqual(model.bracket_bounds("greater", 65, None), (65.5, math.inf))    # "66° o más"

    def test_brackets_of_a_day_sum_to_one(self):
        bounds = [("less", None, 58), ("between", 58, 59), ("between", 60, 61), ("between", 62, 63),
                  ("between", 64, 65), ("greater", 65, None)]
        total = sum(model.prob(61.3, 2.2, *model.bracket_bounds(*b)) for b in bounds)
        self.assertAlmostEqual(total, 1.0, places=3)

    def test_event_date(self):
        self.assertEqual(data.event_date("KXHIGHNY-26NOV18"), "2026-11-18")
        self.assertIsNone(data.event_date("KXHIGHNY-XYZ"))


class CalibrationTest(unittest.TestCase):
    def test_recovers_bias_and_spread(self):
        rng = np.random.default_rng(1)
        samples = []
        for _ in range(3000):
            txn = rng.uniform(40, 90)
            samples.append(("KNYC", txn, 2.0, round(txn + 1.5 + rng.normal(0, 3.0))))
        cal = model.fit(samples)
        self.assertAlmostEqual(cal.bias["KNYC"], 1.5, delta=0.3)
        mu, sigma = cal.params("KNYC", 70, 2.0)
        self.assertAlmostEqual(sigma, 3.0, delta=0.4)

    def test_decision_time_is_before_target_day(self):
        self.assertLess(data.decision_ts("2026-06-03", "dia_antes"), data.decision_ts("2026-06-03", "mismo_dia"))

    def test_cost_includes_fee(self):
        self.assertAlmostEqual(evaluate.cost(0.5), 0.5 + 0.07 * 0.25)


if __name__ == "__main__":
    unittest.main()
