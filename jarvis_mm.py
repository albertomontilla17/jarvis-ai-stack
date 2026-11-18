"""
jarvis_mm.py — Bot de creación de mercado (market making) para Kalshi.
======================================================================
Deja una orden de compra y otra de venta de "Sí" en cada mercado elegido, al
mejor precio del libro, para cobrar el spread y los incentivos de liquidez.
Vigila el inventario y se retira cuando el precio salta (posible noticia).

    python jarvis_mm.py --markets TICKER1 TICKER2 --dry-run       # solo muestra qué cotizaría
    python jarvis_mm.py --markets TICKER1 --size 5 --minutes 30   # opera (demo por defecto)
    python jarvis_mm.py --cancel-all                              # cancela todas las órdenes abiertas
    python jarvis_mm.py --report                                  # llenados, posición y PnL

Usa la misma clave y entorno que jarvis_kalshi.py (.env). En KALSHI_ENV=prod se
niega a operar salvo con --allow-prod.

Reglas de cotización (por mercado, en cada vuelta):
  - Se calcula el mejor bid/ask SIN contar nuestras propias órdenes.
  - Spread de 1–2 centavos: nos unimos al mejor precio. Spread mayor: mejoramos
    1 centavo en cada lado, sin cruzar nunca (post_only).
  - Inventario: no se compra más allá de +max_pos ni se vende más allá de −max_pos.
  - Si el punto medio se mueve ≥ jump desde la vuelta anterior, se cancelan las
    órdenes de ese mercado y se pausa `cooldown` segundos.
  - Fuera de la banda de precio [band_lo, band_hi] no se cotiza.
"""

import os
import sys
import json
import time
import uuid
import sqlite3
import argparse
import datetime as dt
from dataclasses import dataclass

import requests

import jarvis_kalshi as kx
from jarvis_execute import Rejected, load_env

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "mm.db")
TICK = 0.01


@dataclass
class Params:
    size: int = 5              # contratos por orden
    max_pos: int = 20          # inventario máximo (en contratos de Sí, positivo o negativo)
    jump: float = 0.05         # salto del punto medio que dispara la retirada
    cooldown: int = 300        # segundos de pausa tras un salto
    band_lo: float = 0.05
    band_hi: float = 0.95


# ── Lógica pura (testeable) ───────────────────────────────────────────────────
def _levels(levels) -> dict[float, float]:
    out = {}
    for p, q in levels or []:
        p, q = round(float(p), 4), float(q)
        if q > 0:
            out[p] = out.get(p, 0) + q
    return out


def best_prices(book: dict, own: list[dict]) -> tuple[float | None, float | None]:
    """Mejor bid y ask de Sí excluyendo nuestras órdenes. own: [{side, price, remaining}]."""
    yes, no = _levels(book.get("yes_dollars")), _levels(book.get("no_dollars"))
    for o in own:
        if o["side"] == "bid":            # nuestra compra de Sí está en el libro de Sí
            yes[o["price"]] = yes.get(o["price"], 0) - o["remaining"]
        else:                             # nuestra venta de Sí aparece como compra de No a 1 − p
            k = round(1 - o["price"], 4)
            no[k] = no.get(k, 0) - o["remaining"]
    yb = [p for p, q in yes.items() if q > 1e-9]
    nb = [p for p, q in no.items() if q > 1e-9]
    return (max(yb) if yb else None), (round(1 - max(nb), 4) if nb else None)


def desired_quotes(bid: float | None, ask: float | None, position: float, prm: Params) -> dict:
    """{'bid': (precio, contratos) | None, 'ask': (precio, contratos) | None}."""
    none = {"bid": None, "ask": None}
    if bid is None or ask is None or ask - bid < TICK - 1e-9:
        return none
    mid = (bid + ask) / 2
    if not (prm.band_lo <= mid <= prm.band_hi):
        return none
    spread = round(ask - bid, 4)
    if spread > 2 * TICK + 1e-9:          # mejorar un centavo en cada lado
        qb, qa = round(bid + TICK, 2), round(ask - TICK, 2)
        if qa - qb < TICK - 1e-9:         # no dejar el libro cruzado
            qb, qa = round(bid, 2), round(ask, 2)
    else:
        qb, qa = round(bid, 2), round(ask, 2)
    buy = int(max(0, min(prm.size, prm.max_pos - position)))
    sell = int(max(0, min(prm.size, prm.max_pos + position)))
    return {"bid": (qb, buy) if buy > 0 else None, "ask": (qa, sell) if sell > 0 else None}


def plan(own: list[dict], desired: dict) -> tuple[list[str], list[tuple]]:
    """Qué cancelar (order_ids) y qué colocar ((lado, precio, contratos)) para llegar a `desired`."""
    cancels, places = [], []
    for side in ("bid", "ask"):
        mine = [o for o in own if o["side"] == side]
        want = desired.get(side)
        keep = None
        if want:
            keep = next((o for o in mine if abs(o["price"] - want[0]) < 1e-9), None)
        cancels += [o["order_id"] for o in mine if o is not keep]
        if want and keep is None:
            places.append((side, want[0], want[1]))
    return cancels, places


# ── Conexión con Kalshi ───────────────────────────────────────────────────────
class Client:
    def __init__(self, cfg: dict):
        self.cfg, self.base = cfg, cfg["base"]
        self.key = kx.load_key(cfg["key_path"])
        self.s = requests.Session()

    def _req(self, method: str, path: str, params=None, body=None, auth=True):
        for attempt in range(5):
            headers = kx.auth_headers(self.cfg, self.key, method, path) if auth else {}
            r = self.s.request(method, self.base + path, params=params, headers=headers,
                               data=json.dumps(body) if body is not None else None, timeout=20)
            if r.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code >= 300:
                raise Rejected(f"{method} {path} → HTTP {r.status_code}: {r.text[:200]}")
            return r.json() if r.text else {}
        raise Rejected(f"{method} {path}: demasiadas peticiones (429)")

    def book(self, ticker: str) -> dict:
        return self._req("GET", f"/markets/{ticker}/orderbook", {"depth": 0}, auth=False).get("orderbook_fp") or {}

    def resting(self) -> list[dict]:
        out, cursor = [], ""
        while True:
            p = {"status": "resting", "limit": 1000}
            if cursor:
                p["cursor"] = cursor
            d = self._req("GET", "/portfolio/orders", p)
            for o in d.get("orders", []):
                out.append({"order_id": o["order_id"], "ticker": o["ticker"], "side": o["book_side"],
                            "price": round(float(o["yes_price_dollars"]), 4),
                            "remaining": float(o["remaining_count_fp"])})
            cursor = d.get("cursor")
            if not cursor or not d.get("orders"):
                return out

    def positions(self) -> dict[str, dict]:
        d = self._req("GET", "/portfolio/positions", {"limit": 1000, "count_filter": "position,total_traded"})
        return {p["ticker"]: p for p in d.get("market_positions", [])}

    def fills(self, min_ts: int) -> list[dict]:
        out, cursor = [], ""
        while True:
            p = {"min_ts": min_ts, "limit": 1000}
            if cursor:
                p["cursor"] = cursor
            d = self._req("GET", "/portfolio/fills", p)
            out += d.get("fills", [])
            cursor = d.get("cursor")
            if not cursor or not d.get("fills"):
                return out

    def place(self, ticker: str, side: str, price: float, count: int) -> dict:
        return self._req("POST", kx.ORDER_PATH, body={
            "ticker": ticker, "client_order_id": str(uuid.uuid4()), "side": side,
            "count": str(count), "price": f"{price:.2f}", "time_in_force": "good_till_canceled",
            "post_only": True,                          # nunca cruzar: siempre aportar liquidez
            "self_trade_prevention_type": "maker", "cancel_order_on_pause": True})

    def cancel(self, order_id: str, ticker: str) -> None:
        self._req("DELETE", f"{kx.ORDER_PATH}/{order_id}", {"market_ticker": ticker})


# ── Registro ──────────────────────────────────────────────────────────────────
def db(path: str = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS events (ts INTEGER, env TEXT, ticker TEXT, action TEXT, side TEXT,
                                       price REAL, count REAL, info TEXT);
    CREATE TABLE IF NOT EXISTS fills (fill_id TEXT PRIMARY KEY, env TEXT, ts INTEGER, ticker TEXT,
                                      side TEXT, price REAL, count REAL, is_taker INTEGER, fee REAL);
    """)
    return con


def log(con, env, ticker, action, side=None, price=None, count=None, info=""):
    con.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (int(time.time()), env, ticker, action, side, price, count, info))
    con.commit()
    print(f"  {dt.datetime.now():%H:%M:%S} {ticker} {action} {side or ''} {'' if price is None else f'{price:.2f}'}"
          f" {'' if count is None else int(count)} {info}", flush=True)


def sync_fills(con, client: Client, env: str, since: int) -> int:
    n = 0
    for f in client.fills(since):
        cur = con.execute("INSERT OR IGNORE INTO fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (f["fill_id"], env, int(f.get("ts") or 0), f["ticker"], f["book_side"],
                           float(f["yes_price_dollars"]), float(f["count_fp"]), int(f["is_taker"]),
                           float(f.get("fee_cost") or 0)))
        n += cur.rowcount
    con.commit()
    return n


# ── Bucle principal ───────────────────────────────────────────────────────────
def run(client: Client, markets: list[str], prm: Params, minutes: float, interval: float,
        dry: bool, con) -> None:
    env, start = client.cfg["env"], int(time.time())
    last_mid, paused_until = {}, {}
    end = time.time() + minutes * 60
    print(f"🤖 Market making en {env} — {len(markets)} mercado(s), {prm.size} contratos/orden, "
          f"inventario máx ±{prm.max_pos}, {'SIMULACIÓN' if dry else 'ÓRDENES REALES'}", flush=True)
    try:
        while time.time() < end:
            own_all = [] if dry else client.resting()
            pos = {} if dry else client.positions()
            for t in markets:
                own = [o for o in own_all if o["ticker"] == t]
                position = float((pos.get(t) or {}).get("position_fp") or 0)
                bid, ask = best_prices(client.book(t), own)
                mid = None if bid is None or ask is None else (bid + ask) / 2
                if mid is not None and t in last_mid and abs(mid - last_mid[t]) >= prm.jump - 1e-9:
                    paused_until[t] = time.time() + prm.cooldown
                    log(con, env, t, "PAUSA", info=f"salto de precio {last_mid[t]:.2f}→{mid:.2f}")
                if mid is not None:
                    last_mid[t] = mid
                desired = ({"bid": None, "ask": None} if time.time() < paused_until.get(t, 0)
                           else desired_quotes(bid, ask, position, prm))
                cancels, places = plan(own, desired)
                if dry:
                    log(con, env, t, "SIM", info=f"libro {bid}/{ask} → compra {desired['bid']} venta {desired['ask']}")
                    continue
                for oid in cancels:
                    client.cancel(oid, t)
                    log(con, env, t, "CANCELA", info=oid[:8])
                for side, price, count in places:
                    try:
                        client.place(t, side, price, count)
                        log(con, env, t, "COLOCA", side, price, count, f"pos {position:+.0f}")
                    except Rejected as e:
                        log(con, env, t, "RECHAZO", side, price, count, str(e)[:120])
            if not dry:
                new = sync_fills(con, client, env, start)
                if new:
                    print(f"  💰 {new} llenado(s) nuevo(s)", flush=True)
            time.sleep(interval)
    finally:
        if not dry:
            cancel_all(client, con, markets)


def cancel_all(client: Client, con, markets: list[str] | None = None) -> int:
    n = 0
    for o in client.resting():
        if markets is None or o["ticker"] in markets:
            client.cancel(o["order_id"], o["ticker"])
            n += 1
    log(con, client.cfg["env"], "*", "CANCELA_TODO", info=f"{n} órdenes")
    return n


def report(client: Client, con) -> None:
    env = client.cfg["env"]
    sync_fills(con, client, env, 0)
    rows = con.execute("SELECT ticker, side, price, count, is_taker, fee FROM fills WHERE env = ?", (env,)).fetchall()
    pos = client.positions()
    print(f"📊 Market making ({env}) — {len(rows)} llenados registrados")
    by = {}
    for t, side, price, count, taker, fee in rows:
        b = by.setdefault(t, {"bought": 0.0, "sold": 0.0, "cash": 0.0, "fees": 0.0, "maker": 0, "n": 0})
        if side == "bid":
            b["bought"] += count; b["cash"] -= price * count
        else:
            b["sold"] += count; b["cash"] += price * count
        b["fees"] += fee; b["maker"] += int(not taker); b["n"] += 1
    total = 0.0
    for t, b in by.items():
        p = float((pos.get(t) or {}).get("position_fp") or 0)
        try:
            bid, ask = best_prices(client.book(t), [])
            mark = (bid + ask) / 2 if bid is not None and ask is not None else 0.5
        except Rejected:
            mark = 0.5
        pnl = b["cash"] + p * mark - b["fees"]
        total += pnl
        print(f"  {t}: compró {b['bought']:.0f}, vendió {b['sold']:.0f}, posición {p:+.0f} a {mark:.2f}, "
              f"comisiones ${b['fees']:.2f}, PnL ${pnl:+.2f} ({b['maker']}/{b['n']} como maker)")
    print(f"  TOTAL PnL marcado a mercado: ${total:+.2f} (sin contar incentivos, que Kalshi paga aparte)")


def main():
    ap = argparse.ArgumentParser(description="Bot de market making para Kalshi")
    ap.add_argument("--markets", nargs="*", default=[])
    ap.add_argument("--size", type=int, default=5)
    ap.add_argument("--max-pos", type=int, default=20)
    ap.add_argument("--jump", type=float, default=0.05)
    ap.add_argument("--cooldown", type=int, default=300)
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--interval", type=float, default=15)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-prod", action="store_true")
    ap.add_argument("--cancel-all", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    load_env()
    try:
        cfg = kx.config()
        if cfg["env"] == "prod" and not a.allow_prod and not (a.report or a.dry_run):
            raise Rejected("KALSHI_ENV=prod: para operar con dinero real añade --allow-prod")
        client, con = Client(cfg), db()
        if a.cancel_all:
            print(f"✅ {cancel_all(client, con)} órdenes canceladas")
        elif a.report:
            report(client, con)
        else:
            if not a.markets:
                ap.error("indica --markets")
            run(client, a.markets, Params(a.size, a.max_pos, a.jump, a.cooldown), a.minutes,
                a.interval, a.dry_run, con)
    except Rejected as e:
        print(f"⛔ {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
