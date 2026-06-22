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
| Wallet | CDP Server Wallet + Circle Nanopayments |
| Infra | AWS / Amazon Bedrock |
| Hardware | Lenovo Legion (Windows) |

---

## Modules

### 🎯 `polymarket/jarvis_polymarket.py`
Fetches trending or keyword-searched Polymarket markets, analyzes them with Claude, and returns a Telegram-ready briefing.

**Features:**
- Live market data via Polymarket Gamma API (no API key required)
- Claude-powered analysis — spots mispriced odds, high-volume signals, time-sensitive opportunities
- CLI flags for trending, keyword search, and raw data output

**Usage:**
```bash
# Analyze trending markets
python jarvis_polymarket.py

# Search by keyword
python jarvis_polymarket.py --search "FIFA"

# Raw data only (no Claude)
python jarvis_polymarket.py --raw

# Control number of markets
python jarvis_polymarket.py --limit 10
```

**Requirements:**
```bash
pip install anthropic requests
```

Set your Anthropic API key:
```bash
export ANTHROPIC_API_KEY=sk-ant-...   # Mac/Linux
$env:ANTHROPIC_API_KEY="sk-ant-..."   # Windows PowerShell
```

---

## Setup

### 1. Install OpenClaw
```bash
npm install -g openclaw
```

### 2. Install PolyClaw (Chainstack)
```bash
cd ~/.openclaw/workspace/skills
git clone https://github.com/chainstacklabs/polyclaw polyclaw-chainstack
cd polyclaw-chainstack
uv sync
```

### 3. Test PolyClaw CLI
```bash
# Trending markets
uv run python scripts/polyclaw.py markets trending

# Search
uv run python scripts/polyclaw.py markets search "election"
```

### 4. Run Jarvis analysis script
```bash
python polymarket/jarvis_polymarket.py
```

---

## Roadmap

- [ ] Telegram integration — auto-send morning briefing via Jarvis
- [ ] Scheduled runs — daily cron for market summary
- [ ] x402 integration — expose analysis as a paid API endpoint
- [ ] Anomaly alerts — notify when a market moves >20% in an hour
- [ ] Amazon Bedrock AgentCore — migrate Jarvis to AWS with native x402 support

---

## Notes

- Polymarket Gamma API is fully public — no auth required for read access
- PolyClaw trading features require a Polygon wallet + Chainstack RPC node
- All scripts in this repo are for research and educational purposes

---

## Author

Alberto Montilla — Miami, FL  
Applied AI @ Miami Dade College  
Building at the intersection of AI agents, prediction markets, and Web3 payments.
