import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_tmp}/test.db"
os.environ["CIERRE_AUTOMATICO"] = "0"
os.environ["ADMIN_PIN"] = "1234"
os.environ["ADMIN_NOMBRE"] = "Vengil"
os.environ["SMTP_USER"] = ""
os.environ["GAS_AUTO"] = "0"
os.environ["GAS_URL"] = "https://script.google.com/macros/s/FAKE/exec"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


@pytest.fixture(autouse=True, scope="module")
def base_limpia():
    """Cada archivo de tests arranca con la base vacía (más el admin, secciones y catálogo)."""
    from app.db import Base, engine, SessionLocal
    from app.seed import sembrar
    from app.services import ajustes
    from app.auth import olvidar_secciones
    ajustes.olvidar()
    olvidar_secciones()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        sembrar(db)
    finally:
        db.close()
    yield
