"""
jarvis_execute.py — Ejecutor LOCAL de trades aprobados.
=======================================================
Corre SOLO en la computadora que tiene la wallet. La nube (o tú) le pasa una
propuesta ya aprobada; este script es la última barrera: valida límites duros
propios y solo entonces firma y envía la orden a Polymarket.

Por defecto es simulación. Solo con --live envía una orden real.

    python jarvis_execute.py --order proposals/abc123.json          # simulación
    python jarvis_execute.py --order proposals/abc123.json --live   # real
    python jarvis_execute.py --status                               # gasto de hoy

Configuración: archivo .env junto al script (ver .env.example).
Dependencias (solo para --live):  pip install py-clob-client python-dotenv
"""

import os
import sys
import json
import time
import sqlite3
import argparse
import datetime as dt

import requests

CLOB_API = "https://clob.polymarket.com"
HEADERS = {"User-Agent": "jarvis-executor/1.0"}
LEDGER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "executions.db")
REQUIRED = ("id", "token_id", "side", "category", "question", "price", "amount_usd", "expires_at")


class Rejected(Exception):
    """La orden no pasa una validación. Nunca se ejecuta."""


def load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    except ImportError:
        pass


def limits() -> dict:
    return {
        "max_per_trade": float(os.getenv("JARVIS_MAX_PER_TRADE", "5")),
        "max_per_day": float(os.getenv("JARVIS_MAX_PER_DAY", "15")),
        "categories": [c.strip() for c in os.getenv(
            "JARVIS_ALLOWED_CATEGORIES", "sports,finance_prices,economics").split(",") if c.strip()],
        "max_slippage": float(os.getenv("JARVIS_MAX_SLIPPAGE", "0.03")),
    }


def ledger(path: str = LEDGER) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS executions (
        order_id TEXT PRIMARY KEY, ts TEXT, day TEXT, question TEXT, side TEXT,
        amount_usd REAL, limit_price REAL, live INTEGER, response TEXT)""")
    return con


def spent_today(con, live: bool) -> float:
    day = dt.datetime.now(dt.timezone.utc).date().isoformat()
    return con.execute("SELECT COALESCE(SUM(amount_usd), 0) FROM executions WHERE day = ? AND live = ?",
                       (day, int(live))).fetchone()[0]


def validate(order: dict, lim: dict, con, live: bool, ask: float | None, now: float | None = None):
    """Lanza Rejected si algo no cuadra. Devuelve el precio límite a usar."""
    missing = [k for k in REQUIRED if order.get(k) in (None, "")]
    if missing:
        raise Rejected(f"faltan campos: {missing}")
    if order["side"] != "BUY":
        raise Rejected("solo se aceptan compras (BUY)")
    if order["category"] not in lim["categories"]:
        raise Rejected(f"categoría '{order['category']}' no permitida ({lim['categories']})")
    amount, price = float(order["amount_usd"]), float(order["price"])
    if not (0.02 <= price <= 0.97):
        raise Rejected(f"precio {price} fuera de [0.02, 0.97]")
    if amount < 1:
        raise Rejected("monto menor a $1")
    if amount > lim["max_per_trade"]:
        raise Rejected(f"${amount:.2f} supera el máximo por trade (${lim['max_per_trade']:.2f})")
    if (now or time.time()) > float(order["expires_at"]):
        raise Rejected("la propuesta expiró")
    if con.execute("SELECT 1 FROM executions WHERE order_id = ? AND live = ?",
                   (order["id"], int(live))).fetchone():
        raise Rejected("esta propuesta ya se ejecutó")
    spent = spent_today(con, live)
    if spent + amount > lim["max_per_day"]:
        raise Rejected(f"superaría el máximo diario: ${spent:.2f} + ${amount:.2f} > ${lim['max_per_day']:.2f}")
    if ask is None:
        raise Rejected("no se pudo leer el precio actual del mercado")
    if ask > price + lim["max_slippage"]:
        raise Rejected(f"el precio subió: ahora {ask:.3f}, propuesta {price:.3f} "
                       f"(tolerancia {lim['max_slippage']})")
    # 2 decimales: válido tanto en mercados con tick 0.01 como 0.001
    return round(min(price + lim["max_slippage"], 0.99), 2)


def current_ask(token_id: str) -> float | None:
    """Mejor precio al que se puede COMPRAR ahora mismo (ask más bajo del libro)."""
    try:
        r = requests.get(f"{CLOB_API}/book", params={"token_id": token_id},
                         headers=HEADERS, timeout=15)
        asks = [float(x["price"]) for x in r.json().get("asks", [])] if r.ok else []
        return min(asks) if asks else None
    except (requests.RequestException, KeyError, ValueError, TypeError):
        return None


def send_order(order: dict, limit_price: float) -> dict:
    """Firma y envía una orden de compra FOK (se llena completa al límite o se cancela)."""
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import MarketOrderArgs, OrderType

    key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    funder = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
    if not key or "REEMPLAZA" in key or not funder or "REEMPLAZA" in funder:
        raise Rejected("faltan POLYMARKET_PRIVATE_KEY / POLYMARKET_FUNDER_ADDRESS en .env")

    client = ClobClient(CLOB_API, chain_id=137, key=key, funder=funder,
                        signature_type=int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0")))
    client.set_api_creds(client.create_or_derive_api_creds())
    signed = client.create_market_order(MarketOrderArgs(
        token_id=order["token_id"], amount=float(order["amount_usd"]),
        side="BUY", price=limit_price, order_type=OrderType.FOK))
    return client.post_order(signed, OrderType.FOK)


def execute(order: dict, live: bool, con=None, ask: float | None = None) -> dict:
    con = con or ledger()
    lim = limits()
    ask = ask if ask is not None else current_ask(order.get("token_id", ""))
    limit_price = validate(order, lim, con, live, ask)

    response = send_order(order, limit_price) if live else {"simulado": True}
    if live and not (isinstance(response, dict) and response.get("success")):
        raise Rejected(f"Polymarket no aceptó la orden: {response}")
    now = dt.datetime.now(dt.timezone.utc)
    con.execute("INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (order["id"], now.isoformat(), now.date().isoformat(), order["question"],
                 order.get("outcome", order["side"]), float(order["amount_usd"]), limit_price,
                 int(live), json.dumps(response, default=str)))
    con.commit()
    return {"ok": True, "live": live, "limit_price": limit_price, "ask": ask, "response": response}


def main():
    ap = argparse.ArgumentParser(description="Ejecutor local de trades aprobados")
    ap.add_argument("--order", help="ruta a un JSON de propuesta")
    ap.add_argument("--order-json", help="la propuesta como texto JSON")
    ap.add_argument("--live", action="store_true", help="enviar orden REAL (sin esto, simula)")
    ap.add_argument("--status", action="store_true", help="ver gasto de hoy y límites")
    a = ap.parse_args()
    load_env()

    if a.status:
        con, lim = ledger(), limits()
        print(f"Límites: ${lim['max_per_trade']}/trade, ${lim['max_per_day']}/día, {lim['categories']}")
        print(f"Gastado hoy (real): ${spent_today(con, True):.2f} | simulado: ${spent_today(con, False):.2f}")
        return
    if not (a.order or a.order_json):
        ap.error("indica --order, --order-json o --status")
    order = json.loads(a.order_json) if a.order_json else json.load(open(a.order, encoding="utf-8"))

    try:
        res = execute(order, a.live)
    except Rejected as e:
        print(f"⛔ RECHAZADA: {e}")
        sys.exit(2)
    tag = "✅ ORDEN REAL ENVIADA" if a.live else "🧪 SIMULACIÓN OK (usa --live para ejecutar de verdad)"
    print(f"{tag}\n   {order['question']}\n   Comprar {order.get('outcome', '')} — ${float(order['amount_usd']):.2f} "
          f"a ≤ {res['limit_price']} (ahora {res['ask']})\n   {res['response']}")


if __name__ == "__main__":
    main()
