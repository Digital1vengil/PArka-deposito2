"""Mercado Libre directo por el proxy de la app de Costos (clave cargada a mano en Conexiones)."""
import json
import re
import urllib.parse

from fastapi.testclient import TestClient

from app.main import app
from app.services import meli, parkahub


def _login(c, nombre="Vengil", pin="1234"):
    uid = re.search(r"elegir\((\d+), '" + nombre + r"'\)", c.get("/login").text).group(1)
    assert c.post("/login", data={"usuario_id": uid, "pin": pin}).json()["ok"]


ORDEN = {"id": 2000000111, "date_created": "2026-10-07T10:00:00.000-03:00", "status": "paid", "total_amount": 50000,
         "buyer": {"nickname": "COMPRADOR"}, "shipping": {"id": 44000000001},
         "order_items": [{"item": {"id": "MLA1", "title": "Campera Puffer", "seller_sku": "PUF-BLK-M",
                                   "variation_attributes": [{"name": "Color", "value_name": "Negro"}]},
                          "quantity": 1, "unit_price": 50000}]}
ENVIOS = {"44000000001": {"status": "ready_to_ship", "substatus": "printed", "logistic_type": "self_service"},
          "44000000009": {"status": "cancelled", "substatus": None, "logistic_type": "self_service"},
          "44000000005": {"status": "ready_to_ship", "logistic_type": "fulfillment"}}


def falso_pedir(llamadas):
    def pedir(req):
        url = req.full_url
        llamadas.append((req.get_method(), url, req.get_header("X-api-key")))
        if req.get_header("X-api-key") != "clave-ok":
            raise meli.MeliError("La clave de Mercado Libre no es válida o fue revocada (pedile una nueva a Gonzalo).", 401)
        if url.endswith("/perfil"):
            return {"perfil": "publicaciones", "lectura": ["/items", "/orders/search"], "bloqueadas": ["/payments"]}
        if url.endswith("/write"):
            return {"status": 200, "ok": True, "data": json.loads(req.data)}
        path = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["path"][0]
        if path == "/users/me":
            return {"status": 200, "data": {"id": 413629450, "nickname": "PARKA.AR",
                                            "seller_reputation": {"level_id": "5_green", "power_seller_status": "platinum",
                                                                  "metrics": {"claims": {"rate": 0.01}}}}}
        if path.startswith("/questions/search"):
            assert "seller_id=413629450" in path
            return {"status": 200, "data": {"questions": [{"id": 9, "item_id": "MLA1", "text": "¿Hay talle M?",
                                                           "date_created": "2026-10-07T09:00:00"}]}}
        if path.startswith("/items?ids="):
            return {"status": 200, "data": [{"code": 200, "body": {"id": "MLA1", "title": "Campera Puffer",
                                                                     "attributes": [{"id": "MODEL", "value_name": "PUF"}]}}]}
        if path.startswith("/orders/search"):
            assert "seller=413629450" in path
            return {"status": 200, "data": {"results": [ORDEN], "paging": {"total": 1}}}
        if path == "/orders/2000000111":
            return {"status": 200, "data": ORDEN}
        m = re.match(r"/shipments/(\d+)$", path)
        if m:
            sh = ENVIOS.get(m.group(1))
            return {"status": 200, "data": sh} if sh else {"status": 404, "data": {"message": "not found"}}
        return {"status": 404, "data": {}}
    return pedir


def test_conexion_manual_y_seccion_ml_directa(monkeypatch):
    llamadas = []
    monkeypatch.setattr(meli, "_pedir", falso_pedir(llamadas))
    monkeypatch.setattr(meli, "PAUSA", 0)
    meli.olvidar_cache()
    with TestClient(app) as c:
        _login(c)
        html = c.get("/admin/conexiones").text
        assert "Mercado Libre directo" in html and 'name="meli_api_key"' in html
        assert not parkahub.disponible()

        # clave mala → mensaje claro
        assert c.post("/admin/api/conexiones", json={"valores": {"meli_api_key": "mala"}}).json()["ok"]
        r = c.post("/admin/api/conexiones/probar/meli").json()
        assert not r["ok"] and "no es válida" in r["mensaje"]

        # clave buena cargada a mano
        assert c.post("/admin/api/conexiones", json={"valores": {"meli_api_key": "clave-ok"}}).json()["ok"]
        r = c.post("/admin/api/conexiones/probar/meli").json()
        assert r["ok"] and "PARKA.AR" in r["mensaje"] and "publicaciones" in r["mensaje"]
        assert c.get("/admin/api/conexiones/meli/perfil").json()["perfil"]["bloqueadas"] == ["/payments"]
        # la clave no vuelve entera a la pantalla
        assert "clave-ok" not in c.get("/admin/conexiones").text
        assert all(k in (None, "clave-ok", "mala") for _, _, k in llamadas)

        assert parkahub.fuente() == "directo"
        assert "ML directo" in c.get("/ml").text
        res = c.get("/ml/api/resumen").json()
        assert res["estado"]["cuenta"] == "PARKA.AR"
        assert res["reputacion"]["nivel"] == "5_green"
        assert res["ventas_hoy"]["total"] == 1
        assert res["preguntas"][0]["publicacion"] == "Campera Puffer"

        v = c.get("/ml/api/buscar", params={"q": "2000000111"}).json()
        assert v["tipo"] == "venta" and v["dato"]["items"][0]["sku"] == "PUF-BLK-M"
        assert v["dato"]["envio"]["estado"] == "ready_to_ship"

        # semáforo de Despacho sin ParkaHub
        assert c.get("/despacho/api/ml/estado").json()["conectado"]
        assert c.get("/despacho/api/ml/verificar", params={"codigo": "44000000001"}).json()["veredicto"] == "green"
        assert c.get("/despacho/api/ml/verificar", params={"codigo": "44000000009"}).json()["veredicto"] == "red"
        assert c.get("/despacho/api/ml/verificar", params={"codigo": "44000000005"}).json()["motivo"] == "other_logistic"
        assert c.get("/despacho/api/ml/verificar", params={"codigo": "44000000077"}).json()["motivo"] == "not_found"

        # devoluciones y cotejo siguen siendo de ParkaHub: error claro, no se cae
        assert "ParkaHub" in c.get("/ml/api/devoluciones").json()["detail"]


def test_write_manda_metodo_y_ruta(monkeypatch):
    llamadas = []
    monkeypatch.setattr(meli, "_pedir", falso_pedir(llamadas))
    monkeypatch.setattr(meli, "PAUSA", 0)
    r = meli.write("PUT", "/items/MLA1", {"available_quantity": 40})
    assert r["ok"] and r["data"] == {"method": "PUT", "path": "/items/MLA1", "body": {"available_quantity": 40}}
    assert llamadas[-1][0] == "POST" and llamadas[-1][1].endswith("/api/meli/write")
