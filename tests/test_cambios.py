"""App de cambios (Cloudflare) ↔ Depósito: los cambios aceptados entran a la búsqueda del día, los cancelados salen,
y desde la sección Cambios se despachan (en la app de cambios quedan "Despachado")."""
import re

from fastapi.testclient import TestClient

from app.main import app
from app.services import cambios, meli, tiendanube as tn

SOL = [
    {"ID": "CAM-0001", "Orden": "#1285", "Estado": "Aceptado", "Cliente": "Cliente Uno", "Envío": "Envío Flex",
     "Enviar al cliente": "1x Campera Holy — Negro / M (SKU W-7-BLK-M)",
     "Devuelve el cliente": "1x Campera Holy (Verde, M) — Verde / M (SKU W-7-GREEN-M)", "Localidad": "Almagro"},
    {"ID": "CAM-0002", "Orden": "#1277", "Estado": "Pendiente", "Cliente": "Cliente Dos", "Envío": "Correo Argentino",
     "Enviar al cliente": "1x Campera Vesubio — Verde / S (SKU M-112-GREEN-S)", "Devuelve el cliente": "x"},
    {"ID": "CAM-0003", "Orden": "#1300", "Estado": "Recibido", "Cliente": "Cliente Tres", "Envío": "Correo Argentino",
     "Enviar al cliente": "2x Sweater Lino — Gris / L (SKU SW-1-GRY-L)", "Devuelve el cliente": "x"},
]


def falso(llamadas, clave="ok"):
    estado = {"vence": False}

    def post(base, cuerpo, cookie=""):
        llamadas.append((cuerpo.get("action"), cookie))
        if cuerpo["action"] == "login":
            if cuerpo.get("password") != clave:
                return 401, {"ok": False, "error": "Clave incorrecta"}, []
            return 200, {"ok": True}, ["sesion=abc123; Path=/; HttpOnly", "otra=1; Path=/"]
        if cookie != "sesion=abc123; otra=1" or estado["vence"]:
            estado["vence"] = False
            return 401, {"ok": False, "error": "auth"}, []
        if cuerpo["action"] == "adminListar":
            return 200, {"ok": True, "solicitudes": [dict(s) for s in SOL], "estados": []}, []
        if cuerpo["action"] == "adminActualizar":
            for s in SOL:
                if s["ID"] == cuerpo["id"]:
                    s.update(cuerpo["cambios"])
            return 200, {"ok": True}, []
        return 400, {"ok": False, "error": "acción"}, []
    return post, estado


def _login(c, nombre="Vengil", pin="1234"):
    uid = re.search(r"elegir\((\d+), '" + nombre + r"'\)", c.get("/login").text).group(1)
    assert c.post("/login", data={"usuario_id": uid, "pin": pin}).json()["ok"]


def test_items_de():
    it = cambios.items_de("2x Sweater Lino — Gris / L (SKU SW-1-GRY-L)")
    assert it == [{"sku": "SW-1-GRY-L", "producto": "Sweater Lino", "color": "Gris", "talle": "L", "cantidad": 2,
                   "variante": "Color: Gris · Talle: L"}]


def test_sesion_y_reintento(monkeypatch):
    llamadas = []
    post, estado = falso(llamadas)
    monkeypatch.setattr(cambios, "_post", post)
    with TestClient(app) as c:
        _login(c)
        assert c.post("/admin/api/conexiones", json={"valores": {"cambios_clave": "ok"}}).json()["ok"]
        r = c.post("/admin/api/conexiones/probar/cambios").json()
        assert r["ok"] and "2 para preparar" in r["mensaje"]
        estado["vence"] = True   # la sesión venció: entra de nuevo solo
        assert len(cambios.solicitudes(refrescar=True)) == 3
        assert [a for a, _ in llamadas].count("login") == 2


def test_busqueda_y_despacho(monkeypatch):
    llamadas = []
    post, _ = falso(llamadas)
    monkeypatch.setattr(cambios, "_post", post)
    monkeypatch.setattr(meli, "configurado", lambda: False)
    monkeypatch.setattr(tn, "configurado", lambda *a: False)
    cambios.olvidar()
    with TestClient(app) as c:
        _login(c)
        c.post("/admin/api/conexiones", json={"valores": {"cambios_clave": "ok"}})
        r = c.post("/ecommerce/api/auto/ahora").json()
        assert r["ok"] and r["nuevas"] == 2, r       # CAM-0001 (Aceptado) y CAM-0003 (Recibido); la Pendiente no
        cid = r["busqueda_id"]
        todo = c.get(f"/ecommerce/api/{cid}/todo").json()["grupos"]
        assert any("Cambio CAM-0001" in " ".join(g["paquetes"]) for g in todo)
        # la sección Cambios muestra qué mandar y si está en la búsqueda
        assert "Cambios" in c.get("/cambios").text
        d = c.get("/cambios/api/lista?refrescar=1").json()
        assert [x["id"] for x in d["cambios"]] == ["CAM-0001", "CAM-0003"] and d["pendientes_aceptar"] == 1
        assert d["cambios"][0]["busqueda"]["control_id"] == cid
        # despachar por nº de orden → Despachado en la app de cambios
        r = c.post("/cambios/api/despachar", json={"codigo": "1285"}).json()
        assert r["ok"] and r["id"] == "CAM-0001" and "mesa de control" in r["aviso"]
        assert SOL[0]["Estado"] == "Despachado"
        assert c.post("/cambios/api/despachar", json={"codigo": "CAM-0001"}).json()["motivo"] == "ya"
        assert c.post("/cambios/api/despachar", json={"codigo": "CAM-0002"}).json()["motivo"] == "no_listo"
        # se cancela CAM-0003 en la app → sale de la búsqueda con aviso
        SOL[2]["Estado"] = "Cancelado"
        r = c.post("/ecommerce/api/auto/ahora").json()
        assert r["canceladas"] == 1
        textos = [n["texto"] for n in c.get("/ecommerce/api/novedades").json()["novedades"]]
        assert any("CAM-0003" in t and "no despachar" in t for t in textos)
        assert c.post("/cambios/api/despachar", json={"codigo": "CAM-0003"}).json()["motivo"] == "cancelado"
    SOL[0]["Estado"], SOL[2]["Estado"] = "Aceptado", "Recibido"


def test_solapa_cambios_en_despacho(monkeypatch):
    llamadas = []
    post, _ = falso(llamadas)
    monkeypatch.setattr(cambios, "_post", post)
    cambios.olvidar()
    try:
        with TestClient(app) as c:
            _login(c)
            c.post("/admin/api/conexiones", json={"valores": {"cambios_clave": "ok"}})
            d = c.get("/despacho/api/vivo?tipo=cambios&refrescar=1").json()
            assert d["ok"] and [p["trackingId"] for p in d["paquetes"]] == ["CAM-0001", "CAM-0003"]
            p = d["paquetes"][0]
            assert p["ventaId"] == "1285" and p["items"][0]["talle"] == "M" and p["medioEnvio"] == "Envío Flex"
            assert c.get("/despacho/api/vivo/canceladas?tipo=cambios").json()["ok"]
            r = c.get("/despacho/api/vivo/envio?tipo=cambios&codigo=CAM-0002").json()
            assert r["motivo"] == "no_lista"
            # sale en el scanner → Despachado en la app de cambios, y la lista lo muestra en verde
            r = c.post("/despacho/api/vivo/escaneado", json={"tipo": "cambios", "tracking": "CAM-0001", "venta": "1285",
                                                             "comprador": "Cliente Uno"}).json()
            assert r["ok"] and r.get("cambio") == "Despachado" and SOL[0]["Estado"] == "Despachado"
            assert "btn-drive-cambios" in c.get("/despacho/scanner/").text
    finally:
        SOL[0]["Estado"] = "Aceptado"
