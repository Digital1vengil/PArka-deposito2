"""PARKA Reparto con la app Despachos (Gonzalo): el chofer ve sus despachos (etiqueta = su nombre) y entrega por QR."""
from fastapi.testclient import TestClient

from app.db import SessionLocal, hoy
from app.main import app
from app.services import ajustes, despachos as dsp

from tests.test_hojas_ruta import CLAVE, _chofer


def _datos(fecha: str) -> dict:
    d = {"id": "dfea07586c314ee9", "fecha": fecha, "etiqueta": "Juani", "salidaConfirmadaAt": "2026-10-13T14:14:17.739Z"}
    ped = lambda venta, cli, b, exp, dire, oe: {"pedido": venta, "cliente": cli, "bultos": b, "bultosCargados": True, "expreso": exp,
                                                "direccion": dire, "ordenEntrega": oe, "ordenCarga": 4 - oe, "observaciones": "",
                                                "estado": "en_viaje", "despacho": d}
    return {"fecha": fecha, "cantidad": 4, "despachos": [{**d, "cantidad": 4}], "pedidos": [
        ped("S01101", "GOMEZ RODOLFO", 2, "DIAGONAL(BARRACAS)", "Beron de Astrada 2836 - Soldati, Capital Federal, Ciudad Autónoma de Buenos Aires (AR)", 3),
        ped("S01104", "OTOSINSA SRL", 9, "TRANSPORTELLI", "Av. Int. Francisco Rabanal 276, Capital Federal, Ciudad Autónoma de Buenos Aires (AR)", 1),
        ped("S01099", "TRANCHE ESTEBAN TOMAS", 1, "LAGO", "Ferré 2373, Ciudad Autonoma de BsAs, Ciudad Autónoma de Buenos Aires (AR)", 2),
        ped("S01110", "BBN S.R.L.", 1, "TRANSPORTELLI", "Av. Int. Francisco Rabanal 276, Capital Federal, Ciudad Autónoma de Buenos Aires (AR)", 4),
    ]}


def test_despachos_del_chofer(monkeypatch):
    llamadas = []

    def falso_get(db, ruta, params=None):
        llamadas.append((ruta, params))
        f = (params or {}).get("fecha")
        return _datos(f) if f == hoy().isoformat() else {"fecha": f, "cantidad": 0, "despachos": [], "pedidos": []}
    monkeypatch.setattr(dsp, "_get", falso_get)
    dsp.olvidar()
    juani, pedro = _chofer("Juani", "2468"), _chofer("Pedro", "1357")
    db = SessionLocal()
    ajustes.guardar(db, "reparto_clave", CLAVE)
    ajustes.guardar(db, "despachos_api_key", "dsp_test")
    db.close()
    ajustes.olvidar()
    H = {"X-Reparto-Clave": CLAVE}
    with TestClient(app) as c:
        tok = c.post("/api/reparto/login", headers=H, json={"usuario_id": juani, "pin": "2468"}).json()["token"]
        A = {**H, "Authorization": f"Bearer {tok}"}
        d = c.get("/api/reparto/hojas", headers=A).json()
        assert d["origen"] == "despachos" and [h["id"] for h in d["hojas"]] == ["dfea07586c314ee9"]
        assert d["hojas"][0]["bultos"] == 13 and d["hojas"][0]["paradas"] == 3
        assert all(r == "pedidos" for r, _ in llamadas) and len(llamadas) == dsp.DIAS_ATRAS
        h = c.get("/api/reparto/hojas/dfea07586c314ee9", headers=A).json()
        # una parada por expreso, en el orden de entrega de Despachos; los dos pedidos de TRANSPORTELLI juntos
        assert [p["nombre"] for p in h["paradas"]] == ["TRANSPORTELLI", "LAGO", "DIAGONAL(BARRACAS)"]
        tr = h["paradas"][0]
        assert tr["direccion"] == "Av. Int. Francisco Rabanal 276" and tr["bultos"] == 10
        assert [x["nombre"] for x in tr["clientes"]] == ["OTOSINSA SRL", "BBN S.R.L."]
        lago = h["paradas"][1]["id"]
        url = "/api/reparto/hojas/dfea07586c314ee9/escanear"
        esc = lambda par, cod: c.post(url, headers=A, json={"parada_id": par, "codigo": cod}).json()
        assert esc(tr["id"], "https://x/b/S01104-1-9")["ok"] is True
        assert esc(tr["id"], "S01104-1-9")["tipo"] == "ya"
        assert esc(tr["id"], "S01099-1-1")["tipo"] == "otra_parada"
        assert esc(tr["id"], "S09999-1-1")["tipo"] == "no_esta"
        r = esc(tr["id"], "S01104-12-12")                       # el rótulo dice más bultos que Despachos
        assert r["tipo"] == "no_esta" and "9 bultos" in r["mensaje"]
        assert esc(lago, "S01099-1-1")["ok"] is True
        r = c.post(f"/api/reparto/hojas/dfea07586c314ee9/paradas/{lago}/cerrar", headers=A, json={"nota": "Recibió Ana"}).json()
        assert r == {"estado": "entregada", "faltan": 0}
        r = c.post(f"/api/reparto/hojas/dfea07586c314ee9/paradas/{tr['id']}/cerrar", headers=A, json={}).json()
        assert r == {"estado": "incompleta", "faltan": 9}
        h = c.get("/api/reparto/hojas/dfea07586c314ee9", headers=A).json()
        assert h["entregados"] == 2 and h["paradas_hechas"] == 2 and h["paradas"][1]["nota"] == "Recibió Ana"
        # Pedro no tiene despachos ni puede ver el de Juani
        tok2 = c.post("/api/reparto/login", headers=H, json={"usuario_id": pedro, "pin": "1357"}).json()["token"]
        B = {**H, "Authorization": f"Bearer {tok2}"}
        assert c.get("/api/reparto/hojas", headers=B).json()["hojas"] == []
        assert c.get("/api/reparto/hojas/dfea07586c314ee9", headers=B).status_code == 404
    db = SessionLocal()
    from app.models import EntregaDespacho
    assert [(e.pedido, e.bulto, e.enviado) for e in db.query(EntregaDespacho).order_by(EntregaDespacho.id)] == \
        [("S01104", 1, False), ("S01099", 1, False)]   # guardados, falta avisarle a Despachos
    db.close()


def test_etiqueta_es_el_chofer():
    assert dsp.es_del_chofer("Juani", "Juani") and dsp.es_del_chofer("juani", "Juani Pérez")
    assert not dsp.es_del_chofer("Juani", "Juan") and not dsp.es_del_chofer("", "Juani")
