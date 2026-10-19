import json
import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import devoluciones as svc
from app.services.etiquetas import pdf_etiquetas, ean13_valido


def _login(c, nombre="Vengil", pin="1234"):
    uid = re.search(r"elegir\((\d+), '" + nombre + r"'\)", c.get("/login").text).group(1)
    assert c.post("/login", data={"usuario_id": uid, "pin": pin}).json()["ok"]


def test_ean_y_pdf():
    assert ean13_valido("2000000000909") and not ean13_valido("2000000000900")
    pdf = pdf_etiquetas([{"articulo": "00AXE", "color": "Navystripes", "talle": "XXL", "sku": "1-CAMI-M-00AXE-NVST-XXL-NOBR",
                          "codigo_barras": "2000000000909"}] * 45, desde=3, guias=True)
    assert pdf[:4] == b"%PDF" and pdf.count(b"/Type /Page\n") + pdf.count(b"/Type /Page ") >= 2


def test_flujo_devolucion(monkeypatch):
    enviados = []

    def fake_enviar(dev):
        enviados.append(dev)
        return {"ok": True, "id": "D-001", "credito": "Crédito D-001", "reingreso": "Reingreso D-001", "total": dev.total}
    monkeypatch.setattr(svc, "enviar", fake_enviar)
    with TestClient(app) as c:
        _login(c)
        assert "Devoluciones" in c.get("/").text
        # solo Parka o Puffers
        r = c.get("/devoluciones/api/buscar?q=abba 02003").json()["modelos"]
        m = next(x for x in r if x["articulo"] == "ABBA 02003")
        assert {v["marca"] for v in m["variantes"]} == {"Parka"}
        assert all(v["sku"].endswith("-PARK") for v in m["variantes"])
        assert c.get("/devoluciones/api/buscar?q=00axe").json()["modelos"] == []   # 00AXE es sin marca
        v = next(x for x in m["variantes"] if x["color"] == "BLACK" and x["talle"] == "M")
        assert v["codigo_barras"] == "2000000035925"
        r2 = c.get("/devoluciones/api/buscar?q=2000000035925").json()["modelos"]
        assert r2[0]["preseleccion"] == v["id"]
        assert c.get("/devoluciones/api/buscar?q=2000000035918").json()["modelos"] == []  # mismo talle, sin marca
        res = c.post("/devoluciones/api/confirmar", json={"plataforma": "ML", "items": [
            {"articulo_id": v["id"], "cantidad": 3, "estado": "OPTIMO", "venta": "2000012345678901"},
            {"articulo_id": v["id"], "cantidad": 1, "estado": "FALLA", "venta": "2000012345678901"}]}).json()
        d = res["devolucion"]
        assert d["drive_ok"] and d["total"] == 4 and d["drive"]["id"] == "D-001"
        assert enviados[0].items[1].estado == "FALLA" and enviados[0].items[0].talle == "M"
        pdf = c.get(f"/devoluciones/{d['id']}/etiquetas.pdf?desde=5")
        assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
        assert c.get("/devoluciones/api/historial").json()["devoluciones"][0]["id"] == d["id"]
        assert c.post("/devoluciones/api/confirmar", json={"plataforma": "ML", "items": []}).status_code == 400
        # un artículo sin marca no se acepta aunque manden el id a mano
        from app.db import SessionLocal
        from app.models import Articulo
        with SessionLocal() as db:
            nobr = db.query(Articulo).filter(Articulo.codigo_barras == "2000000035918").one().id
        assert c.post("/devoluciones/api/confirmar", json={"plataforma": "ML", "items": [{"articulo_id": nobr, "cantidad": 1}]}).status_code == 400
        # Puffers
        p = c.get("/devoluciones/api/buscar?q=axis 01155").json()["modelos"][0]
        assert "Puffers" in {x["marca"] for x in p["variantes"]} and {x["marca"] for x in p["variantes"]} <= {"Parka", "Puffers"}


def test_drive_falla_y_reintento(monkeypatch):
    def falla(dev):
        raise RuntimeError("sin conexión")
    monkeypatch.setattr(svc, "enviar", falla)
    with TestClient(app) as c:
        _login(c)
        v = c.get("/devoluciones/api/buscar?q=2000000035901").json()["modelos"][0]["variantes"][0]
        d = c.post("/devoluciones/api/confirmar", json={"plataforma": "ML", "items": [{"articulo_id": v["id"], "cantidad": 1, "estado": "OPTIMO", "venta": "2000012345678901"}]}).json()["devolucion"]
        assert not d["drive_ok"] and "sin conexión" in d["drive_error"]
        monkeypatch.setattr(svc, "enviar", lambda dev: {"ok": True, "id": "D-2"})
        assert c.post(f"/devoluciones/api/{d['id']}/reintentar").json()["devolucion"]["drive_ok"]


def test_color_talle_casos():
    from app.models import Articulo
    assert svc.color_talle(Articulo(variante="Navystripes, XXL", talle="XXL")) == ("Navystripes", "XXL")
    assert svc.color_talle(Articulo(variante="XL, Parka", talle="XL", color="BLCK")) == ("BLCK", "XL")
    assert svc.color_talle(Articulo(variante="parca", talle="")) == ("Único", "Único")
    assert svc.color_talle(Articulo(variante="BLACK, M, nobrand", talle="M")) == ("BLACK", "M")


def test_marca_parka_puffers():
    from app.models import Articulo
    assert svc.marca(Articulo(sku="1-CAMP-M-01155-GREN-L-PUFE", variante="Green, L, Puffers")) == "Puffers"
    assert svc.marca(Articulo(sku="1-CAMP-W-02003-BLCK-M-PARK", variante="BLACK, M, Parka")) == "Parka"
    assert svc.marca(Articulo(sku="1-CAMP-W-02003-BLCK-M-NOBR", variante="BLACK, M, nobrand")) == "No Brand"
    assert svc.color_talle(Articulo(variante="Green, L, Puffers", talle="L")) == ("Green", "L")


def test_celular_https(monkeypatch):
    with TestClient(app) as c:
        assert "no está activa" in c.get("/celular").text
        monkeypatch.setenv("PARKA_HTTPS_PUERTO", "8443")
        monkeypatch.setenv("PARKA_URL_CELULAR", "https://192.168.0.15:8443")
        r = c.get("/despacho?x=1", follow_redirects=False)       # un celular por http → https
        assert r.status_code == 307 and r.headers["location"] == "https://testserver:8443/despacho?x=1"
        assert c.get("/salud").json()["ok"]                      # el lanzador sigue viendo /salud por http
    with TestClient(app, base_url="https://testserver") as c:
        t = c.get("/celular?ir=/despacho").text
        assert "https://192.168.0.15:8443/despacho" in t and "<svg" in t


def test_etiquetas_html_ean_y_code128():
    from app.services.etiquetas_html import html_etiquetas, _svg_barras
    h = html_etiquetas([{"nombre": "ALPHA 01159 (BLACK, S, Parka)", "sku": "1-CAMP-M-01159-BLCK-S-PARK",
                         "codigo_barras": "2000000023496"}] * 41, desde=1)
    assert h.count('class="hoja"') == 2 and h.count('class="et') == 41
    assert "window.print()" in h and "2000000023496" in h
    assert "<rect" in _svg_barras("A-03-2")


def test_escaner_codigo_directo():
    """Escanear el código suma la variante sin escribir nada; avisa el motivo si no se puede."""
    with TestClient(app) as c:
        _login(c)
        r = c.get("/devoluciones/api/codigo?c=2000000035925").json()
        assert r["ok"] and r["variante"]["articulo"] == "ABBA 02003"
        assert r["variante"]["talle"] == "M" and r["variante"]["marca"] == "Parka"
        sku = r["variante"]["sku"]
        assert c.get(f"/devoluciones/api/codigo?c={sku}").json()["variante"]["id"] == r["variante"]["id"]
        nb = c.get("/devoluciones/api/codigo?c=2000000035918").json()   # mismo talle, sin marca
        assert not nb["ok"] and "Parka o Puffers" in nb["motivo"]
        no = c.get("/devoluciones/api/codigo?c=1234567890123").json()
        assert not no["ok"] and "no está en el catálogo" in no["motivo"]
        assert not c.get("/devoluciones/api/codigo?c=").json()["ok"]
        html = c.get("/devoluciones").text
        assert "Listo para escanear" in html and "html5-qrcode" in html


def test_plataforma_por_prenda(monkeypatch):
    enviados = []
    monkeypatch.setattr(svc, "enviar", lambda dev: enviados.append(dev) or {"ok": True, "id": "D-9"})
    with TestClient(app) as c:
        _login(c)
        v = c.get("/devoluciones/api/buscar?q=2000000035901").json()["modelos"][0]["variantes"][0]
        # sin plataforma (o una que no es TN/ML) no se guarda
        assert c.post("/devoluciones/api/confirmar", json={"items": [{"articulo_id": v["id"], "cantidad": 1}]}).status_code == 400
        assert c.post("/devoluciones/api/confirmar", json={"items": [
            {"articulo_id": v["id"], "cantidad": 1, "plataforma": "XX"}]}).status_code == 400
        # el estado (Óptimo / Falla) también es obligatorio: no hay valor por defecto
        r = c.post("/devoluciones/api/confirmar", json={"items": [{"articulo_id": v["id"], "cantidad": 1, "plataforma": "ML"}]})
        assert r.status_code == 400 and "estado" in r.json()["detail"]
        d = c.post("/devoluciones/api/confirmar", json={"items": [
            {"articulo_id": v["id"], "cantidad": 2, "estado": "OPTIMO", "plataforma": "tn", "venta": "#1234"},
            {"articulo_id": v["id"], "cantidad": 1, "estado": "FALLA", "plataforma": "ML", "venta": "2000012345678901"}]}).json()["devolucion"]
        assert d["plataforma"] == "ML+TN"
        assert [(i["plataforma"], i["estado"], i["cantidad"]) for i in d["items"]] == [("TN", "OPTIMO", 2), ("ML", "FALLA", 1)]
        # al Apps Script: plataforma por prenda (también en "sector" para que se vea en el Excel)
        import app.services.devoluciones as real
        dev = enviados[-1]
        assert [i.plataforma for i in dev.items] == ["TN", "ML"]
        h = c.get("/devoluciones/api/historial").json()["devoluciones"][0]
        assert h["id"] == d["id"] and h["items"][1]["plataforma"] == "ML"


def test_payload_sector_plataforma(monkeypatch):
    from app.models import Devolucion, DevolucionItem
    from datetime import datetime
    capt = {}

    class R:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return b'{"ok": true}'

    def fake_urlopen(req, timeout=0):
        capt["body"] = json.loads(req.data.decode())
        return R()
    monkeypatch.setattr(svc.urllib.request, "urlopen", fake_urlopen)
    d = Devolucion(id=5, creado_at=datetime(2026, 10, 8, 12), plataforma="ML+TN")
    d.items = [DevolucionItem(codigo_barras="1", articulo="A", color="B", talle="M", estado="OPTIMO", cantidad=1, plataforma="TN")]
    svc.enviar(d)
    it = capt["body"]["items"][0]
    assert it["plataforma"] == "TN" and it["sector"] == "TN"
    d.items[0].venta = "1234"
    svc.enviar(d)
    it = capt["body"]["items"][0]
    assert it["venta"] == "1234" and it["sector"] == "TN · 1234"


def test_busqueda_equivalencias():
    """W7 / w 7 / W-7 / w007 / 7 encuentran W-00007 (HOLY 00007); W2003 encuentra ABBA 02003."""
    assert svc.tokens_busqueda("w 7") == ["w7"]
    assert svc.tokens_busqueda("W - 7 negra") == ["W7", "negra"]
    with TestClient(app) as c:
        _login(c)
        for q in ("W7", "w7", "w 7", "W-7", "w-00007", "W007", "00007"):
            arts = [m["articulo"] for m in c.get("/conteo/api/buscar?q=" + q).json()["modelos"]]
            assert "HOLY 00007" in arts, q
        arts = [m["articulo"] for m in c.get("/devoluciones/api/buscar?q=W2003").json()["modelos"]]
        assert arts == ["ABBA 02003"]
        # una letra que no es de ningún SKU con ese número no trae nada raro
        assert c.get("/conteo/api/buscar?q=Q7").json()["modelos"] == []


def test_venta_obligatoria_por_prenda(monkeypatch):
    enviados = []
    monkeypatch.setattr(svc, "enviar", lambda dev: enviados.append(dev) or {"ok": True, "id": "D-10"})
    with TestClient(app) as c:
        _login(c)
        v = c.get("/devoluciones/api/buscar?q=2000000035901").json()["modelos"][0]["variantes"][0]
        r = c.post("/devoluciones/api/confirmar", json={"items": [
            {"articulo_id": v["id"], "cantidad": 1, "estado": "OPTIMO", "plataforma": "ML"}]})
        assert r.status_code == 400 and "número de venta" in r.json()["detail"]
        d = c.post("/devoluciones/api/confirmar", json={"items": [
            {"articulo_id": v["id"], "cantidad": 1, "estado": "OPTIMO", "plataforma": "ML", "venta": "2000099998888777"},
            {"articulo_id": v["id"], "cantidad": 1, "estado": "OPTIMO", "plataforma": "TN", "venta": "#5521"}]}).json()["devolucion"]
        assert [i["venta"] for i in d["items"]] == ["2000099998888777", "5521"]
        assert d["ventas"] == ["ML 2000099998888777", "TN 5521"]
        assert enviados[-1].items[1].venta == "5521"
        html = c.get("/devoluciones").text
        assert "venta-num" in html and "Escanear etiqueta" in html


def test_codigo_de_etiqueta():
    f = svc.codigo_de_etiqueta
    assert f('{"id":"47383333425","t":"lm"}') == "47383333425"
    assert f('LA,{"id":"47383333425","t":"lm"}') == "47383333425"
    assert f('¨id¨Ñ¨47383333425¨,¨t¨Ñ¨lm¨*') == "47383333425"   # QR leído con el teclado en español
    assert f("]C047383333425") == "47383333425" and f(">:47383333425") == "47383333425"
    assert f(" #1234 ") == "1234" and f("2000 0123 4567 8901") == "2000012345678901"


def test_buscar_venta_ml(monkeypatch):
    from app.services import meli
    llamadas = []
    datos = {
        "/shipments/47383333425": {"id": 47383333425, "order_id": 2000011111111111},
        "/orders/2000011111111111": {"id": 2000011111111111, "status": "paid", "date_created": "2026-10-10T12:00:00-04:00",
                                     "buyer": {"nickname": "COMPRADOR1"}, "pack_id": None, "shipping": {"id": 47383333425},
                                     "order_items": [{"quantity": 1, "item": {"title": "Campera Abba 02003 Negra", "seller_sku": "1-CAMP-W-02003-BLK-M-PARK",
                                                      "variation_attributes": [{"name": "Color", "value_name": "Negro"}, {"name": "Talle", "value_name": "M"}]}}]},
        "/orders/2000022222222222": {"id": 2000022222222222, "status": "paid", "pack_id": 2000033333333333,
                                     "date_created": "2026-10-11T10:00:00-04:00", "order_items": []},
        "/orders/2000044444444444": {"id": 2000044444444444, "status": "cancelled", "pack_id": 2000033333333333, "order_items": []},
        "/packs/2000033333333333": {"orders": [{"id": 2000022222222222}, {"id": 2000044444444444}]},
    }

    def fake_get(path, api_version=None):
        llamadas.append(path)
        if path in datos:
            return datos[path]
        raise meli.MeliError("no", 404)
    monkeypatch.setattr(meli, "configurado", lambda: True)
    monkeypatch.setattr(meli, "get", fake_get)
    with TestClient(app) as c:
        _login(c)
        r = c.get("/devoluciones/api/venta", params={"plataforma": "ML", "c": 'LA,{"id":"47383333425","t":"lm"}'}).json()
        assert r["ok"] and r["numero"] == "2000011111111111" and r["envio"] == "47383333425" and r["fecha"] == "10/10/2026"
        assert r["items"][0]["color"] == "Negro" and r["items"][0]["talle"] == "M" and r["comprador"] == "COMPRADOR1"
        # venta de un carrito → se muestra el nº del pack con todas sus ventas
        r = c.get("/devoluciones/api/venta", params={"plataforma": "ML", "c": "2000022222222222"}).json()
        assert r["ok"] and r["numero"] == "2000033333333333" and len(r["ventas"]) == 2 and not r["cancelada"]
        # el nº del pack también sirve
        assert c.get("/devoluciones/api/venta", params={"plataforma": "ML", "c": "2000033333333333"}).json()["numero"] == "2000033333333333"
        r = c.get("/devoluciones/api/venta", params={"plataforma": "ML", "c": "2000099999999999"}).json()
        assert not r["ok"] and not r["sin_conexion"]
        assert not c.get("/devoluciones/api/venta", params={"plataforma": "", "c": "1"}).json()["ok"]
    monkeypatch.setattr(meli, "configurado", lambda: False)
    with TestClient(app) as c:
        _login(c)
        r = c.get("/devoluciones/api/venta", params={"plataforma": "ML", "c": "2000011111111111"}).json()
        assert not r["ok"] and r["sin_conexion"] and r["codigo"] == "2000011111111111"


def test_buscar_venta_tn(monkeypatch):
    from app.services import tiendanube as tn
    vistos = []
    orden = {"id": 99, "number": 1234, "status": "closed", "shipping_status": "delivered", "created_at": "2026-10-05T09:00:00-03:00",
             "contact_name": "Ana Perez", "products": [{"name": "Campera Holy (Negro, L)", "sku": "1-CAMP-W-00007-BLK-L-PARK", "quantity": 1}]}

    def fake_get(path, **params):
        vistos.append(params)
        return [orden] if params.get("q") == "1234" else []
    monkeypatch.setattr(tn, "configurado", lambda db=None: True)
    monkeypatch.setattr(tn, "get", fake_get)
    with TestClient(app) as c:
        _login(c)
        r = c.get("/devoluciones/api/venta", params={"plataforma": "TN", "c": "#1234"}).json()
        assert r["ok"] and r["numero"] == "1234" and r["fecha"] == "05/10/2026" and "entregada" in r["estado"]
        assert r["items"][0]["sku"] == "1-CAMP-W-00007-BLK-L-PARK" and r["comprador"] == "Ana Perez"
        assert vistos[0]["status"] == "any"     # también órdenes archivadas
        r = c.get("/devoluciones/api/venta", params={"plataforma": "TN", "c": "777"}).json()
        assert not r["ok"] and "777" in r["motivo"]


def test_mismo_distinto_y_ajeno(monkeypatch):
    """Venta encontrada → cada prenda trae su variante del catálogo (por SKU) para sumarla con Óptimo / Falla;
    un artículo que no vendemos se guarda sin artículo, no va al Apps Script ni lleva etiqueta."""
    from app.services import tiendanube as tn
    enviados = []
    monkeypatch.setattr(svc, "enviar", lambda dev: enviados.append([i.estado for i in dev.items]) or {"ok": True, "id": "D-11"})
    with TestClient(app) as c:
        _login(c)
        v = c.get("/devoluciones/api/buscar?q=2000000035925").json()["modelos"][0]["variantes"]
        v = next(x for x in v if x["codigo_barras"] == "2000000035925")
        orden = {"id": 5, "number": 1264, "status": "closed", "products": [
            {"name": "Campera ABBA (Negro, M)", "sku": v["sku"], "quantity": 1},
            {"name": "Otra cosa", "sku": "", "quantity": 1}]}
        monkeypatch.setattr(tn, "configurado", lambda db=None: True)
        monkeypatch.setattr(tn, "get", lambda path, **p: [orden])
        r = c.get("/devoluciones/api/venta", params={"plataforma": "TN", "c": "1264"}).json()
        assert r["items"][0]["variante"]["id"] == v["id"] and r["items"][0]["motivo"] == ""
        assert r["items"][1]["variante"] is None and "SKU" in r["items"][1]["motivo"]
        # artículo que no vendemos: hace falta la descripción
        mal = c.post("/devoluciones/api/confirmar", json={"items": [
            {"cantidad": 1, "estado": "AJENO", "plataforma": "TN", "venta": "1264"}]})
        assert mal.status_code == 400
        d = c.post("/devoluciones/api/confirmar", json={"items": [
            {"articulo_id": v["id"], "cantidad": 1, "estado": "FALLA", "plataforma": "TN", "venta": "1264", "llego": "MISMO"},
            {"cantidad": 2, "estado": "AJENO", "descripcion": "Buzo gris  otra marca", "plataforma": "TN", "venta": "1264"}]}).json()["devolucion"]
        assert [(i["estado"], i["llego"], i["articulo"]) for i in d["items"]] == [
            ("FALLA", "MISMO", "ABBA 02003"), ("AJENO", "AJENO", "Buzo gris otra marca")]
        # etiquetas: solo la prenda nuestra
        html = c.get(f"/devoluciones/{d['id']}/etiquetas.html").text
        assert html.count("2000000035925") >= 1 and "Buzo gris" not in html
    # al Apps Script no va el artículo ajeno
    from app.models import Devolucion, DevolucionItem
    from datetime import datetime
    capt = {}

    class R:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return b'{"ok": true}'
    monkeypatch.undo()
    monkeypatch.setattr(svc.urllib.request, "urlopen", lambda req, timeout=0: capt.setdefault("b", json.loads(req.data)) and R() or R())
    dv = Devolucion(id=9, creado_at=datetime(2026, 10, 19, 12), plataforma="TN")
    dv.items = [DevolucionItem(codigo_barras="1", articulo="A", estado="OPTIMO", cantidad=1, plataforma="TN", venta="1"),
                DevolucionItem(articulo="Ajeno", estado="AJENO", cantidad=1, plataforma="TN", venta="1")]
    svc.enviar(dv)
    assert [i["articulo"] for i in capt["b"]["items"]] == ["A"]
    solo = Devolucion(id=10, creado_at=datetime(2026, 10, 19, 12), plataforma="TN")
    solo.items = [DevolucionItem(articulo="Ajeno", estado="AJENO", cantidad=1, plataforma="TN", venta="1")]
    assert svc.enviar(solo)["ok"]
