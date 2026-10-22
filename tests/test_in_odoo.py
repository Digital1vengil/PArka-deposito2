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
    assert filas[0] == in_odoo.ENCABEZADOS and len(filas[0]) == 17 and filas[0][3] == "origin"
    assert len(filas) == 5
    # bloque ML primero (3 escaneos: M + L x2), después TN; partner unificado y origin = nombre del archivo
    assert filas[1][:9] == ["Magontex: Recepciones", "Devolucion ecommerce", "2026-10-21", "IN_DEV_2026-10-21_PD-15",
                            "Partners/Customers", "WH/Existencias", "1-CAMP-M-WORKJ-BLCK-M-PARK",
                            "1-CAMP-M-WORKJ-BLCK-M-PARK", 1]
    assert filas[1][9:] == ["PD-15", "ML", "2000000011226", "ORIGIN 00002", "BLACK", "M", "M3-R2", "OPTIMO"]
    assert filas[2][:6] == [None] * 6 and filas[3][:6] == [None] * 6
    assert filas[2][6] == filas[3][6] == "1-CAMP-M-WORKJ-BLCK-L-PARK" and filas[2][8] == 1
    assert filas[3][11] == "0000000011196" and ws["L4"].data_type == "s"   # código como texto, con sus ceros
    assert filas[4][:6] == ["Magontex: Recepciones", "Devolucion ecommerce", "2026-10-21", "IN_DEV_2026-10-21_PD-15",
                            "Partners/Customers", "WH/Existencias"]
    assert filas[4][9:] == ["PD-15", "TN", "2000000011318", "ORIGIN 00002", "BLACK", "XXL", "M1-R4", "OPTIMO"]
    assert ws["C2"].data_type == "s" and ws["C2"].number_format == "@"
    assert all(r[16] == "OPTIMO" for r in filas[1:])
    assert not any("MERCADO" in str(c).upper() or "TIENDA NUBE" in str(c).upper() for r in filas for c in r if c)


def test_sin_optimos_no_genera(monkeypatch):
    d = Devolucion(id=16, plataforma="ML")
    d.items = [_item(sku="X-1", codigo_barras="1", plataforma="ML", estado="FALLA", guardados=json.dumps({"F": 1}))]
    contenido, n, avisos = in_odoo.excel(d)
    assert contenido is None and n == 0
    from app.services import dev_archivos
    r = dev_archivos.generar_y_subir(None, d)
    assert r["ok"] and not r["nombre"] and "No hay prendas" in r["motivo"] and r["archivos"] == []


def test_generar_y_subir(monkeypatch):
    from app.services import re_drive
    subidos = []
    monkeypatch.setattr(re_drive, "guardar_deposito_archivo",
                        lambda db, sec, nombre, b: subidos.append((sec, nombre, len(b))) or {"url": "https://drive/x"})
    monkeypatch.setattr("app.actividad.registrar", lambda *a, **k: None)
    from app.services import dev_archivos
    r = dev_archivos.generar_y_subir(None, _dev())
    assert r["ok"] and r["filas"] == 4 and r["url"] == "https://drive/x" and r["nombre"] == "IN_DEV_2026-10-21_PD-15.xlsx"
    assert [s[:2] for s in subidos] == [("dev_01_cantidades", "CANT_DEV_2026-10-21_PD-15.xlsx"),
                                        ("dev_02_detalle", "DET_DEV_2026-10-21_PD-15.xlsx"),
                                        ("dev_03_historial", "HIST_DEV_2026-10-21_PD-15.xlsx"),
                                        ("dev_04_odoo", "IN_DEV_2026-10-21_PD-15.xlsx")]

    # Apps Script viejo (sin las carpetas 01–04): todo a "Plantilla credito odoo" y se avisa
    subidos.clear()

    def viejo(db, sec, nombre, b):
        if sec.startswith("dev_"):
            raise re_drive.REDriveError(f"Sección inválida: {sec}")
        subidos.append((sec, nombre))
        return {"url": "https://drive/y"}
    monkeypatch.setattr(re_drive, "guardar_deposito_archivo", viejo)
    r = dev_archivos.generar_y_subir(None, _dev())
    assert r["ok"] and len(subidos) == 4 and all(s[0] == "credito_odoo" for s in subidos)
    assert any("2026.10.22-1" in a for a in r["avisos"])

    def falla(*a):
        raise re_drive.REDriveError("Acción desconocida: guardarDepositoArchivo")
    monkeypatch.setattr(re_drive, "guardar_deposito_archivo", falla)
    r = dev_archivos.generar_y_subir(None, _dev())
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
        assert x["completa"] and x["in_odoo"]["ok"] and x["in_odoo"]["filas"] == 1 and len(x["in_odoo"]["archivos"]) == 4
        assert subidos[-1][0] == "dev_04_odoo" and subidos[-1][1].endswith(f"_PD-{did}.xlsx")
        assert [s[0] for s in subidos[-4:]] == ["dev_01_cantidades", "dev_02_detalle", "dev_03_historial", "dev_04_odoo"]
        z = c.get(f"/devoluciones/{did}/archivos.zip")
        assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
        xl = c.get(f"/devoluciones/{did}/in-odoo.xlsx")
        assert xl.status_code == 200 and f"PD-{did}.xlsx" in xl.headers["content-disposition"]
        assert c.post(f"/devoluciones/api/{did}/in-odoo").json()["ok"]


def _hoja(contenido):
    ws = load_workbook(io.BytesIO(contenido)).active
    return ws, [[c.value for c in r] for r in ws.iter_rows()]


def test_cuatro_archivos_cuadran():
    from datetime import date
    from app.services import dev_archivos
    d = _dev()
    d.fecha = date(2026, 10, 20)
    for i, v in zip(d.items, ["2000009111", "2000009222", "2000009222", "2000009333", "2000009444", "2000009555"]):
        i.venta = v
    d.items[0].venta = "1234"   # TN: n° de orden
    lista, avisos = dev_archivos.archivos(d)
    assert [a["clave"] for a in lista] == list(dev_archivos.ARCHIVOS) and len(avisos) == 1
    a = {x["clave"]: x for x in lista}

    ws, f1 = _hoja(a["dev_01_cantidades"]["contenido"])
    assert ws.title == "CANTIDADES NETAS" and f1[0] == ["SKU", "Descripcion_Articulo", "Cantidad_Total_Devuelta"]
    assert f1[1:] == [["1-CAMP-M-WORKJ-BLCK-L-PARK", "ORIGIN 00002 BLACK L", 2],
                      ["1-CAMP-M-WORKJ-BLCK-M-PARK", "ORIGIN 00002 BLACK M", 1],
                      ["1-CAMP-M-WORKJ-BLCK-XXL-PARK", "ORIGIN 00002 BLACK XXL", 1]]

    ws, f2 = _hoja(a["dev_02_detalle"]["contenido"])
    assert f2[0] == ["Fecha_Devolucion", "Plataforma", "Numero_Venta_Orden", "SKU", "Descripcion", "Cantidad"]
    assert f2[1:] == [["2026-10-20", "Mercado Libre", "2000009222", "1-CAMP-M-WORKJ-BLCK-L-PARK", "ORIGIN 00002 BLACK L", 2],
                      ["2026-10-20", "Mercado Libre", "2000009222", "1-CAMP-M-WORKJ-BLCK-M-PARK", "ORIGIN 00002 BLACK M", 1],
                      ["2026-10-20", "Tienda Nube", "1234", "1-CAMP-M-WORKJ-BLCK-XXL-PARK", "ORIGIN 00002 BLACK XXL", 1]]
    assert ws["C2"].data_type == "s"

    ws, f3 = _hoja(a["dev_03_historial"]["contenido"])
    assert f3[0] == ["Fecha", "Devolucion", "Plataforma", "Numero_Venta_Orden", "SKU", "Estado"]
    est = [r[5] for r in f3[1:]]
    assert set(est) <= {"Devuelto", "Pendiente"}
    # Devuelto = filas del IN; Pendiente = XL sin guardar + la prenda sin SKU (guardada pero no cruza); FALLA no está
    assert est.count("Devuelto") == 4 and est.count("Pendiente") == 2
    assert f3[1][0] == "2026-10-20" and f3[1][5] == "Pendiente"          # cronológico: paso 1 (20/10) antes del rack (21/10)
    assert ["2026-10-21", "PD-15", "Tienda Nube", "1234", "1-CAMP-M-WORKJ-BLCK-XXL-PARK", "Devuelto"] in f3
    assert not any(r[4] == "1-CAMP-M-WORKJ-BLCK-S-PARK" for r in f3)

    _ws, f4 = _hoja(a["dev_04_odoo"]["contenido"])
    assert sum(r[2] for r in f1[1:]) == sum(r[5] for r in f2[1:]) == len(f4) - 1 == est.count("Devuelto")


def test_sin_rack_solo_historial(monkeypatch):
    from app.services import dev_archivos, re_drive
    d = _dev()
    for i in d.items:
        i.guardados, i.devuelto_at = "", None
    lista, _ = dev_archivos.archivos(d)
    assert [a["clave"] for a in lista] == ["dev_03_historial"]
    _ws, f3 = _hoja(lista[0]["contenido"])
    assert {r[5] for r in f3[1:]} == {"Pendiente"} and len(f3) == 1 + 6   # 6 unidades OPTIMO (L x2, FALLA no)
    monkeypatch.setattr(re_drive, "guardar_deposito_archivo", lambda db, sec, nombre, b: {"url": "u"})
    monkeypatch.setattr("app.actividad.registrar", lambda *a, **k: None)
    r = dev_archivos.generar_y_subir(None, d)
    assert r["ok"] and not r["nombre"] and len(r["archivos"]) == 1 and "historial" in r["motivo"]
