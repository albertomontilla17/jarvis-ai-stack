"""
Analizador de estadísticas deportivas de Jarvis.

    data.py    Resultados de partidos (ESPN) + mercados de partido y precios reales (Kalshi).
    model.py   Modelo de probabilidad a partir de stats: Elo, descanso y racha reciente.
    evaluate.py  ¿El modelo le gana al precio del mercado? Con costos y sin ver el futuro.

Uso:  python -m sports --help
"""

LEAGUES = {
    "nba": {"espn": "basketball/nba", "kalshi": "KXNBAGAME"},
    "nhl": {"espn": "hockey/nhl", "kalshi": "KXNHLGAME"},
    "nfl": {"espn": "football/nfl", "kalshi": "KXNFLGAME"},
    "mlb": {"espn": "baseball/mlb", "kalshi": "KXMLBGAME"},
}
