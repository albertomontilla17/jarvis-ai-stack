"""
jarvis_market_selector.py
=========================
Selector de mercados en dos etapas para el paper trading de Jarvis.

Etapa 1 (gratis, sin Claude): trae el universo de mercados activos de la
Gamma API y aplica filtros duros en código — liquidez/volumen mínimo,
ventana de resolución (2-30 días, para que los trades cierren pronto y
generen datos de aprendizaje), y precio no decidido (0.05-0.95).

Etapa 2 (1 llamada a Claude por día): Claude recibe los candidatos que
pasaron el filtro y elige una watchlist con justificación explícita por
mercado (por qué seguirlo, qué tipo de edge espera). La selección se
guarda en la tabla `watchlist` de paper_trades.db, así jarvis_learn
puede evaluar después no solo los trades sino las SELECCIONES.

La selección se cachea por fecha: la primera corrida del día llama a
Claude, el resto del día reusa la misma watchlist.

Uso:
    python jarvis_market_selector.py             # watchlist de hoy (crea si no existe)
    python jarvis_market_selector.py --refresh   # fuerza nueva selección
    python jarvis_market_selector.py --dry-run   # solo muestra candidatos, sin Claude
"""

import os
import json
import sqlite3
import argparse
import datetime
import requests
from anthropic import Anthropic

DB_PATH        = "paper_trades.db"
GAMMA_API      = "https://gamma-api.polymarket.com"
HEADERS        = {"User-Agent": "jarvis-paper-trading/1.0"}

# ── Filtros duros (Etapa 1) ───────────────────────────────────────────────────
MIN_VOLUME_24H   = 10_000    # USD — que el precio signifique algo
MIN_DAYS_TO_END  = 2         # muy cerca = ya casi decidido, poco que aprender
MAX_DAYS_TO_END  = 30        # muy lejos = el trade no cierra nunca, cero feedback
PRICE_LO, PRICE_HI = 0.05, 0.95
MAX_CANDIDATES   = 60        # tope de candidatos que ve Claude (controla tokens)
WATCHLIST_SIZE   = 8         # cuántos mercados elige Claude


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _today():
    return datetime.date.today().isoformat()


def init_watchlist_table(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT,     -- YYYY-MM-DD de la selección
            market_id   TEXT,     -- conditionId
            slug        TEXT,
            question    TEXT,
            category    TEXT,
            reason      TEXT,     -- justificación de Claude
            edge_type   TEXT,     -- mispricing | event-volatility | information | time-decay
            priority    INTEGER,  -- 1 = mayor convicción
            yes_price   REAL,
            volume24h   REAL,
            end_date    TEXT,
            created_at  TEXT
        )
    """)
    con.commit()


# ── Etapa 1: universo + filtro duro ──────────────────────────────────────────
def fetch_candidates(limit: int = 150) -> list[dict]:
    """Trae los mercados activos con más volumen de 24h de toda Polymarket."""
    try:
        r = requests.get(
            f"{GAMMA_API}/markets",
            params={
                "closed": "false", "active": "true",
                "order": "volume24hr", "ascending": "false",
                "limit": limit,
            },
            headers=HEADERS, timeout=20,
        )
        if not r.ok:
            print(f"⚠️  Gamma respondió {r.status_code} buscando candidatos")
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"⚠️  Error trayendo universo de mercados: {e}")
        return []


def _parse_yes_price(m: dict):
    prices = m.get("outcomePrices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except json.JSONDecodeError:
            return None
    if isinstance(prices, list) and prices:
        try:
            return float(prices[0])
        except (ValueError, TypeError):
            return None
    return None


def hard_filter(markets: list[dict]) -> list[dict]:
    """Filtros objetivos en código — cero llamadas a Claude."""
    now = datetime.datetime.now(datetime.timezone.utc)
    out = []
    for m in markets:
        slug = m.get("slug")
        if not slug:
            continue

        vol24 = float(m.get("volume24hr") or 0)
        if vol24 < MIN_VOLUME_24H:
            continue

        end_raw = m.get("endDate")
        if not end_raw:
            continue
        try:
            end_dt = datetime.datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        days_left = (end_dt - now).total_seconds() / 86400
        if not (MIN_DAYS_TO_END <= days_left <= MAX_DAYS_TO_END):
            continue

        yes = _parse_yes_price(m)
        if yes is None or not (PRICE_LO <= yes <= PRICE_HI):
            continue

        out.append({
            "market_id": m.get("conditionId") or m.get("id") or slug,
            "slug":      slug,
            "question":  m.get("question", ""),
            "category":  m.get("category", "") or "N/A",
            "yes_price": yes,
            "volume24h": vol24,
            "end_date":  end_raw,
            "days_left": round(days_left, 1),
        })

    out.sort(key=lambda c: c["volume24h"], reverse=True)
    return out[:MAX_CANDIDATES]


# ── Etapa 2: selección razonada con Claude ────────────────────────────────────
SELECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "selections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slug":      {"type": "string"},
                    "reason":    {"type": "string"},
                    "edge_type": {
                        "type": "string",
                        "enum": ["mispricing", "event-volatility", "information", "time-decay"],
                    },
                    "priority":  {"type": "integer"},
                },
                "required": ["slug", "reason", "edge_type", "priority"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["selections"],
    "additionalProperties": False,
}


def select_with_claude(client: Anthropic, candidates: list[dict]) -> list[dict]:
    """Una llamada a Claude por día: elige la watchlist con justificación."""
    lines = [
        f"- slug={c['slug']} | {c['question'][:80]} | cat={c['category']} | "
        f"YES={c['yes_price']:.2f} | vol24h=${c['volume24h']:,.0f} | resuelve en {c['days_left']}d"
        for c in candidates
    ]

    prompt = f"""Eres el selector de mercados de Jarvis, un sistema de paper trading en Polymarket.

Tu trabajo NO es decidir trades — eso lo hace otro módulo. Tu trabajo es elegir
QUÉ mercados vale la pena seguir hoy, y justificar cada elección.

CANDIDATOS (ya pasaron filtros de liquidez, ventana de resolución y precio no decidido):
{chr(10).join(lines)}

Elige exactamente {WATCHLIST_SIZE} mercados. Reglas:
- DIVERSIFICA entre categorías (deportes, política, cripto, economía, entretenimiento).
  No más de 2 mercados del mismo evento subyacente (p.ej. dos props del mismo partido
  cuentan como el mismo evento — están correlacionados, no diversifican nada).
- Prefiere mercados donde un analista con información pública tenga edge plausible
  sobre el precio actual, no simplemente los de más volumen.
- edge_type: "mispricing" (el precio parece mal calibrado), "event-volatility"
  (evento en vivo con movimientos explotables), "information" (hay información
  pública que el mercado no digirió), "time-decay" (precio extremo con tiempo de sobra).
- priority: 1 = mayor convicción, {WATCHLIST_SIZE} = menor.
- reason: 1-2 frases concretas en español. Nada genérico — di POR QUÉ este mercado
  y no otro de su categoría."""

    resp = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=4000,
        thinking={"type": "adaptive"},
        output_config={"format": {"type": "json_schema", "schema": SELECTION_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )

    raw = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        selections = json.loads(raw).get("selections", [])
    except json.JSONDecodeError as e:
        print(f"⚠️  Respuesta de selección no parseable: {e}")
        return []

    by_slug = {c["slug"]: c for c in candidates}
    out = []
    for s in selections:
        c = by_slug.get(s.get("slug"))
        if not c:
            continue  # Claude inventó un slug — ignorar
        out.append({**c, "reason": s["reason"], "edge_type": s["edge_type"],
                    "priority": s["priority"]})
    out.sort(key=lambda s: s["priority"])
    return out


# ── Persistencia ──────────────────────────────────────────────────────────────
def save_watchlist(con, selections: list[dict]):
    ts = _now()
    for s in selections:
        con.execute("""
            INSERT INTO watchlist
                (date, market_id, slug, question, category, reason, edge_type,
                 priority, yes_price, volume24h, end_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            _today(), s["market_id"], s["slug"], s["question"], s["category"],
            s["reason"], s["edge_type"], s["priority"],
            s["yes_price"], s["volume24h"], s["end_date"], ts,
        ))
    con.commit()


def get_todays_watchlist(con) -> list[dict]:
    rows = con.execute("""
        SELECT slug, question, category, reason, edge_type, priority
        FROM watchlist WHERE date = ? ORDER BY priority
    """, (_today(),)).fetchall()
    return [
        {"slug": r[0], "question": r[1], "category": r[2],
         "reason": r[3], "edge_type": r[4], "priority": r[5]}
        for r in rows
    ]


def ensure_watchlist(con, client: Anthropic, force: bool = False) -> tuple[list[dict], bool]:
    """
    Devuelve (watchlist, es_nueva). Reusa la selección de hoy si existe
    (1 llamada a Claude por día, no por corrida).
    """
    init_watchlist_table(con)

    if not force:
        existing = get_todays_watchlist(con)
        if existing:
            return existing, False

    candidates = hard_filter(fetch_candidates())
    if not candidates:
        print("⚠️  Sin candidatos que pasen el filtro duro — watchlist vacía")
        return [], False

    print(f"🔎 {len(candidates)} candidatos pasaron el filtro duro. Seleccionando con Claude...")
    selections = select_with_claude(client, candidates)
    if not selections:
        return [], False

    if force:
        con.execute("DELETE FROM watchlist WHERE date = ?", (_today(),))
    save_watchlist(con, selections)
    return get_todays_watchlist(con), True


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Jarvis Market Selector")
    parser.add_argument("--refresh", action="store_true", help="Forzar nueva selección hoy")
    parser.add_argument("--dry-run", action="store_true", help="Solo mostrar candidatos (sin Claude)")
    args = parser.parse_args()

    con = sqlite3.connect(DB_PATH)

    if args.dry_run:
        candidates = hard_filter(fetch_candidates())
        print(f"📋 {len(candidates)} candidatos pasan el filtro duro:\n")
        for c in candidates:
            print(f"  [{c['category']:>14s}] {c['question'][:60]:60s} "
                  f"YES={c['yes_price']:.2f} vol24h=${c['volume24h']:>10,.0f} {c['days_left']}d")
        return

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY no encontrado. Ejecuta set_env.ps1 primero.")
    client = Anthropic(api_key=api_key)

    watchlist, fresh = ensure_watchlist(con, client, force=args.refresh)
    tag = "nueva" if fresh else "cacheada (de hoy)"
    print(f"\n📡 WATCHLIST {tag} — {len(watchlist)} mercado(s):\n")
    for w in watchlist:
        print(f"  #{w['priority']} [{w['category']}] {w['question']}")
        print(f"     edge: {w['edge_type']} — {w['reason']}\n")


if __name__ == "__main__":
    main()
