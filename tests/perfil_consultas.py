"""Mide cuántas consultas a la base hace cada pantalla/endpoint que se refresca solo.
Uso: python -m tests.perfil_consultas   (no es un test de pytest)"""
import os, sys, tempfile, re
from pathlib import Path
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_tmp}/perf.db"
for k, v in {"CIERRE_AUTOMATICO": "0", "ADMIN_PIN": "1234", "ADMIN_NOMBRE": "Vengil", "GAS_AUTO": "0",
             "GAS_URL": "https://script.google.com/macros/s/FAKE/exec"}.items():
    os.environ[k] = v
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import event
from fastapi.testclient import TestClient
from app.db import engine
from app.main import app
from tests.test_parsers import ZPL
from tests.test_ecommerce import ARCHIVOS, _login

N = {"q": 0}
event.listen(engine, "before_cursor_execute", lambda *a, **k: N.__setitem__("q", N["q"] + 1))


def medir(c, metodo, url, **kw):
    N["q"] = 0
    r = getattr(c, metodo)(url, **kw)
    return r, N["q"]


with TestClient(app) as c:
    _login(c)
    for i in range(4):
        c.post("/despacho/api/lotes", data={"tipo": "flex", "nombre": f"Flex {i}"},
               files=[("archivos", ("et.txt", ZPL.encode()))])
    files = [("archivos", (n, b, "application/octet-stream")) for n, b in ARCHIVOS]
    for _ in range(int(os.environ.get("BUSQ", "5"))):
        cid = c.post("/ecommerce/api/crear/archivo", files=files).json()["busqueda"]["id"]
    c.post(f"/ecommerce/api/{cid}/sumarme")
    urls = ["/", "/despacho", "/despacho/api/estado", "/ecommerce", "/ecommerce/api/novedades?desde=-1",
            "/ecommerce/api/novedades", "/ecommerce/api/auto", "/ecommerce/api/lista", f"/ecommerce/api/{cid}",
            f"/ecommerce/api/{cid}/mia", f"/ecommerce/api/{cid}/todo", f"/ecommerce/api/{cid}/boxes",
            "/tareas/api/lista", "/supervision", "/supervision/api/datos", "/conteo/api/historial", "/devoluciones/api/historial", "/conteo", "/devoluciones", "/admin/secciones"]
    res = {}
    for u in urls:
        r, q = medir(c, "get", u)
        res[u] = (r.status_code, q, len(r.content))
    for u, (s, q, b) in res.items():
        print(f"{q:5d} consultas  {s}  {b/1024:7.1f} KB  {u}")

if os.environ.get("VER"):
    from collections import Counter
    with TestClient(app) as c:
        _login(c)
        for u in os.environ["VER"].split(","):
            sql = Counter()
            def f(conn, cur, st, *a, **k): sql[re.sub(r"\s+", " ", st)[:150]] += 1
            event.listen(engine, "before_cursor_execute", f)
            c.get(u)
            event.remove(engine, "before_cursor_execute", f)
            print("==", u)
            for s, n in sql.most_common(12): print(f"  {n:3d}x {s}")
