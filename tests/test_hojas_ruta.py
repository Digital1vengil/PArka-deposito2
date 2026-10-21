"""Hojas de ruta: el depósito arma la hoja con pedidos de Odoo y el chofer entrega escaneando cada bulto (API de Reparto)."""
from fastapi.testclient import TestClient

from app.auth import hash_pin
from app.db import SessionLocal
from app.main import app
from app.models import Usuario
from app.services import ajustes, hojas_ruta as hr, odoo, rotulos

from tests.test_rotulos import PARTNERS, FakeCliente, _limpiar, _login

CLAVE = "clave-de-prueba"


def _chofer(nombre="Chofer Juan", pin="5678") -> int:
    db = SessionLocal()
    try:
        u = Usuario(nombre=nombre, rol="chofer", pin_hash=hash_pin(pin), activo=True)
        db.add(u)
        db.commit()
        return u.id
    finally:
        db.close()


def test_armar_paradas_expreso_y_propio():
    """PROPIO (o sin expreso) → parada en el cliente; otro expreso → parada en su depósito, con todos sus clientes."""
    cli = lambda cid, nombre, exp, eid: {"id": cid, "codigo": rotulos.codigo_cliente(cid), "nombre": nombre, "calle": "Calle 1",
                                         "localidad": "Rosario", "cp": "2000", "provincia": "Santa Fe", "telefono": "",
                                         "expreso": exp, "expreso_id": eid, "expreso_dir": {"calle": "Rabanal 3321", "localidad": "CABA",
                                                                                            "cp": "", "provincia": ""}}
    peds = [{"id": 1, "nombre": "WH/PICK/1", "origen": "S1", "bultos": 2, "cliente": cli(10, "BBN", "JUPITER", 553)},
            {"id": 2, "nombre": "WH/PICK/2", "origen": "S2", "bultos": 1, "cliente": cli(11, "LALA", "JUPITER", 553)},
            {"id": 3, "nombre": "WH/PICK/3", "origen": "S3", "bultos": 3, "cliente": cli(12, "LEGAR", "PROPIO", 696)}]
    ps = hr.armar_paradas(peds, {3: 1})
    assert [p["tipo"] for p in ps] == ["expreso", "cliente"]
    assert ps[0]["nombre"] == "JUPITER" and ps[0]["direccion"] == "Rabanal 3321" and len(ps[0]["bultos"]) == 3
    assert ps[1]["nombre"] == "LEGAR" and ps[1]["direccion"] == "Calle 1" and len(ps[1]["bultos"]) == 1   # bultos corregidos


def test_hoja_de_ruta_completa(monkeypatch):
    fake = FakeCliente()
    monkeypatch.setattr(odoo, "cliente", lambda db=None, timeout=60: fake)
    _limpiar()
    cid = _chofer()
    with TestClient(app) as c:
        _login(c)
        assert "Hojas de ruta" in c.get("/hojas-ruta").text
        r = c.get("/s/hojas_ruta", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/hojas-ruta"
        peds = c.get("/hojas-ruta/api/pedidos?refrescar=1").json()["pedidos"]
        p412 = next(p for p in peds if p["id"] == 412)
        assert p412["destino"] == "Expreso JUPITER" and p412["hoja"] is None
        # solo usuarios Chofer
        db = SessionLocal()
        admin_id = db.query(Usuario).filter(Usuario.rol == "admin").first().id
        db.close()
        assert c.post("/hojas-ruta/api/crear", json={"chofer_id": admin_id, "pedidos": [412]}).status_code == 400
        h = c.post("/hojas-ruta/api/crear", json={"chofer_id": cid, "pedidos": [412, 500], "bultos": {"500": 2}}).json()
        assert h["bultos"] == 5 and [p["tipo"] for p in h["paradas"]] == ["expreso", "expreso"]
        jup = next(p for p in h["paradas"] if p["nombre"] == "JUPITER")
        via = next(p for p in h["paradas"] if p["nombre"] == "VIA 2")
        assert jup["direccion"] == "Rabanal 3321, CABA" and jup["clientes"][0]["nombre"] == "BBN S.R.L."
        # el mismo pedido no puede estar en dos hojas abiertas
        assert c.post("/hojas-ruta/api/crear", json={"chofer_id": cid, "pedidos": [412]}).status_code == 400
        assert next(p for p in c.get("/hojas-ruta/api/pedidos").json()["pedidos"] if p["id"] == 412)["hoja"] == h["id"]
        # reordenar paradas
        d = c.post(f"/hojas-ruta/api/{h['id']}/orden", json={"paradas": [via["id"], jup["id"]]}).json()
        assert [p["nombre"] for p in d["paradas"]] == ["VIA 2", "JUPITER"]
        assert "Hoja de ruta n.º" in c.get(f"/hojas-ruta/{h['id']}/imprimir.html").text

        # ── app del chofer ──
        assert c.get("/api/reparto/choferes").status_code == 503            # sin clave = apagada
        db = SessionLocal()
        ajustes.guardar(db, "reparto_clave", CLAVE)
        db.close()
        assert c.get("/api/reparto/choferes", headers={"X-Reparto-Clave": "otra"}).status_code == 401
        H = {"X-Reparto-Clave": CLAVE}
        assert [x["nombre"] for x in c.get("/api/reparto/choferes", headers=H).json()["choferes"]] == ["Chofer Juan"]
        assert c.post("/api/reparto/login", headers=H, json={"usuario_id": cid, "pin": "0000"}).status_code == 401
        assert c.post("/api/reparto/login", headers=H, json={"usuario_id": admin_id, "pin": "1234"}).status_code == 401
        tok = c.post("/api/reparto/login", headers=H, json={"usuario_id": cid, "pin": "5678"}).json()["token"]
        assert c.get("/api/reparto/hojas", headers=H).status_code == 401    # sin token
        A = {**H, "Authorization": f"Bearer {tok}"}
        hs = c.get("/api/reparto/hojas", headers=A).json()["hojas"]
        assert [x["id"] for x in hs] == [h["id"]] and hs[0]["bultos"] == 5
        hoja = c.get(f"/api/reparto/hojas/{h['id']}", headers=A).json()
        assert hoja["paradas"][0]["nombre"] == "VIA 2"
        url = f"/api/reparto/hojas/{h['id']}/escanear"
        esc = lambda par, cod: c.post(url, headers=A, json={"parada_id": par, "codigo": cod}).json()
        assert esc(jup["id"], "CLI-011387")["tipo"] == "invalido"
        assert esc(jup["id"], "http://testserver/b/412-1-3")["ok"] is True
        assert esc(jup["id"], "412-1-3")["tipo"] == "ya"
        assert esc(jup["id"], "500-1-2")["tipo"] == "otra_parada"
        assert esc(jup["id"], "999-1-1")["tipo"] == "no_esta"
        assert esc(jup["id"], "412-2-3")["ok"] is True
        # cerrar con un bulto sin escanear → incompleta
        r = c.post(f"/api/reparto/hojas/{h['id']}/paradas/{jup['id']}/cerrar", headers=A, json={"nota": "falta el 3"}).json()
        assert r == {"estado": "incompleta", "faltan": 1}
        assert esc(via["id"], "500-1-2")["ok"] and esc(via["id"], "500-2-2")["ok"]
        assert c.post(f"/api/reparto/hojas/{h['id']}/paradas/{via['id']}/cerrar", headers=A, json={}).json()["estado"] == "entregada"
        # el depósito ve el avance
        d = c.get(f"/hojas-ruta/api/{h['id']}").json()
        assert d["entregados"] == 4 and d["paradas_hechas"] == 2
        # otro chofer no ve esta hoja
        otro = _chofer("Chofer Pedro", "4321")
        tok2 = c.post("/api/reparto/login", headers=H, json={"usuario_id": otro, "pin": "4321"}).json()["token"]
        assert c.get(f"/api/reparto/hojas/{h['id']}", headers={**H, "Authorization": f"Bearer {tok2}"}).status_code == 404
        # con bultos entregados no se borra; se cierra
        assert c.post(f"/hojas-ruta/api/{h['id']}/borrar", json={}).status_code == 400
        assert c.post(f"/hojas-ruta/api/{h['id']}/cerrar", json={}).json()["estado"] == "cerrada"
        assert c.get("/api/reparto/hojas", headers=A).json()["hojas"] == []
        assert esc(jup["id"], "412-3-3")["tipo"] == "cerrada"
