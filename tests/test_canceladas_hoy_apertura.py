"""Canceladas = solo las de HOY con etiqueta impresa (como el panel de ML "Canceladas. No despachar"), sin avisos
sueltos; y la búsqueda del día se abre sola de lunes a viernes hábiles a las 8:00."""
import re
from datetime import datetime, timedelta

from sqlalchemy import select

from app.db import SessionLocal, hoy
from app.models import Control, Novedad
from app.services import meli, ventas_api, ventas_auto
from app.services import tiendanube as tn
from tests.test_tiendanube import falso

H = hoy().isoformat()
AYER = (hoy() - timedelta(days=1)).isoformat()


def _o(oid, sid):
    return {"id": oid, "shipping": {"id": sid}, "buyer": {"nickname": f"C{oid}"},
            "order_items": [{"quantity": 1, "item": {"title": f"Campera {oid}", "seller_sku": "X"}}]}


SH = {
    "1": {"logistic_type": "cross_docking", "date_first_printed": H + "T09:00:00.000-03:00",
          "status_history": {"date_ready_to_ship": H + "T08:00:00", "date_cancelled": H + "T11:00:00.000-03:00"}},
    "2": {"logistic_type": "cross_docking", "date_first_printed": AYER + "T09:00:00.000-03:00",      # cancelada ayer
          "status_history": {"date_ready_to_ship": AYER + "T08:00:00", "date_cancelled": AYER + "T18:00:00.000-03:00"}},
    "3": {"logistic_type": "self_service", "date_first_printed": None,                               # sin imprimir
          "status_history": {"date_ready_to_ship": H + "T08:00:00", "date_cancelled": H + "T10:00:00.000-03:00"}},
    "4": {"logistic_type": "cross_docking", "date_first_printed": H + "T09:00:00.000-03:00",          # salía mañana
          "shipping_option": {"estimated_handling_limit": {"date": "2099-01-01T14:00:00.000-03:00"}},
          "status_history": {"date_ready_to_ship": H + "T08:00:00", "date_cancelled": H + "T10:00:00.000-03:00"}},
}


def _mocks(monkeypatch, con_canceladas=True):
    ventas_api._finales.clear()
    ventas_api._canceladas_info.clear()
    ventas_api._estado.clear()
    ventas_api.olvidar()
    ventas_auto._ultima[0] = None

    def get(path, api_version=None):
        if "order.status=cancelled" in path:
            return {"results": [_o(10 + int(k), k) for k in SH] if con_canceladas else []}
        if path.startswith("/orders/search"):
            return {"results": [], "paging": {"total": 0}}
        m = re.match(r"/shipments/(\d+)$", path)
        if m:
            return SH[m.group(1)]
        raise AssertionError(path)
    monkeypatch.setattr(meli, "configurado", lambda: True)
    monkeypatch.setattr(meli, "seller_id", lambda: 413)
    monkeypatch.setattr(meli, "get", get)
    monkeypatch.setattr(tn, "_pedir", falso([]))
    monkeypatch.setattr(tn, "configurado", lambda *a: True)


def test_solo_canceladas_de_hoy_con_etiqueta(monkeypatch):
    _mocks(monkeypatch)
    assert [p["tracking"] for p in ventas_api.ml_canceladas()] == ["1"]


def test_canceladas_fuera_de_la_busqueda_no_avisan(monkeypatch):
    _mocks(monkeypatch)
    monkeypatch.setattr(ventas_auto, "puede_abrir", lambda db=None: True)
    db = SessionLocal()
    try:
        db.add(Novedad(tipo="cancelada", texto="vieja", clave="cancel:colecta:999"))
        db.commit()
        assert ventas_auto.limpiar_avisos_cancelada_sueltos(db) == 1
        ventas_auto.sincronizar(db)
        assert not db.scalars(select(Novedad).where(Novedad.tipo == "cancelada")).all()
    finally:
        db.close()


def test_dia_habil_y_hora():
    assert ventas_auto.dia_habil(datetime(2026, 10, 7).date())          # miércoles
    assert not ventas_auto.dia_habil(datetime(2026, 10, 10).date())     # sábado
    assert not ventas_auto.dia_habil(datetime(2026, 10, 12).date())     # feriado


def test_puede_abrir(monkeypatch):
    monkeypatch.setattr(ventas_auto, "ahora", lambda: datetime(2026, 10, 7, 7, 59))
    assert not ventas_auto.puede_abrir()
    monkeypatch.setattr(ventas_auto, "ahora", lambda: datetime(2026, 10, 7, 8, 0))
    assert ventas_auto.puede_abrir()
    monkeypatch.setattr(ventas_auto, "ahora", lambda: datetime(2026, 10, 11, 9, 0))   # domingo
    assert not ventas_auto.puede_abrir()


def test_se_abre_sola_a_las_8_aunque_no_haya_ventas(monkeypatch):
    _mocks(monkeypatch, con_canceladas=False)
    db = SessionLocal()
    try:
        for c in db.scalars(select(Control)).all():
            c.estado = "cerrado"
        db.commit()
        monkeypatch.setattr(ventas_auto, "puede_abrir", lambda db=None: False)
        assert ventas_auto.toca(db)            # primera vuelta
        ventas_auto.sincronizar(db)
        assert ventas_auto.busqueda_del_dia(db) is None          # antes de las 8 / no hábil: no se abre
        monkeypatch.setattr(ventas_auto, "puede_abrir", lambda db=None: True)
        assert ventas_auto.toca(db)            # a las 8 no espera la vuelta
        r = ventas_auto.sincronizar(db)
        c = ventas_auto.busqueda_del_dia(db)
        assert c is not None and r["busqueda"] == c.id
        assert not ventas_auto.toca(db)
    finally:
        db.close()
