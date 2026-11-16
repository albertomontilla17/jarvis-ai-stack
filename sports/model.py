"""
Modelo de stats: probabilidad de que gane el local.

Tres ingredientes, todos calculados SOLO con partidos anteriores al que se predice:
  - Elo: fuerza de cada equipo, actualizada tras cada partido (con margen de
    victoria y ventaja de local), con regresión parcial a la media entre temporadas.
  - Descanso: diferencia de días desde el último partido de cada equipo.
  - Racha: diferencia de margen medio en los últimos N partidos.

Una regresión logística combina los tres. Sus pesos y los parámetros del Elo
(K y ventaja de local) se ajustan únicamente con partidos anteriores a `cutoff`.
"""

from collections import defaultdict, deque
from dataclasses import dataclass

import numpy as np

CARRY = 0.75          # cuánto Elo se conserva de una temporada a la siguiente
FORM_N = {"nfl": 5}   # partidos de racha (10 por defecto)
K_GRID = {"nba": (10, 15, 20, 25), "nhl": (4, 6, 8, 10), "nfl": (15, 20, 25, 30), "mlb": (2, 3, 4, 6)}
HFA_GRID = (0, 20, 40, 60, 80, 100)


@dataclass
class Game:
    gid: str
    ts: int
    season: int
    home: str
    away: str
    home_score: int
    away_score: int


def load_games(con, league: str) -> list[Game]:
    rows = con.execute(
        "SELECT gid, ts, season, home, away, home_score, away_score FROM games"
        " WHERE league = ? AND home_score != away_score ORDER BY ts, gid", (league,)).fetchall()
    return [Game(*r) for r in rows]


def expected(diff: float) -> float:
    return 1.0 / (1.0 + 10 ** (-diff / 400.0))


def features(games: list[Game], k: float, hfa: float, form_n: int, mov_norm: float) -> np.ndarray:
    """Una fila por partido: [elo_diff/400, descanso_local - descanso_visita, racha_local - racha_visita]."""
    elo = defaultdict(lambda: 1500.0)
    last_ts, last_season = {}, {}
    form = defaultdict(lambda: deque(maxlen=form_n))
    X = np.zeros((len(games), 3))
    for i, g in enumerate(games):
        for t in (g.home, g.away):
            if last_season.get(t, g.season) != g.season:
                elo[t] = CARRY * elo[t] + (1 - CARRY) * 1500.0
            last_season[t] = g.season
        diff = elo[g.home] + hfa - elo[g.away]
        rest = [min((g.ts - last_ts[t]) / 86400, 4.0) if t in last_ts else 4.0 for t in (g.home, g.away)]
        frm = [float(np.mean(form[t])) if form[t] else 0.0 for t in (g.home, g.away)]
        X[i] = (diff / 400.0, rest[0] - rest[1], (frm[0] - frm[1]) / mov_norm)

        # ── a partir de aquí se usa el resultado: solo afecta a partidos FUTUROS ──
        margin = g.home_score - g.away_score
        won = 1.0 if margin > 0 else 0.0
        mult = np.log1p(abs(margin)) / np.log1p(mov_norm)
        delta = k * mult * (won - expected(diff))
        elo[g.home] += delta
        elo[g.away] -= delta
        form[g.home].append(margin / 1.0)
        form[g.away].append(-margin / 1.0)
        last_ts[g.home] = last_ts[g.away] = g.ts
    return X


def fit_logistic(X: np.ndarray, y: np.ndarray, iters: int = 30) -> tuple[np.ndarray, np.ndarray]:
    """Regresión logística por Newton. Devuelve (coeficientes, errores estándar); X sin intercepto."""
    A = np.column_stack([np.ones(len(X)), X])
    w = np.zeros(A.shape[1])
    H = np.eye(A.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-A @ w))
        H = A.T @ (A * (p * (1 - p))[:, None]) + 1e-8 * np.eye(A.shape[1])
        step = np.linalg.solve(H, A.T @ (y - p))
        w += step
        if np.abs(step).max() < 1e-9:
            break
    return w, np.sqrt(np.diag(np.linalg.inv(H)))


def predict(w: np.ndarray, X: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-(w[0] + X @ w[1:])))


def log_loss(p: np.ndarray, y: np.ndarray) -> float:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


@dataclass
class Fitted:
    league: str
    games: list[Game]
    X: np.ndarray
    y: np.ndarray
    w: np.ndarray
    p: np.ndarray        # prob. de victoria local para cada partido (válida fuera de muestra si ts >= cutoff)
    k: float
    hfa: float
    n_train: int


def fit(con, league: str, cutoff_ts: int) -> Fitted:
    games = load_games(con, league)
    y = np.array([1.0 if g.home_score > g.away_score else 0.0 for g in games])
    train = np.array([g.ts < cutoff_ts for g in games])
    if train.sum() < 200:
        raise ValueError(f"{league}: muy pocos partidos de entrenamiento antes del corte")
    mov_norm = float(np.mean([abs(g.home_score - g.away_score) for g, t in zip(games, train) if t]))
    form_n = FORM_N.get(league, 10)

    # Elegir K y ventaja de local por log-loss del Elo puro, solo en entrenamiento
    # (se descarta el primer 20% porque ahí el Elo aún no ha convergido).
    warm = int(train.sum() * 0.2)
    best = None
    for k in K_GRID[league]:
        for hfa in HFA_GRID:
            X = features(games, k, hfa, form_n, mov_norm)
            ll = log_loss(1 / (1 + 10 ** (-X[train][warm:, 0])), y[train][warm:])
            if best is None or ll < best[0]:
                best = (ll, k, hfa, X)
    _, k, hfa, X = best
    w, _ = fit_logistic(X[train][warm:], y[train][warm:])
    return Fitted(league, games, X, y, w, predict(w, X), k, hfa, int(train.sum()))
