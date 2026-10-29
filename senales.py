"""Lógica pura del bot intradía (sin API), para poder probarla aparte.

Estrategia: rotura de rango con confirmación VWAP y filtro de mercado (SPY).
  * Rango de referencia: los primeros 15 min de la sesión (o, si la sesión ya
    va avanzada, los últimos 30 min antes de empezar).
  * LARGO si el precio rompe el máximo del rango, está por encima de su VWAP y
    el SPY también está por encima de su VWAP (mercado a favor).
  * CORTO si rompe el mínimo, está por debajo de su VWAP y el SPY también.
  * Se eligen las roturas más fuertes (distancia de rotura / anchura del rango).
  * Stop = media anchura del rango (entre 0,35% y 1,5% del precio); objetivo = 2x stop.
  * Riesgo por operación: 1,5% de la cuenta. Hasta 2x de exposición total.
"""
from __future__ import annotations

import math

import pandas as pd

# ---------------------------------------------------------------- parámetros
UNIVERSO = ["NVDA", "TSLA", "AMD", "META", "AAPL", "AMZN", "MSFT", "GOOGL",
            "NFLX", "AVGO", "PLTR", "COIN", "QQQ", "SPY"]
REFERENCIA = "SPY"
MINUTOS_RANGO_APERTURA = 15
MINUTOS_RANGO_TARDIO = 30
RIESGO_POR_OPERACION = 0.015   # 1,5% de la cuenta en riesgo por trade
EXPOSICION_MAXIMA = 2.0        # 2x la cuenta como mucho (largos + cortos)
POSICION_MAXIMA = 0.5          # una sola posición no supera el 50% de la cuenta
MAX_POSICIONES = 4
STOP_MIN_PCT = 0.0035
STOP_MAX_PCT = 0.015
RATIO_OBJETIVO = 2.0           # objetivo = 2 veces el stop
MARGEN_ROTURA = 0.0005         # el precio debe superar el rango un 0,05%
PERDIDA_DIARIA_MAX = 0.05      # si el día pierde un 5%, cierra todo y para
DURACION_SESION_MIN = 70       # minutos de operativa por día (presupuesto GitHub)
CIERRE_ANTES_MIN = 5           # cierra todo 5 min antes del fin de la ventana


def vwap(barras: pd.DataFrame) -> float:
    """barras: columnas high, low, close, volume (de hoy)."""
    tipico = (barras["high"] + barras["low"] + barras["close"]) / 3
    vol = barras["volume"].clip(lower=1)
    return float((tipico * vol).sum() / vol.sum())


def rango(barras: pd.DataFrame, desde: pd.Timestamp, hasta: pd.Timestamp) -> tuple[float, float] | None:
    tramo = barras[(barras.index >= desde) & (barras.index < hasta)]
    if len(tramo) < 5:
        return None
    return float(tramo["high"].max()), float(tramo["low"].min())


def senal(barras: pd.DataFrame, rango_ref: tuple[float, float], spy_alcista: bool | None
          ) -> tuple[str, float] | None:
    """Devuelve ('largo'|'corto', fuerza) o None."""
    if barras.empty or rango_ref is None:
        return None
    alto, bajo = rango_ref
    ancho = alto - bajo
    if ancho <= 0:
        return None
    precio = float(barras["close"].iloc[-1])
    v = vwap(barras)
    if precio > alto * (1 + MARGEN_ROTURA) and precio > v and spy_alcista is not False:
        return "largo", (precio - alto) / ancho
    if precio < bajo * (1 - MARGEN_ROTURA) and precio < v and spy_alcista is not True:
        return "corto", (bajo - precio) / ancho
    return None


def niveles(lado: str, precio: float, rango_ref: tuple[float, float]) -> tuple[float, float, float]:
    """(distancia_stop, precio_stop, precio_objetivo)."""
    alto, bajo = rango_ref
    dist = (alto - bajo) / 2
    dist = min(max(dist, precio * STOP_MIN_PCT), precio * STOP_MAX_PCT)
    if lado == "largo":
        return dist, round(precio - dist, 2), round(precio + RATIO_OBJETIVO * dist, 2)
    return dist, round(precio + dist, 2), round(precio - RATIO_OBJETIVO * dist, 2)


def tamano(patrimonio: float, precio: float, dist_stop: float, exposicion_actual: float) -> int:
    """Número de acciones (enteras: los cortos no admiten fracciones)."""
    if dist_stop <= 0 or precio <= 0:
        return 0
    por_riesgo = patrimonio * RIESGO_POR_OPERACION / dist_stop
    por_posicion = patrimonio * POSICION_MAXIMA / precio
    hueco = max(0.0, patrimonio * EXPOSICION_MAXIMA - exposicion_actual)
    por_exposicion = hueco / precio
    return max(0, math.floor(min(por_riesgo, por_posicion, por_exposicion)))
