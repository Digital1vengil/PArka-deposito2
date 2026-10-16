import base64
import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.parsers import leer_archivo, articulos_desde_tabla
from app.services import drive
from tests.test_parsers import ZPL

TN_CSV = ("Número de orden;Email;Fecha;Nombre del comprador;Nombre para el envío;Nombre del producto;SKU;"
          "Cantidad del producto;Código de tracking del envío;Medio de envío\n"
          "2051;a@x.com;30/09;Ana Gómez;No informado;Buzo Gris;BZ-1;1;=\"TN123456789AR\";Correo\n"
          "2051;a@x.com;30/09;Ana Gómez;No informado;Gorro;GR-2;2;;Correo\n"
          "2052;b@x.com;30/09;Luis;Luis Díaz;Campera;CP-3;1;TN999888777AR;Correo\n").encode("cp1252")


def test_tiendanube_como_scanner():
    r = leer_archivo("ventas.csv", TN_CSV, tipo="tiendanube")
    assert r["formato"] == "tiendanube"
    a = r["paquetes"][0]
    assert a["tracking"] == "2051" and a["cantidad"] == 3 and a["tracking_alt"] == "TN123456789AR"
    assert a["producto"] == "Buzo Gris + Gorro" and a["comprador"] == "Ana Gómez"
    assert r["paquetes"][1]["comprador"] == "Luis Díaz"


def test_articulos_tabla():
    csv = "codigo_barras,sku,nombre\n2000000000909,1-CAMI-M-00AXE-NVST-XXL-NOBR,\"00AXE (Navystripes, XXL)\"\n".encode()
    a = articulos_desde_tabla("a.csv", csv)[0]
    assert a["talle"] == "XXL" and a["color"] == "NVST" and a["articulo"] == "00AXE"


class FakeGAS:
    def __init__(self):
        self.posts = []

    def get(self, action, timeout=60, **p):
        if action == "listFiles":
            return {"rootFolderUrl": "https://drive/x", "groups": [
                {"date": "2026-09-30", "folderId": "f", "files": [
                    {"id": "zpl1", "name": "Etiqueta de envio.txt", "size": 10, "modifiedAt": "2026-09-30T12:00:00Z"},
                    {"id": "tn1", "name": "ventas.csv", "size": 10, "modifiedAt": "2026-09-30T12:00:00Z"}]}]}
        if action == "getFile":
            if p["fileId"] == "zpl1":
                return {"type": "text", "name": "Etiqueta de envio.txt", "content": ZPL}
            return {"type": "base64", "name": "ventas.csv", "content": base64.b64encode(TN_CSV).decode()}
        raise AssertionError(action)

    def post(self, action, data=None, tipo=None, timeout=120, **extra):
        self.posts.append((action, tipo, data))
        if action == "procesarExcel":
            return {"ok": True, "fileName": f"Despacho_{tipo}.xlsx", "fileUrl": "u", "count": len(data["datos"])}
        if action == "cerrarDia":
            return {"ok": True, "count": 3, "mail": "gerencia@magontex.com.ar", "despachados": 2, "noDespachados": 1}
        if action == "getConfig":
            return {"mail": "gerencia@magontex.com.ar", "copia": "", "hora": 17, "avisar": False}
        raise AssertionError(action)


@pytest.fixture
def gas(monkeypatch):
    f = FakeGAS()
    monkeypatch.setattr(drive, "gas_get", f.get)
    monkeypatch.setattr(drive, "gas_post", f.post)
    return f


def _login(c, nombre="Vengil", pin="1234"):
    uid = re.search(r"elegir\((\d+), '" + nombre + r"'\)", c.get("/login").text).group(1)
    assert c.post("/login", data={"usuario_id": uid, "pin": pin}).json()["ok"]


def test_drive_de_punta_a_punta(gas, monkeypatch):
    from app.services import re_drive
    mails = []
    monkeypatch.setattr(re_drive, "configurado", lambda db=None: True)
    monkeypatch.setattr(re_drive, "guardar_deposito", lambda db, fecha, archivos: {"archivos": []})
    monkeypatch.setattr(re_drive, "enviar_mail", lambda db, para, *a, **k: mails.append(para) or {})
    with TestClient(app) as c:
        _login(c)
        d = c.get("/despacho/api/drive/archivos?tipo=flex").json()
        assert d["grupos"][0]["files"][0]["cargado"] is False
        pv = c.post("/despacho/api/drive/preview", json={"ids": ["zpl1"], "tipo": "flex"}).json()
        assert pv["resultados"][0]["cantidad"] == 2
        r = c.post("/despacho/api/drive/lotes", json={"ids": ["zpl1"], "tipo": "flex", "nombre": "Flex Drive"}).json()
        assert r["cantidad"] == 2
        r2 = c.post("/despacho/api/drive/lotes", json={"ids": ["tn1"], "tipo": "tiendanube"}).json()
        assert r2["cantidad"] == 2
        assert c.get("/despacho/api/drive/archivos?tipo=flex").json()["grupos"][0]["files"][0]["cargado"] is True

        # escaneo TN por código de tracking del envío
        assert c.post("/despacho/api/tanda/iniciar").json()["ok"]
        assert c.post("/despacho/api/scan", json={"codigo": "TN123456789AR"}).json()["resultado"] == "ok"
        assert c.post("/despacho/api/scan", json={"codigo": "47383333425"}).json()["resultado"] == "ok"
        fin = c.post("/despacho/api/tanda/finalizar").json()
        assert fin["drive_error"] == "" and {x["tipo"] for x in fin["drive"]} == {"flex", "tiendanube"}
        env = [p for p in gas.posts if p[0] == "procesarExcel"]
        assert all(i["Estado"] == "despachado" for _, _, d in env for i in d["datos"])
        assert c.get("/despacho/api/estado").json()["sin_drive"] == 0

        # cierre manual: informa pendientes al Sheet una sola vez y manda el mail NUEVO (nunca el cerrarDia viejo)
        gas.posts.clear()
        r = c.post("/admin/api/google/cerrar").json()
        assert r["ok"] and r["despachados"] == 2 and mails == ["gerencia@magontex.com.ar"]
        pend = [p for p in gas.posts if p[0] == "procesarExcel"]
        assert pend and all(i["Estado"] == "pendiente" for _, _, d in pend for i in d["datos"])
        assert "cerrarDia" not in [p[0] for p in gas.posts]
        gas.posts.clear()
        c.post("/admin/api/google/informar")
        assert not [p for p in gas.posts if p[0] == "procesarExcel"]  # no se duplican en LogDia
        assert c.get("/admin/api/google/config").json()["hora"] == 17
        assert c.get("/admin/cierre").status_code == 200


def test_articulos_seccion():
    with TestClient(app) as c:
        _login(c)
        assert c.get("/s/articulos", follow_redirects=False).headers["location"] == "/articulos"
        assert c.get("/s/tareas", follow_redirects=False).headers["location"] == "/tareas"
        r = c.get("/articulos/api/buscar?q=2000000000909").json()
        assert r["exacto"] and r["resultados"][0]["sku"] == "1-CAMI-M-00AXE-NVST-XXL-NOBR"
        r = c.get("/articulos/api/buscar?q=00axe navystripes").json()
        assert len(r["resultados"]) >= 5
        assert c.get("/articulos").status_code == 200
        # tareas no se pueden crear en Artículos
        assert c.post("/tareas/api", json={"seccion": "articulos", "titulo": "x"}).status_code == 403


def test_panel_parka_quitado():
    with TestClient(app) as c:
        _login(c)
        assert c.get("/panel").status_code == 404
        assert 'href="/panel"' not in c.get("/").text and 'href="/s/panel"' not in c.get("/").text
        assert c.post("/admin/api/usuarios", json={"nombre": "Op", "rol": "despacho", "pin": "2222"}).json()["ok"]
    with TestClient(app) as op:
        _login(op, "Op", "2222")
        assert op.get("/s/panel").status_code == 403


def test_despacho_es_el_scanner():
    with TestClient(app) as c:
        assert c.get("/despacho/scanner/", follow_redirects=False).status_code == 303  # sin login
        _login(c)
        r = c.get("/despacho/scanner/")
        assert r.status_code == 200 and "AKfycbwP_qnW67" in r.text  # misma URL del Apps Script
        assert c.get("/despacho/scanner/styles.css").status_code == 200
        assert c.get("/despacho/scanner/../main.py").status_code == 404
        assert 'src="/despacho/scanner/"' in c.get("/despacho").text
        assert c.get("/despacho/lotes", follow_redirects=False).headers["location"] == "/despacho"
        assert "beta" not in c.get("/despacho").text
    with TestClient(app) as op:
        _login(op, "Op", "2222")
        assert op.get("/despacho/scanner/").status_code == 200
        assert op.get("/despacho/lotes", follow_redirects=False).status_code == 303


def test_accesos_por_usuario():
    with TestClient(app) as adm:
        _login(adm)
        assert adm.post("/admin/api/usuarios", json={"nombre": "Lu", "rol": "despacho", "pin": "3333"}).json()["ok"]
        html = adm.get("/admin/usuarios").text
        uid = int(re.search(r'<tr data-id="(\d+)">\s*<td><input class="input !py-1.5 f-nombre min-w-\[120px\]" value="Lu"', html).group(1))
        d = adm.get(f"/admin/api/usuarios/{uid}/accesos").json()
        assert d["personalizado"] is False and "despacho" in d["accesos"] and "supervision" not in d["accesos"]
    with TestClient(app) as lu:
        _login(lu, "Lu", "3333")
        assert lu.get("/supervision").status_code == 403
        assert lu.get("/ml").status_code == 403
        assert lu.get("/articulos").status_code == 200
    with TestClient(app) as adm:
        _login(adm)
        # le saco Artículos y le agrego Supervisión y Mercado Libre
        r = adm.post(f"/admin/api/usuarios/{uid}/accesos", json={"accesos": ["despacho", "smartpost", "supervision", "ml", "panel", "inventado"]}).json()
        assert set(r["accesos"]) == {"despacho", "supervision", "ml"}
    with TestClient(app) as lu:
        _login(lu, "Lu", "3333")
        assert lu.get("/supervision").status_code == 200
        assert lu.get("/ml").status_code == 200
        assert lu.get("/articulos").status_code == 403
        assert lu.get("/tareas/api/lista").json()["secciones"][0]["slug"] == "despacho"
        home = lu.get("/").text
        assert 'href="/supervision"' in home and 'href="/articulos"' not in home and 'href="/admin/usuarios"' not in home
        assert lu.get("/admin/usuarios").status_code == 403
    with TestClient(app) as adm:
        _login(adm)
        assert adm.post(f"/admin/api/usuarios/{uid}/accesos", json={"accesos": None}).json()["ok"]
        assert adm.get(f"/admin/api/usuarios/{uid}/accesos").json()["personalizado"] is False
        me = re.search(r'elegir\((\d+), \'Vengil\'\)', adm.get("/login").text)


def test_sin_seccion_smartpost():
    with TestClient(app) as c:
        _login(c)
        assert c.get("/smartpost", follow_redirects=False).headers["location"] == "/despacho"
        assert 'href="/smartpost"' not in c.get("/").text and "/s/smartpost" not in c.get("/").text
