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
    # Baloncesto universitario masculino, División I. ESPN corta en 1.000 partidos por
    # consulta, así que se pide día a día; groups=50 = toda la División I.
    "ncaamb": {"espn": "basketball/mens-college-basketball", "kalshi": "KXNCAAMBGAME",
               "daily": True, "params": {"groups": "50"}, "season_months": (11, 12, 1, 2, 3, 4)},
}
