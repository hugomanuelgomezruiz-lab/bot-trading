"""Lógica pura de la estrategia (sin llamadas a la API), para poder probarla aparte.

Estrategia "momentum con filtro de tendencia" - riesgo medio:
  * Universo de ETFs amplios + grandes empresas de EE. UU. muy líquidas.
  * Puntuación de momentum = 20% rentab. 1 mes + 40% 3 meses + 40% 6 meses.
  * Solo se compran activos con momentum > 0 y precio por encima de su media de 100 días.
  * Se eligen los 6 mejores, ponderados por volatilidad inversa (lo más volátil pesa menos),
    con un máximo del 22% por posición y el 83% de la cuenta en este bloque.
  * Bloque cripto (24/7): BTC, ETH y SOL con la misma lógica (media de 50 días),
    máximo 12% de la cuenta, 2 posiciones y stop del 15%.
  * Si menos de 6 activos pasan el filtro, la parte sobrante se queda en efectivo
    (en mercados bajistas el bot se protege solo quedándose en liquidez).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# ---------------------------------------------------------------- parámetros
UNIVERSO = [
    # ETFs (índices, sectores, bonos, oro)
    "SPY", "QQQ", "IWM", "DIA", "EFA", "EEM", "TLT", "IEF", "GLD",
    "XLK", "XLV", "XLF", "XLE", "XLI",
    # Grandes empresas
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "JPM", "AVGO", "COST", "LLY",
]
NUM_POSICIONES = 6
PESO_MAXIMO = 0.22          # máximo por posición
FRACCION_INVERTIDA = 0.83   # parte de la cuenta para acciones/ETFs (+12% cripto, 5% efectivo)
MEDIA_TENDENCIA = 100       # días de la media móvil de tendencia
VENTANA_VOLATILIDAD = 63
DIAS_ENTRE_REBALANCEOS = 5  # semanal (en sesiones de mercado)
STOP_DINAMICO = 0.10        # vende si cae un 10% desde el máximo alcanzado
FRENO_EMERGENCIA = False    # desactivado a petición del usuario (cuenta Paper)
DRAWDOWN_MAXIMO = 0.15      # solo se usa si FRENO_EMERGENCIA = True
DIAS_PAUSA = 28             # días naturales en pausa tras activar el freno de drawdown
OPERACION_MINIMA_USD = 50
DESVIO_MINIMO = 0.015       # no ajusta posiciones si el cambio es < 1,5% de la cuenta
MIN_HISTORIAL = 130         # sesiones mínimas de datos para valorar un activo


# ---------------------------------------------------------------- cripto (24/7)
UNIVERSO_CRIPTO = ["BTC/USD", "ETH/USD", "SOL/USD"]
CRIPTO_FRACCION = 0.12      # como mucho el 12% de la cuenta en cripto
CRIPTO_POSICIONES = 2
CRIPTO_STOP = 0.15          # stop dinámico más holgado (la cripto se mueve más)
CRIPTO_MEDIA = 50           # media de tendencia en días


def puntuar(cierres: pd.DataFrame, media_dias: int = MEDIA_TENDENCIA,
            anual: int = 252) -> pd.DataFrame:
    """cierres: índice fecha, una columna por símbolo. Devuelve tabla con métricas."""
    filas = []
    for simbolo in cierres.columns:
        s = cierres[simbolo].dropna()
        if len(s) < MIN_HISTORIAL:
            continue
        precio = s.iloc[-1]
        r21 = precio / s.iloc[-22] - 1
        r63 = precio / s.iloc[-64] - 1
        r126 = precio / s.iloc[-127] - 1
        momentum = 0.2 * r21 + 0.4 * r63 + 0.4 * r126
        media = s.iloc[-media_dias:].mean()
        vol = s.pct_change().iloc[-VENTANA_VOLATILIDAD:].std() * np.sqrt(anual)
        filas.append({
            "simbolo": simbolo, "precio": precio, "momentum": momentum,
            "media": media, "volatilidad": vol,
            "valido": bool(momentum > 0 and precio > media and vol > 0),
        })
    if not filas:
        return pd.DataFrame(columns=["simbolo", "precio", "momentum", "media", "volatilidad", "valido"]).set_index("simbolo")
    return pd.DataFrame(filas).set_index("simbolo").sort_values("momentum", ascending=False)


def pesos_objetivo(tabla: pd.DataFrame, excluidos: set[str] | None = None,
                   num: int = NUM_POSICIONES, fraccion: float = FRACCION_INVERTIDA,
                   maximo: float = PESO_MAXIMO) -> dict[str, float]:
    """Pesos de cartera (fracción de la cuenta) para cada símbolo elegido."""
    excluidos = excluidos or set()
    candidatos = tabla[tabla["valido"] & ~tabla.index.isin(list(excluidos))]
    elegidos = candidatos.head(num)
    if elegidos.empty:
        return {}
    inv_vol = 1.0 / elegidos["volatilidad"]
    # Cada hueco vale como máximo 1/num del capital de este bloque:
    # si solo hay 2 activos válidos de 6, solo se invierte 2/6 y el resto queda en liquidez.
    presupuesto = fraccion * len(elegidos) / num
    pesos = inv_vol / inv_vol.sum() * presupuesto
    # Recorta al máximo por posición sin redistribuir (el exceso queda en efectivo).
    pesos = pesos.clip(upper=maximo)
    return {s: float(round(w, 4)) for s, w in pesos.items()}


def ordenes_necesarias(objetivo: dict[str, float], valores_actuales: dict[str, float],
                       patrimonio: float) -> list[tuple[str, float]]:
    """Devuelve [(símbolo, delta_usd)]. Negativo = vender, positivo = comprar.
    Las ventas van primero."""
    simbolos = set(objetivo) | set(valores_actuales)
    deltas = []
    for s in simbolos:
        actual = valores_actuales.get(s, 0.0)
        deseado = objetivo.get(s, 0.0) * patrimonio
        delta = deseado - actual
        cierre_total = s not in objetivo and actual > 0
        if cierre_total or (abs(delta) >= OPERACION_MINIMA_USD and abs(delta) >= DESVIO_MINIMO * patrimonio):
            deltas.append((s, round(delta, 2)))
    deltas.sort(key=lambda x: x[1])  # ventas (negativas) primero
    return deltas


def actualizar_maximos(maximos: dict[str, float], precios: dict[str, float],
                       en_cartera: set[str]) -> dict[str, float]:
    nuevos = {}
    for s in en_cartera:
        p = precios.get(s)
        if p is None:
            if s in maximos:
                nuevos[s] = maximos[s]
            continue
        nuevos[s] = max(maximos.get(s, p), p)
    return nuevos


def stops_activados(maximos: dict[str, float], precios: dict[str, float],
                    stop: float = STOP_DINAMICO) -> list[str]:
    return [s for s, m in maximos.items()
            if s in precios and precios[s] <= m * (1 - stop)]


def drawdown(patrimonio: float, maximo: float) -> float:
    return 0.0 if maximo <= 0 else 1 - patrimonio / maximo
