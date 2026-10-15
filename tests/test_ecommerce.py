"""Recolección Ecommerce: Excel real de ventas de ML (Flex y Colecta) y CSV de Tienda Nube (anonimizados),
reparto de la ruta entre los operarios que se suman, escaneo en la búsqueda (verde / rojo) y mesa de control."""
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

DATOS = Path(__file__).parent / "datos"
FLEX = (DATOS / "ventas_ml_flex_ejemplo.xlsx").read_bytes()
COLECTA = (DATOS / "ventas_ml_colecta_ejemplo.xlsx").read_bytes()
TN = (DATOS / "ventas_tn_ejemplo.csv").read_bytes()
ARCHIVOS = [("20261006_Ventas_AR_Mercado_Libre_y_Mercado_Shops_flex.xlsx", FLEX),
            ("20261006_Ventas_AR_Mercado_Libre_y_Mercado_Shops_colecta.xlsx", COLECTA),
            ("ventas-abc.csv", TN)]


def _login(c, nombre="Vengil", pin="1234"):
    uid = re.search(r"elegir\((\d+), '" + nombre + r"'\)", c.get("/login").text).group(1)
    assert c.post("/login", data={"usuario_id": uid, "pin": pin}).json()["ok"]


def _codigo(sku: str) -> str:
    from app.db import SessionLocal
    from app.models import Articulo
    from sqlalchemy import select
    db = SessionLocal()
    try:
        return db.scalar(select(Articulo.codigo_barras).where(Articulo.sku == sku))
    finally:
        db.close()


def test_parser_excel_ml_y_csv_tn():
    from app.parsers import ventas_ml, tracking_ml, variante_tn
    flex = ventas_ml("v.xlsx", FLEX)
    assert len(flex) == 7 and {p["tipo"] for p in flex} == {"flex"}
    p = next(x for x in flex if x["tracking"] == "48185193093")
    assert p["venta_id"] == "2000015379915829" and p["items"][0]["sku"] == "W-2055-BLK-S"
    assert p["items"][0]["talle"] == "S" and "Talle: S" in p["items"][0]["variante"]
    col = ventas_ml("v.xlsx", COLECTA)
    assert len(col) == 5 and {p["tipo"] for p in col} == {"colecta"}
    assert any(p["tracking"] == "48183013451" and p["tracking_alt"] == "MEL48183013451FMXDF01" for p in col)
    assert tracking_ml("MEL48183013451FMXDF01") == ("48183013451", "MEL48183013451FMXDF01")
    assert variante_tn("Campera Beast (Gris, M)") == ("Gris", "M")
    assert ventas_ml("v.csv", TN) is None

    from app.services.control import leer_archivos
    ps, errores = leer_archivos(ARCHIVOS, solo_pendientes=True)
    assert len(ps) == 17 and sum(i["cantidad"] for p in ps for i in p["items"]) == 18 and not errores
    tn = next(x for x in ps if x["tracking"] == "1283")
    assert [i["sku"] for i in tn["items"]] == ["M-1188-BLK-M", "M-1188-GREY-M"] and tn["tipo"] == "tiendanube"


def test_sku_mochila_sin_talle():
    from app.db import SessionLocal
    from app.services import mapeo
    db = SessionLocal()
    try:
        assert mapeo.articulo_por_sku(db, "B-3014-BLK").sku == "4-MOCH-U-03014-BLCK-PARK"
    finally:
        db.close()


def test_seccion_unica_y_accesos_migrados():
    from app.db import SessionLocal
    from app.models import Seccion, Usuario
    from app.auth import hash_pin
    from app.seed import sembrar
    from sqlalchemy import select
    db = SessionLocal()
    try:
        db.add(Seccion(slug="control", nombre="Control de paquetes", roles="admin,deposito"))
        db.add(Usuario(nombre="Lucas", rol="deposito", pin_hash=hash_pin("7777"), accesos="recoleccion,control,mapeo"))
        db.commit()
        sembrar(db)
        slugs = {s.slug for s in db.scalars(select(Seccion)).all()}
        assert "ecommerce" in slugs and not {"control", "recoleccion"} & slugs
        assert db.scalar(select(Usuario).where(Usuario.nombre == "Lucas")).accesos == "mapeo,ecommerce"
    finally:
        db.close()
    with TestClient(app) as c:
        _login(c)
        inicio = c.get("/").text
        assert "/s/ecommerce" in inicio and "Recolección Ecommerce" in inicio
        assert "/s/recoleccion" not in inicio and "/s/control\"" not in inicio
        assert c.get("/s/ecommerce", follow_redirects=False).headers["location"] == "/ecommerce"


def test_flujo_completo():
    with TestClient(app) as adm, TestClient(app) as ana, TestClient(app) as beto:
        _login(adm)
        for n, pin in (("Ana", "2468"), ("Beto", "1357")):
            assert adm.post("/admin/api/usuarios", json={"nombre": n, "rol": "deposito", "pin": pin}).json()["ok"]
        _login(ana, "Ana", "2468")
        _login(beto, "Beto", "1357")
        assert ana.get("/ecommerce").status_code == 200

        # crear: solo quien organiza
        files = [("archivos", (n, b, "application/octet-stream")) for n, b in ARCHIVOS]
        assert ana.post("/ecommerce/api/crear/archivo", files=files).status_code == 403
        r = adm.post("/ecommerce/api/crear/archivo", files=files).json()
        b = r["busqueda"]
        cid = b["id"]
        assert b["paquetes"] == 17 and b["busqueda"]["unidades"] == 18
        assert b["tipos"] == {"Flex": 7, "Colecta": 5, "Tienda Nube": 5}
        assert b["busqueda"]["sin_asignar"] == 18 and b["sin_vincular"] == 2  # WORKJAC2 ×2 (PUFCOMBI ya cruza: DUNE PUFCOMBI)

        # se suman: la ruta se reparte en dos tramos parejos
        assert ana.post(f"/ecommerce/api/{cid}/sumarme").json()["busqueda"]["me_sume"]
        eq = beto.post(f"/ecommerce/api/{cid}/sumarme").json()["busqueda"]["busqueda"]
        assert eq["sin_asignar"] == 0
        cargas = {p["nombre"]: p["unidades"] for p in eq["equipo"]}
        assert sum(cargas.values()) == 18 and abs(cargas["Ana"] - cargas["Beto"]) <= 4

        mia_ana = ana.get(f"/ecommerce/api/{cid}/mia").json()["grupos"]
        mia_beto = beto.get(f"/ecommerce/api/{cid}/mia").json()["grupos"]
        assert mia_ana and mia_beto
        claves_a = {g["clave"] for g in mia_ana}
        assert not claves_a & {g["clave"] for g in mia_beto}  # la misma prenda no se parte entre dos

        # Ana escanea una prenda suya → OK (verde)
        g = next(x for x in mia_ana if x["vinculado"] and x["codigo_barras"])
        r = ana.post(f"/ecommerce/api/{cid}/encontre", json={"codigo": g["codigo_barras"]}).json()
        assert r["ok"] and r["busqueda"]["busqueda"]["buscadas"] == 1
        # una de Beto → rojo, dice quién la busca
        gb = next(x for x in mia_beto if x["vinculado"] and x["codigo_barras"])
        r = ana.post(f"/ecommerce/api/{cid}/encontre", json={"codigo": gb["codigo_barras"]}).json()
        assert not r["ok"] and r["motivo"] == "otro" and "Beto" in r["mensaje"]
        # código desconocido → rojo
        r = ana.post(f"/ecommerce/api/{cid}/encontre", json={"codigo": "9999999999999"}).json()
        assert not r["ok"] and r["motivo"] == "desconocido"
        # la misma prenda pero No Brand → rojo (ecommerce es todo Parka)
        nobr = _codigo("1-CAMP-W-02101-BLCK-M-NOBR")
        if nobr:
            r = ana.post(f"/ecommerce/api/{cid}/encontre", json={"codigo": nobr}).json()
            assert not r["ok"]
        # sumar a mano una prenda vinculada: no (se escanea)
        assert ana.post(f"/ecommerce/api/{cid}/la-tengo", json={"item_ids": g["item_ids"]}).status_code == 400
        # sin vincular: sí
        sv = next((x for x in mia_ana + mia_beto if not x["vinculado"]), None)
        quien = ana if sv in mia_ana else beto
        assert quien.post(f"/ecommerce/api/{cid}/la-tengo", json={"item_ids": sv["item_ids"]}).json()["ok"]
        # −1
        assert ana.post(f"/ecommerce/api/{cid}/deshacer", json={"item_ids": g["item_ids"]}).json()["ok"]
        assert ana.post(f"/ecommerce/api/{cid}/encontre", json={"codigo": g["codigo_barras"]}).json()["ok"]

        # faltante → tarea en la sección
        otro = next(x for x in mia_beto if x["vinculado"] and x["clave"] != gb["clave"])
        assert beto.post(f"/ecommerce/api/{cid}/faltante", json={"item_ids": otro["item_ids"], "nota": "no hay"}).json()["lineas"] >= 1
        from app.db import SessionLocal
        from app.models import Tarea
        from sqlalchemy import select
        db = SessionLocal()
        try:
            assert db.scalar(select(Tarea).where(Tarea.seccion_slug == "ecommerce", Tarea.prioridad == "alta"))
        finally:
            db.close()

        # Beto se va: lo que le faltaba pasa a Ana
        eq = beto.post(f"/ecommerce/api/{cid}/salir").json()["busqueda"]["busqueda"]
        assert eq["sin_asignar"] == 0
        assert {p["nombre"]: p["activo"] for p in eq["equipo"]} == {"Ana": True, "Beto": False}
        assert sum(x["faltan"] for x in ana.get(f"/ecommerce/api/{cid}/mia").json()["grupos"]) == eq["faltan"]

        # mesa de control (con la API de /control, usuario sin acceso a la vieja sección)
        assert ana.get(f"/control?re={cid}").status_code == 200
        r = ana.post(f"/control/api/{cid}/escanear", json={"codigo": "48183013451"}).json()
        assert r["tipo"] == "paquete" and r["paquete"]["tipo"] == "colecta"
        pk = r["paquete"]
        it = pk["items"][0]
        if it["codigo_barras"]:
            r = ana.post(f"/control/api/{cid}/escanear", json={"codigo": it["codigo_barras"], "paquete_id": pk["id"]}).json()
            assert r["ok"] and r["completo"]
        r = ana.post(f"/control/api/{cid}/escanear", json={"codigo": "48185193093"}).json()
        assert r["tipo"] == "paquete"
        r = ana.post(f"/control/api/{cid}/escanear", json={"codigo": g["codigo_barras"], "paquete_id": r["paquete"]["id"]}).json()
        assert r["tipo"] == "prenda"  # la prenda de otro paquete: rojo salvo que justo sea la de este
        # TN: se escanea el nº de orden
        assert ana.post(f"/control/api/{cid}/escanear", json={"codigo": "1283"}).json()["paquete"]["unidades"] == 2

        # cerrar: solo quien organiza; sin Drive conectado se cierra igual
        assert ana.post(f"/ecommerce/api/{cid}/cerrar").status_code == 403
        r = adm.post(f"/ecommerce/api/{cid}/cerrar").json()
        assert r["ok"] and r["busqueda"]["estado"] == "cerrado"
        assert ana.post(f"/ecommerce/api/{cid}/encontre", json={"codigo": g["codigo_barras"]}).status_code == 400
        lista = ana.get("/ecommerce/api/lista").json()["busquedas"]
        assert lista[0]["id"] == cid


def test_crear_desde_drive(monkeypatch):
    from app.services import re_drive
    nombres = {"f1": ARCHIVOS[0], "f2": ARCHIVOS[2]}
    monkeypatch.setattr(re_drive, "configurado", lambda db=None: True)
    monkeypatch.setattr(re_drive, "hoy", lambda db=None: {
        "fecha": "2026-10-06", "url": "https://drive.google.com/x", "archivos": [
            {"id": "f1", "nombre": ARCHIVOS[0][0], "tipo": "ml", "mime": "", "bytes": 1000, "modificado": "2026-10-06T11:42:00"},
            {"id": "f2", "nombre": "ventas-abc.csv", "tipo": "tn", "mime": "", "bytes": 900, "modificado": "2026-10-06T11:45:00"},
            {"id": "f3", "nombre": "RE-1 cierre.xlsx", "tipo": "resultado", "mime": "", "bytes": 5, "modificado": "2026-10-06T18:00:00"}]})
    monkeypatch.setattr(re_drive, "archivo", lambda db, i: nombres[i])
    guardados = []
    monkeypatch.setattr(re_drive, "guardar", lambda db, fecha, nombre, contenido, mime=None:
                        guardados.append((fecha, nombre, len(contenido))) or {"url": "https://drive.google.com/r"})
    with TestClient(app) as c:
        _login(c)
        d = c.get("/ecommerce/api/drive").json()
        assert [a["id"] for a in d["archivos"]] == ["f1", "f2"]  # el resultado no se ofrece para cargar
        r = c.post("/ecommerce/api/crear/drive", json={"ids": ["f1", "f2"]}).json()
        assert r["busqueda"]["paquetes"] == 12 and r["busqueda"]["nombre"].startswith("Flex + Tienda Nube")
        cid = r["busqueda"]["id"]
        r = c.post(f"/ecommerce/api/{cid}/cerrar").json()
        assert r["busqueda"]["drive_resultado"] == "https://drive.google.com/r" and not r["aviso"]
        assert guardados and guardados[0][1].startswith(f"RE-{cid} ")


def test_organiza_suma_personas_y_todos_ven_participantes():
    with TestClient(app) as adm, TestClient(app) as caro:
        _login(adm)
        for n, pin in (("Caro", "2580"), ("Dani", "1470")):
            assert adm.post("/admin/api/usuarios", json={"nombre": n, "rol": "deposito", "pin": pin}).json()["ok"]
        _login(caro, "Caro", "2580")
        files = [("archivos", (n, b, "application/octet-stream")) for n, b in ARCHIVOS]
        cid = adm.post("/ecommerce/api/crear/archivo", files=files).json()["busqueda"]["id"]

        cand = adm.get(f"/ecommerce/api/{cid}/candidatos").json()["usuarios"]
        ids = {x["nombre"]: x["id"] for x in cand}
        assert {"Caro", "Dani", "Vengil"} <= set(ids) and not any(x["esta"] for x in cand)
        # un operario no puede sumar gente ni ver la lista de candidatos
        assert caro.get(f"/ecommerce/api/{cid}/candidatos").status_code == 403
        assert caro.post(f"/ecommerce/api/{cid}/sumar", json={"usuario_ids": [ids["Dani"]]}).status_code == 403

        r = adm.post(f"/ecommerce/api/{cid}/sumar", json={"usuario_ids": [ids["Caro"], ids["Dani"], ids["Caro"]]}).json()
        assert sorted(r["sumados"]) == ["Caro", "Dani"]
        eq = r["busqueda"]["busqueda"]
        assert eq["sin_asignar"] == 0 and {p["nombre"] for p in eq["equipo"] if p["activo"]} == {"Caro", "Dani"}
        # repetir no duplica
        assert adm.post(f"/ecommerce/api/{cid}/sumar", json={"usuario_ids": [ids["Caro"]]}).json()["sumados"] == []
        assert {x["nombre"] for x in adm.get(f"/ecommerce/api/{cid}/candidatos").json()["usuarios"] if x["esta"]} == {"Caro", "Dani"}

        # Caro quedó sumada sin tocar "Me sumo" y ya tiene su tramo
        b = caro.get(f"/ecommerce/api/{cid}").json()["busqueda"]
        assert b["me_sume"] and caro.get(f"/ecommerce/api/{cid}/mia").json()["grupos"]
        # el admin (sin estar sumado) ve los participantes en la lista y en la búsqueda
        lista = adm.get("/ecommerce/api/lista").json()["busquedas"]
        mia = next(x for x in lista if x["id"] == cid)
        assert not mia["me_sume"] and {p["nombre"] for p in mia["busqueda"]["equipo"]} == {"Caro", "Dani"}
        assert "Sumar personas" in adm.get("/ecommerce").text and "Sumar personas" not in caro.get("/ecommerce").text


def test_todos_ven_lo_de_los_demas_y_queda_quien_la_trajo():
    """Cada uno busca lo suyo, pero ve (plegado) lo que buscan los demás con el nombre. Si trae una de otro,
    confirma y queda a su nombre; la guía completa dice quién la trajo."""
    with TestClient(app) as adm, TestClient(app) as eva, TestClient(app) as fede:
        _login(adm)
        for n, pin in (("Eva", "2461"), ("Fede", "1351")):
            adm.post("/admin/api/usuarios", json={"nombre": n, "rol": "deposito", "pin": pin})
        _login(eva, "Eva", "2461")
        _login(fede, "Fede", "1351")
        files = [("archivos", (n, b, "application/octet-stream")) for n, b in ARCHIVOS]
        cid = adm.post("/ecommerce/api/crear/archivo", files=files).json()["busqueda"]["id"]
        eva.post(f"/ecommerce/api/{cid}/sumarme")
        fede.post(f"/ecommerce/api/{cid}/sumarme")
        d_eva = eva.get(f"/ecommerce/api/{cid}/mia").json()
        d_fede = fede.get(f"/ecommerce/api/{cid}/mia").json()
        # Eva ve lo de Fede en "otros", con el nombre
        ids_fede = {i for g in d_fede["grupos"] for i in g["item_ids"]}
        assert ids_fede == {i for g in d_eva["otros"] for i in g["item_ids"]}
        assert all(g["asignado"] == "Fede" for g in d_eva["otros"])
        gf = next(x for x in d_fede["grupos"] if x["vinculado"] and x["codigo_barras"] and x["faltan"])
        # "La tengo" sobre una de Fede: primero pide confirmación
        r = eva.post(f"/ecommerce/api/{cid}/la-tengo", json={"item_ids": gf["item_ids"]}).json()
        assert r["ok"] is False and r["motivo"] == "otro" and r["quien"] == "Fede"
        # escaneada: avisa de quién es y devuelve los ítems para confirmar
        r = eva.post(f"/ecommerce/api/{cid}/encontre", json={"codigo": gf["codigo_barras"]}).json()
        assert r["motivo"] == "otro" and r["quien"] == "Fede" and r["item_ids"]
        # confirma: queda a nombre de Eva
        assert eva.post(f"/ecommerce/api/{cid}/la-tengo", json={"item_ids": r["item_ids"], "ajena": True}).json()["ok"]
        mia = eva.get(f"/ecommerce/api/{cid}/mia").json()
        g = next(x for x in mia["grupos"] if set(r["item_ids"]) & set(x["item_ids"]))
        assert g["buscado"] >= 1 and g["trajo"] == ["Eva"]
        todo = adm.get(f"/ecommerce/api/{cid}/todo").json()["grupos"]
        assert any(x["trajo"] == ["Eva"] for x in todo)
        # una suya: OK directo y queda a su nombre
        ge = next(x for x in d_eva["grupos"] if x["faltan"])
        assert eva.post(f"/ecommerce/api/{cid}/la-tengo", json={"item_ids": ge["item_ids"]}).json()["ok"]


def test_fichas_con_canal_ventas_y_sku_odoo():
    """Cada prenda trae el SKU de Odoo, los números de venta y el canal (ML / TN / cambio) para el ícono."""
    with TestClient(app) as c:
        _login(c)
        r = c.post("/ecommerce/api/crear/archivo", data={"nombre": "Canales"},
                   files=[("archivos", ARCHIVOS[0]), ("archivos", ARCHIVOS[2])]).json()
        cid = r["busqueda"]["id"]
        gs = c.get(f"/ecommerce/api/{cid}/todo").json()["grupos"]
        ml = next(g for g in gs if g["sku"] == "W-2055-BLK-S")
        assert ml["sku_odoo"] == "1-CAMP-W-02055-BLCK-S-PARK" and ml["canales"] == ["ml"]
        v = next(x for x in ml["ventas"] if x["tracking"] == "48185193093")
        assert (v["canal"], v["numero"], v["tipo"]) == ("ml", "2000015379915829", "Flex")
        assert len(v["hora"]) == 5 and ml["entro"] == min(x["hora"] for x in ml["ventas"])   # hora en que entró (HH:MM)
        tn = next(g for g in gs if "tn" in g["canales"])
        assert tn["ventas"][0]["canal"] == "tn" and tn["ventas"][0]["numero"] == tn["ventas"][0]["tracking"]
        assert c.get("/ecommerce").status_code == 200
