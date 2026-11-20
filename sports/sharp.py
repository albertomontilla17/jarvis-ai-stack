"""
Kalshi contra las casas de apuestas: ¿el precio de Kalshi se desvía de la línea
de una casa grande lo bastante como para apostar con ventaja?

No hay modelo propio. La "probabilidad justa" sale de la cuota moneyline de la
casa (hoy DraftKings, vía el resumen de partido de ESPN, sin API key) quitándole
el margen. Si el ask de Kalshi + comisión queda por debajo de esa probabilidad
por al menos `umbral`, se compra.

Cuidado con el momento: ESPN guarda la línea de CIERRE (al inicio del partido),
que ya contiene toda la información previa. Para no comparar una línea "del
futuro" con un precio viejo, el precio de Kalshi se toma pocos minutos antes del
inicio (`minutes_before`, 5 por defecto), no 30.

Tablas nuevas en sports_data.db:
  book_odds   (gid, provider, home_ml, away_ml)     — home_ml NULL = sin cuota
  kalshi_near (gid, ticker, team, bid, ask, quote_ts, minutes_before)
"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from . import LEAGUES, data, evaluate, model

SCHEMA = """
CREATE TABLE IF NOT EXISTS book_odds (
    gid TEXT PRIMARY KEY, provider TEXT, home_ml REAL, away_ml REAL);
CREATE TABLE IF NOT EXISTS kalshi_near (
    gid TEXT PRIMARY KEY, ticker TEXT, team TEXT, bid REAL, ask REAL, quote_ts INTEGER,
    minutes_before INTEGER);
"""

# ESPN empezó a publicar la línea de DraftKings de forma consistente en diciembre de 2025.
FIRST_TS = 1764547200   # 2025-12-01 00:00 UTC
THRESHOLDS = (0.01, 0.02, 0.03, 0.05, 0.08)


def connect(path: str = data.DB_PATH) -> sqlite3.Connection:
    con = data.connect(path)
    con.executescript(SCHEMA)
    return con


# ── Cuotas ────────────────────────────────────────────────────────────────────
def implied(ml: float) -> float:
    """Cuota americana → probabilidad implícita (con margen)."""
    return 100 / (ml + 100) if ml > 0 else -ml / (-ml + 100)


def fair_home(home_ml: float, away_ml: float) -> float:
    """Probabilidad justa del local: implícitas normalizadas para que sumen 1 (quita el margen)."""
    h, a = implied(home_ml), implied(away_ml)
    return h / (h + a)


def parse_pickcenter(pc: list) -> tuple[str, float, float] | None:
    """Primera casa con moneyline de ambos lados → (casa, ML local, ML visita)."""
    for p in sorted(pc or [], key=lambda p: p.get("provider", {}).get("priority", 99)):
        h = (p.get("homeTeamOdds") or {}).get("moneyLine")
        a = (p.get("awayTeamOdds") or {}).get("moneyLine")
        if isinstance(h, (int, float)) and isinstance(a, (int, float)) and abs(h) >= 100 and abs(a) >= 100:
            return p.get("provider", {}).get("name", "?"), float(h), float(a)
    return None


def download_odds(con, league: str, since_ts: int = FIRST_TS, workers: int = 8) -> int:
    rows = con.execute(
        "SELECT DISTINCT g.gid FROM games g JOIN kalshi k ON k.gid = g.gid"
        " WHERE g.league = ? AND g.ts >= ? AND g.gid NOT IN (SELECT gid FROM book_odds)",
        (league, since_ts)).fetchall()
    url = f"{data.ESPN}/{LEAGUES[league]['espn']}/summary"

    def job(gid):
        try:
            s = data._get(url, {"event": gid.split("-", 1)[1]}, tries=3)
        except RuntimeError:
            return gid, None, False
        return gid, parse_pickcenter(s.get("pickcenter")), True

    n = got = 0
    with ThreadPoolExecutor(workers) as ex:
        for gid, odds, ok in ex.map(job, [r[0] for r in rows]):
            if not ok:
                continue   # fallo de red: se reintenta en la próxima corrida
            prov, h, a = odds if odds else (None, None, None)
            con.execute("INSERT OR REPLACE INTO book_odds VALUES (?, ?, ?, ?)", (gid, prov, h, a))
            n += 1
            got += odds is not None
            if n % 500 == 0:
                con.commit()
                print(f"  {league}: {n}/{len(rows)} cuotas", flush=True)
    con.commit()
    print(f"  {league}: {got} de {n} partidos con cuota")
    return n


def download_near_quotes(con, league: str, minutes_before: int = 5, since_ts: int = FIRST_TS,
                         workers: int = 6) -> int:
    """Última cotización real de Kalshi (velas de 1 minuto) al menos `minutes_before` antes del inicio."""
    rows = con.execute(
        "SELECT k.gid, k.ticker, k.team, k.historical, g.ts FROM kalshi k JOIN games g ON g.gid = k.gid"
        " JOIN book_odds b ON b.gid = k.gid AND b.home_ml IS NOT NULL"
        " WHERE k.league = ? AND g.ts >= ? AND k.gid NOT IN (SELECT gid FROM kalshi_near)"
        " AND k.ticker IN (SELECT MIN(ticker) FROM kalshi GROUP BY gid)", (league, since_ts)).fetchall()
    series = LEAGUES[league]["kalshi"]

    def job(row):
        gid, ticker, team, historical, start = row
        cutoff = start - minutes_before * 60
        paths = [f"series/{series}/markets/{ticker}/candlesticks", f"historical/markets/{ticker}/candlesticks"]
        if historical:
            paths.reverse()
        for path in paths:
            try:
                d = data._get(f"{data.KALSHI}/{path}",
                              {"start_ts": cutoff - 3 * 3600, "end_ts": cutoff, "period_interval": 1}, tries=3)
            except RuntimeError:
                continue
            best = None
            for c in d.get("candlesticks", []):
                bid, ask = data._price(c.get("yes_bid"), "close"), data._price(c.get("yes_ask"), "close")
                if c["end_period_ts"] <= cutoff and bid and ask and 0 < bid < ask < 1:
                    best = (bid, ask, c["end_period_ts"])
            if best:
                return gid, ticker, team, best
        return gid, ticker, team, None

    n = 0
    with ThreadPoolExecutor(workers) as ex:
        for gid, ticker, team, q in ex.map(job, rows):
            bid, ask, qts = q if q else (None, None, 0)
            con.execute("INSERT OR REPLACE INTO kalshi_near VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (gid, ticker, team, bid, ask, qts, minutes_before))
            n += 1
            if n % 500 == 0:
                con.commit()
                print(f"  {league}: {n}/{len(rows)} cotizaciones", flush=True)
    con.commit()
    return n


# ── Evaluación ────────────────────────────────────────────────────────────────
def bets(rows: list[evaluate.Row], threshold: float) -> np.ndarray:
    """Retorno por $1: se compra el lado donde prob. de la casa − (ask + comisión) ≥ umbral.
    Mismo criterio que la estrategia deportes_casa de paper.py."""
    rets = []
    for r in rows:
        best = None
        for p, ask, win in ((r.p, r.home_ask, r.y), (1 - r.p, 1 - r.home_bid, 1 - r.y)):
            cost = ask + evaluate.KALSHI_FEE * ask * (1 - ask)
            if p - cost >= threshold and 0.03 <= ask <= 0.97 and (best is None or p - cost > best[0]):
                best = (p - cost, win / cost - 1)
        if best:
            rets.append(best[1])
    return np.array(rets)


def rows_for(con, league: str) -> list[evaluate.Row]:
    out = []
    for ts, home, hs, as_, team, bid, ask, hml, aml in con.execute(
            "SELECT g.ts, g.home, g.home_score, g.away_score, n.team, n.bid, n.ask, b.home_ml, b.away_ml"
            " FROM kalshi_near n JOIN games g ON g.gid = n.gid JOIN book_odds b ON b.gid = n.gid"
            " WHERE g.league = ? AND n.quote_ts > 0 AND b.home_ml IS NOT NULL AND g.home_score != g.away_score",
            (league,)):
        hb, ha = (bid, ask) if team == home else (1 - ask, 1 - bid)
        out.append(evaluate.Row(ts, float(hs > as_), fair_home(hml, aml), hb, ha))
    return sorted(out, key=lambda r: r.ts)


def report(label: str, rows: list[evaluate.Row]) -> dict:
    y = np.array([r.y for r in rows])
    p = np.array([r.p for r in rows])
    mid = np.array([(r.home_bid + r.home_ask) / 2 for r in rows])
    w, se = model.fit_logistic(np.column_stack([evaluate.logit(mid), evaluate.logit(p)]), y)
    print(f"\n══ {label.upper()} ══ {len(rows)} partidos con cuota de la casa y precio de Kalshi")
    print(f"   Log-loss (menor = mejor):  casa {model.log_loss(p, y):.4f}  |  Kalshi {model.log_loss(mid, y):.4f}")
    print(f"   ¿La casa aporta sobre Kalshi?  peso {w[2]:+.2f} (z = {w[2] / se[2]:+.1f}; hace falta |z| > 2)")
    print(f"   Diferencia media |casa − medio Kalshi|: {np.mean(np.abs(p - mid)):.3f}"
          f"  |  spread medio Kalshi: {np.mean([r.home_ask - r.home_bid for r in rows]):.3f}")
    half = len(rows) // 2
    print(f"   {'umbral':>7s} {'apuestas':>9s} {'ROI/$':>8s}  {'IC 95%':^20s}   1ª mitad   2ª mitad")
    res = {}
    for thr in THRESHOLDS:
        r = bets(rows, thr)
        roi, lo, hi = evaluate.ci(r)
        h1, h2 = bets(rows[:half], thr), bets(rows[half:], thr)
        flag = "✅ edge" if len(r) and lo > 0 else ""
        print(f"   {thr:7.2f} {len(r):9d} {roi:+8.2%}  [{lo:+7.2%}, {hi:+7.2%}]   "
              f"{(h1.mean() if len(h1) else 0):+7.2%} ({len(h1)})  {(h2.mean() if len(h2) else 0):+7.2%} ({len(h2)})  {flag}")
        res[thr] = (len(r), roi, lo, hi)
    return {"n": len(rows), "z": float(w[2] / se[2]), "bets": res}


def run(con, leagues: list[str]) -> dict:
    out, every = {}, []
    for lg in leagues:
        rows = rows_for(con, lg)
        if not rows:
            print(f"\n══ {lg.upper()} ══ sin datos; corre primero: python -m sports sharp-download")
            continue
        out[lg] = report(lg, rows)
        every += rows
    if len(out) > 1:
        out["todas"] = report("todas", sorted(every, key=lambda r: r.ts))
    print("\nROI/$ = ganancia neta por dólar, comprando al ask de Kalshi y pagando su comisión."
          "\nSe apuesta cuando prob. justa de la casa − (ask + comisión) ≥ umbral.")
    return out
