"""
jarvis_scan.py — Escáner de oportunidades (corre en la nube o en local).
========================================================================
No usa claves ni mueve dinero. Busca mercados en vivo de las categorías
elegidas, les aplica la MISMA estrategia que se probó en el backtest y,
cuando hay señal, guarda una propuesta en proposals/<id>.json para que tú
la apruebes y jarvis_execute.py la ejecute en la computadora local.

Además lleva un registro propio (scan.db) con el precio REAL de compra
(ask del libro) de cada propuesta, para medir con --settle si la estrategia
gana de verdad hacia adelante — sin arriesgar nada.

    python jarvis_scan.py                 # escanear y generar propuestas
    python jarvis_scan.py --settle        # liquidar propuestas resueltas + resultado acumulado
"""

import os
import json
import time
import sqlite3
import argparse

import numpy as np

from backtest import data
from backtest.engine import View, fee_per_share
from backtest.strategies import reversion

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "scan.db")
PROPOSALS = os.path.join(HERE, "proposals")

# Única estrategia con resultado positivo en el backtest (finanzas, jun–oct 2026).
STRATEGY_NAME = "reversion 10pts/6h"
STRATEGY = reversion(0.10, 6)
CATEGORIES = ("finance_prices", "economics")

MAX_SPREAD = 0.04        # con spreads mayores el edge del backtest desaparece
MIN_VOLUME_24H = 2_000
MAX_DAYS_TO_END = 31
PROPOSAL_TTL_S = 2 * 3600
PAGES = 10               # × 100 mercados, ordenados por volumen de 24h


def db(path: str = DB) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS proposals (
        id TEXT PRIMARY KEY, ts INTEGER, slug TEXT, question TEXT, category TEXT,
        outcome TEXT, token_id TEXT, ask REAL, spread REAL, fee REAL, strategy TEXT,
        payout REAL, settled_ts INTEGER)""")
    return con


def live_markets() -> list[dict]:
    out, now = [], time.time()
    for page in range(PAGES):
        batch = data._get(f"{data.GAMMA_API}/markets", {
            "closed": "false", "active": "true", "order": "volume24hr",
            "ascending": "false", "limit": 100, "offset": page * 100})
        for m in batch:
            cat = (m.get("feeType") or "sin_fee").split("_fees")[0]
            end = data._ts(m.get("endDate"))
            if cat not in CATEGORIES or not end or not m.get("acceptingOrders"):
                continue
            if not (0 < end - now <= MAX_DAYS_TO_END * 86400):
                continue
            if float(m.get("volume24hr") or 0) < MIN_VOLUME_24H:
                continue
            try:
                tokens, outcomes = json.loads(m["clobTokenIds"]), json.loads(m["outcomes"])
            except (KeyError, ValueError, TypeError):
                continue
            if len(tokens) != 2:
                continue
            fs = m.get("feeSchedule") or {}
            out.append({"id": m["conditionId"], "slug": m.get("slug"), "question": m.get("question"),
                        "category": cat, "end_ts": end, "tokens": tokens, "outcomes": outcomes,
                        "fee_rate": float(fs.get("rate") or 0), "fee_exp": float(fs.get("exponent") or 1)})
        if len(batch) < 100:
            break
    return out


def book(token_id: str) -> tuple[float, float] | None:
    """(mejor bid, mejor ask) del libro real."""
    try:
        b = data._get(f"{data.CLOB_API}/book", {"token_id": token_id}, tries=2)
        bids = [float(x["price"]) for x in b.get("bids", [])]
        asks = [float(x["price"]) for x in b.get("asks", [])]
        return (max(bids), min(asks)) if bids and asks else None
    except (RuntimeError, KeyError, ValueError):
        return None


def scan(con, amount_usd: float) -> list[dict]:
    now = int(time.time())
    new = []
    markets = live_markets()
    print(f"🔎 {len(markets)} mercados en vivo de {', '.join(CATEGORIES)}")
    for m in markets:
        if con.execute("SELECT 1 FROM proposals WHERE id = ?", (m["id"],)).fetchone():
            continue  # una sola entrada por mercado, igual que en el backtest
        try:
            hist = data.fetch_history(m["tokens"][0], now - 3 * 86400, now)
        except RuntimeError:
            continue
        if len(hist) < 3:
            continue
        ts = np.array([h[0] for h in hist], dtype=np.int64)
        ps = np.array([h[1] for h in hist], dtype=float)
        sig = STRATEGY(View(m["question"], m["category"], m["end_ts"], now, ts, ps, None))
        if sig is None:
            continue
        i = 0 if sig.action == "YES" else 1
        ba = book(m["tokens"][i])
        if ba is None:
            continue
        bid, ask = ba
        if ask - bid > MAX_SPREAD + 1e-9 or not (0.05 <= ask <= 0.95):
            print(f"   · descartado (spread {ask - bid:.3f}, ask {ask:.3f}): {m['question'][:60]}")
            continue
        fee = fee_per_share(ask, m["fee_rate"], m["fee_exp"])
        prop = {"id": m["id"], "token_id": m["tokens"][i], "side": "BUY",
                "outcome": m["outcomes"][i], "category": m["category"], "question": m["question"],
                "slug": m["slug"], "price": ask, "spread": round(ask - bid, 4),
                "amount_usd": amount_usd, "strategy": STRATEGY_NAME,
                "created_at": now, "expires_at": now + PROPOSAL_TTL_S}
        os.makedirs(PROPOSALS, exist_ok=True)
        with open(os.path.join(PROPOSALS, f"{m['id'][:12]}.json"), "w", encoding="utf-8") as f:
            json.dump(prop, f, ensure_ascii=False, indent=2)
        con.execute("INSERT INTO proposals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                    (m["id"], now, m["slug"], m["question"], m["category"], m["outcomes"][i],
                     m["tokens"][i], ask, ask - bid, fee, STRATEGY_NAME))
        con.commit()
        new.append(prop)
    return new


def settle(con) -> None:
    """Liquida propuestas cuyo mercado ya resolvió y muestra el resultado acumulado."""
    for pid, token in con.execute("SELECT id, token_id FROM proposals WHERE payout IS NULL").fetchall():
        try:
            m = data._get(f"{data.CLOB_API}/markets/{pid}", {}, tries=2)
        except RuntimeError:
            continue
        if not m.get("closed"):
            continue
        for t in m.get("tokens", []):
            if str(t.get("token_id")) == str(token) and t.get("price") is not None:
                con.execute("UPDATE proposals SET payout = ?, settled_ts = ? WHERE id = ?",
                            (float(t["price"]), int(time.time()), pid))
    con.commit()
    rows = con.execute("SELECT ask, fee, payout FROM proposals WHERE payout IS NOT NULL").fetchall()
    pending = con.execute("SELECT COUNT(*) FROM proposals WHERE payout IS NULL").fetchone()[0]
    if not rows:
        print(f"📊 Aún no hay propuestas resueltas ({pending} pendientes).")
        return
    rets = [p / (a + f) - 1 for a, f, p in rows]
    print(f"📊 RESULTADO HACIA ADELANTE — {STRATEGY_NAME}")
    print(f"   {len(rets)} resueltas, {pending} pendientes | aciertos {np.mean([r > 0 for r in rets]):.0%} "
          f"| ROI por $ apostado {np.mean(rets):+.1%} (a precio real de compra, con comisión)")
    if len(rets) < 100:
        print("   ⚠️ Menos de 100 resueltas: todavía es ruido, no una conclusión.")


def main():
    ap = argparse.ArgumentParser(description="Escáner de oportunidades de Jarvis")
    ap.add_argument("--settle", action="store_true", help="liquidar y mostrar resultado acumulado")
    ap.add_argument("--amount", type=float, default=5.0, help="USD por propuesta")
    a = ap.parse_args()
    con = db()
    if a.settle:
        settle(con)
        return
    new = scan(con, a.amount)
    if not new:
        print("⏸️  Sin propuestas nuevas.")
    for p in new:
        print(f"\n💡 PROPUESTA {p['id'][:12]}\n   {p['question']}\n   Comprar «{p['outcome']}» a {p['price']:.3f} "
              f"(spread {p['spread']:.3f}) — ${p['amount_usd']:.2f}\n   Estrategia: {p['strategy']}"
              f"\n   Ejecutar en local: python jarvis_execute.py --order proposals/{p['id'][:12]}.json")


if __name__ == "__main__":
    main()
