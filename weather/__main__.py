"""
CLI del analizador de temperatura.

    python -m weather download --from 2026-04-01     # NBM + mercados + cotizaciones
    python -m weather evaluate                       # ¿le gana el pronóstico al mercado?
"""

import argparse
import datetime as dt

from . import DECISIONS, data, evaluate


def main():
    p = argparse.ArgumentParser(prog="weather", description="Analizador de temperatura máxima de Jarvis")
    p.add_argument("--db", default=data.DB_PATH)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download")
    d.add_argument("--from", dest="start", default="2026-04-01")
    e = sub.add_parser("evaluate")
    e.add_argument("--decision", nargs="*", default=list(DECISIONS), choices=list(DECISIONS))
    a = p.parse_args()
    con = data.connect(a.db)
    if a.cmd == "download":
        data.download_all(con, dt.date.fromisoformat(a.start), dt.date.today() + dt.timedelta(days=1))
    else:
        for dec in a.decision:
            evaluate.run(con, dec)
        print("\nROI/$ = ganancia neta por dólar, comprando al precio real (ask, o 1 − bid para el No) con comisión.")


if __name__ == "__main__":
    main()
