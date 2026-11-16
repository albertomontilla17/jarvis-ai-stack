"""
Backtester de Jarvis para Polymarket, construido por piezas independientes:

    data.py        Pieza 1 — descarga mercados RESUELTOS + historial de precios a SQLite.
    engine.py      Pieza 2 — simulador sin lookahead, con costos (spread + comisiones).
    strategies.py  Pieza 3 — estrategias base sin LLM (gratis, miles de mercados).
    report.py      Pieza 4 — métricas con intervalos de confianza y calibración.

Uso:  python -m backtest --help
"""
