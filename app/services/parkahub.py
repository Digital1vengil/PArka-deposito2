"""Conector con ParkaHub (la app de Martin): de acá salen los datos de Mercado Libre y Tienda Nube.

ParkaHub ya tiene el token de ML, lo renueva solo y cachea catálogo/ventas/devoluciones en su base.
PARKA Depósito NO tiene token propio de ML: le pregunta a ParkaHub. Así hay una sola conexión con ML
y ParkaHub sigue siendo el único que escribe precios y promos.

Autenticación: service token de Cloudflare Access (Client ID + Client Secret) que crea Martin.
Ese token es SOLO LECTURA por diseño de ParkaHub (cualquier POST/PUT devuelve 403), y acá además
solo se hacen GET a una lista cerrada de rutas.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

from . import ajustes, meli
from ..db import ahora

TIMEOUT = 20

# Rutas de ML que se pueden leer a través de /api/ml/raw (todo lo demás se rechaza acá mismo)
RAW_PERMITIDO = re.compile(r"^/(orders/\d+|items/MLA\d+|shipments/\d+|users/\d+/items/search)(\?.*)?$")


class ParkaHubError(Exception):
    pass


class _SinRedireccion(urllib.request.HTTPRedirectHandler):
    """Si Access no acepta el token redirige al login: lo tomamos como error, no seguimos."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_SinRedireccion)


def configurado() -> bool:
    """Solo ParkaHub (service token de Martin)."""
    return bool(ajustes.leer("parkahub_client_id") and ajustes.leer("parkahub_client_secret"))


def _directo() -> bool:
    """Sin ParkaHub pero con la clave del proxy de Costos: se consulta ML directo (services/meli.py)."""
    return not configurado() and meli.configurado()


def disponible() -> bool:
    """Hay de dónde sacar datos de ML: ParkaHub o el proxy de Costos."""
    return configurado() or meli.configurado()


def fuente() -> str:
    return "parkahub" if configurado() else ("directo" if meli.configurado() else "")


def _ml(path: str):
    """GET directo a ML por el proxy de Costos, con los errores traducidos a ParkaHubError."""
    try:
        return meli.get(path)
    except meli.MeliError as e:
        raise ParkaHubError(str(e)) from None


def _q(**kw) -> str:
    return urllib.parse.urlencode({k: v for k, v in kw.items() if v not in (None, "")})


def get(ruta: str, params: dict | None = None) -> dict:
    """GET a ParkaHub. `ruta` empieza con /api/."""
    if not ruta.startswith("/api/"):
        raise ParkaHubError("ruta inválida")
    cid, sec = ajustes.leer("parkahub_client_id"), ajustes.leer("parkahub_client_secret")
    if not (cid and sec):
        raise ParkaHubError("ParkaHub no está conectado: faltan el Client ID y el Client Secret "
                            "(se cargan en Conexiones; los genera Martin en Cloudflare Access).")
    base = ajustes.leer("parkahub_url").rstrip("/")
    q = ("?" + urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})) if params else ""
    req = urllib.request.Request(base + ruta + q, headers={
        "CF-Access-Client-Id": cid, "CF-Access-Client-Secret": sec,
        "Accept": "application/json", "User-Agent": "PARKA-Deposito/1.0"})
    try:
        with _opener.open(req, timeout=TIMEOUT) as r:
            cuerpo = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308, 401, 403):
            detalle = ""
            try:
                detalle = json.loads(e.read().decode("utf-8", "replace")).get("error", "")
            except Exception:  # noqa: BLE001
                pass
            if detalle == "service_token_read_only":
                raise ParkaHubError("ParkaHub solo permite lectura con este token.") from None
            raise ParkaHubError("ParkaHub rechazó el acceso: revisá el Client ID / Secret o pedile a Martin "
                                "que agregue el token a la política de Access.") from None
        if e.code == 503:
            raise ParkaHubError("Mercado Libre no respondió o está desconectado en ParkaHub.") from None
        raise ParkaHubError(f"ParkaHub respondió error {e.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ParkaHubError(f"No se pudo conectar con ParkaHub ({getattr(e, 'reason', e)}).") from None
    try:
        return json.loads(cuerpo)
    except ValueError:
        raise ParkaHubError("ParkaHub devolvió algo que no es JSON (¿pantalla de login de Access?).") from None


def raw(path: str) -> dict:
    if not RAW_PERMITIDO.match(path):
        raise ParkaHubError("consulta a ML no permitida")
    if _directo():
        return _ml(path) or {}
    d = get("/api/ml/raw", {"path": path})
    if not d.get("ok"):
        if d.get("httpStatus") == 404:
            raise ParkaHubError("Mercado Libre no lo encontró (404).")
        raise ParkaHubError(f"Mercado Libre respondió {d.get('httpStatus')}.")
    return d.get("body") or {}


# ───────────── Consultas de alto nivel (devuelven datos chicos, listos para el asistente o la UI) ─────────────

def estado() -> dict:
    if _directo():
        try:
            u = meli.yo()
        except meli.MeliError as e:
            raise ParkaHubError(str(e)) from None
        return {"ml_conectado": True, "fuente": "directo", "cuenta": u.get("nickname"), "actualizado": {},
                "salud_cron": None, "errores_escritura_24h": None}
    d = get("/api/status")
    wh = d.get("warehouse") or {}
    return {"ml_conectado": (d.get("ml") or {}).get("connected"),
            "actualizado": {"catalogo": wh.get("tsCat"), "ventas": wh.get("tsSales"), "precios": wh.get("tsPrices")},
            "salud_cron": (d.get("cron") or {}).get("health"), "errores_escritura_24h": d.get("writeErrors24h")}


def probar() -> dict:
    """Para el botón 'Probar conexión'."""
    salud = get("/api/status")
    return {"ok": True, "ml_conectado": bool((salud.get("ml") or {}).get("connected"))}


def _titulos(mlas: list[str]) -> dict:
    """Títulos de varias publicaciones en una sola llamada (si el perfil no deja, sin títulos)."""
    ids = [m for m in dict.fromkeys(mlas) if m][:20]
    if not ids:
        return {}
    try:
        d = _ml("/items?" + _q(ids=",".join(ids), attributes="id,title,attributes"))
    except ParkaHubError:
        return {}
    out = {}
    for r in d or []:
        b = (r or {}).get("body") or {}
        if b.get("id"):
            modelo = next((a.get("value_name") for a in b.get("attributes") or [] if a.get("id") == "MODEL"), None)
            out[b["id"]] = {"titulo": b.get("title"), "modelo": modelo}
    return out


def _preguntas_directo(estado_q: str, limite: int) -> list[dict]:
    sid = _seller()
    d = _ml("/questions/search?" + _q(seller_id=sid, status=estado_q, sort_fields="date_created",
                                      sort_types="DESC", limit=min(max(limite, 1), 50), api_version=4))
    qs = (d or {}).get("questions") or []
    tit = _titulos([q.get("item_id") for q in qs])
    out = []
    for q in qs[:limite]:
        t = tit.get(q.get("item_id")) or {}
        out.append({"id": q.get("id"), "fecha": q.get("date_created"), "mla": q.get("item_id"),
                    "publicacion": t.get("titulo"), "modelo": t.get("modelo"), "pregunta": q.get("text"),
                    "categoria": None, "sugerencia_parkahub": None,
                    "respuesta": (q.get("answer") or {}).get("text")})
    return out


def _seller() -> int:
    try:
        return meli.seller_id()
    except meli.MeliError as e:
        raise ParkaHubError(str(e)) from None


def preguntas_sin_responder(limite: int = 20) -> list[dict]:
    if _directo():
        return _preguntas_directo("UNANSWERED", limite)
    d = get("/api/questions")
    out = []
    for q in (d.get("pending") or [])[:limite]:
        out.append({"id": q.get("question_id"), "fecha": q.get("date_created"), "mla": q.get("item_id"),
                    "publicacion": q.get("title"), "modelo": q.get("model"), "pregunta": q.get("text"),
                    "categoria": q.get("category"), "sugerencia_parkahub": q.get("suggested")})
    return out


def _resumir_orden(o: dict) -> dict:
    items = []
    for it in o.get("order_items") or []:
        i = it.get("item") or {}
        var = ", ".join(f"{a.get('name')}: {a.get('value_name')}" for a in i.get("variation_attributes") or [])
        items.append({"mla": i.get("id"), "titulo": i.get("title"), "sku": i.get("seller_sku"),
                      "variante": var, "cantidad": it.get("quantity"), "precio": it.get("unit_price")})
    return {"venta": o.get("id"), "pack": o.get("pack_id"), "fecha": o.get("date_created"),
            "estado": o.get("status"), "total": o.get("total_amount"),
            "comprador": (o.get("buyer") or {}).get("nickname"),
            "envio_id": (o.get("shipping") or {}).get("id"), "items": items,
            "etiquetas": o.get("tags") or []}


def buscar_venta(numero: str) -> dict:
    n = re.sub(r"\D", "", str(numero))
    if not n:
        raise ParkaHubError("número de venta inválido")
    o = _resumir_orden(raw(f"/orders/{n}"))
    if o.get("envio_id") and _directo():
        try:
            v = verificar_envio(str(o["envio_id"]))
            o["envio"] = {"estado": v["estado"], "subestado": v["subestado"], "logistica": v["logistica"],
                          "veredicto_despacho": v["mensaje"], "detalle": v["detalle"]}
        except ParkaHubError:
            pass
    elif o.get("envio_id"):
        try:
            v = get("/api/dispatch/verify", {"code": o["envio_id"]})
            sh = v.get("shipment") or {}
            o["envio"] = {"estado": sh.get("status"), "subestado": sh.get("substatus"),
                          "logistica": sh.get("logistic_type"), "veredicto_despacho": v.get("message"), "detalle": v.get("detail")}
        except ParkaHubError:
            pass
    return o


def ver_publicacion(mla: str) -> dict:
    m = re.sub(r"[^A-Z0-9]", "", str(mla).upper())
    if not m.startswith("MLA"):
        m = "MLA" + m
    b = raw(f"/items/{m}")
    attrs = {a.get("id"): a.get("value_name") for a in b.get("attributes") or []}
    variaciones = []
    for v in b.get("variations") or []:
        comb = ", ".join(f"{a.get('name')}: {a.get('value_name')}" for a in v.get("attribute_combinations") or [])
        sku = next((a.get("value_name") for a in v.get("attributes") or [] if a.get("id") == "SELLER_SKU"), "")
        variaciones.append({"variante": comb, "stock": v.get("available_quantity"), "precio": v.get("price"), "sku": sku})
    return {"mla": b.get("id"), "titulo": b.get("title"), "estado": b.get("status"), "precio": b.get("price"),
            "stock_total": b.get("available_quantity"), "modelo": attrs.get("MODEL"), "marca": attrs.get("BRAND"),
            "tipo": {"gold_pro": "Premium", "gold_special": "Clásica"}.get(b.get("listing_type_id"), "Otro"),
            "envio_gratis": (b.get("shipping") or {}).get("free_shipping"), "link": b.get("permalink"),
            "variaciones": variaciones}


def buscar_publicaciones(texto: str) -> list[dict]:
    if _directo():
        d = _ml(f"/users/{_seller()}/items/search?" + _q(q=texto, limit=15))
        ids = (d or {}).get("results") or []
        try:
            filas = _ml("/items?" + _q(ids=",".join(ids[:20]), attributes="id,title,status,available_quantity,sold_quantity,attributes")) if ids else []
        except ParkaHubError:
            filas = [{"body": {"id": i}} for i in ids]
        out = []
        for r in filas or []:
            b = (r or {}).get("body") or {}
            modelo = next((a.get("value_name") for a in b.get("attributes") or [] if a.get("id") == "MODEL"), None)
            out.append({"mla": b.get("id"), "modelo": modelo, "titulo": b.get("title"), "estado": b.get("status"),
                        "stock": b.get("available_quantity"), "ventas_14d": None, "visitas_14d": None,
                        "vendidas_total": b.get("sold_quantity")})
        return out
    d = get("/api/ml/pub-search", {"q": texto})
    return [{"mla": h.get("id"), "modelo": h.get("model"), "titulo": h.get("title"), "estado": h.get("status"),
             "stock": h.get("stock"), "ventas_14d": h.get("sales"), "visitas_14d": h.get("vis")}
            for h in (d.get("hits") or [])[:15]]


def ventas_recientes(dias: int = 1, limite: int = 30) -> dict:
    desde = (ahora() - timedelta(days=max(0, dias))).strftime("%Y-%m-%dT00:00:00.000-03:00")
    if _directo():
        d = _ml("/orders/search?" + _q(seller=_seller(), **{"order.date_created.from": desde},
                                       sort="date_desc", limit=min(max(limite, 1), 50))) or {}
        ordenes = [_resumir_orden(o) for o in d.get("results") or []]
        return {"total": (d.get("paging") or {}).get("total"), "mostradas": len(ordenes), "ventas": ordenes}
    d = get("/api/ml/orders", {"from": desde, "limit": min(max(limite, 1), 50)})
    ordenes = [_resumir_orden(o) for o in d.get("results") or []]
    return {"total": (d.get("paging") or {}).get("total"), "mostradas": len(ordenes), "ventas": ordenes}


def devoluciones_ml(dias: int = 30) -> dict:
    if _directo():
        raise ParkaHubError("Las devoluciones por motivo/SKU las arma ParkaHub; con la conexión directa todavía no están.")
    d = get("/api/returns")
    limite = (ahora() - timedelta(days=dias)).strftime("%Y-%m-%d")
    filas = [r for r in d.get("rows") or [] if str(r.get("date_created") or "")[:10] >= limite]
    motivos: dict[str, int] = {}
    skus: dict[str, int] = {}
    for r in filas:
        motivos[r.get("reason_name") or "sin motivo"] = motivos.get(r.get("reason_name") or "sin motivo", 0) + 1
        if r.get("sku"):
            skus[r["sku"]] = skus.get(r["sku"], 0) + 1
    top = sorted(skus.items(), key=lambda x: -x[1])[:10]
    return {"dias": dias, "total": len(filas), "por_motivo": dict(sorted(motivos.items(), key=lambda x: -x[1])),
            "skus_mas_devueltos": [{"sku": s, "cantidad": n} for s, n in top]}


def reputacion() -> dict:
    if _directo():
        try:
            d = meli.yo().get("seller_reputation") or {}
        except meli.MeliError as e:
            raise ParkaHubError(str(e)) from None
        return {"nivel": d.get("level_id"), "mercadolider": d.get("power_seller_status"), "metricas": d.get("metrics")}
    d = get("/api/ml/reputation")
    return {"nivel": d.get("level_id"), "mercadolider": d.get("power_seller_status"), "metricas": d.get("metrics")}


def estado_envio(codigo: str) -> dict:
    if _directo():
        v = verificar_envio(codigo)
        return {"codigo": v["codigo"], "semaforo": {"green": "verde", "yellow": "amarillo", "red": "rojo"}.get(v["veredicto"]),
                "veredicto": v["mensaje"], "detalle": v["detalle"], "estado": v["estado"],
                "subestado": v["subestado"], "logistica": v["logistica"]}
    d = get("/api/dispatch/verify", {"code": codigo})
    sh = d.get("shipment") or {}
    return {"codigo": d.get("scanned"), "semaforo": {"green": "verde", "yellow": "amarillo", "red": "rojo"}.get(d.get("verdict"), d.get("verdict")),
            "veredicto": d.get("message"), "detalle": d.get("detail"),
            "estado": sh.get("status"), "subestado": sh.get("substatus"), "logistica": sh.get("logistic_type")}


# ───────────── Despacho: semáforo y cotejo (los mismos datos que usa el scanner de ParkaHub) ─────────────
# ParkaHub decide el veredicto con /api/dispatch/verify (envío en vivo de ML) y mantiene en su base la lista
# de envíos que ML da por "listos para despachar" (/api/dispatch/ready). Acá solo se leen: el despacho
# sigue guardándose en PARKA Depósito / Drive como siempre.

TIPOS_COTEJO = {"flex", "colecta"}


def verificar_envio(codigo: str) -> dict:
    """Semáforo de un envío. verde = se despacha; rojo = cancelada; amarillo = no corresponde (con motivo)."""
    n = re.sub(r"\D", "", str(codigo))
    if len(n) < 8:
        raise ParkaHubError("código de envío inválido")
    if _directo():
        return _verificar_directo(n)
    d = get("/api/dispatch/verify", {"code": n})
    sh = d.get("shipment") or {}
    return {"codigo": n, "veredicto": d.get("verdict"), "motivo": d.get("reason"),
            "mensaje": d.get("message"), "detalle": d.get("detail"),
            "estado": sh.get("status"), "subestado": sh.get("substatus"), "logistica": sh.get("logistic_type")}


LOGISTICAS_PROPIAS = {"self_service": "Flex", "cross_docking": "Colecta", "xd_drop_off": "Colecta"}


def _verificar_directo(n: str) -> dict:
    """Semáforo armado acá con el envío en vivo (/shipments/{id}) cuando no hay ParkaHub.
    verde = listo para despachar; rojo = cancelado; amarillo = ya salió / no está listo / otra logística."""
    try:
        sh = meli.get(f"/shipments/{n}") or {}
    except meli.MeliError as e:
        if e.status == 404:
            return {"codigo": n, "veredicto": "not_found", "motivo": "not_found", "mensaje": "ML no tiene ese envío",
                    "detalle": None, "estado": None, "subestado": None, "logistica": None}
        raise ParkaHubError(str(e)) from None
    st, sub, lg = sh.get("status"), sh.get("substatus"), sh.get("logistic_type")
    if st == "cancelled":
        v, motivo, msg = "red", "cancelled", "VENTA CANCELADA: no despachar"
    elif st in ("shipped", "delivered", "not_delivered"):
        v, motivo, msg = "yellow", "already_shipped", "Ya fue despachado" if st == "shipped" else "Ya fue entregado / cerrado"
    elif lg and lg not in LOGISTICAS_PROPIAS:
        v, motivo, msg = "yellow", "other_logistic", f"Otra logística ({lg}): no sale por acá"
    elif st == "ready_to_ship":
        v, motivo, msg = "green", None, f"Listo para despachar · {LOGISTICAS_PROPIAS.get(lg, lg or '')}".rstrip(" ·")
    else:
        v, motivo, msg = "yellow", "not_ready", f"Todavía no está listo en ML ({st}{' / ' + sub if sub else ''})"
    return {"codigo": n, "veredicto": v, "motivo": motivo, "mensaje": msg, "detalle": sub,
            "estado": st, "subestado": sub, "logistica": lg}


def listos_ml(tipo: str, refrescar: bool = False) -> dict:
    """Envíos que ML da por listos para este tipo (últimos 14 días, cache de ParkaHub)."""
    if _directo():
        raise ParkaHubError("Cotejar con ML usa la lista de envíos listos que arma ParkaHub; con la conexión directa no está disponible.")
    if tipo not in TIPOS_COTEJO:
        raise ParkaHubError("El cotejo con ML es para Flex y Colecta (Tienda Nube no pasa por ML).")
    return get("/api/dispatch/ready", {"tipo": tipo, "refresh": "1" if refrescar else None})


def _explicar_estado(crudo: str | None) -> str:
    if not crudo:
        return "ML no lo tiene en los últimos 14 días"
    st, sub, lg, _dia = (crudo.split("|") + ["", "", "", ""])[:4]
    textos = {"cancelled": "CANCELADO", "shipped": "ya despachado", "delivered": "ya entregado",
              "not_delivered": "no entregado", "pending": "en preparación", "handling": "en preparación",
              "ready_to_ship": "listo"}
    t = textos.get(st, st or "?")
    if sub:
        t += f" ({sub})"
    if lg and lg not in ("self_service", "cross_docking"):
        t += f" · otra logística: {lg}"
    return t


def cotejar(paquetes: list[dict], ready: list[dict], states: dict) -> dict:
    """Compara la lista cargada contra lo que ML da por listo.

    paquetes: [{tracking, estado}] (estado: pendiente|despachado). Devuelve qué falta cargar, qué sobra
    y qué está listo en ML pero todavía sin escanear.
    """
    en_lista = {re.sub(r"\D", "", str(p.get("tracking") or "")): p for p in paquetes}
    en_lista.pop("", None)
    listos = {str(r.get("shipment_id")): r for r in ready}
    solo_ml = [{"envio": k, "venta": r.get("order_id"), "dia": r.get("day")}
               for k, r in listos.items() if k not in en_lista]
    solo_lista = [{"envio": k, "estado_lista": p.get("estado"), "estado_ml": _explicar_estado(states.get(k)),
                   "cancelado": (states.get(k) or "").startswith("cancelled")}
                  for k, p in en_lista.items() if k not in listos]
    coinciden = [k for k in en_lista if k in listos]
    sin_escanear = [k for k in coinciden if en_lista[k].get("estado") != "despachado"]
    solo_lista.sort(key=lambda x: (not x["cancelado"], x["envio"]))
    return {"coinciden": len(coinciden), "sin_escanear": len(sin_escanear),
            "solo_ml": sorted(solo_ml, key=lambda x: str(x.get("dia") or "")),
            "solo_lista": solo_lista, "cancelados": sum(1 for x in solo_lista if x["cancelado"])}


def preguntas_respondidas(limite: int = 20) -> list[dict]:
    if _directo():
        return [{k: q[k] for k in ("id", "fecha", "mla", "publicacion", "pregunta", "respuesta")}
                for q in _preguntas_directo("ANSWERED", limite)]
    d = get("/api/questions")
    return [{"id": q.get("question_id"), "fecha": q.get("date_created"), "mla": q.get("item_id"),
             "publicacion": q.get("title"), "pregunta": q.get("text"), "respuesta": q.get("answer_text")}
            for q in (d.get("answered") or [])[:limite]]
