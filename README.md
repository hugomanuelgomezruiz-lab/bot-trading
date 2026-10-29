# Bot intradía AGRESIVO — SOLO PAPER (Alpaca)

Opera **largos y cortos** dentro del día con **dinero ficticio** (cuenta Paper).
Funciona en GitHub Actions, ajustado a los **2.000 minutos gratis** al mes.

## Cómo opera
- **Una sesión al día de ~70 min**, normalmente justo tras la apertura de EE. UU.
  (la parte más movida del día). Si GitHub se retrasa, la hace más tarde.
- **Universo:** NVDA, TSLA, AMD, META, AAPL, AMZN, MSFT, GOOGL, NFLX, AVGO, PLTR, COIN, QQQ, SPY.
- **Señal:** rotura del rango de los 15 primeros minutos (o de los últimos 30 si
  empieza tarde), confirmada por el VWAP del valor y por la dirección del SPY.
  - Rompe al alza + sobre su VWAP + SPY alcista → **LARGO**.
  - Rompe a la baja + bajo su VWAP + SPY bajista → **CORTO**.
- **Órdenes bracket:** stop y objetivo puestos en el bróker desde el primer
  segundo. Stop = media anchura del rango (0,35%–1,5%); objetivo = 2× el stop.
- **Riesgo:** 1,5% de la cuenta por operación, hasta 4 posiciones, exposición
  total hasta 2× la cuenta (apalancado), máx. 50% en una sola posición.
- **Cierra todo** al final de la ventana: nunca duerme con posiciones.
- Si el día pierde un 5%, cierra todo y para hasta el día siguiente.

## Resultados
- `registros/resumen.txt` — resultado acumulado y de los últimos días.
- `registros/diario.csv` — resultado de cada día.
- `registros/operaciones_intradia.csv` — cada entrada con su stop y objetivo.
- `registros/intradia.log` — registro detallado.

## Manejo
- Lanzar a mano: **Actions → Bot intradia agresivo (Paper) → Run workflow**.
- Pararlo: **Actions → ··· → Disable workflow**.
- Parámetros: `senales.py`.

> Dinero ficticio. Las fills en Paper son más optimistas que en real; ninguna
> estrategia garantiza beneficios.
