"""
Pieza 4 — Métricas.

La cifra clave es el ROI por $ apostado con su intervalo de confianza al 95%.
El IC se calcula con bootstrap POR EVENTO: mercados del mismo evento (props
del mismo partido, candidatos de la misma elección) están correlacionados y
contarlos como independientes haría parecer significativo lo que no lo es.

Criterio: una estrategia solo "muestra edge" si el límite INFERIOR del IC
está por encima de 0 — y además se sostiene en la mitad de datos que no se
usó para elegirla (fuera de muestra).
"""

from collections import defaultdict

import numpy as np

from .engine import Market, Trade


def roi_ci(trades: list[Trade], n_boot: int = 2000, seed: int = 0) -> tuple[float, float, float]:
    """(ROI medio, IC 2.5%, IC 97.5%) con bootstrap agrupado por evento."""
    if not trades:
        return 0.0, 0.0, 0.0
    groups = defaultdict(list)
    for t in trades:
        groups[t.event].append(t.ret)
    sums = np.array([sum(v) for v in groups.values()])
    cnts = np.array([len(v) for v in groups.values()])
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(sums), size=(n_boot, len(sums)))
    boots = sums[idx].sum(axis=1) / cnts[idx].sum(axis=1)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return float(sums.sum() / cnts.sum()), float(lo), float(hi)


def summarize(trades: list[Trade]) -> dict:
    roi, lo, hi = roi_ci(trades)
    rets = [t.ret for t in trades]
    return {
        "n": len(trades),
        "eventos": len({t.event for t in trades}),
        "win": float(np.mean([r > 0 for r in rets])) if rets else 0.0,
        "roi": roi, "lo": lo, "hi": hi,
        "edge": bool(trades) and lo > 0,
    }


def fmt_row(name: str, s: dict) -> str:
    flag = "✅ edge" if s["edge"] else ("—" if s["n"] else "sin trades")
    return (f"{name:26s} {s['n']:6d} {s['eventos']:6d} {s['win']:6.1%} "
            f"{s['roi']:+8.2%}  [{s['lo']:+7.2%}, {s['hi']:+7.2%}]  {flag}")


HEADER = f"{'':26s} {'trades':>6s} {'event.':>6s} {'win%':>6s} {'ROI/$':>8s}  {'IC 95%':^20s}"


def split_by_time(trades: list[Trade], cutoff: int) -> tuple[list[Trade], list[Trade]]:
    return [t for t in trades if t.t_open < cutoff], [t for t in trades if t.t_open >= cutoff]


def calibration(markets: list[Market], hours_before_end: float = 24, bins: int = 10) -> list[dict]:
    """
    ¿El precio ya es una buena probabilidad? Toma el precio de YES de cada
    mercado `hours_before_end` horas antes del cierre PROGRAMADO y lo compara
    con la frecuencia real con la que YES ganó, por tramo de precio.
    Si precio ≈ frecuencia en todos los tramos, no hay edge solo con el precio.
    """
    pts = []
    for m in markets:
        target = m.end_ts - hours_before_end * 3600
        i = int(np.searchsorted(m.ts, target, side="right")) - 1
        if i < 0 or m.ts[i] >= m.closed_ts or target - m.ts[i] > 6 * 3600:
            continue
        pts.append((float(m.ps[i]), m.payout_yes))
    out = []
    edges = np.linspace(0, 1, bins + 1)
    for a, b in zip(edges[:-1], edges[1:]):
        sel = [(p, y) for p, y in pts if a <= p < b or (b == 1 and p == 1)]
        if not sel:
            continue
        ps, ys = np.array(sel).T
        # SE bajo la hipótesis "el precio ES la probabilidad" (no se anula si real = 0 o 1)
        se = float(np.sqrt(max(ps.mean() * (1 - ps.mean()), 1e-9) / len(sel)))
        out.append({"rango": f"{a:.1f}-{b:.1f}", "n": len(sel), "precio": float(ps.mean()),
                    "real": float(ys.mean()), "dif": float(ys.mean() - ps.mean()), "se": se})
    return out
