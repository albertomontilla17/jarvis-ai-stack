"""
Datos deportivos. Dos fuentes públicas, sin API key:

  - ESPN: resultados de todos los partidos (para calcular las stats).
  - Kalshi: mercados "¿quién gana el partido?" ya liquidados y su bid/ask REAL
    justo antes del inicio (para saber a qué precio se habría podido apostar).

Todo va a sports_data.db.
"""

import re
import time
import sqlite3
import unicodedata
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import requests

from . import LEAGUES

ESPN = "https://site.api.espn.com/apis/site/v2/sports"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
DB_PATH = "sports_data.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    gid TEXT PRIMARY KEY, league TEXT, ts INTEGER, season INTEGER, stype INTEGER,
    home TEXT, away TEXT, home_score INTEGER, away_score INTEGER);
CREATE TABLE IF NOT EXISTS teams (
    league TEXT, abbr TEXT, location TEXT, name TEXT, PRIMARY KEY (league, abbr));
CREATE TABLE IF NOT EXISTS kalshi (
    ticker TEXT PRIMARY KEY, league TEXT, event TEXT, team TEXT,   -- team = abreviatura ESPN
    won INTEGER, close_ts INTEGER, historical INTEGER, gid TEXT,
    bid REAL, ask REAL, quote_ts INTEGER);                         -- cotización previa al partido
"""


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def _get(url: str, params: dict | None = None, tries: int = 6):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=60)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
            if r.status_code in (400, 404):
                break
        except (requests.RequestException, ValueError) as e:
            last = str(e)
        time.sleep(1.0 * (i + 1))   # también cubre el límite de peticiones (429)
    raise RuntimeError(f"{url} falló: {last}")


def _ts(s: str) -> int:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


# ── ESPN ──────────────────────────────────────────────────────────────────────
def download_games(con, league: str, first_month: str) -> int:
    """Baja todos los partidos terminados desde first_month (AAAAMM) hasta hoy."""
    y, m = int(first_month[:4]), int(first_month[4:])
    today = dt.date.today()
    n = 0
    while (y, m) <= (today.year, today.month):
        d = _get(f"{ESPN}/{LEAGUES[league]['espn']}/scoreboard",
                 {"dates": f"{y}{m:02d}", "limit": 1000})
        for e in d.get("events", []):
            c = e["competitions"][0]
            stype = int(e.get("season", {}).get("type", 0))
            if not e["status"]["type"].get("completed") or stype not in (2, 3):
                continue   # solo temporada regular y playoffs, ya terminados
            side = {t["homeAway"]: t for t in c["competitors"]}
            if set(side) != {"home", "away"}:
                continue
            try:
                hs, as_ = int(side["home"]["score"]), int(side["away"]["score"])
            except (KeyError, ValueError, TypeError):
                continue
            for t in side.values():
                tm = t["team"]
                con.execute("INSERT OR REPLACE INTO teams VALUES (?, ?, ?, ?)",
                            (league, tm["abbreviation"], tm.get("location", ""), tm.get("name", "")))
            con.execute("INSERT OR REPLACE INTO games VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (f"{league}-{e['id']}", league, _ts(e["date"]), int(e["season"]["year"]), stype,
                         side["home"]["team"]["abbreviation"], side["away"]["team"]["abbreviation"],
                         hs, as_))
            n += 1
        con.commit()
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return n


# ── Emparejar nombres Kalshi ↔ ESPN ───────────────────────────────────────────
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def team_lookup(con, league: str) -> dict[str, str]:
    """
    Kalshi nombra equipos por ciudad ("Sacramento") y desambigua con iniciales
    ("Los Angeles L", "Chicago WS", "A's"). Devuelve {nombre normalizado: abreviatura ESPN}.
    """
    rows = con.execute("SELECT abbr, location, name FROM teams WHERE league = ?", (league,)).fetchall()
    out, by_loc = {}, {}
    for abbr, loc, name in rows:
        by_loc.setdefault(_norm(loc), []).append(abbr)
        initials = "".join(w[0] for w in name.split() if w)
        for cand in (f"{loc} {name[:1]}", f"{loc} {initials}", name, f"{loc} {name}",
                     f"{abbr} {name}"):
            out.setdefault(_norm(cand), abbr)
    for loc, abbrs in by_loc.items():
        if len(abbrs) == 1:
            out[loc] = abbrs[0]
    out.setdefault("as", out.get("athletics", ""))
    return out


# ── Kalshi ────────────────────────────────────────────────────────────────────
def _kalshi_markets(series: str, historical: bool):
    cursor = ""
    path = "historical/markets" if historical else "markets"
    while True:
        params = {"series_ticker": series, "limit": 500}
        if not historical:
            params["status"] = "settled"
        if cursor:
            params["cursor"] = cursor
        d = _get(f"{KALSHI}/{path}", params)
        yield from d.get("markets", [])
        cursor = d.get("cursor")
        if not cursor or not d.get("markets"):
            return


def download_kalshi(con, league: str) -> int:
    """Lista mercados de partido liquidados y los empareja con el partido de ESPN."""
    lookup = team_lookup(con, league)
    abbrs = {r[0] for r in con.execute("SELECT abbr FROM teams WHERE league = ?", (league,))}
    n = miss = 0
    for historical in (True, False):
        for m in _kalshi_markets(LEAGUES[league]["kalshi"], historical):
            if m.get("result") not in ("yes", "no"):
                continue
            team = lookup.get(_norm(m.get("yes_sub_title", "")))
            if not team:   # último recurso: el sufijo del ticker suele ser la abreviatura
                suffix = m["ticker"].rsplit("-", 1)[-1]
                team = suffix if suffix in abbrs else None
            close_ts = _ts(m["close_time"])
            if not team:
                miss += 1
                continue
            # El mercado cierra al terminar el partido: el partido empezó poco antes.
            g = con.execute(
                "SELECT gid FROM games WHERE league = ? AND (home = ? OR away = ?)"
                " AND ts < ? AND ts > ? ORDER BY ts DESC LIMIT 1",
                (league, team, team, close_ts, close_ts - 10 * 3600)).fetchone()
            if not g:
                miss += 1
                continue
            con.execute(
                "INSERT OR IGNORE INTO kalshi (ticker, league, event, team, won, close_ts, historical, gid)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (m["ticker"], league, m["event_ticker"], team, int(m["result"] == "yes"),
                 close_ts, int(historical), g[0]))
            n += 1
    con.commit()
    print(f"  {league}: {n} mercados emparejados, {miss} sin partido de ESPN (pretemporada, etc.)")
    return n


def _price(obj: dict, key: str) -> float | None:
    v = (obj or {}).get(f"{key}_dollars", (obj or {}).get(key))
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def download_quotes(con, league: str, minutes_before: int = 30, workers: int = 6) -> int:
    """
    Para cada partido, la última cotización REAL (bid y ask) al menos
    `minutes_before` minutos antes del inicio. Es el precio al que el modelo
    habría podido apostar sin saber nada del partido.
    """
    rows = con.execute(
        "SELECT k.ticker, k.historical, g.ts FROM kalshi k JOIN games g ON g.gid = k.gid"
        " WHERE k.league = ? AND k.quote_ts IS NULL"
        # Un mercado por partido basta: comprar al rival cuesta 1 - bid de este.
        " AND k.ticker IN (SELECT MIN(ticker) FROM kalshi GROUP BY gid)", (league,)).fetchall()
    series = LEAGUES[league]["kalshi"]

    def job(row):
        ticker, historical, start = row
        cutoff = start - minutes_before * 60
        path = (f"historical/markets/{ticker}/candlesticks" if historical
                else f"series/{series}/markets/{ticker}/candlesticks")
        try:
            d = _get(f"{KALSHI}/{path}",
                     {"start_ts": cutoff - 24 * 3600, "end_ts": cutoff, "period_interval": 60})
        except RuntimeError:
            return ticker, None
        best = None
        for c in d.get("candlesticks", []):
            bid, ask = _price(c.get("yes_bid"), "close"), _price(c.get("yes_ask"), "close")
            if c["end_period_ts"] <= cutoff and bid and ask and 0 < bid < ask < 1:
                best = (bid, ask, c["end_period_ts"])
        return ticker, best

    done = 0
    with ThreadPoolExecutor(workers) as ex:
        for ticker, q in ex.map(job, rows):
            bid, ask, qts = q if q else (None, None, 0)   # quote_ts = 0: intentado, sin cotización
            con.execute("UPDATE kalshi SET bid = ?, ask = ?, quote_ts = ? WHERE ticker = ?",
                        (bid, ask, qts, ticker))
            done += 1
            if done % 500 == 0:
                con.commit()
                print(f"  {league}: {done}/{len(rows)} cotizaciones", flush=True)
    con.commit()
    return done
