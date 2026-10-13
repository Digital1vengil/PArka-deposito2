"""Respondedor de preguntas de ML con Gemini (sección Mercado Libre → Preguntas)."""
import json
import re
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models import Actividad, PreguntaML
from app.services import ajustes, asistente, meli, respondedor


def _login(c, nombre="Vengil", pin="1234"):
    uid = re.search(r"elegir\((\d+), '" + nombre + r"'\)", c.get("/login").text).group(1)
    assert c.post("/login", data={"usuario_id": uid, "pin": pin}).json()["ok"]


PREGUNTAS = [
    {"id": 101, "item_id": "MLA1", "text": "¿Tenés talle M en negro?", "date_created": "2026-10-07T09:00:00"},
    {"id": 102, "item_id": "MLA1", "text": "Hola, me llegó fallada la campera, ¿cómo la cambio?", "date_created": "2026-10-07T09:05:00"},
]
ITEM = {"id": "MLA1", "title": "Campera Puffer PARKA", "status": "active", "price": 89999, "available_quantity": 5,
        "listing_type_id": "gold_pro", "shipping": {"free_shipping": True}, "permalink": "https://x",
        "attributes": [{"id": "MODEL", "value_name": "PUF"}, {"id": "BRAND", "value_name": "Parka"}],
        "variations": [{"available_quantity": 3, "price": 89999,
                        "attribute_combinations": [{"name": "Color", "value_name": "Negro"}, {"name": "Talle", "value_name": "M"}],
                        "attributes": [{"id": "SELLER_SKU", "value_name": "PUF-NEG-M"}]},
                       {"available_quantity": 0, "price": 89999,
                        "attribute_combinations": [{"name": "Color", "value_name": "Negro"}, {"name": "Talle", "value_name": "XL"}],
                        "attributes": [{"id": "SELLER_SKU", "value_name": "PUF-NEG-XL"}]}]}


@pytest.fixture
def ml(monkeypatch):
    """ML falso por el proxy de Costos + Gemini falso."""
    estado = {"vivas": [dict(q) for q in PREGUNTAS], "respuestas": [], "gemini": [], "falla_answer": None}

    def pedir(req):
        url = req.full_url
        if url.endswith("/write"):
            body = json.loads(req.data)
            if estado["falla_answer"]:
                raise meli.MeliError(*estado["falla_answer"])
            assert body["method"] == "POST" and body["path"] == "/answers"
            estado["respuestas"].append(body["body"])
            estado["vivas"] = [q for q in estado["vivas"] if q["id"] != body["body"]["question_id"]]
            return {"status": 200, "ok": True, "data": {"id": body["body"]["question_id"]}}
        path = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["path"][0]
        if path == "/users/me":
            return {"status": 200, "data": {"id": 413629450}}
        if path.startswith("/questions/search") and "status=UNANSWERED" in path:
            return {"status": 200, "data": {"questions": estado["vivas"]}}
        if path.startswith("/questions/search"):
            return {"status": 200, "data": {"questions": [{"id": 1, "text": "¿Abriga?", "answer": {"text": "¡Hola! Sí, mucho."}}]}}
        if path.startswith("/items?ids="):
            return {"status": 200, "data": [{"code": 200, "body": {"id": "MLA1", "title": ITEM["title"], "attributes": ITEM["attributes"]}}]}
        if path == "/items/MLA1/description":
            return {"status": 200, "data": {"plain_text": "Largo talle M: 70 cm."}}
        if path == "/items/MLA1":
            return {"status": 200, "data": ITEM}
        return {"status": 404, "data": {}}

    def gemini(cuerpo):
        texto = cuerpo["contents"][0]["parts"][0]["text"]
        estado["gemini"].append(texto)
        assert cuerpo["generationConfig"]["responseMimeType"] == "application/json"
        if "PEDIDO DE QUIEN REVISA" in texto:
            r = {"respuesta": "¡Hola! Sí, hay M en negro.", "decision": "simple", "categoria": "stock", "motivo": "corto"}
        elif "fallada" in texto:
            r = {"respuesta": "¡Hola! Lamentamos lo ocurrido.", "decision": "simple", "categoria": "postventa", "motivo": "x"}
        else:
            r = {"respuesta": "¡Hola! Sí, tenemos talle M en negro disponible, podés comprarlo directo.",
                 "decision": "simple", "categoria": "stock", "motivo": "stock en la publicación"}
        return {"candidates": [{"content": {"parts": [{"text": json.dumps(r)}]}}]}

    monkeypatch.setattr(meli, "_pedir", pedir)
    monkeypatch.setattr(meli, "_yo", {})
    monkeypatch.setattr(asistente, "_llamar", gemini)
    monkeypatch.setattr(respondedor, "_ultima", [None])
    db = SessionLocal()
    for k, v in (("meli_api_key", "clave-ok"), ("gemini_api_key", "g-ok"), ("parkahub_client_id", ""),
                 ("resp_modo", "manual"), ("resp_firma", "¡Saludos! Equipo PARKA"), ("resp_instrucciones", "")):
        ajustes.guardar(db, k, v)
    db.query(PreguntaML).delete()
    db.commit()
    db.close()
    return estado


def test_panel_parka_ya_no_esta():
    with TestClient(app) as c:
        _login(c)
        assert c.get("/panel").status_code == 404
        assert 'href="/panel"' not in c.get("/").text


def test_manual_redacta_y_no_publica(ml):
    with TestClient(app) as c:
        _login(c)
        r = c.post("/ml/api/respondedor/revisar").json()
        v = r["vuelta"]
        assert v["nuevas"] == 2 and v["redactadas"] == 2 and v["publicadas"] == 0
        assert ml["respuestas"] == []
        p = {q["question_id"]: q for q in r["pendientes"]}
        simple, reclamo = p["101"], p["102"]
        assert simple["borrador"].endswith("¡Saludos! Equipo PARKA") and simple["saldria_sola"]
        # el reclamo nunca sale solo en mixto aunque Gemini diga "simple"
        assert not reclamo["saldria_sola"] and any("reclamo" in f for f in reclamo["frenos"])
        # Gemini recibió stock real por variante, descripción y respuestas anteriores
        ctx = ml["gemini"][0]
        assert "Talle: M" in ctx and '"stock": 3' in ctx and "70 cm" in ctx and "¿Abriga?" in ctx
        # segunda vuelta: no redacta de nuevo
        r2 = c.post("/ml/api/respondedor/revisar").json()
        assert r2["vuelta"]["nuevas"] == 0 and r2["vuelta"]["redactadas"] == 0 and len(ml["gemini"]) == 2


def test_mixto_publica_solo_las_simples(ml):
    with TestClient(app) as c:
        _login(c)
        assert c.post("/ml/api/respondedor/config", json={"modo": "mixto"}).json()["modo"] == "mixto"
        r = c.post("/ml/api/respondedor/revisar").json()
        assert r["vuelta"]["publicadas"] == 1
        assert ml["respuestas"] == [{"question_id": 101, "text": "¡Hola! Sí, tenemos talle M en negro disponible, podés comprarlo directo. ¡Saludos! Equipo PARKA"}]
        assert [q["question_id"] for q in r["pendientes"]] == ["102"]
        pub = r["publicadas"]
        assert pub[0]["auto"] and pub[0]["publicada_por"] == "Automático"
    db = SessionLocal()
    assert db.query(Actividad).filter(Actividad.accion == "pregunta_auto").count() == 1
    assert db.query(Actividad).filter(Actividad.accion == "respondedor_modo").count() == 1
    db.close()


def test_publicar_a_mano_editada_y_otra_version(ml):
    with TestClient(app) as c:
        _login(c)
        r = c.post("/ml/api/respondedor/revisar").json()
        pid = next(q["id"] for q in r["pendientes"] if q["question_id"] == "102")
        q = c.post(f"/ml/api/preguntas/{pid}/redactar", json={"indicacion": "más corto"}).json()
        assert q["borrador"] == "¡Hola! Sí, hay M en negro. ¡Saludos! Equipo PARKA"
        assert "BORRADOR ANTERIOR" in ml["gemini"][-1]
        assert c.post(f"/ml/api/preguntas/{pid}/publicar", json={"texto": "x" * 2001}).status_code == 400
        q = c.post(f"/ml/api/preguntas/{pid}/publicar", json={"texto": "¡Hola! Escribinos por la mensajería de la venta."}).json()
        assert q["estado"] == "publicada" and q["publicada_por"] == "Vengil" and not q["auto"]
        assert c.post(f"/ml/api/preguntas/{pid}/publicar", json={"texto": "otra"}).status_code == 400
        # la que quedó: se respondió en ML por otro lado → sale de la lista en la próxima vuelta
        ml["vivas"] = []
        r = c.post("/ml/api/respondedor/revisar").json()
        assert r["vuelta"]["cerradas"] == 1 and r["pendientes"] == []


def test_auto_frena_contacto_y_error_de_ml(ml, monkeypatch):
    def gemini(cuerpo):
        r = {"respuesta": "¡Hola! Escribinos al 11 5555 1234", "decision": "simple", "categoria": "otro", "motivo": ""}
        return {"candidates": [{"content": {"parts": [{"text": json.dumps(r)}]}}]}
    monkeypatch.setattr(asistente, "_llamar", gemini)
    with TestClient(app) as c:
        _login(c)
        c.post("/ml/api/respondedor/config", json={"modo": "auto"})
        r = c.post("/ml/api/respondedor/revisar").json()
        assert r["vuelta"]["publicadas"] == 0 and ml["respuestas"] == []
        assert all("contacto" in " ".join(q["frenos"]) for q in r["pendientes"])
        ml["falla_answer"] = ("Fuera del perfil de la clave: /answers", 403)
        pid = r["pendientes"][0]["id"]
        e = c.post(f"/ml/api/preguntas/{pid}/publicar", json={"texto": "¡Hola! Sí."})
        assert e.status_code == 400 and "perfil" in e.json()["detail"]
        assert c.get("/ml/api/respondedor").json()["pendientes"][-1]["estado"] in ("error", "borrador")


def test_sin_clave_de_costos_no_publica(ml):
    db = SessionLocal()
    ajustes.guardar(db, "meli_api_key", "")
    db.close()
    with TestClient(app) as c:
        _login(c)
        assert c.post("/ml/api/respondedor/revisar").status_code == 400   # sin ML conectado
        cfg = c.get("/ml/api/respondedor").json()["config"]
        assert cfg["ml"] is False and cfg["publica"] is False


def test_solo_admin_cambia_el_modo(ml):
    with TestClient(app) as c:
        _login(c)
        assert c.post("/admin/api/usuarios", json={"nombre": "Ana", "rol": "despacho", "pin": "5555"}).json()["ok"]
        html = c.get("/admin/usuarios").text
        uid = int(re.search(r'<tr data-id="(\d+)">\s*<td><input class="input !py-1.5 f-nombre min-w-\[120px\]" value="Ana"', html).group(1))
        c.post(f"/admin/api/usuarios/{uid}/accesos", json={"accesos": ["ml"]})
        assert c.post("/ml/api/respondedor/config", json={"modo": "nada"}).status_code == 400
    with TestClient(app) as ana:
        _login(ana, "Ana", "5555")
        assert ana.get("/ml").status_code == 200
        assert ana.post("/ml/api/respondedor/config", json={"modo": "auto"}).status_code == 403
