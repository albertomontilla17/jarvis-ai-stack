"""
¿El pronóstico NBM calibrado le gana al precio de Kalshi?

Para cada momento de decisión ("dia_antes", "mismo_dia"):
  - el modelo usa la corrida NBM más reciente ya publicada antes de decidir;
  - la calibración se ajusta con el primer 40% de los días y se evalúa en el resto;
  - el mercado se mide con el punto medio bid/ask normalizado entre los rangos del día;
  - las apuestas compran Sí (al ask) o No (a 1 − bid) cuando el modelo supera el
    costo, comisión incluida, por al menos el umbral.
"""

import math
from collections import defaultdict

import numpy as np

from . import CITIES, DECISIONS
from . import data, model

FEE = 0.07   # comisión taker de Kalshi: 0.07 × p × (1 − p) por contrato


def cost(p: float) -> float:
    return p + FEE * p * (1 - p)


def load(con, decision: str) -> list[dict]:
    """Un registro por día y ciudad, con sus rangos, cotizaciones y el pronóstico disponible."""
    _, _, run_hour = DECISIONS[decision]
    events = defaultdict(list)
    for t, series, event, target, floor, cap, stype, won, value, bid, ask in con.execute(
            "SELECT m.ticker, m.series, m.event, m.target, m.floor, m.cap, m.strike_type, m.won,"
            " m.settle_value, q.bid, q.ask FROM markets m JOIN quotes q ON q.ticker = m.ticker"
            " WHERE q.decision = ? AND q.ts > 0", (decision,)):
        events[(series, event, target)].append(dict(ticker=t, floor=floor, cap=cap, stype=stype, won=won,
                                                    value=value, bid=bid, ask=ask))
    out = []
    for (series, event, target), brackets in events.items():
        station = CITIES[series][0]
        dts = data.decision_ts(target, decision)
        # corrida NBM publicada antes de decidir (la de run_hour con ≥ 1 h de margen)
        row = con.execute(
            "SELECT txn, xnd, runtime FROM nbm WHERE station = ? AND target = ? AND runtime <= ?"
            " AND CAST(strftime('%H', runtime, 'unixepoch') AS INTEGER) = ?"
            " ORDER BY runtime DESC LIMIT 1", (station, target, dts - 3600, run_hour)).fetchone()
        won = [b for b in brackets if b["won"]]
        if not row or len(won) != 1 or len(brackets) < 4:
            continue
        out.append(dict(series=series, station=station, target=target, txn=row[0], xnd=row[1],
                        value=won[0]["value"], brackets=brackets))
    return sorted(out, key=lambda e: e["target"])


def run(con, decision: str, train_frac: float = 0.4, thresholds=(0.03, 0.05, 0.10)) -> dict:
    days = load(con, decision)
    if len(days) < 30:
        print(f"\n══ {decision}: muy pocos días con datos ({len(days)})")
        return {}
    dates = sorted({d["target"] for d in days})
    cutoff = dates[int(len(dates) * train_frac)]
    train = [d for d in days if d["target"] < cutoff and d["value"] is not None]
    test = [d for d in days if d["target"] >= cutoff]
    cal = model.fit([(d["station"], d["txn"], d["xnd"], d["value"]) for d in train])

    ll_model, ll_mkt, bets, by_city = [], [], defaultdict(list), defaultdict(list)
    spreads = []
    for d in test:
        mu, sigma = cal.params(d["station"], d["txn"], d["xnd"])
        mids = [max((b["bid"] + b["ask"]) / 2, 0.005) for b in d["brackets"]]
        z = sum(mids)
        for b, mid in zip(d["brackets"], mids):
            p = model.prob(mu, sigma, *model.bracket_bounds(b["stype"], b["floor"], b["cap"]))
            if b["won"]:
                ll_model.append(-math.log(p))
                ll_mkt.append(-math.log(mid / z))
            spreads.append(b["ask"] - b["bid"])
            for thr in thresholds:
                # comprar Sí al ask
                if 0.02 <= b["ask"] <= 0.98 and p - cost(b["ask"]) >= thr:
                    r = b["won"] / cost(b["ask"]) - 1
                    bets[thr].append(r)
                    if thr == thresholds[1]:
                        by_city[d["series"]].append(r)
                # comprar No a 1 − bid
                no_price = 1 - b["bid"]
                if 0.02 <= no_price <= 0.98 and (1 - p) - cost(no_price) >= thr:
                    r = (1 - b["won"]) / cost(no_price) - 1
                    bets[thr].append(r)
                    if thr == thresholds[1]:
                        by_city[d["series"]].append(r)

    print(f"\n══ Decisión «{decision}» ══ {len(train)} días de calibración, {len(test)} días de prueba "
          f"(desde {cutoff}), {len({d['series'] for d in test})} ciudades")
    print(f"   Calibración: escala σ ×{cal.scale:.1f}, σ mínima {cal.sigma_min}°F, sesgo por ciudad "
          + ", ".join(f"{k} {v:+.1f}" for k, v in sorted(cal.bias.items())))
    print(f"   Log-loss del rango ganador (menor = mejor): modelo {np.mean(ll_model):.3f} | "
          f"mercado {np.mean(ll_mkt):.3f}")
    print(f"   Spread medio real: {np.mean(spreads):.3f}")
    print(f"   {'umbral':>8s} {'apuestas':>9s} {'aciertos':>9s} {'ROI/$':>8s}  {'IC 95%':^20s}")
    res = {}
    for thr in thresholds:
        r = np.array(bets[thr])
        roi, lo, hi = _ci(r)
        flag = "✅ edge" if len(r) and lo > 0 else ""
        print(f"   {thr:8.2f} {len(r):9d} {np.mean(r > 0) if len(r) else 0:9.1%} {roi:+8.2%}  "
              f"[{lo:+7.2%}, {hi:+7.2%}]  {flag}")
        res[thr] = (len(r), roi, lo, hi)
    print(f"   Por ciudad (umbral {thresholds[1]}): " + ", ".join(
        f"{k.replace('KXHIGH', '')} {np.mean(v):+.0%} ({len(v)})" for k, v in sorted(by_city.items())))
    return {"ll_model": float(np.mean(ll_model)), "ll_mkt": float(np.mean(ll_mkt)), "bets": res}


def _ci(r: np.ndarray, n_boot: int = 4000, seed: int = 0):
    if len(r) == 0:
        return 0.0, 0.0, 0.0
    rng = np.random.default_rng(seed)
    boots = r[rng.integers(0, len(r), size=(n_boot, len(r)))].mean(axis=1)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return float(r.mean()), float(lo), float(hi)
