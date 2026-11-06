"""Pruebas del pool de claves. Ejecutar:  cd backend && python -m unittest discover tests -v"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from claves import PoolClaves, leer_claves, clasificar_error  # noqa: E402


class TestClaves(unittest.TestCase):
    def test_leer_claves_formatos(self):
        entorno = {
            "GEMINI_API_KEYS": "a, b;c",
            "GEMINI_API_KEY": "a",
            "GEMINI_API_KEY_2": "d",
            "GEMINI_API_KEY_3": "tu_clave_gemini_aqui",
        }
        with mock.patch.dict(os.environ, entorno, clear=True):
            self.assertEqual(leer_claves("GEMINI"), ["a", "b", "c", "d"])

    def test_clasificar(self):
        self.assertEqual(clasificar_error(Exception("429 RESOURCE_EXHAUSTED")), "cuota")
        self.assertEqual(clasificar_error(Exception("quota exceeded for metric PerDay")), "cuota_diaria")
        self.assertEqual(clasificar_error(Exception("API_KEY_INVALID")), "clave_invalida")
        self.assertEqual(clasificar_error(Exception("boom")), "otro")

    def test_round_robin(self):
        p = PoolClaves("t", ["k0", "k1", "k2"], lambda k: k)
        self.assertEqual([p.orden()[0] for _ in range(6)], [0, 1, 2, 0, 1, 2])

    def test_failover_y_enfriamiento(self):
        p = PoolClaves("t", ["k0", "k1"], lambda k: k)
        llamadas = []

        def f(c):
            llamadas.append(c)
            if c == "k0":
                raise Exception("429 quota")
            return "ok:" + c

        self.assertEqual(p.ejecutar(f), "ok:k1")
        self.assertEqual(llamadas, ["k0", "k1"])
        llamadas.clear()
        for _ in range(4):  # k0 en pausa: ya no se prueba primero
            self.assertEqual(p.ejecutar(f), "ok:k1")
        self.assertNotIn("k0", llamadas)
        self.assertEqual(p.resumen(), {"total": 2, "en_espera": 1})

    def test_todas_fallan_relanza(self):
        p = PoolClaves("t", ["a", "b"], lambda k: k)

        def f(c):
            raise Exception("500 " + c)

        with self.assertRaisesRegex(Exception, "500"):
            p.ejecutar(f)

    def test_stream_failover_antes_del_primer_pedazo(self):
        p = PoolClaves("t", ["k0", "k1"], lambda k: k)

        def f(c):
            if c == "k0":
                raise Exception("429")
            yield "ho"
            yield "la"

        self.assertEqual("".join(t for t, _ in p.ejecutar_stream(f)), "hola")

    def test_stream_no_cambia_de_clave_a_mitad(self):
        p = PoolClaves("t", ["k0", "k1"], lambda k: k)

        def f(c):
            yield "ho"
            raise Exception("429 a mitad")

        salida = []
        with self.assertRaisesRegex(Exception, "a mitad"):
            for t, _ in p.ejecutar_stream(f):
                salida.append(t)
        self.assertEqual(salida, ["ho"])

    def test_sin_claves(self):
        p = PoolClaves("t", [], lambda k: k)
        self.assertFalse(p.disponible)
        with self.assertRaises(RuntimeError):
            p.ejecutar(lambda c: 1)


if __name__ == "__main__":
    unittest.main()
