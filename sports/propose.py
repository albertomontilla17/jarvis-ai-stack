"""
Propuestas de orden para Kalshi a partir del paper trading.

Toma las operaciones abiertas de una estrategia (por defecto el modelo ajustado)
cuyo partido aún no empieza y escribe una propuesta por cada una en
proposals/kalshi/<id>.json. La propuesta siempre COMPRA "Sí" en el mercado
"gana <equipo>" del equipo elegido, a un precio tope; nunca vende.

La ejecuta jarvis_kalshi.py en la computadora local, que vuelve a validar todo.
"""

import os
import re
import json
import time
import sqlite3

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(HERE, "proposals", "kalshi")
EXPIRE_BEFORE_START_S = 5 * 60   # la propuesta caduca 5 min antes del inicio


def build(paper_db: str, log_db: str, strategy: str, amount_usd: float, now: int | None = None) -> list[dict]:
    now = now or int(time.time())
    if not (os.path.exists(paper_db) and os.path.exists(log_db)):
        return []
    log = sqlite3.connect(log_db)
    cols = {r[1] for r in log.execute("PRAGMA table_info(forecasts)")}
    if "home_ticker" not in cols:
        return []
    paper = sqlite3.connect(paper_db)
    out = []
    for ref, grupo, label, team, price, prob, event_ts in paper.execute(
            "SELECT ref, grupo, label, side, price, prob, event_ts FROM trades"
            " WHERE strategy = ? AND payout IS NULL AND event_ts > ?", (strategy, now)):
        row = log.execute("SELECT home, away, home_ticker, away_ticker FROM forecasts WHERE gid = ?",
                          (ref,)).fetchone()
        if not row:
            continue
        home, away, home_ticker, away_ticker = row
        ticker = home_ticker if team == home else away_ticker if team == away else None
        if not ticker:
            continue
        out.append({
            "id": f"{strategy}-{ref}", "venue": "kalshi", "ticker": ticker, "league": grupo,
            "question": f"{label}: gana {team}", "team": team, "action": "BUY_YES",
            "price": round(float(price), 2), "prob": round(float(prob), 4) if prob is not None else None,
            "amount_usd": float(amount_usd), "strategy": strategy,
            "start_ts": int(event_ts), "created_at": now,
            "expires_at": int(event_ts) - EXPIRE_BEFORE_START_S,
        })
    return out


def write(proposals: list[dict], out_dir: str = OUT_DIR) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for p in proposals:
        path = os.path.join(out_dir, re.sub(r"[^A-Za-z0-9_.-]", "_", p["id"]) + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(p, f, ensure_ascii=False, indent=2)
        paths.append(path)
    return paths
