import re

from fastapi.testclient import TestClient

from app.main import app
from app.services import croquis
from app.services import devoluciones as svc


def _login(c, nombre="Vengil", pin="1234"):
    uid = re.search(r"elegir\((\d+), '" + nombre + r"'\)", c.get("/login").text).group(1)
    assert c.post("/login", data={"usuario_id": uid, "pin": pin}).json()["ok"]


def test_croquis_codigos_y_ruta():
    cods = [c["codigo"] for c in croquis.codigos_iniciales()]
    assert len(cods) == 15 + 24 and "B01-C07-N0" in cods and "B01-C08-N2" in cods and "A03" in cods and "B01" not in cods
    assert croquis.describir("B01-C07-N0") == "Zona B · rack B01 · columna 7 · planta baja"
    assert croquis.partes("b01-c07-n0") == ("B01", 7, 0) and croquis.partes("Z99") is None
    # columnas de izquierda a derecha
    assert croquis.acceso("B01-C01-N0")[0] < croquis.acceso("B01-C08-N0")[0]
    # saliendo de la Persiana 2 (zona A/B) se arranca por lo de la zona A; de la Persiana 1, por la C
    r2 = croquis.ordenar(["C05", "A01", "B01-C04-N1"], "P2")
    r1 = croquis.ordenar(["C05", "A01", "B01-C04-N1"], "P1")
    assert r2["orden"][0] == "A01" and r1["orden"][0] != "A01" and len(r1["tramos"]) == 3
    assert all(len(t) >= 2 for t in r1["tramos"]) and r1["metros_aprox"] > 0
    assert croquis.ordenar(["XX-1", "A02"], "P1")["orden"] == ["A02", "XX-1"]   # lo que no está en el croquis va al final


def test_flujo_ruta_devolucion(monkeypatch):
    enviados = []
    monkeypatch.setattr(svc, "enviar", lambda d: enviados.append(d) or {"ok": True, "id": "D-9"})
    with TestClient(app) as c:
        _login(c)
        assert c.post("/mapeo/api/croquis/cargar").json()["ok"]
        assert c.post("/mapeo/api/croquis/cargar").json()["creadas"] == 0
        cq = c.get("/mapeo/api/croquis").json()
        assert not cq["faltan"] and cq["inicio"] == "P1"
        ubs = {u["codigo"]: u for u in cq["ubicaciones"]}
        m = next(x for x in c.get("/devoluciones/api/buscar?q=abba 02003").json()["modelos"] if x["articulo"] == "ABBA 02003")
        v1 = next(x for x in m["variantes"] if x["color"] == "BLACK" and x["talle"] == "M")
        v2 = next(x for x in m["variantes"] if x["id"] != v1["id"])
        # el modelo está en dos racks: A03 y B01-C07-N0
        c.post("/mapeo/api/asignar", json={"ubicacion_id": ubs["A03"]["id"], "articulo_id": v1["id"]})
        c.post("/mapeo/api/asignar", json={"ubicacion_id": ubs["B01-C07-N0"]["id"], "articulo_id": v1["id"]})
        assert c.post("/mapeo/api/croquis/ajustes", json={"inicio": "P1", "rack_fallas": "C05"}).json()["ok"]
        assert c.post("/mapeo/api/croquis/ajustes", json={"inicio": "P1", "rack_fallas": "Z-NO"}).status_code == 400
        res = c.post("/devoluciones/api/confirmar", json={"ruta": True, "items": [
            {"articulo_id": v1["id"], "cantidad": 2, "estado": "OPTIMO", "plataforma": "ML", "venta": "2000012345678901"},
            {"articulo_id": v1["id"], "cantidad": 1, "estado": "FALLA", "plataforma": "ML", "venta": "2000012345678901"},
            {"articulo_id": v2["id"], "cantidad": 1, "estado": "OPTIMO", "plataforma": "ML", "venta": "2000012345678901"}]}).json()
        d, ruta = res["devolucion"], res["ruta"]
        assert d["ruta_estado"] == "PENDIENTE" and not d["drive_ok"] and not enviados   # Drive recién al devolver
        cods = [p["codigo"] for p in ruta["paradas"]]
        assert "C05" in cods and ruta["rack_fallas"] == "C05" and ruta["inicio"]["nombre"] == "Persiana 1"
        assert len([x for x in cods if x in ("A03", "B01-C07-N0")]) == 1   # elige uno solo de los dos racks
        assert [i["articulo"] for i in ruta["sin_ubicacion"]] == ["ABBA 02003"]   # v2 no tiene rack
        # sin terminar la ruta no se reintenta Drive
        assert c.post(f"/devoluciones/api/{d['id']}/reintentar").status_code == 409
        assert c.get("/devoluciones/api/rack?c=b01-c07-n0").json() == {"ok": True, "codigo": "B01-C07-N0",
                                                                      "descripcion": croquis.describir("B01-C07-N0")}
        assert not c.get("/devoluciones/api/rack?c=Z99").json()["ok"]
        p_opt = next(p for p in ruta["paradas"] if p["codigo"] in ("A03", "B01-C07-N0"))
        p_fal = next(p for p in ruta["paradas"] if p["codigo"] == "C05")
        sin = ruta["sin_ubicacion"][0]
        r = c.post(f"/devoluciones/api/{d['id']}/devuelto", json={"guardados": [
            {"item_id": p_opt["items"][0]["item_id"], "codigo": p_opt["codigo"], "cantidad": 2},
            {"item_id": p_fal["items"][0]["item_id"], "codigo": "C05", "cantidad": 1},
            {"item_id": sin["item_id"], "codigo": "B02", "cantidad": 1}]}).json()
        assert r["devolucion"]["ruta_estado"] == "DEVUELTO" and r["devolucion"]["drive_ok"] and len(enviados) == 1
        assert r["aprendidas"] == 1   # v2 quedó en B02: la próxima vez la ruta la manda ahí
        assert sorted(i["ubicacion"] for i in r["devolucion"]["items"]) == sorted([p_opt["codigo"], "C05", "B02"])
        assert c.post(f"/devoluciones/api/{d['id']}/devuelto", json={"guardados": []}).status_code == 409
        # la próxima devolución de v2 ya va a B02
        res2 = c.post("/devoluciones/api/confirmar", json={"ruta": True, "items": [
            {"articulo_id": v2["id"], "cantidad": 1, "estado": "OPTIMO", "plataforma": "TN", "venta": "1234"}]}).json()
        assert [p["codigo"] for p in res2["ruta"]["paradas"]] == ["B02"] and not res2["ruta"]["sin_ubicacion"]
        # "Guardar en Drive sin devolver"
        s = c.post(f"/devoluciones/api/{res2['devolucion']['id']}/sin-devolver").json()["devolucion"]
        assert s["ruta_estado"] == "SIN_DEVOLVER" and s["drive_ok"] and len(enviados) == 2
        # sin ruta (el botón del resumen) se sube directo, como antes
        res3 = c.post("/devoluciones/api/confirmar", json={"items": [
            {"articulo_id": v2["id"], "cantidad": 1, "estado": "OPTIMO", "plataforma": "TN", "venta": "1235"}]}).json()
        assert res3["devolucion"]["drive_ok"] and "ruta" not in res3 and len(enviados) == 3
        assert c.get("/mapeo").status_code == 200 and "croquis.js" in c.get("/devoluciones").text
