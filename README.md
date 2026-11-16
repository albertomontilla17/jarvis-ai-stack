# jarvis-ai-stack

Bot de mercados de predicción (Polymarket) de Alberto Montilla, reconstruido por piezas:
primero se demuestra con datos que una estrategia gana, y solo después se le pone dinero.

## Arquitectura

```
 NUBE (sin claves, sin dinero)                    COMPUTADORA LOCAL (wallet)
 ┌────────────────────────────┐   propuesta      ┌──────────────────────────────┐
 │ jarvis_scan.py             │ ───────────────► │ jarvis_execute.py            │
 │ tarea programada: busca    │   tú apruebas    │ valida límites duros propios │
 │ señales y mide resultados  │                  │ firma y envía la orden       │
 └────────────────────────────┘                  └──────────────────────────────┘
```

| Pieza | Archivo | Qué hace |
|---|---|---|
| 1. Datos | `backtest/data.py` | Baja mercados **resueltos** y su historial horario a `backtest_data.db` (sin API key) |
| 2. Simulador | `backtest/engine.py` | Reproduce cada mercado barra a barra, sin ver el futuro, con spread y comisiones |
| 3. Estrategias | `backtest/strategies.py` | Reglas base (favorito, longshot, momentum, reversión) y un control aleatorio |
| 4. Métricas | `backtest/report.py` | ROI por $ apostado con IC 95% (bootstrap por evento), dentro/fuera de muestra, calibración |
| 5. Escáner | `jarvis_scan.py` | Aplica la estrategia a mercados en vivo, genera propuestas y mide el resultado real hacia adelante |
| 6. Ejecutor | `jarvis_execute.py` | En la computadora local: valida límites y ejecuta la propuesta aprobada. Simula por defecto |

## Uso

```bash
pip install -r requirements.txt

# Backtest
python -m backtest download --from 2026-06-01 --to 2026-11-01 --sample 5000
python -m backtest prices --categories finance_prices economics --sample 2000
python -m backtest calibration --category sports
python -m backtest run --category finance_prices economics

# En vivo (sin dinero)
python jarvis_scan.py              # genera proposals/<id>.json
python jarvis_scan.py --settle     # resultado acumulado de las propuestas ya resueltas

# Computadora local
python jarvis_execute.py --order proposals/<id>.json          # simulación
python jarvis_execute.py --order proposals/<id>.json --live   # orden real
python jarvis_execute.py --status

# Tests
python -m unittest discover tests
```

## Claves

Copia `.env.example` a `.env` **solo en la computadora local** y rellena los valores marcados
con `REEMPLAZA`. El escáner y el backtest no necesitan ninguna clave.

## Resultados del backtest (mercados resueltos jun–oct 2026, volumen ≥ $50k)

Costos asumidos: 1 centavo de medio spread + la comisión de cada mercado.

**Deportes (6.480 mercados): ninguna estrategia gana.** Todas pierden entre 5% y 22% por dólar
apostado, también fuera de muestra. El control aleatorio pierde 5%, que es el costo de operar.

**Finanzas y economía (756 mercados): una pista, no una prueba.**

| Estrategia | Trades | ROI por $ | IC 95% |
|---|---|---|---|
| Reversión 10 pts / 6 h | 495 | +15,9% | +2,6% a +29,3% |
| Momentum 10 pts / 6 h | 496 | −11,7% | −20,1% a −2,7% |
| Favorito 80–95 | 267 | −4,5% | −10,4% a +0,9% |

La reversión (apostar contra un movimiento brusco de 10 puntos en 6 horas) da ~+16% en las dos
mitades del periodo y se mantiene positiva con otros parámetros (5/10/15 puntos, 3/6/12 horas).
Pero depende del spread real: con 3 centavos de medio spread baja a +9% y el intervalo ya incluye
el cero; con 5 centavos queda en +4%. El historial solo trae precio medio, no el libro de órdenes,
así que el backtest no puede resolver esa duda.

Por eso `jarvis_scan.py` registra el precio **real** de compra de cada señal y descarta mercados
con spread mayor a 4 centavos. Criterio para pasar a dinero real: al menos 100 propuestas
resueltas con ROI positivo en `python jarvis_scan.py --settle`.

## Analizador de stats deportivos (`sports/`) contra precios de Kalshi

```bash
python -m sports download     # resultados de ESPN + mercados de partido y bid/ask real de Kalshi
python -m sports evaluate     # ¿el modelo de stats le gana al mercado?
```

| Archivo | Qué hace |
|---|---|
| `sports/data.py` | Baja resultados (ESPN, desde 2021) y los mercados "quién gana" de Kalshi con su bid/ask 30+ min antes del partido |
| `sports/model.py` | Probabilidad de victoria a partir de Elo (con margen y ventaja de local), descanso y racha reciente |
| `sports/evaluate.py` | Compara el modelo con el precio: precisión, si aporta información nueva y si gana dinero pagando ask + comisión |

Resultado con partidos que el modelo no vio al ajustarse (abril 2025 – noviembre 2026):

| Liga | Partidos | Log-loss modelo | Log-loss mercado | ¿Aporta sobre el precio? | ROI apostando con ventaja ≥ 5 pts |
|---|---|---|---|---|---|
| NFL | 429 | 0,635 | 0,611 | No (z = −0,1) | −0,8% (−17,6% a +17,0%) |
| NBA | 1.588 | 0,603 | 0,575 | No (z = −0,6) | −13,2% (−22,2% a −3,7%) |
| NHL | 1.805 | 0,684 | 0,672 | No (z = −2,0, en contra) | −14,5% (−23,1% a −5,8%) |

El precio de Kalshi predice mejor que este modelo en las tres ligas, y el modelo no añade nada
que el precio no contenga ya. Cuando el modelo "ve" una ventaja, casi siempre es porque le falta
información que el mercado sí tiene (lesiones, alineaciones, descansos de estrellas).

### Analizador activo (registro en vivo, sin dinero)

```bash
python -m sports live                          # modelo + bajas + precio de Kalshi de los partidos próximos
python -m sports adjust <gid> 0.62 "motivo"    # probabilidad ajustada del local (lesiones, alineaciones)
python -m sports results                       # mercado vs modelo base vs modelo ajustado
```

Cada corrida guarda en `sports_log.db` el pronóstico previo a cada partido de NBA, NHL y NFL.
Los ajustes solo se aceptan antes del inicio. Criterio para pasar a dinero real: 200+ partidos
terminados en los que el modelo ajustado tenga menor log-loss que el mercado y ROI con IC > 0.

## Paper trading (`paper.py`)

Convierte los registros de los escáneres en operaciones simuladas al precio real de compra
(ask + comisión) previo al evento, con $1.000 ficticios y $20 por operación en cada estrategia:

| Estrategia | Regla |
|---|---|
| `finanzas_reversion` | cada propuesta de `jarvis_scan.py` (Polymarket) |
| `deportes_modelo` | modelo base, si su prob. supera el ask + comisión en 5+ puntos (Kalshi) |
| `deportes_ajustado` | modelo ajustado por bajas, mismo umbral (Kalshi) |
| `deportes_favorito` | control: siempre el favorito del mercado; mide el costo de operar sin ventaja |

```bash
python paper.py     # sincroniza operaciones y muestra PnL, ROI con IC 95% y caída máxima
```

Las decisiones deportivas se rehacen con cada nuevo precio hasta que empieza el partido; desde
ese momento quedan congeladas.

## Límites del ejecutor

Están en `.env` y se aplican en la computadora local, pase lo que pase en la nube:
máximo por trade, máximo diario, categorías permitidas, tolerancia de precio, caducidad de la
propuesta y una sola ejecución por propuesta. Las órdenes son de compra, tipo FOK con precio tope.

## Aviso

Operar solo en una plataforma en la que tengas permitido operar desde tu país de residencia.
Nada de esto es asesoría financiera; el backtest describe el pasado, no garantiza el futuro.
