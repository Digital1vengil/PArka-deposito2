"""SKU de ML / TN que no sigue el formato de Odoo: WORKJ-BLACK-L ↔ 1-CAMP-M-WORKJ-BLCK-L-PARK."""
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.services.catalogo import por_sku_plataforma


def test_flexible_modelo_color_talle():
    with SessionLocal() as db:
        a = por_sku_plataforma(db, "WORKJ-BLACK-L", consultar_odoo=False)
        assert a is not None and a.sku == "1-CAMP-M-WORKJ-BLCK-L-PARK"      # Parka antes que No Brand / Briganti
        assert por_sku_plataforma(db, "workj-negro-xl", consultar_odoo=False).sku == "1-CAMP-M-WORKJ-BLCK-XL-PARK"
        assert por_sku_plataforma(db, "WORKJ BLACK M", consultar_odoo=False).sku == "1-CAMP-M-WORKJ-BLCK-M-PARK"
        assert por_sku_plataforma(db, "WORKJ-BLACK", consultar_odoo=False) is None     # sin talle: no se adivina
        assert por_sku_plataforma(db, "WORKJ-PINK-L", consultar_odoo=False) is None    # color que no existe
        assert por_sku_plataforma(db, "WORKJ-BLACK-L", consultar_odoo=False).sku == "1-CAMP-M-WORKJ-BLCK-L-PARK"
        # ejemplo real (TN #1262): WORKJAC2-BLK-XXL ↔ 1-CAMP-M-WORKJ-BLCK-XXL-PARK
        assert por_sku_plataforma(db, "WORKJAC2-BLK-XXL", consultar_odoo=False).sku == "1-CAMP-M-WORKJ-BLCK-XXL-PARK"
        # lo de antes sigue igual
        assert por_sku_plataforma(db, "W-2003-BLCK-M", consultar_odoo=False).sku == "1-CAMP-W-02003-BLCK-M-PARK"


def test_venta_con_sku_flexible(monkeypatch):
    from app.services import tiendanube as tn
    from tests.test_devoluciones import _login
    with TestClient(app) as c:
        _login(c)
        orden = {"id": 7, "number": 1262, "status": "closed", "products": [
            {"name": "Campera Origin (Negro, L)", "sku": "WORKJ-BLACK-L", "quantity": 1}]}
        monkeypatch.setattr(tn, "configurado", lambda db=None: True)
        monkeypatch.setattr(tn, "get", lambda path, **p: [orden])
        it = c.get("/devoluciones/api/venta", params={"plataforma": "TN", "c": "1262"}).json()["items"][0]
        assert it["variante"]["sku"] == "1-CAMP-M-WORKJ-BLCK-L-PARK" and it["motivo"] == ""
