"""
Analizador de temperatura máxima diaria (mercados KXHIGH* de Kalshi).

    data.py      Pronósticos oficiales del NWS (National Blend of Models, vía el archivo
                 de la Iowa Environmental Mesonet) + mercados de Kalshi, su liquidación
                 y el bid/ask real en el momento de decidir.
    model.py     Probabilidad de cada rango de temperatura a partir del pronóstico NBM
                 (máxima y su desviación estándar), calibrada con días anteriores.
    evaluate.py  ¿El modelo le gana al precio? Fuera de muestra, con costos.

Uso:  python -m weather --help
"""

# serie de Kalshi → (estación ICAO del informe climático oficial, zona horaria)
CITIES = {
    "KXHIGHNY": ("KNYC", "America/New_York"),      # Central Park
    "KXHIGHLAX": ("KLAX", "America/Los_Angeles"),
    "KXHIGHMIA": ("KMIA", "America/New_York"),
    "KXHIGHTPHX": ("KPHX", "America/Phoenix"),
    "KXHIGHAUS": ("KAUS", "America/Chicago"),
    "KXHIGHCHI": ("KMDW", "America/Chicago"),       # Midway
    "KXHIGHDEN": ("KDEN", "America/Denver"),
}

# Momentos de decisión (hora UTC) y la corrida NBM que se usa: siempre una ya publicada.
#   "dia_antes": a las 15Z del día anterior, con la corrida de las 12Z de ese día.
#   "mismo_dia": a las 15Z del mismo día (mañana en EE. UU.), con la corrida de las 12Z.
DECISIONS = {"dia_antes": (-1, 15, 12), "mismo_dia": (0, 15, 12)}
