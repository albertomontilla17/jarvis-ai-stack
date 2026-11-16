"""Tests de los límites del ejecutor local.  Correr:  python -m unittest discover tests"""

import time
import sqlite3
import unittest

import jarvis_execute as ex

LIM = {"max_per_trade": 5, "max_per_day": 12, "categories": ["finance_prices"], "max_slippage": 0.03}


def order(**kw):
    o = {"id": "0xabc", "token_id": "1", "side": "BUY", "outcome": "Up", "category": "finance_prices",
         "question": "¿Sube el S&P?", "price": 0.40, "amount_usd": 5, "expires_at": time.time() + 600}
    o.update(kw)
    return o


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.con = ex.ledger(":memory:")
        import os
        os.environ.update(JARVIS_MAX_PER_TRADE="5", JARVIS_MAX_PER_DAY="12",
                          JARVIS_ALLOWED_CATEGORIES="finance_prices")

    def reject(self, text, ask=0.40, **kw):
        with self.assertRaises(ex.Rejected) as c:
            ex.validate(order(**kw), LIM, self.con, True, ask)
        self.assertIn(text, str(c.exception))

    def test_valid_order_gets_capped_limit_price(self):
        self.assertEqual(ex.validate(order(), LIM, self.con, True, 0.41), 0.43)

    def test_rejections(self):
        self.reject("máximo por trade", amount_usd=6)
        self.reject("no permitida", category="crypto")
        self.reject("expiró", expires_at=time.time() - 1)
        self.reject("solo se aceptan compras", side="SELL")
        self.reject("precio subió", ask=0.44)
        self.reject("precio actual", ask=None)
        self.reject("fuera de", price=0.99)
        self.reject("faltan campos", token_id="")

    def test_daily_cap_and_no_double_execution(self):
        for i in range(2):
            ex.execute(order(id=f"o{i}"), live=False, con=self.con, ask=0.40)
        with self.assertRaises(ex.Rejected):   # misma propuesta dos veces
            ex.execute(order(id="o0"), live=False, con=self.con, ask=0.40)
        with self.assertRaises(ex.Rejected):   # 5 + 5 + 5 > 12
            ex.execute(order(id="o2"), live=False, con=self.con, ask=0.40)

    def test_live_without_keys_is_rejected_and_not_recorded(self):
        import os
        os.environ.pop("POLYMARKET_PRIVATE_KEY", None)
        try:
            import py_clob_client  # noqa: F401
        except ImportError:
            self.skipTest("py-clob-client no instalado")
        with self.assertRaises(ex.Rejected):
            ex.execute(order(), live=True, con=self.con, ask=0.40)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM executions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
