import io
import json
from datetime import datetime

from openpyxl import load_workbook

from app.models import Devolucion, DevolucionItem
from app.services import in_odoo


def _item(**k):
    base = dict(articulo="ORIGIN 00002", color="BLACK", cantidad=1, estado="OPTIMO", guardados="",
                devuelto_at=datetime(2026, 10, 21, 15, 42))
    base.update(k)
    return DevolucionItem(**base)


def _dev():
    d = Devolucion(id=15, plataforma="ML+TN")
    d.items = [
        _item(sku="1-CAMP-M-WORKJ-BLCK-XXL-PARK", codigo_barras="2000000011318", talle="XXL", plataforma="TN",
              guardados=json.dumps({"M1-R4": 1})),
        _item(sku="1-CAMP-M-WORKJ-BLCK-M-PARK", codigo_barras="2000000011226", talle="M", plataforma="ML",
              guardados=json.dumps({"M3-R2": 1})),
        _item(sku="1-CAMP-M-WORKJ-BLCK-L-PARK", codigo_barras="0000000011196", talle="L", plataforma="ML", cantidad=2,
              guardados=json.dumps({"M3-R2": 2})),
        _item(sku="1-CAMP-M-WORKJ-BLCK-S-PARK", codigo_barras="2000000011100", talle="S", plataforma="ML",
              estado="FALLA", guardados=json.dumps({"FALLAS": 1})),                 # FALLA: no va
        _item(sku="1-CAMP-M-WORKJ-BLCK-XL-PARK", codigo_barras="2000000011200", talle="XL", plataforma="ML"),  # sin guardar
        _item(sku="", codigo_barras="", talle="L", plataforma="ML", guardados=json.dumps({"M3-R2": 1})),  # sin SKU
    ]
    return d


def test_excel_in_odoo_formato():
    d = _dev()
    contenido, n, avisos = in_odoo.excel(d)
    assert n == 4 and len(avisos) == 1 and "no coincide" in avisos[0]
    assert in_odoo.nombre_archivo(d) == "IN_DEV_2026-10-21_PD-15.xlsx"
    wb = load_workbook(io.BytesIO(contenido))
    assert wb.sheetnames == ["IN DEVOLUCIONES"]
    ws = wb.active
    filas = [[c.value for c in r] for r in ws.iter_rows()]
    assert filas[0] == in_odoo.ENCABEZADOS
    assert len(filas) == 5
    # bloque ML primero (3 escaneos: M + L x2), después TN
    assert filas[1][:8] == ["Magontex: Recepciones", "DEVOLUCIONES MERCADOLIBRE", "2026-10-21", "Partners/Customers",
                            "WH/Existencias", "1-CAMP-M-WORKJ-BLCK-M-PARK", "1-CAMP-M-WORKJ-BLCK-M-PARK", 1]
    assert filas[1][8:] == ["PD-15", "ML", "2000000011226", "ORIGIN 00002", "BLACK", "M", "M3-R2", "OPTIMO"]
    assert filas[2][:5] == [None] * 5 and filas[3][:5] == [None] * 5
    assert filas[2][5] == filas[3][5] == "1-CAMP-M-WORKJ-BLCK-L-PARK" and filas[2][7] == 1
    assert filas[3][10] == "0000000011196" and ws["K4"].data_type == "s"   # código como texto, con sus ceros
    assert filas[4][:3] == ["Magontex: Recepciones", "DEVOLUCIONES TIENDA NUBE", "2026-10-21"]
    assert filas[4][8:] == ["PD-15", "TN", "2000000011318", "ORIGIN 00002", "BLACK", "XXL", "M1-R4", "OPTIMO"]
    assert ws["C2"].data_type == "s" and ws["C2"].number_format == "@"
    assert all(r[15] == "OPTIMO" for r in filas[1:])


def test_sin_optimos_no_genera(monkeypatch):
    d = Devolucion(id=16, plataforma="ML")
    d.items = [_item(sku="X-1", codigo_barras="1", plataforma="ML", estado="FALLA", guardados=json.dumps({"F": 1}))]
    contenido, n, avisos = in_odoo.excel(d)
    assert contenido is None and n == 0
    r = in_odoo.generar_y_subir(None, d)
    assert r["ok"] and not r["nombre"] and "No hay prendas" in r["motivo"]


def test_generar_y_subir(monkeypatch):
    from app.services import re_drive
    subidos = []
    monkeypatch.setattr(re_drive, "guardar_deposito_archivo",
                        lambda db, sec, nombre, b: subidos.append((sec, nombre, len(b))) or {"url": "https://drive/x"})
    monkeypatch.setattr("app.actividad.registrar", lambda *a, **k: None)
    r = in_odoo.generar_y_subir(None, _dev())
    assert r["ok"] and r["filas"] == 4 and r["url"] == "https://drive/x"
    assert subidos and subidos[0][:2] == ("credito_odoo", "IN_DEV_2026-10-21_PD-15.xlsx")

    def falla(*a):
        raise re_drive.REDriveError("Acción desconocida: guardarDepositoArchivo")
    monkeypatch.setattr(re_drive, "guardar_deposito_archivo", falla)
    r = in_odoo.generar_y_subir(None, _dev())
    assert not r["ok"] and "Acción desconocida" in r["error"] and r["nombre"].startswith("IN_DEV_")


def test_endpoints_in_odoo(monkeypatch):
    """Al confirmar la última prenda en su rack se genera y sube el IN; se puede bajar y re-subir."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.services import re_drive, devoluciones as svc
    from tests.test_devoluciones import _login
    subidos = []
    monkeypatch.setattr(re_drive, "guardar_deposito_archivo",
                        lambda db, sec, nombre, b: subidos.append((sec, nombre)) or {"url": "https://drive/in"})
    monkeypatch.setattr(svc, "enviar", lambda dev: {"ok": True, "id": "D-1"})
    with TestClient(app) as c:
        _login(c)
        v = c.get("/devoluciones/api/codigo?c=2000000035925").json()["variante"]
        d = c.post("/devoluciones/api/confirmar", json={"ruta": True, "items": [
            {"articulo_id": v["id"], "cantidad": 1, "estado": "OPTIMO", "plataforma": "ML", "venta": "2000000000001"}]}).json()
        did, item_id = d["devolucion"]["id"], None
        from app.db import SessionLocal
        from app.models import Devolucion
        with SessionLocal() as db:
            item_id = db.get(Devolucion, did).items[0].id
        assert c.get(f"/devoluciones/{did}/in-odoo.xlsx").status_code == 404   # todavía nada guardado
        r = c.post(f"/devoluciones/api/{did}/guardar", json={"item_id": item_id, "codigo": "A01", "cantidad": 1})
        if r.status_code == 400:   # sin rack A01 en Mapeo en esta base de prueba
            from app.models import Ubicacion
            with SessionLocal() as db:
                db.add(Ubicacion(codigo="A01", zona="A", pasillo="A01", descripcion="test"))
                db.commit()
            r = c.post(f"/devoluciones/api/{did}/guardar", json={"item_id": item_id, "codigo": "A01", "cantidad": 1})
        x = r.json()
        assert x["completa"] and x["in_odoo"]["ok"] and x["in_odoo"]["filas"] == 1
        assert subidos[-1][0] == "credito_odoo" and subidos[-1][1].endswith(f"_PD-{did}.xlsx")
        xl = c.get(f"/devoluciones/{did}/in-odoo.xlsx")
        assert xl.status_code == 200 and f"PD-{did}.xlsx" in xl.headers["content-disposition"]
        assert c.post(f"/devoluciones/api/{did}/in-odoo").json()["ok"]
