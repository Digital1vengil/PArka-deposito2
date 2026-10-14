"""SmartPost: en Despacho → Tienda Nube se escanea la etiqueta de SmartPost (nº de guía) y se encuentra la orden."""
from fastapi.testclient import TestClient

from app.main import app
from app.services import ajustes, smartpost
from tests.test_despacho_vivo import _mocks
from tests.test_tiendanube import _login

GUIAS = {
    # remito = nº de orden de TN
    "SP000111": {"guia": {"nro_guia": "SP000111", "remito": "#7004",
                          "destinatario": {"nombre": "Ana", "apellido": "Gómez"},
                          "fechas": [{"fecha": "14/10", "estado": "En sucursal"}]}},
    # sin remito: se busca por nombre (la 7004 de los mocks no tiene nombre → no coincide)
    "SP000222": {"guia": {"nro_guia": "SP000222", "destinatario": {"nombre": "Nadie", "apellido": "Igual"}}},
    # remito de una orden cancelada
    "SP000333": {"guia": {"nro_guia": "SP000333", "remito": "TN-7003"}},
}


def _sp(monkeypatch, llamadas=None):
    def llamar(ruta, body=None):
        if llamadas is not None:
            llamadas.append((ruta, body))
        if ruta == "v2/dummy-test.json":
            return {"user": "OK", "cliente": "MAGONTEX S.A"}
        return GUIAS.get((body or {}).get("nro_guia") or (body or {}).get("remito"), {"guia": None})
    monkeypatch.setattr(smartpost, "llamar", llamar)
    monkeypatch.setattr(smartpost, "activo", lambda *a: True)
    monkeypatch.setattr(smartpost, "configurado", lambda *a: True)
    monkeypatch.setattr(smartpost, "panel_configurado", lambda *a: False)
    smartpost.olvidar()


def test_leer_guia_y_numero_de_orden(monkeypatch):
    _sp(monkeypatch)
    g = smartpost.leer_guia("SP000111")
    assert g["encontrada"] and g["remitos"] == ["#7004"] and g["nombre"] == "Ana Gómez" and g["estado"] == "En sucursal"
    assert smartpost.numeros_orden(["#7004", "TN-007003"]) == ["7004", "7003"]
    assert not smartpost.leer_guia("NOEXISTE")["encontrada"]


def test_match_por_nombre(monkeypatch):
    monkeypatch.setattr(smartpost, "configurado", lambda *a: True)
    monkeypatch.setattr(smartpost, "panel_configurado", lambda *a: False)
    por_enviar = [{"tracking": "7004", "comprador": "Ana María Gómez"}, {"tracking": "7005", "comprador": "Otro"}]
    import app.services.smartpost as sp
    orig = sp.leer_guia
    sp.leer_guia = lambda c: {"encontrada": True, "guia": c, "remitos": [], "textos": [], "nombre": "ANA GOMEZ", "estado": "", "crudo": {}}
    try:
        sp.olvidar()
        m = sp.orden_de_guia("X1", por_enviar)
        assert m["numero"] == "7004" and m["via"] == "nombre"
    finally:
        sp.leer_guia = orig
        sp.olvidar()


def test_despacho_tn_por_guia(monkeypatch):
    _mocks(monkeypatch)
    _sp(monkeypatch)
    with TestClient(app) as c:
        _login(c)
        q = lambda cod: c.get(f"/despacho/api/vivo/envio?tipo=tiendanube&codigo={cod}").json()
        r = q("SP000111")
        assert r["motivo"] == "agregar" and r["paquete"]["trackingId"] == "7004" and r["guia"] == "SP000111"
        assert q("SP000333")["motivo"] == "cancelada"
        r = q("SP000222")
        assert r["motivo"] == "no_existe" and "no coincide" in r["mensaje"]
        assert q("NOEXISTE")["motivo"] == "no_existe"
        assert q("7001")["motivo"] == "no_lista"            # el nº de orden sigue funcionando
        # ya despachada hoy → no se vuelve a cargar
        c.post("/despacho/api/vivo/escaneado", json={"tipo": "tiendanube", "tracking": "7004", "venta": "7004"})
        assert q("SP000111")["motivo"] == "ya_escaneada"


def test_conexiones_smartpost(monkeypatch):
    _mocks(monkeypatch)
    _sp(monkeypatch)
    with TestClient(app) as c:
        _login(c)
        r = c.post("/admin/api/conexiones", json={"valores": {"sp_token": "tok-123456789", "sp_despacho": "1"}}).json()
        assert r["ok"] and r["aj"]["sp_token"]["cargado"] and r["aj"]["sp_token"]["valor"].startswith("•")
        assert "SmartPost" in c.get("/admin/conexiones").text
        p = c.post("/admin/api/conexiones/probar/smartpost").json()
        assert p["ok"] and "MAGONTEX" in p["mensaje"]
        g = c.get("/admin/api/conexiones/smartpost/guia?codigo=SP000111").json()
        assert g["ok"] and g["orden"] == "7004" and g["via"] == "remito"
        assert c.get("/admin/api/conexiones/smartpost/guia?codigo=NOEXISTE").json()["encontrada"] is False
    ajustes.olvidar()


# ─────────────── Panel web (Mis Pedidos + guías del día) ───────────────
PEDIDOS = {"total": 3, "rows": [
    {"tnb_order_number": 7001, "retiro_id": 6000001, "estadoNombre": "SOLICITUD CONFIRMADA", "cancelado": 0, "empaquetado": 0},
    {"tnb_order_number": 7004, "retiro_id": 6000004, "estadoNombre": "ENTREGADA", "cancelado": 0, "empaquetado": 0},
    {"tnb_order_number": 7003, "retiro_id": 6000003, "estadoNombre": "SOLICITUD CONFIRMADA", "cancelado": 0, "empaquetado": 0},
]}
GUIAS_HOY = {"total": 3, "rows": [
    {"id": 6000001, "remito": "01M4X7ED0ZAV", "canal": "TN", "estado": "PEND-SOLICITUD CONFIRMADA", "comprador_apellido_nombre": "Cliente Uno"},
    {"id": 6000009, "remito": "R-55", "canal": "API", "estado": "PEND-SOLICITUD CONFIRMADA", "comprador_apellido_nombre": "Juan Pérez"},
    {"id": 6000010, "remito": "CAM-0003", "canal": "API", "estado": "PEND-SOLICITUD CONFIRMADA", "comprador_apellido_nombre": "Cambio"},
]}


def _panel(monkeypatch, llamadas=None):
    def panel_json(ruta, datos):
        if llamadas is not None:
            llamadas.append((ruta, datos))
        return PEDIDOS if "pedidos" in ruta else GUIAS_HOY
    monkeypatch.setattr(smartpost, "_panel_json", panel_json)
    monkeypatch.setattr(smartpost, "panel_configurado", lambda *a: True)
    monkeypatch.setattr(smartpost, "configurado", lambda *a: False)
    smartpost.olvidar()
    from app.services import ventas_api
    ventas_api._tn_ab.update(ts=0.0, data=None)


def test_csrf_del_login():
    assert smartpost._csrf_de('<meta name="csrf-token" content="abc123">') == "abc123"
    assert smartpost._csrf_de('<input type="hidden" name="_token" value="zzz">') == "zzz"
    assert smartpost.pendiente("PEND-SOLICITUD CONFIRMADA") and not smartpost.pendiente("ENTREGADA")


def test_despacho_tn_desde_panel(monkeypatch):
    _mocks(monkeypatch)
    llamadas = []
    _panel(monkeypatch, llamadas)
    with TestClient(app) as c:
        _login(c)
        d = c.get("/despacho/api/vivo?tipo=tiendanube&refrescar=1").json()
        assert d["ok"] and d["fuente"] == "smartpost"
        ids = [p["trackingId"] for p in d["paquetes"]]
        assert ids == ["7001", "SP-6000009"]          # 7004 entregada, 7003 cancelada en TN, CAM va por Cambios
        p = d["paquetes"][0]
        assert p["tnTracking"] == "6000001" and p["ordenes"] == ["6000001"] and p["orderNum"] == 7001
        assert d["paquetes"][1]["comprador"] == "Juan Pérez" and d["paquetes"][1]["orderNum"] == 0
        fecha = [x for x in llamadas if x[0] == "/guias/filter"][0][1]["fecha_remito"]
        assert " - " in fecha and fecha.count("/") == 4
        q = lambda cod: c.get(f"/despacho/api/vivo/envio?tipo=tiendanube&codigo={cod}").json()
        r = q("6000001")
        assert r["motivo"] == "agregar" and r["paquete"]["trackingId"] == "7001" and r["guia_via"] == "panel"
        r = q("6000009")
        assert r["motivo"] == "agregar" and r["paquete"]["trackingId"] == "SP-6000009"
        assert q("6000004")["motivo"] == "no_lista"     # la guía ya figura entregada
        assert q("6000003")["motivo"] == "cancelada"
        assert q("7001")["motivo"] == "agregar"          # el nº de orden también sirve
        g = c.get("/admin/api/conexiones/smartpost/guia?codigo=6000001").json()
        assert g["orden"] == "7001" and g["via"] == "panel"
        p = c.post("/admin/api/conexiones/probar/smartpost").json()
        assert p["ok"] and "Panel ✓" in p["mensaje"]
