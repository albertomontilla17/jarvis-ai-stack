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

import jarvis_paper_trade as jpt
import jarvis_market_selector as jms

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_API     = "https://api.telegram.org"
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "7022147500")
GAMMA_API        = "https://gamma-api.polymarket.com"
HEADERS          = {"User-Agent": "jarvis-paper-trading/1.0"}

# Serie de Polymarket que agrupa todos los partidos del Mundial
WORLDCUP_SERIES_ID = "11433"

# Sufijos de tipos de mercado por partido. El slug base (sin sufijo) es el
# resultado (gana/empata/pierde). Los demás son props del partido.
MATCH_MARKET_SUFFIXES = [
    "",                  # resultado del partido (base)
    "-halftime-result",
    "-second-half-result",
    "-total-corners",
    "-first-to-score",
    # "-exact-score",    # multi-outcome, muchos sub-mercados — opcional
    # "-player-props",   # muchos jugadores — opcional, descomenta si lo quieres
]

# Si AUTO_DETECT_MATCHES=True, el runner busca los partidos de hoy solo.
# Si pasas --events manualmente, se usan esos en su lugar.
AUTO_DETECT_MATCHES = True

# Presupuesto por partido (USDC ficticios). Cada partido tiene su propio límite
# para que Jarvis no gaste todo en el primer partido que ve.
MATCH_BUDGET = 33.0

# Presupuesto por mercado de la watchlist (no-Mundial). Menor que el de partido
# porque la watchlist trae ~8 mercados independientes a la vez.
WATCHLIST_BUDGET = 15.0

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
TRADE_SCRIPT = os.path.join(SCRIPT_DIR, "jarvis_paper_trade.py")


# ── Auto-detección de partidos de hoy ─────────────────────────────────────────
def find_todays_matches() -> list:
    """
    Busca los partidos del Mundial de HOY vía la serie de Polymarket.
    Devuelve los slugs de eventos a trackear (resultado + props elegidas).
    """
    today = datetime.date.today().isoformat()  # YYYY-MM-DD
    try:
        r = requests.get(
            f"{GAMMA_API}/events",
            params={"series_id": WORLDCUP_SERIES_ID, "closed": "false", "limit": 200},
            headers=HEADERS, timeout=20
        )
        if not r.ok:
            print(f"⚠️  No se pudieron buscar partidos de hoy (status {r.status_code})")
            return []

        events = r.json()
        # Slugs base de partidos de hoy (sin sufijos): fifwc-XXX-YYY-FECHA
        import re
        base_pattern = re.compile(rf"^fifwc-[a-z]{{3}}-[a-z]{{3}}-{today}$")
        base_matches = sorted({
            e.get("slug") for e in events
            if e.get("slug") and base_pattern.match(e.get("slug"))
        })

        if not base_matches:
            print(f"ℹ️  No hay partidos del Mundial hoy ({today})")
            return []

        # Para cada partido base, agregar los tipos de mercado elegidos
        to_track = []
        for base in base_matches:
            for suffix in MATCH_MARKET_SUFFIXES:
                to_track.append(base + suffix)

        print(f"⚽ {len(base_matches)} partido(s) hoy: {', '.join(b.split('-')[1]+'-'+b.split('-')[2] for b in base_matches)}")
        return to_track

    except Exception as e:
        print(f"⚠️  Error buscando partidos: {e}")
        return []


# ── Expansión de eventos en sub-mercados ──────────────────────────────────────
def expand_event(slug: str) -> list:
    """
    Si 'slug' es un evento (partido), devuelve los slugs de sus sub-mercados.
    Si es un market directo o no se encuentra como evento, lo devuelve tal cual.
    Filtra placeholders (will-team-XX) y mercados sin volumen real.
    """
    import re
    try:
        r = requests.get(f"{GAMMA_API}/events/slug/{slug}", headers=HEADERS, timeout=15)
        if r.ok:
            data = r.json()
            markets = data.get("markets", []) if isinstance(data, dict) else []
            sub_slugs = []
            for m in markets:
                s = m.get("slug")
                if not s or m.get("closed"):
                    continue
                # Filtrar placeholders: will-team-am, will-team-ai, etc. (equipos sin asignar)
                if re.search(r"will-team-[a-z]{2}-", s):
                    continue
                # Filtrar el catch-all "any other team" con lista larga de IDs
                if "will-any-other-team" in s:
                    continue
                # Filtrar mercados sin volumen real
                vol = float(m.get("volume", 0) or 0)
                if vol < 1000:  # menos de $1k de volumen = ilíquido / placeholder
                    continue
                sub_slugs.append(s)
            if sub_slugs:
                return sub_slugs
    except Exception as e:
        print(f"⚠️  No se pudo expandir '{slug}' como evento: {e}")
    # No es evento o falló → tratarlo como market directo
    return [slug]


def resolve_markets(events: list) -> list:
    """
    Expande una lista de eventos/slugs en la lista final de markets a correr.
    Devuelve pares (slug, parent_event): todos los sub-mercados de un mismo
    evento comparten el parent, y con él el presupuesto (--match-id). Sin esto,
    7 props de la misma pelea/partido recibirían 7 presupuestos independientes
    pese a estar totalmente correlacionados.
    """
    resolved = []
    for ev in events:
        subs = expand_event(ev)
        if len(subs) > 1 or subs[0] != ev:
            print(f"📂 {ev} → {len(subs)} mercado(s)")
        resolved.extend((s, ev) for s in subs)
    # Deduplicar preservando orden
    seen = set()
    out = []
    for pair in resolved:
        if pair[0] not in seen:
            seen.add(pair[0])
            out.append(pair)
    return out


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
def run_market(slug: str, budget: float = MATCH_BUDGET, match_id: str = None) -> dict:
    """
    Corre jarvis_paper_trade.py para un mercado y captura el resultado.
    Retorna dict con slug, ok, y un resumen extraído del output.
    """
    try:
        # Forzar UTF-8 en el hijo: Windows usa cp1252 y los emojis (📋📈) lo rompen
        child_env = os.environ.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"

        cmd = [sys.executable, TRADE_SCRIPT, "--market", slug, "--once",
               "--match-budget", str(budget)]
        if match_id:
            cmd += ["--match-id", match_id]
        result = subprocess.run(
            cmd,
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
            elif "⛔" in line or "Bloqueado" in line:
                summary["blocked"] = line.replace("⛔", "").strip()
            elif "⏭️" in line or "saltando" in line:
                summary["skipped"] = True

        if not ok:
            summary["error"] = result.stderr.strip()[-300:]  # últimos 300 chars

        return summary

    except subprocess.TimeoutExpired:
        return {"slug": slug, "ok": False, "error": "Timeout (>120s)"}
    except Exception as e:
        return {"slug": slug, "ok": False, "error": str(e)}


# ── Formato del resumen para Telegram ─────────────────────────────────────────
def format_summary(results: list, settled: list = None, watchlist: list = None) -> str:
    ts = datetime.datetime.now().strftime("%H:%M")
    lines = [f"🤖 Jarvis Paper Trading — {ts}", ""]

    # Solo se incluye cuando la selección es NUEVA (una vez al día), para que
    # el usuario vea el porqué de cada mercado sin repetirlo en cada corrida.
    if watchlist:
        lines.append(f"📡 Watchlist nueva de hoy ({len(watchlist)} mercados):")
        for w in watchlist:
            lines.append(f"   #{w['priority']} [{w['category']}] {w['question'][:60]}")
            lines.append(f"      {w['edge_type']}: {w['reason']}")
        lines.append("")

    if settled:
        total_pnl = sum(s["pnl"] for s in settled)
        lines.append(f"🧾 {len(settled)} posición(es) liquidada(s) (mercados ya resueltos) | PnL: ${total_pnl:.2f}")
        for s in settled:
            emoji = "🟢" if s["outcome"] == "win" else "🔴"
            lines.append(f"   {emoji} #{s['trade_id']} {s['question'][:40]} | {s['side']} @{s['entry']:.2f}→{s['exit']:.2f} | ${s['pnl']:.2f}")
        lines.append("")

    for r in results:
        if not r.get("ok"):
            lines.append(f"❌ {r['slug']}")
            if r.get("error"):
                lines.append(f"   Error: {r['error'][:150]}")
            lines.append("")
            continue

        # Omitir del resumen los mercados resueltos/saltados (reducir ruido)
        if r.get("skipped"):
            continue

        q = r.get("question", r["slug"])
        lines.append(f"📋 {q}")
        if r.get("prices"):
            lines.append(f"   {r['prices']}")
        if r.get("decision"):
            lines.append(f"   Decisión: {r['decision']}")
        if r.get("trade"):
            lines.append(f"   ✅ {r['trade']}")
        if r.get("blocked"):
            lines.append(f"   ⛔ {r['blocked']}")
        for c in r.get("closed", []):
            lines.append(f"   {c}")
        lines.append("")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Jarvis Paper Trading Runner (multi-mercado)")
    parser.add_argument("--events", nargs="+", default=None,
                        help="Slugs de eventos (partidos) o markets. Si se omite, "
                             "auto-detecta los partidos de hoy. Un evento se expande "
                             "en sus sub-mercados automáticamente.")
    parser.add_argument("--markets", nargs="+", default=None,
                        help="(alias de --events, mismo comportamiento)")
    parser.add_argument("--no-telegram", action="store_true",
                        help="No enviar a Telegram (solo consola)")
    args = parser.parse_args()

    print("🔧 Revisando posiciones abiertas de mercados ya resueltos...")
    con = jpt.init_db()
    settled = jpt.settle_resolved_positions(con)
    if settled:
        total_pnl = sum(s["pnl"] for s in settled)
        print(f"✅ {len(settled)} posición(es) liquidada(s) | PnL total: ${total_pnl:.2f}\n")
    else:
        print("ℹ️  Nada que liquidar.\n")

    # WATCHLIST: la fuente principal de mercados. El selector filtra el universo
    # de Polymarket con criterios duros y Claude elige una watchlist diversificada
    # (1 llamada al día — cacheada por fecha). Los partidos del Mundial compiten
    # por cupo en la watchlist vía volumen, igual que cualquier otro mercado.
    watchlist, fresh_watchlist = [], False
    events = args.events or args.markets
    if not events:
        try:
            from anthropic import Anthropic
            client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
            watchlist, fresh_watchlist = jms.ensure_watchlist(con, client)
        except Exception as e:
            print(f"⚠️  Selector de mercados falló: {e}")

        if watchlist:
            tag = "NUEVA selección" if fresh_watchlist else "selección de hoy (cacheada)"
            print(f"📡 Watchlist — {tag}, {len(watchlist)} mercado(s):")
            for w in watchlist:
                print(f"   #{w['priority']} [{w['category']}] {w['question'][:60]} — {w['edge_type']}")
            events = [w["slug"] for w in watchlist]
        elif AUTO_DETECT_MATCHES:
            # Fallback: si el selector no produjo nada, volver al comportamiento
            # anterior (partidos del Mundial de hoy).
            print("🔎 Watchlist vacía — auto-detectando partidos de hoy...")
            events = find_todays_matches()
            if not events:
                print("⚠️  No se detectaron partidos. Usa --events para especificar manualmente.")
                con.close()
                return
        else:
            print("⚠️  Sin watchlist y auto-detección desactivada.")
            con.close()
            return
    con.close()

    print(f"🔍 Resolviendo {len(events)} evento(s)/slug(s)...")
    markets = resolve_markets(events)
    print(f"🚀 Corriendo {len(markets)} mercado(s) en total\n")

    results = []
    for slug, parent in markets:
        print(f"── {slug} ──")
        budget = MATCH_BUDGET if slug.startswith("fifwc-") else WATCHLIST_BUDGET
        r = run_market(slug, budget, match_id=parent)
        results.append(r)
        status = "✅" if r.get("ok") else "❌"
        print(f"{status} {slug}: {r.get('decision', r.get('error', 'sin info'))}\n")

    summary = format_summary(results, settled, watchlist if fresh_watchlist else None)
    print("\n" + "="*50)
    print(summary)

    if not args.no_telegram:
        if send_telegram(summary):
            print("✅ Resumen enviado a Telegram")
        else:
            print("⚠️  No se pudo enviar a Telegram")


if __name__ == "__main__":
    main()
