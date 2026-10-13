"""Etapa 2: lo de ParkaHub dentro de PARKA Depósito (semáforo, cotejo y sección Mercado Libre)."""
import re

from fastapi.testclient import TestClient

from app.main import app
from app.services import ajustes, parkahub


def _login(c, nombre="Vengil", pin="1234"):
    uid = re.search(r"elegir\((\d+), '" + nombre + r"'\)", c.get("/login").text).group(1)
    assert c.post("/login", data={"usuario_id": uid, "pin": pin}).json()["ok"]


READY = {"tipo": "flex", "supported": True,
         "ready": [{"shipment_id": 44000000001, "order_id": 2000001, "day": "2026-10-01"},
                   {"shipment_id": 44000000002, "order_id": 2000002, "day": "2026-10-01"}],
         "states": {"44000000001": "ready_to_ship|ready_to_print|self_service|2026-10-01",
                    "44000000002": "ready_to_ship||self_service|2026-10-01",
                    "44000000009": "cancelled|cancelled_manually|self_service|2026-09-30"},
         "fresh": {"min": "2026-10-01 12:00:00", "max": "2026-10-01 12:30:00", "covered": True}}


def falso_get(llamadas):
    def get(ruta, params=None):
        llamadas.append((ruta, dict(params or {})))
        if ruta == "/api/dispatch/verify":
            if params["code"] == "44000000009":
                return {"verdict": "red", "reason": "cancelled", "message": "VENTA CANCELADA — NO DESPACHAR",
                        "detail": "Sacá el paquete", "shipment": {"status": "cancelled"}, "scanned": params["code"]}
            return {"verdict": "green", "reason": "ready", "message": "Lista para despachar",
                    "shipment": {"status": "ready_to_ship", "logistic_type": "self_service"}, "scanned": params["code"]}
        if ruta == "/api/dispatch/ready":
            return READY
        if ruta == "/api/questions":
            return {"ok": True, "pending": [{"question_id": 1, "item_id": "MLA1", "text": "¿Hay talle L?",
                                             "title": "Campera", "suggested": "Sí, hay L"}],
                    "answered": [{"question_id": 2, "item_id": "MLA1", "text": "¿Color?", "answer_text": "Negro"}]}
        if ruta == "/api/returns":
            return {"ok": True, "rows": [{"sku": "P-1", "reason_name": "Talle", "date_created": "2099-01-01"}]}
        if ruta == "/api/status":
            return {"ml": {"connected": True}, "warehouse": {}}
        if ruta == "/api/ml/reputation":
            return {"level_id": "5_green", "power_seller_status": "platinum", "metrics": {}}
        if ruta == "/api/ml/orders":
            return {"results": [], "paging": {"total": 0}}
        if ruta == "/api/ml/pub-search":
            return {"hits": [{"id": "MLA9", "model": "PK-100", "title": "Campera PK-100"}]}
        raise parkahub.ParkaHubError("ruta no simulada " + ruta)
    return get


def test_cotejar_puro():
    paquetes = [{"tracking": "44000000001", "estado": "despachado"},
                {"tracking": "44000000009", "estado": "pendiente"},
                {"tracking": "44000000077", "estado": "pendiente"}]
    r = parkahub.cotejar(paquetes, READY["ready"], READY["states"])
    assert r["coinciden"] == 1 and r["sin_escanear"] == 0
    assert [x["envio"] for x in r["solo_ml"]] == ["44000000002"]
    assert r["cancelados"] == 1 and r["solo_lista"][0]["envio"] == "44000000009"  # cancelados primero
    assert "CANCELADO" in r["solo_lista"][0]["estado_ml"]
    assert "14 días" in r["solo_lista"][1]["estado_ml"]


def test_semaforo_cotejo_y_seccion_ml(monkeypatch):
    llamadas = []
    with TestClient(app) as c:
        _login(c)
        # sin token: el scanner sigue igual y no aparece el cotejo
        assert c.get("/despacho/api/ml/verificar?codigo=44000000001").json() == {"ok": False, "sin_conexion": True}
        assert "Cotejar con ML" not in c.get("/despacho").text
        html = c.get("/despacho/scanner/").text
        assert '<script src="ml-semaforo.js"></script>' in html
        assert "processScan" in c.get("/despacho/scanner/ml-semaforo.js").text

        from app.db import SessionLocal
        db = SessionLocal()
        ajustes.guardar(db, "parkahub_client_id", "id.access")
        ajustes.guardar(db, "parkahub_client_secret", "secreto")
        db.close()
        monkeypatch.setattr(parkahub, "get", falso_get(llamadas))

        assert "Cotejar con ML" in c.get("/despacho").text
        v = c.get("/despacho/api/ml/verificar?codigo=44000000001").json()
        assert v["ok"] and v["veredicto"] == "green"
        v = c.get("/despacho/api/ml/verificar?codigo=44000000009").json()
        assert v["veredicto"] == "red" and "CANCELADA" in v["mensaje"]
        # cotejo: nunca pide refresh salvo que se pida
        r = c.post("/despacho/api/ml/cotejo", json={"tipo": "flex", "paquetes": [{"tracking": "44000000001", "estado": "pendiente"}]}).json()
        assert r["coinciden"] == 1 and r["sin_escanear"] == 1 and len(r["solo_ml"]) == 1
        assert llamadas[-1] == ("/api/dispatch/ready", {"tipo": "flex", "refresh": None})
        c.post("/despacho/api/ml/cotejo", json={"tipo": "colecta", "paquetes": [], "refrescar": True})
        assert llamadas[-1][1]["refresh"] == "1"
        r = c.post("/despacho/api/ml/cotejo", json={"tipo": "tiendanube", "paquetes": []})
        assert r.status_code == 400 and "Tienda Nube" in r.json()["detail"]

        # sección Mercado Libre
        assert "/ml" in c.get("/").text
        assert "Mercado Libre" in c.get("/ml").text
        res = c.get("/ml/api/resumen").json()
        assert res["reputacion"]["nivel"] == "5_green" and len(res["preguntas"]) == 1 and not res["errores"]
        p = c.get("/ml/api/preguntas").json()
        assert p["pendientes"][0]["sugerencia_parkahub"] == "Sí, hay L" and p["respondidas"][0]["respuesta"] == "Negro"
        assert c.get("/ml/api/devoluciones?dias=30").json()["por_motivo"] == {"Talle": 1}
        assert c.get("/ml/api/buscar?q=PK-100").json()["tipo"] == "publicaciones"
        assert c.get("/ml/api/buscar?q=44000000009").json()["tipo"] == "envio"   # no es venta → envío
        # ningún pedido a ParkaHub fue escritura
        assert all(r.startswith("/api/") for r, _ in llamadas)


def test_ml_solo_admin_por_defecto():
    with TestClient(app) as c:
        _login(c)
        sec = c.get("/admin/secciones").text
        assert "Mercado Libre" in sec
