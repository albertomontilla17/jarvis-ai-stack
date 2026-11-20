"""
CLI del analizador deportivo.

    python -m sports download            # resultados ESPN + mercados y precios Kalshi
    python -m sports evaluate            # ¿el modelo de stats le gana al mercado?
    python -m sports evaluate --leagues nba nfl

    python -m sports live                # registrar pronósticos de los partidos próximos
    python -m sports adjust <gid> 0.62 "motivo"   # probabilidad ajustada del local
    python -m sports results             # mercado vs modelo vs modelo ajustado, en vivo
    python -m sports propose --amount 1  # propuestas de orden para Kalshi (proposals/kalshi/)

    python -m sports sharp-download      # cuota de la casa (ESPN) + precio de Kalshi justo antes del inicio
    python -m sports sharp-evaluate      # ¿Kalshi se desvía de la casa lo bastante para ganar?
"""

import os
import argparse

import datetime as dt

from . import LEAGUES, data, evaluate, live, propose, sharp

LIVE = ["nba", "nhl", "nfl"]

FIRST_MONTH = {"nba": "202110", "nhl": "202110", "nfl": "202109", "mlb": "202104", "ncaamb": "202111"}


def cmd_download(a):
    con = data.connect(a.db)
    for lg in a.leagues:
        print(f"📥 {lg}: resultados de ESPN desde {FIRST_MONTH[lg]}")
        data.download_games(con, lg, FIRST_MONTH[lg])
        data.download_kalshi(con, lg)
        print(f"📈 {lg}: cotizaciones previas a cada partido")
        data.download_quotes(con, lg)


def cmd_evaluate(a):
    evaluate.run(data.connect(a.db), a.leagues)
    print("\nROI/$ = ganancia neta por dólar apostado, comprando al ask real y pagando comisión.")
    print("✅ edge = el límite inferior del IC 95% es > 0.")


def cmd_sharp_download(a):
    con = sharp.connect(a.db)
    for lg in a.leagues:
        print(f"📥 {lg}: cuotas de la casa (ESPN)")
        sharp.download_odds(con, lg)
        print(f"📈 {lg}: precio de Kalshi {a.minutes} min antes del inicio")
        sharp.download_near_quotes(con, lg, a.minutes)


def cmd_sharp_evaluate(a):
    sharp.run(sharp.connect(a.db), a.leagues)


def cmd_live(a):
    rows = live.snapshot(data.connect(a.db), live.connect(a.log), a.leagues, a.hours)
    if not rows:
        print(f"⏸️  Sin partidos con mercado en las próximas {a.hours:g} h.")
    for r in sorted(rows, key=lambda r: r["start_ts"]):
        start = dt.datetime.fromtimestamp(r["start_ts"], dt.timezone.utc).strftime("%d/%m %H:%M UTC")
        mid = (r["home_bid"] + r["home_ask"]) / 2
        print(f"\n🏟️  {r['gid']}  {r['away']} @ {r['home']}  ({start})")
        print(f"   Local {r['home']}: modelo {r['p_model']:.1%} | Kalshi {r['home_bid']:.2f}/{r['home_ask']:.2f}"
              f" (medio {mid:.1%}) | diferencia {r['p_model'] - mid:+.1%}")
        if r.get("p_book") is not None:
            print(f"   Casa ({r['book']}): local {r['p_book']:.1%} sin margen | diferencia con Kalshi {r['p_book'] - mid:+.1%}")
        for team, players in r["injuries"].items():
            names = ", ".join(f"{n} ({pos}, {st})" for n, pos, st in players) or "sin bajas"
            print(f"   Bajas {team}: {names}")


def cmd_adjust(a):
    live.adjust(live.connect(a.log), a.gid, a.p, a.note)
    print(f"✅ {a.gid}: probabilidad ajustada del local = {a.p:.1%}")


def cmd_results(a):
    stats, log = data.connect(a.db), live.connect(a.log)
    live.settle(stats, log)
    live.results(log)


def cmd_propose(a):
    here = propose.HERE
    props = propose.build(os.path.join(here, "paper.db"), os.path.join(here, live.LOG_PATH),
                          a.strategy, a.amount)
    if not props:
        print(f"⏸️  Sin propuestas: no hay operaciones abiertas de {a.strategy} antes del partido.")
        return
    for p, path in zip(props, propose.write(props)):
        print(f"💡 {p['question']} | Sí en {p['ticker']} a ≤ {p['price']:.2f} | ${p['amount_usd']:.2f}"
              f" | modelo {p['prob']:.0%}\n   python jarvis_kalshi.py --order {os.path.relpath(path, here)}")


def main():
    p = argparse.ArgumentParser(prog="sports", description="Analizador de stats deportivos de Jarvis")
    p.add_argument("--db", default=data.DB_PATH)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("download", cmd_download), ("evaluate", cmd_evaluate)):
        s = sub.add_parser(name)
        s.add_argument("--leagues", nargs="*", default=list(LEAGUES), choices=list(LEAGUES))
        s.set_defaults(fn=fn)
    for name, fn in (("sharp-download", cmd_sharp_download), ("sharp-evaluate", cmd_sharp_evaluate)):
        s = sub.add_parser(name)
        s.add_argument("--leagues", nargs="*", default=LIVE, choices=list(LEAGUES))
        s.add_argument("--minutes", type=int, default=5, help="minutos antes del inicio para el precio de Kalshi")
        s.set_defaults(fn=fn)
    p.add_argument("--log", default=live.LOG_PATH)
    lv = sub.add_parser("live")
    lv.add_argument("--leagues", nargs="*", default=LIVE, choices=list(LEAGUES))
    lv.add_argument("--hours", type=float, default=3.0, help="partidos que empiezan en las próximas N horas")
    lv.set_defaults(fn=cmd_live)
    ad = sub.add_parser("adjust")
    ad.add_argument("gid")
    ad.add_argument("p", type=float, help="probabilidad de victoria del LOCAL, 0.01–0.99")
    ad.add_argument("note")
    ad.set_defaults(fn=cmd_adjust)
    sub.add_parser("results").set_defaults(fn=cmd_results)
    pp = sub.add_parser("propose")
    pp.add_argument("--strategy", default="deportes_ajustado")
    pp.add_argument("--amount", type=float, default=1.0, help="USD por propuesta")
    pp.set_defaults(fn=cmd_propose)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
