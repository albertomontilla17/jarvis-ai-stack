"""
jarvis_paper_runner.py
======================
Wrapper para Task Scheduler. Corre el paper trading sobre varios mercados
en secuencia y manda un resumen a Telegram cada run.

Reusa el mismo método de Telegram que jarvis_alerts.py (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID).

Uso:
    python jarvis_paper_runner.py
    python jarvis_paper_runner.py --markets slug1 slug2 slug3
"""

import os
import io
import sys
import argparse
import datetime
import contextlib
import subprocess
import requests

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_API     = "https://api.telegram.org"
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "7022147500")

# Mercados por defecto (varios del Mundial). Edita esta lista o pasa --markets.
DEFAULT_MARKETS = [
    "world-cup-winner",
]

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
TRADE_SCRIPT = os.path.join(SCRIPT_DIR, "jarvis_paper_trade.py")


# ── Telegram (mismo método que jarvis_alerts.py) ──────────────────────────────
def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        print("⚠️  TELEGRAM_TOKEN not set — skipping Telegram.")
        return False

    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    # Telegram corta en 4096 chars; partimos por si acaso
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)] or [text]
    for chunk in chunks:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text":    chunk,
        }
        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code != 200:
                print(f"❌ Telegram error: {r.text}")
                return False
        except Exception as e:
            print(f"❌ Telegram exception: {e}")
            return False
    return True


# ── Run un mercado ────────────────────────────────────────────────────────────
def run_market(slug: str) -> dict:
    """
    Corre jarvis_paper_trade.py para un mercado y captura el resultado.
    Retorna dict con slug, ok, y un resumen extraído del output.
    """
    try:
        # Forzar UTF-8 en el hijo: Windows usa cp1252 y los emojis (📋📈) lo rompen
        child_env = os.environ.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"

        result = subprocess.run(
            [sys.executable, TRADE_SCRIPT, "--market", slug, "--once"],
            capture_output=True, text=True, timeout=120,
            cwd=SCRIPT_DIR,
            encoding="utf-8", errors="replace",
            env=child_env
        )
        output = result.stdout
        ok = result.returncode == 0

        # Extraer las líneas clave del output
        summary = {"slug": slug, "ok": ok}
        for line in output.split("\n"):
            line = line.strip()
            if line.startswith("📋"):
                summary["question"] = line.replace("📋", "").strip()
            elif line.startswith("📈"):
                summary["prices"] = line.replace("📈", "").strip()
            elif line.startswith("📌 Decisión:"):
                summary["decision"] = line.replace("📌 Decisión:", "").strip()
            elif "Trade #" in line and "abierto" in line:
                summary["trade"] = line.replace("✅", "").strip()
            elif "cerrado" in line and ("🟢" in line or "🔴" in line):
                summary.setdefault("closed", []).append(line.strip())

        if not ok:
            summary["error"] = result.stderr.strip()[-300:]  # últimos 300 chars

        return summary

    except subprocess.TimeoutExpired:
        return {"slug": slug, "ok": False, "error": "Timeout (>120s)"}
    except Exception as e:
        return {"slug": slug, "ok": False, "error": str(e)}


# ── Formato del resumen para Telegram ─────────────────────────────────────────
def format_summary(results: list) -> str:
    ts = datetime.datetime.now().strftime("%H:%M")
    lines = [f"🤖 Jarvis Paper Trading — {ts}", ""]

    for r in results:
        if not r.get("ok"):
            lines.append(f"❌ {r['slug']}")
            if r.get("error"):
                lines.append(f"   Error: {r['error'][:150]}")
            lines.append("")
            continue

        q = r.get("question", r["slug"])
        lines.append(f"📋 {q}")
        if r.get("prices"):
            lines.append(f"   {r['prices']}")
        if r.get("decision"):
            lines.append(f"   Decisión: {r['decision']}")
        if r.get("trade"):
            lines.append(f"   ✅ {r['trade']}")
        for c in r.get("closed", []):
            lines.append(f"   {c}")
        lines.append("")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Jarvis Paper Trading Runner (multi-mercado)")
    parser.add_argument("--markets", nargs="+", default=None,
                        help="Lista de slugs. Si se omite, usa DEFAULT_MARKETS.")
    parser.add_argument("--no-telegram", action="store_true",
                        help="No enviar a Telegram (solo consola)")
    args = parser.parse_args()

    markets = args.markets or DEFAULT_MARKETS

    print(f"🚀 Corriendo {len(markets)} mercado(s): {', '.join(markets)}\n")

    results = []
    for slug in markets:
        print(f"── {slug} ──")
        r = run_market(slug)
        results.append(r)
        status = "✅" if r.get("ok") else "❌"
        print(f"{status} {slug}: {r.get('decision', r.get('error', 'sin info'))}\n")

    summary = format_summary(results)
    print("\n" + "="*50)
    print(summary)

    if not args.no_telegram:
        if send_telegram(summary):
            print("✅ Resumen enviado a Telegram")
        else:
            print("⚠️  No se pudo enviar a Telegram")


if __name__ == "__main__":
    main()
