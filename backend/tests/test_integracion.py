"""Prueba de integración del chat con varias claves (sin llamar a ninguna API real).
Ejecutar:  cd backend && python -m unittest discover tests -v
Requiere: pip install httpx  (lo usa el TestClient de FastAPI)"""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
for k in list(os.environ):
    if k.startswith(("GEMINI_API_KEY", "GROQ_API_KEY")):
        del os.environ[k]
os.environ["GEMINI_API_KEYS"] = "g0,g1,g2"
os.environ["GROQ_API_KEYS"] = "q0"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

try:
    import app as codia  # noqa: E402
    from fastapi.testclient import TestClient  # noqa: E402
    HAY_DEPS = True
except ImportError:  # faltan fastapi/httpx/sqlalchemy en este entorno
    HAY_DEPS = False
from claves import PoolClaves  # noqa: E402


class GeminiFalso:
    """Cliente falso: las claves de `malas` responden 429 como Gemini."""
    llamadas = []
    malas = set()

    def __init__(self, clave):
        self.clave = clave
        self.models = self

    def generate_content_stream(self, **_):
        GeminiFalso.llamadas.append(self.clave)
        if self.clave in GeminiFalso.malas:
            raise Exception("429 RESOURCE_EXHAUSTED quota exceeded")
        return iter([SimpleNamespace(text="Hola "), SimpleNamespace(text=f"desde {self.clave}")])

    def generate_content(self, **_):
        GeminiFalso.llamadas.append(self.clave)
        if self.clave in GeminiFalso.malas:
            raise Exception("429 RESOURCE_EXHAUSTED")
        return SimpleNamespace(text=f"ok {self.clave}")


class GroqFalso:
    def __init__(self, clave):
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kw):
        if kw.get("stream"):
            return iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="groq-ok"))])])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="groq-ok"))])


@unittest.skipUnless(HAY_DEPS, "faltan dependencias (pip install -r requirements.txt httpx)")
class TestIntegracion(unittest.TestCase):
    def setUp(self):
        GeminiFalso.llamadas, GeminiFalso.malas = [], set()
        codia.POOL_GEMINI = PoolClaves("Gemini", ["g0", "g1", "g2"], GeminiFalso)
        codia.POOL_GROQ = PoolClaves("Groq", ["q0"], GroqFalso)
        codia.GEMINI_DISPONIBLE = codia.GROQ_DISPONIBLE = True
        self.c = TestClient(codia.app)
        self.cid = self.c.post("/api/conversaciones", json={"usuario_id": "u"}).json()["id"]

    def _chat(self):
        r = self.c.post("/api/chat/stream", json={"usuario_id": "u", "conversacion_id": self.cid, "mensaje": "hola"})
        self.assertEqual(r.status_code, 200)
        return r.text

    def test_reparte_entre_claves(self):
        textos = [self._chat() for _ in range(3)]
        self.assertEqual(sorted(GeminiFalso.llamadas), ["g0", "g1", "g2"])
        self.assertTrue(all(t.startswith("Hola desde g") for t in textos))

    def test_failover_si_una_clave_da_429(self):
        GeminiFalso.malas = {"g0", "g1"}
        self.assertEqual(self._chat(), "Hola desde g2")
        # g0 y g1 quedaron en pausa: la siguiente petición ni las toca
        GeminiFalso.llamadas.clear()
        self._chat()
        self.assertNotIn("g0", GeminiFalso.llamadas)
        self.assertNotIn("g1", GeminiFalso.llamadas)

    def test_cae_a_groq_si_todas_fallan(self):
        GeminiFalso.malas = {"g0", "g1", "g2"}
        self.assertEqual(self._chat(), "groq-ok")

    def test_no_streaming_failover(self):
        GeminiFalso.malas = {"g0", "g1"}
        r = self.c.post("/api/chat", json={"usuario_id": "u", "conversacion_id": self.cid, "mensaje": "hola"})
        self.assertEqual(r.json()["respuesta"], "ok g2")

    def test_estado_muestra_claves(self):
        GeminiFalso.malas = {"g0"}
        self._chat(); self._chat()
        e = self.c.get("/api/estado").json()
        self.assertEqual(e["claves"]["gemini"]["total"], 3)
        self.assertEqual(e["claves"]["groq"]["total"], 1)


if __name__ == "__main__":
    unittest.main()
