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
    for table, col, typ in (("games", "neutral", "INTEGER DEFAULT 0"), ("teams", "short", "TEXT")):
        if col not in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")   # migración no destructiva
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
def _date_keys(league: str, first_month: str):
    """Claves de fecha para el scoreboard de ESPN: por mes, o por día en ligas muy grandes."""
    cfg = LEAGUES[league]
    y, m = int(first_month[:4]), int(first_month[4:])
    today = dt.date.today()
    if not cfg.get("daily"):
        while (y, m) <= (today.year, today.month):
            yield f"{y}{m:02d}"
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return
    day = dt.date(y, m, 1)
    while day <= today:
        if day.month in cfg.get("season_months", range(1, 13)):
            yield day.strftime("%Y%m%d")
        day += dt.timedelta(days=1)


def parse_event(league: str, e: dict) -> dict | None:
    """Un partido de ESPN → dict con local, visita, neutral y marcador (None si no sirve)."""
    c = e["competitions"][0]
    side = {t["homeAway"]: t for t in c["competitors"]}
    if set(side) != {"home", "away"} or not all(t["team"].get("abbreviation") for t in side.values()):
        return None   # p. ej. cruces de playoff con rival aún por definir
    teams = [(t["team"]["abbreviation"], t["team"].get("location", ""), t["team"].get("name", ""),
              t["team"].get("shortDisplayName", "")) for t in side.values()]
    out = {"gid": f"{league}-{e['id']}", "ts": _ts(e["date"]), "season": int(e["season"]["year"]),
           "stype": int(e.get("season", {}).get("type", 0)), "home": side["home"]["team"]["abbreviation"],
           "away": side["away"]["team"]["abbreviation"], "neutral": int(bool(c.get("neutralSite"))),
           "completed": bool(e["status"]["type"].get("completed")), "teams": teams,
           "state": e["status"]["type"].get("state")}
    try:
        out["home_score"], out["away_score"] = int(side["home"]["score"]), int(side["away"]["score"])
    except (KeyError, ValueError, TypeError):
        out["home_score"] = out["away_score"] = None
    return out


def download_games(con, league: str, first_month: str, workers: int = 6) -> int:
    """Baja todos los partidos terminados desde first_month (AAAAMM) hasta hoy."""
    url = f"{ESPN}/{LEAGUES[league]['espn']}/scoreboard"
    extra = LEAGUES[league].get("params", {})

    def fetch(key):
        try:
            return _get(url, {"dates": key, "limit": 1000, **extra}).get("events", [])
        except RuntimeError:
            return []

    n = 0
    with ThreadPoolExecutor(workers) as ex:
        for events in ex.map(fetch, list(_date_keys(league, first_month))):
            for e in events:
                g = parse_event(league, e)
                # solo temporada regular y postemporada, ya terminados
                if not g or not g["completed"] or g["stype"] not in (2, 3) or g["home_score"] is None:
                    continue
                for abbr, loc, name, short in g["teams"]:
                    con.execute("INSERT OR REPLACE INTO teams (league, abbr, location, name, short)"
                                " VALUES (?, ?, ?, ?, ?)", (league, abbr, loc, name, short))
                con.execute("INSERT OR REPLACE INTO games (gid, league, ts, season, stype, home, away,"
                            " home_score, away_score, neutral) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (g["gid"], league, g["ts"], g["season"], g["stype"], g["home"], g["away"],
                             g["home_score"], g["away_score"], g["neutral"]))
                n += 1
            con.commit()
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
    if LEAGUES.get(league, {}).get("daily"):
        return _college_lookup(con, league)
    return out


def _college_lookup(con, league: str) -> dict[str, str]:
    """
    Universidades: Kalshi usa el nombre corto ("Washington St.", "UNLV", "Duke").
    Se prueban nombre corto, ubicación, abreviatura y la variante State↔St; un nombre
    que apunta a dos equipos distintos se descarta en vez de adivinar.
    """
    cands: dict[str, set] = {}
    rows = con.execute("SELECT abbr, location, name, COALESCE(short, '') FROM teams WHERE league = ?",
                       (league,)).fetchall()
    for abbr, loc, name, short in rows:
        for c in {loc, short, abbr, f"{loc} {name}"}:
            k = _norm(c)
            if not k:
                continue
            for v in {k, re.sub(r"\bstate\b", "st", k), re.sub(r"\bst\b", "state", k)}:
                cands.setdefault(v, set()).add(abbr)
    return {k: next(iter(v)) for k, v in cands.items() if len(v) == 1}


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
