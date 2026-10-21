import re

from fastapi.testclient import TestClient

from app.main import app
from app.services import plano
from app.services import devoluciones as svc


def _login(c, nombre="Vengil", pin="1234"):
    uid = re.search(r"elegir\((\d+), '" + nombre + r"'\)", c.get("/login").text).group(1)
    assert c.post("/login", data={"usuario_id": uid, "pin": pin}).json()["ok"]


def test_plano_codigos_y_ruta():
    cods = [c["codigo"] for c in plano.codigos_iniciales()]
    assert len(cods) == 228 == len(set(cods)) and "B01-C07-N0" in cods and "B01-C08-N2" in cods and "A03" not in cods
    assert plano.partes("A01-C08") is None and plano.partes("A-03") is None and plano.canonico("b2-c5-n1") == "B02-C05-N1"
    assert plano.describir("B01-C07-N0") == "Zona B · rack B01 · columna 7 · planta baja"
    assert plano.partes("b01-c07-n0") == ("B01", 7, 0) and plano.partes("Z99") is None
    # columnas de izquierda a derecha
    assert plano.acceso("B01-C01-N0")[0] < plano.acceso("B01-C08-N0")[0]
    # saliendo de la Persiana 2 (zona A/B) se arranca por lo de la zona A; de la Persiana 1, por la C
    r2 = plano.ordenar(["C05", "A01", "B01-C04-N1"], "P2")
    r1 = plano.ordenar(["C05", "A01", "B01-C04-N1"], "P1")
    assert r2["orden"][0] == "A01" and r1["orden"][0] != "A01" and len(r1["tramos"]) == 3
    assert all(len(t) >= 2 for t in r1["tramos"]) and r1["metros_aprox"] > 0
    assert plano.ordenar(["XX-1", "A02"], "P1")["orden"] == ["A02", "XX-1"]   # lo que no está en el croquis va al final


def test_flujo_ruta_devolucion(monkeypatch):
    enviados = []
    monkeypatch.setattr(svc, "enviar", lambda d: enviados.append(d) or {"ok": True, "id": "D-9"})
    with TestClient(app) as c:
        _login(c)
        assert c.post("/mapeo/api/modulos/cargar").json()["ok"]
        assert c.post("/mapeo/api/modulos/cargar").json()["creadas"] == 0
        cq = c.get("/mapeo/api/plano").json()
        assert not cq["faltan"] and cq["inicio"] == "P1"
        ubs = {u["codigo"]: u for u in cq["ubicaciones"]}
        m = next(x for x in c.get("/devoluciones/api/buscar?q=abba 02003").json()["modelos"] if x["articulo"] == "ABBA 02003")
        v1 = next(x for x in m["variantes"] if x["color"] == "BLACK" and x["talle"] == "M")
        v2 = next(x for x in m["variantes"] if x["id"] != v1["id"])
        # el modelo está en dos racks: A03 y B01-C07-N0
        c.post("/mapeo/api/asignar", json={"ubicacion_id": ubs["A03-C02-N0"]["id"], "articulo_id": v1["id"]})
        c.post("/mapeo/api/asignar", json={"ubicacion_id": ubs["B01-C07-N0"]["id"], "articulo_id": v1["id"]})
        assert c.post("/mapeo/api/ajustes", json={"inicio": "P1", "rack_fallas": "C05-C01-N0"}).json()["ok"]
        assert c.post("/mapeo/api/ajustes", json={"inicio": "P1", "rack_fallas": "Z-NO"}).status_code == 400
        res = c.post("/devoluciones/api/confirmar", json={"ruta": True, "items": [
            {"articulo_id": v1["id"], "cantidad": 2, "estado": "OPTIMO", "plataforma": "ML", "venta": "2000012345678901"},
            {"articulo_id": v1["id"], "cantidad": 1, "estado": "FALLA", "plataforma": "ML", "venta": "2000012345678901"},
            {"articulo_id": v2["id"], "cantidad": 1, "estado": "OPTIMO", "plataforma": "ML", "venta": "2000012345678901"}]}).json()
        d, ruta = res["devolucion"], res["ruta"]
        assert d["ruta_estado"] == "PENDIENTE" and d["drive_ok"] and len(enviados) == 1   # paso 1: Drive al confirmar
        cods = [p["codigo"] for p in ruta["paradas"]]
        assert "C05-C01-N0" in cods and ruta["rack_fallas"] == "C05-C01-N0" and ruta["inicio"]["nombre"] == "Persiana 1"
        assert len([x for x in cods if x in ("A03-C02-N0", "B01-C07-N0")]) == 1   # elige uno solo de los dos racks
        assert [i["articulo"] for i in ruta["sin_ubicacion"]] == ["ABBA 02003"]   # v2 no tiene rack
        assert c.get("/devoluciones/api/rack?c=b01-c07-n0").json() == {"ok": True, "codigo": "B01-C07-N0",
                                                                      "descripcion": plano.describir("B01-C07-N0")}
        assert not c.get("/devoluciones/api/rack?c=Z99").json()["ok"]
        p_opt = next(p for p in ruta["paradas"] if p["codigo"] in ("A03-C02-N0", "B01-C07-N0"))
        p_fal = next(p for p in ruta["paradas"] if p["codigo"] == "C05-C01-N0")
        sin = ruta["sin_ubicacion"][0]
        r = c.post(f"/devoluciones/api/{d['id']}/devuelto", json={"guardados": [
            {"item_id": p_opt["items"][0]["item_id"], "codigo": p_opt["codigo"], "cantidad": 2},
            {"item_id": p_fal["items"][0]["item_id"], "codigo": "C05-C01-N0", "cantidad": 1},
            {"item_id": sin["item_id"], "codigo": "B02-C01-N0", "cantidad": 1}]}).json()
        assert r["devolucion"]["ruta_estado"] == "DEVUELTO" and r["devolucion"]["drive_ok"] and len(enviados) == 1   # no se repite
        assert r["aprendidas"] == 1   # v2 quedó en B02: la próxima vez la ruta la manda ahí
        assert sorted(i["ubicacion"] for i in r["devolucion"]["items"]) == sorted([p_opt["codigo"], "C05-C01-N0", "B02-C01-N0"])
        assert c.post(f"/devoluciones/api/{d['id']}/devuelto", json={"guardados": []}).status_code == 409
        # la próxima devolución de v2 ya va a B02
        res2 = c.post("/devoluciones/api/confirmar", json={"ruta": True, "items": [
            {"articulo_id": v2["id"], "cantidad": 1, "estado": "OPTIMO", "plataforma": "TN", "venta": "1234"}]}).json()
        assert [p["codigo"] for p in res2["ruta"]["paradas"]] == ["B02-C01-N0"] and not res2["ruta"]["sin_ubicacion"]
        # "Guardar en Drive sin devolver"
        s = c.post(f"/devoluciones/api/{res2['devolucion']['id']}/sin-devolver").json()["devolucion"]
        assert s["ruta_estado"] == "SIN_DEVOLVER" and s["drive_ok"] and len(enviados) == 2   # ya subida al confirmar
        # sin ruta (el botón del resumen) se sube directo, como antes
        res3 = c.post("/devoluciones/api/confirmar", json={"items": [
            {"articulo_id": v2["id"], "cantidad": 1, "estado": "OPTIMO", "plataforma": "TN", "venta": "1235"}]}).json()
        assert res3["devolucion"]["drive_ok"] and "ruta" not in res3 and len(enviados) == 3
        assert c.get("/mapeo").status_code == 200 and "plano.js" in c.get("/devoluciones").text


def test_cada_prenda_se_confirma_al_instante(monkeypatch):
    from app.services import ruta_devolucion as ruta_svc
    from app.services import deposito_drive
    enviados, excel = [], []
    monkeypatch.setattr(svc, "enviar", lambda d: enviados.append(d.id) or {"ok": True, "id": "D-7"})
    monkeypatch.setattr(ruta_svc, "actualizar_excel_dia", lambda fecha: excel.append(fecha))
    with TestClient(app) as c:
        _login(c)
        c.post("/mapeo/api/modulos/cargar")
        ubs = {u["codigo"]: u for u in c.get("/mapeo/api/plano").json()["ubicaciones"]}
        m = next(x for x in c.get("/devoluciones/api/buscar?q=abba 02003").json()["modelos"] if x["articulo"] == "ABBA 02003")
        v = m["variantes"][0]
        c.post("/mapeo/api/asignar", json={"ubicacion_id": ubs["A01-C01-N0"]["id"], "articulo_id": v["id"]})
        res = c.post("/devoluciones/api/confirmar", json={"ruta": True, "items": [
            {"articulo_id": v["id"], "cantidad": 2, "estado": "OPTIMO", "plataforma": "ML", "venta": "2000099999999999"}]}).json()
        did, it = res["devolucion"]["id"], res["ruta"]["paradas"][0]["items"][0]
        assert excel   # el Excel del día ya la muestra pendiente
        r1 = c.post(f"/devoluciones/api/{did}/guardar", json={"item_id": it["item_id"], "codigo": "a01-c01-n0"}).json()
        assert enviados == [did]   # paso 1: Crédito y Reingreso al confirmar
        assert r1["guardados"] == {"A01-C01-N0": 1} and not r1["completa"] and len(excel) == 2
        # el Excel del día muestra la prenda pendiente (1 de 2) con su rack
        from app.db import SessionLocal
        from app.models import Devolucion
        with SessionLocal() as db:
            d = db.get(Devolucion, did)
            fila = deposito_drive._filas_dev([d])[0]
            assert fila[-2] == "A01-C01-N0 x1" and fila[-1] == "Pendiente (1 de 2)"
        # retomar la ruta trae lo confirmado desde el servidor
        assert c.get(f"/devoluciones/api/{did}/ruta").json()["paradas"][0]["items"][0]["guardados"] == {"A01-C01-N0": 1}
        r2 = c.post(f"/devoluciones/api/{did}/guardar", json={"item_id": it["item_id"], "codigo": "A01-C01-N0"}).json()
        assert r2["completa"] and r2["devolucion"]["ruta_estado"] == "DEVUELTO" and enviados == [did]   # no se sube dos veces
        assert c.post(f"/devoluciones/api/{did}/guardar", json={"item_id": it["item_id"], "codigo": "A01-C01-N0"}).status_code == 409
        with SessionLocal() as db:
            fila = deposito_drive._filas_dev([db.get(Devolucion, did)])[0]
            assert fila[-2] == "A01-C01-N0" and fila[-1].startswith("Devuelto a A01-C01-N0 · ")
        assert deposito_drive.ENC_DEV[-2:] == ["Ubicación", "Devuelto al rack"]


def test_ubicacion_de_odoo_se_verifica(monkeypatch):
    """Paso 2: el rack escaneado tiene que coincidir con la ubicación del producto en Odoo."""
    from app.services import ruta_devolucion as ruta_svc
    monkeypatch.setattr(svc, "enviar", lambda d: {"ok": True, "id": "D-8"})
    monkeypatch.setattr(ruta_svc, "actualizar_excel_dia", lambda fecha: None)
    monkeypatch.setattr(ruta_svc, "ubicaciones_odoo", lambda db: {"B01-C07-N0": 67, "C01-C01-N0": 66})
    monkeypatch.setattr(ruta_svc, "ubicacion_de_producto", lambda db, a: "C01-C01-N0")
    with TestClient(app) as c:
        _login(c)
        c.post("/mapeo/api/modulos/cargar")
        m = next(x for x in c.get("/devoluciones/api/buscar?q=abba 02003").json()["modelos"] if x["articulo"] == "ABBA 02003")
        v = m["variantes"][0]
        res = c.post("/devoluciones/api/confirmar", json={"ruta": True, "items": [
            {"articulo_id": v["id"], "cantidad": 1, "estado": "OPTIMO", "plataforma": "TN", "venta": "1262"}]}).json()
        p = res["ruta"]["paradas"]
        assert [x["codigo"] for x in p] == ["C01-C01-N0"] and p[0]["items"][0]["odoo"] == "C01-C01-N0"
        assert res["devolucion"]["drive_ok"]
        # C01-C01-N0 no estaba en Mapeo pero es de Odoo: se acepta como rack
        assert c.get("/devoluciones/api/rack?c=c01-c01-n0").json()["ok"]
        did, it = res["devolucion"]["id"], p[0]["items"][0]["item_id"]
        mal = c.post(f"/devoluciones/api/{did}/guardar", json={"item_id": it, "codigo": "B01-C07-N0"})
        assert mal.status_code == 409 and "No coincide con Odoo" in mal.json()["detail"] and "C01-C01-N0" in mal.json()["detail"]
        ok = c.post(f"/devoluciones/api/{did}/guardar", json={"item_id": it, "codigo": "C01-C01-N0"}).json()
        assert ok["completa"] and ok["devolucion"]["items"][0]["ubicacion"] == "C01-C01-N0"


def test_migrar_codigos_al_formato_del_plano():
    from app.db import SessionLocal
    from app.models import Articulo, Ubicacion, UbicacionArticulo
    from app.services import mapeo
    with TestClient(app) as c:
        _login(c)
        with SessionLocal() as db:
            for cod in ("B1-C3-N1", "A3C2N0", "A03", "A-03-2"):
                if not db.query(Ubicacion).filter_by(codigo=cod).first():
                    db.add(Ubicacion(codigo=cod))
            db.commit()
            a = db.query(Articulo).first()
            db.add(UbicacionArticulo(ubicacion_id=db.query(Ubicacion).filter_by(codigo="B1-C3-N1").one().id, modelo=a.articulo))
            db.commit()
        prev = c.get("/mapeo/api/migrar").json()
        ren = {x["codigo"]: x for x in prev["renombrar"]}
        assert ren["B1-C3-N1"]["nuevo"] == "B01-C03-N1" and ren["B1-C3-N1"]["articulos"] == 1
        assert "A03" in {x["codigo"] for x in prev["incompletas"]} and "A-03-2" in {x["codigo"] for x in prev["fuera"]}
        with SessionLocal() as db:   # la vista previa no toca nada
            assert db.query(Ubicacion).filter_by(codigo="B1-C3-N1").first()
        r = c.post("/mapeo/api/migrar").json()
        assert r["aplicado"] and not c.get("/mapeo/api/migrar").json()["renombrar"]
        assert any(x["modelo"] == a.articulo for x in c.get("/mapeo/api/ubicacion?codigo=B01-C03-N1").json()["asignaciones"])
        with SessionLocal() as db:
            assert not db.query(Ubicacion).filter_by(codigo="B1-C3-N1").first()
            assert mapeo.migrar_codigos(db)["ok"] >= 2
