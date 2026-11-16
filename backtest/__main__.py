"""
CLI del backtester.

    python -m backtest download --from 2026-05-01 --to 2026-11-01 --sample 3000
    python -m backtest calibration
    python -m backtest run
"""

import argparse
import datetime as dt

from . import data, engine, report
from .strategies import REGISTRY


def cmd_download(a):
    con = data.connect(a.db)
    print(f"📥 Listando mercados resueltos {a.date_from} → {a.date_to} (vol ≥ ${a.min_volume:,.0f})")
    data.download_markets(con, dt.date.fromisoformat(a.date_from),
                          dt.date.fromisoformat(a.date_to), a.min_volume)
    print(f"📈 Bajando historial horario de una muestra aleatoria de {a.sample} mercados")
    data.download_prices(con, a.sample, a.max_days, seed=a.seed)
    tot, with_p = con.execute(
        "SELECT COUNT(*), SUM(has_prices = 1) FROM markets").fetchone()
    print(f"✅ {tot} mercados en la base, {with_p or 0} con historial de precios")


def _load(a):
    markets = engine.load_markets(data.connect(a.db))
    if not markets:
        raise SystemExit("No hay datos. Corre primero: python -m backtest download")
    return markets


def cmd_calibration(a):
    markets = _load(a)
    rows = report.calibration(markets, a.hours)
    print(f"\n📐 CALIBRACIÓN — precio de YES {a.hours:.0f}h antes del cierre programado "
          f"vs. frecuencia real ({sum(r['n'] for r in rows)} mercados)\n")
    print(f"{'precio':>9s} {'n':>6s} {'precio medio':>13s} {'ganó YES':>9s} {'diferencia':>11s}")
    for r in rows:
        mark = "  ← fuera de ±2 SE" if abs(r["dif"]) > 2 * r["se"] else ""
        print(f"{r['rango']:>9s} {r['n']:6d} {r['precio']:13.3f} {r['real']:9.3f} {r['dif']:+11.3f}{mark}")


def cmd_run(a):
    markets = _load(a)
    if a.category:
        markets = [m for m in markets if m.category == a.category]
    t0, t1 = markets[0].closed_ts, markets[-1].closed_ts
    cutoff = t0 + (t1 - t0) * 0.6
    day = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).date()
    print(f"\n🧪 BACKTEST — {len(markets)} mercados, {day(t0)} → {day(t1)}")
    print(f"   costos: medio spread {a.half_spread:.3f} + comisión "
          f"{'del mercado' if a.fee is None else f'fija {a.fee}'}")
    print(f"   corte dentro/fuera de muestra: {day(cutoff)}\n")

    for label, pick in (("TODO EL PERIODO", None), ("DENTRO DE MUESTRA (primer 60%)", 0),
                        ("FUERA DE MUESTRA (último 40%)", 1)):
        print(f"── {label} " + "─" * (70 - len(label)))
        print(report.HEADER)
        for name, strat in REGISTRY.items():
            trades = engine.generate_trades(markets, strat, half_spread=a.half_spread,
                                            fee_override=a.fee)
            if pick is not None:
                trades = report.split_by_time(trades, cutoff)[pick]
            print(report.fmt_row(name, report.summarize(trades)))
            if pick is None and a.bankroll:
                pf = engine.simulate_portfolio(trades, a.bankroll, a.stake_pct)
                print(f"{'':26s} ↳ ${a.bankroll:.0f} → ${pf.final:.2f} apostando {a.stake_pct:.0%} "
                      f"por trade | caída máx. {pf.max_drawdown:.0%} | {pf.taken} trades")
        print()
    print("ROI/$ = ganancia neta por dólar apostado, después de costos.")
    print("✅ edge = el límite inferior del IC 95% (bootstrap por evento) es > 0.")


def main():
    p = argparse.ArgumentParser(prog="backtest", description="Backtester de Jarvis para Polymarket")
    p.add_argument("--db", default=data.DB_PATH)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("download", help="Bajar mercados resueltos + precios")
    d.add_argument("--from", dest="date_from", required=True)
    d.add_argument("--to", dest="date_to", required=True)
    d.add_argument("--min-volume", type=float, default=50_000)
    d.add_argument("--sample", type=int, default=3000, help="mercados a los que bajar precios")
    d.add_argument("--max-days", type=int, default=30, help="días de historial antes del cierre")
    d.add_argument("--seed", type=int, default=7)
    d.set_defaults(fn=cmd_download)

    c = sub.add_parser("calibration", help="¿El precio ya es una buena probabilidad?")
    c.add_argument("--hours", type=float, default=24)
    c.set_defaults(fn=cmd_calibration)

    r = sub.add_parser("run", help="Correr las estrategias base")
    r.add_argument("--half-spread", type=float, default=engine.HALF_SPREAD)
    r.add_argument("--fee", type=float, default=None, help="forzar tasa de comisión (p.ej. 0.05)")
    r.add_argument("--category", default=None)
    r.add_argument("--bankroll", type=float, default=100.0)
    r.add_argument("--stake-pct", type=float, default=0.02)
    r.set_defaults(fn=cmd_run)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
