"""
Pieza 3 — Estrategias base (sin LLM).

Una estrategia es cualquier callable  f(view: View) -> Signal | None.
Estas son reglas simples y baratas que responden a la pregunta previa a
gastar en Claude: ¿hay algún edge explotable solo con el precio, después
de costos? También sirven de vara de medir: una estrategia con LLM tiene
que ganarles (y a `random`) para justificar su costo.

Todas entran como máximo una vez por mercado y mantienen hasta resolución.
"""

import random

from .engine import Signal, View

HOUR = 3600


def _side_in_band(p: float, lo: float, hi: float) -> str | None:
    """Lado cuyo precio cae en [lo, hi]."""
    if lo <= p <= hi:
        return "YES"
    if lo <= 1 - p <= hi:
        return "NO"
    return None


def favorite(lo: float = 0.80, hi: float = 0.95, max_hours_left: float = 72):
    """Compra el favorito (precio alto). Apuesta a que los favoritos están infravalorados."""
    def f(v: View):
        if v.side or (v.end_ts - v.now) > max_hours_left * HOUR:
            return None
        s = _side_in_band(v.price, lo, hi)
        return Signal(s) if s else None
    return f


def longshot(lo: float = 0.03, hi: float = 0.15, max_hours_left: float = 72):
    """Compra el no-favorito barato. Apuesta a que las sorpresas están infravaloradas."""
    return favorite(lo, hi, max_hours_left)


def momentum(move: float = 0.10, lookback_h: int = 6, lo: float = 0.10, hi: float = 0.90):
    """Si YES se movió ≥ `move` en las últimas `lookback_h` horas, sigue el movimiento."""
    def f(v: View):
        if v.side or not (lo <= v.price <= hi):
            return None
        past = v.ps[v.ts >= v.now - lookback_h * HOUR]
        if len(past) < 2:
            return None
        d = v.price - float(past[0])
        if abs(d) < move:
            return None
        return Signal("YES" if d > 0 else "NO")
    return f


def reversion(move: float = 0.10, lookback_h: int = 6, lo: float = 0.10, hi: float = 0.90):
    """Lo contrario de momentum: apuesta a que el movimiento brusco se revierte."""
    mom = momentum(move, lookback_h, lo, hi)

    def f(v: View):
        s = mom(v)
        return Signal("NO" if s.action == "YES" else "YES") if s else None
    return f


def coinflip(seed: int = 1, lo: float = 0.10, hi: float = 0.90, p_enter: float = 0.02):
    """Control: entra al azar en un lado al azar. Su ROI esperado ≈ -costos."""
    rng = random.Random(seed)

    def f(v: View):
        if v.side or not (lo <= v.price <= hi) or rng.random() > p_enter:
            return None
        return Signal(rng.choice(["YES", "NO"]))
    return f


REGISTRY = {
    "coinflip (control)":        coinflip(),
    "favorito 80-95, ≤72h":      favorite(0.80, 0.95, 72),
    "favorito 65-80, ≤72h":      favorite(0.65, 0.80, 72),
    "longshot 3-15, ≤72h":       longshot(0.03, 0.15, 72),
    "momentum 10pts/6h":         momentum(0.10, 6),
    "reversion 10pts/6h":        reversion(0.10, 6),
}
