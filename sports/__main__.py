"""
CLI del analizador deportivo.

    python -m sports download            # resultados ESPN + mercados y precios Kalshi
    python -m sports evaluate            # ¿el modelo de stats le gana al mercado?
    python -m sports evaluate --leagues nba nfl
"""

import argparse

from . import LEAGUES, data, evaluate

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


def main():
    p = argparse.ArgumentParser(prog="sports", description="Analizador de stats deportivos de Jarvis")
    p.add_argument("--db", default=data.DB_PATH)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("download", cmd_download), ("evaluate", cmd_evaluate)):
        s = sub.add_parser(name)
        s.add_argument("--leagues", nargs="*", default=list(LEAGUES), choices=list(LEAGUES))
        s.set_defaults(fn=fn)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
