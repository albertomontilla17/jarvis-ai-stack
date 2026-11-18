"""Tests del ejecutor de Kalshi y de las propuestas.  Correr:  python -m unittest discover tests"""

import os
import json
import time
import base64
import sqlite3
import tempfile
import unittest

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import jarvis_kalshi as kx
from jarvis_execute import Rejected, ledger
from sports import propose

CFG = {"env": "demo", "base": kx.BASES["demo"], "key_id": "test-key", "key_path": "",
       "max_per_trade": 5, "max_per_day": 6, "leagues": ["nba", "nhl", "nfl"],
       "max_slippage": 0.03, "min_edge": 0.02}


def order(**kw):
    now = time.time()
    o = {"id": "deportes_ajustado-nba-1", "venue": "kalshi", "ticker": "KXNBAGAME-26NOV18NYTOR-NY",
         "league": "nba", "question": "NY @ TOR: gana NY", "action": "BUY_YES", "price": 0.61,
         "prob": 0.70, "amount_usd": 5, "start_ts": now + 3600, "expires_at": now + 3300}
    o.update(kw)
    return o


class SignTest(unittest.TestCase):
    def test_signature_verifies_with_public_key(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        sig = kx.sign(key, "post", "/trade-api/v2/portfolio/events/orders?x=1", 1700000000000)
        key.public_key().verify(
            base64.b64decode(sig), b"1700000000000POST/trade-api/v2/portfolio/events/orders",
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256())   # lanza InvalidSignature si no coincide

    def test_key_loads_from_pem_file(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        path = os.path.join(tempfile.mkdtemp(), "k.pem")
        with open(path, "wb") as f:
            f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
        h = kx.auth_headers(CFG, kx.load_key(path), "GET", "/portfolio/balance")
        self.assertEqual(h["KALSHI-ACCESS-KEY"], "test-key")
        with self.assertRaises(Rejected):
            kx.load_key("")


class SizingTest(unittest.TestCase):
    def test_fee_rounds_up_and_contracts_fit_budget(self):
        self.assertEqual(kx.fee(8, 0.61), 0.14)          # 0.07·8·0.61·0.39 = 0.1332 → 0.14
        n = kx.contracts_for(5.0, 0.64)
        self.assertEqual(n, 7)
        self.assertLessEqual(n * 0.64 + kx.fee(n, 0.64), 5.0)
        self.assertEqual(kx.contracts_for(1.0, 0.97), 1)
        self.assertEqual(kx.contracts_for(0.5, 0.97), 0)


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.con = ledger(":memory:")

    def reject(self, text, ask=0.61, **kw):
        with self.assertRaises(Rejected) as c:
            kx.validate(order(**kw), CFG, self.con, True, ask)
        self.assertIn(text, str(c.exception))

    def test_plan_and_body(self):
        plan = kx.validate(order(), CFG, self.con, True, 0.62)
        self.assertEqual(plan["limit"], 0.64)
        body = kx.order_body(order(), plan)
        self.assertEqual((body["side"], body["price"], body["time_in_force"]), ("bid", "0.64", "fill_or_kill"))
        self.assertEqual(body["client_order_id"], kx.order_body(order(), plan)["client_order_id"])

    def test_rejections(self):
        self.reject("máximo por operación", amount_usd=6)
        self.reject("no permitida", league="mlb")
        self.reject("caducó", expires_at=time.time() - 1)
        self.reject("solo se aceptan compras", action="SELL")
        self.reject("precio subió", ask=0.65)
        self.reject("libro de órdenes", ask=None)
        self.reject("ya no hay ventaja", ask=0.64, prob=0.65)
        self.reject("faltan campos", ticker="")

    def test_daily_cap_and_no_double_execution(self):
        kx.execute(order(id="a"), live=False, cfg=CFG, con=self.con, ask=0.61)
        with self.assertRaises(Rejected):
            kx.execute(order(id="a"), live=False, cfg=CFG, con=self.con, ask=0.61)
        with self.assertRaises(Rejected):    # 4.94 gastado + 5 > 6
            kx.execute(order(id="b"), live=False, cfg=CFG, con=self.con, ask=0.61)

    def test_simulating_first_does_not_block_recording_the_real_order(self):
        kx.execute(order(id="s"), live=False, cfg=CFG, con=self.con, ask=0.61)
        self.con.execute("INSERT INTO executions VALUES ('s', 't', 'd', 'q', 'x', 1, 0.6, 1, '{}')")
        with self.assertRaises(Rejected):   # y la real ya registrada bloquea repetirla
            kx.validate(order(id="s"), CFG, self.con, True, 0.61)

    def test_old_ledger_is_migrated(self):
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE executions (order_id TEXT PRIMARY KEY, ts TEXT, day TEXT, question TEXT,"
                    " side TEXT, amount_usd REAL, limit_price REAL, live INTEGER, response TEXT)")
        old.execute("INSERT INTO executions VALUES ('a', 't', 'd', 'q', 'x', 1, 0.5, 0, '{}')")
        old.commit()
        old.close()
        con = ledger(path)
        con.execute("INSERT INTO executions VALUES ('a', 't', 'd', 'q', 'x', 1, 0.5, 1, '{}')")
        self.assertEqual(con.execute("SELECT COUNT(*) FROM executions").fetchone()[0], 2)

    def test_live_without_key_is_rejected_and_not_recorded(self):
        with self.assertRaises(Rejected):
            kx.execute(order(), live=True, cfg=CFG, con=self.con, ask=0.61,
                       post=lambda *a, **k: self.fail("no debería enviar"))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM executions").fetchone()[0], 0)

    def test_unfilled_live_order_is_rejected(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        path = os.path.join(tempfile.mkdtemp(), "k.pem")
        with open(path, "wb") as f:
            f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
        cfg = dict(CFG, key_path=path)

        class Resp:
            status_code = 201
            def __init__(self, d): self.d = d
            def json(self): return self.d

        with self.assertRaises(Rejected):
            kx.execute(order(), True, cfg, self.con, 0.61, post=lambda *a, **k: Resp({"fill_count": "0.00"}))
        sent = {}
        res = kx.execute(order(), True, cfg, self.con, 0.61, post=lambda url, data, headers, timeout: (
            sent.update(url=url, body=json.loads(data), headers=headers) or
            Resp({"order_id": "x", "fill_count": "7.00", "average_fill_price": "0.61", "average_fee_paid": "0.02"})))
        self.assertTrue(sent["url"].endswith("/portfolio/events/orders"))
        self.assertIn("KALSHI-ACCESS-SIGNATURE", sent["headers"])
        self.assertAlmostEqual(res["spent"], 7 * 0.63)


class ProposeTest(unittest.TestCase):
    def test_builds_buy_yes_on_chosen_team_market(self):
        d = tempfile.mkdtemp()
        paper, log = os.path.join(d, "paper.db"), os.path.join(d, "log.db")
        p = sqlite3.connect(paper)
        p.execute("CREATE TABLE trades (strategy TEXT, ref TEXT, grupo TEXT, label TEXT, side TEXT,"
                  " price REAL, prob REAL, event_ts INTEGER, payout REAL)")
        p.executemany("INSERT INTO trades VALUES (?, ?, 'nba', 'NY @ TOR', ?, 0.61, 0.70, ?, NULL)", [
            ("deportes_ajustado", "g1", "NY", 2000), ("deportes_ajustado", "g2", "NY", 500),
            ("deportes_modelo", "g1", "NY", 2000)])
        p.commit()
        l = sqlite3.connect(log)
        l.execute("CREATE TABLE forecasts (gid TEXT, home TEXT, away TEXT, home_ticker TEXT, away_ticker TEXT)")
        l.executemany("INSERT INTO forecasts VALUES (?, 'TOR', 'NY', ?, ?)",
                      [("g1", "T-TOR", "T-NY"), ("g2", "T2-TOR", "T2-NY")])
        l.commit()
        props = propose.build(paper, log, "deportes_ajustado", 1.0, now=1000)
        self.assertEqual(len(props), 1)   # g2 ya empezó
        self.assertEqual((props[0]["ticker"], props[0]["action"], props[0]["expires_at"]), ("T-NY", "BUY_YES", 1700))
        self.assertEqual(kx.REQUIRED, tuple(k for k in kx.REQUIRED if k in props[0]))


if __name__ == "__main__":
    unittest.main()
