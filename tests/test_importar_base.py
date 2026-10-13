"""Admin → Importar base: copia un archivo SQLite del ejecutable a la base actual."""
import sqlite3
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.db import Base
from app.main import app
from tests.test_flujo import login


def _base_vieja() -> bytes:
    """Base del ejecutable con datos y una referencia rota (id 0), como pasa en SQLite."""
    d = Path(tempfile.mkdtemp()) / "vieja.db"
    eng = create_engine(f"sqlite:///{d}")
    Base.metadata.create_all(eng)
    eng.dispose()
    con = sqlite3.connect(d)
    from app.auth import hash_pin
    con.execute("INSERT INTO usuarios (id, nombre, rol, pin_hash, activo, intentos_fallidos, creado_at) "
                "VALUES (5, 'Diego', 'admin', ?, 1, 0, '2026-10-01 09:00:00')", (hash_pin("4321"),))
    con.execute("INSERT INTO ajustes (clave, valor, actualizado_at) VALUES ('odoo_url', 'https://magontex.odoo.com', '2026-10-10 10:00:00')")
    con.execute("INSERT INTO tareas (id, titulo, detalle, ref, seccion_slug, estado, prioridad, origen, creado_por_id, "
                "asignado_a_id, creada_at) VALUES (1, 'Prueba', '', '', 'despacho', 'pendiente', 'normal', 'manual', 5, 0, "
                "'2026-10-10 10:00:00')")
    con.commit()
    con.close()
    return d.read_bytes()


def test_importar_rechaza_archivo_que_no_es_base():
    with TestClient(app) as c:
        login(c, "Vengil", "1234")
        r = c.post("/admin/api/importar-base", files={"archivo": ("x.db", b"hola")},
                   headers={"x-requested-with": "fetch"})
        assert r.status_code == 400


def test_importar_base_reemplaza_y_arregla_referencias():
    with TestClient(app) as c:
        login(c, "Vengil", "1234")
        r = c.post("/admin/api/importar-base", files={"archivo": ("parka_deposito.db", _base_vieja())},
                   headers={"x-requested-with": "fetch"})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["tablas"]["usuarios"] == 1 and j["tablas"]["tareas"] == 1
        assert j["tablas"]["_referencias_arregladas"] >= 1
    with TestClient(app) as c:
        login(c, "Diego", "4321")
        assert c.get("/admin/importar-base", follow_redirects=False).status_code == 200
