"""
jarvis_trade.py
---------------
Jarvis autonomous trading module. Fetches Polymarket data, uses Claude
to identify opportunities, and executes trades automatically via PolyClaw.

Designed to run after jarvis_polymarket.py analysis. Trades conservatively
with small amounts to validate the system before scaling.

Usage:
    python jarvis_trade.py                    # auto mode - Claude decides
    python jarvis_trade.py --dry-run          # simulate only, no real trades
    python jarvis_trade.py --amount 2         # set max trade size (default $2)
    python jarvis_trade.py --limit 20         # number of markets to analyze

Environment variables required:
    ANTHROPIC_API_KEY
    TELEGRAM_TOKEN
    POLYCLAW_PRIVATE_KEY   — same as POLYGON_PRIVATE_KEY
    CHAINSTACK_NODE        — Polygon RPC URL
    TELEGRAM_CHAT_ID       — optional, defaults to 7022147500
"""

import argparse
import json
import os
import subprocess
import sys
import requests
from anthropic import Anthropic

# ── Config ────────────────────────────────────────────────────────────────────
GAMMA_API        = "https://gamma-api.polymarket.com"
TELEGRAM_API     = "https://api.telegram.org"
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "7022147500")
POLYCLAW_DIR     = r"C:\Users\alber\.openclaw\workspace\skills\polyclaw-chainstack"
DEFAULT_AMOUNT   = 2      # default trade size in USD
MAX_AMOUNT       = 10     # hard cap per trade
MIN_CONFIDENCE   = 0.75   # Claude must be >= 75% confident to trade

# ── Polymarket ────────────────────────────────────────────────────────────────

def get_trending_markets(limit: int = 20) -> list[dict]:
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


def format_markets(markets: list[dict]) -> str:
    lines = []
    for m in markets:
        try:
            prices    = json.loads(m.get("outcomePrices", '["?","?"]'))
            yes_price = float(prices[0]) if prices[0] != "?" else None
            vol       = float(m.get("volume24hr") or m.get("volume") or 0)
            question  = m.get("question", "Unknown")
            market_id = str(m.get("id", ""))
            end_date  = m.get("endDate", "")[:10]

            yes_pct  = f"{yes_price*100:.0f}%" if yes_price is not None else "N/A"
            vol_fmt  = f"${vol/1_000_000:.1f}M" if vol >= 1_000_000 else f"${vol/1_000:.0f}K"

            lines.append(f"ID:{market_id} | {question} | YES:{yes_pct} | Vol:{vol_fmt} | Ends:{end_date}")
        except Exception:
            continue
    return "\n".join(lines)

# ── Claude decision ───────────────────────────────────────────────────────────

def get_trade_decision(markets_text: str, max_amount: int) -> dict:
    """Ask Claude to identify the best trade opportunity."""
    client = Anthropic()

    system_prompt = """Eres Jarvis, un agente de trading especializado en mercados de predicción.
Tu trabajo es analizar mercados de Polymarket e identificar UNA sola oportunidad de trade clara.
Debes ser conservador — solo recomienda un trade si tienes alta confianza (>75%).
Responde SOLO en formato JSON válido, sin texto adicional ni markdown."""

    user_prompt = f"""Analiza estos mercados de Polymarket e identifica la mejor oportunidad de trade.

{markets_text}

Responde con este JSON exacto:
{{
  "trade": true/false,
  "market_id": "ID del mercado o null",
  "side": "YES o NO o null",
  "confidence": 0.0-1.0,
  "amount": 2-{max_amount},
  "reasoning": "explicación en español de máximo 2 oraciones",
  "question": "pregunta del mercado o null"
}}

Reglas:
- Solo recomienda trade si confidence >= {MIN_CONFIDENCE}
- Prefiere mercados con >$1M volumen
- Evita mercados que cierran hoy
- Monto máximo: ${max_amount}
- Si no hay oportunidad clara, pon trade: false"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": user_prompt}],
        system=system_prompt,
    )

    raw = message.content[0].text.strip()
    # Clean potential markdown
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    
    return json.loads(raw.strip())

# ── PolyClaw execution ────────────────────────────────────────────────────────

def execute_trade(market_id: str, side: str, amount: int) -> tuple[bool, str]:
    """Execute trade via PolyClaw CLI."""
    cmd = [
        "uv", "run", "python", "scripts/polyclaw.py",
        "buy", market_id, side.upper(), str(amount)
    ]
    
    env = os.environ.copy()
    
    result = subprocess.run(
        cmd,
        cwd=POLYCLAW_DIR,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    
    success = result.returncode == 0
    output  = result.stdout + result.stderr
    return success, output

# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        return False
    url     = f"{TELEGRAM_API}/bot{token}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    r       = requests.post(url, json=payload, timeout=10)
    return r.ok

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Jarvis Autonomous Trading")
    parser.add_argument("--dry-run", action="store_true", help="Simulate only, no real trades")
    parser.add_argument("--amount",  type=int, default=DEFAULT_AMOUNT, help="Max trade size in USD")
    parser.add_argument("--limit",   type=int, default=20, help="Number of markets to analyze")
    args = parser.parse_args()

    amount = min(args.amount, MAX_AMOUNT)

    print("🔍 Fetching Polymarket data...")
    markets = get_trending_markets(args.limit)
    if not markets:
        print("No markets found.")
        return

    markets_text = format_markets(markets)
    print(f"📊 {len(markets)} markets loaded. Asking Claude for trade decision...\n")

    decision = get_trade_decision(markets_text, amount)

    print("🤖 Claude's decision:")
    print(json.dumps(decision, indent=2, ensure_ascii=False))

    if not decision.get("trade"):
        msg = f"🤖 <b>Jarvis Trading</b>\n\n❌ Sin oportunidad clara hoy.\n\n{decision.get('reasoning', '')}"
        print("\n" + msg)
        send_telegram(msg)
        return

    market_id  = decision["market_id"]
    side       = decision["side"]
    confidence = decision["confidence"]
    reasoning  = decision["reasoning"]
    question   = decision.get("question", market_id)
    trade_amt  = min(decision.get("amount", amount), MAX_AMOUNT)

    print(f"\n💡 Trade opportunity found!")
    print(f"   Market: {question}")
    print(f"   Side: {side} | Amount: ${trade_amt} | Confidence: {confidence*100:.0f}%")

    if args.dry_run:
        msg = (
            f"🤖 <b>Jarvis Trading — Simulación</b>\n\n"
            f"📌 {question}\n"
            f"➡️ Comprar {side} — ${trade_amt}\n"
            f"🎯 Confianza: {confidence*100:.0f}%\n\n"
            f"💬 {reasoning}\n\n"
            f"⚠️ Modo simulación — no se ejecutó trade real."
        )
        print("\n[DRY RUN] No real trade executed.")
        send_telegram(msg)
        return

    print(f"\n⚡ Executing trade via PolyClaw...")
    success, output = execute_trade(market_id, side, trade_amt)

    if success:
        msg = (
            f"🤖 <b>Jarvis Trading</b>\n\n"
            f"✅ Trade ejecutado!\n"
            f"📌 {question}\n"
            f"➡️ Compré {side} — ${trade_amt}\n"
            f"🎯 Confianza: {confidence*100:.0f}%\n\n"
            f"💬 {reasoning}"
        )
        print("✅ Trade executed successfully!")
    else:
        msg = (
            f"🤖 <b>Jarvis Trading</b>\n\n"
            f"❌ Error ejecutando trade.\n"
            f"📌 {question}\n"
            f"🔍 {output[:200]}"
        )
        print(f"❌ Trade failed: {output}")

    send_telegram(msg)


if __name__ == "__main__":
    main()
