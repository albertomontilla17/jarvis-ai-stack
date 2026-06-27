"""
jarvis_alerts.py
-----------------
Monitors Polymarket markets for price movements and generates smart
differential briefings — only reporting what actually changed.

Each run appends to a rolling history file (market_history.json) so
Claude can see price trajectories over time, not just the last snapshot.

Logic:
  - Every run: appends current prices with timestamp to history
  - First run: saves baseline, sends initial briefing
  - Subsequent runs:
      - Computes delta vs last snapshot
      - Passes full price history to Claude for trend context
      - No changes → short "todo tranquilo" message (no essay)
      - Changes detected → Claude analyzes delta + trajectory

Designed to run every hour via Windows Task Scheduler.

Usage:
    python jarvis_alerts.py

Environment variables required:
    ANTHROPIC_API_KEY   — for Claude analysis
    TELEGRAM_TOKEN      — Telegram bot token
    TELEGRAM_CHAT_ID    — Your Telegram chat ID (default: 7022147500)
"""

import json
import os
import requests
from anthropic import Anthropic
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
GAMMA_API        = "https://gamma-api.polymarket.com"
TELEGRAM_API     = "https://api.telegram.org"
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "7022147500")
CLAUDE_MODEL     = "claude-sonnet-4-6"

ALERT_THRESHOLD  = 0.10   # 10% move = notable change worth mentioning
BIG_MOVE         = 0.20   # 20%+ move = urgent alert
MARKETS_LIMIT    = 50
HISTORY_FILE     = Path(__file__).parent / "market_history.json"
MAX_HISTORY_RUNS = 24     # keep up to 24 hourly snapshots (~1 day)

# ── Polymarket ────────────────────────────────────────────────────────────────

def get_active_markets(limit: int = MARKETS_LIMIT) -> list[dict]:
    params = {
        "active": "true",
        "closed": "false",
        "order": "volume24hr",
        "ascending": "false",
        "limit": limit,
    }
    r = requests.get(f"{GAMMA_API}/markets", params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def extract_yes_price(market: dict) -> float | None:
    try:
        prices = json.loads(market.get("outcomePrices", '["?","?"]'))
        return float(prices[0]) if prices[0] != "?" else None
    except Exception:
        return None


def build_snapshot(markets: list[dict]) -> dict:
    """Build a clean snapshot dict keyed by market ID."""
    snapshot = {}
    for m in markets:
        market_id = str(m.get("id", ""))
        yes_price = extract_yes_price(m)
        if yes_price is None:
            continue
        vol = float(m.get("volume24hr") or m.get("volume") or 0)
        snapshot[market_id] = {
            "question": m.get("question", "Unknown"),
            "price":    yes_price,
            "volume":   vol,
            "endDate":  m.get("endDate", "")[:10],
        }
    return snapshot

# ── History persistence ───────────────────────────────────────────────────────

def load_history() -> list[dict]:
    """Load full history list. Each entry: {timestamp, snapshot}."""
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_history(history: list[dict]):
    """Save history, trimming to MAX_HISTORY_RUNS entries."""
    trimmed = history[-MAX_HISTORY_RUNS:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, indent=2, ensure_ascii=False)

# ── Delta detection ───────────────────────────────────────────────────────────

def compute_delta(previous: dict, current: dict) -> dict:
    """Compare two snapshots. Returns movers, resolved, and new markets."""
    moved    = []
    resolved = []
    new      = []

    prev_ids = set(previous.keys())
    curr_ids = set(current.keys())

    for mid in prev_ids & curr_ids:
        prev_price = previous[mid]["price"]
        curr_price = current[mid]["price"]
        change     = curr_price - prev_price
        abs_change = abs(change)

        if abs_change >= ALERT_THRESHOLD:
            moved.append({
                "id":        mid,
                "question":  current[mid]["question"],
                "prev":      prev_price,
                "curr":      curr_price,
                "change":    change,
                "abs_change": abs_change,
                "volume":    current[mid]["volume"],
                "endDate":   current[mid]["endDate"],
                "big_move":  abs_change >= BIG_MOVE,
            })

    for mid in prev_ids - curr_ids:
        resolved.append({
            "question":   previous[mid]["question"],
            "last_price": previous[mid]["price"],
        })

    for mid in curr_ids - prev_ids:
        new.append({
            "question": current[mid]["question"],
            "price":    current[mid]["price"],
            "volume":   current[mid]["volume"],
            "endDate":  current[mid]["endDate"],
        })

    moved.sort(key=lambda x: x["abs_change"], reverse=True)
    return {"moved": moved, "resolved": resolved, "new": new}

# ── Prompt builders ───────────────────────────────────────────────────────────

def build_trajectory(market_id: str, history: list[dict]) -> str:
    """Build a price trajectory string for a specific market across history."""
    points = []
    for entry in history:
        snap = entry["snapshot"]
        if market_id in snap:
            ts    = entry["timestamp"]
            price = snap[market_id]["price"]
            points.append(f"{ts}: {price*100:.0f}%")
    return " → ".join(points) if points else "sin historial previo"


def format_delta_for_prompt(delta: dict, history: list[dict]) -> str:
    lines = []

    if delta["moved"]:
        lines.append("MERCADOS CON MOVIMIENTO EN LA ÚLTIMA HORA:")
        for m in delta["moved"]:
            direction = "📈" if m["change"] > 0 else "📉"
            urgent    = " ⚠️ MOVIMIENTO GRANDE" if m["big_move"] else ""
            vol_fmt   = f"${m['volume']/1_000_000:.1f}M" if m["volume"] >= 1_000_000 else f"${m['volume']/1_000:.0f}K"
            trajectory = build_trajectory(m["id"], history)
            lines.append(
                f"{direction} {m['question']}\n"
                f"   Cambio: {m['prev']*100:.0f}% → {m['curr']*100:.0f}% "
                f"(Δ {m['change']*100:+.0f}%) | Vol: {vol_fmt} | Cierra: {m['endDate']}{urgent}\n"
                f"   Historial: {trajectory}"
            )
        lines.append("")

    if delta["resolved"]:
        lines.append("MERCADOS RESUELTOS (salieron del listado activo):")
        for r in delta["resolved"]:
            lines.append(f"✅ {r['question']} — último precio YES: {r['last_price']*100:.0f}%")
        lines.append("")

    if delta["new"]:
        lines.append("MERCADOS NUEVOS ENTRANDO AL TRENDING:")
        for n in delta["new"]:
            vol_fmt = f"${n['volume']/1_000_000:.1f}M" if n["volume"] >= 1_000_000 else f"${n['volume']/1_000:.0f}K"
            lines.append(f"🆕 {n['question']} — YES: {n['price']*100:.0f}% | Vol: {vol_fmt} | Cierra: {n['endDate']}")
        lines.append("")

    return "\n".join(lines)


def format_snapshot_for_prompt(snapshot: dict) -> str:
    """For first-run initial briefing."""
    lines = []
    for data in list(snapshot.values())[:20]:
        vol     = data["volume"]
        vol_fmt = f"${vol/1_000_000:.1f}M" if vol >= 1_000_000 else f"${vol/1_000:.0f}K"
        lines.append(
            f"- {data['question']} → YES: {data['price']*100:.0f}% | Vol: {vol_fmt} | Cierra: {data['endDate']}"
        )
    return "\n".join(lines)

# ── Claude analysis ───────────────────────────────────────────────────────────

def analyze_delta_with_claude(delta_text: str, hours_of_history: int) -> str:
    client = Anthropic()

    system_prompt = """Eres Jarvis, asistente de mercados de predicción.
Responde SIEMPRE en español. Formato para Telegram: texto plano, sin markdown, emojis con moderación.
Sé conciso y directo. Analiza tendencias cuando el historial lo permite."""

    user_prompt = f"""Estos son los cambios detectados en Polymarket en la última hora.
Tengo {hours_of_history} horas de historial de precios para contexto de tendencia.

{delta_text}

Analiza brevemente:
1. ¿Qué implican estos movimientos? ¿Hay tendencia sostenida o fue un salto puntual?
2. ¿El historial sugiere que el movimiento va a continuar o es una reversión?
3. ¿Hay alguna oportunidad accionable por estos cambios?
4. ¿Algún mercado nuevo merece atención inmediata?

Máximo 4-5 puntos cortos. No rellenes si no hay mucho que decir."""

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=600,
        messages=[{"role": "user", "content": user_prompt}],
        system=system_prompt,
    )
    return message.content[0].text


def analyze_first_run_with_claude(snapshot_text: str) -> str:
    client = Anthropic()

    system_prompt = """Eres Jarvis, asistente de mercados de predicción.
Responde SIEMPRE en español. Formato para Telegram: texto plano, sin markdown, emojis con moderación."""

    user_prompt = f"""Aquí están los mercados más activos de Polymarket ahora mismo:

{snapshot_text}

Es el primer registro del día — no tengo historial previo todavía.
Dame un análisis inicial: qué mercados son interesantes, qué precios llaman la atención,
y cuáles merecen seguimiento. Máximo 5 puntos concisos."""

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=700,
        messages=[{"role": "user", "content": user_prompt}],
        system=system_prompt,
    )
    return message.content[0].text

# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        print("⚠️  TELEGRAM_TOKEN not set — skipping Telegram.")
        return False
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    url    = f"{TELEGRAM_API}/bot{token}/sendMessage"
    for chunk in chunks:
        r = requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       chunk,
            "parse_mode": "HTML",
        }, timeout=10)
        if not r.ok:
            print(f"❌ Telegram error: {r.text}")
            return False
    return True

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now().strftime("%H:%M")
    print(f"🔍 [{now}] Jarvis checking Polymarket...")

    markets = get_active_markets()
    if not markets:
        print("No markets found.")
        return

    current_snapshot = build_snapshot(markets)
    history          = load_history()

    # ── First run ─────────────────────────────────────────────────────────────
    if not history:
        print("📝 First run — saving baseline and sending initial briefing.")
        history.append({"timestamp": now, "snapshot": current_snapshot})
        save_history(history)

        snapshot_text = format_snapshot_for_prompt(current_snapshot)
        analysis      = analyze_first_run_with_claude(snapshot_text)
        header        = f"🤖 <b>Jarvis Briefing Inicial ({now})</b>\n\n"
        print(header + analysis)
        send_telegram(header + analysis)
        return

    # ── Compute delta vs last snapshot ────────────────────────────────────────
    last_snapshot  = history[-1]["snapshot"]
    delta          = compute_delta(last_snapshot, current_snapshot)
    total_changes  = len(delta["moved"]) + len(delta["resolved"]) + len(delta["new"])

    # Append current snapshot to history and save
    history.append({"timestamp": now, "snapshot": current_snapshot})
    save_history(history)

    hours_of_history = len(history)

    # ── Nothing significant happened ──────────────────────────────────────────
    if total_changes == 0:
        msg = f"🤖 <b>Jarvis ({now})</b>\n\nSin cambios relevantes en la última hora. Mercados estables."
        print(msg)
        send_telegram(msg)
        return

    # ── Build and send differential briefing ─────────────────────────────────
    has_urgent = any(m["big_move"] for m in delta["moved"])
    urgency_prefix = "🚨" if has_urgent else "📊"

    print(f"\n{urgency_prefix} Detected: {len(delta['moved'])} movers, "
          f"{len(delta['resolved'])} resolved, {len(delta['new'])} new | "
          f"{hours_of_history}h of history available")

    delta_text = format_delta_for_prompt(delta, history[:-1])  # exclude current from trajectory
    analysis   = analyze_delta_with_claude(delta_text, hours_of_history)

    header = f"{urgency_prefix} <b>Jarvis — Cambios en Polymarket ({now})</b>\n"
    header += f"<i>({hours_of_history}h de historial disponible)</i>\n\n"

    if has_urgent:
        big_movers = [m for m in delta["moved"] if m["big_move"]]
        header += "⚠️ Movimientos grandes:\n"
        for bm in big_movers:
            direction = "📈" if bm["change"] > 0 else "📉"
            header += f"{direction} {bm['question'][:70]} ({bm['change']*100:+.0f}%)\n"
        header += "\n"

    message = header + analysis
    print(message)
    send_telegram(message)
    print("✅ Done!")


if __name__ == "__main__":
    main()
