"""Recolección Ecommerce: ventas de 2 o más prendas en una sola ficha (y con una sola persona) y BOX de la mesa."""
from fastapi.testclient import TestClient

from app.main import app
from tests.test_ecommerce import ARCHIVOS, _login


def _crear(adm):
    files = [("archivos", (n, b, "application/octet-stream")) for n, b in ARCHIVOS]
    return adm.post("/ecommerce/api/crear/archivo", files=files).json()["busqueda"]["id"]


def test_venta_de_varias_prendas_va_junta_y_box():
    with TestClient(app) as adm, TestClient(app) as ana, TestClient(app) as beto:
        _login(adm)
        for n, pin in (("Ana", "2468"), ("Beto", "1357")):
            assert adm.post("/admin/api/usuarios", json={"nombre": n, "rol": "deposito", "pin": pin}).json()["ok"]
        _login(ana, "Ana", "2468")
        _login(beto, "Beto", "1357")
        cid = _crear(adm)
        assert ana.post(f"/ecommerce/api/{cid}/sumarme").json()["ok"]
        assert beto.post(f"/ecommerce/api/{cid}/sumarme").json()["ok"]

        # ventas de 2+ prendas: una ficha con la cabecera de la venta, entera para una sola persona
        todo = adm.get(f"/ecommerce/api/{cid}/todo").json()["grupos"]
        multi = [g for g in todo if g["venta"]]
        assert multi, "el CSV de TN trae la orden 1283 con 2 prendas"
        por_venta = {}
        for g in multi:
            por_venta.setdefault(g["venta"]["clave"], set()).add(g["asignado_id"])
        assert all(len(x) == 1 for x in por_venta.values())
        # las prendas de la venta quedan seguidas en la guía
        claves = [g["venta"]["clave"] if g["venta"] else None for g in todo]
        for v in por_venta:
            idx = [k for k, x in enumerate(claves) if x == v]
            assert idx == list(range(idx[0], idx[0] + len(idx)))
        v1283 = next(g["venta"] for g in multi if g["venta"]["venta_id"] == "1283" or g["venta"]["tracking"] == "1283")
        assert v1283["prendas"] == 2

        # BOX: cada persona tiene los suyos y la prenda entra sola al marcarla
        assert ana.post("/ecommerce/api/box/cantidad", json={"cantidad": 2}).status_code == 403
        assert adm.post("/ecommerce/api/box/cantidad", json={"cantidad": 2}).json()["cantidad"] == 2
        d = ana.get(f"/ecommerce/api/{cid}/mia").json()
        assert d["box"]["actual"] is None and d["box"]["cantidad"] == 2

        def marcar(cli, g):
            if g["vinculado"] and g["codigo_barras"]:
                r = cli.post(f"/ecommerce/api/{cid}/encontre", json={"codigo": g["codigo_barras"]}).json()
            else:
                r = cli.post(f"/ecommerce/api/{cid}/la-tengo", json={"item_ids": g["item_ids"]}).json()
            assert r["ok"], r
            return r

        g = next(x for x in d["grupos"] if x["faltan"])
        r = marcar(ana, g)
        assert r["box"]["actual"] == 1 and r["box"]["en_actual"] == 1
        g = next(x for x in ana.get(f"/ecommerce/api/{cid}/mia").json()["grupos"] if x["item_ids"] == g["item_ids"])
        assert g["boxes"] == [1] and g["en_box"] == 1

        # −1: sale del BOX (y el BOX vacío desaparece)
        assert ana.post(f"/ecommerce/api/{cid}/deshacer", json={"item_ids": g["item_ids"]}).json()["ok"]
        assert ana.get(f"/ecommerce/api/{cid}/mia").json()["box"]["actual"] is None
        r = marcar(ana, g)
        assert r["box"]["actual"] == 1

        # Beto arranca en el BOX 2: el número es único en toda la búsqueda (no hay dos BOX 1 en la mesa)
        gb = next(x for x in beto.get(f"/ecommerce/api/{cid}/mia").json()["grupos"] if x["faltan"])
        assert marcar(beto, gb)["box"]["actual"] == 2
        bs = adm.get(f"/ecommerce/api/{cid}/boxes").json()
        assert bs["cantidad"] == 2
        assert [(b["quien"], b["numero"]) for b in bs["boxes"]] == [("Ana", 1), ("Beto", 2)]
        ba = next(b for b in bs["boxes"] if b["quien"] == "Ana")
        assert ba["nombre"] == "BOX 1 · Ana" and ba["activo"] and ba["esperando"] == 1
        venta = ba["ventas"][0]

        # mesa de control: al escanear la etiqueta dice de quién y qué BOX
        p = ana.post(f"/control/api/{cid}/escanear", json={"codigo": venta["tracking"]}).json()
        assert p["tipo"] == "paquete" and p["paquete"]["boxes"] == [1]
        assert p["paquete"]["boxes_det"][0]["nombre"] == "BOX 1 · Ana"


def test_box_por_persona_se_llena_solo_y_no_parte_ventas():
    """Servicio: de a N prendas por BOX, número único en la búsqueda, una venta entera en el mismo BOX."""
    from app.db import SessionLocal
    from app.models import Control, ControlItem, ControlPaquete, Usuario
    from app.services import ajustes
    from app.services import ecommerce as svc
    from sqlalchemy import select
    db = SessionLocal()
    try:
        u = db.scalar(select(Usuario).where(Usuario.nombre == "Vengil"))
        otro = db.scalar(select(Usuario).where(Usuario.id != u.id))
        c = Control(fecha=svc.hoy(), usuario_id=u.id, modo=svc.MODO, nombre="test box")
        db.add(c)
        for k, cant in enumerate((1, 1, 3, 1, 1)):   # venta de 3 prendas en el medio
            p = ControlPaquete(tracking=f"T{k}", tipo="flex")
            for j in range(cant):
                p.items.append(ControlItem(sku=f"S{k}{j}", cantidad=1, orden_ruta=k * 10 + j))
            c.paquetes.append(p)
        db.flush()
        items = [i for p in c.paquetes for i in p.items]
        for i in items:
            i.asignado_id = u.id
        items[-1].asignado_id = otro.id
        db.commit()
        ajustes.guardar(db, "re_box_cant", "3")

        def marcar(i, quien):
            svc.marcar_buscado(db, i, quien, +1)
            return [b.numero for b in svc.asignar_box(db, c, quien.id)]

        assert marcar(items[0], u) == []                 # T0 → BOX 1
        assert marcar(items[1], u) == []                 # T1 → BOX 1 (2 de 3)
        assert marcar(items[2], u) == []                 # venta de 3 no entra en el lugar que queda → BOX 2
        assert marcar(items[3], u) == []
        assert marcar(items[4], u) == [2]                # BOX 2 lleno con la venta entera
        assert marcar(items[5], u) == []                 # T3 → BOX 3 (el 1 quedó con 2, la venta no entraba)
        assert marcar(items[6], otro) == []              # la otra persona: el próximo número libre (BOX 4)
        mios = svc.boxes_de(c, u.id)
        assert [(b.numero, svc.unidades_box(b)) for b in mios] == [(1, 2), (2, 3), (3, 1)]
        assert [(b.numero, svc.unidades_box(b)) for b in svc.boxes_de(c, otro.id)] == [(4, 1)]
        assert svc.estado_box(db, c, u.id)["actual"] == 3

        # −1 de la única prenda del BOX 3 → el BOX 3 desaparece
        svc.restar(db, c, [items[5]], u)
        assert [b.numero for b in svc.boxes_de(c, u.id)] == [1, 2]
        assert marcar(items[5], u) == []
        assert [b.numero for b in svc.boxes_de(c, u.id)] == [1, 2, 3]

        # una venta controlada en la mesa ya no se puede restar
        c.paquetes[0].estado = "completo"
        db.commit()
        try:
            svc.restar(db, c, [items[0]], u)
            assert False, "tendría que fallar"
        except ValueError:
            pass
        b1 = next(b for b in svc.boxes(c) if b["usuario_id"] == u.id and b["numero"] == 1)
        assert b1["esperando"] == 1 and b1["controladas"] == 1
    finally:
        ajustes.guardar(db, "re_box_cant", "5")
        db.close()


def test_mesa_por_box_detecta_la_venta_sola():
    """Mesa de control por BOX: con el BOX elegido alcanza con escanear las prendas (sin la etiqueta)."""
    from app.db import SessionLocal
    from app.models import Articulo
    with TestClient(app) as adm:
        _login(adm)
        cid = _crear(adm)
        assert adm.post(f"/ecommerce/api/{cid}/sumarme").json()["ok"]
        assert adm.post("/ecommerce/api/box/cantidad", json={"cantidad": 50}).json()["ok"]
        gs = [g for g in adm.get(f"/ecommerce/api/{cid}/mia").json()["grupos"] if g["vinculado"] and g["codigo_barras"]]
        uno = next(g for g in gs if not g["venta"] and g["cantidad"] == 1)
        assert adm.post(f"/ecommerce/api/{cid}/la-tengo", json={"item_ids": uno["item_ids"]}).json()["ok"]
        bs = adm.get(f"/ecommerce/api/{cid}/boxes").json()["boxes"]
        assert len(bs) == 1 and bs[0]["numero"] == 1
        box = bs[0]
        # el BOX se está llenando (quedan prendas por buscar y no llegó a 50)
        assert box["mesa"] == "llenando" and box["ventas"][0]["items"][0]["escaneado"] == 0
        # una prenda que no está en este BOX
        otra = next(g for g in gs if g["item_ids"] != uno["item_ids"] and g["codigo_barras"] != uno["codigo_barras"])
        r = adm.post(f"/ecommerce/api/{cid}/mesa/escanear", json={"codigo": otra["codigo_barras"], "box_id": box["id"]}).json()
        assert r["ok"] is False and r["motivo"] == "otro_box"
        # la prenda del BOX: encuentra sola la venta y la controla
        r = adm.post(f"/ecommerce/api/{cid}/mesa/escanear", json={"codigo": uno["codigo_barras"], "box_id": box["id"]}).json()
        assert r["ok"] and r["auto"] and r["completo"] and r["paquete"]["estado"] == "completo"
        assert r["box"]["controladas"] == 1 and r["box"]["mesa"] == "controlado"
        # otra vez la misma: ya no hay venta pendiente en el BOX que la lleve
        r = adm.post(f"/ecommerce/api/{cid}/mesa/escanear", json={"codigo": uno["codigo_barras"], "box_id": box["id"]}).json()
        assert r["ok"] is False
        # la etiqueta de una venta abre la venta (como la mesa de antes)
        r = adm.post(f"/ecommerce/api/{cid}/mesa/escanear", json={"codigo": r.get("paquete", {}).get("tracking") or box["ventas"][0]["tracking"],
                                                               "box_id": box["id"]}).json()
        assert r["tipo"] == "paquete" and r["en_este_box"]
        with SessionLocal() as db:
            assert db.query(Articulo).count() > 0
