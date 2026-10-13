"""Conexión con Odoo: normalización de productos, sincronización, consulta en vivo y API común del catálogo.
No se conecta a Odoo de verdad: se reemplaza el cliente XML-RPC por uno falso con datos reales de muestra."""
import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.models import Articulo
from app.services import ajustes, catalogo, odoo

VALORES = {11342: ("color", "BLACK"), 11347: ("Talle", "L"), 11351: ("Talle", "3XL"),
           11352: ("Brand", "nobrand"), 11353: ("Brand", "Parka"), 99001: ("color", "Green")}


def _p(pid, sku, barcode, nombre, vals, qty=0.0, active=True, name="THOR 00114"):
    return {"id": pid, "default_code": sku, "barcode": barcode, "display_name": f"[{sku}] {nombre}", "name": name,
            "categ_id": [7, "Camperas"], "product_template_variant_value_ids": vals, "qty_available": qty,
            "active": active, "write_date": "2026-09-04 18:46:10", "meli_id": False}


PRODUCTOS = [
    _p(31635, "1-CAMP-M-00114-BLCK-3XL-NOBR", "2000000016603", "THOR 00114 (BLACK, 3XL, nobrand)", [11342, 11351, 11352], 4),
    _p(31636, "1-CAMP-M-00114-BLCK-3XL-PARK", "2000000016610", "THOR 00114 (BLACK, 3XL, Parka)", [11342, 11351, 11353], 2),
    _p(31628, "1-CAMP-M-00114-BLCK-L-PARK", "2000000016634", "THOR 00114 (BLACK, L, Parka)", [11342, 11347, 11353], 7.0),
    # nuevo, que no estaba en el catálogo local
    _p(99999, "1-CAMP-M-09999-GREN-L-PARK", "2000000099999", "NUEVO 09999 (Green, L, Parka)", [99001, 11347, 11353],
       1, name="NUEVO 09999"),
    # sin SKU ni código: se ignora
    _p(1, False, False, "Envío", []),
]


class ClienteFalso:
    def __init__(self, productos):
        self.productos, self.uid, self.llamadas = productos, 7, []

    def login(self):
        return 7

    def campos(self):
        return odoo.CAMPOS + ["meli_id"]

    def call(self, modelo, metodo, *args, **kw):
        self.llamadas.append((modelo, metodo, args))
        if modelo == "product.product" and metodo == "search_read":
            dom = args[0]
            filas = self.productos
            cod = next((c[2] for c in dom if isinstance(c, list) and c[0] == "barcode"), None)
            if cod is not None:
                filas = [p for p in filas if p["barcode"] == cod or (p["default_code"] or "").upper() == cod.upper()]
            off, lim = kw.get("offset", 0), kw.get("limit") or len(filas)
            return filas[off:off + lim]
        if modelo == "product.template.attribute.value" and metodo == "read":
            return [{"id": i, "name": VALORES[i][1], "attribute_id": [1, VALORES[i][0]]} for i in args[0] if i in VALORES]
        if modelo == "product.product" and metodo == "read":
            return [{"id": i, "qty_available": 11.0} for i in args[0]]
        if metodo == "search_count":
            return len(self.productos)
        raise AssertionError((modelo, metodo))


def _login(c, nombre="Vengil", pin="1234"):
    uid = re.search(r"elegir\((\d+), '" + nombre + r"'\)", c.get("/login").text).group(1)
    assert c.post("/login", data={"usuario_id": uid, "pin": pin}).json()["ok"]


def _configurar(monkeypatch, productos=PRODUCTOS):
    falso = ClienteFalso(productos)
    monkeypatch.setattr(odoo, "cliente", lambda db=None, timeout=60: falso)
    db = SessionLocal()
    for k, v in {"odoo_usuario": "app@magontex.com.ar", "odoo_api_key": "clave-de-prueba"}.items():
        ajustes.guardar(db, k, v)
    db.close()
    catalogo._NO_ESTA.clear()
    return falso


def test_normalizar():
    d = odoo.normalizar(PRODUCTOS[0], VALORES)
    assert d["sku"] == "1-CAMP-M-00114-BLCK-3XL-NOBR" and d["codigo_barras"] == "2000000016603"
    assert d["nombre"] == "THOR 00114 (BLACK, 3XL, nobrand)" and d["articulo"] == "THOR 00114"
    assert d["variante"] == "BLACK, 3XL, nobrand"
    assert (d["color"], d["color_nombre"], d["talle"], d["marca"]) == ("BLCK", "BLACK", "3XL", "No Brand")
    assert d["categoria"] == "Camperas" and d["stock"] == 4.0 and d["meli_id"] == "" and d["inactivo"] is False
    assert odoo.normalizar(PRODUCTOS[-1], VALORES) is None
    assert odoo.nombre_marca("", "1-X-M-1-BLCK-L-PUFE") == "Puffers"


def test_sin_configurar():
    with TestClient(app) as c:
        _login(c)
        r = c.post("/admin/api/conexiones/probar/odoo").json()
        assert not r["ok"] and "Falta configurar" in r["mensaje"]
        assert c.get("/api/catalogo/estado").json()["configurado"] is False
        assert c.post("/api/catalogo/sincronizar").status_code == 400
        # el catálogo incluido igual se puede buscar
        a = c.get("/api/catalogo/codigo/2000000016603").json()
        assert a["articulo"] == "THOR 00114" and a["marca"] == "No Brand"


def test_sincronizar_y_api(monkeypatch):
    _configurar(monkeypatch)
    db = SessionLocal()
    antes = db.scalar(select(Articulo).where(Articulo.codigo_barras == "2000000016603"))
    id_antes = antes.id
    r = catalogo.sincronizar(db, "test")
    assert r["ok"] and r["cantidad"] == 4 and r["nuevos"] == 1 and r["actualizados"] == 3
    a = db.get(Articulo, id_antes)  # se actualizó la misma fila (no se duplica, las referencias siguen valiendo)
    assert a.odoo_id == 31635 and a.stock == 4 and a.marca == "No Brand" and a.color_nombre == "BLACK"
    nuevo = db.scalar(select(Articulo).where(Articulo.codigo_barras == "2000000099999"))
    assert nuevo.articulo == "NUEVO 09999" and nuevo.marca == "Parka" and nuevo.talle == "L"
    # segunda pasada sin cambios
    r2 = catalogo.sincronizar(db, "test")
    assert r2["nuevos"] == 0 and r2["actualizados"] == 0 and r2["archivados"] == 0
    # Odoo archiva uno y otro desaparece
    cambiados = [dict(PRODUCTOS[0], active=False)] + PRODUCTOS[1:3]
    _configurar(monkeypatch, cambiados)
    r3 = catalogo.sincronizar(db, "test")
    assert r3["archivados"] == 1  # el NUEVO ya no vino
    db.expire_all()
    assert db.get(Articulo, id_antes).inactivo and db.scalar(select(Articulo).where(Articulo.odoo_id == 99999)).inactivo
    assert not catalogo.toca_sincronizar(db)  # recién sincronizado (cada 60 min por defecto)
    db.close()

    with TestClient(app) as c:
        _login(c)
        e = c.get("/api/catalogo/estado").json()
        assert e["configurado"] and e["ultima"]["ok"] and e["ultima"]["cantidad"] == 3
        r = c.get("/api/catalogo/buscar?q=thor 3xl").json()
        skus = [x["sku"] for x in r["resultados"]]
        assert not r["exacto"] and "1-CAMP-M-00114-BLCK-3XL-PARK" in skus
        assert "1-CAMP-M-00114-BLCK-3XL-NOBR" not in skus  # archivado en Odoo
        r = c.get("/api/catalogo/buscar?q=2000000016610").json()
        assert r["exacto"] and r["resultados"][0]["stock"] == 2
        v = c.get("/api/catalogo/modelo?articulo=thor 00114").json()["variantes"]
        assert {x["talle"] for x in v} >= {"3XL", "L"}
        s = c.post("/api/catalogo/stock", json={"ids": [r["resultados"][0]["id"]]}).json()
        assert s["ok"] and list(s["stock"].values()) == [11.0]
        assert c.post("/admin/api/conexiones/probar/odoo").json()["ok"]
        assert "Sincronizar ahora" in c.get("/articulos").text
        assert "Odoo · artículos" in c.get("/admin/conexiones").text


def test_codigo_nuevo_se_trae_de_odoo(monkeypatch):
    nuevo = _p(77777, "1-CAMP-W-07777-BLCK-L-PARK", "2000000077777", "ROSA 07777 (BLACK, L, Parka)",
               [11342, 11347, 11353], 3, name="ROSA 07777")
    falso = _configurar(monkeypatch, PRODUCTOS + [nuevo])
    with TestClient(app) as c:
        _login(c)
        a = c.get("/api/catalogo/codigo/2000000077777").json()
        assert a["articulo"] == "ROSA 07777" and a["odoo_id"] == 77777
        n = len(falso.llamadas)
        assert c.get("/api/catalogo/codigo/2000000077777").status_code == 200 and len(falso.llamadas) == n  # ya local
        assert c.get("/api/catalogo/codigo/2000000000001").status_code == 404
        n = len(falso.llamadas)
        assert c.get("/api/catalogo/codigo/2000000000001").status_code == 404 and len(falso.llamadas) == n  # recordado
        # Devoluciones también lo encuentra al escanearlo
        mods = c.get("/devoluciones/api/buscar?q=2000000077777").json()
        assert mods["modelos"][0]["articulo"] == "ROSA 07777"
