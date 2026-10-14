"""Rendimiento: las pantallas que se refrescan solas tienen que hacer pocas consultas a la base,
sin importar cuántos paquetes / búsquedas haya (en la nube cada consulta va y vuelve a Neon)."""
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.db import engine
from app.main import app
from tests.test_ecommerce import ARCHIVOS, _login
from tests.test_parsers import ZPL


def _contar(c, url):
    n = [0]

    def f(*a, **k):
        n[0] += 1
    event.listen(engine, "before_cursor_execute", f)
    try:
        r = c.get(url)
    finally:
        event.remove(engine, "before_cursor_execute", f)
    assert r.status_code == 200, url
    return n[0]


def test_consultas_constantes():
    with TestClient(app) as c:
        _login(c)
        for i in range(4):
            c.post("/despacho/api/lotes", data={"tipo": "flex", "nombre": f"Flex {i}"},
                   files=[("archivos", ("et.txt", ZPL.encode()))])
        files = [("archivos", (n, b, "application/octet-stream")) for n, b in ARCHIVOS]
        for _ in range(4):
            cid = c.post("/ecommerce/api/crear/archivo", files=files).json()["busqueda"]["id"]
        c.post(f"/ecommerce/api/{cid}/sumarme")
        limites = {"/": 20, "/despacho/api/estado": 10, "/ecommerce/api/lista": 10, f"/ecommerce/api/{cid}": 10,
                   f"/ecommerce/api/{cid}/mia": 10, f"/ecommerce/api/{cid}/todo": 10, "/tareas/api/lista": 8,
                   "/supervision/api/datos": 20, "/conteo/api/historial": 4, "/devoluciones/api/historial": 4}
        for url, tope in limites.items():
            n = _contar(c, url)
            assert n <= tope, f"{url}: {n} consultas (máximo {tope})"


def test_estaticos_con_version_se_guardan_en_cache():
    with TestClient(app) as c:
        r = c.get("/static/app.js?v=x")
        assert "immutable" in r.headers["cache-control"]
        assert "must-revalidate" in c.get("/static/app.js").headers["cache-control"]
