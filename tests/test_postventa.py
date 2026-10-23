"""Respondedor de mensajes de post venta de ML con Gemini (sección Mercado Libre → Post venta)."""
import json
import re
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models import Actividad, MensajeML
from app.services import ajustes, asistente, meli, postventa

SELLER, COMPRADOR = 413629450, 777


def _login(c, nombre="Vengil", pin="1234"):
    uid = re.search(r"elegir\((\d+), '" + nombre + r"'\)", c.get("/login").text).group(1)
    assert c.post("/login", data={"usuario_id": uid, "pin": pin}).json()["ok"]


def _msj(mid, de, texto, hora):
    return {"id": mid, "from": {"user_id": de}, "to": {"user_id": SELLER if de == COMPRADOR else COMPRADOR},
            "text": texto, "message_date": {"created": f"2026-10-22T{hora}:00.000-03:00"}}


ORDEN = {"id": 2000001, "status": "paid", "date_created": "2026-10-20T10:00:00", "shipping": {"id": 44},
         "order_items": [{"item": {"id": "MLA1", "title": "Campera Puffer PARKA",
                                   "variation_attributes": [{"name": "Talle", "value_name": "L"}]}, "quantity": 1}]}


@pytest.fixture
def ml(monkeypatch):
    """ML falso por el proxy de Costos + Gemini falso."""
    estado = {"unread": ["2000001", "2000002"], "enviados": [], "gemini": [],
              "convs": {"2000001": [_msj("a1", COMPRADOR, "Hola, ¿cuándo me llega?", "10:00")],
                        "2000002": [_msj("b1", COMPRADOR, "Me llegó fallada la campera, quiero cambiarla", "11:00")]}}

    def pedir(req):
        url = req.full_url
        if url.endswith("/write"):
            body = json.loads(req.data)
            assert body["method"] == "POST"
            m = re.match(r"/messages/packs/(\d+)/sellers/(\d+)\?tag=post_sale$", body["path"])
            assert m and m.group(2) == str(SELLER)
            estado["enviados"].append({"pack": m.group(1), **body["body"]})
            estado["convs"][m.group(1)].append(_msj("r" + m.group(1), SELLER, body["body"]["text"], "12:00"))
            estado["unread"] = [p for p in estado["unread"] if p != m.group(1)]
            return {"status": 200, "ok": True, "data": {}}
        path = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["path"][0]
        if path == "/users/me":
            return {"status": 200, "data": {"id": SELLER}}
        if path.startswith("/messages/unread"):
            return {"status": 200, "data": {"results": [{"resource": f"/packs/{p}/sellers/{SELLER}", "count": 1}
                                                        for p in estado["unread"]]}}
        m = re.match(r"/messages/packs/(\d+)/sellers/\d+\?", path)
        if m:
            assert "mark_as_read=false" in path
            return {"status": 200, "data": {"conversation_status": {"status": "active"},
                                            "messages": list(reversed(estado["convs"][m.group(1)]))}}
        if path == "/orders/2000001":
            return {"status": 200, "data": ORDEN}
        if path == "/orders/2000002":
            return {"status": 200, "data": {**ORDEN, "id": 2000002}}
        if path == "/shipments/44":
            return {"status": 200, "data": {"status": "shipped", "logistic_type": "cross_docking", "tracking_number": "TR1",
                                            "shipping_option": {"estimated_delivery_time": {"date": "2026-10-24"}}}}
        return {"status": 404, "data": {"message": "not found"}}

    def gemini(cuerpo):
        texto = cuerpo["contents"][0]["parts"][0]["text"]
        estado["gemini"].append(texto)
        if "PEDIDO DE QUIEN REVISA" in texto:
            r = {"respuesta": "¡Hola! Lo revisamos hoy y te escribimos.", "decision": "revisar", "categoria": "cambio_devolucion", "motivo": "x"}
        elif "fallada" in texto:
            r = {"respuesta": "¡Hola! Lamentamos lo ocurrido.", "decision": "simple", "categoria": "estado_envio", "motivo": "Gemini se equivoca"}
        else:
            r = {"respuesta": "¡Hola! Tu compra está en camino y llega el 24/10.", "decision": "simple",
                 "categoria": "estado_envio", "motivo": "hay fecha estimada"}
        return {"candidates": [{"content": {"parts": [{"text": json.dumps(r)}]}}]}

    monkeypatch.setattr(meli, "_pedir", pedir)
    monkeypatch.setattr(meli, "PAUSA", 0)
    monkeypatch.setattr(meli, "_yo", {})
    monkeypatch.setattr(asistente, "_llamar", gemini)
    monkeypatch.setattr(postventa, "_ultima", [None])
    db = SessionLocal()
    for k, v in (("meli_api_key", "clave-ok"), ("gemini_api_key", "g-ok"), ("parkahub_client_id", ""),
                 ("resp_modo", "manual"), ("resp_firma", "¡Saludos! Equipo PARKA"), ("resp_instrucciones", "")):
        ajustes.guardar(db, k, v)
    db.query(MensajeML).delete()
    db.commit()
    db.close()
    return estado


def test_manual_redacta_con_datos_de_la_venta_y_no_envia(ml):
    with TestClient(app) as c:
        _login(c)
        assert "Post venta" in c.get("/ml").text
        r = c.post("/ml/api/postventa/revisar").json()
        v = r["vuelta"]
        assert v["nuevas"] == 2 and v["redactadas"] == 2 and v["publicadas"] == 0 and ml["enviados"] == []
        p = {m["pack_id"]: m for m in r["pendientes"]}
        simple, reclamo = p["2000001"], p["2000002"]
        assert simple["borrador"].endswith("¡Saludos! Equipo PARKA") and simple["saldria_sola"]
        assert simple["venta"].startswith("Campera Puffer PARKA") and "Talle L" in simple["venta"]
        # el reclamo nunca sale solo aunque Gemini diga "simple"
        assert not reclamo["saldria_sola"] and any("reclamo" in f for f in reclamo["frenos"])
        ctx = next(t for t in ml["gemini"] if "cuándo me llega" in t)
        assert "2026-10-24" in ctx and "TR1" in ctx and "Comprador: Hola, ¿cuándo me llega?" in ctx
        # segunda vuelta: no redacta de nuevo
        r2 = c.post("/ml/api/postventa/revisar").json()
        assert r2["vuelta"]["nuevas"] == 0 and r2["vuelta"]["redactadas"] == 0 and len(ml["gemini"]) == 2


def test_auto_envia_solo_lo_seguro(ml):
    with TestClient(app) as c:
        _login(c)
        assert c.post("/ml/api/respondedor/config", json={"modo": "auto"}).json()["modo"] == "auto"
        r = c.post("/ml/api/postventa/revisar").json()
        assert r["vuelta"]["publicadas"] == 1
        assert ml["enviados"] == [{"pack": "2000001", "from": {"user_id": str(SELLER)}, "to": {"user_id": str(COMPRADOR)},
                                   "text": "¡Hola! Tu compra está en camino y llega el 24/10. ¡Saludos! Equipo PARKA"}]
        assert [m["pack_id"] for m in r["pendientes"]] == ["2000002"]
        assert r["publicadas"][0]["auto"] and r["publicadas"][0]["publicada_por"] == "Automático"
        # el comprador vuelve a escribir → la conversación vuelve a la lista con el mensaje nuevo
        ml["convs"]["2000001"].append(_msj("a2", COMPRADOR, "¡Gracias! Me llegó perfecto", "13:00"))
        ml["unread"].append("2000001")
        c.post("/ml/api/respondedor/config", json={"modo": "manual"})
        r = c.post("/ml/api/postventa/revisar").json()
        assert r["vuelta"]["actualizadas"] == 1
        m = next(x for x in r["pendientes"] if x["pack_id"] == "2000001")
        assert m["ultimo_texto"] == "¡Gracias! Me llegó perfecto" and "PARKA: ¡Hola! Tu compra" in m["conversacion"]
    db = SessionLocal()
    assert db.query(Actividad).filter(Actividad.accion == "postventa_auto").count() == 1
    db.close()


def test_enviar_a_mano_largo_y_respondido_en_ml(ml):
    with TestClient(app) as c:
        _login(c)
        r = c.post("/ml/api/postventa/revisar").json()
        mid = next(m["id"] for m in r["pendientes"] if m["pack_id"] == "2000002")
        m = c.post(f"/ml/api/mensajes/{mid}/redactar", json={"indicacion": "decile que lo revisamos hoy"}).json()
        assert m["borrador"] == "¡Hola! Lo revisamos hoy y te escribimos. ¡Saludos! Equipo PARKA"
        assert "BORRADOR ANTERIOR" in ml["gemini"][-1]
        assert c.post(f"/ml/api/mensajes/{mid}/publicar", json={"texto": "x" * 351}).status_code == 400
        m = c.post(f"/ml/api/mensajes/{mid}/publicar", json={"texto": "¡Hola! Ya lo vemos."}).json()
        assert m["estado"] == "publicada" and m["publicada_por"] == "Vengil" and not m["auto"]
        assert c.post(f"/ml/api/mensajes/{mid}/publicar", json={"texto": "otra"}).status_code == 400
        # la otra se respondió desde ML (último mensaje es del vendedor) → se cierra sola
        ml["convs"]["2000001"].append(_msj("x9", SELLER, "Respondido desde ML", "12:30"))
        ml["unread"] = []
        r = c.post("/ml/api/postventa/revisar").json()
        assert r["vuelta"]["cerradas"] == 1 and r["pendientes"] == []


def test_sin_clave_de_costos(ml):
    db = SessionLocal()
    ajustes.guardar(db, "meli_api_key", "")
    db.close()
    with TestClient(app) as c:
        _login(c)
        e = c.post("/ml/api/postventa/revisar")
        assert e.status_code == 400 and "API de Costos" in e.json()["detail"]
        assert c.get("/ml/api/postventa").json()["config"]["ml"] is False
