"""
paper.py — Paper trading de todas las estrategias en vivo.
==========================================================
Convierte los registros de los escáneres en operaciones simuladas, cada una al
precio REAL de compra (ask + comisión) que había antes del evento, y lleva una
cartera ficticia por estrategia para compararlas con el mismo criterio.

Estrategias:
  finanzas_reversion   propuestas de jarvis_scan.py (Polymarket)
  deportes_modelo      modelo de stats base, si su prob. supera el ask en 5+ puntos (Kalshi)
  deportes_ajustado    modelo ajustado por bajas, mismo umbral (Kalshi)
  deportes_favorito    control: compra siempre al favorito del mercado (Kalshi)

El control mide cuánto cuesta operar sin ninguna ventaja. Una estrategia solo
vale algo si le gana al control y su intervalo de confianza queda por encima de 0.

    python paper.py            # sincronizar operaciones y mostrar resultados
"""

import os
import time
import sqlite3
import argparse

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PAPER_DB = os.path.join(HERE, "paper.db")
SPORTS_LOG = os.path.join(HERE, "sports_log.db")
SCAN_DB = os.path.join(HERE, "scan.db")

BANKROLL = 1000.0        # capital ficticio inicial por estrategia
STAKE = 20.0             # apuesta fija por operación (2% del capital inicial)
EDGE = 0.05              # ventaja mínima del modelo sobre el ask
KALSHI_FEE = 0.07        # comisión taker: 0.07 × p × (1 − p) por contrato
PRICE_RANGE = (0.03, 0.97)

STRATEGIES = ("finanzas_reversion", "deportes_modelo", "deportes_ajustado", "deportes_favorito")


def connect(path: str = PAPER_DB) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS trades (
        id TEXT PRIMARY KEY,        -- estrategia|referencia
        strategy TEXT, ref TEXT, grupo TEXT, label TEXT, side TEXT,
        price REAL,                 -- ask pagado
        cost REAL,                  -- ask + comisión, por contrato que paga $1
        prob REAL,                  -- prob. estimada del lado comprado (None en el control)
        stake REAL, open_ts INTEGER, event_ts INTEGER,
        payout REAL, settled_ts INTEGER)""")
    return con


def kalshi_cost(ask: float) -> float:
    return ask + KALSHI_FEE * ask * (1 - ask)


# ── Decisiones deportivas ─────────────────────────────────────────────────────
def sports_pick(strategy: str, p_model, p_adj, home_bid: float, home_ask: float):
    """Devuelve (lado, ask, prob) o None. Lado = 'home' | 'away'."""
    sides = {"home": home_ask, "away": 1 - home_bid}
    if strategy == "deportes_favorito":
        mid = (home_bid + home_ask) / 2
        side = "home" if mid >= 0.5 else "away"
        ask = sides[side]
        return (side, ask, None) if PRICE_RANGE[0] <= ask <= PRICE_RANGE[1] else None
    p = p_model if strategy == "deportes_modelo" else p_adj
    if p is None:
        return None
    best = None
    for side, prob in (("home", p), ("away", 1 - p)):
        ask = sides[side]
        edge = prob - kalshi_cost(ask)
        if edge >= EDGE and PRICE_RANGE[0] <= ask <= PRICE_RANGE[1] and (best is None or edge > best[0]):
            best = (edge, side, ask, prob)
    return best[1:] if best else None


def sync_sports(con, now: int) -> None:
    if not os.path.exists(SPORTS_LOG):
        return
    log = sqlite3.connect(SPORTS_LOG)
    rows = log.execute("SELECT gid, league, start_ts, home, away, p_model, p_adj, home_bid, home_ask,"
                       " home_won FROM forecasts").fetchall()
    for gid, league, start, home, away, p_model, p_adj, bid, ask, home_won in rows:
        for strat in STRATEGIES[1:]:
            tid = f"{strat}|{gid}"
            if start > now:   # antes del partido la decisión se rehace con el último precio
                pick = sports_pick(strat, p_model, p_adj, bid, ask)
                if pick is None:
                    con.execute("DELETE FROM trades WHERE id = ?", (tid,))
                    continue
                side, price, prob = pick
                team = home if side == "home" else away
                con.execute(
                    "INSERT INTO trades (id, strategy, ref, grupo, label, side, price, cost, prob, stake,"
                    " open_ts, event_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(id) DO UPDATE SET side = excluded.side, price = excluded.price,"
                    " cost = excluded.cost, prob = excluded.prob, open_ts = excluded.open_ts,"
                    " event_ts = excluded.event_ts",
                    (tid, strat, gid, league, f"{away} @ {home}", team, price, kalshi_cost(price),
                     prob, STAKE, now, start))
            elif home_won is not None:
                row = con.execute("SELECT ref, side FROM trades WHERE id = ? AND payout IS NULL",
                                  (tid,)).fetchone()
                if row:
                    won = (row[1] == home) == bool(home_won)
                    con.execute("UPDATE trades SET payout = ?, settled_ts = ? WHERE id = ?",
                                (1.0 if won else 0.0, now, tid))


def sync_finance(con, now: int) -> None:
    if not os.path.exists(SCAN_DB):
        return
    scan = sqlite3.connect(SCAN_DB)
    for pid, ts, question, outcome, ask, fee, payout, settled_ts in scan.execute(
            "SELECT id, ts, question, outcome, ask, fee, payout, settled_ts FROM proposals"):
        tid = f"finanzas_reversion|{pid}"
        con.execute(
            "INSERT OR IGNORE INTO trades (id, strategy, ref, grupo, label, side, price, cost, prob,"
            " stake, open_ts, event_ts) VALUES (?, 'finanzas_reversion', ?, 'finanzas', ?, ?, ?, ?,"
            " NULL, ?, ?, ?)", (tid, pid, question, outcome, ask, ask + (fee or 0), STAKE, ts, ts))
        if payout is not None:
            con.execute("UPDATE trades SET payout = ?, settled_ts = ? WHERE id = ? AND payout IS NULL",
                        (payout, settled_ts or now, tid))


def sync(con=None, now: int | None = None) -> sqlite3.Connection:
    con = con or connect()
    now = now or int(time.time())
    sync_sports(con, now)
    sync_finance(con, now)
    con.commit()
    return con


# ── Resultados ────────────────────────────────────────────────────────────────
def ci(rets: np.ndarray, n_boot: int = 4000, seed: int = 0) -> tuple[float, float]:
    if len(rets) < 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    boots = rets[rng.integers(0, len(rets), size=(n_boot, len(rets)))].mean(axis=1)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return float(lo), float(hi)


def stats(con, strategy: str) -> dict:
    open_n = con.execute("SELECT COUNT(*) FROM trades WHERE strategy = ? AND payout IS NULL",
                         (strategy,)).fetchone()[0]
    rows = con.execute("SELECT cost, payout, stake FROM trades WHERE strategy = ? AND payout IS NOT NULL"
                       " ORDER BY settled_ts, id", (strategy,)).fetchall()
    rets = np.array([p / c - 1 for c, p, _ in rows])
    pnl = float(sum(s * r for (_, _, s), r in zip(rows, rets)))
    equity = BANKROLL + np.cumsum([s * r for (_, _, s), r in zip(rows, rets)]) if rows else np.array([])
    peak = np.maximum.accumulate(np.concatenate([[BANKROLL], equity])) if rows else np.array([BANKROLL])
    mdd = float(np.max(1 - np.concatenate([[BANKROLL], equity]) / peak)) if rows else 0.0
    lo, hi = ci(rets)
    return {"abiertas": open_n, "cerradas": len(rows), "aciertos": float(np.mean(rets > 0)) if len(rows) else 0.0,
            "pnl": pnl, "roi": float(rets.mean()) if len(rows) else 0.0, "lo": lo, "hi": hi,
            "capital": BANKROLL + pnl, "caida": mdd}


def report(con) -> None:
    print(f"📒 PAPER TRADING — ${BANKROLL:,.0f} ficticios por estrategia, ${STAKE:.0f} por operación\n")
    print(f"{'estrategia':22s} {'abiertas':>8s} {'cerradas':>8s} {'aciertos':>8s} {'PnL':>9s} "
          f"{'ROI/$':>7s} {'IC 95%':^17s} {'capital':>9s} {'caída máx':>9s}")
    for s in STRATEGIES:
        r = stats(con, s)
        ic = f"[{r['lo']:+.0%}, {r['hi']:+.0%}]" if r["cerradas"] >= 2 else "—"
        print(f"{s:22s} {r['abiertas']:8d} {r['cerradas']:8d} {r['aciertos']:8.0%} {r['pnl']:+9.2f} "
              f"{r['roi']:+7.1%} {ic:^17s} {r['capital']:9.2f} {r['caida']:9.1%}")
    total = con.execute("SELECT COUNT(*) FROM trades WHERE payout IS NOT NULL").fetchone()[0]
    if total < 200:
        print("\n⚠️ Menos de 200 operaciones cerradas: todavía es ruido, no una conclusión.")


def main():
    argparse.ArgumentParser(description="Paper trading de Jarvis").parse_args()
    report(sync())


if __name__ == "__main__":
    main()
