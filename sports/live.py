"""
Analizador activo: pronósticos en vivo, registrados para medirlos después.

En cada corrida, para los partidos que empiezan pronto:
  1. baja resultados recientes y recalcula el modelo de stats,
  2. lee el parte de lesiones de ESPN,
  3. toma el bid/ask actual de Kalshi,
  4. guarda todo en sports_log.db.

Sobre ese registro se pueden anotar probabilidades AJUSTADAS (por lesiones,
alineaciones o noticias) con `adjust`. Cuando los partidos terminan, `results`
compara tres pronosticadores con el mismo criterio: mercado, modelo base y
modelo ajustado. Nada de esto mueve dinero.
"""

import json
import time
import sqlite3
import datetime as dt

import numpy as np

from . import LEAGUES, data, evaluate, model

LOG_PATH = "sports_log.db"
OUT_STATUSES = ("out", "injured reserve", "doubtful", "suspension")

SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
    gid TEXT PRIMARY KEY, league TEXT, start_ts INTEGER, home TEXT, away TEXT,
    snap_ts INTEGER,            -- momento del último registro previo al partido
    p_model REAL,               -- prob. de victoria local, modelo base
    home_bid REAL, home_ask REAL,
    injuries TEXT,              -- JSON {equipo: [[jugador, posición, estado], ...]}
    p_adj REAL, adj_note TEXT, adj_ts INTEGER,   -- prob. ajustada y su motivo
    home_won INTEGER);
"""


def connect(path: str = LOG_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def upcoming_games(league: str, now: int, horizon_h: float) -> list[model.Game]:
    out = {}
    for delta in (-1, 0, 1):   # la fecha de ESPN es la de EE. UU., no UTC
        day = (dt.datetime.fromtimestamp(now, dt.timezone.utc) + dt.timedelta(days=delta)).strftime("%Y%m%d")
        d = data._get(f"{data.ESPN}/{LEAGUES[league]['espn']}/scoreboard", {"dates": day})
        for e in d.get("events", []):
            ts = data._ts(e["date"])
            if e["status"]["type"]["state"] != "pre" or not (now < ts <= now + horizon_h * 3600):
                continue
            side = {t["homeAway"]: t["team"]["abbreviation"] for t in e["competitions"][0]["competitors"]}
            if set(side) == {"home", "away"}:
                gid = f"{league}-{e['id']}"
                out[gid] = model.Game(gid, ts, int(e["season"]["year"]), side["home"], side["away"], 0, 0)
    return list(out.values())


def injuries(league: str) -> dict[str, list]:
    d = data._get(f"{data.ESPN}/{LEAGUES[league]['espn']}/injuries")
    out = {}
    for team in d.get("injuries", []):
        for i in team.get("injuries", []):
            a = i.get("athlete", {})
            if str(i.get("status", "")).lower() not in OUT_STATUSES:
                continue
            abbr = a.get("team", {}).get("abbreviation")
            out.setdefault(abbr, []).append(
                [a.get("displayName"), a.get("position", {}).get("abbreviation"), i.get("status")])
    return out


def kalshi_quotes(con, league: str) -> dict[str, list]:
    """{abreviatura ESPN: [(bid, ask, hora prevista de fin), ...]} de los mercados abiertos."""
    lookup = data.team_lookup(con, league)
    abbrs = {r[0] for r in con.execute("SELECT abbr FROM teams WHERE league = ?", (league,))}
    out, cursor = {}, ""
    while True:
        params = {"series_ticker": LEAGUES[league]["kalshi"], "status": "open", "limit": 500}
        if cursor:
            params["cursor"] = cursor
        d = data._get(f"{data.KALSHI}/markets", params)
        for m in d.get("markets", []):
            team = lookup.get(data._norm(m.get("yes_sub_title", "")))
            if not team:
                suffix = m["ticker"].rsplit("-", 1)[-1]
                team = suffix if suffix in abbrs else None
            try:
                bid, ask = float(m["yes_bid_dollars"]), float(m["yes_ask_dollars"])
                end = data._ts(m["expected_expiration_time"])
            except (KeyError, TypeError, ValueError):
                continue
            if team and 0 < bid < ask < 1:
                out.setdefault(team, []).append((bid, ask, end))
        cursor = d.get("cursor")
        if not cursor or not d.get("markets"):
            return out


def _quote_for(quotes: dict, g: model.Game) -> tuple[float, float] | None:
    """Bid/ask del LOCAL para este partido (usa el mercado de la visita si hace falta)."""
    for team, flip in ((g.home, False), (g.away, True)):
        for bid, ask, end in quotes.get(team, []):
            if g.ts < end <= g.ts + 8 * 3600:
                return (1 - ask, 1 - bid) if flip else (bid, ask)
    return None


def snapshot(stats_con, log_con, leagues: list[str], horizon_h: float = 3.0, now: int | None = None) -> list[dict]:
    now = now or int(time.time())
    month = dt.datetime.fromtimestamp(now - 3 * 86400, dt.timezone.utc).strftime("%Y%m")
    rows = []
    for lg in leagues:
        games = upcoming_games(lg, now, horizon_h)
        if not games:
            continue
        have = stats_con.execute("SELECT COUNT(*) FROM games WHERE league = ?", (lg,)).fetchone()[0]
        # Base vacía (contenedor nuevo): baja todo el historial; si no, solo los últimos días.
        data.download_games(stats_con, lg, month if have > 1000 else "202109")
        fitted = model.fit(stats_con, lg, cutoff_ts=now, upcoming=games)
        probs = {g.gid: float(p) for g, p in zip(fitted.games, fitted.p)}
        inj, quotes = injuries(lg), kalshi_quotes(stats_con, lg)
        for g in games:
            q = _quote_for(quotes, g)
            if q is None:
                continue
            gi = {t: inj.get(t, []) for t in (g.home, g.away)}
            log_con.execute(
                "INSERT INTO forecasts (gid, league, start_ts, home, away, snap_ts, p_model, home_bid,"
                " home_ask, injuries) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(gid) DO UPDATE SET start_ts = excluded.start_ts, snap_ts = excluded.snap_ts,"
                " p_model = excluded.p_model, home_bid = excluded.home_bid, home_ask = excluded.home_ask,"
                " injuries = excluded.injuries",
                (g.gid, lg, g.ts, g.home, g.away, now, probs[g.gid], q[0], q[1],
                 json.dumps(gi, ensure_ascii=False)))
            rows.append({"gid": g.gid, "league": lg, "start_ts": g.ts, "home": g.home, "away": g.away,
                         "p_model": probs[g.gid], "home_bid": q[0], "home_ask": q[1], "injuries": gi})
    log_con.commit()
    return rows


def adjust(log_con, gid: str, p: float, note: str) -> None:
    if not 0.01 <= p <= 0.99:
        raise ValueError("la probabilidad debe estar entre 0.01 y 0.99")
    row = log_con.execute("SELECT start_ts FROM forecasts WHERE gid = ?", (gid,)).fetchone()
    if not row:
        raise ValueError(f"no hay registro de {gid}")
    if time.time() >= row[0]:
        raise ValueError("el partido ya empezó: no se aceptan ajustes a posteriori")
    log_con.execute("UPDATE forecasts SET p_adj = ?, adj_note = ?, adj_ts = ? WHERE gid = ?",
                    (p, note, int(time.time()), gid))
    log_con.commit()


def settle(stats_con, log_con) -> int:
    pending = log_con.execute(
        "SELECT gid, league, start_ts FROM forecasts WHERE home_won IS NULL AND start_ts < ?",
        (int(time.time()) - 2 * 3600,)).fetchall()
    for lg in {r[1] for r in pending}:
        first = min(r[2] for r in pending if r[1] == lg)
        data.download_games(stats_con, lg, dt.datetime.fromtimestamp(first, dt.timezone.utc).strftime("%Y%m"))
    n = 0
    for gid, _, _ in pending:
        g = stats_con.execute("SELECT home_score, away_score FROM games WHERE gid = ?", (gid,)).fetchone()
        if g and g[0] != g[1]:
            log_con.execute("UPDATE forecasts SET home_won = ? WHERE gid = ?", (int(g[0] > g[1]), gid))
            n += 1
    log_con.commit()
    return n


def results(log_con, threshold: float = 0.05) -> None:
    rows = log_con.execute(
        "SELECT league, start_ts, home_won, p_model, p_adj, home_bid, home_ask FROM forecasts"
        " WHERE home_won IS NOT NULL").fetchall()
    pend = log_con.execute("SELECT COUNT(*) FROM forecasts WHERE home_won IS NULL").fetchone()[0]
    print(f"📊 REGISTRO EN VIVO — {len(rows)} partidos terminados, {pend} pendientes")
    if not rows:
        return
    for label, sel in [("todas", rows)] + [(lg, [r for r in rows if r[0] == lg]) for lg in LEAGUES]:
        if not sel:
            continue
        y = np.array([r[2] for r in sel], dtype=float)
        mid = np.array([(r[5] + r[6]) / 2 for r in sel])
        print(f"\n  {label.upper()} ({len(sel)} partidos)   log-loss mercado {model.log_loss(mid, y):.4f}")
        for name, col in (("modelo base", 3), ("modelo ajustado", 4)):
            sub = [r for r in sel if r[col] is not None]
            if not sub:
                print(f"    {name:16s} sin datos")
                continue
            ys = np.array([r[2] for r in sub], dtype=float)
            mids = np.array([(r[5] + r[6]) / 2 for r in sub])
            ev = [evaluate.Row(r[1], r[2], r[col], r[5], r[6]) for r in sub]
            roi, lo, hi = evaluate.ci(evaluate.bets(ev, threshold))
            nb = len(evaluate.bets(ev, threshold))
            print(f"    {name:16s} {len(sub):4d} partidos | log-loss {model.log_loss(np.array([r[col] for r in sub]), ys):.4f}"
                  f" (mercado en los mismos: {model.log_loss(mids, ys):.4f}) | {nb} apuestas, "
                  f"ROI {roi:+.1%} [{lo:+.1%}, {hi:+.1%}]")
    if len(rows) < 200:
        print("\n  ⚠️ Menos de 200 partidos: todavía es ruido, no una conclusión.")
