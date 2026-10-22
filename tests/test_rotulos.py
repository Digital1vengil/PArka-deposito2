"""Rótulos de entrega (pedidos Magontex PICK/OUT de Odoo) y Códigos de clientes."""
import re

from fastapi.testclient import TestClient

from app.main import app
from app.services import odoo, rotulos as svc

PARTNERS = {
    11387: {"id": 11387, "name": "BBN S.R.L.", "type": "contact", "parent_id": False, "commercial_partner_id": [11387, "BBN S.R.L."],
            "street": "MENDOZA 4328", "street2": False, "city": "Rosario", "zip": "2000", "state_id": [573, "Santa Fe (AR)"],
            "phone": "+54 9 341 586-5404", "mobile": False, "email": "bbn@x.com", "vat": "30715531816",
            "default_transportista": [553, "JUPITER"], "is_company": True},
    12373: {"id": 12373, "name": False, "type": "delivery", "parent_id": [11387, "BBN S.R.L."],
            "commercial_partner_id": [11387, "BBN S.R.L."], "street": "mendoza 4328 depósito", "street2": False,
            "city": "Rosario", "zip": False, "state_id": [573, "Santa Fe (AR)"], "phone": False, "mobile": False,
            "email": False, "vat": "30715531816", "default_transportista": False, "is_company": False},
    11661: {"id": 11661, "name": "GRUPO LEGAR S R L", "type": "contact", "parent_id": False,
            "commercial_partner_id": [11661, "GRUPO LEGAR S R L"], "street": "MUÑECAS 948", "street2": False, "city": "0",
            "zip": False, "state_id": [553, "Ciudad Autónoma de Buenos Aires (AR)"], "phone": False, "mobile": False,
            "email": False, "vat": "30700570459", "default_transportista": False, "is_company": True},
}
PICKS = [
    {"id": 412, "name": "WH/PICK/00191", "origin": "S01413", "partner_id": [12373, "BBN S.R.L., Delivery Address"],
     "state": "assigned", "scheduled_date": "2026-10-15 17:10:33", "create_date": "2026-10-15 17:10:33", "date_done": False,
     "picking_type_id": [3, "Magontex: Recolectar"], "qty_bultos": 3, "carrier_id": False},
    {"id": 500, "name": "WH/OUT/00050", "origin": "S01400", "partner_id": [11661, "GRUPO LEGAR S R L"],
     "state": "confirmed", "scheduled_date": "2026-10-14 12:00:00", "create_date": "2026-10-14 12:00:00", "date_done": False,
     "picking_type_id": [2, "Magontex: Órdenes de entrega"], "qty_bultos": 0, "carrier_id": [699, "VIA 2"]},
]


class FakeCliente:
    def __init__(self):
        self.llamadas = []

    def campos(self):
        return odoo.CAMPOS

    def call(self, modelo, metodo, *args, **kw):
        self.llamadas.append((modelo, metodo, args))
        if modelo == "stock.picking":
            dom = args[0]
            ps = PICKS
            if dom and dom[0][0] == "id":
                ids = dom[0][2] if dom[0][1] == "in" else [dom[0][2]]
                ps = [p for p in ps if p["id"] in ids]
            return ps
        if modelo == "res.partner" and metodo == "read":
            return [PARTNERS[i] for i in args[0] if i in PARTNERS]
        if modelo == "res.partner" and metodo == "search_read":
            dom = args[0]
            if dom[0][0] == "parent_id":
                return [p for p in PARTNERS.values() if p["parent_id"] and p["parent_id"][0] == dom[0][2]]
            return [PARTNERS[11387], PARTNERS[11661]]
        if modelo == "delivery.carrier":
            dirs = {553: {"id": 553, "street": "Rabanal 3321, CABA", "street2": False, "city": "Capital Federal",
                          "zip": False, "state_id": [553, "Ciudad Autónoma de Buenos Aires (AR)"]},
                    699: {"id": 699, "street": "av int. francisco rabanal 2756", "street2": False, "city": "Capital Federal",
                          "zip": False, "state_id": [553, "Ciudad Autónoma de Buenos Aires (AR)"]}}
            return [dirs[i] for i in args[0] if i in dirs]
        if modelo == "res.company":
            return [{"name": "MAGONTEX S.A.", "street": "PARAGUAY 577", "street2": "Piso:10 Dpto:B", "city": "Retiro",
                     "zip": "1057", "state_id": [553, "Ciudad Autónoma de Buenos Aires (AR)"], "vat": "30714710474"}]
        raise AssertionError((modelo, metodo))


def _login(c, nombre="Vengil", pin="1234"):
    uid = re.search(r"elegir\((\d+), '" + nombre + r"'\)", c.get("/login").text).group(1)
    assert c.post("/login", data={"usuario_id": uid, "pin": pin}).json()["ok"]


def _limpiar():
    for k in (svc._CACHE, svc._CACHE_TODOS, svc._CACHE_CLIENTES, svc._CACHE_EMPRESA):
        k.update(t=0.0, data=None)


def test_codigo_barras_por_bulto():
    cli = {"codigo": "CLI-011661"}
    rs = svc.lista_rotulos({"origen": "S01472"}, cli, {"nombre": "X"}, 55, "/c/1", url_base="https://x")
    cods = [svc.codigo_barras(r) for r in rs]
    assert len(set(cods)) == 55 and cods[0] == "S01472-1-55" and cods[-1] == "S01472-55-55"
    # el scanner USB lee el barras igual que la cámara el QR
    assert svc.leer_codigo_bulto(cods[6]) == ("S01472", 7, 55)
    # sin pedido, el código sale del cliente y también se puede leer
    sp = svc.lista_rotulos(None, cli, {"nombre": "X"}, 3, "/c/1")
    assert svc.codigo_barras(sp[1]) == "CLI011661-2-3" and svc.leer_codigo_bulto("CLI011661-2-3") == ("CLI011661", 2, 3)


def test_codigo_cliente():
    assert svc.codigo_cliente(11661) == "CLI-011661"
    assert svc.id_de_codigo("CLI-011661") == 11661 == svc.id_de_codigo("cli011661") == svc.id_de_codigo(" 11661 ")
    assert svc.id_de_codigo("BBN") is None


def test_pedidos_rotulos(monkeypatch):
    fake = FakeCliente()
    monkeypatch.setattr(odoo, "cliente", lambda db=None, timeout=60: fake)
    _limpiar()
    with TestClient(app) as c:
        _login(c)
        assert "Rótulos de entrega" in c.get("/rotulos").text
        r = c.get("/s/rotulos", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/rotulos"
        d = c.get("/rotulos/api/lista?refrescar=1").json()
        assert d["total"] == 2 and d["nuevos"] == 2
        p = next(x for x in d["pedidos"] if x["id"] == 412)
        assert p["tipo"] == "Recolectar" and p["bultos"] == 3 and p["detectado"] and p["impreso"] is None
        cli = p["cliente"]
        # cliente = contacto principal; dirección = la de entrega; expreso = transportista por defecto
        assert cli["nombre"] == "BBN S.R.L." and cli["codigo"] == "CLI-011387" and cli["expreso"] == "JUPITER"
        assert cli["calle"] == "mendoza 4328 depósito" and cli["telefono"] == "+54 9 341 586-5404"
        assert cli["provincia"] == "Santa Fe"
        out = next(x for x in d["pedidos"] if x["id"] == 500)
        # sin transportista por defecto → el del pedido; sin bultos → 1; ciudad "0" se ignora
        assert out["cliente"]["expreso"] == "VIA 2" and out["bultos"] == 1 and out["cliente"]["localidad"] == "CABA"
        assert out["tipo"] == "Órdenes de entrega"
        dom = [a[0] for m, met, a in fake.llamadas if m == "stock.picking"][0]
        assert ["picking_type_id.warehouse_id.code", "=", "WH"] in dom

        # prueba: 1 rótulo y no cuenta como impreso
        h = c.get("/rotulos/412/rotulo.html?prueba=1")
        assert h.status_code == 200 and h.text.count('class="rot"') == 1
        assert c.get("/rotulos/api/lista").json()["nuevos"] == 2
        # 3 bultos de Odoo → 3 rótulos 1/3, 2/3, 3/3
        h = c.get("/rotulos/412/rotulo.html")
        assert h.text.count('class="rot"') == 3 and "3 de 3" in h.text and "JUPITER" in h.text
        assert "@page{size:100mm 150mm;margin:0}" in h.text and "MAGONTEX S.A." in h.text and "S01413" in h.text
        assert "http://testserver/clientes/11387" not in h.text   # el QR va como dibujo, no como texto
        d = c.get("/rotulos/api/lista").json()
        assert d["nuevos"] == 1
        imp = next(x for x in d["pedidos"] if x["id"] == 412)["impreso"]
        assert imp["bultos"] == 3 and imp["usuario"] == "Vengil"
        # bultos corregidos a mano y ZPL 300 dpi
        z = c.get("/rotulos/412/rotulo.zpl?bultos=2")
        assert z.status_code == 200 and z.text.count("^XA") == 2 and "^PW1181^LL1772" in z.text
        # el código de barras es distinto en cada bulto (hay que escanearlos todos) y el cliente queda como texto
        assert "^FDS01413-1-2^FS" in z.text and "^FDS01413-2-2^FS" in z.text and "Cliente: CLI-011387" in z.text
        assert "QA,http://testserver/b/S01413-1-2" in z.text and "QA,http://testserver/b/S01413-2-2" in z.text and "2 de 2" in z.text
        assert c.get("/rotulos/999/rotulo.html").status_code == 404
        # el QR del bulto, escaneado con la cámara común, abre la ficha del cliente
        r = c.get("/b/412-2-3", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/clientes/11387"
        assert c.get("/b/412-4-3").status_code == 404
        r = c.get("/b/S01413-2-3", follow_redirects=False)   # QR nuevo: venta-bulto-total
        assert r.status_code == 303 and r.headers["location"] == "/clientes/11387"
        assert out["cliente"]["expreso_dir"]["calle"] == "av int. francisco rabanal 2756"


def test_clientes_ficha_y_planilla(monkeypatch):
    fake = FakeCliente()
    monkeypatch.setattr(odoo, "cliente", lambda db=None, timeout=60: fake)
    _limpiar()
    with TestClient(app) as c:
        _login(c)
        assert "Códigos de clientes" in c.get("/clientes").text
        d = c.get("/clientes/api/lista").json()
        assert d["total"] == 2 and d["clientes"][0]["codigo"] == "CLI-011387"
        f = c.get("/clientes/11387")
        assert f.status_code == 200 and "CLI-011387" in f.text and "JUPITER" in f.text and "<svg" in f.text
        assert "mendoza 4328 depósito" in f.text   # dirección de entrega
        pl = c.get("/clientes/codigos.html?ids=11661")
        assert pl.status_code == 200 and pl.text.count('class="c"') == 1 and "CLI-011661" in pl.text
        r = c.get("/clientes/11387/rotulo.html?bultos=2&entrega=12373")
        assert r.text.count('class="rot"') == 2 and "mendoza 4328 depósito" in r.text
        z = c.get("/clientes/11661/rotulo.zpl")
        assert z.text.count("^XA") == 1 and "SIN EXPRESO" in z.text
        assert c.get("/clientes/1/rotulo.html").status_code == 404


def test_completa_cp_ciudad_y_telefono():
    d = lambda calle, ciudad, cp, prov: {"calle": calle, "localidad": ciudad, "cp": cp, "provincia": prov}
    caba = "Ciudad Autónoma de Buenos Aires"
    # misma calle (GRUPO LEGAR): la entrega no tiene CP ni ciudad ("0") → se toman del principal; CABA si no hay ciudad
    r = svc.completar_direccion(d("MUÑECAS 948", "", "", caba), d("MUÑECAS 948", "", "1414", caba))
    assert r["cp"] == "1414" and r["localidad"] == "CABA"
    # misma ciudad (LALA LALA, Rosario): CP de la ciudad
    r = svc.completar_direccion(d("Corrientes 549", "Rosario", "", "Santa Fe"), d("CORRIENTES 549", "ROSARIO", "2000", "Santa Fe"))
    assert r["cp"] == "2000" and r["localidad"] == "Rosario"
    # CABA con otra calle: no se inventa el CP (CABA tiene varios)
    r = svc.completar_direccion(d("Av. Dorrego 1309", caba, "", caba), d("LUIS MARIA DRAGO 348", caba, "1414", caba))
    assert r["cp"] == "" and r["calle"] == "Av. Dorrego 1309"
    # sin calle de entrega → toda la dirección del principal
    r = svc.completar_direccion(d("", "", "", ""), d("CALLE 137 1456", "La Plata", "1900", "Buenos Aires"))
    assert r == d("CALLE 137 1456", "La Plata", "1900", "Buenos Aires")
    # teléfono: de una persona de contacto del cliente si el cliente no tiene
    cli = svc.armar_cliente(PARTNERS[11661], None, [{"phone": "11 4444-5555", "mobile": False}])
    assert cli["telefono"] == "11 4444-5555"
    # CP por localidad cuando Odoo no lo tiene (no en CABA)
    r = svc.completar_direccion(d("Alberti 2319", "MAR DEL PLATA", "", "Buenos Aires"), d("ALBERTI 2319", "MAR DEL PLATA", "", "Buenos Aires"))
    assert r["cp"] == "7600"
    assert svc.cp_de_localidad("CABA", caba) == ""


def test_rotulo_deja_65mm_arriba():
    """Etiqueta 100x150: el rótulo va abajo y los 65 mm de arriba (los que se separan en la de ML) quedan en blanco."""
    cli = svc.armar_cliente(PARTNERS[11387])
    rem = {"nombre": "MAGONTEX S.A.", "cuit": "1", "calle": "x", "localidad": "", "cp": "", "provincia": "", "telefono": ""}
    z = svc.build_zpl(svc.lista_rotulos(None, cli, rem, 1, "http://x/clientes/11387"))
    ys = [int(m) for m in re.findall(r"\^FO\d+,(\d+)", z)]
    assert "^PW1181^LL1772" in z and min(ys) >= round(65 * 300 / 25.4) and max(ys) < 1772
    h = svc.build_html(svc.lista_rotulos(None, cli, rem, 1, "http://x/clientes/11387"))
    assert ".rot{padding-top:65mm}.zona{height:85mm}" in h
