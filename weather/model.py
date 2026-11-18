"""
Probabilidad de cada rango de temperatura.

La máxima oficial se modela como Normal(μ, σ):
    μ = máxima NBM + sesgo de la ciudad
    σ = max(escala × desviación NBM, σ mínima)
El sesgo por ciudad, la escala y la σ mínima se ajustan SOLO con días de
entrenamiento (anteriores al corte), maximizando la verosimilitud del valor oficial.

Los valores oficiales son enteros, así que un rango "58° a 59°" es [57,5 ; 59,5).
"""

import math
from dataclasses import dataclass, field

import numpy as np


def ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bracket_bounds(strike_type: str, floor, cap) -> tuple[float, float]:
    if strike_type == "between":
        return float(floor) - 0.5, float(cap) + 0.5
    if strike_type == "less":          # "57° o menos" llega con cap = 58
        return -math.inf, float(cap) - 0.5
    if strike_type == "greater":       # "66° o más" llega con floor = 65
        return float(floor) + 0.5, math.inf
    raise ValueError(strike_type)


def prob(mu: float, sigma: float, lo: float, hi: float) -> float:
    a = 0.0 if lo == -math.inf else ncdf((lo - mu) / sigma)
    b = 1.0 if hi == math.inf else ncdf((hi - mu) / sigma)
    return min(max(b - a, 1e-4), 1 - 1e-4)


@dataclass
class Calibration:
    bias: dict = field(default_factory=dict)   # por estación
    scale: float = 1.0
    sigma_min: float = 1.0

    def params(self, station: str, txn: float, xnd: float | None) -> tuple[float, float]:
        mu = txn + self.bias.get(station, 0.0)
        sigma = max(self.scale * (xnd or 2.0), self.sigma_min)
        return mu, sigma


def fit(samples: list[tuple[str, float, float | None, float]]) -> Calibration:
    """samples: (estación, txn, xnd, valor oficial). Ajuste por búsqueda en rejilla."""
    if not samples:
        return Calibration()
    by_st: dict[str, list] = {}
    for st, txn, xnd, y in samples:
        by_st.setdefault(st, []).append(y - txn)
    # sesgo: mediana del error, encogida hacia 0 si hay pocos días
    bias = {st: float(np.median(e)) * len(e) / (len(e) + 10) for st, e in by_st.items()}
    best = None
    for scale in np.arange(0.6, 2.01, 0.1):
        for smin in (0.8, 1.0, 1.25, 1.5, 2.0, 2.5):
            cal = Calibration(bias, float(scale), smin)
            ll = 0.0
            for st, txn, xnd, y in samples:
                mu, s = cal.params(st, txn, xnd)
                ll += -0.5 * ((y - mu) / s) ** 2 - math.log(s)
            if best is None or ll > best[0]:
                best = (ll, cal)
    return best[1]
