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
    smartpost.olvidar()


def test_leer_guia_y_numero_de_orden(monkeypatch):
    _sp(monkeypatch)
    g = smartpost.leer_guia("SP000111")
    assert g["encontrada"] and g["remitos"] == ["#7004"] and g["nombre"] == "Ana Gómez" and g["estado"] == "En sucursal"
    assert smartpost.numeros_orden(["#7004", "TN-007003"]) == ["7004", "7003"]
    assert not smartpost.leer_guia("NOEXISTE")["encontrada"]


def test_match_por_nombre():
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
