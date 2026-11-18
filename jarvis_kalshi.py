"""
jarvis_kalshi.py — Ejecutor LOCAL de órdenes aprobadas en Kalshi.
==================================================================
Corre SOLO en la computadora que tiene la clave de API de Kalshi. Recibe una
propuesta (de `python -m sports propose`), vuelve a validarla contra el libro de
órdenes REAL y contra tus límites, y solo con --live la firma y la envía.

    python jarvis_kalshi.py --check                        # verifica la clave: muestra tu saldo (solo lectura)
    python jarvis_kalshi.py --order proposals/kalshi/X.json          # simulación: muestra la orden exacta
    python jarvis_kalshi.py --order proposals/kalshi/X.json --live   # orden REAL
    python jarvis_kalshi.py --status                       # gasto de hoy y límites

Entorno (.env): KALSHI_ENV=demo usa el entorno de prueba de Kalshi (dinero
ficticio); KALSHI_ENV=prod usa tu cuenta real. Empieza siempre por demo.

Detalles de la API (verificados en el SDK oficial kalshi_python_sync 3.36):
  - Firma: RSA-PSS (MGF1-SHA256, sal = longitud del digest) sobre
    timestamp_ms + MÉTODO + ruta_sin_query, en base64.
  - Cabeceras: KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE.
  - Órdenes: POST /portfolio/events/orders. side "bid" = comprar Sí; precio en
    dólares como texto; count en contratos como texto.
"""

import os
import sys
import json
import math
import time
import uuid
import base64
import argparse
import datetime as dt

import requests

from jarvis_execute import Rejected, ledger, load_env, spent_today

BASES = {
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
}
ORDER_PATH = "/portfolio/events/orders"
BALANCE_PATH = "/portfolio/balance"
TAKER_FEE = 0.07     # comisión: 0.07 × contratos × p × (1 − p), redondeada al centavo hacia arriba
REQUIRED = ("id", "venue", "ticker", "league", "question", "action", "price", "amount_usd",
            "start_ts", "expires_at")


# ── Configuración ─────────────────────────────────────────────────────────────
def config() -> dict:
    env = os.getenv("KALSHI_ENV", "demo").strip().lower()
    if env not in BASES:
        raise Rejected(f"KALSHI_ENV debe ser 'demo' o 'prod', no '{env}'")
    return {
        "env": env, "base": BASES[env],
        "key_id": os.getenv("KALSHI_API_KEY_ID", ""),
        "key_path": os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
        "max_per_trade": float(os.getenv("JARVIS_MAX_PER_TRADE", "5")),
        "max_per_day": float(os.getenv("JARVIS_MAX_PER_DAY", "15")),
        "leagues": [x.strip() for x in os.getenv("JARVIS_KALSHI_LEAGUES", "nba,nhl,nfl").split(",") if x.strip()],
        "max_slippage": float(os.getenv("JARVIS_MAX_SLIPPAGE", "0.03")),
        "min_edge": float(os.getenv("JARVIS_MIN_EDGE", "0.02")),
    }


# ── Firma ─────────────────────────────────────────────────────────────────────
def load_key(path: str):
    from cryptography.hazmat.primitives import serialization
    if not path or "REEMPLAZA" in path or not os.path.exists(path):
        raise Rejected("falta KALSHI_PRIVATE_KEY_PATH en .env (ruta al archivo .pem de tu clave)")
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def sign(key, method: str, path: str, ts_ms: int) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    msg = f"{ts_ms}{method.upper()}{path.split('?')[0]}".encode()
    if isinstance(key, Ed25519PrivateKey):
        sig = key.sign(msg)
    else:
        sig = key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                        salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return base64.b64encode(sig).decode()


def auth_headers(cfg: dict, key, method: str, path: str) -> dict:
    if not cfg["key_id"] or "REEMPLAZA" in cfg["key_id"]:
        raise Rejected("falta KALSHI_API_KEY_ID en .env")
    ts = int(time.time() * 1000)
    full = "/trade-api/v2" + path
    return {"KALSHI-ACCESS-KEY": cfg["key_id"], "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "KALSHI-ACCESS-SIGNATURE": sign(key, method, full, ts), "Content-Type": "application/json"}


# ── Mercado ───────────────────────────────────────────────────────────────────
def best_yes_ask(base: str, ticker: str) -> float | None:
    """Precio más bajo al que se puede COMPRAR Sí ahora: 1 − mejor oferta de compra de No."""
    try:
        for attempt in range(4):   # Kalshi responde 429 si se le pregunta demasiado seguido
            r = requests.get(f"{base}/markets/{ticker}/orderbook", params={"depth": 10}, timeout=15)
            if r.status_code != 429:
                break
            time.sleep(1.5 * (attempt + 1))
        if not r.ok:
            return None
        book = r.json().get("orderbook_fp") or {}
        no_bids = [float(p) for p, qty in (book.get("no_dollars") or []) if float(qty) > 0]
        return round(1 - max(no_bids), 4) if no_bids else None
    except (requests.RequestException, ValueError, TypeError):
        return None


def fee(contracts: int, price: float) -> float:
    return math.ceil(TAKER_FEE * contracts * price * (1 - price) * 100 - 1e-9) / 100


def contracts_for(amount: float, price: float) -> int:
    """Máximo de contratos cuyo costo total (precio + comisión) no supera el monto."""
    n = int(amount // price)
    while n > 0 and n * price + fee(n, price) > amount + 1e-9:
        n -= 1
    return n


# ── Validación ────────────────────────────────────────────────────────────────
def validate(order: dict, cfg: dict, con, live: bool, ask: float | None, now: float | None = None) -> dict:
    """Lanza Rejected si algo no cuadra. Devuelve el plan de la orden."""
    now = now or time.time()
    missing = [k for k in REQUIRED if order.get(k) in (None, "")]
    if missing:
        raise Rejected(f"faltan campos: {missing}")
    if order["venue"] != "kalshi" or order["action"] != "BUY_YES":
        raise Rejected("solo se aceptan compras de Sí en Kalshi")
    if order["league"] not in cfg["leagues"]:
        raise Rejected(f"liga '{order['league']}' no permitida ({cfg['leagues']})")
    amount, price = float(order["amount_usd"]), float(order["price"])
    if not (0.03 <= price <= 0.97):
        raise Rejected(f"precio {price} fuera de [0.03, 0.97]")
    if amount < 1:
        raise Rejected("monto menor a $1")
    if amount > cfg["max_per_trade"]:
        raise Rejected(f"${amount:.2f} supera el máximo por operación (${cfg['max_per_trade']:.2f})")
    if now >= float(order["expires_at"]) or now >= float(order["start_ts"]):
        raise Rejected("la propuesta caducó o el partido ya empezó")
    if con.execute("SELECT 1 FROM executions WHERE order_id = ? AND live = ?",
                   (order["id"], int(live))).fetchone():
        raise Rejected("esta propuesta ya se ejecutó")
    spent = spent_today(con, live)
    if spent + amount > cfg["max_per_day"]:
        raise Rejected(f"superaría el máximo diario: ${spent:.2f} + ${amount:.2f} > ${cfg['max_per_day']:.2f}")
    if ask is None:
        raise Rejected("no se pudo leer el libro de órdenes de Kalshi")
    if ask > price + cfg["max_slippage"]:
        raise Rejected(f"el precio subió: ahora {ask:.2f}, propuesta {price:.2f} (tolerancia {cfg['max_slippage']})")
    limit = round(min(price + cfg["max_slippage"], 0.97), 2)
    prob = order.get("prob")
    if prob is not None:
        edge = float(prob) - (ask + TAKER_FEE * ask * (1 - ask))
        if edge < cfg["min_edge"]:
            raise Rejected(f"ya no hay ventaja: modelo {float(prob):.0%} vs costo {ask:.2f} + comisión "
                           f"(ventaja {edge:+.1%} < {cfg['min_edge']:.0%})")
    n = contracts_for(amount, limit)
    if n < 1:
        raise Rejected(f"${amount:.2f} no alcanza para un contrato a {limit:.2f}")
    return {"limit": limit, "contracts": n, "max_cost": round(n * limit + fee(n, limit), 2)}


def order_body(order: dict, plan: dict) -> dict:
    return {
        "ticker": order["ticker"],
        # Mismo id para la misma propuesta: Kalshi rechaza duplicados si se reintenta.
        "client_order_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "jarvis/" + order["id"])),
        "side": "bid",                       # comprar Sí
        "count": str(plan["contracts"]),
        "price": f"{plan['limit']:.2f}",     # precio tope; se llena al mejor precio disponible ≤ tope
        "time_in_force": "fill_or_kill",     # todo o nada, sin dejar órdenes colgadas
        "self_trade_prevention_type": "taker_at_cross",
    }


# ── Ejecución ─────────────────────────────────────────────────────────────────
def execute(order: dict, live: bool, cfg: dict | None = None, con=None, ask: float | None = None,
            post=requests.post) -> dict:
    cfg = cfg or config()
    con = con or ledger()
    ask = ask if ask is not None else best_yes_ask(cfg["base"], order.get("ticker", ""))
    plan = validate(order, cfg, con, live, ask)
    body = order_body(order, plan)

    if live:
        key = load_key(cfg["key_path"])
        r = post(cfg["base"] + ORDER_PATH, data=json.dumps(body),
                 headers=auth_headers(cfg, key, "POST", ORDER_PATH), timeout=20)
        try:
            response = r.json()
        except ValueError:
            response = {"http": r.status_code, "text": r.text[:300]}
        filled = float((response or {}).get("fill_count") or 0)
        if r.status_code >= 300 or filled <= 0:
            raise Rejected(f"Kalshi no llenó la orden (HTTP {r.status_code}): {response}")
        spent = filled * float(response.get("average_fill_price") or plan["limit"]) + \
            filled * float(response.get("average_fee_paid") or 0)
    else:
        response, spent = {"simulado": True, "orden": body}, plan["max_cost"]

    now = dt.datetime.now(dt.timezone.utc)
    con.execute("INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (order["id"], now.isoformat(), now.date().isoformat(), order["question"],
                 f"kalshi:{order['ticker']}", round(spent, 2), plan["limit"], int(live),
                 json.dumps(response, default=str)))
    con.commit()
    return {"live": live, "ask": ask, "plan": plan, "body": body, "response": response, "spent": spent}


def check_key(cfg: dict) -> dict:
    """Llamada de solo lectura para confirmar que la clave funciona."""
    key = load_key(cfg["key_path"])
    r = requests.get(cfg["base"] + BALANCE_PATH, headers=auth_headers(cfg, key, "GET", BALANCE_PATH), timeout=15)
    if r.status_code != 200:
        raise Rejected(f"Kalshi rechazó la clave (HTTP {r.status_code}): {r.text[:200]}")
    return r.json()


def main():
    ap = argparse.ArgumentParser(description="Ejecutor local de órdenes aprobadas en Kalshi")
    ap.add_argument("--order", help="ruta a un JSON de propuesta")
    ap.add_argument("--live", action="store_true", help="enviar orden REAL (sin esto, simula)")
    ap.add_argument("--check", action="store_true", help="verificar la clave y ver el saldo")
    ap.add_argument("--status", action="store_true", help="gasto de hoy y límites")
    a = ap.parse_args()
    load_env()
    try:
        cfg = config()
        if a.check:
            b = check_key(cfg)
            print(f"✅ Clave válida en Kalshi ({cfg['env']}). Saldo disponible: ${float(b.get('balance_dollars') or 0):,.2f}")
            return
        if a.status:
            con = ledger()
            print(f"Entorno: {cfg['env']} | límites: ${cfg['max_per_trade']}/orden, ${cfg['max_per_day']}/día, "
                  f"ligas {cfg['leagues']}, tolerancia {cfg['max_slippage']}, ventaja mínima {cfg['min_edge']:.0%}")
            print(f"Gastado hoy (real): ${spent_today(con, True):.2f} | simulado: ${spent_today(con, False):.2f}")
            return
        if not a.order:
            ap.error("indica --order, --check o --status")
        order = json.load(open(a.order, encoding="utf-8"))
        res = execute(order, a.live, cfg)
    except Rejected as e:
        print(f"⛔ RECHAZADA: {e}")
        sys.exit(2)
    p = res["plan"]
    tag = f"✅ ORDEN REAL ENVIADA ({cfg['env']})" if a.live else "🧪 SIMULACIÓN OK — no se envió nada (usa --live)"
    print(f"{tag}\n   {order['question']}\n   {p['contracts']} contratos de Sí en {order['ticker']} a ≤ {p['limit']:.2f}"
          f" (ahora {res['ask']:.2f}) — costo máximo ${p['max_cost']:.2f}")
    print("   " + json.dumps(res["response"] if a.live else res["body"], ensure_ascii=False))


if __name__ == "__main__":
    main()
