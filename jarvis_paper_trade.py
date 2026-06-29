"""
jarvis_paper_trade.py
=====================
Jarvis Paper Trading Engine — trades ficticios con memoria adaptativa.
Monitorea un mercado de Polymarket a lo largo del tiempo y simula
entradas/salidas basadas en señales de precio, aprendiendo de trades pasados.

Uso:
    python jarvis_paper_trade.py --market <slug_o_conditionId> [--budget 100]

Dependencias: requests, anthropic, python-dotenv
"""

import os
import json
import sqlite3
import argparse
import datetime
import requests
from anthropic import Anthropic

# ── Configuración ─────────────────────────────────────────────────────────────
DB_PATH       = "paper_trades.db"
GAMMA_API     = "https://gamma-api.polymarket.com"
CLOB_API      = "https://clob.polymarket.com"
DEFAULT_BUDGET = 100.0   # USDC ficticios por sesión
MIN_CONFIDENCE = 0.60    # confianza mínima para entrar (se ajusta por aprendizaje)
MAX_POSITION   = 0.30    # máx % del presupuesto por trade

# ── DB Setup ──────────────────────────────────────────────────────────────────
def init_db(db_path: str = DB_PATH):
    con = sqlite3.connect(db_path)
    cur = con.cursor()

    cur.executescript("""
    CREATE TABLE IF NOT EXISTS markets (
        id          TEXT PRIMARY KEY,
        slug        TEXT,
        question    TEXT,
        category    TEXT,
        created_at  TEXT
    );

    CREATE TABLE IF NOT EXISTS snapshots (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        market_id   TEXT,
        ts          TEXT,
        yes_price   REAL,
        no_price    REAL,
        volume      REAL,
        phase       TEXT   -- 'early' | 'mid' | 'late' | 'closed'
    );

    CREATE TABLE IF NOT EXISTS trades (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        market_id       TEXT,
        ts_open         TEXT,
        ts_close        TEXT,
        side            TEXT,   -- 'YES' | 'NO'
        entry_price     REAL,
        exit_price      REAL,
        size_usdc       REAL,
        pnl_usdc        REAL,
        pnl_pct         REAL,
        outcome         TEXT,   -- 'win' | 'loss' | 'open' | 'canceled'
        confidence      REAL,
        rationale       TEXT,
        phase_open      TEXT,
        phase_close     TEXT,
        strategy_ver    INTEGER
    );

    CREATE TABLE IF NOT EXISTS strategy (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts              TEXT,
        version         INTEGER,
        min_confidence  REAL,
        max_position    REAL,
        phase_weights   TEXT,   -- JSON: {"early":0.5,"mid":1.0,"late":0.8}
        note            TEXT
    );
    """)

    # Estrategia inicial si no existe
    cur.execute("SELECT COUNT(*) FROM strategy")
    if cur.fetchone()[0] == 0:
        cur.execute("""
            INSERT INTO strategy (ts, version, min_confidence, max_position, phase_weights, note)
            VALUES (?, 1, ?, ?, ?, 'Estrategia inicial')
        """, (
            _now(), MIN_CONFIDENCE, MAX_POSITION,
            json.dumps({"early": 0.5, "mid": 1.0, "late": 0.8})
        ))

    con.commit()
    return con


# ── Helpers ───────────────────────────────────────────────────────────────────
def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def _f(val, default=0.0):
    """Convierte de forma segura a float (maneja None y strings)."""
    if val is None:
        return float(default)
    try:
        return float(val)
    except (ValueError, TypeError):
        return float(default)

def _load_strategy(con):
    cur = con.cursor()
    row = cur.execute(
        "SELECT version, min_confidence, max_position, phase_weights FROM strategy ORDER BY version DESC LIMIT 1"
    ).fetchone()
    return {
        "version":        row[0],
        "min_confidence": row[1],
        "max_position":   row[2],
        "phase_weights":  json.loads(row[3])
    }


# ── Polymarket Data ───────────────────────────────────────────────────────────
# Cloudflare bloquea requests sin User-Agent (403). SIEMPRE enviar este header.
HEADERS = {"User-Agent": "jarvis-paper-trading/1.0"}

def fetch_market(slug_or_id: str) -> dict:
    """
    Busca el mercado por slug o conditionId.
    Nota: un slug de EVENTO puede contener varios markets; tomamos el primero
    con orderbook activo. Para binarios YES/NO normalmente hay 1 solo.
    """
    # 1. Filtrar por slug en /markets (slug de market)
    r = requests.get(f"{GAMMA_API}/markets",
                     params={"slug": slug_or_id}, headers=HEADERS, timeout=15)
    if r.ok:
        data = r.json()
        if isinstance(data, list) and data:
            return data[0]

    # 2. Por conditionId directo
    r = requests.get(f"{GAMMA_API}/markets/{slug_or_id}", headers=HEADERS, timeout=15)
    if r.ok:
        data = r.json()
        if isinstance(data, dict) and data.get("id"):
            return data

    # 3. Como slug de EVENTO (puede tener varios markets)
    r = requests.get(f"{GAMMA_API}/events/slug/{slug_or_id}", headers=HEADERS, timeout=15)
    if r.ok:
        event = r.json()
        markets = event.get("markets", []) if isinstance(event, dict) else []
        # Tomar el primer market no cerrado
        for m in markets:
            if not m.get("closed"):
                return m
        if markets:
            return markets[0]

    raise ValueError(
        f"Mercado no encontrado: '{slug_or_id}'. "
        f"Verifica el slug en la URL de Polymarket (la parte después de /event/ o /market/)."
    )


def fetch_orderbook(token_id: str) -> dict:
    """Obtiene el mid-price del orderbook CLOB."""
    try:
        r = requests.get(f"{CLOB_API}/midpoint",
                        params={"token_id": token_id}, headers=HEADERS, timeout=8)
        if r.ok:
            return r.json()
    except Exception:
        pass
    return {}


def parse_prices(market: dict) -> tuple[float, float]:
    """Extrae YES/NO prices del market object."""
    yes_price = no_price = None

    # Intento 1: outcomePrices
    if "outcomePrices" in market:
        prices = market["outcomePrices"]
        if isinstance(prices, str):
            prices = json.loads(prices)
        if len(prices) >= 2:
            yes_price = float(prices[0])
            no_price  = float(prices[1])

    # Intento 2: tokens array
    if yes_price is None and "tokens" in market:
        for t in market["tokens"]:
            outcome = t.get("outcome", "").upper()
            price   = float(t.get("price", 0))
            if outcome == "YES":
                yes_price = price
            elif outcome == "NO":
                no_price  = price

    if yes_price is None:
        yes_price = 0.5
        no_price  = 0.5

    return yes_price, no_price


def detect_phase(market: dict, snapshots: list) -> str:
    """
    Determina la fase del evento.

    El volumen ACUMULADO siempre crece, así que no sirve para detectar fases.
    En su lugar usamos: (a) cercanía a endDate, (b) volatilidad de precio reciente.

    - early:  pocas snapshots, lejos de resolución
    - mid:    historial suficiente, precio moviéndose normal
    - late:   cerca de endDate O precio muy volátil (evento en curso)
    - closed: mercado resuelto/cerrado
    """
    if market.get("closed") or market.get("resolved"):
        return "closed"

    # (a) Cercanía a la fecha de cierre
    end_date = market.get("endDate") or market.get("end_date_iso")
    if end_date:
        try:
            end_dt = datetime.datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            now    = datetime.datetime.now(datetime.timezone.utc)
            hours_left = (end_dt - now).total_seconds() / 3600
            if hours_left <= 6:
                return "late"
        except (ValueError, TypeError):
            pass

    n = len(snapshots)
    if n < 3:
        return "early"

    # (b) Volatilidad de precio reciente (YES price está en índice 3)
    recent_yes = [s[3] for s in snapshots[-5:] if s[3] is not None]
    if len(recent_yes) >= 3:
        price_swing = max(recent_yes) - min(recent_yes)
        if price_swing >= 0.10:   # 10pts de movimiento = evento activo
            return "late"

    if n >= 5:
        return "mid"

    return "early"


# ── Historial para Claude ─────────────────────────────────────────────────────
def build_context(con, market_id: str, snapshots: list, open_trades: list, strategy: dict) -> str:
    """Construye el contexto que recibe Claude para analizar."""

    snap_lines = []
    for s in snapshots[-10:]:
        snap_lines.append(
            f"  [{s[2]}] YES={_f(s[3]):.2f} NO={_f(s[4]):.2f} vol={_f(s[5]):,.0f} fase={s[6]}"
        )

    trade_lines = []
    for t in open_trades:
        # Índices: t[4]=side, t[5]=entry_price, t[7]=size_usdc, t[11]=confidence, t[13]=phase_open
        trade_lines.append(
            f"  Trade#{t[0]}: {t[4]} @{_f(t[5]):.2f} size=${_f(t[7]):.1f} conf={_f(t[11]):.0%} fase={t[13]}"
        )

    # Accuracy histórica
    cur = con.cursor()
    stats = cur.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
            SUM(pnl_usdc) as total_pnl,
            phase_open,
            AVG(confidence) as avg_conf
        FROM trades
        WHERE market_id = ? AND outcome IN ('win','loss')
        GROUP BY phase_open
    """, (market_id,)).fetchall()

    perf_lines = []
    for row in stats:
        win_rate = (row[1] / row[0] * 100) if row[0] > 0 else 0
        perf_lines.append(
            f"  Fase {row[3]}: {row[0]} trades, {win_rate:.0f}% win, PnL=${_f(row[2]):.2f}, conf_avg={_f(row[4]):.0%}"
        )

    ctx = f"""
=== CONTEXTO JARVIS PAPER TRADING ===

ESTRATEGIA ACTUAL (v{strategy['version']}):
  min_confidence: {strategy['min_confidence']:.0%}
  max_position:   {strategy['max_position']:.0%} del presupuesto
  phase_weights:  {strategy['phase_weights']}

HISTORIAL DE PRECIOS (últimas 10 snapshots):
{chr(10).join(snap_lines) if snap_lines else '  (sin historial aún)'}

TRADES ABIERTOS:
{chr(10).join(trade_lines) if trade_lines else '  (ninguno)'}

PERFORMANCE HISTÓRICA POR FASE:
{chr(10).join(perf_lines) if perf_lines else '  (sin trades cerrados aún)'}
"""
    return ctx


# ── Claude Analysis ───────────────────────────────────────────────────────────
def analyze_with_claude(
    client: Anthropic,
    market: dict,
    context: str,
    yes_price: float,
    no_price: float,
    phase: str,
    budget_remaining: float,
    strategy: dict
) -> dict:
    """
    Llama a Claude para decidir: ENTER_YES, ENTER_NO, HOLD, EXIT.
    Retorna dict con action, side, size_pct, confidence, rationale.
    """

    system = """Eres el motor de decisión de Jarvis, un sistema de paper trading para mercados de predicción Polymarket.

Tu trabajo es analizar el contexto del mercado y decidir UNA acción.
Responde SOLO en JSON válido con este formato exacto:

{
  "action": "ENTER_YES" | "ENTER_NO" | "HOLD" | "EXIT",
  "side": "YES" | "NO" | null,
  "size_pct": 0.0-1.0,
  "confidence": 0.0-1.0,
  "rationale": "explicación breve en español"
}

Reglas:
- ENTER solo si confidence >= min_confidence de la estrategia actual
- size_pct: qué fracción del presupuesto usar (máx = max_position de estrategia)
- En fase 'early': sé más conservador (size_pct bajo)
- En fase 'late': puedes ser más agresivo si la señal es clara
- EXIT si tienes una posición abierta y el precio se movió contra ti >15%
- HOLD si no hay señal clara o ya tienes posición en ese lado
- Aprende de la performance histórica por fase mostrada en el contexto"""

    prompt = f"""
{context}

MERCADO ACTUAL:
  Pregunta: {market.get('question', 'N/A')}
  Categoría: {market.get('category', 'N/A')}
  YES price: {yes_price:.3f}
  NO price:  {no_price:.3f}
  Fase:      {phase}
  Presupuesto restante: ${budget_remaining:.2f} USDC (ficticio)

Decide tu acción ahora."""

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system=system,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = resp.content[0].text.strip()
    # Limpiar posibles backticks
    raw = raw.replace("```json", "").replace("```", "").strip()

    try:
        decision = json.loads(raw)
    except json.JSONDecodeError:
        decision = {
            "action": "HOLD",
            "side": None,
            "size_pct": 0.0,
            "confidence": 0.0,
            "rationale": f"Error parseando respuesta: {raw[:100]}"
        }

    return decision


# ── Trade Execution (ficticio) ────────────────────────────────────────────────
def execute_paper_trade(
    con, market_id: str, decision: dict,
    yes_price: float, no_price: float,
    budget: float, phase: str, strategy: dict
) -> dict | None:
    """Registra un trade ficticio en la DB."""
    action = decision.get("action", "HOLD")

    if action == "HOLD":
        return None

    cur = con.cursor()

    # EXIT: cerrar trades abiertos
    if action == "EXIT":
        open_trades = cur.execute(
            "SELECT id, side, entry_price, size_usdc FROM trades WHERE market_id=? AND outcome='open'",
            (market_id,)
        ).fetchall()

        closed = []
        for t in open_trades:
            tid, side, entry, size = t
            exit_price = yes_price if side == "YES" else no_price
            # Compras una posición a 'entry', ganas si su precio SUBE (sea YES o NO).
            # El precio de salida es el precio actual del MISMO lado que compraste.
            pnl_usdc = (exit_price - entry) * size
            pnl_pct  = pnl_usdc / size if size > 0 else 0
            outcome  = "win" if pnl_usdc > 0 else "loss"

            cur.execute("""
                UPDATE trades SET
                    ts_close=?, exit_price=?, pnl_usdc=?, pnl_pct=?,
                    outcome=?, phase_close=?
                WHERE id=?
            """, (_now(), exit_price, pnl_usdc, pnl_pct, outcome, phase, tid))
            closed.append({"id": tid, "pnl": pnl_usdc, "outcome": outcome})

        con.commit()
        return {"action": "EXIT", "closed": closed}

    # ENTER
    if action in ("ENTER_YES", "ENTER_NO"):
        side        = "YES" if action == "ENTER_YES" else "NO"
        entry_price = yes_price if side == "YES" else no_price
        max_size    = budget * strategy["max_position"]
        size_usdc   = min(budget * decision.get("size_pct", 0.1), max_size)

        if size_usdc < 1.0:
            return None  # trade demasiado pequeño

        trade_id = cur.execute("""
            INSERT INTO trades
                (market_id, ts_open, side, entry_price, size_usdc, outcome,
                 confidence, rationale, phase_open, strategy_ver)
            VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
        """, (
            market_id, _now(), side, entry_price, size_usdc,
            decision.get("confidence", 0),
            decision.get("rationale", ""),
            phase,
            strategy["version"]
        )).lastrowid

        con.commit()
        return {
            "action": "ENTER",
            "trade_id": trade_id,
            "side": side,
            "entry": entry_price,
            "size": size_usdc
        }

    return None


# ── Aprendizaje ───────────────────────────────────────────────────────────────
def maybe_update_strategy(con, client: Anthropic):
    """
    Cada 10 trades cerrados, Claude revisa la estrategia y puede ajustarla.
    """
    cur = con.cursor()
    closed_count = cur.execute(
        "SELECT COUNT(*) FROM trades WHERE outcome IN ('win','loss')"
    ).fetchone()[0]

    current_ver = cur.execute(
        "SELECT version FROM strategy ORDER BY version DESC LIMIT 1"
    ).fetchone()[0]

    # Revisar cada 10 trades nuevos
    if closed_count < 10 or closed_count % 10 != 0:
        return

    # ¿Ya revisamos en este checkpoint?
    last_review_count = cur.execute("""
        SELECT COUNT(*) FROM trades t
        JOIN strategy s ON t.strategy_ver = s.version
        WHERE t.outcome IN ('win','loss') AND s.version = ?
    """, (current_ver,)).fetchone()[0]

    if last_review_count < 10:
        return

    # Estadísticas para Claude
    stats = cur.execute("""
        SELECT
            phase_open,
            COUNT(*) as total,
            SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
            AVG(pnl_pct) as avg_pnl_pct,
            AVG(confidence) as avg_conf
        FROM trades
        WHERE outcome IN ('win','loss')
        GROUP BY phase_open
    """).fetchall()

    current = _load_strategy(con)

    stats_text = "\n".join([
        f"  {r[0]}: {r[1]} trades, {r[2]/r[1]*100:.0f}% win, avg_pnl={_f(r[3])*100:.1f}%, avg_conf={_f(r[4]):.0%}"
        for r in stats
    ])

    prompt = f"""Eres el optimizador de estrategia de Jarvis paper trading.

Estrategia actual (v{current['version']}):
  min_confidence: {current['min_confidence']:.0%}
  max_position: {current['max_position']:.0%}
  phase_weights: {current['phase_weights']}

Performance acumulada por fase:
{stats_text}

Basado en estos resultados, ajusta la estrategia. Responde SOLO en JSON:
{{
  "min_confidence": 0.0-1.0,
  "max_position": 0.0-0.5,
  "phase_weights": {{"early": 0.0-1.0, "mid": 0.0-1.0, "late": 0.0-1.0}},
  "note": "explicación del ajuste en español"
}}

Reglas:
- Si win_rate < 40% en una fase, reduce el weight de esa fase
- Si avg_pnl_pct < -5%, aumenta min_confidence
- Cambios conservadores: máx ±0.10 por revisión"""

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = resp.content[0].text.strip().replace("```json","").replace("```","").strip()

    try:
        new_strat = json.loads(raw)
        cur.execute("""
            INSERT INTO strategy (ts, version, min_confidence, max_position, phase_weights, note)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            _now(),
            current_ver + 1,
            new_strat["min_confidence"],
            new_strat["max_position"],
            json.dumps(new_strat["phase_weights"]),
            new_strat.get("note", "Auto-ajuste")
        ))
        con.commit()
        print(f"\n🧠 Estrategia actualizada a v{current_ver + 1}: {new_strat.get('note','')}")
    except Exception as e:
        print(f"⚠️  Error actualizando estrategia: {e}")


# ── Report ────────────────────────────────────────────────────────────────────
def print_report(con, market_id: str, budget_start: float, budget_now: float):
    cur = con.cursor()
    trades = cur.execute("""
        SELECT outcome, COUNT(*), SUM(pnl_usdc), AVG(pnl_pct)
        FROM trades WHERE market_id=?
        GROUP BY outcome
    """, (market_id,)).fetchall()

    print("\n" + "="*50)
    print("📊 REPORTE PAPER TRADING")
    print("="*50)
    print(f"  Presupuesto inicial: ${budget_start:.2f}")
    print(f"  Presupuesto actual:  ${budget_now:.2f}")
    print(f"  PnL neto:            ${budget_now - budget_start:.2f}")
    print()
    for row in trades:
        print(f"  {row[0].upper():8s}: {row[1]} trades | PnL ${_f(row[2]):.2f} | avg {_f(row[3])*100:.1f}%")
    print("="*50)


# ── Main Loop ─────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Jarvis Paper Trading Engine")
    parser.add_argument("--market",  required=True, help="Slug o conditionId del mercado")
    parser.add_argument("--budget",  type=float, default=DEFAULT_BUDGET, help="Presupuesto ficticio en USDC")
    parser.add_argument("--once",    action="store_true", help="Ejecutar una sola vez (para Task Scheduler)")
    args = parser.parse_args()

    # Init
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY no encontrado. Ejecuta set_env.ps1 primero.")

    client = Anthropic(api_key=api_key)
    con    = init_db()
    budget = args.budget

    print(f"🚀 Jarvis Paper Trading — Mercado: {args.market}")
    print(f"💰 Presupuesto ficticio: ${budget:.2f} USDC")

    # Fetch mercado
    market = fetch_market(args.market)
    market_id = market.get("conditionId") or market.get("id") or args.market
    question  = market.get("question", "N/A")

    print(f"📋 {question}")

    # Guardar mercado en DB
    con.execute("""
        INSERT OR IGNORE INTO markets (id, slug, question, category, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (
        market_id,
        market.get("slug", ""),
        question,
        market.get("category", ""),
        _now()
    ))
    con.commit()

    # Snapshot actual
    yes_price, no_price = parse_prices(market)
    volume = float(market.get("volume", 0) or 0)

    cur = con.cursor()
    snapshots = cur.execute(
        "SELECT * FROM snapshots WHERE market_id=? ORDER BY ts DESC LIMIT 20",
        (market_id,)
    ).fetchall()[::-1]  # cronológico

    phase = detect_phase(market, snapshots)

    # Guardar snapshot
    cur.execute("""
        INSERT INTO snapshots (market_id, ts, yes_price, no_price, volume, phase)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (market_id, _now(), yes_price, no_price, volume, phase))
    con.commit()

    snapshots = cur.execute(
        "SELECT * FROM snapshots WHERE market_id=? ORDER BY ts",
        (market_id,)
    ).fetchall()

    # Trades abiertos
    open_trades = cur.execute(
        "SELECT * FROM trades WHERE market_id=? AND outcome='open'",
        (market_id,)
    ).fetchall()

    # Calcular budget restante (descontar posiciones abiertas)
    # t[7] = size_usdc (NO t[6] que es exit_price)
    invested = sum(_f(t[7]) for t in open_trades)
    budget_remaining = budget - invested

    print(f"\n📈 YES: {yes_price:.3f} | NO: {no_price:.3f} | Fase: {phase}")
    print(f"💼 Invertido: ${invested:.2f} | Disponible: ${budget_remaining:.2f}")

    # Estrategia actual
    strategy = _load_strategy(con)

    # Contexto para Claude
    context = build_context(con, market_id, snapshots, open_trades, strategy)

    # Decisión
    print("\n🤖 Analizando con Claude...")
    decision = analyze_with_claude(
        client, market, context,
        yes_price, no_price, phase,
        budget_remaining, strategy
    )

    action     = decision.get("action", "HOLD")
    confidence = decision.get("confidence", 0)
    rationale  = decision.get("rationale", "")

    print(f"\n📌 Decisión: {action}")
    print(f"   Confianza: {confidence:.0%}")
    print(f"   Razón: {rationale}")

    # Ejecutar trade ficticio
    result = execute_paper_trade(
        con, market_id, decision,
        yes_price, no_price,
        budget, phase, strategy
    )

    if result:
        if result["action"] == "ENTER":
            print(f"\n✅ Trade #{result['trade_id']} abierto: {result['side']} @ {result['entry']:.3f} | ${result['size']:.2f}")
        elif result["action"] == "EXIT":
            for c in result["closed"]:
                emoji = "🟢" if c["outcome"] == "win" else "🔴"
                print(f"{emoji} Trade #{c['id']} cerrado | PnL: ${c['pnl']:.2f} ({c['outcome']})")
    else:
        print("⏸️  Sin cambios en posición.")

    # ¿Actualizar estrategia?
    maybe_update_strategy(con, client)

    # Reporte
    # Reporte: budget actual = budget inicial + PnL realizado (trades cerrados)
    realized_pnl = cur.execute(
        "SELECT COALESCE(SUM(pnl_usdc), 0) FROM trades WHERE market_id=? AND outcome IN ('win','loss')",
        (market_id,)
    ).fetchone()[0]
    print_report(con, market_id, budget, budget + realized_pnl)

    print("\n✅ Listo. Vuelve a ejecutar para la próxima snapshot.")


if __name__ == "__main__":
    main()
