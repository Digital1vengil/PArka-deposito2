import json
import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import asistente, parkahub


def _login(c, nombre="Vengil", pin="1234"):
    uid = re.search(r"elegir\((\d+), '" + nombre + r"'\)", c.get("/login").text).group(1)
    assert c.post("/login", data={"usuario_id": uid, "pin": pin}).json()["ok"]


class GeminiFalso:
    """Primero pide una herramienta, después contesta con lo que recibió."""
    def __init__(self, herramienta, args):
        self.herramienta, self.args, self.cuerpos = herramienta, args, []

    def __call__(self, cuerpo):
        self.cuerpos.append(json.loads(json.dumps(cuerpo)))
        ultimo = cuerpo["contents"][-1]["parts"][0]
        if "functionResponse" in ultimo:
            dato = ultimo["functionResponse"]["response"]["resultado"]
            return {"candidates": [{"content": {"role": "model", "parts": [{"text": "Dato: " + dato}]}}]}
        return {"candidates": [{"content": {"role": "model", "parts": [
            {"functionCall": {"name": self.herramienta, "args": self.args}}]}}]}


def test_conexiones_y_asistente_local(monkeypatch):
    with TestClient(app) as c:
        _login(c)
        assert "/asistente" in c.get("/").text
        assert "Gemini sin clave" in c.get("/asistente").text
        # sin clave → error claro
        r = c.post("/asistente/api/mensaje", json={"texto": "hola"})
        assert r.status_code == 400 and "clave de Gemini" in r.json()["detail"]
        # guardar conexiones; el secreto se enmascara y no se pisa si vuelve enmascarado
        r = c.post("/admin/api/conexiones", json={"valores": {"gemini_api_key": "AIzaFAKE12345678", "gemini_modelo": "gemini-2.5-flash"}}).json()
        assert r["aj"]["gemini_api_key"]["valor"].endswith("5678") and "AIza" not in r["aj"]["gemini_api_key"]["valor"]
        c.post("/admin/api/conexiones", json={"valores": {"gemini_api_key": r["aj"]["gemini_api_key"]["valor"]}})
        from app.services import ajustes
        assert ajustes.leer("gemini_api_key") == "AIzaFAKE12345678"
        assert "AIzaFAKE" not in c.get("/admin/conexiones").text
        # herramienta local, sin ParkaHub
        falso = GeminiFalso("resumen_del_dia", {})
        monkeypatch.setattr(asistente, "_llamar", falso)
        r = c.post("/asistente/api/mensaje", json={"texto": "¿cómo viene el día?"}).json()
        assert "tareas_abiertas" in r["texto"] and r["consultas"] == "resumen del día"
        assert "functionDeclarations" in json.dumps(falso.cuerpos[0]["tools"])
        h = c.get("/asistente/api/historial").json()["mensajes"]
        assert [m["rol"] for m in h] == ["user", "model"]
        # la siguiente consulta manda el historial
        c.post("/asistente/api/mensaje", json={"texto": "¿y ahora?"})
        assert len(falso.cuerpos[-1]["contents"]) >= 3
        c.post("/asistente/api/borrar")
        assert c.get("/asistente/api/historial").json()["mensajes"] == []


def test_herramienta_parkahub_sin_token_y_con_token(monkeypatch):
    with TestClient(app) as c:
        _login(c)
        monkeypatch.setattr(asistente, "_llamar", GeminiFalso("buscar_venta", {"numero": "2000012345"}))
        r = c.post("/asistente/api/mensaje", json={"texto": "venta 2000012345"}).json()
        assert "ParkaHub no está conectado" in r["texto"]

        c.post("/admin/api/conexiones", json={"valores": {"parkahub_client_id": "id.access", "parkahub_client_secret": "secretosecreto"}})
        llamadas = []

        def get_falso(ruta, params=None):
            llamadas.append((ruta, params))
            if ruta == "/api/ml/raw":
                return {"ok": True, "httpStatus": 200, "body": {"id": 2000012345, "status": "paid", "total_amount": 99000,
                        "buyer": {"nickname": "COMPRADOR1"}, "shipping": {"id": 4455},
                        "order_items": [{"item": {"id": "MLA1", "title": "Campera Abba", "seller_sku": "ABBA-02003-NEGRO-M-PARK",
                                                  "variation_attributes": [{"name": "Talle", "value_name": "M"}]},
                                         "quantity": 1, "unit_price": 99000}]}}
            if ruta == "/api/dispatch/verify":
                return {"verdict": "green", "message": "Despachar", "shipment": {"status": "ready_to_ship"}}
            raise AssertionError(ruta)
        monkeypatch.setattr(parkahub, "get", get_falso)
        r = c.post("/asistente/api/mensaje", json={"texto": "venta 2000012345"}).json()
        assert "ABBA-02003-NEGRO-M-PARK" in r["texto"] and "Despachar" in r["texto"]
        assert llamadas[0] == ("/api/ml/raw", {"path": "/orders/2000012345"})


def test_raw_solo_rutas_permitidas():
    with pytest.raises(parkahub.ParkaHubError):
        parkahub.raw("/users/me")
    with pytest.raises(parkahub.ParkaHubError):
        parkahub.raw("/orders/123/../../users")
    assert parkahub.RAW_PERMITIDO.match("/orders/2000012345")
    assert parkahub.RAW_PERMITIDO.match("/items/MLA123")


def test_asistente_requiere_acceso():
    with TestClient(app) as c:
        _login(c)
        c.post("/admin/api/usuarios", json={"nombre": "Pepe", "rol": "deposito", "pin": "4321"})
    with TestClient(app) as c2:
        _login(c2, "Pepe", "4321")
        assert c2.get("/asistente", headers={"accept": "application/json"}).status_code == 403
        assert c2.post("/admin/api/conexiones", json={"valores": {}}).status_code == 403
