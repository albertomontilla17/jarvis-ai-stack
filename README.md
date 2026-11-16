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

## Límites del ejecutor

Están en `.env` y se aplican en la computadora local, pase lo que pase en la nube:
máximo por trade, máximo diario, categorías permitidas, tolerancia de precio, caducidad de la
propuesta y una sola ejecución por propuesta. Las órdenes son de compra, tipo FOK con precio tope.

## Aviso

Operar solo en una plataforma en la que tengas permitido operar desde tu país de residencia.
Nada de esto es asesoría financiera; el backtest describe el pasado, no garantiza el futuro.
