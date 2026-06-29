"""
jarvis_learn.py
===============
Análisis de performance del paper trading de Jarvis.
Genera un reporte detallado de lo que Jarvis ha aprendido.

Uso: python jarvis_learn.py [--market <id>]
"""

import os
import json
import sqlite3
import argparse
from anthropic import Anthropic

DB_PATH = "paper_trades.db"

def get_full_stats(con, market_id=None):
    cur = con.cursor()
    where = f"WHERE market_id='{market_id}'" if market_id else ""

    trades = cur.execute(f"""
        SELECT
            t.id, t.market_id, t.side, t.entry_price, t.exit_price,
            t.size_usdc, t.pnl_usdc, t.pnl_pct, t.outcome,
            t.confidence, t.phase_open, t.phase_close, t.strategy_ver,
            t.rationale, t.ts_open, t.ts_close,
            m.question
        FROM trades t
        LEFT JOIN markets m ON t.market_id = m.id
        {where}
        ORDER BY t.ts_open DESC
        LIMIT 50
    """).fetchall()

    strategy_history = cur.execute("""
        SELECT version, ts, min_confidence, max_position, phase_weights, note
        FROM strategy ORDER BY version
    """).fetchall()

    phase_perf = cur.execute(f"""
        SELECT phase_open,
               COUNT(*) as n,
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
               AVG(pnl_pct)*100 as avg_pnl_pct,
               SUM(pnl_usdc) as total_pnl
        FROM trades {where} {"AND" if where else "WHERE"} outcome IN ('win','loss')
        GROUP BY phase_open
    """).fetchall()

    return trades, strategy_history, phase_perf


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", default=None)
    args = parser.parse_args()

    api_key = os.getenv("ANTHROPIC_API_KEY")
    client  = Anthropic(api_key=api_key)
    con     = sqlite3.connect(DB_PATH)

    trades, strat_history, phase_perf = get_full_stats(con, args.market)

    if not trades:
        print("⚠️  Sin trades registrados aún.")
        return

    trades_text = "\n".join([
        f"  #{t[0]} {(t[16] or 'N/A')[:40]} | {t[2]} @{t[3]:.2f}→"
        f"{('%.2f' % t[4]) if t[4] is not None else '?'} | "
        f"${t[5]:.1f} | PnL=${t[6] or 0:.2f} | {t[8]} | fase={t[10]} | conf={t[9]:.0%}"
        for t in trades
    ])

    phase_text = "\n".join([
        f"  {r[0]}: {r[1]} trades, {r[2]/r[1]*100:.0f}% win, avg_pnl={r[3]:.1f}%, total=${r[4]:.2f}"
        for r in phase_perf
    ]) or "  Sin datos"

    strat_text = "\n".join([
        f"  v{s[0]} [{s[1][:16]}]: conf>={s[2]:.0%}, pos<={s[3]:.0%} — {s[5]}"
        for s in strat_history
    ])

    prompt = f"""Eres el analista de aprendizaje de Jarvis, un sistema de paper trading en Polymarket.

Analiza los siguientes datos y genera un informe de aprendizaje en español.

TRADES RECIENTES:
{trades_text}

PERFORMANCE POR FASE:
{phase_text}

EVOLUCIÓN DE ESTRATEGIA:
{strat_text}

Genera un informe estructurado con:
1. Qué está funcionando bien
2. Qué está fallando y por qué
3. Patrones observados en el comportamiento del mercado durante el evento
4. Recomendaciones concretas para mejorar la estrategia
5. Puntuación general del sistema (1-10) con justificación

Sé directo y específico. Máximo 400 palabras."""

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}]
    )

    print("\n" + "="*60)
    print("🧠 INFORME DE APRENDIZAJE — JARVIS PAPER TRADING")
    print("="*60)
    print(resp.content[0].text)
    print("="*60)


if __name__ == "__main__":
    main()
