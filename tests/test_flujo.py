from fastapi.testclient import TestClient

from app.main import app
from tests.test_parsers import ZPL


def login(c, nombre, pin):
    html = c.get("/login").text
    import re
    uid = re.search(r"elegir\((\d+), '" + nombre + r"'\)", html).group(1)
    r = c.post("/login", data={"usuario_id": uid, "pin": pin})
    return r


def tarea_lote(c, nombre="Flex mañana"):
    return next(t for t in c.get("/despacho/api/estado").json()["tareas"] if nombre in t["titulo"])


def test_flujo_completo():
    with TestClient(app) as admin:
        # PIN incorrecto y correcto
        assert login(admin, "Vengil", "0000").status_code == 400
        assert login(admin, "Vengil", "1234").json()["ok"]
        assert admin.get("/").status_code == 200

        # crear operario de despacho y uno de depósito
        assert admin.post("/admin/api/usuarios", json={"nombre": "Cintia", "rol": "despacho", "pin": "4321"}).json()["ok"]
        assert admin.post("/admin/api/usuarios", json={"nombre": "Pedro", "rol": "deposito", "pin": "5555"}).json()["ok"]
        r = admin.post("/admin/api/usuarios", json={"nombre": "X", "rol": "despacho", "pin": "12"})
        assert r.status_code == 400

        # cargar lote flex con ZPL
        r = admin.post("/despacho/api/preview", data={"tipo": "flex"}, files=[("archivos", ("et.txt", ZPL.encode()))])
        assert r.json()["resultados"][0]["cantidad"] == 2
        r = admin.post("/despacho/api/lotes", data={"tipo": "flex", "nombre": "Flex mañana"},
                       files=[("archivos", ("et.txt", ZPL.encode()))])
        assert r.json()["cantidad"] == 2
        lote_id = r.json()["lote_id"]

        # tarea automática creada
        t = tarea_lote(admin)
        assert t["origen"] == "auto" and t["estado"] == "pendiente"

    with TestClient(app) as op:
        assert login(op, "Cintia", "4321").json()["ok"]
        # operario no entra a admin
        assert op.get("/admin/usuarios").status_code == 403
        assert op.get("/supervision/api/datos").status_code == 403
        # tanda + escaneos
        assert op.post("/despacho/api/tanda/iniciar").json()["ok"]
        r = op.post("/despacho/api/scan", json={"codigo": '{"id":"47383333425","t":"lm"}'}).json()
        assert r["resultado"] == "ok" and r["lote"]["hechos"] == 1
        assert tarea_lote(op)["estado"] == "en_curso"
        assert op.post("/despacho/api/scan", json={"codigo": ">:47383333425"}).json()["resultado"] == "dup"
        assert op.post("/despacho/api/scan", json={"codigo": "11111111"}).json()["resultado"] == "no_encontrado"
        r = op.post("/despacho/api/scan", json={"codigo": "]C047383333999"}).json()
        assert r["resultado"] == "ok" and r["lote"]["hechos"] == 2
        # la tarea del lote se completa sola
        t = tarea_lote(op)
        assert t["estado"] == "hecha" and t["hecha_por"] == "Cintia"
        fin = op.post("/despacho/api/tanda/finalizar").json()
        assert fin["cantidad"] == 2
        x = op.get(f"/despacho/tanda/{fin['id']}.xlsx")
        assert x.status_code == 200 and x.content[:2] == b"PK"
        # tareas manuales
        r = op.post("/tareas/api", json={"seccion": "despacho", "titulo": "Reponer cinta de embalar"}).json()
        tid = r["tarea"]["id"]
        assert op.post(f"/tareas/api/{tid}/tomar").json()["tarea"]["asignado_a"] == "Cintia"
        assert op.post(f"/tareas/api/{tid}/completar").json()["tarea"]["estado"] == "hecha"
        # no puede crear tareas en Depósito (no tiene la sección)
        assert op.post("/tareas/api", json={"seccion": "devoluciones", "titulo": "x"}).status_code == 403

    with TestClient(app) as dep:
        assert login(dep, "Pedro", "5555").json()["ok"]
        assert dep.get("/s/devoluciones", follow_redirects=False).headers["location"] == "/devoluciones"
        assert dep.get("/despacho").status_code == 200

    with TestClient(app) as admin:
        login(admin, "Vengil", "1234")
        # asignar tarea a Pedro
        r = admin.post("/tareas/api", json={"seccion": "devoluciones", "titulo": "Contar pasillo A",
                                            "asignado_a_id": None}).json()
        tid = r["tarea"]["id"]
        usuarios = admin.get("/tareas").text
        import re
        pid = re.search(r'<option value="(\d+)">Pedro', usuarios).group(1)
        assert admin.post(f"/tareas/api/{tid}/asignar", json={"usuario_id": int(pid)}).json()["tarea"]["asignado_a"] == "Pedro"
        # supervisión
        d = admin.get("/supervision/api/datos").json()
        ops = {o["nombre"]: o for o in d["operarios"]}
        assert ops["Cintia"]["paquetes"] == 2 and ops["Cintia"]["errores"] == 1 and ops["Cintia"]["tareas"] == 1
        # cierre
        v = admin.get("/admin/cierre/vista").text
        assert "Cierre del día" in v and "Cintia" in v
        x = admin.get("/admin/cierre/excel")
        assert x.status_code == 200 and x.content[:2] == b"PK"
        r = admin.post("/admin/api/cierre/enviar", json={})
        assert r.status_code == 400 and "SMTP" in r.json()["detail"]
        # páginas renderizan
        for url in ["/", "/tareas", "/supervision", "/admin/usuarios", "/admin/secciones", "/admin/cierre",
                    "/despacho", "/despacho/escanear", f"/despacho/lotes/{lote_id}.xlsx"]:
            assert admin.get(url).status_code == 200, url
        # cerrar lote
        assert admin.post(f"/despacho/api/lotes/{lote_id}/cerrar").json()["ok"]
        assert admin.post("/despacho/api/scan", json={"codigo": "47383333425"}).json()["resultado"] == "no_encontrado"


def test_sin_sesion_redirige():
    with TestClient(app) as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert c.get("/despacho/api/estado").status_code == 401
