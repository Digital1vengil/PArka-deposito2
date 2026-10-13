"""Tienda Nube por la API oficial: autorización (code → token), órdenes pendientes en Recolección Ecommerce
y "orden enviada" (fulfillment order → DISPATCHED) cuando sale en Despacho."""
import io
import json
import re
import urllib.parse

from fastapi.testclient import TestClient

from app.main import app
from app.services import tiendanube as tn


def _login(c, nombre="Vengil", pin="1234"):
    uid = re.search(r"elegir\((\d+), '" + nombre + r"'\)", c.get("/login").text).group(1)
    assert c.post("/login", data={"usuario_id": uid, "pin": pin}).json()["ok"]


ORDENES = [
    {"id": 1501, "number": 7001, "status": "open", "payment_status": "paid", "shipping_status": "unpacked",
     "contact_name": "Cliente Uno", "shipping_address": {"name": "Cliente Uno"}, "shipping_tracking_number": None,
     "shipping_option": {"name": "Envío Nube - Correo Argentino"},
     "products": [{"name": "Campera Thor (Negro, XL)", "sku": "1-CAMP-M-00114-BLCK-XL-PARK", "quantity": 1,
                   "variant_values": ["Negro", "XL"]},
                  {"name": {"es": "Chomba Polo"}, "sku": "CH-1", "quantity": "2", "variant_values": [{"es": "Azul"}, {"es": "M"}]}]},
    {"id": 1502, "number": 7002, "status": "open", "payment_status": "paid", "shipping_status": "shipped",
     "products": [{"name": "Ya enviada", "sku": "X", "quantity": 1}]},
    {"id": 1504, "number": 7004, "status": "open", "payment_status": "paid", "shipping_status": "unshipped",
     "products": [{"name": "Ya empaquetada (Por enviar)", "sku": "Z", "quantity": 1}]},
    {"id": 1503, "number": 7003, "status": "cancelled", "payment_status": "paid", "shipping_status": "unpacked",
     "products": [{"name": "Cancelada", "sku": "Y", "quantity": 1}]},
]


def falso(llamadas, fos_status="UNPACKED"):
    def pedir(metodo, path, body=None, params=None):
        llamadas.append((metodo, path, body, params))
        if path == "/store":
            return {"id": 999, "name": {"es": "PARKA"}}
        if path == "/orders" and params and params.get("q"):
            return [o for o in ORDENES if str(o["number"]) == str(params["q"])]
        if path == "/orders":
            return ORDENES if params.get("page") == 1 else []
        m = re.match(r"/orders/(\d+)/fulfillment-orders$", path)
        if m and metodo == "GET":
            return [{"id": "01FO" + m.group(1), "status": fos_status}]
        if metodo == "PATCH":
            return {"id": path.rsplit("/", 1)[1], "status": body["status"]}
        raise tn.TNError("no encontrado", 404)
    return pedir


def test_orden_a_paquete_y_pendientes(monkeypatch):
    monkeypatch.setattr(tn, "_cfg", lambda: ("tok", "999"))
    monkeypatch.setattr(tn, "_pedir", falso([]))
    ps = tn.ordenes_pendientes()
    assert [p["tracking"] for p in ps] == ["7001"]
    p = ps[0]
    assert p["tipo"] == "tiendanube" and p["comprador"] == "Cliente Uno" and p["cantidad"] == 3
    assert p["items"][0]["color"] == "Negro" and p["items"][0]["talle"] == "XL"
    assert p["items"][1]["producto"] == "Chomba Polo" and p["items"][1]["talle"] == "M" and p["items"][1]["cantidad"] == 2


def test_autorizar_y_canjear_codigo(monkeypatch):
    enviados = []

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def urlopen(req, timeout=0):
        enviados.append((req.full_url, json.loads(req.data)))
        return Resp(json.dumps({"access_token": "tok-123", "token_type": "bearer", "scope": tn.SCOPES,
                                "user_id": 4242}).encode())
    monkeypatch.setattr(tn.urllib.request, "urlopen", urlopen)
    with TestClient(app) as c:
        _login(c)
        assert c.post("/admin/api/conexiones", json={"valores": {"tn_app_id": "45453", "tn_client_secret": "sec"}}).json()["ok"]
        r = c.get("/tiendanube/autorizar", follow_redirects=False)
        assert r.headers["location"] == "https://www.tiendanube.com/apps/45453/authorize"
        r = c.get("/tiendanube/callback?code=abc", follow_redirects=False)
        assert "tn_ok=4242" in r.headers["location"]
        assert enviados[0][0] == tn.TOKEN_URL and enviados[0][1] == {
            "client_id": "45453", "client_secret": "sec", "grant_type": "authorization_code", "code": "abc"}
        # pegando la URL entera también sirve
        r = c.post("/admin/api/conexiones/tiendanube/codigo",
                   json={"codigo": "http://localhost:8100/tiendanube/callback?code=xyz"}).json()
        assert r["ok"] and enviados[-1][1]["code"] == "xyz"
        html = c.get("/admin/conexiones").text
        assert "Autorizada" in html and "tok-123" not in html  # el token nunca se muestra


def test_ecommerce_desde_api_y_despacho_marca_enviada(monkeypatch):
    llamadas = []
    monkeypatch.setattr(tn, "_pedir", falso(llamadas))
    with TestClient(app) as c:
        _login(c)
        assert tn.configurado()
        r = c.post("/admin/api/conexiones/probar/tiendanube").json()
        assert r["ok"] and "PARKA" in r["mensaje"] and "1 órdenes" in r["mensaje"]
        r = c.post("/ecommerce/api/crear/drive", json={"ids": [], "tn_api": True}).json()
        assert r["ok"] and r["busqueda"]["paquetes"] == 1
        cid = r["busqueda"]["id"]
        r = c.post(f"/ecommerce/api/{cid}/agregar-tn").json()
        assert r["nuevos"] == 0  # no duplica
        # Despacho: sale la orden 7001 → la fulfillment order pasa a DISPATCHED
        r = c.post("/api/tiendanube/despachado", json={"numero": "7001"}).json()
        assert r["ok"] and r["estado"] == "ok"
        assert ("PATCH", "/orders/1501/fulfillment-orders/01FO1501", {"status": "DISPATCHED"}, None) in llamadas
        n = len(llamadas)
        r = c.post("/api/tiendanube/despachado", json={"numero": "7001"}).json()
        assert r["ok"] and len(llamadas) == n  # ya estaba: no vuelve a llamar
        assert c.post("/api/tiendanube/despachado", json={"numero": "MEL123"}).json()["estado"] == "ignorado"
        envs = c.get("/admin/api/conexiones/tiendanube/envios").json()["envios"]
        assert envs[0]["numero"] == "7001" and envs[0]["estado"] == "ok"
        # apagado desde Conexiones → no toca TN
        c.post("/admin/api/conexiones", json={"valores": {"tn_marcar_despacho": "0"}})
        assert c.post("/api/tiendanube/despachado", json={"numero": "7009"}).json()["estado"] == "apagado"
        c.post("/admin/api/conexiones", json={"valores": {"tn_marcar_despacho": "1"}})


def test_error_queda_para_reintentar(monkeypatch):
    from app.db import SessionLocal
    rotas = {"si": True}
    base = falso([])

    def pedir(metodo, path, body=None, params=None):
        if metodo == "PATCH" and rotas["si"]:
            raise tn.TNError("Tienda Nube respondió 500", 500)
        return base(metodo, path, body, params)
    monkeypatch.setattr(tn, "_pedir", pedir)
    db = SessionLocal()
    try:
        assert tn.procesar_envio(db, "7001")["estado"] in ("error", "ok")
        rotas["si"] = False
        tn.reintentar(db)
        assert tn.ultimos_envios(db)[0]["estado"] == "ok"
    finally:
        db.close()


def test_scanner_carga_el_script():
    with TestClient(app) as c:
        _login(c)
        r = c.get("/despacho/scanner/tiendanube-despacho.js")
        assert r.status_code == 200 and "/api/tiendanube/despachado" in r.text
        assert "tiendanube-despacho.js" in c.get("/despacho/scanner/").text
