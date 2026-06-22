# jarvis-ai-stack

Personal AI agent stack built by Alberto Montilla — Applied AI student at Miami Dade College.

Jarvis is a personal AI assistant running on [OpenClaw](https://github.com/openclaw/openclaw), powered by Claude, connected to Telegram. This repo documents the tools, scripts, and integrations built on top of it.

---

## Stack Overview

| Layer | Tool |
|---|---|
| AI Agent | OpenClaw + Claude Opus 4.6 |
| Messaging | Telegram (@Alberto_Montilla_claw_bot) |
| Prediction Markets | Polymarket via PolyClaw (Chainstack) |
| Payments | x402 protocol on Base (L2) |
| Wallet | MetaMask (Polygon) + CDP Server Wallet + Circle Nanopayments |
| Infra | AWS / Amazon Bedrock |
| Hardware | Lenovo Legion (Windows) |

---

## Modules

### 🎯 `jarvis_polymarket.py`
Fetches trending or keyword-searched Polymarket markets, analyzes them with Claude, and delivers a Spanish-language briefing directly to Telegram.

**Features:**
- Live market data via Polymarket Gamma API (no API key required)
- Claude-powered analysis in Spanish — spots mispriced odds, high-volume signals, time-sensitive opportunities
- Telegram delivery with automatic message chunking
- CLI flags for trending, keyword search, raw data, and skipping Telegram
- Automated daily runs via Windows Task Scheduler (12:00 PM)

**Usage:**
```bash
# Analyze trending markets + send to Telegram
python jarvis_polymarket.py

# Search by keyword
python jarvis_polymarket.py --search "FIFA"

# Raw data only (no Claude, no Telegram)
python jarvis_polymarket.py --raw

# Skip Telegram, print only
python jarvis_polymarket.py --no-telegram

# Control number of markets
python jarvis_polymarket.py --limit 10
```

**Environment variables required:**
```bash
ANTHROPIC_API_KEY    # Anthropic API key
TELEGRAM_TOKEN       # Telegram bot token
TELEGRAM_CHAT_ID     # Your Telegram chat ID (default: 7022147500)
```

**Requirements:**
```bash
pip install anthropic requests
```

### 🚨 `jarvis_alerts.py`
Monitors 50 Polymarket markets every hour for sudden price movements. Sends an immediate Telegram alert when any market moves more than 15%.

**Features:**
- Monitors top 50 markets by volume
- Stores price history locally in `market_prices.json`
- Detects moves >15% and sends Spanish-language alert to Telegram
- Automated hourly runs via Windows Task Scheduler

**Usage:**
```bash
python jarvis_alerts.py
```

**First run:** saves current prices (no alert sent)
**Subsequent runs:** compares prices and alerts on anomalies

---

### 🔑 `setup_polymarket_creds.py`
Generates Polymarket CLOB API credentials (apiKey, secret, passphrase) from your MetaMask private key. Run once to set up trading access.

**Usage:**
```bash
$env:POLYGON_PRIVATE_KEY="0x..."
$env:POLYGON_ADDRESS="0x228D8F8c9409df6C7793FD7A593885698209cAfd"
python setup_polymarket_creds.py
```

---

## Setup

### 1. Install OpenClaw
```bash
npm install -g openclaw
```

### 2. Install clawhub
```bash
npm install -g clawhub
```

### 3. Install PolyClaw (Chainstack)
```bash
cd %USERPROFILE%\.openclaw\workspace\skills
git clone https://github.com/chainstacklabs/polyclaw polyclaw-chainstack
cd polyclaw-chainstack
uv sync
```

### 4. Test PolyClaw CLI
```bash
# Trending markets
uv run python scripts/polyclaw.py markets trending

# Search by keyword
uv run python scripts/polyclaw.py markets search "election"
```

### 5. Run Jarvis analysis script
```bash
# Set environment variables (Windows PowerShell)
$env:ANTHROPIC_API_KEY="sk-ant-..."
$env:TELEGRAM_TOKEN="your-token"

python jarvis_polymarket.py
```

### 6. Automate with Windows Task Scheduler
Create `run_jarvis_polymarket.bat`:
```bat
@echo off
set ANTHROPIC_API_KEY=your-key
set TELEGRAM_TOKEN=your-token
cd C:\Users\alber\Documents\Projects\Polymarket
python jarvis_polymarket.py
```
Schedule via Task Scheduler → Create Basic Task → Daily → 12:00 PM.

---

## Roadmap

- [x] Polymarket data via Gamma API
- [x] Claude-powered market analysis
- [x] Telegram delivery (Spanish)
- [x] Windows Task Scheduler — daily briefing at 12:00 PM
- [x] Anomaly alerts — hourly monitoring of 50 markets, Telegram alert on >15% move
- [x] GitHub documentation
- [ ] Polymarket trading — execute trades via PolyClaw CLOB on Polygon (wallet funded, pending bridge)
- [ ] x402 integration — expose analysis as a paid API endpoint
- [ ] Amazon Bedrock AgentCore — migrate Jarvis to AWS with native x402 support

---

## Notes

- Polymarket Gamma API is fully public — no auth required for read access
- PolyClaw trading features require a Polygon wallet funded with USDC.e + Chainstack RPC node
- Trading on Polymarket requires USDC.e on Polygon network (not Base)
- All scripts in this repo are for research and educational purposes

---

## Author

Alberto Montilla — Miami, FL
Applied AI @ Miami Dade College
Building at the intersection of AI agents, prediction markets, and Web3 payments.
