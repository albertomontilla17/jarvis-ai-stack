"""
Pieza 2 — Simulador.

Reglas para que el resultado sea creíble:
  1. Sin lookahead: la estrategia solo ve precios hasta la barra actual y
     metadatos conocidos de antemano (pregunta, categoría, cierre PROGRAMADO).
     Nunca ve el resultado, el cierre real ni el volumen final.
  2. La orden decidida en la barra i se ejecuta al precio de la barra i+1.
  3. Costos: medio spread al entrar/salir + comisión taker de Polymarket
     (shares * rate * (p*(1-p))^exp) según el feeSchedule del mercado.
  4. PnL en SHARES: $1 a precio q compra 1/q shares; cada share paga $1 o $0.

Dos etapas: generate_trades() produce trades con retorno por $1 apostado
(independiente del tamaño); simulate_portfolio() les aplica bankroll y sizing.
"""

from dataclasses import dataclass, field

import numpy as np

HALF_SPREAD = 0.01   # 1 centavo por lado; el historial es precio medio, no hay libro histórico
MAX_FILL = 0.99      # no se compra por encima de esto


@dataclass
class Market:
    id: str
    slug: str
    question: str
    event: str
    category: str
    end_ts: int          # cierre programado (visible)
    closed_ts: int       # cierre real (oculto a la estrategia)
    payout_yes: float    # resultado (oculto a la estrategia)
    fee_rate: float
    fee_exp: float
    ts: np.ndarray
    ps: np.ndarray       # precio de YES en cada barra


@dataclass
class View:
    """Lo único que una estrategia puede ver en la barra actual."""
    question: str
    category: str
    end_ts: int
    now: int
    ts: np.ndarray       # barras hasta ahora (inclusive)
    ps: np.ndarray
    side: str | None     # posición abierta: "YES", "NO" o None

    @property
    def price(self) -> float:
        return float(self.ps[-1])


@dataclass
class Signal:
    action: str                 # "YES" | "NO" | "EXIT"
    prob: float | None = None   # prob. estimada de que gane ese lado (para Kelly)


@dataclass
class Trade:
    market_id: str
    event: str
    category: str
    side: str
    t_open: int
    t_close: int
    entry: float         # costo total por share (precio + spread + comisión)
    ret: float           # ganancia neta por $1 apostado
    prob: float | None = None
    held_to_resolution: bool = True


def fee_per_share(price: float, rate: float, exp: float) -> float:
    return rate * (price * (1 - price)) ** exp if rate else 0.0


def simulate_market(m: Market, strategy, half_spread: float = HALF_SPREAD,
                    fee_override: float | None = None) -> list[Trade]:
    rate = m.fee_rate if fee_override is None else fee_override
    trades: list[Trade] = []
    side = None
    cost = t_open = prob = None
    n = len(m.ts)

    for i in range(n - 1):  # la última barra no tiene "siguiente" donde ejecutar
        if m.ts[i] >= m.closed_ts:
            break
        view = View(m.question, m.category, m.end_ts, int(m.ts[i]),
                    m.ts[: i + 1], m.ps[: i + 1], side)
        sig = strategy(view)
        if sig is None:
            continue
        p_next = float(m.ps[i + 1])

        if side is None and sig.action in ("YES", "NO"):
            q = p_next if sig.action == "YES" else 1 - p_next
            fill = q + half_spread
            if fill > MAX_FILL or q <= 0:
                continue
            side, prob, t_open = sig.action, sig.prob, int(m.ts[i + 1])
            cost = fill + fee_per_share(q, rate, m.fee_exp)

        elif side is not None and sig.action == "EXIT":
            q = p_next if side == "YES" else 1 - p_next
            proceeds = max(q - half_spread - fee_per_share(q, rate, m.fee_exp), 0.0)
            trades.append(Trade(m.id, m.event, m.category, side, t_open, int(m.ts[i + 1]),
                                cost, proceeds / cost - 1, prob, False))
            side = None

    if side is not None:  # liquidación a resolución
        payout = m.payout_yes if side == "YES" else 1 - m.payout_yes
        trades.append(Trade(m.id, m.event, m.category, side, t_open, m.closed_ts,
                            cost, payout / cost - 1, prob, True))
    return trades


def generate_trades(markets: list[Market], strategy, **kw) -> list[Trade]:
    out: list[Trade] = []
    for m in markets:
        out.extend(simulate_market(m, strategy, **kw))
    return out


@dataclass
class Portfolio:
    bankroll0: float
    final: float
    max_drawdown: float
    taken: int
    skipped: int
    curve: list[tuple[int, float]] = field(default_factory=list)


def kelly_fraction(prob: float, cost: float) -> float:
    """f* = (p - costo) / (1 - costo) para un share que paga $1."""
    if cost >= 1 or prob <= cost:
        return 0.0
    return (prob - cost) / (1 - cost)


def simulate_portfolio(trades: list[Trade], bankroll: float = 100.0, stake_pct: float = 0.02,
                       kelly: float | None = None, max_position: float = 0.10) -> Portfolio:
    """
    Aplica capital real a los trades en orden cronológico. El dinero de una
    posición queda bloqueado hasta su cierre.
      - Por defecto: apuesta fija = stake_pct del capital actual.
      - kelly=0.5: medio-Kelly usando Signal.prob, con techo max_position.
    """
    events = sorted(
        [(t.t_open, 1, i) for i, t in enumerate(trades)] +
        [(t.t_close, 0, i) for i, t in enumerate(trades)]  # cierres antes que aperturas
    )
    cash, locked = bankroll, {}
    peak, mdd, taken, skipped, curve = bankroll, 0.0, 0, 0, []
    for ts, kind, i in events:
        t = trades[i]
        if kind == 1:
            equity = cash + sum(locked.values())
            if kelly is not None and t.prob is not None:
                frac = min(kelly_fraction(t.prob, t.entry) * kelly, max_position)
            else:
                frac = stake_pct
            stake = equity * frac
            if stake < 1.0 or stake > cash:
                skipped += 1
                continue
            cash -= stake
            locked[i] = stake
            taken += 1
        elif i in locked:
            cash += locked.pop(i) * (1 + t.ret)
            equity = cash + sum(locked.values())
            peak = max(peak, equity)
            mdd = max(mdd, 1 - equity / peak)
            curve.append((ts, equity))
    return Portfolio(bankroll, cash + sum(locked.values()), mdd, taken, skipped, curve)


def load_markets(con, min_bars: int = 6) -> list[Market]:
    rows = con.execute(
        "SELECT id, slug, question, event, category, end_ts, closed_ts, payout_yes, fee_rate, fee_exp"
        " FROM markets WHERE has_prices = 1 ORDER BY closed_ts").fetchall()
    out = []
    for r in rows:
        pr = con.execute("SELECT t, p FROM prices WHERE market_id = ? ORDER BY t", (r[0],)).fetchall()
        if len(pr) < min_bars:
            continue
        out.append(Market(*r, ts=np.array([x[0] for x in pr], dtype=np.int64),
                          ps=np.array([x[1] for x in pr], dtype=float)))
    return out
