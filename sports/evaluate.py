"""
¿El modelo de stats le gana al mercado?

Tres preguntas, de menos a más exigente, siempre sobre partidos que el modelo
no usó para ajustarse y al precio REAL (bid/ask de Kalshi 30+ min antes del inicio):

  1. ¿Predice mejor que el precio?  → log-loss del modelo vs. del mercado.
  2. ¿Aporta algo que el precio no sepa ya?  → regresión del resultado sobre
     el precio y el modelo juntos; si el peso del modelo no se distingue de 0,
     el mercado ya contiene esa información.
  3. ¿Gana dinero?  → apostar cuando modelo − ask ≥ umbral, pagando ask + comisión.
"""

from dataclasses import dataclass

import numpy as np

from . import model

KALSHI_FEE = 0.07   # comisión taker: 0.07 × precio × (1 − precio) por contrato


@dataclass
class Row:
    ts: int
    y: float          # 1 si ganó el local
    p: float          # prob. del modelo para el local
    home_bid: float
    home_ask: float


def rows_for(con, fitted: model.Fitted) -> list[Row]:
    idx = {g.gid: i for i, g in enumerate(fitted.games)}
    out = []
    for gid, team, bid, ask in con.execute(
            "SELECT gid, team, bid, ask FROM kalshi WHERE league = ? AND quote_ts > 0", (fitted.league,)):
        i = idx.get(gid)
        if i is None:
            continue
        g = fitted.games[i]
        hb, ha = (bid, ask) if team == g.home else (1 - ask, 1 - bid)
        out.append(Row(g.ts, fitted.y[i], float(fitted.p[i]), hb, ha))
    return sorted(out, key=lambda r: r.ts)


def logit(p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def bets(rows: list[Row], threshold: float) -> np.ndarray:
    """Retorno por $1 de cada apuesta: se compra el lado donde modelo − ask ≥ umbral."""
    rets = []
    for r in rows:
        for p, ask, win in ((r.p, r.home_ask, r.y), (1 - r.p, 1 - r.home_bid, 1 - r.y)):
            if p - ask >= threshold and 0.03 <= ask <= 0.97:
                cost = ask + KALSHI_FEE * ask * (1 - ask)
                rets.append(win / cost - 1)
    return np.array(rets)


def ci(rets: np.ndarray, n_boot: int = 4000, seed: int = 0) -> tuple[float, float, float]:
    if len(rets) == 0:
        return 0.0, 0.0, 0.0
    rng = np.random.default_rng(seed)
    boots = rets[rng.integers(0, len(rets), size=(n_boot, len(rets)))].mean(axis=1)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return float(rets.mean()), float(lo), float(hi)


def report(league: str, fitted: model.Fitted, rows: list[Row]) -> dict:
    y = np.array([r.y for r in rows])
    p = np.array([r.p for r in rows])
    mid = np.array([(r.home_bid + r.home_ask) / 2 for r in rows])
    spread = float(np.mean([r.home_ask - r.home_bid for r in rows]))

    w, se = model.fit_logistic(np.column_stack([logit(mid), logit(p)]), y)
    z_model = w[2] / se[2]

    print(f"\n══ {league.upper()} ══ {len(rows)} partidos con precio real | modelo ajustado con "
          f"{fitted.n_train} partidos anteriores (K={fitted.k}, local=+{fitted.hfa} Elo)")
    print(f"   Acierto del favorito:  modelo {np.mean((p > .5) == (y > .5)):.1%}  |  "
          f"mercado {np.mean((mid > .5) == (y > .5)):.1%}")
    print(f"   Log-loss (menor = mejor):  modelo {model.log_loss(p, y):.4f}  |  "
          f"mercado {model.log_loss(mid, y):.4f}  |  moneda al aire 0.6931")
    print(f"   ¿Aporta el modelo sobre el precio?  peso {w[2]:+.2f} (z = {z_model:+.1f}; hace falta |z| > 2)"
          f"  |  peso del precio {w[1]:+.2f}")
    print(f"   Spread medio real: {spread:.3f}")
    print(f"   {'umbral':>8s} {'apuestas':>9s} {'aciertos':>9s} {'ROI/$':>8s}  {'IC 95%':^20s}")
    res = {}
    for thr in (0.02, 0.05, 0.08, 0.12):
        r = bets(rows, thr)
        roi, lo, hi = ci(r)
        flag = "✅ edge" if len(r) and lo > 0 else ""
        print(f"   {thr:8.2f} {len(r):9d} {np.mean(r > 0) if len(r) else 0:9.1%} {roi:+8.2%}  "
              f"[{lo:+7.2%}, {hi:+7.2%}]  {flag}")
        res[thr] = (len(r), roi, lo, hi)
    return {"n": len(rows), "z_model": float(z_model), "bets": res}


def run(con, leagues: list[str]) -> dict:
    out = {}
    for lg in leagues:
        first = con.execute(
            "SELECT MIN(g.ts) FROM kalshi k JOIN games g ON g.gid = k.gid"
            " WHERE k.league = ? AND k.quote_ts > 0", (lg,)).fetchone()[0]
        if not first:
            print(f"\n══ {lg.upper()} ══ sin cotizaciones; corre primero: python -m sports download")
            continue
        fitted = model.fit(con, lg, cutoff_ts=first)
        out[lg] = report(lg, fitted, rows_for(con, fitted))
    return out
