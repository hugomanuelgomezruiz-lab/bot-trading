# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "alpaca-py>=0.30,<1",
#   "pandas>=2.0",
# ]
# ///
"""Bot intradía AGRESIVO - SOLO PAPER (Alpaca). Largos y cortos.

Cada día de mercado hace UNA sesión de ~70 minutos (para caber en los 2.000
minutos gratis de GitHub Actions):
  1. Cierra cualquier posición que quede abierta.
  2. Mide el rango de referencia (15 primeros min, o últimos 30 si llega tarde).
  3. Cada minuto busca roturas de rango confirmadas por VWAP y por el SPY,
     y abre largos o cortos con órdenes "bracket": el stop y el objetivo quedan
     puestos en el bróker desde el primer momento.
  4. Al final de la ventana cancela órdenes y cierra todo (no duerme con posiciones).
"""
from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import senales as S

CARPETA = Path(__file__).resolve().parent
REGISTROS = CARPETA / "registros"
REGISTROS.mkdir(exist_ok=True)
ESTADO = REGISTROS / "estado_intradia.json"
OPERACIONES = REGISTROS / "operaciones_intradia.csv"
DIARIO = REGISTROS / "diario.csv"
RESUMEN = REGISTROS / "resumen.txt"

log = logging.getLogger("intradia")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
for _h in (logging.FileHandler(REGISTROS / "intradia.log", encoding="utf-8"), logging.StreamHandler(sys.stdout)):
    _h.setFormatter(_fmt)
    log.addHandler(_h)


# ------------------------------------------------------------------ utilidades
class Reloj:
    def ahora(self) -> datetime:
        return datetime.now(timezone.utc)

    def dormir(self, segundos: float) -> None:
        if segundos > 0:
            time.sleep(segundos)


def leer_estado() -> dict:
    try:
        return json.loads(ESTADO.read_text(encoding="utf-8"))
    except Exception:
        return {}


def guardar_estado(estado: dict) -> None:
    ESTADO.write_text(json.dumps(estado, indent=2, ensure_ascii=False), encoding="utf-8")


def anotar(ruta: Path, cabecera: list[str], fila: list) -> None:
    nuevo = not ruta.exists()
    with open(ruta, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        if nuevo:
            w.writerow(cabecera)
        w.writerow(fila)


# ------------------------------------------------------------------ API
class Broker:
    def __init__(self, clave: str, secreto: str):
        if not clave.upper().startswith("PK"):
            raise SystemExit("SEGURIDAD: la clave no es de Paper (debe empezar por PK). No hago nada.")
        from alpaca.trading.client import TradingClient
        from alpaca.data.historical import StockHistoricalDataClient
        self.trading = TradingClient(clave, secreto, paper=True)  # SIEMPRE paper
        self.datos = StockHistoricalDataClient(clave, secreto)
        self._cortable: dict[str, bool] = {}

    def patrimonio(self) -> float:
        c = self.trading.get_account()
        if not str(c.account_number).upper().startswith("P"):
            raise SystemExit("SEGURIDAD: la cuenta no parece Paper. No hago nada.")
        return float(c.equity)

    def estado_mercado(self) -> tuple[bool, datetime, datetime]:
        """(abierto, apertura_hoy_utc, cierre_hoy_utc)."""
        from alpaca.trading.requests import GetCalendarRequest
        reloj = self.trading.get_clock()
        hoy = pd.Timestamp(reloj.timestamp).tz_convert("America/New_York").date()
        cal = self.trading.get_calendar(GetCalendarRequest(start=hoy, end=hoy))
        if not cal:
            return False, None, None

        def a_utc(x):
            t = pd.Timestamp(x)
            if t.tzinfo is None:
                t = t.tz_localize("America/New_York")
            return t.tz_convert("UTC").to_pydatetime()
        return bool(reloj.is_open), a_utc(cal[0].open), a_utc(cal[0].close)

    def barras(self, simbolos: list[str], desde: datetime) -> dict[str, pd.DataFrame]:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.data.enums import DataFeed
        req = StockBarsRequest(symbol_or_symbols=simbolos, timeframe=TimeFrame.Minute,
                               start=desde, feed=DataFeed.IEX)
        df = self.datos.get_stock_bars(req).df
        res = {}
        if df.empty:
            return res
        for s in simbolos:
            if s in df.index.get_level_values(0):
                b = df.xs(s, level=0)[["high", "low", "close", "volume"]].copy()
                b.index = pd.to_datetime(b.index, utc=True)
                res[s] = b.sort_index()
        return res

    def posiciones(self) -> dict[str, dict]:
        return {p.symbol: {"qty": float(p.qty), "valor": abs(float(p.market_value))}
                for p in self.trading.get_all_positions()}

    def se_puede_cortar(self, s: str) -> bool:
        if s not in self._cortable:
            try:
                a = self.trading.get_asset(s)
                self._cortable[s] = bool(a.shortable and a.easy_to_borrow and a.tradable)
            except Exception:
                self._cortable[s] = False
        return self._cortable[s]

    def bracket(self, s: str, lado: str, qty: int, stop: float, objetivo: float) -> str:
        from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
        req = MarketOrderRequest(
            symbol=s, qty=qty, side=OrderSide.BUY if lado == "largo" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY, order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=objetivo),
            stop_loss=StopLossRequest(stop_price=stop))
        return str(self.trading.submit_order(req).id)

    def cerrar_todo(self) -> None:
        self.trading.cancel_orders()
        self.trading.close_all_positions(cancel_orders=True)

    def sin_posiciones(self, segundos: int = 90) -> bool:
        fin = time.time() + segundos
        while time.time() < fin:
            if not self.trading.get_all_positions():
                return True
            time.sleep(3)
        return False


# ------------------------------------------------------------------ sesión
def escribir_resumen(estado: dict, patrimonio: float, nota: str) -> None:
    inicial = estado.get("patrimonio_inicial", patrimonio)
    dias = []
    if DIARIO.exists():
        with open(DIARIO, encoding="utf-8") as f:
            dias = list(csv.DictReader(f, delimiter=";"))
    ganados = sum(1 for d in dias if float(d["resultado_usd"]) > 0)
    lineas = [
        "BOT INTRADIA AGRESIVO - CUENTA PAPER (dinero ficticio)",
        f"Actualizado: {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')} UTC",
        f"Última sesión: {nota}",
        "",
        f"Patrimonio: {patrimonio:,.2f} $",
        f"Desde que empezó el bot intradía ({inicial:,.2f} $): "
        f"{patrimonio - inicial:+,.2f} $ ({(patrimonio / inicial - 1) * 100 if inicial else 0:+.2f}%)",
        f"Días operados: {len(dias)}  (en positivo: {ganados})",
        "",
        "ÚLTIMOS DÍAS:",
    ]
    for d in dias[-10:]:
        lineas.append(f"  {d['fecha']}  {float(d['resultado_usd']):+10,.2f} $  "
                      f"({float(d['resultado_pct']):+.2f}%)  {d['operaciones']} operaciones")
    if not dias:
        lineas.append("  (todavía ninguno)")
    RESUMEN.write_text("\n".join(lineas) + "\n", encoding="utf-8")


def bucle(broker: Broker, reloj: Reloj, fin: datetime, apertura: datetime, rangos: dict,
          patrimonio0: float, operados: set[str]) -> tuple[int, str]:
    num_ops = 0
    while reloj.ahora() < fin:
        try:
            patrimonio = broker.patrimonio()
            if patrimonio <= patrimonio0 * (1 - S.PERDIDA_DIARIA_MAX):
                log.warning("Pérdida diaria >= %.0f%%. Cierro y paro por hoy.", S.PERDIDA_DIARIA_MAX * 100)
                return num_ops, f"límite de pérdida diaria ({S.PERDIDA_DIARIA_MAX:.0%})"
            barras = broker.barras(S.UNIVERSO, apertura)
            posiciones = broker.posiciones()
            spy = barras.get(S.REFERENCIA)
            spy_alcista = None if spy is None or spy.empty else bool(spy["close"].iloc[-1] > S.vwap(spy))

            huecos = S.MAX_POSICIONES - len(posiciones)
            candidatos = []
            if huecos > 0:
                for s, b in barras.items():
                    if s in operados or s in posiciones:
                        continue
                    sg = S.senal(b, rangos.get(s), spy_alcista)
                    if sg:
                        candidatos.append((sg[1], s, sg[0], float(b["close"].iloc[-1])))
            candidatos.sort(reverse=True)
            exposicion = sum(p["valor"] for p in posiciones.values())
            for fuerza, s, lado, precio in candidatos[:huecos]:
                if lado == "corto" and not broker.se_puede_cortar(s):
                    continue
                dist, stop, objetivo = S.niveles(lado, precio, rangos[s])
                qty = S.tamano(patrimonio, precio, dist, exposicion)
                if qty < 1:
                    continue
                try:
                    oid = broker.bracket(s, lado, qty, stop, objetivo)
                except Exception as ex:
                    log.error("No pude abrir %s %s: %s", lado, s, ex)
                    operados.add(s)
                    continue
                operados.add(s)
                num_ops += 1
                exposicion += qty * precio
                log.info("%s %s x%d a ~%.2f | stop %.2f | objetivo %.2f | fuerza %.2f",
                         lado.upper(), s, qty, precio, stop, objetivo, fuerza)
                anotar(OPERACIONES, ["fecha_utc", "simbolo", "lado", "acciones", "precio_aprox",
                                     "stop", "objetivo", "orden"],
                       [reloj.ahora().strftime("%Y-%m-%d %H:%M"), s, lado, qty, f"{precio:.2f}",
                        stop, objetivo, oid])
        except Exception:
            log.exception("Error en el bucle (sigo)")
        reloj.dormir(60)
    return num_ops, "fin de la ventana"


def sesion(broker: Broker, reloj: Reloj) -> str:
    estado = leer_estado()
    ahora = reloj.ahora()
    hoy = ahora.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    if estado.get("sesion_hecha") == hoy:
        log.info("La sesión de hoy ya se hizo. Salgo (ahorro minutos).")
        return ""
    abierto, apertura, cierre = broker.estado_mercado()
    if not abierto:
        log.info("Bolsa cerrada. Salgo.")
        return ""
    fin = min(ahora + timedelta(minutes=S.DURACION_SESION_MIN), cierre - timedelta(minutes=S.CIERRE_ANTES_MIN))
    if fin - ahora < timedelta(minutes=20):
        log.info("Queda muy poca sesión. Salgo.")
        return ""

    estado["sesion_hecha"] = hoy
    patrimonio0 = broker.patrimonio()
    estado.setdefault("patrimonio_inicial", patrimonio0)
    guardar_estado(estado)
    log.info("==== Sesión %s: %s -> %s UTC. Patrimonio %.2f $ ====",
             hoy, ahora.strftime("%H:%M"), fin.strftime("%H:%M"), patrimonio0)

    if broker.posiciones():
        log.info("Cierro posiciones que quedaban abiertas.")
        broker.cerrar_todo()
        broker.sin_posiciones()
        patrimonio0 = broker.patrimonio()

    # Rango de referencia
    fin_rango_apertura = apertura + timedelta(minutes=S.MINUTOS_RANGO_APERTURA)
    if ahora < fin_rango_apertura:
        log.info("Espero a que se forme el rango de apertura (%s UTC).", fin_rango_apertura.strftime("%H:%M"))
        reloj.dormir((fin_rango_apertura - reloj.ahora()).total_seconds() + 5)
        desde, hasta = apertura, fin_rango_apertura
    else:
        desde, hasta = ahora - timedelta(minutes=S.MINUTOS_RANGO_TARDIO), ahora
    barras = broker.barras(S.UNIVERSO, apertura)
    rangos = {s: S.rango(b, pd.Timestamp(desde), pd.Timestamp(hasta)) for s, b in barras.items()}
    log.info("Rangos: %s", ", ".join(f"{s} {r[1]:.2f}-{r[0]:.2f}" for s, r in rangos.items() if r))

    operados: set[str] = set()
    num_ops = 0
    motivo_fin = "fin de la ventana"
    try:
        num_ops, motivo_fin = bucle(broker, reloj, fin, apertura, rangos, patrimonio0, operados)
    finally:
        log.info("Cierro todo (%s).", motivo_fin)
        broker.cerrar_todo()
        broker.sin_posiciones()
    patrimonio1 = broker.patrimonio()
    resultado = patrimonio1 - patrimonio0
    anotar(DIARIO, ["fecha", "patrimonio_inicio", "patrimonio_fin", "resultado_usd", "resultado_pct", "operaciones"],
           [hoy, f"{patrimonio0:.2f}", f"{patrimonio1:.2f}", f"{resultado:.2f}",
            f"{resultado / patrimonio0 * 100:.3f}", num_ops])
    nota = f"{hoy}: {resultado:+,.2f} $ ({resultado / patrimonio0 * 100:+.2f}%), {num_ops} operaciones, {motivo_fin}"
    log.info("Resultado del día: %s", nota)
    guardar_estado(estado)
    escribir_resumen(estado, patrimonio1, nota)
    return nota


def main() -> None:
    k, s = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    if not (k and s):
        raise SystemExit("Faltan ALPACA_API_KEY / ALPACA_SECRET_KEY.")
    try:
        sesion(Broker(k.strip(), s.strip()), Reloj())
    except SystemExit as ex:
        log.error(str(ex))
        raise
    except Exception:
        log.exception("Error inesperado")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
