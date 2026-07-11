# jarvis-ai-stack

Personal AI agent stack built by Alberto Montilla — Applied AI student at Miami Dade College.

Jarvis is a personal AI assistant running on [OpenClaw](https://github.com/openclaw/openclaw), powered by Claude, connected to Telegram. This repo documents the tools, scripts, and integrations built on top of it.

---

## Stack Overview

| Layer | Tool |
|---|---|
| AI Agent | OpenClaw + Claude Sonnet 4.6 |
| Messaging | Telegram (@Alberto_Montilla_claw_bot) |
| Prediction Markets | Polymarket via PolyClaw (Chainstack) |
| Wallet | Coinbase Wallet (Polygon) — 42 USDC.e funded |
| RPC | Chainstack Global Node (Polygon Mainnet) |
| Payments | x402 protocol on Base (L2) — coming soon |
| Infra | AWS / Amazon Bedrock — coming soon |
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
python jarvis_polymarket.py                  # trending markets + Telegram
python jarvis_polymarket.py --search "FIFA"  # search by keyword
python jarvis_polymarket.py --raw            # raw data only
python jarvis_polymarket.py --no-telegram    # skip Telegram
python jarvis_polymarket.py --limit 10       # control market count
```

---

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

### 🤖 `jarvis_trade.py`
Autonomous trading module. Claude analyzes Polymarket opportunities and executes trades automatically via PolyClaw CLOB. Conservative by design — starts at $2/trade.

**Features:**
- Claude analyzes 20 markets and picks the best opportunity
- Only trades when confidence >= 75%
- Dry-run mode for testing without real money
- Sends trade confirmation or "no opportunity" message to Telegram
- Hard cap of $10 per trade

**Usage:**
```bash
python jarvis_trade.py              # auto mode — Claude decides and trades
python jarvis_trade.py --dry-run    # simulate only, no real trades
python jarvis_trade.py --amount 5   # set trade size (default $2)
python jarvis_trade.py --limit 30   # analyze more markets
```

**Environment variables required:**
```bash
ANTHROPIC_API_KEY
TELEGRAM_TOKEN
POLYCLAW_PRIVATE_KEY    # Coinbase Wallet private key
CHAINSTACK_NODE         # Polygon RPC URL
```

---

### 🧠 `jarvis_paper_trade.py` — motor de paper trading (Kelly + debate)
Motor de decisión unificado. Por cada mercado: detecta la fase (early/mid/late),
corre un debate de 3 llamadas a Claude (investigador Bull, investigador Bear, y
Risk-Manager con poder de veto), y dimensiona la posición con **Kelly Criterion
fraccionado** (medio-Kelly). Guardrails forzados en código, no en el prompt:

- `SKIP_LOW_CONFIDENCE` — confianza por debajo del umbral de la estrategia
- `SKIP_NO_EDGE` — Kelly no ve edge (confidence <= precio de entrada) ⇒ no se apuesta
- Techo duro `max_position` y guard de precio máximo de entrada (0.95)
- Presupuesto compartido por evento (`--match-id`): todos los props de un mismo
  partido/pelea comparten un solo límite — no se puede "diversificar" en 7
  mercados correlacionados del mismo evento
- **Liquidación automática** (`settle_resolved_positions`): al inicio de cada
  ciclo revisa las posiciones abiertas contra la API de CLOB (que sí conserva
  mercados resueltos, a diferencia de Gamma) y las cierra al precio final real

```bash
python jarvis_paper_trade.py --market <slug> --once --match-budget 15 --match-id <evento>
```

### 📡 `jarvis_market_selector.py` — selector de mercados en 2 etapas
Decide QUÉ mercados vale la pena seguir, con justificación explícita.

1. **Filtro duro en código** (sin costo de API): universo de Polymarket por
   volumen 24h, filtrado por liquidez mínima, ventana de resolución de 2-30 días
   y precio no decidido (0.05-0.95).
2. **Selección razonada con Claude** (1 llamada/día, cacheada por fecha): elige
   una watchlist diversificada (máx. 2 mercados por evento subyacente) y guarda
   el porqué de cada elección y el tipo de edge esperado (`mispricing`,
   `information`, `event-volatility`, `time-decay`) en la tabla `watchlist` —
   así después se puede evaluar no solo los trades sino las selecciones.

```bash
python jarvis_market_selector.py            # watchlist de hoy (crea si no existe)
python jarvis_market_selector.py --refresh  # forzar nueva selección
python jarvis_market_selector.py --dry-run  # solo candidatos, sin Claude
```

### 🔁 `jarvis_paper_runner.py` — orquestador para Task Scheduler
Cada ciclo: liquida posiciones resueltas → obtiene la watchlist del día →
expande cada mercado/evento en sus sub-mercados → corre el motor sobre cada uno
con presupuesto por evento → manda resumen consolidado a Telegram (incluye las
justificaciones de la watchlist el día que la selección es nueva).

### 🔑 `setup_polymarket_creds.py`
Generates Polymarket CLOB API credentials (apiKey, secret, passphrase) from your wallet private key. Run once to set up trading access.

**Usage:**
```bash
$env:POLYGON_PRIVATE_KEY="0x..."
$env:POLYGON_ADDRESS="0x2eEeaeE3F360c70Db75FB1d36ac8b9b97c58a675"
python setup_polymarket_creds.py
```

---

## Setup

### 1. Clone this repo
```bash
git clone https://github.com/albertomontilla17/jarvis-ai-stack.git
cd jarvis-ai-stack
```

### 2. Install dependencies
```bash
pip install anthropic requests py-clob-client-v2
```

### 3. Install OpenClaw + clawhub
```bash
npm install -g openclaw clawhub
```

### 4. Install PolyClaw (Chainstack)
```bash
cd %USERPROFILE%\.openclaw\workspace\skills
git clone https://github.com/chainstacklabs/polyclaw polyclaw-chainstack
cd polyclaw-chainstack
uv sync
```

### 5. Get a Chainstack RPC node
Sign up at [chainstack.com](https://chainstack.com), create a Polygon Mainnet Global Node, and copy the HTTPS endpoint.

### 6. Set environment variables
Create your `.bat` files with:
```bat
@echo off
set ANTHROPIC_API_KEY=your-key
set TELEGRAM_TOKEN=your-token
set POLYCLAW_PRIVATE_KEY=0x...
set CHAINSTACK_NODE=https://polygon-mainnet.core.chainstack.com/...
set POLYGON_ADDRESS=0x...
cd C:\path\to\jarvis-ai-stack
python jarvis_polymarket.py
```

### 7. Approve Polymarket contracts (one-time)
```bash
cd %USERPROFILE%\.openclaw\workspace\skills\polyclaw-chainstack
uv run python scripts/polyclaw.py wallet approve
```

### 8. Automate with Windows Task Scheduler
- Daily briefing: 12:00 PM → `run_jarvis_polymarket.bat`
- Hourly alerts: every 1 hour → `run_jarvis_alerts.bat`

---

## Roadmap

- [x] Polymarket data via Gamma API
- [x] Claude-powered market analysis in Spanish
- [x] Telegram delivery
- [x] Windows Task Scheduler — daily briefing at 12:00 PM
- [x] Anomaly alerts — hourly monitoring of 50 markets
- [x] Autonomous trading module with dry-run mode
- [x] Wallet funded — 42 USDC.e on Polygon
- [x] CLOB credentials generated
- [x] Chainstack RPC node configured
- [x] GitHub repo with local git workflow
- [x] Liquidación automática de posiciones al resolverse el mercado (vía CLOB API)
- [x] Kelly Criterion (medio-Kelly) para position sizing
- [x] Debate Bull / Bear / Risk-Manager (3 llamadas a Claude por decisión, con veto)
- [x] Selector de mercados en 2 etapas con watchlist diversificada y justificada
- [x] Presupuesto compartido por evento (props correlacionados no multiplican exposición)
- [x] Backtest contra mercados resueltos reales (muestra preliminar: 3 entradas en
      ~185 decisiones, direccionales correctas; Kelly vetó sistemáticamente entradas
      sin edge — vs. -$17.11 y 33% win rate del motor simple anterior)
- [ ] Completar backtest ampliado (~6 mercados pendientes)
- [ ] Wallet approve — pending (nonce reset needed)
- [ ] First live trade execution
- [ ] x402 integration — expose analysis as a paid API endpoint
- [ ] Amazon Bedrock AgentCore — migrate Jarvis to AWS

> **Estado (julio 2026):** proyecto en pausa — las 3 tareas de Task Scheduler
> están deshabilitadas. La infraestructura queda funcional; para retomar:
> recargar créditos de la API de Anthropic y re-habilitar las tareas.

---

## Notes

- Polymarket Gamma API is fully public — no auth required for read access
- Trading requires USDC.e on Polygon + POL for gas + Chainstack RPC
- Polymarket browser trading is restricted in USA — use PolyClaw CLI instead
- Never commit private keys or `.bat` files to GitHub
- All scripts are for research and educational purposes

---

## Author

Alberto Montilla — Miami, FL
Applied AI @ Miami Dade College
Building at the intersection of AI agents, prediction markets, and Web3 payments.
