# Bot de trading — SOLO PAPER (Alpaca)

Bot de riesgo medio que opera **solo con dinero ficticio** (cuenta Paper de Alpaca).
Se ejecuta en GitHub Actions: no necesita tu ordenador encendido.

## Qué hace
**Acciones/ETFs (83% de la cuenta)** — una vez al día con la bolsa de EE. UU. abierta:
- Universo de 24 activos líquidos (SPY, QQQ, GLD, TLT, AAPL, MSFT, NVDA…).
- Cada 5 sesiones elige los 6 con mejor tendencia (1, 3 y 6 meses) que estén por
  encima de su media de 100 días. Pesa menos a los más volátiles. Máx. 22% por posición.
- Stop dinámico: vende si una posición cae un 10% desde su máximo.

**Cripto (máx. 12% de la cuenta)** — cada 4 horas, 24/7:
- BTC, ETH y SOL. Elige los 2 con mejor tendencia por encima de su media de 50 días.
- Rebalancea una vez al día; stop dinámico del 15%.

**Freno de emergencia:** si la cuenta cae un 15% desde su máximo, vende todo y se
pausa 28 días.

**Seguridad:** se niega a funcionar si la clave no es de Paper (`PK…`) o si la cuenta
no es Paper. Nunca toca dinero real.

## Dónde ver los resultados
- `registros/resumen.txt` — estado actual de la cartera.
- `registros/operaciones.csv` — historial de compras y ventas.
- `registros/bot.log` — registro detallado.
- Pestaña **Actions** del repositorio — cada ejecución.
- Web de Alpaca con la vista *Paper Trading*.

## Ejecutarlo a mano
Pestaña **Actions → Bot de trading (Paper) → Run workflow** y elige `acciones` o `cripto`.

## Pararlo
Pestaña **Actions → Bot de trading (Paper) → ··· → Disable workflow**.

> Prueba con dinero ficticio. Resultados simulados no garantizan resultados futuros.
