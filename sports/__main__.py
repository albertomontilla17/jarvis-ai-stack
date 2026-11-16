"""
CLI del analizador deportivo.

    python -m sports download            # resultados ESPN + mercados y precios Kalshi
    python -m sports evaluate            # ¿el modelo de stats le gana al mercado?
    python -m sports evaluate --leagues nba nfl

    python -m sports live                # registrar pronósticos de los partidos próximos
    python -m sports adjust <gid> 0.62 "motivo"   # probabilidad ajustada del local
    python -m sports results             # mercado vs modelo vs modelo ajustado, en vivo
"""

import argparse

import datetime as dt

from . import LEAGUES, data, evaluate, live

LIVE = ["nba", "nhl", "nfl"]

FIRST_MONTH = {"nba": "202110", "nhl": "202110", "nfl": "202109", "mlb": "202104"}


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


def main():
    p = argparse.ArgumentParser(prog="sports", description="Analizador de stats deportivos de Jarvis")
    p.add_argument("--db", default=data.DB_PATH)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("download", cmd_download), ("evaluate", cmd_evaluate)):
        s = sub.add_parser(name)
        s.add_argument("--leagues", nargs="*", default=list(LEAGUES), choices=list(LEAGUES))
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
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
