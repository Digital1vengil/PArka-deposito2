"""Búsqueda automática: las ventas nuevas entran solas a la búsqueda del día con aviso; si una venta se cancela
sale de la búsqueda, la mesa la bloquea y hay aviso. Operarios: ✓ La tengo (a mano) o escanear."""
import re

from fastapi.testclient import TestClient

from app.main import app
from app.services import meli, ventas_api, ventas_auto
from app.services import tiendanube as tn
from tests import test_ventas_api as T
from tests.test_tiendanube import falso, _login, ORDENES


def _mocks(monkeypatch):
    T._mocks(monkeypatch, [])
    ventas_api._estado.clear()
    ventas_api._canceladas_info.clear()
    ventas_auto._ultima[0] = None
    base_ml = meli.get

    def ml(path, api_version=None):
        if "order.status=cancelled" in path:
            return {"results": []}
        return base_ml(path, api_version)
    monkeypatch.setattr(meli, "get", ml)


def test_auto_crea_suma_y_cancela(monkeypatch):
    _mocks(monkeypatch)
    from app.db import SessionLocal
    from app.models import Control, ControlPaquete, Novedad
    from sqlalchemy import select
    with TestClient(app) as c:
        _login(c)
        assert c.get("/ecommerce/api/novedades?desde=-1").json()["ultimo"] == 0
        r = c.post("/ecommerce/api/auto/ahora").json()
        assert r["ok"] and r["nuevas"] == 4    # TN 7001 + ML 101, 102, 103 (la 108 es de mañana)
        cid = r["busqueda_id"]
        nov = c.get("/ecommerce/api/novedades").json()["novedades"]
        assert "Se abrió la búsqueda del día" in nov[0]["texto"] and "4 venta(s) en total" in nov[0]["texto"]
        # segunda vuelta sin cambios: nada nuevo
        assert c.post("/ecommerce/api/auto/ahora").json()["nuevas"] == 0
        # entra una venta nueva de TN y se cancela la 102 de ML
        ORDENES.append({"id": 1600, "number": 7100, "status": "open", "payment_status": "paid", "shipping_status": "unpacked",
                        "contact_name": "Nueva", "products": [{"name": "Campera (Azul, L)", "sku": "S1", "quantity": 1}]})
        T.SH["102"] = {"status": "cancelled", "logistic_type": "self_service"}
        try:
            ventas_api._estado.clear()
            r = c.post("/ecommerce/api/auto/ahora").json()
        finally:
            ORDENES.pop()
            T.SH["102"] = {"status": "ready_to_ship", "substatus": "printed", "logistic_type": "self_service"}
        assert r["nuevas"] == 1 and r["canceladas"] == 1 and r["busqueda_id"] == cid
        textos = [n["texto"] for n in c.get("/ecommerce/api/novedades").json()["novedades"]]
        assert any("Entró una venta: Tienda Nube #7100" in t for t in textos)
        assert any("CANCELADA" in t and "102" in t for t in textos)
        db = SessionLocal()
        try:
            p = db.scalar(select(ControlPaquete).where(ControlPaquete.control_id == cid, ControlPaquete.tracking == "102"))
            assert p.cancelado
            assert db.scalar(select(Control).where(Control.id == cid)).origen == "auto"
        finally:
            db.close()
        # la mesa de control bloquea la cancelada
        r = c.post(f"/control/api/{cid}/escanear", json={"codigo": "102"})
        assert r.status_code == 200 and r.json()["motivo"] == "cancelado", r.text
        # sus prendas ya no están en la búsqueda
        todo = c.get(f"/ecommerce/api/{cid}/todo").json()["grupos"]
        assert not any("102" in " ".join(g["paquetes"]) for g in todo)


def test_la_tengo_para_operarios(monkeypatch):
    _mocks(monkeypatch)
    with TestClient(app) as adm:
        _login(adm)
        cid = adm.post("/ecommerce/api/auto/ahora").json()["busqueda_id"] or ventas_auto.busqueda_del_dia.__name__
        assert adm.post(f"/ecommerce/api/{cid}/sumarme").json()["ok"]
        mia = adm.get(f"/ecommerce/api/{cid}/mia").json()["grupos"]
        g = next(x for x in mia if x["faltan"])
        r = adm.post(f"/ecommerce/api/{cid}/la-tengo", json={"item_ids": g["item_ids"]}).json()
        assert r["ok"]
        r = adm.post(f"/ecommerce/api/{cid}/faltante", json={"item_ids": [x["item_ids"][0] for x in mia if x["faltan"]][1:2], "nota": "no hay"}).json()
        assert r["ok"]


def test_apagada_no_hace_nada(monkeypatch):
    _mocks(monkeypatch)
    with TestClient(app) as c:
        _login(c)
        assert c.post("/ecommerce/api/auto", json={"activo": False}).json()["activo"] is False
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            assert ventas_auto.toca(db) is False
            assert ventas_auto.sincronizar(db)["motivo"] == "apagado"
        finally:
            db.close()
        c.post("/ecommerce/api/auto", json={"activo": True})


def test_minutos_y_no_la_encontre_avisa_sin_stock(monkeypatch):
    _mocks(monkeypatch)
    from app.db import SessionLocal
    from app.models import Articulo, ControlItem
    from app.services import catalogo
    from sqlalchemy import select
    monkeypatch.setattr(catalogo, "stock_en_vivo", lambda db, arts: {a.id: 0.0 for a in arts})
    with TestClient(app) as c:
        _login(c)
        assert c.post("/ecommerce/api/auto", json={"minutos": 10}).json()["minutos"] == 10
        assert c.post("/ecommerce/api/auto", json={"minutos": 7}).status_code == 400
        c.post("/ecommerce/api/auto", json={"minutos": 3})
        cid = c.post("/ecommerce/api/auto/ahora").json()["busqueda_id"]
        todo = c.get(f"/ecommerce/api/{cid}/todo").json()["grupos"]
        g = next((x for x in todo if x["vinculado"] and x["faltan"]), None) or next(x for x in todo if x["faltan"])
        r = c.post(f"/ecommerce/api/{cid}/faltante", json={"item_ids": g["item_ids"], "nota": ""}).json()
        assert r["ok"] and r["aviso"]
        ns = c.get("/ecommerce/api/novedades").json()["novedades"]
        n = next(x for x in ns if x["tipo"] == "sin_stock")
        assert n["para"] == "supervision"
        assert ("SIN STOCK" in n["texto"]) == g["vinculado"]


def test_supervision_ve_avisos_que_el_operario_no(monkeypatch):
    _mocks(monkeypatch)
    from app.db import SessionLocal
    from app.services import ventas_auto as va
    db = SessionLocal()
    try:
        va.avisar(db, "sin_stock", "solo jefes", para="supervision")
        va.avisar(db, "nueva", "para todos")
        db.commit()
        textos_op = [n["texto"] for n in va.novedades(db, supervisa=False)]
        textos_sup = [n["texto"] for n in va.novedades(db, supervisa=True)]
    finally:
        db.close()
    assert "para todos" in textos_op and "solo jefes" not in textos_op
    assert "solo jefes" in textos_sup


def test_busqueda_individual_por_dia_sin_arrastre(monkeypatch):
    """Lo pendiente de ayer NO pasa a la de hoy: la de ayer se cierra como está y la venta no se vuelve a sumar
    aunque ML la siga mostrando lista para despachar."""
    _mocks(monkeypatch)
    from datetime import timedelta
    from app.db import SessionLocal, hoy
    from app.models import Control
    from app.services import ventas_auto as va
    from sqlalchemy import select
    db = SessionLocal()
    try:
        for x in db.scalars(select(Control)).all():
            x.estado = "cerrado"
        vieja = Control(fecha=hoy() - timedelta(days=1), origen="auto", modo="ecommerce", nombre="Ventas ayer", tipo="mixto")
        db.add(vieja)
        db.flush()
        from app.services import control as csvc
        csvc.agregar_paquetes(db, vieja, [{"tracking": "555", "tipo": "flex", "items": [{"sku": "X", "producto": "Vieja", "cantidad": 1}]}])
        db.commit()
        vid = vieja.id
        T.SH["555"] = {"status": "ready_to_ship", "substatus": "printed", "logistic_type": "self_service"}
        try:
            va.sincronizar(db, forzar=True)
        finally:
            T.SH.pop("555", None)
        hoyc = va.busqueda_del_dia(db)
        trks = {p.tracking for p in hoyc.paquetes}
        assert "101" in trks and "555" not in trks
        v = db.get(Control, vid)
        assert v.estado == "cerrado" and {p.tracking for p in v.paquetes} == {"555"}
    finally:
        db.close()


def test_atrasada_de_otro_dia_no_entra():
    assert not ventas_auto._es_del_dia({"tracking": "1", "hoy": True, "atrasada": True}, set())
    assert ventas_auto._es_del_dia({"tracking": "1", "hoy": True, "atrasada": False}, set())
    assert not ventas_auto._es_del_dia({"tracking": "1", "venta_id": "9", "hoy": True}, {"9"})
    assert not ventas_auto._es_del_dia({"tracking": "1", "hoy": False}, set())


def test_reparar_arrastre_devuelve_lo_de_ayer(monkeypatch):
    _mocks(monkeypatch)
    from datetime import timedelta
    from app.db import SessionLocal, hoy
    from app.models import Control, ControlPaquete, Novedad
    from app.services import ajustes
    from sqlalchemy import select
    db = SessionLocal()
    try:
        for x in db.scalars(select(Control)).all():
            x.estado = "cerrado"
        ajustes.guardar(db, "re_fix_arrastre", "")
        ayer = Control(fecha=hoy() - timedelta(days=1), origen="auto", modo="ecommerce", nombre="ayer", tipo="mixto", estado="cerrado")
        c = Control(fecha=hoy(), origen="auto", modo="ecommerce", nombre="hoy", tipo="mixto")
        db.add_all([ayer, c])
        db.flush()
        for t in ("A1", "A2"):
            c.paquetes.append(ControlPaquete(tracking=t, tipo="flex"))
        db.flush()
        c.paquetes.append(ControlPaquete(tracking="900", tipo="flex"))
        db.add(Novedad(tipo="info", texto="Pasaron 2", control_id=c.id, clave=f"arrastre:x:{c.id}"))
        db.add(Novedad(tipo="nueva", texto="Entró", control_id=c.id, clave="nueva:ml:900"))
        db.commit()
        assert ventas_auto.reparar_arrastre(db) == 2
        db.commit()
        assert {p.tracking for p in c.paquetes} == {"900"}
        assert {p.tracking for p in ayer.paquetes} == {"A1", "A2"}
        assert ventas_auto.reparar_arrastre(db) == 0      # una sola vez
    finally:
        db.close()


def test_ventas_de_otra_busqueda_abierta_pasan_a_la_del_dia(monkeypatch):
    """La búsqueda del día tiene TODAS las ventas de hoy: las que estaban en una búsqueda armada a mano pasan
    (con lo ya buscado) y el aviso de apertura muestra siempre el total."""
    _mocks(monkeypatch)
    from app.db import SessionLocal, hoy
    from app.models import Control, ControlPaquete, Novedad
    from app.services import control as csvc
    from sqlalchemy import select
    db = SessionLocal()
    try:
        for cc in db.scalars(select(Control)).all():
            cc.estado = "cerrado"
        manual = Control(fecha=hoy(), origen="archivo", modo="ecommerce", nombre="A mano", tipo="mixto")
        db.add(manual)
        db.flush()
        csvc.agregar_paquetes(db, manual, [{"tracking": "103", "items": [{"sku": "M-1-BLK-S", "cantidad": 1}]},
                                           {"tracking": "999", "items": [{"sku": "ZZ", "cantidad": 1}]}])
        db.commit()
        mid = manual.id
        r = ventas_auto.sincronizar(db, forzar=True)
        c = ventas_auto.busqueda_del_dia(db)
        trks = {p.tracking for p in c.paquetes}
        assert {"7001", "101", "102", "103"} <= trks and "999" not in trks
        assert r["juntadas"] == 1 and r["nuevas"] == 3
        assert db.get(Control, mid).estado == "abierto"          # le queda la 999
        n = db.scalar(select(Novedad).where(Novedad.control_id == c.id).order_by(Novedad.id))
        assert "4 venta(s) en total" in n.texto
        # entra otra venta más tarde → el aviso de apertura se actualiza con el total
        c.paquetes.append(ControlPaquete(tracking="555", tipo="flex"))
        db.commit()
        ventas_auto.sincronizar(db, forzar=True)
        db.refresh(n)
        assert "5 venta(s) en total" in n.texto
    finally:
        db.close()


def test_recolectores_entran_solos_a_cada_busqueda_nueva(monkeypatch):
    """Rol "Buscador / Recolector": cada búsqueda nueva (automática o a mano) los suma sola."""
    _mocks(monkeypatch)
    from app.auth import hash_pin
    from app.db import SessionLocal
    from app.models import Control, ROLES, Seccion, Usuario
    from app.services import ecommerce as svc
    from sqlalchemy import select
    assert ROLES["recolector"] == "Buscador / Recolector"
    db = SessionLocal()
    try:
        assert "recolector" in db.scalar(select(Seccion).where(Seccion.slug == "ecommerce")).roles.split(",")
        for cc in db.scalars(select(Control)).all():
            cc.estado = "cerrado"
        r = db.scalar(select(Usuario).where(Usuario.nombre == "Reco Test"))
        if not r:
            r = Usuario(nombre="Reco Test", rol="recolector", pin_hash=hash_pin("4321"), activo=True)
            db.add(r)
        baja = db.scalar(select(Usuario).where(Usuario.nombre == "Reco Baja"))
        if not baja:
            baja = Usuario(nombre="Reco Baja", rol="recolector", pin_hash=hash_pin("4322"), activo=False)
            db.add(baja)
        db.commit()
        ventas_auto.sincronizar(db, forzar=True)
        c = ventas_auto.busqueda_del_dia(db)
        ids = {p.usuario_id for p in svc.activos(c)}
        assert r.id in ids and baja.id not in ids
        # tienen prendas asignadas apenas entran
        assert any(i.asignado_id == r.id for p in c.paquetes for i in p.items)
        # búsqueda a mano: también entran
        admin = db.scalar(select(Usuario).where(Usuario.rol == "admin"))
        m, _ = svc.crear(db, admin, [], "A mano", "api",
                         extra=[{"tracking": "8888", "tipo": "flex", "items": [{"sku": "X", "producto": "P", "cantidad": 1}]}])
        assert r.id in {p.usuario_id for p in svc.activos(m)}
    finally:
        db.close()


def test_venta_de_ayer_que_ml_pide_hoy_pasa_a_la_busqueda_de_hoy(monkeypatch):
    """Flex vendida ayer después del corte quedó en la búsqueda de ayer (cerrada) sin terminar: como ML la pide
    hoy, pasa a la de hoy con su etiqueta actualizada. Lo atrasado (ML lo pedía ayer) no se trae."""
    _mocks(monkeypatch)
    from datetime import timedelta
    from app.db import SessionLocal, hoy
    from app.models import Control, ControlPaquete
    from app.services import ventas_api
    from sqlalchemy import select
    db = SessionLocal()
    try:
        for cc in db.scalars(select(Control)).all():
            cc.estado = "cerrado"
        ayer = Control(fecha=hoy() - timedelta(days=1), origen="auto", modo="ecommerce", nombre="ayer",
                       tipo="mixto", estado="cerrado")
        db.add(ayer)
        db.flush()
        ayer.paquetes.append(ControlPaquete(tracking="482", tipo="flex", etiqueta="por_imprimir"))
        ayer.paquetes.append(ControlPaquete(tracking="483", tipo="flex", etiqueta="lista"))
        db.commit()
        H = hoy().isoformat()
        extra = [dict(clave="ml:482", tracking="482", tipo="flex", etiqueta="lista", hoy=True, atrasada=False, dia=H,
                      items=[{"sku": "W-2079-BLK-S", "cantidad": 1}]),
                 dict(clave="ml:483", tracking="483", tipo="flex", etiqueta="lista", hoy=True, atrasada=True,
                      dia=(hoy() - timedelta(days=1)).isoformat(), items=[{"sku": "X", "cantidad": 1}])]
        orig = ventas_api.todas

        def todas(refrescar=False):
            d = orig(refrescar)
            d["ml"]["paquetes"] = list(d["ml"]["paquetes"]) + extra
            return d
        monkeypatch.setattr(ventas_api, "todas", todas)
        ventas_auto.sincronizar(db, forzar=True)
        c = ventas_auto.busqueda_del_dia(db)
        por = {p.tracking: p for p in c.paquetes}
        assert "482" in por and por["482"].etiqueta == "lista"
        assert "483" not in por
        assert {p.tracking for p in ayer.paquetes} == {"483"}
    finally:
        db.close()


def test_dia_de_despacho_en_hora_argentina():
    from app.services import ventas_api
    from app.db import hoy
    from datetime import timedelta
    H, M = hoy().isoformat(), (hoy() + timedelta(days=1)).isoformat()
    # ML manda -04:00: 23:30 de hoy en -04 es 00:30 de mañana en Buenos Aires
    sh = {"shipping_option": {"estimated_handling_limit": {"date": H + "T23:30:00.000-04:00"}}}
    assert ventas_api._dia_despacho(sh, "flex").isoformat() == M
    # Flex sin fecha límite: se usa la fecha de entrega
    sh = {"shipping_option": {"estimated_delivery_time": {"date": M + "T00:00:00.000-03:00"}}}
    assert ventas_api._dia_despacho(sh, "flex").isoformat() == M
    assert ventas_api._dia_despacho(sh, "colecta") is None
