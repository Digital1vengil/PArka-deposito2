"""Despacho en vivo (sin Drive): Flex / Colecta = ML "Listas para despachar" de hoy; Tienda Nube = "Por enviar";
canceladas para avisar; etiqueta escaneada fuera de la lista → qué es en ML / TN."""
from fastapi.testclient import TestClient

from app.main import app
from app.services import meli, ventas_api
from tests import test_ventas_api as tv
from tests.test_tiendanube import _login


def _mocks(monkeypatch, llamadas=None):
    tv._mocks(monkeypatch, [] if llamadas is None else llamadas)
    ventas_api._tn_env.update(ts=0.0, data=None)
    for c in ventas_api._canc.values():
        c.update(ts=0.0, data=None)


def test_flex_y_colecta_solo_listas_de_hoy(monkeypatch):
    _mocks(monkeypatch)
    with TestClient(app) as c:
        _login(c)
        f = c.get("/despacho/api/vivo?tipo=flex").json()
        assert f["ok"] and [p["trackingId"] for p in f["paquetes"]] == ["102"]   # la 101 está por imprimir
        assert f["por_imprimir"] == 1 and f["proximos"] == 0
        p = f["paquetes"][0]
        assert p["estado"] == "pendiente" and p["vivo"] and p["ventaId"] == "2" and p["ordenes"] == ["2"]
        assert [x["trackingId"] for x in f["canceladas"]] == []                  # la 109 es Colecta
        col = c.get("/despacho/api/vivo?tipo=colecta").json()
        assert [p["trackingId"] for p in col["paquetes"]] == ["103"]             # la 108 es de mañana
        assert col["proximos"] == 1 and col["paquetes"][0]["ventaId"] == "999" and col["paquetes"][0]["cantidad"] == 2
        assert sorted(col["paquetes"][0]["ordenes"]) == ["3", "4"]
        assert [x["trackingId"] for x in col["canceladas"]] == ["109"]
        assert c.get("/despacho/api/vivo/canceladas?tipo=colecta").json()["canceladas"][0]["ventaId"] == "20"
        assert c.get("/despacho/api/vivo/canceladas?tipo=flex").json()["canceladas"] == []


def test_tiendanube_por_enviar(monkeypatch):
    _mocks(monkeypatch)
    with TestClient(app) as c:
        _login(c)
        d = c.get("/despacho/api/vivo?tipo=tiendanube").json()
        assert d["ok"] and [p["trackingId"] for p in d["paquetes"]] == ["7004"]   # 7001 todavía por empaquetar
        assert d["paquetes"][0]["orderNum"] == 7004


def test_explicar_etiqueta_fuera_de_lista(monkeypatch):
    _mocks(monkeypatch)
    with TestClient(app) as c:
        _login(c)
        q = lambda cod, t="flex": c.get(f"/despacho/api/vivo/envio?tipo={t}&codigo={cod}").json()
        assert q("104")["motivo"] == "cancelada"
        assert q("103")["motivo"] == "otro_tipo" and "COLECTA" in q("103")["mensaje"]
        assert q("101")["motivo"] == "no_lista"                                  # etiqueta por imprimir
        assert q("108", "colecta")["motivo"] == "otro_dia"
        assert q("106", "colecta")["motivo"] == "ya_salio"
        assert q("105")["motivo"] == "otra_logistica"
        r = q("102")
        assert r["motivo"] == "agregar" and r["paquete"]["trackingId"] == "102"
        assert q("7001", "tiendanube")["motivo"] == "no_lista"
        assert q("7003", "tiendanube")["motivo"] == "cancelada"


def test_cancelada_ya_escaneada_crea_tarea(monkeypatch):
    _mocks(monkeypatch)
    with TestClient(app) as c:
        _login(c)
        body = {"tipo": "colecta", "tracking": "109", "venta": "20", "comprador": "X", "ya_escaneada": True}
        assert c.post("/despacho/api/vivo/cancelada-vista", json=body).json()["ok"]
        assert c.post("/despacho/api/vivo/cancelada-vista", json=body).json()["ok"]   # no duplica
        from sqlalchemy import select
        from app.db import SessionLocal
        from app.models import Tarea
        db = SessionLocal()
        try:
            ts = db.scalars(select(Tarea).where(Tarea.ref == "cancelada:109")).all()
            assert len(ts) == 1 and ts[0].prioridad == "alta" and ts[0].seccion_slug == "despacho"
        finally:
            db.close()


def test_sin_conexion(monkeypatch):
    _mocks(monkeypatch)
    monkeypatch.setattr(meli, "configurado", lambda: False)
    with TestClient(app) as c:
        _login(c)
        d = c.get("/despacho/api/vivo?tipo=flex").json()
        assert not d["ok"] and d["conectado"] is False and "Conexiones" in d["error"]
        assert c.get("/despacho/api/vivo?tipo=otro").status_code == 400


def test_scanner_carga_despacho_vivo():
    with TestClient(app) as c:
        _login(c)
        assert "despacho-vivo.js" in c.get("/despacho/scanner/").text
        js = c.get("/despacho/scanner/despacho-vivo.js")
        assert js.status_code == 200 and "/despacho/api/vivo" in js.text


def test_escaneados_hoy_en_verde_y_no_se_recargan(monkeypatch):
    _mocks(monkeypatch)
    with TestClient(app) as c:
        _login(c)
        f = c.get("/despacho/api/vivo?tipo=flex").json()
        assert f["paquetes"][0]["escaneado"] is False and f["escaneados"] == 0
        assert c.post("/despacho/api/vivo/escaneado", json={"tipo": "flex", "tracking": "102", "venta": "2"}).json()["ok"]
        c.post("/despacho/api/vivo/escaneado", json={"tipo": "flex", "tracking": "102", "venta": "2"})   # idempotente
        f = c.get("/despacho/api/vivo?tipo=flex").json()
        assert f["paquetes"][0]["escaneado"] is True and f["paquetes"][0]["escaneadoHora"] and f["escaneados"] == 1
        # en Colecta no cuenta
        assert c.get("/despacho/api/vivo?tipo=colecta").json()["escaneados"] == 0
        # escanear de nuevo esa etiqueta (fuera de la lista cargada): avisa que ya salió, no la suma
        r = c.get("/despacho/api/vivo/envio?tipo=flex&codigo=102").json()
        assert r["motivo"] == "ya_escaneada"
        assert c.get("/despacho/api/vivo/envio?tipo=flex&codigo=2").json()["motivo"] == "ya_escaneada"
        # desmarcar → vuelve a estar pendiente
        c.post("/despacho/api/vivo/escaneado/quitar", json={"tipo": "flex", "tracking": "102"})
        f = c.get("/despacho/api/vivo?tipo=flex").json()
        assert f["paquetes"][0]["escaneado"] is False
        assert c.post("/despacho/api/vivo/escaneado", json={"tipo": "xx", "tracking": "1"}).status_code == 400


def test_sin_aviso_por_whatsapp(monkeypatch):
    _mocks(monkeypatch)
    with TestClient(app) as c:
        _login(c)
        assert c.get("/despacho/api/whatsapp/resumen").status_code == 404
        assert "whatsapp-despacho.js" not in c.get("/despacho/scanner/index.html").text
