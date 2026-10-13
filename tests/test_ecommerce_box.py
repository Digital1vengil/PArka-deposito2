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

        # BOX: se habilita al juntar la cantidad configurada
        assert ana.post("/ecommerce/api/box/cantidad", json={"cantidad": 2}).status_code == 403
        assert adm.post("/ecommerce/api/box/cantidad", json={"cantidad": 2}).json()["cantidad"] == 2
        d = ana.get(f"/ecommerce/api/{cid}/mia").json()
        assert d["box"] == {"en_mano": 0, "cantidad": 2, "pendientes": d["box"]["pendientes"], "habilitado": False}
        assert ana.post(f"/ecommerce/api/{cid}/box/llevar", json={}).status_code == 400

        juntadas = 0
        for g in d["grupos"]:
            if not g["faltan"]:
                continue
            for _ in range(g["faltan"]):
                if g["vinculado"] and g["codigo_barras"]:
                    r = ana.post(f"/ecommerce/api/{cid}/encontre", json={"codigo": g["codigo_barras"]}).json()
                    assert r["ok"], r
                else:
                    assert ana.post(f"/ecommerce/api/{cid}/la-tengo", json={"item_ids": g["item_ids"]}).json()["ok"]
                juntadas += 1
            if juntadas >= 1:
                break
        est = ana.get(f"/ecommerce/api/{cid}/mia").json()["box"]
        assert est["en_mano"] == juntadas
        if not est["habilitado"]:
            assert ana.post(f"/ecommerce/api/{cid}/box/llevar", json={}).status_code == 400
        r = ana.post(f"/ecommerce/api/{cid}/box/llevar", json={"forzar": True}).json()
        assert r["ok"] and r["boxes"][0]["numero"] == 1 and r["boxes"][0]["nuevo"]
        assert sum(b["unidades"] for b in r["boxes"]) == juntadas and r["box"]["en_mano"] == 0
        venta = r["boxes"][0]["ventas"][0]

        # lo que está en un BOX no se resta
        g = next(x for x in ana.get(f"/ecommerce/api/{cid}/mia").json()["grupos"] if x["en_box"])
        assert g["boxes"] == [1]
        assert "BOX 1" in ana.post(f"/ecommerce/api/{cid}/deshacer", json={"item_ids": g["item_ids"]}).json()["detail"]

        # lista por BOX
        bs = beto.get(f"/ecommerce/api/{cid}/boxes").json()
        assert bs["cantidad"] == 2 and bs["boxes"][0]["numero"] == 1 and bs["boxes"][0]["activo"]
        assert bs["boxes"][0]["quien"] == "Ana"

        # mesa de control: al escanear la etiqueta dice en qué BOX está
        p = ana.post(f"/control/api/{cid}/escanear", json={"codigo": venta["tracking"]}).json()
        assert p["tipo"] == "paquete" and p["paquete"]["boxes"] == [1]

        # Beto junta otra venta: el BOX 1 sigue ocupado → le toca el 2
        db_ = beto.get(f"/ecommerce/api/{cid}/mia").json()
        g = next(x for x in db_["grupos"] if x["faltan"])
        if g["vinculado"] and g["codigo_barras"]:
            assert beto.post(f"/ecommerce/api/{cid}/encontre", json={"codigo": g["codigo_barras"]}).json()["ok"]
        else:
            assert beto.post(f"/ecommerce/api/{cid}/la-tengo", json={"item_ids": g["item_ids"]}).json()["ok"]
        r = beto.post(f"/ecommerce/api/{cid}/box/llevar", json={"forzar": True}).json()
        assert r["boxes"][0]["numero"] in (1, 2)   # 1 solo si es la misma venta que ya estaba en el BOX 1


def test_box_no_parte_ventas_y_libera_numero():
    """Servicio: de a N prendas por BOX, una venta entera en el mismo BOX y el número se reusa al controlarla."""
    from app.db import SessionLocal
    from app.models import Control, ControlItem, ControlPaquete, Usuario
    from app.services import ajustes
    from app.services import ecommerce as svc
    from sqlalchemy import select
    db = SessionLocal()
    try:
        u = db.scalar(select(Usuario).where(Usuario.nombre == "Vengil"))
        c = Control(fecha=svc.hoy(), usuario_id=u.id, modo=svc.MODO, nombre="test box")
        db.add(c)
        for k, cant in enumerate((1, 1, 3, 1)):   # venta de 3 prendas en el medio
            p = ControlPaquete(tracking=f"T{k}", tipo="flex")
            for j in range(cant):
                p.items.append(ControlItem(sku=f"S{k}{j}", cantidad=1, orden_ruta=k * 10 + j))
            c.paquetes.append(p)
        db.flush()
        for i in (i for p in c.paquetes for i in p.items):
            i.asignado_id = i.buscado_por_id = u.id
            i.buscado = i.cantidad
        db.commit()
        ajustes.guardar(db, "re_box_cant", "3")
        assert svc.estado_box(db, c, u.id)["habilitado"]
        bs = svc.llevar_a_box(db, c, u)
        # T0+T1 (2) en BOX 1; la venta de 3 no entra → BOX 2; T3 → BOX 3
        assert [(b["numero"], b["unidades"]) for b in bs] == [(1, 2), (2, 3), (3, 1)]
        assert [v["tracking"] for v in bs[1]["ventas"]] == ["T2"]
        # se controla todo el BOX 1 → el número 1 queda libre
        for p in c.paquetes[:2]:
            p.estado = "completo"
        db.commit()
        assert svc._numero_libre(c) == 1
        assert [b["numero"] for b in svc.boxes(c)][:2] == [2, 3]
    finally:
        ajustes.guardar(db, "re_box_cant", "5")
        db.close()
