"""Tests de la lógica del bot de market making.  Correr:  python -m unittest discover tests"""

import unittest

from jarvis_mm import Params, best_prices, desired_quotes, plan

P = Params(size=5, max_pos=20)
BOOK = {"yes_dollars": [["0.40", "10"], ["0.42", "30"]],   # compras de Sí: mejor 0.42
        "no_dollars": [["0.50", "5"], ["0.55", "20"]]}     # compras de No: mejor 0.55 → venta de Sí a 0.45


class BestPricesTest(unittest.TestCase):
    def test_reads_yes_bid_and_derived_ask(self):
        self.assertEqual(best_prices(BOOK, []), (0.42, 0.45))

    def test_excludes_our_own_orders(self):
        own = [{"side": "bid", "price": 0.42, "remaining": 30}, {"side": "ask", "price": 0.45, "remaining": 20}]
        self.assertEqual(best_prices(BOOK, own), (0.40, 0.50))

    def test_empty_side(self):
        self.assertEqual(best_prices({"yes_dollars": [["0.3", "1"]]}, []), (0.3, None))


class QuotesTest(unittest.TestCase):
    def test_wide_spread_improves_one_tick(self):
        self.assertEqual(desired_quotes(0.42, 0.45, 0, P), {"bid": (0.43, 5), "ask": (0.44, 5)})

    def test_tight_spread_joins(self):
        self.assertEqual(desired_quotes(0.42, 0.44, 0, P), {"bid": (0.42, 5), "ask": (0.44, 5)})

    def test_three_tick_spread_does_not_cross(self):
        q = desired_quotes(0.42, 0.45, 0, P)
        self.assertLess(q["bid"][0], q["ask"][0])

    def test_inventory_limits(self):
        self.assertIsNone(desired_quotes(0.42, 0.44, 20, P)["bid"])      # lleno de Sí: no compra más
        self.assertEqual(desired_quotes(0.42, 0.44, 18, P)["bid"], (0.42, 2))
        self.assertIsNone(desired_quotes(0.42, 0.44, -20, P)["ask"])     # corto: no vende más

    def test_no_quotes_outside_band_or_locked(self):
        self.assertEqual(desired_quotes(0.97, 0.99, 0, P), {"bid": None, "ask": None})
        self.assertEqual(desired_quotes(0.44, 0.44, 0, P), {"bid": None, "ask": None})
        self.assertEqual(desired_quotes(None, 0.44, 0, P), {"bid": None, "ask": None})


class PlanTest(unittest.TestCase):
    def test_keeps_matching_order_and_replaces_stale(self):
        own = [{"order_id": "a", "side": "bid", "price": 0.42, "remaining": 5},
               {"order_id": "b", "side": "ask", "price": 0.46, "remaining": 5}]
        cancels, places = plan(own, {"bid": (0.42, 5), "ask": (0.45, 5)})
        self.assertEqual(cancels, ["b"])
        self.assertEqual(places, [("ask", 0.45, 5)])

    def test_withdraw_cancels_everything(self):
        own = [{"order_id": "a", "side": "bid", "price": 0.42, "remaining": 5}]
        self.assertEqual(plan(own, {"bid": None, "ask": None}), (["a"], []))

    def test_duplicates_are_cleaned(self):
        own = [{"order_id": "a", "side": "bid", "price": 0.42, "remaining": 5},
               {"order_id": "c", "side": "bid", "price": 0.42, "remaining": 5}]
        cancels, places = plan(own, {"bid": (0.42, 5), "ask": None})
        self.assertEqual((cancels, places), (["c"], []))


if __name__ == "__main__":
    unittest.main()
