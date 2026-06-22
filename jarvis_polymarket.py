"""
jarvis_polymarket.py
--------------------
Fetches trending Polymarket markets and uses Claude to generate
an analysis report. Sends the briefing to Telegram via Jarvis.

Usage:
    python jarvis_polymarket.py                  # trending markets analysis
    python jarvis_polymarket.py --search "FIFA"  # search by keyword
    python jarvis_polymarket.py --limit 10       # control how many markets to analyze
    python jarvis_polymarket.py --no-telegram    # skip Telegram, print only

Environment variables:
    ANTHROPIC_API_KEY   — required
    TELEGRAM_TOKEN      — required for Telegram delivery
    TELEGRAM_CHAT_ID    — required for Telegram delivery (default: 7022147500)
"""

import argparse
import json
import os
import requests
from anthropic import Anthropic

# ── Config ────────────────────────────────────────────────────────────────────
GAMMA_API        = "https://gamma-api.polymarket.com"
TELEGRAM_API     = "https://api.telegram.org"
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "7022147500")
CLAUDE_MODEL = "claude-opus-4-6"   # swap to claude-sonnet-4-6 to save cost
MAX_MARKETS  = 20                   # default number of markets to pull

# ── Polymarket helpers ────────────────────────────────────────────────────────

def get_trending_markets(limit: int = MAX_MARKETS) -> list[dict]:
    """Fetch active markets sorted by 24h volume."""
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


def search_markets(query: str, limit: int = MAX_MARKETS) -> list[dict]:
    """Search markets by keyword."""
    params = {
        "active": "true",
        "closed": "false",
        "q": query,
        "limit": limit,
    }
    r = requests.get(f"{GAMMA_API}/markets", params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def format_markets_for_prompt(markets: list[dict]) -> str:
    """Convert raw market data into a clean text block for the LLM."""
    lines = []
    for m in markets:
        try:
            prices = json.loads(m.get("outcomePrices", '["?","?"]'))
            yes_price = float(prices[0]) if prices[0] != "?" else None
            vol = m.get("volume24hr") or m.get("volume") or 0
            question = m.get("question", "Unknown")
            end_date = m.get("endDate", "")[:10]  # YYYY-MM-DD

            yes_pct  = f"{yes_price*100:.0f}%" if yes_price is not None else "N/A"
            vol_fmt  = f"${float(vol)/1_000_000:.1f}M" if float(vol) >= 1_000_000 else f"${float(vol)/1_000:.0f}K"

            lines.append(f"- [{vol_fmt} vol] {question} → YES: {yes_pct} (ends {end_date})")
        except Exception:
            continue
    return "\n".join(lines)


# ── Claude analysis ───────────────────────────────────────────────────────────

def analyze_with_claude(markets_text: str, context: str = "trending") -> str:
    """Send market data to Claude and return a Telegram-ready analysis."""
    client = Anthropic()

    system_prompt = """You are Jarvis, an AI research assistant specialized in prediction markets.
Your job is to analyze Polymarket data and deliver sharp, actionable insights.
Format your response for Telegram — use plain text, emojis sparingly, keep it concise.
Structure: brief intro, top insights (3-5 bullets), one strategic takeaway at the end.
Do not use markdown headers or bold — Telegram renders them as raw symbols."""

    user_prompt = f"""Here are the current {context} Polymarket markets:

{markets_text}

Analyze this data. Look for:
1. Markets with surprising probabilities (under or overpriced vs reality)
2. High-volume markets indicating strong crowd interest
3. Interesting patterns or correlations between markets
4. Any time-sensitive opportunities (ending soon with uncertain odds)

Deliver a clear, useful briefing I can act on."""

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": user_prompt}],
        system=system_prompt,
    )
    return message.content[0].text


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_to_telegram(text: str) -> bool:
    """Send a message to Telegram. Returns True if successful."""
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        print("⚠️  TELEGRAM_TOKEN not set — skipping Telegram delivery.")
        return False

    # Telegram messages max 4096 chars — split if needed
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"

    for i, chunk in enumerate(chunks):
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
        }
        r = requests.post(url, json=payload, timeout=10)
        if not r.ok:
            print(f"❌ Telegram error: {r.text}")
            return False

    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Jarvis Polymarket Analyzer")
    parser.add_argument("--search",      type=str,  default=None,  help="Search keyword")
    parser.add_argument("--limit",       type=int,  default=MAX_MARKETS, help="Number of markets")
    parser.add_argument("--raw",         action="store_true", help="Print raw market data only, skip Claude")
    parser.add_argument("--no-telegram", action="store_true", help="Skip Telegram delivery")
    args = parser.parse_args()

    print("🔍 Fetching Polymarket data...")

    if args.search:
        markets = search_markets(args.search, args.limit)
        context = f'search results for "{args.search}"'
    else:
        markets = get_trending_markets(args.limit)
        context = "trending"

    if not markets:
        print("No markets found.")
        return

    markets_text = format_markets_for_prompt(markets)

    if args.raw:
        print(f"\n📊 {len(markets)} markets found:\n")
        print(markets_text)
        return

    print(f"📊 {len(markets)} markets found. Sending to Claude for analysis...\n")
    analysis = analyze_with_claude(markets_text, context)

    print("=" * 60)
    print("🤖 JARVIS POLYMARKET BRIEFING")
    print("=" * 60)
    print(analysis)
    print("=" * 60)

    if not args.no_telegram:
        header = "🤖 <b>Jarvis Polymarket Briefing</b>\n\n"
        print("\n📨 Sending to Telegram...")
        success = send_to_telegram(header + analysis)
        if success:
            print("✅ Delivered to Telegram!")


if __name__ == "__main__":
    main()
