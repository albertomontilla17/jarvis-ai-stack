"""
jarvis_alerts.py
-----------------
Monitors Polymarket markets for sudden price movements.
If any market moves more than ALERT_THRESHOLD in price,
Jarvis sends an immediate alert to Telegram.

Designed to run every hour via Windows Task Scheduler.
Stores previous prices in a local JSON file to detect changes.

Usage:
    python jarvis_alerts.py

Environment variables required:
    TELEGRAM_TOKEN      — Telegram bot token
    TELEGRAM_CHAT_ID    — Your Telegram chat ID (default: 7022147500)
"""

import json
import os
import requests
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
GAMMA_API        = "https://gamma-api.polymarket.com"
TELEGRAM_API     = "https://api.telegram.org"
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "7022147500")
ALERT_THRESHOLD  = 0.15   # 15% price movement triggers alert
MARKETS_LIMIT    = 50     # how many markets to monitor
PRICE_FILE       = Path(__file__).parent / "market_prices.json"

# ── Polymarket ────────────────────────────────────────────────────────────────

def get_active_markets(limit: int = MARKETS_LIMIT) -> list[dict]:
    """Fetch active markets sorted by volume."""
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
    """Extract YES price from market data."""
    try:
        prices = json.loads(market.get("outcomePrices", '["?","?"]'))
        return float(prices[0]) if prices[0] != "?" else None
    except Exception:
        return None


def load_previous_prices() -> dict:
    """Load previously stored prices from disk."""
    if PRICE_FILE.exists():
        with open(PRICE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_current_prices(prices: dict):
    """Save current prices to disk."""
    with open(PRICE_FILE, "w") as f:
        json.dump(prices, f, indent=2)

# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> bool:
    """Send alert to Telegram."""
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        print("⚠️  TELEGRAM_TOKEN not set.")
        return False

    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    r = requests.post(url, json=payload, timeout=10)
    return r.ok

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now().strftime("%H:%M")
    print(f"🔍 [{now}] Checking Polymarket for anomalies...")

    markets = get_active_markets()
    if not markets:
        print("No markets found.")
        return

    previous = load_previous_prices()
    current  = {}
    alerts   = []

    for m in markets:
        market_id = str(m.get("id", ""))
        question  = m.get("question", "Unknown")
        yes_price = extract_yes_price(m)

        if yes_price is None:
            continue

        current[market_id] = {
            "question": question,
            "price": yes_price,
            "timestamp": now,
        }

        # Compare with previous price
        if market_id in previous:
            prev_price = previous[market_id]["price"]
            change     = abs(yes_price - prev_price)

            if change >= ALERT_THRESHOLD:
                direction = "📈" if yes_price > prev_price else "📉"
                alerts.append({
                    "question": question,
                    "prev": prev_price,
                    "curr": yes_price,
                    "change": change,
                    "direction": direction,
                })

    # Save current prices for next run
    save_current_prices(current)

    if not alerts:
        print(f"✅ No anomalies detected across {len(current)} markets.")
        return

    # Sort by biggest move first
    alerts.sort(key=lambda x: x["change"], reverse=True)

    # Build Telegram message
    lines = [f"🚨 <b>Jarvis — Alerta de Mercado ({now})</b>\n"]
    for a in alerts:
        lines.append(
            f"{a['direction']} <b>{a['question'][:80]}</b>\n"
            f"   Antes: {a['prev']*100:.0f}% → Ahora: {a['curr']*100:.0f}% "
            f"(Δ {a['change']*100:.0f}%)\n"
        )

    message = "\n".join(lines)
    print(f"\n🚨 {len(alerts)} anomalies detected!")
    print(message)

    send_telegram(message)
    print("✅ Alert sent to Telegram!")


if __name__ == "__main__":
    main()
