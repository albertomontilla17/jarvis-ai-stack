"""
Pieza 1 — Datos históricos.

Descarga mercados binarios YA RESUELTOS de Polymarket (Gamma API) y su
historial de precios horario (CLOB /prices-history) a una SQLite local.
No necesita API key ni llamadas a Claude.

Límites de la API verificados al escribir esto:
  - Gamma /markets devuelve máx. 100 por página → se pagina con offset.
  - /prices-history con fidelity=60 solo acepta ventanas de ≤ ~15 días
    → se pide en trozos de 14 días.
"""

import json
import random
import sqlite3
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import requests

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
HEADERS = {"User-Agent": "jarvis-backtest/1.0"}  # Cloudflare da 403 sin User-Agent
DB_PATH = "backtest_data.db"
PAGE = 100
CHUNK_S = 14 * 86400

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    id          TEXT PRIMARY KEY,   -- conditionId
    slug        TEXT,
    question    TEXT,
    event       TEXT,               -- slug del evento (para agrupar mercados correlacionados)
    category    TEXT,               -- derivada del feeType (sports, crypto, politics, ...)
    yes_token   TEXT,               -- token del outcome[0] ("YES" para el backtest)
    start_ts    INTEGER,
    end_ts      INTEGER,            -- fecha de cierre PROGRAMADA (conocida de antemano)
    closed_ts   INTEGER,            -- cierre REAL (solo se usa para liquidar, nunca para decidir)
    volume      REAL,
    payout_yes  REAL,               -- 1.0 si ganó outcome[0], 0.0 si perdió
    fee_rate    REAL,
    fee_exp     REAL,
    has_prices  INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS prices (
    market_id   TEXT,
    t           INTEGER,
    p           REAL,               -- precio de outcome[0]
    PRIMARY KEY (market_id, t)
) WITHOUT ROWID;
"""


def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    return con


def _ts(s) -> int | None:
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp())


def _get(url: str, params: dict, tries: int = 4):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}: {r.text[:120]}"
            if r.status_code == 400:
                break
        except requests.RequestException as e:
            last = str(e)
        import time
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url} falló: {last}")


def parse_market(m: dict) -> dict | None:
    """Convierte un market de Gamma en una fila. None si no sirve para backtest."""
    try:
        prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
        tokens = json.loads(m.get("clobTokenIds") or "[]")
    except (ValueError, TypeError):
        return None
    if len(prices) != 2 or len(tokens) != 2:
        return None
    # Solo resoluciones limpias 1/0 (se descartan 50/50 y mercados sin resolver).
    if sorted(prices) != [0.0, 1.0] or m.get("umaResolutionStatus") != "resolved":
        return None
    closed_ts, end_ts = _ts(m.get("closedTime")), _ts(m.get("endDate"))
    if not closed_ts or not end_ts or not m.get("conditionId"):
        return None
    fs = m.get("feeSchedule") or {}
    events = m.get("events") or [{}]
    category = (m.get("feeType") or "sin_fee").split("_fees")[0]
    return {
        "id": m["conditionId"], "slug": m.get("slug"), "question": m.get("question"),
        "event": events[0].get("slug") or m.get("slug"), "category": category,
        "yes_token": tokens[0],
        "start_ts": _ts(m.get("startDate")) or _ts(m.get("createdAt")),
        "end_ts": end_ts, "closed_ts": closed_ts,
        "volume": float(m.get("volumeNum") or 0), "payout_yes": prices[0],
        "fee_rate": float(fs.get("rate") or 0), "fee_exp": float(fs.get("exponent") or 1),
    }


def download_markets(con, date_from: dt.date, date_to: dt.date, min_volume: float) -> int:
    """Lista mercados resueltos con endDate en [date_from, date_to), día por día."""
    n = 0
    day = date_from
    while day < date_to:
        nxt = day + dt.timedelta(days=1)
        offset = 0
        while True:
            page = _get(f"{GAMMA_API}/markets", {
                "closed": "true", "limit": PAGE, "offset": offset,
                "volume_num_min": min_volume,
                "end_date_min": f"{day}T00:00:00Z", "end_date_max": f"{nxt}T00:00:00Z",
                "order": "endDate", "ascending": "true",
            })
            for m in page:
                row = parse_market(m)
                if row:
                    con.execute(
                        "INSERT OR IGNORE INTO markets (id, slug, question, event, category, yes_token,"
                        " start_ts, end_ts, closed_ts, volume, payout_yes, fee_rate, fee_exp)"
                        " VALUES (:id, :slug, :question, :event, :category, :yes_token, :start_ts,"
                        " :end_ts, :closed_ts, :volume, :payout_yes, :fee_rate, :fee_exp)", row)
                    n += 1
            if len(page) < PAGE:
                break
            offset += PAGE
        con.commit()
        print(f"  {day}: {n} mercados acumulados", flush=True)
        day = nxt
    return n


def fetch_history(token: str, start_ts: int, end_ts: int) -> list[tuple[int, float]]:
    out = {}
    s = start_ts
    while s < end_ts:
        e = min(s + CHUNK_S, end_ts)
        data = _get(f"{CLOB_API}/prices-history",
                    {"market": token, "startTs": s, "endTs": e, "fidelity": 60})
        for h in data.get("history", []):
            out[int(h["t"])] = float(h["p"])
        s = e
    return sorted(out.items())


def download_prices(con, sample: int, max_days: int, seed: int = 7, workers: int = 8,
                    categories: list[str] | None = None) -> int:
    """Baja historial horario para una muestra ALEATORIA de mercados sin precios aún."""
    where, args = "", []
    if categories:
        where = f" AND category IN ({','.join('?' * len(categories))})"
        args = list(categories)
    rows = con.execute(
        "SELECT id, yes_token, start_ts, closed_ts FROM markets WHERE has_prices = 0"
        + where + " ORDER BY id", args).fetchall()
    random.Random(seed).shuffle(rows)
    rows = rows[:sample]

    def job(row):
        mid, token, start_ts, closed_ts = row
        start = max(start_ts or 0, closed_ts - max_days * 86400)
        try:
            return mid, fetch_history(token, start, closed_ts + 3600)
        except RuntimeError as e:
            print(f"  ⚠️ {mid[:10]}…: {e}")
            return mid, None

    done = 0
    with ThreadPoolExecutor(workers) as ex:
        for mid, hist in ex.map(job, rows):
            if hist is None:
                continue
            con.executemany("INSERT OR IGNORE INTO prices VALUES (?, ?, ?)",
                            [(mid, t, p) for t, p in hist])
            # has_prices: 1 = con datos, 2 = intentado pero vacío (no reintentar)
            con.execute("UPDATE markets SET has_prices = ? WHERE id = ?", (1 if hist else 2, mid))
            done += 1
            if done % 100 == 0:
                con.commit()
                print(f"  {done}/{len(rows)} historiales", flush=True)
    con.commit()
    return done
