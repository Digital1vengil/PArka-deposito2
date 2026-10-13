"""Etiquetas en rollo Honeywell (ZPL 40x25 de a 2) — checklist de la especificación v1.1 + integración."""
import re
import socket
import threading

from fastapi.testclient import TestClient

from app.main import app
from app.services import etiquetas_rollo as rollo

BERM = [{"nombre": "Bermuda Plano 20023 (Blue, S, nobrand)", "sku": "1-BERM-M-20023-BLUE-S-NOBR", "codigo_barras": "2000000010793"},
        {"nombre": "Bermuda Plano 20023 (Blue, L, nobrand)", "sku": "1-BERM-M-20023-BLUE-L-NOBR", "codigo_barras": "2000000010779"}]


def _login(c, nombre="Vengil", pin="1234"):
    uid = re.search(r"elegir\((\d+), '" + nombre + r"'\)", c.get("/login").text).group(1)
    assert c.post("/login", data={"usuario_id": uid, "pin": pin}).json()["ok"]


def test_checklist_spec():
    z = rollo.build_zpl(BERM)
    assert "^PW824^LL200^LH80,0^MD5^PR3" in z
    assert "^BY2,2,88^FO53,60^BCN,88,N,N,N,A^FD2000000010793^FS" in z
    assert "^FO461,60^BCN" in z and "^FO424,156^FB320,1,0,C^A0N,24,22^FD2000000010779^FS" in z
    assert "^FDBermuda Plano 20023 (Blue, S, nobr^FS" in z            # truncado a 34
    # 123 módulos para 13 dígitos (incluye la barra final del STOP)
    assert len(rollo.code128("2000000010779")) == 123
    assert rollo.page_width_mm(rollo.DEFAULT_CFG) == 93
    h = rollo.build_html(BERM)
    assert "@page{size:93mm 25mm;margin:0}" in h and h.count('class="fila"') == 1


def test_impar_escapes_y_alfanumerico():
    tres = BERM + [{"nombre": "A^B~C", "sku": "", "codigo_barras": "ABCDEFGHIJKLMNOPQRSTU"}]
    z = rollo.build_zpl(tres)
    assert z.count("^XA") == 2
    ultima = z.split("^XA")[-1]
    assert "^FO424" not in ultima                # fila impar: solo la izquierda
    assert "^FDA B C^FS" in ultima and "^BY1," in ultima   # escape y módulo 1 si no entra
    assert "^FO16,34" not in ultima              # SKU vacío se omite
    assert rollo.build_zpl(BERM * 3, max_filas=1).count("^XA") == 1


def test_config_y_regla(monkeypatch):
    cfg = dict(rollo.DEFAULT_CFG, zx=-2)
    z = rollo.build_zpl(BERM, cfg)
    assert "^LH0,0^LS16" in z
    r = rollo.regla_zpl()
    assert r.startswith("^XA^CI28^PW832^LL200^LH0,0") and "^FD100^FS" in r


def _servidor():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1)
    recibido = bytearray()

    def run():
        con, _ = s.accept()
        while True:
            b = con.recv(65536)
            if not b:
                break
            recibido.extend(b)
        con.close(); s.close()
    t = threading.Thread(target=run, daemon=True); t.start()
    return s.getsockname()[1], recibido, t


def test_conteo_y_devolucion_rollo(monkeypatch):
    from app.services import devoluciones as svc
    monkeypatch.setattr(svc, "enviar", lambda d: {"ok": True, "id": "D-1"})
    with TestClient(app) as c:
        _login(c)
        m = c.get("/conteo/api/buscar?q=abba 02003").json()["modelos"]
        v = next(x for x in m if x["articulo"] == "ABBA 02003")["variantes"]
        ct = c.post("/conteo/api/confirmar", json={"items": [{"articulo_id": v[0]["id"], "cantidad": 3}]}).json()["conteo"]
        h = c.get(f"/conteo/{ct['id']}/etiquetas-rollo.html")
        assert h.status_code == 200 and h.text.count('class="fila"') == 2 and "window.print" in h.text
        assert c.get(f"/conteo/{ct['id']}/etiquetas-rollo.html?filas=1").text.count('class="fila"') == 1
        z = c.get(f"/conteo/{ct['id']}/etiquetas.zpl")
        assert z.status_code == 200 and z.text.count("^XA") == 2 and v[0]["codigo_barras"] in z.text
        assert not z.content.startswith(b"\xef\xbb\xbf")   # UTF-8 sin BOM

        # impresora de red simulada (IP:puerto) → recibe el ZPL
        puerto, recibido, t = _servidor()
        assert c.post("/admin/api/conexiones", json={"valores": {"rollo_impresora": f"127.0.0.1:{puerto}"}}).json()["ok"]
        r = c.post(f"/conteo/api/{ct['id']}/imprimir-rollo", json={}).json()
        t.join(3)
        assert r["ok"] and r["etiquetas"] == 3 and r["filas"] == 2
        assert recibido.decode().count("^XZ") == 2

        # devoluciones
        d = c.get("/devoluciones/api/buscar?q=abba 02003").json()["modelos"]
        par = next(x for x in d if x["articulo"] == "ABBA 02003")["variantes"][0]
        dv = c.post("/devoluciones/api/confirmar", json={"plataforma": "ML", "items": [{"articulo_id": par["id"], "cantidad": 2}]}).json()["devolucion"]
        puerto, recibido, t = _servidor()
        c.post("/admin/api/conexiones", json={"valores": {"rollo_impresora": f"127.0.0.1:{puerto}"}})
        r = c.post(f"/devoluciones/api/{dv['id']}/imprimir-rollo", json={"filas": 1}).json()
        t.join(3)
        assert r["etiquetas"] == 2 and recibido.decode().count("^XA") == 1
        assert c.get(f"/devoluciones/{dv['id']}/etiquetas-rollo.html").status_code == 200

        # impresora caída → 502 con mensaje (el navegador cae al driver)
        c.post("/admin/api/conexiones", json={"valores": {"rollo_impresora": "127.0.0.1:1"}})
        e = c.post(f"/devoluciones/api/{dv['id']}/imprimir-rollo", json={})
        assert e.status_code == 502 and "No se pudo conectar" in e.json()["detail"]
        assert c.post("/admin/api/conexiones/probar/rollo").json()["ok"]
        assert c.get("/api/etiquetas/rollo/estado").json()["ancho_pagina_mm"] == 93
        assert "Impresora de etiquetas" in c.get("/admin/conexiones").text
        assert c.get("/admin/etiquetas/rollo/regla.html").status_code == 200
        # las pantallas cargan el selector
        assert "imprimirEtiquetas('devoluciones'" in c.get("/devoluciones").text
        assert "imprimirEtiquetas('conteo'" in c.get("/conteo").text


def test_historial_de_otros_y_formato_fijo():
    with TestClient(app) as c:
        _login(c)
        h = c.get("/conteo/api/historial").json()["conteos"]
        assert h and "usuario_id" in h[0]
        assert "usuario_id" in c.get("/devoluciones/api/historial").json()["devoluciones"][0]
        pag = c.get("/conteo").text
        assert "const YO = " in pag and "setInterval" in pag
        js = c.get("/static/app.js").text
        assert "function formatoEtiquetas() { return 'rollo-driver'; }" in js
