"""Recolecciones Odoo: lista de WH/PICK abiertas, NUEVO hasta imprimir, etiquetas en rollo una por unidad."""
import re

from fastapi.testclient import TestClient

from app.main import app
from app.services import odoo, odoo_recolecciones as svc


class FakeCliente:
    def __init__(self):
        self.llamadas = []

    def campos(self):
        return odoo.CAMPOS

    def call(self, modelo, metodo, *args, **kw):
        self.llamadas.append((modelo, metodo, args))
        dom = args[0] if args else []
        if modelo == "stock.picking":
            picks = [{"id": 381, "name": "WH/PICK/00178", "origin": "S01104", "partner_id": [1, "OTOSINSA SRL, Delivery Address"],
                      "state": "confirmed", "scheduled_date": "2026-10-07 13:13:51", "create_date": "2026-10-07 13:13:51",
                      "picking_type_id": [3, "Magontex: Recolectar"]},
                     {"id": 379, "name": "WH/PICK/00177", "origin": "S01101", "partner_id": [2, "GOMEZ RODOLFO, Delivery Address"],
                      "state": "assigned", "scheduled_date": "2026-10-06 17:56:28", "create_date": "2026-10-06 17:56:27",
                      "picking_type_id": [3, "Magontex: Recolectar"]}]
            if not any(d[0] == "state" for d in dom):   # sin filtro de estado → también las hechas
                picks = picks + [{"id": 300, "name": "WH/PICK/00150", "origin": "S01000", "partner_id": [3, "VIEJO SA"],
                                  "state": "done", "scheduled_date": "2026-09-01 12:00:00", "create_date": "2026-09-01 12:00:00",
                                  "picking_type_id": [3, "Magontex: Recolectar"]}]
            if dom and dom[0][0] == "id":
                picks = [p for p in picks if p["id"] == dom[0][2]]
            return picks
        if modelo == "stock.move":
            ids = dom[0][2]
            moves = [{"picking_id": [381, "WH/PICK/00178"], "product_id": [10, "[4-MOCH-U-03001-BLCK-NOBR] TRAVELLER 03001 (nobrand)"], "product_uom_qty": 3, "quantity": 0},
                     {"picking_id": [379, "WH/PICK/00177"], "product_id": [11, "[2-BI-U-TRIGG-UNIC-S-0PRK-21V] Trigger (S)"], "product_uom_qty": 2, "quantity": 2},
                     {"picking_id": [379, "WH/PICK/00177"], "product_id": [12, "[2-BI-U-EARTH-UNIC-XL-0PRK-21V] Earthquake (XL)"], "product_uom_qty": 1, "quantity": 1}]
            return [m for m in moves if m["picking_id"][0] in ids]
        if modelo == "product.product":
            prods = {10: {"id": 10, "default_code": "4-MOCH-U-03001-BLCK-NOBR", "barcode": "2000000099991", "display_name": "[4-MOCH-U-03001-BLCK-NOBR] TRAVELLER 03001 (nobrand)", "name": "TRAVELLER 03001"},
                     11: {"id": 11, "default_code": "2-BI-U-TRIGG-UNIC-S-0PRK-21V", "barcode": "2000000099992", "display_name": "[2-BI-U-TRIGG-UNIC-S-0PRK-21V] Trigger (S)", "name": "Trigger"},
                     12: {"id": 12, "default_code": "2-BI-U-EARTH-UNIC-XL-0PRK-21V", "barcode": False, "display_name": "[2-BI-U-EARTH-UNIC-XL-0PRK-21V] Earthquake (XL)", "name": "Earthquake"}}
            ids = next(d[2] for d in dom if isinstance(d, list) and d[0] == "id")
            return [prods[i] for i in ids if i in prods]
        if modelo == "product.template.attribute.value":
            return []
        if metodo == "fields_get":
            return {}
        raise AssertionError((modelo, metodo))


def _login(c, nombre="Vengil", pin="1234"):
    uid = re.search(r"elegir\((\d+), '" + nombre + r"'\)", c.get("/login").text).group(1)
    assert c.post("/login", data={"usuario_id": uid, "pin": pin}).json()["ok"]


def test_lista_nuevo_e_imprimir(monkeypatch):
    fake = FakeCliente()
    monkeypatch.setattr(odoo, "cliente", lambda db=None, timeout=60: fake)
    svc._CACHE.update(t=0.0, data=None)
    with TestClient(app) as c:
        _login(c)
        assert "Recolecciones Odoo" in c.get("/recolecciones-odoo").text
        r = c.get("/s/pick_odoo", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/recolecciones-odoo"
        d = c.get("/recolecciones-odoo/api/lista?refrescar=1").json()
        assert d["total"] == 2 and d["nuevas"] == 2
        p = d["recolecciones"][0]
        assert p["nombre"] == "WH/PICK/00178" and p["cliente"] == "OTOSINSA SRL" and p["unidades"] == 3
        assert p["creada"] == "2026-10-07 10:13"            # UTC → Buenos Aires
        assert p["estado_txt"] == "En espera" and p["impresion"] is None
        # prueba (1 fila) no cuenta como impresa
        h = c.get("/recolecciones-odoo/379/etiquetas-rollo.html?filas=1")
        assert h.status_code == 200 and h.text.count('class="fila"') == 1
        assert c.get("/recolecciones-odoo/api/lista").json()["nuevas"] == 2
        # 3 unidades → 2 filas (de a 2), con su código de barras y SKU
        h = c.get("/recolecciones-odoo/379/etiquetas-rollo.html")
        assert h.status_code == 200 and h.text.count('class="fila"') == 2 and "window.print" in h.text
        assert "2-BI-U-TRIGG-UNIC-S-0PRK-21V" in h.text and "2000000099992" in h.text
        assert "@page{size:93mm 25mm;margin:0}" in h.text
        d = c.get("/recolecciones-odoo/api/lista").json()
        assert d["nuevas"] == 1
        imp = next(x for x in d["recolecciones"] if x["id"] == 379)["impresion"]
        assert imp["etiquetas"] == 3 and imp["usuario"] == "Vengil"
        z = c.get("/recolecciones-odoo/381/etiquetas.zpl")
        assert z.status_code == 200 and z.text.count("^XA") == 2 and "2000000099991" in z.text
        assert c.get("/recolecciones-odoo/999/etiquetas-rollo.html").status_code == 404


def test_sin_odoo_avisa(monkeypatch):
    def falla(db=None, timeout=60):
        raise odoo.OdooError("Falta configurar Odoo")
    monkeypatch.setattr(odoo, "cliente", falla)
    svc._CACHE.update(t=0.0, data=None)
    with TestClient(app) as c:
        _login(c)
        r = c.get("/recolecciones-odoo/api/lista")
        assert r.status_code == 502 and "Odoo" in r.json()["detail"]


def test_quitar_filtro_trae_todas(monkeypatch):
    fake = FakeCliente()
    monkeypatch.setattr(odoo, "cliente", lambda db=None, timeout=60: fake)
    svc._CACHE.update(t=0.0, data=None)
    svc._CACHE_TODAS.update(t=0.0, data=None)
    with TestClient(app) as c:
        _login(c)
        d = c.get("/recolecciones-odoo/api/lista?refrescar=1").json()
        assert d["total"] == 2 and all(p["abierta"] for p in d["recolecciones"])
        nuevas_abiertas = d["nuevas"]
        d = c.get("/recolecciones-odoo/api/lista?todas=1&refrescar=1").json()
        assert d["total"] == 3 and d["todas"] is True
        hecha = next(p for p in d["recolecciones"] if p["id"] == 300)
        assert hecha["estado_txt"] == "Hecho" and hecha["abierta"] is False
        assert d["nuevas"] == nuevas_abiertas and hecha["impresion"] is None   # las hechas no cuentan como NUEVAS
        dom = [a[0] for m, met, a in fake.llamadas if m == "stock.picking"][-1]
        assert dom == [["picking_type_id.sequence_code", "=", "PICK"]]
        assert "quitarFiltro" in c.get("/recolecciones-odoo").text
