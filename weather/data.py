"""
Datos de temperatura. Fuentes públicas, sin API key:

  - Iowa Environmental Mesonet (IEM): archivo de pronósticos NBM del NWS por estación,
    con su hora de emisión (runtime), para usar solo lo que se sabía al decidir.
  - Kalshi: mercados de rango de temperatura ya liquidados (con el valor oficial de
    liquidación) y el bid/ask real en el momento de decidir.

Todo va a weather_data.db.
"""

import csv
import io
import re
import time
import sqlite3
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import requests

from . import CITIES, DECISIONS

IEM_MOS = "https://mesonet.agron.iastate.edu/cgi-bin/request/mos.py"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
DB_PATH = "weather_data.db"
HEADERS = {"User-Agent": "jarvis-weather-backtest/1.0"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS nbm (            -- pronóstico de máxima diaria
    station TEXT, runtime INTEGER, target TEXT,     -- target = fecha local AAAA-MM-DD
    txn REAL, xnd REAL, PRIMARY KEY (station, runtime, target));
CREATE TABLE IF NOT EXISTS markets (
    ticker TEXT PRIMARY KEY, series TEXT, event TEXT, target TEXT,
    floor REAL, cap REAL, strike_type TEXT,         -- between: floor..cap; less: < cap; greater: > floor
    won INTEGER, settle_value REAL, historical INTEGER);
CREATE TABLE IF NOT EXISTS quotes (
    ticker TEXT, decision TEXT, bid REAL, ask REAL, ts INTEGER, PRIMARY KEY (ticker, decision));
"""


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def _get(url: str, params: dict, tries: int = 6, as_json: bool = True):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=120)
            if r.status_code == 200:
                return r.json() if as_json else r.text
            last = f"HTTP {r.status_code}"
            if r.status_code in (400, 404):
                break
        except (requests.RequestException, ValueError) as e:
            last = str(e)
        time.sleep(1.5 * (i + 1))   # también cubre el límite de peticiones (429)
    raise RuntimeError(f"{url} falló: {last}")


def _ts(s: str) -> int:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


# ── NBM (IEM) ─────────────────────────────────────────────────────────────────
def download_nbm(con, station: str, start: dt.date, end: dt.date) -> int:
    """Máximas pronosticadas (txn en ftime 00Z = máxima del día anterior en EE. UU.)."""
    n = 0
    day = start
    while day < end:
        nxt = min(day + dt.timedelta(days=31), end)
        text = _get(IEM_MOS, {"station": station, "model": "NBS", "format": "csv",
                              "sts": f"{day}T00:00Z", "ets": f"{nxt}T00:00Z"}, as_json=False)
        for r in csv.DictReader(io.StringIO(text)):
            if not r.get("txn") or not r["ftime"].endswith("00:00:00"):
                continue
            ftime = dt.datetime.fromisoformat(r["ftime"])
            target = (ftime - dt.timedelta(days=1)).date().isoformat()
            runtime = int(dt.datetime.fromisoformat(r["runtime"]).replace(tzinfo=dt.timezone.utc).timestamp())
            con.execute("INSERT OR REPLACE INTO nbm VALUES (?, ?, ?, ?, ?)",
                        (station, runtime, target, float(r["txn"]), float(r["xnd"] or 0) or None))
            n += 1
        con.commit()
        day = nxt
    return n


# ── Kalshi ────────────────────────────────────────────────────────────────────
MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG",
                                      "SEP", "OCT", "NOV", "DEC"], 1)}


def event_date(event: str) -> str | None:
    """'KXHIGHNY-26NOV18' → '2026-11-18'."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})$", event)
    if not m or m.group(2) not in MONTHS:
        return None
    return dt.date(2000 + int(m.group(1)), MONTHS[m.group(2)], int(m.group(3))).isoformat()


def _markets(series: str, historical: bool):
    cursor, path = "", "historical/markets" if historical else "markets"
    while True:
        params = {"series_ticker": series, "limit": 1000}
        if not historical:
            params["status"] = "settled"
        if cursor:
            params["cursor"] = cursor
        d = _get(f"{KALSHI}/{path}", params)
        yield from d.get("markets", [])
        cursor = d.get("cursor")
        if not cursor or not d.get("markets"):
            return


def download_markets(con, series: str) -> int:
    n = 0
    for historical in (True, False):
        for m in _markets(series, historical):
            target = event_date(m.get("event_ticker", ""))
            if m.get("result") not in ("yes", "no") or not target:
                continue
            try:
                value = float(m.get("expiration_value"))
            except (TypeError, ValueError):
                value = None
            con.execute("INSERT OR IGNORE INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (m["ticker"], series, m["event_ticker"], target, m.get("floor_strike"),
                         m.get("cap_strike"), m.get("strike_type"), int(m["result"] == "yes"),
                         value, int(historical)))
            n += 1
    con.commit()
    return n


def decision_ts(target: str, decision: str) -> int:
    day_offset, hour, _ = DECISIONS[decision]
    d = dt.date.fromisoformat(target) + dt.timedelta(days=day_offset)
    return int(dt.datetime(d.year, d.month, d.day, hour, tzinfo=dt.timezone.utc).timestamp())


def _price(obj, key="close"):
    if not obj:
        return None
    v = obj.get(f"{key}_dollars", obj.get(key))
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def download_quotes(con, series: str, decision: str, since: str, workers: int = 4) -> int:
    """Último bid/ask real de cada rango antes del momento de decisión (días desde `since`)."""
    rows = con.execute(
        "SELECT ticker, target, historical FROM markets WHERE series = ? AND target >= ? AND ticker NOT IN"
        " (SELECT ticker FROM quotes WHERE decision = ?)", (series, since, decision)).fetchall()

    def job(row):
        ticker, target, historical = row
        t = decision_ts(target, decision)
        path = (f"historical/markets/{ticker}/candlesticks" if historical
                else f"series/{series}/markets/{ticker}/candlesticks")
        try:
            d = _get(f"{KALSHI}/{path}", {"start_ts": t - 12 * 3600, "end_ts": t, "period_interval": 60})
        except RuntimeError:
            return ticker, None
        best = None
        for c in d.get("candlesticks", []):
            bid, ask = _price(c.get("yes_bid")), _price(c.get("yes_ask"))
            if c["end_period_ts"] <= t and bid is not None and ask is not None and ask > 0:
                best = (bid, ask, c["end_period_ts"])
        return ticker, best

    done = 0
    with ThreadPoolExecutor(workers) as ex:
        for ticker, q in ex.map(job, rows):
            bid, ask, ts = q if q else (None, None, 0)
            con.execute("INSERT OR REPLACE INTO quotes VALUES (?, ?, ?, ?, ?)", (ticker, decision, bid, ask, ts))
            done += 1
            if done % 500 == 0:
                con.commit()
                print(f"  {series}/{decision}: {done}/{len(rows)} cotizaciones", flush=True)
    con.commit()
    return done


def download_all(con, start: dt.date, end: dt.date, decisions=tuple(DECISIONS)) -> None:
    for series, (station, _) in CITIES.items():
        print(f"📥 {series}: mercados, NBM de {station}", flush=True)
        download_markets(con, series)
        download_nbm(con, station, start, end)
        for dec in decisions:
            download_quotes(con, series, dec, start.isoformat())
