"""Ventas por API para Recolección Ecommerce: TN "Por empaquetar" + ML Flex/Colecta separadas en
"Etiquetas por imprimir" (ready_to_print) y "Listas para despachar" (etiqueta ya impresa)."""
import re

from fastapi.testclient import TestClient

from app.db import hoy
from app.main import app
from app.services import meli, ventas_api
from app.services import tiendanube as tn
from tests.test_tiendanube import falso, _login

H = hoy().isoformat()
MANANA = "2099-01-01"


def _orden(oid, sid, sku, titulo, talle="M", pack=None, tags=("paid", "not_delivered")):
    return {"id": oid, "pack_id": pack, "tags": list(tags), "buyer": {"nickname": f"COMP{oid}"}, "shipping": {"id": sid},
            "order_items": [{"quantity": 1, "item": {"id": "MLA1", "title": titulo, "seller_sku": sku,
                                                     "variation_attributes": [{"name": "Color", "value_name": "Negro"},
                                                                              {"name": "Talle", "value_name": talle}]}}]}


ORD = [_orden(1, 101, "M-114-BLACK-XL", "Campera Thor", "XL"),            # flex por imprimir
       _orden(2, 102, "W-2101-BLK-M", "Puffer"),                          # flex lista (impresa)
       _orden(3, 103, "M-1-BLK-S", "Colecta A"),                          # colecta impresa
       _orden(4, 103, "M-2-BLK-S", "Colecta B", pack=999),                # mismo envío (carrito)
       _orden(5, 104, "X", "Cancelada"), _orden(6, 105, "X", "Full"), _orden(7, 106, "X", "Ya salió"),
       _orden(8, 107, "X", "Entregada", tags=("delivered",)), _orden(9, 108, "M-9-BLK-L", "Mañana")]
SH = {"101": {"status": "ready_to_ship", "substatus": "ready_to_print", "logistic_type": "self_service",
              "shipping_option": {"estimated_handling_limit": {"date": H + "T21:00:00.000-03:00"}},
              "receiver_address": {"receiver_name": "Juan Pérez"}},
      "102": {"status": "ready_to_ship", "substatus": "printed", "logistic_type": "self_service"},
      "103": {"status": "ready_to_ship", "substatus": "printed", "logistic_type": "cross_docking",
              "shipping_option": {"estimated_handling_limit": {"date": H + "T14:00:00.000-03:00"}}},
      "104": {"status": "cancelled", "logistic_type": "self_service"},
      "105": {"status": "ready_to_ship", "substatus": "ready_to_print", "logistic_type": "fulfillment"},
      "106": {"status": "ready_to_ship", "substatus": "picked_up", "logistic_type": "cross_docking"},
      "108": {"status": "ready_to_ship", "substatus": "ready_to_print", "logistic_type": "cross_docking",
              "shipping_option": {"estimated_handling_limit": {"date": MANANA + "T14:00:00.000-03:00"}}},
      "109": {"status": "cancelled", "logistic_type": "cross_docking", "status_history": {"date_ready_to_ship": H + "T09:00:00"}},
      "110": {"status": "cancelled", "logistic_type": "self_service", "status_history": {"date_cancelled": H + "T09:00:00"}}}


def ml_falso(llamadas):
    def get(path, api_version=None):
        llamadas.append(path)
        if path.startswith("/orders/search") and "order.status=cancelled" in path:
            return {"results": [_orden(20, 109, "M-5-BLK-M", "Cancelada con etiqueta"), _orden(21, 110, "X", "Cancelada antes")]}
        if path.startswith("/orders/search"):
            assert "seller=413" in path and "order.status=paid" in path
            return {"results": ORD, "paging": {"total": len(ORD)}}
        m = re.match(r"/shipments/(\d+)$", path)
        if m:
            assert m.group(1) != "107"  # entregada: ni se consulta
            return SH[m.group(1)]
        raise AssertionError(path)
    return get


def _mocks(monkeypatch, llamadas):
    ventas_api._finales.clear()
    ventas_api._canceladas_info.clear()
    ventas_api.olvidar()
    monkeypatch.setattr(meli, "configurado", lambda: True)
    monkeypatch.setattr(meli, "seller_id", lambda: 413)
    monkeypatch.setattr(meli, "get", ml_falso(llamadas))
    monkeypatch.setattr(tn, "_pedir", falso([]))
    monkeypatch.setattr(tn, "configurado", lambda *a: True)


def test_ml_separa_por_imprimir_y_listas(monkeypatch):
    llamadas = []
    _mocks(monkeypatch, llamadas)
    ps = ventas_api.ml_pendientes()
    por = {p["tracking"]: p for p in ps}
    assert set(por) == {"101", "102", "103", "108"}
    assert por["101"]["etiqueta"] == "por_imprimir" and por["101"]["tipo"] == "flex" and por["101"]["comprador"] == "Juan Pérez"
    assert por["101"]["items"][0]["talle"] == "XL" and por["101"]["items"][0]["color"] == "Negro"
    assert por["102"]["etiqueta"] == "lista"
    assert por["103"]["tipo"] == "colecta" and len(por["103"]["items"]) == 2 and por["103"]["venta_id"] == "999"
    assert por["108"]["hoy"] is False and por["101"]["hoy"] is True
    # segunda vuelta: los que ya no vuelven (cancelado, Full, ya salió) no se consultan de nuevo
    n = len(llamadas)
    ventas_api.ml_pendientes()
    assert not any(p.endswith(("/104", "/105", "/106")) for p in llamadas[n:])


def test_pantalla_y_crear_busqueda(monkeypatch):
    llamadas = []
    _mocks(monkeypatch, llamadas)
    with TestClient(app) as c:
        _login(c)
        assert "Ventas (ML + Tienda Nube)" in c.get("/ecommerce").text
        d = c.get("/ecommerce/api/ventas").json()
        assert [v["clave"] for v in d["tn"]["ventas"]] == ["tn:7001"]   # 7004 ya empaquetada: no entra
        ml = {v["clave"]: v for v in d["ml"]["ventas"]}
        assert ml["ml:101"]["etiqueta"] == "por_imprimir" and ml["ml:102"]["etiqueta"] == "lista"
        assert [v["clave"] for v in d["ml"]["canceladas"]] == ["ml:109"]   # la 110 nunca estuvo lista
        r = c.post("/ecommerce/api/crear/ventas", json={"claves": ["tn:7001", "ml:101", "ml:103"]}).json()
        assert r["ok"] and r["busqueda"]["paquetes"] == 3
        cid = r["busqueda"]["id"]
        det = c.get(f"/control/api/{cid}").json() if c.get(f"/control/api/{cid}").status_code == 200 else None
        # la etiqueta de ML se imprimió después → al sumar ventas nuevas se actualiza y entra la 102
        SH["101"]["substatus"] = "printed"
        try:
            r = c.post(f"/ecommerce/api/{cid}/agregar-ventas").json()
        finally:
            SH["101"]["substatus"] = "ready_to_print"
        assert r["nuevos"] == 1  # la 102 (la 108 es de mañana)
        from app.db import SessionLocal
        from app.models import ControlPaquete
        from sqlalchemy import select
        db = SessionLocal()
        try:
            et = dict(db.execute(select(ControlPaquete.tracking, ControlPaquete.etiqueta)
                                 .where(ControlPaquete.control_id == cid)).all())
        finally:
            db.close()
        assert et == {"7001": "", "101": "lista", "103": "lista", "102": "lista"}


def test_seller_id_sin_users_me(monkeypatch):
    """El proxy de Costos puede devolver /users/me sin id: se saca de las preguntas recibidas."""
    meli.olvidar_cache()

    def get(path, api_version=None):
        if path == "/users/me":
            return {}
        if path.startswith("/my/received_questions/search"):
            return {"questions": [{"id": 1, "seller_id": 777}]}
        raise AssertionError(path)
    monkeypatch.setattr(meli, "get", get)
    assert meli.seller_id() == 777
    meli.olvidar_cache()
