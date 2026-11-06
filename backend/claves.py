"""
CodIA - Pool de claves de API (rotación + failover)

Para qué sirve: los planes gratuitos de Gemini y Groq tienen límites de
peticiones por minuto/día POR CLAVE. Si cargas varias claves (idealmente de
distintas cuentas/proyectos), CodIA:

  1) REPARTE la carga: cada petición nueva empieza por la siguiente clave
     (round-robin), así ninguna se gasta antes que las demás.
  2) HACE FAILOVER: si una clave devuelve 429 / cuota agotada, se pone en
     "enfriamiento" unos minutos y la misma petición se reintenta
     automáticamente con la siguiente clave, sin que el usuario note nada.
  3) DESCARTA claves inválidas por un buen rato para no volver a intentarlas.

Cómo se configuran (en .env o en las variables de Render), cualquiera de:

    GEMINI_API_KEYS=clave1,clave2,clave3        # lista separada por comas
    GEMINI_API_KEY=clave1                       # (la forma antigua sigue sirviendo)
    GEMINI_API_KEY_2=clave2                     # numeradas, hasta _20
    GEMINI_API_KEY_3=clave3

Lo mismo con GROQ_*. Se pueden mezclar formatos; las repetidas se ignoran.
"""

import os
import re
import threading
import time
from typing import Callable, Generator, List, Tuple

SEGUNDOS_CUOTA = 65          # límite por minuto: se recupera rápido
SEGUNDOS_CUOTA_DIARIA = 3600  # cuota diaria agotada: no insistir pronto
SEGUNDOS_CLAVE_INVALIDA = 6 * 3600


def leer_claves(prefijo: str) -> List[str]:
    """Junta todas las claves del entorno para un proveedor (GEMINI / GROQ)."""
    candidatas: List[str] = []

    lista = os.getenv(f"{prefijo}_API_KEYS", "")
    candidatas += re.split(r"[,;\s]+", lista)

    candidatas.append(os.getenv(f"{prefijo}_API_KEY", ""))
    for n in range(1, 21):
        candidatas.append(os.getenv(f"{prefijo}_API_KEY_{n}", ""))

    claves, vistas = [], set()
    for c in candidatas:
        c = (c or "").strip().strip('"').strip("'")
        # ignora vacías y los placeholders del .env.example
        if not c or c.startswith("tu_clave") or c in vistas:
            continue
        vistas.add(c)
        claves.append(c)
    return claves


def clasificar_error(error: Exception) -> str:
    """'cuota' | 'cuota_diaria' | 'clave_invalida' | 'otro'"""
    t = str(error)
    bajo = t.lower()
    if "perday" in bajo.replace("_", "").replace(" ", "") or "per day" in bajo or "daily" in bajo:
        return "cuota_diaria"
    if "resource_exhausted" in bajo or "429" in t or "quota" in bajo or "rate limit" in bajo or "rate_limit" in bajo:
        return "cuota"
    if (
        "api_key_invalid" in bajo
        or "api key not valid" in bajo
        or "invalid api key" in bajo
        or "invalid_api_key" in bajo
        or "permission_denied" in bajo
        or "401" in t
        or "403" in t
    ):
        return "clave_invalida"
    return "otro"


class PoolClaves:
    def __init__(self, nombre: str, claves: List[str], fabrica_cliente: Callable[[str], object]):
        self.nombre = nombre
        self._claves = list(claves)
        self._fabrica = fabrica_cliente
        self._clientes: dict = {}
        self._hasta: dict = {}  # indice -> timestamp hasta el que está en enfriamiento
        self._siguiente = 0
        self._lock = threading.Lock()

    # ---- información ----
    def __len__(self) -> int:
        return len(self._claves)

    @property
    def disponible(self) -> bool:
        return len(self._claves) > 0

    def resumen(self) -> dict:
        ahora = time.time()
        with self._lock:
            en_espera = sum(1 for i in range(len(self._claves)) if self._hasta.get(i, 0) > ahora)
        return {"total": len(self._claves), "en_espera": en_espera}

    # ---- uso ----
    def _cliente(self, i: int):
        if i not in self._clientes:
            self._clientes[i] = self._fabrica(self._claves[i])
        return self._clientes[i]

    def orden(self) -> List[int]:
        """Orden en que se probarán las claves para UNA petición: rotadas
        (round-robin), con las que están en enfriamiento al final."""
        ahora = time.time()
        with self._lock:
            n = len(self._claves)
            if n == 0:
                return []
            inicio = self._siguiente
            self._siguiente = (self._siguiente + 1) % n
            rotadas = [(inicio + k) % n for k in range(n)]
            libres = [i for i in rotadas if self._hasta.get(i, 0) <= ahora]
            frias = sorted((i for i in rotadas if self._hasta.get(i, 0) > ahora), key=lambda i: self._hasta[i])
        return libres + frias

    def penalizar(self, i: int, error: Exception) -> str:
        tipo = clasificar_error(error)
        segundos = {
            "cuota": SEGUNDOS_CUOTA,
            "cuota_diaria": SEGUNDOS_CUOTA_DIARIA,
            "clave_invalida": SEGUNDOS_CLAVE_INVALIDA,
        }.get(tipo, 0)
        if segundos:
            with self._lock:
                self._hasta[i] = time.time() + segundos
            print(f"[CodIA] {self.nombre}: clave #{i + 1} en pausa {segundos}s ({tipo})")
        return tipo

    def ejecutar(self, funcion: Callable[[object], object]):
        """Llama funcion(cliente) con cada clave hasta que una funcione.
        Si todas fallan, relanza el último error."""
        ultimo = None
        for i in self.orden():
            try:
                return funcion(self._cliente(i))
            except Exception as e:  # noqa: BLE001
                ultimo = e
                self.penalizar(i, e)
        if ultimo is not None:
            raise ultimo
        raise RuntimeError(f"No hay claves de {self.nombre} configuradas")

    def ejecutar_stream(
        self, funcion: Callable[[object], Generator]
    ) -> Generator[Tuple[str, int], None, None]:
        """funcion(cliente) debe devolver un generador de texto. Hace failover
        a otra clave SOLO si el error ocurre antes del primer pedazo (después
        ya no se puede 'deshacer' lo que el usuario vio). Produce
        (pedazo, indice_de_clave)."""
        ultimo = None
        for i in self.orden():
            enviado = False
            try:
                for pedazo in funcion(self._cliente(i)):
                    enviado = True
                    yield pedazo, i
                if enviado:
                    return
                # stream vacío: probar con otra clave
                ultimo = RuntimeError("respuesta vacía")
            except Exception as e:  # noqa: BLE001
                if enviado:
                    self.penalizar(i, e)
                    raise
                ultimo = e
                self.penalizar(i, e)
        if ultimo is not None:
            raise ultimo
        raise RuntimeError(f"No hay claves de {self.nombre} configuradas")
