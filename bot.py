# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "alpaca-py>=0.30,<1",
#   "pandas>=2.0",
#   "numpy>=1.24",
#   "truststore>=0.9",
# ]
# ///
"""Bot de trading SOLO PAPER (dinero ficticio) para Alpaca.

Uso:
    python bot.py acciones   -> una vez al día con la bolsa de EE. UU. abierta
    python bot.py cripto     -> cada pocas horas, 24/7

En cada ejecución:
  1. Comprueba que la cuenta es Paper; si no, se detiene sin hacer nada.
  2. Freno de emergencia (DESACTIVADO por defecto; ver estrategia.FRENO_EMERGENCIA).
  3. Stop dinámico: vende posiciones que caigan un 10% (acciones) o 15% (cripto)
     desde su máximo.
  4. Rebalancea: acciones cada 5 sesiones; cripto una vez al día.
Todo queda registrado en la carpeta "registros".
"""
from __future__ import annotations

import csv
import glob
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Usa los certificados del sistema (necesario si un antivirus inspecciona HTTPS).
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:  # pragma: no cover
    pass

import pandas as pd

import estrategia as E

CARPETA = Path(__file__).resolve().parent
REGISTROS = CARPETA / "registros"
REGISTROS.mkdir(exist_ok=True)
ESTADO = REGISTROS / "estado.json"
OPERACIONES = REGISTROS / "operaciones.csv"
RESUMEN = REGISTROS / "resumen.txt"

log = logging.getLogger("bot")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
_fh = logging.FileHandler(REGISTROS / "bot.log", encoding="utf-8")
_fh.setFormatter(_fmt)
log.addHandler(_fh)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
log.addHandler(_sh)

CRIPTO_SIN_BARRA = {s.replace("/", ""): s for s in E.UNIVERSO_CRIPTO}


def ahora() -> datetime:
    return datetime.now(timezone.utc)


def es_cripto(simbolo: str) -> bool:
    return "/" in simbolo


# ------------------------------------------------------------------ claves
def cargar_claves() -> tuple[str, str]:
    """Variables de entorno (GitHub Secrets) o, en un PC, la config de Claude Desktop."""
    k, s = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    if k and s:
        return k.strip(), s.strip()
    candidatos = []
    local = os.environ.get("LOCALAPPDATA", "")
    roaming = os.environ.get("APPDATA", "")
    if local:
        candidatos += glob.glob(os.path.join(local, "Packages", "Claude_*", "LocalCache",
                                             "Roaming", "Claude", "claude_desktop_config.json"))
    if roaming:
        candidatos.append(os.path.join(roaming, "Claude", "claude_desktop_config.json"))
    for ruta in candidatos:
        try:
            with open(ruta, encoding="utf-8-sig") as f:
                env = json.load(f)["mcpServers"]["alpaca"]["env"]
            if env.get("ALPACA_API_KEY") and env.get("ALPACA_SECRET_KEY"):
                return env["ALPACA_API_KEY"].strip(), env["ALPACA_SECRET_KEY"].strip()
        except Exception:
            continue
    raise SystemExit("No encuentro las claves de Alpaca (ALPACA_API_KEY / ALPACA_SECRET_KEY).")


# ------------------------------------------------------------------ estado
def leer_estado() -> dict:
    try:
        return json.loads(ESTADO.read_text(encoding="utf-8"))
    except Exception:
        return {}


def guardar_estado(estado: dict) -> None:
    ESTADO.write_text(json.dumps(estado, indent=2, ensure_ascii=False), encoding="utf-8")


def anotar_operacion(simbolo: str, lado: str, importe: float, motivo: str, detalle: str) -> None:
    nuevo = not OPERACIONES.exists()
    with open(OPERACIONES, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        if nuevo:
            w.writerow(["fecha_utc", "simbolo", "lado", "importe_usd", "motivo", "detalle"])
        w.writerow([ahora().strftime("%Y-%m-%d %H:%M"), simbolo, lado,
                    f"{importe:.2f}", motivo, detalle])


# ------------------------------------------------------------------ API
class Broker:
    def __init__(self, clave: str, secreto: str):
        if not clave.upper().startswith("PK"):
            raise SystemExit("SEGURIDAD: la clave no es de Paper (debe empezar por PK). No hago nada.")
        from alpaca.trading.client import TradingClient
        from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
        self.trading = TradingClient(clave, secreto, paper=True)  # SIEMPRE paper
        self.datos = StockHistoricalDataClient(clave, secreto)
        self.datos_cripto = CryptoHistoricalDataClient(clave, secreto)
        self._fraccionable: dict[str, bool] = {}

    def cuenta(self):
        c = self.trading.get_account()
        if not str(c.account_number).upper().startswith("P"):
            raise SystemExit("SEGURIDAD: la cuenta no parece Paper. No hago nada.")
        return c

    def mercado_abierto(self) -> bool:
        return bool(self.trading.get_clock().is_open)

    def posiciones(self) -> dict[str, dict]:
        """Clave = símbolo normalizado ('AAPL', 'BTC/USD')."""
        res = {}
        for p in self.trading.get_all_positions():
            sim = CRIPTO_SIN_BARRA.get(p.symbol, p.symbol)
            if "crypto" in str(p.asset_class).lower() and "/" not in sim:
                sim = sim[:-3] + "/" + sim[-3:] if sim.endswith("USD") else sim
            res[sim] = {"qty": float(p.qty), "valor": float(p.market_value),
                        "precio": float(p.current_price),
                        "pyg": float(p.unrealized_pl), "pyg_pct": float(p.unrealized_plpc)}
        return res

    def cierres(self, simbolos: list[str]) -> pd.DataFrame:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.data.enums import DataFeed, Adjustment
        req = StockBarsRequest(symbol_or_symbols=simbolos, timeframe=TimeFrame.Day,
                               start=ahora() - timedelta(days=330),
                               feed=DataFeed.IEX, adjustment=Adjustment.ALL)
        df = self.datos.get_stock_bars(req).df
        cierres = df["close"].unstack(level=0)
        cierres.index = pd.to_datetime(cierres.index).tz_convert("America/New_York").normalize()
        return cierres.sort_index()

    def cierres_cripto(self, simbolos: list[str]) -> pd.DataFrame:
        from alpaca.data.requests import CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame
        req = CryptoBarsRequest(symbol_or_symbols=simbolos, timeframe=TimeFrame.Day,
                                start=ahora() - timedelta(days=330))
        df = self.datos_cripto.get_crypto_bars(req).df
        cierres = df["close"].unstack(level=0)
        cierres.index = pd.to_datetime(cierres.index).tz_convert("UTC").normalize()
        return cierres.sort_index()

    def fraccionable(self, s: str) -> bool:
        if es_cripto(s):
            return True
        if s not in self._fraccionable:
            try:
                a = self.trading.get_asset(s)
                self._fraccionable[s] = bool(a.fractionable) and bool(a.tradable)
            except Exception:
                self._fraccionable[s] = False
        return self._fraccionable[s]

    def orden(self, s: str, lado: str, importe: float, precio: float, qty_max: float | None = None) -> str:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        side = OrderSide.SELL if lado == "venta" else OrderSide.BUY
        tif = TimeInForce.GTC if es_cripto(s) else TimeInForce.DAY
        if lado == "venta":
            qty = importe / precio
            if qty_max is not None:
                qty = min(qty, qty_max)
            qty = math.floor(qty * 1e6) / 1e6 if self.fraccionable(s) else math.floor(qty)
            if qty <= 0:
                return "omitida (cantidad 0)"
            req = MarketOrderRequest(symbol=s, qty=qty, side=side, time_in_force=tif)
        elif self.fraccionable(s):
            req = MarketOrderRequest(symbol=s, notional=round(importe, 2), side=side, time_in_force=tif)
        else:
            qty = math.floor(importe / precio)
            if qty <= 0:
                return "omitida (cantidad 0)"
            req = MarketOrderRequest(symbol=s, qty=qty, side=side, time_in_force=tif)
        o = self.trading.submit_order(req)
        return f"orden {o.id}"

    def cerrar(self, s: str) -> str:
        o = self.trading.close_position(s.replace("/", ""))
        return f"cierre {getattr(o, 'id', '')}"

    def esperar_ordenes(self, segundos: int = 120) -> None:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        fin = time.time() + segundos
        while time.time() < fin:
            if not self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN)):
                return
            time.sleep(3)
        log.warning("Quedan órdenes abiertas tras %ss; sigo igualmente.", segundos)


# ------------------------------------------------------------------ resumen
def escribir_resumen(cuenta, posiciones: dict, estado: dict, nota: str) -> None:
    patrimonio = float(cuenta.equity)
    inicial = estado.get("patrimonio_inicial", patrimonio)
    lineas = [
        "BOT DE TRADING - CUENTA PAPER (dinero ficticio)",
        f"Actualizado: {ahora().strftime('%d/%m/%Y %H:%M')} UTC",
        f"Última acción: {nota}",
        "",
        f"Patrimonio: {patrimonio:,.2f} $   (inicio: {inicial:,.2f} $, "
        f"resultado: {patrimonio - inicial:+,.2f} $ / {(patrimonio / inicial - 1) * 100 if inicial else 0:+.2f}%)",
        f"Efectivo:   {float(cuenta.cash):,.2f} $",
        f"Máximo histórico: {estado.get('patrimonio_maximo', patrimonio):,.2f} $",
        f"Último rebalanceo acciones: {estado.get('ultimo_rebalanceo') or '-'}",
        f"Último rebalanceo cripto:   {estado.get('ultimo_rebalanceo_cripto') or '-'}",
        f"En pausa desde: {estado['pausa_desde']}" if estado.get("pausa_desde") else "Pausa: no",
        "",
        "POSICIONES:",
    ]
    if not posiciones:
        lineas.append("  (ninguna, todo en efectivo)")
    for s, p in sorted(posiciones.items(), key=lambda x: -x[1]["valor"]):
        lineas.append(f"  {s:<8} {p['valor']:>11,.2f} $  ({p['valor'] / patrimonio * 100:5.1f}%)  "
                      f"P/G {p['pyg']:+,.2f} $ ({p['pyg_pct'] * 100:+.1f}%)")
    RESUMEN.write_text("\n".join(lineas) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ ciclo
def sesiones_desde(indice: pd.DatetimeIndex, fecha_iso: str | None) -> int:
    if not fecha_iso:
        return 10 ** 6
    return int((indice > pd.Timestamp(fecha_iso).tz_localize(indice.tz)).sum())


def rebalancear(broker: Broker, objetivo: dict[str, float], posiciones: dict, precios: dict,
                maximos: dict, motivo: str) -> None:
    """Ajusta solo los símbolos de este bloque (posiciones ya filtradas)."""
    patrimonio = float(broker.cuenta().equity)
    actuales = {s: p["valor"] for s, p in posiciones.items()}
    deltas = E.ordenes_necesarias(objetivo, actuales, patrimonio)
    for s, d in [x for x in deltas if x[1] < 0]:
        try:
            if s not in objetivo:
                det = broker.cerrar(s)
            else:
                det = broker.orden(s, "venta", -d, precios[s], posiciones[s]["qty"])
            anotar_operacion(s, "venta", -d, motivo, det)
            log.info("Venta %s %.2f $ (%s)", s, -d, det)
        except Exception as ex:
            log.error("Error vendiendo %s: %s", s, ex)
    broker.esperar_ordenes()
    compras = [x for x in deltas if x[1] > 0]
    disponible = float(broker.cuenta().cash) * 0.99
    total = sum(d for _, d in compras)
    factor = min(1.0, disponible / total) if total > 0 else 0
    for s, d in compras:
        importe = d * factor
        if importe < E.OPERACION_MINIMA_USD:
            continue
        try:
            det = broker.orden(s, "compra", importe, precios[s])
            anotar_operacion(s, "compra", importe, motivo, det)
            log.info("Compra %s %.2f $ (%s)", s, importe, det)
            maximos.setdefault(s, precios[s])
        except Exception as ex:
            log.error("Error comprando %s: %s", s, ex)
    broker.esperar_ordenes()


def aplicar_stops(broker: Broker, maximos: dict, precios: dict, posiciones: dict,
                  stop: float) -> set[str]:
    vendidos = set()
    for s in E.stops_activados(maximos, precios, stop):
        log.info("STOP: %s cae >%.0f%% desde su máximo (%.2f -> %.2f). Vendo.",
                 s, stop * 100, maximos[s], precios[s])
        try:
            det = broker.cerrar(s)
            anotar_operacion(s, "venta", posiciones[s]["valor"], "stop_dinamico", det)
        except Exception as ex:
            log.error("Error cerrando %s: %s", s, ex)
            continue
        maximos.pop(s, None)
        vendidos.add(s)
        posiciones.pop(s, None)
    return vendidos


def liquidar(broker: Broker) -> None:
    """Vende todo lo que se pueda vender ahora (acciones solo con la bolsa abierta)."""
    posiciones = broker.posiciones()
    if not posiciones:
        return
    abierta = broker.mercado_abierto()
    for s, p in posiciones.items():
        if not es_cripto(s) and not abierta:
            continue  # se venderán en la próxima sesión
        try:
            det = broker.cerrar(s)
            anotar_operacion(s, "venta", p["valor"], "freno_drawdown", det)
        except Exception as ex:
            log.error("Error cerrando %s: %s", s, ex)
    broker.esperar_ordenes()


def control_comun(broker: Broker, estado: dict) -> tuple[bool, str]:
    """Pausa y freno de emergencia. Devuelve (seguir, nota)."""
    cuenta = broker.cuenta()
    patrimonio = float(cuenta.equity)
    estado.setdefault("patrimonio_inicial", patrimonio)
    estado["patrimonio_maximo"] = max(estado.get("patrimonio_maximo", patrimonio), patrimonio)

    if estado.get("pausa_desde"):
        dias = (ahora().date() - datetime.fromisoformat(estado["pausa_desde"]).date()).days
        if dias < E.DIAS_PAUSA:
            liquidar(broker)  # por si quedaron acciones sin vender con la bolsa cerrada
            return False, f"EN PAUSA por caída fuerte ({dias}/{E.DIAS_PAUSA} días)"
        log.info("Fin de la pausa: vuelvo a operar.")
        estado.pop("pausa_desde", None)
        estado["patrimonio_maximo"] = patrimonio
        estado["ultimo_rebalanceo"] = None
        estado["ultimo_rebalanceo_cripto"] = None

    dd = E.drawdown(patrimonio, estado["patrimonio_maximo"])
    if E.FRENO_EMERGENCIA and dd >= E.DRAWDOWN_MAXIMO:
        log.warning("FRENO DE EMERGENCIA: caída del %.1f%% desde el máximo. Vendo todo.", dd * 100)
        liquidar(broker)
        estado["pausa_desde"] = ahora().date().isoformat()
        estado["maximos"] = {}
        estado["maximos_cripto"] = {}
        broker.esperar_ordenes()
        return False, "freno de emergencia activado"
    return True, ""


def ciclo_acciones(broker: Broker, estado: dict) -> str:
    if not broker.mercado_abierto():
        log.info("Bolsa cerrada: no opero acciones.")
        return "bolsa cerrada, sin operar acciones"
    todas = broker.posiciones()
    posiciones = {s: p for s, p in todas.items() if not es_cripto(s)}
    cierres = broker.cierres(sorted(set(E.UNIVERSO) | set(posiciones)))
    precios = {s: float(cierres[s].dropna().iloc[-1]) for s in cierres.columns if cierres[s].notna().any()}
    for s, p in posiciones.items():
        precios[s] = p["precio"]

    maximos = E.actualizar_maximos(estado.get("maximos", {}), precios, set(posiciones))
    vendidos = aplicar_stops(broker, maximos, precios, posiciones, E.STOP_DINAMICO)
    estado["maximos"] = maximos

    if sesiones_desde(cierres.index, estado.get("ultimo_rebalanceo")) < E.DIAS_ENTRE_REBALANCEOS:
        log.info("Acciones: hoy no toca rebalancear (cada %d sesiones).", E.DIAS_ENTRE_REBALANCEOS)
        return f"acciones vigiladas ({len(vendidos)} stops)"
    broker.esperar_ordenes()
    tabla = E.puntuar(cierres[[c for c in cierres.columns if c in E.UNIVERSO]])
    objetivo = E.pesos_objetivo(tabla, excluidos=vendidos)
    log.info("Ranking acciones: %s", ", ".join(f"{s} {r.momentum:+.1%}{'' if r.valido else ' (x)'}"
                                                for s, r in tabla.head(10).iterrows()))
    log.info("Objetivo acciones: %s", objetivo or "100% efectivo")
    posiciones = {s: p for s, p in broker.posiciones().items() if not es_cripto(s)}
    rebalancear(broker, objetivo, posiciones, precios, maximos, "rebalanceo_acciones")
    estado["ultimo_rebalanceo"] = ahora().date().isoformat()
    estado["maximos"] = {s: m for s, m in maximos.items() if s in objetivo}
    return f"acciones rebalanceadas ({len(objetivo)} posiciones)"


def ciclo_cripto(broker: Broker, estado: dict) -> str:
    todas = broker.posiciones()
    posiciones = {s: p for s, p in todas.items() if es_cripto(s)}
    cierres = broker.cierres_cripto(sorted(set(E.UNIVERSO_CRIPTO) | set(posiciones)))
    precios = {s: float(cierres[s].dropna().iloc[-1]) for s in cierres.columns if cierres[s].notna().any()}
    for s, p in posiciones.items():
        precios[s] = p["precio"]

    maximos = E.actualizar_maximos(estado.get("maximos_cripto", {}), precios, set(posiciones))
    vendidos = aplicar_stops(broker, maximos, precios, posiciones, E.CRIPTO_STOP)
    estado["maximos_cripto"] = maximos

    hoy = ahora().date().isoformat()
    if estado.get("ultimo_rebalanceo_cripto") == hoy:
        log.info("Cripto: ya rebalanceado hoy; solo vigilo stops.")
        return f"cripto vigilada ({len(vendidos)} stops)"
    broker.esperar_ordenes()
    tabla = E.puntuar(cierres[[c for c in cierres.columns if c in E.UNIVERSO_CRIPTO]],
                      media_dias=E.CRIPTO_MEDIA, anual=365)
    objetivo = E.pesos_objetivo(tabla, excluidos=vendidos, num=E.CRIPTO_POSICIONES,
                                fraccion=E.CRIPTO_FRACCION, maximo=E.CRIPTO_FRACCION)
    log.info("Ranking cripto: %s", ", ".join(f"{s} {r.momentum:+.1%}{'' if r.valido else ' (x)'}"
                                             for s, r in tabla.iterrows()))
    log.info("Objetivo cripto: %s", objetivo or "0% (en efectivo)")
    posiciones = {s: p for s, p in broker.posiciones().items() if es_cripto(s)}
    rebalancear(broker, objetivo, posiciones, precios, maximos, "rebalanceo_cripto")
    estado["ultimo_rebalanceo_cripto"] = hoy
    estado["maximos_cripto"] = {s: m for s, m in maximos.items() if s in objetivo}
    return f"cripto rebalanceada ({len(objetivo)} posiciones)"


def ejecutar(broker: Broker, modo: str) -> None:
    estado = leer_estado()
    seguir, nota = control_comun(broker, estado)
    if seguir:
        nota = ciclo_cripto(broker, estado) if modo == "cripto" else ciclo_acciones(broker, estado)
    else:
        log.info(nota)
    guardar_estado(estado)
    escribir_resumen(broker.cuenta(), broker.posiciones(), estado, f"[{modo}] {nota}")
    log.info("Ciclo %s terminado.", modo)


def main() -> None:
    modo = (sys.argv[1] if len(sys.argv) > 1 else "acciones").lower()
    if modo not in ("acciones", "cripto"):
        raise SystemExit("Modo desconocido. Usa: acciones | cripto")
    log.info("==== Inicio del bot (PAPER) - modo %s ====", modo)
    try:
        clave, secreto = cargar_claves()
        ejecutar(Broker(clave, secreto), modo)
    except SystemExit as ex:
        log.error(str(ex))
        raise
    except Exception:
        log.exception("Error inesperado")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
