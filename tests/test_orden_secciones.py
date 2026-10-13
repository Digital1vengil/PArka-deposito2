"""Secciones: el admin las ordena libremente y el orden no se pisa al reiniciar."""
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.models import Seccion
from app.seed import sembrar
from tests.test_flujo import login


def _orden():
    with SessionLocal() as db:
        return [s.slug for s in db.scalars(select(Seccion).order_by(Seccion.orden, Seccion.id))]


def test_ordenar_secciones_y_no_se_resetea():
    with TestClient(app) as c:
        login(c, "Vengil", "1234")
        antes = _orden()
        with SessionLocal() as db:
            ids = {s.slug: s.id for s in db.scalars(select(Seccion))}
        nuevo = ["conteo", "despacho"]  # el resto queda después, en su orden
        r = c.post("/admin/api/secciones/orden", json={"ids": [ids[x] for x in nuevo]},
                   headers={"x-requested-with": "fetch"})
        assert r.status_code == 200, r.text
        esperado = nuevo + [x for x in antes if x not in nuevo]
        assert _orden() == esperado
        assert "orden-lista" in c.get("/admin/secciones").text
    with SessionLocal() as db:   # reinicio: sembrar() ya no reordena
        sembrar(db)
    assert _orden() == esperado
