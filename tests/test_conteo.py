import io
import re

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.main import app


def _login(c, nombre="Vengil", pin="1234"):
    uid = re.search(r"elegir\((\d+), '" + nombre + r"'\)", c.get("/login").text).group(1)
    assert c.post("/login", data={"usuario_id": uid, "pin": pin}).json()["ok"]


def test_flujo_conteo():
    with TestClient(app) as c:
        _login(c)
        assert "/conteo" in c.get("/").text
        assert c.get("/conteo").status_code == 200
        # incluye No Brand (00AXE) además de Parka y Puffers
        m = c.get("/conteo/api/buscar?q=00axe").json()["modelos"]
        assert m and {v["marca"] for v in m[0]["variantes"]} == {"No Brand"}
        r = c.get("/conteo/api/buscar?q=abba 02003").json()["modelos"]
        abba = next(x for x in r if x["articulo"] == "ABBA 02003")
        assert {"Parka", "No Brand"} <= {v["marca"] for v in abba["variantes"]}
        # escaneo de un código sin marca preselecciona la variante
        r2 = c.get("/conteo/api/buscar?q=2000000035918").json()["modelos"]
        pre = r2[0]["preseleccion"]
        assert pre
        park = next(v for v in abba["variantes"] if v["codigo_barras"] == "2000000035925")
        res = c.post("/conteo/api/confirmar", json={"nota": "Estante A3", "items": [
            {"articulo_id": pre, "cantidad": 5}, {"articulo_id": park["id"], "cantidad": 2},
            {"articulo_id": park["id"], "cantidad": 1}, {"articulo_id": m[0]["variantes"][0]["id"], "cantidad": 0}]}).json()
        ct = res["conteo"]
        assert ct["total"] == 8 and len(ct["items"]) == 2 and ct["nota"] == "Estante A3"
        x = c.get(f"/conteo/{ct['id']}/conteo.xlsx")
        assert x.status_code == 200
        ws = load_workbook(io.BytesIO(x.content)).active
        filas = [r for r in ws.iter_rows(min_row=4, values_only=True)]
        assert filas[-1][-1] == 8 and {f[3] for f in filas[:-1]} == {"Parka", "No Brand"}
        pdf = c.get(f"/conteo/{ct['id']}/etiquetas.pdf?desde=2")
        assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
        assert c.get("/conteo/api/historial").json()["conteos"][0]["id"] == ct["id"]
        assert c.post("/conteo/api/confirmar", json={"items": []}).status_code == 400
        assert "conteos hoy" in c.get("/").text


def test_variantes_sin_codigo_de_barras():
    """Las variantes de Odoo sin código de barras también se cuentan y se devuelven (marcadas "sin código")."""
    from app.db import SessionLocal
    from app.models import Articulo
    with SessionLocal() as db:
        db.add_all([Articulo(sku="1-BANA-M-ZZTST-BLCK-M-PARK", codigo_barras="", nombre="ZZTEST (BLACK, M, Parka)",
                             articulo="ZZTEST", variante="BLACK, M, Parka", color="BLCK", talle="M", odoo_id=999901),
                    Articulo(sku="1-BANA-M-ZZTST-BLCK-L-NOBR", codigo_barras="", nombre="ZZTEST (BLACK, L, no brand)",
                             articulo="ZZTEST", variante="BLACK, L, no brand", color="BLCK", talle="L", odoo_id=999902)])
        db.commit()
    with TestClient(app) as c:
        _login(c)
        m = c.get("/conteo/api/buscar?q=zztest").json()["modelos"]
        vs = m[0]["variantes"]
        assert {v["marca"] for v in vs} == {"Parka", "No Brand"} and all(v["sin_codigo"] for v in vs)
        ct = c.post("/conteo/api/confirmar", json={"items": [{"articulo_id": vs[0]["id"], "cantidad": 2}]}).json()["conteo"]
        assert ct["total"] == 2
        pdf = c.get(f"/conteo/{ct['id']}/etiquetas.pdf")
        assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
        # devoluciones: solo Parka / Puffers, también sin código
        d = c.get("/devoluciones/api/buscar?q=zztest").json()["modelos"]
        assert [v["marca"] for v in d[0]["variantes"]] == ["Parka"]
        x = c.get("/articulos/sin-codigo.xlsx")
        assert x.status_code == 200
        ws = load_workbook(io.BytesIO(x.content)).active
        assert "1-BANA-M-ZZTST-BLCK-M-PARK" in [r[4] for r in ws.iter_rows(min_row=2, values_only=True)]
